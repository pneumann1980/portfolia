# ruff: noqa: F811  (Fixture ``master`` aus test_bitpanda wird importiert und als Argument verwendet)
"""DKB-Abrechnungen → vorhandene Buchungen (Phase 2): Zuordnung, Ergänzen (Sammelaktion, Warnung statt Sperre),
Rückgängig, Beleg-Verknüpfung (``tx_document``) und Anzeige. Alle Daten sind erfunden."""

from __future__ import annotations

import os
import re
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.datasources import dkb as D
from app.diagnosis import actions as A
from app.documentimport import amend as AM
from app.documentimport import txdocs
from app.importer.zipbuilder import build_zip
from app.jobs import tasks
from app.journal.service import journal_service
from app.main import build_app
from tests import dkbdocs as X
from tests.helpers import ASSETS, tx
from tests.test_bitpanda import master, post  # noqa: F401
from tests.test_dkb import ctx, rows, wait
from tests.test_dkb_zip import finish, make_zip, run_id, upload

DKB = "DKB"
TEMPUS = {"asset_id": "WKN:A40EDP", "name": "Beispiel AI", "asset_class": "security", "wkn": "A40EDP",
          "isin": "US88023B1035", "quote_source": "yahoo", "quote_id": "BAI", "category": "Aktien: Einzeltitel"}
PLTR = {"asset_id": "WKN:A2QA4J", "name": "Beispiel Tech", "asset_class": "security", "wkn": "A2QA4J",
        "isin": "US69608A1088", "quote_source": "yahoo", "quote_id": "BTX", "category": "Aktien: Einzeltitel"}
TAX = (("Kapitalertragsteuer", "25,00", "8.765,71", "2.191,43"), ("Solidaritätszuschlag", "5,50", "2.191,43", "120,52"))
D1 = Decimal


def curated() -> list[dict]:
    """Einträge wie im kuratierten Import: einseitig (ohne EUR-Bein), Wert ohne Gebührentrennung, Datum ohne Uhrzeit."""
    return [
        tx("K-0", "2021-11-17", "buy", to=(DKB, "WKN:A2QA4J", "100"), value="2064.50"),
        tx("K-1", "2024-08-05", "buy", to=(DKB, "WKN:A40EDP", "10"), value="337.00"),
        tx("K-2", "2025-02-17", "sell", frm=(DKB, "WKN:A2QA4J", "100"), value="11310"),
    ]


def buy_pdf() -> bytes:
    return X.pdf_of(X.abrechnung("Kauf", order="286297/67.00"))


def sell_pdf() -> bytes:
    return X.pdf_of(X.abrechnung("Verkauf", order="518604/81.00", day="17.02.2025", wkn="A2QA4J",
                                 isin="US69608A1088", name="BEISPIEL TECH INC.", qty="100", price="113,10 EUR",
                                 fx=None, kurswert="11.310,00", fees=(("Provision", "15,00"),), taxes=TAX,
                                 close="17.02.2025 14:40:26"))


@pytest.fixture
def app(config: Config, master):
    dst = config.import_dir / "imp.zip"
    build_zip(dst, transactions=curated(), assets=[*ASSETS, TEMPUS, PLTR], generated_at="2025-03-01T00:00:00Z",
              valuation_date="2025-02-28")
    old = time.time() - 3600
    os.utime(dst, (old, old))
    c = TestClient(build_app(config, start_scheduler=False))
    c.__enter__()
    c.get("/settings")
    c.token = c.cookies.get("portfolia_csrf")
    assert tasks.import_check(ctx(c), "test").status == "imported"
    r = post(c, "/settings/datasources", kind="exchange", provider="dkb", name=DKB, account=DKB, sync_interval_min="0")
    sid = int(r.headers["location"].split("/settings/datasources/")[1].split("?")[0].split("#")[0])
    yield c, sid
    c.__exit__(None, None, None)


def txs(c) -> dict:
    return {t.tx_id: t for t in ctx(c).portfolio().txs}


def run_zip(c, sid, entries: dict[str, bytes]) -> dict:
    return finish(c, sid, run_id(upload(c, sid, make_zip(entries))))


def test_abrechnungen_are_matched_to_existing_entries_without_new_bookings(app) -> None:
    c, sid = app
    run = run_zip(c, sid, {"a.pdf": buy_pdf(), "b.pdf": sell_pdf()})
    assert run["status"] == "ok", run["message"]
    tr = run["summary"]["trades"]
    assert sorted(m["tx"] for m in tr["matched"]) == ["K-1", "K-2"] and not tr["new"] and not tr["unclear"]
    # nur die Steuerzeile des Verkaufs ist ein Vorschlag im Prüf-Stapel – Käufe/Verkäufe nicht doppelt
    batch_rows = rows(c, "SELECT r.status, r.rec_json FROM csv_row r")
    assert len(batch_rows) == 1 and '"tag": "tax"' in batch_rows[0]["rec_json"].replace('"tag":"tax"', '"tag": "tax"')
    assert rows(c, "SELECT COUNT(*) AS n FROM journal_tx")[0]["n"] == 0
    # Beleg-Verknüpfung (automatisch, ohne Änderung der Buchung)
    links = rows(c, "SELECT tx_id, role, source FROM tx_document ORDER BY tx_id")
    assert links == [{"tx_id": "K-1", "role": "abrechnung", "source": "auto"},
                     {"tx_id": "K-2", "role": "abrechnung", "source": "auto"}]
    assert txs(c)["K-1"].fee_qty is None and txs(c)["K-1"].value_eur == D1("337.00")
    # Anzeige in der Buchungsliste
    page = c.get("/journal").text
    assert page.count('class="badge info" href="/journal/documents/') >= 2 and "Abrechnung" in page
    assert "/original" in page
    # zweites Hochladen: Dubletten, keine zweite Zuordnung
    run2 = run_zip(c, sid, {"a.pdf": buy_pdf(), "b.pdf": sell_pdf()})
    assert run2["summary"]["stored"] == 0 and len(rows(c, "SELECT * FROM tx_document")) == 2


def test_matching_is_strict_about_account_quantity_window_and_ambiguity(app) -> None:
    c, sid = app
    # andere Menge, anderes Datum (> 3 Tage), anderes Konto → keine Zuordnung, Vorschlag im Prüf-Stapel
    other_qty = X.pdf_of(X.abrechnung("Kauf", order="286297/68.00", qty="11", kurswert="331,61"))
    far = X.pdf_of(X.abrechnung("Kauf", order="286297/69.00", close="20.08.2024 15:30:00", day="21.08.2024"))
    run = run_zip(c, sid, {"q.pdf": other_qty, "d.pdf": far})
    tr = run["summary"]["trades"]
    assert not tr["matched"] and len(tr["new"]) == 2
    # Konto: Eintrag in anderem Konto zählt nicht (Z0)
    ctx(c).db.x("UPDATE data_source SET account='Anderes Depot'")
    run = run_zip(c, sid, {"a.pdf": buy_pdf()})
    tr = run["summary"]["trades"]
    assert not tr["matched"] and len(tr["new"]) == 1


def test_two_equal_candidates_are_not_guessed(config: Config, master) -> None:
    rows_ = [*curated(), tx("K-1b", "2024-08-05", "buy", to=(DKB, "WKN:A40EDP", "10"), value="337.00")]
    dst = config.import_dir / "imp.zip"
    build_zip(dst, transactions=rows_, assets=[*ASSETS, TEMPUS, PLTR], generated_at="2025-03-01T00:00:00Z",
              valuation_date="2025-02-28")
    old = time.time() - 3600
    os.utime(dst, (old, old))
    with TestClient(build_app(config, start_scheduler=False)) as c:
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        tasks.import_check(ctx(c), "test")
        r = post(c, "/settings/datasources", kind="exchange", provider="dkb", name=DKB, account=DKB,
                 sync_interval_min="0")
        sid = int(r.headers["location"].split("/settings/datasources/")[1].split("?")[0].split("#")[0])
        run = run_zip(c, sid, {"a.pdf": buy_pdf()})
        tr = run["summary"]["trades"]
        assert not tr["matched"] and len(tr["new"]) == 1  # zwei gleichwertige Kandidaten: keine automatische Zuordnung


def test_failed_proof_is_unclear_and_never_booked(app) -> None:
    c, sid = app
    bad = X.pdf_of(X.abrechnung("Kauf", order="286297/70.00", total="400,00"))
    run = run_zip(c, sid, {"x.pdf": bad})
    tr = run["summary"]["trades"]
    assert len(tr["unclear"]) == 1 and "Rechenprobe" in tr["unclear"][0]["reason"] and not tr["matched"]
    assert rows(c, "SELECT COUNT(*) AS n FROM journal_tx")[0]["n"] == 0
    page = c.get(f"/settings/datasources/{sid}/dkb/zip/{run['id']}").text
    assert "Ungeklärt" in page and "Rechenprobe" in page


def test_bulk_amend_preview_apply_and_undo(app) -> None:
    c, sid = app
    run = run_zip(c, sid, {"a.pdf": buy_pdf(), "b.pdf": sell_pdf()})
    base = f"/settings/datasources/{sid}/dkb/zip/{run['id']}/ergaenzen"
    page = c.get(base)
    assert page.status_code == 200 and "K-1" in page.text and "36.43" in page.text.replace(",", ".")
    docs = sorted({int(x) for x in re.findall(r'name="d" value="(\d+)"', page.text)})
    assert len(docs) == 2
    prev = c.get(base + "?v=1&" + "&".join(f"d={d}" for d in docs))
    assert prev.status_code == 200 and "ergänzen" in prev.text
    token = re.search(r'name="token" value="([0-9a-f]+)"', prev.text).group(1)
    before = txs(c)
    r = c.post(base, data={"csrf_token": c.token, "token": "falsch", "d": docs}, follow_redirects=False)
    assert r.status_code == 409 and txs(c)["K-1"].fee_qty is None  # veraltete Vorschau ändert nichts
    r = c.post(base, data={"csrf_token": c.token, "token": token, "d": docs}, follow_redirects=False)
    assert r.status_code == 303, r.text[:400]
    now = txs(c)
    assert now["K-1"].fee_qty == D1("36.43") and now["K-1"].fee_asset == "EUR" and now["K-1"].value_eur == D1("301.46")
    assert now["K-2"].fee_qty == D1("15.00") and now["K-2"].value_eur == D1("11310.00")
    for k in ("K-1", "K-2"):  # Menge, Asset, Konto, Art unverändert
        assert (now[k].type, now[k].to_asset, now[k].to_qty, now[k].from_asset, now[k].from_qty,
                now[k].to_account, now[k].from_account) == (before[k].type, before[k].to_asset, before[k].to_qty,
                                                             before[k].from_asset, before[k].from_qty,
                                                             before[k].to_account, before[k].from_account)
    assert {r["source"] for r in rows(c, "SELECT source FROM tx_document")} == {"amend"}
    # eine Entscheidung → als Ganzes rückgängig
    dec = rows(c, "SELECT id, ops_json FROM diag_decision ORDER BY id DESC LIMIT 1")[0]
    assert dec["ops_json"].count('"kind": "amend"') == 2
    res = A.undo(ctx(c), dec["id"])
    assert res.ok, res.errors
    back = txs(c)
    assert back["K-1"].fee_qty is None and back["K-1"].value_eur == D1("337.00") and back["K-2"].fee_qty is None
    # nach der Ergänzung ist nichts mehr vorzuschlagen
    ctx(c).db.x("DELETE FROM diag_decision")  # nur für die Folgeprüfung: Zustand bleibt, Beleg schlägt erneut vor
    assert AM.propose(ctx(c), docs[0], 1).target is not None


def test_hand_edited_entry_is_warned_not_locked(app) -> None:
    c, sid = app
    js = journal_service(ctx(c))
    data = js.import_form_data("K-1")
    assert data is not None
    data["value_eur"] = "340.00"
    assert js.save_import("K-1", data).ok
    assert txs(c)["K-1"].value_eur == D1("340.00")
    run = run_zip(c, sid, {"a.pdf": buy_pdf()})
    doc_id = run["summary"]["trades"]["matched"][0]["doc"]
    p = AM.propose(ctx(c), doc_id, 1)
    assert p.target.tx_id == "K-1" and not p.blocked and p.needs_confirm and "von Hand" in p.warning
    assert {c_.field for c_ in p.changes} >= {"fee", "value_eur"}
    # Sammelaktion nimmt sie nicht auf
    items = AM.bulk_candidates(ctx(c), [(doc_id, 1)])
    assert items and items[0].needs_confirm
    pl, _f, _l = AM.bulk_plan(ctx(c), [(doc_id, 1)])
    assert pl.errors and not pl.ops
    # Einzel-Vorschau zeigt die Warnung; Übernehmen ohne Bestätigung wird abgelehnt
    page = c.get(f"/journal/documents/{doc_id}/amend")
    assert "Von Hand geändert" in page.text and 'name="confirm"' in page.text
    token = re.search(r'name="token" value="([0-9a-f]+)"', page.text).group(1)
    sel = [c_.field for c_ in p.changes]
    r = c.post(f"/journal/documents/{doc_id}/amend", data={"csrf_token": c.token, "n": "1", "token": token, "f": sel},
               follow_redirects=False)
    assert r.status_code == 409 and txs(c)["K-1"].fee_qty is None
    r = c.post(f"/journal/documents/{doc_id}/amend",
               data={"csrf_token": c.token, "n": "1", "token": token, "f": sel, "confirm": "1"},
               follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    assert txs(c)["K-1"].fee_qty == D1("36.43") and txs(c)["K-1"].value_eur == D1("301.46")
    # Rückgängig stellt die Handänderung exakt wieder her
    dec = rows(c, "SELECT id FROM diag_decision ORDER BY id DESC LIMIT 1")[0]
    assert A.undo(ctx(c), dec["id"]).ok
    assert txs(c)["K-1"].value_eur == D1("340.00") and txs(c)["K-1"].fee_qty is None


def test_savings_plan_estimate_stays_locked() -> None:
    from app.documentimport.amend import Proposal

    p = Proposal(1, 1, None)
    assert not p.needs_confirm and p.warning == "" and p.blocked == ""


def test_tx_document_backfill_display_and_missing_original(app) -> None:
    c, sid = app
    run = run_zip(c, sid, {"a.pdf": buy_pdf()})
    doc_id = run["summary"]["trades"]["matched"][0]["doc"]
    db = ctx(c).db
    assert txdocs.link_document(db, "K-0", doc_id, "kosteninformation", "manual")
    assert not txdocs.link_document(db, "K-0", doc_id, "kosteninformation", "auto")  # idempotent
    got = txdocs.for_txs(db, ["K-0", "K-1", "gibtsnicht"])
    assert [d["label"] for d in got["K-0"]] == ["Kosteninformation"] and got["K-1"][0]["label"] == "Abrechnung"
    assert "gibtsnicht" not in got and got["K-1"][0]["id"] == doc_id and got["K-1"][0]["date"] == "2024-08-06"
    # Original nicht gespeichert → Eintrag ohne Download
    db.x("UPDATE document SET stored=0 WHERE id=?", (doc_id,))
    page = c.get("/journal?q=K-1").text
    assert "Originaldatei nicht gespeichert" in page
    # Rückfüllung aus Notiz mit SHA-Präfix (App-Buchung mit Belegverweis in der Notiz)
    sha = db.q1("SELECT sha256 FROM document WHERE id=?", (doc_id,))["sha256"]
    db.x("DELETE FROM tx_document")
    r = post(c, "/journal/new", kind="buy", date="2025-03-01", account=DKB, asset="WKN:A40EDP", qty="1",
             amount="10,00", note=f"Beleg x.pdf (SHA {sha[:12]})")
    assert r.status_code == 303, r.text[:300]
    assert txdocs.backfill(db) == 1
    linked = rows(c, "SELECT source, role FROM tx_document")
    assert linked == [{"source": "backfill", "role": "abrechnung"}]
    # Verknüpfung entfernen
    txdocs.unlink_document(db, "K-0", doc_id)
    assert "K-0" not in txdocs.for_txs(db, ["K-0"])


def test_ledger_unchanged_by_matching_and_linking(app) -> None:
    c, sid = app
    before = {k: (t.type, t.to_qty, t.from_qty, t.value_eur, t.fee_qty) for k, t in txs(c).items()}
    run_zip(c, sid, {"a.pdf": buy_pdf(), "b.pdf": sell_pdf()})
    after = {k: (t.type, t.to_qty, t.from_qty, t.value_eur, t.fee_qty) for k, t in txs(c).items()}
    assert before == after  # Zuordnen und Verknüpfen ändern keine Buchung
    assert D.documents(ctx(c).db, sid)["categories"]["Wertpapierdokumente"]["evaluated"] == 2


def test_amend_page_for_unmatched_document_explains(app) -> None:
    c, sid = app
    run = run_zip(c, sid, {"q.pdf": X.pdf_of(X.abrechnung("Kauf", order="286297/68.00", qty="11", kurswert="331,61"))})
    doc = run["summary"]["trades"]["new"][0]["doc"]
    assert "neuen Vorgang" in c.get(f"/journal/documents/{doc}/amend").text
    wait(sid, 5)
