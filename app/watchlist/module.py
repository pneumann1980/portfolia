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
from app.watchlist.service import watchlist_service
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


def _page(request: Request, list_id: int | None = None, sort: str = "standard", errors: list[str] | None = None,
          choices: list[dict[str, Any]] | None = None, form: dict[str, str] | None = None,
          status_code: int = 200, msg: str = "", err: str = "", only: str = "", job: int = 0) -> HTMLResponse:
    """Watchlist als eine Tabelle (0.115): Listentitel, beobachtete Unternehmen ohne Liste und offene Vorschläge;
    darunter eingeklappt Suche, KI-Status, Vorwärtstest, Discovery-Einstellungen und Listenverwaltung."""
    from app.discovery import engine as EN
    from app.discovery import forward as FW
    from app.discovery import jobs as J
    from app.discovery import service as SV
    from app.discovery import universe as U
    from app.discovery import watch as W
    from app.discovery.web import START_KINDS, _cfg, _recover_once
    from app.watchlist import view as V

    ctx = get_ctx(request)
    _recover_once(ctx)
    svc = watchlist_service(ctx)
    lists = svc.lists()
    lid = list_id if list_id and any(int(r["id"]) == list_id for r in lists) else None
    only = only if only == "vorschlaege" and lid is None else ""
    sort = "standard" if sort == "manual" else sort  # frühere Adresse ?sort=manual
    sort = sort if sort in V.SORTS else "standard"
    rows, counts = V.rows(ctx, svc, lists, lid, only, sort)
    loading = _bg_refresh(ctx, [r.item.series for r in rows if r.item is not None and r.item.series])
    q = {k: v for k, v in (("list", lid), ("nur", only), ("sort", sort if sort != "standard" else "")) if v}
    here = "/watchlist" + (("?" + urlencode(q)) if q else "")
    return render(request, "watchlist.html", status_code=status_code, active="watchlist", rows=rows, lists=lists,
                  counts=counts, list_id=lid, only=only, sort=sort, sorts=V.SORTS, markers=V.MARKERS, here=here,
                  default_list=svc.default_id(), kinds=KINDS, choices=choices or [],
                  errors=(errors or []) + ([err] if err else []), msg=msg, form=form or {}, loading=loading,
                  sparkline=sparkline, watch_count=W.count_watched(ctx.db), focus_job=job, stages=SV.STAGES,
                  list_counts={int(r["list_id"]): int(r["n"]) for r in ctx.db.q(
                      "SELECT list_id, COUNT(*) AS n FROM watchlist_item GROUP BY list_id")},
                  # Discovery-Bereiche unter der Tabelle (gleiche Bausteine wie die Kartenansicht)
                  st=EN.ai_status(ctx), prog=EN.screening_progress(ctx), jobs=J.recent(ctx.db, 8), cfg=_cfg(ctx),
                  fw=FW.evaluate(ctx), start_kinds=START_KINDS, sched_labels=EN.SCHEDULE_LABELS,
                  sizes=U.SIZE_LABELS, disc_back="/watchlist")


def _detail(request: Request, template: str, item_id: int = 0, company_id: int = 0, **extra: Any) -> Response:
    """Detailfenster (Seitenfenster bzw. ganze Seite) für einen Listentitel oder ein Discovery-Unternehmen."""
    from app.watchlist import view as V

    ctx = get_ctx(request)
    svc = watchlist_service(ctx)
    it = svc.get(item_id) if item_id else None
    if item_id and it is None:
        raise HTTPException(404)
    d = V.detail(ctx, svc, it, company_id or None)
    if d is None:
        raise HTTPException(404)
    loading = _ensure_history(ctx, d["series"])
    return render(request, template, active="watchlist", loading=loading, sparkline=sparkline, **d, **extra)


def _chart(ctx: Any, series: str, label: str, range: str, snap: Any) -> JSONResponse:
    from app.web.routes.api import _range_start

    today = today_local()
    rng = range.upper()
    start = today - timedelta(days=7) if rng in ("1T", "7T", "1W") else _range_start(rng, today, today -
                                                                                     timedelta(days=3650))
    pts = [[d.isoformat(), round(c, 8), round(n, 8)] for d, c, n, _s, _r in
           market_data(ctx).eur_series(series, start, today)]
    if snap and snap.price_eur and (not pts or pts[-1][0] < today.isoformat()):
        pts.append([today.isoformat(), round(snap.price_eur, 8), None])
    return JSONResponse({"asset": label, "range": rng, "kind": "line", "intraday": False, "avg_cost": None,
                         "ccy": "EUR", "points": pts, "candles": [], "markers": [], "source": series})


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/watchlist", response_class=HTMLResponse)
    def page(request: Request, list: int = 0, sort: str = "standard", nur: str = "", msg: str = "", err: str = "",
             job: int = 0) -> HTMLResponse:
        return _page(request, list or None, sort, msg=msg[:300], err=err[:300], only=nur, job=job)

    @router.post("/watchlist/add")
    async def add(request: Request) -> Response:
        ctx = get_ctx(request)
        svc = watchlist_service(ctx)
        f = await request.form()
        kind = str(f.get("kind") or "crypto")
        value = str(f.get("value") or "").strip()
        lid = int(str(f.get("list") or 0) or 0) or svc.default_id()
        entry, errors, choices = svc.resolve_any(value) if kind not in KINDS else svc.resolve(kind, value)
        if entry is None:
            return _page(request, lid, errors=errors, choices=choices, form={"kind": kind, "value": value},
                         status_code=400 if not choices else 200)
        label = " ".join(str(f.get("label") or "").split())[:80]
        if label and not entry.get("name"):  # Name aus dem gewählten Vorschlag (Yahoo liefert sonst nur das Symbol)
            entry["name"] = label
        item_id, errors = svc.add(lid, entry)
        if errors:
            return _page(request, lid, errors=errors, form={"kind": kind, "value": value}, status_code=400)
        await run_in_threadpool(market_data(ctx).refresh, [svc.series(svc.get(item_id))], True)  # type: ignore[arg-type,list-item]
        return _back(request, f"/watchlist?{urlencode({'list': lid})}")

    @router.get("/api/watchlist/suggest")
    async def suggest(request: Request, q: str = "") -> JSONResponse:
        """Vorschläge zu Name oder Symbol (0.115) – der Suchtext geht für Wertpapiere an die Yahoo-Suche."""
        from app.watchlist.suggest import suggest as find

        return JSONResponse(await run_in_threadpool(find, get_ctx(request), q))

    @router.post("/watchlist/lists")
    async def create_list(request: Request) -> Response:
        svc = watchlist_service(get_ctx(request))
        f = await request.form()
        lid, errors = svc.create_list(str(f.get("name") or ""))
        if errors:
            return _page(request, errors=errors, status_code=400)
        return _back(request, f"/watchlist?list={lid}")

    @router.post("/watchlist/lists/{list_id}/{action}")
    async def manage_list(request: Request, list_id: int, action: str) -> Response:
        """Listenverwaltung (0.114): umbenennen, als Standard setzen, löschen."""
        ctx = get_ctx(request)
        svc = watchlist_service(ctx)
        f = await request.form()
        target = list_id
        if action == "rename":
            errors, done = svc.rename_list(list_id, str(f.get("name") or "")), "Liste umbenannt."
        elif action == "default":
            errors, done = svc.set_default(list_id), "Standardliste geändert."
        elif action == "delete":
            gone, errors = svc.delete_list(list_id)
            if not errors:
                from app.discovery import watch as W

                for source, qid in gone:  # letzte Liste eines Unternehmens → alter Status „beobachten“ endet
                    W.after_item_removed(ctx, source, qid)
                target = svc.default_id()
            done = f"Liste gelöscht ({len(gone)} Einträge entfernt)." if not errors else ""
        else:
            raise HTTPException(404)
        q = {"list": target, "err": "; ".join(errors)} if errors else {"list": target, "msg": done}
        return _back(request, f"/watchlist?{urlencode(q)}#listen")

    @router.post("/watchlist/item/{item_id}/remove")
    async def remove(request: Request, item_id: int) -> Response:
        ctx = get_ctx(request)
        svc = watchlist_service(ctx)
        it = svc.get(item_id)
        if it is None:
            raise HTTPException(404)
        svc.remove(item_id)
        from app.discovery import watch as W

        W.after_item_removed(ctx, it.quote_source, it.quote_id)  # letzte Liste → alter Status „beobachten“ endet
        back = str((await request.form()).get("back") or "")
        ok = (back.startswith("/watchlist") or back.startswith("/discovery")) and "//" not in back and "\\" not in back
        return _back(request, back if ok else f"/watchlist?list={it.list_id}")

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
        it = watchlist_service(ctx).get(item_id)
        if it is None:
            raise HTTPException(404)
        if it.asset_id and ctx.portfolio() is not None and it.asset_id in ctx.portfolio().assets:
            return Response(status_code=303, headers={"Location": f"/asset/{it.asset_id}"})
        return _detail(request, "watchlist_item.html", item_id=item_id)

    @router.get("/panel/watchlist/item/{item_id}", response_class=HTMLResponse)
    def detail_panel(request: Request, item_id: int) -> Response:
        """Seitenfenster eines Listentitels – gleiche Art von Fenster wie bei den Positionen (0.115)."""
        return _detail(request, "partials/watch_detail.html", item_id=item_id, in_panel=True, alerts=[])

    @router.get("/panel/watchlist/c/{cid}", response_class=HTMLResponse)
    def company_panel(request: Request, cid: int) -> Response:
        """Seitenfenster eines Discovery-Unternehmens ohne Listeneintrag (Vorschlag bzw. „ohne Liste“)."""
        return _detail(request, "partials/watch_detail.html", company_id=cid, in_panel=True, alerts=[])

    @router.get("/api/watchlist/{item_id}/chart")
    def chart(request: Request, item_id: int, range: str = "1J") -> JSONResponse:
        ctx = get_ctx(request)
        it = watchlist_service(ctx).get(item_id)
        if it is None or not it.series:
            raise HTTPException(404)
        return _chart(ctx, it.series, it.label, range, it.snap)

    @router.get("/api/watchlist/c/{cid}/chart")
    def company_chart(request: Request, cid: int, range: str = "1J") -> JSONResponse:
        from app.watchlist import view as V

        ctx = get_ctx(request)
        d = V.detail(ctx, watchlist_service(ctx), None, cid)
        if d is None or not d["series"]:
            raise HTTPException(404)
        return _chart(ctx, d["series"], d["name"], range, d["snap"])

    @router.post("/watchlist/item/{item_id}/position")
    async def create_position(request: Request, item_id: int) -> Response:
        """„Position erstellen“: Asset anlegen (falls neu) und in die normale Buchungserfassung (Kauf) wechseln."""

        def work() -> Response:
            ctx = get_ctx(request)
            svc = watchlist_service(ctx)
            it = svc.get(item_id)
            if it is None:
                raise HTTPException(404)
            if it.is_index:
                msg = (f"{it.sym} ist ein Index – nur beobachten, nicht kaufbar (dafür "
                       "einen ETF auf den Index hinzufügen).")
                return _page(request, it.list_id, errors=[msg], status_code=400)
            aid = it.asset_id
            if not aid:
                from app.journal.service import journal_service

                js = journal_service(ctx)
                aid = (it.sym if it.asset_class == "crypto" else it.quote_id)[:40]
                known = js.known_assets()
                if aid in known and (known[aid].quote_source, known[aid].quote_id) != (it.quote_source, it.quote_id):
                    aid = f"{aid}#{it.quote_id}"[:60]
                if aid not in known:
                    res = js.save_asset({"asset_id": aid, "name": it.label, "asset_class": it.asset_class,
                                         "quote_source": it.quote_source, "quote_id": it.quote_id})
                    if res.errors:
                        return _page(request, it.list_id, errors=res.errors, status_code=400)
                ctx.db.x("UPDATE watchlist_item SET asset_id=? WHERE id=?", (aid, item_id))
            return _back(request, "/journal/quick?" + urlencode({"asset": aid, "side": "buy"}))

        return await run_in_threadpool(work)

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
