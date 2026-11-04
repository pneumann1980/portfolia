"""Regressionstests zum Review „Buchungen prüfen“ 0.90.0 vom 04.11.2026 (N05–N09).

Die 13 Abnahmefälle des Reviews (``test_review_090.py`` aus den Nachweisen) sind hier unverändert in ihren fachlichen
Erwartungen übernommen; ergänzt um eigene Fälle (Gebühr + Mengendifferenz, nicht übertragbare EUR-Gebühr, veraltete
Formulare im selben Lauf, angewendete Entscheidungen nach Neustart, Idempotenz der Verknüpfung). Nur synthetische Daten.

Teststeuerung N06: Der Reviewfall ersetzt ``BA.execute`` durch einen Wrapper, der zwischen der Kartenprüfung
(Fingerprint unter der CSV-Sperre) und dem Aufruf von ``execute`` einen zweiten Thread die Verknüpfungsziel-Buchung
ändern lässt. Die Korrektur prüft den Stand der abhängigen Buchungen als erste Anweisung *innerhalb* der
Schreibtransaktion von ``execute`` – der Wrapper greift also weiterhin genau vor der maßgeblichen Prüfung ein; an der
Steuerung war nichts anzupassen.
"""

# ruff: noqa: F811  (Fixtures aus anderen Testmodulen werden importiert und als Argumente verwendet)
from __future__ import annotations

import json
import threading
from copy import deepcopy
from decimal import Decimal

import pytest

from app.csvimport import batch as BA
from app.csvimport.service import csv_service
from app.datasources import takeover as TK
from tests.helpers import tx
from tests.test_bitpanda import api, ctx, master  # noqa: F401  (Fixtures)
from tests.test_nachpruefung import ProcessStopped, _all_state, _row
from tests.test_review_fixes import c, cfg, form_values, post  # noqa: F401  (Fixtures/Helfer)
from tests.test_takeover import staged, started

UNDECIDE = "/settings/datasources/{sid}/uebernahme/entscheidung/zurueck"


def _rows(fee=None, l1_qty="0.001"):
    """Import: Auszahlung K3 von Bitpanda und Eingang L1 auf Ledger (mit optionaler expliziter Empfängergebühr)."""
    rows = [
        tx("K1", "2024-02-01T08:00:00Z", "deposit", to=("Bitpanda", "EUR", "500"), value="500"),
        tx("K2", "2024-02-02T09:01:00Z", "buy", frm=("Bitpanda", "EUR", "250"),
           to=("Bitpanda", "BTC", "0.0055"), value="251.5"),
        tx("K3", "2024-02-12T12:00:00Z", "withdrawal", frm=("Bitpanda", "BTC", "0.001"), value="55"),
        tx("L1", "2024-02-12T12:30:00Z", "deposit", to=("Ledger", "BTC", l1_qty), fee=fee, value="55"),
    ]
    if fee and fee[0] not in ("BTC", ""):
        rows.append(tx("F0", "2024-01-01T12:00:00Z", "deposit", to=("Ledger", fee[0], "10"), value="20"))
    return rows


def _active(cx):
    return [dict(r) for r in cx.db.q(
        "SELECT tx_id, type, from_account, to_account, from_qty, to_qty, fee_asset, fee_qty, fee_eur "
        "FROM journal_tx WHERE status='active'")]


def _confirm_and_apply(client, sid):
    cx = ctx(client)
    a = staged(client, sid)
    problem = _row(a)
    assert problem["partner"]["kind"] == "imp" and problem["partner"]["tx"] == "L1"
    assert TK.decide(cx, sid, problem["key"], "pair", a.token)["errors"] == []
    ready = TK.assess(cx, sid)
    assert ready.ok, [(x.key, x.detail) for x in ready.blockers]
    assert TK.apply(cx, sid, ready.token)["errors"] == []
    cx.invalidate_data()
    return cx


# ---------------------------------------------------------------------------------------------------------------------
# N05 – Gebühren der ersetzten Import-Gegenbuchung bleiben erhalten (alle Konten nach der Übernahme geprüft)
# ---------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("fee", [("BTC", "0.00002", "1"), ("ETH", "0.0001", "1"), ("EUR", "1", "1")])
def test_import_transfer_counterpart_keeps_explicit_recipient_fee(config, master, api, fee):
    """Reviewfall N05: Bestand des Empfängerkontos nach Übernahme gleich, Rücknahme vollständig."""
    client, sid = started(config, api, rows=_rows(fee))
    try:
        before = _all_state(client)
        cx = _confirm_and_apply(client, sid)
        after = dict(cx.ledger().holdings_by_account("Ledger"))
        assert after == before[1]["Ledger"], {"explicit_fee": fee, "ledger_before": before[1]["Ledger"],
                                              "ledger_after": after, "active": _active(cx)}
        assert TK.undo(cx, sid)["errors"] == []
        cx.invalidate_data()
        assert _all_state(client) == before
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("fee", [("BTC", "0.00002", "1"), ("ETH", "0.0001", "1")])
def test_fee_and_received_quantity_difference_are_both_preserved(config, master, api, fee):
    """Eigener Fall: Empfänger erhält weniger als gesendet *und* trägt eine explizite Gebühr – beides bleibt; Gebühr
    in EUR, Belastungskonto und Einstand der Gegenseite unverändert, alle übrigen Konten unverändert."""
    client, sid = started(config, api, rows=_rows(fee, l1_qty="0.00097"))
    try:
        before = _all_state(client)
        cx = _confirm_and_apply(client, sid)
        assert dict(cx.ledger().holdings_by_account("Ledger")) == before[1]["Ledger"], _active(cx)
        assert {k: v for k, v in _all_state(client)[1].items() if k not in ("Bitpanda", "Ledger")} == \
            {k: v for k, v in before[1].items() if k not in ("Bitpanda", "Ledger")}
        rows = [dict(r) for r in cx.db.q("SELECT * FROM journal_tx WHERE status='active'")]
        transfer = next(r for r in rows if r["type"] == "transfer" and r["to_account"] == "Ledger")
        assert (Decimal(transfer["from_qty"]), Decimal(transfer["to_qty"])) == (Decimal("0.001"), Decimal("0.00097"))
        # Senderseite (API-Gebühr)
        assert transfer["fee_asset"] == "BTC" and Decimal(transfer["fee_qty"]) == Decimal("0.00005")
        # Empfängergebühr: eigene Gebührenbuchung am Zugangskonto, zum Transfer gruppiert, mit Asset, Menge, EUR-Wert
        fee_rows = [r for r in rows if r["group_ref"] == transfer["tx_id"] and r["tag"] == "fee"]
        assert [(r["from_account"], r["from_asset"], Decimal(r["from_qty"]), Decimal(r["value_eur"]))
                for r in fee_rows] == [("Ledger", fee[0], Decimal(fee[1]), Decimal(fee[2]))], fee_rows
        assert TK.undo(cx, sid)["errors"] == []
        cx.invalidate_data()
        assert _all_state(client) == before
    finally:
        client.__exit__(None, None, None)


def test_eur_fee_without_asset_is_not_silently_dropped(config, master, api):
    """Eigener Fall: Eine Gebühr nur als EUR-Wert (ohne Asset/Menge) lässt sich im Transfer nicht erhalten – die
    Import-Buchung wird nicht ersetzt; die bestätigte Entscheidung erscheint sichtbar als nicht anwendbar."""
    client, sid = started(config, api, rows=_rows(("", "", "1.5")))
    try:
        cx = ctx(client)
        before = _all_state(client)
        a = staged(client, sid)
        problem = _row(a)
        assert problem["partner"]["kind"] == "imp"
        assert TK.decide(cx, sid, problem["key"], "pair", a.token)["errors"] == []
        b = TK.assess(cx, sid)
        assert [t.tx_id for t in b.replaced] == []
        assert any("Gebühr" in d["why"] and "L1" in d["why"] for d in b.stale), b.stale
        res = TK.apply(cx, sid, b.token)
        cx.invalidate_data()
        if not res["errors"]:  # falls dennoch übernommen: L1 und seine Gebühr bleiben unverändert
            assert "L1" in {t.tx_id for t in cx.recorded_portfolio().txs}
            assert TK.undo(cx, sid)["errors"] == []
            cx.invalidate_data()
        assert _all_state(client) == before
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------------------------------------------------
# N07 – Einzeln buchen wirkt im zentralen Planer
# ---------------------------------------------------------------------------------------------------------------------

def test_offered_standalone_include_decision_actually_resolves_review_flag(config, master, api):
    client, sid = started(config, api)
    try:
        cx = ctx(client)
        assert not TK.run_fetch(cx, sid)["errors"]
        row = TK.latest(cx.db, sid)
        events = json.loads(row["events_json"])
        events[0]["review"] = "Synthetischer prüfbedürftiger, vollständig abbildbarer Vorgang"
        cx.db.x("UPDATE ds_takeover SET events_json=? WHERE id=?", (json.dumps(events), row["id"]))
        a = TK.assess(cx, sid)
        problem = _row(a)
        assert problem["options"] == ["include", "skip"]
        assert not TK.decide(cx, sid, problem["key"], "include", a.token)["errors"]
        b = TK.assess(cx, sid)
        assert b.ok, {"saved": b.decisions, "blockers_after_offered_include": [(x.key, x.detail) for x in b.blockers]}
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------------------------------------------------
# N08 – Entfernen ist an Lauf und Entscheidungsstand gebunden
# ---------------------------------------------------------------------------------------------------------------------

def test_decision_remove_from_old_fetch_cannot_remove_a_new_run_decision(config, master, api):
    client, sid = started(config, api, rows=_rows())
    try:
        cx = ctx(client)
        a = staged(client, sid)
        assert TK.decide(cx, sid, _row(a)["key"], "pair", a.token)["errors"] == []
        action = UNDECIDE.format(sid=sid)
        old_form = form_values(client.get(f"/settings/datasources/{sid}/uebernahme").text, action)
        b = staged(client, sid)
        assert b.rid != a.rid
        assert TK.decide(cx, sid, _row(b)["key"], "nopair", b.token)["errors"] == []
        current = TK.load_decisions(cx.db, sid, b.rid)
        result = post(client, action, old_form)
        assert TK.load_decisions(cx.db, sid, b.rid) == current, {
            "old_run": a.rid, "new_run": b.rid, "location": result.headers["location"], "old_fields": old_form}
        assert "error=" in result.headers["location"]
    finally:
        client.__exit__(None, None, None)


def test_old_remove_form_in_same_run_cannot_remove_a_changed_decision(config, master, api):
    """Eigener Fall: gleicher Lauf, Entscheidung inzwischen geändert (Zweittab) – das alte Formular löscht nichts; das
    Formular zum aktuellen Stand wirkt genau einmal."""
    client, sid = started(config, api, rows=_rows())
    try:
        cx = ctx(client)
        a = staged(client, sid)
        key = _row(a)["key"]
        assert TK.decide(cx, sid, key, "pair", a.token)["errors"] == []
        action = UNDECIDE.format(sid=sid)
        old_form = form_values(client.get(f"/settings/datasources/{sid}/uebernahme").text, action)
        b = TK.assess(cx, sid)
        assert TK.undecide(cx, sid, key, b.rid, b.decisions[key]["stamp"])["errors"] == []
        c2 = TK.assess(cx, sid)
        assert TK.decide(cx, sid, key, "nopair", c2.token)["errors"] == []
        current = TK.load_decisions(cx.db, sid, a.rid)
        assert current[key]["choice"] == "nopair"
        res = post(client, action, old_form)
        assert "error=" in res.headers["location"] and TK.load_decisions(cx.db, sid, a.rid) == current
        fresh = form_values(client.get(f"/settings/datasources/{sid}/uebernahme").text, action)
        assert "error=" not in post(client, action, fresh).headers["location"]
        assert TK.load_decisions(cx.db, sid, a.rid) == {}
        assert "error=" in post(client, action, fresh).headers["location"]  # zweites Absenden: nichts mehr da
        # ohne Lauf-/Standbezug (Formular vor 0.91.0) wird nichts entfernt
        d = TK.assess(cx, sid)
        assert TK.decide(cx, sid, key, "skip", d.token)["errors"] == []
        legacy = post(client, action, {"key": key})
        assert "error=" in legacy.headers["location"] and key in TK.load_decisions(cx.db, sid, a.rid)
    finally:
        client.__exit__(None, None, None)


def test_concurrent_remove_requests_remove_exactly_once(config, master, api):
    """Eigener Fall: zwei gleichzeitige Entfernen-Anfragen zum selben Stand – genau eine wirkt, die andere meldet
    den geänderten Stand; der gespeicherte Zustand bleibt konsistent (keine wiederbelebte Entscheidung)."""
    client, sid = started(config, api, rows=_rows())
    try:
        cx = ctx(client)
        a = staged(client, sid)
        key = _row(a)["key"]
        assert TK.decide(cx, sid, key, "pair", a.token)["errors"] == []
        stamp = TK.assess(cx, sid).decisions[key]["stamp"]
        out: list[dict] = []
        gate = threading.Barrier(2)

        def remove():
            try:
                gate.wait(5)
                out.append(TK.undecide(cx, sid, key, a.rid, stamp))
            finally:
                cx.db.close_thread_conn()

        ts = [threading.Thread(target=remove) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        assert sorted(bool(r["errors"]) for r in out) == [False, True], out
        assert TK.load_decisions(cx.db, sid, a.rid) == {}
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------------------------------------------------
# N09 – Angewendete Entscheidungen werden als angewendet gezeigt, nicht als „verworfen“
# ---------------------------------------------------------------------------------------------------------------------

def test_confirmed_import_partner_is_not_shown_as_discarded_after_success(config, master, api):
    client, sid = started(config, api, rows=_rows())
    try:
        cx = _confirm_and_apply(client, sid)
        applied = TK.assess(cx, sid)
        assert not applied.stale, {"status": TK.latest(cx.db, sid)["status"], "shown_as_stale": applied.stale}
    finally:
        client.__exit__(None, None, None)


def test_applied_decisions_are_listed_as_applied_and_locked_after_restart(config, master, api):
    """Eigener Fall: nach Übernahme und Neustart (neuer Kontext) erscheint die Entscheidung als „angewendet“, nie als
    „verworfen“; Entscheiden/Entfernen im angewendeten Lauf wird abgelehnt; nach der Rücknahme ist alles wie vorher."""
    from tests.test_bitpanda import make_client

    client, sid = started(config, api, rows=_rows())
    try:
        before = _all_state(client)
        cx = _confirm_and_apply(client, sid)
        rid = TK.latest(cx.db, sid)["id"]
        key = next(iter(TK.load_decisions(cx.db, sid, rid)))
    finally:
        client.__exit__(None, None, None)
    client = make_client(config)
    client.__enter__()
    try:
        client.get("/settings")
        client.token = client.cookies.get("portfolia_csrf")
        cx = ctx(client)
        a = TK.assess(cx, sid)
        assert not a.stale and not a.decisions
        assert [(d["key"], d["choice"]) for d in a.applied_decisions] == [(key, "pair")]
        page = client.get(f"/settings/datasources/{sid}/uebernahme").text
        assert "angewendet" in page and "verworfen" not in page
        assert UNDECIDE.format(sid=sid) not in page
        stamp = TK.decision_stamp(TK.load_decisions(cx.db, sid, rid)[key])
        assert TK.undecide(cx, sid, key, rid, stamp)["errors"]
        assert key in TK.load_decisions(cx.db, sid, rid)
        assert TK.undo(cx, sid)["errors"] == []
        cx.invalidate_data()
        assert _all_state(client) == before
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------------------------------------------------
# Erhalt: zwei API-Seiten in einem Abruf, gemeinsame Rücknahme (N03)
# ---------------------------------------------------------------------------------------------------------------------

def test_two_api_sides_in_one_saved_fetch_can_be_confirmed_and_undone(config, master, api):
    """Contract-level synthetic same-fetch case, not a claim about Bitpanda raw API coverage."""
    rows = [r for r in _rows() if r["tx_id"] != "L1"]
    client, sid = started(config, api, rows=rows)
    try:
        cx = ctx(client)
        before = _all_state(client)
        assert not TK.run_fetch(cx, sid)["errors"]
        saved = TK.latest(cx.db, sid)
        recs = json.loads(saved["events_json"])
        withdrawal = next(r for r in recs if r["kind"] == "withdrawal")
        partner = deepcopy(withdrawal)
        partner.update(kind="deposit", out_sym=None, out_qty=None, in_sym=withdrawal["out_sym"],
                       in_qty=withdrawal["out_qty"], account="Ledger", fee_sym=None, fee_qty=None,
                       fee_value=None, fee_value_ccy=None, fee_basis=None,
                       event_key="bitpanda:00000000-0000-0000-0000-00000000ffff",
                       ext_id="bitpanda:00000000-0000-0000-0000-00000000ffff#0",
                       line=999, aliases=[], label="Synthetic other account side", raw=None,
                       ts="2024-02-12T12:30:00+00:00")
        recs.append({k: v for k, v in partner.items() if v is not None})  # wie rec_to_json: ohne None
        meta = json.loads(saved["meta_json"])
        meta["coverage"]["operations"] += 1
        cx.db.x("UPDATE ds_takeover SET events_json=?, meta_json=? WHERE id=?",
                (json.dumps(recs), json.dumps(meta), saved["id"]))
        a = TK.assess(cx, sid)
        p = next(p for p in a.problems if p["kind"] == "row" and (p.get("partner") or {}).get("kind") == "b")
        assert TK.decide(cx, sid, p["key"], "pair", a.token)["errors"] == []
        b = TK.assess(cx, sid)
        assert b.ok, [(x.key, x.detail) for x in b.blockers]
        assert not TK.apply(cx, sid, b.token)["errors"]
        cx.invalidate_data()
        assert cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE type='transfer' AND status='active'") == 1
        assert dict(cx.ledger().holdings_by_account("Ledger")) == {"BTC": Decimal("0.001")}
        assert not TK.undo(cx, sid)["errors"]
        cx.invalidate_data()
        assert _all_state(client) == before
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("failure", ["false", "exception_before", "exception_after", "process_before", "process_after"])
def test_takeover_undo_resumes_when_discard_step_fails(config, master, api, monkeypatch, failure):
    client, sid = started(config, api)
    try:
        cx = ctx(client)
        before = _all_state(client)
        assert not TK.apply(cx, sid, staged(client, sid).token)["errors"]
        svc = csv_service(cx)
        real_discard = svc.discard

        def fail(*args, **kwargs):
            if failure.endswith("after"):
                real_discard(*args, **kwargs)
            if failure.startswith("process"):
                raise ProcessStopped
            if failure.startswith("exception"):
                raise RuntimeError("synthetic discard failure")
            return False

        monkeypatch.setattr(svc, "discard", fail)
        if failure.startswith("process"):
            with pytest.raises(ProcessStopped):
                TK.undo(cx, sid)
        else:
            assert TK.undo(cx, sid)["errors"]
        monkeypatch.setattr(svc, "discard", real_discard)
        assert TK.latest(cx.db, sid)["status"] == "undoing"
        assert not TK.recover(cx, sid)["errors"]
        cx.invalidate_data()
        assert _all_state(client) == before
        assert TK.undo(cx, sid)["errors"] and _all_state(client) == before
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------------------------------------------------
# N06 – Kartenprüfung und Schreiben atomar (abhängige Buchungen in der Schreibtransaktion geprüft)
# ---------------------------------------------------------------------------------------------------------------------

def _link_card(c, monkeypatch, csv_text):
    from app.diagnosis import guide as G
    from tests.test_diagnosis import PF_HEADER, upload

    cx = ctx(c)
    bid = upload(c, "dup.csv", PF_HEADER + csv_text)
    task = next(t for t in G.tasks(cx) if t.kind == "zeile" and t.ref.startswith(f"{bid}:"))
    monkeypatch.setattr(G, "tasks", lambda context, hints=False: [task])
    shown = form_values(c.get("/quality/assistent?new=1").text)
    shown["a"] = ["row:link"]
    target = next(str(t) for rc in csv_service(cx).rows(bid) for t in [(rc.match or {}).get("target")] if t)
    return cx, bid, shown, target


def _edit_target(cx, target, value):
    from app.journal.service import journal_service

    return journal_service(cx).save_import(target, {
        "kind": "expert", "date": "2025-01-01", "time": "11:00", "type": "deposit",
        "to_account": "Wallet K", "to_asset": "KAS", "to_qty": "10", "value_eur": value})


def test_link_target_edit_between_card_check_and_commit_is_rejected(c, monkeypatch):
    """Reviewfall N06: deterministische Verschränkung mit echter Journaländerung in einem zweiten Thread."""
    cx, bid, shown, target = _link_card(
        c, monkeypatch, "N1,2025-01-01T10:00:00Z,deposit,,,,,Wallet K,KAS,10,,,,1,dup\n")
    enter, changed = threading.Event(), threading.Event()
    outcome = {}

    def other_editor():
        try:
            assert enter.wait(8)
            outcome["edit"] = _edit_target(cx, target, "7")
        except BaseException as exc:
            outcome["error"] = repr(exc)
        finally:
            changed.set()
            cx.db.close_thread_conn()

    real_execute = BA.execute

    def after_check(*args, **kwargs):
        enter.set()
        assert changed.wait(8), "journal writer unexpectedly blocked by the CSV lock"
        assert "error" not in outcome and not outcome["edit"].errors, outcome
        return real_execute(*args, **kwargs)

    worker = threading.Thread(target=other_editor)
    worker.start()
    monkeypatch.setattr(BA, "execute", after_check)
    result = post(c, "/quality/assistent/antwort", shown)
    worker.join(timeout=8)
    links = [dict(r) for r in cx.db.q("SELECT tx_id, record_json FROM tx_link WHERE batch_id=?", (bid,))]
    assert "err=" in result.headers["location"] and not links, {
        "location": result.headers["location"], "links_written": links,
        "target_now": [(t.tx_id, str(t.value_eur)) for t in cx.recorded_portfolio().txs if t.tx_id == target]}


def test_link_after_stale_rejection_works_once_and_is_idempotent(c, monkeypatch):
    """Eigener Fall: Nach der Ablehnung (Ziel zwischen Anzeige und Antwort geändert) schreibt das neu geladene Formular
    genau eine Verknüpfung; erneutes Absenden desselben Formulars bucht nichts doppelt."""
    cx, bid, shown, target = _link_card(
        c, monkeypatch, "N1,2025-01-01T10:00:00Z,deposit,,,,,Wallet K,KAS,10,,,,1,dup\n")
    assert not _edit_target(cx, target, "7").errors
    first = post(c, "/quality/assistent/antwort", shown)
    assert "err=" in first.headers["location"]
    assert cx.db.scalar("SELECT COUNT(*) FROM tx_link WHERE batch_id=?", (bid,)) == 0
    fresh = form_values(c.get("/quality/assistent?new=1").text)
    fresh["a"] = ["row:link"]
    assert "err=" not in post(c, "/quality/assistent/antwort", fresh).headers["location"]
    assert cx.db.scalar("SELECT COUNT(*) FROM tx_link WHERE batch_id=?", (bid,)) == 1
    post(c, "/quality/assistent/antwort", fresh)
    assert cx.db.scalar("SELECT COUNT(*) FROM tx_link WHERE batch_id=?", (bid,)) == 1


def test_execute_rejects_changed_dependency_inside_the_write_transaction(c):
    """Eigener Fall auf Dienstebene: ``execute`` mit erwartetem Stand der Bezüge – eine zwischenzeitliche Änderung der
    referenzierten Buchung wird in der Transaktion erkannt; es wird nichts geschrieben (kein Teilzustand)."""
    from tests.test_diagnosis import PF_HEADER, upload

    cx = ctx(c)
    svc = csv_service(cx)
    bid = upload(c, "dup.csv", PF_HEADER + "N1,2025-01-01T10:00:00Z,deposit,,,,,Wallet K,KAS,10,,,,1,dup\n")
    svc.evaluate(bid)
    rows = svc.rows(bid)
    rc = next(r for r in rows if (r.match or {}).get("target"))
    target = str(rc.match["target"])
    refs = BA.effect_refs(rows, rc.rec.event_key or f"#{rc.idx}")
    assert target in refs
    rev = BA.refs_revision(cx.db.conn, refs)
    assert not _edit_target(cx, target, "7").errors
    assert BA.refs_revision(cx.db.conn, refs) != rev
    p = BA.plan(svc, bid, "link", {rc.idx}, True)
    res = BA.execute(svc, bid, "link", {rc.idx}, token=BA.new_token(), fingerprint=p.fingerprint(), force=True,
                     expect_refs=(refs, rev))
    assert res["errors"] and "geändert" in res["errors"][0]
    assert cx.db.scalar("SELECT COUNT(*) FROM tx_link WHERE batch_id=?", (bid,)) == 0


def test_include_approval_keeps_duplicate_and_incomplete_guards(c):
    """Eigener Fall N07: „Einzeln buchen“ öffnet im Planer nur die Bestätigungssperre dieser Zeile – Dubletten
    (verknüpfen statt buchen) und unvollständige Zeilen bleiben gesperrt, auch mit ausdrücklicher Freigabe."""
    from tests.test_diagnosis import PF_HEADER, upload

    cx = ctx(c)
    svc = csv_service(cx)
    bid = upload(c, "mix.csv", PF_HEADER + "N1,2025-01-01T10:00:00Z,deposit,,,,,Wallet K,KAS,10,,,,1,dup\n"
                 "N2,2025-01-03T10:00:00Z,deposit,,,,,Wallet K,KAS,,,,,1,ohne Menge\n")
    svc.evaluate(bid)
    rows = svc.rows(bid)
    idxs = {rc.idx for rc in rows}
    p = BA.plan(svc, bid, "include", idxs, False, rows, approved=dict.fromkeys(idxs, "include"))
    assert not p.todo, [(it.rc.line, it.act, it.note) for it in p.items]
    dup = next(it for it in p.items if (it.rc.match or {}).get("target"))  # Gegenstück D1 vorhanden
    assert dup.act is None and dup.note, dup.note
    assert any(it is not dup and it.act is None and it.note.startswith("unvollständig") for it in p.items)
