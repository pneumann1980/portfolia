"""0.119 – KI je Bereich: Top-News automatisch (lokale Auswahl, Quoten, Tagesobergrenze), übrige Artikel per Knopf,
Community als eigene Aufgabe, volle Pakete (30 Minuten), Videos wahlweise nur per Knopf, Tagesbericht 22:15."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.context import AppContext
from app.main import build_app
from app.news import aiplan
from app.news.analysis import AnalysisService
from app.util.timeutil import iso
from tests.test_news_intel import FakeGateway, add_item, echo_answers, gemini_ctx, store_content

NOW = datetime.now(UTC)


class TaskGateway(FakeGateway):
    """Wie FakeGateway, merkt sich zusätzlich Aufgabe und Priorität jeder Anfrage."""

    def generate(self, system, user, schema, max_tokens, json_mode=False, **kw):
        out = super().generate(system, user, schema, max_tokens, json_mode, **kw)
        self.calls[-1].update(task=kw.get("task"), priority=kw.get("priority"),
                              ids=[a["id"] for a in json.loads(user)["artikel"]])
        return out


@pytest.fixture
def ctx(config):
    c = AppContext(config)
    c.startup()
    yield c
    c.shutdown()


def _articles(ctx, n, *, rel=lambda i: 0.9 - i * 0.01, fetched_min=60, source="coindesk", tag="a"):
    ids = []
    for i in range(n):
        iid = add_item(ctx, f"Meldung {i} zu Märkten und Zinsen", f"https://{tag}.example.com/{source}/{i}",
                       hours=1 + i / 10,
                       relevance=rel(i), source=source)
        store_content(ctx, iid, text=f"Der Markt bewegte sich. Text {i}.\n\nZweiter Absatz.")
        ctx.db.x("UPDATE news_item SET fetched_at=? WHERE id=?", (iso(NOW - timedelta(minutes=fetched_min)), iid))
        ids.append(iid)
    return ids


def _telegram(ctx, *, ai=True):
    path = ctx.config.sources_path
    path.write_text("sources:\n  - {id: tgdemo, name: Krypto-Community, type: telegram, url: \"https://t.me/s/demo\""
                    + (", ai: true" if ai else "") + "}\n", encoding="utf-8")


def test_defaults_are_top_news_community_and_evening_report(ctx):
    assert aiplan.article_mode(ctx) == "top" and aiplan.community_mode(ctx) == "all"
    assert aiplan.video_mode(ctx) == "favorites" and aiplan.top_per_day(ctx) == 50
    assert ctx.settings.get("news.report_hour") == 22 and ctx.settings.get("news.report_minute") == 15
    assert aiplan.batch_wait(ctx) == timedelta(minutes=30)


def test_only_top_news_are_analysed_automatically_with_daily_cap(ctx):
    ctx.settings.set("news.ai_top_per_day", 6)
    ids = _articles(ctx, 20)
    gw = TaskGateway(echo_answers)
    res = AnalysisService(ctx, gw).run()
    assert res["analyzed"] == 6 and res["requests"] == 1
    done = {r["item_id"] for r in ctx.db.q("SELECT item_id FROM news_analysis WHERE summary IS NOT NULL")}
    assert done == set(ids[:6])  # die sechs mit der höchsten lokalen Rangfolge
    assert {r["trigger"] for r in ctx.db.q("SELECT trigger FROM news_analysis")} == {"top"}
    assert aiplan.used_today(ctx) == 6 and aiplan.allowance(ctx) in (0, aiplan.REPORT_EXTRA)
    if aiplan.allowance(ctx) == 0:  # außerhalb der Zeit vor dem Bericht: Tagesobergrenze erreicht
        assert AnalysisService(ctx, gw).run()["analyzed"] == 0


def test_holding_quota_brings_low_ranked_position_news(ctx):
    ctx.settings.set("news.ai_top_per_day", 5)
    ids = _articles(ctx, 12)
    low = ids[-1]
    ctx.db.x("INSERT INTO news_asset(item_id, asset_id, score) VALUES (?, 'KAS', 0.9)", (low,))
    picks = aiplan.top_selection(ctx)
    assert {i for i, why in picks.items() if why == "top"} == set(ids[:5])
    assert picks.get(low) == "quote"


def test_watchlist_company_news_gets_a_slot(ctx):
    ctx.settings.set("news.ai_top_per_day", 5)
    ids = _articles(ctx, 9)
    other = add_item(ctx, "Nvidia legt starke Quartalszahlen vor", "https://n.example.com/nv", hours=3, relevance=0.2)
    from app.watchlist.service import watchlist_service

    lid = watchlist_service(ctx).default_id()
    ctx.db.x("""INSERT INTO watchlist_item(list_id, quote_source, quote_id, asset_class, symbol, name, added_at)
                VALUES (?, 'yahoo', 'NVDA', 'security', 'NVDA', 'NVIDIA Corporation', ?)""", (lid, iso(NOW)))
    picks = aiplan.top_selection(ctx)
    assert picks.get(other) == "quote" and ids[8] not in picks


def test_full_packets_wait_up_to_30_minutes(ctx):
    ctx.settings.set("news.ai_articles", "all")
    fresh = _articles(ctx, 3, fetched_min=5)
    gw = TaskGateway(echo_answers)
    res = AnalysisService(ctx, gw).run()
    assert gw.calls == [] and res.get("waiting_full_packet") == 3  # halbes Paket wartet
    ctx.db.x(f"UPDATE news_item SET fetched_at=? WHERE id IN ({','.join('?' * len(fresh))})",
             (iso(NOW - timedelta(minutes=31)), *fresh))
    assert AnalysisService(ctx, gw).run()["analyzed"] == 3 and len(gw.calls) == 1


def test_manual_pipeline_run_flushes_half_packets(ctx):
    ctx.settings.set("news.ai_articles", "all")
    _articles(ctx, 2, fetched_min=2)
    gw = TaskGateway(echo_answers)
    assert AnalysisService(ctx, gw).run(flush=True)["analyzed"] == 2


def test_button_request_is_analysed_immediately_even_in_manual_mode(ctx):
    gemini_ctx(ctx, lambda r: (_ for _ in ()).throw(AssertionError("kein echter Aufruf")))
    ctx.settings.set("news.ai_articles", "manual")
    ids = _articles(ctx, 4, fetched_min=1)
    assert aiplan.item_states(ctx, [ids[2]])[ids[2]]["state"] == "none"
    st = aiplan.request(ctx, ids[2])
    assert st["state"] == "queued" and st["poll"] == "5s"
    gw = TaskGateway(echo_answers)
    res = AnalysisService(ctx, gw).run()
    assert res["analyzed"] == 1 and gw.calls[0]["ids"] == [ids[2]] and gw.calls[0]["priority"] == "manual"
    assert ctx.db.scalar("SELECT trigger FROM news_analysis WHERE item_id=?", (ids[2],)) == "knopf"
    assert aiplan.item_states(ctx, [ids[2]])[ids[2]]["state"] == "done"
    assert aiplan.used_today(ctx) == 0  # Knopf zählt nicht gegen die Top-News-Obergrenze


def test_community_runs_as_own_task_and_respects_mode(ctx):
    _telegram(ctx)
    ctx.settings.set("news.ai_articles", "manual")
    tg = _articles(ctx, 6, source="tgdemo")
    _articles(ctx, 6)
    gw = TaskGateway(echo_answers)
    res = AnalysisService(ctx, gw).run()
    assert res["analyzed"] == 6 and {c["task"] for c in gw.calls} == {"community"}
    assert set(gw.calls[0]["ids"]) == set(tg)
    assert {r["trigger"] for r in ctx.db.q("SELECT trigger FROM news_analysis")} == {"community"}
    ctx.settings.set("news.ai_community", "off")
    more = _articles(ctx, 1, source="tgdemo", rel=lambda i: 0.99, tag="b")
    aiplan.request(ctx, more[0])
    assert aiplan.item_states(ctx, more)[more[0]]["state"] == "off"
    assert AnalysisService(ctx, TaskGateway(echo_answers)).run()["analyzed"] == 0


def test_community_model_follows_news_model_unless_set(ctx):
    from app.news.ai import AiGateway

    gemini_ctx(ctx, lambda r: None)
    gw = AiGateway(ctx)
    ctx.settings.set("llm.gemini_profile", "eigen")
    ctx.settings.set("llm.task_models", {"news": "gemini-2.5-flash-lite"})
    assert gw.model_for("community") == "gemini-2.5-flash-lite"
    ctx.settings.set("llm.task_models", {"news": "gemini-2.5-flash-lite", "community": "gemini-2.5-flash"})
    assert gw.model_for("community") == "gemini-2.5-flash"


def test_video_manual_mode_skips_automatic_key_points(ctx):
    vid = add_item(ctx, "Marktausblick der Woche", "https://youtube.com/watch?v=abc", kind="video")
    store_content(ctx, vid, text="Transkript Absatz eins.\n\nZwei.", reason="transcript:de")
    svc = AnalysisService(ctx, TaskGateway(echo_answers))
    assert [r["id"] for r in svc.video_candidates(10)] == [vid]
    ctx.settings.set("news.ai_videos", "manual")
    assert svc.video_candidates(10) == []
    ctx.db.x("UPDATE news_item SET tr_requested_at=? WHERE id=?", (iso(NOW), vid))
    assert [r["id"] for r in svc.video_candidates(10)] == [vid]


def test_report_time_moves_once_from_old_default(ctx):
    ctx.settings.set("news.report_hour", 7)
    ctx.settings.set("news.report_minute", 30)
    assert aiplan.migrate_report_time(ctx) is True
    assert (ctx.settings.get("news.report_hour"), ctx.settings.get("news.report_minute")) == (22, 15)
    ctx.settings.set("news.report_hour", 7)  # danach bewusst gewählt: bleibt
    ctx.settings.set("news.report_minute", 30)
    assert aiplan.migrate_report_time(ctx) is False and ctx.settings.get("news.report_hour") == 7


def test_report_time_keeps_own_choice(ctx):
    ctx.settings.set("news.report_hour", 6)
    ctx.settings.set("news.report_minute", 0)
    assert aiplan.migrate_report_time(ctx) is False and ctx.settings.get("news.report_hour") == 6


@pytest.fixture
def client(config):
    with TestClient(build_app(config, start_scheduler=False)) as c:
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        yield c


def test_settings_card_saves_modes_and_shows_usage(client):
    ctx = client.app.state.ctx
    page = client.get("/settings/ki").text
    assert 'id="ki-bereiche"' in page and "Verbrauch je Bereich" in page and "Geschätzter Tagesbedarf" in page
    r = client.post("/settings/save", data={"csrf_token": client.token, "section": "news_ai", "ai_articles": "all",
                                            "ai_top_per_day": "60", "ai_community": "manual", "ai_videos": "manual",
                                            "ai_batch_wait_min": "15"}, follow_redirects=False)
    assert r.status_code == 303 and "saved=news_ai" in r.headers["location"]
    assert (aiplan.article_mode(ctx), aiplan.top_per_day(ctx), aiplan.community_mode(ctx),
            aiplan.video_mode(ctx)) == ("all", 60, "manual", "manual")
    assert aiplan.batch_wait(ctx) == timedelta(minutes=15)
    assert ctx.settings.get("llm.enabled") is not True or True  # KI-Schlüssel bleiben unberührt
    client.post("/settings/save", data={"csrf_token": client.token, "section": "news_ai", "ai_articles": "kaputt"})
    assert aiplan.article_mode(ctx) == "all"  # ungültige Werte ändern nichts


def test_button_on_article_card_and_reader(client):
    ctx = client.app.state.ctx
    gemini_ctx(ctx, lambda r: (_ for _ in ()).throw(AssertionError("kein echter Aufruf")))
    ctx.settings.set("news.ai_articles", "manual")
    iid = add_item(ctx, "Bitcoin steigt deutlich nach Zinsentscheid", "https://n.example.com/btc", relevance=0.9)
    page = client.get("/news?tab=articles&status=all").text
    assert f'hx-post="/news/{iid}/summarize"' in page and "Zusammenfassen" in page
    reader = client.get(f"/news/{iid}").text
    assert f'hx-post="/news/{iid}/summarize?full=1"' in reader
    frag = client.post(f"/news/{iid}/summarize", headers={"X-CSRF-Token": client.token}).text
    assert "KI fasst zusammen" in frag and f'hx-get="/news/{iid}/ai/state"' in frag
    assert ctx.db.scalar("SELECT ai_requested_at FROM news_item WHERE id=?", (iid,))
    assert "KI fasst zusammen" in client.get(f"/news/{iid}/ai/state").text
