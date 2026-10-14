"""Seite „Abweichungsentwicklung“ (M29): Differenz Referenz − Ledger über mehrere Referenzbestände, Fenster mit
Buchungen und Ursachenkandidaten. Die Seite selbst ändert nichts und ruft nichts ab; nur der ausdrückliche Start einer
Recherche liest (ausschließlich lesend) bei der Datenquelle – siehe :mod:`app.diagnosis.research`."""

from __future__ import annotations

import json
import re
from datetime import UTC
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.diagnosis import actions as A
from app.diagnosis import research as RS
from app.diagnosis import stock as ST
from app.diagnosis.engine import report_for
from app.diagnosis.web import _common
from app.diagnosis.windows import EVIDENCE_BADGE, KIND_BADGE, KIND_LABEL, PositionTrace
from app.web.app import register_router
from app.web.deps import get_ctx, render

SOURCE_LABEL = {"statement": "Kontoauszug", "api": "Anzeige beim Anbieter"}


def _s(v: Decimal) -> str:
    return format(v.normalize(), "f") if v else "0"


def _chart_data(tr: PositionTrace, fiat: bool) -> dict[str, Any]:
    ends = {s.end.ref_id: s.key for s in tr.segments}
    points = []
    for p in tr.points:
        points.append({"id": p.ref_id, "t": p.at.astimezone(UTC).isoformat(), "when": p.at.astimezone(UTC).strftime(
            "%d.%m.%Y %H:%M UTC" if p.basis != "datum" else "%d.%m.%Y"), "basis": p.basis_label, "qty": _s(p.qty),
            "ledger": _s(p.ledger), "diff": _s(p.diff), "src": SOURCE_LABEL.get(p.source, "sonstiger Beleg"),
            "conflict": p.conflict, "seg": ends.get(p.ref_id)})
    segs = [{"key": s.key, "kind": s.kind, "label": s.label, "delta": _s(s.delta), "n_tx": s.n_tx,
             "start": s.start.at.astimezone(UTC).isoformat() if s.start else None,
             "end": s.end.at.astimezone(UTC).isoformat()} for s in tr.segments]
    return {"fiat": fiat, "points": points, "segs": segs}


def _rows(ctx: Any, report: Any, only_dev: bool, acc: str, asset: str) -> list[dict[str, Any]]:
    idx = report.index
    out = []
    for tr in (report.traces or {}).values():
        if only_dev and not tr.deviating:
            continue
        if (acc and tr.account != acc) or (asset and tr.asset != asset):
            continue
        last = tr.last
        value = idx.value(tr.asset, abs(last.diff)) if idx is not None and last is not None and last.diff else None
        try:
            fiat = bool(idx.asset(tr.asset).is_fiat)
        except Exception:
            fiat = False
        refs = {tx_id for s in tr.segments for tx_id in s.tx_ids}
        txs = {i: idx.ref(idx.by_id[i]) for i in refs if i in idx.by_id}
        plans = {sg.key: RS.plan_for(ctx, report, tr, sg) for sg in tr.segments if sg.kind != "ok"}
        out.append({"tr": tr, "value": value, "chart": json.dumps(_chart_data(tr, fiat)), "txs": txs, "plans": plans,
                    "sort": (not tr.deviating, -(value or 0), tr.account, tr.asset)})
    out.sort(key=lambda r: r["sort"])
    return out


def _back(msg: str = "", err: str = "", pos: str = "") -> Response:
    q = {k: v for k, v in (("msg", msg), ("err", err), ("p", pos), ("all", "1")) if v}
    return RedirectResponse("/quality/diagnose/windows?" + urlencode(q), status_code=303)


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/quality/diagnose/windows", response_class=HTMLResponse)
    def windows_page(request: Request, acc: str = "", asset: str = "", all: str = "", p: str = "", msg: str = "",
                     err: str = "") -> HTMLResponse:
        ctx = get_ctx(request)
        report = report_for(ctx)
        traces = report.traces or {}
        rows = _rows(ctx, report, all != "1", acc, asset)
        RS._stale(ctx.db)
        research_runs = RS.runs(ctx.db, 8)
        return render(request, "diagnosis_windows.html", active="quality", rows=rows, n_traces=len(traces),
                      n_dev=sum(1 for t in traces.values() if t.deviating), acc=acc, asset=asset, show_all=all == "1",
                      open_pos=p, accounts=sorted({t.account for t in traces.values()}),
                      assets=sorted({t.asset for t in traces.values()}), kind_label=KIND_LABEL, kind_badge=KIND_BADGE,
                      evidence_badge=EVIDENCE_BADGE, source_label=SOURCE_LABEL, research_runs=research_runs, msg=msg,
                      err=err, **_common())

    @router.get("/quality/abdeckung", response_class=HTMLResponse)
    def coverage_page(request: Request, frm: str = "", to: str = "", area: str = "", account: str = "") -> HTMLResponse:
        """Historische Quellenabdeckung je Konto, Quelle und Monat (rein lesend)."""
        from app.diagnosis import coverage as CV

        ctx = get_ctx(request)
        frm = frm[:7] if re.fullmatch(r"\d{4}-\d{2}(-\d{2})?", frm or "") else ""
        to = to[:7] if re.fullmatch(r"\d{4}-\d{2}(-\d{2})?", to or "") else ""
        area = area if area in CV.AREAS else ""
        mx = CV.build(ctx, frm, to, area, account[:80])
        accounts = sorted({r.account for r in CV.build(ctx).rows}) if account else sorted({r.account for r in mx.rows})
        return render(request, "coverage.html", active="quality", mx=mx, states=CV.STATES, accounts=accounts,
                      account=account, gap_end=CV.month_end, **_common())

    @router.get("/quality/anschaffung", response_class=HTMLResponse)
    def provenance_page(request: Request) -> HTMLResponse:
        """Herkunft der Anschaffungsdaten je Lot und je Veräußerungsjahr (rein lesend)."""
        from app.ledger import provenance as PV

        rep = PV.build(get_ctx(request))
        return render(request, "provenance.html", active="quality", rep=rep, evidenced=PV.EVIDENCED, **_common())

    @router.get("/quality/diagnose/stock", response_class=HTMLResponse)
    def stock_page(request: Request, view: str = "", p: str = "") -> HTMLResponse:
        """Bestandsklärung (A buchhalterisch · B bestätigter Referenzbestand · C ungeklärte Differenz) und die
        optionale „bestätigte Bestandssicht“. Rein berechnet – nichts wird gespeichert oder gebucht."""
        ctx = get_ctx(request)
        report = report_for(ctx)
        sv = ST.build(report, A.decisions(ctx.db, 400))
        return render(request, "diagnosis_stock.html", active="quality", sv=sv, view=view == "confirmed", open_pos=p,
                      stale_days=ST.STALE_DAYS, **_common())

    @router.post("/quality/diagnose/research/start")
    async def research_start(request: Request) -> Response:
        """Datenquelle für ein Fenster gezielt und nur lesend erneut lesen (Hintergrund, abbrechbar)."""
        ctx = get_ctx(request)
        form = await request.form()
        try:
            sid = int(str(form.get("source") or ""))
        except ValueError:
            return _back(err="Datenquelle fehlt.")
        pos = str(form.get("pos") or "")
        kw: dict[str, Any] = {"account": str(form.get("account") or "") or None,
                              "asset": str(form.get("asset") or "") or None,
                              "win_from": str(form.get("win_from") or "") or None,
                              "win_to": str(form.get("win_to") or "") or None}
        if form.get("resume"):
            kw["resume"] = int(str(form["resume"]))
        res = await run_in_threadpool(RS.start, ctx, sid, **kw)
        if res.get("error"):
            return _back(err=res["error"], pos=pos)
        return _back(msg="Recherche gestartet – sie liest nur und ändert nichts. Das Ergebnis erscheint unten bei den "
                         "Kandidaten, sobald der Lauf beendet ist (Seite neu laden).", pos=pos)

    @router.post("/quality/diagnose/research/cancel")
    async def research_cancel(request: Request) -> Response:
        ctx = get_ctx(request)
        form = await request.form()
        try:
            sid = int(str(form.get("source") or ""))
        except ValueError:
            return _back(err="Datenquelle fehlt.")
        return _back(msg=RS.cancel(ctx, sid), pos=str(form.get("pos") or ""))

    return router


register_router(make_router)
