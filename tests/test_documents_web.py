"""M25 Oberfläche: Upload (XHR und Formular), Fortschritt, Prüfansicht, Ausschnitt, Korrektur, Ergänzung, Datenschutz.

Nur synthetische Belege (tests/docfixtures.py); keine Netzwerkzugriffe.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.documentimport.service import DocumentService
from app.importer.loader import import_file
from app.importer.zipbuilder import build_zip
from app.main import build_app
from tests.docfixtures import screenshot, text_pdf
from tests.helpers import ASSETS, tx

UUID = "1eecf73c-0000-4000-8000-00000000abcd"
SAP = {"asset_id": "SAP", "name": "SAP SE", "asset_class": "security", "quote_source": "yahoo", "quote_id": "SAP.DE",
       "isin": "DE0007164600", "wkn": "716460"}
BANK_PDF = ["Musterbank AG", "Wertpapierabrechnung Kauf", "Depot  1234567890",
            "Handelstag  02.01.2025  Handelszeit  09:15:03", "ISIN  DE0007164600  Stück  10", "SAP SE Inhaber-Aktien",
            "Kurs  123,45 EUR  Kurswert  1.234,50 EUR", "Provision  4,90 EUR", "Ausmachender Betrag  1.239,40 EUR",
            "Auftragsnummer  A-778899"]
BP_PDF = ["Bitpanda GmbH", "Kaufbestätigung", "Kauf  BTC", "Menge  0,00512345 BTC", "Preis  45.123,40 EUR",
          "Betrag  231,18 EUR", "Gebühr  1,49 EUR", "Datum  03.02.2025 14:22:05", f"Transaktions-ID  {UUID}"]


def _sync_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hintergrundlauf in Tests synchron (deterministisch); der echte Thread-Weg wird separat geprüft."""
    monkeypatch.setattr(DocumentService, "start", lambda self, sid: (self.run(sid), {"started": True})[1])


def _client(config: Config, extra: list[dict] | None = None) -> TestClient:
    rows = [tx("D1", "2024-12-01T10:00:00Z", "deposit", to=("Depot", "EUR", "5000"), value="5000")]
    if extra:
        rows += extra
    build_zip(config.import_dir / "k.zip", transactions=rows, assets=[*ASSETS, SAP],
              generated_at="2025-03-01T00:00:00Z", valuation_date="2024-12-31")
    app = build_app(config, start_scheduler=False)
    c = TestClient(app)
    c.__enter__()
    ctx = app.state.ctx
    assert import_file(ctx.db, config.import_dir / "k.zip", ctx.engine_options()).status == "imported"
    ctx.invalidate_data()
    c.get("/journal/documents")
    c.token = c.cookies.get("portfolia_csrf")  # type: ignore[attr-defined]
    return c


def _upload(c: TestClient, files: list[tuple[str, bytes, str]], xhr: bool = True, **data: str):
    headers = {"X-CSRF-Token": c.token, "X-Requested-With": "XMLHttpRequest"} if xhr else {}  # type: ignore[attr-defined]
    if not xhr:
        data = {"csrf_token": c.token, **data}  # type: ignore[attr-defined]
    return c.post("/journal/documents/upload", files=[("files", f) for f in files], data=data, headers=headers,
                  follow_redirects=False)


def test_index_renders_upload_and_privacy(config: Config) -> None:
    c = _client(config)
    r = c.get("/journal/documents")
    assert r.status_code == 200
    assert 'id="doc-upload"' in r.text and "documents.js" in r.text
    assert "Transaktions-Hash" in r.text  # Datenschutzhinweis zur öffentlichen Recherche
    assert 'name="public" value="1" disabled' in r.text  # ohne Freigabe nicht wählbar


def test_upload_xhr_stage_detail_preview_and_correct(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    _sync_start(monkeypatch)
    c = _client(config)
    shot = screenshot(["Bitpanda", "Kauf  BTC", "Menge  0,00512345 BTC", "Preis  45.123,40 €", "Betrag  231,18 €",
                       "Gebühr  1,49 €", "Datum  03.02.2025 14:22"])
    files = [("abrechnung.pdf", text_pdf(BANK_PDF), "application/pdf")]
    if shutil.which("tesseract"):
        files.append(("btc.png", shot, "image/png"))
    r = _upload(c, files, account="Depot")
    assert r.status_code == 200, r.text
    target = r.json()["redirect"]
    assert target.startswith("/journal/documents/stack/")
    page = c.get(target)
    assert page.status_code == 200 and "Prüf-Stapel" in page.text and "nichts gebucht" in page.text
    assert "/progress\" hx-trigger" not in page.text  # fertig → kein weiteres Nachfragen
    ctx = c.app.state.ctx  # type: ignore[attr-defined]
    doc = ctx.db.q1("SELECT * FROM document WHERE filename='abrechnung.pdf'")
    assert doc["status"] == "staged" and doc["batch_id"]
    # nichts gebucht
    assert ctx.db.scalar("SELECT COUNT(*) FROM journal_tx", default=0) == 0
    d = c.get(f"/journal/documents/{doc['id']}")
    assert d.status_code == 200
    for s in ("DE0007164600", "A · belegt", "Recherche-Protokoll", "empfohlen", "Angaben korrigieren"):
        assert s in d.text, s
    # Ausschnitt eines Felds
    pv = c.get(f"/journal/documents/{doc['id']}/preview?page=1&box=0.1,0.1,0.6,0.2")
    assert pv.status_code == 200 and pv.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert "no-store" in pv.headers["cache-control"]
    assert c.get(f"/journal/documents/{doc['id']}/preview?box=2,0,1,1").status_code == 400
    orig = c.get(f"/journal/documents/{doc['id']}/original")
    assert orig.status_code == 200 and orig.content.startswith(b"%PDF") and "attachment" in orig.headers[
        "content-disposition"]
    # Korrektur (Herkunft „Korrektur“) → neu bewertet, Prüfzeile ersetzt
    r = c.post(f"/journal/documents/{doc['id']}/correct", data={"csrf_token": c.token, "n": "1",  # type: ignore
                                                                  "fee": "5,00"}, follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    d = c.get(f"/journal/documents/{doc['id']}")
    assert "Korrektur durch den Nutzer" in d.text
    # Datenschutz: Original und Volltext löschen
    r = c.post(f"/journal/documents/{doc['id']}/delete-original", data={"csrf_token": c.token},  # type: ignore
               follow_redirects=False)
    assert r.status_code == 303
    assert c.get(f"/journal/documents/{doc['id']}/original").status_code == 404
    assert c.get(f"/journal/documents/{doc['id']}/preview").status_code == 404
    row = ctx.db.q1("SELECT * FROM document WHERE id=?", (doc["id"],))
    assert row["stored"] == 0 and row["analysis_json"] is None and row["result_json"]
    assert not list(Path(config.data_dir, "documents").rglob(f"{doc['sha256']}*"))


def test_reupload_detected_and_classic_form(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    _sync_start(monkeypatch)
    c = _client(config)
    pdf = text_pdf(BANK_PDF)
    assert _upload(c, [("a.pdf", pdf, "application/pdf")]).status_code == 200
    r = _upload(c, [("kopie.pdf", pdf, "application/pdf")], xhr=False)
    assert r.status_code == 303
    assert "Bereits+verarbeitet" in r.headers["location"] or "Bereits%20verarbeitet" in r.headers["location"]
    ctx = c.app.state.ctx  # type: ignore[attr-defined]
    assert ctx.db.scalar("SELECT COUNT(*) FROM document", default=0) == 1


def test_upload_rejects_wrong_type_and_too_many(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    _sync_start(monkeypatch)
    c = _client(config)
    r = _upload(c, [("x.pdf", b"MZ\x90\x00 not a pdf", "application/pdf")])
    assert r.status_code == 200 and r.json()["errors"]
    r = _upload(c, [(f"f{i}.pdf", b"%PDF-1.4", "application/pdf") for i in range(21)])
    assert any("Höchstens" in e for e in r.json()["errors"])
    ctx = c.app.state.ctx  # type: ignore[attr-defined]
    assert ctx.db.scalar("SELECT COUNT(*) FROM document_stack", default=0) == 0


def test_upload_requires_csrf(config: Config) -> None:
    c = _client(config)
    r = c.post("/journal/documents/upload", files=[("files", ("a.pdf", text_pdf(BANK_PDF), "application/pdf"))],
               headers={"X-Requested-With": "XMLHttpRequest"})
    assert r.status_code == 403


def test_amend_preview_apply_and_undo(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    _sync_start(monkeypatch)
    k = tx("K1", "2025-02-03", "buy", frm=("Bitpanda", "EUR", "231.18"), to=("Bitpanda", "BTC", "0.00512345"),
           value="230")
    k["source"], k["source_ref"] = "bitpanda", UUID
    c = _client(config, [k])
    assert _upload(c, [("bp.pdf", text_pdf(BP_PDF), "application/pdf")]).status_code == 200
    ctx = c.app.state.ctx  # type: ignore[attr-defined]
    doc = ctx.db.q1("SELECT * FROM document")
    d = c.get(f"/journal/documents/{doc['id']}")
    assert "Bestehende Buchung ergänzen" in d.text
    pv = c.get(f"/journal/documents/{doc['id']}/amend?n=1")
    assert pv.status_code == 200 and "Auswirkungen" in pv.text and "K1" in pv.text
    import re

    token = re.search(r'name="token" value="([^"]+)"', pv.text).group(1)  # type: ignore[union-attr]
    fields = re.findall(r'<input type="hidden" name="f" value="([^"]+)">', pv.text)
    assert "value_eur" in fields and "fee" in fields
    r = c.post(f"/journal/documents/{doc['id']}/amend", data={"csrf_token": c.token, "n": "1",  # type: ignore
                                                               "token": token, "f": fields}, follow_redirects=False)
    assert r.status_code == 303, r.text
    ctx.invalidate_data()
    t = next(t for t in ctx.portfolio().txs if t.tx_id == "K1")
    assert str(t.value_eur) == "231.18" and t.fee_qty is not None and not t.date_only
    # stale token → nichts geändert, 409
    r = c.post(f"/journal/documents/{doc['id']}/amend", data={"csrf_token": c.token, "n": "1",  # type: ignore
                                                               "token": token, "f": fields}, follow_redirects=False)
    assert r.status_code == 409
    from app.diagnosis import actions as A

    did = ctx.db.scalar("SELECT MAX(id) FROM diag_decision", default=None)
    assert A.undo(ctx, int(did)).ok
    ctx.invalidate_data()
    t = next(t for t in ctx.portfolio().txs if t.tx_id == "K1")
    assert str(t.value_eur) == "230" and t.date_only


def test_background_start_progress_and_cancel_state(config: Config) -> None:
    c = _client(config)
    r = _upload(c, [("a.pdf", text_pdf(BANK_PDF), "application/pdf")])
    sid = r.json()["redirect"].rsplit("/", 1)[1].split("?")[0]
    ctx = c.app.state.ctx  # type: ignore[attr-defined]
    for _ in range(200):
        st = ctx.db.q1("SELECT status FROM document_stack WHERE id=?", (sid,))
        if st["status"] not in ("queued", "running"):
            break
        part = c.get(f"/journal/documents/stack/{sid}/progress")
        assert part.status_code == 200
        time.sleep(0.05)
    assert st["status"] == "done"
    assert "/progress\" hx-trigger" not in c.get(f"/journal/documents/stack/{sid}/progress").text
    r = c.post(f"/journal/documents/stack/{sid}/cancel", data={"csrf_token": c.token},  # type: ignore
               follow_redirects=False)
    assert r.status_code == 303  # nichts läuft mehr – Hinweis, keine Änderung
    assert ctx.db.q1("SELECT status FROM document_stack WHERE id=?", (sid,))["status"] == "done"


def test_bulk_and_settings(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    _sync_start(monkeypatch)
    c = _client(config)
    _upload(c, [("a.pdf", text_pdf(BANK_PDF), "application/pdf")])
    ctx = c.app.state.ctx  # type: ignore[attr-defined]
    did = ctx.db.scalar("SELECT id FROM document", default=None)
    r = c.post("/journal/documents/bulk", data={"csrf_token": c.token, "action": "reevaluate",  # type: ignore
                                                "ids": [str(did)]}, follow_redirects=False)
    assert r.status_code == 303 and "neu+bewertet" in r.headers["location"]
    r = c.post("/journal/documents/settings", data={"csrf_token": c.token, "keep_originals": "1",  # type: ignore
                                                    "public_lookup": "1", "language": "deu"}, follow_redirects=False)
    assert r.status_code == 303
    assert ctx.settings.get("documents.public_lookup") is True and ctx.settings.get("documents.ocr") is False
    assert ctx.settings.get("documents.language") == "deu"
    r = c.post("/journal/documents/bulk", data={"csrf_token": c.token, "action": "delete_original",  # type: ignore
                                                "ids": [str(did)]}, follow_redirects=False)
    assert "Originale+gel" in r.headers["location"]
