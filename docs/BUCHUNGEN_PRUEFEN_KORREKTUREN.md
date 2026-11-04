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
