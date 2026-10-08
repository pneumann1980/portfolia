"""Datenquellen: Migration 7, Anlegen/Bearbeiten/Deaktivieren/Entfernen, Connector-Vertrag, idempotente
Synchronisierung, Abgleich CSV-Import ↔ Datenquelle, Fehlermeldungen ohne Geheimnisse, Zeitplan."""

from __future__ import annotations

import copy
import gzip
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar

import httpx
import pytest
from fastapi.testclient import TestClient

from app.csvimport import model as M
from app.csvimport.events import derive_event_key, derive_tx_hash, match_keys
from app.csvimport.model import Rec
from app.csvimport.service import CsvImportService, csv_service
from app.datasources import connector as K
from app.datasources.providers import PROVIDERS, looks_secret, normalize_address
from app.datasources.service import datasource_service, next_run, sanitize_error
from app.db import MIGRATIONS, Database
from app.jobs.scheduler import Scheduler
from app.main import build_app
from tests.test_csvimport import create_unknown_assets, journal, post, upload

D = Decimal
KEY = "PORTFOLIA_DS_KRAKEN"


# ----------------------------------------------------------------------------------------------------
# Testaufbau: App, Test-Connector
# ----------------------------------------------------------------------------------------------------

@pytest.fixture
def client(config):
    with TestClient(build_app(config, start_scheduler=False)) as c:
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        yield c


class FakeKraken(K.Connector):
    """Liefert vorgegebene Vorgänge – wie ein echter Connector, aber ohne Netz. Der Abrufstand ist die Anzahl
    bereits gelieferter Vorgänge; ``use_cursor=False`` simuliert überlappende Abrufe (alles erneut)."""

    provider = "kraken"
    label = "Kraken (Test)"
    needs_credentials = True
    events: ClassVar[list[K.SourceEvent]] = []
    fail: ClassVar[BaseException | None] = None
    complete: ClassVar[bool] = True
    use_cursor: ClassVar[bool] = True
    cursors: ClassVar[list[Any]] = []

    def check(self, cfg: K.SourceConfig, secret: K.Secret) -> K.CheckResult:
        secret.reveal()
        return K.CheckResult(True, "Leserechte vorhanden.")

    def fetch(self, cfg: K.SourceConfig, secret: K.Secret, cursor: dict[str, Any] | None) -> K.FetchResult:
        FakeKraken.cursors.append(cursor)
        if FakeKraken.fail is not None:
            raise FakeKraken.fail
        start = int((cursor or {}).get("n", 0)) if FakeKraken.use_cursor else 0
        evs = copy.deepcopy(FakeKraken.events[start:])
        return K.FetchResult(events=evs, cursor={"n": len(FakeKraken.events)}, complete=FakeKraken.complete)


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv(KEY, "APIKEY-1234567890:SECRET-abcdefghij")
    FakeKraken.events, FakeKraken.fail, FakeKraken.complete, FakeKraken.cursors = [], None, True, []
    FakeKraken.use_cursor = True
    K.register(FakeKraken)
    try:
        yield FakeKraken
    finally:
        K.unregister("kraken")


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def buy(key: str, when: str, eur: str, btc: str, fee: str | None = None) -> K.SourceEvent:
    rec = Rec(line=0, ts=ts(when), kind=M.TRADE, out_sym="EUR", out_qty=D(eur), in_sym="BTC", in_qty=D(btc),
              fee_sym="EUR" if fee else None, fee_qty=D(fee) if fee else None, label="trade")
    return K.SourceEvent(key, ts(when), [rec])


def deposit(key: str, when: str, sym: str, qty: str) -> K.SourceEvent:
    return K.SourceEvent(key, ts(when), [Rec(line=0, ts=ts(when), kind=M.DEPOSIT, in_sym=sym, in_qty=D(qty),
                                             label="deposit")])


def buy_with_extra_fee(key: str, when: str) -> K.SourceEvent:
    """Ein Trade → zwei Buchungszeilen (Gebühr zusätzlich in einem dritten Asset)."""
    t = ts(when)
    return K.SourceEvent(key, t, [
        Rec(line=0, ts=t, kind=M.TRADE, out_sym="EUR", out_qty=D(300), in_sym="BTC", in_qty=D("0.005"),
            fee_sym="EUR", fee_qty=D("0.9"), label="trade"),
        Rec(line=0, ts=t, kind=M.FEE, fee_sym="EUR", fee_qty=D("0.25"), label="trade"),
    ])


def create_source(c, **form) -> int:
    data = {"kind": "exchange", "provider": "kraken", "name": "Kraken Hauptkonto", "account": "Kraken",
            "credential_ref": KEY, "sync_interval_min": "60", **form}
    r = post(c, "/settings/datasources", **data)
    assert r.status_code == 303, r.text[:800]
    return int(re.search(r"/settings/datasources/(\d+)", r.headers["location"]).group(1))


def source(c, sid) -> dict[str, Any]:
    r = c.app.state.ctx.db.q1("SELECT * FROM data_source WHERE id=?", (sid,))
    return dict(r) if r else {}


def sync(c, sid) -> Any:
    return post(c, f"/settings/datasources/{sid}/sync")


def batch_of(r) -> int:
    m = re.search(r"/journal/csv/(\d+)$", r.headers["location"])
    assert m, r.headers["location"]
    return int(m.group(1))


def rows_by_ext(c, bid) -> dict[str, Any]:
    return {rc.rec.ext_id: rc for rc in csv_service(c.app.state.ctx).rows(bid)}


def batches(c) -> list[dict[str, Any]]:
    return [dict(r) for r in c.app.state.ctx.db.q("SELECT id, kind, status, source, datasource_id FROM csv_batch "
                                                  "ORDER BY id")]


# ----------------------------------------------------------------------------------------------------
# Migration
# ----------------------------------------------------------------------------------------------------

def test_migration_7_keeps_data_and_adds_provenance(tmp_path):
    d = Database(tmp_path / "app.sqlite")
    d.migrate(target=6)
    assert d.scalar("PRAGMA user_version") == 6
    stamp = "2024-03-02T11:00:00Z"
    d.x("INSERT INTO journal_tx(tx_id, source, external_id, status, ts_utc, type, from_account, from_asset, "
        "from_qty, to_account, to_asset, to_qty, value_eur, created_at, updated_at) "
        "VALUES ('PF-C-000001', 'csv:kraken', 'kraken:T1', 'active', ?, 'buy', 'Kraken', 'EUR', '500', 'Kraken', "
        "'BTC', '0.01', '500', ?, ?)", (stamp, stamp, stamp))
    d.x("INSERT INTO journal_tx(tx_id, source, status, ts_utc, type, to_account, to_asset, to_qty, created_at, "
        "updated_at) VALUES ('PF-M-000001', 'manual', 'active', ?, 'deposit', 'Bank', 'EUR', '100', ?, ?)",
        (stamp, stamp, stamp))
    d.x("INSERT INTO csv_batch(filename, file_sha256, file_size, raw_gz, profile, account, status, created_at, "
        "updated_at) VALUES ('kraken.csv', 'x', 3, ?, 'kraken', 'Kraken', 'committed', ?, ?)",
        (gzip.compress(b"abc"), stamp, stamp))
    d.x("INSERT INTO csv_row(batch_id, idx, line, rec_json, status, tx_id) VALUES (1, 0, 2, '{}', 'committed', "
        "'PF-C-000001')")
    before = [dict(r) for r in d.q("SELECT * FROM journal_tx ORDER BY id")]

    d.migrate(target=7)
    assert d.scalar("PRAGMA user_version") == 7
    after = [dict(r) for r in d.q("SELECT * FROM journal_tx ORDER BY id")]
    assert [{k: r[k] for k in before[0]} for r in after] == before  # bestehende Daten unverändert
    assert all(r["event_key"] is None and r["event_line"] is None and r["tx_hash"] is None
               and r["datasource_id"] is None for r in after)
    b = d.q1("SELECT * FROM csv_batch")
    assert (b["kind"], b["source"], b["datasource_id"]) == ("csv", None, None)
    assert CsvImportService.source_of(b) == "csv:kraken"  # Quelle alter Stapel wie bisher
    row = d.q1("SELECT * FROM csv_row")
    assert (row["status"], row["event_key"], row["event_line"]) == ("committed", None, None)
    tables = {r["name"] for r in d.q("SELECT name FROM sqlite_master WHERE type IN ('table', 'index')")}
    assert {"data_source", "data_source_run", "ix_data_source_due", "ix_journal_tx_event", "ix_journal_tx_hash",
            "ux_journal_tx_ext"} <= tables
    # erneut migrieren ändert nichts
    d.migrate(target=7)
    assert d.scalar("PRAGMA user_version") == 7
    assert [dict(r) for r in d.q("SELECT * FROM journal_tx ORDER BY id")] == after
    d.migrate()
    assert d.scalar("PRAGMA user_version") == MIGRATIONS[-1][0]


def test_data_source_constraints(db):
    stamp = "2026-01-01T00:00:00Z"
    ins = ("INSERT INTO data_source(kind, provider, name, account, address, created_at, updated_at) "
           "VALUES (?,?,?,?,?,?,?)")
    db.x(ins, ("wallet", "ethereum", "A", "A", "0x" + "a" * 40, stamp, stamp))
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        db.x(ins, ("wallet", "ethereum", "B", "B", "0x" + "a" * 40, stamp, stamp))
    db.x(ins, ("wallet", "polygon", "C", "C", "0x" + "a" * 40, stamp, stamp))  # gleiche Adresse, andere Chain
    sid = db.scalar("SELECT id FROM data_source WHERE name='A'")
    db.x("INSERT INTO data_source_run(source_id, trigger, started_at, status) VALUES (?, 'manual', ?, 'ok')",
         (sid, stamp))
    row = db.q1("SELECT * FROM data_source WHERE id=?", (sid,))
    assert (row["status"], row["enabled"], row["sync_interval_min"], row["auto_commit"]) == ("created", 1, 0, 0)
    db.x("DELETE FROM data_source WHERE id=?", (sid,))  # Fremdschlüssel sind je Verbindung aktiv
    assert db.scalar("SELECT COUNT(*) FROM data_source_run") == 0  # Laufhistorie geht mit


# ----------------------------------------------------------------------------------------------------
# Kennungen, Adressen, Geheimnisse
# ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("ext", "expected"), [
    ("kraken:T1", "kraken:T1"), ("kraken:T1:fee:ZEUR", "kraken:T1"), ("kraken:T1:XXBT", "kraken:T1"),
    ("coinbase:5f1a-22", "coinbase:5f1a-22"), ("bitpanda:abc-123", "bitpanda:abc-123"),
    ("kraken:0123456789abcdef0123", None),  # Prüfsumme statt nativer ID
    ("coinbase:0123456789abcdef0123#2", None), ("binance:0123456789abcdef0123", None), ("pf:X1", None),
    ("koinly:abc", None), ("", None), (None, None),
])
def test_event_key_from_csv_ids(ext, expected):
    assert derive_event_key(ext) == expected


def test_tx_hash_from_wallet_exports():
    h = "AB" * 32
    assert derive_tx_hash(f"ledger:0x{h}:out:Ledger:ETH") == h.lower()
    assert derive_tx_hash(f"electrum:{h}") == h.lower()
    assert derive_tx_hash("ledger:kurz:out") is None and derive_tx_hash(f"kraken:{h}") is None
    assert match_keys("kraken:T1", "0x" + h) == {"e:kraken:T1", f"h:{h.lower()}"}
    assert match_keys(None, None) == set()


@pytest.mark.parametrize(("provider", "raw", "expected"), [
    ("ethereum", " 0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed ", "0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"),
    ("ethereum", "0xabcdef0123456789abcdef0123456789abcdef01", "0xabcdef0123456789abcdef0123456789abcdef01"),
    ("bitcoin", "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq", "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"),
    ("bitcoin", "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2", "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2"),
    ("solana", "7EcDhSYGxXyscszYEp35KHN8vvw3svAuLKTzXwCFLtV", "7EcDhSYGxXyscszYEp35KHN8vvw3svAuLKTzXwCFLtV"),
    ("kaspa", "kaspa:qqkqkzjvr7zwxxmjxjkmxxdwju9kjs6e9u82uh59z07vgaks6gg62v8707g73",
     "kaspa:qqkqkzjvr7zwxxmjxjkmxxdwju9kjs6e9u82uh59z07vgaks6gg62v8707g73"),
])
def test_address_normalization(provider, raw, expected):
    assert normalize_address(PROVIDERS[provider], raw) == (expected, None)


PRIVATE_HEX = "4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
SEED = "abandon ability able about above absent absorb abstract absurd abuse access accident"
WIF = "5HueCGU8rMjxEXxiPuD5BDku4MkFqeZyd4dZ1jvhTVqvbTLvyTJ"


@pytest.mark.parametrize("secret", [PRIVATE_HEX, "0x" + PRIVATE_HEX, SEED, WIF, "xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJU"
                                    "D2nW2nRk4stbPy6cq3jPPqjiChkVvvNKmPGJxWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGBxrMPHi"])
def test_private_keys_and_seeds_are_rejected_without_echo(secret):
    assert looks_secret(secret)
    addr, err = normalize_address(PROVIDERS["ethereum"], secret)
    assert addr is None and "privaten Schlüssel" in err and secret not in err


def test_checksums_catch_typos():
    for pid, raw in [("ethereum", "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAeD"),
                     ("bitcoin", "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdp"),
                     ("bitcoin", "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN3"),
                     ("kaspa", "kaspa:qqkqkzjvr7zwxxmjxjkmxxdwju9kjs6e9u82uh59z07vgaks6gg62v8707g74"),
                     ("solana", "7EcDhSYGxXyscszYEp35KHN8vvw3svAuLKTzXwCFL")]:
        addr, err = normalize_address(PROVIDERS[pid], raw)
        assert addr is None and err, (pid, raw)


def test_invalid_address_error_does_not_repeat_input():
    addr, err = normalize_address(PROVIDERS["bitcoin"], "hallo-welt-123")
    assert addr is None and "Keine gültige Bitcoin-Adresse" in err and "hallo" not in err


def test_secret_reference_and_masking(monkeypatch, tmp_path):
    monkeypatch.setenv(KEY, "  wert-12345678  ")
    s = K.Secret(KEY)
    assert s.present and s.reveal() == "wert-12345678"
    assert "wert" not in repr(s) and "wert" not in str(s) and "gesetzt" in repr(s)
    monkeypatch.delenv(KEY)
    f = tmp_path / "secret"
    f.write_text("aus-datei-987654\n")
    monkeypatch.setenv(f"{KEY}_FILE", str(f))
    assert K.Secret(KEY).reveal() == "aus-datei-987654"  # Docker-Secret
    assert not K.Secret("HOME").present  # nur Variablen mit Präfix PORTFOLIA_DS_
    with pytest.raises(K.ConnectorError) as e:
        K.Secret(None).reveal()
    assert e.value.kind == "config"


def test_sanitize_error_removes_secrets():
    text = ("401 for https://api.example.com/0/private/Ledgers?nonce=1&signature=abc123 "
            "API-Key: pk_live_998877 token=tok_55555 wert-12345678")
    out = sanitize_error(text, ["wert-12345678"])
    for s in ("abc123", "pk_live_998877", "tok_55555", "wert-12345678", "nonce=1"):
        assert s not in out
    assert "https://api.example.com/0/private/Ledgers?…" in out


# ----------------------------------------------------------------------------------------------------
# Einstellungen → Datenquellen (CRUD)
# ----------------------------------------------------------------------------------------------------

def test_crud_exchange_and_wallet(client):
    c = client
    page = c.get("/settings/datasources")
    assert page.status_code == 200 and "Noch keine Datenquelle" in page.text
    assert "/settings/datasources" in c.get("/settings").text
    assert "Kraken" in c.get("/settings/datasources/new?kind=exchange").text
    assert "Ethereum" in c.get("/settings/datasources/new?kind=wallet").text

    ex = create_source(c, account="", credential_ref="portfolia_ds_kraken", sync_interval_min="60")
    row = source(c, ex)
    assert (row["kind"], row["provider"], row["name"], row["account"], row["credential_ref"]) == \
        ("exchange", "kraken", "Kraken Hauptkonto", "Kraken Hauptkonto", KEY)  # Konto = Name, Variable groß
    assert (row["status"], row["enabled"], row["sync_interval_min"], row["next_run_at"]) == ("created", 1, 60, None)

    r = post(c, "/settings/datasources", kind="wallet", provider="ethereum", name="Ledger ETH", account="Ledger",
             address="0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed", sync_interval_min="1440")
    assert r.status_code == 303
    w = int(re.search(r"/settings/datasources/(\d+)", r.headers["location"]).group(1))
    assert source(c, w)["address"] == "0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"

    # Chain ohne Anbindung (nur CSV-Import): TRON
    r = post(c, "/settings/datasources", kind="wallet", provider="tron", name="TronLink TRX", account="TronLink",
             address="T" + "9" * 33, sync_interval_min="0", wallet_group="TronLink")
    assert r.status_code == 303, r.text[:500]
    page = c.get("/settings/datasources").text
    for s in ("Kraken Hauptkonto", "Ledger ETH", "angelegt", "Manuell / noch nicht unterstützt", KEY, "fehlt",
              "0x5aaeb6", "Ohne Gruppe", "TronLink TRX", "Anbieter-Schlüssel"):
        assert s in page, s
    assert "Jetzt synchronisieren" not in page  # ohne Connector bzw. ohne Lauf keine Synchronisierung anbieten

    # ansehen / bearbeiten
    form = c.get(f"/settings/datasources/{w}")
    assert form.status_code == 200 and "Ledger ETH" in form.text and "Synchronisierung" in form.text
    assert "0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed" in form.text
    r = post(c, f"/settings/datasources/{w}", provider="ethereum", name="Ledger ETH (alt)", account="Ledger",
             address="0xabcdef0123456789abcdef0123456789abcdef01", sync_interval_min="0", note="Cold Storage")
    assert r.status_code == 303
    row = source(c, w)
    assert (row["name"], row["note"], row["sync_interval_min"], row["kind"]) == \
        ("Ledger ETH (alt)", "Cold Storage", 0, "wallet")

    # deaktivieren / aktivieren
    assert post(c, f"/settings/datasources/{w}/toggle").status_code == 303
    assert source(c, w)["enabled"] == 0
    page = c.get("/settings/datasources").text
    assert "deaktiviert" in page and "Aktivieren" in page
    post(c, f"/settings/datasources/{w}/toggle")
    assert source(c, w)["enabled"] == 1

    # entfernen
    r = post(c, f"/settings/datasources/{w}/delete")
    assert r.status_code == 303 and "entfernt" in c.get(r.headers["location"]).text
    assert source(c, w) == {} and "Ledger ETH" not in c.get("/settings/datasources").text
    assert c.get(f"/settings/datasources/{w}").status_code == 404
    assert post(c, f"/settings/datasources/{w}/toggle").status_code == 404


def test_validation_and_no_secret_echo(client):
    c = client
    n0 = c.app.state.ctx.db.scalar("SELECT COUNT(*) FROM data_source")
    for secret in (PRIVATE_HEX, SEED):
        r = post(c, "/settings/datasources", kind="wallet", provider="ethereum", name="Hot Wallet", address=secret,
                 sync_interval_min="0")
        assert r.status_code == 400 and "privaten Schlüssel" in r.text
        assert secret not in r.text and secret.split()[0] + " " + secret.split()[-1] not in r.text
    # API-Schlüssel versehentlich statt Variablenname
    r = post(c, "/settings/datasources", kind="exchange", provider="kraken", name="Kraken",
             credential_ref="sk_live_1234567890abcdef", sync_interval_min="0")
    assert r.status_code == 400 and "PORTFOLIA_DS_" in r.text and "sk_live_1234567890abcdef" not in r.text
    assert c.app.state.ctx.db.scalar("SELECT COUNT(*) FROM data_source") == n0  # nichts gespeichert

    cases = [
        ({"kind": "wallet", "provider": "bitcoin", "name": "BTC", "address": "hallo"}, "Keine gültige Bitcoin"),
        ({"kind": "wallet", "provider": "kraken", "name": "X", "address": "0x" + "a" * 40}, "Chain wählen"),
        ({"kind": "exchange", "provider": "kraken", "name": ""}, "Name fehlt"),
        ({"kind": "exchange", "provider": "kraken", "name": "x" * 61}, "Name fehlt oder ist zu lang"),
        ({"kind": "exchange", "provider": "kraken", "name": "K", "sync_interval_min": "7"}, "intervall ungültig"),
        ({"kind": "other", "provider": "kraken", "name": "K"}, "Art wählen"),
    ]
    for data, msg in cases:
        r = post(c, "/settings/datasources", **{"sync_interval_min": "0", **data})
        assert r.status_code == 400 and msg in r.text, (data, r.text[:300])
    # gleiche Adresse zweimal
    addr = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
    assert post(c, "/settings/datasources", kind="wallet", provider="bitcoin", name="BTC 1", address=addr,
                sync_interval_min="0").status_code == 303
    r = post(c, "/settings/datasources", kind="wallet", provider="bitcoin", name="BTC 2", address=addr.upper(),
             sync_interval_min="0")
    assert r.status_code == 400 and "bereits als „BTC 1“ angelegt" in r.text
    # ohne CSRF-Token
    r = c.post("/settings/datasources", data={"kind": "exchange", "provider": "kraken", "name": "K"},
               follow_redirects=False)
    assert r.status_code == 403


def test_source_without_connector_is_honest(client):
    c = client
    sid = create_source(c, provider="bitvavo", name="Bitvavo", credential_ref="", sync_interval_min="60")
    card = c.get("/settings/datasources").text
    assert "Manuell / noch nicht unterstützt" in card and "keine automatische Anbindung" in card
    r = sync(c, sid)
    assert r.status_code == 303 and "noch+nicht+unterst" in r.headers["location"]
    ok, msg = datasource_service(c.app.state.ctx).check(sid)
    assert not ok and "CSV-Import" in msg
    row = source(c, sid)
    assert (row["status"], row["next_run_at"], row["last_run_at"]) == ("created", None, None)
    assert datasource_service(c.app.state.ctx).due(datetime.now(UTC) + timedelta(days=2)) == []


# ----------------------------------------------------------------------------------------------------
# Synchronisieren: Idempotenz, mehrere Zeilen je Ereignis, Herkunft
# ----------------------------------------------------------------------------------------------------

def test_sync_is_idempotent_and_records_provenance(client, fake):
    c = client
    sid = create_source(c)
    assert "Historischen Abgleich starten" in c.get("/settings/datasources").text
    ok, msg = datasource_service(c.app.state.ctx).check(sid)
    assert ok and msg == "Leserechte vorhanden." and source(c, sid)["status"] == "connected"
    assert datasource_service(c.app.state.ctx).runs(sid)[0]["trigger"] == "check"

    fake.events = [buy("kraken:T1", "2024-06-01T10:00:00", "500", "0.01", fee="1.3"),
                   buy_with_extra_fee("kraken:T2", "2024-06-02T10:00:00"),
                   deposit("kraken:L1", "2024-05-30T09:00:00", "EUR", "1000")]
    r = sync(c, sid)
    bid = batch_of(r)  # ohne automatische Übernahme → zur Prüfung
    create_unknown_assets(c, bid)
    rs = rows_by_ext(c, bid)
    assert set(rs) == {"kraken:T1#0", "kraken:T2#0", "kraken:T2#1", "kraken:L1#0"}
    assert {rc.status for rc in rs.values()} == {"new"}, {k: (v.status, v.errors) for k, v in rs.items()}
    assert (rs["kraken:T2#1"].rec.event_key, rs["kraken:T2#1"].rec.event_line) == ("kraken:T2", 1)
    page = c.get(f"/journal/csv/{bid}").text
    assert "Synchronisierung prüfen" in page and "Datenquelle" in page
    assert datasource_service(c.app.state.ctx).pending_batch(sid)["id"] == bid
    r = post(c, f"/journal/csv/{bid}/commit")
    assert r.status_code == 303 and "n=4" in r.headers["location"]

    txs = journal(c, "source='sync:kraken'")
    assert len(txs) == 4 and all(t["tx_id"].startswith("PF-S-") and t["datasource_id"] == sid
                                 and t["batch_id"] == bid for t in txs)
    by_ext = {t["external_id"]: t for t in txs}
    assert (by_ext["kraken:T2#0"]["event_key"], by_ext["kraken:T2#0"]["event_line"]) == ("kraken:T2", 0)
    assert (by_ext["kraken:T2#1"]["event_key"], by_ext["kraken:T2#1"]["event_line"]) == ("kraken:T2", 1)
    fee = by_ext["kraken:T2#1"]
    assert (fee["type"], fee["tag"], fee["from_asset"], fee["value_eur"]) == ("withdrawal", "fee", "EUR", "0.25")
    assert by_ext["kraken:T1#0"]["type"] == "buy"
    row = source(c, sid)
    assert row["status"] == "synced" and row["last_success_at"] and row["last_error"] is None
    assert json.loads(row["cursor_json"]) == {"n": 3} and row["next_run_at"] > row["last_run_at"]
    assert "Datenquelle · Kraken" in c.get("/journal").text
    assert "Datenquelle · Kraken" in c.get("/journal/csv").text  # Stapelliste
    raw = c.get(f"/journal/csv/{bid}/file")
    assert raw.status_code == 200 and raw.headers["content-type"].startswith("application/json")
    assert "kraken:T2#1" in raw.text and "APIKEY" not in raw.text and "SECRET" not in raw.text

    # dieselben Vorgänge erneut (überlappender Abruf): alles bekannt, kein neuer Stapel, keine neuen Buchungen
    fake.use_cursor = False
    n_batches = len(batches(c))
    r = sync(c, sid)
    assert r.status_code == 303 and "/settings/datasources" in r.headers["location"]
    assert "bekannt+4" in r.headers["location"]
    assert fake.cursors[-1] == {"n": 3}  # Abrufstand wird weitergegeben
    assert len(batches(c)) == n_batches and len(journal(c, "source='sync:kraken'")) == 4

    # ein neuer Vorgang: nur dieser ist neu
    fake.events.append(buy("kraken:T3", "2024-06-05T10:00:00", "200", "0.004"))
    bid2 = batch_of(sync(c, sid))
    st = {k: rc.status for k, rc in rows_by_ext(c, bid2).items()}
    assert st == {"kraken:T1#0": "known", "kraken:T2#0": "known", "kraken:T2#1": "known", "kraken:L1#0": "known",
                  "kraken:T3#0": "new"}
    post(c, f"/journal/csv/{bid2}/commit")
    assert len(journal(c, "source='sync:kraken'")) == 5
    runs = datasource_service(c.app.state.ctx).runs(sid)
    assert [r["status"] for r in runs][:3] == ["ok", "ok", "ok"] and runs[0]["rows_new"] == 1
    assert "Letzte Läufe" in c.get(f"/settings/datasources/{sid}").text


def test_duplicate_event_keys_in_one_fetch_are_collapsed(client, fake):
    c = client
    sid = create_source(c)
    ev = buy("kraken:T1", "2024-06-01T10:00:00", "500", "0.01")
    fake.events = [ev, copy.deepcopy(ev)]  # überlappende Abrufseiten
    bid = batch_of(sync(c, sid))
    assert list(rows_by_ext(c, bid)) == ["kraken:T1#0"]


@pytest.mark.parametrize("key", ["coinbase:T1", "kraken:", "kraken:T 1", "kraken:T1#0", "T1"])
def test_connector_contract_violation_is_an_error(client, fake, key):
    c = client
    sid = create_source(c)
    fake.events = [buy(key, "2024-06-01T10:00:00", "500", "0.01")]
    r = sync(c, sid)
    assert "Ung%C3%BCltige+Ereignis-ID" in r.headers["location"] or "Ungültige Ereignis-ID" in \
        c.get(r.headers["location"]).text
    row = source(c, sid)
    assert row["status"] == "error" and "Ereignis-ID" in row["last_error"]
    assert not [b for b in batches(c) if b["kind"] == "sync"]


# ----------------------------------------------------------------------------------------------------
# Abgleich CSV-Import ↔ Datenquelle
# ----------------------------------------------------------------------------------------------------

def test_csv_import_then_sync_shows_overlap_before_commit(client, fake):
    c = client
    b_csv, _ = upload(c, "kraken.csv", account="Kraken")
    create_unknown_assets(c, b_csv)
    assert post(c, f"/journal/csv/{b_csv}/commit").status_code == 303
    csv_tx = journal(c, "source='csv:kraken' AND external_id='kraken:T1'")
    assert csv_tx and csv_tx[0]["event_key"] == "kraken:T1"  # Ereignis-ID auch für CSV-Buchungen

    sid = create_source(c)
    fake.events = [buy("kraken:T1", "2024-03-02T11:00:00", "500", "0.01", fee="1.3"),  # schon per CSV da
                   deposit("kraken:L1", "2024-03-01T10:00:00", "EUR", "1000"),  # schon per CSV da
                   buy("kraken:T9", "2024-06-10T10:00:00", "250", "0.004")]  # neu
    bid = batch_of(sync(c, sid))
    rs = rows_by_ext(c, bid)
    # gleiche Anbieter-ID → bekannt: geht nicht erneut in Bewertung und Lots ein
    assert rs["kraken:T1#0"].status == "known" and rs["kraken:L1#0"].status == "known"
    assert rs["kraken:T9#0"].status == "new"
    assert all(not rs[k].include() for k in ("kraken:T1#0", "kraken:L1#0"))
    assert csv_tx[0]["tx_id"] in rs["kraken:T1#0"].dup_of
    page = c.get(f"/journal/csv/{bid}").text
    assert "bereits vorhanden als" in page and csv_tx[0]["tx_id"] in page and "gleiche Anbieter-ID" in page
    run = datasource_service(c.app.state.ctx).runs(sid)[0]
    assert (run["rows_new"], run["rows_known"], run["batch_id"]) == (1, 2, bid)
    assert "bekannt 2" in c.get(f"/settings/datasources/{sid}").text

    r = post(c, f"/journal/csv/{bid}/commit")
    assert "n=1" in r.headers["location"]
    assert [t["external_id"] for t in journal(c, "source='sync:kraken'")] == ["kraken:T9#0"]
    assert len(journal(c, "event_key='kraken:T1' AND status='active'")) == 1  # keine Doppelbuchung


def test_sync_then_csv_import_detects_same_events(client, fake):
    c = client
    sid = create_source(c)
    fake.events = [buy("kraken:T1", "2024-03-02T11:00:00", "500", "0.01", fee="1.3"),
                   deposit("kraken:L1", "2024-03-01T10:00:00", "EUR", "1000"),
                   # gleiche ID, andere Buchungsseite (wie Sender/Empfänger derselben Blockchain-Transaktion)
                   deposit("kraken:L7", "2024-03-04T12:00:00", "BTC", "0.005")]
    bid = batch_of(sync(c, sid))
    create_unknown_assets(c, bid)
    post(c, f"/journal/csv/{bid}/commit")
    assert len(journal(c, "source='sync:kraken'")) == 3

    b_csv, _ = upload(c, "kraken.csv", account="Kraken")
    create_unknown_assets(c, b_csv)
    by_ext = {rc.rec.ext_id: rc for rc in csv_service(c.app.state.ctx).rows(b_csv)}
    assert by_ext["kraken:T1"].status == "known" and "gleiche Anbieter-ID" in by_ext["kraken:T1"].warnings[0]
    assert by_ext["kraken:T1"].warnings[0].startswith("bereits vorhanden als PF-S-")
    assert by_ext["kraken:L1"].status == "known" and not by_ext["kraken:L1"].include()
    # gleiche Anbieter-ID = dasselbe Ereignis, auch bei anderer Buchungsseite (die Seitenprüfung gilt nur für
    # Blockchain-Hashes, wo dieselbe Transaktion Abgang beim Sender und Zugang beim Empfänger ist)
    assert by_ext["kraken:L7"].status == "known"
    assert by_ext["kraken:L8"].status not in ("known", "duplicate")  # kam nicht über die Datenquelle
    r = post(c, f"/journal/csv/{b_csv}/commit")
    assert "errors" in r.text or r.status_code in (200, 303)
    assert len(journal(c, "event_key='kraken:T1' AND status='active'")) == 1


# ----------------------------------------------------------------------------------------------------
# Automatische Übernahme, offener Prüf-Stapel, Zeitplan
# ----------------------------------------------------------------------------------------------------

def test_auto_commit_per_event_without_blocking(client, fake):
    c = client
    svc = datasource_service(c.app.state.ctx)
    sid = create_source(c, auto_commit="1")
    fake.events = [buy("kraken:T1", "2024-06-01T10:00:00", "500", "0.01")]
    bid = batch_of(sync(c, sid))  # unbekannte Assets → ungültig → zur Prüfung statt automatisch
    assert not journal(c, "source='sync:kraken'")
    # ein offener Prüf-Stapel blockiert weder Zeitplan noch manuelle Läufe; wartende Vorgänge kommen nicht doppelt
    fake.use_cursor = False
    r = sync(c, sid)
    assert "wartet+bereits+auf+Pr%C3%BCfung+1" in r.headers["location"]
    assert [d.id for d in svc.due(datetime.now(UTC) + timedelta(hours=2))] == [sid]
    assert len(batches(c)) == 1
    create_unknown_assets(c, bid)  # jetzt eindeutig → der nächste Lauf übernimmt automatisch
    sync(c, sid)
    assert [t["external_id"] for t in journal(c, "source='sync:kraken'")] == ["kraken:T1#0"]
    assert svc.pending_batch(sid) is None

    # neue eindeutige Vorgänge → sofort übernommen, kein Prüf-Stapel
    fake.events.append(buy("kraken:T2", "2024-06-03T10:00:00", "100", "0.002"))
    r = sync(c, sid)
    assert "/settings/datasources" in r.headers["location"] and "%C3%BCbernommen+1" in r.headers["location"]
    assert [t["external_id"] for t in journal(c, "source='sync:kraken'")] == ["kraken:T1#0", "kraken:T2#0"]

    # bereits per CSV vorhanden → bekannt; die übrigen neuen Vorgänge werden trotzdem übernommen
    b_csv, _ = upload(c, "kraken.csv", account="Kraken")
    create_unknown_assets(c, b_csv)
    post(c, f"/journal/csv/{b_csv}/commit")
    fake.events += [buy("kraken:T3", "2024-06-04T10:00:00", "100", "0.002"),
                    deposit("kraken:L1", "2024-03-01T10:00:00", "EUR", "1000")]
    sync(c, sid)
    assert journal(c, "external_id='kraken:T3#0'") and not journal(c, "external_id='kraken:L1#0'")
    assert svc.pending_batch(sid) is None


def test_schedule_due_and_next_run(client, fake):
    now = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    assert next_run(False, True, 60, None, now) is None
    assert next_run(True, False, 60, None, now) is None
    assert next_run(True, True, 0, None, now) is None
    assert next_run(True, True, 60, None, now) == now
    assert next_run(True, True, 60, now - timedelta(hours=3), now) == now  # überfällig → sofort, nicht rückwirkend
    assert next_run(True, True, 60, now - timedelta(minutes=10), now) == now + timedelta(minutes=50)

    c = client
    svc = datasource_service(c.app.state.ctx)
    sid = create_source(c, sync_interval_min="60")
    manual = create_source(c, name="Nur manuell", account="Kraken 2", sync_interval_min="0")
    assert source(c, sid)["next_run_at"] and source(c, manual)["next_run_at"] is None
    soon = datetime.now(UTC) + timedelta(seconds=5)
    assert [d.id for d in svc.due(soon)] == [sid]
    fake.events = [deposit("kraken:L1", "2024-03-01T10:00:00", "EUR", "1000")]
    res = svc.run_due()
    assert res["ran"] == 1
    runs = svc.runs(sid)
    assert runs[0]["trigger"] == "schedule"
    assert svc.due(soon) == [] and [d.id for d in svc.due(soon + timedelta(minutes=61))] == [sid]
    svc.set_enabled(sid, False)
    assert source(c, sid)["next_run_at"] is None and svc.sync(sid, "schedule") == {"skipped": "deaktiviert"}

    sched = Scheduler(c.app.state.ctx)
    try:
        sched.setup_default_jobs()
        assert "datasources_sync" in sched.jobs
    finally:
        sched.shutdown()


# ----------------------------------------------------------------------------------------------------
# Status, Fehler ohne Geheimnisse, Entfernen, Abrufstand
# ----------------------------------------------------------------------------------------------------

def test_errors_are_meaningful_and_free_of_secrets(client, fake, monkeypatch):
    c = client
    sid = create_source(c)
    secret_parts = ("APIKEY-1234567890", "SECRET-abcdefghij")
    fake.fail = K.ConnectorError("auth", "Schlüssel APIKEY-1234567890 hat keine Leserechte")
    sync(c, sid)
    row = source(c, sid)
    assert row["status"] == "error" and row["last_error"].startswith("Zugangsdaten abgelehnt")
    assert all(p not in row["last_error"] for p in secret_parts)

    fake.fail = RuntimeError("boom https://api.kraken.com/0/private/Ledgers?nonce=1&signature=zzz "
                             "key=SECRET-abcdefghij")
    sync(c, sid)
    err = source(c, sid)["last_error"]
    assert "Unerwarteter Fehler (RuntimeError)" in err and "zzz" not in err
    assert all(p not in err for p in secret_parts)

    req = httpx.Request("GET", "https://api.kraken.com/0/private/Balance")
    fake.fail = httpx.HTTPStatusError("x", request=req, response=httpx.Response(429, request=req))
    sync(c, sid)
    assert "HTTP 429" in source(c, sid)["last_error"]
    fake.fail = httpx.ConnectTimeout("timeout", request=req)
    sync(c, sid)
    assert "Zeitüberschreitung" in source(c, sid)["last_error"]

    page = c.get("/settings/datasources").text + c.get(f"/settings/datasources/{sid}").text
    assert "Fehler" in page and "Zeitüberschreitung" in page
    assert all(p not in page for p in secret_parts)
    runs = datasource_service(c.app.state.ctx).runs(sid)
    assert all(r["status"] == "error" for r in runs[:4])
    assert all(p not in (r["message"] or "") for r in runs for p in secret_parts)

    # fehlende Zugangsdaten → klare Meldung
    monkeypatch.delenv(KEY)
    ok, msg = datasource_service(c.app.state.ctx).check(sid)
    assert not ok and "PORTFOLIA_DS_KRAKEN fehlt" in msg
    assert "fehlt" in c.get("/settings/datasources").text

    # Erholung: vollständiger Lauf → synchronisiert, Fehler gelöscht; unvollständig → teilweise
    monkeypatch.setenv(KEY, "neu-1234567890")
    fake.fail = None
    fake.events = [deposit("kraken:L1", "2024-03-01T10:00:00", "EUR", "1000")]
    fake.complete = False
    bid = batch_of(sync(c, sid))
    row = source(c, sid)
    assert row["status"] == "partial" and "unvollständig" in row["last_error"]
    assert "teilweise synchronisiert" in c.get("/settings/datasources").text
    post(c, f"/journal/csv/{bid}/discard")
    fake.complete = True
    sync(c, sid)
    row = source(c, sid)
    assert row["status"] == "synced" and row["last_error"] is None


def test_delete_keeps_bookings_and_reset_cursor_refetches(client, fake):
    c = client
    svc = datasource_service(c.app.state.ctx)
    sid = create_source(c)
    fake.events = [deposit("kraken:L1", "2024-03-01T10:00:00", "EUR", "1000")]
    bid = batch_of(sync(c, sid))
    post(c, f"/journal/csv/{bid}/commit")
    fake.events.append(buy("kraken:T1", "2024-06-01T10:00:00", "500", "0.01"))
    pending = batch_of(sync(c, sid))
    create_unknown_assets(c, pending)

    # Entfernen: Konfiguration und Laufhistorie weg, offener Prüf-Stapel verworfen, übernommene Buchungen bleiben
    post(c, f"/settings/datasources/{sid}/delete")
    assert source(c, sid) == {} and not svc.runs(sid) and csv_service(c.app.state.ctx).batch(pending) is None
    assert csv_service(c.app.state.ctx).batch(bid) is not None
    assert "Datenquelle entfernt" in c.get(f"/journal/csv/{bid}").text
    assert [t["external_id"] for t in journal(c, "source='sync:kraken' AND status='active'")] == ["kraken:L1#0"]

    # neu angelegt: bereits übernommene Vorgänge werden wiedererkannt
    sid2 = create_source(c, name="Kraken neu")
    bid2 = batch_of(sync(c, sid2))
    st = {k: rc.status for k, rc in rows_by_ext(c, bid2).items()}
    assert st == {"kraken:L1#0": "known", "kraken:T1#0": "new"}

    # verworfen → Abrufstand automatisch zurückgesetzt, der nächste Lauf liefert die Vorgänge erneut
    assert source(c, sid2)["cursor_json"]
    post(c, f"/journal/csv/{bid2}/discard")
    assert source(c, sid2)["cursor_json"] is None  # Test-Connector: vollständig neu abrufen
    bid3 = batch_of(sync(c, sid2))
    assert fake.cursors[-1] is None
    assert {k: rc.status for k, rc in rows_by_ext(c, bid3).items()} == st
    assert svc.pending_batch(sid2)["id"] == bid3
    # ausdrücklich zurücksetzen bleibt möglich
    assert "Abrufstand zurücksetzen" in c.get(f"/settings/datasources/{sid2}").text or \
        source(c, sid2)["cursor_json"] is None
    r = post(c, f"/settings/datasources/{sid2}/reset")
    assert r.status_code == 303 and source(c, sid2)["cursor_json"] is None


def test_changing_target_resets_status(client, fake):
    c = client
    sid = create_source(c)
    fake.events = [deposit("kraken:L1", "2024-03-01T10:00:00", "EUR", "1000")]
    batch_of(sync(c, sid))
    assert source(c, sid)["status"] == "synced" and source(c, sid)["cursor_json"]
    post(c, f"/settings/datasources/{sid}", provider="kraken", name="Umbenannt", account="Kraken",
         credential_ref=KEY, sync_interval_min="60")
    assert source(c, sid)["status"] == "synced"  # nur umbenannt
    post(c, f"/settings/datasources/{sid}", provider="kraken", name="Umbenannt", account="Kraken Pro",
         credential_ref=KEY, sync_interval_min="60")
    row = source(c, sid)
    assert (row["status"], row["cursor_json"], row["account"]) == ("created", None, "Kraken Pro")


def test_sync_robustness_lock_retry_after_and_pipeline_errors(client, fake, monkeypatch):
    from app.datasources import service as S

    c = client
    svc = datasource_service(c.app.state.ctx)
    sid = create_source(c, sync_interval_min="60")
    fake.events = [deposit("kraken:L1", "2024-03-01T10:00:00", "EUR", "1000")]

    # ein Lauf zur Zeit je Quelle
    assert S._acquire(sid) is not None
    try:
        assert "läuft bereits" in svc.sync(sid)["error"]
    finally:
        S._release(sid)
    assert fake.cursors == [] and not svc.runs(sid)

    # Drosselung: nächster Lauf frühestens nach der Wartezeit des Anbieters
    fake.fail = K.ConnectorError("rate_limit", "Zu viele Anfragen.", retry_after_s=3 * 3600)
    svc.sync(sid)
    row = source(c, sid)
    assert row["status"] == "error" and "drosselt" in row["last_error"]
    wait = datetime.fromisoformat(row["next_run_at"]) - datetime.fromisoformat(row["last_run_at"])
    assert timedelta(hours=3) <= wait < timedelta(hours=3, minutes=1)

    # Fehler in der Import-Pipeline: Lauf wird als Fehler abgeschlossen, nichts bleibt „läuft“
    fake.fail = None

    def boom(*args, **kwargs):
        raise RuntimeError("Pipeline kaputt")

    monkeypatch.setattr(CsvImportService, "ingest", boom)
    res = svc.sync(sid)
    assert "Unerwarteter Fehler (RuntimeError)" in res["error"]
    assert source(c, sid)["status"] == "error"
    assert {r["status"] for r in svc.runs(sid)} == {"error"}
    assert not S.is_busy(sid)


def test_sanitize_error_keeps_plain_words_after_key_label():
    assert sanitize_error("Kein API-Key hinterlegt – bitte eingeben.") == "Kein API-Key hinterlegt – bitte eingeben."
    assert "Abc123xyz" not in sanitize_error("API-Key Abc123xyz abgelehnt")
    assert "ABCDEFGHIJKLMNOPQR" not in sanitize_error("token ABCDEFGHIJKLMNOPQR")
