"""Buchungen des kuratierten Imports bearbeiten oder löschen – als Überlagerung, die Import-Datei bleibt unverändert.

Regeln
    * Schlüssel ist die ``tx_id``; eine Änderung gilt über spätere Importe hinweg.
    * **Bearbeiten:** Die bearbeitete Fassung ersetzt die Import-Fassung (gleiche ``tx_id``; Herkunft ``source``,
      ``source_ref``, ``flag``, ``orig_price``/``orig_ccy`` bleiben erhalten). Geprüft wird wie beim Import.
    * **Löschen:** Die Buchung zählt nicht mehr; umkehrbar. Eine vorher bearbeitete Fassung bleibt dabei erhalten.
    * Ein neuer Import ohne diese ``tx_id`` → Änderung „ohne Wirkung“ (angezeigt, entfernbar). Liefert ein neuer
      Import eine andere Fassung als beim Bearbeiten → Hinweis „Import geändert“; die eigene Änderung gilt weiter.
    * Passt eine bearbeitete Fassung nicht mehr (z. B. Asset fehlt im neuen Import), gilt die Import-Fassung und die
      Änderung wird als „nicht anwendbar“ gemeldet – nie still verworfen.
    * Bewertung, Steuer, Sparpläne und Gesamtexport sehen die wirksame Fassung.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.importer.validate import validate_tx_rows
from app.ledger.models import AssetInfo, Portfolio, Tx

STATUS_TEXT = {
    "ok": "",
    "changed": "Der aktive Import enthält eine andere Fassung als beim Bearbeiten – deine Änderung gilt weiter; bitte "
               "prüfen.",
    "orphan": "Die Buchung ist im aktiven Import nicht mehr enthalten – die Änderung hat keine Wirkung.",
    "invalid": "Die bearbeitete Fassung passt nicht zum aktiven Import (z. B. Asset fehlt) – es gilt die "
               "Import-Fassung.",
}


@dataclass(frozen=True)
class OverrideState:
    tx_id: str
    action: str  # edit | delete
    status: str  # ok | changed | orphan | invalid
    updated_at: str

    @property
    def text(self) -> str:
        return STATUS_TEXT.get(self.status, "")


def import_row(t: Tx) -> dict[str, str]:
    """Import-Fassung als Zeile (Vergleich und Ausgangspunkt der Bearbeitung)."""
    from app.journal.service import tx_row

    return tx_row(t)


def asset_classes(assets: Mapping[str, AssetInfo]) -> dict[str, dict[str, Any]]:
    return {aid: {"asset_class": a.asset_class} for aid, a in assets.items()}


def to_tx(row: Mapping[str, str], seq: int, classes: dict[str, dict[str, Any]]) -> tuple[Tx | None, list[str]]:
    """Zeile (Spalten wie transactions.csv) → Buchung mit denselben Regeln wie beim Import."""
    rep, parsed = validate_tx_rows([dict(row)], classes)
    if rep.errors or not parsed:
        return None, [m.message for m in rep.errors]
    return Tx.from_parsed({**parsed[0], "seq": seq}), [m.message for m in rep.warnings]


def load(db: Any) -> dict[str, Any]:
    return {r["tx_id"]: r for r in db.q("SELECT * FROM tx_override")}


def apply(db: Any, base: Portfolio | None, extra_assets: Mapping[str, AssetInfo] | None = None
          ) -> tuple[Portfolio | None, dict[str, OverrideState]]:
    """Import mit wirksamen Änderungen/Löschungen und Zustand je Änderung."""
    rows = load(db)
    if not rows:
        return base, {}
    states: dict[str, OverrideState] = {}
    if base is None:
        return base, {tid: OverrideState(tid, r["action"], "orphan", r["updated_at"]) for tid, r in rows.items()}
    classes = asset_classes({**(extra_assets or {}), **base.assets})
    out: list[Tx] = []
    seen: set[str] = set()
    touched = False
    for t in base.txs:
        seen.add(t.tx_id)
        r = rows.get(t.tx_id)
        if r is None:
            out.append(t)
            continue
        status = "ok" if json.loads(r["base_json"]) == import_row(t) else "changed"
        if r["action"] == "delete":
            states[t.tx_id] = OverrideState(t.tx_id, "delete", status, r["updated_at"])
            touched = True
            continue
        new, _ = to_tx(json.loads(r["row_json"] or "{}"), t.seq, classes)
        if new is None:
            states[t.tx_id] = OverrideState(t.tx_id, "edit", "invalid", r["updated_at"])
            out.append(t)
            continue
        states[t.tx_id] = OverrideState(t.tx_id, "edit", status, r["updated_at"])
        out.append(new)
        touched = True
    for tid, r in rows.items():
        if tid not in seen:
            states[tid] = OverrideState(tid, r["action"], "orphan", r["updated_at"])
    return (dataclasses.replace(base, txs=out) if touched else base), states
