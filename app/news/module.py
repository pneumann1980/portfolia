"""News & Videos: Routen, Hintergrundjobs und Bild-Proxy (registriert sich beim Import in main)."""

from __future__ import annotations

import html
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.context import AppContext
from app.jobs.scheduler import Scheduler, extra_jobs, startup_job
from app.news.ai import AiGateway
from app.news.analysis import AnalysisService
from app.news.enrich import EnrichService
from app.news.events import EventService
from app.news.images import cached_image
from app.news.llm import LlmService
from app.news.relevance import recency_factor
from app.news.report import ReportService
from app.news.service import NewsService
from app.news.status import pipeline_status
from app.news.telegram import TelegramInputError, add_telegram_source
from app.progress import Progress, job_progress
from app.util.timeutil import iso, parse_iso
from app.web.app import register_router
from app.web.deps import get_ctx, render

log = logging.getLogger(__name__)
PAGE_SIZE = 30
_services: dict[int, NewsService] = {}
_enrichers: dict[int, EnrichService] = {}


def news_service(ctx: AppContext) -> NewsService:
    svc = _services.get(id(ctx))
    if svc is None:
        svc = NewsService(ctx)
        _services[id(ctx)] = svc
    return svc


def enrich_service(ctx: AppContext) -> EnrichService:
    svc = _enrichers.get(id(ctx))
    if svc is None:
        svc = EnrichService(ctx)
        _enrichers[id(ctx)] = svc
    return svc


# ----------------------------------------------------------------------------------------------------
# Jobs
# ----------------------------------------------------------------------------------------------------

def job_fetch(ctx: AppContext) -> dict[str, Any]:
    res = news_service(ctx).run()
    llm = LlmService(ctx)
    ai = AiGateway(ctx)
    # Die strukturierte Analyse (job_analyze) ersetzt die frühere Kurzzusammenfassung; ohne sie bleibt der alte Weg.
    if llm.enabled() and not (ctx.settings.get("news.analysis", True) and ai.enabled()):
        res["llm"] = llm.summarize()
    if ctx.scheduler is not None:  # Stufe B getrennt: Fehler dort blockieren den Import nie
        ctx.scheduler.trigger("news_enrich", 5)
        ctx.scheduler.trigger("news_group", 20)
    return res


def job_enrich(ctx: AppContext) -> dict[str, Any]:
    res = enrich_service(ctx).run()
    if ctx.scheduler is not None and res.get("selected"):
        ctx.scheduler.trigger("news_analyze", 10)
    return res


def job_analyze(ctx: AppContext) -> dict[str, Any]:
    res = AnalysisService(ctx).run()
    if ctx.scheduler is not None and res.get("analyzed"):
        ctx.scheduler.trigger("news_group", 5)
    return res


def job_group(ctx: AppContext) -> dict[str, Any]:
    return EventService(ctx).run()


def _report_progress(ctx: AppContext) -> Progress:
    return job_progress(ctx, "news_report", "Täglicher Finanzmarktbericht",
                        phases=["rep_collect", "rep_ai", "rep_save"], unit="Schritte")


def job_report(ctx: AppContext, manual: bool = False) -> dict[str, Any]:
    svc = ReportService(ctx)
    if manual:
        return svc.generate(manual=True, progress=lambda: _report_progress(ctx))
    return svc.run_due(progress=lambda: _report_progress(ctx))


def job_rematch(ctx: AppContext) -> dict[str, Any]:
    return news_service(ctx).rematch()


def job_discovery(ctx: AppContext) -> dict[str, Any]:
    return news_service(ctx).discover()


def job_digest(ctx: AppContext) -> dict[str, Any]:
    if ctx.settings.get("news.report", True) and AiGateway(ctx).enabled():
        return {"skipped": "ersetzt durch den täglichen Finanzmarktbericht"}
    return LlmService(ctx).digest()


@extra_jobs
def _register_jobs(s: Scheduler) -> None:
    svc = news_service(s.ctx)
    minutes = int(svc.cfg.settings.get("fetch_interval_minutes", 30) or 30)
    s.register("news_fetch", job_fetch, IntervalTrigger(minutes=max(10, minutes)))
    s.register("news_rematch", job_rematch, None)
    s.register("youtube_discovery", job_discovery, CronTrigger(hour=7, minute=5))
    s.register("llm_digest", job_digest, CronTrigger(hour=7, minute=40))
    s.register("news_enrich", job_enrich, IntervalTrigger(minutes=15))
    s.register("news_analyze", job_analyze, IntervalTrigger(minutes=10))
    s.register("news_group", job_group, IntervalTrigger(minutes=20))
    s.register("news_report", job_report, IntervalTrigger(minutes=15), executor="interactive")


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
    events: dict[int, Any] = {}
    cstatus: dict[int, str] = {}
    sentiment: dict[int, tuple[str, float]] = {}
    if ids:
        ph = ",".join("?" * len(ids))
        for e in ctx.db.q(f"SELECT i.item_id, i.event_id, v.n_sources, v.n_items FROM news_event_item i "
                          f"JOIN news_event v ON v.id=i.event_id WHERE i.item_id IN ({ph})", ids):
            events[e["item_id"]] = e
        for c in ctx.db.q(f"SELECT item_id, status FROM news_content WHERE item_id IN ({ph})", ids):
            cstatus[c["item_id"]] = c["status"]
        for a in ctx.db.q(f"SELECT item_id, sentiment, importance FROM news_analysis "
                          f"WHERE summary IS NOT NULL AND item_id IN ({ph})", ids):
            sentiment[a["item_id"]] = (a["sentiment"], a["importance"])
    for x in page:
        x["assets"] = [(aid, pf.asset(aid).name if pf else aid) for aid in assets_by_item.get(x["row"]["id"], [])]
        x["event"] = events.get(x["row"]["id"])
        x["cstatus"] = cstatus.get(x["row"]["id"])
        x["sentiment"], x["importance"] = sentiment.get(x["row"]["id"], (None, None))
    return page, total


# ----------------------------------------------------------------------------------------------------
# Routen
# ----------------------------------------------------------------------------------------------------

def _safe_back(back: str) -> str:
    """Rücksprung nur auf interne News-Seiten (kein Open-Redirect)."""
    return back if back.startswith("/news") and "//" not in back and "\\" not in back else "/news"


def report_ctx(ctx: AppContext, report_id: int = 0) -> dict[str, Any]:
    svc = ReportService(ctx)
    rep = svc.by_id(report_id) if report_id else svc.latest()
    return {"report": rep, "history": svc.history(30), "report_error": svc.last_error(), "live": report_state(ctx),
            "ai_status": AiGateway(ctx).status(), "report_time": svc.due_time(),
            "report_on": bool(ctx.settings.get("news.report", True))}


def report_state(ctx: AppContext) -> dict[str, Any]:
    """Zustand einer manuell angestoßenen Berichtserstellung: wartend | laeuft | fertig | fehler | leer."""
    req = ctx.db.get_state("news.report.manual")
    st = ctx.job_status().get("news_report") or {}
    asked = parse_iso(req.get("requested_at")) if isinstance(req, dict) else None
    if asked is None or (datetime.now(UTC) - asked).total_seconds() > 3600:
        return {"state": "leer"}  # alte Meldungen (auch Fehler) nicht ewig anzeigen
    started = parse_iso(st.get("last_start")) if st.get("last_start") else None
    if st.get("running") and started and started >= asked - timedelta(seconds=2):
        p = st.get("progress") or {}
        return {"state": "laeuft", "p": p if p.get("v") == 2 else {"v": 2, "label": "Täglicher Finanzmarktbericht",
                                                                   "running": True, "pct": 1, "phase_label": "Start"}}
    ended = parse_iso(st.get("last_end")) if st.get("last_end") else None
    if started and started >= asked - timedelta(seconds=2) and ended and ended >= started:
        res = st.get("progress") or {}
        if st.get("last_ok") and res.get("report"):
            return {"state": "fertig"}
        err = (ReportService(ctx).last_error() or {}).get("text") or res.get("skipped") or res.get("error") \
            or st.get("last_error") or "Der Bericht wurde nicht erstellt."
        return {"state": "fehler", "text": str(err)}
    if (datetime.now(UTC) - asked).total_seconds() > 600:
        return {"state": "fehler", "text": "Keine Rückmeldung – der Auftrag wurde nicht gestartet (Scheduler prüfen)."}
    return {"state": "wartend"}


def report_status_view(request: Request, ctx: AppContext) -> HTMLResponse:
    st = report_state(ctx)
    resp = render(request, "partials/report_progress.html", alerts=[], st=st)
    if st["state"] == "fertig":
        ctx.db.set_state("news.report.manual", None)
        resp.headers["HX-Refresh"] = "true"
    return resp


def reader_context(ctx: AppContext, row: Any, back: str = "") -> dict[str, Any]:
    """Alles, was die Leseansicht braucht – getrennt nach Quelle (Artikel), KI-Zusammenfassung und KI-Einordnung."""
    iid = row["id"]
    content = ctx.db.q1("SELECT * FROM news_content WHERE item_id=?", (iid,))
    analysis = AnalysisService(ctx).get(iid)
    events = EventService(ctx)
    ev = events.event_of(iid)
    others = [m for m in events.members(ev["id"]) if m["id"] != iid] if ev else []
    pf = ctx.portfolio()
    assets = [(r["asset_id"], pf.asset(r["asset_id"]).name if pf else r["asset_id"]) for r in ctx.db.q(
        "SELECT asset_id FROM news_asset WHERE item_id=? ORDER BY score DESC", (iid,))]
    paragraphs = [p for p in (content["content"] or "").split("\n\n") if p.strip()] if content else []
    return {"n": row, "content": content, "paragraphs": paragraphs, "analysis": analysis, "event": ev,
            "others": others, "assets": assets, "back": _safe_back(back), "ai": AiGateway(ctx)}


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/news", response_class=HTMLResponse)
    def news_page(request: Request, tab: str = "articles", asset: str = "", source: str = "", days: int = 7,
                  status: str = "all", sort: str = "relevance", shorts: int = 0, hidden: int = 0,
                  page: int = 1, report_id: int = 0) -> HTMLResponse:
        ctx = get_ctx(request)
        svc = news_service(ctx)
        days = max(1, min(int(days), 90))
        params = {"tab": tab, "asset": asset, "source": source, "days": days, "status": status, "sort": sort,
                  "shorts": shorts, "hidden": hidden}
        base: dict[str, Any] = {"active": "news", "params": params, "cfg_errors": svc.cfg.errors,
                                "back_url": "/news?" + urlencode({k: v for k, v in params.items() if v not in ("", 0)})}
        pf = ctx.portfolio()
        val = ctx.valuation()
        base["assets"] = [(p.asset_id, p.asset.name) for p in (val.positions if val else []) if not p.asset.is_fiat]
        base["sources"] = ctx.db.q("SELECT DISTINCT source_id, source_name FROM news_item ORDER BY source_name")
        base["n_suggestions"] = ctx.db.scalar("SELECT COUNT(*) FROM yt_suggestion WHERE status='new'", default=0)
        digest = ctx.db.q1("SELECT * FROM digest ORDER BY day DESC LIMIT 1")
        base["digest"] = digest
        base["llm"] = LlmService(ctx)
        rep = ReportService(ctx)
        base["report_teaser"] = rep.latest()
        base["ai"] = AiGateway(ctx)
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
                        settings=svc.cfg.settings, sources_path=str(ctx.config.sources_path),
                        tg_bridge=str(ctx.settings.get("news.telegram_bridge", "")))
        elif tab == "report":
            base.update(report_ctx(ctx, report_id))
        elif tab == "status":
            base["st"] = pipeline_status(ctx)
        elif tab == "suggestions":
            base["suggestions"] = ctx.db.q(
                "SELECT * FROM yt_suggestion ORDER BY CASE status WHEN 'new' THEN 0 ELSE 1 END, hits DESC, "
                "subscribers DESC LIMIT 200")
        del pf
        return render(request, "news.html", **base)

    @router.get("/news/{item_id:int}", response_class=HTMLResponse)
    def reader(request: Request, item_id: int, back: str = "", keep: int = 0) -> Response:
        ctx = get_ctx(request)
        row = ctx.db.q1("SELECT * FROM news_item WHERE id=?", (item_id,))
        if row is None:
            raise HTTPException(404, "Meldung nicht gefunden")
        if row["kind"] == "video":  # Videos laufen beim Anbieter
            ctx.db.x("UPDATE news_item SET is_read=1 WHERE id=?", (item_id,))
            return RedirectResponse(row["url"], status_code=303)
        if not keep and not row["is_read"]:
            ctx.db.x("UPDATE news_item SET is_read=1 WHERE id=?", (item_id,))
            row = ctx.db.q1("SELECT * FROM news_item WHERE id=?", (item_id,))
        return render(request, "news_reader.html", active="news", **reader_context(ctx, row, back))

    @router.get("/news/event/{event_id:int}", response_class=HTMLResponse)
    def event_page(request: Request, event_id: int, back: str = "") -> HTMLResponse:
        ctx = get_ctx(request)
        ev = ctx.db.q1("SELECT * FROM news_event WHERE id=?", (event_id,))
        if ev is None:
            raise HTTPException(404, "Ereignis nicht gefunden")
        svc = EventService(ctx)
        return render(request, "news_event.html", active="news", ev=ev, members=svc.members(event_id),
                      back=_safe_back(back))

    @router.post("/news/{item_id:int}/event/split")
    def event_split(request: Request, item_id: int) -> Response:
        ctx = get_ctx(request)
        ok = EventService(ctx).split_item(item_id)
        if not ok:
            raise HTTPException(404, "Meldung gehört zu keinem Ereignis")
        return Response(status_code=204, headers={"HX-Redirect": f"/news/{item_id}?keep=1"})

    @router.post("/news/{item_id:int}/refetch")
    def refetch(request: Request, item_id: int) -> Response:
        ctx = get_ctx(request)
        if ctx.db.q1("SELECT 1 FROM news_item WHERE id=?", (item_id,)) is None:
            raise HTTPException(404)
        enrich_service(ctx).reset(item_id)
        if ctx.scheduler is not None:
            ctx.scheduler.trigger("news_enrich", 1)
        return Response(status_code=204, headers={"HX-Redirect": f"/news/{item_id}?keep=1"})

    @router.post("/news/report/regenerate", response_class=HTMLResponse)
    def report_regenerate(request: Request) -> HTMLResponse:
        ctx = get_ctx(request)
        if not AiGateway(ctx).enabled():
            return HTMLResponse('<span class="badge warn">KI ist nicht aktiviert (Einstellungen → News)</span>')
        ctx.db.set_state("news.report.manual", {"requested_at": iso(datetime.now(UTC))})
        if ctx.scheduler is not None:
            ctx.scheduler.trigger("news_report", 0.3, manual=True)
        else:  # ohne Scheduler (Tests, Einzelaufruf): sofort erzeugen, Zustand wie bei einem Job festhalten
            from app.jobs import tasks

            tasks.run_job(ctx, "news_report", lambda: job_report(ctx, manual=True))
        return report_status_view(request, ctx)

    @router.get("/news/report/status", response_class=HTMLResponse)
    def report_status(request: Request) -> HTMLResponse:
        return report_status_view(request, get_ctx(request))

    @router.post("/news/pipeline/run", response_class=HTMLResponse)
    def pipeline_run(request: Request) -> HTMLResponse:
        ctx = get_ctx(request)
        if ctx.scheduler is None:
            return HTMLResponse('<span class="badge warn">Scheduler inaktiv</span>')
        for name, delay in (("news_enrich", 0.5), ("news_analyze", 30), ("news_group", 60)):
            ctx.scheduler.trigger(name, delay)
        return HTMLResponse('<span class="badge info">Anreicherung, Analyse und Gruppierung gestartet.</span>')

    @router.post("/news/{item_id:int}/read")
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
        elif action in ("ai_on", "ai_off"):
            svc.cfg.set_source(sid, ai=action == "ai_on")
        elif action == "remove":
            src = next((x for x in svc.cfg.sources() if x.id == sid), None)
            if src is None or src.type != "telegram":  # nur selbst hinzugefügte Telegram-Kanäle sind löschbar
                raise HTTPException(400, "Nur Telegram-Kanäle können entfernt werden (andere: deaktivieren).")
            svc.cfg.remove_source(sid)
        return Response(status_code=204, headers={"HX-Refresh": "true"})

    @router.post("/news/source/telegram")
    async def add_telegram(request: Request) -> Response:
        ctx = get_ctx(request)
        svc = news_service(ctx)
        form = await request.form()
        try:
            weight = float(str(form.get("weight") or "0.4").replace(",", "."))
        except ValueError:
            return HTMLResponse('<span class="badge crit">Gewicht ungültig (0 bis 1).</span>')
        try:
            res = await run_in_threadpool(
                add_telegram_source, svc.cfg, ctx.http, channel=str(form.get("channel") or ""),
                bridge=str(form.get("bridge") or ctx.settings.get("news.telegram_bridge", "")),
                name=str(form.get("name") or ""), language=str(form.get("language") or "en"), weight=weight,
                require_match=form.get("require_match") == "1", ai=form.get("ai") == "1")
        except TelegramInputError as e:
            return HTMLResponse(f'<span class="badge crit">{html.escape(str(e))}</span>')
        if res["base"]:
            ctx.settings.set("news.telegram_bridge", res["base"])
        if ctx.scheduler is not None:
            ctx.scheduler.trigger("news_fetch", 1)
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
