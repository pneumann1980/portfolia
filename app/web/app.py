"""FastAPI-App (Jinja2 + HTMX, keine externen Ressourcen)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.gzip import GZipMiddleware

from app import APP_NAME, REVISION, __version__
from app.context import AppContext
from app.web import fmt
from app.web.accesslog import AccessLogMiddleware
from app.web.flow import FlowMiddleware
from app.web.security import BasicAuthMiddleware, CsrfMiddleware, SecurityHeadersMiddleware

log = logging.getLogger(__name__)

BASE = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE / "templates"
STATIC_DIR = BASE / "static"

NAV = [
    ("dashboard", "/", "Übersicht", "home"),
    ("positions", "/positions", "Positionen", "list"),
    ("watchlist", "/watchlist", "Watchlist", "eye"),
    ("journal", "/journal", "Buchungen", "book"),
    ("accounts", "/accounts", "Konten", "list"),
    ("plans", "/plans", "Sparpläne", "repeat"),
    ("performance", "/performance", "Performance", "chart"),
    ("tax", "/tax", "Steuern", "tax"),
    ("news", "/news", "News & Videos", "news"),
    ("quality", "/quality", "Datenqualität", "check"),
    ("settings", "/settings", "Einstellungen", "gear"),
]


def create_app(ctx: AppContext, lifespan: Any = None) -> FastAPI:
    app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan,
                  root_path=ctx.config.root_path)
    app.state.ctx = ctx
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    fmt.register(templates.env)
    templates.env.globals.update(app_name=APP_NAME, version=__version__, revision=REVISION, nav=NAV,
                                 demo_mode=ctx.config.demo_mode)
    app.state.templates = templates

    app.add_middleware(FlowMiddleware)  # innerste Schicht: schreibt nur Weiterleitungen um
    app.add_middleware(CsrfMiddleware)
    if ctx.config.auth_mode == "basic":
        app.add_middleware(BasicAuthMiddleware, user=ctx.config.auth_user, password_hash=ctx.config.auth_password_hash)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(AccessLogMiddleware, mode=ctx.config.log_access)  # äußerste Schicht: sieht jede Anfrage
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from app.web.routes import actions, api, pages

    app.include_router(pages.router)
    app.include_router(api.router)
    app.include_router(actions.router)
    for extra in _EXTRA_ROUTERS:
        app.include_router(extra())

    @app.get("/sw.js", include_in_schema=False)
    def service_worker() -> Response:
        """Service Worker im Wurzelpfad (Geltungsbereich die ganze App) – für die Installation als App."""
        return Response((STATIC_DIR / "js" / "sw.js").read_bytes(), media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> JSONResponse:
        try:
            ctx.db.scalar("SELECT 1")
            ok = True
        except Exception as e:
            log.error("Healthcheck: Datenbank nicht erreichbar: %s: %s", type(e).__name__, e)
            ok = False
        return JSONResponse({"status": "ok" if ok else "degraded", "version": __version__,
                             "revision": REVISION or None},
                            status_code=200 if ok else 503)

    @app.exception_handler(404)
    async def not_found(request: Request, exc: Exception) -> HTMLResponse:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "not found"}, status_code=404)  # type: ignore[return-value]
        return templates.TemplateResponse(request, "error.html", {"code": 404, "message": "Seite nicht gefunden",
                                                                  "active": None, "csrf_token": _csrf(request),
                                                                  "alerts": []}, status_code=404)

    @app.exception_handler(Exception)
    async def server_error(request: Request, exc: Exception) -> Response:
        """Unbehandelter Fehler: verständliche Seite mit Fehler-ID statt nacktem „Internal Server Error“.

        Protokolliert wird in :class:`AccessLogMiddleware` (eine Zeile mit Pfad und Fundstelle); Starlette gibt die
        Ausnahme danach weiter, damit Testclient und Server sie sehen.
        """
        rid = getattr(request.state, "rid", "")
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "internal error", "rid": rid}, status_code=500,
                                headers={"X-Request-ID": rid})
        try:
            return templates.TemplateResponse(request, "error.html", {
                "code": 500, "message": "Es ist ein Fehler aufgetreten. Die Daten wurden nicht verändert, wenn die "
                "Aktion nicht bestätigt wurde.", "rid": rid, "active": None, "csrf_token": _csrf(request),
                "alerts": []}, status_code=500, headers={"X-Request-ID": rid})
        except Exception as e:  # die Fehlerseite selbst darf nie den Fehler verdecken
            log.error("Fehlerseite konnte nicht erzeugt werden (ID %s): %s: %s", rid, type(e).__name__, e)
            return PlainTextResponse(f"Interner Fehler (ID {rid})", status_code=500)

    @app.exception_handler(OverflowError)
    async def too_large(request: Request, exc: OverflowError) -> HTMLResponse:
        """Eine ID jenseits des SQLite-Wertebereichs (≥ 2⁶³) kann nicht existieren → 404 statt Serverfehler."""
        if "SQLite INTEGER" not in str(exc):
            raise exc
        return await not_found(request, exc)

    return app


def _csrf(request: Request) -> str:
    return getattr(request.state, "csrf_token", "")


_EXTRA_ROUTERS: list[Any] = []


def register_router(factory: Any) -> None:
    _EXTRA_ROUTERS.append(factory)
