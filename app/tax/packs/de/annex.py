"""Nachweise für Prüfung und Rückfragen (BMF-Schreiben vom 06.03.2025, Rz. 90–104): Anpassungen am Datenbestand,
Bestand zum 31.12., Umschichtungen zwischen Wallets und der Fingerabdruck des Buchungsstands."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from app.tax.base import ZERO, Column, Section, Table, TaxInput, money

# Markierungen, die auf eine Anpassung, Schätzung oder Einzelfallwertung hinweisen (Schlüssel → Erklärung)
FLAG_LABEL = {
    "KOINLY_MANUAL": "manuell im Quellsystem (Koinly) angelegt",
    "KOINLY_MISSING_RATE": "Kurs fehlte im Quellsystem",
    "KOINLY_NEG_BALANCE": "Negativbestand im Quellsystem",
    "PRESALE": "Presale-Tranche (Einzelfallwertung)",
    "AVG_PRICE": "Durchschnittskurs",
    "RECONSTRUCTED_PLAN": "aus Sparplan rekonstruiert",
    "RECONSTRUCTED_GAP": "zur Schließung einer Lücke rekonstruiert",
    "PROPOSED_B": "Korrekturvorschlag übernommen",
    "CORRECTED_A": "automatisch korrigiert",
    "USER_INSTRUCTION": "auf Anweisung des Nutzers gebucht",
    "THEFT": "Diebstahl",
    "THEFT_VERIFIED_ONCHAIN": "Diebstahl (on-chain belegt)",
    "WRITE_OFF": "Ausbuchung (Totalverlust)",
    "REVIEW_INCOME": "Einstufung des Ertrags zur Prüfung markiert",
    "ESTIMATE": "geschätzt",
    "TOKEN_MIGRATION": "Token-Migration",
    "BRIDGE_HOP": "Bridge-Zwischenschritt",
    "CORPORATE_ACTION": "Kapitalmaßnahme",
    "SOURCE_ACCOUNT_WITHOUT_DATA": "Herkunftskonto ohne Daten",
    "BISON_BONUS": "Bonus (Bison)",
    "TRANSFER_IN_FROM_FLATEX": "Übertrag von flatex",
}
SKIP_FLAGS = frozenset({"BROKER_UNASSIGNED", "SAVINGS_PLAN"})  # Verwaltungsmarken ohne Wertung
ORIGIN_LABEL = {"import": "Import", "journal": "in Portfolia erfasst/synchronisiert", "plan": "Sparplan (geschätzt)"}
TYPE_LABEL = {
    "buy": "Kauf",
    "sell": "Verkauf",
    "trade": "Tausch",
    "deposit": "Zugang",
    "withdrawal": "Abgang",
    "transfer": "Transfer",
    "fee": "Gebühr",
    "income": "Ertrag",
}
MIN_QTY = Decimal("1e-9")


def _flags(raw: str | None) -> list[str]:
    return [f for f in (raw or "").split(";") if f and f not in SKIP_FLAGS]


def data_fingerprint(inp: TaxInput) -> dict[str, Any]:
    """SHA-256 über alle Buchungen des Standes (Import, in Portfolia erfasste/synchronisierte Buchungen, Sparplan):
    ändert sich eine Buchung, ändert sich der Wert – Nachweis, auf welchem Stand ein Bericht beruht."""
    h = hashlib.sha256()
    for t in sorted(inp.pf.txs, key=lambda t: (t.ts, t.tx_id)):
        h.update(
            "|".join(
                str(x)
                for x in (
                    t.tx_id,
                    t.ts.isoformat(),
                    t.type,
                    t.tag,
                    t.from_account,
                    t.from_asset,
                    t.from_qty,
                    t.to_account,
                    t.to_asset,
                    t.to_qty,
                    t.fee_asset,
                    t.fee_qty,
                    t.fee_eur,
                    t.value_eur,
                    t.related_asset,
                    t.flag,
                    t.origin,
                )
            ).encode()
        )
        h.update(b"\n")
    manual = sum(1 for t in inp.pf.txs if t.origin != "import")
    return {"sha256": h.hexdigest(), "txs": len(inp.pf.txs), "manual": manual}


def _symbol(inp: TaxInput, asset_id: str | None) -> str:
    return inp.asset(asset_id).symbol if asset_id else ""


def _adjust_table(inp: TaxInput, year: int) -> Table:
    wanted = {t.tx_id for t in inp.pf.txs if t.date.year == year}
    for d in inp.ledger.disposals:
        if d.date.year == year:
            wanted.update(p.acq_tx for p in d.parts if p.acq_tx)
    rows: list[dict[str, Any]] = []
    for t in sorted(inp.pf.txs, key=lambda t: (t.ts, t.tx_id)):
        if t.tx_id not in wanted:
            continue
        flags = _flags(t.flag)
        if not flags and t.origin == "import":
            continue
        assets = " → ".join(x for x in (_symbol(inp, t.from_asset), _symbol(inp, t.to_asset)) if x)
        rows.append(
            {
                "date": t.date,
                "type": TYPE_LABEL.get(t.type, t.type) + (f" ({t.tag})" if t.tag else ""),
                "asset": assets,
                "account": t.to_account or t.from_account or "",
                "value": money(t.value_eur) if t.value_eur is not None else None,
                "flags": "; ".join(FLAG_LABEL.get(f, f) for f in flags) or "–",
                "origin": ORIGIN_LABEL.get(t.origin, t.origin),
                "note": (t.note or "")[:110],
                "ref": f"{t.source or '–'} · {t.tx_id[:8]}",
            }
        )
    return Table(
        "x_adjust",
        f"Anhang: Anpassungen, Schätzungen und manuelle Buchungen {year}",
        [
            Column("date", "Datum", "date", 0.8),
            Column("type", "Vorgang", "text", 1.0),
            Column("asset", "Wert", "text", 0.9),
            Column("account", "Wallet/Konto", "text", 1.2),
            Column("value", "Wert (EUR)", "eur", 0.9),
            Column("flags", "Markierung", "text", 1.8),
            Column("origin", "Herkunft", "text", 1.2),
            Column("note", "Hinweis", "text", 2.2),
            Column("ref", "Quelle · Tx", "text", 1.0),
        ],
        rows,
        landscape=True,
        note="Buchungen des Jahres und die Anschaffungsbuchungen der veräußerten Posten, die angepasst, geschätzt oder "
        "in Portfolia erfasst wurden (BMF Rz. 90: Anpassungen kenntlich machen). Belege bereithalten.",
    )


def _stock_table(inp: TaxInput, year: int) -> Table:
    snap = date(year, 12, 31)
    final = snap in inp.ledger.lot_snapshots
    lots = inp.snapshot(snap) if final else inp.ledger.lots
    agg: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"qty": ZERO, "cost": ZERO, "lots": 0, "first": None}
    )
    for lot in lots:
        a = inp.asset(lot.asset)
        if a.is_fiat or lot.qty < MIN_QTY:
            continue
        g = agg[(lot.account, lot.asset)]
        g["qty"] += lot.qty
        g["cost"] += lot.cost
        g["lots"] += 1
        g["first"] = lot.acq_date if g["first"] is None else min(g["first"], lot.acq_date)
    rows = [
        {
            "account": acc,
            "asset": inp.asset(aid).symbol,
            "kind": "Krypto" if inp.asset(aid).is_crypto else "Wertpapier",
            "qty": g["qty"],
            "cost": money(g["cost"]),
            "lots": g["lots"],
            "first": g["first"],
        }
        for (acc, aid), g in agg.items()
    ]
    rows.sort(key=lambda r: (r["account"], r["asset"]))
    title = (
        f"Anhang: Bestand zum 31.12.{year}" if final else f"Anhang: Bestand (Stand {inp.today.strftime('%d.%m.%Y')})"
    )
    return Table(
        "x_stock",
        title,
        [
            Column("account", "Wallet/Konto", "text", 1.5),
            Column("asset", "Wert", "text", 0.9),
            Column("kind", "Art", "text", 0.8),
            Column("qty", "Menge", "qty", 1.3),
            Column("cost", "Anschaffungskosten", "eur", 1.1, True),
            Column("lots", "Posten", "int", 0.6),
            Column("first", "ältester Erwerb", "date", 0.9),
        ],
        rows,
        landscape=True,
        note="Bestand je Wallet/Konto aus der Verbrauchsfolge (FIFO) mit den Anschaffungskosten der noch vorhandenen "
        "Posten (BMF Rz. 104: Bestände zum 31.12.).",
    ).with_totals("account")


def _transfer_table(inp: TaxInput, year: int) -> Table:
    rows = []
    for t in sorted(inp.pf.txs, key=lambda t: (t.ts, t.tx_id)):
        if t.date.year != year or t.type != "transfer" or not t.from_account or t.from_account == t.to_account:
            continue
        rows.append(
            {
                "date": t.date,
                "asset": _symbol(inp, t.from_asset),
                "qty": t.from_qty,
                "src": t.from_account,
                "dst": t.to_account or "",
                "ref": f"{t.source or '–'} · {t.tx_id[:8]}",
            }
        )
    return Table(
        "x_transfers",
        f"Anhang: Umschichtungen zwischen eigenen Wallets/Konten {year}",
        [
            Column("date", "Datum", "date", 0.8),
            Column("asset", "Wert", "text", 0.8),
            Column("qty", "Menge", "qty", 1.2),
            Column("src", "von", "text", 1.5),
            Column("dst", "nach", "text", 1.5),
            Column("ref", "Quelle · Tx", "text", 1.0),
        ],
        rows,
        landscape=True,
        note="Übertragungen zwischen eigenen Wallets sind keine Veräußerung; die Posten behalten Anschaffungsdatum und "
        "-kosten (BMF Rz. 104). Als Transfer gebuchte Vorgänge; zusammengeführte Ein-/Auszahlungen siehe Methodik.",
    )


def annex_section(inp: TaxInput, year: int) -> Section:
    sec = Section("annex", "Nachweise für Prüfung und Rückfragen")
    sec.tables.extend([_adjust_table(inp, year), _stock_table(inp, year), _transfer_table(inp, year)])
    return sec
