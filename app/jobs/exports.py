"""Datierte ZIP-Sicherungen im einheitlichen Import-Format und Archiv der importierten ZIP-Dateien.

* **Nach jeder Änderung** an Buchungen (manuell, CSV-Import, Sparplan-Freigabe, neuer Import, Assets) wird –
  gebündelt, sobald ``DELAY_S`` Sekunden lang keine weitere Änderung folgt – ein Gesamtexport
  ``portfolia-export-JJJJ-MM-TT_HHMMSS.zip`` geschrieben. Er entspricht dem Datenvertrag (Schema 1.1) und kann als
  kuratierter Import dienen. Geschrieben wird nur, wenn sich der Inhalt (Buchungen, Assets, Konten, manuelle Kurse)
  seit dem letzten Export geändert hat – Einstellungen oder neue Kurse erzeugen keine Kopie.
* **Jede erfolgreich importierte ZIP-Datei** wird mit Datum ins Unterverzeichnis ``import-archiv`` kopiert
  (gleiche Dateien nur einmal).
* Aufbewahrung: ``export.keep`` Exporte und ``export.import_keep`` Import-Kopien (älteste zuerst gelöscht).

Verzeichnis: ``EXPORT_DIR`` (Standard ``/data/exports``) – für eine Kopie außerhalb des Containers z. B. auf eine
Unraid-Freigabe legen. Die Dateien enthalten die vollständigen Portfolio-Daten.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import shutil
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from app.context import AppContext
from app.jobs.scheduler import Scheduler, extra_jobs, startup_job
from app.util.timeutil import local_tz, now_local
from app.web.app import register_router
from app.web.deps import get_ctx
from app.web.routes.actions import settings_section

log = logging.getLogger(__name__)

DELAY_S = 120
EXPORT_RE = re.compile(r"^portfolia-export-(\d{4}-\d{2}-\d{2}_\d{6})\.zip$")
ARCHIVE_RE = re.compile(r"^import-(\d{4}-\d{2}-\d{2}_\d{6})-([0-9a-f]{12})-[A-Za-z0-9._-]{1,100}\.zip$")
ARCHIVE_DIR = "import-archiv"
# Inhalt, der eine neue Sicherung rechtfertigt (ohne Stichtags-Bestände, API-Verbrauch und Kurshistorie)
HASHED = ("transactions.csv", "assets.csv", "accounts.csv", "manual_prices.csv", "portfolia/state.json")


def content_hash(zip_bytes: bytes) -> str:
    """Prüfsumme über die inhaltlichen Dateien (ohne Zeitstempel im Manifest und ohne Stichtags-Bestände)."""
    h = hashlib.sha256()
    with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
        names = set(zf.namelist())
        for name in HASHED:
            if name in names:
                h.update(name.encode())
                h.update(zf.read(name))
    return h.hexdigest()


def _stamp() -> str:
    return now_local().strftime("%Y-%m-%d_%H%M%S")


def _write_atomic(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def export_now(ctx: AppContext, reason: str = "auto") -> dict[str, Any]:
    """Gesamtexport als datierte ZIP-Datei; bei ``reason="auto"`` nur, wenn sich der Inhalt geändert hat."""
    from app.journal.service import journal_service

    try:
        data = journal_service(ctx).export_zip()
    except ValueError:
        return {"skipped": "keine Buchungen"}
    digest = content_hash(data)
    if reason == "auto" and digest == ctx.db.get_state("export.last_hash"):
        return {"skipped": "unverändert"}
    d = ctx.config.export_dir
    name = f"portfolia-export-{_stamp()}.zip"
    target = d / name
    if target.exists() and digest == ctx.db.get_state("export.last_hash"):  # gleicher Stand in derselben Sekunde
        return {"file": name, "bytes": target.stat().st_size, "removed": 0}
    _write_atomic(target, data)
    ctx.db.set_state("export.last_hash", digest)
    ctx.db.set_state("export.last_file", name)
    removed = prune(d, EXPORT_RE, int(ctx.settings.get("export.keep", 30) or 30))
    log.info("ZIP-Sicherung geschrieben: %s (%d KB, %s)", name, len(data) // 1024, reason)
    return {"file": name, "bytes": len(data), "removed": removed}


def archive_import(ctx: AppContext, path: Path) -> dict[str, Any]:
    """Kopie einer importierten ZIP-Datei mit Datum (je Inhalt nur einmal)."""
    if not path.is_file():
        return {"skipped": "Datei fehlt"}
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    short = h.hexdigest()[:12]
    d = ctx.config.export_dir / ARCHIVE_DIR
    d.mkdir(parents=True, exist_ok=True)
    for p in d.iterdir():
        m = ARCHIVE_RE.match(p.name)
        if m and m.group(2) == short:
            return {"skipped": "bereits archiviert", "file": p.name}
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", path.stem)[:80] or "import"
    name = f"import-{_stamp()}-{short}-{safe}.zip"
    tmp = d / f".{name}.tmp"
    try:
        shutil.copyfile(path, tmp)
        os.replace(tmp, d / name)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    removed = prune(d, ARCHIVE_RE, int(ctx.settings.get("export.import_keep", 20) or 20))
    log.info("Import-Datei archiviert: %s", name)
    return {"file": name, "removed": removed}


def prune(d: Path, pattern: re.Pattern[str], keep: int) -> int:
    files = sorted((p for p in d.iterdir() if p.is_file() and pattern.match(p.name)),
                   key=lambda p: pattern.match(p.name).group(1), reverse=True)  # type: ignore[union-attr]
    removed = 0
    for p in files[max(keep, 1):]:
        with contextlib.suppress(FileNotFoundError):
            p.unlink()
            removed += 1
    return removed


def _list(d: Path, pattern: re.Pattern[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not d.is_dir():
        return out
    for p in d.iterdir():
        m = pattern.match(p.name)
        if m and p.is_file():
            ts = datetime.strptime(m.group(1), "%Y-%m-%d_%H%M%S").replace(tzinfo=local_tz())
            out.append({"name": p.name, "bytes": p.stat().st_size, "created": ts})
    out.sort(key=lambda x: x["created"], reverse=True)
    return out


def list_exports(ctx: AppContext) -> list[dict[str, Any]]:
    return _list(ctx.config.export_dir, EXPORT_RE)


def list_archive(ctx: AppContext) -> list[dict[str, Any]]:
    return _list(ctx.config.export_dir / ARCHIVE_DIR, ARCHIVE_RE)


def file_path(ctx: AppContext, name: str) -> Path | None:
    """Nur Dateien nach dem Namensschema (kein Pfadzugriff von außen)."""
    if EXPORT_RE.match(name):
        p = ctx.config.export_dir / name
    elif ARCHIVE_RE.match(name):
        p = ctx.config.export_dir / ARCHIVE_DIR / name
    else:
        return None
    return p if p.is_file() else None


def schedule(ctx: AppContext) -> None:
    if ctx.scheduler is not None and ctx.settings.get("export.auto", True):
        ctx.scheduler.debounce("auto_export", DELAY_S)


@extra_jobs
def _register(s: Scheduler) -> None:
    s.register("auto_export", lambda ctx: export_now(ctx, "auto"), None)
    ctx = s.ctx
    if not any(getattr(fn, "_portfolia_export", False) for fn in ctx.change_listeners):
        def on_change(kind: str) -> None:
            schedule(ctx)

        on_change._portfolia_export = True  # type: ignore[attr-defined]
        ctx.change_listeners.append(on_change)


startup_job("auto_export", 90)  # nach einem Update/Neustart einmal prüfen (unverändert → keine neue Datei)


@settings_section("export")
async def _save_settings(ctx: AppContext, f: Any) -> None:
    from app.web.routes.actions import _int

    ctx.settings.set("export.auto", f.get("auto") == "1")
    ctx.settings.set("export.keep", _int(f.get("keep"), 30, 1, 1000))
    ctx.settings.set("export.import_keep", _int(f.get("import_keep"), 20, 1, 1000))
    d = ctx.config.export_dir
    if d.is_dir():
        prune(d, EXPORT_RE, int(ctx.settings.get("export.keep", 30)))
    if (d / ARCHIVE_DIR).is_dir():
        prune(d / ARCHIVE_DIR, ARCHIVE_RE, int(ctx.settings.get("export.import_keep", 20)))


def make_router() -> APIRouter:
    router = APIRouter()

    @router.post("/actions/export", response_class=HTMLResponse)
    async def export_action(request: Request) -> HTMLResponse:
        ctx = get_ctx(request)
        try:
            res = await run_in_threadpool(export_now, ctx, "manual")
        except OSError as e:
            log.error("ZIP-Sicherung fehlgeschlagen: %s", e)
            return HTMLResponse('<span class="badge crit">Export fehlgeschlagen – Verzeichnis prüfen</span>',
                                status_code=500)
        if "file" not in res:
            return HTMLResponse(f'<span class="badge">Kein Export: {res.get("skipped", "")}</span>')
        return HTMLResponse(f'<span class="badge good">Gesichert: {res["file"]} ({res["bytes"] // 1024} KB)</span>')

    @router.post("/actions/restore/{action}")
    async def restore_action(request: Request, action: str) -> Response:
        """Zusatzdaten eines importierten Portfolia-Exports übernehmen oder die Rückfrage verwerfen."""
        from app import fullexport

        ctx = get_ctx(request)
        f = await request.form()
        st = fullexport.status(ctx)
        try:
            iid = int(str(f.get("import_id") or "0"))
        except ValueError:
            iid = 0
        if action == "resume":
            if fullexport.pending(ctx.db) is None:
                raise HTTPException(404)
        elif st is None or st["import_id"] != iid or action not in ("apply", "dismiss"):
            raise HTTPException(404)
        target = f"/settings?saved=restore_{action}#export"
        try:
            if action == "apply":
                await run_in_threadpool(fullexport.apply, ctx, iid)
            elif action == "resume":
                await run_in_threadpool(fullexport.resume, ctx)
                target = "/settings?saved=restore_apply#export"
            else:
                fullexport.dismiss(ctx, iid)
        except (fullexport.RestoreError, fullexport.RestoreIncomplete, OSError, ValueError) as e:
            from urllib.parse import quote

            log.warning("Übernahme der Zusatzdaten: %s", e)
            target = f"/settings?err={quote(str(e)[:300])}#export"
        if request.headers.get("hx-request") == "true":
            return Response(status_code=204, headers={"HX-Redirect": target})
        return Response(status_code=303, headers={"Location": target})

    @router.get("/exports/{name}")
    def download(request: Request, name: str) -> FileResponse:
        p = file_path(get_ctx(request), name)
        if p is None:
            raise HTTPException(404)
        return FileResponse(p, media_type="application/zip", filename=name,
                            headers={"Cache-Control": "private, no-store"})

    return router


register_router(make_router)
