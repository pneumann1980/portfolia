"""0.101.0 Stufe 5: abgeleitete Vorschläge nur mit Beleg nach dem Stichtag; sonst nur Hinweise zu Ursachen."""

# ruff: noqa: F811
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.cutover import core as CO
from app.cutover import corrections as CR
from app.cutover import derive as DV
from app.cutover import service as CS
from tests.helpers import ASSETS, tx
from tests.test_cutover_fix import _rows
from tests.test_diagnosis import client_with_import
from tests.test_review_fixes import cfg  # noqa: F401

D = Decimal
CUT = date(2025, 1, 1)


def _ref(ctx, account, asset, qty, as_of):
    ctx.db.x("INSERT INTO reference_balance(account, asset_id, qty, as_of, source, created_at) VALUES (?,?,?,?,?,?)",
             (account, asset, qty, as_of, "statement", "2026-01-01"))
    ctx.invalidate_overlay()


@pytest.fixture
def cx(cfg):
    c = client_with_import(cfg, _rows(), ASSETS, valuation="2025-03-31")
    try:
        yield c
    finally:
        c.__exit__(None, None, None)


def _m(ctx):
    return CO.models(ctx, CUT)[0]


def test_no_booking_since_the_cutoff_gives_a_derived_proposal_that_resolves_itself(cx):
    ctx = cx.app.state.ctx
    _ref(ctx, "Wallet", "ETH", "1.5", "2025-03-15")  # berechnet 2, seit dem Stichtag keine Buchung auf Wallet/ETH
    out = DV.derive(ctx, _m(ctx), observed=False)
    (p,) = out["proposals"]
    got = (p["account"], p["asset"], p["kind"], p["qty"], p["mode"])
    assert got == ("Wallet", "ETH", "remove", D("0.5"), "derived")
    assert p["evidence"].startswith("abgeleitet:") and "keine Buchung" in p["evidence"] and not out["hints"]
    r = CR.apply(ctx, CUT, account=p["account"], asset=p["asset"], kind=p["kind"], qty=p["qty"], mode=p["mode"],
                 evidence=p["evidence"])
    assert not r["errors"]
    row = ctx.db.q1("SELECT mode, evidence FROM cutover_correction WHERE id=?", (r["id"],))
    assert row["mode"] == "derived"
    note = ctx.db.q1("SELECT note FROM journal_tx WHERE tx_id=?", (r["tx_id"],))["note"]
    assert note.startswith("Stichtag-Korrektur 01.01.2025, abgeleitet:") and len(note) <= 120
    again = DV.derive(ctx, _m(ctx), observed=False)
    assert not again["proposals"] and not again["hints"]  # Beleg und berechneter Bestand stimmen jetzt überein


def test_derived_add_needs_date_and_cost_like_any_addition(cx):
    ctx = cx.app.state.ctx
    _ref(ctx, "Wallet", "ETH", "2.25", "2025-03-15")
    (p,) = DV.derive(ctx, _m(ctx), observed=False)["proposals"]
    assert p["kind"] == "add" and p["qty"] == D("0.25")
    bad = CR.apply(ctx, CUT, account="Wallet", asset="ETH", kind="add", qty=p["qty"], mode="derived",
                   evidence=p["evidence"])
    assert bad["errors"]
    ok = CR.apply(ctx, CUT, account="Wallet", asset="ETH", kind="add", qty=p["qty"], mode="derived",
                  evidence="Abrechnung 2024-12", acq_date="2024-12-10", cost_eur="500")
    assert not ok["errors"]
    note = ctx.db.q1("SELECT note FROM journal_tx WHERE tx_id=?", (ok["tx_id"],))["note"]
    assert "abgeleitet: Abrechnung 2024-12" in note


def test_bookings_in_between_give_only_hints_never_a_cutoff_proposal(cx):
    ctx = cx.app.state.ctx
    _ref(ctx, "Börse", "BTC", "0.35", "2025-03-31")  # berechnet 0,4; Verkauf S1 und Kauf B4 liegen dazwischen
    out = DV.derive(ctx, _m(ctx), observed=False)
    assert not out["proposals"]
    (h,) = out["hints"]
    assert h["cause"] == "fees" and "S1" in h["tx_ids"] and h["diff"] == D("-0.05") and h["window"] == 2


def test_open_transfers_that_explain_the_difference_are_named(cfg):
    rows = [*_rows(),
            tx("T1", "2025-02-10T10:00:00Z", "withdrawal", frm=("Börse", "BTC", "0.05"), value="0"),
            tx("W1", "2025-02-20T10:00:00Z", "deposit", to=("Wallet", "BTC", "0.01"), value="10")]
    c = client_with_import(cfg, rows, ASSETS, valuation="2025-03-31")
    try:
        ctx = c.app.state.ctx
        _ref(ctx, "Wallet", "BTC", "0.06", "2025-03-20")
        out = DV.derive(ctx, _m(ctx), observed=False)
        assert not out["proposals"]
        (h,) = [x for x in out["hints"] if x["asset"] == "BTC" and x["account"] == "Wallet"]
        assert h["cause"] == "transfer" and h["tx_ids"] == ["T1"] and "Börse" in h["text"]
    finally:
        c.__exit__(None, None, None)


def test_evidence_before_the_cutoff_or_equal_is_ignored_and_nothing_without_evidence(cx):
    ctx = cx.app.state.ctx
    assert DV.derive(ctx, _m(ctx), observed=False) == {"proposals": [], "hints": []}
    _ref(ctx, "Wallet", "ETH", "9", "2024-12-31")  # vor dem Stichtag: Sache der Stichtag-Belege, nicht abgeleitet
    _ref(ctx, "Wallet", "ETH", "2", "2025-03-15")  # stimmt
    assert DV.derive(ctx, _m(ctx), observed=False) == {"proposals": [], "hints": []}


def test_page_shows_derived_only_on_request(cx):
    ctx = cx.app.state.ctx
    _ref(ctx, "Wallet", "ETH", "1.5", "2025-03-15")
    plain = cx.get("/settings/stichtag?cutoff=2025-01-01")
    assert plain.status_code == 200 and "Belege nach dem Stichtag auswerten" in plain.text
    assert "vorbereiten (abgeleitet)" not in plain.text
    r = cx.get("/settings/stichtag?cutoff=2025-01-01&derive=1")
    assert r.status_code == 200 and "vorbereiten (abgeleitet)" in r.text and "keine Buchung" in r.text
    assert CS.preview(ctx, CUT)["derived"] is None


def test_observed_balance_of_a_complete_source_is_evidence_too(cfg):
    from datetime import UTC, datetime, timedelta

    from tests.test_diagnosis import add_wallet, make_ctx

    rows = [tx("H1", "2024-12-01T00:00:00Z", "deposit", to=("Wallet 1", "KAS", "100"), value="10"),
            tx("H2", "2024-12-01T00:00:00Z", "deposit", to=("Wallet 2", "KAS", "200"), value="20"),
            tx("H3", "2025-02-01T00:00:00Z", "deposit", to=("Wallet 2", "KAS", "5"), value="1")]
    ctx = make_ctx(cfg, rows, ASSETS, valuation="2025-06-30")
    obs = datetime.now(UTC) - timedelta(hours=1)
    add_wallet(ctx, "Wallet 1", {"KAS": "99.5"}, observed=obs, complete=True, sid=1)  # keine Buchung seit dem Stichtag
    add_wallet(ctx, "Wallet 2", {"KAS": "204"}, observed=obs, complete=True, sid=2)  # Einzahlung im aktiven Zeitraum
    m = CO.models(ctx, CUT)[0]
    out = DV.derive(ctx, m)
    (p,) = out["proposals"]
    assert (p["account"], p["kind"], p["qty"], p["kind_label"]) == ("Wallet 1", "remove", D("0.5"),
                                                                    "Bestand laut Quelle")
    (h,) = out["hints"]
    assert h["account"] == "Wallet 2" and h["diff"] == D("-1") and h["window"] == 1
    assert DV.derive(ctx, m, observed=False) == {"proposals": [], "hints": []}  # ohne Quellen-Beleg nichts
