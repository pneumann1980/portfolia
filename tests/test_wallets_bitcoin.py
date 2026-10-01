"""Bitcoin-Wallets (Esplora) – synthetische Fixtures, ohne Netz.

Abgedeckt: Kontoschlüssel (zpub nach BIP84-Testvektor) mit Empfangs- und Wechselgeldadressen bis zum Gap-Limit,
mehrere Eingänge, Wechselgeld, Gebühren, Konsolidierung, fremde Eingänge (CoinJoin/PayJoin), unbestätigte bzw.
zu junge Transaktionen, Einzeladressen mit sichtbarer Abdeckungsgrenze, Paginierung (25 je Seite), inkrementelle
Läufe, Drosselung, Abbruch mit Fortsetzung, Bestandsabgleich, Ablehnung privater Schlüssel.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from app.datasources import chainhttp as CH
from app.datasources.chains import bitcoin as BT
from app.datasources.chains.btckeys import Account
from app.datasources.service import datasource_service
from app.datasources.wallet import WalletConnector
from tests.wallet_fakes import MASTER, FakeEsplora, all_rows, balances, create_wallet, ctx, make_client, post, source

D = Decimal
ZPUB = ("zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868RvUUkgDKf31mGDtKsAYz2oz2AGut"
        "ZYs")  # öffentlicher BIP84-Testvektor (Mnemonic „abandon … about“), keine echten Mittel
ACC = Account(ZPUB)
R = [ACC.addr(0, i) for i in range(6)]
C = [ACC.addr(1, i) for i in range(3)]
EXT = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"  # BIP173-Beispieladresse (fremd)
EXT2 = "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2"
T0 = 1772445600


def txid(n: int) -> str:
    return f"{n:064x}"


def tx(n: int, height: int, vin: list[tuple[str, int]], vout: list[tuple[str, int]], fee: int,
       coinbase: bool = False) -> dict:
    return {"txid": txid(n), "version": 2, "locktime": 0, "fee": fee,
            "status": {"confirmed": True, "block_height": height, "block_hash": "00" * 32,
                       "block_time": T0 + (height - 800_000) * 600},
            "vin": [{"txid": txid(10_000 + n), "vout": i, "is_coinbase": coinbase,
                     "prevout": {"scriptpubkey_address": a, "value": v}} for i, (a, v) in enumerate(vin)],
            "vout": [{"scriptpubkey_address": a, "value": v} for a, v in vout]}


def history() -> list[dict]:
    return [
        tx(1, 800_000, [(EXT, 2_000_000)], [(R[0], 1_000_000), (EXT, 990_000)], 10_000),        # Eingang 0,01
        tx(2, 800_100, [(EXT2, 600_000)], [(R[1], 500_000), (EXT2, 95_000)], 5_000),             # Eingang 0,005
        tx(3, 800_200, [(R[0], 1_000_000), (R[1], 500_000)], [(EXT, 1_200_000), (C[0], 290_000)], 10_000),
        tx(4, 800_300, [(C[0], 290_000)], [(R[2], 285_000)], 5_000),                             # Konsolidierung
        tx(5, 800_400, [(R[2], 285_000), (EXT2, 1_000_000)], [(R[3], 280_000), (EXT2, 1_000_000)], 5_000),
    ]


@pytest.fixture
def esplora(monkeypatch):
    fake = FakeEsplora(history(), tip=800_500)
    sleeps: list[float] = []
    monkeypatch.setattr(WalletConnector, "transport", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(WalletConnector, "sleep", staticmethod(sleeps.append))
    monkeypatch.setattr(CH, "_LIMITERS", {})
    fake.sleeps = sleeps  # type: ignore[attr-defined]
    return fake


@pytest.fixture
def client(config, monkeypatch):
    monkeypatch.setenv("PORTFOLIA_MASTER_KEY", MASTER)
    with make_client(config) as c:
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        from app.journal.service import journal_service
        journal_service(ctx(c)).save_asset({"asset_id": "BTC", "name": "Bitcoin", "asset_class": "crypto",
                                            "quote_source": "none"})
        yield c


def sync(c, sid):
    return datasource_service(ctx(c)).sync(sid, "manual")


def by_tx(rows: dict, n: int) -> list:
    return [v for k, v in rows.items() if f":{txid(n)}:" in k]


def test_xpub_account_books_change_fees_and_multiple_inputs(client, esplora):
    sid = create_wallet(client, "bitcoin", ZPUB, name="Ledger BTC", script="p2wpkh")
    res = sync(client, sid)
    assert res["status"] == "synced", res
    rows = all_rows(client, sid)
    (dep,) = by_tx(rows, 1)
    assert (dep.rec.kind, dep.rec.in_qty, dep.rec.review) == ("deposit", D("0.01"), None)
    (wd,) = by_tx(rows, 3)  # zwei eigene Eingänge, Wechselgeld an eigene Adresse
    assert (wd.rec.kind, wd.rec.out_qty, wd.rec.fee_qty, wd.rec.fee_sym) == ("withdrawal", D("0.012"), D("0.0001"),
                                                                            "BTC")
    assert wd.rec.review is None and len(wd.rec.raw["own_addresses"]) == 3
    (cons,) = by_tx(rows, 4)
    assert cons.rec.kind == "fee" and cons.rec.fee_qty == D("0.00005") and "Konsolidierung" in cons.rec.note
    (mixed,) = by_tx(rows, 5)
    assert mixed.rec.kind == "withdrawal" and mixed.rec.out_qty == D("0.00005") and "fremden Eingängen" in \
        mixed.rec.review and mixed.rec.fee_qty is None
    ds = datasource_service(ctx(client)).get(sid)
    owner = ds.watch.watch_id
    assert dep.rec.event_key == f"bitcoin:{txid(1)}:{owner}" and dep.rec.ext_id.endswith("#in")
    assert balances(client, sid) == {"BTC": "0.0028"}
    cov = json.loads(source(client, sid)["coverage_json"])
    assert cov["derive"]["0"] == {"used": 3, "scanned": 24} and cov["derive"]["1"] == {"used": 0, "scanned": 21}
    assert any("Kontoschlüssel" in lim and "20 ungenutzte" in lim for lim in cov["limits"])
    assert ds.sync_state == ("vollständig synchronisiert", "good")
    # nur öffentliche Abfragen an den Esplora-Host
    assert {r.url.host for r in esplora.calls} == {"mempool.space"}


def test_young_and_unconfirmed_transactions_wait(client, esplora):
    sid = create_wallet(client, "bitcoin", ZPUB, name="Ledger BTC", script="p2wpkh")
    esplora.txs.append(tx(6, 800_499, [(EXT, 500_000)], [(R[4], 400_000), (EXT, 95_000)], 5_000))  # 2 Bestätigungen
    esplora.mempool.append(tx(7, 0, [(EXT, 300_000)], [(R[4], 200_000)], 1_000))
    res = sync(client, sid)
    assert not by_tx(all_rows(client, sid), 6)
    cov = json.loads(source(client, sid)["coverage_json"])
    assert cov["pending"] == 2 and res["status"] == "synced"
    esplora.tip = 800_510
    sync(client, sid)
    (late,) = by_tx(all_rows(client, sid), 6)
    assert late.rec.in_qty == D("0.004")


def test_single_addresses_do_not_claim_full_wallet(client, esplora):
    sid = create_wallet(client, "bitcoin", f"{R[0]}\n{R[1]}", name="Ledger BTC (Adressen)")
    res = sync(client, sid)
    assert res["status"] == "synced"
    (wd,) = by_tx(all_rows(client, sid), 3)
    # Wechselgeld an eine nicht eingetragene Adresse zählt als Abgang – die Abdeckungsgrenze steht am Konto
    assert wd.rec.out_qty == D("0.0149")
    ds = datasource_service(ctx(client)).get(sid)
    assert any("Einzeladressen" in lim for lim in ds.limits)
    page = client.get("/settings/datasources").text
    assert "Einzeladressen decken die Wallet nicht vollständig ab" in page


def test_pagination_and_incremental_runs(client, esplora):
    big = [tx(100 + i, 700_000 + i, [(EXT, 200_000)], [(R[0], 100_000 + i), (EXT, 95_000)], 5_000)
           for i in range(60)]
    esplora.txs = big
    sid = create_wallet(client, "bitcoin", R[0], name="Alt-Adresse")
    sync(client, sid)
    assert len(all_rows(client, sid)) == 60
    chain_calls = [r.url.path for r in esplora.calls if "/txs/chain" in r.url.path]
    assert len(chain_calls) == 3 and chain_calls[1].endswith(txid(100 + 35))
    esplora.calls.clear()
    esplora.txs.append(tx(500, 700_100, [(EXT, 200_000)], [(R[0], 150_000)], 5_000))
    esplora.tip = 800_000
    sync(client, sid)
    chain_calls = [r.url.path for r in esplora.calls if "/txs/chain" in r.url.path]
    assert chain_calls == [f"/api/address/{R[0]}/txs/chain"]  # nur bis zur bekannten Transaktion
    assert len(all_rows(client, sid)) == 61
    esplora.calls.clear()
    sync(client, sid)  # nichts Neues: keine Historienabfrage
    assert not [r for r in esplora.calls if "/txs/chain" in r.url.path]


def test_rate_limit_and_interrupted_import_resume(client, esplora, monkeypatch):
    esplora.txs = [tx(200 + i, 700_000 + i, [(EXT, 200_000)], [(R[i % 3], 100_000 + i)], 5_000) for i in range(90)]
    sid = create_wallet(client, "bitcoin", f"{R[0]}\n{R[1]}\n{R[2]}", name="Drei Adressen")
    esplora.fail_next(httpx.Response(429, headers={"Retry-After": "3"}), lambda r: "/txs/chain" in r.url.path)
    monkeypatch.setattr(BT.BitcoinConnector, "max_requests", 7)
    res = sync(client, sid)
    assert 3.0 in esplora.sleeps
    st = source(client, sid)
    assert st["status"] == "partial" and json.loads(st["coverage_json"])["resume"]
    first = set(all_rows(client, sid))
    assert 0 < len(first) < 90
    monkeypatch.setattr(BT.BitcoinConnector, "max_requests", 2500)
    while datasource_service(ctx(client)).get(sid).backfill_pending:
        sync(client, sid)
    rows = all_rows(client, sid)
    assert len(rows) == 90 and first <= set(rows)
    assert res["batch_id"]


def test_check_suggests_script_type_and_rejects_private_keys(client, esplora):
    sid = create_wallet(client, "bitcoin", ZPUB, name="Ledger BTC", script="p2pkh")  # falscher Typ gewählt
    ok, _msg = datasource_service(ctx(client)).check(sid)
    det = json.loads(source(client, sid)["last_check_json"])["details"]["xpub"]
    assert det["ok"] is False and "Native SegWit" in det["text"] and "anpassen" in det["text"]
    xprv = ("xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiChkVvvNKmPGJxWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGB"
            "xrMPHi")
    r = post(client, "/settings/datasources", kind="wallet", provider="bitcoin", name="X", address=xprv,
             sync_interval_min="0")
    assert r.status_code == 400 and "privaten Schlüssel" in r.text and xprv not in r.text
    assert ok in (True, False)
