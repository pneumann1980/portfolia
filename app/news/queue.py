"""Warteschlange je Pipeline-Stufe als Zyklus: „n von m“ für alles, was seit Beginn des Zyklus anstand.

Ein Zyklus beginnt, wenn in der Stufe (Artikeltexte, KI-Analyse) Arbeit wartet und kein Zyklus offen ist, und endet,
sobald nichts mehr wartet. Erledigt = seit Zyklusbeginn bearbeitete Meldungen im Fenster der Stufe (Abrufversuch bzw.
Analyse); gesamt = erledigt + jetzt wartend. Ist die Warteschlange leer, steht „0 von 0“ mit Zeitpunkt und Umfang des
letzten Zyklus – der alte Vorgang fällt heraus. Automatische und manuelle Läufe arbeiten dieselbe Warteschlange ab.

Den Zustand schreiben nur die Jobs (``observe`` vor und nach jedem Lauf); die Statusseite liest nur (``bar``).
Annahmen/Grenzen: Bei Dauerzufluss endet ein Zyklus nie – der Beginn wird deshalb auf höchstens ``CYCLE_MAX_H``
zurück begrenzt. Meldungen, die das Fenster verlassen, verlassen die Warteschlange, ohne als erledigt zu zählen.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.context import AppContext
from app.util.timeutil import iso, parse_iso

CYCLE_MAX_H = 24
STAGES = ("enrich", "analyze")


def _key(stage: str) -> str:
    return f"news.queue.{stage}"


def _window_since(ctx: AppContext, stage: str) -> str:
    now = datetime.now(UTC)
    if stage == "enrich":
        return iso(now - timedelta(days=int(ctx.settings.get("news.enrich_days", 3))))
    from app.news.analysis import WINDOW_H

    return iso(now - timedelta(hours=int(ctx.settings.get("news.analysis_hours", WINDOW_H))))


def done_since(ctx: AppContext, stage: str, since: str) -> int:
    """Seit ``since`` bearbeitete Meldungen im Fenster der Stufe (jede Meldung einmal, auch nach Wiederholung)."""
    win = _window_since(ctx, stage)
    if stage == "enrich":
        sql = """SELECT COUNT(*) FROM news_content c JOIN news_item n ON n.id=c.item_id
                 WHERE c.fetched_at>=? AND n.kind='article' AND n.hidden_reason IS NULL AND n.published_at>=?"""
    else:
        sql = """SELECT COUNT(*) FROM news_analysis a JOIN news_item n ON n.id=a.item_id
                 WHERE a.analyzed_at>=? AND n.kind='article' AND n.hidden_reason IS NULL AND n.published_at>=?"""
    return int(ctx.db.scalar(sql, (since, win), default=0) or 0)


def _open_start(st: Any) -> datetime | None:
    if isinstance(st, dict) and st.get("start") and not st.get("closed_at"):
        return parse_iso(st["start"])
    return None


def _capped(start: datetime) -> datetime:
    return max(start, datetime.now(UTC) - timedelta(hours=CYCLE_MAX_H))


def observe(ctx: AppContext, stage: str, waiting: int) -> None:
    """Zyklus öffnen (Arbeit wartet) oder schließen (nichts wartet) – von Jobs vor und nach jedem Lauf aufgerufen."""
    st = ctx.db.get_state(_key(stage))
    start = _open_start(st)
    now = datetime.now(UTC)
    if waiting > 0 and start is None:
        # Zeitstempel sind sekundengenau: ein neuer Zyklus beginnt frühestens eine Sekunde nach dem Ende des alten,
        # damit dessen letzte Meldungen nicht doppelt zählen
        closed = parse_iso(st.get("closed_at")) if isinstance(st, dict) and st.get("closed_at") else None
        begin = max(now, closed + timedelta(seconds=1)) if closed else now
        ctx.db.set_state(_key(stage), {"start": iso(begin), "closed_at": None})
    elif waiting == 0 and start is not None:
        ctx.db.set_state(_key(stage), {"start": iso(start), "closed_at": iso(now),
                                       "last_done": done_since(ctx, stage, iso(_capped(start)))})


def bar(ctx: AppContext, stage: str, waiting: int) -> dict[str, Any]:
    """Balken der Stufe (nur lesend). Ohne offenen Zyklus beginnt einer gedanklich jetzt (erledigt 0)."""
    st = ctx.db.get_state(_key(stage))
    start = _open_start(st)
    if waiting > 0:
        done = done_since(ctx, stage, iso(_capped(start))) if start is not None else 0
        total = done + waiting
        return {"done": done, "waiting": waiting, "total": total, "pct": int(100 * done / total),
                "since": iso(_capped(start)) if start is not None else None, "finished_at": None, "last_done": None}
    if start is not None:  # leer, vom nächsten Job noch nicht geschlossen
        last, finished = done_since(ctx, stage, iso(_capped(start))), None
    else:
        last = int(st.get("last_done") or 0) if isinstance(st, dict) else None
        finished = st.get("closed_at") if isinstance(st, dict) else None
    return {"done": 0, "waiting": 0, "total": 0, "pct": 100, "since": None, "finished_at": finished,
            "last_done": last}


def waiting_now(ctx: AppContext, enrich: Any = None, analysis: Any = None) -> dict[str, int]:
    """Jetzt wartend je Stufe: abrufbare Artikeltexte (ohne pausierte Quellen/spätere Wiederholungen) und Analysen."""
    from app.news.analysis import AnalysisService
    from app.news.enrich import EnrichService

    enrich = enrich or EnrichService(ctx)
    analysis = analysis or AnalysisService(ctx)
    return {"enrich": len(enrich.candidates(100_000)), "analyze": analysis.backlog()}


def observe_all(ctx: AppContext, enrich: Any = None, analysis: Any = None) -> dict[str, int]:
    waiting = waiting_now(ctx, enrich, analysis)
    for stage in STAGES:
        observe(ctx, stage, waiting[stage])
    return waiting


__all__ = ["CYCLE_MAX_H", "STAGES", "bar", "done_since", "observe", "observe_all", "waiting_now"]
