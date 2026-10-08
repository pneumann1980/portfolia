"""Stapelaktionen der Importprüfung: Auswahl, Filter, Vorschau, Ausführung, Protokoll, Rückgängig.

Ablauf: Zeilen auswählen (einzeln, aktuelle Seite, alle gefilterten, Ergebnisgruppe, Vorauswahl „sicher“) → Vorschau
(Wirkung je Zeile, Bestandswirkung, Ausschlüsse mit Grund, offene Fragen) → Ausführen (genau einmal je Vorschau, in
**einer** Transaktion: alles oder nichts) → Protokoll mit dem Vorher-Zustand jeder Änderung (``import_action``) →
Rückgängig (nur, was seitdem unverändert ist).

Sicherheitsregeln (nie abschaltbar): bereits vorhandene Vorgänge (Dublette/Ergänzung) werden nie per Stapel gebucht,
nur verknüpft; „übernehmen“ per Stapel nur für Ergebnis „neu“ ohne Prüfhinweis (Widersprüche, komplexe Fälle und
Vorgänge vor dem Stichtag nur mit ausdrücklicher Bestätigung); Transfer-Paare nur gemeinsam. Verknüpfen ändert keine
Buchung: Die vorhandene Buchung bleibt, wie sie ist; die Zeile wird mit Werten und Herkunft als verknüpfter
Quelldatensatz gespeichert (``tx_link``) und über ``journal_event_alias`` bei künftigen Abrufen bzw. Importen
wiedererkannt.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from app.csvimport import assess as A
from app.csvimport.events import derive_tx_hash, identity_keys, normalize_hash
from app.csvimport.service import _LOCK, STATUS_LABEL, RowCtx, line_key, pair_accepted
from app.util.timeutil import iso, to_local_date

log = logging.getLogger(__name__)

ACTIONS: dict[str, tuple[str, str]] = {
    "suggest": ("Vorschlag anwenden", "Dubletten und Ergänzungen verknüpfen, neue Vorgänge übernehmen – "
                                      "Widersprüche und komplexe Fälle bleiben zur Einzelprüfung"),
    "link": ("Verknüpfen", "nicht buchen – Kennung und Angaben der Quelle bleiben an der vorhandenen Buchung "
                           "erhalten, die Buchung selbst bleibt unverändert"),
    "include": ("Übernehmen", "als neue Buchungen übernehmen"),
    "skip": ("Auslassen", "in diesem Stapel nicht übernehmen (jederzeit änderbar)"),
    "ignore": ("Dauerhaft ignorieren", "bei keinem Abruf mehr vorlegen (je Anbieter-Ereignis)"),
    "reset": ("Vorschlag wiederherstellen", "eigene Auswahl ja/nein zurücksetzen"),
}
EFFECT_LABEL = {"link": "verknüpfen (nicht buchen)", "include": "übernehmen (buchen)", "skip": "auslassen",
                "ignore": "dauerhaft ignorieren", "reset": "Vorschlag wiederherstellen"}
CONFLICTS = {"ts": "Zeitpunkt", "value": "EUR-Wert", "fee": "Gebühr", "qty": "Menge", "asset": "Asset",
             "acct": "Konto", "type": "Art/Einordnung", "hash": "Hash", "struct": "Struktur/Zuordnung"}
GROUPS = {  # Vorschläge der Übersicht (Reihenfolge = Anzeige)
    "dublette": ("Sichere Dubletten verknüpfen", "Identische, eindeutig zugeordnete Buchungen – nicht erneut "
                                                 "buchen, Kennung merken"),
    "ergaenzung": ("Ergänzungen verknüpfen", "Gleicher Vorgang mit zusätzlichen Angaben (Hash, Zeitpunkt, EUR-Wert, "
                                             "Kennung) – Angaben bleiben mit Herkunft erhalten"),
    "neu": ("Neue übernehmen", "Kein Gegenstück im Bestand, ohne Prüfhinweis"),
}
MAX_RAW = 20_000  # Rohdaten je verknüpftem Datensatz (Zeichen)


# ----------------------------------------------------------------------------------------------------
# Gruppen, Filter, Auswahl
# ----------------------------------------------------------------------------------------------------

def group_of(rc: RowCtx) -> str | None:
    """Gruppe der Übersicht: dublette | ergaenzung | neu (sichere Vorschläge) | manuell | None (nichts zu tun)."""
    if not rc.open or rc.status in ("known", "ignored", "before"):
        return None
    m = rc.match
    if not m:
        return "manuell" if rc.status in ("unclear", "invalid", "duplicate") else None
    if m["cat"] in ("dublette", "ergaenzung") and m.get("action") == "link" and m["conf"] in A.SAFE_CONF:
        return m["cat"]
    if m["cat"] == "neu" and m.get("action") == "include" and m["conf"] in A.SAFE_CONF:
        return "neu"
    return "manuell"


def overview(rows: list[RowCtx]) -> dict[str, Any]:
    """Zahlen für die Übersicht „Vorschlag: Importprüfung“."""
    groups: dict[str, int] = dict.fromkeys((*GROUPS, "manuell"), 0)
    cats: dict[str, int] = dict.fromkeys(A.CATS, 0)
    reasons: dict[str, int] = defaultdict(int)
    for rc in rows:
        g = group_of(rc)
        if g is None:
            continue
        groups[g] += 1
        if rc.match:
            cats[rc.match["cat"]] += 1
        if g == "manuell":
            m = rc.match or {}
            if rc.status == "unclear":
                reasons["ungeklärte Vorgangsart"] += 1
            elif rc.status == "invalid":
                reasons["unvollständig (Asset/EUR-Wert)"] += 1
            elif m.get("cat") in ("widerspruch", "komplex"):
                reasons[A.CAT_LABEL[m["cat"]]] += 1
            else:
                reasons["mittlere Sicherheit bzw. Prüfhinweis"] += 1
    before = [rc for rc in rows if rc.open and rc.status == "before"]
    gaps = sum(1 for rc in before if rc.match and rc.match["cat"] == "neu")
    return {"groups": groups, "cats": cats, "total": sum(groups.values()), "reasons": dict(reasons),
            "before": len(before), "before_found": sum(1 for rc in before if rc.dup_of), "before_gaps": gaps,
            "known": sum(1 for rc in rows if rc.open and rc.status == "known"),
            "linked": sum(1 for rc in rows if rc.status == "linked")}


def group_state(rows: list[RowCtx], chosen: set[int]) -> dict[str, tuple[int, int]]:
    """Je Gruppe: (ausgewählt, gesamt)."""
    out: dict[str, list[int]] = {g: [0, 0] for g in (*GROUPS, "manuell")}
    for rc in rows:
        g = group_of(rc)
        if g is not None:
            out[g][1] += 1
            out[g][0] += rc.idx in chosen
    return {g: (v[0], v[1]) for g, v in out.items()}


@dataclass
class Filters:
    status: str = ""
    cat: str = ""  # Ergebnis bzw. Gruppe „manuell“
    conf: str = ""
    dec: str = ""  # open | include | skip
    acc: str = ""
    frm: str = ""
    to: str = ""
    conflict: str = ""

    KEYS = ("status", "cat", "conf", "dec", "acc", "frm", "to", "conflict")

    @classmethod
    def parse(cls, data: Mapping[str, Any], statuses: Iterable[str] = ()) -> Filters:
        def g(k: str) -> str:
            return str(data.get(k) or data.get(f"_{k}") or "").strip()[:80]

        f = cls(**{k: g(k) for k in cls.KEYS})
        if f.status and statuses and f.status not in statuses:
            f.status = ""
        if f.cat not in ("", *A.CATS, "manuell", *GROUPS):
            f.cat = ""
        if f.conf not in ("", *A.CONF):
            f.conf = ""
        if f.dec not in ("", "open", "include", "skip"):
            f.dec = ""
        if f.conflict not in ("", *CONFLICTS):
            f.conflict = ""
        for k in ("frm", "to"):
            try:
                date.fromisoformat(getattr(f, k)) if getattr(f, k) else None
            except ValueError:
                setattr(f, k, "")
        return f

    def params(self) -> dict[str, str]:
        return {k: getattr(self, k) for k in self.KEYS if getattr(self, k)}

    @property
    def extra(self) -> bool:
        """Filter über den Status-Reiter hinaus aktiv?"""
        return any(getattr(self, k) for k in self.KEYS if k != "status")

    def test(self, rc: RowCtx) -> bool:
        st = self.status
        if st == "committed" and rc.status not in ("committed", "merged"):
            return False
        if st and st != "committed" and rc.status != st:
            return False
        m = rc.match or {}
        if self.cat:
            if self.cat == "manuell":
                if group_of(rc) != "manuell":
                    return False
            elif self.cat != m.get("cat"):
                return False
        if self.conf and m.get("conf") != self.conf:
            return False
        if self.dec == "open" and rc.decision is not None:
            return False
        if self.dec in ("include", "skip") and rc.decision != self.dec:
            return False
        if self.acc:
            accs = {rc.rec.account, rc.rec.to_account}
            if rc.row is not None:
                accs |= {rc.row.get("from_account"), rc.row.get("to_account")}
            if self.acc not in accs:
                return False
        if self.frm or self.to:
            if rc.rec.ts_missing:
                return False
            d = to_local_date(rc.ts).isoformat()
            if (self.frm and d < self.frm) or (self.to and d > self.to):
                return False
        if self.conflict:
            return self.conflict in conflict_kinds(rc)
        return True


def conflict_kinds(rc: RowCtx) -> set[str]:
    m = rc.match or {}
    out: set[str] = set()
    for d in m.get("diff") or []:
        f = d.get("f")
        if f in ("out", "inn"):
            out.add("asset" if "anderes Asset" in d.get("t", "") else "qty")
        elif f:
            out.add(f)
    if (m.get("fee") or {}).get("state") in ("conflict", "open"):
        out.add("fee")
    return out


def _summary(db: Any, bid: int) -> dict[str, Any]:
    return json.loads(db.scalar("SELECT summary_json FROM csv_batch WHERE id=?", (bid,)) or "{}")


def selection(db: Any, bid: int, rows: list[RowCtx]) -> set[int]:
    """Aktuelle Auswahl des Stapels; ohne eigene Auswahl die Vorauswahl „sicher verknüpfen“."""
    sel = _summary(db, bid).get("selection")
    present = {rc.idx for rc in rows}
    if sel is None:
        return {rc.idx for rc in rows if group_of(rc) in ("dublette", "ergaenzung")}
    return {int(i) for i in sel} & present


def set_selection(db: Any, bid: int, idxs: Iterable[int] | None) -> None:
    """Auswahl speichern (``None`` = zurück zur Vorauswahl)."""
    with _LOCK:
        summ = _summary(db, bid)
        if idxs is None:
            summ.pop("selection", None)
        else:
            summ["selection"] = sorted({int(i) for i in idxs})
        db.x("UPDATE csv_batch SET summary_json=? WHERE id=?", (json.dumps(summ, ensure_ascii=False), bid))


def change_selection(db: Any, bid: int, rows: list[RowCtx], op: str, *, idxs: Iterable[int] = (), on: bool = True,
                     groups: Iterable[str] = (), filters: Filters | None = None) -> set[int]:
    """Auswahl ändern: ``toggle``/``page`` (Zeilen ``idxs``), ``groups`` (Gruppen der Übersicht genau so setzen),
    ``filtered`` (alle Zeilen der Filter), ``safe`` (Vorauswahl), ``none`` (leeren)."""
    cur = selection(db, bid, rows)
    if op == "safe":
        set_selection(db, bid, None)
        return selection(db, bid, rows)
    if op == "none":
        cur = set()
    elif op in ("toggle", "page"):
        ids = {int(i) for i in idxs} & {rc.idx for rc in rows}
        cur = cur | ids if on else cur - ids
    elif op == "filtered" and filters is not None:
        ids = {rc.idx for rc in rows if filters.test(rc)}
        cur = cur | ids if on else cur - ids
    elif op == "groups":  # genau diese Gruppen (alle anderen abwählen)
        want = set(groups)
        for g in (*GROUPS, "manuell"):
            ids = {rc.idx for rc in rows if group_of(rc) == g}
            cur = cur | ids if g in want else cur - ids
    elif op == "group":  # eine Gruppe an- bzw. abwählen
        ids = {rc.idx for rc in rows if group_of(rc) in set(groups)}
        cur = cur | ids if on else cur - ids
    set_selection(db, bid, cur)
    return cur


# ----------------------------------------------------------------------------------------------------
# Vorschau
# ----------------------------------------------------------------------------------------------------

@dataclass
class Item:
    rc: RowCtx
    act: str | None  # wirksame Aktion – None: ausgeschlossen
    note: str
    target: str | None = None


@dataclass
class Plan:
    action: str
    force: bool
    items: list[Item] = field(default_factory=list)
    impact: list[dict[str, Any]] = field(default_factory=list)  # Bestandswirkung (übernehmen)
    prevented: list[dict[str, Any]] = field(default_factory=list)  # sonst gebuchte Mengen (verknüpfen/auslassen)
    questions: list[tuple[RowCtx, str]] = field(default_factory=list)  # offene Fragen (z. B. Gebühr nicht belegbar)
    fixes: list[tuple[RowCtx, str]] = field(default_factory=list)  # Korrekturvorschläge (nie automatisch)

    @property
    def todo(self) -> list[Item]:
        return [i for i in self.items if i.act]

    @property
    def excluded(self) -> list[Item]:
        return [i for i in self.items if not i.act]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for i in self.todo:
            out[i.act] += 1  # type: ignore[index]
        return dict(out)

    def reasons(self) -> list[tuple[str, int, list[Item]]]:
        by: dict[str, list[Item]] = defaultdict(list)
        for i in self.excluded:
            by[i.note].append(i)
        return sorted(((k, len(v), v[:5]) for k, v in by.items()), key=lambda x: -x[1])

    def fingerprint(self) -> str:
        data = json.dumps(sorted((i.rc.idx, i.act or "", i.target or "") for i in self.items))
        return hashlib.sha256(f"{self.action}|{int(self.force)}|{data}".encode()).hexdigest()[:24]


def _first_issue(m: Mapping[str, Any]) -> str:
    for d in m.get("diff") or []:
        if d.get("sev") == "relevant":
            return d["t"]
    fee = m.get("fee") or {}
    if fee.get("state") == "conflict":
        return fee["t"]
    return A.CAT_HINT.get(m.get("cat", ""), "")


def _decide(action: str, rc: RowCtx, kind: str, force: bool, by_idx: Mapping[int, RowCtx],
            chosen: set[int]) -> tuple[str | None, str, str | None]:
    """(wirksame Aktion, Begründung bzw. Ausschlussgrund, verknüpfte Buchung)."""
    m = rc.match or {}
    if not rc.open:
        return None, f"bereits erledigt ({STATUS_LABEL.get(rc.status, rc.status)})", None
    if rc.status == "ignored":
        return None, "ignoriert", None
    if action == "reset":
        return ("reset", "eigene Auswahl zurücksetzen", None) if rc.decision is not None else \
            (None, "keine eigene Auswahl gesetzt", None)
    if action == "ignore":
        if kind != "sync" or not rc.rec.event_key:
            return None, "nur für Vorgänge einer Datenquelle", None
        if rc.status == "known":
            return None, "bereits vorhanden – nichts zu ignorieren", None
        return "ignore", "wird bei keinem Abruf mehr vorgelegt", None
    if action == "skip":
        if rc.status not in ("new", "duplicate"):
            return None, ("vor dem Stichtag – wird ohnehin nicht übernommen" if rc.status == "before" else
                          f"nicht übernehmbar ({STATUS_LABEL.get(rc.status, rc.status)})"), None
        if not rc.include():
            return None, "wird bereits nicht übernommen", None
        return "skip", "wird nicht übernommen", None
    want = m.get("action") if action == "suggest" else action
    if want == "link":
        target = m.get("target")
        if not target:
            return None, "kein Gegenstück zum Verknüpfen", None
        if m.get("basis") in A.SAME_SOURCE:
            return None, "bereits übernommen bzw. entschieden", None
        if m.get("cat") in ("widerspruch", "komplex") and not force:
            return None, f"{A.CAT_LABEL[m['cat']]} – nur mit „auch abweichende Fälle verknüpfen“", None
        if action == "suggest" and m.get("conf") not in A.SAFE_CONF:
            return None, "mittlere Sicherheit – einzeln prüfen oder ausdrücklich verknüpfen", None
        if rc.status not in ("duplicate", "known", "before", "new"):
            return None, f"nicht verknüpfbar ({STATUS_LABEL.get(rc.status, rc.status)})", None
        return "link", f"verknüpfen mit {target}", target
    if want == "include":
        if rc.row is None or rc.errors:
            return None, "unvollständig: " + (rc.errors[0] if rc.errors else "keine Buchungszeile"), None
        if rc.status not in ("new", "duplicate", "before"):
            return None, f"nicht übernehmbar ({STATUS_LABEL.get(rc.status, rc.status)})", None
        cat = m.get("cat", "neu")
        if cat in ("dublette", "ergaenzung"):
            return None, "bereits vorhanden – verknüpfen statt buchen", None
        if cat != "neu" and not force:
            return None, f"{A.CAT_LABEL.get(cat, cat)} – nur einzeln oder mit ausdrücklicher Bestätigung", None
        if rc.status == "before" and not force:
            return None, "vor dem Stichtag – maßgeblich ist der kuratierte Import (nur mit Bestätigung)", None
        if rc.rec.review and not force:
            return None, "von der Quelle als prüfbedürftig markiert", None
        if (rc.pair_ref or "").startswith("j:"):
            return None, "Transfer mit einer bereits übernommenen Buchung – nur einzeln bestätigen", None
        if rc.transfer_unclear:
            return None, "möglicher Transfer – zuerst bestätigen oder ablehnen", None
        if rc.pair_ref and rc.pair_ref.startswith("b:") and pair_accepted(rc):
            partner = by_idx.get(int(rc.pair_ref[2:]))
            if partner is not None and partner.open and partner.idx not in chosen:
                return None, f"Transfer-Partner (Zeile {partner.line}) nicht ausgewählt – nur gemeinsam", None
        return "include", "wird gebucht", None
    if action == "suggest":
        return None, "einzeln prüfen: " + (_first_issue(m) if m else "ohne Abgleich"), None
    return None, "nicht anwendbar", None


def plan(svc: Any, bid: int, action: str, idxs: Iterable[int], force: bool = False,
         rows: list[RowCtx] | None = None) -> Plan:
    batch = svc.batch(bid)
    rows = rows if rows is not None else svc.rows(bid)
    by_idx = {rc.idx: rc for rc in rows}
    chosen = {i for i in idxs if i in by_idx}
    p = Plan(action=action, force=force)
    for i in sorted(chosen):
        rc = by_idx[i]
        act, note, target = _decide(action, rc, batch["kind"], force, by_idx, chosen)
        p.items.append(Item(rc, act, note, target))
    # Transfer-Paare, deren Partner ausgeschlossen wurde, ebenfalls ausschließen (nur gemeinsam buchen)
    inc = {i.rc.idx for i in p.items if i.act == "include"}
    for it in p.items:
        rc = it.rc
        if it.act == "include" and rc.pair_ref and rc.pair_ref.startswith("b:") and pair_accepted(rc):
            other = int(rc.pair_ref[2:])
            if other in by_idx and by_idx[other].open and other not in inc:
                it.act, it.note = None, "Transfer-Partner nicht übernehmbar – nur gemeinsam"
    _impact(svc, p)
    for it in p.todo:
        m = it.rc.match or {}
        fee = m.get("fee") or {}
        if it.act == "link" and fee.get("state") in ("open", "conflict"):
            p.questions.append((it.rc, fee["t"]))
        for fx in m.get("fix") or []:
            p.fixes.append((it.rc, fx))
    return p


def _row_delta(rc: RowCtx) -> dict[tuple[str, str], Decimal]:
    out: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    r = rc.row
    if r is None:
        return out

    def dec(v: Any) -> Decimal:
        return Decimal(str(v)) if v not in (None, "") else Decimal(0)

    if r.get("from_asset") and r.get("from_qty"):
        out[(r.get("from_account") or "", r["from_asset"])] -= dec(r["from_qty"])
    if r.get("to_asset") and r.get("to_qty"):
        out[(r.get("to_account") or "", r["to_asset"])] += dec(r["to_qty"])
    if r.get("fee_asset") and r.get("fee_qty"):
        out[(r.get("from_account") or r.get("to_account") or "", r["fee_asset"])] -= dec(r["fee_qty"])
    return out


def _impact(svc: Any, p: Plan) -> None:
    inc: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    prev: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for it in p.todo:
        if it.act == "include":
            for k, v in _row_delta(it.rc).items():
                inc[k] += v
        elif it.act in ("link", "skip", "ignore") and it.rc.include():
            for k, v in _row_delta(it.rc).items():
                prev[k] += v
    led = None
    if inc:
        try:
            led = svc.ctx.ledger()
        except Exception:  # Vorschau ohne Bestand ist besser als keine
            led = None
    for (acc, asset), d in sorted(inc.items()):
        if not d:
            continue
        now = led.balances.get((acc, asset), Decimal(0)) if led is not None else None
        p.impact.append({"account": acc, "asset": asset, "now": now, "delta": d,
                         "after": (now + d) if now is not None else None, "negative": now is not None and now + d < 0})
    p.prevented = [{"account": acc, "asset": asset, "delta": d} for (acc, asset), d in sorted(prev.items()) if d]


def new_token() -> str:
    return secrets.token_hex(12)


# ----------------------------------------------------------------------------------------------------
# Ausführen
# ----------------------------------------------------------------------------------------------------

def _record(rc: RowCtx, source: str) -> dict[str, Any]:
    """Werte des verknüpften Quelldatensatzes (bleiben an der Buchung, auch wenn der Stapel verworfen wird)."""
    r, rec = rc.row or {}, rc.rec
    h = normalize_hash(rec.txhash or derive_tx_hash(rec.ext_id))
    raw = rec.raw or None
    if raw is not None and len(json.dumps(raw, default=str)) > MAX_RAW:
        raw = {k: raw[k] for k in ("parser", "operation_id", "operation_type", "credited_at", "time_source")
               if k in raw} | {"gekürzt": True}
    out = {"source": source, "ts": None if rec.ts_missing else iso(rec.ts), "date_only": bool(rec.date_only),
           "type": r.get("type"), "tag": r.get("tag") or rec.tag, "kind": rec.kind,
           "out": [r.get("from_account"), r.get("from_asset"), r.get("from_qty")] if r.get("from_asset") else
           ([rec.account, rec.out_sym, str(rec.out_qty)] if rec.out_sym else None),
           "inn": [r.get("to_account"), r.get("to_asset"), r.get("to_qty")] if r.get("to_asset") else
           ([rec.to_account or rec.account, rec.in_sym, str(rec.in_qty)] if rec.in_sym else None),
           "fee": [r.get("fee_asset"), r.get("fee_qty")] if r.get("fee_asset") else
           ([rec.fee_sym, str(rec.fee_qty)] if rec.fee_sym else None),
           "fee_eur": r.get("fee_eur") or None, "value_eur": r.get("value_eur") or None, "value_src": rc.value_src,
           "fee_basis": A.fee_basis_of(rec), "hash": h, "ext_id": rec.ext_id, "event_key": rec.event_key,
           "aliases": list(rec.aliases or []), "label": rec.label, "note": rec.note, "line": rc.line, "raw": raw}
    return {k: v for k, v in out.items() if v not in (None, "", [], {})}


def _event_one_to_one(rc: RowCtx, rows: list[RowCtx], link_targets: Mapping[int, str]) -> bool:
    """Alle Zeilen des Ereignisses werden mit derselben Buchung verknüpft (oder das Ereignis hat nur diese Zeile) –
    nur dann darf die Ereigniskennung selbst auf die Buchung zeigen."""
    key = rc.rec.event_key
    if not key:
        return True
    lines = [x for x in rows if x.rec.event_key == key]
    target = link_targets.get(rc.idx)
    return all(link_targets.get(x.idx) == target for x in lines)


def execute(svc: Any, bid: int, action: str, idxs: Iterable[int], *, token: str, fingerprint: str,
            force: bool = False, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Stapelaktion ausführen – genau einmal je Vorschau (``token``) und nur, wenn die Vorschau noch gilt
    (``fingerprint``). Alles in einer Transaktion; danach neu bewerten."""
    if action not in ACTIONS:
        return {"errors": ["Unbekannte Aktion."]}
    db = svc.db
    with _LOCK:
        done = db.q1("SELECT id FROM import_action WHERE token=?", (token,))
        if done is not None:
            return {"action_id": int(done["id"]), "repeat": True, "errors": []}
        batch = svc.batch(bid)
        if batch is None or batch["status"] in ("mapping", "reverted"):
            return {"errors": ["Stapel nicht bearbeitbar."]}
        svc.evaluate(bid)
        rows = svc.rows(bid)
        p = plan(svc, bid, action, idxs, force, rows)
        if p.fingerprint() != fingerprint:
            return {"errors": ["Die Vorschau ist nicht mehr aktuell (der Stapel wurde inzwischen neu bewertet oder "
                               "geändert). Bitte die Vorschau erneut öffnen."]}
        if not p.todo:
            return {"errors": ["Nichts auszuführen – alle ausgewählten Zeilen sind ausgeschlossen."]}
        include = [i for i in p.todo if i.act == "include"]
        orig = {rc.idx: rc.decision for rc in rows}  # Vorher-Zustand (vor dem Setzen im Speicher)
        cplan: dict[str, Any] | None = None
        only = {i.rc.idx for i in include}
        if include:
            for i in include:
                i.rc.decision = "include"  # nur im Speicher – geschrieben wird in der Transaktion unten
            cplan = svc.commit_plan(rows, only)
            if cplan.get("errors"):
                return {"errors": ["Übernehmen nicht möglich – nichts wurde geändert:", *cplan["errors"]]}
        source = svc.source_of(batch)
        from app.journal.service import _now

        stamp = _now()
        js = svc.journal
        ops: dict[str, Any] = {"rows": {}, "aliases": [], "decisions": [], "links": [], "txs": []}
        targets = {i.rc.idx: i.target for i in p.todo if i.act == "link" and i.target}
        label = ACTIONS[action][0]
        with db.transaction() as c:
            aid = int(c.execute(
                "INSERT INTO import_action(token, batch_id, action, label, params_json, ops_json, created_at) "
                "VALUES (?,?,?,?,?, '{}', ?)",
                (token, bid, action, label, json.dumps({"rows": sorted(only | set(targets) | {
                    i.rc.idx for i in p.todo}), "force": force, **(params or {})}, ensure_ascii=False),
                 stamp)).lastrowid)  # type: ignore[arg-type]
            seen_events: set[str] = set()
            for it in p.todo:
                rc = it.rc
                prev = {"status": rc.status, "decision": orig.get(rc.idx), "tx_id": rc.tx_id}
                if it.act == "link":
                    assert it.target is not None
                    keys = [k for k in [line_key(source, rc.rec)] if k]
                    if _event_one_to_one(rc, rows, targets):
                        keys += sorted(identity_keys(rc.rec.event_key, rc.rec.aliases, rc.rec.ext_id))
                    for k in dict.fromkeys(keys):
                        if c.execute("INSERT OR IGNORE INTO journal_event_alias(key, tx_id) VALUES (?,?)",
                                     (k, it.target)).rowcount:
                            ops["aliases"].append([k, it.target])
                    lid = int(c.execute(
                        "INSERT INTO tx_link(tx_id, source, ext_id, event_key, role, batch_id, row_idx, action_id, "
                        "record_json, assessment_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (it.target, source, rc.rec.ext_id, rc.rec.event_key, (rc.match or {}).get("role"), bid,
                         rc.idx, aid, json.dumps(_record(rc, source), ensure_ascii=False, default=str),
                         json.dumps(rc.match, ensure_ascii=False, default=str), stamp)).lastrowid)  # type: ignore
                    ops["links"].append(lid)
                    msgs = json.loads(c.execute("SELECT messages FROM csv_row WHERE id=?", (rc.id,)).fetchone()[0]
                                      or "{}")
                    msgs["linked"] = {"to": it.target, "action": aid, "link": lid, "at": stamp}
                    c.execute("UPDATE csv_row SET status='linked', tx_id=?, messages=? WHERE id=?",
                              (it.target, json.dumps(msgs, ensure_ascii=False), rc.id))
                    rc.status, rc.tx_id = "linked", it.target
                    ops["rows"][str(rc.idx)] = {**prev, "set": {"status": "linked", "tx_id": it.target}}
                elif it.act in ("skip", "reset", "include"):
                    dec = {"skip": "skip", "reset": None, "include": "include"}[it.act]
                    c.execute("UPDATE csv_row SET decision=? WHERE id=?", (dec, rc.id))
                    ops["rows"][str(rc.idx)] = {**prev, "set": {"decision": dec}}
                elif it.act == "ignore":
                    key = rc.rec.event_key or ""
                    if key not in seen_events:
                        seen_events.add(key)
                        if c.execute("SELECT 1 FROM event_decision WHERE event_key=?", (key,)).fetchone() is None:
                            c.execute("INSERT INTO event_decision(event_key, decision, reason, batch_id, decided_at) "
                                      "VALUES (?, 'ignore', ?, ?, ?)", (key, "Stapelaktion", bid, stamp))
                            ops["decisions"].append(key)
            res: dict[str, Any] = {"created": 0, "transfers": 0, "merged": 0, "tx_ids": []}
            if cplan is not None:
                res = svc.commit_apply(c, batch, rows, cplan, only, stamp)
                ops["txs"] = res["tx_ids"]
            summary = {"counts": p.counts(), "excluded": len(p.excluded), "created": res["created"],
                       "transfers": res["transfers"],
                       "impact": [{k: (str(v) if isinstance(v, Decimal) else v) for k, v in x.items()}
                                  for x in p.impact[:50]]}
            c.execute("UPDATE import_action SET ops_json=?, summary_json=? WHERE id=?",
                      (json.dumps(ops, ensure_ascii=False), json.dumps(summary, ensure_ascii=False), aid))
            js._log(c, "import_batch", f"csv:{bid}", None, {"action": action, "id": aid, **p.counts()}, stamp)
        svc.evaluate(bid)
        svc.refresh_status(bid)
        set_selection(db, bid, selection(db, bid, svc.rows(bid)) - {i.rc.idx for i in p.todo})
    js.after_change()
    log.info("Stapelaktion %s in Prüf-Stapel %s: %s", action, bid, p.counts())
    return {"action_id": aid, "counts": p.counts(), "created": res["created"], "errors": []}


def safe_link_rows(rows: list[RowCtx]) -> set[int]:
    """Stufe A: Zeilen, deren Identität mit einer vorhandenen Buchung technisch feststeht (gleiche Kennung bzw.
    Blockchain-Transaktion, Sicherheit „sicher“, Ergebnis Dublette/Ergänzung, ohne offene Gebührenfrage) – eine
    Verknüpfung ergänzt nur Herkunft und Kennungen, ändert keine Mengen, Gebühren, Lots oder Kostenbasis."""
    out: set[int] = set()
    for rc in rows:
        m = rc.match or {}
        if not rc.open or rc.status not in ("duplicate", "known") or rc.decision is not None:
            continue  # eigene Entscheidung des Nutzers hat Vorrang
        if m.get("conf") != "sicher" or m.get("cat") not in ("dublette", "ergaenzung") or not m.get("target"):
            continue
        if m.get("basis") not in A.IDENTITY or m.get("basis") in A.SAME_SOURCE:
            continue
        if (m.get("fee") or {}).get("state") in ("open", "conflict"):
            continue
        out.add(rc.idx)
    return out


def auto_link(svc: Any, bid: int) -> int:
    """Sichere technische Verknüpfungen eines Prüf-Stapels automatisch ausführen (protokolliert als Stapelaktion,
    rückgängig wie jede andere). Idempotent: bereits verknüpfte Zeilen sind erledigt. Rückgabe: Anzahl."""
    rows = svc.rows(bid)
    idxs = safe_link_rows(rows)
    if not idxs:
        return 0
    p = plan(svc, bid, "link", idxs, rows=rows)
    ok = {i.rc.idx for i in p.todo if i.act == "link"} - {rc.idx for rc, _t in p.questions}
    if not ok:
        return 0
    if ok != {i.rc.idx for i in p.todo}:
        p = plan(svc, bid, "link", ok)
    res = execute(svc, bid, "link", ok, token=new_token(), fingerprint=p.fingerprint(),
                  params={"auto": True, "stage": "A"})
    if res.get("errors"):
        log.info("Automatische Verknüpfung in Stapel %s übersprungen: %s", bid, "; ".join(res["errors"]))
        return 0
    return int((res.get("counts") or {}).get("link", 0))


def _revert_txs(c: Any, svc: Any, bid: int, tx_ids: list[str], stamp: str) -> int:
    """Buchungen einer Stapelaktion zurücknehmen (wie „Import rückgängig“, aber nur diese)."""
    js = svc.journal
    own = [t for t in tx_ids if t]
    own_set = set(own)
    transfers = [t for t in c.execute("SELECT * FROM journal_tx WHERE source='transfer' AND status IN "
                                      "('active','deleted')")
                 if t["tx_id"] in own_set or own_set & {x.strip() for x in (t["pair_refs"] or "").split(",")}]
    for t in transfers:
        js.unpair_in(c, t, stamp, keep=frozenset({bid}))
    n = 0
    for tx in own:
        n += c.execute("UPDATE journal_tx SET status='reverted', merged_into=NULL, updated_at=? WHERE tx_id=? AND "
                       "status <> 'reverted' AND source <> 'transfer'", (stamp, tx)).rowcount
    for i in range(0, len(own), 500):
        part = own[i:i + 500]
        c.execute(f"UPDATE csv_row SET status='new', tx_id=NULL WHERE batch_id=? AND status IN ('committed','merged') "
                  f"AND tx_id IN ({','.join('?' * len(part))})", [bid, *part])
    left = c.execute("SELECT COUNT(*) FROM journal_tx WHERE batch_id=? AND source <> 'transfer' AND status IN "
                     "('active','merged')", (bid,)).fetchone()[0]
    c.execute("UPDATE csv_batch SET status=?, updated_at=? WHERE id=? AND status IN ('partial','committed')",
              ("partial" if left else "preview", stamp, bid))
    js._log(c, "csv_revert", f"csv:{bid}", None, {"reverted": n, "transfers": len(transfers), "scope": "action"},
            stamp)
    return n


def undo(svc: Any, action_id: int) -> dict[str, Any]:
    """Stapelaktion rückgängig machen. Zurückgesetzt wird nur, was seitdem unverändert ist; Buchungen werden wie
    beim Rückgängigmachen eines Imports zurückgenommen (Status „rückgängig gemacht“, nicht gelöscht)."""
    db = svc.db
    with _LOCK:
        a = db.q1("SELECT * FROM import_action WHERE id=?", (action_id,))
        if a is None or a["status"] != "active":
            return {"errors": ["Aktion nicht gefunden oder bereits rückgängig gemacht."]}
        bid = int(a["batch_id"])
        ops = json.loads(a["ops_json"] or "{}")
        from app.journal.service import _now

        stamp = _now()
        restored = changed = reverted = 0
        exists = svc.batch(bid) is not None
        with db.transaction() as c:
            if ops.get("txs"):
                reverted = _revert_txs(c, svc, bid, ops["txs"], stamp)
            for key, tx in ops.get("aliases") or []:
                c.execute("DELETE FROM journal_event_alias WHERE key=? AND tx_id=?", (key, tx))
            for key in ops.get("decisions") or []:
                c.execute("DELETE FROM event_decision WHERE event_key=? AND batch_id=? AND decided_at=?",
                          (key, bid, a["created_at"]))
            for lid in ops.get("links") or []:
                c.execute("UPDATE tx_link SET status='undone', undone_at=? WHERE id=? AND status='active'",
                          (stamp, lid))
            for idx, st in (ops.get("rows") or {}).items() if exists else ():
                r = c.execute("SELECT id, status, decision, tx_id, messages FROM csv_row WHERE batch_id=? AND idx=?",
                              (bid, int(idx))).fetchone()
                if r is None:
                    changed += 1
                    continue
                want = st.get("set") or {}
                if "status" in want and (r["status"], r["tx_id"]) != (want["status"], want.get("tx_id")):
                    changed += 1
                    continue
                if "decision" in want and r["decision"] != want["decision"]:
                    changed += 1
                    continue
                msgs = json.loads(r["messages"] or "{}")
                msgs.pop("linked", None)
                status = st["status"] if "status" in want else r["status"]
                tx_id = st.get("tx_id") if "status" in want else r["tx_id"]
                c.execute("UPDATE csv_row SET status=?, decision=?, tx_id=?, messages=? WHERE id=?",
                          (status, st.get("decision"), tx_id, json.dumps(msgs, ensure_ascii=False), r["id"]))
                restored += 1
            c.execute("UPDATE import_action SET status='undone', undone_at=? WHERE id=?", (stamp, action_id))
            svc.journal._log(c, "import_batch_undo", f"csv:{bid}", None,
                             {"id": action_id, "restored": restored, "changed": changed, "reverted": reverted}, stamp)
        if exists:
            svc.evaluate(bid)
            svc.refresh_status(bid)
    svc.journal.after_change()
    return {"restored": restored, "changed": changed, "reverted": reverted, "batch_id": bid, "errors": []}


# ----------------------------------------------------------------------------------------------------
# Abfragen
# ----------------------------------------------------------------------------------------------------

def actions(db: Any, bid: int) -> list[dict[str, Any]]:
    out = []
    for r in db.q("SELECT * FROM import_action WHERE batch_id=? ORDER BY id DESC LIMIT 50", (bid,)):
        d = dict(r)
        d["summary"] = json.loads(r["summary_json"] or "{}")
        out.append(d)
    return out


def links_for(db: Any, tx_ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    """Aktive verknüpfte Quelldatensätze je Buchung (Herkunft der zusätzlichen Angaben)."""
    ids = sorted(set(tx_ids))
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for r in db.q(f"SELECT * FROM tx_link WHERE status='active' AND tx_id IN ({','.join('?' * len(part))}) "
                      f"ORDER BY id", part):
            d = dict(r)
            d["record"] = json.loads(r["record_json"] or "{}")
            d["assessment"] = json.loads(r["assessment_json"] or "{}")
            out[r["tx_id"]].append(d)
    return dict(out)


__all__ = ["ACTIONS", "CONFLICTS", "EFFECT_LABEL", "GROUPS", "Filters", "Plan", "actions", "change_selection",
           "execute", "group_of", "group_state", "links_for", "new_token", "overview", "plan", "selection",
           "set_selection", "undo"]
