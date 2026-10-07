"""Wallet-Übersicht und Gruppen: Überschneidungen ohne Doppelzählung, Werte ohne irreführende 0,00 €, Zustände,
Suche/Sortierung, Gruppen umbenennen/zuordnen, mehrere Konten aktualisieren (Fehler isoliert), Dubletten-Erkennung
über Datenquellen hinweg (synthetische Daten, ohne Netz)."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from app.csvimport.service import csv_service
from app.datasources import chainhttp as CH
from app.datasources import overview as OV
from app.datasources.service import datasource_service
from app.datasources.wallet import WalletConnector
from app.prices.models import Quote
from tests.test_wallets_bitcoin import ACC, ZPUB, R
from tests.test_wallets_bitcoin import history as btc_history
from tests.wallet_fakes import (
    ETHERSCAN_KEY,
    MASTER,
    FakeEsplora,
    FakeEvm,
    all_rows,
    create_wallet,
    ctx,
    load,
    make_client,
    post,
    set_provider_key,
    source,
)

D = Decimal
A = "0x1111111111111111111111111111111111111111"
B = "0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


@pytest.fixture
def client(config, monkeypatch):
    monkeypatch.setenv("PORTFOLIA_MASTER_KEY", MASTER)
    with make_client(config) as c:
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        from app.journal.service import journal_service
        js = journal_service(ctx(c))
        for aid, qid in (("BTC", "bitcoin"), ("ETH", "ethereum"), ("BNB", "binancecoin")):
            if aid not in js.known_assets():
                js.save_asset({"asset_id": aid, "name": aid, "asset_class": "crypto", "quote_source": "coingecko",
                               "quote_id": qid})
        yield c


@pytest.fixture
def chains(monkeypatch):
    evm = FakeEvm({1: load("evm_eth.json"), 56: load("evm_eth.json")}, free_chains=(1,))
    esplora = FakeEsplora(btc_history(), tip=800_500)

    def route(req: httpx.Request) -> httpx.Response:
        return (esplora.handler if req.url.host in ("mempool.space", "blockstream.info") else evm.handler)(req)

    monkeypatch.setattr(WalletConnector, "transport", httpx.MockTransport(route))
    monkeypatch.setattr(WalletConnector, "sleep", staticmethod(lambda s: None))
    monkeypatch.setattr(CH, "_LIMITERS", {})
    return evm, esplora


def price(c, aid: str, eur: float) -> None:
    from app.journal.service import journal_asset_infos
    series = ctx(c).prices.series_for(journal_asset_infos(ctx(c).db)[aid])
    ctx(c).store.upsert_quotes([Quote(series, eur, "EUR", datetime.now(UTC), "coingecko")])


def balance(c, sid: int, key: str, qty: str) -> None:
    ctx(c).db.x("INSERT OR REPLACE INTO ds_balance(source_id, asset_key, qty, name, note, observed_at) VALUES "
                "(?,?,?,?,?,?)", (sid, key, qty, None, None, "2026-10-01T10:00:00Z"))


# ----------------------------------------------------------------------------------------------------
# Überschneidungen
# ----------------------------------------------------------------------------------------------------

def test_single_address_covered_by_xpub_is_rejected_both_ways(client, chains):
    create_wallet(client, "bitcoin", ZPUB, name="Ledger BTC")
    r = post(client, "/settings/datasources", kind="wallet", provider="bitcoin", name="Einzeladresse",
             address=R[3], account="X", sync_interval_min="0")
    assert r.status_code == 400 and "Kontoschlüssel" in r.text and "Ledger BTC" in r.text
    ctx(client).db.x("DELETE FROM data_source")
    create_wallet(client, "bitcoin", R[2], name="Alte Einzeladresse")
    r = post(client, "/settings/datasources", kind="wallet", provider="bitcoin", name="Ledger BTC", address=ZPUB,
             account="X", sync_interval_min="0")
    assert r.status_code == 400 and "Einzeladresse" in r.text
    # andere Chain mit derselben Kennung ist kein Konflikt
    create_wallet(client, "ethereum", A, name="ETH")
    create_wallet(client, "bsc", A, name="BNB")


def test_existing_overlap_is_shown_and_counted_once(client, chains):
    svc = datasource_service(ctx(client))
    x = create_wallet(client, "bitcoin", ZPUB, name="Ledger BTC", group="Ledger")
    # Altbestand (vor der Prüfung angelegt): Einzeladresse aus dem Kontoschlüssel – direkt in der Datenbank
    old = ACC.addr(0, 1)
    ctx(client).db.x("INSERT INTO data_source(kind, provider, name, account, address, wallet_group, watch_json, "
                     "created_at, updated_at) VALUES ('wallet','bitcoin','Alt','Alt',?, 'Ledger', ?, "
                     "'2030-01-01T00:00:00Z','2030-01-01T00:00:00Z')", (old, json.dumps({"addresses": [old]})))
    y = int(ctx(client).db.scalar("SELECT id FROM data_source WHERE name='Alt'"))
    for sid in (x, y):
        balance(client, sid, "BTC", "0.5")
    price(client, "BTC", 60000.0)
    wallets = [d for d in svc.list() if d.is_wallet]
    ov = OV.overlaps(wallets)
    assert ov[x][0]["counted"] is True and ov[y][0]["counted"] is False
    vals = OV.account_values(ctx(client), wallets)
    s = OV.group_summary(wallets, vals, ov)
    assert s["value"] == D("30000.00") and s["skipped"] == 1  # nicht 60.000 €
    page = client.get("/settings/datasources").text
    assert "überschneidet sich mit" in page and "nicht doppelt gezählt" in page


# ----------------------------------------------------------------------------------------------------
# Werte, Zustände, Hinweise
# ----------------------------------------------------------------------------------------------------

def test_values_unknown_partial_and_last_known(client, chains):
    set_provider_key(client, "etherscan", ETHERSCAN_KEY)
    a = create_wallet(client, "ethereum", A, name="Ledger ETH", group="Ledger")
    b = create_wallet(client, "ethereum", B, name="MetaMask ETH", group="MetaMask")
    price(client, "ETH", 2000.0)
    balance(client, a, "ETH", "0.5")
    balance(client, a, f"USDC@ETH:{USDC}", "10")  # Token ohne Zuordnung → unvollständig
    svc = datasource_service(ctx(client))
    vals = OV.account_values(ctx(client), [svc.get(a), svc.get(b)])
    assert vals[a].state == "partial" and vals[a].value == D("1000.00") and "nicht zugeordnet" in vals[a].missing[0]
    assert vals[b].state == "unknown" and vals[b].value is None
    page = client.get("/settings/datasources").text
    assert "mind." in page and "1.000,00" in page and "unbekannt" in page
    assert "0,00 €" not in page  # nie eine irreführende Null für unbekannte Werte
    # fehlgeschlagener Abruf: letzter bekannter Wert bleibt, als solcher gekennzeichnet
    ctx(client).db.x("UPDATE data_source SET status='error', last_error='Anbieter nicht erreichbar' WHERE id=?", (a,))
    page = client.get("/settings/datasources").text
    assert "letzter bekannter Wert" in page and "Technischer Fehler" in page and "1.000,00" in page
    assert ctx(client).db.scalar("SELECT COUNT(*) FROM ds_balance WHERE source_id=?", (a,)) == 2


def test_sync_states_and_data_notes_are_separate(client, chains):
    set_provider_key(client, "etherscan", ETHERSCAN_KEY)
    a = create_wallet(client, "ethereum", A, name="Ledger ETH")
    svc = datasource_service(ctx(client))
    assert OV.sync_state(svc.get(a)) == "never"
    res = svc.sync(a, "manual")
    assert res["status"] == "synced"
    ds = svc.get(a)
    assert OV.sync_state(ds) == "ok"
    notes = OV.data_notes(ds, None, svc.open_counts(a), svc.holdings(ds), None)
    kinds = {n["kind"] for n in notes}
    assert "unclear" in kinds or "new" in kinds  # Prüfpunkte sind Datenhinweise, nicht Abruf-Fehler
    page = client.get("/settings/datasources").text
    assert "erfolgreich" in page and "Datenhinweise" in page


# ----------------------------------------------------------------------------------------------------
# Suche, Sortierung, Gruppen
# ----------------------------------------------------------------------------------------------------

def test_search_sort_and_groups(client, chains):
    set_provider_key(client, "etherscan", ETHERSCAN_KEY)
    a = create_wallet(client, "ethereum", A, name="Zeta ETH", group="Ledger")
    b = create_wallet(client, "bsc", A, name="Alpha BNB", group="Ledger")
    c = create_wallet(client, "bitcoin", R[0], name="Mitte BTC", group="")
    price(client, "ETH", 2000.0)
    balance(client, a, "ETH", "1")
    def cards(q: str) -> set[int]:
        page = client.get(f"/settings/datasources?q={q}").text
        return {sid for sid in (a, b, c) if f'id="ds-{sid}"' in page}

    assert cards("bnb") == {b}  # Netzwerk
    assert cards(R[0][:12]) == {c}  # Adresse
    assert cards("ledger") == {a, b}  # Gruppenname
    assert cards("zeta") == {a}  # Kontoname
    page = client.get("/settings/datasources?sort=name").text
    assert page.index(f'id="ds-{b}"') < page.index(f'id="ds-{a}"')  # Alpha vor Zeta
    page = client.get("/settings/datasources?sort=value").text
    assert page.index(f'id="ds-{a}"') < page.index(f'id="ds-{b}"')  # Wert absteigend, unbekannt zuletzt
    page = client.get("/settings/datasources?sort=name&desc=1").text
    assert page.index(f'id="ds-{a}"') < page.index(f'id="ds-{b}"')
    # Gruppe umbenennen: nur die Zuordnung ändert sich
    r = post(client, "/settings/datasources/groups/rename", old="Ledger", new="Ledger Nano")
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert {source(client, a)["wallet_group"], source(client, b)["wallet_group"]} == {"Ledger Nano"}
    assert source(client, a)["provider"] == "ethereum" and source(client, b)["provider"] == "bsc"
    # bestehendes Konto einer Gruppe zuordnen; Kontoname unabhängig
    post(client, f"/settings/datasources/{c}/group", group="Ledger Nano")
    assert source(client, c)["wallet_group"] == "Ledger Nano" and source(client, c)["name"] == "Mitte BTC"
    page = client.get("/settings/datasources").text
    assert "Ledger Nano" in page and "Gruppe aktualisieren" in page and "Alle aktualisieren" in page
    assert "chain-ethereum" in page and "chain-bsc" in page and "data-copy" in page
    assert f"https://etherscan.io/address/{A}" in page and f"https://bscscan.com/address/{A}" in page


# ----------------------------------------------------------------------------------------------------
# Mehrere Konten aktualisieren
# ----------------------------------------------------------------------------------------------------

def wait_batch(svc) -> dict:
    for _ in range(400):
        b = svc.batch_progress()
        if not b.get("running"):
            return b
        time.sleep(0.05)
    raise AssertionError("Aktualisierung endet nicht")


def test_refresh_all_isolates_failures(client, chains):
    set_provider_key(client, "etherscan", ETHERSCAN_KEY)
    eth = create_wallet(client, "ethereum", A, name="Ledger ETH", group="Ledger")
    bnb = create_wallet(client, "bsc", A, name="Ledger BNB", group="Ledger")  # Etherscan-Free ohne BNB → Fehler
    btc = create_wallet(client, "bitcoin", ZPUB, name="Ledger BTC", group="Ledger")
    r = post(client, "/settings/datasources/sync-many", scope="all")
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    svc = datasource_service(ctx(client))
    b = wait_batch(svc)
    assert b["done"] == 3 and len(b["errors"]) == 1 and "Ledger BNB" in b["errors"][0]
    assert source(client, eth)["status"] == "synced" and source(client, btc)["status"] == "synced"
    assert source(client, bnb)["status"] == "error"
    assert all_rows(client, eth) and all_rows(client, btc)
    # Gruppe aktualisieren: nur Konten der Gruppe; fehlgeschlagenes Konto verliert nichts
    rows_eth = len(all_rows(client, eth))
    post(client, "/settings/datasources/sync-many", scope="group", group="Ledger")
    b = wait_batch(svc)
    assert b["total"] == 3 and len(all_rows(client, eth)) == rows_eth


# ----------------------------------------------------------------------------------------------------
# Neu angelegte Datenquelle für dieselbe Wallet → mögliche Dubletten statt Doppelbuchung
# ----------------------------------------------------------------------------------------------------

def test_recreated_source_flags_same_hash_as_duplicate(client, chains):
    svc = datasource_service(ctx(client))
    w1 = create_wallet(client, "bitcoin", ZPUB, name="Ledger BTC")
    res = svc.sync(w1, "manual")
    out = csv_service(ctx(client)).commit(res["batch_id"])
    assert out.get("created"), out
    svc.delete(w1)  # Buchungen bleiben
    w2 = create_wallet(client, "bitcoin", ZPUB, name="Ledger BTC neu")  # neue Kennung des Kontos
    svc.sync(w2, "manual")
    rows = all_rows(client, w2)
    # gleiche Blockchain-Transaktion und gleiche Buchungsseite → Entscheidung statt stiller Doppelbuchung; nicht
    # übernommene Zeilen (hier Gebühr ohne EUR-Wert) bleiben unvollständig – nichts wird neu gebucht
    assert rows and not any(v.status == "new" for v in rows.values()), {k: v.status for k, v in rows.items()}
    dups = [v for v in rows.values() if v.status == "duplicate"]
    assert len(dups) >= 4 and all("gleiche Blockchain-Transaktion" in v.warnings[0] for v in dups)


def test_transfer_between_own_wallets_of_one_chain_is_not_a_duplicate(client, chains):
    """Abgang in Wallet A und Zugang in Wallet B derselben Transaktion sind zwei Seiten eines Transfers – weder
    „bekannt“ noch Dublette (früher erzeugte die Hash-Kennung eine scheinbare UUID und damit einen Fehltreffer)."""
    svc = datasource_service(ctx(client))
    for one, two in ((R[2], R[3]), (R[0], R[1])):  # A→B in Transaktion 5; A und B geben gemeinsam aus (Tx 3)
        ctx(client).db.x("DELETE FROM data_source")
        a = create_wallet(client, "bitcoin", one, name=f"Wallet A {one[-4:]}")
        res = svc.sync(a, "manual")
        csv_service(ctx(client)).commit(res["batch_id"])
        b = create_wallet(client, "bitcoin", two, name=f"Wallet B {two[-4:]}")
        svc.sync(b, "manual")
        rows = all_rows(client, b)
        assert rows and not any(v.status in ("known", "duplicate") for v in rows.values()), \
            {k: (v.status, v.warnings[:1]) for k, v in rows.items()}


def test_hash_shaped_event_ids_get_no_uuid_alias():
    from app.csvimport.events import identity_keys
    h = "0" * 30 + "1" * 34
    assert identity_keys(f"bitcoin:{h}:abcd1234") == {f"bitcoin:{h}:abcd1234"}
    assert identity_keys(f"ethereum:0x{h}:0xabc") == {f"ethereum:0x{h}:0xabc"}
    u = "0f0e0d0c-0b0a-4a09-8807-060504030201"
    assert f"bitpanda:{u}" in identity_keys(f"bitpanda:{u.replace('-', '')}")
