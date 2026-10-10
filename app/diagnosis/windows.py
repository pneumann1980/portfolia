"""Abweichungsfenster (M29): Entwicklung der Differenz zwischen mehreren Referenzbeständen derselben Position.

Ein Referenzbestand belegt den Bestand **zu einem Zeitpunkt** – nicht, wann und zu welchen Kosten er entstand. Je Konto
und Asset wird für jeden Referenzzeitpunkt ``Differenz(t) = Referenz(t) − Ledger-Bestand(t)`` berechnet (Ledger-Bestand
wie in der übrigen Diagnose: :func:`app.diagnosis.audit.soll_at_time` bzw. ``soll_at_date`` – keine zweite
Bestandsrechnung) und die Differenzentwicklung zwischen aufeinanderfolgenden Referenzen eingeordnet.

Ein Fenster lokalisiert die **Nettoveränderung** der Differenz. Es beweist nicht, dass darin genau eine fehlerhafte
Buchung liegt: gegenläufige Fehler können sich aufheben, und mit nur einem Referenzbestand lässt sich der Zeitraum nicht
eingrenzen – beides wird ausgewiesen statt eine Genauigkeit vorzutäuschen. Alles hier arbeitet rein lesend und mit
``Decimal``; Ursachenkandidaten (:func:`candidates`) sind ausschließlich Hypothesen (nichts wird gebucht) und werden
nach Evidenz eingestuft – nie nach der Höhe der Differenz.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from app.diagnosis.model import Finding
from app.util.timeutil import local_tz

ZERO = Decimal(0)
MAX_WINDOW_TXS = 200  # Anzeige je Fenster (die Anzahl bleibt vollständig)
COARSE_DAYS = 90  # Fenster über mehr Tage gelten als grob (wenig Referenzdaten)

# Einordnung der Differenzentwicklung zwischen zwei Referenzpunkten
KIND_LABEL = {
    "ok": "keine Abweichung",
    "erstmals": "Abweichung erstmals nachweisbar",
    "veraendert": "Abweichung verändert sich",
    "stabil": "Abweichung bleibt stabil",
    "verschwindet": "Abweichung verschwindet wieder",
    "nicht_eingrenzbar": "Zeitraum nicht eingrenzbar",
}
KIND_BADGE = {"ok": "good", "erstmals": "crit", "veraendert": "warn", "stabil": "info", "verschwindet": "good",
              "nicht_eingrenzbar": "warn"}
EVIDENCE_OF_STATUS = {"belegt": "Belegt", "wahrscheinlich": "Stark gestützt", "verdacht": "Plausibel",
                      "hinweis": "Ungeklärt"}
EVIDENCE_BADGE = {"Belegt": "good", "Stark gestützt": "info", "Plausibel": "warn", "Ungeklärt": "",
                  "Widersprüchlich": "crit"}


@dataclass(frozen=True)
class RefPoint:
    """Ein Referenzbestand als Mess- bzw. Nachweiszeitpunkt (kein kontinuierlich gemessener Bestand)."""

    ref_id: int
    at: datetime  # Vergleichszeitpunkt (UTC); bei Tagesangaben Ende des Tages
    basis: str  # exakt | tagesende | datum (Tag in Ortszeit, ohne Uhrzeit)
    as_of: date
    tz: str | None
    qty: Decimal
    ledger: Decimal
    diff: Decimal  # Referenz − Ledger zum Zeitpunkt
    source: str
    note: str
    value_basis: str | None = None  # booking | value (Wertstellungstag: Buchungsdatum kann abweichen)
    conflict: bool = False  # widerspricht einem anderen Referenzbestand desselben Zeitpunkts

    @property
    def basis_label(self) -> str:
        return {"exakt": "exakter Zeitpunkt", "tagesende": "Ende des Tages (Zeitzone angegeben)",
                "datum": "Tag in Ortszeit, ohne Uhrzeit"}[self.basis]


@dataclass
class Segment:
    """Fenster zwischen zwei aufeinanderfolgenden Referenzpunkten (bzw. Beginn der Historie → erster Punkt)."""

    start: RefPoint | None
    end: RefPoint
    kind: str
    delta: Decimal  # Änderung der Differenz im Fenster (bzw. Differenz am ersten Punkt)
    tx_ids: list[str] = field(default_factory=list)  # wirksame Buchungen der Position im Fenster (gekürzt)
    all_ids: frozenset[str] = field(default_factory=frozenset, repr=False)  # vollständig (für die Kandidatensuche)
    n_tx: int = 0
    net_effect: Decimal = ZERO  # Nettowirkung dieser Buchungen auf den Ledger-Bestand
    days: int | None = None
    flags: list[str] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.start.ref_id if self.start else 0}-{self.end.ref_id}"

    @property
    def label(self) -> str:
        return KIND_LABEL[self.kind]


@dataclass
class Candidate:
    """Hypothetische Erklärung eines Fensters – Evidenz statt Wahrscheinlichkeit, nie automatisch angewendet."""

    key: str
    kind: str  # duplicate | plan_unfunded | reconstruction
    title: str
    evidence: str  # Belegt | Stark gestützt | Plausibel | Ungeklärt | Widersprüchlich
    tx_ids: list[str]
    ledger_effect: Decimal  # Änderung des Ledger-Bestands am Fensterende, falls umgesetzt
    diff_before: Decimal
    diff_after: Decimal
    finding_id: str | None = None
    creates_negative: bool = False
    other_positions: list[tuple[str, str, Decimal]] = field(default_factory=list)
    note: str = ""

    @property
    def reduces(self) -> bool:
        return abs(self.diff_after) < abs(self.diff_before)

    @property
    def effect_text(self) -> str:
        if self.diff_after == self.diff_before:
            return "ändert die Differenz nicht"
        return "verringert die Differenz" if self.reduces else "vergrößert die Differenz"


@dataclass
class PositionTrace:
    account: str
    asset: str
    points: list[RefPoint] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    summary: list[str] = field(default_factory=list)  # eindeutige Aussagen (Reihenfolge = Gewicht)
    contradictions: list[str] = field(default_factory=list)
    since_last: int = 0  # Buchungen der Position nach dem letzten Referenzpunkt (ungeprüft)
    quality: list[str] = field(default_factory=list)  # Datengrundlage (Quellen, Lücken)
    sources: list[str] = field(default_factory=list)
    n_unique_ref: int = 0
    rest_strong: Decimal | None = None  # Differenz am letzten Punkt nach Belegten/Stark gestützten Kandidaten
    rest_all: Decimal | None = None  # … auch nach Plausiblen (rein rechnerisch)

    @property
    def position(self) -> tuple[str, str]:
        return (self.account, self.asset)

    @property
    def deviating(self) -> bool:
        return any(p.diff != ZERO for p in self.points)

    @property
    def last(self) -> RefPoint | None:
        return self.points[-1] if self.points else None

    @property
    def key(self) -> str:
        return f"{self.account}|{self.asset}"


class _Series:
    """Bestandswirkung einer Position mit Präfixsummen: Bestand zu beliebigem Zeitpunkt in O(log n)."""

    def __init__(self, items: list[tuple[datetime, date, Decimal, str]], balance_now: Decimal) -> None:
        self.items = sorted(items, key=lambda x: (x[0], x[3]))
        self.ts = [x[0] for x in self.items]
        self.cum = [ZERO]
        for it in self.items:
            self.cum.append(self.cum[-1] + it[2])
        by_date = sorted(items, key=lambda x: (x[1], x[3]))
        self.dates = [x[1] for x in by_date]
        self.cum_d = [ZERO]
        for it in by_date:
            self.cum_d.append(self.cum_d[-1] + it[2])
        self.total = self.cum[-1]
        self.now = balance_now

    def at_time(self, when: datetime) -> Decimal:
        return self.now - (self.total - self.cum[bisect_right(self.ts, when)])

    def at_date(self, d: date) -> Decimal:
        return self.now - (self.total - self.cum_d[bisect_right(self.dates, d)])

    def min_running(self, extra: list[tuple[datetime, Decimal]] | None = None,
                    remove: set[str] | None = None) -> Decimal:
        """Tiefster Zwischenbestand (ab 0) – optional hypothetisch ohne bestimmte Buchungen bzw. mit zusätzlichen
        Bewegungen."""
        rows = [(ts, v, i) for ts, _d, v, i in self.items if not remove or i not in remove]
        rows += [(ts, v, "~") for ts, v in (extra or [])]
        run = lo = ZERO
        for _ts, v, _i in sorted(rows, key=lambda r: (r[0], r[2])):
            run += v
            lo = min(lo, run)
        return lo


def _is_fiat(idx: Any, asset: str) -> bool:
    try:
        return bool(idx.asset(asset).is_fiat)
    except Exception:
        return False


def _same(idx: Any, asset: str, a: Decimal, b: Decimal) -> bool:
    """Gleich im Sinne des Referenzvergleichs: Fiat auf den Cent, Krypto exakt."""
    if _is_fiat(idx, asset):
        cent = Decimal("0.01")
        return a.quantize(cent) == b.quantize(cent)
    return a == b


def _instant(ref: Any) -> tuple[datetime, str]:
    """(Zeitpunkt, Basis) eines Referenzbestands für Reihenfolge und Fenster."""
    from app.diagnosis.audit import reference_instant

    at = reference_instant(ref)
    if at is not None:
        return at, ("exakt" if getattr(ref, "at", None) is not None else "tagesende")
    end = datetime.combine(ref.as_of, time(23, 59, 59, 999999), tzinfo=local_tz())
    return end.astimezone(UTC), "datum"


def _within(point: RefPoint | None, ts: datetime, d: date) -> bool:
    """Buchung bis einschließlich Referenzpunkt (Tagesangaben ohne Zeitzone: Buchungsdatum in Ortszeit)."""
    if point is None:
        return False
    return d <= point.as_of if point.basis == "datum" else ts <= point.at


def build_points(idx: Any, refs: list[Any], series: _Series, asset: str) -> list[RefPoint]:
    pts: list[RefPoint] = []
    for r in refs:
        at, basis = _instant(r)
        ledger = series.at_date(r.as_of) if basis == "datum" else series.at_time(at)
        pts.append(RefPoint(ref_id=r.id, at=at, basis=basis, as_of=r.as_of, tz=getattr(r, "tz", None), qty=r.qty,
                            ledger=ledger, diff=r.qty - ledger, source=r.source or "", note=(r.note or "").strip(),
                            value_basis=getattr(r, "basis", None)))
    pts.sort(key=lambda p: (p.at, p.ref_id))
    return pts


def _mark_conflicts(idx: Any, asset: str, pts: list[RefPoint]) -> tuple[list[RefPoint], list[str]]:
    """Referenzen desselben Vergleichszeitpunkts mit unterschiedlichem Bestand widersprechen sich. Beide bleiben
    sichtbar, fließen aber nicht in die Fenster ein (kein Rückschluss aus widersprüchlichen Angaben)."""
    out: list[RefPoint] = []
    msgs: list[str] = []
    groups: dict[Any, list[RefPoint]] = defaultdict(list)
    for p in pts:
        groups[(p.at if p.basis != "datum" else p.as_of, p.basis == "datum")].append(p)
    bad: set[int] = set()
    for grp in groups.values():
        if len(grp) > 1 and any(not _same(idx, asset, grp[0].qty, p.qty) for p in grp[1:]):
            bad |= {p.ref_id for p in grp}
            when = grp[0].at.astimezone(UTC).strftime("%d.%m.%Y %H:%M UTC") if grp[0].basis != "datum" \
                else grp[0].as_of.strftime("%d.%m.%Y")
            msgs.append(f"Widersprüchliche Referenzbestände zum selben Zeitpunkt ({when}): "
                        + " ↔ ".join(f"{p.qty.normalize():f}" for p in grp)
                        + " – keine Fenster daraus abgeleitet; einen der Einträge entfernen.")
    for p in pts:
        out.append(RefPoint(**{**p.__dict__, "conflict": True}) if p.ref_id in bad else p)
    return out, msgs


def _classify(idx: Any, asset: str, d0: Decimal | None, d1: Decimal) -> str:
    zero1 = _same(idx, asset, d1, ZERO)
    if d0 is None:
        return "ok" if zero1 else "nicht_eingrenzbar"
    zero0 = _same(idx, asset, d0, ZERO)
    if zero0 and zero1:
        return "ok"
    if zero0:
        return "erstmals"
    if zero1:
        return "verschwindet"
    return "stabil" if _same(idx, asset, d0, d1) else "veraendert"


def analyse(idx: Any, snap: Any, findings: list[Finding]) -> dict[tuple[str, str], PositionTrace]:
    """Spuren aller Positionen mit mindestens einem Referenzbestand (rein lesend, deterministisch)."""
    from app.diagnosis.audit import deltas

    by_pos: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for r in snap.references:
        by_pos[(r.account, r.asset_id)].append(r)
    if not by_pos:
        return {}
    all_deltas = deltas(idx)
    traces: dict[tuple[str, str], PositionTrace] = {}
    states = {s.account: s for s in snap.sources}
    for pos in sorted(by_pos):
        acc, aid = pos
        series = _Series(list(all_deltas.get(pos, ())), idx.bal(acc, aid))
        pts, conflicts = _mark_conflicts(idx, aid, build_points(idx, by_pos[pos], series, aid))
        tr = PositionTrace(account=acc, asset=aid, points=pts, contradictions=conflicts)
        usable = [p for p in pts if not p.conflict]
        tr.n_unique_ref = len({(p.at if p.basis != "datum" else p.as_of) for p in usable})
        prev: RefPoint | None = None
        for p in usable:
            if prev is not None and (p.at, p.as_of) == (prev.at, prev.as_of):
                continue  # gleicher Zeitpunkt mit gleichem Bestand: ein Punkt genügt
            kind = _classify(idx, aid, prev.diff if prev is not None else None, p.diff)
            seg = Segment(start=prev, end=p, kind=kind, delta=p.diff - prev.diff if prev is not None else p.diff,
                          days=(p.at - prev.at).days if prev is not None else None)
            win = [(ts, dd, v, i) for ts, dd, v, i in series.items
                   if _within(p, ts, dd) and not _within(prev, ts, dd)]
            seg.n_tx = len(win)
            seg.net_effect = sum((v for _ts, _dd, v, _i in win), ZERO)
            seg.all_ids = frozenset(i for _ts, _dd, _v, i in win)
            seg.tx_ids = [i for _ts, _dd, _v, i in win][:MAX_WINDOW_TXS]
            if prev is None and kind == "nicht_eingrenzbar":
                seg.flags.append("vor dem ersten Referenzbestand beginnt keine Eingrenzung – gesamte Historie bis zum "
                                 "Nachweis")
            if seg.days is not None and seg.days > COARSE_DAYS and kind != "ok":
                seg.flags.append(f"grobes Fenster ({seg.days} Tage ohne Referenzbestand)")
            if kind in ("erstmals", "veraendert", "verschwindet") and seg.n_tx == 0:
                seg.flags.append("keine Buchung im Fenster – fehlende Buchung (z. B. Zugang, Reward, Gebühr) "
                                 "wahrscheinlicher als eine fehlerhafte")
            if p.value_basis == "value":
                seg.flags.append("Wertstellungsdatum – Buchungsdatum kann abweichen (Fenstergrenzen unscharf)")
            tr.segments.append(seg)
            prev = p
        last = usable[-1] if usable else None
        if last is not None:
            tr.since_last = sum(1 for ts, dd, _v, _i in series.items if not _within(last, ts, dd))
        _quality(tr, snap, states, acc)
        _summarise(idx, tr)
        traces[pos] = tr
    attach_candidates(idx, traces, findings, {pos: _Series(list(all_deltas.get(pos, ())), idx.bal(*pos))
                                              for pos in traces})
    return traces


def _quality(tr: PositionTrace, snap: Any, states: dict[str, Any], acc: str) -> None:
    srcs = [s for s in snap.sources if s.account == acc]
    tr.sources = [f"{s.provider_label} ({s.name})" for s in srcs]
    if not srcs:
        tr.quality.append("keine Börsen-/Wallet-Anbindung für dieses Konto – nur Import bzw. manuelle Buchungen")
    for s in srcs:
        if not s.complete:
            tr.quality.append(f"{s.provider_label}: Abruf unvollständig bzw. mit Lücken ({s.state})")
        for g in s.gaps[:3]:
            tr.quality.append(f"{s.provider_label}: {g}")
    srcs_ref = {p.source for p in tr.points}
    if srcs_ref == {"other"}:
        tr.quality.append("alle Referenzbestände mit „sonstiger Beleg“ – Kontoauszug bzw. Anzeige beim Anbieter ist "
                          "belastbarer")


def _summarise(idx: Any, tr: PositionTrace) -> None:
    kinds = [s.kind for s in tr.segments]
    s = tr.summary
    if tr.contradictions:
        s.append("Widersprüchliche Referenzbestände")
    if tr.n_unique_ref <= 1:
        s.append("Zeitraum nicht eingrenzbar: nur ein Referenzbestand" if tr.deviating
                 else "nur ein Referenzbestand – ohne Abweichung")
    if "erstmals" in kinds:
        s.append(KIND_LABEL["erstmals"])
    if "veraendert" in kinds:
        s.append(KIND_LABEL["veraendert"])
    if "verschwindet" in kinds:
        s.append(KIND_LABEL["verschwindet"])
    if "stabil" in kinds:
        s.append(KIND_LABEL["stabil"])
    if "nicht_eingrenzbar" in kinds and tr.n_unique_ref > 1:
        s.append("Anfang der Abweichung liegt vor dem ersten Referenzbestand")
    if not s:
        s.append(KIND_LABEL["ok"])


# ----------------------------------------------------------------------------------------------------
# Ursachenkandidaten (hypothetisch)
# ----------------------------------------------------------------------------------------------------

def _effect(idx: Any, tx_id: str, pos: tuple[str, str]) -> Decimal:
    from app.diagnosis.engine import _effect as eff

    t = idx.by_id.get(tx_id)
    return eff([t]).get(pos, ZERO) if t is not None else ZERO


def _others(idx: Any, tx_ids: list[str], pos: tuple[str, str], sign: int = -1) -> list[tuple[str, str, Decimal]]:
    """Weitere betroffene Positionen, wenn die Buchungen entfielen (sign = −1) bzw. hinzukämen."""
    from app.diagnosis.engine import _effect as eff

    tot: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    for i in tx_ids:
        t = idx.by_id.get(i)
        if t is None:
            continue
        for k, v in eff([t]).items():
            if k != pos:
                tot[k] += sign * v
    return [(a, b, v) for (a, b), v in sorted(tot.items()) if v]


def _evidence_of(f: Finding) -> str:
    return EVIDENCE_OF_STATUS.get(f.status, "Ungeklärt")


def _drops(idx: Any, f: Finding) -> list[str]:
    """Buchungen eines Dublettenbefunds, die bei Umsetzung entfielen (die jeweils zweite des Paars)."""
    d = f.data or {}
    typ = d.get("type")
    if typ in ("econ_pairs",):
        return [p[1] for p in d.get("pairs", [])]
    if typ == "hash_pairs":
        return [p[1] for p in d.get("pairs", [])]
    if typ in ("same_qty", "conversion_twin"):
        return [d["weak"]] if d.get("weak") else []
    return []


def attach_candidates(idx: Any, traces: dict[tuple[str, str], PositionTrace], findings: list[Finding],
                      series: dict[tuple[str, str], _Series]) -> None:
    """Je Fenster Kandidaten aus vorhandenen Befunden (Dubletten, nicht finanzierte Sparpläne) mit hypothetischer
    Wirkung auf die Differenz. Kandidaten, die sich eine Buchung teilen, schließen einander aus (Widersprüchlich)."""
    from app.diagnosis.audit import PRIMARY_EXCLUDE, _family_last, family

    top = [f for f in findings if f.parent is None and f.kind == "duplicate"]
    for pos, tr in traces.items():
        acc, _aid = pos
        ser = series[pos]
        base_min = ser.min_running()
        for seg in tr.segments:
            ids = seg.all_ids
            cands: list[Candidate] = []
            diff_end = seg.end.diff
            for f in top:
                if pos not in f.positions:
                    continue
                removed = sorted({i for i in _drops(idx, f) if i in ids})
                if not removed:
                    continue
                eff = sum((_effect(idx, i, pos) for i in removed), ZERO)
                cands.append(Candidate(
                    key=f"dup|{f.id}|{seg.key}", kind="duplicate", title=f.title, evidence=_evidence_of(f),
                    tx_ids=removed, ledger_effect=-eff, diff_before=diff_end, diff_after=diff_end + eff,
                    finding_id=f.id, creates_negative=ser.min_running(remove=set(removed)) < min(ZERO, base_min),
                    other_positions=_others(idx, removed, pos),
                    note="Eine verringerte Differenz ist kein Beleg; maßgeblich ist die Evidenz des Befunds."))
            # nicht finanzierte Sparplan-Ausführungen (Einzahlung fehlt in den Daten)
            others = [ts for fm, ts in _family_last(idx, acc).items() if fm not in PRIMARY_EXCLUDE]
            last_other = max(others) if others else None
            plan = [i for i in sorted(ids) if family(idx, idx.by_id[i]) == "sparplan"
                    and (last_other is None or idx.by_id[i].ts > last_other) and _effect(idx, i, pos) < 0]
            if plan:
                eff = sum((_effect(idx, i, pos) for i in plan), ZERO)  # negativ: fehlende Einzahlung
                cands.append(Candidate(
                    key=f"plan|{seg.key}", kind="plan_unfunded",
                    title=f"{len(plan)} Sparplan-Ausführung(en) nach dem Ende der Quellhistorie – Einzahlung fehlt "
                          "in den Daten", evidence="Plausibel", tx_ids=plan, ledger_effect=-eff,
                    diff_before=diff_end, diff_after=diff_end + eff,
                    note="Hypothese: fehlende Einzahlung in Höhe der Ausführungen – nur mit Kontoauszug bzw. "
                         "Import belegen (keine Ausgleichsbuchung)."))
            use: dict[str, int] = defaultdict(int)  # gemeinsame Buchungen → einander ausschließend
            for c in cands:
                for i in c.tx_ids:
                    use[i] += 1
            for c in cands:
                if any(use[i] > 1 for i in c.tx_ids):
                    c.evidence = "Widersprüchlich"
                    c.note = "Teilt Buchungen mit einem anderen Kandidaten – beide lassen sich nicht zugleich umsetzen."
            effs = [c.ledger_effect for c in cands if c.evidence != "Widersprüchlich" and c.ledger_effect]
            opposite = len({(e > 0) - (e < 0) for e in effs}) > 1
            if opposite or (seg.kind == "stabil" and effs):
                seg.flags.append("gegenläufige Buchungsfehler möglich: Kandidaten mit entgegengesetzter Wirkung "
                                 "bzw. Wirkung bei unveränderter Differenz")
            elif seg.kind == "ok":
                cands = []  # ohne Abweichung nur dann Kandidaten zeigen, wenn sie sich gegenseitig aufheben könnten
            order = list(EVIDENCE_BADGE)
            seg.candidates = sorted(cands, key=lambda c: (order.index(c.evidence), c.key))
        if tr.segments:  # Restdifferenz am letzten Punkt – rein rechnerisch, nie als Beleg
            every = {c.key: c for sg in tr.segments for c in sg.candidates if c.evidence != "Widersprüchlich"}
            strong = [c for c in every.values() if c.evidence in ("Belegt", "Stark gestützt")]
            last = tr.segments[-1].end.diff
            tr.rest_strong = last - sum((c.ledger_effect for c in strong), ZERO)
            tr.rest_all = last - sum((c.ledger_effect for c in every.values()), ZERO)
