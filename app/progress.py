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
# Zusätzliche Phasen (nur auf ausdrückliche Wahl, z. B. Dokumentimport M25); Standard bleibt ``PHASES``
EXTRA_PHASES: list[tuple[str, str, int]] = [
    ("analyze", "Dokument analysieren", 5),
    ("extract", "Text extrahieren", 20),
    ("ocr", "OCR durchführen", 30),
    ("detect", "Transaktionen erkennen", 10),
    ("research", "Fehlende Daten recherchieren", 15),
    ("match", "Bestehende Buchungen abgleichen", 10),
    ("propose", "Vorschläge vorbereiten", 10),
    # Prüflauf der Datenqualität (app.diagnosis.checkrun)
    ("chk_stapel", "Prüfstapel neu abgleichen", 15),
    ("chk_diag", "Diagnose berechnen", 20),
    ("chk_integrity", "Integritätsprüfung", 30),
    ("chk_clarify", "Fälle untersuchen", 40),
    ("chk_plan", "Arbeitsplan erstellen", 10),
    # ZIP-Import der DKB-Wertpapierdokumente (app.datasources.dkb_run)
    ("zip_unpack", "ZIP entpacken und ablegen", 15),
    ("zip_eval", "Dokumente erkennen und auswerten", 70),
    ("zip_match", "Buchungen zuordnen", 10),
    ("zip_report", "Bericht erstellen", 5),
]
PHASE_LABEL = {k: v for k, v, _w in PHASES + EXTRA_PHASES} | {"done": "Fertig"}
STALE_S = 180  # ohne Lebenszeichen gilt ein „laufender“ Fortschritt als abgebrochen
_WRITE_EVERY_S = 0.8
ETA_HALF_LIFE_S = 20.0  # Glättung der Geschwindigkeit (exponentiell)
ETA_MIN_ITEMS = 3  # Restzeit erst nach so vielen verarbeiteten Einheiten …
ETA_MIN_SECONDS = 10.0  # … oder so vielen Sekunden Laufzeit
log = logging.getLogger(__name__)


class Progress:
    """Fortschritt eines Vorgangs. ``sink`` speichert den Zustand (wird gedrosselt aufgerufen)."""

    def __init__(self, sink: Callable[[dict[str, Any]], None], label: str, *, source: str | None = None,
                 unit: str = "Datensätze", phases: list[str] | None = None,
                 clock: Callable[[], float] = time.monotonic, eta: bool = False) -> None:
        self.sink = sink
        self.label = label
        self.source = source
        self.unit = unit
        keys = phases or [k for k, _l, _w in PHASES]
        table = {k: w for k, _l, w in PHASES + EXTRA_PHASES}
        self.phases = [(k, PHASE_LABEL[k], table[k]) for k in keys if k in table] if phases else \
            [(k, PHASE_LABEL[k], w) for k, _l, w in PHASES if k in keys]
        total_w = sum(w for _k, _l, w in self.phases) or 1
        self._weights = {k: w / total_w * 100 for k, _l, w in self.phases}
        self.clock = clock
        self._eta_on = eta
        self._t0 = clock()
        self._expect: dict[str, float] = {}  # geplante Arbeit je Phase (Einheiten, z. B. Bytes oder Dateien)
        self._hint: dict[str, float] = {}  # Startschätzung der Geschwindigkeit (Einheiten je Sekunde)
        self._rate: dict[str, float] = {}  # geglättete Geschwindigkeit je Phase
        self._avg: dict[str, float] = {}  # Durchschnitt über die abgeschlossene Phase
        self._work = 0.0  # erledigte Arbeit der laufenden Phase
        self._work_total: float | None = None
        self._sample_t = self._t0
        self._sample_w = 0.0
        self._phase_t = self._t0
        self._items_before = 0  # in abgeschlossenen Phasen verarbeitete Einheiten
        self._eta_prev: tuple[float, float] | None = None  # (zuletzt gezeigt, Zeitpunkt)
        self.phase_key: str | None = None
        self.done = 0
        self.total: int | None = None
        self.text = ""
        self.pct = 0.0
        self.started_at = iso(datetime.now(UTC))
        self.extra: dict[str, Any] = {}
        self._last_write = -math.inf
        self._lock = threading.Lock()
        self._emit_lock = threading.Lock()  # Berechnen und Speichern einer Meldung ist unteilbar (siehe ``_emit``)
        self.finished = False
        self._emit(force=True)

    # -- Fortschreiben ------------------------------------------------------------------------------------
    def expect(self, key: str, work: float, rate: float | None = None) -> Progress:
        """Für die Restzeit: geplante Arbeit einer (auch noch nicht begonnenen) Phase in beliebigen Einheiten
        (z. B. Bytes) und Startschätzung der Geschwindigkeit in Einheiten je Sekunde."""
        with self._lock:
            self._expect[key] = max(float(work), 0.0)
            if rate:
                self._hint[key] = float(rate)
        return self

    def phase(self, key: str, total: int | None = None, text: str = "", unit: str | None = None,
              work_total: float | None = None) -> Progress:
        """In Phase ``key`` wechseln (unbekannte Phasen werden ignoriert, Rückschritte nicht angezeigt)."""
        with self._lock:
            if key not in self._weights:
                return self
            keys = [k for k, _l, _w in self.phases]
            if self.phase_key is not None and keys.index(key) < keys.index(self.phase_key):
                return self  # nie rückwärts
            now = self.clock()
            if self.phase_key in self._weights and self.phase_key != key:
                self._close_phase(now)
            self.phase_key = key
            self.done, self.total, self.text = 0, total, text
            if unit:
                self.unit = unit
            self._work, self._sample_w, self._sample_t, self._phase_t = 0.0, 0.0, now, now
            self._work_total = work_total if work_total is not None else self._expect.get(key, total)
            self._recompute()
        self._emit(force=True)
        return self

    def _close_phase(self, now: float) -> None:
        key = self.phase_key or ""
        span = now - self._phase_t
        if self._work > 0 and span > 0:
            self._avg[key] = self._work / span
        self._items_before += self.done

    def update(self, done: int, total: int | None = None, text: str | None = None, work: float | None = None) -> None:
        """``work``: erledigte Arbeit in den Einheiten von :meth:`expect` (Standard: ``done``) – für die Restzeit."""
        with self._lock:
            self.done = max(0, int(done))
            if total is not None:
                self.total = max(int(total), self.done)
                if self.phase_key not in self._expect and work is None:
                    self._work_total = self.total
            if text is not None:
                self.text = text[:200]
            self._work = float(work) if work is not None else float(self.done)
            self._sample()
            self._recompute()
        self._emit()

    def advance(self, n: int = 1, text: str | None = None, work: float | None = None) -> None:
        self.update(self.done + n, None, text, None if work is None else self._work + work)

    def touch(self) -> None:
        """Lebenszeichen ohne Fortschritt (lange Einzelschritte): schreibt den Zustand neu."""
        self._emit(force=True)

    def _sample(self) -> None:
        now = self.clock()
        dt = now - self._sample_t
        if dt < 0.25:
            return
        inst = max(self._work - self._sample_w, 0.0) / dt
        key = self.phase_key or ""
        prev = self._rate.get(key)
        a = 1.0 - 0.5 ** (dt / ETA_HALF_LIFE_S)
        self._rate[key] = inst if prev is None else prev + a * (inst - prev)
        self._sample_t, self._sample_w = now, self._work

    def finish(self, ok: bool = True, result: str = "", **extra: Any) -> None:
        with self._lock:
            self.finished = True
            now = self.clock()
            if self.phase_key in self._weights:
                self._close_phase(now)
            self.extra.update(extra)
            self.extra.update(ok=ok, result=result[:500], finished_at=iso(datetime.now(UTC)))
            if self._eta_on:
                self.extra["duration_s"] = round(max(now - self._t0, 0.0))
                self.extra["duration_text"] = f"{'Fertig' if ok else 'Beendet'} in {fmt_clock(now - self._t0)} Min."
            if ok:
                self.pct = 100.0
            self.phase_key = "done" if ok else self.phase_key
        self._emit(force=True, final=True)

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
        eta = self._eta_payload()
        return {**eta, "v": 2, "running": not self.finished, "label": self.label, "source": self.source,
                "phase": self.phase_key, "phase_label": label, "phase_no": max(cur, 0) + 1 if cur >= 0 else 0,
                "phase_count": len(keys), "pct": round(self.pct, 1), "done": self.done, "total": self.total,
                "unit": self.unit, "text": self.text, "stage": label, "steps": steps, "started_at": self.started_at,
                "updated_at": iso(datetime.now(UTC)), **self.extra}

    def measured_rates(self) -> dict[str, float]:
        """Gemessene Durchschnittsgeschwindigkeit je abgeschlossener Phase (Einheiten je Sekunde) – als Startschätzung
        für den nächsten Lauf."""
        return {k: round(v, 4) for k, v in self._avg.items() if v > 0}

    def _eta_payload(self) -> dict[str, Any]:
        if not self._eta_on:
            return {}
        now = self.clock()
        elapsed = max(now - self._t0, 0.0)
        out: dict[str, Any] = {"elapsed_s": int(elapsed), "elapsed_text": fmt_clock(elapsed)}
        if self.finished:
            return {**out, "eta_s": None, "eta_text": ""}
        rem = self._remaining(now, elapsed)
        if rem is None:
            return {**out, "eta_s": None, "eta_text": "Restzeit wird berechnet …"}
        if self._eta_prev is not None:  # nicht sprunghaft: höchstens Faktor 2 gegenüber dem fortgeschriebenen Wert
            expected = max(self._eta_prev[0] - (now - self._eta_prev[1]), 1.0)
            rem = min(max(rem, expected / 2), expected * 2)
        rem = max(rem, 1.0)
        self._eta_prev = (rem, now)
        return {**out, "eta_s": round(rem), "eta_text": "noch " + fmt_eta(rem)}

    def _remaining(self, now: float, elapsed: float) -> float | None:
        key = self.phase_key
        if key is None or key == "done" or key not in self._weights:
            return None
        if self._items_before + self.done < ETA_MIN_ITEMS and elapsed < ETA_MIN_SECONDS:
            return None
        total = self._work_total
        rate = self._rate.get(key) or (self._work / max(now - self._phase_t, 1e-9) if self._work > 0 else 0.0) \
            or self._hint.get(key, 0.0)
        if not total or rate <= 0:
            return None
        rem = max(total - self._work, 0.0) / rate
        keys = [k for k, _l, _w in self.phases]
        for k in keys[keys.index(key) + 1:]:  # noch nicht begonnene Phasen: geplante Arbeit ÷ Startschätzung
            w, r = self._expect.get(k), self._hint.get(k) or self._rate.get(k)
            if w and r:
                rem += w / r
        return rem

    def _emit(self, force: bool = False, final: bool = False) -> None:
        """Zustand speichern. Berechnen und Speichern laufen unter einer Sperre: Ein Lebenszeichen (Hintergrund-
        Thread) kann so nicht mit veraltetem „läuft“-Zustand nach dem Abschluss ankommen. Nach dem Abschluss schreibt
        nur noch der Abschluss selbst (``final``)."""
        with self._emit_lock:
            if self.finished and not final:
                return
            now = self.clock()
            if not force and now - self._last_write < _WRITE_EVERY_S:
                return
            self._last_write = now
            try:
                self.sink(self.payload())
            except Exception as e:  # Anzeige darf den Vorgang nie stören
                log.debug("Fortschritt nicht gespeichert: %s", e)


def fmt_clock(seconds: float) -> str:
    """Dauer als ``m:ss`` (ab einer Stunde ``h:mm:ss``)."""
    n = int(max(seconds, 0))
    h, rest = divmod(n, 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def fmt_eta(seconds: float) -> str:
    """Restzeit für die Anzeige: unter 45 s „unter 1 Min.“, bis 5 Min. auf halbe Minuten, bis 1 Std. auf Minuten,
    darüber Stunden und Minuten. Nie 0 und nie negativ."""
    s = max(float(seconds), 1.0)
    if s < 45:
        return "unter 1 Min."
    if s < 300:
        half = max(round(s / 30) / 2, 1.0)
        return "ca. " + (f"{int(half)}" if half == int(half) else f"{half:.1f}".replace(".", ",")) + " Min."
    if s < 3600:
        return f"ca. {round(s / 60)} Min."
    h, m = divmod(round(s / 60), 60)
    return f"ca. {h} Std." + (f" {m} Min." if m else "")


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
