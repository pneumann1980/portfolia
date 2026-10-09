"""Job-Funktionen (werden vom Scheduler und von UI-Aktionen aufgerufen)."""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from typing import Any

from app.context import AppContext
from app.importer.loader import ImportOutcome, check_import_dir
from app.plans.service import plan_service
from app.progress import Progress, job_progress

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
    from app import fullexport

    fresh = fullexport.is_fresh(ctx.db)
    out = check_import_dir(ctx.db, ctx.config.import_dir, ctx.engine_options("global"), trigger=trigger, force=force,
                           progress=lambda: job_progress(ctx, "import", "Import", unit="Transaktionen",
                                                         phases=["process", "reconcile", "save"]))
    if out.status == "imported" and fresh and out.import_id is not None:
        # Neue Installation + Portfolia-Export: Einstellungen, Zuordnungen und Kurshistorie gleich mit übernehmen
        try:
            if fullexport.summary(fullexport.extras_for(ctx.db, out.import_id)) is not None:
                fullexport.apply(ctx, out.import_id)
        except Exception as e:  # Übernahme darf den Import nie scheitern lassen – Rückfrage bleibt möglich
            log.warning("Zusatzdaten des Exports nicht übernommen: %s", e)
    if out.status == "imported":
        if out.filename:
            try:  # datierte Kopie der importierten ZIP-Datei (Archiv neben den ZIP-Sicherungen)
                from app.jobs.exports import archive_import

                archive_import(ctx, ctx.config.import_dir / out.filename)
            except Exception as e:  # das Archiv darf den Import nie blockieren
                log.warning("Import-Datei konnte nicht archiviert werden: %s", e)
        ctx.invalidate_data()
        # Schätzungen sofort mit dem neuen Import abgleichen (keine Doppelzählung echter Sparplan-Buchungen)
        try:
            plan_service(ctx).reconcile()
        except Exception as e:  # Abgleich darf den Import nie blockieren
            log.warning("Sparplan-Abgleich fehlgeschlagen: %s", e)
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
        sched.trigger("plans_update", 20)
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
        out["krc20"] = ctx.prices.update_krc20(pf, led, force=force).as_dict()
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
    prog = job_progress(ctx, "history_backfill", "Historische Kurse laden", unit="Kursreihen",
                        phases=["prepare", "prices", "save"])
    prog.phase("prepare", text="Zeiträume je Asset bestimmen")
    try:
        res = ctx.prices.backfill(pf, led, progress=backfill_progress(prog), force=force)
        try:  # Kurse für Sparplan-Termine sind jetzt verfügbar
            res["plans"] = plan_service(ctx).update()
        except Exception as e:
            log.warning("Sparplan-Aktualisierung fehlgeschlagen: %s", e)
        prog.phase("save", text="Historie und Kursqualität neu berechnen")
        hist = ctx.recompute_history(persist=True)
        res["days"] = hist.n if hist else 0
        prog.finish(True, f"{res.get('rows', 0)} Kurse")
        return res
    except Exception:
        prog.finish(False, "Abbruch")
        raise


def backfill_progress(prog: Progress) -> Callable[[dict[str, Any]], None]:
    """Rückruf von PriceService.backfill (done/total/current) → Phase „Kurse ergänzen“."""
    def cb(p: dict[str, Any]) -> None:
        if prog.phase_key != "prices":
            prog.phase("prices", p.get("total"))
        cur = p.get("current")
        prog.update(int(p.get("done") or 0), p.get("total"), f"{cur}" if cur else None)
    return cb


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
