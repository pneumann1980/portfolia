"""News-/YouTube-Pipeline mit gemockten Quellen, Konfigurations-Persistenz und LLM-Datenschutz."""

import json
import os
import shutil
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from app.context import AppContext
from app.importer.loader import check_import_dir
from app.jobs import tasks
from app.news.config import SourcesConfig
from app.news.dedupe import normalize_title, normalize_url
from app.news.youtube import parse_channel_page, parse_count, parse_duration

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime.now(UTC)


def rss(items):
    body = "".join(
        f"<item><title>{t}</title><link>{link}</link><description>{d}</description>"
        f"<pubDate>{format_datetime(NOW - timedelta(hours=h))}</pubDate></item>" for t, link, d, h in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'.encode()


YT_FEED = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
 <title>Coin Bureau</title>
 <entry><id>yt:video:vid00000001</id><yt:videoId>vid00000001</yt:videoId><yt:channelId>UCqK_GSMbpiV8spgD3ZGloSw</yt:channelId>
  <title>Bitcoin outlook for Q4</title><link rel="alternate" href="https://www.youtube.com/watch?v=vid00000001"/>
  <author><name>Coin Bureau</name></author><published>{(NOW - timedelta(hours=3)).isoformat()}</published>
  <media:group><media:title>Bitcoin outlook</media:title><media:thumbnail url="https://i.ytimg.com/vi/vid00000001/hqdefault.jpg"/>
  <media:description>Macro view on BTC</media:description>
  <media:community><media:statistics views="12345"/></media:community></media:group></entry>
 <entry><id>yt:video:vid00000002</id><yt:videoId>vid00000002</yt:videoId><yt:channelId>UCqK_GSMbpiV8spgD3ZGloSw</yt:channelId>
  <title>This coin will 100x!!!</title><link rel="alternate" href="https://www.youtube.com/watch?v=vid00000002"/>
  <author><name>Coin Bureau</name></author><published>{(NOW - timedelta(hours=5)).isoformat()}</published></entry>
</feed>""".encode()

CHANNEL_PAGE = ('<html><head><meta property="og:title" content="Coin Bureau">'
                '<link rel="canonical" href="https://www.youtube.com/channel/UCqK_GSMbpiV8spgD3ZGloSw"></head>'
                '<script>var ytInitialData = {"metadata":{"channelMetadataRenderer":{"title":"Coin Bureau",'
                '"externalId":"UCqK_GSMbpiV8spgD3ZGloSw"}},"header":{"metadataParts":['
                '{"text":{"content":"@CoinBureau"}},{"text":{"content":"2.51M subscribers"}}]}};</script></html>')


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    host = request.url.host
    if host == "www.coindesk.com":
        return httpx.Response(200, content=rss([
            ("Bitcoin tops $100k as ETF inflows surge", "https://www.coindesk.com/a/1?utm_source=rss", "BTC rally", 2),
            ("Fed minutes: what to watch", "https://www.coindesk.com/a/2", "macro", 3),
            ("Kaspa miners expand capacity", "https://www.coindesk.com/a/3", "The KAS network grows", 4),
            ("Kaspa will 100x - last chance", "https://www.coindesk.com/a/4", "crypto token", 1),
        ]))
    if host == "cointelegraph.com":
        return httpx.Response(200, content=rss([
            ("Bitcoin tops $100k as ETF inflows surge - Cointelegraph", "https://cointelegraph.com/x", "", 2)]))
    if host == "news.google.com":
        return httpx.Response(200, content=rss([("Weekly market wrap", f"https://news.example.com/{hash(url)}", "",
                                                  6)]))
    if host == "www.binance.com":
        return httpx.Response(200, json={"data": {"catalogs": [{"catalogName": "New Listings", "articles": [
            {"code": "abc", "title": "Binance Will List Kaspa (KAS)", "releaseDate": int(NOW.timestamp() * 1000)},
            {"code": "def", "title": "Binance Will Delist XYZ", "releaseDate": int(NOW.timestamp() * 1000)}]}]}})
    if host == "www.youtube.com" and request.url.path == "/@CoinBureau":
        return httpx.Response(200, text=CHANNEL_PAGE)
    if host == "www.youtube.com" and request.url.path.startswith("/@"):
        return httpx.Response(404, text="not found")
    if host == "www.youtube.com" and request.url.path == "/feeds/videos.xml":
        return httpx.Response(200, content=YT_FEED)
    if host == "www.youtube.com" and request.url.path.startswith("/shorts/"):
        return httpx.Response(303, headers={"location": "https://www.youtube.com/watch?v=x"})
    return httpx.Response(503)


@pytest.fixture
def ctx(config, tmp_path):
    dst = config.import_dir / "b.zip"
    shutil.copy(ROOT / "examples" / "beispiel-import.zip", dst)
    old = time.time() - 3600
    os.utime(dst, (old, old))
    c = AppContext(config)
    c.startup()
    assert check_import_dir(c.db, config.import_dir, c.engine_options()).status == "imported"
    c.invalidate_data()
    tasks.refresh_prices(c, force=True)
    # Nur ausgewählte Quellen aktiv lassen
    config.sources_path.write_text((ROOT / "examples" / "sources.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    cfg = SourcesConfig(config.sources_path, ROOT / "examples" / "sources.yaml")
    keep = {"coindesk", "cointelegraph", "google_news_en", "binance_announcements"}
    for s in cfg.sources():
        cfg.set_source(s.id, active=s.id in keep)
    c.http = httpx.Client(transport=httpx.MockTransport(handler))
    return c


def test_pipeline_end_to_end(ctx):
    from app.news.module import news_service

    svc = news_service(ctx)
    svc.yt.limiter.min_interval = 0
    for lim in svc._limiters.values():
        lim.min_interval = 0
    import app.news.service as S

    S.HOST_INTERVALS.clear()
    res = svc.run()
    assert not [e for e in res["errors"] if "coindesk" in e]
    rows = {r["title"]: r for r in ctx.db.q("SELECT * FROM news_item")}
    # irrelevante Meldung ohne Asset-Bezug nicht gespeichert (require_match)
    assert "Fed minutes: what to watch" not in rows
    btc = rows["Bitcoin tops $100k as ETF inflows surge"]
    assert btc["dup_count"] == 1  # Cointelegraph-Duplikat über Titelähnlichkeit erkannt
    assets = {r["asset_id"] for r in ctx.db.q("SELECT asset_id FROM news_asset WHERE item_id=?", (btc["id"],))}
    assert assets == {"BTC"}
    assert rows["Kaspa will 100x - last chance"]["hidden_reason"].startswith("reißerisch")
    kas = rows["Kaspa miners expand capacity"]
    assert {r["asset_id"] for r in ctx.db.q("SELECT asset_id FROM news_asset WHERE item_id=?", (kas["id"],))} == {"KAS"}
    # Börsen-Ankündigung nur für gehaltenes Asset
    assert "Binance Will List Kaspa (KAS)" in rows and "Binance Will Delist XYZ" not in rows
    # YouTube: Handle aufgelöst, Video gespeichert, Shorts-Prüfung, reißerisches Video ausgeblendet
    ch = ctx.db.q1("SELECT * FROM yt_channel WHERE handle='@CoinBureau'")
    assert ch["channel_id"] == "UCqK_GSMbpiV8spgD3ZGloSw" and ch["subscribers"] == 2510000
    unresolvable = ctx.db.q1("SELECT * FROM yt_channel WHERE handle='@RaoulPalTJM'")
    assert unresolvable["status"] == "unresolvable"
    vids = {r["video_id"]: r for r in ctx.db.q("SELECT * FROM news_item WHERE kind='video'")}
    assert vids["vid00000001"]["is_short"] == 0 and vids["vid00000001"]["views"] == 12345
    assert vids["vid00000002"]["hidden_reason"]
    # Seiten rendern
    from fastapi.testclient import TestClient

    from app.web.app import create_app

    app = create_app(ctx)
    with TestClient(app) as client:
        for tab in ("articles", "videos", "sources", "suggestions"):
            assert client.get(f"/news?tab={tab}").status_code == 200
        page = client.get("/news?tab=articles&days=7&sort=time").text
        assert "Bitcoin tops $100k" in page and "100x" not in page
    # Rematch nach Import behält Zuordnungen
    assert svc.rematch()["items"] >= 4


def test_sources_config_roundtrip_keeps_comments(tmp_path):
    p = tmp_path / "sources.yaml"
    cfg = SourcesConfig(p, ROOT / "examples" / "sources.yaml")
    cfg.ensure()
    cfg.add_channel("UC123456789012345678901a", "Neuer Kanal", "@neu", language="de")
    cfg.list_channel("blocked_channels", "UCbad", "Spam")
    text = p.read_text(encoding="utf-8")
    assert "# Portfolia – Quellen für News & Videos" in text  # Kommentare bleiben erhalten
    cfg2 = SourcesConfig(p, ROOT / "examples" / "sources.yaml")
    ch = next(c for c in cfg2.channels() if c.channel_id == "UC123456789012345678901a")
    assert ch.confirmed and ch.handle == "@neu" and ch.weight == 0.6
    assert "UCbad" in cfg2.blocked_ids()


def test_youtube_parsers():
    assert parse_channel_page(CHANNEL_PAGE) == ("UCqK_GSMbpiV8spgD3ZGloSw", "Coin Bureau", 2510000)
    assert parse_count("1,2 Mio. Abonnenten") == 1200000
    assert parse_count("987 subscribers") == 987
    assert parse_duration("PT1H2M3S") == 3723 and parse_duration("PT45S") == 45


def test_dedupe_helpers():
    assert normalize_url("https://www.Example.com/a/?utm_source=x&b=2&fbclid=1#frag") == "https://example.com/a?b=2"
    assert normalize_url("https://youtu.be/abc123") == "youtube:abc123"
    assert normalize_title("Bitcoin tops $100k - Cointelegraph") == normalize_title("Bitcoin tops $100k")


def test_llm_payload_contains_no_portfolio_data(ctx):
    from app.news.llm import LlmService

    ctx.db.x("""INSERT INTO news_item(kind, url, url_norm, title, title_norm, summary, source_id, source_name,
               source_weight, language, published_at, fetched_at, relevance) VALUES
               ('article','https://x/1','https://x/1','Kaspa hits high','kaspa hits high','Kaspa rallies',
               'coindesk','CoinDesk',0.7,'en',?,?,0.9)""", (NOW.isoformat(), NOW.isoformat()))
    item_id = ctx.db.scalar("SELECT id FROM news_item WHERE url='https://x/1'")
    ctx.db.x("INSERT INTO news_asset(item_id, asset_id, score) VALUES (?, 'KAS', 0.9)", (item_id,))
    llm = LlmService(ctx)
    payload = llm.build_payload(llm.candidates())
    data = json.loads(payload)
    assert data["meldungen"][0]["assets"] == ["Kaspa"]
    val = ctx.valuation()
    for acc in ("Börse X", "Depot A", "Hardware-Wallet"):
        assert acc not in payload
    kas = next(p for p in val.positions if p.asset_id == "KAS")
    assert f"{kas.qty:g}" not in payload and f"{kas.value:.2f}" not in payload


def test_llm_call_uses_structured_output_budget_and_fallbacks(ctx, monkeypatch):
    from types import SimpleNamespace

    from app.news.llm import SUMMARY_SCHEMA, LlmService

    calls = []

    class FakeMessages:
        def create(self, **kw):
            calls.append(kw)
            return SimpleNamespace(stop_reason="end_turn", usage=SimpleNamespace(input_tokens=120, output_tokens=80),
                                   content=[SimpleNamespace(type="text", text='{"items": []}')])

    llm = LlmService(ctx)
    llm._client = SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages()), messages=FakeMessages())
    ctx.settings.set("llm.daily_token_budget", 10000)
    out = llm._call("sys", "payload", 1000, SUMMARY_SCHEMA)
    assert out == '{"items": []}'
    kw = calls[0]
    assert kw["model"] == "claude-opus-5" and kw["fallbacks"] == "default"
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["output_config"]["effort"] == "low" and kw["output_config"]["format"]["type"] == "json_schema"
    assert llm.usage_today()[:2] == (120, 80)
    ctx.settings.set("llm.daily_token_budget", 500)
    assert llm._call("sys", "payload", 1000, None) is None  # Budget reicht nicht → kein Aufruf
    assert len(calls) == 1
