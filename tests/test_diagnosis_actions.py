"""Empfehlungen und Korrekturen aus der Diagnose – synthetische, anonymisierte Fälle.

Geprüft wird: Empfehlung und Alternativen je Befundtyp, Vorschau ohne Schreibzugriff mit berechneten Auswirkungen,
Übernehmen nur mit aktueller Vorschau (Prüfsumme), atomares Ausführen, „Rückgängig“ stellt den Ausgangszustand her
und überschreibt keine späteren Änderungen, „als geprüft markieren“ gilt nur bei unveränderten Befunddaten.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta
from decimal import Decimal

import pytest

from app.config import Config, Secrets
from app.diagnosis import actions as A
from app.diagnosis.engine import report_for
from app.diagnosis.recommend import recommend
from app.journal.service import journal_service
from app.prices.sources import source_service
from tests.helpers import ASSETS, tx
from tests.test_diagnosis import (
    NOW,
    TOKA_ASSETS,
    add_wallet,
    asset,
    by_kind,
    client_with_import,
    fingerprint,
    kaspa_like_rows,
    make_ctx,
    popkat_like_rows,
    th_like_ctx,
)

LOG_TABLES = ("event_log", "api_usage", "job_status", "source_status", "journal_log", "diag_decision",
              "sqlite_sequence")


@pytest.fixture
def cfg(tmp_path) -> Config:
    imp = tmp_path / "import"
    imp.mkdir(parents=True, exist_ok=True)
    return Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                  startup_jobs=False, log_format="text", secrets=Secrets())


def data_fp(db) -> str:
    """Prüfsumme der Nutzdaten (ohne Protokolle und Entscheidungen der Diagnose)."""
    h = hashlib.sha256()
    for (t,) in db.q("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        if t in LOG_TABLES:
            continue
        for r in db.q(f"SELECT * FROM {t} ORDER BY 1"):
            h.update(repr(tuple(r)).encode())
    return h.hexdigest()


def finding(ctx, kind: str, pred=lambda f: True):
    rep = report_for(ctx)
    (f,) = [x for x in by_kind(rep, kind) if pred(x)]
    return rep, f


def plan_for(ctx, kind, option, params=None, pred=lambda f: True, given=None):
    rep, f = finding(ctx, kind, pred)
    plan = A.build_plan(ctx, rep, f, option, params, given=params is not None if given is None else given)
    return rep, f, plan


def toka_ctx(cfg):
    holdings = [{"asset_id": "TOKA", "account": "Wallet K", "qty": "100246.91357802"}]
    return make_ctx(cfg, popkat_like_rows(), TOKA_ASSETS, holdings)


def is_toka(f) -> bool:
    return "TOKA" in f.title


# ----------------------------------------------------------------------------------------------------
# Empfehlung, Vorschau, Übernehmen, Rückgängig
# ----------------------------------------------------------------------------------------------------

def test_recommendation_offers_fix_alternatives_and_dismiss(cfg):
    ctx = toka_ctx(cfg)
    rep, f = finding(ctx, "duplicate", is_toka)
    rec = recommend(rep, f)
    assert not rec.conditional and "M1 ausblenden" in rec.text
    assert rec.primary is not None and rec.primary.key == "hide_weak"
    assert [o.key for o in rec.options] == ["hide_weak", "hide_strong", "hide_custom", "dismiss"]
    assert rec.options[-1].dismiss
    # Prüfen vor dem Übernehmen: Explorer-Links nur mit dem Hash (keine Mengen, Konten oder Werte)
    (text, links), = rec.checks
    assert "T1" in text and links and all(link.external for link in links)
    assert all(re.fullmatch(r"https://[a-z.]+/tx/0x[0-9a-f]{64}", link.url) for link in links)


def test_preview_computes_effects_without_writing(cfg):
    ctx = toka_ctx(cfg)
    before = fingerprint(ctx.db)
    rep, f, plan = plan_for(ctx, "duplicate", "hide_weak", pred=is_toka)
    assert not plan.errors and [(op.kind, op.target, op.origin) for op in plan.ops] == [("hide", "M1", "import")]
    assert plan.ops[0].ref is not None and plan.ops[0].ref.tx_id == "M1"
    eff = A.preview(ctx, rep, plan)
    (pos,) = [p for p in eff.positions if (p.account, p.asset) == ("Wallet K", "TOKA")]
    assert pos.bal == (Decimal("100246.91357802"), Decimal("50123.45678901"))
    assert pos.status == ("intern konsistent", "Import-Soll + Änderungen in Portfolia")
    assert eff.target_resolved and f.id in {x.id for x in eff.resolved}
    assert not eff.new and not eff.issues_new
    # konto-übergreifendes FIFO: die Transfergebühr verbraucht danach ein Lot mit Einstand → sichtbar je Jahr
    assert [(y.year, y.realized) for y in eff.years] == [(2025, (Decimal("0.00"), Decimal("-0.18")))]
    assert eff.tax_pack  # Steuerwerte mit dem Regelwerk gerechnet (hier: Zusammenfassung unverändert)
    assert fingerprint(ctx.db) == before
    assert not ctx.db.q("SELECT 1 FROM diag_decision")


def test_apply_hides_import_booking_and_undo_restores_exactly(cfg):
    ctx = toka_ctx(cfg)
    before = data_fp(ctx.db)
    _rep, f, plan = plan_for(ctx, "duplicate", "hide_weak", pred=is_toka)
    res = A.apply(ctx, f.id, "hide_weak", plan.params, plan.token)
    assert res.ok and res.decision_id
    ov = ctx.db.q1("SELECT * FROM tx_override WHERE tx_id='M1'")
    assert ov["action"] == "delete" and json.loads(ov["base_json"])["to_qty"] == "50123.45678901"
    assert ctx.ledger().balances[("Wallet K", "TOKA")] == Decimal("50123.45678901")
    rep2 = report_for(ctx)
    assert rep2.by_id(f.id) is None
    (row,) = [h for h in rep2.holdings if (h.account, h.asset) == ("Wallet K", "TOKA")]
    assert row.status == "intern_app" and "1 in Portfolia ausgeblendete Import-Buchung" in row.explanations[0]
    (d,) = A.decisions(ctx.db)
    assert d.active and d.action == "fix" and d.option == "hide_weak" and d.changes[0]["target"] == "M1"
    assert ctx.db.q1("SELECT 1 FROM journal_log WHERE action='diagnose_apply'")
    # Rückgängig: Ausgangszustand exakt wiederhergestellt
    res2 = A.undo(ctx, d.id)
    assert res2.ok
    assert data_fp(ctx.db) == before
    assert ctx.ledger().balances[("Wallet K", "TOKA")] == Decimal("100246.91357802")
    assert report_for(ctx).by_id(f.id) is not None
    assert not A.decisions(ctx.db)[0].active
    assert not A.undo(ctx, d.id).ok  # zweimal geht nicht


def test_stale_preview_is_rejected_and_nothing_changes(cfg):
    ctx = toka_ctx(cfg)
    _rep, f, plan = plan_for(ctx, "duplicate", "hide_weak", pred=is_toka)
    assert journal_service(ctx).delete_import("F1")  # andere Änderung zwischen Vorschau und Übernehmen
    before = data_fp(ctx.db)
    res = A.apply(ctx, f.id, "hide_weak", plan.params, plan.token)
    assert not res.ok and "nicht mehr aktuell" in res.errors[0]
    assert data_fp(ctx.db) == before and not ctx.db.q("SELECT 1 FROM diag_decision")
    # Auswahl manipuliert (andere Lösung, gleiche Prüfsumme) → abgelehnt
    _rep, f, plan = plan_for(ctx, "duplicate", "hide_weak", pred=is_toka)
    assert not A.apply(ctx, f.id, "hide_strong", plan.params, plan.token).ok
    assert not A.apply(ctx, "f-000000000000", "hide_weak", {}, plan.token).ok
    assert data_fp(ctx.db) == before


def test_undo_does_not_override_a_later_manual_restore(cfg):
    ctx = toka_ctx(cfg)
    _rep, f, plan = plan_for(ctx, "duplicate", "hide_weak", pred=is_toka)
    did = A.apply(ctx, f.id, "hide_weak", plan.params, plan.token).decision_id
    assert journal_service(ctx).restore_import("M1")  # Nutzer stellt die Buchung selbst wieder her
    res = A.undo(ctx, did)
    assert res.ok and "zählt bereits wieder" in res.message
    assert not ctx.db.q("SELECT 1 FROM tx_override")


# ----------------------------------------------------------------------------------------------------
# Lösungen je Befundtyp
# ----------------------------------------------------------------------------------------------------

def test_hash_pairs_partial_selection(cfg):
    ctx = make_ctx(cfg, kaspa_like_rows(), ASSETS)
    rep, f = finding(ctx, "duplicate", lambda x: x.key.startswith("hash|"))
    opt = recommend(rep, f).option("hide_second")
    assert opt is not None and opt.recommended and len(opt.params[0].default) == 3
    _rep, f, plan = plan_for(ctx, "duplicate", "hide_second", {"pairs": ["P0a|P0b"]},
                             pred=lambda x: x.key.startswith("hash|"))
    assert [op.target for op in plan.ops] == ["P0b"]
    eff = A.preview(ctx, rep, plan)
    assert not eff.target_resolved and any("2 Paare" in x.title for x in eff.new)
    assert A.apply(ctx, f.id, "hide_second", plan.params, plan.token).ok
    assert ctx.ledger().balances[("Wallet A", "KAS")] == Decimal("517.25")
    # leere oder fremde Auswahl → keine Änderung
    rep, f = finding(ctx, "duplicate", lambda x: x.key.startswith("hash|"))
    assert A.build_plan(ctx, rep, f, "hide_second", {}, given=True).errors
    assert A.build_plan(ctx, rep, f, "hide_second", {"pairs": ["X|Y"]}, given=True).errors


def journal_row(ctx, tx_id: str, uuid: str) -> None:
    stamp = "2025-06-01T00:00:00Z"
    ctx.db.x("INSERT INTO journal_tx(tx_id, source, external_id, status, ts_utc, type, from_account, from_asset, "
             "from_qty, to_account, to_asset, to_qty, value_eur, created_at, updated_at, event_key) VALUES "
             "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (tx_id, "sync:bitpanda", f"bitpanda:{uuid}#0", "active", "2025-05-02T10:00:01Z", "buy", "Bitpanda", "EUR",
              "100", "Bitpanda", "BTC", "0.002", "100", stamp, stamp, f"bitpanda:{uuid}"))
    ctx.invalidate_overlay()


def test_app_booking_marked_as_contained_in_import(cfg):
    uuid = "0a1b2c3d-1111-4222-8333-444455556666"
    r = tx("KX", "2025-05-02T10:00:00Z", "buy", frm=("Bitpanda", "EUR", "100"), to=("Bitpanda", "BTC", "0.002"),
           value="100")
    r["source"], r["source_ref"], r["note"] = "koinly", "KOINLYX", f"txhash={uuid}"
    ctx = make_ctx(cfg, [r], ASSETS)
    journal_row(ctx, "PF-S-000001", uuid)
    pred = lambda x: x.key.startswith("id|")  # noqa: E731
    rep, f = finding(ctx, "duplicate", pred)
    rec = recommend(rep, f)
    assert rec.primary.key == "cover" and rec.option("hide_import") is not None
    _rep, f, plan = plan_for(ctx, "duplicate", "cover", pred=pred)
    assert [(op.kind, op.target, op.link) for op in plan.ops] == [("cover", "PF-S-000001", "KX")]
    assert ctx.ledger().balances[("Bitpanda", "BTC")] == Decimal("0.004")
    did = A.apply(ctx, f.id, "cover", plan.params, plan.token).decision_id
    assert ctx.db.q1("SELECT decision FROM journal_import_link")["decision"] == "covered"
    assert ctx.ledger().balances[("Bitpanda", "BTC")] == Decimal("0.002")
    assert ctx.db.q1("SELECT status FROM journal_tx")["status"] == "active"  # bleibt erhalten
    assert A.undo(ctx, did).ok
    assert not ctx.db.q("SELECT 1 FROM journal_import_link")
    assert ctx.ledger().balances[("Bitpanda", "BTC")] == Decimal("0.004")


def transfer_rows() -> list[dict]:
    return [tx("E0", "2025-02-01T09:00:00Z", "deposit", to=("Börse X", "ETH", "2"), value="5000"),
            tx("W1", "2025-02-02T10:00:00Z", "withdrawal", frm=("Börse X", "ETH", "1")),
            tx("D1", "2025-02-02T10:40:00Z", "deposit", to=("Wallet B", "ETH", "0.998"), value="2500"),
            tx("S1", "2025-08-02T10:40:00Z", "sell", frm=("Wallet B", "ETH", "0.5"), to=("Wallet B", "EUR", "2000"),
               value="2000")]


def test_transfer_link_creates_transfer_and_shows_tax_effect(cfg):
    ctx = make_ctx(cfg, transfer_rows(), ASSETS)
    rep, f, plan = plan_for(ctx, "transfer", "link")
    assert [(op.kind, op.mode or op.origin) for op in plan.ops] == [("create", ""), ("hide", "import"),
                                                                     ("hide", "import")]
    new = plan.ops[0].tx
    assert (new.type, new.from_account, new.to_account, new.from_qty, new.to_qty) == (
        "transfer", "Börse X", "Wallet B", Decimal("1"), Decimal("0.998"))
    eff = A.preview(ctx, rep, plan)
    # Bestände bleiben, Einstand und Haltedauer wandern mit: Steuerbericht 2025 zeigt die Änderung
    assert all(p.bal[0] == p.bal[1] for p in eff.positions)
    tax = {t.label: (t.before, t.after) for t in eff.tax if t.year == 2025}
    assert tax["Veräußerungen innerhalb der Haltefrist – Saldo"] == (Decimal("747.49"), Decimal("750.00"))
    did = A.apply(ctx, f.id, "link", plan.params, plan.token).decision_id
    row = ctx.db.q1("SELECT * FROM journal_tx")
    assert row["tx_id"].startswith("PF-D-") and row["source"] == "diagnose" and row["pair_refs"] == "W1,D1"
    assert {r["tx_id"] for r in ctx.db.q("SELECT tx_id FROM tx_override WHERE action='delete'")} == {"W1", "D1"}
    lots = [lot for lot in ctx.ledger().lots if lot.account == "Wallet B"]
    assert lots and all(str(lot.acq_date) == "2025-02-01" for lot in lots)  # Anschaffungsdatum des Abgangskontos
    assert not by_kind(report_for(ctx), "transfer")
    assert A.undo(ctx, did).ok
    assert ctx.db.q1("SELECT status FROM journal_tx")["status"] == "reverted"
    assert not ctx.db.q("SELECT 1 FROM tx_override")
    assert by_kind(report_for(ctx), "transfer")


def test_provider_quote_override_and_undo(cfg):
    ctx = th_like_ctx(cfg)
    rep, f, plan = plan_for(ctx, "asset", "set_quote", pred=lambda x: x.key.startswith("provider|"))
    (op,) = plan.ops
    assert (op.kind, op.value, op.mode) == ("quote", "threshold-network-token", "override")
    eff = A.preview(ctx, rep, plan)
    (pos,) = eff.positions
    assert pos.value == (Decimal("100.00"), None) and "Kursabruf" in pos.note
    assert eff.target_resolved and any("Kurse werden nach dem Übernehmen" in n for n in eff.notes)
    did = A.apply(ctx, f.id, "set_quote", plan.params, plan.token).decision_id
    assert ctx.portfolio().assets["TH"].quote_id == "threshold-network-token"  # ersetzt die Import-Kursquelle
    assert ctx.db.q1("SELECT origin FROM asset_source WHERE asset_id='TH'")["origin"] == "override"
    assert A.undo(ctx, did).ok
    assert ctx.portfolio().assets["TH"].quote_id == "team-heretics-fan-token"
    assert not ctx.db.q("SELECT 1 FROM asset_source")


def test_undo_refuses_when_quote_was_changed_afterwards(cfg):
    ctx = th_like_ctx(cfg)
    _rep, f, plan = plan_for(ctx, "asset", "set_quote", pred=lambda x: x.key.startswith("provider|"))
    did = A.apply(ctx, f.id, "set_quote", plan.params, plan.token).decision_id
    assert source_service(ctx).accept("TH", "other-coin") is None  # spätere eigene Entscheidung
    before = data_fp(ctx.db)
    res = A.undo(ctx, did)
    assert not res.ok and "nach der Korrektur geändert" in res.errors[0]
    assert data_fp(ctx.db) == before and A.decisions(ctx.db)[0].active


def test_custom_coin_is_validated(cfg):
    ctx = th_like_ctx(cfg)
    pred = lambda x: x.key.startswith("provider|")  # noqa: E731
    _rep, _f, plan = plan_for(ctx, "asset", "set_quote_custom", {"coin": ["kein gültiger Wert!"]}, pred=pred)
    assert plan.errors and "Ungültige CoinGecko-ID" in plan.errors[0]
    _rep, _f, plan = plan_for(ctx, "asset", "set_quote_custom",
                              {"coin": ["https://www.coingecko.com/de/munze/threshold-network-token"]}, pred=pred)
    assert not plan.errors and plan.ops[0].value == "threshold-network-token"


def test_accept_suggestion_restores_previous_row_on_undo(cfg):
    rows = [tx("O2", "2025-01-06T10:00:00Z", "deposit", to=("Börse M", "BRX", "1000"), value="0")]
    ctx = make_ctx(cfg, rows, [*ASSETS, asset("BRX")])
    ctx.db.x("INSERT INTO asset_source(asset_id, quote_source, quote_id, status, origin, confidence, reason, "
             "candidates_json, checked_at, updated_at) VALUES ('BRX','coingecko','brx-coin','suggested','auto','hoch',"
             "'Symbol + Chain','[]','2025-06-01T00:00:00Z','2025-06-01T00:00:00Z')")
    ctx.invalidate_overlay()
    before = [tuple(r) for r in ctx.db.q("SELECT * FROM asset_source")]
    rep, f = finding(ctx, "price", lambda x: x.key == "price|BRX")
    assert recommend(rep, f).primary.key == "accept_suggestion"
    _rep, f, plan = plan_for(ctx, "price", "accept_suggestion", pred=lambda x: x.key == "price|BRX")
    assert plan.ops[0].mode == "user"  # Import ohne Kursquelle → normale Zuordnung
    did = A.apply(ctx, f.id, "accept_suggestion", plan.params, plan.token).decision_id
    assert ctx.db.q1("SELECT status, origin FROM asset_source")["status"] == "active"
    assert A.undo(ctx, did).ok
    assert [tuple(r) for r in ctx.db.q("SELECT * FROM asset_source")] == before


def test_holdings_adjustment_is_an_explicit_alternative(cfg):
    rows = [tx("H1", "2025-03-01T00:00:00Z", "deposit", to=("Wallet 2", "KAS", "200"), value="20")]
    ctx = make_ctx(cfg, rows, ASSETS)
    add_wallet(ctx, "Wallet 2", {"KAS": "199.5"}, observed=NOW - timedelta(hours=1), complete=True, sid=2)
    rep, f = finding(ctx, "holdings")
    rec = recommend(rep, f)
    assert rec.primary is None and rec.conditional  # keine Ausgleichsbuchung als Empfehlung
    opt = rec.option("adjust")
    assert opt is not None and "Notlösung" in opt.summary and opt.caution
    _rep, f, plan = plan_for(ctx, "holdings", "adjust", {"tag": ["fee"], "value": ["0"], "date": ["2026-01-15"]})
    (op,) = plan.ops
    assert (op.tx.type, op.tx.tag, op.tx.from_qty, op.tx.from_account) == ("withdrawal", "fee", Decimal("0.5"),
                                                                           "Wallet 2")
    eff = A.preview(ctx, rep, plan)
    (pos,) = eff.positions
    assert pos.status == ("Differenz zur externen Quelle", "mit externer Quelle abgestimmt")
    did = A.apply(ctx, f.id, "adjust", plan.params, plan.token).decision_id
    assert [h.status for h in report_for(ctx).holdings] == ["extern_ok"]
    assert A.undo(ctx, did).ok
    assert [h.status for h in report_for(ctx).holdings] == ["extern_diff"]
    # Eingaben werden geprüft
    rep, f = finding(ctx, "holdings")
    for bad in ({"tag": ["reward"]}, {"value": ["-1"]}, {"date": ["2999-01-01"]}, {"value": ["abc"]}):
        params = {"tag": ["fee"], "value": ["0"], "date": ["2026-01-15"], **bad}
        assert A.build_plan(ctx, rep, f, "adjust", params, given=True).errors, bad


def test_migration_booking_moves_cost_basis(cfg):
    rows = [tx("R1", "2025-01-10T10:00:00Z", "deposit", to=("Wallet V", "RPX", "123456789.5"), value="10"),
            tx("R2", "2025-05-10T10:00:00Z", "deposit", to=("Wallet V", "RPX#2", "123.4567895"), value="0")]
    ctx = make_ctx(cfg, rows, [*ASSETS, asset("RPX"), asset("RPX#2", "RPX (zweite ID)")])
    rep, f = finding(ctx, "migration")
    assert recommend(rep, f).primary is None  # ohne Contract-Prüfung keine Empfehlung zur Buchung
    _rep, f, plan = plan_for(ctx, "migration", "book_migration")
    assert [op.kind for op in plan.ops] == ["create", "hide"] and plan.ops[0].tx.tag == "migration"
    did = A.apply(ctx, f.id, "book_migration", plan.params, plan.token).decision_id
    led = ctx.ledger()
    assert ("Wallet V", "RPX") not in led.balances or not led.balances[("Wallet V", "RPX")]
    (lot,) = [x for x in led.lots if x.asset == "RPX#2"]
    assert lot.cost == Decimal("10") and str(lot.acq_date) == "2025-01-10"
    assert A.undo(ctx, did).ok
    assert ctx.ledger().balances[("Wallet V", "RPX")] == Decimal("123456789.5")


def test_contract_mapping_can_be_removed_and_restored(cfg):
    ctx = make_ctx(cfg, [tx("U1", "2025-01-10T10:00:00Z", "deposit", to=("Wallet E", "USDT", "5"), value="5")],
                   [*ASSETS, asset("USDT", "Tether", "coingecko", "tether")])
    a, b = "USDT@ETH:0X" + "A" * 40, "USDT@ETH:0X" + "B" * 40
    for sym in (a, b):
        ctx.db.x("INSERT INTO csv_symbol(symbol, asset_id, updated_at) VALUES (?,?,?)", (sym, "USDT", "2025-01-01"))
    rep, f = finding(ctx, "asset", lambda x: x.key.startswith("contracts|"))
    opt = recommend(rep, f).option("unmap")
    assert opt is not None and not opt.recommended and {v for v, _l in opt.params[0].choices} == {a, b}
    _rep, f, plan = plan_for(ctx, "asset", "unmap", {"symbol": [b]}, pred=lambda x: x.key.startswith("contracts|"))
    assert [(op.kind, op.target) for op in plan.ops] == [("unmap", b)]
    did = A.apply(ctx, f.id, "unmap", plan.params, plan.token).decision_id
    assert {r["symbol"] for r in ctx.db.q("SELECT symbol FROM csv_symbol")} == {a}
    assert A.undo(ctx, did).ok
    assert {r["symbol"] for r in ctx.db.q("SELECT symbol FROM csv_symbol")} == {a, b}


def test_custom_hide_selection(cfg):
    ctx = toka_ctx(cfg)
    rep, f = finding(ctx, "duplicate", is_toka)
    assert A.build_plan(ctx, rep, f, "hide_custom", {}, given=True).errors  # nichts ausgewählt
    assert A.build_plan(ctx, rep, f, "hide_custom", {"txs": ["B1"]}, given=True).errors  # nicht betroffen
    plan = A.build_plan(ctx, rep, f, "hide_custom", {"txs": ["M1", "T1"]}, given=True)
    assert [op.target for op in plan.ops] == ["M1", "T1"] and not plan.errors
    assert A.build_plan(ctx, rep, f, "dismiss").errors  # „geprüft“ ist keine Korrektur


# ----------------------------------------------------------------------------------------------------
# „Als geprüft markieren“
# ----------------------------------------------------------------------------------------------------

def test_dismiss_holds_until_finding_data_changes(cfg):
    rows = [tx("H1", "2025-03-01T00:00:00Z", "deposit", to=("Wallet 2", "KAS", "200"), value="20")]
    ctx = make_ctx(cfg, rows, ASSETS)
    add_wallet(ctx, "Wallet 2", {"KAS": "199.5"}, observed=NOW - timedelta(hours=1), complete=True, sid=2)
    before = data_fp(ctx.db)
    _rep, f = finding(ctx, "holdings")
    res = A.dismiss(ctx, f.id, "Gebühr beim Anbieter, belegt")
    assert res.ok and data_fp(ctx.db) == before  # ändert keine Daten
    marks = A.active_dismissals(ctx.db)
    assert marks[f.id].fingerprint == A.fingerprint(f) and marks[f.id].note == "Gebühr beim Anbieter, belegt"
    # neuer Abruf mit gleicher Menge (nur Abrufzeit neu) → bleibt geprüft
    ctx.db.x("UPDATE ds_balance SET observed_at=? WHERE source_id=2", ((NOW - timedelta(minutes=5)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"),))
    _rep, f2 = finding(ctx, "holdings")
    assert f2.id == f.id and A.fingerprint(f2) == marks[f.id].fingerprint
    # andere Differenz → Daten geändert → Markierung gilt nicht mehr
    ctx.db.x("UPDATE ds_balance SET qty='150' WHERE source_id=2")
    _rep, f3 = finding(ctx, "holdings")
    assert f3.id == f.id and A.fingerprint(f3) != marks[f.id].fingerprint
    assert A.reopen(ctx, marks[f.id].id).ok and not A.active_dismissals(ctx.db)


# ----------------------------------------------------------------------------------------------------
# Oberfläche
# ----------------------------------------------------------------------------------------------------

def test_web_flow_preview_apply_undo(cfg):
    c = client_with_import(cfg, popkat_like_rows(), TOKA_ASSETS, valuation="2025-06-30")
    ctx = c.app.state.ctx
    try:
        _rep, f = finding(ctx, "duplicate", is_toka)
        page = c.get(f"/quality/diagnose?f={f.id}")
        assert page.status_code == 200
        html = page.text
        assert "Empfehlung" in html and f"/quality/diagnose/plan?f={f.id}&amp;o=hide_weak" in html
        assert "Als geprüft markieren" in html or "als geprüft markieren" in html
        forms = re.findall(r'<form method="post" action="([^"]+)"', html[html.index('class="stack diag"'):])
        assert forms and all(a.startswith("/quality/diagnose/") for a in forms)
        before = fingerprint(ctx.db)
        prev = c.get(f"/quality/diagnose/plan?f={f.id}&o=hide_weak")
        assert prev.status_code == 200 and "Vorschau – noch nichts geändert" in prev.text
        assert "Was geändert wird (1)" in prev.text and "Ausblenden: Import-Buchung M1" in prev.text
        assert "Bestände und Einstand je Konto" in prev.text and "Der Befund ist danach erledigt" in prev.text
        assert fingerprint(ctx.db) == before
        token = re.search(r'name="token" value="([0-9a-f]+)"', prev.text).group(1)
        form = {"f": f.id, "o": "hide_weak", "token": token, "p__set": "1"}
        r = c.post("/quality/diagnose/apply", data=form, follow_redirects=False)
        assert r.status_code == 403 and fingerprint(ctx.db) == before  # ohne CSRF-Token
        r = c.post("/quality/diagnose/apply", data={**form, "csrf_token": c.token}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/quality/diagnose?msg=")
        assert ctx.db.q1("SELECT action FROM tx_override WHERE tx_id='M1'")["action"] == "delete"
        page = c.get(r.headers["location"])
        assert "Übernommen: M1 ausblenden" in page.text and "Rückgängig" in page.text
        # zweites Übernehmen derselben Vorschau → Befund besteht nicht mehr
        r2 = c.post("/quality/diagnose/apply", data={**form, "csrf_token": c.token}, follow_redirects=False)
        assert r2.status_code == 404 and "besteht nicht mehr" in r2.text
        did = A.decisions(ctx.db)[0].id
        r = c.post(f"/quality/diagnose/decision/{did}/undo", data={"csrf_token": c.token}, follow_redirects=False)
        assert r.status_code == 303 and not ctx.db.q("SELECT 1 FROM tx_override")
        # als geprüft markieren und wieder öffnen
        r = c.post("/quality/diagnose/dismiss", data={"csrf_token": c.token, "f": f.id, "note": "geprüft"},
                   follow_redirects=False)
        assert r.status_code == 303
        page = c.get("/quality/diagnose").text
        assert "Als geprüft markiert (1)" in page and 'id="checked"' in page
        did = A.active_dismissals(ctx.db)[f.id].id
        c.post(f"/quality/diagnose/decision/{did}/reopen", data={"csrf_token": c.token})
        assert not A.active_dismissals(ctx.db)
    finally:
        c.__exit__(None, None, None)


def test_dismissals_travel_with_the_full_export(cfg):
    from app.fullexport import _state, apply, collect, summary

    ctx = toka_ctx(cfg)
    _rep, f = finding(ctx, "duplicate", is_toka)
    A.dismiss(ctx, f.id, "zwei Eingänge belegt")
    extras = collect(ctx, set())
    st = _state(extras)
    assert st["diag_dismissed"][0]["finding_id"] == f.id and summary(extras)["checked"] == 1
    ctx.db.x("DELETE FROM diag_decision")
    iid = ctx.active_import_id()
    ctx.db.x("INSERT OR REPLACE INTO import_extra(import_id, name, data) VALUES (?,?,?)",
             (iid, "state.json", extras["state.json"]))
    counts = apply(ctx, iid)
    assert counts["checked"] == 1 and A.active_dismissals(ctx.db)[f.id].note == "zwei Eingänge belegt"
    assert apply(ctx, iid)["checked"] == 0  # nicht doppelt
