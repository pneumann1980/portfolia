"""0.120 – News schlanker: Aufbewahrung 7 Tage (gemerkte bleiben, Löschen in Portionen), News-Seite ohne Ledger-/
Bewertungsaufbau im Seitenaufruf, ECharts nur auf Seiten mit Diagramm."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.news import held as H
from app.news.module import news_service
from app.news.service import retention_days
from app.util.timeutil import iso
from tests.test_news_intel import add_item, store_content

NOW = datetime.now(UTC)
ROOT = Path(__file__).resolve().parents[1]


def _with_example_import(config):
    import os
    import shutil
    import time

    dst = config.import_dir / "b.zip"
    shutil.copy(ROOT / "examples" / "beispiel-import.zip", dst)
    old = time.time() - 3600
    os.utime(dst, (old, old))


@pytest.fixture
def client(config):
    from tests.test_journal import _client

    _with_example_import(config)
    with _client(config) as c:
        from app.importer.loader import check_import_dir

        ctx = c.app.state.ctx
        if ctx.portfolio() is None:
            assert check_import_dir(ctx.db, config.import_dir, ctx.engine_options()).status == "imported"
            ctx.invalidate_data()
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        c.headers["X-CSRF-Token"] = c.token
        yield c


# ---- Aufbewahrung ----------------------------------------------------------------------------------------------

def test_retention_default_is_seven_days_and_clamped(client):
    ctx = client.app.state.ctx
    assert retention_days(ctx) == 7
    for raw, want in ((1, 3), (200, 90), ("x", 7), (14, 14)):
        ctx.settings.set("news.retention_days", raw)
        assert retention_days(ctx) == want


def test_retention_deletes_in_chunks_keeps_saved_and_recent(client):
    ctx = client.app.state.ctx
    old = [add_item(ctx, f"Alte Meldung {i}", f"https://pub.example.com/o{i}", hours=24 * 8 + i, assets=("BTC",))
           for i in range(5)]
    for i in old:
        store_content(ctx, i)
    recent = add_item(ctx, "Frische Meldung", "https://pub.example.com/r", hours=24 * 6)
    saved = add_item(ctx, "Gemerkte alte Meldung", "https://pub.example.com/s", hours=24 * 30)
    ctx.db.x("UPDATE news_item SET saved_at=? WHERE id=?", (iso(NOW), saved))
    ctx.db.x("INSERT INTO news_event_split(item_id, other_id) VALUES (?,?)", (old[0], recent))
    assert news_service(ctx).retention(chunk=2) == 5
    ids = {r["id"] for r in ctx.db.q("SELECT id FROM news_item")}
    assert ids == {recent, saved}
    ph = ",".join("?" * len(old))
    assert ctx.db.scalar(f"SELECT COUNT(*) FROM news_content WHERE item_id IN ({ph})", old) == 0
    assert ctx.db.scalar(f"SELECT COUNT(*) FROM news_asset WHERE item_id IN ({ph})", old) == 0
    assert ctx.db.scalar("SELECT COUNT(*) FROM news_event_split") == 0
    assert news_service(ctx).retention() == 0


def test_retention_setting_saved_from_news_card(client):
    ctx = client.app.state.ctx
    page = client.get("/settings").text
    assert 'name="retention_days"' in page and "gemerkte ★ bleiben" in page
    form = {"csrf_token": client.token, "section": "news", "min_relevance": "0.2", "dashboard_count": "5",
            "report_hour": "22", "report_minute": "15", "retention_days": "5"}
    r = client.post("/settings/save", data=form, follow_redirects=False)
    assert r.status_code == 303 and ctx.settings.get("news.retention_days") == 5
    client.post("/settings/save", data=form | {"retention_days": "1"}, follow_redirects=False)
    assert ctx.settings.get("news.retention_days") == 3
    assert "5 Tage" not in client.get("/news?tab=sources").text
    assert "3 Tage, gemerkte ★ unbegrenzt" in client.get("/news?tab=sources").text


# ---- News-Seite ohne Bewertungsaufbau --------------------------------------------------------------------------

def test_news_page_uses_held_cache_not_valuation(client, monkeypatch):
    ctx = client.app.state.ctx
    H.forget(ctx)
    first = client.get("/news").text  # ohne Speicher: einmal berechnet
    held = H.held(ctx)
    assert held.options and held.options[0][1] in first
    iid = add_item(ctx, "Bitcoin steigt", "https://pub.example.com/b", assets=(held.options[0][0],))
    ctx.invalidate_data()  # wie nach einem Import: Portfolio/Bewertung wären neu aufzubauen

    def boom(*a, **k):
        raise AssertionError("Seitenaufruf darf Portfolio/Bewertung nicht neu aufbauen")

    monkeypatch.setattr(ctx, "valuation", boom)
    monkeypatch.setattr(ctx, "portfolio", boom)
    page = client.get("/news?status=all").text
    assert held.options[0][1] in page and "Bitcoin steigt" in page
    assert client.get(f"/news/{iid}").status_code == 200  # Leseansicht ebenso


def test_news_jobs_refresh_held_cache(client):
    ctx = client.app.state.ctx
    H.forget(ctx)
    svc = news_service(ctx)
    assets, _ = svc.held()
    cached = H.held(ctx, max_age_s=0.0 + 1e9)
    assert [a.asset_id for a in assets] == [aid for aid, _ in cached.options]
    assert all(cached.name(aid) for aid, _ in cached.options) and cached.name("UNBEKANNT") == "UNBEKANNT"


def test_held_cache_expires_after_max_age(client):
    ctx = client.app.state.ctx
    H.forget(ctx)
    h1 = H.held(ctx)
    assert H.held(ctx) is h1
    h2 = H.held(ctx, max_age_s=-1)
    assert h2 is not h1 and h2.options == h1.options


# ---- ECharts nur bei Bedarf ------------------------------------------------------------------------------------

def test_echarts_loaded_on_demand_only(client):
    news = client.get("/news").text
    assert "echarts.min.js" not in news and "/static/js/charts.js" in news
    js = (ROOT / "app/static/js/charts.js").read_text(encoding="utf-8")
    assert "function needEcharts" in js and "/static/vendor/echarts.min.js" in js
    assert js.count("needEcharts()") >= 2  # Datenabruf und eingebettete Daten
    assert client.get("/static/vendor/echarts.min.js").status_code == 200


# ---- Hinweiszeile ohne Ledger-Neuaufbau im Seitenaufruf --------------------------------------------------------

def test_global_hints_never_compute_in_request_with_background_jobs(client, monkeypatch):
    import threading

    from app.assetchange import detect as D

    ctx = client.app.state.ctx
    import dataclasses

    monkeypatch.setattr(ctx, "config", dataclasses.replace(ctx.config, scheduler_enabled=True))
    D.hints(ctx)  # bekannter Stand
    ctx.invalidate_data()
    calls: list[str] = []
    real = D.hints
    done = threading.Event()

    def spy(c):
        calls.append(threading.current_thread().name)
        try:
            return real(c)
        finally:
            done.set()

    monkeypatch.setattr(D, "hints", spy)
    assert client.get("/news").status_code == 200
    assert done.wait(30)
    assert calls and all(n == "asset-hints" for n in calls)  # nur im Hintergrund gerechnet
    key, _ = D._key(ctx)
    assert D._CACHE[id(ctx)][0] == key  # danach aktuell
    calls.clear()
    client.get("/news")
    assert calls == []  # aktueller Stand: kein weiterer Lauf
