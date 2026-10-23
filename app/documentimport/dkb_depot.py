"""DKB-Depotauszug („Aufstellung über Kundenfinanzinstrumente per …“): Bestandsnachweis und Depotentgelt.

* **Bestandsnachweis:** je Position ein Beleg-Vorgang ``holdings`` (ISIN, Menge, Stichtag). Er erzeugt nie eine
  Buchung; der Lauf hinterlegt daraus **Referenzbestände** (Prüfwert der Diagnose) und vergleicht sie mit dem
  Bestand der Buchungen zum Stichtag. Positionen ``ohne Bewertung`` zählen mit Menge, aber ohne Wert.
* **Depotentgelt:** ``Die Depotentgelte für den Zeitraum vom … bis … betragen netto … + 19 % USt … = brutto …``;
  gebucht wird der **Bruttobetrag** als ``withdrawal``/``fee`` (Konto-Ebene, kein Wertpapierbezug) mit der
  **Valuta** der Belastung als Datum; Netto/USt stehen in der Notiz. Kennung ``Depotentgelt:<Depot>:<Quartalsende>``.

Rechenprobe (sonst „ungeklärt“, keine Bestandsnachweise, keine Buchung): Anzahl der gelesenen Positionen = ``n Posten``;
Σ Kurswerte = ``Summe Kurswerte`` (± 0,01 €); ISIN-Prüfziffer jeder Position; netto + USt = brutto und USt ≈ Satz ×
netto beim Depotentgelt. Menge × Kurs ÷ Devisenkurs ≈ Kurswert je Position ist nur ein Hinweis (die Spalte
„Devisenkurs/Depotentgelt“ ist im Layout nicht eindeutig).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.documentimport.dkb import Classified
from app.documentimport.dkb_trade import _GLUE, AMT, PROOF_TOL, _date, de
from app.documentimport.evidence import FieldEvidence
from app.documentimport.extract import DocumentResult, Line
from app.documentimport.profiles import DocTx, _derived, _ev, isin_ok

CENT = Decimal("0.01")
_ISIN = r"[A-Z]{2}[A-Z0-9]{9}\d"
_STICHTAG = re.compile(r"Kundenfinanzinstrumente per (\d{2}\.\d{2}\.\d{4})")
_POS = re.compile(rf"^Stück\s+(?P<q>\d[\d.]*(?:,\d+)?)\s+(?P<name>.+?)\s+"
                  rf"(?:(?P<p>{AMT})\s+(?P<c>[A-Z]{{3}})\s+(?P<v>{AMT})|(?P<none>ohne Bewertung))\s*$")
_IDENT = re.compile(rf"(?P<isin>{_ISIN})\s*\((?P<wkn>[A-Z0-9]{{6}})\)(?:\s+(?P<d>\d{{2}}\.\d{{2}}\.\d{{4}}))?")
_FX_TAIL = re.compile(rf"\s{{2,}}(?P<fx>{AMT})\s*$")
_SUM = re.compile(rf"(?P<n>\d+)\s+Posten\s+Summe Kurswerte\s+(?P<s>{AMT})")
_FEE_PERIOD = re.compile(r"Depotentgelte? für den Zeitraum vom (\d{2}\.\d{2}\.\d{4}) bis (\d{2}\.\d{2}\.\d{4})")
_FEE_AMT = re.compile(rf"netto\s+(?P<n>{AMT})\s+Euro\s*\+\s*(?P<r>\d+(?:,\d+)?)\s*%\s*USt\s+(?P<u>{AMT})\s+Euro"
                      rf"\s*=\s*brutto\s+(?P<b>{AMT})\s+Euro")
_FEE_VALUTA = re.compile(r"Belastung erfolgt mit Valuta (\d{2}\.\d{2}\.\d{4})")
_SECTION = re.compile(r"^(Aktienwerte|Anleihen|Fondsanteile|Fonds|Zertifikate|Optionsscheine|Sonstige Werte)\b", re.I)


@dataclass
class Position:
    qty: Decimal
    name: str
    isin: str = ""
    wkn: str = ""
    price: Decimal | None = None
    ccy: str = ""
    value: Decimal | None = None  # Kurswert EUR; None = ohne Bewertung
    price_day: date | None = None
    fx: Decimal | None = None
    line: Line | None = None


@dataclass
class Depot:
    stichtag: date | None = None
    positions: list[Position] = field(default_factory=list)
    count: int | None = None
    total: Decimal | None = None
    total_line: Line | None = None
    period: tuple[date, date] | None = None
    net: Decimal | None = None
    vat_rate: Decimal | None = None
    vat: Decimal | None = None
    gross: Decimal | None = None
    fee_line: Line | None = None
    valuta: date | None = None
    sections: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def parse_depot(doc: DocumentResult, cl: Classified) -> Depot:
    d = Depot()
    cur: Position | None = None
    for ln in (x for x in doc.lines() if len(x.text.strip()) > 3):
        text = ln.text.strip()
        for s in [text] if not _GLUE.match(text) else [text, _GLUE.sub("", text, count=1)]:
            if (m := _STICHTAG.search(s)) and d.stichtag is None:
                d.stichtag = _date(m[1])
            elif (m := _POS.match(s)):
                cur = Position(de(m["q"]), m["name"].strip(), line=ln)
                if not m["none"]:
                    cur.price, cur.ccy, cur.value = de(m["p"]), m["c"], de(m["v"])
                d.positions.append(cur)
            elif (m := _SUM.search(s)):
                d.count, d.total, d.total_line = int(m["n"]), de(m["s"]), ln
                cur = None
            elif (m := _FEE_PERIOD.search(s)):
                a, b = _date(m[1]), _date(m[2])
                if a and b:
                    d.period = (a, b)
            elif (m := _FEE_AMT.search(s)):
                d.net, d.vat_rate, d.vat, d.gross, d.fee_line = de(m["n"]), de(m["r"]), de(m["u"]), de(m["b"]), ln
            elif (m := _FEE_VALUTA.search(s)):
                d.valuta = _date(m[1])
            elif (m := _SECTION.match(s)):
                d.sections.append(m[1])
            elif cur is not None and (m := _IDENT.search(s)) and not cur.isin:
                cur.isin, cur.wkn = m["isin"], m["wkn"]
                cur.price_day = _date(m["d"]) if m["d"] else None
            elif cur is not None and cur.ccy not in ("", "EUR") and cur.fx is None and (m := _FX_TAIL.search(s)):
                cur.fx = de(m["fx"])
            else:
                continue
            break
    _prove(d)
    return d


def _prove(d: Depot) -> None:
    p = d.problems
    if d.stichtag is None:
        p.append("Rechenprobe: Stichtag („per …“) nicht gelesen")
    if d.count is None or d.total is None:
        p.append("Rechenprobe: Zeile „n Posten – Summe Kurswerte“ nicht gefunden")
    else:
        if len(d.positions) != d.count:
            p.append(f"Rechenprobe: {len(d.positions)} Positionen gelesen, der Beleg nennt {d.count} Posten")
        total = sum((x.value or Decimal(0) for x in d.positions), Decimal(0))
        if abs(total - d.total) > PROOF_TOL:
            p.append(f"Rechenprobe: Σ Kurswerte {total} ≠ Summe Kurswerte {d.total}")
        elif len(d.positions) == d.count:
            d.checks.append(f"{d.count} Posten · Summe Kurswerte {d.total}")
    for x in d.positions:
        if not x.isin:
            p.append(f"Rechenprobe: Position {x.qty} Stück „{x.name}“ ohne ISIN")
        elif not isin_ok(x.isin):
            p.append(f"Rechenprobe: ISIN {x.isin} mit ungültiger Prüfziffer")
        if x.price is not None and x.value is not None:
            raw = x.qty * x.price
            if x.ccy != "EUR":
                raw = raw / x.fx if x.fx else Decimal(-1)
            if raw >= 0 and abs(raw - x.value) > Decimal("0.02") + x.value * Decimal("0.0002"):
                d.hints.append(f"{x.name}: Menge × Kurs ({raw.quantize(CENT)}) weicht vom Kurswert {x.value} ab")
    seen = [x.isin for x in d.positions if x.isin]
    if len(seen) != len(set(seen)):
        d.hints.append("Eine ISIN kommt mehrfach vor (mehrere Verwahrarten?) – Mengen werden je Position geführt")
    if d.sections and any(s.lower() != "aktienwerte" for s in d.sections):
        d.hints.append("Der Auszug enthält andere Wertpapierarten als Aktien (" + ", ".join(
            sorted({s for s in d.sections if s.lower() != 'aktienwerte'})) + ") – nur Stück-Positionen sind belegt")
    if d.gross is not None:
        if d.net is None or d.vat is None or abs(d.net + d.vat - d.gross) > PROOF_TOL:
            p.append(f"Rechenprobe: Depotentgelt netto {d.net} + USt {d.vat} ≠ brutto {d.gross}")
        elif d.vat_rate is not None and abs(d.net * d.vat_rate / 100 - d.vat) > PROOF_TOL:
            p.append(f"Rechenprobe: USt {d.vat_rate} % × netto {d.net} ≠ {d.vat}")
        else:
            d.checks.append(f"Depotentgelt netto {d.net} + USt {d.vat} = brutto {d.gross}")
        if d.valuta is None:
            p.append("Rechenprobe: Valuta der Belastung des Depotentgelts nicht gefunden")
        if d.period is None:
            p.append("Rechenprobe: Zeitraum des Depotentgelts nicht gefunden")
    elif d.period is not None or d.valuta is not None:
        p.append("Rechenprobe: Depotentgelt unvollständig gelesen")


def to_doctxs(d: Depot, doc: DocumentResult, cl: Classified) -> list[DocTx]:
    """Ein ``holdings``-Vorgang je Position und – falls im Auszug – ein ``fee``-Vorgang (Depotentgelt). Ohne bestandene
    Rechenprobe ein einziger ``unknown``-Vorgang (nichts wird geschrieben)."""
    sha = doc.sha256
    out: list[DocTx] = []

    def new(n: int, kind: str) -> tuple[DocTx, list[FieldEvidence]]:
        tx = DocTx(n, kind, "depotauszug", "dkb")
        return tx, tx.evidence

    def add(tx: DocTx, fld: str, value: object, ln: Line | None, reason: str = "") -> None:
        tx.evidence.append(_derived(fld, value, sha, reason or "abgeleitet") if ln is None else
                           _ev(fld, value, ln, sha, reason=reason))

    if not d.ok:
        tx, _ = new(1, "unknown")
        tx.warnings.extend(d.problems)
        tx.resolve()
        return [tx]
    depot = cl.depot_short or "depot"
    day = d.stichtag.isoformat() if d.stichtag else cl.doc_date
    n = 0
    for x in d.positions:
        n += 1
        tx, _ = new(n, "holdings")
        add(tx, "isin", x.isin, x.line)
        add(tx, "wkn", x.wkn, x.line)
        add(tx, "asset", x.name, x.line)
        add(tx, "quantity", x.qty, x.line, "Bestand laut Depotauszug")
        add(tx, "date", day, x.line, "Stichtag laut Depotauszug")
        if x.value is not None:
            add(tx, "value_eur", x.value, x.line, "Kurswert in EUR laut Depotauszug")
            add(tx, "price", x.price, x.line, f"Kurs {x.price} {x.ccy} laut Depotauszug")
            add(tx, "price_ccy", x.ccy, x.line)
        add(tx, "ext_id", f"Depotauszug:{depot}:{day}:{x.isin}", None, "Depot, Stichtag und ISIN")
        tx.lines = [(x.line.page, x.line.no)] if x.line else []
        tx.resolve()
        out.append(tx)
    if d.gross is not None and d.period is not None and d.valuta is not None:
        n += 1
        tx, _ = new(n, "fee")
        add(tx, "gross", d.gross, d.fee_line, "Depotentgelt brutto laut Depotauszug")
        add(tx, "value_eur", d.gross, d.fee_line, "Depotentgelt brutto laut Depotauszug")
        add(tx, "fee", d.gross, d.fee_line)
        add(tx, "fee_ccy", "EUR", d.fee_line)
        add(tx, "ccy", "EUR", d.fee_line)
        add(tx, "date", d.valuta.isoformat(), d.fee_line, "Valuta der Belastung laut Depotauszug")
        add(tx, "ext_id", f"Depotentgelt:{depot}:{d.period[1].isoformat()}", None, "Depot und Quartalsende")
        add(tx, "ca_kind", "Depotentgelt", None, "Art laut Depotauszug")
        add(tx, "ca_detail", f"Zeitraum {d.period[0]:%d.%m.%Y}–{d.period[1]:%d.%m.%Y}, netto {d.net} + "
            f"{d.vat_rate} % USt {d.vat} = brutto {d.gross}", None, "Angaben laut Depotauszug")
        tx.lines = [(d.fee_line.page, d.fee_line.no)] if d.fee_line else []
        if d.checks:
            tx.evidence.append(FieldEvidence(
                "proof", " · ".join(d.checks), "document", f"document:{sha}", "Rechenprobe", "rekonstruiert",
                "Rechenprobe bestanden: " + " · ".join(d.checks)))
        tx.warnings.extend(d.hints)
        tx.resolve()
        out.append(tx)
    if out:
        out[0].warnings.extend(h for h in d.hints if out[0].kind == "holdings")
    return out


def dkb_depot_doctxs(doc: DocumentResult, lines: list[str], filename: str = "") -> list[DocTx] | None:
    from app.documentimport.dkb import classify

    cl = classify(lines, filename)
    if cl is None or cl.kind != "depotauszug" or not cl.depot:
        return None
    return to_doctxs(parse_depot(doc, cl), doc, cl)


__all__ = ["Depot", "Position", "dkb_depot_doctxs", "parse_depot", "to_doctxs"]
