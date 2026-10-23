# ruff: noqa: F811  (Fixture ``master`` aus test_bitpanda wird importiert und als Argument verwendet)
"""DKB-Kosteninformationen und Order-Belege (Phase 6): Parser, Zuordnung Z0–Z6, geschätzte Gebühr nur mit Bestätigung
und ohne Vorrang vor der Abrechnung, Order-Belege je Auftragsnummer und Jahres-Download. Alle Daten sind erfunden."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import time
import zipfile
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.diagnosis import actions as A
from app.documentimport import dkb_costinfo as K
from app.documentimport.dkb import classify
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
DKB = "DKB"


def doc_of(lines: list[str]) -> DocumentResult:
    ls = [Line(text, 1, i + 1) for i, text in enumerate(lines)]
    return DocumentResult(sha256="a" * 64, file_type="pdf", pages=[Page(1, "text", ls)])


def parse(lines: list[str]) -> K.CostInfo:
    c = classify(lines)
    assert c is not None and c.kind == "kosteninfo_order"
    return K.parse_costinfo(doc_of(lines), c)


# -- Parser ---------------------------------------------------------------------------------------------------------

def test_new_layout_buy_uses_entry_column_without_product_costs() -> None:
    k = parse(X.kosteninfo_voll("Kauf"))
    assert not k.problems, k.problems
    assert (k.layout, k.side, k.qty, k.price, k.amount) == ("neu", "buy", D(10), D("32.82"), D("328.20"))
    assert str(k.day) == "2024-08-05" and str(k.at) == "15:02:25"
    assert k.fee_in == D("38.20") and k.fee_out == D("10.00")  # 36,81 + 1,39 Fremdwährung; Produktkosten zählen nie
    assert k.checks and "328.20" in k.checks[0]


def test_new_layout_sell_has_only_the_exit_column() -> None:
    k = parse(X.kosteninfo_voll("Verkauf", qty="1.000,00", price="1,33", amount="1.330,00", dl_out="13,15"))
    assert not k.problems and k.side == "sell" and k.qty == D(1000)
    assert k.fee_out == D("13.15") and k.fee_in is None  # Gesamtkosten-Tabelle (25,00) wird nicht gelesen


def test_savings_plan_setup_is_information() -> None:
    k = parse(X.kosteninfo_voll("Kauf", plan=True, amount="3.000,00"))
    assert k.plan and not k.problems
    t = analyze(doc_of(X.kosteninfo_voll("Kauf", plan=True, amount="3.000,00"))).txs[0]
    assert t.kind == "info" and "Sparplan" in (t.value("info") or "")


def test_legacy_layout_has_no_direction_and_reads_entry_and_exit_blocks() -> None:
    k = parse(X.kosteninfo_legacy_voll(dl_in="10,00", dl_out="12,00"))
    assert not k.problems, k.problems
    assert k.layout == "legacy" and k.side == "" and k.qty == D(100) and k.amount == D("2054.50")
    assert (k.fee_in, k.fee_out) == (D("10.00"), D("12.00"))  # die Gesamtzeile „20,00“ vor den Blöcken zählt nicht


def test_calculation_proof_rejects_inconsistent_price() -> None:
    k = parse(X.kosteninfo_voll("Kauf", amount="400,00"))
    assert k.problems and "Anlagebetrag" in k.problems[0]
    assert analyze(doc_of(X.kosteninfo_voll("Kauf", amount="400,00"))).txs[0].kind == "unknown"
    k2 = parse(X.kosteninfo_legacy_voll(qty="100,00", amount="3.000,00", price="20,00"))
    assert k2.problems and "Anlagebetrag" in k2.problems[0]  # 100 × 20 = 2.000 ≠ 3.000 → außerhalb 1 %


def test_estimate_fields_carry_estimated_status_and_never_become_bookings() -> None:
    t = analyze(doc_of(X.kosteninfo_voll("Kauf"))).txs[0]
    assert t.kind == "estimate" and t.status("fee_buy") == "geschaetzt" and t.status("value_eur") == "geschaetzt"
    assert t.dec("fee_buy") == D("38.20") and t.value("tz") == "Europe/Berlin" and t.value("side") == "buy"
    from app.documentimport.bridge import to_recs
    from app.documentimport.enrich import Enriched

    assert to_recs(Enriched(t, "WKN:A40EDP", [], None, []), sha="a" * 64, doc={}, account=DKB, n0=1) == []


def test_order_documents_are_information_with_order_number() -> None:
    a = analyze(doc_of(X.ausfuehrungsanzeige())).txs[0]
    assert a.kind == "info" and a.value("ext_id") == "Order:1234567800:ausfuehrungsanzeige"
    assert a.dec("price") == D("32.82") and "ausgeführt" in (a.value("info") or "")
    s = analyze(doc_of(X.streichung())).txs[0]
    assert s.kind == "info" and "gestrichen" in (s.value("info") or "") and s.value("order_no") == "5270627500"
    assert "vorgemerkt" in (analyze(doc_of(X.auftragsbestaetigung())).txs[0].value("info") or "")


# -- Lauf über das ZIP ----------------------------------------------------------------------------------------------

TEMPUS = {"asset_id": "WKN:A40EDP", "name": "Beispiel AI", "asset_class": "security", "wkn": "A40EDP",
          "isin": "US88023B1035", "quote_source": "yahoo", "quote_id": "BAI", "category": "Aktien: Einzeltitel"}
PLTR = {"asset_id": "WKN:A2QA4J", "name": "Beispiel Tech", "asset_class": "security", "wkn": "A2QA4J",
        "isin": "US69608A1088", "quote_source": "yahoo", "quote_id": "BTX", "category": "Aktien: Einzeltitel"}


def _start(config: Config, extra: list[dict] | None = None) -> TestClient:
    dst = config.import_dir / "imp.zip"
    base = [tx("K-1", "2024-08-05", "buy", to=(DKB, "WKN:A40EDP", "10"), value="328.20"),
            tx("K-2", "2024-08-20", "sell", frm=(DKB, "WKN:A40EDP", "4"), value="150.00"),
            tx("K-3", "2021-11-17", "buy", to=(DKB, "WKN:A2QA4J", "100"), value="2064.50"),
            *(extra or [])]
    build_zip(dst, transactions=base, assets=[*ASSETS, TEMPUS, PLTR], generated_at="2025-03-01T00:00:00Z",
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


def txs(c) -> dict:
    return {t.tx_id: t for t in ctx(c).portfolio().txs}


@pytest.fixture
def app(config: Config, master):
    c = _start(config)
    yield c, _sid(c)
    c.__exit__(None, None, None)


def pdf(lines: list[str]) -> bytes:
    return X.pdf_of(lines)


def test_matching_states_for_every_case(app) -> None:
    c, sid = app
    docs = {
        "ok.pdf": pdf(X.kosteninfo_voll("Kauf")),  # K-1: Menge 10, am selben Tag
        "late.pdf": pdf(X.kosteninfo_voll("Verkauf", day="19.08.2024", at="22:10:00", qty="4,00", price="37,50",
                                          amount="150,00", dl_out="13,15")),                    # K-2 am nächsten Tag
        "qty.pdf": pdf(X.kosteninfo_voll("Kauf", day="05.08.2024", at="15:05:00", qty="12,00", price="27,35",
                                         amount="328,20")),                                     # Menge 12 ≠ 10
        "dir.pdf": pdf(X.kosteninfo_voll("Verkauf", day="05.08.2024", at="15:06:00", qty="10,00", price="32,82",
                                         amount="328,20")),  # Beleg Verkauf, Buchung Kauf
        "none.pdf": pdf(X.kosteninfo_voll("Kauf", day="01.03.2024", at="09:00:00")),            # keine Buchung
        "plan.pdf": pdf(X.kosteninfo_voll("Kauf", day="11.02.2025", at="14:47:22", plan=True, amount="3.000,00")),
        "bad.pdf": pdf(X.kosteninfo_voll("Kauf", day="07.08.2024", at="10:00:00", amount="999,00")),
        "legacy.pdf": pdf(X.kosteninfo_legacy_voll()),                                           # K-3 ohne Richtung
    }
    run = run_zip(c, sid, docs)
    assert run["status"] == "ok", run["message"]
    ci = run["summary"]["costs"]
    assert sorted(m["tx"] for m in ci["matched"]) == ["K-1", "K-2", "K-3"], ci
    assert len(ci["qty"]) == 1 and "Menge weicht ab" in ci["qty"][0]["note"]
    assert len(ci["dir"]) == 1 and "Richtungskonflikt" in ci["dir"][0]["note"]
    assert len(ci["none"]) == 1 and len(ci["info"]) == 1 and len(ci["unclear"]) == 1
    # nie eine Buchung, nie ein Vorschlag im Prüf-Stapel, Bestand unverändert
    assert rows(c, "SELECT COUNT(*) AS n FROM journal_tx")[0]["n"] == 0
    kinds = {json.loads(r["rec_json"])["kind"] for r in rows(c, "SELECT rec_json FROM csv_row")}
    assert kinds <= {"review"}  # höchstens der ungeklärte Beleg als Prüfhinweis
    links = {(r["tx_id"], r["role"]) for r in rows(c, "SELECT tx_id, role FROM tx_document")}
    assert links == {("K-1", "kosteninformation"), ("K-2", "kosteninformation"), ("K-3", "kosteninformation")}
    page = c.get(f"/settings/datasources/{sid}/dkb/zip/{run['id']}").text
    assert "Kosteninformationen (Order-Spur)" in page and "Richtungskonflikt" in page


def test_partial_executions_are_summed_and_ambiguity_is_not_guessed(config: Config, master) -> None:
    extra = [tx("P-1", "2024-09-02", "buy", to=(DKB, "WKN:A40EDP", "6"), value="100"),
             tx("P-2", "2024-09-02", "buy", to=(DKB, "WKN:A40EDP", "4"), value="70"),
             tx("A-1", "2024-10-01", "buy", to=(DKB, "WKN:A40EDP", "5"), value="100"),
             tx("A-2", "2024-10-01", "buy", to=(DKB, "WKN:A40EDP", "5"), value="100")]
    c = _start(config, extra)
    try:
        sid = _sid(c)
        run = run_zip(c, sid, {
            "part.pdf": pdf(X.kosteninfo_voll("Kauf", day="02.09.2024", at="10:00:00", amount="328,20")),
            "amb.pdf": pdf(X.kosteninfo_voll("Kauf", day="01.10.2024", at="10:00:00", qty="5,00", price="65,64",
                                             amount="328,20"))})
        ci = run["summary"]["costs"]
        assert [m["tx"] for m in ci["matched"]] == ["P-1"] and len(ci["ambiguous"]) == 1
        linked = {r["tx_id"] for r in rows(c, "SELECT tx_id FROM tx_document")}
        assert linked == {"P-1", "P-2"}  # alle Teilausführungen bekommen den Beleg
    finally:
        c.__exit__(None, None, None)


def test_fee_estimate_is_proposed_only_with_confirmation_and_marked_as_estimated(app) -> None:
    c, sid = app
    run = run_zip(c, sid, {"ok.pdf": pdf(X.kosteninfo_voll("Kauf"))})
    base = f"/settings/datasources/{sid}/dkb/zip/{run['id']}/ergaenzen"
    page = c.get(base + "?quelle=kosten")
    assert page.status_code == 200 and "keine Abrechnung" in page.text and "38.20" in page.text.replace(",", ".")
    assert "Gebühr (geschätzt)" in page.text
    doc = int(re.search(r'name="d" value="(\d+)"', page.text).group(1))
    prev = c.get(base + f"?quelle=kosten&v=1&d={doc}")
    token = re.search(r'name="token" value="([0-9a-f]+)"', prev.text).group(1)
    assert txs(c)["K-1"].fee_qty is None  # bis zur Bestätigung ändert sich nichts
    form = {"csrf_token": c.token, "token": token, "d": [doc], "quelle": "kosten"}
    r = c.post(base, data=form, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    t = txs(c)["K-1"]
    assert (t.fee_qty, t.fee_asset, t.value_eur, t.to_qty) == (
        D("38.20"), "EUR", D("328.20"), D(10))  # Wert/Menge unberührt
    assert "Gebühr aus Kosteninformation vom 05.08.2024, geschätzt" in (t.note or "")
    dec = rows(c, "SELECT id FROM diag_decision ORDER BY id DESC LIMIT 1")[0]
    assert A.undo(ctx(c), dec["id"]).ok and txs(c)["K-1"].fee_qty is None  # exakt rückgängig


def test_a_settlement_takes_precedence_over_the_cost_estimate(app) -> None:
    c, sid = app
    settle = X.abrechnung("Kauf", order="123456/78.00", day="06.08.2024", qty="10", price="32,82 USD", fx="1,0887",
                          kurswert="301,46", close="05.08.2024 15:30:00")
    run = run_zip(c, sid, {"abr.pdf": pdf(settle), "ki.pdf": pdf(X.kosteninfo_voll("Kauf"))})
    assert [m["tx"] for m in run["summary"]["costs"]["matched"]] == ["K-1"]
    assert [m["tx"] for m in run["summary"]["trades"]["matched"]] == ["K-1"]
    from app.documentimport import amend as AM

    doc = run["summary"]["costs"]["matched"][0]["doc"]
    p = AM.propose(ctx(c), doc, 1)
    assert p.blocked and "Abrechnung" in p.blocked and not p.changes  # Z6/R1: nur als Beleg verknüpft
    page = c.get(f"/settings/datasources/{sid}/dkb/zip/{run['id']}/ergaenzen?quelle=kosten").text
    assert "Keine Ergänzungen nötig" in page


def test_order_documents_follow_the_settlement_and_report_state(app) -> None:
    c, sid = app
    settle = X.abrechnung("Kauf", order="123456/78.00", day="06.08.2024", qty="10", price="32,82 USD", fx="1,0887",
                          kurswert="301,46", close="05.08.2024 15:30:00")
    run = run_zip(c, sid, {"abr.pdf": pdf(settle), "exec.pdf": pdf(X.ausfuehrungsanzeige()),
                           "conf.pdf": pdf(X.auftragsbestaetigung()), "cancel.pdf": pdf(X.streichung())})
    states = {o["kind"]: o for o in run["summary"]["orders"]}
    ausf = states["Ausführungsanzeige"]
    assert "Abrechnung vorhanden" in ausf["state"] and ausf["linked"] == 1
    assert "keine Ausführung belegt" in states["Auftragsbestätigung"]["state"]
    assert "gestrichen – keine Ausführung" in states["Streichungsbestätigung"]["state"]
    roles = {(r["tx_id"], r["role"]) for r in rows(c, "SELECT tx_id, role FROM tx_document")}
    assert roles == {("K-1", "abrechnung"), ("K-1", "ausfuehrung")}


def test_order_documents_get_linked_when_the_settlement_is_booked_later(app) -> None:
    """Abrechnung noch Vorschlag → Order-Belege hängen nach dem Buchen an derselben Buchung."""
    c, sid = app
    settle = X.abrechnung("Kauf", order="123456/78.00", day="21.01.2025", qty="7", price="10,00 EUR", fx=None,
                          kurswert="70,00", close="20.01.2025 10:00:00")
    run = run_zip(c, sid, {"abr.pdf": pdf(settle), "exec.pdf": pdf(X.ausfuehrungsanzeige())})
    assert run["summary"]["trades"]["new"] and not run["summary"]["trades"]["matched"]
    assert not rows(c, "SELECT * FROM tx_document")  # nichts verknüpft, solange nichts gebucht ist
    from app.documentimport import txdocs

    doc = run["summary"]["trades"]["new"][0]["doc"]
    ctx(c).db.x("INSERT INTO tx_document(tx_id, document_id, role, source, ref, created_at) VALUES "
                "('T-9', ?, 'abrechnung', 'auto', NULL, '2025-01-21T00:00:00Z')", (doc,))
    assert txdocs.backfill(ctx(c).db) == 1
    assert {(r["tx_id"], r["role"]) for r in rows(c, "SELECT tx_id, role FROM tx_document")} == {
        ("T-9", "abrechnung"), ("T-9", "ausfuehrung")}


# -- Jahres-Download ------------------------------------------------------------------------------------------------

def _manifest(z: zipfile.ZipFile) -> list[dict]:
    name = next(n for n in z.namelist() if n.endswith("manifest.csv"))
    text = z.read(name).decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text), delimiter=";"))


def test_year_download_contains_linked_documents_and_a_manifest(app) -> None:
    c, sid = app
    settle = X.abrechnung("Kauf", order="123456/78.00", day="06.08.2024", qty="10", price="32,82 USD", fx="1,0887",
                          kurswert="301,46", close="05.08.2024 15:30:00")
    run_zip(c, sid, {"abr.pdf": pdf(settle), "exec.pdf": pdf(X.ausfuehrungsanzeige()),
                     "ki.pdf": pdf(X.kosteninfo_voll("Kauf")),
                     "depot.pdf": pdf(X.depotauszug_voll(stichtag="30.06.2024", with_fee=False))})
    r = c.get("/journal/documents/export?year=2024")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"] and r.headers["cache-control"] == "private, no-store"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    assert any(n.startswith("belege-2024/01_Abrechnungen/2024-08-06_Kauf_US88023B1035_1234567800") for n in names)
    assert any("/05_Orderbelege/" in n for n in names)
    assert any(n.startswith("belege-2024/ohne_buchung/") and "Depotauszug" in n for n in names)
    assert all(".." not in n and not n.startswith("/") for n in names)
    m = _manifest(z)
    assert {r_["status"] for r_ in m} == {"ok"}
    k1 = [r_ for r_ in m if r_["buchungs_id"] == "K-1"]
    assert {r_["beleg_rolle"] for r_ in k1} == {"Abrechnung", "Ausführungsanzeige", "Kosteninformation"}
    assert k1[0]["kurswert_eur"] == "328,20" and k1[0]["isin"] == "US88023B1035"  # Dezimalkomma
    assert all(len(r_["sha256"]) == 64 for r_ in m)
    # fehlendes Original: Manifest meldet es, der Download bricht nicht ab
    ctx(c).db.x("UPDATE document SET stored=0 WHERE id=(SELECT id FROM document ORDER BY id LIMIT 1)")
    z2 = zipfile.ZipFile(io.BytesIO(c.get("/journal/documents/export?year=2024").content))
    assert "original_fehlt" in {r_["status"] for r_ in _manifest(z2)}


def test_year_download_without_documents_explains(app) -> None:
    c, _ = app
    r = c.get("/journal/documents/export?year=2019", follow_redirects=False)
    assert r.status_code in (302, 303) and "keine" in r.headers["location"].lower()
    assert c.get("/journal/documents/export?year=abc").status_code in (400, 422)
    page = c.get("/journal?year=2024").text
    assert "Belege 2024 herunterladen" in page
