"""Anfrage-Protokoll und Fehlerbehandlung für die Docker-Konsole.

* Jede Anfrage bekommt eine kurze **Anfrage-ID** (Antwort-Header ``X-Request-ID``, Fehlerseite, jede Logzeile dazu).
* ``LOG_ACCESS=slow`` (Standard): 4xx (INFO), 5xx (ERROR), Anfragen ab :data:`SLOW_S` (WARNING); ``all``: jede
  Anfrage auf INFO (ohne Dauerabfragen wie ``/healthz`` und Fortschrittsanzeigen); ``off``: nichts.
* Ein unbehandelter Fehler wird als **eine** Zeile mit Methode, Pfad, ID, Ausnahme und Fundstelle protokolliert; der
  vollständige Traceback nur bei ``LOG_LEVEL=DEBUG``.

Nie protokolliert: Query-Werte (nur die Namen), Header, Formular-/Upload-Inhalte, Antwortkörper.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
import traceback
from typing import Any
from urllib.parse import parse_qsl

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.logging_setup import request_id

log = logging.getLogger("app.web")
SLOW_S = 1.0
# Dauerabfragen der Oberfläche (htmx-Polling): im Modus „all“ nicht einzeln protokollieren
_QUIET = re.compile(r"^(/healthz|/static/|/sw\.js|/favicon).*|.*/(live|progress|status|batch-progress)(/.*)?$")


def safe_target(path: str, query: str) -> str:
    """Pfad plus Query-Schlüssel ohne Werte (``/news?[tab,days]``) – Werte können Eingaben oder Geheimnisse sein."""
    keys = []
    for k, _ in parse_qsl(query, keep_blank_values=True)[:12]:
        if k[:30] not in keys:
            keys.append(k[:30])
    base = path if len(path) <= 200 else path[:197] + "…"
    return base + (f"?[{','.join(keys)}]" if keys else "")


def where(exc: BaseException, limit: int = 3) -> str:
    """Fundstelle(n) im eigenen Code (``datei.py:123 funktion``) und in Vorlagen – ohne Framework-Frames."""
    out: list[str] = []
    for fs in traceback.extract_tb(exc.__traceback__):
        name = fs.filename.replace("\\", "/")
        if "site-packages" in name or "/lib/python" in name or name.endswith(("web/security.py", "web/accesslog.py")):
            continue  # Bibliothek und Middleware sind nie die Ursache
        if "/app/" in name or "/tests/" in name or name.endswith((".html", ".txt", ".xml")):
            short = name.rsplit("/app/", 1)[-1] if "/app/" in name else name.rsplit("/", 1)[-1]
            func = "" if fs.name in ("top-level template code", "<module>") else f" {fs.name}"
            out.append(f"{short}:{fs.lineno}{func}")
    return " < ".join(reversed(out[-limit:]))


def describe(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= 300 else text[:297] + "…"


class AccessLogMiddleware:
    """Reine ASGI-Middleware (kein ``BaseHTTPMiddleware``: Streaming und Fehlerpfade bleiben unverändert)."""

    def __init__(self, app: ASGIApp, mode: str = "slow") -> None:
        self.app = app
        self.mode = mode if mode in ("slow", "all", "off") else "slow"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = secrets.token_hex(3)
        token = request_id.set(rid)
        scope.setdefault("state", {})["rid"] = rid
        t0 = time.perf_counter()
        status = 0
        size = 0

        async def send_wrapper(message: Message) -> None:
            nonlocal status, size
            if message["type"] == "http.response.start":
                status = int(message["status"])
                MutableHeaders(scope=message).append("X-Request-ID", rid)
            elif message["type"] == "http.response.body":
                size += len(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            self._log_failure(scope, rid, exc, (time.perf_counter() - t0) * 1000)
            raise
        else:
            self._log_done(scope, rid, status, size, time.perf_counter() - t0)
        finally:
            request_id.reset(token)

    def _target(self, scope: Scope) -> tuple[str, str]:
        raw = scope.get("query_string", b"")
        query = raw.decode("latin-1") if isinstance(raw, (bytes, bytearray)) else str(raw)
        return scope.get("method", "?"), safe_target(scope.get("path", ""), query)

    def _log_done(self, scope: Scope, rid: str, status: int, size: int, secs: float) -> None:
        if self.mode == "off":
            return
        method, target = self._target(scope)
        extra: dict[str, Any] = {"status": status, "ms": round(secs * 1000), "bytes": size}
        line = f"{method} {target} {status} {extra['ms']} ms"
        if status >= 500:
            log.error(line, extra=extra)
        elif secs >= SLOW_S:
            log.warning(line, extra=extra)
        elif status >= 400:
            log.info(line, extra=extra)  # Client-Fehler (403, 404, 422 …): Konsole, nicht das Fehlerprotokoll der DB
        elif self.mode == "all" and not _QUIET.match(scope.get("path", "")):
            log.info(line, extra=extra)

    def _log_failure(self, scope: Scope, rid: str, exc: Exception, ms: float) -> None:
        method, target = self._target(scope)
        place = where(exc)
        log.error("500 %s %s %d ms %s%s", method, target, round(ms), describe(exc), f" @ {place}" if place else "",
                  extra={"status": 500, "ms": round(ms)},
                  exc_info=exc if log.isEnabledFor(logging.DEBUG) else None)
