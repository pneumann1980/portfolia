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
