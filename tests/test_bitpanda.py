"""Bitpanda-Anbindung (Public API, nur lesend) – mit synthetischen Fixtures im Format der offiziellen Referenz.

Der Mock (``FakeBitpanda``) bildet den dokumentierten Vertrag ab und lehnt ab, was er nicht kennt: nicht
dokumentierte Parameter (z. B. ``pageSize``), Endpunkte (z. B. ``/portfolio/holdings``) und Cursor, die er nie
ausgegeben hat. Abgedeckt: verschlüsselte Zugangsdaten, Pagination laut ``has_next_page``/``next_cursor``,
Zeitpunkt nur aus ``transactions[].credited_at`` (nie ergänzt), Betragsobjekte, Gebühren und ``trade``, Bestände aus
``/portfolio`` (``balance.value``), Ersetzen älterer Prüfzeilen, Abgrenzung je Datenquelle, vollständiger Neuabruf
und inkrementeller Abruf ohne Dubletten, Abgleich mit Bitpanda-CSV und kuratiertem Import, Migration 8.
"""

from __future__ import annotations

import base64
import copy
import gzip
import itertools
import json
import os
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.csvimport import model as M
from app.csvimport.model import Rec
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


EUR_ID, BTC_ID, ETH_ID, BEST_ID = U(0xE0), U(0xB1), U(0xE1), U(0xBE)


def load(name: str) -> Any:
    return json.loads((FIX / name).read_text())


# ----------------------------------------------------------------------------------------------------
# Test-API (MockTransport) laut Referenz und App
# ----------------------------------------------------------------------------------------------------

DOC_PARAMS = {"/v1/operations": {"cursor", "page_size", "from", "to", "asset_id", "currency_id"},
              "/v1/assets": {"cursor", "page_size", "isin", "type", "group", "symbol", "id"},
              "/v1/currencies": {"id"},
              "/v1/portfolio": {"equivalent_currency_id", "currency_id", "asset_id"}}
NEEDS_KEY = {"/v1/operations", "/v1/portfolio"}


def _err(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code}})


def _dt(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")) if s else None
    except ValueError:
        return None


def _op_time(o: dict[str, Any]) -> datetime | None:
    times = [t for t in (_dt(x.get("credited_at")) for x in o.get("transactions") or []) if t]
    return min(times) if times else None


class FakeBitpanda:
    """Bitpanda Public API laut Referenz: Seiten je Cursor (``None`` = erste Seite), ``page_size`` kürzt eine Seite,
    ``from`` filtert nach ``credited_at``. Unbekannte Parameter, Endpunkte und Cursor → Fehler wie die echte API."""

    def __init__(self) -> None:
        self.pages: dict[str | None, Any] = {None: load("operations_page1.json"), "c-2": load("operations_page2.json"),
                                             "c-3": load("operations_page3.json")}
        self.assets = {a["id"]: a for a in load("assets.json")["data"]}
        self.currencies = load("currencies.json")
        self.portfolio: Any = load("portfolio.json")
        self.portfolio_status = 200
        self.max_page_size = 100
        self.calls: list[httpx.Request] = []
        self.fail: dict[str, list[httpx.Response]] = {}  # Pfad bzw. Pfad?cursor=… → vorgegebene Antworten (einmalig)

    def set_pages(self, *bodies: Any) -> None:
        """Seiten in Folge: erste ohne Cursor, weitere unter ihrem ``next_cursor`` der Vorseite."""
        self.pages = {None: bodies[0]}
        for prev, body in itertools.pairwise(bodies):
            self.pages[prev["next_cursor"]] = body

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        assert req.method == "GET", "nur lesende Aufrufe"
        assert req.url.scheme == "https" and req.url.host == "api.public.bitpanda.com"
        path = req.url.path
        if path not in DOC_PARAMS:
            return _err(404, "NOT_FOUND")
        if path in NEEDS_KEY and req.headers.get("x-api-key") not in (API_KEY, API_KEY2):
            return _err(401, "UNAUTHORIZED")
        params = req.url.params
        if set(params.keys()) - DOC_PARAMS[path]:
            return _err(400, "INVALID_QUERY_PARAMETERS")
        ps = params.get("page_size")
        if ps is not None and (not ps.isdigit() or not 1 <= int(ps) <= self.max_page_size):
            return _err(400, "INVALID_PAGE_SIZE")
        if any(k in params and _dt(params[k]) is None for k in ("from", "to")):
            return _err(400, "INVALID_DATE")
        for k in (f"{path}?cursor={params.get('cursor')}", path):
            if self.fail.get(k):
                return self.fail[k].pop(0)
        if path == "/v1/operations":
            return self._operations(params)
        if path == "/v1/assets":
            return self._assets(params)
        if path == "/v1/currencies":
            return httpx.Response(200, json=self.currencies)
        if self.portfolio_status != 200:
            return _err(self.portfolio_status, "FORBIDDEN")
        return httpx.Response(200, json=self.portfolio)

    def _operations(self, params: httpx.QueryParams) -> httpx.Response:
        cursor = params.get("cursor")
        if cursor not in self.pages:
            return _err(400, "INVALID_CURSOR")
        body = copy.deepcopy(self.pages[cursor])
        if isinstance(body, dict) and isinstance(body.get("data"), list):
            since = _dt(params.get("from"))
            if since is not None:
                body["data"] = [o for o in body["data"] if (_op_time(o) or since.replace(year=1)) >= since]
            if params.get("page_size"):
                body["data"] = body["data"][:int(params["page_size"])]
        return httpx.Response(200, json=body)

    def _assets(self, params: httpx.QueryParams) -> httpx.Response:
        ids = params["id"].split(",") if params.get("id") else list(self.assets)
        found = [self.assets[i] for i in ids if i in self.assets]
        size = int(params.get("page_size") or 25)
        start = int((params.get("cursor") or "a:0").split(":")[1]) if params.get("cursor") else 0
        part = found[start:start + size]
        more = start + size < len(found)
        return httpx.Response(200, json={"data": part, "self_cursor": f"a:{start}",
                                         "next_cursor": f"a:{start + size}" if more else None, "has_next_page": more})

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


def open_events(c, sid) -> list[str]:
    """Ereigniskennungen aller Zeilen offener Prüf-Stapel der Quelle (je Zeile, für Dublettenprüfungen)."""
    return [r["event_key"] + "#" + str(r["event_line"]) for r in ctx(c).db.q(
        "SELECT r.event_key, r.event_line FROM csv_row r JOIN csv_batch b ON b.id = r.batch_id WHERE b.kind='sync' "
        "AND b.datasource_id=? AND b.status IN ('preview', 'partial')", (sid,))]


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
# Connector: Vertrag, Pagination, Zeitpunkt, Beträge, Gebühren, Bestände
# ----------------------------------------------------------------------------------------------------

def fetch(cursor=None, key=API_KEY) -> K.FetchResult:
    conn = B.BitpandaConnector()
    return conn.fetch(K.SourceConfig(1, "exchange", "bitpanda", "BP", "Bitpanda"), K.Secret(value=key), cursor)


def by_event(res: K.FetchResult) -> dict[str, K.SourceEvent]:
    return {ev.event_key: ev for ev in res.events}


def amt(value: str, ref: str) -> dict[str, str]:
    return {"value": value, ("currency_id" if ref == EUR_ID else "asset_id"): ref}


def txn(n: int, flow: str, value: str, ref: str, at: str | None = None, *, ttype: str | None = None,
        fee: str = "0", fee_ref: str | None = None, trade: dict[str, Any] | None = None, balance: str | None = None,
        wallet: str | None = None, **extra: Any) -> dict[str, Any]:
    """Teil eines Vorgangs im Format der Referenz."""
    return {"transaction_id": U(0x7A0 + n), "asset_id": None if ref == EUR_ID else ref,
            "currency_id": ref if ref == EUR_ID else None, "wallet_id": wallet or U(0xF00 + int(ref[-3:], 16)),
            "asset_amount": amt(value, ref), "fee_amount": amt(fee, fee_ref or ref), "transaction_type": ttype,
            "flow": flow, "credited_at": at, "asset_balance_after": amt(balance, ref) if balance else None,
            "compensates": None, "trade": trade, **extra}


def trade(n: int, fee: str, rate: str | None, rate_with_fee: str | None, fee_ref: str = EUR_ID) -> dict[str, Any]:
    return {"trade_id": U(0x2A0 + n), "fee": amt(fee, fee_ref), "fee_percentage": None, "rate": rate,
            "rate_with_fee": rate_with_fee, "to_eur_rate": "1"}


def oper(n: int, typ: str, *txs: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"operation_id": U(0x700 + n), "operation_type": typ, "transactions": list(txs), **extra}


def page(data: list[Any], *, more: bool = False, nxt: str | None = None, slf: str | None = None) -> dict[str, Any]:
    return {"data": data, "self_cursor": slf, "next_cursor": nxt, "has_next_page": more}


def ev_of(res: K.FetchResult, n: int) -> list[Rec]:
    return by_event(res)[f"bitpanda:{U(0x700 + n)}"].lines


def test_documented_contract_pagination_and_mapping(api):
    res = fetch()
    assert res.complete and res.cursor["v"] == B.CURSOR_VERSION and res.cursor["from"].endswith("Z")
    ops = api.ops_requests()
    # drei Seiten über next_cursor; die letzte Seite trägt next_cursor „c-4“, aber has_next_page=false → Ende
    assert [r.url.params.get("cursor") for r in ops] == [None, "c-2", "c-3"]
    assert all(r.url.params.get("page_size") == "100" and "pageSize" not in r.url.params for r in ops)
    assert res.coverage["pages"] == 3 and res.coverage["operations"] == 17 and res.coverage["mode"] == "vollständig"
    assert res.coverage["pagination"] == {"page_size": 100, "end": "has_next_page=false",
                                          "next_cursor_on_last_page": True}
    assert res.coverage["time_fields"] == {"transactions[].credited_at": 16, "fehlt": 1}
    diag = res.coverage["diagnostics"]
    assert diag["parser"] == B.PARSER_VERSION and diag["missing"] == {"transaction.credited_at": 2}
    assert not diag["undocumented"] and not diag["counts"].get("Saldoverlauf: Brüche")
    assert diag["counts"]["Saldoverlauf: stimmig"] == diag["counts"]["Saldoverlauf: Übergänge"] > 10
    ev = by_event(res)
    (buy,) = ev[op_key(1)].lines
    assert (buy.kind, buy.out_sym, buy.out_qty, buy.in_sym, buy.in_qty, buy.value) == (
        M.TRADE, "EUR", D("250.00"), "BTC", D("0.00550000"), D("250.00"))
    # trade.fee: 0,0055 × rate_with_fee = 250 → im Betrag enthalten (Einstand stimmt ohne zusätzliche Gebühr)
    assert buy.fee_qty is None and buy.review is None and "im Betrag enthalten" in buy.note
    assert f"bitpanda:{U(0x2001)}" in buy.aliases  # trade.trade_id → Alias für den CSV-Abgleich
    assert buy.ts.isoformat() == "2024-02-02T09:00:00+00:00" and not buy.ts_missing
    assert buy.raw["parser"] == B.PARSER_VERSION and buy.raw["time_source"] == "transactions[].credited_at"
    assert buy.raw["api"]["transactions"][0]["asset_amount"] == {"value": "250.00", "currency_id": EUR_ID}
    assert buy.raw["transactions"][0]["amount"] == "250.00" and buy.raw["transactions"][1]["symbol"] == "BTC"
    multi = ev[op_key(2)].lines  # Kauf + Gebühr in drittem Asset (transaction_type „fee“) → zwei Zeilen
    assert [(r.kind, r.in_sym, r.fee_sym, r.fee_qty) for r in multi] == \
        [(M.TRADE, "ETH", None, None), (M.FEE, None, "BEST", D("0.25000000"))]
    (dep,) = ev[op_key(3)].lines
    assert (dep.kind, dep.in_sym, dep.in_qty) == (M.DEPOSIT, "EUR", D("500.00"))
    (wd,) = ev[op_key(4)].lines  # fee_amount: Saldoverlauf belegt zusätzlichen Abzug → ohne Prüfhinweis
    assert (wd.kind, wd.out_qty, wd.fee_sym, wd.fee_qty) == (M.WITHDRAWAL, D("0.00100000"), "BTC", D("0.00005000"))
    assert wd.review is None and "zusätzlich abgezogen (laut Saldoverlauf)" in wd.note
    (reward,) = ev[op_key(5)].lines
    assert (reward.kind, reward.tag, reward.in_sym) == (M.DEPOSIT, "reward", "ETH")
    assert op_key(6) not in ev and op_key(16) not in ev
    assert res.skipped == {"Bitpanda: interne Umbuchung zwischen Bitpanda-Wallets": 1,
                           "Bitpanda: Umbuchung in bzw. aus Bitpanda Staking (kein Zu- oder Abgang)": 1}
    for n, text in ((7, "Tausch Krypto"), (8, "Aktie/ETF"), (9, "storniert"), (10, "Korrektur/Storno"),
                    (11, "mystery_event")):
        (line,) = ev[op_key(n)].lines
        assert line.kind == M.REVIEW and text in line.note and not line.ts_missing, (n, line.note)
    (sell,) = ev[op_key(12)].lines
    assert (sell.out_sym, sell.in_sym, sell.in_qty, sell.fee_qty) == ("BTC", "EUR", D("98.12345678"), None)
    assert isinstance(sell.in_qty, Decimal)
    (plan,) = ev[op_key(13)].lines
    assert (plan.kind, plan.out_sym, plan.out_qty, plan.in_sym, plan.in_qty) == (
        M.TRADE, "EUR", D("25.00"), "BTC", D("0.00050000"))
    assert plan.note.startswith("Sparplan") and plan.review is None
    assert plan.ts.isoformat() == "2024-02-24T05:00:00+00:00"
    (plan_dep,) = ev[op_key(14)].lines
    assert (plan_dep.kind, plan_dep.in_sym, plan_dep.in_qty) == (M.DEPOSIT, "EUR", D("25.00"))
    assert "Einzahlung für den Sparplan" in plan_dep.note
    sell2, buy2 = ev[op_key(15)].lines  # Swap über EUR = Verkauf + Kauf, Gebühren laut Kurs im Betrag
    assert (sell2.out_sym, sell2.in_sym, sell2.value, buy2.out_sym, buy2.in_sym, buy2.in_qty) == (
        "BTC", "EUR", D("9.80"), "EUR", "ETH", D("0.00400000"))
    assert sell2.review is None and buy2.review is None
    (pending,) = ev[op_key(17)].lines  # ohne credited_at: ungeklärt, gekennzeichnet, Mengen sichtbar
    assert pending.kind == M.REVIEW and pending.ts_missing and "Zeitpunkt fehlt" in pending.note
    assert (pending.out_sym, pending.out_qty, pending.in_sym, pending.in_qty) == (
        "EUR", D("25.00"), "BTC", D("0.00050000"))
    assert pending.raw["credited_at"] is None and pending.raw["time_source"] is None
    assert any("ohne Zeitpunkt" in w for w in res.warnings)


def test_mock_rejects_undocumented_parameters_and_endpoints(api):
    with httpx.Client(transport=httpx.MockTransport(api.handler), base_url=B.BASE_URL) as cl:
        h = {"x-api-key": API_KEY}
        assert cl.get("/operations", params={"page_size": 100}, headers=h).status_code == 200
        assert cl.get("/operations", params={"pageSize": 100}, headers=h).status_code == 400
        assert cl.get("/operations", params={"page_size": 1000}, headers=h).status_code == 400
        assert cl.get("/operations", params={"cursor": "c-4"}, headers=h).status_code == 400  # nie ausgegeben
        assert cl.get("/operations", params={"cursor": U(0x10C)}, headers=h).status_code == 400  # Kennung ≠ Cursor
        assert cl.get("/operations", params={"from": "gestern"}, headers=h).status_code == 400
        assert cl.get("/operations").status_code == 401
        assert cl.get("/portfolio/holdings", headers=h).status_code == 404
        assert cl.get(f"/assets/{BTC_ID}", headers=h).status_code == 404
        assert cl.get("/portfolio", params={"page_size": 1}, headers=h).status_code == 400


def test_time_comes_only_from_credited_at_and_is_never_invented(api):
    t1, t2 = "2025-04-01T06:05:00Z", "2025-04-01T06:05:03Z"
    api.set_pages(page([
        # Zeitpunkt nur an den Teilen (laut Referenz) – Vorgang selbst ohne Zeitfeld
        oper(1, "savings_plan", txn(1, "OUTGOING", "50", EUR_ID, t2, ttype="buy"),
             txn(2, "INCOMING", "0.00061234", BTC_ID, t1, ttype="buy")),
        # nicht dokumentiertes Zeitfeld am Vorgang wird nicht gelesen – ohne credited_at fehlt der Zeitpunkt
        oper(2, "savings_plan", txn(3, "INCOMING", "50", EUR_ID, None, ttype="deposit"),
             timestamp="2025-01-01T00:00:00Z"),
        oper(3, "deposit", txn(4, "INCOMING", "5", EUR_ID, "gestern")),  # nicht lesbar
    ]))
    before = datetime.now(UTC)
    res = fetch()
    assert res.complete and res.coverage["time_fields"] == {"transactions[].credited_at": 1, "fehlt": 2}
    (plan,) = ev_of(res, 1)
    assert plan.ts.isoformat() == "2025-04-01T06:05:00+00:00"  # frühester Teil
    assert plan.raw["time_spread_s"] == 3 and plan.raw["time_source"] == "transactions[].credited_at"
    for n in (2, 3):
        (line,) = ev_of(res, n)
        assert line.kind == M.REVIEW and line.ts_missing and "Zeitpunkt fehlt" in line.note
        assert line.ts >= before and line.ts.year != 2025  # nur Sortierhilfe, nie ein Datum aus der Luft
    diag = res.coverage["diagnostics"]
    assert diag["undocumented"] == {"operation": ["timestamp"]}  # sichtbar, nicht still benutzt
    assert diag["counts"]["credited_at nicht lesbar"] == 1 and diag["missing"]["transaction.credited_at"] == 1
    assert any("2 Vorgang/Vorgänge ohne Zeitpunkt" in w for w in res.warnings)


def test_amount_objects_fees_and_nested_trade(api):
    T = [f"2025-05-0{d}T10:00:00Z" for d in range(1, 10)]
    W_EUR, W_BTC, W_BTC2 = U(0xF0E), U(0xF0B), U(0xF0C)
    api.set_pages(page([
        oper(0, "deposit", txn(0, "INCOMING", "1000", EUR_ID, T[0], balance="1000", wallet=W_EUR)),
        # 1: Gebühr laut Kurs im Betrag (0,002 × 50000 = 100) – trade an beiden Teilen, einmal gezählt
        oper(1, "buy", txn(1, "OUTGOING", "100", EUR_ID, T[1], trade=trade(1, "1.5", "49250", "50000"),
                           balance="900", wallet=W_EUR),
             txn(2, "INCOMING", "0.002", BTC_ID, T[1], trade=trade(1, "1.5", "49250", "50000"), balance="0.002",
                 wallet=W_BTC)),
        # 2: Gebühr laut Kurs zusätzlich (0,002 × 50000 = 100) und laut Saldo zusätzlich abgebucht (−101,5)
        oper(2, "buy", txn(3, "OUTGOING", "100", EUR_ID, T[2], trade=trade(2, "1.5", "50000", "50750"),
                           balance="798.5", wallet=W_EUR),
             txn(4, "INCOMING", "0.002", BTC_ID, T[2], wallet=W_BTC2)),
        # 3: laut Kurs zusätzlich, Abbuchung nicht belegt (kein Saldo) → übernommen, prüfbedürftig
        oper(3, "buy", txn(5, "OUTGOING", "100", EUR_ID, T[3], trade=trade(3, "1.5", "50000", "50750")),
             txn(6, "INCOMING", "0.002", BTC_ID, T[3], wallet=W_BTC2)),
        # 4: Handelsgebühr ohne Kurse → weder gebucht noch geraten, prüfbedürftig
        oper(4, "buy", txn(7, "OUTGOING", "100", EUR_ID, T[4], trade=trade(4, "1.5", None, None)),
             txn(8, "INCOMING", "0.002", BTC_ID, T[4], wallet=W_BTC2)),
        # 5: fee_amount im Betrag (Saldo sinkt nur um den Betrag) → Abgang ohne Gebühr + Gebühr
        oper(5, "withdrawal", txn(9, "OUTGOING", "0.001", BTC_ID, T[5], fee="0.0001", balance="0.001",
                                  wallet=W_BTC)),
        # 6: Einzahlung, Gebühr ohne Wirkung auf den Bestand laut Saldo → nur Hinweis
        oper(6, "deposit", txn(10, "INCOMING", "50", EUR_ID, T[6], fee="1", balance="848.5", wallet=W_EUR)),
        # 7: Einzahlung mit Gebühr ohne Beleg → übernommen, prüfbedürftig
        oper(7, "deposit", txn(11, "INCOMING", "20", EUR_ID, T[7], fee="0.5", wallet=U(0xF99))),
        # 8: Gebühr in anderem Asset als der Betrag (EUR auf BTC-Auszahlung) → Symbol aufgelöst, prüfbedürftig
        oper(8, "withdrawal", txn(12, "OUTGOING", "0.0005", BTC_ID, T[8], fee="0.5", fee_ref=EUR_ID,
                                  wallet=W_BTC2)),
    ]))
    res = fetch()
    assert res.complete
    (b1,) = ev_of(res, 1)
    assert b1.fee_qty is None and b1.review is None and "Gebühr 1.5 EUR laut Bitpanda im Betrag enthalten" in b1.note
    assert f"bitpanda:{U(0x2A1)}" in b1.aliases
    (b2,) = ev_of(res, 2)
    assert (b2.fee_sym, b2.fee_qty, b2.review) == ("EUR", D("1.5"), None) and "Saldoverlauf" in b2.note
    (b3,) = ev_of(res, 3)
    assert (b3.fee_sym, b3.fee_qty) == ("EUR", D("1.5")) and "Abbuchung nicht belegt" in b3.review
    (b4,) = ev_of(res, 4)
    assert b4.fee_qty is None and "nicht ableitbar" in b4.review and b4.out_qty == D("100")
    (w5,) = ev_of(res, 5)
    assert (w5.out_qty, w5.fee_sym, w5.fee_qty, w5.review) == (D("0.0009"), "BTC", D("0.0001"), None)
    assert "enthält die Gebühr" in w5.note
    (d6,) = ev_of(res, 6)
    assert (d6.in_qty, d6.fee_qty, d6.review) == (D("50"), None, None) and "ohne Wirkung" in d6.note
    (d7,) = ev_of(res, 7)
    assert (d7.in_qty, d7.fee_sym, d7.fee_qty) == (D("20"), "EUR", D("0.5")) and "nicht dokumentiert" in d7.review
    (w8,) = ev_of(res, 8)
    assert (w8.out_sym, w8.fee_sym, w8.fee_qty) == ("BTC", "EUR", D("0.5")) and w8.review
    counts = res.coverage["diagnostics"]["counts"]
    assert counts["Saldoverlauf: stimmig"] == counts["Saldoverlauf: Übergänge"] == 4  # EUR ×3, BTC ×1


def test_multiple_pages_and_last_page_with_cursor(api):
    o = [oper(i, "deposit", txn(i, "INCOMING", "1", EUR_ID, f"2025-01-0{i}T00:00:00Z")) for i in range(1, 6)]
    api.set_pages(page(o[:2], more=True, nxt="n1", slf="s0"), page(o[2:4], more=True, nxt="n2", slf="n1"),
                  page(o[4:], more=False, nxt="n3", slf="n2"))  # n3 gibt es nicht – darf nie angefragt werden
    res = fetch()
    assert res.complete and res.coverage["operations"] == 5 and res.coverage["pages"] == 3
    assert [r.url.params.get("cursor") for r in api.ops_requests()] == [None, "n1", "n2"]


def test_repeated_cursor_with_has_next_page_true_is_an_error(api):
    a, b, c = (oper(i, "deposit", txn(i, "INCOMING", "1", EUR_ID, "2025-01-01T00:00:00Z")) for i in (1, 2, 3))
    api.pages = {None: page([a], more=True, nxt="c1"), "c1": page([b], more=True, nxt="c1", slf="c1x")}
    res = fetch()
    assert not res.complete and res.cursor is None and res.coverage["operations"] == 2  # Seite voll verarbeitet
    assert any("wiederholt denselben Cursor trotz has_next_page=true" in w for w in res.warnings)
    assert res.coverage["pagination"]["end"].startswith("Pagination wiederholt")
    api.calls.clear()
    api.pages = {None: page([a], more=True, nxt="c1"), "c1": page([b], more=True, nxt="c2"),
                 "c2": page([c], more=True, nxt="c1")}  # Rücksprung auf einen früheren Cursor
    res = fetch()
    assert not res.complete and res.coverage["operations"] == 3
    assert len(api.ops_requests()) == 3


@pytest.mark.parametrize(("pages", "text"), [
    ({None: {"data": ["OP"]}}, "has_next_page fehlt"),
    ({None: {"data": ["OP"], "next_cursor": "x", "has_next_page": "false"}}, "kein Wahrheitswert"),
    ({None: page(["OP"], more=True)}, "has_next_page=true ohne next_cursor"),
    ({None: page(["OP"], more=True, nxt="s", slf="s")}, "next_cursor gleich self_cursor"),
    ({None: page(["OP"], more=True, nxt="e"), "e": page([], more=True, nxt="f"), "f": page([], more=True, nxt="g"),
      "g": page([], more=True, nxt="h")}, "3 leere Seiten in Folge trotz has_next_page=true"),
    ({None: page(["OP"], more=True, nxt="d"), "d": page(["OP"], more=True, nxt="e")}, "nur bereits gelieferte"),
])
def test_missing_or_contradictory_pagination_is_visible(api, pages, text):
    op = oper(1, "deposit", txn(1, "INCOMING", "1", EUR_ID, "2025-01-01T00:00:00Z"))
    api.pages = {k: {**v, "data": [op if x == "OP" else x for x in v["data"]]} for k, v in pages.items()}
    res = fetch()
    assert not res.complete and res.cursor is None, text
    assert any(text in w for w in res.warnings), res.warnings
    assert res.coverage["operations"] == 1  # Daten der Seite bleiben nutzbar
    assert res.coverage["balances"].get("compared", 0) == 0  # kein Abgleich mit unvollständiger Historie


def test_empty_page_with_has_next_page_is_followed_not_trusted(api):
    a, b = (oper(i, "deposit", txn(i, "INCOMING", "1", EUR_ID, "2025-01-01T00:00:00Z")) for i in (1, 2))
    api.set_pages(page([a], more=True, nxt="p2"), page([], more=True, nxt="p3"), page([b], more=False, nxt="p9"))
    res = fetch()  # leere Zwischenseite: Cursor wird gefolgt, das Ende belegt erst has_next_page=false
    assert res.complete and res.coverage["operations"] == 2
    assert res.coverage["diagnostics"]["counts"]["leere Seite mit has_next_page=true"] == 1


def test_page_length_never_decides_the_end(api):
    full = [oper(i, "deposit", txn(i, "INCOMING", "1", EUR_ID, "2025-01-01T00:00:00Z")) for i in range(25)]
    api.set_pages(page(full, more=False, nxt="x"))  # volle Standardseite, has_next_page=false → Ende
    assert fetch().complete
    api.set_pages(page([], more=False))  # leere erste Seite, eindeutig zu Ende
    res = fetch()
    assert res.complete and res.coverage["operations"] == 0
    api.pages = {None: {"items": []}}  # keine Liste „data“ → Vertragsfehler, kein stiller Erfolg
    with pytest.raises(K.ConnectorError) as e:
        fetch()
    assert e.value.kind == "data" and "data" in e.value.message


def test_page_size_falls_back_to_documented_default(api):
    api.max_page_size = 50
    res = fetch()
    sizes = [r.url.params.get("page_size") for r in api.ops_requests()]
    assert sizes[:2] == ["100", "25"] and set(sizes[1:]) == {"25"}
    assert res.complete and res.coverage["page_size"] == 25
    assert any("page_size=100 nicht akzeptiert" in w for w in res.warnings)


def test_incremental_fetch_uses_from_and_cursor_version(api):
    res = fetch({"v": B.CURSOR_VERSION, "from": "2024-02-18T00:00:00.000Z"})
    first = api.ops_requests()[0]
    assert first.url.params.get("from") == "2024-02-18T00:00:00.000Z" and res.coverage["mode"] == "inkrementell"
    assert op_key(1) not in by_event(res) and op_key(12) in by_event(res)
    assert res.balances is not None and res.coverage["balances"]["compared"] == 0  # Summenvergleich nur vollständig
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", res.cursor["from"])
    api.calls.clear()  # Abrufstand einer älteren Auswertung → vollständiger Neuabruf
    res = fetch({"v": 2, "from": "2024-02-18T00:00:00Z"})
    assert "from" not in api.ops_requests()[0].url.params and res.coverage["mode"] == "vollständig"
    assert any("Auswertung aktualisiert" in w for w in res.warnings)
    conn = B.BitpandaConnector()
    assert conn.rewind({"v": 3, "from": "2024-03-10T00:00:00.000Z"}, datetime(2024, 2, 20, tzinfo=UTC)) is None


def test_rate_limit_waits_and_partial_on_exhaustion(api):
    api.fail["/v1/operations?cursor=c-2"] = [httpx.Response(429, headers={"Retry-After": "3"})]
    res = fetch()
    assert res.complete and api.sleeps == [3.0] and res.coverage["throttled"] == 1
    api.sleeps.clear()
    api.fail["/v1/operations?cursor=c-2"] = [httpx.Response(429, headers={"Retry-After": "30"})] * 10
    res = fetch()
    assert not res.complete and res.cursor is None  # Abrufstand rückt nicht vor
    assert any("abgebrochen" in w for w in res.warnings) and res.coverage["pagination"]["end"] == "abgebrochen"
    assert op_key(1) in by_event(res) and op_key(12) not in by_event(res)  # Seite 1 bleibt nutzbar
    assert sum(api.sleeps) <= B.WAIT_BUDGET_S
    api.sleeps.clear()
    api.fail["/v1/operations?cursor=c-2"] = [httpx.Response(429, headers={"Retry-After": "900"})]
    res = fetch()  # längere Wartezeit als erlaubt: nicht verkürzt erneut fragen, sondern abbrechen
    assert not res.complete and api.sleeps == [] and any("abgebrochen" in w for w in res.warnings)
    api.fail["/v1/operations"] = [httpx.Response(429, headers={"Retry-After": "900"})]
    with pytest.raises(K.ConnectorError) as ei:
        fetch()
    assert ei.value.kind == "rate_limit" and ei.value.retry_after_s == 900 and api.sleeps == []


def test_portfolio_balance_value_is_reconciled_with_operations(api):
    res = fetch()
    bal = res.coverage["balances"]
    assert bal["checked"] and bal["assets"] == 6 and bal["compared"] == 6 and bal["differences"] == 1
    assert len(bal["examples"]) == 1 and bal["examples"][0].startswith("XAU: /portfolio 1, Vorgänge 0")
    by_key = {b.asset_key: b for b in res.balances}
    assert set(by_key) == {"AAPL", "BEST", "BTC", "ETH", "EUR", "XAU"}
    assert by_key["EUR"].qty == D("123.12345678") and "stimmen" in by_key["EUR"].note
    assert by_key["BTC"].qty == D("0.00385") and "Gebühren (fee_amount) zusätzlich abgezogen" in by_key["BTC"].note
    assert "ohne Staking-Umbuchungen" in by_key["ETH"].note and "nicht verfügbar 0.002" in by_key["ETH"].note
    assert by_key["XAU"].name == "Gold" and "Vorgänge 0" in by_key["XAU"].note
    assert any("Bestandsprüfung: 1 Asset" in w for w in res.warnings)
    # HTTP 200 ohne auswertbare Position (anderer Aufbau) ist keine erfolgreiche Prüfung
    api.portfolio = {"data": [{"asset_id": BTC_ID, "quantity": "1"}]}
    res = fetch()
    assert res.balances is None and not res.coverage["balances"]["checked"]
    assert "ohne auswertbare Position" in res.coverage["balances"]["note"]
    assert any("ohne auswertbare Position" in w for w in res.warnings)
    api.portfolio = {"data": []}
    res = fetch()
    assert res.balances is None and not res.coverage["balances"]["checked"]
    api.portfolio_status = 403
    res = fetch()
    assert res.balances is None and "Balances" in res.coverage["balances"]["note"] and res.complete


def test_errors_are_classified(api, monkeypatch):
    conn = B.BitpandaConnector()
    cfg = K.SourceConfig(1, "exchange", "bitpanda", "BP", "Bitpanda")
    res = conn.check(cfg, K.Secret(value="bp_wrong_key_0000000000000000"))
    assert not res.ok and "ungültig" in res.message and not res.details["transaction"]["ok"]
    # gültiger Schlüssel, aber ohne Leserecht „Transaction“: Bestände lesbar, Vorgänge 401
    api.fail["/v1/operations"] = [_err(401, "UNAUTHORIZED")] * 2
    res = conn.check(cfg, K.Secret(value=API_KEY))
    assert not res.ok and "Leserecht „Transaction“" in res.message and res.details["balances"]["ok"]
    api.fail["/v1/operations"] = [_err(403, "INSUFFICIENT_SCOPE")] * 2
    res = conn.check(cfg, K.Secret(value=API_KEY))
    assert not res.ok and "Transaction" in res.message
    api.fail["/v1/operations"] = [_err(401, "API_KEY_EXPIRED")] * 2
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
    assert "nicht gefolgt" in e.value.message


def test_check_reports_scopes_and_contract(client, api):
    c = client
    sid = create_bitpanda(c)
    api.portfolio_status = 403
    r = post(c, f"/settings/datasources/{sid}/check")
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    page_ = c.get(r.headers["location"]).text
    assert "Vorgänge lesbar" in page_ and "Bestände" in page_ and "verbunden" in page_
    chk = json.loads(source(c, sid)["last_check_json"])
    assert chk["ok"] and chk["details"]["transaction"]["ok"] and not chk["details"]["balances"]["ok"]
    assert API_KEY not in page_
    # Antwortformat weicht ab (credited_at fehlt im ersten Vorgang) → sichtbar, Zugang trotzdem in Ordnung
    first = copy.deepcopy(api.pages[None])
    for t in first["data"][0]["transactions"]:
        t.pop("credited_at")
    api.pages[None] = first
    api.portfolio_status = 200
    ok, msg = datasource_service(ctx(c)).check(sid)
    chk = json.loads(source(c, sid)["last_check_json"])
    assert ok and "weicht von der Referenz ab" in msg
    assert "transactions[].credited_at" in chk["details"]["transaction"]["text"]


# ----------------------------------------------------------------------------------------------------
# Synchronisieren: historisch, inkrementell, Herkunft, Reparatur, Abgrenzung, Verwerfen, Ignorieren
# ----------------------------------------------------------------------------------------------------

UNCLEAR = (7, 8, 9, 10, 11, 17)


def test_historical_then_incremental_sync(client, api):
    c = client
    sid = create_bitpanda(c)
    r = sync(c, sid)
    bid = batch_of(r)
    create_unknown_assets(c, bid)
    rs = rows_by_key(c, bid)
    st = {k: rc.status for k, rc in rs.items()}
    assert st[f"{op_key(1)}#0"] == "new" and st[f"{op_key(2)}#0"] == "new" and st[f"{op_key(13)}#0"] == "new"
    assert st[f"{op_key(2)}#1"] == "invalid"  # BEST-Gebühr ohne Kurs: EUR-Wert fehlt
    assert "EUR-Wert fehlt" in rs[f"{op_key(2)}#1"].errors[0]
    assert st[f"{op_key(5)}#0"] == "new" and "Kurs" in rs[f"{op_key(5)}#0"].value_src  # Kurs aus dem ETH-Kauf
    for n in UNCLEAR:
        assert st[f"{op_key(n)}#0"] == "unclear", n
    assert rs[f"{op_key(17)}#0"].rec.ts_missing
    assert "Bitpanda: interne Umbuchung zwischen Bitpanda-Wallets" in json.loads(
        csv_service(ctx(c)).batch(bid)["summary_json"])["skipped"]
    page_ = c.get(f"/journal/csv/{bid}").text
    assert "ungeklärt" in page_ and "Tausch Krypto" in page_ and "dauerhaft ignorieren" in page_
    assert "Zeitpunkt fehlt" in page_ and "Herkunft" in page_ and "Auswertung v3" in page_
    run = datasource_service(ctx(c)).runs(sid)[0]
    assert run["rows_unclear"] == 6 and "ungeklärt 6" in run["message"]
    r = post(c, f"/journal/csv/{bid}/commit")
    assert r.status_code == 303
    txs = journal(c, "source='sync:bitpanda'")
    assert {t["event_key"] for t in txs} >= {op_key(n) for n in (1, 2, 3, 4, 12, 13, 14, 15)}
    assert not any(t["event_key"] == op_key(17) for t in txs)  # ohne Zeitpunkt nie gebucht
    assert csv_service(ctx(c)).batch(bid)["status"] == "partial"  # ungeklärte Vorgänge offen
    assert json.loads(source(c, sid)["cursor_json"])["from"]
    # zweiter Lauf: Zeitfilter (from) mit Überlappung – nichts Neues seit dem letzten Abruf
    n_calls = len(api.ops_requests())
    r = sync(c, sid)
    assert "/settings/datasources" in r.headers["location"]
    assert api.ops_requests()[n_calls].url.params.get("from")
    assert datasource_service(ctx(c)).runs(sid)[0]["message"].startswith("0 Vorgänge")
    # vollständig neu: alles bekannt oder wartet bereits → kein neuer Stapel, keine Dubletten
    events = sorted(open_events(c, sid))
    post(c, f"/settings/datasources/{sid}/reset")
    r = sync(c, sid)
    assert "/settings/datasources" in r.headers["location"]
    assert len(journal(c, "source='sync:bitpanda'")) == len(txs)
    assert csv_service(ctx(c)).batch(bid) is not None
    assert "wartet bereits auf Prüfung 7" in datasource_service(ctx(c)).runs(sid)[0]["message"]  # 6 ungeklärt + 1
    assert sorted(open_events(c, sid)) == events


def test_trace_savings_plan_from_http_json_to_review_row(client, api):
    """Ein Sparplan-Vorgang vom unveränderten HTTP-JSON über _parse(), Abbildung, Speicherung und Wiederabruf bis
    zur angezeigten Prüfzeile – mit und ohne credited_at."""
    http = api.pages["c-3"]["data"]
    plan_json = next(o for o in http if o["operation_id"] == U(0x10D))
    pending_json = next(o for o in http if o["operation_id"] == U(0x111))
    assert "credited_at" not in plan_json and all(t["credited_at"] for t in plan_json["transactions"])
    diag = B._Diag()
    plan = B._parse(copy.deepcopy(plan_json), diag)
    assert plan.ts == datetime(2024, 2, 24, 5, 0, tzinfo=UTC)
    assert plan.raw["time_source"] == "transactions[].credited_at"
    assert [(lg.side, lg.amount, lg.ttype) for lg in plan.legs] == [("out", D("25.00"), "buy"),
                                                                     ("in", D("0.00050000"), "buy")]
    assert B._parse(copy.deepcopy(pending_json), diag).ts is None
    c = client
    sid = create_bitpanda(c)
    bid = batch_of(sync(c, sid))
    stored = {r["event_key"]: json.loads(r["rec_json"]) for r in ctx(c).db.q(
        "SELECT event_key, rec_json FROM csv_row WHERE batch_id=?", (bid,))}
    s13, s17 = stored[op_key(13)], stored[op_key(17)]
    assert s13["ts"].startswith("2024-02-24T05:00:00") and (D(s13["out_qty"]), D(s13["in_qty"])) == (D(25), D("0.0005"))
    assert s13["raw"]["parser"] == B.PARSER_VERSION and "ts_missing" not in s13
    assert s17["kind"] == M.REVIEW and s17["ts_missing"] is True and s17["raw"]["credited_at"] is None
    rs = rows_by_key(c, bid)
    assert rs[f"{op_key(13)}#0"].rec.in_qty == D("0.0005") and rs[f"{op_key(17)}#0"].status == "unclear"
    page_ = c.get(f"/journal/csv/{bid}?status=unclear").text  # sechs ungeklärte Zeilen, nur eine ohne Zeitpunkt
    assert page_.count('title="Die Quelle liefert keinen Zeitpunkt') == 1
    assert page_.count("Zeitpunkt: fehlt in den Daten") == 1 and "Vorgangsart „savings_plan“" in page_


def _old_parser_batch(c, sid, events: list[int]) -> int:
    """Prüf-Stapel wie von einer älteren Auswertung (vor 0.16.2): ungeklärt, ohne Zeitpunkt und Mengen,
    Abrufzeitpunkt als Datum, Rohdaten ohne Versionsangabe."""
    recs = []
    for n in events:
        key = op_key(n)
        recs.append(Rec(line=0, ts=datetime(2026, 10, 2, 6, 43, tzinfo=UTC), kind=M.REVIEW,
                        label="Bitpanda: savings_plan", note="Ungeklärt: Zeitpunkt fehlt in den API-Daten",
                        ext_id=f"{key}#0", event_key=key, event_line=0, account="Bitpanda",
                        raw={"operation_id": U(0x100 + n), "type": "savings_plan", "timestamp": None,
                             "transactions": [{"flow": "outgoing", "amount": "{'value': '25.00'}"}]}))
    return csv_service(ctx(c)).ingest(recs, source="sync:bitpanda", profile="sync:bitpanda", account="Bitpanda",
                                      label="Bitpanda · Synchronisierung (alt)", datasource_id=sid, payload=b"[]")


def test_old_parser_rows_are_replaced_edited_kept_and_decisions_preserved(client, api):
    c = client
    sid = create_bitpanda(c)
    svc = datasource_service(ctx(c))
    old_bid = _old_parser_batch(c, sid, [1, 3, 13, 14, 17])
    assert svc.outdated(sid) == 5
    page_ = c.get(f"/journal/csv/{old_bid}").text
    assert "5 Vorgänge mit älterer Auswertung" in page_ and "Veraltete Zeilen neu auswerten" in page_
    assert "ältere Auswertung (ohne Versionsangabe)" in page_
    # Nutzer: Vorgang 3 dauerhaft ignoriert (je Ereignis), Vorgang 14 mit Entscheidung bearbeitet
    assert csv_service(ctx(c)).set_ignored(old_bid, op_key(3), True, "Test")
    ctx(c).db.x("UPDATE csv_row SET decision='skip' WHERE batch_id=? AND event_key=?", (old_bid, op_key(14)))
    res = svc.sync(sid)
    assert "neu ausgewertet 4" in res["message"] and "1 bearbeitete Vorgänge mit älterer Auswertung" in res["message"]
    new_bid = res["batch_id"]
    assert new_bid != old_bid  # alter Stapel ist bearbeitet → neue Zeilen in einem eigenen Stapel
    old_rows = rows_by_key(c, old_bid)
    assert set(old_rows) == {f"{op_key(14)}#0"} and old_rows[f"{op_key(14)}#0"].decision == "skip"
    summ = json.loads(csv_service(ctx(c)).batch(old_bid)["summary_json"])
    assert (summ["events"], summ["recs"], summ["replaced"]) == (1, 1, 4)
    rs = rows_by_key(c, new_bid)
    assert rs[f"{op_key(3)}#0"].status == "ignored"  # Entscheidung je Ereignis gilt für die neue Auswertung
    assert rs[f"{op_key(1)}#0"].rec.kind == M.TRADE and rs[f"{op_key(1)}#0"].rec.in_qty == D("0.0055")
    assert rs[f"{op_key(17)}#0"].rec.ts_missing and rs[f"{op_key(17)}#0"].status == "unclear"
    assert f"{op_key(14)}#0" not in rs  # bearbeitet: nicht doppelt abgelegt
    events = open_events(c, sid)
    assert len(events) == len(set(events))  # jede Ereigniszeile genau einmal offen
    assert svc.outdated(sid) == 1
    # Wiederholung: nichts mehr zu ersetzen, nichts doppelt
    res2 = svc.sync(sid)
    assert "neu ausgewertet" not in res2["message"] and sorted(open_events(c, sid)) == sorted(events)
    # ausdrücklicher Reparaturweg im Prüf-Stapel: Eingaben an veralteten Zeilen verwerfen, neu abrufen
    r = post(c, f"/journal/csv/{old_bid}/refresh-outdated")
    assert r.status_code == 303 and "/journal/csv/" in r.headers["location"]
    assert csv_service(ctx(c)).batch(old_bid) is None  # leer geworden → verworfen
    assert svc.outdated(sid) == 0
    events = open_events(c, sid)
    assert f"{op_key(14)}#0" in events and len(events) == len(set(events))
    assert ctx(c).db.scalar("SELECT decision FROM event_decision WHERE event_key=?", (op_key(3),)) == "ignore"


def test_committed_bookings_survive_reparse_without_double_booking(client, api):
    c = client
    sid = create_bitpanda(c)
    svc = datasource_service(ctx(c))
    csv = csv_service(ctx(c))
    # ältere Auswertung hat Vorgang 2 (Kauf + BEST-Gebühr) als eine Zeile gebucht
    rec = Rec(line=0, ts=datetime(2024, 2, 5, 10, 30, tzinfo=UTC), kind=M.TRADE, out_sym="EUR", out_qty=D("100"),
              in_sym="ETH", in_qty=D("0.03"), value=D("100"), value_ccy="EUR", ext_id=f"{op_key(2)}#0",
              event_key=op_key(2), event_line=0, account="Bitpanda", raw={"operation_id": U(0x102)})
    bid0 = csv.ingest([rec], source="sync:bitpanda", profile="sync:bitpanda", account="Bitpanda", label="alt",
                      datasource_id=sid, payload=b"[]")
    create_unknown_assets(c, bid0)
    assert post(c, f"/journal/csv/{bid0}/commit").status_code == 303
    before = journal(c, "source='sync:bitpanda'")
    assert len(before) == 1
    bid = svc.sync(sid)["batch_id"]
    rs = rows_by_key(c, bid)
    assert rs[f"{op_key(2)}#0"].status == "known"
    fee = rs[f"{op_key(2)}#1"]  # neue Zeile derselben, bereits übernommenen Buchung → nicht zusätzlich buchen
    assert fee.status == "known" and "Vorgang bereits übernommen" in fee.warnings[0]
    create_unknown_assets(c, bid)
    post(c, f"/journal/csv/{bid}/commit")
    assert not journal(c, f"source='sync:bitpanda' AND event_key='{op_key(2)}' AND tx_id <> '{before[0]['tx_id']}'")
    assert journal(c, f"tx_id='{before[0]['tx_id']}'")[0]["status"] == "active"  # Nutzerbuchung unverändert


def test_full_refetch_then_incremental_without_duplicates(client, api):
    c = client
    sid = create_bitpanda(c)
    bid = batch_of(sync(c, sid))
    create_unknown_assets(c, bid)
    post(c, f"/journal/csv/{bid}/commit")
    n = len(journal(c, "source='sync:bitpanda'"))
    events = sorted(open_events(c, sid))
    batches = ctx(c).db.scalar("SELECT COUNT(*) FROM csv_batch WHERE datasource_id=?", (sid,))
    page_ = c.get(f"/settings/datasources/{sid}").text
    assert "Vollständig neu abrufen" in page_
    r = post(c, f"/settings/datasources/{sid}/refetch")
    assert r.status_code == 303 and "/settings/datasources/" in r.headers["location"]
    assert "from" not in api.ops_requests()[-3].url.params  # vollständig
    r = sync(c, sid)  # danach inkrementell
    assert api.ops_requests()[-1].url.params.get("from") or api.ops_requests()[-3].url.params.get("from")
    assert len(journal(c, "source='sync:bitpanda'")) == n
    assert sorted(open_events(c, sid)) == events
    assert ctx(c).db.scalar("SELECT COUNT(*) FROM csv_batch WHERE datasource_id=?", (sid,)) == batches


def test_waiting_events_are_scoped_per_datasource(client, api):
    c = client
    a = create_bitpanda(c, name="Bitpanda A")
    bid_a = batch_of(sync(c, a))
    b = create_bitpanda(c, name="Bitpanda B")  # dasselbe Konto ein zweites Mal verbunden
    bid_b = batch_of(sync(c, b))  # offene Prüfung von A blockiert B nicht
    assert bid_b != bid_a and f"{op_key(1)}#0" in rows_by_key(c, bid_b)
    create_unknown_assets(c, bid_a)
    post(c, f"/journal/csv/{bid_a}/commit")
    n = len(journal(c, "source='sync:bitpanda'"))
    post(c, f"/journal/csv/{bid_b}/commit")  # gleiche Kennungen → bekannt, nicht doppelt gebucht
    assert len(journal(c, "source='sync:bitpanda'")) == n
    assert rows_by_key(c, bid_b)[f"{op_key(1)}#0"].status == "known"


def test_discard_makes_events_refetchable(client, api):
    c = client
    sid = create_bitpanda(c)
    bid = batch_of(sync(c, sid))
    cur = json.loads(source(c, sid)["cursor_json"])
    assert cur["from"] > "2026"
    assert post(c, f"/journal/csv/{bid}/discard").status_code == 303
    assert source(c, sid)["cursor_json"] is None  # vollständiger Neuabruf
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
    for n in UNCLEAR:
        post(c, f"/journal/csv/{bid}/ignore", ignore=op_key(n))
    assert csv_service(ctx(c)).batch(bid)["status"] == "committed"  # nichts mehr offen
    # vollständig neu abrufen: ignorierte erscheinen nicht erneut zur Prüfung, übernommene sind bekannt
    post(c, f"/settings/datasources/{sid}/reset")
    r = sync(c, sid)
    assert "/settings/datasources" in r.headers["location"], r.headers["location"]
    assert datasource_service(ctx(c)).open_counts(sid) == {}
    post(c, f"/journal/csv/{bid}/ignore", release=op_key(11))  # Entscheidung aufheben
    assert not ctx(c).db.scalar("SELECT COUNT(*) FROM event_decision WHERE event_key=?", (op_key(11),))


def test_auto_commit_takes_only_unambiguous_events(client, api):
    c = client
    sid = create_bitpanda(c, auto_commit="1")
    res = datasource_service(ctx(c)).sync(sid)  # nur Euro-Einzahlungen sind ohne Zuordnung eindeutig
    bid = res["batch_id"]
    assert {t["event_key"] for t in journal(c, "source='sync:bitpanda'")} == {op_key(3), op_key(14)}
    assert csv_service(ctx(c)).batch(bid)["status"] == "partial"  # Rest wartet auf Prüfung
    create_unknown_assets(c, bid)  # Nutzer ordnet Assets zu – übernimmt aber nicht selbst
    res2 = datasource_service(ctx(c)).sync(sid)  # nächster Lauf übernimmt, was jetzt eindeutig ist
    keys = {t["event_key"] for t in journal(c, "source='sync:bitpanda'")}
    assert {op_key(n) for n in (1, 3, 4, 5, 12, 13, 14, 15)} <= keys
    assert op_key(2) not in keys  # Gebührenzeile ohne EUR-Wert → ganzes Ereignis bleibt offen
    assert not keys & {op_key(n) for n in UNCLEAR}
    rs = rows_by_key(c, bid)
    assert rs[f"{op_key(7)}#0"].status == "unclear" and rs[f"{op_key(17)}#0"].status == "unclear"
    assert rs[f"{op_key(2)}#0"].status == "new" and rs[f"{op_key(2)}#1"].status == "invalid"
    assert res2["committed"] >= 7 and res2.get("batch_id") is None


def test_partial_failure_keeps_cursor_and_success_time(client, api):
    c = client
    sid = create_bitpanda(c)
    api.fail["/v1/operations?cursor=c-2"] = [httpx.Response(429, headers={"Retry-After": "30"})] * 10
    bid = batch_of(sync(c, sid))
    row = source(c, sid)
    assert row["status"] == "partial" and row["cursor_json"] is None and "unvollständig" in row["last_error"]
    assert row["last_success_at"] is None  # ein abgebrochener Lauf ist keine erfolgreiche Synchronisierung
    assert f"{op_key(1)}#0" in rows_by_key(c, bid)
    assert "teilweise synchronisiert" in c.get("/settings/datasources").text
    assert "unvollständig" in c.get(f"/settings/datasources/{sid}").text
    api.fail.clear()
    sync(c, sid)
    ok = source(c, sid)
    assert ok["status"] == "synced" and ok["last_success_at"] and ok["cursor_json"]
    api.fail["/v1/operations?cursor=c-2"] = [httpx.Response(429, headers={"Retry-After": "30"})] * 10
    sync(c, sid)
    after = source(c, sid)
    assert after["status"] == "partial" and after["cursor_json"] == ok["cursor_json"]
    assert after["last_success_at"] == ok["last_success_at"]


def test_datasource_page_shows_diagnostics_and_holdings(client, api):
    c = client
    sid = create_bitpanda(c)
    batch_of(sync(c, sid))
    page_ = c.get(f"/settings/datasources/{sid}").text
    assert "Parser v3" in page_ and "transactions[].credited_at 16×" in page_ and "fehlt 1×" in page_
    assert "Ende has_next_page=false (next_cursor der letzten Seite laut has_next_page nicht verwendet)" in page_
    assert "Saldoverlauf (asset_balance_after)" in page_ and "Bestandsprüfung (/portfolio)" in page_
    # Bestände je Asset – auch solche, die nur Bitpanda kennt
    assert "Bestände: laut Bitpanda vs. durch Portfolia-Buchungen erklärt" in page_ and "XAU" in page_
    assert API_KEY not in page_


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
    from datetime import timedelta

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
    assert known.status == "known" and "gleiche Anbieter-ID" in known.warnings[0]  # trade.trade_id
    sell = rs[f"{op_key(12)}#0"]
    assert sell.status == "duplicate" and not sell.include()  # unsicher → Entscheidung, nicht still
    # Gegenüberstellung mit der vorhandenen Buchung: offen bei Dubletten, Abweichung (Erlös) hervorgehoben
    legacy = journal(c, "source='csv:bitpanda' AND external_id LIKE '%legacy%'")[0]["tx_id"]
    assert sell.dup_of == [legacy]
    page_ = c.get(f"/journal/csv/{bid}?status=duplicate").text
    block = page_[page_.index('<details class="dup-compare" open>'):]
    block = block[:block.index("</details>")]
    assert f"Vorhanden · <span class=\"mono\">{legacy}</span>" in block and "1 Abweichung" in block
    assert block.count('class="diff"') == 1 and "+98,12 EUR" in block and "+98,12345678 EUR" in block
    assert "App · CSV · Bitpanda" in block and "Abstand" not in block  # gleicher Zeitpunkt
    known_page = c.get(f"/journal/csv/{bid}?status=known").text  # bereits vorhanden: eingeklappt, gleiche Angaben
    assert '<details class="dup-compare">' in known_page and "Angaben gleich" in known_page
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
    d.migrate(target=8)
    assert d.scalar("PRAGMA user_version") == 8
    row = dict(d.q1("SELECT * FROM data_source"))
    assert {k: row[k] for k in before[0]} == before[0]
    assert row["key_expires_on"] is None and row["coverage_json"] is None
    run = d.q1("SELECT * FROM data_source_run")
    assert (run["rows_new"], run["rows_unclear"], run["rows_ignored"]) == (3, 0, 0)
    tables = {r["name"] for r in d.q("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"data_source_secret", "event_decision", "journal_event_alias", "journal_import_link",
            "ds_asset_cache"} <= tables
    d.migrate(target=8)  # erneut: nichts zu tun
    assert d.scalar("PRAGMA user_version") == 8
