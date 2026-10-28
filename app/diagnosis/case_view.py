"""Visuelle Fallklärung – lesender Darstellungsadapter.

Projiziert einen Klärungsfall (:class:`app.diagnosis.clarify.Case`) mit dem Bericht (:class:`Report`) und den
vorhandenen Buchungen, Quellenanhängen und Bestandsnachweisen in ein kleines, deterministisches Modell: **benannte
Konten/Wallets als feste Anker**, **Vorgänge als Verbindungen**, **Quellen als Belege** (nie als zusätzliche
Geldbewegungen), **Vorschläge und offene Fragen** getrennt von Gebuchtem.

Der Adapter berechnet nichts selbst – keine Bestände, Treffer, Gebühren oder Wirkungen. Mengen kommen aus den
Buchungen, Bestände aus Ledger und Nachweisen, Wirkungen aus der gemeinsamen Simulation des Falls. Er ändert keinen
Zustand (nur Lesen) und ruft nichts extern ab.

Aussagegrenzen (bewusst):

* Anbieter, Kontoname, Unterkonto und Netzwerk erscheinen nur, soweit sie belegt sind (Datenquelle bzw. Nachweis);
  sonst steht „nicht bekannt“. Eine Wallet-Anwendung legt das Netzwerk nicht fest.
* Ein fehlendes Gegenkonto ist **unbekannt** – daraus folgt weder eine eigene Wallet noch eine externe Auszahlung.
* „Keine Gebühr geliefert“ und „belegte Gebühr 0“ sind verschiedene Aussagen.
* Bestände unterschiedlicher Zeitpunkte werden nie als unmittelbare Abweichung verglichen.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from app.db import fallback
from app.diagnosis import clarify as K
from app.diagnosis.model import Finding, Report, TxRef
from app.util.timeutil import fmt_de_datetime, local_tz
from app.web.fmt import qty_exact

log = logging.getLogger(__name__)

ZERO = Decimal(0)
MAX_MOVES = 24  # Vorgänge je Darstellung (darüber: gekürzt und ausdrücklich so gekennzeichnet)
MAX_SOURCES = 6
MAX_OPEN_QUESTIONS = 4
TYPE_TEXT = {"buy": "Kauf", "sell": "Verkauf", "trade": "Tausch", "deposit": "Einzahlung", "withdrawal": "Auszahlung",
             "transfer": "Übertrag", "corporate_action": "Kapitalmaßnahme"}
SCOPE_TEXT = {"spot": "Spot", "funding": "Funding", "derivatives": "Derivate/Futures", "combined": "zusammengefasst"}
# Zustand einer Verbindung (Text; nie nur Farbe) und Linienart
STATE = {"booked": ("gebucht", "solid"), "proposed": ("Vorschlag – nicht gebucht", "dashed"),
         "removed": ("entfällt in dieser Simulation", "struck"), "created": ("neu in dieser Simulation", "dashed"),
         "open": ("offen", "dotted")}


@dataclass
class Fact:
    """Eine Mengenangabe an einem Ort – mit Zeitpunkt, Herkunft und Umfang (nie ohne diese Angaben vergleichbar)."""

    label: str
    qty: str | None  # None = unbekannt
    asset: str
    at: str = ""
    source: str = ""
    scope: str = ""
    note: str = ""


@dataclass
class Place:
    pid: str
    mark: str  # A, B, C … (Wiedererkennung ohne Farbe)
    key: str  # stabile interne Identität (Konto in Portfolia); der Anzeigename ist davon getrennt
    parts: list[str]  # Anbieter · Kontoname · Unterkonto/Netzwerk – nur belegte Teile
    kind: str  # exchange | wallet | account | unknown
    kind_label: str
    own: bool | None  # True = eigenes Konto; None = Zugehörigkeit unbekannt
    network: str = ""
    network_basis: str = ""
    sub: str = ""
    sub_basis: str = ""
    addresses: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    note: str = ""  # Unterscheidungsmerkmal bei gleichlautendem Anzeigenamen
    facts: list[Fact] = field(default_factory=list)
    unknown: bool = False

    @property
    def name(self) -> str:
        return " · ".join(self.parts)

    @property
    def addresses_short(self) -> list[str]:
        return [short_addr(a) for a in self.addresses]


@dataclass
class Leg:
    asset: str
    label: str  # Name (Asset-ID)
    identity: str  # Asset-ID, Netzwerk/Contract – soweit belegt
    qty: str  # exakt, deutsches Format


@dataclass
class Fee:
    text: str
    place: str  # pid des belasteten Orts ("" = unbekannt)
    asset: str = ""
    qty: str | None = None
    basis: str = ""  # zusätzlich gebucht | in der Differenz enthalten | belegt 0 | keine Gebühr geliefert


@dataclass
class SourceRef:
    label: str
    ext_id: str = ""
    role: str = ""
    batch_url: str = ""
    conflicts: list[str] = field(default_factory=list)


@dataclass
class Move:
    mid: str  # stabil: m-<Buchung> bzw. m-<Buchung>-<Buchung> (Vorschlag)
    state: str  # booked | proposed | removed | created | open
    tx_ids: list[str]
    type_label: str
    src: str  # pid
    dst: str  # pid
    out: Leg | None = None
    inn: Leg | None = None
    fee: Fee | None = None
    ts_out: str = ""
    ts_in: str = ""
    reason: list[str] = field(default_factory=list)  # konkrete Begründung der Zuordnung (Belege/Widersprüche)
    sources: list[SourceRef] = field(default_factory=list)
    sources_more: int = 0
    conflicts: list[str] = field(default_factory=list)
    technical: dict[str, Any] = field(default_factory=dict)
    alt_of: str = ""  # mid der Verbindung, zu der dies eine ausdrücklich mögliche Alternative ist
    edit_url: str = ""
    group_url: str = ""
    value_text: str = ""

    @property
    def status_text(self) -> str:
        return STATE[self.state][0]

    @property
    def line(self) -> str:
        return STATE[self.state][1]


@dataclass
class NextAction:
    key: str  # hyp | stock | research | compare | revision | partial | blocked | scope
    label: str
    why: str
    mode: str  # automatisch | bestaetigung | information
    href: str = ""
    hyp_id: str = ""


STOCK_TEXT = {
    "alle_erfuellt": "Alle hinterlegten Bestandsnachweise werden erfüllt.",
    "teilweise": "Hinterlegte Bestandsnachweise werden nur teilweise erfüllt.",
    "widerspruch": "Widerspricht einem hinterlegten Bestandsnachweis.",
    "unberuehrt": "Hinterlegte Nachweise bleiben unverändert abweichend – die Simulation erklärt die Differenz nicht.",
    "kein_nachweis": "Kein Bestandsnachweis vorhanden – die Wirkung auf den Bestand lässt sich nicht überprüfen.",
}


@dataclass
class Impact:
    """Wirkung einer simulierten Hypothese (aus der gemeinsamen Simulation des Falls, nichts gebucht)."""

    hyp_id: str
    ok: bool
    reasons: list[str]
    positions: list[dict[str, Any]]  # Ort, Asset, vorher, nachher, Änderung, Wert, Kursangabe
    tax: list[dict[str, str]]  # Jahr: realisiertes Ergebnis / Einkünfte vorher → nachher
    lots: list[tuple[str, str, str, str]]
    stock_text: str
    stock_diffs: list[str]
    notes: list[str]
    price_note: str


@dataclass
class CaseView:
    key: str
    simulated: bool
    mode_text: str
    hyp_id: str
    places: list[Place]
    moves: list[Move]
    questions: list[str]
    action: NextAction | None
    quality: dict[str, tuple[str, str]]  # getrennte Zustände: Datenqualität | Importabdeckung | offene Konflikte
    selected: dict[str, Any] | None = None
    truncated: int = 0
    derivatives: list[str] = field(default_factory=list)
    revisions: list[dict[str, str]] = field(default_factory=list)
    impact: Impact | None = None

    def place(self, pid: str) -> Place | None:
        return next((p for p in self.places if p.pid == pid), None)


# ----------------------------------------------------------------------------------------------------
# Hilfen
# ----------------------------------------------------------------------------------------------------

def short_addr(a: str) -> str:
    a = (a or "").strip()
    return a if len(a) <= 14 else f"{a[:6]}…{a[-4:]}"


def _pid(prefix: str, key: str) -> str:
    return f"{prefix}-{hashlib.sha256(key.encode()).hexdigest()[:8]}"


def _ts(ts: datetime | None, date_only: bool = False) -> str:
    if ts is None:
        return ""
    if date_only:
        return ts.astimezone(local_tz()).strftime("%d.%m.%Y") + " (ohne Uhrzeit)"
    return f"{fmt_de_datetime(ts)} {ts.astimezone(local_tz()).tzname()}"


def _q(v: Any) -> str:
    return qty_exact(v)


_GENERIC_BROKER = ("andere", "unbekannt", "unknown", "sonstige", "other")


def _generic_broker(name: str) -> bool:
    """Sammelbezeichnungen des kuratierten Imports (z. B. „Andere Börse“) sind kein belegter Anbieter."""
    return any(w in name.casefold() for w in _GENERIC_BROKER)


class _Names:
    """Orte (Konten/Wallets) mit belegten Eigenschaften (Datenquellen, Portfolio, Nachweise, Zusammenführungen)."""

    def __init__(self, ctx: Any, report: Report) -> None:
        self.ctx, self.report = ctx, report
        snap = report.snapshot
        self.pf = snap.pf if snap is not None else None
        self.sources = {}  # Konto → [SourceState]
        for s in (snap.sources if snap is not None else []):
            self.sources.setdefault(s.account, []).append(s)
        self.refs = snap.references if snap is not None else []
        self.aliases: dict[str, list[str]] = {}
        try:
            for r in ctx.db.q("SELECT alias_account, target_account FROM account_merge WHERE status='active'"):
                self.aliases.setdefault(r["target_account"], []).append(r["alias_account"])
        except Exception as e:  # Tabelle fehlt (sehr alte Datenbank)
            log.debug("Kontozusammenführungen nicht lesbar: %s", e)
        self.places: dict[str, Place] = {}
        self._unknown: dict[str, Place] = {}

    def place(self, account: str) -> Place:
        p = self.places.get(account)
        if p is not None:
            return p
        srcs = self.sources.get(account, [])
        info = self.pf.accounts.get(account) if self.pf is not None else None
        provider = ""
        kind, kind_label, network, network_basis = "account", "Konto", "", ""
        addrs: list[str] = []
        if srcs:
            s0 = srcs[0]
            provider = s0.provider_label if s0.kind == "exchange" else ""
            kind = s0.kind
            kind_label = {"exchange": "Börse", "wallet": "Wallet"}.get(s0.kind, "Konto")
            if s0.kind == "wallet":
                network, network_basis = s0.provider_label, "laut Datenquelle (Chain)"
            addrs = [a for s in srcs for a in s.addresses]
        elif info is not None and info.broker and not _generic_broker(info.broker):
            provider = info.broker
        sub_refs = sorted({(r.subaccount or "", r.scope or "") for r in self.refs if r.account == account
                           and (r.subaccount or r.scope)})
        sub, sub_basis = "", ""
        if len(sub_refs) == 1:
            sub = " / ".join(x for x in (SCOPE_TEXT.get(sub_refs[0][1], sub_refs[0][1]), sub_refs[0][0]) if x)
            sub_basis = "laut Bestandsnachweis"
        parts = [x for x in (provider if provider.casefold() not in account.casefold() else "", account, sub or network)
                 if x]
        p = Place(pid=_pid("p", account), mark="", key=account, parts=parts or [account], kind=kind,
                  kind_label=kind_label, own=True, network=network, network_basis=network_basis, sub=sub,
                  sub_basis=sub_basis, addresses=list(dict.fromkeys(addrs)),
                  aliases=sorted(self.aliases.get(account, [])))
        self.places[account] = p
        return p

    def unknown(self, why: str, tx_id: str) -> Place:
        """Je Vorgang ein eigener unbekannter Ort – mehrere „unbekannt“ sind nicht dieselbe Gegenpartei."""
        key = f"{why}|{tx_id}"
        p = self._unknown.get(key)
        if p is None:
            p = Place(pid=_pid("u", key), mark="?", key="?" + key, parts=[why], kind="unknown",
                      kind_label="Zugehörigkeit unbekannt", own=None, unknown=True)
            self._unknown[key] = p
        return p

    def finish(self) -> list[Place]:
        """Marken vergeben und gleichlautende Anzeigenamen unterscheidbar machen (getrennte Konten bleiben getrennt)."""
        ordered = list(self.places.values())
        for i, p in enumerate(ordered):
            p.mark = chr(ord("A") + i) if i < 26 else str(i + 1)
        seen: dict[str, list[Place]] = {}
        for p in ordered:
            seen.setdefault(p.name.casefold(), []).append(p)
        for group in seen.values():
            if len(group) > 1:
                for n, p in enumerate(group, 1):
                    p.note = (f"Adresse {short_addr(p.addresses[0])}" if p.addresses else f"gleichlautender Name, "
                              f"getrenntes Konto Nr. {n}")
        return [*ordered, *self._unknown.values()]


def _legs(idx: Any, r: TxRef, holdings: dict[tuple[str, str], Any]) -> tuple[Leg | None, Leg | None]:
    def leg(x: tuple[str, str, Decimal] | None) -> Leg | None:
        if x is None:
            return None
        acc, aid, qty = x
        h = holdings.get((acc, aid))
        ident = (getattr(h, "identity", "") or aid) if h is not None else aid
        return Leg(asset=aid, label=idx.label(aid), identity=ident, qty=_q(qty))

    return leg(r.out), leg(r.inn)


def _fee(idx: Any, names: _Names, tx: Any, r: TxRef) -> Fee:
    """Gebühr am belasteten Konto; ``None`` heißt „keine Gebühr geliefert“ – nie „kostenlos“."""
    acc = (tx.from_account or tx.to_account or "") if tx is not None else ""
    pid = names.place(acc).pid if acc else ""
    if tx is None or tx.fee_qty is None or not tx.fee_asset:
        implicit = None
        if r.out and r.inn and r.out[1] == r.inn[1] and r.out[2] > r.inn[2] and r.type == "transfer":
            implicit = r.out[2] - r.inn[2]
        if implicit:
            return Fee(f"{_q(implicit)} {r.out[1]} in der Differenz Abgang − Eingang enthalten (nicht als Gebühr "
                       "belegt)", pid, r.out[1], _q(implicit), "in der Differenz enthalten")
        return Fee("keine Gebühr geliefert", pid, basis="keine Gebühr geliefert")
    if tx.fee_qty == 0:
        return Fee(f"Gebühr 0 {tx.fee_asset} (belegt)", pid, tx.fee_asset, _q(ZERO), "belegt 0")
    return Fee(f"{_q(tx.fee_qty)} {tx.fee_asset} zusätzlich gebucht, belastet {names.place(acc).name if acc else '?'}",
               pid, tx.fee_asset, _q(tx.fee_qty), "zusätzlich gebucht")


def _sources(ctx: Any, report: Report, tx_id: str) -> tuple[list[SourceRef], int, list[str], dict[str, Any]]:
    """Quellenanhänge einer Buchung als Belege (vorhandene Gruppierung); Widersprüche zwischen den Quellen."""
    from app.csvimport import groups as G

    snap = report.snapshot
    meta = snap.journal.get(tx_id) if snap is not None else None
    tech: dict[str, Any] = {}
    if meta is not None:
        tech = {"stabile Quell-ID": meta.external_id or "", "Ereignis": meta.event_key or "",
                "Importlauf": f"#{meta.batch_id}" if meta.batch_id else "", "Datenquelle": meta.datasource_id or ""}
    try:
        g = G.group(ctx, tx_id)
    except Exception as e:  # nie die Ansicht blockieren
        log.debug("Quellengruppe von %s nicht lesbar: %s", tx_id, e)
        return [], 0, [], tech
    roles = {"same": "gleicher Vorgang", "out": "Abgangsseite", "in": "Zugangsseite", "part": "Teil"}
    refs = [SourceRef(label=m.source, ext_id=m.ext_id or "", role=roles.get(m.role, m.role),
                      batch_url=f"/journal/csv/{m.batch_id}" if m.batch_id else "") for m in g.members]
    conflicts = [f"{G.KINDS.get(k, k)}: {t}" for k, t in g.conflicts]
    return refs[:MAX_SOURCES], max(0, len(refs) - MAX_SOURCES), conflicts, tech


# ----------------------------------------------------------------------------------------------------
# Aufbau
# ----------------------------------------------------------------------------------------------------

def _holdings(report: Report) -> dict[tuple[str, str], Any]:
    return {(h.account, h.asset): h for h in report.holdings}


def _facts(report: Report, names: _Names, place: Place, assets: list[str]) -> list[Fact]:
    """Bestände am Ort, die zum Fall gehören – mit Zeitpunkt, Herkunft und Umfang; fehlender Nachweis = unbekannt."""
    idx = report.index
    out: list[Fact] = []
    hold = _holdings(report)
    for aid in assets:
        h = hold.get((place.key, aid))
        out.append(Fact("Ledger-Bestand", _q(idx.bal(place.key, aid)) if idx is not None else None, aid,
                        at="jetzt", source="alle wirksamen Buchungen", scope="Konto gesamt"))
        if h is None:
            out.append(Fact("Externer Nachweis", None, aid, note="kein Nachweis – unbekannt, nicht 0"))
            continue
        if h.observed is not None:
            out.append(Fact("Bestand laut Quelle", _q(h.observed), aid, at=_ts(h.observed_at) if h.observed_at else "",
                            source=f"{h.observed_by or 'Datenquelle'}"
                                   + (f" · {h.observed_state}" if h.observed_state else ""),
                            scope="Konto gesamt (Abruf)",
                            note=(f"Ledger zum selben Zeitpunkt {_q(h.computed_at_obs)}"
                                  if h.computed_at_obs is not None else "Ledger zum Abrufzeitpunkt nicht bestimmbar")))
        if h.reference is not None:
            refs = [r for r in (report.snapshot.references if report.snapshot else [])
                    if r.account == place.key and r.asset_id == aid]
            ref = refs[-1] if refs else None
            scope = " / ".join(x for x in (SCOPE_TEXT.get((ref.scope if ref else "") or "", ""),
                                           (ref.subaccount if ref else "") or "") if x) or "Umfang nicht angegeben"
            out.append(Fact("Nachweis (Nutzer)", _q(h.reference), aid,
                            at=_ts(h.reference_ts) if h.reference_ts else str(h.reference_at or ""),
                            source=h.reference_note or "Bestandsnachweis", scope=scope,
                            note=(f"Ledger zum selben Zeitpunkt {_q(h.soll_at_ref)}" if h.soll_at_ref is not None
                                  else "")))
        if h.observed is None and h.reference is None:
            out.append(Fact("Externer Nachweis", None, aid, note="kein Nachweis – unbekannt, nicht 0"))
    return out


def _ops(case: K.Case, hyp_id: str) -> tuple[K.Hyp | None, set[str], list[Any]]:
    hyp = next((h for h in case.hyps if h.id == hyp_id and not h.base), None) if hyp_id else None
    if hyp is None:
        return None, set(), []
    removed: set[str] = set()
    created: list[Any] = []
    for c in hyp.cands:
        for op in c.plan.ops:
            if op.kind in ("hide", "cover"):
                removed |= {op.target, *op.members}
            elif op.kind == "create" and op.tx is not None:
                created.append(op)
    return hyp, removed, created


def _reason_for_pair(w: TxRef, d: TxRef) -> list[str]:
    """Konkrete Begründung eines Zuordnungsvorschlags – Belege, fehlende Belege, Konkurrenz (keine Prozentwerte)."""
    out: list[str] = []
    common = sorted(set(w.hashes) & set(d.hashes))
    if common:
        out.append(f"Gleicher Onchain-Hash {common[0][:10]}… an beiden Buchungen (Identitätsbeleg).")
    elif w.hashes and d.hashes:
        out.append("Beide Buchungen tragen einen Hash, aber verschiedene – das spricht gegen dieselbe Bewegung.")
    else:
        missing = "Abgang" if not w.hashes else "Eingang"
        out.append(f"Kein gemeinsamer Hash belegt (am {missing} fehlt er) – Betrag und Zeit allein reichen zur "
                   "eindeutigen Zuordnung nicht aus.")
    if w.out and d.inn and w.out[1] == d.inn[1]:
        diff = w.out[2] - d.inn[2]
        if diff == 0:
            out.append("Abgangs- und Eingangsmenge sind gleich.")
        elif diff > 0:
            out.append(f"Der Eingang ist um {_q(diff)} {w.out[1]} kleiner als der Abgang – die Differenz ist nur "
                       "dann eine Gebühr, wenn sie belegt ist.")
        else:
            out.append(f"Der Eingang ist um {_q(-diff)} {w.out[1]} größer als der Abgang – Herkunft der Differenz "
                       "unbekannt.")
    if w.ts and d.ts:
        secs = abs((d.ts - w.ts).total_seconds())
        out.append(f"Zeitabstand zwischen Abgang und Eingang: {int(secs // 60)} min – beide Zeitpunkte bleiben "
                   "getrennt angegeben.")
    return out


def build(ctx: Any, case: K.Case, report: Report, *, hyp_id: str = "", sel: str = "", expand: bool = False,
          authorization_auto: bool = False) -> CaseView:
    """Fall → Darstellungsmodell. ``hyp_id`` zeigt die gemeinsame Simulation dieser Hypothese (noch nichts gebucht),
    sonst den aktuellen Stand."""
    idx = report.index
    names = _Names(ctx, report)
    hyp, removed, created = _ops(case, hyp_id)
    holdings = _holdings(report)
    tx_ids = list(case.tx_ids)
    truncated = 0
    if len(tx_ids) > MAX_MOVES:
        truncated = len(tx_ids) - MAX_MOVES
        tx_ids = tx_ids[:MAX_MOVES]
    moves: list[Move] = []
    if idx is not None:
        txs = [idx.by_id.get(t) for t in tx_ids]
        for tx in sorted((t for t in txs if t is not None), key=lambda t: (t.ts, t.tx_id)):
            r = idx.ref(tx)
            moves.append(_move(ctx, report, names, tx, r, "removed" if tx.tx_id in removed else "booked", holdings))
        for op in created:
            t = op.tx
            cached = t.tx_id in idx._refs
            moves.append(_move(ctx, report, names, t, op.ref or idx.ref(t), "created", holdings))
            if not cached:
                idx._refs.pop(t.tx_id, None)  # die Simulation darf den Bericht nicht verändern
        moves += _proposals(report, names, case, holdings, moves)
    # Orte: Konten der Vorgänge und des Falls, auch ohne Vorgang (Bestandsfälle)
    for acc in case.accounts:
        names.place(acc)
    assets = list(case.assets)
    for p in names.places.values():
        p.facts = _facts(report, names, p, [a for (acc, a) in case.positions if acc == p.key] or assets)
    impact = _impact(report, names, case, hyp) if hyp is not None else None
    places = names.finish()
    questions = _questions(case, moves)
    mode = (f"Simulation: {hyp.label} – nichts davon ist gebucht" if hyp is not None
            else "Aktueller Stand – wie gebucht")
    view = CaseView(key=case.key, simulated=hyp is not None, mode_text=mode, hyp_id=hyp.id if hyp else "",
                    places=places, moves=moves, questions=questions, action=_action(ctx, case, authorization_auto),
                    quality=_quality(ctx, case, report, moves), truncated=truncated, derivatives=list(case.deriv),
                    revisions=_revisions(ctx, case))
    view.selected = _select(view, sel)
    view.impact = impact
    return view


def _impact(report: Report, names: _Names, case: K.Case, hyp: K.Hyp) -> Impact:
    """Bestands-, Steuer- und Anschaffungswirkung aus der gemeinsamen Simulation. Bewertung nur mit dem gespeicherten
    Kurs (vor und nach gleich, keine Abfrage); ohne Kurs bleibt der Wert unbekannt."""
    idx = report.index
    base = case.baseline
    rows: list[dict[str, Any]] = []
    prices: dict[str, str] = {}
    for key in sorted(set(base.balances) | set(hyp.balances)):
        before, after = base.balances.get(key, ZERO), hyp.balances.get(key, ZERO)
        if before == after:
            continue
        acc, aid = key
        pinfo = idx.price(aid) if idx is not None else None
        value = ""
        if pinfo is not None:
            value = f"{(after - before) * Decimal(str(pinfo.price_eur)):+.2f} EUR"
            when = _ts(pinfo.ts) if pinfo.ts else "Zeitpunkt unbekannt"
            prices[aid] = f"{aid}: gespeicherter Kurs ({pinfo.source}, {when}{', veraltet' if pinfo.stale else ''})"
        rows.append({"place": names.place(acc).name, "pid": names.place(acc).pid, "asset": aid,
                     "before": _q(before), "after": _q(after), "delta": f"{after - before:+f}".replace(".", ","),
                     "value": value or "nicht bewertbar (kein Kurs)"})
    tax = []
    for y in sorted(set(base.years) | set(hyp.years)):
        (g0, i0), (g1, i1) = base.years.get(y, (ZERO, ZERO)), hyp.years.get(y, (ZERO, ZERO))
        if (g0, i0) != (g1, i1):
            tax.append({"year": str(y), "gain": f"{g0:.2f} → {g1:.2f} EUR", "income": f"{i0:.2f} → {i1:.2f} EUR"})
    diffs = [f"{k[0]} · {k[1]}: Differenz zum Nachweis " + " | ".join(v) for k, v in sorted(hyp.stock_diffs.items())]
    notes = []
    if hyp.resolved:
        notes.append(f"{len(hyp.resolved)} Befund(e) wären danach erledigt.")
    if hyp.successors:
        notes.append("Danach eigenständig zu prüfen (bisher verdeckt): " + "; ".join(hyp.successors[:3]))
    if hyp.new_findings:
        notes.append("Neuer Befund: " + "; ".join(hyp.new_findings[:3]))
    return Impact(hyp_id=hyp.id, ok=hyp.ok, reasons=list(hyp.reasons), positions=rows, tax=tax,
                  lots=list(hyp.lot_dates), stock_text=STOCK_TEXT.get(hyp.stock, hyp.stock), stock_diffs=diffs,
                  notes=notes, price_note="; ".join(prices.values()) if prices else "")


def _move(ctx: Any, report: Report, names: _Names, tx: Any, r: TxRef, state: str,
          holdings: dict[tuple[str, str], Any]) -> Move:
    idx = report.index
    out, inn = _legs(idx, r, holdings)
    src_acc = r.out[0] if r.out else ""
    dst_acc = r.inn[0] if r.inn else ""
    if r.type == "deposit" or (not src_acc and dst_acc):
        src = names.unknown("Herkunft nicht bekannt", tx.tx_id).pid
    else:
        src = names.place(src_acc).pid if src_acc else names.unknown("Herkunft nicht bekannt", tx.tx_id).pid
    if r.type == "withdrawal" or (src_acc and not dst_acc):
        dst = names.unknown("Gegenkonto nicht bekannt", tx.tx_id).pid
    else:
        dst = names.place(dst_acc).pid if dst_acc else names.unknown("Gegenkonto nicht bekannt", tx.tx_id).pid
    srcs, more, conflicts, tech = _sources(ctx, report, tx.tx_id) if state != "created" else ([], 0, [], {})
    tech.update({"Buchung": tx.tx_id, "Hash": ", ".join(r.hashes) or "keiner", "Quelle": r.source or ""})
    kind = TYPE_TEXT.get(r.type, r.type) + (f" ({r.tag})" if r.tag else "")
    return Move(mid=f"m-{tx.tx_id}", state=state, tx_ids=[tx.tx_id], type_label=kind, src=src, dst=dst, out=out,
                inn=inn, fee=_fee(idx, names, tx, r), ts_out=_ts(r.ts, tx.date_only) if r.out else "",
                ts_in=_ts(r.ts, tx.date_only) if r.inn else "", sources=srcs, sources_more=more, conflicts=conflicts,
                technical=tech, edit_url=r.edit_url or "",
                group_url=f"/journal/ereignis/{quote(tx.tx_id, safe='')}" if state != "created" else "",
                value_text="" if r.value_eur is None else f"{r.value_eur:.2f} EUR")


def _candidate_pairs(idx: Any, f: Finding) -> list[tuple[TxRef, TxRef]]:
    """Abgang/Zugang-Paare eines Transfer-Befunds: eindeutige Vorschläge (``pairs``) und – bei Mehrdeutigkeit – alle
    ausdrücklich möglichen Alternativen (``data["alternatives"]``)."""
    out: list[tuple[TxRef, TxRef]] = []
    for a, b, _why in f.pairs:
        w, d = (a, b) if a.type == "withdrawal" else (b, a)
        out.append((w, d))
    for wid, dids in (f.data.get("alternatives") or {}).items():
        w = idx.by_id.get(wid)
        for did in dids:
            d = idx.by_id.get(did)
            if w is not None and d is not None:
                out.append((idx.ref(w), idx.ref(d)))
    return out


def _proposals(report: Report, names: _Names, case: K.Case, holdings: dict[tuple[str, str], Any],
               moves: list[Move]) -> list[Move]:
    """Zuordnungsvorschläge (gestrichelt): mögliche Gegenbuchungen aus den Befunden des Falls; konkurrierende
    Möglichkeiten werden ausdrücklich als Alternativen gezeigt. Gebucht ist dadurch nichts."""
    idx = report.index
    seen: set[tuple[str, str]] = set()
    by_w: dict[str, list[Move]] = {}
    for f in case.findings:
        if f.kind != "transfer":
            continue
        for w, d in _candidate_pairs(idx, f):
            if w.type != "withdrawal" or d.type != "deposit" or (w.tx_id, d.tx_id) in seen or not w.out or not d.inn:
                continue
            seen.add((w.tx_id, d.tx_id))
            by_w.setdefault(w.tx_id, []).append(Move(
                mid=f"m-{w.tx_id}-{d.tx_id}", state="proposed", tx_ids=[w.tx_id, d.tx_id],
                type_label="möglicher Übertrag (Zuordnungsvorschlag)", src=names.place(w.out[0]).pid,
                dst=names.place(d.inn[0]).pid, out=_legs(idx, w, holdings)[0], inn=_legs(idx, d, holdings)[1],
                fee=None, ts_out=_ts(w.ts), ts_in=_ts(d.ts), reason=_reason_for_pair(w, d),
                technical={"Abgang": w.tx_id, "Eingang": d.tx_id, "Hash Abgang": ", ".join(w.hashes) or "keiner",
                           "Hash Eingang": ", ".join(d.hashes) or "keiner"}))
    out: list[Move] = []
    for ms in by_w.values():
        if len(ms) > 1:
            for n, m in enumerate(ms, 1):
                m.type_label = f"mögliche Zuordnung {n} von {len(ms)} (konkurrierend)"
                m.alt_of = ms[0].mid if n > 1 else ""
                m.reason.append(f"Es gibt {len(ms)} mögliche Zugänge für diesen Abgang – Portfolia wählt keinen aus.")
        out += ms
    return out


def _questions(case: K.Case, moves: list[Move]) -> list[str]:
    qs: list[str] = []
    open_moves = 0
    for m in moves:
        if m.state in ("booked", "removed"):
            leg = m.out or m.inn
            side = m.dst if m.inn is None else m.src if m.out is None else ""
            if leg is not None and side.startswith("u-"):
                open_moves += 1
                if open_moves > MAX_OPEN_QUESTIONS:
                    continue
                qs.append(f"{m.type_label} {leg.qty} {leg.asset}: {'Gegenkonto' if side == m.dst else 'Herkunft'} "
                          "nicht bekannt – Zugehörigkeit offen (weder eigene Wallet noch externe Auszahlung belegt).")
    if open_moves > MAX_OPEN_QUESTIONS:
        qs.append(f"… und {open_moves - MAX_OPEN_QUESTIONS} weitere Vorgänge mit unbekanntem Gegenkonto.")
    if case.decision is not None:
        qs += list(case.decision.missing)
    if case.question is not None:
        qs.append(case.question.get("text", ""))
    return list(dict.fromkeys(q for q in qs if q))


def _action(ctx: Any, case: K.Case, auto_ok: bool) -> NextAction | None:
    """Eine begründete nächste Handlung hervorheben (Parameter aus dem Fall); weitere bleiben im Fall erreichbar."""
    d = case.decision
    if d is None:
        return None
    base = f"/quality/klaerung/{case.key}"
    if d.kind == "apply" and d.hyp is not None:
        auto = auto_ok and all(c.action in K.AUTO_TYPES for c in d.hyp.cands)
        return NextAction("hyp", "Belegte Lösung prüfen und übernehmen",
                          d.reason or "Alle Regeln der Entscheidung sind erfüllt.",
                          "automatisch" if auto else "bestaetigung", f"{base}?hyp={d.hyp.id}#wirkung", d.hyp.id)
    if d.kind == "ask_stock":
        return NextAction("stock", "Bestand zu diesem Zeitpunkt bestätigen", d.reason, "information",
                          f"{base}#bestandsnachweis")
    if d.kind == "confirm_partial":
        return NextAction("partial", "Restdifferenz prüfen", d.reason, "information", f"{base}#belege")
    if d.kind == "blocked":
        return NextAction("blocked", "Folgeprobleme der Simulation prüfen", d.reason, "information",
                          f"{base}#hypothesen")
    if d.kind == "umfang":
        return NextAction("scope", "Prüfumfang eingrenzen", d.reason, "information", f"{base}#hypothesen")
    if d.kind == "need_info":
        return NextAction("compare", "Belege vergleichen", d.reason or "Es fehlt ein Beleg.", "information",
                          f"{base}#belege")
    return NextAction("compare", "Belege vergleichen", d.reason, "information", f"{base}#belege")


def _quality(ctx: Any, case: K.Case, report: Report, moves: list[Move]) -> dict[str, tuple[str, str]]:
    """Datenqualität, Importabdeckung und offene Konflikte getrennt – ein passender Endbestand schließt Historienfehler
    nicht aus; die Abdeckung einer Quelle ist keine nachgewiesene vollständige Historie."""
    open_dims = [K.DIM_LABEL[k] for k, (st, _t) in case.dims.items() if st == "offen"]
    unknown_dims = [K.DIM_LABEL[k] for k, (st, _t) in case.dims.items() if st == "unbekannt"]
    dq = (("offen: " + ", ".join(open_dims)) if open_dims else
          ("unbekannt: " + ", ".join(unknown_dims)) if unknown_dims else "alle betroffenen Dimensionen geklärt")
    srcs = [s for s in (report.snapshot.sources if report.snapshot else []) if s.account in set(case.accounts)]
    if not srcs:
        cov = ("keine Datenquelle für diese Konten – Historie nur aus Import bzw. manuellen Buchungen "
               "(Vollständigkeit unbelegt)")
    else:
        bad = [s for s in srcs if not s.complete or s.gaps]
        cov = ("Abruf ohne erkannte Lücke – das belegt nicht, dass die gesamte Historie erfasst ist"
               if not bad else "erkannte Lücke oder unvollständiger Abruf: " + "; ".join(
                   f"{s.name} ({s.state})" for s in bad[:3]))
    conflicts = sum(len(m.conflicts) for m in moves)
    rev = len(_revisions(ctx, case))
    cf = (f"{conflicts + rev} offen" if conflicts + rev else "keine offenen Quellenkonflikte")
    return {"Datenqualität": (dq, "offen" if open_dims else "unbekannt" if unknown_dims else "geklärt"),
            "Importabdeckung": (cov, "unbelegt" if not srcs or bad else "ohne Lücke"),
            "Offene Konflikte": (cf, "offen" if conflicts + rev else "keine")}


def _revisions(ctx: Any, case: K.Case) -> list[dict[str, str]]:
    """Offene Quellenrevisionen (gleiche Kennung, geänderter Inhalt) zu Buchungen dieses Falls."""
    if not case.tx_ids:
        return []
    out: list[dict[str, str]] = []
    try:
        rows = ctx.db.q("SELECT r.batch_id, r.idx, r.messages FROM csv_row r JOIN csv_batch b ON b.id=r.batch_id WHERE "
                        "r.status='revision' AND b.status IN ('preview','partial')")
    except sqlite3.Error as e:  # sehr alte Datenbank
        fallback("_revisions", e)
        return []
    wanted = set(case.tx_ids)
    for r in rows:
        try:
            msgs = json.loads(r["messages"] or "{}")
        except ValueError:
            continue
        rev = msgs.get("revision") or {}
        dup = [x for x in (msgs.get("dup_of") or []) if x in wanted]
        if rev and dup:
            out.append({"tx": dup[0], "diffs": "; ".join(rev.get("diffs") or []),
                        "url": f"/journal/csv/{r['batch_id']}"})
    return out


def _select(view: CaseView, sel: str) -> dict[str, Any] | None:
    """Auswahl eines Orts (``p-…``) bzw. einer Verbindung (``m-…``): zugehörige Vorgänge, Belege und Bestände."""
    if not sel:
        return None
    place = view.place(sel)
    if place is not None:
        return {"kind": "place", "id": sel, "place": place,
                "moves": [m for m in view.moves if sel in (m.src, m.dst)]}
    move = next((m for m in view.moves if m.mid == sel), None)
    if move is not None:
        return {"kind": "move", "id": sel, "move": move, "src": view.place(move.src), "dst": view.place(move.dst)}
    return None
