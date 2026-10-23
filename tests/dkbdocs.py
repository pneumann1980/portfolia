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
               close: str | None = "05.08.2024 15:30:00", total: str | None = None, glue: bool = True,
               gain: str | None = None, tail: tuple[str, ...] = ()) -> list[str]:
    """Kauf-/Verkaufsabrechnung wie bei der DKB: Vorzeichen als Suffix, Summen konsistent berechnet. ``taxes`` und
    ``refund`` sind (Bezeichnung, Satz, Grundlage, Betrag); ``total`` überschreibt den Ausmachenden Betrag
    (Negativtest). ``gain`` (``"654,39"``, Verlust mit ``-``) schreibt das Veräußerungsergebnis auch ohne Steuern,
    ``tail`` (siehe ``steuer_tail``) hängt Töpfe und Anschaffungsgeschäfte hinter „Nachrichtlich“."""
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
    if taxes or gain:
        g = gain or "9.230,50"
        lines += ["Ermittlung steuerrelevante Erträge",
                  f"Veräußerungsverlust  {g[1:]}- EUR" if g.startswith("-") else f"Veräußerungsgewinn  {g} EUR"]
    if taxes:
        lines += ["Bemessungsgrundlage für die Kapitalertragsteuer  9.230,50 EUR",
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
              "Kapitalertragsteuer 25,00 % auf 1,00 EUR  0,25- EUR", *tail, *NOISE, *FOOT]
    return lines


def topf_zeilen(year: str = "2025", before: tuple[str, ...] = ("0,00", "0,00", "2.000,00", "0,00", "0,00"),
                after: tuple[str, ...] | None = None) -> list[str]:
    """Übersicht der Verlustverrechnungstöpfe (Aktien, Sonstige, Sparer-Pauschbetrag, anrechenbare Quellensteuer,
    Berechnungsgrundlage); ``after=None`` lässt die Zeile „Nachher“ weg."""
    lines = [f"Verlustverrechnungstöpfe {year}  Berechnungsgrundlage", "der gezahlten Steuern",
             "Euro  Aktien  Sonstige  Sparer-  anrechenbare  Aktien und Sonstige", "Pauschbetrag  Quellensteuer",
             "Vorher  " + "  ".join(before)]
    if after is not None:
        lines.append("Nachher  " + "  ".join(after))
    return lines


def steuer_tail(year: str = "2025", before: tuple[str, ...] = ("0,00", "0,00", "2.000,00", "0,00", "0,00"),
                after: tuple[str, ...] = ("0,00", "0,00", "2.000,00", "0,00", "0,00"),
                lots: tuple[tuple[str, str, str, str, str, str], ...] = (), total: str | None = None
                ) -> tuple[str, ...]:
    """Seite 2 einer Verkaufsabrechnung: Töpfe und „Berücksichtigte Anschaffungsgeschäfte“. ``lots`` =
    (Auftragsnr., Datum, Stück, AS-Kosten, Erlös, Ergebnis); Vorzeichen als Suffix im Ergebnis."""
    lines = topf_zeilen(year, before, after)
    if lots:
        lines += ["Berücksichtigte Anschaffungsgeschäfte (alle ermittelten Beträge in EUR)",
                  "Geschäft  Auftragsnr. Ausführ.-tag Whr./St. Nennwert/Stück  AS-Kosten  Erlös  ant. Ergebnis"]
        lines += [f"Kauf  {no}  {d} Stück  {q}  {c}-  {p}  {r} (D)" for no, d, q, c, p, r in lots]
        lines.append("Summe aller Erträge nach Differenzmethode und/oder Ersatzbemessungsgrundlage  "
                     + (total or lots[0][5]))
    return tuple(lines)


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


def ertrag(titel: str = "Dividendengutschrift", unter: str = "", abr: str = "97764418230",
           day: str = "18.03.2025") -> list[str]:
    kopf = [*HEAD, f"Datum  {day}", *ADDR, titel, *([unter] if unter else [])]
    return [*kopf, "Nominale  Wertpapierbezeichnung  ISIN  (WKN)", "Stück 20  BEISPIEL HOLDING INC.  US02079K3059  "
            "(A14Y6F)", "Zahlbarkeitstag  18.03.2025  Dividenden-Betrag pro Stück  0,20 USD",
            "Ausmachender Betrag  2,73+ EUR", "Seite 2", f"Abrechnungsnr.  {abr}", *NOISE, *FOOT]


def kapital(untertitel: list[str], beleg: str = "01432975", day: str = "17.02.2025",
            wkn: str = "A3CQRG", isin: str = "US67421J1088") -> list[str]:
    return [*HEAD[:3], f"Belegnummer  {beleg}", "Frau  Datum  " + day,
            "Erika Mustermann  Kapitalmaßnahme  KSPLR40190012000",
            "Beispielweg 1", "12345 Beispielstadt", *untertitel, "Nominale  Wertpapierbezeichnung  ISIN  (WKN)",
            f"Stück 100  BEISPIEL GROUP AB  {isin}  ({wkn})", "Sehr geehrte Frau Mustermann,", *FOOT]


def ertragsabrechnung(sub: str = "dividende", abr: str = "97764418230", day: str = "18.03.2025",
                      pay: str = "17.03.2025", value_day: str | None = None, qty: str = "20", per: str = "0,20",
                      ccy: str = "USD", fx: str = "1,0970", gross_f: str = "4,00", gross: str = "3,65",
                      qst: tuple[str, str] | None = ("15", "0,55"), kest: tuple[str, str, str] | None = None,
                      soli: tuple[str, str, str] | None = None, verrechnet: bool = False,
                      rueckforderbar: bool = False, pots: list[str] | None = None,
                      total: str | None = None, isin: str = "US02079K3059", wkn: str = "A14Y6F",
                      name: str = "BEISPIEL HOLDING INC.", direction: str = "Gunsten") -> list[str]:
    """Ertragsabrechnung wie bei der DKB (Alle Beträge EUR, Vorzeichen als Suffix). ``qst`` = (Satz, Betrag EUR),
    ``kest``/``soli`` = (Satz, Basis, Betrag). Informationszeilen (anrechenbar, rückforderbar, verrechnet) stehen im
    Beleg, gehen aber nie in die Summe ein. ``total`` überschreibt den Ausmachenden Betrag (Negativtest)."""
    n = lambda t: Decimal(t.replace(".", "").replace(",", "."))  # noqa: E731
    cash = n(gross) - (n(qst[1]) if qst else 0) - (n(kest[2]) if kest else 0) - (n(soli[2]) if soli else 0)
    div = sub == "dividende"
    titel = ["Dividendengutschrift"] if div else ["Gutschrift", "Kapitalrückzahlung" if sub == "rueckzahlung"
                                                  else "Liquidationszahlung"]
    lines = [*_kopf([f"Abrechnungsnr.  {abr}", f"Datum  {day}"]), *titel,
             "Nominale  Wertpapierbezeichnung  ISIN  (WKN)", f"Stück {qty}  {name}  {isin}  ({wkn})",
             "REG. SHS CL. A DL-,001",
             f"Zahlbarkeitstag  {pay}  {'Dividende' if div else 'Auszahlung'} pro Stück  {per}  {ccy}",
             "Bestandsstichtag  07.03.2025  Herkunftsland  Beispielland",
             "Ex-Tag  10.03.2025" + ("  Art der Dividende  Quartalsdividende" if div else ""),
             f"Devisenkurs  EUR / {ccy} {fx}", f"Devisenkursdatum  {day}",
             f"{'Dividendengutschrift' if div else 'Kurswert'}  {gross_f}  {ccy}  {gross}+ EUR",
             f"Umrechnung in EUR  {gross}  EUR"]
    if qst:
        lines += [f"Einbehaltene Quellensteuer {qst[0]} % auf {gross_f} {ccy}  {qst[1]}- EUR",
                  f"Anrechenbare Quellensteuer {qst[0]} % auf {gross} EUR  {qst[1]}  EUR"]
    if div:
        lines.append(f"Kapitalertragsteuerpflichtige Dividende  {gross}  EUR")
    if verrechnet:
        lines += ["Verrechnete anrechenbare ausländische Quellensteuer", "(Verhältnis 100/25) auf 0,55 EUR  2,20 - EUR"]
    if kest:
        lines.append(f"Berechnungsgrundlage für die Kapitalertragsteuer  {kest[1]}  EUR")
        lines.append(f"Kapitalertragsteuer {kest[0]} % auf {kest[1]} EUR  {kest[2]}- EUR")
    else:
        lines.append("Berechnungsgrundlage für die Kapitalertragsteuer  0,00  EUR")
    if soli:
        lines.append(f"Solidaritätszuschlag {soli[0]} % auf {soli[1]} EUR  {soli[2]}- EUR")
    lines.append(f"Ausmachender Betrag  {total or de(cash)}+ EUR")
    if rueckforderbar:
        lines.append("10 % rückforderbare Quellensteuer  27,55  NOK")
    lines += ["Lagerstelle Clearstream Banking Lux (849133 / 64003)",
              f"98  Den Betrag buchen wir mit Wertstellung {value_day or day} zu {direction} des Kontos 12345678 "
              "(IBAN DE00 0000 0000 0000 0000 00), BLZ 12030000 (BIC BYLADEM1001).", "Keine Steuerbescheinigung.",
              "Seite 2", f"Abrechnungsnr.  {abr}",
              "Nachrichtlich die Übersicht Ihrer Verrechnungs- und Steuertopfsalden zum Zeitpunkt der Erstellung.",
              "Kapitalertragsteuer 25,00 % auf 1,00 EUR  0,25- EUR",
              *(pots or ["Vorher  0,00  0,00  0,00  0,00  10.418,56"]), *NOISE, *FOOT]
    return lines


def _ca(title: list[str], beleg: str, day: str, body: list[str], head: str | None = None) -> list[str]:
    """Kapitalmaßnahme: Kopf mit Belegnummer, Titel unter dem Adressblock, Wertpapierkopf, Anrede, Text."""
    head = head or "Stück 100  BEISPIEL GROUP AB  US67421J1088  (A3CQRG)"
    return [*HEAD[:3], f"Belegnummer  {beleg}", f"Frau  Datum  {day}",
            "Erika Mustermann  Kapitalmaßnahme  KSPLR40190012000", "Beispielweg 1", "12345 Beispielstadt", *title,
            "Nominale  Wertpapierbezeichnung  ISIN  (WKN)", head, "NAMN-AKT.(SPON.ADS)/1 O.N.",
            "Sehr geehrte Frau Mustermann,", *body, "Mit freundlichen Grüßen", *FOOT]


def ca_reverse_split(stage: int = 2, beleg: str = "01433126", valuta: str = "18.02.2025", ratio: str = "20 : 1",
                     out_qty: str = "100", in_qty: str = "5", new_isin: str = "US67421J2078",
                     new_wkn: str = "A41264") -> list[str]:
    if stage == 1:
        body = [f"Ihren Depotbestand haben wir mit Valuta {valuta} zu folgenden Bedingungen umgebucht:",
                "Verhältnis:  1 : 1", "Ausbuchung", f"Stück {out_qty}-  BEISPIEL GROUP AB  US67421J1088  (A3CQRG)",
                "Einbuchung", f"Stück {out_qty}  BEISPIEL GROUP AB  US67421J1088  (A3CQRG)", "Wertpapierrechnung USA",
                "Sperre w/ bevorstehender KM"]
    else:
        body = [f"Ihren Depotbestand haben wir mit Valuta {valuta} zu folgenden Bedingungen umgebucht:",
                f"Ex-Tag:  {valuta}", f"Verhältnis:  {ratio}", "Ausbuchung",
                f"Stück {out_qty}-  BEISPIEL GROUP AB  US67421J1088  (A3CQRG)", "Einbuchung",
                f"Stück {in_qty}  BEISPIEL GROUP AB  {new_isin}  ({new_wkn})", "NAMN-AKTIER (ADR)/20",
                "Wertpapierrechnung USA", "Ab sofort können Sie über die neuen Titel verfügen."]
    return _ca(["Zusammenlegung von Titeln (Reverse Split)"], beleg, "19.02.2025", body)


def ca_gratis(beleg: str = "01454139", title: str = "Gratisausgabe - Neubuchung nach Storno", tax: bool = True,
              total: str | None = None) -> list[str]:
    body = ["Ihrem Depot wurden mit Valuta 10.10.2025 Wertpapiere zu folgenden Bedingungen gutgeschrieben:",
            "Ex-Tag:  03.10.2025", "Verhältnis:  10 : 1", "Einbuchung",
            "Stück 2  BEISPIEL CORP.  US36467W1172  (A4AQAG)", "WTS 30.10.26", "Wertpapierrechnung USA",
            "Die Gesellschaft hat Bonustitel ausgegeben."]
    if tax:
        body += ["Seite 2 von 2", f"Belegnummer  {beleg}", "Ermittlung steuerrelevanter Erträge",
                 "steuerrelevanter Bewertungskurs 2,477 EUR", "Unbarer Ertrag aus Kapitalmaßnahmen  4,95 EUR",
                 "Berechnungsgrundlage für die Kapitalertragsteuer  4,95 EUR",
                 "Steuerberechnung (Geschäftsvorfallnummer 80000000736)",
                 "Kapitalertragsteuer 25,00 % auf 4,95 EUR  1,24- EUR",
                 "Solidaritätszuschlag 5,50% auf 1,24 EUR  0,06- EUR",
                 f"Ausmachender Betrag  {total or '1,30'}- EUR",
                 "Den ausmachenden Betrag buchen wir mit Valuta 10.10.2025 zu Lasten des Kontos 12345678",
                 "Nachrichtlich die Übersicht Ihrer Verrechnungs- und Steuertopfsalden zum Zeitpunkt der Erstellung.",
                 "Unbarer Ertrag aus  0,00  0,00  0,00  0,00  4,95"]
    return _ca([title], beleg, "27.10.2025", body, "Stück 20  BEISPIEL CORP.  US36467W1099  (A0HGDX)")


def ca_storno(beleg: str = "01452937") -> list[str]:
    body = [f"wir haben den Vorgang mit der Belegnummer {beleg} und Valuta 10.10.2025 storniert. Es ergibt sich "
            "folgende Buchung:", "Ausbuchung", "Stück 2-  BEISPIEL CORP.  US36467W1172  (A4AQAG)", "WTS 30.10.26",
            "Aufgrund einer geänderten steuerlichen Bewertung sind ein Storno und eine Neubuchung notwendig."]
    return _ca(["Storno", "Gratisausgabe"], beleg, "27.10.2025", body,
               "Stück 20  BEISPIEL CORP.  US36467W1099  (A0HGDX)")


def ca_entgelt(total: str = "1,91", per: str = "0,02 USD", fx: str = "1,0455") -> list[str]:
    body = ["für den obigen Depotbestand aus Buchung vom 17.02.2025 mit Belegnummer 01432975 hat die Lagerstelle ein "
            "Entgelt von", f"{per} pro Stück belastet.", f"Devisenkurs (USD/EUR) {fx} vom 24.02.2025.",
            f"Ausmachender Betrag  {total}- EUR",
            "Den Gegenwert buchen wir mit Valuta 25.02.2025 zu Lasten des Kontos 12345678"]
    return _ca(["Entgeltbelastung in Folge einer Kapitalmaßnahme"], "80014500", "25.02.2025", body)


def ca_wertlos(loss: str = "121,55", cost: str = "121,55") -> list[str]:
    body = ["folgenden Depotbestand haben wir mit Valuta 30.10.2025 ersatzlos ausgebucht:", "Ausbuchung",
            "Stück 100-  BEISPIEL ASA  NO0010890304  (A2QBSN)", "Die zuständige Depotbank hat die Aktien als wertlos "
            "eingestuft.", "Ermittlung steuerrelevanter Erträge auf Folgeseite.", "Seite 2 von 2",
            "Belegnummer  01454421", "Veräußerung infolge von Kapitalmaßnahme - Kurswert  0,00 EUR",
            "steuerrelevanter Bewertungskurs liegt nicht vor", "Ermittlung steuerrelevanter Erträge",
            f"Bemessungsgrundlage für die Kapitalertragsteuer  {loss}- EUR",
            "Berechnungsgrundlage für die Kapitalertragsteuer  0,00 EUR",
            "Berücksichtigte Anschaffungsgeschäfte (alle Beträge in EUR)",
            f"Kauf  6561702500  15.12.2022 Stück  100,0000  {cost}  0,00  {cost}- (D)"]
    return _ca(["Wertlose Titel - Ausbuchungsanzeige"], "01454421", "31.10.2025", body,
               "Stück 100  BEISPIEL ASA  NO0010890304  (A2QBSN)")


def ca_umbenennung() -> list[str]:
    body = ["bitte beachten Sie die neue Emittentenbezeichnung:", "Bezeichnung alt:  ALT BATTERY INC.",
            "Bezeichnung neu:  NEU ENERGY INC."]
    return _ca(["Änderung der Emittentenbezeichnung"], "91818538", "04.03.2025", body,
               "Stück 200  ALT BATTERY INC.  US35834F1049  (A3E3UN)")


def ca_info(title: str = "Antrag auf Chapter VII", beleg: str = "91805815") -> list[str]:
    body = ["die Gesellschaft hat einen Antrag gemäß Chapter VII des US Bankruptcy Code gestellt.",
            "Das bedeutet, dass eine vollständige Liquidation der Gesellschaft angestrebt wird."]
    return _ca([title], beleg, "22.01.2025", body, "Stück 4  BEISPIEL MOTORS INC.  US13803R3003  (A40XUX)")


def ersatzlose_voll(loss: str = "238,95", cost: str = "238,95") -> list[str]:
    return [*_kopf(["Auftragsnummer  792595/75.00", "Datum  07.01.2025"]), "Ersatzlose Ausbuchung",
            "Nominale  Wertpapierbezeichnung  ISIN  (WKN)", "Stück 50  BEISPIEL S.A.  FR0013018041  (A2JQ3R)",
            "Schlusstag  07.01.2025  Auftrags-Nr.  792595/75.00", "Valuta  07.01.2025",
            "Ermittlung steuerrelevante Erträge", f"Veräußerungsverlust  {loss}- EUR",
            "Nachrichtlich die Übersicht Ihrer Verrechnungs- und Steuertopfsalden zum Zeitpunkt der Erstellung.",
            "Vorher  0,00  0,00  2.000,00  0,00  0,00", "Seite 2 von 2",
            f"Kauf  9780911800  30.11.2020 Stück  50,0000  {cost}  nicht relevant  0,00  {cost}- (D)", *FOOT]


def depotuebertrag() -> list[str]:
    return [*_kopf(["Auftragsnummer  792595/75.00", "Datum  07.01.2025"]), "Ersatzlose Ausbuchung",
            "Nominale  Wertpapierbezeichnung  ISIN  (WKN)", "Stück 50  BEISPIEL S.A.  FR0013018041  (A2JQ3R)",
            "Schlusstag  07.01.2025  Auftrags-Nr.  792595/75.00", "Veräußerungsverlust  238,95- EUR", *FOOT]


def depotauszug() -> list[str]:
    return ["Datum  01.07.2024", f"Depotnummer  {DEPOT}", "Kundendepot", "Erika Mustermann",
            "Aufstellung über Kundenfinanzinstrumente per 30.06.2024",
            "Nominale  Wertpapierinformationen  Kurs in Währung pro Stück / %  Kurswert in EUR",
            "Stück  50  BEISPIEL S.A.  0,0001 EUR  0,01", "FR0013018041 (A2JQ3R)  24.12.2024", *FOOT]


def depotauszug_voll(stichtag: str = "30.06.2024", total: str | None = None, count: int | None = None,
                     gross: str = "0,50", net: str = "0,42", vat: str = "0,08", with_fee: bool = True) -> list[str]:
    """Depotauszug wie bei der DKB (Kopfseite + Positionsseiten): Stück/Name/Kurs/Kurswert, Zeile
    ``ISIN (WKN)  Kursdatum``, ``ohne Bewertung``, Summenzeile und Depotentgelt. Werte erfunden, rechnerisch stimmig."""
    lines = ["Datum  01.07.2024", f"Depotnummer  {DEPOT}", "Kundendepot",
             f"Aufstellung über Kundenfinanzinstrumente per {stichtag}", "Sehr geehrte Frau Mustermann,",
             "Mit freundlichen Grüßen", *FOOT, "Seite 1 von 3", "Datum  01.07.2024", f"Depotnummer  {DEPOT}",
             f"Aufstellung über Kundenfinanzinstrumente per {stichtag}",
             "Nominale  Wertpapierinformationen  Kurs in Währung pro Stück / %  Kurswert in EUR",
             "Devisenkurs  Depotentgelt", "Kursdatum", "Handelsplatz", "Aktienwerte",
             "Stück  20  BEISPIEL HOLDING INC.  169,88 EUR  3.397,60", "REG. SHS CL. A DL-,001",
             "US02079K3059 (A14Y6F)  28.06.2024", "Tradegate Freiverk.",
             "Stück  200  BEISPIEL AVIATION INC.  3,335 USD  623,07", "REG. SHARES CL.A DL -,0001  1,0705",
             "10  US03945R1023 (A3C3BQ)  28.06.2024", "Wertpapierrechnung USA  New York (NYSE)", "Seite 2 von 3",
             "Stück  50  BEISPIEL S.A.  ohne Bewertung", "ACTIONS PORT. EO -,001", "FR0013018041 (A2JQ3R)",
             f"{count or 3} Posten  Summe Kurswerte  {total or '4.020,67'}"]
    if with_fee:
        lines += ["Die Depotentgelte für den Zeitraum vom 01.04.2024 bis 30.06.2024 betragen",
                  f"netto {net} Euro + 19% USt {vat} Euro = brutto {gross} Euro.",
                  "Die Belastung erfolgt mit Valuta 30.07.2024 auf dem Konto 12345678 (IBAN DE00 0000 0000 0000 0000),"]
    return lines


def kosteninfo_neu(side: str = "Kauf") -> list[str]:
    return ["Ex-Ante Kosteninformation", "Ergebnisdarstellung",
            "Wertpapierbezeichnung BEISPIEL TURBO 97Y  08.10.2024, 15:02:25 Uhr", "ISIN/WKN  DE000LX4QJM1/LX4QJM",
            "Ausführungsplatz  Best-Execution  Erika Mustermann",
            f"Menge (Kurs)  {side} von 1.000,00 Stück (zu 0,57 €)  Kundennummer:  11112222",
            f"Anlagebetrag  570,00 €  Depotnummer:  {DEPOT}", "1 Aufstellung der Kostenpositionen"]


def kosteninfo_voll(side: str = "Kauf", day: str = "05.08.2024", at: str = "15:02:25", isin: str = "US88023B1035",
                    wkn: str = "A40EDP", qty: str = "10,00", price: str = "32,82", amount: str = "328,20",
                    dl_in: str = "36,81", dl_out: str = "10,00", fx_in: str = "1,39", plan: bool = False) -> list[str]:
    """Ex-Ante-Kosteninformation (neues Layout): Käufe mit drei Kostenspalten, Verkäufe nur mit Ausstiegsspalte."""
    unit = "50,00 € (zu 50,00 €)" if plan else f"{qty} Stück (zu {price} €)"
    head = ["Ex-Ante Kosteninformation", "Ergebnisdarstellung",
            f"Wertpapierbezeichnung BEISPIEL AI INC.  {day}, {at} Uhr", f"ISIN/WKN  {isin}/{wkn}",
            f"Menge (Kurs)  {side} von {unit}  Kundennummer:  11112222",
            f"Anlagebetrag  {amount} €  Depotnummer:  {DEPOT}"]
    if side == "Kauf":
        body = ["1 Aufstellung der Kostenpositionen", "Einstiegskosten  Laufende Kosten  Ausstiegskosten",
                "(einmalig)  (pro Jahr)  (einmalig)",
                f"Dienstleistungskosten  {dl_in} €  2,19 %  0,00 €  0,00 %  {dl_out} €  2,19 %",
                "davon Zuwendungen an die DKB  0,00 €  0,00 %  0,00 €  0,00 %  0,00 €  0,00 %",
                "Produktkosten  5,41 €  0,95 %  20,29 €  3,56 %  14,59 €  2,56 %",
                f"Fremdwährungskosten  {fx_in} €  0,00 %  0,00 €  0,00 %  0,00 €  0,00 %",
                "2 Gesamtkosten für eine Haltedauer von 3 Jahren", "Absolut über Haltedauer  Relativ pro Jahr",
                "Dienstleistungskosten  25,00 €  1,46 %", "Produktkosten  80,87 €  4,73 %",
                "Gesamtkosten  105,87 €  6,19 % p. a."]
    else:
        body = ["Aufstellung der Kostenpositionen", "Ausstiegskosten", "(einmalig)",
                f"Dienstleistungskosten  {dl_out} €  0,99 %", "davon Zuwendungen an die DKB  0,00 €  0,00 %",
                "Produktkosten  5,78 €  0,43 %", "Fremdwährungskosten  0,00 €  0,00 %",
                "Gesamtkosten  18,93 €  1,42 %"]
    return [*head, *body, "DKB AG | 3457 | 02.2023  1 von 1"]


def kosteninfo_legacy_voll(day: str = "17.11.2021", at: str = "09:00:45", qty: str = "100,00", amount: str = "2.054,50",
                           dl_in: str = "10,00", dl_out: str = "10,00", price: str | None = None) -> list[str]:
    """Legacy-Layout (ohne Richtung, Kosten in Blöcken Einstieg/Laufend/Ausstieg)."""
    lines = [f"Frau  Depotnummer: {DEPOT}", "Erika Mustermann  Kundennummer: 11112222", "Beispielweg 1",
             f"Datum/Zeit: {day} {at}", "12345 Beispielstadt", "Ex-Ante-Kostensimulation - Ergebnisdarstellung",
             "Stück  Wertpapierbezeichnung  ISIN  WKN",
             f"Stück {qty}  BEISPIEL TECH INC. REGISTERED  US69608A1088  A2QA4J",
             f"Ausführungsplatz  Best-Execution  Anlagebetrag  {amount} EUR"]
    if price:
        lines.insert(-1, f"Ausführungsplatz  Best-Execution  Kurs  {price} EUR")
    return [*lines, "(1) Aufstellung der Kostenpositionen  (2) Zusammenfassung der Kosten für eine Haltedauer",
            "Anlagebetrag (=Kurswert) angegeben.  Dienstleistungskosten*  20,00 €  0,10 %",
            "Einstiegskosten (einmalig)  - davon Margen  0,00 €  0,00 %", "Produktkosten  0,00 €  0,00 %",
            f"Dienstleistungskosten*  {dl_in} €  0,49 %", "Fremdwährungskosten  0,00 €  0,00 %",
            "Laufende Kosten**  pro Jahr  pro Jahr  Wertentwicklung", "Dienstleistungskosten*  0,00 €  0,00 %",
            "Ausstiegskosten (einmalig)", f"Dienstleistungskosten*  {dl_out} €  0,49 %",
            "Produktkosten  0,00 €  0,00 %", "Fremdwährungskosten  0,00 €  0,00 %", "Seite 1 von 2"]


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
    return [*HEAD[:3], "Belegnummer  50110420", "Frau  Datum  14.02.2025", "Erika Mustermann", "Beispielweg 1",
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
    "dividende": (ertrag(), ("ertrag", "dividende", "", "2025-03-18", "97764418230")),
    "rueckzahlung": (ertrag("Gutschrift", "Kapitalrückzahlung", "97858717490", "19.03.2025"),
                     ("ertrag", "kapitalrueckzahlung", "", "2025-03-19", "97858717490")),
    "liquidation_ertrag": (ertrag("Gutschrift", "Liquidationszahlung", "19363215100", "20.10.2025"),
                           ("ertrag", "liquidation", "", "2025-10-20", "19363215100")),
    "reverse_split": (kapital(["Zusammenlegung von Titeln (Reverse Split)"]),
                      ("kapitalmassnahme", "reverse_split", "", "2025-02-17", "01432975")),
    "storno": (kapital(["Storno", "Gratisausgabe"], "01452937", "27.10.2025"),
               ("kapitalmassnahme", "storno", "", "2025-10-27", "01452937")),
    "neubuchung": (kapital(["Gratisausgabe - Neubuchung nach Storno"], "01454139", "27.10.2025"),
                   ("kapitalmassnahme", "neubuchung", "", "2025-10-27", "01454139")),
    "gratis": (kapital(["Gratisausgabe"], "91877627", "30.09.2025"),
               ("kapitalmassnahme", "gratis", "", "2025-09-30", "91877627")),
    "entgelt": (kapital(["Entgeltbelastung in Folge einer Kapitalmaßnahme"], "80014500", "25.02.2025"),
                ("kapitalmassnahme", "entgelt", "", "2025-02-25", "80014500")),
    "umbenennung": (kapital(["Änderung der Emittentenbezeichnung"], "91818538", "04.03.2025"),
                    ("kapitalmassnahme", "umbenennung", "", "2025-03-04", "91818538")),
    "wertlos": (kapital(["Wertlose Titel - Ausbuchungsanzeige"], "01454421", "31.10.2025"),
                ("kapitalmassnahme", "wertlos", "", "2025-10-31", "01454421")),
    "ausbuchung": (depotuebertrag(), ("depotuebertrag", "ersatzlose_ausbuchung", "", "2025-01-07", "7925957500")),
    "depotauszug": (depotauszug(), ("depotauszug", "", "", "2024-07-01", "2345-20240701")),
    "kosten_neu": (kosteninfo_neu(), ("kosteninfo_order", "", "buy", "2024-10-08", "LX4QJM-150225")),
    "kosten_verkauf": (kosteninfo_neu("Verkauf"), ("kosteninfo_order", "", "sell", "2024-10-08", "LX4QJM-150225")),
    "kosten_legacy": (kosteninfo_legacy(), ("kosteninfo_order", "", "", "2021-11-17", "A2QA4J-090045")),
    "kosten_jahr": (kosteninfo_jahr(), ("kosteninfo_jahr", "", "", "", "")),
    "hauptversammlung": (hauptversammlung(), ("hauptversammlung", "", "", "2025-02-14", "50110420")),
    "verlustschwelle": (verlustschwelle(), ("verlustschwelle", "", "", "2024-10-08", "")),
}


def pdf_of(lines: list[str]) -> bytes:
    return text_pdf(lines)
