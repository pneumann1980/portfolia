# ruff: noqa: F811  (Fixture ``master`` aus test_bitpanda wird importiert und als Argument verwendet)
"""DKB-Steuervergleich (Phase 7): Einlesen der Steuerangaben, Vergleich je Verkauf/Ertrag/Jahr, Schwellen,
Vorbelegung nur mit Bestätigung, keine Änderung an Buchungen. Alle Daten sind erfunden."""

from __future__ import annotations

import os
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.datasources import dkb_taxcompare as T
from app.documentimport.dkb_tax import read_tax
from app.documentimport.extract import DocumentResult, Line, Page
from app.importer.zipbuilder import build_zip
from app.jobs import tasks
from app.main import build_app
from tests import dkbdocs as X
from tests.helpers import ASSETS, tx
from tests.test_bitpanda import master, post  # noqa: F401
from tests.test_dkb import ctx, rows
from tests.test_dkb_zip import finish, make_zip, run_id, upload

D = Decimal
DKB = "DKB"
SALE_ASSET = {"asset_id": "WKN:A2QA4J", "name": "Beispiel Tech", "asset_class": "security", "wkn": "A2QA4J",
              "isin": "US69608A1088", "quote_source": "yahoo", "quote_id": "BTX", "category": "Aktien: Einzeltitel"}
DIV_ASSET = {"asset_id": "WKN:A14Y6F", "name": "Beispiel Holding", "asset_class": "security", "wkn": "A14Y6F",
             "isin": "US02079K3059", "quote_source": "yahoo", "quote_id": "BHX", "category": "Aktien: Einzeltitel"}
KEST = (("Kapitalertragsteuer", "25,00", "8.765,71", "2.191,43"),
        ("Solidaritätszuschlag", "5,50", "2.191,43", "120,52"))
LOT = ("1000000001", "17.11.2021", "100,0000", "2.064,50", "11.295,00", "9.230,50")


def doc_of(lines: list[str]) -> DocumentResult:
    ls = [Line(text, 1, i + 1) for i, text in enumerate(lines)]
    return DocumentResult(sha256="a" * 64, file_type="pdf", pages=[Page(1, "text", ls)])


def sale_lines(**kw) -> list[str]:
    args = {"side": "Verkauf", "order": "518604/81.00", "day": "17.02.2025", "isin": "US69608A1088", "wkn": "A2QA4J",
            "name": "BEISPIEL TECH INC.", "qty": "100", "price": "113,00 EUR", "fx": None, "kurswert": "11.300,00",
            "fees": (("Provision", "5,00"),), "taxes": KEST, "close": "17.02.2025 15:30:00",
            "tail": X.steuer_tail(before=("0,00", "0,00", "464,79", "0,00", "0,00"),
                                  after=("0,00", "0,00", "0,00", "0,00", "8.765,71"), lots=(LOT,))}
    return X.abrechnung(**{**args, **kw})


def income_lines(**kw) -> list[str]:
    args = {"kest": ("25", "3,10", "0,78"), "soli": ("5,5", "0,78", "0,04"),
            "pots": X.topf_zeilen(before=("0,00", "0,00", "0,00", "0,00", "100,00"),
                                  after=("0,00", "0,00", "0,00", "0,00", "103,10"))}
    return X.ertragsabrechnung(**{**args, **kw})


# -- Parser ---------------------------------------------------------------------------------------------------------

def test_sale_tax_lots_and_pots_are_read() -> None:
    t = read_tax(doc_of(sale_lines()))
    assert t is not None and t.kind == "verkauf" and t.isin == "US69608A1088"
    assert (t.gain, t.kest, t.soli, t.kist, t.tax) == (D("9230.50"), D("2191.43"), D("120.52"), D(0), D("2311.95"))
    assert t.pot_year == 2025 and t.before["pauschbetrag"] == D("464.79") and t.after["basis"] == D("8765.71")
    (lot,) = t.lots
    assert (lot["order_no"], lot["date"], lot["qty"], lot["cost"], lot["proceeds"], lot["result"]) == (
        "1000000001", "2021-11-17", D("100.0000"), D("2064.50"), D("11295.00"), D("9230.50"))
    assert t.lots_sum == D("9230.50")
    assert type(t).from_json(t.to_json()).to_json() == t.to_json()  # JSON-Roundtrip (bank_doc.meta)


def test_loss_refund_and_negative_result() -> None:
    lines = X.abrechnung(
        "Verkauf", gain="-150,33", fees=(("Provision", "10,00"),), taxes=(), kurswert="195,99", qty="100",
        price="1,9599 EUR", fx=None,
        refund=(("Kapitalertragsteuer", "25,00", "150,33", "37,58"), ("Solidaritätszuschlag", "5,50", "37,58", "2,06")),
        tail=X.steuer_tail(lots=(("1000000002", "22.11.2023", "200,0000", "310,20", "159,87", "150,33-"),)))
    t = read_tax(doc_of(lines))
    assert t is not None and t.gain == D("-150.33")
    assert (t.kest, t.soli) == (D("-37.58"), D("-2.06"))  # Erstattung: negativ
    assert t.lots[0]["result"] == D("-150.33")


def test_income_tax_fields() -> None:
    t = read_tax(doc_of(income_lines()))
    assert t is not None and t.kind == "ertrag"
    assert (t.gross, t.qst, t.kest, t.soli) == (D("3.65"), D("0.55"), D("0.78"), D("0.04"))
    assert t.before["basis"] == D("100.00") and t.after["basis"] == D("103.10")


def test_failed_proof_gives_no_tax_info() -> None:
    assert read_tax(doc_of(sale_lines(total="1,00"))) is None
    assert read_tax(doc_of(X.depotauszug_voll())) is None  # kein Verkauf/Ertrag
    assert read_tax(doc_of(X.abrechnung("Kauf"))) is None


@pytest.mark.parametrize(("diff", "expected"), [
    (D("0"), "gleich"), (D("0.04"), "rundung"), (D("-0.50"), "klein"), (D("1.00"), "info"), (D("-9.99"), "info"),
    (D("10.00"), "warn"), (None, "offen")])
def test_levels(diff, expected) -> None:
    assert T.level(diff, T.THRESHOLDS) == expected


def test_thresholds_from_query_are_validated() -> None:
    assert T.thresholds({"hinweis": "2,5", "warnung": "20"})["info"] == D("2.5")
    assert T.thresholds({"hinweis": "50", "warnung": "20"}) == T.THRESHOLDS  # Reihenfolge verletzt → Standard
    assert T.thresholds({"hinweis": "abc"}) == T.THRESHOLDS


# -- Lauf über das ZIP und Vergleich ----------------------------------------------------------------------------------

def _start(config: Config, extra: list[dict] | None = None, lot_value: str = "2064.50", taxes: bool = True,
           split: bool = False) -> TestClient:
    dst = config.import_dir / "imp.zip"
    buys = ([tx("B-1", "2021-11-17", "buy", to=(DKB, "WKN:A2QA4J", "60"), value="1238.70"),
             tx("B-2", "2022-01-10", "buy", to=(DKB, "WKN:A2QA4J", "40"), value="825.80")] if split else
            [tx("B-1", "2021-11-17", "buy", to=(DKB, "WKN:A2QA4J", "100"), value=lot_value)])
    base = [*buys,
            tx("S-1", "2025-02-17", "sell", frm=(DKB, "WKN:A2QA4J", "100"), to=(DKB, "EUR", "11295.00"),
               value="11300.00", fee=("EUR", "5.00", "5.00")),
            tx("D-1", "2025-03-18", "deposit", tag="dividend", to=(DKB, "EUR", "3.65"), value="3.65",
               related="WKN:A14Y6F"),
            *([tx("T-1", "2025-02-17", "withdrawal", tag="tax", frm=(DKB, "EUR", "2311.95"), value="2311.95",
                  related="WKN:A2QA4J"),
               tx("W-1", "2025-03-18", "withdrawal", tag="withholding_tax", frm=(DKB, "EUR", "0.55"), value="0.55",
                  related="WKN:A14Y6F"),
               tx("T-2", "2025-03-18", "withdrawal", tag="tax", frm=(DKB, "EUR", "0.82"), value="0.82",
                  related="WKN:A14Y6F")] if taxes else []),
            *(extra or [])]
    build_zip(dst, transactions=base, assets=[*ASSETS, SALE_ASSET, DIV_ASSET], generated_at="2025-04-01T00:00:00Z",
              valuation_date="2025-03-31")
    old = time.time() - 3600
    os.utime(dst, (old, old))
    c = TestClient(build_app(config, start_scheduler=False))
    c.__enter__()
    c.get("/settings")
    c.token = c.cookies.get("portfolia_csrf")
    assert tasks.import_check(ctx(c), "test").status == "imported"
    return c


def _sid(c) -> int:
    r = post(c, "/settings/datasources", kind="exchange", provider="dkb", name=DKB, account=DKB, sync_interval_min="0")
    return int(r.headers["location"].split("/settings/datasources/")[1].split("?")[0].split("#")[0])


def _prepare(c, sid, read: bool = True) -> None:
    run = finish(c, sid, run_id(upload(c, sid, make_zip({
        "sale.pdf": X.pdf_of(sale_lines()), "div.pdf": X.pdf_of(income_lines())}))))
    assert run["status"] == "ok", run["message"]
    if read:
        r = post(c, f"/settings/datasources/{sid}/dkb/steuer/einlesen")
        assert r.status_code == 303


@pytest.fixture
def app(config: Config, master):
    c = _start(config)
    sid = _sid(c)
    yield c, sid
    c.__exit__(None, None, None)


def _build(c, sid, year=2025, params=None) -> dict:
    ds = ctx(c).db.q1("SELECT * FROM data_source WHERE id=?", (sid,))
    from app.datasources.service import datasource_service

    return T.build(ctx(c), sid, datasource_service(ctx(c)).get(sid) or ds, year, params)


def test_matching_data_gives_no_deviation(app) -> None:
    c, sid = app
    _prepare(c, sid)
    d = _build(c, sid)
    assert d["docs"] == 2 and d["unread"] == 0 and not d.get("error")
    (s,) = d["sales"]
    assert s["tx"] == "S-1" and s["dkb_gain"] == D("9230.50") and s["pf_gain"] == D("9230.50"), s
    assert s["gain_level"] == "gleich" and s["tax_level"] == "gleich" and s["lots_state"] == "gleich", s
    assert s["pf_tax"] == D("2311.95") and not s["causes"]
    (i,) = d["income"]
    assert i["tx"] == "D-1" and (i["gross_level"], i["qst_level"], i["tax_level"]) == ("gleich",) * 3, i
    assert (i["pf_gross"], i["pf_qst"], i["pf_tax"]) == (D("3.65"), D("0.55"), D("0.82"))
    # Jahr: Summen und Topf-Endstände (Aktien-Topf 0, Pauschbetrag laut DKB 8.765,71 Basis ist keine Topfgröße)
    s_lines = {ln["label"]: ln for ln in d["summary"]["lines"]}
    assert s_lines["Veräußerungsgewinne/-verluste (Summe)"]["dkb"] == D("9230.50")
    assert s_lines["Quellensteuer einbehalten"]["level"] == "gleich"
    pots = {p["label"]: p for p in d["summary"]["pots"]}
    assert pots["Verlusttopf Aktien"]["dkb"] == 0 and pots["Verlusttopf Aktien"]["level"] == "gleich"


def test_page_is_read_only_and_german(app) -> None:
    c, sid = app
    _prepare(c, sid)
    before = (rows(c, "SELECT COUNT(*) AS n FROM journal_tx")[0]["n"], rows(c, "SELECT COUNT(*) AS n FROM tx_document"),
              ctx(c).settings.get("tax.options"))
    page = c.get(f"/settings/datasources/{sid}/dkb/steuer")
    assert page.status_code == 200
    assert "Steuervergleich DKB" in page.text and "Beispiel Tech" in page.text and "ändert keine Buchung" in page.text
    assert "Topf-Endstände" in page.text and "Erträge" in page.text
    after = (rows(c, "SELECT COUNT(*) AS n FROM journal_tx")[0]["n"], rows(c, "SELECT COUNT(*) AS n FROM tx_document"),
            ctx(c).settings.get("tax.options"))
    assert before == after
    assert c.get(f"/settings/datasources/{sid}/dkb/steuer?year=1999").status_code == 200  # unbekanntes Jahr → Standard


def test_unread_documents_are_counted_and_not_compared(app) -> None:
    c, sid = app
    _prepare(c, sid, read=False)
    assert T.pending(ctx(c), sid) == 2
    d = _build(c, sid)
    assert d["unread"] == 2 and not d["sales"] and not d["income"]
    page = c.get(f"/settings/datasources/{sid}/dkb/steuer").text
    assert "noch nicht auf Steuerangaben gelesen" in page
    post(c, f"/settings/datasources/{sid}/dkb/steuer/einlesen")
    assert T.pending(ctx(c), sid) == 0


def test_deviation_cause_missing_basis_and_lots(config: Config, master) -> None:
    # Einstand 2.000 statt 2.064,50 und anderes Kaufdatum → Gewinn weicht um 64,50 ab, Lot-Auswahl abweichend
    c = _start(config, lot_value="2000.00")
    try:
        sid = _sid(c)
        _prepare(c, sid)
        (s,) = _build(c, sid)["sales"]
        assert s["gain_diff"] == D("64.50") and s["gain_level"] == "warn", s
        assert s["lots_state"] == "gleich"  # Datum und Menge stimmen, nur der Einstand weicht ab
        assert s["causes"] == ["Ursache nicht erkennbar (Einstand/Gebühren prüfen)"]
    finally:
        c.__exit__(None, None, None)


def test_missing_tax_lines_in_portfolia_are_visible(config: Config, master) -> None:
    c = _start(config, taxes=False)
    try:
        sid = _sid(c)
        _prepare(c, sid)
        d = _build(c, sid)
        (s,) = d["sales"]
        assert s["pf_tax"] == 0 and s["tax_diff"] == D("-2311.95") and s["tax_level"] == "warn"
        (i,) = d["income"]
        assert (i["qst_diff"], i["tax_diff"]) == (D("-0.55"), D("-0.82"))
        assert (i["qst_level"], i["tax_level"]) == ("klein", "klein")  # unter 1 € bleibt unter der Hinweisschwelle
    finally:
        c.__exit__(None, None, None)


def test_other_lot_selection_is_named(config: Config, master) -> None:
    c = _start(config, split=True)
    try:
        sid = _sid(c)
        _prepare(c, sid)
        (s,) = _build(c, sid)["sales"]
        assert s["gain_level"] == "gleich" and s["lots_state"] == "abweichend", s
        assert "17.11.2021 100" in s["lots"] and "10.01.2022 40" in s["lots"]
        page = c.get(f"/settings/datasources/{sid}/dkb/steuer").text
        assert "Lots:" in page
    finally:
        c.__exit__(None, None, None)


def test_prefill_with_confirmation_sets_options(config: Config, master) -> None:
    c = _start(config)
    try:
        sid = _sid(c)
        lines = sale_lines(tail=X.steuer_tail(before=("1.234,56", "10,00", "2.000,00", "0,00", "0,00"),
                                              after=("0,00", "0,00", "2.000,00", "0,00", "0,00"), lots=(LOT,)))
        finish(c, sid, run_id(upload(c, sid, make_zip({"sale.pdf": X.pdf_of(lines)}))))
        post(c, f"/settings/datasources/{sid}/dkb/steuer/einlesen")
        items = {i["key"]: i for i in _build(c, sid)["proposal"]["items"]}
        assert items["loss_cf_kap_shares"]["value"] == D("1234.56")
        assert items["loss_cf_kap_other"]["value"] == D("10.00")
        assert items["joint"]["value"] is True  # Pauschbetrag 2.000 € = Zusammenveranlagung
        r = post(c, f"/settings/datasources/{sid}/dkb/steuer/vorbelegen", year="2025", confirm="1",
                 key=["loss_cf_kap_shares", "joint"])
        assert r.status_code == 303 and "msg=" in r.headers["location"], r.headers["location"]
        from app.tax.service import tax_service

        svc = tax_service(ctx(c))
        o = svc.options(svc.pack(), 2025)
        assert str(o["loss_cf_kap_shares"]) == "1234.56" and o["joint"] is True
        assert not o.get("loss_cf_kap_other")  # nicht ausgewählt → unverändert
    finally:
        c.__exit__(None, None, None)


def test_prefill_needs_confirmation_and_uses_server_values(app) -> None:
    c, sid = app
    _prepare(c, sid)
    prop = _build(c, sid)["proposal"]
    keys = {i["key"] for i in prop["items"]}
    assert "joint" not in keys or prop["items"]  # Pauschbetrag 464,79 → weder Einzel- noch Zusammenveranlagung
    assert "Sparer-Pauschbetrag laut DKB zu Jahresbeginn 464.79" in prop["note"]
    # ohne Bestätigung: nichts passiert
    r = post(c, f"/settings/datasources/{sid}/dkb/steuer/vorbelegen", year="2025", key="loss_cf_kap_shares")
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert not (ctx(c).settings.get("tax.options") or {})
    # unbekannter Schlüssel mit Bestätigung → nichts zu übernehmen
    r = post(c, f"/settings/datasources/{sid}/dkb/steuer/vorbelegen", year="2025", key="marginal_rate", confirm="1")
    assert "error=" in r.headers["location"]
    assert not (ctx(c).settings.get("tax.options") or {})
