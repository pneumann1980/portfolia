"""Jahressteuerbescheinigung und Erträgnisaufstellung (DKB-Layout) → strukturierte Werte.

Eingabe ist der **Text** der Dokumentseiten (Zeilenliste je Seite, siehe :mod:`app.documentimport.extract` mit
``plain=True``); die Funktionen sind reine Textverarbeitung ohne Dateizugriff. Grundsätze wie im übrigen
Dokumentimport: Der Inhalt entscheidet über die Dokumentart; was sich nicht eindeutig lesen lässt, bleibt leer
(nie geraten); Personendaten (Name, Anschrift, Kundennummer) werden **nicht** übernommen.

* **Jahressteuerbescheinigung** (amtliches Muster, „Bescheinigung für alle Privatkonten und/oder -depots“): Höhe der
  Kapitalerträge (Anlage KAP Z. 7), Aktiengewinne (Z. 8), Sparer-Pauschbetrag (Z. 16/17), Kapitalertragsteuer
  (Z. 37), Solidaritätszuschlag (Z. 38), Kirchensteuer, angerechnete ausländische Steuer (Z. 40), nachrichtlich
  Aktiengewinne vor Verrechnung. Rechenprobe: Kapitalertragsteuer = (Erträge − Pauschbetrag − 4 × Quellensteuer) /
  (4 + Kirchensteuersatz).
* **Erträgnisaufstellung** („Erläuterungen zur Steuerbescheinigung“): Summenzeile der Spalten I–VIII, Vorjahres-
  Verlusttöpfe, Einzelvorgänge (Verkäufe, Erträge, Kapitalmaßnahmen, nachträgliche Einbuchungen), ins neue Jahr
  vorgetragene Töpfe, genutzter Pauschbetrag und einbehaltene Steuern.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

ZERO = Decimal(0)
KIND_BESCHEINIGUNG = "bescheinigung"
KIND_ERTRAEGNIS = "ertraegnis"
KIND_LABEL = {KIND_BESCHEINIGUNG: "Jahressteuerbescheinigung", KIND_ERTRAEGNIS: "Erträgnisaufstellung"}

_AMT = r"-?\d{1,3}(?:\.\d{3})*,\d{2}-?"
_AMT_RE = re.compile(_AMT)
_YEAR = re.compile(r"Kalenderjahr\s+(20\d{2})")
_ZEILE = r"(?:Zeile\s+\d+(?:\s+oder\s+\d+)?\s+Anlage\s+KAP\s+)?"
KNOWN_LINES = {"7", "8", "16 oder 17", "37", "38", "39", "40"}
_DATE = r"\d{2}\.\d{2}\.\d{4}"
_EVENT_HEAD = re.compile(r"^(?P<depot>\d{6,12})\s+(?P<isin>[A-Z]{2}[A-Z0-9]{9}\d)$")
_EVENT_KIND = re.compile(r"^(Verkäufe|Käufe|Erträge|Kapitalmaßnahmen|Stückzinsen|Depotüberträge|Vorabpauschale)\b",
                         re.IGNORECASE)
_EVENT_VALUE = re.compile(rf"^(?P<d>{_DATE})\s+(?P<a>{_AMT}(?:\s+{_AMT})*)$")
_SUMMEN = re.compile(rf"^Summen\s+(?P<a>{_AMT}(?:\s+{_AMT}){{7}})$")
_HEADER = re.compile(r"^(I II III|\(Depot-\)Konto|Geschäftsvorfall$|Datum$|\(Abrechnungs|31\.12\.2017$|anrechenbare$|"
                     r"ausländische$|Quellensteuer$|Gewinn2? ab 2018$|aus vor 2009$|erworbenen$|Fonds1?$|"
                     r"1Bei Fonds|Seite \d+ von|Deutsche Kreditbank|Erläuterungen zur)", re.IGNORECASE)


def amount(s: str) -> Decimal:
    """„34.452,97“ / „-5.282,80“ / „1.175,60-“ → Decimal."""
    t = s.strip()
    neg = t.startswith("-") or t.endswith("-")
    d = Decimal(t.strip("-").replace(".", "").replace(",", "."))
    return -d if neg else d


def _flat(pages: Sequence[Sequence[str]]) -> str:
    return re.sub(r"\s+", " ", " ".join(" ".join(p) for p in pages))


def classify(pages: Sequence[Sequence[str]]) -> str | None:
    """Dokumentart aus dem Inhalt; ``None`` = unbekannt."""
    text = _flat(pages)
    if re.search(r"Erläuterungen zur Steuerbescheinigung für das Kalenderjahr", text):
        return KIND_ERTRAEGNIS
    if re.search(r"\bSteuerbescheinigung\b", text) and re.search(r"werden für das Kalenderjahr\s+20\d{2}\s+folgende "
                                                                  r"Angaben bescheinigt", text):
        return KIND_BESCHEINIGUNG
    return None


def _issuer(pages: Sequence[Sequence[str]]) -> str:
    for line in (pages[0] if pages else [])[:12]:
        if (re.search(r"\b(Bank|Sparkasse|AG|eG)\b", line) and "Erläuterungen" not in line
                and not re.search(r"\d{5}", line)):
            return line.strip()[:80]
    return ""


def _year(text: str) -> int | None:
    m = _YEAR.search(text)
    return int(m.group(1)) if m else None


# ----------------------------------------------------------------------------------------------------
# Jahressteuerbescheinigung
# ----------------------------------------------------------------------------------------------------

@dataclass
class Bescheinigung:
    year: int | None
    issuer: str = ""
    values: dict[str, Decimal] = field(default_factory=dict)  # capital_total, share_gains, pauschbetrag, kest, …
    extra_lines: list[str] = field(default_factory=list)  # weitere „Zeile N Anlage KAP“, die nicht gelesen wurden
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.year is not None and "capital_total" in self.values


_B_FIELDS: list[tuple[str, str]] = [
    ("capital_total", rf"Höhe der Kapitalerträge\s+{_ZEILE}(?P<a>{_AMT})"),
    ("share_gains", rf"davon:\s*Gewinn aus Aktienveräußerungen im Sinne des § 20 Abs\. 2 Satz 1 Nr\. 1 EStG\s+"
                    rf"{_ZEILE}(?P<a>{_AMT})"),
    ("pauschbetrag", rf"Höhe des in Anspruch genommenen Sparer-Pauschbetrag(?:es)?\s+{_ZEILE}(?P<a>{_AMT})"),
    ("kest", rf"Kapitalertragsteuer\s+{_ZEILE}(?P<a>{_AMT})"),
    ("soli", rf"Solidaritätszuschlag\s+{_ZEILE}(?P<a>{_AMT})"),
    ("kist", rf"Kirchensteuer\s+{_ZEILE}(?P<a>{_AMT})"),
    ("wht_credited", rf"Summe der angerechneten ausländischen Steuer\s+{_ZEILE}(?P<a>{_AMT})"),
    ("share_gains_gross", rf"vor Verrechnung mit sonstigen\s+(?:Verlusten.{{0,80}}?)?(?P<a>{_AMT})"),
    ("loss_other", rf"nicht ausgeglichene[rn]? Verlust.{{0,80}}?ohne Verlust aus der Veräußerung von Aktien"
                   rf".{{0,120}}?{_ZEILE}(?P<a>{_AMT})"),
    ("loss_shares", rf"nicht ausgeglichene[rn]? Verlust aus der Veräußerung von Aktien.{{0,120}}?{_ZEILE}"
                    rf"(?P<a>{_AMT})"),
]


def parse_bescheinigung(pages: Sequence[Sequence[str]]) -> Bescheinigung:
    text = _flat(pages)
    out = Bescheinigung(year=_year(text), issuer=_issuer(pages))
    for key, pattern in _B_FIELDS:
        m = re.search(pattern, text)
        if m:
            out.values[key] = amount(m.group("a"))
    seen = {re.sub(r"\s+", " ", z) for z in re.findall(r"Zeile\s+(\d+(?:\s+oder\s+\d+)?)\s+Anlage\s+KAP", text)}
    out.extra_lines = sorted(seen - KNOWN_LINES, key=lambda z: int(z.split()[0]))
    v = out.values
    if {"capital_total", "pauschbetrag", "kest"} <= set(v):
        base = max(ZERO, v["capital_total"] - v["pauschbetrag"] - 4 * v.get("wht_credited", ZERO))
        rate = ZERO
        if v.get("kist"):
            m = re.search(r"Kirchensteuersatz.{0,40}?(\d{1,2})\s*%", text)
            rate = Decimal(m.group(1)) / 100 if m else Decimal("0.09")
        expected = base / (4 + rate)
        if abs(expected - v["kest"]) > Decimal("0.05"):
            out.notes.append(f"Rechenprobe Kapitalertragsteuer weicht ab: erwartet {expected:.2f}, bescheinigt "
                             f"{v['kest']:.2f} (Kirchensteuer/Verlusttöpfe können abweichen)")
    if "capital_total" not in v:
        out.notes.append("„Höhe der Kapitalerträge“ nicht gefunden – Layout unbekannt")
    return out


# ----------------------------------------------------------------------------------------------------
# Erträgnisaufstellung
# ----------------------------------------------------------------------------------------------------

COLS = ("income", "loss_other", "share_gains", "share_losses", "fund_old", "fund_2017", "wht", "fund_2009")


@dataclass
class Event:
    kind: str  # Verkäufe Wertpapier | Erträge | Kapitalmaßnahmen | Aktientopf aus Vorjahr … (Töpfe)
    date: date
    name: str = ""
    isin: str = ""
    amounts: list[Decimal] = field(default_factory=list)
    pot: bool = False  # Verlusttopf-Einbuchung (kein Geschäftsvorfall eines Wertpapiers)


@dataclass
class Ertraegnis:
    year: int | None
    issuer: str = ""
    totals: dict[str, Decimal] = field(default_factory=dict)  # Spalten I–VIII laut Summenzeile
    capital_total: Decimal | None = None  # „Höhe der Kapitalerträge“
    events: list[Event] = field(default_factory=list)
    carry_out: dict[str, Decimal] = field(default_factory=dict)  # other, shares (ins neue Jahr vorgetragen)
    hints: dict[str, Decimal] = field(default_factory=dict)  # pauschbetrag, kest, soli, kist, share_gains_gross
    column_probe: bool | None = None  # Spaltensummen aus den Einzelvorgängen = Summenzeile?
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.year is not None and bool(self.totals)

    def carry_in(self) -> dict[str, Decimal]:
        """Verlusttöpfe aus dem Vorjahr (positive Beträge): shares, other."""
        out = {"shares": ZERO, "other": ZERO}
        for e in self.events:
            if e.pot and e.amounts and "Vorjahr" in e.kind:
                out["shares" if "Aktien" in e.kind else "other"] += -e.amounts[0] if e.amounts[0] < 0 else ZERO
        return out


def _iso(d: str) -> date:
    dd, mm, yy = d.split(".")
    return date(int(yy), int(mm), int(dd))


def _events(lines: Sequence[str]) -> list[Event]:
    out: list[Event] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i].strip()
        m = _EVENT_HEAD.match(line)
        if m and i + 2 < n and _EVENT_KIND.match(lines[i + 1].strip()):
            kind = lines[i + 1].strip()
            j = i + 2
            name: list[str] = []
            while j < n and not _EVENT_VALUE.match(lines[j].strip()):
                if _EVENT_HEAD.match(lines[j].strip()) or j - i > 6:
                    break
                name.append(lines[j].strip())
                j += 1
            v = _EVENT_VALUE.match(lines[j].strip()) if j < n else None
            if v:
                out.append(Event(kind, _iso(v.group("d")), " ".join(name), m.group("isin"),
                                 [amount(a) for a in _AMT_RE.findall(v.group("a"))]))
                i = j + 1
                continue
        v = _EVENT_VALUE.match(line)
        if v and not _EVENT_KIND.match(line):
            # Zeile ohne Wertpapier: Verlusttopf-Einbuchung – Bezeichnung steht in den Zeilen davor
            label: list[str] = []
            k = i - 1
            while k >= 0 and len(label) < 3 and not _HEADER.match(lines[k].strip()) \
                    and not _EVENT_VALUE.match(lines[k].strip()) and not _EVENT_HEAD.match(lines[k].strip()) \
                    and not _EVENT_KIND.match(lines[k].strip()) and not _SUMMEN.match(lines[k].strip()):
                label.insert(0, lines[k].strip())
                k -= 1
            text = " ".join(label)
            if re.search(r"Topf|Einbuchung|eingebucht|Verlust", text, re.IGNORECASE):
                out.append(Event(text, _iso(v.group("d")), "", "", [amount(a) for a in _AMT_RE.findall(v.group("a"))],
                                 pot=True))
        i += 1
    return out


def _assign(events: list[Event]) -> dict[str, Decimal]:
    """Spaltenzuordnung der Einzelvorgänge nach Art und Vorzeichen (Probe gegen die Summenzeile)."""
    sums = dict.fromkeys(COLS, ZERO)
    for e in events:
        if not e.amounts:
            continue
        if e.pot:
            sums["share_losses" if "Aktien" in e.kind else "loss_other"] += e.amounts[0]
        elif e.kind.lower().startswith("verkäufe"):
            sums["share_gains" if e.amounts[0] >= 0 else "share_losses"] += e.amounts[0]
        else:
            sums["income"] += e.amounts[0]
            if len(e.amounts) > 1:
                sums["wht"] += e.amounts[1]
    return sums


def parse_ertraegnis(pages: Sequence[Sequence[str]]) -> Ertraegnis:
    text = _flat(pages)
    out = Ertraegnis(year=_year(text), issuer=_issuer(pages))
    lines = [ln.strip() for p in pages for ln in p if ln.strip()]
    for ln in lines:
        m = _SUMMEN.match(ln)
        if m:
            vals = [amount(a) for a in _AMT_RE.findall(m.group("a"))]
            out.totals = dict(zip(COLS, vals, strict=True))
            break
    m = re.search(rf"Höhe der Kapitalerträge \(Zwischensummen III der Spalten I, III, V und VI\)\s+(?P<a>{_AMT})", text)
    if m:
        out.capital_total = amount(m.group("a"))
    out.events = _events(lines)
    for key, pat in (("other", rf'Verlusttopf\s+[„"“]sonstige Verluste[“"”]\s+(?P<a>{_AMT})'),
                     ("shares", rf'Verlusttopf\s+[„"“]Aktien[“"”]\s+(?P<a>{_AMT})')):
        m = re.search(pat, text)
        if m:
            out.carry_out[key] = amount(m.group("a"))
    for key, pat in (("pauschbetrag", rf"in Anspruch genommener Sparer-Pauschbetrag\s+(?P<a>{_AMT})"),
                     ("kest", rf"einbehaltene Kapitalertragsteuer\s+(?P<a>{_AMT})"),
                     ("soli", rf"einbehaltener Solidaritätszuschlag\s+(?P<a>{_AMT})"),
                     ("kist", rf"einbeh\. Kirchensteuer Einzelp\./Ehem\./Lebensp\.\s+(?P<a>{_AMT})"),
                     ("share_gains_gross", rf"Verlusten i\. S\. d\. § 20 Abs\. 2 EStG:.*?(?P<a>{_AMT})\s*Erläuterung")):
        m = re.search(pat, text)
        if m:
            out.hints[key] = amount(m.group("a"))
    if out.totals and out.events:
        got = _assign(out.events)
        main = ("income", "loss_other", "share_gains", "share_losses", "wht")
        out.column_probe = all(abs(got[c] - out.totals[c]) <= Decimal("0.01") for c in main)
        if not out.column_probe:
            out.notes.append("Spaltensummen aus den Einzelvorgängen weichen von der Summenzeile ab – "
                             "nur die Summenzeile wird ausgewertet")
    if not out.totals:
        out.notes.append("Summenzeile nicht gefunden – Layout unbekannt")
    return out
