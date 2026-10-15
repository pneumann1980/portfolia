"""Aufgaben und Hinweise trennen; Hinweise gesammelt als geprüft markieren (synthetische Daten, temporäre DB)."""

from __future__ import annotations

import os
import re
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Config, Secrets
from app.diagnosis import actions as A
from app.diagnosis import triage as TR
from app.diagnosis.engine import report_for
from app.importer.zipbuilder import build_zip
from app.jobs import tasks
from app.main import build_app
from tests.helpers import ASSETS, tx
from tests.test_diagnosis import COLS, make_ctx


@pytest.fixture
def cfg(tmp_path) -> Config:
    imp = tmp_path / "import"
    imp.mkdir(parents=True, exist_ok=True)
    return Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                  startup_jobs=False, log_format="text", secrets=Secrets())


HINT_ROWS = [tx("F1", "2022-01-10T10:00:00Z", "deposit", to=("Wallet Alt", "KAS", "5000"), value="500"),
             tx("F2", "2022-02-10T10:00:00Z", "deposit", to=("Wallet Alt 2", "KAS", "10"), value="1")]


def hint_ctx(cfg):
    return make_ctx(cfg, HINT_ROWS, ASSETS)


def journal_rows(ctx) -> list[tuple]:
    return [tuple(r) for r in ctx.db.q("SELECT * FROM journal_tx ORDER BY 1")]


def test_split_and_plan_are_read_only(cfg):
    ctx = hint_ctx(cfg)
    before = ctx.db.q("SELECT COUNT(*) AS n FROM diag_decision")[0]["n"]
    tasks, hints = TR.split(TR.open_findings(ctx))
    assert hints and all(f.status == "hinweis" for f in hints) and all(f.status != "hinweis" for f in tasks)
    plan = TR.plan_hints(ctx)
    assert {f.id for f in plan.items} == {f.id for f in hints} and plan.fp
    assert ctx.db.q("SELECT COUNT(*) AS n FROM diag_decision")[0]["n"] == before


def test_dismiss_needs_matching_fingerprint_and_undo_restores(cfg):
    ctx = hint_ctx(cfg)
    plan = TR.plan_hints(ctx)
    bad, token = TR.dismiss_hints(ctx, "falsch")
    assert not bad.ok and token == ""
    rows_before = journal_rows(ctx)
    res, token = TR.dismiss_hints(ctx, plan.fp, note="bewusst stillgelegt")
    assert res.ok and token and str(len(plan.items)) in res.message
    assert journal_rows(ctx) == rows_before  # keine Datenänderung
    assert not [f for f in TR.open_findings(ctx) if TR.is_hint(f)]
    assert TR.recent_bulks(ctx.db)[0]["token"] == token
    undo = TR.undo_bulk(ctx, token)
    assert undo.ok
    assert len([f for f in TR.open_findings(ctx) if TR.is_hint(f)]) == len(plan.items)
    assert not TR.undo_bulk(ctx, token).ok  # idempotent
    assert not TR.undo_bulk(ctx, "../x").ok


def test_changed_evidence_reopens_hint(cfg):
    ctx = hint_ctx(cfg)
    res, _ = TR.dismiss_hints(ctx, TR.plan_hints(ctx).fp)
    assert res.ok
    ctx.db.x("UPDATE diag_decision SET fingerprint='alt' WHERE status='active'")
    states = A.mark_states(report_for(ctx).findings, A.active_marks(ctx.db))
    assert states and all(not ok for _d, ok in states.values())


def test_page_hides_hints_by_default_and_bulk_flow(cfg):
    dst = cfg.import_dir / "kuratiert.zip"
    build_zip(dst, transactions=HINT_ROWS, assets=ASSETS, generated_at="2025-06-30T20:00:00Z",
              valuation_date="2025-06-30", extra_tx_columns=COLS)
    old = time.time() - 3600
    os.utime(dst, (old, old))
    with TestClient(build_app(cfg, start_scheduler=False)) as c:
        ctx = c.app.state.ctx
        assert tasks.import_check(ctx, "test").status == "imported"
        n_hints = len([f for f in TR.open_findings(ctx) if TR.is_hint(f)])
        assert n_hints >= 2
        page = c.get("/quality/diagnose").text
        assert "Inaktive Konten" in page and f"{n_hints} Hinweise" in page
        assert page.count('<details class="finding"') == 0  # Hinweise nicht in der Aufgabenliste
        assert c.get("/quality/diagnose?status=hinweis").text.count('<details class="finding"') == n_hints
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        prev = c.get("/quality/diagnose/hints")
        assert prev.status_code == 200 and "noch nichts geändert" in prev.text
        fp = re.search(r'name="fp" value="([^"]+)"', prev.text).group(1)
        rows_before = journal_rows(ctx)
        no = c.post("/quality/diagnose/hints/apply", data={"csrf_token": token, "fp": fp}, follow_redirects=False)
        assert no.status_code == 303 and not A.active_marks(ctx.db)  # ohne Bestätigung nichts
        ok = c.post("/quality/diagnose/hints/apply", data={"csrf_token": token, "fp": fp, "confirm": "1",
                                                           "note": "Test"}, follow_redirects=True)
        assert ok.status_code == 200 and "als geprüft markiert" in ok.text
        assert len(A.active_marks(ctx.db)) == n_hints and journal_rows(ctx) == rows_before
        tok = TR.recent_bulks(ctx.db)[0]["token"]
        assert "Rückgängig" in c.get("/quality/diagnose").text
        c.post(f"/quality/diagnose/hints/{tok}/undo", data={"csrf_token": token})
        assert not A.active_marks(ctx.db)
        stale = c.post("/quality/diagnose/hints/apply", data={"csrf_token": token, "fp": "x", "confirm": "1"},
                       follow_redirects=True)
        assert "geändert" in stale.text
