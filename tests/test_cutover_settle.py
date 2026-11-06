"""0.102.0: Ausgleich zum Stichtag – alles glattziehen, Haltefrist-Reset ausdrücklich, Freigabe danach neu."""

# ruff: noqa: F811
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.cutover import core as CO
from app.cutover import fix as FX
from app.cutover import service as CS
from app.cutover import settle as ST
from tests.helpers import ASSETS, tx
from tests.test_cutover_fix import _fix_all, _rows
from tests.test_diagnosis import client_with_import
from tests.test_review_fixes import cfg  # noqa: F401

D = Decimal
CUT = date(2025, 1, 1)


def _ref(ctx, account, asset, qty, as_of):
    ctx.db.x("INSERT INTO reference_balance(account, asset_id, qty, as_of, source, created_at) VALUES (?,?,?,?,?,?)",
             (account, asset, qty, as_of, "statement", "2026-01-01"))
    ctx.invalidate_overlay()


@pytest.fixture
def cx(cfg):
    c = client_with_import(cfg, _rows(), ASSETS, valuation="2025-03-31")
    try:
        yield c
    finally:
        c.__exit__(None, None, None)


def _plan(ctx):
    return ST.plan(ctx, CO.models(ctx, CUT)[0])


def _holdings(ctx, account, asset):
    return ctx.ledger().balances.get((account, asset), D(0))


def test_plan_books_remove_and_add_and_marks_sales(cx):
    ctx = cx.app.state.ctx
    _ref(ctx, "Wallet", "ETH", "1.5", "2025-03-15")  # berechnet 2 → Abgang 0,5
    _ref(ctx, "Börse", "BTC", "0.5", "2025-03-31")  # berechnet 0,4 → Zugang 0,1 (Verkauf S1 ab Stichtag)
    p = _plan(ctx)
    by = {(r["account"], r["asset"]): r for r in p["rows"]}
    eth, btc = by[("Wallet", "ETH")], by[("Börse", "BTC")]
    assert (eth["kind"], eth["qty"], eth["sold"]) == ("remove", D("0.5"), False)
    assert (btc["kind"], btc["qty"], btc["sold"]) == ("add", D("0.1"), True)
    assert p["n_add"] == 1 and p["n_remove"] == 1 and p["n_sold"] == 1
    res = ST.apply(ctx, CUT, {eth["id"], btc["id"]})
    assert not res["errors"] and len(res["done"]) == 2 and not res["failed"]
    assert _holdings(ctx, "Wallet", "ETH") == D("1.5") and _holdings(ctx, "Börse", "BTC") == D("0.5")
    assert _plan(ctx)["rows"] == []  # nichts mehr auszugleichen
    rows = {r["asset_id"]: r for r in ctx.db.q("SELECT * FROM cutover_correction WHERE status='active'")}
    assert {r["mode"] for r in rows.values()} == {"settle"}
    note = ctx.db.q1("SELECT note, ts_utc FROM journal_tx WHERE tx_id=?", (rows["BTC"]["tx_id"],))
    assert note["note"].startswith("Stichtag-Ausgleich 01.01.2025:") and note["note"].endswith("Haltefrist neu (0 €)")
    assert len(note["note"]) <= 120
    lots = [x for x in CO.models(ctx, CUT)[0].state.lots if (x.account, x.asset) == ("Börse", "BTC")]
    assert any(x.cost == 0 and x.acq_date == date(2024, 12, 31) for x in lots)  # Zugang am Vortag zu 0 €


def test_remove_is_capped_at_the_opening_balance_and_the_rest_is_reported(cfg):
    rows = [*_rows(), tx("D1", "2025-02-01T10:00:00Z", "deposit", to=("Wallet", "ETH", "5"), value="5")]
    c = client_with_import(cfg, rows, ASSETS, valuation="2025-03-31")
    try:
        ctx = c.app.state.ctx
        _ref(ctx, "Wallet", "ETH", "0", "2025-03-15")  # berechnet 7, Eröffnungsbestand 2
        (r,) = [x for x in _plan(ctx)["rows"] if x["asset"] == "ETH"]
        assert (r["kind"], r["qty"], r["rest"]) == ("remove", D("2"), D("5"))
        assert _plan(ctx)["n_rest"] == 1
        res = ST.apply(ctx, CUT, {r["id"]})
        assert len(res["done"]) == 1
        assert _holdings(ctx, "Wallet", "ETH") == D("5")  # Rest bleibt sichtbar (Ursache im aktiven Zeitraum)
        (r2,) = [x for x in _plan(ctx)["rows"] if x["asset"] == "ETH"]
        assert r2["qty"] == 0 and r2["rest"] == D("5")
    finally:
        c.__exit__(None, None, None)


def test_after_the_fix_the_affected_accounts_are_reapproved(cx):
    ctx = cx.app.state.ctx
    _fix_all(ctx)
    _ref(ctx, "Wallet", "ETH", "1.5", "2025-03-15")
    (r,) = _plan(ctx)["rows"]
    res = ST.apply(ctx, CUT, {r["id"]})
    assert res["reapproved"] and not FX.state(ctx)["changed"]


def test_nothing_selected_is_an_error_and_unknown_ids_are_ignored(cx):
    ctx = cx.app.state.ctx
    _ref(ctx, "Wallet", "ETH", "1.5", "2025-03-15")
    assert ST.apply(ctx, CUT, set())["errors"]
    assert ST.apply(ctx, CUT, {"nope"})["errors"]
    assert ctx.db.scalar("SELECT COUNT(*) FROM cutover_correction") == 0


def test_page_and_post(cx):
    ctx = cx.app.state.ctx
    _ref(ctx, "Wallet", "ETH", "1.5", "2025-03-15")
    plain = cx.get("/settings/stichtag?cutoff=2025-01-01")
    assert "Ausgleich vorbereiten" in plain.text and 'name="sel"' not in plain.text
    page = cx.get("/settings/stichtag?cutoff=2025-01-01&settle=1")
    assert page.status_code == 200 and 'name="sel"' in page.text and "Ausgewählte ausgleichen" in page.text
    (r,) = _plan(ctx)["rows"]
    empty = cx.post("/settings/stichtag/ausgleich", data={"csrf_token": cx.token, "cutoff": "2025-01-01"})
    assert empty.status_code == 422
    ok = cx.post("/settings/stichtag/ausgleich", data={"csrf_token": cx.token, "cutoff": "2025-01-01",
                                                          "sel": r["id"]})
    assert ok.status_code == 200 and "1 Ausgleichsbuchung(en) gebucht" in ok.text
    assert _holdings(ctx, "Wallet", "ETH") == D("1.5")
    assert CS.preview(ctx, CUT)["settle"] is None


def test_new_journal_ids_never_collide_with_import_ids(cfg):
    """Nach einer Wiederherstellung tragen Import-Buchungen Kennungen wie ``PF-M-000001``; eine gleiche Kennung im
    Journal würde beim Einblenden stillschweigend übersprungen (unsichtbare Buchung)."""
    from app.journal.service import journal_service

    rows = [*_rows(), tx("PF-M-000001", "2024-06-01T10:00:00Z", "deposit", to=("Wallet", "ETH", "1"), value="1")]
    c = client_with_import(cfg, rows, ASSETS, valuation="2025-03-31")
    try:
        ctx = c.app.state.ctx
        res = journal_service(ctx).save({"kind": "expert", "date": "2025-02-01", "time": "10:00", "type": "deposit",
                                         "to_account": "Wallet", "to_asset": "ETH", "to_qty": "3", "value_eur": "0"})
        assert not res.errors and res.tx_ids != ["PF-M-000001"]
        assert res.tx_ids[0] in {t.tx_id for t in ctx.portfolio().txs}  # sichtbar
        assert _holdings(ctx, "Wallet", "ETH") == D("6")  # 2 + 1 (Import-Buchung) + 3
    finally:
        c.__exit__(None, None, None)


def test_settle_may_create_an_account_that_only_exists_as_a_data_source(cx):
    ctx = cx.app.state.ctx
    m = CO.models(ctx, CUT)[0]
    from app.cutover import corrections as CR

    v, errs = CR.validate(ctx, m, account="Neues Wallet", asset="ETH", kind="add", qty="1", mode="settle",
                          evidence="Bestand Quelle 1", acq_date=None, cost_eur=None)
    assert not errs and v is not None
    _v, errs2 = CR.validate(ctx, m, account="Neues Wallet", asset="ETH", kind="remove", qty="1", mode="settle",
                            evidence="x", acq_date=None, cost_eur=None)
    assert errs2  # Abgang von einem unbekannten Konto bleibt abgelehnt
