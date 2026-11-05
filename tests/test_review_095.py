"""0.95.0: Gesamtkorrektur „Buchungen prüfen“ – ergänzende Repository-Nachweise zu den externen Soll-Fällen
(``tests/external_095``): Randfälle der Gebührenerhaltung (N25), Lesestand (N19), Formulare ohne Eingabestand und
Sammelknopf (N13-Rest), Rücknahme älterer Aktionen (N20), Revisionsdomänen (N21) und Quellidentität (N23)."""

# ruff: noqa: F811  (Fixtures werden importiert und als Argumente verwendet)
from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.csvimport import batch as BA
from app.csvimport import model as M
from app.csvimport.identity import canonical_provider, note_identity_keys, provider_key
from app.csvimport.service import csv_service
from app.datasources import takeover as TK
from app.datasources.wallet import Move, TxView, classify
from app.diagnosis import actions as DA
from app.journal.service import journal_service
from tests.test_bitpanda import ctx
from tests.test_review_092 import _run_editor, _two_rows
from tests.test_review_fixes import c, cfg, form_values, post  # noqa: F401

D = Decimal
TS = datetime(2024, 1, 1, tzinfo=UTC)


def _event(tx: TxView) -> list[M.Rec]:
    recs = classify(tx)
    for i, r in enumerate(recs):
        r.account, r.event_key, r.event_line = "Ledger", "pulsechain:0xh:owner", i
        r.ext_id = f"{r.event_key}#{r.ext_id}"
    return recs


def _effect(recs) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for r in recs:
        for sym, q in ((r.in_sym, r.in_qty), (r.out_sym, -(r.out_qty or 0)), (r.fee_sym, -(r.fee_qty or 0))):
            if sym and q:
                out[sym] = out.get(sym, D(0)) + q
    return {k: v for k, v in out.items() if v}


# -- N25: Gebühr genau einmal, unabhängig nachgerechnet ------------------------------------------------------------

@pytest.mark.parametrize("case", ["fee_third_asset", "two_spam", "spam_out", "no_fee", "only_spam_left"])
def test_reclassification_conserves_raw_movements_and_the_paid_fee(c, case):
    """Nettowirkung je Asset nach der Neueinordnung == Rohbewegungen ohne ignorierte Token − einmal die Gebühr
    (unabhängig aus den Eingabedaten berechnet, nicht aus der Einordnung)."""
    moves = [Move("PLS", D(-2), "n:out"), Move("HEX@PLS:0xc", D(10), "t:1#0"), Move("X@PLS:0xx", D(99), "t:2#1")]
    fee, fee_asset, drop = D("0.01"), "PLS", {"X@PLS:0xx"}
    if case == "fee_third_asset":
        fee_asset = "ETH"
    elif case == "two_spam":
        moves.append(Move("Y@PLS:0xy", D(5), "t:3#2"))
        drop = {"X@PLS:0xx", "Y@PLS:0xy"}
    elif case == "spam_out":
        moves.append(Move("Y@PLS:0xy", D(-5), "t:3#2"))
        drop = {"X@PLS:0xx", "Y@PLS:0xy"}
    elif case == "no_fee":
        fee = D(0)
    elif case == "only_spam_left":  # nach dem Ignorieren bleibt nur ein Abgang
        moves = [Move("PLS", D(-2), "n:out"), Move("X@PLS:0xx", D(99), "t:2#1"), Move("Y@PLS:0xy", D(1), "t:3#2")]
        drop = {"X@PLS:0xx", "Y@PLS:0xy"}
    tx = TxView("pulsechain", "0xh", TS, moves, fee=fee, fee_asset=fee_asset, initiated=True, plain=False)
    recs = _event(tx)
    assert any(r.kind == M.REVIEW for r in recs)
    cx = ctx(c)
    for a in drop:
        csv_service(cx).set_symbol(provider_key(a, "pulsechain"), None, origin="spam")
    effective = TK._effective_recs(cx, recs, "pulsechain")
    want: dict[str, Decimal] = {}
    for m in moves:
        if m.asset not in drop:
            want[m.asset] = want.get(m.asset, D(0)) + m.qty
    if fee:
        want[fee_asset] = want.get(fee_asset, D(0)) - fee
    assert not any(r.kind == M.REVIEW for r in effective)
    assert _effect(effective) == {k: v for k, v in want.items() if v}
    fees = sum((r.fee_qty or D(0) for r in effective if r.fee_sym == fee_asset), D(0))
    assert fees == fee
    assert len({r.ext_id for r in effective}) == len(effective)  # Kennungen im Ereignis eindeutig


def test_failed_and_fee_only_transactions_are_untouched(c):
    cx = ctx(c)
    csv_service(cx).set_symbol(provider_key("X@PLS:0xx", "pulsechain"), None, origin="spam")
    failed = _event(TxView("pulsechain", "0xh", TS, [Move("X@PLS:0xx", D(1), "t:1#0")], fee=D("0.02"),
                           fee_asset="PLS", initiated=True, failed=True))
    fee_only = _event(TxView("pulsechain", "0xh", TS, [], fee=D("0.03"), fee_asset="PLS", initiated=True))
    for recs, fee in ((failed, D("0.02")), (fee_only, D("0.03"))):
        out = TK._effective_recs(cx, recs, "pulsechain")
        assert [(r.kind, r.fee_qty) for r in out] == [(M.FEE, fee)]


def test_inconsistent_reclassification_is_refused_and_stays_open(c, caplog):
    """Unabhängige Erhaltungsprüfung: stimmen die gespeicherten Bewegungen nicht mit der neuen Einordnung überein
    (hier: manipulierte Gebühr im Sammelvorgang), bleibt der Vorgang ungeklärt – nie eine falsche Buchung."""
    cx = ctx(c)
    tx = TxView("pulsechain", "0xh", TS, [Move("PLS", D(-2), "n:out"), Move("HEX@PLS:0xc", D(10), "t:1#0"),
                                         Move("X@PLS:0xx", D(99), "t:2#1")], fee=D("0.01"), fee_asset="PLS",
                initiated=True, plain=False)
    recs = _event(tx)
    review = next(r for r in recs if r.kind == M.REVIEW)
    review.raw["moves"]["moves"][0]["qty"] = "-3"  # Bewegungen ≠ Einordnung (simuliert fehlerhafte Neueinordnung)
    import app.datasources.wallet as W

    real = W.reclassify
    W_calls = []

    def wrong(rec, drop):  # Einordnung aus den ursprünglichen Bewegungen – passt nicht zu den gespeicherten
        W_calls.append(1)
        out = real(rec, drop)
        for r in out or []:
            if r.out_sym == "PLS":
                r.out_qty = D(2)
        return out

    W.reclassify = wrong
    try:
        csv_service(cx).set_symbol(provider_key("X@PLS:0xx", "pulsechain"), None, origin="spam")
        out = TK._effective_recs(cx, recs, "pulsechain")
    finally:
        W.reclassify = real
    assert W_calls and [r.kind for r in out] == [r.kind for r in recs]  # unverändert: Sammelvorgang + Gebühr
    assert "Erhaltungsprüfung" in caplog.text


# -- N19: Lesestand ---------------------------------------------------------------------------------------------

def test_snapshot_is_consistent_and_refuses_writes(c):
    cx, svc, bid, _mine, other = _two_rows(c)
    with cx.db.snapshot():
        before = svc.input_marks(bid)[other.idx]
        _run_editor(cx, lambda: svc.set_rows(bid, {f"val_{other.idx}": "7"}))  # anderer Thread schreibt
        assert svc.input_marks(bid)[other.idx] == before  # derselbe Stand bis zum Ende des Blocks
        with pytest.raises(RuntimeError, match="Lesestand"):
            cx.db.x("UPDATE csv_row SET line=line WHERE 0")
        with pytest.raises(RuntimeError, match="Lesestand"), cx.db.transaction():
            pass
    assert svc.input_marks(bid)[other.idx] != before
    cx.db.x("UPDATE csv_row SET line=line WHERE 0")  # danach wieder normal schreibbar


# -- N13-Rest: Formulare ohne Eingabestand, Sammelknopf ---------------------------------------------------------

def test_http_form_without_input_marks_is_rejected_visibly(c):
    cx, _svc, bid, _mine, other = _two_rows(c)
    r = post(c, f"/journal/csv/{bid}/rows", {f"val_{other.idx}": "50"})
    assert r.status_code == 200 and "neu laden" in r.text
    assert cx.db.scalar("SELECT value_in FROM csv_row WHERE id=?", (other.id,)) is None
    fields = form_values(c.get(f"/journal/csv/{bid}").text, f"/journal/csv/{bid}/rows")
    fields[f"val_{other.idx}"] = ["50"]
    assert post(c, f"/journal/csv/{bid}/rows", fields).status_code == 303
    assert cx.db.scalar("SELECT value_in FROM csv_row WHERE id=?", (other.id,)) == "50"


def test_bulk_button_owns_its_decisions_against_an_older_restore(c):
    cx, svc, bid, mine, _other = _two_rows(c)
    first = svc.set_rows(bid, {f"dec_{mine.idx}": "skip"})
    assert svc.set_all(bid, "duplicate", "skip") == 1  # gleicher Wert, ausdrücklich: neue Kennung
    assert svc.restore_inputs(bid, first["undo"]) == 0
    assert cx.db.scalar("SELECT decision FROM csv_row WHERE id=?", (mine.id,)) == "skip"


# -- N20: Rücknahme alter (vor 0.95.0 protokollierter) Aktionen ---------------------------------------------------

def test_action_logged_before_ownership_tokens_still_undoes_by_value(c):
    cx, svc, bid, mine, _other = _two_rows(c)
    p = BA.plan(svc, bid, "skip", {mine.idx})
    res = BA.execute(svc, bid, "skip", {mine.idx}, token=BA.new_token(), fingerprint=p.fingerprint())
    ops = json.loads(cx.db.scalar("SELECT ops_json FROM import_action WHERE id=?", (res["action_id"],)))
    for st in ops["rows"].values():  # Format vor 0.95.0: ohne Besitzkennung
        st["set"].pop("ver_dec", None)
    cx.db.x("UPDATE import_action SET ops_json=? WHERE id=?", (json.dumps(ops), res["action_id"]))
    assert not BA.undo(svc, res["action_id"])["errors"]
    assert cx.db.scalar("SELECT decision FROM csv_row WHERE id=?", (mine.id,)) is None


# -- N21: Revisionsdomänen --------------------------------------------------------------------------------------

def test_mapping_and_asset_writers_move_the_data_revision(c):
    cx = ctx(c)
    v0 = DA.data_version(cx.db)
    csv_service(cx).set_account("Kraken", "Kraken Spot")
    v1 = DA.data_version(cx.db)
    assert v1 != v0
    saved = journal_service(cx).save_asset({"asset_id": "ZZT", "name": "Test", "asset_class": "crypto",
                                            "quote_source": "none"})
    assert not saved.errors and DA.data_version(cx.db) != v1


# -- N23: kanonischer Anbieter ----------------------------------------------------------------------------------

def test_explicit_source_wins_over_account_label():
    assert canonical_provider("sync:pulsechain", None, "Ledger Bitpanda") == "pulsechain"
    assert canonical_provider("sync:bitpanda", None, "Ledger PLS") == "bitpanda"
    assert canonical_provider("csv:koinly", "koinly", "Bitpanda") == "bitpanda"  # Rückfall ohne Anbieterangabe
    uuid = "0f0e0d0c-0b0a-4908-8706-050403020100"
    assert not note_identity_keys("sync:pulsechain", "Bitpanda", f"txhash={uuid}")
    assert note_identity_keys("csv:koinly", "Bitpanda", f"txhash={uuid}")
