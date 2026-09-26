"""Datentypen für Kurse."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

# Yahoo liefert manche Börsen in Untereinheiten (Pence, Cent, Agorot)
SUBUNIT_CCY = {"GBp": ("GBP", 100.0), "GBX": ("GBP", 100.0), "ZAc": ("ZAR", 100.0), "ILA": ("ILS", 100.0)}


def normalize_ccy(ccy: str | None, price: float | None) -> tuple[str | None, float | None]:
    if ccy in SUBUNIT_CCY and price is not None:
        base, div = SUBUNIT_CCY[ccy]
        return base, price / div
    return (ccy.upper() if ccy else None), price


@dataclass(slots=True)
class Quote:
    series: str
    price: float
    ccy: str | None
    market_time: datetime | None
    source: str
    prev_close: float | None = None
    change_pct: float | None = None
    market_state: str | None = None


@dataclass(slots=True)
class Bar:
    date: date
    close: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    split_factor: float = 1.0


@dataclass(slots=True)
class IntradayBar:
    ts: datetime
    close: float
    open: float | None = None
    high: float | None = None
    low: float | None = None


@dataclass(slots=True)
class PriceInfo:
    """Bewerteter aktueller Kurs eines Assets in EUR."""

    price_eur: float
    price_native: float | None
    ccy: str | None
    ts: datetime | None
    source: str  # yahoo | coingecko | manual | daily | fiat | demo | none
    kind: str  # quote | daily | manual | fiat | unvalued
    stale: bool
    prev_close_eur: float | None = None
    fx_rate: float | None = None
    note: str | None = None

    @property
    def valued(self) -> bool:
        return self.kind != "unvalued"
