"""Web-Oberfläche „Steuern & Haltefristen“ (registriert sich beim Import in main)."""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from app.importer import contract as C
from app.tax import registry
from app.tax.base import RulePack, fmt_value
from app.tax.classify import ACCOUNT_KINDS, ASSET_TYPES, securities_accounts
from app.tax.service import tax_service
from app.util.timeutil import today_local
from app.web.app import register_router
from app.web.deps import get_ctx, render

log = logging.getLogger(__name__)
HTML_ROWS = 150


def _num(v: Any) -> str | None:
    """Deutsche oder englische Zahleneingabe → normalisierter Dezimal-String; leer → None."""
    s = str(v or "").strip().replace(" ", "").replace("€", "").replace("%", "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    if not d.is_finite() or abs(d) > Decimal("1e12"):
        return None
    return format(d.normalize(), "f")


def _default_year(years: list[int]) -> int:
    today = today_local()
    past = [y for y in years if y < today.year]
    return max(past) if past else today.year


def _income_tags(ctx: Any) -> list[str]:
    led = ctx.ledger()
    tags = sorted({e.tag for e in (led.income if led else [])} & set(C.INCOME_TAGS) - {"dividend"})
    return tags


def _setup_filters(request: Request) -> None:
    env = request.app.state.templates.env
    if "tcell" not in env.filters:
        env.filters["tcell"] = fmt_value


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/tax", response_class=HTMLResponse)
    async def tax_page(request: Request, year: int | None = None, saved: str | None = None) -> HTMLResponse:
        ctx = get_ctx(request)
        _setup_filters(request)
        if ctx.portfolio() is None:
            return render(request, "empty.html", active="tax")
        svc = tax_service(ctx)
        pack, inp, ov = await run_in_threadpool(svc.overview)
        years = svc.data_years(inp) if inp is not None else []
        sel = year if year in years else _default_year(years)
        _, _, res = await run_in_threadpool(svc.compute, sel)
        pf = ctx.portfolio()
        assert pf is not None and inp is not None
        sec_accounts = securities_accounts(pf)
        securities = sorted((a for a in pf.assets.values() if a.is_security), key=lambda a: a.name)
        return render(
            request, "tax.html", active="tax", pack=pack, packs=registry.available(), ov=ov, res=res, years=years,
            year=sel, options=svc.options(pack, sel), specs=pack.option_specs(), income_tags=_income_tags(ctx),
            reports=svc.reports(), documents=pack.documents(), sec_accounts=sec_accounts, securities=securities,
            account_kinds=inp.account_kinds, account_kind_source=inp.account_kind_source,
            asset_types=inp.asset_types, asset_type_source=inp.asset_type_source, ACCOUNT_KINDS=ACCOUNT_KINDS,
            ASSET_TYPES=ASSET_TYPES, profile=ctx.settings.get("tax.profile") or {}, saved=saved,
            params_meta=pack.params.meta, override_error=pack.params.override_error,
            override_active=pack.params.override_active, override_path=str(pack.params.override_path or ""),
            html_rows=HTML_ROWS, today=today_local(),
        )

    @router.post("/tax/options")
    async def save_options(request: Request) -> Response:
        ctx = get_ctx(request)
        svc = tax_service(ctx)
        pack = svc.pack()
        f = await request.form()
        try:
            year = int(str(f.get("year") or today_local().year))
        except ValueError:
            year = today_local().year
        values: dict[str, Any] = {}
        for spec in pack.option_specs():
            if spec.kind == "bool":
                values[spec.key] = f.get(spec.key) == "1"
            elif spec.kind == "choice":
                v = str(f.get(spec.key) or "")
                if v in dict(spec.choices):
                    values[spec.key] = v
            elif spec.kind in ("amount", "percent"):
                if spec.key in f:
                    n = _num(f.get(spec.key))
                    if spec.kind == "amount" and n is not None and Decimal(n) < 0:
                        n = None
                    if spec.kind == "percent" and n is not None and not (0 <= Decimal(n) <= 100):
                        n = None
                    values[spec.key] = n if n is not None else (0 if spec.kind == "amount" else None)
            elif spec.kind == "map":
                current = dict(svc.options(pack, year).get(spec.key) or spec.default or {})
                allowed = dict(spec.choices)
                for k, v in f.multi_items():
                    if k.startswith(f"{spec.key}__") and str(v) in allowed:
                        current[k.split("__", 1)[1]] = str(v)
                values[spec.key] = current
            elif spec.kind == "text":
                values[spec.key] = str(f.get(spec.key) or "")[:200]
        svc.save_options(pack, year, values)
        log.info("Steueroptionen gespeichert (%s, %s)", pack.id, year)
        return _back(request, f"/tax?year={year}&saved=options#options")

    @router.post("/tax/mapping")
    async def save_mapping(request: Request) -> Response:
        ctx = get_ctx(request)
        pf = ctx.portfolio()
        f = await request.form()
        year = str(f.get("year") or "")
        if pf is not None:
            kinds = dict(ctx.settings.get("tax.account_withholding") or {})
            for acc in securities_accounts(pf):
                v = str(f.get(f"acc__{acc}") or "")
                if v in ACCOUNT_KINDS:
                    kinds[acc] = v
                elif v == "":
                    kinds.pop(acc, None)
            ctx.settings.set("tax.account_withholding", kinds)
            types = dict(ctx.settings.get("tax.asset_types") or {})
            for a in pf.assets.values():
                if not a.is_security:
                    continue
                v = str(f.get(f"type__{a.asset_id}") or "")
                if v in ASSET_TYPES:
                    types[a.asset_id] = v
                elif v == "":
                    types.pop(a.asset_id, None)
            ctx.settings.set("tax.asset_types", types)
        return _back(request, f"/tax?year={year}&saved=mapping#mapping")

    @router.post("/tax/profile")
    async def save_profile(request: Request) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        prof = {"name": str(f.get("name") or "").strip()[:120],
                "tax_id": re.sub(r"[^0-9 ]", "", str(f.get("tax_id") or ""))[:20].strip(),
                "tax_number": re.sub(r"[^0-9/ ]", "", str(f.get("tax_number") or ""))[:30].strip()}
        ctx.settings.set("tax.profile", prof)
        return _back(request, f"/tax?year={f.get('year') or ''}&saved=profile#profile")

    @router.post("/tax/pack")
    async def select_pack(request: Request) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        pid = str(f.get("pack") or "auto")
        if pid == "auto" or registry.get(pid) is not None:
            ctx.settings.set("tax.rulepack", pid)
        return _back(request, "/tax")

    @router.post("/tax/report", response_class=HTMLResponse)
    async def create_report(request: Request) -> Response:
        ctx = get_ctx(request)
        svc = tax_service(ctx)
        f = await request.form()
        try:
            year = int(str(f.get("year")))
        except (TypeError, ValueError):
            raise HTTPException(400, "Jahr fehlt") from None
        docs = [str(v) for k, v in f.multi_items() if k == "doc"]
        try:
            out = await run_in_threadpool(svc.generate, year, docs or None)
        except ValueError as e:
            return HTMLResponse(f'<div class="alert warn"><span class="icon">!</span><div>{e}</div></div>')
        except Exception as e:  # PDF-Fehler sichtbar machen, Details ins Log
            log.error("Steuerbericht fehlgeschlagen: %s", e)
            return HTMLResponse('<div class="alert crit"><span class="icon">!</span><div>Bericht konnte nicht erzeugt '
                                'werden – Details im Ereignisprotokoll (Datenqualität).</div></div>', status_code=500)
        return _back(request, f"/tax?year={year}&saved=report{out['id']}#reports")

    @router.get("/tax/report/{rid}/{doc_id}.pdf")
    def download(request: Request, rid: int, doc_id: str) -> FileResponse:
        ctx = get_ctx(request)
        found = tax_service(ctx).report_file(rid, doc_id)
        if found is None:
            raise HTTPException(404)
        path, name = found
        inline = request.query_params.get("inline") == "1"
        return FileResponse(path, media_type="application/pdf", filename=name,
                            content_disposition_type="inline" if inline else "attachment",
                            headers={"Cache-Control": "private, no-store"})

    @router.post("/tax/report/{rid}/delete")
    def delete_report(request: Request, rid: int) -> Response:
        ctx = get_ctx(request)
        tax_service(ctx).delete_report(rid)
        return _back(request, "/tax#reports")

    @router.get("/tax/quality", response_class=HTMLResponse)
    async def tax_quality(request: Request) -> HTMLResponse:
        ctx = get_ctx(request)
        rows: list[tuple[int, Any]] = []
        pack_name = ""
        if ctx.portfolio() is not None:
            svc = tax_service(ctx)

            def collect() -> None:
                nonlocal pack_name
                pack, inp, _ = svc.overview()
                pack_name = pack.name
                for y in reversed(svc.data_years(inp) if inp is not None else []):
                    _, _, res = svc.compute(y)
                    rows.extend((y, i) for i in (res.issues if res else []) if i.severity != "info")

            await run_in_threadpool(collect)
        return render(request, "partials/tax_quality.html", alerts=[], rows=rows[:200], pack_name=pack_name)

    @router.get("/api/tax/releases")
    def releases(request: Request) -> JSONResponse:
        ctx = get_ctx(request)
        if ctx.portfolio() is None:
            return JSONResponse({"months": []})
        _, _, ov = tax_service(ctx).overview()
        months = [{"month": m["month"], "value": float(m["value"]), "gain": float(m["gain"]), "count": m["count"]}
                  for m in (ov.release_months if ov else [])]
        return JSONResponse({"months": months})

    return router


def _back(request: Request, target: str) -> Response:
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": target})
    return Response(status_code=303, headers={"Location": target})


def active_pack(ctx: Any) -> RulePack:
    return tax_service(ctx).pack()


register_router(make_router)
