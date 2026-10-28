"""Datenqualität – Struktur der Oberfläche: Heute (Arbeitsplan) · Fälle · Belege · Automatik, Prüflauf mit Fortschritt.

Die Seiten selbst schreiben nichts. ``POST /quality/pruefen`` startet den Prüflauf (:mod:`app.diagnosis.checkrun`) als
Hintergrundjob; die Seite zeigt den Fortschritt (Abfrage alle 2 s) und lädt nach dem Ende neu.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from app.diagnosis import assistant as AS
from app.diagnosis import autocheck as AC
from app.diagnosis import checkrun as CR
from app.diagnosis import workplan as WP
from app.diagnosis.engine import report_for
from app.diagnosis.model import KINDS
from app.jobs.scheduler import Scheduler, extra_jobs
from app.web.app import register_router
from app.web.deps import get_ctx, render

# Reiter und Unterseiten (alte Adressen bleiben als Unterseiten erreichbar)
TABS = [("heute", "Heute", "/quality"), ("faelle", "Fälle", "/quality/faelle"), ("belege", "Belege", "/quality/belege"),
        ("automatik", "Automatik", "/quality/automatik")]
SUBS: dict[str, list[tuple[str, str, str]]] = {
    "faelle": [("Alle Fälle", "/quality/faelle", "Eine Liste für alles, was zu entscheiden ist"),
               ("Der Reihe nach entscheiden", "/quality/diagnose/liste", "Fall für Fall mit Belegen und Empfehlung"),
               ("Klärungsfälle", "/quality/klaerung", "Zusammengehörige Befunde mit Geldweg und Simulation"),
               ("Diagnose-Befunde", "/quality/diagnose", "Alle Befunde nach Art, mit Empfehlung je Befund"),
               ("Prüfstapel (Importe)", "/journal/pruefung", "Importierte Vorgänge prüfen und freigeben"),
               ("Sammelbearbeitung", "/quality/diagnose/bulk", "Dubletten und Transfers gesammelt bearbeiten"),
               ("Hinweise", "/quality/diagnose/hints", "Hinweise ohne Fehler gesammelt als geprüft markieren")],
    "belege": [("Übersicht", "/quality/belege", ""),
               ("Bestände und Nachweise", "/quality/diagnose/stock",
                "Ledger, Referenzbestand, externer Bestand je Position"),
               ("Abweichungsentwicklung", "/quality/diagnose/windows",
                "Differenz über mehrere Nachweise im Zeitverlauf"),
               ("Quellenabdeckung", "/quality/abdeckung", "Für welche Monate liegen Daten woher vor"),
               ("Anschaffungsdaten", "/quality/anschaffung", "Herkunft der Anschaffungsdaten je Lot"),
               ("Vollständigkeitscockpit", "/quality/cockpit", "Getrennte Qualitätsdimensionen je Position"),
               ("Prüfbericht", "/quality/integrity",
                "Finanzielle Integritätsprüfung: Befunde mit Ursache und Lösung")],
    "automatik": [("Übersicht", "/quality/automatik", ""),
                  ("Autopilot", "/quality/autopilot", "Was von selbst erledigt wird, mit Bericht und Rückgängig"),
                  ("Regeln", "/quality/diagnose/regeln", "Dauerhafte Entscheidungsregeln"),
                  ("Einstellungen der Klärung", "/quality/klaerung#einstellungen", "Klärung nach Importen, Nachladen")],
}
HUB = {
    "belege": ("Belege und Bestände", "Hier prüfst du, **woher** Zahlen stammen und ob sie zu externen Nachweisen "
               "passen. Nichts davon musst du regelmäßig tun – die Fälle zeigen dir, wann ein Beleg fehlt."),
    "automatik": ("Automatik", "Was Portfolia von selbst erledigt, und wie du es steuerst. Der Autopilot übernimmt "
                  "nur beweisbare Fälle und nur, wenn du ihn eingeschaltet hast; alles ist rückgängig zu machen."),
}
PENDING_S = 120
PER_KIND = 3  # Zeilen je Stufe und Befundart in der Übersicht der Fälle


def _since(raw: str) -> datetime | None:
    try:
        t = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if t.tzinfo is None:
        return None
    age = (datetime.now(UTC) - t).total_seconds()
    return t if -5 <= age <= 86400 else None


def _pending_view(raw: str) -> dict[str, Any] | None:
    """Gestartet, aber der Job ist noch nicht angelaufen: Platzhalter, damit die Seite weiter abfragt."""
    from app.progress import view

    t = _since(raw)
    if t is None or (datetime.now(UTC) - t).total_seconds() > PENDING_S:
        return None
    return view({"v": 2, "running": True, "label": CR.LABEL, "pct": 1.0, "text": "wird gestartet …"})


def banner(ctx: Any, since: str = "") -> dict[str, Any] | None:
    """Laufender (oder gerade gestarteter) Prüflauf für den Kopf aller Seiten der Datenqualität."""
    p = CR.running(ctx)
    if p is not None:
        return p
    started = _since(since)
    if started is not None and not _finished_since(ctx, started):
        return _pending_view(since)
    return None


@extra_jobs
def _register_jobs(s: Scheduler) -> None:
    s.register(CR.JOB, CR.job_run, None)  # nur auf Anforderung (kein Zeitplan)
    s.register(AC.JOB, AC.job_run, None)  # verzögert nach Datenänderungen (Rückruf unten)
    AC.install(s.ctx)


def _finished_since(ctx: Any, since: datetime) -> bool:
    from app.util.timeutil import parse_iso

    t = parse_iso(str(CR.previous(ctx.db).get("finished_at") or ""))
    return t is not None and t >= since


def _safe_back(raw: str) -> str:
    """Rücksprungziel nur innerhalb der Datenqualität."""
    return raw if raw.startswith("/quality") and "//" not in raw and "\n" not in raw else "/quality"


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/quality/plan", response_class=HTMLResponse)
    def plan_partial(request: Request) -> HTMLResponse:
        """Arbeitsplan (Stufe A/B/C) – wird auf der Startseite nachgeladen, damit die Seite sofort steht."""
        ctx = get_ctx(request)
        plan = WP.build(ctx)
        last = CR.previous(ctx.db)
        n, minutes = AS.summary(ctx)
        return render(request, "partials/dq_plan.html", plan=plan, tiers=WP.TIERS, why_a=WP.WHY_A, last=last,
                      kinds=KINDS, assist_n=n, assist_min=minutes,
                      auto_check=bool(ctx.settings.get(AC.SETTING, True)))

    @router.post("/quality/pruefen")
    async def start(request: Request) -> Response:
        ctx = get_ctx(request)
        form = await request.form()
        back = _safe_back(str(form.get("back") or "/quality"))
        if CR.running(ctx) is not None:
            return _redirect(back, err="Ein Prüflauf läuft bereits.")
        since = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        if ctx.scheduler is not None and ctx.scheduler.trigger(CR.JOB, 0.2):
            return _redirect(back, since=since)
        try:  # ohne Scheduler (Tests, CLI): direkt ausführen
            out = await run_in_threadpool(CR.job_run, ctx)
        except RuntimeError as e:
            return _redirect(back, err=str(e))
        return _redirect(back, msg="Prüflauf abgeschlossen." if out.get("ok") else out.get("error", "Fehler"))

    @router.post("/quality/automatik/pruefung")
    async def set_auto_check(request: Request) -> Response:
        ctx = get_ctx(request)
        form = await request.form()
        on = form.get("auto") == "1"
        ctx.settings.set(AC.SETTING, on)
        state = "eingeschaltet." if on else "ausgeschaltet."
        return _redirect("/quality/automatik", msg=f"Automatische Nachprüfung {state}")

    @router.get("/quality/pruefen/progress", response_class=HTMLResponse)
    def progress(request: Request, since: str = "", back: str = "/quality") -> Response:
        ctx = get_ctx(request)
        p = CR.running(ctx) or _pending_view(since)
        started = _since(since)
        if p is not None and started is not None and _finished_since(ctx, started):
            p = None
        if p is None:  # fertig → Seite neu laden (zeigt den Bericht)
            return Response(status_code=204, headers={"HX-Refresh": "true"})
        return render(request, "partials/dq_check_progress.html", p=p, since=since, back=_safe_back(back))

    @router.get("/quality/faelle", response_class=HTMLResponse)
    def cases_page(request: Request, tier: str = "", kind: str = "", q: str = "", all: str = "") -> HTMLResponse:
        ctx = get_ctx(request)
        all_ = all
        report = report_for(ctx)
        plan = WP.build(ctx, report, granular=True)
        items = [i for i in plan.items if (not tier or i.tier == tier) and (not kind or i.sub == kind)
                 and (not q or q.casefold() in f"{i.title} {i.recommendation}".casefold())]
        kinds = sorted({i.sub for i in plan.items if i.sub})
        # Ohne Artfilter nur die ersten Zeilen je Stufe und Art; der Rest hinter „weitere …“ (Übersicht statt Flut)
        rows: list[dict[str, Any]] = []
        seen: dict[tuple[str, str], int] = {}
        more: dict[tuple[str, str], int] = {}
        for i in items:
            key = (i.tier, i.sub)
            seen[key] = seen.get(key, 0) + 1
            if kind or q or all_ == "1" or not i.sub or seen[key] <= PER_KIND:
                rows.append({"item": i})
            else:
                more[key] = more.get(key, 0) + 1
        for (t, k), n in more.items():
            rows.append({"more": n, "tier": t, "kind": k})
        rows.sort(key=lambda r: ("ABC".index(r["item"].tier if "item" in r else r["tier"]), "more" in r))
        return render(request, "dq_cases.html", active="quality", plan=plan, rows=rows, tiers=WP.TIERS, f_tier=tier,
                      f_kind=kind, q=q, kinds=kinds, kind_label=KINDS, why_a=WP.WHY_A, n_shown=len(items),
                      counts={t: sum(1 for i in plan.items if i.tier == t) for t in WP.TIERS})

    for tab in ("belege", "automatik"):
        def _hub(request: Request, _tab: str = tab) -> HTMLResponse:
            title, intro = HUB[_tab]
            return render(request, "dq_hub.html", active="quality", tab=_tab, title=title, intro=intro,
                          subs=[s for s in SUBS[_tab] if s[2]],
                          auto_check=bool(get_ctx(request).settings.get(AC.SETTING, True)))

        router.add_api_route(f"/quality/{tab}", _hub, methods=["GET"], response_class=HTMLResponse)

    return router


def _redirect(back: str, **q: str) -> Response:
    sep = "&" if "?" in back else "?"
    path, _, frag = back.partition("#")
    loc = path + (sep + urlencode({k: v for k, v in q.items() if v}) if any(q.values()) else "")
    if frag:
        loc += "#" + quote(frag)
    return Response(status_code=303, headers={"Location": loc})


register_router(make_router)
