"""Herkunft der Anschaffungsdaten je Lot (rein lesend, berechnet aus dem Ledger).

FIFO braucht für jede Veräußerung Anschaffungszeitpunkt und -kosten. Wie belastbar diese sind, hängt davon ab, wie
das Lot entstanden ist. Diese Sicht ordnet jedes offene Lot und jeden verbrauchten Lot-Anteil einer Herkunftsklasse zu
– ohne Werte zu ändern oder zu ergänzen:

* **Kauf/Tausch** – Anschaffung mit Gegenwert aus einer Buchung (belegt);
* **Ertrag** – Zuflusswert eines Ertrags (Staking, Reward …) als Anschaffungskosten;
* **Kapitalmaßnahme** – über eine Token-Migration bzw. Fusion fortgeführte Anschaffungsdaten;
* **Eröffnungsposition** – Anschaffung laut Nachweis einer Eröffnungsbuchung (KREST-Fall);
* **Zugang ohne Anschaffungsbeleg** – externer Zugang: Zeitpunkt = Zugang, Kosten = Wert beim Zugang (Ersatz, kein
  Anschaffungsnachweis) bzw. **unbekannt**, wenn kein Wert vorliegt (nie 0 €);
* **Derivat** – Ergebnis aus Futures/Funding (gesondert einzuordnen);
* **Sparplan-Schätzung** – geschätzte, noch nicht erfasste Sparplan-Ausführung (kein Beleg, nur Fortschreibung);
* **ohne Anschaffungsdaten** – Platzhalter („Phantom“) für Abgänge ohne vorhandene Lots.

Transfers zwischen eigenen Konten führen Lots mit unverändertem Anschaffungszeitpunkt fort (Herkunft bleibt). Eine
rückwirkende Buchung ändert die FIFO-Zuordnung späterer Veräußerungen – :func:`retro_years` nennt die betroffenen
Steuerjahre, bevor gebucht wird.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

ZERO = Decimal(0)
CLASSES = {
    "kauf": "Kauf/Tausch mit Gegenwert",
    "ertrag": "Ertrag (Zuflusswert)",
    "kapitalmassnahme": "Kapitalmaßnahme (fortgeführt)",
    "eroeffnung": "Eröffnungsposition (Nachweis)",
    "ohne_beleg": "Zugang ohne Anschaffungsbeleg (Ersatzwert)",
    "unbekannt": "Anschaffungskosten unbekannt",
    "derivat": "Derivat-Ergebnis",
    "geschaetzt": "Sparplan-Schätzung (nicht belegt)",
    "ohne_basis": "ohne Anschaffungsdaten",
}
EVIDENCED = ("kauf", "ertrag", "kapitalmassnahme", "eroeffnung")


def classify(origin: str, tag: str | None, cost: Decimal, via: tuple[str, ...] = (), value: Any = "x",
             flag: str | None = None) -> str:
    if origin == "phantom":
        return "ohne_basis"
    if flag == "estimated":
        return "geschaetzt"
    if via or origin == "corporate_action":
        return "kapitalmassnahme"
    if origin in ("buy", "trade"):
        return "kauf"
    if origin == "income":
        return "ertrag"
    if origin == "derivative":
        return "derivat"
    if tag == "opening_balance":
        return "eroeffnung"
    if value in (None, "") and not cost:
        return "unbekannt"
    return "ohne_beleg"


@dataclass
class AssetRow:
    asset: str
    qty: Decimal = ZERO
    by_class: dict[str, Decimal] = field(default_factory=dict)  # Klasse → Menge
    cost_by_class: dict[str, Decimal] = field(default_factory=dict)
    oldest: date | None = None

    @property
    def evidenced_share(self) -> Decimal | None:
        if not self.qty:
            return None
        return sum((q for k, q in self.by_class.items() if k in EVIDENCED), ZERO) / self.qty


@dataclass
class YearRow:
    year: int
    qty_by_class: dict[str, Decimal] = field(default_factory=dict)
    gain_by_class: dict[str, Decimal] = field(default_factory=dict)
    parts: int = 0


@dataclass
class Report:
    assets: list[AssetRow]
    years: list[YearRow]
    classes: dict[str, str]


def build(ctx: Any) -> Report:
    led = ctx.ledger()
    if led is None:  # noch keine Buchungen
        return Report([], [], CLASSES)
    pf = ctx.portfolio()  # wie das Ledger: erfasste Buchungen plus Sparplan-Schätzungen (flag „estimated“)
    txs = {t.tx_id: t for t in (pf.txs if pf is not None else [])}
    by_asset: dict[str, AssetRow] = {}
    for lot in led.lots:
        if lot.qty <= 0:
            continue
        t = txs.get(lot.acq_tx)
        cls = classify(lot.origin, t.tag if t else None, lot.cost, lot.via, t.value_eur if t else "x",
                       t.flag if t else None)
        row = by_asset.setdefault(lot.asset, AssetRow(lot.asset))
        row.qty += lot.qty
        row.by_class[cls] = row.by_class.get(cls, ZERO) + lot.qty
        row.cost_by_class[cls] = row.cost_by_class.get(cls, ZERO) + lot.cost
        row.oldest = min(row.oldest, lot.acq_date) if row.oldest else lot.acq_date
    years: dict[int, YearRow] = {}
    for d in led.disposals:
        y = years.setdefault(d.date.year, YearRow(d.date.year))
        for p in d.parts:
            t = txs.get(p.acq_tx) if getattr(p, "acq_tx", None) else None
            cls = "ohne_basis" if p.missing_basis else classify(p.origin or "", t.tag if t else None,
                                                                 p.cost, p.via, t.value_eur if t else "x",
                                                                 t.flag if t else None)
            y.qty_by_class[cls] = y.qty_by_class.get(cls, ZERO) + p.qty
            y.gain_by_class[cls] = y.gain_by_class.get(cls, ZERO) + (p.proceeds - p.cost)
            y.parts += 1
    assets = sorted(by_asset.values(), key=lambda r: (r.evidenced_share if r.evidenced_share is not None else 1,
                                                      r.asset))
    return Report(assets, [years[k] for k in sorted(years)], CLASSES)


def retro_years(led: Any, asset: str, day: date) -> list[int]:
    """Steuerjahre mit Veräußerungen des Assets **nach** ``day`` – eine Anschaffung an ``day`` ordnet deren FIFO-Lots
    neu zu (rückwirkende Wirkung)."""
    return sorted({d.date.year for d in led.disposals if d.asset == asset and d.date > day})


def retro_map(led: Any, items: list[tuple[str, date]]) -> dict[str, list[int]]:
    out: dict[str, set[int]] = defaultdict(set)
    for asset, day in items:
        out[asset] |= set(retro_years(led, asset, day))
    return {a: sorted(ys) for a, ys in out.items() if ys}
