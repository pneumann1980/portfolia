"""0.118 – News-Tempo: Seitenaufrufe holen nie Bilder aus dem Netz, nicht abrufbare Bilder werden 24 h nicht erneut
versucht, die Neuzuordnung nach Importen arbeitet in Portionen nur über 30 Tage, Reiter wechseln ohne Vollneuladen,
Ladezeiten stehen im Status-Reiter."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import build_app
from app.news import images as IM
from app.news.service import NewsService
from app.util.timeutil import iso
from app.web import accesslog
from tests.test_news_intel import add_item


def png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 30, 30)).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def client(config):
    with TestClient(build_app(config, start_scheduler=False)) as c:
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        yield c


def _with_image(ctx, url="https://img.example.com/a.jpg", **kw):
    iid = add_item(ctx, "Bitcoin steigt deutlich", f"https://news.example.com/{abs(hash(url))}", **kw)
    ctx.db.x("UPDATE news_item SET image_url=? WHERE id=?", (url, iid))
    return iid


def test_page_view_never_downloads_and_prefetch_fills_the_cache(client, monkeypatch):
    ctx = client.app.state.ctx
    calls: list[str] = []
    monkeypatch.setattr(IM, "_download", lambda c, url, allow: calls.append(url) or (png(), "image/png"))
    iid = _with_image(ctx)
    r = client.get(f"/img/news/{iid}")
    assert r.status_code == 404 and r.headers.get("cache-control") == "no-store"
    assert calls == []  # der Seitenaufruf hat nicht geladen
    res = IM.prefetch(ctx)
    assert res["loaded"] == 1 and calls == ["https://img.example.com/a.jpg"]
    r = client.get(f"/img/news/{iid}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/")
    assert "max-age" in r.headers["cache-control"]


def test_failed_image_is_not_retried_for_24_hours(client, monkeypatch):
    ctx = client.app.state.ctx
    calls: list[str] = []
    monkeypatch.setattr(IM, "_download", lambda c, url, allow: calls.append(url) and None)
    _with_image(ctx, "https://dead.example.com/x.jpg")
    assert IM.prefetch(ctx)["failed"] == 1
    assert IM.prefetch(ctx) == {"loaded": 0, "failed": 0, "skipped": 1} and len(calls) == 1
    key = IM.image_key("https://dead.example.com/x.jpg")
    ctx.db.x("UPDATE img_fail SET failed_at=? WHERE key=?", (iso(datetime.now(UTC) - timedelta(hours=25)), key))
    IM.prefetch(ctx)
    assert len(calls) == 2  # nach Ablauf der Sperre wieder versucht
    assert ctx.db.scalar("SELECT tries FROM img_fail WHERE key=?", (key,)) == 2


def test_host_failing_three_times_is_skipped_in_the_run(client, monkeypatch):
    ctx = client.app.state.ctx
    calls: list[str] = []
    monkeypatch.setattr(IM, "_download", lambda c, url, allow: calls.append(url) and None)
    for i in range(6):
        _with_image(ctx, f"https://slow.example.com/{i}.jpg", hours=1 + i)
    res = IM.prefetch(ctx)
    assert len(calls) == 3 and res["failed"] == 3 and res["skipped"] == 3


def test_community_images_are_left_to_the_feed_fetch(client, monkeypatch):
    ctx = client.app.state.ctx
    calls: list[str] = []
    monkeypatch.setattr(IM, "_download", lambda c, url, allow: calls.append(url) and None)
    _with_image(ctx, "https://tg.example.com/1.jpg", source="tgdemo")
    IM.prefetch(ctx, skip_sources=frozenset({"tgdemo"}))
    assert calls == []


def test_rematch_covers_30_days_in_chunks_and_keeps_older_matches(client, monkeypatch):
    ctx = client.app.state.ctx
    old = add_item(ctx, "Alte Meldung zu Bitcoin", "https://n.example.com/old", hours=40 * 24, relevance=0.77)
    ctx.db.x("INSERT INTO news_asset(item_id, asset_id, score) VALUES (?, 'ALT', 0.9)", (old,))
    new = [add_item(ctx, f"Bitcoin Meldung {i}", f"https://n.example.com/{i}", hours=i + 1) for i in range(5)]
    pauses: list[float] = []
    monkeypatch.setattr("app.news.service.time.sleep", lambda s: pauses.append(s))
    res = NewsService(ctx).rematch(chunk=2)
    assert res == {"items": 5, "days": 30}
    assert len(pauses) == 2  # 5 Meldungen in Portionen zu 2 → 3 Portionen, 2 Pausen
    assert ctx.db.scalar("SELECT relevance FROM news_item WHERE id=?", (old,)) == 0.77
    assert ctx.db.scalar("SELECT COUNT(*) FROM news_asset WHERE item_id=? AND asset_id='ALT'", (old,)) == 1
    assert all(ctx.db.scalar("SELECT relevance FROM news_item WHERE id=?", (i,)) is not None for i in new)


def test_tabs_swap_only_the_news_area(client):
    page = client.get("/news").text
    assert '<div id="news-main">' in page
    nav = page.split('<nav class="tabs" aria-label="Bereiche"', 1)[1].split(">", 1)[0]
    assert 'hx-boost="true"' in nav and 'hx-select="#news-main"' in nav and 'hx-target="#news-main"' in nav
    assert page.index('id="news-main"') < page.index('class="tabs" aria-label="Bereiche"')


def test_load_times_are_recorded_and_shown(client):
    accesslog._timings["news"].clear()
    client.get("/news")
    client.get("/news?tab=videos")
    t = accesslog.timings()["news"]
    assert t["n"] == 2 and t["median"] is not None and t["max"] >= t["median"]
    page = client.get("/news?tab=status").text
    assert "Ladezeiten" in page and "News-Seite" in page and "Vorschaubilder" in page
