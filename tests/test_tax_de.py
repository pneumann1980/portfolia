"""Steuer-Regelwerk Deutschland: Haltefrist, Freigrenzen je Jahr, Verbrauchsfolge, Töpfe, Fonds, Parameter."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.ledger.engine import EngineOptions, run_ledger
from app.tax import registry
from app.tax.base import TaxInput
from app.tax.classify import classify_asset
from app.tax.params import ParamSet
from tests.helpers import ASSETS, portfolio, tx

D = Decimal
TAX_ASSETS = [
    *ASSETS,
    {"asset_id": "US1", "name": "Alpha Inc.", "asset_class": "security", "isin": "US0000000001",
     "quote_source": "none", "quote_id": ""},
    {"asset_id": "US2", "name": "Beta Corp.", "asset_class": "security", "isin": "US0000000002",
     "quote_source": "none", "quote_id": ""},
    {"asset_id": "BOND", "name": "Anleihe 2030", "asset_class": "security", "isin": "DE0000000003",
     "quote_source": "none", "quote_id": "", "extra": {"tax_type": "bond"}},
    {"asset_id": "FUND", "name": "Welt ETF", "asset_class": "security", "isin": "IE0000000004",
     "quote_source": "none", "quote_id": "", "extra": {"tax_type": "etf_equity"}},
]


def run(rows, year, *, options=None, kinds=None, year_prices=None, today=date(2026, 9, 26)):
    pack = registry.get("de")
    pf = portfolio(rows, TAX_ASSETS)
    opts = {**pack.defaults(), **(options or {})}
    led = run_ledger(pf, pack.engine_options(EngineOptions(), opts, list(range(2018, today.year))))
    types, tsrc = {}, {}
    for aid, a in pf.assets.items():
        types[aid], tsrc[aid] = classify_asset(a)
    accs = pf.all_accounts()
    inp = TaxInput(pf=pf, ledger=led, today=today, asset_types=types, asset_type_source=tsrc,
                   account_kinds={a: (kinds or {}).get(a, "domestic") for a in accs},
                   account_kind_source=dict.fromkeys(accs, "settings"),
                   year_prices=(lambda aid, y: (year_prices or {}).get((aid, y), (None, None))))
    return pack, pack.compute(inp, year, opts)


def buy(i, d, asset, qty, eur, acc="Börse"):
    return tx(i, d, "buy", frm=(acc, "EUR", eur), to=(acc, asset, qty), value=eur)


def sell(i, d, asset, qty, eur, acc="Börse"):
    return tx(i, d, "sell", frm=(acc, asset, qty), to=(acc, "EUR", eur), value=eur)


# -- Haltefrist ----------------------------------------------------------------------------------------

def test_holding_period_leap_day_and_boundaries():
    pack = registry.get("de")
    btc = portfolio([], TAX_ASSETS).asset("BTC")
    assert pack.holding_end(btc, date(2024, 2, 29)) == date(2025, 3, 1)  # Fristende 28.02., frei ab 01.03.
    assert pack.holding_end(btc, date(2023, 3, 15)) == date(2024, 3, 16)
    assert pack.holding_end(portfolio([], TAX_ASSETS).asset("US1"), date(2023, 3, 15)) is None
    rows = [buy("b1", "2024-02-29", "BTC", "0.1", 1000),
            sell("s1", "2025-02-28", "BTC", "0.05", 600),   # Jahrestag (Fristende) → steuerpflichtig
            sell("s2", "2025-03-01", "BTC", "0.05", 700)]   # Tag danach → steuerfrei
    _, res = run(rows, 2025)
    c = res.data["crypto"]
    assert c["count_tax"] == 1 and c["net"] == D("100.00")
    assert c["count_free"] == 1 and c["free_gain"] == D("200.00")
    assert c["taxable"] == 0  # unter Freigrenze 2025 (1.000 €)


@pytest.mark.parametrize(("year", "gain", "taxable"), [
    (2023, "599.99", "0"), (2023, "600.00", "600.00"),       # bis VZ 2023: 600 €
    (2024, "999.99", "0"), (2024, "1000.00", "1000.00"),     # ab VZ 2024: 1.000 €
])
def test_freigrenze_23_by_year(year, gain, taxable):
    rows = [buy("b", f"{year}-01-10", "ETH", 1, 1000), sell("s", f"{year}-06-10", "ETH", 1, D(1000) + D(gain))]
    _, res = run(rows, year)
    c = res.data["crypto"]
    assert c["net"] == D(gain)
    assert c["taxable"] == D(taxable)
    meter = next(m for m in res.meters if m.id == "fg23")
    assert meter.state == ("crit" if D(taxable) > 0 else ("warn" if D(gain) >= D("0.8") * meter.limit else "ok"))


@pytest.mark.parametrize(("value", "taxable"), [("255.99", "0"), ("256.00", "256.00")])
def test_freigrenze_22_3(value, taxable):
    rows = [tx("r1", "2025-05-01", "deposit", tag="staking", to=("Börse", "ETH", "0.1"), value=value)]
    _, res = run(rows, 2025)
    assert res.data["income"]["total"] == D(value)
    assert res.data["income"]["taxable"] == D(taxable)
    assert any(f.field_id == "so_22_3_income" and f.amount == D(value) for f in res.fields)


def test_loss_carryforward_and_losses_stay_in_23():
    rows = [buy("b", "2025-01-10", "ETH", 1, 1000), sell("s", "2025-06-10", "ETH", 1, 3000)]
    _, res = run(rows, 2025, options={"loss_cf_23": "1500"})
    assert res.data["crypto"]["taxable"] == D("2000.00") and res.data["crypto"]["after_lcf"] == D("500.00")
    rows = [buy("b", "2025-01-10", "ETH", 1, 1000), sell("s", "2025-06-10", "ETH", 1, 400)]
    _, res = run(rows, 2025)
    assert res.data["crypto"]["net"] == D("-600.00") and res.data["crypto"]["taxable"] == 0
    assert any("Verlust § 23" in ln.label for ln in res.estimate)


# -- Verbrauchsfolge, Gebühren, Datenlücken ------------------------------------------------------------

def test_wallet_fifo_vs_global_fifo():
    rows = [buy("b1", "2022-01-10", "BTC", 1, 30000, acc="Wallet A"),
            buy("b2", "2024-01-10", "BTC", 1, 40000, acc="Wallet B"),
            sell("s1", "2024-06-10", "BTC", 1, 60000, acc="Wallet B")]
    _, res = run(rows, 2024)  # Standard: FIFO je Wallet → Lot aus Wallet B (2024) → steuerpflichtig
    assert res.data["crypto"]["count_tax"] == 1 and res.data["crypto"]["net"] == D("20000.00")
    _, res = run(rows, 2024, options={"scope": "global"})  # global: ältestes Lot (2022) → steuerfrei
    assert res.data["crypto"]["count_tax"] == 0 and res.data["crypto"]["free_gain"] == D("30000.00")


def test_fee_and_transfer_fee_treatment():
    rows = [buy("b1", "2025-01-02", "ETH", 1, 2000), buy("b2", "2025-01-02", "BNB", 1, 300),
            tx("t1", "2025-03-01", "trade", frm=("Börse", "ETH", "0.5"), to=("Börse", "KAS", 5000), value=1100,
               fee=("BNB", "0.01", "4.00")),
            tx("t2", "2025-04-01", "transfer", frm=("Börse", "ETH", "0.1"), to=("Wallet", "ETH", "0.1"),
               fee=("ETH", "0.001", "2.50"))]
    _, res = run(rows, 2025)
    rows_tax = res.section("crypto23").tables[1].rows
    kinds = sorted(r["kind"] for r in rows_tax)
    assert kinds == ["Gebühr", "Tausch"]  # Transfergebühr standardmäßig nicht steuerbar
    trade = next(r for r in rows_tax if r["kind"] == "Tausch")
    assert trade["price"] == D("1100.00") and trade["wk"] == D("4.00") and trade["cost"] == D("1000.00")
    assert trade["gain"] == D("96.00")
    fee = next(r for r in rows_tax if r["kind"] == "Gebühr")
    assert fee["price"] == D("4.00") and fee["cost"] == D("3.00") and fee["gain"] == D("1.00")
    _, res2 = run(rows, 2025, options={"crypto_fee": "ignore", "transfer_fee": "taxable"})
    kinds2 = sorted(r["kind"] for r in res2.section("crypto23").tables[1].rows)
    assert kinds2 == ["Tausch", "Transfergebühr"]


def test_missing_basis_is_conservative():
    rows = [sell("s1", "2025-05-05", "KAS", 1000, 150)]
    _, res = run(rows, 2025)
    r = res.section("crypto23").tables[1].rows[0]
    assert r["cost"] == 0 and r["gain"] == D("150.00") and r["acq"] is None
    assert any(i.code == "missing_basis" for i in res.issues)


def test_income_classification_options_and_new_holding_period():
    rows = [tx("r1", "2024-05-01", "deposit", tag="staking", to=("Börse", "ETH", "0.1"), value=300),
            tx("r2", "2024-05-02", "deposit", tag="cashback", to=("Börse", "ETH", "0.01"), value=30),
            sell("s1", "2025-05-02", "ETH", "0.1", 350)]  # Staking-Lot 01.05.2024 → frei ab 02.05.2025
    _, res = run(rows, 2024)
    assert res.data["income"]["total"] == D("300.00")  # Cashback standardmäßig nicht steuerbar
    _, res = run(rows, 2024, options={"income_map": {"cashback": "22_3"}})
    assert res.data["income"]["total"] == D("330.00")
    _, res = run(rows, 2025)
    assert res.data["crypto"]["count_free"] == 1 and res.data["crypto"]["free_gain"] == D("50.00")


# -- Kapitalerträge ------------------------------------------------------------------------------------

def _kap_rows():
    return [
        buy("k1", "2025-01-10", "US1", 10, 1000, acc="IBKR"), sell("k2", "2025-06-10", "US1", 10, 2000, acc="IBKR"),
        buy("k3", "2025-01-10", "US2", 10, 1000, acc="IBKR"), sell("k4", "2025-06-11", "US2", 10, 700, acc="IBKR"),
        buy("k5", "2025-01-10", "BOND", 1, 1000, acc="IBKR"), sell("k6", "2025-06-12", "BOND", 1, 800, acc="IBKR"),
        tx("k7", "2025-03-01", "deposit", tag="dividend", to=("IBKR", "EUR", 100), value=100, related="US1"),
        tx("k8", "2025-03-01", "withdrawal", tag="withholding_tax", frm=("IBKR", "EUR", 15), value=15, related="US1"),
    ]


def test_capital_pots_fields_and_estimate_foreign_account():
    _, res = run(_kap_rows(), 2025, kinds={"IBKR": "foreign"}, options={"pauschbetrag_used": "1000"})
    f = {x.field_id: x.amount for x in res.fields}
    assert f["kap_foreign_total"] == D("600.00")  # 100 + 1000 − 300 − 200
    assert f["kap_foreign_share_gains"] == D("1000.00")
    assert f["kap_foreign_losses_shares"] == D("300.00")
    assert f["kap_foreign_losses_other"] == D("200.00")
    assert f["kap_wht"] == D("15.00")
    est = {ln.label: ln.amount for ln in res.estimate}
    assert est["Bemessungsgrundlage"] == D("600.00")
    assert est["Abgeltungsteuer nach Anrechnung Quellensteuer"] == D("135.00")  # (600 − 4 × 15) / 4
    assert est["Solidaritätszuschlag"] == D("7.43")


def test_sparer_pauschbetrag_and_domestic_accounts_are_informational():
    _, res = run(_kap_rows(), 2025, kinds={"IBKR": "foreign"})
    est = {ln.label: ln.amount for ln in res.estimate}
    assert est["Bemessungsgrundlage"] == 0  # 600 € < Sparer-Pauschbetrag 1.000 €
    _, res = run(_kap_rows(), 2025)  # Inland: Bank hat abgerechnet → keine Formularfelder, nur nachrichtlich
    assert not any(x.form == "Anlage KAP" for x in res.fields)
    assert any("nachrichtlich" in ln.label for ln in res.summary)


def test_share_losses_only_offset_share_gains():
    rows = [buy("a", "2025-01-10", "US2", 10, 1000, acc="IBKR"), sell("b", "2025-06-11", "US2", 10, 500, acc="IBKR"),
            tx("c", "2025-03-01", "deposit", tag="dividend", to=("IBKR", "EUR", 300), value=300, related="US1")]
    _, res = run(rows, 2025, kinds={"IBKR": "foreign"}, options={"pauschbetrag_used": "1000"})
    est = {ln.label: ln.amount for ln in res.estimate}
    assert est["Bemessungsgrundlage"] == D("300.00")
    assert est["Verbleibender Verlust Aktien (Vortrag)"] == D("500.00")


def test_fund_teilfreistellung_and_vorabpauschale():
    prices = {("FUND", 2024): ((date(2024, 1, 2), D(100)), (date(2024, 12, 30), D(110)))}
    rows = [buy("f1", "2024-03-10", "FUND", 10, 1000, acc="IBKR"),
            tx("f2", "2025-02-01", "deposit", tag="dividend", to=("IBKR", "EUR", 100), value=100, related="FUND"),
            sell("f3", "2025-05-02", "FUND", 10, 1200, acc="IBKR")]
    _, res = run(rows, 2025, kinds={"IBKR": "foreign"}, year_prices=prices, options={"pauschbetrag_used": "1000"})
    b = res.data["capital"]["B"]["foreign"]
    # VP 2024 je Anteil: min(100 × 2,29 % × 0,7; 110 − 100) = 1,603; Erwerb im März → 10/12
    vp = D(10) * D("100") * D("0.0229") * D("0.7") * D(10) / D(12)
    assert b["fund_vp"]["etf_equity"] == vp
    assert b["fund_gain"]["etf_equity"] == D("200") - vp  # Veräußerungsgewinn abzüglich angesetzter VP
    assert b["fund_dist"]["etf_equity"] == D("100")
    fields = {x.label: x.amount for x in res.fields if x.form == "Anlage KAP-INV"}
    assert fields["Vorabpauschalen – Aktienfonds"] == D("13.36")
    assert fields["Ausschüttungen – Aktienfonds"] == D("100.00")
    est = {ln.label: ln.amount for ln in res.estimate}
    # Teilfreistellung 30 %: (100 + 13,36 + 186,64) × 0,7 = 210,00
    assert est["Investmentfonds nach Teilfreistellung"] == D("210.00")


def test_vp_zero_for_negative_basiszins_and_missing_prices_flagged():
    rows = [buy("f1", "2021-01-05", "FUND", 10, 1000, acc="IBKR")]
    _, res = run(rows, 2022, kinds={"IBKR": "foreign"},
                 year_prices={("FUND", 2021): ((date(2021, 1, 4), D(100)), (date(2021, 12, 30), D(120)))})
    assert res.data["capital"]["B"]["foreign"]["fund_vp"] == {}  # Basiszins 2021 negativ → keine VP
    _, res = run(rows, 2024, kinds={"IBKR": "foreign"})  # VP 2023: Kurse fehlen → Hinweis, 0 €
    assert any(i.code == "vp_missing" for i in res.issues)


# -- Parameter & Registry -------------------------------------------------------------------------------

def test_param_override_and_errors(tmp_path: Path):
    bundled = registry.PACKS_DIR / "de" / "params.yaml"
    ps = ParamSet("de", bundled, tmp_path)
    assert ps.for_year(2023)["crypto"]["freigrenze_23"] == 600
    assert ps.for_year(2025)["crypto"]["freigrenze_23"] == 1000
    assert ps.for_year(2025)["basiszins"] == 0.0253 and ps.for_year(2026)["basiszins"] is None
    assert ps.for_year(2022)["capital"]["sparer_pauschbetrag_single"] == 801
    (tmp_path / "de.yaml").write_text(
        "pack: {reviewed_through: 2027}\n"
        "rules:\n  2026:\n    crypto: {freigrenze_23: 2000}\n"
        "per_year:\n  basiszins: {2026: 0.032}\n"
        "forms:\n  2026:\n    anlage_so: {fields: {so_23_gain: {line: '47'}}}\n", encoding="utf-8")
    ps = ParamSet("de", bundled, tmp_path)
    assert ps.override_active and ps.version.endswith("+lokal") and ps.override_error is None
    assert ps.for_year(2026)["crypto"]["freigrenze_23"] == 2000
    assert ps.for_year(2025)["crypto"]["freigrenze_23"] == 1000
    assert ps.for_year(2026)["basiszins"] == 0.032
    assert ps.for_year(2026)["forms"]["anlage_so"]["fields"]["so_23_gain"]["line"] == "47"
    assert ps.for_year(2025)["forms"]["anlage_so"]["fields"]["so_23_gain"]["line"] is None
    assert 2027 in ps.years()
    (tmp_path / "de.yaml").write_text("rules:\n  2026:\n    crypto: {freigrenze_23: '1.000'}\n", encoding="utf-8")
    ps = ParamSet("de", bundled, tmp_path)
    assert ps.override_error and not ps.override_active
    assert ps.for_year(2026)["crypto"]["freigrenze_23"] == 1000
    (tmp_path / "de.yaml").write_text("rules: [\n", encoding="utf-8")
    assert ParamSet("de", bundled, tmp_path).override_error


def test_unreviewed_year_is_flagged():
    rows = [buy("b", "2027-01-10", "ETH", 1, 1000), sell("s", "2027-06-10", "ETH", 1, 1500)]
    _, res = run(rows, 2027, today=date(2028, 3, 1))
    assert any(i.code == "params_unreviewed" for i in res.issues)
    assert res.data["crypto"]["taxable"] == 0  # Werte 2026 fortgeschrieben (Freigrenze 1.000 €)


def test_registry_and_neutral_pack():
    ids = {c.id for c in registry.available()}
    assert {"de", "neutral"} <= ids
    assert registry.resolve("auto", "Europe/Berlin").id == "de"
    assert registry.resolve("auto", "America/New_York").id == "neutral"
    assert registry.resolve("unbekannt").id == "neutral"
    pack = registry.get("neutral")
    pf = portfolio([buy("b", "2025-01-10", "ETH", 1, 1000), sell("s", "2025-06-10", "ETH", 1, 1500),
                    tx("r", "2025-07-01", "deposit", tag="staking", to=("Börse", "ETH", "0.1"), value=40)], TAX_ASSETS)
    led = run_ledger(pf, pack.engine_options(EngineOptions(), pack.defaults(), []))
    res = pack.compute(TaxInput(pf=pf, ledger=led, today=date(2026, 1, 5)), 2025, pack.defaults())
    assert res.data["total"] == D("540.00")
    assert pack.holding_end(pf.asset("ETH"), date(2025, 1, 1)) is None
