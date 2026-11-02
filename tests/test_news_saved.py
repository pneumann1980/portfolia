"""News 0.80.0: Merken ★ mit Notiz (bleibt über die Aufbewahrungsfrist hinaus), Standardfilter „Ungelesen“, „Alle
angezeigten als gelesen“ mit Rückgängig, Transkripte und Video-Analysen in der Statusseite und im manuellen Lauf."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest

from app.news import transcripts as T
from app.news import wave as W
from app.news import ytlib
from app.news.analysis import AnalysisService
from app.news.enrich import EnrichService
from app.news.module import NOTE_MAX, READ_ALL_UNDO, news_service
from app.news.status import pipeline_status
from app.util.timeutil import iso
from tests.test_news_intel import FakeGateway, add_item, store_content
from tests.test_youtube_transcripts import ANSWER, add_video, with_transcript

NOW = datetime.now(UTC)


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


def wctx(client):
    return client.app.state.ctx


def read_flags(ctx) -> dict[int, int]:
    return {r["id"]: r["is_read"] for r in ctx.db.q("SELECT id, is_read FROM news_item")}


# ---- Merken ★ und Notiz ---------------------------------------------------------------------------------------

def test_star_saves_and_unsaves_with_fragment(client):
    ctx = wctx(client)
    iid = add_item(ctx, "Bitcoin steigt deutlich", "https://pub.example.com/a")
    r = client.post(f"/news/{iid}/save", data={"on": "1"})
    assert r.status_code == 200 and "★" in r.text and 'aria-pressed="true"' in r.text and '"on": "0"' in r.text
    first = ctx.db.q1("SELECT saved_at FROM news_item WHERE id=?", (iid,))["saved_at"]
    assert first
    client.post(f"/news/{iid}/save", data={"on": "1"})  # doppelt: Zeitpunkt bleibt
    assert ctx.db.q1("SELECT saved_at FROM news_item WHERE id=?", (iid,))["saved_at"] == first
    r = client.post(f"/news/{iid}/save", data={"on": "0", "where": "reader"})
    assert r.status_code == 200 and 'id="reader-save"' in r.text and "☆ Merken" in r.text
    assert ctx.db.q1("SELECT saved_at FROM news_item WHERE id=?", (iid,))["saved_at"] is None
    assert client.post("/news/999999/save", data={"on": "1"}).status_code == 404


def test_note_saves_item_is_trimmed_and_shown_in_reader_and_saved_tab(client):
    ctx = wctx(client)
    iid = add_item(ctx, "Zinsentscheid der Notenbank", "https://pub.example.com/z")
    r = client.post(f"/news/{iid}/note", data={"note": "  Nach dem Entscheid prüfen.\r\n" + "x" * 900})
    assert r.status_code == 200 and "gespeichert" in r.text and "Notiz speichern" in r.text
    row = ctx.db.q1("SELECT saved_at, note FROM news_item WHERE id=?", (iid,))
    assert row["saved_at"] and len(row["note"]) == NOTE_MAX and row["note"].startswith("Nach dem Entscheid prüfen.\n")
    page = client.get(f"/news/{iid}").text
    assert "★ Gemerkt" in page and "Nach dem Entscheid prüfen." in page
    saved = client.get("/news?tab=saved").text
    assert "1 gemerkt" in saved and "✎ Nach dem Entscheid prüfen." in saved and "★ Gemerkt" in saved
    client.post(f"/news/{iid}/note", data={"note": "   "})
    assert ctx.db.q1("SELECT note FROM news_item WHERE id=?", (iid,))["note"] is None


def test_saved_tab_lists_all_kinds_newest_saved_first_without_time_window(client):
    ctx = wctx(client)
    old = add_item(ctx, "Uralte Meldung zu Bitcoin", "https://pub.example.com/old", hours=24 * 60)
    vid = add_video(ctx, "VID1")
    art = add_item(ctx, "Frische Meldung zu Zinsen", "https://pub.example.com/new")
    client.get(f"/news/{art}")  # gelesen – Merken ist unabhängig davon
    for i, iid in enumerate((old, vid, art)):
        ctx.db.x("UPDATE news_item SET saved_at=? WHERE id=?", (iso(NOW - timedelta(minutes=10 - i)), iid))
    page = client.get("/news?tab=saved").text
    assert "3 gemerkt" in page and ">★ Gemerkt <span class=\"badge\">3</span>" in page
    assert page.index("Frische Meldung") < page.index("Marktausblick VID1") < page.index("Uralte Meldung")
    assert 'name="status"' not in page  # keine Filter im Reiter „Gemerkt“


def test_retention_keeps_saved_items_with_content_analysis_and_assets(client):
    ctx = wctx(client)
    days = int(news_service(ctx).cfg.settings.get("retention_days", 90))
    old_saved = add_item(ctx, "Gemerkte alte Meldung", "https://pub.example.com/s", hours=24 * (days + 5),
                         assets=("BTC",))
    old_plain = add_item(ctx, "Alte Meldung", "https://pub.example.com/p", hours=24 * (days + 5), assets=("BTC",))
    store_content(ctx, old_saved)
    store_content(ctx, old_plain)
    ctx.db.x("UPDATE news_item SET saved_at=?, note='behalten' WHERE id=?", (iso(NOW), old_saved))
    assert news_service(ctx).retention() == 1
    ids = {r["id"] for r in ctx.db.q("SELECT id FROM news_item")}
    assert old_saved in ids and old_plain not in ids
    assert ctx.db.q1("SELECT 1 FROM news_content WHERE item_id=?", (old_saved,)) is not None
    assert ctx.db.q1("SELECT 1 FROM news_asset WHERE item_id=?", (old_saved,)) is not None
    assert ctx.db.q1("SELECT 1 FROM news_content WHERE item_id=?", (old_plain,)) is None
    ctx.db.x("UPDATE news_item SET saved_at=NULL WHERE id=?", (old_saved,))  # Merken aufgehoben → Frist gilt
    assert news_service(ctx).retention() == 1


# ---- Standard „Ungelesen“, alle gelesen mit Rückgängig -----------------------------------------------------------

def test_default_filter_is_unread_and_read_items_stay_until_reload(client):
    ctx = wctx(client)
    a = add_item(ctx, "Erste Meldung zu Bitcoin", "https://pub.example.com/1", relevance=0.9)
    add_item(ctx, "Zweite Meldung zu Bitcoin", "https://pub.example.com/2", relevance=0.9)
    page = client.get("/news?tab=articles&sort=time").text
    assert '<option value="unread" selected' in page and "Erste Meldung" in page
    r = client.post(f"/news/{a}/read", data={"fragment": "1"}, headers={"hx-request": "true"})
    assert r.status_code == 200 and "Als ungelesen markieren" in r.text  # nur der Knopf wechselt, Karte bleibt
    assert "Erste Meldung" not in client.get("/news?tab=articles&sort=time").text  # nach Neuladen weg
    assert "Erste Meldung" in client.get("/news?tab=articles&sort=time&status=all").text
    assert "Erste Meldung" not in client.get("/news?tab=articles&sort=time&status=quatsch").text  # → Ungelesen
    client.post("/news/read-all", data={"tab": "articles", "days": "7", "status": "unread", "sort": "time"})
    t = client.get("/news?tab=articles&sort=time").text
    assert "Alles gelesen" in t and "Keine Meldungen für diese Auswahl" not in t


def test_read_all_marks_exactly_the_filtered_list_and_can_be_undone(client):
    ctx = wctx(client)
    btc = [add_item(ctx, f"Bitcoin Meldung {i}", f"https://pub.example.com/b{i}", assets=("BTC",)) for i in range(3)]
    other = add_item(ctx, "Meldung ohne Bezug zu Zinsen", "https://pub.example.com/o", relevance=0.9)
    already = add_item(ctx, "Schon gelesen", "https://pub.example.com/r", assets=("BTC",))
    ctx.db.x("UPDATE news_item SET is_read=1 WHERE id=?", (already,))
    r = client.post("/news/read-all", data={"tab": "articles", "asset": "BTC", "days": "7", "status": "unread"})
    assert r.status_code == 204 and r.headers.get("HX-Refresh") == "true"
    flags = read_flags(ctx)
    assert all(flags[i] == 1 for i in btc) and flags[other] == 0
    st = ctx.db.get_state(READ_ALL_UNDO)
    assert sorted(st["ids"]) == sorted(btc)  # schon gelesene gehören nicht dazu
    page = client.get("/news?tab=articles").text
    assert "3 Meldungen als gelesen markiert" in page and "Rückgängig" in page
    assert client.post("/news/read-all/undo").status_code == 204
    flags = read_flags(ctx)
    assert all(flags[i] == 0 for i in btc) and flags[already] == 1
    assert ctx.db.get_state(READ_ALL_UNDO) is None and "als gelesen markiert" not in client.get("/news").text
    # Videos: ohne Shorts, solange der Filter sie nicht zeigt
    v = add_video(ctx, "V1")
    s = add_video(ctx, "S1", short=1)
    client.post("/news/read-all", data={"tab": "videos", "days": "7"})
    assert read_flags(ctx)[v] == 1 and read_flags(ctx)[s] == 0


def test_read_all_undo_expires_and_can_be_dismissed(client):
    ctx = wctx(client)
    a = add_item(ctx, "Meldung", "https://pub.example.com/x")
    client.post("/news/read-all", data={"tab": "articles"})
    client.post("/news/read-all/undo", data={"dismiss": "1"})
    assert read_flags(ctx)[a] == 1 and ctx.db.get_state(READ_ALL_UNDO) is None
    ctx.db.x("UPDATE news_item SET is_read=0")
    client.post("/news/read-all", data={"tab": "articles"})
    ctx.db.set_state(READ_ALL_UNDO, dict(ctx.db.get_state(READ_ALL_UNDO), at=iso(NOW - timedelta(minutes=30))))
    assert "als gelesen markiert" not in client.get("/news").text
    client.post("/news/read-all/undo")
    assert read_flags(ctx)[a] == 1  # abgelaufen: nichts zurückgenommen


# ---- Statusseite: Transkripte und Video-Analysen -----------------------------------------------------------------

def test_status_shows_transcript_stage_counts_and_video_analyses(client, monkeypatch):
    ctx = wctx(client)
    monkeypatch.setattr(T.ytlib, "fetch", lambda *a: (_ for _ in ()).throw(ytlib.TranscriptError("none")))
    add_video(ctx, "NOCAP")
    T.TranscriptService(ctx, gap_s=0).run()
    add_video(ctx, "WAIT1")
    add_video(ctx, "WAIT2")
    with_transcript(ctx, "DONE1")
    ps = pipeline_status(ctx)
    tr = ps["transcripts"]
    assert (tr["waiting"], tr["none"], tr["loaded"], tr["enabled"]) == (2, 1, 1, True)
    assert ps["video_waiting"] == 1 and "transcripts" in ps["badges"]
    page = client.get("/news?tab=status").text
    assert "<dt>Transkripte" in page and 'aria-label="Transkripte"' in page
    assert "in 24 h 1 geladen, 1 ohne Untertitel" in page
    bar = ps["bars"]["transcripts"]
    assert (bar["waiting"], bar["total"]) == (2, 2)  # ohne offenen Zyklus: 0 erledigt + 2 wartend
    assert "Rest" in page and ps["eta"]["transcripts"]["minutes"] == 30  # Nennleistung 8 je 30 Min.
    assert "dazu 1 Video mit Transkript – Kernaussagen folgen" in page


def test_status_bottleneck_for_youtube_block_and_broken_library(client):
    ctx = wctx(client)
    add_video(ctx, "V1")
    ctx.db.set_state(T.PAUSE_KEY, iso(NOW + timedelta(hours=5)))
    ps = pipeline_status(ctx)
    assert "YouTube sperrt den Transkript-Abruf" in ps["backlog"]["bottleneck"]
    page = client.get("/news?tab=status").text
    assert "YouTube-Sperre" in page and "Bibliothek aktualisieren" in page and "#yt-transcripts" in page
    ctx.db.set_state(T.PAUSE_KEY, None)
    ctx.db.set_state(T.ERROR_KEY, {"at": iso(NOW), "kind": "broken", "detail": "CouldNotRetrieveTranscript"})
    assert "Transkript-Bibliothek kann Videos nicht lesen" in pipeline_status(ctx)["backlog"]["bottleneck"]
    assert "CouldNotRetrieveTranscript" in client.get("/news?tab=status").text
    ctx.db.set_state(T.ERROR_KEY, None)
    assert "Transkript-Takt" in pipeline_status(ctx)["backlog"]["bottleneck"]  # nur noch Wartende
    ctx.settings.set("news.transcripts", False)
    assert "ausgeschaltet" in client.get("/news?tab=status").text


# ---- Manueller Lauf: Transkripte und Video-Analyse -------------------------------------------------------------

def _wave(ctx, gw, monkeypatch):
    monkeypatch.setattr(T.ytlib, "fetch", lambda cfg, vid, langs: ytlib.Fetched(
        [(float(i * 15), f"Satz Nummer {i}.") for i in range(12)], "de", True, "1.2.4"))
    return W.WaveRunner(ctx, None, enrich=EnrichService(ctx), analysis=AnalysisService(ctx, gw),
                        transcripts=T.TranscriptService(ctx, gap_s=0))


def test_manual_run_loads_transcripts_and_analyses_videos(client, monkeypatch):
    ctx = wctx(client)
    vids = [add_video(ctx, f"W{i}") for i in range(3)]
    ctx.settings.set("news.video_analysis_max_per_run", 1)  # mehrere Runden nötig
    gw = FakeGateway(ANSWER)
    res = _wave(ctx, gw, monkeypatch).run()
    st = W.load(ctx)
    assert res["status"] == "done", st
    assert st["stages"]["transcripts"] == {"status": "done", "note": "3 geladen, 0 ohne Untertitel"}
    assert st["stages"]["analyze"]["status"] == "done"
    assert all(AnalysisService(ctx).get(i)["video"] for i in vids)
    assert AnalysisService(ctx).video_backlog() == 0


def test_manual_run_transcript_stage_skipped_or_waiting(client, monkeypatch):
    ctx = wctx(client)
    add_video(ctx, "V1")
    ctx.db.set_state(T.PAUSE_KEY, iso(NOW + timedelta(hours=3)))
    _wave(ctx, FakeGateway(ANSWER), monkeypatch).run()
    st = W.load(ctx)["stages"]["transcripts"]
    assert st["status"] == "blocked" and "YouTube-Sperre" in st["note"]
    assert "Transkripte: YouTube-Sperre" in client.get("/news?tab=status").text
    ctx.settings.set("news.transcripts", False)
    _wave(ctx, FakeGateway(ANSWER), monkeypatch).run()
    assert W.load(ctx)["stages"]["transcripts"] == {"status": "skipped", "note": "ausgeschaltet"}


def test_old_wave_state_without_transcript_stage_still_loads(client):
    ctx = wctx(client)
    st = W._new_state(False)
    del st["stages"]["transcripts"]
    ctx.db.set_state(W.STATE_KEY, dict(st, status="done", finished_at=iso(NOW)))
    assert client.get("/news?tab=status").status_code == 200
    runner = W.WaveRunner(ctx, None, state=W.reserved(ctx) or st)
    assert "transcripts" in runner.state["stages"]


# ---- 0.80.1: Balken für Transkripte, „Jetzt fortsetzen“ bei YouTube-Sperre --------------------------------------

def test_transcript_bar_counts_cycle_like_other_stages(client, monkeypatch):
    from app.news import queue as Q
    from app.news.module import job_transcripts

    ctx = wctx(client)
    monkeypatch.setattr(T.ytlib, "fetch", lambda cfg, vid, langs: ytlib.Fetched(
        [(0.0, "Satz.")], "de", True, "1.2.4"))
    monkeypatch.setattr(T.time, "sleep", lambda _s: None)  # Pause je Video im Test überspringen
    for i in range(10):
        add_video(ctx, f"C{i}")
    job_transcripts(ctx)  # öffnet den Zyklus, lädt 8 (PER_RUN), schreibt den Stand fort
    bar = Q.bar(ctx, "transcripts", Q.waiting_now(ctx)["transcripts"])
    assert (bar["done"], bar["waiting"], bar["total"]) == (8, 2, 10) and bar["pct"] == 80
    job_transcripts(ctx)  # Rest; leer → Zyklus schließt mit Umfang
    bar = Q.bar(ctx, "transcripts", 0)
    assert (bar["total"], bar["last_done"]) == (0, 10) and bar["finished_at"]


def test_transcript_eta_during_block_and_resume_button(client):
    ctx = wctx(client)
    v1 = add_video(ctx, "B1")
    add_video(ctx, "B2")
    until = NOW + timedelta(hours=20)
    ctx.db.set_state(T.PAUSE_KEY, iso(until))
    T.TranscriptService(ctx)._upsert(v1, "pending", "blocked", url="u", next_at=until)
    ps = pipeline_status(ctx)
    assert ps["eta"]["transcripts"]["minutes"] is None and "ruht bis" in ps["eta"]["transcripts"]["text"]
    page = client.get("/news?tab=status").text
    assert "Jetzt fortsetzen" in page and "/news/transcripts/resume" in page and "YouTube-Sperre" in page

    class Sched:
        calls: ClassVar[list[str]] = []

        def trigger(self, name, delay=1.0, **kw):
            self.calls.append(name)
            return True

        def next_runs(self):
            return {}

    ctx.scheduler = Sched()
    try:
        r = client.post("/news/transcripts/resume")
    finally:
        ctx.scheduler = None
    assert r.status_code == 200 and 'id="pipe-live"' in r.text and "Abruf fortgesetzt" in r.text
    assert Sched.calls == ["news_transcripts"] and T.TranscriptService(ctx).paused_until() is None
    assert ctx.db.q1("SELECT next_attempt FROM news_content WHERE item_id=?", (v1,))["next_attempt"] is None
    assert len(T.TranscriptService(ctx).candidates(10)) == 2  # das zurückgestellte Video ist wieder fällig
    assert "Jetzt fortsetzen" not in r.text
