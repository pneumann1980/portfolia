"""0.111: Gemini-Modell je Aufgabe – Profile, gemeinsame Kontingente je Modell, Pausen nur für das betroffene Modell,
Vorrang manueller Starts, Warteschlange der Discovery-Jobs, Video-Transkripte mit Zeitmarken.

Keine echten KI-Aufrufe: HTTP über ``httpx.MockTransport``, Discovery über Attrappen. Synthetische Daten."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.discovery import ai as DAI
from app.discovery import jobs as J
from app.news import ai as A
from app.news import transcripts as T
from app.news.analysis import AnalysisService, validate_video
from app.news.module import transcript_states
from app.util.timeutil import iso
from tests.test_discovery import _add, fake_parts, make_client
from tests.test_news_intel import FakeGateway, echo_answers, gem_ok, gemini_ctx, seed_for_analysis
from tests.test_youtube_transcripts import ANSWER, add_video, with_transcript

LITE, FLASH_VIDEO, FLASH_CO = "gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-3.8-flash"


@pytest.fixture
def ctx(config):
    from app.context import AppContext

    c = AppContext(config)
    c.startup()
    yield c
    c.shutdown()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("app.news.ai.time.sleep", lambda s: None)
    A.AiGateway._recent.clear()
    A.AiGateway._inflight.clear()
    yield
    A.AiGateway._recent.clear()
    A.AiGateway._inflight.clear()


def _model_of(req: httpx.Request) -> str:
    return req.url.path.rsplit("/", 1)[-1].split(":")[0]


def quota_day() -> httpx.Response:
    return httpx.Response(429, json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota",
                                               "details": [{"violations": [
                                                   {"quotaId": "GenerateRequestsPerDayPerProjectPerModel"}]}]}})


# ------------------------------------------------------------------------------------------- Profile und Übernahme

def test_legacy_settings_are_taken_over_unchanged(ctx):
    """Bestehende Installation: Profil „ein Modell für alle“, bisheriges Modell und bisherige Grenzen, bisheriger
    Tageszähler – keine Änderung, bis der Nutzer ein anderes Profil wählt."""
    gw = gemini_ctx(ctx, lambda r: gem_ok(), **{"llm.gemini_model": "gemini-2.5-flash", "llm.gemini_rpd": 42,
                                                "llm.gemini_rpm": 3})
    assert gw.profile() == "einzeln"
    assert set(gw.task_models().values()) == {"gemini-2.5-flash"}
    assert gw.limits("gemini-2.5-flash")["rpd"] == 42 and gw.limits("gemini-2.5-flash")["rpm"] == 3
    ctx.db.x("INSERT INTO ai_usage(day, provider, requests, input_tokens, output_tokens, errors, quota_hits) "
             "VALUES (?,?,?,?,?,?,?)", (gw.day_key("gemini"), "gemini", 40, 0, 0, 0, 0))  # Zähler von heute (vor 0.111)
    assert gw.requests_left("video") == 2  # derselbe Zähler gilt weiter, für alle Aufgaben gemeinsam
    gw.generate("s", "u", None, 100, task="video", priority="manual")
    assert gw.requests_left("news") == 1 and gw.usage_task("video")["requests"] == 1


def test_free_tier_and_own_profile_mapping(ctx):
    gw = gemini_ctx(ctx, lambda r: gem_ok(), **{"llm.gemini_profile": "free_tier"})
    assert gw.task_models() == {"news": LITE, "events": LITE, "report": LITE, "discovery": LITE,
                                "video": FLASH_VIDEO, "company": FLASH_CO}
    ctx.settings.set("llm.gemini_profile", "eigen")
    ctx.settings.set("llm.task_models", {"video": "gemini-2.5-flash", "company": "gemini-2.5-pro"})  # pro: verboten
    assert gw.model_for("video") == "gemini-2.5-flash"
    assert gw.model_for("company") == gw.base_model() and gw.model_for("news") == gw.base_model()


def test_settings_page_saves_profile_task_models_and_limits(config, monkeypatch):
    for c in make_client(config, monkeypatch):
        ctx = c.app.state.ctx
        page = c.get("/settings/ki").text  # 0.113: eigene Seite „KI & Modelle“
        assert "Modellstrategie" in page and 'name="gemini_profile"' in page
        form = {"csrf_token": c.token, "section": "ai", "gemini_model": LITE, "gemini_rpm": "5",
                "gemini_tpm": "100000", "gemini_rpd": "100", "gemini_budget": "200000", "llm_provider": "gemini",
                "gemini_profile": "free_tier", "manual_reserve_pct": "20", "task_model_video": "gemini-2.5-flash",
                "lim_model_0": FLASH_CO, "lim_rpm_0": "4", "lim_tpm_0": "250000", "lim_rpd_0": "25", "lim_tpd_0": "0",
                "lim_model_1": "gemini-2.5-pro", "lim_rpd_1": "999"}
        r = c.post("/settings/save", data=form, follow_redirects=False)
        assert r.status_code in (200, 303)
        s = ctx.settings
        assert s.get("llm.gemini_profile") == "free_tier" and s.get("llm.manual_reserve_pct") == 20
        assert s.get("llm.task_models") == {"video": "gemini-2.5-flash"}
        lim = s.get("llm.model_limits")
        assert lim[FLASH_CO] == {"rpm": 4, "tpm": 250000, "rpd": 25, "tpd": 0} and "gemini-2.5-pro" not in lim
        assert A.AiGateway(ctx).limits(FLASH_CO)["rpd"] == 25


# ------------------------------------------------------------------------------------------- Kontingente je Modell

def test_exhausted_flash_lite_still_allows_video_and_company(ctx):
    """Flash-Lite meldet ein erschöpftes Tageskontingent: nur Aufgaben auf Flash-Lite warten (bis Mitternacht
    Pazifikzeit); Video-Kernaussagen und Firmenanalyse laufen auf ihren Modellen weiter – ohne Modellwechsel."""
    seen: list[str] = []

    def handler(req):
        seen.append(_model_of(req))
        return quota_day() if _model_of(req) == LITE else gem_ok()

    gw = gemini_ctx(ctx, handler, **{"llm.gemini_profile": "free_tier"})
    with pytest.raises(A.QuotaExceeded):
        gw.generate("s", "u", None, 100, task="news")
    for t in ("news", "events", "report", "discovery"):
        b = gw.blocked(t)
        assert b is not None and b["model"] == LITE and b["scope"] == "day"
        assert abs((b["until"] - A.next_pacific_midnight()).total_seconds()) < 2
        with pytest.raises(A.Paused):
            gw.generate("s", "u", None, 100, task=t)
    assert gw.generate("s", "u", None, 100, task="video").model == FLASH_VIDEO
    assert gw.generate("s", "u", None, 100, task="company").model == FLASH_CO
    assert seen == [LITE, FLASH_VIDEO, FLASH_CO]  # pausierte Aufgaben fragen nicht erneut an
    rows = {r["task"]: r for r in gw.task_status()}
    assert rows["news"]["wait"] and rows["video"]["wait"] is None and rows["company"]["wait"] is None
    assert DAI.availability(ctx, "company")["state"] == "" and DAI.availability(ctx, "discovery")["state"] == "wartet"


def test_tasks_on_the_same_model_share_its_quota(ctx):
    gw = gemini_ctx(ctx, lambda r: gem_ok(), **{"llm.gemini_profile": "free_tier", "llm.manual_reserve_pct": 0,
                                                "llm.model_limits": {LITE: {"rpm": 0, "tpm": 0, "rpd": 3, "tpd": 0}}})
    gw.generate("s", "u", None, 100, task="news")
    gw.generate("s", "u", None, 100, task="news")
    gw.generate("s", "u", None, 100, task="events")
    with pytest.raises(A.BudgetExceeded) as e:  # Tagesbericht teilt das Kontingent mit Nachrichten und Ereignissen
        gw.generate("s", "u", None, 100, task="report")
    assert e.value.model == LITE and e.value.scope == "local"
    assert gw.usage_model(LITE)["requests"] == 3 and gw.usage_task("news")["requests"] == 2
    assert gw.requests_left("discovery") == 0 and gw.requests_left("video") == A.LIMIT_DEFAULTS["flash"]["rpd"]
    row = next(r for r in gw.task_status() if r["task"] == "report")
    assert row["wait"]["scope"] == "local" and set(row["shared"]) >= {"Nachrichtenpakete", "Ereignis-Grenzfälle"}


def test_parallel_calls_at_the_daily_limit_never_exceed_it(ctx):
    lock = threading.Lock()
    sent: list[str] = []

    def handler(req):
        time.sleep(0.02)  # Anfragen überlappen sich
        with lock:
            sent.append(_model_of(req))
        return gem_ok()

    gw = gemini_ctx(ctx, handler, **{"llm.gemini_profile": "free_tier", "llm.manual_reserve_pct": 0,
                                     "llm.model_limits": {FLASH_CO: {"rpm": 0, "tpm": 0, "rpd": 3, "tpd": 0}}})
    ok, refused, other = [], [], []

    def call():
        try:
            A.AiGateway(ctx).generate("s", "u", None, 100, task="company")
            ok.append(1)
        except A.BudgetExceeded:
            refused.append(1)
        except Exception as e:  # pragma: no cover – im Fehlerfall sichtbar
            other.append(repr(e))

    threads = [threading.Thread(target=call) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert other == [] and len(ok) == 3 and len(refused) == 5 and len(sent) == 3
    assert gw.usage_model(FLASH_CO)["requests"] == 3


def test_finished_call_is_never_counted_twice_while_releasing_its_slot(ctx, monkeypatch):
    """Regression (CI 0.112): Zwischen „Verbrauch gespeichert“ und „Platz freigegeben“ zählte ein paralleler Aufruf
    denselben Aufruf doppelt (gespeichert + laufend) und wurde zu früh abgewiesen. Das Fenster wird hier künstlich
    geöffnet: Aufruf A ist gespeichert, hält seinen Platz noch – B (Limit 2) muss trotzdem zugelassen werden."""
    gw = gemini_ctx(ctx, lambda r: gem_ok(), **{"llm.gemini_profile": "free_tier", "llm.manual_reserve_pct": 0,
                                                "llm.model_limits": {FLASH_CO: {"rpm": 0, "tpm": 0, "rpd": 2,
                                                                                "tpd": 0}}})
    recorded, go_on = threading.Event(), threading.Event()
    orig = A.AiGateway._record

    def record_then_hold(self, *a, **k):
        orig(self, *a, **k)
        if k.get("model") and not recorded.is_set():
            recorded.set()
            go_on.wait(5)  # Aufruf A: Verbrauch gespeichert, Platz (noch) nicht freigegeben

    monkeypatch.setattr(A.AiGateway, "_record", record_then_hold)
    errs: list[str] = []

    def call_a():
        try:
            A.AiGateway(ctx).generate("s", "u", None, 100, task="company")
        except Exception as e:  # pragma: no cover – im Fehlerfall sichtbar
            errs.append(repr(e))

    t = threading.Thread(target=call_a)
    t.start()
    assert recorded.wait(5)
    threading.Timer(0.3, go_on.set).start()  # A gibt den Platz nach 0,3 s frei
    try:
        res = gw.generate("s", "u", None, 100, task="company")  # B: 1 von 2 verbraucht – muss durchgehen
    finally:
        go_on.set()
        t.join()
    assert res.model == FLASH_CO and errs == [] and gw.usage_model(FLASH_CO)["requests"] == 2
    with pytest.raises(A.BudgetExceeded):
        gw.generate("s", "u", None, 100, task="company")  # jetzt wirklich voll


def test_manual_starts_keep_a_reserve_and_have_priority(ctx):
    gw = gemini_ctx(ctx, lambda r: gem_ok(), **{"llm.gemini_profile": "free_tier", "llm.manual_reserve_pct": 20,
                                                "llm.model_limits": {FLASH_CO: {"rpm": 0, "tpm": 0, "rpd": 10,
                                                                                "tpd": 0}}})
    assert gw.reserve(FLASH_CO) == 2
    for _ in range(8):
        gw.generate("s", "u", None, 100, task="company")
    with pytest.raises(A.BudgetExceeded) as e:  # Hintergrund: Rest ist für manuelle Starts reserviert
        gw.generate("s", "u", None, 100, task="company")
    assert e.value.scope == "reserve"
    assert DAI.availability(ctx, "company", priority="background")["scope"] == "reserve"
    with A.manual_priority():
        assert DAI.availability(ctx, "company")["state"] == ""
        gw.generate("s", "u", None, 100, task="company")
        gw.generate("s", "u", None, 100, task="company")
        with pytest.raises(A.BudgetExceeded) as e2:
            gw.generate("s", "u", None, 100, task="company")
    assert e2.value.scope == "local"


def test_access_wide_error_pauses_every_model_but_model_errors_only_that_model(ctx):
    def handler(req):
        if _model_of(req) == FLASH_CO:
            return httpx.Response(404, json={"error": {"status": "NOT_FOUND", "message": "model not found"}})
        return gem_ok()

    gw = gemini_ctx(ctx, handler, **{"llm.gemini_profile": "free_tier"})
    with pytest.raises(A.AiRejected) as e:
        gw.generate("s", "u", None, 100, task="company")
    assert e.value.scope == "model"
    assert gw.blocked("company")["model"] == FLASH_CO and gw.blocked("news") is None
    gw.generate("s", "u", None, 100, task="news")  # Nachrichten unberührt
    ctx.http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
        403, json={"error": {"status": "PERMISSION_DENIED", "message": "API key not valid"}})))
    with pytest.raises(A.AiRejected) as e2:
        A.AiGateway(ctx).generate("s", "u", None, 100, task="video")
    assert e2.value.scope == "access"
    for t in A.TASKS:  # Schlüssel/Berechtigung: der ganze Zugang wartet, nicht nur ein Modell
        b = gw.blocked(t)
        assert b is not None and b["scope"] == "access"
    gw.clear_pauses()
    assert all(gw.blocked(t) is None for t in A.TASKS)


# ------------------------------------------------------------------------------------------- Video und Nachrichten

class TaskGateway(FakeGateway):
    """Attrappe mit Modell je Aufgabe: Video-Modell erschöpft, Nachrichtenmodell frei."""

    def __init__(self, answers, video_model: str) -> None:
        super().__init__(answers)
        self.video_model = video_model
        self.tasks: list[str] = []

    def model_for(self, task: str = "news") -> str:
        return self.video_model if task == "video" else LITE

    def generate(self, system, user, schema, max_tokens, json_mode=False, **kw):
        self.tasks.append(kw.get("task", "news"))
        if kw.get("task") == "video" and self.video_model != LITE:
            raise A.Paused(f"Modell {self.video_model} pausiert", datetime.now(UTC) + timedelta(hours=3),
                           self.video_model, "day")
        return super().generate(system, user, schema, max_tokens, json_mode, **kw)


def test_paused_video_model_does_not_stop_news_analysis(ctx):
    ids = seed_for_analysis(ctx, 3)
    vid = with_transcript(ctx, "VIDP")
    ctx.db.x("UPDATE news_item SET tr_requested_at=? WHERE id=?", (iso(datetime.now(UTC)), vid))
    gw = TaskGateway(echo_answers, FLASH_VIDEO)
    res = AnalysisService(ctx, gw).run()
    assert "video" in gw.tasks and "news" in gw.tasks
    assert res.get("video_waiting") and "stopped" not in res
    assert ctx.db.scalar(f"SELECT COUNT(*) FROM news_analysis WHERE item_id IN ({','.join('?' * len(ids))}) "
                         "AND summary IS NOT NULL", tuple(ids)) == 3
    assert ctx.db.scalar("SELECT COUNT(*) FROM news_analysis WHERE item_id=?", (vid,)) == 0  # wartet, nichts verbrannt


def test_same_model_for_video_and_news_stops_both(ctx):
    seed_for_analysis(ctx, 2)
    with_transcript(ctx, "VIDS")

    class SharedPaused(TaskGateway):
        def generate(self, system, user, schema, max_tokens, json_mode=False, **kw):
            self.tasks.append(kw.get("task", "news"))
            raise A.Paused("Modell pausiert", datetime.now(UTC) + timedelta(hours=1), LITE, "day")

    gw = SharedPaused(echo_answers, LITE)
    res = AnalysisService(ctx, gw).run()
    assert res.get("stopped") and gw.tasks == ["video"]  # gemeinsames Kontingent: keine weitere Anfrage


def test_video_card_shows_ai_wait_when_video_model_is_exhausted(ctx):
    gw = gemini_ctx(ctx, lambda r: gem_ok(), **{"llm.gemini_profile": "free_tier", "news.analysis": True})
    vid = with_transcript(ctx, "VIDW")
    gw._pause("gemini", datetime.now(UTC) + timedelta(hours=2), "Kontingent erschöpft", model=FLASH_VIDEO,
              scope="day")
    st = transcript_states(ctx, [vid])[vid]
    assert st["state"] == "ai_waiting" and st["ai_until"] and st["poll"] == "60s"


def test_video_with_and_without_transcript(ctx):
    # ohne Untertitel: „Transkript fehlt“ (wartet auf Einfügen) – kein KI-Aufruf
    vid = add_video(ctx, "NOCAP", requested=True)
    T.TranscriptService(ctx)._upsert(vid, "failed", "no_transcript", url="u", attempts=1)
    assert transcript_states(ctx, [vid])[vid]["state"] == "no_captions"
    gw = FakeGateway(ANSWER)
    AnalysisService(ctx, gw).run()
    assert gw.calls == []
    # eingefügter Text aus einer Browser-Erweiterung (SRT) – Zeitmarken bleiben erhalten
    srt = "\n\n".join(f"{i + 1}\n00:0{i}:05,000 --> 00:0{i}:09,000\nIn Minute {i} spricht der Sprecher über Bitcoin, "
                      f"Zinsen, Gold und die Lage an den Märkten im Herbst." for i in range(6))
    words = T.TranscriptService(ctx).paste(vid, srt)
    assert words >= 30
    content = ctx.db.scalar("SELECT content FROM news_content WHERE item_id=?", (vid,))
    assert content.startswith("[0:05]") and "-->" not in content and "\n1\n" not in content
    st = transcript_states(ctx, [vid])[vid]
    assert st["state"] in ("analyzing", "ai_off") and st["pasted"]
    ans = dict(ANSWER, kernaussagen=[{"text": k, "stelle": f"{i}:05"} for i, k in enumerate(ANSWER["kernaussagen"])])
    gw2 = FakeGateway(ans)
    AnalysisService(ctx, gw2).run()
    assert len(gw2.calls) == 1 and "[0:05]" in gw2.calls[0]["user"]
    a = AnalysisService(ctx).get(vid)
    assert a["kernaussagen"][0] == ANSWER["kernaussagen"][0] and a["kernaussagen_stellen"][:2] == ["0:05", "1:05"]


@pytest.mark.parametrize("text", [
    "(0:00) Einleitung in das Thema\n(1:10) Zweiter Teil\n(2:20) Dritter Teil",
    "[00:00:01.200] erster Abschnitt\n[00:01:02.000] zweiter Abschnitt\n[00:02:03.000] dritter",
    "0:00\nText eins\n1:05\nText zwei\n2:30\nText drei",
    "0:00 – Einleitung\n1:00 – Hauptteil\n2:00 – Ende",
    "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n<c>Hallo</c> zusammen\n\n00:01:00.000 --> 00:01:03.000\nweiter\n\n"
    "00:02:00.000 --> 00:02:02.000\nEnde",
])
def test_pasted_formats_keep_timestamps(text):
    out = T.pasted_paragraphs(text)
    assert out.startswith("[0:0") and "-->" not in out and "<c>" not in out and "WEBVTT" not in out


def test_old_and_new_core_statement_formats_are_accepted():
    old = validate_video(ANSWER)
    assert old and len(old["kernaussagen"]) == 5 and "kernaussagen_stellen" not in old
    new = validate_video(dict(ANSWER, kernaussagen=[{"text": k, "stelle": s} for k, s in
                                                    zip(ANSWER["kernaussagen"], ["0:10", "x", "", "1:02:03", "2:00"],
                                                        strict=True)]))
    assert new["kernaussagen_stellen"] == ["0:10", "", "", "1:02:03", "2:00"]


# ------------------------------------------------------------------------------------------- Discovery: Warteschlange

def test_discovery_uses_its_task_models(config, monkeypatch):
    for c in make_client(config, monkeypatch):
        ctx = c.app.state.ctx
        ctx.settings.set("llm.enabled", True)
        ctx.settings.set("llm.provider", "gemini")
        ctx.settings.set("llm.gemini_profile", "free_tier")
        monkeypatch.setattr("app.news.ai.AiGateway.key_present", lambda self, provider=None: True)
        tasks: list[str] = []
        inner = fake_parts([])

        def gen(self, system, user, schema, max_tokens, json_mode=False, **kw):
            tasks.append(kw.get("task", ""))
            return inner(self, system, user, schema, max_tokens, json_mode, **kw)

        monkeypatch.setattr("app.news.ai.AiGateway.generate", gen)
        cid = int(_add(c, "ALPH").headers["location"].split("/c/")[1].split("?")[0])
        from app.discovery import service as SV

        SV.analyse_company(ctx, cid, mode="ki")
        assert tasks == ["company"]
        assert ctx.db.scalar("SELECT model FROM disc_ai_call ORDER BY id DESC LIMIT 1") in (FLASH_CO, "gemini-test")
        assert DAI.task_of("themen") == "discovery" and DAI.task_of("thesen") == "discovery"


def test_discovery_job_waits_visibly_and_starts_after_the_wait(config, monkeypatch):
    for c in make_client(config, monkeypatch):
        ctx = c.app.state.ctx
        ctx.settings.set("llm.enabled", True)
        monkeypatch.setattr("app.news.ai.AiGateway.key_present", lambda self, provider=None: True)
        seen: list[tuple[str, str]] = []
        monkeypatch.setattr("app.news.ai.AiGateway.generate", fake_parts(seen))
        cid = int(_add(c, "ALPH").headers["location"].split("/c/")[1].split("?")[0])
        ctx.settings.set("discovery.ai_calls_per_day", 0)
        r = J.start(ctx, "analyse", trigger="manuell", company_id=cid, params={"mode": "ki"})
        jb = J.get(ctx.db, r["id"])
        assert jb["status"] == "wartet" and jb["waiting"] and jb["active"] and seen == []
        assert "Tagesgrenze" in jb["wait_reason"] and jb["wait_until"] > iso(datetime.now(UTC))
        again = J.start(ctx, "analyse", trigger="manuell", company_id=cid, params={"mode": "ki"})
        assert not again["new"] and again["id"] == r["id"]  # kein Doppelstart während der Wartezeit
        assert J.wake_waiting(ctx) == []  # Wartezeit noch nicht vorbei
        ctx.settings.set("discovery.ai_calls_per_day", 50)  # neuer Tag
        ctx.db.x("UPDATE disc_job SET wait_until=? WHERE id=?", (iso(datetime.now(UTC) - timedelta(seconds=1)),
                                                                 r["id"]))
        assert J.wake_waiting(ctx) == [r["id"]]
        jb = J.get(ctx.db, r["id"])
        assert jb["status"] == "abgeschlossen" and [p for p, _ in seen] == ["analyse"]


def test_waiting_jobs_wake_manual_first_and_survive_restart(config, monkeypatch):
    for c in make_client(config, monkeypatch):
        ctx = c.app.state.ctx
        past = iso(datetime.now(UTC) - timedelta(minutes=1))
        ids = []
        for trig, cid in (("automatisch", 101), ("manuell", 102)):
            jid, _ = J.create(ctx, "analyse", trigger=trig, company_id=cid, params={"mode": "daten"})
            ctx.db.x("UPDATE disc_job SET status='wartet', wait_until=?, wait_reason='Test', worker='alt' WHERE id=?",
                     (past, jid))
            ids.append(jid)
        assert J.recover(ctx) == 0  # Neustart: wartende Jobs bleiben in der Warteschlange
        order: list[int] = []
        monkeypatch.setattr(J, "dispatch", lambda ctx_, jid, kind: order.append(jid) or "sync")
        assert J.wake_waiting(ctx) == [ids[1], ids[0]] and order == [ids[1], ids[0]]


def test_minute_limit_wait_reason_and_status_card(config, monkeypatch):
    for c in make_client(config, monkeypatch):
        ctx = c.app.state.ctx
        ctx.settings.set("llm.enabled", True)
        ctx.settings.set("llm.provider", "gemini")
        ctx.settings.set("llm.gemini_profile", "free_tier")
        monkeypatch.setattr("app.news.ai.AiGateway.key_present", lambda self, provider=None: True)
        gw = A.AiGateway(ctx)
        gw._pause("gemini", datetime.now(UTC) + timedelta(seconds=40), "Minutenlimit", model=FLASH_CO,
                  scope="minute")
        av = DAI.availability(ctx, "company")
        assert av["state"] == "wartet" and av["scope"] == "minute" and FLASH_CO in av["reason"]
        assert DAI.availability(ctx, "discovery")["state"] == ""  # anderes Modell
        page = c.get("/discovery").text
        assert 'id="ki-modelle"' in page and FLASH_CO in page and "Minutenlimit" in page
        status = c.get("/news?tab=status").text
        assert 'id="ki-aufgaben"' in status and "Firmenanalyse" in status


def test_settings_json_is_public_data_only():
    """Die Modellzuordnung ändert nichts an der Nutzlast: weiterhin nur öffentliche Daten (siehe test_discovery)."""
    assert "Depot" not in json.dumps(A.PROFILES) and set(A.TASKS) == {"news", "events", "report", "discovery",
                                                                      "video", "company"}
