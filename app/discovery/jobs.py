"""Persistente Research-Jobs (``disc_job``): Start per Knopf oder Zeitplan, Fortschritt, Ergebnis, Fehlergrund.

* **Kein Doppelstart:** höchstens ein aktiver Job je *Slot* (Art + Unternehmen) – erzwungen durch einen eindeutigen
  Teilindex in SQLite, also auch bei gleichzeitigen Anfragen, Scheduler und Knopf zugleich.
* **Nicht blockierend:** Der Knopf legt den Job an und übergibt ihn dem Scheduler (APScheduler-Pool); die Seite fragt
  den Zustand per HTMX ab. Ohne Scheduler (Tests, ``scheduler_enabled = false``) läuft der Job im aufrufenden Thread.
* **Warteschlange (0.111):** Reicht das KI-Kontingent nicht (Minuten- oder Tageslimit des Modells, Pause,
  Discovery-Grenze), wartet ein manuell gestarteter Job sichtbar („wartet“, mit Zeitpunkt und Grund) und startet
  danach von selbst; er lässt sich abbrechen. Wartende Jobs überstehen einen Neustart.
* **Neustart:** Jede Instanz hat eine Prozesskennung. Beim Start werden geplante oder laufende Jobs anderer
  Prozesse als „unterbrochen“ markiert (Grund sichtbar); Screening setzt beim nächsten Lauf am gespeicherten
  Fortschritt fort. Laufende Jobs ohne Lebenszeichen seit ``STALE_MIN`` Minuten gelten ebenfalls als unterbrochen.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from app.util.timeutil import iso, parse_iso

log = logging.getLogger(__name__)

KINDS = {"ki_discovery": "KI-Discovery", "screening": "Fundamentalscreening", "voll": "Vollständiger Discovery-Lauf",
         "news": "Nachrichten-Discovery", "analyse": "Unternehmensanalyse",
         "beobachtet": "Aktualisierung beobachteter Unternehmen"}
STATUS = {"geplant": "geplant", "laeuft": "läuft", "abgeschlossen": "abgeschlossen",
          "teilweise": "teilweise abgeschlossen", "uebersprungen": "übersprungen", "fehlgeschlagen": "fehlgeschlagen",
          "kontingent": "Kontingent erschöpft", "ki_fehlt": "KI nicht konfiguriert", "unterbrochen": "unterbrochen",
          "nicht_gestartet": "nicht gestartet", "wartet": "wartet auf KI-Kontingent", "abgebrochen": "abgebrochen"}
ACTIVE = ("geplant", "laeuft", "wartet")  # „wartet“ belegt den Slot weiter (kein Doppelstart während der Wartezeit)
_ACTIVE_SQL = "('geplant', 'laeuft', 'wartet')"
STALE_MIN = 45
WORKER = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
INTERACTIVE = {"analyse"}  # kurze, vom Nutzer gestartete Jobs laufen im Pool „interactive“ (Vorrang)

Handler = Callable[[Any, "Job"], tuple[str, dict[str, Any]]]
HANDLERS: dict[str, Handler] = {}


def handler(kind: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn
    return deco


def _now() -> str:
    return iso(datetime.now(UTC)) or ""


def slot(kind: str, company_id: int | None) -> str:
    return f"{kind}:{company_id or 0}"


class Job:
    """Laufender Job: Fortschritt und Lebenszeichen werden sofort gespeichert (für die Statusanzeige)."""

    def __init__(self, ctx: Any, row: dict[str, Any]) -> None:
        self.ctx, self.db = ctx, ctx.db
        self.id = int(row["id"])
        self.kind = str(row["kind"])
        self.trigger = str(row["trigger"])
        self.company_id = row.get("company_id")
        self.params: dict[str, Any] = json.loads(row.get("params_json") or "{}")
        self.progress: dict[str, Any] = json.loads(row.get("progress_json") or "{}")

    def step(self, text: str, **progress: Any) -> None:
        self.progress.update(progress)
        self.db.x("UPDATE disc_job SET step=?, progress_json=?, heartbeat_at=? WHERE id=?",
                  (text[:200], json.dumps(self.progress, ensure_ascii=False, default=str), _now(), self.id))

    def update(self, **progress: Any) -> None:
        self.progress.update(progress)
        self.db.x("UPDATE disc_job SET progress_json=?, heartbeat_at=? WHERE id=?",
                  (json.dumps(self.progress, ensure_ascii=False, default=str), _now(), self.id))


def get(db: Any, job_id: int) -> dict[str, Any] | None:
    r = db.q1("SELECT * FROM disc_job WHERE id=?", (job_id,))
    return _view(dict(r)) if r else None


def _view(d: dict[str, Any]) -> dict[str, Any]:
    d["progress"] = json.loads(d.get("progress_json") or "{}")
    d["result"] = json.loads(d.get("result_json") or "{}")
    d["params"] = json.loads(d.get("params_json") or "{}")
    d["kind_label"] = KINDS.get(d["kind"], d["kind"])
    d["status_label"] = STATUS.get(d["status"], d["status"])
    d["active"] = d["status"] in ACTIVE
    d["waiting"] = d["status"] == "wartet"
    return d


def create(ctx: Any, kind: str, *, trigger: str = "manuell", company_id: int | None = None,
           params: dict[str, Any] | None = None) -> tuple[int, bool]:
    """Job anlegen; läuft schon einer im selben Slot, dessen ID mit ``False`` (kein neuer Job)."""
    if kind not in KINDS:
        raise ValueError(kind)
    s = slot(kind, company_id)
    try:
        cur = ctx.db.x("INSERT INTO disc_job(kind, trigger, company_id, slot, params_json, status, worker, created_at) "
                       "VALUES (?,?,?,?,?,?,?,?)", (kind, trigger, company_id, s,
                                                    json.dumps(params or {}, ensure_ascii=False), "geplant", WORKER,
                                                    _now()))
        return int(cur.lastrowid or 0), True
    except sqlite3.IntegrityError:
        r = ctx.db.q1(f"SELECT id FROM disc_job WHERE slot=? AND status IN {_ACTIVE_SQL} "
                      "ORDER BY id DESC LIMIT 1", (s,))
        if r is None:  # pragma: no cover – Wettlauf: der andere Job ist inzwischen fertig
            return create(ctx, kind, trigger=trigger, company_id=company_id, params=params)
        return int(r["id"]), False


def dispatch(ctx: Any, job_id: int, kind: str) -> str:
    """An den Scheduler übergeben (nicht blockierend) – ohne Scheduler synchron ausführen."""
    sched = getattr(ctx, "scheduler", None)
    name = "discovery_task_interactive" if kind in INTERACTIVE else "discovery_task"
    if sched is not None and name in getattr(sched, "jobs", {}) and sched.trigger(name, 0.3, job_id=job_id):
        return "scheduler"
    run(ctx, job_id)
    return "sync"


def start(ctx: Any, kind: str, *, trigger: str = "manuell", company_id: int | None = None,
          params: dict[str, Any] | None = None, inline: bool = False) -> dict[str, Any]:
    """Job anlegen und starten. ``inline`` (Zeitplan): im aufrufenden Scheduler-Thread ausführen."""
    jid, new = create(ctx, kind, trigger=trigger, company_id=company_id, params=params)
    if new:
        if inline:
            run(ctx, jid)
        else:
            dispatch(ctx, jid, kind)
    return {"id": jid, "new": new}


def run(ctx: Any, job_id: int) -> dict[str, Any] | None:
    """Job ausführen (genau einmal: nur aus „geplant“ heraus). Manuell gestartete Jobs rufen die KI mit Vorrang auf.

    Gibt der Ablauf „wartet“ zurück (``_wait_until``: KI-Grenze oder Pause), bleibt der Job sichtbar in der
    Warteschlange und startet nach Ablauf der Wartezeit erneut (:func:`wake_waiting`)."""
    from app.news.ai import manual_priority

    db = ctx.db
    now = _now()
    if not db.x("UPDATE disc_job SET status='laeuft', started_at=?, heartbeat_at=?, worker=? WHERE id=? AND "
                "status='geplant'", (now, now, WORKER, job_id)).rowcount:
        return get(db, job_id)
    row = dict(db.q1("SELECT * FROM disc_job WHERE id=?", (job_id,)))
    job = Job(ctx, row)
    fn = HANDLERS.get(job.kind)
    result: dict[str, Any] = {}
    status, err_cls, err = "fehlgeschlagen", "", ""
    try:
        if fn is None:
            raise RuntimeError(f"keine Ausführung für {job.kind} registriert")
        if job.trigger == "manuell":
            with manual_priority():
                status, result = fn(ctx, job)
        else:
            status, result = fn(ctx, job)
        err = str(result.pop("_error", "") or "")
        err_cls = str(result.pop("_error_class", "") or "")
    except Exception as e:  # Job-Grenze: Fehler sichtbar im Protokoll und in der Oberfläche, nie still
        log.exception("Discovery-Job %s (%s) fehlgeschlagen", job_id, job.kind)
        status, err_cls, err = "fehlgeschlagen", type(e).__name__, _safe(str(e) or repr(e))
    wait_until = str(result.pop("_wait_until", "") or "")
    if status == "wartet":
        if not wait_until:  # ohne Zeitpunkt kein Warten (sonst hinge der Job unbegrenzt)
            status = "kontingent"
        else:
            db.x("UPDATE disc_job SET status='wartet', wait_until=?, wait_reason=?, result_json=?, progress_json=?, "
                 "error_class=?, error=NULL, step=?, heartbeat_at=? WHERE id=?",
                 (wait_until, err[:300] or None, json.dumps(result, ensure_ascii=False, default=str),
                  json.dumps(job.progress, ensure_ascii=False, default=str), err_cls or None,
                  f"wartet bis {wait_until}"[:200], _now(), job_id))
            return get(db, job_id)
    db.x("UPDATE disc_job SET status=?, result_json=?, progress_json=?, error_class=?, error=?, finished_at=?, "
         "heartbeat_at=? WHERE id=?", (status, json.dumps(result, ensure_ascii=False, default=str),
                                       json.dumps(job.progress, ensure_ascii=False, default=str), err_cls or None,
                                       err[:500] or None, _now(), _now(), job_id))
    return get(db, job_id)


def _safe(text: str) -> str:
    """Fehlertext ohne Schlüssel/Token (Meldungen von Bibliotheken können URLs mit Parametern enthalten)."""
    import re

    return re.sub(r"(?i)(key|token|secret|password)=[^&\s]+", r"\1=***", text)[:300]


def wait(until: Any, reason: str, scope: str = "", **result: Any) -> tuple[str, dict[str, Any]]:
    """Ergebnis eines Ablaufs, der auf KI-Kontingent wartet (für Handler)."""
    return "wartet", {**result, "_wait_until": iso(until) if isinstance(until, datetime) else str(until or ""),
                      "_error": reason, "_error_class": scope}


def wake_waiting(ctx: Any, limit: int = 5) -> list[int]:
    """Wartende Jobs, deren Wartezeit vorbei ist, wieder einplanen – manuell gestartete zuerst, dann nach Alter."""
    now = _now()
    rows = ctx.db.q("SELECT id, kind FROM disc_job WHERE status='wartet' AND wait_until<=? "
                    "ORDER BY (trigger='manuell') DESC, id LIMIT ?", (now, limit))
    woke = []
    for r in rows:
        if ctx.db.x("UPDATE disc_job SET status='geplant', wait_until=NULL, step=? WHERE id=? AND status='wartet'",
                    ("Wartezeit vorbei – startet erneut", r["id"])).rowcount:
            woke.append(int(r["id"]))
            dispatch(ctx, int(r["id"]), str(r["kind"]))
    return woke


def cancel(ctx: Any, job_id: int) -> bool:
    """Wartenden oder noch nicht gestarteten Job abbrechen (ein laufender Job läuft zu Ende)."""
    return bool(ctx.db.x("UPDATE disc_job SET status='abgebrochen', finished_at=?, error_class='abgebrochen', "
                         "error='vom Nutzer abgebrochen' || COALESCE(' (wartete: ' || wait_reason || ')', ''), "
                         "wait_until=NULL WHERE id=? AND "
                         "status IN ('wartet', 'geplant')", (_now(), job_id)).rowcount)


def waiting(db: Any) -> list[dict[str, Any]]:
    return [_view(dict(r)) for r in db.q("SELECT * FROM disc_job WHERE status='wartet' "
                                         "ORDER BY (trigger='manuell') DESC, wait_until, id")]


def recover(ctx: Any) -> int:
    """Nach Neustart: Jobs anderer Prozesse, die nicht abgeschlossen wurden, als „unterbrochen“ markieren."""
    n = ctx.db.x("UPDATE disc_job SET status='unterbrochen', finished_at=?, error_class='neustart', error=? "
                 "WHERE status IN ('geplant', 'laeuft') AND COALESCE(worker, '') != ?",
                 (_now(), "Anwendung wurde neu gestartet, bevor der Job beendet war – erneut starten setzt fort",
                  WORKER)).rowcount
    if n:
        log.warning("Discovery: %d unterbrochene Job(s) nach Neustart markiert", n)
    return int(n or 0)


def expire_stale(ctx: Any) -> int:
    """Laufende Jobs ohne Lebenszeichen seit ``STALE_MIN`` Minuten gelten als unterbrochen."""
    limit = datetime.now(UTC) - timedelta(minutes=STALE_MIN)
    n = 0
    for r in ctx.db.q("SELECT id, heartbeat_at, started_at FROM disc_job WHERE status='laeuft'"):
        hb = parse_iso(r["heartbeat_at"] or r["started_at"])
        if hb is not None and hb < limit:
            n += ctx.db.x("UPDATE disc_job SET status='unterbrochen', finished_at=?, "
                          "error_class='zeitueberschreitung', error=? WHERE id=? AND status='laeuft'",
                          (_now(), f"kein Lebenszeichen seit {STALE_MIN} Minuten", r["id"])).rowcount or 0
    return n


def active(db: Any, company_id: int | None = None) -> list[dict[str, Any]]:
    q = f"SELECT * FROM disc_job WHERE status IN {_ACTIVE_SQL}"
    args: tuple[Any, ...] = ()
    if company_id is not None:
        q += " AND company_id=?"
        args = (company_id,)
    return [_view(dict(r)) for r in db.q(q + " ORDER BY id DESC", args)]


def recent(db: Any, limit: int = 15, company_id: int | None = None) -> list[dict[str, Any]]:
    q, args = "SELECT * FROM disc_job", ()  # type: tuple[str, tuple[Any, ...]]
    if company_id is not None:
        q, args = q + " WHERE company_id=?", (company_id,)
    return [_view(dict(r)) for r in db.q(q + " ORDER BY id DESC LIMIT ?", (*args, limit))]


def last_by_kind(db: Any) -> dict[str, dict[str, Any]]:
    out = {}
    for k in KINDS:
        r = db.q1("SELECT * FROM disc_job WHERE kind=? ORDER BY id DESC LIMIT 1", (k,))
        if r is not None:
            out[k] = _view(dict(r))
    return out


def calls(db: Any, *, job_id: int | None = None, company_id: int | None = None, limit: int = 40
          ) -> list[dict[str, Any]]:
    q, args = "SELECT * FROM disc_ai_call WHERE 1=1", []  # type: tuple[str, list[Any]]
    if job_id is not None:
        q += " AND job_id=?"
        args.append(job_id)
    if company_id is not None:
        q += " AND company_id=?"
        args.append(company_id)
    return [dict(r) for r in db.q(q + " ORDER BY id DESC LIMIT ?", (*args, limit))]
