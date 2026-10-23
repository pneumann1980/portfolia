"""Konten-Hub (0.45): Datenquellen als Anbindungen eines Kontos, Konto verwalten auf der Karte, Konto-Details,
Zugänge-Seite, eine gemeinsame Depotgruppe. Synthetische Daten."""

from __future__ import annotations

import re

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


def _wallet(c, name="Ledger SOL", group="Ledger", account="Wallet SOL") -> int:
    r = post(c, "/settings/datasources", kind="wallet", provider="solana", name=name, account=account,
             address="So11111111111111111111111111111111111111112", wallet_group=group, sync_interval_min="0")
    assert r.status_code == 303, r.headers
    return int(re.search(r"#ds-(\d+)", r.headers["location"]).group(1)) if "#ds-" in r.headers["location"] else int(
        r.headers["location"].split("/settings/datasources/")[1].split("?")[0].split("#")[0])


def test_hub_is_in_navigation_and_old_address_redirects(client):
    _seed(client)
    page = client.get("/accounts")
    assert page.status_code == 200 and "Konten und Anbindungen" in page.text
    assert 'href="/accounts"' in page.text and "Konto verwalten" in page.text
    r = client.get("/settings/datasources?q=abc&msg=hi", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/accounts?q=abc&msg=hi"


def test_account_without_source_and_without_bookings_is_listed_with_tools(client):
    _seed(client)
    post(client, "/accounts/new", name="Leeres Depot", broker="X", depot_group="Depot 3")
    page = client.get("/accounts").text
    assert "Weitere Konten ohne Anbindung" in page and "Leeres Depot" in page and ">leer<" in page
    assert "Löschen" in page and "Stammdaten speichern" in page and "Umbenennen in" in page
    assert "Wallet A" in page  # Konto mit Buchungen ohne Anbindung


def test_wallet_card_has_account_tools_and_detail_page(client):
    _seed(client)
    sid = _wallet(client)
    page = client.get("/accounts").text
    assert f'id="ds-{sid}"' in page and "Konto verwalten" in page and "Wallet SOL" in page
    d = client.get("/accounts/detail?account=Wallet%20SOL")
    assert d.status_code == 200 and "Konto: Wallet SOL" in d.text and "Ledger SOL" in d.text
    assert f"/settings/datasources/{sid}" in d.text and "/belege?konto=Wallet%20SOL" in d.text
    r = client.get("/accounts/detail?account=Gibtsnicht", follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]


def test_one_group_for_accounts_and_wallets(client):
    from app.accounts.service import meta_rows

    _seed(client)
    sid = _wallet(client, group="Ledger")
    ctx = client.app.state.ctx
    page = client.get("/accounts").text
    assert "Ledger" in page  # ältere Wallet-Gruppe wird weiter angezeigt
    # Gruppe setzen: wird zur Depotgruppe des Kontos, die Wallet-Gruppe der Quelle wird abgelöst
    post(client, f"/settings/datasources/{sid}/group", group="Kalt")
    assert meta_rows(ctx.db)["Wallet SOL"]["depot_group"] == "Kalt"
    assert ctx.db.q1("SELECT wallet_group FROM data_source WHERE id=?", (sid,))["wallet_group"] is None
    assert ctx.portfolio().depot_group("Wallet SOL") == "Kalt"  # gilt auch für Positionen/Performance
    assert "Kalt" in client.get("/accounts").text
    # Umbenennen wirkt auf die Depotgruppen (und alte Wallet-Gruppen)
    r = post(client, "/settings/datasources/groups/rename", old="Kalt", new="Tresor")
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert meta_rows(ctx.db)["Wallet SOL"]["depot_group"] == "Tresor"
    assert "error=" in post(client, "/settings/datasources/groups/rename", old="Nope", new="X").headers["location"]
    # Gruppe leeren
    post(client, f"/settings/datasources/{sid}/group", group="")
    assert not meta_rows(ctx.db)["Wallet SOL"]["depot_group"]


def test_access_page_holds_keys_and_hub_has_no_key_forms(client):
    _seed(client)
    acc = client.get("/settings/zugaenge")
    assert acc.status_code == 200 and "Master-Key" in acc.text and "Anbieter-Schlüssel" in acc.text
    hub = client.get("/accounts").text
    assert "Anbieter-Schlüssel" not in hub and "/settings/zugaenge" in hub
    assert "/settings/zugaenge" in client.get("/settings").text
