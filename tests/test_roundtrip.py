"""Abgang mit Rückfluss auf demselben Konto (KRC-20-Mint: Commit/Reveal) → Netzwerkgebühr. Synthetische Daten.

Koinly bucht den Commit als Abgang und den Reveal als neuen Zugang. Bestand und Summe stimmen, aber jeder Abgang ist
ein Verkauf ohne Gegenbuchung und jeder Zugang eine neue Anschaffung (Einstand, Haltedauer). Regeln: belegt nur bei
gleichem Hash; sonst ein wiederkehrendes Muster (≥ 3 Paare) = „wahrscheinlich“, vereinzelt = „Verdacht“; nie bei
Zwillingen, abweichender Menge, großem Abstand oder anderem Konto; Autopilot nur mit eigener Freigabe.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from app.diagnosis import actions as A
from app.diagnosis import autopilot as AP
from app.diagnosis import bulk as B
from app.diagnosis import recommend as R
from app.diagnosis.engine import report_for
from tests.helpers import tx
from tests.test_clarify import ASSETS_K, ctx_for
from tests.test_clarify import cfg as cfg
from tests.test_diagnosis import fingerprint
from tests.test_diagnosis_audit import koinly

D = Decimal
ACC = "Kaspa (KAS)"
T0 = datetime(2024, 9, 20, 6, 0, 0)


def at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def fund() -> dict:
    return koinly(tx("F0", "2023-01-02T08:00:00Z", "deposit", to=(ACC, "KAS", "1000"), value=100))


def mint(n: int, minute: float, *, gross: str = "21", back: str = "19.99996835", hashes: tuple[str, str] | None = None,
         gap_s: int = 7, acc: str = ACC) -> list[dict]:
    w = koinly(tx(f"W{n}", at(minute), "withdrawal", frm=(acc, "KAS", gross), value=3.2))
    d = koinly(tx(f"D{n}", (T0 + timedelta(minutes=minute, seconds=gap_s)).strftime("%Y-%m-%dT%H:%M:%SZ"), "deposit",
                  to=(acc, "KAS", back), value=3.05))
    if hashes:
        w["note"], d["note"] = f"txhash={hashes[0]}", f"txhash={hashes[1]}"
    return [w, d]


def pattern(n: int = 4, **kw) -> list[dict]:
    rows = [fund()]
    for i in range(n):
        rows += mint(i, i * 3, **kw)
    return rows


def roundtrips(rep) -> list:
    return [f for f in rep.findings if f.kind == "roundtrip"]


def test_pattern_is_one_probable_finding_not_loss_or_transfer(cfg):
    ctx = ctx_for(cfg, pattern(4))
    rep = report_for(ctx)
    (f,) = roundtrips(rep)
    assert f.status == "wahrscheinlich" and len(f.data["pairs"]) == 4 and f.data["proven"] is False
    assert f.data["fee"] == str(D("1.00003165") * 4)
    assert not [x for x in rep.findings if x.kind in ("loss", "transfer")]  # kein Doppelanspruch
    assert rep.stats["roundtrips"] == 4
    rec = R.recommend(rep, f)
    assert rec.primary is not None and rec.primary.key == "book_fee" and not rec.conditional


def test_isolated_pair_is_only_suspected_and_never_automatic(cfg):
    ctx = ctx_for(cfg, pattern(2))
    (f,) = roundtrips(report_for(ctx))
    assert f.status == "verdacht"
    rec = R.recommend(report_for(ctx), f)
    assert rec.conditional
    ctx.settings.set("autopilot.enabled", True)
    ctx.settings.set("autopilot.roundtrips", True)
    out = AP.run(ctx, "manual")
    assert out["fixes"]["applied"] == 0


def test_same_hash_is_proven(cfg):
    h = "ab" * 32
    ctx = ctx_for(cfg, [fund(), *mint(1, 0, hashes=(h, h))])
    (f,) = roundtrips(report_for(ctx))
    assert f.status == "belegt" and f.data["proven"] is True
    assert "denselben Transaktions-Hash" in " ".join(f.known)


def test_no_finding_for_other_shapes(cfg):
    rows = [fund(),
            *mint(1, 0, back="5"),  # nur ein Teil kommt zurück (< 90 %)
            *mint(2, 10, gap_s=1500),  # Rückfluss erst nach 25 Minuten
            *mint(3, 40, back="21"),  # volle Menge zurück: kein Abzug, keine Gebühr
            *mint(4, 50, back="22")]  # mehr zurück als gesendet
    assert not roundtrips(report_for(ctx_for(cfg, rows)))


def test_other_account_is_not_a_roundtrip(cfg):
    w, d = mint(1, 0)
    d["to_account"] = "Anderes Konto"
    assert not roundtrips(report_for(ctx_for(cfg, [fund(), w, d])))


def test_identical_twins_are_left_to_the_duplicate_check(cfg):
    rows = [fund()]
    for n in (1, 2):  # zwei identische Abgänge/Zugänge zur selben Sekunde
        rows += mint(n, 0)
    assert not roundtrips(report_for(ctx_for(cfg, rows)))


def test_competing_withdrawals_of_different_size_stay_open(cfg):
    """Ein Zugang, zu dem zwei Abgänge verschiedener Menge passen: keine Zuordnung erraten."""
    rows = [fund(),
            koinly(tx("W1", at(0), "withdrawal", frm=(ACC, "KAS", "21"), value=3)),
            koinly(tx("W2", at(0.5), "withdrawal", frm=(ACC, "KAS", "21.4"), value=3)),
            koinly(tx("D1", at(1), "deposit", to=(ACC, "KAS", "20.1"), value=3))]
    rep = report_for(ctx_for(cfg, rows))
    assert not roundtrips(rep) and rep.stats["roundtrips_ambiguous"] == 1


def test_bursts_with_equal_amounts_pair_in_order(cfg):
    """Mehrere gleich große Abgänge hintereinander: austauschbar → Zuordnung in zeitlicher Reihenfolge."""
    rows = [fund()]
    for i in range(4):
        rows.append(koinly(tx(f"W{i}", at(i * 0.1), "withdrawal", frm=(ACC, "KAS", "21"), value=3)))
    for i in range(4):
        rows.append(koinly(tx(f"D{i}", at(1 + i * 0.1), "deposit", to=(ACC, "KAS", "19.99996835"), value=3)))
    (f,) = roundtrips(report_for(ctx_for(cfg, rows)))
    assert [p[0] for p in f.data["pairs"]] == ["W0", "W1", "W2", "W3"]
    assert [p[1] for p in f.data["pairs"]] == ["D0", "D1", "D2", "D3"]


def test_apply_replaces_pairs_by_fee_keeps_balance_and_undo_restores(cfg):
    ctx = ctx_for(cfg, pattern(4))
    rep = report_for(ctx)
    (f,) = roundtrips(rep)
    led0 = ctx.ledger()
    bal0 = led0.balances[(ACC, "KAS")]
    n0 = len([x for x in led0.disposals if x.asset == "KAS"])
    before = fingerprint(ctx.db)
    plan = A.build_plan(ctx, rep, f, "book_fee")
    assert not plan.errors
    assert [op.kind for op in plan.ops].count("create") == 4 and [op.kind for op in plan.ops].count("hide") == 8
    eff = A.preview(ctx, rep, plan)
    assert not eff.negative_new and not eff.issues_new
    assert fingerprint(ctx.db) == before  # Vorschau schreibt nichts
    res = A.apply(ctx, f.id, "book_fee", plan.params, plan.token)
    assert res.ok, res.errors
    led1 = ctx.ledger()
    assert led1.balances[(ACC, "KAS")] == bal0  # Bestand unverändert
    fees = ctx.db.q("SELECT tag, from_qty, from_asset, source FROM journal_tx WHERE source='diagnose'")
    assert len(fees) == 4 and {(r["tag"], r["from_qty"], r["source"]) for r in fees} == {("fee", "1.00003165",
                                                                                          "diagnose")}
    assert ctx.db.scalar("SELECT COUNT(*) FROM tx_override WHERE action='delete'") == 8  # Importzeilen bleiben erhalten
    assert not roundtrips(report_for(ctx))
    # die Rückflüsse sind keine neuen Lots mehr: nur noch das Ausgangslot (+ ggf. Gebühren-Restlots) trägt den Bestand
    assert len([x for x in led1.disposals if x.asset == "KAS"]) <= n0
    undo = A.undo(ctx, res.decision_id)
    assert undo.ok, undo.errors
    ctx.invalidate_data()
    assert ctx.ledger().balances[(ACC, "KAS")] == bal0
    assert len(roundtrips(report_for(ctx))) == 1
    assert not ctx.db.q("SELECT 1 FROM tx_override WHERE action='delete'")


def test_value_of_fee_is_proportional_and_unknown_stays_empty(cfg):
    rows = pattern(3)
    rows[1]["value_eur"] = ""  # Wert des ersten Abgangs unbekannt
    ctx = ctx_for(cfg, rows)
    rep = report_for(ctx)
    (f,) = roundtrips(rep)
    plan = A.build_plan(ctx, rep, f, "book_fee")
    vals = sorted(str(op.row["value_eur"]) for op in plan.ops if op.kind == "create")
    assert vals[0] == "" and len(vals) == 3 and all(v for v in vals[1:])


def test_autopilot_needs_its_own_release_and_is_undoable(cfg):
    ctx = ctx_for(cfg, pattern(4))
    bal0 = ctx.ledger().balances[(ACC, "KAS")]
    # Standard: aus
    assert AP.run(ctx, "manual")["fixes"]["applied"] == 0
    # nur der Autopilot an: weiterhin nichts (eigene Freigabe fehlt)
    ctx.settings.set("autopilot.enabled", True)
    assert AP.run(ctx, "manual")["fixes"]["applied"] == 0
    assert len(roundtrips(report_for(ctx))) == 1
    ctx.settings.set("autopilot.roundtrips", True)
    out = AP.run(ctx, "manual")
    assert out["fixes"]["applied"] == 1 and out["fixes"]["bulk_ids"]
    ctx.invalidate_data()
    assert ctx.ledger().balances[(ACC, "KAS")] == bal0 and not roundtrips(report_for(ctx))
    undone = AP.undo(ctx, out["token"])
    assert undone.ok, undone.errors
    ctx.invalidate_data()
    assert len(roundtrips(report_for(ctx))) == 1


def test_general_fix_switch_with_strict_level_does_not_touch_probable_roundtrips(cfg):
    ctx = ctx_for(cfg, pattern(4))
    ctx.settings.set("autopilot.enabled", True)
    ctx.settings.set("autopilot.fixes", True)  # Stufe „eindeutig“ (Standard)
    assert AP.run(ctx, "manual")["fixes"]["applied"] == 0
    ctx.settings.set("autopilot.fixes_level", "hoch plausibel")
    assert AP.run(ctx, "manual")["fixes"]["applied"] == 1


def test_bulk_plan_contains_roundtrips_as_recon_candidates(cfg):
    ctx = ctx_for(cfg, pattern(4))
    rep = report_for(ctx)
    cands = [(f, c) for f, c, _l in B.candidates(rep) if f.kind == "roundtrip"]
    assert len(cands) == 1 and cands[0][1] == "hoch plausibel"


def test_assets_include_kas():
    assert any(a["asset_id"] == "KAS" for a in ASSETS_K)


def test_pages_render_case_preview_and_autopilot_setting(cfg):
    from fastapi.testclient import TestClient

    from app.importer.zipbuilder import build_zip
    from app.jobs import tasks
    from app.main import build_app
    from tests.test_diagnosis import COLS

    dst = cfg.import_dir / "kuratiert.zip"
    build_zip(dst, transactions=pattern(4), assets=ASSETS_K, generated_at="2025-06-30T20:00:00Z",
              valuation_date="2025-06-30", extra_tx_columns=COLS)
    import os
    import time

    old = time.time() - 3600
    os.utime(dst, (old, old))
    with TestClient(build_app(cfg, start_scheduler=False)) as c:
        ctx = c.app.state.ctx
        assert tasks.import_check(ctx, "test").status == "imported"
        (f,) = roundtrips(report_for(ctx))
        before = fingerprint(ctx.db)
        r = c.get(f"/quality/diagnose/case/{f.id}")
        assert r.status_code == 200 and "Netzwerkgebühr" in r.text and "Rückfluss" in r.text
        r = c.get(f"/quality/diagnose/plan?f={f.id}&o=book_fee")
        assert r.status_code == 200 and "Netzwerkgebühr" in r.text
        r = c.get("/quality/faelle?kind=roundtrip")
        assert r.status_code == 200 and "Rückfluss" in r.text
        r = c.get("/quality/autopilot")
        assert r.status_code == 200 and "Netzwerkgebühren aus Abgang" in r.text
        assert fingerprint(ctx.db) == before  # alle Seiten lesen nur


# -- Prüfstapel: Zeilen der Datenquelle (nur Gebühr) gegen Koinly-Abgang/-Zugang bzw. Netto-Gebühr --------------------

def _krc_rows(hc: str, hr: str, label: str | None = "KRC-20 (Commit/Reveal)"):
    from app.csvimport import model as M
    from tests.test_reconcile import _Row

    return [_Row(0, kind=M.FEE, txhash=hc, fee_sym="KAS", fee_qty=D("0.0001"), label=label),
            _Row(1, kind=M.FEE, txhash=hr, fee_sym="KAS", fee_qty=D("1"), label=label)]


def test_krc_commit_reveal_rows_are_covered_by_koinly_gross_booking():
    """Koinly: Commit = Abgang 21 KAS, Reveal = Zugang ~20 KAS; Quelle: nur die Gebühr je Transaktion."""
    from app.csvimport import reconcile as R
    from tests.test_reconcile import _Tx

    hc, hr = "aa" * 32, "bb" * 32
    idx = R.HashIndex([_Tx("W1", f"txhash={hc}", type="withdrawal", from_account=ACC, from_asset="KAS",
                           from_qty=D("21")),
                       _Tx("D1", f"txhash={hr}", type="deposit", to_account=ACC, to_asset="KAS",
                           to_qty=D("19.99996835"))], [])
    res = R.reconcile(_krc_rows(hc, hr), idx, lambda s: s if s == "KAS" else None, ["KAS"])
    assert [res.rows[i].state for i in (0, 1)] == ["full", "full"]
    assert res.rows[0].txs == ["W1"] and res.rows[1].txs == ["D1"] and "Commit/Reveal" in res.rows[0].note
    # ohne die Kennzeichnung der Quelle bleibt es wie bisher (Mengen weichen ab → teilweise)
    plain = R.reconcile(_krc_rows(hc, hr, label=None), idx, lambda s: s if s == "KAS" else None, ["KAS"])
    assert [plain.rows[i].state for i in (0, 1)] == ["partial", "partial"]
    # anderes Asset auf der Gegenseite deckt nichts
    other = R.HashIndex([_Tx("W2", f"txhash={hc}", type="withdrawal", from_account=ACC, from_asset="ETH",
                             from_qty=D("21"))], [])
    assert 0 not in {i for i, m in R.reconcile(_krc_rows(hc, hr), other, lambda s: s, ["KAS", "ETH"]).rows.items()
                     if m.state == "full"}


def test_krc_rows_stay_covered_after_the_net_fee_fix_hides_the_koinly_pair(cfg):
    """Nach „Als Netzwerkgebühr buchen“ sind Abgang und Zugang ausgeblendet; die Gebühren-Buchung trägt beide Hashes."""
    from app.csvimport import reconcile as R

    hashes = [("%064x" % (2 * i + 1), "%064x" % (2 * i + 2)) for i in range(4)]
    rows = [fund()]
    for i in range(4):
        rows += mint(i, i * 3, hashes=(f"0x{hashes[i][0]}", f"0x{hashes[i][1]}"))
    ctx = ctx_for(cfg, rows)
    rep = report_for(ctx)
    (f,) = roundtrips(rep)
    plan = A.build_plan(ctx, rep, f, "book_fee")
    assert A.apply(ctx, f.id, "book_fee", plan.params, plan.token).ok
    journal = [dict(r) for r in ctx.db.q(
        "SELECT tx_id, type, tag, from_account, from_asset, from_qty, to_account, to_asset, to_qty, fee_asset, "
        "fee_qty, tx_hash, source, note FROM journal_tx WHERE status IN ('active', 'merged') AND "
        "((tx_hash IS NOT NULL AND tx_hash <> '') OR source = 'diagnose') AND source <> 'transfer'")]
    assert len(journal) == 4 and all(h in " ".join(j["note"] for j in journal) for pair in hashes for h in pair)
    idx = R.HashIndex([], journal)
    hc, hr = hashes[1]
    res = R.reconcile(_krc_rows(hc, hr), idx, lambda s: s if s == "KAS" else None, ["KAS"])
    assert [res.rows[i].state for i in (0, 1)] == ["full", "full"]
    assert res.rows[0].origin == "journal" and res.rows[0].txs == res.rows[1].txs
