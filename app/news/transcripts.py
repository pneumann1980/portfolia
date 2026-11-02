"""YouTube-Transkripte laden (Hintergrundjob, getrennt vom Feed-Import) – Grundlage der Video-Analyse.

Nur auf Wunsch (seit 0.81.0; der automatische Abruf aller Videos lief nach etwa 24 Abrufen am Tag in YouTubes Sperre):

* **Aufträge** – Videos, für die jemand „Kernaussagen erstellen“ gedrückt hat (``news_item.tr_requested_at``), egal
  wie alt; immer zuerst.
* **Lieblingskanäle** – neue Videos (höchstens ``news.transcripts_days`` Tage alt, ohne Shorts und Livestreams) der in
  ``news.transcripts_fav`` gewählten Kanäle, zusammen höchstens ``news.transcripts_auto_per_day`` Abrufe je Kalendertag.

Je Lauf begrenzt und mit Pause zwischen den Abrufen. Gespeichert wird
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
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from app.context import AppContext
from app.news import ytlib
from app.util.timeutil import iso, now_local, parse_iso

log = logging.getLogger(__name__)

SETTING = "news.transcripts"
FAV_KEY = "news.transcripts_fav"  # Kanal-IDs mit automatischem Abruf
AUTO_PER_DAY_KEY = "news.transcripts_auto_per_day"
AUTO_PER_DAY = 5  # Lieblingskanäle: Abrufe je Kalendertag (beobachtete Sperrgrenze ≈ 24/Tag)
PASTE_MAX = 300_000  # Zeichen eines selbst eingefügten Transkripts
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

    def favorites(self) -> list[str]:
        fav = self.ctx.settings.get(FAV_KEY, []) or []
        return [str(x) for x in fav if x] if isinstance(fav, list) else []

    def set_favorite(self, channel_id: str, on: bool) -> list[str]:
        fav = [c for c in self.favorites() if c != channel_id] + ([channel_id] if on else [])
        self.ctx.settings.set(FAV_KEY, fav)
        return fav

    def auto_per_day(self) -> int:
        return max(0, int(self.ctx.settings.get(AUTO_PER_DAY_KEY, AUTO_PER_DAY) or 0))

    def auto_used_today(self) -> int:
        """Heutige automatische Abrufe (Lieblingskanäle, ohne Aufträge) – Kalendertag Europe/Berlin."""
        midnight = iso(now_local().replace(hour=0, minute=0, second=0, microsecond=0))
        return int(self.db.scalar(
            """SELECT COUNT(*) FROM news_content c JOIN news_item n ON n.id=c.item_id
               WHERE n.kind='video' AND n.tr_requested_at IS NULL AND c.fetched_at >= ?
                 AND (c.reason IS NULL OR c.reason NOT LIKE 'transcript:eingefuegt:%')""",
            (midnight,), default=0) or 0)

    _DUE = "(c.item_id IS NULL OR (c.status='pending' AND (c.next_attempt IS NULL OR c.next_attempt <= ?)))"

    def requested(self, limit: int) -> list[Any]:
        """Offene Aufträge (Knopf „Kernaussagen erstellen“), älteste Anforderung zuerst."""
        return self.db.q(
            f"""SELECT n.id, n.video_id, n.url, n.language, c.attempts FROM news_item n
                LEFT JOIN news_content c ON c.item_id=n.id
                WHERE n.kind='video' AND n.video_id IS NOT NULL AND n.tr_requested_at IS NOT NULL AND {self._DUE}
                ORDER BY n.tr_requested_at, n.id LIMIT ?""", (iso(datetime.now(UTC)), limit))

    def favorite_candidates(self, limit: int) -> list[Any]:
        """Neue Videos der Lieblingskanäle, solange die Tagesgrenze nicht erreicht ist."""
        fav = self.favorites()
        left = min(limit, self.auto_per_day() - self.auto_used_today())
        if not fav or left <= 0:
            return []
        days = int(self.ctx.settings.get("news.transcripts_days", 3))
        return self.db.q(
            f"""SELECT n.id, n.video_id, n.url, n.language, c.attempts FROM news_item n
                LEFT JOIN news_content c ON c.item_id=n.id
                WHERE n.kind='video' AND n.video_id IS NOT NULL AND n.hidden_reason IS NULL
                  AND n.tr_requested_at IS NULL AND n.channel_id IN ({','.join('?' * len(fav))})
                  AND COALESCE(n.is_short, 0)=0 AND COALESCE(n.is_live, 0)=0
                  AND COALESCE(n.duration_s, 0) <= ? AND n.published_at >= ? AND {self._DUE}
                ORDER BY n.published_at DESC LIMIT ?""",
            (*fav, MAX_DURATION_S, iso(datetime.now(UTC) - timedelta(days=days)), iso(datetime.now(UTC)), left))

    def candidates(self, limit: int) -> list[Any]:
        """Aufträge zuerst, danach Lieblingskanäle (Tagesgrenze)."""
        rows = list(self.requested(limit))
        if len(rows) < limit:
            rows += self.favorite_candidates(limit - len(rows))
        return rows

    def run(self, limit: int = PER_RUN) -> dict[str, Any]:
        res: dict[str, Any] = {"loaded": 0, "none": 0, "failed": 0, "retry": 0}
        if not self.enabled():
            return {"skipped": "ausgeschaltet"}
        if self.paused_until() is not None:
            return {"skipped": "pausiert (YouTube-Sperre)"}
        rows = self.requested(limit)
        for i in range(limit):
            if i >= len(rows):  # Lieblingskanäle erst nach den Aufträgen, je Abruf neu gegen die Tagesgrenze
                more = self.favorite_candidates(1) if len(rows) < limit else []
                if not more:
                    break
                rows.append(more[0])
            r = rows[i]
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

    def paste(self, item_id: int, text: str) -> int:
        """Selbst eingefügtes Transkript (z. B. aus „Transkript anzeigen“ der YouTube-App) wie ein geladenes speichern;
        Zeitmarken („1:23“ allein oder am Zeilenanfang) werden erkannt. Rückgabe: Wortzahl."""
        row = self.db.q1("SELECT id, url, language FROM news_item WHERE id=? AND kind='video'", (item_id,))
        if row is None:
            raise ValueError("Video nicht gefunden")
        content = pasted_paragraphs(text[:PASTE_MAX])
        words = len(content.split())
        if words < 30:
            raise ValueError("Zu kurz – bitte das ganze Transkript einfügen (mindestens 30 Wörter).")
        lang = (row["language"] or "de").split("-")[0].lower()
        self._upsert(item_id, "success", f"transcript:eingefuegt:{lang}", url=row["url"], lang=lang, content=content,
                     words=words, chash=hashlib.sha256(content.encode()).hexdigest()[:32])
        self.db.x("UPDATE news_item SET tr_requested_at=COALESCE(tr_requested_at, ?) WHERE id=?",
                  (iso(datetime.now(UTC)), item_id))
        return words

    def pipeline(self) -> dict[str, Any]:
        """Statusseite: wartend (jetzt abrufbar), spätere Wiederholungen, letzte 24 h geladen/ohne Untertitel, Sperre
        und Bibliotheksfehler (``broken`` = „Bibliothek aktualisieren“)."""
        now = datetime.now(UTC)
        days = int(self.ctx.settings.get("news.transcripts_days", 3))
        since24 = iso(now - timedelta(hours=24))
        by = {r["k"]: int(r["n"]) for r in self.db.q(
            """SELECT CASE WHEN c.status='success' THEN 'loaded'
                           WHEN c.status='failed' AND c.reason IN ('no_transcript','unavailable') THEN 'none'
                           WHEN c.status='failed' THEN 'failed' ELSE 'other' END AS k, COUNT(*) AS n
               FROM news_content c JOIN news_item n ON n.id=c.item_id
               WHERE n.kind='video' AND c.fetched_at>=? AND (c.reason LIKE 'transcript:%' OR c.status!='success')
               GROUP BY k""", (since24,))}
        retry = int(self.db.scalar(
            """SELECT COUNT(*) FROM news_content c JOIN news_item n ON n.id=c.item_id
               WHERE n.kind='video' AND c.status='pending' AND c.next_attempt > ? AND n.published_at >= ?""",
            (iso(now), iso(now - timedelta(days=days))), default=0) or 0)
        err = self.db.get_state(ERROR_KEY)
        err = err if isinstance(err, dict) else None
        paused = self.paused_until()
        open_requests = len(self.requested(100_000))
        return {"enabled": self.enabled(), "waiting": open_requests + len(self.favorite_candidates(100_000)),
                "requests": open_requests, "favorites": len(self.favorites()), "auto_used": self.auto_used_today(),
                "auto_per_day": self.auto_per_day(), "retry": retry,
                "loaded": by.get("loaded", 0), "none": by.get("none", 0), "failed": by.get("failed", 0),
                "paused_until": iso(paused) if paused else None, "last_error": err,
                "broken": bool(err and err.get("kind") == ytlib.KIND_BROKEN), "per_run": PER_RUN, "days": days}

    def stats(self) -> dict[str, int]:
        since = iso(datetime.now(UTC) - timedelta(days=7))
        out = {"success": 0, "failed": 0, "pending": 0}
        for r in self.db.q(
                """SELECT c.status, COUNT(*) AS n FROM news_content c JOIN news_item n ON n.id=c.item_id
                   WHERE n.kind='video' AND n.published_at>=? GROUP BY c.status""", (since,)):
            out[r["status"]] = int(r["n"])
        return out


_TS = re.compile(r"^\[?((?:\d{1,2}:)?\d{1,2}:\d{2})\]?\s*(.*)$")


def _seconds(ts: str) -> float:
    sec = 0
    for part in ts.split(":"):
        sec = sec * 60 + int(part)
    return float(sec)


def pasted_paragraphs(text: str) -> str:
    """Eingefügten Text in Absätze bringen: mit Zeitmarken wie ein geladenes Transkript (Absatz je Minute), ohne
    Zeitmarken Absätze des Originals bzw. etwa 150 Wörter."""
    lines = [ln.strip() for ln in text.replace("\r", "").split("\n")]
    segs: list[tuple[float, str]] = []
    pending: float | None = None
    stamped = 0
    for ln in lines:
        if not ln:
            continue
        m = _TS.match(ln)
        if m:
            stamped += 1
            if m.group(2):
                segs.append((_seconds(m.group(1)), m.group(2)))
                pending = None
            else:
                pending = _seconds(m.group(1))
        else:
            t = pending if pending is not None else (segs[-1][0] if segs else 0.0)
            segs.append((t, ln))
            pending = None
    if stamped >= 3:
        return paragraphs(segs)
    blocks = [" ".join(b.split()) for b in text.replace("\r", "").split("\n\n") if b.strip()]
    if len(blocks) >= 3:
        return "\n\n".join(blocks)
    words = " ".join(text.split()).split(" ")
    return "\n\n".join(" ".join(words[i:i + 150]) for i in range(0, len(words), 150))


def is_transcript(content: Any) -> bool:
    return content is not None and content["status"] == "success" and str(content["reason"] or "").startswith(
        "transcript:")
