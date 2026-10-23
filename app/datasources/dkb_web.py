"""Einstellungen → Datenquellen → DKB: Abruf einrichten, Zugang, Abruf per Knopfdruck, Fortschritt, Ergebnis.

Das Passwort steht nur im Formular des jeweiligen Abrufs; es wird nie gespeichert, nie zurück an den Browser gegeben und
nicht protokolliert.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.datasources import dkb as D
from app.datasources import dkb_run as R
from app.datasources import dkb_zip as Z
from app.datasources import plugins as P
from app.datasources.service import datasource_service
from app.web.app import register_router
from app.web.deps import get_ctx, render


def _url(sid: int, **params: str) -> str:
    return f"/settings/datasources/{sid}/dkb" + ("?" + urlencode(params) if params else "")


def _back(request: Request, target: str) -> Response:
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": target})
    return Response(status_code=303, headers={"Location": target})


def make_router() -> APIRouter:
    router = APIRouter()

    def ds_of(request: Request, sid: int) -> Any:
        ds = datasource_service(get_ctx(request)).get(sid)
        if ds is None or not D.is_dkb(ds):
            raise HTTPException(404)
        return ds

    @router.get("/settings/datasources/{sid}/dkb", response_class=HTMLResponse)
    async def page(request: Request, sid: int, msg: str = "", error: str = "") -> Response:
        ds = ds_of(request, sid)
        ctx = get_ctx(request)

        def collect() -> dict[str, Any]:
            plug = P.status(ctx.config, D.PLUGIN)
            run = D.view_run(ctx.db, sid)
            return {"plug": plug, "inst": P.last(D.PLUGIN), "set": D.settings(ctx.db, sid),
                    "folder": D.folder_state(ctx, sid, ds.name), "run": run, "runs": D.runs(ctx.db, sid),
                    "docs": D.documents(ctx.db, sid), "zips": R.runs(ctx.db, sid, 5)}

        data = await run_in_threadpool(collect)
        busy = bool(data["inst"].get("running")) or bool(data["run"] and data["run"]["active"]) or data["plug"]["busy"]
        return render(request, "datasource_dkb.html", active="settings", ds=ds, msg=msg, error=error, busy=busy,
                      status_label=D.STATUS_LABEL, **data)

    @router.post("/settings/datasources/{sid}/dkb/setup")
    async def setup(request: Request, sid: int) -> Response:
        ds_of(request, sid)
        ok = await run_in_threadpool(P.start_install, get_ctx(request).config, D.PLUGIN)
        return _back(request, _url(sid, **({"msg": "Einrichtung gestartet – Fortschritt unten."} if ok else
                                           {"error": "Es läuft bereits eine Einrichtung."})))

    @router.post("/settings/datasources/{sid}/dkb/remove")
    async def remove(request: Request, sid: int) -> Response:
        ds_of(request, sid)
        if D.active(sid):
            return _back(request, _url(sid, error="Während eines Abrufs nicht möglich."))
        ok = await run_in_threadpool(P.remove, get_ctx(request).config, D.PLUGIN)
        return _back(request, _url(sid, **({"msg": "DKB-Abruf entfernt (abgelegte Dokumente bleiben)."} if ok else
                                           {"error": "Es läuft gerade eine Einrichtung."})))

    @router.post("/settings/datasources/{sid}/dkb/settings")
    async def save(request: Request, sid: int) -> Response:
        ds_of(request, sid)
        f = await request.form()
        try:
            mfa = int(str(f.get("mfa_device") or 0))
        except ValueError:
            mfa = -1
        errors = await run_in_threadpool(
            D.save_settings, get_ctx(request).db, sid, username=str(f.get("username") or ""),
            folder=str(f.get("folder") or ""), mfa_device=mfa, evaluate=str(f.get("evaluate") or "") == "1")
        if errors:
            return _back(request, _url(sid, error=" ".join(errors)))
        return _back(request, _url(sid, msg="Einstellungen gespeichert."))

    @router.post("/settings/datasources/{sid}/dkb/run")
    async def run(request: Request, sid: int) -> Response:
        ds_of(request, sid)
        f = await request.form()
        try:
            mfa = int(str(f.get("mfa_device") or 0))
        except ValueError:
            mfa = 0
        res = await run_in_threadpool(D.start, get_ctx(request), sid, str(f.get("username") or ""),
                                      str(f.get("password") or ""), mfa)
        if res.get("errors"):
            return _back(request, _url(sid, error=" ".join(res["errors"])))
        return _back(request, _url(sid, msg="Abruf gestartet – bitte die Anmeldung in der DKB-App freigeben."))

    @router.post("/settings/datasources/{sid}/dkb/evaluate")
    async def evaluate(request: Request, sid: int) -> Response:
        ds_of(request, sid)
        res = await run_in_threadpool(D.start_evaluate, get_ctx(request), sid)
        if res.get("errors"):
            return _back(request, _url(sid, error=" ".join(res["errors"])))
        return _back(request, _url(sid, msg="Auswertung gestartet."))

    @router.post("/settings/datasources/{sid}/dkb/cancel")
    async def cancel(request: Request, sid: int) -> Response:
        ds_of(request, sid)
        ok = D.cancel(sid)
        text = {"msg": "Abbruch angefordert."} if ok else {"error": "Es läuft kein Abruf."}
        return _back(request, _url(sid, **text))

    def zip_url(sid: int, rid: int, **params: str) -> str:
        return f"/settings/datasources/{sid}/dkb/zip/{rid}" + ("?" + urlencode(params) if params else "")

    @router.post("/settings/datasources/{sid}/dkb/zip")
    async def zip_upload(request: Request, sid: int) -> Response:
        """ZIP annehmen (XHR mit Fortschritt oder klassisches Formular); Antwort JSON bzw. Weiterleitung."""
        ds_of(request, sid)
        ctx = get_ctx(request)
        xhr = request.headers.get("x-requested-with") == "XMLHttpRequest"

        def fail(text: str) -> Response:
            if xhr:
                return JSONResponse({"error": text}, status_code=400)
            return _back(request, _url(sid, error=text))

        form = await request.form(max_files=2, max_fields=6)
        f = form.get("file")
        if not isinstance(f, UploadFile) or not f.filename:
            return fail("Bitte eine ZIP-Datei auswählen.")
        try:
            path = await run_in_threadpool(R.save_upload, ctx, f.file)
        except Z.ZipRejected as e:
            return fail(str(e))
        finally:
            await f.close()
        res = await run_in_threadpool(R.start, ctx, sid, path, Z.display_name(f.filename))
        if res.get("errors"):
            return fail(" ".join(res["errors"]))
        target = zip_url(sid, res["run_id"])
        return JSONResponse({"redirect": target}) if xhr else _back(request, target)

    @router.get("/settings/datasources/{sid}/dkb/zip/{rid}", response_class=HTMLResponse)
    async def zip_page(request: Request, sid: int, rid: int, msg: str = "", error: str = "") -> Response:
        ds = ds_of(request, sid)
        run = await run_in_threadpool(R.view, get_ctx(request), sid, rid)
        if run is None:
            raise HTTPException(404)
        return render(request, "datasource_dkb_zip.html", active="settings", ds=ds, run=run, msg=msg, error=error,
                      kinds=R.KINDS)

    @router.post("/settings/datasources/{sid}/dkb/zip/{rid}/cancel")
    async def zip_cancel(request: Request, sid: int, rid: int) -> Response:
        ds_of(request, sid)
        ok = D.cancel(sid)
        return _back(request, zip_url(sid, rid, **({"msg": "Abbruch angefordert."} if ok else
                                                   {"error": "Es läuft kein Vorgang."})))

    @router.post("/settings/datasources/{sid}/dkb/zip/{rid}/resume")
    async def zip_resume(request: Request, sid: int, rid: int) -> Response:
        ds_of(request, sid)
        res = await run_in_threadpool(R.resume, get_ctx(request), sid, rid)
        if res.get("errors"):
            return _back(request, zip_url(sid, rid, error=" ".join(res["errors"])))
        return _back(request, zip_url(sid, res["run_id"]))

    @router.post("/settings/datasources/{sid}/dkb/zip/{rid}/discard")
    async def zip_discard(request: Request, sid: int, rid: int) -> Response:
        ds_of(request, sid)
        ok = await run_in_threadpool(R.discard, get_ctx(request), sid, rid)
        return _back(request, zip_url(sid, rid, **({"msg": "Zwischendatei gelöscht."} if ok else
                                                   {"error": "Nichts zu löschen."})))

    def _quelle(f: Any) -> str:
        return "kosten" if str(f.get("quelle") or "") == "kosten" else "abrechnung"

    def _matched_docs(run: dict[str, Any], quelle: str) -> list[tuple[int, int]]:
        """Belege mit Buchung: Abrechnungen (belegte Werte) bzw. Kosteninformationen (geschätzte Gebühr)."""
        part = (run["summary"].get("costs") if quelle == "kosten" else run["summary"].get("trades")) or {}
        return [(int(x["doc"]), int(x.get("n") or 1)) for x in part.get("matched", []) if x.get("doc")]

    def _amend_picks(run: dict[str, Any], f: Any) -> list[tuple[int, int]]:
        matched = set(_matched_docs(run, _quelle(f)))
        wanted = {int(v) for v in f.getlist("d") if str(v).isdigit()}
        return sorted(p for p in matched if p[0] in wanted)

    def _amend_ctx(request: Request, sid: int, rid: int, f: Any, preview: bool) -> dict[str, Any]:
        from app.documentimport import amend as AM

        ctx = get_ctx(request)
        run = R.view(ctx, sid, rid)
        if run is None:
            raise HTTPException(404)
        quelle = _quelle(f)
        matched = _matched_docs(run, quelle)
        items = AM.bulk_candidates(ctx, matched)
        safe = {it.doc_id for it in items if not it.needs_confirm and not it.skipped}
        sel = {int(v) for v in f.getlist("d") if str(v).isdigit()} if "d" in f or preview else set(safe)
        picks = sorted(p for p in matched if p[0] in sel and p[0] in safe)
        plan = eff = None
        if preview and picks:
            plan, eff = AM.bulk_preview(ctx, picks)
        return {"run": run, "items": items, "safe": safe, "selected": {p[0] for p in picks}, "plan": plan,
                "effects": eff, "picks": picks, "unmatched_total": len(matched) - len(items), "quelle": quelle}

    @router.get("/settings/datasources/{sid}/dkb/zip/{rid}/ergaenzen", response_class=HTMLResponse)
    async def zip_amend(request: Request, sid: int, rid: int) -> Response:
        ds = ds_of(request, sid)
        q = request.query_params
        data = await run_in_threadpool(_amend_ctx, request, sid, rid, q, "v" in q)
        from app.diagnosis.model import STATUS_BADGE

        return render(request, "datasource_dkb_zip_amend.html", active="settings", ds=ds, errors=[],
                      status_badge=STATUS_BADGE, **data)

    @router.post("/settings/datasources/{sid}/dkb/zip/{rid}/ergaenzen")
    async def zip_amend_apply(request: Request, sid: int, rid: int) -> Response:
        from app.documentimport import amend as AM

        ds = ds_of(request, sid)
        ctx = get_ctx(request)
        f = await request.form(max_fields=600)
        run = await run_in_threadpool(R.view, ctx, sid, rid)
        if run is None:
            raise HTTPException(404)
        picks = _amend_picks(run, f)
        if not picks:
            return _back(request, zip_url(sid, rid, error="Keine Belege ausgewählt."))
        res = await run_in_threadpool(AM.bulk_apply, ctx, picks, str(f.get("token") or ""))
        if not res.ok:
            data = await run_in_threadpool(_amend_ctx, request, sid, rid, f, True)
            from app.diagnosis.model import STATUS_BADGE

            return render(request, "datasource_dkb_zip_amend.html", status_code=409, active="settings", ds=ds,
                          errors=res.errors, status_badge=STATUS_BADGE, **data)
        return _back(request, zip_url(sid, rid, msg=f"{len(picks)} Buchungen ergänzt. {res.message}"))

    @router.get("/settings/datasources/{sid}/dkb/zip/{rid}/report.csv")
    async def zip_report(request: Request, sid: int, rid: int) -> Response:
        ds_of(request, sid)
        run = await run_in_threadpool(R.view, get_ctx(request), sid, rid)
        if run is None:
            raise HTTPException(404)
        return Response(R.report_csv(run["summary"]), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="dkb-zip-bericht-{rid}.csv"'})

    return router


register_router(make_router)
