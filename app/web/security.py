"""Sicherheit: Security-Header/CSP, optionale Basic-Auth, CSRF-Schutz für schreibende Aktionen.

* CSP ohne externe Quellen; Ausnahme: YouTube-Vorschaubilder (i.ytimg.com).
* Basic-Auth (AUTH_MODE=basic): AUTH_USER + AUTH_PASSWORD_HASH (bcrypt ``$2b$…`` oder
  ``pbkdf2_sha256$<iter>$<salt>$<hash>``). Fehlversuche werden je Client gebremst.
* CSRF: Double-Submit-Cookie; POST/PUT/DELETE benötigen Header ``X-CSRF-Token`` (HTMX setzt ihn
  automatisch) oder Formularfeld ``csrf_token``; zusätzlich Ablehnung von Cross-Site-Requests.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
from collections import defaultdict
from urllib.parse import parse_qs

from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)

CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "  # ECharts setzt Inline-Styles (Tooltips)
    "img-src 'self' data: blob: https://i.ytimg.com; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "object-src 'none'"
)
CSRF_COOKIE = "portfolia_csrf"
PUBLIC_PATHS = ("/healthz",)


def hash_password(password: str, iterations: int = 390000) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not stored:
        return False
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, it, salt, digest = stored.split("$", 3)
            dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(it))
            return hmac.compare_digest(dk.hex(), digest)
        except (ValueError, TypeError):
            return False
    if stored.startswith(("$2a$", "$2b$", "$2y$")):
        try:
            import bcrypt

            s = stored.replace("$2y$", "$2b$", 1).encode()
            return bcrypt.checkpw(password.encode(), s)
        except (ValueError, ImportError):
            return False
    return False


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        h = response.headers
        h.setdefault("Content-Security-Policy", CSP)
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=(), payment=()")
        h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if request.url.path.startswith("/static/"):
            h.setdefault("Cache-Control", "public, max-age=86400")
        else:
            h.setdefault("Cache-Control", "no-store")
        return response


class BasicAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, user: str | None, password_hash: str | None) -> None:
        super().__init__(app)
        self.user = user or ""
        self.password_hash = password_hash or ""
        self._fails: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()
        if not self.user or not self.password_hash:
            log.error("AUTH_MODE=basic, aber AUTH_USER/AUTH_PASSWORD_HASH fehlen – Zugriff wird verweigert.")

    def _throttled(self, client: str) -> bool:
        now = time.monotonic()
        with self._lock:
            fails = [t for t in self._fails[client] if now - t < 300]
            self._fails[client] = fails
            return len(fails) >= 10

    def _fail(self, client: str) -> None:
        with self._lock:
            self._fails[client].append(time.monotonic())

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)
        client = request.client.host if request.client else "?"
        if self._throttled(client):
            return PlainTextResponse("Zu viele Fehlversuche – bitte 5 Minuten warten.", status_code=429)
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            try:
                raw = base64.b64decode(auth[6:].strip()).decode("utf-8")
                user, _, pw = raw.partition(":")
            except (binascii.Error, UnicodeDecodeError):
                user, pw = "", ""
            if self.user and hmac.compare_digest(user.encode(), self.user.encode()) \
                    and verify_password(pw, self.password_hash):
                return await call_next(request)
            self._fail(client)
            log.warning("Fehlgeschlagene Anmeldung von %s", client, extra={"client": client})
        return PlainTextResponse("Anmeldung erforderlich", status_code=401,
                                 headers={"WWW-Authenticate": 'Basic realm="Portfolia", charset="UTF-8"'})


MAX_BODY = 1_000_000  # Formulare der App sind klein; der kuratierte Import läuft über das Importverzeichnis
# Datei-Uploads (CSV-Import, Steuerdaten) dürfen größer sein – nur auf diesen Pfaden (ohne ROOT_PATH-Präfix)
UPLOAD_LIMITS = {"/journal/csv": 26 * 1024 * 1024, "/tax/data/upload": 26 * 1024 * 1024,
                 "/journal/documents/upload": 101 * 1024 * 1024}  # Belegstapel (höchstens 100 MiB, je Datei 25 MiB)
_MULTIPART_TOKEN = re.compile(rb'name="csrf_token"(?:\r\n[^\r\n]+)*\r\n\r\n([^\r\n]{1,200})\r\n')


def _app_path(scope: Scope) -> str:
    path = scope.get("path", "") or "/"
    root = scope.get("root_path", "") or ""
    if root and path.startswith(root):
        path = path[len(root):] or "/"
    return path


def body_limit(scope: Scope) -> int:
    return UPLOAD_LIMITS.get(_app_path(scope), MAX_BODY)


class CsrfMiddleware:
    """Double-Submit-Cookie als reine ASGI-Middleware (puffert den Body, damit der Endpunkt ihn lesen kann)."""

    SAFE = frozenset({"GET", "HEAD", "OPTIONS"})

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        token = request.cookies.get(CSRF_COOKIE)
        new_token = None if token else secrets.token_urlsafe(24)
        scope.setdefault("state", {})["csrf_token"] = token or new_token
        downstream_receive = receive
        if request.method not in self.SAFE:
            limit = body_limit(scope)
            length = request.headers.get("content-length", "")
            if length.isdigit() and int(length) > limit:
                await PlainTextResponse("Anfrage zu groß", status_code=413)(scope, receive, send)
                return
            site = request.headers.get("sec-fetch-site")
            if site and site not in ("same-origin", "none"):
                await PlainTextResponse("Cross-Site-Anfrage abgelehnt", status_code=403)(scope, receive, send)
                return
            sent = request.headers.get("x-csrf-token")
            ctype = request.headers.get("content-type", "")
            form_body = ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data"))
            if not sent and form_body:
                chunks: list[bytes] = []
                size = 0
                more = True
                while more:
                    msg = await receive()
                    if msg["type"] == "http.disconnect":
                        return
                    part = msg.get("body", b"")
                    chunks.append(part)
                    size += len(part)
                    more = msg.get("more_body", False)
                    if size > limit:
                        await PlainTextResponse("Anfrage zu groß", status_code=413)(scope, receive, send)
                        return
                body = b"".join(chunks)
                if ctype.startswith("multipart/form-data"):
                    m = _MULTIPART_TOKEN.search(body)
                    sent = m.group(1).decode("ascii", "replace") if m else ""
                else:
                    sent = (parse_qs(body.decode("utf-8", "replace")).get("csrf_token") or [""])[0]
                replayed = False

                async def replay() -> Message:
                    nonlocal replayed
                    if not replayed:
                        replayed = True
                        return {"type": "http.request", "body": body, "more_body": False}
                    return await receive()

                downstream_receive = replay
            else:
                downstream_receive = _limited(receive, limit)
            if not token or not sent or not hmac.compare_digest(token, sent):
                await PlainTextResponse("CSRF-Token fehlt oder ist ungültig – Seite neu laden.",
                                        status_code=403)(scope, receive, send)
                return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start" and new_token:
                headers = MutableHeaders(scope=message)
                secure = "; Secure" if scope.get("scheme") == "https" else ""
                headers.append("set-cookie", f"{CSRF_COOKIE}={new_token}; Path=/; SameSite=Strict{secure}")
            await send(message)

        await self.app(scope, downstream_receive, send_wrapper)


def _limited(receive: Receive, limit: int) -> Receive:
    """Body-Größe auch ohne Content-Length begrenzen (z. B. chunked): bei Überschreitung Verbindungsabbruch."""
    size = 0

    async def wrapped() -> Message:
        nonlocal size
        msg = await receive()
        if msg["type"] == "http.request":
            size += len(msg.get("body", b""))
            if size > limit:
                return {"type": "http.disconnect"}
        return msg

    return wrapped
