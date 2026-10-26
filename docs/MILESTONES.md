# Meilensteine: Entscheidungen, Grenzen, offene Fragen

Stand: 02.10.2026 · Version 0.17.0 · Branch `claude/portfolia-dashboard-s9p6zr`

Jeder Meilenstein endete mit lauffähigem Image, grünen Tests und Lint. Abnahmewerte stammen aus
`scripts/bench.py` bzw. `tests/test_scale.py` und `tests/test_privacy.py`.

---

## M1 – Datenmodell, Import, Bestände, Kurse, Dashboard, Positionen

**Entscheidungen**

* Ein Prozess (Uvicorn, 1 Worker) mit APScheduler im Hintergrund; SQLite im WAL-Modus mit
  threadlokalen Verbindungen; Migrationen über `PRAGMA user_version`.
* Import atomar und versioniert (letzte 10 Stände), Duplikaterkennung über SHA-256, Dateien erst ab
  15 s Alter (halbfertige Kopien), Fehlerbericht mit Datei/Zeile/Spalte; unbekannte Spalten werden
  mitgespeichert.
* Konventionen festgelegt: `from_qty` ohne Gebühr, `value_eur` brutto ohne Gebühr; Gebühren in
  Nicht-Fiat-Assets sind eigene Abgänge.
* Ledger mit `Decimal` (60 Stellen); fehlende Anschaffungen werden als „ohne Einstand“ geführt und
  gewarnt, nie still ignoriert. Cash-Führung je Konto wird erkannt und ist übersteuerbar.
* Kurse: Tagesschlusskurse „wie gehandelt“ plus Split-Faktor; aktuelle Kurse mit Zeitstempel/Quelle;
  Veraltung nach Handelstagen; CoinGecko mit einem Sammelaufruf und Monatsbudget.
* Frontend ohne Build-Kette: Jinja2 + HTMX, ECharts 6.1 lokal gebündelt, validierte Palette.

**Grenzen:** nur EUR als Basiswährung; Yahoo über yfinance ist inoffiziell (Ausfälle werden als
„veraltet“ markiert); CoinGecko-Demo liefert Historie nur 365 Tage – davor nur mit eindeutig
konfigurierten Yahoo-Paaren (Standard BTC-EUR, ETH-EUR).

## M2 – Detailansicht, Historie, Performance

**Entscheidungen**

* TTWROR täglich nach Portfolio-Performance-Methodik (Zuflüsse Tagesbeginn, Abflüsse Tagesende),
  Annualisierung erst ab 365 Tagen; IRR/XIRR mit Newton und Bisektion.
* Historie aus Tagesschlusskursen; fehlende Kurse werden aus Transaktionskursen geschätzt und markiert.
* Kauf-/Verkaufsmarker und Einstandslinie split-bereinigt aus den Kapitalmaßnahmen des Imports.
* CSRF als reine ASGI-Middleware (Double-Submit-Cookie, `Sec-Fetch-Site`).

**Grenzen:** Benchmarks als Kursindex (ohne Ausschüttungen); Intraday-Daten nur für 1T/1W und 8 Tage
vorgehalten.

## M3 – News und YouTube

**Entscheidungen**

* `/data/sources.yaml` mit Kommentar-erhaltendem Speichern; Quellen und YouTube-Handles werden **zur
  Laufzeit** verifiziert (die Entwicklungsumgebung hatte keinen Internetzugang); unerreichbare Quellen
  werden deaktiviert und gemeldet, Handles nie geraten.
* Relevanz: Aliase mit Wortgrenzen, Kontextpflicht für mehrdeutige Ticker, Gewichte je Quellentyp,
  Positionsgewicht, Aktualität (Halbwertszeit 48 h); Dedupe über URL und Titelähnlichkeit.
* Bilder: YouTube-Vorschaubilder direkt (CSP-Ausnahme `i.ytimg.com`), alle anderen serverseitig mit
  SSRF-Schutz zwischengespeichert.
* KI optional und standardmäßig aus; hartes Tages-Token-Budget; nur Asset-Namen und Artikeltexte werden
  übertragen. Standardmodell `claude-opus-5` (einstellbar). Für dieses Modell ist der serverseitige
  Modell-Fallback der API aktiviert (bei Überlast antwortet automatisch ein verwandtes Modell statt eines
  Fehlers).

**Grenzen:** Einige Börsen (MEXC, XT, CoinEx, Gate.io) bieten keinen offiziellen Feed – als inaktiv mit
Hinweis enthalten. Discovery benötigt `YOUTUBE_API_KEY` (Kontingent 10.000 Einheiten/Tag, Suche kostet 100).

## M4 – Steuern und Haltefristen, PDF-Berichte, Datenqualität

**Entscheidungen**

* Modulare Architektur: länderneutrale Schnittstelle, Regelwerke als Pakete, Parameter je Jahr in YAML,
  lokale Aktualisierung über `/data/tax_rules/<id>.yaml` (siehe `docs/tax-rulepacks.md`).
* Regelwerk Deutschland: § 23 (Haltefrist nach BGB inkl. Schaltjahr, FIFO je Wallet, Freigrenze je Jahr),
  § 22 Nr. 3 (Freigrenze 256 €), Kapitalerträge mit Aktien-Topf, Teilfreistellung, Vorabpauschale,
  Quellensteuer; Depots mit inländischem Steuerabzug nachrichtlich.
* PDFs sind **Aufstellungen/Belege mit Übertragungshilfe**, keine Nachbildung amtlicher Vordrucke: Die
  Erklärung wird in ELSTER bzw. den amtlichen Formularen ausgefüllt, die Aufstellungen werden als Beleg
  eingereicht. Formularzeilen werden nur ausgegeben, wenn sie in den Parametern hinterlegt sind.
* Neutrales Regelwerk als Vorlage für weitere Länder.

**Grenzen:** Fremdwährungsgewinne (§ 23), Fonds-Altanteile vor 2018, gewerbliche Einkünfte,
Derivate und Günstigerprüfung sind nicht abgebildet; Vorabpauschale mit Börsenschlusskursen statt
Rücknahmepreisen; Basiszins 2026 ist noch nicht hinterlegt (erst für den Bericht 2027 nötig).

## M5 – Härtung, Backups, Unraid, Dokumentation, CI

**Entscheidungen**

* Tägliche Sicherung über die SQLite-Online-Backup-API mit Integritätsprüfung, gzip, Aufbewahrung
  einstellbar; manuell per Oberfläche oder CLI; wöchentliche DB-Pflege (`PRAGMA optimize`, WAL-Checkpoint,
  VACUUM bei viel freiem Platz).
* Anfragegröße begrenzt (1 MB), PDF-Downloads `no-store`, Pfadschutz für Berichte und Sicherungen.
* Demo-Kursgenerator speichersparend (kompakte Arrays, LRU) – vorher Hauptverbraucher bei großen Importen.
* Kein vorkompilierter Bytecode im Image; Python cached ihn unter `/data/cache/pyc` (−45 MB Image,
  schnellerer Warmstart, da auch die Standardbibliothek gecacht wird).
* Tageshistorie für Krypto aus den 23:30-Schlusskursen statt täglichem Historienabruf (CoinGecko-Kontingent).
* Unraid-Template mit getrenntem Read-only-Importpfad und maskierten Schlüsseln; GitHub Actions für
  Lint/Tests und Image nach GHCR.

**Abnahme (synthetisch, 5.725 Transaktionen / 170 Assets)**

| Kriterium | Ziel | Gemessen |
|---|---|---|
| Import inkl. Validierung und Abgleich | < 30 s | 0,4 s, 361/361 Bestände übereinstimmend |
| Dashboard | < 1,5 s | Test: 0,05 s; Container: 1,17 s beim ersten Aufruf nach Neuberechnung der Historie, danach 0,01 s |
| Sunburst-Klick → Detail-Panel | < 500 ms | Test: 0,07 s; Container: 0,28 s |
| Quellenausfall | als veraltet markiert, keine falschen Werte | letzter Wert + Warnung, Backoff |
| CoinGecko | < 8.000 Aufrufe/Monat | 1 Sammelaufruf je 10 min ≈ 4.400/Monat + einmalig 1 Historienabruf je Coin; laufende Tageshistorie aus den 23:30-Schlusskursen; Drosselung ab 80 % |
| Datenschutz | keine Stückzahlen/Werte/Konten in externen Anfragen | per Test geprüft |
| Leerlauf-RAM | < 250 MB | Beispiel-Import 98 MB; Großimport 177 MB (Spitze 413 MB während der erstmaligen Historienberechnung) |
| Image | < 350 MB | 282 MB (CI-Build ohne `strip`, siehe unten) |

---

**Grenzen M5:** Beim erstmaligen Laden der Historie eines großen Portfolios steigt der Speicher kurzzeitig
(gemessen 413 MB bei 170 Assets × 7,7 Jahren) – ein Container-Speicherlimit sollte daher nicht unter
512 MB liegen. Das ursprünglich geplante `strip` der nativen Bibliotheken (−35 MB) wurde nach dem ersten
CI-Smoke-Test entfernt: binutils 2.40 beschädigt die per patchelf angepassten Wheel-Bibliotheken (numpy/
OpenBLAS ließ sich nicht mehr laden).

## M6 – Sparpläne (Erweiterung)

**Entscheidungen**

* Schätzungen sind ein **Overlay** in der App-Datenbank (`plan`, `tx_estimate`, Migration 3); der Import
  bleibt unverändert und maßgeblich. Ledger, Bewertung, Historie und Performance rechnen mit Import +
  Overlay, die Sparplan-Erkennung selbst nur mit dem Import (keine Selbstverstärkung durch Schätzungen).
* Erkennung rückwärts ab der letzten Ausführung (längste lückenlose Folge, Anker unter den letzten vier
  Käufen), damit Einmalkäufe und Ratenänderungen den laufenden Plan nicht verdecken. Mindestens drei
  Ausführungen; Stichtag für „läuft/ausgesetzt/beendet“ ist der Importstand (`holdings_check`-Datum bzw.
  letzte Buchung), nicht das heutige Datum.
* Eine Schätzung entsteht erst, wenn die übliche Ausführungszeit erreicht ist; Stückzahl aus Sparrate und
  Tages-Schlusskurs (vorläufig: aktueller Kurs), abgerundet auf die im Import beobachtete Genauigkeit.
  Plausibilitätsprüfung gegen den letzten Import-Kauf (Faktor 2) mit Hinweis „Kurs prüfen“.
* Abgleich mit Import und manuell erfassten Buchungen über Konto, Asset, Datum (wöchentlich ±3, 14-täglich ±6,
  sonst ±7 Tage – stets weniger als der halbe Terminabstand) und Betrag oder Stückzahl ±20 %;
  jede Import-Buchung ersetzt höchstens eine Schätzung (keine Doppelzählung).
* Ungeprüfte Schätzungen, deren Termin der neue Import abdeckt, ohne sie zu enthalten, werden entfernt;
  freigegebene bleiben (Nutzerentscheidung) und werden als „fehlt im Import“ gemeldet.
* Eingaben akzeptieren deutsche und englische Zahlenformate; „5.000“ gilt als 5000 (Tausenderpunkt),
  „5.5“ als 5,5.

**Grenzen M6:** Nur Käufe gegen Fiat bzw. per Lastschrift (keine Krypto-gegen-Krypto-Sparpläne, keine
Entnahmepläne, keine dynamischen Raten). Ein nach dem Importstand ausgesetzter Plan erzeugt Schätzungen, bis
diese verworfen oder vom nächsten Import entfernt werden. Handelstage/Feiertage der Handelsplätze werden nicht
modelliert (nur Wochenend-Verschiebung auf Montag); die tatsächliche Ausführung kann daher ±1–3 Tage abweichen –
der Abgleich toleriert je nach Rhythmus ±3 bis ±7 Tage.

## M7 – Buchungen in Portfolia erfassen (Erweiterung)

**Entscheidungen**

* Manuell erfasste Buchungen und Assets liegen im **Journal** der App-Datenbank (`journal_tx`,
  `journal_asset`, `journal_log`, Migration 4); der Import bleibt unverändert. `recorded_portfolio()` =
  Import + Journal + freigegebene Sparplan-Ausführungen, `portfolio()` zusätzlich die Schätzungen. Die App
  funktioniert damit auch ohne Import-Datei.
* Jede Journal-Buchung trägt `source` und optional `external_id` (eindeutig je Quelle) – Grundlage des
  idempotenten CSV-Imports (M8).
* Formular-Vorlagen erzeugen Zeilen im Format von `transactions.csv`; die Endprüfung übernimmt derselbe
  Validator wie beim Import. Zusätzliche Hinweise: negativer Bestand, mögliche Dubletten zu Import-Buchungen.
* Gleiche `tx_id` im Import → Import gilt (Rundreise über den Gesamtexport ohne Doppelzählung). Ähnliche
  Buchungen mit anderer ID werden nicht automatisch entfernt, nur gemeldet (Nutzerentscheidung).
* Löschen ist ein Statuswechsel mit Protokoll (umkehrbar), keine physische Löschung.
* Sparpläne: manuell erfasste Ausführungen zählen für Erkennung und Abgleich. Ohne Import-Stichtag gelten
  ungeprüfte Schätzungen für den Plan-Status als Ausführung (sonst würde jede spätere manuelle Buchung laufende
  Pläne „aussetzen“); „fehlt im Import“ gibt es nur mit Import-Stichtag.
* Abgleichfenster der Sparplan-Schätzungen abhängig vom Rhythmus (wöchentlich ±3 statt ±7 Tage): vorher konnte
  die Ausführung der Vorwoche eine Wochen-Schätzung fälschlich ersetzen (Regressionstest).

**Grenzen M7:** Import-Buchungen sind nicht in der App änderbar (nur als Vorlage kopierbar) – Korrekturen im
kuratierten Import oder nach Umstieg über den Gesamtexport. Keine eigene Kontenverwaltung (Broker, Depotgruppe,
Steuerabzug neuer Konten über *Steuern → Zuordnung* bzw. *Einstellungen*). Der Bestandsabgleich
(`holdings_check`) bezieht sich weiterhin nur auf den Import. Einzelnutzerbetrieb ohne Konfliktbehandlung.

## M8 – CSV-Import aus Börsen und Wallets, datierte ZIP-Sicherungen (Erweiterung)

**Entscheidungen**

* **Nur Dateien, keine Online-Anbindung** (Entscheidung des Auftraggebers; ab M11 um die Grundlage für
  Datenquellen erweitert): keine Börsen-API-Schlüssel, keine Blockchain-Abfragen. Profile für Binance (Kontoauszug), Bitpanda, Kraken (Ledgers), Coinbase, Crypto.com App,
  Ledger Live, Trezor Suite/Trezor Wallet, Electrum, Exodus, Koinly (Export, Bulk-Edit, Universal-Vorlage),
  Blockpit, CoinTracking und das eigene Format (`transactions.csv`); alle anderen Quellen über eine gespeicherte
  Spaltenzuordnung, die künftig automatisch erkannt wird. Formatwissen aus öffentlich dokumentierten Exporten;
  kein fremder Code übernommen.
* Zweistufig: **Vorschau** (je Zeile Status, Werte mit Herkunft, Hinweise) → **Übernahme** ins Journal
  (`PF-C-…`, Quelle `csv:<profil>`). Die Originaldatei bleibt (gzip) in der Datenbank; ein Import ist als Ganzes
  **rückgängig** zu machen und danach erneut übernehmbar. Übernahme inkrementell (offene Zeilen später).
* **Idempotenz:** Kennung je Zeile (Börsen-ID, sonst Prüfsumme mit Laufnummer für identische Zeilen);
  eindeutig je Quelle für nicht zurückgenommene Buchungen (Teilindex, Migration 5 baut `journal_tx` um).
* **Transfer-Abgleich:** Abgang + Zugang desselben Kryptowerts auf verschiedenen Konten (Datei oder frühere
  Importe; Hash-Gleichheit oder Zeitfenster −2 h…+72 h und Menge 50–100,1 %) → eine Transfer-Buchung (`PF-T-…`),
  die Einzelbuchungen bleiben als `merged` erhalten (Auflösen/Rückgängig stellen sie wieder her). Nur „hohe“
  Sicherheit (Hash oder ≥ 98 % in 24 h) wird automatisch übernommen. Grund: unverbundene Ab-/Zugänge würden
  Lots veräußern bzw. neu anlegen und damit Einstand und Haltefrist verfälschen.
* **Dubletten:** gleiche Quelle über die Kennung; andere Quellen/Import über gleichen Zeitpunkt
  (± ganze Stunden Zeitzonenversatz, ± 10 min) und gleiche Mengen (± 0,5 %); gleiches Konto → standardmäßig
  auslassen. Mit kuratiertem Import gilt standardmäßig dessen `valuation_date` als Stichtag.
* **EUR-Werte** ausschließlich aus gespeicherten Kursen (keine Abfrage beim Bewerten): Fiat-Seite/EZB,
  Dateiwert, Stablecoin-Anker, Tageskurs, Transaktionskurs (± 31 Tage, auch aus derselben Datei). „Kurse laden“
  lädt Historie für die Assets der Datei (Hintergrundjob).
* **ZIP-Sicherungen:** nach Änderungen (Rückruf `AppContext.change_listeners`, Scheduler-Debounce 120 s) ein
  datierter Gesamtexport im Import-Format nach `EXPORT_DIR`, nur bei geändertem Inhalt (Prüfsumme über Buchungen,
  Assets, Konten, manuelle Kurse); Archiv der importierten ZIPs mit Datum (je Inhalt einmal). Der Export enthält
  jetzt auch steuerliche Einstufungen (`tax_type`, `tax_withholding`) und alle verwendeten Konten.
* Upload-Härtung: 25 MB nur für den CSV-Upload (sonst 1 MB), Grenze auch ohne `Content-Length`, CSRF-Token im
  Multipart-Formular, Zeilenlimit 300.000, ZIP/PDF/Binärdateien werden abgelehnt.
* Messwert (Testumgebung): Binance-Kontoauszug mit 17.500 Zeilen → 10.000 Buchungen: Auswertung ≈ 1,2 s,
  Übernahme ≈ 4,8 s, Rückgängig ≈ 0,2 s.

**Grenzen M8:** Futures, Margin, Optionen und NFTs werden nicht übernommen (Umbuchungen dorthin gelten als
intern). Exportformate der Anbieter können sich ändern – unbekannte Vorgänge werden gemeldet, nicht geraten.
Kraken „transfer“ ohne Untertyp und Bitpanda „transfer“ (Eingang) werden als Airdrop bzw. Reward vorgeschlagen
und sind zu prüfen. Aufteilung mehrerer Kleinstbeträge auf einen Zugang (Binance) erfolgt paarweise nach
Zeilenfolge, sonst zu gleichen Teilen (markiert). Transfers mit Buchungen des kuratierten Imports werden nur
gemeldet (der Import bleibt unverändert). Vorschau-Zeilen und Originaldateien vergrößern die Datenbank
(ca. 1–2 KB je Zeile).

## M9 – Ersatzkurse, Ausbuchen, Historie ohne Scheinverluste (Erweiterung)

Anlass (Rückmeldung mit einem echten, kuratierten Import): Krypto-Wert deutlich höher als in der
Vergleichssoftware, verkaufte Aktien als „unbewertet“ gemeldet, TTWROR gesamt −100 %.

**Befunde**

* Ein zweistelliger Prozentsatz des Krypto-Werts stammte aus `manual_prices.csv`: implizite Transaktionskurse
  der Kuration („veraltet, nur Fallback“) für Token ohne Kursquelle, teils mehrere Jahre alt. Manuelle Kurse
  galten unbegrenzt und nie als veraltet.
* Die Historie setzte Assets ohne Kursquelle (u. a. längst verkaufte Aktien ohne Symbol) über die gesamte
  Haltedauer mit 0 € an: Kauf mit dem gesamten Guthaben → Depotwert 0 → Tagesrendite −100 % → TTWROR dauerhaft
  −100 %.

**Entscheidungen**

* Eine gemeinsame Ersatzkurs-Regel (`app/prices/fallback.py`) für aktuelle Bewertung, Historie und Bewertung
  von Zahlungsströmen ohne EUR-Betrag: Kurspunkte aus manuellen Kursen und Transaktionskursen (tagesweise
  mengengewichtet, ab 1 € Buchungswert, split-bereinigt); zwischen zwei Punkten fortgeschrieben, nach dem letzten
  höchstens 30 Tage (Krypto) bzw. 365 Tage (Wertpapiere), einstellbar. Damit ist der letzte Tag der Historie
  identisch mit der Live-Bewertung (keine Sprünge am Stichtag).
* Ablauf = Wertberichtigung auf 0 € (Verlust fließt am Ablauftag in die Rendite ein). Ein pauschaler Schutz
  „Depotwert 0 → Tagesrendite neutral“ wurde verworfen: er würde echte Totalverluste (z. B. eine Ausbuchung als
  einzige Position einer Sicht) als 0 % ausweisen.
* Zahlungsströme ohne EUR-Betrag (Token-Zugänge ohne Wert) werden mit demselben Ersatzkurs bewertet wie die
  Position – sonst entstünde ein Scheingewinn (Wert > 0, Zufluss 0 €).
* Hinweise „unbewertet“ auf Übersicht und Performance nur für heute gehaltene Positionen (Live-Bewertung),
  frühere unter *Datenqualität → Historie* (Ersatzkurs-Tage je Asset, zeitweise ohne Kurs).
* **Ausbuchen** (`/journal/writeoff`): gesamter Bestand je Konto als `withdrawal` mit `lost|stolen|burn`,
  Wert 0 €, 23:59 Uhr, nicht vor der letzten Buchung; eine Transaktion je Sammel-Ausbuchung, Rücknahme gesammelt
  oder einzeln. Grundlage sind die erfassten Buchungen ohne Sparplan-Schätzungen.

**Messwerte (echter Import, ca. 6.000 Buchungen, ca. 180 Assets):** Der Wert der Token ohne Kursquelle sank auf
den Anteil mit höchstens 30 Tage alten Kursen; die Krypto-Summe liegt danach innerhalb von ca. 2 % der
Vergleichssoftware (Rest: Kurszeitpunkte/-quellen). Historie ohne Tage mit Depotwert 0; Benchmark
(`scripts/bench.py`) unverändert.

**Grenzen M9:** Transaktionskurse bilden keinen Marktverlauf ab (Stufen an Handelstagen); für längere
Haltedauern ist eine Kursquelle besser. Wertpapiere ohne Kursquelle gelten nach 365 Tagen ohne Buchung als
unbewertet – bei nicht börsennotierten Werten den manuellen Kurs regelmäßig erneuern oder die Frist erhöhen.
Steuerliche Anerkennung von Krypto-Totalverlusten ist Einzelfallfrage; Portfolia folgt der Einstellung im
Steuermodul.

## M10 – Kursquellen-Suche (CoinGecko), mobiles Layout (Erweiterung)

**Entscheidungen**

* Kryptowerte ohne Kursquelle werden im CoinGecko-Katalog gesucht (`app/prices/sources.py`): Katalog `/coins/list`
  inkl. Plattformen (1 Aufruf/Woche, gzip-Cache), lokale Suche nach Symbol – an CoinGecko gehen nur Kandidaten-IDs
  (`/coins/markets`, 1 Aufruf je 250 IDs). Chain-Hinweise aus Kontonamen, Plausibilität über Allzeittief/-hoch in EUR
  gegen die eigenen Transaktionskurse (Faktor 3 Spielraum), Marktführer ab 10× Marktkapitalisierung.
* Automatisch übernommen wird standardmäßig nur „hoch“ (ein einziger passender Coin, Chain und Kurse passen). Grund: Eine
  falsche Zuordnung (anderer Token mit gleichem Symbol) würde den Wert verfälschen – genau das Problem aus M9.
* Zuordnungen in `asset_source` (Migration 6) gelten über dem Import (angewendet in `recorded_portfolio`), damit auch
  Kursabruf, Historie und Gesamtexport; Rücknahme jederzeit. Spam-Token (`status=spam`) werden nicht gesucht.
* Mobil: `overflow-x: clip` auf `html`/`body` statt `hidden` am `body` (sonst vergrößert der mobile Browser den
  Layout-Viewport und die fixierte Leiste rutscht aus dem Bild; `hidden` am `body` bräche zudem die klebende
  Kopfzeile); Formularfelder und Kartenzellen dürfen nicht breiter als der Bildschirm werden. Kopfzeile mobil mit Logo
  und Namen. Die Chart-Legende reserviert ihre tatsächliche Zeilenzahl.

**Grenzen M10:** Aus der Entwicklungsumgebung war CoinGecko nicht erreichbar; die Zuordnung ist mit nachgebauten
Katalogdaten getestet. Chains ohne CoinGecko-Plattform (z. B. KRC-20 auf Kaspa) sind nicht prüfbar → Vorschlag statt
Automatik. Token, die CoinGecko nicht führt, bleiben ohne Quelle (Ersatzkurs bzw. Ausbuchen).

## M11 – Datenquellen: Grundlage für Börsen- und Wallet-Anbindungen (Erweiterung)

Anlass (28.09.2026): Börsen und öffentliche Wallet-Adressen sollen unter *Einstellungen → Datenquellen*
konfigurierbar sein; Anbindungen folgen einzeln. Umfang bewusst nur die Grundlage – **keine** Börsen- oder
Blockchain-Anbindung.

**Entscheidungen**

* **Kein neuer Dienst, keine neue Datenbank:** Tabellen in `app.sqlite` (Migration 7: `data_source`,
  `data_source_run`; Herkunftsspalten `event_key`, `event_line`, `tx_hash`, `datasource_id` in `journal_tx`;
  `kind`, `source`, `datasource_id` in `csv_batch`; `event_key`, `event_line` in `csv_row`). Nur neue Spalten
  (NULL bzw. `kind='csv'`) – bestehende Zeilen bleiben unverändert. Zeitplan über den vorhandenen APScheduler
  (Job `datasources_sync`, alle 5 Minuten fällige Quellen).
* **Ein Weg für alle Buchungen:** Connectoren (`app/datasources/connector.py`) liefern normalisierte Vorgänge im
  Zwischenformat des CSV-Imports (`Rec`); `CsvImportService.ingest` legt einen Prüf-Stapel (`kind='sync'`) an.
  Danach gelten Symbolzuordnung, Bewertung, Validator, Dubletten, Stichtag, Transfer-Abgleich, Übernahme und
  Rückgängig unverändert – ein Connector schreibt nie selbst ins Journal.
* **Kennungen:** Ereignis-ID `<anbieter>:<native ID>` im Format der CSV-Profile (Kraken `refid`/Ledger-ID,
  Coinbase-ID, Bitpanda-ID); Zeilen eines Ereignisses in fester Reihenfolge → `external_id = <ereignis>#<n>`,
  eindeutig je Quelle `sync:<anbieter>` (vorhandener Teilindex aus Migration 5). Nicht global eindeutige IDs
  erhalten laut Vertrag den Kontobezug, Wallet-IDs die eigene Adresse. CSV-Buchungen bekommen die Ereignis-ID aus
  ihrer Kennung (Profile mit nativer ID) bzw. den Transaktions-Hash (Ledger Live, Trezor, Electrum, Exodus).
* **Abgleich über Quellen hinweg:** exakter Treffer über Ereignis-ID oder Transaktions-Hash nur bei gleicher Art
  und Buchungsseite (Abgangs-/Zugangs-Asset; derselbe Hash ist Abgang beim Sender und Zugang beim Empfänger) →
  „mögliche Dublette“ mit Verweis, auf gleichem Konto standardmäßig nicht übernommen; sonst die bestehende
  unscharfe Prüfung. In beide Richtungen (CSV → Datenquelle, Datenquelle → CSV).
* **Idempotenz:** bekannte Kennungen (auch gelöschte Buchungen) werden nie erneut angelegt; ein Abruf ohne Neues
  hinterlässt keinen Stapel; Bearbeiten einer Buchung ändert sie an Ort und Stelle (Kennung bleibt).
* **Status:** angelegt → verbunden (Prüfung) → synchronisiert | teilweise | Fehler. „Teilweise“ nur bei
  unvollständigem Abruf; unvollständige Zeilen (unbekanntes Asset, fehlender Wert) sind Teil der Prüfung – sonst
  bliebe der Status nach der Korrektur veraltet. „Deaktiviert“ ist unabhängig und stoppt nur den Zeitplan.
* **Zugangsdaten** nur als Name einer Umgebungsvariable `PORTFOLIA_DS_*` (bzw. `*_FILE` für Docker-Secrets);
  `Secret` maskiert `repr`/`str`; Fehlermeldungen werden bereinigt (bekannte Werte und Teile davon,
  Schlüssel=Wert-Paare, URL-Parameter, globaler Redactor). Formulare spielen abgelehnte Schlüssel/Seeds und
  falsch eingetragene API-Schlüssel nicht zurück; Adressen werden formal geprüft (Prüfsummen erst im Connector).
* **Ohne Connector** bleibt eine Quelle „angelegt“, zeigt „Manuell / noch nicht unterstützt“ mit Verweis auf den
  CSV-Import und bietet weder Synchronisieren noch Prüfen an; der Zeitplan überspringt sie.
* **Konservative Automatik:** „automatisch übernehmen“ ist standardmäßig aus; eingeschaltet nur für Abrufe ohne
  Dublette, ohne Zeile vor dem Stichtag und ohne unvollständige Zeile – sonst geht der ganze Abruf zur Prüfung.
  Ein offener Prüf-Stapel pausiert weitere Abrufe der Quelle (keine gestapelten Stapel, keine konkurrierenden
  Entscheidungen). Der Abrufstand rückt nach jedem erfolgreichen Abruf vor; nach „Verwerfen“ holt „Abrufstand
  zurücksetzen“ alles erneut.
* **Entfernen** löscht Konfiguration, Laufhistorie und offene Vorschau-Stapel; übernommene Buchungen bleiben mit
  Quelle und Ereignis-ID und werden von einer neu angelegten Quelle wiedererkannt. Ändern von Anbieter, Adresse
  oder Konto setzt Status und Abrufstand zurück.
* Nebenbei behoben: Fiat-Erträge und Fiat-Gebühren als eigene Zeile (z. B. Gebühr in EUR) erhielten im CSV-Weg
  keinen EUR-Wert und galten als unvollständig; Bech32-Adressen in Großbuchstaben (QR-Codes) werden akzeptiert.

**Tests:** `tests/test_datasources.py` (46 Fälle) – Migration 6 → 7 mit Bestandsdaten, Constraints und Kaskade;
Anlegen/Ansehen/Bearbeiten/Deaktivieren/Entfernen über die Oberfläche inkl. Validierung, CSRF, doppelter Adresse
und Ablehnung privater Schlüssel ohne Echo; ehrliche Anzeige ohne Connector; mit Test-Connector: Idempotenz
(wiederholter und überlappender Abruf, neue Vorgänge), mehrzeilige Ereignisse, Herkunft in `journal_tx`,
Vertragsverletzungen, Abgleich CSV → Datenquelle und Datenquelle → CSV, automatische Übernahme, offener
Prüf-Stapel und Zeitplan, Fehlertexte ohne Geheimnisse (Anbieterfehler, HTTP 429, Zeitüberschreitung,
unerwartete Ausnahme), teilweise Synchronisierung, Entfernen/Wiedererkennen, Abrufstand zurücksetzen,
Sperre gegen parallele Läufe, Wartezeit nach Drosselung, Fehler in der Import-Pipeline.

**Grenzen M11:** Noch keine Anbindung (bewusst). Exakter Abgleich nur für Profile mit nativer ID (Kraken,
Coinbase, Bitpanda) und Wallet-Exporte mit Hash; Binance-Kontoauszug, Crypto.com, Koinly u. a. liefern keine
passenden IDs → unscharfe Prüfung. Der kuratierte Import wird nur über Stichtag und unscharfe Prüfung abgeglichen
(`source_ref` wird nicht ausgewertet). Ein Kontowechsel verschiebt bereits übernommene Buchungen nicht.

**Offene Entscheidungen** von M11 (Zugangsdaten, Automatik, Doppelzählung mit dem Import, Verwerfen) sind in M12
entschieden; offen bleiben die Wallet-Regeln (unten, Frage 4).

## M12 – Bitpanda-Anbindung, verschlüsselte API-Keys, Abgleich Import ↔ App (Erweiterung)

Anlass (29.09.2026): erster produktiver Connector (Bitpanda, nur lesend); die Einrichtung einschließlich API-Key
soll vollständig in der App möglich sein. Binance- und Wallet-Anbindungen bleiben unverändert (keine).

**Entscheidungen**

* **API:** Bitpanda Public API `https://api.public.bitpanda.com/v1`, nur `GET` mit Header `x-api-key`:
  `/operations` (Vorgänge, Cursor-Pagination, Zeitfilter `from`), `/assets?id=` (Stammdaten, Rückfall
  `/assets/{id}`), `/currencies`, `/portfolio/holdings` (nur Plausibilität). Kein stiller Rückgriff auf die ältere
  API `api.bitpanda.com`, Umleitungen werden nicht verfolgt, keine schreibenden Aufrufe. Leserechte: „Transaction“
  erforderlich, „Balance“ optional; „Trade (Read)“ wird nicht verlangt – „Verbindung testen“ prüft Vorgänge,
  Bestände und Stammdaten einzeln; ohne lesbare Stammdaten werden betroffene Vorgänge „ungeklärt“.
* **Quellenlage:** Die gehostete Entwicklerdokumentation war aus der Build-Umgebung nicht erreichbar (Egress-Sperre);
  Grundlage sind die von Bitpanda auf GitHub veröffentlichte API-Beschreibung und öffentliche Beispiele. Daher
  toleranter Parser (snake/camelCase, Cursor über `cursor`/`next_cursor`/`has_next_page`, Rückfall ohne
  `pageSize` bzw. `from` bei HTTP 400) und eine konservative Vollständigkeitsregel: volle Seite ohne Cursor,
  wiederholter Cursor, mehr als 2000 Seiten, abgebrochene Folgeseite oder unlesbarer Eintrag → „teilweise“, der
  Abrufstand bleibt stehen.
* **Verschlüsselung** (`app/datasources/vault.py`): AES-256-GCM aus `cryptography` 50, Datenschlüssel per
  HKDF-SHA256, Associated Data `portfolia:ds:<id>:api_key` (Chiffrate nicht zwischen Datensätzen vertauschbar),
  Format `PFC1 ‖ Key-ID ‖ Nonce ‖ Chiffrat`. Master-Key aus `PORTFOLIA_MASTER_KEY_FILE` bzw.
  `PORTFOLIA_MASTER_KEY`, nie in der Datenbank, nie automatisch erzeugt; Rotation über `…_OLD(_FILE)` und „Neu
  verschlüsseln“ (Oberfläche oder `python -m app credentials rotate`). Ohne Master-Key: Eingabe gesperrt, nichts
  gespeichert, kein Abruf mit gespeichertem Schlüssel. `PORTFOLIA_DS_*` funktioniert unverändert (ein gespeicherter
  Schlüssel hat Vorrang). Der Entrypoint stellt root-eigene Key-Dateien (Unraid-USB-Stick, FAT nur root-lesbar) dem
  App-Benutzer nur im RAM (`/dev/shm`) bereit. Entfernen mit `PRAGMA secure_delete` und WAL-Checkpoint.
* **Geheimnisschutz:** Der Browser erhält den Schlüssel nie zurück (nur die letzten 4 Zeichen; Formular-Echo ohne
  `api_key`; maskiertes Eingabefeld ohne Autovervollständigung). Der Log-Redactor maskiert während eines Laufs den
  Klartext-Schlüssel (temporär registriert) sowie `x-api-key`-/`Authorization`-Muster; Anbieterfehler werden vor
  Speichern und Anzeige bereinigt. Alle neuen Endpunkte sind POST mit CSRF-Schutz hinter der Anmeldung. Keine
  Validierung über Dritte: der Verbindungstest geht nur an die Bitpanda-API.
* **Abbildung** nur eindeutiger Fälle: Kauf/Verkauf Fiat ↔ Krypto (auch Sparplan), Ein- und Auszahlung,
  Reward/Staking-Reward (Vorgangsart exakt), eigene Gebühren-Teile. Alles andere wird die neue Zeilenart
  **„ungeklärt“** (`review`) mit Grund – nie verworfen, nie geraten: Korrekturen/Stornos (`compensates`, auch der
  stornierte Vorgang), Tausch Krypto → Krypto, Fiat → Fiat, Aktien/ETFs, Edelmetalle, Indizes, unbekannte Assets
  und Vorgangsarten, fehlende Richtung oder Zeit. Gebühren an Haupt-Teilen werden übernommen, aber als
  prüfbedürftig markiert (Brutto/Netto nicht dokumentiert) und deshalb nie automatisch übernommen. Interne
  Umbuchungen werden gezählt, nicht gebucht. Bestände nur als Hinweis (vollständiger Abgleich, Leserecht Balance).
* **Identität:** Ereignis `bitpanda:<Vorgangs-UUID>` (ohne ID: Hash der Rohdaten), Zeilen `#1…n` in fester
  Reihenfolge; Aliase aus Transaktions- und Trade-UUIDs (`journal_event_alias`), damit Bitpanda-CSV (`T<uuid>`)
  und API einander exakt erkennen. Gleiche Anbieter-ID → **„bereits vorhanden“** (revidiert M11: vorher „mögliche
  Dublette“) – geht nicht erneut in Bewertung und Lots ein, beide Herkünfte bleiben; ein Transaktions-Hash führt
  weiterhin nur zu „mögliche Dublette“.
* **Abrufstand:** Der Connector liefert einen neuen Stand nur bei vollständigem Abruf (`from` = Laufbeginn − 2 Tage);
  gespeichert wird er erst nach der Aufnahme in den Prüf-Stapel. Verwerfen setzt ihn per `rewind()` vor den ältesten
  offenen Vorgang zurück (revidiert M11, Frage 9). „Dauerhaft ignorieren“ wird je Ereignis in `event_decision`
  gespeichert. Wartende Vorgänge werden nicht erneut aufgenommen, neue an einen unberührten Stapel angehängt –
  offene Prüfungen blockieren den Zeitplan nicht mehr (revidiert M11).
* **Automatische Übernahme je Ereignis** (revidiert M11, Frage 6): nur vollständig neue Ereignisse, deren Zeilen
  alle gültig sind und keinen Hinweis tragen; die übrigen bleiben zur Prüfung (`commit(only_idx=…)`), ohne die
  sicheren aufzuhalten. Standard weiterhin aus.
* **Import ↔ App** (`app/journal/reconcile.py`, Seite *Buchungen → Abgleich mit dem Import*): exakte Abdeckung über
  `source`/`source_ref` des kuratierten Imports – dynamisch, die App-Buchung zählt wieder, sobald ein Import sie
  nicht mehr enthält; unscharfe Kandidaten (Art, Assets, Menge ± 1 %, ± 2 Tage, bis zum Stand des Imports) nur mit
  Entscheidung (`journal_import_link`). Behebt die in M11 dokumentierte Doppelzählung (Frage 7).
* **Migration 8** (nur additiv): Tabellen `data_source_secret`, `event_decision`, `journal_event_alias`,
  `journal_import_link`, `ds_asset_cache`; Spalten `key_expires_on`, `last_check_json`, `coverage_json`
  (`data_source`) sowie `rows_unclear`, `rows_ignored`, `detail_json` (`data_source_run`).
* **Unraid:** Pfad „Master-Key (Ordner, nur lesen)“ `/boot/config/portfolia` → `/run/secrets/portfolia`, Variablen
  `PORTFOLIA_MASTER_KEY_FILE` (vorbelegt) und `PORTFOLIA_MASTER_KEY_OLD_FILE` (nur Rotation). Ordner statt Datei
  eingebunden, damit Docker bei fehlender Datei kein Verzeichnis `master.key` anlegt.

**Tests:** `tests/test_bitpanda.py` (27 Fälle, anonymisierte Fixtures unter `tests/data/bitpanda/`) –
Verschlüsselung (Roundtrip, Bindung an den Datensatz, falscher/fehlender Key, Formate, Dateirechte), kein Speichern
ohne Master-Key, Schlüssel nie im HTML, Redirect oder DB-Klartext, CSRF, Basic-Auth, Ersetzen/Entfernen ohne
Buchungsverlust, Rotation, Umgebungsvariable weiter nutzbar, Entfernen und Neuanlegen ohne Doppelimport,
Pagination und Abbildung (mehrzeilige Vorgänge, Gebühren, Rewards, Korrektur/Storno, interne Umbuchung, Tausch,
Stocks, unbekannte Vorgangsart), Dezimalgenauigkeit, inkrementeller Abruf mit Überlappung, 429 (Warten, Abbruch,
lange `Retry-After`), unklare Pagination und Cursor-Schleifen, 400-Rückfall, Fehlerklassen (401, 403, abgelaufen,
5xx, Zeitüberschreitung, Umleitung), Verbindungsprüfung je Endpunkt, historisch und inkrementell, Verwerfen und
erneutes Abrufen, Ignorieren über Läufe, automatische Übernahme je Ereignis, Teilfehler mit stehendem Abrufstand,
abgelaufenes Datum blockiert Aufrufe, Zeitplan trotz offener Prüfung, CSV „bereits vorhanden“ bzw. Kandidat in
beide Richtungen, Abdeckung durch den kuratierten Import (exakt, Entscheidung, aufheben), Migration 7 → 8 mit
Bestandsdaten. `tests/test_datasources.py` (46 Fälle) an die revidierten Regeln angepasst.

**Grenzen M12:** keine Live-Verifikation (kein Testschlüssel bereitgestellt) – Feldnamen, Form der Pagination,
Fehlercodes bei fehlendem Recht und Gebührenkonvention sind unbestätigt; Tausch Krypto → Krypto, Stocks/ETFs,
Metalle, Indizes und Korrekturen nur als „ungeklärt“; Bestandsprüfung nur beim vollständigen Abgleich und nur als
Hinweis; die exakte Import-Abdeckung setzt `source_ref` mit Bitpanda-ID im kuratierten Import voraus (Koinly-Exporte
tragen sie nicht → Entscheidung auf der Abgleichseite); das Fenster der unscharfen Prüfung (± 2 Tage, ± 1 %) kann
bei Sparplänen mit gleichen Beträgen mehrere Kandidaten zeigen.

## M13 – Abrufrate der Kursquellen einstellbar (Erweiterung)

Anlass (29.09.2026): Die Aktualisierungsrate soll einstellbar sein, mit Empfehlung, damit das Kontingent reicht.

**Entscheidungen**

* Auswahl aus festen Stufen statt freier Eingabe (Krypto 2 min–4 h, gedrosselt 10 min–12 h, Wertpapiere 5 min–2 h);
  ungültige Werte fallen auf die nächstliegende Stufe bzw. den Standard. Gedrosselt nie häufiger als normal.
* Neue Intervalle gelten sofort (`Scheduler.reschedule_prices`), `crypto_due` nutzt dieselben Stufen.
* Hochrechnung je Stufe für das eigene Portfolio (`app/prices/budget.py`): Kurse = Aufrufe je Aktualisierung ×
  Aktualisierungen je Monat; Historie = nicht mehr gehaltene Coins und CoinGecko-Benchmarks × ≈ 15/Monat (gehaltene
  Coins erhalten den Tagesschluss aus dem Kurs, siehe `write_eod_closes`); Reserve 5 % des Limits.
* Empfehlung: kürzeste Stufe ab 5 min unter der Drosselschwelle; gedrosselt die kürzeste Stufe (≥ doppeltes
  Normalintervall, ≥ 30 min), mit der das Rest-Kontingent einen Monat reicht – reicht schon die Historie allein
  nicht, 60 min (längere Intervalle brächten kaum etwas). Yahoo: feste Empfehlung 15 min.
* Hinweis, wenn die „veraltet“-Grenze kürzer ist als das doppelte (gedrosselte) Intervall; Hinweis ohne
  CoinGecko-Schlüssel. Datenqualität zeigt die tatsächliche Drosselschwelle statt fest „80 %“.

**Tests:** `tests/test_price_budget.py` (8 Fälle) – Stufen, Hochrechnung und Empfehlung (Demo, Pro, viele Coins,
viel Historie), Drosselbetrieb, Yahoo, Kennzahlen aus dem Ledger (gehalten/verkauft), `crypto_due` mit Stufen,
Einstellungsseite und Speichern inkl. Validierung, Umplanung ohne Neustart.

**Nachtrag 0.11.2 – Krypto „veraltet“:** Die Warnung erschien regelmäßig, weil das Alter des Kurses aus
CoinGeckos `last_updated_at` (letzte Kursänderung) berechnet wurde – bei wenig gehandelten Coins oft über 60 min,
obwohl gerade abgerufen. Jetzt zählt das Alter des letzten erfolgreichen Abrufs (`quote_latest.fetched_at`, Grenze
`prices.stale_crypto_minutes`); eine lange unveränderte Kursangabe wird erst ab `prices.stale_crypto_market_hours`
(Standard 24 h) gewarnt. Jede Warnung nennt den Grund (Kontingent, Pause nach Fehlern mit Fehlertext, keine Daten
für die ID, Abruf ausstehend); die Übersicht fasst gleiche Gründe zusammen. Tests: `tests/test_price_freshness.py`.

**Grenzen M13:** Hochrechnung, keine Messung: Neustarts, manuelle Aktualisierungen und neue Coins stecken pauschal in
der Reserve; Yahoo-Werte sind Obergrenzen ohne Feiertagskalender; der Zähler kennt nur Portfolias eigene Aufrufe.

## M14 – Alle Buchungen bearbeitbar, vollständiger Export und Neueinrichtung (Erweiterung)

Anlass (30.09.2026): Alle Buchungen sollen bearbeitet und gelöscht werden können; der Datenexport soll alles
enthalten, um Portfolia mit demselben Stand neu einzurichten – inklusive aller lokalen Änderungen und der Kursquellen.

**Entscheidungen**

* **Import-Buchungen als Überlagerung** (`app/journal/overrides.py`, Tabelle `tx_override`): Bearbeiten
  (Expertenmodus, gleiche Prüfung wie beim Import; Herkunft `source`/`source_ref`/`flag`/`orig_*` bleibt) und Löschen
  (umkehrbar, eine bearbeitete Fassung bleibt dabei erhalten). Die Import-Datei wird nie verändert. Schlüssel ist die
  `tx_id`, die Änderung gilt über spätere Importe hinweg; geänderte Import-Fassung → Hinweis „Import geändert“, fehlende
  Buchung → „ohne Wirkung“, nicht mehr anwendbare Fassung → Import-Fassung gilt, Hinweis „nicht anwendbar“ – nie still.
  Unveränderte Speicherung legt keine Überlagerung an (Sekunden im Zeitstempel bleiben erhalten).
* Wirksamer Import (`ctx.effective_base()`) ist Grundlage für Journal-Überlagerung, Abgleich, Export und alle
  Berechnungen; eine in der App gelöschte Import-Buchung lässt eine gleichnamige Journal-Buchung nicht aufleben
  (IDs des Imports bleiben „belegt“).
* Sparplan-Ausführungen (freigegeben/geschätzt) aus der Buchungsliste heraus bearbeitbar bzw. verwerfbar (vorhandene
  Sparplan-Funktionen).
* **Vollständiger Export** (`app/fullexport.py`): Datenvertrag unverändert (wirksame Buchungen, Assets mit
  Kursquellen, Konten, Bestände, manuelle Kurse) plus Ordner `portfolia/` mit `state.json` (Einstellungen,
  Kursquellen-Status, Sparplan-Wahl/verworfene Ausführungen, Datenquellen ohne Zugangsdaten, Entscheidungen,
  Anbieter-IDs, CSV-Zuordnungen, Kennungen gelöschter CSV-/Sync-Buchungen), `usage.json`, Kurshistorie
  (`price_daily.csv`, `series_meta.csv`) und `files/` (`sources.yaml`, `tax_rules/`). Prüfsummen im Manifest unter
  `extra_files`; ältere Portfolia-Versionen und andere Werkzeuge ignorieren den Ordner. Nie enthalten: API-Keys,
  Master-Key, Passwörter, Protokolle.
* **Neueinrichtung:** Export-ZIP in den Importordner. Neue Installation (keine Buchungen, Einstellungen, Datenquellen,
  Importe) → automatische Übernahme; sonst Rückfrage in Übersicht und Einstellungen. Übernahme idempotent und
  nicht-destruktiv (Einstellungen überschreiben, Rest ergänzen, gleichnamige Datenquellen überspringen, Dateien mit
  `.bak-…` sichern). Beschädigte Zusatzdaten (Prüfsumme) werden ignoriert, der Import selbst bleibt gültig.
* **Wiedererkennung nach der Neueinrichtung:** Import-Buchungen mit Anbieter-ID (auch aus App-Buchungen, Quelle
  `portfolia:csv:…`/`portfolia:sync:…`) und übernommene Aliase gelten bei CSV-Import und Datenquelle als „bereits
  vorhanden“; bei Kennungen ohne Anbieter-ID (Prüfsummen) nur innerhalb derselben Quelle.
* Automatische ZIP-Sicherung auch nach Änderungen an Einstellungen; die Prüfsumme für „unverändert“ umfasst den
  App-Zustand (ohne Kurshistorie und API-Verbrauch).
* **Migration 9** (nur additiv): `tx_override`, `import_extra`.

**Tests:** `tests/test_import_edit.py` (7 Fälle: Bearbeiten ohne Änderung, Bearbeiten mit Wirkung auf Bestand und
Export, ungültige Eingabe, Löschen/Wiederherstellen mit erhaltener Fassung, neuer Import mit geänderter bzw. fehlender
Buchung, gleichnamige Journal-Buchung, Migration 9) und `tests/test_full_export.py` (5 Fälle: Inhalt ohne Geheimnisse,
automatische Neueinrichtung mit identischen Beständen, Rückfrage auf bestehender Installation inkl. Idempotenz,
Verwerfen und manipulierte Prüfsumme, Wiedererkennung derselben CSV-Kennung).

**Grenzen M14:** Nach der Neueinrichtung sind frühere App-Buchungen Import-Buchungen (Herkunft bleibt in `source`,
bearbeitbar als Überlagerung). Nicht übertragen werden CSV-Stapel mit Originaldateien, Laufhistorien, News und
erzeugte Steuer-PDFs (regenerierbar) – für eine byte-genaue Kopie bleibt die SQLite-Sicherung. API-Keys sind nach
der Neueinrichtung neu einzugeben.

## M15 – Wallets read-only: Bitcoin, Ethereum, BNB Chain, Avalanche C-Chain, Solana, Kaspa

Anlass (01.10.2026): Wallets direkt in Portfolia einrichten und synchronisieren – nur öffentliche Adressen bzw.
Kontoschlüssel, nie Seed-Phrase, privater Schlüssel, Signatur oder Gerätezugriff; getrennte, wartbare Adapter mit
gemeinsamem Ablauf; jede Chain getestet und mit dokumentierter Abdeckung.

**Anbieter-Recherche (vor der Umsetzung)**

* Etherscan API V2: kostenloser Key nur noch für Ethereum (u. a.); BNB Chain (56) und Avalanche (43114) nur mit
  bezahltem Plan; seit 07/2026 höchstens 1.000 Einträge je Anfrage im kostenlosen Plan. BscScan-API abgekündigt.
* Routescan (Etherscan-kompatibel, Snowtrace): ohne Key 2/s und 10.000/Tag; Standard für Avalanche, Alternative für
  Ethereum/BNB (Abdeckung per „Verbindung testen“). NodeReal/BSCTrace verworfen: Abfragen auf 1.000 Blöcke je
  Fenster begrenzt – für Historien ungeeignet; Moralis nicht verifizierbar (Doku nicht erreichbar).
* Bitcoin: Esplora-API (mempool.space, Blockstream), 25 Transaktionen je Seite, `chain_stats` je Adresse.
* Solana: öffentlicher RPC (100/10 s, nicht für Dauerbetrieb) bzw. Helius mit Key; `getSignaturesForAddress` erfasst
  eingehende Token-Transfers nur über das Token-Konto → Token-Konten werden mit abgefragt.
* Kaspa: kaspa-rest-server (Quellcode geprüft: `after` aufsteigend, Grenzzeitpunkte vollständig, `light`-Auflösung
  der Eingänge); KRC-20: Kasplex-Indexer go-krc20d (Quellcode geprüft: Op-Liste 50 je Seite, `next` exklusiv,
  `prev` aufsteigend, `address` allein genügt).

**Entscheidungen**

* **Gemeinsames Framework** (`app/datasources/wallet.py`, `chainhttp.py`): geprüfter Anbieter-Katalog mit festen
  HTTPS-Endpunkten (kein SSRF, keine Weiterleitungen, Host-Prüfung je Anfrage), `Decimal`-JSON, Mindestabstand je
  Anbieter, Backoff mit `Retry-After`, Anfrage-/Warte-/Zeitbudget, begrenzte Parallelität; einheitliche Einordnung
  (Zugang/Abgang/Gebühr/Tausch/ungeklärt mit Begründung), Tokens über Chain + Contract/Mint/Tick.
* **Connector-Vertrag erweitert:** stabile Unterkennung je Bewegung (`Rec.ext_id`), sicherer Fortsetzungspunkt bei
  Etappen (`resume`), Fortsetzung bald (`more`), erkannte Lücken (`gaps` → nie „vollständig“), beobachtete
  Bestände (`balances`).
* **Ablauf:** Hintergrundlauf mit Fortschritt (HTMX-Abfrage), Erstabruf in Etappen, die sich selbst fortsetzen
  (auch ohne Intervall und nach Fehlern mit Pause); Zeitplan überspringt laufende Quellen.
* **Adapter:** EVM (eine Klasse, Chain-ID 1/56/43114; Listen im Gleichschritt, Seiten nie mitten im Block, nur
  bestätigte Blöcke), Bitcoin (Adressen + xpub/ypub/zpub, BIP32 nur öffentlich, BIP44/49/84/86, Gap-Limit,
  UTXO-Bilanz), Solana (Wallet + Token-Konten inkl. geschlossener, Vor-/Nach-Bestände, Miete als Eigentum,
  blockweise Zeiger), Kaspa (Blockzeit-Seiten mit Überlappung, Commit/Reveal-Erkennung; KRC-20 mit eigenem Cursor,
  Ausfall = Lücke bei vollständigem KAS).
* **Abgleich:** Transfer-Vorschläge mit Begründung und Hash bestehender Buchungen; Paare mit bereits übernommenen
  Buchungen nie automatisch (Lots nicht still verändern); Zu-/Abgänge, die ein erfasster Transfer schon abdeckt, als
  Dublette; gleiche Blockchain-Transaktion aus Wallet-CSV als Dublette.
* **Oberfläche:** Wallets nach frei benannten Gruppen, Chain-Auswahl in zwei Schritten, Anbieter-Schlüssel
  (verschlüsselt, nie exportiert), „On-Chain beobachtet“ vs. „durch Portfolia-Buchungen erklärt“, Lücken und
  Abdeckungsgrenzen am Konto, Token-Zuordnung über Contract mit Explorer-Link (Spam: Vorschlag „ignorieren“).
* **Migration 10** (nur additiv): `data_source.wallet_group/watch_json/progress_json`, `ds_balance`,
  `provider_secret`.

**Tests:** `tests/test_wallets_evm.py` (22), `tests/test_wallets_bitcoin.py` (6), `tests/test_wallets_solana.py` (6),
`tests/test_wallets_kaspa.py` (5) mit nachgebildeten Anbieter-APIs (`tests/wallet_fakes.py`): mehrere Token-Logs je
Hash, gleiche Adresse auf drei Chains, gleiches Symbol mit anderem Contract, Wechselgeld und mehrere Adressen,
Token-Konten (auch geschlossene), Kaspa-Historie und KRC-20, Gebühren, Paginierung, Drosselung, abgebrochener
Erstabruf, wiederholte Läufe, Bestandsabweichungen, Dubletten gegen CSV und Bitpanda, Migration 10; Codecs gegen
veröffentlichte Testvektoren (Keccak, EIP-55, RIPEMD-160, BIP173/350, BIP44/49/84/86, Kaspa-Referenzadresse).

**Grenzen M15:** Nicht live verifiziert (kein Netzzugang beim Bau). BNB Chain braucht einen bezahlten
Etherscan-Plan oder einen Anbieter, der Chain 56 liefert. NFTs, Native Staking (Solana), Lightning/Multisig, KRC-721
nicht erfasst; Swaps/DeFi nur zur Prüfung, keine automatische Bewertung von Positionen in Verträgen.

### M15.1 – Automatische Vorschläge beim Zuordnen, mobiles Formular (0.13.1)

Anlass (01.10.2026, Rückmeldung zum ersten MetaMask-Abgleich): CoinGecko-ID-Feld ragte mobil aus der Karte; Symbole
und Tokens sollen automatisch gesucht und mit der vorhandenen Datenbasis abgeglichen werden.

* **Vorschläge** (`app/csvimport/suggest.py`) je unbekanntem Symbol mit Aktion, Feldern, Begründung und Sicherheit:
  Datenbasis (Assets, aktive/offene Kursquellen, frühere Zuordnungen auch mit anderem Symbol, Token-Namen der
  Wallet-Anbindung) und lokaler CoinGecko-Katalog, neu mit **Contract-Index je Plattform** (EVM ohne
  Groß-/Kleinschreibung, Solana-Mint, KRC-20-Tick). Tokens nie über das Symbol allein; nicht gelistete, nur
  erhaltene Tokens → „ignorieren“ (mittel). Bei Zuordnung zu einem Asset ohne Kursquelle wird die CoinGecko-ID
  per Häkchen mit übernommen (`asset_source`, Herkunft Nutzer, Grund „Contract laut CoinGecko-Katalog“).
* **Kein zusätzlicher Datenabfluss:** keine Abfrage je Contract (`/coins/{platform}/contract/…` hätte den Bestand
  offengelegt); nur der allgemeine Katalog, höchstens alle 7 Tage, Abruf höchstens alle 30 Minuten angestoßen
  (Hintergrundjob `coingecko_catalog`, Seite fragt den Stand ab).
* **Kursquellen-Suche** nutzt Contracts aus Token-Zuordnungen: eindeutiger Coin → „hoch“ ohne Marktdaten-Abruf.
* **Mobil:** Eingaben im Kartenlayout volle Breite (keine festen Breiten mehr), Vorschlag und Felder je Zeile über
  die volle Kartenbreite. Prüfung neu je Element gegen die eigene Karte (bisher nur seitenweites Querscrollen –
  deshalb blieb der Fehler unentdeckt); gegen das alte Template reproduziert (360/390 px), neu ohne Überlauf bei
  360/390/412/1280 px, hell und dunkel.

**Tests:** `tests/test_asset_suggest.py` (10): Regeln je Fall, Fake-Token mit gleichem Symbol, zweite Chain desselben
Coins, Konflikt mit anderer CoinGecko-ID, offener Kursquellen-Vorschlag, gespeicherte Contracts, Solana-Mint aus
großgeschriebener Kennung, Börsen-Symbole (mehrdeutig, bekannt, eindeutig, mehrere Coins, ohne Katalog), freie IDs;
Prüf-Stapel einer ETH-Wallet Ende-zu-Ende (Formular wie im Browser abgeschickt, Kursquelle mit und ohne Häkchen),
Katalog im Hintergrund (Warteschlange, Fehler, veraltet, kein Abruf je Seitenaufruf), Kursquellen-Suche über Contract.

**Grenzen M15.1:** Live nicht geprüft (CoinGecko aus der Build-Umgebung gesperrt; Katalogformat laut Doku:
Plattform → Contract, EVM klein geschrieben). Plattform-Bezeichnung für KRC-20 bei CoinGecko nicht belegt – erkannt
werden Plattformen mit „kaspa“/„krc“ im Namen. Katalog im Speicher rund 15–25 MB (~18.000 Coins, Laden ~0,1 s).

## M16 – Abgleich mit dem kuratierten Import: vorhanden oder neu, ohne Handarbeit (0.14.0)

Anlass (01.10.2026, erster ETH-Abgleich: 87 Vorgänge, 32 „unvollständig“, 14 Symbole zuzuordnen): Bei vorhandenen
kuratierten Daten soll Portfolia selbst feststellen, was existiert und was neu ist; Interaktion minimieren.

**Befund:** Die kuratierten Wallet-Buchungen (Koinly) tragen den Transaktions-Hash in der Notiz (z. B. 87 von 88 bei
„MetaMask (ETH)“), Gebühren als eigene Buchung (Abgang mit Tag `cost`, 39 Fälle), nie als Gebühr an der Buchung. Die
„unvollständigen“ Zeilen lagen überwiegend vor dem Stichtag – der Status „unvollständig“ wurde vor dem Stichtag
geprüft, deshalb verlangte Portfolia Zuordnungen für Vorgänge, die längst im Import stehen.

**Entscheidungen**

* **Abgleich über den Hash** (`app/csvimport/reconcile.py`): Index aus Notiz/Quellkennung des Imports (EVM, Bitcoin/
  Kaspa, Solana) und `journal_tx.tx_hash`; Vergleich der Beine je Hash (summiert je Seite und Asset, Toleranz 0,5 %,
  Gebühr im Abgang als Rückfall); bekannte Assets zuerst, jede Gegenbuchung nur einmal. Alle Hauptbeine →
  „bereits vorhanden“ (vor dem Stichtag-Test, also auch danach); Teilen fehlt etwas → Hinweis bzw. „mögliche
  Dublette“; vor dem Stichtag ohne Gegenstück → Hinweis „nicht im kuratierten Import“.
* **Lernen nur bei Eindeutigkeit:** Token-Kennung → Asset, wenn alle Treffer übereinstimmen (gespeichert,
  Migration 11: `csv_symbol.origin = 'abgleich'`, im Export enthalten, löschbar); Symbole ohne Contract nur je Stapel.
* **Konto:** Konten der Gegenbuchungen je Stapel; automatische Umstellung der Datenquelle nur einmal, bei ≥ 3 Treffern
  und ≥ 90 % unter einem Konto und wenn das bisherige Konto keine Buchungen hat (sonst Vorschlag mit einem Klick);
  offene Zeilen werden umgebucht (sie tragen das Konto seit dem Abruf), kein neuer Abruf; „Rückgängig“ verhindert
  ein erneutes automatisches Umstellen.
* **Stichtag vor „unvollständig“/„ungeklärt“:** Zeilen bis zum Stichtag brauchen weder Zuordnung noch Entscheidung;
  „Assets zuordnen“ zeigt nur Symbole aus zu übernehmenden Zeilen (ältere eingeklappt, optional), „Fehlende
  EUR-Werte“ ebenso. Übernahme einer alten Zeile nur, wenn sie vollständig ist.
* **Bestehende Stapel** werden beim Öffnen neu bewertet (Version der Auswertung im Stapel).

**Tests:** `tests/test_reconcile.py` (7): ETH-Wallet gegen nachgebildeten Koinly-Import (Hash in der Notiz,
Gebühren als `cost`, zwei Token-Logs gegen eine Buchung, Fake-Token in derselben Transaktion, Freigabe und
fehlgeschlagene Transaktion ohne Gegenstück), gelernte Token-Zuordnung, automatische Konto-Umstellung, Übernahme
setzt den kuratierten Bestand fort (USDC 1000 + 100 − 200), Rückgängig und Klick-Übernahme, keine Umstellung bei
belegtem Konto, Import ohne Hashes (Stichtag entscheidet, keine Zuordnungspflicht für alte Zeilen), abweichende
Menge nach dem Stichtag → mögliche Dublette; Regeln ohne App (Gebühr im Abgang, anderes Asset, mehrdeutige Tokens,
App-Buchung, mehrteiliger Vorgang, Hash-Erkennung im Text).

**Grenzen M16:** Abgleich nur, wo beide Seiten den Hash führen (Börsenvorgänge weiter über Anbieter-IDs bzw.
Ähnlichkeit). Teilt der Import eine Transaktion anders auf als die Wallet (z. B. Swap als zwei Buchungen mit
abweichenden Mengen), bleibt sie „teilweise“. Liegt dieselbe Wallet im Import auf mehreren Konten, entscheidet der
Nutzer (Verteilung wird angezeigt). Mit echten Daten nicht live geprüft (kein Netz beim Bau); Grundlage ist die
Struktur der vorliegenden kuratierten Daten.

## M17 – Diagnose: Datenqualität und Bestandsabgleich, Schutzregeln für künftige Importe (0.15.0)

Anlass (01.10.2026, Analyse eines Gesamtexports): doppelt gutgeschriebene Token (manuell + Transfer), Buchungspaare
mit gleichem Hash ohne Ereignisindex, ein Anbieter-Kürzel mit der Kursquelle eines anderen Coins, fehlende bzw. alte
Kurse, mögliche Token-Migration, rekonstruierte Buchungen. **Vorgabe:** bestehende Nutzerdaten nicht bearbeiten –
nichts löschen, zusammenführen, umklassifizieren, ausschließen oder neu berechnen; Auswirkungen nur als Szenario.

**Entscheidungen**

* **Diagnose rein lesend** (`app/diagnosis/`): `collect.py` liest einen Schnappschuss (nur `SELECT` und die
  vorhandenen Rechen-Caches), `engine.py` wertet ihn ohne Datenbankzugriff aus, `web.py` zeigt `/quality/diagnose`.
  Kein Cache, keine Tabelle: jeder Aufruf rechnet neu; gleiche Daten → gleiche Befunde, Kennung je Befund aus Art und
  betroffenen Buchungen. Befund = Wissen / Vermutung / Belege / Unsicherheiten / Szenario / nötige Entscheidung;
  Status belegt · wahrscheinlich · verdacht · hinweis. Szenarien rechnen Bestandswirkungen exakt aus den Beinen der
  betroffenen Buchungen und verfolgen Lots und Veräußerungen über die anschaffende Buchung (`DisposalPart.acq_tx`,
  rein informativ ergänzt) – ein zweiter Ledger-Lauf je Befund wäre bei einigen Tausend Buchungen zu langsam.
* **Dubletten:** gleicher Hash + identische Angaben (Ereignisindex aus `<ereignis>#<index>` der App-Buchungen bzw.
  Portfolia-Exporte; verschiedene Indizes = legitim); gleiche exakte Menge auf demselben Konto ≤ 36 h mit
  manueller Buchung; gleiche Anbieter-Kennung (auch Bitpanda-UUID in Koinly-Notizen) bzw. gleicher Hash und gleiche
  Menge in Import und App-Buchung. Mehrere Hash-Paare desselben Kontos und Assets bilden einen Befund mit
  gemeinsamem Szenario (Nettowirkung).
* **Bestandsabgleich:** „intern konsistent“ (= Soll des kuratierten Imports) ist sichtbar etwas anderes als „mit
  externer Quelle abgestimmt“; Letzteres nur bei Abruf ≤ 48 h ohne Lücke, sonst „extern nicht bestätigt“. Erklärungen
  nur aus Befunden, die eine Mengendifferenz erklären können.
* **Schutzregeln (nur künftige Vorgänge, Validierung unverändert):** Anbieter-Identität von Kürzeln
  (`app/csvimport/identity.py`, Bitpanda „TH“ = Threshold Network): Auflösung nur über `TH@BITPANDA` bzw. eine
  bestätigende Kursquelle, sonst offen; Vorschlag „eigenes Asset“; die automatische Kursquellen-Suche ordnet solche
  Kürzel nie über das Symbol zu. Kennungen des kuratierten Imports beim CSV-/Sync-Abgleich: Koinly-ID, Bitpanda-UUID
  in der Notiz. Gleiche exakte Menge auf demselben Konto ≤ 36 h → „mögliche Dublette“; ebenso Vorgänge auf Konto und
  Asset einer rekonstruierten Import-Buchung (± 7 Tage – echte Abrechnung statt Schätzung). Hash-Abgleich ohne Buchungen
  derselben Quelle (zweite Bewegung derselben Transaktion bleibt ein eigener Vorgang). Mögliche Transfers ohne
  Entscheidung werden nie automatisch übernommen. Auswertungsversion 3: offene Prüf-Stapel werden beim Öffnen neu
  bewertet; übernommene Buchungen bleiben unberührt.
* **Bewusst nicht rückwirkend:** Die Kennungs-Regeln gelten nicht in `journal.reconcile.coverage()` – sonst fielen
  bereits übernommene App-Buchungen still aus den Berechnungen. Bestehende Zuordnungen (Kursquellen, Symbole)
  werden nicht neu bewertet.

**Tests:** `tests/test_diagnosis.py` (19, synthetisch und anonymisiert): doppelte Gutschrift manuell + Transfer,
drei Hash-Paare ohne Index mit Netto-Szenario, zwei legitime Bewegungen mit verschiedenem Ereignisindex, Anbieter-ID
in Import und App, Anbieter-Kürzel mit Kursquelle eines anderen Coins, Migration 10^6 mit Spam-Status,
rekonstruierte Buchungen mit Ausgleichsbuchung (FIFO-Verbrauch je Jahr), alter manueller Kurs bzw. kein Kurs,
Transfer-Kandidaten (Hash bzw. Zeit/Menge), externer Bestand (abgestimmt, Differenz, veraltet, nur intern); Seite und
erneute Prüfung ändern nichts (Prüfsumme über alle Tabellen, Bestände und Lots), Diagnose deterministisch, keine
Formulare; Schutzregeln im Prüf-Stapel (gleiche Menge → Prüfung, Abrechnung nahe einer rekonstruierten Buchung →
Prüfung, zweite Bewegung derselben Transaktion bleibt neu,
Anbieter-Kürzel → Zuordnung nur für Bitpanda, Koinly-ID und Bitpanda-UUID aus dem Import → vorhanden, unklare
Transfers nie automatisch, Kursquellen-Suche).

**Grenzen M17:** Ohne Ereignisindex bleibt „gleicher Hash, gleiche Angaben“ ein Verdacht. Transfers und Migrationen
werden nur vermutet (Adressen bzw. Contracts fehlen meist). Anbieter-Identitäten nur für hinterlegte Kürzel.
Externe Bestände liegen nur für Wallets vor (Bitpanda meldet nur die eigene Bestandsprüfung). Die Diagnose fragt
keine Kurse ab; Szenario-Bewertungen sind keine Marktbewertung.

## M18 – Diagnose: Empfehlung je Befund, Korrektur per Knopfdruck mit Vorschau und Rückgängig (0.16.0)

Anlass (02.10.2026): Befunde nicht nur aufklappen, sondern je Befund eine Empfehlung erhalten und die Änderungen mit
einem Knopfdruck übernehmen – mit gut sichtbaren Folgen und der Möglichkeit, eine andere Lösung zu wählen. Die Vorgabe
aus M17 („nichts automatisch bereinigen“) gilt weiter: geändert wird nur auf ausdrückliche Entscheidung je Befund.

**Entscheidungen**

* **Empfehlung ≠ Aktion** (`app/diagnosis/recommend.py`): je Befundtyp Text, Prüfhinweise (Explorer-Links nur mit
  Hash/Contract, öffnet ausschließlich der Nutzer), empfohlene Lösung, Alternativen, „Eigene Auswahl: Buchungen
  ausblenden“ und „als geprüft markieren“. Bei *verdacht* ist die Empfehlung ausdrücklich „nach Prüfung“; Migration
  und Bestandsdifferenz haben keine empfohlene Buchung (Ausgleichsbuchung nur als Notlösung).
* **Befunddaten maschinenlesbar** (`Finding.data`): Buchungen, Paare, Asset, Coin, Konto – Grundlage der Lösungen.
* **Korrektur = vorhandene, umkehrbare Mechanismen** (`app/diagnosis/actions.py`): Import-Buchung ausblenden =
  `tx_override` „delete“, App-Buchung = Status `deleted` bzw. `merged`, „im Import enthalten“ =
  `journal_import_link` „covered“, Kursquelle = `asset_source` (neu: Herkunft `override` ersetzt auch eine Kursquelle
  des Imports), Zuordnung entfernen = `csv_symbol`, neue Buchungen = App-Buchungen der Quelle `diagnose` (`PF-D-…`,
  im Journal nicht bearbeitbar, Rückgängig nur über die Diagnose). Nichts wird physisch gelöscht.
* **Vorschau rechnet, schreibt nicht:** hypothetisches Portfolio (Buchungen entfernt/ergänzt, Kursquellen ersetzt),
  zweiter Ledger-Lauf, Diagnose auf dem hypothetischen Schnappschuss (erledigte, neue, geänderte Befunde;
  Bestandsabgleich vorher/nachher), Steuer-Regelwerk mit dessen Ledger-Optionen je Jahr mit geänderten Veräußerungen,
  Erträgen oder Jahresend-Lots (Zusammenfassung des Steuerberichts vorher/nachher). Laufzeit bei rund 6 000 Buchungen
  ca. 1–2 s.
* **Übernehmen nur bei unveränderter Vorschau:** Prüfsumme über Befund, Lösung, Eingaben, alle Änderungen samt
  Ausgangszustand und Datenstand (Buchungen, Überlagerungen, Abgleich, aktive Kursquellen, Zuordnungen, aktiver
  Import). Plan wird beim Übernehmen neu berechnet und verglichen; Ausführung in einer Transaktion mit erneuter
  Zustandsprüfung je Objekt; Modul-Sperre gegen parallele Korrekturen.
* **Rückgängig als Ganzes** (`diag_decision`, Migration 12): Vorher-Zustand je Änderung gespeichert; bereits selbst
  Wiederhergestelltes wird übersprungen, später anderweitig Geändertes (z. B. neue Kursquelle) verweigert das
  Zurücksetzen – nichts wird überschrieben.
* **„Als geprüft markieren“** speichert nur eine Prüfsumme der Befunddaten (ohne Abrufzeit); ändern sich die Daten,
  erscheint der Befund wieder. Wandert mit dem Gesamtexport (`state.json` → `diag_dismissed`).
* **Bestandsabgleich „Import-Soll + Änderungen in Portfolia“:** Ergibt der unveränderte Import das Soll und erklärt
  sich die Differenz vollständig aus App-Änderungen (ausgeblendet, geändert, ergänzt, Sparplan), entsteht kein Befund
  „intern abweichend“ – sonst würde jede übernommene Dubletten-Korrektur einen neuen Befund erzeugen.

**Tests:** `tests/test_diagnosis_actions.py` (19, synthetisch): Empfehlung und Alternativen, Vorschau ohne
Schreibzugriff mit Beständen, Status, Jahreswerten und Steuer, Übernehmen + exaktes Rückgängig (Prüfsumme über alle
Nutzdaten), veraltete bzw. manipulierte Vorschau, späteres eigenes Wiederherstellen, Hash-Paare mit Teilauswahl,
„im Import enthalten“, Transfer mit Steuerwirkung und Anschaffungsdatum, Kursquelle über dem Import und Konflikt beim
Rückgängig, Vorschlag übernehmen (Zeile exakt wiederhergestellt), Ausgleichsbuchung mit Eingabeprüfung, Migration,
Contract-Zuordnung, eigene Auswahl, „geprüft“ mit Prüfsumme, Web-Ablauf mit CSRF, Gesamtexport. Lokal zusätzlich auf
einer Wegwerf-Kopie eines echten Exports geprüft (keine Ausnahme; alle empfohlenen Korrekturen übernommen und exakt
zurückgenommen; Kopie gelöscht, keine Daten im Repository).

**Grenzen M18:** Die Vorschau fragt keine Kurse ab (neue Kursquelle → Wert erst nach dem Abruf). Steuerwerte der
Vorschau sind vorläufig (Zusammenfassung, aktuelle Optionen). Teilbuchungen von Gruppen, abgeglichene Transfers und
Sparplan-Buchungen ändert die Diagnose nicht. Explorer-Prüfung bleibt Sache des Nutzers.

### M18.1 – Bitpanda: aktuelles Antwortformat der Public API (0.16.1)

Anlass (02.10.2026): Alle Vorgänge eines echten Abrufs standen als „ungeklärt: Zeitpunkt fehlt“ im Prüf-Stapel,
der Lauf endete mit „Pagination wiederholt denselben Cursor“. Ursachen (aus den Rohdaten des Stapels, die gehostete
Dokumentation war aus der Build-Umgebung nicht erreichbar): Beträge und Gebühren kommen als Objekt
`{"value", "currency_id"|"asset_id"}`, der Zeitpunkt steht nicht unter den bisher gelesenen Namen am Vorgang, und
die API trägt auch auf der letzten Seite einen Cursor (die leere Folgeseite wiederholt ihn).

**Entscheidungen**

* Beträge/Gebühren aus Objekt oder Text; Gebühr in eigener Währung (`fee_ref`) mit eigenem Symbol.
* Zeitpunkt in fester Rangfolge am Vorgang, sonst an den Teilen (frühester), sonst jedes Feld mit Zeitnamen
  (ohne Änderungs-/Ablaufzeiten); auch Unix-Zeit und Zeitobjekte. Verwendetes Feld in der Abdeckung
  (`time_fields`) und je Zeile; Originalantwort des Vorgangs in den Rohdaten. Ohne Zeitpunkt: weiterhin „ungeklärt“,
  aber mit den gelieferten Feldnamen.
* Pagination: leere Seite = Ende; Cursor-Echo → Kennung des letzten Vorgangs als Fortsetzungspunkt (laut Doku
  bezeichnet der Cursor ein Element); Seite nur mit Bekanntem → „teilweise“.
* Abbildung ergänzt: Sparplan-Einzahlung (Fiat-Zugang), `passive_earn_reward`/`onetime_reward` u. a. als Ertrag,
  Swap als Verkauf + Kauf über die Euro-Teile, `stake`/`unstake` ohne Buchung, `merger_crypto` als prüfbedürftige
  Token-Umstellung.
* Abrufstand Version 2: ältere Stände und das Verwerfen eines Stapels lösen einen vollständigen Neuabruf aus;
  bei vollständiger Historie werden unbearbeitete offene Prüf-Stapel der Quelle durch die neue Auswertung ersetzt
  (`FetchResult.refresh_open`), bearbeitete bleiben mit Hinweis stehen.

**Tests:** aktuelles Antwortformat (9 Vorgangsarten, drei Zeitfeld-Varianten, Cursor auf der letzten Seite),
Cursor-Echo und Schleifenschutz, Ersetzen eines unbearbeiteten Stapels bzw. Stehenlassen eines bearbeiteten.
Lokal an den rekonstruierten Vorgängen des echten Stapels geprüft (mit ergänztem Zeitpunkt alle abgebildet bzw.
bewusst ohne Buchung, keiner „ungeklärt“; Daten nicht im Repository).

**Grenze:** Der tatsächliche Name des Zeitfelds ist nicht belegt (Doku nicht erreichbar); die tolerante Erkennung
deckt die üblichen Varianten ab, ein Restfall erscheint mit Feldnamen statt still.

### M18.2 – Bitpanda: Vertrag laut offizieller Referenz, Reparaturweg, Bestandsabgleich (0.16.2)

Anlass (02.10.2026): Prüfung von 0.16.1 durch den Auftraggeber – weiterhin `pageSize` statt `page_size`, `_next()`
bewertete Cursor-Felder vor `has_next_page=false`, ein wiederholter Cursor wurde durch die letzte Vorgangskennung
ersetzt (nicht belegt), die Bestandsprüfung nutzte `/portfolio/holdings`, Zeitpunkte wurden über geratene Feldnamen
gesucht, die Tests bildeten den Vertrag falsch ab, und die Erfolgsmeldung stützte sich auf Rohdaten mit künstlich
ergänztem Zeitpunkt. Die Referenz (docs.public.bitpanda.com, OpenAPI je Endpunkt) war diesmal abrufbar.

**Belegt (Repository, CI, Registry, Rohdaten):** Der Standard-Branch des Repositorys ist der Entwicklungs-Branch;
die CI veröffentlicht nur für ihn bzw. `v*`-Tags. `latest` und `sha-ad35bd9` hatten am 02.10.2026 denselben Digest
(0.16.1, veröffentlicht 07:59 UTC). Der gemeldete Prüf-Stapel (08:43 Ortszeit = 06:43 UTC) stammt von einer Version
vor 0.16.0: Rohdaten ohne Zeitfeld-Angabe, Betragsobjekte als Text abgelegt, alle Vorgänge „Zeitpunkt fehlt“. Ob
der Container danach aktualisiert wurde, ist aus dem Repository nicht belegbar – deshalb ab 0.16.2 der Commit im
Image und in der App.

**Entscheidungen**

* Nur dokumentierte Parameter und Felder: `page_size` (100, bei HTTP 400 der Standard 25), `cursor`, `from` im
  Format der Referenz; `operation_id`, `operation_type`, `transactions[]`; `flow`, `credited_at`, `asset_amount`,
  `fee_amount`, `asset_balance_after`, `trade` (inkl. `trade_id` als Alias), `compensates`, `wallet_id`.
* Pagination: Seite vollständig verarbeiten, dann `has_next_page`; `false` beendet trotz `next_cursor`, `true`
  folgt `next_cursor` unverändert; kein Ersatz-Cursor. Fehlende/widersprüchliche Angaben, wiederholter Cursor, nur
  Bekanntes, drei leere Seiten → „teilweise“ mit Grund; eine leere Seite mit `true` wird verfolgt, nie als Ende
  gewertet. Ende und Ursache in der Abdeckung (`pagination`).
* Zeitpunkt ausschließlich `transactions[].credited_at`; ohne ihn `Rec.ts_missing` (Anzeige „Zeitpunkt fehlt“,
  Status ungeklärt vor jeder Stichtagsprüfung, nie gebucht) – der Abrufzeitpunkt dient nur der Sortierung.
* Gebühren nur mit Beleg: Saldoverlauf (`asset_balance_after`) für `fee_amount`, Kurse (`rate`/`rate_with_fee`) und
  Saldo für `trade.fee`; sonst prüfbedürftig.
* `/v1/portfolio` (`balance.value`) bei jedem Lauf → Bestände der Datenquelle (Vergleich mit Portfolia-Buchungen);
  nach vollständigem Abruf Abgleich mit der Summe der Vorgänge für alle Asset-IDs beider Seiten mit erklärender
  Lesart; ohne auswertbare Position „nicht geprüft“. Keine Ausgleichsbuchungen.
* Diagnose je Lauf (nur Namen und Zähler): Parser-Version, Zeitquellen, fehlende Pflichtfelder, nicht dokumentierte
  Felder, Saldoverlauf stimmig/Brüche; je Zeile „Herkunft“ im Prüf-Stapel.
* Reparaturweg: `Rec.raw.parser` (3); je neu geliefertem Ereignis werden unbearbeitete Zeilen älterer bzw.
  abweichender Auswertung in offenen Stapeln derselben Datenquelle ersetzt – auch bei unvollständigem Abruf;
  bearbeitete bleiben (Zähler, „Veraltete Zeilen neu auswerten“ setzt Eingaben ausdrücklich zurück und ruft neu ab);
  „Vollständig neu abrufen“ an der Datenquelle. Ersetzt `FetchResult.refresh_open`. Abrufstand Version 3 → ein
  vollständiger Neuabruf nach dem Update.
* Wartende Ereignisse je Datenquelle statt je Anbieter; ein bereits übernommenes Ereignis bleibt bei versionierter
  Auswertung bekannt, auch wenn eine neue Auswertung es anders auf Zeilen verteilt (keine Doppelbuchung).
* Erfolgszeitpunkt rückt nur bei vollständigem Abruf (bzw. einer Etappe des Erstabrufs) vor.
* Build-Kennung: `PORTFOLIA_REVISION` (CI), sichtbar in Seitenleiste, Einstellungen → System, `/healthz`; Smoke-Test
  prüft sie, ein CI-Schritt nennt die veröffentlichten Tags.

**Tests:** synthetische Fixtures im Aufbau der Referenz; Test-Server lehnt `pageSize`, unbekannte Endpunkte
(`/portfolio/holdings`, `/assets/{id}`), nie ausgegebene Cursor und ungültige Zeitangaben ab. Abgedeckt: Zeitpunkt
nur an den Teilen, nicht dokumentiertes Zeitfeld am Vorgang (nicht gelesen), nicht lesbares `credited_at`;
Betragsobjekte, `fee_amount` (zusätzlich/enthalten/ohne Beleg/anderes Asset), `trade.fee` (enthalten/zusätzlich
mit und ohne Saldobeleg/ohne Kurse); mehrere Seiten, letzte Seite mit `next_cursor` und `has_next_page=false`,
wiederholter Cursor, Rücksprung, sechs Arten fehlender bzw. widersprüchlicher Angaben, leere Zwischenseite,
Seitenlänge ohne Bedeutung, Rückfall auf `page_size=25`; `/portfolio` mit `balance.value`, ohne auswertbare
Position, leer, ohne Recht; Spur eines Sparplans vom HTTP-JSON bis zur angezeigten Zeile; alte unbearbeitete und
bearbeitete Prüf-Stapel, Ignorier-Entscheidung, übernommene Buchung mit anderer Zeilenaufteilung; vollständiger
Neuabruf und inkrementeller Abruf ohne Dubletten; zwei Datenquellen desselben Kontos.

**Grenzen:** Nicht gegen echte HTTP-Antworten geprüft (keine vorliegend). Lokal geprüft wurde der gemeldete,
unveränderte Prüf-Stapel (Erkennung als ältere Auswertung, Ersetzen ohne Dubletten); die Original-Antworten der API
enthält er nicht – ob `credited_at` geliefert wird, zeigt erst die Abdeckung des nächsten echten Laufs. Nicht
dokumentierte Semantik (Wertebereiche, Gebühren, Staking im Bestand, Höchstwert von `page_size`, Bezug von `from`)
bleibt Annahme und ist im Code bzw. README benannt.

### M18.3 – Dubletten: vorhandene Buchung gegenüberstellen (0.16.3)

Anlass (02.10.2026): Bei einer möglichen Dublette nannte der Prüf-Stapel nur die Kennung der vorhandenen Buchung
(„ähnelt PF-C-…“), die Buchungsliste nur ein Badge mit Tooltip – vergleichen ließ sich nichts.

**Entscheidungen**

* Prüf-Stapel: Unter jeder Zeile mit Verweis auf vorhandene Buchungen (mögliche Dublette, bereits vorhanden,
  gleicher Transaktions-Hash) steht „Vergleich mit der vorhandenen Buchung“ – neue Zeile und bis zu zwei vorhandene
  Buchungen nebeneinander: Zeitpunkt (mit Abstand), Art, Abgang, Zugang (je mit Konto), Gebühr, EUR-Wert, Herkunft,
  Kennung, Hash, Notiz; Abweichungen hervorgehoben, Zahl der Abweichungen im Titel. Bei *mögliche Dublette*
  aufgeklappt, sonst eingeklappt. Bekannte Zeilen tragen dafür jetzt ebenfalls den Verweis (`dup_of`).
* Buchungsliste: „Dublette?“ springt zur Gegenüberstellung unter der Zeile (App-Buchung ↔ ähnliche Import-Buchung).
* Suche der vorhandenen Buchung: erfasste Buchungen, Import, Journal in jedem Status (gelöscht, zusammengeführt –
  mit Status angezeigt). Nur Anzeige, keine Änderung; gemeinsames Partial `partials/compare.html`, Logik in
  `app/csvimport/compare.py`.

**Tests:** Abweichungen je Feld (Zeit unter einer Minute gleich, Wert auf den Cent, unbekanntes Konto kein
Unterschied, verschiedene Hashes), Zeitabstand als Text, Prüf-Stapel (Dublette aufgeklappt mit genau einer
hervorgehobenen Abweichung, bekannte Zeile eingeklappt mit „Angaben gleich“), Buchungsliste (Sprungmarke, beide
Buchungen).

## M19 – Importprüfung: Abgleich je Zeile, Stapelaktionen, Verknüpfen, Vollständigkeit (0.17.0)

Anlass (02.10.2026): Hunderte mögliche Dubletten mussten einzeln geprüft werden; beim Auslassen gingen die Angaben
der neuen Quelle verloren; Koinly fasst technische Buchungen zusammen; nach der Bitpanda-API-Umstellung war offen,
ob Vorgänge fehlen. Bestandsaufnahme, Plan und Datenmodell: `docs/RECONCILIATION.md`.

**Entscheidungen**

* Erweiterung der vorhandenen Pipeline statt Neubau: Die Erkennungsregeln halten fest, worauf ein Treffer beruht
  und welche Seite einer Buchung er betrifft; `app/csvimport/assess.py` bewertet daraus je Zeile Ergebnis
  (Dublette, Ergänzung, neu, Widerspruch, komplex), qualitative Sicherheit, Belege, Abweichungen (gering/relevant),
  Ergänzungen, Gebührenprüfung, Quellenvorrang und Korrekturvorschläge. Gespeichert in `csv_row.messages`.
* Erkennung geschärft: Gebühr netto/brutto anders dargestellt gilt als derselbe Abgang; verschiedene Tx-Hashes nie
  Dublette; Zeilen eines Ereignisses, dessen Hauptzeile vorhanden ist, nie still gebucht; gleiche Menge/Konto/Zeit
  mit anderem Asset → Widerspruch; Zeilen vor dem Stichtag werden mitgeprüft (mögliche Lücken), ohne Statuswechsel.
  Bitpanda liefert den Gebührenbeleg strukturiert (`Rec.fee_basis`: extra/inside/open; ältere Zeilen aus dem Text).
* Stapelaktionen (`app/csvimport/batch.py`): Auswahl (Gruppe, Seite, alle gefilterten, einzeln, Vorauswahl
  „sicher“), Filter, Vorschau mit Bestandswirkung und Ausschlussgründen, Ausführung genau einmal je Vorschau (Token,
  Fingerabdruck) in **einer** Transaktion (`commit` dafür in Planung und Anwendung geteilt), Protokoll mit
  Vorher-Zustand, Rückgängig nur für Unverändertes. Feste Regeln: Dubletten/Ergänzungen nie gebucht, „übernehmen“
  per Stapel nur für „neu“ ohne Prüfhinweis, Widersprüche/komplexe Fälle/vor Stichtag nur mit Bestätigung,
  Transfer-Paare nur gemeinsam.
* Verknüpfen statt verwerfen: Zeilenstatus `linked`; Werte und Abgleich als Quelldatensatz in `tx_link`,
  Wiedererkennung über `journal_event_alias` (`row:<quelle>|<id>`, bei 1:1-Ereignissen zusätzlich Anbieter-IDs);
  Anzeige „+n Quellen“ in der Buchungsliste, Teil des Gesamtexports. Fehlt das Ziel später, wird die Zeile wieder
  geöffnet.
* Quellenvorrang je Feld nur als begründeter Vorschlag; manuelle Korrekturen gehen vor; nie automatisches
  Überschreiben. Bearbeitungsreihenfolge „Zuerst prüfen“: neue Quellen und Vorgänge ohne Gegenstück zuerst.
* Vollständigkeitsbericht je Börsen-Datenquelle (`app/datasources/quality.py`): nachgewiesen / plausibel / nicht
  verifizierbar aus Belegen der Abrufe; vollständige Abrufe halten Zeitraum, Monate, Vorgänge ohne Zeitpunkt und
  übernommene, aber nicht mehr gelieferte Vorgänge fest.
* Migration 13 (additiv): `import_action`, `tx_link`. Bestehende Tabellen und Daten unverändert; ältere Versionen
  ignorieren die Tabellen. Keine automatische Bereinigung bestehender Daten.

**Tests:** zentraler Regressionsfall Koinly-Transfer ↔ Bitpanda-Auszahlung (Gebühr zusätzlich belegt / nicht
belegbar / im Betrag enthalten), Vorgänge vor dem Stichtag, Übersicht → Vorschau → Ausführen → Rückgängig, doppeltes
Absenden, veraltete Vorschau, Auswahl über Seiten und Filter, Wiedererkennung nach vollständigem Neuabruf, Anzeige
und Gesamtexport samt Neueinrichtung, manuelle Einzahlung ↔ API-Einzahlung, gleiche Beträge mit verschiedenen
Hashes, falsche Kurs- bzw. Asset-Zuordnung, Zugangsseite eines Transfers zwischen eigenen Wallets, Transfer-Paar nur
gemeinsam (inkl. Rückgängig), verschwundene Zielbuchung, Teil-Übernahme hält den Stapel offen,
Vollständigkeitsbericht (Bruch im Saldoverlauf, leerer Monat, Import-Buchung ohne API-Gegenstück, nicht mehr
gelieferter Vorgang).

**Grenzen:** Alle Tests synthetisch; mit echten Exporten bzw. API-Antworten nicht geprüft. Die Sicherheitsstufen
sind Regeln, keine kalibrierten Wahrscheinlichkeiten. Ob Koinly Gebühren zusätzlich zum gesendeten Betrag führt, ist
nicht belegt – geprüft wird, was die Buchung im Ledger bewirkt. Werte der neuen Quelle werden nicht feldweise in
die vorhandene Buchung übernommen (Korrekturvorschlag + „Bearbeiten“). Der Vollständigkeitsbericht braucht nach dem
Update einen vollständigen Neuabruf; Lücken vor dem ersten API-Vorgang sind grundsätzlich nicht prüfbar.

## M19.1 – Transferseite bei verzögerter Auszahlung und anderem Kontonamen (0.17.1)

Anlass (02.10.2026, Meldung des Auftraggebers): Ein Wallet-Zugang der Bitcoin-Datenquelle wurde als neue Buchung
übernommen, obwohl der kuratierte Import denselben Vorgang als Transfer „Börse → Wallet“ führt – die Börse hatte
mehr als einen Tag verzögert ausgezahlt. Diagnose: Die Verzögerung lag im bisherigen Fenster (72 h); die Erkennung
scheiterte am **Kontonamen** (Import: Wallet-Name des Steuertools, Datenquelle: eigener Name) – sie verlangte
dasselbe Konto.
Gebuchte App-Buchungen prüfte der Journal-Abgleich nur gegen Buchungen gleicher Art (Zugang ≠ Transfer).

**Entscheidungen**

* Ein Regelwerk für Prüf-Stapel, Buchungsliste, Abgleich mit dem Import und Datenqualität:
  `app/csvimport/transfer_side.py`. Gleiches Konto wie bisher (± 0,5 %); anderer Kontoname nur bei exakt gleicher
  Menge, ohne Fiat, nicht auf dem Absenderkonto und nur, wenn das Konto des Transfers nicht von einer anderen
  Datenquelle geführt wird (dieselbe Datenquelle zählt nicht: ihr Konto kann inzwischen umgestellt sein). Zugänge
  bis 72 h nach dem Transfer, bei exakter, unverwechselbarer Menge (≥ 6 signifikante Stellen) bis 7 Tage; Abgänge
  ± 2 h; verschiedene Hashes nie. Je Transferseite ein Treffer.
* Prüf-Stapel: Treffer → mögliche Dublette (`transfer_leg` bzw. `transfer_leg_acc`), nie automatisch übernommen;
  Bewertung „Widerspruch: Konto“ (Sicherheit mittel), spätere Gutschrift kein Zeitwiderspruch, EUR-Wert einer
  Transferseite bis 15 % gering (verschiedene Bewertungszeitpunkte), Zeitpunkt laut Notiz der Transfer-Buchung als
  Beleg; Korrekturvorschläge „verknüpfen“ und „Konten angleichen“. Auswertungsstand 6: offene Stapel werden beim
  Öffnen **und vor jeder automatischen Übernahme** neu bewertet.
* Gebuchte App-Buchungen: „Transferseite?“ in der Buchungsliste (Gegenüberstellung, *Import-Transfer gilt* / *Keine
  Dublette*), Kandidat unter *Abgleich mit dem Import*, Befund in der Datenqualität mit Vorschau und Rückgängig
  (vorhandene Lösung „im Import enthalten“; „Import-Buchung ausblenden“ entfällt hier, weil es auch die Auszahlung
  der Börse entfernte). Entscheidungen werden in `journal_import_link` gespeichert – keine Migration.
* Konto der Datenquelle: Transferseiten unter anderem Kontonamen (offen, verknüpft, gebucht, entschieden) gehen als
  Beleg in „Konto laut Abgleich“ ein – Umstellung per Klick ohne Neuabruf, zurücknehmbar; automatisch nur aus dem
  Hash-Abgleich wie bisher.

**Tests:** Regeln (Fenster, exakte/unverwechselbare/runde Menge, Konto, Hash, Datenquelle, Fiat, Absenderkonto, PF-T,
Abgangsseite mit Gebühr, Vorrang gleiches Konto, Notiz-Beleg), Wallet-Datenquelle mit automatischer Übernahme
(Regressionsfall strukturgleich, andere Zahlen), Verknüpfen, bereits gebuchter Zugang (Buchungsliste, Abgleich,
Datenqualität mit Vorschau, Übernehmen, Rückgängig), Entscheidung „keine Dublette“, Konto-Vorschlag ohne
automatische Umstellung, gebuchter Zugang nach Umstellung des Kontos der Datenquelle weiter erkannt. Lokal (nicht im Repository) mit dem echten Export und einem nachgestellten App-Zugang
geprüft: Treffer wie gemeldet, keine Zufallstreffer bei rund 5 000 Zu-/Abgängen des Imports.

**Grenzen:** Ohne gemeinsamen Hash bleibt die Zuordnung ein begründeter Verdacht (Entscheidung beim Nutzer). Bereits
gebuchte App-Buchungen werden nicht verschoben: Nach „Import-Transfer gilt“ liegen spätere Bewegungen der
Datenquelle weiter auf deren Konto, bis die Konten angeglichen sind (Konto der Datenquelle bzw. einzelne Buchungen
bearbeiten, oder im kuratierten Import vereinheitlichen). Auszahlungen, die später als 7 Tage gutgeschrieben werden,
und Teilgutschriften (andere Menge) werden nicht als Transferseite erkannt.

## M20 – Wallets: Polygon, XRP Ledger, Cardano, Polkadot; Gruppen und Übersicht (0.18.0)

Anlass (03.10.2026): Ledger- und weitere Wallet-Konten über öffentliche Adressen verfolgen; vier zusätzliche Chains
wie in der Referenz-Wallet; Gruppen („Ledger“, „MetaMask“) mit Summen; Übersicht mit Suche, Sortierung, Wert und
Zuständen; Doppelbuchungen und falsche Asset-/Kurszuordnungen künftig verhindern bzw. sichtbar machen.

**Ist/Soll vor der Umsetzung** (Belege im Code des Stands 0.17.1)

| Bereich | Stand 0.17.1 | Beleg | Ergebnis 0.18.0 |
|---|---|---|---|
| Chain-Auswahl | teilweise: Polygon, XRP Ledger, Cardano, Polkadot nur Katalogeinträge (Regex) ohne Connector → „manuell/CSV“ | `providers.PROVIDERS`, `web.WALLET_CHAINS` | vier Connectoren, Auswahl mit Symbol |
| Adressprüfung | vorhanden für BTC/EVM/SOL/KAS (Prüfsummen); XRPL/ADA/DOT nur Regex | `chains/addresses.VALIDATORS` | XRPL-Base58Check, CIP-19-Bech32 (inkl. Stake-Ableitung), SS58-Blake2b |
| Mehrere Adressen | nur Bitcoin (≤ 50 Adressen + xpub) | `service._wallet_fields` | zusätzlich Cardano (Stake-Konto bzw. Adressmodus) |
| Doppelte Konten | teilweise: nur gleiche Primäradresse je Chain | `service.validate` | Identität je Chain inkl. xpub-Ableitung und Stake-Teil, Ablehnung mit Begründung |
| Anbieter/HTTP | vorhanden (GET/JSON-RPC, Budgets, Retry-After) | `chainhttp.ChainHttp` | + JSON-POST, Bearer, `{network}`, HTTP 402; Endpunkte Blockscout, XRPL, Koios, PubFi, Subscan |
| Import/Kennungen | vorhanden (`<chain>:<tx>:<konto>#<bewegung>`) | `wallet.event`, `classify` | unverändert genutzt; UUID-Fehltreffer aus Hashes behoben |
| Bestand/Werte | Bestandsabgleich ja, EUR-Wert in der Übersicht nein | `service.holdings` | Wert je Konto/Gruppe/gesamt, „unbekannt“ bzw. „mind.“, letzter bekannter Wert |
| Gruppen | teilweise: Feld, Anlegen in Gruppe | `wallet_group`, `datasources.html` | + Umbenennen/Zusammenführen, Zuordnen, Summe, Gruppe aktualisieren |
| Übersicht | teilweise: Karten je Gruppe, Status | `datasources.html` | + Suche, Sortierung, Symbol, Kopieren, Explorer, Zustände, Datenhinweise, alle aktualisieren |
| Doppelbuchungsschutz | teilweise: Buchungen derselben Quelle vom Hash-Abgleich ausgenommen | `csvimport._same_events` | andere Datenquellen desselben Anbieters einbezogen (gleiche Seite und Menge → mögliche Dublette) |

**Entscheidungen**

* **Polygon** nutzt den EVM-Adapter (Chain-ID 137): Etherscan API V2 (kostenloser Key, Standard) oder Blockscout
  ohne Key (Blockhöhe über `block/eth_block_number`, `status` 2 = sichtbare Lücke). Die Spiegelung nativer
  Überweisungen als Token-Transfer des Systemvertrags `0x…1010` wird übersprungen. Nativer Coin bis Block
  62.278.656 (Hardfork „Ahmedabad“, PIP-45) als MATIC, danach POL; Umstellung einmal als Vorschlag (Konvertierung
  „migration“) über den aus der Historie berechneten Bestand, im Fortsetzungspunkt über Etappen mitgeführt.
* **XRP Ledger** über öffentliche Full-History-Server (xrplcluster.com, s2.ripple.com): Bewegungen aus den
  Saldoänderungen validierter Ledger (`AccountRoot`, `RippleState`), Gebühr herausgerechnet; Tokens je Währung und
  Emittent; Reserve als gesperrter Bestand; Lücke, wenn die Historie nicht mit der Kontoeröffnung beginnt.
* **Cardano** über Koios: Konto über die Stake-Adresse (aus Basisadressen abgeleitet) – Wechselgeld ist kein
  Abgang; Pfand als prüfbedürftige Bewegung, Reward-Abhebung als Umbuchung, Rewards je Epoche ab Verfügbarkeit;
  Assets über den CIP-14-Fingerabdruck; fremde Eingänge ohne Gebühr zur Prüfung.
* **Polkadot** über Subscan (PubFi-Gateway kostenlos bzw. Subscan direkt bezahlt; ohne Schlüssel kein Abruf):
  Relay Chain und Asset Hub getrennt, Gebühr nur aus eigenen Extrinsics, Staking nur Gebühr, Rewards als Ertrag,
  Migration/XCM/Pools zur Prüfung; Einheiten-Annahme mit Prüfung `amount` ↔ `amount_v2` je Vorgang.
* **Überschneidungen** werden beim Anlegen/Ändern abgelehnt (gleiche Chain): gleiche Adresse, Einzeladresse im
  Kontoschlüssel eines anderen Kontos (Empfang/Wechselgeld bis zum geprüften Index bzw. Gap-Limit), gleicher
  Cardano-Stake-Teil. Bestehende Überschneidungen: Hinweis, Summen zählen das jüngere Konto nicht.
* **Abgleich über Datenquellen hinweg:** Buchungen anderer Datenquellen desselben Anbieters gehen in den
  Hash-Abgleich ein (gleiche Seite und Menge → mögliche Dublette, z. B. neu angelegte Quelle). Dabei fiel ein
  verdeckter Fehler auf: Aus 64-stelligen Transaktions-Hashes wurde eine scheinbare UUID abgeleitet – Vorgänge
  verschiedener Wallets mit gleichem Hash hätten als „bereits vorhanden“ gegolten. UUID-Aliase entstehen jetzt nur
  noch für Anbieter mit UUID-Kennungen (Bitpanda, Coinbase, Kraken).
* **Übersicht:** Wert = beobachteter Bestand × aktueller Kurs; fehlender Kurs/Zuordnung → „mind.“, ohne Bestand
  „unbekannt“, nach Fehlern letzter bekannter Wert mit Alter; Abrufzustand und Datenhinweise getrennt; Aktualisieren
  je Konto, Gruppe, alle – nacheinander, Fehler isoliert. Keine Migration (Gruppen nutzen `wallet_group`).

**Tests:** Adressprüfung je Chain (Prüfsummen, Formate, Netze), Chain-Trennung derselben 0x-Adresse, wiederholter
Abruf ohne Duplikate, mehrere Ereignisse je Transaktion, Gebühren, UTXO-Wechselgeld, Pfand, Reward-Abhebung,
Token gleicher Symbole, MATIC/POL-Umstellung über Etappen, Paginierung, abgebrochene Läufe mitten in Ledger bzw.
Blockbereich, Provider- und Drosselfehler ohne Vorrücken des Fortschritts, Schlüsselpflicht, Überschneidungen
(xpub ↔ Einzeladresse, Stake ↔ Basisadresse), Gruppen ohne Doppelzählung, Werte ohne irreführende 0,00 €, Suche/
Sortierung, Aktualisieren mehrerer Konten mit isoliertem Fehler, neu angelegte Quelle → mögliche Dublette,
Transfer zwischen eigenen Wallets bleibt getrennt. Alle mit synthetischen Daten und nachgebildeten Anbietern.

**Grenzen:** Subscan/PubFi nicht live geprüft (Schlüssel nötig); Einheiten der Subscan-Felder nicht dokumentiert.
Polygon-Bridge-Einzahlungen per State-Sync ggf. nicht in der Historie; Blockscout-Interntransaktionen teils
unvollständig (Lücke angezeigt). XRPL: MPT, AMM-/DEX-Positionen nur als Prüfvorgang. Cardano: ungültige
Plutus-Transaktionen nicht gekennzeichnet. Polkadot: Nomination Pools, XCM, Proxy/Multisig, Vesting und andere
Parachains nicht automatisch abgebildet. Bridges/Cross-Chain-Transfers werden nicht als Transfer gepaart.

## M21 – Kursqualität, KRC-20-Fehler, einheitlicher Fortschritt, Portfolio-UX, Watchlist, Steuerdaten je Jahr (0.19.0)

Anlass (07./08.10.2026): Portfolia systematisch weiterentwickeln – zuerst Ist-Analyse, dann Ursachen statt
Symptome (historische Kurslücken, KRC-20 HTTP 403), danach UX und neue Funktionen; vorhandene Modelle erweitern statt
neu bauen; Nutzerdaten nie ungefragt ändern.

**Ursachen und Behebung**

* **Historische Kurslücken:** CoinGecko-Demo liefert 365 Tage; davor wurde mit Transaktionskursen bzw. dem ersten
  Marktkurs geschätzt. Neu: automatischer Yahoo-Ersatz (`SYMBOL-EUR`/`-USD` + EZB) nur nach Identitätsprüfung
  (Überlappung: Median ≤ 6 %, ≥ 60 % der Tage ± 10 %; sonst gegen eigene Transaktionskurse), Ergebnis je Reihe in
  `series_meta.alt_*`. Kursqualität je Tag (`snapshot_asset_daily.price_kind`) und Abschnitt (`price_gap`), Anzeige
  unter Datenqualität, im Positionsdetail und in der Diagnose. Zusätzlich lokal mit einem echten Export
  geprüft (Daten und Ergebnisse bewusst nicht im Repository).
* **KRC-20 HTTP 403:** Kasplex (go-krc20d) liefert Anwendungszustände (`unsynced`, `internal error`) als HTTP 403;
  das galt bisher als Zugriffsfehler. Neu: Auswertung des Rumpfs, Wiederholung mit Pause, sonst sichtbare Lücke mit
  Fehlerart; Cloudflare-Sperren erkannt; Fehlerarten *API-Key fehlt, ungültig, 403, Rate Limit, nicht erreichbar,
  Endpunkt nicht mehr unterstützt, keine Daten*; Fallback-Kette Kasplex → (weitere Indexer) → CSV. Der konkrete
  Rumpf des gemeldeten Fehlers ist nicht belegt (Annahme).

**Neu**

* **Fortschritt** (`app/progress.py`): ein Mechanismus für Sync, Erstabruf, CSV-Import, Import-ZIP, Kurshistorie;
  feste Phasen mit Gewichten, Prozent nur steigend, globaler Balken.
* **Marktdaten-Fassade** (`app/prices/market.py`) für Dashboard, Positionsdetail und Watchlist.
* **Portfolio-UX:** Top-Bewegungen % | € (gespeichert), Treemap mit „Sonstige (n)“, Positionsdetail 1T–MAX,
  Schnellkauf/-verkauf über die normale Erfassung (Marktkurs als Vorgabe, `price_source`).
* **Watchlist** (Migration 15): mehrere Listen vorbereitet, Reihenfolge, Detail, „Position erstellen“.
* **Steuerdaten je Jahr** (Migration 16: `tax_file`, `tax_record`, `tax_scan`): Ordner `/data/tax` und Upload über
  dieselbe Pipeline (`TaxImportService`, `TaxJsonParser`, `TaxCsvParser`), Jahreserkennung, genau eine aktive Datei
  je Jahr (Unique-Index), Ersetzen nur nach Bestätigung mit Gegenüberstellung, Verlauf bleibt, Zuordnung externe ID →
  Buchungs-ID → Asset/Datum/Menge → nicht zugeordnet/Konflikt; nie neue Buchungen.

**Migrationen:** 14 (`series_meta.alt_*`, `price_gap`), 15 (`watchlist`, `watchlist_item`), 16 (`tax_file`,
`tax_record`, `tax_scan`) – nur neue Tabellen/Spalten, keine Änderung bestehender Daten.

**Grenzen:** Kein zweiter dokumentierter KRC-20-Indexer verfügbar; Yahoo ist inoffiziell; Coins ohne Yahoo-Paar
bleiben geschätzt (markiert); Steuerdaten und Watchlist sind nicht Teil des Gesamtexports (Ordnerdateien bleiben auf
dem Datenträger); Anbieterformate Blockpit/Koinly/CoinTracking für Steuerdaten noch nicht als Parser.

## M22 – Ticker- und Token-Änderungen (0.20.0)

Anlass (08.10.2026): Ticker ändern sich (Coins, Tokens, Aktien), z. B. MATIC → POL – automatisch erkennen oder als
Funktion umsetzen.

**Bestand vorher:** manuelle Buchung „Kapitalmaßnahme“ (Art Migration/Umbenennung) je Konto; MATIC → POL nur im
Polygon-Wallet-Adapter (On-Chain-Bestand); Bitpanda-Umstellungen zur Prüfung. Keine asset-weite Funktion, keine
Erkennung, keine verkettete Kurshistorie.

**Neu:** Migration 17 (`asset_change`); `app/assetchange` mit Umbenennung (Overlay: Kursquelle, Name, Kürzel; Kurse
der bisherigen Reihe als `prev:`-Ersatzkurse bis zum Stichtag, von Ersatzanbietern nicht überschrieben) und Umstellung
(Kapitalmaßnahme „migration“ je Konto über `JournalService.save`, Restbestand, nach letzter Bewegung, idempotent,
Rückgängig); Erkennung aus Register, CoinGecko-Katalog (Namensmuster „migrated to“, „[OLD]“, „(Legacy)“ – Stand
08.10.2026: 2 bzw. rund 140 Coins, Nachfolger bei „[OLD]“ in gut 60 % eindeutig), Anbieter-Beständen und
Kursstillstand; Seiten `/changes`, Formular, Vorschau; Hinweis im Positionsdetail und – bei hoher Sicherheit – oben
auf jeder Seite; Gesamtexport enthält die Änderungen.

**Grenzen:** Register enthält nur belegte Fälle (MATIC → POL); Katalog liefert kein Verhältnis und keinen Stichtag;
für Aktien keine automatische Nachfolger-Erkennung (Yahoo bietet dafür keine dokumentierte Schnittstelle).

## M23 – Finanzielle Korrektheit: Atomarität, Invarianten, Bewertungslücken, Wiederherstellung (0.21.0)

Anlass (08.10.2026): Prüfung der finanziellen Korrektheit (AP1–AP8) auf Basis des Repository-Stands; nur synthetische
Daten und temporäre Datenbanken.

**Behobene Fehler:**

* Token-Umstellung nicht atomar (Buchungen einzeln gespeichert, Fehler danach ließ Teilbuchungen zurück) → eine
  Transaktion, Prüfsumme Vorschau ↔ Übernahme, Sperre gegen gleichzeitige Anfragen, Rückgängig atomar und nur bei
  unverändertem/ungenutztem Bestand.
* Mengen mit genau drei Nachkommastellen (z. B. `61.725`) wurden beim Bearbeiten von Import-/Sync-Buchungen, beim
  Ausbuchen und bei Umstellungen als Tausenderpunkt gelesen (×1000) → Formularwerte mit Dezimalkomma (`forms.s_de`).
* FIFO abhängig von der Dateireihenfolge bei identischem Zeitstempel → gedeckte Abgänge zuerst.
* Transfer mit Mehrempfang verschwand still → Lot ohne Anschaffung + Befund `transfer_excess` (auch in der Diagnose).
* Lots ohne Anschaffung (Zugang per Transfer ohne Lots) nach einem Jahr als steuerfrei gewertet → nie steuerfrei.
* Doppelte Haltefrist-Regel außerhalb des Regelpakets entfernt (nur noch das Steuer-Regelpaket).
* Mehrere Zeilen eines Prüf-Stapels konnten unscharf auf dieselbe Buchung als „Dublette“ passen → „komplex“, prüfen.
* Fehlender Kurs wirkte in TTWROR/IRR/G/V als Verlust (bis −100 %) → Bewertungslücken als Aus-/Einbuchung
  neutralisiert, Bewertungszustand je Zeitraum (**Verhaltensänderung**).
* Mehrdeutige IRR (mehrere Nullstellen) wurde als eine Zahl gezeigt → „nicht eindeutig“.
* Gesamtexport ohne Steuerdaten, Watchlists und Entscheidungen zu alternativen Kursreihen → ergänzt (rückwärts-
  kompatibel); Übernahme in einer Transaktion, Dateien erst danach.
* Fehlermaskierung machte aus „Kein API-Key hinterlegt“ „Kein API-Key ***“ → nur schlüsselartige Werte maskiert.

**Tests:** `test_assetchange_atomic` (Fehlerinjektion, Nebenläufigkeit, Wiederholung), `test_number_roundtrip`,
`test_ledger_invariants` (parametrisiert/zufallsbasiert mit festem Seed: Mengen- und Kostenerhaltung, Unabhängigkeit
von Quell- und Listenreihenfolge, Splits, Migration mit Gebühr, Überverkauf, Jahresgrenze, nachträgliche Buchung), `test_tax_reference` (feste Erwartungswerte), `test_restore_roundtrip` (fachlicher
Vergleich, Idempotenz, Fehler mitten in der Übernahme, beschädigte Archive, Schema ab Version 1).

**Grenzen:** steuerliche Einordnung von Token-Umstellungen bleibt Einzelfall (nur gekennzeichnet); Reihenfolge
taggenauer (ohne Uhrzeit) gegenüber minutengenauen Buchungen desselben Tages bleibt eine Annahme; die Sperre gegen
gleichzeitige Umstellungen gilt je Prozess (SQLite-Transaktion schützt zusätzlich); nach einer Neueinrichtung sind
frühere Umstellungen nicht mehr rückgängig zu machen (ihre Buchungen sind Import-Buchungen).

## M24 – Financial Integrity Hardening & Intelligent Reconciliation (0.21.1)

Anlass (08.10.2026): Restore absturzsicher machen, zentrale Integritätsprüfung, Abgleich mit bevorzugten Lösungen,
Sammelbearbeitung, XIRR-Stabilität, Referenzfälle. Nur synthetische Daten und temporäre Datenbanken.

**Bestandsaufnahme (vorher):** Diagnose mit Empfehlungen, Vorschau, Übernehmen/Rückgängig je Befund; Importprüfung
mit Ergebnis/Sicherheit je Zeile, Stapelaktionen, Verknüpfen; Restore DB-Transaktion + Dateien danach (ohne Journal);
XIRR-Mehrdeutigkeit über Raster. Fehlend: Wiederanlauf, zentrale Prüfung mit Invarianten, Sammelbearbeitung der
Diagnose, automatische technische Verknüpfung, nachgewiesene Nullstellensuche.

**Neu:**

* Restore-Journal (`fullexport`): Prüfung vor jeder Änderung, temporäre Dateien mit fsync und Speicherplatzprüfung,
  DB + Journal in einer Transaktion, Status „unvollständig“ bis alle Dateien per Prüfsumme bestätigt sind;
  Fortsetzen beim Start, beim nächsten Übernehmen oder per Knopf; verwaiste temporäre Dateien werden beim Start
  entfernt. Fehler werden angezeigt statt als Serverfehler.
* Integritätsprüfung (`diagnosis/integrity.py`, `/quality/integrity`): Diagnose-Befunde + Invarianten + steuerliche
  Datenqualität + Kurssprünge + Bewertbarkeit; Schweregrad, Ursache (Rechenfehler/nachgewiesen/Datenlücke/Verdacht),
  Konfidenz, Status aus Entscheidungen; Job mit Fortschritt, Filter, Sortierung, CSV/JSON.
* Sammelbearbeitung (`diagnosis/bulk.py`, `/quality/diagnose/bulk`) mit gemeinsamer Vorschau, Konfliktprüfung,
  einer Transaktion, Protokoll je Befund, Rückgängig als Ganzes.
* Stufe A: technische Identität bei Datenquellen mit „automatisch übernehmen“ automatisch verknüpft (keine Buchung).
* Diagnose-Regel „vollständig gleiche Buchungen“ (Verdacht, bevorzugte Lösung „zusätzliche ausblenden“).
* Vorschau: Anschaffungsdaten offener Lots vorher/nachher, neue negative Bestände.
* XIRR: zertifizierte Nullstellensuche (x = ln(1+r), Intervallschranken, Descartes); „nicht eindeutig bestimmbar“
  auch bei numerisch nicht trennbaren Lösungen.

**Behobene Fehler:**

* Restore konnte nach dem Datenbank-Commit bei Dateifehler/Abbruch unbemerkt inkonsistent bleiben.
* Exakt doppelte Buchungen ohne Kennung (gleicher Zeitpunkt, gleiche Beine/Werte) wurden nicht erkannt.
* Bleibende Buchung bei gleichen bzw. hashgleichen Dubletten hing von der Zeilenreihenfolge der Importdatei ab.
* XIRR-Raster übersah Lösungen < 0,17 Prozentpunkte Abstand; Doppelnullstellen wurden nicht gemeldet.
* Zu-/Abgänge ohne EUR-Betrag wurden im Gesamtportfolio anders bewertet als im Bestand → Scheingewinn bei Zugang
  vor dem ersten Marktkurs.

**Messwerte** (synthetisch, 12.637 Buchungen, 230 Assets, 8 Konten, 7,7 Jahre Kurse, 40 eindeutige + 20 mehrdeutige
Dubletten; Container-CPU, ein Prozess): Ledger 3,6 s · Diagnose 0,4–0,5 s · Integritätsprüfung 2,8–3,4 s (davon
Steuer 1,9 s für 8 Jahre, Kurssprünge 0,95 s) · Lösungsvorschläge 6 ms · Sammelvorschau 25 Dubletten 4,6 s (ein
Ledger-Lauf + Diagnose + Steuer auf der Kopie) · Übersicht warm 0,01–0,02 s · RSS max. 362–369 MB.
Standard-Lasttest (5.725 Buchungen): Übersicht warm 0,008 s, Performance 0,037 s.

**Grenzen:** Integritätsprüfung und Sammelvorschau rechnen das Ledger je Lauf neu (kein inkrementelles Ledger);
Sammelbearbeitung nur für Dubletten/Transfers; Stufe A nur bei Datenquellen mit automatischer Übernahme (CSV-Uploads:
Vorauswahl, Bestätigung); Bewertungslücken: Abrechnung zum zuletzt bekannten Kurs ist eine Annahme (Zeitpunkt der
Wertänderung in der Lücke unbekannt); Restore: Dateien und Datenbank bleiben zwei Systeme – zwischen Commit und
Abschluss kann der Zustand „unvollständig“ sichtbar bestehen, wird aber nie als abgeschlossen gemeldet.

## M24.1 – Nachbesserungen aus dem Betrieb (0.21.2)

Anlass (08.10.2026): vier vom Auftraggeber gemeldete, bisher nicht erkannte Probleme.

1. **AITECH → ACN (Ticker-Umbenennung):** Ursache: Steuertool-Import bucht die Umstellung als Tausch
   `ACN#…` → `ACN`, die Bitpanda-API (`merger_crypto`) zusätzlich als Kapitalmaßnahme `AITECH` → `ACN`; `AITECH` ist
   ein eigenes Asset ohne Bestand → doppelter `ACN`-Bestand, negativer `AITECH`-Bestand, Scheinverlust. Neu:
   Diagnose-Regeln „Umtausch doppelt gebucht“ (Lösung: im Import enthalten/ausblenden) und „Umbenennung als Tausch
   gebucht“ (Lösung: Kapitalmaßnahme „migration“ statt Tausch), Hinweis im Befund „Bestand zeitweise negativ“,
   Schutzregel im Prüf-Stapel (`conversion_twin`, nie automatisch). Analyse der echten Daten nur lokal und lesend;
   Tests bilden die Struktur synthetisch nach (`tests/test_rename_conversion.py`).
2. **Watchlist-Layout:** Klasse `form-grid` hatte kein CSS, Eingabefelder ohne `type` waren ungestylt (betraf auch
   Schnellkauf/-verkauf). Raster und Feldstil ergänzt; mobil (390 px) per Screenshot geprüft.
3. **Synchronisierung:** globale Sperre durch Sperren je Datenquelle ersetzt; Einbuchen serialisiert; Zeitplan je
   Quelle in eigenem Thread; *Abbrechen* je Quelle und für „Alle aktualisieren“ (`tests/test_datasource_concurrency.py`).
4. **Untere Leiste:** eigene Compositing-Ebene und Füllfläche unterhalb der Leiste gegen den sichtbaren Spalt beim
   Ein-/Ausblenden der Browser-Adressleiste. **Nicht auf einem echten Gerät geprüft** – Headless-Chromium bildet die
   dynamische Adressleiste nicht nach; geprüft ist nur Lage und Füllfläche der Leiste.

## M24.2 – Watchlist-Indizes, Web-App (0.21.3)

1. **S&P 500 (`^GSPC`) ließ sich nicht hinzufügen:** Das Yahoo-Muster der Watchlist verbot `^` am Anfang; zudem war
   „Krypto (CoinGecko)“ vorausgewählt. Jetzt: Indizes (`^GSPC`, `^GDAXI`), Devisen/Futures (`EURUSD=X`, `GC=F`)
   erlaubt; eindeutige Yahoo-Symbole (`^…`, `…=X`, `….DE`) werden auch bei Art „Krypto“ als Yahoo erkannt. Indizes
   zeigen den Stand in **Punkten** (nicht in EUR umgerechnet) und bieten kein „Position erstellen“. Grenze:
   7-Tage-Änderung und Verlauf eines Index basieren weiter auf der EUR-umgerechneten Tagesreihe (enthalten den
   Wechselkurseffekt); 24h ist in Punkten.
2. **Untere Leiste springt beim Antippen:** Seitenseitig unverändert (Headless-Messung vor/nach Antippen und
   Seitenwechsel identisch). Ursache ist die Werkzeugleiste des mobilen Browsers, die beim Antippen am unteren Rand
   bzw. beim Seitenwechsel wieder eingeblendet wird – im Browser nicht verhinderbar. Neu: Web-App-Manifest
   (`display: standalone`), Icons 192/512/maskable, Apple-Touch-Icon, Meta-Tags. Vom Home-Bildschirm gestartet läuft
   Portfolia ohne Browserleisten. **Nicht auf einem echten Gerät geprüft.**

## M25 – Belegimport: PDF & Screenshot mit belegter Ergänzung (0.22.0)

Grundlage: PR #1 (Extraktion, Feldbelege, read-only Vorschau) übernommen und ersetzt. Details:
[M25_DOCUMENT_IMPORT.md](M25_DOCUMENT_IMPORT.md).

1. **Lizenz/Architektur:** PyMuPDF (AGPL) → pypdfium2 (Apache-2.0/BSD); Tesseract als Programm; Extraktion im
   isolierten Kindprozess mit Speicher-/CPU-/Zeitlimit und Abbruch. Keine zweite Ledger-/FIFO-/Dubletten-Engine:
   Belege → `Rec` → `CsvImportService.ingest()`; Ergänzungen → Diagnose-Operation `amend`.
2. **Extraktion:** PDF-Text mit Zeilen-Boxen, OCR mit Vorverarbeitung (Dark Mode, kleine Schrift, Scans),
   gezieltes Nachlesen unsicherer Zahlenzeilen, `Decimal`, Dokument-Zahlenkonvention, mehrdeutige Werte ungelöst.
3. **Profile:** Wertpapierabrechnung, Dividende (Quellensteuer, Devisenkurs), Krypto-Abrechnung (Anbieter-ID),
   Wallet-Beleg (Hash), Kontoauszug/Tabelle (Kopfzeilen-Währung), generisch; Plausibilitätsprüfungen; mehrere Belege
   eines Vorgangs → ein Vorgang.
4. **Herkunft je Feld:** A belegt / B rekonstruiert / C geschätzt / ungelöst; Kette Beleg → Stapel → Portfolia
   (nur gleiche Identität) → Datenquellen (Originaldaten) → öffentlich (Bitcoin/Kaspa, nur Hash, opt-in). Schätzungen
   nie als Buchungswert; fehlende Währung nie angenommen.
5. **Abgleich:** neu / vorhanden / Ergänzung / Widerspruch / komplex / ungeklärt; bevorzugte Lösung + bis zu 3
   Alternativen; „Bestehende Buchung ergänzen“ mit Vorschau, Prüfsumme, Rückgängig.
6. **Oberfläche:** Drag-and-drop, Upload- und Phasenfortschritt, Abbruch, Prüfansicht mit Ausschnitt, Korrektur mit
   Validierung, Sammelaktionen (neu bewerten, Original löschen), Einstellungen; mobil ohne Überlauf.
7. **Datenschutz/Betrieb:** lokale Originale (0600, abschaltbar, löschbar inkl. Volltext), keine Inhalte im Log,
   Aufräumen beim Start, idempotente Wiederholung, Fassungen, Upload-Limit 100 MiB je Stapel.
8. **Gefundene und behobene Fehler der Grundlage:** Portfolia-/Datenquellen-Belege wurden bei der Feldauflösung stets
   verworfen (fehlende Ereignis-ID) – Rekonstruktion griff nie; Belege anderer Felder erschienen als „Alternativen“;
   Bridge setzte fehlende Währung still auf EUR; erneutes Auswerten erzeugte doppelte offene Vorschläge.
9. **Tests/Messung:** 57 neue Testfälle (49 Pipeline, 8 Oberfläche; gesamt 788), alle synthetisch; Benchmark `scripts/bench_documents.py`
   (20 Belege gegen 12.577 Buchungen: 8,1 s; RSS 183/132 MB); CI installiert Tesseract und lädt im Docker-Smoke-Test
   einen Beleg hoch; Image 385 MiB entpackt (CI) / ≈ 143 MB komprimiert (Grenze 450 MiB).

Offen/Grenzen: Anbieterprofile nicht an Originalbelegen validiert; EVM-Explorer (Schlüssel nötig) und
Wertpapier-Ausführungsdaten nicht recherchierbar; Belege nicht im Vollexport; optionale KI bewusst nicht umgesetzt.

## M25.1 – Installierbar als App auf Android (0.22.1)

Chrome und Firefox auf Android boten nur eine Verknüpfung an. Ursachen: (1) kein Service Worker (Chrome-Kriterium),
(2) Manifest und Icons bei Basic Auth nur mit Anmeldung abrufbar, (3) Zugriff über `http://<LAN-IP>` – Browser
installieren Web-Apps nur von sicheren Adressen. Behoben: (1) Service Worker unter `/sw.js` (Geltungsbereich `/`,
speichert nichts, greift online nicht ein – Basic-Auth-Dialog bleibt erhalten; ohne Netz Hinweisseite), Registrierung
nur in sicherem Kontext; (2) Manifest, App-Icons und Service Worker ohne Anmeldung, alles andere weiter geschützt;
Manifest mit `id`. (3) ist Sache der Einrichtung: README beschreibt Reverse Proxy, Tailscale und – nur zum Test – das
Chrome-Flag. Geprüft mit Headless-Chromium (`Page.getInstallabilityErrors` leer, Worker aktiv); **nicht auf einem
echten Android-Gerät geprüft**, die Offline-Hinweisseite ließ sich headless nicht auslösen.

## M26 – Wallets PulseChain und peaq, Binance-API (0.23.0)

* **PulseChain** (`pulsechain`, Chain-ID 369): EVM-Adapter über den offiziellen Explorer (Blockscout,
  Etherscan-kompatibel, ohne Key). Historie erst ab Block 17.233.001 (`first_block`) – die kopierte
  Ethereum-Vorgeschichte wird nie abgefragt. Beim Erstabruf liest Portfolia den Bestand am Fork-Block 17.233.000
  (`eth_getBalance`, rpc.pulsechain.com) und legt ihn als prüfpflichtige Eröffnung (Tag `fork`) an; RPC-Fehler sind
  nur eine Warnung. Kopierte Tokens werden nicht eröffnet (Bestandsprüfung).
* **peaq** (`peaq`): SS58-Konten (Präfix 1221, 18 Nachkommastellen) über den parametrisierten Polkadot-Adapter
  (PubFi-Gateway oder Subscan direkt; Reward-Route optional → Hinweis statt Fehler); 0x-Adressen (peaq EVM,
  Chain-ID 3338) über die Etherscan-kompatible Subscan-Route – nur mit direktem Subscan-Key, weil das
  PubFi-Gateway dort keine Query-Parameter zulässt (klare Fehlermeldung). SS58-Codec für Zwei-Byte-Präfixe
  (64–16383). Die Zuordnung H160 ↔ SS58 ist nicht dokumentiert und wird nicht berechnet.
* **Binance** (`binance`): read-only Spot-Connector nach developers.binance.com – HMAC-SHA256-Signatur,
  `X-MBX-APIKEY`, Zeitabgleich (-1021), Gewichts- und Routen-Taktung, 429/418 mit `Retry-After`. Abgerufen: Bestände,
  `myTrades` je Paar (`fromId`), Ein-/Auszahlungen (Fenster < 90 Tage, `offset`), Ausschüttungen (≤ 180 Tage, Fenster
  wird bei vollem Ergebnis geteilt), Staubumtausch, Convert (≤ 30 Tage, `moreData` → Fenster teilen statt eine nicht
  dokumentierte Reihenfolge anzunehmen), Fiat-Käufe/-Verkäufe und Fiat-Ein-/Auszahlungen. Abrufstand je Stream nach
  jedem vollständigen Fenster, ausstehende Vorgänge halten ihn (≤ 30 Tage), Paar-Durchlauf über mehrere Läufe.
  Zugang zweiteilig (API-Key + Secret, gespeichert als `Key:Secret`, Hinweis nur aus dem API-Key).
* Behoben: README-Abschnitte „Datenquellen“ und „Wallets“ waren mit 0.22.1 versehentlich entfernt worden –
  wiederhergestellt.
* Tests: `tests/test_wallets_pulse_peaq.py` (8), `tests/test_binance.py` (10, strenger Mock mit
  Signaturprüfung). **Nicht live geprüft:** Binance (kein Konto), peaq (Subscan-Key nötig); PulseChain-Fork-Block,
  Chain-ID und Antwortformate wurden mit öffentlichen Abfragen abgeglichen.

## M26.1 – Rückmeldungen aus dem Betrieb (0.23.1)

* **BNB Chain:** Routescan führt Chain 56 nicht („chain not supported“, live geprüft) – Option entfernt. Neuer
  Standard **NodeReal BSCTrace** (kostenloser Key, von BNB Chain als BscScan-Ersatz empfohlen):
  `nr_getAssetTransfers` in Blockfenstern ≤ 100.000, je Richtung, `pageKey`; Gebühren eigener Transaktionen ohne
  „external“-Eintrag über `eth_getTransactionReceipt`. **Nicht live geprüft** (kein Key), strenger Fake nach Referenz.
* **PulseChain:** Ursache der Zeitüberschreitung: Der Explorer ignoriert `startblock`/`endblock` (nur
  `start_block`/`end_block` wirken, live geprüft) und durchsuchte die ganze Historie samt Ethereum-Vorgeschichte
  (37 s). Jetzt beide Schreibweisen, Fenster zu 1 Mio. Blöcken, Lesezeit 75 s, harter Abbruch bei Zeilen außerhalb
  des Bereichs (nie Vor-Fork-Vorgänge buchen). Live: Erstabruf einer Beispieladresse 58 s, vollständig.
* **peaq 0x mit PubFi:** statt irreführender Schlüssel-Meldung löst Subscan (`v2/scan/search`, frei über PubFi)
  das zugehörige Substrate-Konto auf; abgerufen werden dessen native PEAQ-Bewegungen. EVM-Gas/ERC-20 nur mit
  direktem Subscan-Key (die EVM-Transaktionsliste hat über PubFi kein dokumentiertes Antwortschema).
* **KRC-20-Kurse:** neue Kursquelle KaspaCom (`/api/token-info/<TICK>`, ohne Key) für Tokens ohne CoinGecko-Eintrag;
  `price` ist live KAS (Doku: USD) – Einheit wird je Antwort über `marketCap` gegen KAS/USD geprüft. Live: BRUCE,
  KASPER, POPKAT bewertet; KEI unbekannt (HTTP 500).
* **Kaspa/KRC-20:** Kasplex-Abruf 5/s statt 2/s (Antwort-Header: 1.000 je Fenster) mit vollem Budget je Etappe;
  bei DNS-Fehlern Hinweis auf die Namensauflösung im Container.
* **RPEPE:** ohne Daten nicht eindeutig zuordenbar (mehrere Token dieses Namens; „Token-ID 44382266“ unbekannt) –
  Rückfrage; vorhandene Werkzeuge: Umstellung mit Verhältnis, Kursquelle ablehnen, Kurssprung-Befund.
* Tests: `test_wallets_bsc_nodereal.py`, `test_krc20_prices.py`, erweiterte PulseChain/peaq-Tests.

## M26.2 – Mobile Leiste, Depotwert verbergen (0.23.2)

* **Untere Leiste nach Tipp auf eine Position verschwunden bzw. zu breit:** Ursache waren die sticky Tabellenköpfe
  der Positionsliste: Sie ragten in Chrome mobil aus dem horizontal scrollenden Tabellenrahmen heraus und
  verbreiterten das Layout-Viewport auf 857 px (statt 390 px, gemessen mit Chromium-Mobil-Emulation) – trotz
  `overflow-x: clip` auf `html`. Fixierte Elemente (untere Leiste, Detail-Sheet) übernahmen diese Breite.
  Behoben: Scroll-Container (`.table-wrap`, `.tabs`) sind Bezugsrahmen (`position: relative`). Zusätzlich endet das
  Detail-Sheet mobil über der unteren Leiste; sie bleibt sichtbar und bedienbar (auch „Mehr“).
  Geprüft per Emulation (alle Hauptseiten 390 px, Navigation aus offenem Detail); **nicht auf einem echten Gerät**.
* **Depotwert verbergen:** Augen-Symbol auf der Übersicht; Gesamtwert und Tagesveränderung in € werden ersetzt
  (nicht nur unscharf), Zustand je Gerät (`localStorage`), vor dem ersten Zeichnen angewendet.

## M27 – Buchungsprüfung über Quellen, Soll-Ist zum Stichtag, Datenschutz-Modus (0.24.0)

Erweiterung der bestehenden Diagnose (gleicher Index, gleiche Befunde, Vorschau/Übernehmen/Rückgängig) – keine
zweite Engine. Neue Regeln in `app/diagnosis/audit.py`, eingehängt in `engine.diagnose`.

* **Wirtschaftliche Dubletten über Quellen:** Steuertool-Import und Börsen-API buchen dieselbe Ein-/Auszahlung –
  netto bzw. brutto mit Gebühr, Minuten auseinander, ohne gemeinsame Kennung. Erkannt über Konto, Asset-ID,
  Richtung, Betrag (brutto/netto/Gebühr) und Zeit, 1:1 deterministisch; Status nach Evidenz (≤ 1 h eindeutig,
  ≤ 36 h, regelmäßiger Tagesversatz, sonst „hinweis“ ohne Empfehlung). Nachfolgende Käufe, die genau einen Zugang
  verbrauchen, sind ein zusätzlicher Beleg. Korrektur „verknüpfen“: App-Buchung → „im Import enthalten“,
  Import-Buchung → als Doppelbuchung ausgeblendet (Überlagerung, `duplicate_of` im Protokoll), Undo wie gewohnt.
* **Bestand nach Quellen:** Saldo, Zeitraum und nur einseitig vorhandene Ein-/Auszahlungen je Quelle; Szenarien
  „nur Quelle X“ und „ohne Doppelbuchungen“.
* **Negative Bestände:** Zwischenstand vs. aktuelle Inkonsistenz, Beginn der negativen Phase, Ursachen (doppelte
  Auszahlung, Sparplan ohne Finanzierung, Ende der Quellhistorie, Gebühr, fehlender Eigenübertrag).
* **Transfers:** Abgänge ohne Gegenbuchung mit Alternativen und Sicherheit in %, auch Tokenwechsel (Bridge/Wrapped)
  über `related_asset` bzw. EUR-Wert; keine Verknüpfung bei mehrdeutigen Zielen.
* **Ungeklärter Vermögensabgang / möglicher Verlust** (neue Befundart, A/B/C) und **Inaktive Konten** (neue
  Befundart; letzte Buchung und letzte Synchronisation getrennt) – Inaktivität bzw. ein fehlerhafter Abruf sind nie
  ein Verlust.
* **Soll-Ist zum selben Stichtag:** externer Bestand gegen Soll zum Abrufzeitpunkt; **Referenzbestände** (Migration
  19, Prüfwert, keine Buchung) gegen Soll bis Ende des Stichtags; Soll aus einem Portfolia-Gesamtexport gilt nicht
  als unabhängige Referenz. Keine Ausgleichsbuchung für Referenz-Differenzen.
* **Oberfläche:** Bereiche Bestandsabweichungen (Tabelle mit Filtern), Mögliche Doppelbuchungen, Ungeklärte
  Transfers, Inaktive Konten, Potenzielle Verluste; Formular für Referenzbestände.
* **Datenschutz-Modus (nur Dashboard):** Das Augen-Symbol maskiert alle Beträge des Dashboards (Depotwert,
  G/V, Kapital, Erträge/Gebühren, Top-Bewegungen) sowie Beträge in Achsen und Tooltips der Dashboard-Diagramme;
  Prozentwerte bleiben sichtbar. Andere Seiten bleiben unverändert.
* **Validierung:** ausschließlich lesend gegen eine Kopie eines realen Exports (Ergebnis im Abschlussbericht, ohne
  personenbezogene Daten im Repository); Tests nur mit synthetischen Daten (`tests/test_diagnosis_audit.py`).

## M28 – Evidenzstufen, Einzelvorgänge und transaktionsgenaue Freigabe (0.25.0)

Nachbesserung der Diagnose aus M27 innerhalb derselben Engine (Index, Befunde, Empfehlungen, Vorschau, Übernehmen,
Undo, Sammelbearbeitung) – keine zweite Diagnose-, Abgleichs- oder Korrektur-Engine.

* **Evidenzstufen statt Prozent:** Dubletten *nachgewiesen* nur mit gemeinsamer Anbieterreferenz bzw. Hash, sonst
  wahrscheinlich/verdacht/hinweis; Belege dafür und dagegen sowie fehlende Informationen je Vorgang. Transfer-Scores
  nur noch als „Matching-Score x/95, regelbasiert“ mit qualitativer Einstufung.
* **1:n, n:1, n:m:** begrenzte, deterministische Kombinationssuche; angezeigt, nie verknüpft, Mehrdeutigkeit
  ausgewiesen; keine Buchung in zwei Vorgängen.
* **Transfers global statt gierig:** alle Kandidaten, Komponenten, Hash vor Zeit/Betrag, exakte Zuordnung bis 6 je
  Seite; eindeutige Zuordnung mit Ausschlussbegründung, sonst „mehrdeutig“ ohne Vorschlag. Tokenwechsel nur über
  `related_asset`.
* **Verlust:** C nur über Benutzerkennzeichnung der Buchung (als solche ausgewiesen); kompromittiertes Konto → erst
  Rettungsziel prüfen, sonst B.
* **Referenzbestand mit Zeitpunkt:** Uhrzeit, Zeitzone, Buchungs-/Wertstellungstag (Migration 20, additiv);
  Fiat auf den Cent, Krypto exakt; alte Einträge und Exporte bleiben gültig.
* **Einzelvorgänge:** eigene Befunde je Paar bzw. Abgang mit Vorgangsseite (A Buchungen, B Diagnose, C Optionen,
  D Wirkung in der Vorschau); Sammelbefund ohne Vorauswahl.
* **Entscheidungen:** übernehmen (idempotent per Vorschau-Prüfsumme, eindeutiger Index), ablehnen (bleibt bei
  gleicher Evidenz, „überholt“ bei neuer), später prüfen, ungeklärt lassen, rückgängig (abhängigkeitsgeprüft);
  Zustände je Vorgang und Zähler je Bereich. Markierungen reisen mit dem Gesamtexport.
* **Vorschau:** zusätzlich Gesamtwert/Allokation (nur gespeicherte Kurse), unbewertete und geschätzte Positionen,
  Referenzbestand-Differenzen; Sammel-Vorschau mit Hinweis zur kombinierten Wirkung.
* **Tests:** `tests/test_diagnosis_cases.py` (22 vorgegebene Fälle plus Export/Restore, Web-Ablauf, Undo-Abhängigkeit)
  mit synthetischen Daten; bestehende Tests an die bewusst geänderte Semantik angepasst (Sammelbefund ohne
  Vorauswahl, mehrdeutige Transfers ohne bevorzugte Lösung).
* **Validierung:** lesend gegen eine Kopie eines realen Exports (Datenbank-Fingerprint vorher = nachher); Ergebnis im
  Abschlussbericht, keine personenbezogenen Daten im Repository.

## M28.1 – Rückmeldungen: Tooltip, Datenquellen, Bestandsabweichungen, Quellenrang (0.25.1)

* **Wertentwicklung:** Tooltip liest Depotwert/Kapital/Gewinn aus den Rohdaten des nächsten Tages (lttb-Ausdünnung ließ
  den Depotwert stellenweise fehlen); gleiches Vorgehen bei Performance- und Positionsverlauf.
* **Sammelaktualisierung:** je Anbieter nacheinander, Anbieter gleichzeitig (4 Worker); Abbrechen erreicht alle laufenden
  Konten; Kaspa-Abdeckung nennt die Laufzeit von KAS und KRC-20.
* **PubFi/Polkadot (403):** laut PubFi-Doku 401 = Schlüssel, 403 = Konto/Route; Meldung mit Code, Grund, Anfrage-ID.
  Nicht live prüfbar (kein Schlüssel) – die Ursache auf Kontoseite bleibt beim Anbieter.
* **peaq:** Subscan-Konto unbekannt → Kettenabfrage über öffentlichen EVM-RPC; leer = kein Fehler, sonst Hinweis auf
  Subscan direkt.
* **Bestandsabweichungen:** kompaktere Tabelle, eigener Scrollbereich.
* **Integrität/Diagnose:** „Aktuelle Bestände abfragen“ (nur lesend, Hintergrund); Quellenrang API > CSV > Beleg >
  Steuertool > manuell bei Doppelungen.

## M28.2 – Bestand laut Kette, Explorer-Links (0.25.2)

* **peaq:** 0x-Adresse mit Bestand, aber ohne Subscan-Konto → Kettenbestand (öffentlicher RPC) als Ist-Bestand, Historie als
  Lücke; auch ohne PubFi-Schlüssel erreichbar.
* **Alle EVM-Chains:** Ausweichweg „Bestand laut öffentlichem RPC“ bei Anbieterproblemen (nur Bestand).
* **Diagnose:** „Explorer ↗“ je Konto in den Bestandsabweichungen.

## M29 – Bestandsrekonstruktion, Abweichungsfenster und evidenzbasierte Buchungsaufklärung (0.26.0)

Leitsatz: **trennen, was tatsächlich existiert, was Buchungen erklären und was historisch unbekannt bleibt.**

* **A · Abweichungsfenster** (`app/diagnosis/windows.py`, Seite *Abweichungsentwicklung*): je Position
  `Differenz(t) = Referenz(t) − Ledger(t)` (Decimal, zeitzonenfest, mehrere Referenzen je Tag, widersprüchliche Referenzen
  ohne Rückschluss). Fenster: *erstmals nachweisbar · verändert sich · bleibt stabil · verschwindet wieder · nicht
  eingrenzbar* (eine einzelne Referenz lokalisiert nichts); gegenläufige Fehler werden als Möglichkeit ausgewiesen. Das
  Diagramm zeigt Referenzen als Mess-/Nachweispunkte (keine Verbindungslinie), Ledger-Ringe, Differenz und getönte
  Fenster; Klick öffnet Buchungen und Kandidaten, darunter Tabellenansicht.
* **B · Ursachenkandidaten:** Dubletten (Befunde der Diagnose), nicht finanzierte Sparpläne und ungebuchte Vorgänge
  (Prüf-Stapel, Recherche) – eingestuft nach **Beleglage** (*Belegt · Stark gestützt · Plausibel · Ungeklärt ·
  Widersprüchlich*), nie nach der Höhe; ein zur Differenz passender Betrag ist kein Beleg. Hypothetische Wirkung auf
  Differenz, negative Zwischenbestände und andere Positionen.
* **C · Evidenzrecherche** (`app/diagnosis/research.py`, Migration 21): lokal zuerst (Prüf-Stapel, Belege, gespeicherte
  Ergebnisse), danach gezielt und **nur lesend** bei der Datenquelle (`DataSourceService.research_fetch`: kein Abrufstand,
  kein Status, kein Prüf-Stapel, keine Buchung, keine Stammdaten-Schreibzugriffe; Etappen mit Fortsetzungspunkt,
  Zeit- und Etappenbudget, abbrechbar, je Quelle exklusiv mit dem normalen Sync). Ergebnisse liegen getrennt
  (`diag_research_event`, idempotent je Quelle und Ereignis). Kein Abruf privater Daten an öffentliche Suchdienste oder
  KI; Explorer-Links öffnet nur der Nutzer.
* **D/H · Rekonstruktionskandidaten** (Befundart *Rekonstruktionskandidat*): Einzelfreigabe in zwei Schritten –
  *Als Importkandidat bereitstellen* (Prüf-Stapel, keine Buchung) und *Diesen einen Vorgang buchen* (genau eine Zeile,
  alle anderen unverändert) – je mit Vorschau, Prüfsumme (idempotent), Datenstand-Prüfung, Protokoll, **Rückgängig**.
  Teilweise belegte Vorgänge nennen ihre fehlenden Angaben (nicht ergänzt); nicht rekonstruierbare Differenzen bleiben
  ungeklärt.
* **E · Bestandsklärung** (`/quality/diagnose/stock`): A buchhalterisch · B bestätigter Referenzbestand (fortgeschrieben,
  nie „verifiziert“ bei späteren Buchungen, „veraltet“ ab 90 Tagen) · C ungeklärte Differenz, mit Evidenz,
  Untersuchungsstatus, möglichen Ursachen, Befunden und Entscheidungshistorie. Berechnet, nicht gespeichert; ändert weder
  Ledger noch FIFO/Steuer.
* **F · Bestätigte Bestandssicht** (optional, gekennzeichnet): je Position genau eine Menge (Referenz ersetzt Ledger –
  keine Doppelzählung); nur belastbare Bewertung, sonst „unvollständig“ (nie 0); fließt nicht in FIFO, Einstand,
  realisierte Gewinne, Steuer, Haltefrist oder XIRR ein.
* **G · Eröffnungsposition:** nur als Ausnahme mit belegtem Datum und belegten Kosten (keine Vorgabewerte) – siehe
  `docs/OPENING-POSITIONS.md` (inkl. Prüfung des KREST-Musters).
* **Performance:** Präfixsummen je Position, Bisektion für Zwischenbestand und Ähnlichkeitssuche (3.000 Buchungen,
  1.500 ungebuchte Vorgänge: ≈ 1 s); keine Abfrage bei Seitenaufruf; Diagnose bleibt deterministisch.
* **Grenzen:** Fenster sind so grob wie die Referenzen; Plattform-spezifische Suche beschränkt sich auf die vorhandenen
  Connectoren (Anbieter-API, keine Suche über Hashes/Adressen bei Dritten); Bereitstellen und Buchen sind zwei Schritte
  (die Wirkung auf FIFO/Steuer zeigt die Vorschau der Buchung); Beleg-Dokumente erscheinen als Evidenz erst nach dem Import
  in den Prüf-Stapel; die Belegprüfung der Eröffnungsposition obliegt dem Nutzer.

## Financial Integrity – Prüfworkflow, Universalimport, Reconciliation, historische Vollständigkeit (0.27.0)

Leitsatz: **was bearbeitet ist, bleibt abgeschlossen; was ungeklärt ist, erscheint zuverlässig in der Prüfung; jede
Nullanzeige hat einen Grund.** Plan: `docs/FINANCIAL_INTEGRITY_IMPLEMENTATION_PLAN.md`, Abschlussbericht mit Phasen,
Commits und Tests: `docs/FINANCIAL_INTEGRITY_IMPLEMENTATION_REPORT.md`.

* **Prüfworkflow:** eine zentrale Zustandsdefinition je Vorgang (`app/csvimport/review.py`), Stapel schließen
  fachlich korrekt (auch beim Undo und beim Start), Nullanzeigen mit Grund, „nicht übernehmen“/„zurückstellen“ für
  alle offenen Fälle, keine Wiedervorlage erledigter Vorgänge ohne neue Evidenz, BNB-Entscheidungen bleiben gültig.
* **Zentrale Prüfung** `/journal/pruefung` über alle Stapel mit Filtern, Vorauswahl sicherer Verknüpfungen, einer
  gemeinsamen Vorschau (Bestände, Gebühren, FIFO, rückwirkend betroffene Steuerjahre) und Ausführung je Stapel atomar.
* **M29-Hardening:** Positionen ohne Buchung, bestätigt vs. fortgeschrieben, Teilbewertung, getrennte Quellen- und
  Abgleichsevidenz, Mehrbein-Vorgang als eine Freigabe.
* **Universalimport:** CSV, XLSX, ZIP (sicher entpackt), Portfolia-Formate, jede Börse als Datei-Datenquelle,
  Herkunft je Stapel (Migration 22).
* **Neue Sichten (rein lesend):** Quellenabdeckung `/quality/abdeckung`, Ereignisgruppen `/journal/ereignisse`,
  Anschaffungsdaten `/quality/anschaffung`, Vollständigkeitscockpit `/quality/cockpit` mit Fallanalyse.
* **Derivate:** Funding, Futures-PnL, Liquidation, Margin(-Zins) ohne externen Kapitalfluss, nie still § 23.
* **Qualität:** Regressionen der gemeldeten Fälle (u. a. Pionex-Rest 0,0000388030 USDT exakt), Massentest mit
  100 000 Quellzeilen, Export/Restore, lesende Validierung gegen eine Kopie eines realen Exports (drei Befunde
  behoben: Abdeckung des kuratierten Imports, Rundungs-/Gebührentoleranz der Gruppen, Fallanalyse aller Positionen).
* **Grenzen:** native Futures-Exporte von MEXC/Bybit/Pionex nicht verifiziert (Spaltenzuordnung); Derivate ohne
  automatische steuerliche Einordnung; Docker-Build nur in CI.

## 0.53.0 – Review-Korrekturen Steuerreport (Rundung, Prüfstatus, Quellensteuer, Kursalter, OCR)

Anlass: externe Review des Steuermoduls. Befunde im Code nachvollzogen, bestätigte Punkte korrigiert:

* **§ 23 Rundung (P0):** Der Jahressaldo und damit die Freigrenze werden aus **ungerundeten** Einzelwerten gebildet; die Zeilen
  sind nur zur Anzeige auf Cent gerundet. Beispiel: 999,90 € + 100 × 0,004 € = 1.000,30 € ist voll steuerpflichtig, die
  Zeilensumme ergäbe 999,90 €. Die Differenz steht als Hinweis `rounding` im Bericht (`round_diff`).
* **Prüfstatus (P0):** Jeder Bericht trägt „Prüfstatus: vorläufig – N offene Punkte“ (Deckblatt, Reiter Steuerjahr, Berichtsliste),
  solange Warnungen/kritische Hinweise oder ein offenes Jahr bestehen. „Ohne automatisch erkannte offene Punkte“ ist ausdrücklich
  **kein** Nachweis der Richtigkeit.
* **Rekonstruierte/geschätzte Buchungen (P0):** neuer Warnhinweis `reconstructed_tx` für Buchungen des Jahres und Anschaffungs-
  buchungen der Veräußerungen mit Marke rekonstruiert (Sparplan/Lücke), Korrekturvorschlag, automatisch korrigiert, geschätzt,
  Durchschnittskurs, fehlender Kurs/Negativbestand im Quellsystem, auf Anweisung gebucht.
* **Quellensteuer ohne zugeordnete Dividende (P1):** wird nur gemeldet (`wht_unmatched`), nicht mehr automatisch angerechnet.
* **Höchstalter von Kursen/Devisenkursen (P1):** für steuerlich verwendete Werte höchstens 7 Tage (`PRICE_MAX_AGE_DAYS`,
  `FX_MAX_AGE_DAYS`); ältere Werte gelten als fehlend (Ertrag „ohne Wert“ → Warnung `income_no_value`).
* **OCR (P1):** Bezeichnungen und Spaltenköpfe werden umlautgefaltet erkannt („Gebuhr“ = „Gebühr“, „Stuck“, „Ausfuhrungskurs“).
* **README (P2):** Chain-Zahl korrigiert (dreizehn); Umfang der ZIP-Sicherung präzisiert (Originalbelege und erzeugte Berichte
  gehören nicht dazu).
* **Nicht im Code prüfbar (Nutzerdaten):** 24 rekonstruierte Buchungen, fehlendes `fee_eur`, unbekannte `opening_balance`-Tags,
  CoinGecko-ID des TH-Tokens – siehe Datenqualität bzw. Anhang „Anpassungen“ im Bericht.
* **Grenzen:** Fondsart und Inland/Ausland bleiben Annahmen (Hinweise `fund_type_guess`, `account_kind_default`); keine
  Steuerberatung.

## 0.52.4 – Jahres-Löschknopf entfernt

* „Nur {Jahr} löschen“ entfällt (überflüssig neben „Löschen“ je Bericht und „Alle Berichte löschen“); Route und Service-Funktion entfernt.

## 0.52.3 – „Alle Berichte löschen“

* Der Knopf „Alle {Jahr} löschen“ löschte nur das gewählte Jahr. Neu: **„Alle Berichte löschen“** entfernt alle Berichte aller Jahre samt
  Sammel-ZIP (mit Rückfrage und Anzahl). Einzelne Berichte löscht der Knopf „Löschen“ am Bericht; der Jahres-Knopf entfällt.

## 0.52.2 – Steuerseite: Berichte überall erreichbar, schneller

* **Berichte nach Neustart „weg“:** Die Berichte waren vorhanden (Datenbank und `/data/reports`), standen nach dem Umbau aber nur im
  Reiter „Belege & Berichte“, die Startansicht „Übersicht“ zeigte sie nicht. Jetzt nennt die Kopfleiste auf **jedem Reiter** die
  erzeugten Berichte des gewählten Jahres mit Download-Links, der Reiter trägt die Anzahl. Test inkl. Neustart mit derselben Datenablage.
* **Ladezeit:** `ParamSet.for_year` wurde je Buchungsteil mehrfach neu berechnet (Tiefenmischung, bei 0.52 zusätzlich mehrere Dateiprüfungen).
  Das Ergebnis wird jetzt zwischengespeichert (bis sich eine Datei ändert): 20.000 Aufrufe 7,7 s → 0,4 s.

## 0.52.1 – Steuer-PDF: Wortmarke „Portfolia“ in Firefox/Android

* **Fehlerbild:** Im PDF-Betrachter von Firefox (Android) fehlte im Titelblock das „P“ („ortfolia“); Adobe, Chrome, pdfium,
  PyMuPDF und pdf.js am Desktop (auch Firefox 142) zeigten den Text korrekt – die Textebene war immer vollständig.
* **Maßnahme (ohne Reproduktion im betroffenen Viewer):** Die einzige Besonderheit der Stelle war ein Bild mit Transparenzmaske
  (Logo) unmittelbar vor dem Text. Das Logo wird jetzt **opak auf Weiß** gerechnet (keine `/SMask`), der Titelblock ist ein
  eigener Flowable, der **erst den gesamten Text und zuletzt das Bild** zeichnet; ebenso die Kopfzeile. Test:
  keine `/SMask`/`/Mask` im PDF, Text steht im Inhaltsstrom vor dem Bild.
* **Grenze:** Ob der Viewer des Anwenders damit korrekt darstellt, ließ sich hier nicht prüfen.

## 0.52.0 – Steuerseite: freie Jahre, Reiter, Regelwerk-Dateien (YAML)

* **Jahresauswahl** vom Jahr der ersten Buchung bis in die Zukunft (Auswahl bis laufendes Jahr + 2, jedes Jahr 2000–2100
  eingebbar; Gruppen „Mit Daten“, „Ohne Daten“, „Zukunft (vorläufig)“). Berichte lassen sich für jedes Jahr erzeugen.
* **Alle Jahre erzeugen** als Hintergrundlauf mit Fortschritt und ZIP-Download (je Jahr ersetzt die neue Fassung die
  frühere; ein fehlerhaftes Jahr stoppt den Lauf nicht).
* **Steuerseite vereinfacht:** Kopfleiste (Jahr · Parameterstand · „Bericht erzeugen“ · „Alle Jahre erzeugen“),
  vier Reiter (Übersicht · Steuerjahr · Belege & Berichte · Einstellungen), Hinweise je Seite gruppiert/zugeklappt,
  seltene Optionen unter „Erweitert“.
* **Regelwerk-Dateien (Parameterstände)** als YAML: Upload mit Schema- und Plausibilitätsprüfung, Diff-Vorschau,
  Aktivieren/Deaktivieren/Herunterladen/Löschen, Vorlage-Download; Gültigkeitsjahre, Status Entwurf/gültig, Quelle.
  Der Bericht nennt Parameterstand und Datei-Prüfsumme (Datengrundlage); Entwürfe → Hinweis „vorläufig“.
* **Regelschalter** (Standard = geltendes Recht): `crypto.exempt_after_holding`, `crypto.freigrenze_mode`
  (Freigrenze/Freibetrag), `capital.derivative_loss_cap` (bisher fest 2023; jetzt Parameter: 20.000 € für Verluste
  2021–2023). Grenze: nur Werte/Schalter, keine Rechenlogik; eine Auslegungswahl BMF 2022/2025 ist ohne belegten
  rechenwirksamen Unterschied nicht umgesetzt (Verbrauchsfolge und Ertragseinstufung sind bereits Optionen).
* **Grenzen:** Der Schalter `exempt_after_holding` und die Haltefrist richten sich nach dem Anschaffungsjahr der Lots;
  `derivative_loss_cap` löst nur den Hinweis `deriv_loss_cap` aus (keine getrennte Verrechnung); Werte in Dateien gelten
  „ab Jahr“ und werden von späteren Standardeinträgen überdeckt (Warnung beim Hochladen).

## 0.51.2 – Steuer-PDF und Berichtsliste (mobil)

* **PDF:** Kopf- und Fußzeile kürzen den linken Text mit „…“, damit er nie in den rechten Text („Portfolia · Jahr“,
  „Seite x von y“) hineinläuft; Zeichen ohne Glyph in der eingebetteten Schrift (→, ↔) werden durch „->“/„<->“ ersetzt
  (vorher leeres Kästchen, z. B. im Deckblatt „Steuern → Regelwerk“). Tests: Logo überdeckt keinen Text (Hoch- und
  Querformat), „Portfolia“ steht vollständig auf jeder Seite.
* **Mobil:** Berichtsliste in *Steuern → PDF-Bericht* ohne Überlauf (Kopfzeile mit Datum/Regelwerk, Download-Knöpfe
  brechen um); Löschen als beschrifteter Knopf „Löschen“ mit Rückfrage statt „×“.

## 0.51.1 – ISIN automatisch ermitteln

* Wertpapiere, die nur mit WKN geführt werden, bekommen ihre **ISIN automatisch** (`app/prices/isin.py`, täglicher Lauf
  und Knopf „ISIN jetzt abrufen“ in *Steuern → Zuordnung Wertpapiere*; abschaltbar):
  1. ISIN/WKN-Paare aus ausgewerteten Bankbelegen, 2. Onvista-Suche (öffentlich; nur die WKN wird übermittelt; Treffer nur
  bei exakt gleicher WKN, gültiger Prüfziffer und eindeutiger ISIN), 3. für Derivate deutscher Emittenten die berechnete
  ISIN `DE000` + WKN + Prüfziffer (als „berechnet“ gekennzeichnet).
* Quelle und Zeitpunkt je Eintrag in der Einstellung `assets.isin`; die importierten Stammdaten bleiben unverändert, die
  Steuerauswertung (Verkaufstabellen, Abgleich mit der Bank, Ausbuchungs-Erkennung) ergänzt fehlende ISIN daraus.
  Ohne Treffer erneute Suche nach 14 Tagen; Netzwerkfehler gelten nicht als „kein Treffer“.
* Echtdaten-Test (58 Wertpapiere ohne ISIN): 48 gefunden (38 online, 10 berechnete Derivate), 10 ohne Treffer (u. a.
  delistete Werte). Der Abgleich mit der Erträgnisaufstellung erkennt jetzt auch T1 Energy (Verkauf 05.06.2025 laut Bank ↔
  Totalverlust-Ausbuchung vom 28.09.2026 in Portfolia).

## 0.51.0 – Nachweise für Prüfung und Rückfragen (BMF Rz. 90–104)

* **Veräußerungsliste** (Kryptowerte): je Zeile zusätzlich *Kurs je Einheit*, *Anschaffung durch* (Kauf, Tausch, Ertrag,
  Zugang ohne Gegenbuchung …) und *Quelle · Tx* (Datenquelle und Kennung der Buchung).
* **Anhang im Steuerreport** (und „Anpassungen“ in der Aufstellung zur Anlage SO): Anpassungen, Schätzungen und
  manuelle Buchungen des Jahres mit Markierung im Klartext (Presale-Tranche, im Quellsystem manuell angelegt,
  Durchschnittskurs, rekonstruiert …), Herkunft und Hinweis; **Bestand zum 31.12.** je Wallet/Konto mit
  Anschaffungskosten; **Umschichtungen** zwischen eigenen Wallets.
* **Buchungsstand** (SHA-256 über alle Buchungen inkl. in Portfolia erfasster, synchronisierter und geschätzter) und
  Steuerbescheinigungen (Datei-Hash) in allen drei PDFs.
* Hinweise beziffern: Zugänge ohne Gegenbuchung (Anschaffungskosten, neue Haltefrist), Abgänge ohne Gegenbuchung
  (Anschaffungskosten der Einheiten); Einkünfte-Tabelle mit Markierung, Quelle und Airdrop-Hinweis (Rz. 70–75).

## 0.50.1 – Verkaufsbeleg der Bank ↔ Ausbuchung in Portfolia

* Im „Abgleich Verkäufe“ erkennt Portfolia einen **Verkauf laut Bank, der in Portfolia fehlt**, wenn dasselbe Wertpapier
  (ISIN bzw. WKN) dort später als Totalverlust-Ausbuchung gebucht ist, und nennt Datum und Betrag der Ausbuchung. Umgekehrt
  werden **Ausbuchungen ohne Bankbeleg** („nur in Portfolia – Ausbuchung (Totalverlust)“) gekennzeichnet.
* Bewusst **kein Auto-Ersatz**: Die Buchung ersetzt der Nutzer im Journal (Ausbuchung löschen, Verkauf buchen bzw. den
  Verkaufsbeleg über den DKB-Import zuordnen) – die Wirkung hängt von Belegen ab, die Portfolia nicht kennt.
* Grenze: Der Abgleich nutzt die ISIN der Bankliste; Wertpapiere, die in Portfolia nur mit WKN geführt werden, werden nur
  über Datum und Betrag zugeordnet (ISIN in den Stammdaten nachtragen verbessert die Zuordnung).

## 0.50.0 – Steuerbescheinigungen der Bank einlesen

* Neue Seite **/tax/certs** („Steuerbescheinigungen“): Jahressteuerbescheinigung und Erträgnisaufstellung der Bank als PDF
  hochladen. Auswertung lokal im isolierten Prozess (kein OCR, kein Netzwerk; neuer Modus `plain` für gedrehte
  Tabellenseiten), Dokumentart aus dem Inhalt, keine Personendaten, Rechenprobe der Kapitalertragsteuer; Original als
  Beleg unter `<data>/tax/certs/<Jahr>/`, Werte in der Einstellung `tax.certs`; erneuter Upload ersetzt, gleicher
  Inhalt wird nur gemeldet, Löschen je Dokument.
* Wirkung im Steuerbericht: Kontrollwerte laut Bescheinigung (Anlage KAP Zeilen 7, 8, 16/17, 37, 38, 40) als
  maßgeblich für Inlandsdepots; der ausgewiesene **Sparer-Pauschbetrag ersetzt „bereits genutzt“**, solange dort nichts
  eingetragen ist; weist die Bank mehr als den Einzel-Pauschbetrag aus, wird **Zusammenveranlagung automatisch
  aktiviert** (Hinweis im Upload-Ergebnis, Option bleibt änderbar).
* **Abgleich Verkäufe** (Tabelle in der Aufstellung zur Anlage KAP): Verkäufe der Erträgnisaufstellung ↔ Portfolia (ISIN,
  Datum ± 3 Tage; bei Wertpapieren ohne ISIN über Datum und Betrag) mit Status übereinstimmend / Rundung / abweichend /
  fehlt in Portfolia / nur in Portfolia; Hinweise zu Aktien-Saldo und Kapitalerträgen gegenüber der Bank (Verlusttöpfe
  der Bank, „Einbuchung VT Sonstige aus wertlosen Anlagen“).
* Grenzen: Layout der DKB (andere Banken: Summenzeile und Bescheinigung ggf. nicht lesbar – dann klare Meldung);
  Originaldateien sind nicht Teil des Vollexports (Werte schon, über die Einstellungen).

## 0.49.1 – Steuer: Derivate, Verlustvortrag-Kette, Rundung, Hinweise

* **Derivate** (Turbo-/Optionsscheine, Warrants, Mini-Futures, Knock-outs; Namenserkennung inkl. Kurzformen wie
  „TURBOP O.END“ und „EMITTENT/CALL/…“): eigener Typ `derivative` → Termingeschäft (§ 20 Abs. 2 S. 1 Nr. 3 EStG),
  Topf „Sonstige“ statt Aktien-Topf. Die frühere Verlustgrenze von 20.000 € (§ 20 Abs. 6 S. 5 a. F.) hat das
  Jahressteuergesetz 2024 aufgehoben; für ≤ 2023 mit Verlusten über 20.000 € erscheint ein Hinweis. Im Export des
  Entwicklers wurden 25 Derivate erkannt (zuvor als Aktie geführt).
* **Verlustvortrag § 23**: leere Eingabe = automatisch aus den berechneten Vorjahren (Verlust erhöht, steuerpflichtiger
  Gewinn verbraucht, unter der Freigrenze unverändert); ein eingetragener Betrag – auch „0“ – überschreibt. Summary
  zeigt Vortrag aus Vorjahren und ins Folgejahr. `RulePack.carry_out` + `TaxService._year_result`.
* **Rundung**: Zeilen gehen auf (Preis − Werbungskosten − Anschaffung = Gewinn); „−0,00 €“ wird „0,00 €“.
* **Hinweise im Bericht**: Freigrenzen-Abstand (§ 23, § 22 Nr. 3) mit den zur Prüfung markierten Zuflüssen und dem
  Wert ohne sie; „FIFO über alle Wallets“ als nicht BMF-konform (Rz. 61/62); Sparer-Pauschbetrag der Inlandsdepots.
* **Zusammenveranlagung** steht als erste Option unter „Veranlagung“.
* **Anlage KAP 2025**: Zeilen 7, 8, 16/17, 37, 38, 40 (Steuerbescheinigung der DKB 2025 auf dem amtlichen Muster) sowie
  41/42 (Erläuterung derselben Bescheinigung) belegt; die Auslandsfelder (19–23) bleiben offen, da die Quellen sich
  widersprechen.

## 0.49.0 – Steuer-PDFs: Portfolia-Name und Logo, Berichte ersetzen/löschen

* Alle Steuer-PDFs (Report, Anlage SO, Anlage KAP) tragen das Portfolia-Logo (Variante für hellen Hintergrund,
  `app/static/img/logo-light.png`) in der Kopfzeile jeder Seite und als Titelblock auf Seite 1; fehlt die Datei,
  entsteht der Bericht ohne Logo. `Doc.brand=False` schaltet das Branding ab.
* Ein neuer Bericht **ersetzt** die früheren Berichte desselben Jahres und Regelwerks (erst nach erfolgreicher
  Erzeugung); „Vorige Fassung behalten“ hebt das auf. Zusätzlich „Alle {Jahr} löschen“ (mit Rückfrage).
* Behoben: Zwei Berichte in derselben Sekunde teilten sich einen Ordner – Ordnernamen sind jetzt eindeutig, und
  `delete_report` löscht keinen Ordner, den ein anderer Bericht nutzt.

## 0.48.2 – Reiter „Community“

* Eigener Reiter *News → Community* für die Telegram-Kanäle (neueste zuerst, voller Text, Bild, Telegram-Link, Kanal-Filter).

## 0.48.1 – Telegram-Kanäle in der App hinzufügen

* Formular in *News → Quellen* (Kanal oder Feed-URL + Bridge-Adresse), Feed-Prüfung vor dem Speichern, Duplikatschutz,
  `sources.yaml` kommentarerhaltend; je Quelle „KI an/aus“, „Entfernen“ (nur Telegram). `app/news/telegram.py`.

## 0.48.0 – Telegram-Kanäle als News-Quelle

* Quelltyp `telegram` (RSS/Atom, z. B. RSS-Bridge): Titel aus dem Text, voller Beitragstext, Bild beim Abruf sofort gecacht
  (Quell-Host im LAN ausdrücklich erlaubt), Standard `ai: false` (keine Analyse/Ereignisse/Berichtsaufnahme), Kennzeichnung
  „ohne KI“. Bild-Proxy in `app/news/images.py` ausgelagert. Details in `docs/NEWS_INTELLIGENCE.md`.

## 0.47.4 – News-Status: Restzeit

* „Restzeit: ca. …“ unter den Rückstau-Balken (Durchsatz gemessen/nominal, Zufluss, Pause, Tageskontingent, „wächst derzeit“).

## 0.47.3 – News-Status: Fortschrittsbalken

* Balken für Rückstau Artikeltexte (3 Tage) und KI-Analysen (48 h) unter „Pipeline heute“ (`role="progressbar"`).

## 0.47.2 – News-Status: Abruf-Rückstau

* **Behoben:** Artikel pausierter Quellen verdrängten abrufbare Artikel aus der Auswahl jedes Laufs (Rückstau sank nicht).
* Abruf je Lauf 20 → 40; Status zeigt Zeitraum, Engpass und pausierte Quellen; Hinweisbalken der KI-Box mit mehr Abstand.

## 0.47.1 – Startseite: Ausfall-Fallback und Umschalter KI | Original

* Bei ausgeschalteter, pausierter oder zuletzt fehlgeschlagener KI zeigt die Startseite einen Hinweis mit Grund und die
  neuesten Meldungen nach Aktualität (Analysen nur als Zusatz); `AiGateway.health()`.
* Im Normalbetrieb 2 von 5 Plätzen für die neuesten noch nicht analysierten Meldungen („KI ausstehend“).
* Umschalter **KI-Auswertung | Original** im Nachrichtenkasten, Wahl im Browser (`localStorage`).

## 0.47.0 – News-Intelligence: Rückstau, Startseite, Berichts-Fortschritt, längere Texte

Details in `docs/NEWS_INTELLIGENCE.md` (Abschnitt „0.47“).

* **Tagesbericht „Neu erzeugen“** mit Fortschrittsbalken (Phasen, 2-s-Abfrage, automatisches Neuladen), sichtbarer Fehlermeldung
  und eigenem Thread-Pool (`interactive`) – der Auftrag wartet nicht mehr hinter Import, Textabruf und Analyse.
* **Analyse-Rückstau** wird in Runden abgearbeitet (48-h-Fenster, Stapel zu 6, Priorität nach Positionsbezug,
  Mehrfachberichten, Aktualität; Laufzeitgrenze 8 min, Job alle 10 min); „Pipeline heute“ zeigt den Rückstau.
* **Gemini-Token-Tagesgrenze** ausdrücklich aufhebbar („unbegrenzt“); Anfragen pro Minute/Tag bleiben bestehen.
* **Ereignisgruppierung** ohne Riesenhaufen: Durchschnitts- statt Einfachverkettung, höchstens 30 Meldungen je
  Ereignis, einmaliger Neuaufbau des abgeleiteten Bestands (fixierte Ereignisse bleiben).
* **Startseite:** „Top-News mit KI-Auswertung“ (Tagesüberblick, Top-Ereignisse mit Stimmung, Bedeutung, Assets, Quellen);
  Rückfall auf die bisherige Liste ohne Analysen. Stimmungs-Badge auch in der Artikelliste.
* **Längere KI-Texte** (Prompt-Version 2: Zusammenfassung 4–5 Sätze, dreiteilige Einordnung, Marktüberblick 6–8 Sätze);
  Neuanalyse nur für die letzten 48 h. **Stimmungsbarometer** im Bericht (gesamt und je Asset, lokal berechnet).
* **Behoben:** verspätetes Lebenszeichen überschrieb den Abschluss eines Fortschritts (Anzeige „läuft“ blieb hängen).

## 0.46.0 – News-Intelligence: Leseansicht, Gemini, Analyse, Ereignisse, Tagesbericht

Details, Grenzen und Datenschutz in `docs/NEWS_INTELLIGENCE.md`.

* **Artikeltext (Stufe B)** als eigener Hintergrundjob `news_enrich` (Tabelle `news_content`, Migration 33): SSRF-sicherer
  Abruf mit Anpinnen der geprüften IP (DNS-Rebinding), Weiterleitungs-/Größen-/Zeitgrenzen, Extraktion mit `lxml`
  (JSON-LD, `<article>`, Absatzdichte), Status `success/partial/blocked/failed/pending`, Retry-Backoff, Host-Ruhezeiten,
  Google-News-Link-Auflösung (best effort). Bezahl-/Zustimmungsschranken werden **nicht** umgangen. Marktweite Meldungen
  werden mit angereichert; neue marktweite Quellen (tagesschau Wirtschaft, Fed, EZB) werden einmalig ergänzt.
* **Leseansicht** `/news/<id>`: Artikel, KI-Zusammenfassung und KI-Einordnung getrennt, weitere Berichte zum Ereignis,
  Original-Link separat, automatischer Lesestatus, Fallbacks bei fehlendem Text.
* **Google Gemini** neben Claude (REST, Schlüssel im Header, nur Flash-/Flash-Lite-Modelle mit Free-Tier, lokale
  RPM/TPM/RPD-/Token-Grenzen, kontrollierte Pause bei `RESOURCE_EXHAUSTED`, kein Modell-/Anbieterwechsel, Nutzung aus
  `usageMetadata`). `GEMINI_API_KEY` auch in Unraid-Template und Compose.
* **Einzelanalyse** (`news_analysis`): Fakten / zitierte Einschätzungen / KI-Einordnung / Unsicherheiten getrennt,
  strenge Validierung inkl. Zahlenbeleg, Hash-Idempotenz, Prompt-Injection-Schutz.
* **Ereignisgruppierung** lokal (Titel-Jaccard, Hash, Ereignistitel, Zahlenkonflikt-Sperre), KI nur für Grenzfälle,
  manuelle Korrektur („aus Ereignis lösen“, dauerhaft).
* **Täglicher Finanzmarktbericht** (Deutsch, Quellen, Historie, Versionen, Nachholen nach Neustart, Fehler überschreiben nie).
* **Überwachung** *News → Status*; CI-Smoke-Test für Bericht und Status.
* **Grenzen:** nicht gegen echte Gemini-/Google-News-/Verlagsantworten getestet; Free-Tier-Zahlen sind eigene Annahmen
  (Google veröffentlicht sie nur in AI Studio); Extraktion heuristisch, kein Headless-Browser.

## 0.45.2 – Anbindung entfernen nur mit Bestätigung

* „Entfernen“ einer Anbindung führt auf eine Bestätigungsseite (`/settings/datasources/<id>/entfernen`: was entfernt wird, was bleibt) mit Pflicht-Häkchen; `POST …/delete` ohne `confirm=1` wird abgelehnt. Gilt auch für „Datenquelle entfernen“ in der Konto-Entfernen-Vorschau.

## 0.45.1 – Konten-Hub: Abstände

* Konten- und Zugänge-Seite: Karten standen auf dem Handy ohne Abstand untereinander (Inhalt jetzt in `.stack`); zusätzliche Abstände für Gruppen, Kartenreihen und „Konto verwalten“.

## 0.45.0 – Konten-Hub: Datenquellen und Konten zusammengeführt

Details in `docs/KONTEN.md`.

* **Eine Seite `/accounts`** statt Datenquellen + Konten: Karten der Wallets, Börsen/Depots und weiteren Konten, „Konto verwalten …“ an jeder Karte (Stammdaten, Umbenennen, Zusammenführen, Entfernen, Löschen), Konto anlegen, **Konto-Details** `/accounts/detail`.
* **Eine Depotgruppe** für Konten, Positionen und Performance (ältere Wallet-Gruppen werden weiter angezeigt, Umbenennen wirkt auf beide, keine automatische Migration).
* **Zugänge** `/settings/zugaenge`: Master-Key und Anbieter-Schlüssel; `/settings/datasources` leitet auf `/accounts` weiter, Rücksprünge aller Aktionen führen auf die Konten-Seite. „Konten“ in der Hauptnavigation.

## 0.44.0 – Belege ansehen und herunterladen

* **Seite `/belege`** (`dkb_archive.py`, `archive_web.py`): Archiv der Bankdokumente auf Basis von `bank_doc` mit Filtern (Konto, Jahr, Art, Verknüpfung, Suche), Seitenweise-Anzeige, *Ansehen*, *Herunterladen* und ZIP der gesamten Auswahl mit `manifest.csv`.
* Auch Belege ohne Buchung und reine Ablageordner-Dateien sind sichtbar; Pfade nur aus dem Index (404 bei `..`/fehlender Datei); Download-Namen nie aus dem Originalnamen.
* Links: DKB-Seite, Konto-Detail der Datenquelle, Buchungsliste (Jahresfilter).

## 0.43.0 – DKB: ZIP-Import als Hauptweg, Postbox-Abruf ausgeblendet

* **Postbox-Abruf** (`portfolia-dkb-fetch`/`dkb-robo`) bleibt im Code, die Oberfläche erscheint nur mit `DKB_FETCHER=1`; ohne Flag 404 auf `/dkb/setup|remove|run|cancel`.
* **Seite DKB-Dokumente:** ZIP-Import zuerst („Hauptweg“), Ablageordner ohne Zugangsfelder, Steuervergleich verlinkt; gespeicherter Benutzername/Gerät bleiben beim Speichern erhalten.
* Konto-Detail: Knopf „ZIP-Import (Wertpapierdokumente)“ statt „DKB-Abruf einrichten“.

## 0.42.0 – DKB-Steuervergleich (Phase 7)

Details in `docs/DKB_DOCUMENTS.md`, Abschnitt „Steuervergleich DKB ↔ Portfolia“.

* **Steuerangaben** (`dkb_tax.py`) aus Verkaufs-/Ertragsabrechnungen: Ergebnis, Anschaffungsgeschäfte, Steuern netto, Verlusttöpfe Vorher/Nachher; Speicherung in `bank_doc.meta`.
* **Vergleich** (`dkb_taxcompare.py`, Seite *Steuervergleich*) je Verkauf, Ertrag und Jahr mit Schwellen 0,05/1/10 €, Ursachen und Lot-Auswahl; Portfolia-Seite über
  `GermanyPack.capital_for_accounts` (nur das DKB-Konto, `include_domestic`).
* **Vorbelegung** von Verlustvortrag und Zusammenveranlagung nur mit Bestätigung, serverseitig berechnet.
* **Keine Änderung** an Buchungen oder Steuereinstellungen durch die Anzeige.
* **Grenzen:** laufende (DKB) vs. Jahresend-Rechnung (Portfolia), Kapitalmaßnahmen-Steuern und Teilfreistellung nicht verglichen, kein Ersatz für die Steuerbescheinigung.

## 0.41.0 – DKB-Depotauszüge, Kosteninformationen, Order-Belege, Belege-Download (Phasen 5 und 6)

Details in `docs/DKB_DOCUMENTS.md`, Abschnitte „Depotauszüge“ und „Kosteninformationen und Order-Belege“.

* **Depotauszug** (`dkb_depot.py`): Positionen mit Rechenprobe (n Posten, Σ Kurswerte, ISIN-Prüfziffer) → Referenzbestände + Vergleich mit dem Bestand zum Stichtag;
  Depotentgelt (brutto, Valuta, USt-Probe) als Gebührenvorschlag bzw. Verknüpfung.
* **Kosteninformation** (`dkb_costinfo.py`): nie eine Buchung; Zuordnung Z0–Z6 zu vorhandenen Trades; Gebühr nur „geschätzt“, nur nach Bestätigung, nie gegen eine Abrechnung.
* **Order-Belege** (Ausführung/Bestätigung/Streichung) je Auftragsnummer, spätere Verknüpfung beim Buchen der Abrechnung.
* **Belege-Download** je Jahr als ZIP mit `manifest.csv` (`yearpack.py`).
* **Grenzen:** siehe Abschnitt „Grenzen“ der DKB-Dokumentation (kein Neu-Zuordnen später gebuchter Trades für Kosteninformationen; Vergleich nur so genau wie der Bestand).

## 0.40.0 – DKB-Kapitalmaßnahmen und Ausbuchungen (Phase 4)

Details in `docs/DKB_DOCUMENTS.md`, Abschnitt „Kapitalmaßnahmen und Ausbuchungen“.

* **Profil** (`dkb_capital.py`): Reverse Split/Split (Stufe 1 = Information, Stufe 2 = Buchung mit Verhältnis-Probe), Gratisausgabe mit Bewertungskurs und
  Steuer, Entgelt der Lagerstelle, wertlose und ersatzlose Ausbuchung (Verlust gegen die Anschaffungsgeschäfte geprüft), Storno, Namensänderung,
  Ankündigungen. Unbekannte Arten bleiben „ungeklärt“.
* **Storno-Regel:** Die aufgehobene Einbuchung wird nicht ausgewertet; genau ein Zugang bleibt. Bereits ausgewertete → Hinweis, nie stilles Zurücknehmen.
* **Zuordnung** zu vorhandenen Maßnahmen/Ausbuchungen/Entgelten mit Hinweis bei abweichender Menge; keine zweite Maßnahme.
* **Dokumentindex** `bank_doc.meta` (Migration 32). Klassifikation: Titel vor Fließtext (Chapter VII war als Liquidation erkannt).
* **Grenzen:** Fonds-/Anleihe-Maßnahmen, Umtausch/Fusion/Spin-off, Bezugsrechte nicht belegt (→ ungeklärt); Namensänderung wird nur angezeigt.

## 0.39.0 – DKB-Erträge: Rechenprobe, Quellensteuer-Regeln, Zuordnung (Phase 3)

Details in `docs/DKB_DOCUMENTS.md`, Abschnitt „Ertragsabrechnungen“.

* **Ertragsprofil** (`dkb_income.py`): Dividende, Kapitalrückzahlung, Liquidationszahlung. Gebucht werden EUR-Brutto, die *einbehaltene*
  Quellensteuer und KapESt/Soli/KiSt; „anrechenbare“, „rückforderbare“ und „verrechnete“ Beträge sind Information. Rechenprobe wie bei
  den Abrechnungen; an allen fünf Beispielbelegen bestanden.
* **Zuordnung** zu bereits erfassten Dividenden (Konto, Asset, ± 3 Tage, Betrag); sonst Vorschlag im Prüf-Stapel.
* **Fehlerbehebung:** Belege mit gleicher ISIN, Menge und gleichem Tag, aber verschiedener Abrechnungs-/Auftragsnummer wurden im Stapel zu
  einem Vorgang verschmolzen (Dividende + Kapitalrückzahlung am 19.03.). Die Kennung des Belegs entscheidet jetzt.
* **Bericht/Oberfläche:** eigener Abschnitt „Erträge“ auf der ZIP-Ergebnisseite; am DKB-Konto ein zweiter Knopf „ZIP-Import“.
* **Grenzen:** Kapitalrückzahlung/Liquidation werden als Ertrag erfasst (E3, mit Prüfhinweis); Fonds, Zinsen, Steuererstattungen auf Erträge
  nicht belegt; Kapitalmaßnahmen, Depotauszug, Kosteninformationen folgen.

## 0.38.0 – DKB-Abrechnungen: Rechenprobe, Zuordnung zu vorhandenen Buchungen, Ergänzen, Beleg-Link (Phase 2)

Details in `docs/DKB_DOCUMENTS.md`, Abschnitt „Kauf- und Verkaufsabrechnungen“.

* **DKB-Profil mit Rechenprobe** (`dkb_trade.py`): Kostensumme über alle Zeilen, Steuern (nur echte Steuerzeilen), mehrere
  *Ausmachender Betrag* je Block, Menge × Kurs ÷ Devisenkurs. Nicht bestanden → „ungeklärt“, nie buchbar. An allen Beispiel-
  Abrechnungen bestanden.
* **Zuordnung statt Neuanlage:** Eine Abrechnung gehört zu einer vorhandenen Buchung (Konto, Asset, Menge, ± 3 Tage) → verknüpft,
  keine zweite Buchung. An deinem Export: 5 von 9 Beispiel-Abrechnungen zugeordnet.
* **Ergänzen als Sammelaktion** mit Vorher/Nachher und Wirkungsvorschau; eine Entscheidung, als Ganzes rückgängig. Die bisherige Sperre
  für von Hand geänderte Buchungen ist einer Warnung mit ausdrücklicher Bestätigung gewichen (nur einzeln); Sparplan-Schätzungen
  bleiben gesperrt.
* **Beleg-Link** (Migration 31, `tx_document`): Beleg ↔ Buchung n:m, Anzeige in Buchungsliste und Positionsansicht.
* **Grenzen:** Erträge, Kapitalmaßnahmen, Depotauszug und Kosteninformationen noch nicht ausgewertet; Sparplan-Abrechnungen finden in
  geschätzten Sparplan-Buchungen keine Gegenstücke (Menge unterschiedlich) und kommen als eigene Vorschläge.

## 0.37.0 – DKB-Wertpapierdokumente per ZIP: erkennen und ablegen (Phase 1)

Details in `docs/DKB_DOCUMENTS.md`, Abschnitt „ZIP-Import der Wertpapierdokumente“. Grundlage ist die Analyse
`ANALYSE_DKB_Wertpapierdokumente_Import.md` (Phasen 1–7); diese Stufe bucht nichts.

* **ZIP-Upload auf der DKB-Datenquelle** (≤ 100 MB): sicheres Entpacken im Speicher (kein Zip-Slip, Bomben-/Eintrags-/Größengrenzen,
  verschlüsselt und verschachtelt abgelehnt), Ablage unter aus dem Inhalt gebautem Pfad, nie überschreiben, Dubletten per SHA-256.
* **Dedizierte DKB-Erkennung** (`app/documentimport/dkb.py`): 13 Dokumentarten samt Unterarten und Kennungen aus dem Inhalt; Dateiname
  nur Gegenprobe. An allen 47 Beispiel-PDFs der Analyse geprüft (Golden-Tests mit anonymisierten Nachbauten).
* **Hintergrundlauf** (`dkb_run.py`) mit Fortschrittsbalken, **geschätzter Restzeit** (`app/progress.py`: arbeitsgewichtet, geglättet,
  gemessene Startwerte), Abbrechen, Fortsetzen, Ergebnisseite und CSV-Bericht; Seite darf verlassen werden.
* **Grenzen:** Es wird nichts gebucht oder vorgeschlagen (folgt in Phase 2 ff.); Erkennung beruht auf 47 Beispielen – unbekannte
  Titel werden gezählt, nicht geraten; ein Depot je Datenquelle vorgesehen (mehrere werden gemeldet).

## 0.36.0 – DKB: Postbox per Knopfdruck abrufen, in Ordner ablegen, Depot-Abrechnungen auswerten

Details in `docs/DKB_DOCUMENTS.md`.

* **„DKB-Abruf einrichten“** erscheint an jedem DKB-Konto (Anbieter DKB oder Kontoname beginnt mit „DKB“, auch bestehende):
  installiert auf Knopfdruck das Abrufprogramm in eine eigene Python-Umgebung unter `/data/plugins/dkb` – **kein zweiter
  Container**. Das Programm (`contrib/portfolia-dkb-fetch`, **GPL-3.0**, nutzt dkb-robo 0.31.2) ist nicht Teil des
  MIT-Codes oder des Images, spricht mit Portfolia nur über JSON-Zeilen und Dateien.
* **Abruf per Knopfdruck:** Anmeldung, Freigabe in der DKB-App, alle Postbox-Dokumente nach
  `<Bankunterlagen>/<Datenquelle>/<Kategorie>/<Konto>/<Datum>_<Name>.pdf`; Index mit Prüfsumme (nie doppelt, nie überschrieben),
  Abbruch, Fortschritt. Passwort nur im Arbeitsspeicher, ein Anmeldeversuch je Klick, nichts als gelesen markiert.
* **Auswertung nur der Depot-Abrechnungen** (Wertpapier- und Ertragsabrechnung) über den vorhandenen Dokumentimport →
  Vorschläge im Prüfstapel; Kontoauszüge, Karten, Steuer u. a. werden nur abgelegt. Gebucht wird nichts ohne Prüfung.
* **Unraid:** neue Pfad-Zuordnung „Bankunterlagen“ (`/documents`, `DOCUMENTS_DIR`); die Seite zeigt, ob der Ordner
  beschreibbar und eine eigene Freigabe ist.
* **Grenzen:** gegen die echte DKB nicht getestet (Mock des Fetchers, echte Installation mit dkb-robo geprüft); die
  Textauswertung der DKB-Abrechnungen ist nicht an Originaldokumenten verifiziert – unbekannte Layouts landen als „ungeklärt“
  im Prüfstapel.

## 0.35.0 – Quellenübernahme: die API wird für ein Konto maßgeblich (Pilot Bitpanda)

Auf Frage („Wenn die API-Anbindung Vorrang hat, müssten sich Konflikte zum Koinly-Import doch einfach beheben lassen?“) –
Details in `docs/SOURCE_TAKEOVER.md`. Statt vieler Einzelkonflikte **eine Entscheidung je Konto** mit Beweis am Bestand.

* **Vollabruf (nur lesend):** alle Vorgänge ab Beginn der Historie plus beobachteter Bestand; kein Abrufstand, kein
  Prüf-Stapel, keine Buchung (`ds_takeover`, Migration 29).
* **Beweis:** Abruf vollständig (Seitenende `has_next_page=false`, keine Lücke, aktuelle Auswertung, ≤ 24 h) · alle
  Vorgänge buchbar und zugeordnet · die Vorgänge der API ergeben den **beobachteten Bestand je Asset exakt**
  (berechnet aus denselben Zeilen, die der Import bucht). Ohne Beweis ist die Übernahme gesperrt.
* **Vorschau:** ausgeblendete Buchungen, bleibende Buchungen (Transfers mit anderen Konten, manuelle Buchungen, Korrekturen),
  Bestand jetzt/nachher/beobachtet, Import-Buchungen **ohne** Gegenstück in der API und umgekehrt (Warnsignale).
* **Übernehmen:** Import-Buchungen des Kontos ausblenden (Korrektur der Diagnose), API-Vorgänge in einem eigenen Stapel buchen;
  EUR-Werte, Gebührenwerte und Einordnungen des Imports bleiben erhalten, **wo die API sie nicht liefert**. **Nachprüfung:**
  weicht der Bestand danach ab, wird alles zurückgenommen. **Rückgängig** für die Übernahme als Ganzes.
* **Grenzen:** nur Mengen bewiesen, nicht Werte; realisierte Ergebnisse je Jahr nicht vorab simuliert; Binance und Wallets
  ausgeschlossen (Abruf belegt keine Vollständigkeit); ein exakter Bestand schließt sich aufhebende Fehler nicht aus.

## 0.34.4 – Mobil: Fallseite und Definitionslisten laufen nicht mehr über den Rahmen

Ursache: Tabellen der Fallseite („Technik und Belege“: Dimensionen, Regeln, Kandidaten, Hypothesen, Quellen) standen ohne
Rahmen in der Karte; ihr Mindestmaß (bis 430 px) überragte die Karte und wurde abgeschnitten. Sie sind jetzt Kartentabellen
(mobil gestapelt, Text bricht um, Aktionen unten); auch die Tabelle des Geldwegs. Zusätzlich stapeln Definitionslisten
(`dl.kv`) unter 640 px Bezeichnung über Wert (Diagnose-Legende, Dateiformate im CSV-Import).
Prüfung: Browser-Lauf über 70–90 Seiten (Echtdaten-Kopie, Fälle, Diagnose, Prüfstapel, Einstellungen) bei 390 und 360 px:
kein abgeschnittenes bzw. überstehendes Element mehr; bewusst seitlich scrollende Tabellenrahmen (`table-wrap`: Positionen,
Steuer, Journal …) bleiben. Regressionstest: jede Tabelle der Fallseite ist gerahmt und eine Kartentabelle.

## 0.34.3 – Prüfstapel schrumpft: Prüflauf und „Neu abgleichen“ bewerten offene Stapel neu

Ursache für „erneute Prüfung verkleinert den Prüfstapel nicht“: der Prüflauf der Datenqualität rechnete nur Diagnose,
Integrität und Klärung; die Zeilen des Prüfstapels wurden erst beim **Öffnen eines Stapels** neu bewertet. Jetzt gleicht der
Prüflauf als erster Schritt alle offenen Stapel lokal neu ab (Hash, Dubletten, Transfers; ohne Abruf, ohne Buchung) und
meldet „offene Vorgänge vorher → nachher“; dasselbe als Knopf „Neu abgleichen“ in der Prüfansicht.

## 0.34.2 – Hash-Abgleich: Kontointerne Vorgänge und Zwillinge, Stapel werden neu bewertet

Auf Hinweis („gleiche Transaktion mit Hash im Import sollte automatisch verknüpft werden“): Umbuchungen an die eigene Adresse
zählen wie Commit/Reveal; identische Zwillinge im Import verdoppeln die Menge nicht mehr; `EVAL_VERSION` 8 bewertet offene
Stapel beim Öffnen neu. Prüfung am Export: 4.760/4.760 Kaspa-Buchungen mit Hash, nachgestellte Quellenzeilen zu 100 %
erkannt.

## 0.34.1 – KRC-20-Gebührenzeilen der Datenquelle nicht mehr im Prüfstapel

Der Abgleich erkennt Commit/Reveal-Zeilen (nur Gebühr) als vorhanden, wenn der Hash im Import (Abgang/Zugang) oder in der
Netto-Gebühr der Diagnose vorkommt; die Netto-Gebühr trägt beide Hashes. Siehe `docs/KRC20_MINT_ROUNDTRIP.md`.

## 0.34.0 – KRC-20-Mint im Koinly-Verlauf: Abgang + Rückfluss als Netzwerkgebühr

Auf Frage („Kann es sein, dass die Kaspa-Probleme durch das Minting entstanden sind?“) – Details in
`docs/KRC20_MINT_ROUNDTRIP.md`.

* **Ursache bestätigt:** Commit (Abgang an Skript-Adresse) und Reveal (Rest zurück) stehen im Koinly-Verlauf als fremder
  Abgang und neuer Zugang; Bestand stimmt, Einstand/Haltedauer/realisierte Ergebnisse nicht.
* **Neue Befundart** „Abgang mit Rückfluss auf demselben Konto (Netto-Gebühr)“: belegt (gleicher Hash) · wahrscheinlich
  (Muster ≥ 3) · Verdacht; Lösung „Als Netzwerkgebühr buchen“ mit Vorschau, Rückgängig, Sammelbearbeitung.
* **Automatik (opt-in):** Autopilot-Einstellung „Netzwerkgebühren … zusammenfassen“ (Standard aus), nie bei „Verdacht“;
  Autopilot prüft Befundgruppen getrennt.

## 0.33.0 – Datenqualität neu strukturiert: Arbeitsplan, Reiter, Prüflauf mit Fortschrittsbalken

Auf Wunsch (Screenshots: „relativ unübersichtlich“) – Details in `docs/DATA_QUALITY_STRUCTURE.md`.

* **Arbeitsplan** (`workplan.py`): alles Offene in Stufe A (muss ich tun) / B (kann ich tun) / C (nichts zu tun) mit
  Empfehlung, Warum, Wirkung, Aufwand und einem Primärknopf; ein hervorgehobener nächster Schritt; fehlender Nachweis ist
  höchstens Stufe B; ohne Prüflauf nie „fertig“.
* **Reiter** Heute · Fälle · Belege · Automatik auf allen Seiten der Datenqualität; alle alten Adressen bleiben als
  Unterseiten erreichbar; die elf Schaltflächen der Diagnose sind ersetzt. Neue Seiten `/quality/faelle`,
  `/quality/belege`, `/quality/automatik`.
* **Prüflauf „Alles prüfen“** (`checkrun.py`): Diagnose, Integritätsprüfung und Klärung in einem lokalen Hintergrundlauf
  (keine Abrufe) mit Phasenbalken und Bericht „neu/erledigt“. „Jetzt untersuchen“ der Klärung nutzt ihn.
* **Warte-Balken:** Prüflauf-Fortschritt im Seitenkopf, Arbeitsplan lädt nach (unbestimmter Balken), Klicks/Formulare der
  Datenqualität zeigen oben einen Balken.
* **Fall-Seite:** „Technik und Belege“ eingeklappt (Dimensionen, Regelprüfung, Dossier, Kandidaten); Tausch im selben
  Konto, Sammelbroker, lange Fälle (siehe 0.32.0).
* **Brücken:** Hash der Gegenbuchung in der Notiz = konkreter Bridge-Beleg → Kandidat „mittel“, nie automatisch.
* Kleinigkeiten: Checkbox-Text bricht neben dem Kästchen um; Abdeckung unterscheidet „Dateispanne“ von belegter Lieferung.

## 0.32.0 – Visuelle Fallklärung mit benannten Konten und Wallets (lokal, nicht veröffentlicht)

Umsetzung des Auftrags „Visuelle Fallklärung“ ohne neue Plattform: Darstellung auf Basis des vorhandenen Falls, der
gemeinsamen Simulation und der Quellenanhänge – Details in `docs/VISUAL_CASE_CLARIFICATION.md`.

* **Vorarbeit (Fehlerbilder, mit Tests):** Transferpaare nur mit belegter Identität; beide Transfergebühren bleiben
  erhalten; geänderte Quellenzeile = offener Revisionskonflikt; Datenstand umfasst Nachweise/Zuordnungen/Einstellungen und
  wird in der Schreibtransaktion geprüft.
* **Geldweg** (`case_view.py`, `klaerung_weg.html`): benannte Orte als Anker, Verbindungen mit Zustand (gebucht ·
  Vorschlag · entfällt · offen) als Linienart *und* Text, Details/Begründung, Legende, Tabelle als Textalternative,
  Datenschutz-Maskierung, Mobil/Dunkelmodus, ohne Skript bedienbar.
* **Bestandsfenster im Fall:** fallrelevante Fenster (`devwindows`), Recherche-Plan als gemeinsames Partial mit Rücksprung;
  Seitenaufruf ohne Schreibzugriff (hängende Recherche-Läufe nur in der Anzeige).
* **Simulation & Wirkung:** Auswahl der Hypothese, Wirkung aus der gemeinsamen Simulation (Bestand, Steuer, Anschaffung,
  Kursstand), Übernahme/Nachprüfung/Rückgängig über die vorhandene Ausführung.
* **Abdeckung:** Dateispanne (erster–letzter Vorgang) gilt nicht mehr als vollständige Lieferung.
* **Nicht Teil:** kein Deployment, kein Push, keine Migration, keine produktiven Abrufe; Automatik und externe KI nicht
  neu eingeschaltet.

## 0.31.3 – DOT: Grenzsuche überlebt den Abbruch („Zeitbudget erreicht ohne Fortschritt“)

Beobachtet (Screenshot, DOT nach 0.31.2): „Zeitbudget des Laufs erreicht ohne Fortschritt“ – die Suche nach dem frühesten
abrufbaren Block brauchte zusammen mit den Fehlversuchen der Fensterhalbierung mehr Anfragen, als in einen Lauf passten,
und verlor bei jedem Abbruch ihren Stand.

* **Weniger Anfragen:** Wird der Gesamtbereich abgelehnt, prüft der Abruf zuerst das kleinste Fenster am Anfang. Wird auch
  das abgelehnt, ist es eine Rückblick-Grenze → direkt zur Grenzsuche (spart die rund 14 Halbierungsversuche); nur bei
  einer Breitengrenze wird halbiert.
* **Fortschritt statt Fehler:** Gelerntes (Fensterbreite, Grenzsuche) zählt als Fortschritt. Die Suchgrenzen (`probe`), der
  erste abrufbare Block (`from`) und die Breite (`win`) stehen im Abrufstand, auch nach einem Abbruch. Der nächste Lauf
  setzt die Suche fort (die Grenze wandert mit der Spitze, daher wird der gemerkte Stand erst bestätigt). Ist die Grenze
  gefunden, rückt der Abrufstand dorthin vor; das davor ist als Lücke erklärt.
* **Test:** Anfragebudget so klein, dass die Suche im ersten Lauf abbricht → kein Fehler, `probe` im Abrufstand, spätere Läufe
  buchen den Verlauf ab der Grenze (blockgenau).
* **Grenzen:** Gegen den echten Dienst weiter ungeprüft; bei sehr langsamer Antwortzeit (mehrere Sekunden je Anfrage)
  braucht die Suche mehrere Läufe.

## 0.31.2 – DOT/peaq: Rückblick-Grenze der kostenlosen PubFi-Route – jüngsten Verlauf abrufen

Beobachtet (Screenshot, DOT nach 0.31.1): Auch das kleinste Blockfenster am Anfang der Kette wurde mit
„history_window_exceeded“ abgelehnt; der Sync lief nur noch im Rückfall (Bestand laut RPC 30,668 DOT, 0 Vorgänge), die
Meldung stand doppelt (Warnung und Lücke).

* **Folgerung:** Die Route begrenzt offenbar den **Rückblick**, nicht nur die Fensterbreite (alte Blöcke werden auch in
  kleinen Fenstern abgelehnt). Genaues Limit weiterhin nicht dokumentiert.
* **Neu:** Scheitert das kleinste Fenster, sucht der Abruf den **frühesten abrufbaren Block** (Halbierung, blockgenau, rund
  25 Anfragen je Netz) und ruft ab dort den Verlauf ab. Der ältere Teil ist eine ausgewiesene Lücke („Verlauf vor Block X
  nicht abrufbar“), im Abrufstand gemerkt (`from`) und bei jedem Lauf wieder genannt. Nichts davor wird erfunden. Wird auch
  das jüngste Fenster abgelehnt, bleibt es beim Rückfall (Bestand laut RPC, Lücke).
* **Meldung:** Rückfall ohne Doppelung (Kurzhinweis als Warnung, Begründung als Lücke).
* **Grenzen:** Mit Rückblick-Grenze sind ältere Vorgänge über die kostenlose Route nicht erreichbar – sie kommen nur aus
  dem kuratierten Import, einem CSV-/Koinly-Export oder einem Subscan-Schlüssel. Ein Breitenlimit unter 2.000 Blöcken wird
  nicht gesondert behandelt (dann Rückfall). Die Weboberfläche peaq.subscan.io ist keine Schnittstelle: ihre Web-API
  blockiert Programme (HTTP 403), die offizielle API verlangt einen Schlüssel.

## 0.31.1 – DOT/peaq: Verlaufsfenster der kostenlosen PubFi-Route; Robinhood Chain

Beobachtet (Screenshots): Nach 0.31.0 meldeten DOT und peaq statt „row_limit_exceeded“ nun HTTP 403
„history_window_exceeded“ – bereits bei `row=1` der Überweisungsliste. Außerdem fehlte Robinhood Chain.

* **Ursache (belegt, soweit prüfbar):** Der Abruf fragte den gesamten Verlauf (`block_range` 0 bis Spitze) in einer
  Anfrage ab; die kostenlose Route lehnt zu große Bereiche ab. Die genaue Grenze (Breite oder Rückblick) ist in der
  PubFi-Dokumentation und der OpenAPI-Beschreibung **nicht** angegeben und ohne eigenen Schlüssel nicht prüfbar.
* **Fix – Blockfenster:** Neue Fehlerart `history_window`. Der Abruf läuft in Fenstern (`_scan`/`_window`): ein Fenster
  gilt ganz oder gar nicht (Überweisungen, Extrinsics, Rewards), der fortlaufende Stand rückt pro Fenster vor, ein
  Abbruch (Zeitbudget) verliert keine fertigen Fenster. Meldet die Route das Limit, halbiert sich die Breite bis zum
  zulässigen Wert (Untergrenze 2.000 Blöcke); sie wird je Prozess **und im Abrufstand** (`win`) gemerkt. Ohne Limit bleibt
  es ein einziges Fenster (Verhalten wie zuvor).
* **Rückblick-Grenze:** Scheitert schon das kleinste Fenster, ist das keine Breiten-, sondern eine Rückblick-Grenze. Dann
  kein Fehler und kein erfundener Verlauf: Bestand laut öffentlichem RPC (DOT: `System.Account`; peaq-0x:
  `eth_getBalance`) als Ist-Bestand, die fehlende Historie als ausgewiesene Lücke mit Weg zur Ergänzung (CSV-/Koinly-Import
  bzw. Subscan-Schlüssel).
* **Verbindungsprüfung:** Ohne Gesamtverlauf prüft sie ein Fenster der letzten Blöcke (2.000 → 500 → 100).
* **Robinhood Chain** (Chain-ID 4663, ETH, Aktien-Tokens als ERC-20): neuer Connector über die Etherscan-kompatible
  Schnittstelle des Blockscout-Explorers (ohne Key); Etherscan V2 (nur Bezahlplan) und Routescan (nicht geführt) scheiden
  aus. Antwortet der Explorer nicht (z. B. Cloudflare-Prüfseite aus Rechenzentren), bleibt der ETH-Bestand laut RPC.
  Adressprüfung, Token-Kennung `…@ROBINHOOD:0x…`, Auswahl in der Oberfläche.
* **Recherche DOT-Alternativen:** Statescan (`polkadot-api.statescan.io`, ohne Key) liefert Überweisungen der Relay
  Chain, aber keine Staking-Rewards; die Asset-Hub-Schnittstelle (hier liegen DOT seit 04.11.2025) war nicht belegbar.
  Subscan direkt verlangt einen Schlüssel („Unauthenticated access is disabled“). Daher **nicht** umgesetzt.
* **Tests:** Fensterhalbierung und Merken (Prozess/Abrufstand), Rückblick-Grenze mit Bestand und Lücke (DOT und peaq),
  Prüfung ohne Gesamtverlauf, Robinhood (Abruf, Bot-Schutzseite → RPC-Bestand).
* **Grenzen:** Die Fensterlogik ist gegen ein simuliertes Limit getestet, nicht gegen den echten Dienst. Bei sehr
  kleinem zulässigen Fenster dauert der Erstabruf wegen 2 Anfragen/s lange (Fortsetzung über die Etappen). Robinhood-
  Blockscout ist von hier nicht erreichbar gewesen (Prüfseite) – der Abruf ist nur gegen Fixtures geprüft; RPC antwortet.

## 0.31.0 – Chain-Sync dauerhaft (PulseChain, peaq, Polkadot), Bestandsabweichungen erklären, Kontenverwaltung

Beobachtet (Screenshots + realer Export, lokal ausgewertet, nie im Git): DOT „403 row_limit_exceeded“, PulseChain
„Blockscout 502“, peaq „Adresse bei Subscan unbekannt“; Bestandsabweichungen bei XEN, MEAT, PLS, TH, KET.

* **PulseChain → Blockscout REST v2** (`blockscout_v2.py`): Die Etherscan-kompatiblen Listen hängen (502) bzw. liefern
  Token-Transfers nur ungefiltert und endlos. v2 paginiert seitenweise (50, neueste zuerst, `next_page_params`),
  Token-Transfers je Richtung (`filter=to|from`), Zeitzeiger-Cursor mit Sicherheitsabstand (600 s) und Überlappung
  (3600 s), Dubletten fängt der Ereignisschlüssel. Fallback auf die alte Schnittstelle bleibt. Interne Gutschriften ohne
  Spur (Delta aus `coin-balance-history`) werden als eigene Zeile mit Hinweis erzeugt. Gegen die Live-API geprüft:
  Bestand stimmt exakt.
* **peaq-EVM-Adresse:** Die 0x-Adresse wird per Frontier-Zuordnung (`blake2b-256("evm:"‖H160)`, SS58-Präfix 1221) auf das
  Substrate-Konto abgebildet (on-chain belegt: `System.Account` == `eth_getBalance`). Unbekannt bei Subscan →
  Suche über das berechnete Konto, sonst verständliche Meldung statt Fehler.
* **Polkadot (PubFi-Gateway):** Neue Fehlerart `row_limit`; die Zeilenzahl je Anfrage fällt adaptiv (100→50→20→10→5→1),
  der Wert wird prozessweit gemerkt. Der Bestand kommt zusätzlich per Substrate-RPC (`System.Account`, frei+reserviert),
  damit die Prüfung nicht an der Historie scheitert.
* **Abweichungsanalyse** (`engine`, `recommend`, `clarify`): XEN = Fork-Kopie XEN@PLS (verschwindet mit dem v2-Sync);
  MEAT = Doppelzugang derselben Transaktion (Dublette, Ausblenden belegt); PLS = interne Gutschrift ohne Spur
  (Ursache benannt); TH = Zwillings-Asset T/TH → Empfehlung „Assets zusammenführen“ mit Direktlink, **nie automatisch**;
  KET = Rundung (Toleranz relativ 10⁻⁹ in der Klärung).
* **Export:** `ds_balances` (letzte Bestandswerte der Datenquellen) und `account_meta` sind Teil von `state.json`
  (Export + Restore); damit lassen sich Abweichungen künftig offline aus dem Export analysieren.
* **Hochstufung „wahrscheinlich → belegt“** bei belegter Herkunftskette: bereits seit 0.30.0 aktiv (bestätigt).
* **Kontenverwaltung `/accounts`** (Migration 28, Überlagerung `account_meta`): Konto vorab anlegen, Broker/Depotgruppe/
  Notiz überlagern den Import, Umbenennen über Zusammenführen (Vorschau, Prüfsumme, rückgängig), leeres selbst
  angelegtes Konto löschen. Konten mit Buchungen nur über „Entfernen“ (reversibel). Mobil (390 px) geprüft.
* **Grenzen:** Die PubFi-Row-Limit-Hypothese ist nur mit einem echten Schlüssel prüfbar (nicht bestätigt). Statescan/
  Asset-Hub als Zusatzquelle für DOT-Historie nicht umgesetzt. Ein neuer Sync ist nötig, damit `ds_balances` gefüllt wird.
  RPEPE-Statistik gestrichen. Zwillings-Assets werden nie automatisch zusammengeführt.

## 0.30.2 – KAS-Sync: KRC-20 „prev“-Paginierung ohne Dauerstillstand (vormals als 0.29.4 geplant)

Beobachtet: „Letzter Abruf … KRC-20 … Stillstand erkannt“ bei jedem Lauf, obwohl nichts fehlte.

* **Ursache:** Der Kasplex-Indexer füllt eine `oplist?address=&prev=`-Seite, die weniger als 50 neue Operationen hat, mit den
  *ältesten* Operationen der Adresse auf (Wrap-around). Die Seite war damit „voll“, wurde als „es kommt mehr“ gelesen,
  setzte den Zeiger auf die Auffüller zurück und lieferte danach wieder dieselbe Seite → Stillstandsmeldung.
* **Fix:** Nur Operationen **über** dem Zeiger zählen als neu. Enthält eine Seite Auffüller, ist sie die letzte; der
  Zeiger rückt nur auf echt Neues, nichts wird erneut verarbeitet. Eine Seite mit 50 echt neuen Operationen läuft
  weiter wie bisher; die Stillstandserkennung für wirklich festhängende Seiten bleibt als Sicherung bestehen.
* **Tests:** Fake-Indexer mit Wrap-around (`FakeKaspa.wrap_prev`); drei Fälle (Neues + Auffüller, nur Auffüller, mehrere
  volle neue Seiten) schlagen ohne den Fix fehl.
* **Grenzen:** Setzt voraus, dass `opScore` je Adresse streng aufsteigend vergeben wird (laut Indexer-Verhalten der Fall).
  Ein bereits gespeicherter, fehlerhaft zurückgesetzter Zeiger wird nicht rückwirkend korrigiert – der nächste Lauf
  bringt ihn über die neuen Operationen wieder auf den höchsten Stand; Dubletten verhindert der Ereignisschlüssel.

## 0.30.1 – Buchungsformular: Konto/Asset wählbar; Diagnose mobil: Zahlen brechen um

Beobachtet (Screenshots): Im Formular „Neue Buchung“ musste man Konto und Asset von Hand eintippen (die Browser-Liste
war auf dem Handy nicht auffindbar); in der Diagnose liefen lange Mengen (z. B. 0,011404081487101903) aus der Karte.

* **Auswahl statt Eintippen:** Konto- und Asset-Felder zeigen beim Antippen eine Liste der vorhandenen Werte (▾-Knopf,
  filtert beim Tippen; Asset als „ID Name“). Ein unbekanntes Konto wird einfach eingetippt („als neues Konto verwenden“ –
  es entsteht mit der ersten Buchung). Ein fehlendes Asset: „＋ Neues Asset anlegen …“ führt zur Asset-Seite und danach
  zurück ins Formular – **alle bereits erfassten Angaben bleiben erhalten** und das neue Asset ist vorgewählt
  (`PREFILL_FIELDS`, `pickfor`). In der Bearbeitung einer vorhandenen Buchung wird dieser Weg nicht angeboten (Eingaben
  gingen verloren). Ohne JavaScript bleibt das bisherige Textfeld mit Vorschlägen.
* **Diagnose:** Die Regel `.dev-table td.num { white-space: nowrap }` überstimmte das Karten-Layout; auf schmalen
  Bildschirmen brechen Mengen jetzt innerhalb der Karte um (im Browser bei 390 px geprüft, kein Überlauf).
* **Grenzen:** Die Liste zeigt höchstens 120 Treffer (Filter eingrenzen); Konten stammen aus Buchungen und Datenquellen,
  nicht aus einer eigenen Kontenverwaltung.

## 0.30.0 – Abweichungen selbst erklären: Ausbuchungen, Herkunftskette (Bridge), Erledigt-Bereiche

Beobachtet (Screenshots): Ausgebuchte Coins (Spam, Totalverlust) standen weiter als „Bestand weicht von der externen
Quelle ab“ in der Aufgabenliste; POPKAT (Kauf auf ETH → Brücke/Transfer → Kaspa) war als „wahrscheinlich doppelt“ nicht
lösbar; Erledigtes blieb in der oberen Ansicht.

* **Ausbuchung erklärt die Abweichung** (`engine._holdings`): Zeigt die Wallet/Börse genau die als Verlust, Diebstahl
  bzw. Burn ausgebuchte Menge weiter an (berechnet 0, beobachtet = Ausbuchung, Toleranz 10⁻⁶ relativ), ist das erwartet.
  Status „ausgebucht – Quelle zeigt die Menge weiter“, **keine Aufgabe**, keine Ausgleichsbuchung. Mehr oder weniger als
  ausgebucht, andere Abgangsarten (z. B. Bezahlung) und Ausbuchungen **nach** dem Abruf erklären nichts. Gemessen am realen
  Export (nur lesend, temporäre Datenbank): BEAR, KPER, MRBEAST, RPEPE, POPKAT folgen diesem Muster.
* **Herkunftskette** (`engine._supply_chain`): Stammt der gesamte Bestand des Quellkontos vor einem Transfer aus gebuchten
  Zugängen und entspricht er genau Transfermenge + Gebühr (Rest nur Staub), hat eine zweite, gleich große Gutschrift am Ziel
  keine Herkunft. Der Befund „Gleiche Menge zweimal gutgeschrieben“ nennt die Käufe und wird – bei 14 identischen
  signifikanten Stellen, manuell erfasster Gutschrift ohne Hash und belegtem Transfer – **belegt** (vorher „wahrscheinlich“).
* **Folgeanpassung der Ausbuchung:** Enthielt die Ausbuchung die doppelte Menge mit, ließ das Ausblenden den Bestand negativ
  werden (Klärung „blockiert“). Neue Lösung `hide_weak_adjust`: Doppelung ausblenden **und** die Ausbuchung auf die tatsächliche
  Menge kürzen (alte ausblenden, gekürzte neu buchen). Gemeinsam rückgängig zu machen; die Klärung entscheidet „Herkunft belegt“ →
  übernehmen; Autopilot/Klärung zählen den Plan als sicher (nur Ausblenden + gleichartige gekürzte Ausbuchung).
* **Erledigt-Bereiche:** Diagnose: aufklappbarer Block „Erledigt“ (automatisch erklärte Positionen mit Link zur Ausbuchung bzw.
  deren Rücknahme, übernommene Korrekturen mit „Rückgängig“; „Als geprüft markiert“ bleibt). Klärung: „Erledigt – geklärt“ ist
  auf- und zuklappbar.
* **Grenzen:** Abweichungen ohne Ausbuchung bzw. Beleg (XEN, MEAT, PLS, TH im Beispiel) bleiben Aufgaben – sie lassen sich nur
  mit dem aktuellen Stand der Datenquellen (beobachtete Bestände, offene Vorgänge im Prüf-Stapel) beurteilen; der Voll-Export
  enthält diese nicht. Die Herkunftskette gilt nur für eine Quelle (Transfer von einem Konto); mehrstufige Brücken ohne
  Zwischenbuchung sind nicht abgedeckt.

## 0.29.3 – Prüf-Stapel: gleich große Vorgänge zuordnen, klare Empfehlungen, Rücksprung

Beobachtet (Screenshot): Zwei gleich große Einzahlungen der Börse (10:47 und 10:48) standen im Bestand zwei Buchungen
der Steuersoftware (10:46 und 10:47) gegenüber – beide Zeilen als „Komplexe Zuordnung, mittel“ ohne Lösung, obwohl zwei
Kennungen und zwei korrekte Buchungen vorlagen.

* **Ursache:** Beide Zeilen passten (gleiche Assets, Menge, Zeit ≤ 10 min) auf *beide* Buchungen. Die Schutzregel gegen
  das Verknüpfen zweier echter Vorgänge mit einer Buchung (0.2x, AP3) stufte jede Zeile pauschal als „komplex“ ein –
  auch dann, wenn eine eindeutige 1:1-Zuordnung existiert.
* **Zuordnung statt Pauschalurteil** (`assess._competing`): Zeilen, die sich Kandidaten teilen, bilden eine Gruppe.
  Kostenminimale Zuordnung nach Zeitabstand (ungarisches Verfahren, deterministisch, unabhängig von der Zeilenfolge).
  * *Zeilen = freie Buchungen:* jede Zeile gilt wie eine einzelne Dublette/Ergänzung (Sicherheit „hoch“, Gruppenhinweis,
    im Vergleich „zugeordnet“). Wirtschaftlich gleichwertig, weil sich die Zeilen nur im Zeitpunkt unterscheiden.
  * *mehr Zeilen als Buchungen:* bleibt Einzelentscheidung (nie automatisch), mit Vorschlag nach Zeitabstand.
  * *mehr Buchungen als Zeilen:* nicht eindeutig → einzeln prüfen.
  * Bereits mit einem anderen Datensatz verknüpfte Buchungen sind keine Kandidaten mehr; eine Zeile, deren ähnliche
    Buchungen alle vergeben sind, gilt als eigener Vorgang (Sicherheit „mittel“, nie vorausgewählt).
* **Empfehlungen** (`assess.recommend`): je offener Zeile eine Überschrift und Begründung in einfacher Sprache („Mit X
  verknüpfen – dieselbe Einzahlung ist schon gebucht“, „Als neue Buchung übernehmen“, „Bitte prüfen – nicht eindeutig“)
  und ein Knopf, der sie per Einzelaktion ausführt (Protokoll und Rückgängig wie bisher). Technische Hinweise und die
  übrigen Auswahlfelder stehen unter „Technische Hinweise“ bzw. „Andere Entscheidung“.
* **Ablauf:** Nach einer Einzelaktion bleibt die Prüfseite an der Zeilenliste (nicht am Seitenanfang); sind alle
  Vorgänge geklärt, geht es zurück zur Übersicht der Börsen und Wallets (bei CSV-Dateien zur Importliste). Oben auf der
  Prüfseite steht ein fester Link dorthin.
* **Grenzen:** Die Zuordnung bei gleich großen Vorgängen ist eine Annahme über die Zeitnähe; weicht die tatsächliche
  Zuordnung ab, bleibt die Summe der Bestände identisch, nur Zeitpunkt/Kennung hängen an der anderen Buchung (im
  Verlauf rückgängig zu machen). Stapelaktionen („Vorschläge ausführen“) verhalten sich unverändert; die Rückkehr zur
  Übersicht gilt für Einzelaktionen und „dauerhaft ignorieren“.

## 0.29.2 – Konten zusammenführen: Gruppensortierung, Konto entfernen

Beobachtet (Screenshot): Im Feld „Konto, das bleibt (Ziel)“ ließen sich gleichnamige Positionen nicht unterscheiden;
Konten/Wallets/Börsen ließen sich nicht entfernen; die Vorschlagsliste auf der Zusammenführungsseite war überflüssig
(die Empfehlung bei „Konten ohne Datenquelle“ genügt).

* **Auswahl:** Alias und Ziel sind nach Gruppen sortiert (`Wallet-Gruppe …`, `Wallets`, `Börsen`, `Konten ohne
  Datenquelle`), innerhalb natürlich (Zahlen numerisch). Einträge nennen Buchungszahl, Datenquelle, Anbieter und
  Kurzadresse. Die Vorschlagsliste der Seite entfällt; es bleibt ein Hinweis auf die Karten bei den Datenquellen.
* **Konto entfernen** (`/accounts/remove/preview` → Bestätigung → `…/apply`, Rücknahme `…/{id}/revert`): umkehrbare
  Überlagerung über Buchungskennungen (Tabelle `account_remove`, Migration 27). Rohdaten und Importe bleiben unverändert;
  die Buchungen werden nur aus dem wirksamen Bestand ausgeblendet. Spätere Buchungen gleichen Kontonamens bleiben
  sichtbar. Vorschau mit Herkunft, betroffenen Beständen, FIFO-/Steuerjahren, Befunden vorher/nachher; Bestätigung nur
  mit Fingerabdruck (Zustand zwischen Vorschau und Bestätigung geändert → abgelehnt); Verlauf mit Rücknahme.
* **Sperren:** Konten mit Transfers zu anderen Konten werden nicht entfernt (sie würden Bestände dort verfälschen);
  erst Transfer klären oder zusammenführen.
* **Datenquellen:** Wallet/Börse (Datenquelle) wird mit der bestehenden Funktion „Entfernen“ gelöscht; die Vorschau
  verweist darauf. Die Entfernung des Kontos löscht eine Datenquelle nie still.
* **Export/Restore:** Entfernungen sind Teil des Voll-Exports (`account_removals`); der Export enthält nur den
  wirksamen Bestand, daher werden sie nach Restore als „aus Wiederherstellung“ geführt (nicht mehr rücknehmbar).
* **Grenzen:** Entfernen ist eine Ausblendung, keine Löschung; nach Restore nicht rücknehmbar; nur synthetische Tests.

## 0.29.1 – Konten zusammenführen: Datenquellen-Konten, Adresserkennung, Auswahl

Beobachtet (Screenshots): Das Konto „Ledger (ADA)“ einer Wallet-Datenquelle fehlte in der Auswahl; „Cardano (ADA) -
addr1q...47“ wurde nicht als dasselbe Konto erkannt; fast alle gebuchten Wallet-/Börsenkonten standen unter „Konten ohne
Datenquelle“, obwohl die Datenquelle schon angelegt war; im Feld „Konto, das bleibt (Ziel)“ erschien das gewählte Alias.

* **Ursache 1:** Die Auswahl und die Vorschläge kannten nur Konten mit Buchungen. Eine frisch angelegte Datenquelle hat
  aber noch keine. Jetzt zählen die Konten aller Datenquellen mit (`known_accounts`); die Vorschau erlaubt sie als Ziel.
* **Adresserkennung:** Gekürzte Adressen im Kontonamen (`addr1q...47`, `0x20B7...09`, `0x375A.../5Fq31...`) und
  vollständige Adressen werden mit den Adressen der Datenquellen verglichen (Anfang + Ende; nur der Anfang wird als
  schwächerer Beleg gekennzeichnet; EVM/Bech32 ohne Beachtung der Schreibweise). Treffer erscheinen als „Adresse passt“.
  Schwächer: gleiche Chain (nur das native Asset gebucht, genau eine Datenquelle dieser Chain) – ausdrücklich „nicht über
  die Adresse belegt“; bei mehreren Quellen derselben Chain gibt es keinen Vorschlag.
* **Richtung:** Hat genau ein Konto eine Datenquelle, bleibt es als Ziel; das gebuchte Konto ohne Quelle ist der Alias.
* **Konten ohne Datenquelle:** Jede Karte zeigt passende Datenquellen samt Beleg und einen Knopf „Mit … zusammenführen“
  (Vorschau, nichts wird gelöscht). Die Kopfzeile nennt die Zahl der Konten mit Vorschlag.
* **Auswahl:** Alias und Ziel schließen sich gegenseitig aus (serverseitig beim Vorbelegen, im Browser beim Wählen);
  Einträge zeigen Buchungszahl bzw. Datenquelle.
* **Grenzen:** Nur Vorschläge – die Entscheidung bleibt beim Nutzer. Konten ohne Adresse im Namen und ohne eindeutige
  Chain-Zuordnung (z. B. „Nova Wallet“) werden nur über Namensähnlichkeit gefunden; sonst manuell wählen.

## 0.29.0 – Konfliktklärung mit gezielter Bestandsbestätigung

Ziel: Portfolia untersucht Konflikte selbst, führt vorhandene Belege zusammen, simuliert Lösungen **gemeinsam** und
übernimmt nur eindeutig Belegtes. Der Nutzer wird gezielt nach genau der Information gefragt, die fehlt – nie mit dem
Verweis auf eine allgemeinere Seite. Neue Module: `app/diagnosis/clarify.py` (Kern), `stockconfirm.py`
(Bestandsbestätigung), `derivatives.py` (Derivate), `clarify_web.py` (Oberfläche `/quality/klaerung`). Migration 26.

**Gap-Analyse (Stand 0.28.3, am Code geprüft)**

| Bereich | vorhanden | erweitert | fehlte |
|---|---|---|---|
| Befunde/Empfehlungen/Pläne | `engine`, `audit`, `recommend`, `actions.build_plan` | genutzt, keine zweite Engine | Zusammenfassung abhängiger Befunde |
| Simulation | `hypothetical` + `run_ledger` + `diagnose` je Einzelplan (`preview`) | gleiche Bausteine für Kombinationen | gemeinsame Bewertung von Kombinationen, Gleichwertigkeit |
| Ausführung | `bulk.execute/undo`, Entscheidungen mit `bulk`-Kennung | gleiche Mechanik (`kl…`-Kennung) | Nachprüfung mit automatischer Rücknahme, Idempotenz je Gruppe |
| Automatik | `autopilot.*` (Standard aus) | gilt weiter (`enabled` + `fixes`) | Klärung nach Autopilot, Folgelauf |
| Nachweise | Referenzbestände mit Zeitpunkt/Zeitzone | Umfang, Unterkonto, gebundene Menge, Margin/Ergebnis (nur Angaben), zusammengefasste Konten | gezielte Frage, Bestätigen+Übernehmen |
| Recherche | `research.run` (lesend, Staging, idempotent) | gezielt je Fall (Konto, Asset), Schutzregeln beachtet | Auslösung durch einen Fall, Blockaden als konkrete Aussage |
| Derivate | Kennzeichnungen im Ledger (`derivative`-Lots/-Abgänge, nicht in `REALIZED_KINDS`) | wirtschaftliches Ergebnis getrennt ausgewiesen | Ausweis, Margin-Umbuchung als Kandidat, offene Vorgänge für übersprungene Zeilen |

**Ablauf je Klärungsgruppe:** Erkennen (Gruppen über gemeinsame Buchung/Position; Sammelbefunde zählen über ihre
Einzelvorgänge) → Dossier (Befunde, Zusammensetzung des Bestands je Position, Derivatebuchungen, Nachweise) →
Kandidaten (nur Lösungen der vorhandenen Empfehlung ohne freie Eingabe; Paare bei mehrteiligen Vorgängen nur, wenn die
Zuordnung eindeutig ist) → gemeinsame Simulation aller widerspruchsfreien Kombinationen (Bestände, Lots mit
Anschaffungsdaten, Gebühren, Zahlungsströme, Ergebnisse je Jahr, Ledger-Probleme, neue Befunde, Zwischen- und
Endbestände aller Nachweise) → Entscheidung → atomare Übernahme → Nachprüfung → Folgelauf.

**Beleglage der Kandidaten:** *Identität* (gemeinsamer Hash bzw. Quellreferenz), *rechnerisch exakt erklärt* (Brutto −
Gebühr = Netto, Summe der Teile = Sammelbuchung; keine Identität), *nur Betrag/Zeit ähnlich* (Suchhinweis). Eine
Differenz ohne dokumentierte Ursache (z. B. XT: 260,86516521 − 255,78917 = 5,07599521) ist nie ein Beleg.

**Automatische Übernahme nur, wenn alles zutrifft:** (1) Identität belegt – oder rechnerisch erklärt **und** genau eine
Deutung erfüllt einen Bestandsnachweis (und die Ausgangslage nicht); (2) keine konkurrierende zulässige Deutung
derselben Buchungen (Varianten derselben Deutung, z. B. welche Seite bleibt, entscheidet die vorhandene
Quellenpriorität nur bei vollständig gleicher wirtschaftlicher Wirkung); (3) gemeinsame Simulation ohne neuen
negativen Bestand, neues Ledger-Problem, neuen Befund oder widersprüchlichen Zwischenbestand; (4) Plan nur aus
Verknüpfen/Ausblenden vorhandener Buchungen (Aktionsart „verknüpfen“) – neue Buchungen, Margin-Umbuchungen und
Bereitstellen nur nach ausdrücklicher Bestätigung; (5) Datenstand und Pläne unter Sperre unverändert; (6) Automatik
erlaubt (`autopilot.enabled` + `autopilot.fixes`). Ergebnis jeder Prüfung steht im Fall (Regeltabelle).

**Bestandsbestätigung (Abschlussdialog):** Die Frage wird vorab berechnet: Es wird nur nach einer Position gefragt, auf
der die zulässigen Hypothesen verschiedene Bestände ergeben; erfüllen alle Varianten den Bestand gleich (z. B. reine
Transfer-Verknüpfung), wird nicht gefragt. Erfasst werden Konto, Unterkonto, Asset, Menge (0 gültig), Datum, Uhrzeit,
Zeitzone, Quelle, Umfang (Spot/Funding/Derivate/zusammengefasst), gebundene Menge, Margin und offenes Ergebnis (nur
Angaben; EUR-Gesamtwert ersetzt keine Menge). Spätere Buchungen zählen: Der Vergleich erfolgt zum angegebenen Zeitpunkt.
Passt der Nachweis zu genau einer belegten Deutung, kann der Nutzer Nachweis und Übernahme in einem Schritt
bestätigen. Sonst bleibt es beim Nachweis: „Menge bestätigt, historische Zuordnung offen“, **keine Ausgleichsbuchung**.
Ein bestätigter Endbestand ersetzt weder Anschaffungsbelege noch Gebührenursache noch Verlustursache.

**Qualitätsdimensionen (getrennt):** Bestand/Menge, Transfer und Gegenkonto, Gebühren, wirtschaftliche Vorgangsart,
Anschaffungsdaten, historische Vollständigkeit, steuerliche Einordnung – je *geklärt/offen/unbekannt/nicht betroffen*.
Fehlende EUR-Werte machen die Kostenbasis „unbekannt“ (nie 0 €), ohne bestätigte Mengen als ungelöst erscheinen zu
lassen.

**Derivate (Prüfung der Lücken):** Importkennzeichnungen sind mengenwirksam, das wirtschaftliche Ergebnis war aber nirgends
ausgewiesen: `derivatives.economics` rechnet es getrennt (Funding, Futures-PnL, Liquidation, Marginzinsen,
realisierter Gewinn/Verlust; Margin-Umbuchungen zählen nicht; unbekannte Beträge → „unbekannt“). `"derivative"` wurde
**nicht** zu `REALIZED_KINDS` ergänzt (der Gewinn aus verbrauchten Settlement-Lots ist nicht der Handelsverlust).
`margin`-Paare gleicher Menge sind nur ein Kandidat für einen internen Transfer (ohne verifizierte Anbietersemantik
genügt das Etikett nicht; nie automatisch). Von CSV-Profilen übersprungene Margin-/Derivatezeilen bleiben als
konkrete offene Vorgänge bestehen. Doppelabzug von Gebühren/Funding ist mangels verifizierter Anbietersemantik
(`derivatives.VERIFIED` leer) nie ausgeschlossen; die steuerliche Behandlung ist nicht implementiert und bleibt offen.

**Automatik, Nachprüfung, Rücknahme:** Nach Importen/Abrufen läuft die Klärung im Anschluss an den Autopilot;
Nachweise lösen einen Folgelauf aus. Jeder Lauf dokumentiert Belege, Kandidaten, Ausschlüsse, Simulation, Entscheidung
und Wirkung (`clarify_run`). Übernahme je Gruppe atomar; Wiederholung/Doppelklick führt nichts doppelt aus; parallele
Durchgänge werden abgewiesen und als Folgelauf vorgemerkt; nach der Übernahme wird nachgeprüft (Befund behoben, nichts
Neues, kein Nachweis verschlechtert) – sonst vollständige Rücknahme. Rückgängig nimmt alle zusammengehörigen Korrekturen
gemeinsam zurück. Rückstellungen und Ablehnungen gelten nur bei gleicher Evidenz (Prüfsumme der Befunde und
Nachweise); ein zurückgenommener oder wiederkehrender Fall wird neu bewertet. Ein „als geprüft“ markierter Hinweis
zählt nie als behobener Konflikt. Deterministische Grenzen: 64 Hypothesen je Gruppe (darüber „Prüfumfang überschritten“,
keine Auswahl), 400 Simulationen und 5 Quellenabrufe je Durchgang (übrige Gruppen zuerst im Folgelauf).

**Nachladen:** Je Fall höchstens eine lesende Recherche pro Datenquelle (Konto, Asset), nur bei unterstützter,
aktivierter, nicht laufender und nicht bereits recherchierter Quelle; deaktivierte/fehlgeschlagene Quellen erscheinen als
konkrete Blockade. Der reguläre Abrufstand bleibt unverändert (bestehende Recherche legt nur Staging-Ergebnisse ab).

**Export/Restore:** Fälle und Läufe sind Teil des Gesamtexports; Läufe erscheinen nach der Wiederherstellung als
„aus Wiederherstellung“ (die Wirkung steckt in den exportierten Buchungen).

**Tests:** `tests/test_clarify.py`, `tests/test_clarify_system.py` – die 16 geforderten Regressionsfälle, Oberfläche,
Recherche, echter Restore, Skalierung (24 Gruppen + 600 Ballast-Buchungen, Budget, Rotation, Prüfumfang).

**Grenzen / offen:** Die Klärung bildet keine Buchungen aus Bestandsdifferenzen und führt `adjust/opening/book_migration`
nicht aus. Automatisch ausführbar ist ausschließlich „verknüpfen“ (Ausblenden/Verknüpfen vorhandener Buchungen) mit
Identitätsbeleg oder rechnerischer Erklärung plus Bestandsnachweis. Reale Fallgruppen (K01–K13 der Auswertung vom
11.10.2026): es stand **keine reale Exportdatei** zur Verfügung – gemessene Zahlen liegen nicht vor; die synthetischen
Nachbildungen sind keine Erfolgsquote. Anbietersemantik für Derivate ist nicht verifiziert; Pionex-/Bybit-Native-Exporte
bleiben Spaltenzuordnung.

## 0.28.3 – Datenquellen: Intervall gilt auch bei unvollständigem Abruf (Kaspa)

Beobachtung: Kaspa-Wallets liefen trotz „einmal täglich“ über den Tag verteilt immer wieder und erreichten teils keinen
Abschluss; beim Öffnen der App war oft schon ein Abruf aktiv.

**Bestätigte Ursachen (am Code, ohne Live-Daten):**

* Ein unvollständiger Abruf (``coverage.resume``) zog ``next_run_at`` auf ``min(regulärer Termin, Start + 90 s)`` vor und
  ``due()`` ließ Fortsetzungen sogar bei „nur manuell“ zu; nach einem Fehler galt eine Wiederholung nach 10 Minuten.
  Der technische Fortsetzungspunkt war damit zugleich die Erlaubnis zum sofortigen Neustart.
* Folgetermine wurden vom **Startzeitpunkt** berechnet: dauerte eine Etappe länger als 90 s, endete sie mit einem bereits
  abgelaufenen Termin; Wartezeiten des Anbieters (``Retry-After``) zählten ab dem Start.
* KRC-20-Paginierung ohne Stillstandserkennung: lieferte der Indexer auf ``next``/``prev`` erneut dieselbe volle Seite,
  blieb der Zeiger stehen und der Lauf wiederholte bis zum Budgetende (``more=True``), über Läufe hinweg endlos. Dasselbe
  galt für die KAS-Seiten. Ein fehlgeschlagener KRC-20-Teil ließ außerdem die bekannten Token-Bestände verschwinden.

*Nicht* nachgewiesen (Live-Laufdaten nötig): ob beim betroffenen Konto wiederholte Seiten, Budgeterschöpfung, Indexerfehler
oder anderes den Stillstand ausgelöst hat. Beachtet: ein reiner Indexerausfall setzt ``more`` nicht und führt weiterhin nicht
zur schnellen Fortsetzung.

**Geändertes Verhalten:**

* **Intervall verlässlich:** Fortsetzung zum regulären Termin (Start + Intervall, nie vor Ende des Laufs + 5 min). „Nur manuell“
  und deaktiviert lösen nie automatische Läufe aus (auch nicht für Fortsetzungen); „Jetzt abrufen“ setzt am gespeicherten
  Fortsetzungspunkt fort. Der Folgetermin wird schon beim Start eines Laufs festgehalten (Neustart/Absturz → regulärer Termin,
  keine Wiederholungsschleife). Wartezeiten des Anbieters zählen ab dem Ende des Laufs.
* **Aufholen nur ausdrücklich:** Wallets setzen eine unvollständige Historie erst nach Einschalten von „Lange Historie automatisch
  aufholen“ in kurzen Abständen (90 s nach Laufende) fort – höchstens 12 Mal je regulärem Termin, nur bei nachweislichem
  Fortschritt des Abrufstands, nie nach Abbruch, Deaktivierung, Fehler oder Stillstand. Börsen-Anbindungen behalten ihr bisheriges
  Verhalten (jetzt mit denselben Grenzen). Manuell gestartete Mehr-Etappen-Läufe stoppen bei fehlendem Fortschritt, Abbruch,
  Fehler oder Deaktivierung.
* **Stillstand erkannt:** KAS (volle Seite ohne neue Blockzeit/bereits gelieferte Transaktionen) und KRC-20 (wiederkehrender
  ``next``/``prev``-Zeiger bzw. identische volle Seite) enden mit konkretem Grund; der Abrufstand rückt nur über vollständig
  verarbeitete Seiten vor (Budgetende mitten in einer Seite verliert nichts, bekannte Vorgänge werden erkannt), nichts wird
  übersprungen. Wiederholte Läufe ohne Fortschritt werden gezählt und angezeigt; es bleibt beim regulären Termin.
* **Darstellung:** Detailseite und Karte zeigen KAS und KRC-20 getrennt (vollständig / in Bearbeitung / blockiert / aus), Startgrund,
  letzte Fortschrittsänderung, Stillstandsgrund und den nächsten Versuch samt Begründung. Ein unvollständiger Abruf gilt nie als
  vollständig.
* **Bestände:** Schlägt der KRC-20-Teil (Abruf wie Verbindungsprüfung) fehl, bleiben bekannte Token-Bestände mit dem Vermerk „Stand
  des letzten vollständigen Abrufs“ stehen – nie als Nullbestand; KAS wird trotzdem aktualisiert.

**Grenzen:** Das Verhalten nach einem Absturz mitten im Lauf ist „nächster Versuch zum regulären Termin“ (bei täglichem Intervall bis
zu 24 h); „Jetzt abrufen“ umgeht das. Für eine Ursachenanalyse am echten Konto werden die Laufprotokolle der Datenquelle benötigt:
Zeitpunkte (Start/Ende/Auslöser) der letzten Läufe, ``coverage_json`` (``sched``, ``parts``, ``krc20``) und die Zahl der Anfragen
je Teil – diese Angaben stehen jetzt in der Detailseite unter „Abdeckung“.

## 0.28.2 – Härtung der Zusammenführungen (Codeprüfung)

* **Konten – Zielvorrang unabhängig von der Reihenfolge:** Bei der Zusammenführung der Konto-Stammdaten (Broker,
  Depotgruppe, Zusatzfelder) konnte der Alias gewinnen, wenn er vor dem Ziel verarbeitet wurde. Jetzt hat das Ziel immer
  Vorrang; der Alias füllt nur Lücken (mehrere Aliase in sortierter Reihenfolge, deterministisch). Fehlt das Ziel
  in den Stammdaten, werden die Alias-Angaben unter dem Ziel-Namen geführt.
* **Prüfsumme der Vorschau (Konten und Assets):** Sie deckt jetzt den gesamten entscheidungsrelevanten Stand ab –
  zusätzlich zu Zahlen, Beständen und Jahreswerten auch Warnungen, Hinweise, Fehler, Auffälligkeiten des Ledgers und (bei
  Konten) betroffene Datenquellen. Ändert sich eines davon zwischen Vorschau und Bestätigung, wird nichts übernommen.
* Regressionstests für beide Fälle (schlugen vor der Korrektur fehl).

## 0.28.1 – Konten zusammenführen (Migration 25)

Anlass: dasselbe Depot erscheint unter zwei Namen (z. B. „Wertpapiere Broker unbekannt“ aus dem kuratierten Import und
„DKB“ aus einer Datenquelle).

* **Konten zusammenführen** (`/accounts/merge`, erreichbar unter *Datenquellen → Konten ohne Datenquelle* und
  *Datenqualität*): der Alias-Kontoname wird dem Ziel-Konto **zugeordnet** – ohne Buchung, Rohdaten und Journal bleiben
  unverändert, Rückgängig löscht nur die Zuordnung. Wirkt an derselben Stelle wie die Asset-Zuordnung (Portfolio-Aufbau):
  Buchungen, Stammdaten des Kontos, Soll-Bestände des kuratierten Imports (Mengen werden addiert), Referenzbestände,
  gespeicherte Konto-Zuordnungen des CSV-Imports, Datenquellen (zeigen das Ziel-Konto), Cash-/Quellensteuer-Einstellungen
  (Ziel-Konto hat Vorrang).
* **Vorschau:** Bestände je Asset vorher/nachher, FIFO/Steuer je Jahr, Zahl der Buchungen *zwischen* beiden Konten
  (werden zu Bewegungen innerhalb eines Kontos), **mögliche Doppelbuchungen** (identischer Zeitpunkt/Art/Asset/Menge in
  beiden Konten), betroffene Datenquellen und offene Prüf-Stapel, abweichende Stammdaten. Bestätigung nur gegen die
  Prüfsumme der Vorschau; idempotent; im Gesamtexport enthalten.
* **Keine stille Dublettenbereinigung:** Wurde dasselbe Depot zweimal importiert, entstehen nach der Zusammenführung
  Dubletten – die Diagnose erkennt sie wie gewohnt und legt sie zur Entscheidung vor.
* **Grenzen:** Offene Prüf-Stapel des Alias werden weiter unter dem Alias-Namen abgeglichen (vorher abschließen);
  die Namens-Vorschläge sind Heuristiken (Schreibweise, Teilname, „unbekannt“, identische Buchungen).

## 0.28.0 – Duplikate zusammenführen, weniger manuelles Abarbeiten (Aufgaben/Hinweise, Regeln, Autopilot, Entscheidungsliste)

Anlass: dieselbe Anlage unter zwei Kennungen, doppelt angelegte Börsen/Wallets und zu viele einzeln zu prüfende Befunde
(Messung an einer lesend untersuchten Kopie eines realen Exports: 65 Befunde, davon 31 Hinweise; von 34 Abgleichs-Fällen
sofort ausführbar 0, mit Einbezug prüfbedürftiger 21, 12 widersprüchlich, 1 mit negativem Bestand).

* **Assets zusammenführen** (`/assets/merge`, Migration 23): Zwillingskennungen (z. B. `PEAQ` und `PEAQ#2918…`) werden als
  **umkehrbare Zuordnung** dem Haupt-Asset zugeschlagen – ohne Buchung, ohne Änderung der Rohdaten. Vorschläge nur aus
  Kursquelle/ISIN/WKN/Basiskennung; Vorschau rechnet FIFO/Steuer vorher/nachher, Bestätigung ist an eine Prüfsumme gebunden,
  idempotent, im Export enthalten. Paare, die bereits per Ticker-/Token-Änderung verknüpft sind, werden nicht vorgeschlagen.
* **Börsen/Wallets:** Doppelte Börse wird beim Anlegen erkannt und muss bewusst bestätigt werden; **doppelte
  Datenquellen** lassen sich zusammenführen (Vorschau, Prüfsumme; Stapel, Läufe, Verknüpfungen wandern, nicht rückgängig).
* **Aufgaben und Hinweise getrennt:** die Diagnose zeigt standardmäßig nur **Aufgaben**; Hinweise (Status „hinweis“, kein
  Fehler festgestellt) lassen sich mit Vorschau und Prüfsumme **gesammelt als geprüft** markieren und gemeinsam
  rückgängig machen. Keine Datenänderung; bei geänderter Evidenz erscheint der Befund wieder („überholt“).
* **Entscheidungsregeln** (`/quality/diagnose/regeln`, Migration 24): „Befunde der Art X (Konto/Asset) brauchen keine
  Handlung“. Bewusst eng: nur Hinweise (optional bis Verdacht), nie wahrscheinlich/belegt; geänderte („überholte“)
  Befunde werden nicht erneut ausgeblendet; jede Anwendung ist protokolliert und gemeinsam rückgängig.
* **Importbericht und Autopilot** (`/quality/autopilot`): nach jedem Import/jeder CSV-Übernahme entsteht ein Bericht
  (Aufgaben, Hinweise, neu/erledigt). Der **Autopilot ist standardmäßig aus**; eingeschaltet wendet er Regeln an und –
  nur mit eigener, bestätigter Einstellung – **beweisbare Korrekturen** (Stufe „eindeutig“, auf Wunsch „hoch plausibel“;
  nur Ausblenden/Verknüpfen oder Transfer aus genau zwei vorhandenen Buchungen; gemeinsam kein negativer Bestand, keine
  neuen Befunde). Jeder Lauf ist protokolliert und gemeinsam rückgängig zu machen.
* **Entscheidungsliste** (`/quality/diagnose/liste`): die verbleibenden Aufgaben Fall für Fall mit Empfehlung,
  Alternativen, Belegen, „kein Handlungsbedarf / abgelehnt / später“ und Rückweg aus der Vorschau in die Liste.
* **Grenzen:** Der Autopilot behebt nur, was mit den vorhandenen Belegen (Hash, Menge, Zeit) eindeutig ist; im
  gemessenen Realbestand wären das wenige Fälle – der Hauptgewinn liegt in Hinweisen/Regeln und der Reihenfolge der
  Entscheidungsliste. Zusammengeführte Datenquellen sind nicht umkehrbar; Zwillingskennungen werden nicht automatisch
  zusammengeführt (Menschen entscheiden, ob es dieselbe Anlage ist). Docker-Build nur in CI.

## 0.27.2 – Oberfläche: Abstand der Konto-Aktionen, Ignorieren als Umschalter

* Konto-Karten unter *Datenquellen*: Aktionen mit festem Abstand; der geöffnete Import-Block nimmt die volle Breite.
* *Datenqualität → Kurse*: „Hinweis ignorieren“ wird nach dem Klick zu **„Wieder aktivieren“** (hervorgehoben), die
  Zeile zeigt „ignoriert · nicht mehr in der Übersicht“, und die Seite bleibt an derselben Zeile. Die Tabelle
  nutzt mobil das Kartenlayout (Badges brechen nicht mehr um).

## 0.27.1 – Datei-Import je Konto, veraltete Kurse ausblenden, Integritätsprüfung

* **Börse mit Exportdatei anlegen:** Beim Anlegen einer Börse kann direkt eine CSV-, XLSX- oder ZIP-Datei mitgegeben
  werden; die Vorgänge werden dieser Börse (Konto in Portfolia) zugeordnet und zur Prüfung vorgelegt.
* **Konten ohne Datenquelle** erscheinen unter *Einstellungen → Datenquellen* (Abschnitt „Konten ohne Datenquelle“)
  mit Herkunft der Buchungen (kuratierter Import, Datei-Import, manuell), Zeitraum und Kennzeichen
  „manuell · Datei-Import“. Je Konto: **Neuer Import** oder **Als Datenquelle führen** (Buchungen bleiben unverändert).
* **Umfang je Import:** *Nur neue Transaktionen* (Stichtag = Tag vor der letzten Buchung des Kontos; ältere Zeilen als
  Historie, der Überlappungstag wird abgeglichen) oder *Gesamte Historie* (kein Stichtag; jede Zeile wird abgeglichen,
  fehlende zur Prüfung vorgelegt). Nie automatische Buchung, keine Doppelbuchung.
* **Börsen-Karten** zeigen Buchungen auf dem Konto, letzten Datei-Import und „Neuer Import“; Börsen ohne Anbindung
  heißen „manuell · Datei-Import“.
* **Kurse veraltet:** Unter *Datenqualität → Kurse* lässt sich der Hinweis je Asset ignorieren (und wieder anzeigen); er
  verschwindet aus der Übersicht. Kurs und Bewertung bleiben unverändert.
* **Integritätsprüfung:** Nach „Prüfung starten“ blieb „Prüfung gestartet“ stehen, weil der verzögert startende Job noch
  nicht lief und die Seite daher nicht nachfragte. Jetzt: „Prüfung läuft“ mit Fortschritt, danach „Prüfung
  abgeschlossen“; die Fortschrittsanzeige der Seite erhält wieder ihre Daten.

## Entscheidungen des Auftraggebers (27.09.2026)

* **Lizenz:** MIT (`LICENSE`); Drittkomponenten in `THIRD_PARTY_NOTICES.md`, NOTICE von Apache ECharts und
  d3-Lizenz unter `app/static/vendor/`, OCI-Label `org.opencontainers.image.licenses=MIT`.
* **Voreinstellungen Steuer** bestätigt: Gebühren beim Handel als Veräußerung, Transfergebühren nicht
  steuerbar, Airdrops/Mining als § 22 Nr. 3, Wertpapierdepots standardmäßig „Inland“.
* **Formularzeilen** „sofern bekannt“ hinterlegt: 2024 (Anlage SO, KAP, KAP-INV) und Anlage SO 2025
  (neuer Abschnitt „Kryptowerte“). Anlage KAP/KAP-INV 2025 und § 22 Nr. 3 ohne Zeilen, da die verfügbaren
  (Sekundär-)Quellen widersprüchlich sind; die amtlichen Vordrucke waren aus der Build-Umgebung nicht
  abrufbar. Zusätzlich Basiszins 2026 (3,20 %, BMF vom 13.01.2026).
* **KI-Modell:** Standard `claude-opus-5` bleibt.
* **Sparpläne** (neue Anforderung): siehe M6.
* **`related_asset`** ist ab Schema 1.1 offizieller Teil des Datenvertrags (1.0 bleibt gültig).
* **Buchungen in der App erfassen** (neue Anforderung, mittelfristig alles in Portfolia): siehe M7.
* **Börsen und Wallets:** nur CSV-Import, **keine Online-Synchronisation**; alles jederzeit im einheitlichen
  Import-Format exportierbar; datierte ZIP-Sicherungen nach Änderungen – siehe M8. **Erweitert am 28.09.2026:**
  Grundlage für in der App konfigurierbare Datenquellen (Börsen, öffentliche Adressen), noch ohne Anbindung –
  siehe M11.
* **Wallets auf sechs Chains** (01.10.2026): read-only über öffentliche Adressen bzw. Kontoschlüssel, mit
  Anbieter-Recherche und dokumentierter Abdeckung je Chain – siehe M15.
* **Positionen als Verlust ausbuchen** (28.09.2026, neue Anforderung) und verkaufte Aktien nicht als
  „unbewertet“ melden – siehe M9.
* **Kursquellen automatisch suchen** (CoinGecko) und mobiles Layout korrigieren (28.09.2026) – siehe M10.
* **Bitpanda als erster Connector** (29.09.2026): Einrichtung inkl. API-Key in der App, Schlüssel verschlüsselt
  mit externem Master-Key, nur Leserechte, automatische Übernahme standardmäßig aus und nur je eindeutigem
  Ereignis, Doppelzählung Import ↔ App beheben, verworfene Vorgänge wieder abrufbar – siehe M12 (beantwortet die
  Fragen 4–7 und 9 für Bitpanda).
* **Datenqualität** (01.10.2026): read-only Diagnose mit Bestandsabgleich und Schutzregeln für künftige Importe;
  bestehende Daten werden nicht bearbeitet, Auswirkungen nur als Szenario – siehe M17.
* **Korrekturen aus der Diagnose** (02.10.2026): Empfehlung je Befund, Übernehmen per Knopfdruck nach Vorschau mit
  allen Folgen, Alternativen und eigene Lösung, Rückgängig – weiterhin nichts automatisch; siehe M18.
* **Importprüfung und Reconciliation** (02.10.2026): Stapelverarbeitung mit Vorschau und Rückgängig, Ergänzungen
  ohne Überschreiben, Quellenpriorität, Bitpanda-Vollständigkeit; vorhandene Daten nicht verändern, historische
  Probleme nur als Korrekturvorschlag – siehe M19.
* **Gekoppelte Buchung bei verzögerter Auszahlung** (02.10.2026): Wallet-Zugang als Seite eines Import-Transfers
  erkennen – auch verzögert und unter anderem Kontonamen; bereits gebuchte Fälle zur Entscheidung – siehe M19.1.
* **Wallet-Erweiterung** (03.10.2026): Polygon, XRP Ledger, Cardano, Polkadot als reguläre Datenquellen; Gruppen
  (z. B. „Ledger“) nur als Zuordnung; Übersicht mit Suche, Sortierung, Werten und Zuständen; keine
  Geräteverbindung, Signaturen, Seeds oder privaten Schlüssel; bestehende Buchungen nicht automatisch ändern – siehe
  M20.
* **Weiterentwicklung 0.19.0** (07.10.2026): Ursachen historischer Kurslücken und KRC-20-403 beheben, einheitlicher
  Fortschritt, Treemap, Top-Bewegungen % | €, Positionsdetail, Schnellbuchung, Watchlist, Steuerdaten je Jahr ohne
  stilles Ersetzen und ohne neue Buchungen – siehe M21.
* **Ticker-/Token-Änderungen** (08.10.2026): erkennen (Register, CoinGecko-Katalog, Anbieter-Bestände,
  Kursstillstand) und per Vorschau umstellen bzw. umbenennen, rückgängig machbar – siehe M22.

## Offene Fragen an den Auftraggeber

1. **CSV-Formate:** Welche Börsen/Wallets werden konkret genutzt? Für Formate außerhalb der Liste (z. B. BISON,
   Bitvavo, Bybit, KuCoin) genügt die Spaltenzuordnung; mit einer anonymisierten Beispieldatei kann ein festes
   Profil ergänzt werden.
2. **Name/Pfade:** Umsetzung als „Portfolia“ (`portfolia.xml`, `/mnt/user/appdata/portfolia`) statt
   „Depotblick“ – so gewünscht?
3. **Krypto-Historie > 365 Tage** mit CoinGecko-Demo: weitere Yahoo-Paare vorbelegen oder Pro-Schlüssel?
4. **Wallet-Regeln je Chain** vor der ersten Wallet-Anbindung: eigene Adressen untereinander als Transfer,
   Gas-Gebühren fehlgeschlagener Transaktionen, Spam-Token, interne Transaktionen/Contract-Aufrufe.
5. **Bitpanda live prüfen:** Ein eigens dafür erstellter, rein lesender Test-Schlüssel (Transaction + Balance,
   kurzes Ablaufdatum) würde Feldnamen, Pagination, Fehlercodes und die Gebührenkonvention bestätigen – nur auf
   ausdrücklichen Wunsch, nie im Chat.
6. **Tausch Krypto → Krypto bei Bitpanda:** als Tausch mit EUR-Wert aus dem Tageskurs buchen (steuerlich eine
   Veräußerung) oder – wie jetzt – immer zur Prüfung?
7. **Bitpanda Stocks/ETFs und Edelmetalle:** als Wertpapiere bzw. Metalle abbilden (braucht ISIN-/Asset-Zuordnung
   und Steuerart) oder weiter per CSV bzw. manuell?
8. **Gebühren an Haupt-Teilen:** Nach Klärung der Brutto/Netto-Konvention automatisch übernehmen statt als
   prüfbedürftig zu markieren?
