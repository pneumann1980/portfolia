"""Manueller Pipeline-Lauf („Jetzt abarbeiten“): Import → Artikeltexte → KI-Analyse → Gruppierung, sofort, nacheinander.

Der Lauf arbeitet dieselbe Warteschlange ab wie die Intervall-Jobs (gleiche Fenster: Artikeltexte ``news.enrich_days``,
Analyse ``news.analysis_hours``); der Fortschritt steht deshalb in denselben Balken (``app.news.queue``). Hier wird nur
festgehalten, welche Stufe der manuelle Lauf gerade bearbeitet und wie er endete (Anzeige „läuft (manuell)“,
„angestoßen“, „wartet auf Anbieter“). Die Stufen nehmen die Sperre der Stufe (``locks``), so dass Intervall-Job und
manueller Lauf nie gleichzeitig dieselben Daten bearbeiten.

Annahmen/Grenzen: Hält der Anbieter an (Minuten-/Tageslimit, Pause), endet die Analyse-Stufe als „wartet auf
Anbieter“; der Intervall-Job arbeitet danach weiter, der manuelle Lauf startet nicht erneut. Obergrenze je Stufe
``STAGE_MAX_S``. Ein Lauf ohne Lebenszeichen (Neustart, Absturz) gilt nach ``STALE_AFTER_S`` als abgebrochen.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.context import AppContext
from app.news import queue
from app.news.analysis import AnalysisService
from app.news.enrich import EnrichService
from app.news.events import EventService
from app.news.locks import exclusive, is_busy
from app.util.timeutil import iso, parse_iso

log = logging.getLogger(__name__)

STATE_KEY = "news.wave"
STALE_AFTER_S = 20 * 60  # so lange darf ein laufender Lauf ohne Lebenszeichen bleiben (ein Import kann dauern)
LOCK_WAIT_S = 15 * 60  # so lange wartet der Lauf auf einen laufenden Intervall-Lauf derselben Stufe
STAGE_MAX_S = 45 * 60  # Obergrenze je Stufe (Artikeltexte/Analyse); danach übernimmt der Intervall-Job
MAX_ROUNDS = 40
STAGES = (("fetch", "Import"), ("enrich", "Artikeltexte"), ("analyze", "KI-Analyse"), ("group", "Gruppierung"))
ACTIVE = ("waiting", "running")


def _now() -> str:
    return iso(datetime.now(UTC))


def _new_state(fetch: bool) -> dict[str, Any]:
    now = _now()
    stages = {key: {"status": "waiting", "note": ""} for key, _ in STAGES}
    if not fetch:
        stages["fetch"] = {"status": "skipped", "note": "ohne Import"}
    return {"started_at": now, "updated_at": now, "finished_at": None, "status": "running", "fetch": fetch,
            "stage": None, "stages": stages, "claimed": False}


def load(ctx: AppContext) -> dict[str, Any] | None:
    """Zustand des letzten manuellen Laufs; ein laufender ohne Lebenszeichen wird als abgebrochen ausgewiesen."""
    st = ctx.db.get_state(STATE_KEY)
    if not isinstance(st, dict) or not isinstance(st.get("stages"), dict):
        return None
    if st.get("status") == "running":
        seen = parse_iso(st.get("updated_at") or st.get("started_at"))
        if seen is None or (datetime.now(UTC) - seen).total_seconds() > STALE_AFTER_S:
            st = dict(st, status="aborted", finished_at=st.get("updated_at"), stage=None)
            st["stages"] = {k: ({"status": "error", "note": "abgebrochen (Neustart oder Absturz)"}
                                if v.get("status") in ACTIVE else v) for k, v in st["stages"].items()}
    return st


def is_running(ctx: AppContext) -> bool:
    st = load(ctx)
    return bool(st and st.get("status") == "running")


def reserve(ctx: AppContext, fetch: bool = True) -> dict[str, Any] | None:
    """Lauf vormerken (sofort sichtbar, Doppelklick wird abgefangen). ``None``, wenn schon einer läuft."""
    if is_running(ctx) or is_busy(ctx, "wave"):
        return None
    st = _new_state(fetch)
    ctx.db.set_state(STATE_KEY, st)
    return st


def reserved(ctx: AppContext) -> dict[str, Any] | None:
    """Vorgemerkter, noch nicht gestarteter Lauf (für den Job, der ihn übernimmt)."""
    st = ctx.db.get_state(STATE_KEY)
    if isinstance(st, dict) and st.get("status") == "running" and st.get("claimed") is False:
        return st
    return None


class WaveRunner:
    """Führt einen manuellen Lauf aus und schreibt Stufe/Ergebnis (gedrosselt) nach ``app_state``."""

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
            self.state["stages"]["fetch"] = {"status": "skipped", "note": "ohne Import"}
        self._last_write = 0.0

    # -- Zustand -----------------------------------------------------------------------------------------
    def _save(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._last_write < 2.0:
            return
        self._last_write = time.monotonic()
        self.state["updated_at"] = _now()
        self.ctx.db.set_state(STATE_KEY, self.state)

    def _set(self, key: str, status: str, note: str = "") -> None:
        self.state["stages"][key] = {"status": status, "note": note}
        self.state["stage"] = key if status == "running" else None
        self._save(force=True)

    def _alive(self, *_a: Any) -> None:
        self._save()  # Lebenszeichen während langer Stufen

    def _observe(self) -> None:
        try:
            queue.observe_all(self.ctx, self.enrich, self.analysis)
        except Exception as e:  # die Anzeige darf den Lauf nie stören
            log.debug("Warteschlange nicht aktualisiert: %s", e)

    # -- Stufen ------------------------------------------------------------------------------------------
    def _fetch(self) -> None:
        self._set("fetch", "running")
        with exclusive(self.ctx, "fetch", LOCK_WAIT_S) as got:
            if not got:
                self._set("fetch", "error", "Import läuft bereits sehr lange – übersprungen")
                return
            res = (self.fetch_fn() if self.fetch_fn else None) or {}
        if res.get("skipped"):
            self._set("fetch", "skipped", str(res["skipped"]))
            return
        new = int(res.get("new", 0) or 0)
        errs = len(res.get("errors") or [])
        self._set("fetch", "done", f"{new} neue Meldungen" + (f", {errs} Quellenfehler" if errs else ""))

    def _enrich(self) -> None:
        if not self.enrich.enabled():
            self._set("enrich", "skipped", "Artikelinhalte deaktiviert")
            return
        self._set("enrich", "running")
        deadline = time.monotonic() + STAGE_MAX_S
        for _ in range(MAX_ROUNDS):
            if time.monotonic() > deadline:
                break
            res = self.enrich.run(limit=200, wait_s=LOCK_WAIT_S, on_item=self._alive)
            self._observe()
            processed = sum(int(res.get(k, 0) or 0) for k in ("success", "partial", "blocked", "failed", "retry"))
            if res.get("skipped") or not res.get("selected") or not processed:
                break  # nichts mehr abrufbar (oder nur pausierte Quellen übrig)
        left = len(self.enrich.candidates(100_000))
        if left:
            self._set("enrich", "blocked", f"{left} warten noch (Zeitlimit) – der Intervall-Job arbeitet weiter")
        else:
            self._set("enrich", "done")

    def _analyze(self) -> None:
        if not self.analysis.enabled():
            self._set("analyze", "skipped", "KI-Analyse ausgeschaltet oder nicht konfiguriert")
            return
        self._set("analyze", "running")
        deadline = time.monotonic() + STAGE_MAX_S
        note = ""
        for _ in range(MAX_ROUNDS):
            if time.monotonic() > deadline:
                note = note or "Zeitlimit"
                break
            res = self.analysis.run(wait_s=LOCK_WAIT_S, on_batch=self._alive)
            self._observe()
            if res.get("skipped"):
                break
            if res.get("stopped") or res.get("error"):
                note = str(res.get("stopped") or res.get("error"))
                break
            if not res.get("backlog_left") or not (res.get("analyzed") or res.get("rejected")):
                break
        left = self.analysis.backlog()
        if left:
            self._set("analyze", "blocked", f"{left} warten – {note or 'kein Fortschritt'}; der Intervall-Job "
                                            "arbeitet weiter")
        else:
            self._set("analyze", "done")

    def _group(self) -> None:
        self._set("group", "running")
        with exclusive(self.ctx, "group", LOCK_WAIT_S) as got:
            if not got:
                self._set("group", "error", "Gruppierung läuft bereits sehr lange – übersprungen")
                return
            res = self.events.run() or {}
        self._set("group", "skipped" if res.get("skipped") else "done", str(res.get("skipped") or ""))

    # -- Ablauf ------------------------------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        self.state["claimed"] = True
        self._save(force=True)
        self._observe()
        for key, fn in (("fetch", self._fetch), ("enrich", self._enrich), ("analyze", self._analyze),
                        ("group", self._group)):
            if self.state["stages"][key]["status"] == "skipped":
                continue
            try:
                fn()
            except Exception as e:  # eine Stufe darf den Lauf nie abbrechen; die nächste arbeitet weiter
                log.warning("Manueller Lauf, Stufe %s: %s", key, e)
                self._set(key, "error", f"{type(e).__name__}: {e}")
        self._observe()
        statuses = [s["status"] for s in self.state["stages"].values()]
        self.state["status"] = "error" if "error" in statuses else "partial" if "blocked" in statuses else "done"
        self.state["finished_at"] = _now()
        self.state["stage"] = None
        self._save(force=True)
        return {"status": self.state["status"], "stages": {k: v["status"] for k, v in self.state["stages"].items()}}


__all__ = ["STAGES", "WaveRunner", "is_running", "load", "reserve", "reserved"]
