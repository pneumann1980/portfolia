"""Diagnoseansicht „Datenqualität“: priorisierte Befunde mit Empfehlung, Bestandsabgleich, Korrekturen.

Die Seite selbst ändert nichts – „Erneut prüfen“ ist ein gewöhnlicher Seitenaufruf, die Diagnose wird bei jedem Aufruf
neu berechnet. Korrekturen gibt es nur über die Vorschau (``GET /quality/diagnose/plan`` – rechnet auf einer Kopie)
und ein ausdrückliches „Übernehmen“ (``POST``, mit Prüfsumme der Vorschau). Jede Korrektur lässt sich unter
„Entscheidungen und Korrekturen“ zurücknehmen; „als geprüft markieren“ ändert keine Daten.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from app.diagnosis import actions as A
from app.diagnosis import live as L
from app.diagnosis.engine import report_for
from app.diagnosis.model import CASE_STATES, HOLDING_STATUS, KINDS, LOSS_CLASS, SECTIONS, STATUS, STATUS_BADGE
from app.diagnosis.recommend import recommend
from app.web.app import register_router
from app.web.deps import get_ctx, render

log = logging.getLogger(__name__)

TYPE_LABEL = {"buy": "Kauf", "sell": "Verkauf", "trade": "Tausch", "deposit": "Zugang", "withdrawal": "Abgang",
              "transfer": "Transfer", "corporate_action": "Kapitalmaßnahme"}
ORIGIN_LABEL = {"import": "kuratierter Import", "journal": "App-Buchung", "plan": "Sparplan"}


def _utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%d.%m.%Y %H:%M:%S UTC")


def _common() -> dict[str, Any]:
    return {"type_label": TYPE_LABEL, "origin_label": ORIGIN_LABEL, "utc_fmt": _utc, "status_badge": STATUS_BADGE,
            "holding_status": HOLDING_STATUS, "loss_class": LOSS_CLASS, "case_states": CASE_STATES}


def _case_counts(report: Any, fixes: dict[str, list[Any]], applied_kinds: dict[str, int]) -> dict[str, dict[str, int]]:
    """Je Befundart: offene Vorgänge (Einzelvorgänge statt Sammelbefund), nachgewiesen, wahrscheinlich, ungeklärt,
    abgelehnt, übernommen."""
    out: dict[str, dict[str, int]] = {}
    for f in report.findings:
        if f.children:
            continue  # gezählt werden die Einzelvorgänge
        c = out.setdefault(f.kind, dict.fromkeys(("offen", "nachgewiesen", "wahrscheinlich", "ungeklaert",
                                                  "abgelehnt", "spaeter", "uebernommen"), 0))
        if f.state == "abgelehnt":
            c["abgelehnt"] += 1
            continue
        if f.state == "ungeklaert":
            c["ungeklaert"] += 1
            continue
        if f.state == "spaeter":
            c["spaeter"] += 1
        c["offen"] += 1
        if f.status == "belegt":
            c["nachgewiesen"] += 1
        elif f.status == "wahrscheinlich":
            c["wahrscheinlich"] += 1
    for kind, n in applied_kinds.items():
        out.setdefault(kind, dict.fromkeys(("offen", "nachgewiesen", "wahrscheinlich", "ungeklaert", "abgelehnt",
                                            "spaeter", "uebernommen"), 0))["uebernommen"] = n
    return out


DEVIATION_STATUSES = ("extern_diff", "ref_diff", "intern_diff")
ACCOUNT_TYPES = {"exchange": "Börse", "wallet": "Wallet", "none": "ohne Datenquelle"}


def _deviations(report: Any, q: Mapping[str, str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Bestandsabweichungen (Soll ≠ Ist, negative Bestände, Dublettenverdacht) mit Filtern – nur Anzeige."""
    from decimal import Decimal

    idx = report.index
    snap = report.snapshot
    kinds: dict[str, set[str]] = {}
    for src in (snap.sources if snap is not None else []):
        kinds.setdefault(src.account, set()).add(src.kind)
    rows = []
    for h in report.holdings:
        diff = h.ref_diff if h.ref_diff is not None else h.diff if h.diff is not None else h.internal_diff
        dup = any(e.startswith(("Dublettenverdacht", "nicht verknüpfter Transfer")) for e in h.explanations)
        if not (h.status in DEVIATION_STATUSES or (h.status == "extern_unsicher" and h.diff) or h.computed < 0 or dup):
            continue
        value = None
        if idx is not None and diff:
            value = idx.value(h.asset, abs(diff))
        elif idx is not None and h.computed < 0:
            value = idx.value(h.asset, abs(h.computed))
        acc_kinds = kinds.get(h.account) or {"none"}
        rows.append({"h": h, "diff": diff, "value": value, "types": acc_kinds,
                     "ist": h.reference if h.reference is not None else h.observed,
                     "ist_at": h.reference_at if h.reference is not None else h.observed_at,
                     "soll": h.soll_at_ref if h.reference is not None else h.computed_at_obs
                     if h.observed is not None and h.computed_at_obs is not None else h.computed})
    f = {k: (q.get(k) or "").strip() for k in ("h_type", "h_acc", "h_asset", "h_min", "h_conf")}
    try:
        h_min = Decimal(f["h_min"].replace(",", ".")) if f["h_min"] else None
    except ArithmeticError:
        h_min = None
    shown = [r for r in rows
             if (not f["h_type"] or f["h_type"] in r["types"])
             and (not f["h_acc"] or r["h"].account == f["h_acc"])
             and (not f["h_asset"] or r["h"].asset == f["h_asset"])
             and (h_min is None or (r["value"] is not None and r["value"] >= h_min))
             and (not f["h_conf"] or r["h"].confidence == f["h_conf"])]
    shown.sort(key=lambda r: (-(r["value"] or 0), r["h"].account, r["h"].asset))
    meta = {"filters": f, "n_all": len(rows), "accounts": sorted({r["h"].account for r in rows}),
            "assets": sorted({r["h"].asset for r in rows}), "types": ACCOUNT_TYPES}
    return shown, meta


def _params(data: Mapping[str, Any] | Any) -> tuple[dict[str, list[str]], bool]:
    """Eingaben einer Lösung (Felder ``p_<name>``); ``p__set`` = Formular wurde abgeschickt."""
    out: dict[str, list[str]] = {}
    for k in data:
        if k.startswith("p_") and k != "p__set":
            out[k[2:]] = [str(v) for v in data.getlist(k)]
    return out, "p__set" in data


def _back(target: str) -> Response:
    return Response(status_code=303, headers={"Location": target})


def _page_url(msg: str = "", err: str = "", anchor: str = "") -> str:
    q = {k: v for k, v in (("msg", msg), ("err", err)) if v}
    return "/quality/diagnose" + (f"?{urlencode(q)}" if q else "") + (f"#{anchor}" if anchor else "")


def _plan_page(request: Request, fid: str, opt: str, params: dict[str, list[str]], given: bool,
               errors: list[str] | None = None, status_code: int = 200) -> HTMLResponse:
    ctx = get_ctx(request)
    report = report_for(ctx)
    f = report.by_id(fid)
    if f is None:
        return render(request, "diagnosis_plan.html", status_code=404, active="quality", f=None, plan=None,
                      effects=None, rec=None, errors=errors or ["Der Befund besteht nicht mehr – die Daten haben sich "
                                                               "geändert. Bitte die Diagnose neu öffnen."], **_common())
    rec = recommend(report, f)
    plan = A.build_plan(ctx, report, f, opt, params, given=given)
    effects = A.preview(ctx, report, plan) if not plan.errors else None
    return render(request, "diagnosis_plan.html", status_code=status_code, active="quality", f=f, rec=rec,
                  plan=plan, effects=effects, errors=errors or [], **_common())


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/quality/diagnose", response_class=HTMLResponse)
    def diagnose_page(request: Request, kind: str = "", status: str = "", f: str = "", msg: str = "",
                      err: str = "") -> HTMLResponse:
        ctx = get_ctx(request)
        kind = kind if kind in KINDS else ""
        status = status if status in STATUS else ""
        report = report_for(ctx)
        marks = A.active_marks(ctx.db)
        prints = {x.id: A.fingerprint(x) for x in report.findings if x.id in marks}
        checked = {fid for fid, d in marks.items() if fid in prints and d.fingerprint == prints[fid]
                   and d.action in ("dismiss", "reject")}
        changed = {fid: d for fid, d in marks.items() if fid in prints and d.fingerprint != prints[fid]}
        # Übersicht: Einzelvorgänge erscheinen unter ihrem Sammelbefund (eigene Prüfansicht je Vorgang)
        open_findings = [x for x in report.findings if x.id not in checked and x.parent is None]
        shown = [x for x in open_findings if (not kind or x.kind == kind) and (not status or x.status == status)]
        dismissed = [x for x in report.findings if x.id in checked and x.parent is None]
        counts: dict[str, dict[str, int]] = {k: {} for k in KINDS}
        for x in open_findings:
            counts[x.kind][x.status] = counts[x.kind].get(x.status, 0) + 1
        by_kind = {k: sum(1 for x in open_findings if x.kind == k and (not status or x.status == status))
                   for k in KINDS}
        by_status = {s: sum(1 for x in open_findings if x.status == s and (not kind or x.kind == kind))
                     for s in STATUS}
        accounts: dict[str, list[Any]] = {}
        for h in report.holdings:
            accounts.setdefault(h.account, []).append(h)
        deviations, dev_meta = _deviations(report, request.query_params)
        sections = []
        for key, (label, skinds) in SECTIONS.items():
            items = [x for x in open_findings if x.kind in skinds]
            extra = ""
            if key == "verluste":
                by_cls = {c: sum(1 for x in items if (x.data or {}).get("class") == c) for c in LOSS_CLASS}
                extra = " · ".join(f"{n}× {c}" for c, n in by_cls.items() if n)
            if key == "bestand":
                extra = f"{len(deviations)} Positionen" if deviations else ""
            sections.append({"key": key, "label": label, "kind": skinds[0], "n": len(items), "extra": extra})
        pf = report.snapshot.pf if report.snapshot is not None else None
        ref_rows = []
        by_pos = {(h.account, h.asset): h for h in report.holdings}
        for r in (report.snapshot.references if report.snapshot is not None else []):
            ref_rows.append({"r": r, "h": by_pos.get((r.account, r.asset_id))})
        fixes = A.active_fixes(ctx.db)
        applied_kinds: dict[str, int] = {}
        for lst in fixes.values():
            for d in lst:
                applied_kinds[d.kind] = applied_kinds.get(d.kind, 0) + 1
        case_counts = _case_counts(report, fixes, applied_kinds)
        for sec in sections:
            sec["cc"] = {k: sum(case_counts.get(kd, {}).get(k, 0) for kd in SECTIONS[sec["key"]][1])
                         for k in ("offen", "nachgewiesen", "wahrscheinlich", "ungeklaert", "abgelehnt", "uebernommen")}
        live = L.context(ctx, {r["h"].account for r in _deviations(report, {})[0]}, "/quality/diagnose#bestand")
        explorers = L.explorer_links(ctx)
        return render(request, "diagnosis.html", active="quality", report=report, shown=shown, kind=kind,
                      live=live, explorers=explorers,
                      case_counts=case_counts, by_fid={x.id: x for x in report.findings},
                      deviations=deviations, dev_meta=dev_meta, sections=sections, ref_rows=ref_rows,
                      ref_accounts=sorted(set(pf.all_accounts()) | set(pf.accounts)) if pf is not None else [],
                      ref_assets=sorted(pf.assets) if pf is not None else [], today=datetime.now(UTC).date(),
                      status=status, open_id=f, kinds=KINDS, statuses=STATUS, by_kind=by_kind, by_status=by_status,
                      accounts=accounts, counts=counts, holding_counts=report.holding_counts(),
                      generated=datetime.now(UTC), n_open=len(open_findings),
                      recs={x.id: recommend(report, x) for x in [*shown, *dismissed]}, dismissed=dismissed,
                      marks=marks, changed=changed, fixes=fixes, decisions=A.decisions(ctx.db),
                      msg=msg, err=err, **_common())

    @router.get("/quality/diagnose/case/{fid}", response_class=HTMLResponse)
    def case_page(request: Request, fid: str, msg: str = "", err: str = "") -> HTMLResponse:
        """Prüfansicht eines Einzelvorgangs bzw. Befunds: alle beteiligten Buchungen, Belege, Korrekturmöglichkeiten,
        Entscheidungen. Ändert nichts."""
        ctx = get_ctx(request)
        report = report_for(ctx)
        f = report.by_id(fid)
        if f is None:
            return render(request, "diagnosis_case.html", status_code=404, active="quality", f=None, rows=[],
                          msg=msg, err=err or "Der Vorgang besteht nicht mehr – die Daten haben sich geändert "
                                              "(z. B. nach einer übernommenen Korrektur).", **_common())
        snap = report.snapshot
        by_tx = {t.tx_id: t for t in snap.pf.txs} if snap is not None and snap.pf is not None else {}
        ids = list(dict.fromkeys([r.tx_id for r in f.txs] + [x.tx_id for a, b, _w in f.pairs for x in (a, b)]))
        rows = []
        for tid in ids:
            t = by_tx.get(tid)
            if t is None:
                continue
            meta = snap.journal.get(tid) if snap is not None else None
            rows.append({"t": t, "ref": report.index.ref(t) if report.index is not None else None, "meta": meta,
                         "status": ("App-Buchung, aktiv" if meta is not None and meta.status == "active" else
                                    f"App-Buchung, {meta.status}" if meta is not None else
                                    "Sparplan" if t.origin == "plan" else "Import-Buchung, zählt")})
        marks = A.active_marks(ctx.db)
        mark = marks.get(f.id)
        parent = report.by_id(f.parent) if f.parent else None
        children = [c for c in (report.by_id(x) for x in f.children) if c is not None]
        return render(request, "diagnosis_case.html", active="quality", f=f, rows=rows, rec=recommend(report, f),
                      parent=parent, children=children, mark=mark, fixes=A.active_fixes(ctx.db).get(f.id, []),
                      msg=msg, err=err, **_common())

    @router.post("/quality/diagnose/mark")
    async def mark_route(request: Request) -> Response:
        """Entscheidung ohne Datenänderung: ablehnen, später prüfen, ungeklärt lassen."""
        ctx = get_ctx(request)
        form = await request.form()
        fid, action = str(form.get("f") or ""), str(form.get("action") or "")
        if not fid or action not in A.MARK_ACTIONS:
            raise HTTPException(400)
        res = await run_in_threadpool(A.mark, ctx, fid, action, str(form.get("note") or ""))
        q = {"msg": res.message} if res.ok else {"err": "; ".join(res.errors)}
        if str(form.get("back") or "") == "case":
            return _back(f"/quality/diagnose/case/{fid}?{urlencode(q)}")
        return _back(_page_url(msg=res.message, err="; ".join(res.errors), anchor="findings"))

    @router.get("/quality/diagnose/plan", response_class=HTMLResponse)
    def plan_page(request: Request, f: str = "", o: str = "") -> HTMLResponse:
        """Vorschau einer Lösung – rechnet auf einer Kopie im Speicher, ändert nichts."""
        params, given = _params(request.query_params)
        return _plan_page(request, f, o, params, given)

    @router.post("/quality/diagnose/apply")
    async def apply_route(request: Request) -> Response:
        ctx = get_ctx(request)
        form = await request.form()
        fid, opt, token = str(form.get("f") or ""), str(form.get("o") or ""), str(form.get("token") or "")
        if not fid or not opt or not token:
            raise HTTPException(400)
        params, _given = _params(form)
        res = await run_in_threadpool(A.apply, ctx, fid, opt, params, token)
        if not res.ok:
            return await run_in_threadpool(_plan_page, request, fid, opt, params, True, res.errors, 409)
        return _back(_page_url(msg=res.message, anchor="decisions"))

    @router.post("/quality/diagnose/live-balances")
    async def live_balances(request: Request) -> Response:
        """Aktuelle Bestände der Konten problematischer Positionen bei Börse bzw. Wallet abfragen (nur lesend)."""
        ctx = get_ctx(request)
        form = await request.form()
        if str(form.get("back") or "") == "integrity":
            from app.diagnosis import integrity as I

            run = I.load(ctx.db)
            accounts = {a for it in (run.items if run else []) if it.status == "offen" for a in it.accounts}
            back, target = "/quality/integrity", "/quality/integrity"
        else:
            report = await run_in_threadpool(report_for, ctx)
            accounts = {r["h"].account for r in _deviations(report, {})[0]}
            back, target = "/quality/diagnose#bestand", "/quality/diagnose"
        res = await run_in_threadpool(L.start, ctx, accounts, back)
        if res.get("error"):
            return _back(f"{target}?{urlencode({'err': res['error']})}")
        msg = (f"Bestandsabfrage gestartet: {res['count']} Datenquelle(n) – im Hintergrund, nur lesend; das Ergebnis "
               "erscheint danach als Ist-Bestand.")
        return _back(f"{target}?{urlencode({'msg': msg})}")

    @router.post("/quality/diagnose/dismiss")
    async def dismiss_route(request: Request) -> Response:
        ctx = get_ctx(request)
        form = await request.form()
        fid = str(form.get("f") or "")
        if not fid:
            raise HTTPException(400)
        res = await run_in_threadpool(A.dismiss, ctx, fid, str(form.get("note") or ""))
        if str(form.get("back") or "") == "/quality/integrity":
            q = urlencode({"msg": res.message} if res.ok else {"err": "; ".join(res.errors)})
            return _back(f"/quality/integrity?{q}#items")
        return _back(_page_url(msg=res.message, err="; ".join(res.errors), anchor="checked" if res.ok else ""))

    @router.post("/quality/diagnose/reference")
    async def reference_add(request: Request) -> Response:
        """Referenzbestand hinterlegen (Prüfwert, keine Buchung)."""
        from app.diagnosis import references as R

        form = await request.form()
        res = await run_in_threadpool(R.add, get_ctx(request), str(form.get("account") or ""),
                                      str(form.get("asset") or ""), str(form.get("qty") or ""),
                                      str(form.get("as_of") or ""), str(form.get("source") or "statement"),
                                      str(form.get("note") or ""), str(form.get("time") or ""),
                                      str(form.get("tz") or ""), str(form.get("basis") or ""))
        return _back(_page_url(msg=res.message, err="; ".join(res.errors), anchor="referenzen"))

    @router.post("/quality/diagnose/reference/{rid}/delete")
    async def reference_delete(request: Request, rid: int) -> Response:
        from app.diagnosis import references as R

        res = await run_in_threadpool(R.remove, get_ctx(request), rid)
        return _back(_page_url(msg=res.message, err="; ".join(res.errors), anchor="referenzen"))

    @router.post("/quality/diagnose/decision/{did}/undo")
    async def undo_route(request: Request, did: int) -> Response:
        res = await run_in_threadpool(A.undo, get_ctx(request), did)
        return _back(_page_url(msg=res.message, err="; ".join(res.errors), anchor="decisions"))

    @router.post("/quality/diagnose/decision/{did}/reopen")
    async def reopen_route(request: Request, did: int) -> Response:
        res = await run_in_threadpool(A.reopen, get_ctx(request), did)
        return _back(_page_url(msg=res.message, err="; ".join(res.errors), anchor="findings"))

    @router.get("/quality/diagnose/bulk", response_class=HTMLResponse)
    def bulk_page(request: Request, conf: str = "", account: str = "", asset: str = "", source: str = "",
                  sort: str = "", msg: str = "", err: str = "") -> HTMLResponse:
        """Sammelbearbeitung: Befunde des Abgleichs mit bevorzugter Lösung (nur Anzeige und Auswahl)."""
        from app.diagnosis import bulk as B
        from app.diagnosis.integrity import CONFIDENCE, _value_of

        ctx = get_ctx(request)
        report = report_for(ctx)
        marks = A.active_dismissals(ctx.db)
        rows = []
        for f, c, label in B.candidates(report):
            d = marks.get(f.id)
            if d is not None and d.fingerprint == A.fingerprint(f):
                continue  # geprüft bzw. als unabhängig bestätigt – Nutzerentscheidung gilt
            rows.append({"f": f, "conf": c, "label": label, "value": _value_of(report, f) or 0.0})
        acc_all = sorted({a for r in rows for a in r["f"].accounts})
        asset_all = sorted({a for r in rows for a in r["f"].assets})
        src_all = sorted({s for r in rows for s in r["f"].sources})
        shown = [r for r in rows if (not conf or r["conf"] == conf) and (not account or account in r["f"].accounts)
                 and (not asset or asset in r["f"].assets) and (not source or source in r["f"].sources)]
        rank = {c: i for i, c in enumerate(CONFIDENCE)}
        if sort == "impact":
            shown.sort(key=lambda r: (-r["value"], rank.get(r["conf"], 9)))
        else:
            shown.sort(key=lambda r: (rank.get(r["conf"], 9), -r["value"]))
        return render(request, "diagnosis_bulk.html", active="quality", rows=shown, confs=CONFIDENCE,
                      f={"conf": conf, "account": account, "asset": asset, "source": source, "sort": sort},
                      acc_all=acc_all, asset_all=asset_all, src_all=src_all, recent=B.recent(ctx.db), msg=msg,
                      err=err, **_common())

    def _bulk_preview(request: Request, ids: list[str], review: bool, errors: list[str] | None = None,
                      status_code: int = 200) -> HTMLResponse:
        from app.diagnosis import bulk as B

        ctx = get_ctx(request)
        bp = B.plan(ctx, ids, include_review=review)
        return render(request, "diagnosis_bulk_preview.html", active="quality", bp=bp, ids=ids, review=review,
                      errors=errors or [], effects=bp.effects, bulk=True, status_code=status_code, **_common())

    @router.get("/quality/diagnose/bulk/preview", response_class=HTMLResponse)
    def bulk_preview(request: Request) -> HTMLResponse:
        ids = [str(x) for x in request.query_params.getlist("f") if x]
        if not ids:
            return _back("/quality/diagnose/bulk?" + urlencode({"err": "Bitte mindestens einen Befund auswählen."}))
        return _bulk_preview(request, ids, request.query_params.get("review") == "1")

    @router.post("/quality/diagnose/bulk/apply")
    async def bulk_apply(request: Request) -> Response:
        from app.diagnosis import bulk as B

        ctx = get_ctx(request)
        form = await request.form()
        ids = [str(x) for x in form.getlist("f") if x]
        review, token = str(form.get("review") or "") == "1", str(form.get("token") or "")
        if not ids or not token:
            raise HTTPException(400)
        res = await run_in_threadpool(B.execute, ctx, ids, token, include_review=review)
        if not res.ok:
            return await run_in_threadpool(_bulk_preview, request, ids, review, res.errors, 409)
        return _back("/quality/diagnose/bulk?" + urlencode({"msg": res.message}))

    @router.post("/quality/diagnose/bulk/{bulk_id}/undo")
    async def bulk_undo(request: Request, bulk_id: str) -> Response:
        from app.diagnosis import bulk as B

        res = await run_in_threadpool(B.undo, get_ctx(request), bulk_id)
        q = {"msg": res.message} if res.ok else {"err": "; ".join(res.errors)}
        return _back("/quality/diagnose/bulk?" + urlencode(q))

    return router


register_router(make_router)
