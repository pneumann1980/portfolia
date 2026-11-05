"""0.98.0 Stufe 2: Stichtag-Vorschau (Seite, Eröffnungsstand, Belege, Pflichtprüfungen)."""

# ruff: noqa: F811
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.cutover import core as CO
from app.cutover import service as CS
from tests.test_bitpanda import ctx
from tests.test_review_fixes import c, cfg  # noqa: F401


def test_parse_cutoff():
    assert CS.parse_cutoff("2025-03-01") == date(2025, 3, 1)
    assert CS.parse_cutoff("") == date(2025, 1, 1)
    assert CS.parse_cutoff("2025-02-30") == date(2025, 1, 1)
    assert CS.parse_cutoff("x", date(2024, 5, 5)) == date(2024, 5, 5)


def test_preview_checks_pass_on_the_sample_data(c):
    cx = ctx(c)
    for cutoff in (date(2024, 6, 1), date(2025, 1, 1), date(2030, 1, 1), date(2000, 1, 1)):
        pv = CS.preview(cx, cutoff)
        assert pv["has_data"], cutoff
        bad = [(k.key, k.detail) for k in pv["checks"] if not k.ok]
        assert pv["ok"], (cutoff, bad)


def test_overview_counts_and_account_status(c):
    cx = ctx(c)
    pv = CS.preview(cx, date(2025, 1, 1))
    ov = pv["overview"]
    assert ov["archive"]["txs"] + ov["archive"]["active_txs"] == len(cx.portfolio().txs)
    assert ov["n_lots"] == len(ov["rows"]) or ov["n_lots"] >= len(ov["rows"])
    assert all(a["status"] in ("belegt", "abweichung", "ohne_beleg") and len(a["fingerprint"]) == 24
               for a in pv["accounts"])
    # Fiat taucht im Eröffnungsstand nicht auf
    assert not any(cx.portfolio().asset(r["asset"]).is_fiat for r in ov["rows"])


def test_account_status_uses_reference_balances_at_the_day_before(c):
    cx = ctx(c)
    cutoff = date(2025, 2, 1)  # Beispieldaten: Zugang am 01.01.2025 → Bestand am 31.01.2025
    m = CO.models(cx, cutoff)[0]
    assert m is not None
    key = next(k for k, v in m.state.balances.items() if v and not cx.portfolio().asset(k[1]).is_fiat)
    acc, asset = key
    have = m.state.balances[key]
    cx.db.x("INSERT INTO reference_balance(account, asset_id, qty, as_of, source, created_at) VALUES (?,?,?,?,?,?)",
            (acc, asset, str(have), "2025-01-31", "statement", "2026-01-01"))
    cx.invalidate_overlay()
    pv = CS.preview(cx, cutoff)
    st = next(a for a in pv["accounts"] if a["account"] == acc)
    assert st["status"] == "belegt" and st["refs"] == 1
    cx.db.x("UPDATE reference_balance SET qty=? WHERE account=? AND asset_id=?", (str(have + Decimal("1")), acc, asset))
    cx.invalidate_overlay()
    pv = CS.preview(cx, cutoff)
    assert next(a for a in pv["accounts"] if a["account"] == acc)["status"] == "abweichung"
    assert any(not r["ok"] for r in pv["refs"])
    cx.db.x("UPDATE reference_balance SET qty=? WHERE account=? AND asset_id=?",
            (str(have + Decimal("1e-9")), acc, asset))
    cx.invalidate_overlay()
    pv = CS.preview(cx, cutoff)  # Rundungsrest = Staub: nicht als Abweichung gezählt
    assert next(a for a in pv["accounts"] if a["account"] == acc)["status"] == "belegt"


def test_page_renders_and_is_read_only(c):
    cx = ctx(c)
    before = (cx.db.scalar("SELECT COUNT(*) FROM journal_tx"), cx.db.scalar("SELECT COUNT(*) FROM tx_override"),
              cx.db.q1("SELECT group_concat(name || ':' || n) FROM data_rev WHERE name <> 'input' "
                       "AND name NOT LIKE 'pf:%'")[0])
    page = c.get("/settings/stichtag?cutoff=2025-01-01")
    assert page.status_code == 200
    for needle in ("Pflichtprüfungen", "Eröffnungsstand je Konto", "Veräußerungen ab Stichtag", "31.12.2024"):
        assert needle in page.text, needle
    assert c.get("/settings/stichtag?cutoff=quatsch").status_code == 200  # fällt auf den Standard zurück
    after = (cx.db.scalar("SELECT COUNT(*) FROM journal_tx"), cx.db.scalar("SELECT COUNT(*) FROM tx_override"),
             cx.db.q1("SELECT group_concat(name || ':' || n) FROM data_rev WHERE name <> 'input' "
                      "AND name NOT LIKE 'pf:%'")[0])
    assert before == after
    assert "/settings/stichtag" in c.get("/settings").text


def test_impact_counts_findings_before_the_cutoff(c):
    cx = ctx(c)
    pv = CS.preview(cx, date(2025, 1, 1), impact=True)
    imp = pv["impact"]
    assert imp["total"] == sum(imp["split"].values()) and 0 <= imp["dropped"] <= imp["total"]
    page = c.get("/settings/stichtag?cutoff=2025-01-01&impact=1")
    assert page.status_code == 200 and "Hauptbefunden" in page.text


def test_model_is_cached_and_follows_data_changes(c):
    cx = ctx(c)
    m1 = CO.models(cx, date(2025, 1, 1))[0]
    assert CO.models(cx, date(2025, 1, 1))[0] is m1
    cx.settings.set("ledger.fiat_off", True)
    cx.invalidate_data()
    m2 = CO.models(cx, date(2025, 1, 1))[0]
    assert m2 is not m1 and m2.ok


def test_crossing_transfer_hint():
    from datetime import timedelta

    from app.ledger.engine import EngineOptions, run_ledger
    from tests.helpers import portfolio, tx

    rows = [
        tx("B1", "2024-12-01T10:00:00Z", "buy", frm=("Börse", "EUR", "100"), to=("Börse", "BTC", "1"), value="100"),
        tx("W1", "2024-12-30T10:00:00Z", "withdrawal", frm=("Börse", "BTC", "1"), value="120"),
        tx("D1", "2025-01-02T10:00:00Z", "deposit", to=("Wallet", "BTC", "1"), value="121"),
    ]
    pf = portfolio(rows)
    cutoff = date(2025, 1, 1)
    full = run_ledger(pf, EngineOptions(snapshot_dates=(cutoff - timedelta(days=1),)))
    st = full.state_snapshots[cutoff - timedelta(days=1)]
    act = run_ledger(CO.active_portfolio(pf, cutoff, st), EngineOptions())
    m = CO.Model(cutoff, EngineOptions(), pf, full, st, CO.active_portfolio(pf, cutoff, st), act, 2, 1)
    hints = CO.crossing_transfers(m)
    assert len(hints) == 1 and hints[0]["asset"] == "BTC" and hints[0]["out_account"] == "Börse"
