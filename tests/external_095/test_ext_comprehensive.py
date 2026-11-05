"""Broad invariant and route review of Portfolia 0.94.1; all expectations describe desired behavior."""

# ruff: noqa: F401, F811, S101
from __future__ import annotations

import copy
import importlib.util
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from lxml import html

from app.csvimport import batch as BA
from app.csvimport import bulk as BU
from app.csvimport.service import csv_service, pair_cand_rev, rec_to_json, ref_revs
from app.datasources import takeover as TK
from app.diagnosis import actions as DA
from app.diagnosis import guide as G
from app.journal.service import journal_service
from tests.test_bitpanda import ctx
from tests.test_diagnosis import PF_HEADER, upload
from tests.test_nachpruefung import _all_state
from tests.test_review_090 import _edit_target
from tests.test_review_091 import _duplicate, _journal_transfer
from tests.test_review_092 import _run_editor, _two_rows
from tests.test_review_fixes import c, cfg, form_values, post

_spec = importlib.util.spec_from_file_location(
    "previous_review_094", Path(__file__).with_name("previous_review_094.py")
)
_previous = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_previous)
_withdrawal, _add_partner, _transfer_form = _previous._withdrawal, _previous._add_partner, _previous._transfer_form


def _current(cx, rid, field):
    return cx.db.scalar(f"SELECT {field} FROM csv_row WHERE id=?", (rid,))


GROUPS = [("dec", "decision", "skip", "defer"), ("val", "value_in", "50", "99"), ("fee", "fee_in", "0.5", "0.9")]


@pytest.mark.parametrize("group,column,a,b", GROUPS)
@pytest.mark.parametrize("later", ["different", "aba", "equal_marked", "equal_internal", "none", "repeated_undo"])
def test_input_ownership_matrix(c, group, column, a, b, later):
    cx, svc, bid, _mine, other = _two_rows(c)
    marks = svc.input_marks(bid)
    first = svc.set_rows(bid, {f"{group}_{other.idx}": a})
    assert first["undo"] and not first["errors"]
    expected = a
    if later == "different":
        _run_editor(cx, lambda: svc.set_rows(bid, {f"{group}_{other.idx}": b}))
        expected = b
    elif later == "aba":
        _run_editor(cx, lambda: svc.set_rows(bid, {f"{group}_{other.idx}": b}))
        _run_editor(cx, lambda: svc.set_rows(bid, {f"{group}_{other.idx}": a}))
    elif later in ("equal_marked", "equal_internal"):
        data = {f"{group}_{other.idx}": a}
        if later == "equal_marked":
            data[f"iv_{other.idx}"] = marks[other.idx]
        _run_editor(cx, lambda: svc.set_rows(bid, data))
    elif later == "none":
        expected = None
    elif later == "repeated_undo":
        assert svc.restore_inputs(bid, first["undo"]) == 1
        _run_editor(cx, lambda: svc.set_rows(bid, {f"{group}_{other.idx}": a}))
    svc.restore_inputs(bid, first["undo"])
    assert _current(cx, other.id, column) == expected, {"group": group, "later": later}


@pytest.mark.parametrize("group,column,a,b", GROUPS)
@pytest.mark.parametrize("intent", ["untouched", "conflict", "same_explicit", "fresh"])
def test_stale_input_matrix(c, group, column, a, b, intent):
    cx, svc, bid, _mine, other = _two_rows(c)
    original = svc.input_marks(bid)[other.idx]
    _run_editor(cx, lambda: svc.set_rows(bid, {f"{group}_{other.idx}": a}))
    mark = svc.input_marks(bid)[other.idx] if intent == "fresh" else original
    value = "" if intent == "untouched" else b if intent in ("conflict", "fresh") else a
    out = svc.set_rows(bid, {f"{group}_{other.idx}": value, f"iv_{other.idx}": mark})
    expected = b if intent == "fresh" else a
    assert bool(out["errors"]) == (intent == "conflict")
    assert _current(cx, other.id, column) == expected


@pytest.mark.parametrize("group,column,a,b", GROUPS)
@pytest.mark.parametrize("race", [False, True])
def test_display_snapshot_covers_each_input_group(c, monkeypatch, group, column, a, b, race):
    cx, svc, bid, _mine, other = _two_rows(c)
    if group == "fee":
        recs = [r.rec for r in svc.rows(bid)]
        recs[1].fee_sym, recs[1].fee_qty = "ETH", Decimal("0.01")
        payload = ("[" + ",".join(rec_to_json(r) for r in recs) + "]").encode()
        bid = svc.ingest(
            recs,
            source="csv:portfolia",
            profile="portfolia",
            account="",
            label="Fee input",
            datasource_id=None,
            payload=payload,
            options={"cutoff": ""},
        )
        svc.evaluate(bid)
        other = svc.rows(bid)[1]
    real = svc.input_marks
    changed = []

    def marks(batch_id):
        if race and not changed:
            _run_editor(cx, lambda: svc.set_rows(bid, {f"{group}_{other.idx}": a}))
            changed.append(1)
        return real(batch_id)

    monkeypatch.setattr(svc, "input_marks", marks)
    fields = form_values(c.get(f"/journal/csv/{bid}").text, f"/journal/csv/{bid}/rows")
    monkeypatch.undo()
    assert f"{group}_{other.idx}" in fields
    assert post(c, f"/journal/csv/{bid}/rows", fields).status_code in (200, 303)
    assert _current(cx, other.id, column) == (a if race else None)


@pytest.mark.parametrize("change", [False, True])
def test_displayed_comparison_and_hidden_reference_share_a_snapshot(c, monkeypatch, change):
    cx, _svc, bid, rc = _duplicate(c)
    target = str(rc.match["target"])
    real = BA.row_revision
    changed = []

    def revision(conn, row):
        if change and not changed:
            _run_editor(cx, lambda: _edit_target(cx, target, "777"))
            changed.append(1)
        return real(conn, row)

    monkeypatch.setattr(BA, "row_revision", revision)
    fields = form_values(c.get(f"/journal/csv/{bid}").text, f"/journal/csv/{bid}/rows")
    monkeypatch.undo()
    fields["row_action"] = [f"link:{rc.idx}"]
    response = post(c, f"/journal/csv/{bid}/row-action", fields)
    links = cx.db.scalar("SELECT COUNT(*) FROM tx_link WHERE batch_id=? AND status='active'", (bid,))
    if change:
        assert links == 0, {"http": response.status_code, "active_links": links, "target": target}
    else:
        assert response.status_code == 303 and links == 1


@pytest.mark.parametrize("kind", ["account", "symbol", "spam"])
@pytest.mark.parametrize("when", ["none", "before_preview", "before_commit"])
def test_mapping_changes_are_dependencies_of_booking(c, monkeypatch, kind, when):
    cx, svc, bid, rc = _withdrawal(c)

    def change():
        if kind == "account":
            svc.set_account("Sender", "Changed Sender")
        else:
            svc.set_symbol("BTC", "ETH" if kind == "symbol" else None)

    if when == "before_preview":
        change()
        svc.evaluate(bid)
        now = next(r for r in svc.rows(bid) if r.idx == rc.idx)
        if kind == "spam":
            assert not BA.plan(svc, bid, "include", {rc.idx}, True).todo
        elif kind == "account":
            assert now.row["from_account"] == "Changed Sender"
        else:
            assert now.row["from_asset"] == "ETH"
        return
    shown = form_values(c.get(f"/journal/csv/{bid}/preview?action=include&force=1").text, f"/journal/csv/{bid}/execute")
    if when == "before_commit":
        real = BA._execute_tx

        def race(*args, **kwargs):
            _run_editor(cx, change)
            return real(*args, **kwargs)

        monkeypatch.setattr(BA, "_execute_tx", race)
    response = post(c, f"/journal/csv/{bid}/execute", shown)
    monkeypatch.undo()
    booked = cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE batch_id=? AND status='active'", (bid,))
    if when == "before_commit":
        assert response.status_code == 409 and booked == 0, {
            "kind": kind,
            "http": response.status_code,
            "booked": booked,
        }
    else:
        assert response.status_code == 303 and booked == 1


@pytest.mark.parametrize("op", ["skip", "defer"])
@pytest.mark.parametrize("later", ["none", "different", "aba", "same_marked"])
def test_audited_action_undo_respects_later_input_ownership(c, op, later):
    cx, svc, bid, mine, _other = _two_rows(c)
    original_mark = svc.input_marks(bid)[mine.idx]
    p = BA.plan(svc, bid, op, {mine.idx})
    result = BA.execute(svc, bid, op, {mine.idx}, token=BA.new_token(), fingerprint=p.fingerprint())
    assert not result["errors"]
    expected = None if later == "none" else op
    other = "defer" if op == "skip" else "skip"
    if later in ("different", "aba"):
        svc.set_rows(bid, {f"dec_{mine.idx}": other})
        expected = other
    if later == "aba":
        svc.set_rows(bid, {f"dec_{mine.idx}": op})
        expected = op
    if later == "same_marked":
        svc.set_rows(bid, {f"dec_{mine.idx}": op, f"iv_{mine.idx}": original_mark})
    assert not BA.undo(svc, result["action_id"])["errors"]
    assert _current(cx, mine.id, "decision") == expected, {"op": op, "later": later}


@pytest.mark.parametrize("route", ["batch", "bulk", "legacy", "row"])
@pytest.mark.parametrize("phantom", [False, True])
def test_first_candidate_across_booking_entrypoints(c, monkeypatch, route, phantom):
    cx, svc, bid, rc = _withdrawal(c)
    added = []

    def edit():
        if phantom and not added:
            _run_editor(cx, lambda: added.append(_add_partner(cx)))

    if route == "legacy":
        real = svc.commit_plan

        def commit_plan(*args, **kwargs):
            out = real(*args, **kwargs)
            edit()
            return out

        monkeypatch.setattr(svc, "commit_plan", commit_plan)
        result = svc.commit(bid)
    else:
        real = BA._execute_tx

        def before_commit(*args, **kwargs):
            edit()
            return real(*args, **kwargs)

        monkeypatch.setattr(BA, "_execute_tx", before_commit)
        if route == "batch":
            p = BA.plan(svc, bid, "include", {rc.idx}, True)
            result = BA.execute(
                svc, bid, "include", {rc.idx}, token=BA.new_token(), fingerprint=p.fingerprint(), force=True
            )
        elif route == "bulk":
            mp = BU.preview(svc, {bid: {rc.idx}})
            assert len(mp.parts) == 1 and mp.todo == 1
            part = mp.parts[0]
            result = BU.run(
                svc, [{"batch_id": bid, "idxs": [rc.idx], "token": part.token, "fingerprint": part.fingerprint}]
            )[0]
        else:
            fields = form_values(c.get(f"/journal/csv/{bid}").text, f"/journal/csv/{bid}/rows")
            fields["row_action"] = [f"include:{rc.idx}"]
            response = post(c, f"/journal/csv/{bid}/row-action", fields)
            result = {"errors": [] if response.status_code == 303 else ["rejected"]}
    monkeypatch.undo()
    booked = cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE batch_id=? AND status='active'", (bid,))
    assert bool(result["errors"]) == phantom and booked == (0 if phantom else 1), {
        "route": route,
        "phantom": phantom,
        "booked": booked,
        "result": result,
    }


@pytest.mark.parametrize("count", [1, 2, 10, 11, 12, 20])
def test_candidate_count_does_not_create_a_decision_loop(c, count):
    cx, svc, bid, rc = _withdrawal(c)
    js = journal_service(cx)
    for i in range(count):
        out = js.save(
            {
                "kind": "expert",
                "date": "2025-07-12",
                "time": "14:30",
                "type": "deposit",
                "to_account": f"Ledger {i}",
                "to_asset": "BTC",
                "to_qty": "0.001",
                "value_eur": "55",
            }
        )
        assert not out.errors
    svc.evaluate(bid)
    before = _all_state(c)
    fields = _transfer_form(c, bid, rc.idx)
    fields[f"pair_{rc.idx}"] = ["0"]
    assert post(c, f"/journal/csv/{bid}/rows", fields).status_code == 303
    now = next(r for r in svc.rows(bid) if r.idx == rc.idx)
    assert now.pair_ok == 0, {
        "candidates": count,
        "saved_alternatives": len(now.pair_alts),
        "pair_ok": now.pair_ok,
        "why": now.pair_why,
    }
    shown = form_values(c.get(f"/journal/csv/{bid}/preview?action=include&force=1").text, f"/journal/csv/{bid}/execute")
    assert post(c, f"/journal/csv/{bid}/execute", shown).status_code == 303
    aid = cx.db.scalar("SELECT MAX(id) FROM import_action WHERE batch_id=?", (bid,))
    assert post(c, f"/journal/csv/{bid}/actions/{aid}/undo", {}).status_code == 303
    cx.invalidate_data()
    assert _all_state(c) == before


@pytest.mark.parametrize("kind", ["journal", "batch"])
@pytest.mark.parametrize("later", ["none", "different", "aba", "equal_marked", "equal_internal", "repeat"])
def test_pair_ownership_includes_both_rows_and_revision(c, kind, later):
    if kind == "journal":
        cx, svc, bid, rc, _target = _journal_transfer(c, "1", save=False)
    else:
        cx, svc, bid, rc = _withdrawal(c, with_batch_partner=True)
    marks = svc.input_marks(bid)
    rev = pair_cand_rev(cx.db.conn, rc)

    def data(value, marked=False):
        fields = {f"pair_{rc.idx}": str(value)}
        if rev:
            fields[f"pairrev_{rc.idx}"] = rev
        if marked:
            fields[f"iv_{rc.idx}"] = marks[rc.idx]
        return fields

    first = svc.set_rows(bid, data(1))
    assert first["undo"] and not first["errors"]
    expected = 1
    if later == "none":
        expected = None
    elif later == "different":
        _run_editor(cx, lambda: svc.set_rows(bid, data(0)))
        expected = 0
    elif later == "aba":
        _run_editor(cx, lambda: svc.set_rows(bid, data(0)))
        _run_editor(cx, lambda: svc.set_rows(bid, data(1)))
    elif later in ("equal_marked", "equal_internal"):
        _run_editor(cx, lambda: svc.set_rows(bid, data(1, later == "equal_marked")))
    elif later == "repeat":
        assert svc.restore_inputs(bid, first["undo"])
        _run_editor(cx, lambda: svc.set_rows(bid, data(1)))
    svc.restore_inputs(bid, first["undo"])
    now = next(r for r in svc.rows(bid) if r.idx == rc.idx)
    assert now.pair_ok == expected, {"kind": kind, "later": later, "actual": now.pair_ok}
    if kind == "journal":
        assert now.pair_rev == (rev if expected is not None else None)
    else:
        partner = next(r for r in svc.rows(bid) if r.idx == int(rc.pair_ref[2:]))
        assert partner.pair_ok == expected, {
            "kind": kind,
            "later": later,
            "source": now.pair_ok,
            "partner": partner.pair_ok,
        }


@pytest.mark.parametrize("change", ["none", "candidate", "account", "symbol"])
def test_assistant_confirmation_uses_the_same_dependency_contract(c, monkeypatch, change):
    cx, svc, _bid, rc = _withdrawal(c)
    rec = rc.rec
    rec.review = "Source asks for manual review"
    payload = ("[" + rec_to_json(rec) + "]").encode()
    bid = svc.ingest(
        [rec],
        source="csv:portfolia",
        profile="portfolia",
        account="",
        label="Review withdrawal",
        datasource_id=None,
        payload=payload,
        options={"cutoff": ""},
    )
    svc.evaluate(bid)
    task = next(t for t in G.tasks(cx) if t.kind == "zeile" and t.ref.startswith(f"{bid}:"))
    monkeypatch.setattr(G, "tasks", lambda context, hints=False: [task])
    shown = form_values(c.get("/quality/assistent?new=1").text)
    shown["a"] = ["row:include"]
    real = BA._execute_tx

    def before_commit(*args, **kwargs):
        if change == "candidate":
            _run_editor(cx, lambda: _add_partner(cx))
        elif change == "account":
            _run_editor(cx, lambda: svc.set_account("Sender", "Changed Sender"))
        elif change == "symbol":
            _run_editor(cx, lambda: svc.set_symbol("BTC", None))
        return real(*args, **kwargs)

    monkeypatch.setattr(BA, "_execute_tx", before_commit)
    response = post(c, "/quality/assistent/antwort", shown)
    monkeypatch.undo()
    assert response.status_code == 303
    booked = cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE batch_id=? AND status='active'", (bid,))
    assert booked == (1 if change == "none" else 0), {
        "change": change,
        "booked": booked,
        "redirect": response.headers["location"],
    }


@pytest.mark.parametrize("batch_candidates", [1, 2])
def test_takeover_offered_nopair_is_executable_for_batch_candidates(c, batch_candidates):
    cx, svc, bid, rc = _withdrawal(c, with_batch_partner=True)
    if batch_candidates == 2:
        recs = [r.rec for r in svc.rows(bid)]
        other = replace(
            recs[1],
            ext_id="second-batch-partner",
            line=4,
            ts=recs[1].ts + timedelta(minutes=5),
            row=copy.deepcopy(recs[1].row),
        )
        other.row["to_account"] = "Ledger Other"
        recs.append(other)
        payload = ("[" + ",".join(rec_to_json(r) for r in recs) + "]").encode()
        bid = svc.ingest(
            recs,
            source="csv:portfolia",
            profile="portfolia",
            account="",
            label="Batch candidates",
            datasource_id=None,
            payload=payload,
            options={"cutoff": ""},
        )
        svc.evaluate(bid)
        rc = next(r for r in svc.rows(bid) if r.row and r.row["type"] == "withdrawal")
    info = TK._choice_info(cx, rc, svc.rows(bid))
    assert "nopair" in info["options"] and not (info["partner"].get("revs") or {})
    assessment = SimpleNamespace(rid=1, decisions={rc.rec.ext_id: {**info, "choice": "nopair"}})
    approved, _skip, bad = TK._apply_decisions(cx, svc, bid, assessment)
    assert not bad and approved[rc.idx] == "nopair", {
        "batch_candidates": batch_candidates,
        "offer": info["partner"],
        "refused": bad,
    }
    now = next(r for r in svc.rows(bid) if r.idx == rc.idx)
    assert now.pair_ok == 0
