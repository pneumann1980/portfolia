# Portfolia

Selbst gehostetes Portfolio-Dashboard für Aktien, ETFs und Kryptowerte – ein Docker-Image für Unraid
(6.12+/7.x), nur für das lokale Netz. Portfolia liest einen kuratierten Import (ZIP) **read-only**, bewertet
die Positionen mit öffentlichen Kursquellen, berechnet Performance (TTWROR/IRR nach der Methodik von
Portfolio Performance), zeigt Haltefristen und erstellt Steueraufstellungen als PDF. Passende News und
YouTube-Videos werden je Position gefiltert.

* **Read-only:** Transaktionen und Stammdaten stammen ausschließlich aus dem Import. Die App speichert nur
  abgeleitete Daten (Kurse, Devisenkurse, News, Videos, Snapshots, Berichte) in `/data/app.sqlite`.
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
4. [Berechnungen](#berechnungen)
5. [Kurse und Datenquellen](#kurse-und-datenquellen)
6. [News und YouTube](#news-und-youtube)
7. [Steuern und Haltefristen](#steuern-und-haltefristen)
8. [Einstellungen, Sicherheit, Backups](#einstellungen-sicherheit-backups)
9. [Betrieb und Fehlerbehebung](#betrieb-und-fehlerbehebung)
10. [Entwicklung](#entwicklung)
11. [Grenzen und Lizenzen](#grenzen-und-lizenzen)

---

## Installation

### Unraid (Community Applications / Template)

1. Template hinzufügen: *Docker → Add Container → Template* mit der URL
   `https://raw.githubusercontent.com/pneumann1980/portfolia/main/unraid/portfolia.xml`
   (oder die Datei `unraid/portfolia.xml` nach `/boot/config/plugins/dockerMan/templates-user/` kopieren).
2. Pfade prüfen:

   | Container | Host (Vorschlag) | Modus | Inhalt |
   |---|---|---|---|
   | `/data` | `/mnt/user/appdata/portfolia` | rw | Datenbank, Cache, Backups, Berichte, `sources.yaml`, `tax_rules/` |
   | `/import` | `/mnt/user/appdata/portfolia-import` | **ro** | Import-ZIPs (nicht innerhalb von `/data` ablegen) |

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
    volumes:
      - ./data:/data
      - ./import:/import:ro
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

Ausprobieren ohne Internet: `DEMO_MODE=true` (deutlich gekennzeichnete synthetische Kurse).

---

## Datenvertrag (Import-ZIP)

Ein Import ist eine ZIP-Datei mit folgendem Inhalt (optional in genau einem Unterordner):

| Datei | Pflicht | Inhalt |
|---|---|---|
| `manifest.json` | ja | `schema_version` (1.x), `generated_at` (ISO 8601), `valuation_date` (YYYY-MM-DD), `files` (`{dateiname: sha256}`), `notes` |
| `transactions.csv` | ja | alle Buchungen |
| `assets.csv` | ja | Stammdaten der Assets |
| `holdings_check.csv` | ja | erwartete Bestände (nur zum Abgleich, nicht zur Berechnung) |
| `issues.csv` | ja | bekannte Datenprobleme (Anzeige unter Datenqualität) |
| `manual_prices.csv` | nein | Kurse für Assets ohne Kursquelle |
| `accounts.csv` | nein | Konten/Depots mit Broker, Depotgruppe und optional Steuerabzug |

CSV: UTF-8, Komma als Trenner, **Punkt als Dezimaltrenner**, erste Zeile Spaltennamen. Unbekannte Spalten
werden mitgespeichert (z. B. `tax_type` in `assets.csv`, `tax_withholding` in `accounts.csv`).

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
Zeilennummern erscheinen nur, wenn sie in den Steuerparametern hinterlegt und geprüft sind (sie ändern sich
jährlich). Name, Steuer-ID und Steuernummer für das Deckblatt sind optional und bleiben lokal.

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
    basiszins: {2026: 0.0320}   # Beispielwert – amtlichen Wert des BMF eintragen
  forms:
    2026:
      anlage_so: {fields: {so_23_gain: {line: "47"}}}   # nur geprüfte Zeilennummern eintragen
  ```

  Fehlerhafte Overrides werden ignoriert und auf der Steuerseite gemeldet. Jeder Bericht dokumentiert
  Regelwerk-, Parameter-Version und einen Fingerabdruck der verwendeten Parameter.

---

## Einstellungen, Sicherheit, Backups

* **Einstellungen:** Darstellung (Schwelle „Sonstige“, Standardzeitraum), Ledger (FIFO-Bereich,
  Cash-Führung je Konto), Kurse (Veraltungsgrenzen, CoinGecko-Budget, Benchmarks, Krypto-Historien-Fallback),
  News/KI, Backups.
* **Zugriff:** Nur im LAN betreiben. Optional Basic-Auth (`AUTH_MODE=basic`, PBKDF2- oder bcrypt-Hash,
  Sperre nach 10 Fehlversuchen in 5 Minuten) oder hinter einem Reverse-Proxy mit eigener Anmeldung
  (`FORWARDED_ALLOW_IPS`, ggf. `ROOT_PATH`).
* **Härtung:** CSRF-Schutz (Double-Submit-Cookie, `Sec-Fetch-Site`), Content-Security-Policy ohne externe
  Quellen außer YouTube-Vorschaubildern (`i.ytimg.com`; andere Bilder werden serverseitig mit SSRF-Schutz
  zwischengespeichert), `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, begrenzte Anfragegröße,
  PDF-Downloads mit `Cache-Control: no-store`, Container ohne Root-Rechte und ohne Capabilities.
* **Backups:** täglich (Uhrzeit einstellbar, Standard 03:15) über die SQLite-Online-Backup-API mit
  Integritätsprüfung, gzip-komprimiert nach `/data/backups`, Aufbewahrung 14 Stück (einstellbar); manuell
  unter *Einstellungen → Backups* oder per `docker exec -u 99:100 portfolia python -m app backup`.
  **Wiederherstellen:** Container stoppen, Sicherung entpacken (`gunzip`), als `/data/app.sqlite` ablegen,
  `app.sqlite-wal`/`-shm` entfernen, Container starten. Portfolio-Daten selbst stammen immer aus der
  Import-Datei.

---

## Betrieb und Fehlerbehebung

* **Ressourcen:** Image ca. 275 MB (Build mit `strip`; ohne Zugriff auf Debian-Paketquellen ca. 311 MB),
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

## Grenzen und Lizenzen

Bekannte Grenzen (Auswahl, vollständig in [`docs/MILESTONES.md`](docs/MILESTONES.md)):

* Nur Basiswährung EUR; Fremdwährungsgewinne (§ 23 EStG) werden nicht ermittelt.
* Vorabpauschale mit Börsenschlusskursen statt Rücknahmepreisen; Altanteile vor 2018 nicht berücksichtigt.
* Formularzeilen werden nicht mitgeliefert (nur Feldbezeichnungen), da sie jährlich wechseln.
* Datenquellen sind inoffiziell (Yahoo) bzw. limitiert (CoinGecko Demo); Ausfälle werden sichtbar markiert.

Drittkomponenten im Image: Apache ECharts (Apache-2.0), htmx (BSD-2-Clause) – Lizenztexte unter
`app/static/vendor/`; Bitstream Vera Fonts (über ReportLab, Bitstream-Vera-Lizenz) für PDFs; Python-Pakete
laut `requirements.txt` mit ihren jeweiligen Lizenzen.
