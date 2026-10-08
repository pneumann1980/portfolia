"""AP5 – Steuerliche Referenzfälle (Regelwerk Deutschland) mit vorab festgelegten Erwartungswerten.

Ergänzt ``test_tax_de`` (Haltefrist/Schaltjahr, Freigrenzen, Gebühren, Dividenden/Quellensteuer, Teilfreistellung,
Fehlbestand, Staking). Alle Beträge exakt (Cent); rechtlich nicht eindeutige Fälle (Token-Migration, Zugang ohne
nachgewiesene Anschaffung) müssen als solche gekennzeichnet sein statt still als steuerfrei/neutral zu gelten.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tests.helpers import tx
from tests.test_tax_de import TAX_ASSETS, buy, run, sell

D = Decimal


def rows_of(res, section="crypto23"):
    s = res.section(section)
    return [r for t in s.tables for r in t.rows if "kind" in r] if s else []


@pytest.mark.parametrize(("sell_day", "taxable", "net", "free_gain"), [
    ("2025-03-01", True, D("500.00"), D(0)),     # Jahrestag der Anschaffung → noch steuerpflichtig
    ("2025-03-02", False, D(0), D("500.00")),    # Tag danach → steuerfrei
])
def test_crypto_holding_period_reference(sell_day, taxable, net, free_gain):
    rows = [buy("b", "2024-03-01", "ETH", 1, 1000), sell("s", sell_day, "ETH", 1, 1500)]
    _, res = run(rows, 2025)
    c = res.data["crypto"]
    assert c["count_tax"] == (1 if taxable else 0) and c["net"] == net and c["free_gain"] == free_gain


def test_fifo_over_multiple_lots_splits_free_and_taxable():
    rows = [buy("b1", "2024-01-10", "BTC", 1, 1000), buy("b2", "2024-06-10", "BTC", 1, 2000),
            sell("s", "2025-02-01", "BTC", "1.5", 4500)]
    _, res = run(rows, 2025)
    c = res.data["crypto"]
    assert c["free_gain"] == D("2000.00")       # 1,0 aus Jan. 2024: 3.000 − 1.000, Haltefrist abgelaufen
    assert c["net"] == D("500.00")              # 0,5 aus Juni 2024: 1.500 − 1.000
    assert c["taxable"] == 0                    # unter Freigrenze 2025 (1.000 €)


def test_crypto_swap_is_disposal_and_new_acquisition():
    rows = [buy("b", "2025-01-05", "BTC", 1, 30000),
            tx("t", "2025-04-01", "trade", frm=("Börse", "BTC", "1"), to=("Börse", "ETH", "10"), value=35000),
            sell("s", "2025-08-01", "ETH", "10", 36000)]
    _, res = run(rows, 2025)
    c = res.data["crypto"]
    assert c["net"] == D("6000.00") and c["taxable"] == D("6000.00")  # 5.000 aus Tausch + 1.000 aus Verkauf
    tausch = next(r for r in rows_of(res) if r["kind"] == "Tausch")
    assert tausch["cost"] == D("30000.00") and tausch["price"] == D("35000.00")


def test_internal_transfer_keeps_holding_period():
    rows = [buy("b", "2024-01-01", "ETH", 1, 1000),
            tx("t", "2024-06-01", "transfer", frm=("Börse", "ETH", "1"), to=("Wallet", "ETH", "1")),
            sell("s", "2025-02-01", "ETH", 1, 3000, acc="Wallet")]
    _, res = run(rows, 2025)  # Standard: FIFO je Wallet – Lot wandert mit Anschaffungsdatum
    c = res.data["crypto"]
    assert c["count_free"] == 1 and c["free_gain"] == D("2000.00") and c["count_tax"] == 0


def test_staking_income_then_sale_reference():
    rows = [tx("r", "2025-05-01", "deposit", tag="staking", to=("Börse", "KAS", "1000"), value=50),
            sell("s", "2025-08-01", "KAS", "1000", 80)]
    _, res = run(rows, 2025)
    assert res.data["income"]["total"] == D("50.00")          # § 22 Nr. 3 zum Zuflusswert
    assert res.data["crypto"]["net"] == D("30.00")             # Einstand = Zuflusswert


def test_total_loss_is_distinguishable_and_option_controlled():
    rows = [buy("b", "2025-01-10", "KAS", 1000, 400),
            tx("l", "2025-06-01", "withdrawal", tag="lost", frm=("Börse", "KAS", "1000"), value=0)]
    _, res = run(rows, 2025)
    assert res.data["crypto"]["count_tax"] == 0 and res.data["crypto"]["excluded"] == {"lost": 1}
    _, res = run(rows, 2025, options={"lost": "loss"})
    assert res.data["crypto"]["net"] == D("-400.00")
    assert rows_of(res)[0]["kind"] == "Verlust"  # nicht mit einem Verkauf verwechselbar


def test_transfer_without_prior_acquisition_never_tax_free():
    """Regression: Lot ohne Anschaffung (Zugang per Transfer ohne Lots) galt nach einem Jahr als steuerfrei."""
    rows = [tx("t", "2023-01-10", "transfer", frm=("Börse", "ETH", "1"), to=("Wallet", "ETH", "1")),
            sell("s", "2025-02-01", "ETH", 1, 3000, acc="Wallet")]
    _, res = run(rows, 2025)
    c = res.data["crypto"]
    assert c["count_free"] == 0 and c["count_tax"] == 1 and c["net"] == D("3000.00")
    assert any(i.code == "missing_basis" for i in res.issues)


def test_migration_keeps_basis_but_is_flagged_as_unclear():
    assets = [*TAX_ASSETS, {"asset_id": "POL", "name": "POL", "asset_class": "crypto",
                            "quote_source": "none", "quote_id": ""}]
    rows = [buy("b", "2023-03-01", "KAS", 100, 50),
            tx("m", "2024-09-04", "corporate_action", tag="migration", frm=("Börse", "KAS", "100"),
               to=("Börse", "POL", "100")),
            sell("s", "2025-02-01", "POL", 100, 90)]
    import tests.test_tax_de as T

    orig = T.TAX_ASSETS
    T.TAX_ASSETS = assets
    try:
        _, res = run(rows, 2025)
    finally:
        T.TAX_ASSETS = orig
    c = res.data["crypto"]
    assert c["count_free"] == 1 and c["free_gain"] == D("40.00")  # technisch: Anschaffung 01.03.2023
    issue = next(i for i in res.issues if i.code == "conversion_unclear")
    assert "KAS → POL" in issue.text


def test_year_boundary_uses_local_date():
    rows = [buy("b", "2025-06-01", "BTC", 1, 100), sell("s", "2025-12-31T23:30:00Z", "BTC", 1, 1300)]
    _, res25 = run(rows, 2025)
    _, res26 = run(rows, 2026)
    assert res25.data["crypto"]["count_tax"] == 0           # 00:30 am 1.1.2026 Ortszeit
    assert res26.data["crypto"]["net"] == D("1200.00")


def test_share_sale_and_etf_partial_exemption_reference():
    rows = [buy("a1", "2025-01-10", "US1", 10, 1000, acc="IBKR"), sell("a2", "2025-06-10", "US1", 10, 1500,
                                                                         acc="IBKR"),
            buy("f1", "2025-01-10", "FUND", 10, 1000, acc="IBKR"), sell("f2", "2025-06-10", "FUND", 10, 1300,
                                                                         acc="IBKR")]
    _, res = run(rows, 2025, kinds={"IBKR": "foreign"}, options={"pauschbetrag_used": "1000"})
    est = {ln.label: ln.amount for ln in res.estimate}
    assert est["Investmentfonds nach Teilfreistellung"] == D("210.00")  # 300 × (1 − 30 %)
    f = {x.field_id: x.amount for x in res.fields}
    assert f["kap_foreign_share_gains"] == D("500.00")


def test_identical_records_from_two_sources_are_not_deduplicated_by_tax():
    """Dubletten müssen *vor* der Steuer erkannt werden (Abgleich, AP3); die Steuer zählt, was gebucht ist."""
    rows = [buy("b", "2025-01-10", "ETH", 2, 2000), sell("s1", "2025-03-01", "ETH", 1, 1500),
            sell("s2", "2025-03-01", "ETH", 1, 1500)]
    _, res = run(rows, 2025)
    assert res.data["crypto"]["count_tax"] == 2 and res.data["crypto"]["net"] == D("1000.00")


def test_rule_pack_version_and_parameters_recorded():
    rows = [buy("b", "2025-01-10", "ETH", 1, 1000), sell("s", "2025-06-10", "ETH", 1, 1200)]
    pack, res = run(rows, 2025)
    assert res.pack_id == "de" and res.pack_version == pack.version and res.params_version
    assert res.params["crypto"]["holding_period_years"] == 1
    assert D(str(res.params["crypto"]["freigrenze_23"])) == D(1000)
    _, old = run(rows, 2023)
    assert D(str(old.params["crypto"]["freigrenze_23"])) == D(600)  # Parameter je Steuerjahr
    assert date(2025, 1, 1) <= date(2025, 6, 10)


# ----------------------------------------------------------------------------------------------------------------------
# M24/AP5.2 – weitere Referenzfälle (Sollwerte aus den Eingaben nachgerechnet)
# ----------------------------------------------------------------------------------------------------------------------

def test_partial_sale_over_three_lots_with_different_dates():
    """Lots: 01.02.2024 (1 @ 100), 01.08.2024 (1 @ 200), 01.01.2025 (1 @ 300); Verkauf 2,5 am 15.03.2025 für 1.000.
    FIFO: Lot 1 frei (400 − 100 = 300), Lot 2 steuerpflichtig (400 − 200 = 200), ½ Lot 3 (200 − 150 = 50)."""
    rows = [buy("b1", "2024-02-01", "ETH", 1, 100), buy("b2", "2024-08-01", "ETH", 1, 200),
            buy("b3", "2025-01-01", "ETH", 1, 300), sell("s", "2025-03-15", "ETH", "2.5", 1000)]
    _, res = run(rows, 2025)
    c = res.data["crypto"]
    assert c["free_gain"] == D("300.00") and c["net"] == D("250.00")


def test_partial_transfer_between_own_wallets_keeps_lots():
    """2 Käufe auf der Börse, 1,5 ETH ins Wallet (FIFO je Wallet: Lot 1 ganz + ½ Lot 2), Verkauf 1,5 aus dem Wallet am
    01.02.2025 für 4.500 €: Lot 1 (10.01.2024) frei 3.000 − 1.000 = 2.000; ½ Lot 2 (10.06.2024) 1.500 − 1.000 = 500."""
    rows = [buy("b1", "2024-01-10", "ETH", 1, 1000), buy("b2", "2024-06-10", "ETH", 1, 2000),
            tx("t", "2024-07-01", "transfer", frm=("Börse", "ETH", "1.5"), to=("Wallet", "ETH", "1.5")),
            sell("s", "2025-02-01", "ETH", "1.5", 4500, acc="Wallet")]
    _, res = run(rows, 2025)
    c = res.data["crypto"]
    assert c["free_gain"] == D("2000.00") and c["net"] == D("500.00")


def test_buy_fee_in_third_currency_raises_cost_and_is_a_disposal():
    """Kauf 1 ETH für 1.000 € + 0,01 BNB Gebühr (4 €; BNB-Einstand 3 €): ETH-Anschaffungskosten 1.004 €, die
    Gebühr ist eine Veräußerung von BNB (Gewinn 1 €). Verkauf ETH für 1.500 € → 496 €; Summe 497 €."""
    rows = [buy("b2", "2025-01-02", "BNB", 1, 300),
            tx("b1", "2025-01-10", "buy", frm=("Börse", "EUR", "1000"), to=("Börse", "ETH", "1"), value=1000,
               fee=("BNB", "0.01", "4.00")),
            sell("s", "2025-06-01", "ETH", 1, 1500)]
    _, res = run(rows, 2025)
    sale = next(r for r in rows_of(res) if r["kind"] == "Verkauf")
    assert sale["cost"] == D("1004.00") and sale["gain"] == D("496.00")
    assert res.data["crypto"]["net"] == D("497.00")


def test_retroactive_earlier_purchase_changes_fifo_transparently():
    """Ohne frühen Kauf: Verkauf 02/2025 aus Lot 06/2024 (steuerpflichtig 1.000). Nachträglich erfasster Kauf aus
    01/2023 wird nach FIFO zuerst verbraucht → steuerfrei 2.500, steuerpflichtig 0."""
    base = [buy("b2", "2024-06-10", "ETH", 1, 2000), sell("s", "2025-02-01", "ETH", 1, 3000)]
    _, before = run(base, 2025)
    assert before.data["crypto"]["net"] == D("1000.00") and before.data["crypto"]["free_gain"] == D(0)
    _, after = run([buy("b0", "2023-01-15", "ETH", 1, 500), *base], 2025)
    assert after.data["crypto"]["net"] == D(0) and after.data["crypto"]["free_gain"] == D("2500.00")


def _ctx_tax(ctx, year):
    from app.tax.service import tax_service

    ctx.invalidate_data()
    _p, _i, res = tax_service(ctx).compute(year)
    return res.data["crypto"]


def test_repeated_import_and_duplicate_cleanup_keep_tax_data_traceable(tmp_path):
    """Gleicher Import zweimal → identisches Steuerergebnis. Dublettenbereinigung (doppelter Kauf) ändert
    Anschaffungsdaten nur sichtbar: Vorschau zeigt Lots und Steuerwerte vorher/nachher."""
    from app.config import Config, Secrets
    from app.diagnosis import actions as A
    from app.diagnosis.engine import report_for
    from app.importer.loader import import_file
    from app.importer.zipbuilder import build_zip
    from tests.test_diagnosis import make_ctx

    cfg = Config(data_dir=tmp_path / "data", import_dir=tmp_path / "imp", demo_mode=True, scheduler_enabled=False,
                 startup_jobs=False, log_format="text", secrets=Secrets())
    cfg.import_dir.mkdir(parents=True)
    k = tx("K1", "2024-01-05T10:00:00Z", "buy", frm=("Börse", "EUR", "100"), to=("Börse", "ETH", "0.1"), value=100)
    rows = [tx("F", "2024-01-01", "deposit", to=("Börse", "EUR", "5000"), value=5000), k, {**k, "tx_id": "K2"},
            buy("L", "2024-12-20", "ETH", "0.1", 300), sell("S", "2025-02-01", "ETH", "0.2", 600)]
    ctx = make_ctx(cfg, rows, TAX_ASSETS)
    first = _ctx_tax(ctx, 2025)
    dst = cfg.import_dir / "nochmal.zip"
    build_zip(dst, transactions=rows, assets=TAX_ASSETS, generated_at="2025-06-30T21:00:00Z",
              valuation_date="2025-06-30", extra_tx_columns=["source", "source_ref", "flag", "note"])
    assert import_file(ctx.db, dst, ctx.engine_options()).status == "imported"
    assert _ctx_tax(ctx, 2025) == first  # wiederholter Import: nichts doppelt
    # vorher: K1 + K2 (je 0,1, 05.01.2024) werden verkauft → steuerfrei 2 × (300 − 100) = 400
    assert first["free_gain"] == D("400.00") and first["count_tax"] == 0
    rep = report_for(ctx)
    f = next(x for x in rep.findings if x.kind == "duplicate")
    plan = A.build_plan(ctx, rep, f, "hide_second")
    eff = A.preview(ctx, rep, plan)
    assert eff.tax and any(t.year == 2025 for t in eff.tax)  # Steuerwirkung wird vorab gezeigt
    assert A.apply(ctx, f.id, "hide_second", plan.params, plan.token).ok
    # nachher: K1 (05.01.2024, frei 200) + L (20.12.2024, steuerpflichtig 300 − 300 = 0) – K1 behält sein Datum
    after = _ctx_tax(ctx, 2025)
    assert after["free_gain"] == D("200.00") and after["count_tax"] == 1 and after["net"] == D("0.00")
