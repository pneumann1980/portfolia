# News-Intelligence (0.46): Artikeltext, Leseansicht, KI-Analyse, Ereignisse, Tagesbericht

Stand: 0.48.2. Ergänzt die bestehende News-Pipeline (`docs/MILESTONES.md` M3) um fünf Bausteine. Alles ist
**rückwärtskompatibel**: der Feed-Import, die Relevanz-/Dedupe-Logik, YouTube und Claude-Zusammenfassungen laufen
unverändert; die neuen Stufen sind eigene Hintergrundjobs, deren Ausfall den Import nie blockiert.

## Überblick und Architektur

```
Feed-Import (news_fetch, alle 30 min)          Stufe A: Titel/Teaser → news_item   (unverändert)
   └─ triggert ─► news_enrich  (15 min)         Stufe B: Artikeltext → news_content
                    └─ triggert ─► news_analyze (20 min)   Einzelanalyse → news_analysis (nur mit KI)
                                     └─► news_group (20 min)  Ereignisse → news_event / news_event_item
news_report (15 min, prüft Uhrzeit)             Tagesbericht → news_report            (nur mit KI)
```

| Modul (`app/news/`) | Aufgabe |
|---|---|
| `safefetch.py` | SSRF-sicherer Abruf (siehe unten) |
| `extract.py` | HTML → Artikeltext (`lxml`; JSON-LD, `<article>`, Absatzdichte), Qualitätsprüfung |
| `gnews.py` | Google-News-Weiterleitungslinks auf die Original-URL auflösen |
| `enrich.py` | Auswahl, Abruf, Retry/Backoff, Host-Ruhezeiten, Speicherung (`news_content`) |
| `ai.py` | Anbieter Gemini (REST) und Claude, Budget-/Takt-/Pausenlogik, Nutzung (`ai_usage`) |
| `analysis.py` | strukturierte Einzelanalyse, Validierung, Hash-Idempotenz |
| `events.py` | lokale Ereignisgruppierung, KI nur für Grenzfälle, manuelle Korrektur |
| `report.py` | täglicher Finanzmarktbericht, Planung, Historie |
| `status.py` | Überwachung (Heute-Zähler, Budget, Fehler) |

## Datenmodell (Migration 33)

Alle Tabellen hängen per `ON DELETE CASCADE` an `news_item`; die Aufbewahrungsfrist (`retention_days`, 90 Tage)
löscht Inhalt, Analyse und Ereigniszuordnung mit. Bestehende Daten bleiben unverändert (`news_item`, `news_asset`,
`llm_usage`, `digest` unberührt).

* `news_content` – Status `pending | success | partial | blocked | failed`, Grund (`reason`), `final_url`,
  `canonical_url`, Titel/Autor/Zeit/Sprache/Bild aus der Seite, bereinigter Text (`content`, Absätze durch Leerzeile),
  `excerpt`, `word_count`, `quality`, `content_hash`, `priority`, `attempts`, `next_attempt`. **Kein HTML, keine Skripte.**
* `news_analysis` – je Artikel genau eine aktuelle Analyse: Anbieter, Modell, Prompt-Version, Textbasis
  (`fulltext | excerpt | teaser | error`), Kategorie, Geltungsbereich, Stimmung, Bedeutung, Konfidenz, Zusammenfassung,
  vollständiges Ergebnis als JSON, Tokens, `input_hash`. Fehlschläge stehen als Zeile ohne Zusammenfassung (Ruhe 12 h).
* `news_event` / `news_event_item` / `news_event_split` – Ereignis ↔ Meldungen (eine Meldung gehört höchstens zu einem
  Ereignis), abgelehnte Paare.
* `ai_usage` – je Tag und Anbieter: Anfragen, Eingabe-/Ausgabe-Tokens (**tatsächliche Werte aus der Antwort**), Fehler,
  Kontingenttreffer. Gemini zählt nach Pacific Time (Tageskontingent setzt dort um Mitternacht zurück).
* `news_report` – Tagesberichte (Tag, Version, Anbieter, Modell, Zeitraum, JSON-Inhalt). Neuerzeugung = neue Version.

## Stufe B: Artikeltext

* **Auswahl:** noch nicht angereicherte Artikel der letzten 3 Tage, nach Priorität = Positionsbezug (Relevanz)
  + Mehrfachberichte (`dup_count`) + Quellgewicht + Aktualität. **Marktweite Meldungen ohne Positionsbezug werden
  ebenfalls angereichert** (für den Tagesbericht); pro Lauf höchstens 40 (Einstellung `news.enrich_per_run`, nicht in
  der Oberfläche; alle 15 min). **Artikel pausierter Quellen** (nach wiederholten Fehlern/Sperren 6 h Ruhe) werden bei der
  Auswahl übersprungen – vorher belegten sie die vordersten Plätze jedes Laufs und hielten den Abruf anderer Quellen auf. Videos, YouTube/Social-Hosts, ausgeblendete Meldungen werden übersprungen.
* **Marktweite Quellen:** `examples/sources.yaml` enthält neu tagesschau Wirtschaft, Federal Reserve und EZB mit
  `require_match: false`. In bestehenden `sources.yaml` werden sie **einmalig** ergänzt (Kommentare und vorhandene
  Einträge bleiben unverändert; später gelöschte Einträge kommen nicht zurück). Alle URLs werden – wie bisher – zur
  Laufzeit geprüft; unerreichbare Quellen pausieren und erscheinen unter *News → Quellen*.
* **Google-News-Links:** erst Offline-Dekodierung (Base64), sonst Auflösung über die (undokumentierte)
  `batchexecute`-Schnittstelle von Google. **Best effort:** ändert Google das Verfahren, bleibt der Artikel bei
  `gnews_unresolved`, wird bis zu dreimal später erneut versucht und zeigt in der Leseansicht den Anreißer.
* **Extraktion ohne Zusatz-Bibliothek:** `lxml` (bereits im Image) – JSON-LD `articleBody`, dann
  `<article>`/`itemprop`/bekannte Klassen, sonst Absatzdichte; Navigation, Footer, Cookie-/Teilen-/Verwandt-Blöcke,
  Linklisten und Anzeigenzeilen entfallen. *Trade-off:* `trafilatura` extrahiert auf exotischen Seiten besser, zöge aber
  ~10 Pakete nach (u. a. `dateparser`, `babel`, `tld`) und vergrößerte das Image; die Qualitätsprüfung fängt
  Fehlextraktionen ab (`partial`/`failed`).
* **Status:** `success` ≥ 150 Wörter, ≥ 3 Absätze, nicht link-dominiert · `partial` ≥ 40 Wörter ·
  `blocked` (Bezahlschranke: JSON-LD `isAccessibleForFree=false` oder Marker im Text und < 250 Wörter;
  Zustimmungs-/Bot-Wand; HTTP 401/402/403/451) · `failed` (kein Text, Netzwerk, 404 …).
  **Nichts wird umgangen** – kein Login, keine Cookies, keine Header-Tricks, kein Archivdienst.
* **Wiederholung:** nur bei vorübergehenden Fehlern (Timeout, Netz, DNS, 429, 5xx, Google-News-Auflösung):
  nach 30 min, 3 h, 12 h; danach endgültig `failed`. `blocked`/`partial` werden nicht wiederholt (manuell: „Erneut laden“).
* **Host-Schutz:** mind. 2 s Abstand je Host (Google News 3 s); nach wiederholten Fehlern ruht ein Host 6 h.
* **Urheberrecht/Speicherumfang:** Der Text dient der **privaten Lektüre auf der eigenen Instanz**; er wird nicht
  weitergegeben, mit Quelle und Original-Link angezeigt und mit der Aufbewahrungsfrist gelöscht. Einstellung
  *Speicherumfang*: bereinigter Volltext (Standard, max. 30.000 Zeichen) **oder nur Auszug** (~1500 Zeichen). Ob die
  Speicherung im Einzelfall zulässig ist, richtet sich nach den Nutzungsbedingungen der Verlage – im Zweifel
  „nur Auszug“ wählen oder die Anreicherung abschalten (`Einstellungen → News`).

### SSRF-Schutz (`safefetch.py`)

* nur `http`/`https`, nur Port 80/443, keine Zugangsdaten in URLs;
* Hostname wird **einmal** aufgelöst; alle Adressen müssen öffentlich sein (nicht privat, Loopback, Link-Local,
  Multicast, reserviert, CGNAT, IPv4-mapped/6to4) – gemischte Antworten werden abgelehnt;
* **DNS-Rebinding-Schutz:** die Verbindung geht an die **geprüfte IP**; Host-Header und TLS-SNI/Zertifikatsprüfung
  bleiben beim Hostnamen. Ein zweiter, abweichender DNS-Treffer kann nicht mehr eingeschleust werden;
* Weiterleitungen werden einzeln verfolgt (max. 5), jede Station wird erneut geprüft (auch IP-Literale wie
  `169.254.169.254`);
* Grenzen: 2,5 MB, 15 s je Anfrage, nur Text-Content-Types, gestreamt und bei Überschreitung abgebrochen;
* keine Proxy-Umgebung (`trust_env=False`) – sonst wäre das Anpinnen wirkungslos. *Grenze:* Wer nur über einen
  Proxy ins Internet kommt, kann den Artikeltext-Abruf nicht nutzen (Feeds laufen weiter über den bisherigen Client).

## Leseansicht `/news/<id>`

Klick auf einen Artikel in Liste, Dashboard oder Asset-Seite öffnet die **interne Leseansicht** (Videos weiter beim
Anbieter). Aufbau: Titel, Quelle, Veröffentlichungszeit, Bild (über den vorhandenen SSRF-geschützten Bild-Proxy),
Statushinweis (Volltext/Teiltext/gesperrt/fehlgeschlagen/ausstehend), **Artikeltext**, Originallink **separat**
(`rel="noopener noreferrer"`), „Erneut laden“. Darunter **getrennt und gekennzeichnet**: *KI-Zusammenfassung* (mit
Anbieter, Modell, Zeit, Textbasis), *Fakten laut Artikel*, *Zitierte Einschätzungen Dritter*, *KI-Einordnung*
(„Einschätzung, keine Tatsache“), *Unsicherheiten*. Dann *Weitere Berichte zum Ereignis* mit „Gehört nicht dazu –
aus Ereignis lösen“. Der Lesestatus wird beim Öffnen gesetzt; manuelles Zurücksetzen auf „ungelesen“ bleibt möglich
(`?keep=1`, kein erneutes Lesen). Der Rücksprung (`back=`) akzeptiert nur interne `/news…`-Adressen. Fehlt der
Volltext, erscheinen Anreißer bzw. Auszug – die Seite ist nie leer. Artikeltext wird ausschließlich als Text
ausgegeben (Jinja-Autoescape), nie als HTML.

## KI-Anbieter: Claude und Google Gemini

* **Auswahl:** *Einstellungen → News, Artikeltexte & KI*. `llm.enabled` (Hauptschalter, Standard **aus**) +
  Schlüssel des gewählten Anbieters (`ANTHROPIC_API_KEY` bzw. `GEMINI_API_KEY`). Claude bleibt unverändert nutzbar
  (Modell, Tages-Token-Budget, serverseitige Fallbacks, `llm_usage`).
* **Gemini-Zugriff:** REST `POST /v1beta/models/<modell>:generateContent` (laut Google weiterhin „vollständig
  unterstützt“), Schlüssel im Header `x-goog-api-key` (nie in der URL, nie im Log – Redaction wie bei den anderen
  Schlüsseln), JSON-Modus über `responseMimeType`. Das Schema steht im Systemtext; **die Prüfung gegen das Schema
  erfolgt clientseitig** (unabhängig von Schema-Dialekten einzelner Modellgenerationen). Kein SDK, keine zusätzliche
  Abhängigkeit.
* **Modelle (Stand der Prüfung 2026-10, https://ai.google.dev/gemini-api/docs/pricing):** wählbar sind nur
  Flash-/Flash-Lite-Modelle mit „Free of charge“: `gemini-3.5-flash-lite` (**Standard**), `gemini-3.1-flash-lite`,
  `gemini-2.5-flash-lite`, `gemini-3.8-flash`, `gemini-3.6-flash`, `gemini-2.5-flash`. Pro-, Bild-, TTS-, Live-,
  Audio- und Embedding-Modelle werden abgelehnt (serverseitig geprüft); ein ungültiger Wert fällt auf den Standard
  zurück – **nie** still auf ein kostenpflichtiges Modell.
* **Kontingent – Grenzen der Aussage:** Google veröffentlicht die Free-Tier-Zahlen (RPM, TPM, RPD) **nicht in der
  Dokumentation**, sondern nur in AI Studio, und ändert sie. Die Voreinstellungen (5 Anfragen/min, 100.000 Tokens/min,
  100 Anfragen/Tag, 200.000 Tokens/Tag) sind **eigene vorsichtige Annahmen** und im UI einstellbar. Sie begrenzen die
  Last, **garantieren aber nicht**, dass Google die Nutzung als kostenlos wertet: Kontingent und Abrechnung gelten
  **je Google-Projekt** (ein Schlüssel eines Projekts mit hinterlegter Abrechnung wird abgerechnet). Bitte den Schlüssel
  in einem Projekt ohne Abrechnung anlegen.
* **Verhalten bei Kontingentende:** HTTP 429 / `RESOURCE_EXHAUSTED` → Anbieter wird kontrolliert **pausiert**
  (Minutenlimit: `retryDelay` bzw. ≥ 30 s; Tageslimit: bis Mitternacht Pacific Time). Es erfolgt **kein** Wechsel auf
  ein anderes Modell oder einen anderen Anbieter. Lokale Takt-Grenzen (RPM/TPM, Wartezeit ≤ 75 s, sonst Pause) und
  Tagesgrenzen (Anfragen, Tokens) greifen **vor** dem Aufruf. Verbrauch wird aus `usageMetadata` übernommen (inkl.
  „Thoughts“-Tokens), nicht geschätzt.
* **Batch:** die Analyse sendet 4 Artikel je Anfrage (≈ 3.200 Zeichen Text je Artikel); die Ereignis-Grenzfälle
  höchstens eine Anfrage je Lauf; der Tagesbericht eine Anfrage je Tag (aus gespeicherten Analysen, nicht aus Volltexten).
  Richtwert bei 12 Analysen je Lauf alle 20 min: ≤ 36 Anfragen/h → bei Bedarf `news.analysis_per_run` bzw.
  Tageslimit senken. Bei anhaltenden Störungen (Timeout, 5xx) verdoppelt sich ab dem zweiten Fehler in Folge der Abstand
  zum nächsten Aufruf (5 → 120 min); die betroffenen Artikel werden dabei **nicht** als fehlgeschlagen markiert.
* **Datenschutz:** Im kostenlosen Kontingent darf Google Eingaben zur Produktverbesserung verwenden. Deshalb gehen
  **nur öffentliche Daten** hinaus: Titel, Quelle, Zeit, Artikeltext, Namen betroffener Assets – **nie** Stückzahlen,
  Werte, Gewichte, Konto-/Börsen-/Wallet-Namen, Transaktionen, Adressen oder Steuerdaten (Test:
  `tests/test_news_intel.py::test_ai_payloads_only_contain_public_fields` und `tests/test_privacy.py`). Finanzpfade
  (Ledger, Steuer, Import, Datenquellen …) importieren keinen KI-Client (statische Prüfung in `tests/test_cockpit.py`).

## Einzelanalyse (`news_analysis`)

Pro Artikel (Stapel zu 4): `kategorie` (erweiterbar in `analysis.CATEGORIES`), `geltungsbereich`
(asset/branche/markt/makro), `betroffene` (Wert, Art, Richtung), `fakten`, `zitierte_einschaetzungen`, `einordnung`,
`unsicherheiten`, `stimmung`, `bedeutung`, `konfidenz`, `ereignis`, `zusammenfassung`.

* **Fakten ≠ Einschätzungen ≠ eigene Einordnung ≠ Unsicheres** – eigene Felder, eigene Darstellung.
* **Validierung:** Pflichtfelder, Aufzählungen, Längen, Markup-Entfernung, Zahlenbeleg: Zahlen (≥ 3 Ziffern) einer
  „Tatsache“ müssen im Artikeltext vorkommen, sonst wird die Aussage verworfen und in den Unsicherheiten vermerkt.
  Unbrauchbare Antworten werden nicht gespeichert (nur Fehlerzeile, 12 h Ruhe).
* **Idempotenz:** `input_hash` = Prompt-/Schema-Version + Titel + Inhalts-Hash (bzw. Teaser). Unveränderte Artikel werden nie
  erneut analysiert; ein geänderter Text oder eine neue Prompt-Version führt zur Neuanalyse. Anbieterwechsel allein nicht.
* **Basis:** Analysiert wird erst nach abgeschlossener Textbeschaffung (oder nach 6 h mit Anreißer). Bei Teiltext/Anreißer
  wird die Konfidenz auf 0,6 bzw. 0,4 gedeckelt.
* **Prompt-Injection:** Artikeltexte stehen nur als JSON-Zeichenketten im Nutzerteil; der Systemtext erklärt sie zu
  unvertrauenswürdigen Daten; die Antwort wird nur geprüft, bereinigt und als Text angezeigt (nie ausgeführt oder als
  HTML gerendert). *Grenze:* kein Schutz kann eine inhaltlich manipulierte, aber formal gültige Analyse ausschließen –
  daher die Kennzeichnung als KI-Ausgabe.

## Ereignisgruppierung

Lokal und deterministisch (`events.py`): Titel-Jaccard ≥ 0,6 mit ≥ 3 gemeinsamen inhaltstragenden Wörtern, Zeitfenster
36 h, gleicher Inhalts-Hash, bei vorhandener Analyse zusätzlich der neutrale Ereignistitel (gleiche Kategorie).
**Nie** nur wegen desselben Tickers/Assets. Zahlenkonflikt-Sperre („25 bp“ ≠ „50 bp“). Mittlere Ähnlichkeit
(0,35–0,6) geht – nur bei aktiver KI – gebündelt in **eine** Anfrage („im Zweifel nein“); ohne KI bleiben Grenzfälle
getrennt. Keine Vektor-Datenbank. Korrektur: *Aus Ereignis lösen* merkt sich das Paar dauerhaft und fixiert das
Ereignis (keine automatische Erweiterung mehr). Ereignisse mit < 2 Meldungen existieren nicht.
*Grenze:* rein lexikalisch – gleiche Ereignisse in unterschiedlicher Wortwahl/Sprache werden ohne KI nicht erkannt.

## Täglicher Finanzmarktbericht

* **Zeitplan:** Job alle 15 min; erzeugt den Bericht einmal je Tag ab der eingestellten Uhrzeit (Standard 07:30,
  Europe/Berlin); nach Neustart/Ausfall wird nachgeholt; idempotent. Fehlversuche: Wartezeit 1 h, höchstens 3 je Tag;
  **ein Fehler überschreibt nie einen vorhandenen Bericht**. Zu wenige Analysen (< 3) → kein Bericht (kein Versuch verbraucht).
* **Inhalt:** Marktüberblick, wichtigste Ereignisse (mit Bedeutung und gekennzeichneter KI-Einordnung), Krypto,
  Bedeutung fürs Portfolio (**qualitativ, ohne Beträge** – die KI kennt nur die Namen betroffener Assets), Ausblick
  (nur Termine/Erwartungen, die eine Meldung ausdrücklich nennt), Unsicherheiten, Quellen.
* **Belegzwang:** Jede Aussage verweist auf eine Eingabe-Nummer; ungültige Verweise, Portfolio-Assets, die nicht
  geliefert wurden, und Ausblick-Punkte ohne Beleg werden verworfen. Quellenlinks (interne Leseansicht + Original-URL)
  setzt Portfolia selbst; die KI nennt keine Adressen.
* **Zeiten getrennt:** Ereigniszeit (erste Meldung), Artikelzeit (Veröffentlichung), Berichtszeit (Erstellung).
* **UI:** *News → Tagesbericht* (aktueller Bericht, Historie, „Neu erzeugen“ = neue Version, Fehlerhinweis),
  Kurzfassung oben in der Artikelliste. Der frühere Claude-„Tagesdigest“ ruht, solange der Bericht aktiv ist.

## Überwachung

*News → Status*: heute importiert / Text geladen (vollständig, Teiltext, gesperrt, fehlgeschlagen, wartend, häufige
Gründe) / analysiert / verworfen / Ereignisse; KI-Anbieter, Modell, Nutzung heute (Anfragen, Tokens, Fehler,
Kontingenttreffer), lokale Grenzen, **Restbudget**, Pause bis …, letzter Tagesbericht, Job-Status mit Ergebnis,
letzte Fehler. Der Hinweis zur Free-Tier-Garantie steht dort ebenfalls.

## 0.47: Rückstau, Startseite, Berichts-Fortschritt, längere Texte

* **Rückstau:** `news_analyze` läuft alle 10 Minuten und arbeitet **in Runden** (36 Meldungen je Runde, Stapel zu 6) alles
  ab, was in den letzten **48 Stunden** noch keine aktuelle Analyse hat – nach Priorität (Positionsbezug,
  Mehrfachberichte, Quellgewicht, Aktualität) – bis nichts mehr wartet, ein Limit (Minute/Tag/Budget) greift, der Anbieter
  stört oder die Laufzeitgrenze (8 min) erreicht ist. Einstellungen ohne Oberfläche: `news.analysis_max_per_run` (400),
  `news.analysis_hours` (48). *News → Status* zeigt den Rückstau (Artikeltexte und Analysen).
* **Rückstau-Schutz (0.54.1):** Die Restzeit der Analyse rechnet mit dem Zufluss **analysierbarer** Meldungen (ohne Quellen mit
  `ai: false`). Der gemessene Durchsatz gilt nur als Kapazität, wenn die Pipeline ausgelastet war (mehr wartend als ein Lauf
  schafft); bei kleinem Rückstau misst er lediglich das Angebot, dann gilt die Nennleistung – die frühere Meldung „wächst
  derzeit“ bei 9 wartenden Meldungen war ein Fehlalarm. Zusätzlich prognostiziert sie das Tageskontingent bis zum Zurücksetzen: das Knappere aus restlichen Anfragen (× 6 je
  Anfrage) und restlichem Token-Budget (÷ mittlerer Verbrauch je Analyse). Ohne Tagesgrenze (`llm.gemini_rpd` = 0 bzw.
  „Token-Tagesgrenze aufheben“) entfällt der jeweilige Teil.
  Kann der Rückstau der **frischen** Meldungen (bis 24 h) nicht rechtzeitig abgebaut werden (wächst, Kontingent reicht nicht oder
  dauert länger als 6 Std.), ist der Schutz **aktiv**: analysiert werden nur Meldungen der letzten 24 Std., die **neuesten zuerst**
  (Aktualität vor Relevanz); ältere werden nicht mehr bearbeitet, bis der Druck weg ist – statt am Ende Altes aufzuarbeiten.
  *News → Status* zeigt „Rückstau-Schutz: aktiv“ mit Grund, Zahl der zurückgestellten Meldungen und Abhilfe. Einstellungen ohne
  Oberfläche: `news.analysis_protect` (an), `news.analysis_protect_hours` (6), `news.analysis_protect_age_h` (24). Grenzen: Die
  Schwellen sind Annahmen; das Minutenlimit geht nur über die Nennleistung je Lauf ein. Der Token-Verbrauch je Analyse ist ein
  Durchschnitt der letzten 100; vor der ersten Analyse entfällt die Token-Prognose.
* **Token-Budget:** *Einstellungen → News & KI → „Token-Tagesgrenze aufheben (unbegrenzt)“* (nur Gemini, ausdrücklich zu
  wählen; Standard bleibt 200.000). Anfragen pro Minute und pro Tag sowie Tokens pro Minute bleiben bestehen. Die
  Status-Seite zeigt dann „unbegrenzt“. Claude hat bewusst keine unbegrenzte Option (kostenpflichtig).
* **Tagesbericht „Neu erzeugen“:** Fortschritt in Phasen (Meldungen sammeln → KI erstellt den Bericht → Prüfen und
  speichern), Abfrage alle 2 s, automatisches Neuladen nach Abschluss; Fehler erscheinen direkt (Grund, „bestehender
  Bericht bleibt unverändert“). Der Job läuft im Pool `interactive` (2 Threads) – nicht hinter Import und Analyse.
* **Ereignisse:** Gruppen werden nur vereint, wenn die *mittlere* Ähnlichkeit aller Paare zwischen ihnen ≥ 0,4 ist; höchstens
  30 Meldungen je Ereignis; stärkste Paare zuerst. Beim ersten Lauf nach dem Update wird der abgeleitete, nicht fixierte
  Ereignisbestand einmalig verworfen und sauber neu aufgebaut (manuell fixierte Ereignisse bleiben).
* **Startseite:** Der Kasten unten zeigt **Top-News mit KI-Auswertung**: Tagesüberblick (Kurzfassung des Berichts, wenn jünger
  als 36 h) und die fünf wichtigsten Ereignisse der letzten 24 h (Rang: Bedeutung, Positionsbezug, Anzahl Berichte,
  Relevanz) mit Zusammenfassung, Stimmung, Assets und Quellen; freie Plätze füllt die bisherige Liste („KI ausstehend“).
  Ohne Analysen bleibt die bisherige Liste. Die Artikelliste unter *News* zeigt ebenfalls das Stimmungs-Badge.
* **Ausfall-Fallback (0.47.1):** `AiGateway.health()` meldet „KI nicht erreichbar“, wenn die KI ausgeschaltet/ohne Schlüssel ist,
  der Anbieter pausiert oder der jüngste Aufruf der letzten 30 min fehlgeschlagen ist und seither kein Erfolg folgte
  (`ai.last_ok`/`ai.last_error`). Dann zeigt die Startseite einen Hinweis mit Grund und die **neuesten** Meldungen nach
  Aktualität; vorhandene Analysen erscheinen nur als Zusatz. Im Normalbetrieb sind **2 der 5 Plätze** für die neuesten
  noch nicht analysierten Meldungen („KI ausstehend“) reserviert (gibt es weniger, fallen sie an die Analysen).
  Ein Schalter **KI-Auswertung | Original** im Kasten wechselt zur bisherigen Liste; die Wahl steht im Browser
  (`localStorage`, nur Anzeige, keine Serverdaten). Grenze: Der Zustand wird erst nach einem fehlgeschlagenen Aufruf
  erkannt (kein aktiver Ping); ohne JavaScript bleibt die KI-Ansicht sichtbar.
* **Status (0.47.2):** „Rückstau“ nennt den Zeitraum (Artikeltexte: letzte 3 Tage), „Engpass“ sagt, worauf die Pipeline wartet
  (KI-Kontingent/Pause, KI aus, Abruf-Takt, Analyse-Takt oder nichts), „Pausierte Quellen“ listet Hosts mit Ruhezeit und die
  Zahl zurückgestellter Artikel. Der Textabruf-Rückstau zählt **alle** noch nicht abgerufenen Artikel der letzten 3 Tage
  (nicht nur „heute“ importierte); die Analyse wartet nicht auf den Text (nach 6 h wird mit dem Anreißer analysiert).
* **Fortschrittsbalken (0.47.3):** unter „Pipeline heute“ je ein Balken für Artikeltexte (3 Tage) und KI-Analysen (48 h):
  erledigt / (erledigt + wartend). Beim Abruf zählt der Balken (ab 0.54.2) nur **jetzt Abrufbares** (erledigt / erledigt + abrufbar);
  Artikel pausierter Quellen und geplante Wiederholungen stehen in der Zeile „Zurückgestellt“; Meldungen, die aus der Auswahl fallen (ausgeblendet, älter als das Fenster), zählen nicht mit.
  Ohne Arbeit steht der Balken auf 100 %. Näherung: Neue Meldungen senken den Wert wieder.
* **Restzeit (0.47.4):** unter jedem Balken „Restzeit: ca. …“ (`app/news/eta.py`). Rechnung: wartende Einheiten ÷ (Durchsatz −
  Zufluss neuer Meldungen), aufgerundet auf ganze Lauf-Intervalle (Abruf 15 min, Analyse 10 min). Durchsatz = tatsächlich erledigt in
  den letzten 3 h (ab 12 Einheiten, „gemessen“), sonst Nennleistung je Lauf („Nennleistung“: 40 Texte je Lauf; Analyse
  Anfragen/min × 8 min × 6 Meldungen, höchstens `news.analysis_max_per_run`). Zusätzlich: Anbieter-Pause („bis …, danach“),
  Gemini-Tageskontingent („reicht für ca. N Meldungen, Rest ab 09:00“; Tagesgrenze = Mitternacht Pacific) und „wächst derzeit“, wenn mehr
  eintrifft als abgearbeitet wird. Grenzen: Schätzung; Tokens/min, Netz, Quellensperren und Nachtruhe der Quellen sind nicht
  modelliert; direkt nach einer Änderung wirkt der Messwert der letzten 3 h noch nach.
* **Längere Texte (Prompt-Version 2):** Zusammenfassung 4–5 Sätze; Einordnung 3–5 Sätze (Relevanz, mögliche Folgen,
  Beobachtungspunkte, als Einschätzung formuliert); Bericht: Überblick 6–8 Sätze, Kernaussage 2–3, Einordnung 2–3,
  Krypto 4–6. Da die Version im Eingabe-Hash steckt, werden **nur Meldungen der letzten 48 h** neu analysiert; ältere
  Analysen bleiben unverändert (Kosten: etwa 1,5× Ausgabe-Tokens).
* **Stimmungsbarometer:** im Bericht die Verteilung positiv/neutral/negativ/gemischt über alle analysierten Meldungen des
  Berichtszeitraums, gesamt und je Portfolio-Asset – lokal aus den gespeicherten Analysen berechnet, Modell-Einschätzung
  (nicht gewichtet, keine Messung).

## Telegram-Kanäle (0.48)

Quelltyp `telegram` in `sources.yaml` (technisch ein RSS/Atom-Feed, z. B. RSS-Bridge `TelegramBridge` oder RSSHub auf dem
eigenen Server):

* **In der App hinzufügen (0.48.1):** *News → Quellen → Telegram-Kanal hinzufügen*. Eingabe: Kanal (`@Name`, `Name`, `t.me/Name`,
  `t.me/s/Name` oder eine fertige Feed-URL) und die Adresse der RSS-Bridge (wird für die nächsten Kanäle gemerkt, `news.telegram_bridge`).
  Aus Kanalname + Bridge-Adresse wird `…/?action=display&bridge=TelegramBridge&username=%40Name&format=Atom` gebildet. Vor dem
  Speichern wird der Feed einmal abgerufen und gelesen (nicht erreichbar, HTTP-Fehler, nicht lesbar, keine Beiträge → Meldung, nichts
  wird gespeichert); Duplikate (gleiche ID oder URL) werden abgelehnt. Eintrag in `sources.yaml` ohne Änderung der Kommentare und
  übrigen Quellen; Abruf startet sofort. Je Quelle: „KI an/aus“ (`ai:`), „Deaktivieren“, „Entfernen“ (nur Telegram-Quellen;
  gespeicherte Beiträge bleiben). Grenzen: nur RSS-Bridge-Format (Atom) wird aus dem Kanalnamen gebildet, RSSHub o. ä. per fertiger
  Feed-URL; Bridge-Adressen sind frei wählbar (auch LAN), das ist beabsichtigt – die App ist nur mit Anmeldung erreichbar.
* **Reiter „Community“ (0.48.2):** *News → Community* zeigt nur die Beiträge der Telegram-Quellen, neueste zuerst, mit vollem Text
  (nicht auf 3 Zeilen gekürzt), Bild und Link „Telegram ↗“; Filter nach Kanal, Position, Zeitraum, Status. „Alle als gelesen“ gilt dort
  nur für die Community-Quellen. Die Beiträge bleiben zusätzlich im allgemeinen Artikel-Feed. Inaktive oder entfernte Kanäle: vorhandene
  Beiträge eines noch eingetragenen (auch inaktiven) Kanals bleiben sichtbar, nach „Entfernen“ nur noch im Artikel-Feed.
* **Titel:** Beiträge ohne oder mit abgeschnittenem Feedtitel (`…`, < 8 Zeichen) bekommen den ersten Satz des Textes (höchstens
  110 Zeichen); Beiträge ganz ohne Text werden verworfen. Der volle Beitragstext (bis 2000 Zeichen) bleibt als Text erhalten.
* **Ohne KI:** `ai: false` ist bei Telegram der Standard (überall setzbar, `ai: true` schaltet es wieder ein). Solche Quellen
  werden nicht analysiert, nicht in Ereignisse gruppiert und kommen nicht in den Tagesbericht; sie zählen nicht zum
  KI-Rückstau. Auf der Startseite erscheinen sie in der Ansicht „Original“ und – bei KI-Ausfall – in der Aktualitätsliste mit
  dem Hinweis „ohne KI“ (nie als „KI ausstehend“).
* **Bilder:** Das Bild des Beitrags wird beim Abruf sofort geholt und lokal gecacht (die Telegram-CDN-Links sind signiert und
  laufen ab). Neben öffentlichen Hosts ist ausdrücklich der **Host der Quelle** erlaubt (eigene Bridge im LAN, beliebiger Port);
  alles andere bleibt SSRF-geschützt (nur öffentliche Hosts, Port 80/443). Nach drei Fehlern in Folge pausiert der Bildabruf des
  Laufs. Bild aus `enclosure`, `media:*` oder dem ersten `<img>` im Beitrag; CSS-Hintergrundbilder werden von `feedparser`
  beim Bereinigen entfernt und deshalb nicht erkannt.
* **Filter:** `require_match: true` (Standard) behält nur Beiträge mit Bezug zu einer Position; `title_blocklist` und
  Dubletten-Erkennung gelten wie bei anderen Quellen. Empfohlen: `weight` um 0,4 (Meinung/Werbung, ungeprüft).
* **Grenzen:** nicht gegen eine echte RSS-Bridge-Instanz getestet (Tests mit synthetischem Atom nach dem üblichen Format);
  Albums/Mehrfachbilder: nur das erste Bild; Videos/Sprachnachrichten ohne Text werden verworfen; private Kanäle gehen nur,
  wenn die Bridge sie liefert. Kein Telegram-Konto in der App.

## Tests

`tests/test_news_intel.py` (≈ 100 Fälle, ohne Netzwerk/KI): Migration, SSRF (private/gemischte Adressen,
Rebinding-Pinning, Weiterleitungen, Größe/Typ/Status/Timeout), Extraktion (Boilerplate, JSON-LD, Bezahl-/
Zustimmungsschranke, kurze/kaputte Seiten), Google-News-Auflösung, Anreicherung (Status, Retry-Backoff, Priorität
inkl. Marktmeldungen, Host-Ruhe), Gemini (Anfrageform, Schlüssel nur im Header, Nutzung, Tages-/Minutenkontingent,
Fehlerabbildung ohne Schlüssel, lokale Limits, Modellliste), Claude-Pfad, Analyse (Validierung, Idempotenz, Hash,
Teaser-Fallback, Fehler/Quota, Injection, Datenschutz mit echtem Beispielimport), Ereignisse (Gruppierung,
Zahlenkonflikt, KI-Grenzfall, Korrektur), Tagesbericht (Belegprüfung, Idempotenz, Versionen, Fehler), Oberfläche
(Reader, Fallbacks, Routen, Rücksprung, Status, Einstellungen), Jobs.

## Bekannte Grenzen

* **Nicht gegen echte Gegenstellen getestet** (Sandbox ohne Zugriff): Google-Gemini-Antworten, Google-News-
  `batchexecute`, reale Verlagsseiten. Die Formate folgen der Dokumentation bzw. dem bekannten Verhalten; Abweichungen
  zeigen sich als `failed`/`gnews_unresolved`/`AiRejected` in *News → Status*, nie als Absturz.
* Die Extraktion ist heuristisch; für Seiten, die den Text per JavaScript nachladen, liefert sie `failed` oder nur den
  Anriss (kein Headless-Browser).
* Kein Wechsel zu anderen KI-Modellen bei Ausfall (bewusst); kein Mehrbenutzerbetrieb der Pausenlogik (Zustand je Instanz).
* Free-Tier-Zahlen sind Annahmen (siehe oben); die lokale Zählung ist kein Abrechnungsnachweis.
