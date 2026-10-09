"""Evidence-first field reconciliation. No ledger or provider writes.

Only exact source references and deterministic derivations can substantiate a
transaction field. Conflicts are retained instead of silently choosing a value.
"""
from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

Status = Literal["belegt", "rekonstruiert", "geschaetzt", "ungeloest"]
Origin = Literal["document", "portfolio", "provider", "public", "user", "batch"]
_PRIORITY = {"user": 0, "provider": 1, "document": 2, "batch": 3, "portfolio": 4, "public": 5}
_NOT_EXECUTION_EVIDENCE = frozenset({"market_price", "reference_fx", "estimated_cost_basis"})
NUMERIC_FIELDS = frozenset({"quantity", "amount", "fee", "value_eur", "price", "gross", "net", "fx_rate",
                            "network_fee", "tax", "withholding_tax", "fee_eur"})


@dataclass(frozen=True)
class FieldEvidence:
    field: str
    value: str
    origin: Origin
    source_ref: str
    location: str
    status: Status
    reason: str
    event_key: str | None = None
    category: str = "original"
    # To prevent attaching arbitrary account records to the wrong event,
    # portfolio/provider values require an exact verified event key.
    verified_link: bool = False
    page: int | None = None  # Belegstelle (Dokumentseite, Zeile, relative Box) für Vorschau und Nachprüfung
    line: int | None = None
    box: tuple[float, float, float, float] | None = None
    conf: float | None = None  # OCR-Konfidenz der Zeile (0–100); None = eingebetteter Text bzw. keine OCR


@dataclass
class FieldDecision:
    field: str
    selected: FieldEvidence | None = None
    alternatives: list[FieldEvidence] = field(default_factory=list)
    conflicts: list[FieldEvidence] = field(default_factory=list)
    unresolved_reason: str = ""
    review_required: bool = True


def _equivalent(field_name: str, a: str, b: str) -> bool:
    if a == b:
        return True
    if field_name in NUMERIC_FIELDS:
        try:
            return Decimal(a) == Decimal(b)
        except ArithmeticError:
            return False
    return False


def resolve_field(field_name: str, evidence: list[FieldEvidence],
                  *, event_key: str | Collection[str] | None = None) -> FieldDecision:
    """Select a cited candidate only when identity and financial provenance permit it.

    Public price estimates may be displayed as alternatives but are never used
    as real executed prices, fees or acquisition costs.
    """
    keys = {event_key} if isinstance(event_key, str) else set(event_key or ())
    usable: list[FieldEvidence] = []
    rejected: list[FieldEvidence] = []
    for candidate in evidence:
        if candidate.field != field_name:  # Belege anderer Felder sind keine Alternative
            continue
        if not candidate.value or not candidate.source_ref:
            rejected.append(candidate)
            continue
        if candidate.origin in ("portfolio", "provider") and (
            not keys or not candidate.verified_link or candidate.event_key not in keys
        ):
            rejected.append(candidate)
            continue
        if candidate.origin == "public" and candidate.category in _NOT_EXECUTION_EVIDENCE and field_name in (
            "price", "fee", "cost_basis", "execution_time", "txhash"
        ):
            rejected.append(candidate)
            continue
        if candidate.status == "ungeloest":
            rejected.append(candidate)
            continue
        usable.append(candidate)
    usable.sort(key=lambda c: (
        c.status == "geschaetzt", _PRIORITY[c.origin], c.source_ref, c.location
    ))
    result = FieldDecision(field=field_name, alternatives=[*usable, *rejected])
    if not usable:
        result.unresolved_reason = "Kein überprüfbarer oder eindeutig zugeordneter Wert."
        return result
    winner = usable[0]
    result.selected = winner
    result.conflicts = [
        c for c in usable[1:] if not _equivalent(field_name, winner.value, c.value)
    ]
    result.review_required = bool(result.conflicts) or winner.status == "geschaetzt"
    # Even a source-supported candidate needs review before journal mutation.
    return result


def resolve_fields(evidence: list[FieldEvidence], *,
                   event_key: str | Collection[str] | None = None) -> dict[str, FieldDecision]:
    return {name: resolve_field(name, evidence, event_key=event_key)
            for name in sorted({item.field for item in evidence})}


def safe_for_booking(decisions: dict[str, FieldDecision], required: set[str]) -> bool:
    """Necessary, not sufficient: importer approval and existing reconciliation follow."""
    for name in required:
        decision = decisions.get(name)
        if decision is None or decision.selected is None or decision.conflicts:
            return False
        if decision.selected.status not in ("belegt", "rekonstruiert"):
            return False
        if decision.selected.origin == "public" and name in (
            "price", "fee", "cost_basis", "execution_time", "txhash"
        ):
            return False
    return True
