"""M25 Dokumentimport: Extraktion, Zahlenformate, Profile, Recherche, Abgleich, Wiederholbarkeit, Sicherheit.

Ausschließlich synthetische Belege (tests/docfixtures.py) und gemockte Explorer (httpx.MockTransport) – keine
produktiven Nutzerdaten, keine Netzwerkzugriffe.
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import threading
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.config import Config
from app.context import AppContext
from app.documentimport import enrich as E
from app.documentimport import parse as P
from app.documentimport.bridge import missing, to_recs
from app.documentimport.extract import DocumentError, extract_document, sniff
from app.documentimport.profiles import analyze
from app.documentimport.service import document_service, normalize
from app.documentimport.worker import Cancelled, extract_isolated
from app.importer.loader import import_file
from app.importer.zipbuilder import build_zip
from tests.docfixtures import pdf, scanned_pdf, screenshot, text_pdf
from tests.helpers import ASSETS, tx

OCR = shutil.which("tesseract") is not None
needs_ocr = pytest.mark.skipif(not OCR, reason="Tesseract nicht installiert (im Docker-Image und in CI vorhanden)")
HASH = "a1" * 32
SAP = {"asset_id": "SAP", "name": "SAP SE", "asset_class": "security", "quote_source": "yahoo", "quote_id": "SAP.DE",
       "isin": "DE0007164600", "wkn": "716460"}
BANK = ["Musterbank AG", "Wertpapierabrechnung Kauf", "Depot  1234567890",
        "Handelstag  02.01.2025  Handelszeit  09:15:03", "ISIN  DE0007164600  Stück  10", "SAP SE Inhaber-Aktien",
        "Kurs  123,45 EUR  Kurswert  1.234,50 EUR", "Provision  4,90 EUR", "Ausmachender Betrag  1.239,40 EUR",
        "Auftragsnummer  A-778899"]
BTC_OUT = ["Ledger Live", "Auszahlung BTC", "Datum  12.03.2025", "Menge  0,015 BTC", f"Transaktions-Hash  {HASH}",
           "Netzwerk  Bitcoin"]


def _an(lines: list[str]):
    return analyze(extract_document(text_pdf(lines), ocr=False))


def _ctx(config: Config, rows: list[dict] | None = None) -> AppContext:
    base = [tx("D1", "2024-12-01T10:00:00Z", "deposit", to=("Depot", "EUR", "5000"), value="5000")]
    build_zip(config.import_dir / "k.zip", transactions=base + (rows or []), assets=[*ASSETS, SAP],
              generated_at="2025-03-01T00:00:00Z", valuation_date="2024-12-31")
    ctx = AppContext(config)
    ctx.startup()
    assert import_file(ctx.db, config.import_dir / "k.zip", ctx.engine_options()).status == "imported"
    ctx.invalidate_data()
    return ctx


# -- AP1: Annahme und Formaterkennung ----------------------------------------------------------------------

def test_sniff_by_content_not_name() -> None:
    from PIL import Image

    assert sniff(text_pdf(["x"])) == "pdf"
    for fmt, kind in (("PNG", "png"), ("JPEG", "jpeg"), ("WEBP", "webp")):
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), "white").save(buf, format=fmt)
        assert sniff(buf.getvalue()) == kind
    for bad in (b"", b"PK\x03\x04docx", b"MZ\x90\x00", b"<html><script>alert(1)</script>"):
        with pytest.raises(DocumentError):
            sniff(bad)


def test_corrupt_pdf_and_pixel_bomb_rejected() -> None:
    from PIL import Image

    with pytest.raises(DocumentError):
        extract_document(b"%PDF-1.4\n1 0 obj << broken", ocr=False)
    buf = io.BytesIO()
    Image.new("1", (8000, 8000)).save(buf, format="PNG")  # 64 MPx > Grenze, als PNG nur wenige KB
    with pytest.raises(DocumentError):
        extract_document(buf.getvalue(), ocr=True)


def test_pdf_scripts_are_not_executed() -> None:
    """Eingebettetes JavaScript/OpenAction wird ignoriert: nur Text wird gelesen."""
    raw = text_pdf(BANK)
    js = b"<< /Type /Action /S /JavaScript /JS (app.alert('x')) >>"
    raw = raw.replace(b"/Type /Catalog", b"/Type /Catalog /OpenAction " + js, 1)
    res = extract_document(raw, ocr=False)
    assert any("Wertpapierabrechnung" in ln.text for ln in res.lines())


# -- AP2: Zahlen, Datum, Extraktion ------------------------------------------------------------------------

@pytest.mark.parametrize(("raw", "dec", "want"), [
    ("1.234,56", None, "1234.56"), ("1,234.56", None, "1234.56"), ("0,00512345", None, "0.00512345"),
    ("1.234", ",", "1234"), ("1.234", ".", "1.234"), ("1'234.50", None, "1234.50"), ("12 345,67", None, "12345.67"),
])
def test_number_formats_exact_decimal(raw: str, dec: str | None, want: str) -> None:
    v = P.number(raw, P.Convention(dec) if dec else None)
    assert isinstance(v, Decimal) and v == Decimal(want)


@pytest.mark.parametrize("raw", ["1.234", "1,234", "1.234.5", "abc"])
def test_ambiguous_numbers_not_guessed(raw: str) -> None:
    with pytest.raises(P.Ambiguous):
        P.number(raw)


def test_dates_slash_ambiguity_reported() -> None:
    ds, notes = P.dates("03/04/2025")
    assert not ds and notes
    ds, _ = P.dates("Handelstag 02.01.2025 und 2025-01-03")
    assert [d.isoformat() for d, _s, _e in ds] == ["2025-01-02", "2025-01-03"]


def test_securities_buy_fields_and_provenance() -> None:
    an = _an(BANK)
    assert an.doc_type == "securities_trade" and an.convention.decimal == ","
    t = an.txs[0]
    assert t.kind == "buy"
    want = {"isin": "DE0007164600", "quantity": "10", "price": "123.45", "gross": "1234.50", "fee": "4.90",
            "net": "1239.40", "date": "2025-01-02", "time": "09:15:03", "value_eur": "1234.50", "fee_eur": "4.90"}
    for k, v in want.items():
        assert t.value(k) == v, k
        assert t.status(k) == "belegt", k
    gross = t.decisions["gross"].selected
    assert gross.origin == "document" and gross.page == 1 and gross.line and gross.box  # Fundstelle
    assert not t.warnings
    assert "1234567890" not in json.dumps([e.value for e in t.evidence])  # Depotnummer nur maskiert


def test_arithmetic_contradiction_is_flagged_for_review() -> None:
    an = _an(["Musterbank AG", "Wertpapierabrechnung Kauf", "Handelstag  02.01.2025",
              "ISIN  DE0007164600  Stück  10", "Kurs  123,45 EUR  Kurswert  1.300,00 EUR", "Provision  4,90 EUR",
              "Ausmachender Betrag  1.304,90 EUR"])
    t = an.txs[0]
    assert any("weicht vom Kurswert" in w for w in t.warnings)
    e = E.Enriched(t, "SAP", ["SAP"], None, [])
    recs = to_recs(e, sha="0" * 64, doc={}, account="Depot", n0=1)
    assert recs[0].review and "weicht" in recs[0].review  # nie automatisch übernehmbar


def test_dividend_foreign_currency_with_fx() -> None:
    an = _an(["Musterbank AG", "Dividendengutschrift", "Zahltag  15.05.2025", "ISIN  US0378331005  Stück  20",
              "Dividende pro Stück  0,25 USD", "Bruttobetrag  5,00 USD", "Devisenkurs  EUR/USD 1,1000",
              "Quellensteuer  0,75 USD", "Ausmachender Betrag  3,86 EUR"])
    t = an.txs[0]
    assert t.kind == "dividend" and t.value("ccy") == "USD"
    assert t.value("value_eur") == "4.55" and t.status("value_eur") == "rekonstruiert"  # 5,00 / 1,10 – belegter Kurs
    recs = to_recs(E.Enriched(t, None, [], None, []), sha="1" * 64, doc={}, account="Depot", n0=1)
    assert recs[0].row["to_asset"] == "EUR" and recs[0].row["to_qty"] == "4.55"
    wht = next(r for r in recs if r.row.get("tag") == "withholding_tax")
    assert wht.row["from_qty"] == "0.68" and wht.row["related_asset"] == "US0378331005"


def test_international_number_format() -> None:
    an = _an(["Example Broker Inc.", "Trade Confirmation Buy", "Trade date  2025-01-02",
              "ISIN  US0378331005  Quantity  3", "Price  1,234.50 USD  Gross amount  3,703.50 USD",
              "Commission  1.00 USD", "Exchange rate  1.0500", "Net amount  3,704.50 USD"])
    t = an.txs[0]
    assert an.convention.decimal == "." and t.value("gross") == "3703.50" and t.value("price") == "1234.50"
    assert t.status("value_eur") == "rekonstruiert"


def _statement(pages: int, header: str) -> bytes:
    out, n = [], 0
    for p in range(pages):
        pg = ["Krypto-Börse Kontoauszug", header]
        for i in range(20):
            n += 1
            day, month = (n % 28) + 1, (p % 9) + 1
            pg.append(f"{day:02d}.0{month}.2024  Kauf  ETH  0,0{i + 1}  2.000,00  {20 * (i + 1)},00  0,10")
        out.append(pg)
    return text_pdf(*out)


def test_statement_ten_pages_and_header_currency() -> None:
    an = analyze(extract_document(_statement(10, "Datum  Typ  Asset  Menge  Preis (EUR)  Betrag (EUR)  Gebühr (EUR)"),
                                  ocr=False))
    assert an.doc_type == "statement" and len(an.txs) == 200
    t = an.txs[0]
    assert t.value("ccy") == "EUR" and "Spaltenkopf" in t.decisions["ccy"].selected.reason
    assert t.value("value_eur") == "20.00" and t.value("fee_eur") == "0.10"


def test_missing_currency_is_never_assumed() -> None:
    an = analyze(extract_document(_statement(1, "Datum  Typ  Asset  Menge  Preis  Betrag  Gebühr"), ocr=False))
    t = an.txs[0]
    assert t.value("ccy") is None
    e = E.Enriched(t, None, [], None, [])
    assert "ccy" in missing(e)
    recs = to_recs(e, sha="2" * 64, doc={}, account="Börse", n0=1)
    assert recs[0].kind == "review" and "Währung" in (recs[0].review or "")


@needs_ocr
@pytest.mark.parametrize("dark", [False, True])
def test_screenshot_ocr(dark: bool) -> None:
    img = screenshot(["Bitpanda", "Kauf  BTC", "Menge  0,00512345 BTC", "Preis  45.123,40 €", "Betrag  231,18 €",
                      "Gebühr  1,49 €", "Datum  03.02.2025 14:22"], dark=dark)
    res = extract_document(img)
    assert res.ocr_used
    an = analyze(res)
    t = an.txs[0]
    assert an.provider == "bitpanda" and t.kind == "buy"
    assert t.value("quantity") == "0.00512345" and t.value("gross") == "231.18" and t.value("fee") == "1.49"
    sel = t.decisions["quantity"].selected
    assert sel.conf is not None and sel.box is not None


@needs_ocr
def test_scanned_pdf_ocr() -> None:
    res = extract_document(scanned_pdf(BANK))
    assert res.ocr_used and res.pages[0].method == "ocr"
    t = analyze(res).txs[0]
    assert t.value("isin") == "DE0007164600" and t.value("gross") == "1234.50"


def test_isolated_worker_matches_and_cancels() -> None:
    data = text_pdf(BANK)
    iso = extract_isolated(data, ocr=False)
    assert [ln.text for ln in iso.lines()] == [ln.text for ln in extract_document(data, ocr=False).lines()]
    ev = threading.Event()
    ev.set()
    with pytest.raises(Cancelled):
        extract_isolated(data, ocr=False, cancel=ev)


def test_multi_page_pdf_with_empty_page() -> None:
    raw = pdf([[(56, 64, "Musterbank AG")], [], [(56, 64, line) for line in BANK]])
    res = extract_document(raw, ocr=False)
    assert len(res.pages) == 3


# -- Korrekturen -------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("name", "raw", "want"), [
    ("fee", "1.234,56", "1234.56"), ("fee", "4,90", "4.90"), ("quantity", "0.015", "0.015"),
    ("date", "02.01.2025", "2025-01-02"), ("time", "9:15", "09:15:00"), ("ccy", "usd", "USD"),
    ("isin", "de 0007164600", "DE0007164600"),
])
def test_correction_normalized(name: str, raw: str, want: str) -> None:
    assert normalize(name, raw) == want


@pytest.mark.parametrize(("name", "raw"), [("fee", "-1"), ("fee", "1.2.3"), ("isin", "DE0007164601"),
                                           ("date", "13/14/2025"), ("ccy", "Euro"), ("kind", "trade")])
def test_correction_rejected(name: str, raw: str) -> None:
    with pytest.raises(ValueError):
        normalize(name, raw)


# -- AP3: Recherche (Portfolia, Datenquellen, öffentliche Explorer) ----------------------------------------

class _Recorder:
    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.status, self.body = status, body

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body if self.body is not None else {})


def test_public_lookup_sends_only_hash_and_marks_origin(config: Config) -> None:
    ctx = _ctx(config)
    t = _an(BTC_OUT).txs[0]
    rec = _Recorder(body={"txid": HASH, "fee": 1410, "status": {"confirmed": True, "block_time": 1741780800}})
    e = E.enrich(E.Context.build(ctx), t, E.Budget(), public=True, transport=httpx.MockTransport(rec))
    assert len(rec.requests) == 1
    r = rec.requests[0]
    assert r.method == "GET" and r.url.host == "mempool.space" and r.url.path == f"/api/tx/{HASH}"
    assert not r.url.query and not r.content  # nur der Hash, keine Adresse/Menge/Kontodaten
    nf = e.tx.decisions["network_fee"].selected
    assert nf.origin == "public" and Decimal(nf.value) == Decimal("0.0000141")
    assert any(s.stage == 4 and s.ok for s in e.steps)
    recs = to_recs(e, sha="3" * 64, doc={}, account="Ledger", n0=1)
    assert "öffentlichen Quellen" in (recs[0].review or "")  # nie ohne Bestätigung


def test_public_lookup_off_by_default_and_errors_not_invented(config: Config) -> None:
    ctx = _ctx(config)
    assert E.public_lookup_enabled(ctx) is False
    rec = _Recorder()
    E.enrich(E.Context.build(ctx), _an(BTC_OUT).txs[0], E.Budget(), public=False,
             transport=httpx.MockTransport(rec))
    assert rec.requests == []
    bad = _Recorder(status=500)
    e = E.enrich(E.Context.build(ctx), _an(BTC_OUT).txs[0], E.Budget(), public=True,
                 transport=httpx.MockTransport(bad))
    assert "network_fee" not in e.tx.decisions or e.tx.decisions["network_fee"].selected is None
    assert any(s.stage == 4 and not s.ok for s in e.steps)


def test_portfolio_identity_reconstructs_fields(config: Config) -> None:
    uuid = "1eecf73c-0000-4000-8000-00000000abcd"
    k = tx("K1", "2025-02-03T14:22:05Z", "buy", frm=("Bitpanda", "EUR", "231.18"),
           to=("Bitpanda", "BTC", "0.00512345"), value="231.18")
    k["source"], k["source_ref"] = "bitpanda", uuid
    ctx = _ctx(config, [k])
    t = _an(["Bitpanda GmbH", "Kaufbestätigung", "Kauf  BTC", "Menge  0,00512345 BTC", "Betrag  231,18 EUR",
             "Datum  03.02.2025", f"Transaktions-ID  {uuid}"]).txs[0]
    assert t.value("time") is None
    e = E.enrich(E.Context.build(ctx), t, E.Budget())
    assert e.existing is not None and e.existing.tx_id == "K1"
    tm = e.tx.decisions["time"].selected
    assert tm.value == "14:22:05" and tm.status == "rekonstruiert" and tm.origin == "portfolio"


# -- AP4/AP5: Stapel, Abgleich, Wiederholbarkeit -----------------------------------------------------------

def test_stack_run_stages_without_booking_and_is_idempotent(config: Config, caplog) -> None:
    ctx = _ctx(config)
    svc = document_service(ctx)
    data = text_pdf(BANK)
    caplog.set_level(logging.DEBUG)
    acc = svc.accept([("a.pdf", data)], account="Depot")
    summ = svc.run(acc.stack_id)
    assert summ["transactions"] == 1 and summ["rows"] == {"new": 1}
    assert ctx.db.scalar("SELECT COUNT(*) FROM journal_tx", default=0) == 0
    assert ctx.db.scalar("SELECT COUNT(*) FROM tx_override", default=0) == 0
    row = ctx.db.q1("SELECT * FROM csv_row")
    rec = json.loads(row["rec_json"])
    assert rec["row"]["to_asset"] == "SAP" and Decimal(rec["row"]["value_eur"]) == Decimal("1234.50")
    assert Decimal(rec["row"]["fee_qty"]) == Decimal("4.90") and rec["raw"]["fields"]["gross"]["status"] == "belegt"
    # Logs enthalten keine Dateiinhalte
    text = caplog.text
    assert "Musterbank" not in text and "DE0007164600" not in text and "1234567890" not in text
    # gleicher Inhalt erneut → erkannt, kein neuer Stapel
    again = svc.accept([("kopie.pdf", data)])
    assert again.stack_id is None and again.known
    # ausdrücklich neu auswerten → derselbe Vorgang, keine zweite offene Zeile
    re = svc.accept([("kopie.pdf", data)], reanalyze=True)
    svc.run(re.stack_id)
    assert ctx.db.scalar("SELECT COUNT(*) FROM csv_row WHERE status='new'", default=0) <= 1


def test_changed_version_supersedes_previous(config: Config) -> None:
    ctx = _ctx(config)
    svc = document_service(ctx)
    svc.run(svc.accept([("v1.pdf", text_pdf(BANK))]).stack_id)
    v2 = [*BANK[:-1], "Storno/Korrektur der Abrechnung", BANK[-1]]
    svc.run(svc.accept([("v2.pdf", text_pdf(v2))]).stack_id)
    d1, d2 = ctx.db.q("SELECT * FROM document ORDER BY id")
    assert d2["supersedes"] == d1["id"]


def test_two_documents_same_event_are_merged(config: Config) -> None:
    ctx = _ctx(config)
    svc = document_service(ctx)
    uuid = "1eecf73c-0000-4000-8000-00000000beef"
    a = text_pdf(["Bitpanda GmbH", "Kaufbestätigung", "Kauf  BTC", "Menge  0,01 BTC", "Betrag  450,00 EUR",
                  "Datum  04.02.2025", f"Transaktions-ID  {uuid}"])
    b = text_pdf(["Bitpanda GmbH", "Kauf  BTC", "Menge  0,01 BTC", "Gebühr  1,49 EUR", "Datum  04.02.2025 10:11:12",
                  f"Transaktions-ID  {uuid}"])
    summ = svc.run(svc.accept([("a.pdf", a), ("b.pdf", b)]).stack_id)
    assert summ["transactions"] == 1  # ein Vorgang, eine Buchung
    rec = json.loads(ctx.db.q1("SELECT rec_json FROM csv_row")["rec_json"])
    f = rec["raw"]["fields"]
    assert f["fee"]["value"] == "1.49" and f["time"]["value"] == "10:11:12"
    assert {f["fee"]["origin"], f["gross"]["origin"]} <= {"document", "batch"}


def test_cancel_leaves_nothing_staged(config: Config) -> None:
    ctx = _ctx(config)
    svc = document_service(ctx)
    acc = svc.accept([("a.pdf", text_pdf(BANK))])
    ev = threading.Event()
    ev.set()
    assert svc.run(acc.stack_id, ev) == {"cancelled": True}
    assert ctx.db.scalar("SELECT COUNT(*) FROM csv_row", default=0) == 0
    assert ctx.db.q1("SELECT status FROM document_stack")["status"] == "cancelled"
    assert not list((Path(config.data_dir) / "tmp" / "documents").glob("*.in"))


def test_restart_recovers_interrupted_stack(config: Config) -> None:
    ctx = _ctx(config)
    svc = document_service(ctx)
    acc = svc.accept([("a.pdf", text_pdf(BANK))])
    ctx.db.x("UPDATE document_stack SET status='running' WHERE id=?", (acc.stack_id,))
    AppContext(config).startup()  # Neustart während des Laufs
    assert ctx.db.q1("SELECT status FROM document_stack")["status"] == "failed"
    assert ctx.db.q1("SELECT status FROM document")["status"] == "failed"
    assert not (Path(config.data_dir) / "tmp" / "documents").exists()
    # erneuter Upload funktioniert
    again = svc.accept([("a.pdf", text_pdf(BANK))])
    assert again.stack_id and svc.run(again.stack_id)["transactions"] == 1


def test_originals_unchanged_and_not_kept_when_disabled(config: Config) -> None:
    ctx = _ctx(config)
    svc = document_service(ctx)
    data = text_pdf(BANK)
    svc.run(svc.accept([("a.pdf", data)]).stack_id)
    d = ctx.db.q1("SELECT * FROM document")
    assert svc.original(d["id"]) == data  # unverändert
    ctx.settings.set("documents.keep_originals", False)
    other = text_pdf([*BANK, "Seite 2"])
    svc.run(svc.accept([("b.pdf", other)]).stack_id)
    d2 = ctx.db.q1("SELECT * FROM document ORDER BY id DESC")
    assert d2["stored"] == 0 and svc.original(d2["id"]) is None
    assert not list((Path(config.data_dir) / "documents").rglob(f"{d2['sha256']}*"))


def test_second_stack_while_running_is_rejected_cleanly(config: Config) -> None:
    from app.documentimport import service as S

    ctx = _ctx(config)
    svc = document_service(ctx)
    assert S._LOCK.acquire(blocking=False)
    try:
        acc = svc.accept([("a.pdf", text_pdf(BANK))])
        assert "läuft bereits" in svc.start(acc.stack_id)["error"]
    finally:
        S._LOCK.release()
    assert ctx.db.q1("SELECT status FROM document_stack")["status"] == "cancelled"
    assert ctx.db.q1("SELECT status FROM document")["status"] == "failed"
    assert not list((Path(config.data_dir) / "tmp" / "documents").glob("*.in"))
    again = svc.accept([("a.pdf", text_pdf(BANK))])  # danach erneut möglich
    assert again.stack_id and svc.run(again.stack_id)["transactions"] == 1
