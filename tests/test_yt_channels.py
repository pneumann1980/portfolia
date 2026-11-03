"""0.86.0: YouTube-Kanäle selbst hinzufügen – Suche nach Name, @Handle, Kanal- oder Video-Link, Prüfung über den
Kanal-Feed, Eintrag als „Eigene“, Freigabe ignorierter/blockierter Kanäle, Entfernen. Ohne Netz."""

from __future__ import annotations

import json

import httpx
import pytest

from app.news import ytchannels as Y
from app.news.module import news_service

CID, CID2, CID3 = "UC" + "a" * 22, "UC" + "b" * 22, "UC" + "c" * 22
FEED = ('<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">'
        '<title>{t}</title><author><name>{t}</name></author></feed>')


def results_page(*renderers: dict) -> str:
    data = {"contents": {"twoColumnSearchResultsRenderer": {"primaryContents": {"sectionListRenderer": {"contents": [
        {"itemSectionRenderer": {"contents": [{"channelRenderer": r} for r in renderers]}}]}}}}}
    return f"<html><script>var ytInitialData = {json.dumps(data)};</script></html>"


def renderer(cid: str, title: str, *, handle: str | None = None, subs: str | None = None, new_layout: bool = True):
    r: dict = {"channelId": cid, "title": {"simpleText": title},
               "descriptionSnippet": {"runs": [{"text": "Krypto "}, {"text": "News"}]}}
    if handle:
        r["navigationEndpoint"] = {"browseEndpoint": {"canonicalBaseUrl": f"/{handle}"}}
    if new_layout:  # aktuelles Layout: Handle in subscriberCountText, Abonnenten in videoCountText
        r["subscriberCountText"] = {"simpleText": handle or ""}
        r["videoCountText"] = {"simpleText": subs or ""}
    else:
        r["subscriberCountText"] = {"simpleText": subs or ""}
    return r


@pytest.mark.parametrize(("text", "expected"), [
    (CID, ("channel", CID)),
    (f"https://www.youtube.com/channel/{CID}/videos", ("channel", CID)),
    ("@CoinBureau", ("handle", "@CoinBureau")),
    ("youtube.com/@CoinBureau", ("handle", "@CoinBureau")),
    ("https://m.youtube.com/@Coin.Bureau-1/featured", ("handle", "@Coin.Bureau-1")),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10", ("video", "dQw4w9WgXcQ")),
    ("https://youtu.be/dQw4w9WgXcQ?si=x", ("video", "dQw4w9WgXcQ")),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", ("video", "dQw4w9WgXcQ")),
    ("  Coin   Bureau  ", ("text", "Coin Bureau")),
])
def test_parse_input(text, expected):
    assert Y.parse_input(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "https://www.youtube.com/playlist?list=PL123", "https://youtube.com/",
                                  "A" * 201])
def test_parse_input_rejects(text):
    with pytest.raises(Y.ChannelError):
        Y.parse_input(text)


def test_parse_search_page_both_layouts_and_dedupe():
    html = results_page(renderer(CID, "Coin Bureau", handle="@CoinBureau", subs="2.6M subscribers"),
                        renderer(CID2, "Alt", subs="12,3 Tsd. Abonnenten", new_layout=False),
                        renderer(CID, "Coin Bureau", handle="@CoinBureau"),  # doppelt
                        {"channelId": "kaputt", "title": {"simpleText": "x"}})
    out = Y.parse_search_page(html)
    assert [(x["channel_id"], x["title"], x["handle"], x["subscribers"]) for x in out] == [
        (CID, "Coin Bureau", "@CoinBureau", 2_600_000), (CID2, "Alt", None, 12_300)]
    assert out[0]["description"] == "Krypto News"
    assert Y.parse_search_page("<html>keine Daten</html>") == []
    assert Y.parse_search_page("<script>var ytInitialData = {kaputt</script>") == []


def test_parse_feed_title():
    assert Y.parse_feed_title(FEED.format(t="Coin Bureau").encode()) == "Coin Bureau"
    assert Y.parse_feed_title(b"<html><body>Fehler</body></html>") is None


@pytest.fixture
def client(config):
    from tests.test_journal import _client

    with _client(config) as c:
        c.get("/settings")
        c.headers["X-CSRF-Token"] = c.cookies.get("portfolia_csrf")
        yield c


class FakeYouTube:
    """Ergebnisseite, Kanalseite (Handle), oEmbed und Kanal-Feeds."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.results = results_page(renderer(CID, "Krypto Neu", handle="@KryptoNeu", subs="2.6M subscribers"),
                                    renderer(CID2, "Zweiter Kanal", handle="@Zweiter", subs="150K subscribers"),
                                    renderer(CID3, "Coin Bureau", handle="@CoinBureau", subs="2.6M subscribers"))

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        path = req.url.path
        if path == "/results":
            assert req.url.params["sp"] == "EgIQAg==" and req.url.params["search_query"]
            return httpx.Response(200, text=self.results)
        if path == "/feeds/videos.xml":
            cid = req.url.params["channel_id"]
            if cid == CID3:
                return httpx.Response(404)
            return httpx.Response(200, text=FEED.format(t={CID: "Krypto Neu", CID2: "Zweiter Kanal"}[cid]))
        if path == "/oembed":
            return httpx.Response(200, json={"author_name": "Krypto Neu", "author_url": "https://www.youtube.com/@KryptoNeu"})
        if path == "/@KryptoNeu":
            return httpx.Response(200, text=f'<meta property="og:title" content="Krypto Neu">"externalId":"{CID}"')
        return httpx.Response(404)


@pytest.fixture
def yt(client):
    fake = FakeYouTube()
    svc = news_service(client.app.state.ctx)
    http = httpx.Client(transport=httpx.MockTransport(fake.handler), follow_redirects=True)
    old = (svc.http, svc.yt.client, svc.yt.api_key, svc.yt.limiter.min_interval)
    svc.http = svc.yt.client = http
    svc.yt.api_key = None
    svc.yt.limiter.min_interval = 0
    yield fake
    svc.http, svc.yt.client, svc.yt.api_key, svc.yt.limiter.min_interval = old


def test_search_add_and_remove_own_channel(client, yt):
    page = client.get("/news?tab=suggestions").text
    assert ">Kanäle" in page and "Kanal hinzufügen" in page and 'hx-get="/youtube/search"' in page
    r = client.get("/youtube/search", params={"q": "krypto"})
    assert r.status_code == 200 and "Krypto Neu" in r.text and "@KryptoNeu" in r.text and "2.600.000" in r.text
    assert "Hinzufügen" in r.text and "YouTube-Suche" in r.text
    assert r.text.count("bereits abonniert") == 1  # @CoinBureau steht schon in sources.yaml (nur als Handle)
    r = client.post("/youtube/channel/add", data={"channel_id": CID, "handle": "@KryptoNeu", "language": "de"})
    assert "hinzugefügt" in r.text
    svc = news_service(client.app.state.ctx)
    ch = next(c for c in svc.cfg.channels() if c.channel_id == CID)
    assert (ch.title, ch.handle, ch.group, ch.language, ch.active, ch.confirmed) == (
        "Krypto Neu", "@KryptoNeu", "Eigene", "de", True, True)
    # erneute Suche: als abonniert markiert; Handle und Video-Link führen zum selben Kanal
    assert client.get("/youtube/search", params={"q": "krypto"}).text.count("bereits abonniert") == 2
    assert "bereits abonniert" in client.get("/youtube/search", params={"q": "@KryptoNeu"}).text
    assert "bereits abonniert" in client.get("/youtube/search",
                                             params={"q": "https://youtu.be/dQw4w9WgXcQ"}).text
    sources = client.get("/news?tab=sources").text
    assert "+ Kanal hinzufügen" in sources and '"action": "remove"' in sources
    assert client.post("/youtube/channel", data={"key": "@KryptoNeu", "action": "remove"}).status_code == 204
    assert not any(c.channel_id == CID for c in svc.cfg.channels())
    # nicht selbst hinzugefügte Kanäle lassen sich nicht entfernen (nur deaktivieren)
    other = next((c for c in svc.cfg.channels() if c.group != "Eigene"), None)
    if other is not None:
        assert client.post("/youtube/channel", data={"key": other.key, "action": "remove"}).status_code == 400


def test_add_releases_blocked_channel_and_rejects_unknown(client, yt):
    svc = news_service(client.app.state.ctx)
    svc.cfg.list_channel("blocked_channels", CID2, "Zweiter Kanal")
    r = client.get("/youtube/search", params={"q": "zweiter"})
    assert "blockiert" in r.text
    r = client.post("/youtube/channel/add", data={"channel_id": CID2, "handle": "", "language": "xx"})
    assert "Blockierung aufgehoben" in r.text
    assert CID2 not in svc.cfg.blocked_ids()
    ch = next(c for c in svc.cfg.channels() if c.channel_id == CID2)
    assert ch.language == "en" and ch.handle is None
    # Kanal ohne Feed bzw. ungültige ID wird nicht eingetragen
    assert "nicht gefunden" in client.post("/youtube/channel/add", data={"channel_id": CID3}).text
    assert "Ungültige Kanal-ID" in client.post("/youtube/channel/add", data={"channel_id": "UCx"}).text
    assert not any(c.channel_id == CID3 for c in svc.cfg.channels())
    # Eingabefehler und Zustimmungsseite verständlich
    assert "Bitte einen Kanalnamen" in client.get("/youtube/search", params={"q": " "}).text
    yt.results = "<html>leer</html>"
    assert "Keine Kanäle zu" in client.get("/youtube/search", params={"q": "nichts"}).text


def test_search_uses_api_within_budget(client, yt):
    svc = news_service(client.app.state.ctx)
    svc.yt.api_key = "KEY"
    seen = []

    def api(req: httpx.Request) -> httpx.Response:
        if req.url.host == "www.googleapis.com":
            seen.append(req.url.path)
            if req.url.path.endswith("/search"):
                assert req.url.params["type"] == "channel"
                return httpx.Response(200, json={"items": [{"id": {"kind": "youtube#channel", "channelId": CID},
                                                            "snippet": {"title": "Krypto Neu", "description": "d"}}]})
            return httpx.Response(200, json={"items": [{"id": CID, "snippet": {"title": "Krypto Neu",
                                                                               "customUrl": "@kryptoneu"},
                                                        "statistics": {"subscriberCount": "2600000"}}]})
        return yt.handler(req)

    svc.http = svc.yt.client = httpx.Client(transport=httpx.MockTransport(api), follow_redirects=True)
    r = client.get("/youtube/search", params={"q": "krypto neu"})
    assert "YouTube Data API" in r.text and "@kryptoneu" in r.text and "2.600.000" in r.text
    assert seen == ["/youtube/v3/search", "/youtube/v3/channels"] and svc.yt.units_used() == 101
