"""Oberfläche „Datenqualität → Finanzielle Integritätsprüfung“ und Hintergrundjob.

Der Prüflauf liest nur (:mod:`app.diagnosis.integrity`); gespeichert wird allein sein Ergebnis. Korrekturen führen
über die vorhandene Diagnose (Vorschau → Bestätigung → Rückgängig) bzw. die Sammelbearbeitung des Abgleichs.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from app.diagnosis import integrity as I
from app.jobs.scheduler import Scheduler, extra_jobs
from app.progress import job_progress, view
from app.web.app import register_router
from app.web.deps import get_ctx, render

log = logging.getLogger(__name__)
PHASES = ["prepare", "process", "reconcile", "prices", "save"]


def job_run(ctx: Any) -> dict[str, Any]:
    prog = job_progress(ctx, I.JOB, "Integritätsprüfung", unit="Prüfungen", phases=PHASES)
    r = I.run(ctx, prog)
    return {"ok": r.ok, "open": r.counts()["open"], "ms": r.duration_ms}


@extra_jobs
def _register_jobs(s: Scheduler) -> None:
    s.register(I.JOB, job_run, None)  # nur auf Anforderung (kein Zeitplan)


def _running(ctx: Any) -> dict[str, Any] | None:
    st = ctx.job_status().get(I.JOB) or {}
    if not st.get("running"):
        return None
    p = view({**(st.get("progress") or {}), "running": True, "label": "Integritätsprüfung"})
    return None if p.get("stale") else p


def _to(**q: str) -> Response:
    return Response(status_code=303, headers={"Location": "/quality/integrity?" + urlencode(q)})


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/quality/integrity", response_class=HTMLResponse)
    def page(request: Request, category: str = "", severity: str = "", asset: str = "", account: str = "",
             status: str = "", sort: str = "", msg: str = "", err: str = "") -> HTMLResponse:
        ctx = get_ctx(request)
        r = I.load(ctx.db)
        items: list[I.Item] = []
        if r is not None:
            r = I.refresh_status(ctx, r)
            items = I.filtered(r, category=category if category in I.CATEGORIES else "",
                               severity=severity if severity in I.SEVERITIES else "", asset=asset, account=account,
                               status=status if status in I.STATUSES else "", sort=sort)
        accounts = sorted({a for it in (r.items if r else []) for a in it.accounts})
        assets = sorted({a for it in (r.items if r else []) for a in it.assets})
        return render(request, "integrity.html", active="quality", run=r, items=items, running=_running(ctx),
                      categories=I.CATEGORIES, severities=I.SEVERITIES, statuses=I.STATUSES, causes=I.CAUSES,
                      f={"category": category, "severity": severity, "asset": asset, "account": account,
                         "status": status, "sort": sort},
                      all_accounts=accounts, all_assets=assets, msg=msg, err=err, now=datetime.now(UTC),
                      independent_note=I.INDEPENDENT_NOTE)

    @router.get("/quality/integrity/progress", response_class=HTMLResponse)
    def progress(request: Request) -> Response:
        ctx = get_ctx(request)
        p = _running(ctx)
        if p is None:  # fertig → Seite neu laden
            return Response(status_code=204, headers={"HX-Redirect": "/quality/integrity"})
        return render(request, "partials/integrity_progress.html", p=p)

    @router.post("/quality/integrity/run")
    async def start(request: Request) -> Response:
        ctx = get_ctx(request)
        if _running(ctx) is not None:
            return _to(err="Eine Prüfung läuft bereits.")
        if ctx.scheduler is not None and ctx.scheduler.trigger(I.JOB, 0.2):
            return _to(msg="Prüfung gestartet.")
        try:  # ohne Scheduler (Tests, CLI): direkt ausführen
            await run_in_threadpool(job_run, ctx)
        except RuntimeError as e:
            return _to(err=str(e))
        return _to(msg="Prüfung abgeschlossen.")

    @router.get("/quality/integrity/export.{fmt}")
    def export(request: Request, fmt: str, category: str = "", severity: str = "", asset: str = "",
               account: str = "", status: str = "") -> Response:
        ctx = get_ctx(request)
        r = I.load(ctx.db)
        if r is None or fmt not in ("csv", "json"):
            raise HTTPException(404)
        r = I.refresh_status(ctx, r)
        items = I.filtered(r, category=category, severity=severity, asset=asset, account=account, status=status)
        stamp = (r.finished_at or "")[:19].replace(":", "").replace("-", "")
        if fmt == "csv":
            body, media = I.export_csv(r, items), "text/csv; charset=utf-8"
        else:
            body, media = I.export_json(r, items), "application/json"
        return Response(body, media_type=media, headers={
            "Content-Disposition": f'attachment; filename="integritaetspruefung-{stamp}.{fmt}"',
            "Cache-Control": "private, no-store"})

    return router


register_router(make_router)
