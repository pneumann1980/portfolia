"""Prüflauf „Alles prüfen“: Diagnose, Integritätsprüfung und Klärung in einem Hintergrundlauf mit Fortschritt.

Bisher gab es drei getrennte Knöpfe (Integritätsprüfung starten, Fälle untersuchen, Diagnose neu rechnen). Der
Prüflauf führt sie nacheinander aus und endet mit einem **Bericht**: was hat sich seit dem letzten Lauf geändert
(neu / erledigt), wie viele Punkte stehen in Stufe A/B/C. Er arbeitet lokal – **keine Abrufe bei Börsen, Wallets
oder Anbietern** (dafür gibt es weiterhin ausdrücklich „Aktuelle Bestände abfragen“ bzw. die Recherche im Fall).
Der erste Schritt gleicht die offenen Stapel des Prüfstapels erneut lokal mit dem Import und den App-Buchungen ab
(Hash, Dubletten, Transfers): bereits vorhandene Vorgänge verlassen so die Prüfung. Die Klärung übernimmt nur, was
die vorhandenen Einstellungen (Autopilot/Klärung) ohnehin erlauben; der Prüflauf schaltet nichts ein.

Der Fortschritt nutzt die gemeinsame Anzeige (:mod:`app.progress`): Phasenbalken mit Prozent, auch wenn ein einzelner
Schritt keine Zählung liefert (ein Taktgeber lässt den Balken dann langsam, aber nie bis zum Ende vorrücken).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.csvimport.service import csv_service
from app.diagnosis import clarify as K
from app.diagnosis import integrity as I
from app.diagnosis import workplan as WP
from app.diagnosis.engine import report_for
from app.progress import job_progress, view
from app.util.timeutil import iso

log = logging.getLogger(__name__)
JOB = "data_check"
LABEL = "Prüflauf"
STATE_KEY = "dq_check_last"
PHASES = ["chk_stapel", "chk_diag", "chk_integrity", "chk_clarify", "chk_plan"]
_LOCK = threading.Lock()


class _Ticker:
    """Lässt den Balken während eines Schritts ohne Zählung vorrücken (nie bis zum Ende der Phase)."""

    def __init__(self, prog: Any, every: float = 1.0, step: int = 8) -> None:
        self.prog, self.every, self.step = prog, every, step
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._stop.wait(self.every):
            try:
                self.prog.advance(self.step)
            except Exception as e:  # Anzeige darf den Lauf nie stören
                log.debug("Takt: %s", e)

    def __enter__(self) -> _Ticker:
        self._t.start()
        return self

    def __exit__(self, *_a: Any) -> None:
        self._stop.set()
        self._t.join(timeout=2)


def previous(db: Any) -> dict[str, Any]:
    raw = db.get_state(STATE_KEY)
    return raw if isinstance(raw, dict) else {}


def _step(prog: Any, key: str, text: str, fn: Callable[[], Any]) -> Any:
    prog.phase(key, text=text)
    with _Ticker(prog):
        return fn()


def run(ctx: Any, prog: Any | None = None, *, clarify: bool = True) -> dict[str, Any]:
    """Prüflauf. Läuft nie doppelt; ``RuntimeError``, wenn schon einer läuft."""
    if not _LOCK.acquire(blocking=False):
        raise RuntimeError("Ein Prüflauf läuft bereits.")
    try:
        return _run(ctx, prog, clarify)
    finally:
        _LOCK.release()


def _run(ctx: Any, prog: Any | None, clarify: bool) -> dict[str, Any]:
    started = datetime.now(UTC)
    before = previous(ctx.db)
    out: dict[str, Any] = {"ok": True, "error": "", "applied": 0, "cases": 0}
    if prog is None:
        prog = job_progress(ctx, JOB, LABEL, unit="Schritte", phases=PHASES)

    def fail(message: str) -> dict[str, Any]:
        # Die letzte erfolgreiche Auswertung bleibt als Vergleichsbasis erhalten. Der Fehlversuch bekommt einen
        # eigenen Zeitstempel, damit auch ein im Hintergrund gestarteter Lauf sichtbar und abgeschlossen ist.
        last_ok_at = before.get("last_ok_at") or (before.get("finished_at") if before.get("ok", True) else "")
        out.update(ok=False, error=message[:300], finished_at=iso(datetime.now(UTC)),
                   duration_s=round((datetime.now(UTC) - started).total_seconds(), 1),
                   keys=before.get("keys") or {}, last_ok_at=last_ok_at)
        ctx.db.set_state(STATE_KEY, {**before, **out})
        prog.finish(False, result=out["error"])
        return out

    try:
        out["stapel"] = _step(prog, "chk_stapel", "Importierte Vorgänge erneut mit den Buchungen abgleichen",
                              lambda: csv_service(ctx).reevaluate_open())
        failed = int(out["stapel"].get("failed") or 0)
        if failed:
            return fail(f"{failed} von {out['stapel'].get('batches', 0)} Prüfstapeln konnten nicht neu bewertet werden.")
        report = _step(prog, "chk_diag", "Buchungen, Bestände und Befunde berechnen", lambda: report_for(ctx))
        integ = _step(prog, "chk_integrity", "Bestände, FIFO, Dubletten, Kurse, Steuerdaten", lambda: I.run(ctx, None))
        if not integ.ok:
            return fail(f"Integritätsprüfung fehlgeschlagen: {integ.error or 'unbekannter Fehler'}")
        if integ.stale:
            return fail("Integritätsprüfung veraltet: Die Daten haben sich während der Prüfung geändert.")
        out["integrity_open"] = integ.counts()["open"]
        if clarify:
            res = _step(prog, "chk_clarify", "Fälle untersuchen (lokal, ohne Abrufe)",
                        lambda: K.run(ctx, "manual", research=False))
            if res is None:
                out["clarify_skipped"] = "Die Klärung ist ausgeschaltet oder läuft bereits."
            else:
                out["cases"], out["applied"] = res.get("cases", 0), len(res.get("applied") or [])
        plan = _step(prog, "chk_plan", "Arbeitsplan erstellen", lambda: WP.build(ctx, report))
    except Exception as e:
        log.exception("Prüflauf fehlgeschlagen")
        return fail(f"{type(e).__name__}: {e}")

    keys = {i.key: i.title for i in plan.items if i.tier != "C"}
    old = before.get("keys") or {}
    had_success = bool(before.get("last_ok_at") or (before.get("finished_at") if before.get("ok", True) else ""))
    finished_at = iso(datetime.now(UTC))
    out.update(counts=plan.counts, keys=keys, finished_at=finished_at, last_ok_at=finished_at, state=plan.state,
               duration_s=round((datetime.now(UTC) - started).total_seconds(), 1),
               new=[t for k, t in keys.items() if k not in old] if had_success else [],
               gone=[t for k, t in old.items() if k not in keys] if had_success else [],
               first=not had_success)
    ctx.db.set_state(STATE_KEY, out)
    c = plan.counts
    prog.finish(True, result=f"A {c['A']} · B {c['B']} · C {c['C']}")
    return out


def job_run(ctx: Any) -> dict[str, Any]:
    prog = job_progress(ctx, JOB, LABEL, unit="Schritte", phases=PHASES)
    return run(ctx, prog)


def running(ctx: Any) -> dict[str, Any] | None:
    """Anzeige des laufenden Prüflaufs (oder None)."""
    st = ctx.job_status().get(JOB) or {}
    if not st.get("running"):
        return None
    p = view({**(st.get("progress") or {}), "running": True, "label": LABEL})
    return None if p.get("stale") else p
