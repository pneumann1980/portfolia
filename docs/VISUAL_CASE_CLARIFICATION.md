# Visuelle Fallklärung mit benannten Konten und Wallets

Die Fallseite der Konfliktklärung (`/quality/klaerung/{fall}`) zeigt den Geldweg eines Falls **als benannte Konten und
Wallets mit Verbindungen** statt als Tabelle von Kennungen. Die Erweiterung ist eine **Darstellung**: Sie bildet keinen
neuen Graphen, keine zweite Buchungslogik, keine zweite Warteschlange und keine zweite Simulation. Alle Aussagen kommen
aus dem vorhandenen Fall (`clarify.Case`), dem Bericht (`Report`), den Quellenanhängen (`csvimport.groups`) und der
gemeinsamen Simulation (`clarify.Hyp`).

## Bausteine (wiederverwendet bzw. neu)

| Baustein | Datei | Rolle |
|---|---|---|
| Darstellungsadapter (neu, nur Lesen) | `app/diagnosis/case_view.py` | `build()` → `CaseView`: Orte, Verbindungen, Fragen, nächste Handlung, getrennte Datenlage, Wirkung |
| Geldweg-Template (neu) | `app/templates/partials/klaerung_weg.html` | Kopf, nächster Schritt, Simulation, Orte, Verbindungen, Legende, Tabelle, Fragen, Datenlage |
| Bestandsfenster im Fall (neu, Wiederverwendung) | `partials/klaerung_fenster.html` | `windows_web._rows` + `charts.js` `devwindows`, nur fallrelevante Fenster |
| Recherche-Plan (herausgelöst) | `partials/win_plan.html` | vorher inline in `diagnosis_windows.html`; jetzt gemeinsam, Rücksprung in den Fall (`back`) |
| Route | `app/diagnosis/clarify_web.py` | `case_page(hyp, sel)`; Auswahl und Simulation sind Links ohne Skript |
| Stile | `app/static/css/app.css` (`.vk-*`) | Linienarten, Marken, Mobil, Dunkelmodus, Windows-Kontrast |

Unverändert genutzt: `clarify.analyse/evaluate/decide`, `Hyp`-Simulation, `actions.preview/apply/undo` über
`clarify.apply_hypothesis`, `stockconfirm`, `research.plan_for/start/cancel`, `references`, `derivatives`,
Datenschutz-CSS (`sens-real`/`sens-mask`), Vollexport (keine neue Tabelle, **keine Migration**).

## Was die Ansicht zeigt – und was nicht

* **Orte**: Konto bzw. Wallet als fester Anker mit Marke (A, B, C …). Der Anzeigename steht immer da; Kennungen
  (interne Kontokennung, vollständige Adresse, Hash, Quell-ID) nur in den Details. Anbieter, Netzwerk, Unterkonto/Umfang
  erscheinen nur, soweit **belegt** (Datenquelle bzw. Bestandsnachweis, mit Hinweis auf die Grundlage); sonst „nicht
  bekannt“. Gleichlautende Anzeigenamen getrennter Konten werden durch ein Unterscheidungsmerkmal getrennt gehalten.
* **Unbekanntes Gegenkonto**: je Vorgang ein eigener Ort „Gegenkonto nicht bekannt“ bzw. „Herkunft nicht bekannt“ (`?`).
  Daraus folgt weder eine eigene Wallet noch eine externe Auszahlung; mehrere „?“ sind nicht dieselbe Gegenpartei.
* **Verbindungen**: durchgezogen = gebucht; gestrichelt = Vorschlag bzw. neu in der Simulation (nicht gebucht, mit
  Text); durchgestrichen = entfällt in der Simulation; gepunktet = offen. Der Zustand steht immer auch als Text und
  Symbol (✓ ◌ ✕ + ?), nie nur als Farbe.
* **Tausch/Kauf/Verkauf im selben Konto** (aus den echten Daten) ist kein Weg „von A nach A“: ein Ort, beide Seiten des
  Vorgangs, Hinweis „innerhalb dieses Kontos“. Sammelbezeichnungen des kuratierten Imports („Andere Börse“, „… unbekannt“)
  gelten nicht als belegter Anbieter.
* Große Fälle: die ersten 8 Verbindungen sind offen, der Rest steht in einem aufklappbaren Block; Simulationen ab Nr. 7
  sind eingeklappt.
* **Konkurrierende Zuordnungen** (z. B. ein Abgang, zwei mögliche Zugänge) erscheinen als „mögliche Zuordnung n von m“;
  Portfolia wählt keine aus. Begründungen sind konkret (gleicher/fehlender/abweichender Hash, Mengendifferenz,
  Zeitabstand, Konkurrenz) – keine Prozent-Sicherheit.
* **Gebühr**: „keine Gebühr geliefert“ ≠ „Gebühr 0 (belegt)“ ≠ „in der Differenz Abgang − Eingang enthalten (nicht als
  Gebühr belegt)“ ≠ „zusätzlich gebucht, belastet <Konto>“.
* **Quellen** sind Belege (Quelle, Rolle, Kennung, Importlauf), keine zusätzlichen Geldbewegungen; Widersprüche
  (z. B. Brutto/Netto, Mengenwiderspruch) stehen an der Verbindung. Offene **Quellenrevisionen** (gleiche Kennung,
  geänderter Inhalt) werden als Warnung gezeigt; die bestehende Buchung bleibt unverändert.
* **Bestände** je Ort mit Zeitpunkt, Herkunft und Umfang (Ledger „jetzt“, Bestand laut Quelle zum Abrufzeitpunkt,
  Nachweis des Nutzers mit Umfang/Unterkonto). Fehlt ein Nachweis, steht „unbekannt, nicht 0“. Es werden nie Bestände
  verschiedener Zeitpunkte als unmittelbare Abweichung verglichen; der Ledger-Wert zum selben Zeitpunkt steht daneben.
* **Datenlage getrennt**: Datenqualität (offene Dimensionen), Importabdeckung, offene Konflikte – drei Aussagen. Ein
  passender Endbestand schließt Fehler der Historie nicht aus.
* **Derivate**: das vorhandene Dossier; Margin ist gebundene Sicherheit, kein Handelsverlust; keine offenen Positionen
  aus Margin oder Bestandsdifferenz erschlossen; keine Anbietersemantik behauptet (die Liste verifizierter Formate ist
  leer).
* **Ein nächster Schritt** ist hervorgehoben (Beleg vergleichen / Bestand bestätigen / Lösung übernehmen / …) mit
  Begründung und Art (automatisch möglich · braucht Ihre Bestätigung · braucht weitere Information).

## Simulation, Wirkung, Übernahme

`?hyp=<id>` zeigt die **gemeinsame Simulation** dieser Hypothese: Kopf „Simulation – nichts davon ist gebucht“,
durchgestrichene und gestrichelte Verbindungen. Der Block „Wirkung dieser Simulation“ stammt aus `Hyp` (Bestände vorher
→ nachher, realisiertes Ergebnis/Einkünfte je Jahr, Anschaffungsdaten, Erfüllung der Nachweise, erledigte bzw. neu
entstehende Befunde, Zulässigkeit). **Kursstand:** Bewertung nur mit dem gespeicherten Kurs (Quelle, Zeitpunkt,
„veraltet“), vorher und nachher mit demselben Kurs, kein Abruf; ohne Kurs „nicht bewertbar“, nie 0. Nicht zulässige
Simulationen zeigen die Gründe und bieten keine Übernahme an.

Übernommen wird ausschließlich über `clarify.apply_hypothesis` (unter Sperre neu gebildet; Datenstand, Plan-Token und
Entscheidung werden geprüft; atomar, Nachprüfung, gemeinsames Rückgängig). Die Ansicht selbst schreibt nie.

## Lesender Seitenaufruf

Beim Aufruf der Fallseite oder der Fensterseite wird nichts geschrieben. Die frühere Bereinigung hängengebliebener
Recherche-Läufe beim Anzeigen der Fensterseite (`research._stale`) wurde getrennt: `research.stale_ids`/`runs(live=True)`
berechnen die Anzeige (hängender Lauf = „abgebrochen“), `research.reconcile_stale` schreibt nur aus Schreibvorgängen
(`research.start`).

## Korrigierte Fehlerbilder (Vorarbeit, mit Tests)

1. Transferpaare nur noch mit belegter Identität (verifizierter, eindeutiger Hash); Betrag/Zeit allein = Vorschlag
   mit ausdrücklicher Bestätigung; abweichende Hashes schließen die Paarung aus.
2. Beide Transfergebühren bleiben erhalten (Abgangsgebühr am Transfer, Empfängergebühr als eigene Buchung); Rückgängig
   stellt beide her.
3. Geänderte Quellenzeile mit gleicher Kennung = offener Konflikt (Revision), keine stille Übernahme, kein Überschreiben.
4. Datenstand umfasst Nachweise, Zuordnungen, Einstellungen und wird in der Schreibtransaktion erneut geprüft.
5. **Abdeckung**: Der Zeitraum zwischen erstem und letztem Vorgang einer Datei gilt nicht mehr als „abgedeckt“ (kein
   Nachweis des Exportzeitraums), sondern als eigener Zustand „innerhalb der Dateispanne, ohne Vorgang“; Lücken
   zwischen Dateien bleiben Lücken.

## Prüfung gegen echte Daten (Export vom 17.10.2026, Wegwerf-Kopie, nur lesend)

8 Fälle, 25 Ansichten (aktueller Stand und Simulationen) ohne Fehler; die Datenbank blieb bei Ansicht und Seitenaufruf
bitgleich. Aufgefallen und behoben: Tausch im selben Konto wurde als Pfeil „A → A“ gezeichnet; „Andere Börse“ erschien als
Anbieter; sehr lange Fälle (28 Buchungen, 14 Simulationen) waren unübersichtlich. Offen: die Seitenladezeit großer Fälle
(bis ca. 18 s) kommt aus der bestehenden Fallanalyse (`clarify.analyse` mit vielen Hypothesen), nicht aus der Ansicht
(Adapter < 0,2 s je Ansicht).

## Grenzen

* **Brücken:** Verschiedene verifizierte Hashes schließen eine Paarung aus – außer eine der beiden Buchungen nennt in der
  Notiz den Hash der Gegenbuchung (konkreter Bridge-Beleg). Dann entsteht ein Kandidat („mittel“, „Bridge-Beleg“), der
  nie automatisch angenommen wird. Das Wort „Bridge“, Betrag oder Zeit allein begründen nichts; Asset-Wechsel
  (Wrapped/Migration) werden weiterhin nicht gleichgesetzt.
* Anbieter, Netzwerk und Unterkonto werden nicht geschlussfolgert (eine Wallet-Anwendung legt das Netzwerk nicht
  fest); stehen sie nicht in Datenquelle oder Nachweis, bleiben sie „nicht bekannt“.
* Die Auswahl eines Orts/einer Verbindung lädt die Seite neu (Link, ohne Skript); bewusst kein clientseitiger Graph.
* Gezeigt werden höchstens 24 Vorgänge je Fall (Rest ausdrücklich ausgewiesen) und 6 Positionen im Fensterbereich.
* PubFi-Fenstersemantik, Robinhood-Blockscout u. Ä. sind von dieser Erweiterung unabhängig und unverändert.
