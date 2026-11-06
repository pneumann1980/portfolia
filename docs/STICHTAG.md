# Stichtag: ab einem Tag sauber weiterführen (0.97.0 – 0.105.0)

Stand: 06.11.2026 · Version 0.105.0

Ein Stichtag (Standard 01.01.2025, in der App wählbar) teilt die Buchungen in ein **Archiv** (davor) und den **aktiven
Zeitraum** (ab dem Stichtag). Steuer, Lots, Diagnose und Importe arbeiten ab dem Stichtag mit dem geprüften
Eröffnungsstand; das Archiv bleibt vollständig erhalten, Depotwert, Verlauf und eingesetztes Kapital laufen weiter ab
Depotstart. **Es wird nichts gelöscht.** Einstieg: *Einstellungen → Stichtag* (`/settings/stichtag`).

## Die fünf Stufen

| Stufe | Version | Inhalt |
| --- | --- | --- |
| 1 | 0.97.0 | **Fiat überall aus** (`ledger.fiat_off`): EUR/USD-Bestände werden nirgends geführt; EUR-Werte von Käufen, Verkäufen und Gebühren bleiben. Vorschau vor dem Umschalten (`/settings/fiat`), Diagnose und Bestandsabgleich ohne Fiat |
| 2 | 0.98.0 | **Rechenkern und Vorschau:** Eröffnungsstand = Zustand der vollständigen Berechnung am Ende von Stichtag − 1 (Lots mit ursprünglichem Anschaffungsdatum und Kosten, Bestände, Cash-Führung). Gleichwertigkeitsprüfung „Archiv + aktiv = vollständig“ |
| 3 | 0.99.0 | **Festschreiben** (eine Schreibtransaktion, gebunden an den angezeigten Datenstand), **Freigabe je Konto**, **Korrekturen nur mit Beleg**, **Rückgängig**, **Schutz des Archivs**, Export/Wiederherstellung |
| 4 | 0.100.0 | **Alle Wege am Stichtag ausgerichtet:** Diagnose, Gesamtprüfung, CSV-Import/Sync/Prüf-Stapel, Quellenübernahme |
| 5 | 0.101.0 | **Abgeleitete Vorschläge** aus Belegen nach dem Stichtag – nur wo sie belegt sind |
| + | 0.102.0 | **Ausgleich zum Stichtag** (alles glattziehen) und **Quellenübernahme ab dem Stichtag** |
| + | 0.105.0 | **Jedes Asset zum Stichtag gesetzt:** Rundungsreste im Ausgleich, Abdeckung, Archiv-Pläne ausgeschlossen, Meldung statt Fehlerseite |
| + | 0.104.0 | **Klärungsfälle folgen dem Stichtag:** erledigte/archivierte Fälle erscheinen nicht mehr, Fortschritt im Fall, Nachlauf nach Stichtag-Aktionen |
| + | 0.103.0 | **Ausgleich übersichtlich:** Vorher → Nachher, Gewichtung, Entscheidung je Zeile, eigener Zielbestand, Abruf, Zusammenfassung |

## Eröffnungsstand und Gleichwertigkeit

* Der Eröffnungsstand ist **kein neues Rechenergebnis**, sondern der Zustand der vollständigen Berechnung zum Ende des
  Vortags (`OpeningState`: Lots, Bestände, gemeldete Fehlbestände, Cash-Führung). Die Engine startet damit und rechnet
  nur die Buchungen ab dem Stichtag.
* Vor dem Festschreiben müssen alle Pflichtprüfungen bestehen: Bestände heute, offene Lots (Menge, Kosten,
  Anschaffungsdatum, Haltefrist), Veräußerungen, Erträge, Gebühren, einbehaltene Steuern, Zahlungsströme,
  Ledger-Meldungen und das eingesetzte Kapital ab Depotstart – jeweils vollständig gegen aktiv. **Beide Rechenweisen**
  (FIFO je Konto/Wallet für die Steuer, FIFO global für die App-Ansicht) werden geprüft; angezeigt wird der
  steuerrelevante Stand.
* Steuerjahre **vor** dem Stichtag bleiben aus den vollständigen Daten berechnet; der Stichtag ändert sie nicht.
* Anschaffungskosten gelten wie in den Daten. Die Seite weist nur aus, wo ungenaue Kosten überhaupt steuerlich wirken
  könnten (Lots, die innerhalb eines Jahres nach Anschaffung verkauft werden).

## Festschreiben, Freigabe, Rücknahme

1. **Vorschau** (`CS.preview`): Pflichtprüfungen, Eröffnungsstand je Konto, Belege (Referenzbestände zum Vortag),
   Steuer-Prüfwerte, Auffälligkeiten (z. B. Übertragungen über den Stichtag).
2. **Freigabe je Konto:** `belegt` (alle Referenzen zum Vortag passen), `abweichung`, `ohne_beleg`. Jedes Konto mit
   Eröffnungsstand wird ausdrücklich freigegeben; ohne Beleg heißt: der berechnete Stand gilt. Prüfsumme je Konto.
3. **Festschreiben:** Token aus Stichtag, Datenstand und Prüfsummen aller Konten; Datenstand wird als erste Anweisung
   der Schreibtransaktion erneut geprüft. Es ändert **keine Buchung**, protokolliert Stichtag, Freigaben und
   Prüfergebnis und schützt das Archiv. Höchstens ein aktiver Stichtag.
4. **Rücknahme:** stellt den Zustand davor her (Archiv wieder bearbeitbar, alle Auswertungen über den vollen Stand).
5. **Danach:** Verändert sich der Eröffnungsstand eines Kontos (Korrektur, Kontenzusammenführung, Asset-Änderung), zeigt
   die Seite es an; erst nach ausdrücklicher erneuter Freigabe gilt der neue Stand.

## Schutz des Archivs

Buchungen **vor** dem Stichtag lassen sich nicht anlegen, ändern, löschen, zurücknehmen oder wiederherstellen. Ein
einziger Ort entscheidet (`app/cutover/guard.py`, Vergleich in UTC gegen den Beginn des Stichtags in Ortszeit); alle
Schreibwege des Journals rufen ihn auf (Anlegen, Bearbeiten, Status, Entkoppeln, Import-Bearbeitung/-Löschung/
-Wiederherstellung/-Rückgängig). Über das Web erscheint eine verständliche Meldung (HTTP 409) mit Hinweis auf die
Rücknahme. Nur der Stichtag-Dienst selbst darf mit `guard.allow()` Korrekturen zum Stichtag schreiben.

### Wegematrix (Schreibwege → Schutz)

| Weg | Schutz |
| --- | --- |
| Buchung anlegen / bearbeiten (`save`, `_insert`, `_update`) | `check_ts` für neuen und alten Zeitpunkt |
| Löschen, Wiederherstellen, Status (`delete`, `restore`, `_set_status`) | `check_ts` auf den Zeitpunkt der Buchung |
| Transfer entkoppeln (`unpair`) | `check_ts` |
| Import-Buchung überlagern / ausblenden / zurückholen (`save_import`, `delete_import`, `restore_import`, `revert_import`) | `check_ts`; `save_import` meldet im Formular, die übrigen lehnen ab bzw. werden über den 409-Handler erklärt |
| Diagnose-Korrekturen, Prüf-Stapel, Quellenübernahme, Assistent | laufen über dieselben Journal-Funktionen; die Übernahme prüft zusätzlich vorab |
| Stichtag-Korrektur und deren Rücknahme | einziger Weg mit `guard.allow()` |
| Wiederherstellung aus dem Gesamtexport | schreibt direkt in die Datenbank (Neueinrichtung/Übernahme), nicht über das Journal |

Nicht geschützt, weil keine Buchungsdaten: Zuordnungen (Konten, Assets, Kursquellen), Referenzbestände, Entscheidungen
ohne Datenänderung. Eine Zuordnung, die den Eröffnungsstand eines Kontos verändert (z. B. Kontenzusammenführung), erkennt
die Seite über die Prüfsumme je Konto („Eröffnungsstand seit der Freigabe verändert“).

## Korrekturen nur mit Beleg

Grundsatz: **Der berechnete Stand gilt.** Ein Vorschlag entsteht nur, wo ein Referenzbestand zum Ende von Stichtag − 1
abweicht (Staub < 1e-8 wird nur ausgewiesen). Eine Korrektur ist eine gewöhnliche, gekennzeichnete Buchung **vor** dem
Stichtag, damit vollständige Berechnung und Eröffnungsstand übereinstimmen, und einzeln rücknehmbar. Vorab zeigt die
Seite die Wirkung auf Bestand, Lots, Kosten und realisierte Ergebnisse je Jahr.

| Fall | Buchung | Voraussetzung |
| --- | --- | --- |
| zu viel gebucht | Abgang zum Ende des Vortags, Tag `cutover_correction`: verbraucht die ältesten Lots, ist **weder Veräußerung noch Verlust noch Zahlungsstrom** | Beleg/Begründung; Menge ≤ Bestand zum Stichtag |
| zu wenig gebucht, belegt | Zugang, Tag `opening_balance` | belegtes Anschaffungsdatum **vor** dem Stichtag, belegte Kosten, Beleg |
| zu wenig gebucht, vorsichtig | Zugang am Vortag zu 0 € (Haltefrist beginnt neu) | ausdrücklich als Annahme gekennzeichnet |
| zu wenig gebucht, ohne Beleg | **nichts** – bleibt offen | – |

## Stufe 4: alle Wege am Stichtag ausgerichtet

* **Diagnose:** Befunde, deren Buchungen **alle** vor dem Stichtag liegen, gelten als Archiv: nicht in den offenen
  Listen, nicht im Assistenten, nicht in den Zählern (`Report.archived`, `stats["archiviert"]`). Ein Sammelbefund ist nur
  Archiv, wenn alle seine Einzelvorgänge es sind. Befunde ohne Buchungsbezug (Bestände, Kurse, Quellen) und Befunde, die
  Archiv **und** aktive Buchungen mischen, bleiben sichtbar.
* **Gesamtprüfung:** Prüfpunkte mit ausschließlich Archiv-Buchungen erhalten den Status `archiviert` (zählen nicht als
  offen).
* **CSV-Import, Sync, Prüf-Stapel:** Zeilen vor dem Stichtag sind „vor Stichtag“ mit Hinweis „Archiv – wird nicht
  übernommen“ (auch wenn der Stapel ohne eigenen Stichtag angelegt wurde). Erzwingen scheitert am Schutz.
* **Quellenübernahme (ab 0.102.0):** Die Quelle ersetzt Buchungen **erst ab dem Stichtag**. Buchungen davor bleiben
  (Archiv), Vorgänge der Quelle davor werden nicht gebucht und zählen in der Vorgangszählung als bewusst ausgelassen.
  Die Pflichtprüfung „Archiv vor dem Stichtag bleibt unverändert“ bleibt als Sicherung. Der Bestandsbeweis gilt weiter:
  Archiv + Vorgänge ab dem Stichtag müssen den Bestand der Quelle ergeben – sonst ist der Eröffnungsstand die Ursache und
  der **Ausgleich zum Stichtag** die Lösung (vorher: Übernahme gesperrt, „1 zu ersetzende Buchung liegt vor dem
  Stichtag“).
* **Bewertung, Verlauf, Export:** unverändert über den vollen Stand.

## Stufe 5: abgeleitete Vorschläge (nur mit Beleg)

Auf Wunsch (`?derive=1`, „Belege nach dem Stichtag auswerten“) vergleicht die Seite Belege **nach** dem Stichtag
(Referenzbestände mit `as_of ≥ Stichtag`; vollständig abgerufene Bestände der Quellen mit Abweichung) mit dem berechneten
Bestand zum Zeitpunkt des Belegs:

* **Keine Buchung** auf Konto/Asset zwischen Stichtag und Beleg → die Abweichung kann nur schon am Stichtag bestanden
  haben (Annahme: Quelle vollständig). Ergebnis: **abgeleiteter Vorschlag** (Abgang/Zugang), in Notiz und Protokoll als
  „abgeleitet“ gekennzeichnet; ein Zugang braucht weiterhin Datum und Kosten.
* **Buchungen dazwischen** → **kein Vorschlag zum Stichtag**, nur Hinweise auf Ursachen im aktiven Zeitraum: offene
  Abgänge anderer Konten, deren Summe die Differenz erklärt (fehlender Zugang bzw. Verknüpfung als Transfer, z. B. mehrere
  Übertragungen von einer Börse in eine Wallet), oder Abgänge ohne Gebühr, wenn bei der Quelle weniger liegt (fehlende
  Gebühren). Die Behebung läuft über die gewohnten Wege.
* Ohne Beleg nach dem Stichtag entsteht nichts. Gebucht wird nie automatisch.

## Ausgleich zum Stichtag (0.102.0): alles glattziehen

*Einstellungen → Stichtag → Ausgleich zum Stichtag.* Je Konto und Asset mit Abweichung zum Beleg nach dem Stichtag
(Referenzbestand, Bestand der Quelle – auch nicht mehr aktueller Abruf, dann als „nicht aktuell“ gekennzeichnet) bucht
**eine** gekennzeichnete Buchung am Vortag des Stichtags die Differenz zum berechneten Bestand zum Zeitpunkt des Belegs.
Danach stimmen Berechnung und Beleg überein.

| Fall | Buchung | Wirkung |
| --- | --- | --- |
| Beleg < berechnet | Abgang (Tag `cutover_correction`), verbraucht die ältesten Lots | steuerlich neutral (kein Gewinn, kein Verlust, kein Zahlungsstrom) |
| Beleg > berechnet | Zugang am Vortag zu **0 €** (Tag `opening_balance`) | **Haltefrist beginnt neu**; ein Verkauf binnen eines Jahres wäre voll steuerpflichtig – in der Vorschau markiert, wo ab dem Stichtag aus diesem Konto/Asset verkauft wurde |

* Mehr als der Eröffnungsbestand lässt sich nicht am Stichtag abziehen; der Rest wird ausgewiesen (Ursache liegt im
  aktiven Zeitraum, z. B. fehlende Gebühr).
* Liegt eine Ursache nahe (offene Abgänge anderer Konten, Abgänge ohne Gebühr), steht sie in der Vorschau – es wird
  nichts unterdrückt, aber auch nichts blockiert: alle Zeilen sind vorausgewählt.
* Ein Zugang darf ein Konto anlegen, das nur als Datenquelle existiert (noch ohne Buchung); ein Abgang nicht.
* Bei festgeschriebenem Stichtag werden die betroffenen Konten danach automatisch neu freigegeben. Jede Buchung ist
  einzeln rücknehmbar (Gebuchte Korrekturen, Modus „Ausgleich“).
* **Grenze:** Der Ausgleich ersetzt keinen Beleg. Er stellt fest, dass ab hier der externe Bestand gilt; die Ursache einer
  Abweichung im aktiven Zeitraum bleibt damit ungeklärt (Hinweise in der Vorschau).

### Bedienung (ab 0.103.0)

1. **Quellen jetzt abrufen:** ruft die Bestände aller aktiven Datenquellen mit API ab (nur Bestandsabfrage, keine
   Buchungen; Fortschritt unter *Datenquellen*, danach zurück zur Seite). Börsen ohne API bleiben beim letzten Beleg –
   dort hilft der eigene Zielbestand.
2. **Entscheiden:** Je Konto eine Karte (Kontoregel: *Vorgabe*, *alle ausgleichen*, *nur Abgänge*, *keine*), je Zeile
   eine Entscheidung: *Vorgabe*, *Auf den Beleg ausgleichen*, *App behalten (nichts buchen)*, *Eigener Zielbestand
   (heute)*. Eine Zeile entscheidet vor der Kontoregel.
3. **Zusammenfassung** („Auswahl prüfen“, bucht nichts) und **Buchen**; bei festgeschriebenem Stichtag danach
   automatische Neu-Freigabe der betroffenen Konten.

Jede Zeile zeigt **App heute → Ausgleich am Vortag → App nach Ausgleich** und den Beleg (Quelle, Zeitpunkt, „nicht
aktuell“). „App heute“ ist der Bestand heute, der berechnete Bestand zum Zeitpunkt des Belegs dient der Differenz;
liegen seit dem Beleg weitere Buchungen, steht ein Hinweis, dass „nach Ausgleich“ vom Beleg abweichen kann.

**Gewichtung** (nach EUR-Wert der Differenz, Kurse der App):

| Gewicht | Kriterium | Voreinstellung |
| --- | --- | --- |
| Kleinstbetrag | Wert unter 1 € | in einer Sammelgruppe, ein Häkchen „alle pauschal ausgleichen“ (voreingestellt) |
| normal | Wert 1 € bis unter 100 € | ausgleichen |
| kritisch | Verkauf ab dem Stichtag, nicht ausgleichbarer Rest, Wert ab 100 € oder nicht bewertbar | **nicht vorausgewählt** (App behalten), einzeln entscheiden |

**Eigener Zielbestand:** Du gibst den realen Bestand von heute ein; gebucht wird die Differenz zum Bestand der App von
heute am Vortag des Stichtags (Abgang höchstens bis zum Eröffnungsbestand). Entspricht der Wert der App, entsteht keine
Buchung; ungültige Eingaben werden abgelehnt, ohne zu buchen.

### Jedes Asset zum Stichtag gesetzt (0.105.0)

Rückmeldung zu 0.104.0: Es erschienen noch Aufgaben („Diese Buchungen gehören vermutlich zusammen: SAVM auf MetaMask (ETH)“), die
beim Stichtag ausgeglichen sein müssten; „Jetzt klären?“ endete in einer Fehlerseite (HTTP 409).

* **Ursache:** Der Ausgleich ließ **Rundungsreste** (unter 1e-8, z. B. SAVM: 2·10⁻¹¹ gegenüber der Quelle) bewusst aus. Die
  Diagnose führt sie weiter als Abweichung; ihr Lösungsvorschlag wollte eine Buchung von 2024 ändern – das Archiv ist geschützt.
* **Ausgleich vollständig:** Jede Abweichung zum Beleg wird ausgeglichen, auch Rundungsreste (Gruppe *Kleinstbeträge*,
  nie kritisch, auch ohne Kurs). Nicht (vollständig) am Stichtag ausgleichbare Zeilen (Rest oberhalb des Eröffnungsbestands) sind
  immer „kritisch“ und einzeln zu entscheiden.
* **Abdeckung:** Die Seite zeigt, wie viele Bestände (Konto · Asset) mit dem Beleg übereinstimmen, wie viele abweichen (und
  in der Liste stehen), wie viele die Quelle bewusst als ausgebucht zeigt und wie viele **ohne Beleg** sind (dort gilt der
  berechnete Stand; „Quellen jetzt abrufen“ bzw. Referenzbestand erfassen).
* **Pläne ins Archiv sind nicht ausführbar:** Jede Lösung der Diagnose/Klärung, die Buchungen vor dem Stichtag ändern, anlegen
  oder ausblenden würde, hat einen Fehler in der Vorschau („Betrifft n Buchung(en) vor dem festgeschriebenen Stichtag …
  Bestände gleicht der Ausgleich aus“); in Klärungsfällen scheidet der Kandidat damit aus.
* **Sicherheitsnetz:** Ein Schreibversuch ins Archiv aus Diagnose, Klärung und Sammelaktionen liefert eine Meldung („Es wurde
  nichts geändert“) statt der Fehlerseite.
* **Grenze:** Bestände ohne Beleg lassen sich nicht ausgleichen (kein Sollwert). Ein Befund wegen eines *nicht mehr aktuellen*
  Abrufs (gleicher Wert, älter als 48 Stunden) verschwindet erst mit einem frischen Abruf („Quellen jetzt abrufen“).
* **Echtdaten (Kopie):** Ausgleich 21 Zeilen (davon 8 Rundungsreste; vorher 13), danach nur 5 zur Einzelentscheidung;
  Aufgaben nach Ausgleich 17 (vorher 24); der Fall SAVM entfällt; Abdeckung: 86 Bestände, 10 stimmen, 17 weichen ab, 59 ohne Beleg.

### Klärungsfälle und Gesamtprüfung folgen dem Stand (0.104.0)

Die Aufgaben „Bestände abgleichen“ u. a. sind gespeicherte **Klärungsfälle**. Sie wurden bisher nur bei einem Prüflauf bzw.
durch den Autopiloten neu berechnet – Festschreiben, Ausgleich, Korrekturen und Rücknahme stießen das nicht an. Folge: Fälle,
die erledigt oder nur noch Archiv waren, erschienen weiter, und eine Antwort „ist in Ordnung so“ auf einen Befund *im* Fall
änderte die Aufgabenzahl nicht (ein Fall ist eine Aufgabe, solange er nicht ganz erledigt ist).

* **Nur lebende Fälle:** Ein Fall zählt, solange mindestens einer seiner Befunde im aktuellen Bericht noch offen ist (nicht
  erledigt, nicht als geprüft markiert, nicht im Archiv). Zurückgestellte Fälle und Fälle ohne Befundliste bleiben.
  Gilt für Assistent, Arbeitsplan, Klärungsseite und die Zähler der Startseite.
* **Fortschritt im Fall:** Die Aufgabe nennt „noch n von m Befunden offen“; nach einer Antwort steht in der Rückmeldung
  „Im Fall … sind noch n von m Befunden offen“ bzw. „Der Fall … ist damit erledigt“.
* **Nachlauf:** Nach Festschreiben, Rücknahme, Neu-Freigabe, Korrektur (und deren Rücknahme) und Ausgleich werden die Fälle
  an den neuen Stand angepasst (`clarify.refresh`: ohne Recherche, ohne Übernahme von Lösungen, ohne Protokolleinträge;
  im Betrieb im Hintergrund; der Ausgleich aktualisiert einmal am Ende, nicht je Zeile).
* **Gesamtprüfung:** Die gespeicherte Prüfung wendet die Archiv-Kennzeichnung auch beim Lesen an; nach der Rücknahme des
  Stichtags werden die Punkte wieder offen.
* **Grenzen:** Fälle ohne Buchungsbezug (reine Bestandsabweichungen) bleiben sichtbar, solange der Bestand abweicht. Der
  Nachlauf dauert auf großen Datenbeständen etwa 10–15 Sekunden (Hintergrund); bis er fertig ist, filtert die Anzeige bereits.
* **Echtdaten (Kopie):** Nach Festschreiben und Ausgleich sanken die Aufgaben sofort von 34 auf 24 (offene Fälle 24 → 14);
  vorher blieben sie stehen, davon 10 Fälle ohne noch offenen Befund und 2 mit ausschließlich Archiv-Buchungen.

### Journal-Kennungen nach Wiederherstellung (0.102.0)

Neue Journal-Buchungen erhielten `Präfix + Zeilennummer`. Nach einer Wiederherstellung tragen Import-Buchungen bereits
Kennungen wie `PF-M-000002`; eine gleiche Journal-Kennung wurde beim Einblenden stillschweigend übersprungen – die neue
Buchung war unsichtbar. Das Journal vergibt jetzt nur noch Kennungen, die weder im Import noch im Journal vorkommen.

## Export und Wiederherstellung

`state.json` des Gesamtexports enthält `cutovers` (mit Freigaben je Konto) und `cutover_corrections`. Die
Wiederherstellung ist abwärtskompatibel (ältere Exporte kennen keine), idempotent (Stichtag + Zeitpunkt) und hält die
Regel „höchstens ein aktiver Stichtag“: ist bereits einer aktiv, kommt der übernommene als `undone` hinein.

## Reale Daten (Gesamtexport vom 05.11.2026, isolierte Kopie, kein Netz, nur Zählwerte)

| Messwert | Ergebnis |
| --- | --- |
| Wirksame Buchungen / Archiv / aktiv | 6020 / 5535 / 485 |
| Eröffnungslots zum 01.01.2025 | 241 |
| Pflichtprüfungen (beide Rechenweisen, 18 Prüfungen) | alle bestanden |
| Konten mit Eröffnungsstand | 30: 29 ohne Beleg, 1 mit Abweichung zum Beleg (DKB); 1 Vorschlag |
| Festschreiben | fehlerfrei; Bestände, Lots, Veräußerungen und Zahlungsströme **identisch** |
| Diagnose vorher / nachher | 110 Befunde → 75 offen + 35 Archiv (Summe unverändert) |
| Schutz | Archiv geschützt, aktiver Zeitraum frei, Journal-Schreiben vor dem Stichtag abgelehnt |
| Rücknahme | wirtschaftlich identisch zu vorher, 110 Befunde wie zuvor |
| Abgeleitete Vorschläge | 0 Vorschläge, 1 Hinweis (DKB A40XUX, Ursache unbekannt: 3 Buchungen im Zeitraum) |
| Laufzeit gesamt (Vorschau, Hinweise, Festschreiben, Diagnose, Rücknahme) | 8,4 s |
| **Ausgleich (0.102.0)** | 13 Zeilen (9 Zugänge, 4 Abgänge, davon 12 mit nicht aktuellem Abruf, 1 mit Verkauf ab Stichtag), alle 13 gebucht, danach 0 Abweichungen, Konten automatisch neu freigegeben, offene Diagnosebefunde 75 → 64; 24,5 s |
| **Ausgleich mit Gewichtung (0.103.0)** | 13 Zeilen: 5 Kleinstbeträge, 4 normal, 4 kritisch (DKB A40XUX mit Verkauf, RIO, XEN, PEAQ); Voreinstellung bucht 9 (4 Abgänge, 5 Zugänge, Zugänge zusammen ≈ 6,91 € Wert), lässt die 4 kritischen unberührt; eigener Zielbestand für eine kritische Zeile gebucht; Konten neu freigegeben; 21,8 s |

## Grenzen

* Die Eröffnungslots übernehmen die Anschaffungskosten der Daten. Die Seite prüft sie nicht gegen Belege; sie weist aus,
  wo ungenaue Kosten steuerlich wirken könnten (Verkauf binnen eines Jahres).
* Ein Befund, der Archiv- **und** aktive Buchungen mischt, bleibt sichtbar; seine Behebung kann am Schutz des Archivs
  scheitern (verständliche Meldung; Rücknahme des Stichtags hebt den Schutz auf).
* Abgeleitete Hinweise brauchen einen **frischen, vollständigen** Beleg (Abruf < 48 h ohne Lücke bzw. Referenzbestand).
  Ältere Abrufe führen zu keinem Hinweis; die Ursachenspalte bleibt eine Vermutung aus den Daten, keine Prüfung beim
  Anbieter.
* Die Kontenfreigabe ist eine ausdrückliche Bestätigung des **berechneten** Stands, kein externer Beweis.
* Keine steuerliche Beratung.

## Tests und Befehle

* `tests/test_cutover_fiat.py` (8), `test_cutover_core.py` (8), `test_cutover_page.py` (8), `test_cutover_fix.py` (18,
  einschließlich Export/Wiederherstellung), `test_cutover_scope.py` (6), `test_cutover_takeover.py` (2),
  `test_cutover_derive.py` (7), `test_cutover_settle.py` (14), `test_cutover_cases.py` (10).
* Zusätzlich 192 zufällige Kombinationen für die Gleichwertigkeit (Voll gegen Eröffnungsstand + aktiv) in beiden
  Rechenweisen.
* `TZ=Europe/Berlin python -m pytest -q tests` · `ruff check app tests scripts` ·
  `python -m app validate examples/beispiel-import.zip`.
