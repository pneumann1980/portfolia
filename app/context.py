"""Anwendungskontext: verbindet DB, Einstellungen, Kurse, Ledger und Caches.

Caches werden über Versionszähler invalidiert (Import aktiviert, Kurse aktualisiert, Einstellungen
geändert, Historie neu berechnet). Alle Methoden sind thread-sicher.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import threading
from datetime import UTC, date, datetime
from typing import Any

from app.analytics.history import History, compute_history, persist_snapshots
from app.analytics.valuation import FlowValuer, Valuation, latest_update_time, net_invested, value_positions
from app.config import Config
from app.db import Database
from app.importer.loader import active_import_id, portfolio_from_db
from app.ledger.engine import EngineOptions, LedgerResult, run_ledger
from app.ledger.models import Portfolio
from app.prices.coingecko import CoinGeckoProvider
from app.prices.demo import DemoProvider
from app.prices.ecb import EcbProvider
from app.prices.models import PriceInfo
from app.prices.service import PriceService
from app.prices.store import PriceStore
from app.prices.yahoo import YahooProvider
from app.settings_store import Settings
from app.util.http import Quota, SourceGuard, make_client
from app.util.timeutil import iso, today_local

log = logging.getLogger(__name__)


class AppContext:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.db = Database(config.db_path)
        self.settings = Settings(self.db)
        self.store = PriceStore(self.db)
        self.http = make_client(config.user_agent)
        self.guard = SourceGuard(self.db)
        self.cg_quota = Quota(self.db, "coingecko", "month")
        self.demo = DemoProvider() if config.demo_mode else None
        self.prices = PriceService(
            self.db, self.settings, self.store,
            yahoo=None if config.demo_mode else YahooProvider(config.cache_dir / "yfinance"),
            coingecko=None if config.demo_mode else CoinGeckoProvider(
                self.http, config.secrets.coingecko_api_key, config.coingecko_plan, quota=self.cg_quota),
            ecb=None if config.demo_mode else EcbProvider(self.http, config.fx_frankfurter_url,
                                                          config.fx_frankfurter_fallback_url, config.ecb_hist_url),
            demo=self.demo, guard=self.guard, cg_quota=self.cg_quota,
        )
        self.valuer = FlowValuer(self.store, self.prices.series_for)
        self._lock = threading.RLock()
        self._base_pf: tuple[int, Portfolio] | None = None
        self._rec: tuple[tuple[int, int], Portfolio | None] | None = None
        self._pf: tuple[tuple[int, int], Portfolio] | None = None
        self.overlay_version = 0
        self._ledgers: dict[tuple, LedgerResult] = {}
        self._vals: dict[tuple, Valuation] = {}
        self._hist: tuple[tuple, History | None] | None = None
        self.data_version = 0
        self.history_version = 0
        self.scheduler: Any = None  # wird in main gesetzt
        self.started_at = datetime.now(UTC)

    # -- Lebenszyklus ------------------------------------------------------------------------------
    def startup(self) -> None:
        self.config.ensure_dirs()
        self.db.migrate()
        if self.prices.cg is not None:
            self.prices.cg.monthly_limit = int(self.settings.get("prices.coingecko_monthly_limit", 10000))

    def shutdown(self) -> None:
        with contextlib.suppress(Exception):
            self.http.close()

    # -- Portfolio & Ledger ------------------------------------------------------------------------
    def active_import_id(self) -> int | None:
        return active_import_id(self.db)

    def base_portfolio(self) -> Portfolio | None:
        """Nur der Import (maßgebliche Quelle) – ohne geschätzte/bestätigte Sparplan-Buchungen."""
        iid = self.active_import_id()
        if iid is None:
            return None
        with self._lock:
            if self._base_pf is not None and self._base_pf[0] == iid:
                return self._base_pf[1]
            pf = portfolio_from_db(self.db, iid)
            self._base_pf = (iid, pf)
            if self.demo is not None:
                self._seed_demo(pf)
            return pf

    def recorded_portfolio(self) -> Portfolio | None:
        """Erfasste Buchungen: Import + in der App erfasste Buchungen (Journal) + freigegebene Sparplan-Ausführungen.

        Funktioniert auch ohne Import (nur Journal). Grundlage der Sparplan-Erkennung und des Gesamtexports.
        """
        base = self.base_portfolio()
        key = ((base.import_id or 0) if base is not None else 0, self.overlay_version)
        with self._lock:
            if self._rec is not None and self._rec[0] == key:
                return self._rec[1]
        from app.journal.service import overlay as journal_overlay
        from app.plans.service import overlay_txs

        j_assets, j_txs = journal_overlay(self.db, base)
        pf: Portfolio | None
        if base is None and not j_txs:
            pf = None
        else:
            start = base if base is not None else Portfolio(import_id=None, txs=[], assets={}, accounts={})
            assets = {**j_assets, **start.assets}
            confirmed = overlay_txs(self.db, assets, ("confirmed",))
            pf = start if not (j_txs or j_assets or confirmed) else dataclasses.replace(
                start, txs=[*start.txs, *j_txs, *confirmed], assets=assets)
            if self.demo is not None and pf is not start:
                self._seed_demo(pf)
        with self._lock:
            self._rec = (key, pf)
        return pf

    def portfolio(self) -> Portfolio | None:
        """Erfasste Buchungen plus Sparplan-Schätzungen (``Tx.flag`` = estimated) – Grundlage aller Ansichten."""
        rec = self.recorded_portfolio()
        if rec is None:
            return None
        key = (rec.import_id or 0, self.overlay_version)
        with self._lock:
            if self._pf is not None and self._pf[0] == key:
                return self._pf[1]
        from app.plans.service import overlay_txs

        extra = overlay_txs(self.db, rec.assets, ("estimated",))
        pf = dataclasses.replace(rec, txs=[*rec.txs, *extra]) if extra else rec
        with self._lock:
            self._pf = (key, pf)
        return pf

    def invalidate_overlay(self) -> None:
        """Journal oder Sparplan-Schätzungen geändert: Ledger, Bewertung und Historie neu (Import bleibt gecacht)."""
        with self._lock:
            self.overlay_version += 1
            self._rec = None
            self._pf = None
            self._ledgers.clear()
            self._vals.clear()
            self._hist = None
            self.data_version += 1
            self.history_version += 1

    def _seed_demo(self, pf: Portfolio) -> None:
        anchors: dict[str, tuple[date, float]] = {}
        for t in pf.txs:
            if t.value_eur and t.value_eur > 0 and t.type in ("buy", "sell", "trade"):
                for asset, qty in ((t.to_asset, t.to_qty), (t.from_asset, t.from_qty)):
                    if asset and qty and not pf.asset(asset).is_fiat:
                        s = self.prices.series_for(pf.asset(asset))
                        if s:
                            # Anker auf heutige Stückbasis umrechnen (Splits nach dem Handelstag)
                            anchors[s] = (t.date, float(t.value_eur / qty) / pf.split_factor_after(asset, t.date))
        assert self.demo is not None
        self.demo.anchors = anchors
        self.demo.splits = {s: ev for aid, ev in pf.split_events().items()
                            if (s := self.prices.series_for(pf.asset(aid)))}
        self.demo._cache.clear()

    def engine_options(self, scope: str | None = None) -> EngineOptions:
        overrides = self.settings.get("ledger.cash_overrides") or {}
        return EngineOptions(
            scope=scope or self.settings.get("ledger.scope", "global"),
            unmatched_transfers_as_flows=bool(self.settings.get("ledger.unmatched_transfers_as_flows", True)),
            cash_overrides=tuple(sorted((k, bool(v)) for k, v in overrides.items())),
        )

    def ledger(self, scope: str | None = None, opts: EngineOptions | None = None) -> LedgerResult | None:
        pf = self.portfolio()
        if pf is None:
            return None
        opts = opts or self.engine_options(scope)
        key = (pf.import_id, self.overlay_version, opts)
        with self._lock:
            res = self._ledgers.get(key)
            if res is None:
                res = run_ledger(pf, opts)
                if len(self._ledgers) > 8:
                    self._ledgers.clear()
                self._ledgers[key] = res
            return res

    def invalidate_data(self) -> None:
        with self._lock:
            self._base_pf = None
            self._rec = None
            self._pf = None
            self._ledgers.clear()
            self._vals.clear()
            self._hist = None
            self.data_version += 1

    def invalidate_history(self) -> None:
        with self._lock:
            self._hist = None
            self._vals.clear()
            self.history_version += 1

    # -- Bewertung -----------------------------------------------------------------------------------
    def price_infos(self, pf: Portfolio, ledger: LedgerResult) -> dict[str, PriceInfo]:
        held = ledger.holdings_by_asset()
        return self.prices.latest_eur_many([pf.asset(a) for a in held], pf)

    def resolve_accounts(self, selector: str | None) -> tuple[frozenset[str] | None, str | None]:
        """Filter 'grp:<Depot>' oder 'acc:<Konto>' → Kontenmenge + Bezeichnung."""
        pf = self.portfolio()
        if not selector or pf is None:
            return None, None
        kind, _, name = selector.partition(":")
        if kind == "grp":
            accs = frozenset(a for a in pf.all_accounts() if pf.depot_group(a) == name)
            return accs, name
        if kind == "acc":
            return frozenset({name}), name
        return None, None

    def valuation(self, selector: str | None = None) -> Valuation | None:
        pf = self.portfolio()
        led = self.ledger()
        if pf is None or led is None:
            return None
        key = (pf.import_id, self.overlay_version, self.prices.version, self.settings.version, self.history_version,
               selector)
        with self._lock:
            v = self._vals.get(key)
            if v is not None:
                return v
        accounts, label = self.resolve_accounts(selector)
        prices = self.price_infos(pf, led)
        invested = net_invested(led, pf, self.valuer, accounts=accounts)
        v = value_positions(pf, led, prices, invested, accounts=accounts, last_update=latest_update_time(prices),
                            label=label)
        with self._lock:
            if len(self._vals) > 16:
                self._vals.clear()
            self._vals[key] = v
        return v

    def history(self) -> History | None:
        pf = self.portfolio()
        led = self.ledger()
        if pf is None or led is None:
            return None
        key = (pf.import_id, self.overlay_version, self.history_version, self.settings.version, today_local())
        with self._lock:
            if self._hist is not None and self._hist[0] == key:
                return self._hist[1]
        hist = compute_history(pf, led, self.store, self.prices.series_for, self.valuer)
        with self._lock:
            self._hist = (key, hist)
        return hist

    def history_with_live(self) -> History | None:
        """Historie, deren letzter Tag (heute) mit der Live-Bewertung überschrieben ist."""
        hist = self.history()
        val = self.valuation()
        if hist is None or val is None or hist.n == 0:
            return hist
        if hist.dates[-1] == today_local():
            hist.value[-1] = val.total_value
            idx = hist.asset_index()
            for p in val.positions:
                k = idx.get(p.asset_id)
                if k is not None:
                    hist.asset_value[k][-1] = p.value
                    if p.qty:
                        hist.asset_price[k][-1] = p.price.price_eur
        return hist

    def recompute_history(self, persist: bool = True, kind: str = "backfill") -> History | None:
        self.invalidate_history()
        hist = self.history()
        if hist is not None and persist:
            persist_snapshots(self.db, hist, self.active_import_id(), kind=kind)
        return hist

    # -- Jobstatus -------------------------------------------------------------------------------------
    def job_start(self, job: str) -> None:
        self.db.x(
            """INSERT INTO job_status(job, last_start, running, progress_json) VALUES (?,?,1,NULL)
               ON CONFLICT(job) DO UPDATE SET last_start=excluded.last_start, running=1, progress_json=NULL""",
            (job, iso(datetime.now(UTC))),
        )

    def job_progress(self, job: str, progress: dict[str, Any]) -> None:
        self.db.x("UPDATE job_status SET progress_json=? WHERE job=?", (json.dumps(progress), job))

    def job_end(self, job: str, ok: bool, error: str | None = None, result: dict[str, Any] | None = None) -> None:
        self.db.x(
            "UPDATE job_status SET last_end=?, last_ok=?, last_error=?, running=0, progress_json=? WHERE job=?",
            (iso(datetime.now(UTC)), 1 if ok else 0, (error or "")[:1000] or None,
             json.dumps(result, default=str) if result else None, job),
        )

    def job_status(self) -> dict[str, Any]:
        out = {}
        for r in self.db.q("SELECT * FROM job_status"):
            d = dict(r)
            d["progress"] = json.loads(r["progress_json"]) if r["progress_json"] else None
            out[r["job"]] = d
        return out
