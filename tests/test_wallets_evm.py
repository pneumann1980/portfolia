"""EVM-Wallets (Ethereum, BNB Chain, Avalanche C-Chain) – mit anonymisierten Fixtures, ohne Netz.

Abgedeckt: Einordnung (Ein-/Ausgang, Gebühr, Freigabe, Swap, fehlgeschlagen, Erstattung saldiert, Spam,
Address-Poisoning), mehrere Token-Logs in einem Hash, gleiches Symbol mit anderem Contract, dieselbe Adresse auf
drei Chains, Bestätigungen, Paginierung (volle Seiten, voller Block), Drosselung (Hülle, 429, 5xx, Tageslimit),
abgebrochener Erstabruf mit Fortsetzung, wiederholte Läufe, Bestandsabgleich, Anbieter-Schlüssel, SSRF-Schutz.
"""

from __future__ import annotations

import copy
from decimal import Decimal

import httpx
import pytest

from app.csvimport.service import csv_service
from app.datasources import chainhttp as CH
from app.datasources import connector as K
from app.datasources.chains import evm as E
from app.datasources.service import datasource_service
from app.datasources.wallet import WalletConnector
from tests.wallet_fakes import (
    ETHERSCAN_KEY,
    MASTER,
    FakeEvm,
    all_rows,
    balances,
    create_wallet,
    ctx,
    load,
    make_client,
    post,
    rows_by_ext,
    set_provider_key,
    source,
)

D = Decimal
A = "0x1111111111111111111111111111111111111111"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
FAKE = "0x9999999999999999999999999999999999999999"


def H(n: int) -> str:
    return "0x" + f"{n:064x}"


def key(n: int, sub: str, chain: str = "ethereum") -> str:
    return f"{chain}:{H(n)}:{A}#{sub}"


@pytest.fixture
def evm(monkeypatch):
    eth = load("evm_eth.json")
    fake = FakeEvm({1: eth, 56: copy.deepcopy(eth), 43114: copy.deepcopy(eth)})
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
        yield c


def sync(c, sid):
    return datasource_service(ctx(c)).sync(sid, "manual")


def assets(c, *ids: str) -> None:
    from app.journal.service import journal_service

    js = journal_service(ctx(c))
    for aid in ids:
        if aid not in js.known_assets():
            r = js.save_asset({"asset_id": aid, "name": aid, "asset_class": "crypto", "quote_source": "none"})
            assert not r.errors, r.errors


def eth_wallet(c, **form) -> int:
    set_provider_key(c, "etherscan", ETHERSCAN_KEY)
    assets(c, "ETH", "BNB", "AVAX")
    return create_wallet(c, "ethereum", A, name="Ledger ETH", **form)


# ----------------------------------------------------------------------------------------------------
# Einordnung
# ----------------------------------------------------------------------------------------------------

def test_check_shows_balance_history_and_nft_gap(client, evm):
    sid = eth_wallet(client)
    r = post(client, f"/settings/datasources/{sid}/check")
    assert r.status_code == 303 and "msg=" in r.headers["location"], r.headers["location"]
    chk = source(client, sid)
    assert chk["status"] == "connected"
    assert balances(client, sid) == {"ETH": "0.53982"}
    evm.chains[1]["tokennfttx"] = [{"blockNumber": "1", "from": A, "to": A}]
    datasource_service(ctx(client)).check(sid)
    import json
    details = json.loads(source(client, sid)["last_check_json"])["details"]
    assert details["nft"]["ok"] is False and "NFT" in details["nft"]["text"]
    # Schlüssel nur an Etherscan, nur als dokumentierter Parameter
    for req in evm.calls:
        assert req.url.host == "api.etherscan.io" and req.url.params["apikey"] == ETHERSCAN_KEY


def test_initial_sync_classifies_every_movement(client, evm):
    sid = eth_wallet(client)
    res = sync(client, sid)
    assert res["status"] == "synced", res
    bid = res["batch_id"]
    rows = rows_by_ext(client, bid)
    r = rows[key(1, "n:in")]
    assert (r.rec.kind, r.rec.in_sym, r.rec.in_qty, r.rec.review) == ("deposit", "ETH", D("1.5"), None)
    r = rows[key(2, "n:out")]
    assert (r.rec.kind, r.rec.out_qty, r.rec.fee_qty, r.rec.fee_sym) == ("withdrawal", D("0.2"), D("0.00063"), "ETH")
    r = rows[key(3, "fee")]
    assert r.rec.kind == "fee" and r.rec.fee_qty == D("0.00092") and "Freigabe" in r.rec.note
    assert r.rec.label == "approve"
    swap = next(v for k, v in rows.items() if k.startswith(key(4, "swap:")))
    assert (swap.rec.kind, swap.rec.out_sym, swap.rec.out_qty, swap.rec.in_sym, swap.rec.in_qty) == \
        ("trade", "ETH", D("0.5"), f"USDC@ETH:{USDC}", D("1000"))
    assert swap.rec.review  # Swap immer zur Prüfung
    # zwei gleiche Token-Eingänge in einem Hash: zwei Zeilen mit stabilen, verschiedenen Kennungen
    five = {k: v for k, v in rows.items() if k.startswith(f"ethereum:{H(5)}:")}
    usdc = [v for v in five.values() if v.rec.in_sym == f"USDC@ETH:{USDC}"]
    assert len(usdc) == 2 and {v.rec.in_qty for v in usdc} == {D("50")}
    assert {k.rsplit("#", 1)[1] for k, v in five.items() if v in usdc} == {"0", "1"}
    spam = [v for v in five.values() if v.rec.in_sym == f"USDC@ETH:{FAKE}"]
    assert len(spam) == 1 and "Spam" in spam[0].rec.review  # gleiches Symbol, anderer Contract → eigenes Asset
    r = rows[key(6, "fee")]
    assert r.rec.kind == "fee" and r.rec.fee_qty == D("0.00189") and "fehlgeschlagen" in r.rec.note
    assert not [k for k in rows if k.startswith(f"ethereum:{H(7)}:")]  # 0-Wert-Transfer nicht gebucht
    assert "Menge 0" in res["message"]
    eight = [v for k, v in rows.items() if k.startswith(f"ethereum:{H(8)}:")]
    assert len(eight) == 1 and (eight[0].rec.kind, eight[0].rec.out_qty) == ("withdrawal", D("0.25"))  # saldiert
    assert "Vertragsaufruf" in eight[0].rec.review and eight[0].rec.fee_qty == D("0.0024")
    r = next(v for k, v in rows.items() if k.startswith(f"ethereum:{H(9)}:{A}#t:"))
    assert (r.rec.kind, r.rec.out_sym, r.rec.out_qty, r.rec.review) == ("withdrawal", f"USDC@ETH:{USDC}", D("200"),
                                                                         None)
    # Rohreferenz, Bestätigung, Hash
    assert r.rec.raw["block"] == 900 and r.rec.raw["confirmed"] and r.rec.txhash == H(9)
    st = source(client, sid)
    import json
    assert json.loads(st["cursor_json"])["block"] == 1000 - 64 + 1
    cov = json.loads(st["coverage_json"])
    assert cov["complete"] and not cov["gaps"] and cov["limits"]
    ds = datasource_service(ctx(client)).get(sid)
    assert ds.sync_state == ("vollständig synchronisiert", "good")


def test_resync_is_idempotent_and_incremental(client, evm):
    sid = eth_wallet(client)
    first = sync(client, sid)
    n_rows = len(all_rows(client, sid))
    again = sync(client, sid)
    assert again["status"] == "synced" and again.get("new", 0) == 0
    assert len(all_rows(client, sid)) == n_rows
    # neuer Block, neue Transaktion: nur der neue Bereich wird abgefragt
    d = evm.chains[1]
    d["tip"] = 1100
    d["txlist"].append({**d["txlist"][0], "hash": H(20), "blockNumber": "1010", "timeStamp": str(int(
        d["txlist"][0]["timeStamp"]) + 910 * 60), "value": "1000000000000000000"})
    evm.calls.clear()
    res = sync(client, sid)
    assert res["new"] == 1, res
    starts = {int(r.url.params["startblock"]) for r in evm.requests("txlist")}
    assert starts == {937}
    assert first["batch_id"] and key(20, "n:in") in all_rows(client, sid)


def test_unconfirmed_blocks_are_not_booked_yet(client, evm):
    sid = eth_wallet(client)
    d = evm.chains[1]
    d["txlist"].append({**d["txlist"][0], "hash": H(30), "blockNumber": "990", "value": "7000000000000000000"})
    sync(client, sid)
    assert key(30, "n:in") not in all_rows(client, sid)  # 1000 − 64 = 936 < 990
    d["tip"] = 1060
    sync(client, sid)
    assert key(30, "n:in") in all_rows(client, sid)


# ----------------------------------------------------------------------------------------------------
# Mehrere Chains, gleiche Adresse
# ----------------------------------------------------------------------------------------------------

def test_same_address_on_three_chains_stays_separate(client, evm):
    set_provider_key(client, "etherscan", ETHERSCAN_KEY)
    eth = create_wallet(client, "ethereum", A, name="MetaMask ETH", group="MetaMask")
    bsc = create_wallet(client, "bsc", A, name="MetaMask BNB", group="MetaMask", chain_provider="routescan")
    avax = create_wallet(client, "avalanche", A, name="MetaMask AVAX", group="MetaMask")
    for sid in (eth, bsc, avax):
        assert sync(client, sid).get("status") == "synced"
    keys = {sid: set(all_rows(client, sid)) for sid in (eth, bsc, avax)}
    assert all(k.startswith("ethereum:") for k in keys[eth])
    assert all(k.startswith("bsc:") for k in keys[bsc])
    assert all(k.startswith("avalanche:") for k in keys[avax])
    syms = {sid: {rc.rec.in_sym or rc.rec.out_sym for rc in all_rows(client, sid).values()} for sid in (eth, bsc, avax)}
    assert f"USDC@BSC:{USDC}" in syms[bsc] and f"USDC@ETH:{USDC}" not in syms[bsc]
    assert "BNB" in syms[bsc] and "AVAX" in syms[avax] and "ETH" not in syms[bsc] | syms[avax]
    assert balances(client, bsc).keys() >= {"BNB"} and "ETH" not in balances(client, bsc)
    hosts = {r.url.host for r in evm.calls if "/43114/" in r.url.path or "/56/" in r.url.path}
    assert hosts == {"api.routescan.io"}
    batches = ctx(client).db.q("SELECT DISTINCT source FROM csv_batch WHERE kind='sync'")
    assert {b["source"] for b in batches} == {"sync:ethereum", "sync:bsc", "sync:avalanche"}


def test_etherscan_free_key_for_bnb_reports_plan_limit(client, evm):
    set_provider_key(client, "etherscan", ETHERSCAN_KEY)
    sid = create_wallet(client, "bsc", A, name="MetaMask BNB", chain_provider="etherscan")
    ok, msg = datasource_service(ctx(client)).check(sid)
    assert not ok and "kostenpflichtig" in msg and ETHERSCAN_KEY not in msg
    res = sync(client, sid)
    assert res["kind"] == "scope" and source(client, sid)["status"] == "error"


def test_missing_provider_key_blocks_without_plaintext(client, evm):
    sid = create_wallet(client, "ethereum", A, name="Ledger ETH")
    res = sync(client, sid)
    assert res["kind"] == "config" and "Anbieter-Schlüssel" in res["error"]
    assert not evm.calls


# ----------------------------------------------------------------------------------------------------
# Paginierung, Drosselung, Abbruch
# ----------------------------------------------------------------------------------------------------

def _bulk(n_blocks: int, per_block: int, same_block: int) -> dict:
    base = load("evm_eth.json")
    tx0 = base["txlist"][0]
    tok0 = base["tokentx"][1]
    txs, toks = [], []
    n = 1000
    for b in range(1, n_blocks + 1):
        for _ in range(per_block):
            n += 1
            txs.append({**tx0, "hash": H(n), "blockNumber": str(b), "timeStamp": str(1772445600 + b * 12),
                        "value": str(10**15 + n)})
    for _ in range(same_block):  # ein Block mit mehr als einer vollen Seite Token-Transfers
        n += 1
        toks.append({**tok0, "hash": H(n), "blockNumber": "500", "timeStamp": str(1772445600 + 500 * 12),
                     "value": str(1000 + n)})
    return {"address": A, "tip": n_blocks + 200, "txlist": txs, "txlistinternal": [], "tokentx": toks,
            "balance": "0", "tokenbalance": {}}


def test_pagination_never_splits_a_block_and_loses_nothing(client, evm):
    evm.chains[1] = _bulk(n_blocks=800, per_block=3, same_block=1200)
    sid = eth_wallet(client)
    res = sync(client, sid)
    assert res["status"] == "synced", res
    rows = all_rows(client, sid)
    native = [k for k in rows if k.endswith("#n:in")]
    tokens = [k for k in rows if "#t:" in k]
    assert len(native) == 2400 and len(tokens) == 1200 and len(set(rows)) == len(rows)
    same = [r for r in evm.requests("tokentx") if r.url.params["startblock"] == r.url.params["endblock"] == "500"]
    assert [r.url.params["page"] for r in same] == ["2"]


def test_rate_limits_are_retried_and_daily_limit_defers(client, evm):
    sid = eth_wallet(client)
    evm.fail_next(httpx.Response(200, json={"status": "0", "message": "NOTOK",
                                            "result": "Max calls per sec rate limit reached (3/sec)"}),
                  lambda r: r.url.params.get("action") == "txlist")
    evm.fail_next(httpx.Response(429, headers={"Retry-After": "2"}), lambda r: r.url.params.get("action") == "tokentx")
    evm.fail_next(httpx.Response(503), lambda r: r.url.params.get("action") == "txlistinternal")
    res = sync(client, sid)
    assert res["status"] == "synced", res
    assert 2.0 in evm.sleeps
    evm.fail_next(httpx.Response(200, json={"status": "0", "message": "NOTOK",
                                            "result": "Max daily rate limit reached. 100000 calls/day"}))
    res = sync(client, sid)
    assert res["kind"] == "rate_limit"
    st = source(client, sid)
    assert st["status"] == "error" and "Tageskontingent" in st["last_error"]


def test_interrupted_initial_import_resumes_without_loss(client, evm, monkeypatch):
    evm.chains[1] = _bulk(n_blocks=1500, per_block=2, same_block=0)
    sid = eth_wallet(client)
    monkeypatch.setattr(E.EthereumConnector, "max_requests", 6)  # Budget reicht nicht für den Erstabruf
    res = sync(client, sid)
    st = source(client, sid)
    import json
    cov = json.loads(st["coverage_json"])
    assert st["status"] == "partial" and cov["resume"] and not cov["complete"]
    first_cursor = json.loads(st["cursor_json"])["block"]
    assert 1 < first_cursor < 1500 + 200 - 64
    assert datasource_service(ctx(client)).get(sid).backfill_pending
    got = set(all_rows(client, sid))
    assert got and all(int(rc.rec.raw["block"]) < first_cursor for rc in all_rows(client, sid).values())
    # ein Fehler mitten in der nächsten Etappe: Abrufstand bleibt, nichts geht verloren
    evm.fail_next(httpx.Response(400, json={"message": "kaputt"}), lambda r: r.url.params.get("action") == "txlist")
    assert sync(client, sid).get("error")
    assert json.loads(source(client, sid)["cursor_json"])["block"] == first_cursor
    ds = datasource_service(ctx(client)).get(sid)
    assert ds.backfill_pending and ds.row["next_run_at"]  # Erstabruf bleibt geplant, auch ohne Intervall
    rounds = 0
    while datasource_service(ctx(client)).get(sid).backfill_pending:
        rounds += 1
        assert rounds < 50
        sync(client, sid)
    rows = all_rows(client, sid)
    assert len([k for k in rows if k.endswith("#n:in")]) == 3000 and len(set(rows)) == len(rows)
    assert source(client, sid)["status"] == "synced"
    assert res["batch_id"]


def test_background_sync_reports_progress_and_continues(client, evm, monkeypatch):
    import time

    from app.datasources import service as S
    evm.chains[1] = _bulk(n_blocks=600, per_block=2, same_block=0)
    sid = eth_wallet(client)
    monkeypatch.setattr(E.EthereumConnector, "max_requests", 6)
    r = post(client, f"/settings/datasources/{sid}/sync")
    assert r.status_code == 303 and "Abruf+gestartet" in r.headers["location"]
    for _ in range(400):
        if not S._SYNC_LOCK.locked():
            break
        time.sleep(0.02)
    assert not S._SYNC_LOCK.locked()
    ds = datasource_service(ctx(client)).get(sid)
    assert ds.progress["running"] is False and ds.progress["ok"]
    assert ds.row["status"] == "synced"  # Etappen bis zum Ende fortgesetzt
    assert len([k for k in all_rows(client, sid) if k.endswith("#n:in")]) == 1200
    assert client.get(f"/settings/datasources/{sid}/progress").status_code == 204


# ----------------------------------------------------------------------------------------------------
# Abgleich mit Börse/CSV, Bestände
# ----------------------------------------------------------------------------------------------------

def _book(c, when_utc: str, **fields) -> str:
    """Journal-Buchung im Expertenmodus (Zeit in UTC angegeben, Formular erwartet Ortszeit)."""
    from datetime import datetime

    from app.journal.service import journal_service
    from app.util.timeutil import local_tz

    js = journal_service(ctx(c))
    for aid in {fields.get("from_asset"), fields.get("to_asset")} - {None, "EUR"}:
        if aid not in js.known_assets():
            r = js.save_asset({"asset_id": aid, "name": aid, "asset_class": "crypto", "quote_source": "none"})
            assert not r.errors, r.errors
    local = datetime.fromisoformat(when_utc.replace("Z", "+00:00")).astimezone(local_tz())
    res = js.save({"kind": "expert", "date": local.date().isoformat(), "time": local.strftime("%H:%M:%S"),
                   **fields})
    assert not res.errors, res.errors
    return res.tx_ids[0]


def _journal_withdrawal(c, account: str, asset: str, qty: str, when: str) -> str:
    return _book(c, when, type="withdrawal", from_account=account, from_asset=asset, from_qty=qty)


def test_exchange_withdrawal_and_wallet_deposit_become_one_transfer(client, evm):

    _book(client, "2026-01-10T09:00:00Z", type="buy", from_account="Bitpanda", from_asset="EUR", from_qty="3000",
          to_account="Bitpanda", to_asset="ETH", to_qty="1.5", value_eur="3000")
    wd = _journal_withdrawal(client, "Bitpanda", "ETH", "1.5", "2026-03-02T11:20:00Z")
    sid = eth_wallet(client, auto_commit="1")
    res = sync(client, sid)
    dep = all_rows(client, sid)[key(1, "n:in")]
    assert dep.pair_ref == f"j:{wd}" and dep.pair_conf == "hoch" and "Menge 100.0 %" in dep.pair_why
    assert dep.status == "new" and not dep.tx_id  # nicht automatisch: bestehende Lots nie stillschweigend ändern
    out = csv_service(ctx(client)).commit(res["batch_id"], only_idx={dep.idx})
    assert out["transfers"] == 1, out
    t = ctx(client).db.q1("SELECT * FROM journal_tx WHERE source='transfer' AND status='active'")
    assert (t["from_account"], t["to_account"], t["from_asset"]) == ("Bitpanda", "Ledger ETH", "ETH")
    led = ctx(client).ledger()
    lots = led.lots_for("ETH", "Ledger ETH")
    assert lots and lots[0].acq_date.isoformat() == "2026-01-10"  # Anschaffungsdatum bleibt erhalten
    assert not [d for d in led.disposals if d.tx_id == wd]  # keine Veräußerung/kein Abgang an Dritte
    # Rückgängig löst den Transfer wieder auf
    csv_service(ctx(client)).revert(res["batch_id"])
    assert ctx(client).db.q1("SELECT status FROM journal_tx WHERE tx_id=?", (wd,))["status"] == "active"


def test_deposit_already_recorded_as_transfer_is_flagged(client, evm):
    _book(client, "2026-03-02T11:30:00Z", type="transfer", from_account="Bitpanda", from_asset="ETH",
          from_qty="1.5", to_account="Ledger ETH", to_asset="ETH", to_qty="1.5")
    sid = eth_wallet(client)
    sync(client, sid)
    dep = all_rows(client, sid)[key(1, "n:in")]
    assert dep.status == "duplicate" and dep.dup_same_account and "bereits als Transfer erfasst" in dep.warnings[0]
    assert not dep.include()


def test_same_transaction_from_ledger_live_csv_is_detected(client, evm):
    from tests.test_csvimport import create_unknown_assets, upload

    csv = ("Operation Date,Status,Currency Ticker,Operation Type,Operation Amount,Operation Fees,Operation Hash,"
           "Account Name,Account xpub,Countervalue Ticker,Countervalue at Operation Date,Countervalue at CSV Export\n"
           f"2026-03-02T13:20:00.000Z,Confirmed,ETH,OUT,0.20063,0.00063,{H(2)},Ethereum 1,xpub,EUR,500,600\n")
    bid, _ = upload(client, "ledger.csv", data=csv.encode(), account="Ledger ETH", cutoff="", cutoff_set="1")
    create_unknown_assets(client, bid)
    assert csv_service(ctx(client)).commit(bid)["created"] == 1
    sid = eth_wallet(client)
    sync(client, sid)
    row = all_rows(client, sid)[key(2, "n:out")]
    assert row.status == "duplicate" and "gleiche Blockchain-Transaktion" in row.warnings[0]


def test_observed_vs_explained_balances(client, evm):
    sid = eth_wallet(client)
    res = sync(client, sid)
    svc = datasource_service(ctx(client))
    view = svc.holdings(svc.get(sid))
    eth = next(i for i in view["items"] if i["key"] == "ETH")
    assert eth["observed"] == D("0.53982") and eth["explained"] == 0 and eth["state"] == "diff"
    assert any(i["state"] == "unmapped" and i["key"] == f"USDC@ETH:{USDC}" for i in view["items"])
    assert not any(i["key"] == f"USDC@ETH:{FAKE}" for i in view["items"])  # Spam ohne Bestandsprüfung
    # eindeutige Zeilen übernehmen (Swap/Spam/Vertragsaufrufe bleiben offen) → Differenz wird kleiner, nicht 0
    csv_service(ctx(client)).commit(res["batch_id"])
    view = svc.holdings(svc.get(sid))
    eth = next(i for i in view["items"] if i["key"] == "ETH")
    assert eth["explained"] > 0 and eth["diff"] != 0 and eth["state"] == "diff"


# ----------------------------------------------------------------------------------------------------
# Sicherheit
# ----------------------------------------------------------------------------------------------------

def test_wallet_pages_offer_sync_only_for_supported_chains(client, evm):
    sid = eth_wallet(client)
    post(client, "/settings/datasources", kind="wallet", provider="tron", name="TronLink TRX", account="TronLink",
         address="T" + "9" * 33, sync_interval_min="0")
    page = client.get("/settings/datasources").text
    assert page.count("Erstabruf starten") == 1 and "Ledger" in page and "Ohne Gruppe" in page
    detail = client.get(f"/settings/datasources/{sid}").text
    assert "Etherscan API V2" in detail and "Routescan" in detail
    new = client.get("/settings/datasources/new?kind=wallet").text
    for chain in ("Bitcoin", "Ethereum", "BNB Smart Chain", "Polygon PoS", "Avalanche C-Chain", "XRP Ledger",
                  "Cardano", "Polkadot"):
        assert chain in new


def test_provider_key_is_encrypted_never_echoed_or_exported(client, evm):
    sid = eth_wallet(client)
    page = client.get("/settings/datasources").text + client.get(f"/settings/datasources/{sid}").text
    assert ETHERSCAN_KEY not in page and "••••GHIJ" in page
    blob = ctx(client).db.q1("SELECT ciphertext FROM provider_secret WHERE provider='etherscan'")["ciphertext"]
    assert ETHERSCAN_KEY.encode() not in bytes(blob)
    from app.fullexport import collect
    assert all(ETHERSCAN_KEY.encode() not in v for v in collect(ctx(client), set()).values())
    evm.fail_next(httpx.Response(400, json={"message": f"bad apikey={ETHERSCAN_KEY}"}))
    res = sync(client, sid)
    assert ETHERSCAN_KEY not in res["error"] and ETHERSCAN_KEY not in str(source(client, sid))


def test_without_master_key_no_provider_key_is_stored(client, evm, monkeypatch):
    monkeypatch.delenv("PORTFOLIA_MASTER_KEY")
    r = post(client, "/settings/datasources/provider-keys/etherscan", api_key=ETHERSCAN_KEY)
    assert "error=" in r.headers["location"]
    assert ctx(client).db.scalar("SELECT COUNT(*) FROM provider_secret") == 0


def test_chain_http_refuses_foreign_hosts_paths_and_redirects():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data"})

    http = CH.ChainHttp(CH.ENDPOINTS["mempool"], transport=httpx.MockTransport(handler), sleep=lambda s: None)
    with pytest.raises(K.ConnectorError, match="nicht gefolgt"):
        http.get("/address/bc1qxyz", what="Test")
    assert len(seen) == 1 and seen[0].url.host == "mempool.space"
    for bad in ("/../admin", "/address/x?y=1", "//evil.example/x", "/a b"):
        with pytest.raises(K.ConnectorError):
            http.get(bad, what="Test")
    with pytest.raises(K.ConnectorError, match="verlangt einen Schlüssel"):
        CH.ChainHttp(CH.ENDPOINTS["etherscan"])
    assert all(e.base.startswith("https://") for e in CH.ENDPOINTS.values())


def test_json_is_read_as_decimal():
    assert CH.loads('{"a": 0.1, "b": 12345678901234567890.123456789}') == \
        {"a": D("0.1"), "b": D("12345678901234567890.123456789")}


def test_migration_10_keeps_sources_and_adds_wallet_tables(tmp_path):
    from app.db import Database

    d = Database(tmp_path / "app.sqlite")
    d.migrate(target=9)
    d.x("INSERT INTO data_source(kind, provider, name, account, address, created_at, updated_at) VALUES "
        "('wallet', 'ethereum', 'Alt', 'Alt', ?, 'x', 'x')", (A,))
    d.migrate(target=10)
    assert d.scalar("PRAGMA user_version") == 10
    row = d.q1("SELECT * FROM data_source WHERE name='Alt'")
    assert row["address"] == A and row["wallet_group"] is None and row["watch_json"] is None
    tables = {r["name"] for r in d.q("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"ds_balance", "provider_secret"} <= tables
    # ältere Wallet-Quelle ohne Beobachtungsdaten bleibt nutzbar (Adresse aus der Spalte)
    from app.datasources.service import DataSource
    ds = DataSource(row)
    assert ds.addresses == [A] and ds.watch.addresses == []


def test_token_mapping_in_review_batch_uses_contract(client, evm):
    sid = eth_wallet(client)
    res = sync(client, sid)
    page = client.get(f"/journal/csv/{res['batch_id']}").text
    assert "Token über Chain und Contract erkannt" in page
    assert f"https://etherscan.io/token/{USDC}" in page and f"https://etherscan.io/token/{FAKE}" in page
    assert "USDC · ETH 0xa0b869…eb48" in page
    import re as _re
    form = {}
    for i, sym in _re.findall(r'name="sym_(\d+)" value="([^"]+)"', page):
        if sym.upper() == f"USDC@ETH:{USDC}".upper():
            form |= {f"sym_{i}": sym, f"act_{i}": "new", f"id_{i}": "USDC", f"name_{i}": "USD Coin",
                     f"class_{i}": "crypto", f"qid_{i}": "usd-coin"}
        elif sym.upper() == f"USDC@ETH:{FAKE}".upper():
            form |= {f"sym_{i}": sym, f"act_{i}": "ignore"}
            # Standard für Tokens: „später“ (kein Asset aus einem womöglich gefälschten Symbol)
            sel = _re.search(rf'<select name="act_{i}"[^>]*>(.*?)</select>', page, _re.S).group(1)
            assert 'value="new" selected' not in sel and 'value="ignore" selected' in sel  # Spam: ignorieren
    r = post(client, f"/journal/csv/{res['batch_id']}/symbols", **form)
    assert r.status_code == 303
    rows = rows_by_ext(client, res["batch_id"])
    usdc_rows = [v for v in rows.values() if v.rec.in_sym == f"USDC@ETH:{USDC}" and v.rec.kind == "deposit"]
    assert usdc_rows and all(v.row and v.row["to_asset"] == "USDC" for v in usdc_rows)
    spam_rows = [v for v in rows.values() if v.rec.in_sym == f"USDC@ETH:{FAKE}"]
    assert spam_rows and all(v.status == "ignored" for v in spam_rows)
    # gleiches Symbol, anderer Contract: nie dem echten USDC zugeordnet
    from app.csvimport.service import csv_service as _csv
    saved = _csv(ctx(client)).saved_symbols()
    assert saved[f"USDC@ETH:{USDC}".upper()] == "USDC" and saved[f"USDC@ETH:{FAKE}".upper()] is None
