"""Entscheidungsregeln und Autopilot – Weboberfläche.

* ``/quality/diagnose/regeln``: Regeln anlegen, ausschalten, anwenden, gemeinsam rückgängig machen.
* ``/quality/autopilot``: Einstellung, Bericht des letzten Laufs, Verlauf, gemeinsames Rückgängig.

Regeln und Autopilot ändern keine Rohdaten; Korrekturen des Autopiloten sind nur mit eigener, ausdrücklich bestätigter
Einstellung möglich (siehe :mod:`app.diagnosis.autopilot`).
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from app.diagnosis import autopilot as AP
from app.diagnosis import queue as Q
from app.diagnosis import rules as R
from app.diagnosis import triage as TR
from app.diagnosis.engine import report_for
from app.diagnosis.model import KINDS, STATUS_BADGE
from app.diagnosis.recommend import recommend
from app.web.app import register_router
from app.web.deps import get_ctx, render


def _back(target: str) -> Response:
    return Response(status_code=303, headers={"Location": target})


def _url(base: str, msg: str = "", err: str = "", anchor: str = "") -> str:
    q = {k: v[:300] for k, v in (("msg", msg), ("err", err)) if v}
    return base + (f"?{urlencode(q)}" if q else "") + (f"#{anchor}" if anchor else "")


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/quality/diagnose/regeln", response_class=HTMLResponse)
    def rules_page(request: Request, msg: str = "", err: str = "") -> HTMLResponse:
        ctx = get_ctx(request)
        report = report_for(ctx)
        open_ = TR.open_findings(ctx, report)
        rows = []
        for r in R.rules(ctx.db):
            rows.append({"r": r, "n": len(R.preview(ctx, r, report, open_)) if r.active else 0})
        accounts = sorted({a for f in open_ if f.status in ("hinweis", "verdacht") for a in f.accounts})
        assets = sorted({a for f in open_ if f.status in ("hinweis", "verdacht") for a in f.assets})
        kinds = {k: v for k, v in KINDS.items() if any(f.kind == k and f.status in ("hinweis", "verdacht")
                                                       for f in open_)}
        return render(request, "diagnosis_rules.html", active="quality", rows=rows, accounts=accounts, assets=assets,
                      kinds=kinds, all_kinds=KINDS, recent=R.recent(ctx.db), msg=msg[:300], err=err[:300])

    @router.post("/quality/diagnose/regeln/neu")
    async def rule_new(request: Request) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        res = await run_in_threadpool(R.create, ctx, str(f.get("kind") or ""), str(f.get("account") or ""),
                                      str(f.get("asset") or ""), str(f.get("max_status") or "hinweis"),
                                      str(f.get("note") or ""))
        return _back(_url("/quality/diagnose/regeln", res.message, "; ".join(res.errors)))

    @router.post("/quality/diagnose/regeln/anwenden")
    async def rules_apply(request: Request) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        only = str(f.get("rule") or "")
        res, _token = await run_in_threadpool(R.apply_all, ctx, int(only) if only.isdigit() else None)
        return _back(_url("/quality/diagnose/regeln", res.message, "; ".join(res.errors)))

    @router.post("/quality/diagnose/regeln/{rid}/aus")
    async def rule_off(request: Request, rid: int) -> Response:
        f = await request.form()
        res = await run_in_threadpool(R.switch_off, get_ctx(request), rid, revert=str(f.get("revert") or "") == "1")
        return _back(_url("/quality/diagnose/regeln", res.message, "; ".join(res.errors)))

    @router.post("/quality/diagnose/regeln/{rid}/an")
    async def rule_on(request: Request, rid: int) -> Response:
        res = await run_in_threadpool(R.switch_on, get_ctx(request), rid)
        return _back(_url("/quality/diagnose/regeln", res.message, "; ".join(res.errors)))

    @router.post("/quality/diagnose/regeln/zurueck/{token}")
    async def rules_undo(request: Request, token: str) -> Response:
        n = await run_in_threadpool(R.undo, get_ctx(request), token)
        return _back(_url("/quality/diagnose/regeln", f"{n} Markierung(en) zurückgenommen." if n else "",
                          "" if n else "Nichts zurückzunehmen."))

    @router.get("/quality/autopilot", response_class=HTMLResponse)
    def autopilot_page(request: Request, msg: str = "", err: str = "") -> HTMLResponse:
        ctx = get_ctx(request)
        runs = AP.last_runs(ctx.db, 15)
        return render(request, "autopilot.html", active="quality", runs=runs, last=runs[0] if runs else None,
                      kinds=KINDS, status_badge=STATUS_BADGE, on=AP.enabled(ctx),
                      use_rules=bool(ctx.settings.get("autopilot.rules", True)),
                      use_fixes=bool(ctx.settings.get("autopilot.fixes", False)),
                      level=str(ctx.settings.get("autopilot.fixes_level", "eindeutig")), msg=msg[:300], err=err[:300])

    @router.post("/quality/autopilot/einstellungen")
    async def autopilot_settings(request: Request) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        on = str(f.get("enabled") or "") == "1"
        fixes = str(f.get("fixes") or "") == "1"
        if fixes and str(f.get("fixes_confirm") or "") != "1":
            return _back(_url("/quality/autopilot", err="Für automatische Korrekturen bitte bestätigen."))
        ctx.settings.set("autopilot.enabled", on)
        ctx.settings.set("autopilot.rules", str(f.get("rules") or "") == "1")
        ctx.settings.set("autopilot.fixes", fixes)
        level = str(f.get("fixes_level") or "eindeutig")
        ctx.settings.set("autopilot.fixes_level", level if level in AP.LEVELS else "eindeutig")
        return _back(_url("/quality/autopilot", "Einstellung gespeichert."))

    @router.post("/quality/autopilot/lauf")
    async def autopilot_run(request: Request) -> Response:
        ctx = get_ctx(request)
        out = await run_in_threadpool(AP.run, ctx, "manual")
        return _back(_url("/quality/autopilot", "Durchgang abgeschlossen." if out else "", "" if out else
                          "Ein Durchgang läuft bereits."))

    @router.post("/quality/autopilot/{token}/undo")
    async def autopilot_undo(request: Request, token: str) -> Response:
        res = await run_in_threadpool(AP.undo, get_ctx(request), token)
        return _back(_url("/quality/autopilot", res.message, "; ".join(res.errors)))

    @router.get("/quality/diagnose/liste", response_class=HTMLResponse)
    def queue_page(request: Request, f: str = "", msg: str = "", err: str = "") -> HTMLResponse:
        """Entscheidungsliste: ein Fall nach dem anderen (ändert nichts)."""
        from app.diagnosis.web import _common

        ctx = get_ctx(request)
        report = report_for(ctx)
        items = Q.queue(ctx, report)
        i = Q.position(items, f)
        cur = items[i] if items else None
        return render(request, "diagnosis_queue.html", active="quality", items=items, i=i, cur=cur,
                      rec=recommend(report, cur) if cur is not None else None,
                      prev_id=items[i - 1].id if i > 0 else "", next_id=items[i + 1].id if i + 1 < len(items) else "",
                      msg=msg[:300], err=err[:300], **_common())

    return router


register_router(make_router)
