# Portfolia

Selbst gehostetes Portfolio-Dashboard für Aktien, ETFs und Kryptowerte – ein Docker-Image für Unraid
(6.12+/7.x), nur für das lokale Netz. Portfolia liest einen kuratierten Import (ZIP) **read-only**, bewertet
die Positionen mit öffentlichen Kursquellen, berechnet Performance (TTWROR/IRR nach der Methodik von
Portfolio Performance), zeigt Haltefristen und erstellt Steueraufstellungen als PDF. Passende News und
YouTube-Videos werden je Position gefiltert.

* **Datenquellen:** Buchungen stammen aus dem kuratierten Import (ZIP, **read-only**, wird nie verändert),
  werden direkt in Portfolia erfasst (siehe [Buchungen erfassen](#buchungen-in-portfolia-erfassen)) oder aus
  **CSV-Exporten von Börsen, Wallets und Steuertools** übernommen (siehe [CSV-Import](#csv-import-aus-börsen-und-wallets))
  – auch ganz ohne Import. Börsen und öffentliche Wallet-Adressen lassen sich als [Datenquellen](#datenquellen-börsen-und-wallet-adressen)
  anlegen; **Bitpanda** wird read-only per API-Key synchronisiert (Schlüssel in der App eingegeben, verschlüsselt
  gespeichert, jede Buchung vor der Übernahme prüfbar). In der App erfasste
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
6. [Datenquellen: Börsen und Wallet-Adressen](#datenquellen-börsen-und-wallet-adressen)
7. [Berechnungen](#berechnungen)
8. [Sparpläne](#sparpläne)
9. [Kurse und Datenquellen](#kurse-und-datenquellen)
10. [News und YouTube](#news-und-youtube)
11. [Steuern und Haltefristen](#steuern-und-haltefristen)
12. [Einstellungen, Sicherheit, Backups](#einstellungen-sicherheit-backups)
13. [Betrieb und Fehlerbehebung](#betrieb-und-fehlerbehebung)
14. [Entwicklung](#entwicklung)
15. [Grenzen und Lizenz](#grenzen-und-lizenz)

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
   | `/run/secrets/portfolia` | `/boot/config/portfolia` | **ro** | Master-Key (`master.key`) für in der App gespeicherte API-Keys von Datenquellen – bewusst außerhalb von appdata (siehe [Master-Key](#master-key-für-api-keys)) |

3. Optional API-Schlüssel für Kurse und News eintragen (Umgebungsvariablen, nie angezeigt oder geloggt).
4. Nur für Datenquellen mit API-Key (Bitpanda): einmalig den Master-Key anlegen (Unraid-Terminal), danach bleibt
   er unverändert – Einzelheiten, Backup und Rotation unter [Master-Key](#master-key-für-api-keys):
   ```sh
   mkdir -p /boot/config/portfolia
   openssl rand -base64 32 > /boot/config/portfolia/master.key
   ```
5. Container starten, Weboberfläche über *WebUI* öffnen (Port 8080).

**Aktualisieren:** *Docker → portfolia → Update* (bzw. *Check for Updates*). Zeigt Unraid „not available“, hilft
*Advanced View* → *force update* oder *Edit → Apply* (lädt `latest` neu und erstellt den Container neu; Daten in
`/data` und `/exports` bleiben erhalten). Images ab 0.9.0 werden als Docker-Manifestliste veröffentlicht, damit die
Update-Prüfung von Unraid funktioniert (OCI-Indizes mit Attestierungen erkennt sie nicht).

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
      PORTFOLIA_MASTER_KEY_FILE: /run/secrets/portfolia_master_key   # nur für API-Keys von Datenquellen
    secrets: [portfolia_master_key]
    volumes:
      - ./data:/data
      - ./import:/import:ro
      - ./exports:/exports
    restart: unless-stopped

secrets:
  portfolia_master_key:
    file: ./secrets/master.key   # einmalig: umask 077; openssl rand -base64 32 > secrets/master.key
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
| `PORTFOLIA_MASTER_KEY_FILE` | – | Datei mit dem [Master-Key](#master-key-für-api-keys) (32 zufällige Bytes, Base64 oder Hex) für in der App gespeicherte API-Keys; Unraid-Template: `/run/secrets/portfolia/master.key` |
| `PORTFOLIA_MASTER_KEY` | – | alternativ der Master-Key selbst (sichtbar in `docker inspect` – Datei bevorzugen) |
| `PORTFOLIA_MASTER_KEY_OLD_FILE` / `_OLD` | – | nur während einer Rotation: der bisherige Master-Key |
| `PORTFOLIA_DS_<NAME>` | – | bisheriger Weg für Zugangsdaten einer [Datenquelle](#datenquellen-börsen-und-wallet-adressen) (API-Schlüssel mit Leserechten), alternativ `PORTFOLIA_DS_<NAME>_FILE` = Pfad einer Secret-Datei; in Portfolia steht nur der Variablenname. Funktioniert weiter, ein in der App gespeicherter Schlüssel hat Vorrang. |

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
* `manual_prices.csv`: `asset_id, date, price_eur` (+ `source`) für Assets ohne Marktkurse. Ein manueller Kurs
  gilt bis zum nächsten Kurs, nach dem letzten höchstens 30 Tage (Krypto) bzw. 365 Tage (Wertpapiere) – siehe
  [Ersatzkurse](#kurse-und-datenquellen). Ohne gültigen Kurs bleibt ein Asset sichtbar „unbewertet“ mit 0 €,
  nie still ignoriert.
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
  späterer Import dieselbe `tx_id` oder dieselbe Anbieter-ID (`source_ref`, siehe
  [Abgleich](#doppelzählung-zwischen-kuratiertem-import-und-app-buchungen)), gilt die Import-Buchung (keine
  Doppelzählung); ähnliche Buchungen mit anderer ID landen unter *Buchungen → Abgleich mit dem Import* zur
  Entscheidung. Manuell erfasste Sparplan-Ausführungen ersetzen passende Schätzungen.
* **Gesamtexport:** Import + manuelle und per CSV importierte Buchungen + freigegebene Sparplan-Ausführungen als
  Import-ZIP (Schema 1.1, mit aktuellem Bestand als `holdings_check`, steuerlichen Einstufungen als `tax_type` bzw.
  `tax_withholding` und allen Konten) – als Sicherung, zum Umzug oder als neuer kuratierter Import. Zusätzlich
  entsteht nach jeder Änderung automatisch eine datierte Kopie (siehe [Backups](#einstellungen-sicherheit-backups)).
* **Ohne Import:** Alle Ansichten (Positionen, Performance, Steuern, Sparpläne) funktionieren auch nur mit
  manuell erfassten Buchungen. Steuerberichte weisen manuell erfasste Buchungen des Jahres aus.

### Positionen ausbuchen (Verlust, Diebstahl)

*Buchungen → Ausbuchen* (auch aus der Übersicht, der Performance-Seite und der Detailansicht einer Position)
bucht den **gesamten Bestand** eines Kontos als Abgang ohne Gegenwert – einzeln oder gesammelt:

* Standardansicht: alle gehaltenen Bestände **ohne gültigen Kurs** mit Grund (z. B. „manueller Kurs vom 30.10.2025
  ist älter als 30 Tage“), Einstand und letzter Buchung; umschaltbar auf alle Bestände und je Konto filterbar.
* Art: Verlust/Totalverlust (`lost`), Diebstahl (`stolen`) oder Burn (`burn`); Datum frei wählbar, aber nicht vor
  der letzten Buchung der Position (sonst stimmte die Menge nicht). Gebucht wird um 23:59 Uhr des Tages.
* Wirkung: Der Einstand wird als realisierter Verlust erfasst, die Position verschwindet aus Bestand, Übersicht
  und Warnungen; in der Historie fällt ihr Wert am Buchungstag auf 0 €.
* Jede Ausbuchung ist eine normale Buchung (`PF-M-…`, Wert 0 €): im Journal bearbeit- und löschbar, im
  ZIP-Export enthalten. Sammel-Ausbuchungen lassen sich auf derselben Seite ganz oder teilweise zurücknehmen.
* Steuer: Kryptowerte nach der Einstellung „Verlust/Diebstahl von Kryptowerten“ (Standard: keine Veräußerung),
  Wertpapiere erscheinen als „Ausbuchung/Verlust“; maßgeblich bleibt der Beleg der Bank. Keine Steuerberatung.

---

## CSV-Import aus Börsen und Wallets

*Buchungen → CSV importieren* liest CSV-Exporte ein und übersetzt sie in das einheitliche Buchungsformat des
Datenvertrags – aus Dateien, die du selbst exportierst. Automatische Abrufe (derzeit Bitpanda) laufen über
[Datenquellen](#datenquellen-börsen-und-wallet-adressen) auf demselben Weg.

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
  erkannt (ID der Börse bzw. Prüfsumme). **Dasselbe Ereignis aus einer anderen Quelle** (z. B. CSV-Import und
  Datenquelle derselben Börse) wird exakt über die **Anbieter-ID** erkannt – Kraken `refid` bzw. Ledger-ID,
  Coinbase-ID, Bitpanda-Transaktions-, Trade- bzw. Vorgangs-ID – und gilt als *bereits vorhanden*: Es geht nicht
  erneut in Bewertung und Lots ein, die vorhandene Buchung behält ihre Herkunft. Wallet-Exporte (Ledger Live,
  Trezor, Electrum, Exodus) werden über den Transaktions-Hash nur als *mögliche Dublette* markiert, sofern Art und
  Buchungsseite übereinstimmen (ein Hash kann mehrere Buchungen betreffen). Gegen Import und andere Quellen wird
  zusätzlich auf gleichen Zeitpunkt (± Zeitzonenversatz in ganzen Stunden, ± 10 Minuten) und gleiche Mengen
  (± 0,5 %) geprüft; auf demselben Konto werden solche Zeilen standardmäßig ausgelassen, auf anderen Konten nur
  markiert – zusammengeführt wird nie allein wegen Ähnlichkeit.
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

## Datenquellen: Börsen und Wallet-Adressen

*Einstellungen → Datenquellen* verwaltet Börsenkonten und öffentliche Wallet-Adressen als Quellen für Buchungen:
anlegen, ansehen, bearbeiten, deaktivieren und entfernen – auch auf dem Smartphone.

> **Stand 0.11:** Automatische Anbindung für **Bitpanda** (Public API, ausschließlich lesend). Sie ist mit
> anonymisierten Testdaten (Fixtures) geprüft, **noch nicht mit einem echten Bitpanda-Konto** – siehe
> [Grenzen der Bitpanda-Anbindung](#grenzen-der-bitpanda-anbindung). Alle anderen Börsen und Chains zeigen
> ehrlich **„Manuell / noch nicht unterstützt“** und verweisen auf den CSV-Import.

**Datensatz:** Art (Börse oder Wallet-Adresse), Anbieter bzw. Chain, frei wählbarer Name, Konto in Portfolia (auf
das gebucht wird – bei vorhandenen Buchungen aus Import oder CSV dasselbe Konto wählen), öffentliche Adresse bzw.
xpub (formal geprüft; private Schlüssel und Seed-Phrasen werden abgelehnt, weder gespeichert noch zurückgespielt),
API-Key (verschlüsselt, siehe unten) mit optionalem Ablaufdatum, Synchronisierungsintervall (nur manuell,
stündlich, alle 6/12 Stunden, täglich), automatische Übernahme (Standard: aus), Status, letzter Lauf, letzter
erfolgreicher Lauf, letzter Fehler, nächster Lauf, Abdeckung des letzten Abrufs und Laufhistorie.

**Status:** *angelegt* · *verbunden* (Verbindungsprüfung erfolgreich, noch nicht synchronisiert) ·
*synchronisiert* (letzter Abruf nachweislich vollständig) · *teilweise synchronisiert* (Seitenende oder Abdeckung
unklar, Drosselung, Teilfehler – eine erfolgreiche HTTP-Antwort allein genügt nicht; der nächste Lauf holt erneut
ab) · *Fehler* mit verständlicher Meldung, z. B. „API-Key abgelaufen“, „Berechtigung fehlt“, „Anbieter drosselt
Anfragen (HTTP 429)“, „Anbieter vorübergehend nicht erreichbar“. *Deaktiviert* stoppt nur den Zeitplan.

### Bitpanda einrichten

Alles geschieht in der App; einmalige Voraussetzung ist der [Master-Key](#master-key-für-api-keys).

1. **API-Key bei Bitpanda erstellen** (*Profil → API-Key*, app.bitpanda.com/my-account/apikey, Reiter „Bitpanda“) –
   nur Leserechte:

   | Recht bei Bitpanda | Bedarf | Wofür |
   |---|---|---|
   | **Transaction** (lesen) | **erforderlich** | Vorgänge (`GET /operations`) |
   | **Balance** (lesen) | optional | Bestandsprüfung (`GET /portfolio/holdings`) – nur Hinweis, nie Buchung |
   | Trade (Read) | nicht angefordert | Asset-Stammdaten (`GET /assets`, `/currencies`) – ob sie ohne weiteres Recht lesbar sind, zeigt „Verbindung testen“ |
   | **Trade (Write) / Trading, Earn (Write)** | **nie aktivieren** | Portfolia handelt nie und ruft keine schreibenden Endpunkte auf |

   Ein Ablaufdatum setzen (z. B. 12 Monate) und in Portfolia eintragen: Die App warnt 14 Tage vorher und ruft
   nach Ablauf nicht mehr ab.
2. *Einstellungen → Datenquellen → + Börse*: Anbieter **Bitpanda**, Name, Konto, API-Key einfügen, optional
   Ablaufdatum → **Anlegen**. Synchronisierung zunächst auf „nur manuell“ lassen.
3. **Verbindung testen** prüft Vorgänge, Bestände und Asset-Stammdaten einzeln und unterscheidet – soweit Bitpanda
   es erkennen lässt – ungültigen bzw. widerrufenen Schlüssel (401), fehlendes Leserecht (403 bzw. 401 bei
   lesbaren Beständen), abgelaufenen Schlüssel (Datum bzw. Meldung), Drosselung (429) und vorübergehende
   Störungen (5xx, Zeitüberschreitung). Der Test geht ausschließlich an die dokumentierte Bitpanda-API.
4. **Historischen Abgleich starten**: holt die gesamte Historie und legt einen Prüf-Stapel an. Jeder Vorgang
   erscheint als *neu*, *bereits vorhanden* (gleiche Anbieter-ID, z. B. aus einem Bitpanda-CSV-Import),
   *mögliche Dublette*, *vor Stichtag* (mit kuratiertem Import: bis zu dessen Stand – dort bereits enthalten),
   *unvollständig* (z. B. EUR-Wert fehlt), *ungeklärt* (mit Grund) oder *ignoriert*.
5. **Prüfen und übernehmen** – einzeln oder alle gültigen. Ungeklärte Vorgänge manuell erfassen, per CSV
   nachziehen oder **dauerhaft ignorieren**; eine offene Zeile hält die übrigen nicht auf.
6. Danach auf **stündlich** stellen. Optional „eindeutige neue Vorgänge automatisch übernehmen“.

**Abbildung der Bitpanda-Vorgänge** – nur eindeutige Fälle werden gebucht:

| Bitpanda | Portfolia | Bedingung |
|---|---|---|
| Kauf Krypto gegen Fiat, auch Sparplan | Kauf, Wert = Fiat-Betrag | genau ein Fiat-Ausgang und ein Krypto-Eingang |
| Verkauf Krypto gegen Fiat | Verkauf, Wert = Fiat-Betrag | genau ein Krypto-Ausgang und ein Fiat-Eingang |
| Einzahlung Fiat oder Krypto | Zugang | ein Eingang, Vorgangsart „deposit“ |
| Auszahlung Fiat oder Krypto | Abgang inkl. Gebühr | ein Ausgang, Vorgangsart „withdraw…“ |
| Reward, Staking-Reward | Zugang mit Ertrags-Tag `reward` bzw. `staking` | ein Krypto-Eingang, Vorgangsart genau „reward“/„staking reward“ |
| eigener Gebühren-Teil (z. B. in BEST) | Gebührenzeile desselben Vorgangs | Transaktionsart „fee“ |
| Gebühr an einem Haupt-Teil | Gebühr an der Buchung, **als prüfbedürftig markiert** | ob der Betrag die Gebühr enthält, ist nicht dokumentiert |
| interne Umbuchung (gleiches Asset und gleicher Betrag ein und aus) | keine Buchung, im Lauf gezählt | – |

**Bewusst nicht automatisch – „ungeklärt“ mit Grund:** Korrekturen und Stornos (`compensates`) samt dem
stornierten Vorgang, Tausch Krypto → Krypto (kein EUR-Gegenwert in den API-Daten), Fiat → Fiat, Aktien und ETFs
(Bitpanda Stocks), Edelmetalle, Kryptoindizes, unbekannte Assets oder Vorgangsarten sowie Vorgänge ohne Zeitpunkt
oder Richtung. Sie werden weder still verworfen noch als Kauf oder Verkauf geraten.

**Technik und Aufwand:** nur `GET` an `https://api.public.bitpanda.com/v1` mit Header `x-api-key` – keine
schreibenden Aufrufe, kein stiller Rückgriff auf die ältere API `api.bitpanda.com`, Umleitungen werden nicht
verfolgt. Cursor-Pagination (100 je Seite) mit Schutz gegen Schleifen und unklares Seitenende; Folgeläufe fragen
nur ab dem letzten vollständigen Stand (minus 2 Tage Überlappung) ab, Asset-Stammdaten werden 30 Tage
zwischengespeichert – ein stündlicher Lauf braucht meist ein bis zwei Aufrufe. Timeouts 20 s, bei 429 Warten nach
`Retry-After` (höchstens 60 s je Wartezeit, 120 s je Lauf), bei 5xx drei Versuche. Beträge exakt als Dezimalzahl,
Zeitpunkte in UTC; Originalbeträge, Währungen, Gebühren, Bitpanda-IDs und Rohdaten bleiben je Zeile als Herkunft
gespeichert. Ereignis-ID `bitpanda:<Vorgangs-UUID>`, jede Zeile `…#1`, `…#2` (fest), dazu Aliase für Transaktions-
und Trade-IDs – so wird dieselbe Buchung aus dem Bitpanda-CSV-Export (Transaktions-ID `T…`) erkannt. Die
Bestandsprüfung läuft nur beim vollständigen historischen Abgleich (mit Leserecht „Balance“).

### Master-Key für API-Keys

In der App eingegebene API-Keys speichert Portfolia **nur verschlüsselt** (AES-256-GCM aus der Bibliothek
`cryptography`, Datenschlüssel per HKDF aus dem Master-Key, jeder Datensatz an seine Datenquelle gebunden). Der
Browser sieht nach dem Speichern nur die letzten vier Zeichen; der Schlüssel erscheint nicht in URLs, Logs,
Fehlermeldungen, Exporten oder Browser-Speichern und wird nur als Header an die API des Anbieters gesendet. Der
**Master-Key** liegt nie in der Datenbank, wird nie protokolliert und **nie automatisch erzeugt**. Fehlt er, ist
die Eingabe gesperrt, es wird nichts (auch nicht im Klartext) gespeichert und kein Abruf mit gespeichertem
Schlüssel gestartet; alles andere funktioniert.

**Einrichtung auf Unraid** (einmalig):

```sh
mkdir -p /boot/config/portfolia
openssl rand -base64 32 > /boot/config/portfolia/master.key
```

Im Template: Pfad **„Master-Key (Ordner, nur lesen)“** `/boot/config/portfolia` → `/run/secrets/portfolia` (ro) und
Variable `PORTFOLIA_MASTER_KEY_FILE=/run/secrets/portfolia/master.key` (im Template ab Portfolia 0.11 enthalten; fehlen
sie im bestehenden Container, über *Edit → Add another Path, Port, Variable…* ergänzen), dann neu starten. *Einstellungen →
Datenquellen* zeigt „Master-Key vorhanden“ mit einer Key-ID. Die Datei auf dem USB-Stick gehört root (FAT,
nur root-lesbar); der Container liest sie beim Start als root und stellt sie nur dem App-Benutzer im RAM
(`/dev/shm`) bereit – nach dem Anlegen oder Austauschen der Datei deshalb neu starten. Ohne `openssl`:
`docker exec Portfolia python -m app master-key > /boot/config/portfolia/master.key`.

**Andere Docker-Umgebungen:** Docker-Secret oder Datei mit Rechten 600/400 über `PORTFOLIA_MASTER_KEY_FILE`
(siehe [Docker Compose](#docker-compose--docker-run)); notfalls `PORTFOLIA_MASTER_KEY` (sichtbar in
`docker inspect`). Format: 32 zufällige Bytes als Base64 oder 64 Hex-Zeichen.

**Backup:** Die Datenbank-Sicherungen (`/data/backups`, appdata-Backups) enthalten API-Keys nur verschlüsselt. Den
Master-Key **getrennt** davon sichern – Inhalt von `master.key` im Passwortmanager; ein Flash-Backup des
USB-Sticks kann die Datei ebenfalls enthalten. Wer beides zusammen aufbewahrt, hebt die Trennung auf.

**Restore:** Datenbank wie gewohnt zurückspielen und **denselben** `master.key` wieder ablegen, neu starten. Die
Key-ID unter *Einstellungen → Datenquellen* muss zu der an den gespeicherten Schlüsseln passen. Ist der Master-Key
verloren: neuen anlegen und je Datenquelle „API-Key ersetzen“ – Buchungen, Abrufstand und Entscheidungen bleiben
erhalten, nur die Schlüssel sind neu einzugeben.

**Rotation** (z. B. nach Verdacht auf Offenlegung):

```sh
cd /boot/config/portfolia
mv master.key master-old.key
openssl rand -base64 32 > master.key
```

1. Im Template `PORTFOLIA_MASTER_KEY_OLD_FILE=/run/secrets/portfolia/master-old.key` setzen → *Apply* (Neustart).
2. *Einstellungen → Datenquellen → „Mit aktuellem Master-Key neu verschlüsseln“* (oder
   `docker exec Portfolia python -m app credentials rotate`); `python -m app credentials status` zeigt den Stand.
3. Sobald „0 mit früherem Master-Key“ angezeigt wird: Variable wieder leeren, *Apply*, `master-old.key` löschen und
   den neuen Key im Passwortmanager hinterlegen.

Wurde ein **API-Key** selbst offengelegt, hilft nur ein neuer Schlüssel bei Bitpanda: dort widerrufen, neu
erstellen und in Portfolia „API-Key ersetzen“.

**Bisheriger Weg per Umgebungsvariable** (bestehende Einrichtungen): `PORTFOLIA_DS_<NAME>` bzw.
`PORTFOLIA_DS_<NAME>_FILE` als Container-Variable, in der Datenquelle (*Fortgeschritten*) nur der Name. Das
funktioniert unverändert und braucht keinen Master-Key; ein in der App gespeicherter Schlüssel hat Vorrang.

### Synchronisieren, Abrufstand und Prüfung

1. Der Connector liefert Vorgänge mit **stabiler Ereignis-ID** `<anbieter>:<ID>`; ein Vorgang darf **mehrere
   Buchungszeilen** haben (z. B. Kauf + Gebühr in einem dritten Asset), jede Zeile erhält die feste Kennung
   `<ereignis-id>#<zeile>` – bei erneutem Abruf verschwindet und verdoppelt sich keine.
2. Die Zeilen durchlaufen **denselben Weg wie der CSV-Import**: Symbole zuordnen, EUR-Werte, Validierung,
   Dubletten, Stichtag, Transfer-Abgleich – nichts umgeht Portfolio- oder Steuerlogik.
3. **Prüfen und übernehmen** (*Synchronisierung prüfen*). Übernommene Buchungen heißen `PF-S-…`, tragen Quelle
   „Datenquelle · <Anbieter>“, Ereignis-ID, Zeile und Datenquelle und sind unter *Buchungen* bearbeitbar.

Regeln:

* **Abrufstand (Cursor):** rückt nur nach nachweislich vollständigem Abruf vor und wird erst gespeichert, wenn die
  Vorgänge im Prüf-Stapel stehen. Bei Abbruch, API- oder Datenbankfehler geht nichts verloren – der nächste Lauf
  holt dieselben Vorgänge erneut; bereits bekannte werden erkannt.
* **Idempotent:** Übernommene Kennungen gelten als „bereits vorhanden“ – auch gelöschte Buchungen werden nicht
  wieder angelegt. Vorgänge, die schon in einem offenen Prüf-Stapel warten, werden nicht noch einmal aufgenommen;
  neue werden an einen noch unbearbeiteten Stapel angehängt. Ein Lauf ohne Neues hinterlässt keinen Stapel.
* **Verwerfen** eines Prüf-Stapels setzt den Abrufstand vor dessen ältesten offenen Vorgang zurück: Der nächste
  Lauf liefert diese Vorgänge erneut.
* **Dauerhaft ignorieren** wird je Anbieter-Ereignis gespeichert und gilt für alle künftigen Läufe (auch nach
  Verwerfen oder Zurücksetzen); „Ignorieren aufheben“ macht es rückgängig.
* **Automatisch übernehmen** (Standard: aus): je Vorgang nur vollständig neue, eindeutig zugeordnete Vorgänge ohne
  Prüfhinweis; ungeklärte, möglicherweise doppelte und unvollständige bleiben zur Prüfung, ohne die sicheren
  aufzuhalten. Offene Prüfungen blockieren den Zeitplan nicht.
* **Nie parallel:** Zeitplan und „Jetzt synchronisieren“ teilen sich eine Sperre.
* **API-Key ersetzen oder entfernen** ändert keine Buchungen. **Entfernen** der Datenquelle löscht Konfiguration,
  verschlüsselten Schlüssel (SQLite `secure_delete`, WAL wird geleert), Laufhistorie und offene Prüf-Stapel;
  übernommene Buchungen bleiben und werden von einer neu angelegten Quelle desselben Anbieters erkannt (kein
  Doppelimport). Ändern von Anbieter, Adresse oder Konto setzt Status und Abrufstand zurück.
* **Zeitplan:** Ein Hintergrundjob prüft alle 5 Minuten fällige Quellen (aktiv, mit Anbindung und Intervall).
* **Datenschutz:** Ein Connector überträgt nur, was für den Abruf nötig ist (Adresse bzw. API-Key an den
  jeweiligen Anbieter) – keine Bestände, Werte oder Kontonamen.

### Doppelzählung zwischen kuratiertem Import und App-Buchungen

Wird nach einer Synchronisierung ein **neuer kuratierter Import** eingespielt, der dieselben Börsenbuchungen
enthält, dürfen sie nicht doppelt zählen:

* **Exakt und automatisch:** Trägt die Import-Buchung die Anbieter-ID – `source_ref = bitpanda:<ID>` (oder
  `source = bitpanda` und `source_ref = <ID>`; Portfolia-Exporte enthalten das bereits) –, gilt die Import-Buchung.
  Die App-Buchung zählt nicht mehr, bleibt aber mit Herkunft erhalten und zählt wieder, sobald ein späterer Import
  sie nicht mehr enthält.
* **Unsicher, nur Vorschlag:** gleiche Art und Assets, Menge ± 1 %, Datum ± 2 Tage, App-Buchung nicht nach dem
  Stand des Imports → *Buchungen → Abgleich mit dem Import*: „Import-Buchung gilt“ oder „keine Dublette“. Die
  Entscheidung speichert beide IDs und lässt sich aufheben; nichts wird still zusammengeführt. Das betrifft vor
  allem Importe aus Steuertools (z. B. Koinly), deren IDs keine Bitpanda-IDs enthalten.

### Grenzen der Bitpanda-Anbindung

* **Nicht live verifiziert:** Die gehostete Bitpanda-Entwicklerdokumentation war beim Bau nicht erreichbar;
  Endpunkte, Feldnamen, Pagination und Fehlercodes stützen sich auf die offiziell veröffentlichte API-Beschreibung
  von Bitpanda auf GitHub und öffentliche Beispiele. Der Parser ist deshalb tolerant (mehrere Feldnamen,
  Pagination-Varianten, Rückfall ohne Seitengröße/Zeitfilter bei HTTP 400) und meldet Unklares als „ungeklärt“ bzw.
  „teilweise“ statt zu raten. Beim ersten echten Abgleich bitte die Prüf-Liste und die Bestandsprüfung ansehen.
* **Scope-Fehler:** Ob Bitpanda ein fehlendes Leserecht mit 401 oder 403 beantwortet, ist nicht dokumentiert – die
  Unterscheidung „fehlendes Recht“ vs. „ungültiger Schlüssel“ stützt sich zusätzlich auf den Bestände-Test.
* **Gebühren:** Ob ein Betrag die Gebühr bereits enthält, ist nicht dokumentiert – betroffene Buchungen sind als
  prüfbedürftig markiert.
* **Nicht abgebildet:** Tausch Krypto → Krypto, Stocks/ETFs, Edelmetalle, Indizes, Korrekturen (siehe oben); für
  diese Fälle bleibt der CSV-Import bzw. die manuelle Erfassung.

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
  werden mit Transaktionskursen geschätzt und als „geschätzt“ gekennzeichnet. Positionen ohne Marktkurse – auch
  längst verkaufte – werden mit [Ersatzkursen](#kurse-und-datenquellen) bewertet statt mit 0 € (sonst
  entstünden Scheinverluste bis hin zu −100 % TTWROR). Hinweise auf unbewertete Positionen betreffen nur heute
  gehaltene; frühere stehen unter *Datenqualität → Historie*.

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
| CoinGecko (Demo/Pro) | Krypto aktuell (alle Coins in **einem** `/simple/price`-Aufruf), Historie `market_chart` | Monatsbudget (Standard 10.000) mit Hochrechnung und Empfehlung, Drosselung ab 80 %; Demo-API: Historie max. 365 Tage, davor Yahoo-Paare (z. B. BTC-EUR) laut Einstellung |
| EZB (Frankfurter, EZB-ZIP als Fallback) | Devisenkurse für EUR-Umrechnung | täglich |
| `manual_prices.csv` + Transaktionskurse | Ersatzkurse für Assets ohne Marktkurse | siehe unten |

* Jeder Kurs trägt Zeitstempel und Quelle. **Veraltet** gilt ein Kurs – deutlich markiert, nie still durch 0
  ersetzt, mit Grund in Übersicht, Positionsliste und Datenqualität:
  * Aktien/ETFs: Kursstand älter als 24 h, gezählt nur an Handelstagen.
  * Krypto: letzter **erfolgreicher Abruf** älter als 60 min (Grund z. B. „CoinGecko nach Fehlern pausiert bis
    15:30 (HTTP 429)“, „Monatskontingent erschöpft“, „für diese ID kein Kurs“) **oder** CoinGecko meldet seit mehr
    als 24 h keine Kursänderung (wenig oder kein Handel). Dass kleine Coins ihren Kurs bei CoinGecko oft
    stundenlang nicht ändern, ist allein kein Grund zur Warnung. Beide Grenzen unter *Einstellungen → Kurse*.
* Fällt eine Quelle aus, bleibt der letzte Wert mit Warnung stehen; die Quelle wird mit exponentiellem
  Backoff erneut versucht.
* Zeitplan (Standard): Krypto alle 10 min, Aktien/ETFs und Devisen alle 15 min zu EU/US-Handelszeiten
  (Devisen werktags ganztägig), EZB 16:35, Snapshot 23:30, Historien-Nachladen 06:10.

**Abrufrate einstellen** (*Einstellungen → Kurse → Aktualisierung*, gilt sofort ohne Neustart): Krypto 2 min bis
4 h, Krypto im Drosselbetrieb, Wertpapiere/Devisen 5 min bis 2 h. Jede Auswahl zeigt die hochgerechneten Aufrufe je
Monat für das eigene Portfolio und markiert die Empfehlung:

| CoinGecko-Verbraucher | Aufrufe | Hinweis |
|---|---|---|
| Kurse | 1 je Aktualisierung für alle gehaltenen Coins (mehr erst bei sehr vielen IDs) | alle 10 min ≈ 4.400/Monat, alle 5 min ≈ 8.800/Monat |
| Kurshistorie | ≈ 15/Monat je **nicht mehr gehaltenem** Coin (bzw. CoinGecko-Benchmark) | gehaltene Coins erhalten den Tagesschluss aus dem Kurs |
| Reserve | 5 % des Limits | Neustarts, „Kurse aktualisieren“, CSV-„Kurse laden“, Kursquellen-Suche, neue Coins |

* **Empfehlung Krypto:** das kürzeste Intervall ab 5 min, bei dem die Hochrechnung unter der Drosselschwelle
  (Standard 80 % des Limits) bleibt. Mit Demo-Schlüssel (10.000/Monat) ist das praktisch immer **10 min** – 5 min
  ergäbe über 90 % und würde im Laufe des Monats gedrosselt.
* **Drosselbetrieb:** ab der Schwelle wird automatisch seltener abgerufen (Standard 30 min, nie häufiger als normal);
  empfohlen ist das kürzeste Intervall, mit dem das Rest-Kontingent bis Monatsende reicht. Bei 100 % stoppt der Abruf
  bis zum Monatswechsel, Kurse bleiben mit „veraltet“-Markierung stehen.
* **Yahoo** hat kein veröffentlichtes Kontingent (inoffizielle Schnittstelle), sperrt aber bei zu vielen Anfragen je
  IP zeitweise; empfohlen sind **15 min** (bis 40 Symbole je Abruf, außerhalb der Handelszeiten keine Abrufe).
* Das Monatslimit zählt Portfolia selbst. Wird derselbe CoinGecko-Schlüssel auch anderswo genutzt, das Limit unter
  *Einstellungen → Kurse* entsprechend kleiner setzen. Die „veraltet“-Grenze für Krypto sollte mindestens das Doppelte
  des (gedrosselten) Intervalls betragen – die Seite weist darauf hin.

**Ersatzkurse** (Assets ohne Marktkurse, z. B. Kursquelle `none`/`manual`, nicht mehr gehandelte Token, lange
verkaufte Aktien ohne Symbol) – eine Regel für aktuelle Bewertung, Historie und Zahlungsströme:

* Kurspunkte: manuelle Kurse (`manual_prices.csv`) und Transaktionskurse (EUR-Wert ÷ Menge aus Käufen, Verkäufen,
  Tauschen, Erträgen und bewerteten Zu-/Abgängen, je Tag mengengewichtet; Buchungen unter 1 € zählen nicht).
  Am selben Tag hat der manuelle Kurs Vorrang; Splits werden berücksichtigt.
* Zwischen zwei Kurspunkten gilt der letzte. Nach dem **letzten** Kurspunkt gilt er höchstens **30 Tage (Krypto)**
  bzw. **365 Tage (Wertpapiere)** – einstellbar unter *Einstellungen → Kurse*, 0 = unbegrenzt. Danach ist die
  Position unbewertet (0 €); der Grund steht an der Position. Hintergrund: Ein Transaktionskurs von vor Monaten
  ist bei illiquiden Token keine Bewertung – typischer Fall sind Token, die nicht mehr gehandelt werden.
* Anzeige: Badge „manuell“ bzw. „Transaktionskurs“ mit Datum; abgelaufene Positionen lassen sich
  [ausbuchen](#positionen-ausbuchen-verlust-diebstahl).

**Kursquellen automatisch zuordnen** (*Datenqualität → Kursquellen*): Kryptowerte ohne Kursquelle werden nach jedem
Import, täglich um 06:40 und auf Knopfdruck im CoinGecko-Katalog gesucht.

* Katalog `/coins/list` inkl. Chains, wöchentlich geladen und lokal durchsucht – an CoinGecko gehen keine Symbole,
  Mengen oder Konten, danach nur die IDs der Kandidaten (`/coins/markets`).
* Kandidaten mit gleichem Symbol; die Konten liefern die Chain („MetaMask (BNB)“ → BNB Smart Chain, „Kaspa (KAS)“ →
  Kaspa, Börsenkonten keine). Coins nur auf anderen Chains entfallen, ebenso Coins, deren Kursspanne (Allzeittief ÷ 3 bis
  Allzeithoch × 3) die eigenen Transaktionskurse nicht enthält – z. B. LUNA zu Kursen von LUNA Classic.
* Sicherheit „hoch“ (genau ein passender Coin, Chain und Kurse passen) wird automatisch übernommen, „mittel“/„niedrig“
  erscheinen als Vorschlag mit *Übernehmen*/*Ablehnen*; jede ID lässt sich auch per Eingabe oder Link von coingecko.com
  setzen. Stufe unter *Einstellungen → Kurse* („nur eindeutige“, „auch wahrscheinliche“, „nie“). Spam-Token (Status
  `spam`) werden übersprungen.
* Zuordnungen gelten über dem Import, fließen in den Gesamtexport (`assets.csv`) ein und lassen sich jederzeit
  zurücknehmen; danach werden Kurse und Historie neu geladen.

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
* **API-Keys von Datenquellen:** nur verschlüsselt in der Datenbank (Master-Key außerhalb, siehe
  [Master-Key](#master-key-für-api-keys)); alle Formulare und Aktionen (Schlüssel speichern, ersetzen, entfernen,
  testen, synchronisieren, neu verschlüsseln) sind POST mit CSRF-Schutz und unterliegen der Anmeldung.
* **Backups:** täglich (Uhrzeit einstellbar, Standard 03:15) über die SQLite-Online-Backup-API mit
  Integritätsprüfung, gzip-komprimiert nach `/data/backups`, Aufbewahrung 14 Stück (einstellbar); manuell
  unter *Einstellungen → Backups* oder per `docker exec -u 99:100 portfolia python -m app backup`.
  **Wiederherstellen:** Container stoppen, Sicherung entpacken (`gunzip`), als `/data/app.sqlite` ablegen,
  `app.sqlite-wal`/`-shm` entfernen, Container starten. Gespeicherte API-Keys sind darin nur verschlüsselt
  enthalten und brauchen nach einem Restore denselben Master-Key. **Wichtig:** In Portfolia erfasste und per CSV
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
| Asset „unbewertet“ | Keine Kursquelle/ID, Quelle nicht erreichbar oder Ersatzkurs abgelaufen (Grund an der Position) → *Datenqualität → Kursquellen* (CoinGecko-Suche bzw. ID eintragen), `quote_id` prüfen, aktuellen Kurs in `manual_prices.csv` liefern oder unter *Buchungen → Ausbuchen* als Verlust ausbuchen. |
| Mobil: Leiste unten verschwindet / Seite seitlich verschiebbar | Behoben ab 0.9.1 (kein Element breiter als der Bildschirm, `overflow-x: clip`). Nach dem Update Seite einmal neu laden. |
| Kurs „veraltet“ | Quelle nicht erreichbar oder Budget gedrosselt → *Datenqualität → Datenquellen & Kontingente*. |
| Krypto-Historie vor 365 Tagen fehlt | CoinGecko-Demo-Grenze → Yahoo-Paar unter *Einstellungen → Kurse* zuordnen (nur eindeutige Paare). |
| News-Quelle deaktiviert | *News → Quellen*: Grund und letzter Fehler; URL in `sources.yaml` korrigieren und reaktivieren. |
| YouTube-Handle nicht auflösbar | Handle prüfen (mit `@`), ggf. `YOUTUBE_API_KEY` setzen; nicht auflösbare Kanäle bleiben deaktiviert. |
| Vorabpauschale „Kurse fehlen“ | Kurshistorie des Fonds laden (*Datenqualität → Historie nachladen*) oder Basiszins im Override ergänzen. |
| Zugriff verweigert (403) | CSRF-Schutz: Seite neu laden; Cross-Site-Formulare werden abgelehnt. |
| „Master-Key fehlt“ / „Datei … nicht lesbar“ | Datei fehlt oder Container nach dem Anlegen nicht neu gestartet → [Master-Key](#master-key-für-api-keys). |
| „mit einem anderen Master-Key verschlüsselt (Key-ID …)“ | Falscher Master-Key nach Restore/Rotation: richtigen Key ablegen bzw. alten als `PORTFOLIA_MASTER_KEY_OLD_FILE` bereitstellen, sonst API-Key neu eingeben. |
| Bitpanda „teilweise synchronisiert“ | Abruf unvollständig (Drosselung, Seitenende unklar) – der nächste Lauf holt erneut ab; Details unter „Abdeckung“ der Datenquelle. |
| Bitpanda „Berechtigung fehlt“ / „abgelehnt“ | API-Key mit Leserecht „Transaction“ neu erstellen und unter „API-Key ersetzen“ eintragen. |

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
  csvimport/  CSV-Profile, Vorschau, Dubletten (inkl. Ereignis-IDs), Transfer-Abgleich, Übernahme
  datasources/ Datenquellen: Anbieterkatalog, Connector-Schnittstelle, Synchronisierung, Einstellungen,
              verschlüsselte Zugangsdaten (vault.py), Bitpanda-Connector (bitpanda.py)
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

**Neuer Connector:** Klasse von `app.datasources.connector.Connector` ableiten (`provider` = ID aus
`app/datasources/providers.py`, `check()` und `fetch()`), mit `@register` anmelden und das Modul in
`app/main.py` laden. Der Vertrag (Ereignis-ID im Format des CSV-Profils, feste Zeilenreihenfolge, Aliase,
Cursor nur bei vollständigem Abruf, `rewind()`, Zeilenart „review“ für Ungeklärtes, `ConnectorError` mit Text ohne
Geheimnisse) steht im Modul-Docstring; `tests/test_datasources.py` zeigt einen Test-Connector,
`app/datasources/bitpanda.py` mit `tests/test_bitpanda.py` (anonymisierte Fixtures unter `tests/data/bitpanda/`)
einen produktiven.

CI (GitHub Actions): Lint und Tests bei jedem Push/PR; Image-Build und Veröffentlichung nach GHCR
(`ghcr.io/pneumann1980/portfolia`) für den Standard-Branch und Versions-Tags – als Docker-Manifestliste ohne
Attestierungen (Unraid-Update-Prüfung), was ein eigener CI-Schritt prüft.

---

## Grenzen und Lizenz

Bekannte Grenzen (Auswahl, vollständig in [`docs/MILESTONES.md`](docs/MILESTONES.md)):

* Nur Basiswährung EUR; Fremdwährungsgewinne (§ 23 EStG) werden nicht ermittelt.
* Vorabpauschale mit Börsenschlusskursen statt Rücknahmepreisen; Altanteile vor 2018 nicht berücksichtigt.
* Formularzeilen nur für 2024 und Anlage SO 2025 hinterlegt (aus Sekundärquellen, ohne Gewähr); für andere
  Jahre nennt die Übertragungshilfe nur die Feldbezeichnungen.
* Datenquellen sind inoffiziell (Yahoo) bzw. limitiert (CoinGecko Demo); Ausfälle werden sichtbar markiert.
* Bitpanda-Anbindung mit Fixtures getestet, nicht mit einem echten Konto; nicht abgebildete Vorgänge (Tausch,
  Stocks, Metalle, Indizes, Korrekturen) bleiben zur Prüfung – siehe
  [Grenzen der Bitpanda-Anbindung](#grenzen-der-bitpanda-anbindung).

**Lizenz:** Portfolia steht unter der [MIT-Lizenz](LICENSE) – Nutzung, Änderung und Weitergabe (auch
kommerziell) sind erlaubt, solange Copyright- und Lizenzhinweis erhalten bleiben; keine Gewährleistung.

Drittkomponenten im Image (Details und vollständige Paketliste: [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)):
Apache ECharts 6.1 (Apache-2.0, inkl. NOTICE; enthaltene d3-Teile BSD-3-Clause) und htmx 2.0 (0BSD) –
Lizenztexte unter `app/static/vendor/`; Bitstream Vera Fonts (über ReportLab) für PDFs; Python-Pakete u. a.
unter MIT, BSD, Apache-2.0, PSF-2.0 und MPL-2.0 (certifi, unverändert). Alle sind mit der MIT-Lizenz vereinbar.
