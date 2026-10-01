"""Seite „Kursquellen“ (Datenqualität) und Hintergrundjob für die automatische CoinGecko-Zuordnung."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from app.jobs.scheduler import Scheduler, extra_jobs, startup_job
from app.prices.sources import AUTO_LEVELS, STATUS_LABEL, chain_hints, refresh_catalog, source_service
from app.web.app import register_router
from app.web.deps import get_ctx, render

log = logging.getLogger(__name__)

DEBOUNCE_S = 90


@extra_jobs
def _register(s: Scheduler) -> None:
    from apscheduler.triggers.cron import CronTrigger

    s.register("resolve_sources", lambda ctx, force=False: source_service(ctx).run(force),
               CronTrigger(hour=6, minute=40))
    # Coin-Katalog für Vorschläge im Prüf-Stapel (nur bei Bedarf angestoßen, siehe ``catalog_state``)
    s.register("coingecko_catalog", lambda ctx, force=False: refresh_catalog(ctx, force), None)
    ctx = s.ctx
    if not any(getattr(fn, "_portfolia_sources", False) for fn in ctx.change_listeners):
        def on_change(kind: str) -> None:
            if ctx.scheduler is not None:
                ctx.scheduler.debounce("resolve_sources", DEBOUNCE_S)

        on_change._portfolia_sources = True  # type: ignore[attr-defined]
        ctx.change_listeners.append(on_change)


startup_job("resolve_sources", 150)


def _back(request: Request, target: str) -> Response:
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": target})
    return Response(status_code=303, headers={"Location": target})


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/quality/sources", response_class=HTMLResponse)
    def page(request: Request, msg: str = "", error: str = "") -> HTMLResponse:
        ctx = get_ctx(request)
        ov = source_service(ctx).overview()
        pf = ctx.recorded_portfolio()
        accounts: dict[str, set[str]] = {}
        for t in pf.txs if pf else []:
            for acc, aid in ((t.from_account, t.from_asset), (t.to_account, t.to_asset)):
                if acc and aid:
                    accounts.setdefault(aid, set()).add(acc)
        for it in ov["items"]:
            accs = sorted(accounts.get(it["asset"].asset_id, ()), key=str.lower)
            it["accounts"] = accs
            it["hints"] = sorted(chain_hints(accs))
        job = ctx.job_status().get("resolve_sources")
        return render(request, "sources.html", active="quality", ov=ov, msg=msg, error=error, job=job,
                      status_label=STATUS_LABEL, auto_label=AUTO_LEVELS.get(
                          str(ctx.settings.get("prices.auto_map", "hoch")), ""),
                      demo=ctx.prices.cg is None)

    @router.post("/quality/sources/run")
    async def run(request: Request) -> Response:
        ctx = get_ctx(request)
        try:
            res: dict[str, Any] = await run_in_threadpool(source_service(ctx).run, True)
        except Exception as e:  # Netzwerk/Quelle: Meldung statt Fehlerseite
            log.warning("Kursquellen-Suche fehlgeschlagen: %s", e)
            return _back(request, "/quality/sources?" + urlencode({"error": f"Suche fehlgeschlagen: {e}"}))
        if res.get("skipped"):
            return _back(request, "/quality/sources?" + urlencode({"error": str(res["skipped"])}))
        text = (f"{res.get('checked', 0)} geprüft · {len(res.get('applied') or [])} automatisch zugeordnet · "
                f"{res.get('suggested', 0)} Vorschläge · {res.get('none', 0)} ohne Treffer")
        return _back(request, "/quality/sources?" + urlencode({"msg": text}))

    @router.post("/quality/sources/accept")
    async def accept(request: Request) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        asset_id = str(f.get("asset_id") or "")
        coin = str(f.get("coin_id") or f.get("manual") or "")
        err = await run_in_threadpool(source_service(ctx).accept, asset_id, coin)
        if err:
            return _back(request, "/quality/sources?" + urlencode({"error": f"{asset_id}: {err}"}))
        return _back(request, "/quality/sources?" + urlencode({"msg": f"{asset_id}: Kursquelle übernommen – "
                                                                      "Kurse werden geladen."})
                     + "#a-" + quote(asset_id, safe=""))

    @router.post("/quality/sources/reject")
    async def reject(request: Request) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        asset_id = str(f.get("asset_id") or "")
        await run_in_threadpool(source_service(ctx).reject, asset_id)
        return _back(request, "/quality/sources?" + urlencode({"msg": f"{asset_id}: Vorschlag abgelehnt."}))

    @router.post("/quality/sources/reset")
    async def reset(request: Request) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        asset_id = str(f.get("asset_id") or "")
        await run_in_threadpool(source_service(ctx).reset, asset_id)
        return _back(request, "/quality/sources?" + urlencode({"msg": f"{asset_id}: Zuordnung entfernt – "
                                                                      "wird beim nächsten Suchlauf neu geprüft."}))

    return router


register_router(make_router)
