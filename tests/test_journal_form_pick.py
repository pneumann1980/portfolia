"""Buchungsformular: Konto und Asset wählbar, fehlende Assets anlegen und mit erhaltenen Eingaben zurückkehren (0.30.1).

Die Liste selbst (filterbar, sichtbar auf dem Handy) ist Browser-Logik in ``app/static/js/app.js``; hier steht, was der
Server dafür liefert und beim Rücksprung aus „Neues Asset“ sicherstellt. Synthetische Daten, temporäre Datenbanken.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit

import pytest

from tests.test_journal import _asset, _client, post


@pytest.fixture
def client(config):
    with _client(config) as c:
        c.get("/settings")
        c.headers["X-CSRF-Token"] = c.cookies.get("portfolia_csrf")
        yield c


def _seed(c) -> None:
    _asset(c, "SOL", "Solana", qid="solana")
    assert post(c, "/journal/new", kind="deposit", date="2025-01-02", account="Wallet A", asset="SOL",
                qty="1", value_eur="100").status_code == 303


def test_form_marks_account_and_asset_fields_as_choices(client):
    _seed(client)
    page = client.get("/journal/new?kind=deposit").text
    assert 'data-combo="account"' in page and 'data-combo="asset"' in page and 'data-new="1"' in page
    assert '<option value="Wallet A">' in page and '<option value="SOL">Solana' in page  # Auswahl: vorhandene Werte
    assert "Neues Asset anlegen" in page  # Weg, ein fehlendes Asset anzulegen


def test_transfer_and_expert_forms_offer_the_picker_for_every_account_field(client):
    _seed(client)
    assert client.get("/journal/new?kind=transfer").text.count('data-combo="account"') == 2
    expert = client.get("/journal/new?kind=expert").text
    assert expert.count('data-combo="account"') == 2 and expert.count('data-combo="asset"') >= 4


def test_edit_form_does_not_offer_leaving_for_a_new_asset(client):
    _seed(client)
    tx_id = next(r["tx_id"] for r in client.app.state.ctx.db.q("SELECT tx_id FROM journal_tx"))
    page = client.get(f"/journal/{tx_id}/edit").text
    assert 'data-combo="asset"' in page and 'data-new="1"' not in page  # Eingaben der Bearbeitung nicht verlieren


def test_prefill_from_query_keeps_entered_values(client):
    _seed(client)
    url = "/journal/new?kind=deposit&date=2025-03-04&account=Wallet%20A&asset=sol&qty=2.5&note=Beleg%2012"
    page = client.get(url).text
    assert 'value="2025-03-04"' in page and 'value="Wallet A"' in page and 'value="2.5"' in page
    assert 'value="Beleg 12"' in page and 'value="sol"' in page


def test_creating_an_asset_returns_to_the_form_with_the_new_asset_selected(client):
    _seed(client)
    nxt = "/journal/new?kind=deposit&date=2025-03-04&account=Wallet+A&asset=neu&qty=3&pickfor=asset"
    r = post(client, "/journal/asset", asset_id="NEWTOK", name="Neues Token", asset_class="crypto",
             quote_source="none", next=nxt)
    assert r.status_code == 303
    sp = urlsplit(r.headers["location"])
    q = dict(parse_qsl(sp.query))
    assert sp.path == "/journal/new" and q["asset"] == "NEWTOK" and q["qty"] == "3" and q["account"] == "Wallet A"
    assert "pickfor" not in q  # Hilfsparameter bleibt nicht hängen
    page = client.get(r.headers["location"]).text
    assert 'value="NEWTOK"' in page and 'value="3"' in page


def test_next_target_outside_the_form_is_ignored(client):
    r = post(client, "/journal/asset", asset_id="ZZZ", name="Z", asset_class="crypto", quote_source="none",
             next="https://example.org/journal/new?pickfor=asset")
    assert r.status_code == 303 and r.headers["location"].startswith("/journal")
    assert "example.org" not in r.headers["location"]
    r = post(client, "/journal/asset", asset_id="ZZY", name="Z2", asset_class="crypto", quote_source="none",
             next="/journal/new?kind=buy&pickfor=account")
    assert dict(parse_qsl(urlsplit(r.headers["location"]).query)).get("account") == "ZZY"  # Feld aus der Liste
