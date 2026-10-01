"""Zahlen- und Datumsformatierung de-DE für Templates."""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.util.numbers import plain_de
from app.util.timeutil import fmt_de_date, fmt_de_datetime, local_tz

NBSP = " "


def _group(int_part: str) -> str:
    out = []
    while len(int_part) > 3:
        out.insert(0, int_part[-3:])
        int_part = int_part[:-3]
    out.insert(0, int_part)
    return ".".join(out)


def num(v: Any, decimals: int = 2, sign: bool = False) -> str:
    if v is None or v == "":
        return "–"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if not math.isfinite(f):
        return "–"
    s = f"{abs(f):.{decimals}f}"
    ip, _, dp = s.partition(".")
    res = _group(ip) + ("," + dp if dp else "")
    if f < 0 and float(s) != 0:
        res = "-" + res
    elif sign and f > 0 and float(s) != 0:
        res = "+" + res
    return res


def eur(v: Any, decimals: int = 2, sign: bool = False) -> str:
    if v is None or v == "":
        return "–"
    return f"{num(v, decimals, sign)}{NBSP}€"


def eur_kpi(v: Any, sign: bool = False) -> str:
    """Für Kennzahl-Kacheln: ab 10.000 € ohne Nachkommastellen (bleibt einzeilig)."""
    if v is None or v == "":
        return "–"
    return eur(v, 0 if abs(float(v)) >= 10000 else 2, sign)


def eur_compact(v: Any, sign: bool = False) -> str:
    if v is None:
        return "–"
    f = float(v)
    a = abs(f)
    if a >= 1e6:
        return f"{num(f / 1e6, 2, sign)}{NBSP}Mio.{NBSP}€"
    if a >= 1e4:
        return f"{num(f / 1e3, 1, sign)}{NBSP}Tsd.{NBSP}€"
    return eur(f, 2, sign)


def pct(v: Any, decimals: int = 2, sign: bool = True, ratio: bool = True) -> str:
    """ratio=True: 0.0123 → '+1,23 %'."""
    if v is None or v == "":
        return "–"
    f = float(v) * (100 if ratio else 1)
    return f"{num(f, decimals, sign)}{NBSP}%"


def qty(v: Any, max_decimals: int = 8) -> str:
    if v is None or v == "":
        return "–"
    d = Decimal(str(v))
    if d == d.to_integral_value():
        return num(float(d), 0)
    f = float(d)
    a = abs(f)
    dec = 2 if a >= 1000 else 4 if a >= 1 else max_decimals
    s = num(f, dec)
    if "," in s:
        s = s.rstrip("0").rstrip(",")
    return s


def qty_exact(v: Any) -> str:
    """Menge ohne Rundung (Bestandsabgleich: auch kleinste Abweichungen sichtbar), deutsches Format."""
    if v is None or v == "":
        return "–"
    d = Decimal(str(v))
    text = format(d.normalize(), "f") if d else "0"
    sign = "-" if text.startswith("-") else ""
    text = text.lstrip("-")
    int_part, _, frac = text.partition(".")
    return sign + _group(int_part) + ("," + frac if frac else "")


def asset_label(key: Any) -> str:
    """Token-Kennung ``SYMBOL@CHAIN:Contract`` lesbar kürzen (Symbol · Chain · Contract gekürzt)."""
    k = str(key or "")
    sym, at, rest = k.partition("@")
    if not at or ":" not in rest:
        return k
    chain, _, contract = rest.partition(":")
    c = contract if len(contract) <= 14 else f"{contract[:8]}…{contract[-4:]}"
    return f"{sym} · {chain} {c}"


def price(v: Any) -> str:
    if v is None:
        return "–"
    f = float(v)
    a = abs(f)
    dec = 2 if a >= 1 else 4 if a >= 0.01 else 8
    return eur(f, dec)


def tone(v: Any) -> str:
    """CSS-Klasse für Vorzeichen (Farbe nie allein – Vorzeichen/Pfeil stehen immer dabei)."""
    if v is None:
        return "neutral"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "neutral"
    if abs(f) < 1e-12:
        return "neutral"
    return "up" if f > 0 else "down"


def arrow(v: Any) -> str:
    t = tone(v)
    return {"up": "▲", "down": "▼"}.get(t, "•")


def date_de(v: date | datetime | str | None) -> str:
    return fmt_de_date(v)


def datetime_de(v: datetime | str | None) -> str:
    return fmt_de_datetime(v)


def rel_time(v: datetime | None, now: datetime | None = None) -> str:
    if v is None:
        return "nie"
    from datetime import UTC

    now = now or datetime.now(UTC)
    s = (now - v).total_seconds()
    if s < 90:
        return "gerade eben"
    if s < 3600:
        return f"vor {int(s // 60)} Min."
    if s < 86400:
        return f"vor {int(s // 3600)} Std."
    days = int(s // 86400)
    if days < 14:
        return f"vor {days} Tag{'en' if days != 1 else ''}"
    return v.astimezone(local_tz()).strftime("%d.%m.%Y")


def rel_time_iso(v: str | None) -> str:
    from app.util.timeutil import parse_iso

    try:
        return rel_time(parse_iso(v)) if v else "–"
    except ValueError:
        return str(v)


def duration(seconds: int | None) -> str:
    if not seconds:
        return "–"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fromjson(v: Any) -> Any:
    import json

    try:
        return json.loads(v) if isinstance(v, str) else v
    except ValueError:
        return None


def register(env: Any) -> None:
    env.filters.update({
        "fromjson": fromjson,
        "num": num, "eur": eur, "eur_kpi": eur_kpi, "eur_compact": eur_compact, "pct": pct, "qty": qty, "price": price,
        "qty_exact": qty_exact, "asset_label": asset_label,
        "tone": tone, "arrow": arrow, "date_de": date_de, "datetime_de": datetime_de, "rel_time": rel_time,
        "rel_time_iso": rel_time_iso, "duration": duration, "plain": plain_de,
    })
