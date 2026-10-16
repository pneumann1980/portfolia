"""Konten zusammenführen – Weboberfläche (``/accounts/merge``): Vorschläge, Vorschau, Bestätigung, Rückgängig."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from app.assetchange import account_merge as M
from app.web.app import register_router
from app.web.deps import get_ctx, render


def _back(request: Request, target: str) -> Response:
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": target})
    return Response(status_code=303, headers={"Location": target})


def _accounts(ctx: Any) -> list[dict[str, Any]]:
    """Konten zur Auswahl: mit Buchungen und/oder Datenquelle (auch ohne Buchungen – z. B. frisch angelegte Wallet)."""
    pf = ctx.portfolio()
    n: dict[str, int] = {}
    for t in (pf.txs if pf is not None else []):
        for a in {t.from_account, t.to_account}:
            if a:
                n[a] = n.get(a, 0) + 1
    srcs = M.source_accounts(ctx)
    out = []
    for name in M.known_accounts(ctx):
        parts = [f"{n[name]} Buchung{'' if n[name] == 1 else 'en'}" if n.get(name) else "keine Buchungen"]
        if name in srcs:
            parts.append("Datenquelle " + ", ".join(sorted({d.name for d in srcs[name]})))
        out.append({"name": name, "label": f"{name} ({' · '.join(parts)})"})
    return out


def _page(request: Request, msg: str = "", err: str = "", alias: str = "", target: str = "",
          status_code: int = 200) -> HTMLResponse:
    ctx = get_ctx(request)
    return render(request, "account_merge.html", status_code=status_code, active="quality", msg=msg, err=err,
                  cands=M.candidates(ctx), hist=M.history(ctx.db), accounts=_accounts(ctx), alias=alias,
                  target=target)


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/accounts/merge", response_class=HTMLResponse)
    def overview(request: Request, msg: str = "", err: str = "", alias: str = "", target: str = "") -> HTMLResponse:
        return _page(request, msg[:200], err[:300], alias[:160], target[:160])

    @router.post("/accounts/merge/preview", response_class=HTMLResponse)
    async def preview(request: Request) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        alias, target = str(f.get("alias") or "").strip()[:160], str(f.get("target") or "").strip()[:160]
        if str(f.get("swap") or "") == "1":
            alias, target = target, alias
        p = await run_in_threadpool(M.preview, ctx, alias, target)
        if not p.ok and not p.balances and not p.n_alias_txs:
            return _page(request, err=" ".join(p.errors), alias=alias, target=target, status_code=400)
        return render(request, "account_merge_preview.html", active="quality", p=p)

    @router.post("/accounts/merge/apply")
    async def apply(request: Request) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        alias, target = str(f.get("alias") or "").strip()[:160], str(f.get("target") or "").strip()[:160]
        if str(f.get("confirm") or "") != "1":
            return _back(request, "/accounts/merge?" + urlencode({"err": "Bitte die Vorschau bestätigen."}))
        mid, errors = await run_in_threadpool(M.apply, ctx, alias, target, str(f.get("fp") or ""),
                                              str(f.get("note") or ""))
        if mid is None:
            return _back(request, "/accounts/merge?" + urlencode({"err": " ".join(errors), "alias": alias,
                                                                   "target": target}))
        return _back(request, "/accounts/merge?" + urlencode({
            "msg": f"„{alias}“ ist jetzt „{target}“ zugeordnet – unter „Durchgeführt“ rückgängig zu machen."}))

    @router.post("/accounts/merge/{mid}/revert")
    async def revert(request: Request, mid: int) -> Response:
        errors = await run_in_threadpool(M.revert, get_ctx(request), mid)
        if errors:
            return _back(request, "/accounts/merge?" + urlencode({"err": " ".join(errors)}))
        return _back(request, "/accounts/merge?" + urlencode({"msg": "Zuordnung zurückgenommen."}))

    return router


register_router(make_router)
