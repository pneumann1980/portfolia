"""Kursdienst: Zuordnung Asset→Kursreihe, Aktualisierung, Historie (Backfill), Bewertung in EUR, Veraltung.

Grundsatz: Bei Ausfall einer Quelle wird der letzte bekannte Kurs weiterverwendet und als *veraltet*
markiert – niemals still auf 0 gesetzt. Assets ohne Marktkurs werden mit Ersatzkursen (manuell bzw.
Transaktionskurs, begrenzte Gültigkeit, siehe ``app.prices.fallback``) bewertet, sonst gelten sie als
„unbewertet“ (0 €).
"""

from __future__ import annotations

import bisect
import logging
import re
import statistics
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.db import Database
from app.ledger.engine import LedgerResult
from app.ledger.models import AssetInfo, Portfolio
from app.prices.budget import CRYPTO_PRESETS, THROTTLED_PRESETS, snap
from app.prices.coingecko import BudgetExceeded, CoinGeckoProvider
from app.prices.demo import DemoProvider
from app.prices.ecb import EcbProvider
from app.prices.fallback import KIND_LABEL, FallbackPrices
from app.prices.market_hours import exchange_group, in_window, trading_days_between
from app.prices.models import Bar, IntradayBar, PriceInfo, Quote
from app.prices.store import PriceStore
from app.prices.yahoo import YahooProvider
from app.settings_store import Settings
from app.util.http import Quota, SourceGuard
from app.util.timeutil import iso, local_tz, parse_iso, today_local

log = logging.getLogger(__name__)

# Ersatzhistorie (alternativer Anbieter) für die Zeit vor dem Fenster des Hauptanbieters
ALT_RETRY = timedelta(days=7)  # abgelehnte bzw. fehlende Ersatzhistorie höchstens so oft erneut prüfen
ALT_MIN_OVERLAP = 20  # Tage mit Kursen beider Anbieter für die Identitätsprüfung
ALT_MAX_MEDIAN_DEV = 0.06  # Median der relativen Abweichung im Überlappungszeitraum
ALT_CLOSE_DEV = 0.10  # „übereinstimmender“ Tag: Abweichung höchstens 10 %
ALT_MIN_CLOSE_SHARE = 0.6  # mindestens 60 % der gemeinsamen Tage übereinstimmend
ALT_CONFLICT_DEV = 0.15  # Tage mit mehr Abweichung werden als Widerspruch der Anbieter ausgewiesen
ALT_MIN_TX_POINTS = 5  # ohne Überlappung: so viele eigene Transaktionskurse im Zeitraum des Kandidaten
ALT_MAX_TX_MEDIAN_DEV = 0.15  # … mit höchstens dieser medianen Abweichung (Spread, Tagesschwankung)
ALT_MAX_CANDIDATES = 4
_ALT_SYM = re.compile(r"^[A-Z0-9]{1,15}$")
PRIMARY_SOURCES = ("coingecko", "coingecko-eod", "yahoo", "demo")  # Herkunft eines Kurses des Hauptanbieters


@dataclass
class UpdateResult:
    provider: str
    updated: int = 0
    skipped: str | None = None
    errors: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "updated": self.updated, "skipped": self.skipped,
                "errors": (self.errors or [])[:20]}


def _age_text(d: timedelta) -> str:
    minutes = int(d.total_seconds() // 60)
    if minutes < 90:
        return f"{minutes} Min."
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.0f} Std."
    return f"{hours / 24:.0f} Tagen"


class PriceService:
    def __init__(self, db: Database, settings: Settings, store: PriceStore, *, yahoo: YahooProvider | None,
                 coingecko: CoinGeckoProvider | None, ecb: EcbProvider | None, demo: DemoProvider | None = None,
                 guard: SourceGuard | None = None, cg_quota: Quota | None = None) -> None:
        self.db = db
        self.settings = settings
        self.store = store
        self.yahoo = yahoo
        self.cg = coingecko
        self.ecb = ecb
        self.demo = demo
        self.guard = guard or SourceGuard(db)
        self.cg_quota = cg_quota or Quota(db, "coingecko", "month")
        self._intraday_cache: dict[tuple[str, str], tuple[float, list[IntradayBar]]] = {}
        self._info_inflight: set[str] = set()
        self._lock = threading.Lock()
        self.version = 0  # erhöht sich bei jeder Kursaktualisierung (Cache-Invalidierung)
        self.catalog_path: Path | None = None  # CoinGecko-Katalog (Symbole für die Ersatzhistorie), vom Kontext gesetzt

    # -- Zuordnung ----------------------------------------------------------------------------------
    def series_for(self, asset: AssetInfo) -> str | None:
        if asset.is_fiat or not asset.quote_id:
            return None
        if asset.quote_source == "yahoo":
            base = f"yahoo:{asset.quote_id}"
        elif asset.quote_source == "coingecko":
            base = f"cg:{asset.quote_id}"
        else:
            return None
        return f"demo:{base}" if self.demo else base

    def history_fallback_symbol(self, asset: AssetInfo) -> str | None:
        """Ausdrücklich zugeordnetes Yahoo-Symbol für Krypto-Historie vor dem CoinGecko-Fenster (Asset oder
        Einstellung ``prices.crypto_history_fallback``)."""
        explicit = asset.extra.get("history_yahoo") or asset.extra.get("yahoo_history")
        if explicit:
            return str(explicit)
        mapping = self.settings.get("prices.crypto_history_fallback") or {}
        return mapping.get(asset.symbol.upper())

    def history_alt_candidates(self, asset: AssetInfo, series: str) -> list[tuple[str, str]]:
        """Kandidaten für die Krypto-Historie vor dem Fenster des Hauptanbieters: (Yahoo-Symbol, Herkunft).

        Zuerst eine ausdrückliche Zuordnung („manuell“), dann – sofern aktiviert – automatisch ``SYMBOL-EUR`` und
        ``SYMBOL-USD`` für das Symbol des CoinGecko-Coins (Katalog) und des Assets. Ob ein Kandidat wirklich derselbe
        Coin ist, entscheidet erst der Abgleich im Überlappungszeitraum (:meth:`_fill_gap`) – nie das Symbol allein."""
        out: list[tuple[str, str]] = []
        explicit = self.history_fallback_symbol(asset)
        if explicit:
            out.append((explicit.strip(), "manuell"))
        if not self.settings.get("prices.crypto_history_auto", True):
            return out
        syms: list[str] = []
        cid = series.split(":", 1)[1] if series.startswith("cg:") else ""
        if cid and self.catalog_path is not None:
            from app.prices.sources import cached_catalog

            cat = cached_catalog(self.catalog_path)
            coin = cat.by_id.get(cid) if cat is not None else None
            if coin and coin.get("symbol"):
                syms.append(str(coin["symbol"]).upper())
        syms.append(asset.symbol.upper())
        for sym in dict.fromkeys(s for s in syms if _ALT_SYM.match(s)):
            for ccy in ("EUR", "USD"):
                cand = f"{sym}-{ccy}"
                if all(cand != c for c, _ in out):
                    out.append((cand, "automatisch"))
        return out[:ALT_MAX_CANDIDATES]

    # -- Veraltung ----------------------------------------------------------------------------------
    def is_stale(self, asset: AssetInfo, ts: datetime | None, now: datetime | None = None) -> bool:
        if ts is None:
            return True
        now = now or datetime.now(UTC)
        age = now - ts
        if asset.is_crypto:
            return age > timedelta(minutes=float(self.settings.get("prices.stale_crypto_minutes", 60)))
        if age <= timedelta(hours=float(self.settings.get("prices.stale_security_hours", 24))):
            return False
        loc_now = now.astimezone(local_tz())
        loc_ts = ts.astimezone(local_tz())
        days = trading_days_between(loc_ts, loc_now)
        if days and loc_now.weekday() < 5 and loc_now.hour < 10:
            days -= 1  # heutiger Handel hat noch nicht (sicher) begonnen
        return days >= 1

    def _crypto_fetch_reason(self, now: datetime) -> str:
        """Warum Krypto-Kurse nicht aktualisiert wurden (für die Anzeige, ohne Geheimnisse)."""
        b = self.cg_budget()
        if b["exhausted"]:
            return "CoinGecko-Monatskontingent erschöpft – Abruf wieder ab Monatswechsel"
        row = self.db.q1("SELECT next_allowed, last_error FROM source_status WHERE source_id='price:coingecko'")
        nxt = parse_iso(row["next_allowed"]) if row is not None and row["next_allowed"] else None
        if nxt is not None and nxt > now:
            err = f" ({row['last_error'][:80]})" if row["last_error"] else ""
            return f"CoinGecko nach Fehlern pausiert bis {nxt.astimezone(local_tz()).strftime('%H:%M')}{err}"
        if self.cg is None and not self.demo:
            return "CoinGecko nicht konfiguriert"
        if b["throttled"]:
            return "CoinGecko gedrosselt (Kontingent) – Abruf seltener"
        return "letzter Abruf liegt zurück (Container aus oder Abruf fehlgeschlagen)"

    def _crypto_quote_state(self, market: datetime | None, fetched: datetime | None, now: datetime,
                            reason: Callable[[], str]) -> tuple[bool, str | None]:
        """Krypto: „veraltet“ heißt, der letzte erfolgreiche Abruf ist älter als die Grenze. Liefert CoinGecko
        frisch einen Kurs, dessen letzte Änderung lange zurückliegt (wenig Handel), ist er nur veraltet, wenn die
        Änderung länger als ``prices.stale_crypto_market_hours`` her ist – sonst stünden kleine Coins ständig in der
        Warnung, obwohl es keinen neueren Kurs gibt."""
        limit = timedelta(minutes=float(self.settings.get("prices.stale_crypto_minutes", 60)))
        fetched = fetched or market
        if fetched is None:
            return True, "noch kein Kurs abgerufen"
        if now - fetched > limit:
            last = parse_iso(self.db.get_state("last_crypto_update"))
            if last is not None and last - fetched > timedelta(minutes=5) and now - last <= limit:
                why = "CoinGecko liefert für diese ID derzeit keinen Kurs – Zuordnung unter Datenqualität prüfen"
            else:
                why = reason()
            return True, f"letzter Abruf vor {_age_text(now - fetched)}: {why}"
        market_limit = timedelta(hours=float(self.settings.get("prices.stale_crypto_market_hours", 24)))
        if market is not None and now - market > market_limit:
            return True, (f"CoinGecko meldet seit {_age_text(now - market)} keine Kursänderung – wenig oder kein "
                          "Handel")
        return False, None

    # -- Bewertung ----------------------------------------------------------------------------------
    def fx_to_eur(self, ccy: str | None) -> tuple[float | None, str | None]:
        """Faktor für Umrechnung in EUR (Preis_EUR = Preis × Faktor)."""
        if not ccy or ccy.upper() == "EUR":
            return 1.0, "fiat"
        fx = self.store.fx_latest(ccy)
        if fx is None or not fx[0]:
            return None, None
        return 1.0 / fx[0], fx[2]

    def latest_eur_many(self, assets: Iterable[AssetInfo], pf: Portfolio,
                        now: datetime | None = None) -> dict[str, PriceInfo]:
        now = now or datetime.now(UTC)
        assets = list(assets)
        series_map = {a.asset_id: self.series_for(a) for a in assets}
        quotes = self.store.latest_many([s for s in series_map.values() if s])
        fx_cache: dict[str, tuple[float | None, str | None]] = {}

        def fx(ccy: str | None) -> tuple[float | None, str | None]:
            key = (ccy or "EUR").upper()
            if key not in fx_cache:
                fx_cache[key] = self.fx_to_eur(key)
            return fx_cache[key]

        out: dict[str, PriceInfo] = {}
        pending: list[AssetInfo] = []  # ohne Marktkurs → Ersatzkurs
        today = today_local()
        reason_cache: list[str] = []

        def reason() -> str:
            if not reason_cache:
                reason_cache.append(self._crypto_fetch_reason(now))
            return reason_cache[0]

        for a in assets:
            if a.is_fiat:
                if a.asset_id.upper() == "EUR":
                    out[a.asset_id] = PriceInfo(1.0, 1.0, "EUR", now, "fiat", "fiat", False, 1.0)
                else:
                    f, src = fx(a.asset_id)
                    fxl = self.store.fx_latest(a.asset_id)
                    ts = fxl[1] if fxl else None
                    if f is None:
                        out[a.asset_id] = PriceInfo(0.0, None, a.asset_id, None, "none", "unvalued", False,
                                                    note="Devisenkurs fehlt")
                    else:
                        out[a.asset_id] = PriceInfo(f, 1.0, a.asset_id, ts, src or "fx", "fiat",
                                                    self.is_stale(AssetInfo("fx", "fx", "security"), ts, now), f,
                                                    fx_rate=f)
                continue
            s = series_map.get(a.asset_id)
            if s:
                q = quotes.get(s)
                if q is not None:
                    f, _ = fx(q["ccy"])
                    ts = parse_iso(q["market_time"]) or parse_iso(q["fetched_at"])
                    if f is not None:
                        prev = q["prev_close"] * f if q["prev_close"] else None
                        if prev is None:
                            prev = self._prev_close_eur(s, today, f)
                        if a.is_crypto:
                            stale, note = self._crypto_quote_state(parse_iso(q["market_time"]),
                                                                   parse_iso(q["fetched_at"]), now, reason)
                        else:
                            stale, note = self.is_stale(a, ts, now), None
                        out[a.asset_id] = PriceInfo(q["price"] * f, q["price"], q["ccy"], ts, q["source"], "quote",
                                                    stale, prev, fx_rate=f, note=note)
                        continue
                row = self.store.last_daily(s)
                if row is not None:
                    f, _ = fx(row["ccy"])
                    if f is not None:
                        ts = datetime.fromisoformat(row["date"] + "T17:00:00+00:00")
                        out[a.asset_id] = PriceInfo(row["close"] * f, row["close"], row["ccy"], ts, row["source"],
                                                    "daily", True, None, fx_rate=f,
                                                    note="kein aktueller Kurs – letzter Schlusskurs")
                        continue
            pending.append(a)
        if not pending:
            return out
        fb = FallbackPrices(pf, self.settings, only={a.asset_id for a in pending})
        for a in pending:
            out[a.asset_id] = self._fallback_info(a, pf, fb, today, has_series=bool(series_map.get(a.asset_id)))
        return out

    @staticmethod
    def _fallback_info(a: AssetInfo, pf: Portfolio, fb: FallbackPrices, today: date, has_series: bool) -> PriceInfo:
        """Ersatzkurs (manuell/Transaktion) innerhalb seiner Gültigkeit, sonst „unbewertet“ mit Begründung."""
        res = fb.latest(a, today)
        p = res.point
        if p is not None:
            ts = datetime(p.date.year, p.date.month, p.date.day, 12, tzinfo=UTC)
            label = f"{KIND_LABEL[p.kind]} vom {p.date.strftime('%d.%m.%Y')}"
            if p.kind == "manual":
                # Tagesveränderung nur, wenn der Vorwert vom Vortag stammt (sonst irreführend)
                prev = [x for x in pf.manual_prices.get(a.asset_id, []) if x[0] < p.date]
                prev_close = prev[-1][1] if prev and (p.date - prev[-1][0]).days <= 3 \
                    and p.date >= today - timedelta(days=1) else None
                return PriceInfo(p.price, p.price, "EUR", ts, "manual", "manual", False, prev_close, note=label)
            return PriceInfo(p.price, p.price, "EUR", ts, "tx", "tx", False, None,
                             note=f"{label} (keine Marktkurse)")
        if res.expired is not None:
            e = res.expired
            note = (f"{KIND_LABEL[e.kind]} vom {e.date.strftime('%d.%m.%Y')} ist älter als {res.max_age} Tage – "
                    "nicht mehr verwendet")
        else:
            note = "keine Kursquelle" if not has_series else "noch kein Kurs abgerufen"
        return PriceInfo(0.0, None, None, None, "none", "unvalued", False, None, note=note)

    def _prev_close_eur(self, series: str, today: date, f: float) -> float | None:
        row = self.store.last_daily(series, before=today)
        return row["close"] * f if row is not None else None

    # -- Aktualisierung --------------------------------------------------------------------------
    def held_assets(self, pf: Portfolio, ledger: LedgerResult) -> list[AssetInfo]:
        held = ledger.holdings_by_asset()
        return [pf.asset(a) for a, q in held.items() if q > 0]

    def cg_budget(self) -> dict[str, Any]:
        used, _ = self.cg_quota.used()
        limit = int(self.settings.get("prices.coingecko_monthly_limit", 10000))
        now = datetime.now(UTC)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        nxt = (start + timedelta(days=32)).replace(day=1)
        frac = (now - start) / (nxt - start)
        projected = int(used / frac) if frac > 0.02 else None
        pct = used / limit * 100 if limit else 0
        throttle = float(self.settings.get("prices.coingecko_throttle_pct", 80))
        return {"used": used, "limit": limit, "pct": pct, "projected": projected, "throttled": pct >= throttle,
                "exhausted": used >= limit, "throttle_pct": throttle}

    def budget_inputs(self, pf: Portfolio | None, ledger: LedgerResult | None) -> dict[str, Any]:
        """Kennzahlen für die Hochrechnung der Abrufe (nur Anzahlen, unabhängig vom Demo-Modus)."""
        out: dict[str, Any] = {"cg_calls": 0, "cg_held": 0, "cg_history": 0, "cg_sold": 0, "cg_bench": 0,
                               "yahoo_symbols": 0, "fx": False}
        if pf is None or ledger is None:
            return out

        def cg_id(a: AssetInfo) -> str | None:
            return a.quote_id if a.quote_source == "coingecko" and a.quote_id and not a.is_fiat else None

        held = self.held_assets(pf, ledger)
        held_ids = {a.asset_id for a in held}
        ids = sorted({i for a in held if a.is_crypto and (i := cg_id(a))})
        out["cg_held"] = len(ids)
        out["cg_calls"] = len(CoinGeckoProvider.id_chunks(ids)) if ids else 0
        # Gehaltene Coins bekommen ihren Tagesschluss aus dem Kurs (write_eod_closes) – laufende Historien-Abrufe
        # entstehen nur für nicht mehr gehaltene Coins und CoinGecko-Benchmarks.
        ever = [aid for aid in self.first_dates(pf, ledger) if cg_id(pf.asset(aid))]
        bench = [b for b in self.settings.get("performance.benchmarks") or [] if str(b.get("series", "")).startswith(
            "cg:")]
        out["cg_sold"] = sum(1 for aid in ever if aid not in held_ids)
        out["cg_bench"] = len(bench)
        out["cg_history"] = out["cg_sold"] + out["cg_bench"]
        out["yahoo_symbols"] = sum(1 for a in held if a.is_security and a.quote_source == "yahoo" and a.quote_id)
        out["fx"] = bool(self.fx_currencies(pf, ledger))
        return out

    def crypto_due(self, now: datetime | None = None) -> tuple[bool, str | None]:
        now = now or datetime.now(UTC)
        b = self.cg_budget()
        if b["exhausted"]:
            return False, "Monatskontingent erschöpft"
        interval = snap(self.settings.get("prices.crypto_interval_min", 10), CRYPTO_PRESETS, 10)
        if b["throttled"]:
            interval = max(interval, snap(self.settings.get("prices.crypto_throttled_interval_min", 30),
                                          THROTTLED_PRESETS, 30))
        last = parse_iso(self.db.get_state("last_crypto_update"))
        if last and now - last < timedelta(minutes=interval - 0.75):
            return False, "Intervall" + (" (gedrosselt)" if b["throttled"] else "")
        return True, None

    def update_crypto(self, pf: Portfolio, ledger: LedgerResult, force: bool = False) -> UpdateResult:
        res = UpdateResult("coingecko")
        assets = [a for a in self.held_assets(pf, ledger) if a.is_crypto and self.series_for(a)]
        watch = self.watch_ids("coingecko")  # Watchlist: im selben gebündelten Aufruf, kein Zusatzkontingent
        if not assets and not watch:
            res.skipped = "keine Krypto-Positionen mit Kursquelle"
            return res
        if self.demo:
            series = {self.series_for(a) for a in assets} | {f"demo:cg:{w}" for w in watch}
            quotes = [q for s in sorted(x for x in series if x) if (q := self.demo.quote(s))]
            res.updated = self.store.upsert_quotes(quotes)
            self._bump()
            return res
        if self.cg is None:
            res.skipped = "CoinGecko nicht konfiguriert"
            return res
        due, why = self.crypto_due()
        if not due and not force:
            res.skipped = why
            return res
        if not self.guard.allowed("price:coingecko") and not force:
            res.skipped = "Quelle gedrosselt (vorherige Fehler)"
            return res
        ids = sorted({a.quote_id for a in assets if a.quote_id} | set(watch))
        try:
            quotes, errors = self.cg.quotes(ids)
        except BudgetExceeded as e:
            res.skipped = str(e)
            return res
        except Exception as e:
            self.guard.failure("price:coingecko", "price", f"{type(e).__name__}: {e}", "CoinGecko")
            res.errors = [str(e)]
            return res
        res.errors = errors
        if quotes:
            res.updated = self.store.upsert_quotes(quotes.values())
            self.guard.success("price:coingecko", "price", "CoinGecko", items=len(quotes))
            self.db.set_state("last_crypto_update", iso(datetime.now(UTC)))
            self._bump()
        elif errors:
            self.guard.failure("price:coingecko", "price", "; ".join(errors[:3]), "CoinGecko")
        return res

    def watch_ids(self, source: str) -> list[str]:
        """Kennungen der Watchlist-Einträge eines Anbieters (für gebündelte Kursabrufe)."""
        try:
            return [r["quote_id"] for r in self.db.q("SELECT DISTINCT quote_id FROM watchlist_item WHERE "
                                                     "quote_source=?", (source,))]
        except Exception:  # Datenbank vor Migration 15
            return []

    def fx_currencies(self, pf: Portfolio, ledger: LedgerResult | None = None) -> set[str]:
        ccys = {a.asset_id.upper() for a in pf.assets.values() if a.is_fiat and a.asset_id.upper() != "EUR"}
        series = [s for a in pf.assets.values() if (s := self.series_for(a))]
        for r in self.store.latest_many(series).values():
            if r["ccy"] and r["ccy"].upper() != "EUR":
                ccys.add(r["ccy"].upper())
        for s in series:
            row = self.store.last_daily(s)
            if row is not None and row["ccy"] and row["ccy"].upper() != "EUR":
                ccys.add(row["ccy"].upper())
        return {c for c in ccys if len(c) == 3}

    def update_securities(self, pf: Portfolio, ledger: LedgerResult, force: bool = False,
                          now: datetime | None = None) -> UpdateResult:
        res = UpdateResult("yahoo")
        now_local = (now or datetime.now(UTC)).astimezone(local_tz())
        assets = [a for a in self.held_assets(pf, ledger) if a.is_security and self.series_for(a)]
        if self.demo:
            quotes = [q for a in assets if (q := self.demo.quote(self.series_for(a)))]  # type: ignore[arg-type]
            res.updated = self.store.upsert_quotes(quotes)
            self._bump()
            return res
        if self.yahoo is None:
            res.skipped = "Yahoo nicht konfiguriert"
            return res
        symbols = sorted({a.quote_id for a in assets if a.quote_id} | set(self.watch_ids("yahoo")))
        if not force:
            symbols = [s for s in symbols if in_window(exchange_group(s), now_local)]
        fx_syms = [f"EUR{c}=X" for c in sorted(self.fx_currencies(pf, ledger))]
        if fx_syms and (force or in_window("fx", now_local)):
            symbols += fx_syms
        if not symbols:
            res.skipped = "außerhalb der Handelszeiten"
            return res
        if not self.guard.allowed("price:yahoo") and not force:
            res.skipped = "Quelle gedrosselt (vorherige Fehler)"
            return res
        try:
            quotes, infos, errors = self.yahoo.quotes(symbols)
        except Exception as e:
            self.guard.failure("price:yahoo", "price", f"{type(e).__name__}: {e}", "Yahoo Finance")
            res.errors = [str(e)]
            return res
        res.errors = errors
        out: list[Quote] = []
        for sym, q in quotes.items():
            if sym.endswith("=X") and sym.startswith("EUR"):
                ccy = sym[3:6]
                q.series = self.store.fx_series(ccy, "yahoo")
            out.append(q)
        if out:
            res.updated = self.store.upsert_quotes(out)
            for sym, info in infos.items():
                if info and not sym.endswith("=X"):
                    self._merge_info(f"yahoo:{sym}", info)
            # FX-Tagesschluss aus Quotes pflegen (für Historie)
            for q in out:
                if q.series.startswith("fx:yahoo:") and q.market_time:
                    self.store.upsert_daily(q.series, [Bar(date=q.market_time.date(), close=q.price)], "yahoo", None)
            self.guard.success("price:yahoo", "price", "Yahoo Finance", items=len(out))
            self.db.set_state("last_security_update", iso(datetime.now(UTC)))
            self._bump()
        elif errors:
            self.guard.failure("price:yahoo", "price", "; ".join(errors[:3]), "Yahoo Finance")
        return res

    def update_fx_ecb(self, pf: Portfolio, ledger: LedgerResult | None = None) -> UpdateResult:
        res = UpdateResult("ecb")
        if self.ecb is None or self.demo:
            res.skipped = "deaktiviert"
            return res
        ccys = sorted(self.fx_currencies(pf, ledger))
        if not ccys:
            res.skipped = "keine Fremdwährungen"
            return res
        try:
            latest = self.ecb.latest(ccys)
        except Exception as e:
            self.guard.failure("fx:ecb", "fx", f"{type(e).__name__}: {e}", "EZB-Referenzkurse")
            res.errors = [str(e)]
            return res
        for c, (rate, d) in latest.items():
            self.store.upsert_daily(self.store.fx_series(c, "ecb"), [Bar(date=d, close=rate)], "ecb", None)
            res.updated += 1
        self.guard.success("fx:ecb", "fx", "EZB-Referenzkurse", items=res.updated)
        self._bump()
        return res

    def write_eod_closes(self, pf: Portfolio, ledger: LedgerResult) -> int:
        """Tagesschluss für Krypto aus dem letzten Kurs (für Historie/Tagesveränderung)."""
        today = today_local()
        n = 0
        for a in self.held_assets(pf, ledger):
            s = self.series_for(a)
            if not s or not a.is_crypto:
                continue
            q = self.store.latest(s)
            if q is None:
                continue
            ts = parse_iso(q["market_time"]) or parse_iso(q["fetched_at"])
            if ts and ts.astimezone(local_tz()).date() == today:
                n += self.store.upsert_daily(s, [Bar(date=today, close=q["price"])], f"{q['source']}-eod", q["ccy"])
                meta = self.store.meta(s)
                if meta is not None and meta["history_from"] and (meta["history_to"] or "") < today.isoformat():
                    # Tagesschluss aus dem Kurs ersetzt den täglichen Historienabruf (spart CoinGecko-Kontingent)
                    self.store.set_meta(s, history_to=today.isoformat())
        return n

    def _bump(self) -> None:
        with self._lock:
            self.version += 1

    # -- Stammdaten/Analysten ---------------------------------------------------------------------
    def _merge_info(self, series: str, info: dict[str, Any]) -> None:
        old, _ = self.store.info(series)
        merged = {**(old or {}), **info}
        self.store.set_info(series, merged)

    def asset_info(self, asset: AssetInfo, refresh_async: bool = True) -> tuple[dict[str, Any] | None, datetime | None]:
        s = self.series_for(asset)
        if not s or not s.startswith("yahoo:") or self.yahoo is None:
            return None, None
        info, fetched = self.store.info(s)
        full = bool(info and info.get("_full"))
        stale = fetched is None or datetime.now(UTC) - fetched > timedelta(hours=24) or not full
        if stale and refresh_async and s not in self._info_inflight:
            self._info_inflight.add(s)
            threading.Thread(target=self._refresh_info, args=(s,), daemon=True, name="info-refresh").start()
        return info, fetched

    def _refresh_info(self, series: str) -> None:
        try:
            if not self.guard.allowed("price:yahoo"):
                return
            info = self.yahoo.info(series.split(":", 1)[1])  # type: ignore[union-attr]
            info["_full"] = True
            self._merge_info(series, info)
        except Exception as e:
            log.info("Stammdaten für %s nicht verfügbar: %s", series, e)
            self.store.set_meta(series, last_error=str(e)[:300], last_error_at=iso(datetime.now(UTC)))
        finally:
            self._info_inflight.discard(series)
            self.db.close_thread_conn()

    # -- Intraday (Detailansicht 1T/1W) -----------------------------------------------------------
    def intraday(self, asset: AssetInfo, rng: str) -> list[IntradayBar]:
        s = self.series_for(asset)
        if not s:
            return []
        since = datetime.now(UTC) - (timedelta(days=1) if rng == "1T" else timedelta(days=7))
        if self.demo:
            return self.demo.intraday(s, rng)
        if asset.is_security and self.yahoo is not None:
            key = (s, rng)
            cached = self._intraday_cache.get(key)
            if cached and time.monotonic() - cached[0] < 900:
                return cached[1]
            try:
                bars = self.yahoo.intraday(asset.quote_id, rng)  # type: ignore[arg-type]
                self._intraday_cache[key] = (time.monotonic(), bars)
                return bars
            except Exception as e:
                log.info("Intraday %s nicht verfügbar: %s", s, e)
        pts = self.store.intraday(s, since)
        return [IntradayBar(ts=ts, close=p) for ts, p in pts]

    # -- Historie (Backfill) ------------------------------------------------------------------------
    def first_dates(self, pf: Portfolio, ledger: LedgerResult) -> dict[str, date]:
        first: dict[str, date] = {}
        for ev in ledger.qty_events:
            if ev.asset not in first or ev.date < first[ev.asset]:
                first[ev.asset] = ev.date
        return first

    def backfill(self, pf: Portfolio, ledger: LedgerResult, progress: Callable[[dict[str, Any]], None] | None = None,
                 force: bool = False) -> dict[str, Any]:
        """Tagesschlusskurse bis zur ersten Transaktion laden (inkrementell, dauerhaft gecacht)."""
        today = today_local()
        first = self.first_dates(pf, ledger)
        tasks: list[tuple[str, AssetInfo | None, date]] = []
        for aid, d0 in sorted(first.items(), key=lambda x: x[1]):
            a = pf.asset(aid)
            s = self.series_for(a)
            if s:
                tasks.append((s, a, d0 - timedelta(days=7)))
        start_all = min(first.values()) if first else today
        for b in self.settings.get("performance.benchmarks") or []:
            s = b.get("series")
            if s and not self.demo:
                tasks.append((s, None, start_all - timedelta(days=7)))
            elif s and self.demo:
                tasks.append((f"demo:{s}", None, start_all - timedelta(days=7)))
        total = len(tasks) + 1
        # eigene Transaktionskurse: zweite Identitätsprüfung der Ersatzhistorie, wenn sich die Anbieter nicht überlappen
        fb = FallbackPrices(pf, self.settings, only={a.asset_id for _s, a, _d in tasks if a is not None})
        done = 0
        errors: list[str] = []
        fetched = 0
        for series, asset, start in tasks:
            if progress:
                progress({"done": done, "total": total, "current": series, "errors": len(errors)})
            try:
                ref = [(p.date, p.price) for p in fb.points(asset.asset_id)] if asset is not None else None
                fetched += self._backfill_series(series, asset, start, today, force, ref)
            except BudgetExceeded as e:
                errors.append(f"{series}: {e}")
            except Exception as e:
                errors.append(f"{series}: {type(e).__name__}: {e}"[:300])
                self.store.set_meta(series, history_status="error", history_error=str(e)[:300],
                                    last_history_fetch=iso(datetime.now(UTC)))
            done += 1
        # Devisen
        if progress:
            progress({"done": done, "total": total, "current": "Devisenkurse", "errors": len(errors)})
        try:
            fetched += self._backfill_fx(pf, ledger, start_all - timedelta(days=7), today)
        except Exception as e:
            errors.append(f"FX: {type(e).__name__}: {e}"[:300])
        done += 1
        if progress:
            progress({"done": done, "total": total, "current": None, "errors": len(errors)})
        if fetched:
            self._bump()
        return {"series": len(tasks), "rows": fetched, "errors": errors}

    def _needs(self, series: str, start: date, today: date, force: bool) -> tuple[bool, date]:
        meta = self.store.meta(series)
        if force or meta is None or not meta["history_from"]:
            return True, start
        h_from = date.fromisoformat(meta["history_from"])
        h_to = date.fromisoformat(meta["history_to"]) if meta["history_to"] else start
        if h_from > start:
            return True, start
        last_fetch = parse_iso(meta["last_history_fetch"])
        if h_to < today - timedelta(days=1) and (last_fetch is None or
                                                   datetime.now(UTC) - last_fetch > timedelta(hours=6)):
            return True, max(start, h_to - timedelta(days=5))
        return False, start

    def _backfill_series(self, series: str, asset: AssetInfo | None, start: date, today: date, force: bool,
                         ref_points: list[tuple[date, float]] | None = None) -> int:
        """Hauptanbieter inkrementell; bei Krypto danach die Lücke vor dessen Fenster über einen geprüften
        Ersatzanbieter schließen (eigener Wiederholungsrhythmus, kostet kein CoinGecko-Kontingent)."""
        n = 0
        failed: Exception | None = None
        need, fetch_from = self._needs(series, start, today, force)
        if need:
            try:
                n += self._fetch_primary(series, start, fetch_from, today)
            except BudgetExceeded:
                raise
            except Exception as e:  # Hauptanbieter gestört: Ersatzhistorie trotzdem prüfen, Fehler danach melden
                failed = e
        if series.startswith("cg:") and asset is not None and self.yahoo is not None:
            n += self._fill_gap(series, asset, start, today, force, ref_points)
        if failed is not None:
            raise failed
        return n

    def _fetch_primary(self, series: str, start: date, fetch_from: date, today: date) -> int:
        n = 0
        status = "ok"
        if series.startswith("demo:"):
            assert self.demo is not None
            bars = self.demo.history(series, fetch_from, today)
            n = self.store.upsert_daily(series, bars, "demo", "EUR")
        elif series.startswith("yahoo:"):
            if self.yahoo is None:
                return 0
            bars, ccy = self.yahoo.history(series.split(":", 1)[1], fetch_from, today)
            n = self.store.upsert_daily(series, bars, "yahoo", ccy)
        elif series.startswith("cg:"):
            if self.cg is None:
                return 0
            max_days = int(self.settings.get("prices.coingecko_history_days", 365))
            days_needed = (today - fetch_from).days + 2
            days = min(max_days, days_needed)
            bars = self.cg.history(series.split(":", 1)[1], days)
            n = self.store.upsert_daily(series, bars, "coingecko", "EUR")
            if days_needed > max_days:
                status = "partial"  # älter als das Fenster des Tarifs – Ersatzanbieter siehe _fill_gap
        else:
            return 0
        meta = self.store.meta(series)
        prev_from = meta["history_from"] if meta is not None and meta["history_from"] else None
        prev_status = meta["history_status"] if meta is not None else None
        new_from = min(fetch_from.isoformat(), prev_from) if prev_from else fetch_from.isoformat()
        if status == "partial":
            # Anfangsdatum beim Hauptanbieter als abgefragt merken: ältere Kurse liefert er im Tarif ohnehin nicht,
            # erneute Anfragen kosteten nur Kontingent. Die Lücke schließt ggf. der Ersatzanbieter (_fill_gap).
            new_from = min(new_from, start.isoformat())
        elif prev_status == "partial" and fetch_from > start:
            status = "partial"  # inkrementelle Aktualisierung ändert nichts an der Lücke
        self.store.set_meta(series, history_from=new_from, history_to=today.isoformat(),
                            last_history_fetch=iso(datetime.now(UTC)), history_status=status, history_error=None)
        return n

    # -- Ersatzhistorie --------------------------------------------------------------------------------
    def _fill_gap(self, series: str, asset: AssetInfo, start: date, today: date, force: bool,
                  ref_points: list[tuple[date, float]] | None = None) -> int:
        """Lücke zwischen ``start`` und dem ersten Kurs des Hauptanbieters über Yahoo schließen.

        Identitätsprüfung (gleich lautende Symbole anderer Coins werden so abgelehnt):

        1. Überlappung mit dem Hauptanbieter (≥ 20 gemeinsame Tage): Median der Abweichung ≤ 6 % und mindestens 60 %
           der Tage höchstens 10 % auseinander. Bewusst kein Grenzwert für Ausreißer: Ein zeitweiliger Fehler *eines*
           Anbieters (beobachtet: CoinGecko-Kurse einzelner Coins Jan.–März 2026 um 25–35 % über Kraken und Yahoo)
           sagt nichts über die Identität. Solche Widersprüche werden mit Zeitraum in der Notiz ausgewiesen; die
           Kurse des Hauptanbieters bleiben unverändert.
        2. Sonst gegen eigene Transaktionskurse (``ref_points``, ≥ 5 im Zeitraum des Kandidaten): Median ≤ 15 %.
        3. Sonst nur eine ausdrückliche Zuordnung (ungeprüft, als solche gekennzeichnet).

        USD-Paare werden mit dem Devisenkurs desselben Tages (EZB, sonst Yahoo) in EUR umgerechnet. Übernommene Tage
        tragen als Herkunft das Ersatzsymbol (``yahoo:ADA-USD``); Kurse des Hauptanbieters werden nie überschrieben.
        Ergebnis und Grund stehen in ``series_meta`` (``alt_*``) und erscheinen unter Datenqualität."""
        points = self.store.daily_points(series)
        prim = [(d, c) for d, c, _ccy, src in points if src in PRIMARY_SOURCES]
        if not prim:
            return 0  # ohne Kurse des Hauptanbieters ist keine Identitätsprüfung möglich
        first_prim = date.fromisoformat(prim[0][0])
        gap_end = first_prim - timedelta(days=1)
        need_from = start + timedelta(days=7)  # start = erste Buchung − 7 Tage
        if gap_end < need_from:
            return 0  # keine Lücke vor der ersten Buchung
        meta = self.store.meta(series)
        alt_rows = [d for d, _c, _ccy, src in points if src.startswith("yahoo:") and d < first_prim.isoformat()]
        covered = bool(alt_rows) and alt_rows[0] <= need_from.isoformat() \
            and alt_rows[-1] >= (gap_end - timedelta(days=3)).isoformat()
        checked = parse_iso(meta["alt_checked_at"]) if meta is not None and meta["alt_checked_at"] else None
        if not force and meta is not None and meta["alt_status"] == "ok" and covered and checked is not None:
            return 0
        if not force and checked is not None and datetime.now(UTC) - checked < ALT_RETRY:
            return 0
        cands = self.history_alt_candidates(asset, series)
        now = iso(datetime.now(UTC))
        if not cands:
            why = ("automatische Suche ausgeschaltet, keine ausdrückliche Zuordnung"
                   if not self.settings.get("prices.crypto_history_auto", True)
                   else f"Symbol „{asset.symbol}“ ist kein gültiges Yahoo-Symbol – unter Einstellungen → Kurse "
                        "ausdrücklich zuordnen")
            self.store.set_meta(series, alt_series=None, alt_status="none", alt_checked_at=now,
                                alt_note=f"kein Ersatzanbieter: {why}")
            return 0
        prim_map = dict(prim)
        notes: list[str] = []
        for sym, origin in cands:
            try:
                bars, ccy = self.yahoo.history(sym, start, today)  # type: ignore[union-attr]
            except Exception as e:  # unbekanntes Symbol, gestört – nächster Kandidat
                notes.append(f"{sym}: nicht abrufbar ({type(e).__name__})")
                continue
            ccy = (ccy or "").upper()
            conv = self._to_eur(bars, ccy, start, today)
            if not conv:
                notes.append(f"{sym}: keine Kurse" + (f" bzw. kein Devisenkurs {ccy}" if ccy and ccy != "EUR" else ""))
                continue
            check, why = self._identity(sym, conv, prim_map, ref_points or [], origin)
            if check is None:
                notes.append(why)
                continue
            old = [b for d, b in sorted(conv.items()) if d < first_prim]
            if not old:
                notes.append(f"{sym}: keine Kurse vor {first_prim:%d.%m.%Y}")
                continue
            # frühere Ersatzkurse eines anderen Symbols ersetzen (nie Kurse des Hauptanbieters)
            self.db.x("DELETE FROM price_daily WHERE series=? AND date<? AND source LIKE 'yahoo:%' AND source<>?",
                      (series, first_prim.isoformat(), f"yahoo:{sym}"))
            n = self.store.upsert_daily(series, old, f"yahoo:{sym}", "EUR")
            closes = old[0].date <= need_from
            note = (f"{origin}: {sym}" + (f" (umgerechnet aus {ccy} mit EZB-/Yahoo-Devisenkurs)" if ccy != "EUR"
                                          else "") + f"; {check}; Kurse ab {old[0].date:%d.%m.%Y}"
                    + ("" if closes else " – davor weiterhin geschätzt"))
            self.store.set_meta(series, alt_series=f"yahoo:{sym}", alt_status="ok", alt_note=note[:500],
                                alt_checked_at=now, history_status="ok" if closes else "partial")
            log.info("Kurshistorie %s vor %s ergänzt: %s (%d Tage)", series, first_prim, sym, n)
            return n
        self.store.set_meta(series, alt_series=None, alt_status="rejected", alt_checked_at=now,
                            alt_note=("; ".join(notes) or "kein passender Ersatzanbieter")[:500])
        return 0

    @staticmethod
    def _identity(sym: str, conv: dict[date, Bar], prim: dict[str, float], ref: list[tuple[date, float]],
                  origin: str) -> tuple[str | None, str]:
        """(Prüfvermerk bei Übernahme bzw. None, Ablehnungsgrund) – Regeln siehe :meth:`_fill_gap`."""
        pairs = [(d, b.close / prim[d.isoformat()] - 1) for d, b in conv.items()
                 if prim.get(d.isoformat(), 0) > 0]
        if len(pairs) >= ALT_MIN_OVERLAP:
            devs = [abs(x) for _d, x in pairs]
            med = statistics.median(devs)
            share = sum(1 for x in devs if x <= ALT_CLOSE_DEV) / len(devs)
            if med > ALT_MAX_MEDIAN_DEV or share < ALT_MIN_CLOSE_SHARE:
                return None, (f"{sym}: weicht im Überlappungszeitraum ab (Median {med:.1%}, nur {share:.0%} der "
                              f"{len(devs)} Tage innerhalb ±{ALT_CLOSE_DEV:.0%}) – anderer Coin oder zu grobe Kurse")
            note = f"geprüft gegen den Hauptanbieter: {len(devs)} Tage, Abweichung Median {med:.1%}"
            bad = sorted(d for d, x in pairs if abs(x) > ALT_CONFLICT_DEV)
            if bad:
                note += (f"; Anbieter widersprechen sich an {len(bad)} Tagen (> {ALT_CONFLICT_DEV:.0%}, "
                         f"{bad[0]:%d.%m.%Y}–{bad[-1]:%d.%m.%Y}) – Kurse des Hauptanbieters dort unverändert")
            return note, ""
        lo, hi = (min(conv), max(conv)) if conv else (None, None)
        tx = [(d, p) for d, p in ref if lo is not None and hi is not None and lo <= d <= hi and p > 0]
        devs = [abs(conv[d].close / p - 1) for d, p in tx if d in conv]
        if len(devs) >= ALT_MIN_TX_POINTS:
            med = statistics.median(devs)
            if med > ALT_MAX_TX_MEDIAN_DEV:
                return None, (f"{sym}: passt nicht zu den eigenen Transaktionskursen (Median {med:.1%}, "
                              f"{len(devs)} Kurspunkte) – anderer Coin?")
            return (f"geprüft gegen eigene Transaktionskurse: {len(devs)} Kurspunkte, Abweichung Median {med:.1%} "
                    f"(keine Überlappung mit dem Hauptanbieter)"), ""
        if origin == "manuell":
            return (f"ohne Abgleich übernommen ({len(pairs)} gemeinsame Tage, {len(devs)} eigene Kurspunkte) – "
                    "ausdrücklich zugeordnet"), ""
        return None, (f"{sym}: zu wenig Vergleichswerte ({len(pairs)} gemeinsame Tage mit dem Hauptanbieter, "
                      f"{len(devs)} eigene Transaktionskurse)")

    def _to_eur(self, bars: list[Bar], ccy: str, start: date, today: date) -> dict[date, Bar]:
        """Tagesschlusskurse in EUR (Fremdwährung mit dem Devisenkurs ≤ Tag); Tage ohne Devisenkurs entfallen."""
        if not ccy or ccy == "EUR":
            return {b.date: b for b in bars if b.close and b.close > 0}
        rates = self._fx_points(ccy, start, today)
        if not rates:
            return {}
        days = [d for d, _ in rates]
        out: dict[date, Bar] = {}
        for b in bars:
            i = bisect.bisect_right(days, b.date) - 1
            if i < 0 or not b.close or b.close <= 0 or (b.date - days[i]).days > 7:
                continue
            r = rates[i][1]  # Einheiten Fremdwährung je 1 EUR

            def eur(v: float | None, r: float = r) -> float | None:
                return v / r if v is not None else None

            out[b.date] = Bar(date=b.date, close=b.close / r, open=eur(b.open), high=eur(b.high), low=eur(b.low),
                              volume=b.volume, split_factor=b.split_factor)
        return out

    def _fx_points(self, ccy: str, start: date, today: date) -> list[tuple[date, float]]:
        """Devisenkurse (Einheiten ``ccy`` je 1 EUR) aufsteigend; fehlende Historie wird bei der EZB nachgeladen."""
        have = self.store.fx_series_map(ccy)
        first = min(have) if have else None
        if (first is None or first > start.isoformat()) and not self.demo:
            try:
                if self.ecb is not None:
                    hist = self.ecb.history([ccy], start - timedelta(days=7), today)
                    s = self.store.fx_series(ccy, "ecb")
                    self.store.upsert_daily(s, hist.get(ccy, []), "ecb", None)
                    meta = self.store.meta(s)
                    old = meta["history_from"] if meta is not None and meta["history_from"] else None
                    frm = (start - timedelta(days=7)).isoformat()
                    self.store.set_meta(s, history_from=min(frm, old) if old else frm, history_to=today.isoformat(),
                                        last_history_fetch=iso(datetime.now(UTC)), history_status="ok")
                elif self.yahoo is not None:
                    bars, _ = self.yahoo.history(f"EUR{ccy}=X", start - timedelta(days=7), today)
                    self.store.upsert_daily(self.store.fx_series(ccy, "yahoo"), bars, "yahoo", None)
            except Exception as e:
                log.info("Devisenhistorie %s nicht verfügbar: %s", ccy, e)
            have = self.store.fx_series_map(ccy)
        return sorted((date.fromisoformat(d), r) for d, r in have.items() if r)

    def _backfill_fx(self, pf: Portfolio, ledger: LedgerResult, start: date, today: date) -> int:
        if self.demo:
            return 0
        ccys = sorted(self.fx_currencies(pf, ledger))
        n = 0
        if not ccys:
            return 0
        todo = []
        for c in ccys:
            need, frm = self._needs(self.store.fx_series(c, "ecb"), start, today, False)
            if need:
                todo.append((c, frm))
        if todo and self.ecb is not None:
            frm = min(f for _, f in todo)
            try:
                hist = self.ecb.history([c for c, _ in todo], frm, today)
                for c, bars in hist.items():
                    n += self.store.upsert_daily(self.store.fx_series(c, "ecb"), bars, "ecb", None)
                    self.store.set_meta(self.store.fx_series(c, "ecb"), history_from=frm.isoformat(),
                                        history_to=today.isoformat(), last_history_fetch=iso(datetime.now(UTC)),
                                        history_status="ok")
                self.guard.success("fx:ecb", "fx", "EZB-Referenzkurse", items=n)
            except Exception as e:
                self.guard.failure("fx:ecb", "fx", f"{type(e).__name__}: {e}", "EZB-Referenzkurse")
        if self.yahoo is not None:
            for c in ccys:
                s = self.store.fx_series(c, "yahoo")
                need, frm = self._needs(s, start, today, False)
                if not need:
                    continue
                try:
                    bars, _ = self.yahoo.history(f"EUR{c}=X", frm, today)
                    n += self.store.upsert_daily(s, bars, "yahoo", None)
                    self.store.set_meta(s, history_from=frm.isoformat(), history_to=today.isoformat(),
                                        last_history_fetch=iso(datetime.now(UTC)), history_status="ok")
                except Exception as e:
                    log.info("FX-Historie %s über Yahoo nicht verfügbar: %s", c, e)
        return n
