# Performance-Optimierung: Diagnose je Anfrage und Performance-Kennzahlen

Ausgangsstand `f609562` (0.107.0). Zwei eng begrenzte Themen; keine fachliche Änderung, keine Migration, keine neue
Abhängigkeit, kein prozessweiter Zwischenspeicher.

## 1. Untersuchte Codepfade

| Seite | Pfad |
|---|---|
| `GET /quality/plan` | `dq_web.plan_partial` → `WP.build(ctx)` (eigene Diagnose) und `G.overview` → `G.tasks` (zweite Diagnose) |
| `GET /quality/assistent` | `assistant_web._page` → `G.step` → `G.tasks` + `G.detail` (je Aufgabenart: `_case_detail`, `_case_exits` → `ST.rows_for_case` → `ST.plan`), `G.similar`, `G.rest_overview` |
| `GET /performance` | `pages.performance` → `P.bounds` je Zeitraum → `P.metrics` (TTWROR, IRR über `irr_period_detail` → `xirr_detail`, Drawdown, Flüsse) für 8 Standardzeiträume und erneut für die Auswahl |

`report_for(ctx) = diagnose(collect(ctx))` hat keinen Zwischenspeicher. Jeder Aufruf ist also eine vollständige
Diagnose.

## 2. Nachgewiesene Redundanzen

Gezählt wurde mit gekapselten Modulfunktionen (`engine.diagnose`, `periods.metrics`, `periods.irr_period_detail`).
Grundlage sind synthetische Benchmark-Daten (Abschnitt 4).

| Vorgang | Diagnose- bzw. Kennzahlaufrufe vorher | Nachher |
|---|---|---|
| Qualitätsplan (`/quality/plan`) | 2 Diagnosen | 1 |
| Assistent – Diagnosefrage | 2 Diagnosen | 1 |
| Assistent – keine offene Aufgabe | 3 Diagnosen | 1 |
| Assistent – „für alle gleich“ (`G.similar`) | 2 Diagnosen | 1 |
| Performance – Standardzeitraum (`/performance`, `?period=1J`, `?period=MAX`) | 9 × `metrics` / 9 × IRR | 8 / 8 |
| Performance – individueller Zeitraum (`?start=2023-01-01&end=2024-06-30`) | 9 / 9 | 9 / 9 (andere Grenzen, keine Redundanz) |
| Performance – eigener Zeitraum mit den Grenzen eines Standardzeitraums | 9 / 9 | 8 / 8 |

Die Zahl der Aufrufe ist nicht gleichbedeutend mit der Ladezeit. Die gemessenen Zeiten stehen in Abschnitt 5.

## 3. Implementierte Änderungen

- **`app/diagnosis/guide.py`**
  - `page_scope(ctx)`: Bereich eines Seitenaufrufs, gebunden an Thread und Block. Innerhalb des Bereichs nutzen
    Aufgabenliste, Frage, Klärungsfall, „für alle gleich“ und Restübersicht **einen** Bericht (`scoped_report`).
    Der Bericht entsteht beim ersten Bedarf.
  - Vor jeder Wiederverwendung wird `_report_version` geprüft: `A.data_version` plus Zahl, höchste ID und aktive
    Einträge von `diag_decision`. Hat sich ein Wert geändert, entsteht ein neuer Bericht.
  - Außerhalb des Bereichs, bei verschachtelten Bereichen anderer Threads und über Anfragen hinweg gibt es keine
    Wiederverwendung.
  - `tasks`, `rest_overview`, `detail`, `_case_detail`, `_case_exits` und `overview` nehmen optional `report` an.
    `step` und `similar` laufen im Bereich.
- **`app/cutover/settle.py`**: `plan(…, report=None)` und `rows_for_case(…, report=None)` reichen einen vorhandenen
  Bericht durch. Fehlt er, rechnen sie wie bisher selbst.
- **`app/diagnosis/dq_web.py`**: Arbeitsplan und Aufgabenzählung laufen in einem `page_scope`.
- **`app/diagnosis/assistant_web.py`**: Die Seite rendert im `page_scope`. Das Schreiben (`G.save_pass`) liegt
  **vor** dem Bereich und nie in einem Lese-Snapshot.
- **`app/web/routes/pages.py`**: `metrics_for` hält ein Wörterbuch nur für diesen Aufruf, mit den tatsächlichen
  Grenzen `(start_index, end_index, from_inception)` als Schlüssel. Gleiche Grenzen werden einmal berechnet. Jede
  Verwendung erhält eine Kopie, damit die Ergänzung `sel["quality"]` nicht in die Tabelle wirkt.
- **`scripts/bench.py`**: Neuer Modus `--dq-perf`. Außerdem ein Schutz für das Arbeitsverzeichnis: Erlaubt ist nur ein
  neues, leeres Verzeichnis oder eines mit der Markierung `.portfolia-bench`. Ohne Angabe legt das Skript ein frisches
  temporäres Verzeichnis an.

## 4. Benchmark-Methode

- `TZ=Europe/Berlin PYTHONHASHSEED=0 python scripts/bench.py <neues mktemp-Verzeichnis> --dq-perf out.json --runs N`
  - Daten: synthetischer Import mit 5.725 Transaktionen und 170 Assets, dazu Demo-Kurse und Historie.
  - Keine Echtdaten, Netzwerk aus.
- Ablauf je Endpunkt:
  - **kalt**: nach `ctx.invalidate_data()`, also mit leeren Portfolio-, Ledger-, Bewertungs- und
    Historien-Zwischenspeichern;
  - danach ein Aufwärmaufruf;
  - danach N **warme** Aufrufe mit Median und p95 (`time.perf_counter`).
- Umgebung: Linux x86_64, 4 Kerne, Python 3.11.17. Das ist eine Abweichung von 3.12 im Container-Image.
- Vorher (`f609562` mit identischem `bench.py`) und Nachher liefen **abwechselnd**: drei Serien mit N = 7 und eine
  mit N = 25. Angegeben ist der Median der vier Serienwerte.
- Für `/performance` gab es zusätzlich eine Kontrollmessung mit N = 40 auf denselben Daten. Sie lief einmal ohne und
  einmal mit `gc.collect()` vor jedem Aufruf.
- IRR-Kosten (D2) wurden mit synthetischen Zahlungsreihen über 5 Jahre gemessen, 25 Wiederholungen je Muster.

## 5. Vorher-Nachher-Tabelle

Verbesserung = (Vorher − Nachher) / Vorher × 100 %. Ein negativer Wert bedeutet langsamer.

| Endpunkt | Vorher kalt | Nachher kalt | Vorher warm (Median) | Nachher warm (Median) | Vorher p95 | Nachher p95 | Verbesserung |
|---|---|---|---|---|---|---|---|
| `/quality/plan` | 1.499 ms | 1.314 ms | 502,1 ms | 260,3 ms | 592,9 ms | 348,2 ms | warm **48 %**, p95 41 %, kalt 12 % |
| `/quality/assistent?new=1` (Diagnosefrage) | 1.330 ms | 1.307 ms | 503,4 ms | 280,9 ms | 627,7 ms | 379,5 ms | warm **44 %**, p95 40 %, kalt 2 % |
| `/performance` | 2.049 ms | 2.048 ms | 25,0 ms | 26,5 ms | 26,8 ms | 105,8 ms¹ | warm −6 % (Rauschen) |
| `/performance?period=1J` | 2.211 ms | 2.329 ms | 22,2 ms | 21,2 ms | 29,9 ms | 27,0 ms | warm 4 % (Rauschen) |
| `/performance?period=MAX` | 2.191 ms | 2.161 ms | 26,5 ms | 25,3 ms | 31,2 ms | 28,2 ms | warm 4 % (Rauschen) |
| `/performance?start=2023-01-01&end=2024-06-30` | 2.098 ms | 2.309 ms | 20,9 ms | 25,2 ms | 24,0 ms | 27,9 ms | keine Änderung im Code-Pfad, Rauschen |

¹ Einzelne Ausreißer in drei der vier kurzen Serien (N = 7, dort ist p95 praktisch das Maximum).

Kontrollmessung mit N = 40 auf denselben Daten, Median / p95:

| Endpunkt | Vorher | Nachher |
|---|---|---|
| `/performance` | 22,7 / 27,7 ms | 18,3 / 24,7 ms |
| `/performance?period=1J` | 20,4 / 26,6 ms | 19,2 / 24,5 ms |

Keine Ausreißer in beiden Ständen, keine Verschlechterung.

Kaltwerte werden vom Neuaufbau der Historie und des Ledgers bestimmt (≈ 1–2 s) und streuen zwischen den Serien um
±300 ms. Für sie gibt es keine belastbare Aussage. Bei den Diagnoseseiten entspricht die warme Ersparnis von
≈ 220–240 ms einer eingesparten Diagnose. Bei `/performance` spart der entfallene `metrics`-Aufruf rechnerisch
≈ 1–2 ms. Das liegt unter der Messstreuung.

IRR-Kosten (`irr_period_detail`, Median je Aufruf):

| Muster | Median |
|---|---|
| wenige Einzahlungen | 0,42 ms |
| Sparplan (62 Zahlungen) | 0,49 ms |
| Ein- und Auszahlungen (61 Vorzeichenwechsel) | 1,33 ms |
| mehrere Vorzeichenwechsel | 1,45 ms |
| keine Lösung | 0,30 ms |

## 6. Regressionstests

`tests/test_perf_reuse.py` mit 26 Tests, alle mit strikter Gleichheit über `repr`. Ausgenommen ist allein das je
Anzeige zufällige Einmal-Token des Prüfstapels.

- **Diagnose**
  - Jede Aufgabenart (Frage, Prüfstapel, Einzelzeile auch unvollständig, Klärungsfall-Seite) erzeugt genau
    1 Diagnose je Assistentenseite.
  - Die Karte ist identisch mit der Berechnung ohne gemeinsamen Bericht.
  - Ohne offene Aufgabe gibt es 1 Diagnose, und die Restübersicht ist identisch.
  - `/quality/plan` erzeugt 1 Diagnose; Plan, Zählung und Minuten sind identisch.
  - `similar` und Klärungsfall-Detail sind identisch.
  - Datenstand oder Entscheidung ändert sich im Bereich → neuer Bericht; die entschiedene Aufgabe ist weg.
  - Verschachtelte Bereiche teilen einen Bericht.
  - Außerhalb eines Bereichs und in einem anderen Thread gibt es keine Wiederverwendung.
  - Eine zurückgestellte Antwort ist auf der Folgeseite sichtbar.
- **Performance**
  - Alle Zeiträume 1M bis MAX, Schreibweisen (`1j`, `max`, unbekannt), Scopes (Gesamt, Segment, Position) und eigene
    Zeiträume (Start und Ende, nur Start, nur Ende, vor Depotbeginn, ungültiges Datum).
  - Jede Zeile und die Auswahl stimmen mit einer unabhängigen `P.metrics`-Berechnung überein: TTWROR, IRR,
    Mehrdeutigkeit, Gewinn, Drawdown, Flüsse und Bewertungsstatus.
  - Jeder tatsächliche Zeitraum wird genau einmal berechnet.
  - Ein eigener Zeitraum mit Standardgrenzen wird nicht doppelt berechnet.
  - Es gibt keine Wiederverwendung über Anfragen hinweg. Nach einer Datenänderung (gelöschter Kauf) stimmen die neuen
    Werte mit der unabhängigen Berechnung überein.
  - Fehlende Kurse (Kurslücke über 8 Monate) liefern identische Werte.

Gesamtlauf `TZ=Europe/Berlin pytest -q tests`: ERGEBNIS_VOLLTEST. `ruff check app tests scripts` meldet keinen Befund.
mypy (CI-Umfang plus geänderte Module) findet vorher und nachher 359 Befunde, darunter keinen neuen. Dieser Wert ist
mit einer anderen mypy-Version gemessen als die historischen 344.

## 7. Bewusst nicht umgesetzte Maßnahmen

- **Prozessweiter Cache für Diagnoseberichte:** nicht umgesetzt. Die lokale Übergabe je Seitenaufruf reicht. Eine
  korrekte globale Invalidierung über alle Eingaben (Kurse, Kurslücken, Datum) wäre nicht nachgewiesen.
- **Kennzahl- oder IRR-Cache über Anfragen (D3):** nicht umgesetzt. Ein IRR kostet ≤ 1,5 ms, die warme Seite ≈ 20–25 ms.
  Der Benchmark belegt keinen Nutzen.
- **Prüfung von Kursänderungen im Hintergrund während einer Seite:** absichtlich nicht im Versionsschlüssel. Eine
  Seite zeigt einen Lesestand. Vorher konnten zwei Diagnosen derselben Seite unterschiedliche Kursstände sehen.
- **Kaltstart (Ledger, Historie):** nicht angefasst, außerhalb des Auftrags.
- **Beobachtung außerhalb des Auftrags, nicht behoben:** `/performance?start=…&end=…` mit Start nach Ende endet mit
  HTTP 500, weil das Template `start_value` erwartet. Das tritt schon in `f609562` auf.

## 8. Abschließende Bewertung

- **Diagnose je Anfrage:** Die Redundanz ist entfernt (2 → 1 bzw. 3 → 1). Die Diagnoseseiten sind warm messbar
  schneller: Median −44 bis −48 %, p95 −40 %.
- **Performance-Kennzahlen:** Die doppelte Zeitraumberechnung ist entfernt (9 → 8). Der Laufzeitgewinn liegt im
  Rauschen. Die Änderung ist ein lokales Wörterbuch mit acht Zeilen, ohne Zustand über die Anfrage hinaus.
- Die fachlichen Ergebnisse bleiben in allen geprüften Fällen bitgenau gleich.
