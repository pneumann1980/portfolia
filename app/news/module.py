"""News & Videos: Routen, Hintergrundjobs und Bild-Proxy (registriert sich beim Import in main)."""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from app.context import AppContext
from app.jobs.scheduler import Scheduler, extra_jobs, startup_job
from app.news.llm import LlmService
from app.news.relevance import recency_factor
from app.news.service import NewsService
from app.util.timeutil import iso, parse_iso
from app.web.app import register_router
from app.web.deps import get_ctx, render

log = logging.getLogger(__name__)
PAGE_SIZE = 30
MAX_IMG_BYTES = 2 * 1024 * 1024
_services: dict[int, NewsService] = {}


def news_service(ctx: AppContext) -> NewsService:
    svc = _services.get(id(ctx))
    if svc is None:
        svc = NewsService(ctx)
        _services[id(ctx)] = svc
    return svc


# ----------------------------------------------------------------------------------------------------
# Jobs
# ----------------------------------------------------------------------------------------------------

def job_fetch(ctx: AppContext) -> dict[str, Any]:
    res = news_service(ctx).run()
    llm = LlmService(ctx)
    if llm.enabled():
        res["llm"] = llm.summarize()
    return res


def job_rematch(ctx: AppContext) -> dict[str, Any]:
    return news_service(ctx).rematch()


def job_discovery(ctx: AppContext) -> dict[str, Any]:
    return news_service(ctx).discover()


def job_digest(ctx: AppContext) -> dict[str, Any]:
    return LlmService(ctx).digest()


@extra_jobs
def _register_jobs(s: Scheduler) -> None:
    svc = news_service(s.ctx)
    minutes = int(svc.cfg.settings.get("fetch_interval_minutes", 30) or 30)
    s.register("news_fetch", job_fetch, IntervalTrigger(minutes=max(10, minutes)))
    s.register("news_rematch", job_rematch, None)
    s.register("youtube_discovery", job_discovery, CronTrigger(hour=7, minute=5))
    s.register("llm_digest", job_digest, CronTrigger(hour=7, minute=40))


startup_job("news_fetch", 45)


# ----------------------------------------------------------------------------------------------------
# Abfragen für die Oberfläche
# ----------------------------------------------------------------------------------------------------

def query_items(ctx: AppContext, *, kind: str, asset: str = "", source: str = "", days: int = 7,
                status: str = "all", sort: str = "relevance", shorts: bool = False, hidden: bool = False,
                offset: int = 0, limit: int = PAGE_SIZE) -> tuple[list[dict[str, Any]], int]:
    since = iso(datetime.now(UTC) - timedelta(days=days))
    where = ["n.kind=?", "n.published_at>=?"]
    params: list[Any] = [kind, since]
    if asset:
        where.append("n.id IN (SELECT item_id FROM news_asset WHERE asset_id=?)")
        params.append(asset)
    if source:
        where.append("n.source_id=?")
        params.append(source)
    if status == "unread":
        where.append("n.is_read=0")
    elif status == "read":
        where.append("n.is_read=1")
    if not hidden:
        where.append("n.hidden_reason IS NULL")
    if kind == "video" and not shorts:
        where.append("COALESCE(n.is_short, 0)=0")
    min_rel = float(ctx.settings.get("news.min_relevance", 0.2))
    if not asset and not source and sort == "relevance":
        where.append("(n.relevance>=? OR n.kind='video')")
        params.append(min_rel * 0.5)
    sql = f"SELECT n.* FROM news_item n WHERE {' AND '.join(where)} ORDER BY n.published_at DESC LIMIT 1500"
    rows = ctx.db.q(sql, params)
    now = datetime.now(UTC)
    items = []
    for r in rows:
        pub = parse_iso(r["published_at"]) or now
        score = float(r["relevance"]) * recency_factor(pub, now)
        items.append({"row": r, "score": score, "published": pub})
    if sort == "relevance":
        items.sort(key=lambda x: -x["score"])
    total = len(items)
    page = items[offset:offset + limit]
    ids = [x["row"]["id"] for x in page]
    assets_by_item: dict[int, list[str]] = {}
    if ids:
        ph = ",".join("?" * len(ids))
        for a in ctx.db.q(f"SELECT item_id, asset_id FROM news_asset WHERE item_id IN ({ph}) ORDER BY score DESC", ids):
            assets_by_item.setdefault(a["item_id"], []).append(a["asset_id"])
    pf = ctx.portfolio()
    for x in page:
        x["assets"] = [(aid, pf.asset(aid).name if pf else aid) for aid in assets_by_item.get(x["row"]["id"], [])]
    return page, total


# ----------------------------------------------------------------------------------------------------
# Routen
# ----------------------------------------------------------------------------------------------------

def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/news", response_class=HTMLResponse)
    def news_page(request: Request, tab: str = "articles", asset: str = "", source: str = "", days: int = 7,
                  status: str = "all", sort: str = "relevance", shorts: int = 0, hidden: int = 0,
                  page: int = 1) -> HTMLResponse:
        ctx = get_ctx(request)
        svc = news_service(ctx)
        days = max(1, min(int(days), 90))
        params = {"tab": tab, "asset": asset, "source": source, "days": days, "status": status, "sort": sort,
                  "shorts": shorts, "hidden": hidden}
        base: dict[str, Any] = {"active": "news", "params": params, "cfg_errors": svc.cfg.errors}
        pf = ctx.portfolio()
        val = ctx.valuation()
        base["assets"] = [(p.asset_id, p.asset.name) for p in (val.positions if val else []) if not p.asset.is_fiat]
        base["sources"] = ctx.db.q("SELECT DISTINCT source_id, source_name FROM news_item ORDER BY source_name")
        base["n_suggestions"] = ctx.db.scalar("SELECT COUNT(*) FROM yt_suggestion WHERE status='new'", default=0)
        digest = ctx.db.q1("SELECT * FROM digest ORDER BY day DESC LIMIT 1")
        base["digest"] = digest
        base["llm"] = LlmService(ctx)
        if tab in ("articles", "videos"):
            kind = "article" if tab == "articles" else "video"
            page = max(1, int(page))
            items, total = query_items(ctx, kind=kind, asset=asset, source=source, days=days, status=status, sort=sort,
                                       shorts=bool(shorts), hidden=bool(hidden), offset=(page - 1) * PAGE_SIZE)
            base.update(items=items, total=total, page=page, page_size=PAGE_SIZE)
            if request.headers.get("hx-target") == "news-more":
                return render(request, "partials/news_items.html", alerts=[], **base)
        elif tab == "sources":
            status_rows = {r["source_id"]: r for r in ctx.db.q("SELECT * FROM source_status")}
            counts = {r["source_id"]: r["n"] for r in ctx.db.q(
                "SELECT source_id, COUNT(*) AS n FROM news_item GROUP BY source_id")}
            base.update(src_list=svc.cfg.sources(), status_rows=status_rows, counts=counts,
                        channels=svc.channel_state(), discovery=svc.cfg.discovery(),
                        yt_units=svc.yt.units_used(), has_yt_key=bool(ctx.config.secrets.youtube_api_key),
                        settings=svc.cfg.settings, sources_path=str(ctx.config.sources_path))
        elif tab == "suggestions":
            base["suggestions"] = ctx.db.q(
                "SELECT * FROM yt_suggestion ORDER BY CASE status WHEN 'new' THEN 0 ELSE 1 END, hits DESC, "
                "subscribers DESC LIMIT 200")
        del pf
        return render(request, "news.html", **base)

    @router.post("/news/{item_id}/read")
    async def mark_read(request: Request, item_id: int) -> Response:
        ctx = get_ctx(request)
        form = await request.form()
        val = 0 if form.get("unread") == "1" else 1
        ctx.db.x("UPDATE news_item SET is_read=? WHERE id=?", (val, item_id))
        if request.headers.get("hx-request") == "true" and form.get("fragment") == "1":
            row = ctx.db.q1("SELECT * FROM news_item WHERE id=?", (item_id,))
            if row is None:
                raise HTTPException(404)
            return render(request, "partials/news_read_button.html", alerts=[], n=row)
        return Response(status_code=204)

    @router.post("/news/read-all")
    async def read_all(request: Request) -> Response:
        ctx = get_ctx(request)
        form = await request.form()
        kind = "video" if form.get("tab") == "videos" else "article"
        days = int(form.get("days") or 7)
        since = iso(datetime.now(UTC) - timedelta(days=days))
        ctx.db.x("UPDATE news_item SET is_read=1 WHERE kind=? AND published_at>=?", (kind, since))
        return Response(status_code=204, headers={"HX-Refresh": "true"})

    @router.post("/news/refresh", response_class=HTMLResponse)
    def refresh(request: Request) -> HTMLResponse:
        ctx = get_ctx(request)
        if ctx.scheduler is not None:
            ctx.scheduler.trigger("news_fetch", 0.5)
            return HTMLResponse('<span class="badge info">Abruf gestartet – Ergebnisse erscheinen in Kürze.</span>')
        return HTMLResponse('<span class="badge warn">Scheduler inaktiv</span>')

    @router.post("/youtube/suggestion/{channel_id}/{action}")
    def suggestion(request: Request, channel_id: str, action: str) -> Response:
        ctx = get_ctx(request)
        svc = news_service(ctx)
        row = ctx.db.q1("SELECT * FROM yt_suggestion WHERE channel_id=?", (channel_id,))
        if row is None or action not in ("subscribe", "ignore", "block"):
            raise HTTPException(404)
        handle = row["handle"]
        if handle and not handle.startswith("@"):
            handle = "@" + handle
        if action == "subscribe":
            svc.cfg.add_channel(channel_id, row["title"], handle)
            ctx.db.x("UPDATE yt_suggestion SET status='subscribed' WHERE channel_id=?", (channel_id,))
            ctx.db.x("UPDATE news_item SET source_weight=0.6 WHERE channel_id=? AND source_id='yt:discovery'",
                     (channel_id,))
        elif action == "ignore":
            svc.cfg.list_channel("ignored_channels", channel_id, row["title"])
            ctx.db.x("UPDATE yt_suggestion SET status='ignored' WHERE channel_id=?", (channel_id,))
        else:
            svc.cfg.list_channel("blocked_channels", channel_id, row["title"])
            ctx.db.x("UPDATE yt_suggestion SET status='blocked' WHERE channel_id=?", (channel_id,))
            ctx.db.x("UPDATE news_item SET hidden_reason='Kanal blockiert' WHERE channel_id=?", (channel_id,))
        log.info("YouTube-Vorschlag %s: %s", channel_id, action)
        return Response(status_code=204, headers={"HX-Refresh": "true"})

    @router.post("/youtube/channel")
    async def channel_action(request: Request) -> Response:
        ctx = get_ctx(request)
        svc = news_service(ctx)
        form = await request.form()
        key = str(form.get("key") or "")
        action = form.get("action")
        if action == "confirm":
            row = ctx.db.q1("SELECT * FROM yt_channel WHERE handle=?", (key,))
            if row is None or not row["channel_id"]:
                raise HTTPException(400, "Kanal ist nicht aufgelöst")
            svc.cfg.set_channel(key, channel_id=row["channel_id"], title=row["title"], confirmed=True)
        elif action in ("activate", "deactivate"):
            svc.cfg.set_channel(key, active=action == "activate")
        elif action == "resolve":
            if ctx.scheduler is not None:
                ctx.scheduler.trigger("news_fetch", 0.5)
            row = ctx.db.q1("SELECT handle FROM yt_channel WHERE handle=?", (key,))
            if row:
                ctx.db.x("UPDATE yt_channel SET resolved_at=NULL WHERE handle=?", (key,))
        return Response(status_code=204, headers={"HX-Refresh": "true"})

    @router.post("/news/source")
    async def source_action(request: Request) -> Response:
        ctx = get_ctx(request)
        svc = news_service(ctx)
        form = await request.form()
        sid = str(form.get("id") or "")
        action = form.get("action")
        if action in ("activate", "deactivate"):
            svc.cfg.set_source(sid, active=action == "activate")
            if action == "activate":
                ctx.db.x("UPDATE source_status SET consecutive_failures=0, next_allowed=NULL WHERE source_id=?", (sid,))
        elif action == "weight":
            try:
                w = max(0.0, min(1.0, float(str(form.get("weight")).replace(",", "."))))
            except ValueError:
                raise HTTPException(400, "Gewicht ungültig") from None
            svc.cfg.set_source(sid, weight=w)
        return Response(status_code=204, headers={"HX-Refresh": "true"})

    @router.get("/img/news/{item_id}")
    def news_image(request: Request, item_id: int) -> Response:
        ctx = get_ctx(request)
        row = ctx.db.q1("SELECT image_url FROM news_item WHERE id=?", (item_id,))
        if row is None or not row["image_url"]:
            raise HTTPException(404)
        url = row["image_url"]
        host = (urlsplit(url).hostname or "").lower()
        if host == "i.ytimg.com":
            return RedirectResponse(url, status_code=302)
        path = cached_image(ctx, url)
        if path is None:
            raise HTTPException(404)
        return FileResponse(path[0], media_type=path[1], headers={"Cache-Control": "public, max-age=604800"})

    return router


register_router(make_router)


# ----------------------------------------------------------------------------------------------------
# Bild-Proxy mit Cache (SSRF-geschützt)
# ----------------------------------------------------------------------------------------------------

def _public_host(host: str, port: int) -> bool:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
                or ip.is_unspecified):
            return False
    return bool(infos)


def cached_image(ctx: AppContext, url: str) -> tuple[Path, str] | None:
    key = hashlib.sha256(url.encode()).hexdigest()[:40]
    row = ctx.db.q1("SELECT path, content_type FROM img_cache WHERE key=?", (key,))
    if row is not None and Path(row["path"]).exists():
        return Path(row["path"]), row["content_type"]
    current = url
    for _ in range(4):
        parts = urlsplit(current)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if port not in (80, 443) or not _public_host(parts.hostname, port):
            return None
        try:
            with ctx.http.stream("GET", current, follow_redirects=False, timeout=10,
                                 headers={"Accept": "image/avif,image/webp,image/*"}) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    current = urljoin(current, r.headers["location"])
                    continue
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                if r.status_code != 200 or not ctype.startswith("image/") or "svg" in ctype:
                    return None
                data = b""
                for chunk in r.iter_bytes():
                    data += chunk
                    if len(data) > MAX_IMG_BYTES:
                        return None
        except httpx.HTTPError:
            return None
        ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
               "image/avif": ".avif"}.get(ctype, ".img")
        path = ctx.config.cache_dir / "img" / f"{key}{ext}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        ctx.db.x("INSERT OR REPLACE INTO img_cache(key, path, content_type, size, fetched_at) VALUES (?,?,?,?,?)",
                 (key, str(path), ctype, len(data), iso(datetime.now(UTC))))
        return path, ctype
    return None
