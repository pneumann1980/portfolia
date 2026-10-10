# Eröffnungspositionen und das KREST-Muster (M29, Teil G)

Leitsatz: **Eine Eröffnungsposition ist die Ausnahme.** Ein Bestand ohne belegte Herkunft bleibt in der
Bestandsklärung (`/quality/diagnose/stock`) eine *ungeklärte Differenz* – kein Zugang, kein Kauf, kein Lot, kein
Gewinn oder Verlust.

## Prüfung des bestehenden Musters (KREST, Nova Wallet)

Der Export enthält eine rekonstruierte Eröffnungsposition für die gehackte Wallet (Buchung `a2e243f9…`):

| Aspekt | Befund |
|---|---|
| Modellierung | normaler Zugang (`deposit`) mit Tag `opening_balance`, Quelle `reconstructed`, Kennzeichen `RECONSTRUCTED_GAP;PROPOSED_B`; Menge = Diebstahlsumme (3.155 KREST + Netzgebühr) abzüglich belegter MEXC-Zugänge |
| Datum | Tag des Diebstahls (07.04.2024) – **kein** belegtes Anschaffungsdatum, sondern der Zeitpunkt des Abgangs |
| Kosten | 661,29 € – aus dem implizierten Koinly-Kurs **geschätzt**, nicht belegt |
| Lots/FIFO | ein gewöhnliches Lot mit geschätztem Datum und geschätzten Kosten; es steht in der FIFO-Reihenfolge des Kontos und wird sofort vom Diebstahl (`lost`, 2.088,48 €) verbraucht |
| Sichtbarkeit | Befund „rekonstruierter Anfangsbestand (Lücke)“ (Art *Rekonstruiert oder geschätzt*) mit offenen Lots, Abgängen je Jahr, Haltefrist-Hinweis |
| Vorschau/Rückgängig | Teil des kuratierten Imports – in der App weder anlegbar noch rücknehmbar (nur durch einen neuen Import) |
| Performance | unauffällig (eine Buchung) |

**Bewertung:** Das Muster erfüllt die M29-Vorgaben **nicht**: Datum und Kosten sind Annahmen, die als Lot in FIFO und
Steuerauswertung eingehen. Es bleibt als **gekennzeichnete, geschätzte Ausnahme** bestehen (Portfolia ändert den
kuratierten Import nicht) und wird in der Diagnose weiterhin ausgewiesen; neue Positionen dieser Art entstehen nicht
mehr automatisch.

## Was die Engine abbilden kann

Der Ledger erzeugt Lots nur aus Zugängen **mit EUR-Wert** (`_deposit`); „Anschaffungsdatum unbekannt“ bzw. „Kosten
unbekannt“ lässt sich nicht darstellen. Konsequenz: **Unbekannte Anschaffungsdaten werden nie gebucht**; die Position
bleibt in der Bestandsklärung. Es gibt keinen Vorgabewert für Datum (nicht „heute“, nicht Belegdatum) und keine Kosten 0.

## Die Ausnahme: „Eröffnungsposition buchen“

Angeboten bei einer *Referenz-Abweichung mit fehlendem Bestand* (Referenz über Ledger), nie bei einem Überschuss.
Pflichtangaben (ohne Vorgabe, außer Belegdatum/-art aus dem Referenzbestand):

* Konto, Asset, Menge (= Referenz − Soll, aus dem Befund – nicht editierbar),
* **Datum des Belegs** und **Art** (Kontoauszug · Anzeige beim Anbieter · sonstiger Beleg),
* **nachgewiesenes Anschaffungsdatum** und **nachgewiesene Anschaffungskosten (EUR)**,
* **Beleg der Anschaffungsdaten** (Dokument, Kennung).

Ablauf: Vorschau mit allen Folgen (Bestand, Einstand/Lots, realisierte Ergebnisse, Steuer, erledigte Befunde) →
ausdrückliche Einzelfreigabe → Eintrag im Protokoll (`diag_decision`, Prüfsumme der Vorschau, Datenstand, Wirkung) →
**Rückgängig** (Buchung wird „zurückgenommen“, nichts gelöscht). Die Buchung trägt Tag `opening_balance`, die Notiz
„Eröffnungsposition (Ausnahme, Diagnose): … belegt durch: …“ und erscheint als Befund *Eröffnungsposition mit belegten
Anschaffungsdaten*; sie reist mit dem Gesamtexport (Journal-Buchung mit Notiz). Portfolia prüft den Beleg nicht – die
Angaben stammen vom Nutzer.
