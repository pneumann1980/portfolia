"""Google-News-Weiterleitungslinks (``news.google.com/rss/articles/<id>``) auf die Originaladresse auflösen.

Zwei Wege, beide ohne Anmeldung und ohne Umgehung von Schranken:

1. **Offline:** Ältere Links tragen die Zieladresse Base64-kodiert in der Kennung – dann ist kein Abruf nötig.
2. **Online (best effort):** Neuere Kennungen (``AU_yqL…``) lösen die Google-Seiten selbst über ihre
   ``batchexecute``-Schnittstelle auf (Signatur und Zeitstempel stehen im HTML der Artikelseite). Sie ist
   nicht dokumentiert und kann sich ändern; schlägt sie fehl, bleibt der Artikel bei seinem Anreißer
   (``gnews_unresolved``) und wird später begrenzt erneut versucht.
"""

from __future__ import annotations

import base64
import json
import re
from urllib.parse import quote, urlsplit

from app.news.safefetch import FetchError, SafeFetcher

GNEWS_HOSTS = ("news.google.com",)
_ID = re.compile(r"^/(?:rss/)?(?:articles|read)/([A-Za-z0-9_\-]+)")
_SG = re.compile(r'data-n-a-sg="([^"]+)"')
_TS = re.compile(r'data-n-a-ts="([^"]+)"')
BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"


def is_gnews(url: str) -> bool:
    try:
        return (urlsplit(url).hostname or "").lower() in GNEWS_HOSTS and _ID.match(urlsplit(url).path) is not None
    except ValueError:
        return False


def article_id(url: str) -> str | None:
    m = _ID.match(urlsplit(url).path)
    return m.group(1) if m else None


def decode_offline(gid: str) -> str | None:
    try:
        raw = base64.urlsafe_b64decode(gid + "=" * (-len(gid) % 4))
    except (ValueError, TypeError):
        return None
    if raw.startswith(b"\x08\x13\x22"):
        raw = raw[3:]
    if raw.endswith(b"\xd2\x01\x00"):
        raw = raw[:-3]
    if not raw:
        return None
    n = raw[0]
    if n >= 0x80:
        if len(raw) < 2:
            return None
        n, raw = (raw[0] & 0x7F) | (raw[1] << 7), raw[2:]
    else:
        raw = raw[1:]
    text = raw[:n].decode("utf-8", "ignore")
    return text if re.match(r"https?://", text) else None


def _request_body(gid: str, ts: str, sg: str) -> str:
    inner = json.dumps(["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None,
                                         None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                        gid, int(ts), sg], separators=(",", ":"))
    return json.dumps([[["Fbv4je", inner, None, "generic"]]], separators=(",", ":"))


def _parse_batch(text: str) -> str | None:
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("[["):
            continue
        try:
            data = json.loads(line)
            for row in data:
                if len(row) > 2 and row[0] == "wrb.fr" and isinstance(row[2], str):
                    inner = json.loads(row[2])
                    if len(inner) > 1 and isinstance(inner[1], str) and inner[1].startswith("http"):
                        return inner[1]
        except (ValueError, TypeError):
            continue
    return None


def resolve(url: str, fetcher: SafeFetcher) -> str:
    """Originaladresse oder ``FetchError('gnews_unresolved')``. Nur Google-Hosts werden angefragt."""
    gid = article_id(url)
    if not gid:
        raise FetchError("gnews_unresolved")
    direct = decode_offline(gid)
    if direct:
        return direct
    try:
        page = fetcher.get(f"https://news.google.com/rss/articles/{quote(gid)}?hl=en-US&gl=US&ceid=US:en")
        html = page.text()
        sg, ts = _SG.search(html), _TS.search(html)
        if not sg or not ts:
            raise FetchError("gnews_unresolved")
        body = fetcher.post(BATCH_URL, {"f.req": _request_body(gid, ts.group(1), sg.group(1))},
                            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    except FetchError as e:
        if e.code in ("timeout", "network", "rate_limited"):
            raise
        raise FetchError("gnews_unresolved") from e
    target = _parse_batch(body.text())
    if not target:
        raise FetchError("gnews_unresolved")
    return target


__all__ = ["article_id", "decode_offline", "is_gnews", "resolve"]
