"""Gesamtregression Financial Integrity (Phase 10) – synthetische Nachbildungen der gemeldeten Fälle.

Fälle, die bereits eigene Tests haben, sind dort belegt (Zuordnung im Bericht
``docs/FINANCIAL_INTEGRITY_IMPLEMENTATION_REPORT.md``): Bitpanda/Binance/BNB (``test_review_workflow``),
Bybit-Liquidation (``test_derivatives``), MEXC-Lücke (``test_coverage``), Gutschrift aus mehreren Quellen und
Brutto/Netto (``test_event_groups``), 500 TOKEN ohne Ledger und Mehrbein-Vorgang (``test_m29_hardening``),
PulseChain (``test_wallets_pulse_peaq``). Hier: Ledger-Fälle mit exakten Erwartungswerten.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from app.ledger import provenance as PV
from app.ledger.engine import run_ledger
from tests.helpers import ASSETS, portfolio, tx

D = Decimal


def asset(aid: str, name: str) -> dict:
    return {"asset_id": aid, "name": name, "asset_class": "crypto", "quote_source": "none", "quote_id": "",
            "category": "Krypto", "aliases": aid}


EXTRA = [asset("USDT", "Tether"), asset("SAND", "The Sandbox"), asset("CET", "CoinEx Token")]


def test_bitpanda_to_pionex_usdt_transfer_with_fee():
    """Bitpanda-Auszahlung (Gebühr zusätzlich) → Pionex-Eingang: Transfer, Gebühr als Abgang, kein Doppelzugang."""
    rows = [tx("D", "2024-03-01T09:00:00Z", "deposit", to=("Bitpanda", "USDT", "1000"), value="920"),
            tx("T", "2024-03-19T09:41:00Z", "transfer", frm=("Bitpanda", "USDT", "745.79751117"),
               to=("Pionex", "USDT", "734.512861"))]
    res = run_ledger(portfolio(rows, [*ASSETS, *EXTRA]))
    assert res.balances[("Bitpanda", "USDT")] == D("254.20248883")
    assert res.balances[("Pionex", "USDT")] == D("734.512861")
    moved = [lot for lot in res.lots if lot.account == "Pionex"]
    assert moved and all(lot.acq_date == date(2024, 3, 1) for lot in moved)  # Anschaffung bleibt erhalten
    assert any(i.code == "implicit_fee" for i in res.issues)


def test_pionex_usdt_remainder_is_exact():
    """Pionex-USDT-Rechnung laut Auftrag: Einzahlung + Staking + Futures-Gewinn − Gebühren − Funding − Gesamtabgang
    = 0,0000388030 USDT – exakt, ohne automatische Nullstellung; Futures/Funding als Derivat, nicht als § 23."""
    rows = [tx("D", "2024-05-01T10:00:00Z", "deposit", to=("Pionex", "USDT", "81.317079"), value="75"),
            tx("S", "2024-05-02T10:00:00Z", "deposit", tag="staking", to=("Pionex", "USDT", "0.0209841067"),
               value="0.02"),
            tx("P", "2024-05-03T10:00:00Z", "deposit", tag="futures_pnl", to=("Pionex", "USDT", "3.363790"),
               value="3.1"),
            tx("G", "2024-05-04T10:00:00Z", "withdrawal", tag="fee", frm=("Pionex", "USDT", "0.60672499"),
               value="0.56"),
            tx("F", "2024-05-05T10:00:00Z", "withdrawal", tag="funding", frm=("Pionex", "USDT", "0.2899893137"),
               value="0.27"),
            tx("W", "2024-05-06T10:00:00Z", "withdrawal", frm=("Pionex", "USDT", "83.805100"), value="77")]
    res = run_ledger(portfolio(rows, [*ASSETS, *EXTRA]))
    assert res.balances[("Pionex", "USDT")] == D("0.0000388030")
    assert not any(i.code == "negative_balance" for i in res.issues)
    assert {d.kind for d in res.disposals if d.tx_id == "F"} == {"derivative"}  # Funding: Derivat-Ergebnis


def test_krest_unknown_acquisition_cost_is_flagged_not_invented():
    """KREST: Zugang ohne Wert – Anschaffungskosten „unbekannt“ (nie belegt, nie erfunden), Veräußerung sichtbar
    in dieser Klasse; Steuerpaket warnt (``deposit_basis``)."""
    rows = [tx("K1", "2022-03-01T10:00:00Z", "deposit", to=("Wallet", "KREST", "1000")),
            tx("K2", "2024-03-01T10:00:00Z", "sell", frm=("Wallet", "KREST", "400"), to=("Wallet", "EUR", "60"),
               value="60")]
    pf = portfolio(rows, [*ASSETS, asset("KREST", "Krest")])
    led = run_ledger(pf)
    rep = PV.build(SimpleNamespace(ledger=lambda: led, portfolio=lambda: pf))
    (row,) = rep.assets
    assert row.by_class == {"unbekannt": D(600)} and row.evidenced_share == 0
    (y,) = rep.years
    assert y.qty_by_class == {"unbekannt": D(400)}
    assert [lot.acq_date for lot in led.lots if lot.qty > 0] == [date(2022, 3, 1)]  # Zeitpunkt = Zugang


def test_coinex_kango_kaspy_dust_remains_exact():
    """CoinEx: historische KANGO-/KASPY-Restbestände bleiben mengengenau stehen – keine Nullstellung, kein Ausgleich."""
    rows = [tx("B1", "2023-01-05T10:00:00Z", "trade", frm=("CoinEx", "USDT", "100"), to=("CoinEx", "KANGO", "5000"),
               value="92"),
            tx("B2", "2023-01-06T10:00:00Z", "trade", frm=("CoinEx", "USDT", "50"), to=("CoinEx", "KASPY", "2500.5"),
               value="46"),
            tx("S1", "2023-06-01T10:00:00Z", "trade", frm=("CoinEx", "KANGO", "4999.987"),
               to=("CoinEx", "USDT", "20"), value="18"),
            tx("S2", "2023-06-02T10:00:00Z", "trade", frm=("CoinEx", "KASPY", "2500.4999"),
               to=("CoinEx", "USDT", "10"), value="9")]
    pre = [tx("D", "2023-01-01T10:00:00Z", "deposit", to=("CoinEx", "USDT", "150"), value="138")]
    res = run_ledger(portfolio(pre + rows, [*ASSETS, *EXTRA, asset("KANGO", "Kango"), asset("KASPY", "Kaspy")]))
    assert res.balances[("CoinEx", "KANGO")] == D("0.013")
    assert res.balances[("CoinEx", "KASPY")] == D("0.0001")
    assert res.balances[("CoinEx", "USDT")] == D(30)


def test_gateio_to_bitpanda_sand_keeps_acquisition_and_fee():
    """SAND von Gate.io zu Bitpanda: Lots wandern mit Anschaffungsdatum; Netzwerkgebühr als Transfergebühr."""
    rows = [tx("B", "2022-01-05T10:00:00Z", "buy", frm=("Gate.io", "EUR", "500"), to=("Gate.io", "SAND", "100"),
               value="500"),
            tx("T", "2023-08-01T10:00:00Z", "transfer", frm=("Gate.io", "SAND", "100"), to=("Bitpanda", "SAND", "99"))]
    res = run_ledger(portfolio(rows, [*ASSETS, *EXTRA]))
    assert res.balances.get(("Gate.io", "SAND"), D(0)) == 0 and res.balances[("Bitpanda", "SAND")] == D(99)
    lots = [lot for lot in res.lots if lot.asset == "SAND" and lot.qty > 0]
    assert [(lot.account, lot.acq_date) for lot in lots] == [("Bitpanda", date(2022, 1, 5))]
    assert sum(lot.cost for lot in lots) == D(495)  # 99 % der Anschaffungskosten fortgeführt


def test_coinex_trade_with_fee_in_third_asset():
    """CoinEx: USDT → BTC, Gebühr in CET – drei Bestände ändern sich, keiner wird erfunden."""
    rows = [tx("D1", "2024-01-01T10:00:00Z", "deposit", to=("CoinEx", "USDT", "1000"), value="910"),
            tx("D2", "2024-01-01T10:00:00Z", "deposit", to=("CoinEx", "CET", "50"), value="3"),
            tx("T", "2024-01-02T10:00:00Z", "trade", frm=("CoinEx", "USDT", "430"), to=("CoinEx", "BTC", "0.01"),
               fee=("CET", "1.5", "0.09"), value="392")]
    res = run_ledger(portfolio(rows, [*ASSETS, *EXTRA]))
    assert res.balances[("CoinEx", "USDT")] == D(570)
    assert res.balances[("CoinEx", "BTC")] == D("0.01")
    assert res.balances[("CoinEx", "CET")] == D("48.5")
