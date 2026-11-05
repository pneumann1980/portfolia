"""Independent economic conservation checks for the changes introduced in 0.94.2."""

# ruff: noqa: F401, F811, S101
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.csvimport.identity import provider_key
from app.csvimport.service import csv_service
from app.datasources import takeover as TK
from app.datasources.wallet import Move, TxView, classify
from tests.test_bitpanda import ctx
from tests.test_review_fixes import c, cfg
from tests.test_takeover_wallet_review import (
    HEX,
    R,
    _apply_and_undo,
    _decide_rows,
    _resolve_maps,
    _swap_call,
    _swap_with_spam,
    _tok,
    priced,
)
from tests.test_wallets_pulse_peaq import FORK, WEI, A, H, pulse
from tests.test_wallets_pulse_peaq import client as wallet_client
from tests.wallet_fakes import create_wallet


@pytest.mark.parametrize("recognized_spam", [False, True])
@pytest.mark.parametrize("fee", ["0", "0.01"])
def test_reclassification_conserves_the_one_network_fee(c, recognized_spam, fee):
    drop = "SPAM@PLS:0xs"
    tx = TxView(
        "pulsechain",
        "0xh",
        datetime(2024, 1, 1, tzinfo=UTC),
        [
            Move("PLS", Decimal(-2), "n:out"),
            Move("HEX@PLS:0xc", Decimal(10), "t:1#0"),
            Move(drop, Decimal(99), "t:2#1", spam="advertisement" if recognized_spam else None),
        ],
        fee=Decimal(fee),
        fee_asset="PLS",
        initiated=True,
        plain=False,
    )
    recs = classify(tx)
    for i, r in enumerate(recs):
        r.account, r.event_key, r.event_line = "Ledger", "pulsechain:0xh:owner", i
        r.ext_id = f"{r.event_key}#{r.ext_id}"
    cx = ctx(c)
    csv_service(cx).set_symbol(provider_key(drop, "pulsechain"), None, origin="spam")
    effective = TK._effective_recs(cx, recs, "pulsechain")
    actual = sum((r.fee_qty or Decimal(0) for r in effective if r.fee_sym == "PLS"), Decimal(0))
    assert actual == tx.fee, {
        "original_fee": str(tx.fee),
        "effective_fee": str(actual),
        "records": [(r.kind, str(r.fee_qty), r.ext_id) for r in effective],
    }


@pytest.mark.parametrize("recognized_spam", [False, True])
def test_complete_wallet_takeover_books_the_raw_chain_fee_exactly_once(wallet_client, pulse, priced, recognized_spam):
    c = wallet_client
    sid = _swap_with_spam(c, pulse, "visit www.claim-now.io" if recognized_spam else "Totally Normal")
    _resolve_maps(c, sid, spam=("SPM1", "SPM2"))
    _decide_rows(c, sid)
    source_tx = _swap_call(FORK + 400, 20, 2 * WEI)
    expected = Decimal(source_tx["gasUsed"]) * Decimal(source_tx["gasPrice"]) / Decimal(10**18)
    booked = _apply_and_undo(c, sid)  # real rendered confirmation, background apply and complete undo
    own = [r for r in booked if H(20) in str(r.get("event_key") or r.get("external_id") or "")]
    assert own
    actual = sum((Decimal(r["fee_qty"] or "0") for r in own if r["fee_asset"] == "PLS"), Decimal(0))
    actual += sum(
        (
            Decimal(r["from_qty"] or "0")
            for r in own
            if r["type"] == "withdrawal" and r["tag"] == "fee" and r["from_asset"] == "PLS"
        ),
        Decimal(0),
    )
    assert actual == expected, {
        "recognized_spam": recognized_spam,
        "raw_chain_fee": str(expected),
        "booked_fee": str(actual),
        "rows": [(r["type"], r["tag"], r["from_qty"], r["fee_qty"]) for r in own],
    }


@pytest.mark.parametrize("later", ["none", "one_decision", "new_fetch", "concurrent"])
def test_new_bulk_takeover_entrypoint_rejects_stale_runs_and_serializes_decisions(wallet_client, pulse, priced, later):
    c = wallet_client
    d = pulse.chains[369]
    d["tokentx"] = []
    for i in range(2):
        d["txlist"].append(_swap_call(FORK + 400 + i, 30 + i, WEI // 10))
        d["tokentx"].append(_tok(FORK + 400 + i, 30 + i, R, A, 1, HEX, "HEX", "HEX"))
    d["tokenbalance"] = {HEX: str(2 * 10**18)}
    sid = create_wallet(c, "pulsechain", A, name="Ledger PLS")
    cx = ctx(c)
    assert not TK.run_fetch(cx, sid)["errors"]
    _resolve_maps(c, sid, spam=())
    shown = TK.assess(cx, sid, confirm=True)
    offered = [p for p in shown.problems if p.get("kind") == "row" and "include" in p.get("options", [])]
    assert len(offered) == 3
    if later == "one_decision":
        assert not TK.decide(cx, sid, offered[0]["key"], "include", shown.token, confirm=True)["errors"]
    elif later == "new_fetch":
        assert not TK.run_fetch(cx, sid)["errors"]
        assert TK.latest(cx.db, sid)["id"] != shown.rid
    before = json.dumps(cx.db.get_state(TK._dec_key(sid)), sort_keys=True)
    if later == "concurrent":

        def submit(choice):
            try:
                return TK.decide_many(cx, sid, choice, shown.token, confirm=True)
            finally:
                cx.db.close_thread_conn()

        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(submit, "include"), workers.submit(submit, "skip")]
            results = [f.result(timeout=20) for f in futures]
        assert sum(not r["errors"] for r in results) == 1, results
        decisions = TK.load_decisions(cx.db, sid, shown.rid)
        assert len(decisions) == 3 and len({r["choice"] for r in decisions.values()}) == 1
    else:
        result = TK.decide_many(cx, sid, "include", shown.token, confirm=True)
        if later == "none":
            assert not result["errors"] and result["n"] == 3
            assert len(TK.load_decisions(cx.db, sid, shown.rid)) == 3
            assert TK.assess(cx, sid, confirm=True).ok
        else:
            assert result["errors"] and result["n"] == 0
            assert json.dumps(cx.db.get_state(TK._dec_key(sid)), sort_keys=True) == before
