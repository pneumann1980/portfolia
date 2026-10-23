"""Steuerangaben aus DKB-Verkaufs- und Ertragsabrechnungen für den Steuervergleich (Phase 7, nur Anzeige).

Gelesen wird ausschließlich, was die Abrechnung selbst ausweist (keine Steuerbescheinigung):

* **Verkauf:** ``Veräußerungsgewinn/-verlust`` (vorzeichenbehaftet), die „Berücksichtigten Anschaffungsgeschäfte“
  (Auftragsnummer, Datum, Stück, Anschaffungskosten, Erlös, anteiliges Ergebnis), die einbehaltene Kapitalertragsteuer,
  der Solidaritätszuschlag und die Kirchensteuer – **netto**: Belastungen positiv, Erstattungen (Steuerliche
  Ausgleichrechnung) abgezogen.
* **Ertrag:** Brutto (EUR), einbehaltene Quellensteuer, Kapitalertragsteuer, Solidaritätszuschlag, Kirchensteuer.
* **Verlusttöpfe:** die Zeilen „Vorher“ und „Nachher“ der Übersicht „Verlustverrechnungstöpfe <Jahr>“ (Aktien,
  Sonstige, Sparer-Pauschbetrag, anrechenbare Quellensteuer, Berechnungsgrundlage der gezahlten Steuern).

Ein Beleg, dessen Rechenprobe (Phase 2/3) scheitert, liefert **keine** Steuerangaben. Die Werte stehen als JSON in
``bank_doc.meta["tax"]`` (Beträge als Text); es werden weder Personendaten noch Dateiinhalte gespeichert.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from app.documentimport.dkb import classify
from app.documentimport.dkb_income import parse_income
from app.documentimport.dkb_trade import _GLUE, AMT, de, parse_trade
from app.documentimport.extract import DocumentResult

log = logging.getLogger(__name__)

ZERO = Decimal(0)
POTS = ("aktien", "sonstige", "pauschbetrag", "quellensteuer", "basis")
TAX_KINDS = ("verkauf", "ertrag")
_AMTS = rf"{AMT}[-+]?"
_GAIN = re.compile(rf"^Veräußerungs(?P<k>gewinn|verlust)\s+(?P<a>{AMT})(?P<s>[-+])?\s+EUR")
_POT_YEAR = re.compile(r"^Verlustverrechnungstöpfe\s+(?P<y>\d{4})")
_POT = re.compile(rf"^(?P<l>Vorher|Nachher)\s+(?P<v>{_AMTS}(?:\s+{_AMTS}){{4}})\s*$")
_LOT = re.compile(rf"^(?P<t>[A-Za-zÄÖÜäöü]+)\s+(?P<no>\d{{6,}})\s+(?P<d>\d{{2}}\.\d{{2}}\.\d{{4}})\s+\S+\s+"
                  rf"(?P<q>{AMT})\s+(?P<c>{AMT})(?P<cs>[-+])?\s+(?P<p>{AMT})(?P<ps>[-+])?\s+(?P<r>{AMT})(?P<rs>[-+])?")
_SUM = re.compile(rf"^Summe aller Erträge nach Differenzmethode\b.*?(?P<a>{AMT})(?P<s>[-+])?\s*$")


def _signed(amount: str, sign: str | None) -> Decimal:
    v = de(amount)
    return -v if sign == "-" else v


def _tok(token: str) -> Decimal:
    return _signed(token.rstrip("+-"), "-" if token.endswith("-") else None)


def _day(text: str) -> date:
    d, m, y = text.split(".")
    return date(int(y), int(m), int(d))


@dataclass
class TaxInfo:
    kind: str  # verkauf | ertrag
    doc_date: str = ""
    isin: str = ""
    gain: Decimal | None = None  # Verkauf: Veräußerungsgewinn (+) / -verlust (−)
    gross: Decimal | None = None  # Ertrag: Brutto in EUR
    qst: Decimal = ZERO  # einbehaltene Quellensteuer (positiv)
    kest: Decimal = ZERO  # netto: Belastung positiv, Erstattung negativ
    soli: Decimal = ZERO
    kist: Decimal = ZERO
    pot_year: int | None = None
    before: dict[str, Decimal] = field(default_factory=dict)
    after: dict[str, Decimal] = field(default_factory=dict)
    lots: list[dict[str, Any]] = field(default_factory=list)
    lots_sum: Decimal | None = None

    @property
    def tax(self) -> Decimal:
        """Von der DKB einbehaltene Steuern ohne Quellensteuer (netto)."""
        return self.kest + self.soli + self.kist

    def to_json(self) -> dict[str, Any]:
        s = lambda v: None if v is None else str(v)  # noqa: E731
        return {"kind": self.kind, "date": self.doc_date, "isin": self.isin, "gain": s(self.gain),
                "gross": s(self.gross), "qst": str(self.qst), "kest": str(self.kest), "soli": str(self.soli),
                "kist": str(self.kist), "pot_year": self.pot_year,
                "before": {k: str(v) for k, v in self.before.items()},
                "after": {k: str(v) for k, v in self.after.items()}, "lots_sum": s(self.lots_sum),
                "lots": [{k: (str(v) if isinstance(v, Decimal) else v) for k, v in lot.items()} for lot in self.lots]}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> TaxInfo:
        dec = lambda v: None if v in (None, "") else Decimal(str(v))  # noqa: E731
        t = cls(kind=d.get("kind", ""), doc_date=d.get("date", ""), isin=d.get("isin", ""), gain=dec(d.get("gain")),
                gross=dec(d.get("gross")), qst=dec(d.get("qst")) or ZERO, kest=dec(d.get("kest")) or ZERO,
                soli=dec(d.get("soli")) or ZERO, kist=dec(d.get("kist")) or ZERO, pot_year=d.get("pot_year"),
                lots_sum=dec(d.get("lots_sum")))
        t.before = {k: Decimal(v) for k, v in (d.get("before") or {}).items()}
        t.after = {k: Decimal(v) for k, v in (d.get("after") or {}).items()}
        for lot in d.get("lots") or []:
            t.lots.append({**lot, "qty": Decimal(lot["qty"]), "cost": Decimal(lot["cost"]),
                           "proceeds": Decimal(lot["proceeds"]), "result": Decimal(lot["result"])})
        return t


def _scan(doc: DocumentResult, t: TaxInfo) -> None:
    """Ergebnis, Anschaffungsgeschäfte und Töpfe aus allen Seiten (auch hinter „Nachrichtlich“)."""
    for ln in (x for x in doc.lines() if len(x.text.strip()) > 3):
        text = ln.text.strip()
        for s in ([text] if not _GLUE.match(text) else [text, _GLUE.sub("", text, count=1)]):
            if (m := _GAIN.match(s)) and t.gain is None:
                v = de(m["a"])
                t.gain = v if m["k"] == "gewinn" else -abs(v)
            elif m := _POT_YEAR.match(s):
                t.pot_year = int(m["y"])
            elif m := _POT.match(s):
                vals = dict(zip(POTS, (_tok(x) for x in m["v"].split()), strict=True))
                (t.before if m["l"] == "Vorher" else t.after).update(vals)
            elif m := _LOT.match(s):
                t.lots.append({"order_no": m["no"], "date": _day(m["d"]).isoformat(), "qty": de(m["q"]),
                               "cost": abs(_signed(m["c"], m["cs"])), "proceeds": _signed(m["p"], m["ps"]),
                               "result": _signed(m["r"], m["rs"])})
            elif m := _SUM.match(s):
                t.lots_sum = _signed(m["a"], m["s"])
            else:
                continue
            break


def read_tax(doc: DocumentResult, filename: str = "") -> TaxInfo | None:
    """Steuerangaben eines DKB-Verkaufs oder -Ertrags; ``None`` bei anderen Belegen oder gescheiterter Rechenprobe."""
    c = classify([ln.text for ln in doc.lines()], filename)
    if c is None or not c.depot:
        return None
    if c.kind == "abrechnung_verkauf":
        tr = parse_trade(doc, c)
        if not tr.ok:
            return None
        t = TaxInfo("verkauf", c.doc_date or "", tr.isin)
        taxes = [(i.label, i.value) for i in tr.taxes]
    elif c.kind == "ertrag":
        inc = parse_income(doc, c)
        if not inc.ok:
            return None
        t = TaxInfo("ertrag", c.doc_date or "", inc.isin)
        t.gross = inc.gross
        t.qst = -sum((i.value for i in inc.items if i.kind == "qst"), ZERO)
        taxes = [(i.label, i.value) for i in inc.items if i.kind == "tax"]
    else:
        return None
    for label, value in taxes:
        if label.startswith("Kapitalertragsteuer"):
            t.kest -= value
        elif label.startswith("Solidarit"):
            t.soli -= value
        elif label.startswith("Kirchensteuer"):
            t.kist -= value
    _scan(doc, t)
    return t


__all__ = ["POTS", "TAX_KINDS", "TaxInfo", "read_tax"]
