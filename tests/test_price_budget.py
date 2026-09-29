"""Abrufrate der Kursquellen: Hochrechnung, Empfehlung, Einstellungen und Zeitplan."""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.jobs.scheduler import Scheduler
from app.ledger.engine import run_ledger
from app.main import build_app
from app.prices import budget as B
from app.prices.service import PriceService
from app.prices.store import PriceStore
from app.settings_store import Settings
from tests.helpers import portfolio, tx

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "beispiel-import.zip"


def test_snap_to_presets():
    assert B.snap("10", B.CRYPTO_PRESETS, 10) == 10
    assert B.snap(7, B.CRYPTO_PRESETS, 10) == 5  # nächstliegende Vorgabe
    assert B.snap("abc", B.CRYPTO_PRESETS, 10) == 10 and B.snap(-5, B.CRYPTO_PRESETS, 10) == 10
    assert B.snap(100000, B.SECURITY_PRESETS, 15) == 120


def test_estimate_and_recommendation_demo_plan():
    # 1 Aufruf je Aktualisierung, 6 nicht mehr gehaltene Coins, Demo-Kontingent 10.000, Drosselung ab 80 %
    e = B.cg_estimate(10, 1, 6, 10000, 80)
    assert (e.quotes, e.history, e.reserve) == (4384, 92, 500) and e.total == 4976 and e.level == "ok"
    assert B.cg_estimate(5, 1, 6, 10000, 80).level == "tight"  # ≈ 94 %: würde im Monat gedrosselt
    assert B.cg_estimate(2, 1, 6, 10000, 80).level == "over"
    assert B.recommend_crypto(1, 6, 10000, 80) == 10
    assert B.recommend_crypto(1, 400, 10000, 80) == 60  # viel Historie → längeres Intervall
    assert B.recommend_crypto(2, 6, 10000, 80) == 15  # zwei Aufrufe je Aktualisierung (viele Coins)
    assert B.recommend_crypto(0, 0, 10000, 80) == 10  # keine Krypto-Kurse: Standard
    assert B.recommend_crypto(1, 6, 500000, 80) == 5  # großes Kontingent: nicht unter 5 Min. empfohlen


def test_throttled_recommendation_lasts_the_month():
    assert B.recommend_throttled(10, 1, 6, 10000, 80) == 30
    days = B.throttled_days(30, 1, 6, 10000, 80)
    assert days is not None and days >= B.DAYS_PER_MONTH
    assert B.recommend_throttled(10, 1, 150, 10000, 80) == 60  # Historie allein reicht keinen Monat
    assert B.recommend_throttled(60, 1, 6, 10000, 80) >= 120  # mindestens doppelt so lang wie normal


def test_yahoo_estimate_and_stale_hint():
    assert B.yahoo_calls_per_month(15, 13, True) == pytest.approx(2088, abs=5)
    assert B.yahoo_calls_per_month(15, 0, False) == 0
    assert B.yahoo_calls_per_month(15, 13, False) < B.yahoo_calls_per_month(15, 13, True)  # Devisen ganztägig
    assert B.stale_hint(60, 10, 30) is None
    assert "mindestens 240" in B.stale_hint(60, 60, 120)


def test_budget_inputs_count_held_and_sold_coins(db):
    svc = PriceService(db, Settings(db), PriceStore(db), yahoo=None, coingecko=None, ecb=None)
    pf = portfolio([
        tx("b1", "2026-01-05", "buy", frm=("Börse", "EUR", 100), to=("Börse", "BTC", "0.001"), value=100),
        tx("b2", "2026-01-06", "buy", frm=("Börse", "EUR", 100), to=("Börse", "ETH", "0.05"), value=100),
        tx("s2", "2026-02-06", "sell", frm=("Börse", "ETH", "0.05"), to=("Börse", "EUR", 120), value=120),
        tx("b3", "2026-01-07", "buy", frm=("Depot", "EUR", 100), to=("Depot", "WKN:A0B1C2", "2"), value=100),
    ])
    inp = svc.budget_inputs(pf, run_ledger(pf))
    assert inp["cg_held"] == 1 and inp["cg_calls"] == 1  # nur BTC gehalten
    assert inp["cg_sold"] == 1 and inp["cg_history"] == 1  # ETH verkauft: laufende Historie
    assert inp["yahoo_symbols"] == 1
    assert svc.budget_inputs(None, None)["cg_calls"] == 0


def test_crypto_due_uses_snapped_intervals(db):
    svc = PriceService(db, Settings(db), PriceStore(db), yahoo=None, coingecko=None, ecb=None)
    from datetime import UTC, datetime

    svc.settings.set("prices.crypto_interval_min", 30)
    svc.db.set_state("last_crypto_update", (datetime.now(UTC) - timedelta(minutes=20)).isoformat())
    assert svc.crypto_due()[0] is False
    svc.db.set_state("last_crypto_update", (datetime.now(UTC) - timedelta(minutes=31)).isoformat())
    assert svc.crypto_due()[0] is True
    # gedrosselt nie häufiger als normal, auch wenn so gespeichert
    svc.settings.set("prices.crypto_throttled_interval_min", 10)
    svc.settings.set("prices.coingecko_monthly_limit", 100)
    svc.cg_quota.add(90)
    assert svc.crypto_due()[0] is True and svc.cg_budget()["throttle_pct"] == 80


@pytest.fixture
def client(config):
    dst = config.import_dir / "beispiel.zip"
    shutil.copy(SAMPLE, dst)
    import os
    import time

    old = time.time() - 3600
    os.utime(dst, (old, old))
    with TestClient(build_app(config, start_scheduler=False)) as c:
        from app.jobs import tasks

        tasks.import_check(c.app.state.ctx, "test")
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        yield c


def test_settings_show_estimate_and_save_intervals(client):
    page = client.get("/settings").text
    assert "CoinGecko-Hochrechnung" in page and "empfohlen" in page and 'name="crypto_interval_min"' in page
    assert 'name="stock_interval_min"' in page and 'name="crypto_throttled_interval_min"' in page
    r = client.post("/settings/save", data={"section": "prices", "csrf_token": client.token,
                                            "crypto_interval_min": "30", "crypto_throttled_interval_min": "15",
                                            "stock_interval_min": "abc", "stale_crypto_minutes": "45"},
                    follow_redirects=False)
    assert r.status_code == 303
    s = client.app.state.ctx.settings
    assert s.get("prices.crypto_interval_min") == 30
    assert s.get("prices.crypto_throttled_interval_min") == 30  # nie häufiger als normal
    assert s.get("prices.stock_interval_min") == 15  # ungültig → Standard
    page = client.get("/settings").text
    assert "Krypto-Kurse gelten nach 45 Min. als veraltet" in page and "mindestens 60 Min." in page


def test_scheduler_takes_new_intervals_without_restart(client):
    ctx = client.app.state.ctx
    sched = Scheduler(ctx)
    sched.setup_default_jobs()
    assert sched.sched.get_job("prices_crypto").trigger.interval == timedelta(minutes=10)
    assert sched.sched.get_job("prices_securities").trigger.interval == timedelta(minutes=15)
    ctx.settings.set("prices.crypto_interval_min", 20)
    ctx.settings.set("prices.stock_interval_min", 30)
    sched.reschedule_prices()
    assert sched.sched.get_job("prices_crypto").trigger.interval == timedelta(minutes=20)
    assert sched.sched.get_job("prices_securities").trigger.interval == timedelta(minutes=30)
