"""M29-Hardening (Financial Integrity, Phase 3) – nur synthetische Daten.

Abgedeckt: Positionen ohne Ledger (Referenz bzw. belastbarer externer Bestand) in der Bestandsklärung, bestätigt vs.
fortgeschrieben, Teilbewertung ohne 0-EUR-Annahme, getrennte Quellen- und Abgleichsevidenz, Mehrbein-Vorgang nur
gemeinsam buchbar (und gemeinsam rückgängig), keine Trennung eines teilweise entschiedenen Vorgangs.
"""

# ruff: noqa: RUF059, F811
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.csvimport import model as M
from app.csvimport.model import Rec
from app.datasources import connector as K
from app.diagnosis import actions as A
from app.diagnosis import references as R
from app.diagnosis import research as RS
from app.diagnosis import stock as ST
from app.diagnosis.engine import report_for
from app.diagnosis.model import HoldingRow
from app.journal.service import journal_service
from tests.test_csvimport import post
from tests.test_datasources import client, create_source, deposit, fake, sync  # noqa: F401
from tests.test_diagnosis_research import ACC, approve, fin_fp, recon_findings, research, ts, world  # noqa: F401

D = Decimal


def token(ctx, aid: str = "TOKEN") -> None:
    assert not journal_service(ctx).save_asset({"asset_id": aid, "name": f"Test {aid}", "asset_class": "crypto",
                                                "quote_source": "none"}).errors


# ----------------------------------------------------------------------------------------------------
# Bestandsklärung: Positionen ohne Ledger, Bestätigungsart, Teilbewertung
# ----------------------------------------------------------------------------------------------------

def test_reference_without_any_booking_appears_unvalued_not_zero(world):
    """500 TOKEN laut Kontoauszug, nie gebucht: erscheint mit Ledger 0, Differenz 500, Wert „unvollständig“."""
    c, ctx, sid, fake = world
    token(ctx)
    assert R.add(ctx, ACC, "TOKEN", "500", "2026-09-30", "statement", "", "12:00", "UTC").ok
    before = fin_fp(ctx.db)
    sv = ST.build(report_for(ctx), A.decisions(ctx.db, 100))
    row = next(r for r in sv.rows if (r.account, r.asset) == (ACC, "TOKEN"))
    assert row.no_ledger and row.ledger == 0 and row.diff == D(500) and row.rolled == D(500)
    assert row.confirm_kind == "Referenzbestand" and row.state == "bestätigt"
    assert row.view_value is None and row.ledger_value is None and "nicht bewertbar" in row.value_note
    assert f"{ACC} · TOKEN" in sv.unvalued and sv.n_no_ledger == 1 and sv.n_valued < sv.n_positions
    page = c.get("/quality/diagnose/stock?view=confirmed").text
    assert "ohne Buchung" in page and "Teilbewertung" in page and "unvollständig" in page
    assert fin_fp(ctx.db) == before  # rein berechnet


def test_rolled_forward_vs_confirmed_reference(world):
    """Bestätigt nur ohne spätere Buchungen; mit späteren Buchungen „fortgeschrieben“ (B' = Referenz + Wirkung)."""
    c, ctx, sid, fake = world  # Referenz 1200 EUR am 11.09., beide Einzahlungen davor
    row = next(r for r in ST.build(report_for(ctx), []).rows if (r.account, r.asset) == (ACC, "EUR"))
    assert row.state == "bestätigt" and row.n_after == 0 and row.rolled == D(1200) and row.diff == 0
    fake.events.append(deposit("kraken:L5", "2026-09-20T09:00:00", "EUR", "50"))
    sync(c, sid)  # automatische Übernahme (auto_commit) → Buchung nach dem Nachweis
    row = next(r for r in ST.build(report_for(ctx), []).rows if (r.account, r.asset) == (ACC, "EUR"))
    assert row.state == "fortgeschrieben" and row.n_after == 1 and row.rolled == D(1250) and row.diff == 0


def test_unique_external_balance_without_ledger_is_a_position(world):
    """Belastbarer externer Bestand (aktuell, vollständig) ohne Buchung und ohne Referenz erscheint als Position."""
    c, ctx, sid, fake = world
    token(ctx, "TOKX")
    report = report_for(ctx)
    now = datetime.now(UTC)
    ext = HoldingRow(account=ACC, asset="TOKX", name="Test TOKX", computed=D(0), observed=D("7.5"),
                     observed_at=now - timedelta(hours=1), observed_by="Kraken (Test)", status="extern_diff",
                     computed_at_obs=D(0))
    unsure = dataclasses.replace(ext, asset="TOKY", status="extern_unsicher")
    report = dataclasses.replace(report, holdings=[*report.holdings, ext, unsure])
    sv = ST.build(report, [])
    row = next(r for r in sv.rows if r.asset == "TOKX")
    assert row.no_ledger and row.confirm_kind == "Datenquelle (Abruf)" and row.diff == D("7.5")
    assert row.state == "bestätigt" and row.view_value is None  # ohne Kurs: unvollständig, nicht 0
    assert not [r for r in sv.rows if r.asset == "TOKY"]  # nicht bestätigter Abruf ist keine Bestätigung


# ----------------------------------------------------------------------------------------------------
# Evidenz: Quelle und Abgleich getrennt
# ----------------------------------------------------------------------------------------------------

def _ev(**kw) -> RS.Evidence:
    base = {"kind": "row", "ref": "1:0", "source": "sync:kraken", "ts": ts("2026-09-20T09:00:00"), "status": "new",
            "effects": ((ACC, "EUR", D(300)),)}
    return RS.Evidence(**{**base, **kw})


def test_source_and_reconciliation_evidence_are_separate():
    pos = (ACC, "EUR")
    empty = RS.PositionBookings([])
    similar = RS.PositionBookings([(ts("2026-09-20T10:00:00"), None, D(300), "T-1")])
    a = RS.assess(_ev(), pos, empty, set(), {})
    assert (a.level, a.source_level, a.recon_level) == ("Belegt", "Originalbeleg vollständig",
                                                        "kein Gegenstück gebucht")
    b = RS.assess(_ev(), pos, similar, set(), {})
    # gleiche Quellenevidenz, aber Dublettenverdacht im Abgleich → Gesamtstufe sinkt, Quellenstufe nicht
    assert (b.level, b.source_level, b.recon_level) == ("Plausibel", "Originalbeleg vollständig",
                                                        "ähnliche Buchung vorhanden (Verdacht)")
    c = RS.assess(_ev(source="csv:koinly", flags=("Gebühr fehlt",)), pos, empty, set(), {})
    assert (c.level, c.source_level) == ("Plausibel", "teilweise belegt")
    d = RS.assess(_ev(status="duplicate"), pos, empty, set(), {})
    assert (d.level, d.source_level, d.recon_level) == ("Widersprüchlich", "Originalbeleg vollständig",
                                                        "laut Importprüfung bereits gebucht")
    e = RS.assess(_ev(event_key="kraken:X"), pos, empty, {"kraken:X"}, {})
    assert e.booked and e.recon_level == "bereits gebucht" and e.source_level == "Originalbeleg vollständig"


# ----------------------------------------------------------------------------------------------------
# Mehrbein-Vorgang: nur gemeinsam buchen, gemeinsam zurücknehmen
# ----------------------------------------------------------------------------------------------------

def deposit_with_fee(key: str, when: str, qty: str, fee: str) -> K.SourceEvent:
    t = ts(when)
    return K.SourceEvent(key, t, [Rec(line=0, ts=t, kind=M.DEPOSIT, in_sym="EUR", in_qty=D(qty), label="deposit"),
                                  Rec(line=0, ts=t, kind=M.FEE, fee_sym="EUR", fee_qty=D(fee), label="deposit")])


def staged_multi(world):
    c, ctx, sid, fake = world
    fake.events.append(deposit_with_fee("kraken:L4", "2026-09-20T09:00:00", "300", "0.25"))
    assert R.add(ctx, ACC, "EUR", "1499.75", "2026-09-30", "statement", "", "12:00", "UTC").ok
    research(ctx, sid)
    (f,) = recon_findings(ctx)
    plan, res = approve(ctx, f.id, "stage")
    assert res.ok
    (g,) = recon_findings(ctx)
    return c, ctx, g


def test_multi_leg_event_is_booked_and_undone_as_one(world):
    c, ctx, g = staged_multi(world)
    n0 = ctx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE status='active'")
    plan, res = approve(ctx, g.id, "book_row")
    assert res.ok and [o.kind for o in plan.ops] == ["book_row", "book_row"]
    assert any("gemeinsam gebucht" in w for w in plan.warnings)
    assert ctx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE status='active'") == n0 + 2
    assert not ctx.db.scalar("SELECT COUNT(*) FROM csv_row WHERE event_key='kraken:L4' AND status='new'")
    assert A.undo(ctx, res.decision_id).ok
    assert ctx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE status='active'") == n0
    assert ctx.db.scalar("SELECT COUNT(*) FROM csv_row WHERE event_key='kraken:L4' AND status='new'") == 2


def test_partially_decided_event_is_never_split(world):
    c, ctx, g = staged_multi(world)
    bid = ctx.db.scalar("SELECT batch_id FROM csv_row WHERE event_key='kraken:L4' LIMIT 1")
    fee_idx = ctx.db.scalar("SELECT idx FROM csv_row WHERE event_key='kraken:L4' ORDER BY idx DESC LIMIT 1")
    assert post(c, f"/journal/csv/{bid}/rows", **{f"dec_{fee_idx}": "skip"}).status_code == 303
    n0 = ctx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE status='active'")
    report = report_for(ctx)
    f = report.by_id(g.id) or next(x for x in report.findings if x.kind == "reconstruction")
    plan = A.build_plan(ctx, report, f, "book_row")
    assert plan.errors and not plan.ops
    assert ctx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE status='active'") == n0
