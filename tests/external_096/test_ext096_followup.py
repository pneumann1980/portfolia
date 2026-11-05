"""Independent additional checks of 0.95.0 snapshot/cache and compensating-action boundaries."""

# ruff: noqa: F401, F811, S101
from __future__ import annotations

from decimal import Decimal

import pytest

from app.accounts import service as ACC
from app.assetchange import account_merge as AM
from app.csvimport import batch as BA
from app.csvimport import compare as CMP
from app.csvimport.service import csv_service, ref_revs
from tests.test_bitpanda import ctx
from tests.test_review_090 import _edit_target
from tests.test_review_091 import _duplicate, _journal_transfer
from tests.test_review_092 import _run_editor, _two_rows
from tests.test_review_fixes import c, cfg, form_values, post


def _value(cx, target):
    return next(t.value_eur for t in cx.recorded_portfolio().txs if t.tx_id == target)


@pytest.mark.parametrize("cache", ["warm", "cold"])
def test_snapshot_cache_cannot_mix_old_database_and_new_portfolio(c, cache):
    cx, _svc, _bid, rc = _duplicate(c)
    target = str(rc.match["target"])
    assert _value(cx, target) == Decimal(1)
    with cx.db.snapshot():
        original_revision = ref_revs(cx.db.conn, [target])[target]

        def writer():
            _edit_target(cx, target, "777")
            if cache == "warm":
                assert _value(cx, target) == Decimal(777)
            else:
                assert not ACC.update(cx, "Wallet K", "", "", "Account edited after the journal update")

        _run_editor(cx, writer)
        assert ref_revs(cx.db.conn, [target])[target] == original_revision
        assert _value(cx, target) == Decimal(1), {"cache": cache, "value": str(_value(cx, target))}
    assert _value(cx, target) == Decimal(777), {"cache": cache, "value": str(_value(cx, target))}


@pytest.mark.parametrize("race", [False, True])
def test_next_render_must_not_link_using_a_poisoned_snapshot_cache(c, monkeypatch, race):
    cx, _svc, bid, rc = _duplicate(c)
    target = str(rc.match["target"])
    real = CMP.build
    changed = []

    def compare_after_foreign_write(*args, **kwargs):
        if race and not changed:

            def writer():
                _edit_target(cx, target, "777")
                assert not ACC.update(cx, "Wallet K", "", "", "Account edited after the journal update")

            _run_editor(cx, writer)
            changed.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(CMP, "build", compare_after_foreign_write)
    c.get(f"/journal/csv/{bid}")
    monkeypatch.undo()
    captured = []

    def capture(*args, **kwargs):
        result = real(*args, **kwargs)
        captured.append(result)
        return result

    monkeypatch.setattr(CMP, "build", capture)
    shown = form_values(c.get(f"/journal/csv/{bid}").text, f"/journal/csv/{bid}/rows")
    monkeypatch.undo()
    side = next(s for result in captured for comp in result.values() for s in comp.old if s.label == target)
    expected = Decimal(777) if race else Decimal(1)
    shown["row_action"] = [f"link:{rc.idx}"]
    response = post(c, f"/journal/csv/{bid}/row-action", shown)
    linked = cx.db.scalar("SELECT COUNT(*) FROM tx_link WHERE batch_id=? AND status='active'", (bid,))
    assert side.value_eur == expected or not linked, {
        "displayed_target_value": str(side.value_eur),
        "current_target_value": str(expected),
        "http": response.status_code,
        "active_links": linked,
    }


@pytest.mark.parametrize("second", [False, True])
def test_two_rejected_actions_leave_no_unowned_input(c, monkeypatch, second):
    cx, svc, bid, mine, other = _two_rows(c)
    target = str(mine.match["target"])
    first = form_values(c.get(f"/journal/csv/{bid}").text, f"/journal/csv/{bid}/rows")
    first[f"val_{other.idx}"] = ["50"]
    first["row_action"] = [f"link:{mine.idx}"]
    real_tx, real_restore = BA._execute_tx, svc.restore_inputs
    depth = []
    replies = []

    def change_then_tx(*args, **kwargs):
        _run_editor(cx, lambda: _edit_target(cx, target, "9" if depth else "7"))
        return real_tx(*args, **kwargs)

    def second_then_restore(batch_id, snapshot):
        if second and not depth:
            depth.append(1)
            fresh = form_values(c.get(f"/journal/csv/{bid}").text, f"/journal/csv/{bid}/rows")
            fresh[f"val_{other.idx}"] = ["99"]
            fresh["row_action"] = [f"link:{mine.idx}"]
            _run_editor(cx, lambda: replies.append(post(c, f"/journal/csv/{bid}/row-action", fresh)))
        return real_restore(batch_id, snapshot)

    monkeypatch.setattr(BA, "_execute_tx", change_then_tx)
    monkeypatch.setattr(svc, "restore_inputs", second_then_restore)
    response = post(c, f"/journal/csv/{bid}/row-action", first)
    monkeypatch.undo()
    assert response.status_code == 200 and "geändert" in response.text
    assert all(r.status_code == 200 and "geändert" in r.text for r in replies)
    assert not cx.db.scalar("SELECT COUNT(*) FROM tx_link WHERE batch_id=? AND status='active'", (bid,))
    assert cx.db.scalar("SELECT value_in FROM csv_row WHERE id=?", (other.id,)) is None


@pytest.mark.parametrize("merge", [False, True])
def test_account_merge_is_a_dependency_until_the_booking_transaction(c, monkeypatch, merge):
    cx, _svc, bid, _rc, _target = _journal_transfer(c, "1")
    shown = form_values(c.get(f"/journal/csv/{bid}/preview?action=include&force=1").text, f"/journal/csv/{bid}/execute")
    merged = []
    real = BA._execute_tx

    def before_commit(*args, **kwargs):
        if merge:

            def editor():
                p = AM.preview(cx, "Sender", "Wallet K")
                assert p.ok, p.errors
                mid, errors = AM.apply(cx, "Sender", "Wallet K", p.fp)
                assert mid and not errors, errors
                merged.append(mid)

            _run_editor(cx, editor)
        return real(*args, **kwargs)

    monkeypatch.setattr(BA, "_execute_tx", before_commit)
    response = post(c, f"/journal/csv/{bid}/execute", shown)
    monkeypatch.undo()
    created = cx.db.scalar("SELECT COUNT(*) FROM import_action WHERE batch_id=?", (bid,))
    if merge:
        assert merged
        transfers = [
            (t.from_account, t.to_account)
            for t in cx.recorded_portfolio().txs
            if t.type == "transfer" and t.to_account == "Ledger"
        ]
        assert response.status_code == 409 and created == 0, {
            "http": response.status_code,
            "actions": created,
            "effective_transfers": transfers,
        }
    else:
        assert response.status_code == 303 and created == 1
