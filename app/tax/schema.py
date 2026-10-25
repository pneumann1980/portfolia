"""Schema-Bausteine zur Prüfung von Regelwerk-Dateien (Parameterstände).

Ein Regelwerk liefert über ``RulePack.params_schema()`` einen Baum aus Dicts (Abschnitte) und Blättern
(:class:`Num`, :class:`Int`, :class:`Bool`, :class:`Enum`). Unbekannte Schlüssel sind Fehler – Tippfehler sollen nicht
still ignoriert werden.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Num:
    lo: float
    hi: float
    nullable: bool = False
    warn_factor: float | None = None  # Warnung, wenn der Wert um mehr als diesen Faktor vom Standard abweicht

    def check(self, v: Any) -> str | None:
        if v is None:
            return None if self.nullable else "Zahl erwartet, leer gefunden"
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return f"Zahl erwartet, gefunden „{v}“"
        if not (self.lo <= v <= self.hi):
            return f"Wert {v} außerhalb des plausiblen Bereichs {self.lo:g} … {self.hi:g}"
        return None


@dataclass(frozen=True)
class Int(Num):
    def check(self, v: Any) -> str | None:
        if isinstance(v, float) and v != int(v):
            return f"ganze Zahl erwartet, gefunden {v}"
        return super().check(v)


@dataclass(frozen=True)
class Bool:
    def check(self, v: Any) -> str | None:
        return None if isinstance(v, bool) else f"true/false erwartet, gefunden „{v}“"


@dataclass(frozen=True)
class Enum:
    values: tuple[str, ...]

    def check(self, v: Any) -> str | None:
        return None if v in self.values else f"erlaubt: {', '.join(self.values)} – gefunden „{v}“"


@dataclass(frozen=True)
class YearMap:
    """Zuordnung Jahr → Wert (``per_year``-Tabellen)."""

    leaf: Any


Schema = dict[str, Any]
