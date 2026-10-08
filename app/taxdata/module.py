"""Steuerdaten je Jahr – Weboberfläche und Ordnerprüfung (registriert sich beim Import in main).

Routen: Übersicht ``/tax/data`` (Jahre, wartende Dateien, Upload, „Steuerdateien prüfen“), Detail einer Datei
(Vorschau, Jahr wählen, Gegenüberstellung vor dem Ersetzen, Datensätze, Export, Entfernen nach Bestätigung).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from apscheduler.triggers.interval import IntervalTrigger
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.jobs.scheduler import Scheduler, extra_jobs, startup_job
from app.taxdata.service import MAX_SIZE, tax_import_service
from app.web.app import register_router
from app.web.deps import get_ctx, render

log = logging.getLogger(__name__)
STATUS = {"pending": ("warn", "wartet auf Bestätigung"), "active": ("good", "aktiv"), "replaced": ("", "ersetzt"),
          "removed": ("", "entfernt"), "rejected": ("crit", "abgelehnt")}
MATCH = {"matched": "zugeordnet", "unmatched": "nicht zugeordnet", "conflict": "Konflikt"}
ORIGIN = {"folder": "Ordner", "upload": "Upload"}
LIMIT = 300


def scan_job(ctx: Any, manual: bool = False) -> dict[str, Any]:
    if not manual and not ctx.settings.get("taxdata.auto_scan", True):
        return {"skipped": "automatische Prüfung aus"}
    return tax_import_service(ctx).scan()


@extra_jobs
def _jobs(s: Scheduler) -> None:
    # kein Dateiwächter: Prüfung beim Start, alle 15 Minuten (abschaltbar) und auf Knopfdruck
    s.register("taxdata_scan", lambda ctx, manual=False: scan_job(ctx, manual), IntervalTrigger(minutes=15))


startup_job("taxdata_scan", 20)


def tax_alerts(ctx: Any) -> list[dict[str, str]]:
    """Globale Hinweise: neu erkannte Steuerdateien (bis zum Ansehen) und Dateien, die auf Bestätigung warten."""
    out = []
    # wartende Dateien bis zur Entscheidung, übernommene Ordnerdateien bis zum ersten Ansehen
    for f in ctx.db.q("SELECT id, tax_year, filename, status FROM tax_file WHERE origin='folder' AND "
                      "(status='pending' OR (status='active' AND seen_at IS NULL)) ORDER BY id LIMIT 3"):
        year = f["tax_year"] or "unbekannt"
        tail = "bitte prüfen und bestätigen." if f["status"] == "pending" else "übernommen."
        out.append({"level": "warn" if f["status"] == "pending" else "info",
                    "text": f"Neue Steuerdatei erkannt: Steuerjahr {year} („{f['filename']}“) – {tail}",
                    "href": f"/tax/data/file/{f['id']}"})
    return out


def _back(request: Request, target: str) -> Response:
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": target})
    return Response(status_code=303, headers={"Location": target})


def _j(v: Any, default: Any) -> Any:
    try:
        return json.loads(v) if v else default
    except ValueError:
        return default


def _overview(request: Request, errors: list[str] | None = None, scan: dict[str, Any] | None = None,
              status_code: int = 200) -> HTMLResponse:
    ctx = get_ctx(request)
    svc = tax_import_service(ctx)
    return render(request, "tax_data.html", status_code=status_code, active="tax", years=svc.years(),
                  pending=svc.pending(), errors=errors or [], scan=scan, status=STATUS, origin=ORIGIN,
                  folder=str(svc.dir), max_mb=MAX_SIZE // 1024 // 1024,
                  auto_scan=ctx.settings.get("taxdata.auto_scan", True))


def _detail(request: Request, fid: int, errors: list[str] | None = None, confirm: str = "", match: str = "",
            status_code: int = 200) -> HTMLResponse:
    ctx = get_ctx(request)
    svc = tax_import_service(ctx)
    f = svc.file(fid)
    if f is None:
        raise HTTPException(404)
    if f["seen_at"] is None and f["status"] in ("active", "pending"):
        from datetime import UTC, datetime

        from app.util.timeutil import iso

        ctx.db.x("UPDATE tax_file SET seen_at=? WHERE id=?", (iso(datetime.now(UTC)), fid))
    match = match if match in MATCH else ""
    recs = svc.records(fid, match)
    years = _j(f["years_json"], {})
    active = svc.active(int(f["tax_year"])) if f["tax_year"] is not None else None
    history = ctx.db.q("SELECT * FROM tax_file WHERE tax_year IS ? AND id<>? ORDER BY id DESC",
                       (f["tax_year"], fid))
    return render(request, "tax_data_file.html", status_code=status_code, active="tax", f=f,
                  records=recs[:LIMIT], total=len(recs), limit=LIMIT, match=match, matches=MATCH,
                  warnings=_j(f["warnings_json"], []), file_errors=_j(f["errors_json"], []),
                  candidates=years.get("candidates") or [], year_source=years.get("source"),
                  existing=active if active is not None and int(active["id"]) != fid else None,
                  cmp=svc.compare(fid) if f["status"] == "pending" else None, history=history,
                  errors=errors or [], confirm=confirm, status=STATUS, origin=ORIGIN)


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/tax/data", response_class=HTMLResponse)
    def overview(request: Request) -> HTMLResponse:
        return _overview(request)

    @router.post("/tax/data/scan", response_class=HTMLResponse)
    async def scan(request: Request) -> HTMLResponse:
        res = await run_in_threadpool(scan_job, get_ctx(request), True)
        return _overview(request, scan=res)

    @router.post("/tax/data/upload")
    async def upload(request: Request) -> Response:
        ctx = get_ctx(request)
        f = await request.form(max_files=1, max_fields=10)
        file = f.get("file")
        if not isinstance(file, UploadFile) or not file.filename:
            return _overview(request, ["Bitte eine Steuerdatei (JSON oder CSV) auswählen."], status_code=400)
        data = await file.read(MAX_SIZE + 1)
        await file.close()
        if len(data) > MAX_SIZE:
            return _overview(request, [f"Datei zu groß (höchstens {MAX_SIZE // 1024 // 1024} MB)."], status_code=413)
        fid, a = await run_in_threadpool(tax_import_service(ctx).register, data, file.filename, "upload")
        if fid is None:
            return _overview(request, a.errors or ["Datei nicht lesbar."], status_code=400)
        return _back(request, f"/tax/data/file/{fid}")

    @router.get("/tax/data/file/{fid}", response_class=HTMLResponse)
    def detail(request: Request, fid: int, confirm: str = "", match: str = "") -> HTMLResponse:
        return _detail(request, fid, confirm=confirm if confirm in ("remove", "replace") else "", match=match)

    @router.post("/tax/data/file/{fid}/year")
    async def set_year(request: Request, fid: int) -> Response:
        f = await request.form()
        try:
            year = int(str(f.get("year") or "0"))
        except ValueError:
            year = 0
        errors = tax_import_service(get_ctx(request)).set_year(fid, year)
        if errors:
            return _detail(request, fid, errors, status_code=400)
        return _back(request, f"/tax/data/file/{fid}")

    @router.post("/tax/data/file/{fid}/activate")
    async def activate(request: Request, fid: int) -> Response:
        f = await request.form()
        res = await run_in_threadpool(tax_import_service(get_ctx(request)).activate, fid, f.get("replace") == "1")
        if res.get("errors"):
            return _detail(request, fid, res["errors"], status_code=400)
        if res.get("needs_confirm"):
            return _back(request, f"/tax/data/file/{fid}?confirm=replace")
        if res.get("unchanged"):
            return _back(request, f"/tax/data/file/{res['id']}")
        return _back(request, f"/tax/data/file/{fid}")

    @router.post("/tax/data/file/{fid}/discard")
    async def discard(request: Request, fid: int) -> Response:
        tax_import_service(get_ctx(request)).discard(fid)
        return _back(request, "/tax/data")

    @router.post("/tax/data/file/{fid}/remove")
    async def remove(request: Request, fid: int) -> Response:
        f = await request.form()
        if f.get("confirm") != "1":
            return _back(request, f"/tax/data/file/{fid}?confirm=remove")
        tax_import_service(get_ctx(request)).remove(fid)
        return _back(request, "/tax/data")

    @router.post("/tax/data/file/{fid}/match")
    async def rematch(request: Request, fid: int) -> Response:
        svc = tax_import_service(get_ctx(request))
        if svc.file(fid) is None:
            raise HTTPException(404)
        await run_in_threadpool(svc.match, fid)
        return _back(request, f"/tax/data/file/{fid}")

    @router.get("/tax/data/file/{fid}/export.csv")
    def export(request: Request, fid: int) -> Response:
        svc = tax_import_service(get_ctx(request))
        f = svc.file(fid)
        if f is None:
            raise HTTPException(404)
        name = f"steuerdaten-{f['tax_year'] or 'unbekannt'}-{fid}.csv"
        return Response(svc.export_csv(fid).encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                         headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @router.post("/tax/data/settings")
    async def settings(request: Request) -> Response:
        f = await request.form()
        get_ctx(request).settings.set("taxdata.auto_scan", f.get("auto_scan") == "1")
        return _back(request, "/tax/data")

    return router


register_router(make_router)
