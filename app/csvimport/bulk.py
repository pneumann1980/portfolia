"""Sammelverarbeitung sicherer Vorschläge über mehrere Prüf-Stapel (Prüfwarteschlange).

Ablauf: Vorgänge auswählen → Vorschau (Gesamtwirkung und je Stapel) → ausdrücklich bestätigen → Ausführen.

* **Auswahl:** je Stapel gespeichert (dieselbe Auswahl wie auf der Stapelseite). Vorausgewählt sind nur sichere
  Verknüpfungen (Dubletten/Ergänzungen, Sicherheit „sicher“/„hoch“); sichere *neue* Buchungen werden nur auf
  ausdrücklichen Wunsch gewählt. Eine Abwahl bleibt bestehen – auch nach neuen Abrufen.
* **Einheit:** der wirtschaftliche Vorgang – gewählt werden immer alle Zeilen eines Vorgangs mit sicherem Vorschlag.
* **Ausführung:** Stapel für Stapel über :func:`app.csvimport.batch.execute` – je Stapel atomar (eine Transaktion),
  genau einmal je Vorschau (Token) und nur, wenn die Vorschau noch gilt (Prüfsumme). Scheitert ein Stapel, bleiben die
  übrigen unberührt bzw. ausgeführt; das Ergebnis zeigt den Stand je Stapel. Jede Aktion ist im Verlauf des Stapels
  rückgängig zu machen.
"""

from __future__ import annotations

import contextlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.csvimport import batch as BA
from app.csvimport import queue as Q
from app.csvimport import review as RV

LINK_GROUPS = ("dublette", "ergaenzung")


def _stored(db: Any, bid: int) -> list[int] | None:
    sel = json.loads(db.scalar("SELECT summary_json FROM csv_batch WHERE id=?", (bid,)) or "{}").get("selection")
    return None if sel is None else [int(i) for i in sel]


def selected(db: Any, items: Iterable[Q.Item]) -> dict[int, set[int]]:
    """Ausgewählte Zeilen mit sicherem Vorschlag je Stapel (gespeicherte Auswahl bzw. Vorauswahl)."""
    by_batch: dict[int, list[Q.Item]] = defaultdict(list)
    for it in items:
        by_batch[it.batch_id].append(it)
    out: dict[int, set[int]] = {}
    for bid, its in by_batch.items():
        stored = _stored(db, bid)
        safe = {i for it in its for i in it.safe_idxs}
        if stored is None:  # Vorauswahl: nur sichere Verknüpfungen, keine neuen Buchungen
            cur = {r.idx for it in its for r in it.rows if r.state == "sicher" and r.cat in LINK_GROUPS}
        else:
            cur = set(stored) & safe
        if cur:
            out[bid] = cur
    return out


def is_checked(sel: Mapping[int, set[int]], it: Q.Item) -> bool:
    idxs = set(it.safe_idxs)
    return bool(idxs) and idxs <= sel.get(it.batch_id, set())


def store(db: Any, svc: Any, shown: Iterable[str], on: Iterable[str]) -> int:
    """Auswahl der angezeigten Vorgänge speichern (``"<stapel>:<zeile>,<zeile>"``). Nicht angezeigte Vorgänge behalten
    ihre Auswahl; eine Vorauswahl wird beim ersten Speichern zur eigenen Auswahl des Stapels."""
    want = {str(x) for x in on}
    changes: dict[int, tuple[set[int], set[int]]] = defaultdict(lambda: (set(), set()))
    for key in shown:
        bid, idxs = _parse(str(key))
        if bid is None:
            continue
        add, rem = changes[bid]
        (add if str(key) in want else rem).update(idxs)
    n = 0
    for bid, (add, rem) in changes.items():
        rows = svc.rows(bid)
        cur = BA.selection(db, bid, rows)
        new = (cur | add) - rem
        if new != cur:
            BA.set_selection(db, bid, new)
            n += 1
    return n


def _parse(key: str) -> tuple[int | None, set[int]]:
    b, _, rest = key.partition(":")
    if not b.isdigit():
        return None, set()
    return int(b), {int(x) for x in rest.split(",") if x.isdigit()}


def key_of(it: Q.Item) -> str:
    return f"{it.batch_id}:{','.join(str(i) for i in it.safe_idxs)}"


# ----------------------------------------------------------------------------------------------------
# Vorschau über mehrere Stapel
# ----------------------------------------------------------------------------------------------------

@dataclass
class Part:
    batch: Any
    plan: BA.Plan
    idxs: list[int]
    token: str
    fingerprint: str


@dataclass
class MultiPlan:
    parts: list[Part] = field(default_factory=list)
    impact: list[dict[str, Any]] = field(default_factory=list)  # Bestandswirkung aller Stapel zusammen
    fifo: list[dict[str, Any]] = field(default_factory=list)  # betroffene FIFO-Reihen (Konto, Asset, ab Datum)
    fees: dict[str, Decimal] = field(default_factory=dict)  # Gebühren neuer Buchungen je Asset

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for p in self.parts:
            for k, n in p.plan.counts().items():
                out[k] += n
        return dict(out)

    @property
    def excluded(self) -> int:
        return sum(len(p.plan.excluded) for p in self.parts)

    @property
    def todo(self) -> int:
        return sum(len(p.plan.todo) for p in self.parts)

    @property
    def negative(self) -> bool:
        return any(x["negative"] for x in self.impact)


def preview(svc: Any, sel: Mapping[int, set[int]]) -> MultiPlan:
    """Vorschau je Stapel (frisch bewertet, wie beim Ausführen) und Gesamtwirkung. Nur sichere Zeilen werden
    eingeplant – was seit der Auswahl unsicher geworden ist, fällt sichtbar heraus."""
    mp = MultiPlan()
    inc: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    first: dict[tuple[str, str], str] = {}
    for bid in sorted(sel):
        b = svc.batch(bid)
        if b is None or b["status"] not in ("preview", "partial"):
            continue
        svc.evaluate(bid)  # wie beim Ausführen: sonst wiche die Prüfsumme ab
        rows = svc.rows(bid)
        safe = {rc.idx for rc in rows if RV.row_state(rc) == "sicher"}
        idxs = sorted(sel[bid] & safe)
        if not idxs:
            continue
        p = BA.plan(svc, bid, "suggest", idxs, False, rows)
        mp.parts.append(Part(b, p, idxs, BA.new_token(), p.fingerprint()))
        for it in p.todo:
            if it.act != "include":
                continue
            for k, v in BA._row_delta(it.rc).items():
                inc[k] += v
                day = it.rc.ts.date().isoformat() if not it.rc.rec.ts_missing else ""
                if day and (k not in first or day < first[k]):
                    first[k] = day
            r = it.rc.row or {}
            if r.get("fee_asset") and r.get("fee_qty"):
                with contextlib.suppress(ArithmeticError):
                    mp.fees[r["fee_asset"]] = mp.fees.get(r["fee_asset"], Decimal(0)) + Decimal(str(r["fee_qty"]))
    if inc:
        try:
            led = svc.ctx.ledger()
        except Exception:  # Vorschau ohne Bestand ist besser als keine
            led = None
        for (acc, asset), d in sorted(inc.items()):
            if not d:
                continue
            now = led.balances.get((acc, asset), Decimal(0)) if led is not None else None
            mp.impact.append({"account": acc, "asset": asset, "now": now, "delta": d,
                              "after": (now + d) if now is not None else None,
                              "negative": now is not None and now + d < 0})
        mp.fifo = [{"account": a, "asset": s, "from": first.get((a, s), "")} for (a, s) in sorted(first)]
    return mp


def run(svc: Any, parts: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Stapel für Stapel ausführen; je Stapel atomar. Rückgabe je Stapel: Ergebnis bzw. Fehler."""
    out = []
    for p in parts:
        bid = int(p["batch_id"])
        res = BA.execute(svc, bid, "suggest", [int(i) for i in p["idxs"]], token=str(p["token"]),
                         fingerprint=str(p["fingerprint"]))
        out.append({"batch_id": bid, **res})
    return out


def decide(svc: Any, bid: int, idxs: Iterable[int], op: str) -> dict[str, Any]:
    """Einzelentscheidung aus der Warteschlange (nicht übernehmen, zurückstellen, wieder aufnehmen) – als
    protokollierte Stapelaktion mit Rückgängig. Ändert keinen Bestand (es wird nichts gebucht)."""
    if op not in ("skip", "defer", "reset"):
        return {"errors": ["Unbekannte Entscheidung."]}
    svc.evaluate(bid)
    rows = svc.rows(bid)
    p = BA.plan(svc, bid, op, list(idxs), False, rows)
    if not p.todo:
        return {"errors": [p.items[0].note if p.items else "Nichts zu entscheiden."]}
    return BA.execute(svc, bid, op, list(idxs), token=BA.new_token(), fingerprint=p.fingerprint())
