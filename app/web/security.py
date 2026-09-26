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
import secrets
import threading
import time
from collections import defaultdict

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

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


class CsrfMiddleware(BaseHTTPMiddleware):
    SAFE = frozenset({"GET", "HEAD", "OPTIONS"})

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        token = request.cookies.get(CSRF_COOKIE)
        if request.method not in self.SAFE:
            site = request.headers.get("sec-fetch-site")
            if site and site not in ("same-origin", "none"):
                return PlainTextResponse("Cross-Site-Anfrage abgelehnt", status_code=403)
            sent = request.headers.get("x-csrf-token")
            if not sent and request.headers.get("content-type", "").startswith(
                    ("application/x-www-form-urlencoded", "multipart/form-data")):
                form = await request.form()
                sent = str(form.get("csrf_token") or "")
            if not token or not sent or not hmac.compare_digest(token, sent):
                return PlainTextResponse("CSRF-Token fehlt oder ist ungültig – Seite neu laden.", status_code=403)
        request.state.csrf_token = token or secrets.token_urlsafe(24)
        response = await call_next(request)
        if not token:
            response.set_cookie(CSRF_COOKIE, request.state.csrf_token, httponly=False, samesite="strict",
                                secure=request.url.scheme == "https", path="/")
        return response
