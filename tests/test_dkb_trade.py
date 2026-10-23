"""DKB-Kauf-/Verkaufsabrechnung: Felder, Gebührensumme, Steuern, Rechenprobe (anonymisierte Nachbauten)."""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal

import pytest

from app.documentimport import dkb_trade as T
from app.documentimport.dkb import classify
from app.documentimport.extract import DocumentResult, Line, Page
from app.documentimport.profiles import analyze
from tests import dkbdocs as X

D = Decimal


def doc_of(lines: list[str]) -> DocumentResult:
    ls = [Line(text, 1, i + 1) for i, text in enumerate(lines)]
    return DocumentResult(sha256="a" * 64, file_type="pdf", pages=[Page(1, "text", ls)])


def parse(lines: list[str]) -> T.Trade:
    c = classify(lines)
    assert c is not None
    return T.parse_trade(doc_of(lines), c)


TAX = (("Kapitalertragsteuer", "25,00", "8.765,71", "2.191,43"), ("Solidaritätszuschlag", "5,50", "2.191,43", "120,52"))


def test_buy_usd_three_cost_lines_summed_and_proven() -> None:
    t = parse(X.abrechnung("Kauf"))
    assert t.ok, t.problems
    assert (t.side, t.qty, t.isin, t.wkn, t.order_no) == ("buy", D(10), "US88023B1035", "A40EDP", "1234567800")
    assert (t.kurswert, t.price, t.price_ccy, t.fx) == (D("301.46"), D("32.82"), "USD", D("1.0887"))
    assert [(i.label, -i.value) for i in t.fees] == [("Provision", D("10.00")), ("Fremde Auslagen", D("6.43")),
                                                      ("Eigene Spesen", D("20.00"))]
    assert t.fee_total == D("36.43") and t.cash == D("-337.89")
    assert t.close == (date(2024, 8, 5), time(15, 30)) and t.value_date == date(2024, 8, 6)
    assert t.direction == "Lasten" and t.tax_burden == 0 and t.tax_refund == 0


def test_buy_eur_certificate_thousand_separator() -> None:
    lines = X.abrechnung("Kauf", qty="1.000", price="0,57 EUR", fx=None, kurswert="570,00",
                         fees=(("Provision", "10,00"), ("Eigene Spesen", "2,50")))
    t = parse(lines)
    assert t.ok, t.problems
    assert t.qty == D(1000) and t.fee_total == D("12.50") and t.cash == D("-582.50")


def test_sell_with_three_costs_and_exchange_fee() -> None:
    t = parse(X.abrechnung("Verkauf", qty="1.000", price="1,25 EUR", fx=None, kurswert="1.250,00",
                           fees=(("Provision", "10,00"), ("Eigene Spesen", "2,50"),
                                 ("Transaktionsentgelt Börse", "0,61"))))
    assert t.ok, t.problems
    assert t.fee_total == D("13.11") and t.cash == D("1236.89") and t.side == "sell"


def test_sell_with_capital_gains_tax_and_soli_are_amounts_not_percentages() -> None:
    t = parse(X.abrechnung("Verkauf", qty="100", price="113,10 EUR", fx=None, kurswert="11.310,00",
                           fees=(("Provision", "15,00"),), taxes=TAX))
    assert t.ok, t.problems
    assert [(i.label, -i.value) for i in t.taxes] == [("Kapitalertragsteuer", D("2191.43")),
                                                       ("Solidaritätszuschlag", D("120.52"))]
    assert t.tax_burden == D("2311.95") and t.fee_total == D("15.00") and t.cash == D("8983.05")
    assert t.taxes[0].rate == "25,00" and t.taxes[0].base == D("8765.71")


def test_sell_with_second_settlement_block_is_a_refund() -> None:
    lines = X.abrechnung("Verkauf", qty="200", price="1,12 USD", fx="1,1429", kurswert="195,99",
                         fees=(("Provision", "10,00"), ("Fremde Auslagen", "6,12"), ("Eigene Spesen", "20,00")),
                         refund=(("Kapitalertragsteuer", "25,00", "150,33", "37,58"),
                                 ("Solidaritätszuschlag", "5,50", "37,58", "2,06")))
    t = parse(lines)
    assert t.ok, t.problems
    assert len(t.blocks) == 2 and [b.total for b in t.blocks] == [D("159.87"), D("39.64")]
    assert t.tax_refund == D("39.64") and t.tax_burden == 0 and t.cash == D("199.51")
    assert any("Ausmachender Betrag 2" in c for c in t.checks)


def test_festpreis_fraction_with_zero_cash() -> None:
    t = parse(X.abrechnung("Verkauf", festpreis=True, qty="0,3", price="1,31 USD", fx="1,0316", kurswert="0,38",
                           fees=(("Provision", "0,38"),), close=None, day="10.01.2025"))
    assert t.ok, t.problems
    assert t.cash == D("0.00") and t.close is None and t.doc_date == date(2025, 1, 10)


def test_worthless_sale_zero_cash() -> None:
    t = parse(X.abrechnung("Verkauf", qty="50", price="0,001 EUR", fx=None, kurswert="0,05",
                           fees=(("Eigene Spesen", "0,05"),)))
    assert t.ok, t.problems
    assert t.cash == D("0.00") and t.kurswert == D("0.05")


def test_savings_plan_fractional_quantity_and_rounded_price() -> None:
    t = parse(X.abrechnung("Kauf", qty="8,0671", price="6,198 EUR", fx=None, kurswert="50,00",
                           fees=(("Provision", "1,50"),)))
    assert t.ok, t.problems
    assert t.qty == D("8.0671") and t.cash == D("-51.50")


def test_unknown_cost_label_is_kept_as_fee_and_proven() -> None:
    t = parse(X.abrechnung("Kauf", qty="1.000", price="0,57 EUR", fx=None, kurswert="570,00",
                           fees=(("Provision", "10,00"), ("Börsenplatzentgelt", "0,75"))))
    assert t.ok and t.fee_total == D("10.75") and [i.label for i in t.fees][-1] == "Börsenplatzentgelt"


@pytest.mark.parametrize(("kwargs", "text"), [
    ({"total": "400,00"}, "Rechenprobe: Ausmachender Betrag"),
    ({"kurswert": "350,00", "total": "406,43"}, "Menge × Kurs"),
    ({"fx": None}, "Devisenkurs"),
    ({"fees": (("Finanztransaktionssteuer", "5,00"),), "total": "306,46"}, "keine Gebühr"),
])
def test_failed_proof_names_the_reason(kwargs: dict, text: str) -> None:
    t = parse(X.abrechnung("Kauf", **kwargs))
    assert not t.ok
    assert any(text in p for p in t.problems), t.problems


def test_direction_mismatch_is_flagged() -> None:
    lines = X.abrechnung("Kauf")
    bad = [ln.replace("zu Lasten", "zu Gunsten") for ln in lines]
    assert any("widersprechen" in p for p in parse(bad).problems)
    sell_with_debit = [ln.replace("Wertpapier Abrechnung Kauf", "Wertpapier Abrechnung Verkauf") for ln in lines]
    assert not parse(sell_with_debit).ok


def test_tax_words_in_basis_lines_and_after_the_pot_overview_are_no_amounts() -> None:
    t = parse(X.abrechnung("Verkauf", qty="100", price="113,10 EUR", fx=None, kurswert="11.310,00",
                           fees=(("Provision", "15,00"),), taxes=TAX))
    # „Bemessungs-/Berechnungsgrundlage für die Kapitalertragsteuer“ und der Topf-Teil zählen nicht
    assert len(t.taxes) == 2
    assert all(i.kind == "tax" and i.rate for i in t.taxes)


def test_doctx_fields_and_evidence_for_bridge() -> None:
    lines = X.abrechnung("Kauf")
    an = analyze(doc_of(lines))
    assert (an.doc_type, an.provider) == ("securities_trade", "dkb") and len(an.txs) == 1
    tx = an.txs[0]
    assert tx.kind == "buy" and not tx.warnings
    assert tx.value("ccy") == "EUR" and tx.value("price_ccy") == "USD" and tx.dec("price") == D("32.82")
    assert tx.dec("gross") == D("301.46") and tx.dec("value_eur") == D("301.46") and tx.dec("fee") == D("36.43")
    assert tx.dec("fee_eur") == D("36.43") and tx.value("fee_ccy") == "EUR" and tx.dec("net") == D("337.89")
    assert tx.value("ext_id") == "1234567800" and tx.value("date") == "2024-08-05" and tx.value("time") == "15:30:00"
    assert "Provision 10.00" in tx.value("fee_detail") and "= 301.46" in (tx.value("proof") or "")
    assert tx.status("fee") == "belegt" and tx.status("value_eur") == "belegt"


def test_failed_proof_makes_the_row_unbookable() -> None:
    an = analyze(doc_of(X.abrechnung("Kauf", total="400,00")))
    tx = an.txs[0]
    assert tx.kind == "unknown" and any(w.startswith("Rechenprobe") for w in tx.warnings)
    from app.documentimport.bridge import to_recs
    from app.documentimport.enrich import Enriched

    recs = to_recs(Enriched(tx, None, [], None, []), sha="a" * 64, doc={"filename": "x.pdf"}, account="DKB", n0=1)
    assert len(recs) == 1 and recs[0].kind == "review" and "Rechenprobe" in (recs[0].note or "")


def test_non_dkb_documents_still_use_the_generic_profile() -> None:
    lines = ["Wertpapierabrechnung Kauf", "ISIN  DE0007164600  Stück  10", "Kurswert  100,00 EUR",
             "Ausmachender Betrag  105,00 EUR", "Datum: 02.01.2025"]
    an = analyze(doc_of(lines))
    assert an.provider != "dkb"
