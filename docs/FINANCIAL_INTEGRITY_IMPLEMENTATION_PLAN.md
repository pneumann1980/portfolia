# Financial Integrity – Implementierungsplan

Zentrale Steuerungsdatei des Auftrags „Prüfworkflow, Universalimport, Reconciliation und historische
Vollständigkeit“. Wird nach jeder Phase fortgeschrieben (Status, Commit, Tests).

* **Ausgangscommit:** `ebc06dc` (0.26.0, Branch `claude/portfolia-dashboard-s9p6zr`, CI grün)
* **Statuswerte:** Nicht begonnen · In Bearbeitung · Implementiert · Tests bestanden · Abgenommen · Blockiert
* **Grundsatz:** bestehende Dienste erweitern (CSV-Pipeline, Datenquellen, Diagnose, Korrektur-Engine, Ledger) – keine
  zweite Import-, Ledger-, FIFO-, Steuer- oder Matching-Engine. Tests nur mit synthetischen Daten in temporären
  Datenbanken; reale Exporte nur lesend.

## 1 Architektur-Bestandsaufnahme (verifiziert am Code)

| Bereich | Bestand | Kernfunktionen |
|---|---|---|
| Importpipeline | `app/csvimport/service.py` (Profile → `Rec` → `evaluate()` → `assess_rows()` → `commit()`), Stapel `csv_batch`/`csv_row` | `ingest`, `evaluate`, `commit_plan`/`commit_apply`, `refresh_status`, `set_ignored`, `revert`, `reopen` |
| Stapelaktionen | `app/csvimport/batch.py` | `group_of`, `overview`, `Filters`, `selection`, `plan`, `execute`, `safe_link_rows`, `auto_link`, `undo` (`import_action`) |
| Abgleich | `assess.py` (Ergebnis/Sicherheit je Zeile), `reconcile.py` (Hash), `transfer_side.py`, `compare.py` | `assess`, `_competing`, `is_safe` |
| Datenquellen | `app/datasources/service.py`, `overview.py`, `web.py` | `open_counts`, `pending_batches`, `holdings`, `research_fetch`, `_present`, `_process` |
| Diagnose | `app/diagnosis/*` (engine, audit, windows, research, stock, actions, recommend) | `report_for`, `case_states`, `active_marks`, `fingerprint`, `build_plan`/`apply`/`undo` |
| Ledger/Steuer | `app/ledger/engine.py` (FIFO-Lots), `app/tax/` | `run_ledger`, Lots mit `acq_date`, `cost` |
| Dokumentbelege | `app/documentimport/` (PDF/Bild → `Rec` → Prüf-Stapel) | `documentimport.service` |

Migrationen bis Version 21 (`app/db.py`); Gesamtexport/Restore `app/fullexport.py` (`state.json`).

## 2 Bestätigte Fehlerursachen (Phase 1, P0)

Die Hypothesen des Auftrags wurden am Code geprüft. Bestätigt:

1. **Sieben verschiedene Definitionen von „offen“.** `RowCtx.open` (nur gebucht/verknüpft gilt als erledigt),
   `group_of` (schließt `known`/`ignored`/`before` aus), `overview()`, `DatasourceService.open_counts` (zählt
   `before` und ausgelassene Zeilen als offen), `refresh_status` (verlangt eine Entscheidung auch für `before`),
   `_review_queue` (ignoriert Entscheidungen), der Verwerf-Check in `DatasourceService._process` (zählt `before` als
   offen). Folge: Datenquelle meldet „57 vor Stichtag offen“, der Stapel „0 zu entscheiden“ – beides aus denselben
   Zeilen.
2. **Stapel schließen nie.** `refresh_status` behandelt nur Sync-Stapel im Zustand `partial`. Ein Stapel, dessen
   Zeilen nur verknüpft, ausgelassen oder als vorhanden erkannt wurden, bleibt dauerhaft `preview`; Zeilen vor dem
   Stichtag halten auch `partial`-Stapel für immer offen. CSV-Stapel dagegen werden beim Übernehmen sofort
   `committed`, auch wenn ungeklärte Zeilen übrig sind (verschwinden ungeprüft).
3. **„Jetzt prüfen“ öffnet nur den jüngsten Stapel** (`pending[0]`), die Zähler summieren aber über alle Stapel der
   Quelle → im geöffneten Stapel stehen oft 0 offene Fälle (Bitpanda: offene Fälle in älteren Stapeln).
4. **Ungeklärte/unvollständige Zeilen lassen sich nicht abschließen:** „Auslassen“ ist nur für `new`/`duplicate`
   erlaubt; „dauerhaft ignorieren“ nur für Datenquellen. Es gibt kein „zurückstellen“.
5. **Nullanzeige ohne Grund:** Die Übersicht zeigt 0/0/0/0, ohne zu sagen, ob alles vorhanden, vor dem Stichtag, nicht
   auswertbar oder noch nicht ausgewertet ist.
6. **Zeilen vor dem Stichtag sind per Auswahl „verknüpfbar“** (`_decide` erlaubt `link` für `before`) – „alle
   gefilterten auswählen“ ergibt so z. B. 79 „ausführbare Aktionen“ für bereits maßgeblich importierte Vorgänge.
7. **BNB-Bestandsabweichungen kehren zurück:** (a) die Datenquellenseite (`holdings()`/`data_notes()`) kennt die
   Entscheidungen der Diagnose gar nicht; (b) der Diagnose-Befund trägt den Abrufzustand im Schlüssel
   (`holding|Konto|Asset|extern_diff` vs. `…|extern_unsicher`) und in der Prüfsumme – wird der Abruf älter als 48 h,
   entsteht ein „neuer“ Befund ohne Entscheidung, obwohl sich die Zahlen nicht geändert haben.
8. **Wiederholter Abruf legt erledigte Vorgänge erneut vor:** `_present()` kennt nur offene Stapel. Schließt ein Stapel
   korrekt, würden ausgelassene bzw. vor-Stichtag-Ereignisse beim nächsten überlappenden Abruf neu abgelegt.

Nicht bestätigt: Fehler im Matching selbst (Kandidaten werden gefunden; sie verschwinden in der Zähl-/Statuslogik),
veraltete Auswahl (wird gegen vorhandene Zeilen geschnitten), Vorschau auf anderem Datenstand (Fingerprint-Prüfung).

## 3 Gap-Analyse

| Anforderung | Vorhanden | Teilweise | Fehlerhaft | Fehlt | Maßnahme |
|---|---|---|---|---|---|
| Stapelabschluss | | | ✓ (2) | | zentrale Ableitung `review.batch_state`, `refresh_status` beidseitig, Status `done` |
| Einheitliche Zähler | | | ✓ (1, 3) | | `app/csvimport/review.py` als einzige Quelle; Zähleinheit „Vorgänge“ (Ereignis) |
| Nullgründe/Funnel | | ✓ (Summary) | | ✓ | `review.funnel()` + Begründung je Nullanzeige |
| Manuelle Prüfung erreicht alle Fälle | | ✓ | ✓ (4) | | Abschlussentscheidungen „nicht übernehmen“, „zurückstellen“ für alle offenen Zustände |
| Zentrale Prüfwarteschlange | | | | ✓ | `/journal/pruefung` (Phase 1 Grundgerüst, Phase 2 Sammelverarbeitung) |
| Sammelverarbeitung über Stapel | ✓ je Stapel | | | ✓ stapelübergreifend | Phase 2: Vorschau je Stapel, Ausführung nacheinander, Teilerfolge sichtbar |
| BNB-Abschlüsse | | | ✓ (7) | | statusunabhängiger Befundschlüssel, Evidenz-Prüfsumme, Altentscheidungen anerkennen, Datenquellenseite nutzt sie |
| Bestandsklärung ohne Ledger-Position | | ✓ | | | Phase 3: Positionsmenge = Ledger ∪ Referenzen ∪ eindeutige externe Bestände |
| Quellen- vs. Reconciliation-Evidenz | | ✓ (eine Skala) | | | Phase 3: zwei getrennte Bewertungen |
| Mehrbein-Freigabe | | ✓ (Transferpaare) | | | Phase 3: Ereignis-weite `book_row` |
| Universeller Börsenimport | ✓ CSV-Profile (≈40), Mapping | ✓ | | XLSX, ZIP, Börse ohne API als Quelle | Phase 4 |
| Historische Abdeckung | ✓ `quality.report` je Quelle | ✓ | | Matrix je Produktbereich/Zeitraum | Phase 5 |
| Ereignisgruppen | ✓ `tx_link`, Aliase, Transfer-Merge | ✓ | | Gruppensicht über Quellen | Phase 6 |
| Futures/Funding | | | | ✓ | Phase 7 (nur verifizierte Formate) |
| Lot-Herkunft | ✓ Lots mit `acq_tx` | ✓ | | Herkunftsklasse je Lot | Phase 8 |
| Vollständigkeitscockpit | ✓ Diagnose-Bereiche | ✓ | | getrennte Qualitätsdimensionen | Phase 9 |

## 4 Phasen

| # | Phase | Status | Commit | Tests |
|---|---|---|---|---|
| 1 | P0 Prüfworkflow reparieren | Tests bestanden | b88e1f5 | 15 neu, Suite 929 grün |
| 2 | Zentrale Prüfung, Sammelverarbeitung | Tests bestanden | 204b984 | 5 neu, Suite 934 grün |
| 3 | M29-Hardening | Tests bestanden | b0b3bbe | 6 neu, Suite 940 grün |
| 4 | Universeller Börsenimport | Tests bestanden | c28b702 | 13 neu, Suite 953 grün |
| 5 | Historische Quellenabdeckung | Tests bestanden | d94c6dd, Korrektur Phase 10 | 5 neu |
| 6 | Wirtschaftliche Ereignisgruppen | Tests bestanden | 30c0f5c, Korrektur Phase 10 | 4 neu |
| 7 | Futures, Funding, Margin | Tests bestanden (nur geprüfte Formate) | b6d0491 | 3 neu |
| 8 | Anschaffungskosten, FIFO-Herkunft | Tests bestanden | 5f82d82, 6e825bd | 5 neu |
| 9 | Vollständigkeitscockpit | Tests bestanden | d40296d, Korrektur Phase 10 | 4 neu |
| 10 | Gesamtregression, Release | Tests bestanden | siehe Bericht §16.6/16.7 | 9 neu (Regression, Massendaten, Export/Restore) |

### Phase 1 – Prüfworkflow (P0)

* **Module:** neu `app/csvimport/review.py`; `service.py` (`refresh_status`, `commit`, `RowCtx.include`), `batch.py`
  (`group_of`, `overview`, `_decide`), `module.py` (Stapelseite, Prüfwarteschlange), `datasources/service.py`
  (`open_counts`, `_present`, Verwerf-Check), `datasources/web.py`/Templates („Jetzt prüfen“), `diagnosis/engine.py`
  (Befundschlüssel Bestand), `diagnosis/actions.py` (Evidenz-Prüfsumme), `diagnosis/audit.py` (`case_states`).
* **Migration:** keine (neuer Stapelstatus `done` ist ein Wert in `csv_batch.status`; Zeilenentscheidung `defer` in
  `csv_row.decision`).
* **Zustände je Vorgang:** offen · sicherer Vorschlag · vorgemerkt · zurückgestellt · bereits berücksichtigt ·
  verknüpft · übernommen · nicht übernommen · ignoriert · vor Stichtag (Historie). Abgeschlossen = keiner der vier
  ersten Zustände. Zähleinheit: wirtschaftlicher Vorgang (Ereignis-ID bzw. Zeile ohne Ereignis-ID).
* **Risiken:** Stapel schließen nun auch ohne Übernahme → `_present()` muss geschlossene Stapel berücksichtigen, sonst
  legt ein überlappender Abruf Vorgänge erneut vor; Altentscheidungen zu Bestandsbefunden müssen weiter gelten.
* **Abnahme:** 15 Pflichttests (siehe Auftrag 4.6) + bestehende Suite grün.
* **Zusätzlich umgesetzt:** einmalige, protokollierte Angleichung bestehender Stapel beim Start
  (`CsvImportService.reconcile_statuses`, App-Zustand `review.state_v`); Übernahme ausdrücklicher „nicht übernehmen“-
  Entscheidungen für unveränderte Zeilen erneut hochgeladener Dateien (`_carry_decisions`); Prüfsumme der
  Bestandsbefunde mit normalisierten Zahlen (`_fp_num`).

### Phase 2 – Zentrale Prüfung und Sammelverarbeitung
`/journal/pruefung` mit Reitern (Zu prüfen · Sicher verarbeiten · Abgeschlossen · Historie), Filter (Quelle, Konto,
Art, Evidenz, Zeitraum), Anzeige je Vorgang, Vorauswahl nur sicherer Vorschläge (Abwahl bleibt), „Sichere Vorschläge
gesammelt verarbeiten“ über mehrere Stapel (je Stapel atomar, nacheinander, Teilerfolge sichtbar), Lösungsvarianten im
manuellen Fall. Abnahme: HTTP-End-to-End-Tests automatisch/manuell/Sammel/Teilabschluss.

### Phase 3 – M29-Hardening
`stock.build`: Positionsmenge Ledger ∪ Referenz ∪ eindeutige externe Bestände; Bestätigungsarten; Teilbewertung;
getrennte Quellen-/Reconciliation-Evidenz in `research.assess`; ereignisweite Einzelfreigabe (`book_row`).

### Phase 4–9
Detailplanung und Umsetzung je Phase in §5; Abschlussbericht `docs/FINANCIAL_INTEGRITY_IMPLEMENTATION_REPORT.md`.

### Phase 10
Gesamtregression (synthetische Fälle des Auftrags 13.2), Ruff, CI, Docker-Build, Smoke, Massendaten (≥ 100.000
Quellzeilen), Export/Restore, Bericht `docs/FINANCIAL_INTEGRITY_IMPLEMENTATION_REPORT.md`.

## 5 Fortschritt

### Phase 1 – abgeschlossen (Tests bestanden)

| Ursache (§2) | Behebung | Test |
|---|---|---|
| Sieben Definitionen von „offen“ | `review.state_of` als einzige Definition; Stapelseite (`group_of`, `overview`, Prüfstand), Warteschlange, Datenquellen (`open_counts`, Datenhinweis), Stapelstatus leiten daraus ab | 04, 10 |
| Stapel schließen nie / zu früh | `refresh_status` in beide Richtungen (`committed`/`done` ↔ `partial`/`preview`) nach jeder Änderung; Übernahme markiert nicht gebuchte Vorschläge sichtbar als „nicht übernommen“; Start-Angleichung bestehender Stapel | 01, 02, 03, 07, 09, 13 |
| „Jetzt prüfen“ nur neuester Stapel | `/journal/pruefung` über alle aktiven Stapel, Filter Quelle/Stapel/Grund, Sprung zum Vorgang (`?ev=`) | 06, 10 |
| Ungeklärt/unvollständig nicht abschließbar | Entscheidungen „nicht übernehmen“ und „zurückstellen“ für alle offenen Zeilenarten (Zeile und Stapelaktion, mit Undo) | 03, 09, 14 |
| Nullanzeige ohne Grund | `review.funnel` + `zero_reason` auf der Stapelseite („Prüfstand“) | 05, 07 |
| Vor-Stichtag-Zeilen verknüpfbar (79 Aktionen) | Zustand „Historie“ (geschlossen), Vorschlag/Auswahl schließen sie aus, Verknüpfen nur ausdrücklich | 11 |
| BNB-Entscheidung kippt nach 48 h | Befundschlüssel und Prüfsumme ohne Abrufzustand; Datenquellenseite berücksichtigt Diagnose-Entscheidungen (`holding_mark`); Altschlüssel gelten weiter | 12 |
| `_present` kannte nur offene Stapel | abgeschlossene, nicht gebuchte Ereignisse werden nicht erneut vorgelegt – außer bei geänderter Lieferung (neue Evidenz) | 08 |

Nur-Lese-Garantie der Ansichten (Vorschau, Warteschlange, Stapel-, Datenquellen-, Diagnoseseite): Test 15.

### Phase 2 – abgeschlossen (Tests bestanden)

* **Warteschlange** `/journal/pruefung`: Reiter Zu prüfen · Sicher verarbeitbar · Abgeschlossen · Historie; Filter
  Quelle, Konto, Art, Abgleich (Evidenz), Sicherheit, Zeitraum, Prüfgrund (`queue.Filter`); Einheit Vorgang.
* **Auswahl** (`bulk.selected/store`): je Stapel gespeichert (dieselbe wie auf der Stapelseite). Vorausgewählt nur
  sichere Verknüpfungen; sichere *neue* Buchungen nur ausdrücklich. Abwahl bleibt. Ein Vorgang wird nie geteilt
  (Auswahlschlüssel = alle Zeilen mit sicherem Vorschlag eines Vorgangs).
* **Sammelvorschau** (`bulk.preview`): frisch bewertet; Gesamtwirkung auf Bestände, Gebühren, betroffene FIFO-Reihen ab
  Datum; je Stapel Wirkung, Ausschlüsse mit Grund, offene Fragen und Korrekturvorschläge (nie automatisch).
* **Ausführung** (`bulk.run`): ausdrückliche Bestätigung; Stapel für Stapel über `batch.execute` – je Stapel atomar,
  Token (genau einmal), Prüfsumme (nur unveränderte Vorschau); Ergebnis je Stapel; Undo im Verlauf des Stapels.
* **Manueller Fall** (`Item.variants`): Lösungswege je Grund (Angabe ergänzen, einordnen, Gegenüberstellung prüfen,
  Übernahme prüfen) und Entscheidungen Zurückstellen / Nicht übernehmen / Wieder aufnehmen – protokolliert, umkehrbar.
* **Sicherheitskorrektur:** Vorschläge („suggest“) übergehen keine eigene Entscheidung (nicht übernehmen,
  zurückgestellt) mehr – zuvor hätte eine Stapelaktion eine zurückgestellte Zeile verknüpfen können.
* **Tests** (`tests/test_review_bulk.py`): Vorauswahl/Abwahl, Sammelvorschau mit Bestätigung über zwei Stapel inkl.
  Teilabschluss und Idempotenz, geänderter Stapel wird nicht ausgeführt (andere schon), Lösungsvarianten mit Undo,
  Filter.

### Phase 3 – abgeschlossen (Tests bestanden)

| Lücke | Behebung | Test |
|---|---|---|
| Bestandsklärung kannte nur Ledger-Positionen | `stock.build`: Positionen = Ledger ∪ Referenzbestände ∪ belastbare externe Bestände (frischer, vollständiger Abruf); Kennzeichen „ohne Buchung“ | `test_reference_without_any_booking_appears_unvalued_not_zero`, `test_unique_external_balance_without_ledger_is_a_position` |
| Bestätigt vs. fortgeschrieben | je Position Bestätigungsart (Referenzbestand / Datenquelle) und Zustand bestätigt (keine spätere Buchung) bzw. fortgeschrieben (B′ = Nachweis + spätere Wirkung) | `test_rolled_forward_vs_confirmed_reference` |
| Teilbewertung | Summen nur über bewertete Positionen, Anzahl bewertet/gesamt sichtbar; ohne Kurs „unvollständig“, nie 0 € | wie oben |
| Quellen- und Abgleichsevidenz vermischt | `research.assess`: `source_level` (Originalbeleg vollständig · vollständig ohne Originalbeleg · teilweise belegt · ungeklärt) und `recon_level` (kein Gegenstück · ähnliche Buchung (Verdacht) · laut Importprüfung gebucht · gebucht) getrennt; Gesamtstufe = schwächere | `test_source_and_reconciliation_evidence_are_separate` |
| Mehrbein-Vorgang zerfiel | Evidenz je Ereignis (`load_rows` gruppiert); `book_row` bucht alle Zeilen eines Vorgangs in einer Freigabe (eine Transaktion), Undo nimmt alle zurück; teilweise entschiedener Vorgang wird nicht gebucht | `test_multi_leg_event_is_booked_and_undone_as_one`, `test_partially_decided_event_is_never_split` |

Prüfsummen bestehender Entscheidungen bleiben gültig: die neuen Evidenzangaben stehen in `Finding.evidence` (nicht in
den geprüften Befunddaten).

### Phase 4 – abgeschlossen (Tests bestanden)

* **Dateien** (`app/csvimport/files.py`): CSV, XLSX (ohne Makroausführung, XML über `defusedxml`, nur Zellwerte) und
  ZIP (rekursionsfrei; Grenzen: 50 Einträge, 100 MB entpackt, Kompressionsverhältnis ≤ 100; Pfad-Traversal,
  verschachtelte Archive und unbekannte Typen werden mit Grund abgewiesen). Jede enthaltene Tabelle wird ein eigener
  Stapel.
* **Erkennung:** vorhandene Profile + Portfolia-eigene Exportformate; unbekanntes Format → eigene Spaltenzuordnung.
* **Herkunft:** Migration 22 (`csv_batch.origin_json`, Index auf `file_sha256`): Archiv, Mitglied, Tabellenblatt,
  Datenquelle; „identisch mit Stapel …“ statt stiller Doppelanlage. Rohimporte bleiben unveränderlich.
* **Upload an der Datenquelle:** `POST /settings/datasources/{sid}/upload` (CSRF wie bestehend).
* **Tests:** `tests/test_universal_import.py` (13).

### Phase 5 – abgeschlossen (Tests bestanden)

`app/diagnosis/coverage.py`, Seite `/quality/abdeckung`: Matrix Konto/Quelle × Monat (Filter Zeitraum, Bereich,
Konto) mit Zuständen gebucht · nur Prüf-Stapel · abgedeckt ohne Vorgänge · anderweitig belegt · Lücke; Lücken nur
innerhalb des Aktivitätszeitraums einer Quelle (MEXC-Fall); vollständige Abrufe decken nur ihr Fenster
(`win_from`/`win_to`) ab. Eine Abfrage je Quelle (Massendaten: 0,6 s bei
100 000 Zeilen). Tests: `tests/test_coverage.py` (4).

### Phase 6 – abgeschlossen (Tests bestanden)

`app/csvimport/groups.py`, Seiten `/journal/ereignisse` und `/journal/ereignis/{tx_id}`: Vorgang als Gruppe aller
Quellzeilen (Ereignis-ID, Verknüpfungen); Widerspruchsarten doppelt gebucht, 1:n, n:1, nur teilweise verknüpft,
Gebühr brutto/netto, Mengenwiderspruch – nur angezeigt, nie automatisch aufgelöst. Tests: `tests/test_event_groups.py` (3).

### Phase 7 – abgeschlossen (Tests bestanden, nur geprüfte Formate)

Derivat-Kennzeichen (`contract.DERIVATIVE_TAGS`: Futures-PnL, Funding, Liquidation, Margin-Zins): Ledger-Ursprung
„derivative“, keine Zu-/Abflüsse, nicht in § 23; Importbewertung fordert Prüfung. Native Futures-Exporte von MEXC,
Bybit, Pionex sind **nicht** verifiziert und werden nicht automatisch erkannt (Spaltenzuordnung). Tests:
`tests/test_derivatives.py` (3).

### Phase 8 – abgeschlossen (Tests bestanden)

`app/ledger/provenance.py`, Seite `/quality/anschaffung`: Herkunftsklasse je Lot und verbrauchtem Lot-Anteil (Kauf,
Ertrag, Kapitalmaßnahme, Eröffnung, ohne Beleg, unbekannt, Derivat, ohne Basis); unbekannte Kosten nie 0 €.
Sammelvorschau nennt rückwirkend betroffene Steuerjahre (`retro_years`). Tests: `tests/test_lot_provenance.py` (4).

### Phase 9 – abgeschlossen (Tests bestanden)

`app/diagnosis/cockpit.py`, Seiten `/quality/cockpit` und `/quality/cockpit/fall`: acht getrennte Dimensionen
(Importprüfung, Quellenabdeckung, Bestandsabgleich, Bewertung, Ereignisgruppen, Anschaffungsdaten, steuerliche
Einordnung, Diagnose) mit Zustand, Kennzahl und Weg – **keine Gesamtnote**. Fallanalyse je Position mit möglichen
Belegquellen (Schnittstelle, Exportdatei, Kontoauszug/Beleg, Referenzbestand). Externe KI: standardmäßig aus, nur
Nachrichten, statisch geprüft, dass Finanzpakete keinen KI-Client importieren. Tests: `tests/test_cockpit.py` (3).

### Phase 10 – Gesamtregression und Release (Tests bestanden)

* Regressionen 13.2 (`tests/test_fi_regression.py`, Zuordnung aller Fälle im Bericht §16.5), Massendaten mit 100 000
  Quellzeilen (`tests/test_mass_review.py`), Export/Restore nach FI (`tests/test_fi_export_restore.py`), CI-Smoke um
  die neuen Seiten erweitert, Version 0.27.0.
* Dabei gefunden und behoben: Lots geschätzter Sparplan-Ausführungen galten als belegter Kauf (`6e825bd`).
* Lesende Validierung gegen eine Kopie eines realen Exports: kuratierter Import deckt seine Monate bis zum Stichtag ab
  (117 Scheinlücken), Mengenwidersprüche tolerieren die Rundung der Buchung und erkennen die Gebühr der Quelle
  (33 + 6 von 48 Scheinwidersprüchen), Fallanalyse für alle Positionen.
