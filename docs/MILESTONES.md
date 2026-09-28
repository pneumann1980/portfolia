# Meilensteine: Entscheidungen, Grenzen, offene Fragen

Stand: 28.09.2026 · Version 0.10.0 · Branch `claude/portfolia-dashboard-s9p6zr`

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

**Offene Entscheidungen** (vor der ersten Anbindung zu klären) – siehe unten, Fragen 4–9.

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
* **Positionen als Verlust ausbuchen** (28.09.2026, neue Anforderung) und verkaufte Aktien nicht als
  „unbewertet“ melden – siehe M9.
* **Kursquellen automatisch suchen** (CoinGecko) und mobiles Layout korrigieren (28.09.2026) – siehe M10.

## Offene Fragen an den Auftraggeber

1. **CSV-Formate:** Welche Börsen/Wallets werden konkret genutzt? Für Formate außerhalb der Liste (z. B. BISON,
   Bitvavo, Bybit, KuCoin) genügt die Spaltenzuordnung; mit einer anonymisierten Beispieldatei kann ein festes
   Profil ergänzt werden.
2. **Name/Pfade:** Umsetzung als „Portfolia“ (`portfolia.xml`, `/mnt/user/appdata/portfolia`) statt
   „Depotblick“ – so gewünscht?
3. **Krypto-Historie > 365 Tage** mit CoinGecko-Demo: weitere Yahoo-Paare vorbelegen oder Pro-Schlüssel?
4. **Erste Anbindungen (M11):** Welche Börsen und Chains zuerst? Vorschlag: Kraken (Ledgers-API, Kennungen
   identisch mit dem CSV-Profil) und Bitcoin per xpub bzw. eine EVM-Chain. Explorer-APIs verlangen teils eigene
   Schlüssel und erfahren die Adresse – akzeptabel?
5. **Zugangsdaten:** Umgebungsvariable (jetzt: nichts Geheimes in Datenbank und Backups, aber Container-Neustart
   je neuem Schlüssel) oder verschlüsselt in der Datenbank mit Hauptschlüssel aus der Umgebung?
6. **Automatische Übernahme:** Standard „aus“ beibehalten? Soll eine Überschneidung den ganzen Abruf zur Prüfung
   schicken (jetzt) oder nur die betroffenen Zeilen?
7. **Kuratierter Import und Datenquelle für dieselbe Börse:** Enthält ein neuer kuratierter Import Buchungen, die
   bereits per Datenquelle übernommen wurden, zählen sie doppelt (Dublettenwarnung greift, verhindert es aber
   nicht). Börse künftig nur an einer Stelle führen – oder darf `source_ref` im Datenvertrag die Ereignis-ID
   (`kraken:<refid>`) tragen, damit exakt abgeglichen und ausgeblendet werden kann?
8. **Wallet-Regeln je Chain** vor der ersten Wallet-Anbindung: eigene Adressen untereinander als Transfer,
   Gas-Gebühren fehlgeschlagener Transaktionen, Spam-Token, interne Transaktionen/Contract-Aufrufe.
9. **Verwerfen eines Prüf-Stapels:** Abrufstand automatisch zurücksetzen (Vorgänge kommen beim nächsten Lauf
   wieder, auch reine Dubletten) oder wie jetzt nur auf Knopfdruck?
