# Financial Integrity – Abschlussbericht

Auftrag „Portfolia Financial Integrity – Verbindlicher Master-Implementierungsauftrag“. Steuerungsdatei mit Detailplanung
je Phase: `docs/FINANCIAL_INTEGRITY_IMPLEMENTATION_PLAN.md`. Alle Angaben in diesem Bericht sind am Code bzw. an
Testläufen geprüft; Zahlen aus realen Daten nur aggregiert (keine Beträge, Adressen oder Kennungen).

## 16.1 Ausgangszustand

* **Ausgangscommit:** `ebc06dc` (Version 0.26.0, M29), CI grün.
* **Branch:** `claude/portfolia-dashboard-s9p6zr`.
* **Vorhandene Funktionen (wiederverwendet, nicht dupliziert):** CSV-Pipeline mit ≈ 40 Profilen und eigener
  Spaltenzuordnung (`app/csvimport`), Prüf-Stapel mit Stapelaktionen, Vorschau, Prüfsumme und Undo (`batch.py`),
  Abgleich per Hash/Transfer-Seite (`assess.py`, `reconcile.py`, `transfer_side.py`), Datenquellen mit Sync und
  lesender Evidenzrecherche (`app/datasources`), Diagnose mit Bestandsklärung A/B/C, Abweichungsfenstern,
  Rekonstruktionskandidaten (`app/diagnosis`), FIFO-Ledger mit Lots (`app/ledger/engine.py`), Steuerpaket DE,
  Gesamtexport/Restore (`app/fullexport.py`), Belegimport (`app/documentimport`).
* **Bestätigte Fehlerursachen** (Details: Plan §2):
  1. sieben unterschiedliche Definitionen von „offen“ (Stapel, Übersicht, Datenquelle, Statuslogik, Warteschlange,
     Verwerf-Check) – dieselben Zeilen ergaben „57 offen“ und „0 zu entscheiden“;
  2. Stapel schlossen nie (Sync-Stapel mit nur verknüpften/ausgelassenen Zeilen, Zeilen vor dem Stichtag) bzw. zu früh
     (CSV-Stapel beim Übernehmen trotz ungeklärter Zeilen);
  3. „Jetzt prüfen“ öffnete nur den jüngsten Stapel, die Zähler summierten über alle;
  4. ungeklärte/unvollständige Zeilen ließen sich nicht abschließen (kein „nicht übernehmen“, kein „zurückstellen“);
  5. Nullanzeigen ohne Grund;
  6. Zeilen vor dem Stichtag per Auswahl „verknüpfbar“ (z. B. 79 „ausführbare Aktionen“);
  7. BNB-Bestandsentscheidungen kippten nach 48 h (Abrufzustand im Befundschlüssel und in der Prüfsumme);
  8. ein wiederholter Abruf legte erledigte Vorgänge erneut vor (`_present` kannte nur offene Stapel).
  Nicht bestätigt: Fehler im Matching selbst, veraltete Auswahl, Vorschau auf anderem Datenstand.

## 16.2 Prüfworkflow

* **Warum Stapel nicht abgeschlossen wurden:** `refresh_status` bewertete nur Sync-Stapel im Zustand `partial` und
  verlangte auch für Zeilen vor dem Stichtag eine Entscheidung; Stapel aus Verknüpfungen/Auslassungen blieben
  `preview`. Jetzt leitet `refresh_status` den Zustand in beide Richtungen aus der zentralen Zustandsfunktion ab
  (`committed`/`done` ↔ `partial`/`preview`) – nach jeder Änderung, beim Undo und einmalig beim Start
  (`reconcile_statuses`, protokolliert).
* **Warum die automatische Prüfung 0 zeigte:** Die Übersicht zählte nur Zeilen mit sicherem Vorschlag im jüngsten
  Stapel; Fälle in älteren Stapeln, manuelle Fälle und Historie fielen heraus, und eine 0 trug keinen Grund. Jetzt
  zählt `review.summarize` Vorgänge über alle aktiven Stapel, `review.funnel` liefert die Stufen (Rohzeilen → gültig →
  Vorgänge → bereits vorhanden → offen → sicher/manuell → ausführbar) und `zero_reason` den zutreffenden Grund
  (alles vorhanden, keine gültigen Daten, alles manuell, Zuordnung fehlt, EUR-Wert fehlt, vor Stichtag, abgeschlossen,
  Auswertung fehlgeschlagen, noch nicht ausgewertet).
* **Warum die manuelle Prüfung 0 zeigte:** `_review_queue` ignorierte Entscheidungen und kannte nur einen Stapel;
  „unklar“/„ungültig“ waren nicht entscheidbar. Jetzt erreicht `/journal/pruefung` jeden offenen Vorgang aller Stapel,
  mit Entscheidungen *nicht übernehmen* und *zurückstellen* für alle offenen Zeilenarten (umkehrbar).
* **Zentrale Statusauswertung:** `app/csvimport/review.py` – `state_of(row)` ist die einzige Definition (offen ·
  sicherer Vorschlag · vorgemerkt · zurückgestellt · bereits berücksichtigt · verknüpft · übernommen · nicht
  übernommen · ignoriert · vor Stichtag). Stapelseite, Warteschlange, Datenquellen und Stapelstatus leiten daraus ab;
  Zähleinheit ist der wirtschaftliche Vorgang (Ereignis-ID).
* **Fachlicher Abschluss eines Stapels:** abgeschlossen, wenn kein Vorgang in einem der vier offenen Zustände (offen,
  sicherer Vorschlag, vorgemerkt, zurückgestellt) ist; vor-Stichtag-Zeilen sind Historie und halten keinen Stapel offen.
  Abgeschlossene, nicht gebuchte Ereignisse werden nicht erneut vorgelegt – außer die Lieferung hat sich geändert
  (neue Evidenz, Prüfsumme `_rec_fp`).
* **UI-End-to-End-Tests:** `tests/test_review_workflow.py::test_13_http_end_to_end_import_to_closed` (HTTP vom Upload
  bis zum geschlossenen Stapel), `test_14_decisions_persist_across_restart`, `test_15_preview_queue_and_diagnosis_are_
  read_only`; Sammelverarbeitung per HTTP: `tests/test_review_bulk.py` (Vorauswahl/Abwahl, Vorschau → Bestätigung →
  Ausführung über zwei Stapel inkl. Teilabschluss und Idempotenz, geänderter Stapel wird nicht ausgeführt).

## 16.3 Implementierte Erweiterungen

| Erweiterung | Umsetzung |
|---|---|
| Zentrale Prüfwarteschlange | `/journal/pruefung`, Reiter Zu prüfen · Sicher verarbeitbar · Abgeschlossen · Historie; Filter Quelle, Stapel, Konto, Art, Abgleich, Sicherheit, Zeitraum, Grund (`queue.py`) |
| Vereinfachte Sammelverarbeitung | Vorauswahl nur sicherer Verknüpfungen (Abwahl bleibt), eine gemeinsame Vorschau (Bestände, Gebühren, betroffene FIFO-Reihen inkl. rückwirkend betroffener Steuerjahre), eine Bestätigung, Ausführung je Stapel atomar (`bulk.py`) |
| Sichere manuelle Lösungsvarianten | je Prüfgrund konkrete Wege (Angabe ergänzen, einordnen, Gegenüberstellung, Übernahme prüfen) und Entscheidungen zurückstellen / nicht übernehmen / wieder aufnehmen – protokolliert, umkehrbar (`Item.variants`) |
| M29-Hardening | Positionen = Ledger ∪ Referenzen ∪ belastbare externe Bestände; bestätigt vs. fortgeschrieben; Teilbewertung (nie 0 €); Quellen- und Abgleichsevidenz getrennt; Mehrbein-Vorgang als eine Freigabe mit gemeinsamem Undo |
| Universeller Börsenimport | CSV, XLSX (ohne Makros, `defusedxml`), ZIP (Grenzen, Pfad-Traversal, keine Verschachtelung); Portfolia-Formate erkannt; jede Börse als Datei-Datenquelle; Herkunft je Stapel (Migration 22); gleiche Datei wird erkannt |
| Historische Coverage-Matrix | `/quality/abdeckung`: Konto/Quelle × Monat, Bereichsfilter, Lücken nur im Aktivitätszeitraum; Abrufe decken nur ihr Fenster, der kuratierte Import seine Monate bis zum Stichtag |
| Wirtschaftliche Ereignisgruppen | `/journal/ereignisse`: alle Quellen einer Buchung; Widersprüche 1:n, n:1, teilweise verknüpft, doppelt gebucht, Gebühr brutto/netto, Mengenwiderspruch (Rundung der Buchung toleriert) – nur angezeigt |
| Derivateunterstützung | Kennzeichen Futures-PnL, Funding, Liquidation, Margin(-Zins): Ledger-Ursprung „derivative“, kein externer Zahlungsstrom, nie still § 23; Import nur über verifizierte Spaltenzuordnung, nie automatisch |
| FIFO-Evidenz | `/quality/anschaffung`: Herkunftsklasse je Lot und je verbrauchtem Anteil (Kauf, Ertrag, Kapitalmaßnahme, Eröffnung, ohne Beleg, unbekannt, Derivat, Sparplan-Schätzung, ohne Basis); rückwirkende FIFO-Wirkung in der Vorschau |
| Vollständigkeitscockpit | `/quality/cockpit`: acht getrennte Dimensionen ohne Gesamtnote; Fallanalyse je Position mit Belegquellen; externe KI standardmäßig aus, nie für Finanzdaten |

## 16.4 Datenintegrität

* **Doppelbuchungen:** Abgleich per Hash/Ereignis-ID vor jeder Übernahme; erneut hochgeladene Dateien werden erkannt
  (gleiche Prüfsumme, unveränderte Zeilen behalten ihre Entscheidung); `_present` legt erledigte Ereignisse nicht
  erneut vor; die Gruppensicht meldet ein zweimal gebuchtes Ereignis und 1:n-Verknüpfungen; ein Vorgang wird nie
  geteilt.
* **Atomare Ereignisverarbeitung:** Mehrbein-Vorgänge werden in einer Transaktion gebucht und gemeinsam zurückgenommen;
  teilweise entschiedene Vorgänge werden nicht gebucht; Sammelausführung je Stapel atomar.
* **Evidenzklassifizierung:** Quellenevidenz (Originalbeleg vollständig · vollständig ohne Originalbeleg · teilweise ·
  ungeklärt) und Abgleichsevidenz (kein Gegenstück · ähnliche Buchung (Verdacht) · laut Importprüfung gebucht ·
  gebucht) getrennt; Ähnlichkeit begründet nie eine Buchung.
* **Vorschau und Freigabe:** jede bestandswirksame Aktion zeigt vorher Mengen-/Kontowirkung, Gebühren, betroffene
  FIFO-Reihen und rückwirkend betroffene Steuerjahre; Ausführung nur nach ausdrücklicher Bestätigung.
* **Idempotenz:** Einmal-Token je Vorschau, Prüfsumme des Datenstands (geänderte Vorschau wird nicht ausgeführt),
  Entscheidungen idempotent je Ereignis; Wiederholung bucht nichts doppelt.
* **Undo:** Stapel-Undo im Verlauf, gemeinsames Undo von Mehrbein-Vorgängen, Entscheidungen wieder aufnehmbar;
  Rohimporte bleiben unverändert.
* **Unbekannte historische Daten:** Zeilen vor dem Stichtag sind Historie (nicht offen, nicht verknüpfbar);
  Zugänge ohne Wert → Anschaffungskosten „unbekannt“ (nie 0 € als belegt; Steuerpaket warnt `deposit_basis`);
  Abgänge ohne Lots → „ohne Anschaffungsdaten“ (Warnung `missing_basis`); Sparplan-Schätzungen nie als belegt;
  Bewertung ohne Kurs „unvollständig“; Lücken in der Abdeckung sichtbar, aber nie als fehlende Buchung behauptet.

## 16.5 Tests

* **Neue Tests (dieser Auftrag):** Phase 1: 15 · Phase 2: 5 · Phase 3: 6 · Phase 4: 13 · Phase 5: 5 · Phase 6: 4 ·
  Phase 7: 3 · Phase 8: 5 · Phase 9: 4 · Phase 10: 9 (`test_fi_regression.py` 6, `test_mass_review.py` 2,
  `test_fi_export_restore.py` 1).
* **Regressionsfälle 13.2 (synthetisch):**

  | Fall | Test |
  |---|---|
  | Bitpanda: mehrere Dubletten/ungeklärte Vorgänge über Stapel | `test_review_workflow::test_10_bitpanda_like_counts_over_several_batches` |
  | Binance: vor Stichtag, bereits verknüpft | `test_review_workflow::test_11_binance_rows_before_cutoff_are_history_not_open` |
  | BNB: gültig abgeschlossene Bestandsabweichungen | `test_review_workflow::test_12_bnb_holding_decision_survives_fetch_aging` |
  | Bitpanda/Pionex USDT brutto/netto, Gebühr | `test_fi_regression::test_bitpanda_to_pionex_usdt_transfer_with_fee`, `test_event_groups::test_fee_basis_and_quantity_conflicts` |
  | Gate.io/Bitpanda SAND (eine Gutschrift, mehrere Quellen) | `test_fi_regression::test_gateio_to_bitpanda_sand_keeps_acquisition_and_fee`, `test_event_groups::test_group_view_shows_all_sources_of_one_booking` |
  | Bybit Liquidation mit Gebühr und Verlust | `test_derivatives` (3 Tests) |
  | Pionex-USDT-Rechnung → 0,0000388030 USDT | `test_fi_regression::test_pionex_usdt_remainder_is_exact` (exakt die Posten des Auftrags) |
  | MEXC überlappende Exporte mit Lücke | `test_coverage::test_gap_between_two_export_files_is_a_gap_quiet_month_is_covered` |
  | CoinEx KANGO/KASPY-Reste | `test_fi_regression::test_coinex_kango_kaspy_dust_remains_exact`, `…::test_coinex_trade_with_fee_in_third_asset` |
  | KREST unbekannte Anschaffungskosten | `test_fi_regression::test_krest_unknown_acquisition_cost_is_flagged_not_invented` |
  | PulseChain unplausible Mainnet-Zuordnung | `test_wallets_pulse_peaq::test_pulsechain_starts_at_first_block_and_offers_fork_opening` u. a. |
  | 500 TOKEN ohne Ledger | `test_m29_hardening::test_reference_without_any_booking_appears_unvalued_not_zero` |
  | Mehrteiliger Swap, gemeinsames Undo | `test_m29_hardening::test_multi_leg_event_is_booked_and_undone_as_one` |

* **Bestanden:** vollständige Suite 983/983 (lokal, 8:41 min) und CI; keine Tests übersprungen oder ausgelassen,
  keine Sicherheitsprüfung entfernt.
* **Ruff:** `ruff check app tests` ohne Befund.
* **Massendaten (100 000 synthetische Quellzeilen, 20 Stapel, temporäre DB):** zentrale Prüfzählung 2,2–2,6 s,
  gefilterte Warteschlange 4,5–4,6 s, Abdeckungsmatrix 0,6 s; Einlesen und Auswerten einer 100 000-Zeilen-Datei 5,5 s.
* **CI:** Lint, Tests, Image-Build und Smoke-Test laufen in GitHub Actions (`.github/workflows/ci.yml`); der Smoke-Test
  ruft zusätzlich Prüfwarteschlange, Ereignisgruppen, Abdeckung, Anschaffungsdaten und Cockpit ab.
* **Docker:** lokal nicht ausführbar (kein Docker-Daemon in der Entwicklungsumgebung) – Build und Container-Smoke-Test
  in CI erfolgreich (Lauf 38687922607: Image-Größengrenze, native Bibliotheken, Belegimport, neue Seiten).
* **Smoke-Test lokal:** App im Demo-Modus mit Beispielimport (temporäre Verzeichnisse): alle neuen Seiten HTTP 200,
  keine Fehler im Log.
* **Export und Restore:** bestehende Roundtrip-/Fehlerinjektionstests (`test_restore_roundtrip`, `test_full_export`,
  `test_restore_resilience`) grün; neu `test_fi_export_restore`: Derivat-Buchungen überstehen Export und
  Neueinrichtung mit Kennzeichen, Menge, Wert und Einordnung; belegte Lot-Herkunft identisch. Dabei gefunden und
  behoben: Lots geschätzter Sparplan-Ausführungen galten als belegter Kauf.
* **Reale Validierung (13.3, nur lesend):** Kopie des Exports vom 10.10.2026 in temporärer Instanz, ohne Scheduler,
  ausgehender Netzverkehr blockiert, Kursnachladen ausgelassen; SHA-256 der Quelldatei und Prüfsumme der Buchungs-,
  Stapel-, Verknüpfungs-, Entscheidungs- und Referenztabellen vor/nach allen Ansichten unverändert. Ergebnisse:
  * Restore erfolgreich; alle neuen Seiten HTTP 200; Cockpit 2,8 s, Abdeckung/Anschaffung < 0,1 s.
  * Gefunden und behoben: (1) Ruhige Monate im kuratierten Import erschienen als 117 Lücken – der Import deckt seine
    Monate bis zum Stichtag ab (danach 0 Lücken); (2) 33 von 48 „Mengenwidersprüchen“ waren nur die Rundung der
    Buchung auf weniger Nachkommastellen, 6 entsprachen genau der Gebühr der Quelle bei Buchung ohne Gebühr (jetzt
    Hinweis brutto/netto) – übrig 9 echte Mengenabweichungen, 6 Brutto/Netto-Hinweise, 3 n:1; (3) die Fallanalyse
    bot nur Positionen mit Referenzbestand an – jetzt alle Positionen.
  * Der Export enthält **keine Referenzbestände**: Abweichungsfenster und Restdifferenzen der historischen
    Prüfhinweise (Bitpanda USD/EUR/USDT, Scalable EUR, CoinEx KANGO/KASPY, Binance, Coinlist PEAQ, PulseChain PLS)
    sind daher nicht bezifferbar; die Positionen sind vorhanden und mit Herkunftsklassen versehen. KREST ist im Export
    nicht enthalten. Korrekturszenarien wurden nicht simuliert, nichts gebucht.
  * Anschaffungsdaten: 20 von 67 Assets mit Zugängen ohne Anschaffungsbeleg, Veräußerungen ohne Anschaffungsdaten in
    einzelnen Jahren – sichtbar als „Handlungsbedarf“, nicht ergänzt.

## 16.6 Phasen und Commits

| Phase | Status | Commit | Tests | Ergebnis |
|---|---|---|---|---|
| 0 Plan | abgeschlossen | `585f96c` | – | Bestandsaufnahme, bestätigte Ursachen, Gap-Analyse |
| 1 Prüfworkflow | Tests bestanden | `b88e1f5` | `test_review_workflow` (15) | Stapelabschluss, zentrale Zustände, Nullgründe, BNB, keine Wiedervorlage |
| 2 Prüfung/Sammelverarbeitung | Tests bestanden | `204b984` | `test_review_bulk` (5) | Warteschlange, Vorauswahl, gemeinsame Vorschau, Lösungsvarianten |
| 3 M29-Hardening | Tests bestanden | `b0b3bbe` | `test_m29_hardening` (6) | Ledger-lose Positionen, Fortschreibung, Teilbewertung, getrennte Evidenz, Mehrbein |
| 4 Universalimport | Tests bestanden | `c28b702` | `test_universal_import` (13) | CSV/XLSX/ZIP, Portfolia-Formate, Herkunft, sichere Archive |
| 5 Abdeckung | Tests bestanden | `d94c6dd` + Phase-10-Korrektur | `test_coverage` (5) | Matrix, MEXC-Lücke, Abruffenster, kuratierter Import bis Stichtag |
| 6 Ereignisgruppen | Tests bestanden | `30c0f5c` + Phase-10-Korrektur | `test_event_groups` (4) | Gruppensicht, sechs Widerspruchsarten, Rundungs-/Gebührentoleranz |
| 7 Derivate | Tests bestanden (nur geprüfte Formate) | `b6d0491` | `test_derivatives` (3) | Funding/Liquidation/PnL ohne Kapitalfluss, nicht § 23 |
| 8 FIFO-Herkunft | Tests bestanden | `5f82d82`, `6e825bd` | `test_lot_provenance` (5) | Herkunftsklassen, rückwirkende Steuerjahre, Schätzungen nie belegt |
| 9 Cockpit | Tests bestanden | `d40296d` + Phase-10-Korrektur | `test_cockpit` (4) | acht Dimensionen, Fallanalyse, KI aus |
| 10 Gesamtregression/Release | Tests bestanden, CI grün | `333c3f2`, `febc1de` | `test_fi_regression` (6), `test_mass_review` (2), `test_fi_export_restore` (1) | Regressionen 13.2, 100 000 Zeilen, Export/Restore, reale Validierung |

## 16.7 Abschluss

* **Neue Version:** 0.27.0.
* **Release-Commit:** `febc1de` (davor `333c3f2` Korrekturen aus der realen Validierung); dieser Nachtrag ist ein
  reiner Doku-Commit.
* **Push:** Branch `claude/portfolia-dashboard-s9p6zr`, alle Phasen-Commits `585f96c` … `febc1de` gepusht.
* **CI:** GitHub-Actions-Lauf 38687922607 auf `febc1de` erfolgreich – Job „test“ (Ruff, Testsuite) und Job „image“
  (Docker-Build amd64/arm64, Image-Größengrenze, native Bibliotheken, Container-Smoke-Test inkl. der neuen Seiten).
* **Lokale Suite vor dem Push:** 983 Tests bestanden, 0 fehlgeschlagen, 0 übersprungen; Ruff ohne Befund.
* **Verbleibende Einschränkungen:**
  * Native Futures-/Margin-Exporte von MEXC, Bybit und Pionex sind nicht verifiziert und werden nicht automatisch
    erkannt (Import über eigene Spaltenzuordnung; EUR-Wert Pflicht; nie automatisch übernommen).
  * Derivat-Ergebnisse werden nicht steuerlich eingeordnet (bewusst: nie still § 23); die Einordnung bleibt manuell.
  * Abdeckung: der kuratierte Import gilt bis zu seinem Stichtag als vollständig – fehlt dort ein Konto teilweise,
    zeigt erst ein Referenzbestand bzw. die Bestandsklärung die Lücke.
  * Mengenwidersprüche unterhalb der Stellenzahl der Buchung (≥ 2 Nachkommastellen) werden als Rundung gewertet.
  * Docker-Build und Container-Smoke nur in CI (lokal kein Daemon).
  * Reale Validierung ohne Referenzbestände im Export: historische Restdifferenzen nicht bezifferbar.
* **Nächste Entwicklungsschritte:**
  1. Referenzbestände (Kontoauszüge zum Stichtag) für die Positionen aus 13.3 hinterlegen – erst damit werden
     Abweichungsfenster und Restdifferenzen belastbar.
  2. Native Futures-Exporte (MEXC/Bybit/Pionex) anhand echter, anonymisierter Beispieldateien als Profile
     verifizieren.
  3. Steuerliche Einordnung von Derivat-Ergebnissen (§ 20 / § 22 / § 23 je Produkt) als ausdrückliche Regel im
     Steuerpaket – mit Vorschau und Begründung.
  4. Die 9 echten Mengenabweichungen und 6 Brutto/Netto-Hinweise der realen Daten über die Ereignisgruppen klären.
