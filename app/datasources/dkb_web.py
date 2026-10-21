"""Einstellungen → Datenquellen → DKB: Abruf einrichten, Zugang, Abruf per Knopfdruck, Fortschritt, Ergebnis.

Das Passwort steht nur im Formular des jeweiligen Abrufs; es wird nie gespeichert, nie zurück an den Browser gegeben und
nicht protokolliert.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from app.datasources import dkb as D
from app.datasources import plugins as P
from app.datasources.service import datasource_service
from app.web.app import register_router
from app.web.deps import get_ctx, render


def _url(sid: int, **params: str) -> str:
    return f"/settings/datasources/{sid}/dkb" + ("?" + urlencode(params) if params else "")


def _back(request: Request, target: str) -> Response:
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": target})
    return Response(status_code=303, headers={"Location": target})


def make_router() -> APIRouter:
    router = APIRouter()

    def ds_of(request: Request, sid: int) -> Any:
        ds = datasource_service(get_ctx(request)).get(sid)
        if ds is None or not D.is_dkb(ds):
            raise HTTPException(404)
        return ds

    @router.get("/settings/datasources/{sid}/dkb", response_class=HTMLResponse)
    async def page(request: Request, sid: int, msg: str = "", error: str = "") -> Response:
        ds = ds_of(request, sid)
        ctx = get_ctx(request)

        def collect() -> dict[str, Any]:
            plug = P.status(ctx.config, D.PLUGIN)
            run = D.view_run(ctx.db, sid)
            return {"plug": plug, "inst": P.last(D.PLUGIN), "set": D.settings(ctx.db, sid),
                    "folder": D.folder_state(ctx, sid, ds.name), "run": run, "runs": D.runs(ctx.db, sid),
                    "docs": D.documents(ctx.db, sid)}

        data = await run_in_threadpool(collect)
        busy = bool(data["inst"].get("running")) or bool(data["run"] and data["run"]["active"]) or data["plug"]["busy"]
        return render(request, "datasource_dkb.html", active="settings", ds=ds, msg=msg, error=error, busy=busy,
                      status_label=D.STATUS_LABEL, **data)

    @router.post("/settings/datasources/{sid}/dkb/setup")
    async def setup(request: Request, sid: int) -> Response:
        ds_of(request, sid)
        ok = await run_in_threadpool(P.start_install, get_ctx(request).config, D.PLUGIN)
        return _back(request, _url(sid, **({"msg": "Einrichtung gestartet – Fortschritt unten."} if ok else
                                           {"error": "Es läuft bereits eine Einrichtung."})))

    @router.post("/settings/datasources/{sid}/dkb/remove")
    async def remove(request: Request, sid: int) -> Response:
        ds_of(request, sid)
        if D.active(sid):
            return _back(request, _url(sid, error="Während eines Abrufs nicht möglich."))
        ok = await run_in_threadpool(P.remove, get_ctx(request).config, D.PLUGIN)
        return _back(request, _url(sid, **({"msg": "DKB-Abruf entfernt (abgelegte Dokumente bleiben)."} if ok else
                                           {"error": "Es läuft gerade eine Einrichtung."})))

    @router.post("/settings/datasources/{sid}/dkb/settings")
    async def save(request: Request, sid: int) -> Response:
        ds_of(request, sid)
        f = await request.form()
        try:
            mfa = int(str(f.get("mfa_device") or 0))
        except ValueError:
            mfa = -1
        errors = await run_in_threadpool(
            D.save_settings, get_ctx(request).db, sid, username=str(f.get("username") or ""),
            folder=str(f.get("folder") or ""), mfa_device=mfa, evaluate=str(f.get("evaluate") or "") == "1")
        if errors:
            return _back(request, _url(sid, error=" ".join(errors)))
        return _back(request, _url(sid, msg="Einstellungen gespeichert."))

    @router.post("/settings/datasources/{sid}/dkb/run")
    async def run(request: Request, sid: int) -> Response:
        ds_of(request, sid)
        f = await request.form()
        try:
            mfa = int(str(f.get("mfa_device") or 0))
        except ValueError:
            mfa = 0
        res = await run_in_threadpool(D.start, get_ctx(request), sid, str(f.get("username") or ""),
                                      str(f.get("password") or ""), mfa)
        if res.get("errors"):
            return _back(request, _url(sid, error=" ".join(res["errors"])))
        return _back(request, _url(sid, msg="Abruf gestartet – bitte die Anmeldung in der DKB-App freigeben."))

    @router.post("/settings/datasources/{sid}/dkb/evaluate")
    async def evaluate(request: Request, sid: int) -> Response:
        ds_of(request, sid)
        res = await run_in_threadpool(D.start_evaluate, get_ctx(request), sid)
        if res.get("errors"):
            return _back(request, _url(sid, error=" ".join(res["errors"])))
        return _back(request, _url(sid, msg="Auswertung gestartet."))

    @router.post("/settings/datasources/{sid}/dkb/cancel")
    async def cancel(request: Request, sid: int) -> Response:
        ds_of(request, sid)
        ok = D.cancel(sid)
        text = {"msg": "Abbruch angefordert."} if ok else {"error": "Es läuft kein Abruf."}
        return _back(request, _url(sid, **text))

    return router


register_router(make_router)
