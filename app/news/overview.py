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
from app.news.ai import AiGateway
from app.news.config import ai_off_ids
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


RESERVE = 2  # so viele Plätze gehören im Normalbetrieb den neuesten noch nicht analysierten Meldungen


def _asset_names(ctx: AppContext, ids: list[int]) -> dict[int, list[str]]:
    pf = ctx.portfolio()
    if not ids or pf is None:
        return {}
    ph = ",".join("?" * len(ids))
    out: dict[int, list[str]] = {}
    for r in ctx.db.q(f"SELECT item_id, asset_id FROM news_asset WHERE item_id IN ({ph}) ORDER BY score DESC", ids):
        names = out.setdefault(r["item_id"], [])
        if len(names) < 3:
            names.append(pf.asset(r["asset_id"]).name)
    return out


def _recent(ctx: AppContext, hours: int, limit: int = 60) -> list[dict[str, Any]]:
    """Relevante Meldungen der letzten ``hours`` Stunden, neueste zuerst – mit Analyse, falls vorhanden."""
    min_rel = float(ctx.settings.get("news.min_relevance", 0.2))
    since = iso(datetime.now(UTC) - timedelta(hours=hours))
    rows = ctx.db.q(
        """SELECT n.id, n.title, n.summary AS teaser, n.source_name, n.source_id, n.published_at, n.dup_count,
                  a.summary AS asummary, a.sentiment, a.importance, a.category
           FROM news_item n LEFT JOIN news_analysis a ON a.item_id=n.id AND a.summary IS NOT NULL
           WHERE n.kind='article' AND n.hidden_reason IS NULL AND n.relevance>=? AND n.published_at>=?
           ORDER BY n.published_at DESC LIMIT ?""", (min_rel, since, limit))
    names = _asset_names(ctx, [r["id"] for r in rows])
    off = ai_off_ids(ctx.config.sources_path)
    out = []
    for r in rows:
        ai = r["asummary"] is not None
        text = r["asummary"] if ai else r["teaser"] or ""
        out.append({"id": r["id"], "title": r["title"], "summary": _teaser(text, 300),
                    "sentiment": r["sentiment"] if ai else None, "importance": r["importance"] if ai else None,
                    "assets": names.get(r["id"], []), "reports": 1 + int(r["dup_count"] or 0),
                    "sources": [r["source_name"]] if r["source_name"] else [], "published_at": r["published_at"],
                    "category": r["category"] if ai else None, "ai": ai, "noai": r["source_id"] in off})
    return out


def top_news(ctx: AppContext, n: int = 5, hours: int = 24) -> dict[str, Any]:
    """Zeilen für die Startseite.

    * **Normalbetrieb:** die wichtigsten analysierten Ereignisse; ``RESERVE`` Plätze gehören den neuesten noch nicht
      analysierten Meldungen („KI ausstehend“), damit nichts Aktuelles untergeht.
    * **KI gestört** (aus, pausiert, letzter Aufruf fehlgeschlagen): die neuesten Meldungen nach Aktualität, vorhandene
      Analysen nur als Zusatz – plus Hinweis mit Grund (``degraded``/``reason``).
    """
    healthy, reason = AiGateway(ctx).health()
    entries = list(ReportService(ctx).entries(hours=hours).values())
    recent = _recent(ctx, hours)
    if not healthy:
        rows = recent[:n]
        mode = "recency"
    else:
        entries.sort(key=lambda e: -e["_rank"])
        strong = [e for e in entries if e["bedeutung"] >= MIN_IMPORTANCE]
        ranked = strong + [e for e in entries if e not in strong]
        known = {e["id"] for e in entries}
        raw = [r for r in recent if not r["ai"] and not r["noai"] and r["id"] not in known]
        reserve = min(RESERVE, len(raw), n)
        picked = ranked[:n - reserve]
        rows = [{
            "id": e["id"], "title": e["titel"], "summary": _teaser(e["zusammenfassung"], 300),
            "sentiment": e["stimmung"], "importance": e["bedeutung"], "assets": e["bezug_assets"],
            "reports": e["anzahl_berichte"], "sources": e["quellen"], "published_at": e["veroeffentlicht"],
            "category": e["kategorie"], "ai": True, "noai": False} for e in picked]
        have = {r["id"] for r in rows}
        rows += [r for r in raw if r["id"] not in have][:n - len(rows)]
        mode = "ki"
    rep = ReportService(ctx).latest()
    report = None
    if rep is not None:
        created = parse_iso(rep["created_at"])
        if created and datetime.now(UTC) - created < timedelta(hours=36):
            report = rep
    return {"report": report, "overview": _teaser(report["content"]["ueberblick"]) if report else "",
            "rows": rows, "analyzed": len(entries), "degraded": not healthy, "reason": reason, "mode": mode}


__all__ = ["top_news"]
