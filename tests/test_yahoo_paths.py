"""Yahoo-Kursabruf: Pfade ohne Netz (Fehler, Rückfall je Symbol, Pence-Umrechnung, Split-Faktor, Intraday)."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd

from app.prices.yahoo import YahooProvider
from app.util.http import RateLimiter


def provider(v7=None, fast=None, hist=None, meta=None, info=None):
    """YahooProvider mit nachgebildetem ``yfinance`` (kein Import, kein Netz)."""

    meta_dict, info_dict = meta or {}, info or {}

    class YfData:
        def get_raw_json(self, url, params=None, timeout=None):
            if isinstance(v7, Exception):
                raise v7
            return v7

    class Ticker:
        def __init__(self, sym):
            self.sym = sym

        @property
        def fast_info(self):
            f = (fast or {}).get(self.sym)
            if isinstance(f, Exception):
                raise f
            return f or {}

        history_metadata = meta_dict
        info = info_dict

        def history(self, **kw):
            if isinstance(hist, Exception):
                raise hist
            return hist

    p = YahooProvider(limiter=RateLimiter(0))
    p._yf = SimpleNamespace(data=SimpleNamespace(YfData=YfData), Ticker=Ticker)
    return p


def v7_row(sym, price, ccy="USD", t=1_700_000_000):
    return {"symbol": sym, "regularMarketPrice": price, "currency": ccy, "regularMarketTime": t,
            "regularMarketPreviousClose": price - 1, "regularMarketChangePercent": 1.5, "marketState": "REGULAR",
            "shortName": "X Corp"}


def test_quotes_bundled_with_pence_normalisation_and_info():
    p = provider(v7={"quoteResponse": {"result": [v7_row("AAA", 10.0), v7_row("BBB.L", 250.0, "GBp")]}})
    quotes, infos, errors = p.quotes(["BBB.L", "AAA", "AAA"])
    assert not errors and set(quotes) == {"AAA", "BBB.L"}
    assert quotes["BBB.L"].ccy == "GBP" and quotes["BBB.L"].price == 2.5  # Pence → Pfund
    assert quotes["AAA"].change_pct == 1.5 and infos["AAA"]["shortName"] == "X Corp"
    assert p.quotes([]) == ({}, {}, [])


def test_v7_failure_falls_back_to_fast_info_per_symbol_and_reports_errors():
    p = provider(v7=RuntimeError("429 Too Many Requests"),
                 fast={"AAA": {"lastPrice": 12.0, "previousClose": 11.0, "currency": "USD"},
                       "BBB": {"lastPrice": None, "currency": "USD"}, "CCC": OSError("netz")})
    quotes, _infos, errors = p.quotes(["AAA", "BBB", "CCC"])
    assert set(quotes) == {"AAA"} and quotes["AAA"].prev_close == 11.0
    text = " | ".join(errors)
    assert "v7/quote: RuntimeError" in text and "BBB: kein Kurs" in text and "CCC: OSError" in text


def test_parse_v7_rejects_rows_without_price_or_symbol():
    assert YahooProvider.parse_v7({"symbol": "X"}) == (None, {})
    assert YahooProvider.parse_v7({"regularMarketPrice": 1}) == (None, {})
    q, _ = YahooProvider.parse_v7({"symbol": "X", "regularMarketPrice": float("nan")})
    assert q is None


def test_history_undoes_split_adjustment_and_handles_empty_result():
    idx = pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07"])
    df = pd.DataFrame({"Open": [10, 11, 5.5, 6], "High": [11, 12, 6, 7], "Low": [9, 10, 5, 5.5],
                       "Close": [10, 11, 5.5, 6], "Volume": [1, 1, 2, 2], "Stock Splits": [0, 0, 2.0, 0]}, index=idx)
    p = provider(hist=df, meta={"currency": "USD"})
    bars, ccy = p.history("AAA", date(2025, 1, 1), date(2025, 1, 10))
    assert ccy == "USD" and bars[0].date.isoformat() == "2025-01-02"
    assert bars[0].split_factor == 2.0 and bars[-1].split_factor == 1.0  # Tage vor dem Split tragen den Faktor
    empty, ccy2 = provider(hist=pd.DataFrame(), meta={"currency": "GBp"}).history("AAA", date(2025, 1, 1))
    assert empty == [] and ccy2 == "GBP"
    none_df, _ = provider(hist=None).history("AAA", date(2025, 1, 1))
    assert none_df == []


def test_intraday_and_info_and_chunking():
    idx = pd.to_datetime(["2025-01-02 14:30", "2025-01-02 14:35"]).tz_localize("UTC")
    df = pd.DataFrame({"Open": [1, 2], "High": [2, 3], "Low": [1, 1], "Close": [1.5, None]}, index=idx)
    p = provider(hist=df, meta={"currency": "USD"},
                 info={"shortName": "N", "longBusinessSummary": "x" * 2000, "foo": 1})
    out = p.intraday("AAA", "1W")
    assert len(out) == 1 and out[0].close == 1.5  # Zeile ohne Schlusskurs entfällt
    info = p.info("AAA")
    assert info["shortName"] == "N" and len(info["longBusinessSummary"]) == 800 and "foo" not in info
    from app.prices.yahoo import _chunks

    assert [len(c) for c in _chunks([str(i) for i in range(85)], 40)] == [40, 40, 5]
