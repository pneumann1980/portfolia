"""Futures, Funding, Margin (Financial Integrity, Phase 7) – nur synthetische Daten.

Verifiziert ist ausschließlich der Weg über die eigene Spaltenzuordnung (Mapping mit Vorzeichen-Menge und
Vorgangsarten); native Futures-Exporte von MEXC, Bybit und Pionex sind nicht als Profil hinterlegt (siehe Bericht).
Abgedeckt: Funding in beide Richtungen, Liquidation (Bybit-Fall nachgebildet), realisiertes Ergebnis – Bestand
mengengenau, ohne externen Kapitalfluss, ohne stille Steuerzuordnung, nie automatisch übernommen, EUR-Wert Pflicht.
"""

# ruff: noqa: F811
from __future__ import annotations

from decimal import Decimal

from app.csvimport import review as RV
from app.csvimport.service import csv_service
from app.ledger.engine import run_ledger
from tests.helpers import ASSETS, portfolio, tx
from tests.test_csvimport import client, create_unknown_assets, post, upload  # noqa: F401

D = Decimal
USDT = {"asset_id": "USDT", "name": "Tether", "asset_class": "crypto", "quote_source": "none", "quote_id": "",
        "category": "Krypto: Stablecoins", "aliases": "USDT"}
FUTURES_CSV = (b"Time;Type;Amount;Coin;ID\n"
               b"01.05.2024 08:00;FUNDING;1,25;USDT;F1\n"
               b"01.05.2024 16:00;FUNDING;-0,75;USDT;F2\n"
               b"02.05.2024 10:00;LIQUIDATION;-400;USDT;L1\n"
               b"03.05.2024 10:00;REALIZED_PNL;12,5;USDT;P1\n")


def bybit_like() -> list[dict]:
    return [tx("T0", "2024-04-30T10:00:00Z", "deposit", to=("Bybit", "USDT", "1000"), value="920"),
            tx("F1", "2024-05-01T08:00:00Z", "deposit", tag="funding", to=("Bybit", "USDT", "1.25"), value="1.15"),
            tx("F2", "2024-05-01T16:00:00Z", "withdrawal", tag="funding", frm=("Bybit", "USDT", "0.75"), value="0.69"),
            tx("L1", "2024-05-02T10:00:00Z", "withdrawal", tag="liquidation", frm=("Bybit", "USDT", "400"),
               value="368"),
            tx("P1", "2024-05-03T10:00:00Z", "deposit", tag="futures_pnl", to=("Bybit", "USDT", "12.5"), value="11.5")]


def test_ledger_quantities_are_exact_without_external_flows():
    res = run_ledger(portfolio(bybit_like(), [*ASSETS, USDT]))
    assert res.balances[("Bybit", "USDT")] == D("1000") + D("1.25") - D("0.75") - D("400") + D("12.5")
    kinds = {d.tx_id: d.kind for d in res.disposals}
    assert kinds == {"F2": "derivative", "L1": "derivative"}
    origins = sorted(lot.origin for lot in res.lots if lot.asset == "USDT")
    assert origins.count("derivative") == 2  # Funding-Eingang und realisierter Gewinn: eigene Lot-Herkunft
    derivative_flows = [f for f in res.flows if f.tx_id in ("F1", "F2", "L1", "P1")]
    assert derivative_flows == []  # kein externer Kapitalfluss (Ergebnis innerhalb des Portfolios)
    assert not [e for e in res.income if e.tx_id in ("F1", "P1")]  # nicht still als Ertrag gezählt


def test_derivatives_are_not_silently_taxed():
    from app.tax.packs.de import GermanyPack

    res = run_ledger(portfolio(bybit_like(), [*ASSETS, USDT]))
    for d in res.disposals:  # Methode nutzt keinen Zustand der Regel – ohne Parameter prüfbar
        assert GermanyPack._crypto_treatment(None, d, {}) is False  # type: ignore[arg-type]


def test_futures_export_via_mapping_needs_value_and_is_never_automatic(client):
    c = client
    bid, loc = upload(c, "futures.csv", FUTURES_CSV, account="Bybit")
    assert loc.endswith("/mapping")
    form = {"name": "Futures (eigenes Format)", "col_date": "Time", "col_type": "Type", "col_qty": "Amount",
            "col_sym": "Coin", "col_ext_id": "ID", "tz": "UTC", "decimal": ",",
            "lbl_0": "FUNDING", "map_0": "deposit:funding", "lbl_1": "LIQUIDATION", "map_1": "withdrawal:liquidation",
            "lbl_2": "REALIZED_PNL", "map_2": "deposit:futures_pnl"}
    r = post(c, f"/journal/csv/{bid}/mapping", **form)
    assert r.status_code == 303, r.text[:800]
    create_unknown_assets(c, bid)
    rows = {rc.rec.ext_id.rsplit(":", 1)[-1]: rc for rc in csv_service(c.app.state.ctx).rows(bid)}
    assert {k: (rc.row or {}).get("tag") for k, rc in rows.items()} == \
        {"F1": "funding", "F2": "funding", "L1": "liquidation", "P1": "futures_pnl"}
    assert {k: (rc.row or {}).get("type") for k, rc in rows.items()} == \
        {"F1": "deposit", "F2": "withdrawal", "L1": "withdrawal", "P1": "deposit"}
    # ohne Kurs: EUR-Wert fehlt → unvollständig (nie 0 €)
    assert all(rc.status == "invalid" and any("EUR-Wert" in e for e in rc.errors) for rc in rows.values())
    post(c, f"/journal/csv/{bid}/rows", **{f"val_{rc.idx}": "1" for rc in rows.values()})
    rows = list(csv_service(c.app.state.ctx).rows(bid))
    assert all(rc.status == "new" for rc in rows)
    assert all(RV.row_state(rc) == "offen" and rc.match["action"] == "review" for rc in rows)  # nie „sicher“
    assert all(any("Derivat-Vorgang" in h for h in rc.match.get("hints") or []) for rc in rows)
