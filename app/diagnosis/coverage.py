"""Historische Quellenabdeckung je Konto, Quelle, Zeitraum und Produktbereich (rein lesend, berechnet).

Frage: *Für welche Monate liegen für ein Konto überhaupt Daten vor – und woher?* Eine Monatszelle ist

* **gebucht** – wirksame Buchungen dieser Quelle in diesem Monat;
* **nur Prüf-Stapel** – Vorgänge liegen eingelesen, aber (noch) nicht gebucht vor;
* **abgedeckt, ohne Vorgänge** – eine vollständige Lieferung (vollständiger API-Abruf bzw. Datei mit diesem Zeitraum)
  deckt den Monat ab, enthält aber keinen Vorgang (ein echter „ruhiger“ Monat);
* **anderweitig belegt** – diese Quelle schweigt, eine andere Quelle desselben Kontos ist belegt bzw. abgedeckt;
* **Lücke** – innerhalb des Aktivitätszeitraums weder Buchung noch Lieferung noch Abdeckung: hier fehlen am
  ehesten Daten (z. B. ein fehlender Exportzeitraum bei MEXC, wenn zwei Dateien Januar–März und Juni–August abdecken).

Abdeckung entsteht nur aus belegbaren Lieferungen: Zeitraum je eingelesener Datei (frühester bis spätester Vorgang)
und vollständige API-Abrufe (``coverage_json.history``). Nichts wird angenommen oder gebucht.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

AREAS = {"handel": "Handel", "transfer": "Ein-/Auszahlungen & Transfers", "ertrag": "Erträge (Earn)",
         "gebuehr": "Gebühren", "derivate": "Derivate (Futures, Funding, Margin)", "sonst": "Sonstiges"}
INCOME = {"staking", "reward", "interest", "lending", "airdrop", "mining", "bonus", "cashback", "other_income"}
DERIV = {"funding", "futures", "futures_pnl", "margin", "margin_interest", "liquidation", "realized_pnl",
         "realized_gain", "realized_loss", "derivative"}
STATES = {  # Zustand → (Bezeichnung, Badge)
    "gebucht": ("gebucht", "good"),
    "staged": ("nur Prüf-Stapel", "info"),
    "abgedeckt": ("abgedeckt, ohne Vorgänge", ""),
    "anderweitig": ("anderweitig belegt", ""),
    "luecke": ("Lücke", "warn"),
}


def area_of(typ: str | None, tag: str | None) -> str:
    t, g = (typ or "").lower(), (tag or "").lower()
    if g in DERIV or t in DERIV:
        return "derivate"
    if g in INCOME:
        return "ertrag"
    if g in ("fee", "cost"):
        return "gebuehr"
    if t in ("buy", "sell", "swap", "trade", "conversion", "convert"):
        return "handel"
    if t in ("deposit", "withdrawal", "transfer"):
        return "transfer"
    return "sonst"


def month(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def month_end(m: str) -> str:
    """Letzter Tag eines Monats „JJJJ-MM“ als ISO-Zeitpunkt (Ende des Tages, UTC) – Fenstergrenze der Recherche."""
    y, mo = int(m[:4]), int(m[5:7])
    nxt = date(y + (mo == 12), 1 if mo == 12 else mo + 1, 1)
    return f"{nxt.isoformat()}T00:00:00+00:00"


def months_between(a: str, b: str) -> list[str]:
    y, m = int(a[:4]), int(a[5:7])
    out = []
    while f"{y:04d}-{m:02d}" <= b:
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


@dataclass
class Cell:
    state: str
    booked: int = 0
    staged: int = 0
    areas: dict[str, int] = field(default_factory=dict)


@dataclass
class Row:
    account: str
    source: str  # Quellbezeichnung (z. B. „Datenquelle · Kraken“, „CSV · MEXC“, „Import“)
    first: str
    last: str
    cells: dict[str, Cell] = field(default_factory=dict)
    periods: list[tuple[str, str, str]] = field(default_factory=list)  # (von, bis, Art) belegter Lieferungen
    gaps: list[tuple[str, str]] = field(default_factory=list)
    datasource_id: int | None = None
    research: bool = False  # Evidenzrecherche für Lücken möglich (Datenquelle mit Abruf)

    @property
    def key(self) -> str:
        return f"{self.account}|{self.source}"


@dataclass
class Matrix:
    months: list[str]
    rows: list[Row]
    areas: dict[str, str]
    frm: str
    to: str
    area: str = ""

    @property
    def n_gaps(self) -> int:
        return sum(len(r.gaps) for r in self.rows)


def _ranges(ms: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for m in ms:
        if out and months_between(out[-1][1], m)[1:2] == [m]:
            out[-1] = (out[-1][0], m)
        else:
            out.append((m, m))
    return out


def build(ctx: Any, frm: str = "", to: str = "", area: str = "", account: str = "") -> Matrix:
    """Abdeckungsmatrix aus Buchungen, Prüf-Stapeln und Abrufständen (nur lesend)."""
    from app.csvimport.service import CsvImportService
    from app.journal.service import source_label

    db = ctx.db
    pf = ctx.recorded_portfolio()
    booked: dict[tuple[str, str], dict[str, Cell]] = defaultdict(dict)
    staged: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    periods: dict[tuple[str, str], list[tuple[str, str, str]]] = defaultdict(list)
    ds_of: dict[tuple[str, str], int] = {}

    def label(src: str | None, origin: str) -> str:
        if origin == "import" and not (src or "").startswith(("csv:", "sync:", "doc:")):
            return "Import (kuratiert)" + (f" · {src}" if src else "")
        return source_label(src) if src else "manuell erfasst"

    for t in (pf.txs if pf is not None else []):
        ar = area_of(t.type, t.tag)
        if area and ar != area:
            continue
        accs = {a for a in (t.from_account, t.to_account) if a}
        for acc in accs:
            if account and acc != account:
                continue
            key = (acc, label(t.source, t.origin))
            m = month(t.date)
            c = booked[key].setdefault(m, Cell("gebucht"))
            c.booked += 1
            c.areas[ar] = c.areas.get(ar, 0) + 1
    # eingelesene Dateien und Abrufe: Lieferzeitraum und noch nicht gebuchte Vorgänge
    for b in db.q("SELECT b.id, b.kind, b.source, b.profile, b.account, b.datasource_id, "
                  "MIN(json_extract(r.rec_json, '$.ts')) AS t0, MAX(json_extract(r.rec_json, '$.ts')) AS t1 "
                  "FROM csv_batch b JOIN csv_row r ON r.batch_id = b.id WHERE b.status NOT IN ('mapping', 'reverted') "
                  "AND json_extract(r.rec_json, '$.ts_missing') IS NULL GROUP BY b.id"):
        if account and b["account"] != account:
            continue
        src = CsvImportService.source_of(b)
        key = (b["account"], source_label(src))
        if b["datasource_id"]:
            ds_of[key] = int(b["datasource_id"])
        if b["t0"] and b["kind"] != "sync":
            periods[key].append((str(b["t0"])[:7], str(b["t1"])[:7], "Datei"))
    for r in db.q("SELECT r.status, json_extract(r.rec_json, '$.ts') AS ts, b.account, b.source, b.profile, "
                  "b.kind AS bkind FROM csv_row r JOIN csv_batch b ON "
                  "b.id = r.batch_id WHERE b.status NOT IN ('mapping', 'reverted') AND r.status NOT IN "
                  "('committed', 'merged', 'linked', 'known', 'ignored') AND json_extract(r.rec_json, '$.ts') IS NOT "
                  "NULL AND json_extract(r.rec_json, '$.ts_missing') IS NULL"):
        if account and r["account"] != account:
            continue
        key = (r["account"], source_label(CsvImportService.source_of(r)))
        staged[key][str(r["ts"])[:7]] += 1
    research_ok: set[int] = set()
    for ds in db.q("SELECT id, provider, account, coverage_json, last_success_at FROM data_source"):
        cov = json.loads(ds["coverage_json"] or "{}")
        key = (ds["account"], source_label(f"sync:{ds['provider']}"))
        ds_of.setdefault(key, int(ds["id"]))
        hist = cov.get("history") or {}
        if cov.get("complete") and hist.get("first") and ds["last_success_at"]:
            periods[key].append((str(hist["first"])[:7], str(ds["last_success_at"])[:7], "API vollständig"))
        if cov.get("mode") or hist:
            research_ok.add(int(ds["id"]))
    keys = set(booked) | {k for k, v in staged.items() if v} | set(periods)
    if not keys:
        return Matrix([], [], AREAS, frm, to, area)
    all_months = [m for k in keys for m in (*booked.get(k, {}), *staged.get(k, {}),
                                            *(p for a, b, _ in periods.get(k, []) for p in (a, b)))]
    lo, hi = min(all_months), max(all_months)
    lo, hi = max(lo, frm[:7]) if frm else lo, min(hi, to[:7]) if to else hi
    months = months_between(lo, hi) if lo <= hi else []
    # je Konto: belegt oder abgedeckt (für „anderweitig belegt“)
    acct_cover: dict[str, set[str]] = defaultdict(set)
    for k in keys:
        acct_cover[k[0]] |= set(booked.get(k, {})) | {m for m, n in staged.get(k, {}).items() if n}
        for a, b, _ in periods.get(k, []):
            acct_cover[k[0]] |= set(months_between(a, b))
    rows: list[Row] = []
    for k in sorted(keys):
        own = set(booked.get(k, {})) | {m for m, n in staged.get(k, {}).items() if n}
        covered = {m for a, b, _ in periods.get(k, []) for m in months_between(a, b)}
        span = sorted(own | covered)
        if not span:
            continue
        row = Row(k[0], k[1], span[0], span[-1], periods=sorted(periods.get(k, [])), datasource_id=ds_of.get(k))
        row.research = row.datasource_id in research_ok if row.datasource_id else False
        gap_months = []
        for m in months:
            if m < row.first or m > row.last:
                continue
            c = booked.get(k, {}).get(m)
            n_st = staged.get(k, {}).get(m, 0)
            if c is not None:
                cell = Cell("gebucht", c.booked, n_st, dict(c.areas))
            elif n_st:
                cell = Cell("staged", 0, n_st)
            elif m in covered:
                cell = Cell("abgedeckt")
            elif m in acct_cover[k[0]]:
                cell = Cell("anderweitig")
            else:
                cell = Cell("luecke")
                gap_months.append(m)
            row.cells[m] = cell
        row.gaps = _ranges(gap_months)
        rows.append(row)
    rows.sort(key=lambda r: (-len(r.gaps), r.account.lower(), r.source.lower()))
    return Matrix(months, rows, AREAS, frm, to, area)
