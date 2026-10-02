"""Abgleich kuratierter Import ↔ App-Buchungen (Journal: manuell, CSV-Import, Datenquellen).

Problem
    Ein neuer kuratierter Import kann Börsenbuchungen enthalten, die zuvor schon in der App erfasst wurden (z. B.
    per Bitpanda-Synchronisierung nach dem Stand des alten Imports). Ohne Abgleich zählten beide.

Regeln
    * **Exakt, automatisch:** gleiche Anbieter-ID – die Import-Buchung trägt sie in ``source``/``source_ref``
      (``source_ref = bitpanda:<ID>`` oder ``source = bitpanda`` + nackte ID; Portfolia-Exporte enthalten sie
      ohnehin). Die App-Buchung zählt dann nicht mehr (Import hat Vorrang), bleibt aber mit Herkunft erhalten und
      zählt wieder, sobald ein späterer Import die Buchung nicht mehr enthält.
    * **Unsicher, nur Vorschlag:** gleiche Art, gleiche Assets, Menge ± 1 %, Datum ± 2 Tage, App-Buchung nicht
      nach dem Stand des Imports. Nie still zusammengeführt – der Nutzer entscheidet („Import-Buchung gilt“ bzw.
      „keine Dublette“); die Entscheidung speichert beide IDs (``journal_import_link``).
    * **Transferseite, nur Vorschlag:** ein Zu- bzw. Abgang der App (z. B. Wallet-Datenquelle), der einer Seite
      eines Import-Transfers entspricht – auch verzögert gutgeschrieben (Auszahlung der Börse Stunden bis Tage
      später) und unter anderem Kontonamen (Regeln: :mod:`app.csvimport.transfer_side`). „Import-Buchung gilt“
      lässt den Transfer zählen (Einstand und Haltedauer wandern mit), die App-Buchung nicht mehr.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from app.csvimport.events import identity_keys, source_ref_keys
from app.ledger.models import Portfolio, Tx
from app.util.timeutil import iso

QTY_TOL = Decimal("0.01")
DAYS = 2


@dataclass
class Coverage:
    """App-Buchung, die (wegen des Imports) nicht mitzählt."""

    journal_tx_id: str
    import_tx_id: str
    how: str  # exakt | entschieden


@dataclass
class Candidate:
    journal: Tx
    imports: list[Tx] = field(default_factory=list)
    distinct: set[str] = field(default_factory=set)
    why: dict[str, str] = field(default_factory=dict)  # Import-Buchung → Begründung (Transferseite)
    side: dict[str, Any] = field(default_factory=dict)  # Import-Buchung → Treffer (transfer_side.Hit.info)


def _aliases(db: Any) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for a in db.q("SELECT key, tx_id FROM journal_event_alias"):
        out[a["tx_id"]].add(a["key"])
    return out


def coverage(db: Any, base: Portfolio | None) -> dict[str, Coverage]:
    """App-Buchungen, die der kuratierte Import bereits enthält (exakt oder per Entscheidung)."""
    if base is None or not base.txs:
        return {}
    by_key: dict[str, str] = {}
    for t in base.txs:
        for k in source_ref_keys(t.source, t.source_ref):
            by_key.setdefault(k, t.tx_id)
    base_ids = {t.tx_id for t in base.txs}
    out: dict[str, Coverage] = {}
    if by_key:
        aliases = _aliases(db)
        for r in db.q("SELECT tx_id, external_id, event_key FROM journal_tx WHERE status='active' AND "
                      "(event_key IS NOT NULL OR external_id IS NOT NULL)"):
            for k in sorted(identity_keys(r["event_key"], aliases.get(r["tx_id"], set()), r["external_id"])):
                if k in by_key:
                    out[r["tx_id"]] = Coverage(r["tx_id"], by_key[k], "exakt")
                    break
    for r in db.q("SELECT journal_tx_id, import_tx_id FROM journal_import_link WHERE decision='covered'"):
        if r["import_tx_id"] in base_ids and r["journal_tx_id"] not in out:
            out[r["journal_tx_id"]] = Coverage(r["journal_tx_id"], r["import_tx_id"], "entschieden")
    return out


def _qty_close(a: Decimal | None, b: Decimal | None) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) <= max(abs(b) * QTY_TOL, Decimal("1e-8"))


def _decisions(db: Any) -> dict[str, dict[str, str]]:
    decided: dict[str, dict[str, str]] = defaultdict(dict)
    for r in db.q("SELECT journal_tx_id, import_tx_id, decision FROM journal_import_link"):
        decided[r["journal_tx_id"]][r["import_tx_id"]] = r["decision"]
    return decided


def transfer_sides(db: Any, pf: Portfolio | None) -> dict[str, list[Any]]:
    """Zählende App-Buchungen (Zu- bzw. Abgang ohne Einordnung), die einer Seite eines Import-Transfers entsprechen –
    auch verzögert und unter anderem Kontonamen (:mod:`app.csvimport.transfer_side`). Rückgabe: App-Buchung →
    Treffer (``Hit``). Seiten, für die bereits „Import-Buchung gilt“ entschieden ist, sind vergeben; „keine
    Dublette“ schließt das Paar aus."""
    from app.csvimport import transfer_side as TS
    from app.csvimport.events import normalize_hash

    if pf is None:
        return {}
    imports = [t for t in pf.txs if t.origin == "import"]
    journal = sorted((t for t in pf.txs if t.origin == "journal" and t.type in ("deposit", "withdrawal")
                      and not t.tag), key=lambda t: (t.ts, t.tx_id))
    if not imports or not journal:
        return {}
    idx = TS.TransferIndex(imports, TS.hash_lookup(db), TS.managed_accounts(db))
    if not idx:
        return {}
    decided = _decisions(db)
    covered = [jid for jid, d in decided.items() if "covered" in d.values()]
    types: dict[str, str] = {}
    for i in range(0, len(covered), 500):
        part = covered[i:i + 500]
        types.update({r["tx_id"]: r["type"] for r in db.q(
            f"SELECT tx_id, type FROM journal_tx WHERE tx_id IN ({','.join('?' * len(part))})", part)})
    for jid in covered:
        role = {"deposit": "in", "withdrawal": "out"}.get(types.get(jid, ""))
        for iid, dec in decided[jid].items():
            if dec == "covered" and role:
                idx.claim_side(iid, role)
    meta = {r["tx_id"]: (normalize_hash(r["tx_hash"]), r["datasource_id"]) for r in db.q(
        "SELECT tx_id, tx_hash, datasource_id FROM journal_tx WHERE status='active' AND ((tx_hash IS NOT NULL AND "
        "tx_hash <> '') OR datasource_id IS NOT NULL)")}
    out: dict[str, list[Any]] = {}
    for j in journal:
        h, ds_id = meta.get(j.tx_id, (None, None))
        p = TS.probe_of_tx(j, h, ds_id)
        if p is None:
            continue
        dist = {iid for iid, d in decided.get(j.tx_id, {}).items() if d == "distinct"}
        hit = idx.find(p, exclude=dist)
        if hit is not None:
            idx.claim(hit)
            out[j.tx_id] = [hit]
    return out


def candidates(db: Any, pf: Portfolio | None, base: Portfolio | None) -> list[Candidate]:
    """Mögliche Doppelzählungen: zählende App-Buchungen bis zum Stand des Imports mit ähnlicher Import-Buchung sowie
    Zu-/Abgänge, die einer Seite eines Import-Transfers entsprechen (:func:`transfer_sides`)."""
    if pf is None or base is None or base.valuation_date is None:
        return []
    limit = base.valuation_date + timedelta(days=1)
    index: dict[tuple[str, str, str], list[Tx]] = defaultdict(list)
    for t in base.txs:
        index[(t.type, t.from_asset or "", t.to_asset or "")].append(t)
    for lst in index.values():
        lst.sort(key=lambda t: t.date)
    decided = _decisions(db)
    out = []
    by_journal: dict[str, Candidate] = {}
    for j in pf.txs:
        if j.origin != "journal" or j.date > limit:
            continue
        cands = index.get((j.type, j.from_asset or "", j.to_asset or ""), [])
        lo = bisect.bisect_left([t.date for t in cands], j.date - timedelta(days=DAYS))
        hits = []
        for t in cands[lo:]:
            if t.date > j.date + timedelta(days=DAYS):
                break
            if _qty_close(j.from_qty, t.from_qty) and _qty_close(j.to_qty, t.to_qty):
                hits.append(t)
        if not hits:
            continue
        dist = {tid for tid, d in decided.get(j.tx_id, {}).items() if d == "distinct"}
        hits = [t for t in hits if t.tx_id not in dist]
        if hits:
            by_journal[j.tx_id] = Candidate(j, hits, dist)
            out.append(by_journal[j.tx_id])
    by_id = {t.tx_id: t for t in pf.txs}
    for jid, hits in transfer_sides(db, pf).items():
        j = by_id[jid]
        cand = by_journal.get(jid)
        if cand is None:
            cand = Candidate(j, [], {tid for tid, d in decided.get(jid, {}).items() if d == "distinct"})
            out.append(cand)
        for h in hits:
            if h.tx.tx_id not in {t.tx_id for t in cand.imports}:
                cand.imports.append(h.tx)
            account = (j.to_account if h.role == "in" else j.from_account) or ""
            cand.why[h.tx.tx_id] = h.text(account)
            cand.side[h.tx.tx_id] = h.info()
    out.sort(key=lambda c: (c.journal.ts, c.journal.tx_id))
    return out


def decide(db: Any, journal_tx_id: str, import_tx_id: str, decision: str, stamp: Any) -> None:
    """``covered`` (Import-Buchung gilt), ``distinct`` (keine Dublette) oder ``undo``."""
    if decision == "undo":
        db.x("DELETE FROM journal_import_link WHERE journal_tx_id=? AND import_tx_id=?", (journal_tx_id, import_tx_id))
        return
    if decision not in ("covered", "distinct"):
        raise ValueError(decision)
    db.x("INSERT INTO journal_import_link(journal_tx_id, import_tx_id, decision, decided_at) VALUES (?,?,?,?) "
         "ON CONFLICT(journal_tx_id, import_tx_id) DO UPDATE SET decision=excluded.decision, "
         "decided_at=excluded.decided_at", (journal_tx_id, import_tx_id, decision, iso(stamp)))
