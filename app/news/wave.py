"""Manuelle Pipeline-Welle: Import → Artikeltexte → KI-Analyse → Gruppierung für die letzten 24 Stunden.

Ein Klick erzeugt eine Welle mit Zähler je Stufe („n von m“). Der Zustand liegt in ``app_state`` und übersteht damit
Seitenwechsel und Neustart; eine Welle ohne Lebenszeichen gilt als abgebrochen. Die Stufen laufen nacheinander
(echte Verkettung statt fester Verzögerungen) und nehmen jeweils die Sperre der Stufe (``locks``), so dass der
Intervall-Job nicht parallel dieselben Daten bearbeitet.

Annahmen/Grenzen: Gezählt wird, was beim Start der Stufe wartet (Nenner fix). Artikel pausierter Quellen und geplante
Wiederholungen zählen nicht mit (sie stehen im Hinweis der Stufe). Hält der Anbieter an (Minuten-/Tageslimit, Pause),
endet die Stufe als „wartet“ – der Intervall-Job arbeitet danach weiter, die Welle startet nicht erneut.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.context import AppContext
from app.news.analysis import AnalysisService
from app.news.enrich import EnrichService
from app.news.events import EventService
from app.news.locks import exclusive, is_busy
from app.util.timeutil import iso, parse_iso

log = logging.getLogger(__name__)

STATE_KEY = "news.wave"
WINDOW_H = 24
STALE_AFTER_S = 20 * 60  # so lange darf eine laufende Welle ohne Lebenszeichen bleiben (ein Import kann dauern)
LOCK_WAIT_S = 15 * 60  # so lange wartet die Welle auf einen laufenden Intervall-Lauf derselben Stufe
STAGE_MAX_S = 45 * 60  # Obergrenze je Stufe (Artikeltexte/Analyse); danach endet sie als „wartet“
MAX_ROUNDS = 40
STAGES = (("fetch", "Import"), ("enrich", "Artikeltexte"), ("analyze", "KI-Analyse"), ("group", "Gruppierung"))
ACTIVE = ("waiting", "running")


def _now() -> str:
    return iso(datetime.now(UTC))


def _new_state(fetch: bool) -> dict[str, Any]:
    now = _now()
    stages = []
    for key, label in STAGES:
        st = {"key": key, "label": label, "status": "waiting", "done": 0, "total": 0, "note": ""}
        if key == "fetch" and not fetch:
            st.update(status="skipped", note="nicht angefordert")
        stages.append(st)
    return {"id": now, "started_at": now, "updated_at": now, "finished_at": None, "status": "running",
            "hours": WINDOW_H, "fetch": fetch, "stages": stages}


def reserve(ctx: AppContext, fetch: bool = True) -> dict[str, Any] | None:
    """Welle vormerken (sofort sichtbar, Doppelklick wird abgefangen). ``None``, wenn schon eine läuft."""
    if is_running(ctx) or is_busy(ctx, "wave"):
        return None
    st = _new_state(fetch)
    st["claimed"] = False
    ctx.db.set_state(STATE_KEY, st)
    return st


def load(ctx: AppContext) -> dict[str, Any] | None:
    """Zustand der letzten Welle; eine laufende ohne Lebenszeichen wird als abgebrochen ausgewiesen."""
    st = ctx.db.get_state(STATE_KEY)
    if not isinstance(st, dict) or not st.get("stages"):
        return None
    if st.get("status") == "running":
        seen = parse_iso(st.get("updated_at") or st.get("started_at"))
        if seen is None or (datetime.now(UTC) - seen).total_seconds() > STALE_AFTER_S:
            st = dict(st, status="aborted", finished_at=st.get("updated_at"))
            st["stages"] = [dict(s, status="error", note="abgebrochen (Neustart oder Absturz)")
                            if s["status"] in ACTIVE else s for s in st["stages"]]
    return st


def is_running(ctx: AppContext) -> bool:
    st = load(ctx)
    return bool(st and st.get("status") == "running")


class WaveRunner:
    """Führt eine Welle aus und schreibt den Zustand fortlaufend (gedrosselt) nach ``app_state``."""

    def __init__(self, ctx: AppContext, fetch_fn: Callable[[], dict[str, Any]] | None,
                 enrich: EnrichService | None = None, analysis: AnalysisService | None = None,
                 events: EventService | None = None, state: dict[str, Any] | None = None) -> None:
        self.ctx = ctx
        self.fetch_fn = fetch_fn
        self.enrich = enrich or EnrichService(ctx)
        self.analysis = analysis or AnalysisService(ctx)
        self.events = events or EventService(ctx)
        self.state = state if state is not None else _new_state(fetch_fn is not None)
        if fetch_fn is None:
            fetch_stage = self._stage("fetch")
            fetch_stage.update(status="skipped", note="nicht angefordert")
        self._last_write = 0.0

    # -- Zustand -----------------------------------------------------------------------------------------
    def _save(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._last_write < 1.0:
            return
        self._last_write = time.monotonic()
        self.state["updated_at"] = _now()
        self.ctx.db.set_state(STATE_KEY, self.state)

    def _stage(self, key: str) -> dict[str, Any]:
        return next(s for s in self.state["stages"] if s["key"] == key)

    def _set(self, key: str, status: str | None = None, note: str | None = None, done: int | None = None,
             total: int | None = None, force: bool = True) -> None:
        st = self._stage(key)
        if status is not None:
            st["status"] = status
        if note is not None:
            st["note"] = note
        if done is not None:
            st["done"] = done
        if total is not None:
            st["total"] = total
        self._save(force)

    def _tick(self, key: str, n: int = 1) -> None:
        st = self._stage(key)
        st["done"] = min(st["total"], st["done"] + n) if st["total"] else st["done"] + n
        self._save()

    # -- Stufen ------------------------------------------------------------------------------------------
    def _fetch(self) -> None:
        if self.fetch_fn is None:
            return
        self._set("fetch", "running", "Quellen werden abgerufen …")
        with exclusive(self.ctx, "fetch", LOCK_WAIT_S) as got:
            if not got:
                self._set("fetch", "error", "Import läuft bereits sehr lange – übersprungen")
                return
            res = self.fetch_fn() or {}
        if res.get("skipped"):
            self._set("fetch", "skipped", str(res["skipped"]))
            return
        new = int(res.get("new", 0) or 0)
        errs = len(res.get("errors") or [])
        self._set("fetch", "done", f"{new} neue Meldungen" + (f", {errs} Quellenfehler" if errs else ""),
                  done=int(res.get("sources", 0) or 0), total=int(res.get("sources", 0) or 0))

    def _enrich(self) -> None:
        if not self.enrich.enabled():
            self._set("enrich", "skipped", "Artikelinhalte deaktiviert")
            return
        total = len(self.enrich.candidates(100_000, hours=WINDOW_H))
        self._set("enrich", "running", "", done=0, total=total)
        deadline = time.monotonic() + STAGE_MAX_S
        stats = {"success": 0, "partial": 0, "blocked": 0, "failed": 0}

        def on_item(status: str) -> None:
            if status in stats:
                stats[status] += 1
            self._tick("enrich")

        for _ in range(MAX_ROUNDS):
            if total == 0 or time.monotonic() > deadline:
                break
            before = self._stage("enrich")["done"]
            res = self.enrich.run(limit=max(40, min(total, 200)), hours=WINDOW_H, wait_s=LOCK_WAIT_S, on_item=on_item)
            if res.get("skipped") or not res.get("selected"):
                break
            if self._stage("enrich")["done"] == before:  # nur pausierte Quellen übrig: keine Endlosschleife
                break
        left = len(self.enrich.candidates(100_000, hours=WINDOW_H))
        paused = len(self.enrich.candidates(100_000, include_paused=True, hours=WINDOW_H)) - left
        retry = self.enrich.retry_planned_count()
        bits = [f"{stats['success'] + stats['partial']} mit Text", f"{stats['blocked']} gesperrt",
                f"{stats['failed']} fehlgeschlagen"]
        if paused:
            bits.append(f"{paused} Artikel pausierter Quellen zurückgestellt")
        if retry:
            bits.append(f"{retry} Wiederholungen geplant")
        st = self._stage("enrich")
        st["done"] = st["total"] if left == 0 or left <= paused else min(st["total"], st["done"])
        self._set("enrich", "done" if left <= paused else "blocked", ", ".join(bits)
                  + ("" if left <= paused else f" · {left} warten noch (Zeitlimit der Stufe)"))

    def _analyze(self) -> None:
        if not self.analysis.enabled():
            self._set("analyze", "skipped", "KI-Analyse ausgeschaltet oder nicht konfiguriert")
            return
        total = self.analysis.backlog(WINDOW_H)
        self._set("analyze", "running", "", done=0, total=total)
        deadline = time.monotonic() + STAGE_MAX_S
        note = ""
        stopped = False
        for _ in range(MAX_ROUNDS):
            if total == 0 or time.monotonic() > deadline:
                break
            before = self._stage("analyze")["done"]
            res = self.analysis.run(hours=WINDOW_H, wait_s=LOCK_WAIT_S, on_batch=lambda n: self._tick("analyze", n))
            if res.get("skipped"):
                break
            if res.get("stopped") or res.get("error"):
                note, stopped = str(res.get("stopped") or res.get("error")), True
                break
            if not res.get("backlog_left"):
                break
            if self._stage("analyze")["done"] == before:
                break
        left = self.analysis.backlog(WINDOW_H)
        st = self._stage("analyze")
        if left == 0:
            st["done"] = st["total"]
        if left and (stopped or time.monotonic() > deadline or st["done"] < st["total"]):
            self._set("analyze", "blocked", f"{left} warten – {note or 'Zeitlimit der Stufe'}; der Intervall-Job "
                                           "arbeitet weiter")
        else:
            self._set("analyze", "done", f"{st['done']} analysiert")

    def _group(self) -> None:
        with exclusive(self.ctx, "group", LOCK_WAIT_S) as got:
            if not got:
                self._set("group", "error", "Gruppierung läuft bereits sehr lange – übersprungen")
                return
            self._set("group", "running", "")
            res = self.events.run() or {}
        if res.get("skipped"):
            self._set("group", "skipped", str(res["skipped"]))
        else:
            self._set("group", "done", "Ereignisse aktualisiert", done=1, total=1)

    # -- Ablauf ------------------------------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        self.state["claimed"] = True
        self._save(force=True)
        for key, fn in (("fetch", self._fetch), ("enrich", self._enrich), ("analyze", self._analyze),
                        ("group", self._group)):
            if self._stage(key)["status"] == "skipped":
                continue
            try:
                fn()
            except Exception as e:  # eine Stufe darf die Welle nie abbrechen; die nächste arbeitet weiter
                log.warning("Welle, Stufe %s: %s", key, e)
                self._set(key, "error", f"{type(e).__name__}: {e}")
        statuses = [s["status"] for s in self.state["stages"]]
        self.state["status"] = ("error" if "error" in statuses else "partial" if "blocked" in statuses else "done")
        self.state["finished_at"] = _now()
        self._save(force=True)
        return {"status": self.state["status"],
                "stages": {s["key"]: f"{s['done']}/{s['total']}" for s in self.state["stages"]}}


def reserved(ctx: AppContext) -> dict[str, Any] | None:
    """Vorgemerkte, noch nicht gestartete Welle (für den Job, der sie übernimmt)."""
    st = ctx.db.get_state(STATE_KEY)
    if isinstance(st, dict) and st.get("status") == "running" and st.get("claimed") is False:
        return st
    return None


__all__ = ["STAGES", "WINDOW_H", "WaveRunner", "is_running", "load", "reserve", "reserved"]
