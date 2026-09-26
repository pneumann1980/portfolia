"""Persistenz für Kurse, Devisenkurse und Metadaten (SQLite)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.db import Database
from app.prices.models import Bar, Quote
from app.util.timeutil import iso, parse_iso

FX_SOURCES = ("yahoo", "ecb")


class PriceStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    # -- Tagesschlusskurse ------------------------------------------------------------------------
    def upsert_daily(self, series: str, bars: Iterable[Bar], source: str, ccy: str | None) -> int:
        now = iso(datetime.now(UTC))
        rows = [(series, b.date.isoformat(), b.open, b.high, b.low, float(b.close), b.volume, b.split_factor, ccy,
                 source, now) for b in bars if b.close is not None and b.close == b.close and b.close > 0]
        if not rows:
            return 0
        self.db.xmany(
            """INSERT INTO price_daily(series, date, open, high, low, close, volume, split_factor, ccy, source,
                   fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(series, date) DO UPDATE SET open=excluded.open, high=excluded.high, low=excluded.low,
                   close=excluded.close, volume=excluded.volume, split_factor=excluded.split_factor, ccy=excluded.ccy,
                   source=excluded.source, fetched_at=excluded.fetched_at""",
            rows,
        )
        return len(rows)

    def daily_range(self, series: str, start: date | None = None, end: date | None = None) -> list[Any]:
        sql = "SELECT * FROM price_daily WHERE series=?"
        params: list[Any] = [series]
        if start:
            sql += " AND date>=?"
            params.append(start.isoformat())
        if end:
            sql += " AND date<=?"
            params.append(end.isoformat())
        return self.db.q(sql + " ORDER BY date", params)

    def daily_closes(self, series: str) -> list[tuple[str, float, str | None]]:
        return [(r["date"], r["close"], r["ccy"]) for r in
                self.db.q("SELECT date, close, ccy FROM price_daily WHERE series=? ORDER BY date", (series,))]

    def close_on_or_before(self, series: str, d: date) -> Any:
        return self.db.q1(
            "SELECT date, close, ccy, split_factor FROM price_daily WHERE series=? AND date<=? "
            "ORDER BY date DESC LIMIT 1",
            (series, d.isoformat()),
        )

    def last_daily(self, series: str, before: date | None = None) -> Any:
        if before:
            return self.db.q1("SELECT * FROM price_daily WHERE series=? AND date<? ORDER BY date DESC LIMIT 1",
                              (series, before.isoformat()))
        return self.db.q1("SELECT * FROM price_daily WHERE series=? ORDER BY date DESC LIMIT 1", (series,))

    def coverage(self, series: str) -> tuple[str | None, str | None, int]:
        row = self.db.q1("SELECT MIN(date), MAX(date), COUNT(*) FROM price_daily WHERE series=?", (series,))
        return (row[0], row[1], row[2]) if row else (None, None, 0)

    # -- Aktuelle Kurse ----------------------------------------------------------------------------
    def upsert_quotes(self, quotes: Iterable[Quote], intraday: bool = True) -> int:
        now = datetime.now(UTC)
        rows = []
        intr = []
        for q in quotes:
            if q.price is None or q.price != q.price or q.price <= 0:
                continue
            rows.append((q.series, float(q.price), q.ccy, q.prev_close, iso(q.market_time), iso(now), q.source,
                         q.change_pct, q.market_state))
            if intraday:
                ts = q.market_time or now
                intr.append((q.series, iso(ts), float(q.price)))
        if rows:
            self.db.xmany(
                """INSERT INTO quote_latest(series, price, ccy, prev_close, market_time, fetched_at, source, change_pct,
                       market_state) VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(series) DO UPDATE SET price=excluded.price, ccy=excluded.ccy,
                       prev_close=COALESCE(excluded.prev_close, prev_close), market_time=excluded.market_time,
                       fetched_at=excluded.fetched_at, source=excluded.source, change_pct=excluded.change_pct,
                       market_state=excluded.market_state""",
                rows,
            )
        if intr:
            self.db.xmany("INSERT OR REPLACE INTO quote_intraday(series, ts, price) VALUES (?,?,?)", intr)
        return len(rows)

    def latest(self, series: str) -> Any:
        return self.db.q1("SELECT * FROM quote_latest WHERE series=?", (series,))

    def latest_many(self, series: Iterable[str]) -> dict[str, Any]:
        keys = list(dict.fromkeys(series))
        out: dict[str, Any] = {}
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            ph = ",".join("?" * len(chunk))
            for r in self.db.q(f"SELECT * FROM quote_latest WHERE series IN ({ph})", chunk):
                out[r["series"]] = r
        return out

    def intraday(self, series: str, since: datetime) -> list[tuple[datetime, float]]:
        return [(parse_iso(r["ts"]), r["price"]) for r in  # type: ignore[misc]
                self.db.q("SELECT ts, price FROM quote_intraday WHERE series=? AND ts>=? ORDER BY ts",
                          (series, iso(since)))]

    def prune_intraday(self, keep_days: int = 8) -> int:
        cutoff = iso(datetime.now(UTC) - timedelta(days=keep_days))
        return self.db.x("DELETE FROM quote_intraday WHERE ts<?", (cutoff,)).rowcount

    # -- Devisen (Kurs = Einheiten Fremdwährung je 1 EUR) -------------------------------------------
    @staticmethod
    def fx_series(ccy: str, source: str) -> str:
        return f"fx:{source}:{ccy.upper()}"

    def fx_on_or_before(self, ccy: str, d: date) -> tuple[float, str, str] | None:
        """(Kurs, Datum, Quelle) – Yahoo bevorzugt, EZB als Fallback; letzter Wert ≤ d."""
        if ccy.upper() == "EUR":
            return 1.0, d.isoformat(), "fiat"
        best = None
        for src in FX_SOURCES:
            row = self.close_on_or_before(self.fx_series(ccy, src), d)
            if row is not None and (best is None or row["date"] > best[1]):
                best = (row["close"], row["date"], src)
        return best

    def fx_latest(self, ccy: str) -> tuple[float, datetime | None, str] | None:
        if ccy.upper() == "EUR":
            return 1.0, datetime.now(UTC), "fiat"
        q = self.latest(self.fx_series(ccy, "yahoo"))
        if q is not None:
            return q["price"], parse_iso(q["market_time"] or q["fetched_at"]), "yahoo"
        row = self.last_daily(self.fx_series(ccy, "ecb")) or self.last_daily(self.fx_series(ccy, "yahoo"))
        if row is not None:
            return row["close"], datetime.fromisoformat(row["date"] + "T16:00:00+00:00"), row["source"]
        return None

    def fx_series_map(self, ccy: str) -> dict[str, float]:
        """Alle bekannten Tageskurse (EZB überschrieben durch Yahoo, falls vorhanden)."""
        out: dict[str, float] = {}
        for src in reversed(FX_SOURCES):
            for d, close, _ in self.daily_closes(self.fx_series(ccy, src)):
                out[d] = close
        return out

    # -- Metadaten ---------------------------------------------------------------------------------
    def meta(self, series: str) -> Any:
        return self.db.q1("SELECT * FROM series_meta WHERE series=?", (series,))

    def set_meta(self, series: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(fields)
        ph = ", ".join("?" * len(fields))
        upd = ", ".join(f"{k}=excluded.{k}" for k in fields)
        self.db.x(f"INSERT INTO series_meta(series, {cols}) VALUES (?, {ph}) ON CONFLICT(series) DO UPDATE SET {upd}",
                  [series, *fields.values()])

    def set_info(self, series: str, info: dict[str, Any]) -> None:
        self.set_meta(series, info_json=json.dumps(info, ensure_ascii=False, default=str),
                      info_fetched_at=iso(datetime.now(UTC)))

    def info(self, series: str) -> tuple[dict[str, Any] | None, datetime | None]:
        row = self.meta(series)
        if row is None or not row["info_json"]:
            return None, None
        return json.loads(row["info_json"]), parse_iso(row["info_fetched_at"])
