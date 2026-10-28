"""Assistent der Datenqualität: immer die nächste Aufgabe, nach jeder Aktion die übernächste.

Die Aufgabenliste ist der Arbeitsplan (:mod:`app.diagnosis.workplan`, Einzelbefunde statt Gruppen), Stufe A vor B.
Der Assistent rechnet nichts Eigenes: er wählt aus, führt zur passenden Seite (Befund, Klärungsfall, Prüfstapel,
Prüfbericht) und kennt zwei Merker im Browser (Cookies): ``dq_flow`` (Assistent aktiv) und ``dq_skip`` (für diese
Sitzung übersprungene Aufgaben, nur Kurzkennungen).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from app.diagnosis import workplan as WP

MIN_PER_TASK = 1.5  # grobe Schätzung für die Anzeige („ca. N Min.“)
MAX_SKIP = 40


def kid(key: str) -> str:
    """Kurzkennung einer Aufgabe (cookie- und URL-sicher)."""
    return hashlib.sha1(key.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]


@dataclass
class Step:
    item: WP.WorkItem | None  # None = nichts mehr zu tun
    position: int  # 1-basiert innerhalb der offenen Aufgaben (einschließlich übersprungener)
    total: int
    skipped: int

    @property
    def minutes(self) -> int:
        return max(1, round((self.total - self.skipped) * MIN_PER_TASK)) if self.total else 0


def open_tasks(ctx: Any) -> list[WP.WorkItem]:
    """Offene Aufgaben (Stufe A, dann B), Einzelbefunde einzeln."""
    plan = WP.build(ctx, granular=True)
    return [i for i in plan.items if i.tier in ("A", "B")]


def parse_skip(raw: str) -> list[str]:
    return [x for x in (raw or "").split(",") if len(x) == 8 and all(c in "0123456789abcdef" for c in x)][:MAX_SKIP]


def next_step(ctx: Any, skip: list[str]) -> Step:
    tasks = open_tasks(ctx)
    todo = [t for t in tasks if kid(t.key) not in set(skip)]
    skipped = len(tasks) - len(todo)
    return Step(todo[0] if todo else None, skipped + 1, len(tasks), skipped)


def summary(ctx: Any) -> tuple[int, int]:
    """(Anzahl offener Aufgaben, geschätzte Minuten) für die Übersicht."""
    n = len(open_tasks(ctx))
    return n, (max(1, round(n * MIN_PER_TASK)) if n else 0)
