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
  anlegen; **Bitpanda** und **Binance** werden read-only per API-Key synchronisiert (Schlüssel in der App eingegeben,
  verschlüsselt gespeichert, jede Buchung vor der Übernahme prüfbar). **Wallets** auf zwölf Chains (u. a. Bitcoin,
  Ethereum, PulseChain, Polkadot, peaq, Solana, Kaspa) werden read-only über öffentliche Adressen bzw. einen öffentlichen Kontoschlüssel
  synchronisiert (siehe [Wallets](#wallets-read-only-zwölf-chains)) – nie Seed-Phrase, privater Schlüssel oder
  Signatur. In der App erfasste
  Buchungen und Assets liegen neben abgeleiteten Daten (Kurse, News, Snapshots, Berichte) in `/data/app.sqlite`.
* **Export und Sicherung:** Alles lässt sich jederzeit im einheitlichen Import-Format (Datenvertrag, Schema 1.1)
  exportieren – **vollständig**, inklusive aller Änderungen in der App, Kursquellen, Einstellungen und Kurshistorie,
  sodass eine neue Installation mit einer Datei denselben Stand erreicht (siehe [Neu einrichten](#neu-einrichten-und-umziehen));
  nach jeder Änderung entsteht automatisch eine **datierte ZIP-Sicherung**, importierte ZIP-Dateien werden mit Datum
  archiviert (siehe [Backups](#einstellungen-sicherheit-backups)).
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
5. [CSV-Import aus Börsen und Wallets](#csv-import-aus-börsen-und-wallets) ·
   [Importprüfung: Abgleich, Stapelaktionen, Verknüpfen](#importprüfung-abgleich-je-zeile-stapelaktionen-verknüpfen) ·
   [Belege: PDF & Screenshot](#belege-pdf--screenshot)
6. [Datenquellen: Börsen und Wallet-Adressen](#datenquellen-börsen-und-wallet-adressen) ·
   [Wallets (read-only, zwölf Chains)](#wallets-read-only-zwölf-chains) ·
   [Diagnose: Datenqualität und Bestandsabgleich](#diagnose-datenqualität-und-bestandsabgleich) ·
   [Finanzielle Integritätsprüfung und Sammelbearbeitung](#finanzielle-integritätsprüfung-und-sammelbearbeitung)
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
4. Nur für Datenquellen mit API-Key (Bitpanda, Binance; bei Wallets Etherscan, Routescan, NodeReal, Helius, Koios, PubFi oder Subscan): einmalig den
   Master-Key anlegen (Unraid-Terminal), danach bleibt er unverändert – Einzelheiten, Backup und Rotation unter
   [Master-Key](#master-key-für-api-keys). Wallets über mempool.space/Blockstream (Bitcoin), Routescan (Avalanche),
   Blockscout (Polygon), die öffentlichen XRPL-Server, Koios (Cardano), den öffentlichen Solana-RPC und api.kaspa.org
   brauchen keinen Schlüssel:
   ```sh
   mkdir -p /boot/config/portfolia
   openssl rand -base64 32 > /boot/config/portfolia/master.key
   ```
5. Container starten, Weboberfläche über *WebUI* öffnen (Port 8080).
6. Optional Wallets einrichten: *Einstellungen → Datenquellen → „+ Wallet-Konto“* → Chain → Name/Gruppe →
   öffentliche Adresse (Bitcoin auch xpub/ypub/zpub) → „Verbindung testen“ → „Erstabruf starten“ (siehe
   [Wallets](#wallets-read-only-zwölf-chains)). Der Container braucht dafür ausgehenden HTTPS-Zugriff auf die
   gewählten Anbieter.

**Aktualisieren:** *Docker → portfolia → Update* (bzw. *Check for Updates*). Zeigt Unraid „not available“, hilft
*Advanced View* → *force update* oder *Edit → Apply* (lädt `latest` neu und erstellt den Container neu; Daten in
`/data` und `/exports` bleiben erhalten). Images ab 0.9.0 werden als Docker-Manifestliste veröffentlicht, damit die
Update-Prüfung von Unraid funktioniert (OCI-Indizes mit Attestierungen erkennt sie nicht).

**Welcher Stand läuft?** Die CI veröffentlicht Images nur für Pushes auf den Standard-Branch des Repositorys
(`latest` und `sha-<Commit>`) und für Versions-Tags `v*` (`X.Y.Z`, `X.Y`) – ein grüner Build eines anderen Branches
erzeugt kein Update. Ab 0.16.2 trägt jedes Image seinen Commit: Seitenleiste „v0.16.2 · abc1234“, *Einstellungen →
System → Version* und `GET /healthz` (`"revision"`). Nach dem Update dort prüfen, ob der Commit dem erwarteten
entspricht (GitHub → Actions → Lauf → Schritt „Veröffentlichte Tags“); „ohne Build-Kennung“ heißt lokal bzw. vor
0.16.2 gebaut. Im Unraid-Terminal: `docker image inspect ghcr.io/pneumann1980/portfolia:latest --format
'{{ index .Config.Labels "org.opencontainers.image.revision" }}'`.

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
| `PORTFOLIA_DS_ETHERSCAN`, `…_ROUTESCAN`, `…_NODEREAL`, `…_HELIUS`, `…_KOIOS`, `…_PUBFI`, `…_SUBSCAN` | – | optional: [Anbieter-Schlüssel](#wallets-read-only-zwölf-chains) für Wallet-Abrufe als Container-Variable (bzw. `…_FILE`), falls nicht in der App gespeichert. Nötig: Etherscan (Ethereum, Polygon), NodeReal (BNB Chain, kostenlos; `…_NODEREAL`), PubFi **oder** Subscan (Polkadot, peaq); optional: Routescan, Koios. `PORTFOLIA_DS_<NAME>` für Börsen: Bitpanda-Key bzw. bei Binance `API-Key:Secret-Key`. |

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

### Übersicht, Positionsdetail und Watchlist

* **Depotwert verbergen:** Das Augen-Symbol neben „Depotwert“ blendet den Gesamtwert und die Tagesveränderung in €
  aus (`•••••• €`; die Veränderung in % bleibt). Die Wahl gilt je Gerät/Browser und wird vor dem ersten Zeichnen
  angewendet – der Betrag blitzt beim Laden nicht auf. Weitere Beträge (Kennzahlen, Positionen, Diagramme) bleiben
  sichtbar.
* **Top-Bewegungen** mit Umschalter **% | €**: nach prozentualer oder absoluter Tagesänderung sortiert, beide Werte
  sichtbar; die Wahl bleibt im Browser gespeichert.
* **Allokation als Treemap** (neben Ring/Liste): Fläche = Positionswert, Farbe = Tages- oder Gesamtperformance;
  Positionen unter der Schwelle (*Einstellungen*, Standard 1 %) je Segment als „Sonstige (n)“, per Klick aufklappbar.
* **Positionsdetail:** Zeiträume 1T | 7T | 1M | 3M | 1J | MAX, Linie oder Kerzen, Kauf-/Verkaufsmarker,
  Einstandslinie, Kennzahlen (Bestand, Investiert, Ø Einstand, Gewinn/Verlust), Kursqualität und
  **Schnellkauf/-verkauf** ([−]/[+], auch in der Positionsliste).
* **Watchlist** (*Mehr → Watchlist*): Symbol, Name, Kurs, 24 h, 7 Tage, Marktkapitalisierung, Sparkline;
  hinzufügen (CoinGecko-ID/Link/Symbol, Yahoo-Symbol oder Portfolio-Asset), entfernen, sortieren, eigene
  Reihenfolge, Detailansicht mit Kursverlauf und „Position erstellen“ (öffnet die normale Kauferfassung, legt nichts
  ohne Speichern an). Datenmodell für mehrere Listen vorbereitet. Indizes (`^GSPC` = S&P 500, `^GDAXI`) und
  Devisen/Futures (`EURUSD=X`, `GC=F`) über Yahoo; Indizes zeigen den Stand in Punkten und sind nicht kaufbar.

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
| `portfolia/…` | nein | nur Portfolia-Gesamtexporte: App-Zustand für die [Neueinrichtung](#neu-einrichten-und-umziehen), Prüfsummen im Manifest unter `extra_files`; kein Teil des Datenvertrags, andere Werkzeuge ignorieren den Ordner |

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
* **Schnellkauf/-verkauf** (Positionsliste, Positionsdetail, Watchlist): kompakter Dialog über **dieselbe**
  Erfassung und Validierung wie die Vorlagen Kauf/Verkauf; ohne Preisangabe gilt der Marktkurs des Tages
  (`price_source = market`, Herkunft gespeichert).
* **Prüfung:** derselbe Validator wie beim Import (Pflichtbeine, `value_eur`, Transfers …). Hinweise, wenn
  ein Abgang den Bestand eines Kontos ins Minus drückt oder eine manuelle Buchung einer Import-Buchung stark
  ähnelt (gleiche Konten und Assets, ±2 Tage, Menge ±1 % → „Dublette?“, auch unter *Datenqualität*). Unter der
  Zeile stehen beide Buchungen nebeneinander (Zeitpunkt, Art, Ab-/Zugang, Gebühr, Wert, Herkunft, Kennung, Hash,
  Notiz), Abweichungen sind hervorgehoben – das Badge springt dorthin.
* **Bearbeiten, Kopieren, Löschen – für alle Buchungen:** in der App erfasste (IDs `PF-M-000001` ff.), per CSV
  oder Datenquelle übernommene und **Buchungen des kuratierten Imports**. Import-Buchungen werden im Expertenmodus
  (alle Felder des Datenvertrags) bearbeitet; die Import-Datei bleibt unverändert, die Änderung gilt als
  Überlagerung – in allen Berechnungen, im Gesamtexport und auch für spätere Importe mit derselben `tx_id`.
  Liefert ein neuer Import eine andere Fassung oder fehlt die Buchung dort, erscheint ein Hinweis („Import
  geändert“ bzw. „ohne Wirkung“). „Verwerfen“ stellt die Import-Fassung wieder her. Freigegebene und geschätzte
  Sparplan-Ausführungen lassen sich aus der Liste heraus bearbeiten oder verwerfen. Löschen ist immer umkehrbar
  (*Gelöschte Buchungen*); jede Änderung steht im Änderungsprotokoll (*Datenqualität*).
* **Assets:** neue Positionen mit Kursquelle (CoinGecko-ID bzw. Yahoo-Symbol), Kategorie und Steuerart
  anlegen. Definiert der Import dasselbe Asset, gelten dessen Stammdaten.
* **Zusammenspiel mit dem Import:** Der Import bleibt unverändert, manuelle Buchungen kommen hinzu. Enthält ein
  späterer Import dieselbe `tx_id` oder dieselbe Anbieter-ID (`source_ref`, siehe
  [Abgleich](#doppelzählung-zwischen-kuratiertem-import-und-app-buchungen)), gilt die Import-Buchung (keine
  Doppelzählung); ähnliche Buchungen mit anderer ID landen unter *Buchungen → Abgleich mit dem Import* zur
  Entscheidung. Manuell erfasste Sparplan-Ausführungen ersetzen passende Schätzungen.
* **Gesamtexport:** alle Buchungen in ihrer wirksamen Fassung (Import mit Änderungen und Löschungen, manuell, per CSV
  und Datenquelle erfasst, freigegebene Sparplan-Ausführungen) als Import-ZIP (Schema 1.1, mit aktuellem Bestand als
  `holdings_check`, steuerlichen Einstufungen als `tax_type` bzw. `tax_withholding`, allen Konten und allen Assets
  samt **Kursquelle** – auch in der App zugeordnete CoinGecko-IDs). Dazu der App-Zustand im Ordner `portfolia/`
  (siehe [Neu einrichten](#neu-einrichten-und-umziehen)). Zusätzlich entsteht nach jeder Änderung automatisch eine
  datierte Kopie (siehe [Backups](#einstellungen-sicherheit-backups)).
* **Ohne Import:** Alle Ansichten (Positionen, Performance, Steuern, Sparpläne) funktionieren auch nur mit
  manuell erfassten Buchungen. Steuerberichte weisen manuell erfasste Buchungen des Jahres aus.

### Ticker- und Token-Änderungen (z. B. MATIC → POL)

*Datenqualität → Ticker/Umstellungen* (`/changes`) bzw. im Positionsdetail *Ticker/Umstellung …*. Zwei Arten, weil sie
wirtschaftlich Verschiedenes bedeuten:

* **Umbenennung** – dasselbe Wertpapier bzw. derselbe Token, nur neues Kürzel, neuer Name oder neue Kurs-ID (z. B.
  neues Börsenkürzel). Asset-ID, Buchungen, Lots und Haltefristen bleiben unverändert; die Änderung gilt als Overlay.
  Die Kurshistorie wird verkettet: Tage ohne Kurs der neuen Quelle übernehmen die bisherigen Kurse (als Ersatzkurs
  gekennzeichnet), Kurse der neuen Quelle haben Vorrang. Ein neues Kürzel ordnet künftige CSV-Importe/Datenquellen
  diesem Asset zu.
* **Umstellung** – ein neuer Token ersetzt den bisherigen in festem Verhältnis (MATIC → POL 1:1; auch `1:1000`).
  Je Konto mit **Restbestand** entsteht eine App-Buchung „Kapitalmaßnahme – Migration“ (gleiche Erfassung und
  Validierung wie im Journal): Anschaffungsdaten und Haltefristen gehen auf den neuen Bestand über. Gebucht wird
  frühestens nach der letzten Bewegung des Kontos – bereits umgestellte Bestände (Börse, Wallet-Vorschlag) werden so
  nicht doppelt umgestellt; ein zweiter Lauf findet nichts mehr. Das Ziel-Asset wird bei Bedarf angelegt.

**Erkennung** (nur Hinweise, Sicherheit hoch/mittel/niedrig; hohe Sicherheit zusätzlich als Hinweis oben auf jeder
Seite):

1. Register bekannter Umstellungen mit Verhältnis und Stichtag (`app/assetchange/known.py`; derzeit MATIC → POL).
2. CoinGecko-Katalog (lokal, ohne Abruf): CoinGecko benennt ersetzte Coins um – „MATIC (migrated to POL)“,
   „… [OLD]“, „… (Legacy)“; Nachfolger nur, wenn eindeutig. Das Verhältnis steht nicht im Katalog → prüfen.
3. Anbieter-Bestände der Datenquellen: Konto meldet 0 des bisherigen Assets und vom neuen genau Restbestand ×
   Verhältnis mehr als gebucht (± 1 %).
4. Kursstillstand: seit über 30 Tagen kein Marktkurs, während andere Kurse aktuell sind (Nachfolger unbekannt).

Jeder Schritt hat eine Vorschau und lässt sich unter `/changes` rückgängig machen (die Umstellungsbuchungen erhalten
den Status „rückgängig gemacht“ und bleiben im Journal-Protokoll nachvollziehbar). Hinweise lassen sich ausblenden.

**Sicherheit beim Übernehmen:** Vorschau und Übernahme tragen eine Prüfsumme über den betroffenen Bestand; hat er sich
zwischenzeitlich geändert (neue Buchung, zweiter Tab, Datenquelle), wird nicht gebucht, sondern die Vorschau neu
angezeigt. Alle Buchungen, das Ziel-Asset und der Änderungsdatensatz entstehen in **einer** Datenbank-Transaktion –
scheitert ein Schritt, bleibt nichts zurück („es wurde nichts geändert“). Gleichzeitige oder doppelte Anfragen buchen
nur einmal. Rückgängig wird abgelehnt, wenn eine Umstellungsbuchung inzwischen geändert/gelöscht wurde oder der neue
Bestand bereits verwendet ist (sonst entstünde ein negativer Bestand); ein dafür angelegtes Ziel-Asset wird nur
entfernt, wenn nichts anderes es nutzt. Für Aktien gibt es kein Register: Bei neuem Kürzel
die Umbenennung mit dem neuen Yahoo-Symbol erfassen; bei Fusion/Umtausch in ein anderes Wertpapier die Umstellung.
Grenze: Ob eine Token-Umstellung steuerlich keine Veräußerung ist, hängt vom Einzelfall ab – Portfolia führt sie
technisch wie die Kapitalmaßnahme „Migration“ (Anschaffungsdaten bleiben), kennzeichnet betroffene Veräußerungen in
der Steueraufstellung aber mit „über Umstellung“ und einem Prüfhinweis (*steuerliche Behandlung nicht automatisch
geklärt*).

#### Dieselbe Umbenennung aus zwei Quellen (z. B. AITECH → ACN)

Benennt eine Börse einen Token um, liefern Quellen den Vorgang oft verschieden: das Steuertool führt den Altbestand
schon unter dem neuen Symbol mit eigener Kennung (`ACN#…`) und bucht die Umstellung als **Tausch** `ACN#…` → `ACN`;
die Börsen-API liefert eine Kapitalmaßnahme `AITECH` → `ACN`. Ohne Korrektur zählt `ACN` doppelt, `AITECH` wird
negativ, und der Tausch realisiert einen Scheingewinn bzw. -verlust mit neuer Haltedauer. Portfolia erkennt das:

* **Diagnose „Umtausch doppelt gebucht“** – gleiches Konto und Ziel-Asset, exakt gleiche Mengen auf beiden Seiten,
  ≤ 36 h, verschiedene Quellen; die Buchung, deren Ausgangs-Asset vorher keinen Bestand hatte, ist die zusätzliche.
  Lösung: App-Buchung „im Import enthalten“ (bzw. ausblenden). Der Befund „Bestand zeitweise negativ“ verweist darauf.
* **Diagnose „Umbenennung als Tausch gebucht“** – Tausch zwischen zwei Asset-IDs desselben Instruments (gleiche
  Kurszuordnung bzw. gleiches Symbol), Verhältnis 1 : 1 bzw. 10^k : 1, Altbestand geht vollständig über. Lösung
  nach Vorschau: als Kapitalmaßnahme „migration“ buchen (Einstand und Anschaffungsdaten gehen über). Steuerlich nur
  richtig bei reiner Umbenennung; Berichte des Steuertools weichen danach ab.
* **Prüf-Stapel:** Liefert eine weitere Quelle denselben Umtausch aus einem anders benannten Ausgangs-Asset, wird er
  nicht übernommen, sondern als „komplex“ zur Prüfung vorgelegt.

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
   `interest`, `airdrop`, Gebühr) mit Status *neu*, *bereits vorhanden*, *mögliche Dublette*, *vor Stichtag*,
   *unvollständig* oder *ignoriert*. Verweist eine Zeile auf vorhandene Buchungen (Dublettenverdacht, bereits
   vorhanden, gleicher Transaktions-Hash), zeigt „Vergleich mit der vorhandenen Buchung“ beide Seiten Feld für
   Feld – Zeitpunkt mit Abstand, Art, Ab- und Zugang mit Konto, Gebühr, EUR-Wert, Herkunft, Kennung, Hash, Notiz –
   und hebt hervor, was abweicht; bei *mögliche Dublette* aufgeklappt, sonst eingeklappt. Gesucht wird in den
   erfassten Buchungen, im Import und im Journal (auch gelöschte bzw. zusammengeführte Buchungen, mit Status). Bis zu
   zwei Buchungen stehen nebeneinander, weitere als Verweis. Nur Anzeige – entschieden wird über „Übernehmen“.
3. **Zuordnen:** unbekannte Symbole einem vorhandenen Asset zuordnen, als neues Asset anlegen oder ignorieren;
   Konten der Datei auf Portfolia-Konten abbilden. Zuordnungen gelten für alle weiteren Importe. Portfolia schlägt
   für jedes Symbol automatisch etwas vor und belegt das Formular vor (siehe *Automatische Vorschläge beim Zuordnen* unten) – gespeichert
   wird erst mit „Zuordnungen speichern“.
4. **Übernehmen:** gültige Zeilen werden Journal-Buchungen (`PF-C-…`, Quelle „CSV · <Format>“, bearbeitbar).
   Offene Zeilen (z. B. ohne EUR-Wert) bleiben im Import und lassen sich später ergänzen und nachschieben.
5. **Rückgängig:** nimmt alle Buchungen eines Imports zurück; danach kann dieselbe Datei erneut (korrigiert)
   übernommen werden.

**Automatische Vorschläge beim Zuordnen** – Abgleich mit der vorhandenen Datenbasis (Assets aus Import und App,
aktive und offene Kursquellen-Zuordnungen, frühere Symbol-Zuordnungen, Token-Namen der Wallet-Anbindung) und dem
lokal gespeicherten CoinGecko-Katalog (`/coins/list` mit Contract-Adressen je Chain, höchstens wöchentlich geladen;
fehlt er, wird er im Hintergrund geholt). Gesucht wird lokal: An CoinGecko gehen weder Symbole noch Contracts, Mengen
oder Konten.

| Fall | Vorschlag | Sicherheit |
|---|---|---|
| Token (`SYMBOL@CHAIN:Contract`), Contract im Katalog, ein Asset nutzt diese CoinGecko-ID | zuordnen | hoch |
| … ein Asset hat dazu einen offenen Kursquellen-Vorschlag | zuordnen, Kursquelle bestätigen | hoch |
| … kein passendes Asset | neu anlegen mit Name und CoinGecko-ID; gleicher Coin auf weiterer Chain → dorthin zuordnen | hoch |
| … einziges Krypto-Asset gleichen Symbols ohne Kursquelle | zuordnen, Kursquelle übernehmen (abwählbar) | mittel |
| … Asset gleichen Symbols mit anderer CoinGecko-ID | neu anlegen als `SYMBOL#2` mit Warnhinweis | hoch |
| gleicher Contract früher unter anderem Symbol zugeordnet bzw. ignoriert | übernehmen | hoch |
| Token nicht im Katalog und als Spam erkannt | ignorieren | hoch |
| Token nicht im Katalog, nur erhalten, nie bewegt (typischer Werbe-Token) | ignorieren | mittel |
| Token nicht im Katalog, sonst | offen, mit Hinweis | – |
| Symbol ohne Contract: mehrdeutig (mehrere Assets) | das einzige mit Kursquelle bzw. ohne Spam-Markierung | mittel |
| … bei einem anderen Import schon zugeordnet | zuordnen | mittel |
| … bekannter Coin bzw. einziger Coin mit dem Symbol im Katalog | neu anlegen mit CoinGecko-ID | hoch / mittel |
| … mehrere Coins mit dem Symbol | Auswahlliste; ohne Auswahl entscheidet nach der Übernahme die Kursquellen-Suche anhand von Marktdaten | niedrig |

Tokens werden **nie über das Symbol allein** bestimmt (gefälschte Tokens tragen gern bekannte Symbole wie USDC);
„hoch“ und „mittel“ belegen die Aktion vor, „niedrig“ nur die Felder. Grenzen: Tokens, die CoinGecko nicht führt,
bekommen keinen Kurs-Vorschlag; KRC-20 wird nur gefunden, wenn der Katalog den Tick unter einer Kaspa-Plattform führt
(sonst entscheidet die Kursquellen-Suche über Symbol und Chain-Hinweis). Der Katalog belegt im Speicher rund 15–25 MB.

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
  markiert – zusammengeführt wird nie allein wegen Ähnlichkeit. Gegen den **kuratierten Import** gelten außerdem
  dessen Kennungen: die Koinly-ID (`source = koinly`, `source_ref` = ID wie im Koinly-Export) und
  Bitpanda-UUIDs, die Koinly in der Notiz führt (`txhash=<uuid>` auf einem Bitpanda-Konto) – Treffer sind *bereits
  vorhanden*. Ein Zu- bzw. Abgang mit **exakt derselben Menge** desselben Assets auf demselben Konto innerhalb von
  36 Stunden (z. B. einmal manuell nachgetragen, einmal als Transfer erfasst) wird *mögliche Dublette* und nie
  automatisch übernommen – außer bei zwei verschiedenen Blockchain-Transaktionen, Erträgen und Fiat. Ebenso ein
  Vorgang auf Konto und Asset einer **rekonstruierten Buchung** des Imports (± 7 Tage): Die echte Abrechnung ersetzt
  womöglich die Schätzung – dann dort ersetzen, nicht zusätzlich übernehmen.
* **Stichtag:** Mit kuratiertem Import werden standardmäßig nur Zeilen **nach** dessen Stand (`valuation_date`)
  vorgeschlagen – ältere stehen dort bereits. Zeilen bis zum Stichtag gelten als *vor Stichtag*, auch wenn ihr Asset
  unbekannt ist, ein EUR-Wert fehlt oder die Art ungeklärt ist: Für sie ist keine Zuordnung und keine Entscheidung
  nötig (nur wer eine davon zusätzlich übernehmen will, ergänzt das Fehlende). Der Stichtag lässt sich je Import
  ändern oder leeren.
* **Abgleich über den Transaktions-Hash** (Wallets, Wallet-Exporte): Führt der kuratierte Import den Hash einer
  Blockchain-Transaktion (Notiz oder Quellkennung, z. B. aus Koinly) bzw. eine App-Buchung ihn als `tx_hash`, werden
  die Beine beider Seiten verglichen – Zugang, Abgang, Gebühr, Mengen je Seite und Asset summiert, ± 0,5 %; Gebühren
  als eigene Buchung (Abgang mit Tag `cost`) zählen als Gebühr. Alle Hauptbeine gefunden → *bereits vorhanden*
  (unabhängig vom Stichtag, wird nie erneut gebucht). Gleicher Hash, nicht alle Teile → vor dem Stichtag Hinweis,
  danach *mögliche Dublette*. Vor dem Stichtag ohne Gegenstück → Hinweis „nicht im kuratierten Import“ (z. B. Spam,
  Freigaben, Lücken; nur Information). Jede Gegenbuchung deckt höchstens ein Bein.
* **Daraus abgeleitet, ohne Rückfrage:** Token-Zuordnungen (`SYMBOL@CHAIN:Contract` → Asset der Gegenbuchung, nur
  wenn alle Treffer dasselbe Asset zeigen; gespeichert mit Herkunft „Abgleich“, löschbar) und das **Konto**: Führt
  der Import die Transaktionen einer Wallet-Datenquelle unter einem anderen Konto (mindestens 3 Treffer, ≥ 90 % unter
  einem Konto) und hat das bisherige Konto noch keine Buchungen, stellt Portfolia die Datenquelle einmalig auf
  dieses Konto um – offene Zeilen folgen, *Rückgängig* jederzeit. Sonst erscheint ein Vorschlag mit einem Klick.
  Für Symbole ohne Contract gilt eine gelernte Zuordnung nur in diesem Stapel.
* **Anbieter-Kürzel:** Dasselbe Symbol bezeichnet je Anbieter oft verschiedene Coins (Bitpanda „TH“ = Threshold
  Network, CoinGecko „th“ = Team Heretics Fan Token). Für solche Kürzel wird ein Asset nur zugeordnet, wenn seine
  Kursquelle den Coin des Anbieters bestätigt oder eine Zuordnung genau für diesen Anbieter gespeichert ist
  (`TH@BITPANDA`) – sonst bleibt die Zeile offen („bei Bitpanda ist Threshold Network – Asset zuordnen“); der
  Vorschlag legt ein eigenes Asset mit der richtigen CoinGecko-ID an. Auch die automatische Kursquellen-Suche
  ordnet solche Kürzel nie über das Symbol zu. Bestehende Zuordnungen bleiben unverändert.
* **Interne Umbuchungen** eines Anbieters (Spot ↔ Earn/Staking/Funding, Kraken-Staking-Varianten wie `DOT.S`)
  werden übersprungen; der Bestand bleibt auf dem einen Konto des Anbieters.
* **Nicht unterstützt:** Futures, Margin, Optionen, NFTs (Zeilen werden gezählt und übersprungen). Umbuchungen
  in diese Bereiche gelten als intern – bei aktivem Derivatehandel können Bestände deshalb abweichen.
* Exportformate ändern sich gelegentlich; unbekannte Vorgänge erscheinen als „nicht lesbar“ mit Zeilennummer
  und können über „Eigenes Format“ oder manuell erfasst werden. Rückfragen zu Formaten bitte mit einer
  anonymisierten Beispielzeile.

### Importprüfung: Abgleich je Zeile, Stapelaktionen, Verknüpfen

Hunderte mögliche Dubletten lassen sich in wenigen Schritten abarbeiten, ohne dass eine vorhandene Buchung
verändert wird. Hintergrund und Plan: [`docs/RECONCILIATION.md`](docs/RECONCILIATION.md).

**Ergebnis je Zeile** – jede Zeile wird gegen ihre beste vorhandene Buchung Feld für Feld bewertet (Kennung,
Blockchain-Hash, Konto, Asset, Menge, Gebühr, Zeitpunkt, Art, EUR-Wert); gleiche Beträge oder Zeitpunkte allein gelten
nie als derselbe Vorgang:

| Ergebnis | Bedeutung | Vorschlag |
|---|---|---|
| Eindeutige Dublette | derselbe Vorgang, die neue Quelle bringt nichts hinzu | verknüpfen |
| Ergänzende Informationen | derselbe Vorgang, die Quelle ergänzt z. B. Hash, genaue Uhrzeit, Zeitpunkt bzw. EUR-Wert der Originalquelle, Kennung | verknüpfen |
| Neue Transaktion | kein Gegenstück im Bestand | übernehmen |
| Widersprüchlich / unklar | Gegenstück, aber abweichende Menge, Asset, Konto, Gebühr, Zeit oder EUR-Wert – oder nur gleiche Menge | einzeln prüfen |
| Komplexe Zuordnung | 1:n bzw. n:1: Teil eines Vorgangs, Hash mit fehlenden Teilen, rekonstruierte Buchung, Gegenbuchung nur im Import, mehrere Kandidaten | einzeln prüfen |

Die **Sicherheit** ist qualitativ (*sicher* = gleiche Kennung bzw. Blockchain-Transaktion und gleiche Werte, *hoch* =
alle Merkmale stimmen bis auf geringe Abweichungen, *mittel*, *niedrig* = nur gleiche Menge); keine Scheingenauigkeit
in Prozent. Unter jeder Zeile steht der Abgleich mit Belegen (✓), Abweichungen (≠ relevant, ~ gering), Ergänzungen,
Gebührenprüfung, Quellenvorrang und gegebenenfalls einem Korrekturvorschlag – Vorschläge werden nie ausgeführt.

**Gebührenprüfung.** Verglichen wird der Gesamtabgang, wie der Ledger bucht (Menge + Gebühr im selben Asset), dazu der
Beleg der Quelle (Bitpanda: Saldoverlauf `asset_balance_after`): *zusätzlich belastet* und vorhandene Buchung ebenso →
stimmig; *nicht belegbar* → offene Frage mit dem Gesamtabgang der vorhandenen Buchung; *im Betrag enthalten*, aber
vorhanden zusätzlich gebucht → Widerspruch mit Differenz und Korrekturvorschlag. Eine anders dargestellte Gebühr
(netto/brutto) gilt bei der Erkennung als derselbe Abgang, damit keine zweite Abbuchung entsteht.

**Seite eines erfassten Transfers – auch verzögert und unter anderem Kontonamen.** Führt der kuratierte Import eine
Auszahlung als Transfer „Börse → eigenes Wallet“ (Zeitpunkt der Auszahlung, Wallet-Name des Steuertools), liefert die
Wallet-Datenquelle denselben Vorgang als Zugang – oft Stunden bis Tage später (die Börse zahlt verzögert aus) und unter
ihrem eigenen Kontonamen. Ein solcher Zugang (bzw. Abgang) wird als Seite des Transfers erkannt und **nie automatisch
gebucht**:

| Regel | gleiches Konto | anderer Kontoname |
|---|---|---|
| Menge | ± 0,5 % bzw. Gebühr anders dargestellt | exakt gleich (nur Rundung der Quellen) |
| Zugang | 2 h vor bis 72 h nach dem Transfer; bei exakt gleicher, unverwechselbarer Menge (≥ 6 signifikante Stellen) bis 7 Tage | ebenso |
| Abgang | ± 2 h | ± 2 h |
| ausgeschlossen | verschiedene Transaktions-Hashes | zusätzlich: Fiat, Zugang auf dem Absenderkonto, Zielkonto des Transfers von einer anderen Datenquelle geführt |

Bewertung bei anderem Kontonamen: *Widersprüchlich* (Konto), Sicherheit *mittel*; eine spätere Gutschrift gilt nicht als
Zeitwiderspruch, ein abweichender EUR-Wert (andere Bewertungszeitpunkte) nur bis 15 % als gering. Belegt die Notiz
der Transfer-Buchung den Zeitpunkt der Gutschrift (z. B. „Zugang … am 2024-05-02T10:15:00Z“), steht das bei den Belegen.
Vorgeschlagen werden **verknüpfen** (nicht buchen) und **Konten angleichen**. Bereits gebuchte App-Buchungen (z. B.
früher automatisch übernommene Zugänge) zeigt die Buchungsliste mit „Transferseite?“, Gegenüberstellung und den
Entscheidungen *Import-Transfer gilt* / *Keine Dublette*; dieselben Fälle stehen unter *Buchungen → Abgleich mit dem
Import* und in der *Datenqualität* (mit Vorschau der Bestandswirkung und Rückgängig). Die Datenquelle zeigt unter
*Synchronisierung* „Konto laut Abgleich“ mit einer Umstellung per Klick (ohne Neuabruf, zurücknehmbar; übernommene
Buchungen bleiben auf ihrem Konto) – automatisch umgestellt wird nur aus dem Hash-Abgleich, nie aus Transferseiten.

**Weitere Schutzregeln der Erkennung:** zwei verschiedene Blockchain-Transaktionen sind nie Dubletten (auch bei gleicher
Menge und Zeit); weitere Zeilen eines Ereignisses, dessen Hauptzeile bereits vorhanden ist (z. B. Gebührenzeile), gehen
nie still in die Buchungen (Schutz vor doppelten Gebühren); gleiche Menge, Konto und Zeit mit **anderem Asset** →
Widerspruch („Asset-Zuordnung prüfen“); ein EUR-Wert, der stark vom vorhandenen abweicht → Widerspruch („Kurs- bzw.
Asset-Zuordnung prüfen“). Zeilen **vor dem Stichtag** werden mitgeprüft, ohne ihren Status zu ändern: gefunden →
im Bestand; nicht gefunden → *mögliche Lücke im kuratierten Import* (Hinweis, Übernahme nur ausdrücklich).

**Vorschlag: Importprüfung** (Karte oben im Prüf-Stapel): Zahlen je Gruppe (Eindeutige Dubletten, Ergänzungen, Neu,
Manuell prüfen), Gruppen an- bzw. abwählen, *Alle auswählen*, *Vorauswahl* (Dubletten und Ergänzungen mit hoher
Sicherheit), *Leeren*; einzelne Zeilen in der Liste zusätzlich ab- oder anwählen (gespeichert, gilt über Seiten
hinweg), *Diese Seite* bzw. *Alle gefilterten* auswählen. Filter: Ergebnis, Sicherheit, Abweichungsart, Entscheidung,
Konto/Wallet, Zeitraum, dazu die Status-Reiter.

**Vorschau → Ausführen → Verlauf.** Die Vorschau zeigt je Aktion (Vorschlag anwenden, Verknüpfen, Übernehmen, Auslassen,
Dauerhaft ignorieren, Vorschlag wiederherstellen) die Wirkung je Zeile, die **Bestandswirkung** je Konto und Asset
(jetzt → danach, negative Bestände markiert), den Schutz vor Doppelbuchung (was sonst gebucht würde), offene Fragen,
Korrekturvorschläge und alle **Ausschlüsse mit Grund**. Ausgeführt wird genau einmal je Vorschau und alles oder nichts
in einer Transaktion; eine inzwischen veraltete Vorschau wird abgewiesen. Jede Aktion steht mit Vorher-Zustand im
*Verlauf der Stapelaktionen* und lässt sich rückgängig machen (nur, was seitdem unverändert ist; übernommene Buchungen
werden wie beim Rückgängigmachen eines Imports zurückgenommen).

Feste Regeln: Dubletten und Ergänzungen werden per Stapel nie gebucht, nur verknüpft; *Übernehmen* per Stapel nur für
„neu“ ohne Prüfhinweis – Widersprüche, komplexe Fälle und Vorgänge vor dem Stichtag nur mit ausdrücklicher Bestätigung;
Transfer-Paare nur gemeinsam.

**Verknüpfen statt verwerfen.** Die vorhandene Buchung bleibt unverändert. Die Zeile wird als verknüpfter
Quelldatensatz mit ihren Werten (Zeitpunkt, Beine, Gebühr samt Beleg, EUR-Wert samt Herkunft, Hash, Kennungen,
Rohdaten) und dem Abgleich zum Zeitpunkt der Verknüpfung gespeichert; unter *Buchungen* zeigt „+1 Quelle“ beide
Werte nebeneinander (z. B. EUR-Wert laut Koinly und laut Bitpanda). Künftige Abrufe bzw. Importe derselben Quelle
erkennen die Zeile wieder (Zeilenkennung; bei 1:1-Ereignissen auch die Anbieter-ID). Verknüpfungen gehören zum
Gesamtexport. Verschwindet die verknüpfte Buchung (gelöscht, nicht mehr im Import), erscheint die Zeile wieder zur
Prüfung.

**Quellenvorrang je Feld** (nur Vorschlag, nie automatisch überschrieben; manuelle Korrekturen gehen immer vor):

| Feld | Vorrang |
|---|---|
| Transaktions-Hash, Netzwerkgebühr | Blockchain → Börse → Steuertool |
| Zeitpunkt, Börsengebühr, EUR-Wert der Ausführung | Börse → Blockchain → Steuertool |
| Einordnung, Verknüpfungen (z. B. Transfer zwischen eigenen Wallets), Gegenkonto | Steuertool bzw. Nutzer → Blockchain → Börse |

**Bearbeitungsreihenfolge:** Die Import-Übersicht listet unter *Zuerst prüfen* offene Prüf-Stapel – neue Quellen
(noch nichts übernommen oder verknüpft) und Vorgänge ohne Gegenstück zuerst, dort fehlen am ehesten Buchungen.

Grenzen: Die Bewertung stützt sich auf die Daten beider Seiten; fehlen Angaben (z. B. Bitpanda liefert keinen
Empfänger und keinen Hash), bleibt die Sicherheit entsprechend niedriger. Werte der neuen Quelle werden nicht in die
vorhandene Buchung übernommen – dafür gibt es den Korrekturvorschlag und „Bearbeiten“.

---

### Belege: PDF & Screenshot

**Buchungen → PDF & Screenshot** liest Abrechnungen, Kontoauszüge, Dividendengutschriften, Wallet-Belege und
Screenshots (PDF, PNG, JPEG, WebP; bis 20 Dateien bzw. 100 MB je Stapel, 25 MB je Datei) **lokal** – eingebetteter
PDF-Text bzw. Texterkennung mit Tesseract (im Image enthalten). Es wird **nichts gebucht**:

1. Dateien hineinziehen oder auswählen → „Hochladen und auswerten“. Der Fortschritt zeigt Hochladen und die Phasen
   (Text extrahieren, OCR, Transaktionen erkennen, recherchieren, abgleichen); „Abbrechen“ ist jederzeit möglich.
2. Die Vorgänge landen im bekannten **Prüf-Stapel** (wie beim CSV-Import): übernehmen, mit vorhandener Buchung
   verknüpfen, auslassen, Sammelaktionen, Rückgängig.
3. Die **Prüfansicht je Beleg** zeigt jedes Feld mit Status – **A belegt** (steht im Beleg), **B rekonstruiert**
   (eindeutig berechnet bzw. aus der Buchung mit derselben Anbieter-ID/demselben Hash), **C geschätzt** (Referenzkurs;
   nie als Buchungswert), **ungelöst** – samt Fundstelle, Ausschnitt aus dem Beleg, Widersprüchen, Recherche-Protokoll,
   bevorzugter Lösung und Alternativen. Fehlendes oder Falsches lässt sich korrigieren; der Beleg wird ohne erneute OCR
   neu bewertet.
4. Beschreibt der Beleg eine **vorhandene Buchung** (z. B. Bitpanda-Kauf mit Transaktions-ID), schlägt Portfolia
   „Bestehende Buchung ergänzen“ vor: EUR-Wert, Gebühr, Uhrzeit, Tx-Hash – mit Vorschau der Auswirkungen
   (Einstand, Ergebnisse, Steuer), Übernehmen und Rückgängig unter Datenqualität. Menge, Asset, Konto und Art werden nie
   geändert; manuell bearbeitete Buchungen nie überschrieben.

Datenschutz: Originale bleiben unverändert im Datenverzeichnis (`/data/documents`, abschaltbar) und lassen sich samt
extrahiertem Text löschen; Protokolle enthalten keine Belegdaten; keine KI-Dienste. Öffentliche Blockchain-Explorer
(Bitcoin: mempool.space, Kaspa: api.kaspa.org) nur nach Freigabe in den Beleg-Einstellungen und je Stapel – übertragen
wird ausschließlich der Transaktions-Hash. Gleicher Beleg erneut → erkannt; geänderte Fassung → verweist auf die
frühere. Belege sind nicht Teil des vollständigen Exports (die Buchungen schon). Details, Grenzen und Messwerte:
[docs/M25_DOCUMENT_IMPORT.md](docs/M25_DOCUMENT_IMPORT.md).

Grenzen: Die Erkennung ist regelbasiert und mit synthetischen Belegen getestet, nicht mit Originalbelegen jedes
Anbieters; Unbekanntes landet als „ungeklärt“ im Prüf-Stapel. Handschrift und stark verzerrte Fotos werden nicht
zuverlässig gelesen.

### Als App auf dem Smartphone

Portfolia ist als App installierbar (Web-App-Manifest, Icons, Service Worker): **Chrome (Android)** → Menü →
*App installieren*, **Firefox (Android)** → Menü → *Zum Startbildschirm hinzufügen* / *Installieren*,
**Safari (iOS)** → *Teilen → Zum Home-Bildschirm*. Installiert startet Portfolia ohne Browserleisten, mit eigenem
Symbol und als eigener Eintrag in der App-Übersicht.

**Voraussetzung HTTPS.** Chrome und Firefox installieren Web-Apps nur von sicheren Adressen (`https://…`, außerdem
`localhost`). Über `http://192.168.x.x:8080` legen sie lediglich eine **Verknüpfung** an, die im Browser öffnet –
das ist eine Regel der Browser, nicht von Portfolia. Wege zu HTTPS im Heimnetz:

* **Reverse Proxy** auf dem Unraid-Server (z. B. Nginx Proxy Manager, SWAG, Caddy) mit gültigem Zertifikat für
  einen eigenen Namen, z. B. `portfolia.example.de` per DNS-Challenge (Let's Encrypt) – auch ohne Freigabe ins
  Internet. Portfolia selbst bleibt unverändert (Port 8080 nur intern).
* **Tailscale** (`tailscale serve --https=443 http://localhost:8080` auf dem Server): HTTPS-Adresse
  `https://<server>.<tailnet>.ts.net` mit gültigem Zertifikat, erreichbar von allen Geräten im Tailnet.
* Nur zum Ausprobieren mit Chrome: `chrome://flags/#unsafely-treat-insecure-origin-as-secure`, dort
  `http://192.168.x.x:8080` eintragen und Chrome neu starten (unterläuft den Schutz – nicht dauerhaft).

Auf dem Smartphone bleibt die untere Leiste auch bei geöffnetem Positionsdetail sichtbar; das Detail endet über
ihr. Breite Tabellen scrollen innerhalb ihres Rahmens, die Seite selbst wird nie breiter als der Bildschirm.

Mit Basic Auth sind Manifest, App-Icons und Service Worker ohne Anmeldung abrufbar (sie enthalten keine Daten);
alles andere bleibt geschützt, beim Start fragt die App nach den Zugangsdaten. Der Service Worker speichert nichts
zwischen und greift bei bestehender Verbindung nicht ein; nur ohne Netz zeigt er eine Hinweisseite. Firefox auf dem
Desktop bietet keine Installation von Web-Apps an.

## Datenquellen: Börsen und Wallet-Adressen

*Einstellungen → Datenquellen* verwaltet Börsenkonten und öffentliche Wallet-Adressen als Quellen für Buchungen:
anlegen, ansehen, bearbeiten, deaktivieren und entfernen – auch auf dem Smartphone.

> **Stand 0.23:** Automatische Anbindung für **Bitpanda** (Public API), **Binance** (Spot-API, siehe
> [Binance einrichten](#binance-einrichten)) und für **Wallets auf zwölf Chains** (Bitcoin, Ethereum, BNB Chain,
> Polygon, Avalanche C-Chain, PulseChain, XRP Ledger, Cardano, Polkadot, peaq, Solana, Kaspa) – ausschließlich
> lesend. Alle Anbindungen sind
> mit anonymisierten bzw. synthetischen Testdaten (Fixtures) und nachgebildeten Anbieter-APIs geprüft, **noch
> nicht live** – siehe [Grenzen der Bitpanda-Anbindung](#grenzen-der-bitpanda-anbindung) und
> [Wallets](#wallets-read-only-zwölf-chains). Alle anderen Börsen und Chains zeigen ehrlich **„Manuell / noch
> nicht unterstützt“** und verweisen auf den CSV-Import.

**Unabhängige Abrufe und Abbrechen (0.21.2):** Jede Datenquelle hat ihre eigene Sperre – ein langsamer Abruf (z. B.
KRC-20 mit Ratenlimit) blockiert Bitpanda und andere Wallets nicht; dieselbe Quelle läuft nie doppelt. Abrufe beim
Anbieter laufen parallel, Abgleich und Übernahme nacheinander (quellenübergreifende Dublettenerkennung sieht so stets
den vollständigen Stand). Der Zeitplan startet fällige Quellen je in eigenem Thread. Laufende Abrufe zeigen
*Abbrechen*: der Abruf endet an der nächsten Prüfstelle (Anfrage, Wartezeit, Fortschritt), der Abrufstand bleibt
unverändert, die Quelle geht nicht in den Fehlerzustand. Eine verwaiste Anzeige „Abruf läuft“ (z. B. nach Neustart)
setzt *Abbrechen* sofort zurück. Ein bereits abgerufenes Ergebnis wird nicht mitten im Einbuchen unterbrochen.

**Datensatz:** Art (Börse oder Wallet-Adresse), Anbieter bzw. Chain, frei wählbarer Name, Konto in Portfolia (auf
das gebucht wird – bei vorhandenen Buchungen aus Import oder CSV dasselbe Konto wählen), öffentliche Adresse bzw.
xpub (formal geprüft; private Schlüssel und Seed-Phrasen werden abgelehnt, weder gespeichert noch zurückgespielt),
API-Key (verschlüsselt, siehe unten) mit optionalem Ablaufdatum, Synchronisierungsintervall (nur manuell,
stündlich, alle 6/12 Stunden, täglich), automatische Übernahme (Standard: aus), Status, letzter Lauf, letzter
erfolgreicher Lauf, letzter Fehler, nächster Lauf, Abdeckung des letzten Abrufs und Laufhistorie.

**Status:** *angelegt* · *verbunden* (Verbindungsprüfung erfolgreich, noch nicht synchronisiert) ·
*synchronisiert* bzw. bei Wallets *vollständig synchronisiert* (letzter Abruf nachweislich vollständig, ohne erkannte
Lücke) · *Erstabruf unvollständig* (lange Historie, wird in Etappen automatisch fortgesetzt) · *teilweise
synchronisiert* (Seitenende oder Abdeckung unklar, Drosselung, Teilfehler, erkannte Lücke – eine erfolgreiche
HTTP-Antwort allein genügt nicht; der nächste Lauf holt erneut ab) · *Fehler* mit verständlicher Meldung, z. B. „API-Key abgelaufen“, „Berechtigung fehlt“, „Anbieter drosselt
Anfragen (HTTP 429)“, „Anbieter vorübergehend nicht erreichbar“. *Deaktiviert* stoppt nur den Zeitplan.

### Bitpanda einrichten

Alles geschieht in der App; einmalige Voraussetzung ist der [Master-Key](#master-key-für-api-keys).

1. **API-Key bei Bitpanda erstellen** (*Profil → API-Key*, app.bitpanda.com/my-account/apikey, Reiter „Bitpanda“) –
   nur Leserechte:

   | Recht bei Bitpanda | Bedarf | Wofür |
   |---|---|---|
   | **Transaction** (lesen) | **erforderlich** | Vorgänge (`GET /v1/operations`) |
   | **Balances** (lesen) | optional | Bestandsprüfung (`GET /v1/portfolio`, `balance.value`) – nur Hinweis, nie Buchung |
   | – | kein Recht nötig | Asset-Stammdaten (`GET /v1/assets`, `/v1/currencies`) – laut Referenz öffentlich |
   | **Trade (Write), Earn (Write)** | **nie aktivieren** | Portfolia handelt nie und ruft keine schreibenden Endpunkte auf |

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
| Kauf Krypto gegen Fiat, auch Sparplan (`buy`, `savings_plan`) | Kauf, Wert = Fiat-Betrag | genau ein Fiat-Ausgang und ein Krypto-Eingang |
| Verkauf Krypto gegen Fiat | Verkauf, Wert = Fiat-Betrag | genau ein Krypto-Ausgang und ein Fiat-Eingang |
| Swap Krypto → Krypto (`swap`, über EUR) | zwei Buchungen: Verkauf gegen EUR + Kauf mit EUR, Werte aus den Euro-Teilen | je ein Verkaufs- und Kauf-Paar (Transaktionsart `sell`/`buy`) in derselben Fiat-Währung |
| Einzahlung Fiat oder Krypto, Sparplan-Einzahlung | Zugang | ein Eingang, Vorgangsart „deposit“ bzw. Sparplan mit Transaktionsart „deposit“ |
| Auszahlung Fiat oder Krypto | Abgang und Gebühr (siehe *Gebühren*) | ein Ausgang, Vorgangsart „withdraw…“ |
| Rewards: `reward`, `staking_reward`, `passive_earn_reward`, `onetime_reward`, Cashback, Airdrop | Zugang mit Ertrags-Tag (`reward`, `staking`, `bonus`, `cashback`, `airdrop` …) | ein Krypto-Eingang |
| Token-Umstellung (`merger_crypto`, Migration) | Umstellung (Kapitalmaßnahme, Einstand geht über), **prüfbedürftig** | ein Krypto-Ausgang und ein Krypto-Eingang |
| eigener Gebühren-Teil (z. B. in BEST) | Gebührenzeile desselben Vorgangs | Transaktionsart „fee“ |
| `fee_amount` an einem Haupt-Teil | Gebühr so, wie der Saldoverlauf sie belegt; ohne Beleg **prüfbedürftig** | siehe *Gebühren* |
| `trade.fee` (Handelsgebühr) | im Betrag enthalten → nur Hinweis; zusätzlich → Gebühr an der Buchung | Kurs `rate`/`rate_with_fee` und Saldo, sonst **prüfbedürftig** |
| interne Umbuchung (gleiches Asset und gleicher Betrag ein und aus), Staking `stake`/`unstake` | keine Buchung, im Lauf gezählt | – |

**Bewusst nicht automatisch – „ungeklärt“ mit Grund:** Korrekturen und Stornos (`compensates`) samt dem
stornierten Vorgang, Tausch Krypto → Krypto ohne Euro-Teile, Fiat → Fiat, Aktien und ETFs (Bitpanda Stocks),
Edelmetalle, Kryptoindizes, unbekannte Assets oder Vorgangsarten sowie Vorgänge ohne Zeitpunkt oder Richtung (mit
den gelieferten Feldnamen im Hinweis). Sie werden weder still verworfen noch als Kauf oder Verkauf geraten.

**Vertrag (offizielle Referenz, [docs.public.bitpanda.com](https://docs.public.bitpanda.com/list-operations-4375770e0),
geprüft am 02.10.2026):** `GET /v1/operations` mit `page_size` (Standard 25), `cursor`, `from`/`to`; Antwort `data[]`,
`self_cursor`, `next_cursor`, `has_next_page`. Je Vorgang `operation_id`, `operation_type`, `transactions[]`; je Teil
`flow` (`INCOMING`/`OUTGOING`), `credited_at`, `transaction_type`, `wallet_id` und die Betragsobjekte `asset_amount`,
`fee_amount`, `asset_balance_after` (`{value, asset_id | currency_id}`), dazu `compensates` und `trade` (`trade_id`,
`fee`, `rate`, `rate_with_fee` …). Bestände: `GET /v1/portfolio` → `data[].balance.value`. Portfolia liest genau diese
Felder – keine geratenen Ersatzfelder; nicht dokumentierte Felder und fehlende Pflichtfelder zeigt die Abdeckung des
Laufs mit Namen (nie mit Werten).

**Zeitpunkt:** ausschließlich `transactions[].credited_at` (bei mehreren Teilen der früheste). Fehlt er, bleibt der
Vorgang *ungeklärt* und trägt im Prüf-Stapel „Zeitpunkt fehlt“ – mit Mengen, ohne Datum, nie gebucht. Kein Ersatz
durch den Abrufzeitpunkt; liefert Bitpanda den Zeitpunkt später, ersetzt der nächste Abruf die Zeile.

**Gebühren** (Bedeutung nicht dokumentiert – übernommen wird nur, was die Daten selbst belegen):

* `fee_amount`: Der Saldoverlauf (`asset_balance_after` desselben Wallets gegenüber dem vorherigen Teil im selben
  Abruf) zeigt, ob die Gebühr *zusätzlich* abgezogen wurde (Buchung: Betrag + Gebühr) oder *im Betrag* steckt
  (Abgang = Betrag − Gebühr, dazu die Gebühr; bei Eingängen nur Hinweis). Ohne Beleg: Betrag + Gebühr, prüfbedürftig.
* `trade.fee`: Betrag ≈ Menge × `rate_with_fee` → Gebühr im Fiat-Betrag enthalten (Einstand bzw. Erlös stimmen ohne
  weitere Gebühr, Hinweis an der Zeile); Betrag ≈ Menge × `rate` → zusätzlich (Gebühr an der Buchung, prüfbedürftig,
  solange der Saldoverlauf die Abbuchung nicht belegt); sonst prüfbedürftig, nichts geraten.

**Pagination:** Jede Seite wird vollständig verarbeitet; dann entscheidet `has_next_page`. `false` beendet den Abruf –
auch wenn `next_cursor` gesetzt ist; `true` setzt mit `next_cursor` unverändert fort. `self_cursor` und
Vorgangskennungen sind nie Fortsetzungspunkte. Als *teilweise* (Abrufstand rückt nicht vor, Erfolgszeitpunkt
bleibt) enden: fehlendes oder nicht boolesches `has_next_page`, `true` ohne `next_cursor` oder mit
`next_cursor = self_cursor`, ein wiederholter Cursor, eine Seite nur mit bereits gelieferten Vorgängen, drei leere
Seiten in Folge trotz `true`, Abbruch durch Drosselung bzw. Störung und mehr als 2000 Seiten. Die Seitenlänge
entscheidet nie über das Ende. `page_size` = 100 (Höchstwert nicht dokumentiert); lehnt Bitpanda das mit HTTP 400 ab,
gilt der dokumentierte Standard 25. Wie das Ende erkannt wurde, steht in der Abdeckung („Ende has_next_page=false“).

**Technik und Aufwand:** nur `GET` an `https://api.public.bitpanda.com/v1` mit Header `x-api-key` – keine
schreibenden Aufrufe, kein Rückgriff auf die ältere API `api.bitpanda.com`, Umleitungen werden nicht verfolgt.
Folgeläufe fragen mit `from` (Format `2024-01-01T00:00:00.000Z`) ab dem letzten vollständigen Stand minus 2 Tage ab;
eine neue Auswertungsversion (Parser), das Verwerfen eines Prüf-Stapels oder „Vollständig neu abrufen“ holen die
ganze Historie. Asset-Stammdaten werden gesammelt (`/assets?id=…`) abgerufen und 30 Tage zwischengespeichert.
Timeouts 20 s, bei 429 Warten nach `Retry-After` (höchstens 60 s je Wartezeit, 120 s je Lauf), bei 5xx drei Versuche.
Beträge exakt als Dezimalzahl, Zeitpunkte in UTC; je Zeile bleiben Auswertungsversion, Zeitquelle, Teile und die
Originalantwort des Vorgangs als Herkunft gespeichert (im Prüf-Stapel unter „Herkunft“). Ereignis-ID
`bitpanda:<operation_id>`, Zeilen `…#0`, `…#1` (fest), dazu Aliase für Transaktions- und Trade-IDs (`trade.trade_id`)
– so wird dieselbe Buchung aus dem Bitpanda-CSV-Export (Transaktions-ID `T…`) erkannt.

**Bestände gegenprüfen:** Jeder Lauf liest `/v1/portfolio` (Leserecht „Balances“). Die Datenquelle zeigt je Asset den
Bitpanda-Bestand neben dem Bestand aus Portfolia-Buchungen des Kontos; nach vollständigem Abruf zusätzlich den
Abgleich mit der Summe aller Vorgänge – für alle Asset-IDs beider Seiten, mit der Lesart, die den Bestand erklärt
(z. B. „Gebühren zusätzlich abgezogen“, „ohne Staking-Umbuchungen“), und dem letzten Saldo laut Vorgängen. Eine
Antwort ohne auswertbare Position (`balance.value`) gilt nicht als geprüft. Abweichungen sind Hinweise – es entstehen
nie Ausgleichsbuchungen.

**Ältere Auswertungen ersetzen (Reparaturweg):** Jede Prüfzeile trägt die Version ihrer Auswertung. Liefert ein
Abruf einen Vorgang erneut und unterscheidet sich die neue Auswertung, ersetzt Portfolia dessen Zeilen in offenen
Prüf-Stapeln derselben Datenquelle – nur wenn sie unbearbeitet sind (keine Entscheidung „übernehmen ja/nein“, kein
eingetragener Wert, keine Transfer-Bestätigung, nichts übernommen). Bearbeitete bleiben stehen; der Prüf-Stapel
nennt ihre Zahl und bietet „Veraltete Zeilen neu auswerten“ (Eingaben daran verwerfen, vollständig neu abrufen).
„Dauerhaft ignorieren“ gilt je Vorgang und damit auch für die neue Auswertung; übernommene Buchungen bleiben
unverändert, und ein bereits übernommener Vorgang wird nicht ein zweites Mal gebucht – auch wenn die neue
Auswertung ihn anders auf Zeilen verteilt. Wiederholte Läufe ersetzen nichts doppelt. Wartende Vorgänge blockieren
nur ihre eigene Datenquelle, nie ein anderes Bitpanda-Konto.

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
2. Die Zeilen durchlaufen **denselben Weg wie der CSV-Import**: Abgleich über den Transaktions-Hash, Symbole
   zuordnen, EUR-Werte, Validierung, Dubletten, Stichtag, Transfer-Abgleich – nichts umgeht Portfolio- oder
   Steuerlogik. Der Kasten *Abgleich mit vorhandenen Buchungen* zeigt, was bereits existiert, was neu ist und was
   noch eine Entscheidung braucht.
3. **Prüfen und übernehmen** (*Synchronisierung prüfen*). Übernommene Buchungen heißen `PF-S-…`, tragen Quelle
   „Datenquelle · <Anbieter>“, Ereignis-ID, Zeile und Datenquelle und sind unter *Buchungen* bearbeitbar.

**Fortschritt:** Alle Wege (Datenquellen-Sync, Erstabruf, CSV-Import, Import-ZIP, Kurshistorie) melden über
denselben Mechanismus (`app/progress.py`) mit festen Phasen *Vorbereitung → Daten abrufen → Verarbeiten → Abgleichen
→ Kurse ergänzen → Speichern → Fertig*, z. B. „Synchronisierung 63 %“ und „Bitpanda – 1.284 / 2.013 Datensätze
verarbeitet“. Der Prozentwert steigt nur; ein Balken oben auf jeder Seite zeigt laufende Vorgänge.

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
  aufzuhalten – ebenso mögliche Transfers ohne Entscheidung (Vorschlag mittlerer Sicherheit oder Gegenbuchung nur
  im kuratierten Import). Offene Prüfungen blockieren den Zeitplan nicht.
* **Nie parallel:** Zeitplan und „Jetzt synchronisieren“ teilen sich eine Sperre.
* **API-Key ersetzen oder entfernen** ändert keine Buchungen. **Entfernen** der Datenquelle löscht Konfiguration,
  verschlüsselten Schlüssel (SQLite `secure_delete`, WAL wird geleert), Laufhistorie und offene Prüf-Stapel;
  übernommene Buchungen bleiben und werden von einer neu angelegten Quelle desselben Anbieters erkannt (kein
  Doppelimport). Ändern von Anbieter, Adresse oder Konto in den Einstellungen setzt Status und Abrufstand zurück;
  die Konto-Umstellung aus dem Abgleich ruft nicht neu ab.
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
* **Transferseite, nur Vorschlag:** ein Zu- bzw. Abgang der App (z. B. aus einer Wallet-Datenquelle), der einer Seite
  eines Import-Transfers entspricht – auch verzögert gutgeschrieben und unter anderem Kontonamen (Regeln unter
  *Importprüfung*). „Import-Buchung gilt“ lässt den Transfer zählen (Einstand und Haltedauer wandern mit), die
  App-Buchung nicht mehr. Heißt dasselbe Wallet in Import und Datenquelle verschieden, danach die Konten angleichen –
  sonst laufen spätere Bewegungen der Datenquelle auf dem anderen Konto weiter.

### Grenzen der Bitpanda-Anbindung

* **Geprüft gegen die Referenz, nicht gegen ein echtes Konto:** Endpunkte, Parameter und Feldnamen folgen der
  offiziellen Referenz (Stand 02.10.2026); die Tests nutzen synthetische Antworten in deren Aufbau, und der
  Test-Server lehnt nicht dokumentierte Parameter, Endpunkte und Cursor ab. Echte Antworten wurden nicht geprüft –
  ob z. B. `credited_at` bei allen Vorgängen gefüllt ist, zeigt erst die Abdeckung des ersten Laufs („Zeitpunkt:
  transactions[].credited_at …×, fehlt …×“, fehlende Pflichtfelder, nicht dokumentierte Felder).
* **Nicht dokumentiert, deshalb nie vorausgesetzt:** Wertebereich von `operation_type`/`transaction_type`
  (beobachtet u. a. `buy`, `sell`, `swap`, `deposit`, `withdrawal`, `savings_plan`, `stake`,
  `passive_earn_reward`, `onetime_reward`, `merger_crypto`), Höchstwert von `page_size`, worauf sich `from` bezieht,
  ob Gebühren im Betrag enthalten sind, ob `balance` gestakte Mengen enthält, ob `asset_balance_after` je Wallet gilt.
* **Korrektur gegenüber 0.16.1:** 0.16.1 sendete `pageSize` statt `page_size`, folgte einem `next_cursor` trotz
  `has_next_page=false`, ersetzte einen wiederholten Cursor durch die letzte Vorgangskennung, prüfte Bestände über
  `/portfolio/holdings` und erkannte Zeitpunkte über geratene Feldnamen. 0.16.2 hält sich an die Referenz.
* **Scope-Fehler:** Ob Bitpanda ein fehlendes Leserecht mit 401 oder 403 beantwortet, ist nicht dokumentiert – die
  Unterscheidung „fehlendes Recht“ vs. „ungültiger Schlüssel“ stützt sich zusätzlich auf den Bestände-Test.
* **Gebühren:** Ob ein Betrag die Gebühr bereits enthält, ist nicht dokumentiert – belegt wird es nur über den
  Saldoverlauf bzw. die Kurse des Handels; sonst bleibt die Buchung prüfbedürftig.
* **Bestandsabgleich:** nur Plausibilität. Der Vergleich mit der Summe der Vorgänge braucht einen vollständigen Abruf
  der Historie; die Lesart (Gebühren, Staking) ist eine Erklärung, kein Beleg. Assets ohne Symbol in den
  Stammdaten erscheinen mit ihrer Bitpanda-Kennung; Stocks, Metalle und Indizes werden verglichen, aber nicht gebucht.
* **Nicht abgebildet:** Tausch Krypto → Krypto ohne Euro-Teile, Stocks/ETFs, Edelmetalle, Indizes, Korrekturen
  (siehe oben); für diese Fälle bleibt der CSV-Import bzw. die manuelle Erfassung.
* **Ohne Zeitpunkt:** Vorgänge ohne `credited_at` (z. B. noch nicht gutgeschrieben) bleiben ungeklärt, bis Bitpanda
  den Zeitpunkt liefert; inkrementelle Läufe sehen sie erst wieder, wenn er innerhalb des Abfragefensters liegt –
  sonst „Vollständig neu abrufen“.

### Vollständigkeit der Historie (Bericht je Datenquelle)

*Einstellungen → Datenquellen → Bitpanda → Vollständigkeit der Historie* bewertet nur Belege, nie eine erfolgreiche
Antwort allein:

* **Nachgewiesene Fehler:** Abruf unvollständig (Pagination/Fehler), fehlende Pflichtfelder, Brüche im Saldoverlauf
  (`asset_balance_after` passt nicht zum Betrag), Bestand laut `/portfolio` ≠ Summe der Vorgänge in jeder Lesart,
  übernommene bzw. verknüpfte Vorgänge, die der letzte **vollständige** Abruf nicht mehr liefert (Buchungen bleiben
  unverändert).
* **Plausible Datenlücken:** Monate ohne Vorgänge in sonst aktiven Zeiträumen, Buchungen des kuratierten Imports auf
  dem Bitpanda-Konto ohne Gegenstück in der API, Vorgänge mit älterer Auswertung.
* **Nicht verifizierbar:** Zeitraum vor dem ersten Vorgang laut API, Vorgänge ohne Zeitpunkt, Zeitraum seit dem
  letzten vollständigen Abruf (nur inkrementell), Bestand ohne vollständige Historie.

Dazu eine Tabelle *Vorgänge je Monat*. Grundlage sind die Kennzahlen, die jeder vollständige Abruf seit 0.17.0
festhält – nach dem Update einmal *Vollständig neu abrufen*. Ob ältere Vorgänge nach der API-Umstellung bei Bitpanda
fehlen, lässt sich nur so weit beurteilen, wie diese Belege reichen; der Bericht unterscheidet das ausdrücklich.

### Binance einrichten

Wie bei Bitpanda geschieht alles in der App (einmalig: [Master-Key](#master-key-für-api-keys)).

1. **API-Key bei Binance erstellen:** *Profil → API-Verwaltung → API erstellen → Systemgeneriert* (HMAC).
2. **Nur „Enable Reading“** aktiv lassen. **Nicht** aktivieren: Spot-/Margin-Handel, Futures, Auszahlungen, Universal
   Transfer. Optional die IP-Freigabe auf die feste öffentliche IP des Servers beschränken.
3. In Portfolia *Datenquellen → + Börse → Binance*, **API-Key und Secret Key** in die beiden Felder einfügen
   (Binance zeigt den Secret Key nur einmal), speichern, **Verbindung testen**: Spot-Bestände, Einzahlungen und
   Trades müssen lesbar sein; darf der Key handeln, warnt die Prüfung („Handelsrechte bei Binance abschalten“).
4. **Erstabruf starten.** Als Umgebungsvariable (Fortgeschritten) lautet der Wert `API-Key:Secret-Key`.

Beide Teile werden verschlüsselt gespeichert; angezeigt werden nur die letzten 4 Zeichen des **API-Keys** (nie des
Secrets). An api.binance.com gehen nur der API-Key (Header `X-MBX-APIKEY`) und die HMAC-SHA256-Signatur jeder Abfrage
– der Secret Key verlässt Portfolia nie. Es gibt ausschließlich lesende `GET`-Abfragen.

**Abgerufen:** Spot-Trades je Handelspaar (`myTrades`, Gebühr `commission` in `commissionAsset` als zusätzliche
Gebühr), Krypto-Ein- und -Auszahlungen (mit Tx-Hash für den Transfer-Abgleich mit Wallets), Ausschüttungen
(`assetDividend`: Simple Earn/Zinsen → *interest*, Staking → *staking*, Launchpool/Airdrop → *airdrop*, Unbekanntes →
*sonstiger Ertrag* zur Prüfung), Staubumtausch in BNB, Convert, Kauf/Verkauf mit Karte/Bank (`fiat/payments`) und
Fiat-Ein-/Auszahlungen (`fiat/orders`). Kennungen: `binance:trade:<PAAR>:<id>`, `binance:dep:<id>`, `binance:wd:<id>`,
`binance:div:<tranId>`, `binance:dust:<transId>`, `binance:convert:<orderId>`, `binance:fiatpay:<orderNo>`,
`binance:fiat:<orderNo>`. Ausstehende Vorgänge (Einzahlung „pending“, Auszahlung „in Bearbeitung“, Fiat
„Processing“) werden nicht gebucht und halten den Abrufstand bis zu 30 Tage zurück; abgelehnte erscheinen als „ohne
Buchung“.

**Etappen und Drosselung:** Binance begrenzt die Zeitfenster je Abfrage (Ein-/Auszahlungen < 90 Tage,
Ausschüttungen ≤ 180 Tage, Convert ≤ 30 Tage) und gewichtet einige Endpunkte je Konto sehr hoch (Auszahlungen,
Fiat-Aufträge). Portfolia fragt ab dem Binance-Start (14.07.2017) Fenster für Fenster ab, mit Mindestabständen je
Endpunkt (z. B. Auszahlungen 7 s, Fiat-Aufträge 16 s) und höchstens 4 Minuten je Lauf; der Abrufstand rückt nach
jedem vollständigen Fenster vor. Der **Erstabruf dauert deshalb mehrere Etappen** (Größenordnung 20–30 Minuten,
automatisch fortgesetzt), Folgeabrufe nur wenige Sekunden plus die Paarliste. HTTP 429 mit `Retry-After` ≤ 30 s
wird abgewartet, sonst (und bei 418) wird der nächste Lauf verschoben; Zeitabweichung (-1021) gleicht Portfolia
mit der Serverzeit ab.

#### Grenzen und Annahmen der Binance-Anbindung

* **Handelspaare:** Binance bietet keine Abfrage „alle eigenen Trades“. Portfolia fragt die Paare ab, deren Basis
  und Quote aus Beständen, Ein-/Auszahlungen, Ausschüttungen, Convert, Fiat, Staubumtausch oder früheren Trades
  bekannt sind (laut `exchangeInfo`; delistete Paare fehlen dort). Ein Trade in ein Asset, das sonst nie bewegt
  wurde, bleibt unsichtbar – die Bestandsprüfung zeigt die Differenz; dafür den CSV-Kontoauszug abgleichen.
  Sehr viele Paare werden über mehrere Läufe abgearbeitet.
* **Nicht abgerufen:** Earn-/Staking-Umbuchungen (Bestände in Earn fehlen in der Bestandsprüfung, sie zeigt nur
  Spot), Funding-Konto, Futures, Margin, Options, P2P, Binance Pay, Unterkonten, NFT, Binance Card.
* **Annahmen (gekennzeichnet, Prüf-Stapel/Bestandsprüfung zeigen Abweichungen):** `qty`/`quoteQty` brutto, die
  `commission` zusätzlich; ob die Auszahlungs-`amount` die `transactionFee` enthält, dokumentiert Binance nicht
  (Gebühr „offen“, zur Prüfung); Zeiten der Auszahlungen ohne Zonenangabe als UTC gelesen; bei Fiat-Kauf ist
  `sourceAmount` der Fiat-, `obtainAmount` der Kryptobetrag (beim Verkauf umgekehrt); Gebühren bei Fiat und
  Staubumtausch zur Prüfung. Das UID-Gewichtslimit ist in den gelesenen Doku-Seiten nicht beziffert – die
  Abstände sind konservativ gewählt.
* **Abgleich mit dem Binance-CSV:** Das CSV-Profil hat keine nativen Kennungen; frühere CSV-Importe erkennt der
  Abgleich über Menge/Zeit bzw. den Tx-Hash (Ein-/Auszahlungen) als mögliche Dublette.
* Gegen die offizielle Dokumentation (developers.binance.com) und einen strengen Mock geprüft (Signatur, Fenster,
  Paging, Statuswerte, 429/418/-1021), **nicht mit einem echten Konto**. Beim ersten Abruf „Verbindung testen“,
  Bestandsprüfung und Prüf-Stapel ansehen.

## Wallets (read-only, zwölf Chains)

*Einstellungen → Datenquellen → „+ Wallet-Konto“*: Chain wählen, Namen vergeben (z. B. „Ledger BTC“, „Ledger ETH“,
„MetaMask BNB“), optional einer **Wallet-Gruppe** zuordnen („Ledger“, „MetaMask“), öffentliche Adresse eintragen –
bei Bitcoin auch mehrere Adressen oder den **öffentlichen Kontoschlüssel** (xpub/ypub/zpub). Portfolia prüft die
Eingabe mit Prüfsumme (EIP-55, Bech32/Bech32m, Base58Check, Kaspa), zeigt nach „Verbindung testen“ die beobachteten
Bestände, holt mit „Erstabruf starten“ die Historie (im Hintergrund, mit Fortschritt, in Etappen) und synchronisiert
danach inkrementell. **Nie** werden Seed-Phrase, privater Schlüssel, Wallet-Signatur oder eine Verbindung zum
Ledger-Gerät verlangt – solche Eingaben werden abgelehnt. Portfolia schreibt nichts in eine Wallet.

**Jede Chain ist ein eigenes Konto:** Dieselbe 0x-Adresse auf Ethereum, BNB Chain, Polygon und Avalanche ergibt
vier Konten; Vorgänge (`ethereum:…`, `bsc:…`, `polygon:…`, `avalanche:…`) und Tokens (`USDC@ETH:0xa0b8…`,
`USDC@POLYGON:0x3c49…`) bleiben getrennt – es wird nie automatisch zusammengelegt.

### Wallet-Gruppen und Übersicht

Eine **Gruppe** („Ledger“, „MetaMask“, „Meine Hardware-Wallet“) ist nur eine Zuordnung: Ein Ledger mit BTC-, ETH-,
POL-, XRP-, ADA- und DOT-Konto sind sechs Konten in einer Gruppe – jedes mit eigener Chain, eigenen Adressen,
Vorgängen und eigener Synchronisierung. Neue Konten lassen sich direkt in einer Gruppe anlegen („+ Konto in
„Ledger““), bestehende über *Mehr … → Gruppe speichern* zuordnen; Gruppenname (*Gruppe umbenennen*, auch zum
Zusammenführen) und Kontoname sind unabhängig.

Die Übersicht zeigt je Konto Chain-Symbol, Name, Netzwerk und Anbieter, die gekürzte **kopierbare Adresse** mit
**Explorer-Link** (öffnest du selbst), den **EUR-Wert** des beobachteten Bestands, den Abrufzustand (*noch nicht
synchronisiert, läuft, erfolgreich, teilweise, Fehler*) mit letzter erfolgreicher Synchronisierung sowie getrennt
davon **Datenhinweise** (ungeklärte/nicht unterstützte Vorgänge, fehlende Kurse oder Zuordnungen,
Bestandsabweichungen, Überschneidungen). Je Gruppe und gesamt steht die Summe. **Suche** über Konto-, Gruppen- und
Portfolia-Kontoname, Netzwerk und Adresse; **Sortierung** nach Name, Wert, Hinzufügedatum oder letzter erfolgreicher
Synchronisierung. Aktualisieren geht je Konto, je Gruppe und für alle Wallets – nacheinander im Hintergrund; ein
fehlgeschlagenes Konto hält die übrigen nicht auf.

* **Kein irreführendes 0,00 €:** Ohne beobachteten Bestand steht „unbekannt“; fehlt für ein Asset Kurs oder
  Zuordnung, steht der bekannte Teil als „mind. …“ mit Liste der fehlenden Assets. Schlägt ein Abruf fehl, bleibt der
  letzte bekannte Bestand mit Zeitpunkt stehen („letzter bekannter Wert“) – übernommene Daten werden nie geleert.
* **Keine Doppelzählung:** Konten derselben Chain dürfen sich nicht überschneiden – gleiche Adresse, eine
  Einzeladresse, die ein Bitcoin-Kontoschlüssel desselben Kontos bereits abdeckt (abgeleitete Empfangs- und
  Wechselgeldadressen bis zum zuletzt geprüften Index bzw. Gap-Limit), oder eine Cardano-Adresse, deren Stake-Teil
  schon als Konto geführt wird. Solche Eingaben werden mit Begründung abgelehnt; ältere Überschneidungen werden
  angezeigt und in Summen nur einmal gezählt. Legt man eine Datenquelle für dieselbe Wallet neu an, erscheinen
  bereits übernommene Vorgänge (gleicher Hash, gleiche Buchungsseite und Menge) als **mögliche Dublette** statt
  erneut gebucht zu werden; zwei verschiedene eigene Wallets in derselben Transaktion (Transfer A → B, gemeinsame
  Ausgabe) bleiben dagegen getrennte Vorgänge.
* **Einzeladressen vs. Konto:** Bitcoin mit Kontoschlüssel (xpub/ypub/zpub) und Cardano über die Stake-Adresse
  erfassen alle Adressen inklusive Wechselgeld; reine Einzeladressen sind am Konto als eingeschränkte Abdeckung
  gekennzeichnet („Wechselgeld an nicht eingetragene Adressen erscheint als Abgang“).

### Abdeckung je Chain

| Chain | Adress-/Kontotypen | Native / Tokens | Historie | Gebühren | Anbieter (Standard · Alternative) | Keys | Kosten/Limits (Stand 10/2026) | Bekannte Lücken |
|---|---|---|---|---|---|---|---|---|
| **Bitcoin** | Einzeladressen P2PKH (1…), P2SH (3…), P2WPKH/P2WSH (bc1q…), P2TR (bc1p…), mehrere je Konto; Kontoschlüssel xpub/ypub/zpub mit wählbarem Typ (Legacy, Nested/Native SegWit, Taproot nach BIP44/49/84/86), Empfang + Wechselgeld bis Gap-Limit (Standard 20) | BTC | vollständig je Adresse (Esplora, 25 je Seite) | je Transaktion (`fee`), nur wenn alle Eingänge eigen | mempool.space · Blockstream Esplora | keine | kein Key; Limit nicht beziffert, Portfolia ≤ 1 Anfrage/s | Einzeladressen: Wechselgeld an nicht eingetragene Adressen zählt als Abgang (angezeigt); Adressen jenseits des Gap-Limits; Lightning, Multisig, Ordinals/Runes; CoinJoin/PayJoin nur als Saldo zur Prüfung |
| **Ethereum** | eine 0x-Adresse je Konto | ETH; ERC-20 (Contract-genau) | vollständig, Blockfenster ≤ 1.000 Einträge, nie mitten im Block | gasUsed × gasPrice der eigenen Transaktion | Etherscan API V2 · Routescan | Etherscan: kostenloser Key nötig | 3–5 Anfragen/s, 100.000/Tag, ≤ 1.000 Einträge je Anfrage | NFTs (ERC-721/1155) nicht gebucht (Prüfung zeigt, ob vorhanden); Positionen in Verträgen nicht sichtbar; interne Bewegungen laut Indexer-Trace |
| **BNB Chain** | wie Ethereum | BNB; BEP-20 | Standard **NodeReal BSCTrace** (`nr_getAssetTransfers`, Blockfenster ≤ 100.000, getrennt für Aus- und Eingänge, Seiten über `pageKey`); alternativ Etherscan wie Ethereum | eigene Transaktionen: `gasUsed × gasPrice`; ohne „external“-Eintrag (z. B. Token-Überweisung) aus `eth_getTransactionReceipt` | NodeReal BSCTrace · Etherscan API V2 | NodeReal: **kostenloser Key** (nodereal.io/MegaNode); Etherscan: **kostenpflichtiger Plan** | BscScan-API laut BNB Chain seit Ende 2025 abgekündigt, BNB Chain empfiehlt BSCTrace; Routescan führt Chain 56 nicht (live geprüft 10/2026); NodeReal-Kontingent in Compute Units laut Tarif | NodeReal **nicht live geprüft** (kein Key) – nach Referenz implementiert, erste Synchronisierung mit Bestandsprüfung kontrollieren; Erstabruf ab Block 0 in vielen Fenstern (Etappen); NFTs nicht gebucht |
| **Polygon PoS** | wie Ethereum | **MATIC bis Block 62.278.656, danach POL** (siehe unten); ERC-20 (Contract-genau) | wie Ethereum; native Überweisungen, die Polygon zusätzlich als Token-Transfer des Systemvertrags `0x…1010` meldet, werden nur einmal gezählt | wie Ethereum | Etherscan API V2 · Blockscout | Etherscan: kostenloser Key; Blockscout: keiner | Etherscan wie oben (Polygon im kostenlosen Plan); Blockscout ohne Key, ≤ 10.000 Einträge je Abfrage | wie Ethereum; Blockscout meldet interne Transaktionen älterer Blöcke teils als „noch nicht verarbeitet“ (→ angezeigte Lücke); Einzahlungen über die PoS-Bridge per State-Sync ggf. nicht in der Historie (Bestandsprüfung) |
| **Avalanche C-Chain** | wie Ethereum | AVAX; ERC-20 | wie Ethereum | wie Ethereum | Routescan (Snowtrace) · Etherscan (bezahlt) | optional (Routescan-Key) | ohne Key 2/s, 10.000/Tag; kostenloser Key 5/s, 100.000/Tag | wie Ethereum; X-/P-Chain nicht erfasst |
| **XRP Ledger** | eine klassische Adresse r… je Konto (X-Adressen werden abgelehnt – Destination Tags gehören nicht zur Adresse) | XRP; Trustline-Tokens je Währung **und** Emittent (`USD@XRPL:USD.r…`) | `account_tx` aufsteigend über `marker`, nur validierte Ledger; Lücke, wenn die Historie nicht mit der Kontoeröffnung beginnt | `Fee` der eigenen Transaktion (aus dem Saldo herausgerechnet); fehlgeschlagene `tec…` = nur Gebühr | xrplcluster.com · s2.ripple.com (beide Full History) | keine | kein Key; öffentliche Server, Portfolia ≤ 2/s | Bewegungen aus Saldoänderungen (auch Teilzahlungen, DEX-Ausführungen); DEX, AMM, Escrow, Payment Channels, NFTs zur Prüfung; MPT nicht gebucht (ungeklärt); Reserve bleibt Bestand (als gesperrt ausgewiesen); Destination/Source Tag in Notiz und Rohdaten |
| **Cardano** | **Stake-Adresse** stake1… (ganzes Konto) oder Adresse(n) addr1…; aus einer Basisadresse wird die Stake-Adresse abgeleitet (CIP-19); Enterprise-Adressen ohne Stake-Teil im Adressmodus | ADA; native Assets je Policy + Name über den CIP-14-Fingerabdruck (`HOSKY@CARDANO:asset1…`) | Koios `account_txs` (bzw. `address_txs`) in Seiten zu 1.000, Details über `tx_info`; ab 15 Bestätigungen | Gebühr nur, wenn alle Eingänge eigene sind | Koios (api.koios.rest) | optional (kostenloser Bearer-Token) | ohne Key 5.000 Anfragen/Tag, 100 je 10 s; mit kostenlosem Key 50.000/Tag | Wechselgeld an eigene Adressen ist kein Abgang (Stake-Konto); Pfand für Stake-Registrierung/Governance als prüfbedürftige Bewegung; Reward-Abhebung = Umbuchung, Rewards je Epoche als Ertrag (ab Verfügbarkeit); DEX/Smart Contracts und fremde Eingänge zur Prüfung; ungültige Plutus-Transaktionen nicht gekennzeichnet |
| **PulseChain** | eine 0x-Adresse je Konto | PLS; PRC-20 (Contract-genau) | ab dem ersten PulseChain-Block 17.233.001 (11.05.2023) in Fenstern zu 1 Mio. Blöcken – die Ethereum-Vorgeschichte davor wird nicht abgefragt; Fork-Bestand siehe unten | wie Ethereum | PulseChain-Explorer (Blockscout, Etherscan-kompatibel) | keine | kein Key; Limit nicht beziffert, Portfolia ≤ 2/s; Kaltstart einer Adresse bis ~20 s je Abfrage (Lesezeit 75 s) | Der Explorer ignoriert live `startblock`/`endblock` (nur `start_block`/`end_block` wirken) – Portfolia sendet beide und bricht ab, falls doch Blöcke außerhalb des Bereichs kommen; kopierte Tokens werden nicht eröffnet (Bestandsprüfung) |
| **Polkadot** | SS58-Adresse 1… (generisches Format 5… wird umgerechnet, Kusama abgelehnt) | DOT auf Relay Chain **und** Asset Hub; Asset-Hub-Tokens (`USDT@DOTAH:…`) | Subscan: Überweisungen, eigene Extrinsics, Rewards/Slashes je Netz in Blockbereichen, Seiten zu 100 | `fee_used` (sonst `fee`) der vom Konto signierten Extrinsics | PubFi-Gateway für Subscan · Subscan direkt | **Key nötig**: PubFi kostenlos bzw. Subscan bezahlt | PubFi-Free-Routen 2 Anfragen/s, 20.000/Tag | Einheiten der Subscan-Felder nicht dokumentiert (Prüfung `amount` ↔ `amount_v2`, Abweichung → Prüfung); Asset-Hub-Migration nicht gebucht (ungeklärt); Staking bindet nur (nur Gebühr), Rewards als Ertrag, Slashes zur Prüfung; Nomination Pools, XCM, Proxy/Multisig zur Prüfung; andere Parachains nicht erfasst |
| **peaq** | SS58-Konto (Präfix 1221; generisches 5… wird umgerechnet) **oder** 0x-Adresse (peaq EVM, z. B. MetaMask/Ledger) | PEAQ (18 Nachkommastellen) | SS58: wie Polkadot (Subscan-Routen: Überweisungen, Extrinsics, Rewards, Bestände); 0x über PubFi: Subscan löst die Adresse in das zugehörige Substrate-Konto auf (`v2/scan/search`), dann wie SS58; 0x mit direktem Subscan-Key: Etherscan-kompatible Route (txlist, intern, Token) | SS58: `fee_used`; 0x mit Subscan-Key: wie Ethereum | PubFi-Gateway · Subscan direkt | PubFi kostenlos; vollständige EVM-Historie nur mit **Subscan-Key (bezahlt)** | wie Polkadot | 0x über PubFi: nur native PEAQ-Bewegungen – EVM-Gas und ERC-20 fehlen (keine dokumentierte EVM-Transaktionsliste über PubFi), Bestandsprüfung zeigt die Differenz; Reward-Route ggf. ohne Daten (Hinweis statt Fehler); Einheiten wie bei Polkadot geprüft |
| **Solana** | eine Adresse je Konto | SOL (inkl. Miete der Token-Konten); SPL- und Token-2022-Tokens (Mint-genau) | Signaturen der Wallet **und** aller Token-Konten (aktuelle + frühere aus Transaktionen) | `meta.fee`, wenn die Wallet zahlt | öffentlicher RPC (Solana Foundation) · Helius | Helius: Key nötig | öffentlich 100/10 s je IP, 40/10 s je Methode, nicht für Dauerbetrieb; Helius frei 10/s | Native Staking/Inflations-Rewards nicht als Vorgang; mehrere Bewegungen desselben Tokens je Transaktion saldiert; NFTs gezählt, nicht gebucht; Token-Konten, die der Wallet nur per Autoritätswechsel gehörten und heute anderen gehören, nicht auffindbar |
| **Kaspa** | eine Adresse je Konto | KAS; KRC-20 über Kasplex | KAS vollständig (Blockzeit-Seiten, 30 min Überlappung); KRC-20-Operationen je Adresse | aus Eingängen − Ausgängen; KRC-20-Commit/Reveal als nur Gebühr | api.kaspa.org (KAS) · api.kasplex.org (KRC-20) | keine | kein Key; api.kaspa.org nicht beziffert (≤ 2/s); Kasplex laut Antwort-Header 1.000 je Zeitfenster (nicht dokumentiert) – Portfolia ≤ 5/s, KRC-20 mit eigenem Zeit-/Anfragebudget je Etappe | KRC-20 abhängig vom Kasplex-Indexer (Ausfall/„nicht synchron“ = sichtbare Lücke, KAS bleibt vollständig, Ergänzung per CSV); „nicht erreichbar (Netzwerk/DNS)“ liegt meist an der Namensauflösung im Container (DNS-/Werbefilter); KRC-721 nicht erfasst; Kurse kleiner KRC-20 über KaspaCom (siehe Kurse) |

### So arbeiten alle Chain-Adapter

* **Nur geprüfte Endpunkte:** Anbieter sind fest hinterlegt (HTTPS, kein Freitext-URL-Feld, keine Weiterleitungen,
  Host-Prüfung vor jeder Anfrage) – kein Server-Side-Request-Forgery. Beträge werden exakt als `Decimal` gelesen.
* **Anbieter-Schlüssel** (Etherscan, Routescan, Helius, Koios, PubFi, Subscan) gelten je Anbieter, werden unter
  *Datenquellen → Anbieter-Schlüssel* verschlüsselt gespeichert (derselbe [Master-Key](#master-key-für-api-keys)),
  nie angezeigt, nie exportiert; alternativ `PORTFOLIA_DS_ETHERSCAN`, `PORTFOLIA_DS_ROUTESCAN`,
  `PORTFOLIA_DS_HELIUS`, `PORTFOLIA_DS_KOIOS`, `PORTFOLIA_DS_PUBFI`, `PORTFOLIA_DS_SUBSCAN` (bzw. `…_FILE`). Fehlt
  ein nötiger Schlüssel, zeigt das Konto „Schlüssel fehlt“ mit Anbieter und Konditionen – es wird nichts abgerufen.
* **Datenschutz:** Der gewählte Anbieter sieht die abgefragten öffentlichen Adressen und die IP des Servers und
  kann sie verknüpfen. Ein Kontoschlüssel (xpub) legt alle Adressen des Kontos offen – ausgeben lässt sich damit
  nichts. Stückzahlen, Werte oder Kontonamen verlassen den Server nie.
* **Lückenlos und wiederholbar:** Abrufe in Seiten mit Mindestabstand, Wiederholung bei 429/5xx mit Backoff und
  `Retry-After`, begrenzte Parallelität, Anfrage- und Zeitbudget je Lauf. Der Fortsetzungspunkt rückt nur über
  vollständig verarbeitete Daten vor (Blöcke, Seiten, Signatur-Blöcke); ein abgebrochener Erstabruf setzt sich in
  Etappen selbst fort, auch nach Fehlern. Nur bestätigte Daten werden gebucht (Ethereum 64, BNB 20, Polygon 128,
  Avalanche 6 Blöcke, Bitcoin 3 Bestätigungen, Cardano 15 Blöcke, XRP Ledger validiert, Polkadot finalisiert laut
  Indexer, Solana „finalized“, Kaspa akzeptiert); Unbestätigtes wird gezählt.
* **Stabile Kennungen:** Ereignis `<chain>:<tx>:<eigenes Konto>`, je Bewegung eine feste Unterkennung (z. B.
  `#n:out`, `#t:<fingerabdruck>#1` für den zweiten gleichartigen Token-Transfer im selben Hash, `#fee`) – wiederholte
  und überlappende Läufe erzeugen keine Doppelungen.
* **Beobachtet ≠ gekauft:** Ein Eingang ist ein Zugang, kein Kauf; Anschaffungskosten und -daten werden nie erfunden.
  Swaps, Vertragsaufrufe, Bridges, Staking, Rewards, Mints, mögliche Spam-Tokens (Werbung/Links im Namen,
  unbekannte Tokens ohne eigene Aktion) und Unklares gehen mit Begründung in den Prüf-Stapel. 0-Wert-Transfers
  (Address-Poisoning) werden gezählt, nicht gebucht. Tokens werden über Chain + Contract/Mint/Tick zugeordnet, nie
  über das Symbol – ein gefälschter „USDC“ bleibt ein eigenes Asset (Vorschlag: ignorieren).
* **Abgleich mit Börsen und CSV:** Eine Bitpanda-Auszahlung und der passende Wallet-Eingang werden als Transfer
  vorgeschlagen (mit Begründung: gleicher Hash bzw. Menge/Zeitabstand) – Anschaffungsdatum und -kosten bleiben
  erhalten, nichts zählt als zwei unabhängige Vorgänge. Vorschläge, die eine bereits übernommene Buchung verändern
  würden, werden **nie automatisch** übernommen. Ist der Transfer schon erfasst (Import oder Journal), erscheint der
  Wallet-Vorgang als Dublette; derselbe Hash aus einem Wallet-CSV (z. B. Ledger Live) ebenso.
* **Bestandsabgleich:** Je Konto stehen **On-Chain beobachtet** (laut Anbieter) und **durch Portfolia-Buchungen
  erklärt** (übernommene Buchungen des Kontos) nebeneinander; Abweichungen werden angezeigt, nie ausgeglichen.
* **Status:** „vollständig synchronisiert“ nur, wenn die unterstützten Daten des Kontos ohne erkannte Lücke abgerufen
  sind. Erkannte Lücken (Anbieterfehler, nicht abrufbare Transaktion, Indexer nicht synchron) stehen als Warnung am
  Konto; dauerhafte Abdeckungsgrenzen sind am Konto aufgeführt.

### Fehlerarten und KRC-20 (Kasplex)

Fehler einer Datenquelle werden einzeln benannt: *API-Key fehlt*, *Zugangsdaten abgelehnt*, *Zugriff verweigert
(HTTP 403)*, *Anbieter drosselt Anfragen*, *nicht erreichbar*, *Endpunkt nicht mehr unterstützt*, *keine Daten*.
Ein 403 hinter Cloudflare (Bot-Schutz) wird als solcher erkannt.

**KRC-20 / HTTP 403:** Der Kasplex-Indexer (go-krc20d, API v1) beantwortet auch **Anwendungszustände** mit HTTP 403
und einer Meldung im JSON-Rumpf – u. a. `unsynced` (Indexer hinter der Chain) und `internal error`. Bisher wurde das
als Zugriffsfehler behandelt und der KRC-20-Abruf abgebrochen. Jetzt: vorübergehende Zustände → bis zu zwei
Wiederholungen mit Pause (5 s, 10 s), danach „Indexer vorübergehend nicht synchron“ als sichtbare Lücke; KAS bleibt
vollständig. Echte Sperren (Cloudflare, Zugriff verweigert) werden ohne Wiederholung gemeldet. Fallback-Kette:
Kasplex → (weitere Indexer, sobald ein dokumentierter verfügbar ist) → CSV-Ergänzung. Welcher Rumpf beim gemeldeten
Fehler konkret kam, ist nicht protokolliert – die Zuordnung zu `unsynced`/`internal error` ist eine begründete
Annahme.

### MATIC → POL auf Polygon PoS

Seit dem 04.09.2024 ist POL der native Coin von Polygon PoS (1:1 aus MATIC, automatisch, ohne Transaktion des
Nutzers); on-chain wurde der Ticker mit dem Hardfork „Ahmedabad“ (Block 62.278.656, 26.09.2024, PIP-45)
umbenannt. Portfolia bucht den nativen Coin bis zu diesem Block als `MATIC`, danach als `POL` und schlägt **einmal**
eine Umstellung `MATIC → POL` (Unternehmensereignis „migration“) über den aus der Historie berechneten Bestand vor –
nie automatisch, mit Hinweis, wenn die Historie nicht ab Block 0 vorliegt. Ist die Umstellung schon im kuratierten
Import erfasst, den Vorschlag ignorieren. Bestände außerhalb der Wallet-Anbindung (Börsen, kuratierter Import) stellt
[Ticker- und Token-Änderungen](#ticker--und-token-änderungen-z-b-matic--pol) um – der Hinweis erscheint automatisch. Kurse: CoinGecko `matic-network` (MATIC) bzw. `polygon-ecosystem-token`
(POL).

**Was live geprüft wurde und was nicht:** Alle Wallet-Anbindungen sind mit synthetischen Fixtures gegen nachgebildete
Anbieter-APIs getestet (Paginierung, Drosselung, Abbruch/Fortsetzung, Wiederholung ohne Doppelungen). Für XRP Ledger
(xrplcluster.com), Koios und Blockscout (Polygon) wurden zusätzlich die Antwortformate der dokumentierten Methoden
mit öffentlichen Beispieladressen der Dokumentation abgeglichen. **Subscan/PubFi konnte nicht live geprüft werden**
(jede Anfrage verlangt einen Schlüssel); dort beruhen Feldnamen auf der veröffentlichten OpenAPI-Beschreibung, die
Einheiten sind eine gekennzeichnete Annahme mit Prüfung je Vorgang. Das gilt ebenso für **peaq** (Subscan; Netz,
SS58-Präfix 1221 und 18 Nachkommastellen laut SS58-Registry bzw. PubFi-OpenAPI). Für **PulseChain** wurden Fork-Block,
Chain-ID 369 und die Antwortformate von Explorer-API und RPC mit öffentlichen Abfragen abgeglichen. Beim ersten
echten Abruf bitte „Verbindung testen“, die Bestandsprüfung und den Prüf-Stapel ansehen.

### PulseChain: Fork-Bestand

PulseChain entstand am 11.05.2023 als Kopie des Ethereum-Zustands am Block 17.233.000 (erster eigener Block
17.233.001); Salden und Token-Verträge wurden dabei übernommen. Portfolia fragt die Historie erst ab Block 17.233.001 ab und liest beim
**Erstabruf** einmalig per RPC (`eth_getBalance` am Fork-Block, rpc.pulsechain.com) den kopierten PLS-Bestand. Er
erscheint als **prüfpflichtige Eröffnung** (Tag *fork*, Zeitpunkt des ersten PulseChain-Blocks) – nie automatisch
übernommen; die steuerliche Einordnung (Zugang aus Fork, Anschaffung zu 0 € bzw. Wert) entscheidest du. Ist der RPC
nicht erreichbar, fehlt die Eröffnung mit Warnung; die Bestandsprüfung zeigt die Differenz. Kopierte PRC-20-Tokens
werden nicht eröffnet.

---

## Finanzielle Integritätsprüfung und Sammelbearbeitung

*Datenqualität → Integritätsprüfung* (`/quality/integrity`) prüft auf **einem** Datenstand (ein Ledger, eine
Kursliste): alle Befunde der [Diagnose](#diagnose-datenqualität-und-bestandsabgleich) plus Invarianten, die für ein
korrekt rechnendes Ledger immer gelten müssen – verbleibende Lots = Bestand (je Asset, je Konto), Veräußerung =
Summe ihrer Lot-Anteile, Anschaffung vor Veräußerung, keine negativen Lots, Token-Umstellungen ohne Restbestand und
mit wirksamen Buchungen –, die steuerliche Datenqualität aus dem Regelpaket je Jahr (fehlende Anschaffungskosten,
ungeklärte Umstellungen, Fondstypen …), Abweichungen der Steuerdaten je Jahr zum Journal, ungewöhnliche Kurssprünge
(z. B. nicht splitbereinigte Reihe) und die Bewertbarkeit der Historie. **Die Prüfung liest nur**; gespeichert wird
allein ihr Ergebnis.

| Feld | Inhalt |
|---|---|
| Befund-ID | stabil (Diagnose-Befunde: aus Art und betroffenen Buchungen; Invarianten: aus Prüfung und Objekt) |
| Kategorie | Bestand, FIFO & Kostenbasis, Dubletten & Transfers, Kurse & Bewertung, Steuerliche Datenqualität, Historie & Datenquellen |
| Schweregrad | kritisch, Warnung, Information |
| Ursache | nachgewiesener Rechenfehler · nachgewiesen · Abweichung aus unvollständigen Quelldaten · Verdacht · Hinweis |
| Auswirkung, Empfehlung, Alternativen | aus der Diagnose (bevorzugte Lösung + bis zu drei Alternativen) |
| Konfidenz | Abgleich: eindeutig · hoch plausibel · prüfbedürftig · widersprüchlich (erklärbare Stufen, keine Wahrscheinlichkeiten) |
| Status | offen · geprüft · korrigiert · verworfen (als unabhängige Buchung bestätigt) – aus den Entscheidungen der Diagnose |

Start als Hintergrundjob mit Fortschritt (nie zwei Läufe gleichzeitig), Filter nach Kategorie, Schweregrad, Konto,
Asset und Status, Sortierung nach betroffenem Wert bzw. Konfidenz, Export als CSV oder JSON. Ändern sich die Daten
während bzw. nach dem Lauf, ist das Ergebnis als „überholt“ markiert; ein fehlgeschlagener Lauf wird mit Fehler
angezeigt. Korrekturen laufen über die Vorschau der Diagnose bzw. die Sammelbearbeitung.

**Sammelbearbeitung** (`/quality/diagnose/bulk`): Dubletten- und Transfer-Befunde mit vorausgewählter bevorzugter
Lösung, sortiert nach Konfidenz bzw. Wert, filterbar nach Konto, Asset und Quelle. Die Sammelvorschau rechnet alle
Änderungen gemeinsam auf einer Kopie durch (Bestände, Einstand, Anschaffungsdaten offener Lots, realisierte Ergebnisse
je Jahr, Steuerwerte, Diagnose danach) und schließt aus: widersprüchliche Fälle, prüfbedürftige (nur nach
ausdrücklicher Einbeziehung), Befunde, die dieselbe Buchung betreffen, und Änderungen, die zusammen einen negativen
Bestand erzeugten. Übernommen wird in einer Transaktion, nur bei unveränderter Vorschau (Prüfsumme); jede Korrektur
ist einzeln protokolliert und rückgängig zu machen, die Sammlung auch als Ganzes. Bereits geprüfte bzw. als
unabhängig bestätigte Befunde werden nicht erneut vorgeschlagen, solange ihre Daten gleich bleiben.

**Automatisierung nach Risikostufen:**

| Stufe | Was | Wie |
|---|---|---|
| A – sichere technische Verknüpfung | Zeile eines Abrufs mit technischer Identität zu einer vorhandenen Buchung (gleiche Kennung bzw. Blockchain-Transaktion, Sicherheit „sicher“, ohne offene Gebührenfrage, ohne eigene Entscheidung) | bei Datenquellen mit „automatisch übernehmen“ automatisch **verknüpft** – nur Herkunft und Kennungen, keine Mengen, Gebühren, Lots oder Kosten; protokolliert als Stapelaktion, rückgängig machbar |
| B – hoch plausible finanzielle Korrektur | z. B. zusätzliche Buchung ausblenden, zwei Buchungen als Transfer zusammenführen | vorausgewählt, Übernahme nur nach Vorschau und Bestätigung |
| C – mehrdeutig/widersprüchlich | z. B. gleiche Mengen ohne Kennung, konkurrierende Deutungen | Alternativen mit Auswirkungen, keine Sammelausführung |

Hohe Konfidenz allein löst nie eine wirtschaftlich wirksame Änderung aus. Vollständig gleiche Buchungen ohne
unterscheidende Kennung erscheinen als Verdacht (zwei gleiche Ausführungen in derselben Sekunde sind möglich); welche
Buchung bliebe, hängt nur von der Kennung ab, nicht von der Reihenfolge in der Datei.

## Diagnose: Datenqualität und Bestandsabgleich

*Datenqualität → Diagnose öffnen* (`/quality/diagnose`) prüft alle Buchungen, Bestände, Zuordnungen und Kurse und
zeigt priorisierte Befunde. **Die Diagnose selbst ändert nichts:** Sie wird bei jedem Aufruf neu aus den Daten
berechnet („Erneut prüfen“ lädt nur die Seite neu); gleiche Daten ergeben dieselben Befunde mit denselben
Kennungen. Korrekturen gibt es nur auf ausdrücklichen Wunsch je Befund – nach einer Vorschau mit berechneten
Auswirkungen und jederzeit umkehrbar (siehe [Empfehlungen und Korrekturen](#empfehlungen-und-korrekturen)). Auch bei
starkem Verdacht bereinigt Portfolia nichts automatisch.

Jeder Befund nennt betroffene Buchungen, Konten, Quellen und Kennungen und trennt:

* **Empfehlung** – was Portfolia rät, was vorher zu prüfen ist (mit Explorer-Links) und welche Lösungen zur Wahl
  stehen: die empfohlene, Alternativen, eine eigene Auswahl und „als geprüft markieren“.
* **Was Portfolia aus den Daten weiß** – Fakten aus Buchungen, Kennzeichen, Beständen.
* **Was Portfolia nur vermutet** – die Deutung.
* **Belege** und **Unsicherheiten** – was für und was gegen die Deutung spricht.
* **Szenario (hypothetisch)** – rechnerische Auswirkung, deutlich markiert, nie gebucht (z. B. Bestand ohne die
  vermutete Doppelbuchung, Bewertung zum Kaufkurs).

**Status eines Befunds**

| Status | Bedeutung |
|---|---|
| belegt | folgt unmittelbar aus den Daten (Kennzeichen „rekonstruiert“, fehlender Kurs, Differenz zur Börse, Kursquelle eines anderen Coins) |
| wahrscheinlich | mehrere unabhängige Belege, keine legitime Erklärung in den Daten erkennbar |
| verdacht | Muster passt, aber ein entscheidender Beleg fehlt oder eine legitime Erklärung ist möglich |
| hinweis | zur Einordnung – kein Fehler festgestellt |

**Befundarten und Regeln**

| Art | Erkennung |
|---|---|
| Wahrscheinliche Dublette | gleicher Transaktions-Hash und identische Angaben (Zeit, Konto, Richtung, Menge, EUR) – mit gleichem Ereignisindex *wahrscheinlich*, ohne Index *verdacht*, mit verschiedenen Indizes legitim (kein Befund); exakt gleiche Menge auf demselben Konto in ≤ 36 h, mindestens eine Buchung manuell (*wahrscheinlich*, wenn die Menge unverwechselbar ist und die andere Buchung einen Hash hat); gleiche Anbieter-Kennung (z. B. Bitpanda-UUID) in Import und App-Buchung; gleicher Hash und gleiche Menge in Import und App-Buchung |
| Möglicher interner Transfer | Abgang und Zugang desselben Assets auf verschiedenen eigenen Konten ohne Verknüpfung, Zugang −2 h … +72 h, 90–100,1 % der Menge oder gleicher Hash – beide Seiten nebeneinander mit Begründung (Zeit, Menge, Gebühr, Hash, eigene Adresse) |
| Falsche oder mehrdeutige Asset-Zuordnung | Anbieter-Kürzel mit Kursquelle eines anderen Coins (Bitpanda „TH“), mehrere Token-Contracts für ein Asset, dieselbe Kursquelle für mehrere Assets, Kurszuordnungen nur über das Symbol |
| Bestand: beobachtet ≠ berechnet | Differenz zur Börse bzw. Blockchain (aktueller, vollständiger Abruf) oder zum Soll des kuratierten Imports |
| Unvollständige Transaktionshistorie | negativer Bestand, Abgang ohne Anschaffung, Transfer mit Mehrempfang, Koinly-Kennzeichen (z. B. `KOINLY_NEG_BALANCE`), Lücken einer Datenquelle, offene Prüf-Stapel, Anfangsbestände ohne Einzelbelege |
| Rekonstruiert oder geschätzt | Quelle `reconstructed` bzw. Kennzeichen `RECONSTRUCTED_*`/`AVG_PRICE` (Ausgleichsbuchungen, rekonstruierte Sparpläne, Lücken), Sparplan-Schätzungen, Buchungen ohne EUR-Kurs – mit betroffenen Lots, Veräußerungen je Jahr, Haltefrist und Performance |
| Fehlender oder veralteter Kurs | gehaltene Positionen ohne gültigen Kurs (mit Kursquelle und Alter des letzten Kurspunkts), Bewertung mit manuellem bzw. Transaktionskurs (kein Marktkurs), veraltete Marktkurse |
| Möglicher Token-Migrationsvorgang | gleiches Symbol auf demselben Konto, Mengenverhältnis 10^3/10^6/10^9/10^12/10^18 : 1 (± 0,01 %), Spam-Markierung und Contract-Adressen als Belege |
| Wirtschaftliche Dublette über Quellen (M27) | Zu- bzw. Abgang auf demselben Konto, gleiche Asset-ID (gleichnamige Tokens anderer Netzwerke sind andere Assets), gleiche Richtung, aus zwei Quellen (z. B. Koinly-Import und Börsen-API); Betrag brutto/netto gleich – eine Gebühr im selben Asset erklärt die Differenz; deterministische 1:1-Zuordnung. ≤ 1 h und je Buchung genau ein Partner → *wahrscheinlich*; ≤ 36 h → *verdacht*; regelmäßiger Tagesversatz bei gleicher Uhrzeit (≥ 3 Paare, z. B. Zahlungs- vs. Ausführungsdatum eines Sparplans) → *verdacht*; sonst bis 7 Tage → *hinweis* („ohne ausreichenden Beleg“, keine empfohlene Korrektur). Verschiedene Hashes bzw. Anbieter-Kennungen schließen eine Zuordnung aus |
| Bestand nach Quellen (M27) | Konten, die im selben Zeitraum aus mehreren Quellen gebucht werden: Saldo, Anzahl und Zeitraum je Quelle, nur in einer Quelle vorhandene Ein-/Auszahlungen, Szenario „nur Quelle X“ bzw. „ohne Doppelbuchungen“ – zerlegt eine Abweichung je Konto statt einen Gesamtwert als falsch zu melden |
| Negativer Bestand (erweitert, M27) | Zwischenstand oder aktuelle Inkonsistenz, Beginn der aktuellen negativen Phase, mögliche Ursachen: doppelte Auszahlung aus zwei Quellen (mit Rechnung, ob sie den Fehlbestand vollständig erklärt), Sparplan-Ausführung ohne Finanzierung nach dem Ende der Quellhistorie, Gebühr, fehlender Eingang eines Eigenübertrags |
| Möglicher interner Transfer (erweitert, M27) | Abgänge ohne Gegenbuchung mit möglichen Zielen und Sicherheitsbewertung in % (Hash, Menge, Zeit bis 14 Tage, Netzwerk, unverwechselbare Menge, Tokenwechsel über `related_asset` bzw. EUR-Wert für Bridge/Wrapped/Cross-Chain); bei mehreren ähnlich guten Zielen keine Verknüpfung vorgeschlagen |
| Ungeklärter Vermögensabgang / möglicher Verlust (M27) | Einordnung **A** technischer Buchungsfehler (Wert wahrscheinlich vorhanden), **B** ungeklärter Abgang (Informationen fehlen), **C** nachgewiesener Verlust (Verlust-Buchung, als kompromittiert gekennzeichnetes Konto mit Datum). Abgänge an Dritte ohne Gegenbuchung ab 50 € (*verdacht* ab 500 €), Bestand fehlt bei aktueller, vollständiger Wallet-Prüfung. Nie aus Inaktivität, Kursverfall oder einem veralteten bzw. fehlerhaften Abruf |
| Inaktives Konto mit Restbestand (M27) | letzte Buchung > 365 Tage, Restbestände mit Wert, letzte erfolgreiche Synchronisation und Verbindungszustand getrennt ausgewiesen; Einordnung (ohne Datenquelle, nie bzw. nicht mehr synchronisiert, Fehler, Kurs fehlt, Migration, kompromittiert) – kein Verlust |

**Bestandsabgleich je Konto und Asset:** berechnet (Buchungen) neben beobachtet (Börse/Blockchain mit Zeitpunkt und
Anbieter), Soll laut kuratiertem Import und Differenz, mit möglichen Erklärungen (offene Prüf-Stapel,
unvollständige Historie, Gebühren, Dublettenverdacht, nicht verknüpfte Transfers).

| Status | Bedeutung |
|---|---|
| mit externer Quelle abgestimmt | Börse bzw. Blockchain meldet denselben Bestand; Abruf ≤ 48 h alt und ohne erkannte Lücke |
| Differenz zur externen Quelle | aktueller, vollständiger Abruf meldet einen anderen Bestand |
| extern nicht bestätigt | externer Bestand liegt vor, aber veraltet oder unvollständig – kein „stimmt“, auch bei gleichem Wert |
| intern konsistent | aus den Buchungen reproduzierbar (= Soll des kuratierten Imports) – **nicht** extern geprüft |
| Import-Soll + Änderungen in Portfolia | die Import-Buchungen ergeben das Soll; die Abweichung stammt vollständig aus Änderungen in Portfolia (ausgeblendete, geänderte oder ergänzte Buchungen, Sparplan-Schätzungen) – z. B. nach einer übernommenen Korrektur; kein Befund |
| intern abweichend | Buchungen ergeben einen anderen Bestand als das Soll |
| mit Referenzbestand abgestimmt | vom Nutzer hinterlegter Kontostand (z. B. Kontoauszug) = Soll aus den Buchungen zum selben Stichtag |
| Differenz zum Referenzbestand | Soll zum Stichtag weicht vom hinterlegten Kontostand ab – Befund mit Zerlegung nach Quellen und möglichen Ursachen, ohne Ausgleichsbuchung |
| ohne Abgleich | weder externer Bestand noch Soll noch Referenzbestand vorhanden – ein fehlender Ist-Bestand gilt als unbekannt, nicht als 0 |

**Soll-Ist zum selben Stichtag (M27):** Ein externer Bestand wird mit dem Soll *zum Abrufzeitpunkt* verglichen
(Buchungen danach zählen nicht), ein Referenzbestand mit dem Soll bis Ende des Stichtags (Ortszeit). Rechnung in
`Decimal`, ohne Rundung. Ein Soll aus einem Portfolia-Gesamtexport (`holdings_check.csv` mit Notiz „Export“) ist von
Portfolia selbst berechnet und gilt nicht als unabhängige Referenz („ohne Abgleich“ mit Hinweis).

**Bereiche (M27):** Die Diagnose bündelt *Bestandsabweichungen* (Tabelle mit Konto/Plattform, Asset-Identität, Soll,
Ist, Differenz, Stichtag, Datenquelle/Qualität, letzter Synchronisation, Ursache, Zustand und Sicherheit; Filter
nach Börse/Wallet, Konto, Asset, Abweichung in € und Sicherheit), *Mögliche Doppelbuchungen*, *Ungeklärte
Transfers*, *Inaktive Konten* und *Potenzielle Verluste*.

**Referenzbestände:** *Diagnose → Referenzbestände* nimmt Konto, Asset-ID, Bestand (0 ist gültig), Stichtag und Beleg
auf. Ein Referenzbestand ist ein **Prüfwert, keine Buchung** – er ändert weder Bestand noch Einstand noch Steuer.
Anlegen und Entfernen stehen im Änderungsprotokoll; entfernte Einträge bleiben als „entfernt“ erhalten. Referenzbestände
reisen mit dem Gesamtexport (`state.json` → `reference_balances`). Datenbank: Migration 19 (neue Tabelle, nicht
destruktiv).

**Wirtschaftliche Dublette verknüpfen:** Je ausgewähltem Paar zählt eine Buchung (Vorgabe: der kuratierte Import). Die
andere bleibt mit Herkunft erhalten: eine App-Buchung wird als „im Import enthalten“ verknüpft, eine Import-Buchung als
Doppelbuchung der geltenden Buchung ausgeblendet (Überlagerung, Protokoll mit `duplicate_of`). Vorschau, Übernehmen
und Rückgängig wie bei allen Korrekturen; eine freie Löschung („eigene Auswahl“) wird für diese Befunde nicht
angeboten.

„Intern konsistent“ trotz Dublettenverdacht ist kein Widerspruch: Der Soll-Bestand wurde aus denselben Buchungen
berechnet. Portfolia speichert oder ersetzt dabei keine Bestände.

**Grenzen der Erkennung**

* Ohne Ereignisindex (Output-/Log-Index) sind zwei gleiche Bewegungen in derselben Blockchain-Transaktion nicht von
  einer Doppelbuchung zu unterscheiden; Steuertool-Exporte (z. B. Koinly) liefern keinen Index.
* Gleiche Menge und Zeit beweisen keine Dublette; zwei verschiedene Hashes gelten immer als zwei Vorgänge.
* Transfers zwischen eigenen Konten werden nur vermutet; Adressen der Gegenseite fehlen meist. Abgänge an Dritte mit
  zufällig ähnlichem Zugang sind möglich.
* Die Anbieter-Identität von Kürzeln kennt Portfolia nur für hinterlegte Fälle (`app/csvimport/identity.py`, derzeit
  Bitpanda „TH“); andere Symbolkonflikte erscheinen als mehrdeutig bzw. als Kurszuordnung „nur über das Symbol“.
* Migrationen sind ohne Contract-Adressen und Projektangaben nicht belegbar und bleiben *verdacht*.
* Kurse fragt die Diagnose nicht ab; Szenarien nutzen nur gespeicherte Kurse bzw. Kaufkurse und sind keine
  Marktbewertung. Veräußerungen werden über die anschaffende Buchung der Lots zugeordnet.
* Ein externer Bestand liegt nur für Wallets vor (Bestandsmeldung der Anbieter); Börsenkonten bleiben „intern
  konsistent“ bzw. „ohne Abgleich“.

### Empfehlungen und Korrekturen

Ein Befund lässt sich aufklappen und direkt bearbeiten: Die **Empfehlung** sagt, was Portfolia rät und was vorher zu
prüfen ist; darunter stehen die Lösungen. Jede Lösung führt zuerst in eine **Vorschau**, erst „Übernehmen“ ändert
Daten.

**Grundsätze**

* **Nie automatisch.** Eine Korrektur gilt genau einem Befund und genau der gewählten Lösung; nichts wird im
  Hintergrund oder für mehrere Befunde auf einmal geändert.
* **Alles sichtbar.** Die Vorschau zeigt jede Änderung mit der vollständigen Buchung (ausgeblendet, zusammengeführt,
  „im Import enthalten“, neu angelegt, Kursquelle, Zuordnung) und rechnet die Folgen auf einer Kopie im Speicher
  durch: Bestand, Einstand offener Lots und Wert je Konto/Asset, Bestandsabgleich vorher/nachher, realisierte
  Ergebnisse und Erträge je Jahr, die Zusammenfassung des **Steuerberichts** je betroffenem Jahr (mit dem Regelwerk
  und seinen Optionen), neue bzw. entfallende Ledger-Hinweise und welche Befunde danach erledigt, neu oder geändert
  sind. Die Vorschau schreibt nichts.
* **Nichts wird gelöscht.** Korrekturen nutzen die vorhandenen, umkehrbaren Mechanismen: Import-Buchung ausblenden =
  Überlagerung „gelöscht“ (die Import-Datei bleibt unverändert, ein späterer Import hebt das nicht auf),
  App-Buchung = Status „gelöscht“ bzw. „zusammengeführt“, „im Import enthalten“ = Entscheidung wie unter
  *Journal → Abgleich*, Kursquelle = Zuordnung unter *Kursquellen*, neue Buchungen = App-Buchungen der Quelle
  „Korrektur aus der Diagnose“ (`PF-D-…`).
* **Atomar und aktuell.** „Übernehmen“ berechnet Befund und Lösung neu und vergleicht sie über eine Prüfsumme mit der
  Vorschau (Änderungen, Ausgangszustand jedes betroffenen Objekts, Datenstand). Hat sich seitdem etwas geändert –
  z. B. durch einen Abruf –, wird nichts übernommen. Alle Änderungen einer Korrektur laufen in einer Transaktion.
* **Rückgängig.** *Entscheidungen und Korrekturen* listet jede Korrektur mit allen Änderungen; „Rückgängig“ nimmt sie
  als Ganzes zurück (Vorher-Zustand je Änderung ist gespeichert). Wurde ein betroffenes Objekt danach anderweitig
  geändert (z. B. Kursquelle neu gesetzt), verweigert Portfolia das Zurücksetzen, statt die spätere Entscheidung zu
  überschreiben; bereits selbst Wiederhergestelltes wird übersprungen.

**Lösungen je Befund**

| Befund | Empfehlung | Alternativen |
|---|---|---|
| Gleiche Menge zweimal gebucht (manuell + belegt) | schwächer belegte Buchung ausblenden – bei *wahrscheinlich* direkt, bei *verdacht* erst nach Prüfung (Explorer-Link zum belegten Vorgang) | stattdessen die andere ausblenden · eigene Auswahl · als geprüft markieren |
| Paare mit gleichem Hash | je Paar die zweite Buchung ausblenden, Auswahl je Paar (App-Buchung neben Import-Buchung: „im Import enthalten“) – nur Paare, die im Explorer eine Bewegung zeigen | eigene Auswahl · als geprüft markieren (zwei legitime Bewegungen) |
| Vorgang im Import und als App-Buchung | App-Buchung als „im Import enthalten“ markieren (bleibt erhalten, zählt wieder, falls ein späterer Import den Vorgang nicht mehr enthält) | Import-Buchung ausblenden (App-Buchung gilt) · als geprüft markieren |
| Möglicher interner Transfer | als Transfer verbuchen: je Paar eine Transfer-Buchung, Abgang und Zugang zählen nicht mehr einzeln; Einstand und Anschaffungsdatum wandern mit, Mengendifferenz = Transfergebühr; Auswahl je Paar | eigene Auswahl · als geprüft markieren (Vorgang mit Dritten) |
| Anbieter-Kürzel mit Kursquelle eines anderen Coins | Kursquelle auf den Anbieter-Coin setzen (ersetzt auch eine Kursquelle des Imports) | andere CoinGecko-ID · als geprüft markieren |
| Kein gültiger Kurs / Ersatzkurs | Vorschlag der Kurssuche übernehmen (bei Sicherheit hoch/mittel) | eigene CoinGecko-ID · als Verlust ausbuchen (eigene Seite) · als geprüft markieren |
| Mehrere Contracts je Asset | nach Explorer-Prüfung falsche Token-Zuordnung entfernen (wirkt auf künftige Importe/Abrufe) | als geprüft markieren |
| Möglicher Token-Migrationsvorgang | keine – erst Contract-Adressen prüfen | Migration buchen (Kapitalmaßnahme alt → neu, Zugang des neuen Tokens entfällt) · Zugang ausblenden (Spam) · als geprüft markieren |
| Bestand ≠ extern bzw. Soll | Ursache klären (Prüf-Stapel, fehlende Vorgänge, Gebühren) | Ausgleichsbuchung als Notlösung (Zu- bzw. Abgang der Differenz, Art, EUR-Wert und Datum wählbar) · als geprüft markieren |
| übrige (Historie, Schätzungen, veraltete Kurse) | Hinweis, wo die Ursache zu beheben ist | eigene Auswahl auszublendender Buchungen (wo Buchungen betroffen sind) · als geprüft markieren |

Eine **eigene Lösung** ist immer möglich: „Eigene Auswahl: Buchungen ausblenden“ (mit Vorschau), „bearbeiten“ an
jeder betroffenen Buchung (Journal; Import-Buchungen als Überlagerung) oder „als geprüft markieren“ mit Notiz.

**Als geprüft markieren** ändert keine Daten: Der Befund wandert in *Als geprüft markiert* und gilt dort, solange
seine Daten gleich bleiben (Prüfsumme; eine neue Abrufzeit allein zählt nicht). Ändern sich Mengen, Buchungen oder
Status, erscheint er wieder mit dem Hinweis „seitdem geändert“. „Wieder öffnen“ hebt die Markierung auf. Der
Gesamtexport nimmt geprüfte Befunde mit.

**Grenzen**

* Die Vorschau fragt keine Kurse ab: Nach einer neuen Kursquelle stehen Wert und Kursbefunde erst nach dem
  Kursabruf fest.
* Die Steuerwerte der Vorschau sind die Zusammenfassung des Steuerberichts mit den aktuellen Optionen – vorläufig;
  maßgeblich bleibt der Bericht nach dem Übernehmen. Die Portfolia-Ansicht (realisierte Ergebnisse) nutzt die
  eingestellte Verbrauchsfolge und kann deshalb von den Steuerwerten (je Wallet, Regelwerk) abweichen.
* Übernommen wird nur, was die Diagnose belegen kann; die Prüfung im Explorer bzw. beim Anbieter ersetzt sie nicht.
  Eine Ausgleichsbuchung macht Mengen passend, erklärt aber keine Differenz.
* Korrekturen aus der Diagnose lassen sich nur dort zurücknehmen (im Journal ohne Bearbeiten/Löschen); eine
  Teilbuchung einer Gruppe, ein abgeglichener Transfer oder eine Sparplan-Buchung wird nicht über die Diagnose
  geändert.

---

## Berechnungen

* **Bestände** je Asset und Konto aus dem Ledger (inkl. Gebühren); `holdings_check` dient nur dem Abgleich.
* **Lots/FIFO:** global je Asset (Kontozuordnung wird bei Transfers konsistent gehalten) oder je Konto
  (Einstellung). Erträge (Staking etc.) werden mit dem Marktwert bei Zufluss als Lot angelegt.
  Buchungen mit identischem Zeitpunkt werden unabhängig von ihrer Reihenfolge in der Datei verarbeitet (zuerst die,
  deren Abgang durch den Bestand gedeckt ist). Kommt bei einem internen Transfer mehr an als abging (nur bei
  Altbuchungen möglich; Import und Journal lehnen das ab), wird die Differenz ohne Anschaffung geführt und als Befund
  gemeldet. Lots ohne nachgewiesene Anschaffung gelten steuerlich nie als steuerfrei.
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
* **Bewertungslücken** (ab 0.21.0): Hat eine Position an einzelnen Tagen gar keinen Kurs (auch keinen Ersatzkurs),
  zählt ihr Wegfall nicht als Verlust: TTWROR, IRR, Gewinn, Index und Drawdown rechnen sie in diesen Tagen wie eine
  Aus- bzw. Einbuchung zum zuletzt bekannten Wert heraus (ab 0.21.1). Was sich gegenüber diesem Wert ändert – Kurs
  nach der Lücke, Verkauf oder Ausbuchung in der Lücke –, ist echte Wertänderung und bleibt sichtbar; Zu- und Abgänge
  ohne EUR-Betrag werden in Gesamt- und Positionssicht mit demselben Tageskurs bewertet (kein Scheingewinn, z. B. bei
  einem Token-Zugang vor dem ersten Marktkurs). Die Performance-Seite zeigt den
  Bewertungszustand des Zeitraums („vollständig“, „teilweise geschätzt“, „unvollständig“) und den Hinweis
  „ohne Bewertungslücken“. Früher fiel die Kennzahl in solchen Fällen auf bis zu −100 %.
* **Mehrdeutige IRR:** Wechseln die Zahlungsströme mehrfach das Vorzeichen, sucht Portfolia alle Lösungen zwischen
  −99,99 % und 10⁸ % p. a. mit Nachweis (Substitution x = ln(1 + r), Intervallschranken auf den Ableitungen, Descartes-
  Schranke; ab 0.21.1, vorher Raster). Gibt es mehrere oder lassen sie sich numerisch nicht sicher trennen (z. B.
  doppelte Nullstelle), zeigt Portfolia keinen IRR-Wert, sondern „nicht eindeutig bestimmbar“ (TTWROR bleibt
  maßgeblich).

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
| KaspaCom-Marktplatz (api.kaspa.com) | KRC-20-Tokens ohne CoinGecko-Eintrag (z. B. BRUCE, KASPER, POPKAT) | ohne Key; höchstens alle 30 min; siehe unten |
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

**KRC-20-Kurse (Kaspa-Tokens):** Viele kleine KRC-20-Tokens führt CoinGecko nicht. Portfolia ordnet Tokens aus der
Kaspa-Anbindung (`TICK@KAS:TICK`) ohne CoinGecko-Contract automatisch der Kursquelle **KaspaCom** zu
(`/api/token-info/<TICK>`, ohne Key) – über das Kürzel, das auf Kaspa eindeutig ist, nie über ein Symbol anderer
Chains. Die Doku nennt das Feld `price` „USD“, live ist es **KAS je Token** (`marketCap / (price × totalMinted)` ergibt
bei allen geprüften Tokens den KAS/USD-Kurs). Portfolia prüft deshalb jede Antwort: Passt der Faktor zum aktuellen
KAS/USD-Kurs von CoinGecko (±35 %), wird `price` als KAS gelesen und mit KAS/EUR umgerechnet; liegt er bei ≈ 1, als
USD; sonst wird kein Kurs übernommen. Grenzen: Marktplatzkurs eines Anbieters mit oft geringen Umsätzen; keine
Kurshistorie (Tagesschlüsse ab der Zuordnung, davor Transaktions-/Ersatzkurse); Tokens, die KaspaCom nicht kennt
(z. B. KEI, HTTP 500), bleiben ohne Kurs. Eine eigene CoinGecko-ID unter *Datenqualität → Kursquellen* ersetzt die
Zuordnung. Im Import-ZIP des Exports steht die Quelle als `none` (Datenvertrag), im App-Zustand bleibt sie erhalten.

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
* Stammt das Asset aus einer Wallet-Anbindung (Token-Zuordnung mit Contract), entscheidet der Contract im Katalog –
  eindeutig, Sicherheit „hoch“, ohne Marktdaten-Abruf.
* **Anbieter-Kürzel** (z. B. Bitpanda „TH“ = Threshold Network): nur auf Konten dieses Anbieters gebucht → dessen
  Coin („hoch“); auch auf anderen Konten → nur Vorschlag („niedrig“). Nie über das Symbol – dort hieße „TH“ der Team
  Heretics Fan Token. Bestehende Zuordnungen prüft die Suche nicht erneut; eine falsche zeigt die
  [Diagnose](#diagnose-datenqualität-und-bestandsabgleich) mit ihrer Wirkung auf die Bewertung und bietet die
  Korrektur an – auch wenn die falsche Kursquelle aus dem kuratierten Import stammt (die Zuordnung „ersetzt die
  Kursquelle des Imports“, „Zuordnung entfernen“ stellt sie wieder her).
* Sonst Kandidaten mit gleichem Symbol; die Konten liefern die Chain („MetaMask (BNB)“ → BNB Smart Chain, „Kaspa (KAS)“ →
  Kaspa, Börsenkonten keine). Coins nur auf anderen Chains entfallen, ebenso Coins, deren Kursspanne (Allzeittief ÷ 3 bis
  Allzeithoch × 3) die eigenen Transaktionskurse nicht enthält – z. B. LUNA zu Kursen von LUNA Classic.
* Sicherheit „hoch“ (genau ein passender Coin, Chain und Kurse passen) wird automatisch übernommen, „mittel“/„niedrig“
  erscheinen als Vorschlag mit *Übernehmen*/*Ablehnen*; jede ID lässt sich auch per Eingabe oder Link von coingecko.com
  setzen. Stufe unter *Einstellungen → Kurse* („nur eindeutige“, „auch wahrscheinliche“, „nie“). Spam-Token (Status
  `spam`) werden übersprungen.
* Zuordnungen gelten über dem Import, fließen in den Gesamtexport (`assets.csv`) ein und lassen sich jederzeit
  zurücknehmen; danach werden Kurse und Historie neu geladen.

**Kursqualität der Historie** (*Datenqualität → Kursqualität der Historie*, Badge im Positionsdetail): Jeder
gehaltene Tag trägt die Herkunft seines Kurses – *Marktkurs*, *alternativer Kursanbieter*, *letzter Kurs
fortgeschrieben (interpoliert)*, *Transaktionskurs/manueller Kurs als Schätzung*, *erster Marktkurs rückwirkend*,
*kein Kurs*. Zusammengefasst je Asset: „✓ Marktdaten vollständig“, „⚠ Historische Kursdaten teilweise geschätzt“,
„⚠ 24 Tage ohne Marktdaten“, „✕ Historie konnte nicht geladen werden“; gespeichert je Abschnitt mit Asset,
Zeitraum, Tagen, Ersatzmethode und Datenquelle (Tabelle `price_gap`).

* **Ursache geschätzter Historie:** Die CoinGecko-Demo-API liefert nur 365 Tage. Davor fehlten Marktkurse; die Lücke
  wurde bisher mit Transaktionskursen oder dem ersten Marktkurs gefüllt.
* **Behebung:** Für Krypto ohne ausdrückliche Zuordnung sucht Portfolia automatisch ein Yahoo-Paar
  (`SYMBOL-EUR`, sonst `SYMBOL-USD` mit EZB-Umrechnung) – übernommen nur nach bestandener Identitätsprüfung:
  im Überlappungszeitraum Median-Abweichung zu CoinGecko ≤ 6 % und ≥ 60 % der Tage innerhalb ± 10 %; ohne
  Überlappung gegen die eigenen Transaktionskurse (≥ 5 Punkte, Median ≤ 15 %). Abgelehnte Kandidaten erscheinen mit
  Grund („keine Daten“, „weicht ab“, „zu wenige Vergleichswerte“) und werden nicht verwendet. Ausschalten unter
  *Einstellungen → Kurse* (`prices.crypto_history_auto`).
* Grenze: Yahoo ist eine inoffizielle Schnittstelle; Symbole sind nicht eindeutig (gleiches Kürzel, anderer Coin) –
  deshalb die Prüfung. Coins ohne Yahoo-Paar bleiben geschätzt und sind als solche markiert.

**Gemeinsame Marktdaten:** Dashboard, Positionsdetail und Watchlist lesen Kurse, 24 h/7 Tage, Marktkapitalisierung
und Sparklines aus derselben Ablage (`app/prices/market.py`); Watchlist-Coins laufen im gebündelten CoinGecko-Abruf
mit, Marktkapitalisierung/7 Tage/Sparkline werden höchstens alle 15 Minuten nachgeladen.

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

### Steuerdaten je Jahr (externe Steuerberichte)

*Steuern & Haltefristen → Steuerdaten je Jahr* (`/tax/data`) verwaltet Steuerberichte aus anderen Werkzeugen bzw.
eigene Aufstellungen je Steuerjahr – **ohne** eigene Steuerberechnung und **ohne** Buchungen anzulegen oder zu ändern.

* **Wege:** Ordner `/data/tax` (geprüft beim Start, alle 15 Minuten – abschaltbar – und mit „Steuerdateien prüfen“;
  kein dauerhafter Dateiwächter) oder Upload im Browser. Beide laufen durch dieselbe Pipeline: Format erkennen →
  Parser → Steuerjahr bestimmen → prüfen → Vorschau („1.245 Datensätze erkannt“) → übernehmen.
* **Formate:** JSON bevorzugt (Liste oder `{"taxYear": 2025, "records": [...]}`), CSV als Austauschformat
  (Trennzeichen/Kodierung automatisch, Dezimalkomma, deutsche Spaltennamen). Felder: `taxYear, transactionId,
  externalId, asset, quantity, acquisitionDate, disposalDate, acquisitionCost, disposalValue, holdingPeriod,
  taxable, gainLoss, taxCategory, source, comment`. Unlesbare Werte werden als Warnung je Zeile gemeldet, nie geraten.
  Anbieterformate (Blockpit, Koinly, CoinTracking) lassen sich als weitere Parser ergänzen.
* **Steuerjahr:** aus dem Dateikopf, sonst aus den Datensätzen (`taxYear` bzw. Veräußerungsdatum), sonst aus dem
  Dateinamen. Mehrdeutig (z. B. Datensätze aus 2024 und 2025) → der Nutzer wählt.
* **Genau eine aktive Datei je Jahr** (von der Datenbank erzwungen). Ein neues Jahr aus dem Ordner wird direkt
  übernommen und als „Neue Steuerdatei erkannt: Steuerjahr 2026“ gemeldet; Uploads werden erst nach der Vorschau
  aktiv. Gibt es für das Jahr schon Daten, wartet die neue Datei: „Für 2025 existieren bereits Steuerdaten.“ mit
  Gegenüberstellung (Datensätze, Gewinn/Verlust, hinzugekommen/entfallen/geändert) und *Aktualisieren / Ersetzen* bzw.
  *Abbrechen*. Nie stilles Überschreiben; ersetzte und entfernte Fassungen bleiben mit allen Datensätzen im Verlauf.
* **Idempotent:** unveränderte Dateien (Größe, Änderungszeit) werden nicht erneut gelesen, gleicher Inhalt
  (SHA-256) wird nie doppelt übernommen; Dateien, die gerade geschrieben werden (< 5 s alt), folgen im nächsten Lauf.
* **Zuordnung zu Buchungen** (nur Verweis): externe ID → Portfolia-Buchungs-ID → Asset + Datum + Menge (+ Betrag
  ± 1 %) → sonst „nicht zugeordnet“; mehrere Kandidaten → „Konflikt“. Je Jahr: Anzahl, zugeordnet, nicht zugeordnet,
  Konflikte, Warnungen; Export als CSV, *Zuordnung neu prüfen*, Entfernen nur nach Bestätigung.

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
* **ZIP-Sicherungen im Import-Format:** Nach jeder Änderung an Buchungen oder Einstellungen (manuell, CSV-Import,
  Sparplan-Freigabe, neuer Import, Assets, Kursquellen) schreibt Portfolia – gebündelt nach zwei Minuten ohne weitere
  Änderung – eine datierte Datei `portfolia-export-JJJJ-MM-TT_HHMMSS.zip` nach `EXPORT_DIR` (Standard
  `/data/exports`), nur wenn sich der Inhalt geändert hat. Jede Datei ist ein vollständiger Gesamtexport (siehe
  [Neu einrichten](#neu-einrichten-und-umziehen)) und zugleich ein gültiger kuratierter Import; Journal-Buchungen
  werden über ihre `tx_id` erkannt (keine Doppelzählung). Jede erfolgreich importierte ZIP-Datei wird zusätzlich mit
  Datum unter `EXPORT_DIR/import-archiv/` abgelegt. Aufbewahrung (Standard 30 Sicherungen, 20 Import-Kopien),
  manuelles Sichern und Download unter *Einstellungen → ZIP-Sicherungen*. Empfehlung: `EXPORT_DIR` auf eine
  Freigabe mit eigener Sicherung legen.

### Neu einrichten und umziehen

Der Gesamtexport enthält alles, was eine neue Installation für denselben Stand braucht:

| Teil | Inhalt |
|---|---|
| Datenvertrag (`transactions.csv` …) | alle Buchungen in ihrer wirksamen Fassung (inkl. Änderungen/Löschungen an Import-Buchungen), Assets mit Kursquellen, Konten mit Steuereinstellung, aktueller Bestand, manuelle Kurse |
| `portfolia/state.json` | alle Einstellungen, Kursquellen-Zuordnungen samt Status (auch abgelehnte Vorschläge), Sparplan-Wahl und verworfene Ausführungen, Datenquellen (**ohne** API-Keys), „dauerhaft ignoriert“, Anbieter-IDs, CSV-Zuordnungen (Symbole, Konten, eigene Formate), Kennungen gelöschter CSV-/Sync-Buchungen, als „geprüft“ markierte Befunde der Diagnose (übernommene Korrekturen stecken bereits in Buchungen und Zuordnungen) |
| `portfolia/price_daily.csv`, `series_meta.csv` | Kurshistorie – wichtig, weil die CoinGecko-Demo-API nur 365 Tage nachliefert |
| `portfolia/usage.json` | API-Verbrauch des Monats (das CoinGecko-Kontingent läuft weiter) |
| `portfolia/taxdata.json` | Steuerdaten je Jahr: Dateien mit Status, Datensätzen, Zuordnungen und Ersetzungen (ab 0.21.0); Originaldateien unter `files/tax/` |
| `portfolia/files/` | `sources.yaml` (News-Quellen), lokale Steuerregeln (`tax_rules/`), Original-Steuerberichte |

`state.json` enthält außerdem Watchlists und die Entscheidungen zu alternativen Kursreihen (ab 0.21.0); ältere
Exporte ohne diese Teile bleiben übernehmbar.

Nie enthalten sind API-Keys, Master-Key, Passwörter und Protokolle. Andere Werkzeuge ignorieren den Ordner
`portfolia/`; die Prüfsummen stehen im Manifest unter `extra_files`.

**Ablauf:** Export-ZIP (Download unter *Einstellungen → ZIP-Sicherungen* bzw. *Buchungen → Gesamtexport*, oder eine
Datei aus `EXPORT_DIR`) in den **Importordner** der neuen Installation legen. Auf einer neuen Installation (noch
keine Buchungen, Einstellungen oder Datenquellen) übernimmt Portfolia die Zusatzdaten automatisch, sonst erscheint in
Übersicht und Einstellungen die Rückfrage „Übernehmen / Nicht übernehmen“. Übernehmen löscht nichts: Einstellungen
werden überschrieben, Zuordnungen, Entscheidungen und Kurshistorie ergänzt, gleichnamige Datenquellen übersprungen,
ersetzte Dateien als `.bak-…` gesichert. Ablauf (ab 0.21.1): Zusatzdaten prüfen (Struktur, Widersprüche wie zwei
aktive Steuerdateien für ein Jahr) → Dateien temporär vorbereiten (Speicherplatz, Rechte) → alle Datenbankänderungen
**und** ein Wiederherstellungs-Journal in einer Transaktion → Dateien ersetzen und per Prüfsumme bestätigen. Scheitert
ein Schritt vor dem Datenbank-Commit, bleibt alles unverändert. Bricht das Ersetzen der Dateien ab (Fehler,
Prozess- oder Container-Neustart), steht die Wiederherstellung als „unvollständig“ in Übersicht und Einstellungen und
wird beim nächsten Start bzw. per „Jetzt fortsetzen“ zu Ende geführt (fehlende temporäre Dateien werden aus den
gespeicherten Zusatzdaten neu erzeugt; verwaiste temporäre Dateien beim Start entfernt). Eine zweite Übernahme derselben
Datei ergänzt nichts doppelt. Steuerdaten, deren Jahr bereits aktive Daten hat, warten zur Entscheidung. Datenquellen
ohne API-Key zeigen „Kein API-Key hinterlegt“, bis er eingegeben ist. Danach nur noch API-Keys der Datenquellen neu eingeben (und ggf. den
[Master-Key](#master-key-für-api-keys) einrichten). Ein erneuter CSV-Import oder Abgleich einer Datenquelle erkennt
die übernommenen Buchungen an ihrer Kennung bzw. Anbieter-ID („bereits vorhanden“).

Für eine byte-genaue Wiederherstellung derselben Installation (inkl. CSV-Stapel, Laufhistorie, News) bleibt die
SQLite-Sicherung unter *Backups* der richtige Weg.

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
| Bitpanda „teilweise synchronisiert“ | Abruf unvollständig (Drosselung, Pagination-Angaben fehlen oder widersprechen sich) – der nächste Lauf holt erneut ab, der Abrufstand bleibt; „Abdeckung“ der Datenquelle nennt das Ende der Pagination und die Ursache. |
| Bitpanda: alle Vorgänge „ungeklärt: Zeitpunkt fehlt“ ohne Mengen, „Pagination wiederholt denselben Cursor“ | Zeilen einer Version vor 0.16.2 (Pagination und Zeitfeld nicht nach Referenz). 1. Laufende Version prüfen: Seitenleiste bzw. `/healthz` muss 0.16.2 mit erwartetem Build zeigen (sonst Container aktualisieren, siehe *Welcher Stand läuft?*). 2. *Datenquelle → Vollständig neu abrufen*: unbearbeitete alte Prüfzeilen werden ersetzt, Buchungen bleiben. 3. Bearbeitete alte Zeilen: im Prüf-Stapel „Veraltete Zeilen neu auswerten“. 4. Bleibt „Zeitpunkt fehlt“, zeigt die Abdeckung, ob `credited_at` fehlt („Zeitpunkt: … fehlt n×“) – dann liefert Bitpanda ihn nicht; die Zeile wird nicht gebucht. |
| Bitpanda „Berechtigung fehlt“ / „abgelehnt“ | API-Key mit Leserecht „Transaction“ neu erstellen und unter „API-Key ersetzen“ eintragen. |
| Prüf-Stapel: „rekonstruierte Buchung … im kuratierten Import – ersetzt dieser Vorgang sie?“ | Echte Abrechnung zu einer geschätzten Buchung: im kuratierten Import die rekonstruierte Buchung ersetzen und den Vorgang auslassen – oder übernehmen, wenn es ein zusätzlicher Vorgang ist. |
| Prüf-Stapel: „gleiche Menge wie … – möglicherweise doppelt erfasst“ | Eine vorhandene Buchung auf demselben Konto hat exakt dieselbe Menge (≤ 36 h). Beim Anbieter bzw. im Explorer prüfen; nur bei zwei echten Vorgängen übernehmen. |
| Prüf-Stapel: „„TH“ bei Bitpanda ist Threshold Network – Asset zuordnen“ | Anbieter-Kürzel ohne bestätigte Kursquelle: Vorschlag „neu anlegen“ (eigenes Asset mit CoinGecko-ID des Anbieter-Coins) übernehmen oder ein passendes Asset zuordnen; gilt nur für diesen Anbieter. |
| Diagnose zeigt „intern konsistent“ trotz Dublettenverdacht | Kein Widerspruch: Der Soll-Bestand stammt aus denselben Buchungen. Klären über Explorer bzw. Anbieter – siehe [Diagnose](#diagnose-datenqualität-und-bestandsabgleich). |
| Diagnose: „Die Vorschau ist nicht mehr aktuell“ | Zwischen Vorschau und Übernehmen haben sich Daten geändert (z. B. Abruf einer Datenquelle). Es wurde nichts geändert – Vorschau neu öffnen, prüfen, erneut übernehmen. |
| Diagnose: „Rückgängig nicht möglich“ | Ein betroffenes Objekt wurde nach der Korrektur anderweitig geändert (z. B. Kursquelle neu gesetzt); Portfolia überschreibt das nicht. Den Stand unter Kursquellen bzw. im Journal selbst anpassen. |

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
`app/datasources/bitpanda.py` mit `tests/test_bitpanda.py` (synthetische Fixtures im Format der Referenz unter
`tests/data/bitpanda/`, Test-Server lehnt nicht Dokumentiertes ab) einen produktiven.

CI (GitHub Actions): Lint und Tests bei jedem Push/PR; Image-Build und Veröffentlichung nach GHCR
(`ghcr.io/pneumann1980/portfolia`) nur für den Standard-Branch und Versions-Tags – als Docker-Manifestliste ohne
Attestierungen (Unraid-Update-Prüfung), was ein eigener CI-Schritt prüft. Das Image trägt den Commit
(`PORTFOLIA_REVISION`, Label `org.opencontainers.image.revision`); der Smoke-Test prüft ihn über `/healthz`, der
Schritt „Veröffentlichte Tags“ nennt Tags und Digest.

---

## Grenzen und Lizenz

Bekannte Grenzen (Auswahl, vollständig in [`docs/MILESTONES.md`](docs/MILESTONES.md)):

* Nur Basiswährung EUR; Fremdwährungsgewinne (§ 23 EStG) werden nicht ermittelt.
* Vorabpauschale mit Börsenschlusskursen statt Rücknahmepreisen; Altanteile vor 2018 nicht berücksichtigt.
* Formularzeilen nur für 2024 und Anlage SO 2025 hinterlegt (aus Sekundärquellen, ohne Gewähr); für andere
  Jahre nennt die Übertragungshilfe nur die Feldbezeichnungen.
* Datenquellen sind inoffiziell (Yahoo) bzw. limitiert (CoinGecko Demo); Ausfälle werden sichtbar markiert.
* Bitpanda-Anbindung gegen die offizielle Referenz und synthetische Antworten getestet, nicht mit einem echten
  Konto; nicht abgebildete Vorgänge (Tausch, Stocks, Metalle, Indizes, Korrekturen) bleiben zur Prüfung – siehe
  [Grenzen der Bitpanda-Anbindung](#grenzen-der-bitpanda-anbindung).

**Lizenz:** Portfolia steht unter der [MIT-Lizenz](LICENSE) – Nutzung, Änderung und Weitergabe (auch
kommerziell) sind erlaubt, solange Copyright- und Lizenzhinweis erhalten bleiben; keine Gewährleistung.

Drittkomponenten im Image (Details und vollständige Paketliste: [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)):
Apache ECharts 6.1 (Apache-2.0, inkl. NOTICE; enthaltene d3-Teile BSD-3-Clause) und htmx 2.0 (0BSD) –
Lizenztexte unter `app/static/vendor/`; Bitstream Vera Fonts (über ReportLab) für PDFs; Python-Pakete u. a.
unter MIT, BSD, Apache-2.0, PSF-2.0 und MPL-2.0 (certifi, unverändert). Alle sind mit der MIT-Lizenz vereinbar.
