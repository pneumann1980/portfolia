"""Additional real entry points and revision boundaries in Portfolia 0.94.1."""

# ruff: noqa: F401, F811, S101
from __future__ import annotations

import json

import pytest

from app.csvimport.service import csv_service
from app.datasources import takeover as TK
from app.diagnosis import actions as DA
from app.diagnosis.engine import report_for
from app.journal.service import journal_service
from tests.test_bitpanda import api, ctx, master
from tests.test_datasources import client as source_client
from tests.test_datasources import fake
from tests.test_diagnosis_research import ACC, approve, recon_findings, research, staged_world, world
from tests.test_nachpruefung import _row, _without_counterpart
from tests.test_review_092 import _run_editor
from tests.test_takeover import staged
from tests.test_takeover_wallet_mapping import SPAM, TOK, _blocked_lines
from tests.test_wallets_pulse_peaq import FORK, A, X, pulse, tx
from tests.test_wallets_pulse_peaq import client as wallet_client
from tests.wallet_fakes import create_wallet, post

# world/staged_world use the fixture named "client".
client = source_client


@pytest.mark.parametrize("account", ["Ledger PLS", "Bitpanda", "Ledger Bitpanda"])
@pytest.mark.parametrize("action", ["spam", "map"])
def test_wallet_source_identity_is_independent_of_account_label(wallet_client, pulse, account, action):
    c = wallet_client
    pulse.chains[369]["tokentx"] = [
        {
            **tx(FORK + 300, 7, X, A, 5 * 10**18),
            "contractAddress": TOK,
            "tokenSymbol": "SPAM",
            "tokenName": "Spam",
            "tokenDecimal": "18",
        }
    ]
    pulse.chains[369]["tokenbalance"] = {TOK: str(5 * 10**18)}
    sid = create_wallet(c, "pulsechain", A, name="Source", account=account)
    assert not TK.run_fetch(ctx(c), sid)["errors"]
    initial = TK.assess(ctx(c), sid)
    assert any(p["kind"] == "map" and p["symbol"] == SPAM for p in initial.problems)
    values = {"symbol": SPAM, "action": action}
    if action == "map":
        saved = journal_service(ctx(c)).save_asset(
            {"asset_id": "TOKX", "name": "Known mapped token", "asset_class": "crypto", "quote_source": "none"}
        )
        assert not saved.errors
        values["asset"] = "TOKX"
    response = post(c, f"/settings/datasources/{sid}/uebernahme/zuordnen", **values)
    assert response.status_code == 303 and "error=" not in response.headers["location"]
    after = TK.assess(ctx(c), sid)
    assert not any(p["kind"] == "map" and p["symbol"] == SPAM for p in after.problems)
    assert SPAM not in _blocked_lines(after), {"account": account, "action": action, "blocker": _blocked_lines(after)}
    assert not ctx(c).db.scalar("SELECT COUNT(*) FROM journal_tx WHERE source='sync:pulsechain'")


@pytest.mark.parametrize("count", [2, 11, 12])
def test_takeover_candidate_cardinality_has_a_working_nopair_path(config, master, api, count):
    c, sid = _without_counterpart(config, api)
    try:
        cx = ctx(c)
        for i in range(count):
            saved = journal_service(cx).save(
                {
                    "kind": "expert",
                    "date": "2024-02-12",
                    "time": "13:30",
                    "type": "deposit",
                    "to_account": f"Ledger {i}",
                    "to_asset": "BTC",
                    "to_qty": "0.001",
                    "value_eur": "55",
                }
            )
            assert not saved.errors
        a = staged(c, sid)
        p = _row(a)
        assert p["partner"]["kind"] == "alts"
        assert not TK.decide(cx, sid, p["key"], "nopair", a.token)["errors"]
        ready = TK.assess(cx, sid)
        assert ready.ok, {
            "candidates": count,
            "stale": ready.stale,
            "blockers": [(b.key, b.detail) for b in ready.blockers],
        }
        assert not TK.apply(cx, sid, ready.token)["errors"]
        assert not TK.undo(cx, sid)["errors"]
    finally:
        c.__exit__(None, None, None)


@pytest.mark.parametrize(
    "actor", ["journal", "symbol", "account", "asset_new", "asset_edit", "noop_read", "rolled_back_write"]
)
def test_revision_contract_covers_actual_mapping_and_journal_writers(staged_world, actor):
    c, cx, _sid = staged_world
    csv = csv_service(cx)
    if actor == "asset_edit":
        saved = journal_service(cx).save_asset(
            {"asset_id": "NEWASSET", "name": "Original asset", "asset_class": "crypto", "quote_source": "none"}
        )
        assert not saved.errors
    version = DA.data_version(cx.db)
    if actor == "journal":
        saved = journal_service(cx).save(
            {
                "kind": "expert",
                "date": "2026-09-15",
                "time": "10:00",
                "type": "deposit",
                "to_account": "Test",
                "to_asset": "EUR",
                "to_qty": "5",
                "value_eur": "5",
            }
        )
        assert not saved.errors
    elif actor == "symbol":
        csv.set_symbol("UNMAPPED", "EUR")
    elif actor == "account":
        csv.set_account(ACC, "Other account")
    elif actor == "asset_new":
        saved = journal_service(cx).save_asset(
            {"asset_id": "NEWASSET", "name": "New asset", "asset_class": "crypto", "quote_source": "none"}
        )
        assert not saved.errors
    elif actor == "asset_edit":
        saved = journal_service(cx).save_asset(
            {"name": "Changed asset", "asset_class": "security", "quote_source": "none"}, asset_id="NEWASSET"
        )
        assert not saved.errors
    elif actor == "noop_read":
        c.get("/quality")
    else:
        with pytest.raises(RuntimeError, match="rollback"), cx.db.transaction() as conn:
            conn.execute("UPDATE journal_tx SET note='rolled back'")
            raise RuntimeError("rollback")
    changed = DA.data_version(cx.db) != version
    assert changed == (actor in ("journal", "symbol", "account", "asset_new", "asset_edit")), {
        "actor": actor,
        "revision_changed": changed,
    }


@pytest.mark.parametrize("mapping", ["none", "account", "symbol"])
def test_direct_diagnosis_booking_rechecks_mapping_state(staged_world, mapping):
    _c, cx, _sid = staged_world
    (f,) = recon_findings(cx)
    assert approve(cx, f.id, "stage")[1].ok
    (finding,) = recon_findings(cx)
    report = report_for(cx)
    plan = DA.build_plan(cx, report, finding, "book_row")
    assert not plan.errors and plan.ops
    original_account = plan.ops[0].row["to_account"]
    if mapping == "account":
        csv_service(cx).set_account(original_account, "Changed account")
    elif mapping == "symbol":
        csv_service(cx).set_symbol("EUR", None)
    active_before = cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE status='active'")
    # Same public confirmation service as the diagnosis page; its rebuild reads the staged row.
    result = DA.apply(cx, finding.id, "book_row", {}, plan.token)
    active_after = cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE status='active'")
    if mapping == "none":
        assert result.ok and active_after == active_before + 1
    else:
        assert not result.ok and active_after == active_before, {
            "mapping": mapping,
            "ok": result.ok,
            "errors": result.errors,
            "booked": active_after - active_before,
            "original_account": original_account,
        }
