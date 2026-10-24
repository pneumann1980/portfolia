"""Bild-Proxy mit Cache (SSRF-geschützt): Bilder von Meldungen werden einmal geholt und lokal gespeichert."""

from __future__ import annotations

import hashlib
import ipaddress
import socket
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from app.context import AppContext
from app.util.timeutil import iso

MAX_IMG_BYTES = 2 * 1024 * 1024


def _public_host(host: str, port: int) -> bool:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
                or ip.is_unspecified):
            return False
    return bool(infos)


def cached_image(ctx: AppContext, url: str, allow: frozenset[tuple[str, int]] = frozenset()) -> tuple[Path, str] | None:
    """Bild holen und cachen. ``allow``: ausdrücklich erlaubte (Host, Port)-Paare, z. B. der eigene Feed-Server der
    Quelle (sonst nur öffentliche Hosts auf Port 80/443)."""
    key = hashlib.sha256(url.encode()).hexdigest()[:40]
    row = ctx.db.q1("SELECT path, content_type FROM img_cache WHERE key=?", (key,))
    if row is not None and Path(row["path"]).exists():
        return Path(row["path"]), row["content_type"]
    current = url
    for _ in range(4):
        parts = urlsplit(current)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        port = parts.port or (443 if parts.scheme == "https" else 80)
        trusted = (parts.hostname.lower(), port) in allow
        if not trusted and (port not in (80, 443) or not _public_host(parts.hostname, port)):
            return None
        try:
            with ctx.http.stream("GET", current, follow_redirects=False, timeout=10,
                                 headers={"Accept": "image/avif,image/webp,image/*"}) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    current = urljoin(current, r.headers["location"])
                    continue
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                if r.status_code != 200 or not ctype.startswith("image/") or "svg" in ctype:
                    return None
                data = b""
                for chunk in r.iter_bytes():
                    data += chunk
                    if len(data) > MAX_IMG_BYTES:
                        return None
        except httpx.HTTPError:
            return None
        ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
               "image/avif": ".avif"}.get(ctype, ".img")
        path = ctx.config.cache_dir / "img" / f"{key}{ext}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        ctx.db.x("INSERT OR REPLACE INTO img_cache(key, path, content_type, size, fetched_at) VALUES (?,?,?,?,?)",
                 (key, str(path), ctype, len(data), iso(datetime.now(UTC))))
        return path, ctype
    return None


__all__ = ["MAX_IMG_BYTES", "cached_image"]
