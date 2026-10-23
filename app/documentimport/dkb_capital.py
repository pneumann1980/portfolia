"""DKB-Kapitalmaßnahmen und Ausbuchungen: Felder, Rechenprobe und Entscheidung „buchen / nur Information / ungeklärt“.

Gebucht wird nur, was der Beleg eindeutig als Bestandsänderung ausweist und die Rechenprobe besteht:

====================================  ==========================================================================
Beleg                                 Ergebnis (``DocTx.kind``)
====================================  ==========================================================================
Reverse Split / Split, Stufe 2        ``corporate_action`` (Tag ``reverse_split``/``split``): alt → neu, Einstand bleibt
Reverse Split, Stufe 1 (Sperre 1 : 1) ``info`` – keine Wirkung (gleiche ISIN, gleiche Menge)
Gratisausgabe mit Einbuchung          ``corporate_action`` (Tag ``spinoff``, Zugang) + Steuer (``tax``), Einstand =
                                      steuerrelevanter Bewertungskurs × Menge
Entgeltbelastung                      ``fee`` – Lagerstellenentgelt (Tag ``fee``), Bezug auf die Maßnahme
Wertlose / ersatzlose Ausbuchung      ``writeoff`` – Abgang ohne Erlös (Tag ``lost``), Verlust laut Beleg in der Notiz
Storno                                ``info`` – hebt die frühere Einbuchung (Belegnummer im Text) auf
Namensänderung, Ankündigungen,        ``info`` – keine Buchung (Name/Hinweis wird im Bericht gezeigt)
Chapter VII, Optionsfrist …
alles andere                          ``unknown`` – „ungeklärt“, nie geraten
====================================  ==========================================================================

Die Rechenproben prüfen Mengen und Beträge gegen die Zahlen des Belegs (Verhältnis × Ausbuchung = Einbuchung, Menge ×
Bewertungskurs = unbarer Ertrag, Steuer = Satz × Basis, Σ Steuern = Ausmachender Betrag, Verlust = Σ Anschaffungs-
kosten der „berücksichtigten Anschaffungsgeschäfte“).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.documentimport.dkb import Classified
from app.documentimport.dkb_income import PCT_TOL
from app.documentimport.dkb_trade import _AUSM, _GLUE, _TAX, AMT, PROOF_TOL, _date, de, signed
from app.documentimport.evidence import FieldEvidence
from app.documentimport.extract import DocumentResult, Line
from app.documentimport.profiles import DocTx, _derived, _ev, isin_ok

CENT = Decimal("0.01")
INFO_SUBS = {
    "insolvenz": "Mitteilung zur Insolvenz/Abwicklung des Emittenten – keine Bestandsänderung",
    "optionsfrist": "Hinweis zum Ablauf der Optionsfrist – keine Bestandsänderung",
    "liquidation": "Ankündigung einer Liquidationszahlung – die Zahlung kommt als Ertragsabrechnung",
}

_ISIN = r"[A-Z]{2}[A-Z0-9]{9}\d"
_LEG = re.compile(rf"^Stück\s+(?P<q>\d[\d.]*(?:,\d+)?)(?P<s>-)?\s+(?P<name>.+?)\s+(?P<isin>{_ISIN})\s*"
                  rf"\((?P<wkn>[A-Z0-9]{{6}})\)")
_VALUTA = re.compile(r"\bValuta\s+(\d{2}\.\d{2}\.\d{4})")
_EX = re.compile(r"^Ex-?Tag:?\s+(\d{2}\.\d{2}\.\d{4})", re.I)
_RATIO = re.compile(r"^Verhältnis:?\s+(\d+(?:,\d+)?)\s*:\s*(\d+(?:,\d+)?)")
_REF = re.compile(r"Belegnummer\s+(\d{4,})")
_BEWERTUNG = re.compile(rf"^steuerrelevanter Bewertungskurs\s+(?P<a>{AMT})\s+EUR")
_UNBAR = re.compile(rf"^Unbarer Ertrag aus Kapitalmaßnahmen\s+(?P<a>{AMT})\s+EUR")
_PER_SHARE = re.compile(rf"(?P<a>{AMT})\s+(?P<c>[A-Z]{{3}})\s+pro\s+Stück")
_FX = re.compile(rf"^Devisenkurs\s*\((?P<a>[A-Z]{{3}})/(?P<b>[A-Z]{{3}})\)\s*(?P<r>{AMT})")
_NAME_OLD = re.compile(r"^Bezeichnung alt:\s*(?P<n>.+)$")
_NAME_NEW = re.compile(r"^Bezeichnung neu:\s*(?P<n>.+)$")
_LOSS = re.compile(rf"^(?:Veräußerungsverlust|Bemessungsgrundlage für die Kapitalertragsteuer)\s+(?P<a>{AMT})"
                   rf"(?P<s>[-+])?\s*EUR")
_KURSWERT0 = re.compile(rf"^Veräußerung infolge von Kapitalmaßnahme\s*-\s*Kurswert\s+(?P<a>{AMT})\s*EUR")
_ACQ = re.compile(r"^Kauf\s+(?P<no>\d{6,})\s+(?P<d>\d{2}\.\d{2}\.\d{4})\s+Stück\s+(?P<rest>.+)$")
_AMT_SIGNED = re.compile(rf"({AMT})([-+])?")
_ORDER_TAIL = re.compile(r"Auftrags?(?:nummer|-Nr\.?)\s*:?\s*(?P<n>[\d/. ]*\d)")
_SCHLUSS = re.compile(r"^Schlusstag\s+(\d{2}\.\d{2}\.\d{4})")


@dataclass
class Leg:
    qty: Decimal
    isin: str
    wkn: str
    name: str
    line: Line


@dataclass
class Acquisition:
    order_no: str
    day: date | None
    qty: Decimal
    cost: Decimal
    line: Line


@dataclass
class Capital:
    sub: str = ""
    belegnr: str = ""
    ref: str = ""  # Bezug auf eine frühere Belegnummer (Entgelt)
    storno_of: str = ""
    head: Leg | None = None
    out: list[Leg] = field(default_factory=list)
    inn: list[Leg] = field(default_factory=list)
    valuta: date | None = None
    valuta_line: Line | None = None
    ex: date | None = None
    ratio: tuple[Decimal, Decimal] | None = None
    ratio_line: Line | None = None
    price: Decimal | None = None  # steuerrelevanter Bewertungskurs
    price_line: Line | None = None
    unbar: Decimal | None = None
    unbar_line: Line | None = None
    taxes: list[tuple[str, str, Decimal, Decimal, Line]] = field(default_factory=list)  # Label, Satz, Basis, Betrag(−)
    total: Decimal | None = None
    total_line: Line | None = None
    per_share: Decimal | None = None
    per_ccy: str = ""
    per_line: Line | None = None
    fx: Decimal | None = None
    fx_ccy: str = ""
    loss: Decimal | None = None  # positiv
    loss_line: Line | None = None
    kurswert0: bool = False
    acquisitions: list[Acquisition] = field(default_factory=list)
    order_no: str = ""
    name_old: str = ""
    name_new: str = ""
    text: str = ""
    problems: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    kind: str = "unknown"  # info | corporate_action | fee | writeoff | unknown
    info: str = ""

    @property
    def tax_burden(self) -> Decimal:
        return -sum((t[3] for t in self.taxes), Decimal(0))


def _legs(c: Capital, ln: Line, m: re.Match[str], where: str) -> None:
    leg = Leg(de(m["q"]), m["isin"], m["wkn"], m["name"].strip(), ln)
    if where == "out":
        c.out.append(leg)
    elif where == "in":
        c.inn.append(leg)
    elif c.head is None:
        c.head = leg


def parse_capital(doc: DocumentResult, cl: Classified) -> Capital:
    """Felder lesen und – je Unterart – Rechenprobe, Art des Ergebnisses und Begründung bestimmen."""
    c = Capital(sub=cl.sub, belegnr=cl.belegnr)
    where = "head"
    keep_pots = cl.sub in ("wertlos", "ersatzlose_ausbuchung")  # Anschaffungsgeschäfte stehen hinter „Nachrichtlich“
    texts: list[str] = []
    for ln in (x for x in doc.lines() if len(x.text.strip()) > 3):
        text = ln.text.strip()
        if text.startswith("Nachrichtlich") and not keep_pots:
            break
        texts.append(text)
        for s in [text] if not _GLUE.match(text) else [text, _GLUE.sub("", text, count=1)]:
            if s in ("Ausbuchung", "Einbuchung"):
                where = "out" if s == "Ausbuchung" else "in"
            elif (m := _LEG.match(s)):
                _legs(c, ln, m, where)
            elif (m := _VALUTA.search(s)) and c.valuta is None and "Konto" not in s[:m.start()]:
                c.valuta, c.valuta_line = _date(m[1]), ln
            elif (m := _EX.match(s)) and c.ex is None:
                c.ex = _date(m[1])
            elif (m := _RATIO.match(s)) and c.ratio is None:
                c.ratio, c.ratio_line = (de(m[1]), de(m[2])), ln
            elif (m := _BEWERTUNG.match(s)):
                c.price, c.price_line = de(m["a"]), ln
            elif (m := _UNBAR.match(s)):
                c.unbar, c.unbar_line = de(m["a"]), ln
            elif (m := _TAX.match(s)):
                c.taxes.append((m["l"], m["r"], de(m["b"]), signed(m["a"], m["s"] or "-"), ln))
            elif (m := _AUSM.search(s)) and c.total is None:
                c.total, c.total_line = signed(m["a"], m["s"]), ln
            elif (m := _PER_SHARE.search(s)) and c.per_share is None:
                c.per_share, c.per_ccy, c.per_line = de(m["a"]), m["c"], ln
            elif (m := _FX.match(s)) and c.fx is None:
                c.fx, c.fx_ccy = de(m["r"]), (m["a"] if m["a"] != "EUR" else m["b"])
            elif (m := _NAME_OLD.match(s)):
                c.name_old = m["n"].strip()
            elif (m := _NAME_NEW.match(s)):
                c.name_new = m["n"].strip()
            elif (m := _LOSS.match(s)) and c.loss is None:
                c.loss, c.loss_line = abs(signed(m["a"], m["s"])), ln
            elif (m := _KURSWERT0.match(s)):
                c.kurswert0 = de(m["a"]) == 0
            elif (m := _ACQ.match(s)):
                nums = _AMT_SIGNED.findall(m["rest"])
                if len(nums) >= 2:
                    c.acquisitions.append(Acquisition(re.sub(r"\D", "", m["no"]), _date(m["d"]), de(nums[0][0]),
                                                      de(nums[1][0]), ln))
            elif (m := _ORDER_TAIL.search(s)) and not c.order_no:
                c.order_no = re.sub(r"\D", "", m["n"])
            elif (m := _SCHLUSS.match(s)) and c.valuta is None and cl.sub == "ersatzlose_ausbuchung":
                c.valuta, c.valuta_line = _date(m[1]), ln
            else:
                continue
            break
    c.text = "\n".join(texts)
    refs = [r for r in _REF.findall(c.text) if r != cl.belegnr]
    c.ref = refs[0] if refs else ""
    if cl.sub == "storno":
        c.storno_of = cl.belegnr
    _decide(c, cl)
    return c


# -- Entscheidung je Unterart ---------------------------------------------------------------------------------------

def _isin_problem(c: Capital, leg: Leg | None, what: str) -> None:
    if leg is None:
        c.problems.append(f"Rechenprobe: {what} nicht lesbar")
    elif not isin_ok(leg.isin):
        c.problems.append(f"Rechenprobe: ISIN {leg.isin} mit ungültiger Prüfziffer")


def _decide(c: Capital, cl: Classified) -> None:
    sub = cl.sub
    if sub == "reverse_split" or sub == "split":
        _split(c)
    elif sub in ("gratis", "neubuchung"):
        _gratis(c)
    elif sub == "entgelt":
        _fee(c)
    elif sub in ("wertlos", "ersatzlose_ausbuchung"):
        _writeoff(c, cl)
    elif sub == "storno":
        c.kind = "info"
        c.info = f"Storno der Einbuchung mit Belegnummer {c.belegnr}: hebt die frühere Buchung auf"
    elif sub == "umbenennung":
        c.kind = "info"
        names = f"{c.name_old} → {c.name_new}" if c.name_old and c.name_new else "Name nicht lesbar"
        c.info = f"Namensänderung des Emittenten ({names}); ISIN und Bestand unverändert"
    elif sub in INFO_SUBS:
        c.kind = "info"
        c.info = INFO_SUBS[sub]
    else:
        c.kind = "unknown"
        c.problems.append("Rechenprobe: Art der Kapitalmaßnahme nicht abgebildet – bitte von Hand prüfen" if sub else
                          "Rechenprobe: Art der Kapitalmaßnahme nicht erkannt")


def _split(c: Capital) -> None:
    if not c.out or not c.inn:
        c.kind = "info"
        c.info = "Ankündigung einer Zusammenlegung/Teilung ohne Ein-/Ausbuchung – keine Bestandsänderung"
        return
    a, b = c.out[0], c.inn[0]
    if len(c.out) > 1 or len(c.inn) > 1:
        c.problems.append("Rechenprobe: mehrere Ein-/Ausbuchungen in einem Beleg – noch nicht abgebildet")
        return
    if a.isin == b.isin and a.qty == b.qty:  # Stufe 1: Sperrumbuchung
        c.kind = "info"
        c.info = "Sperrumbuchung vor der Zusammenlegung (gleiche ISIN, gleiche Menge) – keine Bestandsänderung"
        return
    _isin_problem(c, a, "Ausbuchung")
    _isin_problem(c, b, "Einbuchung")
    if c.valuta is None:
        c.problems.append("Rechenprobe: Valuta nicht gefunden")
    if c.ratio is None:
        c.problems.append("Rechenprobe: Verhältnis nicht gefunden")
    elif c.ratio[0] <= 0 or c.ratio[1] <= 0:
        c.problems.append("Rechenprobe: Verhältnis ungültig")
    else:
        exp = a.qty * c.ratio[1] / c.ratio[0]
        # Bruchteile werden von der Bank separat reguliert: Einbuchung ≤ rechnerischer Wert, Rest < 1 Stück
        if b.qty > exp + Decimal("0.00001") or exp - b.qty >= 1:
            c.problems.append(f"Rechenprobe: {a.qty} × {c.ratio[1]} / {c.ratio[0]} = {exp.normalize():f} ≠ "
                              f"Einbuchung {b.qty}")
        else:
            c.checks.append(f"{a.qty} × {c.ratio[1]} / {c.ratio[0]} = {exp.normalize():f} ≈ Einbuchung {b.qty}")
            if exp != b.qty:
                c.hints.append(f"Bruchteil {exp - b.qty} wird von der Bank separat reguliert (Festpreisgeschäft)")
    if c.ex and c.valuta and c.ex != c.valuta:
        c.hints.append(f"Ex-Tag {c.ex:%d.%m.%Y} und Valuta {c.valuta:%d.%m.%Y} weichen ab – Valuta gilt")
    c.kind = "unknown" if c.problems else "corporate_action"


def _gratis(c: Capital) -> None:
    if not c.inn:
        c.kind = "info"
        c.info = "Ankündigung einer Gratisausgabe ohne Einbuchung – keine Bestandsänderung"
        return
    b = c.inn[0]
    _isin_problem(c, b, "Einbuchung")
    if len(c.inn) > 1 or c.out:
        c.problems.append("Rechenprobe: Ein- und Ausbuchung gemischt – noch nicht abgebildet")
    if c.valuta is None:
        c.problems.append("Rechenprobe: Valuta nicht gefunden")
    if c.price is not None and c.unbar is not None:
        exp = b.qty * c.price
        if abs(exp - c.unbar) > PROOF_TOL + b.qty * Decimal("0.0005"):
            c.problems.append(f"Rechenprobe: {b.qty} × Bewertungskurs {c.price} = {exp.quantize(CENT)} ≠ "
                              f"Unbarer Ertrag {c.unbar}")
        else:
            c.checks.append(f"{b.qty} × {c.price} = {exp.quantize(CENT)} ≈ unbarer Ertrag {c.unbar}")
        for label, rate, base, amount, _ln in c.taxes:
            e = de(rate) / 100 * base
            if abs(abs(amount) - e) > PCT_TOL:
                c.problems.append(f"Rechenprobe: {label} {rate} % auf {base} = {e.quantize(CENT)} ≠ {abs(amount)}")
            else:
                c.checks.append(f"{label} {rate} % auf {base} ≈ {abs(amount)}")
        if c.total is not None:
            diff = c.tax_burden + c.total
            if abs(diff) > PROOF_TOL:
                c.problems.append(f"Rechenprobe: Ausmachender Betrag {c.total} ≠ Summe der Steuern {c.tax_burden}")
            else:
                c.checks.append(f"Ausmachender Betrag {c.total} = Steuern {c.tax_burden}")
        elif c.taxes:
            c.problems.append("Rechenprobe: Steuerzeilen ohne Ausmachenden Betrag")
    elif c.price is None and c.unbar is None:
        c.hints.append("Kein steuerrelevanter Bewertungskurs im Beleg (Einstand und Steuer nicht belegt) – meist folgt "
                       "eine Neubuchung nach Storno mit Bewertung")
    else:
        c.problems.append("Rechenprobe: Bewertungskurs und unbarer Ertrag nicht vollständig")
    c.kind = "unknown" if c.problems else "corporate_action"


def _fee(c: Capital) -> None:
    h = c.head
    _isin_problem(c, h, "Wertpapierzeile")
    if c.total is None:
        c.problems.append("Rechenprobe: Ausmachender Betrag nicht gefunden")
    elif c.total >= 0:
        c.problems.append("Rechenprobe: Entgeltbelastung, aber der Ausmachende Betrag ist eine Gutschrift")
    if c.valuta is None:
        c.problems.append("Rechenprobe: Valuta nicht gefunden")
    if not c.ref:
        c.hints.append("Kein Bezug auf eine Belegnummer der Kapitalmaßnahme im Text")
    if h is not None and c.per_share is not None and c.total is not None:
        raw = h.qty * c.per_share
        if c.per_ccy != "EUR":
            if c.fx is None or c.fx_ccy != c.per_ccy:
                c.problems.append(f"Rechenprobe: Entgelt in {c.per_ccy}, aber kein passender Devisenkurs")
                raw = Decimal(-1)
            else:
                raw /= c.fx
        if raw >= 0 and abs(raw - abs(c.total)) > PROOF_TOL + Decimal("0.001"):
            c.problems.append(f"Rechenprobe: {h.qty} × {c.per_share} {c.per_ccy}"
                              f"{'' if c.per_ccy == 'EUR' else f' ÷ {c.fx}'} = {raw.quantize(CENT)} ≠ "
                              f"Ausmachender Betrag {abs(c.total)}")
        elif raw >= 0:
            c.checks.append(f"{h.qty} × {c.per_share} {c.per_ccy}{'' if c.per_ccy == 'EUR' else f' ÷ {c.fx}'} = "
                            f"{raw.quantize(CENT)} ≈ {abs(c.total)}")
    elif c.per_share is None:
        c.problems.append("Rechenprobe: Entgelt je Stück nicht gefunden")
    c.kind = "unknown" if c.problems else "fee"


def _writeoff(c: Capital, cl: Classified) -> None:
    leg = c.out[0] if c.out else c.head
    _isin_problem(c, leg, "Ausbuchung")
    if c.valuta is None:
        c.problems.append("Rechenprobe: Valuta nicht gefunden")
    if cl.sub == "ersatzlose_ausbuchung" and not c.order_no:
        c.problems.append("Rechenprobe: Auftragsnummer nicht lesbar")
    if cl.sub == "wertlos" and not c.kurswert0:
        c.problems.append("Rechenprobe: Veräußerung infolge von Kapitalmaßnahme mit Kurswert 0,00 EUR nicht belegt")
    if c.loss is None:
        c.problems.append("Rechenprobe: Verlust laut Beleg nicht gefunden")
    if leg is not None and c.acquisitions:
        qty = sum((a.qty for a in c.acquisitions), Decimal(0))
        cost = sum((a.cost for a in c.acquisitions), Decimal(0))
        if abs(qty - leg.qty) > Decimal("0.0001"):
            c.problems.append(f"Rechenprobe: Menge der Anschaffungsgeschäfte {qty} ≠ Ausbuchung {leg.qty}")
        elif c.loss is not None and abs(cost - c.loss) > PROOF_TOL:
            c.problems.append(f"Rechenprobe: Anschaffungskosten {cost} ≠ Verlust {c.loss}")
        else:
            c.checks.append(f"{leg.qty} Stück: Anschaffungskosten {cost} = Verlust {c.loss}")
    elif not c.acquisitions:
        c.hints.append("Anschaffungsgeschäfte im Beleg nicht lesbar – Verlust nicht gegen die Käufe geprüft")
    c.kind = "unknown" if c.problems else "writeoff"


# -- DocTx ------------------------------------------------------------------------------------------------------------

def to_doctx(c: Capital, doc: DocumentResult, cl: Classified, n: int = 1) -> DocTx:
    sha = doc.sha256
    tx = DocTx(n, c.kind if not c.problems else "unknown", "corporate_action", "dkb")
    ev = tx.evidence

    def add(fld: str, value: object, ln: Line | None, reason: str = "") -> None:
        ev.append(_derived(fld, value, sha, reason or "abgeleitet") if ln is None else
                  _ev(fld, value, ln, sha, reason=reason))

    sub_label = {"reverse_split": "Zusammenlegung (Reverse Split)", "split": "Aktiensplit", "gratis": "Gratisausgabe",
                 "neubuchung": "Gratisausgabe (Neubuchung nach Storno)", "entgelt": "Entgelt zur Kapitalmaßnahme",
                 "wertlos": "Wertlose Ausbuchung", "ersatzlose_ausbuchung": "Ersatzlose Ausbuchung"}.get(cl.sub, cl.sub)
    add("ca_kind", sub_label, None, "Art laut Titel des Belegs")
    if c.kind == "corporate_action":
        add("ca_tag", cl.sub if cl.sub in ("reverse_split", "split") else "spinoff", None, "Buchungsart laut Titel")
    base = c.out[0] if c.out else c.head
    if c.kind == "corporate_action" and cl.sub in ("reverse_split", "split"):
        a, b = c.out[0], c.inn[0]
        add("isin", a.isin, a.line)
        add("wkn", a.wkn, a.line)
        add("asset", a.name, a.line)
        add("quantity", a.qty, a.line)
        add("isin_new", b.isin, b.line)
        add("wkn_new", b.wkn, b.line)
        add("asset_new", b.name, b.line)
        add("quantity_new", b.qty, b.line)
        if c.ratio:
            add("ratio", f"{c.ratio[0].normalize():f} : {c.ratio[1].normalize():f}", c.ratio_line)
    elif c.kind == "corporate_action":  # Gratisausgabe: gebucht wird der Zugang
        b = c.inn[0]
        add("isin", b.isin, b.line)
        add("wkn", b.wkn, b.line)
        add("asset", b.name, b.line)
        add("quantity", b.qty, b.line)
        if c.head is not None and c.head.isin != b.isin:
            add("ca_detail", f"je {c.ratio[0].normalize():f} : {c.ratio[1].normalize():f} auf {c.head.name} "
                f"({c.head.isin})" if c.ratio else f"auf {c.head.name} ({c.head.isin})", None, "Angaben laut Beleg")
        if c.price is not None and c.unbar is not None:
            add("valuation_price", c.price, c.price_line, "steuerrelevanter Bewertungskurs laut Beleg")
            add("value_eur", c.unbar, c.unbar_line, "Unbarer Ertrag aus Kapitalmaßnahmen laut Beleg (Einstand)")
            add("gross", c.unbar, c.unbar_line, "Unbarer Ertrag aus Kapitalmaßnahmen laut Beleg")
            add("ccy", "EUR", c.unbar_line, "Beleg in EUR")
        if c.tax_burden:
            add("tax", c.tax_burden, c.taxes[0][4], "Steuern laut Beleg: " + " + ".join(
                f"{t[0]} {abs(t[3])}" for t in c.taxes))
            add("tax_ccy", "EUR", c.taxes[0][4])
        if c.total is not None:
            add("net", abs(c.total), c.total_line, "Ausmachender Betrag laut Beleg")
            add("net_ccy", "EUR", c.total_line)
    elif c.kind == "fee" and c.head is not None:
        h = c.head
        add("isin", h.isin, h.line)
        add("wkn", h.wkn, h.line)
        add("asset", h.name, h.line)
        add("quantity", h.qty, h.line)
        amount = abs(c.total or Decimal(0))
        add("gross", amount, c.total_line, "Ausmachender Betrag laut Beleg")
        add("value_eur", amount, c.total_line, "Ausmachender Betrag laut Beleg")
        add("fee", amount, c.total_line, f"{c.per_share} {c.per_ccy} je Stück")
        add("fee_ccy", "EUR", c.total_line)
        add("ccy", "EUR", c.total_line)
        if c.per_share is not None:
            add("price", c.per_share, c.per_line, f"{c.per_share} {c.per_ccy} je Stück laut Beleg")
            add("price_ccy", c.per_ccy, c.per_line)
        if c.fx is not None:
            add("fx_rate", c.fx, None, "Devisenkurs laut Beleg")
        if c.ref:
            add("ca_detail", f"Bezug: Belegnummer {c.ref}", None, "Angabe laut Beleg")
    elif c.kind == "writeoff" and base is not None:
        add("isin", base.isin, base.line)
        add("wkn", base.wkn, base.line)
        add("asset", base.name, base.line)
        add("quantity", base.qty, base.line)
        add("value_eur", Decimal(0), c.loss_line, "ersatzlose Ausbuchung: kein Erlös")
        add("gross", Decimal(0), c.loss_line, "ersatzlose Ausbuchung: kein Erlös")
        add("ccy", "EUR", c.loss_line, "Beleg in EUR")
        if c.loss is not None:
            add("loss", c.loss, c.loss_line, "Verlust laut Beleg")
        if c.acquisitions:
            add("ca_detail", "Anschaffung: " + "; ".join(
                f"Kauf {a.order_no} vom {a.day:%d.%m.%Y} ({a.qty} Stück, {a.cost} €)" if a.day else
                f"Kauf {a.order_no} ({a.qty} Stück, {a.cost} €)" for a in c.acquisitions[:5]), None,
                "Berücksichtigte Anschaffungsgeschäfte laut Beleg")
    else:  # info / unknown: so viel Kontext wie sicher lesbar
        if base is not None:
            add("isin", base.isin, base.line)
            add("wkn", base.wkn, base.line)
            add("asset", base.name, base.line)
            add("quantity", base.qty, base.line)
        if c.info:
            add("info", c.info, None, "Auswertung des Belegs")
    day = c.valuta
    if day is not None:
        add("date", day.isoformat(), c.valuta_line, "Valuta laut Beleg")
    elif cl.doc_date:
        add("date", cl.doc_date, None, "Belegdatum (keine Valuta im Beleg)")
    ext = cl.order_no if cl.sub == "ersatzlose_ausbuchung" and cl.order_no else \
        (f"{c.belegnr}-storno" if cl.sub == "storno" else c.belegnr or cl.order_no)
    if ext:
        add("ext_id", ext, None, "Beleg-/Auftragsnummer laut Beleg")
    if c.belegnr:
        add("belegnr", c.belegnr, None, "Belegnummer laut Beleg")
    tx.lines = [(x.line.page, x.line.no) for x in (*c.out, *c.inn) if x is not None]
    tx.warnings.extend(c.problems)
    tx.warnings.extend(c.hints)
    if not c.problems and c.checks:
        ev.append(FieldEvidence("proof", " · ".join(c.checks), "document", f"document:{sha}", "Rechenprobe",
                                "rekonstruiert", "Rechenprobe bestanden: " + " · ".join(c.checks)))
    tx.resolve()
    return tx


def dkb_capital_doctx(doc: DocumentResult, lines: list[str], filename: str = "") -> DocTx | None:
    """Einstieg für ``profiles.analyze``: DocTx, wenn das Dokument eine DKB-Kapitalmaßnahme bzw. Ausbuchung ist."""
    from app.documentimport.dkb import classify

    cl = classify(lines, filename)
    if cl is None or not cl.depot:
        return None
    if cl.kind == "kapitalmassnahme" or (cl.kind == "depotuebertrag" and cl.sub == "ersatzlose_ausbuchung"):
        return to_doctx(parse_capital(doc, cl), doc, cl)
    return None


__all__ = ["Capital", "dkb_capital_doctx", "parse_capital", "to_doctx"]
