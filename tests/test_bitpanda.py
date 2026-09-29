"""Bitpanda-Anbindung (Public API, nur lesend) – mit anonymisierten Fixtures, ohne Netz.

Abgedeckt: verschlüsselte Zugangsdaten (Master-Key, Zugriffsschutz, Wechsel, Entfernen), Connector (Pagination,
Zuordnung, Gebühren, Korrekturen, Decimal, 429, Teilfehler, Fehlerarten), Synchronisierung (historisch,
inkrementell, idempotent, Verwerfen und erneutes Abrufen, dauerhaft ignorieren, automatische Übernahme je
Ereignis), Abgleich mit Bitpanda-CSV und kuratiertem Import, Migration 8.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import re
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.csvimport import model as M
from app.csvimport.service import csv_service
from app.datasources import bitpanda as B
from app.datasources import connector as K
from app.datasources.service import datasource_service
from app.datasources.vault import Vault, VaultError, parse_master_key
from app.db import Database
from app.importer.zipbuilder import build_zip
from app.jobs import tasks
from app.main import build_app
from tests.helpers import ASSETS, tx
from tests.test_csvimport import create_unknown_assets, journal, post, upload

D = Decimal
FIX = Path(__file__).resolve().parent / "data" / "bitpanda"
MASTER = base64.b64encode(bytes(range(32))).decode()
MASTER2 = base64.b64encode(bytes(range(1, 33))).decode()
API_KEY = "bp_test_key_0123456789abcdefABCDEF"
API_KEY2 = "bp_test_key_zyxwvutsrqponmlkjihgf"


def U(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012x}"


# ----------------------------------------------------------------------------------------------------
# Test-API (MockTransport) und App
# ----------------------------------------------------------------------------------------------------

class FakeBitpanda:
    """Antwortet wie die Bitpanda Public API aus den Fixture-Dateien; zeichnet Anfragen auf."""

    def __init__(self) -> None:
        self.pages = {None: "operations_page1.json", "c-page-2": "operations_page2.json",
                      "c-page-3": "operations_page3.json"}
        self.assets = json.loads((FIX / "assets.json").read_text())
        self.calls: list[httpx.Request] = []
        self.fail: dict[str, list[httpx.Response]] = {}  # Pfad/Cursor → vorgegebene Antworten (einmalig)
        self.ops_override: list[Any] | None = None
        self.holdings_status = 200

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        assert req.method == "GET", "nur lesende Aufrufe"
        assert req.url.host == "api.public.bitpanda.com"
        if req.headers.get("x-api-key") not in (API_KEY, API_KEY2):
            return httpx.Response(401, json={"errors": [{"code": "unauthorized", "status": 401,
                                                         "title": "Credentials / Access token wrong"}]})
        path = req.url.path
        key = f"{path}?cursor={req.url.params.get('cursor')}"
        for k in (key, path):
            if self.fail.get(k):
                return self.fail[k].pop(0)
        if path == "/v1/operations":
            if self.ops_override is not None:
                return httpx.Response(200, json={"data": self.ops_override})
            if req.url.params.get("pageSize") == "1":
                return httpx.Response(200, json={"data": [], "cursor": None})
            name = self.pages.get(req.url.params.get("cursor"))
            return httpx.Response(200, content=(FIX / name).read_bytes()) if name else httpx.Response(400)
        if path == "/v1/currencies":
            return httpx.Response(200, content=(FIX / "currencies.json").read_bytes())
        if path == "/v1/assets":
            aid = req.url.params.get("id")
            if aid is None:
                return httpx.Response(200, json={"data": []})
            return httpx.Response(200, json=self.assets[aid]) if aid in self.assets else httpx.Response(404)
        if path == "/v1/portfolio/holdings":
            if self.holdings_status != 200:
                return httpx.Response(self.holdings_status, json={"message": "forbidden"})
            return httpx.Response(200, content=(FIX / "holdings.json").read_bytes())
        return httpx.Response(404)

    def ops_requests(self) -> list[httpx.Request]:
        return [c for c in self.calls if c.url.path == "/v1/operations"]


@pytest.fixture
def api(monkeypatch):
    fake = FakeBitpanda()
    sleeps: list[float] = []
    monkeypatch.setattr(B.BitpandaConnector, "transport", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(B.BitpandaConnector, "sleep", staticmethod(sleeps.append))
    fake.sleeps = sleeps  # type: ignore[attr-defined]
    return fake


@pytest.fixture
def master(monkeypatch):
    monkeypatch.setenv("PORTFOLIA_MASTER_KEY", MASTER)


def make_client(config, **cfg):
    return TestClient(build_app(Config(**{**config.__dict__, **cfg}) if cfg else config, start_scheduler=False))


@pytest.fixture
def client(config, master):
    with make_client(config) as c:
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        yield c


def ctx(c):
    return c.app.state.ctx


def create_bitpanda(c, *, key: str | None = API_KEY, **form) -> int:
    data = {"kind": "exchange", "provider": "bitpanda", "name": "Bitpanda", "account": "Bitpanda",
            "sync_interval_min": "0", **form}
    if key:
        data["api_key"] = key
    r = post(c, "/settings/datasources", **data)
    assert r.status_code == 303, r.text[:800]
    return int(re.search(r"/settings/datasources/(\d+)", r.headers["location"]).group(1))


def sync(c, sid):
    return post(c, f"/settings/datasources/{sid}/sync")


def batch_of(r) -> int:
    m = re.search(r"/journal/csv/(\d+)$", r.headers["location"])
    assert m, r.headers["location"]
    return int(m.group(1))


def rows_by_key(c, bid) -> dict[str, Any]:
    return {rc.rec.ext_id: rc for rc in csv_service(ctx(c)).rows(bid)}


def op_key(n: int) -> str:
    return f"bitpanda:{U(0x100 + n)}"


def source(c, sid) -> dict[str, Any]:
    r = ctx(c).db.q1("SELECT * FROM data_source WHERE id=?", (sid,))
    return dict(r) if r else {}


def db_bytes(c) -> bytes:
    p = Path(ctx(c).db.path)
    out = p.read_bytes()
    wal = Path(str(p) + "-wal")
    return out + (wal.read_bytes() if wal.exists() else b"")


# ----------------------------------------------------------------------------------------------------
# Master-Key und Verschlüsselung
# ----------------------------------------------------------------------------------------------------

def test_vault_roundtrip_binding_and_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("PORTFOLIA_MASTER_KEY", MASTER)
    v = Vault.load()
    assert v.available and v.key_id and MASTER not in repr(v)
    blob, kid = v.encrypt(API_KEY, 7)
    assert API_KEY.encode() not in blob and blob[:4] == b"PFC1" and kid == v.key_id
    assert v.decrypt(blob, 7) == API_KEY
    with pytest.raises(VaultError, match="vertauscht"):
        v.decrypt(blob, 8)  # an Datenquelle 7 gebunden
    blob2, _ = v.encrypt(API_KEY, 7)
    assert blob2 != blob  # zufällige Nonce
    tampered = bytearray(blob)
    tampered[-1] ^= 1
    with pytest.raises(VaultError):
        v.decrypt(bytes(tampered), 7)
    # anderer Master-Key: klare Meldung, kein Absturz
    monkeypatch.setenv("PORTFOLIA_MASTER_KEY", MASTER2)
    with pytest.raises(VaultError, match="anderen Master-Key"):
        Vault.load().decrypt(blob, 7)
    # ohne Master-Key: weder speichern noch lesen
    monkeypatch.delenv("PORTFOLIA_MASTER_KEY")
    v0 = Vault.load()
    assert not v0.available
    with pytest.raises(VaultError, match="Master-Key fehlt"):
        v0.encrypt(API_KEY, 1)
    with pytest.raises(VaultError, match="Master-Key fehlt"):
        v0.decrypt(blob, 7)


def test_master_key_formats_and_file(monkeypatch, tmp_path):
    raw = bytes(range(32))
    assert parse_master_key(raw.hex()) == raw
    assert parse_master_key(base64.b64encode(raw).decode()) == raw
    assert parse_master_key(base64.urlsafe_b64encode(raw).decode().rstrip("=")) == raw
    for bad in ("", "kurz", base64.b64encode(b"x" * 16).decode(), "nicht base64 !!!"):
        with pytest.raises(VaultError):
            parse_master_key(bad)
    f = tmp_path / "master.key"
    f.write_text(MASTER + "\n")
    f.chmod(0o600)
    monkeypatch.setenv("PORTFOLIA_MASTER_KEY_FILE", str(f))
    v = Vault.load()
    assert v.available and "Datei" in v.source and v.note is None
    f.chmod(0o644)
    assert "lesbar" in Vault.load().note
    monkeypatch.setenv("PORTFOLIA_MASTER_KEY_FILE", str(tmp_path / "fehlt.key"))
    v = Vault.load()
    assert not v.available and v.error is None and "noch nicht angelegt" in v.note  # kein Fehler, nur Hinweis
    monkeypatch.setenv("PORTFOLIA_MASTER_KEY_FILE", str(tmp_path))  # Verzeichnis statt Datei
    v = Vault.load()
    assert not v.available and "nicht lesbar" in v.error and str(tmp_path) not in v.error
    monkeypatch.delenv("PORTFOLIA_MASTER_KEY_FILE")
    monkeypatch.setenv("PORTFOLIA_MASTER_KEY", "zu-kurz")
    assert not Vault.load().available and "32" in Vault.load().error


def test_without_master_key_nothing_is_stored(config):
    with make_client(config) as c:
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        page = c.get("/settings/datasources/new?kind=exchange").text
        assert 'name="api_key"' not in page  # ohne Master-Key keine Eingabe im Anlegeformular
        r = post(c, "/settings/datasources", kind="exchange", provider="bitpanda", name="BP", account="BP",
                 sync_interval_min="0", api_key=API_KEY)
        assert r.status_code == 400 and "Master-Key fehlt" in r.text and API_KEY not in r.text
        assert ctx(c).db.scalar("SELECT COUNT(*) FROM data_source") == 0
        sid = create_bitpanda(c, key=None)
        page = c.get(f"/settings/datasources/{sid}").text
        assert "Master-Key fehlt" in page and 'name="api_key"' not in page
        r = post(c, f"/settings/datasources/{sid}/key", api_key=API_KEY)
        assert "Master-Key+fehlt" in r.headers["location"] or "Master-Key%20fehlt" in r.headers["location"]
        assert ctx(c).db.scalar("SELECT COUNT(*) FROM data_source_secret") == 0
        assert API_KEY.encode() not in db_bytes(c)
        res = datasource_service(ctx(c)).sync(sid)
        assert "Kein API-Key" in res["error"]


def test_key_is_encrypted_never_echoed_and_protected(client, api):
    c = client
    r = post(c, "/settings/datasources", kind="exchange", provider="bitpanda", name="Bitpanda", account="Bitpanda",
             sync_interval_min="0", api_key=API_KEY, key_expires_on="2099-12-31")
    assert r.status_code == 303 and API_KEY not in r.headers["location"]
    sid = int(re.search(r"/settings/datasources/(\d+)", r.headers["location"]).group(1))
    row = ctx(c).db.q1("SELECT * FROM data_source_secret WHERE source_id=?", (sid,))
    assert row["hint"] == API_KEY[-4:] and row["key_id"] == Vault.load().key_id
    assert API_KEY.encode() not in bytes(row["ciphertext"])
    assert API_KEY.encode() not in db_bytes(c)  # weder in der Datenbank noch im WAL
    for url in ("/settings/datasources", f"/settings/datasources/{sid}", "/settings", "/journal"):
        page = c.get(url)
        assert page.status_code == 200 and API_KEY not in page.text, url
    detail = c.get(f"/settings/datasources/{sid}").text
    assert f"••••{API_KEY[-4:]}" in detail and "31.12.2099" in detail
    # Fehlermeldungen und Protokolle enthalten den Schlüssel nie
    assert not ctx(c).db.scalar("SELECT COUNT(*) FROM event_log WHERE message LIKE ?", (f"%{API_KEY}%",))
    # CSRF und Zugriffsschutz
    r = c.post(f"/settings/datasources/{sid}/key", data={"api_key": API_KEY2}, follow_redirects=False)
    assert r.status_code == 403
    r = c.post(f"/settings/datasources/{sid}/key/delete", data={}, follow_redirects=False)
    assert r.status_code == 403
    assert ctx(c).db.q1("SELECT hint FROM data_source_secret WHERE source_id=?", (sid,))["hint"] == API_KEY[-4:]
    # ungültige Eingaben: nie zurückgespielt
    for bad in ("zu kurz", "PORTFOLIA_DS_BITPANDA", "mit leerzeichen im schlüssel 123"):
        r = post(c, f"/settings/datasources/{sid}/key", api_key=bad)
        assert "error=" in r.headers["location"] and bad.replace(" ", "+") not in r.headers["location"]


def test_basic_auth_protects_datasource_endpoints(config, master):
    from app.web.security import hash_password

    with make_client(config, auth_mode="basic", auth_user="u", auth_password_hash=hash_password("pw")) as c:
        assert c.get("/settings/datasources").status_code == 401
        r = c.post("/settings/datasources/1/key", data={"api_key": API_KEY}, follow_redirects=False)
        assert r.status_code in (401, 403)
        c.auth = ("u", "pw")
        assert c.get("/settings/datasources").status_code == 200


def test_replace_and_remove_key_keep_bookings(client, api):
    c = client
    sid = create_bitpanda(c)
    bid = batch_of(sync(c, sid))
    create_unknown_assets(c, bid)
    post(c, f"/journal/csv/{bid}/commit")
    n = len(journal(c, "source='sync:bitpanda'"))
    assert n >= 5
    old_blob = bytes(ctx(c).db.q1("SELECT ciphertext FROM data_source_secret WHERE source_id=?", (sid,))["ciphertext"])
    r = post(c, f"/settings/datasources/{sid}/key", api_key=API_KEY2, key_expires_on="2099-01-31")
    assert r.status_code == 303 and "gespeichert" in c.get(r.headers["location"]).text
    row = ctx(c).db.q1("SELECT * FROM data_source_secret WHERE source_id=?", (sid,))
    assert row["hint"] == API_KEY2[-4:] and bytes(row["ciphertext"]) != old_blob
    assert source(c, sid)["status"] == "created" and source(c, sid)["key_expires_on"] == "2099-01-31"
    assert len(journal(c, "source='sync:bitpanda'")) == n
    # entfernen: sicher gelöscht, Buchungen bleiben, Abruf braucht neuen Schlüssel
    blob = bytes(row["ciphertext"])
    post(c, f"/settings/datasources/{sid}/key/delete")
    assert ctx(c).db.scalar("SELECT COUNT(*) FROM data_source_secret") == 0
    assert blob not in db_bytes(c) and API_KEY2.encode() not in db_bytes(c)
    assert len(journal(c, "source='sync:bitpanda'")) == n
    res = datasource_service(ctx(c)).sync(sid)
    assert "Kein API-Key" in res["error"]


def test_rotation_with_old_master_key(client, api, monkeypatch):
    c = client
    sid = create_bitpanda(c)
    svc = datasource_service(ctx(c))
    old_id = Vault.load().key_id
    monkeypatch.setenv("PORTFOLIA_MASTER_KEY", MASTER2)
    assert "anderen Master-Key" in svc.check(sid)[1]  # ohne alten Key nicht lesbar
    assert svc.key_stats()["stale"] == 1
    monkeypatch.setenv("PORTFOLIA_MASTER_KEY_OLD", MASTER)
    ok, _ = svc.check(sid)  # mit altem Key lesbar
    assert ok
    r = post(c, "/settings/datasources/keys/rotate")
    assert r.status_code == 303 and "1+Schl" in r.headers["location"]
    assert ctx(c).db.scalar("SELECT key_id FROM data_source_secret") == Vault.load().key_id != old_id
    monkeypatch.delenv("PORTFOLIA_MASTER_KEY_OLD")
    assert svc.check(sid)[0] and svc.key_stats()["stale"] == 0


def test_env_var_credentials_still_work(client, api, monkeypatch):
    c = client
    monkeypatch.setenv("PORTFOLIA_DS_BITPANDA", API_KEY)
    sid = create_bitpanda(c, key=None, credential_ref="PORTFOLIA_DS_BITPANDA")
    ok, msg = datasource_service(ctx(c)).check(sid)
    assert ok and "Vorgänge lesbar" in msg
    assert "Umgebungsvariable" in c.get(f"/settings/datasources/{sid}").text
    # ein in der App gespeicherter Schlüssel hat Vorrang
    post(c, f"/settings/datasources/{sid}/key", api_key=API_KEY2)
    datasource_service(ctx(c)).check(sid)
    assert api.calls[-1].headers["x-api-key"] == API_KEY2


def test_delete_source_removes_key_and_recreate_does_not_duplicate(client, api):
    c = client
    sid = create_bitpanda(c)
    bid = batch_of(sync(c, sid))
    create_unknown_assets(c, bid)
    post(c, f"/journal/csv/{bid}/commit")
    n = len(journal(c, "source='sync:bitpanda' AND status='active'"))
    blob = bytes(ctx(c).db.q1("SELECT ciphertext FROM data_source_secret")["ciphertext"])
    post(c, f"/settings/datasources/{sid}/delete")
    assert ctx(c).db.scalar("SELECT COUNT(*) FROM data_source_secret") == 0
    assert blob not in db_bytes(c) and API_KEY.encode() not in db_bytes(c)
    sid2 = create_bitpanda(c, name="Bitpanda neu")
    r = sync(c, sid2)
    assert len(journal(c, "source='sync:bitpanda' AND status='active'")) == n
    loc = r.headers["location"]
    if "/journal/csv/" in loc:  # nur ungeklärte Vorgänge offen – nichts Übernommenes erneut
        assert {rc.status for rc in rows_by_key(c, batch_of(r)).values()} <= {"unclear", "known"}


# ----------------------------------------------------------------------------------------------------
# Connector: Pagination, Zuordnung, Fehler
# ----------------------------------------------------------------------------------------------------

def fetch(cursor=None, key=API_KEY) -> K.FetchResult:
    conn = B.BitpandaConnector()
    return conn.fetch(K.SourceConfig(1, "exchange", "bitpanda", "BP", "Bitpanda"), K.Secret(value=key), cursor)


def by_event(res: K.FetchResult) -> dict[str, K.SourceEvent]:
    return {ev.event_key: ev for ev in res.events}


def test_pagination_and_mapping(api):
    res = fetch()
    assert res.complete and res.cursor and res.cursor["from"]
    ops = api.ops_requests()
    assert [r.url.params.get("cursor") for r in ops] == [None, "c-page-2", "c-page-3"]
    assert all(r.url.params.get("pageSize") == "100" and "from" not in r.url.params for r in ops)
    assert res.coverage["pages"] == 3 and res.coverage["operations"] == 12 and res.coverage["mode"] == "vollständig"
    ev = by_event(res)
    buy = ev[op_key(1)].lines
    assert len(buy) == 1 and (buy[0].kind, buy[0].out_sym, buy[0].out_qty, buy[0].in_sym, buy[0].in_qty) == \
        (M.TRADE, "EUR", D("250.00"), "BTC", D("0.00550000"))
    assert f"bitpanda:{U(0x2001)}" in buy[0].aliases and buy[0].review is None
    assert buy[0].raw["transactions"][0]["amount"] == "250.00"  # Originaldaten zur Nachprüfung
    multi = ev[op_key(2)].lines  # Kauf + Gebühr in drittem Asset → zwei Zeilen in fester Reihenfolge
    assert [(r.kind, r.in_sym, r.fee_sym, r.fee_qty) for r in multi] == \
        [(M.TRADE, "ETH", None, None), (M.FEE, None, "BEST", D("0.25000000"))]
    assert ev[op_key(3)].lines[0].kind == M.DEPOSIT and ev[op_key(3)].lines[0].in_sym == "EUR"
    wd = ev[op_key(4)].lines[0]
    assert (wd.kind, wd.out_qty, wd.fee_sym, wd.fee_qty) == (M.WITHDRAWAL, D("0.00100000"), "BTC", D("0.00005000"))
    assert "Gebühr" in wd.review  # Brutto/Netto nicht dokumentiert → nie automatisch
    reward = ev[op_key(5)].lines[0]
    assert (reward.kind, reward.tag, reward.in_sym) == (M.DEPOSIT, "reward", "ETH")
    assert op_key(6) not in ev and res.skipped == {"Bitpanda: interne Umbuchung zwischen Bitpanda-Wallets": 1}
    for n, text in ((7, "Tausch"), (8, "Aktie/ETF"), (9, "storniert"), (10, "Korrektur/Storno"),
                    (11, "MYSTERY_EVENT")):
        line = ev[op_key(n)].lines[0]
        assert line.kind == M.REVIEW and text in line.note, (n, line.note)
    sell = ev[op_key(12)].lines[0]
    assert (sell.out_sym, sell.in_sym, sell.in_qty) == ("BTC", "EUR", D("98.12345678"))
    assert isinstance(sell.in_qty, Decimal)
    # Plausibilität: Bestand laut Bitpanda weicht ab → nur Hinweis
    assert res.coverage["balances"]["checked"] and res.coverage["balances"]["differences"] >= 1
    assert any("Bestandsprüfung" in w for w in res.warnings)


def test_decimal_precision_and_unknown_structures(api):
    api.ops_override = [
        {"id": U(0x901), "type": "BUY", "timestamp": "2024-03-01T10:00:00+01:00", "transactions": [
            {"transaction_id": U(0x951), "flow": "OUTGOING", "amount": "__NUM__", "currency_id": U(0xE0)},
            {"transaction_id": U(0x952), "flow": "INCOMING", "amount": "0.123456789012345678", "asset_id": U(0xB1)}]},
        {"id": U(0x902), "type": "BUY", "timestamp": "2024-03-01T11:00:00Z", "transactions": [
            {"transaction_id": U(0x953), "amount": "5", "currency_id": U(0xE0)}]},  # ohne Richtung
        {"id": U(0x903), "type": "DEPOSIT", "transactions": [
            {"transaction_id": U(0x954), "flow": "INCOMING", "amount": "5", "currency_id": U(0xE0)}]},  # ohne Zeit
        {"id": U(0x904), "type": "BUY", "timestamp": "2024-03-02T00:00:00Z", "transactions": [
            {"transaction_id": U(0x955), "flow": "OUTGOING", "amount": "5", "currency_id": U(0xE0)},
            {"transaction_id": U(0x956), "flow": "INCOMING", "amount": "1", "asset_id": U(0xDEAD)}]},  # Asset fehlt
    ]
    raw = json.dumps({"data": api.ops_override}).replace('"__NUM__"', "1000.123456789012345678")  # JSON-Zahl
    api.ops_override = None
    api.fail["/v1/operations"] = [httpx.Response(200, content=raw.encode())]
    res = fetch()
    ev = by_event(res)
    buy = ev[f"bitpanda:{U(0x901)}"].lines[0]
    assert buy.in_qty == D("0.123456789012345678") and buy.out_qty == D("1000.123456789012345678")
    assert buy.ts.isoformat() == "2024-03-01T09:00:00+00:00"  # UTC
    assert "Richtung" in ev[f"bitpanda:{U(0x902)}"].lines[0].note
    assert "Zeitpunkt" in ev[f"bitpanda:{U(0x903)}"].lines[0].note
    assert "unbekanntes Asset" in ev[f"bitpanda:{U(0x904)}"].lines[0].note
    assert res.complete  # Seite mit 4 Einträgen und ohne Cursor: Ende erkannt


def test_incremental_fetch_uses_time_filter_and_overlap(api):
    res = fetch({"v": 1, "from": "2024-02-18T00:00:00Z"})
    first = api.ops_requests()[0]
    assert first.url.params.get("from") == "2024-02-18T00:00:00Z" and res.coverage["mode"] == "inkrementell"
    assert "balances" not in res.coverage  # Bestandsprüfung nur bei vollständiger Historie
    conn = B.BitpandaConnector()
    from datetime import UTC, datetime

    rw = conn.rewind({"v": 1, "from": "2024-03-10T00:00:00Z"}, datetime(2024, 2, 20, tzinfo=UTC))
    assert rw["from"] == "2024-02-18T00:00:00Z"  # vor den verworfenen Vorgang (mit Überlappung)
    assert conn.rewind(None, datetime(2024, 2, 20, tzinfo=UTC)) is None


def test_rate_limit_waits_and_partial_on_exhaustion(api):
    api.fail["/v1/operations?cursor=c-page-2"] = [httpx.Response(429, headers={"Retry-After": "3"})]
    res = fetch()
    assert res.complete and api.sleeps == [3.0] and res.coverage["throttled"] == 1
    api.sleeps.clear()
    api.fail["/v1/operations?cursor=c-page-2"] = [httpx.Response(429, headers={"Retry-After": "30"})] * 10
    res = fetch()
    assert not res.complete and res.cursor is None  # Abrufstand rückt nicht vor
    assert any("abgebrochen" in w for w in res.warnings)
    assert op_key(1) in by_event(res) and op_key(12) not in by_event(res)  # Seite 1 bleibt nutzbar
    assert sum(api.sleeps) <= B.WAIT_BUDGET_S
    api.sleeps.clear()
    api.fail["/v1/operations?cursor=c-page-2"] = [httpx.Response(429, headers={"Retry-After": "900"})]
    res = fetch()  # längere Wartezeit als erlaubt: nicht verkürzt erneut fragen, sondern abbrechen
    assert not res.complete and api.sleeps == [] and any("abgebrochen" in w for w in res.warnings)
    api.fail["/v1/operations"] = [httpx.Response(429, headers={"Retry-After": "900"})]
    with pytest.raises(K.ConnectorError) as ei:
        fetch()
    assert ei.value.kind == "rate_limit" and ei.value.retry_after_s == 900 and api.sleeps == []


def test_unclear_pagination_and_cursor_loops_are_incomplete(api):
    api.pages = {None: "operations_page1.json", "c-page-2": "operations_page1.json"}  # Cursor wiederholt sich
    res = fetch()
    assert not res.complete and any("wiederholt" in w for w in res.warnings)
    api.pages = {None: "operations_page1.json"}
    full = [{"id": U(0x800 + i), "type": "DEPOSIT", "timestamp": "2024-01-01T00:00:00Z",
             "transactions": [{"transaction_id": U(0x880 + i), "flow": "INCOMING", "amount": "1",
                               "currency_id": U(0xE0)}]} for i in range(25)]
    api.ops_override = full  # volle Standardseite ohne Cursor: Ende nicht eindeutig
    res = fetch()
    assert not res.complete and any("Seitenende" in w for w in res.warnings)


def test_bad_parameter_falls_back_without_page_size(api):
    api.fail["/v1/operations?cursor=None"] = [httpx.Response(400, json={"message": "unknown parameter pageSize"})]
    res = fetch()
    ops = api.ops_requests()
    assert ops[0].url.params.get("pageSize") == "100" and "pageSize" not in ops[1].url.params
    assert res.complete


def test_errors_are_classified(api, monkeypatch):
    conn = B.BitpandaConnector()
    cfg = K.SourceConfig(1, "exchange", "bitpanda", "BP", "Bitpanda")
    res = conn.check(cfg, K.Secret(value="bp_wrong_key_0000000000000000"))
    assert not res.ok and "ungültig" in res.message and not res.details["transaction"]["ok"]
    # gültiger Schlüssel, aber ohne Leserecht „Transaction“: Bestände lesbar, Vorgänge 401
    api.fail["/v1/operations"] = [httpx.Response(401, json={"errors": [{"code": "unauthorized"}]})] * 2
    res = conn.check(cfg, K.Secret(value=API_KEY))
    assert not res.ok and "Leserecht „Transaction“" in res.message and res.details["balances"]["ok"]
    api.fail["/v1/operations"] = [httpx.Response(403, json={"message": "insufficient scope"})] * 2
    res = conn.check(cfg, K.Secret(value=API_KEY))
    assert not res.ok and "Transaction" in res.message
    api.fail["/v1/operations"] = [httpx.Response(401, json={"errors": [{"code": "api_key_expired",
                                                                         "title": "API key expired"}]})] * 2
    res = conn.check(cfg, K.Secret(value=API_KEY))
    assert not res.ok and "abgelaufen" in res.message
    api.fail["/v1/operations"] = [httpx.Response(503)] * 5
    with pytest.raises(K.ConnectorError) as e:
        conn.check(cfg, K.Secret(value=API_KEY))
    assert e.value.kind == "unavailable"

    def timeout(req):
        raise httpx.ConnectTimeout("timeout", request=req)

    monkeypatch.setattr(B.BitpandaConnector, "transport", httpx.MockTransport(timeout))
    with pytest.raises(K.ConnectorError) as e:
        fetch()
    assert e.value.kind == "unavailable" and "Zeitüberschreitung" in e.value.message
    # Umleitungen werden nicht verfolgt (Schlüssel nur an den dokumentierten Host)
    monkeypatch.setattr(B.BitpandaConnector, "transport", httpx.MockTransport(
        lambda req: httpx.Response(302, headers={"Location": "https://evil.example/x"})))
    with pytest.raises(K.ConnectorError) as e:
        fetch()
    assert "Umleitung" not in e.value.message or "nicht gefolgt" in e.value.message


def test_check_reports_scopes(client, api):
    c = client
    sid = create_bitpanda(c)
    api.holdings_status = 403
    r = post(c, f"/settings/datasources/{sid}/check")
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    page = c.get(r.headers["location"]).text
    assert "Vorgänge lesbar" in page and "Bestände" in page and "verbunden" in page
    chk = json.loads(source(c, sid)["last_check_json"])
    assert chk["ok"] and chk["details"]["transaction"]["ok"] and not chk["details"]["balances"]["ok"]
    assert API_KEY not in page


# ----------------------------------------------------------------------------------------------------
# Synchronisieren: historisch, inkrementell, Verwerfen, Ignorieren, automatische Übernahme
# ----------------------------------------------------------------------------------------------------

def test_historical_then_incremental_sync(client, api):
    c = client
    sid = create_bitpanda(c)
    r = sync(c, sid)
    bid = batch_of(r)
    create_unknown_assets(c, bid)
    rs = rows_by_key(c, bid)
    st = {k: rc.status for k, rc in rs.items()}
    assert st[f"{op_key(1)}#0"] == "new" and st[f"{op_key(2)}#0"] == "new"
    assert st[f"{op_key(2)}#1"] == "invalid"  # BEST-Gebühr ohne Kurs: EUR-Wert fehlt
    assert "EUR-Wert fehlt" in rs[f"{op_key(2)}#1"].errors[0]
    assert st[f"{op_key(5)}#0"] == "new" and "Kurs" in rs[f"{op_key(5)}#0"].value_src  # Kurs aus dem ETH-Kauf
    for n in (7, 8, 9, 10, 11):
        assert st[f"{op_key(n)}#0"] == "unclear"
    assert "Bitpanda: interne Umbuchung zwischen Bitpanda-Wallets" in json.loads(
        csv_service(ctx(c)).batch(bid)["summary_json"])["skipped"]
    page = c.get(f"/journal/csv/{bid}").text
    assert "ungeklärt" in page and "Tausch Krypto" in page and "dauerhaft ignorieren" in page
    run = datasource_service(ctx(c)).runs(sid)[0]
    assert run["rows_unclear"] == 5 and "ungeklärt 5" in run["message"]
    r = post(c, f"/journal/csv/{bid}/commit")
    assert r.status_code == 303
    txs = journal(c, "source='sync:bitpanda'")
    assert {t["event_key"] for t in txs} >= {op_key(1), op_key(2), op_key(3), op_key(12)}
    assert csv_service(ctx(c)).batch(bid)["status"] == "partial"  # ungeklärte Vorgänge offen
    assert json.loads(source(c, sid)["cursor_json"])["from"]
    # zweiter Lauf: Zeitfilter mit Überlappung; alles bekannt oder wartet → kein neuer Stapel
    n_calls = len(api.ops_requests())
    r = sync(c, sid)
    assert "/settings/datasources" in r.headers["location"]
    assert api.ops_requests()[n_calls].url.params.get("from")
    assert len(journal(c, "source='sync:bitpanda'")) == len(txs)
    assert csv_service(ctx(c)).batch(bid) is not None
    assert "wartet bereits auf Pr" in datasource_service(ctx(c)).runs(sid)[0]["message"]


def test_discard_makes_events_refetchable(client, api):
    c = client
    sid = create_bitpanda(c)
    bid = batch_of(sync(c, sid))
    cur = json.loads(source(c, sid)["cursor_json"])
    assert cur["from"] > "2026"
    assert post(c, f"/journal/csv/{bid}/discard").status_code == 303
    cur2 = json.loads(source(c, sid)["cursor_json"] or "null")
    assert cur2 is None or cur2["from"] <= "2024-02-01T00:00:00Z"  # vor den ältesten verworfenen Vorgang
    bid2 = batch_of(sync(c, sid))
    assert f"{op_key(1)}#0" in rows_by_key(c, bid2)  # erneut abgerufen


def test_ignore_decision_persists_across_runs(client, api):
    c = client
    sid = create_bitpanda(c)
    bid = batch_of(sync(c, sid))
    create_unknown_assets(c, bid)
    rs = rows_by_key(c, bid)
    post(c, f"/journal/csv/{bid}/rows", **{f"val_{rs[f'{op_key(2)}#1'].idx}": "0,40"})
    r = post(c, f"/journal/csv/{bid}/ignore", ignore=op_key(11))
    assert r.status_code == 303
    rs = rows_by_key(c, bid)
    assert rs[f"{op_key(11)}#0"].status == "ignored" and "dauerhaft ignoriert" in rs[f"{op_key(11)}#0"].warnings[0]
    assert ctx(c).db.scalar("SELECT decision FROM event_decision WHERE event_key=?", (op_key(11),)) == "ignore"
    post(c, f"/journal/csv/{bid}/commit")
    for n in (7, 8, 9, 10):
        post(c, f"/journal/csv/{bid}/ignore", ignore=op_key(n))
    assert csv_service(ctx(c)).batch(bid)["status"] == "committed"  # nichts mehr offen
    # vollständig neu abrufen: ignorierte erscheinen nicht erneut zur Prüfung, übernommene sind bekannt
    post(c, f"/settings/datasources/{sid}/reset")
    r = sync(c, sid)
    assert "/settings/datasources" in r.headers["location"], r.headers["location"]
    assert datasource_service(ctx(c)).open_counts(sid) == {}
    # Entscheidung aufheben
    post(c, f"/journal/csv/{bid}/ignore", release=op_key(11))
    assert not ctx(c).db.scalar("SELECT COUNT(*) FROM event_decision WHERE event_key=?", (op_key(11),))


def test_auto_commit_takes_only_unambiguous_events(client, api):
    c = client
    sid = create_bitpanda(c, auto_commit="1")
    res = datasource_service(ctx(c)).sync(sid)  # nur die Euro-Einzahlung ist ohne Zuordnung eindeutig
    bid = res["batch_id"]
    assert {t["event_key"] for t in journal(c, "source='sync:bitpanda'")} == {op_key(3)}
    assert csv_service(ctx(c)).batch(bid)["status"] == "partial"  # Rest wartet auf Prüfung
    create_unknown_assets(c, bid)  # Nutzer ordnet Assets zu – übernimmt aber nicht selbst
    res2 = datasource_service(ctx(c)).sync(sid)  # nächster Lauf übernimmt, was jetzt eindeutig ist
    keys = {t["event_key"] for t in journal(c, "source='sync:bitpanda'")}
    assert keys == {op_key(1), op_key(3), op_key(5), op_key(12)}
    assert op_key(4) not in keys  # Gebühr mit unklarer Konvention → Prüfung
    assert op_key(2) not in keys  # Gebührenzeile ohne EUR-Wert → ganzes Ereignis bleibt offen
    rs = rows_by_key(c, bid)
    assert rs[f"{op_key(4)}#0"].status == "new" and rs[f"{op_key(7)}#0"].status == "unclear"
    assert rs[f"{op_key(2)}#0"].status == "new" and rs[f"{op_key(2)}#1"].status == "invalid"
    assert res2["committed"] == 3 and res2.get("batch_id") is None


def test_partial_failure_keeps_cursor_and_marks_partial(client, api):
    c = client
    sid = create_bitpanda(c)
    api.fail["/v1/operations?cursor=c-page-2"] = [httpx.Response(429, headers={"Retry-After": "30"})] * 10
    bid = batch_of(sync(c, sid))
    row = source(c, sid)
    assert row["status"] == "partial" and row["cursor_json"] is None and "unvollständig" in row["last_error"]
    assert f"{op_key(1)}#0" in rows_by_key(c, bid)
    assert "teilweise synchronisiert" in c.get("/settings/datasources").text
    assert "unvollständig" in c.get(f"/settings/datasources/{sid}").text


def test_expired_key_date_blocks_requests(client, api):
    c = client
    sid = create_bitpanda(c, key_expires_on="2020-01-01")
    res = datasource_service(ctx(c)).sync(sid)
    assert res["kind"] == "expired" and "abgelaufen" in res["error"] and not api.calls
    assert "abgelaufen" in c.get("/settings/datasources").text


def test_scheduled_sync_runs_without_blocking_on_open_review(client, api):
    c = client
    svc = datasource_service(ctx(c))
    sid = create_bitpanda(c, sync_interval_min="60")
    batch_of(sync(c, sid))
    from datetime import UTC, datetime, timedelta

    assert [d.id for d in svc.due(datetime.now(UTC) + timedelta(minutes=61))] == [sid]  # offene Prüfung blockiert nicht


# ----------------------------------------------------------------------------------------------------
# Abgleich mit Bitpanda-CSV und kuratiertem Import
# ----------------------------------------------------------------------------------------------------

BP_CSV_HEAD = ('"Transaction ID",Timestamp,"Transaction Type",In/Out,"Amount Fiat",Fiat,"Amount Asset",Asset,'
               '"Asset market price","Asset market price currency","Asset class","Product ID",Fee,"Fee asset",'
               'Spread,"Spread Currency","Tax Fiat"\n')


def test_csv_booking_with_same_id_is_known_and_similar_is_candidate(client, api):
    c = client
    csv = (BP_CSV_HEAD
           + f"T{U(0x2001)},2024-02-02T10:00:00+01:00,buy,outgoing,250.00,EUR,0.0055,BTC,-,-,Cryptocurrency,1,"
             "-,-,-,-,0.00\n"
           # gleicher Verkauf ohne passende ID (z. B. älteres Exportformat) → nur Kandidat
           + "T-legacy-1,2024-02-23T01:00:00+01:00,sell,incoming,98.12,EUR,0.002,BTC,-,-,Cryptocurrency,1,"
             "-,-,-,-,0.00\n")
    b_csv, _ = upload(c, "bitpanda.csv", csv.encode(), account="Bitpanda")
    create_unknown_assets(c, b_csv)
    post(c, f"/journal/csv/{b_csv}/commit")
    assert len(journal(c, "source='csv:bitpanda'")) == 2
    sid = create_bitpanda(c)
    bid = batch_of(sync(c, sid))
    create_unknown_assets(c, bid)
    rs = rows_by_key(c, bid)
    known = rs[f"{op_key(1)}#0"]
    assert known.status == "known" and "gleiche Anbieter-ID" in known.warnings[0]
    sell = rs[f"{op_key(12)}#0"]
    assert sell.status == "duplicate" and not sell.include()  # unsicher → Entscheidung, nicht still
    post(c, f"/journal/csv/{bid}/commit")
    assert not journal(c, f"source='sync:bitpanda' AND event_key='{op_key(1)}'")  # nicht doppelt gebucht
    # umgekehrt: CSV nach der Synchronisierung erkennt die API-Buchung (Alias Trade-ID)
    csv2 = (BP_CSV_HEAD + f"T{U(0x2002)},2024-02-05T11:30:00+01:00,buy,outgoing,100.00,EUR,0.03,ETH,-,-,"
            "Cryptocurrency,2,-,-,-,-,0.00\n")
    b2, _ = upload(c, "bitpanda2.csv", csv2.encode(), account="Bitpanda")
    (row,) = csv_service(ctx(c)).rows(b2)
    assert row.status == "known" and "Datenquelle" in row.warnings[0]


def _import_zip(config, rows, valuation="2024-03-01"):
    dst = config.import_dir / "imp.zip"
    build_zip(dst, transactions=rows, assets=ASSETS, generated_at="2024-03-02T00:00:00Z", valuation_date=valuation)
    old = time.time() - 3600
    os.utime(dst, (old, old))


def test_curated_import_covers_app_bookings(config, master, api):
    with make_client(config) as c:
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        sid = create_bitpanda(c)
        bid = batch_of(sync(c, sid))
        create_unknown_assets(c, bid)
        post(c, f"/journal/csv/{bid}/commit")
        buy = journal(c, f"event_key='{op_key(1)}'")[0]
        sell = journal(c, f"event_key='{op_key(12)}'")[0]
        # neuer kuratierter Import enthält den Kauf mit Bitpanda-ID und den Verkauf ohne ID (z. B. über Koinly)
        r1 = tx("IMP-1", "2024-02-02T09:00:00Z", "buy", frm=("Bitpanda", "EUR", "250"),
                to=("Bitpanda", "BTC", "0.0055"), value="250")
        r1["source"], r1["source_ref"] = "bitpanda", f"T{U(0x2001)}"
        r2 = tx("IMP-2", "2024-02-23T00:00:00Z", "sell", frm=("Bitpanda", "BTC", "0.002"),
                to=("Bitpanda", "EUR", "98.12345678"), value="98.12")
        r2["source"], r2["source_ref"] = "koinly", "ABC123"
        _import_zip(config, [r1, r2])
        assert tasks.import_check(ctx(c), "test").status == "imported"
        pf = ctx(c).recorded_portfolio()
        ids = {t.tx_id for t in pf.txs}
        assert "IMP-1" in ids and buy["tx_id"] not in ids  # exakt: Import gilt, App-Buchung zählt nicht
        assert sell["tx_id"] in ids  # unsicher: zählt, bis entschieden ist
        page = c.get("/journal/abgleich").text
        assert "gleiche Anbieter-ID" in page and buy["tx_id"] in page and "IMP-2" in page and sell["tx_id"] in page
        r = post(c, "/journal/abgleich", journal_tx_id=sell["tx_id"], import_tx_id="IMP-2", decision="covered")
        assert r.status_code == 303
        assert sell["tx_id"] not in {t.tx_id for t in ctx(c).recorded_portfolio().txs}
        assert journal(c, f"tx_id='{sell['tx_id']}'")[0]["status"] == "active"  # Herkunft bleibt erhalten
        post(c, "/journal/abgleich", journal_tx_id=sell["tx_id"], import_tx_id="IMP-2", decision="undo")
        assert sell["tx_id"] in {t.tx_id for t in ctx(c).recorded_portfolio().txs}
        post(c, "/journal/abgleich", journal_tx_id=sell["tx_id"], import_tx_id="IMP-2", decision="distinct")
        assert "Keine möglichen Doppelzählungen" in c.get("/journal/abgleich").text


# ----------------------------------------------------------------------------------------------------
# Migration 8
# ----------------------------------------------------------------------------------------------------

def test_migration_8_keeps_data(tmp_path):
    d = Database(tmp_path / "app.sqlite")
    d.migrate(target=7)
    stamp = "2026-01-01T00:00:00Z"
    d.x("INSERT INTO data_source(kind, provider, name, account, status, created_at, updated_at) VALUES "
        "('exchange', 'kraken', 'K', 'K', 'synced', ?, ?)", (stamp, stamp))
    d.x("INSERT INTO data_source_run(source_id, trigger, started_at, status, rows_new) "
        "VALUES (1, 'manual', ?, 'ok', 3)", (stamp,))
    d.x("INSERT INTO csv_batch(filename, file_sha256, file_size, raw_gz, profile, account, status, created_at, "
        "updated_at) VALUES ('x', 'x', 1, ?, 'bitpanda', 'B', 'committed', ?, ?)", (gzip.compress(b"x"), stamp, stamp))
    before = [dict(r) for r in d.q("SELECT * FROM data_source")]
    d.migrate()
    assert d.scalar("PRAGMA user_version") == 8
    row = dict(d.q1("SELECT * FROM data_source"))
    assert {k: row[k] for k in before[0]} == before[0]
    assert row["key_expires_on"] is None and row["coverage_json"] is None
    run = d.q1("SELECT * FROM data_source_run")
    assert (run["rows_new"], run["rows_unclear"], run["rows_ignored"]) == (3, 0, 0)
    tables = {r["name"] for r in d.q("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"data_source_secret", "event_decision", "journal_event_alias", "journal_import_link",
            "ds_asset_cache"} <= tables
    d.migrate()
    assert d.scalar("PRAGMA user_version") == 8
