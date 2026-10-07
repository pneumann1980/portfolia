"""Polygon PoS – Wiederverwendung des EVM-Adapters (synthetische Daten, ohne Netz).

Abgedeckt: native Bewegungen und Gebühren, Spiegelung nativer Überweisungen als Token-Transfer des Systemvertrags
0x…1010 (nur einmal gezählt), MATIC bis zum Hardfork-Block und POL danach, Umstellungsvorschlag MATIC → POL (einmal,
Menge aus der Historie, auch über Etappen hinweg), Tokens mit gleichem Symbol und verschiedenen Verträgen, dieselbe
Adresse auf Ethereum und Polygon als getrennte Konten, Blockscout ohne Key (Blockhöhe, ``status`` 2 als Lücke),
wiederholter Abruf ohne zusätzliche Buchungen.
"""

from __future__ import annotations

import copy
from decimal import Decimal

import httpx
import pytest

from app.datasources import chainhttp as CH
from app.datasources.service import datasource_service
from app.datasources.wallet import WalletConnector
from tests.wallet_fakes import (
    ETHERSCAN_KEY,
    MASTER,
    FakeBlockscout,
    FakeEvm,
    all_rows,
    balances,
    create_wallet,
    ctx,
    make_client,
    post,
    set_provider_key,
    source,
)

D = Decimal
A = "0x1111111111111111111111111111111111111111"
EXT = "0x2222222222222222222222222222222222222222"
USDC = "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"
FAKE = "0x9999999999999999999999999999999999999999"
MRC20 = "0x0000000000000000000000000000000000001010"
T0 = 1_650_000_000
WEI = 10**18


def H(n: int) -> str:
    return "0x" + f"{n:064x}"


def ntx(n: int, block: int, frm: str, to: str, value: int, gas_used: int = 21_000, gas_price: int = 30 * 10**9) -> dict:
    return {"hash": H(n), "blockNumber": str(block), "timeStamp": str(T0 + n * 1000), "from": frm, "to": to,
            "value": str(value), "gasUsed": str(gas_used), "gasPrice": str(gas_price), "isError": "0",
            "txreceipt_status": "1", "input": "0x", "methodId": "0x", "functionName": ""}


def ttx(n: int, block: int, contract: str, frm: str, to: str, value: int, sym: str, dec: int, name: str = "") -> dict:
    return {"hash": H(n), "blockNumber": str(block), "timeStamp": str(T0 + n * 1000), "contractAddress": contract,
            "from": frm, "to": to, "value": str(value), "tokenSymbol": sym, "tokenName": name or sym,
            "tokenDecimal": str(dec)}


def data(tip: int = 70_000_000) -> dict:
    return {
        "tip": tip, "balance": str(75 * WEI - 630_000 * 10**9),
        "tokenbalance": {USDC: "10000000"},
        "txlist": [ntx(1, 50_000_000, EXT, A, 100 * WEI), ntx(2, 60_000_000, A, EXT, 30 * WEI),
                   ntx(3, 63_000_000, EXT, A, 5 * WEI)],
        "txlistinternal": [],
        "tokentx": [ttx(1, 50_000_000, MRC20, EXT, A, 100 * WEI, "MATIC", 18, "Matic Token"),
                    ttx(2, 60_000_000, MRC20, A, EXT, 30 * WEI, "MATIC", 18, "Matic Token"),
                    ttx(3, 63_000_000, MRC20, EXT, A, 5 * WEI, "POL", 18, "POL (ex-MATIC)"),
                    ttx(4, 64_000_000, USDC, EXT, A, 10_000_000, "USDC", 6, "USD Coin"),
                    ttx(4, 64_000_000, FAKE, EXT, A, 1_000_000, "USDC", 6, "USDC visit claim-usdc.xyz")],
    }


@pytest.fixture
def poly(monkeypatch):
    fake = FakeEvm({1: data(), 137: data()}, free_chains=(1, 137))
    monkeypatch.setattr(WalletConnector, "transport", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(WalletConnector, "sleep", staticmethod(lambda s: None))
    monkeypatch.setattr(CH, "_LIMITERS", {})
    return fake


@pytest.fixture
def client(config, monkeypatch):
    monkeypatch.setenv("PORTFOLIA_MASTER_KEY", MASTER)
    with make_client(config) as c:
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        yield c


def sync(c, sid):
    return datasource_service(ctx(c)).sync(sid, "manual")


def k(n: int, sub: str) -> str:
    return f"polygon:{H(n)}:{A}#{sub}"


def test_native_matic_pol_switch_mirror_and_same_symbol_tokens(client, poly):
    set_provider_key(client, "etherscan", ETHERSCAN_KEY)
    sid = create_wallet(client, "polygon", A, name="Ledger POL")
    res = sync(client, sid)
    assert res["status"] == "synced", res
    rows = all_rows(client, sid)
    r = rows[k(1, "n:in")].rec
    assert (r.kind, r.in_sym, r.in_qty, r.review) == ("deposit", "MATIC", D(100), None)
    r = rows[k(2, "n:out")].rec
    assert (r.kind, r.out_sym, r.out_qty, r.fee_sym, r.fee_qty) == ("withdrawal", "MATIC", D(30), "MATIC",
                                                                    D("0.00063"))
    r = rows[k(3, "n:in")].rec
    assert (r.in_sym, r.in_qty) == ("POL", D(5))  # nach dem Hardfork-Block: POL
    # Spiegelung über 0x…1010 wird nicht zusätzlich gebucht
    assert not [x for x in rows if MRC20 in x or "MATIC@" in x or "POL@" in x]
    assert not any((v.rec.in_sym or "").startswith(("MATIC@", "POL@")) for v in rows.values())
    # Umstellung genau einmal, Menge aus der Historie (100 − 30 − Gebühr), nie automatisch
    conv = rows[f"polygon:switch-matic-pol:{A}#switch"].rec
    assert (conv.kind, conv.out_sym, conv.out_qty, conv.in_sym, conv.in_qty) == \
        ("conversion", "MATIC", D("69.99937"), "POL", D("69.99937"))
    assert conv.review and "Historie" in conv.review and conv.raw["from_block_0"] is True
    # gleiches Symbol, verschiedene Verträge → verschiedene Assets; Werbe-Token zur Prüfung
    usdc = rows[next(x for x in rows if x.startswith(f"polygon:{H(4)}:") and rows[x].rec.in_sym.endswith(USDC))]
    fake = rows[next(x for x in rows if x.startswith(f"polygon:{H(4)}:") and rows[x].rec.in_sym.endswith(FAKE))]
    assert usdc.rec.in_sym == f"USDC@POLYGON:{USDC}" and usdc.rec.in_qty == D(10) and not usdc.rec.review
    assert fake.rec.in_sym == f"USDC@POLYGON:{FAKE}" and "Spam" in fake.rec.review
    assert balances(client, sid)["POL"] == "74.99937"
    skipped = ctx(client).db.q1("SELECT detail_json FROM data_source_run WHERE source_id=? ORDER BY id DESC",
                                (sid,))["detail_json"]
    assert "Systemvertrags" in skipped
    # wiederholter Abruf: keine zusätzlichen Zeilen, keine zweite Umstellung
    n = len(all_rows(client, sid))
    datasource_service(ctx(client)).reset_cursor(sid)
    sync(client, sid)
    assert len(all_rows(client, sid)) == n


def test_switch_amount_carried_across_runs(client, poly):
    """Erster Lauf endet vor dem Hardfork-Block (Kettenspitze davor): der Bestand wird im Fortsetzungspunkt
    mitgeführt; die Umstellung folgt im späteren Lauf mit derselben Menge."""
    poly.chains[137] = data(tip=61_000_000)
    set_provider_key(client, "etherscan", ETHERSCAN_KEY)
    sid = create_wallet(client, "polygon", A, name="Ledger POL")
    sync(client, sid)
    rows = all_rows(client, sid)
    assert not [x for x in rows if "switch" in x]
    import json
    cur = json.loads(source(client, sid)["cursor_json"])
    assert cur["switch"]["pre"] == "69.99937" and not cur["switch"].get("done")
    poly.chains[137] = data()
    sync(client, sid)
    rows = all_rows(client, sid)
    conv = rows[f"polygon:switch-matic-pol:{A}#switch"].rec
    assert conv.out_qty == D("69.99937")
    assert json.loads(source(client, sid)["cursor_json"])["switch"]["done"] is True


def test_same_address_separate_chains_but_not_twice_on_one_chain(client, poly):
    set_provider_key(client, "etherscan", ETHERSCAN_KEY)
    eth = create_wallet(client, "ethereum", A, name="Ledger ETH")
    pol = create_wallet(client, "polygon", A, name="Ledger POL")
    assert eth != pol
    r = post(client, "/settings/datasources", kind="wallet", provider="polygon", name="Doppelt", address=A,
             account="Doppelt", sync_interval_min="0")
    assert r.status_code == 400 and "bereits als „Ledger POL“ angelegt" in r.text
    sync(client, eth)
    sync(client, pol)
    eth_rows, pol_rows = all_rows(client, eth), all_rows(client, pol)
    assert all(x.startswith("ethereum:") for x in eth_rows) and all(x.startswith("polygon:") for x in pol_rows)
    assert any(v.rec.in_sym == "ETH" for v in eth_rows.values())
    assert not any(v.rec.in_sym == "ETH" for v in pol_rows.values())
    assert any(v.rec.in_sym == f"USDC@ETH:{USDC}" for v in eth_rows.values())  # Chain im Token-Schlüssel


def test_blockscout_without_key_marks_unprocessed_internal_txs_as_gap(client, monkeypatch):
    fake = FakeBlockscout(copy.deepcopy(data()), internal_partial=True)
    monkeypatch.setattr(WalletConnector, "transport", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(WalletConnector, "sleep", staticmethod(lambda s: None))
    monkeypatch.setattr(CH, "_LIMITERS", {})
    sid = create_wallet(client, "polygon", A, name="Polygon ohne Key", chain_provider="blockscout_polygon")
    res = sync(client, sid)
    assert res["status"] == "partial", res
    cov = source(client, sid)
    assert "noch nicht vollständig verarbeitet" in cov["coverage_json"]
    rows = all_rows(client, sid)
    assert rows[k(1, "n:in")].rec.in_qty == D(100)
    # Blockhöhe über block/eth_block_number (proxy-Modul kennt Blockscout nicht)
    assert any(c.url.params.get("action") == "eth_block_number" for c in fake.calls)


def test_polygon_without_etherscan_key_shows_missing_key(client, poly):
    sid = create_wallet(client, "polygon", A, name="Ledger POL")
    res = sync(client, sid)
    assert "Schlüssel" in res["error"]
    page = client.get("/settings/datasources").text
    assert "Schlüssel fehlt" in page and "Ledger POL" in page
    assert source(client, sid)["status"] == "error"
