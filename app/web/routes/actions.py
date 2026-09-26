"""Schreibende Aktionen (nur App-eigene Daten – niemals Portfolio-Daten, niemals Broker/Börsen)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from app.jobs import tasks
from app.settings_store import DEFAULTS
from app.util.timeutil import iso, parse_iso
from app.web.deps import get_ctx, render

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/actions/import/check", response_class=HTMLResponse)
async def import_check(request: Request) -> HTMLResponse:
    ctx = get_ctx(request)
    form = await request.form()
    force = form.get("force") == "1"
    from starlette.concurrency import run_in_threadpool

    out = await run_in_threadpool(tasks.import_check, ctx, "manual", force)
    return render(request, "partials/import_result.html", alerts=[], out=out)


MANUAL_REFRESH_COOLDOWN_S = 60


@router.post("/actions/prices/refresh", response_class=HTMLResponse)
def refresh_prices(request: Request) -> HTMLResponse:
    ctx = get_ctx(request)
    # Sperrzeit gegen Mehrfachklicks: jeder erzwungene Abruf kostet Kontingent (CoinGecko, Yahoo)
    last = parse_iso(ctx.db.get_state("last_manual_refresh"))
    now = datetime.now(UTC)
    if last is not None and (now - last).total_seconds() < MANUAL_REFRESH_COOLDOWN_S:
        wait = int(MANUAL_REFRESH_COOLDOWN_S - (now - last).total_seconds()) + 1
        return HTMLResponse(f'<span class="badge warn">Bitte {wait} s warten – Kurse werden gerade '
                            'aktualisiert.</span>')
    ctx.db.set_state("last_manual_refresh", iso(now))
    if ctx.scheduler is not None:
        ctx.scheduler.trigger("prices_crypto", 0.5, force=True)
        ctx.scheduler.trigger("prices_securities", 0.5, force=True)
        msg = "Kursaktualisierung gestartet – die Werte aktualisieren sich in wenigen Sekunden."
    else:
        tasks.refresh_prices(ctx, force=True)
        msg = "Kurse aktualisiert."
    return HTMLResponse(f'<span class="badge info">{msg}</span>')


@router.post("/actions/backfill", response_class=HTMLResponse)
async def backfill(request: Request) -> HTMLResponse:
    ctx = get_ctx(request)
    form = await request.form()
    force = form.get("force") == "1"
    if ctx.scheduler is not None:
        ctx.scheduler.trigger("history_backfill", 0.5, force=force)
    return HTMLResponse('<span class="badge info">Historie wird im Hintergrund geladen …</span>')


def _float(v: Any, default: float, lo: float, hi: float) -> float:
    try:
        f = float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, f))


def _int(v: Any, default: int, lo: int, hi: int) -> int:
    return int(_float(v, default, lo, hi))


@router.post("/settings/save")
async def save_settings(request: Request) -> Response:
    ctx = get_ctx(request)
    s = ctx.settings
    f = await request.form()
    section = f.get("section", "general")
    if section == "general":
        s.set("allocation.other_threshold_pct", _float(f.get("other_threshold_pct"), 1.0, 0.0, 20.0))
        rng = str(f.get("default_range") or "1J").upper()
        s.set("ui.default_range", rng if rng in ("1M", "3M", "6M", "YTD", "1J", "3J", "5J", "MAX") else "1J")
    elif section == "ledger":
        scope = f.get("scope")
        s.set("ledger.scope", scope if scope in ("global", "account") else "global")
        s.set("ledger.unmatched_transfers_as_flows", f.get("unmatched_flows") == "1")
        overrides = {}
        pf = ctx.portfolio()
        for acc in (pf.all_accounts() if pf else []):
            v = f.get(f"cash__{acc}")
            if v in ("yes", "no"):
                overrides[acc] = v == "yes"
        s.set("ledger.cash_overrides", overrides)
        ctx.invalidate_data()
    elif section == "prices":
        s.set("prices.stale_crypto_minutes", _int(f.get("stale_crypto_minutes"), 60, 5, 1440))
        s.set("prices.stale_security_hours", _int(f.get("stale_security_hours"), 24, 1, 240))
        s.set("prices.coingecko_monthly_limit", _int(f.get("coingecko_monthly_limit"), 10000, 100, 10_000_000))
        s.set("prices.coingecko_throttle_pct", _int(f.get("coingecko_throttle_pct"), 80, 10, 100))
        mapping = {}
        for line in str(f.get("crypto_history_fallback") or "").splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                if k.strip() and v.strip():
                    mapping[k.strip().upper()] = v.strip()
        s.set("prices.crypto_history_fallback", mapping)
        benches = []
        for i, line in enumerate(str(f.get("benchmarks") or "").splitlines()):
            if "|" in line:
                name, _, series = line.partition("|")
                series = series.strip()
                if series and ":" in series:
                    benches.append({"id": f"b{i}", "name": name.strip() or series, "series": series})
        s.set("performance.benchmarks", benches or DEFAULTS["performance.benchmarks"])
        if ctx.prices.cg is not None:
            ctx.prices.cg.monthly_limit = int(s.get("prices.coingecko_monthly_limit", 10000))
        if ctx.scheduler is not None:
            ctx.scheduler.trigger("history_backfill", 1)
    elif section == "news":
        s.set("news.min_relevance", _float(f.get("min_relevance"), 0.2, 0.0, 5.0))
        s.set("news.dashboard_count", _int(f.get("dashboard_count"), 5, 1, 20))
        s.set("llm.enabled", f.get("llm_enabled") == "1")
        s.set("llm.daily_token_budget", _int(f.get("llm_budget"), 60000, 0, 5_000_000))
        model = str(f.get("llm_model") or "").strip()
        if model:
            s.set("llm.model", model[:80])
    elif section == "backup":
        s.set("backup.keep", _int(f.get("keep"), 14, 1, 365))
        s.set("backup.hour", _int(f.get("hour"), 3, 0, 23))
        try:
            from app.jobs.maintenance import prune_backups, reschedule_backup

            reschedule_backup(ctx)
            prune_backups(ctx, int(s.get("backup.keep", 14)))
        except ImportError:  # pragma: no cover
            pass
    else:
        handler = _EXTRA_SECTIONS.get(str(section))
        if handler is not None:
            await handler(ctx, f)
    log.info("Einstellungen gespeichert (%s)", section)
    target = f"/settings?saved={section}#{section}"
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": target})
    return Response(status_code=303, headers={"Location": target})


_EXTRA_SECTIONS: dict[str, Any] = {}


def settings_section(name: str) -> Any:
    def deco(fn: Any) -> Any:
        _EXTRA_SECTIONS[name] = fn
        return fn

    return deco
