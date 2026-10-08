"""Portfolio-UX: Top-Bewegungen (% | €), Treemap (Farbe, „Sonstige“), Positionsdetail, Schnellkauf/-verkauf über die
normale Buchungserfassung, Watchlist (Einträge, Reihenfolge, Marktdaten über die gemeinsame Ablage, Position erstellen).

Demo-Modus: Kurse aus dem Demo-Anbieter, keine Netzabrufe.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import build_app
from app.util.timeutil import today_local

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "beispiel-import.zip"


@pytest.fixture
def client(config):
    dst = config.import_dir / "beispiel.zip"
    shutil.copy(SAMPLE, dst)
    old = time.time() - 3600
    os.utime(dst, (old, old))
    with TestClient(build_app(config, start_scheduler=False)) as c:
        c.token = c.get("/settings") and c.cookies.get("portfolia_csrf")
        r = c.post("/actions/import/check", headers={"X-CSRF-Token": c.token})
        assert r.status_code == 200
        from app.jobs import tasks

        ctx = c.app.state.ctx
        tasks.refresh_prices(ctx, force=True)
        tasks.backfill(ctx)
        yield c


def post(c, url, data=None, **kw):
    return c.post(url, data={**(data or {}), "csrf_token": c.token}, follow_redirects=False, **kw)


def ctx(c):
    return c.app.state.ctx


# -- Top-Bewegungen, Treemap, Detail ------------------------------------------------------------------------

def test_top_movers_percent_and_absolute(client):
    html = client.get("/").text
    assert 'data-movers-toggle' in html and 'data-movers="pct"' in html and 'data-movers="eur" hidden' in html
    val = ctx(client).valuation()
    movers = [p for p in val.positions if p.day_change is not None and not p.asset.is_fiat and p.value > 0]
    by_eur = sorted([p for p in movers if p.day_change > 0], key=lambda p: -p.day_change)
    eur_block = html.split('data-movers="eur"', 1)[1].split("</section>", 1)[0]
    if by_eur:  # absolute Sortierung: größte Wertänderung zuerst, beide Werte sichtbar
        first = eur_block.split('class="title">', 1)[1].split("<", 1)[0]
        assert first == by_eur[0].asset.name
        assert "%" in eur_block and "€" in eur_block


def test_treemap_color_modes_and_small_positions(client):
    day = client.get("/api/treemap").json()
    assert day["metric"] == "day" and day["data"]
    total = client.get("/api/treemap?color=total").json()
    assert total["metric"] == "total" and total["scale"] == 50
    c = ctx(client)
    c.settings.set("allocation.other_threshold_pct", 30.0)
    grouped = client.get("/api/treemap").json()
    others = [n for g in grouped["data"] for n in g["children"] if n.get("other")]
    assert others and all(n["members"] for n in others)
    full = client.get("/api/treemap?expand=all").json()
    assert not [n for g in full["data"] for n in g["children"] if n.get("other")]
    html = client.get("/").text
    assert 'data-mode="treemap"' in html and "data-tm-color" in html


def test_asset_detail_ranges_and_quick_buttons(client):
    html = client.get("/asset/BTC").text
    for r in ("1T", "7T", "1M", "3M", "1J", "MAX"):
        assert f'data-range="{r}"' in html
    assert "/journal/quick?asset=BTC&amp;side=buy" in html and "/journal/quick?asset=BTC&amp;side=sell" in html
    assert "Investiert" in html
    assert client.get("/api/asset/BTC/chart?range=7T").status_code == 200
    pos = client.get("/positions").text
    assert "qt-buy" in pos and "qt-sell" in pos


# -- Schnellkauf/-verkauf -----------------------------------------------------------------------------------

def test_quick_buy_uses_market_price_and_normal_journal(client):
    r = client.get("/journal/quick?asset=BTC&side=buy", headers={"HX-Request": "true"})
    assert r.status_code == 200 and 'name="qty"' in r.text and "Börse X" in r.text  # Konto mit Bestand vorbelegt
    d = (today_local() - timedelta(days=3)).isoformat()
    r = post(client, "/journal/quick", {"asset": "BTC", "kind": "buy", "qty": "0,01", "date": d, "time": "10:00",
                                        "account": "Börse X"})
    assert r.status_code == 303, r.text
    row = ctx(client).db.q1("SELECT * FROM journal_tx WHERE status='active' ORDER BY id DESC LIMIT 1")
    assert row["type"] == "buy" and row["to_asset"] == "BTC" and row["source"] == "manual"
    assert "Schlusskurs" in (row["value_source"] or "") or "Kurs" in (row["value_source"] or "")
    close = ctx(client).store.close_on_or_before(ctx(client).prices.series_for(ctx(client).portfolio().asset("BTC")),
                                                 today_local() - timedelta(days=3))
    assert float(row["value_eur"]) == pytest.approx(round(close["close"] * 0.01, 2), abs=0.01)
    form = json.loads(row["form_json"])
    assert form["price_source"] == "market"


def test_quick_sell_same_validation_as_manual_entry(client):
    r = post(client, "/journal/quick", {"asset": "BTC", "kind": "sell", "qty": "", "date": today_local().isoformat(),
                                        "account": "Börse X"}, headers={"HX-Request": "true"})
    assert r.status_code == 400 and "Stückzahl" in r.text
    r = post(client, "/journal/quick", {"asset": "BTC", "kind": "sell", "qty": "0.001", "price": "50000",
                                        "date": today_local().isoformat(), "account": "Börse X", "fee": "1,5"})
    assert r.status_code == 303
    row = ctx(client).db.q1("SELECT * FROM journal_tx WHERE status='active' ORDER BY id DESC LIMIT 1")
    assert row["type"] == "sell" and float(row["value_eur"]) == 50.0 and float(row["fee_qty"]) == 1.5


# -- Watchlist ----------------------------------------------------------------------------------------------

def test_watchlist_add_order_remove_and_market_data(client):
    page = client.get("/watchlist")
    assert page.status_code == 200 and "Noch keine Einträge" in page.text
    assert post(client, "/watchlist/add", {"kind": "crypto", "value": "https://www.coingecko.com/de/munze/kaspa"}
                ).status_code == 303
    assert post(client, "/watchlist/add", {"kind": "security", "value": "sap.de"}).status_code == 303
    assert post(client, "/watchlist/add", {"kind": "asset", "value": "SOL"}).status_code == 303
    r = post(client, "/watchlist/add", {"kind": "crypto", "value": "kaspa"})
    assert r.status_code == 400 and "Bereits in der Watchlist" in r.text
    r = post(client, "/watchlist/add", {"kind": "security", "value": "../etc"})
    assert r.status_code == 400 and "Ungültiges Yahoo-Symbol" in r.text
    items = ctx(client).db.q("SELECT id, quote_source, quote_id, asset_id FROM watchlist_item ORDER BY position")
    assert [(r["quote_source"], r["quote_id"]) for r in items] == [("coingecko", "kaspa"), ("yahoo", "SAP.DE"),
                                                                   ("coingecko", "solana")]
    assert items[0]["asset_id"] == "KAS" and items[2]["asset_id"] == "SOL"  # Bezug zum Portfolio-Asset
    html = client.get("/watchlist").text
    assert html.count("<polyline") == 3  # Sparkline je Eintrag (Demo-Historie)
    assert "im Portfolio" in html
    snap_rows = re.findall(r'id="wl-(\d+)"', html)
    assert len(snap_rows) == 3
    post(client, f"/watchlist/item/{items[2]['id']}/move", {"dir": "up"})
    order = [r["quote_id"] for r in ctx(client).db.q("SELECT quote_id FROM watchlist_item ORDER BY position")]
    assert order == ["kaspa", "solana", "SAP.DE"]
    sorted_html = client.get("/watchlist?sort=name").text
    assert "↑" not in sorted_html  # Verschieben nur in eigener Reihenfolge
    post(client, f"/watchlist/item/{items[1]['id']}/remove")
    assert ctx(client).db.scalar("SELECT COUNT(*) FROM watchlist_item") == 2


def test_watchlist_detail_chart_and_create_position(client):
    post(client, "/watchlist/add", {"kind": "crypto", "value": "dogecoin"})
    it = ctx(client).db.q1("SELECT id FROM watchlist_item WHERE quote_id='dogecoin'")
    r = client.get(f"/watchlist/item/{it['id']}")
    assert r.status_code == 200 and "Position erstellen" in r.text
    ch = client.get(f"/api/watchlist/{it['id']}/chart?range=1M").json()
    assert ch["points"] and ch["source"].endswith("cg:dogecoin")
    r = post(client, f"/watchlist/item/{it['id']}/position")
    assert r.status_code == 303 and r.headers["location"].startswith("/journal/quick?asset=")
    aid = ctx(client).db.scalar("SELECT asset_id FROM watchlist_item WHERE id=?", (it["id"],))
    asset = ctx(client).db.q1("SELECT * FROM journal_asset WHERE asset_id=?", (aid,))
    assert asset is not None and asset["quote_source"] == "coingecko" and asset["quote_id"] == "dogecoin"
    # keine Buchung ohne ausdrückliches Speichern
    assert not ctx(client).db.scalar("SELECT COUNT(*) FROM journal_tx WHERE to_asset=?", (aid,))


def test_watchlist_quotes_bundled_with_regular_price_update(client):
    """Watchlist-Coins laufen im gebündelten CoinGecko-Abruf mit (kein Zusatzaufruf)."""
    post(client, "/watchlist/add", {"kind": "crypto", "value": "dogecoin"})
    assert ctx(client).prices.watch_ids("coingecko") == ["dogecoin"]
    from app.jobs import tasks

    tasks.refresh_prices(ctx(client), force=True, which="crypto")
    assert ctx(client).store.latest("demo:cg:dogecoin") is not None
