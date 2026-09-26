"""YouTube: Handle-Auflösung, Kanal-RSS, Shorts/Live-Erkennung, optionale Entdeckung über die Data API v3.

* Handles werden nie geraten: nicht auflösbare Handles bleiben inaktiv und werden gemeldet.
* Ohne API-Key: Kanalseite abrufen (Consent-Cookie gegen EU-Zustimmungsseite) und ``externalId`` lesen.
* Mit API-Key: ``channels?forHandle=`` (1 Einheit). Entdeckung: ``search`` (100 Einheiten je Suche),
  ``videos``/``channels`` (1 Einheit je 50 IDs); Tagesbudget unter dem Standardkontingent.
Übertragen werden nur Handles, Kanal-/Video-IDs und Asset-Namen (Suchbegriffe) – keine Portfolio-Daten.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.db import Database
from app.util.http import HttpError, Quota, RateLimiter, request_with_retry
from app.util.timeutil import iso, parse_iso

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/youtube/v3"
FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
# Zustimmungs-Cookie gegen die EU-Consent-Seite (als Header, nicht per-request cookies)
CONSENT_HEADERS = {"Cookie": "SOCS=CAI; CONSENT=YES+cb", "Accept-Language": "en-US,en;q=0.8"}
_CID = r"(UC[\w-]{22})"
_SUB_RE = re.compile(r"([\d][\d.,]*)\s*([KMB]|Tsd\.|Mio\.)?\s*(?:subscribers|Abonnenten)", re.IGNORECASE)
_ISO_DUR = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")
UNITS_SEARCH = 100


@dataclass
class Resolved:
    handle: str
    channel_id: str | None
    title: str | None
    subscribers: int | None
    status: str
    error: str | None = None
    method: str = "page"


def parse_count(text: str) -> int | None:
    m = _SUB_RE.search(text or "")
    if not m:
        return None
    num = m.group(1)
    mult = (m.group(2) or "").lower()
    # Dezimaltrennzeichen: "2.51M" (en) oder "2,51 Mio." (de)
    if mult:
        num = num.replace(",", ".")
        try:
            v = float(num)
        except ValueError:
            return None
        factor = {"k": 1e3, "tsd.": 1e3, "m": 1e6, "mio.": 1e6, "b": 1e9}.get(mult, 1)
        return int(v * factor)
    digits = re.sub(r"[.,]", "", num)
    return int(digits) if digits.isdigit() else None


def parse_channel_page(html: str) -> tuple[str | None, str | None, int | None]:
    cid = None
    for pat in (rf'"externalId":"{_CID}"', rf'<link rel="canonical" href="https://www\.youtube\.com/channel/{_CID}"',
                rf'<meta itemprop="identifier" content="{_CID}"', rf'"channelId":"{_CID}"'):
        m = re.search(pat, html)
        if m:
            cid = m.group(1)
            break
    title = None
    m = re.search(r'<meta property="og:title" content="([^"]+)"', html)
    if m:
        title = m.group(1)
    else:
        m = re.search(r'"channelMetadataRenderer":\{"title":"([^"]+)"', html)
        title = m.group(1) if m else None
    subs = None
    for m in re.finditer(r'"(?:content|simpleText|label)":"([^"]{1,60}(?:subscribers|Abonnenten))"', html):
        subs = parse_count(m.group(1))
        if subs:
            break
    return cid, title, subs


def parse_duration(iso_dur: str | None) -> int | None:
    if not iso_dur:
        return None
    m = _ISO_DUR.fullmatch(iso_dur)
    if not m:
        return None
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


class YouTubeClient:
    def __init__(self, client: httpx.Client, db: Database, api_key: str | None) -> None:
        self.client = client
        self.db = db
        self.api_key = api_key
        self.limiter = RateLimiter(1.0)
        self.quota = Quota(db, "youtube", "day")

    # -- Kontingent ------------------------------------------------------------------------------
    def units_used(self) -> int:
        return self.quota.used()[1]

    def _api(self, path: str, params: dict[str, Any], units: int) -> dict[str, Any]:
        assert self.api_key
        try:
            r = request_with_retry(self.client, "GET", f"{API}/{path}", params={**params, "key": self.api_key},
                                   limiter=self.limiter, retries=1)
        finally:
            self.quota.add(1, units)
        return r.json()

    # -- Handle-Auflösung --------------------------------------------------------------------------
    def resolve(self, handle: str) -> Resolved:
        h = handle if handle.startswith("@") else "@" + handle
        if self.api_key:
            try:
                data = self._api("channels", {"part": "snippet,statistics", "forHandle": h}, 1)
                items = data.get("items") or []
                if not items:
                    return Resolved(h, None, None, None, "unresolvable", "Handle nicht gefunden (API)", "api")
                it = items[0]
                subs = (it.get("statistics") or {}).get("subscriberCount")
                return Resolved(h, it["id"], (it.get("snippet") or {}).get("title"),
                                int(subs) if subs and str(subs).isdigit() else None, "ok", None, "api")
            except (HttpError, httpx.HTTPError, ValueError, KeyError) as e:
                log.info("YouTube-API-Auflösung für %s fehlgeschlagen, versuche Kanalseite: %s", h, e)
        try:
            r = request_with_retry(self.client, "GET", f"https://www.youtube.com/{h}", params={"hl": "en"},
                                   headers=CONSENT_HEADERS, limiter=self.limiter, retries=1)
        except HttpError as e:
            if e.status == 404:
                return Resolved(h, None, None, None, "unresolvable", "Kanalseite existiert nicht (404)")
            return Resolved(h, None, None, None, "pending", f"Kanalseite nicht erreichbar: {e}")
        except httpx.HTTPError as e:
            return Resolved(h, None, None, None, "pending", f"Kanalseite nicht erreichbar: {e}")
        if "consent.youtube.com" in str(r.url):
            return Resolved(h, None, None, None, "pending", "YouTube-Zustimmungsseite statt Kanalseite")
        cid, title, subs = parse_channel_page(r.text)
        if not cid:
            return Resolved(h, None, title, subs, "unresolvable", "Keine Channel-ID auf der Kanalseite gefunden")
        return Resolved(h, cid, title, subs, "ok")

    def store_resolved(self, res: Resolved) -> None:
        self.db.x(
            """INSERT INTO yt_channel(handle, channel_id, title, subscribers, status, error, method, resolved_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(handle) DO UPDATE SET channel_id=COALESCE(excluded.channel_id, channel_id),
                   title=COALESCE(excluded.title, title), subscribers=COALESCE(excluded.subscribers, subscribers),
                   status=excluded.status, error=excluded.error, method=excluded.method,
                   resolved_at=excluded.resolved_at""",
            (res.handle, res.channel_id, res.title, res.subscribers, res.status, res.error, res.method,
             iso(datetime.now(UTC))),
        )

    def resolved(self, handle: str) -> Any:
        return self.db.q1("SELECT * FROM yt_channel WHERE handle=?", (handle,))

    def needs_resolution(self, handle: str, max_age: timedelta = timedelta(days=7)) -> bool:
        row = self.resolved(handle)
        if row is None:
            return True
        ts = parse_iso(row["resolved_at"])
        if row["status"] == "ok":
            return ts is None or datetime.now(UTC) - ts > max_age
        # pending/unresolvable: seltener erneut versuchen
        return ts is None or datetime.now(UTC) - ts > timedelta(hours=12)

    # -- Videos ----------------------------------------------------------------------------------
    def feed(self, channel_id: str) -> bytes:
        r = request_with_retry(self.client, "GET", FEED.format(cid=channel_id), limiter=self.limiter, retries=1)
        return r.content

    def is_short(self, video_id: str) -> bool | None:
        """/shorts/{id}: 200 = Short, Weiterleitung = normales Video (ohne API-Key)."""
        try:
            self.limiter.wait()
            r = self.client.request("HEAD", f"https://www.youtube.com/shorts/{video_id}", follow_redirects=False,
                                    headers=CONSENT_HEADERS, timeout=10)
        except httpx.HTTPError:
            return None
        if r.status_code == 200:
            return True
        if r.status_code in (301, 302, 303, 307, 308):
            return False
        return None

    def video_details(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        if not self.api_key:
            return out
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            data = self._api("videos", {"part": "contentDetails,statistics,snippet,liveStreamingDetails",
                                        "id": ",".join(chunk), "maxResults": 50}, 1)
            for it in data.get("items") or []:
                dur = parse_duration((it.get("contentDetails") or {}).get("duration"))
                live = (it.get("snippet") or {}).get("liveBroadcastContent") in ("live", "upcoming") or bool(
                    it.get("liveStreamingDetails"))
                views = (it.get("statistics") or {}).get("viewCount")
                out[it["id"]] = {"duration_s": dur, "is_live": live,
                                 "views": int(views) if views and str(views).isdigit() else None,
                                 "channel_id": (it.get("snippet") or {}).get("channelId"),
                                 "channel_name": (it.get("snippet") or {}).get("channelTitle"),
                                 "title": (it.get("snippet") or {}).get("title"),
                                 "description": (it.get("snippet") or {}).get("description"),
                                 "published": (it.get("snippet") or {}).get("publishedAt"),
                                 "language": (it.get("snippet") or {}).get("defaultAudioLanguage")}
        return out

    def channel_stats(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        if not self.api_key:
            return out
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            data = self._api("channels", {"part": "snippet,statistics", "id": ",".join(chunk), "maxResults": 50}, 1)
            for it in data.get("items") or []:
                subs = (it.get("statistics") or {}).get("subscriberCount")
                out[it["id"]] = {"title": (it.get("snippet") or {}).get("title"),
                                 "handle": (it.get("snippet") or {}).get("customUrl"),
                                 "subscribers": int(subs) if subs and str(subs).isdigit() else None,
                                 "hidden": bool((it.get("statistics") or {}).get("hiddenSubscriberCount"))}
        return out

    def search(self, query: str, published_after: datetime, max_results: int = 15) -> list[dict[str, Any]]:
        data = self._api("search", {"part": "snippet", "type": "video", "order": "relevance", "q": query,
                                    "maxResults": max_results, "publishedAfter": iso(published_after),
                                    "safeSearch": "moderate"}, UNITS_SEARCH)
        out = []
        for it in data.get("items") or []:
            vid = (it.get("id") or {}).get("videoId")
            if vid:
                sn = it.get("snippet") or {}
                out.append({"video_id": vid, "channel_id": sn.get("channelId"), "title": sn.get("title")})
        return out
