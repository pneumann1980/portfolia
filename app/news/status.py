"""Überwachung der News-Pipeline: heute importiert, extrahiert, analysiert, gruppiert – und was fehlschlug."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.context import AppContext
from app.news.ai import AiGateway
from app.news.analysis import AnalysisService
from app.news.enrich import EnrichService
from app.util.timeutil import iso, now_local

JOBS = ("news_fetch", "news_enrich", "news_analyze", "news_group", "news_report")


def pipeline_status(ctx: AppContext) -> dict[str, Any]:
    db = ctx.db
    local_start = now_local().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    day = iso(local_start)
    since24 = iso(datetime.now(UTC) - timedelta(hours=24))

    def n(sql: str, *p: Any) -> int:
        return int(db.scalar(sql, p, default=0) or 0)

    content = {r["status"]: int(r["n"]) for r in db.q(
        "SELECT status, COUNT(*) AS n FROM news_content WHERE fetched_at>=? GROUP BY status", (day,))}
    reasons = db.q("SELECT reason, COUNT(*) AS n FROM news_content WHERE fetched_at>=? AND reason IS NOT NULL "
                   "GROUP BY reason ORDER BY n DESC LIMIT 6", (day,))
    gw = AiGateway(ctx)
    ai = gw.status()
    enrich = EnrichService(ctx)
    paused = enrich.paused_hosts()
    enrich_total = len(enrich.candidates(100_000, include_paused=True))
    enrich_ready = len(enrich.candidates(100_000))
    analyze_backlog = AnalysisService(ctx).backlog()
    per_run = int(ctx.settings.get("news.enrich_per_run", 40))
    if analyze_backlog and ai.get("paused_until"):
        bottleneck = "KI-Kontingent (Anbieter pausiert, Analyse wartet)"
    elif analyze_backlog and not ai.get("enabled"):
        bottleneck = "KI ausgeschaltet – Analysen warten"
    elif enrich_ready:
        bottleneck = f"Abruf-Takt: {per_run} Artikeltexte je Lauf, alle 15 min (Wartezeit je Quelle 2 s)"
    elif analyze_backlog:
        bottleneck = "Analyse-Takt: läuft in Runden, alle 10 min"
    else:
        bottleneck = "keiner – alles abgearbeitet"
    jobs = ctx.job_status()
    errors = []
    for j in JOBS:
        st = jobs.get(j)
        if st and st.get("last_error"):
            errors.append({"job": j, "text": st["last_error"], "at": st.get("last_end")})
    for key, label in (("news.report.last_error", "Tagesbericht"),):
        e = db.get_state(key)
        if isinstance(e, dict) and e.get("text"):
            errors.append({"job": label, "text": e["text"], "at": e.get("at")})
    if ai["last_error"]:
        errors.append({"job": "KI", "text": ai["last_error"], "at": None})
    last_report = db.q1("SELECT id, day, version, created_at, provider, model FROM news_report "
                        "ORDER BY created_at DESC LIMIT 1")
    return {
        "imported": n("SELECT COUNT(*) FROM news_item WHERE kind='article' AND fetched_at>=?", day),
        "content": {k: content.get(k, 0) for k in ("success", "partial", "blocked", "failed", "pending")},
        "content_reasons": [(r["reason"], int(r["n"])) for r in reasons],
        "with_content_total": n("SELECT COUNT(*) FROM news_content WHERE status IN ('success','partial')"),
        "analyzed": n("SELECT COUNT(*) FROM news_analysis WHERE summary IS NOT NULL AND analyzed_at>=?", day),
        "backlog": {"enrich": enrich_ready, "enrich_paused": enrich_total - enrich_ready, "analyze": analyze_backlog,
                    "days": int(ctx.settings.get("news.enrich_days", 3)), "per_run": per_run, "bottleneck": bottleneck,
                    "paused_hosts": sorted(((h, u) for h, u in paused.items()), key=lambda x: x[1])},
        "analysis_failed": n("SELECT COUNT(*) FROM news_analysis WHERE summary IS NULL AND analyzed_at>=?", day),
        "events": n("SELECT COUNT(*) FROM news_event WHERE last_seen>=?", since24),
        "events_multi": n("SELECT COUNT(*) FROM news_event WHERE last_seen>=? AND n_sources>=2", since24),
        "ai": ai, "last_report": dict(last_report) if last_report else None, "errors": errors[:8],
        "jobs": {j: {k: jobs[j].get(k) for k in ("last_start", "last_end", "last_ok", "running")}
                 for j in JOBS if j in jobs},
        "results": {j: jobs[j]["progress"] for j in JOBS if j in jobs and jobs[j].get("progress")},
    }


__all__ = ["JOBS", "pipeline_status"]
