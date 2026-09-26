"""Wartung: tägliche Sicherung der App-Datenbank, Aufbewahrung, DB-Pflege (registriert sich in main).

Sicherung über die SQLite-Online-Backup-API (konsistent auch bei laufendem Betrieb), Integritätsprüfung der
Kopie, gzip-Kompression, atomares Umbenennen. Portfolio-Daten stammen aus dem Import und sind darin nur als
Importstände enthalten – maßgeblich bleibt die Import-Datei.
"""

from __future__ import annotations

import contextlib
import gzip
import logging
import os
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from app.context import AppContext
from app.jobs.scheduler import Scheduler, extra_jobs
from app.util.timeutil import local_tz, now_local
from app.web.app import register_router
from app.web.deps import get_ctx

log = logging.getLogger(__name__)
BACKUP_RE = re.compile(r"^app-(\d{8}-\d{6})\.sqlite\.gz$")


def backup_now(ctx: AppContext, reason: str = "daily") -> dict[str, Any]:
    d = ctx.config.backup_dir
    d.mkdir(parents=True, exist_ok=True)
    stamp = now_local().strftime("%Y%m%d-%H%M%S")
    raw = d / f".app-{stamp}.sqlite.tmp"
    gz_tmp = d / f".app-{stamp}.sqlite.gz.tmp"
    target = d / f"app-{stamp}.sqlite.gz"
    try:
        ctx.db.backup_to(raw)
        con = sqlite3.connect(raw)
        try:
            ok = con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            con.close()
        if not ok:
            raise RuntimeError("Integritätsprüfung der Sicherungskopie fehlgeschlagen")
        with raw.open("rb") as src, gzip.open(gz_tmp, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        os.replace(gz_tmp, target)
    finally:
        for p in (raw, gz_tmp):
            with contextlib.suppress(FileNotFoundError):
                p.unlink()
    removed = prune_backups(ctx, int(ctx.settings.get("backup.keep", 14) or 14))
    size = target.stat().st_size
    log.info("Sicherung erstellt: %s (%d KB, %s)", target.name, size // 1024, reason)
    return {"file": target.name, "bytes": size, "removed": removed}


def list_backups(ctx: AppContext) -> list[dict[str, Any]]:
    d = ctx.config.backup_dir
    out = []
    if not d.is_dir():
        return out
    for p in d.iterdir():
        m = BACKUP_RE.match(p.name)
        if m and p.is_file():
            ts = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=local_tz())
            out.append({"name": p.name, "bytes": p.stat().st_size, "created": ts})
    out.sort(key=lambda b: b["name"], reverse=True)
    return out


def prune_backups(ctx: AppContext, keep: int) -> int:
    removed = 0
    for b in list_backups(ctx)[max(keep, 1):]:
        with contextlib.suppress(FileNotFoundError):
            (ctx.config.backup_dir / b["name"]).unlink()
            removed += 1
    return removed


def db_maintenance(ctx: AppContext) -> dict[str, Any]:
    """Statistiken aktualisieren, WAL zurücksetzen, bei viel freiem Platz VACUUM."""
    db = ctx.db
    db.x("PRAGMA optimize")
    page_count = int(db.scalar("PRAGMA page_count", default=0))
    free = int(db.scalar("PRAGMA freelist_count", default=0))
    vacuumed = False
    if page_count and free / page_count > 0.25 and free > 2000:
        db.x("VACUUM")
        vacuumed = True
    with contextlib.suppress(sqlite3.DatabaseError):
        db.x("PRAGMA wal_checkpoint(TRUNCATE)")
    return {"pages": page_count, "free": free, "vacuumed": vacuumed}


def _backup_trigger(ctx: AppContext) -> CronTrigger:
    hour = int(ctx.settings.get("backup.hour", 3) or 3)
    return CronTrigger(hour=max(0, min(23, hour)), minute=15)


@extra_jobs
def _register(s: Scheduler) -> None:
    s.register("backup", lambda ctx: backup_now(ctx), _backup_trigger(s.ctx))
    s.register("db_maintenance", db_maintenance, CronTrigger(day_of_week="sun", hour=4, minute=40))


def reschedule_backup(ctx: AppContext) -> None:
    if ctx.scheduler is not None:
        ctx.scheduler.register("backup", lambda c: backup_now(c), _backup_trigger(ctx))


def make_router() -> APIRouter:
    router = APIRouter()

    @router.post("/actions/backup", response_class=HTMLResponse)
    async def backup_action(request: Request) -> HTMLResponse:
        ctx = get_ctx(request)
        try:
            res = await run_in_threadpool(backup_now, ctx, "manual")
        except Exception as e:
            log.error("Sicherung fehlgeschlagen: %s", e)
            return HTMLResponse('<span class="badge crit">Sicherung fehlgeschlagen – siehe Datenqualität</span>',
                                status_code=500)
        return HTMLResponse(f'<span class="badge good">Gesichert: {res["file"]} ({res["bytes"] // 1024} KB)</span>')

    return router


register_router(make_router)


def backup_path(ctx: AppContext, name: str) -> Path | None:
    """Nur Dateien, die dem Sicherungsschema entsprechen (kein Pfadzugriff von außen)."""
    if not BACKUP_RE.match(name):
        return None
    p = ctx.config.backup_dir / name
    return p if p.is_file() else None
