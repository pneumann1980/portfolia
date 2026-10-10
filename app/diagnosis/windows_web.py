"""Seite „Abweichungsentwicklung“ (M29): Differenz Referenz − Ledger über mehrere Referenzbestände, Fenster mit
Buchungen und Ursachenkandidaten. Rein lesend – nichts wird gebucht, gespeichert oder abgerufen."""

from __future__ import annotations

import json
from datetime import UTC
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

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


def _rows(report: Any, only_dev: bool, acc: str, asset: str) -> list[dict[str, Any]]:
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
        out.append({"tr": tr, "value": value, "chart": json.dumps(_chart_data(tr, fiat)), "txs": txs,
                    "sort": (not tr.deviating, -(value or 0), tr.account, tr.asset)})
    out.sort(key=lambda r: r["sort"])
    return out


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/quality/diagnose/windows", response_class=HTMLResponse)
    def windows_page(request: Request, acc: str = "", asset: str = "", all: str = "", p: str = "") -> HTMLResponse:
        ctx = get_ctx(request)
        report = report_for(ctx)
        traces = report.traces or {}
        rows = _rows(report, all != "1", acc, asset)
        return render(request, "diagnosis_windows.html", active="quality", rows=rows, n_traces=len(traces),
                      n_dev=sum(1 for t in traces.values() if t.deviating), acc=acc, asset=asset, show_all=all == "1",
                      open_pos=p, accounts=sorted({t.account for t in traces.values()}),
                      assets=sorted({t.asset for t in traces.values()}), kind_label=KIND_LABEL, kind_badge=KIND_BADGE,
                      evidence_badge=EVIDENCE_BADGE, source_label=SOURCE_LABEL, **_common())

    return router


register_router(make_router)
