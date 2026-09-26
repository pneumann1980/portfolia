"""Beispiel-Import mit anonymisierten Testdaten.

Enthält: Aktien (EUR- und USD-Titel, 10:1-Split), ETF-Sparplan, Dividenden, Depot ohne und mit
Cash-Führung, Krypto mit Transfer zur Hardware-Wallet (Gebühr), Staking-Rewards, Mining, Tausch mit
Gebühr in BNB, Verkauf, ein manuell bewertetes Token und eine absichtliche Soll-Ist-Abweichung.
Kurse sind plausibel, aber fiktiv gerundet – keine echten Kontodaten.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.importer.zipbuilder import build_zip

ASSETS: list[dict[str, Any]] = [
    {"asset_id": "EUR", "name": "Euro", "asset_class": "fiat", "quote_source": "none", "category": "Cash"},
    {"asset_id": "USD", "name": "US-Dollar", "asset_class": "fiat", "quote_source": "none", "category": "Cash"},
    {"asset_id": "WKN:918422", "name": "NVIDIA Corp.", "asset_class": "security", "wkn": "918422",
     "isin": "US67066G1040", "quote_source": "yahoo", "quote_id": "NVDA", "category": "Aktien: Einzeltitel",
     "aliases": "NVIDIA;NVDA;Nvidia"},
    {"asset_id": "WKN:865985", "name": "Apple Inc.", "asset_class": "security", "wkn": "865985",
     "isin": "US0378331005", "quote_source": "yahoo", "quote_id": "AAPL", "category": "Aktien: Einzeltitel",
     "aliases": "Apple;AAPL;iPhone"},
    {"asset_id": "WKN:716460", "name": "SAP SE", "asset_class": "security", "wkn": "716460", "isin": "DE0007164600",
     "quote_source": "yahoo", "quote_id": "SAP.DE", "category": "Aktien: Einzeltitel", "aliases": "SAP;SAP SE"},
    {"asset_id": "WKN:A2QA4J", "name": "Palantir Technologies", "asset_class": "security", "wkn": "A2QA4J",
     "isin": "US69608A1088", "quote_source": "yahoo", "quote_id": "PLTR", "category": "Aktien: Einzeltitel",
     "aliases": "Palantir;PLTR"},
    {"asset_id": "WKN:A0RPWH", "name": "iShares Core MSCI World UCITS ETF", "asset_class": "security",
     "wkn": "A0RPWH", "isin": "IE00B4L5Y983", "quote_source": "yahoo", "quote_id": "EUNL.DE",
     "category": "Aktien: ETF", "aliases": "MSCI World;iShares Core MSCI World;EUNL", "tax_type": "etf_equity"},
    {"asset_id": "BTC", "name": "Bitcoin", "asset_class": "crypto", "quote_source": "coingecko", "quote_id": "bitcoin",
     "koinly_id": "BTC", "category": "Krypto: BTC/ETH", "aliases": "Bitcoin;BTC"},
    {"asset_id": "ETH", "name": "Ethereum", "asset_class": "crypto", "quote_source": "coingecko",
     "quote_id": "ethereum", "koinly_id": "ETH", "category": "Krypto: BTC/ETH", "aliases": "Ethereum;ETH;Ether"},
    {"asset_id": "SOL", "name": "Solana", "asset_class": "crypto", "quote_source": "coingecko", "quote_id": "solana",
     "category": "Krypto: Altcoins", "aliases": "Solana;SOL"},
    {"asset_id": "BNB", "name": "BNB", "asset_class": "crypto", "quote_source": "coingecko", "quote_id": "binancecoin",
     "category": "Krypto: Altcoins", "aliases": "BNB;Binance Coin"},
    {"asset_id": "KAS", "name": "Kaspa", "asset_class": "crypto", "quote_source": "coingecko", "quote_id": "kaspa",
     "category": "Krypto: Small Caps", "aliases": "Kaspa;KAS"},
    {"asset_id": "SUI", "name": "Sui", "asset_class": "crypto", "quote_source": "coingecko", "quote_id": "sui",
     "category": "Krypto: Small Caps", "aliases": "SUI;Sui Network"},
    {"asset_id": "MUSTER#mt1", "name": "Mustertoken", "asset_class": "crypto", "quote_source": "manual",
     "category": "Krypto: Small Caps", "aliases": "Mustertoken", "note": "keine Kursquelle – manuelle Kurse"},
]

ACCOUNTS = [
    {"account": "Depot A", "broker": "Muster-Broker", "depot_group": "Depot 1"},
    {"account": "Depot B", "broker": "Beispielbank", "depot_group": "Depot 2"},
    {"account": "Börse X", "broker": "Exchange X", "depot_group": "Krypto"},
    {"account": "Hardware-Wallet", "broker": "Self-Custody", "depot_group": "Krypto"},
]

# grobe Monatskurse ETH in EUR (für Staking-Rewards)
ETH_EUR = {2023: [1450, 1600, 1700, 1650, 1700, 1750, 1650, 1550, 1500, 1600, 1850, 2000],
           2024: [2100, 2400, 3100, 2900, 2800, 3200, 3000, 2400, 2200, 2300, 2900, 3400],
           2025: [3100, 2600, 1900, 1600, 2100, 2300, 2900, 3700, 3800, 3700, 3400, 3000],
           2026: [2900, 2800, 3000, 3200, 3100, 3300, 3400, 3500, 3600, 3600, 3600, 3600]}


def _tx(n: list[int], dt: str, typ: str, *, tag: str = "", frm: tuple = ("", "", ""), to: tuple = ("", "", ""),
        fee: tuple = ("", "", ""), value: Any = "", orig: tuple = ("", ""), src: str = "demo", note: str = "",
        related: str = "") -> dict[str, Any]:
    n[0] += 1
    return {
        "tx_id": f"DEMO-{n[0]:05d}", "datetime": dt, "type": typ, "tag": tag,
        "from_account": frm[0], "from_asset": frm[1], "from_qty": str(frm[2]),
        "to_account": to[0], "to_asset": to[1], "to_qty": str(to[2]),
        "fee_asset": fee[0], "fee_qty": str(fee[1]), "fee_eur": str(fee[2]), "value_eur": str(value),
        "orig_price": str(orig[0]), "orig_ccy": orig[1], "source": src, "source_ref": "", "flag": "", "note": note,
        "related_asset": related,
    }


def sample_transactions(until: date | None = None) -> list[dict[str, Any]]:
    n = [0]
    t = []
    # Depot A – ohne Cash-Konto (Käufe = Einzahlung, Verkäufe = Auszahlung)
    t.append(_tx(n, "2021-11-02T14:31:00Z", "buy", frm=("Depot A", "EUR", "2046.36"), to=("Depot A", "WKN:865985", 15),
                 value="2046.36", fee=("EUR", 1, 1), orig=("153.20", "USD")))
    t.append(_tx(n, "2022-03-15T15:02:00Z", "buy", frm=("Depot A", "EUR", "4363.64"), to=("Depot A", "WKN:918422", 20),
                 value="4363.64", fee=("EUR", 1, 1), orig=("240.00", "USD")))
    for d, q, px in (("2022-08-01", 40, "72.10"), ("2023-01-02", 30, "68.35"), ("2024-01-02", 25, "85.40"),
                     ("2025-01-02", 20, "101.20"), ("2026-01-02", 15, "108.90")):
        v = (Decimal(q) * Decimal(px)).quantize(Decimal("0.01"))
        t.append(_tx(n, d, "buy", frm=("Depot A", "EUR", v), to=("Depot A", "WKN:A0RPWH", q), value=v,
                     orig=(px, "EUR"), note="Sparplan"))
    t.append(_tx(n, "2023-05-10T14:10:00Z", "buy", frm=("Depot A", "EUR", "2650.00"), to=("Depot A", "WKN:918422", 10),
                 value="2650.00", fee=("EUR", 1, 1), orig=("290.00", "USD")))
    t.append(_tx(n, "2023-06-15", "deposit", tag="dividend", to=("Depot A", "EUR", "3.40"), value="3.40",
                 related="WKN:865985", note="Dividende netto"))
    t.append(_tx(n, "2024-06-10", "corporate_action", tag="split", frm=("Depot A", "WKN:918422", 30),
                 to=("Depot A", "WKN:918422", 300), note="Aktiensplit 10:1"))
    t.append(_tx(n, "2025-02-14T15:45:00Z", "sell", frm=("Depot A", "WKN:918422", 100), to=("Depot A", "EUR", "13299"),
                 value="13300.00", fee=("EUR", 1, 1), orig=("138.10", "USD")))
    # Depot B – mit Cash-Konto (Ein-/Auszahlungen)
    t.append(_tx(n, "2022-01-10", "deposit", to=("Depot B", "EUR", 10000), value=10000))
    t.append(_tx(n, "2022-01-12T09:15:00Z", "buy", frm=("Depot B", "EUR", "6250.00"), to=("Depot B", "WKN:716460", 50),
                 value="6250.00", fee=("EUR", "4.90", "4.90"), orig=("125.00", "EUR")))
    t.append(_tx(n, "2023-09-20T15:40:00Z", "buy", frm=("Depot B", "EUR", "1510.00"), to=("Depot B", "WKN:A2QA4J", 100),
                 value="1510.00", fee=("EUR", "4.90", "4.90"), orig=("16.05", "USD")))
    t.append(_tx(n, "2024-05-20", "deposit", tag="dividend", to=("Depot B", "EUR", "110.00"), value="110.00",
                 related="WKN:716460", note="Dividende SAP"))
    t.append(_tx(n, "2024-11-15T16:05:00Z", "sell", frm=("Depot B", "WKN:A2QA4J", 40), to=("Depot B", "EUR", "2265.10"),
                 value="2270.00", fee=("EUR", "4.90", "4.90"), orig=("60.20", "USD")))
    t.append(_tx(n, "2025-05-19", "deposit", tag="dividend", to=("Depot B", "EUR", "117.50"), value="117.50",
                 related="WKN:716460", note="Dividende SAP"))
    t.append(_tx(n, "2025-06-01", "withdrawal", frm=("Depot B", "EUR", 1500), value=1500))
    # Börse X – Krypto mit EUR-Konto
    t.append(_tx(n, "2021-12-01", "deposit", to=("Börse X", "EUR", 5000), value=5000))
    t.append(_tx(n, "2021-12-02T10:00:00Z", "buy", frm=("Börse X", "EUR", 4000), to=("Börse X", "BTC", "0.08"),
                 value=4000, fee=("EUR", 6, 6), orig=("50000", "EUR")))
    t.append(_tx(n, "2022-06-19", "deposit", to=("Börse X", "EUR", 2000), value=2000))
    t.append(_tx(n, "2022-06-20T08:30:00Z", "buy", frm=("Börse X", "EUR", 1500), to=("Börse X", "ETH", "1.5"),
                 value=1500, fee=("EUR", "2.25", "2.25"), orig=("1000", "EUR")))
    t.append(_tx(n, "2023-01-31", "deposit", to=("Börse X", "EUR", 1000), value=1000))
    t.append(_tx(n, "2023-02-01T12:00:00Z", "buy", frm=("Börse X", "EUR", 600), to=("Börse X", "BNB", 2), value=600,
                 orig=("300", "EUR")))
    t.append(_tx(n, "2023-03-15T18:20:00Z", "trade", frm=("Börse X", "ETH", "0.5"), to=("Börse X", "SOL", 35),
                 value=780, fee=("BNB", "0.01", "3.00"), note="Tausch ETH→SOL"))
    t.append(_tx(n, "2023-10-01T09:00:00Z", "transfer", frm=("Börse X", "BTC", "0.05"),
                 to=("Hardware-Wallet", "BTC", "0.05"), fee=("BTC", "0.0001", "2.70"), note="Auszahlung auf Ledger"))
    for y in (2023, 2024, 2025, 2026):
        for m in range(1, 13):
            if (y, m) < (2023, 4):
                continue
            d = date(y, m, 1)
            if until and d > until:
                break
            q = Decimal("0.0030")
            v = (q * ETH_EUR[y][m - 1]).quantize(Decimal("0.01"))
            t.append(_tx(n, d.isoformat() + "T02:00:00Z", "deposit", tag="staking", to=("Börse X", "ETH", q), value=v,
                         note="Staking-Reward"))
    t.append(_tx(n, "2024-02-01", "deposit", tag="mining", to=("Hardware-Wallet", "KAS", 1000), value="180.00",
                 note="Mining-Auszahlung (Wert geschätzt)"))
    t.append(_tx(n, "2024-03-05T11:00:00Z", "buy", frm=("Börse X", "EUR", 650), to=("Börse X", "KAS", 5000), value=650,
                 fee=("EUR", "0.65", "0.65"), orig=("0.13", "EUR")))
    t.append(_tx(n, "2024-08-12T20:00:00Z", "buy", frm=("Börse X", "EUR", 240), to=("Börse X", "SUI", 300), value=240,
                 orig=("0.80", "EUR")))
    t.append(_tx(n, "2024-09-01", "deposit", tag="airdrop", to=("Börse X", "MUSTER#mt1", 5000), value="50.00",
                 note="Airdrop, Wert laut Kurator"))
    t.append(_tx(n, "2025-03-01T13:00:00Z", "sell", frm=("Börse X", "SOL", 10), to=("Börse X", "EUR", "1298.70"),
                 value=1300, fee=("EUR", "1.30", "1.30"), orig=("130", "EUR")))
    t.append(_tx(n, "2025-09-10T07:45:00Z", "trade", frm=("Börse X", "BTC", "0.01"), to=("Börse X", "ETH", "0.25"),
                 value=900, fee=("BNB", "0.005", "3.20")))
    t.append(_tx(n, "2026-02-03T19:00:00Z", "withdrawal", tag="cost", frm=("Börse X", "SUI", 20), value="45.00",
                 note="Bezahlung einer Dienstleistung mit SUI"))
    if until:
        t = [x for x in t if x["datetime"][:10] <= until.isoformat()]
    return t


MANUAL_PRICES = [
    {"asset_id": "MUSTER#mt1", "date": "2024-09-01", "price_eur": "0.0100", "source": "Kurator"},
    {"asset_id": "MUSTER#mt1", "date": "2025-06-30", "price_eur": "0.0140", "source": "Kurator"},
    {"asset_id": "MUSTER#mt1", "date": "2026-09-01", "price_eur": "0.0122", "source": "Kurator"},
]

ISSUES = [
    {"issue_id": "I-1", "severity": "info", "asset_id": "KAS", "tx_id": "", "description":
     "Mining-Auszahlung: Wert zum Zuflusszeitpunkt geschätzt.", "status": "accepted"},
    {"issue_id": "I-2", "severity": "warning", "asset_id": "SUI", "tx_id": "", "description":
     "Demo: Soll-Bestand laut Börse weicht um 5 SUI ab (absichtlich, zeigt den Abgleich).", "status": "open"},
]


def write_sample_zip(path: Path, until: date | None = date(2026, 9, 19)) -> Path:
    from app.importer.validate import parse_decimal
    from app.ledger.engine import run_ledger
    from app.ledger.models import AccountInfo, AssetInfo, Portfolio, Tx
    from app.util.timeutil import parse_tx_datetime, to_local_date

    rows = sample_transactions(until)
    # Soll-Bestände aus dem Ledger berechnen (so stimmt der Abgleich), plus eine absichtliche Abweichung
    txs = []
    for seq, r in enumerate(rows):
        ts, date_only = parse_tx_datetime(r["datetime"])
        txs.append(Tx(seq=seq, tx_id=r["tx_id"], ts=ts, date=to_local_date(ts), date_only=date_only, type=r["type"],
                      tag=r["tag"] or None, from_account=r["from_account"] or None,
                      from_asset=r["from_asset"] or None, from_qty=parse_decimal(r["from_qty"]),
                      to_account=r["to_account"] or None, to_asset=r["to_asset"] or None,
                      to_qty=parse_decimal(r["to_qty"]), fee_asset=r["fee_asset"] or None,
                      fee_qty=parse_decimal(r["fee_qty"]), fee_eur=parse_decimal(r["fee_eur"]),
                      value_eur=parse_decimal(r["value_eur"]), related_asset=r["related_asset"] or None))
    pf = Portfolio(import_id=None, txs=txs, assets={a["asset_id"]: AssetInfo.from_parsed(a) for a in ASSETS},
                   accounts={a["account"]: AccountInfo.from_parsed(a) for a in ACCOUNTS})
    res = run_ledger(pf)
    holdings = []
    for (acc, asset), q in sorted(res.balances.items()):
        if pf.assets[asset].is_fiat:
            continue
        if asset == "SUI":
            q = q + 5
        holdings.append({"asset_id": asset, "account": acc, "qty": format(q.normalize(), "f"),
                         "as_of": "2026-09-19", "note": ""})
    return build_zip(path, transactions=rows, assets=ASSETS, holdings_check=holdings, issues=ISSUES,
                     manual_prices=MANUAL_PRICES, accounts=ACCOUNTS, extra_tx_columns=["related_asset"],
                     generated_at="2026-09-20T08:00:00Z", valuation_date="2026-09-19",
                     notes="Beispieldaten (anonymisiert) für Portfolia")
