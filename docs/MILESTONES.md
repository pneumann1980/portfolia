# Meilensteine: Entscheidungen, Grenzen, offene Fragen

Stand: 26.09.2026 · Version 0.5.0 · Branch `claude/portfolia-dashboard-s9p6zr`

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
| Image | < 350 MB | ≈ 274 MB (regulärer Build mit `strip`, 37,5 MB Symbole laut ELF-Analyse), 311 MB ohne `strip` |

---

**Grenzen M5:** Beim erstmaligen Laden der Historie eines großen Portfolios steigt der Speicher kurzzeitig
(gemessen 413 MB bei 170 Assets × 7,7 Jahren) – ein Container-Speicherlimit sollte daher nicht unter
512 MB liegen. `strip` benötigt beim Bauen Zugriff auf die Debian-Paketquellen (sonst 311 MB statt ≈ 274 MB).

## Offene Fragen an den Auftraggeber

1. **Lizenz** des Projekts (derzeit keine LICENSE-Datei).
2. **Name/Pfade:** Umsetzung als „Portfolia“ (`portfolia.xml`, `/mnt/user/appdata/portfolia`) statt
   „Depotblick“ – so gewünscht?
3. **Steuer-PDFs:** Ausgabe als Aufstellung/Beleg plus Übertragungshilfe (nicht als ausgefülltes amtliches
   Formular). Sollen verifizierte Zeilennummern für bestimmte Jahre fest hinterlegt werden?
4. **Voreinstellungen Steuer:** Gebühren beim Handel als Veräußerung, Transfergebühren nicht steuerbar,
   Airdrops/Mining als § 22 Nr. 3, Wertpapierdepots standardmäßig „Inland“ – passt das?
5. **`related_asset`** ist eine optionale Zusatzspalte in `transactions.csv` (für Dividenden und
   Quellensteuer). Soll sie in Schema 1.1 offiziell aufgenommen werden?
6. **KI-Modell:** Standard `claude-opus-5` mit serverseitigem Fallback – beibehalten oder ein
   kostengünstigeres Modell voreinstellen?
7. **Krypto-Historie > 365 Tage** mit CoinGecko-Demo: weitere Yahoo-Paare vorbelegen oder Pro-Schlüssel?
