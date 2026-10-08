"""M24/AP2 – Finanzielle Integritätsprüfung: rein lesend, Invarianten, keine Doppelbefunde, Status aus den
Entscheidungen, Fortschritt/Job, Export, Fehler transparent. Synthetische Daten, temporäre Datenbanken."""

from __future__ import annotations

import dataclasses
import json
import threading
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.diagnosis import actions as A
from app.diagnosis import integrity as I
from app.diagnosis.collect import collect
from app.diagnosis.engine import diagnose
from app.main import build_app
from tests.helpers import ASSETS, tx
from tests.test_diagnosis import TOKA_ASSETS, asset, cfg, fingerprint, make_ctx, popkat_like_rows  # noqa: F401


def finger(db) -> str:
    """Prüfsumme wie :func:`tests.test_diagnosis.fingerprint`, ohne das gespeicherte Prüfergebnis selbst."""
    saved = db.get_state(I.STATE_KEY)
    db.x("DELETE FROM app_state WHERE key=?", (I.STATE_KEY,))
    try:
        return fingerprint(db)
    finally:
        if saved is not None:
            db.set_state(I.STATE_KEY, saved)


def test_run_is_read_only_and_reports_duplicate_with_preferred_solution(cfg):  # noqa: F811
    ctx = make_ctx(cfg, popkat_like_rows(), TOKA_ASSETS)
    before = finger(ctx.db)
    r = I.run(ctx)
    assert r.ok and not r.stale and r.txs == 4
    assert finger(ctx.db) == before  # Buchungen, Zuordnungen, Entscheidungen unverändert
    dup = next(it for it in r.items if it.category == "dublette" and "TOKA" in it.title)
    assert dup.severity == "kritisch" and dup.confidence == "hoch plausibel" and dup.status == "offen"
    assert dup.preferred and dup.preferred_key and dup.finding_id
    assert set(dup.tx_ids) >= {"M1", "T1"} and dup.cause_kind == "verdacht"
    assert len({it.id for it in r.items}) == len(r.items)  # keine doppelten Befunde
    # gespeichert und wieder lesbar
    again = I.load(ctx.db)
    assert again is not None and [it.id for it in again.items] == [it.id for it in r.items]


def test_ledger_invariants_detect_calculation_errors(cfg):  # noqa: F811
    rows = [tx("b1", "2025-01-10", "buy", frm=("Börse", "EUR", "100"), to=("Börse", "BTC", "1"), value=100),
            tx("b2", "2025-02-10", "buy", frm=("Börse", "EUR", "200"), to=("Börse", "BTC", "1"), value=200),
            tx("s1", "2025-03-10", "sell", frm=("Börse", "BTC", "1.5"), to=("Börse", "EUR", "450"), value=450)]
    ctx = make_ctx(cfg, rows, ASSETS)
    rep = diagnose(collect(ctx))
    checks: dict[str, int] = {}
    assert I.check_ledger(rep, checks) == []  # korrektes Ledger: keine Verletzung
    assert checks["disposals"] == 1
    led = rep.snapshot.ledger
    # Fehler einbauen (nur im Speicher): Lot verschwindet, Veräußerungsanteil mit Anschaffung nach Verkauf
    lots = [dataclasses.replace(lot, qty=lot.qty - Decimal("0.25")) for lot in led.lots]
    d = led.disposals[0]
    parts = [dataclasses.replace(d.parts[0], acq_date=d.date.replace(year=2026)), *d.parts[1:]]
    broken = dataclasses.replace(led, lots=lots, disposals=[dataclasses.replace(d, parts=parts)])
    rep.snapshot = dataclasses.replace(rep.snapshot, ledger=broken)
    items = I.check_ledger(rep, {})
    titles = {it.title for it in items}
    assert "Lots ≠ Bestand: BTC" in titles
    assert any(t.startswith("Inkonsistente Veräußerung: BTC") for t in titles)
    assert all(it.cause_kind == "rechenfehler" and it.severity == "kritisch" for it in items
               if it.title.startswith(("Lots ≠", "Inkonsistente")))


def test_status_follows_user_decisions(cfg):  # noqa: F811
    ctx = make_ctx(cfg, popkat_like_rows(), TOKA_ASSETS)
    r = I.run(ctx)
    dup = next(it for it in r.items if it.category == "dublette" and "TOKA" in it.title)
    assert A.dismiss(ctx, dup.finding_id, I.INDEPENDENT_NOTE).ok
    st = {it.id: it.status for it in I.refresh_status(ctx, I.load(ctx.db)).items}
    assert st[dup.id] == "verworfen"
    r2 = I.run(ctx)  # neue Prüfung respektiert die Entscheidung
    assert next(it for it in r2.items if it.id == dup.id).status == "verworfen"
    d = A.active_dismissals(ctx.db)[dup.finding_id]
    A.reopen(ctx, d.id)
    assert next(it for it in I.run(ctx).items if it.id == dup.id).status == "offen"


def test_concurrent_runs_are_refused(cfg):  # noqa: F811
    ctx = make_ctx(cfg, popkat_like_rows(), TOKA_ASSETS)
    gate, release = threading.Event(), threading.Event()
    real = I.check_prices

    def slow(*a, **k):
        gate.set()
        release.wait(10)
        return real(*a, **k)

    I.check_prices = slow
    try:
        t = threading.Thread(target=I.run, args=(ctx,))
        t.start()
        assert gate.wait(10)
        with pytest.raises(RuntimeError, match="läuft bereits"):
            I.run(ctx)
        release.set()
        t.join(10)
    finally:
        I.check_prices = real
    assert I.load(ctx.db).ok


def test_data_change_during_run_marks_result_stale(cfg, monkeypatch):  # noqa: F811
    ctx = make_ctx(cfg, popkat_like_rows(), TOKA_ASSETS)
    real = I.check_tax

    def change(c, report, checks):
        c.db.x("INSERT INTO journal_tx(tx_id, source, status, ts_utc, date_only, type, to_account, to_asset, to_qty, "
               "value_eur, created_at, updated_at) VALUES ('PF-M-000099', 'manual', 'active', "
               "'2025-05-01T10:00:00Z', 0, 'deposit', 'Wallet E', 'USDT', '1', '1', '2025-05-01', '2025-05-01')")
        return real(c, report, checks)

    monkeypatch.setattr(I, "check_tax", change)
    assert I.run(ctx).stale


def test_failed_run_is_marked_and_shown(cfg, monkeypatch):  # noqa: F811
    ctx = make_ctx(cfg, popkat_like_rows(), TOKA_ASSETS)
    monkeypatch.setattr(I, "check_ledger", lambda *a: (_ for _ in ()).throw(ValueError("kaputt")))
    r = I.run(ctx)
    assert not r.ok and "kaputt" in r.error and I.load(ctx.db).error == r.error


def test_tax_data_conflicts_and_split_price_jump(cfg):  # noqa: F811
    rows = [tx("b", "2024-01-10", "buy", frm=("Depot", "EUR", "1000"), to=("Depot", "AKT", "10"), value=1000),
            tx("sp", "2024-06-10", "corporate_action", tag="split", frm=("Depot", "AKT", "10"),
               to=("Depot", "AKT", "100"))]
    ctx = make_ctx(cfg, rows, [*ASSETS, asset("AKT", qs="yahoo", qid="AKT.DE", asset_class="security")])
    series = ctx.prices.series_for(ctx.portfolio().assets["AKT"])
    for d, close in (("2024-06-06", 100.0), ("2024-06-07", 101.0), ("2024-06-10", 10.1), ("2024-06-11", 10.2)):
        ctx.db.x("INSERT INTO price_daily(series, date, close, ccy, source, fetched_at) VALUES (?,?,?,?,?,?)",
                 (series, d, close, "EUR", "test", "2024-06-12"))
    ctx.db.x("INSERT INTO tax_file(tax_year, filename, origin, sha256, size, format, parser, status, records, matched, "
             "unmatched, conflicts, created_at) VALUES (2024, 'bericht.csv', 'upload', 'abc', 1, 'csv', 'x', 'active', "
             "5, 2, 1, 2, '2025-01-01')")
    r = I.run(ctx)
    jump = next(it for it in r.items if it.title == "Ungewöhnlicher Kurssprung: AKT")
    assert "Split" in jump.cause and jump.category == "kurs"
    tax = next(it for it in r.items if it.title.startswith("Steuerdaten 2024"))
    assert "2 Widersprüche" in tax.cause and tax.severity == "warnung"


def test_web_run_page_filters_and_export(cfg):  # noqa: F811
    make_ctx(cfg, popkat_like_rows(), TOKA_ASSETS)
    with TestClient(build_app(cfg, start_scheduler=False)) as c:
        c.get("/settings")
        c.headers["X-CSRF-Token"] = c.cookies.get("portfolia_csrf")
        assert "Noch keine Prüfung" in c.get("/quality/integrity").text
        r = c.post("/quality/integrity/run", follow_redirects=False)
        assert r.status_code == 303
        html = c.get("/quality/integrity").text
        assert "Letzte Prüfung" in html and "Dubletten &amp; Transfers" in html
        assert "Vorschau der empfohlenen Lösung" in html
        only = c.get("/quality/integrity?category=dublette&severity=kritisch").text
        assert "TOKA" in only
        csv_body = c.get("/quality/integrity/export.csv").content.decode("utf-8-sig")
        assert csv_body.splitlines()[0].startswith("id;category;severity;status") and "dublette" in csv_body
        js = json.loads(c.get("/quality/integrity/export.json").content)
        assert js["run"]["ok"] and js["counts"]["total"] == len(js["items"]) and js["items"][0]["id"]
        assert "Finanzielle Integritätsprüfung" in c.get("/quality").text


def test_identical_bookings_without_identifier_are_suspected_duplicates(cfg):  # noqa: F811
    """Vollständig gleiche Buchungen ohne unterscheidende Kennung → Verdacht mit bevorzugter Lösung (prüfbedürftig);
    gleiche Angaben mit verschiedenen Kennungen derselben Quelle (Teilausführungen) → kein Befund."""
    base = [tx("d", "2025-01-02", "deposit", to=("Börse", "EUR", "1000"), value=1000)]
    buy = tx("k1", "2025-01-05T10:00:00Z", "buy", frm=("Börse", "EUR", "100"), to=("Börse", "ETH", "0.05"), value=100)
    ctx = make_ctx(cfg, [*base, buy, {**buy, "tx_id": "k2"}], ASSETS)
    r = I.run(ctx)
    it = next(i for i in r.items if i.title.startswith("1 Paar vollständig gleicher Buchungen"))
    assert it.confidence == "prüfbedürftig" and it.preferred_key == "hide_second" and set(it.tx_ids) == {"k1", "k2"}
    from app.diagnosis.engine import report_for

    rep = report_for(ctx)
    f = rep.by_id(it.finding_id)
    plan = A.build_plan(ctx, rep, f, "hide_second")
    assert not plan.errors and [op.target for op in plan.ops] == ["k2"]  # die erste Buchung bleibt
    # Teilausführungen derselben Quelle mit eigenen Kennungen: legitim
    a = {**buy, "source": "bitpanda", "source_ref": "fill-1"}
    b = {**buy, "tx_id": "k2", "source": "bitpanda", "source_ref": "fill-2"}
    ctx2 = make_ctx(cfg, [*base, a, b], ASSETS)
    assert not [i for i in I.run(ctx2).items if "vollständig gleicher" in i.title]
