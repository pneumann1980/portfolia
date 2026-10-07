"""Polkadot über Subscan (PubFi-Gateway bzw. direkt) – synthetische Daten, ohne Netz.

Abgedeckt: SS58-Prüfung und Umrechnung (Präfix 42 → 0, Kusama abgelehnt), Schlüsselpflicht mit klarer Anzeige,
Relay Chain und Asset Hub getrennt, Gebühr nur aus eigenen Extrinsics (``fee_used``), Staking ohne Bestandsbewegung
(nur Gebühr), XCM zur Prüfung, Migrationsereignis als ungeklärt (nicht gebucht), Asset-Hub-Token mit eigener
Kennung, Plausibilisierung ``amount`` ↔ ``amount_v2``, Rewards als Staking-Ertrag, Paginierung, wiederholter Abruf
ohne Doppelungen, direkter Subscan-Zugang mit ``X-API-Key``.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from app.datasources import chainhttp as CH
from app.datasources.chains import polkadot as PD
from app.datasources.chains.codec import ss58_encode
from app.datasources.providers import PROVIDERS, normalize_address
from app.datasources.service import datasource_service
from app.datasources.wallet import WalletConnector
from tests.wallet_fakes import (
    MASTER,
    FakeSubscan,
    all_rows,
    balances,
    create_wallet,
    ctx,
    make_client,
    set_provider_key,
    source,
)

D = Decimal
ME, EXT, EXCH, MIG = (ss58_encode(bytes([b]) * 32, 0) for b in (0x55, 0x66, 0x77, 0x88))
KEY = "pubfi_test_key_0123456789abcdef"
T0 = 1_760_000_000


def H(n: int) -> str:
    return "0x" + f"{n:064x}"


def tr(block: int, idx: str, ev: int, frm: str, to: str, amount: str, v2: str | None = None, sym: str = "DOT",
       uid: str = "DOT", module: str = "balances", h: int | None = None) -> dict:
    return {"block_num": block, "block_timestamp": T0 + block, "extrinsic_index": idx, "event_idx": ev, "from": frm,
            "to": to, "amount": amount, "amount_v2": v2 if v2 is not None else "", "asset_symbol": sym,
            "asset_unique_id": uid, "module": module, "success": True, "hash": H(h or block), "fee": "0"}


def ex(block: int, idx: str, call: str, fee: str, fee_used: str, success: bool = True) -> dict:
    mod, _, fn = call.partition(".")
    return {"block_num": block, "block_timestamp": T0 + block, "extrinsic_index": idx, "extrinsic_hash": H(block),
            "call_module": mod, "call_module_function": fn, "fee": fee, "fee_used": fee_used, "success": success,
            "signer": ME}


def nets() -> dict:
    return {
        "polkadot": {"tip": 1000,
                     "transfers": [tr(100, "100-2", 5, EXT, ME, "50", "500000000000"),
                                   tr(110, "110-1", 3, ME, EXCH, "10", "100000000000"),
                                   tr(140, "140-1", 2, ME, EXT, "5", "50000000000", module="xcmpallet")],
                     "extrinsics": [ex(110, "110-1", "balances.transfer_keep_alive", "160000000", "150000000"),
                                    ex(120, "120-1", "staking.bond", "200000000", "200000000"),
                                    ex(140, "140-1", "xcmPallet.limited_teleport_assets", "300000000", "300000000")],
                     "rewards": [{"block_num": 130, "block_timestamp": T0 + 130, "event_index": "130-7",
                                  "amount": "12345678900", "account": ME, "era": 1000, "event_id": "Rewarded"}],
                     "tokens": [{"symbol": "DOT", "unique_id": "DOT", "decimals": 10, "balance": "0"}]},
        "assethub-polkadot": {"tip": 2000,
                              "transfers": [tr(500, "500-0", 1, MIG, ME, "34.98", module="ahmigrator"),
                                            tr(510, "510-2", 4, EXT, ME, "25.5", sym="USDT",
                                               uid="standard_assets/1984", module="assets"),
                                            tr(520, "520-1", 2, EXT, ME, "1", "20000000000")],
                              "extrinsics": [], "rewards": [],
                              "tokens": [{"symbol": "DOT", "unique_id": "DOT", "decimals": 10,
                                          "balance": "350000000000", "bonded": "100000000000"},
                                         {"symbol": "USDT", "unique_id": "standard_assets/1984", "decimals": 6,
                                          "balance": "25500000"}]},
    }


@pytest.fixture
def scan(monkeypatch):
    fake = FakeSubscan(nets(), KEY)
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


def k(net: str, idx: str, sub: str) -> str:
    return f"polkadot:{net}-{idx}:{ME}#{sub}"


def test_ss58_validation():
    p = PROVIDERS["polkadot"]
    assert normalize_address(p, ME) == (ME, None)
    generic = ss58_encode(b"\x55" * 32, 42)
    assert normalize_address(p, generic) == (ME, None)  # dasselbe Konto im Polkadot-Format
    kusama = ss58_encode(b"\x55" * 32, 2)
    assert "anderen Netzes" in normalize_address(p, kusama)[1]
    assert "Prüfsumme" in normalize_address(p, ME[:-1] + ("a" if ME[-1] != "a" else "b"))[1]


def test_missing_key_is_shown_and_nothing_is_fetched(client, scan):
    sid = create_wallet(client, "polkadot", ME, name="Ledger DOT")
    res = sync(client, sid)
    assert "Schlüssel" in res["error"] and not scan.calls
    page = client.get("/settings/datasources").text
    assert "Schlüssel fehlt" in page and "PubFi" in page


def test_relay_and_asset_hub_events(client, scan):
    set_provider_key(client, "pubfi", KEY)
    sid = create_wallet(client, "polkadot", ME, name="Ledger DOT")
    res = sync(client, sid)
    assert res["status"] == "synced", res
    rows = all_rows(client, sid)
    r = rows[k("rc", "100-2", "tr:5")].rec
    assert (r.kind, r.in_sym, r.in_qty, r.review) == ("deposit", "DOT", D(50), None)
    r = rows[k("rc", "110-1", "tr:3")].rec
    assert (r.kind, r.out_qty, r.fee_qty, r.fee_sym, r.review) == ("withdrawal", D(10), D("0.015"), "DOT", None)
    assert r.txhash == H(110)
    r = rows[k("rc", "120-1", "fee")].rec  # staking.bond: gebunden bleibt im Konto → nur Gebühr
    assert r.kind == "fee" and r.fee_qty == D("0.02") and r.label == "staking.bond"
    r = rows[k("rc", "140-1", "tr:2")].rec
    assert r.kind == "withdrawal" and "XCM" in r.review and r.fee_qty == D("0.03")
    rw = rows[f"polkadot:reward-rc-130-7:{ME}#reward"].rec
    assert (rw.kind, rw.in_qty, rw.tag) == ("deposit", D("1.23456789"), "staking")
    mig = rows[k("ah", "500-0", "migration")]
    assert mig.rec.kind == "review" and mig.status == "unclear" and "Migration" in mig.rec.note
    usdt = rows[k("ah", "510-2", "tr:4")].rec
    assert usdt.in_sym == "USDT@DOTAH:standard_assets/1984" and usdt.in_qty == D("25.5")
    odd = rows[k("ah", "520-1", "tr:2")].rec
    assert odd.in_qty == D(1) and "nicht eindeutig" in odd.review
    b = balances(client, sid)
    assert b["DOT"] == "35" and b["USDT@DOTAH:standard_assets/1984"] == "25.5"
    note = ctx(client).db.scalar("SELECT note FROM ds_balance WHERE source_id=? AND asset_key='DOT'", (sid,))
    assert "gebunden 10" in note
    cur = json.loads(source(client, sid)["cursor_json"])
    assert cur["nets"]["polkadot"]["block"] == 1001 and cur["nets"]["assethub-polkadot"]["block"] == 2001
    # Gateway: nur Free-Routen, Bearer, keine Query-Parameter (siehe FakeSubscan)
    assert all(c.url.path.endswith(":free") for c in scan.calls)
    n = len(all_rows(client, sid))
    datasource_service(ctx(client)).reset_cursor(sid)
    sync(client, sid)
    assert len(all_rows(client, sid)) == n


def test_pagination_capped_lists_continue_next_run(client, scan, monkeypatch):
    monkeypatch.setattr(PD, "ROW", 1)
    monkeypatch.setattr(PD, "MAX_PAGES", 2)
    set_provider_key(client, "pubfi", KEY)
    sid = create_wallet(client, "polkadot", ME, name="Ledger DOT")
    res = sync(client, sid)
    assert res["status"] == "partial", res
    for _ in range(6):
        if datasource_service(ctx(client)).sync(sid, "manual").get("status") == "synced":
            break
    rows = all_rows(client, sid)
    for key in (k("rc", "100-2", "tr:5"), k("rc", "110-1", "tr:3"), k("rc", "140-1", "tr:2"),
                k("ah", "510-2", "tr:4"), f"polkadot:reward-rc-130-7:{ME}#reward"):
        assert key in rows, key
    assert len(rows) == len(set(rows))


def test_xcm_to_own_account_on_other_chain_is_not_netted_to_zero(client, scan):
    """Relay Chain → Asset Hub an dieselbe Adresse: Abgang auf der signierenden Chain, Zugang auf der Ziel-Chain –
    beide zur Prüfung (Umbuchung), nie stillschweigend zu null saldiert."""
    scan.nets["polkadot"]["transfers"].append(tr(150, "150-1", 4, ME, ME, "7", "70000000000", module="xcmpallet"))
    scan.nets["polkadot"]["extrinsics"].append(ex(150, "150-1", "xcmPallet.limited_teleport_assets", "1", "1"))
    scan.nets["assethub-polkadot"]["transfers"].append(tr(530, "530-0", 9, ME, ME, "6.99", module="polkadotxcm"))
    set_provider_key(client, "pubfi", KEY)
    sid = create_wallet(client, "polkadot", ME, name="Ledger DOT")
    sync(client, sid)
    rows = all_rows(client, sid)
    out = rows[k("rc", "150-1", "tr:4")].rec
    inn = rows[k("ah", "530-0", "tr:9")].rec
    assert (out.kind, out.out_qty) == ("withdrawal", D(7)) and "XCM" in out.review
    assert (inn.kind, inn.in_qty) == ("deposit", D("6.99")) and "XCM" in inn.review


def test_direct_subscan_with_api_key_header(client, scan):
    set_provider_key(client, "subscan", KEY)
    sid = create_wallet(client, "polkadot", ME, name="Ledger DOT", chain_provider="subscan")
    assert sync(client, sid)["status"] == "synced"
    hosts = {c.url.host for c in scan.calls}
    assert hosts == {"polkadot.api.subscan.io", "assethub-polkadot.api.subscan.io"}
    assert all(c.headers.get("x-api-key") == KEY and "authorization" not in c.headers for c in scan.calls)


def test_provider_error_keeps_cursor(client, scan):
    set_provider_key(client, "pubfi", KEY)
    sid = create_wallet(client, "polkadot", ME, name="Ledger DOT")
    sync(client, sid)
    cursor = source(client, sid)["cursor_json"]
    scan.fail_next(httpx.Response(200, json={"code": 10001, "message": "API rate limit exceeded"}))
    res = sync(client, sid)
    assert "drosselt" in res["error"] and source(client, sid)["cursor_json"] == cursor
