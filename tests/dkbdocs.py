"""Anonymisierte Nachbauten der DKB-Wertpapierdokumente (Layout und Beschriftung wie im Original, alle Namen,
Nummern, Beträge und Konten erfunden). Zwei Leerzeichen trennen Spalten, wie ``tests.docfixtures.text_pdf`` sie setzt.

Grundlage ist die Struktur der echten Dokumente; Originale gehören nicht ins Repository."""

from __future__ import annotations

from decimal import Decimal

from tests.docfixtures import text_pdf

DEPOT = "500012345"
HEAD = ["10000 Musterstadt  Seite 1 von 1", f"Depotnummer  {DEPOT}", "Kundennummer  11112222", "Erika Mustermann"]
ADDR = ["Frau", "Erika Mustermann", "Beispielweg 1", "12345 Beispielstadt"]
NOISE = ["R", "O9", "22", "00", "0."]  # gedrehter Randtext der Originale
FOOT = ["Dieses Dokument wurde maschinell erstellt und wird nicht unterschrieben."]


def _kopf(extra: list[str]) -> list[str]:
    return [*HEAD, *extra, *ADDR]


def de(v: Decimal) -> str:
    """Betrag im deutschen Format (``1.234,56``)."""
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def abrechnung(side: str = "Kauf", order: str = "123456/78.00", day: str = "06.08.2024", wkn: str = "A40EDP",
               isin: str = "US88023B1035", name: str = "BEISPIEL AI INC.", festpreis: bool = False,
               qty: str = "10", price: str = "32,82 USD", fx: str | None = "1,0887", kurswert: str = "301,46",
               fees: tuple[tuple[str, str], ...] = (("Provision", "10,00"), ("Fremde Auslagen", "6,43"),
                                                    ("Eigene Spesen", "20,00")),
               taxes: tuple[tuple[str, str, str, str], ...] = (), refund: tuple[tuple[str, str, str, str], ...] = (),
               close: str | None = "05.08.2024 15:30:00", total: str | None = None, glue: bool = True) -> list[str]:
    """Kauf-/Verkaufsabrechnung wie bei der DKB: Vorzeichen als Suffix, Summen konsistent berechnet. ``taxes`` und
    ``refund`` sind (Bezeichnung, Satz, Grundlage, Betrag); ``total`` überschreibt den Ausmachenden Betrag
    (Negativtest)."""
    n = lambda t: Decimal(t.replace(".", "").replace(",", "."))  # noqa: E731
    kw, fee_sum = n(kurswert), sum((n(a) for _l, a in fees), Decimal(0))
    tax_sum = sum((n(a) for _l, _r, _b, a in taxes), Decimal(0))
    buy = side == "Kauf"
    titel = f"Wertpapier Abrechnung {side}" + ("-Festpreisgeschäft" if festpreis else "")
    sign = "-" if buy else ""
    lines = [*_kopf([f"Auftragsnummer  {order}", f"Datum  {day}", "Rechnungsnummer  W00000-0000000001/24"]), titel,
             "Nominale  Wertpapierbezeichnung  ISIN  (WKN)", f"Stück {qty}  {name}  {isin}  ({wkn})",
             "REG.SHS A DL -,01", "Handels-/Ausführungsplatz  NY Nasdaq (SM) (gemäß Weisung)"]
    if close:
        lines.append(f"Schlusstag/-Zeit {close}  Auftraggeber  Erika Mustermann")
    lines.append(f"{'Abrech.-Preis' if festpreis else 'Ausführungskurs'}  {price}")
    if fx:
        lines.append(f"Devisenkurs (EUR/{price.split()[-1]}) {fx} vom {day}")
    lines.append(f"Kurswert  {de(kw)}{sign} EUR")
    lines += [f"{label}  {amount}- EUR" for label, amount in fees]
    if taxes:
        lines += ["Ermittlung steuerrelevante Erträge", "Veräußerungsgewinn  9.230,50 EUR",
                  "Bemessungsgrundlage für die Kapitalertragsteuer  9.230,50 EUR",
                  "Berechnungsgrundlage für die Kapitalertragsteuer  8.765,71 EUR", "Steuerberechnung"]
        lines += [f"{label} {rate} % auf {base} EUR  {amount}- EUR" for label, rate, base, amount in taxes]
    cash = kw + fee_sum if buy else kw - fee_sum - tax_sum
    glue_prefix = "70  " if glue else ""
    lines.append(f"{glue_prefix}Ausmachender Betrag  {total or de(cash)}{sign} EUR")
    lines.append(f"Den Gegenwert buchen wir mit Valuta {day} zu {'Lasten' if buy else 'Gunsten'} des Kontos 12345678 "
                 "(IBAN DE00 0000 0000 0000 0000 00), BLZ 12030000 (BIC BYLADEM1001).")
    if refund:
        lines += ["Seite 2", f"Auftragsnummer  {order}", "Steuerliche Ausgleichrechnung",
                  "Berechnungsgrundlage für die Kapitalertragsteuer  150,33  EUR"]
        lines += [f"{label} {rate} % auf {base} EUR  {amount}  EUR" for label, rate, base, amount in refund]
        lines.append(f"Ausmachender Betrag  {de(sum((n(a) for *_x, a in refund), Decimal(0)))}  EUR")
    lines += ["Nachrichtlich die Übersicht Ihrer Verrechnungs- und Steuertopfsalden zum Zeitpunkt der Erstellung.",
              "Kapitalertragsteuer 25,00 % auf 1,00 EUR  0,25- EUR", *NOISE, *FOOT]
    return lines


def ausfuehrungsanzeige() -> list[str]:
    return [*_kopf(["Auftragsnummer  123456/78.00"]), "Wertpapier Ausführungsanzeige Kauf",
            "Nominale  Wertpapierbezeichnung  ISIN  (WKN)", "Stück 10  BEISPIEL AI INC.  US88023B1035  (A40EDP)",
            "Ausführungskurs 32,82 USD", "Ihr Auftrag wurde zum oben genannten Kurs ausgeführt. Die rechtsverbindliche "
            "Abrechnung folgt.", *FOOT]


def auftragsbestaetigung() -> list[str]:
    return [*_kopf(["Auftragsnummer  265253/44.00"]), "Wertpapier Auftragsbestätigung Kauf", "gültig ab 02.01.2026",
            "Nominale  Wertpapierbezeichnung  ISIN  (WKN)", "Stück 10  BEISPIEL AI INC.  US88023B1035  (A40EDP)",
            "Wir haben den Auftrag wie vorstehend angegeben in Vormerkung genommen.", *FOOT]


def streichung() -> list[str]:
    return [*_kopf(["Auftragsnummer  527062/75.00"]), "Wertpapier Streichungsbestätigung Verkauf",
            "Nominale  Wertpapierbezeichnung  ISIN  (WKN)", "Stück 4  BEISPIEL MOTORS INC.  US13803R3003  (A40XUX)",
            "Vorstehenden Auftrag haben wir vorbehaltlich einer zwischenzeitlich erfolgten Orderausführung gestrichen.",
            *FOOT]


def ertrag(titel: str = "Dividendengutschrift", unter: str = "", abr: str = "57764418230",
           day: str = "18.03.2025") -> list[str]:
    kopf = [*HEAD, f"Datum  {day}", *ADDR, titel, *([unter] if unter else [])]
    return [*kopf, "Nominale  Wertpapierbezeichnung  ISIN  (WKN)", "Stück 20  BEISPIEL HOLDING INC.  US02079K3059  "
            "(A14Y6F)", "Zahlbarkeitstag  18.03.2025  Dividenden-Betrag pro Stück  0,20 USD",
            "Ausmachender Betrag  2,73+ EUR", "Seite 2", f"Abrechnungsnr.  {abr}", *NOISE, *FOOT]


def kapital(untertitel: list[str], beleg: str = "61432975", day: str = "17.02.2025",
            wkn: str = "A3CQRG", isin: str = "US67421J1088") -> list[str]:
    return [*HEAD[:3], f"Belegnummer  {beleg}", "Frau  Datum  " + day,
            "Erika Mustermann  Kapitalmaßnahme  KSPLR00190012000",
            "Beispielweg 1", "12345 Beispielstadt", *untertitel, "Nominale  Wertpapierbezeichnung  ISIN  (WKN)",
            f"Stück 100  BEISPIEL GROUP AB  {isin}  ({wkn})", "Sehr geehrte Frau Mustermann,", *FOOT]


def depotuebertrag() -> list[str]:
    return [*_kopf(["Auftragsnummer  792595/75.00", "Datum  07.01.2025"]), "Ersatzlose Ausbuchung",
            "Nominale  Wertpapierbezeichnung  ISIN  (WKN)", "Stück 50  BEISPIEL S.A.  FR0013018041  (A2JQ3R)",
            "Schlusstag  07.01.2025  Auftrags-Nr.  792595/75.00", "Veräußerungsverlust  238,95- EUR", *FOOT]


def depotauszug() -> list[str]:
    return ["Datum  01.07.2024", f"Depotnummer  {DEPOT}", "Kundendepot", "Erika Mustermann",
            "Aufstellung über Kundenfinanzinstrumente per 30.06.2024",
            "Nominale  Wertpapierinformationen  Kurs in Währung pro Stück / %  Kurswert in EUR",
            "Stück  50  BEISPIEL S.A.  0,0001 EUR  0,01", "FR0013018041 (A2JQ3R)  24.12.2024", *FOOT]


def kosteninfo_neu(side: str = "Kauf") -> list[str]:
    return ["Ex-Ante Kosteninformation", "Ergebnisdarstellung",
            "Wertpapierbezeichnung BEISPIEL TURBO 97Y  08.10.2024, 15:02:25 Uhr", "ISIN/WKN  DE000LX4QJM1/LX4QJM",
            "Ausführungsplatz  Best-Execution  Erika Mustermann",
            f"Menge (Kurs)  {side} von 1.000,00 Stück (zu 0,57 €)  Kundennummer:  11112222",
            f"Anlagebetrag  570,00 €  Depotnummer:  {DEPOT}", "1 Aufstellung der Kostenpositionen"]


def kosteninfo_legacy() -> list[str]:
    return [f"Frau  Depotnummer: {DEPOT}", "Erika Mustermann  Kundennummer: 11112222", "Beispielweg 1",
            "Datum/Zeit: 17.11.2021 09:00:45", "12345 Beispielstadt", "Ex-Ante-Kostensimulation - Ergebnisdarstellung",
            "Stück  Wertpapierbezeichnung  ISIN  WKN",
            "Stück 100,00  BEISPIEL TECH INC. REGISTERED  US69608A1088  A2QA4J",
            "Ausführungsplatz  Best-Execution  Anlagebetrag  2.054,50 EUR"]


def kosteninfo_jahr() -> list[str]:
    return ["10000 Musterstadt  Seite 1 von 3", "Datum  im April 2025", f"Depotnummer  {DEPOT}", "Erika Mustermann",
            "Frau", "Erika Mustermann", "Beispielweg 1", "12345 Beispielstadt",
            "Information über Kosten und Nebenkosten für depotverwahrte Finanzinstrumente für", "das Jahr 2024"]


def hauptversammlung() -> list[str]:
    return [*HEAD[:3], "Belegnummer  10110420", "Frau  Datum  14.02.2025", "Erika Mustermann", "Beispielweg 1",
            "12345 Beispielstadt", "Terminsache: Eingang bis spätestens 25.02.2025, 10:00 Uhr",
            "Einladung zur außerordentl. Hauptversammlung", "Nominale  Wertpapierbezeichnung  ISIN  (WKN)",
            "Stück 100  BEISPIEL GROUP AB  US67421J1088  (A3CQRG)"]


def verlustschwelle() -> list[str]:
    return [f"Depotnummer  {DEPOT}", "Kundennummer  11112222", "Mustermann", "Datum  08.10.2024",
            "Verlustschwellenreport gem. Art. 62 Abs. 2 MiFID II DVO 2017/565",
            "Nominale  Wertpapierbezeichnung  ISIN  (WKN)", "1.000,0000  BEISPIEL TURBO 97Y  DE000LX4QJM1 (LX4QJM)"]


def kontoauszug() -> list[str]:
    return ["Deutsche Kreditbank AG", "Kontoauszug 01/2025", "IBAN DE00 1234 5678 9012 3456 78", "Saldo 1.000,00 EUR"]


# Name → (Zeilen, erwartet: Art, Unterart, Richtung, Datum, Kennung)
GOLDEN: dict[str, tuple[list[str], tuple[str, str, str, str, str]]] = {
    "kauf": (abrechnung("Kauf"), ("abrechnung_kauf", "", "buy", "2024-08-06", "12345678" + "00")),
    "verkauf": (abrechnung("Verkauf", "654321/09.00", "14.10.2024"),
                ("abrechnung_verkauf", "", "sell", "2024-10-14", "6543210900")),
    "festpreis": (abrechnung("Verkauf", "111111/11.00", "10.01.2025", festpreis=True),
                  ("abrechnung_verkauf", "", "sell", "2025-01-10", "1111111100")),
    "ausfuehrung": (ausfuehrungsanzeige(), ("ausfuehrungsanzeige", "", "buy", "", "1234567800")),
    "bestaetigung": (auftragsbestaetigung(), ("auftragsbestaetigung", "", "buy", "", "2652534400")),
    "streichung": (streichung(), ("streichung", "", "sell", "", "5270627500")),
    "dividende": (ertrag(), ("ertrag", "dividende", "", "2025-03-18", "57764418230")),
    "rueckzahlung": (ertrag("Gutschrift", "Kapitalrückzahlung", "57858717490", "19.03.2025"),
                     ("ertrag", "kapitalrueckzahlung", "", "2025-03-19", "57858717490")),
    "liquidation_ertrag": (ertrag("Gutschrift", "Liquidationszahlung", "79363215100", "20.10.2025"),
                           ("ertrag", "liquidation", "", "2025-10-20", "79363215100")),
    "reverse_split": (kapital(["Zusammenlegung von Titeln (Reverse Split)"]),
                      ("kapitalmassnahme", "reverse_split", "", "2025-02-17", "61432975")),
    "storno": (kapital(["Storno", "Gratisausgabe"], "61452937", "27.10.2025"),
               ("kapitalmassnahme", "storno", "", "2025-10-27", "61452937")),
    "neubuchung": (kapital(["Gratisausgabe - Neubuchung nach Storno"], "61454139", "27.10.2025"),
                   ("kapitalmassnahme", "neubuchung", "", "2025-10-27", "61454139")),
    "gratis": (kapital(["Gratisausgabe"], "51877627", "30.09.2025"),
               ("kapitalmassnahme", "gratis", "", "2025-09-30", "51877627")),
    "entgelt": (kapital(["Entgeltbelastung in Folge einer Kapitalmaßnahme"], "40014500", "25.02.2025"),
                ("kapitalmassnahme", "entgelt", "", "2025-02-25", "40014500")),
    "umbenennung": (kapital(["Änderung der Emittentenbezeichnung"], "51818538", "04.03.2025"),
                    ("kapitalmassnahme", "umbenennung", "", "2025-03-04", "51818538")),
    "wertlos": (kapital(["Wertlose Titel - Ausbuchungsanzeige"], "61454421", "31.10.2025"),
                ("kapitalmassnahme", "wertlos", "", "2025-10-31", "61454421")),
    "ausbuchung": (depotuebertrag(), ("depotuebertrag", "ersatzlose_ausbuchung", "", "2025-01-07", "7925957500")),
    "depotauszug": (depotauszug(), ("depotauszug", "", "", "2024-07-01", "2345-20240701")),
    "kosten_neu": (kosteninfo_neu(), ("kosteninfo_order", "", "buy", "2024-10-08", "LX4QJM-150225")),
    "kosten_verkauf": (kosteninfo_neu("Verkauf"), ("kosteninfo_order", "", "sell", "2024-10-08", "LX4QJM-150225")),
    "kosten_legacy": (kosteninfo_legacy(), ("kosteninfo_order", "", "", "2021-11-17", "A2QA4J-090045")),
    "kosten_jahr": (kosteninfo_jahr(), ("kosteninfo_jahr", "", "", "", "")),
    "hauptversammlung": (hauptversammlung(), ("hauptversammlung", "", "", "2025-02-14", "10110420")),
    "verlustschwelle": (verlustschwelle(), ("verlustschwelle", "", "", "2024-10-08", "")),
}


def pdf_of(lines: list[str]) -> bytes:
    return text_pdf(lines)
