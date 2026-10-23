"""DKB-Kosteninformation (MiFID II, „Ex-Ante“) und Order-Belege (Ausführungsanzeige, Auftragsbestätigung, Streichung).

**Kosteninformation = Order-Spur, kein Buchungsbeleg** (Analyse 6.8): Sie entsteht bei jeder Ordereingabe und nennt
Wertpapier, Zeitpunkt (deutsche Ortszeit), Menge, Kurs bzw. Anlagebetrag und die Kosten. Sie beweist nicht, dass die
Order ausgeführt wurde, und der Kurs ist der Kurs zur Eingabe, nicht der Ausführungskurs. Deshalb gilt:

* Es entsteht **nie** eine Buchung. Die Kosteninformation wird einer **vorhandenen** Buchung zugeordnet (Regeln Z0–Z6,
  siehe ``enrich``) und als Beleg mit der Rolle *Kosteninformation* verknüpft.
* Die Gebühr daraus ist eine **Schätzung** (``fee_buy``/``fee_sell`` mit Status ``geschätzt``): Dienstleistungskosten +
  Fremdwährungskosten der Einstiegs- (Kauf) bzw. Ausstiegsspalte (Verkauf); Produktkosten sind der Spread und **keine**
  Gebühr. Ein Vorschlag entsteht nur über die Ergänzen-Vorschau mit Bestätigung und nur, wenn keine Abrechnung die
  Gebühr belegt (R1/Z6).
* ``Kauf von 50,00 € (zu 50,00 €)`` ist die Einrichtung/Änderung eines Sparplans, keine Order (R6) – nur Information.
* Rechenprobe (R8): Menge × Kurs ≈ Anlagebetrag (1 %), sonst „ungeklärt“. Ohne Kurs gilt der Anlagebetrag als Kurswert.

Layouts: *neu* (ab 2023, ``Menge (Kurs)  Kauf von … Stück (zu … €)``, drei Kostenspalten Einstieg/Laufend/Ausstieg bei
Käufen, eine Ausstiegsspalte bei Verkäufen) und *Legacy* (``Datum/Zeit:``, ohne Richtung, Kosten in Blöcken
Einstieg/Laufend/Ausstieg).

Order-Belege (``order_doctx``) erzeugen nur Information: Auftragsnummer, Menge, Limit, bei der Ausführungsanzeige
Ausführungskurs und Schlusszeit – damit der Bericht „ausgeführt / vorgemerkt / gestrichen“ je Auftragsnummer zeigen und
die Belege an die Buchung hängen kann.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal

from app.documentimport.dkb import Classified
from app.documentimport.dkb_trade import _GLUE, AMT, _date, de
from app.documentimport.extract import DocumentResult, Line
from app.documentimport.profiles import DocTx, _derived, _ev, isin_ok

CENT = Decimal("0.01")
_ISIN = r"[A-Z]{2}[A-Z0-9]{9}\d"
_DAY = r"(?P<d>\d{2}\.\d{2}\.\d{4})"
_NEW_WHEN = re.compile(rf"^Wertpapierbezeichnung\s+(?P<name>.+?)\s+{_DAY},\s+(?P<t>\d{{2}}:\d{{2}}:\d{{2}})\s+Uhr")
_NEW_ID = re.compile(rf"^ISIN/WKN\s+(?P<isin>{_ISIN})/(?P<wkn>[A-Z0-9]{{6}})")
_NEW_QTY = re.compile(rf"^Menge \(Kurs\)\s+(?P<side>Kauf|Verkauf) von (?P<q>{AMT})\s+(?P<u>Stück|€|EUR)\s+"
                      rf"\(zu (?P<p>{AMT})\s+(?P<c>€|[A-Z]{{3}})\)")
_AMOUNT = re.compile(rf"(?:^|\s)Anlagebetrag\s+(?P<a>{AMT})\s*(?:€|EUR)")
_PLANNED = re.compile(rf"für Ihre geplante Anlage von (?P<a>{AMT})\s*EUR")
_LEG_WHEN = re.compile(rf"Datum/Zeit:\s*{_DAY}\s+(?P<t>\d{{2}}:\d{{2}}:\d{{2}})")
_LEG_POS = re.compile(rf"^Stück\s+(?P<q>{AMT})\s+(?P<name>.+?)\s+(?P<isin>{_ISIN})\s+(?P<wkn>[A-Z0-9]{{6}})\s*$")
_LEG_PRICE = re.compile(rf"(?:^|\s)Kurs\s+(?P<p>{AMT})\s+EUR")
_COST = re.compile(rf"(?:^|\s)(?P<l>Dienstleistungskosten\*?|Fremdwährungskosten)\s+"
                   rf"(?P<rest>(?:{AMT}\s*€\s+{AMT}\s*%\s*)+)$")
_PAIR = re.compile(rf"({AMT})\s*€\s+{AMT}\s*%")
_SECTION = re.compile(r"(Einstiegskosten \(einmalig\)|Laufende Kosten|Ausstiegskosten \(einmalig\))")
_END_OF_TABLE = ("Erläuterungen zur Kostendarstellung", "2 Gesamtkosten", "Gesamtkosten für eine Haltedauer")


@dataclass
class CostInfo:
    layout: str = ""  # neu | legacy
    name: str = ""
    isin: str = ""
    wkn: str = ""
    day: date | None = None
    at: time | None = None
    when_line: Line | None = None
    side: str = ""  # buy | sell | "" (Legacy)
    qty: Decimal | None = None
    price: Decimal | None = None
    price_ccy: str = ""
    amount: Decimal | None = None
    plan: bool = False
    fee_in: Decimal | None = None
    fee_out: Decimal | None = None
    cost_line: Line | None = None
    problems: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)


def _clock(text: str) -> time:
    h, mi, se = (int(x) for x in text.split(":"))
    return time(h, mi, se)


def parse_costinfo(doc: DocumentResult, cl: Classified) -> CostInfo:
    c = CostInfo()
    section = ""
    in_fx = out_fx = Decimal(0)
    in_dl = out_dl = None
    first_table = True
    full = " ".join(ln.text for ln in doc.lines())
    for ln in (x for x in doc.lines() if len(x.text.strip()) > 3):
        text = ln.text.strip()
        if any(k in text for k in _END_OF_TABLE):
            first_table = False
        for s in [text] if not _GLUE.match(text) else [text, _GLUE.sub("", text, count=1)]:
            if (m := _NEW_WHEN.match(s)):
                c.layout, c.name, c.day, c.when_line, c.at = "neu", m["name"].strip(), _date(m["d"]), ln, _clock(m["t"])
            elif (m := _NEW_ID.match(s)):
                c.isin, c.wkn = m["isin"], m["wkn"]
            elif (m := _NEW_QTY.match(s)):
                c.side = "buy" if m["side"] == "Kauf" else "sell"
                if m["u"] == "Stück":
                    c.qty = de(m["q"])
                    c.price, c.price_ccy = de(m["p"]), "EUR" if m["c"] == "€" else m["c"]
                else:
                    c.plan = True  # „Kauf von 50,00 € (zu 50,00 €)“: Sparrate, keine Order
            elif (m := _LEG_WHEN.search(s)):
                c.layout, c.day, c.when_line, c.at = "legacy", _date(m["d"]), ln, _clock(m["t"])
            elif (m := _LEG_POS.match(s)) and c.layout == "legacy" and not c.isin:
                c.qty, c.name, c.isin, c.wkn = de(m["q"]), m["name"].strip(), m["isin"], m["wkn"]
            elif (m := _LEG_PRICE.search(s)) and c.layout == "legacy" and c.price is None:
                c.price, c.price_ccy = de(m["p"]), "EUR"
            elif ((m := _AMOUNT.search(s)) or (m := _PLANNED.search(s))) and c.amount is None:
                c.amount = de(m["a"])
            elif (m := _SECTION.search(s)) and c.layout == "legacy":
                section = {"Einstiegskosten (einmalig)": "in", "Laufende Kosten": "run"}.get(m[1], "out")
            elif (m := _COST.search(s)):
                pairs = [de(p) for p in _PAIR.findall(m["rest"])]
                label = m["l"].rstrip("*")
                if c.layout == "neu" and first_table:
                    if len(pairs) >= 3:
                        vin, vout = pairs[0], pairs[2]
                    elif len(pairs) == 1 and "Einstiegskosten" not in full:
                        vin, vout = None, pairs[0]  # Verkauf: nur Ausstiegsspalte
                    else:
                        continue
                    if label == "Dienstleistungskosten" and in_dl is None and out_dl is None:
                        in_dl, out_dl, c.cost_line = vin, vout, ln
                    elif label == "Fremdwährungskosten" and not (in_fx or out_fx):
                        in_fx, out_fx = vin or Decimal(0), vout or Decimal(0)
                elif c.layout == "legacy" and section in ("in", "out"):
                    if label == "Dienstleistungskosten":
                        if section == "in" and in_dl is None:
                            in_dl, c.cost_line = pairs[0], c.cost_line or ln
                        elif section == "out" and out_dl is None:
                            out_dl, c.cost_line = pairs[0], c.cost_line or ln
                    elif section == "in":
                        in_fx = in_fx or pairs[0]
                    else:
                        out_fx = out_fx or pairs[0]
            else:
                continue
            break
    if in_dl is not None:
        c.fee_in = in_dl + in_fx
    if out_dl is not None:
        c.fee_out = out_dl + out_fx
    _prove(c)
    return c


def _prove(c: CostInfo) -> None:
    p = c.problems
    if c.layout == "":
        p.append("Rechenprobe: Layout der Kosteninformation nicht erkannt")
        return
    if not c.isin:
        p.append("Rechenprobe: ISIN/WKN nicht lesbar")
    elif not isin_ok(c.isin):
        p.append(f"Rechenprobe: ISIN {c.isin} mit ungültiger Prüfziffer")
    if c.day is None or c.at is None:
        p.append("Rechenprobe: Zeitpunkt der Ordereingabe nicht lesbar")
    if c.plan:
        return  # Sparplan-Einrichtung: nur Information
    if c.qty is None and c.amount is None:
        p.append("Rechenprobe: weder Menge noch Anlagebetrag lesbar")
    if c.qty is not None and c.price is not None and c.amount is not None and c.price_ccy == "EUR":
        raw = c.qty * c.price
        if abs(raw - c.amount) > max(CENT, c.amount * Decimal("0.01")):
            p.append(f"Rechenprobe: Menge × Kurs = {raw.quantize(CENT)} ≠ Anlagebetrag {c.amount}")
        else:
            c.checks.append(f"{c.qty} × {c.price} = {raw.quantize(CENT)} ≈ Anlagebetrag {c.amount}")
    elif c.price is None and c.amount is None and c.qty is not None:
        c.hints.append("Weder Kurs noch Anlagebetrag im Beleg – Menge und Zeitpunkt sind trotzdem belegt")
    if c.fee_in is None and c.fee_out is None:
        c.hints.append("Dienstleistungskosten nicht lesbar – keine Gebührenschätzung")


def to_doctx(c: CostInfo, doc: DocumentResult, n: int = 1) -> DocTx:
    sha = doc.sha256
    kind = "unknown" if c.problems else ("info" if c.plan else "estimate")
    tx = DocTx(n, kind, "kosteninformation", "dkb")

    def add(fld: str, value: object, ln: Line | None, reason: str = "", status: str = "belegt") -> None:
        tx.evidence.append(_derived(fld, value, sha, reason or "abgeleitet", status=status) if ln is None else
                           _ev(fld, value, ln, sha, reason=reason, status=status))

    if c.isin:
        add("isin", c.isin, c.when_line)
        add("wkn", c.wkn, c.when_line)
        if c.name:
            add("asset", c.name, c.when_line)
    if c.qty is not None:
        add("quantity", c.qty, c.when_line, "Menge der Ordereingabe laut Kosteninformation")
    if c.day is not None:
        add("date", c.day.isoformat(), c.when_line, "Tag der Ordereingabe laut Kosteninformation")
        if c.at is not None:
            add("time", c.at.isoformat(), c.when_line, "Zeitpunkt der Ordereingabe (deutsche Ortszeit)")
            add("tz", "Europe/Berlin", None, "Ortszeit der Bank")
    if c.side:
        add("side", c.side, c.when_line, "Richtung laut „Kauf von/Verkauf von“")
    if c.price is not None:
        add("price", c.price, c.when_line, "Kurs zur Ordereingabe (nicht der Ausführungskurs)", "geschaetzt")
        add("price_ccy", c.price_ccy, c.when_line)
    if c.amount is not None:
        add("value_eur", c.amount, c.when_line, "Anlagebetrag laut Kosteninformation (nicht der Kurswert der "
            "Ausführung)", "geschaetzt")
    for fld, val in (("fee_buy", c.fee_in), ("fee_sell", c.fee_out)):
        if val is not None and (not c.side or (c.side == "buy") == (fld == "fee_buy")):
            add(fld, val, c.cost_line, "Dienstleistungs- + Fremdwährungskosten laut Kosteninformation (Schätzung)",
                "geschaetzt")
    stamp = f"{c.day.isoformat() if c.day else ''}T{c.at.isoformat() if c.at else ''}"
    add("ext_id", f"Kosteninfo:{c.isin}:{stamp}", None, "ISIN, Tag und Uhrzeit der Ordereingabe")
    if c.plan:
        rate = f", hochgerechneter Anlagebetrag {c.amount} €" if c.amount else ""
        add("info", f"Sparplan-Einrichtung/-Änderung (Sparrate{rate}) – keine Order", None, "Auswertung des Belegs")
    tx.warnings.extend(c.problems)
    tx.warnings.extend(c.hints)
    tx.resolve()
    return tx


def dkb_costinfo_doctx(doc: DocumentResult, lines: list[str], filename: str = "") -> DocTx | None:
    from app.documentimport.dkb import classify

    cl = classify(lines, filename)
    if cl is None or cl.kind != "kosteninfo_order" or not cl.depot:
        return None
    return to_doctx(parse_costinfo(doc, cl), doc)


# -- Order-Belege -------------------------------------------------------------------------------------------------

_ORD_STUECK = re.compile(rf"^Stück\s+(?P<q>\d[\d.]*(?:,\d+)?)\s+(?P<name>.+?)\s+(?P<isin>{_ISIN})\s*"
                         rf"\((?P<wkn>[A-Z0-9]{{6}})\)")
_ORD_PRICE = re.compile(rf"^(?P<k>Ausführungskurs|Limit|Abrech\.?-?\s?Preis)\s+(?P<p>{AMT})\s+(?P<c>[A-Z]{{3}})")
_ORD_CLOSE = re.compile(r"Schlusstag/-Zeit\s+(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2}:\d{2})")
ORDER_TEXT = {"ausfuehrungsanzeige": "Ausführungsanzeige: Order ausgeführt, die Abrechnung folgt",
              "auftragsbestaetigung": "Auftragsbestätigung: Order vorgemerkt (Ausführung nicht belegt)",
              "streichung": "Streichungsbestätigung: Order gestrichen (keine Ausführung, keine Buchung)"}


def order_doctx(doc: DocumentResult, lines: list[str], filename: str = "") -> DocTx | None:
    """Order-Beleg → Information (nie eine Buchung); die Auftragsnummer verbindet ihn mit der Abrechnung."""
    from app.documentimport.dkb import classify

    cl = classify(lines, filename)
    if cl is None or cl.kind not in ORDER_TEXT or not cl.depot:
        return None
    sha = doc.sha256
    tx = DocTx(1, "info", "order", "dkb")

    def add(fld: str, value: object, ln: Line | None, reason: str = "") -> None:
        tx.evidence.append(_derived(fld, value, sha, reason or "abgeleitet") if ln is None else
                           _ev(fld, value, ln, sha, reason=reason))

    price = limit = close = None
    seen_isin = False
    for ln in (x for x in doc.lines() if len(x.text.strip()) > 3):
        s = ln.text.strip()
        if (m := _ORD_STUECK.match(s)) and not seen_isin:
            seen_isin = True
            add("isin", m["isin"], ln)
            add("wkn", m["wkn"], ln)
            add("asset", m["name"].strip(), ln)
            add("quantity", de(m["q"]), ln)
        elif (m := _ORD_PRICE.match(s)):
            if m["k"] == "Limit" and limit is None:
                limit = (de(m["p"]), m["c"])
            elif m["k"] != "Limit" and price is None:
                price = (de(m["p"]), m["c"])
                add("price", price[0], ln, "Ausführungskurs laut Order-Beleg")
                add("price_ccy", price[1], ln)
        elif (m := _ORD_CLOSE.search(s)) and close is None:
            close = (m[1], m[2])
    info = ORDER_TEXT[cl.kind]
    if close:
        info += f" ({close[0]} {close[1]}" + (f", Kurs {price[0]} {price[1]})" if price else ")")
    elif limit:
        info += f" (Limit {limit[0]} {limit[1]})"
    add("info", info, None, "Auswertung des Belegs")
    day = cl.doc_date or (_date(close[0]).isoformat() if close and _date(close[0]) else "")
    if day:
        add("date", day, None, "Belegdatum")
    if cl.order_no:
        add("ext_id", f"Order:{cl.order_no}:{cl.kind}", None, "Auftragsnummer und Belegart")
        add("order_no", cl.order_no, None, "Auftragsnummer laut Beleg")
    tx.resolve()
    return tx


__all__ = ["CostInfo", "dkb_costinfo_doctx", "order_doctx", "parse_costinfo", "to_doctx"]
