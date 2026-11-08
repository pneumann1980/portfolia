"""Discovery & Intelligence – Weboberfläche (Radar, Unternehmensanalyse, Portfolio-Fit, Vergleich) und Job."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote, urlencode

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.discovery import ai as AI
from app.discovery import fit as FIT
from app.discovery import fundamentals as F
from app.discovery import service as SV
from app.discovery.charts import bars
from app.discovery.identity import norm_ticker
from app.jobs.scheduler import Scheduler, extra_jobs
from app.web.app import register_router
from app.web.deps import get_ctx, render

log = logging.getLogger(__name__)
BASE = "/discovery"


def discovery_job(ctx: Any) -> dict[str, Any]:
    return SV.run_discovery(ctx)


@extra_jobs
def _jobs(s: Scheduler) -> None:
    # einmal täglich nach der News-Analyse des Morgens; begrenzt (siehe discovery.max_new_per_run)
    s.register("discovery", lambda ctx: discovery_job(ctx), CronTrigger(hour=6, minute=47))


def _back(target: str, **q: Any) -> RedirectResponse:
    qs = urlencode({k: v for k, v in q.items() if v not in (None, "")})
    return RedirectResponse(target + (("?" + qs) if qs else ""), status_code=303)


def _amount(v: Any) -> float:
    """Betrag in EUR aus der Eingabe (deutsch „1.000,50“, „1.000“ oder „1000.5“); 0 … 10 Mio."""
    t = str(v or "").strip().replace(" ", "").replace("€", "")
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", t):
        t = t.replace(".", "")
    try:
        return max(0.0, min(10_000_000.0, float(t)))
    except ValueError:
        return FIT.DEFAULT_AMOUNT


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get(BASE, response_class=HTMLResponse)
    def radar(request: Request, cat: str = "", sort: str = "disruption", q: str = "", alle: int = 0,
              msg: str = "", err: str = "") -> HTMLResponse:
        ctx = get_ctx(request)
        data = SV.radar(ctx, cat=cat if cat in SV.CATEGORIES else "", sort=sort, q=q[:60], show_all=bool(alle))
        every = SV.radar(ctx, show_all=True)["cards"]
        budget = AI.DiscoveryBudget(ctx)
        return render(request, "discovery.html", active="discovery", cards=data["cards"], cat=cat, sort=sort, q=q,
                      alle=alle, cats=SV.CATEGORIES, counts=SV.category_counts(every), sorts=SV.SORTS,
                      last_run=data["last_run"], msg=msg, err=err, ai_on=AI.enabled(ctx), ai_left=budget.left(),
                      n_total=len(every), statuses=SV.STATUSES,
                      cfg={k.split(".", 1)[1]: SV.setting(ctx, k) for k in SV.SETTINGS}
                      | {k.split(".", 1)[1]: ctx.settings.get(k, v) for k, v in AI.DEFAULTS.items()})

    @router.post(BASE + "/add")
    async def add(request: Request) -> Response:
        ctx = get_ctx(request)
        form = await request.form()
        res = await run_in_threadpool(SV.add_manual, ctx, str(form.get("q", "")))
        if res.get("id"):
            return _back(f"{BASE}/c/{res['id']}", msg="Unternehmen aufgenommen – „Analyse erstellen“ startet die "
                                                      "Bewertung.")
        if res.get("choices"):
            names = "; ".join(f"{c['ticker']} ({c['name']})" for c in res["choices"][:8])
            return _back(BASE, err=f"{res['error']}. Bitte Ticker eingeben: {names}")
        return _back(BASE, err=res.get("error", "Nicht gefunden."))

    @router.post(BASE + "/run")
    async def run(request: Request) -> Response:
        ctx = get_ctx(request)
        sched = getattr(ctx, "scheduler", None)
        if sched is not None and sched.trigger("discovery"):
            return _back(BASE, msg="Discovery-Lauf gestartet – Ergebnisse erscheinen in einigen Minuten.")
        stats = await run_in_threadpool(SV.run_discovery, ctx)
        if stats.get("skipped"):
            return _back(BASE, err=str(stats["skipped"]))
        return _back(BASE, msg=f"Discovery-Lauf: {stats['mentions']} Nennungen geprüft, {stats['new_companies']} neue "
                               f"Unternehmen, {stats['signals']} neue Signale.")

    @router.post(BASE + "/einstellungen")
    async def save_settings(request: Request) -> Response:
        ctx = get_ctx(request)
        form = await request.form()
        for key, lo, hi in (("discovery.ai_calls_per_day", 0, 100), ("discovery.ai_calls_per_month", 0, 3000),
                            ("discovery.max_new_per_run", 0, 50), ("discovery.auto_analyse_per_run", 0, 10),
                            ("discovery.news_days", 1, 90)):
            try:
                ctx.settings.set(key, max(lo, min(hi, int(str(form.get(key.split(".", 1)[1], "")).strip()))))
            except ValueError:
                return _back(BASE, err=f"Ungültiger Wert für {key.split('.', 1)[1]}.")
        for key in ("discovery.enabled", "discovery.ai", "discovery.themes"):
            ctx.settings.set(key, form.get(key.split(".", 1)[1]) == "1")
        ua = " ".join(str(form.get("sec_user_agent", "")).split())[:200]
        ctx.settings.set("discovery.sec_user_agent", ua)
        return _back(BASE, msg="Discovery-Einstellungen gespeichert.")

    @router.get(BASE + "/export.json")
    def export(request: Request) -> Response:
        """Discovery-Nutzerdaten (Unternehmen, Status, Signale, Analyseversionen, Meilensteine) als JSON."""
        import json

        ctx = get_ctx(request)
        db = ctx.db
        data = {"companies": [dict(r) for r in db.q("SELECT * FROM disc_company ORDER BY id")],
                "aliases": [dict(r) for r in db.q("SELECT * FROM disc_alias")],
                "signals": [dict(r) for r in db.q("SELECT * FROM disc_signal ORDER BY id")],
                "analyses": [{**{k: v for k, v in dict(r).items() if k != "result_json"},
                              "result": json.loads(r["result_json"])}
                             for r in db.q("SELECT * FROM disc_analysis ORDER BY id")],
                "milestones": [dict(r) for r in db.q("SELECT * FROM disc_milestone ORDER BY id")]}
        return Response(json.dumps(data, ensure_ascii=False, indent=1, default=str), media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="portfolia-discovery.json"'})

    @router.get(BASE + "/t/{ticker}", response_class=HTMLResponse)
    def by_ticker(request: Request, ticker: str) -> Response:
        """Stabiler Link je Ticker (z. B. aus Watchlist oder Positionen)."""
        ctx = get_ctx(request)
        co = SV.by_ticker(ctx.db, ticker)
        if co is not None:
            return RedirectResponse(f"{BASE}/c/{co['id']}", status_code=303)
        return render(request, "discovery_unknown.html", active="discovery", ticker=norm_ticker(ticker)[:12])

    @router.get(BASE + "/c/{cid}", response_class=HTMLResponse)
    def detail(request: Request, cid: int, a: int | None = None, betrag: str = "", msg: str = "",
               err: str = "") -> HTMLResponse:
        ctx = get_ctx(request)
        d = SV.detail_context(ctx, cid, a, _amount(betrag) if betrag else FIT.DEFAULT_AMOUNT)
        if d is None:
            raise HTTPException(404)
        return render(request, "discovery_company.html", active="discovery", msg=msg, err=err,
                      statuses=SV.STATUSES, ai_on=AI.enabled(ctx), labels=F.LABELS, pct_keys=F.PCT, bars=bars, **d)

    @router.post(BASE + "/c/{cid}/analyse")
    async def analyse(request: Request, cid: int) -> Response:
        ctx = get_ctx(request)
        form = await request.form()
        res = await run_in_threadpool(SV.analyse_company, ctx, cid, trigger="manuell",
                                      force=form.get("force") == "1")
        if res.get("status") == "unchanged":
            return _back(f"{BASE}/c/{cid}", msg="Keine neuen Daten seit der letzten Analyse – Version unverändert "
                                                "(„Trotzdem neu erstellen“ erzwingt eine neue Version).")
        if res.get("status") != "ok":
            return _back(f"{BASE}/c/{cid}", err=res.get("error", "Analyse fehlgeschlagen."))
        note = f" KI-Teil: {res['ai_error']}." if res.get("ai_error") else ""
        return _back(f"{BASE}/c/{cid}", msg="Neue Analyseversion erstellt." + note)

    @router.post(BASE + "/c/{cid}/status")
    async def status(request: Request, cid: int) -> Response:
        ctx = get_ctx(request)
        form = await request.form()
        st = str(form.get("status", ""))
        if not SV.set_status(ctx.db, cid, st):
            raise HTTPException(400)
        back = str(form.get("back", ""))
        target = back if back.startswith(BASE) and "//" not in back else f"{BASE}/c/{cid}"
        return RedirectResponse(target, status_code=303)

    @router.post(BASE + "/c/{cid}/watchlist")
    async def to_watchlist(request: Request, cid: int) -> Response:
        ctx = get_ctx(request)
        co = SV.company(ctx.db, cid)
        if co is None:
            raise HTTPException(404)
        from app.watchlist.service import watchlist_service

        ws = watchlist_service(ctx)
        entry, errs, _ = ws.resolve("security", co.get("yahoo_symbol") or co["ticker"])
        if entry is None:
            return _back(f"{BASE}/c/{cid}", err="; ".join(errs))
        entry["name"] = co["name"]
        _id, errs = ws.add(ws.default_id(), entry)
        if errs:
            return _back(f"{BASE}/c/{cid}", err="; ".join(errs))
        return _back(f"{BASE}/c/{cid}", msg="Zur Watchlist hinzugefügt.")

    @router.get(BASE + "/c/{cid}/vergleich", response_class=HTMLResponse)
    def compare(request: Request, cid: int, a: int = 0, b: int = 0) -> HTMLResponse:
        ctx = get_ctx(request)
        co = SV.company(ctx.db, cid)
        if co is None:
            raise HTTPException(404)
        vs = SV.analyses(ctx.db, cid)
        if len(vs) >= 2 and not (a and b):
            a, b = vs[1]["id"], vs[0]["id"]
        cmp = SV.compare(ctx.db, cid, a, b) if a and b else None
        return render(request, "discovery_compare.html", active="discovery", co=co, cmp=cmp, versions=vs)

    return router


def ticker_link(ticker: str) -> str:
    return f"{BASE}/t/{quote(ticker)}"


register_router(make_router)
