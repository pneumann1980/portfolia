"""Beleg ↔ Buchung (Tabelle ``tx_document``): Verknüpfung, Rückfüllung und Anzeige.

Ein Beleg (``document``) kann zu mehreren Buchungen gehören und eine Buchung hat mehrere Belege. Geschrieben wird

* beim **Ergänzen** einer Buchung aus dem Beleg (``source='amend'``) bzw. bei der automatischen Zuordnung ohne
  Änderung, z. B. DKB-Abrechnung → vorhandene Buchung (``source='auto'``),
* bei der **Rückfüllung** (``source='backfill'``, idempotent und lesend gegenüber den Buchungen): übernommene Zeilen aus
  Beleg-Stapeln des Prüf-Stapels und Notizen der Form „Beleg … (SHA 0123456789ab)“.

Angezeigt wird in der Buchungsliste und der Positionsansicht je Buchung ein Hinweis „Beleg“ mit Link auf die
Detailseite des Belegs (Ganzzahl-ID, keine Pfade). Ist das Original nicht gespeichert, steht der Beleg ohne Download.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

ROLE_LABEL = {"abrechnung": "Abrechnung", "ausfuehrung": "Ausführungsanzeige", "order": "Order-Beleg",
              "kosteninformation": "Kosteninformation", "kapitalmassnahme": "Kapitalmaßnahme",
              "depotauszug": "Depotauszug", "ertrag": "Ertragsabrechnung", "beleg": "Beleg"}
# DKB-Dokumentart (app.documentimport.dkb) → Rolle
ROLE_OF_KIND = {"abrechnung_kauf": "abrechnung", "abrechnung_verkauf": "abrechnung", "ertrag": "ertrag",
                "kapitalmassnahme": "kapitalmassnahme", "depotuebertrag": "kapitalmassnahme",
                "depotauszug": "depotauszug", "ausfuehrungsanzeige": "ausfuehrung", "auftragsbestaetigung": "order",
                "streichung": "order", "kosteninfo_order": "kosteninformation"}
_SHA_NOTE = re.compile(r"SHA ([0-9a-f]{12})")
_SRC_RANK = {"manual": 3, "amend": 2, "auto": 1, "backfill": 0}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def link_document(db: Any, tx_id: str, document_id: int, role: str = "beleg", source: str = "auto",
                  ref: str | None = None) -> bool:
    """Verknüpfung anlegen (idempotent). Eine bestehende bleibt, außer die neue Quelle ist höherrangig."""
    if role not in ROLE_LABEL:
        role = "beleg"
    cur = db.q1("SELECT source FROM tx_document WHERE tx_id=? AND document_id=?", (tx_id, document_id))
    if cur is None:
        db.x("INSERT INTO tx_document(tx_id, document_id, role, source, ref, created_at) VALUES (?,?,?,?,?,?)",
             (tx_id, document_id, role, source, ref, _now()))
        return True
    if _SRC_RANK.get(source, 0) > _SRC_RANK.get(cur["source"], 0):
        db.x("UPDATE tx_document SET role=?, source=?, ref=COALESCE(?, ref) WHERE tx_id=? AND document_id=?",
             (role, source, ref, tx_id, document_id))
    return False


def unlink_document(db: Any, tx_id: str, document_id: int) -> None:
    db.x("DELETE FROM tx_document WHERE tx_id=? AND document_id=?", (tx_id, document_id))


def role_of_document(db: Any, document_id: int, default: str = "beleg") -> str:
    """Rolle laut DKB-Index (``bank_doc.doctype``) über die Prüfsumme des Belegs."""
    r = db.q1("SELECT b.doctype FROM bank_doc b JOIN document d ON d.sha256=b.sha256 WHERE d.id=? LIMIT 1",
              (document_id,))
    return ROLE_OF_KIND.get(r["doctype"], default) if r is not None and r["doctype"] else default


def backfill(db: Any) -> int:
    """Rückfüllung: noch nicht verknüpfte, aus Beleg-Stapeln übernommene Zeilen und Notizen mit Belegverweis. Gibt die
    Zahl neuer Verknüpfungen zurück."""
    n = 0
    rows = db.q(
        "SELECT r.tx_id, r.rec_json FROM csv_row r JOIN csv_batch b ON b.id=r.batch_id WHERE b.source LIKE 'doc:%' "
        "AND r.tx_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM tx_document d WHERE d.tx_id=r.tx_id)")
    for r in rows:
        try:
            doc_id = int((((json.loads(r["rec_json"]).get("raw") or {}).get("document")) or {}).get("id") or 0)
        except (ValueError, TypeError):
            continue
        if doc_id and db.scalar("SELECT 1 FROM document WHERE id=?", (doc_id,)):
            n += link_document(db, r["tx_id"], doc_id, role_of_document(db, doc_id, "abrechnung"), "backfill")
    for r in db.q("SELECT tx_id, note FROM journal_tx WHERE status='active' AND note LIKE '%SHA %' AND NOT EXISTS "
                  "(SELECT 1 FROM tx_document d WHERE d.tx_id=journal_tx.tx_id)"):
        m = _SHA_NOTE.search(r["note"] or "")
        if not m:
            continue
        d = db.q1("SELECT id FROM document WHERE sha256 LIKE ? LIMIT 2", (m.group(1) + "%",))
        if d is not None:
            n += link_document(db, r["tx_id"], int(d["id"]), role_of_document(db, int(d["id"]), "abrechnung"),
                               "backfill")
    return n


def for_txs(db: Any, tx_ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    """Belege je Buchung für eine Seite (eine Abfrage, keine N+1-Abfragen)."""
    ids = list(dict.fromkeys(tx_ids))
    out: dict[str, list[dict[str, Any]]] = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        marks = ",".join("?" * len(part))
        for r in db.q(
                f"SELECT t.tx_id, t.role, t.source, d.id, d.filename, d.stored, d.created_at, "
                f"(SELECT b.doc_date FROM bank_doc b WHERE b.sha256=d.sha256 LIMIT 1) AS doc_date "
                f"FROM tx_document t JOIN document d ON d.id=t.document_id WHERE t.tx_id IN ({marks}) "
                f"ORDER BY COALESCE(doc_date, d.created_at), d.id", part):
            out.setdefault(r["tx_id"], []).append({
                "id": int(r["id"]), "role": r["role"], "label": ROLE_LABEL.get(r["role"], "Beleg"),
                "filename": r["filename"], "stored": bool(r["stored"]), "date": r["doc_date"] or "",
                "source": r["source"]})
    return out


def for_page(db: Any, tx_ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    """Rückfüllung (günstig, idempotent) und Belege für die angezeigten Buchungen."""
    backfill(db)
    return for_txs(db, tx_ids)
