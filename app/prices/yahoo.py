"""Yahoo Finance über yfinance: gebündelte Kurse, Tageshistorie (nicht split-adjustiert), Intraday, Stammdaten.

yfinance wird verzögert importiert (Speicher/Startzeit). Alle Aufrufe laufen durch einen Rate-Limiter
(max. 1 Anfrage/s). Es werden ausschließlich Ticker-Symbole übertragen – keine Mengen oder Werte.
"""

from __future__ import annotations

import contextlib
import importlib
import logging
import math
import threading
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.prices.models import Bar, IntradayBar, Quote, normalize_ccy
from app.util.http import RateLimiter

log = logging.getLogger(__name__)

V7_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
INFO_KEYS = (
    "longName", "shortName", "quoteType", "exchange", "fullExchangeName", "currency", "marketCap", "trailingPE",
    "forwardPE", "priceToBook", "dividendYield", "beta", "fiftyTwoWeekHigh", "fiftyTwoWeekLow", "sector", "industry",
    "country", "website", "targetLowPrice", "targetHighPrice", "targetMeanPrice", "targetMedianPrice",
    "numberOfAnalystOpinions", "recommendationKey", "recommendationMean", "longBusinessSummary", "financialCurrency",
)


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


_IMPORT_LOCK = threading.Lock()


def _chunks(items: list[str], n: int) -> Iterable[list[str]]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


class YahooProvider:
    name = "yahoo"

    def __init__(self, cache_dir: Path | None = None, limiter: RateLimiter | None = None) -> None:
        self.limiter = limiter or RateLimiter(1.0)
        self._yf = None
        self._cache_dir = cache_dir

    @property
    def yf(self) -> Any:
        # Import einmalig und thread-sicher (parallele Jobs würden sonst ein halb initialisiertes Paket sehen)
        if self._yf is None:
            with _IMPORT_LOCK:
                if self._yf is None:
                    yf = importlib.import_module("yfinance")
                    importlib.import_module("yfinance.data")
                    if self._cache_dir:
                        self._cache_dir.mkdir(parents=True, exist_ok=True)
                        yf.set_tz_cache_location(str(self._cache_dir))
                    self._yf = yf
        return self._yf

    # -- aktuelle Kurse --------------------------------------------------------------------------
    @staticmethod
    def parse_v7(r: dict[str, Any]) -> tuple[Quote | None, dict[str, Any]]:
        sym = r.get("symbol")
        price = _num(r.get("regularMarketPrice"))
        if not sym or price is None:
            return None, {}
        raw_ccy = r.get("currency")
        ccy, price = normalize_ccy(raw_ccy, price)
        _, prev = normalize_ccy(raw_ccy, _num(r.get("regularMarketPreviousClose")))
        mt = r.get("regularMarketTime")
        market_time = datetime.fromtimestamp(int(mt), UTC) if isinstance(mt, (int, float)) else None
        q = Quote(series=f"yahoo:{sym}", price=price, ccy=ccy, market_time=market_time, source="yahoo",  # type: ignore[arg-type]
                  prev_close=prev, change_pct=_num(r.get("regularMarketChangePercent")),
                  market_state=r.get("marketState"))
        info = {k: r.get(k) for k in INFO_KEYS if r.get(k) not in (None, "")}
        return q, info

    def quotes(self, symbols: list[str]) -> tuple[dict[str, Quote], dict[str, dict[str, Any]], list[str]]:
        """Gebündelter Abruf (v7/quote, bis 40 Symbole je Anfrage); Fallback je Symbol via fast_info."""
        quotes: dict[str, Quote] = {}
        infos: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        if not symbols:
            return quotes, infos, errors
        YfData = self.yf.data.YfData
        for chunk in _chunks(sorted(set(symbols)), 40):
            self.limiter.wait()
            try:
                j = YfData().get_raw_json(V7_QUOTE_URL, params={"symbols": ",".join(chunk), "formatted": "false",
                                                                "lang": "en-US", "region": "US"}, timeout=20)
                for r in ((j or {}).get("quoteResponse") or {}).get("result") or []:
                    q, info = self.parse_v7(r)
                    if q:
                        sym = q.series.split(":", 1)[1]
                        quotes[sym] = q
                        infos[sym] = info
            except Exception as e:
                errors.append(f"v7/quote: {type(e).__name__}: {e}"[:300])
            for sym in chunk:
                if sym in quotes:
                    continue
                q = self._fast_info(sym, errors)
                if q:
                    quotes[sym] = q
        return quotes, infos, errors

    def _fast_info(self, sym: str, errors: list[str]) -> Quote | None:
        self.limiter.wait()
        try:
            fi = self.yf.Ticker(sym).fast_info
            raw_ccy = fi.get("currency")
            ccy, price = normalize_ccy(raw_ccy, _num(fi.get("lastPrice")))
            _, prev = normalize_ccy(raw_ccy, _num(fi.get("previousClose")))
            if price is None:
                errors.append(f"{sym}: kein Kurs")
                return None
            return Quote(series=f"yahoo:{sym}", price=price, ccy=ccy, market_time=datetime.now(UTC), source="yahoo",
                         prev_close=prev)
        except Exception as e:
            errors.append(f"{sym}: {type(e).__name__}: {e}"[:300])
            return None

    # -- Historie ----------------------------------------------------------------------------------
    def history(self, symbol: str, start: date, end: date | None = None) -> tuple[list[Bar], str | None]:
        """Tagesschlusskurse *wie gehandelt* (Split-Adjustierung von Yahoo wird rückgängig gemacht)."""
        end = end or date.today()
        self.limiter.wait()
        t = self.yf.Ticker(symbol)
        df = t.history(start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(), interval="1d",
                       auto_adjust=False, actions=True, raise_errors=True, timeout=30)
        meta: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            meta = t.history_metadata or {}
        raw_ccy = meta.get("currency")
        if df is None or df.empty:
            return [], normalize_ccy(raw_ccy, 1.0)[0]
        return self.bars_from_df(df, raw_ccy), normalize_ccy(raw_ccy, 1.0)[0]

    @staticmethod
    def bars_from_df(df: Any, raw_ccy: str | None) -> list[Bar]:
        rows = []
        for idx, rec in df.iterrows():
            d = idx.date() if hasattr(idx, "date") else date.fromisoformat(str(idx)[:10])
            split = _num(rec.get("Stock Splits")) or 0.0
            rows.append((d, _num(rec.get("Open")), _num(rec.get("High")), _num(rec.get("Low")), _num(rec.get("Close")),
                         _num(rec.get("Volume")), split))
        # Kumulierter Split-Faktor: Produkt der Splits *nach* dem Tag
        def conv(x: float | None, f: float) -> float | None:
            return normalize_ccy(raw_ccy, x * f)[1] if x is not None else None

        bars: list[Bar] = []
        factor = 1.0
        for d, o, h, lo, c, v, split in reversed(rows):
            if c is None:
                if split and split > 0:
                    factor *= split
                continue
            bars.append(Bar(date=d, open=conv(o, factor), high=conv(h, factor), low=conv(lo, factor),
                            close=conv(c, factor), volume=v, split_factor=factor))  # type: ignore[arg-type]
            if split and split > 0:
                factor *= split
        bars.reverse()
        return bars

    def intraday(self, symbol: str, rng: str) -> list[IntradayBar]:
        period, interval = {"1T": ("1d", "5m"), "1W": ("5d", "30m")}.get(rng, ("1d", "5m"))
        self.limiter.wait()
        t = self.yf.Ticker(symbol)
        df = t.history(period=period, interval=interval, auto_adjust=False, actions=False, raise_errors=True,
                       timeout=20)
        raw_ccy = None
        with contextlib.suppress(Exception):
            raw_ccy = (t.history_metadata or {}).get("currency")
        out = []
        for idx, rec in df.iterrows():
            c = _num(rec.get("Close"))
            if c is None:
                continue
            ts = idx.to_pydatetime().astimezone(UTC)
            f = (lambda x: normalize_ccy(raw_ccy, x)[1] if x is not None else None)  # noqa: E731
            out.append(IntradayBar(ts=ts, close=f(c), open=f(_num(rec.get("Open"))), high=f(_num(rec.get("High"))),  # type: ignore[arg-type]
                                   low=f(_num(rec.get("Low")))))
        return out

    def info(self, symbol: str) -> dict[str, Any]:
        self.limiter.wait()
        inf = self.yf.Ticker(symbol).info or {}
        out = {k: inf.get(k) for k in INFO_KEYS if inf.get(k) not in (None, "")}
        if "longBusinessSummary" in out:
            out["longBusinessSummary"] = str(out["longBusinessSummary"])[:800]
        return out
