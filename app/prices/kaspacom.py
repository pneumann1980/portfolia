"""KRC-20-Kurse (Kaspa-Tokens) über die KaspaCom-Marktplatz-API – für Tokens ohne CoinGecko-Eintrag.

Vertrag (api.kaspa.com/api-docs-json, ohne Schlüssel, Stand 10/2026)
    ``GET /api/token-info/{ticker}`` → u. a. ``price`` („Current token price in USD“), ``marketCap`` („Market
    capitalization in USD“), ``totalMinted``, ``totalSupply``, ``volumeUsd``, ``state``.

Abweichung Doku ↔ Antwort (live geprüft 10/2026, BRUCE/KASPER/POPKAT/NACHO)
    ``marketCap / (price × totalMinted)`` ergibt bei allen Tokens denselben Faktor ≈ 0,035 – den KAS/USD-Kurs. ``price``
    ist also **KAS je Token**, nicht USD. Portfolia legt sich nicht auf eine der beiden Lesarten fest, sondern prüft
    jede Antwort: Passt der Faktor zum aktuellen KAS/USD-Kurs (CoinGecko, ±35 %), wird ``price`` als KAS gelesen;
    liegt er bei ≈ 1, als USD; sonst wird kein Kurs übernommen.

Grenzen
    Marktplatzkurs eines einzelnen Anbieters (letzte Abschlüsse/Listungen), oft geringe Umsätze; keine
    Kurshistorie – die Tagesschlüsse entstehen ab der Zuordnung aus dem laufenden Kurs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from app.prices.models import Quote
from app.util.http import HttpError, RateLimiter, request_with_retry

BASE = "https://api.kaspa.com"
SERIES_PREFIX = "kc:"
UNIT_TOLERANCE = 0.35
_TICK = re.compile(r"^[A-Z0-9]{2,10}$")


def series_id(tick: str) -> str:
    return f"{SERIES_PREFIX}{tick.upper()}"


def valid_tick(tick: str | None) -> str | None:
    t = (tick or "").strip().upper()
    return t if _TICK.match(t) else None


@dataclass(frozen=True)
class TokenPrice:
    tick: str
    price_kas: float
    unit: str  # „KAS“ oder „USD“ (Lesart von ``price`` nach der Plausibilitätsprüfung)
    market_cap_usd: float | None
    volume_usd: float | None


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def interpret(body: Any, kas_usd: float) -> TokenPrice | str:
    """Antwort von ``token-info`` prüfen → Kurs in KAS oder Begründung, warum keiner übernommen wird."""
    if not isinstance(body, dict):
        return "Antwort nicht lesbar"
    tick = valid_tick(body.get("ticker"))
    price, mcap = _num(body.get("price")), _num(body.get("marketCap"))
    supply = _num(body.get("totalMinted")) or _num(body.get("totalSupply"))
    if tick is None or not price or price <= 0:
        return "kein Kurs"
    if not mcap or not supply or kas_usd <= 0:
        return "Einheit nicht prüfbar (Marktwert oder Menge fehlt)"
    factor = mcap / (price * supply)
    vol = _num(body.get("volumeUsd"))
    if abs(factor / kas_usd - 1) <= UNIT_TOLERANCE:
        return TokenPrice(tick, price, "KAS", mcap, vol)
    if abs(factor - 1) <= UNIT_TOLERANCE:
        return TokenPrice(tick, price / kas_usd, "USD", mcap, vol)
    return f"Einheit unklar (Marktwert/Kurs-Faktor {factor:.4g}, KAS/USD {kas_usd:.4g})"


class KaspaComProvider:
    name = "kaspacom"

    def __init__(self, client: httpx.Client, limiter: RateLimiter | None = None) -> None:
        self.client = client
        self.limiter = limiter or RateLimiter(1.0)

    def token_info(self, tick: str) -> Any:
        t = valid_tick(tick)
        if t is None:
            raise ValueError("ungültiges KRC-20-Kürzel")
        resp = request_with_retry(self.client, "GET", f"{BASE}/api/token-info/{quote(t)}", retries=1,
                                  limiter=self.limiter, headers={"accept": "application/json"})
        return resp.json()

    def quotes(self, ticks: list[str], kas_eur: float, kas_usd: float) -> tuple[dict[str, Quote], list[str]]:
        out: dict[str, Quote] = {}
        errors: list[str] = []
        now = datetime.now(UTC)
        for t in sorted({x for x in (valid_tick(t) for t in ticks) if x}):
            try:
                res = interpret(self.token_info(t), kas_usd)
            except (HttpError, httpx.HTTPError, ValueError) as e:
                errors.append(f"{t}: {e}")
                continue
            if isinstance(res, str):
                errors.append(f"{t}: {res}")
                continue
            out[t] = Quote(series=series_id(t), price=res.price_kas * kas_eur, ccy="EUR", market_time=now,
                           source=self.name)
        return out, errors
