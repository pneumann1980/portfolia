"""Seite „Kettenbestand“ (Datenqualität → Belege): wann ist der Bestand der Blockchain ohne Buchung gesprungen?

Die Seite zeigt das gespeicherte Ergebnis (:mod:`app.diagnosis.chainscan`) und startet die Suche als Hintergrundjob
(öffentliche Archiv-Knoten, ohne Schlüssel; höchstens :data:`app.diagnosis.chainscan.MAX_CALLS` Abfragen je Lauf, die
Suche lässt sich fortsetzen). Gebucht wird nichts: Die Sprünge erscheinen als Lösung „einzeln buchen“ beim Befund der
Bestandsabweichung (Vorschau, Rückgängig wie bei allen Lösungen der Diagnose).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from app.datasources.chainhistory import readers_for
from app.diagnosis import chainscan as CS
from app.diagnosis.engine import report_for
from app.jobs.scheduler import Scheduler, extra_jobs
from app.progress import view
from app.web.app import register_router
from app.web.deps import get_ctx, render


@extra_jobs
def _register_jobs(s: Scheduler) -> None:
    s.register(CS.JOB, CS.job_run, None)  # nur auf Anforderung


def _running(ctx: Any) -> dict[str, Any] | None:
    st = ctx.job_status().get(CS.JOB) or {}
    if not st.get("running"):
        return None
    p = view({**(st.get("progress") or {}), "running": True, "label": CS.LABEL})
    return None if p.get("stale") else p


def capable(ctx: Any) -> list[dict[str, str]]:
    """Konten mit kostenlosem Archivweg: (Konto, Asset, Quelle, Netze)."""
    from app.datasources.service import datasource_service

    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for ds in datasource_service(ctx).list():
        if not ds.is_wallet:
            continue
        for addr in ds.addresses:
            rs = readers_for(str(ds.row["provider"]), addr)
            if rs and (ds.account, rs[0].asset) not in seen:
                seen.add((ds.account, rs[0].asset))
                out.append({"account": ds.account, "asset": rs[0].asset, "name": ds.name,
                            "nets": ", ".join(r.label for r in rs)})
    return out


def _finding_id(ctx: Any, account: str, asset: str) -> str:
    """Befund der Bestandsabweichung dieses Kontos (für den Link zur Lösung „einzeln buchen“)."""
    for f in report_for(ctx).findings:
        d = f.data or {}
        if f.kind == "holdings" and d.get("account") == account and d.get("asset") == asset:
            return f.id
    return ""


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/quality/kette", response_class=HTMLResponse)
    def page(request: Request, account: str = "", asset: str = "", msg: str = "", err: str = "") -> HTMLResponse:
        ctx = get_ctx(request)
        items = capable(ctx)
        sel = next((i for i in items if i["account"] == account and i["asset"] == asset), None)
        res = CS.stored(ctx.db, account, asset) if sel else None
        fresh = None
        if res is not None and res.ok and sel is not None:
            pf = ctx.recorded_portfolio()
            now_total = CS.LedgerSeries(pf.txs if pf is not None else [], account, asset).cum[-1]
            fresh = now_total == res.ledger_total
        return render(request, "chain.html", active="quality", dq_tab="belege", items=items, sel=sel, res=res,
                      fresh=fresh, running=_running(ctx), msg=msg, err=err, max_calls=CS.MAX_CALLS,
                      fid=_finding_id(ctx, account, asset) if sel and res else "")

    @router.post("/quality/kette/pruefen")
    async def start(request: Request) -> Response:
        ctx = get_ctx(request)
        form = await request.form()
        account, asset = str(form.get("account") or ""), str(form.get("asset") or "")
        q = {"account": account, "asset": asset}
        if not any(i["account"] == account and i["asset"] == asset for i in capable(ctx)):
            return _go(q, err="Für dieses Konto gibt es keinen kostenlosen Weg zum Bestand bei einem früheren Block.")
        if _running(ctx) is not None:
            return _go(q, err="Eine Suche läuft bereits.")
        if ctx.scheduler is not None and ctx.scheduler.trigger(CS.JOB, 0.2, account=account, asset=asset):
            return _go(q)
        out = await run_in_threadpool(CS.job_run, ctx, account, asset)  # ohne Scheduler (Tests, CLI)
        return _go(q, err=out.get("error", "") if not out.get("ok") else "",
                   msg="Suche abgeschlossen." if out.get("ok") else "")

    @router.get("/quality/kette/progress", response_class=HTMLResponse)
    def progress(request: Request, account: str = "", asset: str = "") -> Response:
        p = _running(get_ctx(request))
        if p is None:
            return Response(status_code=204, headers={"HX-Refresh": "true"})
        return render(request, "partials/chain_progress.html", p=p, account=account, asset=asset)

    return router


def _go(q: dict[str, str], **extra: str) -> Response:
    qs = urlencode({**q, **{k: v for k, v in extra.items() if v}})
    return Response(status_code=303, headers={"Location": f"/quality/kette?{qs}"})


register_router(make_router)
