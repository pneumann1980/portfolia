"""DKB-Wertpapierdokumente: Dokumentart und Kennungen aus dem **Inhalt** (nie aus dem Dateinamen).

Dieses Profil ist der erste Schritt der DKB-Auswertung (Phase 1: „Was ist drin?“). Es liest nur Titel und Kennungen
– Beträge, Rechenprobe und Buchungsvorschläge folgen mit den jeweiligen Dokumentarten. Grundsätze:

* **Der Inhalt entscheidet.** Der Dateiname liefert höchstens einen Hinweis; widerspricht er dem Inhalt, wird das
  vermerkt (z. B. „Depot_bertrag“ ist eine ersatzlose Ausbuchung, „(1)“ ist keine Dublette, solange der Inhalt
  abweicht).
* **Unbekannt bleibt unbekannt.** Ein Titel, den keine Regel eindeutig trifft, ergibt ``None`` – das Dokument wird
  gezählt und aufgelistet, nie geraten.
* **Kennungen** (Auftragsnummer nur Ziffern, Belegnummer, Abrechnungsnummer, Depot, Datum, ISIN/WKN) sind die
  Grundlage für Ablagename, Abgleich mit vorhandenen Buchungen und Wiederaufnahme.
* **Keine Personendaten im Ergebnis.** Name, Anschrift, Kunden- und Kontonummer werden nicht übernommen.

Eingabe sind die Textzeilen der vorhandenen Extraktion (:mod:`app.documentimport.extract`).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from app.documentimport.profiles import isin_ok

# Schlüssel → (Anzeige, Gruppe). Gruppe: abrechnung | order | ertrag | kapital | depot | kosten | info
KINDS: dict[str, tuple[str, str]] = {
    "abrechnung_kauf": ("Abrechnung Kauf", "abrechnung"),
    "abrechnung_verkauf": ("Abrechnung Verkauf", "abrechnung"),
    "ertrag": ("Ertragsabrechnung", "ertrag"),
    "kapitalmassnahme": ("Kapitalmaßnahme", "kapital"),
    "depotuebertrag": ("Depotübertrag / ersatzlose Ausbuchung", "kapital"),
    "depotauszug": ("Depotauszug", "depot"),
    "ausfuehrungsanzeige": ("Ausführungsanzeige", "order"),
    "auftragsbestaetigung": ("Auftragsbestätigung", "order"),
    "streichung": ("Streichungsbestätigung", "order"),
    "kosteninfo_order": ("Kosteninformation zur Order", "kosten"),
    "kosteninfo_jahr": ("Kosteninformation Jahr", "kosten"),
    "hauptversammlung": ("Hauptversammlung", "info"),
    "verlustschwelle": ("Verlustschwellenreport", "info"),
}
GROUP_LABEL = {"abrechnung": "Abrechnungen", "ertrag": "Erträge", "kapital": "Kapitalmaßnahmen und Ausbuchungen",
               "depot": "Depotauszüge", "order": "Order-Belege", "kosten": "Kosteninformationen",
               "info": "Nur Information"}

# Unterarten der Kapitalmaßnahmen: (Schlüssel, Muster auf die Titelzeilen)
_CA_SUB: list[tuple[str, re.Pattern[str]]] = [
    ("neubuchung", re.compile(r"Neubuchung nach Storno", re.I)),
    ("storno", re.compile(r"\bStorno\b", re.I)),
    ("entgelt", re.compile(r"Entgeltbelastung", re.I)),
    ("reverse_split", re.compile(r"Zusammenlegung|Reverse\s*Split", re.I)),
    ("split", re.compile(r"Aktiensplit|Splitting|\bSplit\b", re.I)),
    ("gratis", re.compile(r"Gratisaktien|Gratisausgabe|Bonus", re.I)),
    ("umbenennung", re.compile(r"Emittentenbezeichnung|Namens[äa]nderung|Umfirmierung", re.I)),
    ("wertlos", re.compile(r"Wertlose\s+Titel|Ausbuchungsanzeige", re.I)),
    ("liquidation", re.compile(r"Liquidation", re.I)),
    ("optionsfrist", re.compile(r"Optionsfrist|Ablauf der", re.I)),
    ("insolvenz", re.compile(r"Chapter\s+(?:7|VII|11|XI)|Insolvenz", re.I)),
    ("umtausch", re.compile(r"Umtausch|Fusion|Verschmelzung", re.I)),
    ("spinoff", re.compile(r"Abspaltung|Spin-?\s*off", re.I)),
    ("bezugsrecht", re.compile(r"Bezugsrecht", re.I)),
]
_ERTRAG_TITLE = re.compile(r"^(Dividendengutschrift|Ertragsgutschrift|Ausschüttungsgutschrift|Ausschüttung|"
                           r"Zinsgutschrift|Dividende)$", re.I)
_PLZ = re.compile(r"^\d{5}\s+[A-ZÄÖÜ]")
_META_STOP = re.compile(r"^(Nominale|Sehr geehrte|Stück\b|Ihr|Mit freundlichen|Depotnummer|Kundennummer|Belegnummer|"
                        r"Auftragsnummer|Datum)\b", re.I)
_DEPOT = re.compile(r"Depotnummer:?\s*(\d{6,12})")
_DATE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")
_DATE_LABEL = re.compile(r"(?:^|\s)Datum\s+(\d{2}\.\d{2}\.\d{4})\b")
_ORDER = re.compile(r"Auftrags?(?:nummer|-Nr\.?)\s*:?\s*([\d/. ]*\d)")
_BELEG = re.compile(r"Belegnummer\s*:?\s*(\d{4,})")
_BELEG_ALL = _BELEG
_ABR = re.compile(r"Abrechnungsnr\.?\s*:?\s*(\d{4,})")
_ISIN = re.compile(r"\b([A-Z]{2}[A-Z0-9]{9}\d)\b")
_WKN_PAREN = re.compile(r"\(([A-Z0-9]{6})\)")
_WKN_SLASH = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}\d\s*/\s*([A-Z0-9]{6})\b")
_KI_DT = re.compile(r"(\d{2}\.\d{2}\.\d{4}),?\s+(\d{2}:\d{2}:\d{2})")
_KI_SIDE = re.compile(r"\b(Kauf|Verkauf)\s+von\b", re.I)
_FNAME_DATE = re.compile(r"^(?:Archiv|Legacy)_[A-Za-z]+_(\d{4}-\d{2}-\d{2})_")
_FNAME_KIND: list[tuple[str, str]] = [
    ("Abrechnung_Kauf", "abrechnung_kauf"), ("Abrechnung_Verkauf", "abrechnung_verkauf"),
    ("Ertragsabrechnung", "ertrag"), ("Kapitalma", "kapitalmassnahme"), ("DepotTransfer", "depotuebertrag"),
    ("Depot_bertrag", "depotuebertrag"), ("Depotauszug", "depotauszug"), ("Ausf_hrungsanzeige", "ausfuehrungsanzeige"),
    ("Auftragsbest", "auftragsbestaetigung"), ("Streichungsbest", "streichung"),
    ("Kosteninformation_f_r_das_Jahr", "kosteninfo_jahr"), ("Kosteninformation", "kosteninfo_order"),
    ("Hauptversammlung", "hauptversammlung"), ("Verlustschwellen", "verlustschwelle"),
]


@dataclass(frozen=True)
class Classified:
    """Erkannte Dokumentart und Kennungen eines DKB-Wertpapierdokuments (keine Personendaten, keine Beträge)."""

    kind: str
    sub: str = ""  # Unterart (Kapitalmaßnahme, Ertrag, Depotübertrag)
    sub_text: str = ""  # Titelzeile(n) im Wortlaut der Bank
    side: str = ""  # buy | sell (Abrechnung, Order-Belege, Kosteninformation)
    depot: str = ""
    doc_date: str = ""  # ISO
    doc_time: str = ""  # HH:MM:SS (Kosteninformation)
    order_no: str = ""  # nur Ziffern
    belegnr: str = ""
    abrechnungsnr: str = ""
    ref: str = ""  # Bezug auf eine andere Belegnummer (Storno: die stornierte; Entgelt: die Maßnahme)
    isin: str = ""
    wkn: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def label(self) -> str:
        base = KINDS[self.kind][0]
        if self.kind in ("abrechnung_kauf", "abrechnung_verkauf") and "Festpreis" in self.sub_text:
            base += " (Festpreisgeschäft)"
        return base

    @property
    def group(self) -> str:
        return KINDS[self.kind][1]

    @property
    def ident(self) -> str:
        """Kennung für den Ablagenamen: Auftragsnummer, sonst Belegnummer/Abrechnungsnr., sonst WKN (+ Uhrzeit)."""
        if self.order_no:
            return self.order_no
        if self.belegnr:
            return self.belegnr
        if self.abrechnungsnr:
            return self.abrechnungsnr
        if self.kind == "depotauszug" and self.depot and self.doc_date:
            return f"{self.depot_short}-{self.doc_date.replace('-', '')}"
        if self.wkn:
            t = self.doc_time.replace(":", "")
            return f"{self.wkn}-{t}" if t else self.wkn
        return ""

    @property
    def depot_short(self) -> str:
        return self.depot[-4:] if self.depot else ""

    def subject(self) -> str:
        """Kurzbeschreibung für die Liste (ohne Personendaten)."""
        d = date.fromisoformat(self.doc_date).strftime("%d.%m.%Y") if self.doc_date else ""
        extra = self.sub_text if self.kind == "kapitalmassnahme" and self.sub_text else ""
        return " · ".join(x for x in (self.label, extra, self.wkn or self.isin, d) if x)[:200]


def clean_lines(lines: Sequence[str]) -> list[str]:
    """Textzeilen ohne Rauschen: gedrehter Randtext der DKB-PDFs (Rechnungs-IDs senkrecht am Rand) liefert
    Einzelzeilen mit höchstens drei Zeichen."""
    out: list[str] = []
    for ln in lines:
        s = ln.strip()  # Spaltenabstand (zwei Leerzeichen) bleibt erhalten
        if len(s) > 3:
            out.append(s)
    return out


def _iso(d: str) -> str:
    m = _DATE.search(d)
    if not m:
        return ""
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
    except ValueError:
        return ""


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def filename_hint(name: str) -> tuple[str, str]:
    """(Art, Datum ISO) laut Dateiname – nur als Gegenprobe/Rückfall, nie maßgeblich."""
    kind = next((k for frag, k in _FNAME_KIND if frag in name), "")
    m = _FNAME_DATE.match(name)
    return kind, (m.group(1) if m else "")


def _title_lines(cl: list[str]) -> list[str]:
    """Zeilen zwischen Anschrift (zweite PLZ-Zeile) und Fachteil – dort steht die Dokumentüberschrift."""
    plz = [i for i, s in enumerate(cl[:20]) if _PLZ.match(s) and "Seite" not in s]
    start = (plz[-1] + 1) if plz else 0
    out: list[str] = []
    for s in cl[start:start + 8]:
        if _META_STOP.match(s):
            break
        out.append(s)
        if len(out) >= 3:
            break
    return out


def _first(rx: re.Pattern[str], text: str) -> str:
    m = rx.search(text)
    return m.group(1) if m else ""


def classify(lines: Sequence[str], filename: str = "") -> Classified | None:
    """Dokumentart und Kennungen aus den Textzeilen; ``None`` = nicht erkannt (nie raten)."""
    cl = clean_lines(lines)
    if not cl:
        return None
    text = "\n".join(cl)
    low = text.lower()
    notes: list[str] = []
    title = _title_lines(cl)
    title_text = " / ".join(title)

    kind = sub = side = ""
    sub_text = ""
    if "verlustschwellenreport" in low:
        kind = "verlustschwelle"
    elif re.search(r"Information über Kosten und Nebenkosten für depotverwahrte", text):
        kind = "kosteninfo_jahr"
    elif re.search(r"Ex-Ante[- ]Kosten(?:information|simulation)", text, re.I):
        kind = "kosteninfo_order"
    elif re.search(r"Aufstellung über Kundenfinanzinstrumente|Jahresdepotauszug|Depotauszug", text):
        kind = "depotauszug"
    else:
        m = re.search(r"Wertpapier\s+Abrechnung\s+(Kauf|Verkauf)([\w\- ]*)", text)
        a = re.search(r"Wertpapier\s+(Ausführungsanzeige|Auftragsbestätigung|Streichungsbestätigung)\s+(Kauf|Verkauf)",
                      text)
        if m:
            kind = "abrechnung_kauf" if m.group(1) == "Kauf" else "abrechnung_verkauf"
            side = "buy" if m.group(1) == "Kauf" else "sell"
            sub_text = (m.group(1) + m.group(2)).strip()
        elif a:
            kind = {"Ausführungsanzeige": "ausfuehrungsanzeige", "Auftragsbestätigung": "auftragsbestaetigung",
                    "Streichungsbestätigung": "streichung"}[a.group(1)]
            side = "buy" if a.group(2) == "Kauf" else "sell"
        elif re.search(r"Ersatzlose Ausbuchung", text) and _ORDER.search(text):
            kind, sub, sub_text = "depotuebertrag", "ersatzlose_ausbuchung", "Ersatzlose Ausbuchung"
        elif re.search(r"Depotübertrag|Depotüberträge", text):
            kind, sub, sub_text = "depotuebertrag", "", "Depotübertrag"
        elif "hauptversammlung" in " ".join(title).lower() or re.search(r"Einladung zur .*Hauptversammlung", text):
            kind = "hauptversammlung"
        else:
            ertrag = ""
            for i, s in enumerate(title):
                if _ERTRAG_TITLE.match(s):
                    ertrag = s
                    sub_text = s
                    break
                if s.lower() == "gutschrift":
                    ertrag = s
                    sub_text = " ".join(title[i + 1:i + 2]) if i + 1 < len(title) else ""
                    break
            if ertrag:
                kind = "ertrag"
                low_sub = (sub_text or ertrag).lower()
                sub = ("kapitalrueckzahlung" if "kapitalrückzahlung" in low_sub else
                       "liquidation" if "liquidation" in low_sub else
                       "dividende" if "dividend" in low_sub else "")
            elif "kapitalmaßnahme" in low or (_BELEG.search(text) and re.search(r"Nominale\s+Wertpapierbezeichnung",
                                                                                  text)):
                kind = "kapitalmassnahme"
                sub_text = title_text
                sub = next((k for k, rx in _CA_SUB if rx.search(title_text)), "") or \
                    next((k for k, rx in _CA_SUB if rx.search(text[:1500])), "")
                if not sub:
                    notes.append("Unterart der Kapitalmaßnahme nicht erkannt")
    if not kind:
        return None

    depot = _first(_DEPOT, text)
    belegnr = _first(_BELEG, text)
    abr = _first(_ABR, text)
    order = _digits(_first(_ORDER, text))
    isin = wkn = ""
    if kind not in ("depotauszug", "kosteninfo_jahr", "verlustschwelle"):  # mehrere Titel je Dokument
        for m in _ISIN.finditer(text):
            if not isin_ok(m.group(1)):
                continue
            isin = m.group(1)
            tail = text[m.end():m.end() + 20]
            wkn = _first(_WKN_PAREN, tail) or _first(_WKN_SLASH, text[m.start():m.end() + 12]) or \
                _first(re.compile(r"^\s*([A-Z0-9]{6})\b"), tail)
            break
    doc_date = _iso(_first(_DATE_LABEL, text))
    doc_time = ""
    if kind in ("kosteninfo_order", "kosteninfo_jahr"):
        m = _KI_DT.search(text) or re.search(r"Datum/Zeit:\s*(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2}:\d{2})", text)
        if m:
            doc_date, doc_time = _iso(m.group(1)), m.group(2)
        km = _KI_SIDE.search(text)
        if km:
            side = "buy" if km.group(1).lower() == "kauf" else "sell"
    f_kind, f_date = filename_hint(filename) if filename else ("", "")
    if not doc_date and f_date:
        doc_date = f_date
        notes.append("Datum aus dem Dateinamen (im Dokument nicht lesbar)")
    elif doc_date and f_date and doc_date != f_date and kind != "depotauszug":
        notes.append(f"Datum laut Dateiname ({f_date}) weicht ab – Inhalt gilt")
    if f_kind and f_kind != kind and not (f_kind.startswith("abrechnung") and kind == "ertrag"):
        notes.append("Dateiname passt nicht zum Inhalt – Inhalt gilt")
    if kind in ("abrechnung_kauf", "abrechnung_verkauf", "depotuebertrag", "ausfuehrungsanzeige", "streichung",
                "auftragsbestaetigung") and not order:
        notes.append("Auftragsnummer nicht lesbar")
    refs = [r for r in _BELEG_ALL.findall(text) if r != belegnr]
    ref = belegnr if sub == "storno" else (refs[0] if kind == "kapitalmassnahme" and refs else "")
    return Classified(kind=kind, sub=sub, sub_text=sub_text, side=side, depot=depot, doc_date=doc_date,
                      doc_time=doc_time, order_no=order, belegnr=belegnr, abrechnungsnr=abr, ref=ref, isin=isin,
                      wkn=wkn, notes=tuple(notes))
