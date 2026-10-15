"""Entscheidungsregeln und Autopilot (synthetische Daten, temporäre DB)."""

from __future__ import annotations

import os
import re
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Config, Secrets
from app.diagnosis import actions as A
from app.diagnosis import autopilot as AP
from app.diagnosis import rules as R
from app.diagnosis import triage as TR
from app.diagnosis.engine import report_for
from app.importer.zipbuilder import build_zip
from app.jobs import tasks
from app.main import build_app
from tests.helpers import ASSETS, tx
from tests.test_diagnosis import COLS, make_ctx

D = Decimal


@pytest.fixture
def cfg(tmp_path) -> Config:
    imp = tmp_path / "import"
    imp.mkdir(parents=True, exist_ok=True)
    return Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                  startup_jobs=False, log_format="text", secrets=Secrets())


HINT_ROWS = [tx("F1", "2022-01-10T10:00:00Z", "deposit", to=("Wallet Alt", "KAS", "5000"), value="500"),
             tx("F2", "2022-02-10T10:00:00Z", "deposit", to=("Wallet Alt 2", "KAS", "10"), value="1")]

FUND = tx("F0", "2025-01-02T08:00:00Z", "deposit", to=("Börse", "EUR", "10000"), value=10000)
TRANSFER_ROWS = [FUND,
                 tx("B1", "2025-01-10T10:00:00Z", "buy", frm=("Börse", "EUR", "2000"), to=("Börse", "ETH", "1"),
                    value=2000),
                 tx("W1", "2025-03-01T12:00:00Z", "withdrawal", frm=("Börse", "ETH", "1"), value=3000),
                 tx("D1", "2025-03-01T12:20:00Z", "deposit", to=("Wallet", "ETH", "1"), value=3000)]
for _r in TRANSFER_ROWS[-2:]:  # gemeinsamer Transaktions-Hash als Beleg
    _r["note"] = "txhash 0x" + "ab" * 32


def journal_rows(ctx) -> list[tuple]:
    return [tuple(r) for r in ctx.db.q("SELECT * FROM journal_tx ORDER BY 1")]


def hints(ctx):
    """Offene Hinweise der Art „inaktives Konto“ (weitere Hinweise anderer Art sind für die Regeln unerheblich)."""
    return [f for f in TR.open_findings(ctx) if TR.is_hint(f) and f.kind == "inactive"]


# ---------------------------------------------------------------------------------------------------- Regeln

def test_rule_matching_is_narrow():
    r = R.Rule(1, "duplicate", "Konto A", "", "hinweis", "", "active", "x")
    f = SimpleNamespace(parent=None, kind="duplicate", status="hinweis", accounts=["Konto A"], assets=["BTC"])
    assert r.matches(f)
    assert not r.matches(SimpleNamespace(**{**f.__dict__, "status": "verdacht"}))
    assert not r.matches(SimpleNamespace(**{**f.__dict__, "accounts": ["Konto B"]}))
    assert not r.matches(SimpleNamespace(**{**f.__dict__, "kind": "transfer"}))
    wide = R.Rule(2, "duplicate", "", "", "verdacht", "", "active", "x")
    assert wide.matches(SimpleNamespace(**{**f.__dict__, "status": "verdacht"}))
    for status in ("wahrscheinlich", "belegt"):  # nie erfasst, auch nicht bei weitester Stufe
        assert not wide.matches(SimpleNamespace(**{**f.__dict__, "status": status}))
    assert not wide.matches(SimpleNamespace(**{**f.__dict__, "parent": "x"}))


def test_rule_apply_undo_and_changed_evidence(cfg):
    ctx = make_ctx(cfg, HINT_ROWS, ASSETS)
    assert not R.create(ctx, "nope").ok and not R.create(ctx, "inactive", max_status="x").ok
    assert R.create(ctx, "inactive", account="Wallet Alt", note="bewusst stillgelegt").ok
    assert not R.create(ctx, "inactive", account="Wallet Alt").ok  # gleiche aktive Regel
    (rule,) = R.rules(ctx.db)
    assert len(R.preview(ctx, rule)) == 1 and len(hints(ctx)) == 2  # Vorschau ändert nichts
    rows = journal_rows(ctx)
    res, token = R.apply_all(ctx)
    assert res.ok and token and "1 Befund" in res.message
    assert [f.accounts for f in hints(ctx)] == [["Wallet Alt 2"]] and journal_rows(ctx) == rows
    assert R.rules(ctx.db)[0].applied_count == 1
    assert R.apply_all(ctx)[1] == ""  # idempotent
    assert R.undo(ctx, token) == 1 and len(hints(ctx)) == 2
    assert R.undo(ctx, token) == 0 and R.undo(ctx, "../x") == 0
    # neue Evidenz: einmal markiert, dann geänderte Prüfsumme → Regel blendet nicht erneut aus
    R.apply_all(ctx)
    ctx.db.x("UPDATE diag_decision SET fingerprint='alt' WHERE status='active'")
    res, token = R.apply_all(ctx)
    assert token == "" and "bleiben offen" in res.message and len(hints(ctx)) == 2


def test_rule_switch_off_with_revert(cfg):
    ctx = make_ctx(cfg, HINT_ROWS, ASSETS)
    R.create(ctx, "inactive")
    assert R.apply_all(ctx)[0].ok and not hints(ctx)
    rid = R.rules(ctx.db)[0].id
    assert R.switch_off(ctx, rid).ok and not hints(ctx)  # Markierungen bleiben
    assert not R.switch_off(ctx, rid).ok
    assert R.apply_all(ctx)[1] == "" and R.switch_on(ctx, rid).ok
    assert R.switch_off(ctx, rid, revert=True).ok and len(hints(ctx)) == 2


# ---------------------------------------------------------------------------------------------------- Autopilot

def test_report_is_always_written_and_changes_nothing_when_off(cfg):
    ctx = make_ctx(cfg, HINT_ROWS, ASSETS)
    R.create(ctx, "inactive")
    rows = journal_rows(ctx)
    s = AP.run(ctx, "import")
    assert s is not None and s["enabled"] is False and s["hints"] >= 2 and s["rules"]["count"] == 0
    assert len(hints(ctx)) == 2 and journal_rows(ctx) == rows
    (run,) = AP.last_runs(ctx.db)
    assert run["trigger"] == "import" and run["s"]["first"] is True
    assert not AP.undo(ctx, run["token"]).ok  # nichts zurückzunehmen


def test_autopilot_applies_rules_and_undoes_jointly(cfg):
    ctx = make_ctx(cfg, HINT_ROWS, ASSETS)
    R.create(ctx, "inactive")
    ctx.settings.set("autopilot.enabled", True)
    s = AP.run(ctx, "import")
    assert s["rules"]["count"] == 2 and not hints(ctx)
    assert AP.undo(ctx, s["token"]).ok and len(hints(ctx)) == 2
    assert not AP.undo(ctx, s["token"]).ok  # bereits zurückgenommen
    assert AP.last_runs(ctx.db, 1)[0]["status"] == "reverted"
    assert not AP.undo(ctx, "gibtsnicht").ok


def test_report_counts_new_tasks_between_runs(cfg):
    ctx = make_ctx(cfg, TRANSFER_ROWS, ASSETS)
    first = AP.run(ctx, "import")
    assert first["tasks"] >= 1 and first["first"] and first["n_new"] == 0
    again = AP.run(ctx, "import")
    assert again["n_new"] == 0 and again["n_gone"] == 0 and again["tasks"] == first["tasks"]


def test_autopilot_fixes_are_opt_in_and_reversible(cfg):
    ctx = make_ctx(cfg, TRANSFER_ROWS, ASSETS)
    ctx.settings.set("autopilot.enabled", True)
    rows = journal_rows(ctx)
    s = AP.run(ctx, "import")  # Korrekturen nicht eingeschaltet
    assert s["fixes"]["applied"] == 0 and journal_rows(ctx) == rows
    ctx.settings.set("autopilot.fixes", True)
    s = AP.run(ctx, "import")  # Stufe „eindeutig“: der Fall ist nur hoch plausibel
    assert s["fixes"]["applied"] == 0 and journal_rows(ctx) == rows
    ctx.settings.set("autopilot.fixes_level", "hoch plausibel")
    s = AP.run(ctx, "import")
    assert s["fixes"]["applied"] == 1 and s["fixes"]["bulk_id"]
    ctx.invalidate_data()
    assert ctx.ledger().balances.get(("Wallet", "ETH")) == D(1)
    assert not ctx.ledger().balances.get(("Börse", "ETH"))
    assert not [f for f in report_for(ctx).findings if f.kind == "transfer"]
    assert AP.run(ctx, "import")["fixes"]["applied"] == 0  # idempotent
    run = next(r for r in AP.last_runs(ctx.db) if r["s"]["fixes"]["applied"])
    assert AP.undo(ctx, run["token"]).ok
    ctx.invalidate_data()
    assert not ctx.ledger().balances.get(("Börse", "ETH"))
    assert ctx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE status='active'") == 0  # Spur bleibt als „reverted“


def test_safe_plan_rejects_invented_bookings():
    mk = lambda *ops: SimpleNamespace(ops=list(ops))  # noqa: E731
    hide, cover = SimpleNamespace(kind="hide", tx=None), SimpleNamespace(kind="cover", tx=None)
    tr = SimpleNamespace(kind="create", tx=SimpleNamespace(type="transfer"))
    buy = SimpleNamespace(kind="create", tx=SimpleNamespace(type="buy"))
    assert AP._safe(mk(hide)) and AP._safe(mk(cover, hide)) and AP._safe(mk(tr, hide, hide))
    assert not AP._safe(mk(tr, hide))  # Transfer nur aus zwei vorhandenen Buchungen
    assert not AP._safe(mk(buy, hide, hide)) and not AP._safe(mk(SimpleNamespace(kind="amend", tx=None)))


# ---------------------------------------------------------------------------------------------------- Web + Hooks

def test_web_flow_rules_and_autopilot_settings(cfg):
    dst = cfg.import_dir / "kuratiert.zip"
    build_zip(dst, transactions=HINT_ROWS, assets=ASSETS, generated_at="2025-06-30T20:00:00Z",
              valuation_date="2025-06-30", extra_tx_columns=COLS)
    old = time.time() - 3600
    os.utime(dst, (old, old))
    with TestClient(build_app(cfg, start_scheduler=False)) as c:
        ctx = c.app.state.ctx
        assert tasks.import_check(ctx, "test").status == "imported"
        (run,) = AP.last_runs(ctx.db)  # Importbericht entsteht automatisch (Autopilot aus → ohne Aktionen)
        assert run["trigger"] == "import" and run["s"]["rules"]["count"] == 0 and run["s"]["hints"] >= 2
        page = c.get("/quality/diagnose").text
        assert "Letzter Importbericht" in page and "/quality/autopilot" in page
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        assert "Noch keine Regeln" in c.get("/quality/diagnose/regeln").text
        r = c.post("/quality/diagnose/regeln/neu", follow_redirects=True,
                   data={"csrf_token": token, "kind": "inactive", "max_status": "hinweis", "account": "Wallet Alt"})
        assert r.status_code == 200 and "Regel #1 angelegt" in r.text and "trifft jetzt 1" in r.text
        r = c.post("/quality/diagnose/regeln/anwenden", data={"csrf_token": token}, follow_redirects=True)
        assert "1 Befund(e) nach Regeln" in r.text and "Letzte Anwendungen" in r.text
        assert len(hints(ctx)) == 1
        tok = R.recent(ctx.db)[0]["token"]
        c.post(f"/quality/diagnose/regeln/zurueck/{tok}", data={"csrf_token": token})
        assert len(hints(ctx)) == 2
        # Autopilot: Korrekturen nur mit Bestätigung
        bad = c.post("/quality/autopilot/einstellungen", data={"csrf_token": token, "enabled": "1", "fixes": "1"},
                     follow_redirects=True)
        assert "bitte bestätigen" in bad.text and not ctx.settings.get("autopilot.enabled")
        ok = c.post("/quality/autopilot/einstellungen", data={"csrf_token": token, "enabled": "1", "rules": "1"},
                    follow_redirects=True)
        assert "Einstellung gespeichert" in ok.text and ctx.settings.get("autopilot.enabled") is True
        assert ctx.settings.get("autopilot.fixes") is False
        c.post("/quality/autopilot/lauf", data={"csrf_token": token})
        assert len(hints(ctx)) == 1  # Regel (nur „Wallet Alt“) wurde im Durchgang angewandt
        last = AP.last_runs(ctx.db, 1)[0]
        assert c.post(f"/quality/autopilot/{last['token']}/undo", data={"csrf_token": token},
                      follow_redirects=True).status_code == 200
        assert len(hints(ctx)) == 2
        assert c.get("/quality/autopilot").status_code == 200


def test_rules_roundtrip_in_full_export_is_idempotent(cfg):
    from app import fullexport

    ctx = make_ctx(cfg, HINT_ROWS, ASSETS)
    R.create(ctx, "inactive", account="Wallet Alt", note="x")
    files = fullexport.collect(ctx, set())
    assert fullexport.summary(files)["decision_rules"] == 1
    st = __import__("json").loads(files[fullexport.STATE].decode())
    assert st["decision_rules"][0]["account"] == "Wallet Alt"
    with ctx.db.transaction() as c:
        for _ in range(2):  # zweimal einspielen → keine Dublette
            fullexport._insert(c, "decision_rule", st["decision_rules"], "OR IGNORE")
    assert ctx.db.scalar("SELECT COUNT(*) FROM decision_rule") == 1
    assert A  # Import genutzt
