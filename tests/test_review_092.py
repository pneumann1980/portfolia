"""Regressionstests zum Review „Buchungen prüfen“ 0.92.0 vom 04.11.2026 (N13, N14).

Die sieben Fälle des Nachweispakets (``test_review_092.py``) sind mit unveränderten fachlichen Erwartungen übernommen;
ergänzt um eigene Fälle: echte konkurrierende Eingaben auf derselben und auf anderen Zeilen (Entscheidung, EUR-Wert,
Gebührenwert, Transferbestätigung), wiederholte Rücknahme, fehlende bzw. leere Partnerrevision über Formular und
internen Aufruf, „Transfer bestätigen“ und „kein Transfer“ in der Quellenübernahme mit Änderung von Wert, Konto,
Menge, Gebühr und Löschstatus vor dem Anwenden der Entscheidungen und vor der Buchung, sowie neue Bestätigung →
Buchung → vollständige Rücknahme. Nur synthetische Daten.

Teststeuerung: Die Nebenläufigkeitsfälle ersetzen ``BA._execute_tx``, ``svc.restore_inputs``,
``TK._apply_decisions`` bzw. ``BA.execute`` durch Wrapper, die einen echten zweiten Thread über die regulären Dienste
schreiben lassen und danach den unveränderten Dienst aufrufen.
"""

# ruff: noqa: F811  (Fixtures aus anderen Testmodulen werden importiert und als Argumente verwendet)
from __future__ import annotations

import threading
from decimal import Decimal

import pytest
from lxml import html

from app.csvimport import batch as BA
from app.csvimport import model as M
from app.csvimport.service import csv_service, rec_to_json
from app.datasources import takeover as TK
from app.util.timeutil import parse_iso
from tests.test_bitpanda import api, ctx, master  # noqa: F401  (Fixtures)
from tests.test_diagnosis import PF_HEADER, upload
from tests.test_nachpruefung import _journal_deposits, _row, _without_counterpart
from tests.test_review_090 import _edit_target
from tests.test_review_091 import _change_journal, _duplicate, _journal_transfer, _takeover_with_edit
from tests.test_review_fixes import c, cfg, form_values, post  # noqa: F401  (Fixtures/Helfer)
from tests.test_takeover import staged

# ---------------------------------------------------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------------------------------------------------


def _run_editor(cx, fn):
    failures = []

    def run():
        try:
            fn()
        except BaseException as e:
            failures.append(repr(e))
        finally:
            cx.db.close_thread_conn()

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(8)
    assert not worker.is_alive() and not failures, failures


def _two_rows(c):
    """Prüf-Stapel mit einer Dublette (Verknüpfen) und einem offenen Tausch mit editierbarem EUR-Wert."""
    cx = ctx(c)
    bid = upload(c, "mix.csv", PF_HEADER + "N1,2025-01-01T10:00:00Z,deposit,,,,,Wallet K,KAS,10,,,,1,dup\n")
    svc = csv_service(cx)
    other_rec = M.Rec(line=3, ts=parse_iso("2025-03-12T12:00:00Z"), kind=M.TRADE, account="Other",
                      out_sym="ETH", out_qty=Decimal("8"), in_sym="BNB", in_qty=Decimal("1"), ext_id="synthetic:other")
    recs = [svc.rows(bid)[0].rec, other_rec]
    payload = ("[" + ",".join(rec_to_json(r) for r in recs) + "]").encode()
    bid = svc.ingest(recs, source="csv:portfolia", profile="portfolia", account="",
                     label="Synthetic two-row review", datasource_id=None, payload=payload, options={"cutoff": ""})
    svc.evaluate(bid)
    mine, other = svc.rows(bid)
    return cx, svc, bid, mine, other


def _inputs(cx, bid, idx):
    return tuple(cx.db.q1("SELECT decision, value_in, fee_in FROM csv_row WHERE batch_id=? AND idx=?", (bid, idx)))


def _rejected_row_action(c, monkeypatch, own, later):
    """Einzelknopf „verknüpfen“ wird abgelehnt (Ziel parallel geändert); vor der Rücknahme schreibt ein anderer Tab
    ``later`` – Rückgabe: Stand danach je Zeile."""
    cx, svc, bid, mine, other = _two_rows(c)
    target = str(mine.match["target"])
    page = c.get(f"/journal/csv/{bid}").text
    fields = form_values(page, f"/journal/csv/{bid}/rows")
    fields["row_action"] = [f"link:{mine.idx}"]
    for k, v in own.items():  # eigene Eingaben dieses Aufrufs (Tab A)
        fields[k.format(o=other.idx, m=mine.idx)] = [v]
    real_tx, real_restore = BA._execute_tx, svc.restore_inputs

    def change_dependency_then_tx(*args, **kwargs):
        _run_editor(cx, lambda: _edit_target(cx, target, "7"))
        return real_tx(*args, **kwargs)

    def other_input_then_restore(batch_id, snap):
        if later:
            _run_editor(cx, lambda: svc.set_rows(bid, {k.format(o=other.idx, m=mine.idx): v
                                                       for k, v in later.items()}))
        return real_restore(batch_id, snap)

    monkeypatch.setattr(BA, "_execute_tx", change_dependency_then_tx)
    monkeypatch.setattr(svc, "restore_inputs", other_input_then_restore)
    res = post(c, f"/journal/csv/{bid}/row-action", fields)
    monkeypatch.undo()
    assert res.status_code == 200 and "geändert" in res.text
    assert not cx.db.scalar("SELECT COUNT(*) FROM tx_link WHERE batch_id=?", (bid,))
    return cx, svc, bid, mine, other


# ---------------------------------------------------------------------------------------------------------------------
# Fälle des Nachweispakets (unverändert in der Erwartung)
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("later", [False, True])
def test_N13_rejected_row_action_preserves_other_tabs_later_inputs(c, monkeypatch, later):
    cx = ctx(c)
    bid = upload(c, "mix.csv", PF_HEADER +
                 "N1,2025-01-01T10:00:00Z,deposit,,,,,Wallet K,KAS,10,,,,1,dup\n")
    svc = csv_service(cx)
    other_rec = M.Rec(line=3, ts=parse_iso("2025-03-12T12:00:00Z"), kind=M.TRADE, account="Other",
                      out_sym="ETH", out_qty=Decimal("8"), in_sym="BNB", in_qty=Decimal("1"), ext_id="synthetic:other")
    recs = [svc.rows(bid)[0].rec, other_rec]
    payload = ("[" + ",".join(rec_to_json(r) for r in recs) + "]").encode()
    bid = svc.ingest(recs, source="csv:portfolia", profile="portfolia", account="",
                     label="Synthetic two-row review", datasource_id=None, payload=payload, options={"cutoff": ""})
    svc.evaluate(bid)
    mine, other = svc.rows(bid)
    assert other.missing_value
    target = str(mine.match["target"])
    page = c.get(f"/journal/csv/{bid}").text
    assert f'name="val_{other.idx}"' in page  # a real editable field in the second tab
    fields = form_values(page, f"/journal/csv/{bid}/rows")
    fields["row_action"] = [f"link:{mine.idx}"]
    real_tx, real_restore = BA._execute_tx, svc.restore_inputs
    done = []

    def change_dependency_then_tx(*args, **kwargs):
        def edit():
            assert not _edit_target(cx, target, "7").errors
        _run_editor(cx, edit)
        return real_tx(*args, **kwargs)

    def other_input_then_restore(batch_id, snap):
        if later:
            def edit():
                svc.set_rows(bid, {f"dec_{other.idx}": "defer", f"val_{other.idx}": "123"})
            _run_editor(cx, edit)
            saved = cx.db.q1("SELECT decision,value_in FROM csv_row WHERE batch_id=? AND idx=?", (bid, other.idx))
            assert tuple(saved) == ("defer", "123")
        done.append(True)
        return real_restore(batch_id, snap)

    monkeypatch.setattr(BA, "_execute_tx", change_dependency_then_tx)
    monkeypatch.setattr(svc, "restore_inputs", other_input_then_restore)
    res = post(c, f"/journal/csv/{bid}/row-action", fields)
    assert res.status_code == 200 and "geändert" in res.text and done
    assert not cx.db.scalar("SELECT COUNT(*) FROM tx_link WHERE batch_id=?", (bid,))
    current = cx.db.q1("SELECT decision,value_in FROM csv_row WHERE batch_id=? AND idx=?", (bid, other.idx))
    expected = ("defer", "123") if later else (None, None)
    assert tuple(current) == expected, {"later_input": expected, "after_rejected_action": tuple(current)}


def test_control_old_row_button_does_not_link_after_new_source_identity(c):
    cx, svc, bid, mine = _duplicate(c)
    target = str(mine.match["target"])
    fields = form_values(c.get(f"/journal/csv/{bid}").text, f"/journal/csv/{bid}/rows")
    fields["row_action"] = [f"link:{mine.idx}"]
    # A later record of the same source identity is booked on a different date, through real services.
    second = upload(c, "later.csv", PF_HEADER +
                    "N1,2025-03-12T12:00:00Z,deposit,,,,,Wallet K,KAS,3.5,,,,1,later source version\n")
    assert second != bid
    rows = svc.rows(second)
    assert rows[0].status == "new", [(r.status, r.match) for r in rows]
    assert not svc.commit(second)["errors"]
    booked = cx.db.scalar("SELECT tx_id FROM journal_tx WHERE batch_id=? AND status='active'", (second,))
    assert booked and booked != target
    assert not cx.db.scalar("SELECT COUNT(*) FROM tx_link WHERE batch_id=? AND status='active'", (bid,))
    res = post(c, f"/journal/csv/{bid}/row-action", fields)
    links = [dict(r) for r in cx.db.q("SELECT tx_id FROM tx_link WHERE batch_id=? AND status='active'", (bid,))]
    assert not links, {"originally_shown_target": target, "new_target": booked, "linked": links,
                       "http": res.status_code, "location": res.headers.get("location")}


@pytest.mark.parametrize("choice", ["pair", "nopair"])
def test_N14_takeover_does_not_rebind_stored_decision_to_changed_partner(config, master, api, monkeypatch, choice):
    client, sid = _without_counterpart(config, api)
    try:
        cx = ctx(client)
        (target,) = _journal_deposits(client, ("2024-02-12T12:30:00Z", "Ledger", "0.001"))
        a = staged(client, sid)
        problem = _row(a)
        assert problem["partner"]["kind"] == "j"
        assert not TK.decide(cx, sid, problem["key"], choice, a.token)["errors"]
        ready = TK.assess(cx, sid)
        assert ready.ok
        real = TK._apply_decisions
        did = []

        def edit_then_set(context, csv, bid, assessment):
            if not getattr(TK._SANDBOX, "on", False) and not did:
                _run_editor(cx, lambda: _change_journal(cx, target, "value"))
                did.append(True)
            return real(context, csv, bid, assessment)

        monkeypatch.setattr(TK, "_apply_decisions", edit_then_set)
        result = TK.apply(cx, sid, ready.token)
        assert did
        partner = dict(cx.db.q1("SELECT status,value_eur FROM journal_tx WHERE tx_id=?", (target,)))
        assert partner["value_eur"] == "777"
        booked = [dict(r) for r in cx.db.q("SELECT type,to_account,from_account FROM journal_tx WHERE batch_id=? AND "
                                           "status='active'", (TK.latest(cx.db, sid)["batch_id"],))]
        assert result["errors"], {"choice": choice, "exact": result.get("result", {}).get("check", {}).get("exact"),
                                  "partner_value": partner["value_eur"],
                                  "source_types": [r["type"] for r in booked], "errors": result["errors"]}
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("omit", [False, True])
def test_N14_transfer_form_missing_revision_cannot_confirm_changed_partner(c, omit):
    cx, svc, bid, rc, target = _journal_transfer(c, "1", save=False)
    svc.set_rows(bid, {f"dec_{rc.idx}": "include"})
    page = c.get(f"/journal/csv/{bid}").text
    form = html.fromstring(page).xpath(f'//form[.//select[@name="pair_{rc.idx}"]]')[0]
    fields = {el.get("name"): [el.get("value") or ""] for el in form.xpath(".//input[@name]")}
    assert fields[f"pairrev_{rc.idx}"]
    if omit:  # same user-visible decision form as 0.91, without the newly introduced hidden field
        del fields[f"pairrev_{rc.idx}"]
    _change_journal(cx, target, "value")
    fields[f"pair_{rc.idx}"] = ["1"]
    post(c, f"/journal/csv/{bid}/rows", fields)
    svc.evaluate(bid)
    now = next(r for r in svc.rows(bid) if r.idx == rc.idx)
    commit_http = None
    if omit and now.pair_ok == 1:
        # The old page also contains this button; no new comparison is rendered before committing.
        commit_http = post(c, f"/journal/csv/{bid}/commit", {}).status_code
    target_now = dict(cx.db.q1("SELECT value_eur,status FROM journal_tx WHERE tx_id=?", (target,)))
    assert now.pair_ok is None, {"missing_revision": omit, "pair_ok": now.pair_ok,
                                 "value": target_now["value_eur"], "status": target_now["status"],
                                 "commit_http": commit_http}


# ---------------------------------------------------------------------------------------------------------------------
# Eigene Fälle N13: konkurrierende Eingaben bleiben, eigene abgelehnte Eingaben gehen vollständig zurück
# ---------------------------------------------------------------------------------------------------------------------


def test_own_rejected_inputs_are_fully_reverted_without_foreign_change(c, monkeypatch):
    cx, _svc, bid, _mine, other = _rejected_row_action(
        c, monkeypatch, own={"dec_{o}": "skip", "val_{o}": "50", "fee_{o}": "0.5"}, later=None)
    assert _inputs(cx, bid, other.idx) == (None, None, None)


@pytest.mark.parametrize("later", [
    {"dec_{o}": "defer"}, {"val_{o}": "123"}, {"fee_{o}": "0.7"},
    {"dec_{o}": "defer", "val_{o}": "123", "fee_{o}": "1"},
])
def test_foreign_later_inputs_on_another_row_survive(c, monkeypatch, later):
    cx, _svc, bid, _mine, other = _rejected_row_action(c, monkeypatch, own={}, later=later)
    expect = {"dec_{o}": None, "val_{o}": None, "fee_{o}": None}
    expect.update(later)
    assert _inputs(cx, bid, other.idx) == (expect["dec_{o}"], expect["val_{o}"], expect["fee_{o}"])


def test_foreign_later_inputs_on_the_same_row_survive_and_own_rest_is_reverted(c, monkeypatch):
    """Tab A schreibt Entscheidung, Wert und Gebühr; Tab B ändert danach den Wert derselben Zeile – nur dieser bleibt,
    die übrigen eigenen Felder von Tab A gehen zurück."""
    cx, _svc, bid, _mine, other = _rejected_row_action(
        c, monkeypatch, own={"dec_{o}": "skip", "val_{o}": "50", "fee_{o}": "0.5"}, later={"val_{o}": "123"})
    assert _inputs(cx, bid, other.idx) == (None, "123", None)


def test_repeated_restore_never_overwrites_newer_inputs(c):
    cx, svc, bid, _mine, other = _two_rows(c)
    saved = svc.set_rows(bid, {f"val_{other.idx}": "50", f"dec_{other.idx}": "skip"})
    assert not saved["errors"] and saved["undo"]
    assert svc.restore_inputs(bid, saved["undo"]) == 2
    assert _inputs(cx, bid, other.idx) == (None, None, None)
    svc.set_rows(bid, {f"val_{other.idx}": "50"})  # neuer Aufruf schreibt zufällig denselben Wert
    svc.set_rows(bid, {f"dec_{other.idx}": "defer"})
    assert svc.restore_inputs(bid, saved["undo"]) == 1  # alter Rücknahmeauftrag erneut: nur der noch eigene Wert …
    assert _inputs(cx, bid, other.idx) == ("defer", None, None)  # … „defer“ des anderen Aufrufs bleibt


def test_foreign_transfer_decision_survives_rejected_row_action(c, monkeypatch):
    """Transferbestätigung (Journalpartner) eines anderen Tabs nach der Ablehnung bleibt samt Partnerstand erhalten."""
    cx, svc, bid, mine, _other = _two_rows(c)
    _cx, _svc2, bid2, rc2, _target = _journal_transfer(c, "1", save=False)
    page = c.get(f"/journal/csv/{bid2}").text
    rev = html.fromstring(page).xpath(f'//input[@name="pairrev_{rc2.idx}"]')[0].get("value")
    target = str(mine.match["target"])
    fields = form_values(c.get(f"/journal/csv/{bid}").text, f"/journal/csv/{bid}/rows")
    fields["row_action"] = [f"link:{mine.idx}"]
    real_tx, real_restore = BA._execute_tx, svc.restore_inputs

    def change_then_tx(*args, **kwargs):
        _run_editor(cx, lambda: _edit_target(cx, target, "7"))
        return real_tx(*args, **kwargs)

    def confirm_then_restore(batch_id, snap):
        _run_editor(cx, lambda: svc.set_rows(bid2, {f"pair_{rc2.idx}": "1", f"pairrev_{rc2.idx}": rev}))
        return real_restore(batch_id, snap)

    monkeypatch.setattr(BA, "_execute_tx", change_then_tx)
    monkeypatch.setattr(svc, "restore_inputs", confirm_then_restore)
    assert post(c, f"/journal/csv/{bid}/row-action", fields).status_code == 200
    monkeypatch.undo()
    row = cx.db.q1("SELECT pair_ok, pair_rev FROM csv_row WHERE batch_id=? AND idx=?", (bid2, rc2.idx))
    assert (row["pair_ok"], row["pair_rev"]) == (1, rev)


# ---------------------------------------------------------------------------------------------------------------------
# Eigene Fälle N14: fehlende oder leere Revision, interne Aufrufer, beide Entscheidungen in der Übernahme
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("rev", [None, "", "   "])
@pytest.mark.parametrize("decision", ["1", "0"])
def test_transfer_form_without_revision_is_rejected_visibly(c, rev, decision):
    cx, svc, bid, rc, target = _journal_transfer(c, decision, save=False)
    page = c.get(f"/journal/csv/{bid}").text
    form = html.fromstring(page).xpath(f'//form[.//select[@name="pair_{rc.idx}"]]')[0]
    fields = {el.get("name"): [el.get("value") or ""] for el in form.xpath(".//input[@name]")}
    if rev is None:
        del fields[f"pairrev_{rc.idx}"]
    else:
        fields[f"pairrev_{rc.idx}"] = [rev]
    fields[f"pair_{rc.idx}"] = [decision]
    res = post(c, f"/journal/csv/{bid}/rows", fields)
    assert res.status_code == 200 and "ohne angezeigten Stand der Gegenbuchung" in res.text
    svc.evaluate(bid)
    now = next(r for r in svc.rows(bid) if r.idx == rc.idx)
    assert now.pair_ok is None and now.pair_rev is None
    assert post(c, f"/journal/csv/{bid}/commit", {}).status_code == 303
    assert not cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE source='transfer' AND status='active'")
    assert cx.db.q1("SELECT status FROM journal_tx WHERE tx_id=?", (target,))["status"] == "active"


def test_internal_caller_without_revision_is_rejected(c):
    """Interne Aufrufer müssen den erwarteten Stand ausdrücklich mitgeben – kein stiller Rückfall."""
    cx, svc, bid, rc, _target = _journal_transfer(c, "1", save=False)
    res = svc.set_rows(bid, {f"pair_{rc.idx}": "1"})
    assert res["errors"] and not res["undo"]
    assert tuple(cx.db.q1("SELECT pair_ok, pair_rev FROM csv_row WHERE id=?", (rc.id,))) == (None, None)


@pytest.mark.parametrize("change", ["value", "account", "qty", "fee", "delete"])
@pytest.mark.parametrize("when", ["decisions", "execute"])
@pytest.mark.parametrize("choice", ["pair", "nopair"])
def test_takeover_both_decisions_reject_partner_change(config, master, api, monkeypatch, choice, when, change):
    client, sid, cx, target, _before, res = _takeover_with_edit(config, api, monkeypatch, change, when, choice)
    try:
        assert res["errors"], (choice, when, change)
        cx.invalidate_data()
        partner = cx.db.q1("SELECT status, value_eur FROM journal_tx WHERE tx_id=?", (target,))
        assert partner["status"] == ("deleted" if change == "delete" else "active")  # Fremdänderung bleibt
        if change == "value":
            assert partner["value_eur"] == "777"
        row = TK.latest(cx.db, sid)
        assert row["status"] == "staged" and row["decision_id"] is None and row["batch_id"] is None
        assert TK.needs_recovery(cx.db, sid) is None
        assert not cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE source='sync:bitpanda' AND status='active'")
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("choice", ["pair", "nopair"])
def test_takeover_new_confirmation_after_rejection_books_once_and_undoes(config, master, api, monkeypatch, choice):
    client, sid, cx, target, _before, res = _takeover_with_edit(config, api, monkeypatch, "value", "decisions",
                                                               choice)
    try:
        assert res["errors"]
        cx.invalidate_data()
        from tests.test_nachpruefung import _all_state

        base = _all_state(client)  # Stand nach der Fremdänderung (sie bleibt)
        a = TK.assess(cx, sid)
        assert not a.decisions
        for d in [d for d in a.stale if d.get("key")]:  # verworfene Entscheidung entfernen (an Lauf und Stand)
            assert TK.undecide(cx, sid, d["key"], a.rid, d["stamp"])["errors"] == []
        a = TK.assess(cx, sid)
        problem = next(p for p in a.problems if p.get("kind") == "row" and (p.get("partner") or {}).get("kind") == "j")
        assert problem["partner"]["rev"]
        assert TK.decide(cx, sid, problem["key"], choice, a.token)["errors"] == []
        b = TK.assess(cx, sid)
        assert b.ok, [(x.key, x.detail) for x in b.blockers]
        assert TK.apply(cx, sid, b.token)["errors"] == []
        assert TK.apply(cx, sid, b.token)["errors"]  # erneutes Absenden: kein zweiter Lauf
        cx.invalidate_data()
        status = cx.db.q1("SELECT status FROM journal_tx WHERE tx_id=?", (target,))["status"]
        assert status == ("merged" if choice == "pair" else "active")
        assert TK.undo(cx, sid)["errors"] == []
        cx.invalidate_data()
        assert _all_state(client) == base
    finally:
        client.__exit__(None, None, None)
