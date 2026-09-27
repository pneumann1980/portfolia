"""Web-Oberfläche und Jobs für Sparpläne (registriert sich beim Import in main)."""

from __future__ import annotations

import logging
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from app.jobs.scheduler import Scheduler, extra_jobs, startup_job
from app.plans.service import cutoff_of, plan_service
from app.util.numbers import parse_number
from app.util.timeutil import today_local
from app.web.app import register_router
from app.web.deps import get_ctx, render

log = logging.getLogger(__name__)


def job_update(ctx: Any) -> dict[str, Any]:
    return plan_service(ctx).update()


@extra_jobs
def _register_jobs(s: Scheduler) -> None:
    # morgens (Ausführungen des Vortags/Wochenendes) und nach Börsenschluss mit Schlusskursen
    s.register("plans_update", job_update, CronTrigger(hour=6, minute=40))
    s.register("plans_update_evening", job_update, CronTrigger(hour=23, minute=45))


startup_job("plans_update", 25)


def _back(request: Request, target: str = "/plans") -> Response:
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": target})
    return Response(status_code=303, headers={"Location": target})


def _page(request: Request, status_code: int = 200, **extra: Any) -> HTMLResponse:
    ctx = get_ctx(request)
    svc = plan_service(ctx)
    base = ctx.recorded_portfolio()
    names = dict(base.assets) if base else {}
    return render(
        request, "plans.html", status_code=status_code, active="plans", plans=svc.plans(),
        pending=svc.estimates(("estimated",)), confirmed=svc.estimates(("confirmed",)),
        history=svc.estimates(("superseded", "missing", "dismissed"), limit=60), assets=names,
        cutoff=cutoff_of(base) if base else None, has_import=bool(base and base.valuation_date),
        today=today_local(), **extra,
    )


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/plans", response_class=HTMLResponse)
    def plans_page(request: Request, saved: str | None = None) -> HTMLResponse:
        ctx = get_ctx(request)
        if ctx.recorded_portfolio() is None:
            return render(request, "empty.html", active="plans")
        return _page(request, saved=saved)

    @router.post("/plans/update")
    async def update_now(request: Request) -> Response:
        ctx = get_ctx(request)
        await run_in_threadpool(plan_service(ctx).update)
        return _back(request, "/plans?saved=update")

    @router.post("/plans/tx/confirm-all")
    def confirm_all(request: Request) -> Response:
        n = plan_service(get_ctx(request)).confirm_all()
        log.info("Sparplan-Schätzungen bestätigt: %d", n)
        return _back(request, "/plans?saved=confirmed")

    @router.post("/plans/tx/{eid}/confirm")
    def confirm(request: Request, eid: int) -> Response:
        if not plan_service(get_ctx(request)).confirm(eid):
            raise HTTPException(404)
        return _back(request, "/plans?saved=confirmed")

    @router.post("/plans/tx/{eid}/dismiss")
    def dismiss(request: Request, eid: int) -> Response:
        if not plan_service(get_ctx(request)).dismiss(eid):
            raise HTTPException(404)
        return _back(request, "/plans?saved=dismissed")

    @router.get("/plans/tx/{eid}/edit", response_class=HTMLResponse)
    def edit_form(request: Request, eid: int) -> HTMLResponse:
        svc = plan_service(get_ctx(request))
        row = next((r for r in svc.estimates(("estimated", "confirmed"), limit=10000) if r["id"] == eid), None)
        if row is None:
            raise HTTPException(404)
        if request.headers.get("hx-request") == "true":
            return render(request, "partials/plan_edit.html", alerts=[], e=row, errors=[])
        return _page(request, edit_id=eid)

    @router.post("/plans/tx/{eid}/edit")
    async def edit_save(request: Request, eid: int) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        form = {k: f.get(k) for k in ("date", "time", "qty", "price", "amount", "fee")}
        errors = plan_service(ctx).edit(eid, form, confirm=f.get("confirm") == "1")
        if errors:
            return _page(request, status_code=400, edit_id=eid, errors=errors, form=form)
        return _back(request, "/plans?saved=edited")

    @router.post("/plans/plan/enabled")
    async def plan_enabled(request: Request) -> Response:
        f = await request.form()
        key, value = str(f.get("key") or ""), str(f.get("value") or "auto")
        if value not in ("auto", "on", "off"):
            raise HTTPException(400)
        await run_in_threadpool(plan_service(get_ctx(request)).set_enabled, key, value)
        return _back(request, "/plans?saved=plan#plans")

    @router.post("/plans/plan/amount")
    async def plan_amount(request: Request) -> Response:
        f = await request.form()
        key = str(f.get("key") or "")
        raw = str(f.get("amount") or "").strip()
        amount = parse_number(raw) if raw else None
        if raw and (amount is None or amount <= 0 or amount > 1_000_000):
            return _page(request, status_code=400, errors=["Sparrate muss eine positive Zahl sein."])
        await run_in_threadpool(plan_service(get_ctx(request)).set_amount, key, amount)
        return _back(request, "/plans?saved=plan#plans")

    @router.get("/plans/export.csv")
    def export(request: Request) -> Response:
        include_all = request.query_params.get("all") == "1"
        statuses = ("estimated", "confirmed") if include_all else ("confirmed",)
        body = plan_service(get_ctx(request)).export_csv(statuses)
        return Response(body, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="sparplan-buchungen.csv"',
                                 "Cache-Control": "private, no-store"})

    return router


register_router(make_router)
