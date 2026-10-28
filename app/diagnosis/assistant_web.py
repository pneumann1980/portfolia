"""Assistent „Buchungen prüfen“: eine Frage je Aufgabe, Antwort speichern, weiter – Rückgängig auf der nächsten Karte.

``GET /quality/assistent`` zeigt die nächste Aufgabe (:mod:`app.diagnosis.guide`): Diagnose-Befunde als Frage in
Alltagssprache mit Antworten, darunter jeweils die Wirkung; Aufgaben mit eigener Seite (neue Vorgänge,
zusammenhängende Buchungen, Rechenprüfung) erklärt und mit einem Knopf – nach der Aktion dort geht es dank Flow-Modus
(:mod:`app.web.flow`) hier weiter. ``POST …/antwort`` führt die zugeordnete, vorhandene Aktion aus (Korrektur mit
Vorschau-Token bzw. Entscheidung ohne Datenänderung), ``POST …/rueckgaengig`` nimmt die letzte Antwort zurück.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.diagnosis import actions as A
from app.diagnosis import guide as G
from app.diagnosis.web import _common
from app.web.app import register_router
from app.web.deps import get_ctx, render

log = logging.getLogger(__name__)
FLOW_MAX_AGE = 4 * 3600
COOKIES = ("dq_flow", "dq_cur", "dq_skip", "dq_hints")
BASE = "/quality/assistent"
MARK_TEXT = {"defer": "Für später vorgemerkt – nichts geändert.", "reject": "Gespeichert: beide Buchungen bleiben.",
             "dismiss": "Gespeichert: ist in Ordnung so."}


def _set(resp: Response, cur: str, skip: list[str], hints: bool) -> None:
    kw: dict[str, Any] = {"max_age": FLOW_MAX_AGE, "path": "/", "samesite": "lax", "httponly": True}
    resp.set_cookie("dq_flow", "1", **kw)
    resp.set_cookie("dq_cur", cur, **kw)
    resp.set_cookie("dq_skip", ",".join(skip), **kw)
    resp.set_cookie("dq_hints", "1" if hints else "", **kw)


def _clear(resp: Response) -> None:
    for c in COOKIES:
        resp.delete_cookie(c, path="/")


def _go(**q: Any) -> Response:
    qs = urlencode({k: v for k, v in q.items() if v not in (None, "", 0)})
    return RedirectResponse(BASE + (f"?{qs}" if qs else ""), status_code=303)


def _params(form: Any) -> dict[str, list[str]]:
    return {k[2:]: [str(v) for v in form.getlist(k)] for k in form if k.startswith("p_") and k != "p__set"}


def _page(request: Request, *, msg: str = "", err: str = "", skip: str = "", new: int = 0, hints: str = "",
          done: int = 0, gone: int = 0, errors: list[str] | None = None, chosen: str = "",
          values: dict[str, list[str]] | None = None) -> Response:
    ctx = get_ctx(request)
    show_hints = hints == "1" if hints else (request.cookies.get("dq_hints") == "1" and not new)
    skipped = [] if new else G.parse_skip(request.cookies.get("dq_skip", ""))
    for x in G.parse_skip(skip):
        if x not in skipped:
            skipped.append(x)
    st = G.step(ctx, skipped, hints=show_hints)
    undo = None
    if done:
        row = ctx.db.q1("SELECT id, action, status, title FROM diag_decision WHERE id=?", (done,))
        if row is not None and row["status"] == "active":
            undo = dict(row)
    resp = render(request, "guide.html", active="quality", step=st, task=st.task, title=G.TITLE,
                  pos=G.theme_position(st), msg=msg, err=err, errors=errors or [], undo=undo, gone=gone,
                  chosen=chosen, values=values or {}, hints=show_hints, **_common())
    if st.task is None:
        _clear(resp)
    else:
        _set(resp, st.task.kid, skipped, show_hints)
    return resp


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get(BASE, response_class=HTMLResponse)
    def assistant(request: Request, msg: str = "", err: str = "", skip: str = "", new: int = 0, hints: str = "",
                  done: int = 0, gone: int = 0) -> Response:
        return _page(request, msg=msg, err=err, skip=skip, new=new, hints=hints, done=done, gone=gone)

    @router.post(f"{BASE}/antwort")
    async def answer(request: Request) -> Response:
        ctx = get_ctx(request)
        form = await request.form()
        fid, aid = str(form.get("f") or ""), str(form.get("a") or "")
        try:
            before = int(str(form.get("before") or "0"))
        except ValueError:
            before = 0
        if not fid or not aid:
            return _go(err="Bitte eine Antwort auswählen.")

        def gone() -> int:
            return max(0, before - len(G.tasks(ctx)) - 1) if before else 0

        if aid.startswith("mark:"):
            action = aid.split(":", 1)[1]
            res = await run_in_threadpool(A.mark, ctx, fid, action, "Antwort im Assistenten")
            if not res.ok:
                return _go(err=" ".join(res.errors))
            return _go(msg=MARK_TEXT.get(action, "Gespeichert."), done=res.decision_id,
                       gone=await run_in_threadpool(gone))
        if not aid.startswith("opt:"):
            return _go(err="Unbekannte Antwort.")
        key = aid.split(":", 1)[1]
        params = _params(form)
        token = str(form.get(f"t_{aid}") or form.get("token") or "")
        needs = str(form.get(f"n_{aid}") or "") == "1"
        if (needs or not token) and not form.get("confirm"):  # Eingaben bzw. keine Vorschau → Wirkung zeigen
            return await run_in_threadpool(_confirm, request, fid, key, aid, params)
        res = await run_in_threadpool(A.apply, ctx, fid, key, params, token)
        if not res.ok:
            return _go(err=" ".join(res.errors))
        return _go(msg="Gespeichert und korrigiert.", done=res.decision_id, gone=await run_in_threadpool(gone))

    def _confirm(request: Request, fid: str, key: str, aid: str, params: dict[str, list[str]]) -> Response:
        from app.diagnosis.engine import report_for

        ctx = get_ctx(request)
        report = report_for(ctx)
        f = report.by_id(fid)
        if f is None:
            return _go(err="Die Aufgabe besteht nicht mehr – die Daten haben sich geändert.")
        plan = A.build_plan(ctx, report, f, key, params, given=bool(params))
        if plan.errors:
            return _page(request, errors=plan.errors, chosen=aid, values=params)
        effects = A.preview(ctx, report, plan)
        return render(request, "guide_confirm.html", active="quality", title=G.TITLE, f=f, plan=plan,
                      effects=effects, aid=aid, params=plan.params)

    @router.post(f"{BASE}/rueckgaengig")
    async def undo(request: Request) -> Response:
        ctx = get_ctx(request)
        form = await request.form()
        try:
            did = int(str(form.get("d") or "0"))
        except ValueError:
            return _go(err="Ungültige Angabe.")
        row = ctx.db.q1("SELECT action, status FROM diag_decision WHERE id=?", (did,))
        if row is None or row["status"] != "active":
            return _go(err="Diese Antwort ist schon zurückgenommen.")
        fn = A.undo if row["action"] == "fix" else A.reopen
        res = await run_in_threadpool(fn, ctx, did)
        if res.errors:
            return _go(err=" ".join(res.errors))
        return _go(msg="Zurückgenommen – die Aufgabe ist wieder offen.", new=1)

    @router.get(f"{BASE}/ende")
    def finish(request: Request) -> Response:
        resp = RedirectResponse("/quality", status_code=303)
        _clear(resp)
        return resp

    return router


register_router(make_router)
