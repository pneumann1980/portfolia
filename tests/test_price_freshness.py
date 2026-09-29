"""Krypto-Kurse „veraltet“: Alter des Abrufs statt Zeitpunkt der letzten Kursänderung, mit Begründung."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.prices.models import Quote
from app.prices.service import PriceService
from app.prices.store import PriceStore
from app.settings_store import Settings
from app.util.timeutil import iso
from tests.helpers import portfolio, tx


def _setup(db):
    svc = PriceService(db, Settings(db), PriceStore(db), yahoo=None, coingecko=None, ecb=None)
    pf = portfolio([
        tx("b1", "2026-01-05", "buy", frm=("Börse", "EUR", 100), to=("Börse", "BTC", "0.001"), value=100),
        tx("b2", "2026-01-05", "buy", frm=("Börse", "EUR", 100), to=("Börse", "KAS", "1000"), value=100),
    ])
    return svc, pf


def _quote(svc, asset_id, pf, market_age: timedelta, fetched_age: timedelta | None = None) -> None:
    s = svc.series_for(pf.asset(asset_id))
    now = datetime.now(UTC)
    svc.store.upsert_quotes([Quote(s, 1.0, "EUR", now - market_age, "coingecko")])
    if fetched_age is not None:
        svc.db.x("UPDATE quote_latest SET fetched_at=? WHERE series=?", (iso(now - fetched_age), s))


def _info(svc, pf, asset_id):
    return svc.latest_eur_many([pf.asset(asset_id)], pf)[asset_id]


def test_freshly_fetched_illiquid_coin_is_not_stale(db):
    svc, pf = _setup(db)
    _quote(svc, "KAS", pf, market_age=timedelta(hours=3))  # CoinGecko: letzte Änderung vor 3 h, gerade abgerufen
    info = _info(svc, pf, "KAS")
    assert info.stale is False and info.note is None


def test_long_unchanged_coin_is_stale_with_reason(db):
    svc, pf = _setup(db)
    _quote(svc, "KAS", pf, market_age=timedelta(hours=30))
    info = _info(svc, pf, "KAS")
    assert info.stale is True and "keine Kursänderung" in info.note and "30 Std." in info.note
    svc.settings.set("prices.stale_crypto_market_hours", 48)
    assert _info(svc, pf, "KAS").stale is False


def test_old_fetch_is_stale_and_names_the_cause(db):
    svc, pf = _setup(db)
    _quote(svc, "BTC", pf, market_age=timedelta(hours=2), fetched_age=timedelta(hours=2))
    info = _info(svc, pf, "BTC")
    assert info.stale is True and info.note.startswith("letzter Abruf vor 2 Std.")
    # Quelle nach Fehlern pausiert
    svc.guard.failure("price:coingecko", "price", "HTTP 429 Too Many Requests", "CoinGecko")
    svc.guard.failure("price:coingecko", "price", "HTTP 429 Too Many Requests", "CoinGecko")
    svc.guard.failure("price:coingecko", "price", "HTTP 429 Too Many Requests", "CoinGecko")
    info = _info(svc, pf, "BTC")
    assert "pausiert bis" in info.note and "HTTP 429" in info.note
    # Kontingent erschöpft hat Vorrang
    svc.settings.set("prices.coingecko_monthly_limit", 100)
    svc.cg_quota.add(100)
    assert "Monatskontingent erschöpft" in _info(svc, pf, "BTC").note


def test_coin_without_price_while_others_update(db):
    svc, pf = _setup(db)
    _quote(svc, "KAS", pf, market_age=timedelta(hours=3), fetched_age=timedelta(hours=3))
    svc.db.set_state("last_crypto_update", iso(datetime.now(UTC) - timedelta(minutes=5)))
    info = _info(svc, pf, "KAS")
    assert info.stale is True and "keinen Kurs" in info.note


def test_fetch_limit_is_the_configured_minutes(db):
    svc, pf = _setup(db)
    _quote(svc, "BTC", pf, market_age=timedelta(minutes=50), fetched_age=timedelta(minutes=50))
    assert _info(svc, pf, "BTC").stale is False
    svc.settings.set("prices.stale_crypto_minutes", 30)
    assert _info(svc, pf, "BTC").stale is True


def test_dashboard_names_the_reason(config):
    import os
    import shutil
    import time
    from pathlib import Path

    from fastapi.testclient import TestClient

    from app.jobs import tasks
    from app.main import build_app

    dst = config.import_dir / "beispiel.zip"
    shutil.copy(Path(__file__).resolve().parents[1] / "examples" / "beispiel-import.zip", dst)
    os.utime(dst, (time.time() - 3600, time.time() - 3600))
    with TestClient(build_app(config, start_scheduler=False)) as c:
        ctx = c.app.state.ctx
        tasks.import_check(ctx, "test")
        pf = ctx.portfolio()
        btc = next(a for a in pf.assets.values() if a.is_crypto and ctx.prices.series_for(a))
        _quote(ctx.prices, btc.asset_id, pf, market_age=timedelta(hours=3), fetched_age=timedelta(hours=3))
        ctx.invalidate_data()
        page = c.get("/").text
        assert "veraltet – es wird der letzte bekannte Wert verwendet" in page
        assert "letzter Abruf vor 3 Std." in page
