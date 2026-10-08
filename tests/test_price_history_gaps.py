"""Historische Kurslücken: Ursache (CoinGecko-Demo-Fenster) und Behebung über einen geprüften Ersatzanbieter.

Synthetische Kurse, ohne Netz. Abgedeckt: automatische Yahoo-Kandidaten (EUR, USD mit EZB-Umrechnung), Ablehnung
gleich lautender Symbole anderer Coins über den Abgleich im Überlappungszeitraum, Vorrang der ausdrücklichen
Zuordnung, kein Überschreiben von Kursen des Hauptanbieters, Wiederholung frühestens nach 7 Tagen (kein dauerhafter
„partial“-Zustand), Herkunft je Tag in der Historie (Marktkurs, alternativer Anbieter, fortgeschrieben, Schätzung, kein
Kurs) mit gespeicherten Abschnitten.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import pytest

from app.analytics import quality as Q
from app.analytics.history import compute_history, persist_snapshots
from app.analytics.valuation import FlowValuer
from app.ledger.engine import run_ledger
from app.prices.models import Bar
from app.prices.service import PriceService
from app.prices.store import PriceStore
from app.settings_store import Settings
from app.util.timeutil import iso, today_local
from tests.helpers import ASSETS, portfolio, tx

TODAY = today_local()


def wave(d: date) -> float:
    """Gleichmäßig schwankender „echter“ Kurs von ADA in EUR."""
    return 0.5 + 0.2 * math.sin(d.toordinal() / 30.0)


class FakeCoinGecko:
    def __init__(self, fn, window: int = 365) -> None:
        self.fn = fn
        self.window = window
        self.calls: list[tuple[str, int]] = []

    def history(self, cid: str, days: int = 365) -> list[Bar]:
        self.calls.append((cid, days))
        days = min(days, self.window)
        return [Bar(TODAY - timedelta(days=i), self.fn(TODAY - timedelta(days=i))) for i in range(days, 0, -1)]


class FakeYahoo:
    def __init__(self, series: dict[str, tuple[str, object]]) -> None:
        self.series = series  # Symbol → (Währung, Kursfunktion)
        self.calls: list[str] = []

    def history(self, symbol: str, start: date, end: date | None = None) -> tuple[list[Bar], str | None]:
        if not symbol.endswith("=X"):  # Devisen (EURUSD=X) zählen nicht als Kandidaten-Abruf
            self.calls.append(symbol)
        if symbol not in self.series:
            raise ValueError(f"{symbol}: possibly delisted")
        ccy, fn = self.series[symbol]
        end = end or TODAY
        out = []
        d = start
        while d <= end:
            out.append(Bar(d, fn(d)))  # type: ignore[operator]
            d += timedelta(days=1)
        return out, ccy


USD_PER_EUR = 1.10


def make(db, yahoo: FakeYahoo, cg: FakeCoinGecko) -> PriceService:
    svc = PriceService(db, Settings(db), PriceStore(db), yahoo=yahoo, coingecko=cg, ecb=None)  # type: ignore[arg-type]
    svc.settings.set("performance.benchmarks", [])
    # EZB-Kurse USD für die Umrechnung (Einheiten USD je 1 EUR)
    bars = [Bar(TODAY - timedelta(days=i), USD_PER_EUR) for i in range(2000, -1, -1)]
    svc.store.upsert_daily(svc.store.fx_series("USD", "ecb"), bars, "ecb", None)
    return svc


def ada_portfolio(first: date):
    assets = [*ASSETS, {"asset_id": "ADA", "name": "Cardano", "asset_class": "crypto", "quote_source": "coingecko",
                        "quote_id": "cardano", "category": "Krypto: Altcoins", "aliases": "ADA"}]
    return portfolio([tx("b1", first.isoformat(), "buy", frm=("Börse", "EUR", 100), to=("Börse", "ADA", 200),
                         value=100)], assets=assets)


def run_backfill(svc: PriceService, pf, force: bool = False):
    led = run_ledger(pf)
    return svc.backfill(pf, led, force=force), led


def test_gap_closed_with_eur_pair_after_overlap_check(db):
    first = TODAY - timedelta(days=900)
    yahoo = FakeYahoo({"ADA-EUR": ("EUR", lambda d: wave(d) * 1.01)})  # 1 % Abweichung: gleicher Coin
    cg = FakeCoinGecko(wave)
    svc = make(db, yahoo, cg)
    pf = ada_portfolio(first)
    res, led = run_backfill(svc, pf)
    assert not res["errors"]
    meta = svc.store.meta("cg:cardano")
    assert meta["alt_status"] == "ok" and meta["alt_series"] == "yahoo:ADA-EUR"
    assert meta["history_status"] == "ok" and "Median" in meta["alt_note"]
    pts = svc.store.daily_points("cg:cardano")
    assert pts[0][0] <= (first - timedelta(days=7)).isoformat()
    first_cg = (TODAY - timedelta(days=365)).isoformat()
    # Kurse des Hauptanbieters bleiben unverändert, Ersatzkurse nur davor
    assert all(src == "coingecko" for d, _c, _ccy, src in pts if d >= first_cg)
    assert all(src == "yahoo:ADA-EUR" for d, _c, _ccy, src in pts if d < first_cg)
    # kein weiterer CoinGecko-Abruf für die alte Zeit: CoinGecko nur einmal (365 Tage)
    assert cg.calls == [("cardano", 365)]
    # Historie: keine geschätzten Tage mehr, Herkunft je Tag
    hist = compute_history(pf, led, svc.store, svc.series_for, FlowValuer(svc.store, svc.series_for))
    assert hist is not None and not hist.estimated_days.get("ADA")
    aq = hist.quality["ADA"]
    assert aq.state == "complete" and aq.alt_days > 500 and aq.estimated_days == 0
    assert [s.kind for s in aq.segments] == ["alt"] and aq.segments[0].source == "yahoo:ADA-EUR"


def test_usd_pair_converted_with_ecb_rate(db):
    first = TODAY - timedelta(days=600)
    yahoo = FakeYahoo({"ADA-USD": ("USD", lambda d: wave(d) * USD_PER_EUR)})
    svc = make(db, yahoo, FakeCoinGecko(wave))
    pf = ada_portfolio(first)
    run_backfill(svc, pf)
    meta = svc.store.meta("cg:cardano")
    assert meta["alt_series"] == "yahoo:ADA-USD" and "umgerechnet aus USD" in meta["alt_note"]
    old = [p for p in svc.store.daily_points("cg:cardano") if p[3] == "yahoo:ADA-USD"]
    d0 = date.fromisoformat(old[0][0])
    assert old[0][2] == "EUR" and old[0][1] == pytest.approx(wave(d0), rel=1e-9)
    assert yahoo.calls[:2] == ["ADA-EUR", "ADA-USD"]  # EUR-Paar zuerst, existiert hier nicht


def test_same_symbol_other_coin_rejected_and_not_retried_daily(db):
    first = TODAY - timedelta(days=800)
    # Yahoo „ADA-EUR“ ist ein anderer Coin (Faktor 3) – darf nie als Cardano-Historie gelten
    yahoo = FakeYahoo({"ADA-EUR": ("EUR", lambda d: wave(d) * 3)})
    svc = make(db, yahoo, FakeCoinGecko(wave))
    pf = ada_portfolio(first)
    _res, led = run_backfill(svc, pf)
    meta = svc.store.meta("cg:cardano")
    assert meta["alt_status"] == "rejected" and "weicht im Überlappungszeitraum ab" in meta["alt_note"]
    assert meta["history_status"] == "partial"
    assert all(p[3] == "coingecko" for p in svc.store.daily_points("cg:cardano"))
    n_calls = len(yahoo.calls)
    run_backfill(svc, pf)  # gleicher Tag: kein erneuter Versuch (Wiederholung frühestens nach 7 Tagen)
    assert len(yahoo.calls) == n_calls
    svc.store.set_meta("cg:cardano", alt_checked_at=iso(datetime.now(UTC) - timedelta(days=8)))
    run_backfill(svc, pf)
    assert len(yahoo.calls) > n_calls
    # Historie: transparent geschätzt, mit Grund
    hist = compute_history(pf, led, svc.store, svc.series_for, FlowValuer(svc.store, svc.series_for))
    aq = hist.quality["ADA"]
    assert aq.state == "estimated" and aq.days["tx"] == hist.estimated_days["ADA"] > 400
    seg = aq.segments[0]
    assert (seg.kind, seg.source, seg.start) == ("tx", "Transaktionen", first)
    assert "Transaktionskurs" in seg.method and aq.label == "⚠ Historische Kursdaten teilweise geschätzt"
    assert "weicht" in aq.alt_note


def test_explicit_mapping_has_priority_and_settings_change_rechecks(db):
    first = TODAY - timedelta(days=700)
    yahoo = FakeYahoo({"ADA-EUR": ("EUR", lambda d: wave(d) * 3),
                       "ADA9999-EUR": ("EUR", lambda d: wave(d))})
    svc = make(db, yahoo, FakeCoinGecko(wave))
    svc.settings.set("prices.crypto_history_fallback", {"ADA": "ADA9999-EUR"})
    pf = ada_portfolio(first)
    run_backfill(svc, pf)
    meta = svc.store.meta("cg:cardano")
    assert meta["alt_series"] == "yahoo:ADA9999-EUR" and meta["alt_note"].startswith("manuell")
    assert yahoo.calls == ["ADA9999-EUR"]


def test_auto_search_can_be_disabled(db):
    yahoo = FakeYahoo({"ADA-EUR": ("EUR", wave)})
    svc = make(db, yahoo, FakeCoinGecko(wave))
    svc.settings.set("prices.crypto_history_auto", False)
    run_backfill(svc, ada_portfolio(TODAY - timedelta(days=500)))
    meta = svc.store.meta("cg:cardano")
    assert meta["alt_status"] == "none" and not yahoo.calls and meta["history_status"] == "partial"


def test_existing_partial_series_from_older_version_is_repaired(db):
    """Stand vor der Korrektur: 365 Tage CoinGecko, history_from auf das Startdatum gesetzt, Status „partial“ – der
    nächste Backfill ergänzt die Lücke ohne erneuten CoinGecko-Abruf."""
    first = TODAY - timedelta(days=900)
    cg = FakeCoinGecko(wave)
    yahoo = FakeYahoo({"ADA-EUR": ("EUR", wave)})
    svc = make(db, yahoo, cg)
    s = "cg:cardano"
    svc.store.upsert_daily(s, cg.history("cardano", 365), "coingecko", "EUR")
    cg.calls.clear()
    svc.store.set_meta(s, history_from=(first - timedelta(days=7)).isoformat(), history_to=TODAY.isoformat(),
                       last_history_fetch=iso(datetime.now(UTC)), history_status="partial")
    run_backfill(svc, ada_portfolio(first))
    assert cg.calls == []  # Kontingent geschont
    assert svc.store.meta(s)["history_status"] == "ok"


def test_quality_kinds_interp_none_and_persisted_segments(db):
    """Token-Migration: Reihe endet → Kurs wird fortgeschrieben („interpoliert“); Asset ohne Kursquelle →
    Transaktionskurs; ohne Kurspunkt → kein Kurs. Abschnitte landen in price_gap, Herkunft je Tag in
    snapshot_asset_daily.price_kind."""
    first = TODAY - timedelta(days=120)
    assets = [*ASSETS,
              {"asset_id": "MATIC", "name": "Polygon (alt)", "asset_class": "crypto", "quote_source": "coingecko",
               "quote_id": "matic-network", "category": "Krypto: Altcoins", "aliases": "MATIC"},
              {"asset_id": "XYZ", "name": "Ohne Kurs", "asset_class": "crypto", "quote_source": "none",
               "category": "Krypto: Small Caps"}]
    pf = portfolio([
        tx("m", first.isoformat(), "buy", frm=("Börse", "EUR", 100), to=("Börse", "MATIC", 100), value=100),
        tx("x", first.isoformat(), "buy", frm=("Börse", "EUR", 50), to=("Börse", "XYZ", 1000), value=50),
    ], assets=assets)
    store = PriceStore(db)
    end_of_series = TODAY - timedelta(days=40)
    store.upsert_daily("cg:matic-network", [Bar(first - timedelta(days=7) + timedelta(days=i), 1.0)
                                            for i in range((end_of_series - first).days + 8)], "coingecko", "EUR")
    svc = PriceService(db, Settings(db), store, yahoo=None, coingecko=None, ecb=None)
    svc.settings.set("prices.fallback_max_age_crypto_days", 30)
    led = run_ledger(pf)
    hist = compute_history(pf, led, store, svc.series_for, FlowValuer(store, svc.series_for), settings=svc.settings)
    mq = hist.quality["MATIC"]
    assert mq.state == "gaps" and mq.gap_days == (TODAY - end_of_series).days - Q.MAX_CARRY["crypto"]
    assert mq.label.endswith("Tage ohne Marktdaten") and mq.segments[0].kind == "interp"
    assert f"Kurs vom {end_of_series:%d.%m.%Y}" in mq.segments[0].source
    xq = hist.quality["XYZ"]
    kinds = [s.kind for s in xq.segments]
    assert kinds == ["tx", "none"]  # Transaktionskurs 30 Tage gültig, danach kein Kurs (0 €)
    persist_snapshots(db, hist, None)
    rows = db.q("SELECT asset_id, kind, days FROM price_gap ORDER BY asset_id, date_from")
    assert [(r["asset_id"], r["kind"]) for r in rows] == [("MATIC", "interp"), ("XYZ", "tx"), ("XYZ", "none")]
    pk = dict(db.q("SELECT price_kind, COUNT(*) FROM snapshot_asset_daily WHERE asset_id='MATIC' GROUP BY price_kind"))
    assert set(pk) == {"market", "interp"}


def test_diagnosis_and_quality_page_show_estimated_periods(tmp_path):
    """Diagnose (Befund mit Zeitraum, Methode, Quelle) und Datenqualität (Tabelle) – über die gespeicherten
    Abschnitte der letzten Neuberechnung."""
    from fastapi.testclient import TestClient

    from app.config import Config, Secrets
    from app.context import AppContext
    from app.diagnosis.collect import collect
    from app.diagnosis.engine import diagnose
    from app.importer.loader import import_file
    from app.importer.zipbuilder import build_zip
    from app.main import build_app

    imp = tmp_path / "import"
    imp.mkdir()
    cfg = Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                 startup_jobs=False, log_format="text", secrets=Secrets())
    first = TODAY - timedelta(days=100)
    assets = [*ASSETS, {"asset_id": "XYZ", "name": "Ohne Kurs", "asset_class": "crypto", "quote_source": "none",
                        "quote_id": "", "category": "Krypto: Small Caps"}]
    rows = [tx("d", first.isoformat(), "deposit", to=("Börse", "EUR", 500), value=500),
            tx("b", first.isoformat(), "trade", frm=("Börse", "EUR", 50), to=("Börse", "XYZ", 1000), value=50)]
    dst = imp / "k.zip"
    build_zip(dst, transactions=rows, assets=assets, valuation_date=TODAY.isoformat(),
              generated_at=f"{TODAY.isoformat()}T20:00:00Z")
    ctx = AppContext(cfg)
    ctx.startup()
    assert import_file(ctx.db, dst, ctx.engine_options()).status == "imported"
    ctx.invalidate_data()
    ctx.recompute_history(persist=True)
    rep = diagnose(collect(ctx))
    f = next(x for x in rep.findings if x.data.get("type") == "price_history")
    assert f.assets == ["XYZ"] and "Transaktionskurs als Schätzung" in f.evidence[0]
    assert "keine Kursquelle zugeordnet" in f.evidence[0]
    app = build_app(cfg, start_scheduler=False)
    with TestClient(app) as c:
        page = c.get("/quality").text
        assert "Kursqualität der Historie" in page and "Transaktionskurs als Schätzung" in page
        assert "Ohne Kurs" in page


def test_provider_anomaly_window_does_not_block_identity(db):
    """Hauptanbieter mit fehlerhaftem Zeitfenster (+30 % an 70 Tagen, wie bei CoinGecko Jan.–März 2026 beobachtet):
    derselbe Coin wird trotzdem erkannt, der Widerspruch mit Zeitraum ausgewiesen, CoinGecko-Kurse bleiben."""
    bad_from, bad_to = TODAY - timedelta(days=200), TODAY - timedelta(days=130)

    def cg_wave(d: date) -> float:
        return wave(d) * (1.3 if bad_from <= d <= bad_to else 1.0)

    yahoo = FakeYahoo({"ADA-EUR": ("EUR", wave)})
    svc = make(db, yahoo, FakeCoinGecko(cg_wave))
    run_backfill(svc, ada_portfolio(TODAY - timedelta(days=600)))
    meta = svc.store.meta("cg:cardano")
    assert meta["alt_status"] == "ok", meta["alt_note"]
    assert "Anbieter widersprechen sich an 71 Tagen" in meta["alt_note"]
    pt = {d: c for d, c, _x, _s in svc.store.daily_points("cg:cardano")}
    assert pt[(bad_from + timedelta(days=1)).isoformat()] == pytest.approx(cg_wave(bad_from + timedelta(days=1)))


def test_no_overlap_checked_against_own_transaction_prices(db):
    """Ersatzreihe endet vor dem CoinGecko-Fenster (keine Überlappung): Identität über eigene Transaktionskurse."""
    first = TODAY - timedelta(days=900)
    stop = TODAY - timedelta(days=400)

    class EndingYahoo(FakeYahoo):
        def history(self, symbol, start, end=None):
            bars, ccy = super().history(symbol, start, min(end or TODAY, stop))
            return bars, ccy

    rows = [tx("b1", first.isoformat(), "buy", frm=("Börse", "EUR", 100), to=("Börse", "ADA", 200), value=100)]
    for i in range(6):  # eigene Käufe zum (fast) Marktkurs
        d = first + timedelta(days=60 * i + 10)
        rows.append(tx(f"k{i}", d.isoformat(), "buy", frm=("Börse", "EUR", round(wave(d) * 100 * 1.02, 2)),
                       to=("Börse", "ADA", 100), value=round(wave(d) * 100 * 1.02, 2)))
    assets = [*ASSETS, {"asset_id": "ADA", "name": "Cardano", "asset_class": "crypto", "quote_source": "coingecko",
                        "quote_id": "cardano", "category": "Krypto: Altcoins", "aliases": "ADA"}]
    pf = portfolio(rows, assets=assets)
    svc = make(db, EndingYahoo({"ADA-EUR": ("EUR", wave)}), FakeCoinGecko(wave))
    run_backfill(svc, pf)
    meta = svc.store.meta("cg:cardano")
    assert meta["alt_status"] == "ok" and "eigene Transaktionskurse" in meta["alt_note"], meta["alt_note"]
    # anderer Coin ohne Überlappung → abgelehnt
    db.x("DELETE FROM price_daily WHERE source LIKE 'yahoo:%'")
    db.x("UPDATE series_meta SET alt_status=NULL, alt_checked_at=NULL")
    svc.yahoo = EndingYahoo({"ADA-EUR": ("EUR", lambda d: wave(d) * 40)})
    run_backfill(svc, pf)
    meta = svc.store.meta("cg:cardano")
    assert meta["alt_status"] == "rejected" and "passt nicht zu den eigenen Transaktionskursen" in meta["alt_note"]
