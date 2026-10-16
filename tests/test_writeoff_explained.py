"""Ausgebuchte Menge (Totalverlust, Spam, Burn), die die externe Quelle weiterhin zeigt (0.30.0).

Die Wallet führt Spam-/Rugpull-Token weiter auf, obwohl sie ausgebucht sind. Der berechnete Bestand ist dann 0, der
beobachtete die ausgebuchte Menge – das ist erwartet und keine Aufgabe. Jede andere Abweichung bleibt eine Aufgabe.
Synthetische Daten, temporäre Datenbanken.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.diagnosis.engine import report_for
from app.journal.service import journal_service
from tests.helpers import ASSETS, tx
from tests.test_diagnosis import NOW, add_wallet, asset, cfg, make_ctx  # noqa: F401

ACC = "MetaMask (AVAX)"


def _setup(cfg, observed: str, *, tag: str = "lost", qty: str = "32.258636955", day: str = "2026-09-30"):  # noqa: F811
    rows = [tx("B1", "2025-05-16T13:20:00Z", "deposit", to=(ACC, "BEAR", "32.258636955"), value="0")]
    ctx = make_ctx(cfg, rows, [*ASSETS, asset("BEAR")])
    res = journal_service(ctx).save({"kind": "expert", "time": "21:59", "type": "withdrawal", "tag": tag, "date": day,
                                     "from_account": ACC, "from_asset": "BEAR", "from_qty": qty,
                                     "note": "Ausbuchung (Totalverlust)"})
    assert not res.errors, res.errors
    add_wallet(ctx, ACC, {"BEAR": observed}, observed=NOW - timedelta(hours=1), complete=True, sid=1)
    ctx.invalidate_data()
    rep = report_for(ctx)
    row = next(h for h in rep.holdings if h.asset == "BEAR")
    return ctx, rep, row


def _holding_findings(rep):
    return [f for f in rep.findings if f.kind == "holdings"]


def test_written_off_amount_still_shown_by_wallet_is_explained_not_a_task(cfg):  # noqa: F811
    _ctx, rep, row = _setup(cfg, "32.258636955014")  # Wallet führt 14 Stellen, ausgebucht sind 9
    assert row.status == "extern_ausgebucht" and row.writeoff == Decimal("32.258636955")
    assert row.computed == 0 and row.observed == Decimal("32.258636955014")
    assert any("ausgebucht" in e and "erwartet" in e for e in row.explanations) and row.tx_ids
    assert not _holding_findings(rep)  # keine Aufgabe, keine Ausgleichsbuchung vorgeschlagen
    assert rep.stats["holdings_extern_ausgebucht"] == 1 and rep.stats["holdings_extern_diff"] == 0


@pytest.mark.parametrize("tag", ["burn", "stolen"])
def test_burn_and_stolen_tags_count_as_write_off(cfg, tag):  # noqa: F811
    _ctx, rep, row = _setup(cfg, "32.258636955", tag=tag)
    assert row.status == "extern_ausgebucht"
    assert not _holding_findings(rep)


@pytest.mark.parametrize("observed", ["40", "10"])
def test_other_differences_stay_tasks(cfg, observed):  # noqa: F811
    # mehr als ausgebucht (neue Airdrops) bzw. weniger (teilweise bewegt): nie still erklärt
    _ctx, rep, row = _setup(cfg, observed)
    assert row.status == "extern_diff" and len(_holding_findings(rep)) == 1


def test_ordinary_withdrawal_does_not_explain_a_difference(cfg):  # noqa: F811
    _ctx, rep, row = _setup(cfg, "32.258636955", tag="cost")  # Bezahlung, kein Verlust
    assert row.status == "extern_diff" and len(_holding_findings(rep)) == 1


def test_write_off_after_the_observation_does_not_count(cfg):  # noqa: F811
    rows = [tx("B1", "2025-05-16T13:20:00Z", "deposit", to=(ACC, "BEAR", "10"), value="0")]
    ctx = make_ctx(cfg, rows, [*ASSETS, asset("BEAR")])
    add_wallet(ctx, ACC, {"BEAR": "10"}, observed=NOW - timedelta(days=1, hours=1), complete=True, sid=1)
    res = journal_service(ctx).save({"kind": "expert", "time": "12:00", "type": "withdrawal", "tag": "lost",
                                     "date": NOW.strftime("%Y-%m-%d"), "from_account": ACC, "from_asset": "BEAR",
                                     "from_qty": "10", "note": "Ausbuchung (Totalverlust)"})
    assert not res.errors, res.errors
    ctx.invalidate_data()
    row = next(h for h in report_for(ctx).holdings if h.asset == "BEAR")
    # zum Abrufzeitpunkt stimmte der Bestand (10 = 10): später ausgebucht, daher abgestimmt – nicht „ausgebucht“
    assert row.status != "extern_ausgebucht"


def test_overview_lists_explained_position_under_done_and_allows_undo(cfg):  # noqa: F811
    from fastapi.testclient import TestClient

    from tests.test_diagnosis import client_with_import

    rows = [tx("B1", "2025-05-16T13:20:00Z", "deposit", to=(ACC, "BEAR", "32.258636955"), value="0")]
    c = client_with_import(cfg, rows, [*ASSETS, asset("BEAR")])
    try:
        ctx = c.app.state.ctx
        res = journal_service(ctx).save({"kind": "expert", "time": "21:59", "type": "withdrawal", "tag": "lost",
                                         "date": "2026-09-30", "from_account": ACC, "from_asset": "BEAR",
                                         "from_qty": "32.258636955", "note": "Ausbuchung (Totalverlust)"})
        assert not res.errors, res.errors
        add_wallet(ctx, ACC, {"BEAR": "32.258636955"}, observed=NOW - timedelta(hours=1), complete=True, sid=1)
        ctx.invalidate_data()
        page = c.get("/quality/diagnose").text
        assert 'id="erledigt"' in page and "automatisch erklärt" in page and "Ausbuchung ansehen" in page
        assert "Bestand weicht von der externen Quelle ab: BEAR" not in page  # keine Aufgabe mehr
        assert "/journal/writeoff?asset=BEAR" in page
        # die Ausbuchung zurückzunehmen (eigene Seite) öffnet die Abweichung wieder
        assert c.get("/journal/writeoff?asset=BEAR&account=" + ACC.replace(" ", "%20")).status_code == 200
    finally:
        c.__exit__(None, None, None)
    assert isinstance(c, TestClient)


def test_undoing_the_write_off_reopens_the_deviation(cfg):  # noqa: F811
    from app.journal.writeoff import writeoff_service

    ctx, _rep, row = _setup(cfg, "32.258636955")
    assert row.status == "extern_ausgebucht" and row.tx_ids
    assert writeoff_service(ctx).undo(list(row.tx_ids)) == len(row.tx_ids)
    ctx.invalidate_data()
    rep = report_for(ctx)
    row = next(h for h in rep.holdings if h.asset == "BEAR")
    assert row.status == "extern_ok" and not _holding_findings(rep)  # Bestand wieder 32,258…, Wallet stimmt
