"""Watchlist – Weboberfläche und Job (registriert sich beim Import in main)."""

from __future__ import annotations

import logging
import threading
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from app.jobs.scheduler import Scheduler, extra_jobs
from app.prices.market import market_data
from app.util.timeutil import today_local
from app.watchlist.service import SORTS, watchlist_service
from app.web.app import register_router
from app.web.deps import get_ctx, render

log = logging.getLogger(__name__)
KINDS = {"crypto": "Krypto (CoinGecko)", "security": "Wertpapier (Yahoo-Symbol)", "asset": "aus dem Portfolio"}


def refresh_job(ctx: Any, force: bool = False) -> dict[str, Any]:
    series = watchlist_service(ctx).all_series()
    return market_data(ctx).refresh(series, force=force) if series else {"skipped": "Watchlist leer"}


@extra_jobs
def _jobs(s: Scheduler) -> None:
    # Marktkapitalisierung, 7 Tage und Sparkline morgens und abends; Kurs/24h laufen mit dem regulären Kursabruf
    s.register("watchlist_refresh", lambda ctx, force=False: refresh_job(ctx, force),
               CronTrigger(hour="7,19", minute=12))


def sparkline(values: list[float], w: int = 96, h: int = 28) -> str:
    """SVG-Polyline-Punkte (ohne JavaScript, CSP-konform)."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    step = w / (len(vals) - 1)
    return " ".join(f"{i * step:.1f},{h - 2 - (v - lo) / span * (h - 4):.1f}" for i, v in enumerate(vals))


def _back(request: Request, target: str) -> Response:
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": target})
    return Response(status_code=303, headers={"Location": target})


def _bg_refresh(ctx: Any, series: list[str]) -> bool:
    md = market_data(ctx)
    due = md.due(series)
    if not due:
        return False

    def run() -> None:
        try:
            md.refresh(due)
        except Exception as e:  # Hintergrund – Anzeige zeigt den letzten Stand
            log.info("Watchlist-Aktualisierung fehlgeschlagen: %s", e)
        finally:
            ctx.db.close_thread_conn()

    threading.Thread(target=run, name="watchlist-refresh", daemon=True).start()
    return True


def _page(request: Request, list_id: int | None = None, sort: str = "manual", errors: list[str] | None = None,
          choices: list[dict[str, Any]] | None = None, form: dict[str, str] | None = None,
          status_code: int = 200) -> HTMLResponse:
    ctx = get_ctx(request)
    svc = watchlist_service(ctx)
    lists = svc.lists()
    lid = list_id if list_id and any(int(r["id"]) == list_id for r in lists) else svc.default_id()
    sort = sort if sort in SORTS else "manual"
    items = svc.items(lid, sort)
    loading = _bg_refresh(ctx, [i.series for i in items if i.series])
    pf = ctx.portfolio()
    assets = sorted((a for a in (pf.assets.values() if pf else []) if not a.is_fiat and a.quote_id
                     and a.quote_source in ("coingecko", "yahoo")), key=lambda a: a.name.lower())
    return render(request, "watchlist.html", status_code=status_code, active="watchlist", items=items, lists=lists,
                  list_id=lid, sort=sort, sorts=SORTS, kinds=KINDS, errors=errors or [], choices=choices or [],
                  form=form or {}, loading=loading, sparkline=sparkline, assets=assets)


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/watchlist", response_class=HTMLResponse)
    def page(request: Request, list: int = 0, sort: str = "manual") -> HTMLResponse:
        return _page(request, list or None, sort)

    @router.post("/watchlist/add")
    async def add(request: Request) -> Response:
        ctx = get_ctx(request)
        svc = watchlist_service(ctx)
        f = await request.form()
        kind = str(f.get("kind") or "crypto")
        value = str(f.get("value") or "").strip()
        lid = int(str(f.get("list") or 0) or 0) or svc.default_id()
        entry, errors, choices = svc.resolve(kind if kind in KINDS else "crypto", value)
        if entry is None:
            return _page(request, lid, errors=errors, choices=choices, form={"kind": kind, "value": value},
                         status_code=400 if not choices else 200)
        item_id, errors = svc.add(lid, entry)
        if errors:
            return _page(request, lid, errors=errors, form={"kind": kind, "value": value}, status_code=400)
        await run_in_threadpool(market_data(ctx).refresh, [svc.series(svc.get(item_id))], True)  # type: ignore[arg-type,list-item]
        return _back(request, f"/watchlist?{urlencode({'list': lid})}")

    @router.post("/watchlist/lists")
    async def create_list(request: Request) -> Response:
        svc = watchlist_service(get_ctx(request))
        f = await request.form()
        lid, errors = svc.create_list(str(f.get("name") or ""))
        if errors:
            return _page(request, errors=errors, status_code=400)
        return _back(request, f"/watchlist?list={lid}")

    @router.post("/watchlist/item/{item_id}/remove")
    async def remove(request: Request, item_id: int) -> Response:
        svc = watchlist_service(get_ctx(request))
        it = svc.get(item_id)
        if it is None:
            raise HTTPException(404)
        svc.remove(item_id)
        return _back(request, f"/watchlist?list={it.list_id}")

    @router.post("/watchlist/item/{item_id}/move")
    async def move(request: Request, item_id: int) -> Response:
        svc = watchlist_service(get_ctx(request))
        f = await request.form()
        it = svc.get(item_id)
        if it is None:
            raise HTTPException(404)
        svc.move(item_id, -1 if f.get("dir") == "up" else 1)
        return _back(request, f"/watchlist?list={it.list_id}")

    @router.get("/watchlist/item/{item_id}", response_class=HTMLResponse)
    def detail(request: Request, item_id: int) -> Response:
        ctx = get_ctx(request)
        svc = watchlist_service(ctx)
        it = svc.get(item_id)
        if it is None:
            raise HTTPException(404)
        if it.asset_id and ctx.portfolio() is not None and it.asset_id in ctx.portfolio().assets:
            return Response(status_code=303, headers={"Location": f"/asset/{it.asset_id}"})
        loading = _ensure_history(ctx, it.series)
        return render(request, "watchlist_item.html", active="watchlist", it=it, loading=loading,
                      sparkline=sparkline)

    @router.get("/api/watchlist/{item_id}/chart")
    def chart(request: Request, item_id: int, range: str = "1J") -> JSONResponse:
        ctx = get_ctx(request)
        it = watchlist_service(ctx).get(item_id)
        if it is None or not it.series:
            raise HTTPException(404)
        from app.web.routes.api import _range_start

        today = today_local()
        rng = range.upper()
        start = today - timedelta(days=7) if rng in ("1T", "7T", "1W") else _range_start(rng, today, today -
                                                                                         timedelta(days=3650))
        pts = [[d.isoformat(), round(c, 8), round(n, 8)] for d, c, n, _s, _r in
               market_data(ctx).eur_series(it.series, start, today)]
        snap = it.snap
        if snap and snap.price_eur and (not pts or pts[-1][0] < today.isoformat()):
            pts.append([today.isoformat(), round(snap.price_eur, 8), None])
        return JSONResponse({"asset": it.label, "range": rng, "kind": "line", "intraday": False, "avg_cost": None,
                             "ccy": "EUR", "points": pts, "candles": [], "markers": [], "source": it.series})

    @router.post("/watchlist/item/{item_id}/position")
    async def create_position(request: Request, item_id: int) -> Response:
        """„Position erstellen“: Asset anlegen (falls neu) und in die normale Buchungserfassung (Kauf) wechseln."""
        ctx = get_ctx(request)
        svc = watchlist_service(ctx)
        it = svc.get(item_id)
        if it is None:
            raise HTTPException(404)
        aid = it.asset_id
        if not aid:
            from app.journal.service import journal_service

            js = journal_service(ctx)
            aid = (it.sym if it.asset_class == "crypto" else it.quote_id)[:40]
            known = js.known_assets()
            if aid in known and (known[aid].quote_source, known[aid].quote_id) != (it.quote_source, it.quote_id):
                aid = f"{aid}#{it.quote_id}"[:60]
            if aid not in known:
                res = await run_in_threadpool(js.save_asset, {"asset_id": aid, "name": it.label,
                                                              "asset_class": it.asset_class,
                                                              "quote_source": it.quote_source,
                                                              "quote_id": it.quote_id})
                if res.errors:
                    return _page(request, it.list_id, errors=res.errors, status_code=400)
            ctx.db.x("UPDATE watchlist_item SET asset_id=? WHERE id=?", (aid, item_id))
        return _back(request, "/journal/quick?" + urlencode({"asset": aid, "side": "buy"}))

    return router


def _ensure_history(ctx: Any, series: str | None) -> bool:
    """Für die Detailansicht ein Jahr Tageshistorie nachladen (einmalig, im Hintergrund)."""
    if not series:
        return False
    first = ctx.store.coverage(series)[0]
    if first and first <= (today_local() - timedelta(days=330)).isoformat():
        return False

    def run() -> None:
        try:
            ctx.prices._backfill_series(series, None, today_local() - timedelta(days=365), today_local(), False)
            ctx.prices._bump()
        except Exception as e:
            log.info("Watchlist: Historie %s nicht geladen: %s", series, e)
        finally:
            ctx.db.close_thread_conn()

    threading.Thread(target=run, name="watchlist-history", daemon=True).start()
    return True


register_router(make_router)
