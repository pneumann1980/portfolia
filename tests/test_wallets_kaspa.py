"""Kaspa-Wallets (KAS + KRC-20) – synthetische Fixtures, ohne Netz.

Abgedeckt: KAS-Historie mit Wechselgeld, Gebühren, Coinbase, Umbuchung, fremden Eingängen, KRC-20-Inskription
(Commit/Reveal als nur Gebühr), Seiten nach Blockzeit mit gleichen Grenzzeitpunkten, Abbruch und Fortsetzung,
Überlappung nach dem Aufholen, KRC-20-Operationen (Transfer, Mint, list, abgelehnt, zu jung), KRC-20-Ausfall bzw.
„nicht synchron“ als sichtbare Lücke bei vollständigem KAS, abgeschaltete KRC-20-Abfrage.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from app.datasources import chainhttp as CH
from app.datasources.chains import kaspa as KA
from app.datasources.chains.codec import kaspa_encode
from app.datasources.service import datasource_service
from app.datasources.wallet import WalletConnector
from tests.wallet_fakes import MASTER, FakeKaspa, all_rows, balances, create_wallet, ctx, make_client, source

D = Decimal
A = kaspa_encode(0, bytes([1]) * 32)
E = kaspa_encode(0, bytes([2]) * 32)
P2SH = kaspa_encode(8, bytes([3]) * 32)
NOW_MS = 1_780_000_000_000
T0 = 1_772_445_600_000
KAS = 100_000_000


def txid(n: int) -> str:
    return f"{n:064x}"


def tx(n: int, t_ms: int, ins: list[tuple[str, int]], outs: list[tuple[str, int]], coinbase: bool = False) -> dict:
    return {"subnetwork_id": "01" + "0" * 38 if coinbase else "0" * 40, "transaction_id": txid(n), "hash": txid(n),
            "mass": "2036", "payload": None, "block_hash": ["ab" * 32], "block_time": t_ms, "is_accepted": True,
            "accepting_block_hash": "cd" * 32, "accepting_block_blue_score": 1000 + n,
            "inputs": [{"transaction_id": txid(n), "index": i, "previous_outpoint_hash": txid(9000 + n),
                        "previous_outpoint_index": "0", "previous_outpoint_address": a,
                        "previous_outpoint_amount": v} for i, (a, v) in enumerate(ins)],
            "outputs": [{"transaction_id": txid(n), "index": i, "amount": v, "script_public_key_address": a,
                         "script_public_key_type": "scripthash" if a.startswith("kaspa:p") else "pubkey"}
                        for i, (a, v) in enumerate(outs)]}


def history() -> list[dict]:
    return [
        tx(1, T0 + 1_000, [(E, 2000 * KAS)], [(A, 1000 * KAS), (E, 999 * KAS)]),
        tx(2, T0 + 2_000, [(A, 1000 * KAS)], [(E, 100 * KAS), (A, 899 * KAS + 99_000_000)]),   # Gebühr 0,01
        tx(3, T0 + 3_000, [(A, 899 * KAS + 99_000_000)], [(P2SH, 30_000_000), (A, 899 * KAS + 68_980_000)]),
        tx(4, T0 + 3_500, [(P2SH, 30_000_000)], [(A, 29_990_000)]),                             # Reveal
        tx(5, T0 + 4_000, [], [(A, 50 * KAS)], coinbase=True),
        tx(6, T0 + 5_000, [(A, 29_990_000), (A, 50 * KAS)], [(A, 50 * KAS + 29_980_000)]),      # Umbuchung
        tx(7, T0 + 6_000, [(A, 100 * KAS), (E, 10 * KAS)], [(A, 99 * KAS), (E, 11 * KAS - 1000)]),
    ]


def op(n: int, kind: str, tick: str, amt: int, frm: str, to: str, *, accept: str = "1", age_ms: int = 3_600_000):
    return {"p": "KRC-20", "op": kind, "tick": tick, "amt": str(amt), "from": frm, "to": to,
            "opScore": str(900_000_000 + n), "hashRev": txid(5000 + n), "feeRev": "100000", "txAccept": "1",
            "opAccept": accept, "opError": "", "mtsAdd": str(NOW_MS - age_ms), "mtsMod": str(NOW_MS - age_ms)}


def ops() -> list[dict]:
    return [op(1, "transfer", "NACHO", 100 * KAS, E, A), op(2, "mint", "KASPY", 1000 * KAS, "", A),
            op(3, "transfer", "NACHO", 50 * KAS, A, E), op(4, "list", "NACHO", 10 * KAS, A, A),
            op(5, "transfer", "NACHO", 5 * KAS, E, A, accept="-1"),
            op(6, "transfer", "NACHO", 7 * KAS, E, A, age_ms=60_000)]


@pytest.fixture
def kas(monkeypatch):
    fake = FakeKaspa(history(), {A: 1000 * KAS}, ops(),
                     [{"tick": "NACHO", "balance": str(57 * KAS), "locked": "0", "dec": "8", "opScoreMod": "1"},
                      {"tick": "KASPY", "balance": str(1000 * KAS), "locked": "0", "dec": "8", "opScoreMod": "1"}],
                     {"NACHO": 8, "KASPY": 8})
    sleeps: list[float] = []
    monkeypatch.setattr(WalletConnector, "transport", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(WalletConnector, "sleep", staticmethod(sleeps.append))
    monkeypatch.setattr(CH, "_LIMITERS", {})
    monkeypatch.setattr(KA.KaspaConnector, "now_ms", staticmethod(lambda: NOW_MS))
    fake.sleeps = sleeps  # type: ignore[attr-defined]
    return fake


@pytest.fixture
def client(config, monkeypatch):
    monkeypatch.setenv("PORTFOLIA_MASTER_KEY", MASTER)
    with make_client(config) as c:
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        from app.journal.service import journal_service
        journal_service(ctx(c)).save_asset({"asset_id": "KAS", "name": "Kaspa", "asset_class": "crypto",
                                            "quote_source": "none"})
        yield c


def sync(c, sid):
    return datasource_service(ctx(c)).sync(sid, "manual")


def rows_for(c, sid, n: int, krc: bool = False) -> list:
    key = f"kaspa:krc20-{txid(n)}:{A}#" if krc else f"kaspa:{txid(n)}:{A}#"
    return [v for k, v in all_rows(c, sid).items() if k.startswith(key)]


def test_kas_history_and_krc20(client, kas):
    sid = create_wallet(client, "kaspa", A, name="Kaspium KAS", group="Kaspium")
    res = sync(client, sid)
    assert res["status"] == "synced", res
    (dep,) = rows_for(client, sid, 1)
    assert (dep.rec.kind, dep.rec.in_qty, dep.rec.review) == ("deposit", D("1000"), None)
    (wd,) = rows_for(client, sid, 2)
    assert (wd.rec.kind, wd.rec.out_qty, wd.rec.fee_qty) == ("withdrawal", D("100"), D("0.01"))
    (commit,) = rows_for(client, sid, 3)
    (reveal,) = rows_for(client, sid, 4)
    assert commit.rec.kind == reveal.rec.kind == "fee" and "KRC-20" in commit.rec.note
    assert commit.rec.fee_qty == D("0.0002") and reveal.rec.fee_qty == D("0.0001")
    (cb,) = rows_for(client, sid, 5)
    assert cb.rec.kind == "deposit" and "Coinbase" in cb.rec.review
    (comp,) = rows_for(client, sid, 6)
    assert comp.rec.kind == "fee" and "eigene Adresse" in comp.rec.note
    (mixed,) = rows_for(client, sid, 7)
    assert "fremden Eingängen" in mixed.rec.review and mixed.rec.out_qty == D("1")
    (k1,) = rows_for(client, sid, 5001, krc=True)
    assert (k1.rec.kind, k1.rec.in_sym, k1.rec.in_qty, k1.rec.review) == ("deposit", "NACHO@KAS:NACHO", D("100"),
                                                                            None)
    (k2,) = rows_for(client, sid, 5002, krc=True)
    assert "Mint" in k2.rec.review and k2.rec.in_sym == "KASPY@KAS:KASPY"
    (k3,) = rows_for(client, sid, 5003, krc=True)
    assert (k3.rec.kind, k3.rec.out_qty) == ("withdrawal", D("50"))
    assert not rows_for(client, sid, 5004, krc=True) and not rows_for(client, sid, 5005, krc=True)
    assert not rows_for(client, sid, 5006, krc=True)  # zu jung → nächster Lauf
    assert "abgelehnte KRC-20" in res["message"]
    assert balances(client, sid) == {"KAS": "1000", "NACHO@KAS:NACHO": "57", "KASPY@KAS:KASPY": "1000"}
    cur = json.loads(source(client, sid)["cursor_json"])
    assert cur["after"] == T0 + 6_000 - KA.OVERLAP_MS or cur["after"] == 0
    # später: die junge Operation ist alt genug → über „prev“ (aufsteigend) nachgeholt
    KA.KaspaConnector.now_ms = staticmethod(lambda: NOW_MS + 3_600_000)
    kas.calls.clear()
    sync(client, sid)
    (k6,) = rows_for(client, sid, 5006, krc=True)
    assert k6.rec.in_qty == D("7")
    assert any(r.url.params.get("prev") for r in kas.calls if r.url.path.endswith("/oplist"))


def test_krc20_outage_keeps_kas_complete_and_shows_gap(client, kas):
    sid = create_wallet(client, "kaspa", A, name="Kaspium KAS")
    for _ in range(6):
        kas.fail_next(httpx.Response(503), lambda r: r.url.host == "api.kasplex.org")
    sync(client, sid)
    st = source(client, sid)
    cov = json.loads(st["coverage_json"])
    assert st["status"] == "partial" and any("KRC-20 nicht abrufbar" in g for g in cov["gaps"])
    assert rows_for(client, sid, 2) and not rows_for(client, sid, 5001, krc=True)  # KAS vollständig
    ds = datasource_service(ctx(client)).get(sid)
    assert ds.sync_state[0] != "vollständig synchronisiert"
    assert "KRC-20 nicht abrufbar" in client.get("/settings/datasources").text
    kas.inject.clear()
    sync(client, sid)
    assert rows_for(client, sid, 5001, krc=True) and source(client, sid)["status"] == "synced"


def test_krc20_unsynced_indexer_is_a_gap(client, kas):
    kas.krc_status = "unsynced"
    sid = create_wallet(client, "kaspa", A, name="Kaspium KAS")
    sync(client, sid)
    cov = json.loads(source(client, sid)["coverage_json"])
    assert any("nicht synchron" in g for g in cov["gaps"]) and not cov["complete"]


def test_krc20_disabled_is_a_visible_limit(client, kas):
    sid = create_wallet(client, "kaspa", A, name="Kaspium KAS", tokens_shown="1", tokens="")
    assert sync(client, sid)["status"] == "synced"
    assert not [r for r in kas.calls if r.url.host == "api.kasplex.org"]
    ds = datasource_service(ctx(client)).get(sid)
    assert any("KRC-20 nicht abgerufen" in lim for lim in ds.limits)


def test_kas_pages_with_equal_block_times_resume_and_overlap(client, kas, monkeypatch):
    kas.txs = [tx(100 + i, T0 + (i // 3) * 1000, [(E, 2 * KAS)], [(A, KAS + i)]) for i in range(1300)]
    sid = create_wallet(client, "kaspa", A, name="Viele", tokens_shown="1", tokens="")
    monkeypatch.setattr(KA.KaspaConnector, "max_requests", 2)
    sync(client, sid)
    st = source(client, sid)
    assert st["status"] == "partial" and json.loads(st["coverage_json"])["resume"]
    monkeypatch.setattr(KA.KaspaConnector, "max_requests", 2500)
    while datasource_service(ctx(client)).get(sid).backfill_pending:
        sync(client, sid)
    rows = all_rows(client, sid)
    assert len(rows) == 1300 and len(set(rows)) == 1300
    kas.calls.clear()
    res = sync(client, sid)  # Überlappung: letzte 30 Minuten erneut, nichts doppelt
    assert res.get("new", 0) == 0 and len(all_rows(client, sid)) == 1300
    afters = [int(r.url.params["after"]) for r in kas.calls if "full-transactions-page" in r.url.path]
    assert afters and afters[0] < T0 + (1299 // 3) * 1000


# -- KRC-20: HTTP 403 des Kasplex-Indexers (go-krc20d) ------------------------------------------------------------
# Laut Quelltext von go-krc20d (api/v1op.go, v1address.go, v1info.go) antwortet der Indexer auf Anwendungsfehler mit
# HTTP 403 und einer Meldung im JSON-Rumpf; Kasplex verlangt keinen Schlüssel.

def _oplist(r: httpx.Request) -> bool:
    return r.url.host == "api.kasplex.org" and r.url.path.endswith("/krc20/oplist")


def _gaps(c, sid) -> list[str]:
    return json.loads(source(c, sid)["coverage_json"])["gaps"]


def test_krc20_403_unsynced_is_transient_retried_and_never_a_key_problem(client, kas):
    sid = create_wallet(client, "kaspa", A, name="Kaspium KAS")
    kas.fail_next(httpx.Response(403, json={"message": "unsynced", "result": None}), _oplist)
    assert sync(client, sid)["status"] == "synced"  # nach kurzer Pause wiederholt – erfolgreich
    assert rows_for(client, sid, 5001, krc=True) and 5.0 in kas.sleeps
    for _ in range(3):
        kas.fail_next(httpx.Response(403, json={"message": "unsynced", "result": None}), _oplist)
    datasource_service(ctx(client)).reset_cursor(sid)
    sync(client, sid)
    gaps = _gaps(client, sid)
    assert any("nicht synchron" in g and "HTTP 403" in g for g in gaps), gaps
    assert not any("Schlüssel" in g for g in gaps)
    cov = json.loads(source(client, sid)["coverage_json"])
    assert cov["krc20"]["status"] == "unavailable" and rows_for(client, sid, 2)  # KAS vollständig


def test_krc20_api3_unsynced_on_all_endpoints_is_a_gap(client, kas):
    kas.krc_status = "unsynced403"
    sid = create_wallet(client, "kaspa", A, name="Kaspium KAS")
    res = sync(client, sid)
    assert res["status"] == "partial" and rows_for(client, sid, 2)
    gaps = _gaps(client, sid)
    assert any("nicht synchron" in g for g in gaps) and not any("Schlüssel" in g for g in gaps)


@pytest.mark.parametrize(("resp", "kind", "text"), [
    (httpx.Response(403, json={"message": "internal error", "result": None}), "unavailable", "internen Fehler"),
    (httpx.Response(403, json={"message": "address invalid", "result": None}), "config", "lehnt die Adresse ab"),
    (httpx.Response(403, json={"message": "data expired", "result": None}), "no_data", "nicht mehr vor"),
    (httpx.Response(403, json={"message": "something new", "result": None}), "forbidden", "„something new“"),
    (httpx.Response(403, text="<html>Attention Required! | Cloudflare</html>",
                    headers={"content-type": "text/html", "server": "cloudflare", "cf-ray": "8f1e2d3c4b5a-FRA"}),
     "forbidden", "Cloudflare"),
    (httpx.Response(404, json={"message": "not found"}), "gone", "nicht gefunden"),
])
def test_krc20_error_classes(client, kas, resp, kind, text):
    sid = create_wallet(client, "kaspa", A, name="Kaspium KAS")
    for _ in range(4):
        kas.fail_next(resp, _oplist)
    sync(client, sid)
    cov = json.loads(source(client, sid)["coverage_json"])
    assert cov["krc20"]["status"] == kind, cov["krc20"]
    gaps = cov["gaps"]
    assert any(text in g for g in gaps), gaps
    assert not any("Schlüssel unter" in g for g in gaps)  # Kasplex hat keine Schlüssel
    assert rows_for(client, sid, 2) and not rows_for(client, sid, 5001, krc=True)  # KAS ja, KRC-20 nicht
    assert json.loads(source(client, sid)["cursor_json"]).get("krc20") in ({}, None)  # KRC-Zeiger nicht vorgerückt
