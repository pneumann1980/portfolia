"""Selbstaktualisierung der Datenqualität: nach Datenänderungen läuft der Prüflauf von selbst nach.

Importe, Journalbuchungen und Korrekturen ändern die Grundlage der Befunde. Statt „Alles prüfen“ von Hand zu
verlangen, stößt ein Änderungsrückruf einen verzögerten Lauf an (Änderungen werden gebündelt, :data:`DEBOUNCE_S`).
Der Lauf ist rein lesend gegenüber den Buchungen: ohne Klärung (``clarify=False``), also ohne Autopilot-Übernahmen
und ohne Abrufe. Er unterbleibt, wenn die Einstellung ``dq.auto_check`` aus ist, ein Import oder Prüflauf läuft oder
der letzte Lauf jünger als :data:`MIN_GAP_S` ist (verhindert Schleifen, falls der Lauf selbst Daten berührt).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.diagnosis import checkrun as CR
from app.progress import job_progress
from app.util.timeutil import parse_iso

log = logging.getLogger(__name__)
JOB = "data_autocheck"
SETTING = "dq.auto_check"
DEBOUNCE_S = 45
MIN_GAP_S = 120
_KINDS = {"data", "overlay"}


def _skip_reason(ctx: Any) -> str:
    if not ctx.settings.get(SETTING, True):
        return "ausgeschaltet"
    if (ctx.job_status().get("import") or {}).get("running"):
        return "Import läuft"
    if CR.running(ctx) is not None:
        return "Prüflauf läuft"
    last = parse_iso(str(CR.previous(ctx.db).get("finished_at") or ""))
    if last is not None and (datetime.now(UTC) - last).total_seconds() < MIN_GAP_S:
        return "letzter Lauf zu jung"
    return ""


def job_run(ctx: Any) -> dict[str, Any]:
    why = _skip_reason(ctx)
    if why:
        log.debug("Auto-Prüfung übersprungen: %s", why)
        return {"skipped": why}
    prog = job_progress(ctx, CR.JOB, CR.LABEL, unit="Schritte", phases=CR.PHASES)
    try:
        out = CR.run(ctx, prog, clarify=False)
    except RuntimeError as e:  # ein manueller Lauf kam dazwischen
        return {"skipped": str(e)}
    log.info("Auto-Prüfung: A %s · B %s", (out.get("counts") or {}).get("A"), (out.get("counts") or {}).get("B"))
    return out


def install(ctx: Any) -> None:
    """Hängt den verzögerten Lauf an die Datenänderungen (einmalig)."""
    if any(getattr(fn, "_portfolia_autocheck", False) for fn in ctx.change_listeners):
        return

    def on_change(kind: str) -> None:
        if kind in _KINDS and ctx.scheduler is not None and ctx.settings.get(SETTING, True):
            ctx.scheduler.debounce(JOB, DEBOUNCE_S)

    on_change._portfolia_autocheck = True  # type: ignore[attr-defined]
    ctx.change_listeners.append(on_change)
