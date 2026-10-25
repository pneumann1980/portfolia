"""Nachweise für Prüfung und Rückfragen: Anhänge, Kurs/Quelle je Zeile, Buchungsstand-Fingerabdruck."""

import io
from decimal import Decimal

import pypdfium2 as pdfium

from tests.helpers import tx
from tests.test_tax_de import buy, run, sell
from tests.test_tax_web import client  # noqa: F401  (Fixture)

D = Decimal


def _table(res, tid):
    return next(t for s in res.sections for t in s.tables if t.id == tid)


def test_disposal_rows_carry_unit_price_origin_and_source():
    rows = [tx("b1", "2025-01-10", "buy", frm=("Börse", "EUR", 1000), to=("Börse", "ETH", 2), value=1000,
               source="bitpanda"),
            tx("s1", "2025-03-10", "sell", frm=("Börse", "ETH", 1), to=("Börse", "EUR", 600), value=600,
               fee=("EUR", 2, 2), source="koinly")]
    _, res = run(rows, 2025)
    t = _table(res, "c23_taxable")
    cols = {c.key: c for c in t.columns}
    assert {"unit", "origin", "ref"} <= set(cols) and cols["unit"].kind == "qty"
    r = t.rows[0]
    assert r["origin"] == "Kauf" and r["unit"] == D("600.00") and r["ref"] == "koinly · s1"
    assert "BMF Rz. 91/103" in t.note


def test_adjustment_annex_lists_flags_and_acquisition_of_sold_lots():
    rows = [tx("p1", "2024-02-01", "buy", frm=("Börse", "EUR", 1000), to=("Börse", "ETH", 1), value=1000,
               flag="PRESALE;KOINLY_MANUAL", source="koinly", note="Coinlist Presale, Tranche 3"),
            tx("s1", "2025-03-10", "sell", frm=("Börse", "ETH", 1), to=("Börse", "EUR", 1500), value=1500),
            tx("b2", "2025-04-01", "buy", frm=("Börse", "EUR", 500), to=("Börse", "BTC", "0.01"), value=500,
               flag="BROKER_UNASSIGNED;SAVINGS_PLAN"),
            tx("m1", "2025-05-01", "deposit", tag="staking", to=("Börse", "BTC", "0.001"), value="40",
               origin="journal", note="manuell erfasst"),
            tx("x1", "2023-02-01", "buy", frm=("Börse", "EUR", 100), to=("Börse", "SOL", 1), value=100,
               flag="AVG_PRICE")]
    _, res = run(rows, 2025)
    t = _table(res, "x_adjust")
    by = {r["ref"].split(" · ")[1]: r for r in t.rows}
    assert set(by) == {"p1", "m1"}  # Verwaltungsmarken und fremde Jahre/ungenutzte Posten nicht
    assert "Presale-Tranche (Einzelfallwertung)" in by["p1"]["flags"] and "Coinlist" in by["p1"]["note"]
    assert by["p1"]["ref"].startswith("koinly") and by["m1"]["origin"] == "in Portfolia erfasst/synchronisiert"
    assert "BMF Rz. 90" in t.note


def test_stock_annex_year_end_and_current():
    rows = [buy("b1", "2024-01-10", "ETH", 2, 1000), buy("b2", "2024-06-10", "ETH", 1, 900),
            buy("b3", "2025-02-10", "BTC", "0.5", 5000), sell("s1", "2025-03-10", "ETH", 1, 700),
            tx("t1", "2025-04-01", "transfer", frm=("Börse", "BTC", "0.2"), to=("Ledger", "BTC", "0.2"))]
    _, res = run(rows, 2024)
    t = _table(res, "x_stock")
    assert t.title == "Anhang: Bestand zum 31.12.2024"
    assert [(r["account"], r["asset"], r["qty"], r["cost"], r["lots"]) for r in t.rows] == [
        ("Börse", "ETH", D("3"), D("1900.00"), 2)]
    assert t.totals["cost"] == D("1900.00")
    _, res = run(rows, 2025)
    t = _table(res, "x_stock")
    got = {(r["account"], r["asset"]): (r["qty"], r["cost"]) for r in t.rows}
    assert got == {("Börse", "ETH"): (D("2"), D("1400.00")), ("Börse", "BTC"): (D("0.3"), D("3000.00")),
                   ("Ledger", "BTC"): (D("0.2"), D("2000.00"))}


def test_transfer_annex_and_fingerprint():
    rows = [buy("b1", "2025-01-10", "BTC", 1, 20000),
            tx("t1", "2025-04-01", "transfer", frm=("Börse", "BTC", "0.4"), to=("Ledger", "BTC", "0.4"),
               source="koinly"),
            tx("t2", "2024-04-01", "transfer", frm=("Börse", "BTC", "0.1"), to=("Ledger", "BTC", "0.1"))]
    _, res = run(rows, 2025)
    t = _table(res, "x_transfers")
    assert [(r["asset"], r["qty"], r["src"], r["dst"], r["ref"]) for r in t.rows] == [
        ("BTC", D("0.4"), "Börse", "Ledger", "koinly · t1")]
    fp = res.data["data"]
    assert len(fp["sha256"]) == 64 and fp["txs"] == 3 and fp["manual"] == 0
    _, again = run(rows, 2025)
    assert again.data["data"]["sha256"] == fp["sha256"]  # reproduzierbar
    changed = [*rows[:2], tx("t2", "2024-04-01", "transfer", frm=("Börse", "BTC", "0.2"), to=("Ledger", "BTC", "0.2"))]
    assert run(changed, 2025)[1].data["data"]["sha256"] != fp["sha256"]
    with_manual = [*rows, tx("m1", "2025-05-01", "deposit", tag="staking", to=("Börse", "BTC", "0.1"), value="9",
                             origin="journal")]
    assert run(with_manual, 2025)[1].data["data"]["manual"] == 1


def test_issues_quantify_deposits_and_excluded_withdrawals():
    rows = [tx("d1", "2025-01-10", "deposit", to=("Börse", "ETH", 1), value=800),  # Zugang ohne Gegenbuchung
            sell("s1", "2025-03-10", "ETH", 1, 1200),
            buy("b1", "2025-01-11", "BTC", 1, 3000),
            tx("w1", "2025-05-01", "withdrawal", frm=("Börse", "BTC", "0.5"))]  # Abgang ohne Gegenbuchung
    _, res = run(rows, 2025)
    codes = {i.code: i.text for i in res.issues}
    assert "Anschaffungskosten 800,00 €" in codes["deposit_basis"]
    assert "Haltefrist beginnt neu" in codes["deposit_basis"]
    assert "Anschaffungskosten der betroffenen Einheiten 1.500,00 €" in codes["excluded_withdrawal"]


def test_income_table_has_marking_source_and_airdrop_note():
    rows = [tx("k1", "2025-10-22", "deposit", tag="airdrop", to=("Wallet", "ETH", 1), value="80", flag="REVIEW_INCOME",
               source="koinly")]
    _, res = run(rows, 2025)
    t = _table(res, "i22")
    assert t.rows[0]["flag"] == "Einstufung des Ertrags zur Prüfung markiert" and t.rows[0]["ref"] == "koinly · k1"
    assert "Rz. 70–75" in t.note and "Teilnahmebedingungen" in t.note


def test_report_pdf_contains_annex_and_data_state(client):  # noqa: F811
    from app.tax.service import tax_service

    ctx = client.app.state.ctx
    assert client.post("/tax/report", data={"year": "2025", "doc": ["report", "anlage_so"]},
                       follow_redirects=False).status_code == 303
    rep = tax_service(ctx).reports()[0]
    texts = {}
    for d in rep["summary"]["docs"]:
        pdf = pdfium.PdfDocument(io.BytesIO(client.get(f"/tax/report/{rep['id']}/{d['id']}.pdf").content))
        raw = " ".join(pdf[i].get_textpage().get_text_bounded() for i in range(len(pdf)))
        texts[d["id"]] = " ".join(raw.split())
    r = texts["report"]
    assert "Buchungsstand" in r and "SHA-256" in r and "Anhang: Bestand zum 31.12.2025" in r
    assert "Buchungsstand" in texts["anlage_so"]
