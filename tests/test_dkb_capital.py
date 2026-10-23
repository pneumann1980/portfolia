# ruff: noqa: F811  (Fixture ``master`` aus test_bitpanda wird importiert und als Argument verwendet)
"""DKB-Kapitalmaßnahmen und Ausbuchungen (Phase 4): Erkennung, Rechenprobe, Abbildung, Zuordnung, Storno, Lauf.
Alle Daten sind erfunden (anonymisierte Nachbauten der Beleg-Layouts)."""

from __future__ import annotations

import json
import os
import time
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.documentimport import dkb_capital as K
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


def parse(lines: list[str]) -> K.Capital:
    c = classify(lines)
    assert c is not None
    return K.parse_capital(doc_of(lines), c)


def test_reverse_split_stage_one_is_information_only() -> None:
    c = parse(X.ca_reverse_split(stage=1, beleg="01432975", valuta="17.02.2025"))
    assert c.kind == "info" and "Sperrumbuchung" in c.info


def test_reverse_split_stage_two_is_proven_and_dated_by_valuta() -> None:
    c = parse(X.ca_reverse_split())
    assert c.kind == "corporate_action" and not c.problems, c.problems
    assert (c.out[0].qty, c.inn[0].qty, c.inn[0].isin) == (D(100), D(5), "US67421J2078")
    assert c.valuta == date(2025, 2, 18) and c.ratio == (D(20), D(1))  # Valuta/Ex-Tag, nicht das Belegdatum


def test_reverse_split_fraction_and_wrong_ratio() -> None:
    ok = parse(X.ca_reverse_split(out_qty="86", in_qty="4,3000", new_isin="US13803R3003", new_wkn="A40XUX"))
    assert ok.kind == "corporate_action" and ok.checks and not ok.problems
    # 87 / 20 = 4,35 → 4 eingebucht, Bruchteil 0,35 separat reguliert (Hinweis, kein Fehler)
    frac = parse(X.ca_reverse_split(out_qty="87", in_qty="4"))
    assert frac.kind == "corporate_action" and any("Bruchteil" in h for h in frac.hints)
    bad = parse(X.ca_reverse_split(in_qty="7"))
    assert bad.kind == "unknown" and any("Rechenprobe" in p for p in bad.problems)


def test_bonus_issue_without_tax_page_is_booked_without_value_but_flagged() -> None:
    c = parse(X.ca_gratis(beleg="01452937", title="Gratisausgabe", tax=False))
    assert c.kind == "corporate_action" and any("Bewertungskurs" in h for h in c.hints)


def test_rebooking_after_storno_has_valuation_and_tax_proven() -> None:
    c = parse(X.ca_gratis())
    assert c.kind == "corporate_action" and not c.problems, c.problems
    assert (c.price, c.unbar, c.tax_burden, c.total) == (D("2.477"), D("4.95"), D("1.30"), D("-1.30"))
    assert c.valuta == date(2025, 10, 10)
    bad = parse(X.ca_gratis(total="1,99"))
    assert bad.kind == "unknown" and any("Summe der Steuern" in p for p in bad.problems)


def test_storno_is_information_and_names_the_cancelled_receipt() -> None:
    c = parse(X.ca_storno())
    assert c.kind == "info" and c.storno_of == "01452937"
    cl = classify(X.ca_storno())
    assert cl is not None and cl.sub == "storno" and cl.ref == "01452937"


def test_fee_proof_uses_quantity_rate_and_fx() -> None:
    c = parse(X.ca_entgelt())
    assert c.kind == "fee" and not c.problems, c.problems
    assert c.valuta == date(2025, 2, 25) and c.ref == "01432975"
    assert parse(X.ca_entgelt(total="2,50")).kind == "unknown"


def test_worthless_and_replacement_free_write_offs_check_loss_against_purchases() -> None:
    w = parse(X.ca_wertlos())
    assert w.kind == "writeoff" and w.loss == D("121.55") and w.valuta == date(2025, 10, 30)
    assert parse(X.ca_wertlos(cost="100,00")).kind == "unknown"  # Verlust ≠ Anschaffungskosten
    e = parse(X.ersatzlose_voll())
    assert e.kind == "writeoff" and e.valuta == date(2025, 1, 7) and e.loss == D("238.95")
    assert parse(X.ersatzlose_voll(cost="200,00")).kind == "unknown"


def test_name_change_and_announcements_are_information() -> None:
    n = parse(X.ca_umbenennung())
    assert n.kind == "info" and "ALT BATTERY INC. → NEU ENERGY INC." in n.info
    ch = classify(X.ca_info())
    assert ch is not None and ch.sub == "insolvenz"  # „Liquidation“ im Fließtext darf den Titel nicht überstimmen
    assert parse(X.ca_info()).kind == "info"
    assert parse(X.ca_info("Ablauf der Optionsfrist")).kind == "info"


def test_unknown_kinds_of_actions_are_unclear_not_guessed() -> None:
    c = parse(X.ca_info("Umtausch / Fusion"))
    assert c.kind == "unknown" and any("nicht abgebildet" in p for p in c.problems)


def test_doctx_and_bridge_rows() -> None:
    def recs_of(lines: list[str], asset: str = "WKN:A3CQRG", asset2: str | None = None):
        t = analyze(doc_of(lines)).txs[0]
        return t, to_recs(Enriched(t, asset, [], None, [], None, asset2), sha="a" * 64, doc={"filename": "x.pdf"},
                          account="DKB", n0=1)

    t, r = recs_of(X.ca_reverse_split(), asset2="WKN:A41264")
    assert t.kind == "corporate_action" and len(r) == 1
    assert {k: r[0].row[k] for k in ("type", "tag", "from_asset", "from_qty", "to_asset", "to_qty")} == {
        "type": "corporate_action", "tag": "reverse_split", "from_asset": "WKN:A3CQRG", "from_qty": "100",
        "to_asset": "WKN:A41264", "to_qty": "5"}
    t, r = recs_of(X.ca_gratis(), asset="WKN:A4AQAG")
    assert [(x.row["type"], x.row.get("tag")) for x in r] == [("corporate_action", "spinoff"), ("withdrawal", "tax")]
    assert r[0].row["to_qty"] == "2" and r[0].row["value_eur"] == "4.95" and r[1].row["from_qty"] == "1.3"
    t, r = recs_of(X.ca_entgelt())
    assert r[0].row["tag"] == "fee" and r[0].row["from_asset"] == "EUR" and r[0].row["from_qty"] == "1.91"
    assert r[0].row["related_asset"] == "WKN:A3CQRG"
    t, r = recs_of(X.ca_wertlos(), asset="WKN:A2QBSN")
    assert (r[0].row["tag"], r[0].row["from_qty"], r[0].row["value_eur"]) == ("lost", "100", "0")
    assert "Verlust laut Beleg 121.55" in r[0].row["note"]
    t, r = recs_of(X.ca_storno())
    assert t.kind == "info" and r == []  # Information erzeugt nie eine Buchungszeile
    t, r = recs_of(X.ca_wertlos(cost="1,00"))
    assert t.kind == "unknown" and r[0].kind == "review"


# -- Lauf über das ZIP ----------------------------------------------------------------------------------------------

DKB = "DKB"
OATLY = {"asset_id": "WKN:A3CQRG", "name": "Beispiel Group", "asset_class": "security", "wkn": "A3CQRG",
         "isin": "US67421J1088", "quote_source": "yahoo", "quote_id": "BGX", "category": "Aktien: Einzeltitel"}
OATLY2 = {"asset_id": "WKN:A41264", "name": "Beispiel Group neu", "asset_class": "security", "wkn": "A41264",
          "isin": "US67421J2078", "quote_source": "yahoo", "quote_id": "BGY", "category": "Aktien: Einzeltitel"}
WARRANT = {"asset_id": "WKN:A4AQAG", "name": "Beispiel Optionsschein", "asset_class": "security", "wkn": "A4AQAG",
           "isin": "US36467W1172", "quote_source": "yahoo", "quote_id": "BWT", "category": "Derivate"}
ASA = {"asset_id": "WKN:A2QBSN", "name": "Beispiel ASA", "asset_class": "security", "wkn": "A2QBSN",
       "isin": "NO0010890304", "quote_source": "yahoo", "quote_id": "BAS", "category": "Aktien: Einzeltitel"}


def _start(config: Config, extra: list[dict]) -> TestClient:
    dst = config.import_dir / "imp.zip"
    base = [tx("K-0", "2024-01-05", "buy", to=(DKB, "WKN:A3CQRG", "100"), value="1000.00"),
            tx("K-1", "2024-02-05", "buy", to=(DKB, "WKN:A2QBSN", "100"), value="121.55"), *extra]
    build_zip(dst, transactions=base, assets=[*ASSETS, OATLY, OATLY2, WARRANT, ASA],
              generated_at="2025-03-01T00:00:00Z", valuation_date="2025-02-28")
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


def all_docs() -> dict[str, bytes]:
    return {
        "stufe1.pdf": X.pdf_of(X.ca_reverse_split(stage=1, beleg="01432975", valuta="17.02.2025")),
        "stufe2.pdf": X.pdf_of(X.ca_reverse_split()),
        "entgelt.pdf": X.pdf_of(X.ca_entgelt()),
        "einbuchung_a.pdf": X.pdf_of(X.ca_gratis(beleg="01452937", title="Gratisausgabe", tax=False)),
        "storno.pdf": X.pdf_of(X.ca_storno()),
        "neubuchung.pdf": X.pdf_of(X.ca_gratis()),
        "wertlos.pdf": X.pdf_of(X.ca_wertlos()),
        "ersatzlos.pdf": X.pdf_of(X.ersatzlose_voll()),
        "name.pdf": X.pdf_of(X.ca_umbenennung()),
        "chapter.pdf": X.pdf_of(X.ca_info()),
        "umtausch.pdf": X.pdf_of(X.ca_info("Umtausch / Fusion", beleg="51805816")),
    }


@pytest.fixture
def app(config: Config, master):
    c = _start(config, [])
    yield c, _sid(c)
    c.__exit__(None, None, None)


def test_run_books_nothing_and_sorts_documents_into_booking_information_and_unclear(app) -> None:
    c, sid = app
    run = run_zip(c, sid, all_docs())
    assert run["status"] == "ok", run["message"]
    cap = run["summary"]["capital"]
    # Vorschläge: Stufe 2, Entgelt, Neubuchung, wertlos, ersatzlos – Einbuchung A ist storniert (nicht ausgewertet)
    assert len(cap["new"]) == 5 and not cap["matched"], cap
    # Stufe 1, Storno, Einbuchung A (aufgehoben), Namensänderung, Chapter VII
    assert len(cap["info"]) == 5, [(x["date"], x["reason"][:40]) for x in cap["info"]]
    assert len(cap["unclear"]) == 1 and "nicht abgebildet" in cap["unclear"][0]["reason"]
    reasons = " | ".join(x["reason"] for x in cap["info"])
    assert "Sperrumbuchung" in reasons and "Storno" in reasons and "storniert durch Beleg" in reasons
    assert "ALT BATTERY INC. → NEU ENERGY INC." in reasons
    recs = [json.loads(r["rec_json"]) for r in rows(c, "SELECT r.rec_json FROM csv_row r")]
    tags = sorted((r["row"]["type"], r["row"].get("tag")) for r in recs if r.get("row"))
    assert tags == [("corporate_action", "reverse_split"), ("corporate_action", "spinoff"), ("withdrawal", "fee"),
                    ("withdrawal", "lost"), ("withdrawal", "lost"), ("withdrawal", "tax")]
    assert rows(c, "SELECT COUNT(*) AS n FROM journal_tx")[0]["n"] == 0
    # genau ein Zugang (Neubuchung), nicht zwei
    assert sum(1 for r in recs if r.get("row", {}).get("tag") == "spinoff") == 1
    page = c.get(f"/settings/datasources/{sid}/dkb/zip/{run['id']}").text
    assert "Kapitalmaßnahmen und Ausbuchungen" in page and "Nur Information" in page
    # zweites Hochladen: nichts doppelt
    n = len(recs)
    run_zip(c, sid, all_docs())
    assert len(rows(c, "SELECT r.rec_json FROM csv_row r")) == n


def test_storno_after_the_rebooking_was_already_evaluated_leaves_a_note(app) -> None:
    c, sid = app
    run_zip(c, sid, {"a.pdf": X.pdf_of(X.ca_gratis(beleg="01452937", title="Gratisausgabe", tax=False))})
    run = run_zip(c, sid, {"s.pdf": X.pdf_of(X.ca_storno())})
    assert any("storniert, war aber schon ausgewertet" in n for n in run["summary"]["notes"])
    assert rows(c, "SELECT COUNT(*) AS n FROM journal_tx")[0]["n"] == 0  # nie still zurückgenommen


def test_existing_entries_are_linked_and_deviations_are_shown(config: Config, master) -> None:
    existing = [
        tx("S-1", "2025-02-18", "corporate_action", tag="reverse_split", frm=(DKB, "WKN:A3CQRG", "50"),
           to=(DKB, "WKN:A41264", "5")),
        tx("W-1", "2025-10-07", "corporate_action", tag="spinoff", to=(DKB, "WKN:A4AQAG", "2"), value="4.95"),
        tx("L-1", "2025-10-30", "withdrawal", tag="lost", frm=(DKB, "WKN:A2QBSN", "100"), value="0"),
    ]
    c = _start(config, existing)
    try:
        sid = _sid(c)
        docs = {k: v for k, v in all_docs().items() if k in ("stufe2.pdf", "neubuchung.pdf", "wertlos.pdf")}
        run = run_zip(c, sid, docs)
        cap = run["summary"]["capital"]
        assert sorted(m["tx"] for m in cap["matched"]) == ["L-1", "S-1", "W-1"] and not cap["new"], cap
        by = {m["tx"]: m for m in cap["matched"]}
        assert "weicht ab" in by["S-1"]["note"]  # Beleg 100 → 5, Buchung 50 → 5: gleiche Maßnahme, Menge abweichend
        assert not by["W-1"]["note"] and not by["L-1"]["note"]
        assert sorted(r["tx_id"] for r in rows(c, "SELECT tx_id FROM tx_document")) == ["L-1", "S-1", "W-1"]
        # Steuerzeile der Neubuchung bleibt als einziger Vorschlag; keine zweite Maßnahme
        tags = sorted(json.loads(r["rec_json"])["row"]["tag"] for r in rows(c, "SELECT rec_json FROM csv_row"))
        assert tags == ["tax"]
    finally:
        c.__exit__(None, None, None)


def test_different_documents_of_one_day_stay_separate_events(app) -> None:
    c, sid = app
    # Entgelt (Belegnummer …500) und Stufe 2 am selben Tag/Asset dürfen nicht verschmelzen
    run = run_zip(c, sid, {"a.pdf": X.pdf_of(X.ca_entgelt()),
                           "b.pdf": X.pdf_of(X.ca_reverse_split(valuta="25.02.2025"))})
    assert len(run["summary"]["capital"]["new"]) == 2
