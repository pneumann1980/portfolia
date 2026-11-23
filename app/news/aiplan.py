"""KI je Bereich (0.119): welche Meldungen die KI automatisch analysiert und welche nur auf Knopfdruck.

**Artikel** (Einstellung ``news.ai_articles``)

* ``top`` (Standard): Automatisch analysiert wird nur, was zu den wichtigsten Meldungen gehört – ausgewählt **ohne KI**
  aus der lokalen Rangfolge (Relevanz, Zahl der Berichte zum selben Thema, Gewicht der Quelle, Aktualität) über die
  letzten 24 Stunden: die ersten ``news.ai_top_per_day`` Meldungen dieser Rangfolge. Zusätzlich zwei Quoten, damit
  Stimmungsbarometer und Nachrichtensignale tragen: je gehaltener Position die zwei relevantesten Meldungen
  (Barometer je Position braucht mindestens zwei) und je Watchlist-/Vorschlags-Unternehmen die relevanteste Meldung,
  die es nennt (lokaler Namensabgleich). Feste Tagesobergrenze ``news.ai_top_per_day`` (lokaler Kalendertag); in den
  90 Minuten vor dem Tagesbericht dürfen bis zu zehn weitere Meldungen der Rangfolge nachrücken, damit der Bericht
  vollständig ist.
* ``all``: alle Meldungen im Analysefenster (Verhalten bis 0.118).
* ``manual``: nur per Knopf.

**Community** (``news.ai_community``): ``all`` alle Beiträge der Telegram-Quellen mit KI, ``manual`` nur per Knopf,
``off`` nie. Quellen mit ``ai: false`` in ``sources.yaml`` bleiben unabhängig davon ausgenommen.

**Knopf „Zusammenfassen“** (``news_item.ai_requested_at``): immer, solange KI aktiv ist und die Quelle KI zulässt.

Jede Analyse speichert ihre Herkunft (``news_analysis.trigger``): ``top``, ``quote``, ``alle``, ``knopf``,
``community``. Die Tagesobergrenze zählt ``top`` und ``quote``.
"""

from __future__ import annotations

import contextlib
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from app.news.config import ai_off_ids, not_in_clause, telegram_ids
from app.util.timeutil import iso, now_local, parse_iso

ARTICLE_MODES = {"top": "Top-News automatisch, übrige per Knopf", "all": "Alle automatisch",
                 "manual": "Nur per Knopf"}
COMMUNITY_MODES = {"all": "Alle automatisch", "manual": "Nur per Knopf", "off": "Aus"}
VIDEO_MODES = {"favorites": "Lieblingskanäle automatisch, übrige per Knopf", "manual": "Nur per Knopf"}
HOLDING_QUOTA = 2  # je gehaltener Position so viele Meldungen (Stimmungsbarometer je Position braucht ≥ 2)
WATCH_QUOTA = 12  # höchstens so viele Meldungen über Watchlist-/Vorschlags-Unternehmen je 24 h
REPORT_EXTRA = 10  # so viele Meldungen dürfen kurz vor dem Tagesbericht über die Tagesobergrenze nachrücken
REPORT_PREP_MIN = 90
WINDOW_H = 24
log = logging.getLogger(__name__)
_STOP = {"inc", "corp", "corporation", "co", "ltd", "plc", "ag", "se", "sa", "nv", "holdings", "holding", "group",
         "the", "class", "company", "technologies", "technology", "international", "limited", "gmbh", "lp"}


def _pick(ctx: Any, key: str, allowed: dict[str, str], default: str) -> str:
    v = str(ctx.settings.get(key, default) or default)
    return v if v in allowed else default


def article_mode(ctx: Any) -> str:
    return _pick(ctx, "news.ai_articles", ARTICLE_MODES, "top")


def community_mode(ctx: Any) -> str:
    return _pick(ctx, "news.ai_community", COMMUNITY_MODES, "all")


def video_mode(ctx: Any) -> str:
    return _pick(ctx, "news.ai_videos", VIDEO_MODES, "favorites")


def top_per_day(ctx: Any) -> int:
    try:
        return max(5, min(300, int(ctx.settings.get("news.ai_top_per_day", 50) or 50)))
    except (TypeError, ValueError):
        return 50


def batch_wait(ctx: Any) -> timedelta:
    try:
        return timedelta(minutes=max(0, min(240, int(ctx.settings.get("news.ai_batch_wait_min", 30) or 0))))
    except (TypeError, ValueError):
        return timedelta(minutes=30)


def community_ids(ctx: Any) -> frozenset[str]:
    return telegram_ids(ctx.config.sources_path)


def local_priority(relevance: Any, dup: Any, weight: Any, age_h: float) -> float:
    """Lokale Rangfolge (wie die Analysewarteschlange): Relevanz, Mehrfachberichte, Quelle, Aktualität."""
    return (float(relevance or 0) + 0.06 * min(int(dup or 0), 6) + 0.1 * float(weight or 0)
            + 0.2 / (1.0 + age_h / 12.0))


def day_start() -> str:
    """Beginn des lokalen Kalendertags (UTC-ISO) – Bezug der Tagesobergrenze."""
    now = now_local()
    return str(iso(now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)))


def used_today(ctx: Any) -> int:
    return int(ctx.db.scalar("SELECT COUNT(*) FROM news_analysis WHERE trigger IN ('top','quote') AND analyzed_at>=?",
                             (day_start(),), default=0) or 0)


def report_soon(ctx: Any) -> bool:
    """Liegt der Tagesbericht in den nächsten ``REPORT_PREP_MIN`` Minuten (und ist er eingeschaltet)?"""
    if not ctx.settings.get("news.report", True):
        return False
    now = now_local()
    at = now.replace(hour=max(0, min(23, int(ctx.settings.get("news.report_hour", 22) or 0))),
                     minute=max(0, min(59, int(ctx.settings.get("news.report_minute", 15) or 0))), second=0,
                     microsecond=0)
    return timedelta(0) <= at - now <= timedelta(minutes=REPORT_PREP_MIN)


def allowance(ctx: Any) -> int:
    """Wie viele Top-/Quoten-Meldungen heute noch automatisch analysiert werden dürfen."""
    cap = top_per_day(ctx) + (REPORT_EXTRA if report_soon(ctx) else 0)
    return max(0, cap - used_today(ctx))


def _watch_terms(ctx: Any) -> dict[str, re.Pattern[str]]:
    """Watchlist-Titel und beobachtete/offene Vorschlags-Unternehmen → Suchmuster (Name ohne Rechtsform, Symbol ab
    drei Zeichen als ganzes Wort, Großschreibung)."""
    terms: dict[str, re.Pattern[str]] = {}

    def add(key: str, name: str, sym: str) -> None:
        words = [w for w in re.findall(r"[A-Za-zÄÖÜäöüß0-9&]+", name or "") if w.lower() not in _STOP]
        core = " ".join(words[:2]).strip()
        parts = []
        if len(core) >= 4:
            parts.append(re.escape(core))
        base = (sym or "").split(".")[0].split("-")[0]
        if len(base) >= 3 and base.isalpha():
            parts.append(r"(?-i:\b" + re.escape(base.upper()) + r"\b)")
        if parts:
            terms[key] = re.compile(r"\b(?:" + "|".join(parts) + r")\b", re.IGNORECASE)

    try:
        for r in ctx.db.q("SELECT DISTINCT name, symbol FROM watchlist_item WHERE quote_source='yahoo'"):
            add(f"w:{r['symbol'] or r['name']}", str(r["name"] or ""), str(r["symbol"] or ""))
    except Exception as e:  # Tabelle fehlt in sehr alten Datenständen
        log.debug("Watchlist für die Top-Auswahl nicht lesbar: %s", e)
    try:
        for r in ctx.db.q("SELECT id, name, ticker FROM disc_company WHERE status IN ('beobachten','neu') LIMIT 400"):
            add(f"c:{r['id']}", str(r["name"] or ""), str(r["ticker"] or ""))
    except Exception as e:
        log.debug("Vorschläge für die Top-Auswahl nicht lesbar: %s", e)
    return terms


def top_selection(ctx: Any) -> dict[int, str]:
    """Meldungs-ID → Grund (``top`` | ``quote``) der Artikel, die im Modus ``top`` automatisch analysiert werden dürfen
    (analysierte eingeschlossen – die Rangfolge gilt über alle Meldungen der letzten 24 Stunden)."""
    min_rel = float(ctx.settings.get("news.min_relevance", 0.2) or 0) * 0.5
    since = iso(datetime.now(UTC) - timedelta(hours=WINDOW_H))
    skip = ai_off_ids(ctx.config.sources_path) | community_ids(ctx)
    skip_sql, skip_args = not_in_clause("n.source_id", skip)
    rows = ctx.db.q(
        """SELECT n.id, n.title, n.summary, n.relevance, n.dup_count, n.source_weight, n.published_at
           FROM news_item n WHERE n.kind='article' AND n.hidden_reason IS NULL AND n.published_at >= ?
             AND n.relevance >= ?""" + skip_sql, (since, min_rel, *skip_args))
    now = datetime.now(UTC)
    prio: dict[int, float] = {}
    for r in rows:
        age_h = max(0.0, (now - (parse_iso(r["published_at"]) or now)).total_seconds() / 3600)
        prio[int(r["id"])] = local_priority(r["relevance"], r["dup_count"], r["source_weight"], age_h)
    ranked = sorted(prio, key=lambda i: -prio[i])
    out: dict[int, str] = dict.fromkeys(ranked[:top_per_day(ctx)], "top")
    if prio:  # Quote je gehaltener Position (news_asset enthält nur Positionen des Portfolios)
        ids = list(prio)
        per: dict[str, list[int]] = {}
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            ph = ",".join("?" * len(part))
            for a in ctx.db.q(f"SELECT item_id, asset_id FROM news_asset WHERE item_id IN ({ph})", part):
                per.setdefault(str(a["asset_id"]), []).append(int(a["item_id"]))
        for items in per.values():
            for i in sorted(items, key=lambda x: -prio[x])[:HOLDING_QUOTA]:
                out.setdefault(i, "quote")
        terms = _watch_terms(ctx)
        if terms:
            text = {int(r["id"]): f"{r['title']} {r['summary'] or ''}" for r in rows}
            added = 0
            for _key, rx in terms.items():
                hit = next((i for i in ranked if rx.search(text[i])), None)
                if hit is not None and hit not in out:
                    out[hit] = "quote"
                    added += 1
                    if added >= WATCH_QUOTA:
                        break
    return out


def migrate_report_time(ctx: Any) -> bool:
    """Einmalig (0.119): Tagesbericht vom bisherigen Standard 07:30 auf 22:15 (nach US-Börsenschluss) – nur wenn die
    gespeicherte Zeit noch der alte Standard ist; eine selbst gewählte Zeit bleibt."""
    if ctx.db.get_state("news.report_time_0119"):
        return False
    changed = False
    if int(ctx.settings.get("news.report_hour", 22)) == 7 and int(ctx.settings.get("news.report_minute", 15)) == 30:
        ctx.settings.set("news.report_hour", 22)
        ctx.settings.set("news.report_minute", 15)
        changed = True
        log.info("Tagesbericht: Uhrzeit einmalig von 07:30 auf 22:15 (nach US-Börsenschluss) umgestellt")
    ctx.db.set_state("news.report_time_0119", True)
    return changed


def plan(ctx: Any, raw: list[Any]) -> list[dict[str, Any]]:
    """Kandidaten der Analyse (``AnalysisService._raw_candidates``) auf das reduzieren, was laut Einstellung jetzt
    automatisch bzw. auf Anforderung analysiert wird. Setzt je Zeile ``trigger`` und ``task`` (``news``/``community``);
    angeforderte Meldungen (Knopf) kommen immer und zuerst."""
    tg = community_ids(ctx)
    amode, cmode = article_mode(ctx), community_mode(ctx)
    picks: dict[int, str] | None = None
    out: list[dict[str, Any]] = []
    auto: list[dict[str, Any]] = []
    for r in raw:
        community = r.get("source_id") in tg
        r["task"] = "community" if community else "news"
        if r.get("ai_requested_at"):
            if community and cmode == "off":
                continue
            r["trigger"] = "knopf"
            r["priority"] = float(r.get("priority") or 0) + 10.0  # Knopf zuerst
            out.append(r)
        elif community:
            if cmode == "all":
                r["trigger"] = "community"
                out.append(r)
        elif amode == "all":  # wie bis 0.118: jede Meldung im Analysefenster
            r["trigger"] = "alle"
            out.append(r)
        elif amode == "top":
            if picks is None:
                picks = top_selection(ctx)
            why = picks.get(int(r["id"]))
            if why:
                r["trigger"] = why
                auto.append(r)
    if auto:
        left = allowance(ctx)
        auto.sort(key=lambda x: -float(x.get("priority") or 0))
        out.extend(auto[:left])
    return out


AI_POLL = {"queued": "5s", "waiting": "60s"}


def item_states(ctx: Any, ids: list[int]) -> dict[int, dict[str, Any]]:
    """Stand der KI-Auswertung je Artikel für Karte und Leseansicht (Knopf „Zusammenfassen“):
    ``none`` (Knopf) | ``auto`` (folgt automatisch) | ``queued`` (angefordert) | ``waiting`` (Kontingent/Pause) |
    ``done`` | ``failed`` | ``off`` (KI aus, Quelle ohne KI oder Community aus)."""
    if not ids:
        return {}
    from app.news.ai import AiGateway

    gw = AiGateway(ctx)
    on = bool(ctx.settings.get("news.analysis", True)) and gw.enabled()
    off_src, tg = ai_off_ids(ctx.config.sources_path), community_ids(ctx)
    amode, cmode = article_mode(ctx), community_mode(ctx)
    picks: dict[int, str] | None = None
    blocked: dict[str, Any] = {}
    ph = ",".join("?" * len(ids))
    out: dict[int, dict[str, Any]] = {}
    for r in ctx.db.q(f"""SELECT n.id, n.source_id, n.ai_requested_at, n.relevance, a.item_id AS aid,
                                 a.summary AS asum FROM news_item n LEFT JOIN news_analysis a ON a.item_id=n.id
                          WHERE n.id IN ({ph}) AND n.kind='article'""", ids):
        community = r["source_id"] in tg
        task = "community" if community else "news"
        st, extra = "none", {}
        if r["asum"]:
            st, extra = "done", {"summary": r["asum"]}
        elif not on or r["source_id"] in off_src or (community and cmode == "off"):
            st = "off"
        elif r["aid"] and not r["ai_requested_at"]:
            st = "failed"
        elif r["ai_requested_at"]:
            if task not in blocked:
                blocked[task] = gw.blocked(task)
            b = blocked[task]
            st, extra = ("waiting", {"until": b["until"], "reason": b["reason"]}) if b else ("queued", {})
        elif (community and cmode == "all") or (not community and amode == "all"):
            st = "auto"
        elif not community and amode == "top":
            if picks is None:
                picks = top_selection(ctx)
            st = "auto" if int(r["id"]) in picks and allowance(ctx) > 0 else "none"
        out[int(r["id"])] = {"state": st, "poll": AI_POLL.get(st), **extra}
    return out


def request(ctx: Any, item_id: int) -> dict[str, Any]:
    """Knopf „Zusammenfassen“: Meldung vormerken (auch nach Fehlschlag erneut) und die Analyse anstoßen. Kurz
    nacheinander angeforderte Meldungen gehen gemeinsam in ein Paket (der Lauf startet nach 20 Sekunden)."""
    row = ctx.db.q1("SELECT id FROM news_item WHERE id=? AND kind='article'", (item_id,))
    if row is None:
        raise LookupError("Meldung nicht gefunden")
    ctx.db.x("UPDATE news_item SET ai_requested_at=? WHERE id=?", (iso(datetime.now(UTC)), item_id))
    ctx.db.x("DELETE FROM news_analysis WHERE item_id=? AND summary IS NULL", (item_id,))
    if ctx.scheduler is not None:
        ctx.scheduler.trigger("news_analyze", 20)
    log.info("KI-Zusammenfassung angefordert für Meldung %s", item_id)
    return item_states(ctx, [item_id]).get(item_id, {"state": "off"})


def pending_requests(ctx: Any) -> int:
    return int(ctx.db.scalar("""SELECT COUNT(*) FROM news_item n LEFT JOIN news_analysis a ON a.item_id=n.id
                                WHERE n.ai_requested_at IS NOT NULL AND a.item_id IS NULL""", default=0) or 0)


AREA_TASKS = (("news", "Artikel"), ("community", "Community"), ("video", "Videos"), ("report", "Tagesbericht"),
              ("events", "Ereignis-Grenzfälle"), ("discovery", "Watchlist: KI-Suche"),
              ("company", "Watchlist: Firmenanalyse"))


def overview(ctx: Any) -> dict[str, Any]:
    """Für die Einstellungskarte „KI je Bereich“: gewählte Modi, Verbrauch je Bereich (heute, Ø der letzten sieben
    Tage) und geschätzter Tagesbedarf der Artikel und Community-Beiträge mit der aktuellen Einstellung."""
    from app.news.ai import AiGateway

    gw = AiGateway(ctx)
    today = gw.day_key("gemini")
    days = [r["day"] for r in ctx.db.q("SELECT DISTINCT day FROM ai_model_usage ORDER BY day DESC LIMIT 7")]
    per: dict[str, dict[str, float]] = {}
    for r in ctx.db.q("SELECT day, task, SUM(requests) AS n FROM ai_model_usage WHERE day >= ? GROUP BY day, task",
                      (min(days) if days else today,)):
        d = per.setdefault(str(r["task"]), {"today": 0, "sum": 0})
        d["sum"] += int(r["n"] or 0)
        if r["day"] == today:
            d["today"] += int(r["n"] or 0)
    ndays = max(1, len(days))
    usage = [{"task": t, "label": lbl, "today": int(per.get(t, {}).get("today", 0)),
              "avg": round(per.get(t, {}).get("sum", 0) / ndays, 1)} for t, lbl in AREA_TASKS]
    # Zufluss der letzten 7 Tage (je Tag) für die Schätzung
    since = iso(datetime.now(UTC) - timedelta(days=7))
    off, tg = ai_off_ids(ctx.config.sources_path), community_ids(ctx)
    arts = comm = 0
    for r in ctx.db.q("""SELECT source_id FROM news_item WHERE kind='article' AND hidden_reason IS NULL
                         AND published_at >= ?""", (since,)):
        if r["source_id"] in off:
            continue
        if r["source_id"] in tg:
            comm += 1
        else:
            arts += 1
    from app.news.analysis import BATCH

    amode, cmode = article_mode(ctx), community_mode(ctx)
    n_top = top_per_day(ctx)
    est_articles = {"top": -(-n_top // BATCH), "all": -(-arts // 7 // BATCH), "manual": 0}[amode]
    est_comm = -(-comm // 7 // BATCH) if cmode == "all" else 0
    report = 1 if ctx.settings.get("news.report", True) else 0
    return {"articles": amode, "community": cmode, "videos": video_mode(ctx), "top_per_day": n_top,
            "article_modes": ARTICLE_MODES, "community_modes": COMMUNITY_MODES, "video_modes": VIDEO_MODES,
            "batch_wait": int(batch_wait(ctx).total_seconds() // 60), "used_today": used_today(ctx),
            "usage": usage, "days": ndays, "inflow": {"articles": round(arts / 7), "community": round(comm / 7)},
            "estimate": {"articles": est_articles, "community": est_comm, "report": report,
                         "total": est_articles + est_comm + report},
            "community_off_sources": sorted(tg & off),
            "report_time": f"{int(ctx.settings.get('news.report_hour', 22)):02d}:"
                           f"{int(ctx.settings.get('news.report_minute', 15)):02d}",
            "report_on": bool(ctx.settings.get("news.report", True))}


def save(ctx: Any, form: Any) -> None:
    """Formular der Karte „KI je Bereich“ speichern (nur diese Schlüssel)."""
    s = ctx.settings
    a = str(form.get("ai_articles") or "")
    if a in ARTICLE_MODES:
        s.set("news.ai_articles", a)
    c = str(form.get("ai_community") or "")
    if c in COMMUNITY_MODES:
        s.set("news.ai_community", c)
    v = str(form.get("ai_videos") or "")
    if v in VIDEO_MODES:
        s.set("news.ai_videos", v)
    with contextlib.suppress(ValueError):
        s.set("news.ai_top_per_day", max(5, min(300, int(str(form.get("ai_top_per_day") or "50")))))
    with contextlib.suppress(ValueError):
        s.set("news.ai_batch_wait_min", max(0, min(240, int(str(form.get("ai_batch_wait_min") or "30")))))
