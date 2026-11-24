"""Zwischenspeicher nach Datenänderungen im Hintergrund vorrechnen (0.121).

Nach jedem Import, Sparplan-Abgleich, jeder Kursquellen-Zuordnung oder Buchungsänderung verwirft der Kontext Portfolio,
Ledger, Bewertung und Historie (Regel aus 0.96, N26). Die erste Seite danach rechnete alles neu – gemessen 0,6–1,5 s auf
einem Bestand mit 6000 Buchungen, auf schwächerer Hardware ein Mehrfaches. Dieser Job rechnet 3 s nach der letzten
Änderung (entprellt) einmal vor: Bewertung, Historie, Hinweiszeile, News-Positionen und die Kennzahlen der
Einstellungsseite. Die Berechnung selbst ist unverändert; es ändert sich nur, *wann* sie läuft.

Grenzen: Wer in den ersten Sekunden nach einer Änderung eine Seite öffnet, rechnet wie bisher selbst (dann doppelt mit
dem Job). Jede Änderung kostet einen Lauf, auch wenn niemand hinschaut.
"""

from __future__ import annotations

import logging
import threading
import time
import weakref
from dataclasses import dataclass, field
from typing import Any

from app.jobs.scheduler import Scheduler, extra_jobs, startup_job

log = logging.getLogger(__name__)

JOB = "warm_caches"
DEBOUNCE_S = 3.0
_KINDS = ("data", "overlay")


# ---- Kennzahlen der Einstellungsseite (B) ---------------------------------------------------------------------

@dataclass(frozen=True)
class SettingsInputs:
    """Was die Einstellungsseite aus Portfolio/Ledger braucht – mit dem Cache-Schlüssel, unter dem es galt."""
    key: tuple[Any, ...]
    accounts: list[str] = field(default_factory=list)
    detected_cash: dict[str, bool] = field(default_factory=dict)
    budget_inputs: dict[str, Any] = field(default_factory=dict)
    has_tx: bool = False


_lock = threading.Lock()
_settings: weakref.WeakKeyDictionary[Any, SettingsInputs] = weakref.WeakKeyDictionary()


def settings_key(ctx: Any) -> tuple[Any, ...]:
    """Billiger Schlüssel: aktiver Import, Überlagerung, persistente Datenrevision (inkl. ``ledger.*``/``tax.*``-
    Einstellungen) und Engine-Optionen – dieselben Bestandteile wie der Ledger-Cache, ohne etwas zu berechnen."""
    return (ctx.active_import_id(), ctx.overlay_version, ctx.cache_rev(), ctx.engine_options())


def compute_settings_inputs(ctx: Any) -> SettingsInputs:
    key = settings_key(ctx)
    pf = ctx.portfolio()
    led = ctx.ledger()
    si = SettingsInputs(key, pf.all_accounts() if pf else [], dict(led.cash_tracked) if led else {},
                        ctx.prices.budget_inputs(pf, led), ctx.recorded_portfolio() is not None)
    with _lock:
        _settings[ctx] = si
    return si


def settings_inputs(ctx: Any, *, fresh: bool = False) -> tuple[SettingsInputs, bool]:
    """(Kennzahlen, aktuell?). Ohne ``fresh`` reicht der letzte Stand, wenn er veraltet ist – dann rechnet der Job im
    Hintergrund nach. ``fresh`` (direkt nach dem Speichern der Ledger-Karte) rechnet im Aufruf."""
    with _lock:
        si = _settings.get(ctx)
    if si is not None and si.key == settings_key(ctx):
        return si, True
    if si is None or fresh or not trigger(ctx):
        return compute_settings_inputs(ctx), True
    return si, False


# ---- Job (A) --------------------------------------------------------------------------------------------------

def warm(ctx: Any) -> dict[str, Any]:
    t0 = time.perf_counter()
    val = ctx.valuation()
    pf = ctx.portfolio()
    if pf is None:
        return {"skipped": "kein Import"}
    steps: dict[str, float] = {"bewertung": round(time.perf_counter() - t0, 2)}
    t = time.perf_counter()
    ctx.history()
    steps["historie"] = round(time.perf_counter() - t, 2)
    t = time.perf_counter()
    try:
        from app.assetchange.detect import hints

        hints(ctx)
    except Exception as e:  # Hinweise dürfen den Lauf nie scheitern lassen
        log.debug("Hinweise nicht vorgerechnet: %s", e)
    steps["hinweise"] = round(time.perf_counter() - t, 2)
    from app.news import held

    held.remember(ctx, val, pf)
    compute_settings_inputs(ctx)
    return {"sekunden": round(time.perf_counter() - t0, 2), **steps}


def trigger(ctx: Any) -> bool:
    """Vorrechnen anstoßen (entprellt); False ohne laufende Hintergrund-Jobs."""
    sched = getattr(ctx, "scheduler", None)
    if sched is None or not getattr(ctx.config, "scheduler_enabled", False) or JOB not in sched.jobs:
        return False
    return bool(sched.debounce(JOB, DEBOUNCE_S))


def install(ctx: Any) -> None:
    """An die Datenänderungen hängen (einmalig)."""
    if any(getattr(fn, "_portfolia_warm", False) for fn in ctx.change_listeners):
        return

    def on_change(kind: str) -> None:
        if kind in _KINDS:
            trigger(ctx)

    on_change._portfolia_warm = True  # type: ignore[attr-defined]
    ctx.change_listeners.append(on_change)


@extra_jobs
def _register(s: Scheduler) -> None:
    s.register(JOB, warm, None)
    install(s.ctx)


startup_job(JOB, 20)  # nach dem Start einmal vorrechnen: die erste Seite des Tages wartet nicht
