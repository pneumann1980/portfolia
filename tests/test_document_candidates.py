"""Synthetic candidate splitting; no inferred execution facts."""
from app.documentimport.candidates import candidates
from app.documentimport.extract import DocumentPage, DocumentResult


def document(text: str) -> DocumentResult:
    return DocumentResult("sha", "pdf", [DocumentPage(1, text, "text")])


def test_two_trades_are_separate_and_never_bookable_automatically():
    rows = candidates(document(
        "Kauf 09.10.2026 DE000BASF111 150,25 EUR\n"
        "Verkauf 10.10.2026 DE000BASF111 160,00 EUR"
    ))
    assert len(rows) == 2
    assert [r.kind for r in rows] == ["buy", "sell"]
    assert all(r.review_required for r in rows)
    assert [next(f.value for f in r.fields if f.name == "amount") for r in rows] == [
        "150.25", "160.00"
    ]


def test_missing_values_remain_unresolved():
    rows = candidates(document("Kauf 09.10.2026\nGebühr 8,90 EUR"))
    assert len(rows) == 1
    assert not any(f.name == "amount" for f in rows[0].fields)
    assert rows[0].warnings


def test_ambiguous_amount_is_not_taken_as_trade():
    rows = candidates(document("Kauf 09.10.2026 1,234 EUR"))
    assert len(rows) == 1
    assert not any(f.name == "amount" for f in rows[0].fields)
    assert rows[0].warnings


def test_different_pages_not_silently_merged():
    d = DocumentResult("sha", "pdf", [
        DocumentPage(1, "Kauf 09.10.2026", "text"),
        DocumentPage(2, "150,25 EUR", "text"),
    ])
    rows = candidates(d)
    assert len(rows) == 1
    assert not any(f.name == "amount" for f in rows[0].fields)
