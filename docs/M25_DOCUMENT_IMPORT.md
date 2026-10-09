# M25 – Belegimport: PDF & Screenshot mit belegter Ergänzung (0.22.0)

Abrechnungen, Kontoauszüge, Dividendengutschriften, Wallet-Belege und Screenshots werden **lokal** gelesen, in
Vorgänge zerlegt, mit nachvollziehbarer Herkunft je Feld ergänzt und an den **bestehenden** Prüf-Stapel der
Import-Pipeline übergeben. Es entsteht **keine Buchung ohne Bestätigung**; vorhandene Buchungen werden nur über die
Korrektur-Engine der Diagnose ergänzt (Vorschau, Prüfsumme, Rückgängig).

Oberfläche: **Buchungen → PDF & Screenshot** (`/journal/documents`).

## 1. Ablauf

```
Upload (1–20 Dateien, Drag-and-drop, XHR-Fortschritt)
  └─ Annahme: Inhaltstyp per Signatur, Größe, SHA-256, Dublette?  → Original lokal (optional), Stapel anlegen
       └─ Hintergrundlauf (abbrechbar, Phasen im Fortschritt):
            Dokument analysieren → Text extrahieren / OCR (isolierter Kindprozess)
            → Transaktionen erkennen (Profile, Zahlenformat, Plausibilität)
            → fehlende Daten recherchieren (Stufe 1–4, Herkunft je Feld)
            → bestehende Buchungen abgleichen (CsvImportService.ingest → evaluate/assess)
            → Vorschläge vorbereiten → fertig
  └─ Prüfansicht je Beleg: Ausschnitt, Feldstatus A/B/C, Widersprüche, Recherche-Protokoll,
     bevorzugte Lösung + bis zu 3 Alternativen, Korrektur → neu bewerten
  └─ Entscheidung im Prüf-Stapel (übernehmen / verknüpfen / auslassen / Sammelaktion / Rückgängig)
     bzw. „Bestehende Buchung ergänzen“ (Diagnose-Operation „amend“: Vorschau → Übernehmen → Rückgängig)
```

Module (`app/documentimport/`): `extract.py` (PDF-Text, OCR, Zeilen mit Boxen), `worker.py` (Kindprozess mit
Limits), `parse.py` (Zahlen, Datum, Uhrzeit, Währung), `profiles.py` (Dokumenttypen, Anbieter, Felder,
Plausibilität), `evidence.py` (Feldbelege und -auflösung), `enrich.py` (Recherchekette), `explorer.py` (öffentliche
Explorer), `bridge.py` (Beleg → `Rec`), `service.py` (Stapel, Speicher, Korrektur, Datenschutz), `amend.py`
(Ergänzung bestehender Buchungen), `web.py` (Oberfläche).

**Keine zweite Ledger-, FIFO- oder Dubletten-Engine:** Belege werden als `Rec`-Zeilen (`DIRECT` bzw. `REVIEW`) über
`CsvImportService.ingest()` gestaget; Asset-/Kontozuordnung, Bewertung, Dubletten-, Transfer- und Quellenabgleich,
Sammelaktionen und Rückgängig sind die vorhandenen. Ergänzungen laufen ausschließlich über
`app.diagnosis.actions` (neue Operation `amend` neben `create`/`hide`/…, gleiche Prüfsummen- und Undo-Mechanik).
Es gibt keine eigene Journal-Schreiblogik.

## 2. Annahme und Sicherheit (AP1)

| Prüfung | Regel |
|---|---|
| Dateityp | Signatur (Magic Bytes), nicht Name/Content-Type: PDF, PNG, JPEG, WebP. ZIP/Office/HTML/EXE → abgelehnt |
| Größe | je Datei 25 MiB, je Stapel 20 Dateien und 100 MiB (Body-Limit nur auf diesem Pfad) |
| PDF | höchstens 50 Seiten, Seitenfläche begrenzt; passwortgeschützt/beschädigt → verständliche Meldung |
| Bilder | höchstens 30 Mio. Pixel (auch gerenderte PDF-Seiten) – Schutz vor Dekompressionsbomben |
| Ausführung | keine Skripte, Formulare, Anhänge oder Links: pypdfium2 liest nur Text und rendert Seiten; JavaScript/OpenAction bleiben wirkungslos (Test) |
| Isolation | Extraktion/OCR im Kindprozess (`python -I`), `RLIMIT_AS` 1 GiB, CPU 300 s, Wanduhr 240 s (`PORTFOLIA_DOC_MEM_MB`, `PORTFOLIA_DOC_CPU_S`, `PORTFOLIA_DOC_TIMEOUT_S`), minimale Umgebung, `OMP_THREAD_LIMIT=1`; Abbruch beendet den Prozess sofort |
| Zwischendateien | `/data/tmp/documents/<sha>.in` (0600), nach dem Lauf gelöscht; beim Start aufgeräumt |
| Protokolle | nur Kennungen, Zähler, Fehlerklassen – **kein Dateiinhalt** (Test prüft Firmenname, ISIN, Depotnummer) |
| Anzeige | Dateinamen bereinigt; Download des Originals mit `Content-Disposition: attachment`, `nosniff`, `CSP: sandbox` |
| Upload | CSRF über Kopfzeile (XHR, gestreamt) bzw. Formularfeld (ohne JavaScript) |

Wiederholbar und idempotent: Gleicher SHA-256 → „bereits verarbeitet“ (kein neuer Stapel). „Erneut auswerten“
ersetzt nur **unbearbeitete** offene Vorschläge desselben Vorgangs; entschiedene Zeilen bleiben, der neue Vorschlag
wird dann als bekannt bzw. Dublette erkannt. Eine **geänderte Fassung** (anderer Inhalt, gleiche Auftrags-/
Transaktions-ID) verweist auf die frühere (`supersedes`) und wird vom Abgleich als derselbe Vorgang behandelt.
Unterbrochene Läufe (Neustart) werden beim Start als „fehlgeschlagen“ markiert – nichts halb Gestagetes.

## 3. Extraktion (AP2)

* **PDF mit Text:** pypdfium2 (Apache-2.0/BSD-3-Clause, PDFium) – Zeichen mit Koordinaten → Zeilen mit Box
  (normiert 0–1, für den Belegausschnitt). PyMuPDF (AGPL) bewusst nicht verwendet.
* **Scan/Screenshot:** Tesseract 5 als Programm (TSV-Ausgabe, `--psm 6`, Sprache `deu+eng` einstellbar),
  Vorverarbeitung mit Pillow: Ausrichtung (EXIF), Graustufen, dunkler Hintergrund → invertiert, Hochskalieren
  kleiner Schrift, Kontrast. Unsichere Zahlenzeilen werden gezielt einzeln nachgelesen (`--psm 7`, höchstens 12 je
  Seite). Konfidenz je Wort/Zeile bleibt am Feld; unter 80 % → Prüfhinweis, keine Sammelübernahme.
* **Zahlen:** `Decimal`, nie `float`. Deutsch (`1.234,56`), international (`1,234.56`), Apostroph/Leerzeichen als
  Tausender. Die **Dokumentkonvention** wird aus eindeutigen Werten bestimmt; mehrdeutige Werte (`1.234`) ohne
  Konvention bleiben ungelöst statt geraten. Datum: `TT.MM.JJJJ`, ISO, Monatsnamen; `03/04/2025` ohne Hinweis →
  „Tag/Monat unklar“. Uhrzeit mit optionaler Zone; ohne Zone gilt die lokale Zeitzone mit Begründung.

## 4. Dokumenttypen und Profile (AP4)

| Typ | Erkennung | Vorgänge |
|---|---|---|
| Wertpapierabrechnung | ISIN + „Abrechnung/Kurswert/Ausmachender Betrag/Order“ | Kauf/Verkauf: Stück, Kurs, Kurswert, Gebühren, Steuern, Devisenkurs, Auftragsnummer, Depot (maskiert) |
| Dividenden-/Ertragsgutschrift | ISIN + „Dividende/Ertrag/Ausschüttung“ | Ertrag + Quellensteuer/Steuern als eigene Zeilen (verknüpft über `related_asset`) |
| Krypto-Abrechnung | Anbieter + Menge/Betrag + Vorgangswort | Kauf/Verkauf (Bitpanda, Binance, Coinbase, Kraken …: Anbieter-ID als Identität) |
| Wallet-Beleg | Tx-Hash (EVM `0x…`, 64 Hex) | Ein-/Ausgang, Netzwerk, Netzwerkgebühr |
| Kontoauszug/Tabelle | Kopfzeile mit ≥ 3 bekannten Spalten inkl. Datum | je Zeile ein Vorgang (bis 300), Währung auch aus dem Spaltenkopf („Betrag (EUR)“) |
| sonst | Schlüssel/Wert-Muster | generisch, sonst „ungeklärt“ |

Anbieter werden am Text erkannt (Bitpanda, Binance, Coinbase, Kraken, Ledger Live, Trade Republic, Scalable,
comdirect, Consorsbank, ING, DKB, flatex, Etherscan, Kaspa). **Grenze:** Die Profile sind generisch
(Beschriftung → Feld, Spaltenköpfe, Muster) und mit synthetischen Belegen getestet – **nicht** mit Originalbelegen
der Anbieter. Abweichende Layouts landen als „ungeklärt“ bzw. mit Lücken im Prüf-Stapel, nicht als falsche Buchung.

Plausibilität: Menge × Kurs ≈ Kurswert, Kurswert ± Gebühren/Steuern = ausmachender Betrag (gleiche Währung),
EUR-Gegenprobe bei Devisenkurs, Widersprüche zwischen Fundstellen → Prüfhinweis (nie automatisch).

**Mehrere Belege desselben Vorgangs** (z. B. Auftragsbestätigung + Screenshot) werden im Stapel zusammengeführt:
gleiche technische Identität, sonst gleiche ISIN/Symbol + Menge + Datum + Art. Ein Vorgang, eine Buchung; Felder
des zweiten Belegs mit Herkunft „anderer Beleg im Stapel“, Widersprüche bleiben sichtbar.

## 5. Herkunft und Recherche (AP3)

Jedes Feld trägt Belege (`FieldEvidence`): Wert, Herkunft, Quelle, Fundstelle (Seite/Zeile/Box, OCR-Konfidenz),
Status, Begründung. Auflösung je Feld nach Status (geschätzt zuletzt) und Herkunft:

`Korrektur (Nutzer) > Datenquelle (Originaldaten) > Beleg > anderer Beleg im Stapel > Portfolia-Buchung > öffentlich`

| Status | Bedeutung | Darf Buchungswert sein? |
|---|---|---|
| **A · belegt** | steht im Beleg bzw. in Originaldaten derselben Kennung | ja (nach Bestätigung) |
| **B · rekonstruiert** | eindeutig aus belegten Werten berechnet (Kurswert/Devisenkurs) oder aus der Buchung **derselben technischen Identität** | ja (nach Bestätigung, mit Hinweis) |
| **C · geschätzt** | Marktpreis/Referenzkurs | **nein** – nur als Alternative angezeigt, nie als Ausführungspreis, Gebühr oder Anschaffungskosten |
| **ungelöst** | kein überprüfbarer Wert | Pflichtfeld fehlt → Zeile „ungeklärt“ |

Recherchekette (Protokoll je Vorgang in der Prüfansicht):

1. **Beleg und Stapel** – Fundstellen, Zusammenführung.
2. **Portfolia** – vorhandene Buchung nur über technische Identität (Anbieter-ID `bitpanda:<uuid>` bzw. Tx-Hash):
   EUR-Wert, Gebühr, Zeitpunkt als „rekonstruiert“. Ähnlichkeit (Datum/Menge) allein reicht nicht – die übernimmt der
   Prüf-Stapel als Zuordnungsvorschlag. Belege mit anderer Identität werden verworfen (Test).
3. **Verbundene Datenquellen** – abgerufene Originaldaten derselben Kennung (Zeitpunkt, Gebühr, Hash) als „belegt“.
   Es werden keine neuen Abrufe ausgelöst und keine Schreibrechte angefragt.
4. **Öffentliche Quellen** – nur wenn in den Einstellungen freigegeben **und** je Stapel angehakt; nur Ein-/Ausgänge
   mit Hash; übertragen wird **ausschließlich der Hash** (Test prüft Pfad, leere Query, leeren Body):
   * Bitcoin: mempool.space `GET /api/tx/{txid}` → Blockzeit, Bestätigung, Gebühr (Satoshi)
   * Kaspa: api.kaspa.org `GET /transactions/{id}?resolve_previous_outpoints=light` → Blockzeit, Annahme,
     Gebühr = Eingänge − Ausgänge
   * Budget je Stapel 6 Anfragen / 25 s, Ratenlimit und Abbruch über den vorhandenen `ChainHttp`-Client.
   * **Grenzen:** EVM-Ketten (Etherscan/RPC) brauchen Schlüssel bzw. Knoten und sind für Belege nicht angebunden;
     Wertpapiere: keine frei nutzbare, belastbare API für Ausführungsdaten – es gibt keine Recherche. Fehler,
     Zeitüberschreitung, „nicht gefunden“ werden protokolliert; es wird nichts ergänzt (Test). Ergebnisse
     öffentlicher Quellen markieren den Vorgang immer zur Prüfung.

Lokale Referenzdaten (ohne Netz): Asset-Zuordnung über ISIN/Symbol (eindeutig) bzw. lokaler CoinGecko-Katalog
(Kandidaten); für fehlende EUR-Werte nur als **Schätzung (C)**: lokal gespeicherter EZB-Referenzkurs bzw. Menge ×
gespeicherter Marktkurs – nie als Buchungswert, nur zur Orientierung in der Prüfansicht.

**Feldregeln** (Auszug): Währung wird nie angenommen (fehlt sie bei Kauf/Verkauf/Ertrag → Pflichtlücke, auch
Kopfzeilen-Währung zählt). Ein geschätzter EUR-Wert wird nicht als `value_eur` eingetragen. Gebühren in
Fremdwährung nur mit belegtem Devisenkurs in EUR. Die Netzwerkgebühr eines Ausgangs gehört dem Absender. Die
Uhrzeit aus der Blockzeit wird nur übernommen, wenn das Belegdatum dazu passt. Depot-/Kontonummern werden maskiert
gespeichert (`…7890`).

## 6. Abgleich und Lösungen (AP5)

Ergebnisarten je Beleg (aus dem vorhandenen Abgleich): **neu**, **Dublette/vorhanden**, **Ergänzung** (gleiche
Buchung, zusätzliche Angaben), **Widerspruch**, **komplex**, **ungeklärt** (Pflichtangabe fehlt). Die Prüfansicht
zeigt die bevorzugte Lösung und bis zu drei Alternativen:

| Lage | bevorzugt | Alternativen |
|---|---|---|
| Buchung vorhanden, Beleg belegt mehr/anderes | Bestehende Buchung ergänzen | unverändert lassen, verknüpfen, auslassen |
| neu | im Prüf-Stapel übernehmen | Angaben korrigieren, auslassen |
| Dublette/Ergänzung | mit vorhandener Buchung verknüpfen | auslassen |
| Widerspruch/komplex | im Prüf-Stapel einzeln prüfen | korrigieren, auslassen |
| ungeklärt | fehlende Angaben ergänzen (Korrektur) | auslassen |

**Bestehende Buchung ergänzen** (`amend.py`): nur belegte/rekonstruierte Felder – EUR-Gegenwert (fehlt, geschätzt,
abweichend), Gebühr, Uhrzeit (Buchung nur mit Datum, gleicher Tag), Tx-Hash, Belegverweis in der Notiz. **Nie**
Menge, Asset, Konto, Vorgangsart (Abweichung → Widerspruch). Manuell erfasste bzw. bearbeitete Buchungen werden nicht
überschrieben. Vorschau mit berechneten Auswirkungen (Bestände, Einstand offener Lots, realisierte Ergebnisse,
Steuerbericht, Diagnose); Übernehmen nur mit unveränderter Prüfsumme; Rückgängig unter Datenqualität →
Entscheidungen und Korrekturen (stellt Overlay bzw. Journalzeile exakt wieder her). Import-Buchungen werden über das
bestehende Overlay (`tx_override`) ergänzt – die Importdatei bleibt unverändert.

## 7. Oberfläche (AP6)

* Upload: Drag-and-drop, mehrere Dateien, Liste mit Größenprüfung, Upload-Fortschritt in %, Abbrechen; ohne
  JavaScript normales Formular.
* Stapel: Fortschritt mit Phasen (Dokument analysieren, Text extrahieren, OCR, Transaktionen erkennen, recherchieren,
  abgleichen, Vorschläge), Abbrechen, Ergebnis mit Links in die Prüf-Stapel.
* Prüfansicht: Beleg (Seiten), Feldtabelle mit Status A/B/C, Herkunft, Fundstelle, **Ausschnitt** je Feld
  (gerendert aus dem lokalen Original, `no-store`), Widersprüche, Alternativen, Recherche-Protokoll, Lösungen,
  Korrekturformular (Validierung: Zahlen DE/EN, Datum, Uhrzeit, ISIN mit Prüfziffer, ISO-Währung; gespeichert als
  Herkunft „Korrektur“, Neubewertung ohne erneute OCR).
* Sammelaktionen: im Prüf-Stapel (vorhanden); auf der Belegseite „neu bewerten“ und „Original löschen“ für mehrere
  Belege.
* Mobil geprüft (390 px, kein horizontales Überlaufen; Headless-Chromium).

## 8. Datenschutz und Speicherung (AP7)

* Alles lokal. Keine KI-Dienste, kein Upload an Dritte; öffentliche Explorer nur nach Freigabe und nur mit Hash.
* Originale (optional, Standard an): `/data/documents/<sha[:2]>/<sha>.<ext>`, Modus 0600, unverändert (Test).
  Ausschalten → nur Feldbelege, kein Ausschnitt/Download.
* „Original löschen“ entfernt Datei **und** extrahierten Volltext (`analysis_json`); es bleiben SHA-256,
  Dateiname, Feldbelege mit Herkunft (ohne Volltext), Prüfzeilen/Buchungen.
* **Sicherungen:** Die tägliche SQLite-Sicherung enthält Belegmetadaten, Feldbelege und – solange nicht gelöscht –
  den extrahierten Text. Originaldateien liegen im Volume `/data` (Sicherung des Volumes, z. B. Unraid-Appdata).
* **Vollständiger Export:** enthält die daraus entstandenen Buchungen (mit Belegverweis in der Notiz) und die
  Einstellungen `documents.*`, **nicht** die Belege selbst (bewusst: personenbezogene Finanzdokumente gehören nicht in
  einen weitergebbaren Export). Grenze: Nach einer Neueinrichtung aus dem Export fehlen Prüfansicht und Ausschnitte.
* **Optionale KI:** geprüft, **nicht umgesetzt**. Die Anforderungen (keine Übertragung ohne Freigabe, keine
  ungeprüften KI-Werte in Buchungen, nachvollziehbare Herkunft) lassen sich mit der regelbasierten Erkennung besser
  einhalten; ein späteres Modul müsste als eigene Herkunft (`ai`, Status höchstens „geschätzt“) andocken und wäre
  standardmäßig aus.

Einstellungen (Belegseite → Einstellungen und Datenschutz): `documents.keep_originals` (an),
`documents.ocr` (an), `documents.language` (`deu+eng`), `documents.public_lookup` (aus).

## 9. Tests (AP8)

`tests/test_documents_pipeline.py`, `tests/test_documents_web.py`, `tests/test_document_evidence.py` – nur
synthetische Belege (`tests/docfixtures.py`: PDF-Schreiber ohne Zusatzbibliothek, gescannte PDFs, Screenshots hell/
dunkel) und `httpx.MockTransport`:

* Formaterkennung, beschädigte PDFs, Pixelbomben, PDF mit JavaScript/OpenAction
* Zahlenformate (DE/EN/Apostroph/Leerzeichen), mehrdeutige Zahlen/Datumsangaben nicht geraten
* Wertpapierkauf (alle Felder belegt, Fundstellen, Maskierung), Rechenwiderspruch → Prüfung, Dividende in USD mit
  Devisenkurs und Quellensteuer, internationales Format, Kontoauszug 10 Seiten/200 Zeilen mit Kopfzeilen-Währung,
  fehlende Währung → Pflichtlücke
* OCR: Screenshot hell/dunkel, gescanntes PDF (mit Tesseract; CI installiert ihn)
* Kindprozess = In-Prozess-Ergebnis, Abbruch
* Recherche: öffentlich nur Hash, aus per Standard, Fehler ergänzen nichts; Portfolia-Identität rekonstruiert Uhrzeit
* Stapel: nichts gebucht, keine Overlays, Logs ohne Inhalt, Wiederholung idempotent, geänderte Fassung, zwei Belege
  desselben Vorgangs → eine Zeile, Abbruch, Neustart während des Laufs, Originale unverändert bzw. nicht gespeichert
* Oberfläche: Upload (XHR/Formular), CSRF, Fortschritt, Prüfansicht, Ausschnitt, Download, Korrektur, Löschen,
  Sammelaktionen, Einstellungen, Ergänzung mit Vorschau → Übernehmen → veraltete Prüfsumme (409) → Rückgängig

## 10. Leistung und Docker (AP9)

`python scripts/bench_documents.py` (synthetisch, temporäres Verzeichnis). Messung in der Entwicklungsumgebung
(Container, 09.10.2026):

| Messung | Zeit |
|---|---|
| Einzelbeleg PDF (Kindprozess inkl. Start) | 0,18 s |
| Kontoauszug 10 Seiten / 200 Zeilen: Extraktion + Analyse | 0,25 s + 0,10 s |
| Screenshot 900 px (OCR) | 0,51 s |
| Gescanntes PDF, 1 Seite (Rendern + OCR) | 1,06 s |
| Stapel 20 Belege bis Prüf-Stapel gegen 12.577 Buchungen / 230 Assets | 8,1 s |
| Prüfansicht (HTTP) / Neu bewerten | 0,66 s / 0,30 s |
| Spitzenspeicher Hauptprozess / Kindprozess (RSS) | 183 MB / 132 MB |

Docker-Image (amd64): **385 MiB entpackt** (CI-Messung, Commit 230e1f7), ≈ 143 MB komprimiert (lokaler Build);
die Tesseract-Schicht macht ≈ 80 MB entpackt aus (davon `libicu72` ≈ 36 MB, harte Abhängigkeit). CI-Grenze daher
450 MiB (vorher 350 MiB). Der
CI-Smoke-Test prüft zusätzlich `pypdfium2`, Tesseract mit `deu`, und lädt einen synthetischen Beleg per HTTP hoch
(Stapel läuft durch, Beleg im Prüf-Stapel). Keine GPU, kein Modellserver, keine zusätzlichen Dienste.

## 11. Grenzen

* Anbieterprofile generisch, nicht an Originalbelegen validiert (keine echten Belege verfügbar/verwendet).
* Handschrift, stark verzerrte Fotos, mehrspaltige Layouts mit verschachtelten Tabellen: OCR/Zeilenbildung
  unzuverlässig → „ungeklärt“ bzw. Prüfhinweise.
* Öffentliche Recherche nur Bitcoin/Kaspa; keine Wertpapier-Ausführungsdaten aus öffentlichen Quellen.
* Ein Upload-Stapel läuft zur Zeit; ein weiterer Upload währenddessen wird abgewiesen (Stapel „abgebrochen“,
  Zwischendateien gelöscht) – keine Warteschlange.
* Belege sind nicht Teil des vollständigen Exports.
