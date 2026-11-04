"""Assistent „Buchungen prüfen“: eine Frage je Aufgabe, Antwort speichern, weiter – Rückgängig auf der nächsten Karte.

``GET /quality/assistent`` zeigt die nächste Aufgabe (:mod:`app.diagnosis.guide`): Diagnose-Befunde als Frage in
Alltagssprache mit Antworten, darunter jeweils die Wirkung. Eine andere Seite öffnet sich nur, wenn man die Antwort
„… ansehen“ ausdrücklich wählt (``go:open``) – nach der Aktion dort geht es dank Flow-Modus (:mod:`app.web.flow`)
hier weiter. ``POST …/antwort`` führt die zugeordnete, vorhandene Aktion aus (Korrektur mit
Vorschau-Token bzw. Entscheidung ohne Datenänderung), ``POST …/rueckgaengig`` nimmt die letzte Antwort zurück.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.diagnosis import actions as A
from app.diagnosis import guide as G
from app.diagnosis import reset as RS
from app.diagnosis.web import _common
from app.jobs.scheduler import Scheduler, extra_jobs
from app.web.app import register_router
from app.web.deps import get_ctx, render

log = logging.getLogger(__name__)
FLOW_MAX_AGE = 4 * 3600
COOKIES = ("dq_flow", "dq_cur", "dq_skip", "dq_hints")
BASE = "/quality/assistent"
_SAFE = re.compile(r"[A-Za-z0-9_:.\-]{1,120}")
MARK_TEXT = {"defer": "Für später vorgemerkt – nichts geändert.", "reject": "Gespeichert: beide Buchungen bleiben.",
             "dismiss": "Gespeichert: ist in Ordnung so."}


def _set(resp: Response, cur: str, ref: str, hints: bool) -> None:
    """Cookies des Durchgangs: nur der Bezug ``ref`` auf den serverseitig gespeicherten Durchgang (übersprungene
    Aufgaben), nie die Liste selbst – sie ist unbegrenzt und passt in kein Cookie."""
    kw: dict[str, Any] = {"max_age": FLOW_MAX_AGE, "path": "/", "samesite": "lax", "httponly": True}
    resp.set_cookie("dq_flow", "1", **kw)
    resp.set_cookie("dq_cur", cur, **kw)
    resp.set_cookie("dq_skip", ref, **kw)
    resp.set_cookie("dq_hints", "1" if hints else "", **kw)


def _clear(resp: Response) -> None:
    for c in COOKIES:
        resp.delete_cookie(c, path="/")


def with_kid(href: str, kid: str) -> str:
    """Aufgabenbezug an eine interne Adresse hängen (vor einem Anker): Rückweg und gezieltes Überspringen funktionieren
    dann je Tab und auch ohne Cookie."""
    if not kid or not _SAFE.fullmatch(kid):
        return href
    path, hash_, frag = href.partition("#")
    return f"{path}{'&' if '?' in path else '?'}{urlencode({'kid': kid})}" + (hash_ + frag)


def _go(**q: Any) -> Response:
    qs = urlencode({k: v for k, v in q.items() if v not in (None, "", 0)})
    return RedirectResponse(BASE + (f"?{qs}" if qs else ""), status_code=303)


def param_ns(aid: str) -> str:
    """Feldpräfix der Eingaben einer Antwort (``ap.<antwort>.<name>``): jede Antwort hat ihren eigenen Namensraum."""
    return f"ap.{aid}."


def _params(form: Any, aid: str) -> dict[str, list[str]]:
    """Eingaben ausschließlich der gewählten Antwort – gleichnamige Felder anderer Antworten bleiben wirkungslos."""
    ns = param_ns(aid)
    return {k[len(ns):]: [str(v) for v in form.getlist(k)] for k in form if k.startswith(ns) and len(k) > len(ns)}


def _keep_task(resp: Response, kid: str, aid: str) -> Response:
    """Bei einem Fehler bleibt die beantwortete Aufgabe samt gewählter Antwort stehen (statt der nächsten)."""
    loc = resp.headers.get("location", "") if isinstance(resp, RedirectResponse) else ""
    if not loc.startswith(BASE + "?") or "err=" not in loc or not kid or not _SAFE.fullmatch(kid):
        return resp
    extra = {"cur": kid} | ({"ch": aid} if aid and _SAFE.fullmatch(aid) else {})
    return RedirectResponse(f"{loc}&{urlencode(extra)}", status_code=303)


def _page(request: Request, *, msg: str = "", err: str = "", skip: str = "", new: int = 0, hints: str = "",
          done: int = 0, gone: int = 0, acts: str = "", kl: str = "", dids: str = "",
          same: dict[str, str] | None = None, errors: list[str] | None = None, chosen: str = "",
          values: dict[str, list[str]] | None = None, more: dict[str, str] | None = None,
          focus: str = "") -> Response:
    ctx = get_ctx(request)
    show_hints = hints == "1" if hints else (request.cookies.get("dq_hints") == "1" and not new)
    raw = request.cookies.get("dq_skip", "")
    ref = raw if G.is_pass(raw) and not new else G.new_pass()
    skipped = [] if new else G.load_pass(ctx.db, raw)
    for x in G.parse_skip(skip):
        if x not in skipped:
            skipped.append(x)
    G.save_pass(ctx.db, ref, skipped)
    st = G.step(ctx, skipped, hints=show_hints, focus=focus)
    if st.task is not None and chosen and chosen not in {a.id for a in st.task.answers}:
        chosen = ""  # Antwort gehört zu einer anderen (inzwischen erledigten) Aufgabe
    undo = None
    if done:
        row = ctx.db.q1("SELECT id, action, status, title FROM diag_decision WHERE id=?", (done,))
        if row is not None and row["status"] == "active":
            undo = {"d": row["id"]}
    elif acts and all(x.isdigit() for x in acts.split(",")):
        undo = {"acts": acts}
    elif kl and kl.isalnum():
        undo = {"kl": kl}
    elif dids and all(x.isdigit() for x in dids.split(",")):
        undo = {"dids": dids}
    else:  # vorgemerkte Buchung bzw. Rechenprüfung
        undo = next(({k: v} for k, v in (more or {}).items() if v and _SAFE.fullmatch(v)), None)
    batch = None
    if same and same.get("sig") and same.get("sa"):  # „für alle gleich beantworten“ anbieten
        n = len(G.similar(ctx, same["sig"], same["sa"]))
        if n:
            batch = {**same, "n": n}
    rest = G.rest_overview(ctx) if st.task is None else {}
    declined = G.declined_takeovers(ctx) if st.task is None else []
    resp = render(request, "guide.html", active="quality", step=st, task=st.task, title=G.TITLE, rest=rest,
                  rest_n=sum(rest.values()), declined=declined,
                  pos=G.theme_position(st), msg=msg, err=err, errors=errors or [], undo=undo, gone=gone,
                  chosen=chosen, values=values or {}, values_for=chosen if values is not None else "",
                  hints=show_hints, batch=batch, **_common())
    if st.task is None:
        _clear(resp)
    else:
        _set(resp, st.task.kid, ref, show_hints)
    return resp


def _to_log(**q: str) -> Response:
    qs = urlencode({k: v[:600] for k, v in q.items() if v})
    return RedirectResponse(f"{BASE}/protokoll" + (f"?{qs}" if qs else "") + "#antworten", status_code=303)


def _log_extra(ctx: Any, rows: list[Any]) -> dict[str, Any]:
    """Abhängigkeiten und weitere Protokollquellen für „Was ich geändert habe“: Teil einer Quellenübernahme (nur
    gemeinsam zurücknehmbar), spätere Korrekturen an denselben Buchungen, Stapelaktionen und Quellenübernahmen."""
    import json

    db = ctx.db
    owner = {int(r["decision_id"]): int(r["source_id"]) for r in db.q(
        "SELECT decision_id, source_id FROM ds_takeover WHERE status IN ('applied', 'undoing') AND decision_id IS NOT "
        "NULL")}
    deps: dict[int, list[int]] = {}
    for d in rows:
        if d.status == "active" and d.action == "fix" and d.id not in owner:
            r = db.q1("SELECT ops_json FROM diag_decision WHERE id=?", (d.id,))
            try:
                later = A._dependents(db, d.id, json.loads(r["ops_json"] or "[]")) if r is not None else []
            except ValueError:
                later = []
            if later:
                deps[d.id] = later
    acts = db.q("SELECT a.id, a.batch_id, a.action, a.label, a.status, a.created_at, b.filename AS blabel "
                "FROM import_action a LEFT JOIN csv_batch b ON b.id=a.batch_id ORDER BY (a.status='active') DESC, "
                "a.id DESC LIMIT 40")
    takeovers = db.q("SELECT t.id, t.source_id, t.status, t.applied_at, t.undone_at, s.name FROM ds_takeover t "
                     "LEFT JOIN data_source s ON s.id=t.source_id WHERE t.status IN ('applied', 'undone') "
                     "ORDER BY t.id DESC LIMIT 20")
    by_takeover = {int(r["action_id"]) for r in db.q("SELECT action_id FROM ds_takeover WHERE status='applied' AND "
                                                       "action_id IS NOT NULL")}
    return {"owner": owner, "deps": deps, "acts": acts, "takeovers": takeovers, "act_owned": by_takeover,
            "declined": G.declined_takeovers(ctx), "acks": G.integrity_acks(ctx)}


def _go_log(**q: str) -> Response:
    qs = urlencode({k: v for k, v in q.items() if v})
    return RedirectResponse(f"{BASE}/protokoll" + (f"?{qs}" if qs else "") + "#sicherung", status_code=303)


@extra_jobs
def _register_jobs(s: Scheduler) -> None:
    s.register(RS.JOB, RS.job_run, None)  # nur auf Anforderung („Von vorn beginnen“)


def _undo_decision(ctx: Any, did: int) -> list[str] | None:
    """Antwort zurücknehmen (Korrektur bzw. Entscheidung); ``None``, wenn sie nicht mehr aktiv ist."""
    row = ctx.db.q1("SELECT action, status FROM diag_decision WHERE id=?", (did,))
    if row is None or row["status"] != "active":
        return None
    return (A.undo if row["action"] == "fix" else A.reopen)(ctx, did).errors


def _undo(ctx: Any, v: dict[str, str]) -> Response:
    """Rückgängig auf der nächsten Karte bzw. im Protokoll (synchron, im Threadpool)."""
    if v["dids"]:  # „für alle gleich“: alle Entscheidungen zurücknehmen
        errs: list[str] = []
        for did in [int(x) for x in v["dids"].split(",") if x.isdigit()]:
            errs += _undo_decision(ctx, did) or []
        return _go(err=" ".join(errs[:3])) if errs else _go(msg="Zurückgenommen – die Fragen sind wieder offen.", new=1)
    if v["acts"]:  # Prüfstapel: Stapelaktionen zurücknehmen
        from app.csvimport import batch as BA
        from app.csvimport.service import csv_service

        svc = csv_service(ctx)
        errs = []
        for a in [int(x) for x in v["acts"].split(",") if x.isdigit()]:
            errs += BA.undo(svc, a).get("errors") or []
        if v["back"] == "protokoll":
            return _to_log(err=" ".join(errs[:3]),
                           msg="" if errs else "Zurückgenommen – die Vorgänge sind wieder offen.")
        return _go(err=" ".join(errs[:3])) if errs else _go(msg="Zurückgenommen – die Vorgänge sind wieder offen.",
                                                            new=1)
    if v["rm"]:  # „Buchung stimmt“ → Vormerkung wieder setzen
        ok = G.recheck_restore(ctx, v["rm"])
        return _go(msg="Zurückgenommen – die Buchung ist wieder vorgemerkt.", new=1) if ok else \
            _go(err="Diese Antwort ist schon zurückgenommen.")
    if v["ro"].isdigit():  # zurückgenommene Buchung wieder in Kraft setzen
        from app.diagnosis import recheck as RC

        errs = RC.undo(ctx, int(v["ro"]))["errors"]
        return _go(err=" ".join(errs)) if errs else _go(msg="Zurückgenommen – die Buchung zählt wieder.", new=1)
    if v["ia"]:
        ok = G.unack_integrity(ctx, v["ia"])
        if v["back"] == "protokoll":
            return _to_log(msg="Zurückgenommen – der Befund ist wieder offen." if ok else "",
                           err="" if ok else "Diese Antwort ist schon zurückgenommen.")
        return _go(msg="Zurückgenommen – die Aufgabe ist wieder offen.", new=1) if ok else \
            _go(err="Diese Antwort ist schon zurückgenommen.")
    if v["kl"]:  # Klärungsfall
        from app.diagnosis import clarify as K

        r = K.undo_run(ctx, v["kl"])
        return _go(msg="Zurückgenommen." if r.ok else "", err=" ".join(r.errors), new=1)
    if not v["d"].isdigit():
        return _go(err="Ungültige Angabe.")
    errs2 = _undo_decision(ctx, int(v["d"]))
    if v["back"] == "protokoll":  # Ergebnis erst auswerten, dann mit Meldung zurück ins Protokoll
        return _to_log(err="Diese Antwort ist schon zurückgenommen." if errs2 is None else " ".join(errs2),
                       msg="" if errs2 is None or errs2 else "Zurückgenommen – die Frage ist wieder offen.")
    if errs2 is None:
        return _go(err="Diese Antwort ist schon zurückgenommen.")
    if errs2:
        return _go(err=" ".join(errs2))
    return _go(msg="Zurückgenommen – die Aufgabe ist wieder offen.", new=1)


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get(BASE, response_class=HTMLResponse)
    def assistant(request: Request, msg: str = "", err: str = "", skip: str = "", new: int = 0, hints: str = "",
                  done: int = 0, gone: int = 0, acts: str = "", kl: str = "", dids: str = "", sig: str = "",
                  sa: str = "", st: str = "", rm: str = "", ro: int = 0, ia: str = "", cur: str = "",
                  ch: str = "") -> Response:
        keep = bool(err) and bool(_SAFE.fullmatch(cur))  # nach einem Fehler: dieselbe Aufgabe, dieselbe Antwort
        return _page(request, msg=msg, err=err, skip=skip, new=new, hints=hints, done=done, gone=gone, acts=acts,
                     kl=kl, dids=dids, same={"sig": sig[:80], "sa": sa[:80], "st": st[:160]},
                     more={"rm": rm[:120], "ro": str(ro or ""), "ia": ia[:80]}, focus=cur if keep else "",
                     chosen=ch if keep and _SAFE.fullmatch(ch) else "")

    @router.post(f"{BASE}/antwort")
    async def answer(request: Request) -> Response:
        form = await request.form()
        resp = await _answer(request, form)
        return _keep_task(resp, str(form.get("kid") or ""), str(form.get("a") or ""))

    async def _answer(request: Request, form: Any) -> Response:
        ctx = get_ctx(request)
        fid, aid = str(form.get("f") or form.get("ref") or ""), str(form.get("a") or "")
        typ = str(form.get("typ") or "frage")
        try:
            before = int(str(form.get("before") or "0"))
        except ValueError:
            before = 0
        kid = str(form.get("kid") or "")
        if not aid:
            return _go(err="Bitte eine Antwort auswählen.")

        def gone() -> int:
            return max(0, before - len(G.tasks(ctx)) - 1) if before else 0

        # Navigation und Überspringen brauchen nur den Aufgabenbezug (kid) – eine fachliche Referenz verlangen erst
        # die schreibenden Antworten unten (Seitenaufgaben haben keine)
        if aid == "skip":
            return _go(skip=kid) if kid else _go(err="Die Aufgabe ist nicht mehr bekannt – bitte neu laden.")
        if aid == "go:open":  # ausdrücklich gewählt: Seite öffnen (Ziel aus der Aufgabe, nie aus dem Formular)
            href = await run_in_threadpool(_href_of, ctx, kid) if kid else ""
            return RedirectResponse(with_kid(href, kid), status_code=303) if href else \
                _go(err="Die Aufgabe besteht nicht mehr.")
        if not fid:
            return _go(err="Diese Antwort braucht einen Vorgang – die Aufgabe hat sich geändert, bitte neu laden.")
        if typ in ("erneut", "rechnung"):
            return await run_in_threadpool(_simple_answer, ctx, typ, fid, aid, gone)
        if typ in ("stapel", "zeile"):
            return await _stack_answer(ctx, typ, fid, aid, form, gone)
        if typ == "fall":
            return await _case_answer(ctx, fid, aid, form, gone)
        if typ == "vorrang":
            if aid == "vr:yes" and fid.isdigit():  # mit Aufgabenbezug: Rückweg und gezieltes Überspringen dort
                return RedirectResponse(with_kid(f"/settings/datasources/{int(fid)}/uebernahme",
                                                 str(form.get("kid") or "")), status_code=303)
            if aid == "vr:no" and fid.isdigit():
                await run_in_threadpool(G.decline_takeover, ctx, int(fid))
                return _go(msg="Gespeichert: der Import bleibt für dieses Konto maßgeblich.",
                           gone=await run_in_threadpool(gone))
            return _go(err="Unbekannte Antwort.")
        if aid == "chain:scan":
            return await run_in_threadpool(_chain_scan, ctx, fid, str(form.get("kid") or ""))
        sig, _a = await run_in_threadpool(_signature, ctx, fid, aid)
        text = str(form.get(f"l_{aid}") or "")[:160]

        if aid.startswith("mark:"):
            action = aid.split(":", 1)[1]
            res = await run_in_threadpool(A.mark, ctx, fid, action, "Antwort im Assistenten")
            if not res.ok:
                return _go(err=" ".join(res.errors))
            return _go(msg=MARK_TEXT.get(action, "Gespeichert."), done=res.decision_id,
                       gone=await run_in_threadpool(gone), sig=sig, sa=aid, st=text)
        if not aid.startswith("opt:"):
            return _go(err="Unbekannte Antwort.")
        key = aid.split(":", 1)[1]
        params = _params(form, aid)
        token = str(form.get(f"t_{aid}") or form.get("token") or "")
        needs = str(form.get(f"n_{aid}") or "") == "1"
        if (needs or not token) and not form.get("confirm"):  # Eingaben bzw. keine Vorschau → Wirkung zeigen
            return await run_in_threadpool(_confirm, request, fid, key, aid, params, kid)
        res = await run_in_threadpool(A.apply, ctx, fid, key, params, token)
        if not res.ok:
            return _go(err=" ".join(res.errors))
        return _go(msg="Gespeichert und korrigiert.", done=res.decision_id, gone=await run_in_threadpool(gone),
                   sig=sig, sa=aid if not params else "", st=text)

    def _href_of(ctx: Any, kid: str) -> str:
        t = next((t for t in G.tasks(ctx, hints=True) if t.kid == kid), None)
        return t.href if t is not None and t.href.startswith("/") and not t.href.startswith("//") else ""

    def _simple_answer(ctx: Any, typ: str, ref: str, aid: str, gone: Any) -> Response:
        """Vorgemerkte Buchung (behalten/zurücknehmen) bzw. Rechenprüfung (zur Kenntnis genommen)."""
        from app.diagnosis import recheck as RC

        if typ == "rechnung" and aid == "int:ack":
            if not G.ack_integrity(ctx, ref):
                return _go(err="Die Aufgabe besteht nicht mehr.")
            return _go(msg="Zur Kenntnis genommen – nichts geändert.", ia=ref, gone=gone())
        if typ == "erneut" and aid == "re:keep":
            if not G.recheck_keep(ctx, ref):
                return _go(err="Die Buchung ist nicht mehr vorgemerkt.")
            return _go(msg="Gespeichert: die Buchung stimmt.", rm=ref, gone=gone())
        if typ == "erneut" and aid == "re:reopen":
            res = RC.reopen(ctx, ref)
            if res["errors"]:
                return _go(err=" ".join(res["errors"]))
            return _go(msg="Zurückgenommen – der Vorgang ist wieder bei den neuen Vorgängen.", ro=res["id"],
                       gone=gone())
        return _go(err="Unbekannte Antwort.")

    def _signature(ctx: Any, fid: str, aid: str) -> tuple[str, str]:
        """Muster des Befunds und Antworttext – vor dem Ausführen (danach gibt es den Befund nicht mehr)."""
        from app.diagnosis.engine import report_for

        f = report_for(ctx).by_id(fid)
        return (G.signature(f), aid) if f is not None else ("", "")

    def _chain_scan(ctx: Any, fid: str, kid: str) -> Response:
        from app.diagnosis import chainscan as CS
        from app.diagnosis.engine import report_for

        f = report_for(ctx).by_id(fid)
        d = (f.data or {}) if f is not None else {}
        acc, asset = str(d.get("account") or ""), str(d.get("asset") or "")
        if not acc or not asset:
            return _go(err="Die Aufgabe besteht nicht mehr.")
        if ctx.scheduler is not None and ctx.scheduler.trigger(CS.JOB, 0.2, account=acc, asset=asset):
            return _go(msg="Die Suche auf der Blockchain läuft (1–5 Minuten). Die Frage kommt danach mit dem Ergebnis "
                           "wieder – solange geht es mit der nächsten weiter.", skip=kid)
        out = CS.job_run(ctx, acc, asset)  # ohne Scheduler (Tests, CLI): direkt
        return _go(msg="Suche auf der Blockchain abgeschlossen." if out.get("ok") else "",
                   err=out.get("error", "") if not out.get("ok") else "")

    @router.post(f"{BASE}/alle")
    async def answer_all(request: Request) -> Response:
        """Dieselbe Antwort für alle offenen Fragen mit gleichem Muster (höchstens :data:`G.BATCH_LIMIT`)."""
        from app.diagnosis.engine import report_for

        ctx = get_ctx(request)
        form = await request.form()
        sig, aid = str(form.get("sig") or ""), str(form.get("a") or "")

        def run() -> tuple[list[int], list[str]]:
            ids, errs = [], []
            for fid in G.similar(ctx, sig, aid):
                if aid.startswith("mark:"):
                    res = A.mark(ctx, fid, aid.split(":", 1)[1], "Im Assistenten: für alle gleich beantwortet")
                else:
                    report = report_for(ctx)
                    f = report.by_id(fid)
                    if f is None:
                        continue
                    key = aid.split(":", 1)[1]
                    plan = A.build_plan(ctx, report, f, key, {}, given=False)
                    if plan.errors:
                        errs += plan.errors[:1]
                        continue
                    res = A.apply(ctx, fid, key, {}, plan.token)
                if res.ok and res.decision_id:
                    ids.append(int(res.decision_id))
                else:
                    errs += res.errors[:1]
            return ids, errs

        ids, errs = await run_in_threadpool(run)
        if not ids:
            return _go(err=" ".join(errs[:2]) or "Keine passenden Fragen mehr offen.")
        return _go(msg=f"{len(ids)} Fragen gleich beantwortet." + (f" Nicht alle gingen: {errs[0]}" if errs else ""),
                   dids=",".join(str(i) for i in ids))

    @router.get(f"{BASE}/protokoll", response_class=HTMLResponse)
    def log_page(request: Request) -> HTMLResponse:
        """Was ich geändert habe: Antworten mit Korrektur oder Entscheidung, jeweils zurücknehmbar; darunter „Von vorn
        beginnen“ (alles zurücknehmen) und „Sicherung wiederherstellen“."""
        from app import dbrestore as DR
        from app.jobs.maintenance import list_backups

        ctx = get_ctx(request)
        rows = A.decisions(ctx.db, 100)
        return render(request, "guide_log.html", active="quality", title=G.TITLE, rows=rows, log=_log_extra(ctx, rows),
                      mark_actions=A.MARK_ACTIONS, reset_last=RS.last(ctx.db), reset_running=RS.running(ctx),
                      backups=list_backups(ctx), restore_pending=DR.pending(ctx.config),
                      restore_result=DR.result(ctx.config), restore_word=DR.CONFIRM_WORD)

    @router.get(f"{BASE}/neustart", response_class=HTMLResponse)
    def reset_page(request: Request, s: list[str] = Query(default=[]), err: str = "") -> HTMLResponse:
        """Von vorn beginnen: Umfang wählen, Vorschau mit Prüfsumme, doppelte Bestätigung."""
        ctx = get_ctx(request)
        return render(request, "guide_reset.html", active="quality", title=G.TITLE, plan=RS.plan(ctx, s),
                      scopes=RS.SCOPES, word=RS.CONFIRM_WORD, kind_label=RS.KIND_LABEL, state_label=RS.STATE_LABEL,
                      err=err[:300])

    def _reset_start(ctx: Any, scopes: list[str], token: str) -> Response:
        back = f"{BASE}/neustart?" + urlencode([("s", x) for x in scopes])
        if RS.running(ctx) is not None:
            return RedirectResponse(back + "&" + urlencode({"err": "Das Zurücksetzen läuft bereits."}), 303)
        if RS.plan(ctx, scopes).token != token:
            return RedirectResponse(back + "&" + urlencode({"err": "Seit der Vorschau hat sich etwas geändert – bitte "
                                                                   "die Vorschau prüfen und erneut bestätigen."}), 303)
        if ctx.scheduler is None or not ctx.scheduler.trigger(RS.JOB, 0.2, scopes=scopes, token=token):
            RS.run(ctx, scopes, token)  # ohne Scheduler (Tests, CLI): direkt
        return RedirectResponse(f"{BASE}/protokoll#neustart", status_code=303)

    @router.post(f"{BASE}/neustart")
    async def reset_start(request: Request) -> Response:
        ctx = get_ctx(request)
        form = await request.form()
        scopes = RS.normalize([str(x) for x in form.getlist("s")])
        if str(form.get("word") or "").strip() != RS.CONFIRM_WORD or form.get("ok") != "1":
            return RedirectResponse(f"{BASE}/neustart?" + urlencode(
                [("s", x) for x in scopes] + [("err", f"Bitte das Häkchen setzen und {RS.CONFIRM_WORD} eintippen – "
                                                      "es wurde nichts geändert.")]), status_code=303)
        return await run_in_threadpool(_reset_start, ctx, scopes, str(form.get("token") or ""))

    @router.get(f"{BASE}/neustart/status", response_class=HTMLResponse)
    def reset_status(request: Request) -> Response:
        p = RS.running(get_ctx(request))
        if p is None:  # fertig → Seite neu laden (zeigt das Ergebnis)
            return Response(status_code=204, headers={"HX-Refresh": "true"})
        return render(request, "partials/reset_progress.html", reset_running=p)

    @router.post(f"{BASE}/sicherung")
    async def restore(request: Request) -> Response:
        """Sicherung für den nächsten Start vormerken und Portfolia geordnet neu starten."""
        from app import dbrestore as DR

        ctx = get_ctx(request)
        form = await request.form()
        name = str(form.get("name") or "")
        if str(form.get("word") or "").strip() != DR.CONFIRM_WORD or form.get("ok") != "1":
            return _go_log(err=f"Bitte das Häkchen setzen und {DR.CONFIRM_WORD} eintippen – es wurde nichts "
                               "geändert.")
        errs = DR.request(ctx, name)
        if errs:
            return _go_log(err=" ".join(errs))
        DR.restart_process()
        return render(request, "guide_restart.html", active="quality", name=name)

    @router.post(f"{BASE}/sicherung/abbrechen")
    async def restore_cancel(request: Request) -> Response:
        from app import dbrestore as DR

        DR.cancel(get_ctx(request).config)
        return _go_log(msg="Vorgemerkte Wiederherstellung verworfen.")

    @router.post(f"{BASE}/sicherung/ok")
    async def restore_ok(request: Request) -> Response:
        from app import dbrestore as DR

        DR.dismiss_result(get_ctx(request).config)
        return _go_log()

    async def _stack_answer(ctx: Any, typ: str, ref: str, aid: str, form: Any, gone: Any) -> Response:
        import json

        from app.csvimport import batch as BA
        from app.csvimport import bulk as BU
        from app.csvimport.service import csv_service

        svc = csv_service(ctx)
        if aid == "stk:look":
            return RedirectResponse("/journal/pruefung?tab=sicher", status_code=303)
        if typ == "stapel" and aid == "stk:all":
            try:
                parts = json.loads(str(form.get("x_parts") or "[]"))
            except ValueError:
                parts = []
            if not parts:
                return _go(err="Nichts mehr zu übernehmen – die Vorgänge haben sich inzwischen geändert.")
            res = await run_in_threadpool(BU.run, svc, parts)
            errs = [e for r in res for e in r.get("errors") or []]
            acts = ",".join(str(r["action_id"]) for r in res if r.get("action_id"))
            if errs and not acts:
                return _go(err=" ".join(errs[:3]))
            return _go(msg="Übernommen." + (f" Nicht alles ging: {errs[0]}" if errs else ""), acts=acts,
                       gone=await run_in_threadpool(gone))
        bid_s, _, event = ref.partition(":")
        op = aid.split(":", 1)[1] if aid.startswith("row:") else ""
        if not bid_s.isdigit() or op not in ("link", "include", "skip", "defer", "pair", "nopair"):
            return _go(err="Unbekannte Antwort.")
        bid = int(bid_s)

        shown = str(form.get("x_fp") or "")
        shown_rev = str(form.get("x_rev") or "")

        def run() -> dict[str, Any]:
            from app.csvimport.service import _LOCK as CSV_LOCK

            with CSV_LOCK:  # Prüfung und Änderung unter derselben Sperre wie Eingaben und Stapelaktionen
                return decide()

        def decide() -> dict[str, Any]:
            svc.evaluate(bid)
            rows = svc.rows(bid)
            idxs = [rc.idx for rc in rows if (rc.rec.event_key or f"#{rc.idx}") == event and rc.open]
            if not idxs:
                return {"errors": ["Der Vorgang ist schon entschieden oder hat sich geändert."]}
            # Bindung an den angezeigten Stand der gesamten Wirkungsmenge (eigene Zeilen, Transferpartner, Journal)
            if op in ("include", "link", "pair", "nopair") and BA.effect_digest(ctx, rows, event) != shown:
                return {"errors": ["Der Vorgang oder seine Gegenbuchung hat sich seit der Anzeige geändert (Betrag, "
                                   "Wert, Konto, Gebühr oder Transferzuordnung) – bitte die neu angezeigten Angaben "
                                   "prüfen; es wurde nichts geändert."]}
            if op in ("skip", "defer"):
                return BU.decide(svc, bid, idxs, op)
            # dieselbe Revision wie angezeigt – die Ausführung prüft sie erneut in ihrer Schreibtransaktion
            expect = (BA.effect_refs(rows, event), shown_rev)
            if op in ("pair", "nopair"):  # Transferfrage beantworten (pair_ok wie im Prüf-Stapel), dann buchen
                mine = [rc for rc in rows if rc.idx in set(idxs)]
                refs = [rc for rc in mine if (rc.pair_ref or "").startswith("b:") and not rc.pair_alts]
                if not refs:
                    return {"errors": ["Dieser Transfer lässt sich nur in der Detailansicht klären."]}
                touched = {rc.idx for rc in refs} | {int(rc.pair_ref[2:]) for rc in refs}
                before = {rc.idx: rc.pair_ok for rc in rows if rc.idx in touched}
                svc.set_rows(bid, {f"pair_{rc.idx}": "1" if op == "pair" else "0" for rc in refs})
                svc.evaluate(bid)
                rows = svc.rows(bid)
                want = set(idxs) | ({int(rc.pair_ref[2:]) for rc in refs} if op == "pair" else set())
                p = BA.plan(svc, bid, "include", want, True, rows)
                if not p.todo or any(not it.act for it in p.items if it.rc.idx in want):
                    why = next((it.note for it in p.items if not it.act), "nicht buchbar")
                    _restore_pairs(bid, before)  # keine halbe Entscheidung: Transferstand wie vorher
                    return {"errors": [f"Nicht gebucht: {why}. Es wurde nichts geändert."]}
                res = BA.execute(svc, bid, "include", {i.rc.idx for i in p.todo}, token=BA.new_token(),
                                 fingerprint=p.fingerprint(), force=True, expect_refs=expect)
                if res.get("errors"):
                    _restore_pairs(bid, before)  # nichts halb gespeichert
                return res
            p = BA.plan(svc, bid, op, idxs, True, rows)  # ausdrückliche Antwort: auch bei abweichenden Angaben
            if not p.todo:
                return {"errors": [p.items[0].note if p.items else "Nichts auszuführen."]}
            return BA.execute(svc, bid, op, idxs, token=BA.new_token(), fingerprint=p.fingerprint(), force=True,
                              expect_refs=expect)

        def _restore_pairs(bid: int, before: dict[int, int | None]) -> None:
            ctx.db.xmany("UPDATE csv_row SET pair_ok=? WHERE batch_id=? AND idx=?",
                         [(v, bid, i) for i, v in before.items()])
            svc.evaluate(bid)

        res = await run_in_threadpool(run)
        if res.get("errors"):
            return _go(err=" ".join(res["errors"][:3]))
        text = {"link": "Verknüpft – zählt nicht doppelt.", "include": "Gebucht.", "skip": "Wird nicht gebucht.",
                "defer": "Zurückgestellt – nichts geändert.", "pair": "Als Transfer gebucht.",
                "nopair": "Als eigener Vorgang gebucht – kein Transfer."}[op]
        return _go(msg=text, acts=str(res.get("action_id") or ""), gone=await run_in_threadpool(gone))

    async def _case_answer(ctx: Any, key: str, aid: str, form: Any, gone: Any) -> Response:
        import json

        from app.diagnosis import clarify as K

        if aid == "case:defer":
            res = await run_in_threadpool(K.defer, ctx, key, "Im Assistenten: passt nicht")
            return _go(msg="Gespeichert: passt nicht – der Fall ruht bis zu neuen Belegen." if res.ok else "",
                       err=" ".join(res.errors), gone=await run_in_threadpool(gone))
        if aid != "case:apply":
            return _go(err="Unbekannte Antwort.")
        try:
            tokens = json.loads(str(form.get("x_tokens") or "{}"))
        except ValueError:
            tokens = None
        res = await run_in_threadpool(lambda: K.apply_hypothesis(
            ctx, key, str(form.get("x_hyp") or ""), "manual", authorization="user",
            expect_version=str(form.get("x_version") or "") or None, expect_tokens=tokens))
        if not res.ok:
            return _go(err=" ".join(res.errors))
        # Teilfortschritt: was geändert wurde und was offen bleibt (statt pauschal „Übernommen“)
        return _go(msg=res.message or "Übernommen.", kl=res.token, gone=await run_in_threadpool(gone))

    def _confirm(request: Request, fid: str, key: str, aid: str, params: dict[str, list[str]],
                 kid: str = "") -> Response:
        from app.diagnosis.engine import report_for

        ctx = get_ctx(request)
        report = report_for(ctx)
        f = report.by_id(fid)
        if f is None:
            return _go(err="Die Aufgabe besteht nicht mehr – die Daten haben sich geändert.")
        # Das Formular wurde abgeschickt: eine leere Mehrfachauswahl bleibt leer (keine Vorgaben einsetzen)
        plan = A.build_plan(ctx, report, f, key, params, given=True)
        if plan.errors:  # dieselbe Aufgabe, dieselbe Antwort, dieselben Eingaben
            return _page(request, errors=plan.errors, chosen=aid, values=params, focus=kid)
        effects = A.preview(ctx, report, plan)
        return render(request, "guide_confirm.html", active="quality", title=G.TITLE, f=f, plan=plan,
                      effects=effects, aid=aid, params=plan.params, ns=param_ns(aid), kid=kid,
                      shown=_shown_params(plan))

    def _shown_params(plan: Any) -> list[tuple[str, str]]:
        """Eingaben der gewählten Antwort zur Kontrolle in der Bestätigung (Bezeichnung, Wert in Klartext)."""
        out = []
        for p in plan.option.params:
            vals = plan.params.get(p.name, [])
            names = dict(p.choices)
            out.append((p.label, ", ".join(names.get(v, v) for v in vals) if vals else "keine Auswahl"))
        return out

    @router.post(f"{BASE}/vorrang/{{sid}}/wieder")
    def takeover_reopen(request: Request, sid: int) -> Response:
        """Abgelehnte Quellenpräferenz gezielt wieder öffnen – ohne den übrigen Prüfablauf zurückzusetzen."""
        ok = G.reopen_takeover(get_ctx(request), sid)
        return _to_log(msg="Die Frage zum Quellenvorrang erscheint wieder in „Buchungen prüfen“." if ok else "",
                       err="" if ok else "Diese Ablehnung besteht nicht mehr.")

    @router.post(f"{BASE}/rueckgaengig")
    async def undo(request: Request) -> Response:
        ctx = get_ctx(request)
        form = await request.form()
        vals = {k: str(form.get(k) or "") for k in ("acts", "kl", "dids", "d", "back", "rm", "ro", "ia")}
        return await run_in_threadpool(_undo, ctx, vals)

    @router.get(f"{BASE}/ende")
    def finish(request: Request) -> Response:
        resp = RedirectResponse("/quality", status_code=303)
        _clear(resp)
        return resp

    return router


register_router(make_router)
