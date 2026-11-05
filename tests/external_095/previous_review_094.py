"""Independent adversarial review of Portfolia 0.94.0, desired behavior.

Only scheduling is patched. Forms are rendered by the application and writes
use the real services. Application code is not modified.
"""
# ruff: noqa: F811, F401, S101
from __future__ import annotations

import pytest
from lxml import html

from app.csvimport import batch as BA
from app.csvimport.service import csv_service
from app.journal.service import journal_service
from tests.test_bitpanda import ctx
from tests.test_diagnosis import PF_HEADER, upload
from tests.test_nachpruefung import _all_state
from tests.test_review_090 import _edit_target
from tests.test_review_092 import _run_editor, _two_rows
from tests.test_review_fixes import c, cfg, form_values, post


def _withdrawal(c, with_batch_partner=False):
    cx = ctx(c)
    saved = journal_service(cx).save({
        "kind": "expert", "date": "2025-07-01", "time": "10:00", "type": "deposit",
        "to_account": "Sender", "to_asset": "BTC", "to_qty": "0.01", "value_eur": "550",
    })
    assert not saved.errors
    data = PF_HEADER + "OUT,2025-07-12T12:00:00Z,withdrawal,,Sender,BTC,0.001,,,,,,,55,out\n"
    if with_batch_partner:
        data += "IN,2025-07-12T12:30:00Z,deposit,,,,,Ledger,BTC,0.001,,,,55,in\n"
    bid = upload(c, "out.csv", data)
    svc = csv_service(cx)
    svc.evaluate(bid)
    out = next(r for r in svc.rows(bid) if r.row and r.row["type"] == "withdrawal")
    assert not out.errors
    if with_batch_partner:
        assert out.pair_ref and out.pair_ref.startswith("b:")
    else:
        assert not out.pair_ref and not out.pair_alts and not out.counterpart
    BA.set_selection(cx.db, bid, {out.idx})
    return cx, svc, bid, out


def _add_partner(cx):
    saved = journal_service(cx).save({
        "kind": "expert", "date": "2025-07-12", "time": "14:30", "type": "deposit",
        "to_account": "Ledger", "to_asset": "BTC", "to_qty": "0.001", "value_eur": "55",
    })
    assert not saved.errors
    return saved.tx_ids[0]


@pytest.mark.parametrize("when", ["none", "before_preview", "before_commit"])
def test_first_transfer_candidate_is_bound_through_commit(c, monkeypatch, when):
    cx, svc, bid, rc = _withdrawal(c)
    if when == "before_preview":
        _add_partner(cx)
        svc.evaluate(bid)
        now = next(r for r in svc.rows(bid) if r.idx == rc.idx)
        assert now.pair_ref and now.pair_ref.startswith("j:")
        assert not BA.plan(svc, bid, "include", {rc.idx}, True).todo
        return
    before = _all_state(c)
    shown = form_values(c.get(f"/journal/csv/{bid}/preview?action=include&force=1").text,
                        f"/journal/csv/{bid}/execute")
    added = []
    if when == "before_commit":
        real_tx = BA._execute_tx

        def before_commit(*args, **kwargs):
            _run_editor(cx, lambda: added.append(_add_partner(cx)))
            return real_tx(*args, **kwargs)

        monkeypatch.setattr(BA, "_execute_tx", before_commit)
    res = post(c, f"/journal/csv/{bid}/execute", shown)
    monkeypatch.undo()
    if when == "none":
        assert res.status_code == 303
        aid = cx.db.scalar("SELECT id FROM import_action WHERE batch_id=?", (bid,))
        assert post(c, f"/journal/csv/{bid}/actions/{aid}/undo", {}).status_code == 303
        cx.invalidate_data()
        assert _all_state(c) == before
        return
    assert added and cx.db.scalar("SELECT status FROM journal_tx WHERE tx_id=?", (added[0],)) == "active"
    assert res.status_code == 409, {
        "http": res.status_code,
        "booked": cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE batch_id=? AND status='active'", (bid,)),
        "redirect": res.headers.get("location"),
    }
    assert not cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE batch_id=? AND status='active'", (bid,))


def _transfer_form(c, bid, idx):
    root = html.fromstring(c.get(f"/journal/csv/{bid}").text)
    form = root.xpath(f'//form[.//select[@name="pair_{idx}"]]')[0]
    out = {e.get("name"): [e.get("value") or ""] for e in form.xpath('.//input[@name]')}
    return out


@pytest.mark.parametrize("intent", ["untouched", "conflict", "fresh"])
def test_stale_batch_transfer_form_preserves_both_sides(c, intent):
    cx, svc, bid, rc = _withdrawal(c, with_batch_partner=True)
    partner_idx = int(rc.pair_ref[2:])
    stale = _transfer_form(c, bid, rc.idx)
    assert f"iv_{rc.idx}" in stale
    fresh = dict(stale)
    fresh[f"pair_{rc.idx}"] = ["0"]
    _run_editor(cx, lambda: post(c, f"/journal/csv/{bid}/rows", fresh))
    before = {r.idx: r.pair_ok for r in svc.rows(bid)}
    assert before[rc.idx] == before[partner_idx] == 0
    if intent == "fresh":
        fresh = _transfer_form(c, bid, rc.idx)
        fresh[f"pair_{rc.idx}"] = ["1"]
        assert post(c, f"/journal/csv/{bid}/rows", fresh).status_code == 303
        assert {r.idx: r.pair_ok for r in svc.rows(bid)} == {rc.idx: 1, partner_idx: 1}
        return
    stale[f"pair_{rc.idx}"] = ["" if intent == "untouched" else "1"]
    res = post(c, f"/journal/csv/{bid}/rows", stale)
    after = {r.idx: r.pair_ok for r in svc.rows(bid)}
    if intent == "conflict":
        assert res.status_code == 200 and "inzwischen an anderer Stelle geändert" in res.text
    assert after == before, {"before": before, "after": after, "http": res.status_code,
                              "partner_has_mark": f"iv_{partner_idx}" in stale}


@pytest.mark.parametrize("marked", [False, True])
def test_identical_explicit_later_write_keeps_its_value(c, marked):
    cx, svc, bid, _mine, other = _two_rows(c)
    # Both tabs saw the same original form, before the first request writes 50.
    shown = form_values(c.get(f"/journal/csv/{bid}").text, f"/journal/csv/{bid}/rows")
    first = svc.set_rows(bid, {f"val_{other.idx}": "50"})
    later = {f"val_{other.idx}": "50"}
    if marked:
        later[f"iv_{other.idx}"] = shown[f"iv_{other.idx}"][0]
    results = []
    _run_editor(cx, lambda: results.append(svc.set_rows(bid, later)))
    assert not results[0]["errors"]
    svc.restore_inputs(bid, first["undo"])
    assert cx.db.scalar("SELECT value_in FROM csv_row WHERE id=?", (other.id,)) == "50"


@pytest.mark.parametrize("new_candidate", [False, True])
def test_legacy_commit_also_binds_absence_of_transfer_candidates(c, monkeypatch, new_candidate):
    cx, svc, bid, _rc = _withdrawal(c)
    added = []
    if new_candidate:
        real_plan = svc.commit_plan

        def plan_then_add(*args, **kwargs):
            result = real_plan(*args, **kwargs)
            _run_editor(cx, lambda: added.append(_add_partner(cx)))
            return result

        monkeypatch.setattr(svc, "commit_plan", plan_then_add)
    result = svc.commit(bid)
    monkeypatch.undo()
    if not new_candidate:
        assert not result["errors"]
        assert cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE batch_id=? AND status='active'", (bid,)) == 1
        return
    assert added
    booked = cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE batch_id=? AND status='active'", (bid,))
    assert result["errors"], {"result": result, "booked": booked}
    assert not cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE batch_id=? AND status='active'", (bid,))


@pytest.mark.parametrize("edit_during_render", [False, True])
def test_rendered_values_and_input_marks_describe_the_same_state(c, monkeypatch, edit_during_render):
    cx, svc, bid, _mine, other = _two_rows(c)
    actual_marks = svc.input_marks
    changed = []

    def marks_after_edit(batch_id):
        if edit_during_render and not changed:
            _run_editor(cx, lambda: svc.set_rows(bid, {f"val_{other.idx}": "123"}))
            changed.append(1)
        return actual_marks(batch_id)

    monkeypatch.setattr(svc, "input_marks", marks_after_edit)
    page = c.get(f"/journal/csv/{bid}").text
    monkeypatch.undo()
    fields = form_values(page, f"/journal/csv/{bid}/rows")
    assert f"iv_{other.idx}" in fields and f"val_{other.idx}" in fields
    shown_value = fields[f"val_{other.idx}"][0]
    assert cx.db.scalar("SELECT value_in FROM csv_row WHERE id=?", (other.id,)) == (
        "123" if edit_during_render else None
    )
    response = post(c, f"/journal/csv/{bid}/rows", fields)  # user did not change this field
    current = cx.db.scalar("SELECT value_in FROM csv_row WHERE id=?", (other.id,))
    assert current == ("123" if edit_during_render else None), {
        "shown_value": shown_value, "saved_value": current, "http": response.status_code,
        "marks": fields[f"iv_{other.idx}"],
    }


@pytest.mark.parametrize("later_choice", ["include", "skip"])
def test_current_bulk_form_owns_its_explicit_decision(c, monkeypatch, later_choice):
    cx, svc, bid, mine, _other = _two_rows(c)
    assert mine.status == "duplicate"
    page = html.fromstring(c.get(f"/journal/csv/{bid}?status=duplicate").text)
    value = f"duplicate:{later_choice}"
    form = page.xpath(f'//form[.//button[@name="bulk"][@value="{value}"]]')[0]
    fields = {e.get("name"): [e.get("value") or ""] for e in form.xpath('.//input[@name]')}
    fields["bulk"] = [value]
    first = form_values(c.get(f"/journal/csv/{bid}").text, f"/journal/csv/{bid}/rows")
    first[f"dec_{mine.idx}"] = ["skip"]
    first["row_action"] = [f"link:{mine.idx}"]
    real_tx, real_restore = BA._execute_tx, svc.restore_inputs
    responses = []

    def edit_then_tx(*args, **kwargs):
        _run_editor(cx, lambda: _edit_target(cx, str(mine.match["target"]), "7"))
        return real_tx(*args, **kwargs)

    def bulk_then_restore(batch_id, snap):
        assert any(rid == mine.id and group == "dec" for rid, group, _before, _token in snap)
        _run_editor(cx, lambda: responses.append(post(c, f"/journal/csv/{bid}/rows", fields)))
        assert responses[0].status_code == 303
        assert cx.db.scalar("SELECT decision FROM csv_row WHERE id=?", (mine.id,)) == later_choice
        return real_restore(batch_id, snap)

    monkeypatch.setattr(BA, "_execute_tx", edit_then_tx)
    monkeypatch.setattr(svc, "restore_inputs", bulk_then_restore)
    rejected = post(c, f"/journal/csv/{bid}/row-action", first)
    monkeypatch.undo()
    assert rejected.status_code == 200 and "geändert" in rejected.text
    assert not cx.db.scalar("SELECT COUNT(*) FROM tx_link WHERE batch_id=?", (bid,))
    assert cx.db.scalar("SELECT decision FROM csv_row WHERE id=?", (mine.id,)) == later_choice, {
        "later_choice": later_choice, "has_mark": f"iv_{mine.idx}" in fields,
    }
