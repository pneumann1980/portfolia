"""Zeit- und Datumsfunktionen (Europe/Berlin als fachliche Zeitzone)."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Europe/Berlin")

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def set_local_tz(name: str) -> None:
    global _TZ
    try:
        _TZ = ZoneInfo(name)
    except Exception:  # pragma: no cover - ungültige TZ -> Berlin
        _TZ = ZoneInfo("Europe/Berlin")


def local_tz() -> ZoneInfo:
    return _TZ


def utcnow() -> datetime:
    return datetime.now(UTC)


def now_local() -> datetime:
    return datetime.now(_TZ)


def today_local() -> date:
    return now_local().date()


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    """ISO-8601 → aware datetime (UTC). Naive Zeitangaben gelten als UTC."""
    if not value:
        return None
    v = value.strip()
    if v.endswith("Z") or v.endswith("z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_tx_datetime(value: str) -> tuple[datetime, bool]:
    """Datetime aus transactions.csv.

    * Mit Uhrzeit: ISO 8601, ohne Offset = UTC.
    * Nur Datum: 12:00 Europe/Berlin (Datenvertrag).

    Rückgabe: (UTC-Zeitpunkt, date_only)
    """
    v = value.strip()
    if _DATE_ONLY_RE.match(v):
        d = date.fromisoformat(v)
        local = datetime.combine(d, time(12, 0), tzinfo=ZoneInfo("Europe/Berlin"))
        return local.astimezone(UTC), True
    dt = parse_iso(v)
    if dt is None:
        raise ValueError("leer")
    return dt, False


def to_local_date(dt: datetime) -> date:
    return dt.astimezone(_TZ).date()


def parse_date(value: str) -> date:
    return date.fromisoformat(value.strip()[:10])


def add_years(d: date, years: int) -> date:
    """Kalenderjahre addieren; 29.02. → 28.02. im Nicht-Schaltjahr."""
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(year=d.year + years, day=28)


def daterange(start: date, end: date):
    d = start
    one = timedelta(days=1)
    while d <= end:
        yield d
        d += one


def month_start(d: date) -> date:
    return d.replace(day=1)


def fmt_de_date(d: date | datetime | str | None) -> str:
    if d is None or d == "":
        return "–"
    if isinstance(d, str):
        try:
            d = parse_iso(d) if "T" in d else date.fromisoformat(d[:10])
        except ValueError:
            return d
    if isinstance(d, datetime):
        d = d.astimezone(_TZ)
        return d.strftime("%d.%m.%Y")
    return d.strftime("%d.%m.%Y")


def fmt_de_datetime(d: datetime | str | None) -> str:
    if d is None or d == "":
        return "–"
    if isinstance(d, str):
        try:
            d = parse_iso(d)
        except ValueError:
            return d
    assert isinstance(d, datetime)
    return d.astimezone(_TZ).strftime("%d.%m.%Y %H:%M")
