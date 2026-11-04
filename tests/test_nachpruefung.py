"""Regressionstests zur Nachprüfung „Buchungen prüfen“ vom 04.11.2026 (N01–N04, R03-Entscheidungen je Übernahmelauf).

Soll-Verhalten; synthetische Datenbanken und Anbieterantworten aus den Fixtures. Fehler werden an den tatsächlichen
Commitgrenzen eingespeist (vor bzw. nach der jeweiligen Transaktion), nicht nur an den Phasen-Haken von 0.89.0.
"""

# ruff: noqa: F811  (Fixtures aus anderen Testmodulen werden importiert und als Argumente verwendet)
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from app.csvimport import batch as BA
from app.csvimport.service import csv_service
from app.datasources import takeover as TK
from app.diagnosis import actions as A
from tests.test_bitpanda import api, ctx, master  # noqa: F401  (Fixtures)
from tests.test_review_fixes import c, cfg, economic_state, post  # noqa: F401  (Fixtures/Helfer)
from tests.test_takeover import staged, started


class ProcessStopped(BaseException):
    """Prozessabbruch (kein ``Exception``: wird von keiner Fehlerbehandlung abgefangen)."""


def _sandbox() -> bool:
    return bool(getattr(TK._SANDBOX, "on", False))


def _fault_hide_before(monkeypatch, exc):
    real = A._exec

    def boom(*a, **k):
        if not _sandbox():
            raise exc("vor dem Commit der Ausblendung")
        return real(*a, **k)

    monkeypatch.setattr(A, "_exec", boom)


def _fault_hide_after(monkeypatch, exc):
    real = A._after

    def boom(*a, **k):
        if not _sandbox():
            raise exc("nach dem Commit der Ausblendung")
        return real(*a, **k)

    monkeypatch.setattr(A, "_after", boom)


def _fault_batch_after(monkeypatch, exc, cx):
    svc = csv_service(cx)
    real = svc.evaluate
    state = {"done": False}

    def boom(bid):
        owned = cx.db.scalar("SELECT batch_id FROM ds_takeover WHERE batch_id=?", (bid,))
        if owned is not None and not state["done"]:
            state["done"] = True
            raise exc("nach dem Commit des Stapels")
        return real(bid)

    monkeypatch.setattr(svc, "evaluate", boom)


def _fault_action_before(monkeypatch, exc, cx):
    svc = csv_service(cx)
    real = svc.commit_apply

    def boom(*a, **k):
        raise exc("vor dem Commit der Buchungsaktion")

    monkeypatch.setattr(svc, "commit_apply", boom)
    return real


def _fault_action_after(monkeypatch, exc):
    real = BA.set_selection

    def boom(*a, **k):
        if not _sandbox():
            raise exc("nach dem Commit der Buchungsaktion")
        return real(*a, **k)

    monkeypatch.setattr(BA, "set_selection", boom)


FAULTS = ["hide_before", "hide_after", "batch_after", "action_before", "action_after"]


def _arm(name, monkeypatch, exc, cx):
    if name == "hide_before":
        _fault_hide_before(monkeypatch, exc)
    elif name == "hide_after":
        _fault_hide_after(monkeypatch, exc)
    elif name == "batch_after":
        _fault_batch_after(monkeypatch, exc, cx)
    elif name == "action_before":
        _fault_action_before(monkeypatch, exc, cx)
    else:
        _fault_action_after(monkeypatch, exc)


# ---- N01 · Wirtschaftliche Änderung und Rücknahmebezug in derselben Transaktion ----------------------------------

@pytest.mark.parametrize("where", FAULTS)
def test_N01_error_at_a_commit_boundary_restores_the_old_state(config, master, api, monkeypatch, where):
    client, sid = started(config, api)
    try:
        cx = ctx(client)
        preview = staged(client, sid)
        before = economic_state(client)
        _arm(where, monkeypatch, RuntimeError, cx)
        res = TK.apply(cx, sid, preview.token)
        assert res["errors"]
        monkeypatch.undo()
        cx.invalidate_data()
        last = TK.latest(cx.db, sid)
        assert economic_state(client) == before, (where, dict(last))
        assert last["status"] == "staged" and TK.needs_recovery(cx.db, sid) is None
        assert not cx.db.q("SELECT 1 FROM diag_decision WHERE kind='takeover' AND status='active'")
        assert TK.apply(cx, sid, TK.assess(cx, sid).token)["errors"] == []  # danach normal übernehmbar
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("where", FAULTS)
def test_N01_process_stop_at_a_commit_boundary_is_recoverable(config, master, api, monkeypatch, where):
    client, sid = started(config, api)
    try:
        cx = ctx(client)
        preview = staged(client, sid)
        before = economic_state(client)
        _arm(where, monkeypatch, ProcessStopped, cx)
        with pytest.raises(ProcessStopped):
            TK.apply(cx, sid, preview.token)
        monkeypatch.undo()
        cx.invalidate_data()
        if economic_state(client) != before:  # Teilzustand: muss vollständig referenziert und wiederherstellbar sein
            r = TK.needs_recovery(cx.db, sid)
            assert r is not None and (r["decision_id"] or r["batch_id"] or r["action_id"]), where
            assert "Unterbrochene Übernahme" in client.get(f"/settings/datasources/{sid}/uebernahme").text
            assert TK.start_apply(cx, sid, preview.token)["errors"] == [TK.RECOVER_FIRST]
            assert TK.recover(cx, sid)["errors"] == []
            cx.invalidate_data()
        assert economic_state(client) == before
        assert TK.needs_recovery(cx.db, sid) is None
        assert not cx.db.q("SELECT 1 FROM diag_decision WHERE kind='takeover' AND status='active'")
        assert not cx.db.q("SELECT 1 FROM import_action WHERE status='active'")
    finally:
        client.__exit__(None, None, None)


# ---- N03 · Gemeinsame Rücknahme fortsetzbar --------------------------------------------------------------------

def _applied(config, api):
    client, sid = started(config, api)
    cx = ctx(client)
    before = economic_state(client)
    assert TK.apply(cx, sid, staged(client, sid).token)["errors"] == []
    assert economic_state(client) != before
    return client, sid, cx, before


@pytest.mark.parametrize("mode", ["result", "exception", "process"])
@pytest.mark.parametrize("step", ["action", "hide"])
def test_N03_takeover_undo_resumes_after_a_failed_step(config, master, api, monkeypatch, mode, step):
    client, sid, cx, before = _applied(config, api)
    try:
        target = (BA, "undo") if step == "action" else (A, "undo")
        real = getattr(*target)

        def fail(*a, **k):
            if mode == "exception":
                raise RuntimeError("vorübergehender Fehler")
            if mode == "process":
                raise ProcessStopped
            return {"errors": ["vorübergehend abgelehnt"]} if step == "action" else A.Result(
                errors=["vorübergehend abgelehnt"])

        monkeypatch.setattr(*target, fail)
        if mode == "process":
            with pytest.raises(ProcessStopped):
                TK.undo(cx, sid)
        else:
            assert TK.undo(cx, sid)["errors"]
        monkeypatch.setattr(*target, real)
        last = TK.latest(cx.db, sid)
        assert last["status"] == "undoing" and TK.needs_recovery(cx.db, sid) is not None
        assert "Unterbrochene Übernahme" in client.get(f"/settings/datasources/{sid}/uebernahme").text
        # separate Rücknahme der Ausblendung bleibt gesperrt, solange API-Buchungen des Laufs bestehen
        if step == "action":
            assert A.undo(cx, int(last["decision_id"])).errors
        retry = TK.undo(cx, sid) if mode != "process" else TK.recover(cx, sid)
        assert retry["errors"] == [], retry
        cx.invalidate_data()
        assert economic_state(client) == before and TK.latest(cx.db, sid)["status"] == "undone"
        third = TK.undo(cx, sid)  # dritter Aufruf ändert nichts
        assert third["errors"] and economic_state(client) == before
    finally:
        client.__exit__(None, None, None)


def test_N03_undo_takes_back_every_action_of_the_run(config, master, api):
    """Auch Verknüpfen/Nicht-übernehmen-Aktionen auf dem Stapel des Laufs gehören zur gemeinsamen Rücknahme."""
    client, sid, cx, before = _applied(config, api)
    try:
        bid = int(TK.latest(cx.db, sid)["batch_id"])
        acts = [r["id"] for r in cx.db.q("SELECT id FROM import_action WHERE batch_id=? AND status='active'", (bid,))]
        assert acts
        assert TK.undo(cx, sid)["errors"] == []
        cx.invalidate_data()
        assert economic_state(client) == before
        assert not cx.db.q("SELECT 1 FROM import_action WHERE batch_id=? AND status='active'", (bid,))
    finally:
        client.__exit__(None, None, None)


# ---- N02 · Transferbestätigung gilt für die gesamte angezeigte Wirkungsmenge --------------------------------------

def _partner(svc, bid, task):
    event = task.ref.partition(":")[2]
    rows = svc.rows(bid)
    mine = [r for r in rows if (r.rec.event_key or f"#{r.idx}") == event]
    return mine, next(r for r in rows if r.idx == int(mine[0].pair_ref[2:]))


def _booked(cx, bid):
    return cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE batch_id=? AND status='active'", (bid,))


@pytest.mark.parametrize("change", ["account", "value", "fee", "decision"])
def test_N02_change_of_the_counterpart_requires_a_new_confirmation(c, monkeypatch, change):
    from tests.test_review_fixes import _xfer_sheet, _xfer_task, form_values

    cx = ctx(c)
    svc = csv_service(cx)
    out = _xfer_sheet(c)
    bid = out["bid"]
    task, shown = _xfer_task(c, bid, monkeypatch)
    mine, partner = _partner(svc, bid, task)
    pair_before = {r.idx: r.pair_ok for r in svc.rows(bid)}
    mine_digest = BA.rows_digest(mine)
    if change == "account":
        svc.set_account(partner.row["to_account"] or partner.row["from_account"], "Changed after preview")
    elif change == "value":
        svc.set_rows(bid, {f"val_{partner.idx}": "1234"})
    elif change == "fee":
        svc.set_rows(bid, {f"fee_{partner.idx}": "0.5"})
    else:
        svc.set_rows(bid, {f"dec_{partner.idx}": "defer"})
    svc.evaluate(bid)
    if change == "account":  # die eigene Zeile ist unverändert – nur die Gegenseite hat sich geändert
        assert BA.rows_digest([r for r in svc.rows(bid) if r.idx in {m.idx for m in mine}]) == mine_digest
    shown["a"] = ["row:pair"]
    loc = post(c, "/quality/assistent/antwort", shown).headers["location"]
    err = parse_qs(urlsplit(loc).query).get("err", [""])[0]
    assert "Gegenbuchung" in err and _booked(cx, bid) == 0  # alte Bestätigung: keine wirtschaftliche Änderung
    assert {r.idx: r.pair_ok for r in svc.rows(bid)} == pair_before  # nichts halb gespeichert
    if change == "decision":
        return  # Gegenseite zurückgestellt: sie wird nicht stillschweigend mitgebucht
    fresh = form_values(c.get("/quality/assistent?new=1").text)
    fresh["a"] = ["row:pair"]
    first = post(c, "/quality/assistent/antwort", fresh).headers["location"]
    assert "err=" not in first, first
    again = post(c, "/quality/assistent/antwort", fresh).headers["location"]  # zweites Absenden
    assert "err=" in again and _booked(cx, bid) == 1  # genau einmal
    if change == "account":
        assert cx.db.scalar("SELECT to_account FROM journal_tx WHERE batch_id=? AND status='active'",
                            (bid,)) == "Changed after preview"  # bestätigt wurde der neu angezeigte Stand


def test_N02_link_answer_is_bound_to_the_target_booking(c, monkeypatch):
    """Antwort „ist schon gebucht – verknüpfen“: ändert sich die vorhandene Buchung, gilt die alte Antwort nicht."""
    from tests.test_diagnosis import PF_HEADER, upload
    from tests.test_review_fixes import form_values

    cx = ctx(c)
    from app.diagnosis import guide as G

    bid = upload(c, "dup.csv", PF_HEADER + "N1,2025-01-01T10:00:00Z,deposit,,,,,Wallet K,KAS,10,,,,1,dup\n")
    task = next(t for t in G.tasks(cx) if t.kind == "zeile" and t.ref.startswith(f"{bid}:"))
    monkeypatch.setattr(G, "tasks", lambda ctx, hints=False: [task])
    shown = form_values(c.get("/quality/assistent?new=1").text)
    shown["a"] = ["row:link"]
    target = next(str(t) for rc in csv_service(cx).rows(bid) for t in [(rc.match or {}).get("target")] if t)
    cx.db.x("UPDATE tx SET value_eur='7' WHERE tx_id=?", (target,))  # vorhandene Buchung geändert
    cx.invalidate_data()
    loc = post(c, "/quality/assistent/antwort", shown).headers["location"]
    assert "err=" in loc
    assert not cx.db.q("SELECT 1 FROM tx_link WHERE batch_id=?", (bid,))
