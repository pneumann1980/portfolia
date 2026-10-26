"""Überwachung der News-Pipeline: eine Zeile je Stufe (Import, Artikeltexte, KI-Analyse, Gruppierung, Tagesbericht),
Balken nur für die beiden Stufen mit Warteschlange (``app.news.queue``), dazu wer gerade läuft (automatisch/manuell),
Tageszahlen und was fehlschlug."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.context import AppContext
from app.news import queue, wave
from app.news.ai import AiGateway
from app.news.analysis import AnalysisService
from app.news.enrich import EnrichService
from app.news.eta import enrich_eta
from app.util.timeutil import iso, now_local

JOBS = ("news_fetch", "news_enrich", "news_analyze", "news_group", "news_report")
STAGE_JOB = {"fetch": "news_fetch", "enrich": "news_enrich", "analyze": "news_analyze", "group": "news_group",
             "report": "news_report"}
_MANUAL_BADGE = {"blocked": ("warn", "wartet auf Anbieter/Limit"), "error": ("crit", "Fehler (manuell)")}


def _badges(jobs: dict[str, Any], manual: dict[str, Any] | None) -> dict[str, tuple[str, str] | None]:
    """Wer arbeitet gerade an der Stufe? Manueller Lauf vor Intervall-Job, danach Ergebnis des letzten manuellen
    Laufs (wartet auf Anbieter, Fehler)."""
    out: dict[str, tuple[str, str] | None] = {}
    running = bool(manual and manual.get("status") == "running")
    for key, job in STAGE_JOB.items():
        m = (manual or {}).get("stages", {}).get(key, {})
        if running and manual.get("stage") == key:
            out[key] = ("info", "läuft (manuell)")
        elif (jobs.get(job) or {}).get("running"):
            out[key] = ("info", "läuft (automatisch)")
        elif running and m.get("status") == "waiting":
            out[key] = ("", "angestoßen")
        elif manual and m.get("status") in _MANUAL_BADGE:
            out[key] = _MANUAL_BADGE[m["status"]]
        else:
            out[key] = None
    return out


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
    enrich_ready = len(enrich.candidates(100_000))  # jetzt abrufbar (Quelle nicht pausiert, Abruf fällig)
    enrich_paused = len(enrich.candidates(100_000, include_paused=True)) - enrich_ready  # Quelle pausiert
    enrich_retry = enrich.retry_planned_count()  # fehlgeschlagen, Wiederholung später
    analysis = AnalysisService(ctx, gw)
    guard = analysis.pressure()  # Rückstau-Lage der Analyse (frisch/älter, Schutz aktiv?)
    analyze_backlog = guard["total"]
    per_run = int(ctx.settings.get("news.enrich_per_run", 40))
    since3 = iso(datetime.now(UTC) - timedelta(hours=3))
    inflow = n("SELECT COUNT(*) FROM news_item WHERE kind='article' AND hidden_reason IS NULL AND fetched_at>=?",
               since24) / 24
    eta = {
        "enrich": enrich_eta(
            enrich_ready, per_run=per_run, inflow_per_hour=inflow,
            observed_done_3h=n("SELECT COUNT(*) FROM news_content WHERE fetched_at>=? AND status!='pending'", since3)),
        "analyze": guard["eta"],
    }
    if guard["active"]:
        bottleneck = f"KI-Analyse: {guard['reason']}"
    elif analyze_backlog and ai.get("paused_until"):
        bottleneck = "KI-Kontingent (Anbieter pausiert, Analyse wartet)"
    elif analyze_backlog and not ai.get("enabled"):
        bottleneck = "KI ausgeschaltet – Analysen warten"
    elif enrich_ready:
        bottleneck = f"Abruf-Takt: {per_run} Artikeltexte je Lauf, alle 15 min (Wartezeit je Quelle 2 s)"
    elif analyze_backlog:
        bottleneck = "Analyse-Takt: läuft in Runden, alle 10 min"
    elif enrich_paused or enrich_retry:
        bottleneck = (f"keiner – alles Abrufbare abgearbeitet ({enrich_paused} Artikel pausierter Quellen und "
                      f"{enrich_retry} Wiederholungen später)")
    else:
        bottleneck = "keiner – alles abgearbeitet"
    jobs = ctx.job_status()
    manual = wave.load(ctx)
    badges = _badges(jobs, manual)
    fetch_res = (jobs.get("news_fetch") or {}).get("progress") or {}
    next_runs = ctx.scheduler.next_runs() if ctx.scheduler is not None else {}
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
        "bars": {"enrich": queue.bar(ctx, "enrich", enrich_ready),
                 "analyze": queue.bar(ctx, "analyze", analyze_backlog)},
        "eta": eta,
        "backlog": {"enrich": enrich_ready, "enrich_paused": enrich_paused, "enrich_retry": enrich_retry,
                    "analyze": analyze_backlog,
                    "days": int(ctx.settings.get("news.enrich_days", 3)), "per_run": per_run, "bottleneck": bottleneck,
                    "paused_hosts": sorted(((h, u) for h, u in paused.items()), key=lambda x: x[1])},
        "analysis_failed": n("SELECT COUNT(*) FROM news_analysis WHERE summary IS NULL AND analyzed_at>=?", day),
        "events": n("SELECT COUNT(*) FROM news_event WHERE last_seen>=?", since24),
        "events_multi": n("SELECT COUNT(*) FROM news_event WHERE last_seen>=? AND n_sources>=2", since24),
        "analyze_guard": {"active": guard["active"], "reason": guard["reason"], "fresh": guard["fresh"],
                          "older": guard["older"], "age_h": int(guard["age_h"]),
                          "quota_note": guard["eta"].get("quota_note", "")},
        "ai": ai, "last_report": dict(last_report) if last_report else None, "errors": errors[:8],
        "badges": badges, "manual": manual,
        "running": any(b is not None and b[1].startswith(("läuft", "angestoßen")) for b in badges.values()),
        "fetch": {"last_end": (jobs.get("news_fetch") or {}).get("last_end"), "new": fetch_res.get("new"),
                  "errors": len(fetch_res.get("errors") or []), "next": next_runs.get("news_fetch")},
        "group_last": (jobs.get("news_group") or {}).get("last_end"),
        "jobs": {j: {k: jobs[j].get(k) for k in ("last_start", "last_end", "last_ok", "running")}
                 for j in JOBS if j in jobs},
        "results": {j: jobs[j]["progress"] for j in JOBS if j in jobs and jobs[j].get("progress")},
    }


__all__ = ["JOBS", "pipeline_status"]
