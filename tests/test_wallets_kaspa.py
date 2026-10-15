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


# -- Zeitplan, Stillstand und Teilabrufe (Regression: wiederholtes Synchronisieren trotz „einmal täglich“) -----------

def _page_wallet(client, **form) -> int:
    return create_wallet(client, "kaspa", A, name="Kaspium KAS", tokens_shown="1", tokens="", **form)


def _gap_hist(n: int) -> list[dict]:
    return [tx(100 + i, T0 + i * 1000, [(E, 2 * KAS)], [(A, KAS + i)]) for i in range(n)]


def _lag(row: dict) -> float:
    from datetime import datetime
    return (datetime.fromisoformat(row["next_run_at"]) - datetime.fromisoformat(row["last_run_at"])).total_seconds()


def test_daily_interval_holds_after_an_incomplete_kaspa_fetch(client, kas, monkeypatch):
    kas.txs = _gap_hist(40)
    monkeypatch.setattr(KA, "PAGE", 5)
    monkeypatch.setattr(KA.KaspaConnector, "max_requests", 2)
    sid = _page_wallet(client, sync_interval_min="1440")
    ds = datasource_service(ctx(client)).get(sid)
    assert not ds.catchup  # Wallet: Fortsetzung in kurzen Abständen nur mit ausdrücklicher Einstellung
    datasource_service(ctx(client)).sync(sid, "schedule")
    ds = datasource_service(ctx(client)).get(sid)
    assert ds.backfill_pending and ds.sched["next_kind"] == "schedule"
    assert 23 * 3600 < _lag(source(client, sid)) <= 24 * 3600 + 5  # nicht Start + 90 s
    assert datasource_service(ctx(client)).due() == []
    assert "Aufholen aus" in ds.sched["next_reason"]
    assert [p["state"] for p in ds.parts if p["key"] == "kas"] == ["progress"]


def test_explicit_catchup_continues_quickly_but_bounded_and_completes_without_gaps(client, kas, monkeypatch):
    kas.txs = _gap_hist(60)
    monkeypatch.setattr(KA, "PAGE", 5)
    monkeypatch.setattr(KA.KaspaConnector, "max_requests", 2)
    sid = _page_wallet(client, sync_interval_min="1440", catchup_shown="1", catchup="1")
    svc = datasource_service(ctx(client))
    assert svc.get(sid).catchup and json.loads(source(client, sid)["watch_json"])["catchup"] is True
    seen_cursors = set()
    n = 0
    while svc.get(sid).backfill_pending or n == 0:
        out = svc.sync(sid, "catchup" if n else "schedule")
        assert "error" not in out, out
        cur = source(client, sid)["cursor_json"]
        if svc.get(sid).backfill_pending:
            assert cur not in seen_cursors  # jede unvollständige Etappe rückt nachweislich vor
        seen_cursors.add(cur)
        n += 1
        assert n <= S_CATCHUP_MAX + 25
    rows = all_rows(client, sid)
    assert len(rows) == 60 and len(set(rows)) == 60  # lückenlos, keine Doppelten
    assert svc.get(sid).sync_state[0] == "vollständig synchronisiert"
    ids = [r["event_key"] for r in ctx(client).db.q(
        "SELECT r.event_key FROM csv_row r JOIN csv_batch b ON b.id=r.batch_id WHERE b.datasource_id=?", (sid,))]
    assert len(ids) == len(set(ids))


S_CATCHUP_MAX = 12


def test_kas_pagination_without_progress_is_detected(client, kas, monkeypatch):
    kas.txs = _gap_hist(40)
    monkeypatch.setattr(KA, "PAGE", 5)
    first = sorted(kas.txs, key=lambda t: t["block_time"])[:5]
    real = kas.handler

    def stuck(req):
        if req.url.host == "api.kaspa.org" and "full-transactions-page" in req.url.path and kas.page_calls >= 1:
            kas.calls.append(req)
            return httpx.Response(200, json=sorted(first, key=lambda t: -t["block_time"]))  # immer dieselbe Seite
        if req.url.host == "api.kaspa.org" and "full-transactions-page" in req.url.path:
            kas.page_calls += 1
        return real(req)

    kas.page_calls = 0  # type: ignore[attr-defined]
    monkeypatch.setattr(WalletConnector, "transport", httpx.MockTransport(stuck))
    sid = _page_wallet(client, sync_interval_min="60", catchup_shown="1", catchup="1")
    out = sync(client, sid)
    st = source(client, sid)
    cov = json.loads(st["coverage_json"])
    assert out["status"] == "partial" and st["status"] == "partial"
    assert cov["sched"]["stalled"] and "KAS: Paginierung ohne Fortschritt" in cov["sched"]["stalled"]
    pages = [r for r in kas.calls if "full-transactions-page" in r.url.path]
    assert len(pages) <= 3  # nicht endlos weiterfragen
    assert json.loads(st["cursor_json"])["after"] == first[-1]["block_time"]  # nur verarbeitete Seiten
    assert len(all_rows(client, sid)) == 5  # die erste Seite ist verarbeitet, nichts übersprungen
    assert cov["parts"]["kas"]["state"] == "blocked"
    ds = datasource_service(ctx(client)).get(sid)
    assert ds.sync_state[0] == "Abruf unvollständig – Stillstand erkannt"
    assert _lag(st) >= 3500  # kein zügiger Neustart: reguläres Intervall (60 min)


def test_unchanged_full_krc20_page_does_not_repeat_endlessly(client, kas, monkeypatch):
    many = [op(n, "transfer", "NACHO", KAS, E, A) for n in range(1, 61)]
    kas.ops = many
    real = kas.handler
    newest = sorted(many, key=lambda o: -int(o["opScore"]))[:50]

    def same_page(req):
        if req.url.host == "api.kasplex.org" and req.url.path.endswith("/krc20/oplist") and (
                req.url.params.get("next") or req.url.params.get("prev")):
            kas.calls.append(req)
            return httpx.Response(200, json={"message": "successful", "result": newest, "prev": newest[0]["opScore"],
                                             "next": newest[-1]["opScore"]})
        return real(req)

    monkeypatch.setattr(WalletConnector, "transport", httpx.MockTransport(same_page))
    sid = create_wallet(client, "kaspa", A, name="Kaspium KAS", sync_interval_min="1440", catchup_shown="1",
                        catchup="1")
    sync(client, sid)
    cov = json.loads(source(client, sid)["coverage_json"])
    assert cov["sched"]["stalled"] and "dieselbe volle Seite" in cov["sched"]["stalled"]
    assert cov["parts"]["krc20"]["state"] == "blocked" and cov["parts"]["kas"]["state"] == "complete"
    assert len([r for r in kas.calls if _oplist(r)]) <= 3
    st = source(client, sid)
    assert st["status"] == "partial" and _lag(st) > 23 * 3600  # reguläres Tagesintervall, keine Schleife
    cur = json.loads(st["cursor_json"])["krc20"]
    assert cur["back"] == newest[-1]["opScore"] and not cur["done"]  # Zeiger nur über verarbeitete Seite
    n_calls = len([r for r in kas.calls if _oplist(r)])
    sync(client, sid)  # erneuter Lauf: wieder begrenzt, Zähler für „ohne Fortschritt“ steigt
    assert len([r for r in kas.calls if _oplist(r)]) - n_calls <= 3
    assert datasource_service(ctx(client)).get(sid).sched["stalls"] >= 2


def test_budget_end_in_the_middle_of_a_krc20_page_loses_nothing(client, kas, monkeypatch):
    ticks = [f"T{n:03d}" for n in range(50)]  # 50 verschiedene Tokens → je eine Anfrage für die Nachkommastellen
    kas.ops = [op(100 + i, "transfer", t, KAS, E, A) for i, t in enumerate(ticks)]
    monkeypatch.setattr(KA.KaspaConnector, "max_requests", 40)  # zu knapp für alle Tokens
    sid = create_wallet(client, "kaspa", A, name="Kaspium KAS")
    n = 0
    first_back = "unset"
    while True:
        out = sync(client, sid)
        n += 1
        assert n <= 6, out
        if n == 1:
            cur = json.loads(source(client, sid)["cursor_json"])["krc20"]
            first_back = cur.get("back")
            assert len(cur["dec"]) >= 20  # Teilfortschritt gesichert
        if not datasource_service(ctx(client)).get(sid).backfill_pending:
            break
    assert n >= 2 and first_back is None  # Zeiger nicht über die unvollständige Seite hinaus
    rows = {k for k in all_rows(client, sid) if k.startswith("kaspa:krc20-")}
    assert len(rows) == 50  # keine Operation verloren, keine doppelt
    assert source(client, sid)["status"] == "synced"


def test_indexer_outage_keeps_kas_visible_and_token_balances(client, kas):
    sid = create_wallet(client, "kaspa", A, name="Kaspium KAS", sync_interval_min="1440", catchup_shown="1",
                        catchup="1")
    assert sync(client, sid)["status"] == "synced"
    assert balances(client, sid) == {"KAS": "1000", "NACHO@KAS:NACHO": "57", "KASPY@KAS:KASPY": "1000"}
    kas.bal[A] = 1200 * KAS
    for _ in range(8):
        kas.fail_next(httpx.Response(503), lambda r: r.url.host == "api.kasplex.org")
    sync(client, sid)
    st = source(client, sid)
    cov = json.loads(st["coverage_json"])
    assert st["status"] == "partial" and cov["parts"]["kas"]["state"] == "complete"
    assert cov["parts"]["krc20"]["state"] == "blocked"
    bal = balances(client, sid)
    assert bal["KAS"] == "1200"  # KAS-Erfolg bleibt erkennbar
    assert bal["NACHO@KAS:NACHO"] == "57" and bal["KASPY@KAS:KASPY"] == "1000"  # nie als Nullbestand
    notes = {r["asset_key"]: r["note"] for r in ctx(client).db.q("SELECT asset_key, note FROM ds_balance WHERE "
                                                                 "source_id=?", (sid,))}
    assert "Stand des letzten vollständigen Abrufs" in (notes["NACHO@KAS:NACHO"] or "")
    assert not notes["KAS"]
    assert _lag(st) > 23 * 3600  # Indexer-Ausfall ist kein Grund für eine zügige Wiederholung
    page = client.get(f"/settings/datasources/{sid}").text
    assert "KAS · vollständig" in page and "KRC-20 · blockiert" in page
    assert "Abruf unvollständig (Lücke/Ausfall)" in page and "Letzter Start" in page  # Grund des Folgetermins
    # auch die Verbindungsprüfung löscht bei KRC-20-Ausfall keine bekannten Token-Bestände
    for _ in range(8):
        kas.fail_next(httpx.Response(503), lambda r: r.url.host == "api.kasplex.org")
    datasource_service(ctx(client)).check(sid)
    assert balances(client, sid)["NACHO@KAS:NACHO"] == "57"
    kas.inject.clear()
    assert sync(client, sid)["status"] == "synced"
    assert "Stand des letzten" not in " ".join(
        str(r["note"] or "") for r in ctx(client).db.q("SELECT note FROM ds_balance WHERE source_id=?", (sid,)))


def test_rate_limit_waits_after_the_run_and_does_not_loop(client, kas):
    sid = create_wallet(client, "kaspa", A, name="Kaspium KAS", sync_interval_min="60", catchup_shown="1",
                        catchup="1", tokens_shown="1", tokens="")
    for _ in range(10):
        kas.fail_next(httpx.Response(429, headers={"Retry-After": "7200"}), lambda r: r.url.host == "api.kaspa.org")
    out = sync(client, sid)
    assert "error" in out
    st = source(client, sid)
    assert _lag(st) >= 7200 and st["status"] == "error"  # Wartezeit des Anbieters, kein erneuter Versuch davor
    assert len([r for r in kas.calls if r.url.host == "api.kaspa.org"]) <= 3
    assert datasource_service(ctx(client)).due() == []
    assert "Wartezeit" in datasource_service(ctx(client)).get(sid).sched["next_reason"]


def test_manual_wallet_never_starts_by_itself_and_continues_on_demand(client, kas, monkeypatch):
    kas.txs = _gap_hist(40)
    monkeypatch.setattr(KA, "PAGE", 5)
    monkeypatch.setattr(KA.KaspaConnector, "max_requests", 2)
    sid = _page_wallet(client, sync_interval_min="0", catchup_shown="1", catchup="1")
    svc = datasource_service(ctx(client))
    sync(client, sid)
    st = source(client, sid)
    assert svc.get(sid).backfill_pending and st["next_run_at"] is None and svc.due() == []
    assert svc.run_due(wait=True)["ran"] == 0
    before = st["cursor_json"]
    sync(client, sid)  # manuell: setzt am gespeicherten Fortsetzungspunkt fort
    assert source(client, sid)["cursor_json"] != before


def test_catchup_setting_roundtrip_in_the_form(client, kas):
    sid = _page_wallet(client, sync_interval_min="1440")
    assert 'name="catchup"' in client.get(f"/settings/datasources/{sid}").text
    assert 'name="catchup" value="1" checked' not in client.get(f"/settings/datasources/{sid}").text
    r = client.post(f"/settings/datasources/{sid}", data={
        "csrf_token": client.token, "kind": "wallet", "provider": "kaspa", "name": "Kaspium KAS", "address": A,
        "account": "Kaspium KAS", "sync_interval_min": "1440", "tokens_shown": "1", "catchup_shown": "1",
        "catchup": "1"}, follow_redirects=False)
    assert r.status_code == 303 and datasource_service(ctx(client)).get(sid).catchup
    assert 'name="catchup" value="1" checked' in client.get(f"/settings/datasources/{sid}").text
    # ohne das Formularfeld (z. B. andere Änderung) bleibt die Einstellung erhalten
    client.post(f"/settings/datasources/{sid}", data={
        "csrf_token": client.token, "kind": "wallet", "provider": "kaspa", "name": "Kaspium KAS", "address": A,
        "account": "Kaspium KAS", "sync_interval_min": "1440", "tokens_shown": "1"}, follow_redirects=False)
    assert datasource_service(ctx(client)).get(sid).catchup
