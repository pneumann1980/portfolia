# ruff: noqa: F811  (Fixture ``master`` aus test_bitpanda wird importiert und als Argument verwendet)
"""DKB-Depotauszug (Phase 5): Positionen, Rechenprobe, Depotentgelt, Referenzbestände und Vergleich mit dem Bestand.
Alle Daten sind erfunden."""

from __future__ import annotations

import json
import os
import time
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.documentimport import dkb_depot as P
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


def doc_of(lines: list[str]) -> DocumentResult:
    ls = [Line(text, 1, i + 1) for i, text in enumerate(lines)]
    return DocumentResult(sha256="a" * 64, file_type="pdf", pages=[Page(1, "text", ls)])


def parse(lines: list[str]) -> P.Depot:
    c = classify(lines)
    assert c is not None and c.kind == "depotauszug"
    return P.parse_depot(doc_of(lines), c)


def test_positions_total_and_fee_are_read_and_proven() -> None:
    d = parse(X.depotauszug_voll())
    assert d.ok, d.problems
    assert d.stichtag == date(2024, 6, 30) and d.count == 3 and d.total == D("4020.67")
    by = {p.isin: p for p in d.positions}
    assert by["US02079K3059"].qty == D(20) and by["US02079K3059"].value == D("3397.60")
    assert by["US03945R1023"].fx == D("1.0705") and by["US03945R1023"].ccy == "USD"  # Kennung trotz Randzahl „10  …“
    assert by["FR0013018041"].value is None and by["FR0013018041"].qty == D(50)  # ohne Bewertung: Menge zählt
    assert (d.gross, d.net, d.vat, d.valuta) == (D("0.50"), D("0.42"), D("0.08"), date(2024, 7, 30))
    assert d.period == (date(2024, 4, 1), date(2024, 6, 30))


def test_thousands_separator_in_quantity() -> None:
    lines = [ln.replace("Stück  20  BEISPIEL HOLDING INC.  169,88 EUR  3.397,60",
                        "Stück  20.000  BEISPIEL HOLDING INC.  0,051 EUR  1.020,00") for ln in X.depotauszug_voll()]
    d = parse([ln.replace("4.020,67", "1.643,07") for ln in lines])
    assert d.ok, d.problems
    assert d.positions[0].qty == D(20000)


@pytest.mark.parametrize(("kw", "needle"), [
    ({"total": "4.000,00"}, "Summe Kurswerte"),
    ({"count": 4}, "Posten"),
    ({"gross": "0,60"}, "Depotentgelt"),
    ({"vat": "0,20", "gross": "0,62"}, "USt"),
])
def test_failed_proof_makes_everything_unclear(kw: dict, needle: str) -> None:
    d = parse(X.depotauszug_voll(**kw))
    assert not d.ok and any(needle in p for p in d.problems), d.problems
    txs = analyze(doc_of(X.depotauszug_voll(**kw))).txs
    assert [t.kind for t in txs] == ["unknown"]  # keine Bestandsnachweise, keine Buchung


def test_invalid_isin_is_rejected() -> None:
    lines = [ln.replace("US02079K3059", "US02079K3050") for ln in X.depotauszug_voll()]
    assert not parse(lines).ok


def test_doctx_holdings_and_fee_rows() -> None:
    an = analyze(doc_of(X.depotauszug_voll()))
    assert (an.doc_type, an.provider) == ("depotauszug", "dkb")
    assert [t.kind for t in an.txs] == ["holdings", "holdings", "holdings", "fee"]
    h = an.txs[0]
    assert h.value("isin") == "US02079K3059" and h.dec("quantity") == D(20) and h.value("date") == "2024-06-30"
    assert h.value("ext_id") == "Depotauszug:2345:2024-06-30:US02079K3059"
    fee = an.txs[3]
    # Datum = Valuta der Belastung, nicht das Quartalsende
    assert fee.dec("gross") == D("0.50") and fee.value("date") == "2024-07-30"
    assert fee.value("ext_id") == "Depotentgelt:2345:2024-06-30" and "netto 0.42" in fee.value("ca_detail")
    assert to_recs(Enriched(h, "WKN:A14Y6F", [], None, []), sha="a" * 64, doc={}, account="DKB", n0=1) == []
    recs = to_recs(Enriched(fee, None, [], None, []), sha="a" * 64, doc={"filename": "x.pdf"}, account="DKB", n0=1)
    assert len(recs) == 1 and recs[0].kind != "review"
    row = recs[0].row
    assert (row["type"], row["tag"], row["from_asset"], row["from_qty"], row["value_eur"]) == (
        "withdrawal", "fee", "EUR", "0.5", "0.5")
    assert "related_asset" not in row  # Konto-Ebene


def test_statement_without_fee_text_has_no_fee_row() -> None:
    an = analyze(doc_of(X.depotauszug_voll(with_fee=False)))
    assert [t.kind for t in an.txs] == ["holdings"] * 3


# -- Lauf über das ZIP ----------------------------------------------------------------------------------------------

DKB = "DKB"
A = [{"asset_id": "WKN:A14Y6F", "name": "Beispiel Holding", "asset_class": "security", "wkn": "A14Y6F",
      "isin": "US02079K3059", "quote_source": "yahoo", "quote_id": "BHX", "category": "Aktien: Einzeltitel"},
     {"asset_id": "WKN:A3C3BQ", "name": "Beispiel Aviation", "asset_class": "security", "wkn": "A3C3BQ",
      "isin": "US03945R1023", "quote_source": "yahoo", "quote_id": "BAV", "category": "Aktien: Einzeltitel"},
     {"asset_id": "WKN:A2QA4J", "name": "Beispiel Tech", "asset_class": "security", "wkn": "A2QA4J",
      "isin": "US69608A1088", "quote_source": "yahoo", "quote_id": "BTX", "category": "Aktien: Einzeltitel"}]


def _start(config: Config, extra: list[dict] | None = None) -> TestClient:
    dst = config.import_dir / "imp.zip"
    base = [tx("K-0", "2024-01-05", "buy", to=(DKB, "WKN:A14Y6F", "20"), value="3000.00"),
            tx("K-1", "2024-02-05", "buy", to=(DKB, "WKN:A3C3BQ", "150"), value="400.00"),   # Auszug: 200 → Abweichung
            tx("K-2", "2024-03-05", "buy", to=(DKB, "WKN:A2QA4J", "100"), value="2000.00"),  # nicht im Auszug
            tx("K-3", "2024-08-05", "buy", to=(DKB, "WKN:A14Y6F", "5"), value="900.00"),     # nach dem Stichtag
            *(extra or [])]
    build_zip(dst, transactions=base, assets=[*ASSETS, *A], generated_at="2025-03-01T00:00:00Z",
              valuation_date="2025-02-28")
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


def run_zip(c, sid, entries: dict[str, bytes]) -> dict:
    return finish(c, sid, run_id(upload(c, sid, make_zip(entries))))


@pytest.fixture
def app(config: Config, master):
    c = _start(config)
    yield c, _sid(c)
    c.__exit__(None, None, None)


def test_run_creates_reference_balances_compares_with_bookings_and_proposes_the_fee(app) -> None:
    c, sid = app
    run = run_zip(c, sid, {"d.pdf": X.pdf_of(X.depotauszug_voll())})
    assert run["status"] == "ok", run["message"]
    (h,) = run["summary"]["holdings"]
    status = {p["isin"]: p["status"] for p in h["positions"]}
    # Holding 20 = 20 (K-3 liegt nach dem Stichtag), Aviation 200 ≠ 150, FR0013018041 hat kein Asset
    assert status == {"US02079K3059": "stimmt", "US03945R1023": "weicht ab", "FR0013018041": "unbekannt"}
    assert (h["ok"], h["diff"], h["unknown"], h["refs_new"]) == (1, 1, 1, 2)
    assert [x["asset"] for x in h["only_in_portfolia"]] == ["WKN:A2QA4J"]
    refs = rows(c, "SELECT account, asset_id, qty, as_of, source FROM reference_balance ORDER BY asset_id")
    base = {"account": DKB, "as_of": "2024-06-30", "source": "statement"}
    assert refs == [{**base, "asset_id": "WKN:A14Y6F", "qty": "20"}, {**base, "asset_id": "WKN:A3C3BQ", "qty": "200"}]
    # Depotentgelt: ein Vorschlag, sonst nichts gebucht
    assert len(run["summary"]["depot"]["new"]) == 1
    recs = [json.loads(r["rec_json"])["row"] for r in rows(c, "SELECT rec_json FROM csv_row")]
    assert [(r["tag"], r["from_qty"]) for r in recs] == [("fee", "0.5")]
    assert rows(c, "SELECT COUNT(*) AS n FROM journal_tx")[0]["n"] == 0
    page = c.get(f"/settings/datasources/{sid}/dkb/zip/{run['id']}").text
    assert "Depotauszüge" in page and "weichen ab" in page and "nur in Portfolia" in page
    # zweites Hochladen: keine doppelten Referenzbestände, keine weiteren Vorschläge
    run_zip(c, sid, {"d.pdf": X.pdf_of(X.depotauszug_voll())})
    assert len(rows(c, "SELECT id FROM reference_balance")) == 2 and len(rows(c, "SELECT id FROM csv_row")) == 1


def test_failed_proof_writes_no_reference_balances(app) -> None:
    c, sid = app
    run = run_zip(c, sid, {"d.pdf": X.pdf_of(X.depotauszug_voll(total="4.000,00"))})
    assert not run["summary"]["holdings"]
    assert rows(c, "SELECT COUNT(*) AS n FROM reference_balance")[0]["n"] == 0
    assert len(run["summary"]["depot"]["unclear"]) == 1


def test_existing_fee_is_linked_not_duplicated(config: Config, master) -> None:
    fee = tx("F-1", "2024-07-30", "withdrawal", tag="fee", frm=(DKB, "EUR", "0.50"), value="0.50")
    c = _start(config, [fee])
    try:
        sid = _sid(c)
        run = run_zip(c, sid, {"d.pdf": X.pdf_of(X.depotauszug_voll())})
        dp = run["summary"]["depot"]
        assert [m["tx"] for m in dp["matched"]] == ["F-1"] and not dp["new"], dp
        assert rows(c, "SELECT tx_id, role FROM tx_document") == [{"tx_id": "F-1", "role": "depotauszug"}]
        assert not rows(c, "SELECT id FROM csv_row")
    finally:
        c.__exit__(None, None, None)
