# Discovery & Intelligence (0.108.0)

Research-Modul: Es entdeckt börsennotierte Unternehmen, bewertet sie in getrennten, vollständig aufschlüsselbaren
Scorecards und prüft den Fit zum eigenen Portfolio lokal. Es ist **kein** Kaufsignal-Generator. Es erzeugt keine
Buchung und keine Order und verändert weder Ledger noch Steuer- oder Buchungsprüfungsdaten.

## 1. Produktvision und Funktionsumfang

Fünf Stufen, jede mit eigenem Ergebnis:

| Stufe | Frage | Ergebnis |
|---|---|---|
| Discovery | Welche Unternehmen sind auffällig? | Kandidaten und Signale |
| Disruption | Wie groß ist das Potenzial des Geschäftsmodells? | Disruption Score (100) |
| Fundamental Validation | Was zeigen die Zahlen? | Quality (100), Growth/GARP (100), Tom-Nash-inspiriert (125) |
| Valuation | Was ist eingepreist? | Multiplikatoren, 3 Szenarien, eingepreistes Wachstum, Valuation Score (100) |
| Portfolio Fit | Passt es zum eigenen Depot? | Kauf-Simulation, Portfolio-Fit (100), nur lokal |

Die Scores werden nie zu einem Gesamtwert verrechnet. Ein Unternehmen kann innovativ und zugleich riskant sein, stark
und zugleich teuer, günstig und zugleich konzentrationserhöhend.

Seiten:

- `/discovery`: Radar mit Kategorien, Sortierung, Suche, Lauf starten, manuell aufnehmen, Einstellungen, Export.
- `/discovery/c/{id}`: Unternehmensanalyse (stabiler Link). `?a=<Version>` zeigt eine frühere Version,
  `?betrag=` setzt den simulierten Kaufbetrag.
- `/discovery/t/{Ticker}`: stabiler Link je Ticker. Er wird aus Watchlist und Positionsdetail verlinkt.
- `/discovery/c/{id}/vergleich?a=&b=`: Vergleich zweier Analyseversionen.
- `/discovery/export.json`: Export aller Discovery-Nutzerdaten.

Aktionen:

- Analyse erstellen bzw. aktualisieren; „Trotzdem neu erstellen“ erzwingt eine neue Version.
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
| Manuelle Eingabe | Ticker oder Name | – | – |

Bewusst nicht verwendet werden kostenpflichtige Datenanbieter, Scraping von Investor-Relations-Seiten oder
Paywall-Umgehung. Nicht-US-Emittenten ohne SEC-Registrierung haben keine Fundamentaldaten. Sie bleiben „fehlt“ und
werden nicht geschätzt.

## 4. Discovery-Methodik

Täglicher Lauf (06:47) bzw. „Discovery-Lauf starten“:

1. **News-Nennungen:** In `news_analysis.result_json` stehen „betroffene“ Werte der Art `aktie` der letzten
   `discovery.news_days` Tage (Standard 14). Sie werden je (Name, Ereignis) gruppiert; das Ereignis stammt aus
   `news_event`, ohne Ereignis gilt die einzelne Meldung.
2. **Identität:** Siehe Abschnitt 5. Nicht auflösbare (private, nicht US-registrierte) und mehrdeutige Namen werden
   gezählt und im Radar unter „nicht aufgenommen“ gelistet, aber nicht übernommen.
3. **Dubletten:** Je Unternehmen und Ereignis entsteht höchstens ein Signal (`fp` UNIQUE). Weitere Artikel erhöhen nur
   `n_sources` (verschiedene Quellen).
4. **Stärke:** `Bedeutung × min(1; 0,4 + 0,15 × min(Quellen; 4))`. Eine einzelne Meldung ergibt höchstens 0,55 ×
   Bedeutung. Viele Kopien zählen nicht mehr als 4 Quellen.
5. **Themen (KI, optional):** Wichtige Ereignisse der letzten 7 Tage (Kategorie Unternehmen/Branche/Regulierung,
   Bedeutung ≥ 0,5, höchstens 25) gehen an die KI. Sie leitet Themen und Unternehmen entlang der Wertschöpfungskette
   ab: Anbieter, Zulieferer, Infrastruktur, Nutznießer. Jedes Thema braucht Ereignis-IDs als Beleg. Jeder Ticker muss
   im SEC-Verzeichnis existieren und zum Namen passen.
6. **Fundamentale Frühsignale** (deterministisch bei jeder Analyse):
   - Umsatzwachstum beschleunigt (≥ 15 % und +10 Prozentpunkte zum Vorjahr);
   - Bruttomarge +3 Prozentpunkte;
   - Free Cashflow erstmals positiv.
7. **Begrenzung und Priorisierung:**
   - höchstens `discovery.max_new_per_run` neue Unternehmen (Standard 10);
   - danach `discovery.auto_analyse_per_run` Analysen (Standard 2) für die stärksten neuen Signale.

Kategorien im Radar (Regeln, keine Bauchentscheidung):

| Kategorie | Regel |
|---|---|
| Neue Entdeckungen | aufgenommen in den letzten 14 Tagen, Status „neu“ |
| Early Disruptors | Disruption vollständig und ≥ 55 **und** FCF-Marge ≤ 0 oder operative Marge ≤ 5 % |
| Hidden Champions | Marktkapitalisierung < 10 Mrd. USD **und** Quality vollständig ≥ 60 **und** ≤ 3 Nachrichtenquellen insgesamt (nie allein wegen Größe) |
| Quality | Quality vollständig ≥ 70 |
| Growth/GARP | Growth/GARP vollständig ≥ 65 |
| Fundamentale Verbesserungen | Fundamentalsignal in den letzten 180 Tagen |
| Zu beobachten | Status „beobachten“ |
| Bereits im Portfolio | gehaltene Position mit gleichem Ticker oder Namen |

Sortierung: nach jedem Score, nach Portfolio-Fit (1.000 €) oder nach Aktualität des jüngsten Signals. Es gibt keine
Bevorzugung nach Bekanntheit oder Größe.

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

## 9. Tom-Nash-inspirierte Fundamentalanalyse (125 Punkte)

**Recherche:** Tom Nash kündigt eine „Company Scorecard“ für seine Plattform Stock MVP an
([X, März 2024](https://x.com/iamtomnash/status/1766765502553112868)); eine „Stock Evaluation Scorecard“ gibt es als
Lektion hinter einer Bezahlschranke ([Patreon](https://www.patreon.com/posts/lecture-59-stock-134653136)). Kriterien,
Gewichte und Schwellen sind **nicht öffentlich belastbar dokumentiert**. Portfolia übernimmt daher keine angeblichen
Originalregeln.

Die Portfolia-Scorecard verwendet die im Auftrag genannten Themen als Kriterien. Gewichte und Schwellen sind eigene
Festlegungen, Gesamtsumme 125:

| Kriterium | Max | Art | Regel |
|---|---|---|---|
| Umsatzwachstum | 15 | Regel | CAGR: ≥ 30 % 15; ≥ 20 % 12; ≥ 12 % 9; ≥ 6 % 5; ≥ 0 2 |
| Operative Margen | 10 | Regel | ≥ 25 % 10; ≥ 15 % 8; ≥ 8 % 5; ≥ 0 2 |
| Bilanz und Verschuldung | 10 | Regel | Nettoschulden/FCF: Nettoliquidität 10; ≤ 1 8; ≤ 2 6; ≤ 3 3 |
| Liquidität | 10 | Regel | Reichweite (6) + Current Ratio (4) |
| Historische Aktienperformance | 10 | Regel | Kurs-CAGR bis 5 J. (split-bereinigt, Handelswährung): ≥ 25 % 10; ≥ 15 % 8; ≥ 8 % 5; ≥ 0 2 |
| Wettbewerbsvorteile | 15 | KI | Stufe |
| Managementqualität | 10 | KI | Stufe |
| Institutionelle Beteiligung | 5 | Regel | 40–90 % 5; 20–40 % oder > 90 % 3; < 20 % 1 (Yahoo) |
| Short Interest | 5 | Regel | ≤ 3 % 5; ≤ 6 % 4; ≤ 10 % 2; ≤ 20 % 1 (Yahoo) |
| Skalierbarkeit | 10 | KI | Stufe |
| Langfristige Branchentrends | 10 | KI | Stufe |
| Relevanz der Produkte | 10 | KI | Stufe |
| Organisation und Talent | 5 | KI | Stufe |

Grenzen der Vergleichbarkeit: Das Ergebnis ist nicht mit Tom Nashs eigener Bewertung vergleichbar.

## 10. Valuation Score (100 Punkte) und Szenarien

**Multiplikatoren** (Basis letztes Geschäftsjahr; Fundamentaldaten bei abweichender Berichtswährung mit aktuellem
Devisenkurs umgerechnet):

| Kennzahl | Formel |
|---|---|
| KGV | Marktkapitalisierung / Nettoergebnis |
| EV | Marktkapitalisierung − Nettofinanzposition |
| EV/Umsatz, EV/EBIT | EV geteilt durch Umsatz bzw. operatives Ergebnis |
| Kurs/FCF, FCF-Rendite | Marktkapitalisierung / FCF bzw. Kehrwert |
| PEG | KGV ÷ (Umsatzwachstum × 100) |
| Forward-KGV | Yahoo, Analystenkonsens („geschätzt“) |

EBITDA wird nicht berechnet, weil Abschreibungen nicht einheitlich verfügbar sind.

**Szenarien** (5 Jahre, je konservativ / Basis / optimistisch, alle Annahmen am Ergebnis):

| Annahme | Regel |
|---|---|
| Startwachstum | Basis = Mittel aus Jahreswachstum und 3-J.-CAGR, begrenzt auf −10 % … 50 %; konservativ × 0,5; optimistisch × 1,4 (≤ 80 %); fällt linear auf 4 % im Jahr 5 |
| Ziel-FCF-Marge | profitabel: Ø FCF-Marge (× 0,8 / × 1 / × 1,2, höchstens 60 % der Bruttomarge, nie unter Basis); sonst 15 / 25 / 35 % der Bruttomarge |
| Kurs/FCF im Jahr 5 | 15 / 22 / 30 |
| Verwässerung p. a. | Ø der letzten Jahre (0–8 %) +1 / 0 / −1 Pp. |
| Nettofinanzposition | unverändert (vereinfacht) |
| Diskontierung | 9 % p. a. (Annahme) |

**Rechenweg:**

- Wert je Aktie_5 = (FCF_5 × Multiplikator + Nettofinanzposition) / Aktien_5.
- Rendite p. a. = (Wert_5 / Kurs)^(1/5) − 1.
- Barwert = Wert_5 / 1,09^5.

Das **eingepreiste Startwachstum** ist das g, bei dem der Barwert im Basismodell dem Kurs entspricht (Bisektion,
−30 % … +150 %). Liegt es außerhalb dieser Spanne, wird das angezeigt.

**Valuation Score:**

| Kriterium | Max | Regel |
|---|---|---|
| Bewertungsniveau | 30 | profitabel: FCF-Rendite ≥ 6 % 30; ≥ 4 % 24; ≥ 2,5 % 16; ≥ 1,5 % 9; ≥ 0,5 % 4; sonst EV/Umsatz ≤ 2 30; ≤ 4 22; ≤ 7 14; ≤ 12 7; ≤ 20 3 |
| Wachstum relativ zur Bewertung | 25 | PEG ≤ 1 25; ≤ 1,5 19; ≤ 2 12; ≤ 3 5 (ohne positives KGV: EV/Umsatz je Wachstumspunkt ≤ 0,2 25 …) |
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

## 14. KI-Integration

Es gibt keine zweite Infrastruktur: Aufrufe laufen über `AiGateway.generate` (Claude oder Gemini laut Einstellung),
mit JSON-Modus.

**Unternehmensanalyse** (eine Anfrage je Analyse):

- Eingabe: Firmendaten, „Item 1. Business“ (S1), berechnete Kennzahlen (K), Unternehmensprofil (Y) und bis zu 8
  Meldungen (N1…).
- Ausgabe: Geschäftsmodell, These mit Treibern, Gegenhypothese, 12 qualitative Kriterien (Stufe, Begründung, Belege,
  Gegenargumente, Fehlendes, Konfidenz), Meilensteine, Unsicherheiten.

**Prüfung** (`ai.validate`):

- Längen, Aufzählungen und Stufen 0–4 werden geprüft.
- Unbekannte Quellen-IDs werden verworfen und gezählt.
- URLs der KI werden nie übernommen.
- Sätze mit Zahlen ab 3 Ziffern, die in keiner Quelle stehen, werden entfernt und gezählt.
- Meilenstein-Schwellen außerhalb plausibler Bereiche werden nicht maschinell geprüft.
- Fehlen These, Gegenhypothese oder Geschäftsmodell, wird die Antwort verworfen; die Analyse wird dann ohne KI-Teil
  gespeichert.

**Prompt-Injection:** Quellen stehen als JSON-Zeichenketten im Nutzerteil. Der Systemtext erklärt sie zu
unvertrauenswürdigen Daten; die Antwort wird nur als Text angezeigt.

## 15. KI-Kosten

- Die Discovery hat eigene Grenzen zusätzlich zum Gateway-Budget: `discovery.ai_calls_per_day` (Standard 6) und
  `discovery.ai_calls_per_month` (60), protokolliert in `disc_ai_usage`.
- Die Ausgabelänge ist begrenzt (`discovery.ai_max_output_tokens` 6000), Quellen werden gekürzt (9.000 Zeichen je
  Quelle).
- Es gibt keinen Anbieter- oder Modellwechsel; Pausen und Kontingentfehler des Gateways gelten.
- Bei Fehler, Zeitüberschreitung oder erschöpftem Budget wird die Analyse ohne KI-Teil gespeichert und der Grund
  angezeigt.
- Eine manuelle Einzelanalyse ist jederzeit möglich, aber an dieselben Grenzen gebunden.
- Automatische Läufe analysieren höchstens `discovery.auto_analyse_per_run` Unternehmen.
- Unveränderte Eingaben erzeugen keine neue Version und keinen KI-Aufruf (Eingabe-Hash aus Kennzahlen, Quellen,
  Kurs-Tag sowie Prompt- und Berechnungsversion).

## 16. Datenschutz

- An die KI gehen nur öffentliche Unternehmensdaten.
- Nie an die KI gehen Konten, Wallets, Transaktionen, Mengen, Werte oder Gewichte; ein Test prüft die Nutzlast.
- An die SEC gehen nur CIK bzw. feste Pfade; an Yahoo nur Ticker.
- Portfolio-Fit und Simulation werden ausschließlich lokal berechnet.

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

## 18. Tests

`tests/test_discovery.py` mit 21 Tests auf synthetischen Daten, ohne Netzwerk und ohne echte KI. Geprüft werden:

- **Fundamentaldaten:** Herkunft, Lücken, Restatements, `as_of`, negative Werte, Split nur mit Nachweis (Sprung beim
  Börsengang bleibt), jüngstes Quartal mit Wechsel des XBRL-Elements, gemischte Währungen.
- **Identität:** Ticker, Name, Präfix, mehrdeutig, privat, Mehrfachlisting, falscher Ticker zum Namen, SPAC.
- **Scorecards:**
  - Summe = Score, reproduzierbar, Maximalwerte;
  - Kappung ohne Beleg und bei niedriger Konfidenz;
  - ohne KI unvollständig;
  - junge Unternehmen bleiben bewertbar.
- **Bewertung:** Szenarien geordnet, Annahmen vorhanden, eingepreistes Wachstum reproduziert den Kurs, fehlende Daten
  und Währungen.
- **KI:**
  - erfundene Quellen und unbelegte Zahlen werden entfernt;
  - ungültige Antworten werden abgelehnt;
  - Injection-Text bleibt Daten;
  - Datenschutz der Nutzlast;
  - Budgetgrenze;
  - Zeitüberschreitung führt zu einer Analyse ohne KI.
- **App-Ebene:**
  - Aufnahme, Analyse, Detailseite (alle Abschnitte), stabiler Ticker-Link;
  - Version unverändert bzw. erzwungen, Vergleich;
  - Status, Watchlist, Radar-Filter und -Sortierung, Einstellungen, Export;
  - Discovery-Lauf mit Ereignis-Dubletten, privaten und mehrdeutigen Namen;
  - Meilensteinprüfung.
- **Portfolio-Fit:** leeres Portfolio, Beispielportfolio, bereits gehalten, 50-%-Kauf, keine Buchung.
- **Sonstiges:** Migration ohne Revisionstrigger; Diagnose-Datenstand nach Analyse unverändert.

**Historische Fallstudie** (Phase N, nur regelbasierte Kriterien, Daten bis zum Stichtag eingereicht über
`as_of`, ohne KI und ohne damalige Kurse):

| Unternehmen, Stichtag | Daten | Wachstum | Brutto- / op. Marge | Quality | Growth | Nash (Regel) | Entwicklung danach |
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

## 19. Bekannte Grenzen

- Fundamentaldaten nur für SEC-registrierte Emittenten. Europäische und asiatische Unternehmen ohne SEC-Bericht
  bleiben ohne Kennzahlen.
- Yahoo ist eine inoffizielle Schnittstelle. Marktkapitalisierung, Forward-KGV, institutioneller Anteil und Short
  Interest können fehlen oder veraltet sein.
- Identität ist konservativ: Kurznamen (z. B. „Google“ statt „Alphabet“) und Tochtergesellschaften werden oft nicht
  aufgelöst und nur gelistet.
- Discovery aus News hängt von der vorhandenen News-Analyse ab (KI an, Quellen konfiguriert). Ohne KI entstehen nur
  manuelle Kandidaten und Fundamentalsignale.
- Qualitative Kriterien (≈ 2/3 der Disruption-Punkte) gibt es nur mit KI. Ohne KI sind Disruption und
  Nash-inspirierte Scores unvollständig.
- Szenarien sind vereinfachte Multiplikatormodelle (konstante Nettofinanzposition, feste Multiplikatoren).
  Unternehmen mit hohen Bewertungen liegen oft außerhalb des erklärbaren Bereichs.
- Peer-Vergleiche der Multiplikatoren sind noch nicht umgesetzt (keine verlässliche Peer-Gruppe ohne Zusatzdaten).
- ETF-Durchschau fehlt (keine Bestandteildaten).
- Historische Validierung nur qualitativ (siehe 18).
