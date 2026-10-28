"""Erneut prüfen: Herkunft einer übernommenen Buchung, Vormerken, Zurücklegen in den Prüfstapel, Rückholen."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.config import Config, Secrets
from app.csvimport.service import csv_service
from app.diagnosis import recheck as RC
from app.diagnosis import workplan as WP
from tests.helpers import ASSETS, tx
from tests.test_diagnosis import PF_HEADER, client_with_import, rows_of, upload

D = Decimal


@pytest.fixture
def cfg(tmp_path) -> Config:
    imp = tmp_path / "import"
    imp.mkdir(parents=True, exist_ok=True)
    return Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                  startup_jobs=False, log_format="text", secrets=Secrets())


@pytest.fixture
def setup(cfg, monkeypatch):
    c = client_with_import(cfg, [tx("B1", "2025-01-02", "deposit", to=("Depot D", "EUR", "10"), value="10")], ASSETS)
    ctx = c.app.state.ctx
    data = PF_HEADER + "N1,2025-03-10T12:00:00Z,deposit,,,,,Depot D,EUR,25,,,,25,manuell\n"
    bid = upload(c, "n.csv", data)
    assert csv_service(ctx).commit(bid)["errors"] == []
    (row,) = ctx.db.q("SELECT tx_id FROM journal_tx WHERE batch_id=? AND status='active'", (bid,))
    try:
        yield c, ctx, bid, row["tx_id"], monkeypatch
    finally:
        c.__exit__(None, None, None)


def eur(ctx):
    return ctx.ledger().holdings_by_account("Depot D").get("EUR", D(0))


def test_origin_is_shown_but_actions_need_a_finding(setup):
    _c, ctx, bid, tid, _mp = setup
    i = RC.info(ctx, tid)
    assert i and i["batch_id"] == bid and i["can_reopen"] and not i["marked"] and f"Prüfstapel {bid}" in i["text"]
    assert RC.info(ctx, "B1") is None  # Import-Buchung: keine App-Buchung aus dem Prüfstapel
    for fn in (RC.mark, RC.reopen):
        assert fn(ctx, tid)["errors"] == [RC.NOT_NAMED]
    assert RC.marks(ctx.db) == {} and eur(ctx) == D(35)


def test_mark_shows_up_in_the_plan_and_can_be_removed(setup):
    c, ctx, _bid, tid, mp = setup
    mp.setattr(RC, "named_by_finding", lambda ctx_, t: "Testbefund")
    assert RC.mark(ctx, tid)["errors"] == []
    assert RC.info(ctx, tid)["marked"] and eur(ctx) == D(35)  # Buchung unverändert
    plan = WP.build(ctx)
    item = next(i for i in plan.items if i.key == "erneut")
    assert item.tier == "B" and item.n == 1 and item.href == "/quality/erneut"
    page = c.get("/quality/erneut").text
    assert tid in page and "Testbefund" in page and "Zurück in den Prüfstapel" in page
    assert RC.unmark(ctx, tid)["errors"] == [] and RC.unmark(ctx, tid)["errors"]
    assert not any(i.key == "erneut" for i in WP.build(ctx).items)


def test_reopen_puts_the_row_back_and_undo_restores_it(setup):
    c, ctx, bid, tid, mp = setup
    mp.setattr(RC, "named_by_finding", lambda ctx_, t: "Testbefund")
    RC.mark(ctx, tid)
    out = RC.reopen(ctx, tid)
    assert out["errors"] == [] and out["batch_id"] == bid
    assert ctx.db.q1("SELECT status FROM journal_tx WHERE tx_id=?", (tid,))["status"] == "reverted"
    (r,) = rows_of(c, bid)
    assert r.status in ("new", "duplicate") and r.decision is None and r.tx_id is None and r.open
    assert eur(ctx) == D(10) and RC.marks(ctx.db) == {}  # Markierung erledigt
    # Rückholen
    eid = RC.entries(ctx.db)[-1]["id"]
    assert RC.undo(ctx, eid)["errors"] == []
    assert eur(ctx) == D(35) and ctx.db.q1("SELECT status FROM journal_tx WHERE tx_id=?", (tid,))["status"] == "active"
    (r,) = rows_of(c, bid)
    assert r.tx_id == tid and not r.open
    assert RC.undo(ctx, eid)["errors"]  # nur einmal


def test_undo_refuses_when_the_row_was_decided_again(setup):
    _c, ctx, bid, tid, mp = setup
    mp.setattr(RC, "named_by_finding", lambda ctx_, t: "Testbefund")
    assert RC.reopen(ctx, tid)["errors"] == []
    assert csv_service(ctx).commit(bid)["errors"] == []  # Zeile wird neu übernommen
    assert eur(ctx) == D(35)
    res = RC.undo(ctx, RC.entries(ctx.db)[-1]["id"])
    assert res["errors"] and "doppelt" in res["errors"][0] and eur(ctx) == D(35)


def test_routes_check_csrf_and_redirect_only_inside_the_data_quality(setup):
    c, _ctx, _bid, tid, mp = setup
    mp.setattr(RC, "named_by_finding", lambda ctx_, t: "Testbefund")
    r = c.post("/quality/erneut/vormerken", data={"tx": tid}, follow_redirects=False)
    assert r.status_code == 403
    r = c.post("/quality/erneut/vormerken", data={"csrf_token": c.token, "tx": tid, "back": "https://evil.example/x"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/quality/erneut?msg=")
    r = c.post("/quality/erneut/entfernen", data={"csrf_token": c.token, "tx": tid, "back": "/quality/diagnose"},
               follow_redirects=False)
    assert r.headers["location"].startswith("/quality/diagnose?msg=")
    r = c.post("/quality/erneut/zurueck", data={"csrf_token": c.token, "tx": "PF-gibt-es-nicht"},
               follow_redirects=False)
    assert "err=" in r.headers["location"]
