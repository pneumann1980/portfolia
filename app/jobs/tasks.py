"""Job-Funktionen (werden vom Scheduler und von UI-Aktionen aufgerufen)."""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from typing import Any

from app.context import AppContext
from app.importer.loader import ImportOutcome, check_import_dir

log = logging.getLogger(__name__)


def run_job(ctx: AppContext, name: str, fn: Callable[[], dict[str, Any] | None]) -> dict[str, Any] | None:
    """Einheitlicher Rahmen: Status in job_status, Fehler protokollieren, nie Exceptions nach außen."""
    ctx.job_start(name)
    try:
        result = fn()
        ctx.job_end(name, True, result=result)
        return result
    except Exception as e:
        log.error("Job %s fehlgeschlagen: %s", name, e, extra={"job": name})
        log.debug("%s", traceback.format_exc())
        ctx.job_end(name, False, error=f"{type(e).__name__}: {e}")
        return None
    finally:
        ctx.db.close_thread_conn()


def import_check(ctx: AppContext, trigger: str = "poll", force: bool = False) -> ImportOutcome:
    out = check_import_dir(ctx.db, ctx.config.import_dir, ctx.engine_options("global"), trigger=trigger, force=force)
    if out.status == "imported":
        ctx.invalidate_data()
        after_import(ctx)
    return out


def after_import(ctx: AppContext) -> None:
    """Nach einem Import: Kurse aktualisieren, Historie nachladen, Snapshots neu berechnen, News neu zuordnen."""
    sched = ctx.scheduler
    if sched is not None:
        sched.trigger("prices_crypto", force=True)
        sched.trigger("prices_securities", force=True)
        sched.trigger("history_backfill")
        sched.trigger("news_rematch")
    else:
        refresh_prices(ctx, force=True)
        backfill(ctx)


def refresh_prices(ctx: AppContext, force: bool = False, which: str = "all") -> dict[str, Any]:
    pf = ctx.portfolio()
    led = ctx.ledger()
    if pf is None or led is None:
        return {"skipped": "kein Import"}
    out: dict[str, Any] = {}
    if which in ("all", "crypto"):
        out["crypto"] = ctx.prices.update_crypto(pf, led, force=force).as_dict()
    if which in ("all", "securities"):
        out["securities"] = ctx.prices.update_securities(pf, led, force=force).as_dict()
    return out


def fx_ecb(ctx: AppContext) -> dict[str, Any]:
    pf = ctx.portfolio()
    if pf is None:
        return {"skipped": "kein Import"}
    return ctx.prices.update_fx_ecb(pf, ctx.ledger()).as_dict()


def backfill(ctx: AppContext, force: bool = False) -> dict[str, Any]:
    pf = ctx.portfolio()
    led = ctx.ledger()
    if pf is None or led is None:
        return {"skipped": "kein Import"}
    res = ctx.prices.backfill(pf, led, progress=lambda p: ctx.job_progress("history_backfill", p), force=force)
    hist = ctx.recompute_history(persist=True)
    res["days"] = hist.n if hist else 0
    return res


def eod_snapshot(ctx: AppContext) -> dict[str, Any]:
    pf = ctx.portfolio()
    led = ctx.ledger()
    if pf is None or led is None:
        return {"skipped": "kein Import"}
    n = ctx.prices.write_eod_closes(pf, led)
    ctx.invalidate_history()
    hist = ctx.history_with_live()
    if hist is not None:
        from app.analytics.history import persist_snapshots

        persist_snapshots(ctx.db, hist, ctx.active_import_id(), kind="eod", only_last=True)
    return {"eod_closes": n}


def cleanup(ctx: AppContext) -> dict[str, Any]:
    removed = ctx.store.prune_intraday(8)
    return {"intraday_removed": removed}
