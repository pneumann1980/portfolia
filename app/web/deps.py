"""Gemeinsame Helfer für Routen (Kontext, Rendering, globale Hinweise)."""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse

from app.context import AppContext

log = logging.getLogger(__name__)


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def urlq(v: str) -> str:
    return quote(str(v), safe="")


def asset_url(asset_id: str) -> str:
    return f"/asset/{urlq(asset_id)}"


def panel_url(asset_id: str) -> str:
    return f"/panel/asset/{urlq(asset_id)}"


def global_alerts(ctx: AppContext) -> list[dict[str, str]]:
    alerts: list[dict[str, str]] = []
    last = ctx.db.q1("SELECT id, filename, status, processed_at FROM imports ORDER BY id DESC LIMIT 1")
    active = ctx.active_import_id()
    if last is not None and last["status"] == "failed" and last["id"] != active:
        alerts.append({"level": "crit", "text": f"Import von „{last['filename']}“ wurde abgelehnt – aktiver Stand "
                                                "bleibt unverändert.", "href": "/quality#import"})
    try:
        from app.plans.service import missing_confirmed_count, pending_count

        n = pending_count(ctx.db)
        if n:
            alerts.append({"level": "warn", "text": f"{n} geschätzte Sparplan-Ausführung{'en' if n != 1 else ''} im "
                                                    "Portfolio – bitte prüfen und freigeben.", "href": "/plans"})
        m = missing_confirmed_count(ctx.db)
        if m:
            alerts.append({"level": "warn", "text": f"{m} freigegebene Sparplan-Buchung{'en' if m != 1 else ''} "
                                                    "fehlen im aktuellen Import.", "href": "/plans#confirmed"})
    except Exception as e:  # Tabelle erst nach Migration vorhanden
        log.debug("Sparplan-Hinweise nicht verfügbar: %s", e)
    job = ctx.db.q1("SELECT running, progress_json FROM job_status WHERE job='history_backfill'")
    if job is not None and job["running"]:
        p = json.loads(job["progress_json"] or "{}")
        done, total = p.get("done", 0), p.get("total", 0)
        alerts.append({"level": "info", "text": f"Historische Kurse werden geladen ({done}/{total}) – Charts "
                                                "vervollständigen sich im Hintergrund.", "href": "/quality#jobs",
                       "progress": str(int(done / total * 100)) if total else "0"})
    return alerts


def _estimated_assets(ctx: AppContext) -> set[str]:
    try:
        from app.plans.service import estimated_assets

        return estimated_assets(ctx.db)
    except Exception:
        return set()


def render(request: Request, template: str, status_code: int = 200, **kw: Any) -> HTMLResponse:
    ctx = get_ctx(request)
    tpl = request.app.state.templates
    base = {
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "alerts": kw.pop("alerts", None) if "alerts" in kw else global_alerts(ctx),
        "urlq": urlq,
        "asset_url": asset_url,
        "panel_url": panel_url,
        "has_import": ctx.active_import_id() is not None,
        "estimated_assets": _estimated_assets(ctx),
        "is_htmx": request.headers.get("hx-request") == "true",
    }
    base.update(kw)
    return tpl.TemplateResponse(request, template, base, status_code=status_code)
