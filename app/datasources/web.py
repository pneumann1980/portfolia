"""Einstellungen → Datenquellen: anlegen, ansehen, bearbeiten, (de)aktivieren, entfernen, prüfen, synchronisieren.

Hintergrundjob ``datasources_sync`` (alle 5 Minuten): fällige, aktive Quellen mit Connector synchronisieren.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from app.datasources.connector import CREDENTIAL_RE
from app.datasources.providers import EXCHANGE, INTERVALS, KIND_LABEL, WALLET, looks_secret, providers
from app.datasources.service import RUN_STATUS_LABEL, STATUS_LABEL, datasource_service
from app.jobs.scheduler import Scheduler, extra_jobs
from app.web.app import register_router
from app.web.deps import get_ctx, render

log = logging.getLogger(__name__)

FORM_FIELDS = ("kind", "provider", "name", "account", "address", "credential_ref", "sync_interval_min", "auto_commit",
               "note")


@extra_jobs
def _register(s: Scheduler) -> None:
    from apscheduler.triggers.interval import IntervalTrigger

    s.register("datasources_sync", lambda ctx: datasource_service(ctx).run_due(), IntervalTrigger(minutes=5))


def _back(request: Request, target: str) -> Response:
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": target})
    return Response(status_code=303, headers={"Location": target})


def _safe_echo(data: dict[str, Any]) -> dict[str, Any]:
    """Formular nach Fehlern erneut füllen – ohne mögliche Geheimnisse (Schlüssel, Seed) zurückzuspielen."""
    out = dict(data)
    if looks_secret(out.get("address") or ""):
        out["address"] = ""
    ref = (out.get("credential_ref") or "").strip().upper()
    if ref and not CREDENTIAL_RE.match(ref):
        out["credential_ref"] = ""
    return out


def _form_page(request: Request, data: dict[str, Any], errors: list[str], sid: int | None = None,
               status_code: int = 200) -> HTMLResponse:
    ctx = get_ctx(request)
    svc = datasource_service(ctx)
    ds = svc.get(sid) if sid else None
    kind = ds.kind if ds else (data.get("kind") if data.get("kind") in (EXCHANGE, WALLET) else EXCHANGE)
    return render(request, "datasource_form.html", status_code=status_code, active="settings", ds=ds, data=data,
                  errors=errors, kind=kind, kind_label=KIND_LABEL, providers=providers(kind), intervals=INTERVALS,
                  accounts=svc.accounts(), runs=svc.runs(sid) if sid else [], run_status=RUN_STATUS_LABEL,
                  pending=svc.pending_batch(sid) if sid else None)


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/settings/datasources", response_class=HTMLResponse)
    def index(request: Request, msg: str = "", error: str = "") -> HTMLResponse:
        ctx = get_ctx(request)
        svc = datasource_service(ctx)
        items = svc.list()
        pending = {ds.id: svc.pending_batch(ds.id) for ds in items}
        return render(request, "datasources.html", active="settings", items=items, pending=pending, msg=msg,
                      error=error, status_label=STATUS_LABEL)

    @router.get("/settings/datasources/new", response_class=HTMLResponse)
    def new_form(request: Request, kind: str = EXCHANGE) -> HTMLResponse:
        return _form_page(request, {"kind": kind, "sync_interval_min": "0"}, [])

    @router.post("/settings/datasources")
    async def create(request: Request) -> Response:
        svc = datasource_service(get_ctx(request))
        f = await request.form()
        data = {k: str(f.get(k) or "") for k in FORM_FIELDS}
        sid, errors = await run_in_threadpool(svc.create, data)
        if errors:
            return _form_page(request, _safe_echo(data), errors, status_code=400)
        return _back(request, "/settings/datasources?" + urlencode({"msg": f"„{data['name'].strip()}“ angelegt."})
                     + f"#ds-{sid}")

    @router.get("/settings/datasources/{sid}", response_class=HTMLResponse)
    def edit_form(request: Request, sid: int) -> HTMLResponse:
        ds = datasource_service(get_ctx(request)).get(sid)
        if ds is None:
            raise HTTPException(404)
        data = {k: ("" if ds.row[k] is None else str(ds.row[k])) for k in FORM_FIELDS}
        return _form_page(request, data, [], sid)

    @router.post("/settings/datasources/{sid}")
    async def update(request: Request, sid: int) -> Response:
        svc = datasource_service(get_ctx(request))
        if svc.get(sid) is None:
            raise HTTPException(404)
        f = await request.form()
        data = {k: str(f.get(k) or "") for k in FORM_FIELDS if k != "kind"}
        errors = await run_in_threadpool(svc.update, sid, data)
        if errors:
            return _form_page(request, _safe_echo(data), errors, sid, status_code=400)
        return _back(request, "/settings/datasources?" + urlencode({"msg": "Änderungen gespeichert."}) + f"#ds-{sid}")

    @router.post("/settings/datasources/{sid}/toggle")
    async def toggle(request: Request, sid: int) -> Response:
        svc = datasource_service(get_ctx(request))
        ds = svc.get(sid)
        if ds is None:
            raise HTTPException(404)
        await run_in_threadpool(svc.set_enabled, sid, not ds.enabled)
        text = f"„{ds.name}“ " + ("deaktiviert." if ds.enabled else "aktiviert.")
        return _back(request, "/settings/datasources?" + urlencode({"msg": text}) + f"#ds-{sid}")

    @router.post("/settings/datasources/{sid}/delete")
    async def delete(request: Request, sid: int) -> Response:
        svc = datasource_service(get_ctx(request))
        ds = svc.get(sid)
        if ds is None:
            raise HTTPException(404)
        await run_in_threadpool(svc.delete, sid)
        return _back(request, "/settings/datasources?" + urlencode({"msg": f"„{ds.name}“ entfernt – übernommene "
                                                                          "Buchungen bleiben erhalten."}))

    @router.post("/settings/datasources/{sid}/reset")
    async def reset(request: Request, sid: int) -> Response:
        svc = datasource_service(get_ctx(request))
        if not await run_in_threadpool(svc.reset_cursor, sid):
            raise HTTPException(404)
        return _back(request, "/settings/datasources?" + urlencode(
            {"msg": "Abrufstand zurückgesetzt – der nächste Lauf holt alle Vorgänge erneut; bereits übernommene "
                    "werden erkannt."}) + f"#ds-{sid}")

    @router.post("/settings/datasources/{sid}/check")
    async def check(request: Request, sid: int) -> Response:
        svc = datasource_service(get_ctx(request))
        if svc.get(sid) is None:
            raise HTTPException(404)
        ok, text = await run_in_threadpool(svc.check, sid)
        return _back(request, "/settings/datasources?" + urlencode({"msg" if ok else "error": text}) + f"#ds-{sid}")

    @router.post("/settings/datasources/{sid}/sync")
    async def sync(request: Request, sid: int) -> Response:
        svc = datasource_service(get_ctx(request))
        if svc.get(sid) is None:
            raise HTTPException(404)
        res = await run_in_threadpool(svc.sync, sid, "manual")
        if res.get("batch_id") and not res.get("committed"):
            return _back(request, f"/journal/csv/{res['batch_id']}")  # zur Prüfung
        key = "error" if (res.get("error") or res.get("unsupported")) else "msg"
        text = res.get("error") or res.get("unsupported") or res.get("message") or "Keine neuen Vorgänge."
        return _back(request, "/settings/datasources?" + urlencode({key: text}) + f"#ds-{sid}")

    return router


register_router(make_router)
