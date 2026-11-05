"""Weboberfläche: Fiat-Schalter (``/settings/fiat``) und Stichtag (``/settings/stichtag``)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from app.cutover import core as CO
from app.cutover import corrections as CR
from app.cutover import fiat as FI
from app.cutover import fix as FX
from app.cutover import service as CS
from app.web.app import register_router
from app.web.deps import get_ctx, render


def _back(request: Request, target: str) -> Response:
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": target})
    return Response(status_code=303, headers={"Location": target})


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/settings/fiat", response_class=HTMLResponse)
    async def fiat_page(request: Request, saved: str | None = None) -> HTMLResponse:
        ctx = get_ctx(request)
        pv = await run_in_threadpool(FI.preview, ctx)
        return render(request, "fiat_preview.html", active="settings", p=pv, saved=saved, errors=[])

    @router.post("/settings/fiat")
    async def fiat_switch(request: Request) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        off = str(f.get("fiat_off") or "") == "1"
        res = await run_in_threadpool(FI.switch, ctx, off)
        if res["errors"]:
            pv: dict[str, Any] = await run_in_threadpool(FI.preview, ctx)
            return render(request, "fiat_preview.html", status_code=409, active="settings", p=pv, saved=None,
                          errors=res["errors"])
        return _back(request, "/settings/fiat?saved=" + ("off" if off else "on"))

    def _page(request: Request, cutoff: str | None, *, impact: bool = False, derive: bool = False, status: int = 200,
              errors: list[str] | None = None, saved: str | None = None, sim: dict[str, Any] | None = None,
              form: dict[str, Any] | None = None) -> HTMLResponse:
        ctx = get_ctx(request)
        st = FX.state(ctx)  # aktiver Stichtag: dessen Datum gilt, nicht die Auswahl
        c = st["cutoff"] if st else CS.parse_cutoff(cutoff)
        pv = st["preview"] if st else CS.preview(ctx, c, impact=impact, derive=derive)
        if st and (impact or derive):
            pv = CS.preview(ctx, c, impact=impact, derive=derive)
        return render(request, "cutover.html", status_code=status, active="settings", p=pv, st=st,
                      errors=errors or [], saved=saved, sim=sim, form=form or {}, modes=CR.MODES)

    @router.get("/settings/stichtag", response_class=HTMLResponse)
    async def cutover_page(request: Request, cutoff: str | None = None, impact: str | None = None,
                           saved: str | None = None, derive: str | None = None) -> HTMLResponse:
        return await run_in_threadpool(lambda: _page(request, cutoff, impact=impact == "1", derive=derive == "1",
                                                     saved=saved))

    @router.post("/settings/stichtag/fixieren")
    async def cutover_fix(request: Request) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        c = CS.parse_cutoff(str(f.get("cutoff") or ""))
        approved = {str(v) for k, v in f.multi_items() if k == "approve"}
        res = await run_in_threadpool(FX.fix, ctx, c, str(f.get("token") or ""), approved)
        if res["errors"]:
            return await run_in_threadpool(lambda: _page(request, c.isoformat(), status=409, errors=res["errors"]))
        return _back(request, "/settings/stichtag?saved=fixed")

    @router.post("/settings/stichtag/zurueck")
    async def cutover_undo(request: Request) -> Response:
        ctx = get_ctx(request)
        res = await run_in_threadpool(FX.undo, ctx)
        if res["errors"]:
            return await run_in_threadpool(lambda: _page(request, None, status=409, errors=res["errors"]))
        return _back(request, "/settings/stichtag?saved=undone")

    @router.post("/settings/stichtag/freigabe")
    async def cutover_reapprove(request: Request) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        accounts = {str(v) for k, v in f.multi_items() if k == "account"}
        res = await run_in_threadpool(FX.reapprove, ctx, accounts)
        if res["errors"]:
            return await run_in_threadpool(lambda: _page(request, None, status=409, errors=res["errors"]))
        return _back(request, "/settings/stichtag?saved=approved")

    @router.post("/settings/stichtag/korrektur")
    async def cutover_correction(request: Request) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        form = {k: str(v) for k, v in f.items() if k not in ("csrf_token",)}
        c = CS.parse_cutoff(form.get("cutoff"))
        kw = {"account": form.get("account", ""), "asset": form.get("asset", ""), "kind": form.get("kind", ""),
              "qty": form.get("qty", ""), "mode": form.get("mode", ""), "evidence": form.get("evidence", ""),
              "acq_date": form.get("acq_date"), "cost_eur": form.get("cost_eur")}
        if form.get("do") == "preview":
            def sim_() -> HTMLResponse:
                ms = CO.models(ctx, c)
                v, errs = (None, ["Keine Daten."]) if not ms else CR.validate(ctx, ms[0], **kw)
                sim = CR.simulate(ctx, ms[0], v) if v is not None else None
                return _page(request, c.isoformat(), errors=errs, sim=sim, form=form,
                             status=200 if not errs else 422)
            return await run_in_threadpool(sim_)
        res = await run_in_threadpool(lambda: CR.apply(ctx, c, **kw))
        if res["errors"]:
            return await run_in_threadpool(lambda: _page(request, c.isoformat(), status=422, errors=res["errors"],
                                                         form=form))
        return _back(request, f"/settings/stichtag?cutoff={c.isoformat()}&saved=corrected")

    @router.post("/settings/stichtag/korrektur/{cid}/zurueck")
    async def cutover_correction_undo(request: Request, cid: int) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        res = await run_in_threadpool(CR.undo, ctx, cid)
        if res["errors"]:
            return await run_in_threadpool(lambda: _page(request, str(f.get("cutoff") or ""), status=409,
                                                         errors=res["errors"]))
        return _back(request, f"/settings/stichtag?cutoff={f.get('cutoff') or ''}&saved=correction_undone")

    return router


register_router(make_router)
