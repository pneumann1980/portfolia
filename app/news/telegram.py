"""Telegram-Kanäle über einen eigenen RSS-Bridge-/RSSHub-Feed: Eingabe erkennen, Feed-URL bauen, Quelle anlegen."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from app.news.config import SourcesConfig
from app.news.parse import parse_feed

HANDLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")  # Telegram: 5–32 Zeichen, beginnt mit Buchstabe


class TelegramInputError(ValueError):
    """Eingabe unbrauchbar; die Meldung ist für die Oberfläche gedacht."""


def parse_channel_input(text: str) -> tuple[str, str]:
    """Rückgabe ``(handle, feed_url)``; genau eines von beiden ist gefüllt.

    Erkennt ``@Name``, ``Name``, ``t.me/Name``, ``t.me/s/Name`` (Handle) und eine vollständige http(s)-Feed-URL."""
    s = (text or "").strip()
    if not s:
        raise TelegramInputError("Bitte einen Kanal angeben (@Name, t.me-Link oder Feed-URL).")
    if len(s) > 500:
        raise TelegramInputError("Eingabe zu lang.")
    low = s.lower()
    if low.startswith(("http://", "https://")) or "t.me/" in low or "telegram.me/" in low:
        parts = urlsplit(s if "://" in s else "https://" + s)
        host = (parts.hostname or "").lower()
        if host in ("t.me", "telegram.me", "www.t.me"):
            segs = [x for x in parts.path.split("/") if x]
            if segs and segs[0] == "s":
                segs = segs[1:]
            if not segs:
                raise TelegramInputError("Im t.me-Link fehlt der Kanalname.")
            return _handle(segs[0]), ""
        if parts.scheme in ("http", "https") and host:
            return "", s
        raise TelegramInputError("Die Adresse ist keine gültige http(s)-URL.")
    return _handle(s), ""


def _handle(raw: str) -> str:
    h = raw.strip().lstrip("@")
    if not HANDLE_RE.match(h):
        raise TelegramInputError(
            f"„{raw.strip()[:40]}“ ist kein gültiger Kanalname (5–32 Zeichen: Buchstaben, Ziffern, Unterstrich).")
    return h


def clean_base(base: str) -> str:
    """Bridge-Adresse prüfen und normalisieren (``http://host:port``, ohne Pfad/Query)."""
    s = (base or "").strip()
    if not s:
        raise TelegramInputError("Bitte die Adresse Ihrer RSS-Bridge angeben (z. B. http://192.168.1.10:7575).")
    if "://" not in s:
        s = "http://" + s
    parts = urlsplit(s)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if parts.scheme not in ("http", "https") or not parts.hostname or port == -1:
        raise TelegramInputError("Die Bridge-Adresse ist ungültig (Beispiel: http://192.168.1.10:7575).")
    return f"{parts.scheme}://{parts.netloc}"


def feed_url(base: str, handle: str) -> str:
    """RSS-Bridge (``TelegramBridge``, Format Atom)."""
    return f"{clean_base(base)}/?action=display&bridge=TelegramBridge&username=%40{quote(handle)}&format=Atom"


def handle_from_url(url: str) -> str:
    """Kanalname aus einer Bridge-URL (``username=%40Name``); leer, wenn nicht erkennbar."""
    q = parse_qs(urlsplit(url).query)
    for key in ("username", "channel", "user"):
        if q.get(key):
            h = q[key][0].lstrip("@")
            if HANDLE_RE.match(h):
                return h
    return ""


def source_id(handle: str) -> str:
    return "tg_" + re.sub(r"[^a-z0-9_]", "", handle.lower())


def check_feed(http: httpx.Client, url: str) -> tuple[int, str]:
    """Feed einmal abrufen und lesen. Rückgabe ``(Beiträge, erster Titel)``; sonst ``TelegramInputError``."""
    try:
        r = http.get(url, timeout=15, follow_redirects=True, headers={"Accept": "application/atom+xml, */*"})
    except httpx.HTTPError as e:
        raise TelegramInputError(
            f"Feed nicht erreichbar ({type(e).__name__}). Läuft die Bridge und erreicht Portfolia sie?") from None
    if r.status_code != 200:
        raise TelegramInputError(f"Die Bridge antwortet mit HTTP {r.status_code}.")
    items, err = parse_feed(r.content, 60, telegram=True)
    if err:
        raise TelegramInputError(err)
    if not items:
        raise TelegramInputError("Der Feed ist erreichbar, enthält aber keine lesbaren Beiträge (Kanal öffentlich?).")
    return len(items), items[0].title


def add_telegram_source(cfg: SourcesConfig, http: httpx.Client, *, channel: str, bridge: str, name: str,
                        language: str, weight: float, require_match: bool, ai: bool) -> dict[str, Any]:
    """Prüft Eingabe und Feed und trägt die Quelle in ``sources.yaml`` ein. Rückgabe: Ergebnis für die Meldung."""
    handle, url = parse_channel_input(channel)
    if not url:
        url = feed_url(bridge, handle)
        base = clean_base(bridge)
    else:
        handle = handle_from_url(url)
        base = ""
    if not handle:  # Feed-URL ohne erkennbaren Kanalnamen: aus Host/Pfad ableiten
        parts = urlsplit(url)
        raw = (parts.hostname or "feed") + (parts.path or "")
        handle = re.sub(r"[^A-Za-z0-9_]", "_", raw).strip("_")[:32] or "feed"
    sid = source_id(handle)
    existing = cfg.sources()
    if any(s.id == sid or s.url == url for s in existing):
        raise TelegramInputError("Dieser Kanal ist bereits als Quelle eingetragen.")
    count, first = check_feed(http, url)
    label = (name or "").strip()[:80] or f"Telegram · {handle}"
    cfg.add_source({
        "id": sid, "name": label, "type": "telegram", "url": url,
        "language": language if language in ("en", "de") else "en", "weight": max(0.0, min(1.0, weight)),
        "require_match": require_match, "ai": ai, "active": True,
    })
    return {"id": sid, "name": label, "count": count, "first": first, "base": base}


__all__ = ["TelegramInputError", "add_telegram_source", "clean_base", "feed_url", "handle_from_url",
           "parse_channel_input", "source_id"]
