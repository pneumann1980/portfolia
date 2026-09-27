"""Web-Oberfläche „Buchungen“: alle Buchungen, manuelle Erfassung, eigene Assets, Gesamtexport."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from app.importer import contract as C
from app.journal import forms
from app.journal.service import SOURCE_LABEL, TAX_TYPES, journal_service, tx_form_data
from app.util.timeutil import today_local
from app.web.app import register_router
from app.web.deps import get_ctx, render

log = logging.getLogger(__name__)

PAGE = 200
ORIGINS = {"import": "Import", "journal": "manuell", "plan": "Sparplan"}
SAVED = {"created": "Buchung gespeichert.", "updated": "Änderung gespeichert.", "deleted": "Buchung gelöscht.",
         "restored": "Buchung wiederhergestellt.", "asset": "Asset gespeichert."}
ASSET_FIELDS = ("asset_id", "name", "asset_class", "quote_source", "quote_id", "isin", "wkn", "category", "tax_type",
                "aliases", "note")


def _back(request: Request, target: str) -> Response:
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": target})
    return Response(status_code=303, headers={"Location": target})


def _form_page(request: Request, data: dict[str, Any], errors: list[str], tx_id: str | None = None,
               status_code: int = 200, saved_tx: str = "", warnings: list[str] | None = None) -> HTMLResponse:
    svc = journal_service(get_ctx(request))
    kind = data.get("kind") or "buy"
    if kind not in forms.KINDS:
        kind = "buy"
    assets = sorted(svc.known_assets().values(), key=lambda a: (a.is_fiat, a.name.lower()))
    fiat = sorted({a.asset_id for a in assets if a.is_fiat} | {"EUR", "USD", "CHF", "GBP"})
    return render(
        request, "journal_form.html", status_code=status_code, active="journal", kind=kind, kinds=forms.KINDS,
        kinds_short=forms.KIND_SHORT,
        hint=forms.KIND_HINTS[kind], data=data, errors=errors, tx_id=tx_id, accounts=svc.known_accounts(),
        assets=assets, fiat_options=[(c, c) for c in fiat], tags=forms.TAG_CHOICES.get(kind, []),
        tx_types=forms.TYPE_LABEL, known_tags=sorted(C.KNOWN_TAGS),
        action=f"/journal/{tx_id}/edit" if tx_id else "/journal/new", saved_tx=saved_tx, warnings=warnings or [],
    )


def _asset_page(request: Request, data: dict[str, Any], errors: list[str], orig_id: str | None,
                status_code: int = 200) -> HTMLResponse:
    svc = journal_service(get_ctx(request))
    cats = sorted({a.category for a in svc.known_assets().values() if a.category})
    nxt = str(request.query_params.get("next") or data.get("next") or "")
    return render(request, "journal_asset.html", status_code=status_code, active="journal", data=data,
                  errors=errors, orig_id=orig_id, categories=cats, tax_types=TAX_TYPES,
                  next_url=nxt if nxt.startswith("/journal/new") else "",
                  classes={"crypto": "Kryptowert", "security": "Wertpapier (Aktie, ETF, Anleihe …)",
                           "fiat": "Währung"},
                  quote_sources={"coingecko": "CoinGecko (Krypto)", "yahoo": "Yahoo Finance (Wertpapiere)",
                                 "manual": "manuelle Kurse", "none": "keine Kursquelle"})


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/journal", response_class=HTMLResponse)
    def journal_page(request: Request, account: str = "", asset: str = "", origin: str = "", year: str = "",
                     q: str = "", offset: int = 0, saved: str = "", tx: str = "") -> HTMLResponse:
        ctx = get_ctx(request)
        svc = journal_service(ctx)
        typ = request.query_params.get("type", "")
        offset = max(0, offset)
        rows, total = svc.listing(account=account, asset=asset, origin=origin, typ=typ, year=year, q=q,
                                  limit=PAGE, offset=offset)
        pf = ctx.portfolio()
        params = {"account": account, "asset": asset, "origin": origin, "type": typ, "year": year, "q": q}
        more = {**{k: v for k, v in params.items() if v}, "offset": offset + PAGE}
        warnings = [w for w in request.query_params.getlist("w") if w][:5]
        return render(
            request, "journal.html", active="journal", rows=rows, total=total, offset=offset, page=PAGE,
            more_url="/journal?" + urlencode(more), params=params, has_filter=any(params.values()),
            accounts=pf.all_accounts() if pf else [],
            assets=sorted(pf.assets.values(), key=lambda a: a.name.lower()) if pf else [],
            names={aid: a.name for aid, a in pf.assets.items()} if pf else {},
            syms={aid: a.symbol for aid, a in pf.assets.items()} if pf else {},
            years=svc.years(), origins=ORIGINS, tx_types=forms.TYPE_LABEL, source_label=SOURCE_LABEL,
            saved=SAVED.get(saved), saved_tx=tx, warnings=warnings, own_assets=svc.assets(),
            deleted=svc.deleted(), has_import=ctx.active_import_id() is not None,
        )

    @router.get("/journal/new", response_class=HTMLResponse)
    def new_form(request: Request, kind: str = "buy", copy: str = "", account: str = "",
                 saved: str = "") -> HTMLResponse:
        ctx = get_ctx(request)
        svc = journal_service(ctx)
        data: dict[str, Any] = {"kind": kind, "date": today_local().isoformat(), "ccy": "EUR"}
        if copy:
            row = svc.get(copy)
            if row is not None:
                data = svc.form_data(row)
            else:
                pf = ctx.portfolio()
                t = next((t for t in pf.txs if t.tx_id == copy), None) if pf else None
                if t is None:
                    raise HTTPException(404)
                data = tx_form_data(t)
            data |= {"date": today_local().isoformat(), "time": ""}
        if account and not data.get("account"):
            data["account"] = account
        warnings = [w for w in request.query_params.getlist("w") if w][:5]
        return _form_page(request, data, [], saved_tx=saved, warnings=warnings)

    @router.post("/journal/new")
    async def create(request: Request) -> Response:
        svc = journal_service(get_ctx(request))
        f = await request.form()
        data = {k: f.get(k) for k in forms.FORM_FIELDS}
        res = await run_in_threadpool(svc.save, data)
        if res.errors:
            return _form_page(request, data, res.errors, status_code=400)
        log.info("Manuelle Buchung angelegt: %s", ", ".join(res.tx_ids))
        warn = [("w", w) for w in res.warnings[:5]]
        if f.get("again") == "1":
            q = urlencode([("kind", data.get("kind") or "buy"), ("account", data.get("account") or ""),
                           ("saved", res.tx_ids[0]), *warn])
            return _back(request, f"/journal/new?{q}")
        return _back(request, "/journal?" + urlencode([("saved", "created"), ("tx", res.tx_ids[0]), *warn]))

    @router.get("/journal/asset", response_class=HTMLResponse)
    def asset_form(request: Request) -> HTMLResponse:
        svc = journal_service(get_ctx(request))
        aid = request.query_params.get("id", "")
        if not aid:
            return _asset_page(request, {"asset_class": "crypto", "quote_source": "coingecko"}, [], None)
        a = next((x["a"] for x in svc.assets() if x["a"].asset_id == aid), None)
        if a is None:
            raise HTTPException(404)
        data = {"asset_id": a.asset_id, "name": a.name, "asset_class": a.asset_class,
                "quote_source": a.quote_source, "quote_id": a.quote_id or "", "isin": a.isin or "",
                "wkn": a.wkn or "", "category": a.category or "", "tax_type": a.extra.get("tax_type", ""),
                "aliases": ";".join(a.aliases), "note": a.note or ""}
        return _asset_page(request, data, [], a.asset_id)

    @router.post("/journal/asset")
    async def asset_save(request: Request) -> Response:
        svc = journal_service(get_ctx(request))
        f = await request.form()
        data = {k: f.get(k) for k in (*ASSET_FIELDS, "next")}
        orig = str(f.get("orig_id") or "") or None
        res = await run_in_threadpool(svc.save_asset, data, orig)
        if res.errors:
            return _asset_page(request, data, res.errors, orig, status_code=400)
        nxt = str(f.get("next") or "")
        if nxt.startswith("/journal/new"):
            return _back(request, nxt)
        return _back(request, "/journal?" + urlencode({"saved": "asset"}) + "#assets")

    @router.get("/journal/export.zip")
    def export(request: Request) -> Response:
        svc = journal_service(get_ctx(request))
        try:
            body = svc.export_zip()
        except ValueError as e:
            raise HTTPException(404, str(e)) from e
        name = f"portfolia-export-{today_local().isoformat()}.zip"
        return Response(body, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"',
                                 "Cache-Control": "private, no-store"})

    @router.get("/journal/{tx_id}/edit", response_class=HTMLResponse)
    def edit_form(request: Request, tx_id: str) -> HTMLResponse:
        svc = journal_service(get_ctx(request))
        row = svc.get(tx_id)
        if row is None or row["status"] != "active" or row["source"] != "manual" or row["group_ref"]:
            raise HTTPException(404)
        errors = (["Diese Buchung ist inzwischen im Import enthalten (gleiche ID) – Änderungen bitte im Import "
                   "vornehmen."] if svc.in_import(tx_id) else [])
        return _form_page(request, svc.form_data(row), errors, tx_id=tx_id)

    @router.post("/journal/{tx_id}/edit")
    async def edit_save(request: Request, tx_id: str) -> Response:
        svc = journal_service(get_ctx(request))
        f = await request.form()
        data = {k: f.get(k) for k in forms.FORM_FIELDS}
        res = await run_in_threadpool(svc.save, data, tx_id)
        if res.errors:
            return _form_page(request, data, res.errors, tx_id=tx_id, status_code=400)
        warn = [("w", w) for w in res.warnings[:5]]
        return _back(request, "/journal?" + urlencode([("saved", "updated"), ("tx", tx_id), *warn]))

    @router.post("/journal/{tx_id}/delete")
    async def delete(request: Request, tx_id: str) -> Response:
        svc = journal_service(get_ctx(request))
        if not await run_in_threadpool(svc.delete, tx_id):
            raise HTTPException(404)
        log.info("Manuelle Buchung gelöscht: %s", tx_id)
        return _back(request, "/journal?saved=deleted")

    @router.post("/journal/{tx_id}/restore")
    async def restore(request: Request, tx_id: str) -> Response:
        svc = journal_service(get_ctx(request))
        if not await run_in_threadpool(svc.restore, tx_id):
            raise HTTPException(404)
        return _back(request, "/journal?" + urlencode({"saved": "restored", "tx": tx_id}))

    return router


register_router(make_router)
