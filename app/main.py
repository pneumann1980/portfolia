"""Einstiegspunkt: App-Factory mit Lebenszyklus (DB-Migration, Scheduler)."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI

from app import REVISION, __version__
from app.config import Config
from app.context import AppContext
from app.logging_setup import attach_db_handler, setup_logging
from app.util.timeutil import set_local_tz
from app.web.app import create_app

log = logging.getLogger("app")


def _load_modules() -> None:
    """Optionale Module registrieren ihre Routen/Jobs beim Import."""
    for mod in ("app.news.module", "app.tax.module", "app.plans.module", "app.journal.module", "app.csvimport.module",
                "app.jobs.maintenance", "app.jobs.exports", "app.prices.sources_web", "app.datasources.web",
                "app.datasources.bitpanda", "app.datasources.chains.evm", "app.datasources.chains.bitcoin",
                "app.datasources.chains.solana", "app.datasources.chains.kaspa", "app.datasources.chains.xrpl",
                "app.datasources.chains.cardano", "app.datasources.chains.polkadot", "app.datasources.chains.peaq",
                "app.datasources.binance", "app.diagnosis.web",
                "app.diagnosis.integrity_web", "app.diagnosis.windows_web", "app.documentimport.web",
                "app.watchlist.module", "app.taxdata.module", "app.assetchange.module"):
        with contextlib.suppress(ModuleNotFoundError):
            __import__(mod)


def build_app(config: Config | None = None, start_scheduler: bool | None = None) -> FastAPI:
    config = config or Config.from_env()
    setup_logging(config.log_level, config.log_format, config.secrets.values())
    set_local_tz(config.tz)
    _load_modules()
    ctx = AppContext(config)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        ctx.startup()
        attach_db_handler(config.db_path)
        log.info("Portfolia %s (Build %s) gestartet (Port %s, Demo=%s, Auth=%s)", __version__, REVISION[:7] or "lokal",
                 config.port, config.demo_mode, config.auth_mode)
        run_sched = config.scheduler_enabled if start_scheduler is None else start_scheduler
        if run_sched:
            from app.jobs.scheduler import Scheduler

            sched = Scheduler(ctx)
            sched.setup_default_jobs()
            ctx.scheduler = sched
            sched.start(run_startup=config.startup_jobs)
        try:
            yield
        finally:
            if ctx.scheduler is not None:
                ctx.scheduler.shutdown()
            ctx.shutdown()
            log.info("Portfolia beendet")

    return create_app(ctx, lifespan=lifespan)


def app_factory() -> FastAPI:
    return build_app()
