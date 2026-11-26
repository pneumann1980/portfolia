"""Zeitanteile eines Seitenaufrufs (0.122.2) – für die Karte „Ladezeiten“ im News-Status.

Die Middleware startet die Messung nur für ausgewählte Seiten (``app.web.accesslog._TIMED``); alle anderen Aufrufe
und Hintergrund-Jobs zahlen nur das Nachschlagen einer ContextVar. Gemessen wird:

* ``queue``     – vom Eintreffen der Anfrage bis zum Beginn der Bearbeitung im Handler (Warteschlange des
  Thread-Pools, andere Anfragen, Hintergrund-Jobs, die die CPU belegen).
* ``cpu``       – Rechenzeit des bearbeitenden Threads (``time.thread_time``) vom Beginn bis zum fertigen HTML. Ist sie
  deutlich kleiner als die Bearbeitungszeit, wartet der Aufruf (Python-GIL durch Hintergrund-Jobs, Festplatte).
* ``db``        – Summe aller Datenbankabfragen dieses Aufrufs (auch innerhalb von Portfolio/Bewertung).
* ``portfolio`` – Portfolio, Ledger, Bewertung und Historie (äußerster Aufruf, inkl. ihrer Datenbankabfragen).
* ``render``    – Seitenaufbau aus der Vorlage.

Die Anteile überlappen (Datenbank steckt teils in Portfolio); sie zeigen, *wohin* die Zeit geht, nicht eine Summe.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_acc: ContextVar[dict[str, Any] | None] = ContextVar("portfolia_reqtiming", default=None)
PARTS = ("queue", "cpu", "db", "portfolio", "render")


def begin() -> Any:
    """Messung für diesen Aufruf beginnen (Middleware); Rückgabe: Token für :func:`end`."""
    return _acc.set({"t0": time.perf_counter(), "db": 0.0, "portfolio": 0.0, "render": 0.0, "depth": 0})


def end(token: Any) -> dict[str, float] | None:
    acc = _acc.get()
    _acc.reset(token)
    if acc is None or "start" not in acc:
        return None
    out = {k: round(float(acc.get(k, 0.0)) * 1000, 1) for k in ("db", "portfolio", "render")}
    out["queue"] = round((acc["start"] - acc["t0"]) * 1000, 1)
    if "cpu_end" in acc:
        out["cpu"] = round((acc["cpu_end"] - acc["cpu_start"]) * 1000, 1)
    return out


def handler_started() -> None:
    """Beginn der Bearbeitung (erster ``get_ctx`` im Handler-Thread)."""
    acc = _acc.get()
    if acc is not None and "start" not in acc:
        acc["start"] = time.perf_counter()
        acc["cpu_start"] = time.thread_time()


def add(part: str, secs: float) -> None:
    acc = _acc.get()
    if acc is not None:
        acc[part] = acc.get(part, 0.0) + secs


@contextmanager
def measure(part: str) -> Iterator[None]:
    """Zeit eines Abschnitts addieren; verschachtelte Aufrufe desselben Abschnitts zählen nur einmal (äußerster)."""
    acc = _acc.get()
    if acc is None:
        yield
        return
    key = f"depth_{part}"
    outer = not acc.get(key)
    acc[key] = acc.get(key, 0) + 1
    t0 = time.perf_counter()
    try:
        yield
    finally:
        acc[key] -= 1
        if outer:
            acc[part] = acc.get(part, 0.0) + (time.perf_counter() - t0)


def rendered() -> None:
    """HTML fertig (nach dem Seitenaufbau, im Handler-Thread): Ende der Rechenzeitmessung."""
    acc = _acc.get()
    if acc is not None and "cpu_start" in acc:
        acc["cpu_end"] = time.thread_time()
