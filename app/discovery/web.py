"""Discovery & Intelligence – Weboberfläche (Radar, KI-Steuerung, Jobs, Unternehmensanalyse, Vergleich) und Zeitplan."""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import quote, urlencode

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.discovery import ai as AI
from app.discovery import engine as EN
from app.discovery import fit as FIT
from app.discovery import fundamentals as F
from app.discovery import jobs as J
from app.discovery import nash as NASH
from app.discovery import service as SV
from app.discovery import universe as U
from app.discovery.charts import bars
from app.discovery.identity import norm_ticker
from app.jobs.scheduler import Scheduler, extra_jobs
from app.web.app import register_router
from app.web.deps import get_ctx, render

log = logging.getLogger(__name__)
BASE = "/discovery"
START_KINDS = {"ki_discovery": "KI-Discovery", "screening": "Fundamentalscreening", "voll": "Vollständiger "
               "Discovery-Lauf", "news": "Nachrichten-Discovery"}
_RECOVERED: set[int] = set()


def _recover_once(ctx: Any) -> None:
    if id(ctx) not in _RECOVERED:
        _RECOVERED.add(id(ctx))
        J.recover(ctx)


def schedule(s: Scheduler) -> None:
    """Zeitplan aus den Einstellungen (getrennte Arbeitsschritte); erneut aufrufen übernimmt Änderungen sofort."""
    for step in EN.SCHEDULE:
        name = f"disc_auto_{step}"
        hours = int(SV.setting(s.ctx, f"discovery.auto.{step}") or 0)
        kw = EN.cron_kwargs(step, hours)
        if name not in s.jobs:
            s.register(name, lambda ctx, _step=step: EN.run_scheduled(ctx, _step), None, executor="discovery")
        if s.sched.get_job(name) is not None:
            s.sched.remove_job(name)
        if kw is not None:
            s.sched.add_job(s.jobs[name], CronTrigger(**kw), id=name, name=name, replace_existing=True,
                            executor="discovery")


@extra_jobs
def _jobs(s: Scheduler) -> None:
    s.register("discovery_task", lambda ctx, job_id=0: J.run(ctx, int(job_id)), None, executor="discovery")
    s.register("discovery_task_interactive", lambda ctx, job_id=0: J.run(ctx, int(job_id)), None,
               executor="interactive")
    _recover_once(s.ctx)
    schedule(s)


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


def _cfg(ctx: Any) -> dict[str, Any]:
    return {k.split(".", 1)[1]: SV.setting(ctx, k) for k in SV.SETTINGS} | \
        {k.split(".", 1)[1]: ctx.settings.get(k, v) for k, v in AI.DEFAULTS.items()}


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get(BASE, response_class=HTMLResponse)
    def radar(request: Request, cat: str = "", sort: str = "disruption", q: str = "", alle: int = 0,
              msg: str = "", err: str = "", job: int = 0) -> HTMLResponse:
        ctx = get_ctx(request)
        _recover_once(ctx)
        data = SV.radar(ctx, cat=cat if cat in SV.CATEGORIES else "", sort=sort, q=q[:60], show_all=bool(alle))
        every = SV.radar(ctx, show_all=True)["cards"]
        return render(request, "discovery.html", active="discovery", cards=data["cards"], cat=cat, sort=sort, q=q,
                      alle=alle, cats=SV.CATEGORIES, counts=SV.category_counts(every), sorts=SV.SORTS,
                      last_run=data["last_run"], msg=msg, err=err, st=EN.ai_status(ctx),
                      prog=EN.screening_progress(ctx), jobs=J.recent(ctx.db, 8), focus_job=job,
                      n_total=len(every), statuses=SV.STATUSES, cfg=_cfg(ctx), start_kinds=START_KINDS,
                      sched_labels=EN.SCHEDULE_LABELS, sizes=U.SIZE_LABELS, prio_rule=EN.PRIORITY_RULE)

    @router.get(BASE + "/status", response_class=HTMLResponse)
    def status_partial(request: Request) -> HTMLResponse:
        """KI-Researchstatus und laufende Jobs (HTMX, aktualisiert sich selbst, solange Jobs laufen)."""
        ctx = get_ctx(request)
        return render(request, "partials/discovery_status.html", st=EN.ai_status(ctx),
                      prog=EN.screening_progress(ctx), jobs=J.recent(ctx.db, 8))

    @router.post(BASE + "/start/{kind}")
    async def start(request: Request, kind: str) -> Response:
        ctx = get_ctx(request)
        if kind not in START_KINDS:
            raise HTTPException(404)
        r = await run_in_threadpool(J.start, ctx, kind, trigger="manuell")
        jb = J.get(ctx.db, r["id"]) or {}
        if not r["new"]:
            return _back(BASE, err=f"{START_KINDS[kind]} läuft bereits (Job #{r['id']}) – kein zweiter Start.",
                         job=r["id"])
        note = "gestartet" if jb.get("active") else f"beendet: {jb.get('status_label')}"
        return _back(BASE, msg=f"{START_KINDS[kind]} {note} (Job #{r['id']}).", job=r["id"])

    @router.get(BASE + "/jobs/{job_id}", response_class=HTMLResponse)
    def job_partial(request: Request, job_id: int) -> HTMLResponse:
        ctx = get_ctx(request)
        J.expire_stale(ctx)
        jb = J.get(ctx.db, job_id)
        if jb is None:
            raise HTTPException(404)
        return render(request, "partials/discovery_job.html", jb=jb)

    @router.get(BASE + "/protokoll", response_class=HTMLResponse)
    def log_page(request: Request, job: int = 0) -> HTMLResponse:
        """Ausführungsprotokoll: Jobs und KI-Aufrufe (ohne Prompt-Inhalte und Schlüssel)."""
        ctx = get_ctx(request)
        jb = J.get(ctx.db, job) if job else None
        cands = [dict(r) for r in ctx.db.q("SELECT * FROM disc_candidate WHERE job_id=? ORDER BY id", (job,))] \
            if job else []
        return render(request, "discovery_log.html", active="discovery", jobs=J.recent(ctx.db, 60), jb=jb,
                      calls=J.calls(ctx.db, job_id=job or None, limit=200), cands=cands, st=EN.ai_status(ctx))

    @router.post(BASE + "/add")
    async def add(request: Request) -> Response:
        ctx = get_ctx(request)
        form = await request.form()
        res = await run_in_threadpool(SV.add_manual, ctx, str(form.get("q", "")))
        if res.get("id"):
            return _back(f"{BASE}/c/{res['id']}", msg="Unternehmen aufgenommen – „Fundamentaldaten aktualisieren“ oder "
                                                      "„KI-Analyse starten“ erstellt die Bewertung.")
        if res.get("choices"):
            names = "; ".join(f"{c['ticker']} ({c['name']})" for c in res["choices"][:8])
            return _back(BASE, err=f"{res['error']}. Bitte Ticker eingeben: {names}")
        return _back(BASE, err=res.get("error", "Nicht gefunden."))

    @router.post(BASE + "/run")
    async def run_legacy(request: Request) -> Response:
        return await start(request, "voll")

    @router.post(BASE + "/einstellungen")
    async def save_settings(request: Request) -> Response:
        ctx = get_ctx(request)
        form = await request.form()
        for key, lo, hi in (("discovery.ai_calls_per_day", 0, 100), ("discovery.ai_calls_per_month", 0, 3000),
                            ("discovery.max_new_per_run", 0, 50), ("discovery.auto_analyse_per_run", 0, 10),
                            ("discovery.news_days", 1, 90), ("discovery.screen_batch", 0, 200),
                            ("discovery.min_revenue_musd", 0, 100000), ("discovery.ki_companies", 1, 12)):
            raw = form.get(key.split(".", 1)[1])
            if raw is None:
                continue
            try:
                ctx.settings.set(key, max(lo, min(hi, int(str(raw).strip()))))
            except ValueError:
                return _back(BASE, err=f"Ungültiger Wert für {key.split('.', 1)[1]}.")
        for step in EN.SCHEDULE:
            raw = form.get(f"auto_{step}")
            if raw is not None and str(raw) in ("0", "24", "168"):
                ctx.settings.set(f"discovery.auto.{step}", int(str(raw)))
        for key in ("discovery.enabled", "discovery.ai", "discovery.themes"):
            ctx.settings.set(key, form.get(key.split(".", 1)[1]) == "1")
        ua = " ".join(str(form.get("sec_user_agent", "")).split())[:200]
        ctx.settings.set("discovery.sec_user_agent", ua)
        sched = getattr(ctx, "scheduler", None)
        if sched is not None:
            schedule(sched)
        return _back(BASE, msg="Discovery-Einstellungen gespeichert.")

    @router.get(BASE + "/export.json")
    def export(request: Request) -> Response:
        """Discovery-Nutzerdaten als JSON (Ansicht/Analyse; die Wiederherstellung läuft über den Gesamtexport)."""
        from app.discovery.backup import collect

        ctx = get_ctx(request)
        return Response(json.dumps(collect(ctx.db, full=True), ensure_ascii=False, indent=1, default=str),
                        media_type="application/json",
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
               err: str = "", job: int = 0) -> HTMLResponse:
        ctx = get_ctx(request)
        _recover_once(ctx)
        d = SV.detail_context(ctx, cid, a, _amount(betrag) if betrag else FIT.DEFAULT_AMOUNT)
        if d is None:
            raise HTTPException(404)
        return render(request, "discovery_company.html", active="discovery", msg=msg, err=err, focus_job=job,
                      statuses=SV.STATUSES, st=EN.ai_status(ctx), labels=F.LABELS, pct_keys=F.PCT, bars=bars,
                      nash_prov=NASH.PROVENANCE, nash_method=NASH.METHOD, nash_src=(NASH.SOURCE_VIDEO,
                                                                                   NASH.SOURCE_SUMMARY), **d)

    @router.get(BASE + "/c/{cid}/status", response_class=HTMLResponse)
    def company_status(request: Request, cid: int) -> HTMLResponse:
        ctx = get_ctx(request)
        d = SV.detail_context(ctx, cid)
        if d is None:
            raise HTTPException(404)
        return render(request, "partials/discovery_company_status.html", st=EN.ai_status(ctx), **d)

    @router.post(BASE + "/c/{cid}/analyse")
    async def analyse(request: Request, cid: int) -> Response:
        ctx = get_ctx(request)
        if SV.company(ctx.db, cid) is None:
            raise HTTPException(404)
        form = await request.form()
        mode = str(form.get("mode") or "ergaenzen")
        if form.get("force") == "1":  # früherer Knopf „Trotzdem neu erstellen“
            mode = "voll"
        if mode not in SV.MODES or mode == "auto":
            raise HTTPException(400)
        r = await run_in_threadpool(J.start, ctx, "analyse", trigger="manuell", company_id=cid,
                                    params={"mode": mode, "force": form.get("force") == "1"})
        jb = J.get(ctx.db, r["id"]) or {}
        if not r["new"]:
            return _back(f"{BASE}/c/{cid}", err=f"Für dieses Unternehmen läuft bereits eine Analyse (Job #{r['id']}).",
                         job=r["id"])
        if jb.get("active"):
            return _back(f"{BASE}/c/{cid}", msg=f"Job #{r['id']} gestartet: {SV.MODES[mode]}.", job=r["id"])
        res = jb.get("result") or {}
        txt = f"Job #{r['id']} {jb.get('status_label')}: {SV.MODES[mode]}."
        if res.get("status") == "unchanged":
            txt += " Keine neuen Daten oder KI-Teile – Version unverändert."
        if jb.get("error"):
            return _back(f"{BASE}/c/{cid}", err=f"{txt} {jb['error']}", job=r["id"])
        return _back(f"{BASE}/c/{cid}", msg=txt, job=r["id"])

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
