"""Ausbuchen von Beständen (Totalverlust, Diebstahl) und Ersatzkurse für Positionen ohne Kursquelle."""

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.journal.writeoff import row_key, writeoff_service
from app.main import build_app
from app.util.timeutil import today_local


@pytest.fixture
def client(config):
    with TestClient(build_app(config, start_scheduler=False)) as c:
        c.get("/settings")
        c.headers["X-CSRF-Token"] = c.cookies.get("portfolia_csrf")
        yield c


def post(c, url, data):
    return c.post(url, data=data, follow_redirects=False)


def _setup(c):
    """Wallet mit EUR, einem gekauften Token ohne Kursquelle (DEAD) und einem Airdrop ohne Wert (JUNK)."""
    for aid, name in (("DEAD", "Dead Token"), ("JUNK", "Junk Airdrop")):
        r = post(c, "/journal/asset", {"asset_id": aid, "name": name, "asset_class": "crypto",
                                       "quote_source": "none", "quote_id": ""})
        assert r.status_code == 303, r.text
    d = (today_local() - timedelta(days=5)).isoformat()
    for data in ({"kind": "deposit", "date": d, "account": "Wallet", "asset": "EUR", "qty": "500"},
                 {"kind": "buy", "date": d, "account": "Wallet", "asset": "DEAD", "qty": "1000", "amount": "100"},
                 {"kind": "income", "date": d, "account": "Wallet", "asset": "JUNK", "qty": "5000", "tag": "airdrop",
                  "value_eur": "0"}):
        r = post(c, "/journal/new", data)
        assert r.status_code == 303, r.text
    return c.app.state.ctx


def test_writeoff_page_lists_unvalued_first_and_books_total_loss(client):
    ctx = _setup(client)
    val = ctx.valuation()
    pos = val.by_id()
    # gekaufter Token ohne Kursquelle: Transaktionskurs (frisch), Airdrop ohne Wert: unbewertet
    assert pos["DEAD"].price.kind == "tx" and pos["DEAD"].value == pytest.approx(100.0)
    assert [p.asset_id for p in val.unvalued] == ["JUNK"]
    assert "als Verlust ausbuchen" in client.get("/").text

    page = client.get("/journal/writeoff").text
    assert "Junk Airdrop" in page and "Dead Token" not in page  # Standard: nur ohne gültigen Kurs
    assert "Dead Token" in client.get("/journal/writeoff?show=all").text

    r = post(client, "/journal/writeoff", {"sel": [row_key("Wallet", "DEAD"), row_key("Wallet", "JUNK")],
                                           "date": today_local().isoformat(), "tag": "lost",
                                           "note": "Ausbuchung (Totalverlust)"})
    assert r.status_code == 303 and "done=2" in r.headers["location"]
    led = ctx.ledger()
    assert ("Wallet", "DEAD") not in {k for k, q in led.balances.items() if q}
    assert ("Wallet", "JUNK") not in {k for k, q in led.balances.items() if q}
    val = ctx.valuation()
    assert not val.unvalued
    assert val.realized == pytest.approx(-100.0)  # Einstand des Tokens als realisierter Verlust
    rows = ctx.db.q("SELECT * FROM journal_tx WHERE tag='lost' AND status='active' ORDER BY id")
    assert len(rows) == 2 and {r["value_eur"] for r in rows} == {"0"}
    marks = {json.loads(r["form_json"])["writeoff"] for r in rows}
    assert len(marks) == 1  # eine Sammel-Ausbuchung
    assert "gesammelt" in client.get("/journal/writeoff").text


def test_writeoff_rejects_date_before_last_booking_and_can_be_undone(client):
    ctx = _setup(client)
    key = row_key("Wallet", "JUNK")
    too_early = (today_local() - timedelta(days=6)).isoformat()
    r = post(client, "/journal/writeoff", {"sel": key, "date": too_early, "tag": "lost", "note": ""})
    assert r.status_code == 400 and "vor der letzten Buchung" in r.text
    r = post(client, "/journal/writeoff", {"sel": key, "date": today_local().isoformat(), "tag": "stolen",
                                           "note": "Wallet kompromittiert"})
    assert r.status_code == 303
    assert not any(p.asset_id == "JUNK" for p in ctx.valuation().positions)
    recent = writeoff_service(ctx).recent()
    assert [x["tag"] for x in recent] == ["Diebstahl"]
    r = post(client, "/journal/writeoff/undo", {"tx": [recent[0]["tx_id"]]})
    assert r.status_code == 303 and "undone=1" in r.headers["location"]
    assert any(p.asset_id == "JUNK" for p in ctx.valuation().positions)
    # unbekannte Art und leere Auswahl werden abgewiesen
    assert post(client, "/journal/writeoff", {"sel": key, "date": today_local().isoformat(),
                                              "tag": "sell"}).status_code == 400
    assert post(client, "/journal/writeoff", {"date": today_local().isoformat(), "tag": "lost"}).status_code == 400


def test_detail_and_performance_link_to_writeoff(client):
    ctx = _setup(client)
    assert "/journal/writeoff?asset=JUNK" in client.get("/asset/JUNK").text
    page = client.get("/journal/writeoff?asset=JUNK").text
    assert f'value="{row_key("Wallet", "JUNK")}" checked' in page  # aus der Detailansicht vorausgewählt
    ctx.recompute_history(persist=False)
    perf = client.get("/performance").text
    assert "ohne gültigen Kurs" in perf and "Junk Airdrop" in perf
