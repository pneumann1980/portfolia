"""Seite „Steuerbescheinigungen“ (registriert sich beim Import in main)."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.taxcert import parse as P
from app.taxcert.service import MAX_SIZE, taxcert_service
from app.web.app import register_router
from app.web.deps import get_ctx, render

log = logging.getLogger(__name__)
MAX_FILES = 6


def _back(request: Request, target: str) -> Response:
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": target})
    return Response(status_code=303, headers={"Location": target})


def _page(request: Request, results: list[dict[str, Any]] | None = None, errors: list[str] | None = None,
          status_code: int = 200) -> HTMLResponse:
    svc = taxcert_service(get_ctx(request))
    certs = svc.for_tax()
    rows = []
    for y in sorted(certs, reverse=True):
        e = certs[y]
        b, t = e.get(P.KIND_BESCHEINIGUNG), e.get(P.KIND_ERTRAEGNIS)
        rows.append({"year": y, "b": b, "e": t, "checks": svc.checks(y),
                     "vals": {k: Decimal(v) for k, v in ((b or {}).get("values") or {}).items()},
                     "totals": {k: Decimal(v) for k, v in ((t or {}).get("totals") or {}).items()},
                     "carry_in": {k: Decimal(v) for k, v in ((t or {}).get("carry_in") or {}).items()},
                     "carry_out": {k: Decimal(v) for k, v in ((t or {}).get("carry_out") or {}).items()}})
    return render(request, "tax_certs.html", status_code=status_code, active="tax", rows=rows, results=results or [],
                  errors=errors or [], max_mb=MAX_SIZE // 1024 // 1024, kinds=P.KIND_LABEL, max_files=MAX_FILES)


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/tax/certs", response_class=HTMLResponse)
    def overview(request: Request) -> HTMLResponse:
        return _page(request)

    @router.post("/tax/certs/upload", response_class=HTMLResponse)
    async def upload(request: Request) -> HTMLResponse:
        ctx = get_ctx(request)
        form = await request.form(max_files=MAX_FILES, max_fields=10)
        files = [f for f in form.getlist("file") if isinstance(f, UploadFile) and f.filename]
        if not files:
            return _page(request, errors=["Bitte mindestens eine PDF-Datei auswählen."], status_code=400)
        svc = taxcert_service(ctx)
        results = []
        for f in files:
            data = await f.read(MAX_SIZE + 1)
            await f.close()
            results.append(await run_in_threadpool(svc.add, data, f.filename or "beleg.pdf"))
        return _page(request, results=results)

    @router.get("/tax/certs/{year}/{kind}.pdf")
    def original(request: Request, year: int, kind: str) -> FileResponse:
        found = taxcert_service(get_ctx(request)).file_path(year, kind)
        if found is None:
            raise HTTPException(404)
        path, name = found
        return FileResponse(path, media_type="application/pdf", filename=name, content_disposition_type="inline",
                            headers={"Cache-Control": "private, no-store"})

    @router.post("/tax/certs/{year}/{kind}/delete")
    def delete(request: Request, year: int, kind: str) -> Response:
        if kind not in P.KIND_LABEL:
            raise HTTPException(404)
        taxcert_service(get_ctx(request)).remove(year, kind)
        return _back(request, "/tax/certs")

    return router


register_router(make_router)
