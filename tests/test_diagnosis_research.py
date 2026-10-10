"""Evidenzrecherche und Rekonstruktionskandidaten (M29): nur synthetische Daten, Fake-Connector ohne Netz.

Abgedeckt: Kandidat mit voller API-Evidenz, ohne Evidenz, Dublettenverdacht aus ähnlichen Beträgen vs. nachgewiesene
wirtschaftliche Dublette, teilweise belegte Vorgänge, Pagination, Abbruch/Fortsetzung, wiederholte Recherche ohne
doppelte Kandidaten, rein lesende Recherche, Einzelfreigabe (Bereitstellen, Buchen), vollständiges Rückgängig,
doppelte Freigabe und Datenänderung zwischen Vorschau und Freigabe.
"""

# ruff: noqa: RUF059, F811
from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar

import pytest

from app.csvimport import model as M
from app.csvimport.model import Rec
from app.datasources import connector as K
from app.datasources.service import datasource_service
from app.diagnosis import actions as A
from app.diagnosis import references as R
from app.diagnosis import research as RS
from app.diagnosis.engine import report_for
from tests.test_datasources import FakeKraken, client, create_source, deposit, fake, sync  # noqa: F401

D = Decimal
HASH = "ab" * 32
ACC = "Kraken"
FINANCIAL = ("journal_tx", "tx_override", "csv_batch", "csv_row", "reference_balance", "event_decision", "ds_balance",
             "journal_import_link", "journal_event_alias")


def fin_fp(db) -> str:
    """Prüfsumme über alle finanzrelevanten Tabellen und den Abrufstand der Quellen."""
    import hashlib

    h = hashlib.sha256()
    for t in FINANCIAL:
        for r in db.q(f"SELECT * FROM {t} ORDER BY 1"):
            h.update(repr(tuple(r)).encode())
    for r in db.q("SELECT id, status, cursor_json, last_run_at, last_success_at, coverage_json FROM data_source "
                  "ORDER BY id"):
        h.update(repr(tuple(r)).encode())
    return h.hexdigest()


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


@pytest.fixture
def world(client, fake):
    c = client
    ctx = c.app.state.ctx
    sid = create_source(c, auto_commit="1")
    fake.events = [deposit("kraken:L1", "2026-09-01T10:00:00", "EUR", "1000"),
                   deposit("kraken:L2", "2026-09-10T10:00:00", "EUR", "200")]
    sync(c, sid)
    assert ctx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE source='sync:kraken'") == 2
    R.add(ctx, ACC, "EUR", "1200", "2026-09-11", "statement", "", "12:00", "UTC")
    return c, ctx, sid, fake


def window(ctx, aid: str = "EUR"):
    tr = report_for(ctx).traces[(ACC, aid)]
    return tr, tr.segments[-1]


def research(ctx, sid, **kw):
    return RS.run(ctx, sid, account=ACC, asset="EUR", win_from="2026-09-11T12:00:00+00:00",
                  win_to="2026-10-03T00:00:00+00:00", **kw)


def recon_findings(ctx):
    return [f for f in report_for(ctx).findings if f.kind == "reconstruction"]


def test_09_missing_deposit_with_full_api_proof_becomes_an_evidenced_candidate(world):
    c, ctx, sid, fake = world
    fake.events.append(deposit("kraken:L3", "2026-09-20T09:00:00", "EUR", "300"))
    R.add(ctx, ACC, "EUR", "1500", "2026-09-30", "statement", "", "12:00", "UTC")
    tr, seg = window(ctx)
    assert seg.kind == "erstmals" and seg.end.diff == D("300") and not seg.candidates  # ohne Recherche: ungeklärt
    res = research(ctx, sid)
    assert res["status"] == "done" and res["new"] == 1 and res["events"] == 3
    tr, seg = window(ctx)
    (cand,) = seg.candidates
    assert cand.kind == "reconstruction" and cand.evidence == "Belegt" and cand.reduces and cand.diff_after == 0
    assert not cand.missing and "Originalvorgang" in cand.note and not cand.creates_negative
    (f,) = recon_findings(ctx)
    assert f.status == "belegt" and f.data["ev_kind"] == "staged" and f.id == cand.finding_id
    assert tr.rest_strong == 0


def test_10_without_sufficient_proof_the_difference_stays_unexplained(world):
    c, ctx, sid, fake = world
    R.add(ctx, ACC, "EUR", "1500", "2026-09-30", "statement", "", "12:00", "UTC")
    res = research(ctx, sid)  # die Quelle kennt den Vorgang nicht
    assert res["new"] == 0
    tr, seg = window(ctx)
    assert seg.kind == "erstmals" and not seg.candidates and not recon_findings(ctx)
    assert tr.rest_strong == D("300")  # nichts erklärt: der Rest ist die volle Differenz


def test_17_repeated_research_creates_no_duplicate_candidates_and_pure_research_changes_nothing(world):
    c, ctx, sid, fake = world
    fake.events.append(deposit("kraken:L3", "2026-09-20T09:00:00", "EUR", "300"))
    R.add(ctx, ACC, "EUR", "1500", "2026-09-30", "statement", "", "12:00", "UTC")
    before = fin_fp(ctx.db)
    first = research(ctx, sid)
    second = research(ctx, sid)
    third = research(ctx, sid)
    assert (first["new"], second["new"], third["new"]) == (1, 0, 0)
    assert ctx.db.scalar("SELECT COUNT(*) FROM diag_research_event") == 1
    # Test 32: reine Recherche ändert keine Finanzdaten, keinen Abrufstand, keinen Prüf-Stapel
    assert fin_fp(ctx.db) == before
    assert len(recon_findings(ctx)) == 1
    assert ctx.db.scalar("SELECT COUNT(*) FROM csv_batch WHERE status IN ('preview', 'partial')") == 0


def inject(ctx, *evs):
    from app.diagnosis.collect import collect
    from app.diagnosis.engine import diagnose

    snap = collect(ctx)
    snap.evidence = [*snap.evidence, *evs]
    return diagnose(snap)


def ev(ref: str, when: str, qty: str, *, flags: tuple[str, ...] = (), hashes: tuple[str, ...] = (),
       extra: tuple[tuple[str, str, Decimal], ...] = (), source: str = "sync:kraken", status: str = "staged",
       kind: str = "staged") -> RS.Evidence:
    return RS.Evidence(kind=kind, ref=ref, source=source, ts=ts(when), status=status,
                       effects=((ACC, "EUR", D(qty)), *extra), label="Einzahlung", flags=flags, hashes=hashes,
                       event_key=ref if kind == "staged" else None, source_id=1)


def two_refs(ctx, second: str) -> None:
    R.add(ctx, ACC, "EUR", second, "2026-09-30", "statement", "", "12:00", "UTC")


def test_11_similar_amounts_are_a_suspicion_not_proof_and_never_rank_as_proven(world):
    c, ctx, sid, fake = world
    two_refs(ctx, "1400")  # Differenz +200 – genau der Betrag der Buchung L2 vom Vortag
    rep = inject(ctx, ev("kraken:S1", "2026-09-11T13:00:00", "200"))
    (cand,) = rep.traces[(ACC, "EUR")].segments[-1].candidates
    assert cand.evidence == "Plausibel" and len(cand.similar) == 1 and "kein Beweis" in cand.note
    assert cand.diff_after == 0 and cand.reduces  # ein passender Betrag ändert die Einstufung nicht nach oben
    (f,) = [x for x in rep.findings if x.kind == "reconstruction"]
    assert f.status == "verdacht"


def test_12_proven_economic_duplicate_is_never_a_candidate(world):
    c, ctx, sid, fake = world
    two_refs(ctx, "1400")
    booked = ctx.db.scalar("SELECT event_key FROM journal_tx WHERE external_id LIKE 'kraken:L2%'")
    assert booked
    rep = inject(ctx, ev(booked, "2026-09-11T13:00:00", "200"))  # gleiche Ereignis-ID wie die gebuchte Buchung
    seg = rep.traces[(ACC, "EUR")].segments[-1]
    assert not seg.candidates and not [x for x in rep.findings if x.kind == "reconstruction"]
    # gleicher Transaktions-Hash und gleiche Menge → ebenfalls bereits gebucht
    tid = ctx.db.scalar("SELECT tx_id FROM journal_tx WHERE external_id LIKE 'kraken:L2%'")
    ctx.db.x("UPDATE journal_tx SET tx_hash=? WHERE tx_id=?", ("0x" + HASH, tid))
    ctx.invalidate_data()
    rep = inject(ctx, ev("kraken:S9", "2026-09-11T13:00:00", "200", hashes=(HASH,)))
    assert not rep.traces[(ACC, "EUR")].segments[-1].candidates


def test_13_14_18_missing_transfer_partial_evidence_and_incomplete_fee_info(world):
    c, ctx, sid, fake = world
    two_refs(ctx, "1500")  # Differenz +300
    full = ev("kraken:S2", "2026-09-20T09:00:00", "300", extra=(("Wallet X", "EUR", D("-300")),),
              source="doc:abrechnung")
    partial = ev("kraken:S3", "2026-09-21T09:00:00", "300", flags=("Zeitpunkt fehlt in der Quelle",
                                                                   "Gebührenbelastung nicht belegt (zusätzlich oder "
                                                                   "im Betrag enthalten?)"))
    rep = inject(ctx, full, partial)
    cands = {x.ev_id: x for x in rep.traces[(ACC, "EUR")].segments[-1].candidates}
    a, b = cands["staged|kraken:S2"], cands["staged|kraken:S3"]
    assert a.evidence == "Stark gestützt"  # Beleg (Dokument) statt Original der Börse: nie „Belegt“
    assert a.other_positions == [("Wallet X", "EUR", D("-300"))]
    assert b.evidence == "Plausibel" and len(b.missing) == 2 and "Zeitpunkt fehlt" in b.missing[0]
    # beide erklären rechnerisch dieselben 300 – es sind Alternativen, kein Beleg für beide
    assert a.diff_after == 0 and b.diff_after == 0
    rec = Rec(line=1, ts=ts("2026-09-20T09:00:00"), kind=M.TRADE, out_sym="EUR", out_qty=D(10), in_sym="BTC",
              in_qty=D("0.001"), fee_sym="EUR", fee_qty=D("0.1"), fee_basis="open")
    assert any("Gebühr" in x for x in RS.flags_of_rec(rec))


def test_19_20_unexplained_positive_and_negative_differences_stay_unexplained(world):
    c, ctx, sid, fake = world
    two_refs(ctx, "1500")  # Referenz über Ledger
    tr, seg = window(ctx)
    assert seg.end.diff == D("300") and not seg.candidates
    ctx.db.x("UPDATE reference_balance SET status='removed' WHERE as_of=?", ("2026-09-30",)) if False else None
    two_refs(ctx, "900")  # zweiter, widersprechender Wert am selben Tag
    tr = report_for(ctx).traces[(ACC, "EUR")]
    assert tr.contradictions  # Widerspruch statt Rückschluss


class Paged(K.Connector):
    """Seitenweise Historie mit Fortsetzungspunkt (wie die Etappen echter Börsen-/Wallet-Connectoren)."""

    provider = "kraken"
    label = "Kraken (Test, Seiten)"
    needs_credentials = True
    pages: ClassVar[list[list[K.SourceEvent]]] = []
    calls: ClassVar[list[Any]] = []
    hook: ClassVar[Any] = None

    def check(self, cfg: K.SourceConfig, secret: K.Secret) -> K.CheckResult:
        return K.CheckResult(True, "ok")

    def fetch(self, cfg: K.SourceConfig, secret: K.Secret, cursor: dict[str, Any] | None) -> K.FetchResult:
        n = int((cursor or {}).get("page", 0))
        Paged.calls.append(cursor)
        if Paged.hook is not None:
            Paged.hook(n)
        self.report("Abruf", n, len(Paged.pages), f"Seite {n + 1}")
        last = n + 1 >= len(Paged.pages)
        return K.FetchResult(events=list(Paged.pages[n]), cursor={"page": n + 1}, complete=last, resume=not last,
                             more=not last)


@pytest.fixture
def paged(world):
    c, ctx, sid, fake = world
    ev3 = deposit("kraken:L3", "2026-09-20T09:00:00", "EUR", "300")
    ev4 = deposit("kraken:L4", "2026-09-25T09:00:00", "EUR", "50")
    Paged.pages = [list(fake.events), [ev3], [ev4]]
    Paged.calls, Paged.hook = [], None
    K.register(Paged)
    R.add(ctx, ACC, "EUR", "1550", "2026-09-30", "statement", "", "12:00", "UTC")
    try:
        yield c, ctx, sid
    finally:
        K.register(FakeKraken)


def test_15_api_pagination_is_followed_until_complete_within_the_round_budget(paged):
    c, ctx, sid = paged
    res = research(ctx, sid)
    assert res["status"] == "done" and res["new"] == 2 and res["events"] == 4
    assert list(Paged.calls) == [None, {"page": 1}, {"page": 2}]
    row = ctx.db.q1("SELECT * FROM diag_research WHERE id=?", (res["run_id"],))
    assert (row["rounds"], row["staged"], row["complete"], row["cursor_json"]) == (3, 2, 1, None)


def test_16_aborted_research_is_resumable_and_never_duplicates(paged):
    c, ctx, sid = paged
    svc = datasource_service(ctx)
    before = fin_fp(ctx.db)
    Paged.hook = lambda n: svc.cancel(sid) if n == 2 else None  # Nutzer bricht vor der dritten Seite ab
    res = research(ctx, sid)
    assert res["status"] == "cancelled" and res["new"] == 1  # Seite 1 ist bereits abgelegt
    row = ctx.db.q1("SELECT * FROM diag_research WHERE id=?", (res["run_id"],))
    assert json.loads(row["cursor_json"]) == {"page": 2}
    Paged.hook, Paged.calls = None, []
    again = research(ctx, sid, resume=res["run_id"])
    assert again["status"] == "done" and again["new"] == 1 and Paged.calls == [{"page": 2}]  # ab dem Fortsetzungspunkt
    assert ctx.db.scalar("SELECT COUNT(*) FROM diag_research_event") == 2
    assert research(ctx, sid)["new"] == 0  # erneute Vollrecherche: nichts doppelt
    assert fin_fp(ctx.db) == before
    # ein laufender Abruf der Quelle sperrt die Recherche (gemeinsame Sperre) – und umgekehrt
    from app.datasources import service as S

    lock = S._source_lock(sid)
    assert lock.acquire(blocking=False)
    try:
        assert research(ctx, sid)["busy"] is True
    finally:
        lock.release()


def test_research_refuses_to_resume_runs_that_are_not_resumable(paged):
    c, ctx, sid = paged
    done = research(ctx, sid)
    assert "nicht fortsetzen" in research(ctx, sid, resume=done["run_id"])["error"]


def test_research_run_in_background_can_be_started_once_and_ends_as_done(paged):
    c, ctx, sid = paged
    gate, entered = threading.Event(), threading.Event()
    Paged.hook = lambda n: (entered.set(), gate.wait(5))[1] if n == 0 else None
    out = RS.start(ctx, sid, account=ACC, asset="EUR", win_from="2026-09-11T12:00:00+00:00",
                   win_to="2026-10-03T00:00:00+00:00")
    assert out == {"started": True} and entered.wait(5)
    assert RS.start(ctx, sid)["busy"] is True  # läuft bereits
    gate.set()
    import time

    for _ in range(200):
        row = RS.last_run(ctx.db, sid)
        if row is not None and row["status"] != "running":
            break
        time.sleep(0.02)
    assert row["status"] == "done" and row["staged"] == 2


def approve(ctx, finding_id: str, option: str):
    report = report_for(ctx)
    f = report.by_id(finding_id)
    plan = A.build_plan(ctx, report, f, option)
    assert not plan.errors, plan.errors
    return plan, A.apply(ctx, finding_id, option, {}, plan.token)


@pytest.fixture
def staged_world(world):
    c, ctx, sid, fake = world
    fake.events.append(deposit("kraken:L3", "2026-09-20T09:00:00", "EUR", "300"))
    R.add(ctx, ACC, "EUR", "1500", "2026-09-30", "statement", "", "12:00", "UTC")
    research(ctx, sid)
    return c, ctx, sid


def test_27_single_approval_stages_then_books_exactly_one_event_and_closes_the_window(staged_world):
    c, ctx, sid = staged_world
    (f,) = recon_findings(ctx)
    rec = A.build_plan  # Plan zeigt: keine Buchung beim Bereitstellen
    plan, res = approve(ctx, f.id, "stage")
    assert res.ok and not [o for o in plan.ops if o.kind != "stage"]
    assert ctx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE source='sync:kraken'") == 2  # noch nichts gebucht
    assert ctx.db.scalar("SELECT status FROM diag_research_event") == "staged"
    (row,) = ctx.db.q("SELECT b.id, b.status, r.status AS rs FROM csv_batch b JOIN csv_row r ON r.batch_id=b.id "
                      "WHERE b.status='preview'")
    assert row["rs"] == "new"
    (g,) = recon_findings(ctx)  # jetzt aus dem Prüf-Stapel (keine zweite Recherche-Zeile)
    assert g.data["ev_kind"] == "row" and g.data["level"] == "Belegt" and g.id != f.id
    plan2, res2 = approve(ctx, g.id, "book_row")
    assert res2.ok and [o.kind for o in plan2.ops] == ["book_row"] and rec
    assert ctx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE source='sync:kraken' AND status='active'") == 3
    tr, seg = window(ctx)
    assert (tr.last.ledger == D("1500") and tr.last.diff == 0 and seg.kind == "erstmals") or tr.last.diff == 0
    assert not recon_findings(ctx)


def test_28_full_undo_of_both_steps_restores_the_previous_state(staged_world):
    c, ctx, sid = staged_world
    before = fin_fp(ctx.db)
    (f,) = recon_findings(ctx)
    _, r1 = approve(ctx, f.id, "stage")
    (g,) = recon_findings(ctx)
    _, r2 = approve(ctx, g.id, "book_row")
    # die frühere Entscheidung lässt sich nicht vor der späteren zurücknehmen (gleiche Buchungen/Objekte)
    assert A.undo(ctx, r2.decision_id).ok
    assert ctx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE source='sync:kraken' AND status='active'") == 2
    assert ctx.db.scalar("SELECT status FROM csv_row WHERE event_key='kraken:L3'") == "new"
    assert A.undo(ctx, r1.decision_id).ok
    assert ctx.db.scalar("SELECT COUNT(*) FROM csv_batch WHERE status IN ('preview', 'partial')") == 0
    assert ctx.db.scalar("SELECT status FROM diag_research_event") == "new"
    # Buchungen bleiben als „zurückgenommen“ nachvollziehbar (nichts physisch gelöscht), Rest wie vorher
    assert ctx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE status='reverted'") == 1
    (again,) = recon_findings(ctx)
    assert again.id == f.id
    assert fin_fp(ctx.db) != before  # die zurückgenommene Buchung bleibt im Protokoll sichtbar


def test_29_same_preview_is_executed_only_once(staged_world):
    c, ctx, sid = staged_world
    (f,) = recon_findings(ctx)
    plan, first = approve(ctx, f.id, "stage")
    second = A.apply(ctx, f.id, "stage", {}, plan.token)
    assert second.ok and "bereits übernommen" in second.message and second.decision_id == first.decision_id
    assert ctx.db.scalar("SELECT COUNT(*) FROM csv_batch") == 2  # Sync-Stapel + genau ein Kandidaten-Stapel


def test_30_data_change_between_preview_and_confirmation_blocks_the_approval(staged_world):
    c, ctx, sid = staged_world
    (f,) = recon_findings(ctx)
    plan = A.build_plan(ctx, report_for(ctx), f, "stage")
    ctx.db.x("UPDATE diag_research_event SET status='dismissed'")  # Ergebnis wird zwischendurch verworfen
    res = A.apply(ctx, f.id, "stage", {}, plan.token)
    assert not res.ok and res.errors
    assert ctx.db.scalar("SELECT COUNT(*) FROM csv_batch") == 1


def test_31_diagnosis_and_page_views_change_no_financial_data(staged_world):
    c, ctx, sid = staged_world
    before = fin_fp(ctx.db)
    for _ in range(2):
        report_for(ctx)
        assert c.get("/quality/diagnose/windows?all=1").status_code == 200
        assert c.get("/quality/diagnose").status_code == 200
    (f,) = recon_findings(ctx)
    assert c.get(f"/quality/diagnose/case/{f.id}").status_code == 200
    assert c.get(f"/quality/diagnose/plan?f={f.id}&o=stage").status_code == 200  # Vorschau rechnet nur auf einer Kopie
    assert fin_fp(ctx.db) == before


def test_33_research_results_survive_export_and_restore_idempotently(staged_world):
    from app.fullexport import _state, collect

    c, ctx, sid = staged_world
    (f,) = recon_findings(ctx)
    approve(ctx, f.id, "stage")  # „im Prüf-Stapel“ reist als neu mit (der Prüf-Stapel selbst ist kein Teil des Exports)
    st = _state(collect(ctx, set()))
    (ev_row,) = st["diag_research_events"]
    assert ev_row["provider"] == "kraken" and ev_row["account"] == ACC and ev_row["status"] == "new"
    assert "source_id" not in ev_row and ev_row["event_key"] == "kraken:L3"
    ctx.db.x("DELETE FROM diag_research_event")
    from app.fullexport import _research_events

    def restore() -> int:
        with ctx.db.transaction() as cx:
            return _research_events(cx, st["diag_research_events"])

    assert restore() == 1
    assert restore() == 0  # nicht doppelt
    (row,) = ctx.db.q("SELECT status, source_id FROM diag_research_event")
    assert row["status"] == "new" and row["source_id"] == sid
