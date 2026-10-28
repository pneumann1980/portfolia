# Projektanalyse (Stand: Version 0.58.0, 27.10.2026)

Prüfung des gesamten Projekts auf Fehler, nicht erreichbaren Code, Sicherheits- und Betriebsrisiken – **nur lesend, es wurde
nichts am Code geändert**. Dieses Dokument hält Befunde, Auswirkungen und Verbesserungsvorschläge fest; die Umsetzung ist eine
getrennte Entscheidung.

## Behebungsstand (Stand 0.65.0)

| ID | Status | Version |
|---|---|---|
| H1 Pillow | behoben (13.0.0, `pip-audit` sauber, in der CI geprüft) | 0.60.0 |
| M1 `tab` → 500 | behoben (Standardreiter) | 0.60.0 |
| M2 ID ≥ 2⁶³ → 500 | behoben (zentraler Handler → 404) | 0.60.0 |
| M3 stille Fehler | behoben (75 → 25 geprüfte Stellen, Wächtertest; Schutzprüfungen brechen ab) | 0.62.0 |
| M4 gemeinsamer Pool | behoben (Pool „news“, verpasste Läufe im Log) | 0.63.0 (0.61.0) |
| M5 `async` + DB | behoben (Rest im Thread-Pool, Wächtertest) | 0.63.0 |
| M6 Verbindungen | gemildert (Cache 8 → 4 MB, 24 statt 40 Handler-Threads, Messwerte stündlich im Log) – Wirkung im Betrieb zu prüfen | 0.63.0 |
| M7 Bildproxy | behoben (SafeFetcher, Telegram-Bridge als ausdrückliche Ausnahme) | 0.64.0 |
| M8 `AUTH_MODE=none` | Warnung im Log beim Start (kein Hinweisband – Entscheidung des Auftraggebers); rotes Band nur bei blockierten Benutzeraktionen | 0.61.0 |
| N1 Teaser-Host | behoben (aufgelöster Host, Zwischenspeicher) | 0.60.0 |
| L1 `preexec_fn` | behoben | 0.64.0 |
| L2 `\|safe` | behoben (`\|tojson`) | 0.64.0 |
| L3 toter Code | 20 Funktionen entfernt | 0.65.0 |
| L4 Modul-Laden | behoben (Fehler im Log und Startbanner) | 0.61.0 |
| L5 Doku | Kopf aktualisiert | 0.65.0 |
| L6 `date.today()` | behoben, Regel in `ruff` | 0.65.0 |
| L7 CI | `pip-audit` und `mypy` (melden), Abdeckungsgrenze nachts | 0.65.0 |
| L8 `assert` | behoben (42 Stellen), Regel `S101` für `app/` | 0.65.0 |

## 1. Vorgehen und Grenzen

| Prüfung | Werkzeug / Methode | Ergebnis in Kürze |
|---|---|---|
| Statische Analyse | `ruff` (Projektregeln), `ruff` mit zusätzlichen Fehlerklassen, `vulture` (toter Code), `bandit` (Sicherheit), `mypy` | Lint sauber; Sicherheit unauffällig (Details §3); mypy 586 Meldungen, überwiegend Annotationsrauschen |
| Abhängigkeiten | `pip-audit` (direkte + installierte Pakete, 96) | **Pillow 12.3.0: 38 bekannte Schwachstellen** (§3 H1); übrige Laufzeitpakete ohne Meldung |
| Erreichbarkeit | Routen-Tabelle (314) gegen Verweise in Templates/JS/Python; Import aller 201 Module; Registrierungsketten (Routen, Jobs, Konnektoren) | Alle registrierenden Module werden geladen; keine toten Links; 1 Endpunkt ohne Verweis |
| Dynamisch: Crawler | Start bei `/`, folgt allen internen Links (978 Seiten), mit Beispieldaten und mit leerer Datenbank; Jinja meldet jede gerenderte undefinierte Variable | **0 × HTTP 500**, **0 undefinierte Variablen** |
| Dynamisch: Fuzzing | 1 802 GET-Anfragen mit Müllparametern, 561 POST-Anfragen mit Müllformularen, 42 Datei-Uploads (kaputte ZIP/PDF/CSV/Zip-Bombe) | POST und Uploads robust; **2 Fehlerklassen bei GET** (§3 M1, M2) |
| Codepfade | Zeilen- und Zweigabdeckung der gesamten Testsuite (1 425 Tests) | siehe §4 |
| Gezieltes Lesen | Sicherheit (Auth, CSRF, CSP, SSRF, ZIP/Upload, Logs), Datenbankschicht, Scheduler, Fehlerbehandlung, Neuentwicklungen der letzten Versionen | siehe §3 |

**Nicht geprüft:** Rechenkorrektheit der Steuer- und Bestandslogik im Detail (dafür gibt es eigene Tests und frühere Reviews),
Verhalten gegen echte Börsen-/Chain-/KI-Schnittstellen (kein Netz im Prüfumfeld), Lastverhalten auf der Zielhardware, echte
Nutzerdaten. Aussagen zu Laufzeitverhalten (Scheduler, Speicher) sind aus dem Code abgeleitet und **nicht gemessen**; sie sind als
„Risiko“ gekennzeichnet.

Umfang des Projekts: 248 Python-Dateien (≈ 83 000 Zeilen), 118 Templates, 59 Tabellen in 33 Migrationen, 28 Hintergrundjobs,
314 Routen, 36 000 Zeilen Tests.

## 2. Übersicht der Befunde

| ID | Schwere | Bereich | Befund | Aufwand |
|---|---|---|---|---|
| H1 | **hoch** | Abhängigkeiten | Pillow 12.3.0 hat 38 bekannte Schwachstellen (Behebung ab 13.0.0) | S–M |
| M1 | mittel | Web | Unbekannter `tab`-Wert auf `/news` → HTTP 500 | S |
| M2 | mittel | Web | Ganzzahl-Pfadparameter ≥ 2⁶³ → HTTP 500 auf 25 Routen | S |
| M3 | mittel | Datenbank | 53 stille `except Exception`; u. a. wird die Datenstand-Prüfung der Korrekturen bei jedem Fehler übersprungen | M |
| M4 | mittel | Betrieb | Kern- und News-Jobs teilen einen Pool mit 3 Threads; verspätete Läufe werden nach 10 Min. verworfen (Risiko, nicht gemessen) | S–M |
| M5 | mittel | Betrieb | 8 `async`-Handler schreiben blockierend in SQLite im Ereignisschleifen-Thread | S |
| M6 | mittel | Betrieb | SQLite-Verbindungen je Thread ohne Rückgabe: bis ≈ 370 MB im Ungünstigsten (Risiko, nicht gemessen) | S |
| M7 | mittel | Sicherheit | Bildproxy: Prüfung der Zieladresse und Abruf sind getrennt (DNS-Rebinding), kein Anpinnen wie beim Artikelabruf | S–M |
| M8 | mittel | Sicherheit | Standard `AUTH_MODE=none`: jeder im Netz sieht alle Finanzdaten (dokumentiert, aber ohne Warnung in der Oberfläche) | S |
| N1 | niedrig–mittel | News (0.58.0) | „Nur Teaser“-Erkennung nutzt bei Google-News-Meldungen den Host `news.google.com` statt des Verlags | S |
| L1 | niedrig | Betrieb | `preexec_fn` in einem Prozess mit Threads (Beleganalyse) | S |
| L2 | niedrig | Web | `json.dumps(...)\|safe` in `<script type="application/json">` (2 Templates): `</script>` wird nicht maskiert | S |
| L3 | niedrig | Wartung | 27 unreferenzierte Funktionen, 19 nur von Tests genutzt | S |
| L4 | niedrig | Betrieb | Module-Laden mit `suppress(ModuleNotFoundError)`: fehlende Abhängigkeit schaltet Funktion still ab | S |
| L5 | niedrig | Doku | `docs/MILESTONES.md` nennt im Kopf „Version 0.17.0“; README ≈ 2 000 Zeilen | S |
| L6 | niedrig | Zeit | 4 × `date.today()` statt `now_local().date()` | S |
| L7 | niedrig | Qualität | Kein Typprüfer, kein Abhängigkeits-Audit und keine Abdeckungsgrenze in der CI | S–M |
| L8 | niedrig | Qualität | 42 `assert` in Produktivcode, 533 Importe innerhalb von Funktionen | M |

Aufwand: S = Stunden, M = ein bis wenige Tage, L = länger.

## 3. Befunde im Detail

### H1 – Pillow 12.3.0 mit 38 bekannten Schwachstellen
* **Beleg:** `pip-audit` meldet 38 Einträge (u. a. Lese-/Schreibfehler in Decodern, Use-after-free beim TIFF-Schreiben, DoS über
  GIF/PPM); alle sind in **13.0.0** behoben. Alle anderen 17 direkten Pakete sind ohne Meldung; nur `pip`/`setuptools` (Build-Werkzeuge)
  melden etwas.
* **Wo Pillow läuft:** OCR-Vorverarbeitung hochgeladener Belege (`documentimport/extract.py`, im **isolierten Kindprozess** mit Speicher-
  und Zeitgrenzen) und Belegvorschau (`documentimport/service.py:preview`, **im Hauptprozess**, aus dem lokal gespeicherten Original).
  Mit dem vorbereiteten Bildverkleinern (0.59.0) kämen Bilder aus dem Internet hinzu.
* **Auswirkung:** Ein manipuliertes Bild kann im Hauptprozess zu Absturz/Speicherverbrauch (DoS) führen, bei Speicherfehlern im
  schlimmsten Fall zur Codeausführung. Voraussetzung ist ein eigener Upload oder ein bösartiger Feed – im reinen Heimnetz mit eigenen
  Dokumenten ist das Risiko gering, aber nicht null.
* **Vorschlag:** (1) Pillow auf 13.0.0 anheben und die Testsuite laufen lassen. (2) Dekoder für Bilder aus dem Netz auf JPEG/PNG/WebP
  beschränken (`Image.open(..., formats=[...])`; im vorbereiteten 0.59.0 enthalten). (3) Die Belegvorschau ebenfalls im Kindprozess
  rendern oder die Formatliste auch dort einschränken. (4) `pip-audit` in die CI aufnehmen (siehe L7).

### M1 – Unbekannter `tab`-Wert → HTTP 500
* **Beleg:** `GET /news?tab=foo` → `UndefinedError: 'discovery' is undefined`. Das Template rendert für jeden unbekannten Wert den
  Zweig „Quellen“, die Route befüllt dessen Variablen aber nur für `tab=sources`.
* **Auswirkung:** Fehlerseite statt Fehlermeldung bei manipulierter oder veralteter URL (z. B. Lesezeichen eines entfernten Reiters);
  Fehler im Protokoll. Keine Datenwirkung.
* **Vorschlag:** `tab` als `Literal[...]` deklarieren (FastAPI antwortet dann mit 422) oder auf einen Standardwert zurückfallen.
  Dasselbe Muster bei `sort`, `status` prüfen.

### M2 – Ganzzahl-Pfadparameter ≥ 2⁶³ → HTTP 500
* **Beleg:** `GET /news/99999999999999999999` → `OverflowError: Python int too large to convert to SQLite INTEGER`. Betroffen sind
  25 Routen mit `int`-Pfadparameter (u. a. `/belege/{bid}/datei`, `/journal/csv/{bid}/…`, `/journal/documents/{doc_id}/…`,
  `/settings/datasources/{sid}/…`, `/tax/data/file/{fid}`, `/news/{item_id}`, `/watchlist/item/{item_id}`, `/img/news/{item_id}`).
* **Auswirkung:** Wie M1 – 500 statt 404, Rauschen im Fehlerprotokoll; kein Sicherheitsproblem.
* **Vorschlag:** Einen gemeinsamen Typ `Id = Annotated[int, Path(ge=1, le=2**63 - 1)]` für alle ID-Parameter verwenden **oder** einen
  Ausnahmehandler `OverflowError → 404`.

### M3 – Verschluckte Datenbankfehler (53 stille Ausnahmebehandler)
* **Beleg:** Von 189 breiten `except Exception`-Blöcken schreiben 53 weder ein Log noch werfen sie neu (Häufung:
  `diagnosis/actions.py` 8, `assetchange/*` 10, `diagnosis/integrity.py` 3). Viele davon sind der Typ „Tabelle fehlt (DB vor
  Migration N)“ mit Ersatzwert `[]`/`{}`/`None` – obwohl `Database.migrate()` beim Start **immer** auf den neuesten Stand bringt, der
  Fall also nicht eintreten kann.
* **Auswirkung:** Tritt ein *echter* Fehler auf (z. B. gesperrte Datenbank trotz Timeout, SQL-Fehler nach einer Änderung), liefert die
  Funktion still leere Daten statt eines Fehlers: `assetchange.merge.canonical()` → `{}` (Zusammenführungen werden ignoriert, Bestände
  erscheinen getrennt), `diagnosis.actions.decisions()` → `[]` (getroffene Entscheidungen scheinen verschwunden). Besonders kritisch:
  `diagnosis.actions.assert_version()` – die Prüfung, ob sich die Daten seit der Vorschau geändert haben – **kehrt bei jedem Fehler still
  zurück**, die Korrektur würde dann ohne diese Schutzprüfung übernommen.
* **Vorschlag:** Die „Tabelle fehlt“-Abfangungen entfernen (migrationsgesichert) oder auf `sqlite3.OperationalError` mit „no such table“
  einschränken und mindestens `log.warning` schreiben; bei `assert_version` Fehler als Abbruch behandeln, nicht als Freigabe.

### M4 – Ein gemeinsamer Thread-Pool für Kern- und News-Jobs (Risiko)
* **Beleg:** `Scheduler` hat den Pool „default“ mit 3 Threads; fast alle Jobs laufen dort. `misfire_grace_time=600` s und `coalesce=True`:
  ein wartender Lauf, der länger als 10 Minuten verspätet ist, wird **übersprungen**. Die News-Jobs sind lang (Analyse bis 8 Min. je
  10-Minuten-Takt, Textabruf mit Wartezeiten und Zeitüberschreitungen, Abruf aller Quellen); dazu Datenquellen-Abrufe und Historie.
* **Auswirkung (abgeleitet, nicht gemessen):** Bei gleichzeitiger Last können Kurs- oder Import-Jobs ausfallen, ohne dass die Oberfläche
  es zeigt. Frühere Beobachtung des Nutzers: viele Netzwerkfehler im Textabruf (82 an einem Tag) verlängern die Läufe.
* **Vorschlag:** Eigener Pool für die News-Jobs (2 Threads), Kern-Jobs getrennt; Misfires im Log und in der Statusseite sichtbar machen.

### M5 – Blockierende Schreibzugriffe in `async`-Handlern
* **Beleg:** `news.mark_read`, `news.read_all`, `news.channel_action`, `news.source_action`, `watchlist.create_position`,
  `web.actions.save_settings`, `csvimport.undo_action`, `csvimport.review_bulk_execute` führen `db.x(...)`/`db.q(...)` direkt im
  Ereignisschleifen-Thread aus (die übrigen Handler sind `def` und laufen im Thread-Pool).
* **Auswirkung:** Hält ein anderer Thread die Schreibsperre (Import, Quellenübernahme, lange Transaktion), blockiert der Handler bis zu
  30 s (`busy_timeout`) **die gesamte Anwendung** – keine andere Anfrage wird bedient.
* **Vorschlag:** Handler als `def` deklarieren oder den Datenbankteil mit `run_in_threadpool` auslagern.

### M6 – SQLite-Verbindungen je Thread ohne Rückgabe (Risiko)
* **Beleg:** `Database.conn` legt je Thread eine Verbindung an (`cache_size = -8000` ≈ 8 MB); geschlossen wird nur in `run_job`. Der
  Standard-Pool von Starlette hat bis zu 40 Threads, dazu 6 Scheduler-Threads.
* **Auswirkung:** Im Ungünstigsten ≈ 46 × 8 MB ≈ 370 MB Seitencache zusätzlich zum Arbeitsspeicher der App; auf kleinen Unraid-Systemen
  spürbar. Nicht gemessen.
* **Vorschlag:** `cache_size` auf ≈ 2 MB senken oder Verbindungen nach jeder Anfrage schließen (Middleware), Pool begrenzen.

### M7 – Bildproxy ohne Anpinnen der geprüften Adresse
* **Beleg:** `news/images.cached_image` prüft den Host (`_public_host`) und ruft ihn danach über `ctx.http` unter dem Namen ab. Der
  Artikelabruf (`SafeFetcher`) pinnt dagegen die geprüfte Adresse und ignoriert Proxy-Umgebungsvariablen.
* **Auswirkung:** Ein Feed-Betreiber kann per DNS-Rebinding erreichen, dass der Server ein Bild von einer internen Adresse (Router,
  Kamera, NAS) holt und ausliefert. Begrenzt durch „nur `image/*`, ≤ 2 MB, nur Port 80/443“.
* **Vorschlag:** `SafeFetcher` auch für Bilder verwenden.

### M8 – Standard ohne Anmeldung
* **Beleg:** `Config.auth_mode = "none"`; README: „Nur im LAN betreiben“. Basic Auth (PBKDF2/bcrypt, Drosselung, CSRF, Header) ist
  sorgfältig umgesetzt, aber optional.
* **Auswirkung:** Wer den Port erreicht (WLAN-Gäste, kompromittiertes Gerät, Port-Weiterleitung), sieht und ändert alle Daten.
* **Vorschlag:** Beim Start Warnung ins Log und Hinweisband in der Oberfläche, solange `AUTH_MODE=none` und Zugriff nicht von
  `127.0.0.1`/privaten Adressen kommt; Einrichtungsassistent für Basic Auth.

### N1 – „Nur Teaser“-Erkennung bei Google News (eigener Befund aus 0.58.0)
* **Beleg:** `hosts.teaser_hosts` gruppiert nach dem Host der Meldungs-URL. Google-News-Meldungen haben immer `news.google.com`, der Abruf
  löst aber zum Verlag auf (`final_url`). Die Ergebnisse vieler Verlage landen so unter einem Host.
* **Auswirkung:** Überwiegen Sperren/leere Seiten, würde *alles* von Google News als „nur Teaser“ gelten und nicht mehr abgerufen.
  Schwellen (≥ 8 von 10, ≤ 1 Volltext) machen das unwahrscheinlich, aber möglich.
* **Vorschlag:** Host aus `COALESCE(final_url, url)` bilden und `news.google.com` ausnehmen. Außerdem die Abfrage je Lauf zwischenspeichern
  (wird derzeit bei jedem `_raw_candidates`-Aufruf neu ausgeführt, auch durch die 3-Sekunden-Aktualisierung der Statusseite).

### Niedrige Befunde
* **L1 `preexec_fn`** (`documentimport/worker.py`): Laut Python-Dokumentation in Prozessen mit Threads unsicher (Verklemmung möglich).
  Besser: Grenzen im Kindprozess selbst setzen (im Startskript `_BOOT`).
* **L2 `|safe`** (`partials/klaerung_fenster.html`, `diagnosis_windows.html`): `json.dumps` maskiert `</script>` nicht. Die Daten sind
  derzeit interne IDs/Beschriftungen und die CSP (`script-src 'self'`) verhindert Skriptausführung; sauberer ist `|tojson`.
* **L3 Toter Code:** unreferenziert u. a. `analytics.colors.segment_color`, `analytics.performance.xnpv`, `analytics.valuation.utc_now`,
  `assetchange.service.series_of`, `csvimport.review.by_source`, `datasources.chains.btckeys.serialize`, `datasources.service.review_summary`,
  `datasources.vault.needs_rotation`, `datasources.wallet.split_token_key`, `diagnosis.audit._curated/rating`,
  `diagnosis.clarify._equiv/_discriminating`, `jobs.exports._save_settings`, `ledger.engine.realized_by_asset`, `news.enrich.done_count`,
  `tax.module.active_pack`, `util.timeutil.daterange/month_start`, `web.routes.pages.fmt_ts/now_utc`; nur von Tests genutzt u. a.
  `documentimport.evidence.safe_for_booking` (die tatsächliche Prüfung steckt in `documentimport/bridge.py`), `documentimport.txdocs.unlink_document`,
  `jobs.maintenance.backup_path`, `ledger.provenance.retro_map`. Außerdem: Endpunkt `GET /api/asset/{id}/position` ohne Aufrufer. (Falsch
  positive Meldungen von `vulture` – z. B. per Dekorator registrierte Konnektor-Klassen – sind aussortiert.)
* **L4 `suppress(ModuleNotFoundError)`** (`main._load_modules`): Fehlt eine Abhängigkeit *innerhalb* eines Moduls, wird dessen Funktion
  ohne Meldung nicht geladen (die Routen fehlen dann mit 404). Besser: nur das jeweilige Modul selbst tolerieren und sonst loggen.
* **L5 Doku:** `docs/MILESTONES.md` Kopfzeile „Stand 02.10.2026 · Version 0.17.0“; README ≈ 2 000 Zeilen mit verstreuten Versionshinweisen.
* **L6 `date.today()`** (`documentimport/enrich.py`, `prices/yahoo.py`, `prices/demo.py`): nutzt die Prozess-Zeitzone; im Container ist `TZ`
  gesetzt (identisch zur App-Zeitzone), außerhalb nicht. Einheitlich `now_local().date()` verwenden.
* **L7 CI:** `ruff` und `pytest` laufen; es fehlen `pip-audit`, ein (schrittweiser) Typprüfer und eine Abdeckungsgrenze. mypy meldet 586
  Punkte, vor allem Annotationen (z. B. Methoden namens `list` verdecken das Builtin im Klassenkörper), dazu einige echte
  Optional-Zugriffe.
* **L8** 42 `assert` in Produktivcode (entfallen mit `python -O`), 533 Funktions-lokale Importe (verbergen Importzyklen).

### Positiv festgehalten (geprüft, ohne Befund)
* **Eingaben:** 561 Müll-POSTs und 42 manipulierte Uploads (kaputte/verschlüsselte ZIP, Zip-Bombe, Zufallsbytes als PDF/PNG/CSV) ohne einen
  einzigen 500er; ZIP-Verarbeitung ohne Entpacken auf die Platte, mit Obergrenzen für Größe, Anzahl, Kompressionsverhältnis, Symlinks.
* **Web-Sicherheit:** CSP ohne externe Quellen (außer YouTube-Vorschaubilder), `X-Frame-Options`, CSRF per Double-Submit-Cookie plus
  `Sec-Fetch-Site`, `Cache-Control: no-store`, Basic Auth mit Drosselung; `GET` ändert nur an einer Stelle Daten („gelesen“ beim Öffnen).
* **SSRF beim Artikelabruf:** gepinnte Adresse, private/CGNAT/IPv4-in-IPv6-Bereiche gesperrt, Weiterleitungen einzeln geprüft.
* **Geheimnisse:** API-Schlüssel nur im Header bzw. per Redaktionsfilter aus allen Logzeilen entfernt (auch aus Drittbibliotheken, auch
  Schlüssel aus dem verschlüsselten Speicher, die nur zeitweise bekannt sind).
* **Daten:** Geldbeträge in `Decimal`; Schreibzugriffe mit Transaktionen, wo mehrere Schritte zusammengehören; Absturzwiederherstellung
  (z. B. unterbrochene Quellenübernahme wird beim nächsten Start als „unterbrochen“ abgeschlossen).
* **Datenbank:** keine ungenutzte Tabelle (außer der Migrations-Hilfstabelle), keine Einstellung mit abweichendem Standardwert.
* **Service Worker:** speichert nichts zwischen.

## 4. Erreichbarkeit der Codepfade

Ergebnis der statischen und dynamischen Prüfung (Abdeckung siehe unten):

* **Routen:** 314 Routen; 312 werden aus Oberfläche, JavaScript oder Python verlinkt, 1 nur aus Tests (`GET /api/asset/{id}/chart`),
  1 nirgends (`GET /api/asset/{id}/position`). Der Crawler erreichte 978 verschiedene Seiten ohne Fehler; Formular-Ziele (POST) werden
  wie erwartet mit 405 beantwortet, wenn man sie per GET aufruft.
* **Module:** alle 201 importierbar (auch mit Warnungen als Fehler). 25 Module werden von keinem anderen Modul importiert – es sind genau
  die Plug-in-Module (`news.module`, `watchlist.module`, Konnektoren, `*_web` …), die `main._load_modules` beim Start lädt; es gibt kein
  registrierendes Modul außerhalb dieser Liste.
* **Jobs:** 28 registriert; jeder getriggerte Name ist registriert; die 5 Jobs ohne Intervall (`auto_export`, `coingecko_catalog`,
  `csv_prices`, `news_rematch`, `news_wave`) werden jeweils irgendwo ausgelöst.
* **Einstellungen:** 76 Standardwerte; vier gelesene Schlüssel ohne Eintrag in `settings_store` (`isin.auto`, `news.telegram_bridge`,
  `tax.ruleset`, `tax.ruleset_rev` – mit Fallback im Aufruf), zwei Standardwerte ohne Leser (`clarify.budget`, `clarify.limit`).
* **Nicht erreichbar im Sinne von „kann nie eintreten“:** die „Tabelle fehlt“-Abfangungen (M3) und die 25 Funktionen aus L3.

### 4.1 Testabdeckung (pytest-cov, gesamte Suite, Stand 0.58.0)

Gesamt **85,6 % Zeilen** (49 717 von 56 322 Anweisungen), **78,2 % Zweige** (15 802 von 20 214); 35 Dateien vollständig abgedeckt.
Gemessen auf dem Stand vor 0.59.0 (`app/news/images.py` ohne Verkleinerung; dort inzwischen mehr Tests).

| Modul | Abdeckung | Fehlende Zeilen | Bewertung |
|---|---|---|---|
| `app/cli.py` | 0 % | 64 | Kommandozeile nicht getestet (nur manuell) |
| `app/prices/yahoo.py` | 38 % | 87 | Netzwerkpfade; Fehlerbehandlung unbelegt |
| `app/news/youtube.py` | 55 % | 65 | API-Fehlerpfade, Kontingent |
| `app/documentimport/extract.py` | 57 % | 143 | OCR-/PDF-Randfälle (steuerrelevant: ISIN, Beträge) |
| `app/prices/market.py` | 57 % | 72 | Börsenzeiten/Fallbacks |
| `app/csvimport/profiles.py` | 60 % | 381 | größte Lücke absolut – Broker-Profile ohne Beispieldateien |
| `app/news/llm.py` | 58 % | 48 | Anbieter-Fehler, Limits |
| `app/news/service.py` | 69 % | 127 | |
| `app/news/module.py` | 74 % | 104 | Routen-Randfälle (siehe M1) |

**Einordnung:** Lücken liegen überwiegend in Netzwerk-/Fehlerpfaden (Mock-Transport fehlt) und in Importprofilen. Der Steuerkern
(`app/tax`, 3 241 Anweisungen) liegt mit 93,0 % deutlich höher; die Rechenpfade sind also gedeckt, Eingabe (CSV/OCR) ist das schwächere Glied. Hohe Zeilenabdeckung
belegt nicht die Korrektheit: M1/M2 (500er bei Eingaben außerhalb des Wertebereichs) bestehen trotz Abdeckung der Routen.
**Vorschlag:** (1) Parametrisierter Test „jede Route mit Grenzwerten“ (M1/M2), (2) Profil-Tests mit anonymisierten Beispieldateien
je Broker, (3) Mindestschwelle in CI (`--cov-fail-under=84`) gegen schleichenden Rückgang.

## 5. Verbesserungsvorschläge (nach Nutzen geordnet)

1. **Sofort (Stunden):** Pillow auf 13.0.0 (H1); `tab`/`sort`/`status` als `Literal` und ID-Typ mit Obergrenze (M1, M2); `assert_version` bei Fehler
   abbrechen (M3); Warnbanner bei `AUTH_MODE=none` (M8); Host-Bildung in `hosts.teaser_hosts` korrigieren (N1).
2. **Kurzfristig (Tage):** Stille Ausnahmebehandler bereinigen (M3); News-Jobs in eigenen Pool, Misfires sichtbar machen (M4); `async`-Handler mit
   DB-Zugriff auf `def` umstellen (M5); `cache_size` senken (M6); Bildabruf über `SafeFetcher` (M7).
3. **CI erweitern:** `pip-audit` (wöchentlich und bei Änderungen an `requirements.txt`), `mypy` schrittweise für neue/geänderte Module,
   Abdeckungsgrenze (Ist-Wert minus Puffer), Dependabot für Python und GitHub Actions.
4. **Aufräumen:** Toten Code entfernen (L3), `|tojson` statt `|safe` (L2), `date.today()` ersetzen (L6), Doku-Kopf aktualisieren (L5).
5. **Strukturell (länger):** Sehr große Module aufteilen (`csvimport/service.py` 2 670 Zeilen, `datasources/service.py` 2 578,
   `csvimport/module.py:make_router` 656 Zeilen), Rückgabewerte als Datenklassen statt `dict[str, Any]` (verringert mypy-Rauschen und
   Tippfehler), Betriebs-Dashboard (`/healthz` um Scheduler-Herzschlag und letzte Job-Ergebnisse erweitern).
