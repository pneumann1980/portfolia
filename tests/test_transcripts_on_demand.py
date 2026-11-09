"""0.81.0: Kernaussagen auf Wunsch – Knopf auf der Videokarte mit Live-Stand, Transkript selbst einfügen,
Lieblingskanäle mit Tagesgrenze, Analyse auch älterer angeforderter Videos."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest

from app.news import transcripts as T
from app.news import ytlib
from app.news.analysis import AnalysisService
from app.news.module import transcript_states
from app.util.timeutil import iso
from tests.test_news_intel import FakeGateway
from tests.test_youtube_transcripts import ANSWER, add_video, fetched

NOW = datetime.now(UTC)
PASTE = "\n".join(f"{m}:00\nIn diesem Abschnitt spricht der Sprecher über Bitcoin, Zinsen und Gold, Teil {m}."
                  for m in range(6))


@pytest.fixture
def client(config):
    from tests.test_journal import _client

    with _client(config) as c:
        c.get("/settings")
        c.headers["X-CSRF-Token"] = c.cookies.get("portfolia_csrf")
        yield c


@pytest.fixture(autouse=True)
def _clean_lib():
    yield
    ytlib._CACHE.clear()
    ytlib._purge()


class Sched:
    calls: ClassVar[list[str]] = []

    def trigger(self, name, delay=1.0, **kw):
        self.calls.append(name)
        return True

    def next_runs(self):
        return {}


def test_card_shows_button_and_request_runs_live_until_core_statements(client, monkeypatch):
    ctx = client.app.state.ctx
    vid = add_video(ctx, "CARD1")
    page = client.get("/news?tab=videos").text
    assert "Kernaussagen erstellen" in page and f"/news/{vid}/transcript/request" in page
    assert f'href="/news/{vid}/transkript"' in page  # selbst einfügen
    Sched.calls = []
    ctx.scheduler = Sched()
    try:
        r = client.post(f"/news/{vid}/transcript/request")
    finally:
        ctx.scheduler = None
    assert r.status_code == 200 and "Transkript wird geladen" in r.text and 'hx-trigger="every 4s"' in r.text
    assert Sched.calls == ["news_transcripts", "news_analyze"]
    assert ctx.db.q1("SELECT tr_requested_at FROM news_item WHERE id=?", (vid,))["tr_requested_at"]
    monkeypatch.setattr(T.ytlib, "fetch", lambda *a: fetched())
    assert T.TranscriptService(ctx, gap_s=0).run()["loaded"] == 1
    st = client.get(f"/news/{vid}/transcript/state").text
    assert "KI erstellt Kernaussagen" in st or "KI ist aus" in st
    AnalysisService(ctx, FakeGateway(ANSWER)).run()
    st = client.get(f"/news/{vid}/transcript/state").text
    assert "Kernaussagen lesen" in st and "hx-trigger" not in st  # fertig: keine Abfrage mehr
    assert client.get(f"/news/{vid}").status_code == 200  # Leseansicht statt Weiterleitung


def test_states_paused_no_captions_failed_and_retry(client, monkeypatch):
    ctx = client.app.state.ctx
    a = add_video(ctx, "S1", requested=True)
    ctx.db.set_state(T.PAUSE_KEY, iso(NOW + timedelta(hours=5)))
    st = transcript_states(ctx, [a])[a]
    assert st["state"] == "paused" and st["poll"] == "60s"
    assert "vorgemerkt – YouTube sperrt bis" in client.get(f"/news/{a}/transcript/state").text
    ctx.db.set_state(T.PAUSE_KEY, None)
    monkeypatch.setattr(T.ytlib, "fetch", lambda *a: (_ for _ in ()).throw(ytlib.TranscriptError("none")))
    T.TranscriptService(ctx, gap_s=0).run()
    t = client.get(f"/news/{a}/transcript/state").text
    assert "Transkript fehlt" in t and "wartet auf deine Eingabe" in t and "Transkript einfügen" in t  # 0.111
    b = add_video(ctx, "S2", requested=True)
    T.TranscriptService(ctx)._upsert(b, "failed", "broken", url="u", attempts=3)
    assert transcript_states(ctx, [b])[b]["state"] == "failed"
    client.post(f"/news/{b}/transcript/request")  # erneut: Fehlschlag zurückgesetzt, wieder fällig
    assert transcript_states(ctx, [b])[b]["state"] == "queued"
    client.post(f"/news/{a}/transcript/request")  # „keine Untertitel“ bleibt (kein sinnloser Abruf)
    assert transcript_states(ctx, [a])[a]["state"] == "no_captions"
    assert client.post("/news/999999/transcript/request").status_code == 404
    ctx.settings.set(T.SETTING, False)
    c = add_video(ctx, "S3")
    assert transcript_states(ctx, [c])[c]["state"] == "off"


def test_paste_transcript_stores_triggers_analysis_and_shows_badge(client):
    ctx = client.app.state.ctx
    vid = add_video(ctx, "PASTE1", hours=24 * 20)  # älter als das Analysefenster
    page = client.get(f"/news/{vid}/transkript").text
    assert "Transkript selbst einfügen" in page and "Transkript anzeigen" in page
    r = client.post(f"/news/{vid}/transkript", data={"text": "zu kurz"})
    assert r.status_code == 200 and "Zu kurz" in r.text
    r = client.post(f"/news/{vid}/transkript", data={"text": PASTE})
    assert r.status_code == 204 and r.headers["HX-Redirect"] == f"/news/{vid}?keep=1"
    c = ctx.db.q1("SELECT * FROM news_content WHERE item_id=?", (vid,))
    assert c["reason"] == "transcript:eingefuegt:de" and c["content"].startswith("[0:00] ") and T.is_transcript(c)
    gw = FakeGateway(dict(ANSWER, konfidenz=0.95))
    assert AnalysisService(ctx, gw).run()["videos"] == 1  # angefordert: auch außerhalb des 96-h-Fensters
    assert "selbst eingefügt" in gw.calls[0]["user"]
    assert AnalysisService(ctx).get(vid)["konfidenz"] <= 0.7
    reader = client.get(f"/news/{vid}").text
    assert "selbst eingefügt" in reader and "Kernaussagen des Videos" in reader and "Transkript ersetzen" in reader
    assert client.get("/news/123456/transkript").status_code == 404


def test_favorite_channels_toggle_and_daily_cap_in_sources(client):
    ctx = client.app.state.ctx
    svc = T.TranscriptService(ctx)
    assert client.post("/news/transcripts/fav", data={"channel_id": "UCX", "on": "1"}).status_code == 204
    assert svc.favorites() == ["UCX"]
    client.post("/news/transcripts/fav", data={"per_day": "3"})
    assert svc.auto_per_day() == 3
    assert client.post("/news/transcripts/fav", data={"per_day": "viele"}).status_code == 400
    client.post("/news/transcripts/fav", data={"channel_id": "UCX", "on": "0"})
    assert svc.favorites() == []
    page = client.get("/news?tab=sources").text
    assert "Auto-Kernaussagen" in page and "Lieblingskanäle" in page and "heute 0 von 3" in page


def test_status_counts_requests_not_all_videos(client):
    ctx = client.app.state.ctx
    for i in range(5):
        add_video(ctx, f"N{i}")  # ohne Auftrag: zählen nicht
    add_video(ctx, "R1", requested=True)
    page = client.get("/news?tab=status").text
    assert "1 Auftrag offen" in page and "0 von 1" in page and "Lieblingskanäle: 0" in page
