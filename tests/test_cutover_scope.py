"""0.100.0 Stufe 4: Diagnose, Prüfung und Importwege ab dem festgeschriebenen Stichtag."""

# ruff: noqa: F811
from __future__ import annotations

from app.diagnosis.engine import report_for
from tests.test_diagnosis import TOKA_ASSETS, by_kind, make_ctx, popkat_like_rows
from tests.test_review_fixes import cfg  # noqa: F401

HOLD = [{"asset_id": "TOKA", "account": "Wallet K", "qty": "100246.91357802"}]


def _fix(ctx, cutoff: str, utc: str) -> None:
    ctx.db.x("INSERT INTO cutover(cutoff, cutoff_utc, status, options_json, fingerprint, created_at) "
             "VALUES (?,?,?,?,?,?)", (cutoff, utc, "active", "{}", "x", "2026-01-01T00:00:00Z"))
    ctx.invalidate_data()


def _dups(rep):
    return [x for x in by_kind(rep, "duplicate") if "TOKA" in x.title]


def test_findings_only_about_archive_bookings_are_not_open(cfg):
    ctx = make_ctx(cfg, popkat_like_rows(), TOKA_ASSETS, HOLD)
    open_before = _dups(report_for(ctx))
    assert open_before and not report_for(ctx).archived
    _fix(ctx, "2025-06-01", "2025-05-31T22:00:00Z")  # beide Buchungen liegen davor
    rep = report_for(ctx)
    assert not _dups(rep) and rep.stats["archiviert"] == len(rep.archived)
    assert {f.id for f in rep.archived} >= {f.id for f in open_before}
    assert not [f for f in rep.findings if f.id in {a.id for a in rep.archived}]
    # Kinder verweisen nie auf entfernte Befunde
    ids = {f.id for f in rep.findings}
    assert all(c in ids for f in rep.findings for c in f.children)


def test_findings_touching_active_bookings_stay_open(cfg):
    ctx = make_ctx(cfg, popkat_like_rows(), TOKA_ASSETS, HOLD)
    _fix(ctx, "2025-03-10", "2025-03-09T23:00:00Z")  # beide Buchungen am Stichtag selbst = aktiv
    rep = report_for(ctx)
    assert _dups(rep) and not [f for f in rep.archived if "TOKA" in f.title and f.kind == "duplicate"]


def test_mixed_finding_stays_open_when_only_one_booking_is_archive(cfg):
    ctx = make_ctx(cfg, popkat_like_rows(), TOKA_ASSETS, HOLD)
    _fix(ctx, "2025-03-10", "2025-03-10T18:00:00Z")  # M1 (12:00) davor, T1 (21:30) danach
    rep = report_for(ctx)
    assert _dups(rep)  # ein Befund über Archiv- und aktive Buchung bleibt sichtbar


def test_no_cutover_changes_nothing(cfg):
    ctx = make_ctx(cfg, popkat_like_rows(), TOKA_ASSETS, HOLD)
    rep = report_for(ctx)
    assert rep.archived == [] and "archiviert" not in rep.stats


def test_csv_rows_before_the_frozen_cutoff_are_archive_rows(cfg):
    from tests.helpers import ASSETS, tx
    from tests.test_diagnosis import PF_HEADER, client_with_import, rows_of, upload

    c = client_with_import(cfg, [tx("B0", "2025-02-01T10:00:00Z", "deposit", to=("Depot D", "EUR", "10"), value="10")],
                           ASSETS)
    try:
        ctx = c.app.state.ctx
        data = PF_HEADER + "N1,2025-03-01T12:00:00Z,deposit,,,,,Depot D,EUR,25,,,,25,alt\n" \
                           "N2,2025-03-20T12:00:00Z,deposit,,,,,Depot D,EUR,30,,,,30,neu\n"
        old, new = rows_of(c, upload(c, "a.csv", data))
        assert old.status == "new" and new.status == "new"  # ohne Stichtag: beides neu
        _fix(ctx, "2025-03-05", "2025-03-04T23:00:00Z")
        old, new = rows_of(c, upload(c, "b.csv", data.replace("N1", "M1").replace("N2", "M2")))
        assert old.status == "before" and "Stichtag 05.03.2025" in old.warnings[0] and "Archiv" in old.warnings[0]
        assert new.status == "new"
        assert not old.include()
    finally:
        c.__exit__(None, None, None)


def test_integrity_items_about_archive_bookings_are_archived(cfg):
    from types import SimpleNamespace

    from app.diagnosis import integrity as I
    from app.util.timeutil import parse_iso

    snap = SimpleNamespace(cutoff_utc=parse_iso("2025-06-01T00:00:00Z"), pf=SimpleNamespace(txs=[
        SimpleNamespace(tx_id="a", ts=parse_iso("2025-01-01T00:00:00Z")),
        SimpleNamespace(tx_id="b", ts=parse_iso("2025-07-01T00:00:00Z"))]))
    mk = lambda i, ids: I.Item(id=i, category="x", severity="warnung", title="t", cause_kind="k", cause="c",  # noqa: E731
                               impact="i", tx_ids=ids)
    items = [mk("1", ["a"]), mk("2", ["a", "b"]), mk("3", []), mk("4", ["b"])]
    I._archive(items, snap)
    assert [it.status for it in items] == ["archiviert", "offen", "offen", "offen"]
    I._archive(items, SimpleNamespace(cutoff_utc=None, pf=snap.pf))  # ohne Stichtag: nichts
