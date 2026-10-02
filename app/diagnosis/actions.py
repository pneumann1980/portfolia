"""Korrekturen aus der Diagnose: Plan, Vorschau mit berechneten Auswirkungen, Übernehmen, Rückgängig, „geprüft“.

Grundsätze
* **Nie automatisch.** Jede Korrektur ist eine ausdrückliche Entscheidung des Nutzers für genau einen Befund und genau
  eine gewählte Lösung (Empfehlung oder Alternative, :mod:`app.diagnosis.recommend`).
* **Sichtbar.** Die Vorschau zeigt jede Änderung – ausgeblendete, verknüpfte und neue Buchungen, geänderte
  Zuordnungen – und rechnet die Folgen auf einer Kopie im Speicher durch: Bestände, Einstand, Werte, realisierte
  Ergebnisse und Erträge je Jahr, Steuerwerte des Regelwerks, Bestandsabgleich, Ledger-Hinweise und Befunde danach.
  Die Vorschau schreibt nichts.
* **Reversibel, nichts wird gelöscht.** Korrekturen nutzen die vorhandenen Überlagerungen: Import-Buchung ausblenden
  = Überlagerung „gelöscht“ (``tx_override``), App-Buchung = Status, „im Import enthalten“ = ``journal_import_link``,
  Kursquelle = ``asset_source``, neue Buchungen = App-Buchungen der Quelle „diagnose“. Jede Korrektur speichert den
  Vorher-Zustand je Änderung (``diag_decision``) und lässt sich als Ganzes zurücknehmen; wurde ein Objekt danach
  anderweitig geändert, verweigert „Rückgängig“ statt zu überschreiben.
* **Atomar und aktuell.** Übernehmen berechnet Befund und Plan neu und vergleicht ihn mit der Vorschau (Prüfsumme über
  Plan und Ausgangszustand). Weicht etwas ab, wird nichts geändert. Alle Änderungen laufen in einer Transaktion.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import threading
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.diagnosis.collect import JournalMeta
from app.diagnosis.engine import diagnose, report_for
from app.diagnosis.model import HOLDING_STATUS, Finding, Report, TxRef
from app.diagnosis.recommend import (
    DEPOSIT_TAGS,
    WITHDRAWAL_TAGS,
    Option,
    balance_before,
    facts_for,
    pair_choice,
    recommend,
    tx_short,
)
from app.importer import contract as C
from app.ledger.engine import DUST, REALIZED_KINDS, LedgerResult, run_ledger
from app.ledger.models import Portfolio, Tx
from app.util.timeutil import iso, to_local_date, today_local
from app.web.fmt import eur, qty_exact

log = logging.getLogger(__name__)

SOURCE = "diagnose"
ZERO = Decimal(0)
CENT = Decimal("0.01")
MAX_TAX_YEARS = 8
_LOCK = threading.Lock()
OP_LABEL = {"hide": "Ausblenden", "merge": "Zusammenführen", "cover": "Im Import enthalten", "create": "Neue Buchung",
            "quote": "Kursquelle", "unmap": "Zuordnung entfernen"}


class Conflict(Exception):
    """Zustand hat sich zwischen Plan und Ausführung geändert – nichts wird geändert."""


# ----------------------------------------------------------------------------------------------------
# Plan
# ----------------------------------------------------------------------------------------------------

@dataclass
class Op:
    """Eine Änderung. ``before`` hält den Ausgangszustand (Prüfsumme der Vorschau, Grundlage für „Rückgängig“)."""

    kind: str  # hide | cover | create | quote | unmap
    target: str  # hide/cover: Buchung; quote: Asset; unmap: Symbol; create: Schlüssel im Plan (t0, t1, …)
    label: str
    origin: str = ""  # hide: import | journal
    mode: str = ""  # hide (App-Buchung): deleted | merged; quote: user | override
    link: str = ""  # hide (merged): Schlüssel der neuen Transfer-Buchung; cover: Import-Buchung
    row: dict[str, str] | None = None  # create: neue Buchung (Spalten wie transactions.csv)
    value: str = ""  # quote: CoinGecko-ID; create: Quellbezug (pair_refs)
    before: Any = None
    members: list[str] = field(default_factory=list)  # hide (App-Buchung): Teilbuchungen derselben Gruppe
    ref: TxRef | None = None  # Anzeige: betroffene bzw. neue Buchung
    tx: Tx | None = None  # create: neue Buchung (Vorschau)

    @property
    def badge(self) -> str:
        return OP_LABEL["merge" if self.kind == "hide" and self.mode == "merged" else self.kind]

    def state(self) -> dict[str, Any]:
        return {"kind": self.kind, "target": self.target, "origin": self.origin, "mode": self.mode,
                "link": self.link, "row": self.row, "value": self.value, "before": self.before,
                "members": self.members}


@dataclass
class Plan:
    finding: Finding
    option: Option
    params: dict[str, list[str]]
    ops: list[Op] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    version: str = ""  # Datenstand beim Planen (Buchungen, Überlagerungen, Abgleich, Zuordnungen, aktiver Import)

    @property
    def token(self) -> str:
        """Prüfsumme über Befund, Lösung, Eingaben, Änderungen, Ausgangszustand und Datenstand – stimmt sie beim
        Übernehmen nicht mehr mit der Vorschau überein, wird nichts geändert."""
        payload = json.dumps({"f": self.finding.id, "o": self.option.key, "p": self.params, "v": self.version,
                              "ops": [op.state() for op in self.ops]}, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(payload.encode()).hexdigest()[:32]

    @property
    def touches_quotes(self) -> bool:
        return any(op.kind in ("quote", "unmap") for op in self.ops)


_VERSION_SQL = (
    "SELECT (SELECT COUNT(*) || '/' || COALESCE(MAX(updated_at), '') FROM journal_tx), "
    "(SELECT COUNT(*) || '/' || COALESCE(MAX(updated_at), '') FROM tx_override), "
    "(SELECT COUNT(*) || '/' || COALESCE(MAX(decided_at), '') FROM journal_import_link), "
    "(SELECT COUNT(*) || '/' || COALESCE(MAX(updated_at), '') FROM asset_source WHERE status='active'), "
    "(SELECT COUNT(*) || '/' || COALESCE(MAX(updated_at), '') FROM csv_symbol), "
    "(SELECT COALESCE(MAX(id), 0) FROM imports WHERE status='active')")


def data_version(db: Any) -> str:
    """Datenstand, auf dem Vorschau und Übernehmen beruhen (ändert sich nur bei echten Datenänderungen)."""
    try:
        return "|".join(str(v) for v in tuple(db.q1(_VERSION_SQL)))
    except Exception:  # Tabellen fehlen (sehr alte DB) – dann zählen nur die Objekte selbst
        return ""


def _s(v: Decimal | None) -> str:
    from app.journal.forms import s

    return s(v)


def _dt(t: Tx) -> str:
    return to_local_date(t.ts).isoformat() if t.date_only else t.ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(r: Any) -> dict[str, Any] | None:
    return {k: r[k] for k in r.keys()} if r is not None else None  # noqa: SIM118 - sqlite3.Row


def _strip(msg: str) -> str:
    from app.journal.service import _strip as strip

    return strip(msg)


class _State:
    """Ausgangszustand für den Plan (nur lesend)."""

    def __init__(self, ctx: Any, report: Report) -> None:
        self.ctx = ctx
        self.db = ctx.db
        self.report = report
        self.facts = facts_for(report)
        self.pf: Portfolio = self.facts.pf
        self._raw: dict[str, Tx] | None = None
        self._seq: int | None = None
        self._n = 0

    def tx(self, tx_id: str) -> Tx | None:
        return self.facts.tx(tx_id)

    def raw_import(self, tx_id: str) -> Tx | None:
        if self._raw is None:
            base = self.ctx.base_portfolio()
            self._raw = {t.tx_id: t for t in base.txs} if base is not None else {}
        return self._raw.get(tx_id)

    def ref(self, t: Tx) -> TxRef:
        idx = self.report.index
        if idx is not None:
            return idx.ref(t)
        return TxRef(t.tx_id, t.ts, t.type, t.tag, None, None, None, t.value_eur, t.source, t.source_ref, t.origin)

    def next_seq(self) -> int:
        """Reihenfolge wie nach dem Speichern (App-Buchungen: SEQ_BASE + laufende Nummer)."""
        from app.journal.service import SEQ_BASE

        if self._seq is None:
            last = self.db.scalar("SELECT MAX(id) FROM journal_tx", default=0)
            try:
                last = max(int(last), int(self.db.scalar("SELECT seq FROM sqlite_sequence WHERE name='journal_tx'",
                                                         default=0)))
            except Exception:  # sqlite_sequence fehlt (keine AUTOINCREMENT-Tabelle beschrieben)
                last = int(last)
            self._seq = SEQ_BASE + last
        self._seq += 1
        return self._seq

    def classes(self) -> dict[str, dict[str, Any]]:
        out = {aid: {"asset_class": a.asset_class} for aid, a in self.pf.assets.items()}
        for c in C.ISO_CURRENCIES:
            out.setdefault(c, {"asset_class": "fiat"})
        return out

    def new_key(self) -> str:
        self._n += 1
        return f"t{self._n}"


def _hide(st: _State, t: Tx | None, plan: Plan, mode: str = "deleted", link: str = "") -> Op | None:
    if t is None:
        plan.errors.append("Eine betroffene Buchung ist nicht mehr vorhanden.")
        return None
    from app.journal.service import tx_row

    if t.origin == "import":
        if st.raw_import(t.tx_id) is None:
            plan.errors.append(f"{t.tx_id} ist im aktiven Import nicht enthalten.")
            return None
        ov = _row(st.db.q1("SELECT * FROM tx_override WHERE tx_id=?", (t.tx_id,)))
        if ov is not None and ov["action"] == "delete":
            plan.errors.append(f"{t.tx_id} ist bereits ausgeblendet.")
            return None
        verb = "Geht im Transfer auf" if mode == "merged" else "Ausblenden"
        op = Op("hide", t.tx_id, f"{verb}: Import-Buchung {tx_short(t)}", origin="import", link=link,
                before={"override": ov, "tx": tx_row(t)}, ref=st.ref(t))
    elif t.origin == "journal":
        row = st.db.q1("SELECT * FROM journal_tx WHERE tx_id=?", (t.tx_id,))
        if row is None or row["status"] != "active":
            plan.errors.append(f"{t.tx_id} ist keine aktive App-Buchung mehr.")
            return None
        if row["group_ref"]:
            plan.errors.append(f"{t.tx_id} ist Teil der Buchung {row['group_ref']} – bitte die ganze Buchung im "
                               "Journal bearbeiten.")
            return None
        if row["source"] in ("transfer", SOURCE):
            plan.errors.append(f"{t.tx_id} ist ein abgeglichener Transfer bzw. eine Korrektur aus der Diagnose – bitte "
                               "dort auflösen bzw. zurücknehmen.")
            return None
        members = [r["tx_id"] for r in st.db.q("SELECT tx_id FROM journal_tx WHERE group_ref=? AND status='active' "
                                               "ORDER BY id", (t.tx_id,))]
        verb = "Geht im Transfer auf" if mode == "merged" else "Ausblenden"
        op = Op("hide", t.tx_id, f"{verb}: App-Buchung {tx_short(t)}" + (
            f" (mit {len(members)} Teilbuchung(en))" if members else ""), origin="journal", mode=mode, link=link,
            before={"status": row["status"], "updated_at": row["updated_at"], "tx": tx_row(t)}, members=members,
            ref=st.ref(t))
    else:
        plan.errors.append(f"{t.tx_id} ist eine Sparplan-Buchung – bitte unter Sparpläne verwalten.")
        return None
    plan.ops.append(op)
    return op


def _cover(st: _State, j: Tx | None, i: Tx | None, plan: Plan) -> None:
    if j is None or i is None or j.origin != "journal" or i.origin != "import":
        plan.errors.append("„Im Import enthalten“ braucht eine App-Buchung und eine Import-Buchung.")
        return
    row = st.db.q1("SELECT status, updated_at, group_ref FROM journal_tx WHERE tx_id=?", (j.tx_id,))
    if row is None or row["status"] != "active":
        plan.errors.append(f"{j.tx_id} ist keine aktive App-Buchung mehr.")
        return
    link = _row(st.db.q1("SELECT * FROM journal_import_link WHERE journal_tx_id=? AND import_tx_id=?",
                         (j.tx_id, i.tx_id)))
    n_members = st.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE group_ref=? AND status='active'", (j.tx_id,),
                             default=0)
    if n_members:
        plan.warnings.append(f"{j.tx_id} hat {n_members} Teilbuchung(en) (z. B. Gebühr), die weiter zählen.")
    plan.ops.append(Op("cover", j.tx_id, f"Im Import enthalten: App-Buchung {tx_short(j)} zählt nicht mehr – es gilt "
                                         f"{i.tx_id}", link=i.tx_id,
                       before={"link": link, "status": row["status"], "updated_at": row["updated_at"]},
                       ref=st.ref(j)))


def _create(st: _State, row: dict[str, str], label: str, plan: Plan, refs: str = "") -> Op | None:
    from app.importer.validate import validate_tx_rows

    rep, parsed = validate_tx_rows([{**row, "tx_id": "PF-PRUEFUNG"}], st.classes())
    if rep.errors or not parsed:
        plan.errors += [_strip(m.message) for m in rep.errors]
        return None
    plan.warnings += [_strip(m.message) for m in rep.warnings if m.code != "value_eur" or row["type"] != "transfer"]
    key = st.new_key()
    t = dataclasses.replace(Tx.from_parsed({**parsed[0], "seq": st.next_seq()}), tx_id=f"neu-{key[1:]}",
                            origin="journal", source=SOURCE)
    op = Op("create", key, label, row=row, value=refs, tx=t, ref=st.ref(t))
    plan.ops.append(op)
    return op


def _quote(st: _State, aid: str, raw_coin: str, plan: Plan) -> None:
    from app.prices.sources import OVERRIDE, needs_source, parse_coin_id, source_service

    a = st.pf.assets.get(aid)
    if a is None or not a.is_crypto:
        plan.errors.append(f"{aid}: Kursquelle über CoinGecko nur für Kryptowerte.")
        return
    coin = parse_coin_id(raw_coin)
    if coin is None:
        plan.errors.append("Ungültige CoinGecko-ID – z. B. „threshold-network-token“ oder den Link von coingecko.com "
                           "einfügen.")
        return
    if not source_service(st.ctx)._known(coin):
        plan.errors.append(f"„{coin}“ ist im CoinGecko-Katalog nicht vorhanden.")
        return
    if a.quote_source == "coingecko" and a.quote_id == coin:
        plan.errors.append(f"{aid} ist bereits CoinGecko „{coin}“ zugeordnet.")
        return
    cur = _row(st.db.q1("SELECT * FROM asset_source WHERE asset_id=?", (aid,)))
    eff_base, _ = st.ctx.effective_base()
    raw = eff_base.assets.get(aid) if eff_base is not None else None
    if raw is None:
        from app.journal.service import journal_asset_infos

        raw = journal_asset_infos(st.db).get(aid)
    origin = OVERRIDE if raw is not None and not needs_source(raw) else "user"
    now = f"{a.quote_source} „{a.quote_id}“" if a.quote_id else "keine Kursquelle"
    plan.ops.append(Op("quote", aid, f"Kursquelle {aid}: {now} → CoinGecko „{coin}“"
                                     + (" (ersetzt die Kursquelle des Imports)" if origin == OVERRIDE else ""),
                       mode=origin, value=coin, before={"row": cur, "quote": [a.quote_source, a.quote_id]}))


def _unmap(st: _State, symbol: str, asset: str, plan: Plan) -> None:
    row = _row(st.db.q1("SELECT * FROM csv_symbol WHERE UPPER(symbol)=UPPER(?)", (symbol,)))
    if row is None or row["asset_id"] != asset:
        plan.errors.append(f"Zuordnung {symbol} → {asset} ist nicht (mehr) gespeichert.")
        return
    plan.ops.append(Op("unmap", row["symbol"], f"Zuordnung entfernen: {row['symbol']} → {asset} (wirkt auf künftige "
                                               "Importe und Abrufe)", before={"row": row}))


# -- Lösungen ------------------------------------------------------------------------------------------

def _selected(plan: Plan, name: str, valid: set[str]) -> list[str] | None:
    sel = plan.params.get(name, [])
    if not sel:
        plan.errors.append("Bitte mindestens einen Eintrag auswählen.")
        return None
    if set(sel) - valid:
        plan.errors.append("Die Auswahl passt nicht mehr zum Befund – bitte die Vorschau neu öffnen.")
        return None
    return sel


def _transfer(st: _State, w: Tx | None, d: Tx | None, plan: Plan) -> None:
    if w is None or d is None or w.type != "withdrawal" or d.type != "deposit" or w.from_asset != d.to_asset \
            or not w.from_qty or not d.to_qty:
        plan.errors.append("Abgang und Zugang passen nicht (mehr) zueinander.")
        return
    fq, tq = w.from_qty, d.to_qty
    src = w if w.fee_qty else d
    row = {"datetime": _dt(w), "type": "transfer", "tag": "",
           "from_account": w.from_account or "", "from_asset": w.from_asset or "", "from_qty": _s(fq),
           "to_account": d.to_account or "", "to_asset": d.to_asset or "", "to_qty": _s(min(tq, fq)),
           "fee_asset": src.fee_asset or "" if src.fee_qty else "", "fee_qty": _s(src.fee_qty),
           "fee_eur": _s(src.fee_eur) if src.fee_qty else "", "value_eur": "", "orig_price": "", "orig_ccy": "",
           "related_asset": "",
           "note": f"Interner Transfer {w.from_account} → {d.to_account} (Korrektur aus der Diagnose: {w.tx_id} + "
                   f"{d.tx_id})"}
    if tq > fq:
        plan.warnings.append(f"Zugang {d.tx_id} ({qty_exact(tq)}) ist größer als der Abgang {w.tx_id} "
                             f"({qty_exact(fq)}): Der Transfer bucht {qty_exact(fq)}; die Differenz "
                             f"{qty_exact(tq - fq)} {d.to_asset} entfällt auf {d.to_account}.")
    op = _create(st, row, f"Transfer {qty_exact(fq)} {w.from_asset}: {w.from_account} → {d.to_account}"
                          + (f" (Gebühr {qty_exact(fq - tq)} {w.from_asset})" if fq > tq else ""), plan,
                 refs=f"{w.tx_id},{d.tx_id}")
    if op is not None:
        _hide(st, w, plan, mode="merged", link=op.target)
        _hide(st, d, plan, mode="merged", link=op.target)


def _dec_param(plan: Plan, name: str, label: str) -> Decimal | None:
    raw = (plan.params.get(name) or [""])[0].strip().replace(" ", "")
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        v = Decimal(raw or "0")
    except InvalidOperation:
        plan.errors.append(f"{label}: keine Zahl.")
        return None
    if v < 0 or not v.is_finite():
        plan.errors.append(f"{label}: nicht negativ.")
        return None
    return v


def _adjust(st: _State, plan: Plan) -> None:
    d = plan.finding.data
    acc, aid = d["account"], d["asset"]
    comp = Decimal(d["computed"])
    target_raw = d.get("observed") if d["status"].startswith("extern") else d.get("expected")
    if not target_raw:
        plan.errors.append("Kein Zielbestand vorhanden.")
        return
    target = Decimal(target_raw)
    diff = target - comp
    if not diff:
        plan.errors.append("Keine Differenz.")
        return
    tags = dict(DEPOSIT_TAGS if diff > 0 else WITHDRAWAL_TAGS)
    tag = (plan.params.get("tag") or [""])[0]
    if tag not in tags:
        plan.errors.append("Unbekannte Art der Ausgleichsbuchung.")
        return
    value = _dec_param(plan, "value", "EUR-Wert")
    raw_date = (plan.params.get("date") or [""])[0]
    try:
        day = date.fromisoformat(raw_date)
    except ValueError:
        plan.errors.append("Datum im Format JJJJ-MM-TT angeben.")
        return
    if day > today_local():
        plan.errors.append("Das Datum darf nicht in der Zukunft liegen.")
        return
    if value is None:
        return
    ref = "beobachteten Bestand" if d["status"].startswith("extern") else "Soll des Imports"
    side = ("to", "deposit") if diff > 0 else ("from", "withdrawal")
    row = {"datetime": day.isoformat(), "type": side[1], "tag": tag, "from_account": "", "from_asset": "",
           "from_qty": "", "to_account": "", "to_asset": "", "to_qty": "", "fee_asset": "", "fee_qty": "",
           "fee_eur": "", "value_eur": _s(value), "orig_price": "", "orig_ccy": "", "related_asset": "",
           "note": f"Ausgleichsbuchung aus der Diagnose: {ref} {qty_exact(target)} {aid}, berechnet "
                   f"{qty_exact(comp)} (Differenz {qty_exact(diff)})"}
    row[f"{side[0]}_account"], row[f"{side[0]}_asset"], row[f"{side[0]}_qty"] = acc, aid, _s(abs(diff))
    _create(st, row, f"{'Zugang' if diff > 0 else 'Abgang'} {qty_exact(abs(diff))} {aid} "
                     f"{'auf' if diff > 0 else 'von'} {acc} ({tags[tag]}, {eur(value)})", plan)


def _migration(st: _State, plan: Plan) -> None:
    d = plan.finding.data
    r = st.tx(d.get("receipt") or "")
    if r is None or r.type != "deposit" or not r.to_qty:
        plan.errors.append("Der Zugang des neuen Tokens ist nicht (mehr) vorhanden.")
        return
    acc, old, new = d["account"], d["old"], d["new"]
    q_old = balance_before(st.facts, acc, old, r)
    if q_old <= 0:
        plan.errors.append(f"Vor {r.tx_id} war kein Bestand {old} auf {acc} gebucht.")
        return
    row = {"datetime": _dt(r), "type": "corporate_action", "tag": "migration",
           "from_account": acc, "from_asset": old, "from_qty": _s(q_old), "to_account": acc, "to_asset": new,
           "to_qty": _s(r.to_qty), "fee_asset": "", "fee_qty": "", "fee_eur": "", "value_eur": "", "orig_price": "",
           "orig_ccy": "", "related_asset": "",
           "note": f"Token-Migration {old} → {new} (Korrektur aus der Diagnose; ersetzt den Zugang {r.tx_id})"}
    if _create(st, row, f"Migration {qty_exact(q_old)} {old} → {qty_exact(r.to_qty)} {new} auf {acc}", plan,
               refs=r.tx_id) is not None:
        _hide(st, r, plan)


def build_plan(ctx: Any, report: Report, f: Finding, option_key: str,
               params: Mapping[str, list[str]] | None = None, *, given: bool = False) -> Plan:
    """Plan für eine Lösung. ``given`` = Eingaben wurden abgeschickt (leere Mehrfachauswahl bleibt leer);
    sonst gelten die Vorgaben der Lösung."""
    rec = recommend(report, f)
    opt = rec.option(option_key)
    if opt is None or opt.dismiss:
        dummy = Option(option_key or "?", option_key or "?", "")
        return Plan(f, dummy, {}, errors=["Diese Lösung steht für den Befund nicht (mehr) zur Verfügung."])
    raw = dict(params or {})
    norm: dict[str, list[str]] = {}
    for p in opt.params:
        vals = [str(v).strip() for v in raw.get(p.name, []) if str(v).strip()]
        norm[p.name] = vals if (given or vals) else list(p.default)
    plan = Plan(f, opt, norm, version=data_version(ctx.db))
    st = _State(ctx, report)
    d = f.data
    key = opt.key
    if key in ("hide_weak", "hide_strong"):
        _hide(st, st.tx(d["weak" if key == "hide_weak" else "strong"]), plan)
    elif key == "hide_second":
        sel = _selected(plan, "pairs", {f"{a}|{b}" for a, b in d["pairs"]})
        for a_id, b_id in d["pairs"] if sel else []:
            if f"{a_id}|{b_id}" not in sel:  # type: ignore[operator]
                continue
            a, b = st.tx(a_id), st.tx(b_id)
            if a is None or b is None:
                plan.errors.append(f"{a_id}/{b_id}: Buchung nicht mehr vorhanden.")
                continue
            how, drop, keep = pair_choice(st.facts, a, b)
            if how == "cover":
                _cover(st, drop, keep, plan)
            else:
                _hide(st, drop, plan)
    elif key == "cover":
        imp = st.tx(d["imports"][0])
        for j in d["journals"]:
            _cover(st, st.tx(j), imp, plan)
    elif key == "hide_import":
        for i in d["imports"]:
            _hide(st, st.tx(i), plan)
    elif key == "link":
        sel = _selected(plan, "pairs", {f"{w}|{x}" for w, x in d["pairs"]})
        for w_id, d_id in d["pairs"] if sel else []:
            if f"{w_id}|{d_id}" in sel:  # type: ignore[operator]
                _transfer(st, st.tx(w_id), st.tx(d_id), plan)
    elif key in ("set_quote", "accept_suggestion", "set_quote_custom"):
        coin = {"set_quote": d.get("coin"), "accept_suggestion": d.get("suggestion")}.get(key) or \
            (plan.params.get("coin") or [""])[0]
        if not coin:
            plan.errors.append("Bitte eine CoinGecko-ID angeben.")
        else:
            _quote(st, d["asset"], coin, plan)
    elif key == "unmap":
        sym = (plan.params.get("symbol") or [""])[0]
        if sym.upper() not in {k.upper() for k in d["keys"]}:
            plan.errors.append("Bitte eine der gezeigten Zuordnungen wählen.")
        else:
            _unmap(st, sym, d["asset"], plan)
    elif key == "book_migration":
        _migration(st, plan)
    elif key == "hide_receipt":
        _hide(st, st.tx(d.get("receipt") or ""), plan)
    elif key == "adjust":
        _adjust(st, plan)
    elif key == "hide_custom":
        valid = {c for p in opt.params if p.name == "txs" for c, _l in p.choices}
        for tid in _selected(plan, "txs", valid) or []:
            _hide(st, st.tx(tid), plan)
    else:
        plan.errors.append("Unbekannte Lösung.")
    if not plan.ops and not plan.errors:
        plan.errors.append("Keine Änderung ausgewählt.")
    plan.warnings = list(dict.fromkeys(plan.warnings))
    return plan


# ----------------------------------------------------------------------------------------------------
# Vorschau: Auswirkungen auf einer Kopie im Speicher
# ----------------------------------------------------------------------------------------------------

@dataclass
class PosEffect:
    account: str
    asset: str
    bal: tuple[Decimal, Decimal]
    cost: tuple[Decimal, Decimal]
    value: tuple[Decimal | None, Decimal | None]
    status: tuple[str | None, str | None]
    note: str = ""

    @property
    def delta(self) -> Decimal:
        return self.bal[1] - self.bal[0]


@dataclass
class YearEffect:
    year: int
    realized: tuple[Decimal, Decimal]
    income: tuple[Decimal, Decimal]


@dataclass
class TaxLine:
    year: int
    label: str
    before: Decimal | None
    after: Decimal | None


@dataclass
class Effects:
    positions: list[PosEffect] = field(default_factory=list)
    years: list[YearEffect] = field(default_factory=list)
    tax: list[TaxLine] = field(default_factory=list)
    tax_pack: str = ""
    tax_note: str = ""
    resolved: list[Finding] = field(default_factory=list)
    new: list[Finding] = field(default_factory=list)
    pending: list[Finding] = field(default_factory=list)  # Kursbefunde, die erst nach dem Kursabruf feststehen
    issues_new: list[str] = field(default_factory=list)
    issues_gone: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    target_resolved: bool = False
    target_pending: bool = False  # Kursbefund: entscheidet sich erst mit dem Kursabruf nach dem Übernehmen
    successors: list[Finding] = field(default_factory=list)  # Befund besteht in geänderter Form weiter


def hypothetical(pf: Portfolio, plan: Plan) -> Portfolio:
    remove: set[str] = set()
    for op in plan.ops:
        if op.kind in ("hide", "cover"):
            remove.add(op.target)
            remove.update(op.members)
    add = [op.tx for op in plan.ops if op.kind == "create" and op.tx is not None]
    assets = pf.assets
    quotes = {op.target: op.value for op in plan.ops if op.kind == "quote"}
    if quotes:
        assets = dict(assets)
        for aid, coin in quotes.items():
            assets[aid] = dataclasses.replace(assets[aid], quote_source="coingecko", quote_id=coin)
    return dataclasses.replace(pf, txs=[t for t in pf.txs if t.tx_id not in remove] + add, assets=assets)


def _hyp_snapshot(snap: Any, pf2: Portfolio, led2: LedgerResult, plan: Plan) -> Any:
    journal = dict(snap.journal)
    sources = dict(snap.asset_sources)
    token_keys = {k: list(v) for k, v in snap.token_keys.items()}
    saved = dict(snap.saved_symbols)
    for op in plan.ops:
        if op.kind == "create" and op.tx is not None:
            journal[op.tx.tx_id] = JournalMeta(op.tx.tx_id, SOURCE, "active")
        elif op.kind == "hide" and op.origin == "journal" and op.target in journal:
            journal[op.target] = dataclasses.replace(journal[op.target], status=op.mode or "deleted")
        elif op.kind == "quote":
            sources[op.target] = {**(sources.get(op.target) or {}), "asset_id": op.target, "quote_id": op.value,
                                  "status": "active", "origin": op.mode}
        elif op.kind == "unmap":
            saved.pop(op.target, None)
            for k, lst in token_keys.items():
                token_keys[k] = [x for x in lst if x.upper() != op.target.upper()]
    return dataclasses.replace(snap, pf=pf2, ledger=led2, journal=journal, asset_sources=sources,
                               token_keys=token_keys, saved_symbols=saved)


def _lot_cost(led: LedgerResult) -> dict[tuple[str, str], Decimal]:
    out: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    for lot in led.lots:
        out[(lot.account, lot.asset)] += lot.cost
    return out


def _per_year(led: LedgerResult) -> dict[int, tuple[Decimal, Decimal]]:
    out: dict[int, list[Decimal]] = defaultdict(lambda: [ZERO, ZERO])
    for d in led.disposals:
        if d.kind in REALIZED_KINDS:
            out[d.date.year][0] += d.gain
    for e in led.income:
        out[e.date.year][1] += e.value_eur
    return {y: (v[0], v[1]) for y, v in out.items()}


def _issue_keys(led: LedgerResult) -> dict[tuple[Any, ...], str]:
    return {(i.code, i.tx_id, i.asset, i.account): i.message for i in led.issues if i.severity != "info"}


def _year_sig(led: LedgerResult) -> dict[int, tuple[Any, ...]]:
    out: dict[int, list[Any]] = defaultdict(list)
    for d in led.disposals:
        out[d.date.year].append(("d", d.tx_id, d.asset, d.account, str(d.qty), str(d.proceeds), str(d.cost), d.kind,
                                 tuple((str(p.qty), str(p.cost), str(p.acq_date)) for p in d.parts)))
    for e in led.income:
        out[e.date.year].append(("i", e.tx_id, e.asset, e.account, str(e.qty), str(e.value_eur), e.tag))
    for day, lots in led.lot_snapshots.items():
        out[day.year].append(("s", tuple(sorted((x.asset, x.account, str(x.qty), str(x.cost)) for x in lots))))
    return {y: tuple(sorted(map(repr, v))) for y, v in out.items()}


def _tax_effects(ctx: Any, pf2: Portfolio, eff: Effects) -> None:
    """Steuerwerte des Regelwerks je betroffenem Jahr (Zusammenfassung des Steuerberichts) vorher/nachher."""
    try:
        from app.tax.service import tax_service

        svc = tax_service(ctx)
        pack = svc.pack()
        inp = svc.build_input(pack, svc.options(pack))
        if inp is None:
            return
        led2 = run_ledger(pf2, inp.ledger.options)
        inp2 = dataclasses.replace(inp, pf=pf2, ledger=led2)
        s1, s2 = _year_sig(inp.ledger), _year_sig(led2)
        years = sorted(y for y in set(s1) | set(s2) if s1.get(y) != s2.get(y))
        eff.tax_pack = pack.name
        if len(years) > MAX_TAX_YEARS:
            eff.tax_note = f"{len(years)} Steuerjahre betroffen – gezeigt werden die letzten {MAX_TAX_YEARS}."
            years = years[-MAX_TAX_YEARS:]
        for y in years:
            o = svc.options(pack, y)
            r1, r2 = pack.compute(inp, y, o), pack.compute(inp2, y, o)
            a = [(ln.label, ln.amount) for ln in r1.summary if ln.kind == "eur" and ln.label]
            b = [(ln.label, ln.amount) for ln in r2.summary if ln.kind == "eur" and ln.label]
            labels = list(dict.fromkeys([lb for lb, _ in b] + [lb for lb, _ in a]))
            da, db_ = dict(a), dict(b)
            changed = [TaxLine(y, lb, da.get(lb), db_.get(lb)) for lb in labels if da.get(lb) != db_.get(lb)]
            eff.tax += changed or [TaxLine(y, "Zusammenfassung unverändert (Einzelposten ändern sich)", None, None)]
    except Exception as e:  # Steuerteil darf die Vorschau nie verhindern
        log.warning("Steuerwerte für die Vorschau nicht berechenbar: %s", e)
        eff.tax_note = (f"Steuerwerte nicht berechenbar ({type(e).__name__}) – bitte nach dem Übernehmen im "
                        "Steuerbereich prüfen.")


def preview(ctx: Any, report: Report, plan: Plan) -> Effects:
    """Auswirkungen des Plans – gerechnet auf einer Kopie (Ledger, Diagnose, Steuer); schreibt nichts."""
    snap = report.snapshot
    pf, led = snap.pf, snap.ledger
    eff = Effects()
    pf2 = hypothetical(pf, plan)
    led2 = run_ledger(pf2, led.options)
    rep2 = diagnose(_hyp_snapshot(snap, pf2, led2, plan))
    st1 = {(h.account, h.asset): h.status for h in report.holdings}
    st2 = {(h.account, h.asset): h.status for h in rep2.holdings}
    c1, c2 = _lot_cost(led), _lot_cost(led2)
    quoted = {op.target for op in plan.ops if op.kind == "quote"}
    keys = set(led.balances) | set(led2.balances) | set(c1) | set(c2)
    for k in sorted(keys):
        b1, b2 = led.balances.get(k, ZERO), led2.balances.get(k, ZERO)
        k1, k2 = c1.get(k, ZERO), c2.get(k, ZERO)
        if k[1] not in quoted and abs(b2 - b1) <= DUST and abs(k2 - k1) < CENT and st1.get(k) == st2.get(k):
            continue
        if k[1] in quoted and abs(b1) <= DUST and abs(b2) <= DUST:
            continue
        p = snap.prices.get(k[1])
        price = Decimal(str(p.price_eur)) if p is not None and p.valued else None
        v1 = (b1 * price).quantize(CENT) if price is not None else None
        v2 = (b2 * price).quantize(CENT) if price is not None and k[1] not in quoted else None
        note = "neuer Wert erst nach dem Kursabruf" if k[1] in quoted else ""
        lab1 = HOLDING_STATUS[st1[k]][0] if k in st1 else None
        lab2 = HOLDING_STATUS[st2[k]][0] if k in st2 else None
        eff.positions.append(PosEffect(k[0], k[1], (b1, b2), (k1.quantize(CENT), k2.quantize(CENT)), (v1, v2),
                                       (lab1, lab2), note))
    y1, y2 = _per_year(led), _per_year(led2)
    for y in sorted(set(y1) | set(y2)):
        a, b = y1.get(y, (ZERO, ZERO)), y2.get(y, (ZERO, ZERO))
        if abs(a[0] - b[0]) >= CENT or abs(a[1] - b[1]) >= CENT:
            eff.years.append(YearEffect(y, (a[0].quantize(CENT), b[0].quantize(CENT)),
                                        (a[1].quantize(CENT), b[1].quantize(CENT))))
    i1, i2 = _issue_keys(led), _issue_keys(led2)
    eff.issues_new = [m for k, m in i2.items() if k not in i1][:20]
    eff.issues_gone = [m for k, m in i1.items() if k not in i2][:20]
    ids1 = {f.id for f in report.findings}
    ids2 = {f.id for f in rep2.findings}
    eff.resolved = [f for f in report.findings if f.id not in ids2]
    for f in rep2.findings:
        if f.id in ids1:
            continue
        (eff.pending if f.kind == "price" and set(f.assets) & quoted else eff.new).append(f)
    target = plan.finding
    t_txs = {r.tx_id for r in target.txs} | {r.tx_id for a, b, _w in target.pairs for r in (a, b)}
    eff.successors = [f for f in eff.new if f.kind == target.kind and (
        t_txs & ({r.tx_id for r in f.txs} | {r.tx_id for a, b, _w in f.pairs for r in (a, b)})
        or set(target.positions) & set(f.positions))]
    eff.target_resolved = target.id not in ids2 and not eff.successors
    eff.target_pending = not eff.target_resolved and target.kind == "price" and bool(set(target.assets) & quoted)
    if quoted:
        eff.notes.append("Kurse werden nach dem Übernehmen abgerufen; Wert und Kursbefunde der Position stehen erst "
                         "danach fest. Die Vorschau fragt keine Kurse ab.")
    if any(op.kind == "unmap" for op in plan.ops):
        eff.notes.append("Bestehende Buchungen bleiben unverändert; die Zuordnung fehlt erst bei künftigen Importen "
                         "und Abrufen (der Token geht dann in die Prüfung).")
    if any(op.kind in ("hide", "cover", "create") for op in plan.ops):
        _tax_effects(ctx, pf2, eff)
    return eff


# ----------------------------------------------------------------------------------------------------
# Übernehmen
# ----------------------------------------------------------------------------------------------------

@dataclass
class Result:
    ok: bool = False
    message: str = ""
    errors: list[str] = field(default_factory=list)
    decision_id: int | None = None


def _now() -> str:
    return iso(datetime.now(UTC)) or ""


def fingerprint(f: Finding) -> str:
    """Prüfsumme der Befunddaten: „geprüft“ gilt nur, solange sie gleich bleibt (Abrufzeit zählt nicht)."""
    data = {k: v for k, v in (f.data or {}).items() if k != "observed_at"}
    payload: dict[str, Any] = {"status": f.status, "title": f.title, "data": data,
                               "txs": sorted(r.tx_id for r in f.txs)}
    if not f.data:
        payload["known"] = f.known
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False).encode()
                          ).hexdigest()[:16]


def _exec(c: Any, ctx: Any, js: Any, op: Op, stamp: str, created: dict[str, str]) -> dict[str, Any]:
    """Eine Änderung ausführen (innerhalb der Transaktion); Rückgabe: Protokoll für „Rückgängig“."""
    rec: dict[str, Any] = {"kind": op.kind, "target": op.target, "label": op.label, "origin": op.origin,
                           "mode": op.mode, "before": op.before}
    if op.kind == "hide" and op.origin == "import":
        from app.journal.overrides import import_row

        cur = _row(c.execute("SELECT * FROM tx_override WHERE tx_id=?", (op.target,)).fetchone())
        if cur != op.before["override"]:
            raise Conflict(f"{op.target} wurde inzwischen geändert.")
        if cur is not None:
            c.execute("UPDATE tx_override SET action='delete', updated_at=? WHERE tx_id=?", (stamp, op.target))
        else:
            from app.importer.loader import active_import_id

            orig = ctx.base_portfolio()
            t = next((x for x in orig.txs if x.tx_id == op.target), None) if orig is not None else None
            if t is None:
                raise Conflict(f"{op.target} ist nicht mehr im aktiven Import.")
            c.execute("INSERT INTO tx_override(tx_id, action, base_json, import_id, created_at, updated_at) "
                      "VALUES (?, 'delete', ?, ?, ?, ?)",
                      (op.target, json.dumps(import_row(t), ensure_ascii=False), active_import_id(ctx.db), stamp,
                       stamp))
        js._log(c, "import_delete", op.target, op.before["tx"], {"diagnose": True}, stamp)
    elif op.kind == "hide":
        row = c.execute("SELECT status, updated_at FROM journal_tx WHERE tx_id=?", (op.target,)).fetchone()
        if row is None or row["status"] != "active" or row["updated_at"] != op.before["updated_at"]:
            raise Conflict(f"{op.target} wurde inzwischen geändert.")
        merged_into = created.get(op.link) if op.mode == "merged" else None
        for tid in [op.target, *op.members]:
            c.execute("UPDATE journal_tx SET status=?, merged_into=?, updated_at=? WHERE tx_id=? AND status='active'",
                      (op.mode or "deleted", merged_into, stamp, tid))
            js._log(c, "merge" if op.mode == "merged" else "delete", tid, None, {"diagnose": True}, stamp)
        rec["members"] = op.members
    elif op.kind == "cover":
        cur = _row(c.execute("SELECT * FROM journal_import_link WHERE journal_tx_id=? AND import_tx_id=?",
                             (op.target, op.link)).fetchone())
        if cur != op.before["link"]:
            raise Conflict(f"Abgleich {op.target} ↔ {op.link} wurde inzwischen geändert.")
        c.execute("INSERT INTO journal_import_link(journal_tx_id, import_tx_id, decision, decided_at) VALUES "
                  "(?,?,?,?) ON CONFLICT(journal_tx_id, import_tx_id) DO UPDATE SET decision=excluded.decision, "
                  "decided_at=excluded.decided_at", (op.target, op.link, "covered", stamp))
        js._log(c, "reconcile_covered", op.target, None, {"import_tx_id": op.link, "diagnose": True}, stamp)
        rec["link"] = op.link
    elif op.kind == "create":
        from app.importer.validate import validate_tx_rows

        assert op.row is not None
        classes = {aid: {"asset_class": a.asset_class} for aid, a in ctx.recorded_portfolio().assets.items()}
        for code in C.ISO_CURRENCIES:
            classes.setdefault(code, {"asset_class": "fiat"})
        rep, parsed = validate_tx_rows([{**op.row, "tx_id": "PF-PRUEFUNG"}], classes)
        if rep.errors or not parsed:
            raise Conflict("Neue Buchung ist ungültig: " + "; ".join(_strip(m.message) for m in rep.errors))
        tx_id = js._insert(c, parsed[0], SOURCE, None, stamp, None, SOURCE, pair_refs=op.value or None)
        created[op.target] = tx_id
        rec["target"] = tx_id
        rec["row"] = op.row
    elif op.kind == "quote":
        cur = _row(c.execute("SELECT * FROM asset_source WHERE asset_id=?", (op.target,)).fetchone())
        if cur != op.before["row"]:
            raise Conflict(f"Kursquelle {op.target} wurde inzwischen geändert.")
        c.execute("""INSERT INTO asset_source(asset_id, quote_source, quote_id, status, origin, confidence, reason,
                         checked_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(asset_id) DO UPDATE SET quote_source=excluded.quote_source, quote_id=excluded.quote_id,
                         status='active', origin=excluded.origin, reason=excluded.reason, updated_at=excluded.updated_at
                  """, (op.target, "coingecko", op.value, "active", op.mode, None, "Korrektur aus der Diagnose",
                        stamp, stamp))
        rec["after"] = _row(c.execute("SELECT * FROM asset_source WHERE asset_id=?", (op.target,)).fetchone())
        rec["value"] = op.value
    elif op.kind == "unmap":
        cur = _row(c.execute("SELECT * FROM csv_symbol WHERE symbol=?", (op.target,)).fetchone())
        if cur != op.before["row"]:
            raise Conflict(f"Zuordnung {op.target} wurde inzwischen geändert.")
        c.execute("DELETE FROM csv_symbol WHERE symbol=?", (op.target,))
    else:  # pragma: no cover - Programmierfehler
        raise ValueError(op.kind)
    return rec


def _after(ctx: Any, quotes: bool) -> None:
    from app.journal.service import journal_service

    journal_service(ctx).after_change()
    if quotes:
        from app.prices.sources import source_service

        source_service(ctx).changed()


def apply(ctx: Any, finding_id: str, option_key: str, params: Mapping[str, list[str]], token: str) -> Result:
    """Lösung übernehmen – nur wenn Befund und Plan noch exakt der Vorschau entsprechen (``token``)."""
    with _LOCK:
        report = report_for(ctx)
        f = report.by_id(finding_id)
        if f is None:
            return Result(errors=["Der Befund besteht nicht mehr – die Daten haben sich geändert. Bitte die Diagnose "
                                  "neu öffnen."])
        plan = build_plan(ctx, report, f, option_key, params, given=True)
        if plan.errors:
            return Result(errors=plan.errors)
        if plan.token != token:
            return Result(errors=["Die Vorschau ist nicht mehr aktuell – Daten oder Auswahl haben sich seit der "
                                  "Vorschau geändert. Bitte die Vorschau prüfen und erneut übernehmen."])
        stamp = _now()
        from app.journal.service import journal_service

        js = journal_service(ctx)
        created: dict[str, str] = {}
        try:
            with ctx.db.transaction() as c:
                recs = [_exec(c, ctx, js, op, stamp, created) for op in sorted(
                    plan.ops, key=lambda o: 0 if o.kind == "create" else 1)]
                cur = c.execute(
                    "INSERT INTO diag_decision(finding_id, kind, title, action, option, option_label, params_json, "
                    "ops_json, fingerprint, note, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f.id, f.kind, f.title[:300], "fix", plan.option.key, plan.option.label[:200],
                     json.dumps(plan.params, ensure_ascii=False),
                     json.dumps(recs, ensure_ascii=False, default=str), fingerprint(f), None, "active", stamp))
                did = int(cur.lastrowid)
                js._log(c, "diagnose_apply", f"diagnose:{did}", None,
                        {"finding": f.id, "option": plan.option.key, "changes": len(recs)}, stamp)
        except Conflict as e:
            return Result(errors=[f"{e} Es wurde nichts geändert – bitte die Vorschau neu öffnen."])
        _after(ctx, plan.touches_quotes)
        log.info("Diagnose-Korrektur %s übernommen: %s / %s (%d Änderungen)", did, f.id, plan.option.key, len(recs))
        return Result(ok=True, decision_id=did,
                      message=f"Übernommen: {plan.option.label} ({len(recs)} Änderung{'en' if len(recs) != 1 else ''})."
                              " Rückgängig unter „Entscheidungen und Korrekturen“.")


# ----------------------------------------------------------------------------------------------------
# Rückgängig, geprüft, wieder öffnen
# ----------------------------------------------------------------------------------------------------

def _revert(c: Any, js: Any, rec: dict[str, Any], stamp: str) -> str | None:
    """Eine Änderung zurücknehmen. Rückgabe: Hinweis (bereits zurückgenommen) oder None; Konflikt → Ausnahme."""
    kind, target = rec["kind"], rec["target"]
    if kind == "hide" and rec.get("origin") == "import":
        cur = c.execute("SELECT * FROM tx_override WHERE tx_id=?", (target,)).fetchone()
        if cur is None or cur["action"] != "delete":
            return f"{target} zählt bereits wieder."
        before = rec["before"]["override"]
        if before is None:
            c.execute("DELETE FROM tx_override WHERE tx_id=?", (target,))
        else:
            c.execute("UPDATE tx_override SET action=?, updated_at=? WHERE tx_id=?", (before["action"], stamp, target))
        js._log(c, "import_restore", target, None, {"diagnose": True}, stamp)
    elif kind == "hide":
        mode = rec.get("mode") or "deleted"
        cur = c.execute("SELECT status FROM journal_tx WHERE tx_id=?", (target,)).fetchone()
        if cur is None or cur["status"] != mode:
            return f"{target} wurde inzwischen anderweitig geändert bzw. wiederhergestellt."
        for tid in [target, *(rec.get("members") or [])]:
            c.execute("UPDATE journal_tx SET status='active', merged_into=NULL, updated_at=? WHERE tx_id=? AND "
                      "status=?", (stamp, tid, mode))
            js._log(c, "restore", tid, None, {"diagnose": True}, stamp)
    elif kind == "cover":
        cur = c.execute("SELECT decision FROM journal_import_link WHERE journal_tx_id=? AND import_tx_id=?",
                        (target, rec["link"])).fetchone()
        if cur is None or cur["decision"] != "covered":
            return f"Abgleich {target} ↔ {rec['link']} besteht nicht mehr."
        before = rec["before"]["link"]
        if before is None:
            c.execute("DELETE FROM journal_import_link WHERE journal_tx_id=? AND import_tx_id=?", (target, rec["link"]))
        else:
            c.execute("UPDATE journal_import_link SET decision=?, decided_at=? WHERE journal_tx_id=? AND "
                      "import_tx_id=?", (before["decision"], before["decided_at"], target, rec["link"]))
        js._log(c, "reconcile_undo", target, None, {"import_tx_id": rec["link"], "diagnose": True}, stamp)
    elif kind == "create":
        cur = c.execute("SELECT status FROM journal_tx WHERE tx_id=?", (target,)).fetchone()
        if cur is None or cur["status"] == "reverted":
            return f"{target} ist bereits zurückgenommen."
        if cur["status"] not in ("active", "deleted"):
            raise Conflict(f"{target} wurde inzwischen anderweitig geändert ({cur['status']}).")
        c.execute("UPDATE journal_tx SET status='reverted', updated_at=? WHERE tx_id=?", (stamp, target))
        js._log(c, "revert", target, None, {"diagnose": True}, stamp)
    elif kind == "quote":
        cur = _row(c.execute("SELECT * FROM asset_source WHERE asset_id=?", (target,)).fetchone())
        after = rec.get("after") or {}
        if cur is None or cur.get("quote_id") != after.get("quote_id") or cur.get("status") != "active" \
                or cur.get("origin") != after.get("origin"):
            raise Conflict(f"Die Kursquelle von {target} wurde nach der Korrektur geändert – sie wird nicht "
                           "zurückgesetzt (Einstellungen → Kursquellen).")
        before = rec["before"]["row"]
        if before is None:
            c.execute("DELETE FROM asset_source WHERE asset_id=?", (target,))
        else:
            cols = list(before)
            c.execute(f"INSERT OR REPLACE INTO asset_source({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                      [before[k] for k in cols])
    elif kind == "unmap":
        before = rec["before"]["row"]
        cur = _row(c.execute("SELECT * FROM csv_symbol WHERE symbol=?", (target,)).fetchone())
        if cur is not None:
            if cur["asset_id"] == before["asset_id"]:
                return f"Zuordnung {target} besteht bereits wieder."
            raise Conflict(f"{target} ist inzwischen {cur['asset_id'] or 'ignoriert'} zugeordnet – nicht "
                           "überschrieben.")
        cols = list(before)
        c.execute(f"INSERT INTO csv_symbol({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                  [before[k] for k in cols])
    return None


def undo(ctx: Any, decision_id: int) -> Result:
    """Korrektur als Ganzes zurücknehmen (in umgekehrter Reihenfolge; Konflikt → nichts wird geändert)."""
    with _LOCK:
        row = ctx.db.q1("SELECT * FROM diag_decision WHERE id=?", (decision_id,))
        if row is None or row["action"] != "fix" or row["status"] != "active":
            return Result(errors=["Diese Korrektur ist nicht (mehr) aktiv."])
        recs = json.loads(row["ops_json"] or "[]")
        from app.journal.service import journal_service

        js = journal_service(ctx)
        stamp = _now()
        notes: list[str] = []
        try:
            with ctx.db.transaction() as c:
                for rec in reversed(recs):
                    note = _revert(c, js, rec, stamp)
                    if note:
                        notes.append(note)
                c.execute("UPDATE diag_decision SET status='undone', undone_at=? WHERE id=?", (stamp, decision_id))
                js._log(c, "diagnose_undo", f"diagnose:{decision_id}", None, {"notes": notes}, stamp)
        except Conflict as e:
            return Result(errors=[f"Rückgängig nicht möglich: {e} Es wurde nichts geändert."])
        _after(ctx, any(r["kind"] in ("quote", "unmap") for r in recs))
        log.info("Diagnose-Korrektur %s zurückgenommen", decision_id)
        return Result(ok=True, decision_id=decision_id,
                      message="Korrektur zurückgenommen." + (" Hinweis: " + " ".join(notes) if notes else ""))


def dismiss(ctx: Any, finding_id: str, note: str = "") -> Result:
    """„Geprüft, kein Handlungsbedarf“ – ändert keine Daten; gilt, solange die Befunddaten gleich bleiben."""
    with _LOCK:
        report = report_for(ctx)
        f = report.by_id(finding_id)
        if f is None:
            return Result(errors=["Der Befund besteht nicht mehr – bitte die Diagnose neu öffnen."])
        stamp = _now()
        with ctx.db.transaction() as c:
            c.execute("UPDATE diag_decision SET status='undone', undone_at=? WHERE finding_id=? AND action='dismiss' "
                      "AND status='active'", (stamp, f.id))
            cur = c.execute("INSERT INTO diag_decision(finding_id, kind, title, action, fingerprint, note, status, "
                            "created_at) VALUES (?,?,?,?,?,?,?,?)",
                            (f.id, f.kind, f.title[:300], "dismiss", fingerprint(f), note.strip()[:500] or None,
                             "active", stamp))
        return Result(ok=True, decision_id=int(cur.lastrowid), message=f"Als geprüft markiert: {f.title}")


def reopen(ctx: Any, decision_id: int) -> Result:
    with _LOCK:
        cur = ctx.db.x("UPDATE diag_decision SET status='undone', undone_at=? WHERE id=? AND action='dismiss' AND "
                       "status='active'", (_now(), decision_id))
        if not cur.rowcount:
            return Result(errors=["Diese Markierung ist nicht (mehr) aktiv."])
        return Result(ok=True, decision_id=decision_id, message="Befund wieder geöffnet.")


# ----------------------------------------------------------------------------------------------------
# Anzeige der Entscheidungen
# ----------------------------------------------------------------------------------------------------

@dataclass
class Decision:
    id: int
    finding_id: str
    kind: str
    title: str
    action: str
    option: str | None
    option_label: str | None
    note: str | None
    status: str
    created_at: str
    undone_at: str | None
    fingerprint: str | None
    changes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.status == "active"

    @property
    def created(self) -> datetime | None:
        from app.util.timeutil import parse_iso

        return parse_iso(self.created_at)


def _decision(r: Any) -> Decision:
    try:
        changes = json.loads(r["ops_json"]) if r["ops_json"] else []
    except ValueError:
        changes = []
    return Decision(int(r["id"]), r["finding_id"], r["kind"], r["title"], r["action"], r["option"], r["option_label"],
                    r["note"], r["status"], r["created_at"], r["undone_at"], r["fingerprint"], changes)


def decisions(db: Any, limit: int = 60) -> list[Decision]:
    try:
        rows = db.q("SELECT * FROM diag_decision ORDER BY (status='active') DESC, id DESC LIMIT ?", (limit,))
    except Exception:  # Tabelle fehlt (DB vor Migration 12)
        return []
    return [_decision(r) for r in rows]


def active_dismissals(db: Any) -> dict[str, Decision]:
    try:
        rows = db.q("SELECT * FROM diag_decision WHERE action='dismiss' AND status='active' ORDER BY id")
    except Exception:
        return {}
    return {r["finding_id"]: _decision(r) for r in rows}


def active_fixes(db: Any) -> dict[str, list[Decision]]:
    out: dict[str, list[Decision]] = defaultdict(list)
    try:
        rows = db.q("SELECT * FROM diag_decision WHERE action='fix' AND status='active' ORDER BY id")
    except Exception:
        return {}
    for r in rows:
        out[r["finding_id"]].append(_decision(r))
    return out


