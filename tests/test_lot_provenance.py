"""Anschaffungsdaten und FIFO-Herkunft (Financial Integrity, Phase 8) – nur synthetische Daten.

Abgedeckt: Herkunftsklassen je Lot (Kauf, Ertrag, Eröffnungsposition, Zugang ohne Beleg, unbekannte Kosten, Derivat,
ohne Anschaffungsdaten), Transfer zwischen eigenen Konten behält Anschaffungszeitpunkt und Herkunft, Herkunft der
verbrauchten Lots je Veräußerungsjahr, rückwirkende Wirkung einer Anschaffung auf spätere Steuerjahre, Seite nur lesend.
"""

# ruff: noqa: F811
from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from app.ledger import provenance as PV
from app.ledger.engine import run_ledger
from tests.helpers import ASSETS, portfolio, tx
from tests.test_datasources import client  # noqa: F401
from tests.test_review_workflow import snapshot

D = Decimal


def fake_ctx(rows, estimated: tuple[str, ...] = ()):
    pf = portfolio(rows, ASSETS)
    if estimated:  # wie ctx.portfolio(): Sparplan-Schätzungen tragen flag „estimated“
        pf = replace(pf, txs=[replace(t, flag="estimated") if t.tx_id in estimated else t for t in pf.txs])
    led = run_ledger(pf)
    return SimpleNamespace(ledger=lambda: led, portfolio=lambda: pf), led


ROWS = [
    tx("B1", "2023-01-10T10:00:00Z", "buy", frm=("Börse", "EUR", "1000"), to=("Börse", "BTC", "0.1"), value="1000"),
    tx("S1", "2023-02-01T10:00:00Z", "deposit", tag="staking", to=("Börse", "ETH", "0.5"), value="800"),
    tx("O1", "2023-03-01T10:00:00Z", "deposit", tag="opening_balance", to=("Wallet", "KAS", "1000"), value="50"),
    tx("D1", "2023-04-01T10:00:00Z", "deposit", to=("Wallet", "BNB", "2"), value="400"),
    tx("D2", "2023-04-02T10:00:00Z", "deposit", to=("Wallet", "BNB", "1")),
    tx("T1", "2023-05-01T10:00:00Z", "transfer", frm=("Börse", "BTC", "0.1"), to=("Wallet", "BTC", "0.1")),
    tx("X1", "2024-06-01T10:00:00Z", "sell", frm=("Wallet", "BTC", "0.05"), to=("Wallet", "EUR", "1500"),
       value="1500"),
    tx("X2", "2024-07-01T10:00:00Z", "sell", frm=("Wallet", "KAS", "2000"), to=("Wallet", "EUR", "100"), value="100"),
]


def test_lot_classes_and_transfer_keeps_acquisition():
    ctx, led = fake_ctx(ROWS)
    rep = PV.build(ctx)
    by = {r.asset: r for r in rep.assets}
    assert by["BTC"].by_class == {"kauf": D("0.05")} and by["BTC"].oldest == date(2023, 1, 10)  # Transfer: Datum bleibt
    assert by["ETH"].by_class == {"ertrag": D("0.5")}
    assert by["BNB"].by_class == {"ohne_beleg": D(2), "unbekannt": D(1)}  # ohne Wert: unbekannt, nicht 0 €
    assert by["BNB"].evidenced_share == 0 and by["BTC"].evidenced_share == 1
    btc_lots = [lot for lot in led.lots if lot.asset == "BTC" and lot.qty > 0]
    assert [(lot.account, lot.acq_date) for lot in btc_lots] == [("Wallet", date(2023, 1, 10))]


def test_estimated_savings_plan_lots_are_not_evidenced():
    rows = [*ROWS, tx("P1", "2024-08-01T10:00:00Z", "buy", frm=("Börse", "EUR", "100"), to=("Börse", "BTC", "0.002"),
                      value="100")]
    ctx, _led = fake_ctx(rows, estimated=("P1",))
    by = {r.asset: r for r in PV.build(ctx).assets}
    assert by["BTC"].by_class == {"kauf": D("0.05"), "geschaetzt": D("0.002")}
    assert by["BTC"].evidenced_share < 1  # Schätzung ist kein Anschaffungsbeleg


def test_disposals_by_provenance_and_missing_basis():
    ctx, _led = fake_ctx(ROWS)
    rep = PV.build(ctx)
    (y,) = rep.years
    assert y.year == 2024
    assert y.qty_by_class["kauf"] == D("0.05")
    assert y.qty_by_class["eroeffnung"] == D(1000)  # Eröffnungsposition mit Nachweis
    assert y.qty_by_class["ohne_basis"] == D(1000)  # Verkauf über den Bestand hinaus: ohne Anschaffungsdaten


def test_backdated_acquisition_names_affected_tax_years():
    _ctx, led = fake_ctx(ROWS)
    assert PV.retro_years(led, "BTC", date(2022, 12, 31)) == [2024]  # rückwirkend: Veräußerung 2024 neu zugeordnet
    assert PV.retro_years(led, "BTC", date(2024, 12, 31)) == []
    assert PV.retro_map(led, [("BTC", date(2023, 6, 1)), ("ETH", date(2023, 6, 1))]) == {"BTC": [2024]}


def test_provenance_page_is_read_only(client):
    c = client
    before = snapshot(c)
    page = c.get("/quality/anschaffung").text
    assert "Herkunft der Anschaffungsdaten" in page and "Zugang ohne Anschaffungsbeleg" in page
    assert snapshot(c) == before
