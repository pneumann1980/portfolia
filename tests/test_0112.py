"""0.112: Klammer-Reparatur für KI-Antworten, von Google gemeldete Grenzen (429 ``quotaValue``), Kontingent 0,
Modellliste (``models.list``), Position halten nach Formular-Knöpfen auf den Discovery-Seiten.

Keine echten KI- oder Netzaufrufe: HTTP über ``httpx.MockTransport``, synthetische Daten."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.discovery import ai as DAI
from app.news import ai as A
from app.util.timeutil import iso
from tests.test_discovery import _add, make_client
from tests.test_news_intel import gem_ok, gemini_ctx

LITE, FLASH = "gemini-3.5-flash-lite", "gemini-3.8-flash"


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


def quota(qid: str, value: str | None, *, delay: str = "") -> httpx.Response:
    v: dict = {"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
               "quotaId": qid, "quotaDimensions": {"location": "global", "model": "x"}}
    if value is not None:
        v["quotaValue"] = value
    details: list = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [v]}]
    if delay:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay})
    return httpx.Response(429, json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota",
                                               "details": details}})


# ------------------------------------------------------------------------------------------- JSON-Reparatur

@pytest.mark.parametrize(("text", "expected"), [
    ('{"themen": [{"thema": "A"}, {"thema": "B"}}', {"themen": [{"thema": "A"}, {"thema": "B"}]}),
    ('{"themen": [{"thema": "A", "x": [1, 2}]}', {"themen": [{"thema": "A", "x": [1, 2]}]}),
    ('{"themen": [{"thema": "A"}]}}', {"themen": [{"thema": "A"}]}),
    ('```json\n{"t": [{"s": "Text mit } und ] im Satz"}]\n```', {"t": [{"s": "Text mit } und ] im Satz"}]}),
    ('Hier das Ergebnis:\n{"a": [1, 2,], "b": {"c": "d"},}\nViel Erfolg', {"a": [1, 2], "b": {"c": "d"}}),
    ('{"a": "Zitat mit \\"Anführung\\" }", "b": [1]', {"a": 'Zitat mit "Anführung" }', "b": [1]}),
])
def test_bracket_repair_keeps_content(text, expected):
    assert DAI.parse_lenient(text) == expected


def test_valid_json_is_untouched_and_garbage_still_fails():
    assert DAI.parse_lenient('{"a": [1, {"b": "}"}]}') == {"a": [1, {"b": "}"}]}
    with pytest.raises(ValueError):
        DAI.parse_lenient("kein JSON, nur Text")


def test_call_repairs_brackets_and_reports_tail_when_unrepairable(config, monkeypatch):
    for c in make_client(config, monkeypatch):
        ctx = c.app.state.ctx
        ctx.settings.set("llm.enabled", True)
        monkeypatch.setattr("app.news.ai.AiGateway.key_present", lambda self, provider=None: True)
        answers = ['{"themen": [{"thema": "Netzteile", "fakten": ["x"]}}',  # } statt ]}
                   '{"themen": "kaputt" "noch": was}']
        monkeypatch.setattr("app.news.ai.AiGateway.generate",
                            lambda self, *a, **k: A.AiResult(answers.pop(0), 10, 10, "gemini", LITE))
        data, meta = DAI.call(ctx, "themen", "SYS", "{}", max_tokens=100, prompt_version=1)
        assert meta["status"] == "ok" and data == {"themen": [{"thema": "Netzteile", "fakten": ["x"]}]}
        data2, meta2 = DAI.call(ctx, "themen", "SYS", "{}", max_tokens=100, prompt_version=1)
        assert data2 is None and meta2["status"] == "ungueltig"
        assert "Ende der Antwort: „…" in meta2["error"] and "noch" in meta2["error"]
        row = ctx.db.q1("SELECT error FROM disc_ai_call ORDER BY id DESC LIMIT 1")
        assert "Ende der Antwort" in row["error"]


# ------------------------------------------------------------------------------------------- Grenzen von Google

def test_daily_quota_value_from_google_becomes_upper_limit(ctx):
    gw = gemini_ctx(ctx, lambda r: quota("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "20"),
                    **{"llm.gemini_profile": "free_tier"})
    assert gw.limits(FLASH)["rpd"] == A.LIMIT_DEFAULTS["flash"]["rpd"]  # Startwert (Annahme)
    with pytest.raises(A.QuotaExceeded) as e:
        gw.generate("s", "u", None, 100, task="company")
    assert e.value.limits == {"rpd": 20} and "Google meldet Anfragen/Tag 20" in e.value.text
    assert gw.learned(FLASH) == {"rpd": 20}
    assert gw.limits(FLASH)["rpd"] == 20 and gw.limits(FLASH, learned=False)["rpd"] == A.LIMIT_DEFAULTS["flash"]["rpd"]
    assert gw.limits(LITE) == gw.limits(LITE, learned=False)  # nur das betroffene Modell
    ctx.settings.set("llm.model_limits", {FLASH: {"rpm": 5, "tpm": 0, "rpd": 500, "tpd": 0}})
    assert gw.limits(FLASH)["rpd"] == 20  # eigene höhere Einstellung: Googles Wert bleibt Obergrenze
    ctx.settings.set("llm.model_limits", {FLASH: {"rpm": 5, "tpm": 0, "rpd": 10, "tpd": 0}})
    assert gw.limits(FLASH)["rpd"] == 10  # eigene niedrigere Einstellung gilt
    row = next(r for r in gw.task_status() if r["task"] == "company")
    assert row["learned"] == {"rpd": 20}


def test_minute_quota_value_and_expiry_and_forget(ctx):
    gw = gemini_ctx(ctx, lambda r: quota("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "7", delay="20s"),
                    **{"llm.gemini_profile": "free_tier"})
    with pytest.raises(A.QuotaExceeded) as e:
        gw.generate("s", "u", None, 100, task="news")
    own = gw.limits(LITE, learned=False)["rpm"]  # Grundmodell: bisherige Einstellung (5/min)
    assert e.value.scope == "minute" and gw.learned(LITE) == {"rpm": 7} and gw.limits(LITE)["rpm"] == min(own, 7)
    ctx.settings.set("llm.gemini_rpm", 60)
    assert gw.limits(LITE)["rpm"] == 7  # höhere eigene Einstellung: Googles Wert begrenzt
    st = ctx.db.get_state("ai.learned_limits")
    st[LITE]["at"] = iso(datetime.now(UTC) - timedelta(days=A.LEARNED_DAYS + 1))
    ctx.db.set_state("ai.learned_limits", st)
    assert gw.learned(LITE) == {} and gw.limits(LITE)["rpm"] == 60  # veraltet: wieder die eigene Einstellung
    gw._learn(LITE, {"rpd": 30})
    assert gw.learned(LITE) == {"rpd": 30}  # veralteter Eintrag wird nicht mitgeführt
    gw.forget_learned()
    assert gw.learned_all() == {}


def test_quota_without_value_learns_nothing(ctx):
    gw = gemini_ctx(ctx, lambda r: quota("GenerateRequestsPerDayPerProjectPerModel-FreeTier", None))
    with pytest.raises(A.QuotaExceeded) as e:
        gw.generate("s", "u", None, 100)
    assert e.value.limits == {} and gw.learned_all() == {} and "Google meldet" not in e.value.text


def test_quota_zero_means_model_not_in_free_tier_and_pauses_only_that_model(ctx):
    def handler(req):
        if FLASH in str(req.url):
            return quota("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "0")
        return gem_ok()

    gw = gemini_ctx(ctx, handler, **{"llm.gemini_profile": "free_tier"})
    with pytest.raises(A.AiRejected) as e:
        gw.generate("s", "u", None, 100, task="company")
    assert e.value.scope == "model" and "Kontingent 0" in e.value.text
    b = gw.blocked("company")
    assert b is not None and b["model"] == FLASH and b["scope"] == "model"
    assert gw.blocked("news") is None and gw.generate("s", "u", None, 100, task="news").model == LITE
    assert gw.learned_all() == {}  # 0 wird nie als „unbegrenzt“ gespeichert


# ------------------------------------------------------------------------------------------- Modellliste

def models_handler(seen: list[httpx.Request]):
    pages = {
        "": {"models": [
            {"name": "models/gemini-3.5-flash-lite", "displayName": "Lite", "inputTokenLimit": 1048576,
             "outputTokenLimit": 65536, "supportedGenerationMethods": ["generateContent", "countTokens"]},
            {"name": "models/gemini-3.9-flash", "displayName": "Neu", "inputTokenLimit": 1048576,
             "outputTokenLimit": 65536, "supportedGenerationMethods": ["generateContent"], "thinking": True}],
            "nextPageToken": "p2"},
        "p2": {"models": [
            {"name": "models/gemini-3.5-pro", "displayName": "Pro", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/text-embedding-9", "displayName": "Emb",
             "supportedGenerationMethods": ["embedContent"]}]},
    }

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=pages[req.url.params.get("pageToken", "")])
    return handler


def test_model_list_from_google_without_quota_and_key_in_header(ctx):
    seen: list[httpx.Request] = []
    gw = gemini_ctx(ctx, models_handler(seen))
    st = gw.refresh_models()
    ids = [m["id"] for m in st["models"]]
    assert ids == ["gemini-3.5-flash-lite", "gemini-3.9-flash", "gemini-3.5-pro"]  # nur generateContent
    assert len(seen) == 2 and all("key=" not in str(r.url) for r in seen)
    assert seen[0].headers["x-goog-api-key"] == ctx.config.secrets.gemini_api_key
    assert gw.usage("gemini")["requests"] == 0  # kein Kontingentverbrauch gezählt
    assert gw.models_available()["models"][1]["thinking"] is True


def test_settings_offer_reported_flash_models_but_never_pro(config, monkeypatch):
    from dataclasses import replace

    from app.config import Secrets

    for c in make_client(config, monkeypatch):
        ctx = c.app.state.ctx
        ctx.config = replace(ctx.config, secrets=Secrets(gemini_api_key="test-gemini-key-0123456789abcdef"))
        ctx.settings.set("llm.provider", "gemini")
        seen: list[httpx.Request] = []
        ctx.http = httpx.Client(transport=httpx.MockTransport(models_handler(seen)))
        r = c.post("/settings/ai-models", data={"csrf_token": c.token}, follow_redirects=False)
        assert r.status_code == 303 and "models=3" in r.headers["location"]
        page = c.get(r.headers["location"]).text
        assert "Modellliste abgerufen (3 Modelle)" in page and "Verfügbare Modelle abrufen" in page
        assert '<option value="gemini-3.9-flash"' in page  # neues Flash-Modell wählbar ohne Codeänderung
        assert '<option value="gemini-3.5-pro"' not in page  # kostenpflichtig: nie wählbar
        assert "gemini-2.5-flash – von Google nicht gemeldet" in page
        ctx.http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(
            403, json={"error": {"status": "PERMISSION_DENIED", "message": "API key not valid"}})))
        r2 = c.post("/settings/ai-models", data={"csrf_token": c.token}, follow_redirects=False)
        page2 = c.get(r2.headers["location"]).text
        assert "Modellliste nicht abrufbar" in page2 and "test-gemini-key" not in page2
        assert A.AiGateway(ctx).access_paused()[0] is None  # Abruf der Liste pausiert nichts


def test_settings_show_learned_limit_and_forget(config, monkeypatch):
    for c in make_client(config, monkeypatch):
        ctx = c.app.state.ctx
        ctx.settings.set("llm.provider", "gemini")
        ctx.settings.set("llm.gemini_profile", "free_tier")
        A.AiGateway(ctx)._learn(FLASH, {"rpd": 20})
        page = c.get("/settings").text
        assert "Google meldet" in page and "20/Tag" in page
        form = {"csrf_token": c.token, "section": "news", "gemini_model": LITE, "gemini_rpm": "5",
                "gemini_tpm": "100000", "gemini_rpd": "100", "gemini_budget": "200000", "llm_provider": "gemini",
                "gemini_profile": "free_tier", "forget_learned": "1"}
        c.post("/settings/save", data=form, follow_redirects=False)
        assert A.AiGateway(ctx).learned_all() == {}


# ------------------------------------------------------------------------------------------- Position halten

def test_discovery_pages_keep_position_after_buttons(config, monkeypatch):
    for c in make_client(config, monkeypatch):
        cid = int(_add(c, "ALPH").headers["location"].split("/c/")[1].split("?")[0])
        radar = c.get("/discovery?msg=ok").text
        assert "data-keep-scroll" in radar and 'data-flash' in radar and f'id="karte-{cid}"' in radar
        page = c.get(f"/discovery/c/{cid}?msg=ok").text
        assert "data-keep-scroll" in page and "data-flash" in page
        js = c.get("/static/js/app.js").text
        assert "pf-keep-scroll" in js and "htmx:afterSettle" in js
        assert "data-keep-scroll" not in c.get("/settings").text  # nur wo angemeldet


def test_json_dump_of_learned_state_is_plain():
    """Der gespeicherte Zustand bleibt einfaches JSON (Sicherung/Export)."""
    json.dumps({"m": {"rpd": 20, "at": iso(datetime.now(UTC))}})
