"""Einheitlicher Fortschritt für alle Synchronisierungen und Importe.

Ein :class:`Progress` beschreibt einen laufenden Vorgang mit festen Phasen

    Vorbereitung → Daten abrufen → Verarbeiten → Abgleichen → Kurse ergänzen → Speichern → Fertig

und liefert daraus einen **monotonen** Gesamtwert in Prozent: Jede Phase hat ein Gewicht; innerhalb einer Phase zählt
``erledigt / gesamt``, bei unbekannter Gesamtzahl eine asymptotische Schätzung (nie 100 % vor dem Abschluss). Der
Wert springt weder zurück noch vorzeitig auf 100 % und bleibt nicht bei 0 % stehen: Schon der Eintritt in eine Phase
zählt die vorherigen Phasen als erledigt. Vorgänge ohne bestimmte Phasen überspringen diese einfach.

Gespeichert wird der Zustand dort, wo er bisher lag (``data_source.progress_json`` für Datenquellen,
``job_status.progress_json`` für Jobs) – immer im selben Format (:meth:`Progress.payload`), dargestellt mit einem
gemeinsamen Baustein (``partials/progress.html``). Ältere Formate (nur ``done``/``total`` bzw. ``stage``) werden von
:func:`view` weiterhin verstanden.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.util.timeutil import iso, parse_iso

# (Schlüssel, Anzeige, Gewicht)
PHASES: list[tuple[str, str, int]] = [
    ("prepare", "Vorbereitung", 5),
    ("fetch", "Daten abrufen", 45),
    ("process", "Verarbeiten", 15),
    ("reconcile", "Abgleichen", 15),
    ("prices", "Kurse ergänzen", 10),
    ("save", "Speichern", 10),
]
PHASE_LABEL = {k: v for k, v, _w in PHASES} | {"done": "Fertig"}
STALE_S = 180  # ohne Lebenszeichen gilt ein „laufender“ Fortschritt als abgebrochen
_WRITE_EVERY_S = 0.8
log = logging.getLogger(__name__)


class Progress:
    """Fortschritt eines Vorgangs. ``sink`` speichert den Zustand (wird gedrosselt aufgerufen)."""

    def __init__(self, sink: Callable[[dict[str, Any]], None], label: str, *, source: str | None = None,
                 unit: str = "Datensätze", phases: list[str] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.sink = sink
        self.label = label
        self.source = source
        self.unit = unit
        keys = phases or [k for k, _l, _w in PHASES]
        self.phases = [(k, PHASE_LABEL[k], w) for k, _l, w in PHASES if k in keys]
        total_w = sum(w for _k, _l, w in self.phases) or 1
        self._weights = {k: w / total_w * 100 for k, _l, w in self.phases}
        self.clock = clock
        self.phase_key: str | None = None
        self.done = 0
        self.total: int | None = None
        self.text = ""
        self.pct = 0.0
        self.started_at = iso(datetime.now(UTC))
        self.extra: dict[str, Any] = {}
        self._last_write = -math.inf
        self._lock = threading.Lock()
        self.finished = False
        self._emit(force=True)

    # -- Fortschreiben ------------------------------------------------------------------------------------
    def phase(self, key: str, total: int | None = None, text: str = "", unit: str | None = None) -> Progress:
        """In Phase ``key`` wechseln (unbekannte Phasen werden ignoriert, Rückschritte nicht angezeigt)."""
        with self._lock:
            if key not in self._weights:
                return self
            keys = [k for k, _l, _w in self.phases]
            if self.phase_key is not None and keys.index(key) < keys.index(self.phase_key):
                return self  # nie rückwärts
            self.phase_key = key
            self.done, self.total, self.text = 0, total, text
            if unit:
                self.unit = unit
            self._recompute()
        self._emit(force=True)
        return self

    def update(self, done: int, total: int | None = None, text: str | None = None) -> None:
        with self._lock:
            self.done = max(0, int(done))
            if total is not None:
                self.total = max(int(total), self.done)
            if text is not None:
                self.text = text[:200]
            self._recompute()
        self._emit()

    def advance(self, n: int = 1, text: str | None = None) -> None:
        self.update(self.done + n, None, text)

    def finish(self, ok: bool = True, result: str = "", **extra: Any) -> None:
        with self._lock:
            self.finished = True
            self.extra.update(extra)
            self.extra.update(ok=ok, result=result[:500], finished_at=iso(datetime.now(UTC)))
            if ok:
                self.pct = 100.0
            self.phase_key = "done" if ok else self.phase_key
        self._emit(force=True)

    # -- Berechnung ---------------------------------------------------------------------------------------
    def _recompute(self) -> None:
        if self.phase_key is None or self.phase_key == "done":
            return
        base = 0.0
        for k, _l, _w in self.phases:
            if k == self.phase_key:
                break
            base += self._weights[k]
        w = self._weights[self.phase_key]
        if self.total:
            frac = min(1.0, self.done / self.total)
        else:
            frac = min(0.9, self.done / (self.done + 200.0)) if self.done else 0.0
        self.pct = max(self.pct, min(99.0, base + w * frac))  # monoton, 100 % erst mit finish()

    def payload(self) -> dict[str, Any]:
        keys = [k for k, _l, _w in self.phases]
        cur = keys.index(self.phase_key) if self.phase_key in keys else (len(keys) if self.phase_key == "done"
                                                                          else -1)
        steps = [{"key": k, "label": lbl, "state": "done" if i < cur else ("active" if i == cur else "open")}
                 for i, (k, lbl, _w) in enumerate(self.phases)]
        label = PHASE_LABEL.get(self.phase_key or "", "Start")
        return {"v": 2, "running": not self.finished, "label": self.label, "source": self.source,
                "phase": self.phase_key, "phase_label": label, "phase_no": max(cur, 0) + 1 if cur >= 0 else 0,
                "phase_count": len(keys), "pct": round(self.pct, 1), "done": self.done, "total": self.total,
                "unit": self.unit, "text": self.text, "stage": label, "steps": steps, "started_at": self.started_at,
                "updated_at": iso(datetime.now(UTC)), **self.extra}

    def _emit(self, force: bool = False) -> None:
        now = self.clock()
        if not force and now - self._last_write < _WRITE_EVERY_S:
            return
        self._last_write = now
        try:
            self.sink(self.payload())
        except Exception as e:  # Anzeige darf den Vorgang nie stören
            log.debug("Fortschritt nicht gespeichert: %s", e)


# -- Speicherorte ---------------------------------------------------------------------------------------

def job_sink(ctx: Any, job: str) -> Callable[[dict[str, Any]], None]:
    """``job_status.progress_json`` (Jobs: Import, Kurse, CSV)."""
    def sink(p: dict[str, Any]) -> None:
        ctx.db.x("INSERT INTO job_status(job, last_start, running, progress_json) VALUES (?,?,?,?) "
                 "ON CONFLICT(job) DO UPDATE SET progress_json=excluded.progress_json, running=excluded.running",
                 (job, p.get("started_at"), 1 if p.get("running") else 0, json.dumps(p, ensure_ascii=False,
                                                                                       default=str)))
    return sink


def job_progress(ctx: Any, job: str, label: str, **kw: Any) -> Progress:
    return Progress(job_sink(ctx, job), label, **kw)


# -- Anzeige --------------------------------------------------------------------------------------------

def view(p: dict[str, Any] | None) -> dict[str, Any]:
    """Fortschritt (auch ältere Formate) → einheitliche Anzeige: Prozent, Zählerzeile, Phase."""
    p = dict(p or {})
    if p.get("v") != 2:  # älteres Format: done/total bzw. stage/text
        done, total = int(p.get("done") or 0), p.get("total")
        pct = (done / total * 100) if total else (min(90.0, done / (done + 200.0) * 100) if done else 5.0)
        p.update(pct=round(min(99.0, pct), 1), phase_label=p.get("stage") or "läuft", unit=p.get("unit") or "",
                 steps=[])
    if not p.get("running") and p.get("ok"):
        p["pct"] = 100.0
    done, total = p.get("done"), p.get("total")
    count = ""
    if total:
        count = f"{_n(done)} / {_n(total)} {p.get('unit') or ''}".strip()
    elif done:
        count = f"{_n(done)} {p.get('unit') or ''}".strip()
    p["count"] = count
    p["pct_int"] = int(p.get("pct") or 0)
    seen = parse_iso(p.get("updated_at"))
    p["stale"] = bool(p.get("running") and seen is not None
                      and (datetime.now(UTC) - seen).total_seconds() > STALE_S)
    return p


def _n(v: Any) -> str:
    try:
        return f"{int(v):,}".replace(",", ".")
    except (TypeError, ValueError):
        return str(v)


JOB_LABELS = {"history_backfill": "Historische Kurse laden", "csv_prices": "Kurse für CSV-Import laden",
              "import": "Import prüfen", "csv_upload": "CSV-Datei einlesen", "csv_commit": "CSV-Import übernehmen"}


def active(ctx: Any) -> list[dict[str, Any]]:
    """Alle laufenden Vorgänge (Jobs mit Fortschritt, Datenquellen) für die globale Anzeige."""
    out: list[dict[str, Any]] = []
    for r in ctx.db.q("SELECT job, running, progress_json FROM job_status WHERE running=1 AND progress_json IS NOT "
                      "NULL"):
        try:
            p = json.loads(r["progress_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(p, dict):
            continue
        p.setdefault("running", True)
        p.setdefault("label", JOB_LABELS.get(r["job"], r["job"]))
        v = view(p)
        if not v["stale"]:
            out.append({**v, "id": f"job:{r['job']}"})
    try:
        rows = ctx.db.q("SELECT id, name, progress_json FROM data_source "
                        "WHERE progress_json LIKE '%\"running\": true%'")
    except Exception:  # Tabelle fehlt (sehr alte Datenbank)
        rows = []
    for r in rows:
        try:
            p = json.loads(r["progress_json"])
        except (TypeError, ValueError):
            continue
        p.setdefault("label", "Synchronisierung")
        p.setdefault("source", r["name"])
        v = view(p)
        if v.get("running") and not v["stale"]:
            out.append({**v, "id": f"ds:{r['id']}", "href": f"/settings/datasources#ds-{r['id']}"})
    return out
