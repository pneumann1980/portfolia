"""Flow-Modus der Datenqualität: nach einer erledigten Aktion geht es **immer** beim Assistenten weiter.

Solange der Browser das Cookie ``dq_flow`` trägt (der Assistent setzt es), schreibt diese Schicht die Weiterleitung
bestimmter abschließender POST-Aktionen der Datenqualität auf ``/quality/assistent`` um und reicht Meldung bzw.
Fehler (``msg``/``err``) weiter. So endet jede Aktion – egal in welchem Teilsystem (Diagnose, Klärung, Prüfstapel,
Sammelaktion) – in der nächsten Aufgabe statt auf einer fremden Seite. Zwischenschritte (Auswahl, Optionen, Vorschau)
bleiben unverändert, ebenso alles ohne Cookie.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

ASSISTANT = "/quality/assistent"
# abschließende Aktionen (Aufgabe erledigt oder bewusst entschieden)
_DONE = re.compile(
    r"^/(?:quality/diagnose/(?:apply|mark|dismiss|bulk/apply|hints/apply|regeln/neu)"
    r"|quality/klaerung/[^/]+/(?:uebernehmen|zurueckstellen|bestand)"
    r"|journal/csv/\d+/(?:commit|execute|discard|ignore)"
    r"|journal/pruefung/(?:ausfuehren|entscheiden)"
    r"|quality/erneut/(?:vormerken|entfernen|zurueck|rueckgaengig))$")
_TARGET = re.compile(r"^/(?:quality|journal/pruefung|journal/csv)(?:/|$)")


def wants_flow(headers: Headers) -> bool:
    cookie = headers.get("cookie", "")
    return "dq_flow=1" in cookie.replace(" ", "").split(";")


def rewrite(target: str) -> str | None:
    """Neues Ziel für eine Weiterleitung der Datenqualität oder ``None`` (unverändert lassen)."""
    parts = urlsplit(target)
    if parts.netloc or not _TARGET.match(parts.path) or parts.path == ASSISTANT:
        return None
    q = {k: v for k, v in parse_qsl(parts.query) if k in ("msg", "err")}
    return ASSISTANT + (f"?{urlencode(q)}" if q else "")


class FlowMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST" or not _DONE.match(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        if not wants_flow(Headers(scope=scope)):
            await self.app(scope, receive, send)
            return

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                status = int(message["status"])
                if status in (302, 303) and "location" in headers:
                    new = rewrite(headers["location"])
                    if new:
                        headers["location"] = new
                elif status == 204 and "hx-redirect" in headers:  # htmx-Antworten des Prüfstapels
                    new = rewrite(headers["hx-redirect"])
                    if new:
                        headers["hx-redirect"] = new
            await send(message)

        await self.app(scope, receive, wrapped)
