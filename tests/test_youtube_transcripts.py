"""YouTube-Transkripte: Wrapper (Spurwahl, Fehlerarten, Update per Knopf mit Selbsttest und Rückfall), Abruf-Job,
Video-Analyse (Kernaussagen, genannte Werte), Leseansicht mit Transkript und Einstellungen."""

from __future__ import annotations

import json
import subprocess
import sys
import types
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.context import AppContext
from app.news import transcripts as T
from app.news import ytlib
from app.news.analysis import VIDEO_TEXT_LIMIT, AnalysisService, validate_video, video_text
from app.news.dedupe import normalize_title, normalize_url
from app.util.timeutil import iso
from tests.test_news_intel import FakeGateway

NOW = datetime.now(UTC)


@pytest.fixture
def ctx(config):
    c = AppContext(config)
    c.startup()
    yield c
    c.shutdown()


@pytest.fixture(autouse=True)
def _clean_lib():
    yield
    ytlib._CACHE.clear()
    ytlib._purge()


def add_video(ctx, vid: str, *, source="yt:UC123", hours=2.0, short=0, live=0, assets=(), title="Marktausblick",
              channel="UC123", requested=False):
    """Video; ``requested`` = Knopf „Kernaussagen erstellen“ gedrückt (seit 0.81.0 lädt nur ein Auftrag oder ein
    Lieblingskanal ein Transkript)."""
    url = f"https://www.youtube.com/watch?v={vid}"
    cur = ctx.db.x(
        """INSERT INTO news_item(kind, url, url_norm, title, title_norm, summary, source_id, source_name, source_weight,
               language, published_at, fetched_at, video_id, channel_id, channel_name, duration_s, is_short, is_live,
               tr_requested_at)
           VALUES ('video',?,?,?,?,?,?,?,0.5,'de',?,?,?,?,?,?,?,?,?)""",
        (url, normalize_url(url), f"{title} {vid}", normalize_title(f"{title} {vid}"), "Beschreibung", source,
         "Kanal", iso(NOW - timedelta(hours=hours)), iso(NOW), vid, channel, "Kanal", 900, short, live,
         iso(NOW) if requested else None))
    iid = int(cur.lastrowid)
    for a in assets:
        ctx.db.x("INSERT INTO news_asset(item_id, asset_id, score) VALUES (?,?,0.9)", (iid, a))
    return iid


# ---- Wrapper: Spurwahl und Fehlerarten ------------------------------------------------------------------------

class Track:
    def __init__(self, lang: str, generated: bool, text: str = "Hallo Welt.") -> None:
        self.language_code, self.is_generated, self._text = lang, generated, text

    def fetch(self):
        return [types.SimpleNamespace(start=float(i * 20), text=f"{self._text} {i}") for i in range(4)]


def fake_module(tracks=None, error: Exception | None = None):
    class Api:
        def __init__(self, http_client=None) -> None:
            assert http_client is not None  # eigene Verbindung mit Zeitlimit
            self.timeout = http_client.request.__func__.__name__

        def list(self, video_id):
            if error is not None:
                raise error
            return list(tracks or [])

    return types.SimpleNamespace(YouTubeTranscriptApi=Api)


def named(name: str) -> Exception:
    return type(name, (Exception,), {})("x")


def test_track_choice_prefers_language_then_manual(config, monkeypatch):
    tracks = [Track("en", False), Track("de", True), Track("fr", False)]
    monkeypatch.setattr(ytlib, "module", lambda cfg: fake_module(tracks))
    got = ytlib.fetch(config, "v1", ["de", "en"])
    assert got.language == "de" and got.generated and len(got.segments) == 4
    got = ytlib.fetch(config, "v1", ["it"])  # keine Wunschsprache: erste manuelle Spur
    assert got.language == "en" and not got.generated


@pytest.mark.parametrize(("error", "kind"), [
    (named("TranscriptsDisabled"), ytlib.KIND_NONE), (named("NoTranscriptFound"), ytlib.KIND_NONE),
    (named("VideoUnavailable"), ytlib.KIND_UNAVAILABLE), (named("AgeRestricted"), ytlib.KIND_UNAVAILABLE),
    (named("RequestBlocked"), ytlib.KIND_BLOCKED), (named("IpBlocked"), ytlib.KIND_BLOCKED),
    (named("YouTubeDataUnparsable"), ytlib.KIND_BROKEN), (named("PoTokenRequired"), ytlib.KIND_BROKEN),
    (ConnectionError("weg"), ytlib.KIND_NETWORK)])
def test_errors_are_classified_by_name(config, monkeypatch, error, kind):
    monkeypatch.setattr(ytlib, "module", lambda cfg: fake_module(error=error))
    with pytest.raises(ytlib.TranscriptError) as e:
        ytlib.fetch(config, "v1", ["de"])
    assert e.value.kind == kind


def test_real_library_is_bundled_and_session_has_a_timeout(config):
    mod = ytlib.module(config)
    assert hasattr(mod.YouTubeTranscriptApi(http_client=ytlib._session()), "list")
    info = ytlib.info(config)
    assert info["version"] == ytlib.bundled_version() and not info["updated"] and info["available"]


# ---- Update per Knopf: Wheel in /data/pylib, Selbsttest, Umschalten, Rückfall --------------------------------

FAKE_INIT = '''
class _E(Exception): pass
class TranscriptsDisabled(_E): pass
class NoTranscriptFound(_E): pass
class RequestBlocked(_E): pass
class VideoUnavailable(_E): pass
class _T:
    language_code, is_generated = "de", False
    def fetch(self):
        class S: start, text = 0.0, "aus der neuen Version"
        return [S()]
class YouTubeTranscriptApi:
    def __init__(self, http_client=None): pass
    def list(self, video_id): return [_T()]
'''


class FakeHttp:
    def __init__(self, version: str) -> None:
        self.version = version

    def get(self, url, timeout=None):
        assert url == ytlib.PYPI_URL
        return types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"info": {"version": self.version}})


def fake_pip(monkeypatch, *, broken: bool = False):
    real = subprocess.run
    calls: list[list[str]] = []

    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[1:4] == ["-m", "pip", "install"]:
            assert "--only-binary=:all:" in cmd and "--no-deps" in cmd  # kein Installationscode, keine Abhängigkeiten
            target = cmd[cmd.index("--target") + 1]
            pkg = __import__("pathlib").Path(target, ytlib.MOD)
            pkg.mkdir(parents=True)
            (pkg / "__init__.py").write_text("raise ImportError('kaputt')" if broken else FAKE_INIT)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real(cmd, **kw)  # Selbsttest läuft wirklich (eigener Prozess)

    monkeypatch.setattr(ytlib.subprocess, "run", run)
    return calls


def test_update_installs_checks_activates_and_resets(config, monkeypatch):
    calls = fake_pip(monkeypatch)
    res = ytlib.update(config, FakeHttp("1.99.0"))
    assert res == {"ok": True, "version": "1.99.0"}, res
    assert any(c[1] == "-c" for c in calls)  # Selbsttest lief
    assert ytlib.info(config)["updated"] and ytlib.info(config)["version"] == "1.99.0"
    got = ytlib.fetch(config, "v1", ["de"])  # lädt jetzt die Version aus /data/pylib
    assert got.segments == [(0.0, "aus der neuen Version")] and got.version == "1.99.0"
    assert ytlib.update(config, FakeHttp("1.99.0"))["unchanged"]
    assert ytlib.reset(config) and not ytlib.info(config)["updated"]
    assert ytlib.module(config).__file__.startswith(sys.prefix)  # wieder die mitgelieferte


def test_update_refuses_new_major_and_keeps_version_when_selftest_fails(config, monkeypatch):
    fake_pip(monkeypatch, broken=True)
    assert "Hauptversion" in ytlib.update(config, FakeHttp("2.0.0"))["error"]
    res = ytlib.update(config, FakeHttp("1.99.1"))
    assert not res["ok"] and "Selbsttest" in res["error"]
    assert not ytlib.info(config)["updated"] and not (ytlib.lib_dir(config) / ".staging-1.99.1").exists()


# ---- Abruf-Job ------------------------------------------------------------------------------------------------

def fetched(lang="de", generated=True):
    segs = [(float(i * 15), f"Satz Nummer {i}.") for i in range(12)]
    return ytlib.Fetched(segs, lang, generated, "1.2.4")


def test_paragraphs_have_timestamps():
    text = T.paragraphs([(0.0, "Erster Satz."), (30.0, "noch was"), (65.0, "Ende."), (3700.0, "Spät.")])
    assert text.split("\n\n") == ["[0:00] Erster Satz. noch was Ende.", "[1:01:40] Spät."]


def test_job_loads_only_requests_and_favorite_channels_up_to_daily_cap(ctx, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(T.ytlib, "fetch", lambda cfg, vid, langs: seen.append(vid) or fetched())
    old = add_video(ctx, "OLDREQ", hours=24 * 30, requested=True)  # Auftrag: egal wie alt
    add_video(ctx, "PLAIN")  # kein Auftrag, kein Lieblingskanal → nie automatisch
    favs = [add_video(ctx, f"FAV{i}", channel="UCFAV", hours=1 + i) for i in range(4)]
    add_video(ctx, "FAVSHORT", channel="UCFAV", short=1)
    add_video(ctx, "FAVOLD", channel="UCFAV", hours=24 * 5)
    svc = T.TranscriptService(ctx, gap_s=0)
    assert svc.run()["loaded"] == 1 and seen == ["OLDREQ"]  # ohne Lieblingskanäle nur der Auftrag
    c = ctx.db.q1("SELECT * FROM news_content WHERE item_id=?", (old,))
    assert c["status"] == "success" and c["reason"] == "transcript:auto:de" and c["content"].startswith("[0:00] ")
    assert T.is_transcript(c)
    svc.set_favorite("UCFAV", True)
    ctx.settings.set(T.AUTO_PER_DAY_KEY, 2)
    assert svc.run()["loaded"] == 2 and seen[1:] == ["FAV0", "FAV1"]  # neueste zuerst, Tagesgrenze 2
    assert svc.auto_used_today() == 2 and not svc.favorite_candidates(5)
    req = add_video(ctx, "REQ2", requested=True)  # Aufträge zählen nicht gegen die Grenze
    assert svc.run()["loaded"] == 1 and seen[-1] == "REQ2"
    ctx.settings.set(T.AUTO_PER_DAY_KEY, 10)
    svc.run()
    assert sorted(seen[4:]) == ["FAV2", "FAV3"] and "PLAIN" not in seen and "FAVSHORT" not in seen
    assert svc.run()["loaded"] == 0  # nie doppelt
    assert svc.set_favorite("UCFAV", False) == [] and favs and req


def test_pasted_transcript_with_and_without_timestamps():
    app_style = ("0:00\nHallo und willkommen zurück.\n0:31\nHeute geht es um Bitcoin.\n1:05\nUnd um Zinsen.\n"
                 "1:40\nZum Schluss Gold.\n")  # Format von „Transkript anzeigen“: Zeitmarke in eigener Zeile
    assert T.pasted_paragraphs(app_style) == ("[0:00] Hallo und willkommen zurück. Heute geht es um Bitcoin. "
                                              "Und um Zinsen.\n\n[1:40] Zum Schluss Gold.")
    inline = "\n".join(f"{m}:00 Satz {m}." for m in range(4))
    assert T.pasted_paragraphs(inline).startswith("[0:00] Satz 0. Satz 1.")
    plain = " ".join(["wort"] * 400)
    assert T.pasted_paragraphs(plain).count("\n\n") == 2  # 150 Wörter je Absatz


def test_job_pauses_on_block_and_retries_broken_then_gives_up(ctx, monkeypatch):
    add_video(ctx, "V1", requested=True)
    blocked = ytlib.TranscriptError("blocked", "IpBlocked")
    monkeypatch.setattr(T.ytlib, "fetch", lambda *a: (_ for _ in ()).throw(blocked))
    res = T.TranscriptService(ctx, gap_s=0).run()
    assert "Sperre" in res["stopped"] and T.TranscriptService(ctx).paused_until() is not None
    assert "pausiert" in T.TranscriptService(ctx, gap_s=0).run()["skipped"]
    ctx.db.set_state(T.PAUSE_KEY, None)
    ctx.db.x("UPDATE news_content SET next_attempt=NULL")
    monkeypatch.setattr(T.ytlib, "fetch", lambda *a: (_ for _ in ()).throw(ytlib.TranscriptError("broken", "X")))
    for _ in range(3):
        T.TranscriptService(ctx, gap_s=0).run()
        ctx.db.x("UPDATE news_content SET next_attempt=NULL WHERE status='pending'")
    c = ctx.db.q1("SELECT status, reason, attempts FROM news_content")
    assert (c["status"], c["reason"], c["attempts"]) == ("failed", "broken", 3)
    assert ctx.db.get_state(T.ERROR_KEY)["kind"] == "broken"


def test_video_without_captions_is_not_retried(ctx, monkeypatch):
    add_video(ctx, "V1", requested=True)
    monkeypatch.setattr(T.ytlib, "fetch", lambda *a: (_ for _ in ()).throw(ytlib.TranscriptError("none")))
    assert T.TranscriptService(ctx, gap_s=0).run()["none"] == 1
    assert ctx.db.q1("SELECT status, reason FROM news_content")["reason"] == "no_transcript"
    assert not T.TranscriptService(ctx).candidates(10)


# ---- Video-Analyse ------------------------------------------------------------------------------------------------

ANSWER = {"kernaussagen": ["Der Sprecher erwartet steigende Zinsen im Herbst.",
                           "Technologieaktien seien nach dem Anstieg teuer bewertet.",
                           "Bitcoin profitiere vom Zufluss in börsengehandelte Fonds.",
                           "Rohstoffe dienten als Absicherung gegen Inflation.",
                           "Er empfiehlt, Gewinne teilweise zu sichern."],
          "empfehlungen": [{"name": "Bitcoin", "art": "krypto", "aussage": "kaufen",
                            "begruendung": "ETF-Zuflüsse und knappes Angebot.", "kursziel": "", "stelle": "3:20"},
                           {"name": "Nvidia", "art": "aktie", "aussage": "Verkaufen",
                            "begruendung": "Bewertung zu hoch.", "kursziel": "100 Dollar", "stelle": "quatsch"}],
          "betroffene": [{"name": "Bitcoin", "art": "krypto", "richtung": "positiv"}],
          "kategorie": "krypto-markt", "einordnung": "Meinungsvideo ohne Belege für die Kursziele.",
          "unsicherheiten": [], "stimmung": "gemischt", "bedeutung": 0.4, "konfidenz": 0.9}


def with_transcript(ctx, vid="AAA", text=None):
    iid = add_video(ctx, vid)
    T.TranscriptService(ctx)._upsert(iid, "success", "transcript:auto:de", url=f"https://www.youtube.com/watch?v={vid}",
                                     lang="de", content=text or "[0:00] Hallo zusammen.\n\n[3:20] Bitcoin kaufen.",
                                     words=6, chash="h1")
    return iid


def test_video_analysis_stores_core_statements_and_named_values(ctx):
    iid = with_transcript(ctx)
    gw = FakeGateway(ANSWER)
    res = AnalysisService(ctx, gw).run()
    assert res["videos"] == 1 and '"transkript"' in gw.calls[0]["user"] and "kernaussagen" in gw.calls[0]["system"]
    a = AnalysisService(ctx).get(iid)
    assert a["video"] and len(a["kernaussagen"]) == 5 and a["basis"] == "transcript"
    btc, nvda = a["empfehlungen"]
    assert (btc["aussage"], btc["stelle"]) == ("kaufen", "3:20")
    assert (nvda["aussage"], nvda["stelle"], nvda["kursziel"]) == ("verkaufen", "", "100 Dollar")
    assert a["konfidenz"] <= 0.7  # automatische Untertitel
    assert ctx.db.q1("SELECT llm_summary FROM news_item WHERE id=?", (iid,))["llm_summary"].startswith("Der Sprecher")
    AnalysisService(ctx, gw).run()
    assert len(gw.calls) == 1  # unverändert → keine zweite Anfrage


def test_video_analysis_rejects_too_few_statements(ctx):
    with_transcript(ctx)
    gw = FakeGateway({**ANSWER, "kernaussagen": ["Nur ein Satz hier.", "Und noch einer."]})
    assert AnalysisService(ctx, gw).run()["rejected"] == 1
    assert validate_video({"kernaussagen": ["kurz"]}) is None


def test_long_transcripts_are_sampled_over_the_whole_video():
    pars = [f"[{i}:00] " + "wort " * 200 for i in range(200)]  # ≈ 200.000 Zeichen
    text, short = video_text("\n\n".join(pars))
    assert short and len(text) <= VIDEO_TEXT_LIMIT + 200 and text.count("[…]") == 11
    assert all(f"[{m}:00]" in text for m in (0, 100, 183))  # Anfang, Mitte, Ende
    assert video_text("kurz") == ("kurz", False)


# ---- Leseansicht und Einstellungen --------------------------------------------------------------------------------

@pytest.fixture
def client(config):
    from tests.test_journal import _client

    with _client(config) as c:
        c.get("/settings")
        c.headers["X-CSRF-Token"] = c.cookies.get("portfolia_csrf")
        yield c


def test_reader_shows_core_statements_values_and_transcript_with_jumps(client):
    ctx = client.app.state.ctx
    iid = with_transcript(ctx)
    AnalysisService(ctx, FakeGateway(ANSWER)).run()
    r = client.get(f"/news/{iid}")
    assert r.status_code == 200
    t = r.text
    assert "Kernaussagen des Videos" in t and "Der Sprecher erwartet" in t and "Genannte Werte" in t
    assert "Aussagen des Videos, keine Empfehlung von Portfolia" in t and "ETF-Zuflüsse" in t
    assert 'href="https://www.youtube.com/watch?v=AAA&amp;t=200s"' in t  # Stelle und Zeitmarke springen ins Video
    assert "Transkript lesen" in t and "Bitcoin kaufen." in t and "automatische Untertitel" in t
    r = client.get(f"/news/{iid}?watch=1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://www.youtube.com/")
    plain = add_video(ctx, "NOTR")
    r = client.get(f"/news/{plain}", follow_redirects=False)
    assert r.status_code == 303  # ohne Transkript wie bisher direkt zu YouTube
    cards = client.get("/news?tab=videos&status=all").text  # geöffnet = gelesen (Standard „Ungelesen“)
    assert f'href="/news/{iid}?back=' in cards and "Transkript" in cards


def test_settings_card_toggle_and_update(client, monkeypatch):
    ctx = client.app.state.ctx
    page = client.get("/news?tab=sources").text
    assert "YouTube-Transkripte" in page and f"youtube-transcript-api {ytlib.bundled_version()}" in page
    r = client.post("/news/transcripts/toggle", data={"on": "0"})
    assert "ausgeschaltet" in r.text and ctx.settings.get(T.SETTING) is False
    assert T.TranscriptService(ctx).run() == {"skipped": "ausgeschaltet"}
    client.post("/news/transcripts/toggle", data={"on": "1"})
    monkeypatch.setattr(ytlib, "update", lambda cfg, http: {"ok": False, "version": "1.3.0", "error": "Netz weg."})
    r = client.post("/news/transcripts/update")
    assert "Nicht aktualisiert (1.3.0): Netz weg." in r.text and "bisherige Version bleibt aktiv" in r.text
    monkeypatch.setattr(ytlib, "update", lambda cfg, http: {"ok": True, "version": "1.3.0"})
    ctx.db.set_state(T.PAUSE_KEY, iso(NOW + timedelta(hours=5)))
    r = client.post("/news/transcripts/update")
    assert "Aktualisiert auf 1.3.0" in r.text and ctx.db.get_state(T.PAUSE_KEY) is None


def test_json_payload_contains_only_public_data(ctx):
    with_transcript(ctx)
    gw = FakeGateway(ANSWER)
    AnalysisService(ctx, gw).run()
    payload: dict[str, Any] = json.loads(gw.calls[0]["user"])["video"]
    assert set(payload) == {"titel", "kanal", "veroeffentlicht", "laenge_min", "bezug_assets", "untertitel",
                            "gekuerzt", "transkript"}
