"""Web-Oberfläche „PDF & Screenshot“ (unter Buchungen): Belege hochladen, Auswertung verfolgen, prüfen, korrigieren.

Die Seiten ändern **keine Buchungen**. Übernehmen, Verknüpfen, Auslassen, Sammelaktionen und Rückgängig laufen über
den bestehenden Prüf-Stapel (``/journal/csv/{id}``); das Ergänzen einer vorhandenen Buchung über die Korrektur-Engine
der Diagnose (Vorschau mit berechneten Auswirkungen, Übernehmen mit Prüfsumme, Rückgängig unter Datenqualität).
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.documentimport.extract import MAX_FILE
from app.documentimport.profiles import DOC_LABEL, FIELD_LABEL, KIND_LABEL, PROVIDER_LABEL
from app.documentimport.service import EDITABLE, JOB, MAX_FILES, document_service
from app.web.app import register_router
from app.web.deps import get_ctx, render

log = logging.getLogger(__name__)

MAX_STACK = 100 * 1024 * 1024  # Summe je Stapel (der Body wird nicht vollständig im Speicher gepuffert)
STATUS = {"belegt": ("A", "belegt", "good"), "rekonstruiert": ("B", "rekonstruiert", "info"),
          "geschaetzt": ("C", "geschätzt", "warn"), "ungeloest": ("–", "ungelöst", "crit")}
ORIGIN = {"document": "Beleg", "batch": "anderer Beleg im Stapel", "portfolio": "Portfolia-Daten",
          "provider": "Datenquelle", "public": "öffentliche Quelle", "user": "Korrektur", None: "–"}
DOC_STATUS = {"queued": ("wartet", "info"), "analysed": ("ausgewertet", "info"), "staged": ("im Prüf-Stapel", "good"),
              "failed": ("fehlgeschlagen", "crit")}
STACK_STATUS = {"queued": ("wartet", "info"), "running": ("läuft", "info"), "done": ("fertig", "good"),
                "cancelled": ("abgebrochen", "warn"), "failed": ("fehlgeschlagen", "crit")}
STAGE = {"document": "1 · Beleg", "batch": "1 · Stapel", "portfolio": "2 · Portfolia", "provider": "3 · Datenquelle",
         "public": "4 · öffentlich", "user": "Korrektur"}
SETTINGS = ("documents.keep_originals", "documents.ocr", "documents.public_lookup")
LANGS = {"deu+eng": "Deutsch + Englisch", "deu": "Deutsch", "eng": "Englisch"}
# Bearbeitbare Felder in der Prüfansicht (Reihenfolge); ``kind`` als Auswahl
EDIT_ORDER = ("kind", "date", "time", "quantity", "symbol", "isin", "price", "gross", "ccy", "fee", "fee_ccy",
              "value_eur", "network_fee", "txhash", "ext_id")
KIND_CHOICES = ("buy", "sell", "dividend", "deposit", "withdrawal")


def _back(request: Request, target: str) -> Response:
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": target})
    return Response(status_code=303, headers={"Location": target})


def _url(path: str, msg: str = "", err: str = "", anchor: str = "") -> str:
    q = {k: v for k, v in (("msg", msg), ("err", err)) if v}
    return path + (f"?{urlencode(q)}" if q else "") + (f"#{anchor}" if anchor else "")


def _common() -> dict[str, Any]:
    from app.csvimport.service import STATUS_LABEL

    return {"row_label": STATUS_LABEL, "ev_status": STATUS,
             "origin_label": ORIGIN, "doc_status": DOC_STATUS, "stack_status": STACK_STATUS,
            "doc_label": DOC_LABEL, "provider_label": PROVIDER_LABEL, "kind_label": KIND_LABEL,
            "field_label": FIELD_LABEL, "stage_label": STAGE}


def _settings(ctx: Any) -> dict[str, Any]:
    g = ctx.settings.get
    return {"keep": bool(g("documents.keep_originals", True)), "ocr": bool(g("documents.ocr", True)),
            "public": bool(g("documents.public_lookup", False)), "language": str(g("documents.language", "deu+eng"))}


def _ocr_available() -> bool:
    import shutil

    return shutil.which("tesseract") is not None


def _index(request: Request, errors: list[str] | None = None, msg: str = "", status_code: int = 200) -> HTMLResponse:
    from app.journal.service import journal_service

    ctx = get_ctx(request)
    svc = document_service(ctx)
    stacks = []
    for s in ctx.db.q("SELECT * FROM document_stack ORDER BY created_at DESC LIMIT 10"):
        summ = json.loads(s["summary_json"] or "{}")
        n = ctx.db.scalar("SELECT COUNT(*) FROM document WHERE stack_id=?", (s["id"],), default=0)
        stacks.append({"s": s, "summary": summ, "n": n})
    docs = [{"d": d, "result": json.loads(d["result_json"] or "{}")} for d in svc.list(200)]
    return render(request, "documents.html", status_code=status_code, active="journal", errors=errors or [], msg=msg,
                  stacks=stacks, docs=docs, cfg=_settings(ctx), langs=LANGS, ocr_available=_ocr_available(),
                  accounts=journal_service(ctx).known_accounts(), max_files=MAX_FILES,
                  max_file_mb=MAX_FILE // 1024 // 1024, max_stack_mb=MAX_STACK // 1024 // 1024,
                  running=svc.running(), **_common())


def _rows_of(ctx: Any, doc: Any, txs: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Zeile des Prüf-Stapels je Belegvorgang (Status, Ergebnis des Abgleichs)."""
    from app.csvimport.assess import BASIS_LABEL, CAT_BADGE, CAT_LABEL
    from app.csvimport.service import STATUS_BADGE, STATUS_LABEL

    out: dict[int, dict[str, Any]] = {}
    if not doc["batch_id"]:
        return out
    for t in txs:
        ek = t.get("event_key")
        if not ek:
            continue
        r = ctx.db.q1("SELECT id, status, decision, tx_id, messages FROM csv_row WHERE batch_id=? AND event_key=? "
                      "ORDER BY event_line LIMIT 1", (doc["batch_id"], ek))
        if r is None:
            continue
        m = (json.loads(r["messages"] or "{}") or {}).get("match") or {}
        out[int(t.get("n") or 0)] = {
            "id": r["id"], "status": r["status"], "status_label": STATUS_LABEL.get(r["status"], r["status"]),
            "status_badge": STATUS_BADGE.get(r["status"], ""), "tx_id": r["tx_id"], "cat": m.get("cat"),
            "cat_label": CAT_LABEL.get(m.get("cat"), "") if m.get("cat") else "", "target": m.get("target"),
            "cat_badge": CAT_BADGE.get(m.get("cat"), "") if m.get("cat") else "", "conf": m.get("conf"),
            "basis": BASIS_LABEL.get(m.get("basis"), "") if m.get("basis") else "", "why": m.get("why") or "",
            "ok": [x for x in m.get("ok") or [] if isinstance(x, str)][:6],
            "diff": [x.get("t") for x in m.get("diff") or [] if isinstance(x, dict)][:6],
            "add": [x.get("t") for x in m.get("add") or [] if isinstance(x, dict)][:6]}
    return out


def solutions(t: dict[str, Any], row: dict[str, Any] | None, amend: Any, batch_id: int | None) -> list[dict[str, Any]]:
    """Bevorzugte Lösung und bis zu drei Alternativen je Belegvorgang (nur Wege über bestehende Funktionen)."""
    stack = f"/journal/csv/{batch_id}" if batch_id else None
    fields = t.get("fields") or {}
    missing = [f for f, v in fields.items() if v.get("status") == "ungeloest" and f in EDITABLE]
    out: list[dict[str, Any]] = []
    closed = row is not None and row["status"] in ("committed", "merged", "linked", "known")
    if amend is not None and amend.target is not None and amend.changes and not amend.blocked:
        out.append({"key": "amend", "label": "Bestehende Buchung ergänzen",
                    "text": f"{amend.target.tx_id}: " + ", ".join(c.label for c in amend.changes)
                            + " – Vorschau mit Auswirkungen, danach Übernehmen bzw. Rückgängig.", "href": None})
    if row is not None and not closed:
        cat = row.get("cat")
        if row["status"] == "invalid" or row["status"] == "unclear":
            out.append({"key": "complete", "label": "Fehlende Angaben ergänzen",
                        "text": "Felder unten korrigieren bzw. ergänzen und neu bewerten; danach im Prüf-Stapel "
                                "übernehmen.", "href": f"#korrektur{t.get('n')}"})
        elif cat in ("dublette", "ergaenzung"):
            out.append({"key": "link", "label": "Mit vorhandener Buchung verknüpfen",
                        "text": "Der Abgleich hat die Buchung gefunden – Beleg verknüpfen statt doppelt buchen.",
                        "href": stack})
        elif cat == "neu":
            out.append({"key": "include", "label": "Im Prüf-Stapel übernehmen",
                        "text": "Neuer Vorgang – nach Prüfung übernehmen (einzeln oder per Sammelaktion).",
                        "href": stack})
        else:
            out.append({"key": "review", "label": "Im Prüf-Stapel einzeln prüfen",
                        "text": "Widerspruch bzw. komplexer Fall – Gegenüberstellung im Prüf-Stapel ansehen.",
                        "href": stack})
        if missing and not any(o["key"] == "complete" for o in out):
            out.append({"key": "complete", "label": "Angaben korrigieren",
                        "text": "Ungelöst: " + ", ".join(FIELD_LABEL.get(f, f) for f in missing[:6]),
                        "href": f"#korrektur{t.get('n')}"})
        out.append({"key": "skip", "label": "Auslassen", "text": "Vorgang im Prüf-Stapel ignorieren (rückgängig "
                                                                 "machbar).", "href": stack})
    elif row is not None and closed:
        ref = row.get("tx_id") or row.get("target")
        if row["status"] == "known":
            out.append({"key": "keep", "label": "Unverändert lassen",
                        "text": "Der Vorgang ist bereits gebucht" + (f" ({ref})" if ref else "")
                                + " – der Beleg dient nur als Nachweis.", "href": stack})
        else:
            out.append({"key": "done", "label": "Erledigt",
                        "text": f"Im Prüf-Stapel: {row['status_label']}" + (f" ({ref})" if ref else ""),
                        "href": stack})
    if not out:
        out.append({"key": "reupload", "label": "Neu bewerten",
                    "text": "Kein Prüf-Stapel zugeordnet – neu bewerten legt einen an (keine Buchung).",
                    "href": None})
    return out[:4]


def _detail(request: Request, doc_id: int, errors: list[str] | None = None, msg: str = "",
            status_code: int = 200) -> HTMLResponse:
    from app.documentimport import amend as AM

    ctx = get_ctx(request)
    svc = document_service(ctx)
    d = svc.get(doc_id)
    if d is None:
        raise HTTPException(404)
    res = json.loads(d["result_json"] or "{}")
    txs = res.get("txs") or []
    rows = _rows_of(ctx, d, txs)
    ov = json.loads(d["overrides_json"] or "{}")
    items = []
    for t in txs:
        n = int(t.get("n") or 0)
        try:
            p = AM.propose(ctx, doc_id, n)
        except Exception as e:  # Anzeige darf nicht scheitern – Ergänzung dann nicht angeboten
            log.warning("Ergänzungsvorschlag für Beleg %s nicht berechenbar: %s", doc_id, type(e).__name__)
            p = None
        row = rows.get(n)
        items.append({"t": t, "n": n, "row": row, "amend": p, "solutions": solutions(t, row, p, d["batch_id"]),
                      "overrides": ov.get(str(n)) or {}})
    prev = svc.get(int(d["supersedes"])) if d["supersedes"] else None
    later = ctx.db.q("SELECT id, filename FROM document WHERE supersedes=?", (doc_id,))
    return render(request, "document_detail.html", status_code=status_code, active="journal", d=d, res=res,
                  items=items, errors=errors or [], msg=msg, prev=prev, later=later, edit_order=EDIT_ORDER,
                  kind_choices=KIND_CHOICES, has_preview=bool(d["stored"]), **_common())


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/journal/documents", response_class=HTMLResponse)
    def index(request: Request, msg: str = "", err: str = "") -> HTMLResponse:
        return _index(request, [err] if err else None, msg)

    @router.post("/journal/documents/upload")
    async def upload(request: Request) -> Response:
        """Mehrere Dateien (XHR mit Fortschritt oder klassisches Formular). Antwort: JSON (XHR) bzw. Weiterleitung."""
        ctx = get_ctx(request)
        xhr = request.headers.get("x-requested-with") == "XMLHttpRequest"
        form = await request.form(max_files=MAX_FILES + 1, max_fields=10)
        uploads = [f for f in form.getlist("files") if isinstance(f, UploadFile) and f.filename]
        errors: list[str] = []
        files: list[tuple[str | None, bytes]] = []
        total = 0
        if len(uploads) > MAX_FILES:
            errors.append(f"Höchstens {MAX_FILES} Dateien je Stapel.")
        else:
            for f in uploads:
                data = await f.read(MAX_FILE + 1)
                await f.close()
                total += len(data)
                if total > MAX_STACK:
                    errors.append(f"Stapel zu groß (zusammen höchstens {MAX_STACK // 1024 // 1024} MiB).")
                    files = []
                    break
                files.append((f.filename, data))
        for f in form.getlist("files"):
            if isinstance(f, UploadFile):
                await f.close()
        account = str(form.get("account") or "").strip()[:80]
        public = form.get("public") == "1"
        reanalyze = form.get("reanalyze") == "1"
        svc = document_service(ctx)
        acc = None
        if not errors:
            acc = await run_in_threadpool(svc.accept, files, account=account, public=public, reanalyze=reanalyze)
            errors += acc.errors
            del files
        target = None
        if acc is not None and acc.stack_id:
            started = await run_in_threadpool(svc.start, acc.stack_id)
            if started.get("error"):
                errors.append(started["error"])
            target = f"/journal/documents/stack/{acc.stack_id}"
        known = acc.known if acc is not None else []
        msg = ""
        if known:
            msg = "Bereits verarbeitet (gleicher Inhalt, nicht erneut ausgewertet): " + ", ".join(
                k["filename"] for k in known[:10])
        if target is None:
            if not errors and not known:
                errors.append("Keine Datei ausgewählt.")
            target = _url("/journal/documents", msg=msg, err="; ".join(errors)[:900])
        elif errors or msg:
            target = _url(target, msg=msg, err="; ".join(errors)[:900])
        if xhr:
            return JSONResponse({"redirect": target, "errors": errors, "known": known})
        return Response(status_code=303, headers={"Location": target})

    @router.post("/journal/documents/settings")
    async def save_settings(request: Request) -> Response:
        ctx = get_ctx(request)
        f = await request.form()
        for key in SETTINGS:
            ctx.settings.set(key, f.get(key.split(".", 1)[1]) == "1")
        lang = str(f.get("language") or "deu+eng")
        ctx.settings.set("documents.language", lang if lang in LANGS else "deu+eng")
        return _back(request, _url("/journal/documents", msg="Einstellungen gespeichert.", anchor="einstellungen"))

    @router.get("/journal/documents/stack/{sid}", response_class=HTMLResponse)
    def stack_page(request: Request, sid: str, msg: str = "", err: str = "") -> HTMLResponse:
        ctx = get_ctx(request)
        st = document_service(ctx).stack(sid)
        if st is None:
            raise HTTPException(404)
        return render(request, "document_stack.html", active="journal", **_stack_ctx(ctx, st), msg=msg,
                      errors=[err] if err else [], **_common())

    @router.get("/journal/documents/stack/{sid}/progress", response_class=HTMLResponse)
    def stack_progress(request: Request, sid: str) -> HTMLResponse:
        ctx = get_ctx(request)
        st = document_service(ctx).stack(sid)
        if st is None:
            raise HTTPException(404)
        return render(request, "partials/document_stack_body.html", **_stack_ctx(ctx, st), **_common())

    @router.post("/journal/documents/stack/{sid}/cancel")
    def stack_cancel(request: Request, sid: str) -> Response:
        msg = document_service(get_ctx(request)).cancel(sid)
        return _back(request, _url(f"/journal/documents/stack/{sid}", msg=msg))

    @router.post("/journal/documents/bulk")
    async def bulk(request: Request) -> Response:
        ctx = get_ctx(request)
        f = await request.form(max_fields=250)
        action = str(f.get("action") or "")
        ids = [int(x) for x in f.getlist("ids") if str(x).isdigit()][:200]
        if not ids:
            return _back(request, _url("/journal/documents", err="Keine Belege ausgewählt.", anchor="belege"))
        svc = document_service(ctx)
        done, errs = 0, []
        for i in ids:
            if action == "delete_original":
                done += 1 if await run_in_threadpool(svc.delete_original, i) else 0
            elif action == "reevaluate":
                r = await run_in_threadpool(svc.reevaluate, i)
                if r.get("error"):
                    errs.append(f"#{i}: {r['error']}")
                else:
                    done += 1
            else:
                return _back(request, _url("/journal/documents", err="Unbekannte Aktion.", anchor="belege"))
        label = "Originale gelöscht" if action == "delete_original" else "neu bewertet"
        return _back(request, _url("/journal/documents", msg=f"{done} Beleg(e) {label}.",
                                   err="; ".join(errs)[:900], anchor="belege"))

    @router.get("/journal/documents/{doc_id}", response_class=HTMLResponse)
    def detail(request: Request, doc_id: int, msg: str = "", err: str = "") -> HTMLResponse:
        return _detail(request, doc_id, [err] if err else None, msg)

    @router.get("/journal/documents/{doc_id}/preview")
    def preview(request: Request, doc_id: int, page: int = 1, box: str = "") -> Response:
        svc = document_service(get_ctx(request))
        b = None
        if box:
            try:
                parts = tuple(float(x) for x in box.split(","))
            except ValueError:
                raise HTTPException(400) from None
            if len(parts) != 4 or not all(0.0 <= x <= 1.0 for x in parts):
                raise HTTPException(400)
            b = parts
        try:
            png = svc.preview(doc_id, max(1, min(page, 50)), b)  # type: ignore[arg-type]
        except Exception as e:
            log.warning("Belegvorschau %s nicht erzeugbar: %s", doc_id, type(e).__name__)
            png = None
        if png is None:
            raise HTTPException(404)
        return Response(png, media_type="image/png", headers={"Cache-Control": "private, no-store",
                                                              "X-Content-Type-Options": "nosniff"})

    @router.get("/journal/documents/{doc_id}/original")
    def original(request: Request, doc_id: int) -> Response:
        from app.documentimport.extract import FORMATS
        from app.documentimport.service import EXT

        svc = document_service(get_ctx(request))
        d = svc.get(doc_id)
        data = svc.original(doc_id) if d is not None else None
        if d is None or data is None:
            raise HTTPException(404)
        name = f"beleg-{doc_id}.{EXT.get(d['file_type'], 'bin')}"
        return Response(data, media_type=FORMATS.get(d["file_type"], "application/octet-stream"),
                        headers={"Content-Disposition": f'attachment; filename="{name}"',
                                 "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
                                 "Content-Security-Policy": "sandbox"})

    @router.post("/journal/documents/{doc_id}/correct")
    async def correct(request: Request, doc_id: int) -> Response:
        f = await request.form(max_fields=60)
        try:
            n = int(str(f.get("n") or "1"))
        except ValueError:
            n = 1
        values = {k: str(f.get(k) or "") for k in EDITABLE if k in f}
        r = await run_in_threadpool(document_service(get_ctx(request)).correct, doc_id, n, values)
        return _back(request, _url(f"/journal/documents/{doc_id}", msg="" if r.get("error") else
                                   "Korrektur gespeichert und neu bewertet.", err=r.get("error") or "",
                                   anchor=f"tx{n}"))

    @router.post("/journal/documents/{doc_id}/reevaluate")
    async def reevaluate(request: Request, doc_id: int) -> Response:
        r = await run_in_threadpool(document_service(get_ctx(request)).reevaluate, doc_id)
        return _back(request, _url(f"/journal/documents/{doc_id}", msg="" if r.get("error") else "Neu bewertet.",
                                   err=r.get("error") or ""))

    @router.post("/journal/documents/{doc_id}/delete-original")
    async def delete_original(request: Request, doc_id: int) -> Response:
        ok = await run_in_threadpool(document_service(get_ctx(request)).delete_original, doc_id)
        if not ok:
            raise HTTPException(404)
        return _back(request, _url(f"/journal/documents/{doc_id}",
                                   msg="Original und Volltext gelöscht – Feldbelege und Prüfzeilen bleiben erhalten."))

    @router.get("/journal/documents/{doc_id}/amend", response_class=HTMLResponse)
    def amend_preview(request: Request, doc_id: int, n: int = 1) -> HTMLResponse:
        q = request.query_params
        return _amend_page(request, doc_id, n, q.getlist("f"), "set" in q)

    @router.post("/journal/documents/{doc_id}/amend")
    async def amend_apply(request: Request, doc_id: int) -> Response:
        from app.documentimport import amend as AM

        ctx = get_ctx(request)
        form = await request.form(max_fields=40)
        try:
            n = int(str(form.get("n") or "1"))
        except ValueError:
            n = 1
        sel = {str(x) for x in form.getlist("f")}
        res = await run_in_threadpool(AM.apply, ctx, doc_id, n, sel, str(form.get("token") or ""))
        if not res.ok:
            return await run_in_threadpool(_amend_page, request, doc_id, n, sorted(sel), True, res.errors, 409)
        return _back(request, _url(f"/journal/documents/{doc_id}", msg=res.message + " Rückgängig unter "
                                   "Datenqualität → Entscheidungen und Korrekturen.", anchor=f"tx{n}"))

    return router


def _stack_ctx(ctx: Any, st: Any) -> dict[str, Any]:
    from app.progress import view

    docs = ctx.db.q("SELECT id, filename, status, error, doc_type, provider, pages, ocr, batch_id FROM document "
                    "WHERE stack_id=? ORDER BY id", (st["id"],))
    p = None
    row = ctx.db.q1("SELECT progress_json FROM job_status WHERE job=?", (JOB,))
    if row is not None and row["progress_json"]:
        p = json.loads(row["progress_json"])
        if (p.get("stack") or None) not in (None, st["id"]) and st["status"] != "running":
            p = None
    batches = []
    summ = json.loads(st["summary_json"] or "{}")
    for bid in summ.get("batches") or []:
        b = ctx.db.q1("SELECT id, status FROM csv_batch WHERE id=?", (bid,))
        if b is not None:
            batches.append(b)
    return {"st": st, "docs": docs, "p": p, "pv": view(p) if p else None, "summary": summ, "batches": batches,
            "live": st["status"] in ("queued", "running")}


def _amend_page(request: Request, doc_id: int, n: int, fields: list[str] | None, given: bool,
                errors: list[str] | None = None, status_code: int = 200) -> HTMLResponse:
    from app.documentimport import amend as AM

    ctx = get_ctx(request)
    d = document_service(ctx).get(doc_id)
    if d is None:
        raise HTTPException(404)
    prop = AM.propose(ctx, doc_id, n)
    sel = set(fields or []) if given else {c.field for c in prop.changes}
    plan, effects, prop = AM.preview(ctx, doc_id, n, sel) if prop.target is not None and not prop.blocked else \
        (None, None, prop)
    from app.diagnosis.model import STATUS_BADGE

    return render(request, "document_amend.html", status_code=status_code, active="journal", d=d, n=n, prop=prop,
                  status_badge=STATUS_BADGE,
                  plan=plan, effects=effects, selected=sel, errors=(errors or []) + (plan.errors if plan else []),
                  **_common())


register_router(make_router)
