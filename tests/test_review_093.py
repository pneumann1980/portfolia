"""Regressionstests zum Review „Buchungen prüfen“ 0.93.0 vom 04.11.2026 (N15, N16, N13-Grenze) – Testmatrix der
gemeinsamen Fehlerklasse: veraltete Bestätigungen, unvollständig geprüfte Abhängigkeiten, Entscheidungsschleifen und
verlorene Fremdeingaben.

Die fünf Fälle des Nachweispakets (``test_review_093.py``) sind mit unveränderten fachlichen Erwartungen übernommen.
Ergänzt (Matrix: Aktion × Abhängigkeit × Änderungszeitpunkt):

* Datenrevision: gleicher Zeitstempel, Änderung ohne ``updated_at``, Wiederverwendung unveränderter Vorschauen,
  Prozessneustart.
* Kandidatenmengen: jeder angebotene Partner (vorgeschlagen/nicht vorgeschlagen), neuer Kandidat, Wert/Menge/Konto/
  Status, Änderung nach der Bewertung, nach dem Anwenden der Entscheidungen und unmittelbar vor dem Commit – in der
  Quellenübernahme und in der Detailansicht (gerendertes Formular → Entscheidung → Vorschau → Buchung → Rücknahme).
* Eingaben: gleichlautende Fremdeingabe, A → B → A, wiederholte Rücknahme, veraltetes Formular (unberührte Felder,
  eigene abweichende Änderung, ausdrücklich gleicher Wert), Transferentscheidung und Partnerstand als Gruppe.

Teststeuerung: echte zweite Threads über die regulären Dienste; ``TK._apply_decisions``, ``BA.execute`` bzw.
``BA._execute_tx`` werden nur umhüllt, um den Zeitpunkt zu wählen – danach läuft der unveränderte Dienst.
"""

# ruff: noqa: F811  (Fixtures aus anderen Testmodulen werden importiert und als Argumente verwendet)
from __future__ import annotations

from datetime import timedelta

import pytest
from lxml import html

from app.csvimport import batch as BA
from app.csvimport.service import csv_service, ref_revs
from app.datasources import takeover as TK
from app.diagnosis import actions as A
from app.journal import service as J
from app.journal.service import journal_service
from app.util.timeutil import iso, parse_iso
from tests.test_bitpanda import api, ctx, master  # noqa: F401  (Fixtures)
from tests.test_diagnosis import PF_HEADER, upload
from tests.test_nachpruefung import _all_state, _journal_deposits, _row, _without_counterpart
from tests.test_review_091 import _change_journal
from tests.test_review_092 import _run_editor, _two_rows
from tests.test_review_fixes import c, cfg, form_values, post  # noqa: F401  (Fixtures/Helfer)
from tests.test_takeover import staged

# ---------------------------------------------------------------------------------------------------------------------
# Fälle des Nachweispakets (unverändert in der Erwartung)
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("edit", ["none", "selected", "alternative"])
def test_all_offered_transfer_partners_stay_bound_until_commit(config, master, api, monkeypatch, edit):
    client, sid = _without_counterpart(config, api)
    try:
        cx = ctx(client)
        targets = _journal_deposits(client, ("2024-02-12T12:30:00Z", "Ledger A", "0.001"),
                                    ("2024-02-12T12:45:00Z", "Ledger B", "0.001"))
        before = _all_state(client)
        a = staged(client, sid)
        problem = _row(a)
        assert problem["partner"]["kind"] == "alts"
        assert set(problem["partner"]["revs"]) == set(targets)
        assert not TK.decide(cx, sid, problem["key"], "nopair", a.token)["errors"]
        ready = TK.assess(cx, sid)
        assert ready.ok
        real = TK._apply_decisions
        changed = []

        def apply_then_change(context, csv, bid, assessment):
            result = real(context, csv, bid, assessment)
            if not getattr(TK._SANDBOX, "on", False) and not changed and edit != "none":
                rc = next(r for r in csv.rows(bid) if r.rec.ext_id == problem["key"])
                selected = rc.pair_ref[2:]
                assert selected in targets and len(rc.pair_alts) == 1
                target = selected if edit == "selected" else next(t for t in targets if t != selected)
                _run_editor(cx, lambda: _change_journal(cx, target, "value"))
                changed.append(target)
            return result

        monkeypatch.setattr(TK, "_apply_decisions", apply_then_change)
        out = TK.apply(cx, sid, ready.token)
        if edit == "none":
            assert not out["errors"]
            assert not TK.undo(cx, sid)["errors"]
            cx.invalidate_data()
            assert _all_state(client) == before
            return
        assert changed
        assert cx.db.scalar("SELECT value_eur FROM journal_tx WHERE tx_id=?", (changed[0],)) == "777"
        assert out["errors"], {"edited": edit, "changed": changed[0],
                               "exact": out.get("result", {}).get("check", {}).get("exact"), "errors": out["errors"]}
        last = TK.latest(cx.db, sid)
        assert last["status"] == "staged" and not last["decision_id"] and not last["batch_id"]
        assert not cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE source='sync:bitpanda' AND status='active'")
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("same_second", [False, True])
def test_takeover_preview_refreshes_partner_revision_after_journal_edit(config, master, api, monkeypatch,
                                                                       same_second):
    client, sid = _without_counterpart(config, api)
    try:
        cx = ctx(client)
        (target,) = _journal_deposits(client, ("2024-02-12T12:30:00Z", "Ledger", "0.001"))
        a = staged(client, sid)
        problem = _row(a)
        assert problem["partner"]["kind"] == "j"
        before = A.data_version(cx.db)
        stamp = cx.db.scalar("SELECT MAX(updated_at) FROM journal_tx")
        # Scheduling only: a journal update in the same timestamp second, or a later one.
        if not same_second:
            stamp = iso(parse_iso(stamp) + timedelta(seconds=2))
        monkeypatch.setattr(J, "_now", lambda: stamp)
        _run_editor(cx, lambda: _change_journal(cx, target, "value"))
        actual_rev = ref_revs(cx.db.conn, [target])[target]
        assert actual_rev != problem["partner"]["rev"]
        collision = A.data_version(cx.db) == before
        fresh = TK.assess(cx, sid)
        shown = next(p for p in fresh.problems
                     if p.get("kind") == "row" and (p.get("partner") or {}).get("tx") == target)
        base = _all_state(client)
        stored = TK.decide(cx, sid, shown["key"], "pair", fresh.token)
        assert not stored["errors"], stored
        ready = TK.assess(cx, sid)
        retries = 0
        # Diagnose whether the normal remove/re-decide path can recover without another data change.
        if same_second and not ready.ok:
            for _ in range(2):
                for d in ready.stale:
                    if d.get("key"):
                        assert not TK.undecide(cx, sid, d["key"], ready.rid, d["stamp"])["errors"]
                nxt = TK.assess(cx, sid)
                offer = next(p for p in nxt.problems
                             if p.get("kind") == "row" and (p.get("partner") or {}).get("tx") == target)
                assert not TK.decide(cx, sid, offer["key"], "pair", nxt.token)["errors"]
                ready = TK.assess(cx, sid)
                retries += 1
        assert ready.ok, {"same_second": same_second, "version_collision": collision, "retries": retries,
                          "stale": [d["why"] for d in ready.stale]}
        assert shown["partner"]["rev"] == actual_rev
        assert not TK.apply(cx, sid, ready.token)["errors"]
        assert not TK.undo(cx, sid)["errors"]
        cx.invalidate_data()
        assert _all_state(client) == base
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------------------------------------------------
# Datenrevision (N16-Klasse)
# ---------------------------------------------------------------------------------------------------------------------


def test_data_version_detects_every_write_and_is_stable_without_one(c, monkeypatch):
    cx = ctx(c)
    js = journal_service(cx)
    saved = js.save({"kind": "expert", "date": "2025-07-01", "time": "10:00", "type": "deposit",
                     "to_account": "Ledger", "to_asset": "BTC", "to_qty": "0.01", "value_eur": "550"})
    tx_id = saved.tx_ids[0]
    v0 = A.data_version(cx.db)
    assert A.data_version(cx.db) == v0  # lesend: unverändert (Vorschauen bleiben wiederverwendbar)
    c.get("/journal")
    c.get("/quality")
    assert A.data_version(cx.db) == v0
    stamp = cx.db.scalar("SELECT updated_at FROM journal_tx WHERE tx_id=?", (tx_id,))
    monkeypatch.setattr(J, "_now", lambda: stamp)  # gleiche gespeicherte Sekunde, gleiche Zeilenzahl
    form = js.form_data(cx.db.q1("SELECT * FROM journal_tx WHERE tx_id=?", (tx_id,)))
    form["value_eur"] = "551"
    assert not js.save(form, tx_id).errors
    v1 = A.data_version(cx.db)
    assert v1 != v0
    cx.db.x("UPDATE journal_tx SET note='x' WHERE tx_id=?", (tx_id,))  # ohne updated_at
    v2 = A.data_version(cx.db)
    assert v2 != v1
    form["value_eur"] = "550"  # A → B → A: Inhalt wie vorher, Revision trotzdem neu
    assert not js.save(form, tx_id).errors
    assert A.data_version(cx.db) not in (v0, v1, v2)


def test_unchanged_takeover_preview_is_reused_and_survives_restart(config, master, api, monkeypatch):
    from tests.test_bitpanda import make_client

    client, sid = _without_counterpart(config, api)
    try:
        cx = ctx(client)
        _journal_deposits(client, ("2024-02-12T12:30:00Z", "Ledger", "0.001"))
        staged(client, sid)
        calls = []
        real = TK._apply_steps

        def counting(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)

        monkeypatch.setattr(TK, "_apply_steps", counting)
        a = TK.assess(cx, sid)
        n = len(calls)
        b = TK.assess(cx, sid)
        assert b.token == a.token and len(calls) == n  # unveränderter Stand: Probelauf wiederverwendet
    finally:
        client.__exit__(None, None, None)
    client = make_client(config)
    client.__enter__()
    try:
        client.get("/settings")
        cx = ctx(client)
        calls.clear()
        a2 = TK.assess(cx, sid)
        assert a2.token == a.token and not calls  # nach Neustart: dieselbe gespeicherte Grundlage
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------------------------------------------------
# Kandidatenmengen in der Quellenübernahme (N15-Klasse)
# ---------------------------------------------------------------------------------------------------------------------


def _alts_takeover(config, api):
    client, sid = _without_counterpart(config, api)
    cx = ctx(client)
    targets = _journal_deposits(client, ("2024-02-12T12:30:00Z", "Ledger A", "0.001"),
                                ("2024-02-12T12:45:00Z", "Ledger B", "0.001"))
    a = staged(client, sid)
    problem = _row(a)
    assert problem["partner"]["kind"] == "alts" and problem["partner"]["rev"]
    assert TK.decide(cx, sid, problem["key"], "nopair", a.token)["errors"] == []
    return client, sid, cx, targets, problem


def _add_candidate(cx, client):
    """Neuer passender Journal-Zugang (dritter Kandidat), regulär über den Journaldienst erfasst."""
    saved = journal_service(cx).save({"kind": "expert", "date": "2024-02-12", "time": "13:40", "type": "deposit",
                                      "to_account": "Ledger C", "to_asset": "BTC", "to_qty": "0.001",
                                      "value_eur": "55"})
    assert not saved.errors, saved.errors
    return saved.tx_ids


@pytest.mark.parametrize("change", ["value", "qty", "account", "delete"])
@pytest.mark.parametrize("which", [0, 1])
@pytest.mark.parametrize("when", ["assessed", "decisions", "commit"])
def test_takeover_alternatives_any_change_any_time_is_rejected(config, master, api, monkeypatch, when, which, change):
    client, sid, cx, targets, _problem = _alts_takeover(config, api)
    try:
        ready = TK.assess(cx, sid)
        assert ready.ok
        target = targets[which]
        done = []

        def edit():
            if not done:
                _run_editor(cx, lambda: _change_journal(cx, target, change))
                done.append(1)

        if when == "assessed":
            edit()
        elif when == "decisions":
            real = TK._apply_decisions

            def wrapped(*args, **kwargs):
                res = real(*args, **kwargs)
                if not getattr(TK._SANDBOX, "on", False):
                    edit()
                return res

            monkeypatch.setattr(TK, "_apply_decisions", wrapped)
        else:
            real_tx = BA._execute_tx

            def before_commit(*args, **kwargs):
                if not getattr(TK._SANDBOX, "on", False):
                    edit()
                return real_tx(*args, **kwargs)

            monkeypatch.setattr(BA, "_execute_tx", before_commit)
        out = TK.apply(cx, sid, ready.token)
        monkeypatch.undo()
        assert done and out["errors"], (when, which, change)
        last = TK.latest(cx.db, sid)
        assert last["status"] == "staged" and not last["decision_id"] and not last["batch_id"]
        assert TK.needs_recovery(cx.db, sid) is None
        assert not cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE source='sync:bitpanda' AND status='active'")
        status = cx.db.scalar("SELECT status FROM journal_tx WHERE tx_id=?", (target,))
        assert status == ("deleted" if change == "delete" else "active")  # Fremdänderung bleibt
    finally:
        client.__exit__(None, None, None)


def test_takeover_new_candidate_after_decision_is_rejected_then_redecided(config, master, api):
    client, sid, cx, _targets, _problem = _alts_takeover(config, api)
    try:
        _add_candidate(cx, client)
        base = _all_state(client)
        a = TK.assess(cx, sid)
        assert not a.decisions  # Kandidatenmenge geändert: Entscheidung verworfen, nie still angewendet
        for d in [d for d in a.stale if d.get("key")]:
            assert TK.undecide(cx, sid, d["key"], a.rid, d["stamp"])["errors"] == []
        a = TK.assess(cx, sid)
        p = next(p for p in a.problems if p.get("kind") == "row" and (p.get("partner") or {}).get("kind") == "alts")
        assert len(p["partner"]["revs"]) == 3
        assert TK.decide(cx, sid, p["key"], "nopair", a.token)["errors"] == []
        b = TK.assess(cx, sid)
        assert b.ok, [(x.key, x.detail) for x in b.blockers]
        assert TK.apply(cx, sid, b.token)["errors"] == []
        assert TK.undo(cx, sid)["errors"] == []
        cx.invalidate_data()
        assert _all_state(client) == base
    finally:
        client.__exit__(None, None, None)


def test_takeover_rejected_alternatives_recover_to_one_booking_and_exact_undo(config, master, api, monkeypatch):
    client, sid, cx, targets, _problem = _alts_takeover(config, api)
    try:
        ready = TK.assess(cx, sid)
        real_tx = BA._execute_tx
        done = []

        def before_commit(*args, **kwargs):
            if not getattr(TK._SANDBOX, "on", False) and not done:
                _run_editor(cx, lambda: _change_journal(cx, targets[1], "value"))
                done.append(1)
            return real_tx(*args, **kwargs)

        monkeypatch.setattr(BA, "_execute_tx", before_commit)
        assert TK.apply(cx, sid, ready.token)["errors"]
        monkeypatch.undo()
        cx.invalidate_data()
        base = _all_state(client)
        a = TK.assess(cx, sid)
        for d in [d for d in a.stale if d.get("key")]:
            assert TK.undecide(cx, sid, d["key"], a.rid, d["stamp"])["errors"] == []
        a = TK.assess(cx, sid)
        p = next(p for p in a.problems if p.get("kind") == "row" and (p.get("partner") or {}).get("kind") == "alts")
        assert TK.decide(cx, sid, p["key"], "nopair", a.token)["errors"] == []
        b = TK.assess(cx, sid)
        assert b.ok and TK.apply(cx, sid, b.token)["errors"] == []
        assert TK.apply(cx, sid, b.token)["errors"]  # erneutes Absenden: kein zweiter Lauf
        assert TK.undo(cx, sid)["errors"] == []
        assert TK.undo(cx, sid)["errors"]  # wiederholte Rücknahme ändert nichts
        cx.invalidate_data()
        assert _all_state(client) == base
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------------------------------------------------
# Kandidatenmengen in der Detailansicht
# ---------------------------------------------------------------------------------------------------------------------


def _detail_alts(c):
    """Neuer BTC-Abgang (CSV) mit zwei passenden, bereits gebuchten Zugängen: mehrere Journal-Kandidaten."""
    cx = ctx(c)
    js = journal_service(cx)
    assert not js.save({"kind": "expert", "date": "2025-07-01", "time": "10:00", "type": "deposit",
                        "to_account": "Sender", "to_asset": "BTC", "to_qty": "0.01", "value_eur": "550"}).errors
    tids = []
    for acc, t in (("Ledger A", "14:30"), ("Ledger B", "14:40")):
        s = js.save({"kind": "expert", "date": "2025-07-12", "time": t, "type": "deposit", "to_account": acc,
                     "to_asset": "BTC", "to_qty": "0.001", "value_eur": "55"})
        assert not s.errors
        tids.append(s.tx_ids[0])
    bid = upload(c, "out.csv", PF_HEADER + "OUT,2025-07-12T12:00:00Z,withdrawal,,Sender,BTC,0.001,,,,,,,55,out\n")
    svc = csv_service(cx)
    svc.evaluate(bid)
    rc = next(r for r in svc.rows(bid) if r.pair_ref)
    assert (rc.pair_ref or "").startswith("j:") and rc.pair_alts
    return cx, svc, bid, rc, tids


def _transfers_form(c, bid, idx):
    page = c.get(f"/journal/csv/{bid}").text
    form = html.fromstring(page).xpath(f'//form[.//select[@name="pair_{idx}"]]')[0]
    return {el.get("name"): [el.get("value") or ""] for el in form.xpath(".//input[@name]")}


def test_detail_nopair_for_alternatives_books_and_undoes(c):
    cx, _svc, bid, rc, tids = _detail_alts(c)
    before = _all_state(c)
    fields = _transfers_form(c, bid, rc.idx)
    fields[f"pair_{rc.idx}"] = ["0"]
    assert post(c, f"/journal/csv/{bid}/rows", fields).status_code == 303
    BA.set_selection(cx.db, bid, {rc.idx})
    shown = form_values(c.get(f"/journal/csv/{bid}/preview?action=include&force=1").text,
                        f"/journal/csv/{bid}/execute")
    assert post(c, f"/journal/csv/{bid}/execute", shown).status_code == 303
    cx.invalidate_data()
    assert cx.db.q1("SELECT type FROM journal_tx WHERE batch_id=? AND status='active'", (bid,))["type"] == "withdrawal"
    assert all(cx.db.scalar("SELECT status FROM journal_tx WHERE tx_id=?", (t,)) == "active" for t in tids)
    aid = cx.db.scalar("SELECT id FROM import_action WHERE batch_id=?", (bid,))
    assert post(c, f"/journal/csv/{bid}/actions/{aid}/undo", {}).status_code == 303
    cx.invalidate_data()
    assert _all_state(c) == before


@pytest.mark.parametrize("which", [0, 1])
@pytest.mark.parametrize("when", ["before_preview", "after_preview", "before_commit"])
def test_detail_alternative_change_invalidates_nopair(c, monkeypatch, which, when):
    cx, svc, bid, rc, tids = _detail_alts(c)
    fields = _transfers_form(c, bid, rc.idx)
    fields[f"pair_{rc.idx}"] = ["0"]
    assert post(c, f"/journal/csv/{bid}/rows", fields).status_code == 303
    BA.set_selection(cx.db, bid, {rc.idx})
    if when == "before_preview":
        _change_journal(cx, tids[which], "value")
        svc.evaluate(bid)  # wie jede Vorschau/Ausführung: frisch bewerten
        assert not BA.plan(svc, bid, "include", {rc.idx}, True).todo  # Entscheidung verworfen → neu fragen
        page = c.get(f"/journal/csv/{bid}").text
        assert "nach deiner Transferentscheidung geändert" in page
        return
    shown = form_values(c.get(f"/journal/csv/{bid}/preview?action=include&force=1").text,
                        f"/journal/csv/{bid}/execute")
    if when == "after_preview":
        _change_journal(cx, tids[which], "value")
    else:
        real_tx = BA._execute_tx

        def before_commit(*args, **kwargs):
            _run_editor(cx, lambda: _change_journal(cx, tids[which], "value"))
            return real_tx(*args, **kwargs)

        monkeypatch.setattr(BA, "_execute_tx", before_commit)
    res = post(c, f"/journal/csv/{bid}/execute", shown)
    monkeypatch.undo()
    assert res.status_code == 409
    assert not cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE batch_id=? AND status='active'", (bid,))


def test_detail_new_candidate_between_check_and_commit_is_rejected(c, monkeypatch):
    """Neue Gegenbuchung zwischen Bewertung und Commit (Zusammensetzung der Kandidatenmenge): abgelehnt."""
    cx, svc, bid, rc, _tids = _detail_alts(c)
    fields = _transfers_form(c, bid, rc.idx)
    fields[f"pair_{rc.idx}"] = ["0"]
    assert post(c, f"/journal/csv/{bid}/rows", fields).status_code == 303
    BA.set_selection(cx.db, bid, {rc.idx})
    shown = form_values(c.get(f"/journal/csv/{bid}/preview?action=include&force=1").text,
                        f"/journal/csv/{bid}/execute")
    real_tx = BA._execute_tx

    def add_then_commit(*args, **kwargs):
        _run_editor(cx, lambda: journal_service(cx).save({
            "kind": "expert", "date": "2025-07-12", "time": "14:35", "type": "deposit", "to_account": "Ledger C",
            "to_asset": "BTC", "to_qty": "0.001", "value_eur": "55"}))
        return real_tx(*args, **kwargs)

    monkeypatch.setattr(BA, "_execute_tx", add_then_commit)
    res = post(c, f"/journal/csv/{bid}/execute", shown)
    monkeypatch.undo()
    assert res.status_code == 409
    assert not cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE batch_id=? AND status='active'", (bid,))
    # danach: neue Anzeige mit drei Kandidaten → neue Entscheidung → genau eine Buchung
    svc.evaluate(bid)
    now = next(r for r in svc.rows(bid) if r.idx == rc.idx)
    assert now.pair_ok is None and len(now.pair_alts) == 2
    fields = _transfers_form(c, bid, rc.idx)
    fields[f"pair_{rc.idx}"] = ["0"]
    assert post(c, f"/journal/csv/{bid}/rows", fields).status_code == 303
    shown = form_values(c.get(f"/journal/csv/{bid}/preview?action=include&force=1").text,
                        f"/journal/csv/{bid}/execute")
    assert post(c, f"/journal/csv/{bid}/execute", shown).status_code == 303
    assert cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE batch_id=? AND status='active'", (bid,)) == 1


def test_legacy_commit_rejects_alternative_change_before_commit(c, monkeypatch):
    cx, svc, bid, rc, tids = _detail_alts(c)
    fields = _transfers_form(c, bid, rc.idx)
    fields[f"pair_{rc.idx}"] = ["0"]
    fields[f"dec_{rc.idx}"] = ["include"]
    assert post(c, f"/journal/csv/{bid}/rows", fields).status_code == 303
    real = svc.commit_plan

    def plan_then_edit(*args, **kwargs):
        out = real(*args, **kwargs)
        _run_editor(cx, lambda: _change_journal(cx, tids[1], "value"))
        return out

    monkeypatch.setattr(svc, "commit_plan", plan_then_edit)
    res = svc.commit(bid)
    monkeypatch.undo()
    assert res["errors"]
    assert not cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE batch_id=? AND status='active'", (bid,))


# ---------------------------------------------------------------------------------------------------------------------
# Eingaben: Besitzkennungen statt Wertvergleich (N13-Grenze)
# ---------------------------------------------------------------------------------------------------------------------


def _vals(cx, bid, idx):
    return tuple(cx.db.q1("SELECT decision, value_in, fee_in FROM csv_row WHERE batch_id=? AND idx=?", (bid, idx)))


def test_same_value_foreign_write_is_not_reverted(c):
    cx, svc, bid, _mine, other = _two_rows(c)
    a = svc.set_rows(bid, {f"val_{other.idx}": "50"})
    svc.set_rows(bid, {f"val_{other.idx}": ""})
    b = svc.set_rows(bid, {f"val_{other.idx}": "50"})  # anderer Aufruf, gleicher Wert
    assert b["undo"]
    assert svc.restore_inputs(bid, a["undo"]) == 0  # gehört nicht mehr A
    assert _vals(cx, bid, other.idx) == (None, "50", None)


def test_a_b_a_sequence_and_repeated_restore(c):
    cx, svc, bid, _mine, other = _two_rows(c)
    a = svc.set_rows(bid, {f"dec_{other.idx}": "skip"})
    svc.set_rows(bid, {f"dec_{other.idx}": "defer"})
    svc.set_rows(bid, {f"dec_{other.idx}": "skip"})  # A → B → A
    assert svc.restore_inputs(bid, a["undo"]) == 0
    assert _vals(cx, bid, other.idx)[0] == "skip"
    own = svc.set_rows(bid, {f"fee_{other.idx}": "0.7"})
    assert svc.restore_inputs(bid, own["undo"]) == 1
    assert svc.restore_inputs(bid, own["undo"]) == 0  # wiederholt: nichts mehr
    svc.set_rows(bid, {f"fee_{other.idx}": "0.7"})
    assert svc.restore_inputs(bid, own["undo"]) == 0  # verspätet: neuere Eingabe bleibt
    assert _vals(cx, bid, other.idx) == ("skip", None, "0.7")


def test_stale_form_does_not_overwrite_newer_inputs(c):
    cx, svc, bid, _mine, other = _two_rows(c)
    page = c.get(f"/journal/csv/{bid}").text
    tab_a = form_values(page, f"/journal/csv/{bid}/rows")
    assert f"iv_{other.idx}" in tab_a
    svc.set_rows(bid, {f"val_{other.idx}": "123", f"dec_{other.idx}": "defer"})  # Tab B speichert zuerst
    # Tab A: unberührte Felder (leer) dürfen B nicht überschreiben
    assert post(c, f"/journal/csv/{bid}/rows", tab_a).status_code == 303
    assert _vals(cx, bid, other.idx) == ("defer", "123", None)
    # Tab A ändert dasselbe Feld abweichend: sichtbar abgelehnt, B bleibt
    tab_a[f"val_{other.idx}"] = ["99"]
    res = post(c, f"/journal/csv/{bid}/rows", tab_a)
    assert res.status_code == 200 and "inzwischen an anderer Stelle geändert" in res.text
    assert _vals(cx, bid, other.idx) == ("defer", "123", None)
    # Tab A setzt ausdrücklich denselben Wert: übernimmt die Gruppe (B's alte Rücknahme wirkt nicht mehr)
    tab_a[f"val_{other.idx}"] = ["123"]
    assert post(c, f"/journal/csv/{bid}/rows", tab_a).status_code == 303
    assert _vals(cx, bid, other.idx) == ("defer", "123", None)
    # frisches Formular: Änderung wird gespeichert
    fresh = form_values(c.get(f"/journal/csv/{bid}").text, f"/journal/csv/{bid}/rows")
    fresh[f"val_{other.idx}"] = ["77"]
    assert post(c, f"/journal/csv/{bid}/rows", fresh).status_code == 303
    assert _vals(cx, bid, other.idx)[1] == "77"


def test_transfer_decision_and_partner_state_are_restored_together(c):
    from tests.test_review_091 import _journal_transfer

    cx, svc, bid, rc, _target = _journal_transfer(c, "1", save=False)
    fields = _transfers_form(c, bid, rc.idx)
    saved = svc.set_rows(bid, {f"pair_{rc.idx}": "1", f"pairrev_{rc.idx}": fields[f"pairrev_{rc.idx}"][0]})
    assert [g for _r, g, _b, _t in saved["undo"]] == ["pair"]
    assert svc.restore_inputs(bid, saved["undo"]) == 1
    row = cx.db.q1("SELECT pair_ok, pair_rev FROM csv_row WHERE id=?", (rc.id,))
    assert (row["pair_ok"], row["pair_rev"]) == (None, None)
