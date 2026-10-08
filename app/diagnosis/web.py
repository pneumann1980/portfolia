"""Diagnoseansicht „Datenqualität“: priorisierte Befunde mit Empfehlung, Bestandsabgleich, Korrekturen.

Die Seite selbst ändert nichts – „Erneut prüfen“ ist ein gewöhnlicher Seitenaufruf, die Diagnose wird bei jedem Aufruf
neu berechnet. Korrekturen gibt es nur über die Vorschau (``GET /quality/diagnose/plan`` – rechnet auf einer Kopie)
und ein ausdrückliches „Übernehmen“ (``POST``, mit Prüfsumme der Vorschau). Jede Korrektur lässt sich unter
„Entscheidungen und Korrekturen“ zurücknehmen; „als geprüft markieren“ ändert keine Daten.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from app.diagnosis import actions as A
from app.diagnosis.engine import report_for
from app.diagnosis.model import HOLDING_STATUS, KINDS, STATUS, STATUS_BADGE
from app.diagnosis.recommend import recommend
from app.web.app import register_router
from app.web.deps import get_ctx, render

log = logging.getLogger(__name__)

TYPE_LABEL = {"buy": "Kauf", "sell": "Verkauf", "trade": "Tausch", "deposit": "Zugang", "withdrawal": "Abgang",
              "transfer": "Transfer", "corporate_action": "Kapitalmaßnahme"}
ORIGIN_LABEL = {"import": "kuratierter Import", "journal": "App-Buchung", "plan": "Sparplan"}


def _utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%d.%m.%Y %H:%M:%S UTC")


def _common() -> dict[str, Any]:
    return {"type_label": TYPE_LABEL, "origin_label": ORIGIN_LABEL, "utc_fmt": _utc, "status_badge": STATUS_BADGE,
            "holding_status": HOLDING_STATUS}


def _params(data: Mapping[str, Any] | Any) -> tuple[dict[str, list[str]], bool]:
    """Eingaben einer Lösung (Felder ``p_<name>``); ``p__set`` = Formular wurde abgeschickt."""
    out: dict[str, list[str]] = {}
    for k in data:
        if k.startswith("p_") and k != "p__set":
            out[k[2:]] = [str(v) for v in data.getlist(k)]
    return out, "p__set" in data


def _back(target: str) -> Response:
    return Response(status_code=303, headers={"Location": target})


def _page_url(msg: str = "", err: str = "", anchor: str = "") -> str:
    q = {k: v for k, v in (("msg", msg), ("err", err)) if v}
    return "/quality/diagnose" + (f"?{urlencode(q)}" if q else "") + (f"#{anchor}" if anchor else "")


def _plan_page(request: Request, fid: str, opt: str, params: dict[str, list[str]], given: bool,
               errors: list[str] | None = None, status_code: int = 200) -> HTMLResponse:
    ctx = get_ctx(request)
    report = report_for(ctx)
    f = report.by_id(fid)
    if f is None:
        return render(request, "diagnosis_plan.html", status_code=404, active="quality", f=None, plan=None,
                      effects=None, rec=None, errors=errors or ["Der Befund besteht nicht mehr – die Daten haben sich "
                                                               "geändert. Bitte die Diagnose neu öffnen."], **_common())
    rec = recommend(report, f)
    plan = A.build_plan(ctx, report, f, opt, params, given=given)
    effects = A.preview(ctx, report, plan) if not plan.errors else None
    return render(request, "diagnosis_plan.html", status_code=status_code, active="quality", f=f, rec=rec,
                  plan=plan, effects=effects, errors=errors or [], **_common())


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/quality/diagnose", response_class=HTMLResponse)
    def diagnose_page(request: Request, kind: str = "", status: str = "", f: str = "", msg: str = "",
                      err: str = "") -> HTMLResponse:
        ctx = get_ctx(request)
        kind = kind if kind in KINDS else ""
        status = status if status in STATUS else ""
        report = report_for(ctx)
        marks = A.active_dismissals(ctx.db)
        prints = {x.id: A.fingerprint(x) for x in report.findings if x.id in marks}
        checked = {fid for fid, d in marks.items() if fid in prints and d.fingerprint == prints[fid]}
        changed = {fid: d for fid, d in marks.items() if fid in prints and d.fingerprint != prints[fid]}
        open_findings = [x for x in report.findings if x.id not in checked]
        shown = [x for x in open_findings if (not kind or x.kind == kind) and (not status or x.status == status)]
        dismissed = [x for x in report.findings if x.id in checked]
        counts: dict[str, dict[str, int]] = {k: {} for k in KINDS}
        for x in open_findings:
            counts[x.kind][x.status] = counts[x.kind].get(x.status, 0) + 1
        by_kind = {k: sum(1 for x in open_findings if x.kind == k and (not status or x.status == status))
                   for k in KINDS}
        by_status = {s: sum(1 for x in open_findings if x.status == s and (not kind or x.kind == kind))
                     for s in STATUS}
        accounts: dict[str, list[Any]] = {}
        for h in report.holdings:
            accounts.setdefault(h.account, []).append(h)
        return render(request, "diagnosis.html", active="quality", report=report, shown=shown, kind=kind,
                      status=status, open_id=f, kinds=KINDS, statuses=STATUS, by_kind=by_kind, by_status=by_status,
                      accounts=accounts, counts=counts, holding_counts=report.holding_counts(),
                      generated=datetime.now(UTC), n_open=len(open_findings),
                      recs={x.id: recommend(report, x) for x in [*shown, *dismissed]}, dismissed=dismissed,
                      marks=marks, changed=changed, fixes=A.active_fixes(ctx.db), decisions=A.decisions(ctx.db),
                      msg=msg, err=err, **_common())

    @router.get("/quality/diagnose/plan", response_class=HTMLResponse)
    def plan_page(request: Request, f: str = "", o: str = "") -> HTMLResponse:
        """Vorschau einer Lösung – rechnet auf einer Kopie im Speicher, ändert nichts."""
        params, given = _params(request.query_params)
        return _plan_page(request, f, o, params, given)

    @router.post("/quality/diagnose/apply")
    async def apply_route(request: Request) -> Response:
        ctx = get_ctx(request)
        form = await request.form()
        fid, opt, token = str(form.get("f") or ""), str(form.get("o") or ""), str(form.get("token") or "")
        if not fid or not opt or not token:
            raise HTTPException(400)
        params, _given = _params(form)
        res = await run_in_threadpool(A.apply, ctx, fid, opt, params, token)
        if not res.ok:
            return await run_in_threadpool(_plan_page, request, fid, opt, params, True, res.errors, 409)
        return _back(_page_url(msg=res.message, anchor="decisions"))

    @router.post("/quality/diagnose/dismiss")
    async def dismiss_route(request: Request) -> Response:
        ctx = get_ctx(request)
        form = await request.form()
        fid = str(form.get("f") or "")
        if not fid:
            raise HTTPException(400)
        res = await run_in_threadpool(A.dismiss, ctx, fid, str(form.get("note") or ""))
        if str(form.get("back") or "") == "/quality/integrity":
            q = urlencode({"msg": res.message} if res.ok else {"err": "; ".join(res.errors)})
            return _back(f"/quality/integrity?{q}#items")
        return _back(_page_url(msg=res.message, err="; ".join(res.errors), anchor="checked" if res.ok else ""))

    @router.post("/quality/diagnose/decision/{did}/undo")
    async def undo_route(request: Request, did: int) -> Response:
        res = await run_in_threadpool(A.undo, get_ctx(request), did)
        return _back(_page_url(msg=res.message, err="; ".join(res.errors), anchor="decisions"))

    @router.post("/quality/diagnose/decision/{did}/reopen")
    async def reopen_route(request: Request, did: int) -> Response:
        res = await run_in_threadpool(A.reopen, get_ctx(request), did)
        return _back(_page_url(msg=res.message, err="; ".join(res.errors), anchor="findings"))

    return router


register_router(make_router)
