"""Sperren je Pipeline-Stufe: Intervall-Job und manuelle Welle laufen nie gleichzeitig auf denselben Daten.

Ohne Sperre ziehen zwei parallele Läufe dieselben Kandidaten, verbrauchen das KI-Kontingent doppelt und können
Duplikate schreiben (``max_instances=1`` gilt im Scheduler nur je Job-ID, Einmal-Jobs haben eigene IDs).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_guard = threading.Lock()
_locks: dict[tuple[int, str], threading.Lock] = {}


def stage_lock(ctx: Any, stage: str) -> threading.Lock:
    """Sperre der Stufe (je Kontext, damit Tests mit mehreren Kontexten sich nicht blockieren)."""
    key = (id(ctx), stage)
    with _guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.Lock()
        return lock


@contextmanager
def exclusive(ctx: Any, stage: str, wait_s: float = 0.0) -> Iterator[bool]:
    """``with exclusive(ctx, "analyze") as got:`` – ``got`` ist False, wenn die Stufe schon läuft (nach ``wait_s``)."""
    lock = stage_lock(ctx, stage)
    got = lock.acquire(timeout=wait_s) if wait_s > 0 else lock.acquire(blocking=False)
    try:
        yield got
    finally:
        if got:
            lock.release()


def is_busy(ctx: Any, stage: str) -> bool:
    return stage_lock(ctx, stage).locked()


__all__ = ["exclusive", "is_busy", "stage_lock"]
