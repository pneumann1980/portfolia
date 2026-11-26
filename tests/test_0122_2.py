"""0.122.2 – Messpunkte: Zeitanteile der News-Seite (Warten, Rechenzeit, Datenbank, Portfolio, Seitenaufbau) und
KI-Fehler nach Art mit letztem Fehlertext."""

from __future__ import annotations

import httpx
import pytest

from app.news import ai as A
from app.util import reqtiming
from app.web import accesslog
from tests.test_news_intel import ctx, gem_ok, gemini_ctx  # noqa: F401


@pytest.fixture
def client(config):
    from tests.test_journal import _client

    for d in (accesslog._timings, accesslog._parts):
        for q in d.values():
            q.clear()
    with _client(config) as c:
        c.get("/settings")
        yield c


def test_news_page_records_time_parts(client):
    for _ in range(3):
        assert client.get("/news").status_code == 200
    t = accesslog.timings()["news"]
    assert t["n"] == 3 and set(t["parts"]) == set(reqtiming.PARTS)
    assert all(v is not None and v >= 0 for v in t["parts"].values())
    assert t["parts"]["render"] > 0 and t["parts"]["db"] > 0 and t["slowest"]["total"] >= t["parts"]["render"]
    page = client.get("/news?tab=status").text
    assert "davon (Median)" in page and "Warten auf Bearbeitung" in page and "langsamster Aufruf" in page


def test_only_timed_pages_are_measured(client):
    client.get("/settings")
    assert accesslog.timings()["news"]["n"] == 0 and not accesslog._parts["news"]
    reqtiming.add("db", 1.0)  # außerhalb einer gemessenen Anfrage: wirkungslos


def test_nested_portfolio_calls_count_once():
    import time

    tok = reqtiming.begin()
    reqtiming.handler_started()
    with reqtiming.measure("portfolio"):
        with reqtiming.measure("portfolio"):
            time.sleep(0.01)
        time.sleep(0.01)
    reqtiming.rendered()
    parts = reqtiming.end(tok)
    assert 15 <= parts["portfolio"] < 60 and parts["cpu"] >= 0 and parts["queue"] >= 0


def test_ai_errors_counted_by_kind_with_last_text(ctx):  # noqa: F811
    answers = iter([httpx.Response(503, json={"error": {"status": "UNAVAILABLE", "message": "overloaded"}}),
                    httpx.Response(429, json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                                         "message": "quota"}}),
                    gem_ok()])
    gw = gemini_ctx(ctx, lambda req: next(answers))
    for task in ("events", "news"):
        with pytest.raises(A.AiError):
            gw.generate("s", "u", None, 100, task=task)
    kinds = {g["code"]: g for g in gw.errors_today()}
    assert set(kinds) == {"unavailable", "quota"}
    assert kinds["unavailable"]["tasks"] == {"events": 1} and kinds["unavailable"]["n"] == 1
    assert kinds["unavailable"]["label"].startswith("nicht erreichbar") and kinds["unavailable"]["last_text"]
    assert gw.status()["errors_today"] == gw.errors_today()
