"""Gemeinsame Marktdaten-Schnittstelle für Dashboard, Positionen, Detailansicht, Watchlist und Schnellbuchung.

Alle Kurse kommen aus *einer* Ablage (``PriceStore``: ``quote_latest``, ``price_daily``, ``series_meta``) und werden
von *einem* Dienst (``PriceService``) mit Kontingent, Drosselung und Fehlerschutz abgerufen. Diese Fassade bündelt die
lesenden Zugriffe, damit keine Ansicht eigene Umrechnungen oder Abrufe baut:

* :meth:`MarketData.eur_series` – Tagesschlusskurse in EUR (mit Devisenkurs des Tages, Split-Faktor, Herkunft),
* :meth:`MarketData.price_on` – Kurs zu einem Stichtag (manuelle Buchung, Schnellkauf/-verkauf),
* :meth:`MarketData.snapshots` – aktueller Kurs, 24h, 7d, Marktkapitalisierung, Sparkline, Anbieter, Datenqualität,
* :meth:`MarketData.refresh` – Watchlist-Werte nachladen: **ein** CoinGecko-Aufruf für alle Coins
  (``/coins/markets`` mit 7-Tage-Verlauf), Yahoo gebündelt (``v7/quote``) plus kurze Tageshistorie je Wertpapier.
  Ergebnisse sind 15 Minuten gültig; parallele Abrufe derselben Reihe gibt es nicht (Sperre).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.prices.models import Bar, Quote
from app.util.timeutil import iso, parse_iso, today_local

log = logging.getLogger(__name__)

REFRESH_TTL = timedelta(minutes=15)
SPARK_DAYS = 30
_LOCK = threading.Lock()


@dataclass
class MarketSnapshot:
    series: str
    price_eur: float | None = None
    price_native: float | None = None  # Kurs in Handelswährung bzw. Indexpunkten (Yahoo-Indizes: Punkte, nie EUR)
    ccy: str | None = None
    change_24h: float | None = None  # Anteil (0.043 = +4,3 %)
    change_7d: float | None = None
    market_cap_eur: float | None = None
    sparkline: list[float] = field(default_factory=list)  # EUR, älteste zuerst
    provider: str = ""
    ts: datetime | None = None
    stale: bool = False
    name: str | None = None
    symbol: str | None = None

    @property
    def provider_label(self) -> str:
        return {"cg": "CoinGecko", "yahoo": "Yahoo Finance", "demo": "Demo"}.get(self.provider, self.provider)


class MarketData:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.store = ctx.store
        self.prices = ctx.prices

    # -- Reihen ---------------------------------------------------------------------------------------------
    def eur_series(self, series: str, start: date | None = None, end: date | None = None,
                   split_adjust: bool = True) -> list[tuple[date, float, float, str, Any]]:
        """(Tag, Schluss in EUR, Schluss in Handelswährung, Herkunft, Zeile) – Tage ohne Devisenkurs entfallen."""
        out: list[tuple[date, float, float, str, Any]] = []
        fx_cache: dict[str, dict[str, float]] = {}
        for r in self.store.daily_range(series, start, end):
            ccy = (r["ccy"] or "EUR").upper()
            rate = 1.0
            if ccy != "EUR":
                if ccy not in fx_cache:
                    fx_cache[ccy] = self.store.fx_series_map(ccy)
                fx = fx_cache[ccy].get(r["date"])
                if not fx:
                    near = self.store.fx_on_or_before(ccy, date.fromisoformat(r["date"]))
                    fx = near[0] if near else None
                if not fx:
                    continue
                rate = 1.0 / fx
            sf = (r["split_factor"] or 1.0) if split_adjust else 1.0
            out.append((date.fromisoformat(r["date"]), r["close"] * rate / sf, r["close"] / sf, r["source"] or "", r))
        return out

    # -- Stichtag -------------------------------------------------------------------------------------------
    def price_on(self, asset_id: str, d: date) -> tuple[Decimal, str, bool] | None:
        """Kurs in EUR am Tag ``d`` (Schlusskurs, sonst aktueller/letzter Kurs, manueller oder Transaktionskurs –
        Regeln siehe :func:`app.prices.lookup.price_eur_on`)."""
        from app.prices.lookup import price_eur_on

        pf = self.ctx.portfolio()
        if pf is None or asset_id not in pf.assets:
            return None
        return price_eur_on(self.ctx, pf, asset_id, d, today_local())

    # -- Momentaufnahme ---------------------------------------------------------------------------------------
    def snapshots(self, series_list: Iterable[str]) -> dict[str, MarketSnapshot]:
        keys = list(dict.fromkeys(s for s in series_list if s))
        latest = self.store.latest_many(keys)
        out: dict[str, MarketSnapshot] = {}
        today = today_local()
        for s in keys:
            snap = MarketSnapshot(s, provider=s.split(":", 1)[0] if not s.startswith("demo:") else "demo")
            info, _at = self.store.info(s)
            info = info or {}
            q = latest.get(s)
            factor = 1.0
            if q is not None and q["price"]:
                ccy = (q["ccy"] or "EUR").upper()
                snap.price_native, snap.ccy = float(q["price"]), ccy
                f, _src = self.prices.fx_to_eur(ccy)
                factor = f or 0.0
                if factor:
                    snap.price_eur = q["price"] * factor
                    if q["prev_close"]:
                        snap.change_24h = q["price"] / q["prev_close"] - 1
                    elif q["change_pct"] is not None:
                        snap.change_24h = q["change_pct"] / 100
                snap.ts = parse_iso(q["market_time"] or q["fetched_at"])
            pts = self.eur_series(s, today - timedelta(days=SPARK_DAYS + 7), today)
            if snap.price_eur is None and pts:
                snap.price_eur = pts[-1][1]
                snap.ts = datetime.fromisoformat(pts[-1][0].isoformat() + "T22:00:00+00:00")
                snap.stale = True
            if pts and snap.price_eur:
                ref = [p for p in pts if p[0] <= today - timedelta(days=7)]
                if ref:
                    snap.change_7d = snap.price_eur / ref[-1][1] - 1
            if info.get("change_7d_pct") is not None:  # CoinGecko (exakter als Tagesschluss)
                snap.change_7d = float(info["change_7d_pct"]) / 100
            if info.get("market_cap_eur"):
                snap.market_cap_eur = float(info["market_cap_eur"])
            elif info.get("marketCap") and factor:
                snap.market_cap_eur = float(info["marketCap"]) * factor
            spark = info.get("sparkline_eur")
            if isinstance(spark, list) and len(spark) >= 2:
                snap.sparkline = [float(x) for x in spark]
            else:
                snap.sparkline = [p[1] for p in pts[-SPARK_DAYS:]]
            snap.name = info.get("name") or info.get("longName") or info.get("shortName")
            snap.symbol = info.get("symbol")
            if snap.ts is not None and datetime.now(UTC) - snap.ts > timedelta(days=4):
                snap.stale = True
            out[s] = snap
        return out

    # -- Nachladen ------------------------------------------------------------------------------------------
    def due(self, series_list: Iterable[str]) -> list[str]:
        now = datetime.now(UTC)
        out = []
        for s in dict.fromkeys(series_list):
            _info, at = self.store.info(s)
            if at is None or now - at > REFRESH_TTL:
                out.append(s)
        return out

    def refresh(self, series_list: Iterable[str], force: bool = False) -> dict[str, Any]:
        """Watchlist-Werte nachladen (gebündelt, kontingentschonend). Nur Kennungen gehen an die Anbieter."""
        if not _LOCK.acquire(blocking=False):
            return {"skipped": "läuft bereits"}
        try:
            keys = list(dict.fromkeys(series_list)) if force else self.due(series_list)
            res: dict[str, Any] = {"cg": 0, "yahoo": 0, "errors": []}
            cg_ids = [s.split(":", 1)[1] for s in keys if s.startswith("cg:")]
            ysyms = [s.split(":", 1)[1] for s in keys if s.startswith("yahoo:")]
            if self.prices.demo is not None:
                return self._refresh_demo(keys)
            if cg_ids and self.prices.cg is not None:
                try:
                    res["cg"] = self._refresh_cg(cg_ids)
                except Exception as e:
                    res["errors"].append(f"CoinGecko: {type(e).__name__}: {e}"[:200])
            if ysyms and self.prices.yahoo is not None:
                try:
                    res["yahoo"] = self._refresh_yahoo(ysyms)
                except Exception as e:
                    res["errors"].append(f"Yahoo: {type(e).__name__}: {e}"[:200])
            if res["cg"] or res["yahoo"]:
                self.prices._bump()
            return res
        finally:
            _LOCK.release()

    def _refresh_cg(self, ids: list[str]) -> int:
        data = self.prices.cg.markets(ids, sparkline=True, changes="24h,7d")
        quotes, now = [], datetime.now(UTC)
        for cid, m in data.items():
            price = m.get("current_price")
            if not price:
                continue
            ts = parse_iso(m.get("last_updated")) or now
            ch = m.get("price_change_percentage_24h")
            prev = price / (1 + ch / 100) if isinstance(ch, int | float) and ch > -100 else None
            quotes.append(Quote(f"cg:{cid}", float(price), "EUR", ts, "coingecko", prev_close=prev,
                                change_pct=float(ch) if isinstance(ch, int | float) else None))
            spark = ((m.get("sparkline_in_7d") or {}).get("price") or [])
            step = max(1, len(spark) // 42)
            self.prices._merge_info(f"cg:{cid}", {
                "name": m.get("name"), "symbol": str(m.get("symbol") or "").upper() or None,
                "market_cap_eur": m.get("market_cap"),
                "change_7d_pct": m.get("price_change_percentage_7d_in_currency"),
                "sparkline_eur": [round(float(x), 10) for x in spark[::step] if isinstance(x, int | float)],
            })
        self.store.upsert_quotes(quotes)
        return len(quotes)

    def _refresh_yahoo(self, syms: list[str]) -> int:
        quotes, infos, _errors = self.prices.yahoo.quotes(syms)
        if quotes:
            self.store.upsert_quotes(list(quotes.values()))
        today = today_local()
        n = 0
        for sym in syms:
            if infos.get(sym):
                self.prices._merge_info(f"yahoo:{sym}", infos[sym])
            last = self.store.last_daily(f"yahoo:{sym}")
            if last is None or last["date"] < (today - timedelta(days=3)).isoformat():
                try:
                    bars, ccy = self.prices.yahoo.history(sym, today - timedelta(days=SPARK_DAYS + 10), today)
                    self.store.upsert_daily(f"yahoo:{sym}", bars, "yahoo", ccy)
                except Exception as e:
                    log.info("Watchlist: Historie %s nicht verfügbar: %s", sym, e)
            self.prices._merge_info(f"yahoo:{sym}", {"_watch_refresh": iso(datetime.now(UTC))})
            n += 1
        return n

    def _refresh_demo(self, keys: list[str]) -> dict[str, Any]:
        demo = self.prices.demo
        today = today_local()
        quotes = []
        for s in keys:
            q = demo.quote(s)
            if q is not None:
                quotes.append(q)
            if self.store.last_daily(s) is None:
                bars = demo.history(s, today - timedelta(days=SPARK_DAYS + 10), today)
                self.store.upsert_daily(s, [Bar(b.date, b.close) for b in bars], "demo", "EUR")
            self.prices._merge_info(s, {"_watch_refresh": iso(datetime.now(UTC))})
        self.store.upsert_quotes(quotes)
        return {"demo": len(quotes)}


def market_data(ctx: Any) -> MarketData:
    return MarketData(ctx)
