"""Einstiegspunkt: App-Factory mit Lebenszyklus (DB-Migration, Scheduler)."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI

from app import health
from app.config import Config
from app.context import AppContext
from app.logging_setup import attach_db_handler, setup_logging
from app.util.timeutil import set_local_tz
from app.web.app import create_app

log = logging.getLogger("app")


MODULES = (
    "app.news.module", "app.tax.module", "app.plans.module", "app.journal.module", "app.csvimport.module",
    "app.jobs.maintenance", "app.jobs.exports", "app.prices.sources_web", "app.datasources.web",
    "app.datasources.dkb_web", "app.datasources.archive_web",
    "app.datasources.bitpanda", "app.datasources.chains.evm", "app.datasources.chains.bitcoin",
    "app.datasources.chains.solana", "app.datasources.chains.kaspa", "app.datasources.chains.xrpl",
    "app.datasources.chains.cardano", "app.datasources.chains.polkadot", "app.datasources.chains.peaq",
    "app.datasources.binance", "app.diagnosis.web",
    "app.diagnosis.integrity_web", "app.diagnosis.windows_web", "app.documentimport.web",
    "app.watchlist.module", "app.taxdata.module", "app.taxcert.module", "app.prices.isin_web",
    "app.assetchange.module",
    "app.assetchange.merge_web", "app.diagnosis.auto_web", "app.assetchange.account_merge_web",
    "app.diagnosis.clarify_web", "app.accounts.web", "app.diagnosis.dq_web",
)


def _load_modules() -> dict[str, list[str]]:
    """Optionale Module registrieren ihre Routen/Jobs beim Import.

    Fehlt ein Modul selbst, wird es übersprungen. Fehlt eine Abhängigkeit *innerhalb* eines Moduls, bliebe dessen
    Funktion sonst still aus (Routen → 404): das wird als Fehler protokolliert und im Bericht als ``failed`` geführt.
    """
    report: dict[str, list[str]] = {"loaded": [], "failed": [], "absent": []}
    for mod in MODULES:
        try:
            __import__(mod)
        except ModuleNotFoundError as e:
            if e.name is not None and (e.name == mod or mod.startswith(e.name + ".")):
                report["absent"].append(mod)  # das Modul selbst gibt es nicht (optional)
            else:
                report["failed"].append(mod)
                log.error("Modul %s nicht geladen: Abhängigkeit %r fehlt", mod, e.name, exc_info=True)
        else:
            report["loaded"].append(mod)
    return report


def build_app(config: Config | None = None, start_scheduler: bool | None = None) -> FastAPI:
    config = config or Config.from_env()
    setup_logging(config.log_level, config.log_format, config.secrets.values(), config.log_levels)
    set_local_tz(config.tz)
    load_report = _load_modules()
    ctx = AppContext(config)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        ctx.startup()
        health.log_startup(ctx)
        attach_db_handler(config.db_path)
        health.log_modules(load_report)
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
