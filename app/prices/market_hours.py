"""Handelszeitfenster (vereinfachte Abruffenster in Europe/Berlin, ohne Feiertagskalender)."""

from __future__ import annotations

from datetime import datetime, time

EU_SUFFIXES = {
    "DE", "F", "SG", "MU", "BE", "DU", "HM", "HA", "PA", "AS", "MI", "MC", "L", "IL", "SW", "VI", "BR", "LS", "ST",
    "CO", "OL", "HE", "IR", "WA", "PR", "BD", "AT", "IC", "TL", "RG", "VS",
}


def exchange_group(symbol: str) -> str:
    s = symbol.upper()
    if s.endswith("=X"):
        return "fx"
    if s.endswith("-EUR") or s.endswith("-USD"):
        return "crypto"
    if "." in s:
        suffix = s.rsplit(".", 1)[1]
        if suffix == "AX":
            return "asx"
        if suffix in EU_SUFFIXES:
            return "eu"
        return "other"
    if s.startswith("^"):
        return "index"
    return "us"


# Abruffenster (lokale Zeit Europe/Berlin), großzügig inkl. Vor-/Nachbörse (Tradegate/L&S/Gettex)
WINDOWS: dict[str, tuple[time, time]] = {
    "eu": (time(7, 30), time(22, 45)),
    "us": (time(14, 45), time(22, 45)),
    "asx": (time(0, 0), time(8, 45)),
    "index": (time(7, 30), time(22, 45)),
    "other": (time(0, 0), time(23, 59)),
    "fx": (time(0, 0), time(23, 59)),
}


def in_window(group: str, now_local: datetime) -> bool:
    if group == "crypto":
        return True
    wd = now_local.weekday()
    if wd >= 5:  # Sa/So
        return False
    start, end = WINDOWS.get(group, WINDOWS["other"])
    return start <= now_local.time() <= end


def trading_days_between(d0: datetime, d1: datetime) -> int:
    """Anzahl Werktage (Mo–Fr) strikt nach d0.date() bis einschließlich d1.date()."""
    from datetime import timedelta

    n = 0
    d = d0.date() + timedelta(days=1)
    while d <= d1.date():
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n
