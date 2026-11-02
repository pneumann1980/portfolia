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
from app.news import queue, wave, ytlib
from app.news.ai import AiGateway
from app.news.analysis import AnalysisService
from app.news.enrich import EnrichService
from app.news.events import EventService
from app.news.images import cached_image
from app.news.llm import LlmService
from app.news.locks import exclusive
from app.news.relevance import recency_factor
from app.news.report import ReportService
from app.news.service import NewsService
from app.news.status import pipeline_status
from app.news.telegram import TelegramInputError, add_telegram_source
from app.news.transcripts import TranscriptService, is_transcript
from app.news.wave import WaveRunner
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
    with exclusive(ctx, "fetch") as got:
        if not got:
            return {"skipped": "läuft bereits"}
        res = news_service(ctx).run()
    llm = LlmService(ctx)
    ai = AiGateway(ctx)
    # Die strukturierte Analyse (job_analyze) ersetzt die frühere Kurzzusammenfassung; ohne sie bleibt der alte Weg.
    if llm.enabled() and not (ctx.settings.get("news.analysis", True) and ai.enabled()):
        res["llm"] = llm.summarize()
    _observe(ctx)  # neue Meldungen öffnen den Zyklus der Artikeltexte sofort
    if ctx.scheduler is not None:  # Stufe B getrennt: Fehler dort blockieren den Import nie
        ctx.scheduler.trigger("news_enrich", 5)
        ctx.scheduler.trigger("news_transcripts", 15)
        ctx.scheduler.trigger("news_group", 20)
    return res


def job_transcripts(ctx: AppContext) -> dict[str, Any]:
    """YouTube-Transkripte neuer Videos laden; danach die Analyse anstoßen (Kernaussagen)."""
    with exclusive(ctx, "transcripts") as got:
        if not got:
            return {"skipped": "läuft bereits"}
        _observe(ctx)
        res = TranscriptService(ctx).run()
        _observe(ctx)
    if ctx.scheduler is not None and res.get("loaded"):
        ctx.scheduler.trigger("news_analyze", 2)
    return res


def _observe(ctx: AppContext) -> None:
    """Warteschlangen-Zyklen (Balken der Statusseite) fortschreiben; darf einen Job nie scheitern lassen."""
    try:
        queue.observe_all(ctx, enrich_service(ctx))
    except Exception as e:
        log.debug("Warteschlange nicht aktualisiert: %s", e)


def job_enrich(ctx: AppContext) -> dict[str, Any]:
    _observe(ctx)
    res = enrich_service(ctx).run()
    _observe(ctx)
    if ctx.scheduler is not None and res.get("selected"):
        ctx.scheduler.trigger("news_analyze", 10)
    return res


def job_analyze(ctx: AppContext) -> dict[str, Any]:
    _observe(ctx)
    res = AnalysisService(ctx).run()
    _observe(ctx)
    if ctx.scheduler is not None and res.get("analyzed"):
        ctx.scheduler.trigger("news_group", 5)
    return res


def job_group(ctx: AppContext) -> dict[str, Any]:
    with exclusive(ctx, "group") as got:
        if not got:
            return {"skipped": "läuft bereits"}
        return EventService(ctx).run()


def job_wave(ctx: AppContext, fetch: bool = True) -> dict[str, Any]:
    """Manueller Lauf „Jetzt abarbeiten“ (Import → Transkripte → Artikeltexte → Analyse → Gruppierung) –
    ``app.news.wave``."""
    with exclusive(ctx, "wave") as got:
        if not got:
            return {"skipped": "manueller Lauf läuft bereits"}
        runner = WaveRunner(ctx, (lambda: news_service(ctx).run()) if fetch else None,
                            enrich=enrich_service(ctx), state=wave.reserved(ctx))
        return runner.run()


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
    # Eigener Pool „news“: die langen News-Läufe (Abruf, Textabruf, KI-Analyse) verdrängen keine Kurs-/Import-Jobs
    s.register("news_fetch", job_fetch, IntervalTrigger(minutes=max(10, minutes)), executor="news")
    s.register("news_rematch", job_rematch, None, executor="news")
    s.register("youtube_discovery", job_discovery, CronTrigger(hour=7, minute=5), executor="news")
    s.register("llm_digest", job_digest, CronTrigger(hour=7, minute=40), executor="news")
    s.register("news_enrich", job_enrich, IntervalTrigger(minutes=15), executor="news")
    s.register("news_analyze", job_analyze, IntervalTrigger(minutes=10), executor="news")
    s.register("news_transcripts", job_transcripts, IntervalTrigger(minutes=30), executor="news")
    s.register("news_group", job_group, IntervalTrigger(minutes=20), executor="news")
    s.register("news_report", job_report, IntervalTrigger(minutes=15), executor="interactive")
    s.register("news_wave", job_wave, None, executor="wave")


startup_job("news_fetch", 45)


# ----------------------------------------------------------------------------------------------------
# Abfragen für die Oberfläche
# ----------------------------------------------------------------------------------------------------

STATUSES = ("unread", "all", "read")
READ_ALL_UNDO = "news.read_all.undo"
UNDO_MINUTES = 15
NOTE_MAX = 500


def query_items(ctx: AppContext, *, kind: str, asset: str = "", source: str = "", days: int = 7,
                status: str = "all", sort: str = "relevance", shorts: bool = False, hidden: bool = False,
                offset: int = 0, limit: int = PAGE_SIZE,
                sources: list[str] | None = None) -> tuple[list[dict[str, Any]], int]:
    """``sources``: nur diese Quellen (z. B. Community); leere Liste = keine Treffer."""
    items = _filtered(ctx, kind=kind, asset=asset, source=source, days=days, status=status, sort=sort,
                      shorts=shorts, hidden=hidden, sources=sources)
    return _decorate(ctx, items[offset:offset + limit]), len(items)


def _filtered(ctx: AppContext, *, kind: str, asset: str = "", source: str = "", days: int = 7, status: str = "all",
              sort: str = "relevance", shorts: bool = False, hidden: bool = False,
              sources: list[str] | None = None) -> list[dict[str, Any]]:
    """Alle Meldungen der Auswahl in Anzeigereihenfolge (höchstens 1500 neueste) – Liste und „alle gelesen“."""
    since = iso(datetime.now(UTC) - timedelta(days=days))
    where = ["n.kind=?", "n.published_at>=?"]
    params: list[Any] = [kind, since]
    if sources is not None:
        if not sources:
            return []
        where.append(f"n.source_id IN ({','.join('?' * len(sources))})")
        params.extend(sources)
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
    if not asset and not source and sources is None and sort == "relevance":
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
    return items


def query_saved(ctx: AppContext, *, offset: int = 0, limit: int = PAGE_SIZE) -> tuple[list[dict[str, Any]], int]:
    """Gemerkte Meldungen aller Arten (Artikel, Videos, Community), zuletzt gemerkte zuerst – ohne Zeitfenster."""
    total = int(ctx.db.scalar("SELECT COUNT(*) FROM news_item WHERE saved_at IS NOT NULL", default=0) or 0)
    rows = ctx.db.q("SELECT * FROM news_item WHERE saved_at IS NOT NULL ORDER BY saved_at DESC, id DESC "
                    "LIMIT ? OFFSET ?", (limit, offset))
    now = datetime.now(UTC)
    items = []
    for r in rows:
        pub = parse_iso(r["published_at"]) or now
        items.append({"row": r, "score": float(r["relevance"]) * recency_factor(pub, now), "published": pub})
    return _decorate(ctx, items), total


def _decorate(ctx: AppContext, page: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Positionen, Ereignis, Textstatus und Stimmung je Karte ergänzen (nur für die angezeigte Seite)."""
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
    trstates = transcript_states(ctx, [x["row"]["id"] for x in page if x["row"]["kind"] == "video"])
    for x in page:
        x["assets"] = [(aid, pf.asset(aid).name if pf else aid) for aid in assets_by_item.get(x["row"]["id"], [])]
        x["event"] = events.get(x["row"]["id"])
        x["cstatus"] = cstatus.get(x["row"]["id"])
        x["sentiment"], x["importance"] = sentiment.get(x["row"]["id"], (None, None))
        x["tr"] = trstates.get(x["row"]["id"])
    return page


# ---- Transkripte auf Wunsch (Knopf „Kernaussagen erstellen“, selbst einfügen) -------------------------------------

TR_POLL = {"queued": "4s", "analyzing": "4s", "paused": "60s"}


def transcript_states(ctx: AppContext, ids: list[int]) -> dict[int, dict[str, Any]]:
    """Stand der Kernaussagen je Video: none | queued | paused | analyzing | done | ai_off | no_captions | failed |
    analysis_failed – mit Abfrageintervall für die Live-Anzeige auf der Karte."""
    if not ids:
        return {}
    svc = TranscriptService(ctx)
    on = svc.enabled()
    paused = svc.paused_until()
    ai_on = AnalysisService(ctx).enabled()
    ph = ",".join("?" * len(ids))
    out: dict[int, dict[str, Any]] = {}
    for r in ctx.db.q(
            f"""SELECT n.id, n.tr_requested_at, c.status, c.reason, a.item_id AS aid, a.summary AS asum
                FROM news_item n LEFT JOIN news_content c ON c.item_id=n.id
                LEFT JOIN news_analysis a ON a.item_id=n.id WHERE n.id IN ({ph})""", ids):
        reason = str(r["reason"] or "")
        if r["status"] == "success" and reason.startswith("transcript:"):
            st = ("done" if r["asum"] else "analysis_failed" if r["aid"] else "analyzing" if ai_on else "ai_off")
        elif r["status"] == "failed":
            st = "no_captions" if reason in ("no_transcript", "unavailable") else "failed"
        elif r["tr_requested_at"]:
            st = "paused" if paused is not None else "queued" if on else "off"
        else:
            st = "none" if on else "off"
        out[int(r["id"])] = {"state": st, "poll": TR_POLL.get(st), "paused_until": iso(paused) if paused else None,
                             "pasted": ":eingefuegt:" in reason, "reason": reason}
    return out


def request_transcript(ctx: AppContext, item_id: int) -> dict[str, Any]:
    """Kernaussagen anfordern (bzw. nach Fehler erneut): Auftrag vormerken und den Abruf sofort anstoßen."""
    row = ctx.db.q1("SELECT id FROM news_item WHERE id=? AND kind='video'", (item_id,))
    if row is None:
        raise HTTPException(404, "Video nicht gefunden")
    ctx.db.x("UPDATE news_item SET tr_requested_at=? WHERE id=?", (iso(datetime.now(UTC)), item_id))
    # erneuter Versuch: Fehlschlag (nicht „keine Untertitel“) bzw. verworfene Analyse zurücksetzen
    ctx.db.x("""DELETE FROM news_content WHERE item_id=? AND (status='pending' OR (status='failed'
                AND reason NOT IN ('no_transcript','unavailable')))""", (item_id,))
    ctx.db.x("DELETE FROM news_analysis WHERE item_id=? AND summary IS NULL", (item_id,))
    if ctx.scheduler is not None:
        ctx.scheduler.trigger("news_transcripts", 0.3)
        ctx.scheduler.trigger("news_analyze", 2)  # liegt das Transkript schon vor (oder eingefügt): gleich auswerten
    log.info("Kernaussagen angefordert für Video %s", item_id)
    return transcript_states(ctx, [item_id])[item_id]


def paste_transcript(ctx: AppContext, item_id: int, text: str) -> int:
    words = TranscriptService(ctx).paste(item_id, text)
    ctx.db.x("DELETE FROM news_analysis WHERE item_id=? AND summary IS NULL", (item_id,))
    if ctx.scheduler is not None:
        ctx.scheduler.trigger("news_analyze", 1)
    log.info("Transkript für Video %s eingefügt (%s Wörter)", item_id, words)
    return words


# ---- Merken, Notiz, alle gelesen (synchron: läuft im Threadpool) ----------------------------------------

def set_saved(ctx: AppContext, item_id: int, on: bool) -> Any:
    """Merken (★) setzen/aufheben; die Notiz bleibt erhalten (erscheint wieder, wenn erneut gemerkt)."""
    if on:
        ctx.db.x("UPDATE news_item SET saved_at=COALESCE(saved_at, ?) WHERE id=?", (iso(datetime.now(UTC)), item_id))
    else:
        ctx.db.x("UPDATE news_item SET saved_at=NULL WHERE id=?", (item_id,))
    row = ctx.db.q1("SELECT * FROM news_item WHERE id=?", (item_id,))
    if row is None:
        raise HTTPException(404, "Meldung nicht gefunden")
    return row


def set_note(ctx: AppContext, item_id: int, note: str) -> Any:
    """Notiz speichern (gekürzt auf ``NOTE_MAX`` Zeichen, leer = keine); eine Notiz merkt die Meldung zugleich."""
    text = note.replace("\r", "").strip()[:NOTE_MAX] or None
    ctx.db.x("UPDATE news_item SET note=?, saved_at=COALESCE(saved_at, ?) WHERE id=?",
             (text, iso(datetime.now(UTC)), item_id))
    row = ctx.db.q1("SELECT * FROM news_item WHERE id=?", (item_id,))
    if row is None:
        raise HTTPException(404, "Meldung nicht gefunden")
    return row


def _filters_from(form: Any) -> dict[str, Any]:
    def num(key: str, default: int) -> int:
        try:
            return int(str(form.get(key) or default))
        except ValueError:
            return default

    status = str(form.get("status") or "unread")
    return {"tab": str(form.get("tab") or "articles"), "asset": str(form.get("asset") or ""),
            "source": str(form.get("source") or ""), "days": max(1, min(num("days", 7), 90)),
            "status": status if status in STATUSES else "unread", "sort": str(form.get("sort") or "relevance"),
            "shorts": bool(num("shorts", 0)), "hidden": bool(num("hidden", 0))}


def mark_all_read(ctx: AppContext, f: dict[str, Any]) -> int:
    """Genau die angezeigte Auswahl (alle Seiten, gleiche Filter) als gelesen markieren; die geänderten IDs werden
    für „Rückgängig“ gemerkt (``UNDO_MINUTES``)."""
    tab = f["tab"]
    if tab not in ("articles", "videos", "community"):
        return 0
    community = community_ids(news_service(ctx)) if tab == "community" else None
    items = _filtered(ctx, kind="video" if tab == "videos" else "article", asset=f["asset"], source=f["source"],
                      days=f["days"], status=f["status"], sort="time" if community is not None else f["sort"],
                      shorts=f["shorts"], hidden=f["hidden"], sources=community)
    ids = [int(x["row"]["id"]) for x in items if not x["row"]["is_read"]]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        ctx.db.x(f"UPDATE news_item SET is_read=1 WHERE id IN ({','.join('?' * len(chunk))})", chunk)
    ctx.db.set_state(READ_ALL_UNDO, {"ids": ids, "at": iso(datetime.now(UTC)), "tab": tab} if ids else None)
    return len(ids)


def _undo_valid(st: Any) -> bool:
    at = parse_iso(st.get("at")) if isinstance(st, dict) else None
    return at is not None and bool(st.get("ids")) and datetime.now(UTC) - at <= timedelta(minutes=UNDO_MINUTES)


def read_all_undo_info(ctx: AppContext) -> dict[str, Any] | None:
    """Letztes „alle gelesen“, solange es rückgängig gemacht werden kann."""
    st = ctx.db.get_state(READ_ALL_UNDO)
    return {"n": len(st["ids"]), "at": st["at"], "tab": st.get("tab")} if _undo_valid(st) else None


def undo_read_all(ctx: AppContext, dismiss: bool = False) -> int:
    st = ctx.db.get_state(READ_ALL_UNDO)
    ctx.db.set_state(READ_ALL_UNDO, None)
    if dismiss or not _undo_valid(st):
        return 0
    ids = [int(i) for i in st["ids"]]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        ctx.db.x(f"UPDATE news_item SET is_read=0 WHERE id IN ({','.join('?' * len(chunk))})", chunk)
    return len(ids)



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


def pipeline_view(request: Request, ctx: AppContext, note: str = "") -> HTMLResponse:
    """Stufen der Pipeline (Fragment; fragt sich selbst nach – häufig, solange etwas läuft)."""
    return render(request, "partials/news_pipeline.html", alerts=[], st=pipeline_status(ctx), pipe_note=note)


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
    transcript = [_ts_paragraph(p, row["url"]) for p in paragraphs] if is_transcript(content) else None
    return {"n": row, "content": content, "paragraphs": paragraphs, "analysis": analysis, "event": ev,
            "others": others, "assets": assets, "back": _safe_back(back), "ai": AiGateway(ctx),
            "transcript": transcript, "video_url": row["url"], "video_at": video_at,
            "tr": transcript_states(ctx, [iid]).get(iid) if row["kind"] == "video" else None}


def video_at(url: str, stelle: str) -> str:
    """YouTube-Link an eine Zeitmarke („m:ss“ bzw. „h:mm:ss“) – leer, wenn sie nicht lesbar ist."""
    try:
        sec = 0
        for part in stelle.split(":"):
            sec = sec * 60 + int(part)
    except ValueError:
        return ""
    return f"{url}{'&' if '?' in url else '?'}t={sec}s"


def _ts_paragraph(p: str, url: str) -> dict[str, str]:
    """Absatz eines Transkripts: Zeitmarke (mit Link ins Video) und Text."""
    if p.startswith("[") and "] " in p[:12]:
        ts, text = p[1:].split("] ", 1)
        return {"ts": ts, "href": video_at(url, ts), "text": text}
    return {"ts": "", "href": "", "text": p}


def transcripts_view(ctx: AppContext) -> dict[str, Any]:
    svc = TranscriptService(ctx)
    return {"on": svc.enabled(), "paused_until": svc.paused_until(), "stats": svc.stats(),
            "fav": svc.favorites(), "auto_per_day": svc.auto_per_day(), "auto_used": svc.auto_used_today(),
            "last_error": ctx.db.get_state("news.transcripts.last_error"), "lib": ytlib.info(ctx.config)}


def _transcripts_card(request: Request, ctx: AppContext, result: dict[str, Any] | None) -> HTMLResponse:
    return render(request, "partials/news_transcripts.html", tr=transcripts_view(ctx), tr_result=result)


def _transcripts_update(request: Request, ctx: AppContext) -> HTMLResponse:
    res = ytlib.update(ctx.config, ctx.http)
    if res.get("ok") and not res.get("unchanged"):  # neue Version: Sperre/Fehler neu bewerten
        ctx.db.set_state("news.transcripts.last_error", None)
        ctx.db.set_state("news.transcripts.paused_until", None)
    return _transcripts_card(request, ctx, res)


def resume_transcripts(ctx: AppContext) -> str:
    """YouTube-Sperre vorzeitig beenden: Pause aufheben, wegen der Sperre zurückgestellte Videos wieder fällig machen
    und den Abruf sofort anstoßen. Ist YouTube weiter gesperrt, endet der Lauf nach dem ersten Video und ruht erneut."""
    from app.news.transcripts import PAUSE_KEY

    ctx.db.set_state(PAUSE_KEY, None)
    ctx.db.x("UPDATE news_content SET next_attempt=NULL WHERE status='pending' AND reason='blocked'")
    log.info("YouTube-Transkripte: Sperre vorzeitig aufgehoben (manuell)")
    if ctx.scheduler is not None:
        ctx.scheduler.trigger("news_transcripts", 0.5)
        return ("Abruf fortgesetzt – läuft gleich an. Ist YouTube weiterhin gesperrt, ruht er nach dem ersten Video "
                "erneut 24 Stunden.")
    return "Pause aufgehoben – der nächste Transkript-Lauf startet nach Plan (Scheduler inaktiv)."


def _transcripts_fav(ctx: AppContext, form: dict[str, Any]) -> None:
    svc = TranscriptService(ctx)
    if form.get("channel_id"):
        svc.set_favorite(str(form["channel_id"]), form.get("on") == "1")
    if form.get("per_day") not in (None, ""):
        try:
            ctx.settings.set("news.transcripts_auto_per_day", max(0, min(int(str(form["per_day"])), 50)))
        except ValueError:
            raise HTTPException(400, "Tagesgrenze ungültig") from None


def _transcripts_toggle(request: Request, ctx: AppContext, on: bool) -> HTMLResponse:
    ctx.settings.set("news.transcripts", on)
    if on:
        ctx.db.set_state("news.transcripts.paused_until", None)
    return _transcripts_card(request, ctx, None)


def community_ids(svc: NewsService) -> list[str]:
    """IDs der Community-Quellen (Telegram-Kanäle) – eigener Reiter, auch inaktive (vorhandene Beiträge bleiben)."""
    return [x.id for x in svc.cfg.sources() if x.type == "telegram"]


TABS = ("articles", "videos", "community", "saved", "report", "suggestions", "sources", "status")


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/news", response_class=HTMLResponse)
    def news_page(request: Request, tab: str = "articles", asset: str = "", source: str = "", days: int = 7,
                  status: str = "unread", sort: str = "relevance", shorts: int = 0, hidden: int = 0,
                  page: int = 1, report_id: int = 0) -> HTMLResponse:
        ctx = get_ctx(request)
        svc = news_service(ctx)
        if tab not in TABS:  # veraltetes Lesezeichen / manipulierte URL: Standardreiter statt Fehlerseite
            tab = "articles"
        if status not in STATUSES:  # Standard „Ungelesen“: gelesene bleiben bis zum Neuladen sichtbar
            status = "unread"
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
        base["n_saved"] = int(ctx.db.scalar("SELECT COUNT(*) FROM news_item WHERE saved_at IS NOT NULL",
                                            default=0) or 0)
        if tab in ("articles", "videos", "community"):
            kind = "video" if tab == "videos" else "article"
            page = max(1, int(page))
            community = community_ids(svc) if tab == "community" else None
            if community is not None:  # Community: Telegram-Kanäle, neueste zuerst
                base["sources"] = ctx.db.q(
                    f"SELECT DISTINCT source_id, source_name FROM news_item WHERE source_id IN "
                    f"({','.join('?' * len(community)) or 'NULL'}) ORDER BY source_name", community)
                sort = base["params"]["sort"] = "time"
            items, total = query_items(ctx, kind=kind, asset=asset, source=source, days=days, status=status, sort=sort,
                                       shorts=bool(shorts), hidden=bool(hidden), offset=(page - 1) * PAGE_SIZE,
                                       sources=community)
            # leer unter „Ungelesen“: alles gelesen (Hinweis auf „Alle“) oder wirklich nichts da (Quellen-Hinweis)
            all_read = not total and status == "unread" and bool(_filtered(
                ctx, kind=kind, asset=asset, source=source, days=days, status="all", sort=sort, shorts=bool(shorts),
                hidden=bool(hidden), sources=community))
            base.update(items=items, total=total, page=page, page_size=PAGE_SIZE, undo=read_all_undo_info(ctx),
                        all_read=all_read)
            if request.headers.get("hx-target") == "news-more":
                return render(request, "partials/news_items.html", alerts=[], **base)
        elif tab == "saved":
            page = max(1, int(page))
            items, total = query_saved(ctx, offset=(page - 1) * PAGE_SIZE)
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
                        tg_bridge=str(ctx.settings.get("news.telegram_bridge", "")), tr=transcripts_view(ctx),
                        tr_result=None)
        elif tab == "report":
            base.update(report_ctx(ctx, report_id))
        elif tab == "status":
            base["st"] = pipeline_status(ctx)
            base["pipe_note"] = ""
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
        if row["kind"] == "video":  # ohne Transkript: Video beim Anbieter; mit Transkript: Leseansicht
            ctx.db.x("UPDATE news_item SET is_read=1 WHERE id=?", (item_id,))
            content = ctx.db.q1("SELECT status, reason FROM news_content WHERE item_id=?", (item_id,))
            if not is_transcript(content) or request.query_params.get("watch") == "1":
                return RedirectResponse(row["url"], status_code=303)
            row = ctx.db.q1("SELECT * FROM news_item WHERE id=?", (item_id,))
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

    @router.post("/news/transcripts/update", response_class=HTMLResponse)
    async def transcripts_update(request: Request) -> HTMLResponse:
        """Neueste Version der Transkript-Bibliothek laden, prüfen und aktivieren (Wheel, Selbsttest)."""
        ctx = get_ctx(request)
        return await run_in_threadpool(_transcripts_update, request, ctx)

    @router.post("/news/transcripts/reset", response_class=HTMLResponse)
    def transcripts_reset(request: Request) -> HTMLResponse:
        ctx = get_ctx(request)
        ytlib.reset(ctx.config)
        return _transcripts_card(request, ctx, {"ok": True, "reset": True})

    @router.post("/news/{item_id:int}/transcript/request", response_class=HTMLResponse)
    def transcript_request(request: Request, item_id: int) -> HTMLResponse:
        """Knopf „Kernaussagen erstellen“ auf der Videokarte – Fragment mit Live-Stand."""
        ctx = get_ctx(request)
        return render(request, "partials/news_tr_state.html", alerts=[], iid=item_id,
                      tr=request_transcript(ctx, item_id))

    @router.get("/news/{item_id:int}/transcript/state", response_class=HTMLResponse)
    def transcript_state(request: Request, item_id: int) -> HTMLResponse:
        ctx = get_ctx(request)
        st = transcript_states(ctx, [item_id]).get(item_id)
        if st is None:
            raise HTTPException(404)
        return render(request, "partials/news_tr_state.html", alerts=[], iid=item_id, tr=st)

    @router.get("/news/{item_id:int}/transkript", response_class=HTMLResponse)
    def transcript_paste_page(request: Request, item_id: int, back: str = "") -> HTMLResponse:
        """Transkript selbst einfügen (Text aus „Transkript anzeigen“ der YouTube-App)."""
        ctx = get_ctx(request)
        row = ctx.db.q1("SELECT * FROM news_item WHERE id=? AND kind='video'", (item_id,))
        if row is None:
            raise HTTPException(404, "Video nicht gefunden")
        return render(request, "news_paste.html", active="news", n=row, back=_safe_back(back), error="")

    @router.post("/news/{item_id:int}/transkript", response_class=HTMLResponse)
    async def transcript_paste(request: Request, item_id: int) -> Response:
        ctx = get_ctx(request)
        form = await request.form()
        text = str(form.get("text") or "")
        try:
            await run_in_threadpool(paste_transcript, ctx, item_id, text)
        except ValueError as e:
            return HTMLResponse(f'<div class="alert warn" role="alert"><span class="icon">!</span>'
                                f'<div>{html.escape(str(e))}</div></div>')
        return Response(status_code=204, headers={"HX-Redirect": f"/news/{item_id}?keep=1"})

    @router.post("/news/transcripts/fav")
    async def transcripts_fav(request: Request) -> Response:
        """Lieblingskanal (automatische Kernaussagen) an/aus bzw. Tagesgrenze setzen."""
        ctx = get_ctx(request)
        form = await request.form()
        await run_in_threadpool(_transcripts_fav, ctx, dict(form))
        return Response(status_code=204, headers={"HX-Refresh": "true"})

    @router.post("/news/transcripts/resume", response_class=HTMLResponse)
    def transcripts_resume(request: Request) -> HTMLResponse:
        """„Jetzt fortsetzen“ (Statusseite): YouTube-Sperre vorzeitig beenden und sofort weitermachen."""
        ctx = get_ctx(request)
        return pipeline_view(request, ctx, resume_transcripts(ctx))

    @router.post("/news/transcripts/toggle", response_class=HTMLResponse)
    async def transcripts_toggle(request: Request) -> HTMLResponse:
        ctx = get_ctx(request)
        form = await request.form()
        return await run_in_threadpool(_transcripts_toggle, request, ctx, form.get("on") == "1")

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
    async def pipeline_run(request: Request) -> HTMLResponse:
        """„Jetzt abarbeiten“: Import (optional) → Transkripte → Artikeltexte → Analyse → Gruppierung, nacheinander."""
        ctx = get_ctx(request)
        if ctx.scheduler is None:
            return HTMLResponse('<span class="badge warn">Scheduler inaktiv</span>')
        form = await request.form()
        fetch = str(form.get("fetch", "1")) not in ("0", "", "off")
        if wave.reserve(ctx, fetch) is None:
            note = "Ein manueller Lauf ist bereits angestoßen – der Fortschritt steht unten."
        else:
            ctx.scheduler.trigger("news_wave", 0.3, fetch=fetch)
            note = ""
        return pipeline_view(request, ctx, note)

    @router.get("/news/pipeline/live", response_class=HTMLResponse)
    def pipeline_live(request: Request) -> HTMLResponse:
        return pipeline_view(request, get_ctx(request))

    @router.post("/news/{item_id:int}/read")
    async def mark_read(request: Request, item_id: int) -> Response:
        ctx = get_ctx(request)
        form = await request.form()

        def work() -> Response:
            val = 0 if form.get("unread") == "1" else 1
            ctx.db.x("UPDATE news_item SET is_read=? WHERE id=?", (val, item_id))
            if request.headers.get("hx-request") == "true" and form.get("fragment") == "1":
                row = ctx.db.q1("SELECT * FROM news_item WHERE id=?", (item_id,))
                if row is None:
                    raise HTTPException(404)
                return render(request, "partials/news_read_button.html", alerts=[], n=row)
            return Response(status_code=204)

        return await run_in_threadpool(work)

    @router.post("/news/read-all")
    async def read_all(request: Request) -> Response:
        """„Alle angezeigten als gelesen“: genau die gefilterte Liste (alle Seiten); rückgängig machbar."""
        ctx = get_ctx(request)
        form = await request.form()
        await run_in_threadpool(mark_all_read, ctx, _filters_from(form))
        return Response(status_code=204, headers={"HX-Refresh": "true"})

    @router.post("/news/read-all/undo")
    async def read_all_undo(request: Request) -> Response:
        ctx = get_ctx(request)
        form = await request.form()
        await run_in_threadpool(undo_read_all, ctx, form.get("dismiss") == "1")
        return Response(status_code=204, headers={"HX-Refresh": "true"})

    @router.post("/news/{item_id:int}/save", response_class=HTMLResponse)
    async def save_item(request: Request, item_id: int) -> HTMLResponse:
        """Merken ★ (``on=1``) bzw. aufheben (``on=0``) – Fragment für Karte oder Leseansicht (``where=reader``)."""
        ctx = get_ctx(request)
        form = await request.form()
        row = await run_in_threadpool(set_saved, ctx, item_id, form.get("on") == "1")
        tpl = "partials/news_save_reader.html" if form.get("where") == "reader" else "partials/news_save_button.html"
        return render(request, tpl, alerts=[], n=row)

    @router.post("/news/{item_id:int}/note", response_class=HTMLResponse)
    async def save_note(request: Request, item_id: int) -> HTMLResponse:
        ctx = get_ctx(request)
        form = await request.form()
        row = await run_in_threadpool(set_note, ctx, item_id, str(form.get("note") or ""))
        return render(request, "partials/news_save_reader.html", alerts=[], n=row, note_saved=True)

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

        def work() -> Response:
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

        return await run_in_threadpool(work)

    @router.post("/news/source")
    async def source_action(request: Request) -> Response:
        ctx = get_ctx(request)
        svc = news_service(ctx)
        form = await request.form()

        def work() -> Response:
            sid = str(form.get("id") or "")
            action = form.get("action")
            if action in ("activate", "deactivate"):
                svc.cfg.set_source(sid, active=action == "activate")
                if action == "activate":
                    ctx.db.x("UPDATE source_status SET consecutive_failures=0, next_allowed=NULL WHERE source_id=?",
                             (sid,))
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

        return await run_in_threadpool(work)

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
