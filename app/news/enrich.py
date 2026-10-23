"""Stufe B – Artikelinhalt nachladen (Hintergrundjob, getrennt vom Feed-Import).

Der Feed-Import (``NewsService.run``) speichert nur Titel/Teaser und bleibt davon unberührt: Fehler hier blockieren ihn
nie. Ausgewählt werden die wichtigsten noch nicht angereicherten Artikel (Positionsbezug, Mehrfachberichte,
Quellgewicht, Aktualität) – **auch marktweite Meldungen ohne Positionsbezug**, weil sie für den Tagesbericht gebraucht
werden. Pro Lauf und Host sind Anzahl und Takt begrenzt; fällt ein Host wiederholt aus, ruht er (Backoff).

Status je Artikel: ``pending`` (Wiederholung geplant), ``success``, ``partial``, ``blocked`` (Bezahl-/Zustimmungs-
schranke, Zugriff verweigert – wird nicht umgangen), ``failed``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from app.context import AppContext
from app.news import gnews
from app.news.extract import assess, excerpt, parse_article
from app.news.safefetch import FetchError, SafeFetcher
from app.util.http import RateLimiter
from app.util.timeutil import iso, parse_iso

log = logging.getLogger(__name__)

RETRY_DELAYS = (timedelta(minutes=30), timedelta(hours=3), timedelta(hours=12))
RETRYABLE = {"timeout", "network", "rate_limited", "http_error", "dns", "gnews_unresolved"}
HOST_INTERVAL_S = 2.0
HOST_FAIL_LIMIT = 4
HOST_PAUSE = timedelta(hours=6)
SKIP_HOSTS = ("youtube.com", "youtu.be", "twitter.com", "x.com", "facebook.com", "instagram.com", "t.me")


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


class EnrichService:
    def __init__(self, ctx: AppContext, fetcher: SafeFetcher | None = None) -> None:
        self.ctx = ctx
        self.db = ctx.db
        self.fetcher = fetcher or SafeFetcher()
        self._limiters: dict[str, RateLimiter] = {}

    # -- Einstellungen -----------------------------------------------------------------------------------
    def enabled(self) -> bool:
        return bool(self.ctx.settings.get("news.enrich", True))

    def _limit(self, host: str) -> RateLimiter:
        if host not in self._limiters:
            self._limiters[host] = RateLimiter(3.0 if host in gnews.GNEWS_HOSTS else HOST_INTERVAL_S)
        return self._limiters[host]

    # -- Auswahl -----------------------------------------------------------------------------------------
    def candidates(self, limit: int) -> list[dict[str, Any]]:
        """Noch nicht (erfolgreich) angereicherte Artikel in Prioritätsreihenfolge."""
        days = int(self.ctx.settings.get("news.enrich_days", 3))
        since = iso(datetime.now(UTC) - timedelta(days=days))
        now = iso(datetime.now(UTC))
        rows = self.db.q(
            """SELECT n.id, n.url, n.title, n.summary, n.relevance, n.dup_count, n.source_weight, n.published_at,
                      c.status AS cstatus
               FROM news_item n LEFT JOIN news_content c ON c.item_id = n.id
               WHERE n.kind='article' AND n.hidden_reason IS NULL AND n.published_at >= ?
                 AND (c.item_id IS NULL OR (c.status='pending' AND (c.next_attempt IS NULL OR c.next_attempt <= ?)))
               ORDER BY n.published_at DESC LIMIT 600""", (since, now))
        pub_now = datetime.now(UTC)
        scored = []
        for r in rows:
            if any(_host(r["url"]) == h or _host(r["url"]).endswith("." + h) for h in SKIP_HOSTS):
                continue
            pub = parse_iso(r["published_at"]) or pub_now
            age_h = max(0.0, (pub_now - pub).total_seconds() / 3600)
            # Positionsbezug (relevance), Mehrfachberichte = marktweite Bedeutung, Quellgewicht, Aktualität
            prio = (float(r["relevance"]) + 0.06 * min(int(r["dup_count"]), 6) + 0.1 * float(r["source_weight"])
                    + 0.2 / (1.0 + age_h / 12.0))
            scored.append({**dict(r), "priority": round(prio, 4)})
        scored.sort(key=lambda x: -x["priority"])
        return scored[:limit]

    # -- Lauf ----------------------------------------------------------------------------------------------
    def run(self, limit: int | None = None) -> dict[str, Any]:
        res = {"selected": 0, "success": 0, "partial": 0, "blocked": 0, "failed": 0, "retry": 0, "skipped_host": 0}
        if not self.enabled():
            return {"skipped": "Artikelinhalte deaktiviert"}
        limit = limit or int(self.ctx.settings.get("news.enrich_per_run", 20))
        batch = self.candidates(limit)
        res["selected"] = len(batch)
        for it in batch:
            host = _host(it["url"])
            if self._host_paused(host):
                res["skipped_host"] += 1
                continue
            try:
                status = self.process(it)
            except Exception as e:  # ein Artikel darf den Lauf nie abbrechen
                log.warning("Artikelinhalt %s: unerwarteter Fehler %s", it["id"], e)
                status = self._store_failure(it, "internal", retry=False)
            res[status if status in res else "failed"] += 1
        res["total_success"] = self.db.scalar("SELECT COUNT(*) FROM news_content WHERE status='success'", default=0)
        return res

    def process(self, it: dict[str, Any]) -> str:
        url = it["url"]
        host = _host(url)
        try:
            if gnews.is_gnews(url):
                self._limit("news.google.com").wait()
                url = gnews.resolve(url, self.fetcher)
                host = _host(url)
            self._limit(host).wait()
            page = self.fetcher.get(url)
        except FetchError as e:
            self._host_result(host, ok=False, hard=e.code in ("blocked_http", "consent"))
            if e.code == "blocked_http":
                return self._store(it, "blocked", reason="http_denied", status_code=e.status, final_url=url)
            return self._store_failure(it, e.code, status_code=e.status, final_url=url)
        art = parse_article(page.text(), page.url)
        status, reason, quality = assess(art, it.get("summary") or "")
        self._host_result(host, ok=status in ("success", "partial"), hard=status == "blocked")
        mode = str(self.ctx.settings.get("news.content_mode", "full"))
        return self._store(it, status, reason=reason, art=art, quality=quality, status_code=page.status,
                           final_url=page.url, mode=mode)

    # -- Speichern -----------------------------------------------------------------------------------------
    def _store(self, it: dict[str, Any], status: str, *, reason: str = "", art: Any = None, quality: float = 0.0,
               status_code: int | None = None, final_url: str | None = None, mode: str = "full") -> str:
        now = iso(datetime.now(UTC))
        text = art.text if art is not None and status in ("success", "partial") else ""
        keep = text if mode == "full" else excerpt(text, 1500)
        preview = art.text if art is not None else ""
        row_exc = (excerpt(preview, 600) if preview else (excerpt(art.description, 600) if art is not None else ""))
        chash = art.content_hash() if text else None
        prev = self.db.q1("SELECT attempts FROM news_content WHERE item_id=?", (it["id"],))
        self.db.x(
            """INSERT INTO news_content(item_id, status, reason, final_url, canonical_url, http_status, title, author,
                   published_at, lang, image_url, content, excerpt, word_count, quality, content_hash, priority,
                   attempts, next_attempt, fetched_at, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)
               ON CONFLICT(item_id) DO UPDATE SET status=excluded.status, reason=excluded.reason,
                   final_url=excluded.final_url, canonical_url=excluded.canonical_url,
                   http_status=excluded.http_status, title=excluded.title, author=excluded.author,
                   published_at=excluded.published_at, lang=excluded.lang, image_url=excluded.image_url,
                   content=excluded.content, excerpt=excluded.excerpt, word_count=excluded.word_count,
                   quality=excluded.quality, content_hash=excluded.content_hash, priority=excluded.priority,
                   attempts=excluded.attempts, next_attempt=NULL, fetched_at=excluded.fetched_at""",
            (it["id"], status, reason or None, final_url, getattr(art, "canonical_url", None) or None, status_code,
             getattr(art, "title", None) or None, getattr(art, "author", None) or None,
             getattr(art, "published", None) or None, getattr(art, "lang", None) or None,
             getattr(art, "image_url", None) or None, keep or None, row_exc or None,
             art.words if text else 0, quality, chash, it.get("priority"), (prev["attempts"] if prev else 0) + 1,
             now, now))
        if art is not None and art.image_url and status in ("success", "partial"):
            self.db.x("UPDATE news_item SET image_url=? WHERE id=? AND (image_url IS NULL OR image_url='')",
                      (art.image_url, it["id"]))
        return status

    def _store_failure(self, it: dict[str, Any], code: str, *, retry: bool | None = None,
                       status_code: int | None = None, final_url: str | None = None) -> str:
        prev = self.db.q1("SELECT attempts FROM news_content WHERE item_id=?", (it["id"],))
        attempts = (prev["attempts"] if prev else 0) + 1
        can_retry = (code in RETRYABLE) if retry is None else retry
        now = datetime.now(UTC)
        pending = can_retry and attempts <= len(RETRY_DELAYS)
        nxt = iso(now + RETRY_DELAYS[attempts - 1]) if pending else None
        self.db.x(
            """INSERT INTO news_content(item_id, status, reason, final_url, http_status, priority, attempts,
                   next_attempt, fetched_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(item_id) DO UPDATE SET status=excluded.status, reason=excluded.reason,
                   final_url=excluded.final_url, http_status=excluded.http_status, attempts=excluded.attempts,
                   next_attempt=excluded.next_attempt, fetched_at=excluded.fetched_at""",
            (it["id"], "pending" if pending else "failed", code, final_url, status_code, it.get("priority"),
             attempts, nxt, iso(now), iso(now)))
        return "retry" if pending else "failed"

    # -- Host-Backoff ----------------------------------------------------------------------------------------
    def _host_paused(self, host: str) -> bool:
        st = self.db.get_state(f"news.enrich.host.{host}") or {}
        until = parse_iso(st.get("until")) if isinstance(st, dict) else None
        return until is not None and datetime.now(UTC) < until

    def _host_result(self, host: str, *, ok: bool, hard: bool = False) -> None:
        key = f"news.enrich.host.{host}"
        st = self.db.get_state(key) or {}
        fails = 0 if ok else int(st.get("fails", 0)) + (2 if hard else 1)
        until = iso(datetime.now(UTC) + HOST_PAUSE) if fails >= HOST_FAIL_LIMIT else None
        if fails or st:
            self.db.set_state(key, {"fails": 0 if until else fails, "until": until})

    # -- Wiederholung / Pflege -----------------------------------------------------------------------------
    def reset(self, item_id: int) -> None:
        """Manuell „Erneut laden“: Zustand löschen, nächster Lauf nimmt den Artikel wieder auf."""
        self.db.x("DELETE FROM news_content WHERE item_id=?", (item_id,))

    def stats(self) -> dict[str, int]:
        since = iso(datetime.now(UTC) - timedelta(days=1))
        out = {r["status"]: r["n"] for r in self.db.q(
            "SELECT status, COUNT(*) AS n FROM news_content WHERE fetched_at>=? GROUP BY status", (since,))}
        return {k: int(out.get(k, 0)) for k in ("success", "partial", "blocked", "failed", "pending")}


__all__ = ["EnrichService"]
