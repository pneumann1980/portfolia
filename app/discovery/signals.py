"""Discovery-Signale: aus der vorhandenen News-Analyse, aus KI-Themen (Wertschöpfungsketten) und aus Fundamentaldaten.

* **Nachricht:** Die News-KI nennt je Artikel „betroffene“ Werte (``news_analysis.result_json``). Aktien darunter, die
  sich eindeutig einem börsennotierten Emittenten zuordnen lassen, werden Kandidaten – auch ohne Bezug zum Portfolio.
  Je Unternehmen und **Ereignis** (``news_event``) entsteht höchstens ein Signal (Dublettenschutz); mehrere Artikel
  desselben Ereignisses erhöhen nicht die Zahl der Signale, nur ``n_sources`` (verschiedene Quellen, gekappt).
* **Thema:** Die KI leitet aus belegten Ereignissen Themen und Unternehmen entlang der Wertschöpfungskette ab; jeder
  genannte Ticker muss im SEC-Verzeichnis existieren und zum genannten Namen passen.
* **Fundamental:** deterministische Frühsignale aus SEC-Daten (Wachstumsbeschleunigung, steigende Bruttomarge,
  Free Cashflow erstmals positiv).

Stärke (0–1) einer Nachricht = Bedeutung laut News-Analyse × min(1; 0,4 + 0,15 × verschiedene Quellen), höchstens
4 Quellen zählen. Ein einzelner Artikel ergibt also höchstens 0,55 × Bedeutung.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.discovery.fundamentals import Fundamentals
from app.discovery.text import P
from app.util.timeutil import iso

log = logging.getLogger(__name__)

MAX_SOURCES = 4
FRESH_DAYS = 90


def fp(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:32]


def strength(importance: float, n_sources: int) -> float:
    return round(max(0.0, min(1.0, importance)) * min(1.0, 0.4 + 0.15 * min(n_sources, MAX_SOURCES)), 2)


def news_mentions(db: Any, days: int) -> list[dict[str, Any]]:
    """Aktien-Nennungen aus analysierten Meldungen der letzten ``days`` Tage, je Ereignis zusammengefasst."""
    since = iso(datetime.now(UTC) - timedelta(days=days))
    rows = db.q(
        "SELECT n.id, n.title, n.url, n.source_name, n.published_at, a.result_json, a.importance, a.sentiment, "
        "ei.event_id FROM news_item n JOIN news_analysis a ON a.item_id = n.id "
        "LEFT JOIN news_event_item ei ON ei.item_id = n.id "
        "WHERE n.published_at >= ? AND n.hidden_reason IS NULL ORDER BY n.published_at DESC", (since,))
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        try:
            res = json.loads(r["result_json"] or "{}")
        except ValueError:
            continue
        ev = f"E{r['event_id']}" if r["event_id"] else f"I{r['id']}"
        for b in res.get("betroffene") or []:
            if not isinstance(b, dict) or b.get("art") != "aktie" or not b.get("name"):
                continue
            key = (str(b["name"]).strip().lower(), ev)
            g = groups.setdefault(key, {"name": str(b["name"]).strip(), "event": ev, "items": [], "sources": set(),
                                        "importance": 0.0, "direction": b.get("richtung") or "unklar",
                                        "title": res.get("ereignis") or r["title"], "published": r["published_at"],
                                        "url": r["url"], "source_name": r["source_name"],
                                        "summary": res.get("zusammenfassung") or ""})
            g["items"].append(int(r["id"]))
            if r["source_name"]:
                g["sources"].add(r["source_name"])
            g["importance"] = max(g["importance"], float(r["importance"] or 0.0))
    for g in groups.values():
        g["n_sources"] = max(1, len(g["sources"]))
        g["sources"] = sorted(g["sources"])
    return list(groups.values())


def recent_events(db: Any, days: int = 7, limit: int = 25) -> list[dict[str, Any]]:
    """Wichtige Ereignisse (Unternehmen/Branche) als Eingabe der Themen-Discovery – nur öffentliche Meldungstexte."""
    since = iso(datetime.now(UTC) - timedelta(days=days))
    rows = db.q(
        "SELECT e.id, e.title, e.last_seen, MAX(a.importance) AS imp, "
        "(SELECT a2.summary FROM news_analysis a2 WHERE a2.item_id = e.main_item) AS summary "
        "FROM news_event e JOIN news_event_item ei ON ei.event_id = e.id "
        "JOIN news_analysis a ON a.item_id = ei.item_id "
        "WHERE e.last_seen >= ? AND a.category IN ('unternehmen', 'branche', 'regulierung') "
        "GROUP BY e.id HAVING imp >= 0.5 ORDER BY imp DESC, e.n_sources DESC LIMIT ?", (since, limit))
    return [{"id": f"E{r['id']}", "title": r["title"], "text": r["summary"] or "", "date": str(r["last_seen"])[:10]}
            for r in rows]


def add_signal(db: Any, company_id: int, *, kind: str, title: str, origin: str, key: str, detail: str = "",
               ref: str = "", url: str = "", source_name: str = "", n_sources: int = 1, direction: str = "unklar",
               strength_: float = 0.0, published_at: str | None = None) -> bool:
    """Signal speichern; ``False``, wenn es (Unternehmen + ``key``) schon gibt."""
    cur = db.x("INSERT OR IGNORE INTO disc_signal(company_id, kind, title, detail, origin, ref, url, source_name, "
               "n_sources, direction, strength, published_at, observed_at, fp) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (company_id, kind, title[:200], detail[:1200], origin, ref[:200], url[:600], source_name[:120],
                n_sources, direction, strength_, published_at, iso(datetime.now(UTC)), fp(company_id, key)))
    return bool(cur.rowcount)


def fundamental_signals(fu: Fundamentals) -> list[dict[str, Any]]:
    """Deterministische Frühsignale aus den zwei jüngsten Geschäftsjahren."""
    out: list[dict[str, Any]] = []
    if len(fu.years) < 3:
        return out
    y, p = fu.years[-1], fu.years[-2]
    g1, g0 = fu.get("revenue_growth", y).v, fu.get("revenue_growth", p).v
    if g1 is not None and g0 is not None and g1 >= 0.15 and g1 - g0 >= 0.10:
        out.append({"key": f"accel:{y}", "title": f"Umsatzwachstum beschleunigt: {P(g0, 0)} → {P(g1, 0)}",
                    "detail": f"Geschäftsjahr bis {y} ggü. Vorjahr (SEC-Daten, berechnet).", "period": y,
                    "accn": fu.get("revenue", y).accn})
    m1, m0 = fu.get("gross_margin", y).v, fu.get("gross_margin", p).v
    if m1 is not None and m0 is not None and m1 - m0 >= 0.03:
        out.append({"key": f"gm:{y}", "title": f"Bruttomarge gestiegen: {P(m0)} → {P(m1)}",
                    "detail": f"Geschäftsjahr bis {y} (SEC-Daten, berechnet).", "period": y,
                    "accn": fu.get("revenue", y).accn})
    f1, f0 = fu.get("fcf", y).v, fu.get("fcf", p).v
    if f1 is not None and f0 is not None and f1 > 0 >= f0:
        out.append({"key": f"fcf:{y}", "title": "Free Cashflow erstmals positiv",
                    "detail": f"Geschäftsjahr bis {y}: von negativ auf positiv (SEC-Daten, berechnet).", "period": y,
                    "accn": fu.get("ocf", y).accn})
    return out
