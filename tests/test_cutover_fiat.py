"""0.97.0 Stufe 1: „Fiat überall aus“ – Engine, Vorschau, Schalter, Diagnose."""

# ruff: noqa: F811
from __future__ import annotations

import dataclasses
from decimal import Decimal

from app.cutover import fiat as FI
from app.ledger.engine import EngineOptions, run_ledger
from tests.helpers import portfolio, tx
from tests.test_bitpanda import ctx
from tests.test_review_fixes import c, cfg  # noqa: F401

D = Decimal


def _rows():
    return [
        tx("D1", "2024-01-05T10:00:00Z", "deposit", to=("Bitpanda", "EUR", "1000"), value="1000"),
        tx("B1", "2024-01-06T10:00:00Z", "buy", frm=("Bitpanda", "EUR", "600"), to=("Bitpanda", "BTC", "0.01"),
           value="600", fee=("EUR", "1", "1")),
        tx("B2", "2024-02-06T10:00:00Z", "buy", frm=("Bitpanda", "EUR", "200"), to=("Bitpanda", "ETH", "0.1"),
           value="200"),
        tx("S1", "2024-06-06T10:00:00Z", "sell", frm=("Bitpanda", "BTC", "0.004"), to=("Bitpanda", "EUR", "300"),
           value="300"),
        tx("W1", "2024-07-01T10:00:00Z", "withdrawal", frm=("Bitpanda", "EUR", "100"), value="100"),
        # Depot ohne Cash-Führung: Ertrag fließt ab
        tx("DV", "2024-08-01T10:00:00Z", "deposit", tag="dividend", to=("Depot", "EUR", "5"), value="5",
           related="WKN:A0B1C2"),
    ]


def _led(fiat_off: bool, rows=None):
    return run_ledger(portfolio(rows or _rows()), EngineOptions(fiat_off=fiat_off))


def test_fiat_off_keeps_assets_lots_and_disposals():
    a, b = _led(False), _led(True)
    keep = lambda led: {k: v for k, v in led.balances.items() if k[1] not in ("EUR", "USD")}  # noqa: E731
    assert keep(a) == keep(b)
    sig = lambda led: sorted((x.asset, x.account, x.qty, x.cost, x.acq_date, x.acq_tx) for x in led.lots)  # noqa: E731
    assert sig(a) == sig(b)
    dsig = lambda led: sorted((d.tx_id, d.kind, d.proceeds, d.cost, d.gain) for d in led.disposals)  # noqa: E731
    assert dsig(a) == dsig(b)


def test_fiat_off_removes_fiat_balances_and_changes_the_capital_definition():
    a, b = _led(False), _led(True)
    assert ("Bitpanda", "EUR") in a.balances and a.cash_tracked["Bitpanda"]
    assert not any(k[1] == "EUR" for k in b.balances) and not any(b.cash_tracked.values())
    # mit Fiat: Einzahlungen − Auszahlungen; ohne: Käufe − Verkäufe
    def total(led):
        return sum((f.amount or D(0)) for f in led.flows)
    assert total(a) == D(1000) - D(100) + D(-5)  # Einzahlung − Auszahlung − Dividende-Abfluss? (Depot ohne Cash)
    assert total(b) == D(601) + D(200) - D(300) - D(5)  # Käufe inkl. Gebühr − Verkauf − Ausschüttung


def test_fiat_off_ignores_cash_overrides_and_is_part_of_the_cache_key():
    o = EngineOptions(fiat_off=True, cash_overrides=(("Bitpanda", True),))
    assert not any(run_ledger(portfolio(_rows()), o).cash_tracked.values())
    assert EngineOptions(fiat_off=True) != EngineOptions(fiat_off=False)
    assert hash(EngineOptions(fiat_off=True)) != hash(EngineOptions(fiat_off=False))


def test_fiat_off_also_for_usd_and_fx_and_fiat_transfers():
    rows = [
        *_rows(),
        tx("X1", "2024-03-01T10:00:00Z", "trade", frm=("Bitpanda", "EUR", "50"), to=("Bitpanda", "USD", "55"),
           value="50"),
        tx("T1", "2024-03-02T10:00:00Z", "transfer", frm=("Bitpanda", "EUR", "10"), to=("N26", "EUR", "10"),
           value="10"),
    ]
    b = _led(True, rows)
    assert not any(k[1] in ("EUR", "USD") for k in b.balances)
    assert not b.issues or all(i.code != "negative_balance" for i in b.issues)


def test_settings_default_and_engine_options(c):
    cx = ctx(c)
    assert cx.engine_options().fiat_off is False
    cx.settings.set("ledger.fiat_off", True)
    assert cx.engine_options().fiat_off is True
    assert dataclasses.replace(cx.engine_options(), fiat_off=False) != cx.engine_options()


def test_preview_and_switch_roundtrip(c):
    cx = ctx(c)
    pv = FI.preview(cx)
    assert pv["has_data"] and pv["ok"] and pv["rows"]
    assert all(ok for _l, ok in pv["checks"])
    before = {k: v for k, v in cx.ledger().balances.items() if not cx.portfolio().asset(k[1]).is_fiat}
    assert FI.switch(cx, True) == {"errors": []}
    assert FI.is_off(cx) and cx.ledger().options.fiat_off
    after = {k: v for k, v in cx.ledger().balances.items() if not cx.portfolio().asset(k[1]).is_fiat}
    assert before == after
    assert not any(cx.portfolio().asset(a).is_fiat for (_acc, a) in cx.ledger().balances)
    assert FI.switch(cx, False) == {"errors": []}
    assert not FI.is_off(cx)


def test_pages_render_and_post_switches(c):
    page = c.get("/settings/fiat")
    assert page.status_code == 200 and "Fiat-Guthaben" in page.text and "Prüfsummen" in page.text
    assert "/settings/fiat" in c.get("/settings").text
    tok = c.cookies.get("portfolia_csrf")
    r = c.post("/settings/fiat", data={"csrf_token": tok, "fiat_off": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert "aktiv: nicht geführt" in c.get("/settings/fiat").text
    r = c.post("/settings/fiat", data={"csrf_token": tok, "fiat_off": "0"}, follow_redirects=False)
    assert r.status_code == 303 and "aktiv: nicht geführt" not in c.get("/settings/fiat").text


def test_diagnosis_ignores_fiat_legs_when_off(c):
    from app.diagnosis.engine import report_for

    cx = ctx(c)
    cx.settings.set("ledger.fiat_off", True)
    cx.invalidate_data()
    rep = report_for(cx)
    assert not any(a for f in rep.findings if f.kind in ("holdings", "duplicate") for a in f.assets
                   if cx.portfolio().asset(a).is_fiat)
