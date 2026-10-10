"""Abweichungsfenster (M29): Differenzentwicklung aus mehreren Referenzbeständen, nur synthetische Daten.

Abgedeckt: neu entstehende, konstante, verschwindende und gegenläufige Abweichungen, Zeitzonen, mehrere Referenzen am
selben Tag, widersprüchliche Referenzen, Einzelreferenz (nicht eingrenzbar), Ursachenkandidaten mit Evidenz statt
Wahrscheinlichkeit, rein lesendes und deterministisches Verhalten, Laufzeit bei großen Beständen.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.config import Config, Secrets
from app.diagnosis import references as R
from app.diagnosis import windows as W
from app.diagnosis.engine import report_for
from tests.helpers import tx
from tests.test_diagnosis import fingerprint, make_ctx
from tests.test_diagnosis_audit import AUDIT_ASSETS, api, koinly

D = Decimal
ACC = "Börse B"


@pytest.fixture
def cfg(tmp_path) -> Config:
    imp = tmp_path / "import"
    imp.mkdir(parents=True, exist_ok=True)
    return Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                  startup_jobs=False, log_format="text", secrets=Secrets())


def dep(tid: str, ts: str, qty: str, aid: str = "EUR", acc: str = ACC) -> dict:
    return tx(tid, ts, "deposit", to=(acc, aid, qty), value=qty if aid == "EUR" else "1")


def wd(tid: str, ts: str, qty: str, aid: str = "EUR", acc: str = ACC) -> dict:
    return tx(tid, ts, "withdrawal", frm=(acc, aid, qty), value=qty if aid == "EUR" else "1")


def ref(ctx, qty: str, day: str, at: str = "12:00", tz: str = "UTC", aid: str = "EUR", acc: str = ACC):
    res = R.add(ctx, acc, aid, qty, day, "statement", "", at, tz)
    assert res.ok, res.errors
    return res


def trace(ctx, aid: str = "EUR", acc: str = ACC) -> W.PositionTrace:
    return report_for(ctx).traces[(acc, aid)]


def base_rows() -> list[dict]:
    return [dep("D1", "2026-09-01T08:00:00Z", "1000"), dep("D2", "2026-09-15T08:00:00Z", "200")]


def test_01_two_references_new_deviation_is_localised_to_the_window_with_its_transactions(cfg):
    """Zwei Referenzen, zwischen ihnen entsteht eine Abweichung: Fenster, Buchungen und Kandidat (API gilt)."""
    rows = [*base_rows(),
            koinly(dep("K3", "2026-09-24T09:00:00Z", "2000")), api(dep("A3", "2026-09-24T09:03:00Z", "2000"), "u3")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS, valuation="2026-10-01")
    ref(ctx, "1200", "2026-09-16")
    ref(ctx, "3200", "2026-09-30")
    t = trace(ctx)
    first, second = t.segments
    assert first.kind == "nicht_eingrenzbar" or first.kind == "ok"  # erster Punkt: Differenz 0 → keine Abweichung
    assert t.points[0].diff == 0 and t.points[1].ledger == D("5200") and t.points[1].diff == D("-2000")
    assert second.kind == "erstmals" and second.delta == D("-2000")
    assert set(second.tx_ids) == {"K3", "A3"} and second.n_tx == 2
    assert W.KIND_LABEL["erstmals"] in t.summary
    # Kandidat: API-Buchung A3 gilt, Import-Eintrag K3 entfällt; Evidenz statt Prozent; Differenz würde 0
    (c,) = second.candidates
    assert c.kind == "duplicate" and c.evidence == "Stark gestützt" and c.tx_ids == ["K3"]
    assert c.diff_after == 0 and c.reduces and "ist kein Beleg" in c.note
    assert t.rest_strong == 0 and t.rest_all == 0


def test_02_three_references_with_constant_deviation_do_not_localise_a_new_error(cfg):
    rows = [*base_rows(), dep("X1", "2026-09-05T08:00:00Z", "300")]  # Ledger 1500 ab 05.09., Referenz kennt X1 nie
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS, valuation="2026-10-01")
    ref(ctx, "1000", "2026-09-10")
    ref(ctx, "1200", "2026-09-20")
    ref(ctx, "1200", "2026-09-30")
    t = trace(ctx)
    assert [p.diff for p in t.points] == [D("-300"), D("-300"), D("-300")]
    kinds = [s.kind for s in t.segments]
    assert kinds == ["nicht_eingrenzbar", "stabil", "stabil"]
    assert W.KIND_LABEL["stabil"] in t.summary
    assert t.segments[0].flags and "keine Eingrenzung" in t.segments[0].flags[0]


def test_03_deviation_that_disappears_later(cfg):
    rows = [dep("D1", "2026-09-01T08:00:00Z", "1000"), dep("X1", "2026-09-10T08:00:00Z", "100"),
            wd("X2", "2026-09-20T08:00:00Z", "100")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS, valuation="2026-10-01")
    ref(ctx, "1000", "2026-09-05")
    ref(ctx, "1000", "2026-09-15")
    ref(ctx, "1000", "2026-09-25")
    t = trace(ctx)
    assert [p.diff for p in t.points] == [D("0"), D("-100"), D("0")]
    assert [s.kind for s in t.segments] == ["ok", "erstmals", "verschwindet"]
    assert W.KIND_LABEL["verschwindet"] in t.summary and W.KIND_LABEL["erstmals"] in t.summary


def test_04_offsetting_errors_are_flagged_instead_of_claiming_no_error(cfg):
    """Doppelte Einzahlung (+100) und doppelte Auszahlung (−100) im selben Fenster: Differenz bleibt 0 – Portfolia
    weist darauf hin, dass sich Fehler aufheben können."""
    rows = [dep("D1", "2026-09-01T08:00:00Z", "1000"),
            koinly(dep("K2", "2026-09-10T09:00:00Z", "100")), api(dep("A2", "2026-09-10T09:03:00Z", "100"), "u2"),
            koinly(wd("K3", "2026-09-12T09:00:00Z", "100")), api(wd("A3", "2026-09-12T09:03:00Z", "100"), "u3")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS, valuation="2026-10-01")
    ref(ctx, "1000", "2026-09-05")
    ref(ctx, "1000", "2026-09-20")
    t = trace(ctx)
    assert [p.diff for p in t.points] == [0, 0] and [s.kind for s in t.segments] == ["ok", "ok"]
    seg = t.segments[1]
    assert any("gegenläufig" in f for f in seg.flags)
    assert {c.tx_ids[0] for c in seg.candidates} == {"K2", "K3"}
    assert {(c.ledger_effect > 0) - (c.ledger_effect < 0) for c in seg.candidates} == {1, -1}


def test_05_references_in_different_time_zones_use_the_exact_instant(cfg):
    """10:00 Europe/Berlin (= 08:00 UTC im Sommer) und 09:00 UTC: Buchung um 08:30 UTC liegt dazwischen."""
    rows = [dep("D1", "2026-09-01T08:00:00Z", "1000"), dep("D2", "2026-09-15T08:30:00Z", "50")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS, valuation="2026-10-01")
    ref(ctx, "1000", "2026-09-15", "10:00", "Europe/Berlin")  # 08:00 UTC
    ref(ctx, "1050", "2026-09-15", "09:00", "UTC")
    t = trace(ctx)
    assert [p.at.hour for p in t.points] == [8, 9]  # nach Zeitpunkt geordnet, nicht nach Eingabe
    assert [p.diff for p in t.points] == [0, 0] and t.points[0].ledger == 1000 and t.points[1].ledger == 1050
    assert t.segments[1].tx_ids == ["D2"]


def test_06_multiple_references_on_the_same_day_are_separate_points(cfg):
    rows = [dep("D1", "2026-09-01T08:00:00Z", "1000"), dep("D2", "2026-09-15T10:00:00Z", "10"),
            dep("D3", "2026-09-15T16:00:00Z", "10")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS, valuation="2026-10-01")
    ref(ctx, "1000", "2026-09-15", "09:00")
    ref(ctx, "1010", "2026-09-15", "12:00")
    ref(ctx, "1030", "2026-09-15", "20:00")  # D3 (+10) fehlt im Beleg nicht – Referenz kennt eine Einzahlung mehr
    t = trace(ctx)
    assert len(t.points) == 3 and [p.ledger for p in t.points] == [1000, 1010, 1020]
    assert [p.diff for p in t.points] == [0, 0, 10]
    assert [s.kind for s in t.segments] == ["ok", "ok", "erstmals"] and t.segments[2].tx_ids == ["D3"]


def test_07_contradicting_references_at_the_same_instant_derive_no_window(cfg):
    ctx = make_ctx(cfg, base_rows(), AUDIT_ASSETS, valuation="2026-10-01")
    ref(ctx, "1200", "2026-09-20")
    ref(ctx, "1300", "2026-09-20")  # gleicher Tag, gleiche Uhrzeit, anderer Bestand
    t = trace(ctx)
    assert all(p.conflict for p in t.points) and not t.segments
    assert any("Widersprüchliche Referenzbestände" in m for m in t.contradictions)
    assert "Widersprüchliche Referenzbestände" in t.summary


def test_08_single_reference_cannot_localise_the_period(cfg):
    ctx = make_ctx(cfg, base_rows(), AUDIT_ASSETS, valuation="2026-10-01")
    ref(ctx, "1000", "2026-09-30")
    t = trace(ctx)
    (seg,) = t.segments
    assert seg.kind == "nicht_eingrenzbar" and seg.start is None and seg.delta == D("-200")
    assert "Zeitraum nicht eingrenzbar: nur ein Referenzbestand" in t.summary
    assert t.n_unique_ref == 1


def test_legacy_reference_without_time_uses_the_local_booking_date(cfg):
    rows = [dep("D1", "2026-09-01T08:00:00Z", "1000"), dep("D2", "2026-09-02T08:00:00Z", "5")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS, valuation="2026-10-01")
    ctx.db.x("INSERT INTO reference_balance(account, asset_id, qty, as_of, source, status, created_at) VALUES "
             "(?, 'EUR', '1000', '2026-09-01', 'statement', 'active', '2026-09-03T00:00:00Z')", (ACC,))
    t = trace(ctx)
    (p,) = t.points
    assert p.basis == "datum" and p.diff == 0 and "ohne Uhrzeit" in p.basis_label
    assert t.since_last == 1  # D2 liegt nach dem Referenztag


def test_candidate_hypotheses_never_change_data_and_run_read_only(cfg):
    rows = [*base_rows(), koinly(dep("K3", "2026-09-24T09:00:00Z", "2000")),
            api(dep("A3", "2026-09-24T09:03:00Z", "2000"), "u3")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS, valuation="2026-10-01")
    ref(ctx, "1200", "2026-09-16")
    ref(ctx, "3200", "2026-09-30")
    before = fingerprint(ctx.db)
    r1, r2 = report_for(ctx), report_for(ctx)
    assert fingerprint(ctx.db) == before  # 31: rein lesend
    sig = lambda r: {k: (t.summary, [(s.kind, s.delta, s.tx_ids, [c.key for c in s.candidates]) for s in t.segments])  # noqa: E731
                     for k, t in r.traces.items()}
    assert sig(r1) == sig(r2)  # 34: deterministisch
    assert r1.signature() == r2.signature()


def test_large_history_stays_fast(cfg):
    """35: 3.000 Buchungen, 6 Referenzen – Präfixsummen statt wiederholter Durchläufe."""
    rows = [dep(f"T{n:05d}", f"2025-{1 + n % 12:02d}-{1 + n % 27:02d}T{n % 24:02d}:00:00Z", "1") for n in range(3000)]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS, valuation="2026-10-01")
    for n, day in enumerate(("2025-02-01", "2025-04-01", "2025-06-01", "2025-08-01", "2025-10-01", "2025-12-01")):
        ref(ctx, str(n), day)
    t0 = time.monotonic()
    rep = report_for(ctx)
    took = time.monotonic() - t0
    t = rep.traces[(ACC, "EUR")]
    assert len(t.points) == 6 and all(len(s.tx_ids) <= W.MAX_WINDOW_TXS for s in t.segments)
    assert sum(s.n_tx for s in t.segments) <= 3000 and took < 20
    assert datetime.now(UTC) > datetime(2026, 1, 1, tzinfo=UTC)


def test_page_renders_chart_data_windows_candidates_and_changes_nothing(cfg):
    from fastapi.testclient import TestClient

    from app.main import build_app

    rows = [*base_rows(),
            koinly(dep("K3", "2026-09-24T09:00:00Z", "2000")), api(dep("A3", "2026-09-24T09:03:00Z", "2000"), "u3")]
    ctx = make_ctx(cfg, rows, AUDIT_ASSETS, valuation="2026-10-01")
    ref(ctx, "1200", "2026-09-16")
    ref(ctx, "3200", "2026-09-30")
    with TestClient(build_app(cfg, start_scheduler=False)) as c:
        db = c.app.state.ctx.db
        before = fingerprint(db)
        page = c.get("/quality/diagnose/windows?all=1").text
        assert 'data-chart="devwindows"' in page and 'id="wd-1"' in page
        assert W.KIND_LABEL["erstmals"] in page and "Stark gestützt" in page
        assert "Nettoveränderung" in page and "keine</em> durchgehende Linie" in page
        assert "ändert keine Daten" in page or "Nichts hier verändert" in page
        assert 'id="win-' in page and "K3" in page and "A3" in page
        # Verknüpfung aus der Bestandsabweichungs-Tabelle bzw. Filter ohne Treffer
        assert "Keine Positionen" in c.get("/quality/diagnose/windows?acc=gibt-es-nicht").text
        assert fingerprint(db) == before  # Seitenaufruf ändert nichts
