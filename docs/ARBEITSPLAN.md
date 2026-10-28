# Arbeitsplan: Behebung der Befunde aus der Projektanalyse + besseres Logging

Grundlage: [`PROJEKTANALYSE.md`](PROJEKTANALYSE.md) (Befund-IDs H1, M1–M8, N1, L1–L8). Dieser Plan beschreibt **wie** jeder Befund
gelöst wird, womit er geprüft wird und in welcher Reihenfolge.

> **Stand 28.10.2026:** umgesetzt in 0.60.0–0.65.0 (Entscheidungen E1–E5 des Auftraggebers: Format `text`, Zugriffsprotokoll `slow`, Log-Download
> ja, kein Hinweisband für M8 – nur bei blockierten Benutzeraktionen –, alle Pakete). Abweichungen vom Plan stehen in den Einträgen von
> `MILESTONES.md`; Reihenfolge der Veröffentlichung: A, B, C, D, E, F wie in Abschnitt 1.

## 0. Grundsätze

* **Ein Paket = ein Release** (Versionssprung, Eintrag in `docs/MILESTONES.md`), innerhalb des Pakets **ein Commit je Befund**, damit sich
  jede Änderung einzeln zurücknehmen lässt.
* **Test zuerst:** Für jeden Befund entsteht zuerst ein fehlschlagender Test (Reproduktion aus der Analyse), dann die Korrektur.
  Befunde, die nur ein Test absichert (z. B. M5), bekommen einen Wächtertest, der eine Wiederkehr verhindert.
* **Abnahme je Paket:** `ruff check app tests scripts` · gezielte Tests · **Volltest grün** · erst dann Push · CI prüfen.
* **Keine Verhaltensänderung nebenbei.** Wo ein Befund eine Entscheidung verlangt (Standardwerte, Oberfläche), steht sie in Abschnitt 5.
* **Messen statt vermuten:** M4 und M6 sind Risiken, keine gemessenen Fehler. Das Logging-Paket kommt deshalb vor ihnen und liefert
  die Messwerte, auf deren Basis über die Maßnahme entschieden wird.

## 1. Reihenfolge

| Paket | Release | Inhalt | Warum hier | Aufwand |
|---|---|---|---|---|
| **A** | 0.60.0 | H1 Pillow 13 · M1 · M2 · N1 | hoch bzw. klein, risikoarm, unabhängig | S |
| **B** | 0.61.0 | **Logging/Diagnose** (Abschnitt 3) · M8-Warnung | macht alle folgenden Pakete beobachtbar | M |
| **C** | 0.62.0 | M3 stille Fehler | Datenintegrität; profitiert von B (Fehler werden sichtbar) | M |
| **D** | 0.63.0 | M4 Pools · M5 `async` · M6 Verbindungen | Entscheidung anhand der Messwerte aus B | M |
| **E** | 0.64.0 | M7 Bildproxy · L1 · L2 · L4 | Härtung | S–M |
| **F** | 0.65.0 | L3 · L5–L8 · CI-Prüfungen · Testlücken | Aufräumen und Absicherung | M |

Zwischen B und D sollten **mindestens 24 h Betrieb** liegen (reale Messwerte zu Threads, Verbindungen, verspäteten Läufen).

## 2. Lösung je Befund

### Paket A – 0.60.0

**H1 Pillow 12.3.0 → 13.0.0** (`requirements.txt:19`; 13.0.0 ist auf PyPI verfügbar)
1. Pin anheben, Changelog auf entfernte APIs prüfen (`grep -rn "from PIL\|import PIL" app`).
2. `Dockerfile:28` entfernt `PIL/_avif*.so` und `pillow.libs/libavif*` – bei geändertem Wheel-Layout prüfen, dass der Build nicht bricht
   (der Docker-Smoke-Test der CI deckt das ab).
3. Tests: Beleg-/OCR-Tests (`tests/test_documentimport*`), Bildproxy (`test_news_intel`), PDF/Logo (`reportlab` bleibt unberührt).
4. `pip-audit -r requirements.txt` → keine Meldung (außer Build-Werkzeuge `pip`/`setuptools`, die nicht im Image-Laufzeitpfad liegen).
* *Abnahme:* Audit leer, Volltest grün, Docker-Image baut. *Rückfall:* Pin zurück auf 12.3.0 und stattdessen Formatliste
  (`formats=[...]`) auch in `documentimport` setzen (L-Variante aus H1 Punkt 3).

**M1 `/news?tab=foo` → 500**
* `tab`, `sort`, `status` in den News-Routen als `Literal[...]` deklarieren (FastAPI antwortet 422). Zusätzlich im Template den
  `else`-Zweig „Quellen“ durch ein explizites `elif tab == 'sources'` ersetzen, damit ein neuer Reiter nie still im falschen Zweig landet.
* Alle übrigen Routen mit String-Auswahlparametern per Skript aus der Routenliste prüfen (die Analyse hat `tab`/`sort`/`status` gefunden;
  Skript `ana/fuzz*.py` wird als Test übernommen).
* *Test:* parametrisiert über alle GET-Routen mit je einem ungültigen Wert je Query-Parameter → Antwort ist 2xx/3xx/4xx, nie 5xx.

**M2 ID ≥ 2⁶³ → 500 (25 Routen)**
* Ein **zentraler** Ausnahmehandler `OverflowError → 404` (nur wenn die Meldung `SQLite INTEGER` enthält), registriert in
  `web/app.py` neben dem 404-Handler. Vorteil gegenüber 25 einzelnen Typannotationen: deckt auch künftige Routen und Query-Parameter ab.
* *Test:* alle Routen mit `int`-Pfadparameter (aus `app.routes` abgeleitet) mit `10**20` aufrufen → 404, nie 500.
* *Grenze:* maskiert einen `OverflowError` aus anderer Quelle nicht (Bedingung auf die SQLite-Meldung).

**N1 „Nur Teaser“ bei Google News**
* `hosts.teaser_hosts`: Host aus `COALESCE(final_url, url)` bilden (`news_content.final_url` existiert, `db.py` Migration 33); Meldungen, deren Host
  `news.google.com` bleibt (nicht aufgelöst), zählen nicht.
* Ergebnis 60 s im Prozess zwischenspeichern (Statusseite fragt alle 3 s, `_raw_candidates` bei jedem Lauf).
* *Test:* 10 gesperrte Verlags-Abrufe über Google-News-URLs verschiedener Verlage → kein Host als Teaser markiert; ein einzelner Verlag mit
  10 Sperren → markiert.

### Paket C – 0.62.0: M3 verschluckte Fehler

Vorgehen in vier Schritten:
1. **Inventar:** AST-Skript der Analyse als `scripts/silent_excepts.py` übernehmen; Ausgabe in drei Klassen einteilen.
   * **A – „Tabelle fehlt“** (migrationsgesichert, kann nicht eintreten): `try/except` entfernen.
   * **B – echtes Best-Effort** (Nebenwirkung darf den Hauptvorgang nie stoppen, z. B. Archivkopie): beibehalten, aber `log.warning` mit
     `exc_info` (gedrosselt, siehe Abschnitt 3).
   * **C – Schutzprüfung**: bei Fehler **abbrechen**. Hierzu gehören `diagnosis.actions.assert_version`, `assetchange.merge.canonical`
     (Fehler soll sichtbar sein, nicht `{}`) und `diagnosis.actions.decisions`.
2. **Zuerst Klasse C** (höchstes Risiko), je Funktion ein Test, der die DB mit `sqlite3.OperationalError` fehlschlagen lässt und prüft, dass
   die Korrektur **nicht** übernommen wird.
3. Danach A und B.
4. **Wächtertest** `tests/test_no_silent_except.py`: zählt stille Ausnahmebehandler per AST; die Obergrenze steht als Zahl im Test
   (53 → Zielwert nach der Bereinigung) und darf nur sinken.
* *Risiko:* Entfernte Abfangungen machen bisher verdeckte Fehler plötzlich sichtbar (500 statt leerer Liste). Das ist gewollt; deshalb
  Paket B davor (Fehler-ID und Kontext im Log) und Volltest + Crawler (`ana/crawl.py` als Test) nach der Änderung.

### Paket D – 0.63.0: Betrieb

**M4 gemeinsamer Pool.** Messung aus Paket B auswerten (verspätete/übersprungene Läufe, Wartezeit je Job). Maßnahme:
* Neuer Executor `news` (2 Threads) für alle `news_*`-Jobs über den vorhandenen Parameter `register(..., executor=…)`; Kern-Jobs bleiben auf
  `default` (3). Manuelle Welle bleibt auf `wave`.
* Verpasste/verworfene Läufe (APScheduler-Ereignisse `MISSED`, `MAX_INSTANCES`) als WARNING mit Jobname und Verspätung loggen und je Job in
  `job_status` zählen (Anzeige auf der Statusseite).
* *Test:* Scheduler mit Fake-Jobs: ein blockierter News-Job verzögert einen Kern-Job nicht.
* *Abnahme:* über 24 h im Echtbetrieb 0 verworfene Läufe der Kern-Jobs (Logzeile `missed`).

**M5 `async`-Handler mit blockierender DB.** 8 Handler (Liste in der Analyse).
* Handler ohne `await` im Rumpf → `def` (läuft im Thread-Pool). Handler mit `await request.form()` bleiben `async`, der Datenbankteil geht in
  eine Hilfsfunktion, aufgerufen mit `await run_in_threadpool(...)`.
* *Wächtertest:* AST-Prüfung „kein `db.x`/`db.q`/`db.transaction` direkt in einer `async def` unter `app/`“ (die Analyse-Logik).
* *Test:* gleichzeitiger langer Schreibvorgang in einem Thread, `GET /healthz` bleibt innerhalb von 1 s erreichbar.

**M6 SQLite-Verbindungen.** Zuerst Messwerte aus B (Zeile „Betriebszustand“: Threads, offene Verbindungen, RSS).
* Ist die Summe unkritisch (< ~100 MB Zusatz): nur `cache_size` von −8000 auf −2000 senken.
* Sonst zusätzlich Threadpool begrenzen (`anyio`-Limiter auf 16 beim Start). Verbindungen je Anfrage zu schließen verwerfe ich als erste
  Wahl: das kostet pro Anfrage Verbindungsaufbau und PRAGMA-Setup und löst das Problem nicht besser als die Begrenzung.
* *Test:* Verbindungsanzahl nach 100 parallelen Anfragen ≤ Limit.

### Paket E – 0.64.0: Härtung

**M7 Bildproxy:** `news/images.cached_image` über `SafeFetcher` (gepinnte, geprüfte Adresse, Weiterleitungen einzeln, Größenlimit).
* `SafeFetcher.get` prüft `allowed_types` exakt; für Bilder eine Liste `image/jpeg|png|webp|gif|avif` übergeben.
* **Sonderfall Telegram-Bridge:** der Bridge-Host liegt absichtlich im LAN („Quell-Host im LAN ausdrücklich erlaubt“). `SafeFetcher`
  sperrt private Adressen. Lösung: Parameter `allow_hosts` (nur der konfigurierte Bridge-Host, exakt) – ohne ihn bricht der Telegram-Bildabruf.
* *Tests:* Rebinding-Simulation (Resolver liefert beim zweiten Aufruf `127.0.0.1`) → blockiert; Telegram-Bridge im LAN → erlaubt; Bild > 2 MB → abgewiesen.

**L1 `preexec_fn`:** Grenzen (`RLIMIT_AS`, `RLIMIT_CPU`) im Startskript des Kindprozesses (`_BOOT`) setzen statt via `preexec_fn`;
Test: Kindprozess mit Speichergrenze bricht bei Überschreitung ab (vorhandener Test bleibt grün).
**L2 `|safe`:** in beiden Templates `|tojson`; Test mit `</script>` im Beschriftungstext.
**L4 `suppress(ModuleNotFoundError)`:** nur `exc.name == <Modulname>` tolerieren, alles andere als ERROR loggen und im Startbanner nennen.

### Paket F – 0.65.0: Wartung und Absicherung

* **L3 toter Code:** je Funktion `git grep` + Test-Nutzung prüfen; löschen, wo nur Tests sie nutzen *und* kein Zweck erkennbar ist
  (z. B. `documentimport.evidence.safe_for_booking` bleibt, wenn der Test die Semantik dokumentiert – dann Kommentar statt Löschen).
  Endpunkt `GET /api/asset/{id}/position` entfernen oder dokumentieren.
* **L5 Doku:** Kopfzeile von `docs/MILESTONES.md` aktualisieren; README-Versionshinweise nicht umschreiben, nur Kopf/Inhaltsverzeichnis prüfen.
* **L6** `date.today()` → `now_local().date()`; `ruff`-Regel `DTZ011` aktivieren (verhindert Wiederkehr).
* **L7 CI:** `pip-audit` (Fehler bei Meldung, Allowlist nur für `pip`/`setuptools`), `mypy` schrittweise (nur `app/news app/jobs app/web`,
  zunächst nicht blockierend, Baseline-Zahl im Log), Abdeckungsgrenze `--cov-fail-under=84` als **nächtlicher** Lauf (der Volltest dauert
  ~20 Min.; die Grenze in jedem Push-Lauf würde ihn verlängern).
* **L8:** `assert` in Produktivcode durch explizite `if … raise` ersetzen (42 Stellen, mechanisch, mit `ruff S101`); lokale Importe nicht
  pauschal anfassen – nur dort herausziehen, wo kein Zyklus entsteht (Test: `python -X importtime` / Importprüfung der Analyse).
* **Testlücken** (aus der Abdeckung): CSV-Profile mit anonymisierten Beispieldateien je Broker (`csvimport/profiles.py`, 381 Zeilen
  ungetestet), Beleg-Extraktion (`documentimport/extract.py`), Netzwerk-Fehlerpfade (Mock-Transport) für `prices/yahoo.py`, `news/youtube.py`,
  `news/llm.py`; Test für `app/cli.py`.

## 3. Paket B – Logging für die Docker-Konsole (0.61.0)

### Ist-Zustand (geprüft, nicht vermutet)
Ich habe die App lokal wie im Container gestartet (`python -m app`, `LOG_FORMAT=text`) und einen Fehler ausgelöst (`/news?tab=foo`):

| Beobachtung | Folge |
|---|---|
| `access_log=False`: **keine Zeile pro Anfrage** | Man sieht nicht, was vor einem Fehler passiert ist, und nichts über Langsamkeit |
| 500 erzeugt `ERROR uvicorn.error: Exception in ASGI application` + **~110 Zeilen Framework-Traceback**; **Methode und Pfad stehen nirgends** (0 Treffer für `tab=foo`) | Man sieht *dass*, aber nicht *wo* (`UndefinedError: 'discovery' is undefined` steht ganz unten) |
| `run_job`: Start/Ende/Dauer/Ergebnis werden **nicht** geloggt; bei Fehler steht nur `Job x fehlgeschlagen: <Meldung>`, der **Traceback nur auf DEBUG** | Bei `INFO` (Standard) fehlt der Ort des Fehlers |
| `TextFormatter` gibt **Zusatzfelder (`extra=`) nicht aus** (`job=…` geht verloren); nur das JSON-Format enthält sie | Im lesbaren Format fehlt Kontext |
| Kein Startbanner mit Konfiguration; keine Zeile zu Scheduler/Jobs/Modulen | Man weiß nicht, was aktiv ist (z. B. warum eine Funktion fehlt, L4) |
| `httpx`/`urllib3` auf WARNING (richtig wegen Schlüsseln in URLs) → **ausgehende Abrufe unsichtbar**, auch Dauer | Kein Hinweis, welche Quelle langsam/fehlerhaft ist |
| Keine Meldung bei langen DB-Sperrwartezeiten (`busy_timeout` 30 s) | M5/M6-Auswirkungen nicht sichtbar |
| Keine Korrelation zwischen Browser-Fehlerseite und Logzeile | Nutzer kann keinen Fehler benennen |

### Maßnahmen

1. **Anfrage-Zeile (Access-Log, neu)** – reine ASGI-Middleware (kein `BaseHTTPMiddleware`, damit Streaming/Fehlerpfade unverändert bleiben).
   Felder: `rid`, Methode, **Pfad ohne Query-Werte** (nur Schlüsselnamen: `tab=*`), Status, Dauer in ms, Antwortgröße.
   `LOG_ACCESS=slow` (Standard): Status ≥ 400 → WARNING, Dauer ≥ 1 s → WARNING, sonst keine Zeile; `all`: jede Anfrage auf INFO; `off`.
   Dauerabfragen (`/healthz`, `/static/*`, `/news/pipeline/live`, Fortschritts-Endpunkte) sind ausgenommen, sonst Flut (3-s-Polling).
2. **Request-ID:** je Anfrage 6 Zeichen (`contextvars`), als Antwort-Header `X-Request-ID`, im Logfilter an **jede** Logzeile der Anfrage
   gehängt und auf der Fehlerseite angezeigt („Fehler-ID a3f9c2“). Der Nutzer nennt die ID, `docker logs portfolia | grep a3f9c2` liefert
   alles dazu.
3. **Globaler Ausnahmehandler** für unbehandelte Fehler: *eine* Zeile `ERROR 500 GET /news rid=a3f9c2 12ms UndefinedError: 'discovery' is
   undefined @ app/templates/news.html:214` plus Traceback **gekürzt auf App-Frames** (Framework-Frames von Starlette/uvicorn/anyio
   ausgeblendet; vollständig bei `LOG_LEVEL=DEBUG`). Antwort bleibt die bestehende `error.html` (mit Fehler-ID).
4. **Job-Rahmen (`run_job`):** `DEBUG Job x gestartet`; Erfolg **`INFO Job x ok in 41.2 s {Kurzergebnis}`** nur, wenn der Lauf ≥ 5 s dauerte oder
   etwas getan hat (Ergebnis mit Zählern > 0), sonst `DEBUG` – die Jobs laufen rechnerisch **≈ 1 380-mal pro Tag** (`import_poll` und
   `datasources_sync` je 288, `prices_crypto`/`news_analyze`/`news_enrich` je 96–144), eine INFO-Zeile je Lauf wäre Rauschen. Bei Fehler
   **`ERROR` mit vollständigem Traceback (`exc_info`) unabhängig vom Level**. Läuft ein Job länger als sein Intervall, WARNING
   „überschreitet Intervall“. Scheduler-Ereignisse (verpasst, `max_instances`, Fehler) als WARNING mit Jobname und Verspätung.
   Lange Läufe (Welle, Analyse) melden Stufenwechsel auf INFO.
5. **Startbanner:** Version/Build, Python- und SQLite-Version, Zeitzone, Datenverzeichnis und DB-Größe, Schema-Version, `AUTH_MODE`
   (bei `none` WARNING, vgl. M8), Scheduler an/aus und **Anzahl Jobs mit nächstem Lauf**, geladene/fehlgeschlagene Module (L4),
   konfigurierte Anbieter **nur als ja/nein** (Schlüssel nie ausgeben), aktive Log-Einstellungen. Diese Zeilen sind **keine** Geheimnisse; der
   `SecretRedactor` bleibt als letzter Filter aktiv.
6. **Ausgehende Abrufe:** `httpx`-Event-Hooks im gemeinsamen Client (`ctx.http`) und in `SafeFetcher`: `DEBUG GET host/pfad 200 340ms`
   (Query nie), `WARNING` bei Fehler/Zeitüberschreitung/≥ 5 s. Die `httpx`-Bibliothek bleibt auf WARNING (Schlüssel in URLs).
7. **Datenbank:** Anweisungen ≥ 500 ms und Sperrwartezeiten ≥ 1 s als WARNING (erste 120 Zeichen des SQL, **ohne Parameter**, Threadname).
8. **Betriebszustand (stündlich, INFO):** `Threads`, offene DB-Verbindungen, RSS-Speicher (aus `/proc/self/status`, keine neue Abhängigkeit),
   Jobs in Warteschlange/laufend, verpasste Läufe seit Start. Das liefert die Messwerte für M4/M6.
9. **Formatierung:** `TextFormatter` hängt `extra`-Felder als `key=value` an; `LOG_LEVELS="app.news=DEBUG,app.db=WARNING"` zum Anheben
   einzelner Bereiche, ohne alles auf DEBUG zu stellen.
10. **Flutschutz:** identische Meldungen desselben Loggers werden pro Minute zusammengefasst (`… (37× in 60 s)`). Hintergrund: Der Textabruf
    erzeugte an einem Tag 82 Netzwerkfehler – einzeln ist das nur Rauschen.
11. **Docker:** `docker-compose.yml` und README: `logging: {driver: json-file, options: {max-size: "10m", max-file: "3"}}` (sonst wächst das
    Containerlog unbegrenzt), `LOG_ACCESS`/`LOG_LEVELS` dokumentieren, Beispiel „Fehler finden“ (`docker logs --since 30m portfolia | grep -E
    'ERROR|WARNING'`).
12. **Optional (Entscheidung E3):** Ringpuffer der letzten 2 000 Zeilen (≥ INFO, redigiert) als Download auf der Diagnose-Seite – hilfreich,
    wenn man auf Unraid nicht bequem an `docker logs` kommt.

### Beispiel: Wie die Ausgabe aussehen soll
```
2026-10-28 06:15:11 INFO    app: Portfolia 0.61.0 (Build 6a70fe8) | Python 3.11.9 | SQLite 3.45.1 | TZ Europe/Berlin
2026-10-28 06:15:11 INFO    app: Daten /data (DB 84 MB, Schema 33) | Auth=none | Scheduler an, 28 Jobs | Module 25/25
2026-10-28 06:15:11 WARNING app: AUTH_MODE=none – jede Person im Netz sieht alle Daten (Hinweis: AUTH_MODE=basic setzen)
2026-10-28 06:15:11 INFO    app: Anbieter: coingecko=ja finnhub=nein youtube=ja gemini=ja anthropic=nein
2026-10-28 06:20:03 INFO    app.jobs: Job news_enrich ok in 41.2 s {abgerufen: 38, gesperrt: 2}
2026-10-28 06:21:40 WARNING app.http: GET /journal 200 2310 ms rid=a3f9c2
2026-10-28 06:22:05 ERROR   app.web: 500 GET /news?tab=* rid=b41d7e 12 ms UndefinedError: 'discovery' is undefined @ news.html:214
2026-10-28 06:30:00 INFO    app.health: Threads 14 | DB-Verbindungen 9 | RSS 212 MB | Jobs laufend 1 | verpasst seit Start 0
```

### Sicherheit und Nebenwirkungen (verbindlich)
* **Nie** loggen: Query-Werte, Formular-/Upload-Inhalte, Header (`Authorization`, `Cookie`, `X-Api-Key`), Antwortkörper, SQL-Parameter.
  Test: Anfrage mit `?key=SECRET123&token=abc` und gesetztem Schlüssel → weder in `text` noch in `json` auffindbar.
* **Last:** Standard `LOG_ACCESS=slow` erzeugt im Normalbetrieb kaum Zeilen. Das Zusatzvolumen (Job-Zeilen nach der Regel aus Punkt 4,
  Betriebszustand 24/Tag, langsame/fehlerhafte Anfragen) ist **nicht gemessen**; Ziel ≤ 500 Zeilen/Tag im Leerlauf, vor der Freigabe an einem
  24-h-Lauf mit Demo-Daten und Scheduler zu bestätigen (Zählung der Zeilen je Logger).
* **Pfad:** Logging darf keine Anfrage verlangsamen oder abbrechen (alle neuen Handler fangen eigene Fehler ab; Messung der Mehrkosten pro
  Anfrage < 0,2 ms im Test).
* **Verhalten bei vorhandenen Installationen:** neue Zeilen kommen hinzu, bestehende Zeilen und Felder (JSON) bleiben unverändert;
  `event_log`-Seite zeigt weiterhin WARNING+.

### Tests
* `caplog`/Stream-Test: 500-Route → eine ERROR-Zeile mit Methode, Pfad, `rid`, Ausnahmetyp; Fehlerseite enthält dieselbe ID.
* Anfrage ≥ 1 s (Fake-Handler) → WARNING; schnelle Anfrage → keine Zeile; `/healthz` nie.
* `run_job`: Erfolg → INFO mit Dauer; Fehler → ERROR mit Traceback bei `LOG_LEVEL=INFO`.
* `TextFormatter` gibt `extra` aus; JSON unverändert (Snapshot-Test auf Feldnamen).
* Redaktion: Geheimnisse in Pfad/Query/Ausnahmetext/SQL-Parametern erscheinen nicht.
* Flutschutz: 100 identische Warnungen → ≤ 3 Zeilen pro Minute + Zusammenfassung.
* Startbanner enthält keinen Schlüsselwert (Test mit gesetzten Dummy-Schlüsseln).

## 4. Zeit- und Risikoübersicht

| Paket | Aufwand (grob) | Hauptrisiko | Gegenmaßnahme |
|---|---|---|---|
| A | 0,5 Tag | Pillow-13-Inkompatibilität (OCR/Dockerfile) | Docker-Build in CI, Rückfall auf 12.3.0 + Formatliste |
| B | 1–2 Tage | Logflut, versehentliche Geheimnisse | Standard `slow`, Flutschutz, Redaktionstests |
| C | 1–2 Tage | bisher verdeckte Fehler werden sichtbar | Klasse C zuerst, Crawler-Test, B davor |
| D | 1 Tag + 24 h Messung | Pool-Änderung verschiebt Lastverhalten | Messwerte vor/nach, Wächtertests |
| E | 1 Tag | Telegram-Bildabruf (LAN) bricht | `allow_hosts` nur für den Bridge-Host, Test |
| F | 2–3 Tage | Löschen „toter“ Funktionen, die Plug-ins nutzen | je Funktion `git grep` + Registrierungs-Check |

## 5. Entscheidungen für dich

* **E1 Log-Format im Container:** Standard ist heute `json` (maschinenlesbar, in `docker logs` schlecht lesbar). Vorschlag: Standard `text`
  mit `key=value`-Feldern, `LOG_FORMAT=json` bleibt wählbar. *Nachteil:* Wer JSON weiterverarbeitet, muss es setzen (Hinweis im README/Banner).
* **E2 Access-Log-Standard:** `slow` (nur Fehler und langsame Anfragen, Vorschlag) oder `all` (jede Anfrage; mehr zu lesen, aber ein Verlauf).
* **E3 Ringpuffer-Download** auf der Diagnose-Seite (Punkt 12): ja/nein.
* **E4 M8 Oberfläche:** nur Logwarnung (Vorschlag für B) oder zusätzlich ein Hinweisband in der Oberfläche (eigene Entscheidung zur Darstellung,
  Variable `AUTH_MODE_ACK=true` zum Ausblenden).
* **E5 Reihenfolge:** A zuerst (Vorschlag, wegen H1) oder B zuerst (Logging hilft sofort bei der Fehlersuche, die du gerade im Betrieb hast).

## 6. Offene Annahmen und Grenzen

* Aufwandsangaben sind Schätzungen; der Volltest (~20 Min.) bestimmt den Takt.
* Zu M7 ist der genaue Bridge-Host-Pfad im Telegram-Code vor der Umsetzung zu prüfen.
* M4/M6 sind abgeleitete Risiken. Zeigen die Messwerte aus Paket B keine Verdrängung und keinen nennenswerten Speicher, entfallen die
  Pool-Trennung und die Threadpool-Begrenzung; es bleibt dann bei `cache_size` und der Sichtbarkeit.
* Die Abdeckungszahlen stammen aus dem Stand 0.58.0.
