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
