"""Konfliktklärung – Weboberfläche.

* ``/quality/klaerung``: Fälle mit Zustand, getrennten Qualitätsdimensionen und dem, was noch fehlt; Durchgang starten;
  Einstellungen (die automatische Übernahme folgt den Autopilot-Einstellungen).
* ``/quality/klaerung/{fall}``: Dossier, Kandidaten mit Beleglage, Hypothesen mit Simulationsergebnis, Entscheidung samt
  Regelprüfung, gezielte Bestandsfrage (Abschlussdialog), ausdrückliche Übernahme, Verlauf.
"""

from __future__ import annotations

import json
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from app.diagnosis import clarify as K
from app.diagnosis import derivatives as DV
from app.diagnosis import stockconfirm as SC
from app.web.app import register_router
from app.web.deps import get_ctx, render


def _back(target: str) -> Response:
    return Response(status_code=303, headers={"Location": target})


def _url(base: str, msg: str = "", err: str = "") -> str:
    q = {k: v[:300] for k, v in (("msg", msg), ("err", err)) if v}
    return base + (f"?{urlencode(q)}" if q else "")


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/quality/klaerung", response_class=HTMLResponse)
    def index(request: Request, msg: str = "", err: str = "") -> HTMLResponse:
        ctx = get_ctx(request)
        cases = K.stored_cases(ctx.db)
        open_ = [c for c in cases if c["status"] in K.OPEN_STATUSES or c["status"] == "zurueck"]
        done = [c for c in cases if c["status"] == "geloest"]
        pf = ctx.recorded_portfolio()
        econ = DV.economics(pf.txs) if pf is not None else []
        return render(request, "klaerung.html", active="quality", open_=open_, done=done, status=K.STATUS,
                      dim_order=K.DIM_ORDER, dim_label=K.DIM_LABEL, dim_state=K.DIM_STATE, econ=econ,
                      auto=K.auto_allowed(ctx), research=bool(ctx.settings.get("clarify.research", True)),
                      enabled=bool(ctx.settings.get("clarify.enabled", True)),
                      followup=K.pending_followup(), dims_text=K.dims_text, msg=msg[:300], err=err[:300])

    @router.post("/quality/klaerung/lauf")
    async def run(request: Request) -> Response:
        ctx = get_ctx(request)
        out = await run_in_threadpool(K.run, ctx, "manual")
        if out is None:
            return _back(_url("/quality/klaerung", err="Ein Durchgang läuft bereits – ein Folgelauf ist vorgemerkt."))
        n = len(out["applied"])
        return _back(_url("/quality/klaerung", f"Durchgang abgeschlossen: {out['cases']} Fall/Fälle untersucht"
                                               + (f", {n} automatisch übernommen" if n else "") + "."))

    @router.post("/quality/klaerung/einstellungen")
    async def settings(request: Request) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        ctx.settings.set("clarify.enabled", str(f.get("enabled") or "") == "1")
        ctx.settings.set("clarify.research", str(f.get("research") or "") == "1")
        return _back(_url("/quality/klaerung", "Einstellung gespeichert."))

    @router.post("/quality/klaerung/rueckgaengig/{token}")
    async def undo(request: Request, token: str) -> Response:
        res = await run_in_threadpool(K.undo_run, get_ctx(request), token)
        return _back(_url("/quality/klaerung", res.message, "; ".join(res.errors)))

    @router.get("/quality/klaerung/{key}", response_class=HTMLResponse)
    def case_page(request: Request, key: str, msg: str = "", err: str = "") -> HTMLResponse:
        ctx = get_ctx(request)
        case = next(iter(K.analyse(ctx, only=key)), None)
        stored = next((c for c in K.stored_cases(ctx.db) if c["key"] == key), None)
        if case is None and stored is None:
            return render(request, "klaerung_case.html", status_code=404, active="quality", case=None, stored=None,
                          runs=[], msg="", err="Fall nicht gefunden.", status=K.STATUS, evidence=K.EVIDENCE,
                          dim_order=K.DIM_ORDER, dim_label=K.DIM_LABEL, dim_state=K.DIM_STATE, dims_text=K.dims_text)
        return render(request, "klaerung_case.html", active="quality", case=case, stored=stored,
                      runs=K.runs_of(ctx.db, key), status=K.STATUS, evidence=K.EVIDENCE, dim_order=K.DIM_ORDER,
                      dim_label=K.DIM_LABEL, dim_state=K.DIM_STATE, dims_text=K.dims_text, auto=K.auto_allowed(ctx),
                      tokens=lambda h: json.dumps({c.key: c.plan.token for c in h.cands}), msg=msg[:300],
                      err=err[:300])

    @router.post("/quality/klaerung/{key}/bestand")
    async def stock(request: Request, key: str) -> Response:
        ctx = get_ctx(request)
        f = await request.form()

        def g(n: str) -> str:
            return str(f.get(n) or "").strip()

        res = await run_in_threadpool(
            SC.submit, ctx, key, g("account"), g("asset"), g("qty"), g("as_of"), g("time"), g("tz"), g("source") or
            "api", g("note"), subaccount=g("subaccount"), scope=g("scope"), qty_locked=g("qty_locked"),
            qty_margin=g("qty_margin"), qty_pnl=g("qty_pnl"), apply=g("apply") == "1", hyp_id=g("hyp"))
        return _back(_url(f"/quality/klaerung/{key}", res.message if res.ok else "", "; ".join(res.errors)))

    @router.post("/quality/klaerung/{key}/uebernehmen")
    async def apply(request: Request, key: str) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        try:
            tokens = json.loads(str(f.get("tokens") or "{}"))
        except ValueError:
            tokens = None
        res = await run_in_threadpool(K.apply_hypothesis, ctx, key, str(f.get("hyp") or ""), "manual",
                                      authorization="user", expect_version=str(f.get("version") or "") or None,
                                      expect_tokens=tokens)
        return _back(_url(f"/quality/klaerung/{key}", res.message if res.ok else "", "; ".join(res.errors)))

    @router.post("/quality/klaerung/{key}/zurueckstellen")
    async def defer(request: Request, key: str) -> Response:
        f = await request.form()
        res = await run_in_threadpool(K.defer, get_ctx(request), key, str(f.get("note") or ""))
        return _back(_url(f"/quality/klaerung/{key}", res.message, "; ".join(res.errors)))

    @router.post("/quality/klaerung/{key}/oeffnen")
    async def reopen(request: Request, key: str) -> Response:
        res = await run_in_threadpool(K.reopen, get_ctx(request), key)
        return _back(_url(f"/quality/klaerung/{key}", res.message, "; ".join(res.errors)))

    return router


register_router(make_router)
