"""Claude-Dienst (``news.llm``): Aufrufpfade, Fehler, Budget, Zusammenfassung und Tagesüberblick – ohne Netz."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from app.context import AppContext
from app.news.llm import LlmService
from app.util.timeutil import iso

REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


@pytest.fixture
def ctx(config):
    c = AppContext(config)
    c.startup()
    yield c
    c.shutdown()


class FakeMessages:
    def __init__(self, outcome):
        self.outcome, self.calls = outcome, []

    def create(self, **kw):
        self.calls.append(kw)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def reply(text="{}", stop="end_turn", inp=10, out=5):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=inp, output_tokens=out))


def svc(ctx, outcome, *, model=None, key="sk-test-123456"):
    s = LlmService(ctx)
    m = FakeMessages(outcome)
    s._client = SimpleNamespace(messages=m, beta=SimpleNamespace(messages=m))
    if model:
        ctx.settings.set("llm.model", model)
    s.fake = m  # type: ignore[attr-defined]
    return s


def add_news(ctx, n=3, asset="BTC", hours=1.0, score=0.9):
    ids = []
    for i in range(n):
        ts = iso(datetime.now(UTC) - timedelta(hours=hours))
        cur = ctx.db.x(
            """INSERT INTO news_item(kind, url, url_norm, title, title_norm, summary, source_id, source_name,
                   source_weight, language, published_at, fetched_at, relevance, dup_count) VALUES
               ('article',?,?,?,?,?,'s','S',0.5,'de',?,?,0.8,0)""",
            (f"https://e.org/{asset}{i}", f"u{asset}{i}", f"Titel {asset} {i}", f"titel {asset} {i}", "Text", ts, ts))
        iid = int(cur.lastrowid)
        ctx.db.x("INSERT INTO news_asset(item_id, asset_id, score) VALUES (?,?,?)", (iid, asset, score))
        ids.append(iid)
    return ids


def test_enabled_requires_key_switch_and_claude_provider(ctx, monkeypatch):
    s = LlmService(ctx)
    assert not s.enabled()
    object.__setattr__(ctx.config.secrets, "anthropic_api_key", "sk-test-123456")
    ctx.settings.set("llm.enabled", True)
    assert s.enabled()
    ctx.settings.set("llm.provider", "gemini")
    assert not s.enabled()


def test_call_uses_beta_fallback_for_opus_and_plain_for_others_and_records_usage(ctx):
    s = svc(ctx, reply("hallo", inp=100, out=20), model="claude-opus-5")
    assert s._call("sys", "payload", 500, None) == "hallo"
    kw = s.fake.calls[0]
    assert kw["betas"] == ["server-side-fallback-2026-07-01"] and kw["output_config"] == {"effort": "low"}
    assert s.usage_today() == (100, 20, 1)
    s2 = svc(ctx, reply("x"), model="claude-haiku-4-5")
    s2._call("sys", "p", 100, {"type": "object"})
    kw2 = s2.fake.calls[0]
    assert "betas" not in kw2 and "effort" not in kw2["output_config"]
    assert kw2["output_config"]["format"]["type"] == "json_schema"


@pytest.mark.parametrize("exc", [
    anthropic.RateLimitError("zu viele", response=httpx.Response(429, request=REQ), body=None),
    anthropic.InternalServerError("kaputt", response=httpx.Response(500, request=REQ), body=None),
    anthropic.APIConnectionError(request=REQ),
])
def test_call_returns_none_on_provider_errors_without_recording_usage(ctx, exc):
    s = svc(ctx, exc)
    assert s._call("sys", "p", 100, None) is None
    assert s.usage_today() == (0, 0, 0)


def test_refusal_is_skipped_but_tokens_are_counted(ctx):
    s = svc(ctx, reply("nein", stop="refusal", inp=7, out=1))
    assert s._call("sys", "p", 100, None) is None and s.usage_today() == (7, 1, 1)


def test_budget_blocks_the_call(ctx):
    ctx.settings.set("llm.daily_token_budget", 1000)
    s = svc(ctx, reply("x"))
    assert s._call("s", "p", 5000, None) is None and not s.fake.calls  # geschätzt > Budget → gar nicht erst anfragen
    assert s.budget_left() == 1000


def test_candidates_limit_per_asset_and_floor(ctx):
    add_news(ctx, 5, "BTC")
    add_news(ctx, 5, "ETH")
    add_news(ctx, 2, "LOW", score=0.01)  # unter news.min_relevance
    rows = LlmService(ctx).candidates(limit=12)
    per = {}
    for r in rows:
        per[r["asset_id"]] = per.get(r["asset_id"], 0) + 1
    assert per == {"BTC": 3, "ETH": 3}  # höchstens 3 je Asset, schwache Treffer fehlen
    assert len(LlmService(ctx).candidates(limit=2)) == 2


def test_summarize_applies_valid_items_and_survives_garbage(ctx):
    object.__setattr__(ctx.config.secrets, "anthropic_api_key", "sk-test-123456")
    ctx.settings.set("llm.enabled", True)
    a, b, _c = add_news(ctx, 3)
    items = [{"id": a, "summary": "Kurz.", "relevance": 7}, {"id": b, "summary": "", "relevance": 0.2},
             {"id": 99999, "summary": "fremd", "relevance": 0.5}]
    good = json.dumps({"items": items})
    s = svc(ctx, reply(good))
    res = s.summarize()
    assert res["summarized"] == 1
    row = ctx.db.q1("SELECT llm_summary, llm_score FROM news_item WHERE id=?", (a,))
    assert row["llm_summary"] == "Kurz." and row["llm_score"] == 1.0  # Relevanz auf 0..1 begrenzt
    assert ctx.db.q1("SELECT llm_summary FROM news_item WHERE id=?", (b,))["llm_summary"] is None
    assert svc(ctx, reply("kein json")).summarize() == {"summarized": 0}
    assert svc(ctx, anthropic.APIConnectionError(request=REQ)).summarize() == {"summarized": 0}


def test_summarize_and_digest_skip_when_disabled_or_nothing_to_do(ctx):
    assert LlmService(ctx).summarize() == {"skipped": "LLM deaktiviert"}
    assert "skipped" in LlmService(ctx).digest()
    object.__setattr__(ctx.config.secrets, "anthropic_api_key", "sk-test-123456")
    ctx.settings.set("llm.enabled", True)
    assert svc(ctx, reply()).summarize() == {"summarized": 0}  # keine Kandidaten
    assert svc(ctx, reply()).digest() == {"skipped": "keine relevanten Meldungen"}


def test_digest_is_stored_once_per_day(ctx):
    object.__setattr__(ctx.config.secrets, "anthropic_api_key", "sk-test-123456")
    ctx.settings.set("llm.enabled", True)
    add_news(ctx, 2)
    s = svc(ctx, reply("  Überblick heute  "))
    assert s.digest()["digest"] is True
    assert ctx.db.scalar("SELECT content FROM digest") == "Überblick heute"
    assert svc(ctx, reply("zweiter")).digest() == {"skipped": "bereits erstellt"}
    ctx.db.x("DELETE FROM digest")
    assert svc(ctx, anthropic.APIConnectionError(request=REQ)).digest() == {"digest": False}


def test_payload_contains_only_titles_and_asset_names(ctx):
    add_news(ctx, 1)
    s = LlmService(ctx)
    payload = json.loads(s.build_payload(s.candidates()))
    assert set(payload["meldungen"][0]) == {"id", "assets", "title", "text"}  # keine Mengen, Werte, Konten
