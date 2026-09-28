"""CoinGecko (Demo- oder Pro-API): aktuelle Kurse aller Coins in einem /simple/price-Aufruf, Historie
über /market_chart (Demo: max. 365 Tage). Jeder Aufruf wird im Monatskontingent gezählt.

Übertragen werden nur CoinGecko-IDs – keine Mengen, Werte oder Kontonamen.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from app.prices.models import Bar, Quote
from app.util.http import HttpError, Quota, RateLimiter, request_with_retry

log = logging.getLogger(__name__)

BASES = {"demo": "https://api.coingecko.com/api/v3", "pro": "https://pro-api.coingecko.com/api/v3"}
MAX_URL_IDS_CHARS = 6000


class BudgetExceeded(Exception):
    pass


class CoinGeckoProvider:
    name = "coingecko"

    def __init__(self, client: httpx.Client, api_key: str | None, plan: str = "demo",
                 limiter: RateLimiter | None = None, quota: Quota | None = None,
                 monthly_limit: int = 10000) -> None:
        self.client = client
        self.plan = plan if plan in BASES else "demo"
        self.base = BASES[self.plan]
        self.headers = {"accept": "application/json"}
        if api_key:
            self.headers["x-cg-pro-api-key" if self.plan == "pro" else "x-cg-demo-api-key"] = api_key
        # Demo: 30 Aufrufe/min → ≥2 s Abstand; ohne Key (öffentlich) deutlich strenger
        self.limiter = limiter or RateLimiter(2.1 if api_key else 6.5)
        self.quota = quota
        self.monthly_limit = monthly_limit

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        if self.quota is not None:
            used, _ = self.quota.used()
            if used >= self.monthly_limit:
                raise BudgetExceeded(f"CoinGecko-Monatskontingent erschöpft ({used}/{self.monthly_limit})")
        try:
            resp = request_with_retry(self.client, "GET", f"{self.base}{path}", params=params, headers=self.headers,
                                      limiter=self.limiter, retries=2)
        finally:
            if self.quota is not None:
                self.quota.add(1)
        return resp.json()

    @staticmethod
    def id_chunks(ids: list[str]) -> list[list[str]]:
        chunks: list[list[str]] = [[]]
        size = 0
        for i in ids:
            if size + len(i) + 1 > MAX_URL_IDS_CHARS and chunks[-1]:
                chunks.append([])
                size = 0
            chunks[-1].append(i)
            size += len(i) + 1
        return [c for c in chunks if c]

    def quotes(self, ids: list[str]) -> tuple[dict[str, Quote], list[str]]:
        out: dict[str, Quote] = {}
        errors: list[str] = []
        for chunk in self.id_chunks(sorted(set(ids))):
            try:
                data = self._get("/simple/price", {
                    "ids": ",".join(chunk), "vs_currencies": "eur", "include_24hr_change": "true",
                    "include_last_updated_at": "true", "precision": "full",
                })
            except (HttpError, httpx.HTTPError, ValueError) as e:
                errors.append(f"simple/price: {e}")
                continue
            for cid in chunk:
                rec = (data or {}).get(cid)
                if not rec or rec.get("eur") in (None, 0):
                    errors.append(f"{cid}: kein Kurs")
                    continue
                ts = rec.get("last_updated_at")
                out[cid] = Quote(series=f"cg:{cid}", price=float(rec["eur"]), ccy="EUR",
                                 market_time=datetime.fromtimestamp(int(ts), UTC) if ts else datetime.now(UTC),
                                 source="coingecko", change_pct=rec.get("eur_24h_change"))
        return out, errors

    def history(self, cid: str, days: int = 365) -> list[Bar]:
        """Tägliche Kurse. Punkte um 00:00 UTC gelten als Schlusskurs des Vortags."""
        data = self._get(f"/coins/{cid}/market_chart", {"vs_currency": "eur", "days": str(days)})
        return self.parse_market_chart(data)

    @staticmethod
    def parse_market_chart(data: dict[str, Any]) -> list[Bar]:
        by_day: dict[date, float] = {}
        now = datetime.now(UTC)
        for ms, price in (data or {}).get("prices") or []:
            if price is None:
                continue
            ts = datetime.fromtimestamp(ms / 1000, UTC)
            if now - ts < timedelta(hours=1):
                continue  # letzter Punkt = aktueller Kurs, kein Tagesschluss
            d = (ts - timedelta(minutes=1)).date() if ts.hour == 0 and ts.minute < 5 else ts.date()
            by_day[d] = float(price)
        return [Bar(date=d, close=p) for d, p in sorted(by_day.items())]

    def coins_list(self) -> list[dict[str, Any]]:
        """Gesamter Coin-Katalog (id, symbol, name, platforms) – ein Aufruf, ohne Bezug zum Portfolio."""
        data = self._get("/coins/list", {"include_platform": "true"})
        return [c for c in data if isinstance(c, dict) and c.get("id") and c.get("symbol")] if isinstance(data, list) \
            else []

    def markets(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        """Marktdaten (Kurs, Marktkapitalisierung, Volumen, Allzeithoch/-tief in EUR) für Kandidaten-IDs."""
        out: dict[str, dict[str, Any]] = {}
        uniq = sorted(set(ids))
        for i in range(0, len(uniq), 250):
            chunk = uniq[i:i + 250]
            data = self._get("/coins/markets", {"vs_currency": "eur", "ids": ",".join(chunk), "per_page": "250",
                                                "page": "1", "sparkline": "false"})
            for rec in data if isinstance(data, list) else []:
                if isinstance(rec, dict) and rec.get("id"):
                    out[str(rec["id"])] = rec
        return out

    def ping(self) -> bool:
        self._get("/ping", {})
        return True
