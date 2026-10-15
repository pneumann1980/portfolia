"""Aufgaben und Hinweise trennen; Hinweise gesammelt als geprüft markieren.

Die Diagnose liefert zwei Arten von Befunden: **Aufgaben** (Status belegt / wahrscheinlich / verdacht – hier ist etwas
zu klären) und **Hinweise** (Status „hinweis“ – zur Einordnung, kein Fehler festgestellt, z. B. inaktive Konten). Beides
in einer Liste zu führen verdeckt, wie viel tatsächlich zu tun ist. Die Übersicht zeigt deshalb standardmäßig nur
Aufgaben.

Hinweise lassen sich gesammelt als „geprüft, kein Handlungsbedarf“ markieren (:func:`dismiss_hints`). Das ist die
vorhandene Entscheidung ohne Datenänderung (:func:`app.diagnosis.actions.mark`): keine Buchung, kein Bestand, kein
FIFO/Steuer wird berührt; sie gilt nur, solange die Befunddaten (Prüfsumme) gleich bleiben – ändert sich die Evidenz,
erscheint der Hinweis wieder („überholt“). Vorher gibt es eine Vorschau mit Prüfsumme, danach ein gemeinsames
Rückgängig.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from app.diagnosis import actions as A
from app.diagnosis.engine import report_for
from app.diagnosis.model import Finding, Report

log = logging.getLogger(__name__)
BULK_PREFIX = "Sammelaktion "
MAX_ITEMS = 500


def open_findings(ctx: Any, report: Report | None = None) -> list[Finding]:
    """Offene Befunde der Übersicht (ohne Einzelvorgänge, ohne bereits als geprüft/abgelehnt markierte)."""
    report = report or report_for(ctx)
    states = A.mark_states(report.findings, A.active_marks(ctx.db))
    checked = {fid for fid, (d, ok) in states.items() if ok and d.action in ("dismiss", "reject")}
    return [f for f in report.findings if f.id not in checked and f.parent is None]


def is_hint(f: Finding) -> bool:
    return f.status == "hinweis"


def split(findings: list[Finding]) -> tuple[list[Finding], list[Finding]]:
    """(Aufgaben, Hinweise)."""
    return [f for f in findings if not is_hint(f)], [f for f in findings if is_hint(f)]


@dataclass
class HintPlan:
    items: list[Finding] = field(default_factory=list)
    by_kind: Counter[str] = field(default_factory=Counter)
    fp: str = ""
    truncated: bool = False


def plan_hints(ctx: Any, kind: str = "", report: Report | None = None) -> HintPlan:
    """Vorschau (rein lesend): welche Hinweise würden als geprüft markiert."""
    items = [f for f in open_findings(ctx, report) if is_hint(f) and (not kind or f.kind == kind)]
    p = HintPlan(truncated=len(items) > MAX_ITEMS)
    p.items = items[:MAX_ITEMS]
    p.by_kind = Counter(f.kind for f in p.items)
    p.fp = hashlib.sha256(json.dumps([(f.id, A.fingerprint(f)) for f in p.items]).encode()).hexdigest()[:20]
    return p


def dismiss_hints(ctx: Any, fp: str, kind: str = "", note: str = "") -> tuple[A.Result, str]:
    """Hinweise gemeinsam als geprüft markieren – nur für genau den Stand der Vorschau (``fp``). Rückgabe: Ergebnis und
    Sammel-Kennung (für das gemeinsame Rückgängig)."""
    with A._LOCK:
        p = plan_hints(ctx, kind)
        if not p.items:
            return A.Result(ok=True, message="Keine offenen Hinweise."), ""
        if p.fp != fp:
            return A.Result(errors=["Die Hinweise haben sich seit der Vorschau geändert – bitte erneut prüfen."]), ""
        token = uuid.uuid4().hex[:10]
        stamp = A._now()
        label = f"{BULK_PREFIX}{token}" + (f": {note.strip()[:200]}" if note.strip() else "")
        version = A.data_version(ctx.db)
        with ctx.db.transaction() as c:
            for f in p.items:
                c.execute("UPDATE diag_decision SET status='undone', undone_at=? WHERE finding_id=? AND action IN "
                          "('dismiss', 'reject', 'defer') AND status='active'", (stamp, f.id))
                tx_ids = list(dict.fromkeys([r.tx_id for r in f.txs] + [x.tx_id for a, b, _w in f.pairs
                                                                         for x in (a, b)]))
                c.execute("INSERT INTO diag_decision(finding_id, kind, title, action, fingerprint, note, status, "
                          "created_at, tx_ids_json, data_version) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (f.id, f.kind, f.title[:300], "dismiss", A.fingerprint(f), label, "active", stamp,
                           json.dumps(tx_ids), version))
    log.info("Hinweise gesammelt als geprüft markiert: %s (%s)", len(p.items), token)
    return A.Result(ok=True, message=f"{len(p.items)} Hinweise als geprüft markiert – keine Daten geändert."), token


def undo_bulk(ctx: Any, token: str) -> A.Result:
    """Gemeinsames Rückgängig einer Sammelaktion (alle Hinweise erscheinen wieder)."""
    if not token or not token.isalnum():
        return A.Result(errors=["Unbekannte Sammelaktion."])
    with A._LOCK:
        cur = ctx.db.x("UPDATE diag_decision SET status='undone', undone_at=? WHERE status='active' AND action="
                       "'dismiss' AND note LIKE ?", (A._now(), f"{BULK_PREFIX}{token}%"))
    if not cur.rowcount:
        return A.Result(errors=["Diese Sammelaktion ist nicht (mehr) aktiv."])
    return A.Result(ok=True, message=f"{cur.rowcount} Hinweise wieder geöffnet.")


def recent_bulks(db: Any, limit: int = 8) -> list[dict[str, Any]]:
    """Letzte Sammelaktionen (für das Rückgängig)."""
    out: dict[str, dict[str, Any]] = {}
    for r in db.q("SELECT note, status, created_at FROM diag_decision WHERE action='dismiss' AND note LIKE ? "
                  "ORDER BY id DESC LIMIT 2000", (f"{BULK_PREFIX}%",)):
        token = str(r["note"])[len(BULK_PREFIX):].split(":", 1)[0].strip()
        e = out.setdefault(token, {"token": token, "at": r["created_at"], "active": 0, "total": 0})
        e["total"] += 1
        e["active"] += 1 if r["status"] == "active" else 0
    return [e for e in out.values() if e["active"]][:limit]
