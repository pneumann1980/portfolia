# „Buchungen prüfen“ – Korrekturen zum Review vom 03.11.2026

Stand: 04.11.2026 · Version 0.90.0 (Nachprüfung) · Branch `claude/portfolia-dashboard-s9p6zr`

## Grundlage

| | |
| --- | --- |
| Review | `Portfolia_Buchungen_pruefen_Review_2026-11-03.md` mit Reproduktionspaket `Portfolia_Buchungen_pruefen_Reproduktionspaket_2026-11-03.zip` |
| Stand des Reviews | Commit `85e3a81` (0.87.1) |
| Ausgangsstand dieser Arbeit | Commit `8b8e90c` (0.88.0) – Spam-Tokens/News-Filter, ohne Bezug zu den Befunden |
| Einzelabgleich | Alle 14 Reproduktionstests des Pakets wurden auf `8b8e90c` ausgeführt; **jeder reproduzierte** sein Fehlerbild (Ausgabe je Test „R0x: …“). Kein Befund war durch 0.88.0 bereits behoben. |
| Commits | `0c872d7` R04/R09 · `bda7e91` R13/R14/V01 · `507db0f` R01/R11/V05 · `cd4a2e3` R02/R03/R10 · `b502d4f` R05/R06/R07/V02/V04 · `1b10538` R08/R12/V03 · danach Version 0.89.0 und diese Dokumentation |

Statusbegriffe (Spalte „Status vor Änderung“): *bestätigt* (Fehlerbild und Ursache wie beschrieben), *abweichende
Ursache*, *teilweise behoben*, *bereits behoben*, *nicht bestätigt*, *blockiert*. Spalte „Prüfung danach“: Ergebnis
der Regressionstests in `tests/test_review_fixes.py` (Soll-Verhalten) nach der Änderung.

## Prüftabelle

| ID | Status vor Änderung | Aktuelle Codefundstelle (8b8e90c) | Reproduktion / Beleg | Bewertung des Lösungsvorschlags | Umsetzung oder begründeter Verzicht | Prüfung danach |
| --- | --- | --- | --- | --- | --- | --- |
| R01 | bestätigt | `assistant_web._confirm`: `build_plan(..., given=bool(params))`; `guide.html`: `values.get(p.name) or p.default` | Repro-Test R01: leere Auswahl → erneute Vorschau für `D1\|D2` | zutreffend; `p__set` war im Assistenten ohne Wirkung | `given=True` für jedes abgeschickte Formular; Wiederanzeige prüft das Vorhandensein des Schlüssels (nur für die gewählte Antwort) | behoben – `test_R01_unchecking_every_pair_is_rejected_and_stays_empty` |
| R02 | bestätigt | `guide._row_detail` ohne Transferzweig; `batch._decide`: „möglicher Transfer – zuerst bestätigen“ | Repro-Test R02: `row:include` angeboten, POST scheitert, Aufgabe bleibt | zutreffend; Schutzprüfung bleibt | Transferfrage aus der Ausführbarkeit abgeleitet: genau ein Partner im Stapel → „Ja, Transfer“ (`pair_ok=1`, beide Seiten gemeinsam) / „Nein, zwei Vorgänge“ (`pair_ok=0`); Journal-Partner, konkurrierende Partner, Import-Gegenbuchung → gezielte Detailansicht (`go:open` mit `?ev=`); immer „Gehört nicht dazu“ und „Weiß ich nicht“. `row:include` wird bei offenem Transfer nicht mehr angeboten | behoben – `test_R02_*` (4 Tests) |
| R03 | bestätigt | `takeover.assess`: Prüfung „rows“ nur aus einem früheren Fehlversuch; Plan erst in `_apply_steps` | Repro-Test R03: `assess().ok`, `apply` lehnt ab, Stapel verworfen, nur Neuabruf angeboten | zutreffend; „rein lesender Planungskontext“ umgesetzt als Probelauf auf einer DB-Kopie | **Probelauf**: `_apply_steps` (Ausblenden, Auswerten, `batch.plan`, Buchen, Nachprüfung) läuft auf einer Kopie der Datenbank (`sqlite3`-Backup, eigener `AppContext`, gemeinsamer Kursbestand) und wird verworfen; Ergebnis je Vorschau-Token und Datenstand zwischengespeichert. Blocker erscheinen vor jeder Änderung als Prüfung „rows“ bzw. „probe“. „Jetzt entscheiden“ legt die betroffenen Vorgänge aus dem **gespeicherten** Vollabruf in einen Prüf-Stapel und öffnet den ersten direkt (kein Anbieterabruf) | **teilweise behoben** – Erkennung und Weg vollständig; Grenze siehe unten (Entscheidungen im Übernahmekontext) – `test_R03_*` |
| R04 | bestätigt | `takeover._apply`: Ausnahme nach dem Ausblenden ohne Kompensation | Repro-Test R04: Ausblendung bleibt, Status `failed`, `undo` lehnt ab | zutreffend | Jede Schreibphase speichert ihre Kennung sofort (`decision_id`, `batch_id`, `action_id`); Ausnahmen kompensieren in umgekehrter Reihenfolge; unvollständige Rücknahme bzw. Prozessabbruch → sichtbarer Weg „Unterbrochene Übernahme zurücknehmen“ (`recover`), neue Läufe gesperrt bis dahin | behoben – `test_R04_*` (6 Tests, Fehler nach jeder Phase, gescheiterte Kompensation, Prozessabbruch) |
| R05 | bestätigt | `takeover.start_apply`: Rückgabe von `apply` ignoriert; Polling nur bei `fetching`/`running` | Repro-Test R05: beschäftigte Quelle → `errors=[]`, Datensatz ohne Meldung | zutreffend | Beschäftigte Quelle und fehlender Vollabruf werden sofort beantwortet; vor dem Start Laufzustand „Übernahme gestartet …“; zurückgegebene Fehler ohne eigene Meldung werden gespeichert („Übernahme nicht ausgeführt: …“); die Seite aktualisiert sich auch zwischen Start und Laufbeginn | behoben – `test_R05_*` (3 Tests) |
| R06 | bestätigt | `takeover._apply.refuse`: `result_json = {"detail": []}` | Repro-Test R06: zweiter Versuch löscht `rows`/`dv`, Vorschau wieder grün | zutreffend | `note_refusal` aktualisiert nur Meldung und Feld `refused`; `rows`/`dv` der fachlichen Ablehnung bleiben bis zu einer neuen Prüfung | behoben – `test_R06_resubmitting_an_old_preview_keeps_the_row_blockers` |
| R07 | bestätigt | `assistant_web._undo`: Umleitung bei `back=protokoll` vor Auswertung; `guide_log.html` bietet denselben Knopf | Repro-Test R07: Klick ohne Meldung, Stand unverändert | zutreffend | Ergebnis wird vor dem Rückweg ausgewertet (`err`/`msg`); Teile einer aktiven Quellenübernahme zeigen statt des Knopfs den Weg zur gemeinsamen Rücknahme; Korrekturen mit späteren abhängigen Korrekturen nennen diese | behoben – `test_R07_*` |
| R08 | bestätigt | `guide.parse_skip`: `[:60]` | Repro-Test R08: 61./62. Aufgabe pendeln | zutreffend; größere Cookie-Liste wäre keine Lösung | Durchgang serverseitig (`app_state` `guide.passes`, bis 5 000 Aufgaben, 30 Durchgänge); Cookie `dq_skip` hält nur den Bezug `p:<16 hex>`; alte Listen-Cookies werden weiter gelesen | behoben – `test_R08_*` (130 Aufgaben ohne Pendeln, Fortsetzen, Neubeginn) |
| R09 | bestätigt | `batch.Plan.fingerprint` ohne Werte; Assistent ohne Bindung | Repro-Test R09: geänderter Wert nach Vorschau wird gebucht | zutreffend | Fingerprint bindet je Zeile Status, Entscheidung, Eingaben (Wert, Gebühr), Transferpaar und Buchungszeile (Asset, Menge, Konto, Datum, Einordnung); Assistent-Karten senden die Prüfsumme der gezeigten Zeilen (`x_fp`) bzw. den Stapelplan; Token-Idempotenz unverändert | behoben – `test_R09_*` (6 Tests) |
| R10 | bestätigt | `takeover._counts` nur Anzeige | Repro-Test R10: `gap=1`, `a.ok=True` | zutreffend; Zähleinheit je Anbieter nötig | `COUNT_UNIT`: nur Bitpanda zählt Rohobjekte von `/operations` (je Objekt genau ein Vorgang oder eine begründete Auslassung). Unerklärte Differenz → blockierende Prüfung „count“; Zählung fehlt/mehrere Etappen → „unbekannt“ (nicht blockierend, nicht als vollständig gewertet); andere Quellen: Zahl nur Anzeige | behoben – `test_R10_*` (3 Tests) |
| R11 | bestätigt | `guide.html`: alle Antwortfelder `p_<name>`; `assistant_web._params` ohne Antwortbezug | Repro-Test R11: „Zugang ohne Ertrag“ gewählt, `reward` gebucht | zutreffend | Feldnamen `ap.<antwort>.<name>`; Server liest nur den Namensraum der gewählten Antwort (auch in der Bestätigung); Felder ungewählter Antworten werden beim Absenden deaktiviert; skalare Eingaben genau ein Wert, Auswahlwerte gegen die Liste, leere gültige Auswahl („Zugang ohne Ertrag“) bleibt erhalten; Bestätigung zeigt die Angaben in Klartext | behoben – `test_R11_*` (3 Tests) |
| R12 | bestätigt | `guide.html`: „Alles geprüft“ bei leerer Liste ohne Übersprungene | Repro-Test R12: zurückgestellter Import offen, Seite „Alles geprüft“ | zutreffend | Abschluss aus den vorhandenen Zuständen (`review.active_summary`, „Weiß ich nicht“-Befunde, `clarify_case`): übersprungen, zurückgestellt (Import, Befunde, Fälle), noch offen, wartet auf Belege – je mit Weg; „Alles geprüft“ nur ohne offene Arbeit | behoben – `test_R12_*` |
| R13 | bestätigt | `guide._page_task` ohne `ref`; `answer` verlangt `ref` vor `go:open`/`skip` | Repro-Test R13: „Bitte eine Antwort auswählen“ | zutreffend | Antwort zuerst prüfen; Navigation und „Später“ nur mit Aufgabenbezug `kid`; Ziel weiter serverseitig (`_href_of`); Seitenaufgaben tragen ihren Schlüssel als `ref` | behoben – `test_R13_*` (4 Tests, inkl. Rückstufung `fall`→`seite`) |
| R14 | bestätigt | `batch.Filters.parse`: `[:80]` für alle Filter | Repro-Test R14: Solana-ID → „Keine Zeilen“ | zutreffend | Identitätsfilter (`ev`, `acc`) werden nie gekürzt; `ev` nach dem Ereignis-ID-Vertrag (`anbieter:<≤200>` bzw. `#<Zeile>`, ≤ 256 Zeichen) geprüft; ungültige IDs sichtbar zurückgewiesen | behoben – `test_R14_*` (6 Tests, inkl. gleiche 80-Zeichen-Präfixe) |
| V01 | bestätigt (Verbesserung) | `guide._stack_tasks`: `href=/journal/csv/<bid>` | Code-Lesung; Test vor der Änderung schlägt fehl (kein `ev`) | sinnvoll | Einzelvorgangs-Aufgaben springen mit vollständiger ID (`?ev=…#zeilen`); Ereignisfilter und `kid` bleiben im Filterformular erhalten; Hinweis „Gezeigt wird ein einzelner Vorgang“ mit Weg zu allen Zeilen | umgesetzt – `test_V01_*` |
| V02 | bestätigt (Verbesserung) | `guide_log.html` nur `diag_decision` | Code-Lesung | sinnvoll, mit vorhandenen Speichern | Protokoll „Weitere Änderungen“: Stapelaktionen (`import_action`) mit Rücknahme, Quellenübernahmen mit Weg zur Übernahmeseite, abgelehnte Quellenpräferenzen; Teile einer Übernahme und abhängige Korrekturen gekennzeichnet | umgesetzt – `test_V02_*`, `test_R07_*` |
| V03 | bestätigt (Verbesserung) | `guide.takeover_declined` nur Liste ohne Datum, kein Rückweg | Code-Lesung | sinnvoll | Ablehnung mit Datum (`guide.takeover_declined_at`); im Protokoll und auf der Abschlussseite sichtbar; „Erneut fragen“ nimmt genau diese Ablehnung zurück | umgesetzt – `test_V03_*` |
| V04 | bestätigt (Verbesserung) | `assistant_web._case_answer`: pauschal „Übernommen.“ | Code-Lesung | sinnvoll | Meldung des Klärungsergebnisses (geändert, offen bleibend) wird auf der nächsten Karte angezeigt | umgesetzt – `test_V04_*` |
| V05 | bestätigt (Verbesserung) | Fehler leiten zur jeweils ersten Aufgabe | Code-Lesung | sinnvoll | Eingabefehler zeigen dieselbe Aufgabe mit gewählter Antwort und Eingaben (`G.step(focus=…)`); weitergeleitete Fehler tragen `cur`/`ch` | umgesetzt – `test_V05_*`, `test_R01_*` |

## Ursachen und Umsetzung im Einzelnen

Gemeinsame Ursache (wie im Review): zwei Bausteine trafen unterschiedliche Annahmen über dieselbe Aktion – Vorschau
und Ausführung prüften nicht denselben Plan (R03, R09), die Karte bot Antworten an, die der Planer nicht ausführen kann
(R02, R13), und Zustände lagen dort, wo sie verloren gehen (Cookie-Liste R08, überschriebenes Ergebnisdokument R06,
Rückgabewert im Hintergrund R05). Die Korrekturen verbinden die vorhandenen Dienste statt neue Prüfungen nachzubilden.

**Übernahme ohne Teilzustand (R04).** `_apply` hält ein Zustandsobjekt (`decision_id`, `batch_id`, `action_id`), das
nach jeder Schreibphase sofort in `ds_takeover` steht. `_compensate` nimmt in umgekehrter Reihenfolge zurück
(Stapelaktion → Stapel → Ausblendung), löscht jede erfolgreich zurückgenommene Kennung sofort und ist idempotent
(bereits Zurückgenommenes gilt als erledigt). Scheitert ein Schritt, bleiben Status `failed` und die Kennungen;
`needs_recovery` sperrt neue Abrufe/Übernahmen, `recover` (Knopf „Unterbrochene Übernahme zurücknehmen“) holt die
Kompensation nach – auch nach einem Prozessabbruch (`running` ohne laufenden Thread).

**Probelauf (R03).** `probe()` kopiert die Datenbank per SQLite-Backup in ein temporäres Verzeichnis des Datenordners,
baut darauf einen eigenen `AppContext` (ohne Scheduler; Kursdienst und Kursbestand des echten Kontexts, damit keine
Kurse doppelt abgerufen werden) und ruft `_apply_steps` – exakt die Schritte der echten Übernahme. Die Rücknahme-
Funktion des Probelaufs sammelt nur Gründe. Testhaken für Fehlerphasen sind im Probelauf wirkungslos. Ergebnis je
`(Lauf, Vorschau-Token, data_version)` in `app_state`; während eines laufenden Hintergrundlaufs wird nur das
gespeicherte Ergebnis verwendet. `to_review()` legt die blockierten Ereignisse aus `events_json` in einen Prüf-Stapel
der Quelle (bereits vorgelegte nicht doppelt) und öffnet den ersten per `?ev=`.

**Bindung an die gezeigten Werte (R09).** `batch.row_state` (Status, Entscheidung, EUR-Wert/Gebühr-Eingabe,
Transferpaar und -bestätigung, Buchungs-ID, Buchungszeile) geht in `Plan.fingerprint` ein; `rows_digest` bindet
Assistentenkarten (`x_fp`). `execute` prüft unter derselben Sperre wie `set_rows`.

**Antworten und Eingaben (R01, R11, R13, V05).** `answer()` prüft zuerst die Antwortart; nur fachliche Schreib-
antworten brauchen eine Referenz. Felder heißen `ap.<antwort>.<name>`; `_params(form, aid)` liest nur diesen
Namensraum. `build_plan` erhält `given=True` und prüft skalare Eingaben (genau ein Wert) und Auswahllisten; eine leere
Auswahl, die selbst eine Einordnung ist, bleibt erhalten. Fehler zeigen dieselbe Aufgabe (`G.step(focus=kid)`).

**Transfers (R02).** `_transfer_case` bestimmt für den Vorgang, ob die Transferfrage hier entscheidbar ist (genau ein
Partner im Stapel) oder nur in der Detailansicht (Journal-Partner, konkurrierende Partner, Import-Gegenbuchung). Die
Antworten setzen `pair_ok` über `set_rows` und buchen über `batch.plan`/`execute` (bei „Ja“ beide Seiten) – die
Schutzprüfung bleibt unverändert wirksam.

**Zählung (R10).** `_counts` liefert zusätzlich `state` (`ok`, `gap`, `unknown`, `na`) und die Zähleinheit; `_count_check`
macht daraus eine Prüfung (blockierend nur bei `gap`).

**Laufende und Protokoll (R05–R08, R12, V02–V04).** Siehe Prüftabelle; der Durchgang liegt in `app_state`, der Browser
hält nur `p:<16 hex>`.

## Nachweis „schlägt vor der Korrektur fehl“

Die Regressionstests beschreiben das Soll-Verhalten. Sie wurden zusätzlich auf dem Ausgangsstand `8b8e90c` ausgeführt
(Worktree, unveränderte Tests): **47 von 49 Tests schlagen fehl, 2 bestehen.**
Die beiden bestandenen sind bewusst Absicherungen ohne Fehlerbild: `test_R14_short_and_row_ids_open_their_row` (kurze
IDs und „#Zeile“ funktionierten schon vorher und dürfen nicht brechen) und `test_R08_old_cookie_lists_are_still_read`
(Abwärtskompatibilität alter Cookies).

| Befund | Fehlschlag auf 8b8e90c (Auszug) | Art |
| --- | --- | --- |
| R01 | Feld `ap.opt:hide_second.pairs` fehlt; leere Auswahl wird wieder befüllt | Verhalten |
| R02 | `row:include` wird angeboten; „Ja/Nein Transfer“ unbekannt („Unbekannte Antwort“) | Verhalten |
| R03 | `assess().ok` trotz späterer Ablehnung; Probelauf fehlt | Verhalten |
| R04 | Fehlerhaken `_FAULTS`/`_fault` existieren noch nicht | neue Testschnittstelle – das Fehlverhalten selbst belegt Repro-Test R04 |
| R05 | Rückgabefehler nicht im Datensatz; beschäftigte Quelle → `errors=[]` | Verhalten |
| R06 | `result_json.rows` nach erneutem Absenden leer (über neuen Probelauf-Haken `probe` gesteuert) | Verhalten, Steuerung über neue Schnittstelle |
| R07 | Protokoll ohne Weg zur Übernahme, Rückweg ohne Meldung | Verhalten |
| R08 | bereits übersprungene Aufgabe erscheint erneut (Pendeln ab Nr. 61) | Verhalten |
| R09 | gleicher Fingerprint nach geänderter Eingabe; `x_fp` fehlt; Vorschau veraltet unbemerkt | Verhalten |
| R10 | `counts.state` fehlt, Übernahme nicht gesperrt | Verhalten (Schlüssel `state` neu) |
| R11 | gemeinsame Felder `p_tag`; Mehrfachwerte ohne Fehler | Verhalten |
| R12 | `G.rest_overview` fehlt; Seite „Alles geprüft“ (Repro-Test R12) | neue Schnittstelle + Verhalten |
| R13 | „Bitte eine Antwort auswählen“ statt Seitenwechsel | Verhalten |
| R14 | ID auf 80 Zeichen gekürzt, Zeile nicht gefunden, ungültige IDs nicht zurückgewiesen | Verhalten |
| V01–V05 | kein `ev` im Sprungziel, kein Abschnitt „Weitere Änderungen“, keine Ablehnung mit Datum, pauschal „Übernommen.“, Fehler zeigen die erste Aufgabe | Verhalten |

Zusätzlich wurden die 14 Reproduktionstests des Pakets (`test_review_findings.py`) auf dem **Endstand** ausgeführt:
alle 14 schlagen an ihrer Fehlerbild-Zusicherung fehl, keiner gibt mehr seine Defektzeile („R0x: …“) aus – das
beschriebene Ist-Verhalten tritt nicht mehr ein.

Die Reproduktionstests des Pakets bestätigen das Ist-Verhalten und wurden nicht übernommen; sie schlagen nach den
Korrekturen erwartungsgemäß fehl (ihre Zusicherungen beschreiben den Fehler).

## Tests und Befehle

Alle Tests mit isolierten synthetischen Datenbanken (`tmp_path`) und gemockten Anbieterantworten (Bitpanda-Fixture
`api`, Wallet-Ereignisse über `app.datasources.wallet.event`); keine echten Zugangsdaten, kein Netz.

| Befehl | Ergebnis |
| --- | --- |
| `ruff check app tests scripts` | ohne Befund |
| `pytest tests/test_review_fixes.py` | 49 bestanden |
| Nachbarsuiten je Paket (`test_assistant*`, `test_review_*`, `test_takeover*`, `test_bitpanda*`, `test_transfer*`, `test_csvimport`, `test_decision_queue`, `test_mass_review`, `test_diagnos*`, `test_clarify*`, `test_reset*`) | bestanden (nach den unten genannten Anpassungen) |
| `pytest tests/test_review_fixes.py` auf `8b8e90c` | 47 fehlgeschlagen, 2 bestanden (siehe oben) |
| Repro-Tests des Reviews auf dem Endstand | 14 fehlgeschlagen (Fehlerbild tritt nicht mehr ein) |
| Volltest `TZ=Europe/Berlin pytest -q -x tests` (Worktree des Endstands) | 2081 Tests, davon 2078 bestanden und 3 übersprungen, 0 fehlgeschlagen (Exit-Code 0) |

**End-to-End-Pfade** (über HTTP mit gerenderten Formularen, wie ein Browser sie absendet): Seitenaufgabe öffnen und
„Später“ (R13), Einzelvorgang mit langer ID direkt öffnen und zurück (R14/V01), Antwortwechsel mit Eingaben bis zur
gebuchten Diagnosebuchung (R11), Transfer bestätigen/ablehnen/zurückstellen/in der Detailansicht öffnen (R02),
Übernahme: Vorschau mit Probelauf → „Jetzt entscheiden“ → Prüf-Stapel mit genau dem Vorgang → Entscheidung →
Neubewertung (R03), Übernahme mit Fehlern in jeder Phase und nach Prozessabbruch → Wiederherstellung (R04),
Protokoll-Rücknahmen (R07/V02), Durchgang mit 130 Aufgaben (R08), Abschlussseite mit zurückgestellter Arbeit (R12).

**Angepasste Bestandstests** (sie schrieben das bemängelte Verhalten fest): `test_takeover.py::
test_failed_postcheck_rolls_everything_back` und `test_review_repair.py::test_rows_needing_a_decision_…` (Vorschau war
grün, Ablehnung erst beim Buchen – jetzt zeigt der Probelauf den Grund vorab; der Rücknahmepfad wird weiter mit einem
gezielt überbrückten Probelauf geprüft), `test_review_repair.py::test_observed_missing_…` (neue Schlüssel der Zählung),
`test_assistant.py::test_answer_with_inputs_…` (Feldnamen je Antwort), `test_assistant_accept.py::
test_open_answer_uses_the_target_of_the_task_only` (Sprungziel mit `ev`), `test_assistant_more.py::
test_log_page_lists_answers_…` (Rückweg mit Meldung), `test_assistant.py::test_finished_state_…` und
`test_review_repair.py::test_finish_screen_…` (Abschluss nur ohne offene Arbeit).

## Datenmodell, Migrationen, Bedienung

* **Keine Schemamigration.** Neue Zustände liegen in `app_state`: `takeover.probe.<Quelle>` (Probelauf-Ergebnis je
  Vorschau und Datenstand), `guide.passes` (Durchgänge), `guide.takeover_declined_at` (Datum der Ablehnung).
  Vorhandene Spalten von `ds_takeover` (`decision_id`, `batch_id`, `action_id`) werden während des Laufs sofort
  gesetzt; `result_json` erhält zusätzlich `refused`.
* **Rücknahme:** Übernahme ohne Teilzustand (Kompensation je Phase, Wiederherstellung nach Abbruch); Protokoll verweist
  auf den jeweils richtigen Rücknahmeweg.
* **Bedienung:** Transferfrage im Assistenten; „Jetzt entscheiden“ auf der Übernahmeseite; Einzelvorgang direkt;
  Abschlussseite mit offenen Resten; „Weitere Änderungen“ im Protokoll; Felder je Antwort.

## Verbleibende Grenzen

* **R03 (bis 0.89.0):** Entscheidungen im Übernahmekontext fehlten – mit 0.90.0 umgesetzt, siehe „Nachprüfung“.
* **Probelauf:** braucht kurzzeitig Plattenplatz für eine Kopie der Datenbank und Rechenzeit (einmal je Vorschau und
  Datenstand). Gelingt die Kopie nicht, wird das als nicht blockierende Prüfung gemeldet; die echte Übernahme prüft
  weiterhin selbst und nimmt bei Ablehnung alles zurück.
* **R10:** Die Zählung blockiert nur für Bitpanda (belastbare Zähleinheit). Für Wallets und Binance ist
  `coverage.operations` aus den Ereignissen abgeleitet und kein unabhängiger Beleg.
* **R08:** Durchgänge sind je Browser (Cookie), nicht je Tab getrennt.
* **R11:** Das Deaktivieren ungewählter Felder setzt JavaScript voraus; die Trennung gilt serverseitig unabhängig davon.


---

# Nachprüfung vom 04.11.2026 (Version 0.90.0)

Grundlage: `Portfolia_Buchungen_pruefen_Nachpruefung_2026-11-04.md` mit Nachweispaket
(`test_additional_review.py`, geprüft auf `725ad2c`, 0.89.0). Die Nachprüfung stufte R03, R04, R09 und V02 als
teilweise erledigt ein und belegte vier neue Befunde N01–N04. **Alle sechs Soll-Tests des Nachweispakets schlugen auf
`725ad2c` fehl (Einzelabgleich vor jeder Änderung) und bestehen auf dem Endstand.** Die beiden „Nachweis“-Fälle des
Pakets prüften Zwischenstände, die es nicht mehr gibt (Weg über den normalen Prüf-Stapel entfernt; die im Test lokal
geöffnete Planersperre ist jetzt regulär und eng geöffnet, mit erweiterter Signatur) – sie sind durch die Tests unten
ersetzt.

Commits: `be36937` N01/N02/N03 · `9231eef` N04 · `9b8d539` R03 · danach Version 0.90.0 und diese Dokumentation.

| ID | Abgleich auf 725ad2c | Tatsächliche Ursache | Korrektur | Test danach | Verbleibende Grenze |
| --- | --- | --- | --- | --- | --- |
| N01 (R04) | bestätigt: Ausnahme bzw. Abbruch nach dem Commit von `apply_plan` → alle Buchungen ausgeblendet, Bezüge `NULL`, kein Wiederherstellungsweg | `apply_plan`, `ingest` und `batch.execute` committen, bevor `_apply_steps` die Kennung speichert; danach laufen noch Nachberechnung, Bewertung, Cache | Haken `in_tx(c, id)` in allen drei Diensten schreibt den Bezug in `ds_takeover` **in derselben Transaktion** wie die Änderung; die Rücknahme liest die Bezüge aus der Datenbank; Kompensation erkennt Teile, die nach ihrem Commit scheiterten, an ihrem Zustand | `test_N01_*`: Fehler und Prozessabbruch **vor und nach** jedem Commit (Ausblendung, Stapel, Buchungsaktion) – jeweils alter Stand oder vollständig referenzierter, sichtbar wiederherstellbarer Teilzustand; 10/10 bestanden | keine eigene Spalte für zusätzliche Stapelaktionen nötig: sie hängen am gespeicherten Stapel des Laufs |
| N02 (R09) | bestätigt: geänderte Gegenseite, alter POST bucht Transfer nach „Changed after preview“ | `x_fp` band nur die eigenen Zeilen; „Ja, Transfer“ nahm den Partner erst beim Absenden auf; `pair_ok` wurde vor der Schlussprüfung gespeichert | `batch.effect_digest`: Wirkungsmenge = eigene Zeilen + Transferpartner + wirksamer Zustand referenzierter Buchungen (Journalpartner, Verknüpfungsziel inkl. Import); dieselbe Grundlage beim Anzeigen und Schreiben; Prüfung und Änderung unter der Sperre des Prüf-Stapels; bei Ablehnung wird `pair_ok` zurückgesetzt | `test_N02_*`: Konto, Wert, Gebühr, Entscheidung der Gegenseite, geänderte Verknüpfungszielbuchung – alte Antwort ohne Wirkung, neue Vorschau genau einmal; 5/5 | – |
| N03 (R04) | bestätigt: Fehlerergebnis → Status wieder `applied`, zweiter Versuch scheitert; Ausnahme → schon `undone` | `undo` setzte nur den Gesamtstatus zurück, obwohl Teilschritte wirksam waren; nutzte nicht die idempotente Kompensation; nahm nur die Hauptaktion zurück | `undo` setzt vor dem ersten Schritt dauerhaft `undoing`, nutzt `_compensate(keep=True)`: alle aktiven Stapelaktionen des Laufs, Stapel, Ausblendung; erledigte Teile werden übersprungen; `undone` erst bei vollständiger Rücknahme; `needs_recovery`/`recover` führen ein begonnenes Rückgängig fort; Ausblendung bleibt getrennt gesperrt, solange API-Buchungen des Laufs bestehen | `test_N03_*`: Fehlerergebnis, Ausnahme, Prozessabbruch je Schritt (Aktion, Ausblendung), zweiter Aufruf stellt her, dritter ändert nichts; 7/7 | – |
| N04 (V02) | bestätigt: Bestätigung verschwindet aus den Aufgaben, fehlt im Protokoll | `guide.integrity_ack` wurde vom Protokoll nicht gelesen; nur ID und Prüfsumme gespeichert | zusätzlich Bezeichnung, Konto/Asset, Zeitpunkt (`guide.integrity_ack_info`); Protokoll listet Bestätigungen mit „Zurücknehmen“; alte Einträge und vom neuen Prüflauf nicht mehr gemeldete Befunde bleiben verständlich | `test_N04_*`; 2/2 | – |
| R03 | bestätigt: normales Verknüpfen im Prüf-Stapel löst den Übernahme-Blocker nicht | Entscheidungen wurden im Zustand **vor** dem Ausblenden getroffen | Entscheidungen je Quelle und gespeichertem Lauf (siehe unten) | `test_R03_*`; 9/9 | siehe Grenzen |

## R03 – Entscheidungen je Übernahmelauf

* **Angebot aus dem tatsächlichen Plan:** Der Probelauf (Zustand nach dem Ausblenden) liefert je abgelehntem Vorgang
  die ausführbaren Antworten und den Partner: API-Vorgang im selben Abruf, Journalbuchung, Import-Buchung eines anderen
  Kontos oder konkurrierende Partner. Bei konkurrierenden Partnern gibt es nur „Kein Transfer“ und „Nicht übernehmen“ –
  ein Partner wird nie still gewählt.
* **Ablage:** `app_state` `takeover.decisions.<Quelle>` mit Laufkennung; Prüfsumme des Vorgangs und des Partnerzustands.
  Gespeichert nur zur angezeigten Vorschau (Token); jede Entscheidung ändert Token und damit den Probelauf-Cache; ein
  neuer Vollabruf löscht sie. Geänderter Vorgang oder Partner → „verworfen“ mit Grund, nie angewendet.
* **Anwendung** in `_apply_steps` (Probelauf und Übernahme identisch): `pair_ok` wie im Prüf-Stapel, „nicht übernehmen“
  als Stapelaktion nur dieses Laufs (keine dauerhafte Ignorier-Entscheidung). Der Planer öffnet über `approved` nur für
  genau diese Zeilen die Transfer-Sperren („Partner im Journal“, „möglicher Transfer“); Dubletten-, Vollständigkeits- und
  Mehrdeutigkeitsprüfungen bleiben. Passt der Partner im Zustand nach dem Ausblenden nicht, wird abgelehnt.
* **Import-Buchung als Partner** (Originalfall K3/L1): Sie wird im selben Lauf ausgeblendet und durch die Gegenseite
  eines Transfers ersetzt (gekennzeichneter Vorgang `takeover:<Lauf>:partner:<Buchung>`). Ergebnis: ein Transfer
  Bitpanda → Ledger, Bestand beider Konten exakt, Einstand und Anschaffungsdatum des Kaufs fortgeführt.
* **Journalpartner:** bestätigtes Paar wird mit den vorhandenen Diensten zusammengeführt (`merged`) und mit dem Lauf
  zurückgenommen (Partner wieder aktiv).
* **Nachweis:** Lebenszyklus Vorschau → Entscheidung → alte Vorschau abgelehnt → neuer Probelauf → Übernahme → exakte
  Bestände (alle Konten) → vollständige Rücknahme (Bestände, Lots mit Einstand/Datum, Ergebnisse je Jahr, wirksame
  Aliase und Journalzustand) → wiederholte Rücknahme ohne Wirkung; mit und ohne Gebühr am Zugangskonto.

## Tests und Befehle (0.90.0)

| Befehl | Ergebnis |
| --- | --- |
| `ruff check app tests scripts` | ohne Befund |
| `python -m app validate examples/beispiel-import.zip` | 0 Fehler, 0 Warnungen, 124 Transaktionen |
| `pytest tests/test_nachpruefung.py` | 33 bestanden |
| Nachweispaket der Nachprüfung auf `725ad2c` | 6 Soll-Tests fehlgeschlagen (wie berichtet) |
| Nachweispaket auf dem Endstand | 6 Soll-Tests bestanden; 2 Zwischenstand-Nachweise nicht mehr anwendbar (s. o.) |
| Nachbarsuiten (Übernahme, Prüf-Stapel, Assistent, Transfer, Diagnose, Reset, Bitpanda) | bestanden |
| Volltest `TZ=Europe/Berlin pytest -q -x tests` (Worktree des Endstands) | 2114 Tests, davon 2111 bestanden und 3 übersprungen, 0 fehlgeschlagen (Exit-Code 0) |

Angepasste Bestandstests: `test_review_fixes.py::test_R03_preview_…` und `test_review_repair.py::test_rows_needing_…`
(Entscheidung jetzt auf der Übernahmeseite statt über den Prüf-Stapel).

## Grenzen nach der Nachprüfung

* **Konkurrierende Partner:** Die Übernahme bietet „Kein Transfer“ bzw. „Nicht übernehmen“; einen bestimmten der
  konkurrierenden Partner auszuwählen ist nicht vorgesehen (die Paarbildung des Prüf-Stapels kennt nur den Vorschlag).
* **Partner im selben API-Abruf** (`b:`): gleicher Codepfad wie die ersetzte Import-Buchung; mit dem Bitpanda-Fixture
  nicht eigens als Übernahme-Szenario konstruierbar – abgedeckt über den Import-Partner und die Assistententests.
* **Probelauf-Kosten:** je neuer Entscheidung ein Probelauf auf einer Datenbankkopie.
* Zurückgenommene Buchungen und Aliase auf sie bleiben als Nachweis stehen; wirksam sind sie nicht.

# Review vom 04.11.2026 zu 0.90.0 (Version 0.91.0)

Grundlage: `Portfolia_Buchungen_pruefen_Review_090_2026-11-04.md` mit Nachweispaket (`test_review_090.py`, 13 Fälle,
geprüft auf `e950b88`, 0.90.0). **Reproduktion auf `e950b88`: 7 der 13 Fälle schlugen fehl** (N05 ×3, N06, N07, N08,
N09); die übrigen 6 (zwei API-Seiten in einem Abruf, fünf Fehlerarten beim Verwerfen des Stapels während der
Rücknahme) bestanden bereits und bestätigen die Korrekturen zu N01/N03. **Auf dem Endstand bestehen alle 13.** Die Fälle
sind mit unveränderten fachlichen Erwartungen in `tests/test_review_090.py` übernommen und um 9 eigene ergänzt.

| ID | Abgleich auf e950b88 | Ursache | Änderung | Nachweis | Verbleibende Grenze |
| --- | --- | --- | --- | --- | --- |
| N05 Gebühren erhalten | bestätigt: bei BTC-, ETH- und EUR-Gebühr am Zugangskonto fehlte nach der Übernahme genau die Gebühr im Bestand von Ledger; die Nachprüfung meldete nichts | `_partner_recs` baute die Gegenseite der ersetzten Import-Buchung ohne Gebührenfelder; die Nachprüfung verglich nur das Konto der Quelle | Gegenseite übernimmt Gebührenasset, -menge und EUR-Wert der ersetzten Buchung; der Planer bucht sie wie jede Empfängergebühr als Gebührenbuchung am Zugangskonto, zum Transfer gruppiert (Belastungslogik unverändert). Eine Gebühr nur als EUR-Wert ohne Asset wird nicht still verworfen: die Buchung wird dann nicht ersetzt, die Entscheidung erscheint mit Grund als nicht anwendbar. **Nachprüfung über alle Konten:** erwarteter Bestand jedes anderen Kontos = vorher + Wirkung der gebuchten API-Zeilen (ersetzte Gegenseiten neutral) – jede Abweichung führt zur vollständigen Rücknahme | Reviewfälle BTC/ETH/EUR; eigene: Gebühr + empfangene Mengendifferenz (BTC, ETH; Transfer 0,001 → 0,00097, Sendergebühr der API, Empfängergebühr mit EUR-Wert am Zugangskonto, übrige Konten unverändert, vollständige Rücknahme); EUR-Gebühr ohne Asset. Gegenprobe: Gebührenübertrag deaktiviert → Probelauf sperrt mit „Ledger · BTC: danach 0,001, erwartet 0,00098“ | Reine EUR-Gebühr ohne Asset ist nicht übertragbar (sichtbar, kein Ersatz) |
| N06 Bestätigung atomar | bestätigt: Änderung der Verknüpfungsziel-Buchung zwischen Kartenprüfung und Schreiben → Verknüpfung trotzdem geschrieben | Prüfsumme wurde unter der Sperre des Prüf-Stapels geprüft, Journaländerungen nehmen diese Sperre nicht; `execute` prüfte die referenzierten Buchungen nicht erneut | Karte trägt zusätzlich die **Revision der abhängigen Buchungen aus der Datenbank** (`x_rev`: Journalzeile, App-Änderung, Zeile des aktiven Imports von Verknüpfungsziel, Dubletten-Bezug und Journal-Transferpartner). `batch.execute(expect_refs=…)` prüft sie als erste Anweisung **innerhalb** seiner Schreibtransaktion (`BEGIN IMMEDIATE`): eine parallele Änderung davor wird erkannt, eine danach wartet auf den Commit (serialisiert). Bei Ablehnung wird nichts geschrieben; ein vorab gesetztes `pair_ok` wird zurückgesetzt | Reviewfall (echter zweiter Thread); eigene: Dienstebene (Revision geändert → Fehler, keine Verknüpfung), neu geladenes Formular verknüpft genau einmal, erneutes Absenden ohne Doppelung | Prüfung gilt für Bezüge der Wirkungsmenge; andere Journaländerungen ohne Bezug zur Antwort blockieren nicht (gewollt) |
| N07 Einzeln buchen | bestätigt: „Einzeln buchen“ angeboten und gespeichert, Vorgang blieb gesperrt | Freigabefilter im Planer ließ nur `pair`/`nopair` zu | Planer akzeptiert `include`; es öffnet **ausschließlich** die Prüfmarkierung der Quelle. Abweichungen zu einer vorhandenen Buchung, Dubletten, Unvollständiges, offene Transferfragen und Mehrdeutigkeit bleiben gesperrt (`pair`/`nopair` unverändert) | Reviewfall; eigener: Freigabe `include` für Zeile mit Gegenstück und für unvollständige Zeile → beide gesperrt | – |
| N08 Entfernen gebunden | bestätigt: Formular eines alten Abrufs entfernte die Entscheidung des neuen Laufs | `undecide` nutzte nur die Kennung des Vorgangs und den jüngsten Lauf | Formular trägt Laufkennung und Stand der Entscheidung (`stamp` aus Antwort, Grundlage, Partnerzustand, Zeitpunkt, laufender Nummer). `undecide(sid, key, rid, stamp)` prüft Lauf (muss der jüngste, noch offene sein) und Stand in **einer Schreibtransaktion** unter der Übernahmesperre; Abweichung → sichtbare Meldung, nichts geändert. `decide` schreibt ebenfalls atomar und nur in den offenen, angezeigten Lauf | Reviewfall; eigene: gleicher Lauf mit inzwischen geänderter Entscheidung (alter Tab), Formular ohne Lauf/Stand, doppeltes Absenden, zwei gleichzeitige Entfernen-Anfragen (genau eine wirkt) | – |
| N09 Angewendete Entscheidungen | bestätigt: nach erfolgreicher Übernahme erschien die bestätigte Import-Gegenbuchung als „verworfen – Gegenbuchung hat sich geändert“ | Die Bewertung prüfte gespeicherte Entscheidungen auch im angewendeten Lauf gegen den aktuellen Stand – dort ist die ersetzte Buchung planmäßig ausgeblendet | Nur Vorschauentscheidungen (Lauf `staged`) werden geprüft. Beim Übernehmen werden die angewendeten Entscheidungen im Ergebnis des Laufs protokolliert; die Seite zeigt sie als „angewendet“ (nach Rücknahme als „zurückgenommen“), ohne Entfernen-Knopf; Ändern nur über „Übernahme zurücknehmen“ | Reviewfall; eigener: nach Neustart (neuer Kontext) „angewendet“, kein „verworfen“, Entfernen im angewendeten Lauf abgelehnt, Rücknahme vollständig | Läufe aus 0.90.0 ohne Protokoll zeigen keine Liste (keine falsche „verworfen“-Meldung) |

## Erhalt früherer Korrekturen

N01 (Bezüge in derselben Transaktion), N03 (fortsetzbare Rücknahme inkl. aller fünf Fehlerarten beim Verwerfen des
Stapels) und N04 sind durch die Bestandstests und die übernommenen Reviewfälle abgedeckt. Herkunft, Gebühren,
Kostenbasis (Lots mit Einstand/Datum) und vollständige Rücknahme prüft `_all_state` (alle Konten, Lots, Ergebnisse je
Jahr, wirksamer Journalzustand) vor und nach jeder Rücknahme.

## Teststeuerung

Keine Änderung der Synchronisierung nötig: Der N06-Reviewfall ersetzt `BA.execute` durch einen Wrapper, der die
parallele Journaländerung genau vor dem Aufruf auslöst; die neue Prüfung liegt innerhalb der Transaktion von
`execute` und damit hinter diesem Punkt. Angepasst ist nur `test_nachpruefung.py::test_R03_skip_and_standalone_…`
(`undecide` verlangt jetzt Lauf und Stand – Erwartung unverändert).

## Tests und Befehle (0.91.0)

| Befehl | Ergebnis |
| --- | --- |
| `ruff check app tests scripts` | ohne Befund |
| `python -m app validate examples/beispiel-import.zip` | 0 Fehler, 0 Warnungen, 124 Transaktionen |
| `tests/test_review_090.py` auf `e950b88` | 14 fehlgeschlagen (7 Reviewfälle + 7 eigene), 8 bestanden (6 Reviewfälle zu N01/N03; eigene Sicherungsfälle „Schutzprüfungen bei Einzelbuchung“ und „Verknüpfung genau einmal“) |
| `pytest tests/test_review_090.py` (Endstand) | 22 bestanden (13 Reviewfälle + 9 eigene) |
| Nachbarsuiten (Übernahme, Prüf-Stapel, Assistent, Diagnose, Journal, Bitpanda, Nachprüfung, Reviews) | bestanden |
| Volltest `TZ=Europe/Berlin pytest -q -x tests` (Worktree des Endstands) | 2136 Tests, davon 2133 bestanden und 3 übersprungen, 0 fehlgeschlagen (Exit-Code 0) |

## Grenzen nach dem Review 0.90.0

* Die Revisionsprüfung (N06) schützt die Bezüge einer Antwort; sie ersetzt keine fachliche Neubewertung anderer,
  gleichzeitig geänderter Vorgänge.
* Mehrere Prozesse auf derselben Datenbank: Entscheidungen (N08) und Stapelaktionen (N06) sind über die
  SQLite-Schreibtransaktion serialisiert; der Probelauf selbst bleibt prozesslokal zwischengespeichert.
* Die Grenzen der Nachprüfung (konkurrierende Partner, Probelauf-Kosten) gelten weiter.

# Review vom 04.11.2026 zu 0.91.0 (Version 0.92.0)

Grundlage: `Portfolia_Buchungen_pruefen_Review_091_2026-11-04.md` mit Nachweispaket (`test_review_091.py`, 10 Fälle,
geprüft auf `bc612c0`, 0.91.0). **Reproduktion auf `bc612c0`: 5 Soll-Tests schlugen fehl** (N10 ×2, N11 ×2, N12), die
5 Kontrollfälle bestanden. **Auf dem Endstand bestehen alle 10.** Die Fälle sind mit unveränderten fachlichen
Erwartungen in `tests/test_review_091.py` übernommen und um eigene Fälle ergänzt; zwei weitere stehen in
`tests/test_reconciliation_pipeline.py` (Sammelvorschau, automatisches Verknüpfen).

## Gemeinsamer Vertrag: Revision abhängiger Buchungen (Grundlage für N10–N12)

* `service.ref_revs(conn, refs)`: Revision je abhängiger Buchung **aus der Datenbank** – Journalzeile, Änderung in der
  App (`tx_override`), Zeile des aktiven Imports; ohne technische Spalten (Kennung, Zeitstempel, Importnummer), also
  unverändert bei inhaltsgleichem Speichern. Abhängig sind Verknüpfungsziel, Dubletten-Bezug und Journal-Transferpartner
  (`batch.row_refs`).
* `batch.Plan.refs`: erwarteter Stand dieser Buchungen für alle geplanten Verknüpfungen/Buchungen; bei einer
  gespeicherten Transferentscheidung der Stand **zum Zeitpunkt der Entscheidung**. Er ist Teil von
  `Plan.fingerprint()` – eine aus der Anzeige stammende Vorschau ist damit an den angezeigten Stand gebunden.
* `batch.execute` prüft `Plan.refs` **immer** als erste Anweisung der Schreibtransaktion (`BEGIN IMMEDIATE`), zusätzlich
  einen vom Aufrufer übergebenen angezeigten Stand (`expect_refs`). Eine Änderung davor wird erkannt; eine danach wartet
  auf den Commit. Abgelehnt wird mit Rücknahme der Transaktion und ohne Teilzustand.
* Der ältere Weg „Buchungen übernehmen“ (`commit_apply`) prüft Transferentscheidungen zu Journalpartnern ebenfalls in
  seiner Transaktion.

| ID | Abgleich auf bc612c0 | Ursache | Änderung | Nachweis | Verbleibende Grenze |
| --- | --- | --- | --- | --- | --- |
| **N10** Detailaktionen | bestätigt: alte Detailvorschau (`action=link&force=1`) und alter Knopf „verknüpfen“ verknüpften nach Änderung des Ziels (EUR-Wert 1 → 7) | `expect_refs` war optional und nur im Assistenten übergeben; `Plan.fingerprint` band nur Zeile und Ziel-ID; der Einzelknopf bildete seinen Fingerabdruck erst beim Absenden | Vertrag oben: Fingerabdruck enthält den Stand der Abhängigkeiten, Prüfung in der Transaktion verbindlich. Detailseite rendert je Zeile mit Abhängigkeiten `rev_<Zeile>`; der Einzelknopf prüft ihn **vor** dem Speichern der übrigen Eingaben (sonst nichts gespeichert, nichts ausgeführt), übergibt ihn an `execute` und stellt bei einer Ablehnung danach die vorherigen Eingaben wieder her. Fehlt die Bindung bei einer Zeile mit Abhängigkeiten, wird abgelehnt. Sammelvorschau (`bulk.run`) nutzt den gerenderten Fingerabdruck und ist damit gebunden; automatisches Verknüpfen (Stufe A, ohne Anzeige) ist über `Plan.refs` von der Planung bis zur Transaktion gebunden | Reviewfälle ×2; eigene: alte Vorschau und alter Knopf nach Änderung von Wert, Menge, Konto, Gebühr, Löschstatus (10 Fälle; keine Verknüpfung, keine Stapelaktion, keine gespeicherte Nebeneingabe, erneutes Absenden weiterhin abgelehnt); neue Vorschau bzw. neuer Knopf genau einmal mit Rücknahme; echte parallele Änderung zwischen Prüfung und Schreiben (Verknüpfung, Journaltransfer); Dienstebene mit frisch gebildetem Fingerabdruck; Sammelvorschau und Stufe A mit parallelem zweitem Thread | Formulare aus 0.91.0 ohne `rev_` werden nach dem Update einmal abgelehnt (Seite neu laden) |
| **N11** Journaltransfer im Vorschauweg | bestätigt: nach gespeichertem „ja“ bzw. „nein“ blieb der Plan leer („Transfer mit einer bereits übernommenen Buchung – nur einzeln bestätigen“) | Die Planersperre für `j:`-Partner verlangte eine ausdrückliche Freigabe (`approved`), die nur die Quellenübernahme übergab; das gespeicherte `pair_ok` zählte nicht | Transferentscheidung zum Journalpartner speichert dessen Stand (`csv_row.pair_rev`, Migration 36; aus dem gerenderten Formular `pairrev_<Zeile>`). Der Planer wertet eine so gebundene Entscheidung als Antwort genau auf die Transferfrage (wie die Freigabe der Übernahme) – **ohne** weitere Sperren zu öffnen (kein `force`): Dubletten, Abweichungen, Unvollständiges, Mehrdeutigkeit bleiben gesperrt. Ändert sich der Partner danach, setzt die Bewertung die Entscheidung zurück und zeigt „Gegenbuchung … nach deiner Transferentscheidung geändert – bitte erneut entscheiden“ | Reviewfälle ×2 und Kontrollfälle (älterer Weg „Buchungen übernehmen“ ×2); eigene: gerenderte Formulare → ja/nein → Vorschau → Buchung → Bestände/Lots (Anschaffungsdatum fortgeführt) → erneutes Absenden ohne Doppelung → Rücknahme exakt, je mit und ohne Gebühr (4 Fälle); Partneränderung nach der Entscheidung (Wert, Menge, Konto, Gebühr, Löschen: 5 Fälle); veraltetes Entscheidungsformular; älterer Commit-Weg mit Partneränderung vor der Transaktion | Entscheidungen zu Journalpartnern, die vor 0.92.0 gespeichert wurden, haben keinen Partnerstand; die Bewertung setzt sie sichtbar zurück – sie müssen auf jedem Weg einmal neu bestätigt werden (bewusst: der Partner kann sich seitdem geändert haben). Paare mit einem Journalpartner werden ohne ausdrückliche Entscheidung auf keinem Weg gebucht (unverändert) |
| **N12** Übernahme mit Journalpartner | bestätigt: Änderung des Partners (55 → 777 €) zwischen Planung und Buchung → Zusammenführung, `exact=True` | `_apply_steps` übergab `approved`, aber keinen erwarteten Partnerstand; die Mengen-Nachprüfung erkennt reine Wertänderungen nicht | Die Entscheidung speichert den Partnerstand aus der bestätigten Vorschau (`partner.rev`); `_validate_decisions` prüft ihn; `_apply_decisions` gibt **diesen** Stand an den Prüf-Stapel (`pairrev_`), nicht den jetzigen; `execute` prüft ihn in der Buchungstransaktion. Abweichung → Ablehnung und vollständige Kompensation (Ausblendung, Stapel); die Fremdänderung bleibt. Ausblendungen und ersetzte Gegenbuchungen des Laufs sind eigene Planschritte und gehen nicht in die Partnerprüfung ein. Eine abgelehnte Entscheidung legt keine Zeilensperre mehr ab: sie erscheint als „verworfen“, nach dem Entfernen bietet der Probelauf neu an | Reviewfall; eigene: Änderung von Wert, Gebühr, Menge, Konto, Löschstatus jeweils vor der Ausblendung und vor der Buchung (10 Fälle) – abgelehnt, Lauf `staged` ohne Bezüge, kein Wiederherstellungsbedarf, Fremdänderung erhalten; Wiederanlauf: verworfene Entscheidung entfernen, neu entscheiden, genau eine Übernahme, zweites Absenden abgelehnt, Rücknahme; unveränderter Partner weiterhin zusammengeführt und exakt zurückgenommen | Import-Gegenbuchungen anderer Konten (`imp`) werden im selben Lauf ausgeblendet und aus dem bestätigten Stand ersetzt; ihre Prüfung bleibt die der Bewertung (`_tx_basis`) |

## Teststeuerung

* Nebenläufigkeit: Die Fälle ersetzen `BA.execute` (Reviewfall N12), `BA._execute_tx`, `A.apply_plan` bzw.
  `commit_plan` durch Wrapper, die einen echten zweiten Thread über den regulären Journaldienst ändern lassen und dann
  den unveränderten Dienst aufrufen. Die Prüfung liegt in der Schreibtransaktion, also hinter diesen Punkten.
* **Angepasst:** `tests/test_transfer_side.py` und `tests/test_assess_matching.py` sendeten den Einzelknopf ohne die
  gerenderten Felder. Sie verwenden jetzt `tests.helpers.row_action`, das wie ein Browser den auf der Detailseite
  angezeigten Stand (`rev_<Zeile>`) mitsendet; die fachlichen Erwartungen sind unverändert.

## mypy

Der CI-Schritt ist ausdrücklich nur meldend (`continue-on-error: true`, `|| true`) – grün belegt keine Fehlerfreiheit.
Lokal mit demselben Aufruf (`mypy app/news app/jobs app/web app/health.py app/logging_setup.py --ignore-missing-imports`):
**334 Fehler in 61 Dateien** auf `bc612c0` **und** auf dem Endstand (Ausgabe zeilenweise ohne Zeilennummern identisch);
in den geänderten Modulen `csvimport/batch.py`, `csvimport/module.py`, `csvimport/service.py`, `datasources/takeover.py`
jeweils 39 bestehende Fehler vorher und nachher. Die Änderung führt also keine neuen Typfehler ein; der Bestand bleibt
offen (vor allem `datasources/service.py` 67, `diagnosis/audit.py` 31, `documentimport/bridge.py` 25,
`csvimport/service.py` 23). Der CI-Schritt gibt jetzt die Gesamtzahl aus (`--no-error-summary` entfernt), bleibt aber
nicht blockierend.

## Tests und Befehle (0.92.0)

| Befehl | Ergebnis |
| --- | --- |
| `ruff check app tests scripts` | ohne Befund |
| `python -m app validate examples/beispiel-import.zip` | 0 Fehler, 0 Warnungen, 124 Transaktionen |
| Reviewfälle (`test_review_091.py`) auf `bc612c0` | 5 fehlgeschlagen, 5 bestanden |
| `tests/test_review_091.py` (47) + `tests/test_reconciliation_pipeline.py` (7, davon 2 neu) auf `bc612c0` | 33 fehlgeschlagen (5 Reviewfälle, 26 eigene, 2 neue Abgleichsfälle); 21 bestanden (5 Kontrollfälle, 11 eigene Kontroll-/Sicherungsfälle, z. B. Änderungen, bei denen die Zuordnung schon vorher entfiel, 5 bestehende Abgleichsfälle) |
| dieselben 54 auf dem Endstand | 54 bestanden |
| `mypy` wie in CI | 334 Fehler in 61 Dateien – identisch zu `bc612c0` |
| Volltest `TZ=Europe/Berlin pytest -q -x tests` (Worktree des Endstands) | 2185 Tests, davon 2182 bestanden und 3 übersprungen, 0 fehlgeschlagen (Exit-Code 0) |

## Grenzen nach dem Review 0.91.0

* Geprüft werden die Buchungen, von denen die jeweilige Aktion abhängt; andere gleichzeitig geänderte Buchungen ohne
  Bezug blockieren nicht (gewollt).
* Der Probelauf der Übernahme bleibt prozesslokal zwischengespeichert; maßgeblich ist die Prüfung im echten Lauf.
* Bestehender mypy-Bestand (s. o.) und die Grenzen der früheren Runden (konkurrierende Partner, reine EUR-Gebühr ohne
  Asset) gelten weiter.

# Review vom 04.11.2026 zu 0.92.0 (Version 0.93.0)

Grundlage: `Portfolia_Buchungen_pruefen_Review_092_2026-11-04.md` mit Nachweispaket (`test_review_092.py`, 7 Fälle,
geprüft auf `023f11d`, 0.92.0). **Reproduktion auf `023f11d`: 3 Soll-Tests schlugen fehl** (N13 mit späterer Eingabe,
N14 „kein Transfer“ in der Übernahme, N14 Transferformular ohne `pairrev_`), die 4 Kontrollfälle bestanden (N13 ohne
spätere Eingabe, alter Einzelknopf nach neuer Quellidentität, N14 „Transfer bestätigen“, Formular mit Revisionsfeld).
**Auf dem Endstand bestehen alle 7.** Die Fälle stehen mit unveränderten fachlichen Erwartungen in
`tests/test_review_092.py`, ergänzt um eigene Fälle (insgesamt 44).

| ID | Abgleich auf 023f11d | Ursache | Änderung | Nachweis | Verbleibende Grenze |
| --- | --- | --- | --- | --- | --- |
| **N13** Fehlerrücknahme überschreibt neuere Eingaben | bestätigt: nach abgelehnter Verknüpfung wurden „zurückgestellt“ und EUR-Wert 123 eines anderen Tabs auf leer gesetzt | `row_action` sicherte die Eingaben **aller** Zeilen und schrieb sie nach der Ablehnung unbedingt zurück; Sicherung, Speichern, Ausführen und Rücknahme waren getrennte Aufrufe | `set_rows` liest und schreibt die Eingaben in **einer** Schreibtransaktion und liefert den Rücknahmeauftrag nur der von diesem Aufruf **tatsächlich geänderten** Zeilen (vorher/geschrieben). `restore_inputs` setzt in einer Schreibtransaktion **feldweise** zurück – nur Felder, die dieser Aufruf geändert hat und die noch genau den von ihm geschriebenen Wert tragen; gebuchte/verknüpfte Zeilen bleiben unberührt. Die alte Gesamtsicherung (`inputs`) entfällt | Reviewfälle (mit/ohne spätere Eingabe); eigene: Entscheidung, EUR-Wert, Gebührenwert einzeln und zusammen auf einer anderen Zeile; Änderung desselben Felds derselben Zeile durch einen anderen Tab (bleibt, übrige eigene Felder zurück); eigene abgelehnte Eingaben ohne Fremdänderung vollständig zurück; wiederholte Rücknahme ändert nur noch eigene Werte; Transferbestätigung samt Partnerstand eines anderen Stapels bleibt | Ein anderer Aufruf, der zufällig **denselben** Wert in dasselbe Feld schreibt, ist nicht unterscheidbar – der Wert wird dann (gleichlautend) zurückgenommen. Die vom Speichern ausgelöste Neubewertung bleibt (abgeleitete Felder) |
| **N14** Fehlende Partnerrevision durch aktuellen Stand ersetzt | bestätigt: (A) Übernahme mit „kein Transfer“ lief nach Änderung des Partners (55 → 777 €) durch; (B) Formular ohne `pairrev_` speicherte „ja“ mit dem **jetzigen** Stand, „Buchungen übernehmen“ führte den geänderten Partner zusammen | `set_rows` fiel bei fehlendem `pairrev_` auf `ref_revs(...)` (Stand beim Speichern) zurück; der `nopair`-Zweig der Übernahme gab keinen Partnerstand mit | Kein Rückfall mehr: Eine Transferentscheidung („ja“ oder „nein“) zu einem Journalpartner ohne bzw. mit leerem `pairrev_` wird **nicht gespeichert** und sichtbar gemeldet („… ohne angezeigten Stand der Gegenbuchung – nicht gespeichert“) – im Formular (Detailseite zeigt den Fehler) wie bei internen Aufrufern (`set_rows` gibt den Fehler zurück). Die Übernahme gibt bei **beiden** Entscheidungen Partnerkennung und Stand aus der bestätigten Vorschau mit; bei konkurrierenden Partnern speichert die Entscheidung die Stände aller angebotenen Journalpartner und prüft genau den jetzt vorgeschlagenen. Abweichung → abgelehnt, Übernahme vollständig kompensiert, Fremdänderung bleibt; die Entscheidung erscheint als „verworfen“ | Reviewfälle (A: pair/nopair, B: mit/ohne Feld); eigene: fehlendes, leeres und nur aus Leerzeichen bestehendes Feld bei „ja“ und „nein“ (6 Fälle, danach kein Transfer über „Buchungen übernehmen“); interner Aufruf ohne Stand; Übernahme mit „Transfer bestätigen“ und „kein Transfer“, Änderung von Wert, Konto, Menge, Gebühr, Löschstatus jeweils vor dem Anwenden der Entscheidungen und vor der Buchung (20 Fälle); neue Bestätigung nach Ablehnung → genau eine Übernahme → zweites Absenden abgelehnt → vollständige Rücknahme (beide Entscheidungen) | Formulare und Skripte, die Transferentscheidungen zu Journalpartnern ohne den angezeigten Stand absenden, werden abgelehnt (gewollt) |

## Teststeuerung

* Nebenläufigkeit: Wrapper um `BA._execute_tx`, `svc.restore_inputs`, `TK._apply_decisions` bzw. `BA.execute` lassen
  einen echten zweiten Thread über die regulären Dienste schreiben und rufen danach den unveränderten Dienst auf.
* **Angepasst:** `tests/test_csvimport.py::test_transfer_matching_across_imports_and_revert` und
  `tests/test_wallets_evm.py::test_exchange_withdrawal_and_wallet_deposit_become_one_transfer` sendeten „ja“ zu einem
  Journalpartner ohne Partnerstand. Sie verwenden jetzt `tests.helpers.transfer_decision` bzw. `shown_pair_rev` (Stand
  von der gerenderten Detailseite); die fachlichen Erwartungen sind unverändert. `tests/test_review_091.py`:
  `_takeover_with_edit` kennt zusätzlich die Entscheidung („pair“/„nopair“) und den Eingriffspunkt „vor dem Anwenden der
  Entscheidungen“.

## mypy

Der CI-Schritt bleibt nur meldend – grün belegt keine Fehlerfreiheit. Lokal (Python 3.11, derselbe Aufruf wie in CI):
siehe Tabelle unten; CI (Python 3.12) meldete für 0.92.0 333 Fehler in 61 Dateien.

## Tests und Befehle (0.93.0)

| Befehl | Ergebnis |
| --- | --- |
| `ruff check app tests scripts` | ohne Befund |
| `python -m app validate examples/beispiel-import.zip` | 0 Fehler, 0 Warnungen, 124 Transaktionen |
| Reviewfälle (`test_review_092.py`) auf `023f11d` | 3 fehlgeschlagen, 4 bestanden |
| `tests/test_review_092.py` (44) auf `023f11d` | 18 fehlgeschlagen, 26 bestanden (Kontroll- und Sicherungsfälle, u. a. „Transfer bestätigen“ in der Übernahme und Änderungen, die schon 0.92.0 ablehnte) |
| dieselben auf dem Endstand | 44 bestanden |
| `mypy` wie in CI, lokal (Python 3.11) | 334 Fehler in 61 Dateien auf `023f11d` und auf dem Endstand – zeilenweise identisch; in den geänderten Modulen `csvimport/service.py`, `csvimport/module.py`, `datasources/takeover.py` 36 bestehende Fehler, unverändert. Keine neuen Typfehler, Bestand offen |
| Volltest `TZ=Europe/Berlin pytest -q -x tests` (Worktree des Endstands) | 2229 Tests, davon 2226 bestanden und 3 übersprungen, 0 fehlgeschlagen (Exit-Code 0) |

## Grenzen nach dem Review 0.92.0

* N13: gleichlautende Fremdeingabe nicht unterscheidbar (s. o.); Eingaben werden weiterhin **vor** der Aktion
  gespeichert (die Planung braucht sie) und bei Ablehnung kompensiert, nicht in derselben Transaktion wie die Aktion.
* Die Grenzen der früheren Runden (konkurrierende Partner nur „kein Transfer“, reine EUR-Gebühr ohne Asset,
  mypy-Bestand) gelten weiter.

# Systematische Überarbeitung nach dem Doppelcheck zu 0.93.0 (Version 0.94.0)

Grundlage: `Portfolia_Buchungen_pruefen_Review_093_2026-11-04.md` mit Nachweispaket (`test_review_093.py`, 5 Fälle,
geprüft auf `ecf69ff`). **Reproduktion auf `ecf69ff`: 2 Soll-Tests schlugen fehl** (N15: geänderter, nicht
vorgeschlagener Partner nach dem Anwenden der Entscheidung; N16: Journaländerung in derselben gespeicherten Sekunde →
alte Vorschau, Entscheidungsschleife), 3 Kontrollen bestanden. **Auf dem Endstand bestehen alle 5.** Ziel dieser
Runde war nicht die Einzelkorrektur, sondern die gemeinsame Fehlerklasse: veraltete Bestätigungen, unvollständig
geprüfte Abhängigkeiten, Entscheidungsschleifen und verlorene Fremdeingaben.

## Gemeinsame Lösung

| Baustein | Umsetzung | Ersetzt |
| --- | --- | --- |
| **Persistente Schreibrevisionen** | Migration 37: Tabelle `data_rev` mit je einem Zähler für jede Tabelle, aus der Vorschau, Prüfungen und Übernehmen kommen (`db.REV_TABLES`: Journal, App-Änderungen, Verknüpfungen, Aliase, Zuordnungen, Bestandsnachweise, Zusammenführungen, Ereignis-Entscheidungen, aktiver Import, Einstellungen `ledger.`/`tax.`). SQLite-Trigger erhöhen ihn **in derselben Transaktion** wie jede Änderung. `actions.data_version` liest nur diese Zähler (Index-Zugriff statt Zählungen) | Anzahl + jüngster sekundengenauer Zeitstempel je Tabelle (N16: gleiche Sekunde, gleiche Zeilenzahl, Änderung ohne `updated_at` blieben unerkannt) |
| **Ein Abhängigkeitsvertrag je Plan** | `batch.plan_deps`: Revision jeder abhängigen Buchung (Verknüpfungsziel, Dubletten-Bezug, **alle** Journal-Kandidaten eines möglichen Transfers) und für entschiedene Transferfragen der Stand der **gesamten Kandidatenmenge** (`service.cands_rev`: Zusammensetzung + Inhalt aller Kandidaten). Teil des Fingerabdrucks; `batch.execute` prüft beides als erste Anweisung der Schreibtransaktion (`check_deps`), dazu die Zähler der Transfer-Tabellen seit **vor** der Bewertung (`check_tables`: neue/entfernte Kandidaten). `row_state` enthält zusätzlich konkurrierende Kandidaten, Import-Gegenstück und Entscheidungsstand | Einzelpartner-Revision (N15: nicht vorgeschlagene Alternativen ungeprüft) |
| **Transferentscheidung = Antwort auf eine Kandidatenmenge** | `csv_row.pair_rev` speichert den angezeigten Stand der Kandidatenmenge (`pairrev_<Zeile>`, Pflicht bei Journal-Kandidaten, kein Rückfall). Die Bewertung setzt eine Entscheidung zurück, sobald Zusammensetzung oder Inhalt abweicht, und zeigt den Grund; der Planer wertet nur gebundene Entscheidungen (`decided_pair`). Quellenübernahme: Entscheidung speichert `rev` (Kandidatenmenge) und `cands`; `_apply_decisions` übergibt genau diesen Stand für „Transfer bestätigen“ und „kein Transfer“ | Partnerstand nur des vorgeschlagenen Partners |
| **Besitzkennungen der Eingaben** | Migration 37: `csv_row.ver_dec/ver_val/ver_fee/ver_pair` je Eingabegruppe. Jede inhaltliche Änderung erhält eine neue, global eindeutige Kennung (`set_rows` explizit, alle übrigen Schreiber per Trigger). `restore_inputs` setzt eine Gruppe nur zurück, solange sie **genau die eigene Kennung** trägt, und schreibt selbst mit neuer Kennung (wiederholte/verspätete Aufträge finden nichts). Transferentscheidung und Partnerstand sind eine Gruppe | Wertvergleich (Grenze N13: gleichlautende Fremdeingabe, A → B → A) |
| **Veraltete Formulare** | Detailseite rendert je Zeile den angezeigten Eingabestand (`iv_<Zeile>`: Version + Prüfsumme je Gruppe). Ein veraltetes Formular überschreibt keine neuere Eingabe: unberührte Felder werden übergangen, eine eigene abweichende Änderung sichtbar abgelehnt („inzwischen an anderer Stelle geändert“), derselbe Wert ausdrücklich übernommen | „Letzter Speichervorgang gewinnt“ für alle Felder der Seite |
| **Probelauf-Rückmeldung** | Kann der Probelauf eine gespeicherte Übernahme-Entscheidung im Zustand nach dem Ausblenden nicht anwenden (z. B. neuer Kandidat), erscheint sie sofort als „verworfen“ – Entfernen → neues Angebot → neue Entscheidung | Entscheidung galt in der Vorschau als gültig und scheiterte erst beim Übernehmen |

## Umgestellte Aufrufer

`batch.plan`/`execute` (alle Stapelaktionen), `module.preview`/`execute`/`row_action`/`rows`, `service.set_rows`/
`restore_inputs`/`commit`/`commit_apply`/`evaluate`, `assistant_web._stack_answer` (Transferantwort: gemeinsame
Rücknahme statt unbedingtem Zurückschreiben von `pair_ok` – dieselbe Fehlerklasse wie N13, in dieser Runde gefunden),
`takeover._choice_info`/`_validate_decisions`/`_apply_decisions`/`probe`/`_probe_checks`, `actions.data_version`/
`assert_version` (alle Vorschau-/Cache-Nutzer: Diagnose-Korrekturen, Sammelbearbeitung, Klärung, Arbeitsplan,
Integrität, Übernahme-Probelauf). `bulk.run` und `auto_link` nutzen den Vertrag über `batch.execute` unverändert.

## Wegematrix (abgeschlossen)

| Einstieg | Aktion | Betroffene Daten | Abhängigkeiten | Gültigkeitsprüfung | Schreibtransaktion | Rücknahme | Nachweis |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Assistent (Karte) | verknüpfen, buchen, Transfer ja/nein, nicht übernehmen, zurückstellen | Prüf-Stapel, Journal, Verknüpfung | eigene Zeilen, Partnerzeilen, Verknüpfungsziel, Journalpartner | `x_fp` (Wirkungsmenge), `x_rev` + `Plan.refs/pairs` in der Transaktion | `batch.execute` | Protokoll-Rückgängig (`batch.undo`); abgelehnte Transferantwort: `restore_inputs` | `test_review_090` (N06), `test_assistant*`, `test_review_fixes` |
| Detail: Vorschau → Ausführen | alle Stapelaktionen | wie oben | wie oben + Kandidatenmengen | Fingerabdruck (inkl. Abhängigkeiten) + Prüfung in der Transaktion + Tabellenzähler | `batch.execute` | Verlauf → Rückgängig | `test_review_091`, `test_review_093` (Detail-Matrix) |
| Detail: Einzelknopf | verknüpfen, übernehmen | Eingaben der Seite + Aktion | Zeilenabhängigkeiten (`rev_`) | `rev_` vor dem Speichern, `expect_refs` in der Transaktion | `set_rows` + `batch.execute` | `restore_inputs` (Besitzkennung) | `test_review_091`, `test_review_092`, `test_review_093` |
| Detail: Eingaben speichern | Entscheidung, Wert, Gebühr, Transfer | `csv_row` | angezeigter Eingabestand (`iv_`), Kandidatenstand (`pairrev_`) | in derselben Transaktion | `set_rows` | – (keine Buchung) | `test_review_092`, `test_review_093` |
| Detail: „… Buchungen übernehmen“ | Stapel übernehmen | Journal, Transfers | gebundene Transferentscheidungen, Kandidatenmenge, Tabellenzähler | `check_pairs` + `check_tables` in der Transaktion | `commit` | „Import rückgängig“ (`revert`) | `test_review_091/092/093` (Commit-Weg) |
| Sammelvorschau (Prüfwarteschlange) | sichere Vorschläge mehrerer Stapel | wie Stapelaktion | wie Stapelaktion | gerenderter Fingerabdruck je Stapel + Transaktion | `batch.execute` je Stapel | Verlauf je Stapel | `test_reconciliation_pipeline` (Sammelvorschau), `test_review_bulk` |
| Automatische Verknüpfung (Stufe A) | sichere Verknüpfung ohne Anzeige | Verknüpfung | Ziel | `Plan.refs` von der Planung bis zur Transaktion | `batch.execute` | Verlauf | `test_reconciliation_pipeline` (paralleler Thread) |
| API-Import / Hintergrundabruf | Übernahme eindeutiger Ereignisse | Journal | wie Commit | wie Commit | `commit(only_idx)` | Verlauf/Import rückgängig | `test_bitpanda*`, `test_wallets*`, `test_transfer_side` |
| Quellenübernahme: Vorschau | Probelauf | – (Kopie) | Datenstand (`data_version`) | Cache-Schlüssel = Lauf + Token + Schreibzähler | keine (Kopie) | – | `test_review_093` (N16, Wiederverwendung, Neustart) |
| Quellenübernahme: Entscheidung | entscheiden/entfernen | `app_state` | Lauf, Token, Entscheidungsstand | Schreibtransaktion | `decide`/`undecide` | Entfernen | `test_review_090` (N08), `test_review_093` |
| Quellenübernahme: Übernehmen | Ausblenden, Auswerten, Entscheidungen, Buchen, Verknüpfen, Nachprüfung | Ausblendung, Stapel, Journal | Datenstand (Ausblenden), Kandidatenmengen (Entscheidungen), Plan-Abhängigkeiten (Buchen) | `assert_version`; `pairrev_` aus der Entscheidung; `check_deps`/`check_tables` | je Phase eine Transaktion mit Rücknahmebezug (`in_tx`) | vollständige Kompensation; `undo`/`recover` fortsetzbar | `test_nachpruefung` (N01/N03), `test_review_091/092/093` |
| Rücknahme | Rückgängig, Wiederherstellen | alle obigen | eigene Bezüge | idempotent, fortsetzbar | je Schritt | – | `test_nachpruefung`, `test_review_090` |

## Zuordnung aller Befunde

| Befund | Stand | Nachweis |
| --- | --- | --- |
| R01–R14, V01–V05 (Review 03.11.) | geschlossen in 0.89.0/0.90.0; Tests unverändert grün | `test_review_fixes`, `test_review_repair`, `test_review_workflow`, `test_review_bulk` |
| N01 Commitbezüge, N03 fortsetzbare Rücknahme, N04 Integritätsprotokoll | geschlossen (0.90.0) | `test_nachpruefung` |
| N02 Wirkungsmenge der Assistentenkarte | geschlossen (0.90.0), seit 0.94.0 mit Kandidatenmengen im `row_state` | `test_nachpruefung`, `test_review_090` |
| N05 Gebühren ersetzter Gegenbuchungen | geschlossen (0.91.0) | `test_review_090` |
| N06 Bestätigung atomar (Assistent) | geschlossen (0.91.0), Teil des gemeinsamen Vertrags | `test_review_090` |
| N07 Einzelbuchung, N08 Entfernen gebunden, N09 Anzeige angewendeter Entscheidungen | geschlossen (0.91.0) | `test_review_090` |
| N10 Detailaktionen gebunden | geschlossen (0.92.0), Vertrag erweitert | `test_review_091`, `test_review_093` |
| N11 Journaltransfer im Vorschauweg | geschlossen (0.92.0), Bindung an Kandidatenmenge | `test_review_091`, `test_review_093` |
| N12 Journalpartner in der Übernahme atomar | geschlossen (0.92.0) | `test_review_091` |
| N13 Fehlerrücknahme ohne Fremdverlust | geschlossen (0.93.0); **offengelegte Grenze (gleichlautende Fremdeingabe, A → B → A) in 0.94.0 geschlossen** (Besitzkennungen) | `test_review_092`, `test_review_093` |
| N14 Partnerstand ohne Rückfall | geschlossen (0.93.0) | `test_review_092` |
| **N15** alle angebotenen Partner bis zum Commit gebunden | **geschlossen (0.94.0)** | Reviewfälle + Matrix in `test_review_093` |
| **N16** zuverlässige Datenrevision, keine Entscheidungsschleife | **geschlossen (0.94.0)** | Reviewfälle + Matrix in `test_review_093` |
| neu: Assistent setzte nach abgelehnter Transferantwort `pair_ok` unbedingt zurück | **geschlossen (0.94.0)** – gemeinsame Rücknahme | `test_assistant*` |
| neu: neuer Kandidat in der Übernahme erst beim Übernehmen erkannt | **geschlossen (0.94.0)** – Probelauf-Rückmeldung | `test_review_093::test_takeover_new_candidate…` |
| neu: veraltetes Formular überschrieb neuere Eingaben anderer Tabs beim Speichern | **geschlossen (0.94.0)** – `iv_` | `test_review_093::test_stale_form…` |

## Testmatrix (`tests/test_review_093.py`, 46 Fälle)

| Bereich | Aktion | Abhängigkeit | Zeitpunkte | Fälle |
| --- | --- | --- | --- | --- |
| Reviewfälle | Übernahme „kein Transfer“ / „Transfer bestätigen“ | alle Kandidaten / Journalpartner | nach Anwenden der Entscheidung; gleiche Sekunde | 5 |
| Datenrevision | jede Schreibart | Journal | gleiche Sekunde, ohne `updated_at`, A → B → A, nur lesen | 1 |
| Vorschaucache | Probelauf | Datenstand | unverändert, nach Neustart | 1 |
| Übernahme, mehrere Kandidaten | „kein Transfer“ | jeder Kandidat × Wert/Menge/Konto/Status | nach der Bewertung, nach dem Anwenden der Entscheidungen, unmittelbar vor dem Commit | 24 |
| Übernahme, Kandidatenmenge | „kein Transfer“ | neuer Kandidat | nach der Entscheidung → verworfen → neu entscheiden → Übernahme → exakte Rücknahme | 1 |
| Übernahme, Wiederanlauf | „kein Transfer“ | Alternative | vor dem Commit → abgelehnt → neu → genau einmal → Rücknahme (auch wiederholt) | 1 |
| Detail, mehrere Kandidaten | gerendertes Formular → nein → Vorschau → Buchung → Rücknahme | alle Kandidaten | – | 1 |
| Detail, mehrere Kandidaten | Vorschau/Ausführen | jeder Kandidat | vor der Vorschau, nach der Vorschau, vor dem Commit | 6 |
| Detail, Kandidatenmenge | Ausführen | neuer Kandidat | vor dem Commit → abgelehnt → neue Anzeige → genau eine Buchung | 1 |
| Commit-Weg | „Buchungen übernehmen“ | Alternative | vor der Transaktion | 1 |
| Eingaben | Rücknahme | Besitz | gleichlautende Fremdeingabe, A → B → A, wiederholt, verspätet | 2 |
| Eingaben | veraltetes Formular | angezeigter Stand | unberührt, abweichend, gleicher Wert, frisch | 1 |
| Eingaben | Transfergruppe | Entscheidung + Partnerstand | Rücknahme als Gruppe | 1 |

## Teststeuerung

* `tests/test_diagnosis_actions.py::LOG_TABLES` nimmt `data_rev` aus der Nutzdaten-Prüfsumme aus (Zähler zählen jede
  Schreiboperation, auch Rücknahmen – Metadaten wie Protokolle). Dass eine **Vorschau nichts schreibt**, prüft
  `test_diagnosis.fingerprint` weiterhin **einschließlich** der Zähler.
* `tests/test_review_092.py::test_repeated_restore_never_overwrites_newer_inputs` erwartet jetzt die stärkere Zusage
  (gleichlautende spätere Eingabe bleibt) statt der in 0.93.0 dokumentierten Grenze.
* Nebenläufigkeit über echte zweite Threads; Wrapper wählen nur den Zeitpunkt und rufen den unveränderten Dienst auf.

## Grenzen

* Formulare **ohne** `iv_` (vor 0.94.0 geöffnet, eigene Skripte) speichern wie bisher (letzter Speichervorgang gewinnt);
  Formulare der Anwendung tragen das Feld immer.
* Die Prüfung der Tabellenzähler für Transfer-Kandidaten ist bewusst grob: jede Journaländerung zwischen Bewertung und
  Commit einer transferrelevanten Aktion führt zur Ablehnung (sicher, im Millisekundenfenster selten).
* Kandidaten im selben Stapel (`b:`) zählen mit ihrer Kennung zur Kandidatenmenge; ihr Inhalt ist über `row_state`
  gebunden, nicht über `pair_rev`.
* Konkurrierende Partner: gezielte Wahl eines bestimmten Partners weiterhin nicht angeboten; reine EUR-Gebühr ohne
  Asset weiterhin nicht übertragbar; mypy-Bestand (s. u.).

## Tests und Befehle (0.94.0)

| Befehl | Ergebnis |
| --- | --- |
| `ruff check app tests scripts` | ohne Befund |
| `python -m app validate examples/beispiel-import.zip` | 0 Fehler, 0 Warnungen, 124 Transaktionen |
| Reviewfälle (`test_review_093.py`) auf `ecf69ff` | 2 fehlgeschlagen, 3 bestanden |
| `tests/test_review_093.py` (46) auf `ecf69ff` | 38 fehlgeschlagen, 8 bestanden (Kontrollfälle ohne Änderung bzw. mit Änderung des vorgeschlagenen Partners und Detailfälle, die schon 0.93.0 ablehnte) |
| dieselben auf dem Endstand | 46 bestanden |
| `mypy` wie in CI, lokal (Python 3.11) | 334 Fehler in 61 Dateien auf `ecf69ff` und auf dem Endstand – zeilenweise identisch. In den geänderten Modulen (`csvimport/service.py`, `batch.py`, `module.py`, `datasources/takeover.py`, `diagnosis/actions.py`, `assistant_web.py`, `db.py`) 57 → 56 bestehende Fehler (einer entfällt mit dem entfernten `_restore_pairs`); fünf zwischenzeitlich eingeführte Typfehler wurden vor dem Commit behoben. Keine neuen Typfehler, Bestand offen |
| Volltest `TZ=Europe/Berlin pytest -q -x tests` (Worktree des Endstands) | 2275 Tests, davon 2272 bestanden und 3 übersprungen, 0 fehlgeschlagen (Exit-Code 0) |
