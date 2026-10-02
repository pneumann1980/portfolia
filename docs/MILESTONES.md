# Meilensteine: Entscheidungen, Grenzen, offene Fragen

Stand: 01.10.2026 · Version 0.15.0 · Branch `claude/portfolia-dashboard-s9p6zr`

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
