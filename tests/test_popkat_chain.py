"""Doppelte Gutschrift nach Brücke/Transfer, belegt durch die Herkunft der Menge (0.30.0).

Muster (synthetische Werte): Kauf auf dem Quell-Konto (zwei Käufe mit Hash) → Transfer zum Ziel-Konto (mit Hash, Gebühr
im Asset) – dazu eine manuell erfasste Gutschrift gleicher Menge am Ziel (ohne Hash). Die Käufe ergeben genau
Transfermenge + Gebühr: eine zweite Gutschrift hat keine Herkunft. Wurde danach „alles“ ausgebucht, enthielt die
Ausbuchung die doppelte Menge mit – sie wird beim Ausblenden der Doppelung passend gekürzt.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.diagnosis import actions as A
from app.diagnosis import autopilot as AP
from app.diagnosis import clarify as K
from app.diagnosis.engine import report_for
from app.diagnosis.recommend import recommend
from app.journal.service import journal_service
from tests.helpers import ASSETS, tx
from tests.test_diagnosis import NOW, add_wallet, asset, cfg, make_ctx  # noqa: F401

D = Decimal
SRC, DST = "MetaMask (ETH)", "Kaspa (KAS)"
Q = "124232.32918951"


def _rows(*, with_hash_on_deposit: bool = False):
    h = ["0x" + c * 64 for c in "123"]
    fund = tx("F0", "2024-10-01T08:00:00Z", "deposit", to=(SRC, "USDT", "1000"), value=1000)
    dep = tx("P0", "2024-10-30T12:00:00Z", "deposit", to=(DST, "POPKAT", Q), value="0")
    dep["source"], dep["flag"] = "koinly", "KOINLY_MANUAL"
    if with_hash_on_deposit:
        dep["note"] = f"txhash={h[2]}"
    t1 = tx("P1", "2024-10-30T20:56:00Z", "trade", frm=(SRC, "USDT", "200"), to=(SRC, "POPKAT", "63367.67844392"),
            value="200")
    t2 = tx("P2", "2024-10-30T20:58:00Z", "trade", frm=(SRC, "USDT", "200"), to=(SRC, "POPKAT", "61120.66112911"),
            value="200")
    tr = tx("P3", "2024-10-30T21:03:00Z", "transfer", frm=(SRC, "POPKAT", Q), to=(DST, "POPKAT", Q),
            fee=("POPKAT", "256.01038049", ""), value="0")
    for t, x in ((t1, h[0]), (t2, h[1]), (tr, h[2])):
        t["source"], t["note"] = "koinly", f"txhash={x}"
    return [fund, dep, t1, t2, tr]


def _ctx(cfg, writeoff_qty: str | None = "248464.65837902", rows=None):  # noqa: F811
    ctx = make_ctx(cfg, rows or _rows(), [*ASSETS, asset("POPKAT"), asset("USDT")])
    if writeoff_qty:
        res = journal_service(ctx).save({"kind": "expert", "time": "21:59", "type": "withdrawal", "tag": "lost",
                                         "date": "2026-10-09", "from_account": DST, "from_asset": "POPKAT",
                                         "from_qty": writeoff_qty, "note": "Ausbuchung (Totalverlust)"})
        assert not res.errors, res.errors
        ctx.invalidate_data()
    return ctx


def _dup(rep):
    return next(f for f in rep.findings if f.kind == "duplicate" and "POPKAT" in f.title)


def test_supply_chain_makes_the_duplicate_proven_and_adds_evidence(cfg):  # noqa: F811
    rep = report_for(_ctx(cfg, None))
    f = _dup(rep)
    assert f.status == "belegt" and f.data["chain"] is True
    ev = " ".join(f.evidence)
    assert "Herkunft belegt" in ev and "P1" in ev and "P2" in ev and "zweite Gutschrift" in ev


def test_no_chain_when_supply_does_not_match_the_transfer(cfg):  # noqa: F811
    rows = _rows()
    rows[2]["to_qty"] = "30000"  # die Käufe decken die Transfermenge nicht
    rep = report_for(_ctx(cfg, None, rows))
    f = _dup(rep)
    assert f.data["chain"] is False and f.status != "belegt"


def test_adjusting_the_write_off_is_the_recommended_fix_and_is_reversible(cfg):  # noqa: F811
    ctx = _ctx(cfg)
    rep = report_for(ctx)
    f = _dup(rep)
    rec = recommend(rep, f)
    assert rec.primary is not None and rec.primary.key == "hide_weak_adjust"
    # nur „ausblenden“ ließe den Bestand negativ werden und ist nicht empfohlen
    assert not next(o for o in rec.options if o.key == "hide_weak").recommended
    plan = A.build_plan(ctx, rep, f, "hide_weak_adjust")
    assert not plan.errors, plan.errors
    assert [op.kind for op in plan.ops] == ["hide", "hide", "create"]
    new = next(op for op in plan.ops if op.kind == "create")
    assert new.tx is not None and new.tx.from_qty == D(Q) and new.tx.tag == "lost" and new.tx.from_account == DST
    res = A.apply(ctx, f.id, "hide_weak_adjust", plan.params, plan.token)
    assert res.ok, res.errors
    ctx.invalidate_data()
    assert ctx.ledger().balances.get((DST, "POPKAT"), D(0)) == 0  # nichts negativ, Ausbuchung = tatsächliche Menge
    assert not any(f.kind == "duplicate" and "POPKAT" in f.title for f in report_for(ctx).findings)
    # gemeinsam zurücknehmen: Doppelung und alte Ausbuchung zählen wieder, die gekürzte ist weg
    assert A.undo(ctx, res.decision_id).ok
    ctx.invalidate_data()
    assert ctx.ledger().balances.get((DST, "POPKAT"), D(0)) == 0
    assert _dup(report_for(ctx)).data["chain"] is True


def test_wallet_still_showing_the_written_off_amount_is_explained_after_the_fix(cfg):  # noqa: F811
    ctx = _ctx(cfg)
    add_wallet(ctx, DST, {"POPKAT": Q}, observed=NOW - timedelta(hours=1), complete=True, sid=1)
    ctx.invalidate_data()
    rep = report_for(ctx)
    f = _dup(rep)
    plan = A.build_plan(ctx, rep, f, "hide_weak_adjust")
    assert A.apply(ctx, f.id, "hide_weak_adjust", plan.params, plan.token).ok
    ctx.invalidate_data()
    rep2 = report_for(ctx)
    row = next(h for h in rep2.holdings if h.asset == "POPKAT" and h.account == DST)
    assert row.status == "extern_ausgebucht" and row.writeoff == D(Q)
    assert not [x for x in rep2.findings if x.kind == "holdings" and "POPKAT" in x.title]


def test_clarification_decides_apply_on_the_chain_and_autopilot_considers_it_safe(cfg):  # noqa: F811
    ctx = _ctx(cfg)
    rep = report_for(ctx)
    cases = K.analyse(ctx, rep)
    case = next(c for c in cases if any(a == "POPKAT" for _acc, a in c.positions))
    assert case.decision is not None and case.decision.kind == "apply", case.decision
    assert "Herkunft" in case.decision.reason
    assert case.decision.hyp is not None and any(c.option == "hide_weak_adjust" for c in case.decision.hyp.cands)
    f = _dup(rep)
    plan = A.build_plan(ctx, rep, f, "hide_weak_adjust")
    assert AP._safe(plan)  # nur Ausblenden + gekürzte Ausbuchung der eigenen Art
