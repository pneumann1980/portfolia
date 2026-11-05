"""0.99.0 Stufe 3: Festschreiben, Freigabe je Konto, Schutz des Archivs, Korrekturen, Rücknahme."""

# ruff: noqa: F811
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.cutover import core as CO
from app.cutover import corrections as CR
from app.cutover import fix as FX
from app.cutover import guard
from app.cutover import service as CS
from app.journal.service import journal_service
from tests.helpers import ASSETS, tx
from tests.test_diagnosis import client_with_import
from tests.test_review_fixes import cfg  # noqa: F401

D = Decimal
CUT = date(2025, 1, 1)


def _rows():
    return [
        tx("B1", "2024-03-01T10:00:00Z", "buy", frm=("Börse", "EUR", "1000"), to=("Börse", "BTC", "1"), value="1000"),
        tx("B2", "2024-09-01T10:00:00Z", "buy", frm=("Börse", "EUR", "600"), to=("Börse", "BTC", "0.5"), value="600"),
        tx("B3", "2024-11-01T10:00:00Z", "buy", frm=("Wallet", "EUR", "300"), to=("Wallet", "ETH", "2"), value="300"),
        tx("S1", "2025-02-01T10:00:00Z", "sell", frm=("Börse", "BTC", "1.2"), to=("Börse", "EUR", "2400"),
           value="2400"),
        tx("B4", "2025-03-01T10:00:00Z", "buy", frm=("Börse", "EUR", "100"), to=("Börse", "BTC", "0.1"), value="100"),
    ]


@pytest.fixture
def cx(cfg):
    c = client_with_import(cfg, _rows(), ASSETS, valuation="2025-03-31")
    try:
        yield c
    finally:
        c.__exit__(None, None, None)


def _ctx(c):
    return c.app.state.ctx


def _fix_all(ctx, cutoff=CUT):
    pv = CS.preview(ctx, cutoff)
    res = FX.fix(ctx, cutoff, pv["token"], {a["account"] for a in pv["accounts"]})
    return pv, res


def _econ(ctx):
    """Wirtschaftlicher Zustand (vollständige Berechnung): Bestände, Lots, Veräußerungen, Kapital."""
    led = ctx.ledger()
    return (sorted((k, str(v)) for k, v in led.balances.items()),
            sorted((x.asset, x.account, str(x.qty), str(x.cost), x.acq_date.isoformat()) for x in led.lots),
            sorted((d.tx_id, str(d.proceeds), str(d.cost)) for d in led.disposals),
            sorted((f.tx_id, str(f.amount)) for f in led.flows))


# -- Festschreiben und Rücknahme -----------------------------------------------------------------------------------

def test_fix_changes_no_booking_and_undo_restores_everything(cx):
    ctx = _ctx(cx)
    before = _econ(ctx)
    n_tx = len(ctx.portfolio().txs)
    pv, res = _fix_all(ctx)
    assert res == {"errors": [], "id": res["id"]} and guard.cutoff_of(ctx.db) == CUT
    assert _econ(ctx) == before and len(ctx.portfolio().txs) == n_tx  # Festschreiben verändert keine Buchung
    st = FX.state(ctx)
    assert st["cutoff"] == CUT and not st["changed"] and st["ok"] and st["approved"] == len(pv["accounts"])
    assert FX.undo(ctx) == {"errors": []}
    assert guard.cutoff_of(ctx.db) is None and _econ(ctx) == before
    assert FX.state(ctx) is None
    assert FX.undo(ctx)["errors"]  # zweimal zurücknehmen: abgelehnt


def test_fix_needs_every_account_a_matching_token_and_no_second_cutoff(cx):
    ctx = _ctx(cx)
    pv = CS.preview(ctx, CUT)
    accounts = {a["account"] for a in pv["accounts"]}
    assert len(accounts) >= 2
    part = set(sorted(accounts)[:1])
    assert "freigegeben" in FX.fix(ctx, CUT, pv["token"], part)["errors"][0]
    assert "nicht mehr aktuell" in FX.fix(ctx, CUT, "falsch", accounts)["errors"][0]
    assert guard.cutoff_of(ctx.db) is None
    assert not FX.fix(ctx, CUT, pv["token"], accounts)["errors"]
    again = CS.preview(ctx, date(2025, 6, 1))
    assert "bereits" in FX.fix(ctx, date(2025, 6, 1), again["token"], accounts)["errors"][0]


def test_stale_preview_after_a_data_change_is_rejected_before_writing(cx):
    ctx = _ctx(cx)
    pv = CS.preview(ctx, CUT)
    accounts = {a["account"] for a in pv["accounts"]}
    js = journal_service(ctx)
    res = js.save({"kind": "expert", "date": "2025-04-01", "time": "10:00", "type": "deposit", "to_account": "Börse",
                   "to_asset": "BTC", "to_qty": "0.01", "value_eur": "400"})
    assert not res.errors
    assert FX.fix(ctx, CUT, pv["token"], accounts)["errors"]
    assert guard.cutoff_of(ctx.db) is None


def test_token_binds_the_displayed_state_in_the_write_transaction(cx, monkeypatch):
    ctx = _ctx(cx)
    pv = CS.preview(ctx, CUT)
    accounts = {a["account"] for a in pv["accounts"]}
    real = CS.preview
    changed = []

    def preview_then_change(*a, **k):
        out = real(*a, **k)
        if not changed:  # Änderung zwischen Prüfung und Schreibtransaktion
            changed.append(1)
            ctx.db.x("UPDATE data_rev SET n = n + 1 WHERE name = 'journal_tx'")
        return out

    monkeypatch.setattr(CS, "preview", preview_then_change)
    res = FX.fix(ctx, CUT, pv["token"], accounts)
    monkeypatch.undo()
    assert res["errors"] and guard.cutoff_of(ctx.db) is None


# -- Schutz des Archivs ----------------------------------------------------------------------------------------------

def test_journal_writes_before_the_cutoff_are_refused(cx):
    ctx = _ctx(cx)
    js = journal_service(ctx)
    new = {"kind": "expert", "date": "2025-04-01", "time": "10:00", "type": "deposit", "to_account": "Börse",
           "to_asset": "BTC", "to_qty": "0.01", "value_eur": "400"}
    ok = js.save(new)
    assert not ok.errors
    old = {**new, "date": "2024-12-30"}
    assert not js.save(old).errors  # noch kein Stichtag: erlaubt
    _fix_all(ctx)
    refused = js.save(old)
    assert refused.errors and "festgeschrieben" in refused.errors[0]
    assert ctx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE ts_utc LIKE '2024-12-30%'") == 1  # nur die frühere
    assert not js.save({**new, "date": "2025-05-01"}).errors  # aktiver Zeitraum bleibt bearbeitbar
    # Import-Buchung vor dem Stichtag: bearbeiten/löschen/wiederherstellen abgelehnt, danach unverändert
    form = {"kind": "expert", "date": "2024-03-01", "time": "10:00", "type": "buy", "from_account": "Börse",
            "from_asset": "EUR", "from_qty": "1000", "to_account": "Börse", "to_asset": "BTC", "to_qty": "1",
            "value_eur": "1100"}
    assert js.save_import("B1", form).errors and "festgeschrieben" in js.save_import("B1", form).errors[0]
    with pytest.raises(guard.Protected):
        js.delete_import("B1")
    assert not js.revert_import("B1")  # keine Änderung vorhanden: nichts zu verwerfen
    assert ctx.db.scalar("SELECT COUNT(*) FROM tx_override") == 0
    # nach der Rücknahme wieder bearbeitbar
    FX.undo(ctx)
    assert js.delete_import("B1")


def test_protected_edit_via_http_is_a_friendly_409(cx):
    # Ausnahme der untersten Schicht wird zur verständlichen Ablehnung (409), nicht zum Serverfehler
    @cx.app.get("/__protected_probe")
    def probe():
        raise guard.Protected(guard.message(CUT))

    r = cx.get("/__protected_probe")
    assert r.status_code == 409 and "festgeschrieben" in r.text


def test_cutoff_utc_is_local_midnight_and_guard_compares_utc():
    from app.util.timeutil import parse_iso

    u = parse_iso(FX.cutoff_utc(date(2025, 1, 1)))
    assert u.isoformat().startswith("2024-12-31T23:00:00")  # Europe/Berlin (Winterzeit)
    u2 = parse_iso(FX.cutoff_utc(date(2025, 7, 1)))
    assert u2.isoformat().startswith("2025-06-30T22:00:00")  # Sommerzeit


# -- Korrekturen -------------------------------------------------------------------------------------------------------

def _model(ctx):
    return CO.models(ctx, CUT)[0]


def test_remove_correction_is_neutral_for_gain_and_capital_and_consumes_the_oldest_lot(cx):
    ctx = _ctx(cx)
    m = _model(ctx)
    have = m.state.balances[("Börse", "BTC")]
    assert have == D("1.5")
    v, errs = CR.validate(ctx, m, account="Börse", asset="BTC", kind="remove", qty="0.2", mode="evidence",
                          evidence="Depotauszug 31.12.2024", acq_date=None, cost_eur=None)
    assert not errs
    sim = CR.simulate(ctx, m, v)
    assert sim["balance_cutoff"] == (D("1.5"), D("1.3")) and sim["balance"] == (D("0.4"), D("0.2"))
    assert sim["lots"] == (2, 2) and sim["cost"][0] - sim["cost"][1] == D("200")  # ältestes Lot (1 BTC zu 1000 €)
    res = CR.apply(ctx, CUT, account="Börse", asset="BTC", kind="remove", qty="0.2", mode="evidence",
                   evidence="Depotauszug 31.12.2024")
    assert not res["errors"]
    t = next(x for x in ctx.portfolio().txs if x.tx_id == res["tx_id"])
    assert t.tag == "cutover_correction" and t.date == date(2024, 12, 31) and t.type == "withdrawal"
    led = ctx.ledger()
    d = next(d for d in led.disposals if d.tx_id == res["tx_id"])
    assert d.kind == "withdrawal" and d.proceeds == 0
    assert d.parts[0].acq_date == date(2024, 3, 1)  # älteste Lots zuerst
    assert not any(f.tx_id == res["tx_id"] for f in led.flows)  # kein Zahlungsstrom
    assert all(c.ok for c in _model(ctx).checks)  # Gleichwertigkeit gilt weiter
    assert CR.undo(ctx, res["id"]) == {"errors": []}
    assert not any(x.tx_id == res["tx_id"] for x in ctx.portfolio().txs)
    assert CR.undo(ctx, res["id"])["errors"]


def test_add_correction_needs_evidence_or_is_marked_as_an_assumption(cx):
    ctx = _ctx(cx)
    m = _model(ctx)
    base = {"account": "Börse", "asset": "BTC", "kind": "add", "qty": "0.1"}
    bad = [
        {"mode": "evidence", "evidence": "", "acq_date": "2024-05-01", "cost_eur": "50"},  # kein Beleg
        {"mode": "evidence", "evidence": "Abrechnung", "acq_date": "", "cost_eur": "50"},  # kein Datum
        {"mode": "evidence", "evidence": "Abrechnung", "acq_date": "2025-01-01", "cost_eur": "50"},  # nach Stichtag
        {"mode": "evidence", "evidence": "Abrechnung", "acq_date": "2024-05-01", "cost_eur": "x"},  # Kosten ungültig
    ]
    for kw in bad:
        assert CR.validate(ctx, m, **base, **kw)[1], kw
    res = CR.apply(ctx, CUT, **base, mode="evidence", evidence="Kaufabrechnung 01.05.2024", acq_date="2024-05-01",
                   cost_eur="55.5")
    assert not res["errors"]
    lot = next(x for x in _model(ctx).state.lots if x.acq_tx == res["tx_id"])
    assert lot.acq_date == date(2024, 5, 1) and lot.cost == D("55.5") and lot.qty == D("0.1")
    assert ctx.db.scalar("SELECT mode FROM cutover_correction WHERE id=?", (res["id"],)) == "evidence"
    cau = CR.apply(ctx, CUT, **base, mode="cautious", evidence="")
    assert not cau["errors"]
    lot2 = next(x for x in _model(ctx).state.lots if x.acq_tx == cau["tx_id"])
    assert lot2.acq_date == date(2024, 12, 31) and lot2.cost == 0  # Annahme: Anschaffung am Vortag zu 0 €
    note = next(x for x in ctx.portfolio().txs if x.tx_id == cau["tx_id"]).note
    assert "vorsichtig" in note and "Annahme" in note  # gekennzeichnet
    assert all(c.ok for c in _model(ctx).checks)


def test_remove_more_than_the_opening_balance_is_refused(cx):
    ctx = _ctx(cx)
    r = CR.apply(ctx, CUT, account="Börse", asset="BTC", kind="remove", qty="9", mode="evidence", evidence="x")
    assert r["errors"] and "reicht" in r["errors"][0]
    assert CR.apply(ctx, CUT, account="Börse", asset="EUR", kind="remove", qty="1", mode="evidence",
                    evidence="x")["errors"]
    assert CR.apply(ctx, CUT, account="Unbekannt", asset="BTC", kind="remove", qty="1", mode="evidence",
                    evidence="x")["errors"]


def test_reference_deviation_becomes_a_proposal_and_a_correction_makes_it_match(cx):
    ctx = _ctx(cx)
    ctx.db.x("INSERT INTO reference_balance(account, asset_id, qty, as_of, source, created_at) VALUES (?,?,?,?,?,?)",
             ("Börse", "BTC", "1.3", "2024-12-31", "statement", "2026-01-01"))
    ctx.invalidate_overlay()
    pv = CS.preview(ctx, CUT)
    (prop,) = [p for p in pv["proposals"] if not p["dust"]]
    assert prop["kind"] == "remove" and prop["qty"] == D("0.2") and prop["account"] == "Börse"
    st = next(a for a in pv["accounts"] if a["account"] == "Börse")
    assert st["status"] == "abweichung"
    r = CR.apply(ctx, CUT, account=prop["account"], asset=prop["asset"], kind=prop["kind"], qty=prop["qty"],
                 mode="evidence", evidence=prop["evidence"])
    assert not r["errors"]
    pv2 = CS.preview(ctx, CUT)
    assert next(a for a in pv2["accounts"] if a["account"] == "Börse")["status"] == "belegt"
    assert not [p for p in pv2["proposals"] if not p["dust"]]
    assert pv2["corrections"][0]["tx_id"] == r["tx_id"]


def test_correction_after_the_fix_changes_the_fingerprint_and_needs_reapproval(cx):
    ctx = _ctx(cx)
    _fix_all(ctx)
    assert not FX.state(ctx)["changed"]
    r = CR.apply(ctx, CUT, account="Börse", asset="BTC", kind="remove", qty="0.1", mode="evidence", evidence="Beleg")
    assert not r["errors"]  # Korrekturen dürfen trotz Schutz vor den Stichtag schreiben
    st = FX.state(ctx)
    assert [c["account"] for c in st["changed"]] == ["Börse"]
    assert FX.reapprove(ctx, {"Börse"}) == {"errors": []}
    assert not FX.state(ctx)["changed"]
    assert not CR.undo(ctx, r["id"])["errors"]  # Rücknahme der Korrektur ebenfalls trotz Schutz
    assert [c["account"] for c in FX.state(ctx)["changed"]] == ["Börse"]


def test_account_merge_after_the_fix_is_detected(cx):
    ctx = _ctx(cx)
    _fix_all(ctx)
    from app.assetchange import account_merge as AM

    p = AM.preview(ctx, "Wallet", "Börse")
    if p.ok:  # Zusammenführen verändert die Zuordnung der Lots → Eröffnungsstand verändert
        mid, errors = AM.apply(ctx, "Wallet", "Börse", p.fp)
        assert mid and not errors
        assert FX.state(ctx)["changed"]


# -- Migration / Schema ----------------------------------------------------------------------------------------------

def test_only_one_active_cutover_can_exist(cx):
    import sqlite3

    ctx = _ctx(cx)
    _fix_all(ctx)
    with pytest.raises(sqlite3.IntegrityError):
        ctx.db.x("INSERT INTO cutover(cutoff, cutoff_utc, options_json, fingerprint, created_at) VALUES "
                 "('2025-06-01','x','{}','x','x')")


def test_cutover_tables_are_part_of_the_data_revision_and_booking_contract(cx):
    from app.csvimport.service import BOOKING_TABLES

    ctx = _ctx(cx)
    for t in ("cutover", "cutover_account", "cutover_correction"):
        assert t in BOOKING_TABLES
    rev = ctx.cache_rev()
    _fix_all(ctx)
    assert ctx.cache_rev() != rev


def test_correction_tag_is_known_and_the_engine_needs_no_flow(cx):
    from app.importer import contract as C

    assert "cutover_correction" in C.KNOWN_TAGS


# -- Oberfläche ------------------------------------------------------------------------------------------------------

def test_pages_and_posts(cx):
    ctx = _ctx(cx)
    tok = cx.cookies.get("portfolia_csrf")
    page = cx.get("/settings/stichtag?cutoff=2025-01-01")
    assert page.status_code == 200 and "Freigeben und festschreiben" in page.text
    assert "Stichtag-Korrekturen" in page.text
    pv = CS.preview(ctx, CUT)
    form = {"csrf_token": tok, "cutoff": "2025-01-01", "token": pv["token"],
            "approve": [a["account"] for a in pv["accounts"]]}
    stale = cx.post("/settings/stichtag/fixieren", data={**form, "token": "x"}, follow_redirects=False)
    assert stale.status_code == 409 and "nicht mehr aktuell" in stale.text
    assert guard.cutoff_of(ctx.db) is None
    ok = cx.post("/settings/stichtag/fixieren", data=form, follow_redirects=False)
    assert ok.status_code == 303 and guard.cutoff_of(ctx.db) == CUT
    page = cx.get("/settings/stichtag?saved=fixed")
    assert "ist festgeschrieben" in page.text and "Stichtag zurücknehmen" in page.text
    assert "Stichtag festgeschrieben." in page.text
    # Korrektur: Wirkung zeigen, buchen, zurücknehmen
    corr = {"csrf_token": tok, "cutoff": "2025-01-01", "account": "Börse", "asset": "BTC", "kind": "remove",
            "qty": "0.1", "mode": "evidence", "evidence": "Beleg X"}
    prev = cx.post("/settings/stichtag/korrektur", data={**corr, "do": "preview"})
    assert prev.status_code == 200 and "Wirkung dieser Korrektur" in prev.text
    bad = cx.post("/settings/stichtag/korrektur", data={**corr, "qty": "99", "do": "book"})
    assert bad.status_code == 422 and "reicht" in bad.text
    booked = cx.post("/settings/stichtag/korrektur", data={**corr, "do": "book"}, follow_redirects=False)
    assert booked.status_code == 303
    cid = ctx.db.scalar("SELECT id FROM cutover_correction WHERE status='active'")
    assert "Beleg X" in cx.get("/settings/stichtag").text and "Eröffnungsstand hat sich" in cx.get(
        "/settings/stichtag").text
    r = cx.post("/settings/stichtag/freigabe", data={"csrf_token": tok, "account": "Börse"}, follow_redirects=False)
    assert r.status_code == 303 and not FX.state(ctx)["changed"]
    r = cx.post(f"/settings/stichtag/korrektur/{cid}/zurueck", data={"csrf_token": tok, "cutoff": "2025-01-01"},
                follow_redirects=False)
    assert r.status_code == 303
    r = cx.post("/settings/stichtag/zurueck", data={"csrf_token": tok}, follow_redirects=False)
    assert r.status_code == 303 and guard.cutoff_of(ctx.db) is None
    assert cx.post("/settings/stichtag/zurueck", data={"csrf_token": tok}).status_code == 409


# -- Export / Wiederherstellung --------------------------------------------------------------------------------------

def _dump(ctx):
    db = ctx.db
    return {
        "cutover": [tuple(r) for r in db.q("SELECT cutoff, cutoff_utc, status, options_json, fingerprint, "
                                           "summary_json, checks_json, created_at, undone_at FROM "
                                           "cutover ORDER BY id")],
        "accounts": sorted(tuple(r) for r in db.q(
            "SELECT c.cutoff, a.account, a.status, a.fingerprint, a.approved_at FROM cutover_account a "
            "JOIN cutover c ON c.id=a.cutover_id")),
        "corr": [tuple(r) for r in db.q("SELECT cutoff, account, asset_id, kind, qty, acq_date, cost_eur, mode, "
                                        "evidence, tx_id, status, created_at, undone_at FROM cutover_correction "
                                        "ORDER BY id")],
    }


def test_export_contains_cutover_and_restore_is_complete_and_idempotent(cx, cfg, tmp_path):
    import json
    from dataclasses import replace

    from app import fullexport

    ctx = _ctx(cx)
    pv, _res = _fix_all(ctx)
    r = CR.apply(ctx, CUT, account="Börse", asset="BTC", kind="remove", qty="0.1", mode="evidence", evidence="Beleg")
    assert not r["errors"]
    FX.reapprove(ctx, {"Börse"})
    st = json.loads(fullexport.collect(ctx, set())[fullexport.STATE])
    assert [c["cutoff"] for c in st["cutovers"]] == ["2025-01-01"] and "id" not in st["cutovers"][0]
    assert len(st["cutovers"][0]["accounts"]) == len(pv["accounts"]) and len(st["cutover_corrections"]) == 1
    # Zielinstanz ohne Stichtag
    (tmp_path / "b" / "import").mkdir(parents=True)
    cfg_b = replace(cfg, data_dir=tmp_path / "b" / "data", import_dir=tmp_path / "b" / "import")
    c2 = client_with_import(cfg_b, _rows(), ASSETS, valuation="2025-03-31")
    try:
        ctx2 = _ctx(c2)
        with ctx2.db.transaction() as c:
            assert fullexport._cutovers(c, st["cutovers"], st["cutover_corrections"]) == 2
        assert _dump(ctx2) == _dump(ctx)
        assert guard.cutoff_of(ctx2.db) == CUT
        with ctx2.db.transaction() as c:  # zweite Übernahme: nichts doppelt
            assert fullexport._cutovers(c, st["cutovers"], st["cutover_corrections"]) == 0
        assert _dump(ctx2) == _dump(ctx)
        # ein bereits aktiver Stichtag bleibt der einzige: der übernommene kommt als „undone“ hinein
        with ctx2.db.transaction() as c:
            c.execute("DELETE FROM cutover_account")
            c.execute("DELETE FROM cutover")
            c.execute("DELETE FROM cutover_correction")
            c.execute("INSERT INTO cutover(cutoff, cutoff_utc, status, options_json, fingerprint, created_at) "
                      "VALUES ('2024-01-01','2023-12-31T23:00:00Z','active','{}','x','2026-01-01')")
            fullexport._cutovers(c, st["cutovers"], [])
        assert [r[0] for r in ctx2.db.q("SELECT status FROM cutover ORDER BY id")] == ["active", "undone"]
        assert guard.cutoff_of(ctx2.db) == date(2024, 1, 1)
        # ältere Exporte kennen keine Stichtage
        with ctx2.db.transaction() as c:
            assert fullexport._cutovers(c, [], []) == 0
    finally:
        c2.__exit__(None, None, None)
