"""DKB-Ertragsabrechnung (Dividende, Kapitalrückzahlung, Liquidationszahlung): Felder, Steuern und **Rechenprobe**.

Alle Beträge der Abrechnung stehen in EUR; nur ``Dividende pro Stück`` / ``Auszahlung pro Stück``, der Bruttobetrag
in Fremdwährung und der Devisenkurs nennen die Fremdwährung. Gebucht wird der **EUR-Bruttobetrag** (Zeile
``Dividendengutschrift`` bzw. ``Kurswert`` mit ``… 3,71+ EUR``) als ``deposit``/``dividend``; abgezogen werden
**nur** die tatsächlich einbehaltenen Beträge:

* ``Einbehaltene Quellensteuer <p> % auf <Betrag> <Währung>  <EUR>-``  → ``withdrawal``/``withholding_tax``
* ``Kapitalertragsteuer``, ``Solidaritätszuschlag``, ``Kirchensteuer`` (``<p> % auf <Basis> EUR  <EUR>-``) → ``tax``

Reine Informationszeilen (``Anrechenbare …``, ``… rückforderbare Quellensteuer``, ``Verrechnete anrechenbare …``,
``Kapitalertragsteuerpflichtige Dividende``, ``Berechnungsgrundlage …``, ``Eingebuchte sonstige negative
Kapitalerträge``) sind **keine** Abzüge und gehen nie in die Rechnung ein. Seite 2 („Nachrichtlich … Verrechnungs-
und Steuertopfsalden“) wird nicht gelesen.

Rechenprobe (Abweichung ≤ 0,01 €; sonst „ungeklärt“, keine Buchung)::

    EUR-Brutto + Σ (einbehaltene Quellensteuer, KapESt, Soli, KiSt; Abzüge negativ) = Ausmachender Betrag
    Brutto in Fremdwährung ÷ Devisenkurs ≈ EUR-Brutto  ·  Stück × Betrag pro Stück ≈ Brutto in Fremdwährung
    Quellensteuer ≈ Satz × Brutto (Fremdwährung) ÷ Devisenkurs  ·  KapESt/Soli ≈ Satz × Basis laut Beleg
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.documentimport.dkb import Classified
from app.documentimport.dkb_trade import _AUSM, _GLUE, _STUECK, _TAX, AMT, PROOF_TOL, _date, de, signed
from app.documentimport.evidence import FieldEvidence
from app.documentimport.extract import DocumentResult, Line
from app.documentimport.profiles import DocTx, _derived, _ev, isin_ok

CENT = Decimal("0.01")
PCT_TOL = Decimal("0.02")  # Bank rundet/schneidet ab (z. B. Soli 5,5 % × 0,36 = 0,0198 → gedruckt 0,01)
KIND_TEXT = {"dividende": "Dividende", "kapitalrueckzahlung": "Kapitalrückzahlung",
             "liquidation": "Liquidationszahlung"}

_PAY = re.compile(rf"^Zahlbarkeitstag\s+(?P<d>\d{{2}}\.\d{{2}}\.\d{{4}})\s+(?:Dividende|Auszahlung)\s+pro\s+Stück\s+"
                  rf"(?P<a>{AMT})\s+(?P<c>[A-Z]{{3}})\b")
_STICHTAG = re.compile(r"^Bestandsstichtag\s+(\d{2}\.\d{2}\.\d{4})(?:\s+Herkunftsland\s+(?P<land>.+?))?\s*$")
_EXTAG = re.compile(r"^Ex-Tag\s+(\d{2}\.\d{2}\.\d{4})")
_FX = re.compile(rf"^Devisenkurs\s+EUR\s*/\s*(?P<c>[A-Z]{{3}})\s+(?P<r>{AMT})\b")
_GROSS = re.compile(rf"^(?P<l>Dividendengutschrift|Kurswert)\s+(?P<f>{AMT})\s+(?P<c>[A-Z]{{3}})\s+(?P<a>{AMT})"
                    rf"(?P<s>[-+])?\s*EUR\b")
_GROSS_EUR = re.compile(rf"^(?P<l>Dividendengutschrift|Kurswert)\s+(?P<a>{AMT})(?P<s>[-+])?\s*EUR\s*$")
_CONV = re.compile(rf"^Umrechnung in EUR\s+(?P<a>{AMT})\s*EUR")
_QST = re.compile(rf"^Einbehaltene Quellensteuer\s+(?P<r>\d[\d,]*)\s*%\s+auf\s+(?P<b>{AMT})\s+(?P<c>[A-Z]{{3}})\s+"
                  rf"(?P<a>{AMT})(?P<s>[-+])?\s*EUR")
_ABR = re.compile(r"^Abrechnungsnr\.\s+(?P<n>\d+)")
_WERT = re.compile(r"Wertstellung\s+(\d{2}\.\d{2}\.\d{4})\s+zu\s+(Lasten|Gunsten)")


@dataclass
class Item:
    kind: str  # gross | qst | tax
    label: str
    value: Decimal  # vorzeichenbehaftet (− Abzug)
    line: Line
    rate: str = ""
    base: Decimal | None = None
    base_ccy: str = "EUR"


@dataclass
class Income:
    sub: str = ""
    isin: str = ""
    wkn: str = ""
    name: str = ""
    qty: Decimal | None = None
    qty_line: Line | None = None
    per_share: Decimal | None = None
    per_places: int = 0
    pay_ccy: str = ""
    pay_date: date | None = None
    pay_line: Line | None = None
    record: date | None = None
    ex: date | None = None
    country: str = ""
    fx: Decimal | None = None
    fx_ccy: str = ""
    fx_line: Line | None = None
    gross_foreign: Decimal | None = None
    gross_ccy: str = ""
    gross: Decimal | None = None  # EUR, positiv
    gross_line: Line | None = None
    conv: Decimal | None = None  # „Umrechnung in EUR“
    abr_no: str = ""
    doc_date: date | None = None
    value_date: date | None = None
    value_line: Line | None = None
    direction: str = ""
    items: list[Item] = field(default_factory=list)
    total: Decimal | None = None
    total_line: Line | None = None
    totals: int = 0
    problems: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)

    @property
    def withheld(self) -> Decimal:
        return -sum((i.value for i in self.items if i.kind == "qst"), Decimal(0))

    @property
    def tax_items(self) -> list[Item]:
        return [i for i in self.items if i.kind == "tax"]

    @property
    def tax_burden(self) -> Decimal:
        return -sum((i.value for i in self.tax_items), Decimal(0))

    @property
    def ok(self) -> bool:
        return not self.problems


def _places(text: str) -> int:
    return len(text.split(",", 1)[1]) if "," in text else 0


def parse_income(doc: DocumentResult, c: Classified) -> Income:
    """Felder und Rechenprobe einer Ertragsabrechnung. Probleme stehen in ``Income.problems``."""
    t = Income(sub=c.sub, abr_no=c.abrechnungsnr)
    with contextlib.suppress(ValueError):
        t.doc_date = date.fromisoformat(c.doc_date) if c.doc_date else None
    seen_abr = ""
    for ln in (x for x in doc.lines() if len(x.text.strip()) > 3):
        text = ln.text.strip()
        if text.startswith("Nachrichtlich"):
            break
        for s in [text] if not _GLUE.match(text) else [text, _GLUE.sub("", text, count=1)]:
            if (m := _STUECK.match(s)) and t.qty is None:
                t.qty, t.qty_line = de(m["q"]), ln
                t.name, t.isin, t.wkn = m["name"].strip(), m["isin"], m["wkn"]
            elif (m := _PAY.match(s)) and t.pay_date is None:
                t.pay_date, t.pay_line, t.per_share, t.pay_ccy = _date(m["d"]), ln, de(m["a"]), m["c"]
                t.per_places = _places(m["a"])
            elif (m := _STICHTAG.match(s)) and t.record is None:
                t.record, t.country = _date(m[1]), (m["land"] or "").strip()
            elif (m := _EXTAG.match(s)) and t.ex is None:
                t.ex = _date(m[1])
            elif (m := _FX.match(s)) and t.fx is None:
                t.fx, t.fx_ccy, t.fx_line = de(m["r"]), m["c"], ln
            elif (m := _GROSS.match(s)) and t.gross is None:
                t.gross_foreign, t.gross_ccy, t.gross, t.gross_line = de(m["f"]), m["c"], signed(m["a"], m["s"]), ln
                t.items.append(Item("gross", m["l"], t.gross, ln))
            elif (m := _GROSS_EUR.match(s)) and t.gross is None:
                t.gross_foreign, t.gross_ccy, t.gross, t.gross_line = de(m["a"]), "EUR", signed(m["a"], m["s"]), ln
                t.items.append(Item("gross", m["l"], t.gross, ln))
            elif (m := _CONV.match(s)) and t.conv is None:
                t.conv = de(m["a"])
            elif (m := _QST.match(s)):
                t.items.append(Item("qst", "Einbehaltene Quellensteuer", signed(m["a"], m["s"] or "-"), ln,
                                    rate=m["r"], base=de(m["b"]), base_ccy=m["c"]))
            elif (m := _TAX.match(s)):
                t.items.append(Item("tax", m["l"], signed(m["a"], m["s"]), ln, rate=m["r"], base=de(m["b"])))
            elif (m := _AUSM.search(s)):
                if m["c"] != "EUR":
                    t.problems.append(f"Ausmachender Betrag in {m['c']} statt EUR – nicht unterstützt")
                t.totals += 1
                if t.total is None:
                    t.total, t.total_line = signed(m["a"], m["s"]), ln
            elif (m := _ABR.match(s)) and not seen_abr:
                seen_abr = m["n"]
            elif (m := _WERT.search(s)) and t.value_date is None:
                t.value_date, t.value_line, t.direction = _date(m[1]), ln, m[2]
            else:
                continue
            break
    if seen_abr and not t.abr_no:
        t.abr_no = seen_abr
    _prove(t, c, seen_abr)
    return t


def _prove(t: Income, c: Classified, seen_abr: str) -> None:
    p = t.problems
    if not t.sub:
        p.append("Rechenprobe: Ertragsart (Dividende / Kapitalrückzahlung / Liquidationszahlung) nicht erkannt")
    if t.qty is None or not t.isin:
        p.append("Rechenprobe: Wertpapierzeile (Stück, Bezeichnung, ISIN, WKN) nicht lesbar")
    elif not isin_ok(t.isin):
        p.append(f"Rechenprobe: ISIN {t.isin} mit ungültiger Prüfziffer")
    if t.pay_date is None:
        p.append("Rechenprobe: Zahlbarkeitstag nicht gefunden")
    if t.gross is None:
        p.append("Rechenprobe: Bruttobetrag in EUR nicht gefunden")
    if t.total is None:
        p.append("Rechenprobe: kein Ausmachender Betrag gefunden")
    if not t.abr_no:
        p.append("Rechenprobe: Abrechnungsnummer nicht lesbar")
    elif seen_abr and c.abrechnungsnr and seen_abr != c.abrechnungsnr:
        p.append("Rechenprobe: Abrechnungsnummer weicht zwischen Klassifikation und Beleg ab")
    if t.totals > 1:
        p.append("Rechenprobe: mehrere Ausmachende Beträge (z. B. Steuererstattung) – noch nicht abgebildet")
    if t.gross is None or t.total is None:
        return
    if t.gross <= 0:
        p.append("Rechenprobe: Bruttobetrag ist keine Gutschrift")
    if any(i.value > 0 for i in t.items if i.kind != "gross"):
        p.append("Rechenprobe: positive Abzugszeile (Erstattung) bei einem Ertrag – noch nicht abgebildet")
    # Summe: Brutto + Abzüge = Ausmachender Betrag
    parts = list(t.items)
    diff = sum((i.value for i in parts), Decimal(0)) - t.total
    text = " ".join(f"{'+' if i.value >= 0 else '−'} {abs(i.value)}" for i in parts).lstrip("+ ").strip()
    if abs(diff) > PROOF_TOL:
        p.append(f"Rechenprobe: Ausmachender Betrag {t.total} ≠ Summe der Zeilen ({text}), Differenz {diff:+}")
    else:
        t.checks.append(f"Ausmachender Betrag {t.total} = {text}")
    # Richtung
    if t.total < 0:
        p.append("Rechenprobe: Ertrag, aber der Ausmachende Betrag ist eine Belastung")
    if t.direction and t.direction != "Gunsten":
        p.append(f"Rechenprobe: Ertrag, aber „zu {t.direction} des Kontos“")
    # Umrechnung
    if t.conv is not None and abs(t.conv - t.gross) > PROOF_TOL:
        p.append(f"Rechenprobe: „Umrechnung in EUR“ {t.conv} ≠ Brutto {t.gross}")
    fx_div: Decimal | None = None
    if t.gross_ccy and t.gross_ccy != "EUR":
        if t.fx is None or t.fx_ccy != t.gross_ccy:
            p.append(f"Rechenprobe: Betrag in {t.gross_ccy}, aber kein passender Devisenkurs")
            return
        fx_div = t.fx
        raw = (t.gross_foreign or Decimal(0)) / t.fx
        if abs(raw - t.gross) > PROOF_TOL + t.gross * Decimal("0.0001"):
            p.append(f"Rechenprobe: {t.gross_foreign} {t.gross_ccy} ÷ {t.fx} = {raw.quantize(CENT)} ≠ Brutto {t.gross}")
        else:
            t.checks.append(f"{t.gross_foreign} {t.gross_ccy} ÷ {t.fx} = {raw.quantize(CENT)} ≈ {t.gross}")
    # Stück × Betrag pro Stück
    if t.qty is not None and t.per_share is not None and t.gross_foreign is not None:
        if t.pay_ccy and t.gross_ccy and t.pay_ccy != t.gross_ccy:
            p.append(f"Rechenprobe: Währung pro Stück ({t.pay_ccy}) ≠ Währung des Bruttobetrags ({t.gross_ccy})")
        else:
            raw = t.qty * t.per_share
            slack = PROOF_TOL + t.qty * Decimal(5) / Decimal(10) ** (t.per_places + 1)
            if abs(raw - t.gross_foreign) > slack:
                p.append(f"Rechenprobe: {t.qty} × {t.per_share} = {raw.quantize(CENT)} ≠ Brutto {t.gross_foreign}")
            else:
                t.checks.append(f"{t.qty} × {t.per_share} = {raw.quantize(CENT)} ≈ {t.gross_foreign}")
    # Steuersätze
    for i in t.items:
        if i.kind == "gross" or i.base is None or not i.rate:
            continue
        rate = de(i.rate) / 100
        if i.kind == "qst":
            if i.base_ccy != t.gross_ccy or t.gross_foreign is None or abs(i.base - t.gross_foreign) > PROOF_TOL:
                p.append(f"Rechenprobe: Quellensteuer-Basis {i.base} {i.base_ccy} ≠ Brutto "
                         f"{t.gross_foreign} {t.gross_ccy}")
                continue
            exp = rate * i.base / (fx_div or Decimal(1))
        else:
            exp = rate * i.base
        if abs(abs(i.value) - exp) > PCT_TOL:
            p.append(f"Rechenprobe: {i.label} {i.rate} % auf {i.base} = {exp.quantize(CENT)} ≠ {abs(i.value)}")
        else:
            t.checks.append(f"{i.label} {i.rate} % auf {i.base} ≈ {abs(i.value)}")
    # Hinweise (kein Fehler, aber im Prüf-Stapel sichtbar)
    if t.sub in ("kapitalrueckzahlung", "liquidation"):
        t.hints.append(f"{KIND_TEXT[t.sub]}: von der Bank wie ein Ertrag besteuert und deshalb als Ertrag erfasst – "
                       "steuerliche Einordnung (Kapitalrückzahlung mindert sonst die Anschaffungskosten) bitte prüfen")
    if t.pay_date and t.value_date and t.pay_date.year != t.value_date.year:
        t.hints.append(f"Zahlbarkeitstag {t.pay_date:%d.%m.%Y} und Wertstellung {t.value_date:%d.%m.%Y} liegen in "
                       "verschiedenen Jahren – Steuerjahr prüfen")


def to_doctx(t: Income, doc: DocumentResult, c: Classified, n: int = 1) -> DocTx:
    """Belegvorgang mit Feldbelegen; bei nicht bestandener Rechenprobe ``kind='unknown'`` (nie buchbar)."""
    sha = doc.sha256
    tx = DocTx(n, "dividend" if t.ok else "unknown", "dividend", "dkb")
    ev = tx.evidence

    def add(fld: str, value: object, ln: Line | None, reason: str = "") -> None:
        ev.append(_derived(fld, value, sha, reason or "abgeleitet") if ln is None else
                  _ev(fld, value, ln, sha, reason=reason))

    if t.isin:
        add("isin", t.isin, t.qty_line)
        add("wkn", t.wkn, t.qty_line)
        add("asset", t.name, t.qty_line)
    if t.qty is not None:
        add("quantity", t.qty, t.qty_line)
    if t.per_share is not None:
        add("price", t.per_share, t.pay_line, f"{t.per_share} {t.pay_ccy} pro Stück laut Beleg")
        add("price_ccy", t.pay_ccy, t.pay_line)
    if t.fx is not None:
        add("fx_rate", t.fx, t.fx_line, f"Devisenkurs EUR/{t.fx_ccy} laut Beleg")
        add("fx_pair", f"EUR/{t.fx_ccy}", t.fx_line)
    if t.gross is not None:
        add("gross", t.gross, t.gross_line, "Brutto in EUR laut Beleg")
        add("value_eur", t.gross, t.gross_line, "Brutto in EUR laut Beleg")
        add("ccy", "EUR", t.gross_line, "Abrechnung in EUR")
    if t.pay_date is not None:
        add("date", t.pay_date.isoformat(), t.pay_line, "Zahlbarkeitstag laut Beleg")
    elif t.doc_date is not None:
        add("date", t.doc_date.isoformat(), None, "Belegdatum (kein Zahlbarkeitstag im Beleg)")
    if t.value_date is not None:
        add("value_date", t.value_date.isoformat(), t.value_line, "Wertstellung laut Beleg")
    if t.abr_no:
        add("ext_id", t.abr_no, None, "Abrechnungsnummer laut Beleg")
    if t.withheld:
        qs = [i for i in t.items if i.kind == "qst"]
        add("withholding_tax", t.withheld, qs[0].line,
            "Einbehaltene Quellensteuer laut Beleg: " + " + ".join(
                f"{i.rate} % auf {i.base} {i.base_ccy} = {abs(i.value)}" for i in qs))
        add("withholding_tax_ccy", "EUR", qs[0].line)
    if t.tax_burden:
        add("tax", t.tax_burden, t.tax_items[0].line,
            "Steuern laut Beleg: " + " + ".join(f"{i.label} {abs(i.value)}" for i in t.tax_items))
        add("tax_ccy", "EUR", t.tax_items[0].line)
    if t.total is not None:
        add("net", abs(t.total), t.total_line, "Ausmachender Betrag laut Beleg")
        add("net_ccy", "EUR", t.total_line)
    if t.sub:
        add("income_kind", KIND_TEXT.get(t.sub, t.sub), None, "Ertragsart laut Titel des Belegs")
    foreign = f"{t.gross_foreign} {t.gross_ccy} zu {t.fx}" if t.gross_ccy not in ("", "EUR") and t.fx else ""
    detail = " · ".join(x for x in (f"Ex-Tag {t.ex:%d.%m.%Y}" if t.ex else "",
                                    f"Bestand {t.record:%d.%m.%Y}" if t.record else "",
                                    f"Herkunft {t.country}" if t.country else "", foreign) if x)
    if detail:
        add("income_detail", detail, None, "Angaben laut Beleg")
    tx.lines = [(i.line.page, i.line.no) for i in t.items]
    tx.warnings.extend(t.problems)
    tx.warnings.extend(t.hints)
    if t.ok and t.checks:
        ev.append(FieldEvidence("proof", " · ".join(t.checks), "document", f"document:{sha}", "Rechenprobe",
                                "rekonstruiert", "Rechenprobe bestanden: " + " · ".join(t.checks)))
    tx.resolve()
    return tx


def dkb_income_doctx(doc: DocumentResult, lines: list[str], filename: str = "") -> DocTx | None:
    """Einstieg für ``profiles.analyze``: DocTx, wenn das Dokument eine DKB-Ertragsabrechnung ist."""
    from app.documentimport.dkb import classify

    c = classify(lines, filename)
    if c is None or c.kind != "ertrag" or not c.depot:
        return None
    return to_doctx(parse_income(doc, c), doc, c)


__all__ = ["Income", "Item", "dkb_income_doctx", "parse_income", "to_doctx"]
