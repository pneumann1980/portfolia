"""Independent cutover checks: confirmation, cutoff ownership and atomic corrections."""

# ruff: noqa: F401,F811
from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

import pytest
from app.cutover import corrections as CR
from app.cutover import settle as ST
from app.diagnosis import guide as G
from app.diagnosis.engine import report_for
from tests.test_cutover_cases import _add_case
from tests.test_cutover_fix import _fix_all
from tests.test_cutover_settle import CUT, _holdings, _plan, _ref, cfg, cx
from tests.test_diagnosis import (
    TOKA_ASSETS,
    TOKA_QTY,
    client_with_import,
    popkat_like_rows,
)


@pytest.mark.parametrize("changed", [False, True])
def test_settlement_confirmation_binds_the_displayed_quantity(cx, changed):
    ctx = cx.app.state.ctx
    _ref(ctx, "Wallet", "ETH", "1.5", "2025-03-15")
    assert not _fix_all(ctx)[1]["errors"]
    row = next(r for r in _plan(ctx)["rows"] if r["asset"] == "ETH")
    form = {
        "csrf_token": cx.token,
        "cutoff": CUT.isoformat(),
        f"dec_{row['id']}": "settle",
    }
    preview = cx.post("/settings/stichtag/ausgleich", data=form)
    assert preview.status_code == 200
    if changed:
        ctx.db.x(
            "UPDATE reference_balance SET qty='0.5' WHERE account='Wallet' AND asset_id='ETH'"
        )
        ctx.invalidate_overlay()
    response = cx.post("/settings/stichtag/ausgleich", data={**form, "do": "book"})
    count = ctx.db.scalar("SELECT COUNT(*) FROM cutover_correction")
    actual = _holdings(ctx, "Wallet", "ETH")
    expected = Decimal(2) if changed else Decimal("1.5")
    assert actual == expected, {
        "changed": changed,
        "http": response.status_code,
        "balance": str(actual),
        "corrections": count,
    }
    assert count == (0 if changed else 1)


@pytest.mark.parametrize("changed", [False, True])
def test_assistant_settlement_binds_the_displayed_quantity(cx, changed):
    ctx = cx.app.state.ctx
    _ref(ctx, "Wallet", "ETH", "1.5", "2025-03-15")
    assert not _fix_all(ctx)[1]["errors"]
    fid = next(
        f.id
        for f in report_for(ctx).findings
        if ("Wallet", "ETH") in {tuple(p) for p in f.positions}
    )
    _add_case(ctx, "kg-t-review106", [fid])
    task = G.detail(
        ctx, next(t for t in G.tasks(ctx) if t.key == "fall:kg-t-review106")
    )
    assert "case:settle" in [a.id for a in task.answers]
    if changed:
        ctx.db.x(
            "UPDATE reference_balance SET qty='0.5' WHERE account='Wallet' AND asset_id='ETH'"
        )
        ctx.invalidate_overlay()
    response = cx.post(
        "/quality/assistent/antwort",
        data={
            "csrf_token": cx.token,
            "kid": task.kid,
            "a": "case:settle",
            "typ": "seite",
            "before": "5",
        },
        follow_redirects=False,
    )
    actual = _holdings(ctx, "Wallet", "ETH")
    expected = Decimal(2) if changed else Decimal("1.5")
    assert actual == expected, {
        "changed": changed,
        "http": response.status_code,
        "balance": str(actual),
        "location": response.headers.get("location"),
    }


@pytest.mark.parametrize("changed", [False, True])
def test_assistant_case_with_stable_findings_binds_the_displayed_evidence(cfg, changed):
    rows = popkat_like_rows()
    for row in rows:
        if row["tx_id"] in ("F1", "B1"):
            row["datetime"] = row["datetime"].replace("2025-", "2024-", 1)
    client = client_with_import(cfg, rows, TOKA_ASSETS, valuation="2025-06-30")
    try:
        ctx = client.app.state.ctx
        original = _holdings(ctx, "Wallet K", "TOKA")
        _ref(ctx, "Wallet K", "TOKA", str(original + 1), "2025-06-30")
        assert not _fix_all(ctx)[1]["errors"]
        fid = next(
            f.id
            for f in report_for(ctx).findings
            if f.kind == "duplicate" and f.parent is None
        )
        _add_case(ctx, "kg-t-stable-review106", [fid])
        task = G.detail(
            ctx, next(t for t in G.tasks(ctx) if t.key == "fall:kg-t-stable-review106")
        )
        assert "case:settle" in [a.id for a in task.answers]
        if changed:
            ctx.db.x(
                "UPDATE reference_balance SET qty=? WHERE account='Wallet K' AND asset_id='TOKA'",
                (str(original + 100),),
            )
            ctx.invalidate_overlay()
            assert (
                report_for(ctx).by_id(fid) is not None
            )  # the underlying duplicate remains the same
        response = client.post(
            "/quality/assistent/antwort",
            data={
                "csrf_token": client.token,
                "kid": task.kid,
                "a": "case:settle",
                "typ": "seite",
                "before": "5",
            },
            follow_redirects=False,
        )
        actual = _holdings(ctx, "Wallet K", "TOKA")
        assert actual == (original if changed else original + 1), {
            "changed": changed,
            "http": response.status_code,
            "approved_addition": "1",
            "actual_addition": str(actual - original),
        }
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("concurrent", [False, True])
def test_confirming_the_same_settlement_twice_books_once(cx, monkeypatch, concurrent):
    ctx = cx.app.state.ctx
    _ref(ctx, "Wallet", "ETH", "1.5", "2025-03-15")
    assert not _fix_all(ctx)[1]["errors"]
    row = next(r for r in _plan(ctx)["rows"] if r["asset"] == "ETH")
    if concurrent:
        barrier = threading.Barrier(2)
        original = ST._book

        def synchronized(*args, **kwargs):
            barrier.wait(timeout=20)
            return original(*args, **kwargs)

        monkeypatch.setattr(ST, "_book", synchronized)
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = [pool.submit(ST.apply, ctx, CUT, {row["id"]}) for _ in range(2)]
            results = [f.result(timeout=40) for f in calls]
    else:
        results = [ST.apply(ctx, CUT, {row["id"]}) for _ in range(2)]
    actual = _holdings(ctx, "Wallet", "ETH")
    assert actual == Decimal("1.5"), {
        "concurrent": concurrent,
        "balance": str(actual),
        "done": [len(r.get("done", [])) for r in results],
    }
    assert (
        ctx.db.scalar("SELECT COUNT(*) FROM cutover_correction WHERE status='active'")
        == 1
    )


@pytest.mark.parametrize("operation", ["apply", "undo"])
@pytest.mark.parametrize("fail_audit", [False, True])
def test_correction_and_its_audit_record_are_atomic(cx, operation, fail_audit):
    ctx = cx.app.state.ctx
    assert not _fix_all(ctx)[1]["errors"]
    kw = {
        "account": "Wallet",
        "asset": "ETH",
        "kind": "add",
        "qty": "1",
        "mode": "cautious",
        "evidence": "Synthetic review",
    }
    correction = CR.apply(ctx, CUT, **kw) if operation == "undo" else None
    before = _holdings(ctx, "Wallet", "ETH")
    before_count = ctx.db.scalar(
        "SELECT COUNT(*) FROM journal_tx WHERE status='active'"
    )
    if fail_audit:
        action = "INSERT" if operation == "apply" else "UPDATE"
        ctx.db.x(
            f"CREATE TRIGGER review_fail_audit BEFORE {action} ON cutover_correction "
            "BEGIN SELECT RAISE(ABORT, 'synthetic audit write failure'); END"
        )
    error = None
    try:
        out = (
            CR.apply(ctx, CUT, **kw)
            if operation == "apply"
            else CR.undo(ctx, correction["id"])
        )
        if not fail_audit:
            assert not out["errors"], out
    except sqlite3.IntegrityError as exc:
        error = str(exc)
    after = _holdings(ctx, "Wallet", "ETH")
    expected = (
        before
        if fail_audit
        else before + (Decimal(1) if operation == "apply" else -Decimal(1))
    )
    assert after == expected, {
        "operation": operation,
        "fail_audit": fail_audit,
        "before": str(before),
        "after": str(after),
        "error": error,
        "before_transactions": before_count,
        "after_transactions": ctx.db.scalar(
            "SELECT COUNT(*) FROM journal_tx WHERE status='active'"
        ),
    }


@pytest.mark.parametrize("stale_cutoff", [False, True])
def test_a_correction_form_cannot_write_to_a_different_fixed_cutoff(cx, stale_cutoff):
    ctx = cx.app.state.ctx
    assert not _fix_all(ctx)[1]["errors"]
    cutoff = date(2024, 1, 1) if stale_cutoff else CUT
    response = cx.post(
        "/settings/stichtag/korrektur",
        data={
            "csrf_token": cx.token,
            "cutoff": cutoff.isoformat(),
            "account": "Wallet",
            "asset": "ETH",
            "kind": "add",
            "qty": "1",
            "mode": "cautious",
            "evidence": "Synthetic review",
            "do": "book",
        },
        follow_redirects=False,
    )
    actual = _holdings(ctx, "Wallet", "ETH")
    assert actual == (Decimal(2) if stale_cutoff else Decimal(3)), {
        "stale_cutoff": stale_cutoff,
        "http": response.status_code,
        "balance": str(actual),
        "booked_cutoffs": [
            r["cutoff"] for r in ctx.db.q("SELECT cutoff FROM cutover_correction")
        ],
    }
