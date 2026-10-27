"""Bild-Proxy mit Cache (SSRF-geschützt): Bilder von Meldungen werden einmal geholt und lokal gespeichert."""

from __future__ import annotations

import hashlib
import io
import ipaddress
import logging
import socket
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from app.context import AppContext
from app.util.timeutil import iso

log = logging.getLogger(__name__)

MAX_IMG_BYTES = 2 * 1024 * 1024
MAX_WIDTH = 800  # Karten zeigen die Bilder höchstens ~400 px breit (×2 für scharfe Displays)
SHRINK_ABOVE = 120_000  # Bytes: darunter (und nicht breiter als MAX_WIDTH) bleibt das Original
MAX_PIXELS = 40_000_000  # Schutz vor riesigen Bildern (Dekompressionsbombe)
ALLOWED_FORMATS = ("JPEG", "PNG", "WEBP")  # Bilder aus dem Netz: nur diese Decoder (kleinere Angriffsfläche in Pillow)
_tried: set[str] = set()  # nicht verkleinerbare Bilder in diesem Prozess nicht bei jedem Abruf neu prüfen


def shrink(data: bytes, ctype: str) -> tuple[bytes, str] | None:
    """Bild auf höchstens ``MAX_WIDTH`` Pixel Breite verkleinern und als WebP speichern (spart Datenvolumen).

    ``None`` = Original behalten: Animation/GIF, nicht lesbar, schon klein, zu groß oder Ergebnis nicht kleiner.
    """
    if ctype == "image/gif":
        return None
    try:
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(data), formats=ALLOWED_FORMATS) as im:  # nur gängige Web-Formate dekodieren
            width, height = im.size
            if width * height > MAX_PIXELS or getattr(im, "is_animated", False):
                return None
            if width <= MAX_WIDTH and len(data) <= SHRINK_ABOVE:
                return None
            im = ImageOps.exif_transpose(im)
            im.thumbnail((MAX_WIDTH, MAX_WIDTH * 2))
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA" if ("A" in im.getbands() or im.mode == "P") else "RGB")
            out = io.BytesIO()
            im.save(out, "WEBP", quality=80, method=4)
    except Exception as e:  # ein defektes Bild darf nie die Auslieferung verhindern
        log.debug("Bild nicht verkleinert: %s", e)
        return None
    new = out.getvalue()
    return (new, "image/webp") if 0 < len(new) < len(data) else None


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
    row = ctx.db.q1("SELECT path, content_type, size FROM img_cache WHERE key=?", (key,))
    if row is not None and Path(row["path"]).exists():
        path, ctype = Path(row["path"]), row["content_type"]
        if (row["size"] or 0) > SHRINK_ABOVE and ctype != "image/webp" and key not in _tried:
            _tried.add(key)  # früher gespeichertes Original: einmal verkleinern
            small = shrink(path.read_bytes(), ctype)
            if small is not None:
                data, ctype = small
                new_path = path.with_suffix(".webp")
                new_path.write_bytes(data)
                ctx.db.x("UPDATE img_cache SET path=?, content_type=?, size=? WHERE key=?",
                         (str(new_path), ctype, len(data), key))
                if new_path != path:
                    path.unlink(missing_ok=True)
                return new_path, ctype
        return path, ctype
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
        small = shrink(data, ctype)
        if small is not None:
            data, ctype = small
        ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
               "image/avif": ".avif"}.get(ctype, ".img")
        path = ctx.config.cache_dir / "img" / f"{key}{ext}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        ctx.db.x("INSERT OR REPLACE INTO img_cache(key, path, content_type, size, fetched_at) VALUES (?,?,?,?,?)",
                 (key, str(path), ctype, len(data), iso(datetime.now(UTC))))
        return path, ctype
    return None


__all__ = ["MAX_IMG_BYTES", "MAX_WIDTH", "cached_image", "shrink"]
