"""Independent checks of restore evidence and ownership across cache/action boundaries."""

# ruff: noqa: F401, F811
from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from app import fullexport as FE
from app.csvimport import batch as BA
from app.csvimport.service import csv_service
from app.journal.service import journal_service
from app.prices.models import Bar
from app.prices.sources import SourceService
from tests.test_diagnosis import PF_HEADER, upload
from tests.test_review_090 import _edit_target
from tests.test_review_092 import _run_editor, _two_rows
from tests.test_review_fixes import c, cfg, form_values, post


@pytest.mark.parametrize("mode", ["none", "warm", "cold"])
def test_settings_follow_the_sql_snapshot_and_do_not_poison_current_readers(c, mode):
    cx = c.app.state.ctx
    cx.settings.set("ledger.scope", "global")
    assert cx.settings.get("ledger.scope") == "global"
    with cx.db.snapshot():
        original = cx.db.scalar(
            "SELECT value_json FROM settings WHERE key='ledger.scope'"
        )
        if mode != "none":

            def writer():
                cx.settings.set("ledger.scope", "account")
                if mode == "warm":
                    assert cx.settings.get("ledger.scope") == "account"

            _run_editor(cx, writer)
        assert (
            cx.db.scalar("SELECT value_json FROM settings WHERE key='ledger.scope'")
            == original
        )
        assert cx.engine_options().scope == "global", {
            "mode": mode,
            "scope": cx.engine_options().scope,
        }
    expected = "global" if mode == "none" else "account"
    assert cx.engine_options().scope == expected, {
        "mode": mode,
        "scope": cx.engine_options().scope,
    }


@pytest.mark.parametrize(
    "a_status", ["valid", "rejected_before_undo", "rejected_after_undo"]
)
def test_undo_of_a_later_bulk_action_does_not_revive_a_rejected_input(c, a_status):
    cx, svc, bid, _mine, other = _two_rows(c)
    receipt = svc.set_rows(bid, {f"dec_{other.idx}": "defer"})["undo"]
    plan = BA.plan(svc, bid, "skip", {other.idx}, True, svc.rows(bid))
    result = BA.execute(
        svc,
        bid,
        "skip",
        {other.idx},
        token=BA.new_token(),
        fingerprint=plan.fingerprint(),
        force=True,
    )
    assert not result.get("errors"), result
    assert (
        cx.db.scalar("SELECT decision FROM csv_row WHERE id=?", (other.id,)) == "skip"
    )
    if a_status == "rejected_before_undo":
        svc.restore_inputs(bid, receipt)
    assert not BA.undo(svc, result["action_id"]).get("errors")
    if a_status == "rejected_after_undo":
        svc.restore_inputs(bid, receipt)
    expected = "defer" if a_status == "valid" else None
    assert (
        cx.db.scalar("SELECT decision FROM csv_row WHERE id=?", (other.id,)) == expected
    )


@pytest.mark.parametrize("identity", ["legacy_unique", "legacy_ambiguous", "exact"])
def test_restore_does_not_silently_discard_an_ambiguous_balance_observation(
    c, identity
):
    cx = c.app.state.ctx
    base = {
        "provider": "ethereum",
        "kind": "wallet",
        "account": "Wallet K",
        "name": "Same name",
        "enabled": 1,
        "sync_interval_min": 1440,
        "auto_commit": 0,
    }
    sources = [{**base, "address": "0x" + "1" * 40}]
    if identity != "legacy_unique":
        sources.append({**base, "address": "0x" + "2" * 40})
    observation = {
        "provider": "ethereum",
        "account": "Wallet K",
        "name": "Same name",
        "asset_key": "KAS",
        "qty": "12",
        "asset_name": "Kaspa",
        "observed_at": "2026-11-05T06:00:00Z",
    }
    if identity == "exact":
        observation.update(source_kind="wallet", source_address="0x" + "1" * 40)
    state = {"datasources": sources, "ds_balances": [observation]}
    iid = cx.active_import_id()
    counts = FE._apply_db(cx, iid, state, {}, "2026-11-05T07:00:00Z")
    assert cx.db.scalar("SELECT COUNT(*) FROM data_source") == len(sources)
    restore = cx.db.get_state(f"restore.{iid}")
    applied = cx.db.scalar("SELECT COUNT(*) FROM ds_balance")
    if identity == "legacy_ambiguous":
        assert applied == 1 or restore["state"] != "applied", {
            "observations_exported": 1,
            "observations_restored": applied,
            "restore_state": restore["state"],
            "counts": counts,
        }
    else:
        assert applied == 1 and restore["state"] == "applied"


@pytest.mark.parametrize("rejected_a", [False, True])
def test_http_bulk_undo_does_not_revive_a_failed_row_action(c, monkeypatch, rejected_a):
    cx, svc, bid, mine, other = _two_rows(c)
    fields = form_values(c.get(f"/journal/csv/{bid}").text, f"/journal/csv/{bid}/rows")
    fields[f"dec_{other.idx}"] = ["defer"]
    fields["row_action"] = [f"link:{mine.idx}"]
    real_tx, real_restore = BA._execute_tx, svc.restore_inputs
    changed, b_actions = [], []

    def bulk_skip():
        BA.set_selection(cx.db, bid, {other.idx})
        preview = c.get(f"/journal/csv/{bid}/preview?action=skip")
        form = form_values(preview.text, f"/journal/csv/{bid}/execute")
        response = post(c, f"/journal/csv/{bid}/execute", form)
        assert response.status_code == 303
        b_actions.append(
            cx.db.scalar("SELECT MAX(id) FROM import_action WHERE batch_id=?", (bid,))
        )

    def before_tx(*args, **kwargs):
        if rejected_a and not changed:
            changed.append(True)
            _run_editor(cx, lambda: _edit_target(cx, str(mine.match["target"]), "7"))
        return real_tx(*args, **kwargs)

    def before_restore(batch_id, receipt):
        _run_editor(cx, bulk_skip)
        return real_restore(batch_id, receipt)

    monkeypatch.setattr(BA, "_execute_tx", before_tx)
    monkeypatch.setattr(svc, "restore_inputs", before_restore)
    response = post(c, f"/journal/csv/{bid}/row-action", fields)
    monkeypatch.undo()
    if rejected_a:
        assert response.status_code == 200 and "geändert" in response.text
    else:
        assert response.status_code == 303
        bulk_skip()
    assert len(b_actions) == 1
    undone = c.post(
        f"/journal/csv/{bid}/actions/{b_actions[0]}/undo",
        data={"csrf_token": c.token},
        follow_redirects=False,
    )
    assert undone.status_code == 303
    assert cx.db.scalar("SELECT decision FROM csv_row WHERE id=?", (other.id,)) == (
        None if rejected_a else "defer"
    )


# N33 (Bewertung nach geänderter Kurszuordnung) ist eine Regelentscheidung, kein Defekt: siehe
# docs/BUCHUNGEN_PRUEFEN_KORREKTUREN.md (0.107.0) und tests/test_review_107.py::test_n33_*.
