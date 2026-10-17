"""Kontenverwaltung (0.31): anlegen, Stammdaten, umbenennen (über Zusammenführen), leeres Konto löschen.

Synthetische Daten, temporäre Datenbanken.
"""

from __future__ import annotations

import io
import json
import zipfile

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


def _rows(c) -> dict:
    from app.accounts.service import rows

    return {r.name: r for r in rows(c.app.state.ctx)}


def test_overview_lists_accounts_with_bookings_and_actions(client):
    _seed(client)
    page = client.get("/accounts").text
    assert "Wallet A" in page and "1 Buchung" in page and "1 Position" in page
    assert "/accounts/merge?alias=Wallet%20A" in page and "Entfernen" in page  # vorhandene Wege bleiben erreichbar
    r = _rows(client)["Wallet A"]
    assert (r.n_txs, r.positions, r.empty, r.deletable) == (1, 1, False, False)


def test_create_account_makes_it_available_everywhere_and_rejects_duplicates(client):
    _seed(client)
    r = post(client, "/accounts/new", name="  Scalable   Capital ", broker="Scalable", depot_group="Depot 2",
             note="Sparplan")
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    rows = _rows(client)
    assert "Scalable Capital" in rows  # Leerraum bereinigt
    new = rows["Scalable Capital"]
    assert (new.broker, new.depot_group, new.note, new.empty, new.deletable) == ("Scalable", "Depot 2", "Sparplan",
                                                                                 True, True)
    page = client.get("/journal/new?kind=deposit").text
    assert '<option value="Scalable Capital">' in page  # im Buchungsformular wählbar
    for dup in ("Scalable Capital", "scalable capital", "Wallet A"):
        r = post(client, "/accounts/new", name=dup)
        assert "err=" in r.headers["location"] and "gibt es schon" in client.get(r.headers["location"]).text
    for bad in ("", "   ", "x" * 81, "a\x00b"):
        assert "err=" in post(client, "/accounts/new", name=bad).headers["location"]


def test_metadata_overlays_the_import_and_survives_a_rename_of_nothing(client):
    _seed(client)
    r = post(client, "/accounts/meta", account="Wallet A", broker="Ledger", depot_group="Krypto", note="Cold")
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    pf = client.app.state.ctx.portfolio()
    assert pf.accounts["Wallet A"].broker == "Ledger" and pf.depot_group("Wallet A") == "Krypto"
    assert _rows(client)["Wallet A"].note == "Cold"
    assert "err=" in post(client, "/accounts/meta", account="Gibt es nicht", broker="x").headers["location"]
    # leer lassen = Angabe entfernen
    post(client, "/accounts/meta", account="Wallet A", broker="", depot_group="", note="")
    assert client.app.state.ctx.portfolio().accounts["Wallet A"].broker is None


def test_rename_creates_the_new_name_and_goes_through_the_merge_preview(client):
    _seed(client)
    r = post(client, "/accounts/rename", account="Wallet A", new_name="Ledger Nano")
    assert r.status_code == 200 and "Ledger Nano" in r.text and "Wallet A" in r.text  # Vorschau der Zusammenführung
    assert "Ledger Nano" in _rows(client)  # als leeres Konto angelegt – Vorschau ohne Bestätigung ändert nichts
    ctx = client.app.state.ctx
    assert {t.to_account for t in ctx.portfolio().txs} == {"Wallet A"}
    # bestätigen: die Vorschau trägt Prüfsumme und Ziel
    from app.assetchange import account_merge as AM

    p = AM.preview(ctx, "Wallet A", "Ledger Nano")
    assert p.ok
    mid, errors = AM.apply(ctx, "Wallet A", "Ledger Nano", AM.fingerprint(p), "")
    assert mid is not None and not errors
    assert {t.to_account for t in ctx.portfolio().txs} == {"Ledger Nano"}  # Buchung läuft unter dem neuen Namen
    assert "Wallet A" in _rows(client)["Ledger Nano"].merged_from
    # Rückgängig: wieder der alte Name
    assert not AM.revert(ctx, mid)
    assert {t.to_account for t in ctx.portfolio().txs} == {"Wallet A"}


def test_rename_rejects_taken_names_and_unknown_accounts(client):
    _seed(client)
    post(client, "/accounts/new", name="Zweitkonto")
    for old, new in (("Wallet A", "Zweitkonto"), ("Wallet A", "wallet a"), ("Wallet A", ""), ("Nope", "Neu")):
        r = post(client, "/accounts/rename", account=old, new_name=new)
        assert r.status_code == 303 and "err=" in r.headers["location"], (old, new)


def test_delete_only_empty_own_accounts_without_data_source(client):
    _seed(client)
    post(client, "/accounts/new", name="Leer")
    r = post(client, "/accounts/delete", account="Wallet A")
    assert "err=" in r.headers["location"] and "Buchung" in client.get(r.headers["location"]).text
    r = post(client, "/accounts/delete", account="Leer")
    assert "msg=" in r.headers["location"] and "Leer" not in _rows(client)
    assert "err=" in post(client, "/accounts/delete", account="Leer").headers["location"]  # schon weg


def test_account_meta_survives_the_full_export_and_restore(client):
    _seed(client)
    post(client, "/accounts/new", name="Vorab", broker="Bank", depot_group="G1", note="n")
    post(client, "/accounts/meta", account="Wallet A", broker="Ledger", depot_group="Krypto", note="")
    body = client.get("/journal/export.zip").content
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        state = json.loads(z.read("portfolia/state.json"))
    meta = {m["account"]: m for m in state["account_meta"]}
    assert meta["Vorab"]["broker"] == "Bank" and meta["Wallet A"]["depot_group"] == "Krypto"
    from app.fullexport import _insert

    ctx = client.app.state.ctx
    ctx.db.x("DELETE FROM account_meta")
    with ctx.db.transaction() as c:
        assert _insert(c, "account_meta", state["account_meta"], "OR IGNORE") == 2
    assert {r["account"] for r in ctx.db.q("SELECT account FROM account_meta")} == {"Vorab", "Wallet A"}
