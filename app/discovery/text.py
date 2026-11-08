"""Deutsche Zahlenformate für Begründungen und Signaltexte (gleiche Regeln wie die Oberfläche)."""

from __future__ import annotations

from typing import Any

from app.web import fmt


def P(v: Any, decimals: int = 1) -> str:
    return fmt.pct(v, decimals, sign=False)


def N(v: Any, decimals: int = 2) -> str:
    return fmt.num(v, decimals)


def G(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return fmt.num(f, 0 if f.is_integer() else 1)
