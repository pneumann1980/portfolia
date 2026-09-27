"""Hintergrundjobs mit APScheduler (ein Prozess, keine externe Queue)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.context import AppContext
from app.jobs import tasks

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        tz = ctx.config.tz
        self.sched = BackgroundScheduler(
            timezone=tz,
            executors={"default": ThreadPoolExecutor(max_workers=3)},
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 600},
        )
        self.jobs: dict[str, Callable[..., Any]] = {}

    def register(self, name: str, fn: Callable[..., Any], trigger: Any) -> None:
        def runner(**kwargs: Any) -> Any:
            return tasks.run_job(self.ctx, name, lambda: fn(self.ctx, **kwargs))

        self.jobs[name] = runner
        if trigger is not None:
            self.sched.add_job(runner, trigger, id=name, name=name, replace_existing=True)

    def trigger(self, name: str, delay_s: float = 1.0, **kwargs: Any) -> bool:
        runner = self.jobs.get(name)
        if runner is None:
            return False
        self.sched.add_job(runner, "date", run_date=datetime.now(UTC) + timedelta(seconds=delay_s),
                           id=f"{name}-once-{datetime.now(UTC).timestamp()}", kwargs=kwargs,
                           misfire_grace_time=3600)
        return True

    def debounce(self, name: str, delay_s: float, **kwargs: Any) -> bool:
        """Einmaliger Lauf nach ``delay_s`` – ein erneuter Aufruf davor verschiebt den Lauf (Änderungen bündeln)."""
        runner = self.jobs.get(name)
        if runner is None:
            return False
        self.sched.add_job(runner, "date", run_date=datetime.now(UTC) + timedelta(seconds=delay_s),
                           id=f"{name}-debounce", kwargs=kwargs, replace_existing=True, misfire_grace_time=3600)
        return True

    def setup_default_jobs(self) -> None:
        s = self.ctx.settings
        self.register("import_poll", lambda ctx: _outcome(tasks.import_check(ctx, "poll")),
                      IntervalTrigger(minutes=5))
        self.register("prices_crypto", lambda ctx, force=False: tasks.refresh_prices(ctx, force, "crypto"),
                      IntervalTrigger(minutes=int(s.get("prices.crypto_interval_min", 10))))
        self.register("prices_securities", lambda ctx, force=False: tasks.refresh_prices(ctx, force, "securities"),
                      IntervalTrigger(minutes=int(s.get("prices.stock_interval_min", 15))))
        self.register("fx_ecb", tasks.fx_ecb, CronTrigger(hour=16, minute=35))
        self.register("history_backfill", lambda ctx, force=False: tasks.backfill(ctx, force),
                      CronTrigger(hour=6, minute=10))
        self.register("eod_snapshot", tasks.eod_snapshot, CronTrigger(hour=23, minute=30))
        self.register("cleanup", tasks.cleanup, CronTrigger(hour=4, minute=20))
        for extra in _EXTRA_JOBS:
            extra(self)

    def start(self, run_startup: bool = True) -> None:
        self.sched.start()
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
