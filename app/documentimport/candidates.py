"""Conservative transaction candidates from locally extracted document lines.

Never fabricates times, executed prices, acquisition costs or missing fees.
Every candidate is review-only until mapped through the existing import service.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from app.documentimport.extract import DocumentError, DocumentResult, financial_decimal

DATE = re.compile(r"\b(\d{1,2}\.\d{1,2}\.\d{4}|\d{4}-\d{2}-\d{2})\b")
ISIN = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}\d\b")
AMOUNT = re.compile(r"(?<![\w.])[-−]?(?:\d{1,3}(?:[.,]\d{3})+[.,]\d+|\d+[.,]\d+|\d+)\s*(EUR|USD|CHF|GBP|€|\$)(?!\w)", re.I)
QTY = re.compile(r"\b(?:Menge|Stück|Quantity|Amount)\s*[:=]?\s*([\d.,]+)\s*([A-Z]{2,10})?\b", re.I)
ACTION = re.compile(r"\b(Kauf|Verkauf|Buy|Sell|Dividend|Dividende|Transfer|Übertrag)\b", re.I)
MAX_BLOCKS = 300


@dataclass(frozen=True)
class CandidateField:
    name: str
    value: str
    page: int
    line: int
    status: str = "belegt"


@dataclass
class TransactionCandidate:
    kind: str
    fields: list[CandidateField] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    review_required: bool = True


def _kind(value: str) -> str:
    return {
        "kauf": "buy", "buy": "buy", "verkauf": "sell", "sell": "sell",
        "dividend": "dividend", "dividende": "dividend", "transfer": "transfer",
        "übertrag": "transfer",
    }[value.lower()]


def candidates(document: DocumentResult) -> list[TransactionCandidate]:
    """Split only rows containing an explicit action; other lines remain non-bookable.

    Nearby dates, quantities and amounts are candidates, never assumed to be a
    completed trade or copied to neighbouring operations.
    """
    found: list[TransactionCandidate] = []
    for page in document.pages:
        for lineno, line in enumerate(page.text.splitlines(), 1):
            action = ACTION.search(line)
            if action is None:
                continue
            if len(found) >= MAX_BLOCKS:
                raise DocumentError("Zu viele Vorgänge für eine sichere Analyse.")
            item = TransactionCandidate(_kind(action.group()))
            item.fields.append(CandidateField("action", action.group(), page.number, lineno))
            date = DATE.search(line)
            if date:
                item.fields.append(CandidateField("date", date.group(), page.number, lineno))
            isin = ISIN.search(line)
            if isin:
                item.fields.append(CandidateField("isin", isin.group(), page.number, lineno))
            for match in AMOUNT.finditer(line):
                currency = {"€": "EUR", "$": "USD"}.get(match.group(1).upper(), match.group(1).upper())
                raw = line[match.start():match.end() - len(match.group(1))].strip()
                try:
                    amount: Decimal = financial_decimal(raw)
                except DocumentError:
                    item.warnings.append(f"Mehrdeutiger Betrag in Zeile {lineno}; manuell prüfen")
                    continue
                item.fields.append(CandidateField("amount", str(amount), page.number, lineno))
                item.fields.append(CandidateField("currency", currency, page.number, lineno))
            quantity = QTY.search(line)
            if quantity:
                try:
                    qty = financial_decimal(quantity.group(1))
                except DocumentError:
                    item.warnings.append("Menge nicht eindeutig lesbar")
                else:
                    item.fields.append(CandidateField("quantity", str(qty), page.number, lineno))
                    if quantity.group(2):
                        item.fields.append(CandidateField("symbol", quantity.group(2).upper(), page.number, lineno))
            required = {"action", "date", "amount"}
            present = {f.name for f in item.fields}
            if not required <= present:
                item.warnings.append("Unvollständiger Beleg: keine Buchungsfreigabe")
            found.append(item)
    return found
