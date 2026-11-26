"""0.122.1 – Ereignis-Grenzfälle: jedes Paar höchstens einmal an die KI, nach einem Fehler eine Stunde Pause."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.news import events as EV
from app.news.events import EventService
from app.util.timeutil import iso
from tests.test_news_intel import FakeGateway, add_item, ctx  # noqa: F401


def _pair(c):
    c.settings.set("news.ai_articles", "all")  # Grenzfälle auch zwischen noch nicht analysierten Meldungen
    a = add_item(c, "Fed erhöht Leitzins um 25 Basispunkte auf Rekordhoch", "https://a.com/d", hours=2)
    b = add_item(c, "Fed erhöht Leitzins um 50 Basispunkte auf Rekordhoch", "https://b.com/e", hours=2)
    return a, b


def test_grey_pair_asked_once_across_many_runs(ctx):  # noqa: F811
    _pair(ctx)
    gw = FakeGateway({"paare": [{"id": 0, "gleich": False}]})
    for _ in range(10):  # wie zehn Läufe im 20-Minuten-Takt
        EventService(ctx, gw).run()
    assert len(gw.calls) == 1
    assert ctx.db.scalar("SELECT COUNT(*) FROM news_event_verdict WHERE same=0") == 1


def test_failure_pauses_one_hour_then_retries(ctx):  # noqa: F811
    _pair(ctx)
    bad = FakeGateway("kein json")
    EventService(ctx, bad).run()
    EventService(ctx, bad).run()
    assert len(bad.calls) == 1 and ctx.db.get_state(EV.PAUSE_KEY)  # zweiter Lauf: Pause, keine Anfrage
    assert ctx.db.scalar("SELECT COUNT(*) FROM news_event_verdict") == 0  # nichts gemerkt
    ctx.db.set_state(EV.PAUSE_KEY, iso(datetime.now(UTC) - timedelta(minutes=1)))
    good = FakeGateway({"paare": [{"id": 0, "gleich": True}]})
    EventService(ctx, good).run()
    assert len(good.calls) == 1


def test_verdicts_disappear_with_the_items(ctx):  # noqa: F811
    a, _b = _pair(ctx)
    EventService(ctx, FakeGateway({"paare": [{"id": 0, "gleich": False}]})).run()
    ctx.db.x("DELETE FROM news_item WHERE id=?", (a,))
    assert ctx.db.scalar("SELECT COUNT(*) FROM news_event_verdict") == 0
