"""Ticker-/Token-Änderungen – Weboberfläche (registriert sich beim Import in main).

Ablauf: Hinweis bzw. Asset wählen → Formular (vorbefüllt) → Vorschau mit allen Buchungen bzw. Kursquellen →
Übernehmen → jederzeit rückgängig. Routen unter ``/changes`` (``/asset/{id:path}`` ist der Detailseite vorbehalten).
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from app.assetchange.detect import hints
from app.assetchange.service import KINDS, asset_change_service
from app.web.app import register_router
from app.web.deps import get_ctx, render

FIELDS = ("kind", "date", "ratio", "new_asset", "new_name", "quote_source", "quote_id", "new_ticker", "note", "fp")


def _back(request: Request, target: str) -> Response:
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": target})
    return Response(status_code=303, headers={"Location": target})


def _assets(ctx: Any) -> list[Any]:
    pf = ctx.portfolio()
    if pf is None:
        return []
    return sorted((a for a in pf.assets.values() if not a.is_fiat), key=lambda a: a.name.lower())


def _form_page(request: Request, asset_id: str, form: dict[str, str], hint: str = "", errors: list[str] | None = None,
               status_code: int = 200) -> HTMLResponse:
    ctx = get_ctx(request)
    pf = ctx.portfolio()
    a = pf.assets.get(asset_id) if pf is not None else None
    if a is None:
        raise HTTPException(404)
    led = ctx.ledger()
    held = {acc: q for (acc, aid), q in (led.balances.items() if led else []) if aid == asset_id and q}
    h = next((x for x in hints(ctx) if x.key == hint), None) if hint else None
    return render(request, "asset_change_form.html", status_code=status_code, active="quality", a=a, form=form,
                  hint=h, hint_key=hint, errors=errors or [], kinds=KINDS, held=held, assets=_assets(ctx),
                  changes=asset_change_service(ctx).changes(asset_id))


def _overview(request: Request, asset: str = "", done: int = 0, confirm: int = 0, errors: list[str] | None = None,
              status_code: int = 200) -> HTMLResponse:
    ctx = get_ctx(request)
    svc = asset_change_service(ctx)
    rows = svc.changes(asset or None)
    txs = {int(r["id"]): json.loads(r["tx_ids_json"] or "[]") for r in rows}
    dismissed = ctx.db.q("SELECT * FROM asset_change WHERE kind='hint' AND status='dismissed' ORDER BY id DESC")
    return render(request, "asset_changes.html", status_code=status_code, active="quality", hints=hints(ctx),
                  changes=rows, txs=txs, asset=asset, done=done, confirm=confirm, kinds=KINDS, assets=_assets(ctx),
                  dismissed=dismissed, errors=errors or [])


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/changes", response_class=HTMLResponse)
    def overview(request: Request, asset: str = "", done: int = 0, confirm: int = 0) -> HTMLResponse:
        return _overview(request, asset, done, confirm)

    @router.get("/changes/new", response_class=HTMLResponse)
    def new(request: Request, asset: str = "", hint: str = "") -> HTMLResponse:
        if not asset:
            return _back(request, "/changes")  # type: ignore[return-value]
        form = {k: str(request.query_params.get(k) or "") for k in FIELDS}
        form["kind"] = form["kind"] if form["kind"] in KINDS else "migration"
        return _form_page(request, asset, form, hint)

    @router.post("/changes/preview", response_class=HTMLResponse)
    async def preview(request: Request) -> HTMLResponse:
        ctx = get_ctx(request)
        f = await request.form()
        asset = str(f.get("asset") or "")
        form = {k: str(f.get(k) or "").strip() for k in FIELDS}
        hint = str(f.get("hint") or "")
        plan = await run_in_threadpool(asset_change_service(ctx).plan, asset, form)
        if plan.old is None:
            raise HTTPException(404)
        if not plan.ok:
            return _form_page(request, asset, form, hint, plan.errors, 400)
        back = "/changes/new?" + urlencode({"asset": asset, "hint": hint, **form})
        return render(request, "asset_change_preview.html", active="quality", p=plan, a=plan.old, hint_key=hint,
                      kinds=KINDS, back_url=back)

    @router.post("/changes/apply")
    async def apply(request: Request) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        asset = str(f.get("asset") or "")
        form = {k: str(f.get(k) or "").strip() for k in FIELDS}
        hint = str(f.get("hint") or "") or None
        cid, plan = await run_in_threadpool(asset_change_service(ctx).apply, asset, form, hint)
        if cid is None:
            if plan.old is None:
                raise HTTPException(404)
            return _form_page(request, asset, form, hint or "", plan.errors, 400)
        return _back(request, f"/changes?{urlencode({'done': cid})}")

    @router.post("/changes/{cid}/revert")
    async def revert(request: Request, cid: int) -> Response:
        f = await request.form()
        if f.get("confirm") != "1":
            return _back(request, f"/changes?confirm={cid}#c{cid}")
        errors = await run_in_threadpool(asset_change_service(get_ctx(request)).revert, cid)
        if errors:
            return _overview(request, errors=errors, status_code=409)
        return _back(request, "/changes")

    @router.post("/changes/dismiss")
    async def dismiss(request: Request) -> Response:
        f = await request.form()
        key, asset = str(f.get("hint") or ""), str(f.get("asset") or "")
        if key and asset:
            asset_change_service(get_ctx(request)).dismiss(key, asset)
        return _back(request, "/changes")

    @router.post("/changes/undismiss")
    async def undismiss(request: Request) -> Response:
        f = await request.form()
        asset_change_service(get_ctx(request)).undismiss(str(f.get("hint") or ""))
        return _back(request, "/changes")

    return router


register_router(make_router)
