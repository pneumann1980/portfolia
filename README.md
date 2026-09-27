# Portfolia

Selbst gehostetes Portfolio-Dashboard für Aktien, ETFs und Kryptowerte – ein Docker-Image für Unraid
(6.12+/7.x), nur für das lokale Netz. Portfolia liest einen kuratierten Import (ZIP) **read-only**, bewertet
die Positionen mit öffentlichen Kursquellen, berechnet Performance (TTWROR/IRR nach der Methodik von
Portfolio Performance), zeigt Haltefristen und erstellt Steueraufstellungen als PDF. Passende News und
YouTube-Videos werden je Position gefiltert.

* **Datenquellen:** Buchungen stammen aus dem kuratierten Import (ZIP, **read-only**, wird nie verändert),
  werden direkt in Portfolia erfasst (siehe [Buchungen erfassen](#buchungen-in-portfolia-erfassen)) oder aus
  **CSV-Exporten von Börsen, Wallets und Steuertools** übernommen (siehe [CSV-Import](#csv-import-aus-börsen-und-wallets))
  – auch ganz ohne Import. Es gibt **keine Online-Anbindung** an Börsen oder Wallets. In der App erfasste
  Buchungen und Assets liegen neben abgeleiteten Daten (Kurse, News, Snapshots, Berichte) in `/data/app.sqlite`.
* **Export und Sicherung:** Alles lässt sich jederzeit im einheitlichen Import-Format (Datenvertrag, Schema 1.1)
  exportieren; nach jeder Änderung entsteht automatisch eine **datierte ZIP-Sicherung**, importierte ZIP-Dateien
  werden mit Datum archiviert (siehe [Backups](#einstellungen-sicherheit-backups)).
* **Sparpläne:** Laufende Sparpläne werden erkannt und nach dem Datenstand als **markierte Schätzung**
  fortgeführt, bis der Import oder eine manuell erfasste Buchung die echte Ausführung enthält (siehe
  [Sparpläne](#sparpläne)).
* **Keine Schreibzugriffe** auf Broker oder Börsen, **keine Telemetrie**, keine externen Schriften/CDNs.
* **Datenschutz:** Keine Anfrage an externe Dienste enthält Stückzahlen, Werte oder Kontonamen
  (per Test abgesichert, siehe `tests/test_privacy.py`).
* **Steuerfunktionen sind informativ – keine Steuerberatung.**

Oberfläche: Deutsch, Zahlen im de-DE-Format, Basiswährung EUR, Zeitzone Europe/Berlin, hell/dunkel,
für Smartphones (≈390 px) optimiert.

---

## Inhalt

1. [Installation](#installation)
2. [Erste Schritte](#erste-schritte)
3. [Datenvertrag (Import-ZIP)](#datenvertrag-import-zip)
4. [Buchungen in Portfolia erfassen](#buchungen-in-portfolia-erfassen)
5. [CSV-Import aus Börsen und Wallets](#csv-import-aus-börsen-und-wallets)
6. [Berechnungen](#berechnungen)
7. [Sparpläne](#sparpläne)
8. [Kurse und Datenquellen](#kurse-und-datenquellen)
9. [News und YouTube](#news-und-youtube)
10. [Steuern und Haltefristen](#steuern-und-haltefristen)
11. [Einstellungen, Sicherheit, Backups](#einstellungen-sicherheit-backups)
12. [Betrieb und Fehlerbehebung](#betrieb-und-fehlerbehebung)
13. [Entwicklung](#entwicklung)
14. [Grenzen und Lizenz](#grenzen-und-lizenz)

---

## Installation

### Unraid (Template)

Das Image wird von der CI als `ghcr.io/pneumann1980/portfolia:latest` (amd64/arm64) veröffentlicht. Damit Unraid es
ohne Anmeldung laden kann, muss das Paket auf GitHub **öffentlich** sein (einmalig: *GitHub → Packages → portfolia →
Package settings → Change visibility → Public*; es enthält nur den öffentlichen Programmcode, keine Daten).

1. Template in den Unraid-Vorlagenordner laden (Unraid-Terminal):
   ```sh
   mkdir -p /boot/config/plugins/dockerMan/templates-user
   wget -O /boot/config/plugins/dockerMan/templates-user/my-Portfolia.xml \
     https://raw.githubusercontent.com/pneumann1980/portfolia/HEAD/unraid/portfolia.xml
   ```
   Danach *Docker → Add Container → Template: Portfolia* (unter „User templates“).
2. Pfade prüfen:

   | Container | Host (Vorschlag) | Modus | Inhalt |
   |---|---|---|---|
   | `/data` | `/mnt/user/appdata/portfolia` | rw | Datenbank, Cache, Backups, Berichte, `sources.yaml`, `tax_rules/` |
   | `/import` | `/mnt/user/appdata/portfolia-import` | **ro** | Import-ZIPs (nicht innerhalb von `/data` ablegen) |
   | `/exports` | `/mnt/user/appdata/portfolia-exports` | rw | datierte ZIP-Sicherungen und Import-Archiv (`EXPORT_DIR=/exports`); gern auf eine gesicherte Freigabe legen |

3. Optional API-Schlüssel eintragen (werden nur aus Umgebungsvariablen gelesen, nie angezeigt oder geloggt).
4. Container starten, Weboberfläche über *WebUI* öffnen (Port 8080).

Das Image läuft als Nicht-Root-Benutzer mit `PUID`/`PGID` (Unraid-Standard 99/100), hat einen
`HEALTHCHECK` (`/healthz`) und schreibt strukturierte Logs (JSON) auf stdout.

### Docker Compose / docker run

```yaml
services:
  portfolia:
    image: ghcr.io/pneumann1980/portfolia:latest
    container_name: portfolia
    init: true
    ports: ["8080:8080"]
    environment:
      TZ: Europe/Berlin
      PUID: "1000"
      PGID: "1000"
      COINGECKO_API_KEY: ""      # empfohlen (kostenloser Demo-Schlüssel)
      EXPORT_DIR: /exports       # datierte ZIP-Sicherungen (optional, sonst /data/exports)
    volumes:
      - ./data:/data
      - ./import:/import:ro
      - ./exports:/exports
    restart: unless-stopped
```

Lokal bauen: `docker compose up --build` (siehe `docker-compose.yml`). Hinter einem Proxy mit eigener
Zertifizierungsstelle kann die CA als Build-Secret übergeben werden:
`docker build --secret id=pip_ca,src=/pfad/ca.pem -t portfolia .`

### Umgebungsvariablen

| Variable | Standard | Bedeutung |
|---|---|---|
| `TZ` | `Europe/Berlin` | Zeitzone (Tagesgrenzen, Zeitpläne, Anzeige) |
| `PUID` / `PGID` | `99` / `100` | Benutzer/Gruppe, unter denen die App läuft |
| `PORT` | `8080` | interner Port (bei Änderung auch das Port-Mapping anpassen) |
| `BASE_CURRENCY` | `EUR` | derzeit nur EUR |
| `COINGECKO_API_KEY` | – | CoinGecko-Schlüssel (Demo oder Pro) |
| `COINGECKO_PLAN` | `demo` | `demo` oder `pro` |
| `FINNHUB_API_KEY` | – | optional: Unternehmensnews |
| `YOUTUBE_API_KEY` | – | optional: Handle-Auflösung per API, Discovery |
| `ANTHROPIC_API_KEY` | – | optional: KI-Zusammenfassungen (standardmäßig aus) |
| `CRYPTOPANIC_API_KEY` | – | optional: Krypto-News-Aggregator |
| `AUTH_MODE` | `none` | `none` oder `basic` |
| `AUTH_USER`, `AUTH_PASSWORD_HASH` | – | Basic-Auth; Hash erzeugen: `docker exec -it portfolia python -m app hash-password` |
| `LOG_LEVEL` | `INFO` | `DEBUG` … `ERROR` |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | IPs eines Reverse-Proxys, dessen `X-Forwarded-*` vertraut wird |
| `ROOT_PATH` | – | Betrieb unter Unterpfad hinter einem Proxy (z. B. `/portfolia`) |
| `DEMO_MODE` | `false` | synthetische Kurse ohne Internetzugriff (zum Ausprobieren) |
| `EXPORT_DIR` | `/data/exports` | datierte ZIP-Sicherungen im Import-Format und Archiv der importierten ZIP-Dateien |

---

## Erste Schritte

1. Import-ZIP in den Importordner legen. Portfolia prüft den Ordner beim Start, **alle 5 Minuten** und auf
   Knopfdruck („Import prüfen“). Dateien, die jünger als 15 Sekunden sind, werden erst im nächsten Lauf
   gelesen (noch nicht fertig kopiert).
2. Ein Beispiel liegt im Image unter `/opt/portfolia/examples/beispiel-import.zip` bzw. im Repository unter
   `examples/beispiel-import.zip` (Aktien inkl. USD-Titel und 10:1-Split, ETF-Sparplan, Dividenden,
   Krypto mit Transfer, Staking, Mining, Airdrop, Tausch mit Gebühr, manuell bewertetes Token und eine
   absichtliche Soll-Ist-Abweichung).
3. Nach dem Import lädt Portfolia im Hintergrund aktuelle Kurse und die Kurshistorie (Fortschritt oben
   eingeblendet) und berechnet tägliche Snapshots für Performance-Kennzahlen.
4. Alternativ oder ergänzend Buchungen direkt erfassen: *Buchungen → Neue Buchung* (siehe
   [Buchungen erfassen](#buchungen-in-portfolia-erfassen)) oder CSV-Exporte von Börsen und Wallets einlesen:
   *Buchungen → CSV importieren* (siehe [CSV-Import](#csv-import-aus-börsen-und-wallets)).

Ausprobieren ohne Internet: `DEMO_MODE=true` (deutlich gekennzeichnete synthetische Kurse).

---

## Datenvertrag (Import-ZIP)

Ein Import ist eine ZIP-Datei mit folgendem Inhalt (optional in genau einem Unterordner):

| Datei | Pflicht | Inhalt |
|---|---|---|
| `manifest.json` | ja | `schema_version` (1.0 oder 1.1), `generated_at` (ISO 8601), `valuation_date` (YYYY-MM-DD), `files` (`{dateiname: sha256}`), `notes` |
| `transactions.csv` | ja | alle Buchungen |
| `assets.csv` | ja | Stammdaten der Assets |
| `holdings_check.csv` | ja | erwartete Bestände (nur zum Abgleich, nicht zur Berechnung) |
| `issues.csv` | ja | bekannte Datenprobleme (Anzeige unter Datenqualität) |
| `manual_prices.csv` | nein | Kurse für Assets ohne Kursquelle |
| `accounts.csv` | nein | Konten/Depots mit Broker, Depotgruppe und optional Steuerabzug |

CSV: UTF-8, Komma als Trenner, **Punkt als Dezimaltrenner**, erste Zeile Spaltennamen. Unbekannte Spalten
werden mitgespeichert (z. B. `tax_type` in `assets.csv`, `tax_withholding` in `accounts.csv`).

**Schema-Versionen** (`schema_version` im Manifest; Minor-Versionen sind abwärtskompatibel):

| Version | Änderung |
|---|---|
| 1.0 | Grundformat |
| 1.1 (aktuell) | `related_asset` in `transactions.csv` offiziell: verknüpft Dividenden, Ausschüttungen und Quellensteuer mit dem Wertpapier. Optional – ohne die Spalte bleiben Dividenden unzugeordnete Konto-Erträge (keine Fondsart/Teilfreistellung, kein Quellensteuer-Abgleich). |

Dateien mit 1.0 werden weiterhin ohne Hinweis angenommen; höhere Minor-Versionen (z. B. 1.2) mit Warnung,
unbekannte Felder werden dann ignoriert. Exporte von Portfolia (Sparplan-CSV, Gesamtexport) schreiben 1.1.

### Validierung

Der Import ist atomar: Bei Fehlern wird **nichts** übernommen; der bisherige Stand bleibt aktiv und der
Fehlerbericht steht unter *Datenqualität → Import*. Geprüft werden u. a.: ZIP-Sicherheit (Pfade, Größe,
Kompressionsverhältnis), Manifest und SHA-256-Prüfsummen (optional mit Präfix `sha256:`), Schema-Version
(Major 1), Pflichtspalten, Datentypen, eindeutige `tx_id`, Referenzen auf `asset_id`, typabhängige
Pflichtbeine. Warnungen (z. B. unbekannte Tags, fehlende `fee_eur`) verhindern den Import nicht.

Die letzten 10 Importstände werden versioniert aufbewahrt; zu jedem Import gibt es einen Diff zum Vorgänger
(neue/geänderte/entfernte Transaktionen, Bestandsänderungen) und den Abgleich mit `holdings_check`.
Eine unveränderte Datei (gleiche Prüfsumme) wird nicht erneut importiert.

### transactions.csv

Pflichtspalten: `tx_id, datetime, type, from_account, from_asset, from_qty, to_account, to_asset, to_qty,
fee_asset, fee_qty, fee_eur, value_eur` · optional: `tag, orig_price, orig_ccy, source, source_ref, flag,
note, related_asset`

* `datetime`: ISO 8601; ohne Offset = UTC; nur Datum = 12:00 Uhr Europe/Berlin.
* `type`: `buy`, `sell`, `trade`, `deposit`, `withdrawal`, `transfer`, `corporate_action`.
* **Konventionen:**
  * `from_qty` ist die Menge, die das Abgangskonto **ohne Gebühr** verlässt; `fee_qty` wird zusätzlich
    belastet (bei Transfers: `from_qty` = `to_qty`, Netzwerkgebühr separat als `fee_*`).
  * `value_eur` ist der **Brutto-Gegenwert ohne Gebühr** (Einstand Kauf = `value_eur + fee_eur`,
    Erlös Verkauf/Tausch = `value_eur − fee_eur`, Einstand eines Tausch-Zugangs = `value_eur`).
  * Gebühren in einem Nicht-Fiat-Asset sind eigene Abgänge (verbrauchen Lots, Erlös = `fee_eur`).
  * `value_eur` ist Pflicht für `buy`, `sell`, `trade`; bei Nicht-Fiat-Zu-/Abgängen dringend empfohlen.
* **Tags** (`tag`):
  * Erträge: `staking, lending, interest, reward, bonus, mining, airdrop, cashback, fork, other_income, dividend`
  * Kosten/Verluste: `cost` (Bezahlung mit Krypto), `fee`, `lost`, `stolen`, `burn`
  * Schenkung: `gift`, `donation` (Abgang), `gift_received` (Zugang)
  * Kapitalmaßnahmen: `split, reverse_split, merger, spinoff, migration, rename, swap`
  * Steuern: `withholding_tax` (ausländische Quellensteuer), `tax` (einbehaltene inländische Steuer)
* **Dividenden:** `deposit` mit `tag=dividend`, Fiat-Zugang auf das Konto und `related_asset` = Wertpapier.
  Ist Quellensteuer als eigene Buchung erfasst (`withdrawal`, `tag=withholding_tax`, gleiches Konto und
  `related_asset`, ±7 Tage), gilt `value_eur` der Dividende als **Brutto**betrag.
* **Splits/Kapitalmaßnahmen:** `corporate_action` mit Abgang und Zugang (z. B. 30 → 300 Stück desselben
  Assets); Lots behalten Einstand und Anschaffungsdatum.
* **Transfers** zwischen eigenen Konten übertragen Lots mit Anschaffungsdatum. Zu-/Abgänge ohne
  Gegenbuchung werden als externe Ein-/Auszahlung zum Marktwert behandelt (einstellbar).

### assets.csv

Pflicht: `asset_id, name, asset_class (security|crypto|fiat), quote_source (yahoo|coingecko|manual|none),
quote_id` · optional: `wkn, isin, koinly_id, status, note, aliases (;-getrennt), category, tax_type`

* `quote_id`: Yahoo-Symbol (z. B. `SAP.DE`, `NVDA`) bzw. CoinGecko-ID (z. B. `bitcoin`).
* `category` steuert die Gruppierung (z. B. „Aktien: ETF“, „Krypto: Altcoins“).
* `aliases` verbessern die News-Zuordnung.
* `tax_type` (optional): `share, etf_equity, etf_mixed, etf_other, fund_realestate, fund_realestate_foreign,
  bond, other` – sonst Erkennung aus Name/Kategorie (als „geschätzt“ markiert) bzw. Einstellung.

### holdings_check.csv, issues.csv, manual_prices.csv, accounts.csv

* `holdings_check.csv`: `asset_id, qty` (+ optional `account, as_of, source, note`); Toleranz
  max(1e-8; 1e-6 × |Soll|). Abweichungen erscheinen unter *Datenqualität*.
* `manual_prices.csv`: `asset_id, date, price_eur` (+ `source`); ohne Kurs bleibt ein Asset sichtbar
  „unbewertet“ mit 0 €, nie still ignoriert.
* `accounts.csv`: `account` (+ `broker, depot_group`, optional `tax_withholding` = `domestic|foreign`).

Prüfen ohne Import: `docker exec portfolia python -m app validate /import/datei.zip`

---

## Buchungen in Portfolia erfassen

Buchungen lassen sich ergänzend zum Import oder ganz ohne Import direkt in Portfolia erfassen
(*Buchungen* in der Seitenleiste bzw. *Mehr → Buchungen*). Die Seite listet **alle** Buchungen mit Filtern
(Konto, Asset, Quelle, Typ, Jahr, Suche) und Kennzeichnung der Quelle (Import, manuell, CSV-Import, Sparplan).

* **Vorlagen:** Kauf, Verkauf, Tausch, Übertrag zwischen eigenen Konten, Ertrag (Dividende mit
  Quellensteuer, Zinsen, Staking, Lending, Airdrop …), Ein-/Auszahlung, Kosten/Verlust, Kapitalmaßnahme und
  „Experte“ (alle Felder des Datenvertrags). Die Vorlage bildet Abgangs-, Zugangs- und Gebührenbein gemäß
  Datenvertrag; Zahlen mit Komma oder Punkt.
* **EUR-Werte:** Fremdwährungen per EZB-Devisenkurs des Tages; bei Tausch, Erträgen und Zu-/Abgängen ohne
  Angabe Tageskurs × Menge (Schlusskurs, am laufenden Tag aktueller Kurs, ersatzweise manueller Kurs oder
  letzter Transaktionskurs der letzten 31 Tage). Die Herkunft des Werts wird gespeichert.
* **Prüfung:** derselbe Validator wie beim Import (Pflichtbeine, `value_eur`, Transfers …). Hinweise, wenn
  ein Abgang den Bestand eines Kontos ins Minus drückt oder eine manuelle Buchung einer Import-Buchung stark
  ähnelt (gleiche Konten und Assets, ±2 Tage, Menge ±1 % → „Dublette?“, auch unter *Datenqualität*).
* **Bearbeiten, Kopieren, Löschen:** IDs `PF-M-000001` ff.; Import-Buchungen lassen sich als Vorlage kopieren.
  Löschen ist umkehrbar; jede Änderung steht im Änderungsprotokoll (*Datenqualität*).
* **Assets:** neue Positionen mit Kursquelle (CoinGecko-ID bzw. Yahoo-Symbol), Kategorie und Steuerart
  anlegen. Definiert der Import dasselbe Asset, gelten dessen Stammdaten.
* **Zusammenspiel mit dem Import:** Der Import bleibt unverändert, manuelle Buchungen kommen hinzu. Enthält ein
  späterer Import dieselbe `tx_id`, gilt die Import-Buchung (keine Doppelzählung); ähnliche Buchungen mit
  anderer ID werden nur gemeldet. Manuell erfasste Sparplan-Ausführungen ersetzen passende Schätzungen.
* **Gesamtexport:** Import + manuelle und per CSV importierte Buchungen + freigegebene Sparplan-Ausführungen als
  Import-ZIP (Schema 1.1, mit aktuellem Bestand als `holdings_check`, steuerlichen Einstufungen als `tax_type` bzw.
  `tax_withholding` und allen Konten) – als Sicherung, zum Umzug oder als neuer kuratierter Import. Zusätzlich
  entsteht nach jeder Änderung automatisch eine datierte Kopie (siehe [Backups](#einstellungen-sicherheit-backups)).
* **Ohne Import:** Alle Ansichten (Positionen, Performance, Steuern, Sparpläne) funktionieren auch nur mit
  manuell erfassten Buchungen. Steuerberichte weisen manuell erfasste Buchungen des Jahres aus.

---

## CSV-Import aus Börsen und Wallets

*Buchungen → CSV importieren* liest CSV-Exporte ein und übersetzt sie in das einheitliche Buchungsformat des
Datenvertrags. Es gibt bewusst **keine Online-Anbindung** (keine API-Schlüssel von Börsen, kein Wallet-Sync) –
nur Dateien, die du selbst exportierst.

**Unterstützte Formate** (automatisch erkannt; unbekannte Vorgänge werden mit Zeilennummer gemeldet, nie geraten):

| Gruppe | Format | Export |
|---|---|---|
| Börse | Binance – Kontoauszug (*Transaction History*) | Orders → Transaction History → Generate all statements |
| Börse | Bitpanda – Transaktionsverlauf (inkl. Bitpanda Stocks) | Verlauf → Transaktionen exportieren |
| Börse | Kraken – Ledgers | Documents → Export → Ledgers |
| Börse | Coinbase – Transaktionsbericht (inkl. Advanced Trade) | Profil → Berichte → Transaktionsverlauf |
| Börse | Crypto.com App – Krypto- und Fiat-Wallet | Konten → Wallet → Verlauf exportieren |
| Wallet | Ledger Live, Trezor Suite (auch ältere Trezor-Exporte), Electrum, Exodus | Export der jeweiligen App |
| Steuertool | Koinly (Transaktionsexport, „Bulk edit“, Universal-Vorlage), Blockpit, CoinTracking | Export als CSV |
| Portfolia | Datenvertrag (`transactions.csv`) | eigene Listen, andere Portfolia-Instanzen |
| alle anderen | **Eigenes Format**: Spalten einmal zuordnen (Datum, Zu-/Abgang, Gebühr, Gegenwert, Vorgangsart …) | Zuordnung wird gespeichert und künftig automatisch erkannt |

Über die Steuertool-Formate (bzw. den bei vielen Börsen angebotenen Export „im Koinly-Format“) sind praktisch alle
Börsen und Wallets abgedeckt, die diese Tools unterstützen; Koinly-Wallets werden zu Konten, Koinly-IDs werden über
`koinly_id` den Assets zugeordnet.

**Ablauf**

1. **Hochladen** (bis 25 MB): Format (automatisch), Konto (z. B. „Binance“), optional Zeitzone, Zahlenformat,
   Stichtag. Die Originaldatei bleibt gespeichert (Download jederzeit möglich).
2. **Vorschau:** jede Zeile als Buchung (Kauf, Verkauf, Tausch, Zu-/Abgang, Ertrag mit Tag wie `staking`,
   `interest`, `airdrop`, Gebühr) mit Status *neu*, *bereits importiert*, *mögliche Dublette*, *vor Stichtag*,
   *unvollständig* oder *ignoriert*.
3. **Zuordnen:** unbekannte Symbole einem vorhandenen Asset zuordnen, als neues Asset anlegen (Vorschlag für
   Name und CoinGecko-ID gängiger Coins) oder ignorieren; Konten der Datei auf Portfolia-Konten abbilden.
   Zuordnungen gelten für alle weiteren Importe.
4. **Übernehmen:** gültige Zeilen werden Journal-Buchungen (`PF-C-…`, Quelle „CSV · <Format>“, bearbeitbar).
   Offene Zeilen (z. B. ohne EUR-Wert) bleiben im Import und lassen sich später ergänzen und nachschieben.
5. **Rückgängig:** nimmt alle Buchungen eines Imports zurück; danach kann dieselbe Datei erneut (korrigiert)
   übernommen werden.

**Wichtige Regeln**

* **Überträge zwischen eigenen Konten:** Ein Abgang (z. B. Binance-Auszahlung) und ein passender Zugang auf einem
  anderen Konto (z. B. Ledger-Eingang) – in derselben Datei oder aus einem früheren Import – werden zu **einem
  Transfer** zusammengeführt (`PF-T-…`): Anschaffungsdatum und Einstand bleiben erhalten (Haltefrist!), die
  Differenz gilt als Netzwerkgebühr. Sicherheit „hoch“ (gleicher Transaktions-Hash oder ≥ 98 % der Menge innerhalb
  von 24 h) wird automatisch übernommen, „mittel“ (bis 72 h, ≥ 50 % der Menge) erst nach Bestätigung. Ein Transfer
  lässt sich unter *Buchungen* wieder auflösen. Passt ein Vorgang nur zu einer Buchung im kuratierten Import, gibt
  es einen Hinweis – Importbuchungen werden nie verändert.
* **EUR-Werte** (Käufe, Verkäufe, Tausch, Erträge brauchen einen): Eingabe → Fiat-Seite des Handels
  (EZB-Devisenkurs für Fremdwährungen) → Gegenwert laut Datei → Stablecoin (Marktkurs, sonst 1 USD bzw. 1 EUR) →
  gespeicherter Tageskurs des erhaltenen bzw. abgegebenen Assets → Transaktionskurs aus Import/Journal oder aus
  derselben Datei (± 31 Tage). Die Herkunft wird angezeigt und gespeichert. „Kurse laden“ holt Tageskurse und
  Devisenkurse für Assets und Zeitraum der Datei. Grenze: Die kostenlose CoinGecko-API liefert nur 365 Tage
  Historie – für ältere Krypto-Zeiträume ein Yahoo-Symbol hinterlegen (*Einstellungen → Kurse*) oder Werte
  eingeben.
* **Doppelte Zeilen:** Wiederholte oder überlappende Exporte derselben Quelle werden über die Kennung der Zeile
  erkannt (ID der Börse bzw. Prüfsumme). Gegen Import und andere Quellen wird auf gleichen Zeitpunkt (± Zeitzonen-
  versatz in ganzen Stunden, ± 10 Minuten) und gleiche Mengen (± 0,5 %) geprüft; auf demselben Konto werden solche
  Zeilen standardmäßig ausgelassen, auf anderen Konten nur markiert.
* **Stichtag:** Mit kuratiertem Import werden standardmäßig nur Zeilen **nach** dessen Stand (`valuation_date`)
  vorgeschlagen – ältere stehen dort bereits. Der Stichtag lässt sich je Import ändern oder leeren.
* **Interne Umbuchungen** eines Anbieters (Spot ↔ Earn/Staking/Funding, Kraken-Staking-Varianten wie `DOT.S`)
  werden übersprungen; der Bestand bleibt auf dem einen Konto des Anbieters.
* **Nicht unterstützt:** Futures, Margin, Optionen, NFTs (Zeilen werden gezählt und übersprungen). Umbuchungen
  in diese Bereiche gelten als intern – bei aktivem Derivatehandel können Bestände deshalb abweichen.
* Exportformate ändern sich gelegentlich; unbekannte Vorgänge erscheinen als „nicht lesbar“ mit Zeilennummer
  und können über „Eigenes Format“ oder manuell erfasst werden. Rückfragen zu Formaten bitte mit einer
  anonymisierten Beispielzeile.

---

## Berechnungen

* **Bestände** je Asset und Konto aus dem Ledger (inkl. Gebühren); `holdings_check` dient nur dem Abgleich.
* **Lots/FIFO:** global je Asset (Kontozuordnung wird bei Transfers konsistent gehalten) oder je Konto
  (Einstellung). Erträge (Staking etc.) werden mit dem Marktwert bei Zufluss als Lot angelegt.
* **Zahlungsströme:** Konten mit Cash-Führung (Fiat-Ein-/Auszahlungen im Import) werden automatisch erkannt;
  bei Depots ohne Verrechnungskonto gelten Käufe als Einzahlung und Verkaufserlöse als Auszahlung
  (übersteuerbar unter *Einstellungen → Ledger*).
* **TTWROR** (True Time-Weighted Rate of Return) täglich aus Snapshots:
  `r_t = (MV_t + Out_t) / (MV_{t−1} + In_t) − 1` (Zuflüsse zu Tagesbeginn, Abflüsse zu Tagesende);
  annualisiert erst ab einem Jahr.
* **IRR/XIRR** (geldgewichtet): Anfangswert als Einzahlung, datierte Ein-/Auszahlungen, Endwert als
  Auszahlung (Newton-Verfahren mit Bisektion als Rückfallebene).
* Zeiträume 1M, 3M, 6M, YTD, 1J, 3J, 5J, Max und frei wählbar; je Gesamtportfolio, Segment, Kategorie oder
  Position; Beitrag je Position (Wasserfall), Drawdown, Benchmarks.
* Historische Snapshots werden im Hintergrund aus der Kurshistorie aufgebaut; fehlende historische Kurse
  werden mit Transaktionskursen geschätzt und als „geschätzt“ gekennzeichnet.

---

## Sparpläne

Zwischen zwei Importen fehlen regelmäßige Käufe im Portfolio. Portfolia erkennt deshalb laufende Sparpläne
im Import und führt sie **geschätzt** fort. Geschätzte Buchungen liegen ausschließlich in der App-Datenbank
(Tabelle `tx_estimate`); der Import wird nie verändert und bleibt maßgeblich.

**Erkennung** (je Konto und Position, Käufe gegen Fiat bzw. per Lastschrift der letzten 14 Monate):

* Rhythmen: wöchentlich, 14-täglich, 2× monatlich, monatlich, zweimonatlich, vierteljährlich; mindestens
  3 Ausführungen in Folge. Einzelkäufe dazwischen stören nicht.
* Toleranzen: Ausführungstag ±2 (wöchentlich), ±3 (14-täglich) bzw. ±5 Tage (monatlich); Wochenend-Termine
  werden auf Montag verschoben. Sparrate: letzte Ausführungen weichen ≤ 25 % ab, eine einmalige Änderung
  der Rate (z. B. 100 → 150 €) wird erkannt.
* Übernommen werden typische Uhrzeit, Sparrate, Gebühr, Stückzahl-Genauigkeit (Nachkommastellen) und
  Finanzierung (Verrechnungskonto oder externe Lastschrift).
* Sicherheit **hoch** (≥ 6 Ausführungen, Rate ±2 %), **mittel** (≥ 4, ±10 %) oder **niedrig**. Automatisch
  geschätzt werden Pläne mit hoher/mittlerer Sicherheit; je Plan übersteuerbar (*an/aus/automatisch*).
* Status bezogen auf den Importstand: **läuft**, **ausgesetzt?** (ein Termin fehlt) oder **beendet**
  (≥ 2 Termine fehlen) – nur laufende Pläne werden fortgeführt.

**Schätzung** (morgens 06:40, abends 23:45, beim Start, nach jedem Import und per *Jetzt prüfen*):

* Für jeden Termin nach dem Importstand, dessen übliche Ausführungszeit erreicht ist, entsteht eine
  Buchung: Stück = Sparrate ÷ Kurs (abgerundet auf die übliche Genauigkeit). Kurs = Schlusskurs des Tages,
  am laufenden Tag vorläufig der aktuelle Kurs; vorläufige Kurse werden bis zum Schlusskurs aktualisiert.
* **Plausibilität:** weicht der Kurs um mehr als Faktor 2 vom letzten Kauf laut Import ab (falsche
  Kursreihe, Split, GBp/GBP), erscheint „Kurs prüfen“; „Alle freigeben“ fragt dann nach.
* Datum, Uhrzeit, Stückzahl und Kurs können von der echten Ausführung abweichen.

**Markierung und Freigabe** (*Mehr → Sparpläne*):

| Status | Im Portfolio | Markierung | Übergang |
|---|---|---|---|
| geschätzt | ja | Badge „geschätzt“ (Positionen, Detailansicht), Hinweis im Kopf, Warnung in *Steuern* | *Freigeben*, *Anpassen* (Datum, Uhrzeit, Stück oder Betrag, Kurs, Gebühr) oder *Verwerfen* |
| freigegeben | ja | keine (Detailansicht: „Sparplan, noch nicht im Import“) | nächster Import; fehlt die Buchung dort, obwohl der Import den Termin abdeckt: „fehlt im Import“ + Warnung, bleibt bis zum Entfernen (×) |
| durch Import ersetzt | nein | – | Import oder manuelle Buchung enthält die Ausführung (wöchentlich ±3, 14-täglich ±6, sonst ±7 Tage; Betrag oder Stück ±20 %) |
| nicht im Import | nein | – | nur ungeprüfte Schätzungen: Import deckt den Termin ab, Buchung fehlt |
| verworfen | nein | – | vom Nutzer verworfen oder Sparplan deaktiviert/ausgesetzt |

* Eine geänderte Sparrate gilt für alle noch nicht geprüften Schätzungen; spätere Termine nutzen sie ebenfalls.
* Freigegebene Buchungen lassen sich als CSV im Format von `transactions.csv` exportieren
  (`source=portfolia-sparplan`), z. B. zur Übernahme in den kuratierten Import.
* Steuern: Schätzungen bis einschließlich Berichtsjahr erzeugen im Steuerbericht eine Warnung, freigegebene
  Buchungen außerhalb des Imports einen Hinweis – für die Steuererklärung den Import mit den echten
  Abrechnungen verwenden.

**Grenzen:** Sparpläne gegen Krypto (z. B. USDC → BTC), Entnahmepläne und Sparpläne mit dynamischer Rate
werden nicht erkannt. Wird ein Sparplan nach dem Importstand ausgesetzt, entstehen Schätzungen, bis sie
verworfen oder durch den nächsten Import als „nicht im Import“ entfernt werden.

---

## Kurse und Datenquellen

| Quelle | Verwendung | Grenzen/Verhalten |
|---|---|---|
| Yahoo Finance (yfinance) | Aktien/ETFs aktuell, intraday, Historie, Stammdaten | gebündelt, ≤ 1 Anfrage/s |
| CoinGecko (Demo/Pro) | Krypto aktuell (alle Coins in **einem** `/simple/price`-Aufruf), Historie `market_chart` | Monatsbudget (Standard 10.000) sichtbar, Drosselung ab 80 %; Demo-API: Historie max. 365 Tage, davor Yahoo-Paare (z. B. BTC-EUR) laut Einstellung |
| EZB (Frankfurter, EZB-ZIP als Fallback) | Devisenkurse für EUR-Umrechnung | täglich |
| `manual_prices.csv` | Assets ohne Kursquelle | letzter Kurs ≤ Stichtag |

* Jeder Kurs trägt Zeitstempel und Quelle. **Veraltet** gilt ein Kurs nach > 24 h (Aktien, nur an
  Handelstagen) bzw. > 1 h (Krypto) – deutlich markiert, nie still durch 0 ersetzt.
* Fällt eine Quelle aus, bleibt der letzte Wert mit Warnung stehen; die Quelle wird mit exponentiellem
  Backoff erneut versucht.
* Zeitplan: Krypto alle 10 min, Aktien alle 15 min zu EU/US-Handelszeiten, EZB 16:35, Snapshot 23:30,
  Historien-Nachladen 06:10.

---

## News und YouTube

Konfiguration in `/data/sources.yaml` (beim ersten Start aus dem Beispiel angelegt; Kommentare bleiben beim
Speichern aus der Oberfläche erhalten).

* **Quellen:** Feeds je Asset (Google News, Yahoo, Finnhub), Agenturen/Portale, Krypto-Portale,
  Börsenankündigungen (Binance, Bybit – gefiltert auf gehaltene Assets). URLs und Handles werden **zur
  Laufzeit verifiziert**; unerreichbare Quellen werden deaktiviert und unter *News → Quellen* mit Grund
  gemeldet, nie still entfernt.
* **YouTube:** Kanäle per Handle; Auflösung zur Kanal-ID (API oder Kanalseite) mit Anzeige von Name und
  Abonnenten zur Bestätigung; nicht auflösbare Handles werden deaktiviert (nie geraten). Discovery (mit
  `YOUTUBE_API_KEY`): täglich Vorschläge ab 100.000 Abonnenten, Aktionen *abonnieren/ignorieren/blockieren*
  werden in `sources.yaml` gespeichert. Shorts ausgeblendet, Livestreams markiert.
* **Relevanz:** Aliase mit Wortgrenzen, mehrdeutige Ticker (z. B. SUI, TAP, PI, NIGHT) nur mit Kontextwort,
  Gewichtung nach Quelle (Primär 1,0 · Agenturen/Portale 0,8 · Krypto-Portale 0,7 · YouTube 0,6 ·
  entdeckt 0,4) und Positionsgröße, Aktualität (Halbwertszeit 48 h). Dubletten über URL und Titelähnlichkeit;
  Aufbewahrung 90 Tage; Abruf alle 30 Minuten.
* **Optional KI** (Anthropic Claude, standardmäßig **aus**): Kurz-Zusammenfassungen und Tagesdigest mit hartem
  Tages-Token-Budget. Gesendet werden ausschließlich Asset-Namen und Artikeltexte.

---

## Steuern und Haltefristen

> **Informativ – keine Steuerberatung.** Maßgeblich sind die amtlichen Vordrucke bzw. ELSTER und die
> Steuerbescheinigungen der Banken. Bitte vor der Abgabe prüfen.

### Seite „Steuern & Haltefristen“

* Kryptowerte: steuerfrei veräußerbarer Wert, Wert und unrealisierter Gewinn in Haltefrist, nächste
  Freigaben (12 Monate, Diagramm und Liste), Haltefristen je Kryptowert und Wallet.
* Realisierte Ergebnisse je Jahr (bis/über 1 Jahr Haltedauer), Freigrenzen-Status, Leistungen nach
  § 22 Nr. 3 EStG, Kapitalerträge nach Töpfen.
* Steuerjahr: Zusammenfassung, Frei-/Pauschbeträge, vereinfachte Schätzung, **Übertragungshilfe** für die
  Formulare, alle Aufstellungen, Hinweise, Methodik.

### PDF-Berichte „per Knopfdruck“

Für ein Jahr erzeugt Portfolia:

* **Steuerreport (vollständig)** – Deckblatt, Zusammenfassung, Übertragungshilfe, alle Aufstellungen,
  Datenqualität, Methodik, Parameter, Rechtsgrundlagen.
* **Aufstellung zur Anlage SO** – private Veräußerungsgeschäfte mit Kryptowerten (je Lot: Anschaffung,
  Veräußerung, Veräußerungspreis, Anschaffungs- und Werbungskosten, Gewinn/Verlust) und Leistungen.
* **Aufstellung zur Anlage KAP / KAP-INV** – Wertpapierveräußerungen, Dividenden/Ausschüttungen,
  Quellensteuer, Vorabpauschalen je Depot.

Die Aufstellungen sind als **Beleg** gedacht (Einreichung mit der Erklärung bzw. über die
ELSTER-Belegnachreichung oder auf Anforderung des Finanzamts). Die Erklärung selbst wird in ELSTER bzw. in
den amtlichen Vordrucken ausgefüllt – dafür nennt die Übertragungshilfe die Beträge je Formularfeld.
Zeilennummern erscheinen nur, wenn sie für das Jahr in den Steuerparametern hinterlegt sind (sie ändern sich
jährlich). Mitgeliefert sind die übereinstimmend belegten Zeilen für **2024** (Anlage SO „andere
Wirtschaftsgüter“ Z. 42–47, Anlage KAP Z. 19/20/22/23/41, Anlage KAP-INV Z. 4–13) und **2025** (Anlage SO,
neuer Abschnitt „Kryptowerte“ Z. 48–51). Anlage KAP/KAP-INV 2025 wurden umgebaut; dazu widersprechen sich die
verfügbaren Quellen, daher dort nur Feldbezeichnungen. Die Zeilen stammen aus Sekundärquellen – vor der Abgabe
mit dem amtlichen Vordruck abgleichen. Name, Steuer-ID und Steuernummer für das Deckblatt sind optional und
bleiben lokal.

### Regelwerk Deutschland (Kurzfassung)

* **§ 23 EStG (Kryptowerte):** Veräußerung/Tausch innerhalb eines Jahres steuerpflichtig; Frist nach
  §§ 187, 188 BGB – steuerfrei ab dem Tag nach dem Jahrestag (Anschaffung am 29.02. → Fristende 28.02.).
  Verbrauchsfolge FIFO je Wallet (BMF, walletbezogen) oder global. Freigrenze 600 € bis 2023, 1.000 € ab
  2024 auf den Jahressaldo; Verlustvortrag als Eingabe.
* **§ 22 Nr. 3 EStG:** Staking, Lending u. Ä. mit dem Wert bei Zufluss, Freigrenze 256 €; Einstufung je Tag
  wählbar (z. B. Airdrop, Mining, Cashback). Zugeflossene Einheiten gelten als angeschafft.
* **Gebühren:** Krypto-Gebühren beim Handel standardmäßig als Veräußerung, Netzwerkgebühren bei Transfers
  zwischen eigenen Wallets standardmäßig nicht steuerbar (beides umstellbar). Fehlende Anschaffungsdaten
  werden konservativ mit 0 € Anschaffungskosten und als steuerpflichtig angesetzt.
* **Kapitalerträge:** Aktien-Topf (Verluste nur mit Aktiengewinnen verrechenbar), sonstige Erträge,
  Investmentfonds mit Teilfreistellung (30/15/60/80 %), Vorabpauschale (Basiszins je Jahr, anteilig im
  Erwerbsjahr, Abzug bei Veräußerung), anrechenbare Quellensteuer (Deckel 15 %), Sparer-Pauschbetrag
  (801/1.602 € bis 2022, 1.000/2.000 € ab 2023). Depots mit inländischem Steuerabzug werden nur nachrichtlich
  ausgewiesen; die Einstufung je Depot und die Fondsart je Wertpapier lassen sich unter *Zuordnung* setzen.

Alle Annahmen stehen im Bericht unter „Methodik und Annahmen“; alle Wahlrechte sind Optionen der Seite.

### Regeln aktualisieren oder austauschen

Das Steuermodul ist bewusst modular (Details: [`docs/tax-rulepacks.md`](docs/tax-rulepacks.md)):

* **Regelwerke** liegen als Pakete unter `app/tax/packs/<id>/` (derzeit `de` und das länderneutrale
  `neutral`); Auswahl auf der Steuerseite (`auto` = nach Zeitzone).
* **Parameter je Veranlagungsjahr** (Freigrenzen, Pauschbeträge, Basiszins, Teilfreistellung, Formularfelder)
  stehen in `app/tax/packs/<id>/params.yaml`. Ohne Programm-Update lassen sie sich über
  **`/data/tax_rules/<id>.yaml`** ergänzen oder überschreiben – z. B. der Basiszins eines neuen Jahres:

  ```yaml
  pack: {reviewed_through: 2027}
  per_year:
    basiszins: {2027: 0.0300}   # Beispielwert – amtlichen Wert des BMF eintragen
  forms:
    2026:
      anlage_so: {fields: {so_23_gain: {line: "51"}}}   # nur geprüfte Zeilennummern eintragen
      anlage_kap_inv: {fields: {inv_vp: {lines: {etf_equity: "9"}}}}   # KAP-INV: Zeile je Fondsart
  ```

  Fehlerhafte Overrides werden ignoriert und auf der Steuerseite gemeldet. Jeder Bericht dokumentiert
  Regelwerk-, Parameter-Version und einen Fingerabdruck der verwendeten Parameter.

---

## Einstellungen, Sicherheit, Backups

* **Einstellungen:** Darstellung (Schwelle „Sonstige“, Standardzeitraum), Ledger (FIFO-Bereich,
  Cash-Führung je Konto), Kurse (Veraltungsgrenzen, CoinGecko-Budget, Benchmarks, Krypto-Historien-Fallback),
  News/KI, Backups, ZIP-Sicherungen.
* **Zugriff:** Nur im LAN betreiben. Optional Basic-Auth (`AUTH_MODE=basic`, PBKDF2- oder bcrypt-Hash,
  Sperre nach 10 Fehlversuchen in 5 Minuten) oder hinter einem Reverse-Proxy mit eigener Anmeldung
  (`FORWARDED_ALLOW_IPS`, ggf. `ROOT_PATH`).
* **Härtung:** CSRF-Schutz (Double-Submit-Cookie, `Sec-Fetch-Site`), Content-Security-Policy ohne externe
  Quellen außer YouTube-Vorschaubildern (`i.ytimg.com`; andere Bilder werden serverseitig mit SSRF-Schutz
  zwischengespeichert), `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, begrenzte Anfragegröße
  (Formulare 1 MB, CSV-Upload 25 MB – auch ohne `Content-Length`),
  PDF-Downloads mit `Cache-Control: no-store`, Container ohne Root-Rechte und ohne Capabilities.
* **Backups:** täglich (Uhrzeit einstellbar, Standard 03:15) über die SQLite-Online-Backup-API mit
  Integritätsprüfung, gzip-komprimiert nach `/data/backups`, Aufbewahrung 14 Stück (einstellbar); manuell
  unter *Einstellungen → Backups* oder per `docker exec -u 99:100 portfolia python -m app backup`.
  **Wiederherstellen:** Container stoppen, Sicherung entpacken (`gunzip`), als `/data/app.sqlite` ablegen,
  `app.sqlite-wal`/`-shm` entfernen, Container starten. **Wichtig:** In Portfolia erfasste und per CSV
  importierte Buchungen und Assets existieren nur in der App-Datenbank – deshalb zusätzlich:
* **ZIP-Sicherungen im Import-Format:** Nach jeder Änderung an Buchungen (manuell, CSV-Import, Sparplan-
  Freigabe, neuer Import, Assets) schreibt Portfolia – gebündelt nach zwei Minuten ohne weitere Änderung – eine
  datierte Datei `portfolia-export-JJJJ-MM-TT_HHMMSS.zip` nach `EXPORT_DIR` (Standard `/data/exports`), nur wenn
  sich der Inhalt geändert hat. Jede Datei ist ein vollständiger kuratierter Import (Schema 1.1) und lässt sich
  direkt in den Importordner legen; Journal-Buchungen werden dabei über ihre `tx_id` erkannt (keine Doppelzählung).
  Jede erfolgreich importierte ZIP-Datei wird zusätzlich mit Datum unter `EXPORT_DIR/import-archiv/` abgelegt.
  Aufbewahrung (Standard 30 Sicherungen, 20 Import-Kopien), manuelles Sichern und Download unter
  *Einstellungen → ZIP-Sicherungen*. Empfehlung: `EXPORT_DIR` auf eine Freigabe mit eigener Sicherung legen.

---

## Betrieb und Fehlerbehebung

* **Ressourcen:** Image ca. 280 MB (unkomprimiert),
  Speicherbedarf im Leerlauf ca. 100–180 MB (je nach Portfoliogröße), kurzzeitig mehr beim erstmaligen
  Laden der Historie – ein Speicherlimit sollte nicht unter 512 MB liegen. Python legt seinen Bytecode beim
  ersten Start unter `/data/cache/pyc` ab (ca. 20 MB, darf jederzeit gelöscht werden); Kaltstart ca. 4 s,
  danach ca. 1,5 s.
  Lasttest mit 5.725 Transaktionen / 170 Assets: Import 0,4 s, Dashboard ≤ 1,2 s beim ersten Aufruf nach
  einer Neuberechnung (danach ≈ 0,01 s), Detail-Panel ≤ 0,3 s, PDF-Bericht < 1 s (`python scripts/bench.py`).
* **Logs:** `docker logs portfolia` (JSON). Warnungen und Fehler zusätzlich unter *Datenqualität →
  Fehlerprotokoll*; API-Schlüssel werden in Logs maskiert.
* **Gesundheit:** `GET /healthz` (`{"status": "ok"}`), Docker-`HEALTHCHECK` integriert.

| Problem | Ursache / Lösung |
|---|---|
| Import wird nicht übernommen | *Datenqualität → Import*: Fehlerbericht mit Datei, Zeile, Spalte. Der alte Stand bleibt aktiv. |
| Asset „unbewertet“ | Keine Kursquelle/ID oder Quelle nicht erreichbar → `quote_id` prüfen oder `manual_prices.csv` nutzen. |
| Kurs „veraltet“ | Quelle nicht erreichbar oder Budget gedrosselt → *Datenqualität → Datenquellen & Kontingente*. |
| Krypto-Historie vor 365 Tagen fehlt | CoinGecko-Demo-Grenze → Yahoo-Paar unter *Einstellungen → Kurse* zuordnen (nur eindeutige Paare). |
| News-Quelle deaktiviert | *News → Quellen*: Grund und letzter Fehler; URL in `sources.yaml` korrigieren und reaktivieren. |
| YouTube-Handle nicht auflösbar | Handle prüfen (mit `@`), ggf. `YOUTUBE_API_KEY` setzen; nicht auflösbare Kanäle bleiben deaktiviert. |
| Vorabpauschale „Kurse fehlen“ | Kurshistorie des Fonds laden (*Datenqualität → Historie nachladen*) oder Basiszins im Override ergänzen. |
| Zugriff verweigert (403) | CSRF-Schutz: Seite neu laden; Cross-Site-Formulare werden abgelehnt. |

---

## Entwicklung

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
DATA_DIR=./dev-data/data IMPORT_DIR=./dev-data/import DEMO_MODE=true LOG_FORMAT=text python -m app
pytest              # Unit-, Integrations- und Abnahmetests
ruff check app tests
python scripts/bench.py   # Lasttest (≈5.700 Transaktionen)
```

Struktur:

```
app/
  importer/   Datenvertrag, Validierung, atomarer Import, Diff, Beispiel-ZIP
  ledger/     Engine: Bestände, Lots (FIFO/LIFO/HIFO), Veräußerungen, Erträge, Zahlungsströme
  prices/     Yahoo, CoinGecko, EZB, Demo; Kurs-Store und Veraltungslogik
  analytics/  Bewertung, Allokation, Historie, TTWROR/IRR, Zeiträume, Farben
  news/       Quellen, Relevanz, Dedupe, YouTube, optionales LLM
  tax/        Steuer-Schnittstelle, Registry, Parameter, PDF-Renderer, Regelwerke (packs/de, packs/neutral)
  jobs/       Scheduler, Jobs, Wartung (Backups)
  web/        FastAPI-App, Sicherheit, Routen; templates/ + static/ (HTMX, ECharts lokal gebündelt)
docker/       Entrypoint (PUID/PGID), Healthcheck
unraid/       Community-Applications-Template
examples/     Beispiel-Import, sources.yaml
docs/         Meilensteine, Steuer-Regelwerke, Farbpalette
tests/        pytest (inkl. synthetischer Großimport)
```

CI (GitHub Actions): Lint und Tests bei jedem Push/PR; Image-Build und Veröffentlichung nach GHCR
(`ghcr.io/pneumann1980/portfolia`) für `main` und Versions-Tags.

---

## Grenzen und Lizenz

Bekannte Grenzen (Auswahl, vollständig in [`docs/MILESTONES.md`](docs/MILESTONES.md)):

* Nur Basiswährung EUR; Fremdwährungsgewinne (§ 23 EStG) werden nicht ermittelt.
* Vorabpauschale mit Börsenschlusskursen statt Rücknahmepreisen; Altanteile vor 2018 nicht berücksichtigt.
* Formularzeilen nur für 2024 und Anlage SO 2025 hinterlegt (aus Sekundärquellen, ohne Gewähr); für andere
  Jahre nennt die Übertragungshilfe nur die Feldbezeichnungen.
* Datenquellen sind inoffiziell (Yahoo) bzw. limitiert (CoinGecko Demo); Ausfälle werden sichtbar markiert.

**Lizenz:** Portfolia steht unter der [MIT-Lizenz](LICENSE) – Nutzung, Änderung und Weitergabe (auch
kommerziell) sind erlaubt, solange Copyright- und Lizenzhinweis erhalten bleiben; keine Gewährleistung.

Drittkomponenten im Image (Details und vollständige Paketliste: [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)):
Apache ECharts 6.1 (Apache-2.0, inkl. NOTICE; enthaltene d3-Teile BSD-3-Clause) und htmx 2.0 (0BSD) –
Lizenztexte unter `app/static/vendor/`; Bitstream Vera Fonts (über ReportLab) für PDFs; Python-Pakete u. a.
unter MIT, BSD, Apache-2.0, PSF-2.0 und MPL-2.0 (certifi, unverändert). Alle sind mit der MIT-Lizenz vereinbar.
