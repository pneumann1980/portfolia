"""Assets zusammenführen – Weboberfläche (``/assets/merge``): Kandidaten, Vorschau, Bestätigung, Rückgängig."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from app.assetchange import merge as M
from app.web.app import register_router
from app.web.deps import get_ctx, render


def _back(request: Request, target: str) -> Response:
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": target})
    return Response(status_code=303, headers={"Location": target})


def _assets(ctx: Any) -> list[Any]:
    pf = ctx.portfolio()
    if pf is None:
        return []
    return sorted((a for a in pf.assets.values() if not a.is_fiat), key=lambda a: (a.name.lower(), a.asset_id))


def _page(request: Request, msg: str = "", err: str = "", alias: str = "", target: str = "",
          status_code: int = 200) -> HTMLResponse:
    ctx = get_ctx(request)
    return render(request, "asset_merge.html", status_code=status_code, active="quality", msg=msg, err=err,
                  cands=M.candidates(ctx), hist=M.history(ctx.db), assets=_assets(ctx), alias=alias, target=target)


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/assets/merge", response_class=HTMLResponse)
    def overview(request: Request, msg: str = "", err: str = "", alias: str = "", target: str = "") -> HTMLResponse:
        return _page(request, msg[:200], err[:300], alias[:120], target[:120])

    @router.post("/assets/merge/preview", response_class=HTMLResponse)
    async def preview(request: Request) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        alias, target = str(f.get("alias") or "").strip()[:120], str(f.get("target") or "").strip()[:120]
        if str(f.get("swap") or "") == "1":
            alias, target = target, alias
        p = await run_in_threadpool(M.preview, ctx, alias, target)
        if not p.ok and (p.alias_info is None or p.target_info is None):
            return _page(request, err=" ".join(p.errors), alias=alias, target=target, status_code=400)
        return render(request, "asset_merge_preview.html", active="quality", p=p)

    @router.post("/assets/merge/apply")
    async def apply(request: Request) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        alias, target = str(f.get("alias") or "").strip()[:120], str(f.get("target") or "").strip()[:120]
        if str(f.get("confirm") or "") != "1":
            return _back(request, "/assets/merge?" + urlencode({"err": "Bitte die Vorschau bestätigen."}))
        mid, errors = await run_in_threadpool(M.apply, ctx, alias, target, str(f.get("fp") or ""),
                                              str(f.get("note") or ""))
        if mid is None:
            return _back(request, "/assets/merge?" + urlencode({"err": " ".join(errors), "alias": alias,
                                                                 "target": target}))
        return _back(request, "/assets/merge?" + urlencode({"msg": f"„{alias}“ ist jetzt „{target}“ zugeordnet – "
                                                                    "unter „Durchgeführt“ rückgängig zu machen."}))

    @router.post("/assets/merge/{mid}/revert")
    async def revert(request: Request, mid: int) -> Response:
        errors = await run_in_threadpool(M.revert, get_ctx(request), mid)
        if errors:
            return _back(request, "/assets/merge?" + urlencode({"err": " ".join(errors)}))
        return _back(request, "/assets/merge?" + urlencode({"msg": "Zuordnung zurückgenommen."}))

    return router


register_router(make_router)
