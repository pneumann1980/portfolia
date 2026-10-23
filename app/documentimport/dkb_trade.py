"""DKB-Wertpapierabrechnung (Kauf, Verkauf, Festpreisgeschäft): Felder, Gebührensumme, Steuern und **Rechenprobe**.

Die Abrechnung ist die verbindliche Quelle (Regel R1): Alle Beträge stehen in EUR, auch bei US-/NOK-Titeln; nur Kurs,
Limit und Devisenkurs nennen die Fremdwährung. Vorzeichen stehen als **Suffix** (``-`` Belastung, ``+``/ohne
Gutschrift). Zwischen ``Kurswert`` und ``Ausmachender Betrag`` stehen die Kosten (Summe über **alle** Zeilen, auch
unbekannte Beschriftungen) und – bei Verkäufen – die Steuerzeilen ``Kapitalertragsteuer <p> % auf``, ``Solidaritäts-
zuschlag … auf``, ``Kirchensteuer … auf``. Hinweise für Berechnungsgrundlagen (``Bemessungsgrundlage für die
Kapitalertragsteuer``, ``Verrechnete …``) sind **keine** Beträge.

Ein Dokument kann **mehrere** ``Ausmachender Betrag`` haben (Verkauf mit Verlustverrechnung: Seite 2 mit positiven
Steuerzeilen = Erstattung). Jeder Block wird mit seinen Zeilen einzeln geprüft::

    Σ (vorzeichenbehaftete Zeilen des Blocks) = Ausmachender Betrag des Blocks         (± 0,01 €)
    Menge × Ausführungskurs [÷ Devisenkurs] ≈ Kurswert                                  (Rundung des Kurses)

Nicht bestanden → das Ergebnis trägt eine Begründung (``problems``) und die Zeile wird **nicht** buchbar
(„ungeklärt“). Nichts wird geraten oder passend gerechnet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal

from app.documentimport.dkb import Classified
from app.documentimport.evidence import FieldEvidence
from app.documentimport.extract import DocumentResult, Line
from app.documentimport.profiles import DocTx, _derived, _ev, isin_ok

CENT = Decimal("0.01")
PROOF_TOL = Decimal("0.01")
QTY_TOL = Decimal("0.02")  # Menge × Kurs ≈ Kurswert, zuzüglich Rundung des gedruckten Kurses
TAX_LABELS = ("Kapitalertragsteuer", "Solidaritätszuschlag", "Kirchensteuer")
NOT_FEE = re.compile(r"^(Finanztransaktionssteuer|Stückzinsen|Zinsen)\b", re.I)  # eigene Behandlung nötig → ungeklärt
STOP_FEES = re.compile(r"^(Ermittlung|Steuerberechnung|Ausmachender Betrag|Verrechnete|Bemessungsgrundlage|"
                       r"Berechnungsgrundlage|Veräußerung|Eingebuchte|Steuerliche|Kapitalertragsteuer|"
                       r"Solidaritätszuschlag|Kirchensteuer|Den Gegenwert|Die Wertpapiere)", re.I)
AMT = r"\d[\d.]*,\d+"
_GLUE = re.compile(r"^\S{1,4}\s{2,}")  # gedrehter Randtext vor dem eigentlichen Text („70  Ausmachender Betrag …“)
_KURSWERT = re.compile(rf"^Kurswert\s+(?P<a>{AMT})(?P<s>[-+])?\s+(?P<c>[A-Z]{{3}})\b")
_FEE = re.compile(rf"^(?P<l>[A-ZÄÖÜ][\wÄÖÜäöüß.\-/ ()]*?)\s+(?P<a>{AMT})(?P<s>[-+])?\s+EUR\s*$")
_TAX = re.compile(rf"^(?P<l>{'|'.join(TAX_LABELS)})\s+(?P<r>\d[\d,]*)\s*%\s+auf\s+(?P<b>{AMT})\s+EUR\s+"
                  rf"(?P<a>{AMT})(?P<s>[-+])?\s+EUR")
_AUSM = re.compile(rf"(?:^|\s)Ausmachender Betrag\s+(?P<a>{AMT})(?P<s>[-+])?\s+(?P<c>[A-Z]{{3}})\b")
_STUECK = re.compile(r"^Stück\s+(?P<q>\d[\d.]*(?:,\d+)?)\s+(?P<name>.+?)\s+(?P<isin>[A-Z]{2}[A-Z0-9]{9}\d)\s*"
                     r"\((?P<wkn>[A-Z0-9]{6})\)")
_PRICE = re.compile(rf"^(?:Ausführungskurs|Abrech\.?-?\s?Preis)\s+(?P<p>{AMT})\s+(?P<c>[A-Z]{{3}})\b")
_FX = re.compile(rf"^Devisenkurs\s*\(EUR/(?P<c>[A-Z]{{3}})\)\s*(?P<r>{AMT})\s+vom\s+(?P<d>\d{{2}}\.\d{{2}}\.\d{{4}})")
_CLOSE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2}):(\d{2})")
_DATE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
_ORDER = re.compile(r"^Auftragsnummer\s+(?P<n>[\d/. ]*\d)")
_VALUTA = re.compile(r"Valuta\s+(\d{2}\.\d{2}\.\d{4})\s+zu\s+(Lasten|Gunsten)")


def de(s: str) -> Decimal:
    """Deutsche Zahl (``1.234,56``, ``1.000``, ``0,3``) → Decimal; Punkt ohne Komma = Tausendertrenner."""
    return Decimal(s.replace(".", "").replace(",", "."))


def signed(amount: str, sign: str | None) -> Decimal:
    v = de(amount)
    return -v if sign == "-" else v


def _date(s: str) -> date | None:
    m = _DATE.search(s)
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


@dataclass
class Item:
    kind: str  # kurswert | fee | tax
    label: str
    value: Decimal  # vorzeichenbehaftet (− Belastung)
    line: Line
    rate: str = ""
    base: Decimal | None = None


@dataclass
class Block:
    items: list[Item] = field(default_factory=list)
    total: Decimal | None = None  # vorzeichenbehafteter Ausmachender Betrag
    total_line: Line | None = None

    @property
    def check(self) -> Decimal | None:
        return None if self.total is None else sum((i.value for i in self.items), Decimal(0)) - self.total


@dataclass
class Trade:
    side: str = ""
    order_no: str = ""
    isin: str = ""
    wkn: str = ""
    name: str = ""
    qty: Decimal | None = None
    qty_line: Line | None = None
    price: Decimal | None = None
    price_dec: int = 0
    price_ccy: str = ""
    price_line: Line | None = None
    fx: Decimal | None = None
    fx_ccy: str = ""
    fx_line: Line | None = None
    kurswert: Decimal | None = None  # Betrag, positiv
    kurswert_line: Line | None = None
    close: tuple[date, time] | None = None
    close_line: Line | None = None
    doc_date: date | None = None
    doc_date_line: Line | None = None
    order_at: str = ""
    value_date: date | None = None
    value_line: Line | None = None
    direction: str = ""  # Lasten | Gunsten
    blocks: list[Block] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)

    @property
    def fees(self) -> list[Item]:
        return [i for b in self.blocks for i in b.items if i.kind == "fee"]

    @property
    def taxes(self) -> list[Item]:
        """Steuerzeilen (− Belastung, + Erstattung)."""
        return [i for b in self.blocks for i in b.items if i.kind == "tax"]

    @property
    def fee_total(self) -> Decimal:
        return -sum((i.value for i in self.fees), Decimal(0))

    @property
    def tax_burden(self) -> Decimal:
        return -sum((i.value for i in self.taxes if i.value < 0), Decimal(0))

    @property
    def tax_refund(self) -> Decimal:
        return sum((i.value for i in self.taxes if i.value > 0), Decimal(0))

    @property
    def cash(self) -> Decimal:
        """Gesamtbewegung auf dem Verrechnungskonto laut Beleg (Summe aller Ausmachenden Beträge)."""
        return sum((b.total for b in self.blocks if b.total is not None), Decimal(0))

    @property
    def ok(self) -> bool:
        return not self.problems


def _amount_places(price_text: str) -> int:
    return len(price_text.split(",", 1)[1]) if "," in price_text else 0


def parse_trade(doc: DocumentResult, c: Classified) -> Trade:
    """Felder und Rechenprobe einer Kauf-/Verkaufsabrechnung. Probleme stehen in ``Trade.problems``."""
    t = Trade(side=c.side)
    lines = [ln for ln in doc.lines() if len(ln.text.strip()) > 3]
    block = Block()
    fee_zone = False
    for ln in lines:
        text = ln.text.strip()
        cands = [text] if not _GLUE.match(text) else [text, _GLUE.sub("", text, count=1)]
        if text.startswith("Nachrichtlich"):
            break
        for s in cands:
            if (m := _STUECK.match(s)) and t.qty is None:
                t.qty, t.qty_line = de(m["q"]), ln
                t.name, t.isin, t.wkn = m["name"].strip(), m["isin"], m["wkn"]
                break
            if (m := _PRICE.match(s)) and t.price is None:
                t.price, t.price_ccy, t.price_line = de(m["p"]), m["c"], ln
                t.price_dec = _amount_places(m["p"])
                break
            if (m := _FX.match(s)) and t.fx is None:
                t.fx, t.fx_ccy, t.fx_line = de(m["r"]), m["c"], ln
                break
            if (m := _ORDER.match(s)) and not t.order_no:
                t.order_no = re.sub(r"\D", "", m["n"])
                break
            if s.startswith("Schlusstag/-Zeit") and t.close is None:
                if (m := _CLOSE.search(s)):
                    t.close = (date(int(m[3]), int(m[2]), int(m[1])), time(int(m[4]), int(m[5]), int(m[6])))
                    t.close_line = ln
                break
            if s.startswith("Auftrag vom") and not t.order_at:
                t.order_at = s[len("Auftrag vom"):].strip().removesuffix("Uhr").strip()
                break
            if s.startswith("Datum") and t.doc_date is None and (d := _date(s)):
                t.doc_date, t.doc_date_line = d, ln
                break
            if (m := _KURSWERT.match(s)):
                if m["c"] != "EUR":
                    t.problems.append(f"Kurswert in {m['c']} statt EUR – nicht unterstützt")
                t.kurswert, t.kurswert_line = de(m["a"]), ln
                block.items.append(Item("kurswert", "Kurswert", signed(m["a"], m["s"]), ln))
                fee_zone = True
                break
            if (m := _AUSM.search(s)):
                if m["c"] != "EUR":
                    t.problems.append(f"Ausmachender Betrag in {m['c']} statt EUR – nicht unterstützt")
                block.total, block.total_line = signed(m["a"], m["s"]), ln
                t.blocks.append(block)
                block, fee_zone = Block(), False
                break
            if (m := _TAX.match(s)):
                block.items.append(Item("tax", m["l"], signed(m["a"], m["s"]), ln, rate=m["r"], base=de(m["b"])))
                fee_zone = False
                break
            if (m := _VALUTA.search(s)) and t.value_date is None:
                t.value_date, t.value_line, t.direction = _date(m[1]), ln, m[2]
                break
            if fee_zone:
                if STOP_FEES.match(s):
                    fee_zone = False
                    break
                if (m := _FEE.match(s)):
                    if NOT_FEE.match(m["l"]):
                        t.problems.append(f"Rechenprobe: Position „{m['l']}“ ist keine Gebühr und noch nicht "
                                          "abgebildet")
                    block.items.append(Item("fee", m["l"].strip(), signed(m["a"], m["s"] or "-"), ln))
                    break
    if block.items and block.total is None:
        t.problems.append("Rechenprobe: Block ohne Ausmachenden Betrag")
    _prove(t, c)
    return t


def _prove(t: Trade, c: Classified) -> None:
    if t.qty is None or not t.isin:
        t.problems.append("Rechenprobe: Wertpapierzeile (Stück, Bezeichnung, ISIN, WKN) nicht lesbar")
    elif not isin_ok(t.isin):
        t.problems.append(f"Rechenprobe: ISIN {t.isin} mit ungültiger Prüfziffer")
    if t.kurswert is None:
        t.problems.append("Rechenprobe: Kurswert nicht gefunden")
    if not t.blocks:
        t.problems.append("Rechenprobe: kein Ausmachender Betrag gefunden")
        return
    if not t.order_no:
        t.problems.append("Rechenprobe: Auftragsnummer nicht lesbar")
    # Summe je Block
    for i, b in enumerate(t.blocks, 1):
        diff = b.check
        if diff is None:
            continue
        label = "Ausmachender Betrag" if len(t.blocks) == 1 else f"Ausmachender Betrag {i}"
        parts = " ".join(f"{'+' if x.value >= 0 else '−'} {abs(x.value)}" for x in b.items).lstrip("+ ").strip()
        if abs(diff) > PROOF_TOL:
            t.problems.append(f"Rechenprobe: {label} {b.total} ≠ Summe der Zeilen ({parts}), Differenz {diff:+}")
        else:
            t.checks.append(f"{label} {b.total} = {parts}")
    # Richtung: Titel, Vorzeichen des Gesamtbetrags und „zu Lasten/Gunsten“ müssen übereinstimmen
    first = t.blocks[0].total
    if first is not None and t.side:
        if t.side == "buy" and first > 0:
            t.problems.append("Rechenprobe: Kauf, aber der Ausmachende Betrag ist eine Gutschrift")
        if t.side == "sell" and first < 0:
            t.problems.append("Rechenprobe: Verkauf, aber der Ausmachende Betrag ist eine Belastung")
    if t.direction and t.side and t.direction != ("Lasten" if t.side == "buy" else "Gunsten"):
        t.problems.append(f"Rechenprobe: Titel und „zu {t.direction} des Kontos“ widersprechen sich")
    # Menge × Kurs [÷ Devisenkurs] ≈ Kurswert
    if t.qty is not None and t.price is not None and t.kurswert is not None:
        raw = t.qty * t.price
        ccy_note = ""
        if t.price_ccy and t.price_ccy != "EUR":
            if t.fx is None or t.fx_ccy != t.price_ccy:
                t.problems.append(f"Rechenprobe: Kurs in {t.price_ccy}, aber kein passender Devisenkurs")
                return
            raw /= t.fx
            ccy_note = f" ÷ {t.fx} ({t.price_ccy}/EUR)"
        # Rundung des gedruckten Kurses (Anzahl Nachkommastellen) wirkt mit der Menge
        slack = QTY_TOL + t.qty * Decimal(5) / Decimal(10) ** (t.price_dec + 1) / (t.fx or Decimal(1))
        if abs(raw - t.kurswert) > slack:
            t.problems.append(f"Rechenprobe: Menge × Kurs{ccy_note} = {raw.quantize(CENT)} ≠ Kurswert {t.kurswert}")
        else:
            t.checks.append(f"{t.qty} × {t.price}{ccy_note} = {raw.quantize(CENT)} ≈ Kurswert {t.kurswert}")
    elif t.price is None and not t.problems:
        t.checks.append("kein Ausführungskurs im Beleg (Kurs aus Kurswert ÷ Menge abgeleitet)")
    if c.order_no and t.order_no and c.order_no != t.order_no:
        t.problems.append("Rechenprobe: Auftragsnummer weicht zwischen Klassifikation und Beleg ab")


def to_doctx(t: Trade, doc: DocumentResult, c: Classified, n: int = 1) -> DocTx:
    """Belegvorgang mit Feldbelegen; bei nicht bestandener Rechenprobe ``kind='unknown'`` (nie buchbar)."""
    sha = doc.sha256
    tx = DocTx(n, t.side if t.ok and t.side in ("buy", "sell") else "unknown", "securities_trade", "dkb")
    ev = tx.evidence

    def add(fld: str, value: object, ln: Line | None, reason: str = "", status: str = "belegt") -> None:
        if ln is None:
            ev.append(_derived(fld, value, sha, reason or "abgeleitet", status=status))
        else:
            ev.append(_ev(fld, value, ln, sha, reason=reason, status=status))

    if t.isin:
        add("isin", t.isin, t.qty_line)
        add("wkn", t.wkn, t.qty_line)
        add("asset", t.name, t.qty_line)
    if t.qty is not None:
        add("quantity", t.qty, t.qty_line)
    if t.price is not None:
        add("price", t.price, t.price_line, f"Ausführungskurs {t.price} {t.price_ccy} laut Beleg")
        add("price_ccy", t.price_ccy, t.price_line)
    if t.fx is not None:
        add("fx_rate", t.fx, t.fx_line, f"Devisenkurs EUR/{t.fx_ccy} laut Beleg")
        add("fx_pair", f"EUR/{t.fx_ccy}", t.fx_line)
    if t.kurswert is not None:
        add("gross", t.kurswert, t.kurswert_line, "Kurswert in EUR laut Beleg")
        add("value_eur", t.kurswert, t.kurswert_line, "Kurswert in EUR laut Beleg")
        add("ccy", "EUR", t.kurswert_line, "Abrechnung in EUR")
    if t.close is not None:
        add("date", t.close[0].isoformat(), t.close_line, "Schlusstag laut Beleg (Ortszeit Deutschland)")
        add("time", t.close[1].isoformat(), t.close_line, "Schlusszeit laut Beleg (deutsche Ortszeit)")
    elif t.doc_date is not None:
        add("date", t.doc_date.isoformat(), t.doc_date_line, "Belegdatum (kein Schlusstag im Beleg)")
    if t.value_date is not None:
        add("value_date", t.value_date.isoformat(), t.value_line, "Valuta laut Beleg")
    if t.order_no:
        add("ext_id", t.order_no, None, "Auftragsnummer laut Beleg (nur Ziffern)")
    fees = t.fees
    if fees:
        detail = " + ".join(f"{i.label} {abs(i.value)}" for i in fees)
        total = t.fee_total
        add("fee", total, fees[0].line, f"Summe der Kosten: {detail}")
        add("fee_ccy", "EUR", fees[0].line)
        add("fee_eur", total, fees[0].line, f"Summe der Kosten in EUR: {detail}")
        add("fee_detail", detail, fees[0].line)
    if t.tax_burden:
        add("tax", t.tax_burden, t.taxes[0].line,
            "Steuern laut Beleg: " + " + ".join(f"{i.label} {abs(i.value)}" for i in t.taxes if i.value < 0))
        add("tax_ccy", "EUR", t.taxes[0].line)
    if t.tax_refund:
        add("tax_refund", t.tax_refund, next(i.line for i in t.taxes if i.value > 0),
            "Steuererstattung laut Beleg: " + " + ".join(f"{i.label} {i.value}" for i in t.taxes if i.value > 0))
    first = t.blocks[0] if t.blocks else None
    if first is not None and first.total is not None:
        add("net", abs(first.total), first.total_line, "Ausmachender Betrag laut Beleg")
        add("net_ccy", "EUR", first.total_line)
    tx.lines = [(i.line.page, i.line.no) for b in t.blocks for i in b.items]
    for problem in t.problems:
        tx.warnings.append(problem)
    if t.ok and t.checks:
        ev.append(FieldEvidence("proof", " · ".join(t.checks), "document", f"document:{sha}", "Rechenprobe",
                                "rekonstruiert", "Rechenprobe bestanden: " + " · ".join(t.checks)))
    tx.resolve()
    return tx


def dkb_trade_doctx(doc: DocumentResult, lines: list[str], filename: str = "") -> DocTx | None:
    """Einstieg für ``profiles.analyze``: DocTx, wenn das Dokument eine DKB-Kauf-/Verkaufsabrechnung ist."""
    from app.documentimport.dkb import classify

    c = classify(lines, filename)
    if c is None or c.kind not in ("abrechnung_kauf", "abrechnung_verkauf") or not c.depot:
        return None  # DKB-Erkennung (Analyse 5.2): Depotnummer im Kopf und Titel „Wertpapier Abrechnung …“
    return to_doctx(parse_trade(doc, c), doc, c)


__all__ = ["Block", "Item", "Trade", "de", "dkb_trade_doctx", "parse_trade", "signed", "to_doctx"]
