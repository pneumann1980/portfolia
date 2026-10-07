"""Cardano über Koios – Konto über die Stake-Adresse (synthetische Daten, ohne Netz).

Abgedeckt: Adressprüfung (CIP-19, Prüfsumme, Testnetz), Ableitung der Stake-Adresse aus einer Basisadresse,
Eingang, Abgang mit Wechselgeld an eine andere eigene Adresse (kein Abgang), Gebühr, Pfand der Stake-Registrierung
(prüfbedürftig), Reward-Abhebung als Umbuchung (kein Zugang), native Assets mit gleichem Namen und verschiedener
Policy, DEX-Vorgang mit fremden Eingängen (ohne Gebühr, zur Prüfung), Rewards je Epoche erst ab Verfügbarkeit,
Paginierung, Key optional (Bearer), Adressmodus ohne Stake-Teil, Überschneidung Basisadresse ↔ Stake-Adresse.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import httpx
import pytest

from app.datasources import chainhttp as CH
from app.datasources.chains import cardano as CA
from app.datasources.chains.codec import bech32_encode
from app.datasources.providers import PROVIDERS, normalize_address
from app.datasources.service import datasource_service
from app.datasources.wallet import WalletConnector
from tests.wallet_fakes import (
    MASTER,
    FakeKoios,
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
SC = b"\xb0" * 28  # Stake-Teil der eigenen Wallet
A0 = bech32_encode("addr", bytes([0x01]) + b"\xa0" * 28 + SC)  # Empfang
A1 = bech32_encode("addr", bytes([0x01]) + b"\xa1" * 28 + SC)  # Wechselgeld
S = bech32_encode("stake", bytes([0xE1]) + SC)
F = bech32_encode("addr", bytes([0x01]) + b"\xc0" * 28 + b"\xd0" * 28)  # fremd
FS = bech32_encode("stake", bytes([0xE1]) + b"\xd0" * 28)
F2 = bech32_encode("addr", bytes([0x61]) + b"\xc2" * 28)  # fremd, Enterprise
SCRIPT = bech32_encode("addr", bytes([0x11]) + b"\xe0" * 28 + b"\xd1" * 28)  # DEX-Skript (fremder Stake-Teil)
ENT = bech32_encode("addr", bytes([0x61]) + b"\xa9" * 28)  # eigene Enterprise-Adresse (ohne Stake-Teil)
POOL = "pool1" + "q" * 51
T0 = 1_700_000_000
ADA = 1_000_000
P1, P2 = "aa" * 28, "bb" * 28
NAME = "484f534b59"  # „HOSKY“


def fp(policy: str, name: str) -> str:
    return bech32_encode("asset", hashlib.blake2b(bytes.fromhex(policy + name), digest_size=20).digest())


def io(addr: str, stake: str | None, lovelace: int, h: str = "00" * 32, i: int = 0, assets: list | None = None) -> dict:
    return {"payment_addr": {"bech32": addr, "cred": "00"}, "stake_addr": stake, "tx_hash": h, "tx_index": i,
            "value": str(lovelace), "asset_list": assets or []}


def asset(policy: str, name: str, qty: int, dec: int = 0) -> dict:
    return {"policy_id": policy, "asset_name": name, "fingerprint": fp(policy, name), "decimals": dec,
            "quantity": str(qty)}


def ctx_(n: int, height: int, ins: list, outs: list, fee: int, **kw) -> dict:
    return {"tx_hash": f"{n:064x}", "block_height": height, "epoch_no": 300 + n, "tx_timestamp": T0 + n * 3600,
            "fee": str(fee), "deposit": str(kw.pop("deposit", 0)), "treasury_donation": "0", "inputs": ins,
            "outputs": outs, "withdrawals": kw.pop("withdrawals", []), "certificates": kw.pop("certificates", []),
            "assets_minted": [], **kw}


def history() -> list[dict]:
    return [
        ctx_(1, 100, [io(F, FS, 1000 * ADA)], [io(A0, S, 500 * ADA), io(F, FS, 499_800_000)], 200_000),
        ctx_(2, 110, [io(A0, S, 500 * ADA)], [io(F2, None, 100 * ADA), io(A1, S, 399_800_000)], 200_000),
        ctx_(3, 120, [io(A1, S, 399_800_000)], [io(A1, S, 397_600_000)], 200_000, deposit=2 * ADA,
             certificates=[{"index": 0, "type": "stake_registration", "info": {"stake_address": S}},
                           {"index": 1, "type": "delegation", "info": {"stake_address": S, "pool": POOL}}]),
        ctx_(4, 130, [io(A1, S, 397_600_000)], [io(A0, S, 402_430_000)], 170_000,
             withdrawals=[{"amount": str(5 * ADA), "stake_addr": S}]),
        ctx_(5, 140, [io(F, FS, 10 * ADA)], [io(A0, S, 2 * ADA, assets=[asset(P1, NAME, 1000), asset(P2, NAME, 5)]),
                                            io(F, FS, 7_800_000)], 200_000),
        ctx_(6, 150, [io(A0, S, 50 * ADA), io(F, FS, 10 * ADA)], [io(SCRIPT, None, 59_700_000)], 300_000),
    ]


def rewards() -> list[dict]:
    return [{"stake_address": S, "earned_epoch": 300, "spendable_epoch": 302, "amount": str(3 * ADA), "type": "member",
             "pool_id_bech32": POOL},
            {"stake_address": S, "earned_epoch": 301, "spendable_epoch": 303, "amount": str(2 * ADA), "type": "member",
             "pool_id_bech32": POOL},
            {"stake_address": S, "earned_epoch": 305, "spendable_epoch": 307, "amount": str(ADA), "type": "member",
             "pool_id_bech32": POOL}]


@pytest.fixture
def koios(monkeypatch):
    fake = FakeKoios(history(), {"block_height": 1000, "epoch_no": 306, "block_time": T0 + 10**6}, rewards(),
                     accounts={S: {"utxo": str(354_430_000 + 2 * ADA), "rewards_available": str(ADA),
                                   "deposit": str(2 * ADA), "delegated_pool": POOL}},
                     assets=[{"stake_address": S, **asset(P1, NAME, 1000)}, {"stake_address": S, **asset(P2, NAME, 5)}])
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


def k(n: int, sub: str, owner: str = S) -> str:
    return f"cardano:{n:064x}:{owner}#{sub}"


def test_address_validation_and_stake_derivation():
    p = PROVIDERS["cardano"]
    assert normalize_address(p, A0) == (A0, None)
    assert normalize_address(p, S.upper()) == (S, None)
    bad = A0[:-1] + ("q" if A0[-1] != "q" else "p")
    assert "Prüfsumme" in normalize_address(p, bad)[1]
    test_net = bech32_encode("addr_test", bytes([0x00]) + b"\xa0" * 56)
    assert normalize_address(p, test_net)[0] is None
    from app.datasources.chains.codec import cardano_stake_of
    assert cardano_stake_of(A0) == cardano_stake_of(A1) == S and cardano_stake_of(ENT) is None


def test_account_mode_books_change_fees_deposit_withdrawal_assets_and_rewards(client, koios):
    sid = create_wallet(client, "cardano", A0, name="Ledger ADA")  # Basisadresse → Konto über die Stake-Adresse
    res = sync(client, sid)
    assert res["status"] == "synced", res
    rows = all_rows(client, sid)
    r = rows[k(1, "ada")].rec
    assert (r.kind, r.in_sym, r.in_qty, r.review) == ("deposit", "ADA", D(500), None)
    r = rows[k(2, "ada")].rec  # 100 ADA an Fremde; Wechselgeld an A1 ist kein Abgang
    assert (r.kind, r.out_qty, r.fee_qty, r.review) == ("withdrawal", D(100), D("0.2"), None)
    r = rows[k(3, "deposit")].rec
    assert (r.kind, r.out_qty, r.fee_qty) == ("withdrawal", D(2), D("0.2")) and "Pfand" in r.review
    assert "stake_registration" in (r.label or "")
    assert not [x for x in rows if x.startswith(f"cardano:{3:064x}:") and x.endswith("#ada")]
    r = rows[k(4, "fee")].rec  # Reward-Abhebung: Umbuchung, nur Gebühr
    assert r.kind == "fee" and r.fee_qty == D("0.17") and r.label == "Reward-Abhebung"
    h1, h2 = (rows[k(5, f"a:{fp(p, NAME)}")].rec for p in (P1, P2))
    assert h1.in_sym == f"HOSKY@CARDANO:{fp(P1, NAME)}" and h1.in_qty == D(1000)
    assert h2.in_sym == f"HOSKY@CARDANO:{fp(P2, NAME)}" and h2.in_sym != h1.in_sym
    r = rows[k(6, "ada")].rec  # DEX mit fremdem Eingang: keine Gebühr, zur Prüfung
    assert (r.kind, r.out_qty, r.fee_qty) == ("withdrawal", D(50), None) and "fremden Eingängen" in r.review
    rw = [v.rec for x, v in rows.items() if x.startswith("cardano:reward-")]
    assert sorted(r.in_qty for r in rw) == [D(2), D(3)] and all(r.tag == "staking" and not r.review for r in rw)
    assert not [x for x in rows if x.startswith("cardano:reward-305-")]  # noch nicht verfügbar
    assert json.loads(source(client, sid)["cursor_json"])["reward_epoch"] == 301
    b = balances(client, sid)
    assert b["ADA"] == "357.43"  # UTXO + verfügbare Rewards (Pfand nicht enthalten)
    assert b[f"HOSKY@CARDANO:{fp(P1, NAME)}"] == "1000"
    # Rewards der Epoche 305 erst, wenn verfügbar; keine Doppelungen beim erneuten Abruf
    koios.tip = {"block_height": 1100, "epoch_no": 308, "block_time": T0 + 2 * 10**6}
    n = len(all_rows(client, sid))
    sync(client, sid)
    rows = all_rows(client, sid)
    assert len(rows) == n + 1 and any(x.startswith("cardano:reward-305-") for x in rows)
    datasource_service(ctx(client)).reset_cursor(sid)
    sync(client, sid)
    assert len(all_rows(client, sid)) == n + 1


def test_pagination_and_tx_batches(client, koios, monkeypatch):
    monkeypatch.setattr(CA, "PAGE", 2)
    monkeypatch.setattr(CA, "TX_BATCH", 2)
    sid = create_wallet(client, "cardano", S, name="Ledger ADA")
    assert sync(client, sid)["status"] == "synced"
    assert len([x for x in all_rows(client, sid) if not x.startswith("cardano:reward-")]) >= 7
    assert sum(1 for c in koios.calls if c.url.path.endswith("/account_txs")) >= 4
    assert sum(1 for c in koios.calls if c.url.path.endswith("/tx_info")) == 3


def test_interrupted_run_resumes_without_duplicates(client, koios, monkeypatch):
    monkeypatch.setattr(CA, "TX_BATCH", 2)
    monkeypatch.setattr(WalletConnector, "max_requests", 4)  # tip, account_txs, 2× tx_info → Abbruch
    sid = create_wallet(client, "cardano", S, name="Ledger ADA")
    res = sync(client, sid)
    assert res["status"] == "partial", res
    first = set(all_rows(client, sid))
    cur = json.loads(source(client, sid)["cursor_json"])
    assert cur["height"] == 140  # Blöcke bis 130 vollständig
    monkeypatch.setattr(WalletConnector, "max_requests", 2500)
    sync(client, sid)
    rows = all_rows(client, sid)
    assert first <= set(rows) and k(6, "ada") in rows
    assert len({x.split(":")[1] for x in rows if not x.startswith("cardano:reward-")}) == 6


def test_optional_key_sent_as_bearer_and_provider_errors(client, koios):
    set_provider_key(client, "koios", "koios_test_token_0123456789")
    koios.key = "koios_test_token_0123456789"
    sid = create_wallet(client, "cardano", S, name="Ledger ADA")
    assert sync(client, sid)["status"] == "synced"
    koios.fail_next(httpx.Response(429, headers={"Retry-After": "3600"}, json={"message": "rate limited"}))
    cursor = source(client, sid)["cursor_json"]
    res = sync(client, sid)
    assert "drosselt" in res["error"] and source(client, sid)["cursor_json"] == cursor


def test_address_mode_and_overlap_with_stake_account(client, koios):
    koios.txs[f"{7:064x}"] = ctx_(7, 160, [io(F, FS, 5 * ADA)], [io(ENT, None, 4 * ADA)], 200_000)
    ent = create_wallet(client, "cardano", ENT, name="Enterprise")
    res = sync(client, ent)
    assert res["status"] == "synced"
    rows = all_rows(client, ent)
    assert rows[k(7, "ada", ENT)].rec.in_qty == D(4)
    assert "Adressmodus" in client.get("/settings/datasources").text
    create_wallet(client, "cardano", A0, name="Ledger ADA")
    r = post(client, "/settings/datasources", kind="wallet", provider="cardano", name="Doppelt", address=S,
             account="Doppelt", sync_interval_min="0")
    assert r.status_code == 400 and "bereits als „Ledger ADA“ angelegt" in r.text
    other = bech32_encode("addr", bytes([0x01]) + b"\xa0" * 28 + b"\xb9" * 28)  # anderer Stake-Teil
    r = post(client, "/settings/datasources", kind="wallet", provider="cardano", name="Gemischt",
             address=f"{A0}\n{other}", account="G", sync_interval_min="0")
    assert r.status_code == 400 and "verschiedenen Cardano-Konten" in r.text
