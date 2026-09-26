"""Feeds parsen (RSS/Atom via feedparser, JSON-Schnittstellen) → einheitliche Rohmeldungen."""

from __future__ import annotations

import calendar
import html
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import feedparser

_TAG_RE = re.compile(r"<[^>]+>")
_IMG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


@dataclass
class RawItem:
    title: str
    url: str
    summary: str = ""
    published: datetime | None = None
    image_url: str | None = None
    author: str | None = None
    kind: str = "article"
    video_id: str | None = None
    channel_id: str | None = None
    channel_name: str | None = None
    views: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def clean_text(s: str | None, limit: int = 600) -> str:
    if not s:
        return ""
    t = html.unescape(_TAG_RE.sub(" ", s))
    t = _WS_RE.sub(" ", t).strip()
    if len(t) > limit:
        t = t[: limit - 1].rsplit(" ", 1)[0] + "…"
    return t


def _dt(struct: Any) -> datetime | None:
    if not struct:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(struct), UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def _image(entry: Any) -> str | None:
    for key in ("media_thumbnail", "media_content"):
        val = entry.get(key)
        if val and isinstance(val, list):
            for m in val:
                url = m.get("url")
                if url and (key == "media_thumbnail" or str(m.get("medium", "image")).startswith("image")
                            or str(m.get("type", "")).startswith("image")):
                    return url
    for enc in entry.get("enclosures") or []:
        if str(enc.get("type", "")).startswith("image") and enc.get("href"):
            return enc["href"]
    for key in ("summary", "content"):
        raw = entry.get(key)
        if isinstance(raw, list):
            raw = " ".join(c.get("value", "") for c in raw)
        if raw:
            m = _IMG_RE.search(raw)
            if m:
                return m.group(1)
    return None


def parse_feed(content: bytes, max_items: int = 60) -> tuple[list[RawItem], str | None]:
    """Rückgabe: (Meldungen, Fehlertext bei unlesbarem Feed)."""
    d = feedparser.parse(content)
    if d.get("bozo") and not d.get("entries"):
        return [], f"Feed nicht lesbar: {d.get('bozo_exception')}"
    out: list[RawItem] = []
    for e in (d.get("entries") or [])[:max_items]:
        title = clean_text(e.get("title"), 300)
        link = e.get("link") or ""
        if not title or not link:
            continue
        summary = e.get("summary") or ""
        if not summary and e.get("content"):
            summary = " ".join(c.get("value", "") for c in e["content"])
        item = RawItem(
            title=title, url=link, summary=clean_text(summary), published=_dt(e.get("published_parsed"))
            or _dt(e.get("updated_parsed")), image_url=_image(e), author=e.get("author"),
        )
        vid = e.get("yt_videoid")
        if vid:
            item.kind = "video"
            item.video_id = vid
            item.channel_id = e.get("yt_channelid")
            item.channel_name = e.get("author")
            stats = e.get("media_statistics") or {}
            try:
                item.views = int(stats.get("views")) if stats.get("views") is not None else None
            except (TypeError, ValueError):
                item.views = None
            item.image_url = item.image_url or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
            item.url = f"https://www.youtube.com/watch?v={vid}"
        out.append(item)
    return out, None


def parse_binance(data: dict[str, Any]) -> list[RawItem]:
    out = []
    for cat in ((data or {}).get("data") or {}).get("catalogs") or []:
        for a in cat.get("articles") or []:
            code = a.get("code")
            if not code or not a.get("title"):
                continue
            ts = a.get("releaseDate")
            out.append(RawItem(title=clean_text(a["title"], 300),
                               url=f"https://www.binance.com/en/support/announcement/{code}",
                               published=datetime.fromtimestamp(ts / 1000, UTC) if ts else None,
                               summary=clean_text(cat.get("catalogName"))))
    return out


def parse_bybit(data: dict[str, Any]) -> list[RawItem]:
    out = []
    for a in ((data or {}).get("result") or {}).get("list") or []:
        if not a.get("title") or not a.get("url"):
            continue
        ts = a.get("publishTime") or a.get("dateTimestamp")
        out.append(RawItem(title=clean_text(a["title"], 300), url=a["url"], summary=clean_text(a.get("description")),
                           published=datetime.fromtimestamp(int(ts) / 1000, UTC) if ts else None))
    return out


def parse_finnhub(data: list[dict[str, Any]]) -> list[RawItem]:
    out = []
    for a in data or []:
        if not a.get("headline") or not a.get("url"):
            continue
        ts = a.get("datetime")
        out.append(RawItem(title=clean_text(a["headline"], 300), url=a["url"], summary=clean_text(a.get("summary")),
                           published=datetime.fromtimestamp(int(ts), UTC) if ts else None,
                           image_url=a.get("image") or None, author=a.get("source")))
    return out


def parse_cryptopanic(data: dict[str, Any]) -> list[RawItem]:
    out = []
    for a in (data or {}).get("results") or []:
        url = a.get("original_url") or a.get("url")
        if not a.get("title") or not url:
            continue
        pub = a.get("published_at")
        try:
            dt = datetime.fromisoformat(pub.replace("Z", "+00:00")) if pub else None
        except ValueError:
            dt = None
        out.append(RawItem(title=clean_text(a["title"], 300), url=url, summary=clean_text(a.get("description")),
                           published=dt, author=(a.get("source") or {}).get("title")))
    return out
