"""Einstellungen → Datenquellen: anlegen, ansehen, bearbeiten, (de)aktivieren, entfernen, API-Key verwalten,
Verbindung testen, synchronisieren.

Alle schreibenden Endpunkte sind POST und laufen durch CSRF-Schutz und (falls aktiv) Basic-Auth der App. Ein
eingegebener API-Key wird nie zurück an den Browser gegeben – weder in Formularen noch in Meldungen oder URLs.

Hintergrundjob ``datasources_sync`` (alle 5 Minuten): fällige, aktive Quellen mit Connector synchronisieren.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from app.datasources.chainhttp import ENDPOINTS
from app.datasources.connector import CREDENTIAL_RE, connector_for, supported
from app.datasources.providers import EXCHANGE, INTERVALS, KIND_LABEL, WALLET, contains_secret, providers
from app.datasources.service import PROVIDER_KEYS, RUN_STATUS_LABEL, STATUS_LABEL, datasource_service
from app.datasources.wallet import GAP_DEFAULT, SCRIPT_TYPES
from app.jobs.scheduler import Scheduler, extra_jobs
from app.web.app import register_router
from app.web.deps import get_ctx, render

log = logging.getLogger(__name__)

FORM_FIELDS = ("kind", "provider", "name", "account", "address", "credential_ref", "sync_interval_min", "auto_commit",
               "note", "key_expires_on", "wallet_group", "script", "gap", "chain_provider", "tokens", "tokens_shown")
WALLET_CHAINS = ("bitcoin", "ethereum", "bsc", "avalanche", "solana", "kaspa")  # mit Anbindung (Reihenfolge)


@extra_jobs
def _register(s: Scheduler) -> None:
    from apscheduler.triggers.interval import IntervalTrigger

    s.register("datasources_sync", lambda ctx: datasource_service(ctx).run_due(), IntervalTrigger(minutes=5))


def _back(request: Request, target: str) -> Response:
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": target})
    return Response(status_code=303, headers={"Location": target})


def _list_url(sid: int | None = None, **params: str) -> str:
    return "/settings/datasources" + ("?" + urlencode(params) if params else "") + (f"#ds-{sid}" if sid else "")


def _detail_url(sid: int, **params: str) -> str:
    return f"/settings/datasources/{sid}" + ("?" + urlencode(params) if params else "")


def _safe_echo(data: dict[str, Any]) -> dict[str, Any]:
    """Formular nach Fehlern erneut füllen – ohne mögliche Geheimnisse (Schlüssel, Seed) zurückzuspielen."""
    out = {k: v for k, v in data.items() if k != "api_key"}
    addr = out.get("address") or ""
    if contains_secret(addr) or "prv" in addr:
        out["address"] = ""
    ref = (out.get("credential_ref") or "").strip().upper()
    if ref and not CREDENTIAL_RE.match(ref):
        out["credential_ref"] = ""
    return out


def _form_page(request: Request, data: dict[str, Any], errors: list[str], sid: int | None = None,
               status_code: int = 200, msg: str = "", error: str = "") -> HTMLResponse:
    ctx = get_ctx(request)
    svc = datasource_service(ctx)
    ds = svc.get(sid) if sid else None
    kind = ds.kind if ds else (data.get("kind") if data.get("kind") in (EXCHANGE, WALLET) else EXCHANGE)
    provs = providers(kind)
    if kind == WALLET:  # Chains mit Anbindung zuerst
        provs = sorted(provs, key=lambda p: (p.id not in WALLET_CHAINS, WALLET_CHAINS.index(p.id)
                                             if p.id in WALLET_CHAINS else 0, p.label))
    chain_info = {}
    for p in provs:
        c = connector_for(p.id)
        if c is not None and getattr(c, "wallet", False):
            chain_info[p.id] = {"endpoints": [ENDPOINTS[e] for e in c.endpoints], "limits": list(c.limits),
                                "native": c.native}
    groups = sorted({d.group for d in svc.list() if d.is_wallet and d.group})
    return render(request, "datasource_form.html", status_code=status_code, active="settings", ds=ds, data=data,
                  errors=errors, kind=kind, kind_label=KIND_LABEL, providers=provs, intervals=INTERVALS,
                  supported_ids={p.id for p in provs if supported(p.id)},
                  accounts=svc.accounts(), runs=svc.runs(sid) if sid else [], run_status=RUN_STATUS_LABEL,
                  pending=svc.pending_batches(sid) if sid else [], open_counts=svc.open_counts(sid) if sid else {},
                  vault=svc.vault().status(), keys=svc.key_stats(), msg=msg, error=error, chain_info=chain_info,
                  script_types=SCRIPT_TYPES, gap_default=GAP_DEFAULT, groups=groups,
                  provider_keys={k["id"]: k for k in svc.provider_keys()},
                  holdings=svc.holdings(ds) if ds is not None and (ds.is_wallet or svc.balances(int(ds.id))) else None,
                  outdated=svc.outdated(int(ds.id)) if ds is not None else 0, quality=_quality(ctx, ds))


def _quality(ctx: Any, ds: Any) -> Any:
    from app.datasources.quality import report

    try:
        return report(ctx, ds)
    except Exception:  # Bericht ist Zusatzinformation – die Seite muss immer laden
        log.exception("Vollständigkeitsbericht für Datenquelle %s fehlgeschlagen", getattr(ds, "id", "?"))
        return None


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/settings/datasources", response_class=HTMLResponse)
    def index(request: Request, msg: str = "", error: str = "") -> HTMLResponse:
        ctx = get_ctx(request)
        svc = datasource_service(ctx)
        items = svc.list()
        pending = {ds.id: svc.pending_batches(ds.id) for ds in items}
        opens = {ds.id: svc.open_counts(ds.id) for ds in items}
        wallets = [ds for ds in items if ds.is_wallet]
        groups: dict[str, list[Any]] = {}
        for ds in sorted(wallets, key=lambda d: (d.group.lower() or "~", d.name.lower())):
            groups.setdefault(ds.group or "Ohne Gruppe", []).append(ds)
        holdings = {ds.id: svc.holdings(ds) for ds in wallets}
        return render(request, "datasources.html", active="settings", items=items, pending=pending, opens=opens,
                      msg=msg, error=error, status_label=STATUS_LABEL, keys=svc.key_stats(),
                      exchanges=[ds for ds in items if not ds.is_wallet], groups=groups, holdings=holdings,
                      provider_keys=svc.provider_keys())

    @router.get("/settings/datasources/new", response_class=HTMLResponse)
    def new_form(request: Request, kind: str = EXCHANGE, provider: str = "", group: str = "") -> HTMLResponse:
        return _form_page(request, {"kind": kind, "provider": provider, "sync_interval_min": "0",
                                    "wallet_group": group[:40], "tokens": "1", "gap": str(GAP_DEFAULT)}, [])

    @router.post("/settings/datasources")
    async def create(request: Request) -> Response:
        svc = datasource_service(get_ctx(request))
        f = await request.form()
        data = {k: str(f.get(k) or "") for k in FORM_FIELDS}
        data["api_key"] = str(f.get("api_key") or "")
        sid, errors = await run_in_threadpool(svc.create, data)
        data.pop("api_key", None)
        if errors and sid is None:
            return _form_page(request, _safe_echo(data), errors, status_code=400)
        if errors:
            return _back(request, _detail_url(int(sid), error=" ".join(errors)))  # type: ignore[arg-type]
        return _back(request, _detail_url(int(sid), msg=f"„{data['name'].strip()}“ angelegt."))  # type: ignore[arg-type]

    @router.get("/settings/datasources/{sid}", response_class=HTMLResponse)
    def edit_form(request: Request, sid: int, msg: str = "", error: str = "") -> HTMLResponse:
        ds = datasource_service(get_ctx(request)).get(sid)
        if ds is None:
            raise HTTPException(404)
        cols = set(ds.row.keys())
        data = {k: ("" if ds.row[k] is None else str(ds.row[k])) for k in FORM_FIELDS if k in cols}
        if ds.is_wallet:
            w = ds.watch
            data.update({"address": "\n".join(ds.addresses), "script": w.script or "", "gap": str(w.gap),
                         "chain_provider": w.provider or "", "tokens": "1" if w.tokens else ""})
        return _form_page(request, data, [], sid, msg=msg, error=error)

    @router.post("/settings/datasources/{sid}")
    async def update(request: Request, sid: int) -> Response:
        svc = datasource_service(get_ctx(request))
        if svc.get(sid) is None:
            raise HTTPException(404)
        f = await request.form()
        data = {k: str(f.get(k) or "") for k in FORM_FIELDS if k != "kind"}
        errors = await run_in_threadpool(svc.update, sid, data)
        if errors:
            return _form_page(request, _safe_echo(data), errors, sid, status_code=400)
        return _back(request, _list_url(sid, msg="Änderungen gespeichert."))

    @router.post("/settings/datasources/{sid}/key")
    async def set_key(request: Request, sid: int) -> Response:
        svc = datasource_service(get_ctx(request))
        ds = svc.get(sid)
        if ds is None:
            raise HTTPException(404)
        f = await request.form()
        value = str(f.get("api_key") or "")
        errors = await run_in_threadpool(svc.set_api_key, sid, value, str(f.get("key_expires_on") or "") or None)
        del value
        if errors:
            return _back(request, _detail_url(sid, error=" ".join(errors)) + "#zugang")
        return _back(request, _detail_url(sid, msg="API-Key verschlüsselt gespeichert – jetzt „Verbindung testen“.")
                     + "#zugang")

    @router.post("/settings/datasources/{sid}/key/delete")
    async def delete_key(request: Request, sid: int) -> Response:
        svc = datasource_service(get_ctx(request))
        if not await run_in_threadpool(svc.remove_api_key, sid):
            raise HTTPException(404)
        return _back(request, _detail_url(sid, msg="API-Key entfernt – übernommene Buchungen bleiben erhalten.")
                     + "#zugang")

    @router.post("/settings/datasources/keys/rotate")
    async def rotate(request: Request) -> Response:
        svc = datasource_service(get_ctx(request))
        res = await run_in_threadpool(svc.rotate_keys)
        if res["errors"]:
            return _back(request, _list_url(error=f"{res['rotated']} neu verschlüsselt; " + "; ".join(res["errors"])))
        return _back(request, _list_url(msg=f"{res['rotated']} Schlüssel mit dem aktuellen Master-Key neu "
                                            "verschlüsselt."))

    @router.post("/settings/datasources/{sid}/toggle")
    async def toggle(request: Request, sid: int) -> Response:
        svc = datasource_service(get_ctx(request))
        ds = svc.get(sid)
        if ds is None:
            raise HTTPException(404)
        await run_in_threadpool(svc.set_enabled, sid, not ds.enabled)
        text = f"„{ds.name}“ " + ("deaktiviert." if ds.enabled else "aktiviert.")
        return _back(request, _list_url(sid, msg=text))

    @router.post("/settings/datasources/{sid}/delete")
    async def delete(request: Request, sid: int) -> Response:
        svc = datasource_service(get_ctx(request))
        ds = svc.get(sid)
        if ds is None:
            raise HTTPException(404)
        await run_in_threadpool(svc.delete, sid)
        return _back(request, _list_url(msg=f"„{ds.name}“ entfernt – Zugangsdaten gelöscht, übernommene Buchungen "
                                            "bleiben erhalten."))

    @router.post("/settings/datasources/{sid}/reset")
    async def reset(request: Request, sid: int) -> Response:
        svc = datasource_service(get_ctx(request))
        if not await run_in_threadpool(svc.reset_cursor, sid):
            raise HTTPException(404)
        return _back(request, _list_url(sid, msg="Abrufstand zurückgesetzt – der nächste Lauf holt alle Vorgänge "
                                                 "erneut; bereits übernommene werden erkannt."))

    @router.post("/settings/datasources/{sid}/refetch")
    async def refetch(request: Request, sid: int) -> Response:
        """Vollständig neu abrufen (Abrufstand verwerfen, sofort synchronisieren) – ersetzt unbearbeitete Prüfzeilen
        älterer Auswertungen; übernommene Buchungen, Eingaben und Ignorier-Entscheidungen bleiben."""
        svc = datasource_service(get_ctx(request))
        ds = svc.get(sid)
        if ds is None:
            raise HTTPException(404)
        res = await run_in_threadpool(svc.refetch, sid)
        if res.get("batch_id") and not res.get("committed"):
            return _back(request, f"/journal/csv/{res['batch_id']}")
        key = "error" if (res.get("error") or res.get("unsupported")) else "msg"
        text = res.get("error") or res.get("unsupported") or res.get("message") or "Keine neuen Vorgänge."
        return _back(request, _detail_url(sid, **{key: text}) + "#status")

    @router.post("/settings/datasources/{sid}/check")
    async def check(request: Request, sid: int) -> Response:
        svc = datasource_service(get_ctx(request))
        if svc.get(sid) is None:
            raise HTTPException(404)
        ok, text = await run_in_threadpool(svc.check, sid)
        return _back(request, _detail_url(sid, **({"msg": text} if ok else {"error": text})) + "#status")

    @router.post("/settings/datasources/{sid}/sync")
    async def sync(request: Request, sid: int) -> Response:
        svc = datasource_service(get_ctx(request))
        ds = svc.get(sid)
        if ds is None:
            raise HTTPException(404)
        if ds.is_wallet and ds.supported:  # Wallets: im Hintergrund mit Fortschritt (Erstabruf in Etappen)
            res = await run_in_threadpool(svc.start_sync, sid, "manual")
            if res.get("error"):
                return _back(request, _detail_url(sid, error=res["error"]) + "#status")
            return _back(request, _detail_url(sid, msg="Abruf gestartet – Fortschritt unten.") + "#status")
        res = await run_in_threadpool(svc.sync, sid, "manual")
        if res.get("batch_id") and not res.get("committed"):
            return _back(request, f"/journal/csv/{res['batch_id']}")  # zur Prüfung
        key = "error" if (res.get("error") or res.get("unsupported")) else "msg"
        text = res.get("error") or res.get("unsupported") or res.get("message") or "Keine neuen Vorgänge."
        return _back(request, _list_url(sid, **{key: text}))

    @router.get("/settings/datasources/{sid}/progress", response_class=HTMLResponse)
    def progress(request: Request, sid: int) -> Response:
        """Fortschritt (für HTMX-Abfrage alle 2 s); nach dem Lauf lädt die Seite neu."""
        svc = datasource_service(get_ctx(request))
        ds = svc.get(sid)
        if ds is None:
            raise HTTPException(404)
        if not ds.progress.get("running"):
            return Response(status_code=204, headers={"HX-Redirect": _detail_url(sid) + "#status"})
        return render(request, "partials/ds_progress.html", ds=ds, p=ds.progress)

    @router.post("/settings/datasources/provider-keys/{provider}")
    async def set_provider_key(request: Request, provider: str) -> Response:
        if provider not in PROVIDER_KEYS:
            raise HTTPException(404)
        svc = datasource_service(get_ctx(request))
        f = await request.form()
        value = str(f.get("api_key") or "")
        errors = await run_in_threadpool(svc.set_provider_key, provider, value)
        del value
        back = str(f.get("back") or "")
        target = _detail_url(int(back)) if back.isdigit() else _list_url()
        if errors:
            sep = "&" if "?" in target else "?"
            return _back(request, target + sep + urlencode({"error": " ".join(errors)}) + "#anbieter")
        sep = "&" if "?" in target else "?"
        return _back(request, target + sep + urlencode({"msg": f"API-Key für {PROVIDER_KEYS[provider].label} "
                                                               "verschlüsselt gespeichert."}) + "#anbieter")

    @router.post("/settings/datasources/provider-keys/{provider}/delete")
    async def delete_provider_key(request: Request, provider: str) -> Response:
        if provider not in PROVIDER_KEYS:
            raise HTTPException(404)
        svc = datasource_service(get_ctx(request))
        await run_in_threadpool(svc.remove_provider_key, provider)
        return _back(request, _list_url(msg=f"API-Key für {PROVIDER_KEYS[provider].label} entfernt.") + "#anbieter")

    return router


register_router(make_router)
