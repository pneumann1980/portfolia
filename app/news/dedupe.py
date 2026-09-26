"""Deduplizierung: normalisierte URL (Tracking-Parameter entfernt) und Titelähnlichkeit."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = {"fbclid", "gclid", "dclid", "mc_cid", "mc_eid", "ref", "ref_src", "cmpid", "ocid", "guccounter",
                   "guce_referrer", "guce_referrer_sig", "soc_src", "soc_trk", "_hsenc", "_hsmi", "ncid", "sr_share",
                   "src", "cid", "at_medium", "at_campaign", "feature", "igshid", "yptr", "tsrc", "via", "rss"}

STOPWORDS = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "with", "at", "by", "from",
             "der", "die", "das", "und", "oder", "von", "zu", "im", "mit", "für", "auf", "ist", "sind", "den",
             "dem", "des", "ein", "eine", "als", "bei", "nach"}


def normalize_url(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m.") and host.count(".") >= 2:
        host = host[2:]
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
             if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS]
    # YouTube: nur die Video-ID zählt
    if host in ("youtube.com", "youtu.be"):
        vid = dict(query).get("v") or (parts.path.strip("/") if host == "youtu.be" else None)
        if vid:
            return f"youtube:{vid}"
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(sorted(query)), ""))


def strip_publisher(title: str) -> str:
    """Google News hängt „ - Verlag“ an; für den Vergleich entfernen."""
    return re.sub(r"\s+[-–|]\s+[^-–|]{2,60}$", "", title.strip())


def normalize_title(title: str) -> str:
    t = unicodedata.normalize("NFKD", strip_publisher(title)).encode("ascii", "ignore").decode("ascii").lower()
    t = re.sub(r"[^a-z0-9$%]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def tokens(title_norm: str) -> set[str]:
    return {w for w in title_norm.split() if w not in STOPWORDS and len(w) > 1}


def similar(a: set[str], b: set[str], threshold: float = 0.8) -> bool:
    if len(a) < 4 or len(b) < 4:
        return a == b and bool(a)
    inter = len(a & b)
    union = len(a | b)
    return union > 0 and inter / union >= threshold


class TitleIndex:
    """Titel der letzten Tage im Speicher für den Ähnlichkeitsvergleich."""

    def __init__(self, window: timedelta = timedelta(hours=48)) -> None:
        self.window = window
        self.items: list[tuple[int, set[str], datetime]] = []

    def add(self, item_id: int, title_norm: str, published: datetime) -> None:
        self.items.append((item_id, tokens(title_norm), published))

    def find(self, title_norm: str, published: datetime) -> int | None:
        tk = tokens(title_norm)
        for item_id, other, pub in self.items:
            if abs(pub - published) <= self.window and similar(tk, other):
                return item_id
        return None
