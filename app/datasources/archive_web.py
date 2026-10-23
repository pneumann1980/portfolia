"""Belege (``/belege``): Bankdokumente ansehen, einzeln oder gesammelt herunterladen (siehe ``dkb_archive``)."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.datasources import dkb_archive as A
from app.web.app import register_router
from app.web.deps import get_ctx, render

_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


def _sel(konto: str, jahr: int, art: str, q: str, verknuepft: str) -> dict[str, object]:
    return {"account": konto.strip()[:120], "year": jahr if 1990 <= jahr <= 2200 else 0, "kind": art.strip()[:40],
            "q": q.strip()[:80], "linked": verknuepft if verknuepft in ("ja", "nein") else ""}


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/belege", response_class=HTMLResponse)
    async def page(request: Request, konto: str = "", jahr: int = 0, art: str = "", q: str = "",
                   verknuepft: str = "", seite: int = 1, err: str = "") -> Response:
        sel = _sel(konto, jahr, art, q, verknuepft)
        data = await run_in_threadpool(lambda: A.listing(get_ctx(request), page=seite, **sel))  # type: ignore[arg-type]
        query = urlencode({k: v for k, v in {"konto": sel["account"], "jahr": sel["year"] or "", "art": sel["kind"],
                                             "q": sel["q"], "verknuepft": sel["linked"]}.items() if v})
        return render(request, "belege.html", active="journal", d=data, sel=sel, query=query, err=err[:300])

    @router.get("/belege/export")
    async def export(request: Request, konto: str = "", jahr: int = 0, art: str = "", q: str = "",
                     verknuepft: str = "") -> Response:
        sel = _sel(konto, jahr, art, q, verknuepft)
        pack = await run_in_threadpool(lambda: A.build_zip(get_ctx(request), **sel))  # type: ignore[arg-type]
        if not pack.ok:
            return Response(status_code=303, headers={"Location": "/belege?" + urlencode({"err": pack.message})})
        label = str(sel["year"] or "alle")
        return StreamingResponse(pack.chunks(), media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="belege-{label}.zip"', "Content-Length": str(pack.size),
            **_HEADERS})

    @router.get("/belege/{bid}/datei")
    async def file(request: Request, bid: int, dl: int = 0) -> Response:
        found = await run_in_threadpool(A.file_of, get_ctx(request), bid)
        if found is None:
            raise HTTPException(404, "Datei nicht gefunden – liegt sie noch im Ablageordner?")
        row, path = found
        data = await run_in_threadpool(path.read_bytes)
        is_pdf = data[:5] == b"%PDF-"
        disp = "attachment" if dl or not is_pdf else "inline"  # nur echte PDFs im Browser anzeigen
        return Response(data, media_type="application/pdf" if is_pdf else "application/octet-stream", headers={
            "Content-Disposition": f'{disp}; filename="{A.file_name(row)}"', "Content-Security-Policy":
            "frame-ancestors 'self'", **_HEADERS})

    return router


register_router(make_router)
