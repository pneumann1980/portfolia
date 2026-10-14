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
| 1 | P0 Prüfworkflow reparieren | In Bearbeitung | – | – |
| 2 | Zentrale Prüfung, Sammelverarbeitung | Nicht begonnen | – | – |
| 3 | M29-Hardening | Nicht begonnen | – | – |
| 4 | Universeller Börsenimport | Nicht begonnen | – | – |
| 5 | Historische Quellenabdeckung | Nicht begonnen | – | – |
| 6 | Wirtschaftliche Ereignisgruppen | Nicht begonnen | – | – |
| 7 | Futures, Funding, Margin | Nicht begonnen | – | – |
| 8 | Anschaffungskosten, FIFO-Herkunft | Nicht begonnen | – | – |
| 9 | Vollständigkeitscockpit | Nicht begonnen | – | – |
| 10 | Gesamtregression, Release | Nicht begonnen | – | – |

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

### Phase 2 – Zentrale Prüfung und Sammelverarbeitung
`/journal/pruefung` mit Reitern (Zu prüfen · Sicher verarbeiten · Abgeschlossen · Historie), Filter (Quelle, Konto,
Art, Evidenz, Zeitraum), Anzeige je Vorgang, Vorauswahl nur sicherer Vorschläge (Abwahl bleibt), „Sichere Vorschläge
gesammelt verarbeiten“ über mehrere Stapel (je Stapel atomar, nacheinander, Teilerfolge sichtbar), Lösungsvarianten im
manuellen Fall. Abnahme: HTTP-End-to-End-Tests automatisch/manuell/Sammel/Teilabschluss.

### Phase 3 – M29-Hardening
`stock.build`: Positionsmenge Ledger ∪ Referenz ∪ eindeutige externe Bestände; Bestätigungsarten; Teilbewertung;
getrennte Quellen-/Reconciliation-Evidenz in `research.assess`; ereignisweite Einzelfreigabe (`book_row`).

### Phase 4–9
Siehe Auftrag; Detailplanung wird zu Beginn jeder Phase hier ergänzt (Module, Migrationen, Risiken, Tests).

### Phase 10
Gesamtregression (synthetische Fälle des Auftrags 13.2), Ruff, CI, Docker-Build, Smoke, Massendaten (≥ 100.000
Quellzeilen), Export/Restore, Bericht `docs/FINANCIAL_INTEGRITY_IMPLEMENTATION_REPORT.md`.

## 5 Fortschritt

*(wird je Phase ergänzt)*
