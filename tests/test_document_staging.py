"""Document candidate staging must be financially inert."""
from app.csvimport.model import REVIEW
from app.documentimport.candidates import candidates
from app.documentimport.extract import DocumentPage, DocumentResult
from app.documentimport.staging import review_recs


def test_review_staging_has_no_economic_legs():
    doc = DocumentResult("a" * 64, "pdf", [
        DocumentPage(1, "Kauf 09.10.2026 DE000BASF111 150,25 EUR", "text")
    ])
    recs = review_recs(doc, candidates(doc))
    assert len(recs) == 1
    rec = recs[0]
    assert rec.kind == REVIEW
    assert rec.review
    assert rec.ts_missing
    assert rec.out_sym is None and rec.in_sym is None
    assert rec.out_qty is None and rec.in_qty is None
    assert rec.value is None and rec.fee_qty is None
    assert rec.event_key == f"document:{doc.sha256}:1"
    assert rec.raw["fields"]


def test_distinct_document_hashes_make_distinct_staging_event_keys():
    doc1 = DocumentResult("a" * 64, "pdf", [DocumentPage(1, "Kauf 10.10.2026", "text")])
    doc2 = DocumentResult("b" * 64, "pdf", [DocumentPage(1, "Kauf 10.10.2026", "text")])
    r1 = review_recs(doc1, candidates(doc1))[0]
    r2 = review_recs(doc2, candidates(doc2))[0]
    assert r1.event_key != r2.event_key
