"""Diagnoseansicht „Datenqualität“: priorisierte Befunde und Bestandsabgleich – nur lesend.

Die Seite hat bewusst keine Aktionen auf Buchungen (löschen, zusammenführen, umbuchen, bestätigen, ausschließen).
„Erneut prüfen“ ist ein gewöhnlicher Seitenaufruf: Die Diagnose wird bei jedem Aufruf neu aus den Daten berechnet
und nirgends gespeichert.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.diagnosis.engine import report_for
from app.diagnosis.model import HOLDING_STATUS, KINDS, STATUS, STATUS_BADGE
from app.web.app import register_router
from app.web.deps import get_ctx, render

log = logging.getLogger(__name__)

TYPE_LABEL = {"buy": "Kauf", "sell": "Verkauf", "trade": "Tausch", "deposit": "Zugang", "withdrawal": "Abgang",
              "transfer": "Transfer", "corporate_action": "Kapitalmaßnahme"}
ORIGIN_LABEL = {"import": "kuratierter Import", "journal": "App-Buchung", "plan": "Sparplan"}


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/quality/diagnose", response_class=HTMLResponse)
    def diagnose_page(request: Request, kind: str = "", status: str = "", f: str = "") -> HTMLResponse:
        ctx = get_ctx(request)
        kind = kind if kind in KINDS else ""
        status = status if status in STATUS else ""
        report = report_for(ctx)
        shown = [x for x in report.findings if (not kind or x.kind == kind) and (not status or x.status == status)]
        by_kind = {k: sum(1 for x in report.findings if x.kind == k and (not status or x.status == status))
                   for k in KINDS}
        by_status = {s: sum(1 for x in report.findings if x.status == s and (not kind or x.kind == kind))
                     for s in STATUS}
        accounts: dict[str, list[Any]] = {}
        for h in report.holdings:
            accounts.setdefault(h.account, []).append(h)
        return render(request, "diagnosis.html", active="quality", report=report, shown=shown, kind=kind,
                      status=status, open_id=f, kinds=KINDS, statuses=STATUS, status_badge=STATUS_BADGE,
                      holding_status=HOLDING_STATUS, by_kind=by_kind, by_status=by_status, accounts=accounts,
                      counts=report.counts(), holding_counts=report.holding_counts(),
                      generated=datetime.now(UTC), type_label=TYPE_LABEL, origin_label=ORIGIN_LABEL,
                      utc_fmt=lambda dt: dt.astimezone(UTC).strftime("%d.%m.%Y %H:%M:%S UTC"))

    return router


register_router(make_router)
