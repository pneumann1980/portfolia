"""0.88.0: Spam-Tokens in der Quellenübernahme ignorieren; Quellenfilter der News mit entdeckten Videos je Kanal."""

# ruff: noqa: F811  (Fixtures aus test_bitpanda werden importiert und als Argumente verwendet)
from __future__ import annotations

import copy

from app.csvimport.service import csv_service
from app.datasources import takeover as TK
from app.news.module import source_options
from tests.test_bitpanda import api, client, ctx, master  # noqa: F401  (Fixtures)
from tests.test_review_repair import LUNC, with_lunc
from tests.test_takeover import started, sync_txs
from tests.test_youtube_transcripts import add_video


def test_spam_token_is_ignored_without_booking_and_can_be_undone(config, master, api):
    c, sid = started(config, api)
    with_lunc(api)
    p = copy.deepcopy(api.portfolio)  # Bitpanda meldet auch einen Bestand des unbekannten Tokens
    lunc = copy.deepcopy(p["data"][1])
    lunc["asset_id"] = lunc["balance"]["asset_id"] = lunc["available_balance"]["asset_id"] = LUNC
    lunc["balance"]["value"] = lunc["available_balance"]["value"] = "5"
    p["data"].append(lunc)
    api.portfolio = p
    try:
        TK.run_fetch(ctx(c), sid)
        a = TK.assess(ctx(c), sid)
        assert [x["id"] for x in a.problems if x["kind"] == "map"] == ["map:LUNC@BITPANDA"]
        assert {"lines", "observed"} <= {b.key for b in a.blockers}
        page = c.get(f"/settings/datasources/{sid}/uebernahme").text
        assert "Als Spam ignorieren" in page and 'name="action" value="spam"' in page
        r = c.post(f"/settings/datasources/{sid}/uebernahme/zuordnen",
                   data={"csrf_token": c.token, "symbol": "LUNC", "action": "spam"}, follow_redirects=False)
        assert r.status_code == 303 and "msg=" in r.headers["location"] and "Spam" in r.headers["location"]
        assert csv_service(ctx(c)).saved_symbols().get("LUNC@BITPANDA", "x") is None  # Kennung ignoriert
        assert not sync_txs(c)  # Markieren bucht nichts
        b = TK.assess(ctx(c), sid)
        assert not [x for x in b.problems if x["kind"] == "map"]
        assert {"lines", "observed"}.isdisjoint({x.key for x in b.blockers}), [(x.key, x.detail) for x in b.blockers]
        assert not any(x["name"] == "LUNC" for x in b.balances)  # weder Vorgänge noch Bestand zählen
        assert "LUNC@BITPANDA" in c.get(f"/settings/datasources/{sid}/uebernahme").text  # unter Zuordnungen sichtbar
        r = c.post(f"/settings/datasources/{sid}/uebernahme/zuordnung-entfernen",
                   data={"csrf_token": c.token, "key": "LUNC@BITPANDA"}, follow_redirects=False)
        assert "msg=" in r.headers["location"]
        assert [x["id"] for x in TK.assess(ctx(c), sid).problems if x["kind"] == "map"] == ["map:LUNC@BITPANDA"]
    finally:
        c.__exit__(None, None, None)


def test_discovered_channels_have_their_own_filter_value(client):
    ctx_ = client.app.state.ctx
    add_video(ctx_, "D1aaaaaaaaa", source="yt:discovery", channel="UCaaa", title="Erster")
    add_video(ctx_, "D2bbbbbbbbb", source="yt:discovery", channel="UCbbb", title="Zweiter")
    add_video(ctx_, "K1ccccccccc", source="yt:UCccc", channel="UCccc", title="Abo")
    ctx_.db.x("UPDATE news_item SET source_name='Entdeckt: ' || channel_id WHERE source_id='yt:discovery'")
    opts = {o["source_id"]: o["source_name"] for o in source_options(ctx_)}
    assert opts["yt:discovery:UCaaa"] == "Entdeckt: UCaaa" and opts["yt:discovery:UCbbb"] == "Entdeckt: UCbbb"
    assert "yt:discovery" not in opts and "yt:UCccc" in opts
    page = client.get("/news?tab=videos&status=all&source=yt:discovery:UCbbb").text
    assert "Zweiter D2bbbbbbbbb" in page and "Erster D1aaaaaaaaa" not in page and "Abo K1ccccccccc" not in page
    assert '<option value="yt:discovery:UCbbb" selected>' in page  # Auswahl bleibt auf dem gewählten Kanal
    page = client.get("/news?tab=videos&status=all&source=yt:UCccc").text
    assert "Abo K1ccccccccc" in page and "Zweiter D2bbbbbbbbb" not in page
