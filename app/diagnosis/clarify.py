"""Konfliktklärung: zusammengehörige Befunde gemeinsam untersuchen, simulieren und – nur bei eindeutiger Beleglage –
übernehmen; sonst genau die Information benennen, die noch fehlt.

Ablauf je Klärungsgruppe: Erkennen (offene Befunde) → Belege sammeln (Dossier) → Kandidaten bilden (nur aus den
Lösungen der vorhandenen Empfehlung) → gemeinsam simulieren (vorhandene Ledger- und Diagnose-Engine auf einer Kopie)
→ entscheiden → atomar übernehmen → nachprüfen (bei Misserfolg vollständig zurücknehmen).

Es gibt keine zweite Ledger-, FIFO- oder Steuer-Engine: Wirkungen kommen ausschließlich aus
:func:`app.diagnosis.actions.hypothetical`, :func:`app.ledger.engine.run_ledger` und
:func:`app.diagnosis.engine.diagnose`.

Entscheidungsregel (automatische Übernahme nur, wenn **alle** Punkte erfüllt sind):

1. Belege: jeder Kandidat der Hypothese ist durch Identität belegt (gemeinsamer Hash bzw. Quellreferenz) – oder
   rechnerisch exakt erklärt (Brutto − Gebühr = Netto) **und** ein Bestandsnachweis erfüllt genau diese Hypothese.
   Gleicher Betrag/ähnliche Uhrzeit allein sind Suchhinweise, keine Identität; eine Differenz ohne belegte Ursache
   (z. B. unbelegte Gebühr) ist nie ein Beleg.
2. Eindeutigkeit: es gibt keine zweite zulässige Hypothese mit abweichender wirtschaftlicher Wirkung, die dieselben
   Buchungen anders deutet (wirtschaftlich gleichwertig sind nur Varianten mit gleichen Beständen, Lots,
   Anschaffungsdaten, Gebühren, Zahlungsströmen und Ergebnissen je Jahr).
3. Gemeinsame Simulation: kein neuer negativer Bestand, kein neues Ledger-Problem, kein neuer oder verschlechterter
   Befund, keine Verschlechterung eines Bestandsnachweises, kein widersprüchlicher Zwischenbestand.
4. Der Plan besteht nur aus Verknüpfen/Ausblenden vorhandener Buchungen (keine erfundene Buchung).
5. Datenstand und Pläne sind beim Schreiben unverändert (Prüfsummen, unter Sperre neu gebildet).
6. Die Automatik erlaubt die Aktion (``autopilot.enabled`` und ``autopilot.fixes``) oder der Nutzer hat ausdrücklich
   bestätigt.

Bestandsdifferenzen erzeugen nie eine Buchung. Ein Bestandsnachweis wählt nur zwischen Hypothesen, die aus Belegen
entstanden sind, und ersetzt weder Anschaffungsbelege noch die Ursache einer Differenz.
"""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import json
import logging
import threading
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.db import fallback
from app.diagnosis import actions as A
from app.diagnosis import bulk as B
from app.diagnosis import derivatives as DV
from app.diagnosis import triage as TR
from app.diagnosis.engine import diagnose, report_for
from app.diagnosis.model import Finding, Report
from app.diagnosis.recommend import recommend
from app.ledger.engine import DUST, LedgerResult, run_ledger

log = logging.getLogger(__name__)

ZERO = Decimal(0)
CENT = Decimal("0.01")
CLARIFY_KINDS = frozenset({"duplicate", "transfer", "holdings", "loss", "reconstruction", "migration"})
# Lösungen, die aus Belegen ableitbar sind und keine freie Eingabe brauchen (Bestandsdifferenzen erzeugen keine
# Buchung: ``adjust``/``opening``/``book_migration`` und freie Auswahl gehören nicht dazu)
CAND_OPTIONS = frozenset({"hide_weak", "hide_weak_adjust", "hide_strong", "hide_second", "cover_twin", "cover",
                          "link_econ", "link_econ_swap", "hide_import", "link", "stage"})
# Varianten derselben Deutung (nur die Darstellung – welche Seite bleibt – unterscheidet sich)
FAMILY = {"link_econ": "econ", "link_econ_swap": "econ", "hide_weak": "hide", "hide_strong": "hide",
          "hide_weak_adjust": "hide"}
AUTO_TYPES = frozenset({"verknüpfen"})  # automatisch ausführbare Aktionsart
DEFAULT_LIMIT = 64  # Hypothesen je Gruppe (deterministisch)
DEFAULT_BUDGET = 400  # Simulationen je Lauf
MAX_PRODUCT = 20000  # darüber wird nicht einmal aufgezählt
MAX_FINDINGS = 40  # Befunde je Gruppe (Anzeige/Dokumentation)
MAX_RESEARCH = 5  # Abrufe bei Datenquellen je Durchgang

STATUS: dict[str, tuple[str, str]] = {
    "geloest": ("gelöst", "good"),
    "bereit": ("Lösung eindeutig – wartet auf Bestätigung", "info"),
    "wartet_bestand": ("wartet auf Bestandsnachweis", "warn"),
    "wartet_info": ("wartet auf Information", "warn"),
    "teilweise": ("Menge bestätigt – Rest offen", "info"),
    "blockiert": ("blockiert", "crit"),
    "umfang": ("Prüfumfang überschritten", "warn"),
    "zurueck": ("zurückgestellt", ""),
}
OPEN_STATUSES = ("bereit", "wartet_bestand", "wartet_info", "teilweise", "blockiert", "umfang")

DIM_ORDER = ("bestand", "transfer", "gebuehren", "vorgangsart", "anschaffung", "historie", "steuer")
DIM_LABEL = {"bestand": "Bestand/Menge", "transfer": "Transfer und Gegenkonto", "gebuehren": "Gebühren",
             "vorgangsart": "Wirtschaftliche Vorgangsart", "anschaffung": "Anschaffungsdaten",
             "historie": "Historische Vollständigkeit", "steuer": "Steuerliche Einordnung"}
DIM_STATE = {"geklaert": ("geklärt", "good"), "offen": ("offen", "warn"), "unbekannt": ("unbekannt", ""),
             "n/a": ("nicht betroffen", "")}
EVIDENCE = {"identitaet": "Identität belegt (gemeinsamer Hash/Quellreferenz)",
            "herkunft": "Herkunft belegt (gebuchte Zugänge = Transfermenge; zweite Gutschrift ohne Herkunft)",
            "betrag_gebuehr": "rechnerisch exakt erklärt (Brutto − Gebühr = Netto), keine Identität",
            "betrag": "nur Betrag/Zeit ähnlich – Suchhinweis, kein Beleg", "ohne": "kein Beleg"}
_RANK = {"identitaet": 3, "herkunft": 3, "betrag_gebuehr": 2, "betrag": 1, "ohne": 0}

_RUN = threading.Lock()
_FLAG = {"dirty": False}


# ----------------------------------------------------------------------------------------------------
# Datenobjekte
# ----------------------------------------------------------------------------------------------------

@dataclass
class Cand:
    """Lösungskandidat eines Befunds (aus der vorhandenen Empfehlung), mit Plan und Beleglage."""

    finding_id: str
    option: str
    label: str
    plan: A.Plan
    txs: frozenset[str]
    positions: frozenset[tuple[str, str]]
    evidence: str = "ohne"
    notes: list[str] = field(default_factory=list)
    action: str = "verknüpfen"  # verknüpfen | bereitstellen | neue Buchung
    fee_explained: bool = False  # Mengendifferenz ist durch dokumentierte Gebühr erklärt
    recommended: bool = False  # bevorzugte Darstellung der vorhandenen Empfehlung (Quellenpriorität)
    finding: Finding | None = None  # Befund (bei Margin-Kandidaten ein abgeleiteter, nicht im Bericht enthalten)

    @property
    def family(self) -> str:
        return FAMILY.get(self.option, self.option)

    @property
    def key(self) -> str:
        return f"{self.finding_id}:{self.option}"

    def doc(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "evidence": self.evidence, "action": self.action,
                "plan": self.plan.token, "notes": self.notes[:4], "txs": sorted(self.txs)[:20]}


@dataclass
class Hyp:
    """Hypothese = widerspruchsfreie Auswahl von Kandidaten (leer = Ausgangslage)."""

    cands: tuple[Cand, ...]
    sig: Any = None
    ok: bool = True
    reasons: list[str] = field(default_factory=list)
    stock: str = "kein_nachweis"  # alle_erfuellt | teilweise | widerspruch | kein_nachweis
    stock_diffs: dict[str, list[str]] = field(default_factory=dict)
    balances: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    new_findings: list[str] = field(default_factory=list)
    successors: list[str] = field(default_factory=list)  # bisher verdeckte Folgefragen – kein Hindernis
    resolved: list[str] = field(default_factory=list)
    years: dict[int, tuple[Decimal, Decimal]] = field(default_factory=dict)
    lot_dates: list[tuple[str, str, str, str]] = field(default_factory=list)  # Anschaffungsdaten vorher/nachher
    sim: Any = field(default=None, repr=False)
    rep_done: bool = False

    @property
    def id(self) -> str:
        return "h-" + hashlib.sha256("|".join(sorted(c.key for c in self.cands)).encode()).hexdigest()[:10] \
            if self.cands else "h-0"

    @property
    def base(self) -> bool:
        return not self.cands

    @property
    def label(self) -> str:
        return "keine Änderung (Ausgangslage)" if self.base else " + ".join(c.label for c in self.cands)

    def doc(self) -> dict[str, Any]:
        return {"id": self.id, "cands": [c.key for c in self.cands], "ok": self.ok, "reasons": self.reasons[:6],
                "stock": self.stock, "stock_diffs": self.stock_diffs, "new": self.new_findings[:6],
                "successors": self.successors[:6],
                "resolved": len(self.resolved), "label": self.label[:200],
                "years": {str(y): [str(a), str(b)] for y, (a, b) in sorted(self.years.items())},
                "lot_dates": [list(x) for x in self.lot_dates[:6]]}


@dataclass
class Decision:
    kind: str  # apply | ask_stock | need_info | confirm_partial | blocked | umfang | nothing | resolved
    hyp: Hyp | None = None
    reason: str = ""
    rules: list[tuple[str, bool, str]] = field(default_factory=list)  # (Regel, erfüllt, Begründung)
    missing: list[str] = field(default_factory=list)


@dataclass
class Case:
    key: str
    kind: str
    title: str
    findings: list[Finding]
    positions: list[tuple[str, str]]
    accounts: list[str]
    assets: list[str]
    tx_ids: list[str]
    cands: list[Cand] = field(default_factory=list)
    excluded: list[tuple[str, str]] = field(default_factory=list)  # (Kandidat, Grund)
    hyps: list[Hyp] = field(default_factory=list)
    total_hyps: int = 0
    decision: Decision | None = None
    status: str = "wartet_info"
    dims: dict[str, tuple[str, str]] = field(default_factory=dict)  # Dimension → (Zustand, Text)
    evidence: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    question: dict[str, Any] | None = None
    version: str = ""
    evidence_fp: str = ""
    research: list[str] = field(default_factory=list)
    deriv: list[str] = field(default_factory=list)  # Derivate-Dossier (Ergebnis getrennt, offene Fragen)
    deferred: bool = False  # Simulationsbudget des Durchgangs ausgeschöpft → im Folgelauf zuerst

    @property
    def baseline(self) -> Hyp:
        return self.hyps[0]

    def summary(self) -> str:
        label, _b = STATUS[self.status]
        return f"{label}: {self.title}"


# ----------------------------------------------------------------------------------------------------
# Hilfen
# ----------------------------------------------------------------------------------------------------

def _tx_ids(f: Finding) -> set[str]:
    return {r.tx_id for r in f.txs} | {x.tx_id for a, b, _w in f.pairs for x in (a, b)}


def _positions(f: Finding) -> set[tuple[str, str]]:
    pos = set(f.positions)
    for r in [*f.txs, *(x for a, b, _w in f.pairs for x in (a, b))]:
        for leg in (r.out, r.inn):
            if leg is not None and leg[0] and leg[1]:
                pos.add((leg[0], leg[1]))
    if not pos:
        pos = {(a, s) for a in f.accounts for s in f.assets}
    return pos


def _setting(ctx: Any, key: str, default: Any) -> Any:
    try:
        return ctx.settings.get(key, default)
    except Exception as e:  # pragma: no cover - Einstellungen nie kritisch
        fallback("_setting", e, quiet=True)
        return default


def _q(v: Decimal) -> str:
    s = format(v.normalize(), "f")
    return s if "." not in s else s.rstrip("0").rstrip(".")


def auto_allowed(ctx: Any) -> bool:
    """Bestehende Automatik-Einstellung: ``autopilot.enabled`` und ``autopilot.fixes``."""
    return bool(_setting(ctx, "autopilot.enabled", False)) and bool(_setting(ctx, "autopilot.fixes", False))


# ----------------------------------------------------------------------------------------------------
# Gruppen
# ----------------------------------------------------------------------------------------------------

def groups(findings: list[Finding]) -> list[list[Finding]]:
    """Fachlich abhängige Befunde zusammenfassen: gemeinsame Buchung oder gemeinsame Position (Konto, Asset).
    Sammelbefunde mit Einzelvorgängen zählen nur über ihre Einzelvorgänge (keine Doppelzählung)."""
    items = [f for f in findings if not f.children and f.kind in CLARIFY_KINDS]
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    seen: dict[str, int] = {}
    for i, f in enumerate(items):
        toks = [f"tx:{t}" for t in _tx_ids(f)] + [f"pos:{a}|{s}" for a, s in _positions(f)]
        for t in toks:
            j = seen.setdefault(t, i)
            if j != i:
                parent[find(i)] = find(j)
    out: dict[int, list[Finding]] = defaultdict(list)
    for i, f in enumerate(items):
        out[find(i)].append(f)
    res = [sorted(g, key=lambda f: f.id) for g in out.values()]
    return sorted(res, key=lambda g: g[0].id)


def case_key(positions: set[tuple[str, str]]) -> str:
    return "kg-" + hashlib.sha256("|".join(sorted(f"{a}|{s}" for a, s in positions)).encode()).hexdigest()[:12]


# ----------------------------------------------------------------------------------------------------
# Beleglage der Kandidaten
# ----------------------------------------------------------------------------------------------------

def _net_in(t: Any) -> Decimal | None:
    """Netto-Zugang einer Buchung im Asset (Brutto − Gebühr im selben Asset)."""
    if t.to_qty is None:
        return None
    fee = t.fee_qty if (t.fee_qty and t.fee_asset == t.to_asset) else ZERO
    return t.to_qty - fee


def _net_out(t: Any) -> Decimal | None:
    if t.from_qty is None:
        return None
    return t.from_qty


def _evidence(report: Report, f: Finding, plan: A.Plan) -> tuple[str, list[str], bool]:
    """Beleglage eines Kandidaten (Stufe, Begründungen, Gebühr erklärt). Nur Belege zählen: gemeinsamer Hash bzw.
    gleiche Quellreferenz = Identität; rechnerische Übereinstimmung nach Gebühr ist kein Identitätsbeleg."""
    idx = report.index
    notes: list[str] = []
    if idx is None:
        return "ohne", ["kein Index"], False
    dropped = [op.target for op in plan.ops if op.kind in ("hide", "cover")]
    tx_ids = _tx_ids(f)
    kept = [t for t in tx_ids if t not in dropped]
    txs = {t: idx.by_id.get(t) for t in tx_ids}
    if any(v is None for v in txs.values()) or not tx_ids:
        return "ohne", ["Buchungen nicht auffindbar"], False
    # Herkunftskette: die gesamte Transfermenge stammt aus gebuchten Zugängen des Quellkontos – die zweite, gleich
    # große Gutschrift am Ziel (ohne Hash, manuell erfasst) hat keine Herkunft und wird ausgeblendet
    d = f.data or {}
    if d.get("type") == "same_qty" and d.get("chain") and d.get("weak") in dropped:
        return "herkunft", [e for e in f.evidence if e.startswith("Herkunft belegt")][:1] or [
            "Herkunft der Transfermenge aus gebuchten Zugängen belegt"], True
    # Identität: gemeinsamer Hash oder gleiche Quellreferenz je Paar der Gegenüberstellung
    pairs: list[tuple[str, str]] = []
    for a, b, _w in f.pairs:
        pairs.append((a.tx_id, b.tx_id))
    for p in (f.data or {}).get("pairs") or []:
        if isinstance(p, (list, tuple)) and len(p) == 2 and p[0] in txs and p[1] in txs:
            pairs.append((p[0], p[1]))
    ident = []
    for a, b in dict.fromkeys(pairs):
        ta, tb = txs.get(a), txs.get(b)
        if ta is None or tb is None:
            continue
        common = sorted(idx.hashes.get(a, set()) & idx.hashes.get(b, set()))
        same_ref = bool(ta.source_ref and ta.source_ref == tb.source_ref)
        if common:
            ident.append(f"gemeinsamer Hash {common[0][:12]}…")
        elif same_ref:
            ident.append(f"gleiche Quellreferenz {ta.source_ref}")
        else:
            ident.append("")
    if ident and all(ident):
        return "identitaet", ident[:3], True
    # rechnerisch erklärt: Summe der ausgeblendeten Seiten = Summe der verbleibenden (nach dokumentierter Gebühr)
    if f.kind == "duplicate" and kept:
        drop_in = [_net_in(txs[d]) for d in dropped if d in txs and txs[d].type == "deposit"]
        keep_in = [_net_in(txs[k]) for k in kept if txs[k].type == "deposit"]
        drop_out = [_net_out(txs[d]) for d in dropped if d in txs and txs[d].type == "withdrawal"]
        keep_out = [_net_out(txs[k]) for k in kept if txs[k].type == "withdrawal"]
        for a, b, what in ((drop_in, keep_in, "Zugang"), (drop_out, keep_out, "Abgang")):
            if a and b and None not in a and None not in b:
                sa, sb = sum(a, ZERO), sum(b, ZERO)  # type: ignore[arg-type]
                if sa == sb:
                    fee_used = any(txs[t].fee_qty for t in (*dropped, *kept) if t in txs)
                    notes.append(f"{what}: Summe {_q(sa)} = {_q(sb)}" + (" (nach dokumentierter Gebühr)" if fee_used
                                                                          else ""))
                    return "betrag_gebuehr", notes, fee_used
                notes.append(f"{what}: Summen {_q(sa)} ≠ {_q(sb)}")
        return "betrag", notes or ["Beträge ähnlich, nicht exakt erklärt"], False
    if f.kind == "transfer":
        for w_id, d_id in (f.data or {}).get("pairs") or []:
            w, d = txs.get(w_id), txs.get(d_id)
            if w is None or d is None or w.from_qty is None or d.to_qty is None:
                continue
            diff = w.from_qty - d.to_qty
            fee = w.fee_qty if (w.fee_qty and w.fee_asset == w.from_asset) else (
                d.fee_qty if (d.fee_qty and d.fee_asset == d.to_asset) else ZERO)
            if diff == 0:
                documented = bool(w.fee_qty or d.fee_qty)
                notes.append(f"Mengen gleich: {_q(w.from_qty)}" + (" (Gebühr separat dokumentiert)" if documented
                                                                      else ""))
                return "betrag_gebuehr", notes, documented
            if fee and diff == fee:
                notes.append(f"Abgang {_q(w.from_qty)} − Gebühr {_q(fee)} = Zugang {_q(d.to_qty)}")
                return "betrag_gebuehr", notes, True
            notes.append(f"Differenz {_q(diff)} {w.from_asset} ist nicht als Gebühr belegt")
        return "betrag", notes or ["Betrag/Zeit ähnlich"], False
    return "betrag", notes or ["keine Hash-/Referenzübereinstimmung"], False


def _auto_params(f: Finding, opt: Any) -> tuple[dict[str, list[str]] | None, str]:
    """Eingaben einer Lösung aus den Belegen: bei mehrteiligen Vorgängen alle Paare des Befunds – nur wenn die
    Zuordnung eindeutig ist (mehrdeutige Summen werden nie automatisch zusammengeführt). Freie Eingaben gibt es
    nicht."""
    params: dict[str, list[str]] = {}
    case = (f.data or {}).get("case") or {}
    for p in opt.params:
        if p.kind == "multi" and p.name == "pairs":
            if case.get("ambiguous") or (f.data or {}).get("ambiguous"):
                return None, "mehrdeutige Zuordnung (mehrere gleich gute Kombinationen) – keine automatische Auswahl"
            vals = [v for v, _l in p.choices]
            if not vals:
                return None, "keine zuordenbaren Paare"
            params["pairs"] = vals
        elif p.default:
            params[p.name] = list(p.default)
        else:
            return None, f"freie Eingabe nötig ({p.label}) – nicht aus Belegen ableitbar"
    return params, ""


def _action_type(plan: A.Plan) -> str:
    kinds = {op.kind for op in plan.ops}
    if kinds <= {"hide", "cover"}:
        return "verknüpfen"
    if kinds == {"stage"}:
        return "bereitstellen"
    from app.diagnosis.autopilot import _safe

    return "verknüpfen" if _safe(plan) else "neue Buchung"


def _cand_txs(plan: A.Plan, f: Finding) -> frozenset[str]:
    return frozenset(B._involved(f, plan))


def _plan_positions(plan: A.Plan, f: Finding) -> frozenset[tuple[str, str]]:
    return frozenset(B._positions(plan) | _positions(f))


# ----------------------------------------------------------------------------------------------------
# Simulation
# ----------------------------------------------------------------------------------------------------

@dataclass
class _Sim:
    led: LedgerResult
    plan: A.Plan
    pf: Any
    _rep: Report | None = None

    def rep(self, base: Report) -> Report:
        if self._rep is None:
            self._rep = diagnose(A._hyp_snapshot(base.snapshot, self.pf, self.led, self.plan))
        return self._rep


def _merged(cands: tuple[Cand, ...]) -> A.Plan:
    ops: list[A.Op] = []
    for i, c in enumerate(cands):
        for op in c.plan.ops:
            if op.kind == "create" and op.tx is not None:
                op = dataclasses.replace(op, tx=dataclasses.replace(op.tx, tx_id=f"{op.tx.tx_id}-{i}"))
            ops.append(op)
    first = cands[0].plan
    return A.Plan(first.finding, first.option, {}, ops=ops, version=first.version)


def _agg(rows: list[tuple[Any, ...]]) -> tuple[Any, ...]:
    """Je Schlüssel summieren (Teilbuchungen derselben Wirkung ergeben dasselbe wie die Sammelbuchung)."""
    acc: dict[tuple[Any, ...], list[Decimal]] = {}
    for r in rows:
        key, vals = r[:-2], r[-2:]
        cur = acc.setdefault(key, [ZERO, ZERO])
        cur[0] += vals[0]
        cur[1] += vals[1]
    return tuple(sorted((*k, format(v[0].normalize(), "f"), str(v[1].quantize(CENT))) for k, v in acc.items()))


def _signature(led: LedgerResult, assets: frozenset[str]) -> tuple[Any, ...]:
    """Wirtschaftliche Wirkung (nur Auszug der Gruppe): Bestände, Lots mit Anschaffungsdatum, Gebühren,
    Zahlungsströme, Veräußerungen und Erträge – je Tag aufsummiert, damit mehrere Teilbuchungen und eine
    Sammelbuchung mit gleicher Wirkung als gleichwertig gelten. Zwei Hypothesen sind nur bei vollständiger Gleichheit
    wirtschaftlich gleichwertig."""
    bal = tuple(sorted((a, s, format(q.normalize(), "f")) for (a, s), q in led.balances.items()
                       if s in assets and abs(q) > DUST))
    lots = _agg([(x.account, x.asset, str(x.acq_date), x.origin, x.qty, x.cost) for x in led.lots
                 if x.asset in assets])
    fees = _agg([(e.asset or "", e.kind, str(e.date), e.qty, e.eur) for e in led.fees if (e.asset or "") in assets])
    flows = _agg([(f.kind, f.asset or "", str(f.date), f.qty, f.amount if f.amount is not None else ZERO)
                  for f in led.flows if (f.asset or "") in assets])
    disp = _agg([(str(d.date), d.asset, d.kind, d.qty, d.proceeds - d.cost) for d in led.disposals
                 if d.asset in assets])
    inc = _agg([(str(e.date), e.asset, e.tag, e.qty, e.value_eur) for e in led.income if e.asset in assets])
    return bal, lots, fees, flows, disp, inc


def _years(led: LedgerResult) -> dict[int, tuple[Decimal, Decimal]]:
    return {y: (a.quantize(CENT), b.quantize(CENT)) for y, (a, b) in A._per_year(led).items()}


def _issue_keys(led: LedgerResult) -> set[tuple[Any, ...]]:
    return set(A._issue_keys(led))


def _rounded(diff: Decimal, ref: Decimal) -> Decimal:
    """Rundungsrest der Quelle (Koinly 3–9, Explorer 18 Nachkommastellen) gilt als erfüllt – gleiche Toleranz wie der
    Bestandsvergleich der Diagnose (1e-8 bzw. 1e-9 relativ), nie darüber hinaus."""
    return ZERO if abs(diff) <= max(Decimal("1e-8"), abs(ref) * Decimal("1e-9")) else diff


def _stock_points(rep: Report, positions: set[tuple[str, str]]) -> dict[str, list[Decimal]]:
    """Nachweise je Position: Differenz (Nachweis − Ledger) je Referenzzeitpunkt bzw. aktuellem externen Bestand."""
    out: dict[str, list[Decimal]] = {}
    for pos in positions:
        tr = rep.traces.get(pos)
        diffs = [_rounded(p.diff, p.qty) for p in tr.points] if tr is not None else []
        for h in rep.holdings:
            if (h.account, h.asset) == pos and h.observed is not None and h.status in ("extern_ok", "extern_diff"):
                diffs.append(_rounded(h.diff, h.observed) if h.diff is not None else ZERO)
        if diffs:
            out[f"{pos[0]} · {pos[1]}"] = diffs
    out.update(_combined_points(rep, positions))
    return out


def _combined_points(rep: Report, positions: set[tuple[str, str]]) -> dict[str, list[Decimal]]:
    """Nachweise über zusammengefasste Konten: Summe der Ledger-Bestände der Konten zum Nachweiszeitpunkt."""
    from app.diagnosis import audit

    snap, idx = rep.snapshot, rep.index
    out: dict[str, list[Decimal]] = {}
    if snap is None or idx is None:
        return out
    for ref in getattr(snap, "combined", []) or []:
        if not any((m, ref.asset_id) in positions for m in ref.members):
            continue
        at = audit.reference_instant(ref)
        total = ZERO
        for m in ref.members:
            total += audit.soll_at_time(idx, m, ref.asset_id, at) if at is not None else \
                audit.soll_at_date(idx, m, ref.asset_id, ref.as_of)
        out[f"{' + '.join(ref.members)} · {ref.asset_id} (zusammengefasst)"] = [ref.qty - total]
    return out


def _stock_state(base_pts: dict[str, list[Decimal]], pts: dict[str, list[Decimal]]) -> str:
    """Wirkung einer Hypothese auf die Bestandsnachweise: alle erfüllt | unberührt (wie Ausgangslage) | teilweise
    (einzelne Nachweise verändert, nicht alle erfüllt) | widerspruch (ein Nachweis wird schlechter) | kein Nachweis."""
    if not pts:
        return "kein_nachweis"
    for k, diffs in pts.items():
        b = base_pts.get(k, [])
        for i, d in enumerate(diffs):
            if i < len(b) and abs(d) > abs(b[i]):
                return "widerspruch"
    flat = [d for v in pts.values() for d in v]
    if all(d == 0 for d in flat):
        return "alle_erfuellt"
    if pts == base_pts:
        return "unberuehrt"
    return "teilweise"


def evaluate(ctx: Any, report: Report, case: Case, *, need_rep: bool) -> None:
    """Hypothesen rechnen (Ledger auf einer Kopie). ``need_rep``: auch die Diagnose danach (für Nachweise und neue
    Befunde)."""
    snap = report.snapshot
    base_led = snap.ledger
    assets = frozenset(case.assets)
    pos = set(case.positions)
    base_pts = _stock_points(report, pos)
    base_issues = _issue_keys(base_led)
    base_years = _years(base_led)
    for h in case.hyps:
        if h.base:
            h.sim = _Sim(base_led, None, snap.pf, report)  # type: ignore[arg-type]
            h.sig = _signature(base_led, assets)
            h.balances = {k: v for k, v in base_led.balances.items() if k[1] in assets}
            h.years = base_years
            h.stock = "kein_nachweis" if not base_pts else (
                "alle_erfuellt" if all(d == 0 for v in base_pts.values() for d in v) else "unberuehrt")
            h.stock_diffs = {k: [_q(d) for d in v] for k, v in base_pts.items()}
            h.rep_done = True
            continue
        plan = _merged(h.cands)
        pf2 = A.hypothetical(snap.pf, plan)
        led2 = run_ledger(pf2, base_led.options)
        sim = _Sim(led2, plan, pf2)
        h.sim = sim
        h.sig = _signature(led2, assets)
        h.balances = {k: v for k, v in led2.balances.items() if k[1] in assets}
        h.years = _years(led2)
        h.lot_dates = [x for x in A._lot_date_changes(base_led, led2) if x[1] in assets]
        issues = _issue_keys(led2)
        new_issues = [m for k, m in A._issue_keys(led2).items() if k not in base_issues]
        if new_issues:
            h.ok = False
            h.reasons.append("neues Ledger-Problem: " + new_issues[0][:120])
        neg = sorted({(k[3], k[2]) for k in issues - base_issues if k[0] == "negative_balance"})
        if neg:
            h.ok = False
            h.reasons.append("neuer negativer Bestand: " + ", ".join(f"{a} auf {acc}" for acc, a in neg[:3]))
        if need_rep or base_pts:
            ensure_rep(report, case, h)


def ensure_rep(report: Report, case: Case, h: Hyp) -> None:
    """Diagnose nach der Hypothese (Bestandsnachweise, behobene und neue Befunde) – nur bei Bedarf, da aufwendiger
    als die reine Ledger-Rechnung."""
    if h.rep_done or h.base:
        return
    h.rep_done = True
    rep2 = h.sim.rep(report)
    pos = set(case.positions)
    base_pts = {k: [Decimal(x) for x in v] for k, v in case.baseline.stock_diffs.items()}
    pts = _stock_points(rep2, pos)
    h.stock = _stock_state(base_pts, pts)
    h.stock_diffs = {k: [_q(d) for d in v] for k, v in pts.items()}
    now_ids = {f.id for f in rep2.findings}
    h.resolved = [f.id for f in report.findings if f.id not in now_ids]
    base_ids = {f.id for f in report.findings}
    fresh = [f for f in rep2.findings if f.id not in base_ids and f.status != "hinweis" and f.kind not in ("price",)]
    # Folgefragen derselben Position (Dublette/Transfer) waren bisher von der Dublette verdeckt: sie sind kein
    # neues Problem, sondern der nächste Schritt – der Folgelauf bewertet sie eigenständig
    h.successors = [f.title[:100] for f in fresh if f.kind in ("duplicate", "transfer") and _positions(f) & pos]
    h.new_findings = [f.title[:100] for f in fresh if f.title[:100] not in h.successors]
    if h.new_findings:
        h.ok = False
        h.reasons.append("neuer Befund: " + h.new_findings[0])
    if h.stock in ("widerspruch",):
        h.ok = False
        h.reasons.append("widerspricht einem Bestandsnachweis (Zwischen- oder Endbestand)")


# ----------------------------------------------------------------------------------------------------
# Fall aufbauen
# ----------------------------------------------------------------------------------------------------

def build_case(ctx: Any, report: Report, items: list[Finding], *, limit: int = DEFAULT_LIMIT,
               synth: dict[str, A.Plan] | None = None) -> Case:
    """Dossier einer Gruppe: Kandidaten, Hypothesen, Entscheidung. Rein lesend."""
    positions: set[tuple[str, str]] = set()
    txs: set[str] = set()
    for f in items:
        positions |= _positions(f)
        txs |= _tx_ids(f)
    accounts = sorted({a for a, _s in positions})
    assets = sorted({s for _a, s in positions})
    key = case_key(positions)
    kinds = sorted({f.kind for f in items})
    title = f"{', '.join(assets[:3])}{'…' if len(assets) > 3 else ''} auf {', '.join(accounts[:3])}" \
            f"{'…' if len(accounts) > 3 else ''}"
    case = Case(key=key, kind="group", title=title, findings=items[:MAX_FINDINGS], positions=sorted(positions),
                accounts=accounts, assets=assets, tx_ids=sorted(txs), version=A.data_version(ctx.db))
    case.evidence = [f"{len(items)} Befund(e): " + ", ".join(f"{KINDS_SHORT.get(k, k)}" for k in kinds)]
    snap_txs = report.snapshot.pf.txs if report.snapshot is not None else []
    case.deriv = DV.dossier(snap_txs, positions)
    case.evidence += composition(report, positions) + case.deriv
    # Kandidaten
    per_finding: list[list[Cand]] = []
    for f in sorted(items, key=lambda x: x.id):
        if (f.data or {}).get("type") == "margin_pair":
            plan = (synth or {}).get(f.id)
            if plan is not None and not plan.errors:
                cs0 = [Cand(f.id, "link_margin", plan.option.label, plan, _cand_txs(plan, f), _plan_positions(plan, f),
                            "betrag", list(f.evidence[:3]), _action_type(plan), False, False, f)]
                per_finding.append(cs0)
            else:
                case.excluded.append((f.title[:100], "; ".join(plan.errors) if plan else "Plan nicht bildbar"))
            continue
        rec = recommend(report, f)
        cs: list[Cand] = []
        for opt in rec.fixes:
            if opt.key not in CAND_OPTIONS:
                continue
            label = f"{f.kind_label}: {opt.label}"
            params, why = _auto_params(f, opt)
            if params is None:
                case.excluded.append((label, why))
                continue
            plan = A.build_plan(ctx, report, f, opt.key, params, given=True) if params else \
                A.build_plan(ctx, report, f, opt.key)
            if plan.errors:
                case.excluded.append((label, "; ".join(plan.errors)[:200]))
                continue
            ev, notes, fee_ok = _evidence(report, f, plan)
            cs.append(Cand(f.id, opt.key, opt.label, plan, _cand_txs(plan, f), _plan_positions(plan, f), ev, notes,
                           _action_type(plan), fee_ok, opt.recommended, f))
        if cs:
            per_finding.append(cs)
        else:
            case.excluded.append((f"{f.kind_label}: {f.title[:80]}", "keine aus Belegen ableitbare Lösung"))
    case.cands = [c for cs in per_finding for c in cs]
    # Hypothesen (deterministisch, begrenzt)
    raw = 1
    for cs in per_finding:
        raw *= len(cs) + 1
    case.total_hyps = raw
    hyps: list[Hyp] = [Hyp(())]
    truncated = False
    if raw > MAX_PRODUCT:
        truncated = True
    else:
        for combo in itertools.product(*[[None, *cs] for cs in per_finding]):
            chosen = tuple(c for c in combo if c is not None)
            if not chosen:
                continue
            used: set[str] = set()
            clash = False
            for c in chosen:
                if used & c.txs:
                    clash = True
                    break
                used |= c.txs
            if clash:
                continue
            hyps.append(Hyp(chosen))
            if len(hyps) > limit:
                truncated = True
                break
    if truncated:
        # Prüfumfang überschritten: nur Ausgangslage und Einzelwirkungen rechnen, keine Auswahl
        hyps = [Hyp(())] + [Hyp((c,)) for c in case.cands[:limit - 1]]
    case.hyps = hyps
    case.evidence_fp = evidence_fp(ctx, items, positions)
    evaluate(ctx, report, case, need_rep=False)
    case.dims = dimensions(report, case)
    case.decision = decide(ctx, report, case, truncated)
    if case.decision.kind == "ask_stock":
        from app.diagnosis import stockconfirm

        case.question = stockconfirm.question(report, case)
    case.status = status_of(case)
    return case


KINDS_SHORT = {"duplicate": "Dublette", "transfer": "Transfer", "holdings": "Bestandsabweichung",
               "loss": "Abgang", "reconstruction": "Rekonstruktion", "migration": "Migration"}


_COMP_LABEL = {"handel": "Handel", "ein": "Einzahlungen", "aus": "Auszahlungen", "transfer": "Transfers",
               "ertrag": "Erträge", "derivat": "Derivate", "gebuehr": "Gebühren"}


def composition(report: Report, positions: set[tuple[str, str]]) -> list[str]:
    """Wie setzt sich der Ledger-Bestand einer Position aus den vorhandenen Buchungen zusammen? Rein aus den Buchungen,
    nach Art getrennt; Buchungen ohne Uhrzeit (nur Datum bzw. Platzhalterzeit) werden benannt – fehlende
    Abrechnungszeitpunkte werden nicht ergänzt."""
    idx = report.index
    if idx is None:
        return []
    from app.diagnosis.engine import NEUTRAL_TAGS

    parts: dict[tuple[str, str], dict[str, list[Decimal]]] = {p: {} for p in positions}
    no_time: dict[tuple[str, str], int] = defaultdict(int)

    def add(pos: tuple[str, str], cat: str, q: Decimal, t: Any) -> None:
        if pos not in parts:
            return
        cur = parts[pos].setdefault(cat, [ZERO, ZERO])
        cur[0] += q
        cur[1] += 1
        if idx.placeholder_time(t):
            no_time[pos] += 1

    for t in idx.txs:
        if t.origin == "plan":
            continue
        tag = (t.tag or "").lower()
        if t.type in ("buy", "sell", "trade", "swap", "conversion", "convert"):
            cat = "handel"
        elif t.type == "transfer":
            cat = "transfer"
        elif tag in DV.C.DERIVATIVE_TAGS:
            cat = "derivat"
        elif tag and tag not in NEUTRAL_TAGS:
            cat = "ertrag" if t.type == "deposit" else "aus"
        else:
            cat = "ein" if t.type == "deposit" else "aus"
        if t.from_account and t.from_asset and t.from_qty:
            add((t.from_account, t.from_asset), cat, -t.from_qty, t)
        if t.to_account and t.to_asset and t.to_qty:
            add((t.to_account, t.to_asset), cat, t.to_qty, t)
        if t.fee_asset and t.fee_qty:
            acc = t.from_account or t.to_account
            if acc:
                add((acc, t.fee_asset), "gebuehr", -t.fee_qty, t)
    out = []
    for pos in sorted(parts):
        if not parts[pos]:
            continue
        total = sum((v[0] for v in parts[pos].values()), ZERO)
        txt = "; ".join(f"{_COMP_LABEL[k]} {v[0]:+f} ({int(v[1])}×)" for k, v in sorted(parts[pos].items()))
        line = f"Zusammensetzung {pos[0]} · {pos[1]}: {txt}; Summe {total:+f}."
        if no_time[pos]:
            line += (f" {no_time[pos]} Buchung(en) nur mit Datum bzw. Platzhalterzeit – Abrechnungszeitpunkt fehlt "
                     "und wird nicht ergänzt.")
        out.append(line)
    return out


def evidence_fp(ctx: Any, items: list[Finding], positions: set[tuple[str, str]]) -> str:
    """Prüfsumme der Evidenz einer Gruppe: Befunddaten und Bestandsnachweise der Positionen. Ändert sie sich, ist neue
    Evidenz da – frühere Entscheidungen (Rückstellung, Ablehnung) gelten dann nicht mehr blind."""
    refs = []
    for r in ctx.db.q("SELECT id, account, asset_id, qty, as_of, as_of_ts FROM reference_balance WHERE status='active' "
                      "ORDER BY id"):
        if (r["account"], r["asset_id"]) in positions:
            refs.append((r["id"], r["qty"], r["as_of"], r["as_of_ts"]))
    payload = json.dumps({"f": sorted((f.id, A.fingerprint(f)) for f in items), "r": refs}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# ----------------------------------------------------------------------------------------------------
# Entscheidung
# ----------------------------------------------------------------------------------------------------

def _equiv(a: Hyp, b: Hyp) -> bool:
    return a.sig == b.sig


def _source_rank(report: Report, tx_id: str) -> int:
    """Quellenpriorität (bestehende Regel): Börsen-/Wallet-API vor sonstiger App-Erfassung vor Datei-Import."""
    idx = report.index
    t = idx.by_id.get(tx_id) if idx is not None else None
    if t is None:
        return 0
    meta = idx.meta.get(tx_id) if idx is not None else None
    src = (meta.source if meta is not None else t.source) or ""
    if "sync" in src or (t.source_ref or "").startswith("portfolia:sync"):
        return 2
    return 1 if t.origin == "journal" else 0


def _kept(h: Hyp, case: Case) -> set[str]:
    dropped = {op.target for c in h.cands for op in c.plan.ops if op.kind in ("hide", "cover")}
    return {t for c in h.cands for t in c.txs} - dropped


def choose_variant(report: Report, case: Case, variants: list[Hyp]) -> Hyp | None:
    """Darstellungsvariante einer Deutung: bevorzugte Lösung der Empfehlung; sonst – nur bei vollständig gleicher
    wirtschaftlicher Wirkung – die Variante, deren verbleibende Buchungen die höhere Quellenpriorität haben."""
    pref = [h for h in variants if all(c.recommended for c in h.cands)]
    if len(pref) == 1:
        return pref[0]
    if len({repr(h.sig) for h in variants}) != 1:
        return None
    ranked = sorted(variants, key=lambda h: -min((_source_rank(report, t) for t in _kept(h, case)), default=0))
    top = [h for h in ranked if min((_source_rank(report, t) for t in _kept(h, case)), default=0)
           == min((_source_rank(report, t) for t in _kept(ranked[0], case)), default=0)]
    return ranked[0] if len(top) == 1 else None


def _pick(report: Report, case: Case, cs: list[Cand]) -> tuple[Cand | None, str]:
    """Bester belegter Kandidat eines Befunds. Varianten derselben Deutung (``FAMILY``) entscheidet die vorhandene
    Quellenpriorität (bevorzugte Lösung der Empfehlung); verschiedene Deutungen oder gleichwertige Darstellungen ohne
    Vorrang bleiben offen."""
    top = max(_RANK[c.evidence] for c in cs)
    if top == 0:
        return None, "kein Beleg"
    tops = [c for c in cs if _RANK[c.evidence] == top]
    if len({c.family for c in tops}) > 1:
        return None, "konkurrierende Deutungen"
    if len(tops) == 1:
        return tops[0], ""
    rec = [c for c in tops if c.recommended]
    if len(rec) == 1:
        return rec[0], ""
    singles = [_hyp_for(case, (c,)) for c in tops]
    if all(h is not None for h in singles):
        win = choose_variant(report, case, [h for h in singles if h is not None])
        if win is not None:
            return win.cands[0], ""
    return None, "gleichwertige Darstellungen ohne Vorrang"


def _hyp_for(case: Case, chosen: tuple[Cand, ...]) -> Hyp | None:
    want = tuple(sorted(c.key for c in chosen))
    return next((h for h in case.hyps if tuple(sorted(c.key for c in h.cands)) == want), None)


def decide(ctx: Any, report: Report, case: Case, truncated: bool) -> Decision:
    """Entscheidungsregel (siehe Modul-Doku). Rein deterministisch."""
    nonbase = [h for h in case.hyps if not h.base]
    base = case.baseline
    if truncated:
        return Decision("umfang", reason=f"Prüfumfang überschritten: {case.total_hyps} Kombinationen "
                                         f"(Grenze {DEFAULT_LIMIT}); nur Einzelwirkungen gerechnet, keine Auswahl.")
    if not case.findings:
        return Decision("resolved", reason="keine offenen Befunde")
    if not nonbase:
        return Decision("need_info", reason="Aus den vorhandenen Belegen lässt sich kein Lösungskandidat ableiten. "
                                            "Eine Bestandsdifferenz allein begründet keine Buchung.",
                        missing=["Beleg für eine Lösung (Hash/Referenz einer Gegenbuchung, Anbieterdaten oder "
                                 "Beleg-Dokument)"])
    ok = [h for h in nonbase if h.ok]
    if base.stock == "alle_erfuellt" and all(h.stock == "widerspruch" for h in nonbase):
        return Decision("confirm_partial", reason="Der Bestandsnachweis stimmt mit den Buchungen ohne Änderung überein "
                                                  "und widerspricht allen Lösungskandidaten. Die Vermutung ist damit "
                                                  "nicht bestätigt; es wird nichts geändert.",
                        missing=["ausdrückliche Entscheidung des Nutzers (Vermutung ablehnen) oder weitere Belege"])
    if not ok:
        return Decision("blocked", reason="Alle Lösungskandidaten scheitern in der gemeinsamen Simulation: "
                                          + "; ".join(sorted({r for h in nonbase for r in h.reasons})[:3]))
    by_finding: dict[str, list[Cand]] = defaultdict(list)
    for c in case.cands:
        by_finding[c.finding_id].append(c)
    picks = {fid: _pick(report, case, cs) for fid, cs in sorted(by_finding.items())}
    # Pfad A: Identität belegt (gemeinsamer Hash/Quellreferenz) – maßgeblich ist der Beleg
    ident = tuple(c for c, _why in picks.values() if c is not None and c.evidence in ("identitaet", "herkunft"))
    if ident:
        h_star = _hyp_for(case, ident)
        if h_star is None:
            return Decision("blocked", reason="Belegte Deutungen überschneiden sich bei denselben Buchungen.",
                            missing=["Entscheidung, welche Deutung gilt"])
        ensure_rep(report, case, h_star)
        rules = _rules(case, h_star, "identitaet")
        if not h_star.ok or not all(r[1] for r in rules):
            return Decision("blocked", h_star, "Beleglage eindeutig, aber die gemeinsame Simulation oder die "
                                               "Aktionsart steht der Übernahme entgegen.", rules)
        what = "Herkunft" if any(c.evidence == "herkunft" for c in h_star.cands) else "Identität"
        return Decision("apply", h_star, f"{what} belegt, gemeinsame Simulation unauffällig.", rules)
    # Pfad B: rechnerisch erklärt – ein Bestandsnachweis muss genau eine Hypothese erfüllen
    calc = [h for h in ok if all(_RANK[c.evidence] >= 2 for c in h.cands)]
    for h in ok:
        ensure_rep(report, case, h)
    ok = [h for h in ok if h.ok]
    calc = [h for h in calc if h.ok]
    pts_exist = bool(base.stock_diffs)
    if not pts_exist:
        if discriminating_positions([base, *ok]):
            return Decision("ask_stock", reason="Mehrere Deutungen führen zu unterschiedlichen Beständen; ein "
                                                "Bestandsnachweis würde sie unterscheiden.",
                            missing=["Bestandsnachweis der unterscheidenden Position"])
        return Decision("need_info", reason="Die Deutungen unterscheiden sich in keinem Bestand; Belege für Identität "
                                            "oder Gebühr fehlen.",
                        missing=["Gemeinsamer Hash bzw. Quellreferenz der Gegenbuchung", "Beleg der Gebühr"])
    fit = [h for h in ok if h.stock == "alle_erfuellt"]
    if base.stock == "alle_erfuellt":
        return Decision("confirm_partial", reason="Der Bestand ist bestätigt und stimmt auch ohne Änderung – er "
                                                  "unterscheidet die Deutungen nicht. Zuordnung bzw. Ursache bleibt "
                                                  "offen; es wird nichts gebucht.",
                        missing=["Identitätsbeleg (Hash/Quellreferenz) oder Beleg der Gebührenursache"])
    fit_calc = [h for h in fit if h in calc]
    if not fit_calc:
        if fit:
            return Decision("need_info", reason="Der Bestandsnachweis wird nur von Deutungen erfüllt, deren Ursache "
                                                "nicht belegt ist (z. B. Differenz ohne dokumentierte Gebühr).",
                            missing=["Beleg der Ursache der Differenz"])
        return Decision("need_info", reason="Der Bestandsnachweis wird von keiner belegten Deutung erfüllt; die "
                                            "Differenz bleibt ungeklärt.",
                        missing=["Weitere Belege (fehlender Vorgang, Gebühr, Zwischenbestand)"])
    # Deutung = Wahl der Familie je Befund; Varianten derselben Deutung (z. B. welche Seite bleibt) entscheidet die
    # vorhandene Quellenpriorität (bevorzugte Lösung)
    interps: dict[tuple[tuple[str, str], ...], list[Hyp]] = defaultdict(list)
    for h in fit_calc:
        interps[tuple(sorted((c.finding_id, c.family) for c in h.cands))].append(h)
    if len(interps) > 1:
        return Decision("confirm_partial", reason="Mehrere Deutungen erfüllen den bestätigten Bestand. Die Menge ist "
                                                  "bestätigt; die historische Zuordnung bleibt offen.",
                        missing=["Beleg, der die Deutungen unterscheidet"])
    variants = next(iter(interps.values()))
    h_star = variants[0] if len(variants) == 1 else choose_variant(report, case, variants)
    if h_star is None:
        return Decision("need_info", reason="Darstellungen mit unterschiedlicher Wirkung oder ohne Vorrang – bitte die "
                                            "geltende wählen.",
                        missing=["Wahl der Darstellung (welche Buchung bleibt maßgeblich)"])
    rules = _rules(case, h_star, "bestand")
    if not h_star.ok or not all(r[1] for r in rules):
        return Decision("blocked", h_star, "Bestand und Rechnung passen, aber Simulation oder Aktionsart stehen der "
                                           "Übernahme entgegen.", rules)
    return Decision("apply", h_star, "Rechnerisch erklärt und durch den Bestandsnachweis als einzige Deutung "
                                     "bestätigt.", rules)


def _rules(case: Case, h: Hyp, path: str) -> list[tuple[str, bool, str]]:
    base = case.baseline
    rules: list[tuple[str, bool, str]] = []
    if path == "identitaet":
        rules.append(("1 Belege", True, "Herkunft der Menge belegt (gebuchte Zugänge = Transfermenge)"
                      if any(c.evidence == "herkunft" for c in h.cands) else
                      "Identität belegt (gemeinsamer Hash bzw. Quellreferenz)"))
        rules.append(("2 Eindeutigkeit", True, "keine konkurrierende Deutung derselben Buchungen"))
    else:
        ok_calc = all(_RANK[c.evidence] >= 2 for c in h.cands)
        rules.append(("1 Belege", ok_calc and h.stock == "alle_erfuellt" and base.stock != "alle_erfuellt",
                      "rechnerisch exakt erklärt und vom Bestandsnachweis bestätigt (keine Identität)"))
        mine = tuple(sorted((c.finding_id, c.family) for c in h.cands))
        rivals = [x for x in case.hyps if x is not h and not x.base and x.ok and x.stock == "alle_erfuellt"
                  and tuple(sorted((c.finding_id, c.family) for c in x.cands)) != mine]
        rules.append(("2 Eindeutigkeit", not rivals, "keine andere Deutung erfüllt den Nachweis" if not rivals
                      else f"{len(rivals)} weitere Deutung(en) erfüllen den Nachweis"))
    mixed = h.stock == "teilweise"
    rules.append(("3 gemeinsame Simulation", h.ok and not mixed,
                  "keine Folgeprobleme, Zwischen- und Endbestände widerspruchsfrei" if h.ok and not mixed else
                  ("Zwischen-/Endbestand widersprüchlich" if mixed else "; ".join(h.reasons[:2]))))
    safe = all(c.action in AUTO_TYPES for c in h.cands)
    rules.append(("4 nur Verknüpfen/Ausblenden", safe, "vorhandene Buchungen werden verknüpft/ausgeblendet" if safe
                  else "Plan enthält Buchungen anderer Art – nur mit ausdrücklicher Bestätigung"))
    return rules


def _discriminating(hyps: list[Hyp]) -> bool:
    """Gibt es eine Position, auf der zulässige Hypothesen (inkl. Ausgangslage) verschiedene Bestände ergeben?"""
    return bool(discriminating_positions(hyps))


def discriminating_positions(hyps: list[Hyp]) -> list[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for h in hyps:
        keys |= set(h.balances)
    out = []
    for k in sorted(keys):
        vals = {format(h.balances.get(k, ZERO).normalize(), "f") for h in hyps}
        if len(vals) > 1:
            out.append(k)
    return out


# ----------------------------------------------------------------------------------------------------
# Qualitätsdimensionen
# ----------------------------------------------------------------------------------------------------

def dimensions(report: Report, case: Case, *, resolved: bool = False) -> dict[str, tuple[str, str]]:
    """Getrennte Zustände (geklärt | offen | unbekannt | nicht betroffen) mit Text – ein offener Punkt lässt
    abgeschlossene nicht als ungelöst erscheinen und umgekehrt."""
    d: dict[str, tuple[str, str]] = {}
    kinds = {f.kind for f in case.findings}
    pts = _stock_points(report, set(case.positions))
    # Bestand/Menge
    if not pts:
        d["bestand"] = ("unbekannt", "Kein Bestandsnachweis (Referenz/Anbieterstand) für die Positionen – ein "
                                     "fehlender Nachweis gilt als unbekannt, nicht als 0.")
    elif all(x == 0 for v in pts.values() for x in v):
        d["bestand"] = ("geklaert", "Bestand zu allen Nachweiszeitpunkten bestätigt.")
    else:
        bad = [k for k, v in pts.items() if any(x != 0 for x in v)]
        d["bestand"] = ("offen", "Abweichung zum Nachweis: " + ", ".join(bad[:3]))
    # Transfer / Dublette
    all_tr = [f for f in case.findings if f.kind in ("transfer", "duplicate")]
    open_tr = [] if resolved else all_tr
    d["transfer"] = ("offen", f"{len(open_tr)} offene Transfer-/Dublettenfrage(n)") if open_tr else \
        ("geklaert", "Transfer/Dublette entschieden und übernommen") if all_tr else \
        ("n/a", "keine Transfer-/Dublettenfrage")
    # Gebühren
    unexplained = [c for c in case.cands if c.evidence == "betrag" and any("nicht als Gebühr belegt" in n
                                                                           for n in c.notes)]
    if unexplained:
        d["gebuehren"] = ("offen", "; ".join(n for c in unexplained for n in c.notes if "Gebühr" in n)[:200])
    elif any(c.fee_explained for c in case.cands):
        d["gebuehren"] = ("geklaert", "Gebühr dokumentiert und rechnerisch berücksichtigt.")
    else:
        d["gebuehren"] = ("n/a", "keine Gebührenfrage im Fall")
    # Vorgangsart
    if kinds & {"loss", "migration"}:
        d["vorgangsart"] = ("offen", "Wirtschaftliche Art des Vorgangs (Verlust/Migration) nicht belegt.")
    else:
        d["vorgangsart"] = ("geklaert", "Vorgangsart aus den Buchungen eindeutig.") if not open_tr else \
            ("offen", "Deutung der Buchungen (Dublette/Transfer) noch nicht entschieden.")
    # Anschaffungsdaten
    led = report.snapshot.ledger
    assets = set(case.assets)
    miss = [x for x in led.disposals if x.asset in assets and x.missing_basis]
    phantom = [x for x in led.lots if x.asset in assets and x.origin == "phantom"]
    idx = report.index
    unknown = [t for t in (idx.txs if idx is not None else []) if t.to_asset in assets and t.to_qty
               and t.value_eur is None and t.type in ("deposit", "buy", "trade", "swap") and t.origin != "plan"
               and (t.tag or "").lower() not in DV.C.DERIVATIVE_TAGS and not idx.asset(t.to_asset).is_fiat]
    if miss or phantom or unknown:
        d["anschaffung"] = ("offen", f"{len(miss) + len(phantom) + len(unknown)} Zugang/Zugänge ohne belegten EUR-Wert "
                                     "bzw. Anschaffungsdaten (Kostenbasis unbekannt – wird nicht als 0 € behandelt; "
                                     "ein bestätigter Bestand ersetzt sie nicht).")
    else:
        d["anschaffung"] = ("geklaert", "Anschaffungsdaten der betroffenen Lots liegen vor.")
    # Historische Vollständigkeit
    hist = [] if resolved else [f for f in case.findings if f.kind in ("reconstruction",)]
    traces = [report.traces[p] for p in case.positions if p in report.traces]
    dev = [t for t in traces if t.deviating]
    if hist or dev:
        d["historie"] = ("offen", "Historie der Position nicht vollständig erklärt "
                                  f"({len(hist)} Rekonstruktionskandidat(en), {len(dev)} Abweichungsfenster).")
    else:
        d["historie"] = ("unbekannt" if not traces else "geklaert",
                         "Kein Zwischennachweis vorhanden." if not traces else "Zwischenbestände passen.")
    if case.deriv:
        d["vorgangsart"] = ("offen", "Derivatebuchungen im Fall: wirtschaftliche Art und Anbietersemantik nicht "
                                     "verifiziert (Doppelerfassung von Gebühren/Funding nicht auszuschließen).")
    # Steuerliche Einordnung
    if case.deriv:
        d["steuer"] = ("offen", "Steuerliche Behandlung von Derivaten ist nicht implementiert – bleibt offen.")
    elif d["anschaffung"][0] == "offen" or d["vorgangsart"][0] == "offen":
        d["steuer"] = ("offen", "Steuerliche Einordnung hängt von offenen Punkten ab (Anschaffung/Vorgangsart).")
    else:
        d["steuer"] = ("unbekannt", "Steuerliche Wertung erfolgt durch das Regelwerk; hier nicht bewertet.")
    return d


def dims_text(dims: dict[str, Any]) -> str:
    """Kurzfassung der getrennten Zustände, z. B. „Bestand, Transfer geklärt. Offen: Anschaffungsdaten (…)“."""
    done = [DIM_LABEL[k].split("/")[0].split(" und ")[0] for k in DIM_ORDER if k in dims and dims[k][0] == "geklaert"]
    open_ = [f"{DIM_LABEL[k]} ({dims[k][1]})" for k in DIM_ORDER if k in dims and dims[k][0] == "offen"]
    unk = [DIM_LABEL[k] for k in DIM_ORDER if k in dims and dims[k][0] == "unbekannt"]
    parts = []
    if done:
        parts.append(", ".join(done) + " geklärt.")
    if open_:
        parts.append("Offen: " + "; ".join(open_) + ".")
    if unk:
        parts.append("Unbekannt: " + ", ".join(unk) + ".")
    return " ".join(parts)


def status_of(case: Case) -> str:
    dec = case.decision
    assert dec is not None
    if dec.kind == "resolved":
        return "geloest"
    if dec.kind == "umfang":
        return "umfang"
    if dec.kind == "apply":
        return "bereit"
    if dec.kind == "ask_stock":
        return "wartet_bestand"
    if dec.kind == "blocked":
        return "blockiert"
    if dec.kind == "confirm_partial":
        return "teilweise"
    return "wartet_info"


# ----------------------------------------------------------------------------------------------------
# Analyse und Persistenz
# ----------------------------------------------------------------------------------------------------

def _now() -> str:
    return A._now()


def analyse(ctx: Any, report: Report | None = None, *, limit: int | None = None, budget: int | None = None,
            only: str | None = None) -> list[Case]:
    """Alle offenen Klärungsgruppen untersuchen (rein lesend, deterministisch). Das Simulationsbudget je Durchgang
    begrenzt die Rechenzeit; Gruppen, die nicht mehr hineinpassen, bleiben ``wartet_info`` mit Hinweis auf den
    Folgelauf."""
    report = report or report_for(ctx)
    limit = int(limit or _setting(ctx, "clarify.limit", DEFAULT_LIMIT))
    budget = int(budget or _setting(ctx, "clarify.budget", DEFAULT_BUDGET))
    findings = [f for f in TR.open_findings(ctx, report, cases=True) if f.kind in CLARIFY_KINDS
                and (not TR.is_hint(f) or f.kind in ("duplicate", "transfer"))]
    synth_f, synth_p = margin_items(ctx, report)
    findings += synth_f
    out: list[Case] = []
    used = 0
    prio: dict[str, tuple[int, str]] = {}
    for r in ctx.db.q("SELECT case_key, updated_at, state_json FROM clarify_case"):
        try:
            was_deferred = bool(json.loads(r["state_json"] or "{}").get("deferred"))
        except ValueError:
            was_deferred = False
        prio[r["case_key"]] = (0 if was_deferred else 1, r["updated_at"] or "")
    keyed = []
    for g in groups(findings):
        pos = set()
        for f in g:
            pos |= _positions(f)
        keyed.append((case_key(pos), g, pos))
    # bei begrenztem Budget kommen zuerst die noch nie bzw. zuletzt nicht untersuchten Gruppen (deterministisch)
    keyed.sort(key=lambda x: (prio.get(x[0], (0, "")), x[0]))
    for key, g, pos in keyed:
        if only and key != only:
            continue
        if used >= budget:
            c = _deferred_case(ctx, g, pos, key)
            out.append(c)
            continue
        c = build_case(ctx, report, g, limit=limit, synth=synth_p)
        used += max(1, len(c.hyps))
        out.append(c)
    if not only or only.startswith("un-"):
        out += [c for c in unsupported_cases(ctx) if not only or c.key == only]
    return out


def margin_items(ctx: Any, report: Report) -> tuple[list[Finding], dict[str, A.Plan]]:
    """Margin-Umbuchungspaare als abgeleitete Befunde samt Plan (interner Transfer über die vorhandene Verknüpfung).
    Nur Kandidaten: das Etikett „Margin“ ist ohne verifizierte Anbietersemantik keine Deutung."""
    snap = report.snapshot
    if snap is None or snap.pf is None:
        return [], {}
    pairs = DV.margin_pairs(snap.pf.txs)
    if not pairs:
        return [], {}
    from app.diagnosis.recommend import Option

    st = A._State(ctx, report)
    version = A.data_version(ctx.db)
    out: list[Finding] = []
    plans: dict[str, A.Plan] = {}
    idx = report.index
    for w, d in pairs:
        q = w.from_qty
        f = Finding(kind="transfer", status="hinweis",
                    title=f"Margin-Umbuchung {_q(q)} {w.from_asset}: {w.from_account} → {d.to_account}",
                    known=[f"Beide Seiten tragen das Etikett „margin“, gleiche Menge, Abstand {abs(d.ts - w.ts)}."],
                    uncertainty=["Das Etikett „Margin“ genügt ohne verifizierte Anbietersemantik nicht zur Deutung "
                                 "als interner Transfer."],
                    evidence=[f"{w.tx_id} −{_q(q)} {w.from_asset} ({w.from_account}) ↔ {d.tx_id} +{_q(q)} "
                              f"({d.to_account}); Etikett margin beidseitig, Anbietersemantik nicht verifiziert"],
                    accounts=sorted({w.from_account or "", d.to_account or ""}), assets=[w.from_asset or ""],
                    positions=[(w.from_account or "", w.from_asset or ""), (d.to_account or "", d.to_asset or "")],
                    key=f"margin|{w.tx_id}|{d.tx_id}", data={"type": "margin_pair", "pairs": [[w.tx_id, d.tx_id]]})
        if idx is not None:
            f.txs = [idx.ref(w), idx.ref(d)]
        plan = A.Plan(f, Option("link_margin", "Margin-Umbuchung als internen Transfer verknüpfen",
                                "Behält Anschaffungsdatum und Einstand der Lots (keine neue Anschaffung auf dem "
                                "Derivatekonto)."), {}, version=version)
        A._transfer(st, w, d, plan)
        out.append(f)
        plans[f.id] = plan
    return out, plans


def unsupported_cases(ctx: Any) -> list[Case]:
    """Nicht unterstützte Margin-/Derivatezeilen übersprungener CSV-Stapel: konkrete offene Vorgänge, nie
    „erledigt“."""
    out = []
    for u in DV.unsupported(ctx.db):
        c = Case(key=u.key, kind="unsupported", title=f"{u.count} nicht übernommene Zeile(n): {u.reason[:90]}",
                 findings=[], positions=[], accounts=[], assets=[], tx_ids=[], version=A.data_version(ctx.db),
                 evidence_fp=hashlib.sha256(f"{u.batch_id}|{u.reason}|{u.count}".encode()).hexdigest()[:16])
        c.evidence = [f"Stapel {u.batch_id} ({u.filename}): {u.count} Zeile(n) – {u.reason}",
                      "Die Zeilen wurden nicht übernommen und sind nicht erledigt: Bestand, Gebühren und Ergebnis "
                      "des Kontos können davon abhängen."]
        c.hyps = [Hyp(())]
        c.hyps[0].sig = ()
        c.decision = Decision("need_info", reason="Das Format wird für diese Zeilen nicht unterstützt; sie bleiben "
                                                  "als offener Vorgang bestehen.",
                              missing=["Export in einem unterstützten Format, manuelle Erfassung nach Beleg oder "
                                       "ausdrückliche Entscheidung, die Zeilen nicht zu erfassen"])
        c.dims = {"bestand": ("unbekannt", "Wirkung der Zeilen auf den Bestand unbekannt."),
                  "transfer": ("n/a", "—"), "gebuehren": ("offen", "Gebühren/Zinsen der Zeilen nicht erfasst."),
                  "vorgangsart": ("offen", "Derivatebuchungen nicht erfasst."),
                  "anschaffung": ("n/a", "—"), "historie": ("offen", "Historie unvollständig (Zeilen fehlen)."),
                  "steuer": ("offen", "Steuerliche Behandlung von Derivaten ist nicht implementiert.")}
        c.status = "wartet_info"
        out.append(c)
    return out


def _deferred_case(ctx: Any, g: list[Finding], pos: set[tuple[str, str]], key: str) -> Case:
    accounts = sorted({a for a, _s in pos})
    assets = sorted({s for _a, s in pos})
    c = Case(key=key, kind="group", title=f"{', '.join(assets[:3])} auf {', '.join(accounts[:3])}", findings=g,
             positions=sorted(pos), accounts=accounts, assets=assets,
             tx_ids=sorted(set().union(*(_tx_ids(f) for f in g))),
             version=A.data_version(ctx.db), evidence_fp=evidence_fp(ctx, g, pos))
    c.hyps = [Hyp(())]
    c.decision = Decision("need_info", reason="Simulationsbudget dieses Durchgangs ausgeschöpft – wird im Folgelauf "
                                              "untersucht.", missing=["Folgelauf"])
    c.status = "wartet_info"
    c.deferred = True
    c.dims = dict.fromkeys(DIM_ORDER, ("unbekannt", "noch nicht untersucht"))
    return c


def _row(db: Any, key: str) -> Any:
    return db.q1("SELECT * FROM clarify_case WHERE case_key=?", (key,))


def state_of(case: Case) -> dict[str, Any]:
    dec = case.decision
    return {
        "decision": {"kind": dec.kind if dec else "", "reason": dec.reason if dec else "",
                     "missing": dec.missing if dec else [], "hyp": dec.hyp.id if dec and dec.hyp else "",
                     "rules": [list(r) for r in (dec.rules if dec else [])]},
        "cands": [c.doc() for c in case.cands], "excluded": case.excluded[:20],
        "hyps": [h.doc() for h in case.hyps[:DEFAULT_LIMIT + 1]], "total_hyps": case.total_hyps,
        "evidence": case.evidence, "sources": case.sources, "question": case.question,
        "research": case.research, "deferred": case.deferred,
        "findings": [{"id": f.id, "kind": f.kind, "title": f.title[:140]} for f in case.findings[:MAX_FINDINGS]],
    }


def persist(ctx: Any, cases: list[Case], *, complete: bool = True) -> dict[str, str]:
    """Stand der Fälle speichern; Rückgabe: Fall → Übergang (neu | geöffnet | unverändert). Ein als gelöst geführter
    Fall wird nur erneut geöffnet, wenn die Gruppe wieder auftaucht (neue Evidenz); eine Rückstellung gilt nur, solange
    die Evidenz gleich bleibt."""
    stamp = _now()
    moves: dict[str, str] = {}
    seen = set()
    for c in cases:
        seen.add(c.key)
        row = _row(ctx.db, c.key)
        data = (c.title[:200], json.dumps(c.accounts), json.dumps(c.assets),
                json.dumps([f.id for f in c.findings][:MAX_FINDINGS]), json.dumps(c.tx_ids[:200]),
                json.dumps({k: list(v) for k, v in c.dims.items()}, ensure_ascii=False),
                json.dumps(state_of(c), ensure_ascii=False, default=str), c.evidence_fp, c.version)
        if row is None:
            ctx.db.x("INSERT INTO clarify_case(case_key, kind, status, title, accounts_json, assets_json, "
                     "finding_ids_json, tx_ids_json, dims_json, state_json, evidence_fp, data_version, created_at, "
                     "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (c.key, c.kind, c.status, *data, stamp, stamp))
            moves[c.key] = "neu"
            continue
        status = c.status
        if row["status"] == "zurueck" and row["evidence_fp"] == c.evidence_fp:
            status = "zurueck"  # Rückstellung gilt, solange keine neue Evidenz vorliegt
        if c.deferred:  # nicht untersucht: Stand und Reihenfolge der Warteschlange nicht verändern
            ctx.db.x("UPDATE clarify_case SET state_json=? WHERE case_key=?",
                     (json.dumps(state_of(c), ensure_ascii=False, default=str), c.key))
            moves[c.key] = "unverändert"
            continue
        reopened = row["status"] == "geloest"
        moves[c.key] = "geöffnet" if reopened else "unverändert"
        ctx.db.x("UPDATE clarify_case SET status=?, title=?, accounts_json=?, assets_json=?, finding_ids_json=?, "
                 "tx_ids_json=?, dims_json=?, state_json=?, evidence_fp=?, data_version=?, updated_at=?, "
                 "resolved_at=NULL, opened_count=opened_count+?, reopened_at=COALESCE(?, reopened_at) "
                 "WHERE case_key=?", (status, *data, stamp, 1 if reopened else 0, stamp if reopened else None, c.key))
        if status == "zurueck":
            c.status = "zurueck"
    # nicht mehr vorhandene offene Fälle sind gelöst (Befunde verschwunden) – Nachprüfung hält das fest
    for r in ctx.db.q("SELECT case_key, status FROM clarify_case WHERE status NOT IN ('geloest')") if complete else []:
        if r["case_key"] not in seen:
            ctx.db.x("UPDATE clarify_case SET status='geloest', resolved_at=?, updated_at=? WHERE case_key=?",
                     (stamp, stamp, r["case_key"]))
    return moves


# ----------------------------------------------------------------------------------------------------
# Übernahme (atomar, idempotent, nachgeprüft)
# ----------------------------------------------------------------------------------------------------

@dataclass
class Result:
    ok: bool = False
    message: str = ""
    errors: list[str] = field(default_factory=list)
    token: str = ""
    bulk_id: str = ""
    decision_ids: list[int] = field(default_factory=list)
    verified: bool = False
    undone: bool = False


def _run_token(case_key: str, hyp: Hyp, version: str, fp: str) -> str:
    plans = sorted((c.key, c.plan.token) for c in hyp.cands)
    return hashlib.sha256(json.dumps([case_key, hyp.id, plans, version, fp]).encode()).hexdigest()[:24]


def _doc(case: Case, hyp: Hyp | None, effect: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"state": state_of(case), "applied": hyp.doc() if hyp else None, "effect": effect or {},
            "dims": {k: list(v) for k, v in case.dims.items()}}


def log_run(ctx: Any, case: Case, trigger: str, outcome: str, *, bulk_id: str = "", ids: list[int] | None = None,
            hyp: Hyp | None = None, effect: dict[str, Any] | None = None, token: str | None = None) -> str:
    tok = token or uuid.uuid4().hex[:24]
    ctx.db.x("INSERT OR IGNORE INTO clarify_run(token, case_key, trigger, outcome, detail_json, bulk_id, "
             "decision_ids_json, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
             (tok, case.key, trigger, outcome, json.dumps(_doc(case, hyp, effect), ensure_ascii=False, default=str),
              bulk_id or None, json.dumps(ids or []), "ok", _now()))
    return tok


def apply_hypothesis(ctx: Any, case_key: str, hyp_id: str, trigger: str = "manual", *,
                     authorization: str = "user", expect_version: str | None = None,
                     expect_tokens: dict[str, str] | None = None) -> Result:
    """Hypothese einer Gruppe atomar übernehmen. ``authorization``: ``user`` (ausdrücklich bestätigt) oder ``auto``
    (Automatik-Einstellung; nur automatisch ausführbare Aktionsarten). Unter der Sperre wird alles neu gebildet:
    weicht Datenstand, Plan oder Entscheidung ab, wird nichts geschrieben."""
    with A._LOCK:
        report = report_for(ctx)
        cases = analyse(ctx, report, only=case_key)
        case = next((c for c in cases if c.key == case_key), None)
        if case is None:
            return Result(errors=["Der Fall besteht nicht mehr – nichts zu übernehmen."])
        hyp = next((h for h in case.hyps if h.id == hyp_id and not h.base), None)
        if hyp is None:
            return Result(errors=["Diese Lösung steht nicht mehr zur Verfügung (Datenstand geändert). Es wurde "
                                  "nichts geändert."])
        if expect_version is not None and expect_version != case.version:
            return Result(errors=["Der Datenstand hat sich seit der Vorschau geändert. Es wurde nichts geändert."])
        if expect_tokens is not None and expect_tokens != {c.key: c.plan.token for c in hyp.cands}:
            return Result(errors=["Der Plan hat sich seit der Vorschau geändert. Es wurde nichts geändert."])
        if len(case.hyps) > 1 and case.decision is not None and case.decision.kind == "umfang":
            return Result(errors=["Prüfumfang überschritten – keine Auswahl möglich."])
        if authorization == "auto":
            dec = case.decision
            if dec is None or dec.kind != "apply" or dec.hyp is None or dec.hyp.id != hyp.id:
                return Result(errors=["Keine eindeutig belegte Lösung – keine automatische Übernahme."])
            if not auto_allowed(ctx):
                return Result(errors=["Die Automatik ist für Korrekturen nicht freigegeben."])
        if not hyp.ok:
            return Result(errors=["Die gemeinsame Simulation dieser Lösung erzeugt Folgeprobleme: "
                                  + "; ".join(hyp.reasons[:2])])
        if any(c.action not in AUTO_TYPES and authorization == "auto" for c in hyp.cands):
            return Result(errors=["Diese Aktionsart wird nie automatisch ausgeführt."])
        token = _run_token(case.key, hyp, case.version, case.evidence_fp)
        done = ctx.db.q1("SELECT * FROM clarify_run WHERE token=? AND status='ok' AND outcome='geloest'", (token,))
        if done is not None:
            return Result(ok=True, token=token, bulk_id=done["bulk_id"] or "",
                          message="Diese Klärung wurde bereits übernommen.", verified=True)
        from app.journal.service import journal_service

        js = journal_service(ctx)
        stamp = _now()
        bulk_id = "kl" + token[:14]
        ids: list[int] = []
        try:
            with ctx.db.transaction() as c:
                A.assert_version(c, case.version)  # Datenstand erneut in der Schreibtransaktion prüfen
                for cand in hyp.cands:
                    created: dict[str, str] = {}
                    recs = [A._exec(c, ctx, js, op, stamp, created) for op in sorted(
                        cand.plan.ops, key=lambda o: 0 if o.kind == "create" else 1)]
                    f = cand.finding or report.by_id(cand.finding_id)
                    assert f is not None
                    cur = c.execute(
                        "INSERT INTO diag_decision(finding_id, kind, title, action, option, option_label, "
                        "params_json, ops_json, fingerprint, note, status, created_at) VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (f.id, f.kind, f.title[:300], "fix", cand.plan.option.key, cand.plan.option.label[:200],
                         json.dumps({**cand.plan.params, "bulk": bulk_id, "clarify": token}, ensure_ascii=False),
                         json.dumps(recs, ensure_ascii=False, default=str), A.fingerprint(f),
                         f"Konfliktklärung {case.key} ({authorization})", "active", stamp))
                    ids.append(int(cur.lastrowid))
                js._log(c, "klaerung_uebernommen", f"klaerung:{bulk_id}", None,
                        {"case": case.key, "hyp": hyp.id, "decisions": ids, "authorization": authorization}, stamp)
        except A.Conflict as e:
            return Result(errors=[f"{e} Es wurde nichts geändert."])
        A._after(ctx, any(c.plan.touches_quotes for c in hyp.cands))
    # Nachprüfung außerhalb der Sperre (B.undo nimmt sie selbst)
    ok, effect = verify(ctx, case, hyp)
    if not ok:
        res = B.undo(ctx, bulk_id)
        log_run(ctx, case, trigger, "fehlgeschlagen", bulk_id=bulk_id, ids=ids, hyp=hyp, token=token + "-v",
                effect={**effect, "zurückgenommen": res.ok})
        ctx.db.x("UPDATE clarify_run SET status='undone', undone_at=? WHERE token=?", (_now(), token + "-v"))
        _FLAG["dirty"] = True
        return Result(errors=["Nachprüfung fehlgeschlagen: " + "; ".join(effect.get("probleme", [])[:2])
                              + (" Die Änderung wurde vollständig zurückgenommen." if res.ok else
                                 " Rücknahme nicht möglich – bitte unter Entscheidungen prüfen.")],
                      token=token, bulk_id=bulk_id, undone=res.ok)
    after = dimensions(report_for(ctx), case, resolved=True)
    effect = {**effect, "dims_nach": {k: list(v) for k, v in after.items()}, "zusammenfassung": dims_text(after)}
    log_run(ctx, case, trigger, "geloest", bulk_id=bulk_id, ids=ids, hyp=hyp, effect=effect, token=token)
    ctx.db.x("UPDATE clarify_case SET status='geloest', resolved_at=?, updated_at=?, dims_json=? WHERE case_key=?",
             (_now(), _now(), json.dumps({k: list(v) for k, v in after.items()}, ensure_ascii=False), case.key))
    log.info("Klärung %s übernommen (%s, %d Entscheidungen)", case.key, authorization, len(ids))
    return Result(ok=True, token=token, bulk_id=bulk_id, decision_ids=ids, verified=True,
                  message=f"Übernommen und nachgeprüft: {len(ids)} Korrektur(en), gemeinsam rückgängig zu machen.")


def verify(ctx: Any, case: Case, hyp: Hyp) -> tuple[bool, dict[str, Any]]:
    """Nachprüfung nach der Übernahme: Befunde der Gruppe behoben, nichts Neues, kein Nachweis verschlechtert."""
    rep = report_for(ctx)
    before_ids = {f.id for f in case.findings}
    still = [f for f in rep.findings if f.id in before_ids]
    pos = set(case.positions)
    problems: list[str] = []
    targeted = {c.finding_id for c in hyp.cands}
    left = [f for f in still if f.id in targeted]
    if left:
        problems.append(f"{len(left)} Befund(e) bestehen weiter: {left[0].title[:80]}")
    base_ids = {f.id for f in case.findings}
    new = [f for f in rep.findings if f.kind in CLARIFY_KINDS and f.kind not in ("duplicate", "transfer")
           and f.id not in base_ids and f.status != "hinweis" and _positions(f) & pos]
    if new:
        problems.append(f"neuer Befund: {new[0].title[:80]}")
    idx = rep.index
    if idx is not None:
        gone = [op.target for c in hyp.cands for op in c.plan.ops if op.kind in ("hide", "cover")
                and op.target in idx.by_id]
        if gone:
            problems.append(f"ausgeblendete Buchung zählt weiter: {gone[0]}")
    pts = _stock_points(rep, pos)
    base_pts = _stock_points_cached(case)
    for k, diffs in pts.items():
        for i, d in enumerate(diffs):
            if i < len(base_pts.get(k, [])) and abs(d) > abs(base_pts[k][i]):
                problems.append(f"Bestandsnachweis verschlechtert: {k}")
                break
    return not problems, {"probleme": problems, "behoben": len(targeted) - len(left),
                          "bestand": {k: [_q(d) for d in v] for k, v in pts.items()}}


def _stock_points_cached(case: Case) -> dict[str, list[Decimal]]:
    return {k: [Decimal(x) for x in v] for k, v in case.baseline.stock_diffs.items()}


def undo_run(ctx: Any, token: str) -> Result:
    """Klärung als Ganzes zurücknehmen (alle zusammengehörigen Korrekturen, eine Transaktion)."""
    row = ctx.db.q1("SELECT * FROM clarify_run WHERE token=?", (token,))
    if row is None or not row["bulk_id"]:
        return Result(errors=["Unbekannte Klärung."])
    if row["status"] == "undone":
        return Result(errors=["Diese Klärung ist bereits zurückgenommen."])
    res = B.undo(ctx, row["bulk_id"])
    if not res.ok:
        return Result(errors=res.errors)
    ctx.db.x("UPDATE clarify_run SET status='undone', undone_at=? WHERE token=?", (_now(), token))
    ctx.db.x("UPDATE clarify_case SET status='wartet_info', resolved_at=NULL, updated_at=?, reopened_at=?, "
             "opened_count=opened_count+1 WHERE case_key=?", (_now(), _now(), row["case_key"]))
    _FLAG["dirty"] = True
    return Result(ok=True, token=token, bulk_id=row["bulk_id"], message="Klärung zurückgenommen – die Befunde "
                                                                       "erscheinen wieder.")


# ----------------------------------------------------------------------------------------------------
# Durchgang (Automatik), Folgelauf
# ----------------------------------------------------------------------------------------------------

def notify_change() -> None:
    """Neue Daten sind eingetroffen (Import, Abruf, Nachweis): ein laufender Durchgang wird danach wiederholt, ein
    künftiger sieht sie ohnehin."""
    _FLAG["dirty"] = True


def pending_followup() -> bool:
    return bool(_FLAG["dirty"])


def run(ctx: Any, trigger: str = "auto", *, force_apply: bool = False, research: bool | None = None,
        max_rounds: int = 3) -> dict[str, Any] | None:
    """Ein Klärungsdurchgang: untersuchen, (Recherche), automatisch übernehmen was zulässig ist, nachprüfen, Folgelauf
    bei zwischenzeitlich eingetroffenen Daten. Parallele Durchgänge: der zweite kehrt sofort zurück und merkt einen
    Folgelauf vor."""
    if not _setting(ctx, "clarify.enabled", True):
        return None
    if not _RUN.acquire(blocking=False):
        _FLAG["dirty"] = True
        return None
    try:
        summary: dict[str, Any] = {"rounds": 0, "applied": [], "cases": 0, "by_status": {}, "researched": []}
        for _round in range(max_rounds):
            _FLAG["dirty"] = False
            start_version = A.data_version(ctx.db)
            summary["rounds"] += 1
            _one_round(ctx, trigger, force_apply, research, summary)
            if not _FLAG["dirty"] and A.data_version(ctx.db) == summary.get("end_version", start_version) \
                    and summary.get("round_changed") is False:
                break
            if not _FLAG["dirty"] and not summary.get("round_changed"):
                break
        summary["followup_pending"] = bool(_FLAG["dirty"])
        return summary
    finally:
        _RUN.release()


def _one_round(ctx: Any, trigger: str, force_apply: bool, research: bool | None, summary: dict[str, Any]) -> None:
    report = report_for(ctx)
    cases = analyse(ctx, report)
    summary["cases"] = len(cases)
    changed = False
    # Recherche nur im Betrieb (mit Scheduler) oder auf ausdrückliche Anforderung – nie als Nebenwirkung von Tests
    calls = [MAX_RESEARCH]  # Abrufe je Durchgang (deterministisch begrenzt)
    do_research = (bool(_setting(ctx, "clarify.research", True)) and getattr(ctx, "scheduler", None) is not None) \
        if research is None else research
    for c in cases:
        if do_research and not c.deferred and c.decision is not None \
                and c.decision.kind in ("need_info", "ask_stock", "blocked"):
            c.research = request_evidence(ctx, c, calls)
            if c.research:
                summary["researched"].append(c.key)
            block = [n for n in c.research if n.startswith("Blockade")]
            if block:  # konkrete Blockade statt allgemeinem „später nochmal“
                c.decision.missing = [*c.decision.missing, *block]
    moves = persist(ctx, cases)
    for c in cases:
        dec = c.decision
        c_summary = {"key": c.key, "status": c.status, "move": moves.get(c.key, "")}
        if dec is not None and dec.kind == "apply" and dec.hyp is not None and c.status != "zurueck" \
                and (auto_allowed(ctx) or force_apply):
            res = apply_hypothesis(ctx, c.key, dec.hyp.id, trigger, authorization="auto" if auto_allowed(ctx)
                                   else "user", expect_version=c.version)
            c_summary["applied"] = res.ok
            c_summary["message"] = res.message or "; ".join(res.errors)
            if res.ok:
                summary["applied"].append({"case": c.key, "token": res.token, "n": len(res.decision_ids)})
                changed = True
            else:
                log_run(ctx, c, trigger, "keine_aenderung", effect={"fehler": res.errors})
        else:
            if trigger != "auto" or (dec is not None and dec.kind not in ("resolved",)):
                _log_nochange(ctx, c, trigger)
        summary["by_status"][c.status] = summary["by_status"].get(c.status, 0) + 1
    summary["round_changed"] = changed
    summary["end_version"] = A.data_version(ctx.db)


def _log_nochange(ctx: Any, case: Case, trigger: str) -> None:
    """Dokumentation eines Durchgangs ohne Änderung – nur wenn sich der Stand gegenüber dem letzten Lauf ändert."""
    last = ctx.db.q1("SELECT detail_json FROM clarify_run WHERE case_key=? ORDER BY id DESC LIMIT 1", (case.key,))
    doc = json.dumps(_doc(case, None), ensure_ascii=False, default=str, sort_keys=True)
    if last is not None:
        try:
            prev = json.dumps(json.loads(last["detail_json"]), ensure_ascii=False, default=str, sort_keys=True)
        except ValueError:
            prev = ""
        if prev == doc:
            return
    log_run(ctx, case, trigger, "wartet" if case.status != "umfang" else "umfang")


def request_evidence(ctx: Any, case: Case, budget: list[int] | None = None) -> list[str]:
    """Fehlende Informationen gezielt nachladen: vorhandene Recherche-Infrastruktur, nur lesend, begrenzt auf Konto
    und Asset. Beachtet Zeitplan/Abbruch/Deaktivierung/Stillstand/Wartezeiten der Datenquelle (kein paralleler Abruf
    derselben Quelle, keine Veränderung des regulären Abrufstands). Rückgabe: Protokollzeilen."""
    from app.diagnosis import research as RS

    notes: list[str] = []
    try:
        sources = [s for s in _ds_list(ctx) if getattr(s, "account", None) in set(case.accounts)]
    except Exception as e:  # pragma: no cover
        fallback("request_evidence", e, quiet=True)
        return notes
    for ds in sources:
        label = f"{getattr(ds, 'provider_label', ds.provider)} ({ds.name})"
        if not getattr(ds, "supported", False):
            notes.append(f"{label}: kein automatischer Abruf möglich")
            continue
        if not getattr(ds, "enabled", True):
            notes.append(f"Blockade: {label} ist deaktiviert – bitte aktivieren")
            continue
        if _ds_busy(ds.id):
            notes.append(f"{label}: Abruf läuft bereits – später")
            continue
        last = RS.last_run(ctx.db, ds.id)
        if last is not None and last["status"] in ("done", "partial") and (last["asset"] in (None, "", *case.assets)):
            notes.append(f"{label}: bereits recherchiert ({last['finished_at'] or last['started_at']})")
            continue
        if budget is not None and budget[0] <= 0:
            notes.append(f"{label}: Abruflimit dieses Durchgangs erreicht – im Folgelauf")
            continue
        if budget is not None:
            budget[0] -= 1
        out = RESEARCH_RUN(ctx, ds.id, account=case.accounts[0], asset=case.assets[0] if len(case.assets) == 1
                           else None)
        if out.get("error"):
            notes.append(f"Blockade: {label}: {out['error']}"[:200])
        else:
            notes.append(f"{label}: {out.get('message', 'Recherche abgeschlossen')}"[:200])
    return notes


def _ds_list(ctx: Any) -> list[Any]:
    from app.datasources.service import datasource_service

    return list(datasource_service(ctx).list())


def _ds_busy(sid: int) -> bool:
    from app.datasources.service import is_busy

    return bool(is_busy(sid))


def _research_run(ctx: Any, sid: int, **kw: Any) -> dict[str, Any]:
    from app.diagnosis import research as RS

    return RS.run(ctx, sid, **kw)


RESEARCH_RUN = _research_run  # austauschbar (Tests)


# ----------------------------------------------------------------------------------------------------
# Anzeige / Verlauf
# ----------------------------------------------------------------------------------------------------

def stored_cases(db: Any) -> list[dict[str, Any]]:
    out = []
    for r in db.q("SELECT * FROM clarify_case ORDER BY (status='geloest'), updated_at DESC, id DESC"):
        try:
            st = json.loads(r["state_json"] or "{}")
            dims = json.loads(r["dims_json"] or "{}")
        except ValueError:
            st, dims = {}, {}
        out.append({"key": r["case_key"], "status": r["status"], "title": r["title"], "state": st, "dims": dims,
                    "updated_at": r["updated_at"], "opened_count": r["opened_count"],
                    "reopened_at": r["reopened_at"], "resolved_at": r["resolved_at"], "kind": r["kind"]})
    return out


def runs_of(db: Any, case_key: str, limit: int = 20) -> list[dict[str, Any]]:
    out = []
    for r in db.q("SELECT * FROM clarify_run WHERE case_key=? ORDER BY id DESC LIMIT ?", (case_key, limit)):
        try:
            d = json.loads(r["detail_json"] or "{}")
        except ValueError:
            d = {}
        out.append({"token": r["token"], "trigger": r["trigger"], "outcome": r["outcome"], "bulk_id": r["bulk_id"],
                    "status": r["status"], "created_at": r["created_at"], "undone_at": r["undone_at"], "detail": d})
    return out


def defer(ctx: Any, case_key: str, note: str = "") -> Result:
    """Fall zurückstellen – gilt nur, solange die Evidenz gleich bleibt (neue Evidenz öffnet ihn wieder)."""
    row = _row(ctx.db, case_key)
    if row is None:
        return Result(errors=["Unbekannter Fall."])
    ctx.db.x("UPDATE clarify_case SET status='zurueck', note=?, updated_at=? WHERE case_key=?",
             (note.strip()[:300] or None, _now(), case_key))
    return Result(ok=True, message="Fall zurückgestellt – er öffnet sich wieder, sobald neue Evidenz vorliegt.")


def reopen(ctx: Any, case_key: str) -> Result:
    row = _row(ctx.db, case_key)
    if row is None:
        return Result(errors=["Unbekannter Fall."])
    ctx.db.x("UPDATE clarify_case SET status='wartet_info', evidence_fp=NULL, updated_at=? WHERE case_key=?",
             (_now(), case_key))
    return Result(ok=True, message="Fall wieder geöffnet.")
