"""Web-Oberfläche „CSV-Import“ (unter Buchungen): Hochladen, Vorschau, Zuordnen, Übernehmen, Rückgängig."""

from __future__ import annotations

import html
import json
import logging
import re
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.csvimport import model as M
from app.csvimport.profiles import BUILTIN, MAPPING_FIELDS, MappingProfile
from app.csvimport.reader import CsvError, read_table
from app.csvimport.service import (
    BATCH_STATUS,
    EVAL_VERSION,
    MAX_UPLOAD,
    PAGE,
    STATUS_BADGE,
    STATUS_LABEL,
    csv_service,
    pair_accepted,
)
from app.csvimport.suggest import batch_suggestions
from app.jobs.scheduler import Scheduler, extra_jobs
from app.journal import forms
from app.journal.service import TAX_TYPES, journal_service, source_label
from app.prices.sources import catalog_state, source_service
from app.web.app import register_router
from app.web.deps import get_ctx, render

log = logging.getLogger(__name__)

GROUPS = ("Börse", "Wallet", "Steuertool", "Portfolia")
CONF_BADGE = {"hoch": "good", "mittel": "info", "niedrig": "warn"}
TZ_CHOICES = [("", "wie Format (Standard)"), ("UTC", "UTC"), ("Europe/Berlin", "Europe/Berlin (Ortszeit)")]
FILTERS = {"": "alle", "new": "neu", "unclear": "ungeklärt", "duplicate": "Dubletten", "invalid": "unvollständig",
           "before": "vor Stichtag", "known": "bereits vorhanden", "ignored": "ignoriert", "committed": "übernommen"}
MAPPING_KINDS = {"trade": "Handel/Tausch", "deposit": "Zugang ohne Ertrag", "withdrawal": "Abgang ohne Kosten",
                 "conversion": "Umstellung (ohne Veräußerung)", "skip": "überspringen",
                 **{f"deposit:{t}": f"Ertrag: {t}" for t in ("staking", "reward", "interest", "lending", "airdrop",
                                                           "mining", "bonus", "cashback", "other_income")},
                 **{f"withdrawal:{t}": f"Abgang: {t}" for t in ("cost", "fee", "gift", "donation", "lost")}}


def _back(request: Request, target: str) -> Response:
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": target})
    return Response(status_code=303, headers={"Location": target})


def _profiles_grouped(svc: Any) -> list[tuple[str, list[Any]]]:
    out = [(g, [p for p in BUILTIN if p.group == g]) for g in GROUPS]
    own = svc.mapping_profiles()
    if own:
        out.append(("Eigene Formate", own))
    return out


def _index(request: Request, errors: list[str] | None = None, status_code: int = 200,
           form: dict[str, Any] | None = None) -> HTMLResponse:
    ctx = get_ctx(request)
    svc = csv_service(ctx)
    batches = []
    for b in svc.batches():
        summ = json.loads(b["summary_json"] or "{}")
        counts = {r["status"]: r["n"] for r in ctx.db.q("SELECT status, COUNT(*) n FROM csv_row WHERE batch_id=? "
                                                        "GROUP BY status", (b["id"],))}
        prof = svc.profile(b["profile"])
        label = prof.label if prof else source_label(b["source"]) if b["kind"] == "sync" else "unbekannt"
        batches.append({"b": b, "summary": summ, "counts": counts, "profile": label,
                        "status": BATCH_STATUS.get(b["status"], b["status"])})
    base = ctx.base_portfolio()
    return render(request, "csv_import.html", status_code=status_code, active="journal", errors=errors or [],
                  groups=_profiles_grouped(svc), batches=batches, tz_choices=TZ_CHOICES, form=form or {},
                  accounts=journal_service(ctx).known_accounts(), symbols=svc.symbol_rows(),
                  account_maps=svc.account_rows(), mappings=svc.mapping_profiles(),
                  cutoff=base.valuation_date.isoformat() if base is not None and base.valuation_date else "",
                  max_mb=MAX_UPLOAD // 1024 // 1024)


def _account_info(ctx: Any, svc: Any, b: Any) -> dict[str, Any] | None:
    """Konto laut Abgleich: Umstellung einer Datenquelle (automatisch, einmal) bzw. Vorschlag mit einem Klick."""
    if b["status"] not in ("preview", "partial"):
        return None
    if b["kind"] == "sync":
        if not b["datasource_id"]:
            return None
        from app.datasources.service import datasource_service

        dsvc = datasource_service(ctx)
        ds = dsvc.get(int(b["datasource_id"]))
        if ds is None:
            return None
        if b["status"] == "preview" and dsvc.adopt_account(ds):
            ds = dsvc.get(int(b["datasource_id"])) or ds
        return {"current": ds.account, "switch": dsvc.account_switch(int(ds.id)),
                "suggestion": dsvc.account_suggestion(ds), "editable": True}
    accs = (json.loads(b["summary_json"] or "{}").get("recon") or {}).get("accounts") or {}
    if not accs:
        return None
    top, n = max(accs.items(), key=lambda kv: kv[1])
    if top == b["account"]:
        return None
    committed = bool(svc.db.scalar("SELECT 1 FROM csv_row WHERE batch_id=? AND status IN ('committed', 'merged')",
                                   (b["id"],)))
    return {"current": b["account"], "switch": None, "editable": not committed,
            "suggestion": {"account": top, "matches": n, "total": sum(accs.values()), "used": 0,
                           "others": sorted(((a, k) for a, k in accs.items() if a != top), key=lambda x: -x[1])[:3]}}


def _batch_page(request: Request, bid: int, status: str = "", offset: int = 0, errors: list[str] | None = None,
                msg: str = "") -> HTMLResponse:
    ctx = get_ctx(request)
    svc = csv_service(ctx)
    b = svc.batch(bid)
    if b is None:
        raise HTTPException(404)
    if b["status"] in ("preview", "partial") and \
            json.loads(b["summary_json"] or "{}").get("eval_v") != EVAL_VERSION:
        svc.evaluate(bid)  # mit älterer Logik bewertet (z. B. vor dem Abgleich über den Hash) → neu bewerten
        b = svc.batch(bid)
    acct = _account_info(ctx, svc, b)
    b = svc.batch(bid)
    prof = svc.profile(b["profile"])
    ov = svc.overview(bid) if b["status"] != "mapping" else {"counts": {}, "unknown": [], "unknown_old": [],
                                                            "accounts": {}, "missing_price": {}, "pairs": [],
                                                            "to_commit": 0, "total": 0, "by_idx": {}, "recon": None,
                                                            "auto_symbols": []}
    rows = list(ov["by_idx"].values())
    if status == "committed":
        sel = [rc for rc in rows if rc.status in ("committed", "merged")]
    elif status:
        sel = [rc for rc in rows if rc.status == status]
    else:
        sel = rows
    sel.sort(key=lambda rc: (rc.status not in ("invalid", "duplicate"), rc.idx))
    offset = max(0, offset)
    page = sel[offset:offset + PAGE]
    js = journal_service(ctx)
    known = js.known_assets()
    assets = sorted(known.values(), key=lambda a: (a.is_fiat, a.name.lower()))
    sugg: dict[str, Any] = {}
    catalog: dict[str, Any] = {}
    if (ov["unknown"] or ov["unknown_old"]) and b["status"] in ("preview", "partial", "committed"):
        sugg, catalog = batch_suggestions(ctx, b, ov, known)
    job = ctx.db.q1("SELECT running, progress_json, last_end, last_ok, last_error FROM job_status WHERE "
                    "job='csv_prices'")
    acc_rows = svc.saved_accounts()
    return render(
        request, "csv_batch.html", active="journal", b=b, prof=prof, summary=json.loads(b["summary_json"] or "{}"),
        options=svc.options(b), ov=ov, rows=page, total_sel=len(sel), offset=offset, page=PAGE, status=status,
        filters=FILTERS, status_label=STATUS_LABEL, status_badge=STATUS_BADGE, batch_status=BATCH_STATUS,
        assets=assets, known_coins=M.KNOWN_COINS, accounts=js.known_accounts(), errors=errors or [], msg=msg,
        tz_choices=TZ_CHOICES, job=job, job_progress=json.loads(job["progress_json"] or "{}") if job else {},
        acc_map=acc_rows, pair_accepted=pair_accepted, tax_types=TAX_TYPES, groups=_profiles_grouped(svc),
        tx_types=forms.TYPE_LABEL, tag_label=forms.TAG_LABEL,
        more_url=f"/journal/csv/{bid}?" + urlencode({"status": status, "offset": offset + PAGE}),
        kind_label=M.KIND_LABEL, source_label=source_label,
        ds_exists=bool(b["datasource_id"] and ctx.db.scalar("SELECT 1 FROM data_source WHERE id=?",
                                                              (b["datasource_id"],))),
        sugg=sugg, catalog=catalog, conf_badge=CONF_BADGE, acct=acct,
    )


def _mapping_page(request: Request, bid: int, errors: list[str] | None = None,
                  spec: dict[str, Any] | None = None) -> HTMLResponse:
    ctx = get_ctx(request)
    svc = csv_service(ctx)
    b = svc.batch(bid)
    if b is None:
        raise HTTPException(404)
    try:
        table = read_table(svc.raw(b))
    except CsvError as e:
        return render(request, "csv_mapping.html", status_code=400, active="journal", b=b, errors=[str(e)],
                      header=[], sample=[], fields=MAPPING_FIELDS, spec={}, kinds=MAPPING_KINDS,
                      tz_choices=TZ_CHOICES)
    spec = spec or {}
    if not spec and b["profile"].startswith("mapping:"):
        p = svc.profile(b["profile"])
        spec = dict(p.spec) if isinstance(p, MappingProfile) else {}
    if not spec:
        spec = {"columns": _guess_columns(table.header), "name": b["filename"].rsplit(".", 1)[0][:40]}
    sample = [dict(zip(table.header, r, strict=False)) for r in table.rows[:6]]
    labels = []
    type_col = (spec.get("columns") or {}).get("type")
    if type_col:
        idx = table.header.index(type_col) if type_col in table.header else None
        if idx is not None:
            labels = sorted({r[idx].strip() for r in table.rows if len(r) > idx and r[idx].strip()})[:40]
    return render(request, "csv_mapping.html", active="journal", b=b, errors=errors or [], header=table.header,
                  sample=sample, fields=MAPPING_FIELDS, spec=spec, kinds=MAPPING_KINDS, labels=labels,
                  tz_choices=TZ_CHOICES, type_map=spec.get("type_map") or {})


def _guess_columns(header: list[str]) -> dict[str, str]:
    """Naheliegende Spalten vorschlagen (deutsch/englisch)."""
    words = {
        "date": ("datum", "date", "zeit", "time", "timestamp"),
        "type": ("art", "typ", "type", "label", "vorgang", "operation", "kind"),
        "in_qty": ("eingang", "received amount", "buy amount", "incoming amount", "erhalten", "zugang"),
        "in_sym": ("eingang währung", "received currency", "buy currency", "incoming asset", "zugang währung"),
        "out_qty": ("ausgang", "sent amount", "sell amount", "outgoing amount", "gesendet", "abgang"),
        "out_sym": ("ausgang währung", "sent currency", "sell currency", "outgoing asset", "abgang währung"),
        "fee_qty": ("gebühr", "fee", "fee amount", "gebühren"),
        "fee_sym": ("gebühr währung", "fee currency", "fee asset"),
        "value": ("wert", "value", "net worth amount", "gegenwert", "betrag eur"),
        "ext_id": ("id", "transaction id", "txid", "transaktions-id", "trade id"),
        "txhash": ("txhash", "hash", "transaction hash"),
        "note": ("notiz", "note", "description", "beschreibung", "kommentar", "comment"),
        "account": ("konto", "wallet", "exchange", "börse", "account"),
    }
    out: dict[str, str] = {}
    low = {h.strip().lower(): h for h in header}
    for f, cands in words.items():
        for c in cands:
            if c in low and low[c] not in out.values():
                out[f] = low[c]
                break
    return out


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/journal/csv", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return _index(request)

    @router.post("/journal/csv")
    async def upload(request: Request) -> Response:
        ctx = get_ctx(request)
        svc = csv_service(ctx)
        f = await request.form(max_files=1, max_fields=40)
        file = f.get("file")
        form = {k: str(f.get(k) or "") for k in ("profile", "account", "tz", "decimal", "dayfirst", "default_asset",
                                                 "cutoff", "accounts_from_file")}
        if not isinstance(file, UploadFile) or not file.filename:
            return _index(request, ["Bitte eine CSV-Datei auswählen."], 400, form)
        data = await file.read(MAX_UPLOAD + 1)
        await file.close()
        if len(data) > MAX_UPLOAD:
            return _index(request, [f"Datei zu groß (höchstens {MAX_UPLOAD // 1024 // 1024} MB)."], 413, form)
        profile = form["profile"] or "auto"
        opts = {k: form[k] for k in ("tz", "decimal", "dayfirst", "default_asset", "accounts_from_file") if form[k]}
        if form["cutoff"] or f.get("cutoff_set") == "1":
            opts["cutoff"] = form["cutoff"]
        bid, errors = await run_in_threadpool(svc.upload, data, file.filename, profile if profile != "mapping:new"
                                              else "auto", form["account"], opts)
        if bid is None:
            return _index(request, errors, 400, form)
        b = svc.batch(bid)
        if profile == "mapping:new" or (b is not None and b["status"] == "mapping"):
            return _back(request, f"/journal/csv/{bid}/mapping")
        return _back(request, f"/journal/csv/{bid}")

    @router.get("/journal/csv/{bid}", response_class=HTMLResponse)
    def batch_page(request: Request, bid: int, status: str = "", offset: int = 0, msg: str = "") -> Response:
        svc = csv_service(get_ctx(request))
        b = svc.batch(bid)
        if b is None:
            raise HTTPException(404)
        if b["status"] == "mapping":
            return _back(request, f"/journal/csv/{bid}/mapping")
        return _batch_page(request, bid, status if status in FILTERS else "", offset, msg=msg)

    @router.get("/journal/csv/{bid}/file")
    def original(request: Request, bid: int) -> Response:
        svc = csv_service(get_ctx(request))
        b = svc.batch(bid)
        if b is None:
            raise HTTPException(404)
        sync = b["kind"] == "sync"
        name = b["filename"] + (".json" if sync else "")
        ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", name) or "import.csv"
        disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"
        return Response(svc.raw(b), media_type="application/json" if sync else "text/csv; charset=utf-8",
                        headers={"Content-Disposition": disposition, "Cache-Control": "private, no-store"})

    async def _form(request: Request) -> dict[str, Any]:
        f = await request.form(max_fields=5000)
        return {k: f.get(k) for k in f}

    def _redir(bid: int, f: dict[str, Any], msg: str = "") -> str:
        q = {k: v for k, v in (("status", f.get("_status") or ""), ("offset", f.get("_offset") or ""),
                               ("msg", msg)) if v}
        return f"/journal/csv/{bid}" + (f"?{urlencode(q)}" if q else "")

    @router.post("/journal/csv/{bid}/options")
    async def options(request: Request, bid: int) -> Response:
        svc = csv_service(get_ctx(request))
        f = await _form(request)
        errors = await run_in_threadpool(svc.set_options, bid, f)
        if errors:
            return _batch_page(request, bid, errors=errors)
        return _back(request, _redir(bid, f, "options"))

    @router.post("/journal/csv/{bid}/profile")
    async def set_profile(request: Request, bid: int) -> Response:
        svc = csv_service(get_ctx(request))
        f = await _form(request)
        pid = str(f.get("profile") or "")
        if pid == "mapping:new":
            return _back(request, f"/journal/csv/{bid}/mapping")
        errors = await run_in_threadpool(svc.set_profile, bid, pid)
        if errors:
            return _batch_page(request, bid, errors=errors)
        return _back(request, f"/journal/csv/{bid}")

    @router.post("/journal/csv/{bid}/symbols")
    async def symbols(request: Request, bid: int) -> Response:
        ctx = get_ctx(request)
        svc = csv_service(ctx)
        js = journal_service(ctx)
        f = await _form(request)
        errors: list[str] = []
        known = js.known_assets()
        created: set[str] = set()  # in diesem Formular angelegt – für spätere Zeilen „zuordnen“ verfügbar
        sources: list[tuple[str, str, str]] = []
        n = 0
        # neue Assets zuerst anlegen: Zeilen weiter oben dürfen auf ein weiter unten angelegtes Asset verweisen
        keys = sorted((k for k in f if k.startswith("sym_")), key=lambda k: str(f.get(f"act_{k[4:]}")) != "new")
        for key in keys:
            symbol = str(f.get(key) or "").strip().upper()
            i = key[4:]
            action = str(f.get(f"act_{i}") or "")
            if not symbol or not action:
                continue
            if action == "map":
                aid = str(f.get(f"asset_{i}") or "").strip()
                if aid not in known and aid not in created:
                    match = [a for a in known.values() if a.name.lower() == aid.lower()]
                    aid = match[0].asset_id if len(match) == 1 else aid
                if aid not in known and aid not in created:
                    errors.append(f"{symbol}: Asset „{aid}“ nicht gefunden.")
                    continue
                svc.set_symbol(symbol, aid)
                src = str(f.get(f"src_{i}") or "").strip()
                if src and aid in known:
                    sources.append((symbol, aid, src))
            elif action == "ignore":
                svc.set_symbol(symbol, None)
            elif action == "new":
                base = symbol.split("@", 1)[0].split(";", 1)[0]
                cls = str(f.get(f"class_{i}") or "crypto")
                qid = str(f.get(f"qid_{i}") or "").strip()
                qs = {"crypto": "coingecko", "security": "yahoo"}.get(cls, "none") if qid else "none"
                res = await run_in_threadpool(js.save_asset, {
                    "asset_id": str(f.get(f"id_{i}") or base).strip(), "name": str(f.get(f"name_{i}") or base).strip(),
                    "asset_class": cls, "quote_source": qs, "quote_id": qid, "category": "",
                    "note": f"angelegt beim CSV-Import (Symbol {symbol})"})
                if res.errors:
                    errors.extend(f"{symbol}: {e}" for e in res.errors)
                    continue
                svc.set_symbol(symbol, res.asset_id)
                created.add(res.asset_id)
            else:
                continue
            n += 1
        if sources:
            src_svc = source_service(ctx)
            done = 0
            for symbol, aid, coin in sources:
                err = await run_in_threadpool(src_svc.accept, aid, coin,
                                              f"Contract laut CoinGecko-Katalog (Symbol {symbol})", False)
                if err:
                    errors.append(f"{symbol}: Kursquelle für {aid} nicht übernommen – {err}")
                else:
                    done += 1
            if done:
                src_svc.changed()
        await run_in_threadpool(svc.evaluate, bid)
        if errors:
            return _batch_page(request, bid, errors=errors)
        return _back(request, _redir(bid, f, "symbols" if n else ""))

    @router.post("/journal/csv/{bid}/account-adopt")
    async def account_adopt(request: Request, bid: int) -> Response:
        """Konto laut Abgleich übernehmen: bei Datenquellen deren Konto (offene Stapel folgen), sonst des Imports."""
        ctx = get_ctx(request)
        svc = csv_service(ctx)
        b = svc.batch(bid)
        if b is None:
            raise HTTPException(404)
        f = await _form(request)
        account = str(f.get("account") or "").strip()
        if b["kind"] == "sync" and b["datasource_id"]:
            from app.datasources.service import datasource_service

            errs = await run_in_threadpool(datasource_service(ctx).switch_account, int(b["datasource_id"]), account)
        else:
            errs = await run_in_threadpool(svc.set_options, bid, {"account": account})
        if errs:
            return _batch_page(request, bid, errors=errs)
        return _back(request, _redir(bid, f, "account"))

    @router.post("/journal/csv/{bid}/account-undo")
    async def account_undo(request: Request, bid: int) -> Response:
        ctx = get_ctx(request)
        b = csv_service(ctx).batch(bid)
        if b is None or not b["datasource_id"]:
            raise HTTPException(404)
        from app.datasources.service import datasource_service

        f = await _form(request)
        errs = await run_in_threadpool(datasource_service(ctx).undo_account_switch, int(b["datasource_id"]))
        if errs:
            return _batch_page(request, bid, errors=errs)
        return _back(request, _redir(bid, f, "account"))

    @router.get("/journal/csv/{bid}/catalog", response_class=HTMLResponse)
    def catalog_status(request: Request, bid: int) -> Response:
        """Fortschritt beim Laden des CoinGecko-Katalogs; danach Seite mit Vorschlägen neu laden."""
        ctx = get_ctx(request)
        state = catalog_state(ctx)
        if state["loading"]:
            return HTMLResponse(_catalog_status_html(bid))
        if state["catalog"] is not None:
            return Response(status_code=204, headers={"HX-Redirect": f"/journal/csv/{bid}#assets"})
        return HTMLResponse(f'<span class="badge warn">CoinGecko-Katalog nicht geladen: '
                            f'{html.escape(state["error"] or "unbekannter Fehler")}</span>')

    @router.post("/journal/csv/{bid}/accounts")
    async def accounts(request: Request, bid: int) -> Response:
        svc = csv_service(get_ctx(request))
        f = await _form(request)
        for key in [k for k in f if k.startswith("accname_")]:
            name = str(f.get(key) or "")
            target = str(f.get("acc_" + key[8:]) or "").strip()
            if name:
                svc.set_account(name, target if target != name else "")
        await run_in_threadpool(svc.evaluate, bid)
        return _back(request, _redir(bid, f, "accounts"))

    @router.post("/journal/csv/{bid}/ignore")
    async def ignore(request: Request, bid: int) -> Response:
        """Vorgang einer Datenquelle dauerhaft ignorieren bzw. freigeben (Entscheidung je Anbieter-Ereignis)."""
        svc = csv_service(get_ctx(request))
        if svc.batch(bid) is None:
            raise HTTPException(404)
        f = await _form(request)
        key = str(f.get("ignore") or f.get("release") or "")
        await run_in_threadpool(svc.set_rows, bid, f)  # übrige Eingaben der Seite nicht verlieren
        if not await run_in_threadpool(svc.set_ignored, bid, key, bool(f.get("ignore")),
                                       str(f.get("reason") or "vom Nutzer entschieden")):
            return _batch_page(request, bid, errors=["Vorgang nicht gefunden."])
        return _back(request, _redir(bid, f, "rows"))

    @router.post("/journal/csv/{bid}/rows")
    async def rows(request: Request, bid: int) -> Response:
        svc = csv_service(get_ctx(request))
        f = await _form(request)
        bulk = str(f.get("bulk") or "")
        if bulk:
            st, _, dec = bulk.partition(":")
            await run_in_threadpool(svc.set_all, bid, st, dec if dec in ("include", "skip") else None)
        else:
            await run_in_threadpool(svc.set_rows, bid, f)
        return _back(request, _redir(bid, f, "rows"))

    @router.post("/journal/csv/{bid}/commit")
    async def commit(request: Request, bid: int) -> Response:
        svc = csv_service(get_ctx(request))
        res = await run_in_threadpool(svc.commit, bid)
        if res.get("errors"):
            return _batch_page(request, bid, errors=res["errors"])
        q = urlencode({"msg": "commit", "n": res["created"], "t": res["transfers"]})
        return _back(request, f"/journal/csv/{bid}?{q}")

    @router.post("/journal/csv/{bid}/revert")
    async def revert(request: Request, bid: int) -> Response:
        svc = csv_service(get_ctx(request))
        res = await run_in_threadpool(svc.revert, bid)
        if res.get("errors"):
            return _batch_page(request, bid, errors=res["errors"])
        return _back(request, f"/journal/csv/{bid}?msg=reverted")

    @router.post("/journal/csv/{bid}/reopen")
    async def reopen(request: Request, bid: int) -> Response:
        svc = csv_service(get_ctx(request))
        if not await run_in_threadpool(svc.reopen, bid):
            raise HTTPException(404)
        return _back(request, f"/journal/csv/{bid}")

    @router.post("/journal/csv/{bid}/discard")
    async def discard(request: Request, bid: int) -> Response:
        svc = csv_service(get_ctx(request))
        if not await run_in_threadpool(svc.discard, bid):
            return _batch_page(request, bid, errors=["Übernommene Stapel zuerst rückgängig machen."])
        return _back(request, "/journal/csv")

    @router.post("/journal/csv/{bid}/prices", response_class=HTMLResponse)
    async def prices(request: Request, bid: int) -> Response:
        ctx = get_ctx(request)
        svc = csv_service(ctx)
        if svc.batch(bid) is None:
            raise HTTPException(404)
        if ctx.scheduler is not None:
            ctx.scheduler.trigger("csv_prices", 0.5, batch_id=bid)
            return HTMLResponse(_price_status_html(bid, running=True))
        await run_in_threadpool(svc.load_prices, bid)
        return _back(request, f"/journal/csv/{bid}?msg=prices")

    @router.get("/journal/csv/{bid}/prices/status", response_class=HTMLResponse)
    def price_status(request: Request, bid: int) -> Response:
        ctx = get_ctx(request)
        job = ctx.db.q1("SELECT running, progress_json FROM job_status WHERE job='csv_prices'")
        if job is not None and job["running"]:
            p = json.loads(job["progress_json"] or "{}")
            return HTMLResponse(_price_status_html(bid, running=True, done=p.get("done", 0), total=p.get("total", 0)))
        return Response(status_code=204, headers={"HX-Redirect": f"/journal/csv/{bid}?msg=prices"})

    @router.get("/journal/csv/{bid}/mapping", response_class=HTMLResponse)
    def mapping_form(request: Request, bid: int) -> HTMLResponse:
        return _mapping_page(request, bid)

    @router.post("/journal/csv/{bid}/mapping")
    async def mapping_save(request: Request, bid: int) -> Response:
        svc = csv_service(get_ctx(request))
        f = await _form(request)
        b = svc.batch(bid)
        if b is None:
            raise HTTPException(404)
        if b["status"] not in ("mapping", "preview"):
            return _mapping_page(request, bid, ["Stapel bereits übernommen – Zuordnung nicht mehr änderbar."])
        header = read_table(svc.raw(b)).header
        cols = {k: str(f.get(f"col_{k}") or "") for k in MAPPING_FIELDS}
        cols = {k: v for k, v in cols.items() if v and v in header}
        type_map: dict[str, str] = {}
        for key in [k for k in f if k.startswith("lbl_")]:
            label = str(f.get(key) or "").strip()
            kind = str(f.get("map_" + key[4:]) or "").strip()
            if label and kind in MAPPING_KINDS:
                type_map[label] = kind
        spec = {"name": str(f.get("name") or "").strip()[:60] or b["filename"][:40], "columns": cols,
                "tz": str(f.get("tz") or ""), "decimal": str(f.get("decimal") or ""),
                "value_ccy_fixed": str(f.get("value_ccy_fixed") or "").strip().upper()[:10],
                "type_map": type_map, "header": header}
        errors = []
        if "date" not in cols:
            errors.append("Spalte für Datum/Zeit wählen.")
        if not ({"in_qty", "in_sym"} <= cols.keys() or {"out_qty", "out_sym"} <= cols.keys()
                or {"qty"} <= cols.keys()):
            errors.append("Mindestens Zugang (Menge + Währung), Abgang (Menge + Währung) oder „Menge mit Vorzeichen“ "
                          "zuordnen.")
        if errors or f.get("preview") == "1":
            return _mapping_page(request, bid, errors, spec)
        mid = b["profile"][8:] if b["profile"].startswith("mapping:") else ""
        new_id = await run_in_threadpool(svc.save_mapping, spec["name"], spec, int(mid) if mid.isdigit() else None)
        errs = await run_in_threadpool(svc.set_profile, bid, f"mapping:{new_id}")
        if errs:
            return _mapping_page(request, bid, errs, spec)
        return _back(request, f"/journal/csv/{bid}")

    @router.post("/journal/csv/symbol/delete")
    async def symbol_delete(request: Request) -> Response:
        svc = csv_service(get_ctx(request))
        f = await _form(request)
        svc.delete_symbol(str(f.get("symbol") or ""))
        return _back(request, "/journal/csv#zuordnungen")

    @router.post("/journal/csv/account/delete")
    async def account_delete(request: Request) -> Response:
        svc = csv_service(get_ctx(request))
        f = await _form(request)
        svc.set_account(str(f.get("name") or ""), "")
        return _back(request, "/journal/csv#zuordnungen")

    @router.post("/journal/csv/mapping/{mid}/delete")
    async def mapping_delete(request: Request, mid: int) -> Response:
        svc = csv_service(get_ctx(request))
        svc.delete_mapping(mid)
        return _back(request, "/journal/csv#formate")

    return router


def _catalog_status_html(bid: int) -> str:
    return (f'<span id="catalog-status" hx-get="/journal/csv/{bid}/catalog" hx-trigger="every 3s" '
            f'hx-swap="outerHTML"><span class="badge info">CoinGecko-Katalog wird geladen …</span></span>')


def _price_status_html(bid: int, running: bool, done: int = 0, total: int = 0) -> str:
    pct = int(done / total * 100) if total else 5
    return (f'<div hx-get="/journal/csv/{bid}/prices/status" hx-trigger="every 3s" hx-swap="outerHTML">'
            f'<span class="badge info">Kurse werden geladen … {done}/{total or "?"}</span>'
            f'<div class="progress" style="margin-top:6px"><div style="width:{pct}%"></div></div></div>')


@extra_jobs
def _jobs(s: Scheduler) -> None:
    s.register("csv_prices", lambda ctx, batch_id=0: csv_service(ctx).load_prices(int(batch_id)), None)


register_router(make_router)
