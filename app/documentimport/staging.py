"""Bridge from document candidates to the existing CSV staging service.

All rows are REVIEW records. No date, price or fee is synthesized. Importing
these records into staging does not authorize booking; a user must resolve the
data in the existing review UI first.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from app.csvimport.model import REVIEW, Rec
from app.csvimport.service import csv_service
from app.documentimport.candidates import TransactionCandidate
from app.documentimport.extract import DocumentResult


def review_recs(document: DocumentResult, items: list[TransactionCandidate]) -> list[Rec]:
    out: list[Rec] = []
    for index, item in enumerate(items, 1):
        fields = [{"name": f.name, "value": f.value, "page": f.page,
                   "line": f.line, "status": f.status} for f in item.fields]
        # REVIEW deliberately has no economic legs and is never auto-bookable.
        out.append(Rec(
            line=index, ts=datetime(1970, 1, 1, tzinfo=UTC), ts_missing=True,
            kind=REVIEW, ext_id=f"document:{document.sha256}:{index}",
            event_key=f"document:{document.sha256}:{index}", event_line=1,
            raw={"document_sha256": document.sha256, "file_type": document.file_type,
                 "candidate_kind": item.kind, "fields": fields},
            label=f"Dokument: {item.kind}",
            review="Unbestätigter Dokumentkandidat: Felder manuell validieren.",
            note="Dokumentauswertung, keine Original-Börsenbuchung",
        ))
    return out


def stage_review(ctx: Any, document: DocumentResult,
                 items: list[TransactionCandidate], *, account: str = "") -> int | None:
    """Persist candidates using the existing import service; no direct journal writes."""
    if not items:
        return None
    recs = review_recs(document, items)
    payload = json.dumps(
        {"document_sha256": document.sha256, "kind": document.file_type,
         "candidates": [r.raw for r in recs]},
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    svc = csv_service(ctx)
    # Repeated identical documents must not create another staging batch.
    # Only return an existing batch for this exact document source.
    prior = ctx.db.q1(
        "SELECT id FROM csv_batch WHERE source=? AND file_sha256=? "
        "AND status != ? ORDER BY id DESC LIMIT 1",
        ("document:review", hashlib.sha256(payload).hexdigest(), "reverted"),
    )
    if prior is not None:
        return int(prior["id"])
    return svc.ingest(
        recs, source="document:review", profile="document:review",
        account=account or "Dokument (ungeklärt)",
        label=f"Dokumentprüfung {document.sha256[:12]}",
        datasource_id=None, payload=payload,
    )
