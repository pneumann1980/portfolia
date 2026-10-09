"""M25: konservative Belegextraktion (keine Journal-Mutationen)."""
from __future__ import annotations

import pytest

from app.documentimport.extract import (
    DocumentError,
    DocumentPage,
    DocumentResult,
    field_evidence,
    financial_decimal,
    sniff,
)


@pytest.mark.parametrize(("value", "hint", "expected"), [
    ("1.234,56", None, "1234.56"),
    ("1,234.56", None, "1234.56"),
    ("0,00001234", None, "0.00001234"),
    ("-25,50", None, "-25.50"),
    ("(1.234,56)", None, "-1234.56"),
    ("1,234", ",", "1.234"),
    ("1.234", ".", "1.234"),
])
def test_decimal(value: str, hint: str | None, expected: str) -> None:
    assert str(financial_decimal(value, decimal_hint=hint)) == expected


@pytest.mark.parametrize("value", ["1,234", "1.234", "NaN", "1,2,3", "EUR", "1e200"])
def test_ambiguous_or_invalid_decimal(value: str) -> None:
    with pytest.raises(DocumentError):
        financial_decimal(value)


def test_content_magic_not_extension() -> None:
    assert sniff(b"%PDF-1.7 example") == "pdf"
    assert sniff(b"\x89PNG\r\n\x1a\nexample") == "png"
    assert sniff(b"RIFFzzzzWEBPextra") == "webp"
    with pytest.raises(DocumentError):
        sniff(b"<script>alert(1)</script>")


def test_file_size() -> None:
    with pytest.raises(DocumentError):
        sniff(b"x" * (25 * 1024 * 1024 + 1))


def test_field_evidence_provenance() -> None:
    doc = DocumentResult("a" * 64, "pdf", [
        DocumentPage(2, "ISIN DE000BASF111 Kauf 09.10.2026", "text"),
    ])
    got = field_evidence(doc)
    assert got["isin"][0].status == "belegt"
    assert got["isin"][0].location.startswith("Seite 2")
    assert got["date"][0].value == "09.10.2026"
