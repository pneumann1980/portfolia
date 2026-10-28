"""Assistent der Datenqualität (Seite): die nächste Aufgabe mit einem Hauptknopf; nach jeder Aktion die nächste.

``GET /quality/assistent`` wählt die erste offene Aufgabe (:mod:`app.diagnosis.assistant`):
Einzelbefunde zeigt die Seite selbst (Empfehlung samt berechneter Wirkung und **einem** Übernehmen-Knopf, wenn die
Lösung belegt, ohne Auswahlparameter und ohne Vorbehalt ist); Klärungsfälle, Prüfstapel und Prüfbericht öffnen ihre
Seite. Das Cookie ``dq_flow`` hält den Modus (:mod:`app.web.flow` leitet erledigte Aktionen hierher zurück).
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.diagnosis import actions as A
from app.diagnosis import assistant as AS
from app.diagnosis.engine import report_for
from app.diagnosis.recommend import recommend
from app.diagnosis.web import _common
from app.web.app import register_router
from app.web.deps import get_ctx, render

log = logging.getLogger(__name__)
FLOW_MAX_AGE = 4 * 3600
COOKIES = ("dq_flow", "dq_cur", "dq_skip")


def _set(resp: Response, step_key: str, skip: list[str]) -> None:
    kw = {"max_age": FLOW_MAX_AGE, "path": "/", "samesite": "lax", "httponly": True}
    resp.set_cookie("dq_flow", "1", **kw)  # type: ignore[arg-type]
    resp.set_cookie("dq_cur", AS.kid(step_key) if step_key else "", **kw)  # type: ignore[arg-type]
    resp.set_cookie("dq_skip", ",".join(skip), **kw)  # type: ignore[arg-type]


def _clear(resp: Response) -> None:
    for c in COOKIES:
        resp.delete_cookie(c, path="/")


def _with_msg(href: str, msg: str, err: str) -> str:
    q = {k: v for k, v in (("msg", msg), ("err", err)) if v}
    if not q:
        return href
    base, _, anchor = href.partition("#")
    return base + ("&" if "?" in base else "?") + urlencode(q) + (f"#{anchor}" if anchor else "")


def _one_click(ctx: Any, report: Any, f: Any, prim: Any, rec: Any) -> dict[str, Any] | None:
    """Vorschau der empfohlenen Lösung für einen Ein-Klick-Knopf – nur bei belegter, unbedingter Empfehlung ohne
    Auswahlparameter. Sonst bleibt der Weg über die Vorschau-Seite."""
    if prim is None or prim.params or rec.conditional or f.status != "belegt":
        return None
    plan = A.build_plan(ctx, report, f, prim.key, {}, given=False)
    if plan.errors or not plan.ops:
        return None
    return {"plan": plan, "effects": A.preview(ctx, report, plan)}


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/quality/assistent", response_class=HTMLResponse)
    def assistant(request: Request, msg: str = "", err: str = "", skip: str = "", new: int = 0) -> Response:
        ctx = get_ctx(request)
        skipped = [] if new else AS.parse_skip(request.cookies.get("dq_skip", ""))
        extra = AS.parse_skip(skip)
        skipped = [*skipped, *[x for x in extra if x not in skipped]][:AS.MAX_SKIP]
        step = AS.next_step(ctx, skipped)
        item = step.item
        if item is None:
            resp = render(request, "assistant.html", active="quality", step=step, item=None, f=None, msg=msg, err=err,
                          **_common())
            _clear(resp)
            return resp
        if item.kind != "befund" or not item.refs:
            target = _with_msg(item.href, msg, err)
            resp = RedirectResponse(target, status_code=303)
            _set(resp, item.key, skipped)
            return resp
        report = report_for(ctx)
        f = report.by_id(item.refs[0])
        if f is None:  # Befund inzwischen verschwunden: nächste Aufgabe
            return RedirectResponse(_with_msg(f"/quality/assistent?skip={AS.kid(item.key)}", msg, err), status_code=303)
        rec = recommend(report, f)
        prim = rec.primary
        one = _one_click(ctx, report, f, prim, rec)
        resp = render(request, "assistant.html", active="quality", step=step, item=item, f=f, rec=rec, prim=prim,
                      one=one, kid=AS.kid(item.key), msg=msg, err=err, **_common())
        _set(resp, item.key, skipped)
        return resp

    @router.get("/quality/assistent/ende")
    def finish(request: Request) -> Response:
        resp = RedirectResponse("/quality", status_code=303)
        _clear(resp)
        return resp

    return router


register_router(make_router)
