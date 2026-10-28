"""YouTube-Client: Auflösung (API, Kanalseite, Fehler), Videos/Kanäle/Suche, Kurzvideo-Erkennung – ohne Netz."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.news.youtube import YouTubeClient, parse_count, parse_duration
from app.util.timeutil import iso

PAGE = ('<html><head><meta property="og:title" content="Kanal Eins"></head><body><script>'
        '{"externalId":"UCabcdefghijklmnopqrstuv","x":{"simpleText":"1,2 Mio. Abonnenten"}}</script></body></html>')


def client(handler, db, key="K"):
    return YouTubeClient(httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True), db, key)


def fast(c):
    c.limiter.min_interval = 0
    return c


def test_parse_helpers():
    assert parse_count("2,51 Mio. Abonnenten") == 2_510_000 and parse_count("1.234 subscribers") == 1234
    assert parse_count("keine Zahl") is None and parse_duration("PT1H2M3S") == 3723 and parse_duration("kaputt") is None


def test_resolve_via_api_ok_empty_and_without_subscriber_count(db):
    def handler(req):
        h = req.url.params.get("forHandle")
        if h == "@leer":
            return httpx.Response(200, json={"items": []})
        item = {"id": "UC1", "snippet": {"title": "T"}, "statistics": {"subscriberCount": "42"}}
        if h == "@ohnezahl":
            item["statistics"] = {}
        return httpx.Response(200, json={"items": [item]})

    c = fast(client(handler, db))
    ok = c.resolve("kanal")
    assert (ok.handle, ok.channel_id, ok.subscribers, ok.status, ok.method) == ("@kanal", "UC1", 42, "ok", "api")
    assert c.resolve("@ohnezahl").subscribers is None
    gone = c.resolve("@leer")
    assert gone.status == "unresolvable" and "nicht gefunden" in gone.error
    assert c.units_used() == 3  # jeder API-Aufruf kostet 1 Einheit, auch der leere


def test_resolve_falls_back_to_channel_page_when_api_fails(db):
    def handler(req):
        if "googleapis" in str(req.url):
            return httpx.Response(403, json={"error": "quotaExceeded"})
        return httpx.Response(200, text=PAGE)

    r = fast(client(handler, db)).resolve("@kanal")
    assert (r.status, r.channel_id, r.title, r.subscribers, r.method) == (
        "ok", "UCabcdefghijklmnopqrstuv", "Kanal Eins", 1_200_000, "page")


@pytest.mark.parametrize(("handler", "status", "needle"), [
    (lambda req: httpx.Response(404), "unresolvable", "404"),
    (lambda req: httpx.Response(503), "pending", "nicht erreichbar"),
    (lambda req: httpx.Response(200, text="<html>nichts</html>"), "unresolvable", "Keine Channel-ID"),
])
def test_resolve_page_outcomes(db, handler, status, needle):
    r = fast(client(handler, db, key=None)).resolve("@kanal")
    assert r.status == status and needle in (r.error or "")


def test_resolve_network_error_and_consent_page(db):
    def boom(req):
        raise httpx.ConnectError("kein Netz")

    assert fast(client(boom, db, key=None)).resolve("@x").status == "pending"
    consent = "https://consent.youtube.com/m"
    c = fast(client(lambda req: httpx.Response(200, text="x") if "consent" in str(req.url)
                    else httpx.Response(302, headers={"location": consent}), db, key=None))
    assert "Zustimmungsseite" in (c.resolve("@x").error or "")


def test_api_requires_key_and_counts_units_even_on_error(db):
    c = fast(client(lambda req: httpx.Response(500), db, key=None))
    with pytest.raises(RuntimeError):
        c._api("videos", {}, 1)
    assert c.video_details(["a"]) == {} and c.channel_stats(["a"]) == {}  # ohne Schlüssel: leer, keine Anfrage
    c2 = fast(client(lambda req: httpx.Response(500), db))
    with pytest.raises(Exception):
        c2._api("videos", {}, 5)
    assert c2.units_used() == 5


def test_video_details_channel_stats_and_search(db):
    def handler(req):
        path = req.url.path
        if path.endswith("/videos"):
            return httpx.Response(200, json={"items": [
                {"id": "v1", "contentDetails": {"duration": "PT5M"}, "statistics": {"viewCount": "7"},
                 "snippet": {"liveBroadcastContent": "none", "channelId": "C", "channelTitle": "Ch", "title": "T",
                             "publishedAt": "2026-01-01T00:00:00Z"}},
                {"id": "v2", "snippet": {"liveBroadcastContent": "live"}}]})
        if path.endswith("/channels"):
            return httpx.Response(200, json={"items": [{"id": "C", "snippet": {"title": "Ch", "customUrl": "@ch"},
                                                         "statistics": {"hiddenSubscriberCount": True}}]})
        hit = {"id": {"videoId": "v9"}, "snippet": {"channelId": "C", "title": "S"}}
        return httpx.Response(200, json={"items": [hit, {"id": {}, "snippet": {}}]})

    c = fast(client(handler, db))
    d = c.video_details(["v1", "v2"])
    assert d["v1"]["duration_s"] == 300 and d["v1"]["views"] == 7 and d["v1"]["is_live"] is False
    assert d["v2"]["is_live"] is True and d["v2"]["duration_s"] is None
    st = c.channel_stats(["C"])["C"]
    assert st["handle"] == "@ch" and st["subscribers"] is None and st["hidden"] is True
    found = c.search("bitcoin", datetime.now(UTC) - timedelta(days=1))
    assert found == [{"video_id": "v9", "channel_id": "C", "title": "S"}]


def test_is_short_and_feed(db):
    status = {"v": 200}

    def handler(req):
        if req.method == "HEAD":
            if status["v"] == -1:
                raise httpx.ReadTimeout("zu langsam")
            return httpx.Response(status["v"], headers={"location": "https://www.youtube.com/watch?v=x"})
        return httpx.Response(200, content=b"<feed/>")

    c = fast(client(handler, db))
    c.client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    assert c.is_short("v") is True
    status["v"] = 303
    assert c.is_short("v") is False
    status["v"] = 429
    assert c.is_short("v") is None
    status["v"] = -1
    assert c.is_short("v") is None
    assert c.feed("UCabc") == b"<feed/>"


def test_store_resolved_and_resolution_schedule(db):
    c = fast(client(lambda req: httpx.Response(404), db))
    from app.news.youtube import Resolved

    assert c.needs_resolution("@a")  # unbekannt
    c.store_resolved(Resolved("@a", "UC1", "T", 5, "ok"))
    assert not c.needs_resolution("@a")
    db.x("UPDATE yt_channel SET resolved_at=? WHERE handle='@a'", (iso(datetime.now(UTC) - timedelta(days=8)),))
    assert c.needs_resolution("@a")
    c.store_resolved(Resolved("@b", None, None, None, "pending", "x"))
    assert not c.needs_resolution("@b")  # nicht erreichbar: erst nach 12 h erneut
    db.x("UPDATE yt_channel SET resolved_at=? WHERE handle='@b'", (iso(datetime.now(UTC) - timedelta(hours=13)),))
    assert c.needs_resolution("@b")
    c.store_resolved(Resolved("@a", None, None, None, "pending", "später"))  # COALESCE: gute Daten bleiben erhalten
    assert c.resolved("@a")["channel_id"] == "UC1" and c.resolved("@a")["subscribers"] == 5
