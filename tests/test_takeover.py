"""Quellenübernahme (API maßgeblich) – Pilot Bitpanda: Beweis am Bestand, Vorschau, Übernahme mit Nachprüfung und
Rücknahme, Spender (EUR-Werte/Einordnung), bleibende Transfers, Abbruch bei abweichendem Bestand.

Der Mock ist ``FakeBitpanda`` aus ``test_bitpanda`` (Vertrag laut Referenz); der kuratierte Import ist ein
Koinly-ähnlicher Export (Bitpanda-Konto, einige Zeilen mit Anbieter-ID, einige ohne)."""

# ruff: noqa: F811  (Fixtures aus test_bitpanda werden importiert und als Argumente verwendet)
from __future__ import annotations

import copy
import json
import re
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.csvimport.service import csv_service
from app.datasources import takeover as TK
from app.datasources.service import datasource_service
from app.diagnosis import actions as A
from app.jobs import tasks
from tests.helpers import tx
from tests.test_bitpanda import (  # noqa: F401  (Fixtures)
    U,
    _import_zip,
    api,
    client,
    create_bitpanda,
    ctx,
    load,
    make_client,
    master,
    op_key,
    post,
    sync,
)

D = Decimal


def ops(*numbers: int) -> list[dict]:
    wanted = {U(0x100 + n) for n in numbers}
    out = []
    for f in ("operations_page1.json", "operations_page2.json", "operations_page3.json"):
        out += [o for o in load(f)["data"] if o["operation_id"] in wanted]
    return sorted(out, key=lambda o: o["operation_id"])


def portfolio(eur: str, btc: str) -> dict:
    p = copy.deepcopy(load("portfolio.json"))
    keep = []
    for h in p["data"]:
        val = h["balance"]["value"]
        if h.get("asset_id") is None:
            h["balance"]["value"] = h["available_balance"]["value"] = eur
            keep.append(h)
        elif h["asset_id"].endswith("0b1"):
            h["balance"]["value"] = h["available_balance"]["value"] = btc
            keep.append(h)
        del val
    p["data"] = keep
    return p



def setup_account(api, config, rows=None, btc="0.00445"):
    """Konto mit 500 € Einzahlung, Kauf 0,0055 BTC für 250 € und Auszahlung 0,001 BTC (+ 0,00005 BTC Gebühr).
    Der Import (Koinly-ähnlich) führt die Auszahlung ohne Gebühr, mit Einordnung „gift“ und Wert 55 €."""
    api.set_pages({"data": ops(1, 3, 4), "self_cursor": None, "next_cursor": None, "has_next_page": False})
    api.portfolio = portfolio("250", btc)
    _import_zip(config, rows or [
        tx("K1", "2024-02-01T08:00:00Z", "deposit", to=("Bitpanda", "EUR", "500"), value="500"),
        tx("K2", "2024-02-02T09:01:00Z", "buy", frm=("Bitpanda", "EUR", "250"), to=("Bitpanda", "BTC", "0.0055"),
           value="251.5"),
        tx("K3", "2024-02-12T12:00:00Z", "withdrawal", tag="gift", frm=("Bitpanda", "BTC", "0.001"), value="55"),
        tx("K9", "2024-02-20T12:00:00Z", "deposit", to=("Bitpanda", "BTC", "1"), value="40000"),  # Phantom
        tx("K10", "2024-02-21T12:00:00Z", "withdrawal", frm=("Bitpanda", "BTC", "1"), value="40000"),
    ])


def started(config, api, **kw):
    """Client mit Import und Datenquelle; liefert (client, sid)."""
    setup_account(api, config, **kw)
    c = make_client(config)
    c.__enter__()
    c.get("/settings")
    c.token = c.cookies.get("portfolia_csrf")
    sid = create_bitpanda(c)
    assert tasks.import_check(ctx(c), "test").status == "imported"
    return c, sid


def holdings(c, account="Bitpanda"):
    return dict(ctx(c).ledger().holdings_by_account(account))


def ids(c):
    return {t.tx_id for t in ctx(c).recorded_portfolio().txs}


def sync_txs(c, status="active"):
    return [dict(r) for r in ctx(c).db.q("SELECT * FROM journal_tx WHERE source='sync:bitpanda' AND status=? "
                                         "ORDER BY id", (status,))]


def staged(c, sid):
    out = TK.run_fetch(ctx(c), sid)
    assert not out["errors"], out
    a = TK.assess(ctx(c), sid)
    assert a is not None
    return a


def test_proof_preview_apply_and_undo(config, master, api):
    c, sid = started(config, api)
    try:
        before = holdings(c)
        assert before == {"EUR": D("250"), "BTC": D("0.0045")}  # Phantom-Paar K9/K10 hebt sich auf, Gebühr fehlt
        a = staged(c, sid)
        assert a.ok, [(x.label, x.detail) for x in a.blockers]
        # Beweis: Vorgänge der API ergeben den beobachteten Bestand exakt – der Import weicht ab (Gebühr fehlt)
        bal = {b["asset"]: b for b in a.balances}
        assert bal["BTC"]["now"] == D("0.0045") and bal["BTC"]["after"] == D("0.00445") == bal["BTC"]["observed"]
        assert bal["BTC"]["ok"] and not bal["BTC"]["was_ok"] and bal["EUR"]["ok"] and bal["EUR"]["was_ok"]
        # Vorschau: 5 Import-Buchungen, 3 mit Gegenstück; Phantom-Paar ohne Gegenstück ist ein Warnsignal
        assert len(a.hide) == 5 and a.matched == 3
        assert {t.tx_id for t in a.unmatched_old} == {"K9", "K10"}
        assert a.unmatched_new == [] and a.kept == []
        # Spender: nur wo die API den Wert nicht liefert (Auszahlung); beim Kauf zählt der Wert der API (250 €)
        assert a.donors[f"{op_key(4)}#0"]["value_eur"] == "55" and a.donors[f"{op_key(4)}#0"]["tag"] == "gift"
        res = TK.apply(ctx(c), sid, a.token)
        assert res["errors"] == [], res
        assert holdings(c) == {"EUR": D("250"), "BTC": D("0.00445")}
        assert not ({"K1", "K2", "K3", "K9", "K10"} & ids(c))
        rows = {r["event_key"]: r for r in sync_txs(c)}
        assert set(rows) == {op_key(1), op_key(3), op_key(4)}
        wd = rows[op_key(4)]
        assert D(wd["value_eur"]) == D("55") and wd["tag"] == "gift"  # Wert und Einordnung aus dem Import
        buy = rows[op_key(1)]
        assert D(buy["value_eur"]) == D("250")  # fiat-implizit aus der API, nicht 251,5
        t = TK.latest(ctx(c).db, sid)
        assert t["status"] == "applied" and t["decision_id"] and t["action_id"]
        dec = ctx(c).db.q1("SELECT kind, status FROM diag_decision WHERE id=?", (t["decision_id"],))
        assert dec["kind"] == "takeover" and dec["status"] == "active"
        assert "Bestand exakt" in t["message"]
        # die Korrektur ist in der Diagnose sichtbar, lässt sich dort aber nicht einzeln zurücknehmen (sonst doppelt)
        assert "Quellenübernahme" in c.get("/quality/diagnose").text
        solo = post(c, f"/quality/diagnose/decision/{t['decision_id']}/undo")
        assert solo.status_code == 303
        assert "K1" not in ids(c) and holdings(c) == {"EUR": D("250"), "BTC": D("0.00445")}
        res_solo = A.undo(ctx(c), int(t["decision_id"]))
        assert res_solo.errors and "Quellenübernahme" in res_solo.errors[0] and "K1" not in ids(c)
        # kein offener Prüf-Stapel der Quelle, künftige Läufe buchen nichts doppelt
        assert not datasource_service(ctx(c)).pending_batches(sid)
        sync(c, sid)
        assert len(sync_txs(c)) == 3 and not datasource_service(ctx(c)).pending_batches(sid)
        assert holdings(c) == {"EUR": D("250"), "BTC": D("0.00445")}
        # Rückgängig: Zustand wie vorher
        out = TK.undo(ctx(c), sid)
        assert out["errors"] == []
        assert holdings(c) == before and {"K1", "K2", "K3", "K9", "K10"} <= ids(c)
        assert sync_txs(c) == [] and len(sync_txs(c, "reverted")) == 3
        assert TK.latest(ctx(c).db, sid)["status"] == "undone"
        assert ctx(c).db.q1("SELECT status FROM diag_decision WHERE id=?", (t["decision_id"],))["status"] == "undone"
        assert not datasource_service(ctx(c)).pending_batches(sid)
    finally:
        c.__exit__(None, None, None)


def test_balance_mismatch_blocks_and_changes_nothing(config, master, api):
    c, sid = started(config, api, btc="0.00446")
    try:
        a = staged(c, sid)
        assert not a.ok and any(b.key == "balance" for b in a.blockers)
        assert "BTC: erklärt 0,00445, laut Quelle 0,00446" in a.blockers[0].detail
        before, n = holdings(c), ctx(c).db.scalar("SELECT COUNT(*) FROM journal_tx")
        res = TK.apply(ctx(c), sid, a.token)
        assert res["errors"] and "Beweis ist nicht erbracht" in res["errors"][0]
        assert holdings(c) == before and ctx(c).db.scalar("SELECT COUNT(*) FROM journal_tx") == n
        assert ctx(c).db.scalar("SELECT COUNT(*) FROM diag_decision WHERE kind='takeover'") == 0
    finally:
        c.__exit__(None, None, None)


def test_incomplete_fetch_and_stale_fetch_block(config, master, api):
    c, sid = started(config, api)
    try:
        a = staged(c, sid)
        assert a.ok
        old = (datetime.now(UTC) - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ctx(c).db.x("UPDATE ds_takeover SET created_at=? WHERE id=?", (old, a.rid))
        a2 = TK.assess(ctx(c), sid)
        assert not a2.ok and [b.key for b in a2.blockers] == ["fresh"]
        # Seitenende nicht belegt (Pagination hört auf, bevor die API fertig ist) → nie „vollständig“
        api.set_pages({"data": ops(1, 3, 4), "self_cursor": None, "next_cursor": "c-x", "has_next_page": True},
                      {"data": [], "self_cursor": "c-x", "next_cursor": "c-x", "has_next_page": True})
        a3 = staged(c, sid)
        assert not a3.ok and "fetch" in [b.key for b in a3.blockers]
        res = TK.apply(ctx(c), sid, a3.token)
        assert res["errors"] and not sync_txs(c)
    finally:
        c.__exit__(None, None, None)


def test_failed_postcheck_rolls_everything_back(config, master, api):
    """Die Simulation hält, die Auswertung bucht eine Zeile aber nicht (früher „dauerhaft ignoriert“): die Nachprüfung
    am Bestand schlägt an, alles wird zurückgenommen – Import, Bestand und Prüf-Stapel wie vorher."""
    c, sid = started(config, api)
    try:
        ctx(c).db.x("INSERT INTO event_decision(event_key, decision, reason, decided_at) VALUES (?, 'ignore', 'test', "
                    "'2024-01-01T00:00:00Z')", (op_key(4),))
        a = staged(c, sid)
        assert a.ok  # die Simulation kennt die Entscheidung nicht
        before, ids_before = holdings(c), ids(c)
        res = TK.apply(ctx(c), sid, a.token)
        assert res["errors"] and "Nachprüfung fehlgeschlagen" in res["errors"][0], res
        assert any("BTC" in e for e in res["errors"])
        assert holdings(c) == before and ids(c) == ids_before and sync_txs(c) == []
        t = TK.latest(ctx(c).db, sid)
        assert t["status"] == "staged" and "Zurückgenommen" in t["message"]
        assert ctx(c).db.scalar("SELECT COUNT(*) FROM csv_batch WHERE datasource_id=?", (sid,)) == 0
        assert ctx(c).db.q1("SELECT status FROM diag_decision WHERE kind='takeover'")["status"] == "undone"
        # nach Aufhebung der Entscheidung gelingt dieselbe Übernahme
        ctx(c).db.x("DELETE FROM event_decision WHERE event_key=?", (op_key(4),))
        a = TK.assess(ctx(c), sid)
        assert TK.apply(ctx(c), sid, a.token)["errors"] == []
    finally:
        c.__exit__(None, None, None)


def test_transfer_with_other_account_stays_and_covers_the_api_side(config, master, api):
    rows = [
        tx("K1", "2024-02-01T08:00:00Z", "deposit", to=("Bitpanda", "EUR", "500"), value="500"),
        tx("K2", "2024-02-02T09:01:00Z", "buy", frm=("Bitpanda", "EUR", "250"), to=("Bitpanda", "BTC", "0.0055"),
           value="250"),
        tx("K3", "2024-02-12T12:00:00Z", "transfer", frm=("Bitpanda", "BTC", "0.001"), to=("Ledger", "BTC", "0.001"),
           fee=("BTC", "0.00005", "2.5")),
    ]
    c, sid = started(config, api, rows=rows)
    try:
        assert holdings(c) == {"EUR": D("250"), "BTC": D("0.00445")}
        a = staged(c, sid)
        assert a.ok, [(x.label, x.detail) for x in a.blockers]
        assert [t.tx_id for t, _why in a.kept] == ["K3"] and a.covered == 1 and len(a.hide) == 2
        assert TK.apply(ctx(c), sid, a.token)["errors"] == []
        assert "K3" in ids(c) and not ({"K1", "K2"} & ids(c))
        assert holdings(c) == {"EUR": D("250"), "BTC": D("0.00445")}
        assert holdings(c, "Ledger") == {"BTC": D("0.001")}  # Gegenseite bleibt
        assert {r["event_key"] for r in sync_txs(c)} == {op_key(1), op_key(3)}  # Auszahlung nicht doppelt
        assert not datasource_service(ctx(c)).pending_batches(sid)
        assert TK.undo(ctx(c), sid)["errors"] == [] and {"K1", "K2", "K3"} <= ids(c)
    finally:
        c.__exit__(None, None, None)


def test_manual_bookings_stay_and_unsupported_provider_is_refused(config, master, api):
    c, sid = started(config, api)
    try:
        r = post(c, "/journal/new", kind="deposit", date="2024-02-25", account="Bitpanda", asset="EUR", qty="10")
        assert r.status_code == 303, r.text[:300]
        manual = [dict(x) for x in ctx(c).db.q("SELECT * FROM journal_tx WHERE source='manual' AND status='active'")]
        assert len(manual) == 1
        a = staged(c, sid)
        assert [t.tx_id for t, _w in a.kept] == [manual[0]["tx_id"]]  # manuelle Buchungen bleiben …
        assert not a.ok and any(b.key == "balance" for b in a.blockers)  # … und müssen durch die API gedeckt sein
        bsid = ctx(c).db.x("INSERT INTO data_source(kind, provider, name, account, status, created_at, updated_at) "
                           "VALUES ('exchange', 'kraken', 'Kraken', 'Kraken', 'created', 'x', 'x')").lastrowid
        res = TK.start_fetch(ctx(c), int(bsid))
        assert res["errors"] and "keine Quellenübernahme" in res["errors"][0]
    finally:
        c.__exit__(None, None, None)


def wait(sid, seconds=20):
    end = time.monotonic() + seconds
    while TK.busy(sid) and time.monotonic() < end:
        time.sleep(0.05)
    assert not TK.busy(sid)


def test_pages_and_routes(config, master, api):
    c, sid = started(config, api)
    try:
        page = c.get(f"/settings/datasources/{sid}/uebernahme")
        assert page.status_code == 200 and "Vollabruf starten" in page.text
        assert "Quellenübernahme" in c.get(f"/settings/datasources/{sid}").text
        r = post(c, f"/settings/datasources/{sid}/uebernahme/abruf")
        assert r.status_code == 303
        wait(sid)
        page = c.get(f"/settings/datasources/{sid}/uebernahme").text
        assert "Beweis" in page and "erbracht" in page and "Import-Buchungen ohne Gegenstück" in page
        assert not re.search(r"\bK9\b", page) and "API maßgeblich machen" in page  # Token ist zufällig
        token = TK.assess(ctx(c), sid).token
        no = post(c, f"/settings/datasources/{sid}/uebernahme/uebernehmen", token=token)  # ohne Bestätigung
        assert no.status_code == 303 and not sync_txs(c)
        stale = post(c, f"/settings/datasources/{sid}/uebernahme/uebernehmen", token="x" * 32, confirm="1")
        assert stale.status_code == 303
        wait(sid)
        assert not sync_txs(c)  # veraltete Vorschau → nichts geändert
        ok = post(c, f"/settings/datasources/{sid}/uebernahme/uebernehmen", token=token, confirm="1")
        assert ok.status_code == 303
        wait(sid)
        assert len(sync_txs(c)) == 3
        page = c.get(f"/settings/datasources/{sid}/uebernahme").text
        assert "Übernahme zurücknehmen" in page and "Übernommen" in page
        back = post(c, f"/settings/datasources/{sid}/uebernahme/zurueck")
        assert back.status_code == 303 and sync_txs(c) == []
        assert TK.latest(ctx(c).db, sid)["status"] == "undone"
        assert post(c, f"/settings/datasources/{sid}/uebernahme/zurueck").status_code == 303  # nichts mehr zu tun
    finally:
        c.__exit__(None, None, None)


def test_parallel_start_is_refused_and_interrupted_run_is_shown_as_failed(config, master, api):
    c, sid = started(config, api)
    try:
        rid = ctx(c).db.x("INSERT INTO ds_takeover(source_id, status, message, created_at) VALUES (?, 'fetching', "
                          "'läuft', '2024-01-01T00:00:00Z')", (sid,)).lastrowid
        v = TK.view(ctx(c).db, sid)
        assert v["status"] == "failed" and "unterbrochen" in v["message"]  # kein Prozess läuft → abgebrochen
        TK._RUNNING.add(sid)
        try:
            assert TK.start_fetch(ctx(c), sid)["errors"]
            assert TK.start_apply(ctx(c), sid, "x")["errors"]
        finally:
            TK._RUNNING.discard(sid)
        assert TK.start_fetch(ctx(c), sid)["errors"] == []
        wait(sid)
        assert ctx(c).db.q1("SELECT status FROM ds_takeover WHERE id=?", (rid,))["status"] == "discarded"
        assert json.loads(TK.latest(ctx(c).db, sid)["meta_json"])["complete"]
        csv_service(ctx(c))
    finally:
        c.__exit__(None, None, None)
