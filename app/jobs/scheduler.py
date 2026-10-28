"""Hintergrundjobs mit APScheduler (ein Prozess, keine externe Queue)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED, JobEvent
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app import health
from app.context import AppContext
from app.jobs import tasks

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        tz = ctx.config.tz
        # „interactive“: kurze, vom Nutzer ausgelöste Jobs (z. B. Tagesbericht) warten nicht hinter dem Import;
        # „wave“: manuelle News-Welle, lange und einzeln
        self.pool_sizes = {"default": 3, "interactive": 2, "wave": 1}
        self.missed: dict[str, int] = {}  # verpasste/übersprungene Läufe seit Start (je Job)
        self.sched = BackgroundScheduler(
            timezone=tz,
            executors={n: ThreadPoolExecutor(max_workers=w) for n, w in self.pool_sizes.items()},
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 600},
        )
        self.jobs: dict[str, Callable[..., Any]] = {}
        self._executor: dict[str, str] = {}

    def register(self, name: str, fn: Callable[..., Any], trigger: Any, executor: str = "default") -> None:
        def runner(**kwargs: Any) -> Any:
            return tasks.run_job(self.ctx, name, lambda: fn(self.ctx, **kwargs))

        self.jobs[name] = runner
        self._executor[name] = executor
        if trigger is not None:
            self.sched.add_job(runner, trigger, id=name, name=name, replace_existing=True, executor=executor)

    def trigger(self, name: str, delay_s: float = 1.0, **kwargs: Any) -> bool:
        runner = self.jobs.get(name)
        if runner is None:
            return False
        self.sched.add_job(runner, "date", run_date=datetime.now(UTC) + timedelta(seconds=delay_s),
                           id=f"{name}-once-{datetime.now(UTC).timestamp()}", kwargs=kwargs,
                           misfire_grace_time=3600, executor=self._executor.get(name, "default"))
        return True

    def debounce(self, name: str, delay_s: float, **kwargs: Any) -> bool:
        """Einmaliger Lauf nach ``delay_s`` – ein erneuter Aufruf davor verschiebt den Lauf (Änderungen bündeln)."""
        runner = self.jobs.get(name)
        if runner is None:
            return False
        self.sched.add_job(runner, "date", run_date=datetime.now(UTC) + timedelta(seconds=delay_s),
                           id=f"{name}-debounce", kwargs=kwargs, replace_existing=True, misfire_grace_time=3600,
                           executor=self._executor.get(name, "default"))
        return True

    def setup_default_jobs(self) -> None:
        s = self.ctx.settings
        self.register("import_poll", lambda ctx: _outcome(tasks.import_check(ctx, "poll")),
                      IntervalTrigger(minutes=5))
        self.register("prices_crypto", lambda ctx, force=False: tasks.refresh_prices(ctx, force, "crypto"),
                      price_trigger(s, "crypto"))
        self.register("prices_securities", lambda ctx, force=False: tasks.refresh_prices(ctx, force, "securities"),
                      price_trigger(s, "securities"))
        self.register("fx_ecb", tasks.fx_ecb, CronTrigger(hour=16, minute=35))
        self.register("history_backfill", lambda ctx, force=False: tasks.backfill(ctx, force),
                      CronTrigger(hour=6, minute=10))
        self.register("eod_snapshot", tasks.eod_snapshot, CronTrigger(hour=23, minute=30))
        self.register("cleanup", tasks.cleanup, CronTrigger(hour=4, minute=20))
        for extra in _EXTRA_JOBS:
            extra(self)

    def reschedule_prices(self) -> None:
        """Neue Kurs-Intervalle sofort übernehmen (ohne Neustart); der nächste Lauf folgt nach einem Intervall."""
        for name, kind in (("prices_crypto", "crypto"), ("prices_securities", "securities")):
            if name in self.jobs and self.sched.get_job(name) is not None:
                self.sched.reschedule_job(name, trigger=price_trigger(self.ctx.settings, kind))

    def _on_missed(self, event: JobEvent) -> None:
        """Verpasste Läufe (zu spät gestartet → verworfen) und übersprungene (Vorlauf läuft noch) sichtbar machen."""
        job = str(event.job_id).split("-once-")[0].removesuffix("-debounce")
        self.missed[job] = self.missed.get(job, 0) + 1
        if event.code == EVENT_JOB_MISSED:
            planned = getattr(event, "scheduled_run_time", None)
            late = f", {round((datetime.now(UTC) - planned).total_seconds())} s zu spät" if planned else ""
            log.warning("Lauf von %s verpasst (Pool ausgelastet%s) – der nächste planmäßige Lauf folgt", job, late,
                        extra={"job": job})
        else:
            log.warning("Lauf von %s übersprungen: der vorherige Lauf ist noch nicht beendet", job, extra={"job": job})

    def start(self, run_startup: bool = True) -> None:
        self.sched.add_listener(self._on_missed, EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES)
        self.sched.add_job(lambda: health.log_state(self.ctx), IntervalTrigger(hours=1), id="health_log",
                           name="health_log", replace_existing=True, executor="interactive",
                           next_run_time=datetime.now(UTC) + timedelta(seconds=90))
        self.sched.start()
        health.log_scheduler(self.ctx)
        if run_startup:
            self.trigger("import_poll", 2)
            self.trigger("prices_crypto", 8, force=True)
            self.trigger("prices_securities", 12, force=True)
            self.trigger("history_backfill", 30)
            for name, delay in _STARTUP_EXTRA:
                self.trigger(name, delay)

    def shutdown(self) -> None:
        try:
            self.sched.shutdown(wait=False)
        except Exception as e:  # pragma: no cover
            log.warning("Scheduler-Stopp: %s", e)

    def next_runs(self) -> dict[str, str | None]:
        out = {}
        for j in self.sched.get_jobs():
            if "-once-" in j.id or j.id.endswith("-debounce"):
                continue
            out[j.id] = j.next_run_time.isoformat() if j.next_run_time else None
        return out


def price_trigger(settings: Any, kind: str) -> IntervalTrigger:
    """Takt der Kursabrufe. Krypto: der Job läuft im normalen Intervall, ``PriceService.crypto_due`` überspringt
    Läufe im Drosselbetrieb bzw. bei erschöpftem Kontingent."""
    from app.prices.budget import CRYPTO_PRESETS, SECURITY_PRESETS, snap

    if kind == "crypto":
        return IntervalTrigger(minutes=snap(settings.get("prices.crypto_interval_min", 10), CRYPTO_PRESETS, 10))
    return IntervalTrigger(minutes=snap(settings.get("prices.stock_interval_min", 15), SECURITY_PRESETS, 15))


def _outcome(o: Any) -> dict[str, Any]:
    return {"status": o.status, "message": o.message, "import_id": o.import_id}


# Erweiterungspunkte für spätere Module (News, Backups, …)
_EXTRA_JOBS: list[Callable[[Scheduler], None]] = []
_STARTUP_EXTRA: list[tuple[str, float]] = []


def extra_jobs(fn: Callable[[Scheduler], None]) -> Callable[[Scheduler], None]:
    _EXTRA_JOBS.append(fn)
    return fn


def startup_job(name: str, delay: float) -> None:
    _STARTUP_EXTRA.append((name, delay))
