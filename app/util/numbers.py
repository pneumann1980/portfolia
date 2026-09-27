"""Zahleneingaben aus Formularen (de-DE oder englisch) robust lesen."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

_DE_THOUSANDS = re.compile(r"^-?[1-9]\d{0,2}(\.\d{3})+$")


def parse_number(v: Any) -> Decimal | None:
    """„1.234,56“, „1234,56“, „1234.56“ → Decimal; leer/ungültig → None.

    Nur Punkte in Dreiergruppen (z. B. „5.000“) gelten als Tausendertrennzeichen (deutsche Eingabe).
    """
    s = str(v if v is not None else "").strip().replace(" ", "").replace(" ", "").replace("€", "").replace("%", "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    elif _DE_THOUSANDS.match(s):
        s = s.replace(".", "")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def plain_de(v: Any, max_decimals: int = 8) -> str:
    """Zahl für Eingabefelder: deutsches Komma, keine Tausendertrennzeichen, ohne überflüssige Nullen."""
    if v is None or v == "":
        return ""
    d = v if isinstance(v, Decimal) else Decimal(str(v))
    q = d.quantize(Decimal(1).scaleb(-max_decimals)).normalize()
    s = format(q, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s.replace(".", ",")
