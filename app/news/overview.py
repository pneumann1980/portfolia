"""Startseite: Top-News mit KI-Auswertung (Ereignisse statt Einzelartikel, nach Bedeutung sortiert).

Grundlage sind die gespeicherten Analysen der letzten 24 Stunden (je Ereignis die Hauptmeldung). Fehlen Analysen,
werden freie Plätze mit den bisher üblichen relevanten Meldungen aufgefüllt (gekennzeichnet „KI ausstehend“);
ohne jede Analyse bleibt die Startseite bei der bisherigen Liste.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from app.context import AppContext
from app.news.report import ReportService
from app.util.timeutil import iso, parse_iso

MIN_IMPORTANCE = 0.25
TEASER_CHARS = 320


def _teaser(text: str, limit: int = TEASER_CHARS) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[:end + 1] if end > limit * 0.5 else cut.rsplit(" ", 1)[0] + "…")


def top_news(ctx: AppContext, n: int = 5, hours: int = 24) -> dict[str, Any]:
    """``{"report": Bericht|None, "overview": Kurztext, "rows": [...], "analyzed": Anzahl}``."""
    entries = list(ReportService(ctx).entries(hours=hours).values())
    entries.sort(key=lambda e: -e["_rank"])
    strong = [e for e in entries if e["bedeutung"] >= MIN_IMPORTANCE]
    picked = (strong + [e for e in entries if e not in strong])[:n]
    items: list[dict[str, Any]] = [{
        "id": e["id"], "title": e["titel"], "summary": _teaser(e["zusammenfassung"], 300), "sentiment": e["stimmung"],
        "importance": e["bedeutung"], "assets": e["bezug_assets"], "reports": e["anzahl_berichte"],
        "sources": e["quellen"], "published_at": e["veroeffentlicht"], "category": e["kategorie"], "ai": True,
    } for e in picked]
    if len(items) < n:  # freie Plätze: bisherige Rohliste, ohne bereits gezeigte Meldungen
        have = {i["id"] for i in items}
        min_rel = float(ctx.settings.get("news.min_relevance", 0.2))
        since = iso(datetime.now(UTC) - timedelta(hours=hours))
        for r in ctx.db.q(
                """SELECT n.id, n.title, n.summary, n.source_name, n.published_at, n.dup_count
                   FROM news_item n LEFT JOIN news_analysis a ON a.item_id=n.id AND a.summary IS NOT NULL
                   WHERE n.kind='article' AND n.hidden_reason IS NULL AND n.relevance>=? AND n.published_at>=?
                     AND a.item_id IS NULL ORDER BY n.relevance DESC, n.published_at DESC LIMIT 40""",
                (min_rel, since)):
            if r["id"] in have:
                continue
            items.append({"id": r["id"], "title": r["title"], "summary": r["summary"] or "", "sentiment": None,
                          "importance": None, "assets": [], "reports": 1 + int(r["dup_count"] or 0),
                          "sources": [r["source_name"]] if r["source_name"] else [],
                          "published_at": r["published_at"], "category": None, "ai": False})
            if len(items) >= n:
                break
    rep = ReportService(ctx).latest()
    report = None
    if rep is not None:
        created = parse_iso(rep["created_at"])
        if created and datetime.now(UTC) - created < timedelta(hours=36):
            report = rep
    return {"report": report, "overview": _teaser(report["content"]["ueberblick"]) if report else "",
            "rows": items, "analyzed": len(entries)}


__all__ = ["top_news"]
