"""Bestandsklärung und „bestätigte Bestandssicht“ (M29) – eine Auswertungsschicht, keine zweite Buchhaltung.

Je Position (Konto, Asset) mit Referenzbestand werden drei Größen **getrennt** gezeigt:

* **A buchhalterisch** – Bestand laut Ledger (alle wirksamen Buchungen),
* **B bestätigter Referenzbestand** – letzter verwendbarer Referenzbestand, mit den Buchungen *nach* dem Nachweis
  fortgeschrieben (``B' = Referenz + Σ spätere Wirkung``); ein Bestand mit späteren Buchungen heißt „fortgeschrieben“,
  nie „verifiziert“,
* **C ungeklärte Differenz** – ``B' − A``; das ist exakt die Differenz am letzten Nachweis.

Nichts davon wird gespeichert (alles wird aus Buchungen und Referenzen berechnet), und ein ungeklärter Bestand ist
**kein** Zugang, Kauf, Transfer, Lot, Gewinn oder Verlust: er ändert weder Ledger noch FIFO, Einstand, realisierte
Ergebnisse, Steuer, Haltefrist oder XIRR. Die alternative Sicht ersetzt je Position die Ledger-Menge durch B'
(nie beides, keine Doppelzählung) und bewertet nur, wo Menge **und** Kurs belastbar sind; sonst gilt der Wert als
„unvollständig“, niemals als 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.diagnosis.windows import PositionTrace, RefPoint

ZERO = Decimal(0)
STALE_DAYS = 90  # Referenzbestand älter als das gilt als veraltet (bleibt sichtbar, mit Hinweis)
CAUSES_IN = ("fehlender Zugang (Einzahlung, Reward, Staking, Airdrop)", "fehlender Transfer-Eingang",
             "falsch abgebildeter Tausch bzw. Token-Umstellung")
CAUSES_OUT = ("Doppelbuchung desselben Vorgangs", "fehlender Abgang (Auszahlung, Verkauf, Gebühr)",
              "fehlender Transfer-Ausgang")


@dataclass
class StockRow:
    account: str
    asset: str
    name: str
    ledger: Decimal  # A
    point: RefPoint | None  # letzter verwendbarer Referenzpunkt
    state: str  # bestätigt | fortgeschrieben | widersprüchlich | keine
    n_after: int = 0  # wirksame Buchungen nach dem Nachweis
    age_days: int | None = None
    stale: bool = False
    rolled: Decimal | None = None  # B'
    diff: Decimal | None = None  # C
    evidence: str = "Ungeklärt"
    rest_strong: Decimal | None = None
    status: str = "offen"  # Untersuchungsstatus (Nutzerentscheidung)
    status_label: str = "offen"
    causes: tuple[str, ...] = ()
    candidates: list[str] = field(default_factory=list)
    findings: list[tuple[str, str]] = field(default_factory=list)  # (Befund-ID, Titel)
    decisions: list[Any] = field(default_factory=list)
    n_refs: int = 0
    price_note: str = ""
    ledger_value: Decimal | None = None
    view_qty: Decimal = ZERO
    view_value: Decimal | None = None
    value_note: str = ""
    sources: list[str] = field(default_factory=list)
    confirm_kind: str = ""  # Art der Bestätigung: Referenzbestand (Nutzer) | Datenquelle (Abruf)
    no_ledger: bool = False  # Position ohne eine einzige wirksame Buchung (nur Referenz bzw. externer Bestand)

    @property
    def key(self) -> str:
        return f"{self.account}|{self.asset}"

    @property
    def unexplained(self) -> bool:
        return bool(self.diff)


@dataclass
class StockView:
    rows: list[StockRow]
    n_unreferenced: int  # Positionen ohne Referenzbestand (Ledger-Menge unverändert, nicht bestätigt)
    ledger_total: Decimal  # A über alle bewerteten Positionen
    view_total: Decimal  # Summe der bestätigten Sicht über alle bewerteten Positionen
    n_unvalued: int  # Positionen ohne belastbare Bewertung (nicht 0, sondern unvollständig)
    unvalued: list[str] = field(default_factory=list)
    n_positions: int = 0  # alle Positionen (Ledger ∪ Referenz ∪ belastbarer externer Bestand)
    n_valued: int = 0  # davon mit belastbarem Kurs (Teilbewertung: Summen nur über diese)
    n_no_ledger: int = 0  # Positionen ohne Buchung (nur Referenz bzw. externer Bestand)


def _age_days(p: RefPoint, now: datetime) -> int:
    return max(0, (now - p.at).days)


def _price_ok(idx: Any, aid: str) -> tuple[Any, str]:
    p = idx.price(aid)
    if p is None:
        return None, "kein bewerteter Kurs"
    if getattr(p, "stale", False):
        return None, "Kurs veraltet"
    return p, ""


EXTERNAL_OK = ("extern_ok", "extern_diff")  # aktueller, vollständiger Abruf einer Datenquelle (nicht „unsicher“)


def _external(report: Any) -> dict[tuple[str, str], Any]:
    """Belastbare externe Bestände je Position: frischer, vollständiger Abruf eines zugeordneten Assets."""
    out: dict[tuple[str, str], Any] = {}
    for h in getattr(report, "holdings", None) or []:
        if h.account == "(alle Konten)" or h.observed is None or h.status not in EXTERNAL_OK or h.observed_at is None:
            continue
        out[(h.account, h.asset)] = h
    return out


def _external_point(h: Any) -> RefPoint:
    soll = h.computed_at_obs if h.computed_at_obs is not None else h.computed
    at = h.observed_at if h.observed_at.tzinfo else h.observed_at.replace(tzinfo=UTC)
    return RefPoint(ref_id=0, at=at, basis="exakt", as_of=at.date(), tz="UTC", qty=h.observed, ledger=soll,
                    diff=h.observed - soll, source=f"Datenquelle: {h.observed_by or 'Abruf'}",
                    note=h.observed_state or "")


def build(report: Any, decisions: list[Any], now: datetime | None = None) -> StockView:
    """Bestandsklärung aus dem Diagnosebericht (rein lesend, berechnet).

    Positionen: Ledger ∪ Referenzbestände ∪ belastbare externe Bestände – auch eine Position ohne jede Buchung (z. B.
    500 TOKEN laut Kontoauszug, nie gebucht) erscheint, mit Ledger 0 und vollständiger Differenz."""
    now = now or datetime.now(UTC)
    idx = report.index
    traces: dict[tuple[str, str], PositionTrace] = report.traces or {}
    external = _external(report)
    positions = sorted(set(idx.led.balances) | set(traces) | set(external))
    after_obs: dict[tuple[str, str], int] = {}
    ext_only = [pos for pos in external if not any(not p.conflict for p in (traces[pos].points if pos in traces
                                                                              else []))]
    if ext_only:
        from app.diagnosis.audit import deltas

        all_d = deltas(idx)
        for pos in ext_only:
            at = external[pos].observed_at
            at = at if at.tzinfo else at.replace(tzinfo=UTC)
            after_obs[pos] = sum(1 for ts, _dd, _v, _i in all_d.get(pos, ()) if ts > at)
    by_pos_findings: dict[tuple[str, str], list[Any]] = {}
    for f in report.findings:
        if f.parent is not None:
            continue
        for pos in f.positions:
            by_pos_findings.setdefault(pos, []).append(f)
    dec_by_finding: dict[str, list[Any]] = {}
    for d in decisions:
        dec_by_finding.setdefault(d.finding_id, []).append(d)
    rows: list[StockRow] = []
    view_total = ledger_total = ZERO
    unvalued: list[str] = []
    for pos in positions:
        acc, aid = pos
        ledger = idx.led.balances.get(pos, ZERO)
        tr = traces.get(pos)
        a = idx.asset(aid)
        price, why = _price_ok(idx, aid)
        led_val = (ledger * Decimal(str(price.price_eur))).quantize(Decimal("0.01")) if price is not None else None
        usable = [p for p in (tr.points if tr else []) if not p.conflict]
        row = StockRow(account=acc, asset=aid, name=a.name or aid, ledger=ledger, point=None, state="keine",
                       ledger_value=led_val, view_qty=ledger, n_refs=len(tr.points) if tr else 0,
                       sources=list(tr.sources) if tr else [], no_ledger=pos not in idx.led.balances)
        if tr is not None and usable:
            p = usable[-1]
            row.point = p
            row.confirm_kind = "Referenzbestand"
            row.age_days = _age_days(p, now)
            row.stale = row.age_days > STALE_DAYS
            row.n_after = tr.since_last
            row.state = "bestätigt" if tr.since_last == 0 else "fortgeschrieben"
            row.rolled = ledger + p.diff  # = Referenz + Σ spätere Wirkung
            row.diff = p.diff
            row.view_qty = row.rolled
            row.rest_strong = tr.rest_strong
            cands = [c for sg in tr.segments for c in sg.candidates if c.evidence != "Widersprüchlich"]
            order = ["Belegt", "Stark gestützt", "Plausibel", "Ungeklärt"]
            row.evidence = min((c.evidence for c in cands), key=order.index, default="Ungeklärt") if p.diff else \
                "keine Abweichung"
            row.candidates = [f"{c.evidence}: {c.title}" for c in cands[:6]]
        elif tr is not None and pos not in external:
            row.state = "widersprüchlich"
        elif pos in external:  # kein verwendbarer Referenzbestand: aktueller Abruf der Datenquelle bestätigt
            h = external[pos]
            p = _external_point(h)
            row.point, row.confirm_kind = p, "Datenquelle (Abruf)"
            row.age_days = _age_days(p, now)
            row.stale = row.age_days > STALE_DAYS
            row.n_after = after_obs.get(pos, 0)
            row.state = "bestätigt" if row.n_after == 0 else "fortgeschrieben"
            row.rolled = ledger + p.diff
            row.diff = p.diff
            row.view_qty = row.rolled
            row.evidence = "keine Abweichung" if not p.diff else "Ungeklärt"
            row.sources = [h.observed_by] if h.observed_by else []
        if row.state != "keine" and (tr is not None or pos in external):
            row.causes = CAUSES_IN if (row.diff or ZERO) > 0 else CAUSES_OUT if row.diff else ()
        fs = by_pos_findings.get(pos, [])
        row.findings = [(f.id, f.title) for f in fs[:8]]
        ref_f = next((f for f in fs if f.kind == "holdings" and (f.data or {}).get("type") == "reference"), None)
        if ref_f is not None:
            row.status = ref_f.state or "offen"
        from app.diagnosis.model import CASE_STATES

        row.status_label = CASE_STATES.get(row.status, (row.status, ""))[0]
        row.decisions = [d for f in fs for d in dec_by_finding.get(f.id, [])][:10]
        # bestätigte Sicht: je Position genau eine Menge (Referenz ersetzt Ledger – keine Doppelzählung)
        if price is None:
            row.value_note = f"nicht bewertbar: {why}"
        elif row.state == "widersprüchlich":
            row.value_note = "widersprüchliche Referenzbestände – Ledger-Menge unverändert, nicht bestätigt"
            row.view_value = led_val
        else:
            row.view_value = (row.view_qty * Decimal(str(price.price_eur))).quantize(Decimal("0.01"))
        if price is not None:
            row.price_note = f"{price.source}, {price.ts:%d.%m.%Y}" if price.ts else price.source
        if led_val is not None:
            ledger_total += led_val
        if row.view_value is not None:
            view_total += row.view_value
        if price is None and (ledger or row.view_qty):
            unvalued.append(f"{acc} · {aid}")
        rows.append(row)
    shown = [r for r in rows if r.point is not None or r.state == "widersprüchlich"]
    shown.sort(key=lambda r: (not r.unexplained, -abs((r.view_value or ZERO) - (r.ledger_value or ZERO)),
                              r.account, r.asset))
    return StockView(rows=shown, n_unreferenced=sum(1 for r in rows if r.state == "keine"),
                     ledger_total=ledger_total, view_total=view_total, n_unvalued=len(unvalued),
                     unvalued=unvalued[:30], n_positions=len(rows),
                     n_valued=sum(1 for r in rows if r.ledger_value is not None or r.view_value is not None),
                     n_no_ledger=sum(1 for r in rows if r.no_ledger))
