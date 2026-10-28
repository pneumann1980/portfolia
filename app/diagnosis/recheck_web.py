"""Erneut prüfen (Seite und Aktionen): vorgemerkte Buchungen, Zurücklegen in den Prüfstapel, Protokoll mit Rückholen."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from app.diagnosis import recheck as RC
from app.web.app import register_router
from app.web.deps import get_ctx, render


def _safe_back(raw: str) -> str:
    return raw if raw.startswith("/") and not raw.startswith("//") and "\n" not in raw and "\\" not in raw \
        and raw.split("?")[0].startswith(("/quality", "/journal/pruefung")) else "/quality/erneut"


def _go(back: str, **q: str) -> Response:
    sep = "&" if "?" in back else "?"
    qs = urlencode({k: v for k, v in q.items() if v})
    return Response(status_code=303, headers={"Location": back + (sep + qs if qs else "")})


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/quality/erneut", response_class=HTMLResponse)
    def page(request: Request, msg: str = "", err: str = "") -> HTMLResponse:
        ctx = get_ctx(request)
        m = RC.marks(ctx.db)
        rows: list[dict[str, Any]] = []
        for tx_id, mk in sorted(m.items(), key=lambda kv: kv[1].get("at", "")):
            i = RC.info(ctx, tx_id, m)
            if i is not None:
                rows.append({**i, "reason": mk.get("reason", ""), "since": mk.get("at", "")})
        return render(request, "recheck.html", active="quality", dq_tab="faelle", rows=rows,
                      log=list(reversed(RC.entries(ctx.db)))[:30], msg=msg, err=err)

    async def _act(request: Request, fn: Any, ok: str) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        back = _safe_back(str(f.get("back") or ""))
        res = fn(ctx, f)
        if res["errors"]:
            return _go(back, err=" ".join(res["errors"]))
        return _go(back, msg=ok)

    @router.post("/quality/erneut/vormerken")
    async def mark(request: Request) -> Response:
        return await _act(request, lambda c, f: RC.mark(c, str(f.get("tx") or ""), str(f.get("reason") or "")),
                          "Vorgemerkt – die Buchung erscheint unter „Erneut prüfen“.")

    @router.post("/quality/erneut/entfernen")
    async def unmark(request: Request) -> Response:
        return await _act(request, lambda c, f: RC.unmark(c, str(f.get("tx") or "")), "Markierung entfernt.")

    @router.post("/quality/erneut/zurueck")
    async def reopen(request: Request) -> Response:
        return await _act(request, lambda c, f: RC.reopen(c, str(f.get("tx") or "")),
                          "Zurück im Prüfstapel – die Zeile ist wieder offen.")

    @router.post("/quality/erneut/rueckgaengig")
    async def undo(request: Request) -> Response:
        def run(c: Any, f: Any) -> dict[str, Any]:
            try:
                return RC.undo(c, int(str(f.get("id") or "0")))
            except ValueError:
                return {"errors": ["Ungültiger Eintrag."]}
        return await _act(request, run, "Die Buchung zählt wieder, die Zeile im Prüfstapel ist wiederhergestellt.")

    return router


register_router(make_router)
