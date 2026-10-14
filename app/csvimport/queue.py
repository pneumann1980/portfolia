"""Zentrale Prüfwarteschlange (``/journal/pruefung``): alle offenen Vorgänge aller Prüf-Stapel an einer Stelle.

Einheit ist der wirtschaftliche Vorgang (Ereignis-ID der Quelle; eine Bitpanda-Order mit Gebührenzeile ist ein
Vorgang). Zustände und Zähler stammen ausschließlich aus :mod:`app.csvimport.review` – Warteschlange, Stapelseite und
Datenquellenübersicht zeigen deshalb dieselben Zahlen.

Reiter:

* ``offen`` – Prüfung erforderlich (ohne sicheren Vorschlag), zurückgestellt bzw. vorgemerkt;
* ``sicher`` – sicherer Vorschlag bereit (Verknüpfen bzw. Übernehmen mit Sicherheit „sicher“/„hoch“);
* ``abgeschlossen`` – übernommen, verknüpft, bereits vorhanden, nicht übernommen, ignoriert;
* ``historie`` – vor dem Stichtag (kuratierter Import maßgeblich; nicht gefundene Vorgänge als Hinweis).

Nur lesend: die Warteschlange ändert nichts; Entscheidungen laufen über die Stapelaktionen mit Vorschau, Protokoll
und Rückgängig.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.csvimport import review as RV

TABS: dict[str, tuple[str, tuple[str, ...]]] = {
    "offen": ("Zu prüfen", ("offen", "zurueckgestellt", "vorgemerkt")),
    "sicher": ("Sicher verarbeitbar", ("sicher",)),
    "abgeschlossen": ("Abgeschlossen", ("uebernommen", "verknuepft", "beruecksichtigt", "nicht_uebernommen",
                                        "ignoriert")),
    "historie": ("Historie", ("historie",)),
}
PAGE = 100


@dataclass
class Item:
    """Ein wirtschaftlicher Vorgang in der Warteschlange."""

    batch_id: int
    event: str  # Ereignis-ID bzw. „#<Zeile>“
    state: str
    rows: list[RV.Lite] = field(default_factory=list)
    reason: str = ""
    ts: str = ""
    label: str = ""
    legs: list[str] = field(default_factory=list)
    account: str = ""
    cat: str | None = None
    conf: str | None = None
    action: str | None = None

    @property
    def idxs(self) -> list[int]:
        return [r.idx for r in self.rows]


@dataclass
class Queue:
    tab: str
    items: list[Item]
    total: int
    counts: dict[str, int]  # Reiter → Vorgänge
    reasons: Counter[str]
    sources: list[dict[str, Any]]  # Filter: Datenquellen/Dateien mit offenen Vorgängen
    batches: dict[int, Any]
    history_gaps: int
    offset: int = 0


def _events(rows: list[RV.Lite]) -> dict[tuple[int, str], list[RV.Lite]]:
    out: dict[tuple[int, str], list[RV.Lite]] = defaultdict(list)
    for r in rows:
        out[RV.event_key(r.batch_id, r.event, r.idx)].append(r)
    return out


def _state(rows: list[RV.Lite]) -> RV.Lite:
    """Dringlichste Zeile eines Vorgangs (bestimmt Zustand und Grund)."""
    order = {s: i for i, s in enumerate((*RV.OPEN, *RV.CLOSED))}
    return min(rows, key=lambda r: order[r.state])


def build(db: Any, tab: str = "offen", *, source: int | None = None, batch: int | None = None,
          reason: str = "", offset: int = 0, page: int = PAGE) -> Queue:
    tab = tab if tab in TABS else "offen"
    where = ["b.status NOT IN ('mapping', 'reverted')"]
    params: list[Any] = []
    if source is not None:
        where.append("b.datasource_id=?")
        params.append(source)
    if batch is not None:
        where.append("b.id=?")
        params.append(batch)
    # offene Vorgänge nur aus aktiven Stapeln; abgeschlossene Stapel enthalten per Definition keine (Stapelstatus wird
    # aus dem Zeilenstand abgeleitet, siehe ``CsvImportService.refresh_status``)
    active = RV.load(db, " AND ".join([*where, RV.ACTIVE]), params)
    closed_rows = RV.load(db, " AND ".join([*where, "NOT " + RV.ACTIVE]), params)
    summ = RV.summarize(active)
    every = summ.events + Counter({k: n for k, n in RV.summarize(closed_rows).events.items() if k in RV.CLOSED})
    counts = {t: sum(every[s] for s in states) for t, (_l, states) in TABS.items()}

    states = TABS[tab][1]
    items: list[Item] = []
    pool = active if tab in ("offen", "sicher") else [*active, *closed_rows]
    for (bid, ev), rows in _events(pool).items():
        top = _state(rows)
        if top.state not in states:
            continue
        it = Item(bid, ev, top.state, rows, cat=top.cat, conf=top.conf, action=top.action)
        if top.state in ("offen", "zurueckgestellt"):
            it.reason = RV._reason(top)
        if reason and it.reason != reason:
            continue
        items.append(it)
    # neueste Stapel zuerst, innerhalb des Stapels in Dateireihenfolge
    items.sort(key=lambda i: (-i.batch_id, min(i.idxs)))
    total = len(items)
    offset = max(0, min(offset, max(0, total - 1)))
    shown = items[offset:offset + page]
    _details(db, shown)
    batches = {int(b["id"]): b for b in db.q(
        "SELECT id, filename, kind, source, datasource_id, status, account, created_at FROM csv_batch WHERE id IN "
        f"({','.join('?' * len({i.batch_id for i in shown})) or 'NULL'})", tuple({i.batch_id for i in shown}))}
    return Queue(tab, shown, total, counts, summ.reasons, sources(db), batches, summ.history_gaps, offset)


def _details(db: Any, items: list[Item]) -> None:
    """Datum, Bezeichnung und Beträge nur für die angezeigte Seite laden."""
    ids = [r.rid for it in items for r in it.rows]
    if not ids:
        return
    recs: dict[int, Mapping[str, Any]] = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for r in db.q(f"SELECT id, rec_json FROM csv_row WHERE id IN ({','.join('?' * len(part))})", tuple(part)):
            try:
                recs[int(r["id"])] = json.loads(r["rec_json"] or "{}")
            except ValueError:
                recs[int(r["id"])] = {}
    for it in items:
        legs: list[str] = []
        for r in it.rows:
            rec = recs.get(r.rid) or {}
            if not it.ts:
                it.ts = "" if rec.get("ts_missing") else str(rec.get("ts") or "")[:19]
            if not it.label:
                it.label = str(rec.get("label") or rec.get("kind") or "")
            if not it.account:
                it.account = str(rec.get("account") or "")
            row = rec.get("row") or {}
            if row:
                for q, a, sign in ((row.get("from_qty"), row.get("from_asset"), "−"),
                                   (row.get("to_qty"), row.get("to_asset"), "+"),
                                   (row.get("fee_qty"), row.get("fee_asset"), "Gebühr ")):
                    if a and q:
                        legs.append(f"{sign}{q} {a}")
                continue
            for q, a, sign in ((rec.get("out_qty"), rec.get("out_sym"), "−"),
                               (rec.get("in_qty"), rec.get("in_sym"), "+"),
                               (rec.get("fee_qty"), rec.get("fee_sym"), "Gebühr ")):
                if a and q:
                    legs.append(f"{sign}{q} {a}")
        it.legs = legs[:6]


def sources(db: Any) -> list[dict[str, Any]]:
    """Datenquellen bzw. Dateien mit aktiven Prüf-Stapeln (Filterauswahl), offene Vorgänge je Quelle."""
    by: dict[Any, dict[str, Any]] = {}
    rows = RV.load(db, RV.ACTIVE)
    per: dict[Any, list[RV.Lite]] = defaultdict(list)
    for r in rows:
        per[r.datasource_id if r.datasource_id is not None else f"csv:{r.batch_id}"].append(r)
    names = {int(r["id"]): r["name"] for r in db.q("SELECT id, name FROM data_source")}
    for key, rs in per.items():
        s = RV.summarize(rs)
        if isinstance(key, int):
            label = names.get(key) or f"Datenquelle {key}"
        else:
            label = rs[0].source
        by[key] = {"key": key, "label": label, "open": s.open, "to_review": s.to_review,
                   "safe": s.events["sicher"], "datasource": isinstance(key, int),
                   "param": {"source": key} if isinstance(key, int) else {"batch": rs[0].batch_id}}
    return sorted(by.values(), key=lambda x: (-x["open"], str(x["label"]).lower()))
