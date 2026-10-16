"""Konten zusammenführen – Weboberfläche (``/accounts/merge``): Vorschläge, Vorschau, Bestätigung, Rückgängig."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from app.assetchange import account_merge as M
from app.assetchange import account_remove as R
from app.web.app import register_router
from app.web.deps import get_ctx, render


def _back(request: Request, target: str) -> Response:
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": target})
    return Response(status_code=303, headers={"Location": target})


def _natural(text: str) -> list[Any]:
    """Natürliche Sortierung („Account 2“ vor „Account 10“)."""
    return [int(x) if x.isdigit() else x.casefold() for x in re.split(r"(\d+)", text)]


def _accounts(ctx: Any) -> list[dict[str, Any]]:
    """Konten zur Auswahl, **nach Gruppe sortiert** (Wallet-Gruppe, Börsen, Konten ohne Datenquelle), je Gruppe
    natürlich nach Name. Auch Konten von Datenquellen ohne Buchungen; die Beschriftung nennt Buchungen, Datenquelle,
    Anbieter und gekürzte Adresse, damit gleichnamige Positionen unterscheidbar bleiben."""
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
        lst = srcs.get(name, [])
        group = ""
        for d in lst:
            bits = [str(d.name), str(d.provider_label)]
            if getattr(d, "is_wallet", False) and d.addresses:
                bits.append(M._short(d.addresses[0]))
            parts.append("Datenquelle " + " · ".join(dict.fromkeys(bits)))
            group = group or (f"Wallet-Gruppe {d.wallet_group}" if getattr(d, "wallet_group", "") else
                              "Wallets" if getattr(d, "is_wallet", False) else "Börsen")
        if not group:
            group = "Konten ohne Datenquelle"
        out.append({"name": name, "group": group, "label": f"{name} ({' · '.join(parts)})"})
    order = {"Konten ohne Datenquelle": 2, "Börsen": 1, "Wallets": 1}
    out.sort(key=lambda a: (order.get(a["group"], 0), _natural(a["group"]), _natural(a["name"])))
    return out


def _grouped(accounts: list[dict[str, Any]], exclude: str = "") -> list[tuple[str, list[dict[str, Any]]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for a in accounts:
        if a["name"] != exclude:
            groups.setdefault(a["group"], []).append(a)
    return list(groups.items())


def _page(request: Request, msg: str = "", err: str = "", alias: str = "", target: str = "",
          status_code: int = 200) -> HTMLResponse:
    ctx = get_ctx(request)
    accounts = _accounts(ctx)
    return render(request, "account_merge.html", status_code=status_code, active="quality", msg=msg, err=err,
                  hist=M.history(ctx.db), removed=R.history(ctx.db), alias=alias, target=target,
                  alias_groups=_grouped(accounts, target), target_groups=_grouped(accounts, alias),
                  all_groups=_grouped(accounts))


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

    @router.api_route("/accounts/remove/preview", methods=["GET", "POST"], response_class=HTMLResponse)
    async def remove_preview(request: Request, account: str = "") -> Response:
        ctx = get_ctx(request)
        if request.method == "POST":
            account = str((await request.form()).get("account") or "")
        account = account.strip()[:160]
        p = await run_in_threadpool(R.preview, ctx, account)
        if not p.ok and not p.tx_ids and not p.sources:
            return _page(request, err=" ".join(p.errors), status_code=400)
        return render(request, "account_remove_preview.html", active="quality", p=p)

    @router.post("/accounts/remove/apply")
    async def remove_apply(request: Request) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        account = str(f.get("account") or "").strip()[:160]
        if str(f.get("confirm") or "") != "1":
            return _back(request, "/accounts/merge?" + urlencode({"err": "Bitte die Vorschau bestätigen."}))
        rid, errors = await run_in_threadpool(R.apply, ctx, account, str(f.get("fp") or ""), str(f.get("note") or ""))
        if rid is None:
            return _back(request, "/accounts/merge?" + urlencode({"err": " ".join(errors)}))
        return _back(request, "/accounts/merge?" + urlencode({
            "msg": f"Konto „{account}“ ist ausgeblendet – unter „Durchgeführt“ rückgängig zu machen."}))

    @router.post("/accounts/remove/{rid}/revert")
    async def remove_revert(request: Request, rid: int) -> Response:
        errors = await run_in_threadpool(R.revert, get_ctx(request), rid)
        if errors:
            return _back(request, "/accounts/merge?" + urlencode({"err": " ".join(errors)}))
        return _back(request, "/accounts/merge?" + urlencode({"msg": "Konto wieder eingeblendet."}))

    return router


register_router(make_router)
