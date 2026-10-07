# Importprüfung und Daten-Reconciliation – Bestandsaufnahme, Plan, Umsetzung

Stand: 02.10.2026 · Version 0.17.0 · Branch `claude/portfolia-dashboard-s9p6zr`

Ziel: Die vorhandene Importprüfung (Prüf-Stapel für CSV-Importe und Datenquellen) wird zu einem
halbautomatischen Abgleich erweitert – Hunderte Vorgänge in wenigen Stapelaktionen statt einzeln, ohne
dass vorhandene Buchungen beschädigt, still überschrieben oder gelöscht werden. Erweitert wird die
bestehende Pipeline; nichts wird parallel neu gebaut.

---

## 1 · Bestandsaufnahme (Phase 0)

### Vorhandene Funktionen

| Bereich | Umsetzung (Datei) | Bewertung |
|---|---|---|
| Datenmodell | Import-ZIP (`tx`) → Overlay `tx_override` → App-Buchungen `journal_tx` → Sparpläne → Ledger. Ledger-Konvention: `from_qty` ohne Gebühr, Gebühr zusätzlich vom Gebührenkonto | stabil, wiederverwendbar |
| Staging | `csv_batch` (CSV-Datei bzw. Abruf einer Datenquelle, Rohdaten gz), `csv_row` (Zwischenformat `rec_json`, Buchungszeile `row_json`, Status, Entscheidung, Meldungen JSON) | Kern der Prüfung |
| Erkennung | `CsvImportService.evaluate` (`app/csvimport/service.py`): gleiche Kennung derselben Quelle, Ereignis bereits übernommen (versionierte Anbindungen), dauerhaft ignoriert, Abgleich über Tx-Hash (`reconcile.py`, voll/teilweise), Anbieter-IDs und Aliase (`_same_events`), gleiche Assets/Mengen/Zeit (`_duplicates`), Teil eines erfassten Transfers (`_covered_by_transfer`), gleiche Menge auf demselben Konto 36 h (`_same_qty`), rekonstruierte Buchung (`_reconstructed`), Transfer-Paare (`_transfers`), Gegenbuchung im kuratierten Import (Hinweis) | gute Detektoren, aber Ergebnis nur „neu / bekannt / mögliche Dublette“ |
| Entscheidungen | `csv_row.decision` (je Stapel), `event_decision` (dauerhaft ignorieren je Anbieter-Ereignis), `journal_event_alias` (weitere IDs je Buchung), `journal_import_link` (Import ↔ App-Buchung: abgedeckt/eigenständig), `diag_decision` (Diagnose-Korrekturen mit Vorher-Zustand und Rückgängig) | wiederverwendbar |
| Stapelfunktionen | „Alle Dubletten übernehmen/auslassen“ (`set_all`, nur je Status), Übernehmen aller gewählten Zeilen (`commit`, eine Transaktion), Rückgängig je Stapel (`revert`) | zu grob: kein Teilstapel, keine Vorschau der Wirkung, kein Protokoll je Aktion |
| Vergleich | `app/csvimport/compare.py` (0.16.3): neue Zeile ↔ vorhandene Buchung Feld für Feld | Anzeige; Grundlage der Bewertung |
| Bitpanda | `app/datasources/bitpanda.py` (0.16.2): dokumentierte Felder, `has_next_page`, Saldoverlauf (`_chain`) belegt Gebührenart, `/v1/portfolio`-Abgleich, Diagnose, Reparaturweg | Gebührenbeleg nur als Notiztext in der Zeile |
| Koinly | `KoinlyProfile` (`profiles.py`): Transfer = ein Vorgang mit Sende-/Empfangskonto, Gebühr separat, Hash, `koinly:<id>` | fasst technische Buchungen zu einem wirtschaftlichen Vorgang zusammen |
| Tests | ~430 Tests (u. a. strenger Bitpanda-Mock, Abgleich, Vergleich, Diagnose) | Basis für Regressionstests |

### Lücken

1. Kein Ergebnis-Typ je Zeile: „mögliche Dublette“ fasst sichere Dubletten, Ergänzungen, Widersprüche und
   1:n-Fälle zusammen → alles muss einzeln geprüft werden.
2. Keine Begründung mit Belegen und Abweichungen; die Sicherheit ist nicht nachvollziehbar.
3. Auslassen verwirft die Information der neuen Quelle (Börsen-Zeitpunkt, EUR-Wert, Original-ID); beim
   nächsten Abruf bzw. Re-Import muss erneut entschieden werden.
4. Stapelaktionen nur je Status, ohne Auswahl einzelner Zeilen, ohne Vorschau (Bestandswirkung) und ohne
   Protokoll/Rückgängig je Aktion.
5. Gebühren: Ob die Börse die Gebühr zusätzlich oder im Betrag belastet hat, steht nur im Notiztext; eine
   Auszahlung „Gebühr im Betrag“ (Netto ≠ Transferbetrag) wurde nicht als derselbe Vorgang erkannt → Gefahr
   einer zweiten Abbuchung.
6. Gleiche Menge/Zeit mit **verschiedenen** Tx-Hashes galt in `_duplicates` als mögliche Dublette.
7. Weitere Zeilen eines Ereignisses (z. B. Gebührenzeile), dessen Hauptzeile bereits vorhanden ist, galten
   als „neu“ → Gefahr doppelter Gebühren bei manueller Übernahme.
8. Keine Quellen- und Feldpriorität; keine Kennzeichnung, welche Quelle welchen Wert liefert.
9. Bitpanda: Vollständigkeit nur je Abruf (Pagination, Diagnose) – kein Bericht über Zeiträume, Brüche im
   Saldoverlauf und Buchungen ohne API-Gegenstück.

### Wiederverwendbare Bausteine

`evaluate` (alle Detektoren), `compare.Lookup`/`differences`, `reconcile.HashIndex`, `journal_event_alias`
(dauerhafte Wiedererkennung über IDs), `event_decision`, `commit` (validiert, eine Transaktion),
`unpair_in`/`revert` (Transfers auflösen), `diag_decision`-Muster (Vorher-Zustand je Änderung), Bitpanda
`_chain`/Diagnose/`coverage_json`.

### Risiken für Datenintegrität und Migration

| Risiko | Gegenmaßnahme |
|---|---|
| Unsichere Automatik bucht doppelt oder lässt Neues aus | Stapelaktionen nur auf Vorschlag mit Vorschau; „übernehmen“ nur für Ergebnis „neu“ ohne Prüfhinweis; Widersprüche/komplexe Fälle nie per Stapel übernehmen |
| Überschreiben vorhandener Buchungen | Verknüpfen statt Zusammenführen: vorhandene Buchung bleibt unverändert, Angaben der neuen Quelle werden als verknüpfter Quelldatensatz mit Herkunft gespeichert |
| Teilfehler | jede Stapelaktion in **einer** DB-Transaktion (Übernehmen inklusive); bei Fehler keine Änderung |
| Doppelte Ausführung (Doppelklick, zweiter Tab) | Aktions-Token aus der Vorschau; bereits ausgeführte Token → keine zweite Ausführung |
| Rückgängig | Vorher-Zustand je Zeile/Alias/Entscheidung/Buchung im Aktionsprotokoll; nur Unverändertes wird zurückgesetzt |
| Schemaänderung | nur additive Migration 13 (zwei neue Tabellen, keine Änderung bestehender Tabellen oder Daten); ältere Versionen ignorieren sie (Migrationen ≤ `user_version` werden übersprungen) |
| Produktivdaten | Entwicklung und Tests ausschließlich mit synthetischen Daten; keine automatische Bereinigung – historische Probleme erscheinen als Korrekturvorschlag |

---

## 2 · Datenmodell (Migration 13, additiv)

```sql
CREATE TABLE import_action (   -- Stapelaktionen der Importprüfung (Protokoll, Rückgängig)
  id, token UNIQUE, batch_id, action, label, params_json, ops_json, summary_json,
  status ('active'|'undone'), created_at, undone_at)
CREATE TABLE tx_link (         -- verknüpfte Quelldatensätze je Buchung (Herkunft, keine Zusammenführung)
  id, tx_id, source, ext_id, event_key, role, batch_id, row_idx, action_id,
  record_json, assessment_json, status ('active'|'undone'), created_at, undone_at)
```

* `tx_link` hält die Werte der verknüpften Quelle (Zeitpunkt, Art, Beine, Gebühr inkl. Gebührenbeleg,
  EUR-Wert, Hash, IDs, Rohangaben) und die Bewertung zum Zeitpunkt der Verknüpfung – unabhängig vom
  Prüf-Stapel (Stapel dürfen später verworfen werden, die Herkunft bleibt). Gehört zum Gesamtexport.
* Wiedererkennung künftiger Abrufe/Importe über `journal_event_alias` (Zeilenkennung `row:<quelle>|<id>`,
  bei eindeutigen 1:1-Ereignissen zusätzlich die Ereignis-IDs).
* Bewertung je Prüfzeile in `csv_row.messages` (JSON, Schlüssel `match`) – keine Schemaänderung.
* Neuer Zeilenstatus `linked` („verknüpft“): erledigt, nicht gebucht, `tx_id` = vorhandene Buchung.

Rücknahme: Tabellen können entfallen, ohne bestehende Daten zu berühren; eine ältere Version arbeitet mit
der Datenbank weiter (neue Tabellen werden ignoriert). Zeilen „verknüpft“ gelten dort wieder als offen und werden
neu bewertet (typisch: mögliche Dublette, Vorschlag „nicht übernehmen“); nach erneutem Update erkennt Portfolia sie
über die gespeicherte Zeilenkennung wieder als verknüpft. Buchungen sind in keinem Fall betroffen.

---

## 3 · Plan und Umsetzungsstand

| # | Schritt | Stand 0.17.0 |
|---|---|---|
| 1 | Bewertung je Zeile: Ergebnis, Sicherheit, Belege, Abweichungen, Ergänzungen, Gebührenprüfung, Rolle (1:1, Transferseite, Teil) | umgesetzt (`app/csvimport/assess.py`) |
| 2 | Erkennung schärfen: Brutto/Netto-Gebühr, verschiedene Hashes ≠ Dublette, Ereignis-Geschwister | umgesetzt |
| 3 | Stapelaktionen: Auswahl (Seite/alle gefilterten/Gruppe, einzeln abwählbar), Filter, Vorschau mit Bestandswirkung und Ausschlüssen, transaktionale idempotente Ausführung, Protokoll, Rückgängig | umgesetzt (`app/csvimport/batch.py`) |
| 4 | Verknüpfen statt verwerfen (Ergänzungen bleiben mit Herkunft erhalten) | umgesetzt |
| 5 | Quellenpriorität: Bearbeitungsreihenfolge, feldbezogener Vorrang, Herkunftskennzeichnung | umgesetzt (Vorschlag, nie automatisch überschrieben) |
| 6 | Bitpanda-Vollständigkeit: Bericht „nachgewiesen / plausibel / nicht verifizierbar“ | umgesetzt (`app/datasources/quality.py`) |
| 7 | Feldweises Übernehmen einzelner Werte in die vorhandene Buchung | **offen** – bewusst nicht automatisch; heute über „Bearbeiten“ anhand des Korrekturvorschlags |
| 8 | Transferseite bei verzögerter Auszahlung bzw. anderem Kontonamen – im Prüf-Stapel und für bereits gebuchte App-Buchungen (0.17.1) | umgesetzt (`app/csvimport/transfer_side.py`, Abschnitt 5) |
| 9 | Mehrere Datenquellen desselben Anbieters (z. B. zwei Bitcoin-Wallets, neu angelegte Quelle): Hash-Abgleich auch gegen Buchungen der anderen Quelle – gleiche Seite **und** Menge → mögliche Dublette; Transfer A → B bzw. gemeinsame Ausgabe bleiben getrennt. Behoben: scheinbare UUID aus Transaktions-Hashes (Fehltreffer „bereits vorhanden“) (0.18.0) | umgesetzt (`app/csvimport/service._same_events`, `events.identity_keys`) |

Details zu Regeln, Grenzen und Tests: README, Abschnitte „Importprüfung: Abgleich je Zeile, Stapelaktionen,
Verknüpfen“ und „Vollständigkeit der Historie“; Meilenstein M19 in `docs/MILESTONES.md`.

---

## 4 · Annahmen, Grenzen, offene Punkte

* **Nur synthetische Tests.** Der Regressionsfall ist strukturgleich nachgebildet (gleiche Konstellation, andere
  Zahlen), damit keine echten Buchungsdaten ins Repository gelangen; mit den Originalwerten der Aufgabenbeschreibung
  wurde er nur lokal geprüft. Echte Exporte bzw. API-Antworten wurden für Tests und Doku nicht verwendet.
* **Neubewertung nach dem Update.** Offene Prüf-Stapel werden beim Öffnen neu bewertet (Auswertungsstand 5). Status
  können sich dabei ändern – z. B. gelten verschiedene Blockchain-Transaktionen nicht mehr als Dublette, Gebühren-
  zeilen bereits vorhandener Vorgänge dagegen schon. Eigene Entscheidungen (ja/nein, Werte, Transfer-Bestätigungen)
  bleiben; gebucht wird nur über „Übernehmen“ bzw. eine eingeschaltete automatische Übernahme.
* **Koinly-Gebührenkonvention nicht belegt.** Ob Koinly die Gebühr zusätzlich zum gesendeten Betrag führt, ist hier
  nicht verifiziert. Geprüft wird deshalb die Wirkung der vorhandenen Buchung im Ledger (Abgang + Gebühr im selben
  Asset) gegen den Beleg der Quelle.
* **Sicherheit qualitativ.** Die Stufen sind feste, nachvollziehbare Regeln (Belege/Abweichungen werden angezeigt),
  keine kalibrierten Wahrscheinlichkeiten. Ganzstündiger Zeitversatz und runde Mengen ohne Kennung bleiben „mittel“.
* **Feldweises Übernehmen** von Werten der neuen Quelle in die vorhandene Buchung ist bewusst nicht automatisiert.
* **Vollständigkeit** ist erst nach einem vollständigen Neuabruf mit 0.17.0 beurteilbar; vor dem ersten
  API-Vorgang ist nichts prüfbar; „plausible Lücken“ sind Hinweise, keine Beweise.
* **Rücknahme der Migration:** Die Tabellen `import_action` und `tx_link` können entfallen; Zeilen mit Status
  `linked` gelten in älteren Versionen wieder als offen (neu bewertet), nach erneutem Update wieder als verknüpft.
  Buchungen sind nicht betroffen.

---

## 5 · Transferseiten: verzögerte Auszahlung, anderer Kontoname (0.17.1)

**Fall.** Kuratierter Import: Transfer „Börse → Wallet“ (Zeitpunkt der Auszahlung, Wallet-Name des
Steuertools, kein Hash, Notiz mit dem Zeitpunkt der Gutschrift). Wallet-Datenquelle unter eigenem Kontonamen:
Zugang derselben Menge mehr als einen Tag später. Bis 0.17.0 wurde der Zugang als neu übernommen (mit automatischer
Übernahme still) – die Menge zählte doppelt, und als Zugang hätte sie einen neuen Einstand begonnen. Ursache war
nicht die Verzögerung (im Fenster von 72 h), sondern die Bedingung „gleiches Konto“; der Journal-Abgleich verglich
nur gleiche Arten.

**Regeln** (ein Modul für alle Stellen, `app/csvimport/transfer_side.py`):

| | gleiches Konto | anderer Kontoname |
|---|---|---|
| Menge | ± 0,5 %, Gebühr netto/brutto | exakt (Rundung der Quellen: 10⁻⁶ relativ, mind. 10⁻⁸) |
| Zugang | −2 h … +72 h; exakt und ≥ 6 signifikante Stellen: bis +7 Tage | ebenso |
| Abgang | ± 2 h | ± 2 h |
| nie | verschiedene Hashes; Erträge/Einordnungen; Paar-Transfers der App (PF-T) | zusätzlich Fiat, Zugang auf dem Absenderkonto, Zielkonto von einer anderen Datenquelle geführt |

Je Transferseite höchstens ein Treffer (gleiches Konto vor anderem, exakt vor ungefähr, dann nächster Zeitpunkt);
bereits als „Import-Buchung gilt“ entschiedene Seiten sind vergeben, „keine Dublette“ schließt das Paar aus.

**Wo es wirkt.**

| Stelle | Wirkung | Entscheidung |
|---|---|---|
| Prüf-Stapel (CSV, Datenquelle) | mögliche Dublette, nie automatisch übernommen; Abgleich „Widerspruch: Konto“, Belege (Notiz-Zeitpunkt), Korrekturvorschläge | „verknüpfen“ (nicht buchen) oder ausdrücklich übernehmen |
| Buchungsliste | „Transferseite?“ mit Gegenüberstellung | *Import-Transfer gilt* / *Keine Dublette* |
| Abgleich mit dem Import | Kandidat mit Begründung | wie bisher, gespeichert in `journal_import_link` |
| Datenqualität | Befund „Zugang … doppelt?“ mit Szenario | „im Import enthalten“ mit Vorschau, Übernehmen, Rückgängig |
| Datenquelle | „Konto laut Abgleich“ (Belege: Hash und Transferseiten) | Umstellung per Klick ohne Neuabruf, zurücknehmbar |

**Annahmen und Grenzen.**

* Ohne gemeinsamen Hash ist die Zuordnung ein begründeter Verdacht; deshalb nie automatisch. Lokal gegen den echten
  Export geprüft (nicht im Repository): der gemeldete Fall (App-Zugang nach dem Screenshot nachgestellt) wird
  gefunden, unter rund 5 000 Zu-/Abgängen des Imports
  kein Zufallstreffer – auch nicht, wenn jeder als „anderes Konto“ geprüft wird.
* „Import-Transfer gilt“ nimmt die App-Buchung aus der Rechnung, verschiebt aber nichts: Heißt dasselbe Wallet in
  Import und Datenquelle verschieden, liegen spätere Bewegungen der Datenquelle weiter auf deren Konto (Bestand je
  Konto aufgeteilt, ggf. negativ). Konten angleichen: Konto der Datenquelle umstellen (neue Vorgänge), gebuchte
  App-Buchungen einzeln bearbeiten oder im kuratierten Import vereinheitlichen. Ein Werkzeug zum Zusammenführen von
  Konten gibt es nicht.
* Die automatische Konto-Umstellung bleibt an den Hash-Abgleich gebunden (≥ 3 Treffer, ≥ 90 %, Konto ohne
  Buchungen); Transferseiten sind nur Belege für den Vorschlag.
* Nicht erkannt: Gutschriften später als 7 Tage, Teilgutschriften (andere Menge, z. B. Netzwerkgebühr beim
  Empfänger abgezogen, aber nicht im Import geführt), runde Mengen nach mehr als 72 h.
* Auswertungsstand 6: offene Prüf-Stapel werden beim Öffnen und vor jeder automatischen Übernahme neu bewertet;
  Status können sich dabei ändern (Zugang → mögliche Dublette). Keine Migration.
