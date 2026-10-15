"""Entscheidungsliste: die verbleibenden Aufgaben der Reihe nach entscheiden.

Nach Regeln und Autopilot bleiben Fälle, die ein Mensch entscheiden muss. Die Liste zeigt sie einzeln – mit Belegen,
Empfehlung samt Alternativen und den Entscheidungen „kein Handlungsbedarf“, „abgelehnt“, „später“ – und springt nach
jeder Entscheidung zum nächsten Fall. Sie enthält nur **Aufgaben** (keine Hinweise) und keine Sammelbefunde mit
Einzelvorgängen (die Vorgänge selbst stehen in der Liste); später Vorgemerkte und bewusst ungeklärt Belassene stehen
am Ende der Diagnose, nicht in dieser Liste. Die Liste ändert nichts, Korrekturen laufen über die gewohnte Vorschau.
"""

from __future__ import annotations

from typing import Any

from app.diagnosis import triage as TR
from app.diagnosis.model import Finding, Report

SKIP_STATES = frozenset({"spaeter", "ungeklaert"})


def queue(ctx: Any, report: Report | None = None) -> list[Finding]:
    """Zu entscheidende Aufgaben in Diagnose-Reihenfolge (Priorität, Art, Gewicht)."""
    return [f for f in TR.open_findings(ctx, report, cases=True)
            if not TR.is_hint(f) and not f.children and f.state not in SKIP_STATES]


def position(items: list[Finding], fid: str) -> int:
    """Index des Falls ``fid`` (unbekannt → 0)."""
    return next((i for i, f in enumerate(items) if f.id == fid), 0)
