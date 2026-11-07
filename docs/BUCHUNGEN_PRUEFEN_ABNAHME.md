# Buchungen prüfen – Abnahme (07.11.2026)

Abschlussprüfung des Funktionsbereichs „Buchungen prüfen“ auf dem Stand 0.107.0. Chronik und frühere Befunde (R01–R14,
N01–N37): [`BUCHUNGEN_PRUEFEN_KORREKTUREN.md`](BUCHUNGEN_PRUEFEN_KORREKTUREN.md). Die Abnahme gilt für die untersuchten und
getesteten Codepfade; allgemeine Fehlerfreiheit wird nicht behauptet.

## A. Ausgangsstand

| | |
| --- | --- |
| Branch / Commit | `claude/portfolia-dashboard-s9p6zr` @ `c1116f1` (0.107.0), Arbeitsbaum sauber |
| PR #3 „unvollständige Prüfläufe nicht als Erfolg melden“ | Kopf `226e829`, CI #230/#231 grün, Diff geprüft (nur `checkrun.py`, `dq_plan.html`, 3 Tests), basiert direkt auf `c1116f1` → **per Fast-Forward übernommen** (Hauptbranch `226e829`) |
| Arbeitsbranch dieser Abnahme | `claude/booking-review-acceptance-20261107` |
| Ausgangstests (`226e829`) | 283 gezielte Tests (Liste unter F) bestanden, Ruff ohne Befund |

## B. Codepfadmatrix

Einstiege aus den Router-Registrierungen (`@router.get/post`), Schreibwege über die Dienstfunktionen.

| Pfadgruppe | Einstieg | Zentrale Funktionen | Wirkung | Rücknahme | Nachweis | Ergebnis |
| --- | --- | --- | --- | --- | --- | --- |
| Assistent | `assistant_web` (13 Routen, 8 POST): `/quality/assistent`, `/antwort`, `/protokoll`, Undo | `guide.tasks/detail/step`, `_answer` → `A.mark/A.apply`, `K.*`, `ST.settle_case`, `RC.*` | Entscheidungen, Korrekturen (Token je Antwort) | Protokoll → `A.undo`, `restore_*` | `test_assistant*`, `test_review_fixes`, `test_review_107`, `test_review_acceptance` | Befund A1 behoben |
| Prüf-Stapel / CSV | `csvimport/module.py` (38 Routen, 27 POST) | `set_rows` (Besitzkennung), `batch.plan/execute` (Fingerabdruck, Token), `commit_apply`, `reevaluate_open` | Buchungen, Verknüpfungen, Entscheidungen | `batch.undo` (Wert + Besitz, N32), `restore_inputs` | `test_review_09x`, `external_096/106`, Echtdaten-Rückimport | unverändert korrekt |
| Diagnose-Aktionen | `diagnosis/web.py` (18 Routen, 12 POST), `bulk` | `actions.build_plan/preview/apply` (Datenstand in der Transaktion, Archivprüfung) | Überlagerungen, Journal | `actions.undo`, Stapel-Undo | `test_diagnosis*`, `test_review_*`; Probe: zweite Ausführung desselben Tokens schreibt nichts | korrekt |
| Klärung | `clarify_web` (9 Routen, 7 POST) | `clarify.run/refresh/apply_hypothesis/defer` | Lösungen über `actions` | über `actions`/Bulk | `test_clarify*`, `test_cutover_cases` | korrekt |
| Gesamtprüfung / Integrität | `dq_web` (`/quality/pruefen`, `/quality/plan`), `integrity_web`, Autocheck | `checkrun._run` (PR #3), `integrity.run`, `autocheck.job_run`, `workplan.build` | nur Prüfergebnisse | – | `test_dq_structure` (PR #3), `test_review_acceptance` | Befunde A1, A2 behoben |
| Stichtag / Ausgleich | `cutover/web.py` (10 Routen, 8 POST), Assistent `case:settle` | `fix`, `corrections.book/apply/undo`, `settle.plan/resolve/apply_form`, `guard` | Korrekturbuchungen am Vortag | `corrections.undo`, `fix.undo` | `test_cutover_*`, `external_106`, Echtdaten | korrekt (0.107-Vertrag) |
| Journal / Ledger / Steuer | Journal-Formulare, alle Schreibwege oben | `JournalService.save/save_in/delete_in`, `run_ledger`, Caches über `data_rev` | Buchungen, Lots, Veräußerungen | Status „deleted/reverted“, Undo-Wege | `test_journal*`, Echtdaten-Prüfsummen | korrekt |
| Datenquellen / Übernahme / Wiederherstellung | `datasources/web.py` (44 Routen), Restore | `takeover` (Token, Prüfungen, Undo), `fullexport._apply_db/_finish` | Übernahme, Wiederherstellung | Übernahme-Undo, Restore-Journal | `test_takeover*`, `test_restore_roundtrip`, Echtdaten | korrekt (mehrdeutig → `applied_gaps`) |

Frontend: Die Vorlagen (`guide.html`, `diagnosis*.html`, `review_queue.html`, `csv_batch.html`, `partials/dq_*`) senden die
fachlichen Schlüssel (Token, Fingerabdrücke, Eingabestände) mit; jede angebotene Antwort hat einen Ausgang (Matrix 0.106.0).
Wird der Datenstand während der Bearbeitung von einem anderen Vorgang geändert, lehnen alle Schreibwege mit Meldung ab
(Datenstand/Fingerabdruck in der Transaktion).

## C. Gefundene Fehler

| ID | Prio | Stelle | Ursache | Reproduktion | Änderung | Test | Ergebnis |
| --- | --- | --- | --- | --- | --- | --- | --- |
| PR #3 | P1 | `checkrun._run`, `dq_plan.html` | fehlgeschlagene Stapelneubewertung / Integritätsprüfung als Erfolg | PR-Tests | übernommen (Fast-Forward) | `test_dq_structure` (3) + HTTP-Test | behoben |
| A1 | P1 (I10/I15) | `guide.html`, `assistant_web` | Der Assistent zeigte „Alles geprüft“, obwohl der letzte Prüflauf bzw. die Integritätsprüfung fehlgeschlagen war; eine fehlgeschlagene Integritätsprüfung speichert keine Einträge, ihre Fragen fehlten still. PR #3 deckte nur die Startseite ab | 2 Tests rot, Kontrolle grün | `guide.incomplete_check` → „Prüfung nicht vollständig“ mit „Neu berechnen“ | `test_review_acceptance` (3) | behoben |
| A2 | P1 (I03/I11) | `autocheck.job_run` | Übersprungene automatische Nachprüfung („Lauf/Import aktiv“, „letzter Lauf zu jung“) wurde nie nachgeholt; bei eingeschalteter Automatik blendet die Startseite den Hinweis „Daten geändert“ aus → Prüfergebnis still veraltet bis zur nächsten Änderung | Test rot (keine Nachplanung) | Nachplanen, wenn das Prüfergebnis älter als der Datenstand ist (nicht bei „ausgeschaltet“, nicht ohne Änderung) | `test_skipped_autocheck_is_rescheduled_when_data_changed` | behoben |

Geprüft und ohne Befund (Proben): doppeltes Absenden im Assistenten (eine aktive Entscheidung), wiederholte Diagnose-
Korrektur mit demselben Token (keine zweite Schreibwirkung), Rückimport bekannter Vorgänge (Echtdaten, 120 → 0 Neubuchungen).

## D. Hardcoding

Keine Sonderregel für persönliche Assets, Konten oder Wallets gefunden (Suche nach Tickern, Kontonamen, Datumsgrenzen,
Anbieterverzweigungen, TODO/FIXME/HACK in Diagnose, CSV, Stichtag, Journal, Ledger, Übernahme).

| Fund | Bewertung |
| --- | --- |
| `identity.py`: `("bitpanda","TH")`, `UUID_PROVIDERS`, `events.NATIVE_ID_PREFIXES` | anbieterdefinierte Aliase bzw. ID-Formate – fachlich erforderlich, behalten |
| `engine.FEE_ASSETS`, `recommend._NATIVE/_CHAIN_TAG`, `suggest.CHAIN_LABEL`, `model` Symbol-Tabellen | native Gas-Token bzw. Chain-Metadaten (Protokoll), unterschiedliche Zwecke – behalten |
| Toleranzen `cutover.STAUB_*`, `ledger.DUST`, `MIGRATION_TOL`, CSV `QTY_TOL` 0,5 % (dreifach, identisch, dokumentiert „wie die Dublettenprüfung“), `takeover` (Untergrenze 1e-12, anderer Zweck) | dokumentierte fachliche Toleranzen – behalten |
| Bestandsvergleich `max(1e-8, |v|·1e-9)` dreifach (engine, audit, clarify) | redundante Implementierung derselben Regel → konsolidiert (E) |

## E. Bereinigung

* `model.holdings_tol`: eine Toleranzregel für Diagnose, Prüfbericht und Klärung (vorher drei identische Formeln; clarify
  verlangte ausdrücklich „gleiche Toleranz wie die Diagnose“). Verhalten unverändert (Volltest, Echtdaten-Prüfsummen).
* Bewusst nicht verändert: große Module (`service.py`, `engine.py`, `actions.py`), Migrationen, CSV-Toleranzen (identisch,
  getrennte Zwecke), Kompatibilitätswege für Aktionen vor 0.95/0.107, alle Schutzprüfungen und Tests.

## F. Tests und Abnahme

| Prüfung | Ergebnis |
| --- | --- |
| `ruff check app tests scripts` | ohne Befund |
| Gezielte Tests (`test_review_096/107/workflow/fixes/repair`, `test_reconciliation_pipeline/scenarios`, `test_dq_structure`, `test_integrity`, `test_clarify`, `test_assistant`, `test_cutover_cases/settle`, `test_restore_roundtrip`, `external_096/106`) | 283 bestanden (Ausgang) |
| Neue Tests `tests/test_review_acceptance.py` | 5 bestanden (3 vor der Korrektur rot) |
| `TZ=Europe/Berlin pytest -q tests` (Worktree `1d5daa8`) | 2683 bestanden, 3 übersprungen, 0 fehlgeschlagen (21:50 min) |
| `python -m app validate examples/beispiel-import.zip` | 0 Fehler, 124 Transaktionen |
| mypy (wie CI) | 344 → 344, Meldungen identisch; geänderte Dateien ohne neue Befunde |
| CI | siehe PR |

Echtdaten (Export 05.11.2026, nur isolierte Kopien, keine Netzzugriffe, Ergebnisse als Aggregate):

| Ablauf | Ergebnis |
| --- | --- |
| Ausgang | 6018 Buchungen, 177 Assets, 34 Konten, 38 Quellen, 89 Bestände, 5284 Lots, 2887 Veräußerungen, 110 Befunde |
| 139 Seiten | HTTP 200, Lesen ändert weder Wirtschaft noch Diagnose |
| Unveränderter CSV-Rückimport (120 Vorgänge) | alle als bekannt erkannt; zurückstellen/überspringen/verknüpfen/Vorschlag: Wiederholung ohne neue Aktion, Rücknahme stellt den Zustand her |
| Export → Wiederherstellung → Export | wirtschaftlich und in der Diagnose identisch; Wiederherstellung `applied_gaps` (113 von 114 Beobachtungen, 1 gemeldet: zwei gleichnamige Quellen) |
| Stichtag 01.01.2025: Vorschau, Festschreiben, Ausgleich | 241 Eröffnungslots, alle Gleichwertigkeitsprüfungen bestanden; 16 Ausgleichsbuchungen, 0 fehlgeschlagen; Wiederholung ohne Wirkung; Aufgaben 43 → 28 → 12, jede mit abschließender Antwort |
| Veraltete Bestätigung / gleichzeitig / altes Stichtag-Formular / Protokollfehler | abgelehnt / 4:0 gebucht / abgelehnt / atomar ohne Teilzustand |
| Vollständige Rücknahme | wirtschaftlicher Zustand identisch, 6018 wirksame Buchungen |
| Steuer-Feldprüfsummen 2019–2026, `c1116f1` gegen Endstand (gleicher Tag, fester Seed) | 128 Felder identisch |

## G. Verbleibende Einschränkungen

* **Fachlich offen:** die eine vom Anwender selbst lösbare Aufgabe; Bestände ohne Sollbeleg (Echtdatenkopie: 59 von 86
  Positionen) gelten mit dem berechneten Stand; N33 als Regel entschieden (bestätigte Bewertung samt Herkunft).
* **Bewusst zurückgestellt:** zurückgestellte Fälle bleiben sichtbar („Für diesen Durchgang fertig“, Klärungsliste).
* **Externe Nachweise:** eine Bestandsbeobachtung des Altexports ohne Quelladresse ist nicht eindeutig zuordenbar
  (sichtbar als Lücke, wird beim nächsten Abruf neu erhoben).
* **Technische Altlasten:** 344 nicht blockierende mypy-Befunde (unverändert); Export normalisiert Journalherkunft und
  Reihenfolge – Rohprüfsummen nach Export/Restore mit aktivem Stichtag nicht bitgleich, Bestände/Lots/Steuer gleich.
* **Nicht reproduzierte Verdachtsfälle:** keine.

## H. Abschlussbewertung

**ABGENOMMEN.** Alle gefundenen P1-Fehler (PR #3, A1, A2) sind mit fehlschlagend-dann-bestehenden Tests behoben; es sind
keine offenen P0/P1-Fehler bekannt. Volltest, Ruff und Beispielvalidierung sind grün, mypy unverändert. Die Echtdaten-
Abnahme zeigt keine unbeabsichtigte wirtschaftliche oder steuerliche Änderung. Die verbleibenden Punkte sind fachliche
Entscheidungen oder fehlende externe Belege und kein Anlass für weitere Programmlogik.
