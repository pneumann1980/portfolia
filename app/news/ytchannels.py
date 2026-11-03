"""YouTube-Kanäle selbst hinzufügen: Suche nach Name, @Handle, Kanal- oder Video-Link; Prüfung vor dem Speichern.

Eingaben (``parse_input``)
    * Channel-ID ``UC…`` oder Link ``youtube.com/channel/UC…`` → direkt,
    * ``@handle`` oder Link ``youtube.com/@handle`` → Auflösung wie bei konfigurierten Kanälen (API ``forHandle`` bzw.
      Kanalseite),
    * Video-Link (``watch?v=``, ``youtu.be/``, ``/shorts/``, ``/live/``) → Kanal über oEmbed (ohne Schlüssel),
    * sonst Freitext → Suche: mit ``YOUTUBE_API_KEY`` ``search?type=channel`` (100 Einheiten, nur im Tagesbudget der
      Entdeckung), sonst die YouTube-Ergebnisseite (Filter „Kanäle“, ``ytInitialData``). Die Ergebnisseite ist kein
      dokumentierter Vertrag – fällt sie aus, bleiben Handle und Links als Weg.

Vor dem Speichern wird der Kanal-Feed (RSS) einmal abgerufen: nur ein Kanal, dessen Feed existiert, wird eingetragen
(Name aus dem Feed). Eingetragen wird in ``sources.yaml`` (Gruppe „Eigene“, bestätigt); ein zuvor ignorierter oder
blockierter Kanal wird dabei freigegeben. Übertragen werden nur die Sucheingabe bzw. Kanal-/Video-IDs.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from app.news.youtube import _CID, CONSENT_HEADERS, UNITS_SEARCH, parse_count
from app.util.http import HttpError, request_with_retry

log = logging.getLogger(__name__)

GROUP = "Eigene"
MAX_RESULTS = 8
CHANNEL_ID = re.compile(rf"^{_CID}$")
HANDLE = re.compile(r"^@[A-Za-z0-9._-]{3,30}$")
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_SEARCH_FILTER_CHANNELS = "EgIQAg=="  # Ergebnisseite: nur Kanäle


class ChannelError(Exception):
    """Für den Nutzer verständlicher Fehler (Eingabe, Kanal nicht gefunden, YouTube nicht erreichbar)."""


def parse_input(text: str) -> tuple[str, str]:
    """Eingabe einordnen: ``("channel", UC…)``, ``("handle", "@x")``, ``("video", id)`` oder ``("text", q)``."""
    q = " ".join((text or "").split())
    if not q:
        raise ChannelError("Bitte einen Kanalnamen, @Handle oder Link eingeben.")
    if len(q) > 200:
        raise ChannelError("Eingabe zu lang (höchstens 200 Zeichen).")
    if CHANNEL_ID.match(q):
        return "channel", q
    if q.startswith("@") and HANDLE.match(q):
        return "handle", q
    if re.match(r"^(https?://)?([\w-]+\.)?(youtube\.com|youtu\.be)/", q, re.IGNORECASE):
        url = urlsplit(q if "://" in q else "https://" + q)
        host = (url.hostname or "").lower()
        parts = [p for p in url.path.split("/") if p]
        if host.endswith("youtu.be") and parts and VIDEO_ID.match(parts[0]):
            return "video", parts[0]
        if parts and parts[0] == "channel" and len(parts) > 1 and CHANNEL_ID.match(parts[1]):
            return "channel", parts[1]
        if parts and parts[0].startswith("@") and HANDLE.match(parts[0]):
            return "handle", parts[0]
        v = (parse_qs(url.query).get("v") or [""])[0]
        if parts and parts[0] == "watch" and VIDEO_ID.match(v):
            return "video", v
        if len(parts) > 1 and parts[0] in ("shorts", "live", "embed") and VIDEO_ID.match(parts[1]):
            return "video", parts[1]
        raise ChannelError("Diesen YouTube-Link kann ich keinem Kanal zuordnen – bitte Kanal-Link, @Handle oder "
                           "Video-Link verwenden.")
    return "text", q


# ----------------------------------------------------------------------------------------------------
# Ergebnisseite ohne API-Schlüssel
# ----------------------------------------------------------------------------------------------------

def _initial_data(html: str) -> Any:
    m = re.search(r"(?:var ytInitialData|window\[\"ytInitialData\"\])\s*=\s*", html)
    if not m:
        return None
    try:
        data, _end = json.JSONDecoder().raw_decode(html, m.end())
    except ValueError:
        return None
    return data


def _text(v: Any) -> str:
    if not isinstance(v, dict):
        return ""
    if isinstance(v.get("simpleText"), str):
        return v["simpleText"]
    return "".join(str(r.get("text") or "") for r in v.get("runs") or [] if isinstance(r, dict))


def _walk(node: Any, key: str, out: list[Any], depth: int = 0) -> None:
    if depth > 40:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key and isinstance(v, dict):
                out.append(v)
            else:
                _walk(v, key, out, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _walk(v, key, out, depth + 1)


def parse_search_page(html: str) -> list[dict[str, Any]]:
    """Kanäle aus der Ergebnisseite (``channelRenderer``). Im aktuellen Layout steht der Handle in
    ``subscriberCountText`` und die Abonnentenzahl in ``videoCountText`` – beide Felder werden geprüft."""
    data = _initial_data(html)
    found: list[Any] = []
    _walk(data, "channelRenderer", found)
    out: list[dict[str, Any]] = []
    for r in found:
        cid = r.get("channelId")
        if not isinstance(cid, str) or not CHANNEL_ID.match(cid) or any(x["channel_id"] == cid for x in out):
            continue
        base = (((r.get("navigationEndpoint") or {}).get("browseEndpoint") or {}).get("canonicalBaseUrl") or "")
        texts = [_text(r.get("subscriberCountText")), _text(r.get("videoCountText"))]
        handle = next((t for t in [base.lstrip("/"), *texts] if HANDLE.match(t or "")), None)
        subs = next((n for n in (parse_count(t) for t in texts) if n), None)
        out.append({"channel_id": cid, "title": _text(r.get("title")) or cid, "handle": handle, "subscribers": subs,
                    "description": _text(r.get("descriptionSnippet"))[:200]})
    return out[:MAX_RESULTS]


def parse_feed_title(data: bytes) -> str | None:
    """Kanalname aus dem Atom-Feed eines Kanals (``feed.title`` bzw. Autor); ``None``, wenn es kein Feed ist."""
    import feedparser

    d = feedparser.parse(data)
    if (d.get("bozo") and not d.get("feed")) or "atom" not in str(d.get("version") or ""):
        return None
    feed = d.get("feed") or {}
    return str(feed.get("title") or feed.get("author") or "").strip()[:120]


# ----------------------------------------------------------------------------------------------------
# Suche
# ----------------------------------------------------------------------------------------------------

def _from_handle(svc: Any, handle: str) -> dict[str, Any]:
    res = svc.yt.resolve(handle)
    if res.status == "ok" and res.channel_id:
        svc.yt.store_resolved(res)
        return {"channel_id": res.channel_id, "title": res.title or handle, "handle": res.handle,
                "subscribers": res.subscribers, "description": ""}
    if res.status == "unresolvable":
        raise ChannelError(f"Kanal {handle} nicht gefunden ({res.error or 'unbekannt'}).")
    raise ChannelError(f"YouTube gerade nicht erreichbar – bitte später erneut versuchen ({res.error or ''}).".strip())


def _from_video(svc: Any, vid: str) -> dict[str, Any]:
    try:
        r = request_with_retry(svc.http, "GET", "https://www.youtube.com/oembed",
                               params={"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"},
                               limiter=svc.yt.limiter, retries=1)
        data = r.json()
    except HttpError as e:
        raise ChannelError("Video nicht gefunden oder nicht öffentlich." if e.status in (400, 401, 403, 404) else
                           "YouTube gerade nicht erreichbar – bitte später erneut versuchen.") from None
    except (httpx.HTTPError, ValueError):
        raise ChannelError("YouTube gerade nicht erreichbar – bitte später erneut versuchen.") from None
    url = str(data.get("author_url") or "")
    kind, val = parse_input(url) if url else ("", "")
    if kind == "channel":
        return {"channel_id": val, "title": str(data.get("author_name") or val), "handle": None, "subscribers": None,
                "description": ""}
    if kind == "handle":
        return _from_handle(svc, val)
    raise ChannelError("Zum Video ließ sich kein Kanal ermitteln.")


def _api_search(svc: Any, q: str) -> list[dict[str, Any]]:
    data = svc.yt._api("search", {"part": "snippet", "type": "channel", "q": q, "maxResults": MAX_RESULTS},
                       UNITS_SEARCH)
    out = []
    for it in data.get("items") or []:
        cid = (it.get("id") or {}).get("channelId") or (it.get("snippet") or {}).get("channelId")
        if isinstance(cid, str) and CHANNEL_ID.match(cid):
            sn = it.get("snippet") or {}
            out.append({"channel_id": cid, "title": sn.get("title") or cid, "handle": None, "subscribers": None,
                        "description": str(sn.get("description") or "")[:200]})
    try:
        stats = svc.yt.channel_stats([x["channel_id"] for x in out])
    except (HttpError, httpx.HTTPError) as e:
        log.info("YouTube-Kanalstatistik nicht verfügbar: %s", e)
        stats = {}
    for x in out:
        st = stats.get(x["channel_id"]) or {}
        h = st.get("handle")
        x["handle"] = h if h and HANDLE.match(str(h)) else (("@" + h) if h and HANDLE.match("@" + str(h)) else None)
        x["subscribers"] = st.get("subscribers")
    return out


def _page_search(svc: Any, q: str) -> list[dict[str, Any]]:
    try:
        r = request_with_retry(svc.http, "GET", "https://www.youtube.com/results",
                               params={"search_query": q, "sp": _SEARCH_FILTER_CHANNELS,
                                       "hl": "en"},
                               headers=CONSENT_HEADERS, limiter=svc.yt.limiter, retries=1)
    except (HttpError, httpx.HTTPError) as e:
        log.info("YouTube-Ergebnisseite nicht erreichbar: %s", e)
        raise ChannelError("Die YouTube-Suche ist gerade nicht erreichbar – bitte @Handle oder einen Kanal- bzw. "
                           "Video-Link eingeben.") from None
    if "consent.youtube.com" in str(r.url):
        raise ChannelError("YouTube zeigt statt der Suche eine Zustimmungsseite – bitte @Handle oder einen Kanal- "
                           "bzw. Video-Link eingeben.")
    return parse_search_page(r.text)


def search(svc: Any, text: str) -> dict[str, Any]:
    """Kanäle zur Eingabe; Ergebnis mit Hinweis auf den Weg (direkt, API, Ergebnisseite) und dem Status je Kanal."""
    kind, val = parse_input(text)
    via = "direkt"
    if kind == "channel":
        results = [{"channel_id": val, "title": val, "handle": None, "subscribers": None, "description": ""}]
        results[0]["title"] = verify(svc, val)
    elif kind == "handle":
        results = [_from_handle(svc, val)]
    elif kind == "video":
        results = [_from_video(svc, val)]
    else:
        budget = int(svc.cfg.discovery().get("daily_unit_budget", 2500))
        if svc.yt.api_key and svc.yt.units_used() + UNITS_SEARCH + 1 <= budget:
            via = "YouTube Data API"
            try:
                results = _api_search(svc, val)
            except (HttpError, httpx.HTTPError, RuntimeError) as e:
                log.info("YouTube-Kanalsuche über die API fehlgeschlagen, versuche Ergebnisseite: %s", e)
                via, results = "YouTube-Suche", _page_search(svc, val)
        else:
            via, results = "YouTube-Suche", _page_search(svc, val)
    states = svc.channel_state()
    active = {st["channel_id"] for st in states if st.get("channel_id") and st["channel"].active}
    inactive = {st["channel_id"] for st in states if st.get("channel_id")} - active
    handles = {str(st["channel"].handle).lower(): st["channel"].active for st in states if st["channel"].handle}
    blocked, ignored = svc.cfg.blocked_ids(), svc.cfg.ignored_ids()
    for x in results:
        cid = x["channel_id"]
        known = handles.get(str(x.get("handle") or "").lower())  # konfiguriert, aber (noch) nicht aufgelöst
        if known is not None:
            (active if known else inactive).add(cid)
        x["state"] = ("subscribed" if cid in active else "blocked" if cid in blocked else
                      "inactive" if cid in inactive else "ignored" if cid in ignored else "new")
    return {"query": text, "kind": kind, "via": via, "results": results}


def verify(svc: Any, channel_id: str) -> str:
    """Kanal-Feed einmal abrufen: existiert der Kanal? Liefert den Kanalnamen."""
    if not CHANNEL_ID.match(channel_id or ""):
        raise ChannelError("Ungültige Kanal-ID.")
    try:
        data = svc.yt.feed(channel_id)
    except HttpError as e:
        raise ChannelError("Kanal nicht gefunden (kein Feed)." if e.status == 404 else
                           "YouTube gerade nicht erreichbar – bitte später erneut versuchen.") from None
    except httpx.HTTPError:
        raise ChannelError("YouTube gerade nicht erreichbar – bitte später erneut versuchen.") from None
    title = parse_feed_title(data)
    if title is None:
        raise ChannelError("Die Antwort von YouTube ist kein Kanal-Feed – Kanal nicht hinzugefügt.")
    return title or channel_id


def add(svc: Any, channel_id: str, handle: str | None, language: str) -> dict[str, Any]:
    """Kanal prüfen und eintragen (Gruppe „Eigene“, bestätigt); ignoriert/blockiert wird aufgehoben."""
    title = verify(svc, channel_id)
    handle = handle if handle and HANDLE.match(handle) else None
    language = language if language in ("de", "en") else "en"
    unblocked = channel_id in svc.cfg.blocked_ids()
    svc.cfg.add_channel(channel_id, title, handle, language=language, group=GROUP)
    svc.cfg.release_channel(channel_id)
    db = svc.db
    db.x("UPDATE yt_suggestion SET status='subscribed' WHERE channel_id=?", (channel_id,))
    db.x("UPDATE news_item SET source_weight=0.6 WHERE channel_id=? AND source_id='yt:discovery'", (channel_id,))
    if unblocked:
        db.x("UPDATE news_item SET hidden_reason=NULL WHERE channel_id=? AND hidden_reason='Kanal blockiert'",
             (channel_id,))
    log.info("YouTube-Kanal selbst hinzugefügt: %s (%s)", channel_id, handle or "ohne Handle")
    return {"channel_id": channel_id, "title": title, "handle": handle, "unblocked": unblocked}
