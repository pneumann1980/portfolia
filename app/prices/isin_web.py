"""ISIN-Ermittlung: täglicher Hintergrundlauf und Knopf auf der Steuerseite (registriert sich beim Import in main)."""

from __future__ import annotations

import logging
from typing import Any

from apscheduler.triggers.interval import IntervalTrigger
from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from app.jobs.scheduler import Scheduler, extra_jobs, startup_job
from app.prices.isin import isin_service
from app.web.app import register_router
from app.web.deps import get_ctx

log = logging.getLogger(__name__)


def isin_job(ctx: Any, manual: bool = False) -> dict[str, int]:
    if not manual and not ctx.settings.get("isin.auto", True):
        return {"skipped": 0}  # tägliche Suche abgeschaltet – nur auf Knopfdruck
    return isin_service(ctx).resolve()


@extra_jobs
def _jobs(s: Scheduler) -> None:
    s.register("isin_lookup", lambda ctx, manual=False: isin_job(ctx, manual), IntervalTrigger(hours=24))


startup_job("isin_lookup", 90)


def make_router() -> APIRouter:
    router = APIRouter()

    @router.post("/tax/isin/run")
    async def run(request: Request) -> Response:
        ctx = get_ctx(request)
        await run_in_threadpool(isin_job, ctx, True)
        year = (await request.form()).get("year") or ""
        target = f"/tax?year={year}&saved=isin#mapping"
        if request.headers.get("hx-request") == "true":
            return Response(status_code=204, headers={"HX-Redirect": target})
        return Response(status_code=303, headers={"Location": target})

    @router.post("/tax/isin/settings")
    async def settings(request: Request) -> Response:
        f = await request.form()
        get_ctx(request).settings.set("isin.auto", f.get("auto") == "1")
        target = f"/tax?year={f.get('year') or ''}#mapping"
        if request.headers.get("hx-request") == "true":
            return Response(status_code=204, headers={"HX-Redirect": target})
        return Response(status_code=303, headers={"Location": target})

    return router


register_router(make_router)
