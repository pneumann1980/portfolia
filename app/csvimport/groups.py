"""Wirtschaftliche Ereignisgruppen über Quellen (rein lesend).

Ein wirtschaftlicher Vorgang (Kauf, Tausch, Transfer, Ertrag) erscheint oft in mehreren Quellen: Börsen-API,
Börsen-CSV, Steuertool-Export, kuratierter Import, Beleg. Gebucht wird er **einmal**; alle weiteren Quelldatensätze
hängen als Verknüpfung (``tx_link``) mit ihren Werten und ihrer Herkunft an dieser einen Buchung. Diese Sicht zeigt je
Buchung alle Quellen nebeneinander und findet Widersprüche, die keine Einzelzeile zeigt:

* **1:n** – derselbe Quelldatensatz ist mit mehreren Buchungen verknüpft (außer ausdrücklichen Teil-Zuordnungen);
* **n:1 aus derselben Quelle** – eine Buchung trägt mehrere verschiedene Datensätze derselben Quelle in derselben
  Rolle (Doppellieferung der Quelle oder falsche Verknüpfung);
* **Gebühr brutto/netto** – Quellen widersprechen sich, ob die Gebühr im Betrag enthalten oder zusätzlich belastet ist,
  bzw. die Differenz entspricht genau der Gebühr der Quelle, die Buchung führt aber keine Gebühr (Hinweis);
* **Mengenwiderspruch** – eine Quelle nennt eine andere Menge, die sich weder durch die Gebühr noch durch die
  Stellenzahl der Buchung erklären lässt (Quelle genauer als die auf ≥ 2 Nachkommastellen gerundete Buchung);
* **teilweise verknüpft** – nur ein Teil der Zeilen eines Ereignisses ist verknüpft, der Rest steht offen (ein
  Vorgang darf nicht geteilt werden);
* **Doppelbuchung** – dasselbe Ereignis (Kennung bzw. Ereigniszeile derselben Quelle) ist zweimal gebucht.

Es wird nichts geändert; Lösungen laufen über die Prüf-Stapel bzw. die Diagnose (Vorschau, Freigabe, Rückgängig).
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

KINDS = {"1:n": "ein Quelldatensatz an mehreren Buchungen", "n:1": "mehrere Datensätze derselben Quelle an einer "
                                                                   "Buchung",
         "fee_basis": "Gebühr brutto/netto widersprüchlich", "qty": "Mengenwiderspruch zwischen Quellen",
         "partial": "Ereignis nur teilweise verknüpft", "double": "Ereignis doppelt gebucht"}


def _dec(v: Any) -> Decimal | None:
    try:
        return Decimal(str(v)) if v not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


@dataclass
class Member:
    source: str
    role: str
    ext_id: str | None
    record: dict[str, Any]
    batch_id: int | None = None
    row_idx: int | None = None
    link_id: int | None = None


@dataclass
class Group:
    tx_id: str
    booking: dict[str, Any] | None
    members: list[Member] = field(default_factory=list)
    conflicts: list[tuple[str, str]] = field(default_factory=list)  # (Art, Text)

    @property
    def sources(self) -> list[str]:
        return sorted({m.source for m in self.members})


@dataclass
class Conflict:
    kind: str
    text: str
    tx_ids: list[str]
    sources: list[str] = field(default_factory=list)
    batch_id: int | None = None
    event: str | None = None

    @property
    def label(self) -> str:
        return KINDS.get(self.kind, self.kind)


def _booking(ctx: Any, tx_id: str, index: dict[str, Any] | None = None) -> dict[str, Any] | None:
    r = ctx.db.q1("SELECT tx_id, source, status, ts_utc, type, tag, from_account, from_asset, from_qty, to_account, "
                  "to_asset, to_qty, fee_asset, fee_qty, value_eur FROM journal_tx WHERE tx_id=?", (tx_id,))
    if r is not None:
        return {**dict(r), "origin": "journal"}
    if index is None:
        pf = ctx.recorded_portfolio()
        index = {x.tx_id: x for x in (pf.txs if pf is not None else [])}
    t = index.get(tx_id)
    if t is None:
        return None
    return {"tx_id": t.tx_id, "source": t.source or "Import", "status": "active", "ts_utc": t.ts.isoformat(),
            "type": t.type, "tag": t.tag, "from_account": t.from_account, "from_asset": t.from_asset,
            "from_qty": str(t.from_qty) if t.from_qty is not None else None, "to_account": t.to_account,
            "to_asset": t.to_asset, "to_qty": str(t.to_qty) if t.to_qty is not None else None,
            "fee_asset": t.fee_asset, "fee_qty": str(t.fee_qty) if t.fee_qty is not None else None,
            "value_eur": str(t.value_eur) if t.value_eur is not None else None, "origin": t.origin}


def _members(db: Any, tx_id: str) -> list[Member]:
    out = []
    for r in db.q("SELECT id, source, ext_id, role, batch_id, row_idx, record_json FROM tx_link WHERE tx_id=? AND "
                  "status='active' ORDER BY id", (tx_id,)):
        try:
            rec = json.loads(r["record_json"] or "{}")
        except ValueError:
            rec = {}
        out.append(Member(r["source"], r["role"] or "same", r["ext_id"], rec, r["batch_id"], r["row_idx"],
                          int(r["id"])))
    return out


def _leg_qty(rec: dict[str, Any], key: str) -> Decimal | None:
    leg = rec.get(key)
    return _dec(leg[2]) if isinstance(leg, list) and len(leg) > 2 else None


def _precision_only(got: Decimal, want: Decimal) -> bool:
    """Abweichung nur durch die Stellenzahl der Buchung (Quelle genauer, Buchung gerundet bzw. abgeschnitten)? Gilt
    erst ab zwei Nachkommastellen der Buchung – „100“ gegen „100,4“ bleibt ein Widerspruch."""
    exp = want.as_tuple().exponent
    if not isinstance(exp, int) or exp > -2:
        return False
    q = Decimal(1).scaleb(exp)
    return any(abs(got).quantize(q, rounding=r) == abs(want) for r in (ROUND_HALF_EVEN, ROUND_DOWN))


def _own_fee(rec: dict[str, Any], asset: Any) -> Decimal | None:
    fee = rec.get("fee")
    if isinstance(fee, list) and len(fee) > 1 and fee[0] == asset:
        return _dec(fee[1])
    return None


def check_group(g: Group) -> None:
    """Widersprüche innerhalb einer Gruppe (Gebührenbasis, Mengen)."""
    bases = {m.record.get("fee_basis") for m in g.members if m.record.get("fee_basis") in ("extra", "inside")}
    if len(bases) > 1:
        who = ", ".join(sorted(f"{m.source}: {m.record.get('fee_basis')}" for m in g.members
                               if m.record.get("fee_basis") in ("extra", "inside")))
        g.conflicts.append(("fee_basis", f"Quellen widersprechen sich zur Gebühr ({who}) – „extra“ heißt zusätzlich "
                                         "belastet (brutto), „inside“ im Betrag enthalten (netto)."))
    b = g.booking or {}
    for key, bkey in (("out", "from_qty"), ("inn", "to_qty")):
        want = _dec(b.get(bkey))
        if want is None:
            continue
        fee = _dec(b.get("fee_qty")) or Decimal(0)
        for m in g.members:
            if m.role not in ("same", "out" if key == "out" else "in"):
                continue
            got = _leg_qty(m.record, key)
            if got is None or abs(got) == abs(want) or _precision_only(got, want):
                continue  # gleich bzw. nur Rundung der Buchung auf weniger Stellen
            if fee and abs(abs(got) - abs(want)) == abs(fee):
                continue  # Differenz genau die Gebühr: brutto/netto, keine echte Mengenabweichung
            side = "Abgang" if key == "out" else "Zugang"
            leg = m.record.get(key)
            own = _own_fee(m.record, leg[1] if isinstance(leg, list) and len(leg) > 1 else None)
            if own and not fee and abs(abs(got) - abs(want)) == abs(own):
                g.conflicts.append(("fee_basis", f"{m.source} nennt {side} {got} und eine Gebühr {own}; die "
                                                 f"Buchung {want} enthält keine Gebühr – brutto/netto prüfen (Gebühr "
                                                 "ggf. nicht gebucht)."))
                continue
            g.conflicts.append(("qty", f"{m.source} nennt {side} {got}, die Buchung {want} – Differenz "
                                       f"{abs(got) - abs(want)} ist nicht durch die Gebühr erklärt."))


def group(ctx: Any, tx_id: str) -> Group:
    g = Group(tx_id, _booking(ctx, tx_id), _members(ctx.db, tx_id))
    check_group(g)
    return g


def scan(ctx: Any, limit: int = 500) -> list[Conflict]:
    """Alle Gruppenwidersprüche (rein lesend)."""
    db = ctx.db
    out: list[Conflict] = []
    links = db.q("SELECT id, tx_id, source, ext_id, role, record_json FROM tx_link WHERE status='active'")
    pf = ctx.recorded_portfolio() if links else None
    index = {x.tx_id: x for x in (pf.txs if pf is not None else [])}
    by_rec: dict[tuple[str, str], set[str]] = defaultdict(set)
    roles: dict[tuple[str, str], str] = {}
    by_tx: dict[str, list[Any]] = defaultdict(list)
    for r in links:
        by_tx[r["tx_id"]].append(r)
        if r["ext_id"]:
            by_rec[(r["source"], r["ext_id"])].add(r["tx_id"])
            roles[(r["source"], r["ext_id"])] = r["role"] or "same"
    for (src, ext), txs in sorted(by_rec.items()):
        if len(txs) > 1 and roles.get((src, ext)) != "part":
            out.append(Conflict("1:n", f"{src} {ext} ist mit {len(txs)} Buchungen verknüpft ({', '.join(sorted(txs))})",
                                sorted(txs), [src]))
    for tx_id, rs in sorted(by_tx.items()):
        per: dict[tuple[str, str], set[str]] = defaultdict(set)
        for r in rs:
            if r["ext_id"]:
                per[(r["source"], r["role"] or "same")].add(r["ext_id"])
        for (src, role), exts in sorted(per.items()):
            if len(exts) > 1 and role != "part":
                out.append(Conflict("n:1", f"{tx_id} trägt {len(exts)} verschiedene Datensätze von {src} in der Rolle "
                                           f"„{role}“ ({', '.join(sorted(exts)[:4])})", [tx_id], [src]))
        g = Group(tx_id, None, [Member(r["source"], r["role"] or "same", r["ext_id"],
                                       json.loads(r["record_json"] or "{}")) for r in rs])
        g.booking = _booking(ctx, tx_id, index)
        check_group(g)
        for kind, text in g.conflicts:
            out.append(Conflict(kind, f"{tx_id}: {text}", [tx_id], g.sources))
    # Ereignis teilweise verknüpft (offene, nicht entschiedene Zeilen neben verknüpften)
    for r in db.q("SELECT r.batch_id, r.event_key, SUM(r.status='linked') AS linked, "
                  "SUM(r.status IN ('new', 'duplicate', 'invalid', 'unclear') AND COALESCE(r.decision, '') NOT IN "
                  "('skip')) AS open_, GROUP_CONCAT(DISTINCT r.tx_id) AS txs FROM csv_row r JOIN csv_batch b ON "
                  "b.id = r.batch_id WHERE r.event_key IS NOT NULL AND b.status <> 'reverted' GROUP BY r.batch_id, "
                  "r.event_key HAVING linked > 0 AND open_ > 0"):
        txs = sorted(x for x in (r["txs"] or "").split(",") if x)
        out.append(Conflict("partial", f"Ereignis {r['event_key']} (Stapel {r['batch_id']}): {r['linked']} Zeile(n) "
                                       f"verknüpft, {r['open_']} offen – ein Vorgang darf nicht geteilt werden",
                            txs, [], int(r["batch_id"]), r["event_key"]))
    # Doppelbuchung: dieselbe Ereigniszeile derselben Quelle zweimal wirksam gebucht
    for r in db.q("SELECT source, event_key, COALESCE(event_line, 0) AS line, GROUP_CONCAT(tx_id) AS txs, "
                  "COUNT(*) AS n FROM journal_tx WHERE status IN ('active', 'merged') AND event_key IS NOT NULL "
                  "GROUP BY source, event_key, COALESCE(event_line, 0) HAVING n > 1"):
        txs = sorted((r["txs"] or "").split(","))
        out.append(Conflict("double", f"{r['source']} {r['event_key']} (Zeile {r['line']}) ist {r['n']}× gebucht: "
                                      f"{', '.join(txs)}", txs, [r["source"]], event=r["event_key"]))
    return out[:limit]
