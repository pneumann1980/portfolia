"""Abruf fremder Webseiten mit SSRF-Schutz (Artikel-Volltext, Google-News-Auflösung).

Schutzmaßnahmen – alle im selben Modul, damit sie nicht umgangen werden können:

* nur ``http``/``https``, nur Port 80/443, keine Zugangsdaten in der URL, keine IP-Literale in Sonderbereichen;
* **DNS-Rebinding-Schutz:** Der Hostname wird einmal aufgelöst, alle Adressen werden geprüft (öffentlich, nicht
  privat/loopback/link-local/multicast/reserviert/CGNAT/IPv4-mapped). Die Verbindung geht anschließend **an die
  geprüfte Adresse** (Host-Header und TLS-SNI/Zertifikatsprüfung bleiben beim Hostnamen) – ein zweiter, abweichender
  DNS-Treffer kann nicht mehr eingeschleust werden;
* Weiterleitungen werden **einzeln** verfolgt (höchstens ``max_redirects``); jede Station durchläuft dieselbe Prüfung;
* Größen-, Zeit- und Content-Type-Grenzen: die Antwort wird gestreamt und bei Überschreitung abgebrochen;
* keine Cookies, keine Anmeldung, keine Umgehung von Bezahlschranken oder Bot-Sperren.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx

MAX_BYTES = 2_500_000
MAX_REDIRECTS = 5
TIMEOUT_S = 15.0
USER_AGENT = "Mozilla/5.0 (compatible; Portfolia/1.0; +https://github.com/pneumann1980/portfolia)"
TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "application/xml", "text/xml")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")

Resolver = Callable[[str, int], list[str]]


class FetchError(Exception):
    """Abruf nicht möglich; ``code`` ist ein stabiler Kurzgrund (kein Text der Gegenstelle)."""

    def __init__(self, code: str, status: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass
class Fetched:
    url: str  # Endadresse nach Weiterleitungen
    status: int
    content_type: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)
    redirects: int = 0

    def text(self) -> str:
        enc = None
        ct = self.headers.get("content-type", "")
        if "charset=" in ct:
            enc = ct.split("charset=", 1)[1].split(";")[0].strip().strip("\"'")
        for candidate in (enc, "utf-8", "latin-1"):
            if not candidate:
                continue
            try:
                return self.body.decode(candidate)
            except (LookupError, UnicodeDecodeError):
                continue
        return self.body.decode("utf-8", "replace")


def public_ip(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped is not None:
        a = a.ipv4_mapped
    if isinstance(a, ipaddress.IPv6Address) and a.sixtofour is not None:
        a = a.sixtofour
    if (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_reserved or a.is_unspecified
            or a.is_global is False):
        return False
    return not (isinstance(a, ipaddress.IPv4Address) and a in _CGNAT)


def system_resolver(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        raise FetchError("dns") from e
    return sorted({str(i[4][0]) for i in infos})


def check_url(url: str) -> tuple[str, str, int]:
    """Syntaxprüfung; liefert (Schema, Host, Port) oder wirft ``FetchError('url')``."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as e:
        raise FetchError("url") from e
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise FetchError("url")
    port = port or (443 if parts.scheme == "https" else 80)
    if port not in (80, 443):
        raise FetchError("port")
    return parts.scheme, parts.hostname.lower().rstrip("."), port


def _ip_literal(host: str) -> str | None:
    try:
        return str(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        return None


def resolve_public(host: str, port: int, resolver: Resolver) -> str:
    """Eine geprüfte öffentliche Adresse für ``host``; wirft ``FetchError('blocked')``, wenn eine Adresse nicht
    öffentlich ist (auch gemischte Antworten werden abgelehnt)."""
    literal = _ip_literal(host)
    addrs = [literal] if literal else resolver(host, port)
    if not addrs:
        raise FetchError("dns")
    if not all(public_ip(a) for a in addrs):
        raise FetchError("blocked")
    return addrs[0]


class SafeFetcher:
    """Hält pro Aufruf Weiterleitungen, Größe und Zeit im Zaum. ``client`` darf ein Mock-Transport-Client sein."""

    def __init__(self, client: httpx.Client | None = None, resolver: Resolver | None = None,
                 max_bytes: int = MAX_BYTES, max_redirects: int = MAX_REDIRECTS, timeout_s: float = TIMEOUT_S,
                 pin: bool = True) -> None:
        # trust_env=False: keine Proxy-Umgebung – sonst wäre das Anpinnen der geprüften Adresse wirkungslos
        self.client = client or httpx.Client(timeout=httpx.Timeout(timeout_s, connect=8.0), follow_redirects=False,
                                             trust_env=False, headers={"User-Agent": USER_AGENT})
        self.resolver = resolver or system_resolver
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.timeout_s = timeout_s
        self.pin = pin

    def post(self, url: str, data: dict[str, str], *, headers: dict[str, str] | None = None) -> Fetched:
        """Formular-POST ohne Weiterleitungen (nur für die Google-News-Auflösung)."""
        return self.get(url, headers=headers, allowed_types=(), data=data, max_redirects=0)

    def get(self, url: str, *, accept: str = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
            headers: dict[str, str] | None = None, allowed_types: tuple[str, ...] = TEXT_TYPES,
            data: dict[str, str] | None = None, max_redirects: int | None = None) -> Fetched:
        current = url
        limit = self.max_redirects if max_redirects is None else max_redirects
        deadline = time.monotonic() + self.timeout_s * 2
        for hop in range(limit + 1):
            scheme, host, port = check_url(current)
            ip = resolve_public(host, port, self.resolver)
            target, hdrs, ext = self._target(current, scheme, host, port, ip, accept, headers)
            try:
                with self.client.stream("POST" if data is not None else "GET", target, headers=hdrs, extensions=ext,
                                        data=data, timeout=self.timeout_s) as r:
                    if r.status_code in (301, 302, 303, 307, 308):
                        loc = r.headers.get("location")
                        if not loc:
                            raise FetchError("redirect", r.status_code)
                        if hop >= limit:
                            raise FetchError("too_many_redirects", r.status_code)
                        current = urljoin(current, loc)
                        continue
                    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                    if r.status_code >= 400:
                        raise FetchError(self._status_code(r.status_code), r.status_code)
                    if ctype and allowed_types and ctype not in allowed_types:
                        raise FetchError("content_type", r.status_code)
                    declared = r.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > self.max_bytes:
                        raise FetchError("too_large", r.status_code)
                    buf = bytearray()
                    for chunk in r.iter_bytes():
                        buf += chunk
                        if len(buf) > self.max_bytes:
                            raise FetchError("too_large", r.status_code)
                        if time.monotonic() > deadline:
                            raise FetchError("timeout", r.status_code)
                    return Fetched(url=current, status=r.status_code, content_type=ctype, body=bytes(buf),
                                   headers={k.lower(): v for k, v in r.headers.items()}, redirects=hop)
            except FetchError:
                raise
            except httpx.TimeoutException as e:
                raise FetchError("timeout") from e
            except httpx.HTTPError as e:
                raise FetchError("network") from e
        raise FetchError("too_many_redirects")

    def _target(self, url: str, scheme: str, host: str, port: int, ip: str, accept: str,
                headers: dict[str, str] | None) -> tuple[str, dict[str, str], dict[str, str]]:
        hdrs = {"Accept": accept, "Accept-Language": "de,en;q=0.8", **(headers or {})}
        if not self.pin or _ip_literal(host):
            return url, hdrs, {}
        parts = urlsplit(url)
        netloc = f"[{ip}]" if ":" in ip else ip
        if (scheme, port) not in (("https", 443), ("http", 80)):
            netloc += f":{port}"
        hdrs["Host"] = host if port in (80, 443) else f"{host}:{port}"
        target = parts._replace(netloc=netloc).geturl()
        return target, hdrs, ({"sni_hostname": host} if scheme == "https" else {})

    @staticmethod
    def _status_code(status: int) -> str:
        if status in (401, 402, 403, 451):
            return "blocked_http"  # Bezahlschranke / Bot-Sperre / rechtlich gesperrt: nicht umgehen
        if status == 404 or status == 410:
            return "gone"
        if status == 429:
            return "rate_limited"
        return "http_error"


__all__ = ["FetchError", "Fetched", "SafeFetcher", "check_url", "public_ip", "resolve_public", "system_resolver"]
