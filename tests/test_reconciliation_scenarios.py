"""M24/AP3.11 – Mehrquellen-Szenarien des Abgleichs mit unabhängig festgelegten Erwartungen.

Je Szenario stehen erwartete Buchungen, Bestände, Lots und Kostenbasis **vorab** fest (aus den Eingabedaten
nachgerechnet, nicht aus dem Code abgelesen). Geprüft wird auf dem Weg, den der Nutzer geht: Befund → bevorzugte
Lösung → Vorschau → Übernehmen bzw. Prüf-Stapel → Verknüpfen. Synthetische Daten, temporäre Datenbanken.

Szenarien mit Prüf-Stapeln (CSV/API) stehen in ``test_reconciliation_pipeline.py``.
"""

from __future__ import annotations

import random
from datetime import date
from decimal import Decimal

import pytest

from app.diagnosis import actions as A
from app.diagnosis import bulk as B
from app.diagnosis import integrity as I
from app.diagnosis.engine import report_for
from tests.helpers import ASSETS, tx
from tests.test_diagnosis import asset, cfg, fingerprint, make_ctx  # noqa: F401

D = Decimal


def bal(ctx, acc: str, aid: str) -> Decimal:
    ctx.invalidate_data()
    return ctx.ledger().balances.get((acc, aid), D(0))


def lots(ctx, aid: str) -> list[tuple[str, Decimal, Decimal, date]]:
    ctx.invalidate_data()
    return sorted((lot.account, lot.qty, lot.cost.quantize(D("0.01")), lot.acq_date)
                  for lot in ctx.ledger().lots if lot.asset == aid and lot.qty > 0)


def finding(ctx, kind: str, *, contains: str = ""):
    rep = report_for(ctx)
    fs = [f for f in rep.findings if f.kind == kind and contains in f.title]
    assert fs, [f.title for f in rep.findings]
    return rep, fs[0]


def apply_preferred(ctx, f_id: str, params=None):
    rep = report_for(ctx)
    f = rep.by_id(f_id)
    from app.diagnosis.recommend import recommend

    opt = recommend(rep, f).primary
    assert opt is not None, "keine bevorzugte Lösung"
    plan = A.build_plan(ctx, rep, f, opt.key, params)
    assert not plan.errors, plan.errors
    res = A.apply(ctx, f.id, opt.key, plan.params, plan.token)
    assert res.ok, res.errors
    return res


FUND = tx("F0", "2025-01-02T08:00:00Z", "deposit", to=("Börse", "EUR", "10000"), value=10000)


# ----------------------------------------------------------------------------------------------------------------------
# 3 · Börsenabgang und Wallet-Zugang  /  7 · Teiltransfer mit Netzwerkgebühr  /  8 · nachträgliche Gegenbuchung
# ----------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("dep_qty", "dep_ts", "label"), [
    ("1", "2025-03-01T12:20:00Z", "3: gleicher Betrag"),
    ("0.999", "2025-03-01T12:20:00Z", "7: Netzwerkgebühr 0,001"),
    ("0.999", "2025-03-03T09:00:00Z", "8: Gegenbuchung zwei Tage später"),
])
def test_exchange_withdrawal_and_wallet_deposit_become_transfer(cfg, dep_qty, dep_ts, label):  # noqa: F811
    rows = [FUND,
            tx("B1", "2025-01-10T10:00:00Z", "buy", frm=("Börse", "EUR", "2000"), to=("Börse", "ETH", "1"),
               value=2000),
            tx("W1", "2025-03-01T12:00:00Z", "withdrawal", frm=("Börse", "ETH", "1"), value=3000),
            tx("D1", dep_ts, "deposit", to=("Wallet", "ETH", dep_qty), value=2997)]
    ctx = make_ctx(cfg, rows, ASSETS)
    # vorher: Abgang ohne Gegenbuchung + Neuanschaffung im Wallet (Haltedauer beginnt neu)
    assert lots(ctx, "ETH") == [("Wallet", D(dep_qty), D("2997.00"), date.fromisoformat(dep_ts[:10]))]
    _rep, f = finding(ctx, "transfer", contains="Börse → Wallet")
    apply_preferred(ctx, f.id)
    # Soll: Transfer – Anschaffung 10.01.2025 bleibt, Kosten 2.000 € anteilig, Differenz als Transfergebühr
    q = D(dep_qty)
    assert bal(ctx, "Börse", "ETH") == 0 and bal(ctx, "Wallet", "ETH") == q
    assert lots(ctx, "ETH") == [("Wallet", q, (D(2000) * q).quantize(D("0.01")), date(2025, 1, 10))]
    led = ctx.ledger()
    assert not [d for d in led.disposals if d.asset == "ETH" and d.kind in ("withdrawal", "sell")]
    fee = [d for d in led.disposals if d.asset == "ETH" and d.kind == "transfer_fee"]
    assert sum((d.qty for d in fee), D(0)) == D(1) - q


# ----------------------------------------------------------------------------------------------------------------------
# 5 · mehrere Token-Events einer Blockchain-Transaktion  /  18 · gleicher Hash auf verschiedenen Chains
# ----------------------------------------------------------------------------------------------------------------------

def test_multiple_events_of_one_transaction_are_not_duplicates(cfg):  # noqa: F811
    h = "ab" * 32
    rows = []
    for i in (0, 1):
        r = tx(f"L{i}", "2025-04-01T09:00:00Z", "deposit", to=("Wallet", "USDT", "50"), value=46)
        r["source"], r["source_ref"] = "portfolia:sync:ethereum", f"ethereum:0x{h}#t:f00d#{i}"
        rows.append(r)
    ctx = make_ctx(cfg, rows, [*ASSETS, asset("USDT")])
    assert not [f for f in report_for(ctx).findings if f.kind == "duplicate"]
    assert bal(ctx, "Wallet", "USDT") == D(100)  # beide Bewegungen zählen


def test_same_hash_on_different_chains_is_not_a_duplicate(cfg):  # noqa: F811
    h = "0x" + "cd" * 32
    a = tx("E1", "2025-05-01T10:00:00Z", "deposit", to=("Wallet ETH", "ETH", "1"), value=3000)
    b = tx("S1", "2025-05-01T10:00:00Z", "deposit", to=("Wallet BSC", "BNB", "1"), value=500)
    for r in (a, b):
        r["note"], r["source"] = f"txhash {h}", "koinly"
    ctx = make_ctx(cfg, [a, b], ASSETS)
    assert not [f for f in report_for(ctx).findings if f.kind == "duplicate"]
    assert bal(ctx, "Wallet ETH", "ETH") == 1 and bal(ctx, "Wallet BSC", "BNB") == 1


# ----------------------------------------------------------------------------------------------------------------------
# 6 · identische Beträge bei unterschiedlichen Trades
# ----------------------------------------------------------------------------------------------------------------------

def test_identical_amounts_at_different_times_are_independent(cfg):  # noqa: F811
    rows = [FUND, *(tx(f"K{i}", f"2025-02-0{i + 3}T10:00:00Z", "buy", frm=("Börse", "EUR", "500"),
                       to=("Börse", "BTC", "0.01"), value=500) for i in range(2))]
    ctx = make_ctx(cfg, rows, ASSETS)
    assert not [f for f in report_for(ctx).findings if f.kind == "duplicate"]
    assert lots(ctx, "BTC") == [("Börse", D("0.01"), D("500.00"), date(2025, 2, 3)),
                                ("Börse", D("0.01"), D("500.00"), date(2025, 2, 4))]


# ----------------------------------------------------------------------------------------------------------------------
# 13 · historische manuelle Doppelbuchung (Import + gleiche App-Buchung)
# ----------------------------------------------------------------------------------------------------------------------

def test_historic_manual_double_booking_keeps_import_acquisition(cfg):  # noqa: F811
    from app.journal.service import journal_service

    rows = [FUND, tx("I1", "2025-02-10T10:00:00Z", "buy", frm=("Börse", "EUR", "1200"), to=("Börse", "KAS", "10"),
                     value=1200)]
    ctx = make_ctx(cfg, rows, ASSETS)
    res = journal_service(ctx).save({"kind": "buy", "date": "2025-02-10", "time": "11:00", "account": "Börse",
                                     "asset": "KAS", "qty": "10", "amount": "1200", "ccy": "EUR"})
    assert not res.errors
    assert bal(ctx, "Börse", "KAS") == D(20)  # doppelt gezählt
    rep = report_for(ctx)
    f = next(x for x in rep.findings if x.kind == "duplicate" and {r.tx_id for r in x.txs} & {"I1"})
    apply_preferred(ctx, f.id)
    # Soll: Import-Buchung gilt (Anschaffung 10.02.2025, 1.200 €), App-Buchung zählt nicht mehr
    assert bal(ctx, "Börse", "KAS") == D(10)
    assert lots(ctx, "KAS") == [("Börse", D(10), D("1200.00"), date(2025, 2, 10))]
    assert bal(ctx, "Börse", "EUR") == D(8800)


# ----------------------------------------------------------------------------------------------------------------------
# 10 · bestätigte Nutzerentscheidung bleibt nach erneutem Import erhalten
# ----------------------------------------------------------------------------------------------------------------------

def _twin_rows():
    buy = tx("K1", "2025-01-05T10:00:00Z", "buy", frm=("Börse", "EUR", "100"), to=("Börse", "ETH", "0.05"),
             value=100)
    return [FUND, buy, {**buy, "tx_id": "K2"}]


def test_confirmed_independent_decision_survives_reimport(cfg):  # noqa: F811
    ctx = make_ctx(cfg, _twin_rows(), ASSETS)
    _rep, f = finding(ctx, "duplicate", contains="vollständig gleicher")
    assert A.dismiss(ctx, f.id, I.INDEPENDENT_NOTE).ok
    # gleiche Daten erneut importiert (neue Import-ID): Befund-ID und Prüfsumme unverändert → Entscheidung gilt
    from app.importer.loader import import_file
    from app.importer.zipbuilder import build_zip

    dst = cfg.import_dir / "erneut.zip"
    build_zip(dst, transactions=_twin_rows(), assets=ASSETS, generated_at="2025-06-30T21:00:00Z",
              valuation_date="2025-06-30", extra_tx_columns=["source", "source_ref", "flag", "note"])
    assert import_file(ctx.db, dst, ctx.engine_options()).status == "imported"
    ctx.invalidate_data()
    r = I.run(ctx)
    it = next(x for x in r.items if x.finding_id == f.id)
    assert it.status == "verworfen"
    rep = report_for(ctx)
    assert A.active_dismissals(ctx.db)[f.id].fingerprint == A.fingerprint(rep.by_id(f.id))  # nicht neu vorgeschlagen
    assert bal(ctx, "Börse", "ETH") == D("0.10")  # beide Käufe zählen (Nutzerentscheidung)


# ----------------------------------------------------------------------------------------------------------------------
# 14 · Sammelkorrektur mehrerer unabhängiger Dubletten  /  16 · Rückgängig  /  17 · Fehler mittendrin
# ----------------------------------------------------------------------------------------------------------------------

def _three_twins():
    rows = [FUND]
    for i, (aid, qty, eur) in enumerate((("BTC", "0.01", "600"), ("ETH", "0.5", "1500"), ("KAS", "5", "700"))):
        r = tx(f"A{i}", f"2025-02-0{i + 1}T10:00:00Z", "buy", frm=("Börse", "EUR", eur), to=("Börse", aid, qty),
               value=eur)
        rows += [r, {**r, "tx_id": f"B{i}"}]
    return rows


def test_bulk_correction_of_independent_duplicates_and_undo(cfg):  # noqa: F811
    ctx = make_ctx(cfg, _three_twins(), ASSETS)
    before = fingerprint(ctx.db)
    ids = [c[0].id for c in B.candidates(report_for(ctx))]
    assert len(ids) == 3
    bp = B.plan(ctx, ids)  # prüfbedürftig → ohne ausdrückliche Einbeziehung ausgeschlossen
    assert not bp.ready and all("prüfbedürftig" in it.reason for it in bp.items)
    bp = B.plan(ctx, ids, include_review=True)
    assert len(bp.ready) == 3 and not bp.conflicts and bp.effects is not None
    eff = {(p.account, p.asset): p for p in bp.effects.positions}
    assert eff[("Börse", "BTC")].bal == (D("0.02"), D("0.01")) and eff[("Börse", "EUR")].bal == (D(4400), D(7200))
    assert fingerprint(ctx.db) == before  # Vorschau ändert nichts
    res = B.execute(ctx, ids, bp.token, include_review=True)
    assert res.ok and len(res.decision_ids) == 3
    # Soll: je Asset genau die erste Buchung (A0..A2) mit ihrem Anschaffungsdatum und Einstand
    assert lots(ctx, "BTC") == [("Börse", D("0.01"), D("600.00"), date(2025, 2, 1))]
    assert lots(ctx, "ETH") == [("Börse", D("0.5"), D("1500.00"), date(2025, 2, 2))]
    assert lots(ctx, "KAS") == [("Börse", D(5), D("700.00"), date(2025, 2, 3))]
    assert bal(ctx, "Börse", "EUR") == D(7200)
    # erneutes Absenden derselben Vorschau: nichts doppelt
    again = B.execute(ctx, ids, bp.token, include_review=True)
    assert again.ok and again.decision_ids == res.decision_ids
    assert ctx.db.scalar("SELECT COUNT(*) FROM diag_decision WHERE action='fix'") == 3
    # 16: als Ganzes zurück → Ausgangszustand
    assert B.undo(ctx, res.bulk_id).ok
    assert bal(ctx, "Börse", "EUR") == D(4400) and bal(ctx, "Börse", "BTC") == D("0.02")


def test_failure_during_bulk_correction_changes_nothing(cfg, monkeypatch):  # noqa: F811
    ctx = make_ctx(cfg, _three_twins(), ASSETS)
    ids = [c[0].id for c in B.candidates(report_for(ctx))]
    bp = B.plan(ctx, ids, include_review=True)
    before = fingerprint(ctx.db)
    real, n = A._exec, [0]

    def flaky(*a, **k):
        n[0] += 1
        if n[0] == 3:
            raise A.Conflict("simuliert: Buchung inzwischen geändert.")
        return real(*a, **k)

    monkeypatch.setattr(A, "_exec", flaky)
    res = B.execute(ctx, ids, bp.token, include_review=True)
    assert not res.ok and "nichts geändert" in res.errors[0]
    assert fingerprint(ctx.db) == before
    assert bal(ctx, "Börse", "BTC") == D("0.02")


# ----------------------------------------------------------------------------------------------------------------------
# 15 · konkurrierende Vorschläge (dieselbe Buchung in zwei Befunden)
# ----------------------------------------------------------------------------------------------------------------------

def test_competing_suggestions_are_never_executed_together(cfg):  # noqa: F811
    dep = tx("D1", "2025-03-01T12:20:00Z", "deposit", to=("Wallet", "ETH", "1"), value=3000)
    rows = [FUND, tx("B1", "2025-01-10T10:00:00Z", "buy", frm=("Börse", "EUR", "2000"), to=("Börse", "ETH", "1"),
                     value=2000),
            tx("W1", "2025-03-01T12:00:00Z", "withdrawal", frm=("Börse", "ETH", "1"), value=3000),
            dep, {**dep, "tx_id": "D2"}]
    ctx = make_ctx(cfg, rows, ASSETS)
    rep = report_for(ctx)
    ids = [c[0].id for c in B.candidates(rep)]
    kinds = {rep.by_id(i).kind for i in ids}
    assert kinds == {"duplicate", "transfer"}
    before = fingerprint(ctx.db)
    bp = B.plan(ctx, ids, include_review=True)
    # Transfer W1→D1 oder D2 ist mehrdeutig: keine Zuordnung, keine bevorzugte Lösung (M28, statt greedy D1);
    # Transfer und Dublette werden nie gemeinsam ausgeführt
    (t,) = [it for it in bp.items if it.finding.kind == "transfer"]
    assert t.finding.data.get("ambiguous") and not t.ready and "keine bevorzugte Lösung" in t.reason
    assert not any(it.ready and it.finding.kind == "transfer" for it in bp.items)
    assert fingerprint(ctx.db) == before


# ----------------------------------------------------------------------------------------------------------------------
# 19 · veränderter Datenstand zwischen Vorschau und Übernahme
# ----------------------------------------------------------------------------------------------------------------------

def test_changed_data_between_preview_and_apply_is_rejected(cfg):  # noqa: F811
    from app.journal.service import journal_service

    ctx = make_ctx(cfg, _three_twins(), ASSETS)
    ids = [c[0].id for c in B.candidates(report_for(ctx))]
    bp = B.plan(ctx, ids, include_review=True)
    assert not journal_service(ctx).save({"kind": "deposit", "date": "2025-04-01", "account": "Börse",
                                          "asset": "EUR", "qty": "5"}).errors
    before = fingerprint(ctx.db)
    res = B.execute(ctx, ids, bp.token, include_review=True)
    assert not res.ok and "nicht mehr aktuell" in res.errors[0]
    assert fingerprint(ctx.db) == before


# ----------------------------------------------------------------------------------------------------------------------
# 20 · gleiche Quelldaten in anderer Reihenfolge
# ----------------------------------------------------------------------------------------------------------------------

def test_same_data_in_different_order_gives_same_findings_and_plans(cfg, tmp_path):  # noqa: F811
    rows = _three_twins()
    ctx = make_ctx(cfg, rows, ASSETS)
    rep = report_for(ctx)
    sig = sorted((f.id, f.kind, f.status) for f in rep.findings)
    plans = sorted((i.finding.id, tuple(op.target for op in i.plan.ops))
                   for i in B.plan(ctx, [c[0].id for c in B.candidates(rep)], include_review=True,
                                   with_effects=False).ready)
    shuffled = rows[:]
    random.Random(7).shuffle(shuffled)
    cfg2 = cfg.__class__(**{**cfg.__dict__, "data_dir": tmp_path / "b" / "data", "import_dir": tmp_path / "b" / "i"})
    cfg2.import_dir.mkdir(parents=True)
    ctx2 = make_ctx(cfg2, shuffled, ASSETS)
    rep2 = report_for(ctx2)
    assert sorted((f.id, f.kind, f.status) for f in rep2.findings) == sig
    plans2 = sorted((i.finding.id, tuple(op.target for op in i.plan.ops))
                    for i in B.plan(ctx2, [c[0].id for c in B.candidates(rep2)], include_review=True,
                                    with_effects=False).ready)
    assert plans2 == plans
    assert ctx2.ledger().balances == ctx.ledger().balances


def test_bulk_web_flow_preview_apply_undo(cfg):  # noqa: F811
    import re

    from fastapi.testclient import TestClient

    from app.main import build_app

    make_ctx(cfg, _three_twins(), ASSETS)
    with TestClient(build_app(cfg, start_scheduler=False)) as c:
        c.get("/settings")
        c.headers["X-CSRF-Token"] = c.cookies.get("portfolia_csrf")
        page = c.get("/quality/diagnose/bulk").text
        ids = re.findall(r'name="f" value="(f-[0-9a-f]+)"', page)
        assert len(ids) == 3 and "prüfbedürftig" in page
        q = "&".join(f"f={i}" for i in ids)
        prev = c.get(f"/quality/diagnose/bulk/preview?{q}").text
        assert "0 ausführbar" in prev and "erst prüfen" in prev  # ohne Einbeziehung nichts ausführbar
        prev = c.get(f"/quality/diagnose/bulk/preview?{q}&review=1").text
        assert "3 ausführbar" in prev and "Auswirkungen (berechnet)" in prev and "Anschaffungsdaten" in prev
        token = re.search(r'name="token" value="([0-9a-f]+)"', prev).group(1)
        r = c.post("/quality/diagnose/bulk/apply", data={"f": ids, "token": token, "review": "1"},
                   follow_redirects=False)
        assert r.status_code == 303 and "Sammelkorrektur" in c.get(r.headers["location"]).text
        ctx = c.app.state.ctx
        assert bal(ctx, "Börse", "EUR") == D(7200)
        bid = re.search(r'/quality/diagnose/bulk/([0-9a-f]+)/undo', c.get("/quality/diagnose/bulk").text).group(1)
        assert c.post(f"/quality/diagnose/bulk/{bid}/undo", follow_redirects=False).status_code == 303
        assert bal(ctx, "Börse", "EUR") == D(4400)
