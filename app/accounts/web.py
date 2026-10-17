"""Konten – Weboberfläche (``/accounts``): Übersicht, anlegen, Stammdaten, umbenennen, löschen."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from app.accounts import service as S
from app.assetchange import account_merge as AM
from app.web.app import register_router
from app.web.deps import get_ctx, render


def _back(request: Request, target: str) -> Response:
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": target})
    return Response(status_code=303, headers={"Location": target})


def _page(request: Request, msg: str = "", err: str = "", status_code: int = 200) -> HTMLResponse:
    ctx = get_ctx(request)
    return render(request, "accounts.html", status_code=status_code, active="settings", msg=msg, err=err,
                  rows=S.rows(ctx), brokers=sorted({r.broker for r in S.rows(ctx) if r.broker}, key=str.casefold),
                  groups=sorted({r.depot_group for r in S.rows(ctx) if r.depot_group}, key=str.casefold))


def _s(f: Any, key: str, limit: int = 160) -> str:
    return str(f.get(key) or "").strip()[:limit]


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/accounts", response_class=HTMLResponse)
    def overview(request: Request, msg: str = "", err: str = "") -> HTMLResponse:
        return _page(request, msg[:200], err[:300])

    @router.post("/accounts/new")
    async def new(request: Request) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        name, err = await run_in_threadpool(S.create, ctx, _s(f, "name"), _s(f, "broker"), _s(f, "depot_group"),
                                            _s(f, "note", S.NOTE_MAX))
        if err:
            return _back(request, "/accounts?" + urlencode({"err": err}))
        return _back(request, "/accounts?" + urlencode({"msg": f"Konto „{name}“ angelegt – es steht jetzt bei "
                                                                 "Buchungen und Datenquellen zur Auswahl."}))

    @router.post("/accounts/meta")
    async def meta(request: Request) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        name = _s(f, "account")
        err = await run_in_threadpool(S.update, ctx, name, _s(f, "broker"), _s(f, "depot_group"),
                                      _s(f, "note", S.NOTE_MAX))
        if err:
            return _back(request, "/accounts?" + urlencode({"err": err}))
        return _back(request, "/accounts?" + urlencode({"msg": f"Stammdaten von „{name}“ gespeichert."}))

    @router.post("/accounts/rename", response_class=HTMLResponse)
    async def rename(request: Request) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        old = _s(f, "account")
        new_name, err = await run_in_threadpool(S.start_rename, ctx, old, _s(f, "new_name"))
        if err:
            return _back(request, "/accounts?" + urlencode({"err": err}))
        p = await run_in_threadpool(AM.preview, ctx, old, new_name)
        return render(request, "account_merge_preview.html", active="quality", p=p)

    @router.post("/accounts/delete")
    async def delete(request: Request) -> Response:
        ctx = get_ctx(request)
        name = _s(await request.form(), "account")
        err = await run_in_threadpool(S.delete_empty, ctx, name)
        if err:
            return _back(request, "/accounts?" + urlencode({"err": err}))
        return _back(request, "/accounts?" + urlencode({"msg": f"Konto „{name}“ gelöscht."}))

    return router


register_router(make_router)
