"""Regressionstests zum Review „Buchungen prüfen“ 0.91.0 vom 04.11.2026 (N10–N12).

Die zehn Fälle des Nachweispakets (``test_review_091.py``) sind mit unveränderten fachlichen Erwartungen übernommen;
ergänzt um eigene Fälle über die gerenderten Formulare bis zur Buchung und Rücknahme, Änderungen an Wert, Menge,
Konto, Gebühr und Löschstatus nach der Anzeige, echte parallele Journaländerungen zwischen Prüfung und Schreiben,
erneutes Absenden und Wiederanlauf. Nur synthetische Daten.

Teststeuerung: Die Nebenläufigkeitsfälle ersetzen ``BA.execute`` bzw. ``BA._execute_tx`` durch einen Wrapper, der einen
echten zweiten Thread die Journal- bzw. Importbuchung über den regulären Dienst ändern lässt und danach den
unveränderten Dienst aufruft. Die Prüfung des erwarteten Stands liegt als erste Anweisung in der Schreibtransaktion von
``_execute_tx`` – also hinter beiden Eingriffspunkten.
"""

# ruff: noqa: F811  (Fixtures aus anderen Testmodulen werden importiert und als Argumente verwendet)
from __future__ import annotations

import json
import threading
from decimal import Decimal

import pytest
from lxml import html

from app.csvimport import batch as BA
from app.csvimport.service import csv_service
from app.datasources import takeover as TK
from app.journal.service import journal_service
from tests.test_bitpanda import api, ctx, master  # noqa: F401  (Fixtures)
from tests.test_diagnosis import PF_HEADER, upload
from tests.test_nachpruefung import _all_state, _journal_deposits, _row, _without_counterpart
from tests.test_review_090 import _edit_target
from tests.test_review_fixes import c, cfg, form_values, post  # noqa: F401  (Fixtures/Helfer)
from tests.test_takeover import staged, started

# ---------------------------------------------------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------------------------------------------------


def _duplicate(c):
    cx = ctx(c)
    bid = upload(c, "dup.csv", PF_HEADER + "N1,2025-01-01T10:00:00Z,deposit,,,,,Wallet K,KAS,10,,,,1,dup\n")
    svc = csv_service(cx)
    svc.evaluate(bid)
    rc = next(rc for rc in svc.rows(bid) if (rc.match or {}).get("target"))
    BA.set_selection(cx.db, bid, {rc.idx})
    return cx, svc, bid, rc


def _links(cx, bid):
    return [dict(r) for r in cx.db.q("SELECT * FROM tx_link WHERE batch_id=?", (bid,))]


def _change_import(cx, target, change):
    """Verknüpfungsziel (Import-Buchung D1: Zugang 10 KAS auf Wallet K) regulär ändern bzw. löschen."""
    js = journal_service(cx)
    if change == "delete":
        assert js.delete_import(target)
        return
    form = {"kind": "expert", "date": "2025-01-01", "time": "11:00", "type": "deposit", "to_account": "Wallet K",
            "to_asset": "KAS", "to_qty": "10", "value_eur": "1"}
    form.update({"value": {"value_eur": "7"}, "qty": {"to_qty": "11"}, "account": {"to_account": "Wallet X"},
                 "fee": {"fee_asset": "KAS", "fee_qty": "0.1", "fee_eur": "0.01"}}[change])
    assert not js.save_import(target, form).errors


def _change_journal(cx, tx_id, change):
    """Journalbuchung regulär ändern bzw. löschen (Wert, Menge, Konto, Gebühr, Löschstatus)."""
    js = journal_service(cx)
    if change == "delete":
        assert js.delete(tx_id)
        return
    form = js.form_data(cx.db.q1("SELECT * FROM journal_tx WHERE tx_id=?", (tx_id,)))
    form.update({"value": {"value_eur": "777"}, "qty": {"to_qty": "0.00099"}, "account": {"to_account": "Cold"},
                 "fee": {"fee_asset": "BTC", "fee_qty": "0.000001", "fee_eur": "0.05"}}[change])
    saved = js.save(form, tx_id)
    assert not saved.errors, saved.errors


def _journal_transfer(c, decision, fee="", save=True):
    """Bereits gebuchter BTC-Zugang auf Ledger; neuer passender Abgang auf Sender; Transferentscheidung über das
    gerenderte Formular (ja = „1“, nein = „0“)."""
    cx = ctx(c)
    fund = journal_service(cx).save({"kind": "expert", "date": "2025-07-01", "time": "10:00", "type": "deposit",
                                     "to_account": "Sender", "to_asset": "BTC", "to_qty": "0.01", "value_eur": "550"})
    assert not fund.errors, fund.errors
    saved = journal_service(cx).save({"kind": "expert", "date": "2025-07-12", "time": "14:30", "type": "deposit",
                                      "to_account": "Ledger", "to_asset": "BTC", "to_qty": "0.001",
                                      "value_eur": "55"})
    assert not saved.errors, saved.errors
    target = saved.tx_ids[0]
    fee_cols = "BTC,0.00001,0.5" if fee else ",,"
    bid = upload(c, "out.csv", PF_HEADER + f"OUT,2025-07-12T12:00:00Z,withdrawal,,Sender,BTC,0.001,,,,{fee_cols},55,"
                                           "out\n")
    svc = csv_service(cx)
    svc.evaluate(bid)
    rc = next(r for r in svc.rows(bid) if r.pair_ref == f"j:{target}")
    action = f"/journal/csv/{bid}/rows"
    page = c.get(f"/journal/csv/{bid}").text
    form = html.fromstring(page).xpath(f'//form[@action="{action}"][.//select[@name="pair_{rc.idx}"]]')[0]
    shown = {el.get("name"): [el.get("value") or ""] for el in form.xpath(".//input[@name]")}
    assert f"pairrev_{rc.idx}" in shown  # Entscheidung an den angezeigten Partnerstand gebunden
    if not save:
        return cx, svc, bid, rc, target
    shown[f"pair_{rc.idx}"] = [decision]
    assert post(c, action, shown).status_code == 303
    svc.evaluate(bid)
    rc = next(r for r in svc.rows(bid) if r.idx == rc.idx)
    assert rc.pair_ok == int(decision) and rc.pair_rev
    BA.set_selection(cx.db, bid, {rc.idx})
    return cx, svc, bid, rc, target


def _preview_form(c, bid, action="include"):
    page = c.get(f"/journal/csv/{bid}/preview?action={action}&force=1")
    assert page.status_code == 200
    return form_values(page.text, f"/journal/csv/{bid}/execute")


# ---------------------------------------------------------------------------------------------------------------------
# Fälle des Nachweispakets (unverändert in der Erwartung)
# ---------------------------------------------------------------------------------------------------------------------


def test_N10_old_detail_preview_rejects_changed_link_target(c):
    cx, _svc, bid, rc = _duplicate(c)
    page = c.get(f"/journal/csv/{bid}/preview?action=link&force=1")
    action = f"/journal/csv/{bid}/execute"
    old = form_values(page.text, action)
    target = str(rc.match["target"])
    assert not _edit_target(cx, target, "7").errors
    res = post(c, action, old)
    links = _links(cx, bid)
    assert res.status_code == 409 and not links, {"http": res.status_code, "location": res.headers.get("location"),
                                                  "links": links}


def test_N10_old_detail_row_button_rejects_changed_link_target(c):
    cx, _svc, bid, rc = _duplicate(c)
    page = c.get(f"/journal/csv/{bid}")
    action = f"/journal/csv/{bid}/row-action"
    old = form_values(page.text, f"/journal/csv/{bid}/rows")
    assert html.fromstring(page.text).xpath(f'//button[@formaction="{action}" and @value="link:{rc.idx}"]')
    old["row_action"] = [f"link:{rc.idx}"]
    target = str(rc.match["target"])
    assert not _edit_target(cx, target, "7").errors
    res = post(c, action, old)
    links = _links(cx, bid)
    assert not links, {"http": res.status_code, "location": res.headers.get("location"), "links": links}


def _confirmed_journal_transfer(c, decision):
    cx = ctx(c)
    fund = journal_service(cx).save({"kind": "expert", "date": "2025-07-01", "time": "10:00",
                                     "type": "deposit", "to_account": "Sender", "to_asset": "BTC",
                                     "to_qty": "0.01", "value_eur": "550"})
    assert not fund.errors, fund.errors
    saved = journal_service(cx).save({"kind": "expert", "date": "2025-07-12", "time": "14:30",
                                      "type": "deposit", "to_account": "Ledger", "to_asset": "BTC",
                                      "to_qty": "0.001", "value_eur": "55"})
    assert not saved.errors, saved.errors
    target = saved.tx_ids[0]
    bid = upload(c, "out.csv", PF_HEADER + "OUT,2025-07-12T12:00:00Z,withdrawal,,Sender,BTC,0.001,,,,,,,55,out\n")
    svc = csv_service(cx)
    svc.evaluate(bid)
    rows = svc.rows(bid)
    assert [(r.status, r.pair_ref) for r in rows], rows
    rc = next(r for r in rows if r.pair_ref == f"j:{target}")
    action = f"/journal/csv/{bid}/rows"
    shown = form_values(c.get(f"/journal/csv/{bid}").text, action)
    shown[f"pair_{rc.idx}"] = [decision]
    shown[f"dec_{rc.idx}"] = ["include"]
    assert post(c, action, shown).status_code == 303
    BA.set_selection(cx.db, bid, {rc.idx})
    svc.evaluate(bid)
    rc = next(r for r in svc.rows(bid) if r.idx == rc.idx)
    assert rc.pair_ok == int(decision)
    return cx, svc, bid, rc


@pytest.mark.parametrize("decision", ["1", "0"])
def test_N11_confirmed_journal_transfer_is_executable_in_detail_preview(c, decision):
    _cx, svc, bid, rc = _confirmed_journal_transfer(c, decision)
    p = BA.plan(svc, bid, "include", {rc.idx}, True)
    assert p.todo, {"confirmed": decision, "reason": [it.note for it in p.items],
                    "preview_http": c.get(f"/journal/csv/{bid}/preview?action=include&force=1").status_code}


def _takeover_with_edit(config, api, monkeypatch, change="value", when="execute"):
    """Übernahme mit bestätigtem Journalpartner; ein echter zweiter Thread ändert ihn an der gewählten Stelle."""
    from app.diagnosis import actions as A

    client, sid = _without_counterpart(config, api)
    cx = ctx(client)
    (target,) = _journal_deposits(client, ("2024-02-12T12:30:00Z", "Ledger", "0.001"))
    before = _all_state(client)
    a = staged(client, sid)
    assert TK.decide(cx, sid, _row(a)["key"], "pair", a.token)["errors"] == []
    ready = TK.assess(cx, sid)
    assert ready.ok
    did, errors = [], []
    enter, changed = threading.Event(), threading.Event()

    def other_editor():
        try:
            assert enter.wait(8)
            _change_journal(cx, target, change)
            did.append(True)
        except BaseException as exc:
            errors.append(repr(exc))
        finally:
            changed.set()
            cx.db.close_thread_conn()

    def gate():
        if not getattr(TK._SANDBOX, "on", False) and not did:
            enter.set()
            assert changed.wait(8), "Journal writer unexpectedly blocked"
            assert not errors, errors

    if when == "execute":
        real = BA.execute

        def wrapped(svc, *args, **kwargs):
            gate()
            return real(svc, *args, **kwargs)

        monkeypatch.setattr(BA, "execute", wrapped)
    else:  # vor der Ausblendung (nach der Bewertung der Übernahme)
        real_apply = A.apply_plan

        def wrapped_apply(*args, **kwargs):
            gate()
            return real_apply(*args, **kwargs)

        monkeypatch.setattr(A, "apply_plan", wrapped_apply)
    worker = threading.Thread(target=other_editor)
    worker.start()
    res = TK.apply(cx, sid, ready.token)
    worker.join(8)
    assert did and not errors, errors
    monkeypatch.undo()
    return client, sid, cx, target, before, res


def test_N12_takeover_rejects_journal_partner_edit_after_assessment(config, master, api, monkeypatch):
    client, sid = _without_counterpart(config, api)
    try:
        cx = ctx(client)
        (target,) = _journal_deposits(client, ("2024-02-12T12:30:00Z", "Ledger", "0.001"))
        a = staged(client, sid)
        assert TK.decide(cx, sid, _row(a)["key"], "pair", a.token)["errors"] == []
        ready = TK.assess(cx, sid)
        assert ready.ok
        real = BA.execute
        did = []
        enter, changed = threading.Event(), threading.Event()
        errors = []

        def other_editor():
            try:
                assert enter.wait(8)
                js = journal_service(cx)
                form = js.form_data(cx.db.q1("SELECT * FROM journal_tx WHERE tx_id=?", (target,)))
                form["value_eur"] = "777"
                saved = js.save(form, target)
                assert not saved.errors, saved.errors
                did.append(True)
            except BaseException as exc:
                errors.append(repr(exc))
            finally:
                changed.set()
                cx.db.close_thread_conn()

        def edit_then_apply(svc, *args, **kwargs):
            if not getattr(TK._SANDBOX, "on", False) and not did:
                enter.set()
                assert changed.wait(8), "Journal writer unexpectedly blocked"
                assert not errors, errors
            return real(svc, *args, **kwargs)

        monkeypatch.setattr(BA, "execute", edit_then_apply)
        worker = threading.Thread(target=other_editor)
        worker.start()
        res = TK.apply(cx, sid, ready.token)
        worker.join(8)
        assert did and not errors, errors
        target_now = dict(cx.db.q1("SELECT tx_id, value_eur, status FROM journal_tx WHERE tx_id=?", (target,)))
        assert target_now["value_eur"] == "777"
        assert res["errors"], {"check": res.get("result", {}).get("check"), "target": target_now,
                               "errors": res["errors"]}
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("path", ["preview", "row"])
def test_control_unchanged_detail_link_and_undo_work(c, path):
    cx, _svc, bid, rc = _duplicate(c)
    if path == "preview":
        action = f"/journal/csv/{bid}/execute"
        fields = form_values(c.get(f"/journal/csv/{bid}/preview?action=link&force=1").text, action)
    else:
        action = f"/journal/csv/{bid}/row-action"
        fields = form_values(c.get(f"/journal/csv/{bid}").text, f"/journal/csv/{bid}/rows")
        fields["row_action"] = [f"link:{rc.idx}"]
    res = post(c, action, fields)
    assert res.status_code == 303
    assert cx.db.scalar("SELECT COUNT(*) FROM tx_link WHERE batch_id=?", (bid,)) == 1
    aid = cx.db.scalar("SELECT id FROM import_action WHERE batch_id=?", (bid,))
    undone = post(c, f"/journal/csv/{bid}/actions/{aid}/undo", {})
    assert undone.status_code == 303
    assert cx.db.scalar("SELECT COUNT(*) FROM tx_link WHERE batch_id=? AND status='active'", (bid,)) == 0


@pytest.mark.parametrize("decision", ["1", "0"])
def test_control_legacy_commit_can_complete_the_confirmed_journal_path(c, decision):
    cx, svc, bid, _rc = _confirmed_journal_transfer(c, decision)
    before = dict(cx.ledger().balances)
    res = post(c, f"/journal/csv/{bid}/commit", {})
    assert res.status_code == 303 and "msg=commit" in res.headers.get("location", "")
    assert svc.rows(bid)[0].status in ("merged", "committed")
    assert cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE source='transfer' AND status='active'") == int(decision)
    back = post(c, f"/journal/csv/{bid}/revert", {})
    assert back.status_code == 303
    cx.invalidate_data()
    assert {k: v for k, v in cx.ledger().balances.items() if v} == {k: v for k, v in before.items() if v}


def test_control_standalone_review_include_has_full_apply_undo_path(config, master, api):
    client, sid = started(config, api)
    try:
        cx = ctx(client)
        before = _all_state(client)
        assert not TK.run_fetch(cx, sid)["errors"]
        saved = TK.latest(cx.db, sid)
        events = json.loads(saved["events_json"])
        events[0]["review"] = "Synthetic fully representable record requiring explicit confirmation"
        cx.db.x("UPDATE ds_takeover SET events_json=? WHERE id=?", (json.dumps(events), saved["id"]))
        a = TK.assess(cx, sid)
        problem = _row(a)
        assert problem["options"] == ["include", "skip"]
        assert not TK.decide(cx, sid, problem["key"], "include", a.token)["errors"]
        ready = TK.assess(cx, sid)
        assert ready.ok
        assert not TK.apply(cx, sid, ready.token)["errors"]
        assert not TK.undo(cx, sid)["errors"]
        cx.invalidate_data()
        assert _all_state(client) == before
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------------------------------------------------
# Eigene Fälle N10: jede Änderung des angezeigten Ziels, erneutes Absenden, keine teilweise gespeicherten Eingaben
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("change", ["value", "qty", "account", "fee", "delete"])
@pytest.mark.parametrize("path", ["preview", "row"])
def test_detail_actions_reject_any_change_of_the_shown_target(c, path, change):
    cx, _svc, bid, rc = _duplicate(c)
    if path == "preview":
        action = f"/journal/csv/{bid}/execute"
        old = _preview_form(c, bid, "link")
    else:
        action = f"/journal/csv/{bid}/row-action"
        old = form_values(c.get(f"/journal/csv/{bid}").text, f"/journal/csv/{bid}/rows")
        old["row_action"] = [f"link:{rc.idx}"]
        old[f"dec_{rc.idx}"] = ["skip"]  # weitere Eingabe derselben Seite – darf bei Ablehnung nicht bleiben
    _change_import(cx, str(rc.match["target"]), change)
    res = post(c, action, old)
    assert not _links(cx, bid), change
    assert not cx.db.scalar("SELECT COUNT(*) FROM import_action WHERE batch_id=?", (bid,))
    if path == "preview":
        assert res.status_code == 409
    else:
        assert res.status_code == 200 and "geändert" in res.text
        assert cx.db.scalar("SELECT decision FROM csv_row WHERE batch_id=? AND idx=?", (bid, rc.idx)) is None
    assert not post(c, action, old).headers.get("location", "").endswith("#verlauf")  # erneut: weiterhin abgelehnt
    assert not _links(cx, bid)


@pytest.mark.parametrize("path", ["preview", "row"])
def test_detail_link_after_rejection_works_exactly_once_and_undoes(c, path):
    cx, _svc, bid, rc = _duplicate(c)
    old = _preview_form(c, bid, "link") if path == "preview" else None
    _change_import(cx, str(rc.match["target"]), "value")
    if path == "preview":
        assert post(c, f"/journal/csv/{bid}/execute", old).status_code == 409
        action, fresh = f"/journal/csv/{bid}/execute", _preview_form(c, bid, "link")
    else:
        action = f"/journal/csv/{bid}/row-action"
        fresh = form_values(c.get(f"/journal/csv/{bid}").text, f"/journal/csv/{bid}/rows")
        fresh["row_action"] = [f"link:{rc.idx}"]
    assert post(c, action, fresh).status_code == 303
    assert len(_links(cx, bid)) == 1
    post(c, action, fresh)  # erneutes Absenden: Vorschau genau einmal (Token) bzw. Zeile bereits erledigt
    assert len(_links(cx, bid)) == 1
    aid = cx.db.scalar("SELECT id FROM import_action WHERE batch_id=?", (bid,))
    assert post(c, f"/journal/csv/{bid}/actions/{aid}/undo", {}).status_code == 303
    assert cx.db.scalar("SELECT COUNT(*) FROM tx_link WHERE batch_id=? AND status='active'", (bid,)) == 0


def test_execute_requires_binding_even_with_a_fresh_fingerprint(c):
    """Dienstebene: Wer den Fingerabdruck erst beim Absenden bildet, ist über ``Plan.refs`` trotzdem an den Stand bei
    der Planung gebunden – und mit ``expect_refs`` an den angezeigten."""
    cx, svc, bid, rc = _duplicate(c)
    shown = BA.expected_revs(cx.db.conn, [rc])
    _change_import(cx, str(rc.match["target"]), "value")
    svc.evaluate(bid)
    p = BA.plan(svc, bid, "link", {rc.idx}, True)
    res = BA.execute(svc, bid, "link", {rc.idx}, token=BA.new_token(), fingerprint=p.fingerprint(), force=True,
                     expect_refs=shown)
    assert res["errors"] and not _links(cx, bid)


# ---------------------------------------------------------------------------------------------------------------------
# Eigene Fälle N11: gerenderte Formulare → Entscheidung → Vorschau → Buchung → vollständige Rücknahme
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("fee", ["", "fee"])
@pytest.mark.parametrize("decision", ["1", "0"])
def test_journal_transfer_decision_books_through_preview_and_undoes(c, decision, fee):
    cx, _svc, bid, _rc, target = _journal_transfer(c, decision, fee)
    before = _all_state(c)
    shown = _preview_form(c, bid)
    res = post(c, f"/journal/csv/{bid}/execute", shown)
    assert res.status_code == 303, res.text[:400]
    cx.invalidate_data()
    transfers = [dict(r) for r in cx.db.q("SELECT * FROM journal_tx WHERE source='transfer' AND status='active'")]
    partner = cx.db.q1("SELECT status FROM journal_tx WHERE tx_id=?", (target,))["status"]
    if decision == "1":
        assert len(transfers) == 1 and partner == "merged"
        t = transfers[0]
        assert (t["from_account"], t["to_account"], Decimal(t["from_qty"])) == ("Sender", "Ledger", Decimal("0.001"))
        if fee:
            assert t["fee_asset"] == "BTC" and Decimal(t["fee_qty"]) == Decimal("0.00001")
        lots = [lot for lot in cx.ledger().lots if lot.account == "Ledger" and lot.qty]
        assert lots and all(str(lot.acq_date).startswith("2025-07-01") for lot in lots)  # Haltedauer fortgeführt
    else:
        assert not transfers and partner == "active"
        out = cx.db.q1("SELECT * FROM journal_tx WHERE batch_id=? AND status='active'", (bid,))
        assert out["type"] == "withdrawal" and (bool(fee) == (out["fee_asset"] == "BTC"))
    assert post(c, f"/journal/csv/{bid}/execute", shown).status_code == 303  # erneut: nichts doppelt
    assert cx.db.scalar("SELECT COUNT(*) FROM import_action WHERE batch_id=?", (bid,)) == 1
    aid = cx.db.scalar("SELECT id FROM import_action WHERE batch_id=?", (bid,))
    assert post(c, f"/journal/csv/{bid}/actions/{aid}/undo", {}).status_code == 303
    cx.invalidate_data()
    assert _all_state(c) == before


@pytest.mark.parametrize("change", ["value", "qty", "account", "fee", "delete"])
def test_partner_change_after_transfer_decision_voids_it(c, change):
    """Geänderter Journalpartner nach der Entscheidung: alte Vorschau abgelehnt, Entscheidung sichtbar zurückgesetzt,
    nichts gebucht; der Nutzer entscheidet neu."""
    cx, svc, bid, rc, target = _journal_transfer(c, "1")
    old = _preview_form(c, bid)
    _change_journal(cx, target, change)
    res = post(c, f"/journal/csv/{bid}/execute", old)
    assert res.status_code == 409
    assert not cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE source='transfer' AND status='active'")
    assert not cx.db.scalar("SELECT COUNT(*) FROM import_action WHERE batch_id=?", (bid,))
    svc.evaluate(bid)
    now = next(r for r in svc.rows(bid) if r.idx == rc.idx)
    assert now.pair_ok is None
    if now.pair_ref == f"j:{target}":  # Partner passt weiterhin: Transferfrage offen, nicht ohne neue Entscheidung
        assert not BA.plan(svc, bid, "include", {rc.idx}, True).todo
    else:  # gelöscht bzw. passt nicht mehr: kein Transferpartner – nie als Transfer mit dem alten Partner
        assert change in ("delete", "account", "qty")
    # neu bestätigen (falls der Partner noch passt) – dann genau einmal ausführbar
    if now.pair_ref == f"j:{target}":
        page = c.get(f"/journal/csv/{bid}").text
        assert "nach deiner Transferentscheidung geändert" in page
        form = html.fromstring(page).xpath(f'//form[.//select[@name="pair_{rc.idx}"]]')[0]
        shown = {el.get("name"): [el.get("value") or ""] for el in form.xpath(".//input[@name]")}
        shown[f"pair_{rc.idx}"] = ["1"]
        assert post(c, f"/journal/csv/{bid}/rows", shown).status_code == 303
        assert post(c, f"/journal/csv/{bid}/execute", _preview_form(c, bid)).status_code == 303
        assert cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE source='transfer' AND status='active'") == 1


def test_stale_transfer_decision_form_does_not_bind_to_the_new_state(c):
    """Formular mit Transferentscheidung, angezeigt vor einer Partneränderung: die Entscheidung gilt dem alten Stand
    und wird nicht für den neuen übernommen."""
    cx, svc, bid, rc, target = _journal_transfer(c, "1", save=False)
    page = c.get(f"/journal/csv/{bid}").text
    form = html.fromstring(page).xpath(f'//form[.//select[@name="pair_{rc.idx}"]]')[0]
    shown = {el.get("name"): [el.get("value") or ""] for el in form.xpath(".//input[@name]")}
    _change_journal(cx, target, "value")
    shown[f"pair_{rc.idx}"] = ["1"]
    assert post(c, f"/journal/csv/{bid}/rows", shown).status_code == 303
    svc.evaluate(bid)
    assert next(r for r in svc.rows(bid) if r.idx == rc.idx).pair_ok is None
    assert not BA.plan(svc, bid, "include", {rc.idx}, True).todo


def test_legacy_commit_also_rejects_changed_partner_inside_its_transaction(c, monkeypatch):
    """Älterer Weg „Buchungen übernehmen“: derselbe Vertrag – Partner nach der Bewertung, vor der Schreibtransaktion
    von einem zweiten Thread geändert → abgelehnt, nichts gebucht, Fremdänderung bleibt."""
    cx, svc, bid, rc, target = _journal_transfer(c, "1")
    real = svc.commit_plan

    def plan_then_edit(*args, **kwargs):
        out = real(*args, **kwargs)
        t = threading.Thread(target=lambda: (_change_journal(cx, target, "value"), cx.db.close_thread_conn()))
        t.start()
        t.join(8)
        return out

    monkeypatch.setattr(svc, "commit_plan", plan_then_edit)
    res = svc.commit(bid)
    monkeypatch.undo()
    assert res["errors"]
    partner = cx.db.q1("SELECT status, value_eur FROM journal_tx WHERE tx_id=?", (target,))
    assert partner["status"] == "active" and partner["value_eur"] == "777"
    assert not cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE batch_id=? AND status IN ('active', 'merged')",
                            (bid,))
    assert next(r for r in svc.rows(bid) if r.idx == rc.idx).pair_ok is None  # sichtbar neu zu entscheiden


# ---------------------------------------------------------------------------------------------------------------------
# Eigene Fälle: echte parallele Journaländerung zwischen Prüfung und Schreiben (Detailvorschau)
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["link", "transfer"])
def test_detail_preview_rejects_parallel_edit_between_check_and_write(c, monkeypatch, kind):
    if kind == "link":
        cx, _svc, bid, rc = _duplicate(c)
        shown, target = _preview_form(c, bid, "link"), str(rc.match["target"])

        def edit():
            _change_import(cx, target, "value")
    else:
        cx, _svc, bid, rc, target = _journal_transfer(c, "1")
        shown = _preview_form(c, bid)

        def edit():
            _change_journal(cx, target, "value")

    enter, changed, errors = threading.Event(), threading.Event(), []

    def other_editor():
        try:
            assert enter.wait(8)
            edit()
        except BaseException as exc:
            errors.append(repr(exc))
        finally:
            changed.set()
            cx.db.close_thread_conn()

    real = BA._execute_tx

    def after_check(*args, **kwargs):
        enter.set()
        assert changed.wait(8), "Journal writer unexpectedly blocked"
        return real(*args, **kwargs)

    monkeypatch.setattr(BA, "_execute_tx", after_check)
    worker = threading.Thread(target=other_editor)
    worker.start()
    res = post(c, f"/journal/csv/{bid}/execute", shown)
    worker.join(8)
    assert not errors, errors
    assert res.status_code == 409
    assert not _links(cx, bid)
    assert not cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE source='transfer' AND status='active'")
    assert not cx.db.scalar("SELECT COUNT(*) FROM import_action WHERE batch_id=?", (bid,))


# ---------------------------------------------------------------------------------------------------------------------
# Eigene Fälle N12: Übernahme – Fremdänderung bleibt, Lauf vollständig kompensiert, Wiederanlauf genau einmal
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("change", ["value", "fee", "qty", "account", "delete"])
@pytest.mark.parametrize("when", ["execute", "hide"])
def test_takeover_rejects_partner_change_and_recovers(config, master, api, monkeypatch, when, change):
    client, sid, cx, target, _before, res = _takeover_with_edit(config, api, monkeypatch, change, when)
    try:
        assert res["errors"], (when, change)
        cx.invalidate_data()
        partner = cx.db.q1("SELECT status, value_eur, to_account, to_qty, fee_qty FROM journal_tx WHERE tx_id=?",
                           (target,))
        assert partner["status"] == ("deleted" if change == "delete" else "active")  # Fremdänderung bleibt
        if change == "value":
            assert partner["value_eur"] == "777"
        row = TK.latest(cx.db, sid)
        assert row["status"] == "staged" and row["decision_id"] is None and row["batch_id"] is None
        assert TK.needs_recovery(cx.db, sid) is None
        assert not cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE source='transfer' AND status='active'")
        # Wiederanlauf: die alte Entscheidung gilt nicht mehr; neu entscheiden → genau eine erfolgreiche Übernahme
        a = TK.assess(cx, sid)
        assert not a.decisions
        if change not in ("value", "fee"):
            return  # Partner passt nicht mehr bzw. gelöscht: keine Transferfrage mehr zu diesem Partner
        stale = [d for d in a.stale if d.get("why") == "Gegenbuchung hat sich geändert"]
        assert stale, a.stale
        for d in stale:  # wie auf der Seite: verworfene Entscheidung entfernen (an Lauf und Stand gebunden)
            assert TK.undecide(cx, sid, d["key"], a.rid, d["stamp"])["errors"] == []
        a = TK.assess(cx, sid)
        problem = next((p for p in a.problems if p.get("kind") == "row"
                        and (p.get("partner") or {}).get("kind") == "j"), None)
        if problem is None:  # nach der Änderung passt der Partner im Zustand nach dem Ausblenden nicht mehr
            assert change != "value"
            return
        assert TK.decide(cx, sid, problem["key"], "pair", a.token)["errors"] == []
        b = TK.assess(cx, sid)
        assert b.ok, [(x.key, x.detail) for x in b.blockers]
        assert TK.apply(cx, sid, b.token)["errors"] == []
        assert TK.apply(cx, sid, b.token)["errors"]  # erneutes Absenden: kein zweiter Lauf
        cx.invalidate_data()
        assert cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE source='transfer' AND status='active'") == 1
        assert TK.undo(cx, sid)["errors"] == []
        cx.invalidate_data()
        assert cx.db.q1("SELECT status FROM journal_tx WHERE tx_id=?", (target,))["status"] == "active"
    finally:
        client.__exit__(None, None, None)


def test_takeover_unchanged_journal_partner_still_merges_and_undoes(config, master, api):
    """Kontrolle: ohne Fremdänderung bleibt der Lebenszyklus (Zusammenführung, exakte Bestände, Rücknahme) erhalten."""
    client, sid = _without_counterpart(config, api)
    try:
        cx = ctx(client)
        (target,) = _journal_deposits(client, ("2024-02-12T12:30:00Z", "Ledger", "0.001"))
        before = _all_state(client)
        a = staged(client, sid)
        problem = _row(a)
        assert problem["partner"]["rev"]
        assert TK.decide(cx, sid, problem["key"], "pair", a.token)["errors"] == []
        b = TK.assess(cx, sid)
        assert b.ok and TK.apply(cx, sid, b.token)["errors"] == []
        cx.invalidate_data()
        assert cx.db.q1("SELECT status FROM journal_tx WHERE tx_id=?", (target,))["status"] == "merged"
        assert TK.undo(cx, sid)["errors"] == []
        cx.invalidate_data()
        assert _all_state(client) == before
    finally:
        client.__exit__(None, None, None)
