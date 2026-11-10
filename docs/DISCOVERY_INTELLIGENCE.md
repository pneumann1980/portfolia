# Discovery & Intelligence (0.112.0 – „Steckbrief, KI per Knopf, Vorwärtstest“; Modell je Aufgabe)

**Neu in 0.111** (Abschnitt [0.7](#07-neu-in-0111-modell-je-aufgabe-und-warteschlange)): eigenes Gemini-Modell für
KI-Discovery und Firmenanalyse, sichtbare **Warteschlange** bei erschöpftem Kontingent (abbrechbar, startet von
selbst), Wiederverwendung gültiger KI-Teile, höchstens 3 Aufrufe je Firmenanalyse.

**Neu in 0.110** (Abschnitt [0](#0-neu-in-0110-und-begründung)): KI-Analysen **nur per Knopf** und mit **einem**
Aufruf statt drei; die KI-Discovery schlägt Unternehmen mit **Vorschau** (Kurzbeschreibung, Disruption-Einschätzung,
Chance/Nachfrage) vor und vertieft nichts automatisch; der **Portfolia-Score** (die bisherige „Tom Nash 125 – Public
Reconstruction v1“, gleiche Berechnung) zeigt Datenteil (65 Punkte, ohne KI) und qualitativen Teil (60 Punkte, nur
mit KI) getrennt; „Portfolia Fundamental v1“ wird nicht mehr angezeigt; die Unternehmensseite ist ein **Steckbrief**
auf einem Bildschirm mit eingeklappten Details; Hinweise zu **Übernahmen** (SEC-Formulare), **Sondereffekten**
(bereinigtes KGV) und Datenlücken stehen oben; Wachstum aus **vergleichbaren Werten derselben SEC-Einreichung**
(umgestellte Vorjahre); gemeinsame Prioritätsskala 0–100; **Vorwärtstest** der Vorschläge.

**Neu in 0.109:** eigenständige Scorecard nach der 125-Punkte-Methode (seit 0.110 „Portfolia-Score“), vollständig
sichtbarer **KI-Researchstatus**, persistente **Research-Jobs** mit Knöpfen und Fortschritt, gezieltes **Nachholen
fehlender KI-Teile**, eine **Discovery Engine** mit drei Suchwegen unabhängig von der News-Pipeline, klassisches PEG,
Bewertungsprofile, Gesamtrendite gegenüber dem S&P 500, Export/Wiederherstellung der Discovery-Daten.

Research-Modul: Es entdeckt börsennotierte Unternehmen, bewertet sie in getrennten, vollständig aufschlüsselbaren
Scorecards und prüft den Fit zum eigenen Portfolio lokal. Es ist **kein** Kaufsignal-Generator. Es erzeugt keine
Buchung und keine Order und verändert weder Ledger noch Steuer- oder Buchungsprüfungsdaten.

## 0. Neu in 0.110 und Begründung

### 0.1 Historische Auswertung: Was freie Kennzahlen leisten (und was nicht)

Vor der Umsetzung wurde geprüft, ob sich spätere Vervielfacher allein aus kostenlosen SEC-Kennzahlen früh erkennen
lassen. Aufbau: alle heute gelisteten US-Aktien (Nasdaq/NYSE/CBOE) mit ≥ 20 Mio. USD Umsatz und ≥ 50 Mio. USD
Börsenwert, ca. 16.000 Firma-Jahre; SEC-Frames 2015–2024 (Geschäftsjahre bis Februar des Folgejahres abgeschlossen),
Bildung jeweils Ende Mai 2018–2025 nur mit damals veröffentlichten Daten, Monatskurse (bereinigt) und Vergleich mit
SPY nach 12/24/36 Monaten.

| Rangliste (24 Monate) | n | Verdoppler | Halbiert | Faktor ggü. allen |
|---|---|---|---|---|
| Alle (Basis) | 13.878 | 11,6 % | 13,5 % | 1,00 |
| Screening wie in der App (≥ 4 Punkte) | 2.451 | 11,8 % | 19,1 % | 1,01 |
| Höchstes Umsatzwachstum (obere 10 %) | 1.390 | 12,7 % | 27,9 % | 1,09 |
| Rule of 40 (obere 10 %) | 1.040 | 9,7 % | 19,9 % | 0,83 |
| Kombination Fundamentaldaten (obere 10 %) | 1.390 | 9,9 % | 20,6 % | 0,85 |
| Kursmomentum 12-1 (obere 10 %) | 1.355 | 14,5 % | 17,3 % | 1,24 |
| Frühphase + Momentum (Top 25 je Jahr) | 175 | 18,3 % | 24,0 % | 1,57 |

Von 694 späteren Verdreifachern (36 Monate) war das Screening bei 18,2 % beteiligt – bei 17,9 % Anteil am Universum
(kein Vorsprung). Momentum: 11,5 % bei 9,8 % Anteil, aber diese Aktien waren im Vorjahr im Median bereits um 84 %
gestiegen. Fallbeispiele: Palantir (Mai 2022, Screening 5 Punkte) und Upstart (Mai 2022, 7 Punkte) sahen in den Zahlen
gleich aus (+1.748 % bzw. −57 % seither); Vertiv, Lumentum und Nvidia 2023 zeigten **vor** der KI-Welle kein Signal.

**Folgerungen für die Produktgestaltung:** (1) Jahreskennzahlen bestätigen eine Wende, sie sagen sie nicht voraus;
eine Rangliste aus freien Kennzahlen täuscht Qualität vor, die sie nicht hat. (2) Wachstumsfilter erhöhen vor allem
das Risiko (mehr Halbierungen). (3) Der Weg „vor der Welle“ führt über Themen und Wertschöpfungsketten – das ist die
Aufgabe der KI-Discovery; die SEC-Daten prüfen und warnen. (4) Ob die KI-Discovery besser als der Zufall ist, lässt
sich **nur vorwärts** messen (ein Sprachmodell kennt vergangene Kursverläufe) – daher der Vorwärtstest (0.5).

Grenzen der Auswertung: nur heute gelistete Unternehmen (Pleiten fehlen; die Ergebnisse der wachstumsstarken Gruppen
sind dadurch eher zu gut), nur Jahreszahlen, ein Marktabschnitt (2018–2025) mit stark schwankenden Jahrgängen,
Geschäftsjahre mit Ende nach Februar teils ausgelassen (kein Vorgriff), ohne Kosten und Steuern. Skripte liegen nicht
im Repository (einmalige Analyse); die Methodik ist hier vollständig beschrieben.

### 0.2 KI nur per Knopf, ein Aufruf

* **Automatisch** (Zeitplan, Datenanalysen neuer Kandidaten, beobachtete Unternehmen): nur SEC- und Marktdaten,
  **nie KI**. Einzige automatische KI: die KI-Discovery (wöchentlich, genau **2 Aufrufe**: Themen, Thesen mit
  Vorschau); sie vertieft nichts automatisch.
* **Knöpfe je Unternehmen:** „Mit KI analysieren · 1 Aufruf“ (Steckbrief, These, Gegenhypothese, Disruption-Kriterien,
  qualitative Score-Kriterien in einer Antwort; jeder Teil wird einzeln geprüft und gespeichert), „KI-Analyse
  aktualisieren · 1 Aufruf“ (Daten neu laden + 1 Aufruf), „Fehlende KI-Teile ergänzen · n Aufrufe“ (nur sichtbar, wenn
  ein Teil ungültig ist), „Daten aktualisieren“ (kostenlos). Ohne KI bzw. ohne Kontingent ist der Knopf gesperrt und
  nennt den Grund; es entsteht kein Job.
* **Veraltet** ist ein KI-Bericht nur, wenn seit seiner Erstellung ein neuer Quartals- oder Jahresbericht bei der SEC
  eingegangen ist (Einreichungsliste bzw. EDGAR-Tagesindex) – nicht bei neuen Meldungen oder Kursen. Er wird dann
  markiert, aber nicht automatisch neu erstellt.
* Textlängen im Prompt begrenzt (Geschäftsmodell 1–2 Sätze, These/Gegenhypothese höchstens 3 Sätze, Pro/Contra
  höchstens 15 Wörter, Kriterien 1–2 Sätze). Prompt-Version 3; Teilversionen 3. Ältere Berichte bleiben gültig.

### 0.2a Automatische Datenaktualisierung (SEC, Kurse – ohne KI)

Der Zeitplan-Schritt „Datenaktualisierung“ (Standard täglich) frischt Kandidaten mit Status „neu“ oder
„beobachten“ in fester Reihenfolge auf: (1) **neuer Quartals- oder Jahresbericht** bei der SEC seit der letzten
Analyse (erkannt über den EDGAR-Tagesindex des Screenings bzw. die Einreichungsliste) – noch am selben Tag;
(2) Kandidaten **ohne Analyse**; (3) alle übrigen, deren Analyse älter ist als der gewählte Abstand
(**wöchentlich** oder **monatlich**, Einstellung `discovery.refresh_days` = 7 bzw. 30). Höchstens
`discovery.auto_analyse_per_run` Firmen je Lauf (Standard 25, einstellbar bis 100); der Rest folgt im nächsten
Lauf (Anzahl im Job-Ergebnis). Je Firma 2–3 SEC-Abrufe (zwischengespeichert, höchstens 2 Anfragen/s), keine Kosten.
Ein Fehler bei einer Firma bricht den Lauf nicht ab (Job „teilweise“, Grund je Firma). Ein vorhandener KI-Bericht
wird bei neuem SEC-Bericht als „veraltet“ markiert, nie automatisch neu erstellt.

### 0.3 Portfolia-Score

Gleiche Berechnung wie „Tom Nash 125 – Public Reconstruction v1“ (Abschnitt 9), neuer Name; die Herkunft der Methode
steht in den Details. Anzeige getrennt: **Datenteil** (7 Kennzahl-Kriterien, 65 Punkte, immer ohne KI berechnet) und
**qualitativer Teil** (6 Kriterien, 60 Punkte, nur nach KI-Analyse). Ohne KI-Analyse steht der qualitative Teil auf
„offen“ (nicht 0); fehlende Datenkriterien werden gezählt und markiert. „Portfolia Fundamental v1“ wird nicht mehr
angezeigt (Werte bleiben in älteren Versionen gespeichert, nichts wird umgerechnet). Quality, Growth/GARP und
Valuation erscheinen im Steckbrief als Balken; Disruption ist Teil der KI-Analyse (vorher: Vorschau-Einschätzung).

### 0.4 Steckbrief und Vorschau-Karten

* **Unternehmensseite:** oben ein Steckbrief (Name, Stand, Hinweise, Kurzbeschreibung, warum vorgeschlagen, ggf.
  KI-Vorschau, Portfolia-Score, Balken, drei Kennzahlen, je höchstens drei Punkte dafür/dagegen, Bewertung in einem
  Satz, größtes Risiko, Knöpfe mit Kosten). Alles Weitere eingeklappt: KI-Status, These/Gegenhypothese, 13 Kriterien,
  weitere Bewertungen, Fundamentaldaten, Bewertung/Szenarien, Portfolio-Fit, Signale/Herkunft/Meilensteine, Quellen,
  Historie. Ohne KI stammen Pro/Contra aus Regeln (Signale, Szenarien, Sondereffekte, Verwässerung).
* **Hinweise oben (ohne KI):** Übernahme/Fusion laut SEC-Formular (SC 14D9, SC TO-T, PREM14A, DEFM14A, 425 in den
  letzten 180 Tagen – belegt) bzw. laut Meldung (Begriffe, 90 Tage – „ungeprüft“, Rolle nicht bestimmt);
  wesentliche **Sondereffekte** des letzten Geschäftsjahres (Schuldentilgung, Wertminderungen, Restrukturierung,
  Veräußerungsergebnisse, aufgegebene Bereiche; wesentlich ab 5 % des Umsatzes oder 25 % des Nettoergebnisses) mit
  bereinigtem Nettoergebnis (vor Steuereffekt) und **bereinigtem KGV**; Datenlücken (< 70 % Kernkennzahlen, letzter
  Abschluss älter als 16 Monate, kein Kurs, mehrere Währungen).
* **Discovery-Liste:** Karten mit Stand (KI-Vorschau, mit KI analysiert, veraltet, nur Daten), Kurzbeschreibung,
  **Disruption (Vorschau)** – was verdrängt wird, womit, Reifegrad, Einschätzung gering/mittel/hoch (hoch nur mit
  wörtlich gefundener Belegstelle, sonst mittel) –, „Warum“, drei Kennzahlen, Portfolia-Score (Daten/qualitativ),
  wichtigste Hinweise, Knöpfe „Mit KI analysieren · 1 Aufruf“, „Öffnen“, „Beobachten“, „Verwerfen“. Kategorien nach
  Stand und Inhalt; Standard-Sortierung „Neueste Signale“.
* **Priorität:** gemeinsame Skala 0–100 (Screening: Punkte ÷ 14 × 100; Migration 43 rechnet gespeicherte Werte um).
  Nur zur Reihenfolge, kein Qualitätsurteil.

### 0.5 Vorwärtstest

Jede KI-Vorschau und jede KI-Analyse wird mit Stichtag, Kurs, SPY-Stand, Portfolia-Score und (Vorschau) Disruption-
Einschätzung gespeichert (`disc_forward`, je Unternehmen/Art/Tag einmal; im Gesamtexport enthalten). Die
Discovery-Seite zeigt je Art den Zwischenstand und – sobald erreicht – Median der Mehrrendite gegenüber dem S&P 500 und
Anteil besser nach 12 und 24 Monaten, bei Vorschauen zusätzlich nach Einschätzung. Kurse werden wöchentlich (Job
„beobachtet“, ohne KI) aktualisiert. Grenzen: Kursrendite ohne Dividenden, nicht mehr notierte Aktien werden als
„ohne Kurs“ gezählt, vor 12 Monaten nur Zwischenstand.

### 0.6 Datenkorrekturen

* **Umgestellte Vorjahre:** Wachstum wird aus Werten **derselben Einreichung** berechnet (jüngste, die beide Jahre
  enthält); die Tabelle zeigt „umgest.“. Beispiel AppLovin: Der 10-K 2026 weist 2023/2024 ohne den verkauften
  App-Bereich aus – der frühere Vergleich „neuer Wert ÷ alter Vorjahreswert“ ergab einen Schein-Rückgang (−35 %). Das
  Screening nutzt dieselben vergleichbaren Werte.
* **Neuer SEC-Bericht erzeugt eine neue Analyseversion** (auch ohne geänderte Kennzahlen), damit „veraltet“ sichtbar
  wird.

### 0.7 Neu in 0.111: Modell je Aufgabe und Warteschlange

* **Modelle:** KI-Discovery (Themen, Thesen) nutzt die Gateway-Aufgabe `discovery`, Firmenanalysen (gemeinsamer Aufruf
  und Nachholen je Teil) die Aufgabe `company` – im Profil „Free Tier“ Flash-Lite bzw. `gemini-3.8-flash`
  (Einzelheiten: [News-Intelligence → KI-Modell je Aufgabe](NEWS_INTELLIGENCE.md#ki-modell-je-aufgabe-0111)).
* **Bis zu 3 Aufrufe je Firmenanalyse:** gültige, aktuelle KI-Teile (kein neuer SEC-Bericht seit Erstellung) werden
  wiederverwendet; ab zwei fehlenden Teilen ein gemeinsamer Aufruf, ein ungültiger Teil wird danach einzeln
  nachgeholt (höchstens 3 Aufrufe). Sind alle Teile aktuell, baut der Knopf bewusst alle neu auf (1 Aufruf).
* **Warteschlange statt Sperre:** Reicht das Kontingent nicht (Minuten-/Tageslimit oder Pause des Modells, Rücklage,
  Discovery-Tages-/Monatsgrenze), legt ein Knopf den Job trotzdem an: Zustand **„wartet“** mit Grund und Zeitpunkt
  (`disc_job.wait_until`, `wait_reason`), sichtbar im KI-Researchstatus (Warteschlange) und auf der
  Unternehmensseite, jederzeit **abbrechbar**. Minutenlimit → Start nach rund einer Minute; Tageslimit → nach dem
  Zurücksetzen (Google: Mitternacht Pazifikzeit, Discovery: Mitternacht lokal, Monatsgrenze: Monatsanfang). Ein
  minütlicher Lauf (`discovery_wake`) und jeder Seitenaufruf starten fällige Jobs – manuelle zuerst. Wartende Jobs
  überstehen einen Neustart und belegen ihren Slot (kein Doppelstart). Nur „KI aus/kein Schlüssel“ sperrt den Knopf
  sofort. Zeitgesteuerte Läufe warten nicht (Zustand „Kontingent erschöpft“, nächster Lauf versucht es erneut).
* **Vorrang:** manuell gestartete Jobs rufen die KI mit Vorrang auf (Rücklage, Minutentakt).

### 0.8 Neu in 0.112

* **Ungültiges JSON der KI-Discovery:** Kleine Modelle schließen verschachtelte Listen gelegentlich falsch (`}` statt
  `]}`) oder vergessen die letzte Klammer – Fehler wie „kein gültiges JSON (an Zeichen 1356 von 1360)“. Die Auswertung
  repariert jetzt die Klammerstruktur (falsch geschlossen, überzählig, fehlend), ohne Inhalte zu ändern; Texte in
  Anführungszeichen bleiben unberührt. Was danach unvollständig ist, verwerfen die üblichen Prüfungen. Bleibt die
  Antwort unlesbar, nennt die Fehlermeldung zusätzlich das Ende der Antwort (öffentlicher Modelltext), damit die Ursache
  sichtbar wird. Kein zusätzlicher KI-Aufruf.
* **Position halten:** Knöpfe auf der Discovery-Liste und der Unternehmensseite laden die Seite nicht mehr „nach oben“:
  Die Ansicht bleibt an der Karte bzw. am Knopf, geöffnete Klappbereiche bleiben offen, Meldungen erscheinen unten als
  Hinweis. Später gezeichnete Teile (Diagramme, nachgeladene Abschnitte) werden bis zu 6 s ausgeglichen, solange man
  nicht selbst scrollt. Knöpfe, die bewusst auf eine andere Seite führen (z. B. „Mit KI analysieren“ → Unternehmensseite),
  beginnen dort oben.

## 1. Produktvision und Funktionsumfang

Fünf Stufen, jede mit eigenem Ergebnis:

| Stufe | Frage | Ergebnis |
|---|---|---|
| Discovery | Welche Unternehmen sind auffällig? | Kandidaten und Signale |
| Disruption | Wie groß ist das Potenzial des Geschäftsmodells? | Disruption Score (100) |
| Fundamental Validation | Was zeigen die Zahlen? | Quality (100), Growth/GARP (100), Tom Nash 125 (Public Reconstruction v1), Portfolia Fundamental v1 (125) |
| Valuation | Was ist eingepreist? | Multiplikatoren, 3 Szenarien, eingepreistes Wachstum, Valuation Score (100) |
| Portfolio Fit | Passt es zum eigenen Depot? | Kauf-Simulation, Portfolio-Fit (100), nur lokal |

Die Scores werden nie zu einem Gesamtwert verrechnet. Ein Unternehmen kann innovativ und zugleich riskant sein, stark
und zugleich teuer, günstig und zugleich konzentrationserhöhend.

Seiten:

- `/discovery`: Research starten (KI-Discovery, Fundamentalscreening, vollständiger Lauf), KI-Researchstatus,
  Research-Jobs, Suchfortschritt, Radar mit Kategorien, Sortierung, Suche, manuell aufnehmen, Einstellungen/Zeitplan.
- `POST /discovery/start/{ki_discovery|screening|voll|news}`: startet einen Hintergrund-Job (Dublettenschutz).
- `/discovery/status`, `/discovery/jobs/{id}`, `/discovery/c/{id}/status`: HTMX-Teilansichten, die sich selbst
  aktualisieren, solange Jobs laufen.
- `/discovery/protokoll?job=`: Ausführungsprotokoll (Jobs, Kandidaten, KI-Aufrufe ohne Prompt-Inhalt).
- `/discovery/c/{id}`: Unternehmensanalyse (stabiler Link). `?a=<Version>` zeigt eine frühere Version,
  `?betrag=` setzt den simulierten Kaufbetrag.
- `/discovery/t/{Ticker}`: stabiler Link je Ticker. Er wird aus Watchlist und Positionsdetail verlinkt.
- `/discovery/c/{id}/vergleich?a=&b=`: Vergleich zweier Analyseversionen.
- `/discovery/export.json`: Export aller Discovery-Nutzerdaten.

Aktionen:

- Unternehmen: „KI-Analyse starten“ (alle drei KI-Teile neu), „Fehlende KI-Teile ergänzen“ (nur fehlende/ungültige
  Teile), „Fundamentaldaten aktualisieren“ (SEC und Kurse neu laden, keine KI), „Vollanalyse aktualisieren“ (Daten neu,
  veraltete und fehlende KI-Teile neu), „Analysehistorie anzeigen“. Jeder Knopf startet einen Job; ein zweiter Klick
  startet keinen zweiten.
- Zur Watchlist hinzufügen.
- Beobachten, Zurückstellen, Verwerfen, Status zurücksetzen.
- Kaufbetrag simulieren.
- Versionen vergleichen.

## 2. Architektur

`app/discovery/` als eigenes Modul (Registrierung über `register_router` und `extra_jobs`):

| Datei | Aufgabe |
|---|---|
| `sec.py` | SEC-EDGAR-Client: Ticker-Verzeichnis, `submissions`, `companyfacts`, „Item 1. Business“; feste Hosts, Rate-Limit, Plattencache |
| `identity.py` | Ticker/Name → Emittent (CIK); Mehrdeutigkeit, Mehrfachlistings, SPAC-Erkennung |
| `fundamentals.py` | XBRL → Jahreswerte mit Herkunft, Korrekturen, Split-Bereinigung, Quartal, `as_of` |
| `scoring.py` | Kriterien-Rahmen, Disruption, Quality, Growth/GARP, Nash-inspiriert |
| `valuation.py` | Multiplikatoren, Szenarien, eingepreistes Wachstum, Valuation Score |
| `fit.py` | Portfolio-Fit und Kauf-Simulation (lokal) |
| `ai.py` | KI-Analyse und Themen über das vorhandene `AiGateway`; Validierung; Discovery-Budget |
| `signals.py` | Signale aus News-Analyse, KI-Themen, Fundamentaldaten |
| `service.py` | Ablauf: aufnehmen, Discovery-Lauf, Analyse (historisiert), Vergleich, Radar |
| `nash.py` | Tom Nash 125 – Public Reconstruction v1 (13 Kriterien, Belegstand je Regel, Einordnung) |
| `jobs.py` | persistente Research-Jobs (Dublettenschutz, Fortschritt, Neustart, Zeitüberschreitung) |
| `universe.py` | Research-Universum, EDGAR-Tagesindex, SEC-Frames-Vorauswahl, Bestätigung, Fehlbewertung |
| `engine.py` | Job-Abläufe, mehrstufige KI-Discovery, Zeitplan, KI-Researchstatus |
| `backup.py` | Discovery-Daten im Gesamtexport und ihre Wiederherstellung |
| `web.py`, `charts.py`, `text.py` | Routen, Inline-SVG-Balken, deutsche Zahlenformate |

Wiederverwendet werden:

- Kursablage und Yahoo-Anbindung (`market_data`, `store.info`, `_backfill_series`);
- `AiGateway` (Anbieter, Tagesbudget, Pausen, Nutzungsprotokoll) und die Prüfhelfer der News-Analyse;
- `news_analysis`, `news_event` und `news_event_item` als Discovery-Quelle;
- `WatchlistService`, `valuation()`, `history_with_live()`, der Scheduler und das Design (CSS-Variablen, Karten,
  Badges, `seg`, `kv`).

Datenmodell (Migration 41), alle Tabellen mit Präfix `disc_`:

| Tabelle | Inhalt |
|---|---|
| `disc_company` | Identität (CIK, Ticker, Börse, Name, Branche, Land, SIC), Status, Herkunft |
| `disc_alias` | weitere Ticker, frühere Namen (Dubletten, Umbenennungen) |
| `disc_signal` | Discovery-Signale, `fp` UNIQUE = Unternehmen + Ereignis |
| `disc_fact` | berichtete XBRL-Werte je Kennzahl/Periode/Accession (Reproduzierbarkeit, Korrekturen) |
| `disc_analysis` | Analyseversionen: Scores, Abdeckung, Konfidenz, Anbieter/Modell, Prompt-/Berechnungsversion, Eingabe-Hash, vollständiger Bericht |
| `disc_milestone` | überprüfbare Meilensteine mit Status |
| `disc_ai_usage` | Discovery-KI-Aufrufe je Tag |
| `disc_run` | Discovery-Läufe mit Statistik |

Migration 42 (0.109) ergänzt:

| Tabelle/Spalte | Inhalt |
|---|---|
| `disc_analysis.nash125`, `.fundamental_v1`, `.ai_state`, `.parts_json`, `.job_id` | Tom Nash 125, Portfolia Fundamental v1 (= bisherige Spalte `nash`, nur benannt), KI-Zustand der Version, Zustand je KI-Teil, auslösender Job |
| `disc_job` | Research-Jobs: Art, Auslöser, Slot (eindeutig solange aktiv), Status, Schritt, Fortschritt, Ergebnis, Fehlerklasse/-text, Prozesskennung, Zeiten |
| `disc_ai_call` | jeder KI-Aufruf: Job, Unternehmen, Arbeitsschritt, Anbieter, Modell, Prompt-Version, Eingabe-Fingerabdruck, Status, Tokens, Kosten (nur wenn verlässlich), Fehler |
| `disc_ai_part` | KI-Teilanalysen je Unternehmen (bericht, disruption, nash) mit Status, Fingerabdruck, Ergebnis |
| `disc_universe` | Research-Universum (CIK, Ticker, Börse, Größenklasse, jüngste Einreichung, Vorauswahl, Zustand, Begründung) |
| `disc_candidate` | Kandidaten je Suchlauf und Suchweg (neu/bekannt/verworfen) mit Begründung, Thema, Priorität, Belegen |

Die Tabellen gehören **nicht** zu den Revisionstabellen (keine Trigger). Discovery-Schreibvorgänge ändern weder
den Diagnose-Datenstand noch die Portfolio-Zwischenspeicher; ein Test prüft das. Kurse und Stammdaten werden nicht
doppelt gespeichert, sondern liegen in der gemeinsamen Kursablage (Reihe `yahoo:<Ticker>`).

## 3. Datenquellen

| Quelle | Inhalt | Qualität / Aktualität | Bedingungen, Grenzen, Kosten |
|---|---|---|---|
| SEC EDGAR `company_tickers_exchange.json` | Ticker, CIK, Name, Börse (≈ 10.400 Emittenten) | amtlich, täglich | gemeinfrei, kostenlos; Fair Access: ≤ 10 Anfragen/s, User-Agent mit Kontakt |
| SEC `submissions` | SIC, Adresse, frühere Namen, Einreichungen | amtlich | wie oben; Cache 1 Tag |
| SEC XBRL `companyfacts` | Jahres- und Quartalswerte mit Einreichungsdatum und Accession | amtlich, Restatements sichtbar | wie oben; nur US-registrierte Emittenten (10-K/10-Q, 20-F/40-F) |
| SEC Geschäftsbericht (10-K/20-F) | „Item 1. Business“ als Text | amtlich | wie oben; Cache 30 Tage; nur Abschnitt, gekürzt |
| Yahoo Finance (vorhandene Anbindung) | Kurs, Marktkapitalisierung, Forward-KGV, institutioneller Anteil, Short Interest, Sektor, Profil | inoffizielle Schnittstelle, nicht garantiert | nur Ticker werden übertragen, Rate-Limit 1/s, Info höchstens wöchentlich |
| Portfolia-News-Pipeline | analysierte Meldungen, Ereignisse | wie News-Analyse | keine zusätzlichen Abrufe |
| SEC XBRL `frames` | ein Wert je Emittent, Element und Kalenderjahr (Vorauswahl über das ganze Universum mit ≈ 15 Anfragen) | amtlich; Geschäftsjahre werden dem nächstliegenden Kalenderjahr zugeordnet (Näherung) | wie oben; Cache 7 Tage |
| EDGAR-Tagesindex (`daily-index/form.YYYYMMDD.idx`) | neue 10-K/10-Q/20-F/40-F je Tag | amtlich | wie oben; Cache 60 Tage |
| Yahoo Finance, bereinigte Tageskurse (`auto_adjust`) | Gesamtrendite (Splits, Dividenden) der Aktie und des S&P 500 Total Return Index (^SP500TR, ersatzweise SPY) | inoffizielle Schnittstelle | nur Ticker; je Tag und Symbol einmal |
| Manuelle Eingabe | Ticker oder Name | – | – |

Bewusst nicht verwendet werden kostenpflichtige Datenanbieter, Scraping von Investor-Relations-Seiten oder
Paywall-Umgehung. Nicht-US-Emittenten ohne SEC-Registrierung haben keine Fundamentaldaten. Sie bleiben „fehlt“ und
werden nicht geschätzt.

## 4. Discovery-Methodik

Drei eigenständige Suchwege plus Nachrichten. Jeder Weg ist ein Job mit eigenem Zeitplan (`discovery.auto.<schritt>`:
0 = aus, 24 = täglich, 168 = wöchentlich) und kann per Knopf gestartet werden.

**A. Technology & Business Model Discovery (KI, Job `ki_discovery`)** – mehrstufig, zwei KI-Aufrufe plus Vertiefung:

1. *Ausgangsbelege* (deterministisch): bis zu 25 Emittenten mit den stärksten fundamentalen Wendepunkten aus dem
   Universum (IDs `B1…`, gemischt nach Größenklassen) und – wenn `discovery.themes` an ist – wichtige Meldungen (`E…`).
2. *Themen und Wertschöpfungsketten* (KI-Aufruf „themen“): höchstens 4 Themen; Fakten (nur mit Beleg-IDs) strikt
   getrennt von der Hypothese; je Wertschöpfungsstufe (direkt, Zulieferer, Infrastruktur, Software,
   Schlüsseltechnologie) Unternehmen mit Name und Ticker; Thema ohne gültigen Beleg wird verworfen.
3. *Identität* (deterministisch): jeder Vorschlag gegen das SEC-Verzeichnis – Ticker und Name müssen zusammenpassen;
   private, nicht registrierte, nur außerbörslich (OTC) gehandelte und SPACs werden mit Grund verworfen; Dubletten
   zusammengeführt.
4. *Discovery-These* (KI-Aufruf „thesen“, bis `discovery.ki_companies` Unternehmen): Besonderheit, Erlösmodell,
   belegte Nachfrage, langfristige Chance, Vorteile, Widerlegung – Belege als wörtliche Zitate aus dem 10-K-Auszug,
   maschinell geprüft; ohne gefundenes Zitat höchstens Stufe 1.
5. *Discovery-Priorität* (nur Auswahl): KI-Stufen Chance/Besonderheit/Nachfrage je 0–2 × 7,5 (45) + Datenqualität
   (geprüfte Belegstelle 10, SEC-Umsatz 5) + Neuheit 15 + unabhängige Evidenz (Wendepunkt im Screening 15, mehrere
   Ausgangsbelege 10) = höchstens 100. Kein Ersatz für die Scores.
6. *Fundamentale Validierung*: Analyse im Modus „daten“ (alle Regelkriterien, keine KI).
7. *Vorschau statt Vertiefung* (seit 0.110): Die Thesen enthalten je Unternehmen eine Kurzbeschreibung und eine
   Disruption-Vorschau (verdrängt, womit, Reifegrad, gering/mittel/hoch); jede Vorschau geht in den Vorwärtstest. Eine
   vertiefte KI-Analyse startet nur der Nutzer per Knopf (1 Aufruf). Bis 0.109: automatische Vertiefung der besten
   Kandidaten mit 3 Aufrufen je Unternehmen.
8. *Speicherung*: Signal „KI-Discovery: Thema“, Kandidaten (neu / bereits bekannt / verworfen mit Grund), Themen samt
   Fakten, Hypothese und Gegenargument im Job-Ergebnis.

**B. Fundamental Inflection Discovery (ohne KI, Job `screening`)** – siehe Abschnitt 5a.

**C. Market Mispricing Discovery (ohne KI, Teil von `screening`)** – für bestätigte Wendepunkte: Rendite über 12
Monate gegenüber dem S&P 500 (bereinigte Kurse). Liegt die Aktie ≥ 15 Prozentpunkte zurück, wird auf Merkmale einer
Value Trap geprüft: Umsatz wächst nicht, FCF-Marge sinkt um mehr als 2 Pp., Verwässerung ≥ 10 % p. a., Nettoschulden
ohne positiven FCF. Ohne Merkmale → Signal „Mögliche Marktfehlbewertung“ (positiv); mit Merkmalen → Signal „Kursrückstand
– Merkmale einer Value Trap“ (negativ, keine Kategorie). Ein fallender Kurs allein ist nie ein Signal.

**Nachrichten-Discovery (Job `news`)** – wie bis 0.108: News-Nennungen → Identität → Dubletten → Signale
(Stärke `Bedeutung × min(1; 0,4 + 0,15 × min(Quellen; 4))`), höchstens `discovery.max_new_per_run` neue Unternehmen.

**Vollständiger Lauf (Job `voll`)**: Nachrichten → Screening → KI-Discovery; jeder Teil mit eigenen Grenzen, ein
Fehler bricht die anderen nicht ab.

Kategorien im Radar (offengelegte Regeln):

| Kategorie | Regel |
|---|---|
| Neue Entdeckungen | von einem Suchweg (nicht manuell) in den letzten 14 Tagen aufgenommen, Status „neu“ |
| Disruptive Geschäftsmodelle | Disruption vollständig bewertet und ≥ 55 |
| Fundamentale Wendepunkte | Signal „wendepunkt“ oder „fundamental“ (nicht negativ) in den letzten 180 Tagen |
| Mögliche Marktfehlbewertungen | Signal „fehlbewertung“ mit positiver Richtung (keine Value-Trap-Merkmale) |
| Quality / Growth/GARP | vollständig bewertet und ≥ 70 bzw. ≥ 65 |
| Nash 100+ | Tom Nash 125 **gesichert** ≥ 100 (fehlende Kriterien könnten nur Punkte hinzufügen) |
| Zu beobachten / Bereits im Portfolio | Status „beobachten“ / gehaltene Position mit gleichem Ticker oder Namen |

Sortierung: nach jedem Score (vollständige Bewertungen vor unvollständigen), Portfolio-Fit, Aktualität oder
Discovery-Priorität. Keine Favoriten, keine Bevorzugung nach Bekanntheit.

## 5. Identifikation neuer Unternehmen

- **Ticker:** exakter Treffer im SEC-Verzeichnis (`BRK.B` = `BRK-B`). Passt ein mitgelieferter Name nicht zum
  Ticker, wird das Ergebnis als mehrdeutig verworfen (z. B. falsche KI-Ticker).
- **Name:** Normalisiert wird auf Kleinbuchstaben, ohne Satzzeichen und ohne Rechtsform (Inc, Corp, AG, plc, Holdings,
  Class A …). Gültig nur bei genau einem Treffer, exakt oder als Wortanfang (mindestens 4 Zeichen). Mehrere Treffer
  gelten als mehrdeutig (z. B. „Meta“ → Meta Platforms und Meta Materials).
- **Mehrere Listings:** Ein CIK ist ein Unternehmen. Hauptticker ist der erste Eintrag einer Börse (Nasdaq/NYSE vor
  OTC); weitere Ticker werden Aliasse (GOOGL/GOOG).
- **Umbenennungen:** `formerNames` aus `submissions` werden als Alias gespeichert.
- **Private Firmen, Tochtergesellschaften:** nicht im Verzeichnis, also „nicht aufgelöst“. Tochtergesellschaften
  werden nicht automatisch der Mutter zugeordnet, weil sonst Fehlzuordnungen drohen.
- **SPACs:** SIC 6770 („Blank Checks“) wird abgelehnt.

## 5a. Research-Universum und Fundamentalscreening

1. **Universum** (`disc_universe`): alle Emittenten an Nasdaq, NYSE, CBOE und TXSE aus dem SEC-Verzeichnis, eine
   Zeile je CIK (Hauptticker); OTC und Emittenten ohne Börse bleiben außen vor; nicht mehr gelistete werden inaktiv.
   Abgleich höchstens wöchentlich.
2. **Neue Einreichungen**: EDGAR-Tagesindex der letzten 7 Tage (10-K, 10-Q, 20-F, 40-F, 10-K/A, 10-KT) → `filed_seen`.
   Ein bereits bestätigter oder verworfener Emittent wird nur nach einer neuen Einreichung erneut geprüft.
3. **Vorauswahl** (ganzes Universum): SEC-Frames für Umsatz (drei Elemente in Vorrangreihenfolge), operatives
   Ergebnis, operativen Cashflow, Investitionen und verwässerte Aktien der letzten drei Kalenderjahre. Merkmale (Punkte):
   Wachstumsbeschleunigung ≥ 15 % und +10 Pp. (3, +1 bei ≥ 30 %), Margenverbesserung ≥ 5 Pp. (2), Übergang zur
   Profitabilität (3), FCF von negativ auf positiv (2), operativer Hebel: Umsatz > 10 % und ≥ 5 Pp. schneller als die
   Gesamtkosten (2), FCF-Marge +10 Pp. (1); Gegenanzeichen: Umsatz −10 % (−3), Marge −10 Pp. (−2), Verwässerung ≥ 15 %
   (−2). Vorauswahl ab 4 Punkten und Umsatz ≥ `discovery.min_revenue_musd` (Standard 20 Mio. USD).
4. **Bestätigung** (je Lauf `discovery.screen_batch`, Standard 20): vollständige `companyfacts`, exakte
   Geschäftsjahre, dieselben Regeln; Auswahl abwechselnd je Größenklasse (groß ≥ 10 Mrd., mittel 1–10 Mrd., klein
   0,1–1 Mrd., Mikro), innerhalb nach Punkten und jüngster Einreichung. Bestätigt → Unternehmen, Signal „Fundamentaler
   Wendepunkt“, Kandidat; sonst verworfen mit Begründung. Der Fortschritt steht je Emittent in der Datenbank – ein
   abgebrochener Lauf setzt beim nächsten Mal fort.
5. **Fehlbewertung**: Abschnitt 4, Weg C.

Fortschritt auf `/discovery`: Größe des Universums, berechnete Vorauswahl, ausstehende Bestätigungen, geprüfte
Emittenten (bestätigt/verworfen), Kandidaten der letzten 30 Tage (je Unternehmen einmal), analysierte Unternehmen,
ausstehende KI-Analysen, Fehler – nur gezählte Werte.

## 6. Disruption Score (100 Punkte)

| Kriterium | Max | Art | Regel |
|---|---|---|---|
| Technologische Differenzierung | 12 | KI | Stufe 0–4 |
| Innovationspotenzial | 8 | KI | Stufe 0–4; F&E-Quote als Information |
| Marktgröße und -entwicklung | 12 | KI | Stufe 0–4 |
| Skalierbarkeit | 10 | Regel 5 + KI 5 | Regel: Bruttomarge ≥ 70 % → 5; ≥ 50 % → 3,5; ≥ 35 % → 2; ≥ 20 % → 1 |
| Wettbewerbsvorteile | 14 | KI | Stufe 0–4 |
| Netzwerkeffekte/Wechselkosten | 8 | KI | Stufe 0–4 |
| Nachgewiesene Nachfrage | 12 | Regel | Umsatzwachstum: ≥ 40 % → 12; ≥ 25 % → 9; ≥ 15 % → 6; ≥ 5 % → 3 |
| Wirtschaftliche Monetarisierung | 10 | Regel | Umsatz ≥ 100 Mio. 4 (≥ 10 Mio. 2) + Bruttomarge ≥ 50 % 3 (≥ 30 % 1,5) + FCF-Marge > 0 3 (steigend 1,5) |
| Fähigkeit zur Umsetzung | 8 | Regel 4 + KI 4 | Regel: Umsatz jedes Jahr gewachsen 2 + operative Marge nicht gesunken 2 |
| Geschäftsmodellrisiken | 6 | KI | hohe Stufe = geringe Risiken |

**KI-Stufen:**

- Punkte = Stufe / 4 × Maximum.
- Stufe > 1 nur mit mindestens einem gültigen Beleg (Quellen-ID aus der Eingabe), sonst Kappung auf 1.
- Stufe > 2 nur mit Konfidenz ≥ 0,5, sonst Kappung auf 2.
- Die Kappung steht in der Begründung.

## 7. Quality Score (100 Punkte)

| Kriterium | Max | Regel |
|---|---|---|
| Umsatzwachstum mehrjährig | 15 | 3-J.-CAGR (ersatzweise 2 J.): ≥ 20 % 15; ≥ 12 % 12; ≥ 7 % 9; ≥ 3 % 5; ≥ 0 % 2 |
| Operative Marge | 12 | ≥ 30 % 12; ≥ 20 % 10; ≥ 12 % 7; ≥ 5 % 4; ≥ 0 % 1 |
| Nachhaltigkeit der Margen | 8 | alle Jahre positiv und Standardabweichung ≤ 5 Pp. 8; ≤ 10 Pp. 5,5; höchstens ein Verlustjahr 3 (mindestens 3 Jahre) |
| Free Cashflow | 15 | Ø FCF-Marge bis 3 J.: ≥ 25 % 15; ≥ 15 % 12; ≥ 8 % 8; ≥ 3 % 4; ≥ 0 % 2 |
| Kapitalrendite (ROIC) | 15 | Ø bis 3 J.: ≥ 25 % 15; ≥ 15 % 11; ≥ 10 % 7; ≥ 5 % 3 |
| Verschuldung | 10 | Nettoschulden / FCF in Jahren: Nettoliquidität 10; ≤ 1 8; ≤ 2 6; ≤ 3 4; ≤ 5 2; Nettoschulden bei FCF ≤ 0: 0 |
| Liquidität | 5 | Current Ratio ≥ 2 5; ≥ 1,5 4; ≥ 1 2 |
| Kapitalallokation (Aktienanzahl) | 8 | Ø Veränderung: ≤ −1 % 8; ≤ 1 % 6; ≤ 3 % 4; ≤ 6 % 2 |
| Stabilität | 4 | Jahre mit Umsatzrückgang (bis 5): 0 → 4; 1 → 2,5; 2 → 1 |
| Wettbewerbsvorteile | 8 | KI-Stufe |

**ROIC:**

- Formel: NOPAT / (Eigenkapital + Finanzverbindlichkeiten), NOPAT = operatives Ergebnis × (1 − Steuersatz).
- Steuersatz = Ertragsteuern / Ergebnis vor Steuern, sofern 0–50 %. Sonst wird 21 % angenommen; der Wert gilt dann
  als „geschätzt“.
- Liquidität wird bewusst nicht abgezogen, weil die Kennzahl bei liquiditätsstarken Unternehmen sonst sinnlos wird.
  Dieses Gegenargument steht am Kriterium.

## 8. Growth/GARP Score (100 Punkte)

| Kriterium | Max | Regel |
|---|---|---|
| Aktuelles Umsatzwachstum | 20 | ≥ 40 % 20; ≥ 25 % 16; ≥ 15 % 12; ≥ 8 % 7; ≥ 3 % 3 |
| Wachstum 3 Jahre | 10 | CAGR ≥ 30 % 10; ≥ 20 % 8; ≥ 12 % 6; ≥ 6 % 3 |
| Bruttomarge | 10 | ≥ 70 % 10; ≥ 55 % 8; ≥ 40 % 5; ≥ 25 % 2 |
| Entwicklung der Bruttomarge | 5 | Δ 3 J.: ≥ +3 Pp. 5; ≥ 0 3; ≥ −3 Pp. 1 |
| Operative Skalierbarkeit | 10 | Δ operative Marge 3 J.: ≥ +10 Pp. 10; ≥ +5 7; ≥ +1 4; ≥ −2 2 |
| Entwicklung des Free Cashflows | 10 | FCF-Marge ≥ 15 % 10; positiv und steigend 7; positiv 5; negativ aber steigend 3 |
| Wachstum relativ zur Bewertung | 15 | (EV/Umsatz) ÷ (Wachstum × 100): ≤ 0,20 15; ≤ 0,35 11; ≤ 0,50 7; ≤ 0,80 3 |
| Finanzierungsbedarf | 10 | FCF ≥ 0: 10; sonst Liquidität / FCF-Abfluss in Jahren: ≥ 4 7; ≥ 2 4; ≥ 1 2 |
| Verwässerung | 5 | Ø Aktienzunahme: ≤ 1 % 5; ≤ 3 % 4; ≤ 6 % 2; ≤ 10 % 1 |
| Marktpotenzial und Wettbewerbsfähigkeit | 5 | Mittel der KI-Stufen Marktgröße und Wettbewerbsvorteile |

Junge Unternehmen werden nicht ausgeschlossen. Fehlender Gewinn, negativer FCF, kurze Historie, Cash Burn
(Reichweite), Verwässerung und Bruttomarge sind einzelne, sichtbare Kriterien.

## 9. Portfolia-Score (bis 0.109 „Tom Nash 125 – Public Reconstruction v1“) und Portfolia Fundamental v1

### 9.1 Quellenlage

| Quelle | Inhalt | Zugang |
|---|---|---|
| Video „The UNTHINKABLE is About to Happen to Stocks (but most will invest wrong)“, [youtube.com/watch?v=6pm5KqQw6RI](https://www.youtube.com/watch?v=6pm5KqQw6RI) | Primärquelle | aus der Entwicklungsumgebung nicht abrufbar (YouTube sperrt Cloud-IP-Adressen; Transkript nicht verfügbar) |
| Zusammenfassung [youtubesummary.com/summary/6pm5KqQw6RI](https://youtubesummary.com/summary/6pm5KqQw6RI) | „125-point test“; Cash vs. Debt (0/5/10), Revenue Growth (< 10 / 10–20 / > 20 %), Operating Margin (< 10 / 10–25 / > 25 %), Scalability (Umsatz- vs. Kostenwachstum, Beispiele), 5-Year vs. S&P 500 (> 20 % besser: 10; 10–20 %: 5; schlechter: 0), Short Interest und Institutional Ownership (ohne Schwellen), Moat, CEO, Recession Resilience, langfristige Trends, Kultur, Talent; Bereiche 100–125 exceptional, ~75–100 Watchlist, darunter schwach | öffentlich, Sekundärquelle |
| Tom Nash auf X ([Status 1766765502553112868](https://x.com/iamtomnash/status/1766765502553112868)), ROIC Academy Lecture #59 ([Patreon](https://www.patreon.com/posts/lecture-59-stock-134653136)) | Ankündigung einer Scorecard bzw. Lektion | Inhalt nicht öffentlich / hinter Bezahlschranke – nicht verwendet |

Die Bezeichnung „Original“ wird nicht verwendet: Die Primärquelle war nicht vollständig zugänglich, die öffentliche
Wiedergabe ist eine KI-Zusammenfassung (kein wörtliches Transkript). Geschützte Schulungsinhalte wurden nicht
verwendet.

**Entscheidung zur Struktur:** Die Auftragsvorgabe nannte als qualitative Kriterien u. a. „Product / Mission
Criticality“ und „Talent / Culture Quality“. Die öffentliche Wiedergabe nennt dagegen „competitive moat, CEO quality,
resilience in recessions, long-term trends such as AI, company culture, and talent“ – zusammen mit den sieben
Kennzahl-Kriterien ergibt das **genau 13 Kriterien, wenn Kultur und Talent getrennt zählen**; „Product / Mission
Criticality“ kommt nicht vor. Portfolia folgt der belegten Fassung: **Company Culture** und **Talent** als getrennte
Kriterien, „Product / Mission Criticality“ entfällt. Die Maximalpunkte je Kriterium (12 × 10 + Recession Resistance 5 =
125) nennt die Wiedergabe nicht; sie stammen aus der Auftragsvorgabe und gehen rechnerisch auf.

### 9.2 Die 13 Kriterien

Herkunft der Regel: **belegt** = in der öffentlichen Wiedergabe genannt; **Auftrag** = Kriterium belegt, Schwellen aus
der Auftragsvorgabe, nicht verifiziert; **Annahme** = Portfolia-Interpretation.

| Kriterium | Max | Regel | Herkunft | Daten |
|---|---|---|---|---|
| Cash vs. Debt | 10 | Liquidität ≥ 2 × Schulden 10; > Schulden 5; sonst 0 (keine Schulden berichtet: als 0 angesetzt, sichtbar) | belegt | SEC: Kasse + kurzfristige Anlagen; Finanzverbindlichkeiten |
| Revenue Growth | 10 | < 10 % 0; 10–20 % 5; > 20 % 10 | belegt (Messzeitraum Geschäftsjahr: Annahme) | SEC |
| Operating Margin | 10 | < 10 % 0; 10–25 % 5; > 25 % 10 | belegt | SEC |
| Short Interest | 10 | < 5 % 10; 5 bis < 10 % 5; ≥ 10 % 0 | Auftrag | Yahoo `shortPercentOfFloat` |
| Institutional Ownership | 10 | < 30 % 0; 30–60 % 5; > 60 % 10 | Auftrag | Yahoo `heldPercentInstitutions` |
| Scalability | 10 | Umsatzwachstum − Wachstum der operativen Aufwendungen ≥ 5 Pp. 10; > 0 5; ≤ 0 0 | Vergleich belegt, Stufen Annahme | SEC: `OperatingExpenses`, sonst Bruttogewinn − operatives Ergebnis, sonst Umsatz − operatives Ergebnis |
| 5-Year Return vs. S&P 500 | 10 | Mehrrendite > 20 Pp. 10; 10–20 Pp. 5; darunter 0 (0–10 Pp.: Annahme, Quelle schweigt) | belegt | Gesamtrendite (dividenden- und splitbereinigt) über identische Handelstage; S&P 500 Total Return Index, ersatzweise SPY; nur USD; < 5 Jahre Historie → unbewertet |
| Competitive Moat | 10 | KI-Stufe 0/1/2 → 0/5/10 | Kriterium belegt, Stufen Annahme | Geschäftsbericht, Profil, Kennzahlen |
| CEO Quality | 10 | dto. | dto. | dto. |
| Recession Resistance | 5 | KI-Stufe 0/1/2 → 0/2,5/5 | dto. | dto. |
| Secular Trend | 10 | KI-Stufe 0/1/2 → 0/5/10 | dto. | dto. |
| Company Culture | 10 | dto. | Kriterium belegt, Stufen Annahme | dto. |
| Talent | 10 | dto. | Kriterium belegt, Stufen Annahme | dto. |

Qualitative Kriterien: Bewertung nur mit mindestens einem Beleg (Quellen-ID); Stufe 2 nur mit **wörtlich in der
Quelle gefundener Belegstelle** (≥ 15 Zeichen) und Konfidenz ≥ 0,5, sonst Stufe 1. Ohne Beleg bleibt das Kriterium
**unbewertet** (0 Punkte, als fehlend gezählt) – es wird nichts aufgefüllt.

### 9.3 Einordnung

Bereiche laut Quelle: 125 „once-in-a-lifetime“, 100–125 Exceptional, etwa 75–100 Watchlist, darunter schwache
Kandidaten (Portfolia: unter 75 niedrige Priorität / No-Go nach der Methode). Laut Video empfiehlt Tom Nash, statt
eines nicht als Exceptional eingestuften Einzelwerts den S&P 500 zu kaufen – die Seite nennt das als seine Aussage,
nicht als Empfehlung von Portfolia. Bei fehlenden Kriterien wird die Spanne `erreichte Punkte … erreichte Punkte + Maximum der fehlenden`
berechnet; eine Einordnung erscheint nur, wenn die ganze Spanne in einem Bereich liegt, sonst „Nicht bestimmbar“ mit
Spanne. Die Kategorie „Nash 100+“ erfordert gesicherte ≥ 100 Punkte. Die Einordnung ist eine Interpretation der
Scorecard, **keine Kaufempfehlung**.

### 9.4 Portfolia Fundamental v1 und Historisierung

Die bis 0.108 als „Tom-Nash-inspiriert“ bezeichnete eigene Systematik bleibt unverändert als **Portfolia Fundamental v1**
erhalten (Kriterien und Gewichte wie bisher: Umsatzwachstum 15, Margen 10, Bilanz 10, Liquidität 10, Kurs-CAGR 10,
Wettbewerbsvorteile 15, Management 10, Institutionelle 5, Short Interest 5, Skalierbarkeit 10, Trends 10, Relevanz 10,
Organisation 5). Ältere Versionen werden beim Lesen nur umbenannt (`nash` → `fundamental_v1`, alte Bezeichnung
sichtbar), nicht neu berechnet; Migration 42 kopiert die Spalte `nash` nach `fundamental_v1`. Tom Nash 125 entsteht
nur für Versionen ab 0.109 (`nash125`, Methodenkennung im Bericht).

## 10. Valuation Score (100 Punkte) und Szenarien

**Multiplikatoren** (Basis letztes Geschäftsjahr; Fundamentaldaten bei abweichender Berichtswährung mit aktuellem
Devisenkurs umgerechnet):

| Kennzahl | Formel |
|---|---|
| KGV | Marktkapitalisierung / Nettoergebnis |
| EV | Marktkapitalisierung − Nettofinanzposition |
| EV/Umsatz, EV/EBIT | EV geteilt durch Umsatz bzw. operatives Ergebnis |
| Kurs/FCF, FCF-Rendite | Marktkapitalisierung / FCF bzw. Kehrwert |
| PEG (klassisch, ab 0.109) | KGV ÷ (Ø jährliches Wachstum des verwässerten Ergebnisses je Aktie über bis zu 3 Jahre × 100); nur bei positivem EPS am Anfang und Ende |
| KGV ÷ Umsatzwachstum | umsatzbasiertes PEG-Analogon (bis 0.108 fälschlich „PEG“ genannt) – nur Ersatz, wenn kein klassisches PEG berechenbar ist, und so beschriftet |
| Gesamtrendite 5 J. | Aktie und S&P 500 über identische Handelstage, dividenden- und splitbereinigt (für Tom Nash 125) |
| Forward-KGV | Yahoo, Analystenkonsens („geschätzt“) |

EBITDA wird nicht berechnet, weil Abschreibungen nicht einheitlich verfügbar sind.

**Szenarien** (5 Jahre, je konservativ / Basis / optimistisch, alle Annahmen am Ergebnis):

| Annahme | Regel |
|---|---|
| Startwachstum | Basis = Mittel aus Jahreswachstum und 3-J.-CAGR, begrenzt auf −10 % … 50 %; konservativ × 0,5; optimistisch × 1,4 (≤ 80 %); fällt linear auf 4 % im Jahr 5 |
| Ziel-FCF-Marge | profitabel: Ø FCF-Marge (× 0,8 / × 1 / × 1,2, höchstens 60 % der Bruttomarge, nie unter Basis); sonst 15 / 25 / 35 % der Bruttomarge |
| Bewertungsprofil (ab 0.109) | nach Unternehmensart, nicht je Unternehmen: **profitables Wachstum** (FCF-Marge > 2 %, Startwachstum ≥ 8 %): Kurs/FCF 15 / 22 / 30, Diskontierung 9 %; **reif** (FCF positiv, < 8 %): 10 / 14 / 18, 8 %; **früh** (ohne nachhaltigen FCF): 15 / 22 / 30, 11 % (höheres Ausführungsrisiko) |
| Kurs/FCF im Jahr 5 | nach Profil (siehe oben) |
| Verwässerung p. a. | Ø der letzten Jahre (0–8 %) +1 / 0 / −1 Pp. |
| Nettofinanzposition | unverändert (vereinfacht) |
| Diskontierung | nach Profil 8 / 9 / 11 % p. a. (Annahme) |

**Rechenweg:**

- Wert je Aktie_5 = (FCF_5 × Multiplikator + Nettofinanzposition) / Aktien_5.
- Rendite p. a. = (Wert_5 / Kurs)^(1/5) − 1.
- Barwert = Wert_5 / (1 + Diskontierung des Profils)^5.

Das **eingepreiste Startwachstum** ist das g, bei dem der Barwert im Basismodell dem Kurs entspricht (Bisektion,
−30 % … +150 %). Liegt es außerhalb dieser Spanne, wird das angezeigt.

**Valuation Score:**

| Kriterium | Max | Regel |
|---|---|---|
| Bewertungsniveau | 30 | profitabel: FCF-Rendite ≥ 6 % 30; ≥ 4 % 24; ≥ 2,5 % 16; ≥ 1,5 % 9; ≥ 0,5 % 4; sonst EV/Umsatz ≤ 2 30; ≤ 4 22; ≤ 7 14; ≤ 12 7; ≤ 20 3 |
| Wachstum relativ zur Bewertung | 25 | klassisches PEG ≤ 1 25; ≤ 1,5 19; ≤ 2 12; ≤ 3 5; ersatzweise KGV ÷ Umsatzwachstum (gleiche Stufen, eigene Bezeichnung); ohne positives KGV: EV/Umsatz je Wachstumspunkt ≤ 0,2 25 … |
| Erwartete Rendite (Basis) | 25 | ≥ 15 % 25; ≥ 10 % 19; ≥ 6 % 12; ≥ 2 % 6 |
| Unsicherheit | 10 | Spannweite (opt. − kons.) / Basis: ≤ 60 % 10; ≤ 100 % 7; ≤ 150 % 4; sonst 1; Datenabdeckung < 70 % → höchstens 3 |
| Finanzielle Risiken | 10 | Nettoliquidität 10; Nettoschulden ≤ 2 J. FCF 7; ≤ 4 J. 4; sonst 1; bei negativem FCF 0 |

Ein DCF im engeren Sinn wird nicht gerechnet. Die Szenarien sind ein transparentes Multiplikatormodell, weil
Kapitalkosten und Endwert für junge Unternehmen nicht belastbar bestimmbar sind.

## 11. Portfolio-Fit (100 Punkte, lokal)

| Kriterium | Max | Regel |
|---|---|---|
| Gewicht nach Kauf | 25 | ≤ 3 % 25; ≤ 5 % 20; ≤ 8 % 12; ≤ 12 % 5 |
| Branchengewicht nach Kauf (Yahoo-Sektor) | 25 | ≤ 10 % 25; ≤ 20 % 18; ≤ 30 % 10; ≤ 40 % 4 |
| Überschneidung | 15 | bereits gehalten 0; gleiche Teilbranche (industry): 0 % 15; ≤ 5 % 12; ≤ 10 % 8; ≤ 20 % 4 |
| Währung nach Kauf | 10 | ≤ 40 % 10; ≤ 60 % 7; ≤ 75 % 4; sonst 1 (Krypto separat) |
| Korrelation mit Portfolio | 15 | Tagesrenditen in EUR bis 1 Jahr, ≥ 120 gemeinsame Tage: ρ ≤ 0,3 15; ≤ 0,5 11; ≤ 0,7 6; ≤ 0,85 3 |
| Geografie nach Kauf | 10 | ≤ 40 % 10; ≤ 60 % 7; ≤ 80 % 4; sonst 1 |

**Kauf-Simulation** (frei wählbarer Betrag; 500, 1.000 und 2.500 € als Schnellwahl): Portfolio vorher/nachher,
Gewicht, Top-5-Konzentration, HHI sowie Verteilungen nach Branche, Währung, Land und Anlageklasse. Es entsteht keine
Buchung; ein Test prüft das.

Grenzen:

- ETF-Überschneidungen werden nicht bewertet, weil keine verlässlichen Bestandteildaten vorliegen. Gehaltene ETFs
  werden benannt.
- Ein leeres Portfolio ergibt „nicht bewertet“.
- Der Fit verändert keine anderen Scores.

## 12. Einzelbewertung und Scorecard

Jedes Kriterium speichert und zeigt:

1. Definition
2. Skala
3. Maximum
4. Punkte (bei gemischten Kriterien je Teil)
5. Fakten mit Wert, Periode, Herkunft und Quelle
6. Rechenregel
7. Begründung
8. Quellen-IDs
9. Gegenargumente
10. Konfidenz
11. Fehlende Informationen

Score = Summe der Teilpunkte. `scoring.recompute()` rechnet aus dem gespeicherten Bericht nach; Tests prüfen das für
alle Scores. Nicht bewertete Kriterien zählen 0 Punkte. Abdeckung = bewertete Maximalpunkte / Maximum; unter 70 %
gilt der Score als „unvollständig“. Es wird nichts hochgerechnet.

## 13. Datenherkunft

| Klasse | Bedeutung | Beispiel |
|---|---|---|
| `reported` | unverändert aus SEC-Einreichung bzw. Datenanbieter | Umsatz (us-gaap:Revenues, 10-K, eingereicht …, Accession …) |
| `derived` | deterministisch berechnet, Formel am Wert | FCF = operativer Cashflow − Investitionsausgaben |
| `estimated` | Annahme, Modell, KI | ROIC mit angenommenem Steuersatz; Szenarien; Forward-KGV; KI-Stufen |
| `missing` | kein belastbarer Wert | Jahr ohne Bericht; Finanzverbindlichkeiten nicht berichtet |

Weitere Regeln:

- **Geschäftsjahr** = Periodenende. Das Feld `fy` in companyfacts bezeichnet das Jahr der Einreichung, nicht der
  Periode.
- **Restatements:** Es gilt die jüngste Einreichung bis `as_of`. Abweichungen werden mit Erst- und Neuwert
  angezeigt („korr.“).
- **Aktiensplits:** Bereinigt wird nur, wenn eine spätere Einreichung dieselbe Periode mit einem Split-Faktor (±3 %)
  neu berichtet. Ein Sprung ohne diesen Nachweis, etwa durch einen Börsengang, bleibt unverändert.
- **Währungen:** Es werden keine Währungen vermischt; mehrere Berichtswährungen werden vermerkt.
- **Fehlende Jahre** werden nie aufgefüllt.

## 14. KI-Integration und KI-Steuerung

Es gibt keine zweite Infrastruktur: Aufrufe laufen über `AiGateway.generate` (Claude oder Gemini laut
`llm.provider`, Modell laut `llm.model` bzw. `llm.gemini_model`), JSON-Modus, Budgets und Pausen des Gateways.

**Teilanalysen** (je Unternehmen, einzeln gespeichert in `disc_ai_part`, einzeln nachholbar; seit 0.110 entstehen
alle drei aus **einem** Aufruf `analyse`, Nachholen eines ungültigen Teils kostet je Teil einen Aufruf):

| Teil | Inhalt | Prüfung |
|---|---|---|
| `bericht` | Geschäftsmodell, These mit Treibern, Gegenhypothese, Kernrisiko, Meilensteine, Unsicherheiten | Pflichtteile, Längen, unbekannte Quellen-IDs verworfen, Sätze mit unbelegten Zahlen entfernt |
| `disruption` | 12 qualitative Kriterien (Stufen 0–4) für Disruption, Quality, GARP, Portfolia Fundamental v1 | Stufen, Belege, Kappung (siehe 6) |
| `nash` | 6 qualitative Tom-Nash-Kriterien (Stufen 0–2) mit Belegstellen | Zitat wörtlich im gesendeten Quelltext? (normalisierte Leerzeichen/Anführungszeichen, ≥ 15 Zeichen) |

Kleine Modelle lieferten in 0.108 teils „KI-Antwort ist kein gültiges JSON“ (eine große Antwort). Abhilfe: drei
kleinere Antworten, ein klarer Formathinweis im Systemtext und eine tolerante Auswertung (`parse_lenient`:
Zeilenumbrüche in Zeichenketten, Code-Zäune, überzählige Kommas) – Inhalte werden nie verändert. Bleibt die Antwort
ungültig, steht im Protokoll Fehlerklasse „json“ mit Position.

**Status** (`/discovery`, Abschnitt „KI-Researchstatus“; je Unternehmen „KI-Researchstatus dieser Firma“): KI
global an/aus, KI für Discovery, Anbieter und **tatsächlich verwendetes Modell**, Schlüssel vorhanden (nie der
Inhalt), Anbieterzustand (verfügbar, pausiert bis, gestört), letzter erfolgreicher und letzter fehlgeschlagener
Aufruf, Zeitplan mit nächstem Lauf, laufende Jobs, Tages- und Monatskontingent mit Restmenge und Tokens. Je
Unternehmen: Zustand je KI-Teil (vollständig, veraltet, noch nicht erstellt, ungültig, Anbieterfehler, Kontingent,
pausiert, nicht konfiguriert, keine Quellen), Zeitpunkt, Anbieter/Modell, letzter Versuch und Fehlergrund.

**Arbeitsschritte und Zustände:** Jobs `ki_discovery`, `screening`, `voll`, `news`, `analyse`, `beobachtet` mit den
Zuständen geplant, läuft, abgeschlossen, teilweise abgeschlossen, übersprungen, fehlgeschlagen, Kontingent erschöpft,
KI nicht konfiguriert, unterbrochen – jeweils mit konkretem Grund.

**Knöpfe → Abläufe:**

| Knopf | Ablauf | KI-Aufrufe |
|---|---|---|
| KI-Discovery starten | Weg A (Abschnitt 4) | 2 + 3 je vertiefter Analyse |
| Fundamentalscreening starten | Wege B und C | 0 |
| Vollständigen Discovery-Lauf starten | Nachrichten → Screening → KI-Discovery | wie KI-Discovery |
| KI-Analyse starten | alle drei Teile neu | 3 |
| Fehlende KI-Teile ergänzen | nur Teile ohne gültiges Ergebnis | 0–3 |
| Fundamentaldaten aktualisieren | SEC und Kurse neu (Cache umgangen), Scores neu, keine KI | 0 |
| Vollanalyse aktualisieren | Daten neu, veraltete (andere Eingabe/Promptversion) und fehlende Teile neu | 0–3 |

Kein Knopf blockiert: Der Job wird angelegt und an den Scheduler übergeben (Pool `discovery` für lange Läufe, Pool
`interactive` für Unternehmensanalysen – Vorrang für manuelle Arbeit); die Seite zeigt Job-ID, Schritt, Ergebnis und
Fehlergrund und fragt per HTMX alle 2–4 s nach, solange der Job läuft. Ohne Scheduler (Tests,
`scheduler_enabled = false`) läuft der Job im Anfrage-Thread.

**Kein Doppelstart:** eindeutiger Teilindex `disc_job(slot) WHERE status IN ('geplant','laeuft','wartet')` – gilt für
gleichzeitige Klicks, Scheduler und Knopf. **Neustart:** Jobs anderer Prozesse werden beim Start als „unterbrochen“
markiert (Grund sichtbar); laufende Jobs ohne Lebenszeichen seit 45 Minuten ebenfalls. Screening setzt am
gespeicherten Fortschritt fort.

**Nachholen (Fehlerbehebung zu 0.108):** Bis 0.108 wurde der Eingabe-Hash geprüft, *bevor* die KI angesprochen wurde –
eine Analyse ohne KI-Teil galt bei unveränderten Daten als „unverändert“, der KI-Teil wurde nie nachgeholt. Jetzt
werden die KI-Teile nach Modus geplant und vor dem Hash-Vergleich erstellt; der Hash enthält die IDs der verwendeten
KI-Teile. Ein nachgeholter Teil erzeugt immer eine neue Version, gültige Teile werden wiederverwendet. Automatische
Läufe wiederholen fehlgeschlagene Teile frühestens nach 24 Stunden.

**Prompt-Injection:** Quellen stehen als JSON-Zeichenketten im Nutzerteil; der Systemtext erklärt sie zu
unvertrauenswürdigen Daten; Antworten werden nur als Text angezeigt.

## 15. KI-Kosten

- Eigene Grenzen zusätzlich zum Gateway-Budget: `discovery.ai_calls_per_day` (Standard 12) und
  `discovery.ai_calls_per_month` (150), Verbrauch je Tag in `disc_ai_usage` (Aufrufe, Tokens).
- Jeder Aufruf steht mit Arbeitsschritt, Job, Unternehmen, Anbieter, Modell, Prompt-Version, Eingabe-Fingerabdruck,
  Status, Tokens und sicherer Fehlermeldung in `disc_ai_call` (ohne Prompt-Inhalt, ohne Schlüssel); einsehbar unter
  `/discovery/protokoll`.
- **Kosten in Euro werden nicht angezeigt**, weil sie nicht verlässlich bestimmbar sind (Gemini: Free Tier oder
  Abrechnung je Google-Projekt; Claude: Preise nicht sicher bekannt). Angezeigt werden Aufrufe und Tokens; `cost_usd`
  bleibt leer.
- Vor jedem Start zeigt die Oberfläche Anbieter, Modell, voraussichtliche Aufrufzahl und Restbudget. Manuelle Knöpfe
  umgehen keine Grenze; es gibt keinen automatischen Wechsel auf ein anderes Modell oder einen anderen Anbieter.
- Konservative Standards: Screening-Stapel 20, 1 vertiefte KI-Analyse je Lauf, KI-Discovery wöchentlich, bis zu 6
  Unternehmen je KI-Discovery, Ausgabelänge 6000 Tokens, Quelltexte gekürzt (9.000 Zeichen je Quelle, 3.000 je
  Unternehmen in der KI-Discovery).

## 16. Datenschutz

- An die KI gehen nur öffentliche Unternehmensdaten (Name, Ticker, Börse, Branche, Land, Auszüge aus SEC-Berichten,
  berechnete Kennzahlen, Meldungstitel) bzw. in der KI-Discovery öffentliche Ausgangsbelege (SEC-Kennzahlen,
  Meldungen).
- Nie an die KI gehen Konten, Wallets, Transaktionen, Mengen, Bestandswerte, Gewichte oder die Liste gehaltener Werte;
  Tests prüfen die Nutzlast aller Teilanalysen und der KI-Discovery mit einem präparierten Depot.
- An die SEC gehen nur CIK bzw. feste Pfade; an Yahoo nur Ticker.
- Portfolio-Fit und Simulation werden ausschließlich lokal berechnet; die Neuheit eines Kandidaten (Portfolio,
  Watchlist) wird lokal bestimmt.

## 17. Historisierung

Jede Analyse ist eine neue Zeile in `disc_analysis`, frühere werden nie überschrieben. Gespeichert werden:

- Zeitpunkt und Auslöser;
- Kurs mit Zeitstempel, Marktdaten, Fundamentaldaten (mit Herkunft);
- Signale, Scores mit allen Kriterien und Gewichten, Quellen;
- Anbieter, Modell, Prompt- und Berechnungsversion;
- Abdeckung, Konfidenz, These, Gegenhypothese, Meilensteine.

Der Vergleich zeigt:

- Score- und Kriterienänderungen;
- geänderte Kennzahlen und nachträglich korrigierte Werte;
- neue Signale zwischen den Versionen;
- Status der Meilensteine der älteren Version;
- neue Risiken.

Meilensteine mit Kennzahl werden bei jeder Analyse automatisch geprüft: „erfüllt“ sofort, „verfehlt“ nach der
Fälligkeit.

**Seit 0.109** zusätzlich je Version: KI-Zustand (`ai_state`), Zustand je KI-Teil (`parts_json`), Eingabe-
Fingerabdrücke, auslösender Job, Modus, Methodenkennung „Tom Nash 125 – Public Reconstruction v1“ und Einordnung.
Ältere Versionen bleiben unverändert lesbar.

**Export und Wiederherstellung:** Der Gesamtexport (`state.json` → `discovery`) enthält Unternehmen, Aliasse,
Signale, alle Analyseversionen, Meilensteine, KI-Teile, Kandidaten, Jobs, KI-Protokoll und Verbrauch. Die
Wiederherstellung läuft in derselben Transaktion wie die übrigen Zusatzdaten: Unternehmen werden über CIK bzw. Ticker
zugeordnet, IDs neu vergeben und Verweise umgeschrieben; wiederholbar ohne Dubletten; aktive Jobs des alten Systems
werden als „unterbrochen“ übernommen. Nicht enthalten: Research-Universum und SEC-Zwischenspeicher (werden aus
öffentlichen Daten neu aufgebaut). `/discovery/export.json` bleibt eine Ansicht, kein Wiederherstellungsweg.

## 18. Tests und Abnahme

`tests/test_discovery.py` (29 Tests) und `tests/test_discovery2.py` (38 Tests), synthetische Daten, ohne Netzwerk
und ohne echte KI-Aufrufe (kontrollierte Attrappen des Gateways). Neu in 0.109 geprüft:

- **Tom Nash 125:** 13 Kriterien, Maximalpunkte (12 × 10 + 5), Summe reproduzierbar, Schwellen an allen Grenzen
  (0/5/10), Cash vs. Debt (ohne Schulden, > Schulden, ≥ 2 ×), Institutionelle, Scalability, Mehrrendite inkl.
  Lücke 0–10 Pp., fehlende Werte → unbewertet, Recession Resistance höchstens 5, Belegpflicht und Kappung qualitativer
  Kriterien, Einordnung nur wenn bestimmbar, Gesamtrendite über identische Zeiträume, kurze Historie, ältere Methode
  nur umbenannt (nicht neu berechnet), Migration.
- **KI-Steuerung:** KI global aus, Schlüssel fehlt, Discovery-KI aus, Tages- und Monatsgrenze, Anbieter pausiert,
  Statusanzeige (Anbieter/Modell aus der Konfiguration, kein Schlüssel im HTML), vollständige, teilweise und
  nachgeholte Analysen (früherer „unverändert“-Fehler), nur fehlgeschlagener Teil wird wiederholt, Wiederverwendung
  gültiger Teile, veraltete Teile, tolerantes JSON, Belegstellen-Prüfung.
- **Jobs:** Doppelklick, vier gleichzeitige Anfragen → ein Job, genau einmalige Ausführung, Fehler mit Klasse und Text,
  Neustart mit offenen Jobs, Zeitüberschreitung, Routen und HTMX-Abfrage (aktiv/beendet), Unternehmens-Knöpfe,
  Zeitplan (Schritt aus/an, Pool, automatischer Auslöser).
- **Discovery Engine:** Universum nur Hauptbörsen, Tagesindex, Vorauswahl über Frames, negative Signale nie
  vorausgewählt, Bestätigung in Stapeln mit Fortsetzung, SPAC verworfen, Fehlbewertung vs. Value Trap vs. kein
  Rückstand, KI-Discovery ohne Meldungen (Unternehmen außerhalb der News-Pipeline), Identitätsprüfung (falscher
  Ticker, privat, nur OTC, Dublette), Thema ohne Beleg verworfen, Priorität, Signale, Kandidaten, Zuordnung jedes
  KI-Aufrufs zum Job, zweiter Lauf ohne künstliche Neuentdeckungen, KI aus/Kontingent, Vollauf bei Teilausfällen.
- **Bewertung:** klassisches PEG vs. umsatzbasiertes Analogon, Bewertungsprofile; **Export/Restore**: Rundreise,
  Wiederholbarkeit, Formatprüfung; **Datenschutz**: präpariertes Depot erscheint in keiner KI-Nutzlast.

### Abnahme 0.109 mit echten Daten (08.11.2026)

Über die Weboberfläche eines lokal gestarteten Servers mit Scheduler, echten SEC- und Yahoo-Daten. **Kein echter
KI-Schlüssel in der Entwicklungsumgebung** – KI-Schritte wurden mit einem *kontrollierten Mock* des Gateways
geprüft (Modellname „MOCK-kontrolliert (kein Live-Modell)“ im Protokoll). **Die Live-Abnahme mit Gemini/Claude steht
aus.**

| Test | Ergebnis |
|---|---|
| Fundamentalscreening (Knopf) | Antwort des Knopfs nach 0,01 s, Job im Hintergrund; Universum 6.067 Emittenten; Vorauswahl 3.543 bewertet, 489 vorausgewählt, 2.524 ohne Umsatzangaben in den Frames, 655 mit Gegenanzeichen; Stapel 20: 20 bestätigt, 20 neu (u. a. Lumentum, Alnylam, Boeing, NIO, Vertex, Comstock, Keros); Größenmix groß/mittel/klein/Mikro; Fehlbewertung: 3 möglich (Keros, Alnylam, Sound Group), 8 Value Traps (z. B. Expand Energy: Verwässerung 53 % durch Fusion; Boeing: Verwässerung und Nettoschulden) |
| Fehler im ersten Lauf | negative Umsatzwerte in den Frames ließen die Größenklasse abstürzen, Fehlermeldung war leer – behoben, Test ergänzt |
| Tom Nash 125 (3 Unternehmen) | siehe unten; alle numerischen Kriterien gegen SEC/Yahoo-Werte nachvollzogen |
| Manuelle KI-Analyse (Mock) | Job startet, Anbieter/Modell aus der Konfiguration, 3 protokollierte Aufrufe mit Tokens, neue Version „vollständig“, 6 von 6 Belegstellen wörtlich im 10-K gefunden |
| Unvollständige KI-Analyse | KI aus → Job „KI nicht konfiguriert“ mit Grund, Version ohne KI-Teil; KI an → „Fehlende KI-Teile ergänzen“ erstellt die drei Teile und eine neue Version |
| Eigenständige Discovery | 20 Kandidaten aus dem Screening, keiner aus der News-Pipeline (keine Meldungen im Abnahme-System) |
| Full Research (Alnylam) | Wendepunkt-Signal → Fehlbewertungs-Signal → Fundamentaldaten → Tom Nash 125 (50, möglich 50–105) → Valuation 39/100: eingepreistes Startwachstum 124 % p. a., Basisszenario −10,7 % p. a. – die Bewertung widerspricht der „möglichen Fehlbewertung“; beides steht nebeneinander |

Tom Nash 125, numerische Kriterien (Stand 08.11.2026; qualitative Kriterien ohne Live-KI unbewertet):

| Kriterium | NVIDIA (GJ bis 25.01.2026) | Rocket Lab (GJ 2025) | Lumentum (GJ bis 27.06.2026) | Quelle | Sicherheit |
|---|---|---|---|---|---|
| Cash vs. Debt | 50,1 Mrd. / 8,5 Mrd. = 5,9× → 10 | 1,02 Mrd. / 0,15 Mrd. = 6,7× → 10 | 2,74 Mrd. / 0,04 Mrd. → 10 | SEC companyfacts | Regel belegt |
| Revenue Growth | +65,5 % → 10 | +38,0 % → 10 | +83,2 % → 10 | SEC | Regel belegt |
| Operating Margin | 60,4 % → 10 | −38,0 % → 0 | 17,4 % → 5 | SEC | Regel belegt |
| Short Interest | 1,3 % → 10 | 7,6 % → 5 | 8,6 % → 5 | Yahoo | Schwellen unverifiziert |
| Institutional Ownership | 71,5 % → 10 | 62,5 % → 10 | 95,6 % → 10 | Yahoo | Schwellen unverifiziert |
| Scalability | Umsatz +65,5 % vs. Kosten +40,7 % → 10 | +38,0 % vs. +42,5 % → 0 | +83,2 % vs. +14,2 % → 10 | SEC (`OperatingExpenses`) | Stufen = Annahme |
| 5-Year Return vs. S&P 500 | +509 % vs. +68 % → 10 | +255 % vs. +68 % → 10 | +491 % vs. +68 % → 10 | Yahoo (bereinigt), ^SP500TR, 08.11.2021–06.11.2026 | Regel belegt |
| Summe (13 Kriterien) | 70, möglich 70–125, „nicht bestimmbar“ | 45, möglich 45–100 | 60, möglich 60–115 | | |

**Historische Fallstudie** (Phase N, nur regelbasierte Kriterien, Daten bis zum Stichtag eingereicht über
`as_of`, ohne KI und ohne damalige Kurse):

| Unternehmen, Stichtag | Daten | Wachstum | Brutto- / op. Marge | Quality | Growth | Fundamental v1 (Regel) | Entwicklung danach |
|---|---|---|---|---|---|---|---|
| NVIDIA, 30.04.2016 | 6 GJ | 7 % | 56 % / 15 % | 48,5 | 33 | 27 | Umsatz-CAGR 46 % über 10 J., op. Marge 60 % |
| Palantir, 31.03.2021 | 3 GJ | 47 % | 68 % / −107 % | 34 | 35 | 33 | CAGR 33 % über 5 J., op. Marge 32 % |
| Peloton, 30.09.2021 | 4 GJ | 120 % | 36 % / −5 % | 34 | 46 | 31 | CAGR −9 % über 5 J. |
| Plug Power, 30.06.2021 | 6 GJ | −141 %¹ | 5 % / −21 % | 16 | 7 | 18 | op. Marge −207 % |
| Beyond Meat, 31.03.2020 | 3 GJ | 239 % | 33 % / 0 % | 34 | 29 | 31 | CAGR −1 % über 6 J., op. Marge −121 % |
| Snap, 31.03.2018 | 3 GJ | 104 % | 13 % / −423 % | 34 | 20 | 29 | CAGR 28 % über 8 J., op. Marge −9 % |

¹ Plug Power berichtete 2020 nach einer Korrektur negativen Nettoumsatz.

Ergebnis:

- Die regelbasierten Fundamentalscores trennen die späteren Gewinner **nicht** zuverlässig von Enttäuschungen.
  Peloton hatte 2021 den höchsten Growth-Score, NVIDIA 2016 einen niedrigen.
- Klar erkannt werden fundamental schwache Fälle (Plug Power).
- Das ist erwartbar: Außergewöhnliche Entwicklungen entstanden aus qualitativen Faktoren (Technologieposition, neue
  Märkte), die Disruption-Kriterien mit KI erfassen sollen.
- Diese lassen sich **nicht seriös rückwirkend testen**, weil heutige KI-Modelle die spätere Entwicklung kennen
  (Zukunftsinformation). Deshalb wird kein rückwirkend optimierter Score präsentiert. Ein systematischer Backtest
  bräuchte damalige Dokumente und ein Modell ohne Wissen über die Zukunft.

### Abnahme 0.110 (09.11.2026)

* **Tests:** `tests/test_discovery.py` (29), `tests/test_discovery2.py` (38), neu `tests/test_discovery3.py` (11):
  ein Aufruf für drei Teile, keine KI in Automatik/Datenaktualisierung, Sperre ohne Kontingent ohne Job, „veraltet“
  nur nach neuem SEC-Bericht, umgestellte Vorjahre, Sondereffekte und bereinigtes KGV, Übernahme-Hinweise (SEC und
  Meldung), Prioritätsskala und Migration 43, Steckbrief ohne KI, Vorwärtstest und Sicherung, Disruption-Vorschau,
  automatische Datenaktualisierung (Reihenfolge neuer Bericht → ohne Analyse → fällig, Grenze, Einstellung,
  Fehler einer Firma bricht den Lauf nicht ab).
* **Echte Daten (SEC, Kurse), KI als gekennzeichneter Mock** (Modellname „MOCK-kontrolliert (kein Live-Modell)“):
  Lumentum (LITE) – Hinweis „Übernahme/Fusion laut SEC-Einreichung“ (15 Mitteilungen nach Form 425 seit 02.11.2026),
  Sondereffekt „Ergebnis aus Schuldentilgung −7,76 Mrd. USD“, bereinigtes Nettoergebnis ≈ 0,82 Mrd. USD, KGV bereinigt
  ≈ 66 (unbereinigt nicht berechenbar). KI-Discovery: genau 2 Aufrufe, 3 Vorschläge mit Vorschau (LITE, CRDO, COHR),
  3 Vorwärtstest-Einträge; KI-Analyse CRDO per Knopf: 1 Aufruf, alle drei Teile gültig, Portfolia-Score 82,5/125
  (Daten 55/65). Mobil (390 px) ohne horizontales Scrollen.
* **Live-KI-Abnahme steht weiterhin aus** (kein Schlüssel in der Entwicklungsumgebung).

## 19. Bekannte Grenzen

- **Live-KI-Abnahme ausstehend:** Ohne Schlüssel in der Entwicklungsumgebung wurden alle KI-Schritte nur mit
  kontrollierten Attrappen geprüft. Ob das konfigurierte Modell (z. B. gemini-3.5-flash-lite) die Schemata zuverlässig
  einhält, zeigt erst der Echtbetrieb – das Protokoll nennt dann Fehlerklasse und Position.
- **Tom Nash 125:** Primärquelle nicht abrufbar, nur eine KI-Zusammenfassung des Videos; Schwellen für Short
  Interest/Institutional Ownership und die Maximalpunkte je Kriterium nach Auftragsvorgabe, Stufen von Scalability
  und den qualitativen Kriterien sind Portfolia-Annahmen (je Kriterium gekennzeichnet). Ob „mehr als 20 % besser als
  der S&P 500“ Prozentpunkte der kumulierten Rendite oder ein relatives Verhältnis meint, ist nicht belegt (Portfolia:
  Prozentpunkte). Kultur und Talent als getrennte Kriterien folgen aus der Zählung der Wiedergabe (13 Kriterien).
- Belegstellen-Prüfung zeigt, dass ein Zitat in der Quelle steht – nicht, dass es das Kriterium inhaltlich trägt.
- Frames-Vorauswahl ist eine Näherung (Kalenderjahr statt Geschäftsjahr, nur US-GAAP-Elemente); IFRS-Emittenten
  (20-F) und Banken/Versicherungen fallen dort meist unter „ohne Daten“ und werden nur über andere Wege gefunden.
- Einmalerlöse (z. B. Lizenzzahlungen bei Biotech) können Wendepunkte vortäuschen; die Regeln bereinigen das nicht.
- Fundamentaldaten nur für SEC-registrierte Emittenten; Nicht-US-Unternehmen ohne SEC-Bericht bleiben ohne Kennzahlen.
- Yahoo ist eine inoffizielle Schnittstelle; Marktdaten, Short Interest und bereinigte Kurse können fehlen.
- Gesamtrendite vs. S&P 500 nur für in USD gehandelte Aktien mit mindestens 5 Jahren Historie.
- Identität ist konservativ (Kurznamen, Tochtergesellschaften); KI-Vorschläge mit falschem Ticker werden verworfen.
- Szenarien sind vereinfachte Multiplikatormodelle; Profile nach Unternehmensart ersetzen keine Peer-Analyse.
- ETF-Durchschau fehlt; historische Validierung nur qualitativ (siehe 18).
- Das Research-Universum gehört nicht zum Gesamtexport (wird neu aufgebaut; Screening-Fortschritt beginnt von vorn).
