"""Steuer-Regelwerk Deutschland: Haltefrist, Freigrenzen je Jahr, Verbrauchsfolge, Töpfe, Fonds, Parameter."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.ledger.engine import EngineOptions, run_ledger
from app.tax import registry
from app.tax.base import TaxInput, fmt_eur, fmt_num, money
from app.tax.classify import classify_asset
from app.tax.params import ParamSet
from tests.helpers import ASSETS, portfolio, tx

D = Decimal
TAX_ASSETS = [
    *ASSETS,
    {"asset_id": "TURBO", "name": "ENDLOS-TURBO CALL AUF SAP SE", "asset_class": "security", "isin": "DE000HC1ABC1",
     "quote_source": "none", "quote_id": ""},
    {"asset_id": "US1", "name": "Alpha Inc.", "asset_class": "security", "isin": "US0000000001",
     "quote_source": "none", "quote_id": ""},
    {"asset_id": "US2", "name": "Beta Corp.", "asset_class": "security", "isin": "US0000000002",
     "quote_source": "none", "quote_id": ""},
    {"asset_id": "BOND", "name": "Anleihe 2030", "asset_class": "security", "isin": "DE0000000003",
     "quote_source": "none", "quote_id": "", "extra": {"tax_type": "bond"}},
    {"asset_id": "FUND", "name": "Welt ETF", "asset_class": "security", "isin": "IE0000000004",
     "quote_source": "none", "quote_id": "", "extra": {"tax_type": "etf_equity"}},
]


def run(rows, year, *, options=None, kinds=None, year_prices=None, today=date(2026, 9, 26), certs=None, isin_map=None):
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
                   year_prices=(lambda aid, y: (year_prices or {}).get((aid, y), (None, None))), certs=certs or {},
                   isin_map=isin_map or {})
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
    # Veräußerungsgewinn abzüglich angesetzter VP – je Zeile auf Cent gerundet, damit die Zeile aufgeht
    assert b["fund_gain"]["etf_equity"] == D("200") - money(vp)
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


def test_negative_foreign_saldo_is_kept_with_note():
    rows = [buy("a", "2025-01-10", "US2", 10, 1000, acc="IBKR"), sell("b", "2025-06-11", "US2", 10, 500, acc="IBKR"),
            tx("c", "2025-03-01", "deposit", tag="dividend", to=("IBKR", "EUR", 100), value=100, related="US1")]
    _, res = run(rows, 2025, kinds={"IBKR": "foreign"})
    f = {x.field_id: x for x in res.fields}
    # Saldo inkl. enthaltener Verluste (ELSTER: Saldo ≥ Aktiengewinne − Verluste ohne Aktien − Aktienverluste)
    assert f["kap_foreign_total"].amount == D("-400.00") and "negativer Saldo" in f["kap_foreign_total"].note
    assert f["kap_foreign_losses_shares"].amount == D("500.00")
    assert f["kap_foreign_total"].amount >= -f["kap_foreign_losses_shares"].amount


def test_form_lines_by_year():
    kap24 = [
        buy("k1", "2024-01-10", "US1", 10, 1000, acc="IBKR"), sell("k2", "2024-06-10", "US1", 10, 2000, acc="IBKR"),
        buy("k3", "2024-01-10", "US2", 10, 1000, acc="IBKR"), sell("k4", "2024-06-11", "US2", 10, 700, acc="IBKR"),
        buy("k5", "2024-01-10", "BOND", 1, 1000, acc="IBKR"), sell("k6", "2024-06-12", "BOND", 1, 800, acc="IBKR"),
        tx("k7", "2024-03-01", "deposit", tag="dividend", to=("IBKR", "EUR", 100), value=100, related="US1"),
        tx("k8", "2024-03-01", "withdrawal", tag="withholding_tax", frm=("IBKR", "EUR", 15), value=15, related="US1"),
        buy("f1", "2023-03-10", "FUND", 10, 1000, acc="IBKR"),
        tx("f2", "2024-02-01", "deposit", tag="dividend", to=("IBKR", "EUR", 50), value=50, related="FUND"),
        buy("c1", "2024-02-01", "ETH", 1, 1000), sell("c2", "2024-08-01", "ETH", 1, 3000),
    ]
    prices = {("FUND", 2023): ((date(2023, 1, 2), D(100)), (date(2023, 12, 29), D(110)))}
    _, res = run(kap24, 2024, kinds={"IBKR": "foreign"}, year_prices=prices)
    lines = {(x.field_id, x.label): x.line for x in res.fields}
    by_id = {x.field_id: x.line for x in res.fields}
    assert by_id["kap_foreign_total"] == "19" and by_id["kap_foreign_share_gains"] == "20"
    assert by_id["kap_foreign_losses_other"] == "22" and by_id["kap_foreign_losses_shares"] == "23"
    assert by_id["kap_wht"] == "41"
    assert lines[("inv_dist", "Ausschüttungen – Aktienfonds")] == "4"
    assert lines[("inv_vp", "Vorabpauschalen – Aktienfonds")] == "9"
    assert by_id["so_23_price"] == "44" and by_id["so_23_gain"] == "47" and by_id["so_23_acq"] == "43"
    # 2025: Anlage SO mit eigenem Krypto-Abschnitt; KAP/KAP-INV umgebaut → keine Zeilen hinterlegt
    rows25 = [*_kap_rows(), buy("c1", "2025-02-01", "ETH", 1, 1000), sell("c2", "2025-08-01", "ETH", 1, 3000)]
    _, res = run(rows25, 2025, kinds={"IBKR": "foreign"})
    f = {x.field_id: x for x in res.fields}
    assert f["so_23_gain"].line == "51" and f["so_23_price"].line == "48" and f["so_23_wk"].line == "50"
    assert f["so_23_gain"].section == "Private Veräußerungsgeschäfte – Kryptowerte" and f["so_23_desc"].line is None
    kap = {x.field_id: x.line for x in res.fields if x.form in ("Anlage KAP", "Anlage KAP-INV")}
    assert kap.get("kap_foreign_total") is None and kap.get("kap_foreign_losses_shares") is None  # Auslandsfelder offen
    assert kap.get("kap_wht") == "41"  # laut Erläuterung der Steuerbescheinigung 2025 belegt
    assert all(v is None for kid, v in kap.items() if kid.startswith("inv_"))
    # 2026: noch keine geprüften Zeilen
    rows26 = [buy("c1", "2026-02-01", "ETH", 1, 1000), sell("c2", "2026-08-01", "ETH", 1, 3000)]
    _, res = run(rows26, 2026, today=date(2027, 3, 1))
    assert all(x.line is None for x in res.fields)


def test_vorabpauschale_2026_uses_basiszins_2026():
    rows = [buy("f1", "2026-01-05", "FUND", 10, 1000, acc="IBKR")]
    prices = {("FUND", 2026): ((date(2026, 1, 2), D(100)), (date(2026, 12, 30), D(110)))}
    _, res = run(rows, 2027, kinds={"IBKR": "foreign"}, year_prices=prices, today=date(2028, 3, 1))
    # 100 × 3,20 % × 0,7 = 2,24 € je Anteil (< Wertzuwachs 10 €), volles Jahr
    assert res.data["capital"]["B"]["foreign"]["fund_vp"]["etf_equity"] == D("22.4")
    assert not any(i.code == "vp_missing" for i in res.issues)


# -- Parameter & Registry -------------------------------------------------------------------------------

def test_param_override_and_errors(tmp_path: Path):
    bundled = registry.PACKS_DIR / "de" / "params.yaml"
    ps = ParamSet("de", bundled, tmp_path)
    assert ps.for_year(2023)["crypto"]["freigrenze_23"] == 600
    assert ps.for_year(2025)["crypto"]["freigrenze_23"] == 1000
    assert ps.for_year(2025)["basiszins"] == 0.0253 and ps.for_year(2026)["basiszins"] == 0.032
    assert ps.for_year(2027)["basiszins"] is None
    assert ps.for_year(2022)["capital"]["sparer_pauschbetrag_single"] == 801
    (tmp_path / "de.yaml").write_text(
        "pack: {reviewed_through: 2027}\n"
        "rules:\n  2026:\n    crypto: {freigrenze_23: 2000}\n"
        "per_year:\n  basiszins: {2027: 0.03}\n"
        "forms:\n  2027:\n    anlage_so: {fields: {so_23_gain: {line: '52'}}}\n", encoding="utf-8")
    ps = ParamSet("de", bundled, tmp_path)
    assert ps.override_active and ps.version.endswith("+lokal") and ps.override_error is None
    assert ps.for_year(2026)["crypto"]["freigrenze_23"] == 2000
    assert ps.for_year(2025)["crypto"]["freigrenze_23"] == 1000
    assert ps.for_year(2027)["basiszins"] == 0.03 and ps.for_year(2026)["basiszins"] == 0.032
    assert ps.for_year(2027)["forms"]["anlage_so"]["fields"]["so_23_gain"]["line"] == "52"
    assert ps.for_year(2025)["forms"]["anlage_so"]["fields"]["so_23_gain"]["line"] == "51"
    assert ps.for_year(2026)["forms"]["anlage_so"]["fields"]["so_23_gain"]["line"] is None
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


# -- Derivate, Rundung, Verlustvortrag, Hinweise (0.49.1) ----------------------------------------------------

def test_derivatives_are_classified_and_sit_in_other_pot():
    from app.ledger.models import AssetInfo

    names = ["ENDLOS-TURBO CALL AUF SAP SE", "UBS AG TURBOP O.END RHEINME. 1330", "GAMESTOP CORP. WTS26",
             "Knock-Out-Call auf Microsoft", "MORGAN STANLEY/CALL/IQIYI, INC./20/0.1/11.09.20",
             "MINI FUTURE ZERTIFIKAT LONG AUF TESLA INC.", "OPEN END TURBO CALL WARRANT AUF TESLA INC.",
             "FAKTOR-OPTIONSSCHEIN AUF CBOE VIX FUTURE"]
    for n in names:
        a = AssetInfo(asset_id=n, name=n, asset_class="security", quote_source="none", quote_id="")
        assert classify_asset(a) == ("derivative", "heuristic"), n
    for n in ("Alphabet Inc. Class A", "ETC-Zertifikat auf Silberpreis (Spot)", "Welt UCITS ETF", "Callaway Golf Co"):
        a = AssetInfo(asset_id=n, name=n, asset_class="security", quote_source="none", quote_id="")
        assert classify_asset(a)[0] != "derivative", n
    # Verlust des Turbos steht im Topf „Sonstige“ und mindert Dividenden – er sperrt nicht den Aktien-Topf
    rows = [buy("t1", "2025-01-10", "TURBO", 100, 1000, acc="IBKR"),
            sell("t2", "2025-03-10", "TURBO", 100, 400, acc="IBKR"),
            tx("d1", "2025-03-01", "deposit", tag="dividend", to=("IBKR", "EUR", 100), value=100, related="US1")]
    _, res = run(rows, 2025, kinds={"IBKR": "foreign"})
    b = res.data["capital"]["B"]["foreign"]
    assert b["other_loss"] == D("-600.00") and b["share_loss"] == 0 and b["dividends"] == D("100.00")
    sale = next(t for t in res.sections if t.id == "kap").tables[0].rows[0]
    assert sale["pot"] == "Sonstige" and "Termingeschäft" in sale["note"]
    assert any(i.code == "derivatives" for i in res.issues)
    assert any("Termingeschäfte" in a and "Jahressteuergesetz 2024" in a for a in res.assumptions)


def test_rows_add_up_and_no_negative_zero():
    rows = [buy("b1", "2025-01-10", "ETH", "0.3333", "1000.01"),
            tx("s1", "2025-03-10", "sell", frm=("Börse", "ETH", "0.1111"), to=("Börse", "EUR", "333.34"),
               fee=("EUR", "1.115", "1.115"), value="334.455")]
    _, res = run(rows, 2025)
    for r in next(t for t in res.sections if t.id == "crypto23").tables[1].rows:
        assert r["price"] - r["wk"] - r["cost"] == r["gain"]
    assert fmt_num(D("-0.00")) == "0,00" and fmt_eur(D("-0.004")) == "0,00 €" and fmt_eur(D("-0.01")) == "−0,01 €"
    # Kapitalerträge: Zeile geht ebenfalls auf (Erlös − Anschaffung − Kosten − VP = Gewinn)
    _, res = run(_kap_rows(), 2025, kinds={"IBKR": "foreign"})
    for r in next(t for t in res.sections if t.id == "kap").tables[0].rows:
        assert r["price"] - r["cost"] - r["wk"] - r["vp"] == r["gain"]


class _Settings:
    version = 1

    def __init__(self, options=None):
        self._v = {"tax.options": options or {}, "tax.rulepack": "de"}

    def get(self, key, default=None):
        return self._v.get(key, default)


def _service(rows, year_options=None, today=date(2026, 9, 26)):
    """TaxService mit gestelltem Eingang (ohne App-Kontext) – prüft die Verkettung der Vorjahre."""
    from types import SimpleNamespace

    from app.tax.service import TaxService

    pack = registry.get("de")
    opts = pack.defaults()
    pf = portfolio(rows, TAX_ASSETS)
    led = run_ledger(pf, pack.engine_options(EngineOptions(), opts, list(range(2018, today.year))))
    types = {aid: classify_asset(a)[0] for aid, a in pf.assets.items()}
    inp = TaxInput(pf=pf, ledger=led, today=today, asset_types=types, asset_type_source={},
                   account_kinds=dict.fromkeys(pf.all_accounts(), "domestic"), account_kind_source={})
    ctx = SimpleNamespace(settings=_Settings({"de": {"global": {}, "years": year_options or {}}}),
                          prices=SimpleNamespace(version=1), history_version=1, data_version=1,
                          config=SimpleNamespace(tz="Europe/Berlin", tax_rules_dir=None),
                          active_import_id=lambda: 1)
    svc = TaxService(ctx)
    svc.build_input = lambda pack_, options: inp  # type: ignore[method-assign]
    return svc


def _carry_rows():
    return [buy("a1", "2023-01-10", "ETH", 1, 1000), sell("a2", "2023-06-10", "ETH", 1, 400),     # 2023: −600
            buy("b1", "2024-01-10", "ETH", 1, 1000), sell("b2", "2024-06-10", "ETH", 1, 700),     # 2024: −300
            buy("c1", "2025-01-10", "ETH", 1, 1000), sell("c2", "2025-06-10", "ETH", 1, 3000)]    # 2025: +2.000


def test_loss_carryforward_23_chains_over_years():
    svc = _service(_carry_rows())
    r23, r24, r25 = (svc.compute(y)[2].data["crypto"] for y in (2023, 2024, 2025))
    assert r23["net"] == D("-600.00") and r23["lcf"] == 0 and r23["carry_out"] == D("600.00")
    assert r24["lcf"] == D("600.00") and r24["lcf_auto"] and r24["carry_out"] == D("900.00")
    assert r25["net"] == D("2000.00") and r25["taxable"] == D("2000.00")
    assert r25["lcf"] == D("900.00") and r25["lcf_used"] == D("900.00")
    assert r25["after_lcf"] == D("1100.00") and r25["carry_out"] == 0
    res = svc.compute(2025)[2]
    assert any(ln.label == "Verlustvortrag aus Vorjahren" and ln.amount == D("900.00") for ln in res.summary)
    assert res.options["loss_cf_23_auto"] == D("900.00")


def test_loss_carryforward_manual_override_and_legacy_zero():
    # eingetragener Betrag überschreibt die Kette – auch die Zeichenkette „0“; die frühere Standard-Null (int) nicht
    svc = _service(_carry_rows(), {"2025": {"loss_cf_23": "0"}})
    c = svc.compute(2025)[2].data["crypto"]
    assert c["lcf"] == 0 and not c["lcf_auto"] and c["after_lcf"] == D("2000.00")
    svc = _service(_carry_rows(), {"2025": {"loss_cf_23": 0}})
    assert svc.compute(2025)[2].data["crypto"]["lcf"] == D("900.00")
    svc = _service(_carry_rows(), {"2024": {"loss_cf_23": "100"}})  # Eingabe eines Vorjahres wirkt auf die Folgejahre
    c24, c25 = (svc.compute(y)[2].data["crypto"] for y in (2024, 2025))
    assert c24["lcf"] == D("100") and c24["carry_out"] == D("400.00") and c25["lcf"] == D("400.00")


def test_loss_carryforward_not_consumed_below_freigrenze():
    rows = [buy("a1", "2023-01-10", "ETH", 1, 1000), sell("a2", "2023-06-10", "ETH", 1, 400),   # 2023: −600
            buy("b1", "2024-01-10", "ETH", 1, 1000), sell("b2", "2024-06-10", "ETH", 1, 1500)]  # 2024: +500 < 1.000
    svc = _service(rows)
    c24 = svc.compute(2024)[2].data["crypto"]
    assert c24["taxable"] == 0 and c24["lcf_used"] == 0 and c24["carry_out"] == D("600.00")


def test_quality_hints_scope_marked_income_and_domestic_allowance():
    rows = [tx("k1", "2025-10-22", "deposit", tag="other_income", to=("Wallet", "ETH", 1), value="200.71",
               flag="REVIEW_INCOME"),
            tx("k2", "2025-11-01", "deposit", tag="airdrop", to=("Wallet", "BTC", "0.01"), value="55.29"),
            *_kap_rows()]
    _, res = run(rows, 2025, options={"scope": "global"}, kinds={"IBKR": "domestic"})
    codes = {i.code: i for i in res.issues}
    assert "scope_global" in codes and "Rz. 61/62" in codes["scope_global"].text
    fg = codes["fg22_marked"]
    assert fg.severity == "warning"  # 256,00 € erreicht (200,71 + 55,29), ohne den markierten Posten 55,29 €
    assert "55,29" in fg.text and "unter der Freigrenze" in fg.text
    assert any(m.id == "fg22" for m in res.meters)
    assert "pb_domestic" in codes  # Inlandsdepot mit Erträgen, Pauschbetrag nicht eingetragen


# -- Rundung: Jahressaldo aus ungerundeten Werten -----------------------------------------------------------

def _tiny_gains(extra_big_gain: str):
    rows = [buy("b1", "2025-01-10", "BTC", "1.1", "1100.00"), sell("s0", "2025-02-01", "BTC", "1", extra_big_gain)]
    rows += [sell(f"s{i}", "2025-02-02", "BTC", "0.001", "1.004") for i in range(1, 101)]
    return rows


def test_freigrenze_uses_unrounded_sum_not_sum_of_rounded_lines():
    # 999,90 € + 100 × 0,004 € = 1.000,30 € ≥ 1.000 € (Freigrenze 2025) → voll steuerpflichtig; einzeln gerundet
    # ergäbe 0,00 € je Kleingewinn und damit 999,90 € (steuerfrei)
    _, res = run(_tiny_gains("1999.90"), 2025)
    c = res.data["crypto"]
    assert c["net"] == D("1000.30") and c["taxable"] == D("1000.30")
    assert c["round_diff"] == D("0.40") and c["count_tax"] == 101
    assert any(i.code == "rounding" for i in res.issues)
    assert c["gains"] + c["losses"] == c["net"] and c["price"] - c["wk"] - c["cost"] == c["net"]
    # knapp darunter bleibt steuerfrei
    _, res2 = run(_tiny_gains("1999.50"), 2025)
    c2 = res2.data["crypto"]
    assert c2["net"] == D("999.90") and c2["taxable"] == 0


def test_rounding_difference_is_zero_for_cent_exact_values():
    rows = [buy("b1", "2025-01-10", "BTC", "1", "1000.00"), sell("s1", "2025-02-01", "BTC", "1", "1500.25")]
    _, res = run(rows, 2025)
    assert res.data["crypto"]["round_diff"] == 0
    assert not any(i.code == "rounding" for i in res.issues)


def test_unmatched_withholding_tax_is_not_credited_automatically():
    rows = [r for r in _kap_rows() if r["tx_id"] != "k7"]  # Quellensteuer ohne zugehörige Dividende
    _, res = run(rows, 2025, kinds={"IBKR": "foreign"}, options={"pauschbetrag_used": "1000"})
    f = {x.field_id: x.amount for x in res.fields}
    assert f.get("kap_wht", D(0)) == 0  # keine Anrechnung ohne Beleg-Zuordnung
    est = {ln.label: ln.amount for ln in res.estimate}
    assert est["Abgeltungsteuer nach Anrechnung Quellensteuer"] == D("125.00")  # 500 / 4, ohne Abzug
    iss = [i for i in res.issues if i.code == "wht_unmatched"]
    assert iss and "nicht angerechnet" in iss[0].text and "15,00" in iss[0].text


def test_tax_prices_and_fx_have_a_maximum_age():
    from types import SimpleNamespace

    from app.tax.service import TaxService

    asset = SimpleNamespace(asset_id="BTC")
    pf = SimpleNamespace(asset=lambda aid: asset, manual_prices={})
    rows = {"2026-01-10": {"date": "2026-01-10", "close": 100.0, "ccy": "USD"}}
    store = SimpleNamespace(
        close_on_or_before=lambda series, d: rows.get("2026-01-10") if d >= date(2026, 1, 10) else None,
        fx_on_or_before=lambda ccy, d: (2.0, "2026-01-09", "yahoo") if d >= date(2026, 1, 9) else None)
    ctx = SimpleNamespace(portfolio=lambda: pf, prices=SimpleNamespace(series_for=lambda a: "BTC-USD"), store=store)
    svc = TaxService.__new__(TaxService)
    svc.ctx = ctx
    assert svc._series_price("BTC", date(2026, 1, 12)) == D("50")  # 100 USD ÷ 2,0 (FX vom Vortag des Kurses)
    assert svc._series_price("BTC", date(2026, 1, 16)) == D("50")  # Kurs 6, Devisen 7 Tage alt → noch zulässig
    assert svc._series_price("BTC", date(2026, 1, 17)) is None  # Devisenkurs 8 Tage alt → kein „Tageskurs“
    assert svc._series_price("BTC", date(2026, 1, 18)) is None  # auch der Kurs ist jetzt 8 Tage alt
    assert svc._fx("USD", date(2026, 1, 16)) == D("0.5") and svc._fx("USD", date(2026, 1, 17)) is None  # FX 8 Tage alt


def test_reconstructed_booking_is_flagged_and_marks_report_as_preliminary():
    rows = [buy("b1", "2025-01-10", "BTC", "1", "1000.00"), sell("s1", "2025-02-01", "BTC", "1", "1500.00")]
    rows[0]["flag"] = "RECONSTRUCTED_GAP;KOINLY_MISSING_RATE"
    _, res = run(rows, 2025)
    iss = [i for i in res.issues if i.code == "reconstructed_tx"]
    assert iss and iss[0].severity == "warning" and iss[0].count == 1  # eine Buchung, zwei Marken
    assert "rekonstruiert" in iss[0].text and "Kurs fehlte" in iss[0].text
    rv = res.review()
    assert rv["status"] == "vorläufig" and "reconstructed_tx" in rv["codes"] and rv["open"] >= 1
    assert res.summary_json()["review"] == rv


def test_review_status_without_open_points_makes_no_claim_of_correctness():
    rows = [buy("b1", "2025-01-10", "BTC", "1", "1000.00"), sell("s1", "2025-02-01", "BTC", "1", "1500.00")]
    _, res = run(rows, 2025)
    rv = res.review()
    assert not any(i.code == "reconstructed_tx" for i in res.issues)
    assert rv == {"status": "ohne automatisch erkannte offene Punkte", "open": 0, "codes": []}
    assert "korrekt" not in rv["status"] and "final" not in rv["status"]


@pytest.mark.parametrize("proceeds, shown, taxable", [
    ("1999.994", "999.99", 0),
    ("1999.995", "1000.00", 0),          # angezeigt 1.000,00 €, aber ungerundet < 1.000 € → steuerfrei
    ("1999.9999", "1000.00", 0),
    ("2000.000", "1000.00", "1000.00"),  # genau an der Grenze → nicht „weniger als“
    ("2000.004", "1000.00", "1000.00"),
])
def test_freigrenze_23_compares_unrounded_saldo(proceeds, shown, taxable):
    rows = [buy("b1", "2025-01-10", "BTC", "1", "1000"), sell("s1", "2025-02-01", "BTC", "1", proceeds)]
    _, res = run(rows, 2025)
    c = res.data["crypto"]
    assert c["net"] == D(shown) and c["taxable"] == D(taxable)
    meter = next(m for m in res.meters if m.id == "fg23")
    edge = any(i.code == "freigrenze_edge" for i in res.issues)
    if D(proceeds) - 1000 < 1000 <= c["net"]:
        assert edge and meter.state != "crit"  # Randfall wird erklärt, Anzeige „Freigrenze erreicht“ bleibt aus
    else:
        assert not edge
    assert (meter.state == "crit") == (D(proceeds) - 1000 >= 1000)


def test_freigrenze_edge_note_names_both_amounts():
    rows = [buy("b1", "2025-01-10", "BTC", "1", "1000"), sell("s1", "2025-02-01", "BTC", "1", "1999.995")]
    _, res = run(rows, 2025)
    note = next(i.text for i in res.issues if i.code == "freigrenze_edge")
    assert "1.000,00" in note and "999,9950" in note
    assert "ungerundet" in note and "weniger als" in note


def test_freigrenze_22_3_compares_unrounded_sum():
    rows = [tx("r1", "2025-05-01", "deposit", tag="staking", to=("Börse", "ETH", "0.1"), value="255.995")]
    _, res = run(rows, 2025)
    i = res.data["income"]
    assert i["einkuenfte"] == D("256.00") and i["taxable"] == 0  # 255,995 € < 256 €
    assert any(x.code == "freigrenze_edge" and "§ 22 Nr. 3" in x.text for x in res.issues)
    assert next(m for m in res.meters if m.id == "fg22").state != "crit"
    rows2 = [tx("r1", "2025-05-01", "deposit", tag="staking", to=("Börse", "ETH", "0.1"), value="256.000")]
    _, res2 = run(rows2, 2025)
    assert res2.data["income"]["taxable"] == D("256.00")
    assert not any(x.code == "freigrenze_edge" for x in res2.issues)
