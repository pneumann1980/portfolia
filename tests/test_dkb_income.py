# ruff: noqa: F811  (Fixture ``master`` aus test_bitpanda wird importiert und als Argument verwendet)
"""DKB-Ertragsabrechnung (Phase 3): Felder, Quellensteuer-Regeln, Rechenprobe, Abbildung, Zuordnung zu vorhandenen
Dividenden und Lauf über das ZIP. Alle Daten sind erfunden (anonymisierte Nachbauten)."""

from __future__ import annotations

import json
import os
import time
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.documentimport import dkb_income as I
from app.documentimport.bridge import to_recs
from app.documentimport.dkb import classify
from app.documentimport.enrich import Enriched
from app.documentimport.extract import DocumentResult, Line, Page
from app.documentimport.profiles import analyze
from app.importer.zipbuilder import build_zip
from app.jobs import tasks
from app.main import build_app
from tests import dkbdocs as X
from tests.helpers import ASSETS, tx
from tests.test_bitpanda import master, post  # noqa: F401
from tests.test_dkb import ctx, rows
from tests.test_dkb_zip import finish, make_zip, run_id, upload

D = Decimal
KEST = ("25", "1,45", "0,36")
SOLI = ("5,5", "0,36", "0,01")
NOK = {"ccy": "NOK", "fx": "11,5448", "gross_f": "275,50", "gross": "23,86", "per": "2,755", "qty": "100",
       "isin": "NO0010890304", "wkn": "A2QBSN", "qst": ("25", "5,97"), "kest": ("25", "23,86", "5,97"),
       "soli": ("5,5", "5,97", "0,32"), "rueckforderbar": True, "pay": "19.03.2025", "value_day": "21.03.2025",
       "day": "19.03.2025", "abr": "57858717480"}


def doc_of(lines: list[str]) -> DocumentResult:
    ls = [Line(text, 1, i + 1) for i, text in enumerate(lines)]
    return DocumentResult(sha256="a" * 64, file_type="pdf", pages=[Page(1, "text", ls)])


def parse(lines: list[str]) -> I.Income:
    c = classify(lines)
    assert c is not None and c.kind == "ertrag"
    return I.parse_income(doc_of(lines), c)


def test_dividend_usd_withholding_only_is_proven() -> None:
    t = parse(X.ertragsabrechnung(gross_f="4,00", gross="3,71", fx="1,0768", qst=("15", "0,56"), day="18.06.2024",
                                  pay="17.06.2024", abr="67060867760"))
    assert t.ok, t.problems
    assert (t.sub, t.qty, t.isin, t.wkn, t.abr_no) == ("dividende", D(20), "US02079K3059", "A14Y6F", "67060867760")
    assert (t.gross, t.gross_foreign, t.gross_ccy, t.fx) == (D("3.71"), D("4.00"), "USD", D("1.0768"))
    assert t.withheld == D("0.56") and t.tax_burden == 0 and t.total == D("3.15")
    assert t.pay_date == date(2024, 6, 17) and t.value_date == date(2024, 6, 18) and t.direction == "Gunsten"


def test_dividend_with_kapest_rest_ignores_information_lines() -> None:
    t = parse(X.ertragsabrechnung(kest=KEST, soli=SOLI, verrechnet=True))
    assert t.ok, t.problems
    # „Anrechenbare Quellensteuer“, „Verrechnete anrechenbare …“ (Zeilenumbruch mit 2,20 -),
    # „Kapitalertragsteuerpflichtige Dividende“ und „Berechnungsgrundlage …“ sind keine Abzüge
    assert t.withheld == D("0.55") and t.tax_burden == D("0.37") and t.total == D("2.73")
    assert [(i.kind, i.label) for i in t.items] == [
        ("gross", "Dividendengutschrift"), ("qst", "Einbehaltene Quellensteuer"), ("tax", "Kapitalertragsteuer"),
        ("tax", "Solidaritätszuschlag")]


def test_dividend_nok_quellensteuer_and_kapest_both_booked() -> None:
    t = parse(X.ertragsabrechnung(**NOK))
    assert t.ok, t.problems
    assert (t.gross, t.withheld, t.tax_burden, t.total) == (D("23.86"), D("5.97"), D("6.29"), D("11.60"))
    assert t.pay_date == date(2025, 3, 19) and t.value_date == date(2025, 3, 21)


@pytest.mark.parametrize(("sub", "kw", "total"), [
    ("rueckzahlung", {"qst": None, "gross": "17,89", "gross_f": "206,50", "per": "2,06499", "ccy": "NOK",
                      "fx": "11,5448", "kest": ("25", "17,89", "4,47"), "soli": ("5,5", "4,47", "0,24"), "qty": "100",
                      "isin": "NO0010890304", "wkn": "A2QBSN", "abr": "57858717490", "day": "19.03.2025",
                      "pay": "19.03.2025"}, "13.18"),
    ("liquidation", {"qst": None, "gross": "1,16", "gross_f": "13,70", "per": "0,137", "ccy": "NOK", "fx": "11,7639",
                     "kest": ("25", "1,16", "0,29"), "soli": ("5,5", "0,29", "0,01"), "qty": "100",
                     "isin": "NO0010890304", "wkn": "A2QBSN", "abr": "79363215100", "day": "20.10.2025",
                     "pay": "17.10.2025", "value_day": "21.10.2025"}, "0.86"),
])
def test_capital_repayment_and_liquidation_are_income_with_hint(sub: str, kw: dict, total: str) -> None:
    t = parse(X.ertragsabrechnung(sub, **kw))
    assert t.ok, t.problems
    assert t.sub == ("kapitalrueckzahlung" if sub == "rueckzahlung" else "liquidation")
    assert t.total == D(total) and t.withheld == 0
    assert any("steuerliche Einordnung" in h for h in t.hints)


def test_wrong_total_or_rate_or_fx_makes_the_row_unclear() -> None:
    bad_total = parse(X.ertragsabrechnung(kest=KEST, soli=SOLI, total="2,99"))
    assert not bad_total.ok and any("Ausmachender Betrag" in p and "Differenz" in p for p in bad_total.problems)
    bad_rate = parse(X.ertragsabrechnung(qst=("15", "0,99"), total="2,66"))  # Summe stimmt, Satz × Basis nicht
    assert not bad_rate.ok and any("Einbehaltene Quellensteuer" in p or "Quellensteuer" in p for p in bad_rate.problems)
    bad_fx = parse(X.ertragsabrechnung(fx="2,0970"))
    assert not bad_fx.ok and any("÷" in p for p in bad_fx.problems)
    bad_qty = parse(X.ertragsabrechnung(qty="25"))
    assert not bad_qty.ok and any("×" in p for p in bad_qty.problems)


def test_sign_and_direction_are_checked() -> None:
    assert not parse(X.ertragsabrechnung(direction="Lasten")).ok
    assert not parse(X.ertragsabrechnung(isin="US02079K3050")).ok  # Prüfziffer
    t = parse([ln for ln in X.ertragsabrechnung() if not ln.startswith("Zahlbarkeitstag")])
    assert not t.ok and any("Zahlbarkeitstag" in p for p in t.problems)


def test_second_page_pot_lines_are_not_read() -> None:
    lines = X.ertragsabrechnung(kest=KEST, soli=SOLI)
    assert any(ln.startswith("Kapitalertragsteuer 25,00 % auf 1,00 EUR") for ln in lines)  # Falle nach „Nachrichtlich“
    t = parse(lines)
    assert t.ok and len(t.tax_items) == 2


def test_doctx_fields_and_bridge_rows_dividend_plus_withholding_plus_tax() -> None:
    an = analyze(doc_of(X.ertragsabrechnung(kest=KEST, soli=SOLI)))
    assert (an.doc_type, an.provider) == ("dividend", "dkb") and len(an.txs) == 1
    t = an.txs[0]
    assert t.kind == "dividend" and not t.warnings
    assert t.dec("gross") == D("3.65") and t.value("ccy") == "EUR" and t.dec("withholding_tax") == D("0.55")
    assert t.dec("tax") == D("0.37") and t.dec("net") == D("2.73") and t.value("ext_id") == "57764418230"
    assert t.value("date") == "2025-03-17" and t.value("value_date") == "2025-03-18"
    assert "Rechenprobe" in (t.value("proof") or "") or "=" in (t.value("proof") or "")
    recs = to_recs(Enriched(t, "WKN:A14Y6F", [], None, []), sha="a" * 64, doc={"filename": "x.pdf"}, account="DKB",
                   n0=1)
    kinds = [(r.row["type"], r.row.get("tag"), r.row.get("to_qty") or r.row.get("from_qty")) for r in recs]
    assert kinds == [("deposit", "dividend", "3.65"), ("withdrawal", "withholding_tax", "0.55"),
                     ("withdrawal", "tax", "0.37")]
    assert all(r.row["related_asset"] == "WKN:A14Y6F" for r in recs)
    assert recs[0].row["to_asset"] == "EUR" and recs[0].row["value_eur"] == "3.65"
    # Bargeldbewegung insgesamt = Ausmachender Betrag
    assert D("3.65") - D("0.55") - D("0.37") == D("2.73")


def test_capital_repayment_note_and_review_hint() -> None:
    an = analyze(doc_of(X.ertragsabrechnung("rueckzahlung", **{k: v for k, v in NOK.items() if k != "rueckforderbar"}
                                            | {"qst": None, "gross": "17,89", "gross_f": "206,50", "per": "2,06499",
                                               "kest": ("25", "17,89", "4,47"), "soli": ("5,5", "4,47", "0,24"),
                                               "abr": "57858717490"})))
    t = an.txs[0]
    assert t.kind == "dividend"
    recs = to_recs(Enriched(t, "WKN:A2QBSN", [], None, []), sha="a" * 64, doc={"filename": "x.pdf"}, account="DKB",
                   n0=1)
    assert "Kapitalrückzahlung" in recs[0].note and recs[0].review and "steuerliche Einordnung" in recs[0].review


def test_failed_proof_is_unbookable() -> None:
    t = analyze(doc_of(X.ertragsabrechnung(total="9,99"))).txs[0]
    assert t.kind == "unknown" and any(w.startswith("Rechenprobe") for w in t.warnings)
    recs = to_recs(Enriched(t, None, [], None, []), sha="a" * 64, doc={"filename": "x.pdf"}, account="DKB", n0=1)
    assert len(recs) == 1 and recs[0].kind == "review"


def test_year_crossing_value_date_is_hinted() -> None:
    t = parse(X.ertragsabrechnung(pay="30.12.2024", value_day="02.01.2025", day="02.01.2025"))
    assert t.ok and any("Steuerjahr" in h for h in t.hints)


# -- Lauf über das ZIP ----------------------------------------------------------------------------------------------

DKB = "DKB"
BTX = {"asset_id": "WKN:A14Y6F", "name": "Beispiel Holding", "asset_class": "security", "wkn": "A14Y6F",
       "isin": "US02079K3059", "quote_source": "yahoo", "quote_id": "BHX", "category": "Aktien: Einzeltitel"}


def _start(config: Config, extra: list[dict]) -> TestClient:
    dst = config.import_dir / "imp.zip"
    base = [tx("K-0", "2024-01-05", "buy", to=(DKB, "WKN:A14Y6F", "20"), value="2000.00"), *extra]
    build_zip(dst, transactions=base, assets=[*ASSETS, BTX], generated_at="2025-03-01T00:00:00Z",
              valuation_date="2025-02-28")
    old = time.time() - 3600
    os.utime(dst, (old, old))
    c = TestClient(build_app(config, start_scheduler=False))
    c.__enter__()
    c.get("/settings")
    c.token = c.cookies.get("portfolia_csrf")
    assert tasks.import_check(ctx(c), "test").status == "imported"
    return c


@pytest.fixture
def app(config: Config, master):
    c = _start(config, [])
    r = post(c, "/settings/datasources", kind="exchange", provider="dkb", name=DKB, account=DKB, sync_interval_min="0")
    sid = int(r.headers["location"].split("/settings/datasources/")[1].split("?")[0].split("#")[0])
    yield c, sid
    c.__exit__(None, None, None)


def run_zip(c, sid, entries: dict[str, bytes]) -> dict:
    return finish(c, sid, run_id(upload(c, sid, make_zip(entries))))


def div_pdf(**kw) -> bytes:
    return X.pdf_of(X.ertragsabrechnung(kest=KEST, soli=SOLI, verrechnet=True, **kw))


def test_income_run_suggests_dividend_and_taxes_without_booking(app) -> None:
    c, sid = app
    run = run_zip(c, sid, {"a.pdf": div_pdf(), "b.pdf": X.pdf_of(X.ertragsabrechnung(**NOK))})
    assert run["status"] == "ok", run["message"]
    inc = run["summary"]["income"]
    assert len(inc["new"]) == 2 and not inc["matched"] and not inc["unclear"], inc
    assert "Erträge" in run["message"]
    recs = rows(c, "SELECT r.rec_json FROM csv_row r")
    tags = sorted(json.loads(r["rec_json"])["row"]["tag"] for r in recs)
    assert tags == ["dividend", "dividend", "tax", "tax", "withholding_tax", "withholding_tax"]
    assert rows(c, "SELECT COUNT(*) AS n FROM journal_tx")[0]["n"] == 0  # nichts ohne Bestätigung gebucht
    page = c.get(f"/settings/datasources/{sid}/dkb/zip/{run['id']}").text
    assert "Erträge" in page and "Rechenprobe" in page
    # zweites Hochladen: keine neuen Vorschläge
    n = len(recs)
    run_zip(c, sid, {"a.pdf": div_pdf(), "b.pdf": X.pdf_of(X.ertragsabrechnung(**NOK))})
    assert len(rows(c, "SELECT r.rec_json FROM csv_row r")) == n


def test_failed_proof_income_is_unclear_and_never_suggested(app) -> None:
    c, sid = app
    run = run_zip(c, sid, {"x.pdf": X.pdf_of(X.ertragsabrechnung(total="9,99"))})
    inc = run["summary"]["income"]
    assert len(inc["unclear"]) == 1 and "Rechenprobe" in inc["unclear"][0]["reason"] and not inc["new"]
    recs = [json.loads(r["rec_json"]) for r in rows(c, "SELECT r.rec_json FROM csv_row r")]
    assert recs and all(r["kind"] == "review" and not r.get("row") for r in recs)  # nur Hinweis, keine Buchungszeile


def test_existing_dividend_is_linked_not_duplicated(config: Config, master) -> None:
    existing = tx("D-1", "2025-03-17", "deposit", tag="dividend", to=(DKB, "EUR", "3.65"), value="3.65",
                  related="WKN:A14Y6F")
    c = _start(config, [existing])
    try:
        r = post(c, "/settings/datasources", kind="exchange", provider="dkb", name=DKB, account=DKB,
                 sync_interval_min="0")
        sid = int(r.headers["location"].split("/settings/datasources/")[1].split("?")[0].split("#")[0])
        run = run_zip(c, sid, {"a.pdf": div_pdf()})
        inc = run["summary"]["income"]
        assert [m["tx"] for m in inc["matched"]] == ["D-1"] and not inc["new"], inc
        assert rows(c, "SELECT tx_id, role, source FROM tx_document") == [
            {"tx_id": "D-1", "role": "ertrag", "source": "auto"}]
        # nur die Steuerzeilen bleiben Vorschlag – keine zweite Dividende
        kinds = sorted(json.loads(x["rec_json"])["row"]["tag"] for x in rows(c, "SELECT rec_json FROM csv_row"))
        assert kinds == ["tax", "withholding_tax"]
    finally:
        c.__exit__(None, None, None)


def test_dividend_in_other_account_or_with_other_amount_is_not_linked(config: Config, master) -> None:
    other_amount = tx("D-1", "2025-03-17", "deposit", tag="dividend", to=(DKB, "EUR", "9.99"), value="9.99",
                      related="WKN:A14Y6F")
    other_account = tx("D-2", "2025-03-17", "deposit", tag="dividend", to=("Anderes Depot", "EUR", "3.65"),
                       value="3.65", related="WKN:A14Y6F")
    c = _start(config, [other_amount, other_account])
    try:
        r = post(c, "/settings/datasources", kind="exchange", provider="dkb", name=DKB, account=DKB,
                 sync_interval_min="0")
        sid = int(r.headers["location"].split("/settings/datasources/")[1].split("?")[0].split("#")[0])
        run = run_zip(c, sid, {"a.pdf": div_pdf()})
        inc = run["summary"]["income"]
        assert not inc["matched"] and len(inc["new"]) == 1
    finally:
        c.__exit__(None, None, None)


def test_same_asset_quantity_and_date_but_different_numbers_are_separate_events(app) -> None:
    """Dividende und Kapitalrückzahlung (gleiche ISIN, Menge, Tag) bzw. zwei Käufe mit verschiedener Auftragsnummer
    dürfen im Stapel nicht zu einem Vorgang verschmelzen – die Kennung des Belegs entscheidet."""
    c, sid = app
    a = X.pdf_of(X.ertragsabrechnung(**NOK))
    kw = {k: v for k, v in NOK.items() if k != "rueckforderbar"} | {
        "qst": None, "gross": "17,89", "gross_f": "206,50", "per": "2,06499", "kest": ("25", "17,89", "4,47"),
        "soli": ("5,5", "4,47", "0,24"), "abr": "57858717490"}
    b = X.pdf_of(X.ertragsabrechnung("rueckzahlung", **kw))
    buy1 = X.pdf_of(X.abrechnung("Kauf", order="111111/11.00"))
    buy2 = X.pdf_of(X.abrechnung("Kauf", order="222222/22.00"))
    run = run_zip(c, sid, {"a.pdf": a, "b.pdf": b, "c.pdf": buy1, "d.pdf": buy2})
    assert len(run["summary"]["income"]["new"]) == 2 and len(run["summary"]["trades"]["new"]) == 2
    recs = [json.loads(r["rec_json"]) for r in rows(c, "SELECT r.rec_json FROM csv_row r")]
    keys = {r["event_key"] for r in recs}
    assert keys == {"doc:dkb:57858717480", "doc:dkb:57858717490", "doc:dkb:1111111100", "doc:dkb:2222222200"}
    deposits = sorted(r["row"]["to_qty"] for r in recs if r["row"].get("tag") == "dividend")
    assert deposits == ["17.89", "23.86"]
