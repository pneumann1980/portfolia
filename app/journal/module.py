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
from app.journal.service import TAX_TYPES, editable, journal_service, source_label, tx_form_data
from app.journal.writeoff import TAGS as WRITE_OFF_TAGS
from app.journal.writeoff import writeoff_service
from app.util.timeutil import today_local
from app.web.app import register_router
from app.web.deps import get_ctx, render

log = logging.getLogger(__name__)

PAGE = 200
ORIGINS = {"import": "Import", "journal": "manuell", "csv": "CSV-Import", "plan": "Sparplan"}
SAVED = {"created": "Buchung gespeichert.", "updated": "Änderung gespeichert.", "deleted": "Buchung gelöscht.",
         "restored": "Buchung wiederhergestellt.", "asset": "Asset gespeichert.",
         "unpaired": "Transfer aufgelöst – Ab- und Zugang gelten wieder einzeln."}
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


def _crypto_loss_option(ctx: Any) -> str | None:
    """Aktuelle Steuer-Einstellung „Verlust/Diebstahl von Kryptowerten“ (falls das Regelwerk sie kennt)."""
    try:
        from app.tax.service import tax_service

        svc = tax_service(ctx)
        pack = svc.pack()
        spec = next((o for o in pack.option_specs() if o.key == "lost"), None)
        if spec is None:
            return None
        val = svc.options(pack, today_local().year).get("lost", spec.default)
        return dict(spec.choices).get(val, str(val))
    except Exception as e:  # Steuer-Modul optional
        log.debug("Steuer-Einstellung nicht lesbar: %s", e)
        return None


def _writeoff_page(request: Request, *, show: str = "", asset: str = "", account: str = "", done: int = 0,
                   undone: int = 0, errors: list[str] | None = None, selected: set[str] | None = None,
                   form: dict[str, str] | None = None, status_code: int = 200) -> HTMLResponse:
    ctx = get_ctx(request)
    svc = writeoff_service(ctx)
    cands = svc.candidates()
    n_unvalued = sum(1 for c in cands if c.unvalued)
    if show not in ("unvalued", "all"):
        show = "all" if asset or not n_unvalued else "unvalued"
    rows = [c for c in cands if (show == "all" or c.unvalued) and (not asset or c.asset.asset_id == asset)
            and (not account or c.account == account)]
    if selected is None:
        selected = {c.key for c in rows} if asset else set()
    return render(request, "journal_writeoff.html", status_code=status_code, active="journal", rows=rows,
                  show=show, asset=asset, account=account, n_all=len(cands), n_unvalued=n_unvalued,
                  accounts=sorted({c.account for c in cands}, key=str.lower), selected=selected,
                  form=form or {"date": today_local().isoformat(), "tag": "lost", "note": "Ausbuchung (Totalverlust)"},
                  tags=WRITE_OFF_TAGS, errors=errors or [], done=done, undone=undone, recent=svc.recent(),
                  loss_option=_crypto_loss_option(ctx), today=today_local())


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
            years=svc.years(), origins=ORIGINS, tx_types=forms.TYPE_LABEL, source_label=source_label,
            jmeta=svc.meta([r["t"].tx_id for r in rows if r["kind"] in ("journal", "csv")]),
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

    @router.get("/journal/writeoff", response_class=HTMLResponse)
    def writeoff_page(request: Request, show: str = "", asset: str = "", account: str = "", done: int = 0,
                      undone: int = 0) -> HTMLResponse:
        return _writeoff_page(request, show=show, asset=asset, account=account, done=done, undone=undone)

    @router.post("/journal/writeoff")
    async def writeoff_book(request: Request) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        keys = [str(k) for k in f.getlist("sel")]
        day = str(f.get("date") or "")
        tag = str(f.get("tag") or "lost")
        note = str(f.get("note") or "").strip()[:200]
        res = await run_in_threadpool(writeoff_service(ctx).book, keys, day, tag, note)
        if res.errors:
            return _writeoff_page(request, show=str(f.get("show") or ""), asset=str(f.get("asset") or ""),
                                  account=str(f.get("account") or ""), errors=res.errors, selected=set(keys),
                                  form={"date": day, "tag": tag, "note": note}, status_code=400)
        return _back(request, "/journal/writeoff?" + urlencode({"done": len(res.tx_ids)}))

    @router.post("/journal/writeoff/undo")
    async def writeoff_undo(request: Request) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        n = await run_in_threadpool(writeoff_service(ctx).undo, [str(t) for t in f.getlist("tx")])
        return _back(request, "/journal/writeoff?" + urlencode({"undone": n}))

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
        if row is None or not editable(row):
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
        row = svc.get(tx_id)
        if not await run_in_threadpool(svc.delete, tx_id):
            raise HTTPException(404)
        if row is not None and row["source"] == "transfer":
            log.info("Transfer aufgelöst: %s", tx_id)
            return _back(request, "/journal?saved=unpaired")
        log.info("Buchung gelöscht: %s", tx_id)
        return _back(request, "/journal?saved=deleted")

    @router.post("/journal/{tx_id}/restore")
    async def restore(request: Request, tx_id: str) -> Response:
        svc = journal_service(get_ctx(request))
        if not await run_in_threadpool(svc.restore, tx_id):
            raise HTTPException(404)
        return _back(request, "/journal?" + urlencode({"saved": "restored", "tx": tx_id}))

    return router


register_router(make_router)
