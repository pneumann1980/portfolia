"""DKB-Postbox: alle Dokumente per Knopfdruck abrufen, in Ordner ablegen und Depot-Abrechnungen auswerten.

Zusammenspiel (siehe :mod:`app.datasources.plugins` für die Lizenzgrenze):

1. Der **Fetcher** (eigenes GPL-Programm, bei Bedarf installiert) meldet sich bei der DKB an,
wartet auf die Freigabe in der
   DKB-App und lädt neue Dokumente nach ``<Dokumentenordner>/<Datenquelle>/<Kategorie>/<Konto>/<Datum>_<Name>``.
   Er kennt nur Zugangsdaten (stdin, einmalig), Zielordner und die Liste bereits geladener Kennungen.
2. Portfolia liest dessen JSON-Zeilen, prüft jeden Pfad (nur unterhalb des Zielordners), berechnet
die Prüfsumme selbst und
   führt den Index (``bank_doc``): ein Dokument wird nie zweimal geladen und nie überschrieben.
3. **Depot-Abrechnungen** (Wertpapierabrechnung, Ertragsabrechnung) gehen durch den vorhandenen
Dokumentimport (PDF-Text →
   Vorgänge → Abgleich mit vorhandenen Buchungen → Prüf-Stapel). Alles andere (Kontoauszüge,
   Karten, Steuer, Depotauszug,
   Kosteninformationen …) wird nur abgelegt. Gebucht wird nichts ohne Prüfung.

Das Passwort wird nie gespeichert: Es kommt mit dem Knopfdruck, geht per stdin an den Fetcher und wird danach verworfen
(in Meldungen maskiert). Pro Knopfdruck gibt es genau einen Anmeldeversuch.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.datasources import plugins
from app.datasources.service import sanitize_error
from app.logging_setup import get_redactor
from app.util.timeutil import iso

log = logging.getLogger(__name__)

PROVIDER = "dkb"
PLUGIN = "dkb"
EVAL_TYPES = frozenset({"dwpOrderStatement", "dwpRevenueStatement"})  # Wertpapier- und Ertragsabrechnung
RUN_TIMEOUT = 1800  # Sekunden je Lauf
EVAL_TIMEOUT = 1200
ACTIVE = ("running", "awaiting_2fa", "fetching", "evaluating")
STATUS_LABEL = {"running": "Anmeldung", "awaiting_2fa": "Freigabe in der DKB-App nötig", "fetching": "Abruf",
                "evaluating": "Auswertung", "ok": "erfolgreich", "partial": "teilweise", "error": "Fehler",
                "cancelled": "abgebrochen"}
KIND_TEXT = {
    "dependency": "Das Abrufprogramm ist unvollständig installiert – bitte „DKB-Abruf einrichten“ erneut ausführen.",
    "login": "Anmeldung abgelehnt. Es wurde nur ein Versuch unternommen.",
    "2fa_timeout": "Keine Freigabe in der DKB-App innerhalb der Wartezeit. Bitte erneut starten und die Anfrage in "
                   "der App bestätigen.",
    "2fa_canceled": "Die Freigabe wurde in der DKB-App abgebrochen.",
    "network": "Die DKB war nicht erreichbar (Netzwerk, Zeitüberschreitung).",
    "changed": "Die DKB hat ihre Schnittstelle offenbar geändert – das Abrufprogramm braucht ein Update (Knopf "
               "„Aktualisieren“, sobald eine neue Version vorliegt). Bereits abgelegte Dokumente sind unberührt.",
    "other": "Der Abruf ist fehlgeschlagen.",
}
_SAFE = re.compile(r"[^\w.\- ]+", re.UNICODE)
_LOCK = threading.Lock()


@dataclass
class Handle:
    cancel: threading.Event = field(default_factory=threading.Event)
    proc: subprocess.Popen[str] | None = None


_RUNNING: dict[int, Handle] = {}


def _now() -> str:
    return iso(datetime.now(UTC)) or ""


_STATUS = re.compile(r"\bRC:?\s*(\d{3})\b")
_STATUS_HINT = {
    401: "Benutzername oder Passwort wurden abgelehnt. Gemeint ist der Anmeldename des Online-Bankings (nicht die "
         "IBAN).",
}


BLOCK_TEXT = ("Die DKB-Anmeldeseite hat die automatisierte Anfrage abgewiesen (Bot-Schutz/Captcha). Das ist kein "
              "Fehler bei Benutzername oder Passwort. Bekanntes Problem der Bibliothek dkb-robo seit November 2025 "
              "(Issue #92 im dkb-robo-Repository): Die Anmeldung der DKB verlangt ein Captcha, das ohne Browser nicht "
              "lösbar ist; "
              "eine stabile Lösung gibt es bisher nicht. Alternative: Dokumente selbst aus der Postbox laden und im "
              "Ablageordner ablegen.")


def explain(kind: str, detail: str) -> str:
    """Meldung für die Anzeige: Erklärung der Fehlerart, Hinweis zum HTTP-Status und technische Meldung der Bibliothek
    (ohne Zugangsdaten) – damit eine abgewiesene Anmeldung nicht ohne Ursache bleibt."""
    m = _STATUS.search(detail)
    status = int(m.group(1)) if m else None
    first = "1st factor" in detail
    if (first and status in (400, 403, 429, 500, 502, 503, 504)) or kind == "blocked":
        text = BLOCK_TEXT
        if status:
            text += f" (HTTP-Status {status})"
    else:
        text = KIND_TEXT.get(kind, KIND_TEXT["other"])
        if kind == "login" and first:
            text += " Die Ablehnung kam beim ersten Schritt (Benutzername/Passwort), vor der Freigabe in der App."
        if status:
            text += f" HTTP-Status {status}: " + _STATUS_HINT.get(status, "Antwort der DKB nicht erwartet.")
    return text + (f" Technische Meldung: {detail[:240]}" if detail else "")


def safe_name(text: str, fallback: str) -> str:
    s = _SAFE.sub("_", text or "").strip(" ._")
    return (s or fallback)[:80]


def is_dkb(ds: Any) -> bool:
    """Datenquelle für den DKB-Abruf: Anbieter DKB oder ein bestehendes Konto, dessen Name mit „DKB“ beginnt."""
    from app.datasources.providers import guess_provider

    return ds.provider == PROVIDER or guess_provider(ds.account or "") == PROVIDER


# -- Einstellungen und Ordner -------------------------------------------------------------------------------------

def settings(db: Any, sid: int) -> dict[str, Any]:
    r = db.q1("SELECT * FROM bank_source WHERE source_id=?", (sid,))
    return {"username": (r["username"] if r else "") or "", "folder": (r["folder"] if r else "") or "",
            "mfa_device": int(r["mfa_device"]) if r else 0, "evaluate": bool(r["evaluate"]) if r else True}


def save_settings(db: Any, sid: int, *, username: str, folder: str, mfa_device: int, evaluate: bool) -> list[str]:
    errors: list[str] = []
    username = username.strip()
    if len(username) > 80 or any(ord(c) < 32 for c in username):
        errors.append("Benutzername ungültig.")
    folder = folder.strip()
    if folder and safe_name(folder, "") != folder:
        errors.append("Ordnername: nur Buchstaben, Ziffern, Leerzeichen, Punkt, Binde- und Unterstrich (höchstens 80).")
    if not 0 <= mfa_device <= 9:
        errors.append("Gerätenummer ungültig.")
    if errors:
        return errors
    db.x("INSERT INTO bank_source(source_id, username, folder, mfa_device, evaluate, updated_at) VALUES (?,?,?,?,?,?) "
         "ON CONFLICT(source_id) DO UPDATE SET username=excluded.username, folder=excluded.folder, "
         "mfa_device=excluded.mfa_device, evaluate=excluded.evaluate, updated_at=excluded.updated_at",
         (sid, username or None, folder or None, mfa_device, 1 if evaluate else 0, _now()))
    return []


def root_dir(ctx: Any, sid: int, ds_name: str) -> Path:
    s = settings(ctx.db, sid)
    return Path(ctx.config.documents_dir) / safe_name(s["folder"] or ds_name, f"quelle-{sid}")


def folder_state(ctx: Any, sid: int, ds_name: str) -> dict[str, Any]:
    """Zielordner: Pfad, vorhanden, beschreibbar, eigene Freigabe (Einhängepunkt) oder nur im appdata-Ordner."""
    base = Path(ctx.config.documents_dir)
    root = root_dir(ctx, sid, ds_name)
    writable = False
    with contextlib.suppress(OSError):
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".schreibtest"
        probe.write_text("x")
        probe.unlink()
        writable = True
    in_data = str(base.resolve()).startswith(str(Path(ctx.config.data_dir).resolve()))
    mounted = os.path.ismount(base) if base.exists() else False
    return {"path": str(root), "base": str(base), "writable": writable, "mounted": mounted, "in_data": in_data,
            "exists": root.exists()}


# -- Abfragen -----------------------------------------------------------------------------------------------------

def latest_run(db: Any, sid: int) -> Any:
    return db.q1("SELECT * FROM bank_run WHERE source_id=? ORDER BY id DESC LIMIT 1", (sid,))


def runs(db: Any, sid: int, limit: int = 8) -> list[Any]:
    return db.q("SELECT * FROM bank_run WHERE source_id=? ORDER BY id DESC LIMIT ?", (sid, limit))


def active(sid: int) -> bool:
    return sid in _RUNNING


def view_run(db: Any, sid: int) -> dict[str, Any] | None:
    """Jüngster Lauf für die Anzeige; ein „laufender“ Lauf ohne Prozess (Neustart) gilt als abgebrochen."""
    r = latest_run(db, sid)
    if r is None:
        return None
    d = dict(r)
    d["summary"] = json.loads(r["summary_json"] or "{}")
    if d["status"] in ACTIVE and sid not in _RUNNING:
        d["status"], d["message"] = "error", "Unterbrochen (Neustart der App) – bitte erneut starten."
    d["label"] = STATUS_LABEL.get(d["status"], d["status"])
    d["active"] = d["status"] in ACTIVE
    return d


def documents(db: Any, sid: int) -> dict[str, Any]:
    rows = db.q("SELECT category, status, COUNT(*) AS n FROM bank_doc WHERE source_id=? GROUP BY category, status",
                (sid,))
    cats: dict[str, dict[str, int]] = {}
    for r in rows:
        cats.setdefault(r["category"] or "Sonstiges", Counter())[r["status"]] += int(r["n"])  # type: ignore[arg-type]
    recent = db.q("SELECT * FROM bank_doc WHERE source_id=? ORDER BY fetched_at DESC, id DESC LIMIT 25", (sid,))
    total = sum(sum(c.values()) for c in cats.values())
    return {"categories": {k: dict(v) for k, v in sorted(cats.items())}, "recent": recent, "total": total,
            "pending": db.scalar("SELECT COUNT(*) FROM bank_doc WHERE source_id=? AND status='downloaded'", (sid,),
                                 default=0)}


# -- Abruf ---------------------------------------------------------------------------------------------------------

def fetcher_command(ctx: Any) -> list[str]:
    """Aufruf des installierten Fetchers (Tests ersetzen diese Funktion)."""
    return plugins.command(ctx.config, PLUGIN)


def ready(ctx: Any) -> bool:
    return plugins.installed(ctx.config, PLUGIN)


def start(ctx: Any, sid: int, username: str, password: str, mfa_device: int | None = None) -> dict[str, Any]:
    """Abruf im Hintergrund starten. Das Passwort wird nur an den Hintergrundlauf übergeben, nie gespeichert."""
    from app.datasources.service import datasource_service

    ds = datasource_service(ctx).get(sid)
    if ds is None or not is_dkb(ds):
        return {"errors": ["Für diese Datenquelle ist kein DKB-Abruf vorgesehen."]}
    username = username.strip()
    if not username or not password:
        return {"errors": ["Bitte Benutzername und Passwort eingeben."]}
    if not ready(ctx):
        return {"errors": ["Der DKB-Abruf ist noch nicht eingerichtet (Knopf „DKB-Abruf einrichten“)."]}
    with _LOCK:
        if sid in _RUNNING:
            return {"errors": ["Für diese Datenquelle läuft bereits ein Abruf."]}
        _RUNNING[sid] = Handle()
    try:
        s = settings(ctx.db, sid)
        if s["username"] != username and s.get("username") == "":
            save_settings(ctx.db, sid, username=username, folder=s["folder"], mfa_device=s["mfa_device"],
                          evaluate=s["evaluate"])
        mfa = s["mfa_device"] if mfa_device is None else mfa_device
        rid = int(ctx.db.x("INSERT INTO bank_run(source_id, status, message, started_at) VALUES (?, 'running', "
                           "'Anmeldung …', ?)", (sid, _now())).lastrowid)  # type: ignore[arg-type]
    except Exception:
        _RUNNING.pop(sid, None)
        raise
    threading.Thread(target=_work, args=(ctx, sid, rid, username, password, int(mfa), ds.account, ds.name),
                     name=f"dkb-{sid}", daemon=True).start()
    return {"run_id": rid, "errors": []}


def start_evaluate(ctx: Any, sid: int) -> dict[str, Any]:
    """Bereits abgelegte, noch nicht ausgewertete Depot-Abrechnungen auswerten – ohne Anmeldung bei der Bank."""
    from app.datasources.service import datasource_service

    ds = datasource_service(ctx).get(sid)
    if ds is None or not is_dkb(ds):
        return {"errors": ["Für diese Datenquelle ist kein DKB-Abruf vorgesehen."]}
    with _LOCK:
        if sid in _RUNNING:
            return {"errors": ["Für diese Datenquelle läuft bereits ein Abruf."]}
        _RUNNING[sid] = Handle()
    rid = int(ctx.db.x("INSERT INTO bank_run(source_id, status, message, started_at) VALUES (?, 'evaluating', "
                       "'Depot-Abrechnungen werden ausgewertet …', ?)", (sid, _now())).lastrowid)  # type: ignore[arg-type]

    def work() -> None:
        handle = _RUNNING[sid]
        summary: dict[str, Any] = {"downloaded": 0, "existing": 0, "failed": 0, "categories": {}, "errors": []}
        status, message = "ok", "Auswertung abgeschlossen."
        try:
            ev = evaluate_pending(ctx, sid, ds.account, root_dir(ctx, sid, ds.name), handle.cancel)
            summary["evaluated"] = ev
            if ev.get("error"):
                status, message = "partial", str(ev["error"])
            else:
                message = (f"{ev['evaluated']} Dokumente ausgewertet, {ev['transactions']} Vorgänge im Prüf-Stapel"
                           if ev["documents"] else "Keine neuen Depot-Abrechnungen.")
        except Exception as e:  # pragma: no cover - Absicherung
            log.exception("DKB: Auswertung fehlgeschlagen")
            status, message = "error", f"Auswertung fehlgeschlagen ({type(e).__name__})."
        finally:
            _upd(ctx.db, rid, status=status, message=message[:500], finished_at=_now(),
                 summary_json=json.dumps(summary, ensure_ascii=False, default=str))
            _RUNNING.pop(sid, None)
            ctx.db.close_thread_conn()

    threading.Thread(target=work, name=f"dkb-eval-{sid}", daemon=True).start()
    return {"run_id": rid, "errors": []}


def cancel(sid: int) -> bool:
    h = _RUNNING.get(sid)
    if h is None:
        return False
    h.cancel.set()
    p = h.proc
    if p is not None and p.poll() is None:
        with contextlib.suppress(OSError):
            p.terminate()
    return True


def _upd(db: Any, rid: int, **fields: Any) -> None:
    cols = ", ".join(f"{k}=?" for k in fields)
    db.x(f"UPDATE bank_run SET {cols} WHERE id=?", (*fields.values(), rid))


def _sha(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def _inside(root: Path, rel: str) -> Path | None:
    """Pfad aus der Meldung des Fetchers: relativ, ohne ``..``, unterhalb des Zielordners, vorhanden."""
    if not rel or rel.startswith(("/", "\\")) or ".." in Path(rel).parts:
        return None
    p = (root / rel).resolve()
    return p if str(p).startswith(str(root.resolve()) + os.sep) and p.is_file() else None


def _work(ctx: Any, sid: int, rid: int, username: str, password: str, mfa: int, account: str, name: str) -> None:
    db = ctx.db
    handle = _RUNNING[sid]
    summary: dict[str, Any] = {"downloaded": 0, "existing": 0, "failed": 0, "categories": {}, "errors": []}
    status, message = "error", "Abruf fehlgeschlagen."
    try:
        with get_redactor().temporary([password]):
            status, message = _fetch(ctx, sid, rid, username, password, mfa, name, handle, summary)
            password = ""
            if status in ("ok", "partial") and settings(db, sid)["evaluate"] and not handle.cancel.is_set():
                _upd(db, rid, status="evaluating", message="Depot-Abrechnungen werden ausgewertet …")
                try:
                    ev = evaluate_pending(ctx, sid, account, root_dir(ctx, sid, name), handle.cancel)
                except Exception as e:  # pragma: no cover - Absicherung
                    log.exception("DKB: Auswertung fehlgeschlagen")
                    ev = {"error": f"Auswertung fehlgeschlagen ({type(e).__name__})."}
                summary["evaluated"] = ev
                if ev.get("error"):
                    summary["errors"].append(ev["error"])
                    status = "partial"
    except Exception as e:  # pragma: no cover - Absicherung: der Hintergrundlauf darf nie hängen bleiben
        log.exception("DKB-Abruf %s fehlgeschlagen", sid)
        status, message = "error", f"Abruf fehlgeschlagen ({type(e).__name__})."
    finally:
        if handle.cancel.is_set() and status not in ("error",):
            status = "cancelled"
            message = "Abgebrochen – bereits geladene Dokumente bleiben abgelegt."
        _upd(db, rid, status=status, message=message[:500], finished_at=_now(),
             summary_json=json.dumps(summary, ensure_ascii=False, default=str))
        _RUNNING.pop(sid, None)
        db.close_thread_conn()


def _fetch(ctx: Any, sid: int, rid: int, username: str, password: str, mfa: int, name: str, handle: Handle,
           summary: dict[str, Any]) -> tuple[str, str]:
    db = ctx.db
    root = root_dir(ctx, sid, name)
    root.mkdir(parents=True, exist_ok=True)
    tmp = Path(ctx.config.data_dir) / "tmp"
    tmp.mkdir(parents=True, exist_ok=True, mode=0o700)
    known_file = tmp / f"dkb-known-{rid}.json"
    known = [r["doc_id"] for r in db.q("SELECT doc_id FROM bank_doc WHERE source_id=?", (sid,))]
    known_file.write_text(json.dumps(known))
    cmd = [*fetcher_command(ctx), "--out", str(root), "--known", str(known_file)]
    tail: deque[str] = deque(maxlen=40)
    error: dict[str, Any] | None = None
    fail_msgs: list[str] = []
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,  # noqa: S603 - fester Aufruf des installierten Plugins, Argumente ohne Shell
                                env=plugins.clean_env(), cwd=str(root), bufsize=1)
    except OSError as e:
        known_file.unlink(missing_ok=True)
        return "error", f"Das Abrufprogramm lässt sich nicht starten ({type(e).__name__}). Bitte neu einrichten."
    handle.proc = proc

    def drain() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            tail.append(line.rstrip()[:300])

    threading.Thread(target=drain, daemon=True).start()
    timer = threading.Timer(RUN_TIMEOUT, lambda: proc.poll() is None and proc.kill())
    timer.daemon = True
    timer.start()
    try:
        assert proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write(json.dumps({"user": username, "password": password, "mfa_device": mfa or None}) + "\n")
            proc.stdin.flush()
            proc.stdin.close()
        except OSError:
            pass
        total = done = 0
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("event")
            if kind == "login":
                _upd(db, rid, status="running", message="Anmeldung bei der DKB …")
            elif kind == "awaiting_2fa":
                _upd(db, rid, status="awaiting_2fa",
                     message="Bitte die Anmeldung in der DKB-App freigeben (Wartezeit etwa "
                             f"{ev.get('timeout_s', 50)} s).")
            elif kind == "logged_in":
                _upd(db, rid, status="fetching", message="Angemeldet – Dokumentenliste wird gelesen …")
            elif kind == "listed":
                total = int(ev.get("new") or 0)
                _upd(db, rid, status="fetching", total=total,
                     message=f"{total} neue Dokumente von {ev.get('total', '?')} in der Postbox.")
            elif kind == "document":
                ok = _record(db, sid, rid, root, ev, summary)
                done += 1
                if not ok:
                    fail_msgs.append(f"{ev.get('id')}: Pfad/Datei ungültig")
                _upd(db, rid, done=done, message=f"Dokument {done} von {total} geladen …")
            elif kind == "document_error":
                summary["failed"] += 1
                fail_msgs.append(f"{ev.get('id')}: {sanitize_error(str(ev.get('message', '')), (password, username))}")
            elif kind == "done":
                summary["failed"] = max(summary["failed"], int(ev.get("failed") or 0))
            elif kind == "error":
                error = ev
        rc = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:  # pragma: no cover
        proc.kill()
        rc = -9
    finally:
        timer.cancel()
        known_file.unlink(missing_ok=True)
        handle.proc = None
    summary["errors"] += fail_msgs[:10]
    if error is not None:
        kind = str(error.get("kind") or "other")
        detail = sanitize_error(str(error.get("message") or ""), (password, username))
        lines = [sanitize_error(x, (password, username)) for x in list(tail)[-8:]]
        summary["diagnose"] = {"kind": kind, "detail": detail, "stderr": [x for x in lines if x],
                               "fetcher": str(error.get("fetcher") or "")}
        return "error", explain(kind, detail)
    if rc not in (0,) and not handle.cancel.is_set():
        last = sanitize_error(" | ".join(list(tail)[-3:]), (password, username))
        return "error", KIND_TEXT["other"] + (f" ({last[:200]})" if last else "")
    n = summary["downloaded"]
    msg = f"{n} neue Dokumente geladen, {summary['existing']} bereits vorhanden"
    if summary["failed"]:
        return "partial", msg + f", {summary['failed']} fehlgeschlagen."
    return "ok", msg + "."


def _record(db: Any, sid: int, rid: int, root: Path, ev: dict[str, Any], summary: dict[str, Any]) -> bool:
    """Eine Meldung „document“ in den Index übernehmen – Pfad und Prüfsumme prüft Portfolia selbst."""
    path = _inside(root, str(ev.get("rel") or ""))
    doc_id = str(ev.get("id") or "")
    if path is None or not doc_id:
        summary["failed"] += 1
        return False
    sha, size = _sha(path)
    category = safe_name(str(ev.get("category") or ""), "Sonstiges")
    doctype = str(ev.get("doctype") or "") or None
    st = "downloaded" if doctype in EVAL_TYPES else "archived"
    cur = db.x("INSERT OR IGNORE INTO bank_doc(source_id, doc_id, category, doctype, account, doc_date, subject, "
               "rel_path, sha256, size, checksum_ok, status, run_id, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (sid, doc_id, category, doctype, str(ev.get("account") or "")[:80], str(ev.get("date") or "")[:10],
                str(ev.get("subject") or "")[:200], str(ev["rel"]), sha, size,
                0 if ev.get("checksum_ok") is False else 1, st, rid, _now()))
    if cur.rowcount:
        summary["existing" if ev.get("status") == "exists" else "downloaded"] += 1
        summary["categories"][category] = summary["categories"].get(category, 0) + 1
    return True


# -- Auswertung der Depot-Abrechnungen -----------------------------------------------------------------------------

def evaluate_pending(ctx: Any, sid: int, account: str, root: Path, cancel: threading.Event | None = None
                     ) -> dict[str, Any]:
    """Noch nicht ausgewertete Depot-Abrechnungen durch den Dokumentimport schicken (→
    Prüf-Stapel). Gebucht wird nichts."""
    from app.documentimport.service import MAX_FILES, document_service

    db = ctx.db
    cancel = cancel or threading.Event()
    rows = db.q("SELECT * FROM bank_doc WHERE source_id=? AND status='downloaded' ORDER BY doc_date, id", (sid,))
    out: dict[str, Any] = {"documents": len(rows), "evaluated": 0, "known": 0, "failed": 0, "transactions": 0,
                           "batches": []}
    svc = document_service(ctx)
    for i in range(0, len(rows), MAX_FILES):
        if cancel.is_set():
            out["error"] = "Auswertung abgebrochen."
            break
        part = rows[i:i + MAX_FILES]
        files: list[tuple[str | None, bytes]] = []
        for r in part:
            p = _inside(root, r["rel_path"])
            if p is None:
                db.x("UPDATE bank_doc SET status='error', note=? WHERE id=?", ("Datei fehlt im Ordner", r["id"]))
                out["failed"] += 1
                continue
            files.append((p.name, p.read_bytes()))
        if not files:
            continue
        acc = svc.accept(files, account=account)
        if acc.stack_id is not None:
            started = svc.start(acc.stack_id)
            if started.get("error"):
                out["error"] = str(started["error"])
                break
            end = time.monotonic() + EVAL_TIMEOUT
            st = svc.stack(acc.stack_id)
            while st is not None and st["status"] in ("queued", "running") and time.monotonic() < end:
                if cancel.is_set():
                    svc.cancel(acc.stack_id)
                time.sleep(0.3)
                st = svc.stack(acc.stack_id)
            if st is not None and st["status"] == "done":
                summ = json.loads(st["summary_json"] or "{}")
                out["transactions"] += int(summ.get("transactions") or 0)
                out["batches"] += [b for b in summ.get("batches") or [] if b not in out["batches"]]
            elif st is None or st["status"] in ("queued", "running"):
                out["error"] = "Die Auswertung dauert zu lange – sie läuft im Dokumentimport weiter."
                break
        for r in part:
            d = db.q1("SELECT status, error, batch_id FROM document WHERE sha256=?", (r["sha256"],))
            if d is None:
                continue
            if d["status"] in ("analysed", "staged"):
                db.x("UPDATE bank_doc SET status='evaluated', stack_id=?, note=NULL WHERE id=?",
                     (acc.stack_id, r["id"]))
                out["evaluated" if acc.stack_id else "known"] += 1
            elif d["status"] == "failed":
                db.x("UPDATE bank_doc SET status='error', note=?, stack_id=? WHERE id=?",
                     (str(d["error"] or "nicht lesbar")[:200], acc.stack_id, r["id"]))
                out["failed"] += 1
    return out
