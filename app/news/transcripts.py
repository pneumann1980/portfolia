"""YouTube-Transkripte laden (Hintergrundjob, getrennt vom Feed-Import) – Grundlage der Video-Analyse.

Auswahl: neue Videos der abonnierten Kanäle sowie Videos mit Bezug zu einer Position – ohne Shorts und Livestreams,
höchstens ``news.transcripts_days`` Tage alt, je Lauf begrenzt und mit Pause zwischen den Abrufen. Gespeichert wird
wie ein Artikeltext (``news_content``: Absätze mit Zeitmarke ``[mm:ss]``, Grund
``transcript:<auto|manuell>:<Sprache>``), damit die Leseansicht und die Analyse ihn nutzen.

Status je Video: ``success`` · ``failed`` (keine Untertitel / Video nicht verfügbar / wiederholt unlesbar – kein
weiterer Versuch) · ``pending`` (später erneut). Meldet YouTube eine Bot-Sperre, ruht der Abruf
``BLOCK_PAUSE`` lang (Anzeige in den Einstellungen); bleibt die Bibliothek unlesbar, steht dort der Hinweis
„Bibliothek aktualisieren“.
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from app.context import AppContext
from app.news import ytlib
from app.util.timeutil import iso, parse_iso

log = logging.getLogger(__name__)

SETTING = "news.transcripts"
PAUSE_KEY = "news.transcripts.paused_until"
ERROR_KEY = "news.transcripts.last_error"
BLOCK_PAUSE = timedelta(hours=24)
RETRY = {ytlib.KIND_NETWORK: timedelta(hours=1), ytlib.KIND_BROKEN: timedelta(hours=12)}
MAX_ATTEMPTS = 3
PER_RUN = 8
GAP_S = 3.0
PARAGRAPH_S = 60.0  # Absatz etwa je Minute (mit Zeitmarke)
MAX_DURATION_S = 4 * 3600  # längere „Videos“ sind praktisch immer aufgezeichnete Livestreams


def fmt_ts(sec: float) -> str:
    s = int(sec)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def paragraphs(segments: list[tuple[float, str]]) -> str:
    """Segmente zu Absätzen (etwa je Minute, an Satzende), jeweils mit Zeitmarke am Anfang."""
    out: list[str] = []
    cur: list[str] = []
    start = 0.0
    for t, text in segments:
        text = " ".join(text.replace("\n", " ").split())
        if not text:
            continue
        if not cur:
            start = t
        cur.append(text)
        if (t - start >= PARAGRAPH_S and text[-1:] in ".!?") or t - start >= PARAGRAPH_S * 1.5:
            out.append(f"[{fmt_ts(start)}] " + " ".join(cur))
            cur = []
    if cur:
        out.append(f"[{fmt_ts(start)}] " + " ".join(cur))
    return "\n\n".join(out)


class TranscriptService:
    def __init__(self, ctx: AppContext, *, gap_s: float = GAP_S) -> None:
        self.ctx = ctx
        self.db = ctx.db
        self.gap_s = gap_s

    def enabled(self) -> bool:
        return bool(self.ctx.settings.get(SETTING, True))

    def paused_until(self) -> datetime | None:
        until = parse_iso(self.db.get_state(PAUSE_KEY))
        return until if until is not None and until > datetime.now(UTC) else None

    def candidates(self, limit: int) -> list[Any]:
        days = int(self.ctx.settings.get("news.transcripts_days", 3))
        now = iso(datetime.now(UTC))
        return self.db.q(
            """SELECT n.id, n.video_id, n.url, n.language, c.attempts FROM news_item n
               LEFT JOIN news_content c ON c.item_id=n.id
               WHERE n.kind='video' AND n.video_id IS NOT NULL AND n.hidden_reason IS NULL
                 AND COALESCE(n.is_short, 0)=0 AND COALESCE(n.is_live, 0)=0
                 AND COALESCE(n.duration_s, 0) <= ? AND n.published_at >= ?
                 AND (n.source_id LIKE 'yt:%' OR EXISTS (SELECT 1 FROM news_asset a WHERE a.item_id=n.id))
                 AND (c.item_id IS NULL OR (c.status='pending' AND (c.next_attempt IS NULL OR c.next_attempt <= ?)))
               ORDER BY n.published_at DESC LIMIT ?""",
            (MAX_DURATION_S, iso(datetime.now(UTC) - timedelta(days=days)), now, limit))

    def run(self, limit: int = PER_RUN) -> dict[str, Any]:
        res: dict[str, Any] = {"loaded": 0, "none": 0, "failed": 0, "retry": 0}
        if not self.enabled():
            return {"skipped": "ausgeschaltet"}
        if self.paused_until() is not None:
            return {"skipped": "pausiert (YouTube-Sperre)"}
        rows = self.candidates(limit)
        for i, r in enumerate(rows):
            if i and self.gap_s:
                time.sleep(self.gap_s)
            kind = self.process(r)
            res[{"success": "loaded", ytlib.KIND_NONE: "none", ytlib.KIND_UNAVAILABLE: "none"}.get(
                kind, "retry" if kind in RETRY else "failed")] += 1
            if kind == ytlib.KIND_BLOCKED:
                res["stopped"] = "YouTube-Sperre – Abruf ruht 24 h"
                break
        return res

    def process(self, r: Any) -> str:
        langs = [x for x in dict.fromkeys([(r["language"] or "").split("-")[0].lower(), "de", "en"]) if x]
        try:
            got = ytlib.fetch(self.ctx.config, r["video_id"], langs)
        except ytlib.TranscriptError as e:
            return self._failure(r, e)
        text = paragraphs(got.segments)
        self._upsert(r["id"], "success", f"transcript:{'auto' if got.generated else 'manuell'}:{got.language}",
                     url=r["url"], lang=got.language, content=text,
                     words=len(text.split()), chash=hashlib.sha256(text.encode()).hexdigest()[:32])
        self.db.set_state(ERROR_KEY, None)
        return "success"

    def _failure(self, r: Any, e: ytlib.TranscriptError) -> str:
        attempts = int(r["attempts"] or 0) + 1
        if e.kind == ytlib.KIND_BLOCKED:
            until = datetime.now(UTC) + BLOCK_PAUSE
            self.db.set_state(PAUSE_KEY, iso(until))
            self.db.set_state(ERROR_KEY, {"at": iso(datetime.now(UTC)), "kind": e.kind, "detail": e.detail})
            log.warning("YouTube-Transkripte: Abruf gesperrt (%s) – Pause bis %s", e.detail, iso(until))
            self._upsert(r["id"], "pending", "blocked", url=r["url"], attempts=attempts - 1, next_at=until)
            return e.kind
        if e.kind in RETRY and attempts < MAX_ATTEMPTS:
            if e.kind == ytlib.KIND_BROKEN:
                self.db.set_state(ERROR_KEY, {"at": iso(datetime.now(UTC)), "kind": e.kind, "detail": e.detail})
                log.warning("YouTube-Transkripte: Bibliothek kann das Video nicht lesen (%s)", e.detail)
            self._upsert(r["id"], "pending", e.kind, url=r["url"], attempts=attempts,
                         next_at=datetime.now(UTC) + RETRY[e.kind])
            return e.kind
        reason = {ytlib.KIND_NONE: "no_transcript", ytlib.KIND_UNAVAILABLE: "unavailable"}.get(e.kind, e.kind)
        self._upsert(r["id"], "failed", reason, url=r["url"], attempts=attempts)
        return e.kind

    def _upsert(self, item_id: int, status: str, reason: str, *, url: str, lang: str | None = None,
                content: str | None = None, words: int | None = None, chash: str | None = None, attempts: int = 0,
                next_at: datetime | None = None) -> None:
        now = iso(datetime.now(UTC))
        self.db.x(
            """INSERT INTO news_content(item_id, status, reason, final_url, lang, content, word_count, content_hash,
                   attempts, next_attempt, fetched_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(item_id) DO UPDATE SET status=excluded.status, reason=excluded.reason,
                   final_url=excluded.final_url, lang=excluded.lang, content=excluded.content,
                   word_count=excluded.word_count, content_hash=excluded.content_hash, attempts=excluded.attempts,
                   next_attempt=excluded.next_attempt, fetched_at=excluded.fetched_at""",
            (item_id, status, reason, url, lang, content, words, chash, attempts, iso(next_at) if next_at else None,
             now, now))

    def stats(self) -> dict[str, int]:
        since = iso(datetime.now(UTC) - timedelta(days=7))
        out = {"success": 0, "failed": 0, "pending": 0}
        for r in self.db.q(
                """SELECT c.status, COUNT(*) AS n FROM news_content c JOIN news_item n ON n.id=c.item_id
                   WHERE n.kind='video' AND n.published_at>=? GROUP BY c.status""", (since,)):
            out[r["status"]] = int(r["n"])
        return out


def is_transcript(content: Any) -> bool:
    return content is not None and content["status"] == "success" and str(content["reason"] or "").startswith(
        "transcript:")
