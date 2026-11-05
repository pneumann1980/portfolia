"""Schutz des Archivs (0.99.0): Buchungen vor dem festgeschriebenen Stichtag lassen sich nicht ändern.

Ein einziger Ort entscheidet (``check_ts``); die Schreibwege des Journals rufen ihn auf. Nur der Stichtag-Dienst
selbst (Korrekturen zum Stichtag, Rücknahme einer Korrektur) darf mit ``allow()`` vor den Stichtag schreiben. Der
Stichtag lässt sich unter *Einstellungen → Stichtag* zurücknehmen; danach ist das Archiv wieder bearbeitbar.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator
from datetime import date, datetime
from typing import Any

from app.util.timeutil import parse_iso

_allow: contextvars.ContextVar[bool] = contextvars.ContextVar("cutover_allow", default=False)


class Protected(Exception):
    """Buchung vor dem festgeschriebenen Stichtag."""


@contextlib.contextmanager
def allow() -> Iterator[None]:
    """Der Stichtag-Dienst darf (nur in diesem Block, nur dieser Thread) Buchungen vor dem Stichtag schreiben."""
    tok = _allow.set(True)
    try:
        yield
    finally:
        _allow.reset(tok)


def active(db: Any) -> Any:
    """Aktiver Stichtag (Zeile) oder ``None``."""
    return db.q1("SELECT id, cutoff, cutoff_utc, fingerprint, created_at FROM cutover WHERE status='active'")


def cutoff_of(db: Any) -> date | None:
    row = active(db)
    return date.fromisoformat(row["cutoff"]) if row is not None else None


def message(cutoff: date) -> str:
    return (f"Der Stichtag {cutoff:%d.%m.%Y} ist festgeschrieben: Buchungen davor lassen sich nicht ändern. "
            "Unter Einstellungen → Stichtag kann er zurückgenommen werden.")


def is_protected(db: Any, ts: str | datetime | None) -> date | None:
    """Stichtag, wenn ``ts`` (UTC-Zeitpunkt) davor liegt und der Schutz gilt; sonst ``None``."""
    if ts is None or _allow.get():
        return None
    row = active(db)
    if row is None:
        return None
    t = parse_iso(ts) if isinstance(ts, str) else ts
    cut = parse_iso(row["cutoff_utc"])
    if t is None or cut is None or t >= cut:
        return None
    return date.fromisoformat(row["cutoff"])


def check_ts(db: Any, *stamps: str | datetime | None) -> None:
    """``Protected`` auslösen, wenn einer der Zeitpunkte vor dem festgeschriebenen Stichtag liegt."""
    for ts in stamps:
        c = is_protected(db, ts)
        if c is not None:
            raise Protected(message(c))
