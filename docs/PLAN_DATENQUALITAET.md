# Plan: Quelle vor kuratiertem Import, PEAQ-Lücke, Import-Warnungen, einfachere Datenqualität

Stand 28.10.2026 · Version 0.66.0 (Paket G umgesetzt) · **Planung (Pakete H–K offen), nichts umgesetzt.** Grundlage: Code (0.65.0), `docs/SOURCE_TAKEOVER.md`,
`docs/RECONCILIATION.md`, zwei Screenshots und drei Abfragen bei Subscan. Was ich geprüft habe und was Annahme ist, steht je Abschnitt.

## 0. Zusammenfassung

| # | Anliegen | Befund (geprüft) | Empfehlung | Aufwand |
|---|---|---|---|---|
| A | PEAQ: „EVM Internal Tx“ fehlen (z. B. 10 718,83 und 1 052,5 PEAQ) | Der kostenlose Weg (PubFi) liefert nur Substrate-Bewegungen; interne EVM-Überweisungen gibt es nur über Subscans Etherscan-Route mit **bezahltem** Schlüssel. Die Subscan-Weboberfläche sperrt Programme ausdrücklich (HTTP 403). | Reihenfolge: Spike „gibt es eine kostenlose PubFi-Route?“ → CSV-Profil für den Subscan-Export → bezahlter Schlüssel (Code ist vorhanden). Scraper nur als letzte, riskante Stufe. | S + M |
| B | Börse/Wallet soll **immer** Vorrang vor dem kuratierten Import haben | Vorrang gibt es nur als Pilot **Bitpanda**, nur mit exaktem Bestandsbeweis, nur als Ganz-oder-gar-nicht je Konto; Wallets sind ausgeschlossen. Im Prüfstapel bleibt der Import maßgeblich. | Vorrang als **Regel je Konto und Zeitraum** (überlebt neue Importversionen), mit Vorschau, Schutz für manuelle Buchungen/Korrekturen und Rückgängig. Vorher ein reiner **Überlappungsbericht**, der die Wirkung auf die Bestandsabweichungen zeigt. | L |
| C | 14 Warnungen beim kuratierten Import | Alle 14 sind „Unerwartete Datei im Unterordner ignoriert: `kuration/…`“ – Prüfunterlagen des Kurators, kein Fehler. | Zu **einem** Hinweis bündeln, Ordner als Prüfunterlagen erkennen und archivieren. | S |
| D | Datenqualität zu umständlich, kein Weg zurück zur Übersicht | 4 Reiter, ~20 Unterseiten, **sechs** Teilsysteme (Diagnose, Klärung, Prüfstapel, Autopilot/Regeln, Sammelbearbeitung, Integrität); jedes leitet nach einer Aktion auf **seine eigene** Seite. | Ein durchgehender **Arbeitsmodus** („Der Reihe nach“): jede Aktion endet beim nächsten Fall bzw. wieder auf „Heute“. | M, später L |
| E | Manuell übernommene Buchungen erneut prüfen? | **Ja, sinnvoll**, aber nur mit Beleg (Befund nennt die Buchung) und nie automatisch. Heute kann man nur ganze Stapel (`reopen`) oder einzelne Stapelaktionen zurücknehmen, keine einzelne Buchung zurück in den Prüfstapel legen. | „Erneut prüfen“ je Buchung/Befund; Entscheidung bleibt beim Nutzer; Schutz gegen Schleifen. | M |

Reihenfolge der Umsetzung: **C → D1 → B1 → A-Spike** (alles klein bzw. nur lesend), dann **B2**, **A-Umsetzung**, **B3 (Wallets)**, **E**, **D2**.
Begründung: zuerst das, was sofort Ärger nimmt und nichts riskiert; dann messen, bevor der Vorrang Buchungen ausblendet.

---

## A. PEAQ: fehlende interne EVM-Überweisungen (Subscan)

**Ausgangslage (Code):** `app/datasources/chains/peaq.py` hat zwei Wege. *PubFi* (kostenloser Schlüssel): Subscan löst die 0x-Adresse in das Substrate-Konto
auf, abgerufen werden native Bewegungen – „EVM-Gasgebühren und ERC-20-Tokens fehlen“, die Bestandsprüfung zeigt die Differenz. *Subscan direkt*:
Etherscan-kompatible Route mit `txlist`, `txlistinternal`, `tokentx` (Adapter `PeaqEvmConnector`, vollständig inkl. interner Bewegungen).
Genau diese Lücke zeigt dein Screenshot (Reiter „EVM Internal Tx (2)“).

**Geprüft heute (Abfragen von hier, ohne Schlüssel):**
* `peaq.webapi.subscan.io/…` (die Schnittstelle der Weboberfläche): **HTTP 403** – „Your request has been blocked. If you want to use a program to access
  the API …“. Programmzugriff auf die Weboberfläche ist von Subscan **ausdrücklich nicht gewollt**.
* `peaq.api.subscan.io/…`: „Subscan API strictly requires an API key. Unauthenticated access is disabled.“
* Subscan-Dokumentation ([Pläne](https://support.subscan.io/doc-360177)): „no longer issues new free API keys“; kostenlos nur über PubFi mit Routen im
  `:free`-Variant. Ob es dort eine **EVM-Internal-Route** gibt, steht in der Dokumentation nicht; die Doku-Navigation führt einen Eintrag „EVM internal Tx“
  (Endpunkt nicht geprüft).

**Optionen**

| Stufe | Weg | Vollständig? | Kosten/Risiko | Aufwand |
|---|---|---|---|---|
| 1 | **Spike:** in Portfolia (mit deinem PubFi-Schlüssel) die Fähigkeiten-/OpenAPI-Liste von PubFi lesen und die Subscan-Routen mit `free_rate_limit` auf eine EVM-Internal-Route prüfen; bei Treffer Antwortschema aufzeichnen | ja, falls vorhanden | keine; ungewiss, ob es die Route gibt | S (½ Tag) |
| 2 | **CSV-Profil „Subscan-Export“** (Transfers, EVM-Transaktionen, EVM Internal Tx): Export aus der Weboberfläche bzw. kopierte Tabelle → vorhandener CSV-Import (Prüfstapel, Dubletten-Erkennung, Hash als Kennung) | ja für die exportierten Zeiträume, manuell | kostenlos, ToS-sicher; manueller Schritt je Aktualisierung | S–M |
| 3 | **Bezahlter Subscan-Plan** (Developer): Anbieter „Subscan direkt“ ist fertig verdrahtet, vollständig und automatisch | ja | laufende Kosten (Preis nicht geprüft) | 0 Code, nur Konfiguration |
| 4 | **Bestandssprung-Rekonstruktion über öffentlichen RPC:** `eth_getBalance` je Block (nur wenn der RPC historische Stände liefert), Sprünge ohne eigene Transaktion als „interne Überweisung in Block N“ buchen | nur Zugänge/Abgänge als Summe, ohne Gegenadresse/Parent-Hash | kostenlos; **ungeprüft**, ob die öffentlichen peaq-RPCs historische Stände liefern; viele Abfragen | M–L |
| 5 | **Scraper** (kopfloser Browser als Plugin im eigenen venv, Muster `plugins.py`/DKB-Fetcher) | grundsätzlich ja | Subscan **blockiert Programme** (403) – wahrscheinlich Verstoß gegen die Nutzungsbedingungen; bricht bei jeder Änderung/Cloudflare-Prüfung; Chromium im Plugin ≈ 150 MB, Systembibliotheken im Slim-Image ungeprüft | L, dauerhaft Pflege |

**Empfehlung:** 1 → 2 sofort (zusammen ≈ 1–2 Tage), 3 als Entscheidung für dich (Kosten), 4 nur wenn 1–3 ausscheiden. **Zum Scraper:** Du hast ihn ausdrücklich
gewünscht; ich plane ihn als Stufe 5 mit, empfehle ihn aber nicht als Erstes, weil Subscan Programmzugriff aktiv sperrt und er fragil und rechtlich
heikel ist. Entscheidung dazu bitte bewusst (siehe Fragen).

**Sofortlösung ohne Code:** Die beiden Buchungen als „Eigene Buchung“ erfassen (Typ Einzahlung, Konto der 0x-Adresse, Datum/Menge aus dem Screenshot,
Notiz mit Parent-Hash). Dann stimmt der Bestand bis zur Umsetzung.

**Abnahme:** Bestandsabweichung PEAQ der Adresse 0x1E27…D856 = 0; Dubletten-Erkennung verknüpft eine später per Abruf gelieferte Buchung mit der manuellen
(kein Doppelzählen); Test mit aufgezeichneter Antwort (Mock).

---

## B. Börse/Wallet hat immer Vorrang vor dem kuratierten Import

**Ausgangslage (Code/Doku):**
* **Quellenübernahme** (`app/datasources/takeover.py`, `docs/SOURCE_TAKEOVER.md`) ist der vorhandene Mechanismus: Import- und Dateibuchungen eines Kontos werden
  ausgeblendet (`tx_override`/Diagnose-Entscheidung „Quellenübernahme“), die API-Vorgänge gebucht, EUR-Werte/Gebührenwerte des Imports übernimmt ein „Spender“.
* **Einschränkungen:** (1) nur `PILOT = ("bitpanda",)`; (2) Bedingung ist der **exakte Bestandsbeweis** je Asset – scheitert er an einem Wert, ist die ganze
  Übernahme gesperrt; (3) Ganz-oder-gar-nicht je Konto, ab Beginn der Historie; (4) Wallets und Binance ausgeschlossen („Abruf belegt keine Vollständigkeit“);
  (5) im **Prüfstapel** bleibt die vorhandene Buchung maßgeblich (`docs/RECONCILIATION.md`: „Verknüpfen statt Zusammenführen: vorhandene Buchung bleibt unverändert“; Neues wird verknüpft oder ausgelassen).
* **Beobachtung (Annahme, noch nicht an deinen Daten belegt):** Deine Abweichungen bei Bitpanda stammen vermutlich aus Doppel-/Abweichungsfällen zwischen Import und API
  (Gebührenart, Rundung, Zeitpunkt). Warum die Übernahme bei dir nicht greift, sehe ich erst im Prüfbericht („Blocker“ der Vorschau) – das ist Schritt B1.
* **Risiko Importversionen:** Überlagerungen hängen an `tx_id`. Der Kurator liefert neue Versionen (v3 …, im ZIP liegt `txid_mapping_prev.csv`). Eine Regel
  „Konto X, Zeitraum Y“ ist dagegen unabhängig von der Kennung. Ob bestehende Ausblendungen eine neue Importversion überstehen, ist **zu testen** (B1).
* **Korrekturen des Kurators:** Der Import nennt „Korrekturen: 133 (davon Klasse A 73)“. Ersetzt man Import-Buchungen durch Quellbuchungen, gehen solche Korrekturen
  verloren (z. B. rekonstruierte Zugänge, umklassifizierte Erträge). Das braucht Schutz bzw. Sichtbarkeit.

**Zielbild – „Quellen-Vorrang“ als Regel je Konto:**
1. **Regel** = Konto + Quelle (Datenquelle des Kontos) + Zeitraum **ab dem ersten Ereignis der Quelle** (davor bleibt der Import). Gilt dauerhaft, auch für künftige Importversionen,
   und wird beim Aufbau des Ledgers ausgewertet (Überlagerung), nicht als Einzelentscheidungen je `tx_id`.
2. **Was ersetzt wird:** Import-/Dateibuchungen des Kontos im Zeitraum. **Was bleibt:** manuelle Buchungen, Transfers mit anderen Konten, Korrekturen der Diagnose,
   Buchungen mit Kurator-Markierung (`flag`/`tag` „reconstructed“, `opening_balance`, korrigiert) – jeweils in der Vorschau sichtbar.
3. **Spender** wie bisher: EUR-Werte/Gebührenwerte/Einordnung des ersetzten Imports gehen auf die Quellbuchung über, wo die Quelle sie nicht liefert.
4. **Belegstufen statt Alles-oder-nichts:**
   * *belegt* (Bestand der Quelle stimmt exakt): sofort anwendbar (wie heute);
   * *plausibel* (Abweichung klein, vollständige Seitenfolge/Blockhöhe): anwendbar mit ausdrücklicher Bestätigung und Liste „Import ohne Gegenstück“;
   * *unbelegt* (Quelle bekannt lückenhaft, z. B. Binance Earn/Futures, PEAQ ohne Internal Tx): nur Zuordnung/Verknüpfung, **kein** Ausblenden.
   Das beantwortet „immer Vorrang“ so weit, wie es Daten nicht beschädigt: ohne Vollständigkeitsbeleg würde das Ausblenden Zugänge streichen, die nur der Import kennt.
5. **Prüfstapel:** Trifft eine Quellzeile auf eine Import-Buchung des Kontos, ist die Standardentscheidung „Quelle übernehmen, Import ersetzen (Spender)“ statt
   „auslassen“; weiterhin mit Vorschau und Rückgängig je Aktion.
6. **Vorschau/Beweis/Rückgängig/Nachprüfung** wie bei der Quellenübernahme (alles-oder-nichts-Rücknahme bleibt).

**Schritte**
* **B1 (lesend, S–M): Überlappungsbericht.** Je Konto mit Datenquelle: Zeitraum der Quelle, Import-Buchungen im Zeitraum, davon mit/ohne Gegenstück, vorhergesagte Wirkung auf die
  Bestandsabweichung je Asset, Blocker der Quellenübernahme im Klartext. Zeigt, ob der Vorrang bei Bitpanda die Abweichungen wirklich beseitigt, bevor etwas ausgeblendet wird.
  Dazu der Test „Ausblendungen überstehen eine neue Importversion?“.
* **B2 (M–L): Vorrang-Regel** für Bitpanda und alle Börsen mit belegter Vollständigkeit (Belegstufen, Regel statt Einzelentscheidungen, Schutz der Kurator-Korrekturen, Prüfstapel-Standard).
* **B3 (M): Wallets** (Ketten mit Bestandsprüfung gegen RPC): Beleg = Ketten-Bestand stimmt mit den Vorgängen überein; PEAQ erst, wenn A die Internal Tx liefert.
* **Abnahme:** Bitpanda: Bestandsabweichungen je Asset 0 bzw. begründet; Rückgängig stellt den Vorzustand bit-genau her (Prüfsumme wie bei Sammelkorrekturen); neue Importversion
  ändert das Ergebnis nicht; Steuerjahre vor/nach der Anwendung als Vergleich (die Übernahme simuliert realisierte Ergebnisse heute **nicht** vorab – das gehört in die Vorschau, sonst ist
  der Vorrang steuerlich ein Blindflug).

**Risiken:** EUR-Werte stammen nach der Übernahme aus der Quelle/Kursen statt aus dem Import (steuerlich relevant, Mengen sind bewiesen, Werte nicht); zwei sich aufhebende Fehler bleiben unsichtbar;
Regeln müssen sichtbar und widerrufbar sein (eigene Liste „Aktive Vorrang-Regeln“).

---

## C. Warnungen beim kuratierten Import (14)

**Ursache (geprüft):** `app/importer/validate.py:181` meldet jede Datei in einem Unterordner einzeln (`zip_extra`). Der Kurator legt Prüfunterlagen unter `kuration/` ab
(`build.py`, `corrections.csv`, `checks_summary.csv`, `data_coverage.csv`, `holdings_reference_history.csv`, `source_inventory.csv`, `txid_mapping_prev.csv`, `inputs/user_statements.json` …).
Nur der Ordner `portfolia/` (Portfolia-eigene Zusatzdaten) ist bekannt. Das ist keine Fehlbedienung – nur Rauschen.

**Maßnahme:**
1. Dateien in Unterordnern **eines** Ordners zu **einem Hinweis** bündeln („14 Dateien in `kuration/` – Prüfunterlagen des Kurators, nicht ausgewertet“), als *Hinweis*, nicht als *Warnung*;
   nur wirklich Unerwartetes (z. B. ausführbare Dateien, zu große Dateien) bleibt Warnung.
2. Prüfunterlagen **archiviert** mitführen (wie `portfolia/`-Zusatzdaten, mit Größen-/Verhältnisgrenzen, ohne sie auszuführen) und in der Importhistorie einsehbar machen.
3. **Optional (Entscheidung):** `holdings_reference_history.csv` und `corrections.csv` auswerten – Erstere kann die **Bestandsreferenzen** der Diagnose speisen, Letztere die Kurator-Korrekturen
   kennzeichnen (nützlich für B: Schutz der Korrekturen). Nur mit Format-Beispiel; sonst nicht.
* **Abnahme:** Import der vorliegenden Datei zeigt 0 Warnungen und 1 Hinweis; Zip-Bomben-/Größenprüfungen unverändert; Test mit `kuration/`-Ordner und mit fremdem Unterordner.

---

## D. Datenqualität: einfacher bedienen

**Befund (Code):** Navigation `partials/dq_nav.html`: Reiter *Heute · Fälle · Belege · Automatik*, darunter ~20 Unterseiten (Alle Fälle, Der Reihe nach, Klärungsfälle, Diagnose-Befunde, Prüfstapel,
Sammelbearbeitung, Hinweise, Bestände, Abweichungen, Quellenabdeckung, Anschaffung, Cockpit, Prüfbericht, Autopilot, Regeln, Klärung …). Sechs Teilsysteme (Diagnose `web.py`, Klärung `clarify_web.py`,
Prüfstapel `csvimport/module.py`, Autopilot/Regeln `auto_web.py`, Bulk, Integrität) haben **je eigene Redirects** (`_back(...)` zeigt nach jeder Aktion auf die eigene Seite). Der „Arbeitsplan“ auf *Heute*
(`workplan.py`) ordnet nur A/B/C, führt aber nicht durch.
**Ursache für „komme nie wieder zur Übersicht“:** Es gibt keinen gemeinsamen Zustand „ich arbeite gerade die Liste ab“; jede Aktion springt in ihren Kontext.

**D1 – Schnelle Verbesserung (M):** *Arbeitsmodus*
* Einstieg „Jetzt klären“ auf *Heute* startet die Reihenfolge der Stufe A (vorhandene `queue.py`/„Der Reihe nach“).
* **Ein** Rücksprung-Zustand (serverseitig je Sitzung, kein Fehler bei abgelaufenem Zustand): nach *jeder* Aktion – egal in welchem Teilsystem – folgt **der nächste Fall der Liste**; ist sie leer, **Heute** mit
  Zusammenfassung („7 erledigt, 2 zurückgestellt, Bestand jetzt …“). Statt `_back(<eigene Seite>)` ein gemeinsamer `next_url(ctx)`.
* Immer sichtbar: Fortschritt („3 von 17“), „Zurück zur Übersicht“, „Überspringen“, „Rückgängig“ (letzte Aktion).
* Prüfstapel-Aktionen (`/journal/pruefung/*`, `/journal/csv/{bid}/…`) bekommen denselben Rücksprung; ihre Zeilen erscheinen als Aufgaben in der Liste, nicht als Sonderwelt.
* **Abnahme:** höchstens 3 Klicks je Fall; kein Pfad endet auf einer „fremden“ Seite; Test über alle `_back`-Ziele (jede Aktion landet auf Fall/Heute).

**D2 – Größere Vereinfachung (L, nach B/E):** ein Fallbegriff statt sechs: Diagnose-Befund, Klärungsfall und Prüfstapel-Zeile als **„Fall“ mit Lösungsaktionen**; Unterseiten für Experten
hinter „Technik“ (die Seite `/quality` versteckt Technisches schon), Reiter auf *Heute · Fälle · Belege* reduzieren, Autopilot/Regeln unter Einstellungen. Erst nach Erfahrung mit D1 festlegen.

---

## E. Manuell übernommene Buchungen erneut prüfen

**Frage:** Sollen sie bei einem erkannten Problem wieder auf den Prüfstapel? **Ja – mit Bedingungen.**
* **Warum sinnvoll:** Häufig wird eine Abweichung durch genau die Buchung verursacht, die man früher mit „übernehmen“ bestätigt hat (Dublette, falscher Betrag). Heute muss man dafür den ganzen Stapel zurücknehmen.
* **Heute (Code):** `CsvImportService.reopen` öffnet nur einen **rückgängig gemachten ganzen Stapel**; `import_action` erlaubt Rücknahme je **Stapelaktion**; Diagnose-Entscheidungen haben `reopen`/`undo`. Eine
  **einzelne** Buchung zurück in den Prüfstapel gibt es nicht.
* **Vorschlag:** In Befund/Fall bei jeder beteiligten Buchung deren **Herkunft** anzeigen („übernommen am … aus Stapel …, Zeile …“) und „**Erneut prüfen**“ anbieten:
  Variante 1 *markieren* (Buchung bleibt, Zeile erscheint wieder als offene Aufgabe), Variante 2 *zurücknehmen* (Buchung entfernt, Zeile wieder im Prüfstapel; Vorher-Zustand protokolliert, rückgängig).
* **Schutz:** nie automatisch; nur wenn ein Befund die Buchung **belegt** nennt; Entscheidungen, die der Nutzer bestätigt hat, werden nicht erneut hochgestuft, solange sich die Belege nicht
  ändern (Fingerabdruck wie bei „überholt“ in `rules.py`); Obergrenze je Lauf, Protokoll.
* **Offen/zu prüfen:** Ob jede übernommene Buchung ihre Ursprungszeile eindeutig kennt (Verknüpfung Buchung ↔ `csv_row`/`import_action`) – Schritt 1 der Umsetzung ist diese Abdeckungsprüfung an
  deinen Daten; fehlt sie, zuerst nachtragen.
* **Abnahme:** Befund → „Erneut prüfen“ → Zeile im Prüfstapel → Entscheidung → Befund aktualisiert; Rückgängig stellt die Prüfsumme wieder her.

---

## Reihenfolge und Releases (Vorschlag)

| Release | Inhalt | Warum jetzt | Aufwand |
|---|---|---|---|
| 0.66.0 | **C** Import-Hinweise · **D1** Arbeitsmodus mit Rücksprung | sofort spürbar, kein Datenrisiko | M |
| 0.67.0 | **B1** Überlappungsbericht (lesend) · **A-Spike** PubFi-Route · **A2** CSV-Profil Subscan | misst, bevor etwas ausgeblendet wird; schließt PEAQ manuell ab | M |
| 0.68.0 | **B2** Vorrang-Regel (Börsen), Prüfstapel-Standard | Kern deiner Anforderung | L |
| 0.69.0 | **A** gewählter Weg (Plan/Schlüssel/Scraper) · **B3** Wallet-Vorrang | braucht Entscheidung zu A | M |
| 0.70.0 | **E** „Erneut prüfen“ | setzt Herkunftsabdeckung voraus | M |
| 0.71.0 | **D2** Fälle vereinheitlichen | nach Erfahrung mit D1/E | L |

Vorgehen wie bisher: Test zuerst, ein Commit je Punkt, Volltest grün vor Push, Eintrag in `MILESTONES.md`. B2/B3 und E ändern Buchungen: dort **Sicherung vor Anwendung** (ZIP-Export bzw.
Datenbank-Sicherung), Vorschau mit Wirkung auf Bestand **und** Steuerjahre, Rückgängig mit Prüfsumme.

## Fragen an dich (Entscheidungen)

1. **Vorrang-Reichweite (B):** Reicht „immer Vorrang, wo die Vollständigkeit belegt oder bestätigt ist; sonst nur verknüpfen“? Alternativ „immer ausblenden“ – dann können Zugänge verschwinden,
   die nur der Import kennt (z. B. Binance Earn).
2. **PEAQ (A):** Hast du einen **PubFi-Schlüssel** in Portfolia (für den Spike)? Wäre ein **bezahlter Subscan-Plan** akzeptabel? Soll der **Scraper** trotz 403/Nutzungsbedingungen als
   Stufe 5 vorbereitet werden – oder reicht dir CSV-Export (Stufe 2)? Kannst du einen echten **Subscan-Export** (CSV) der Adresse bereitstellen (anonymisiert reicht)?
3. **Kurator-Dateien (C):** Nur archivieren, oder `holdings_reference_history.csv` und `corrections.csv` auswerten (Bestandsreferenzen, Schutz der Korrekturen)? Ein kleines Beispiel je Datei würde
   die Auswertung absichern.
4. **Erneut prüfen (E):** Bevorzugst du *markieren* (Buchung bleibt) oder *zurücknehmen* (Buchung weg, Zeile wieder offen) als Standard?
5. **Importversionen:** Wie oft kommt ein neuer kuratierter Import (v4 …), und bleiben `tx_id`s stabil? Das entscheidet, ob Ausblendungen je Kennung genügen oder Regeln nötig sind (ich plane Regeln).

## Annahmen und Grenzen

* Subscan-Abfragen oben wurden von dieser Umgebung aus mit **zwei** POST-Anfragen gemacht (ohne Schlüssel, nur Lesen, je eine an Web- und API-Adresse); Preise der Subscan-Pläne und die PubFi-Routenliste sind **nicht** geprüft.
* Die Wirkung des Vorrangs auf deine Bestandsabweichungen (Bitpanda) ist eine **Annahme** bis zum Überlappungsbericht (B1).
* Aufwände sind Schätzungen (S = Stunden, M = Tage, L = Wochen-Bruchteile) und hängen an den Antworten zu 2 und 3.
* Keine Steuerberatung: Beim Vorrang ändern sich EUR-Werte und damit realisierte Ergebnisse; die Simulation je Steuerjahr gehört in die Vorschau.

---

## Beschlüsse vom 28.10.2026 und Konkretisierung

**Entscheidungen des Auftraggebers:** Vorrang „wo Vollständigkeit belegt **oder bestätigt** ist, sonst nur verknüpfen“ (Ja) · kein PubFi-Schlüssel, kein bezahlter Subscan-Plan, kein Scraper,
kein CSV-Export möglich · Kurator-Dateien: Empfehlung (archivieren + erste Zeilen anzeigen) · „Erneut prüfen“: Standard **markieren** · Importversionen: Regeln statt Kennungen.
Zusätzlich: **Bitpanda und Binance haben eine vollständige Historie und sollen immer Vorrang haben; Wallets in der Regel auch.** Für Wallets mit Abruf-Grenze (z. B. DOT über PubFi: Relay Chain vor Block
32.196.153, Asset Hub vor Block 18.459.746) ist die Lücke **kostenlos** zu schließen.

**Neu geprüft (öffentliche RPCs, nur lesend):** peaq/OnFinality liefert historische Stände (Sprünge **+1 052,5** und **+10 718,83** PEAQ exakt wie im Subscan-Screenshot). Polkadot-Relay-Chain:
`rpc.polkadot.io` (Parity) und OnFinality liefern den Zustand bei Block 20 000 000, PublicNode nicht. Damit ist der **Kettenbestand-Abgleich** (Bestand je Block, Halbierung bis zum Sprung) für EVM-Ketten und
die Relay Chain ohne Anbieter-Schlüssel möglich; der Asset Hub ist ungeprüft (Probe im Programm, Fallback „nicht möglich“ mit Hinweis).

### Umsetzungsfolge (angepasst)

| Release | Inhalt |
|---|---|
| 0.66.0 | **C** Import-Hinweise (`kuration/` archivieren, erste Zeilen anzeigen) · **D1** Assistent (siehe UX-Konzept) |
| 0.67.0 | **A** Kettenbestand-Abgleich: Bestandssprünge ohne Buchung finden und als Aufgabe anbieten (EVM, DOT Relay Chain; Grenzen sichtbar) |
| 0.68.0 | **B** Quellen-Vorrang mit Bestätigung (Bitpanda, Binance; Wallets ab Abdeckungsbeginn), Prüfstapel-Standard „Quelle ersetzt Import“ |
| 0.69.0 | **E** „Erneut prüfen“ (markieren, optional zurücknehmen) |
| 0.70.0 | **D2** Fälle vereinheitlichen, Navigation straffen |

---

## UX-Konzept „Datenqualität einfach“ (verbindlich für D1/D2)

### Was heute schwer ist (aus dem Code, nicht vermutet)
1. **Kein durchgehender Weg:** Sechs Teilsysteme (Diagnose, Klärung, Prüfstapel, Autopilot/Regeln, Sammelbearbeitung, Integrität) leiten nach einer Aktion jeweils auf **ihre eigene Seite** zurück
   (`back`-Parameter mit unterschiedlichen Werten, 40+ eigene Redirects).
2. **Veraltete Ergebnisse verlangen Handarbeit:** Nach Änderungen meldet der Plan „Integritätsprüfung älter als die Daten“ und der Nutzer muss „Alles prüfen“ drücken – das ist das „neu starten“.
3. **Zu viele Begriffe und Orte:** 4 Reiter, ~20 Unterseiten, Fachwörter (Klärungsfall, Befund, Stapel, Autopilot, Regel) für dieselbe Sache: *etwas ist unklar, bitte entscheiden*.
4. **Zu viele Klicks je Fall:** Fall öffnen → „Vorschau“ → prüfen → „Übernehmen“ → zurück zur Liste suchen.

### Leitregeln
* **Eine Aufgabe auf einmal, ein Hauptknopf, immer ein Weg zurück.** Der Nutzer entscheidet; Portfolia schlägt vor und führt.
* **Nach jeder Aktion kommt die nächste Aufgabe** – nie eine fremde Seite. Ist nichts mehr offen: „Alles erledigt“ mit Zusammenfassung, dann Übersicht.
* **Nichts manuell neu starten:** Ergebnisse aktualisieren sich nach Änderungen von selbst.
* **Klare Sprache:** eine Bezeichnung („Aufgabe“) statt sechs; Fachbegriffe nur in „Details“.
* **Nichts passiert ohne Bestätigung, alles ist rückgängig** (bleibt wie bisher); Vorschau zeigt die Wirkung auf Bestand direkt in der Aufgabe.

### Bausteine (D1)
1. **Assistent** `GET /quality/assistent`: zeigt die nächste Aufgabe (Reihenfolge: Muss ich tun → Kann ich tun), kompakt: *Was ist los* (ein Satz) · *Empfehlung* mit **einem Hauptknopf**
   („Empfehlung ansehen und übernehmen“) · *Anders lösen* · *Kein Handlungsbedarf* · *Später* · *Überspringen* · *Details* (Belege eingeklappt). Kopfzeile: „Aufgabe 3 von 17“, Fortschrittsbalken,
   „Zur Übersicht“. Aufgabenquellen: Diagnose (Entscheidungsliste), Klärungsfälle, Prüfstapel (Stapel mit offenen Zeilen), Integritätsbefunde – vereinheitlicht als **Aufgabe**
   (Titel, Kurztext, Hauptaktion, Alternativen, Quelle).
2. **Flow-Modus ohne Umbau aller Routen:** Beim Start setzt der Assistent ein Cookie `dq_flow`. Eine Antwort-Schicht schreibt **erfolgreiche** POST-Weiterleitungen (303 bzw. `HX-Redirect`) innerhalb der
   Datenqualität (`/quality/*`, `/journal/pruefung*`, `/journal/csv/*`) auf `/quality/assistent` um und reicht `msg` weiter. Fehler (`err`, 4xx) bleiben auf der Seite, wo man sie beheben kann.
   Die Übersicht („Heute“) und „Zur Übersicht“ löschen das Cookie. Fehlt das Cookie, verhält sich alles wie bisher.
3. **Sichtbarer Modus:** Solange `dq_flow` gilt, zeigt die Navigation der Datenqualität eine Leiste „Assistent aktiv · Weiter · Beenden“ – man verliert den Weg nie.
4. **Ein Klick statt drei:** Die Aufgabenkarte rechnet die Vorschau der empfohlenen Lösung **für diese eine Aufgabe** (wie `diagnosis_plan`) und zeigt „Wirkung: …“ sowie den Knopf „Übernehmen und weiter“
   mit dem Prüfsummen-Token; ohne Empfehlung bleibt der Weg über die Vorschau.
5. **Selbstaktualisierend:** Nach Datenänderungen läuft der Prüflauf (Diagnose, Integrität, Klärung) automatisch nach (entprellt, nicht öfter als alle 2 Minuten, nicht während eines Imports);
   „Alles prüfen“ bleibt als Handknopf, ist aber nie nötig. Der Plan zeigt statt der Aufforderung „… älter als die Daten“ „wird aktualisiert …“.
6. **Heute:** oben ein Block „**N Aufgaben · ca. M Minuten** – Weiter“ (Assistent) statt Listenansicht; die A/B/C-Liste bleibt darunter als Überblick.
7. **Messbar:** ≤ 2 Klicks je Standardaufgabe; jede Aktion im Assistenten endet in der nächsten Aufgabe oder „Alles erledigt“; Test über alle Redirect-Ziele im Flow-Modus.

### D2 (später, nach Erfahrung)
Reiter auf *Heute · Fälle · Belege* kürzen, Automatik unter Einstellungen; Diagnose-Befund, Klärungsfall und Prüfstapel-Zeile als **eine** Fallansicht; Expertenseiten hinter „Technik“.

---

## Assistent 2.0 – „Geführte Klärung“ (Planung 29.10.2026; Umsetzung ab 0.72.0)

Beschlüsse: Hinweise nur auf Wunsch; Fortschritt im Browser; Bezeichnung „Buchungen prüfen“.

Anlass: Rückmeldung zu 0.66–0.70 mit Screenshot. Der Assistent ist „aktiv“, aber auf der Seite steht nicht, **was jetzt zu tun ist**; darunter stehen die üblichen
Korrekturvorschläge der Expertenseiten. Für jemanden ohne Vorwissen ist das weiterhin nicht bedienbar.

### 1. Ursachen (aus dem Code, nicht vermutet)

| # | Ursache | Wo |
|---|---|---|
| U1 | Der Assistent zeigt nur **Einzelbefunde der Diagnose** selbst. Klärungsfälle, Prüfstapel und Integritätsbefunde **leitet er auf die Expertenseiten um** (`assistant_web.py`: `item.kind != "befund"` → 303 auf `item.href`). Dort fehlt jeder Hinweis, welche Aufgabe gemeint ist und was man tun soll. | `app/diagnosis/assistant_web.py` |
| U2 | **Doppelt gezählt:** „14 kritische Befunde der Integritätsprüfung“ sind überwiegend **dieselben** Diagnose-Befunde (`integrity.from_findings`), die zusätzlich als Einzelaufgaben im Plan stehen. „Heute“ zeigt A 14 / B 36, der Assistent „37 Aufgaben“ – zwei verschiedene Zählungen derselben Sache. | `workplan.py` (Gruppe `integritaet`), `integrity.py` |
| U3 | Die Leiste „Assistent aktiv“ erschien auch auf der Übersicht, obwohl der Modus dort endet (behoben in 0.71.1). Sie nennt die laufende Aufgabe nicht. | `base.html`, `partials/dq_flow.html` |
| U4 | **Lösungen statt Fragen:** Die Karte zeigt technische Lösungen („M1 ausblenden“, „Ausgleichsbuchung“, „als im Import enthalten markieren“). Ein Laie kann nicht beurteilen, welche richtig ist – er weiß aber, ob er *eine* oder *zwei* Einzahlungen gemacht hat. | `recommend.py` (Option-Texte) |
| U5 | **Sechs Begriffe für dieselbe Sache:** Befund, Fall, Klärungsfall, Prüfstapel-Zeile, Integritätsbefund, Hinweis. Dazu Stufen A/B/C, „belegt/wahrscheinlich/Verdacht“. | gesamte Datenqualität |
| U6 | **Reihenfolge nach Schwere, nicht nach Logik:** Ein offener Prüfstapel (fehlende Buchungen) verursacht Bestands- und Steuerbefunde; wer diese zuerst bearbeitet, arbeitet doppelt oder falsch. | `workplan.py` (Sortierung A → B) |
| U7 | Nach einer Antwort sieht man nicht, **was sie bewirkt hat** (wie viele Aufgaben dadurch verschwunden sind) und kann sie nicht direkt zurücknehmen. | Assistent |

### 2. Ziel und Messgrößen

* Ein Mensch **ohne Fachwissen** kommt von „37 offene Punkte“ bis „fertig“, ohne eine andere Seite als den Assistenten zu sehen.
* Je Aufgabe **eine verständliche Frage**, die er aus eigener Erinnerung oder einem genannten Beleg beantworten kann; „Weiß ich nicht“ ist immer eine gültige Antwort.
* **Messbar (Abnahme):** (a) keine Weiterleitung aus dem Assistenten auf eine Expertenseite; (b) Standardaufgabe ≤ 2 Klicks; (c) jede Seite des Assistenten zeigt Aufgabe *n von m*,
  Thema und Frage im sichtbaren Bereich ohne Scrollen (Mobil: 390 px Breite); (d) eine Zählung überall (Heute = Assistent); (e) jede Antwort ist auf der nächsten Karte rückgängig zu machen;
  (f) Begriffe auf den Assistent-Seiten nur: *Aufgabe, Buchung, Konto, Bestand* (Fachbegriffe ausschließlich unter „Details“).

### 3. Bildschirm (eine Vorlage für alle Aufgaben)

```
┌───────────────────────────────────────────────────────────────────────────────┐
│ Datenqualität prüfen        Aufgabe 4 von 31 · ca. 25 Min. · [Pause]          │
│ ███████████░░░░░░░░░░░  Thema 2 von 5: Doppelte Buchungen (2 von 6)           │
├───────────────────────────────────────────────────────────────────────────────┤
│ ✓ Gespeichert: „Zwei Einzahlungen“ – 2 weitere Aufgaben dadurch erledigt [↶]  │  ← nach jeder Antwort
├───────────────────────────────────────────────────────────────────────────────┤
│ Bitpanda · 12.03.2025                                                         │
│ Hast du am 12.03.2025 einmal oder zweimal 0,5 BTC auf Bitpanda erhalten?      │  ← DIE Frage (Alltagssprache)
│                                                                               │
│ Portfolia sieht diese Gutschrift zweimal: im Koinly-Import und im Abruf von   │  ← Was ist los (1–2 Sätze, Zahlen)
│ Bitpanda. Zählt sie doppelt, ist dein Bestand um 0,5 BTC (≈ 31.000 €) zu hoch.│
│                                                                               │
│ (●) Einmal – eine ist doppelt            Empfohlen: gleiche Menge, 2 Min.     │  ← Antworten statt Lösungen,
│     → Bestand BTC 1,5 → 1,0 (passt dann zu Bitpanda)                          │    Wirkung direkt darunter
│ ( ) Zweimal – beide sind echt                                                 │
│     → nichts wird geändert, die Aufgabe ist erledigt                          │
│ ( ) Weiß ich nicht                                                            │
│     → später; Tipp: Bitpanda-App → Verlauf → 12.03.2025                       │
│                                                                               │
│ [ Antwort speichern und weiter ]                       Überspringen           │
│ ▸ Belege und Details (für Fortgeschrittene)                                   │  ← eingeklappt: Buchungen, Hashes,
└───────────────────────────────────────────────────────────────────────────────┘    Expertenseite als Link
```

* Kein Kopf der Expertenseiten, keine Reiter, keine Navigation der Datenqualität auf dieser Seite (Fokus); oben links „Pause“ = zurück zur Übersicht, Fortschritt bleibt.
* Eingaben (EUR-Wert, Datum) erscheinen nur, wenn die gewählte Antwort sie braucht, mit Vorschlag („Tageskurs am 12.03.2025: 31.240 € – übernehmen?“).
* Mehrere gleiche Fälle: nach der Antwort „Es gibt 5 weitere Fälle dieser Art (gleiche Quelle, gleiches Muster). [Für alle gleich beantworten] [Einzeln]“ – mit Vorschau der Anzahl.

### 4. Ein Aufgabenmodell für alles (Adapter statt Weiterleitung)

`Task` (neu, `app/diagnosis/guide.py`): `id`, `theme`, `question`, `context` (1–2 Sätze), `answers[]` (je Antwort: Text, Wirkung als Satz, `action` = vorhandene Aktion mit Parametern,
`needs` = nötige Eingaben, `recommended`, `why`), `evidence` (eingeklappt), `similar` (Schlüssel für „für alle gleich“), `source` (Befund/Fall/Stapelzeile/Prüfung).
Jede Quelle bekommt einen **Adapter**, der ihre vorhandene Aktion aufruft – es wird **keine** neue Buchungslogik geschrieben:

| Quelle heute | Adapter → Aufgabe | Aktion (vorhanden) |
|---|---|---|
| Diagnose-Befund (je Art) | Frage je Befundart (Katalog unten) | `actions.build_plan` + `apply_plan`, `mark`/`dismiss` |
| Klärungsfall | Frage zum Fall („Gehören diese 3 Buchungen zu einer Überweisung von Binance zu Ledger?“) | `clarify` übernehmen/zurückstellen/Bestand |
| Prüfstapel | **zwei** Aufgabentypen: „N sichere Vorgänge übernehmen?“ (ein Klick, Vorschau als Satz) und je unklarer Zeile eine Frage | `csvimport.batch.plan/execute` (include/skip/link) |
| Integritätsprüfung | nur Befunde **ohne** Diagnose-Befund (Invarianten, Steuer, Kurse, Historie) werden eigene Aufgaben; die übrigen sind bereits Befund-Aufgaben → **keine Doppelzählung** | Diagnose-Aktion bzw. Hinweis |
| Bestandsabweichung mit Kettenbestand | „Laut Blockchain kamen am 27.04.2025 1.052,5 PEAQ an, die nicht gebucht sind. Kennst du diesen Zugang?“ → Airdrop/Belohnung/Einzahlung/weiß nicht | `chain_steps` |
| Vorgemerkte Buchungen | „Stimmt diese übernommene Buchung?“ → behalten / zurück in den Prüfstapel | `recheck` |

Die Expertenseiten bleiben unverändert (für Fortgeschrittene, über „Details“ und die Reiter erreichbar). Der Flow-Modus (Cookie, Umschreiben der Weiterleitungen) wird für den
Assistenten **nicht mehr gebraucht** und nur noch für den Absprung aus „Details“ behalten (Leiste dann mit Titel der Aufgabe und „Zurück zu Aufgabe 4“).

### 5. Reihenfolge: Themen, die Ursachen vor Folgen behandeln

1. **Neue Vorgänge übernehmen** (Prüfstapel) – fehlende Buchungen erzeugen sonst Scheinfehler in allen folgenden Themen.
2. **Doppelte Buchungen** (Dubletten, Import ↔ Abruf, Quellenvorrang je Konto als *eine* Frage: „Soll Bitpanda für dein Bitpanda-Konto maßgeblich sein?“).
3. **Überweisungen zwischen eigenen Konten** (Transfers, Klärungsfälle).
4. **Bestände abgleichen** (Bestandsabweichungen, Kettenbestand, fehlende Zugänge/Abgänge, Verluste).
5. **Werte und Kurse** (fehlende Kurse, Asset-Zuordnung, Migrationen).
6. **Hinweise** (optional, gesammelt bestätigen).

Nach jeder Antwort wird neu geplant (der Prüflauf läuft schon automatisch nach, 0.66); erledigte Folgeaufgaben verschwinden, die Meldung nennt ihre Zahl (U7). Innerhalb eines
Themas: größte Wirkung (EUR) zuerst. Stufen A/B/C bleiben nur auf den Expertenseiten.

### 6. Fragenkatalog (Beispiele je Befundart; Texte werden mit Zahlen/Namen gefüllt)

| Art | Frage | Antworten (→ vorhandene Aktion) |
|---|---|---|
| Dublette | „Hast du am {Datum} einmal oder zweimal {Menge} {Asset} auf {Konto} erhalten/gesendet?“ | einmal → schwächere Buchung ausblenden · zweimal → als geprüft · weiß nicht → später |
| Möglicher Transfer | „Hast du am {Datum} {Menge} {Asset} von {Konto A} an {Konto B} geschickt?“ | ja → Transfer verknüpfen · nein, an jemand anderen → als Abgang belassen · weiß nicht |
| Bestandsabweichung | „{Konto} zeigt {beobachtet} {Asset}, Portfolia rechnet {berechnet}. Was stimmt?“ | Kettenbestand/Quelle stimmt → Sprünge buchen bzw. Quellenvorrang · Portfolia stimmt (z. B. noch nicht abgerufen) → später · ich weiß nicht → Tipp |
| Ungeklärter Abgang | „{Menge} {Asset} sind am {Datum} von {Konto} abgegangen, ohne Ziel. Was war das?“ | an eigenes Konto (Auswahl) · verkauft/bezahlt · verloren/gestohlen · weiß nicht |
| Fehlender Kurs | „Für {Asset} gibt es keinen Kurs. Ist der Token noch etwas wert?“ | ja, CoinGecko-Suche mit Vorschlägen · nein, wertlos/Spam → als geprüft · weiß nicht |
| Migration | „Wurde {alt} in {neu} umgetauscht (gleiches Projekt, neuer Name)?“ | ja → Migration · nein, eigener Kauf → als geprüft |
| Prüfstapel sicher | „{N} neue Vorgänge von {Quelle} sind eindeutig. Übernehmen?“ | ja (Vorschau: Bestände danach) · einzeln ansehen |
| Prüfstapel unklar | „Ist {Vorgang} dasselbe wie die Buchung vom {Datum}?“ | ja → verknüpfen · nein → übernehmen · nicht buchen |

Der Katalog liegt als Daten neben `recommend.py` (Frage, Antworttexte, Zuordnung Antwort → Option); fehlt für eine Art ein Eintrag, nutzt der Adapter die heutige Empfehlung
als Antwort „Empfehlung übernehmen“ plus „Weiß ich nicht“ (nie eine Sackgasse).

### 7. Sicherheit und Nachvollziehbarkeit (bleibt wie heute, sichtbarer)

* Jede Antwort mit Wirkung läuft über die vorhandene Vorschau/Übernahme mit Prüfsummen-Token; „Rückgängig“ auf der nächsten Karte und im Protokoll „Was ich geändert habe“.
* „Für alle gleich beantworten“ nur bei identischem Muster (gleiche Befundart, Quelle, Empfehlung, Belegstufe) und mit Anzahl-Vorschau; einzeln rücknehmbar.
* „Weiß ich nicht“ ändert nie Daten; die Aufgabe kommt nach 7 Tagen oder bei neuen Belegen wieder.

### 8. Umsetzung in Schritten (je Schritt ein Release, Test zuerst, Volltest)

| Release | Inhalt | Aufwand |
|---|---|---|
| 0.72.0 | `Task`-Modell und Adapter für Diagnose-Befunde inkl. Fragenkatalog für die 6 häufigsten Arten; neue Assistent-Seite (Vorlage oben), Themen-Reihenfolge, eine Zählung (Heute = Assistent), Integritäts-Doppelzählung entfernt, Meldung „N weitere erledigt“ + Rückgängig | L |
| 0.73.0 | Adapter Prüfstapel (sichere Vorgänge als eine Frage, unklare Zeilen einzeln) und Klärungsfälle – keine Weiterleitung mehr aus dem Assistenten | L |
| 0.74.0 | „Für alle gleich beantworten“, Quellenvorrang als Frage je Konto, Kettenbestand als Frage, Protokoll „Was ich geändert habe“; Flow-Modus nur noch für „Details“ | M |
| 0.75.0 | Feinschliff nach deiner Rückmeldung (Texte, Reihenfolge), Mobil-Ansicht, Bedientest (Abnahmekriterien 2a–f als automatische Tests) | M |

### 9. Annahmen, Grenzen, offene Punkte

* Fragen in Alltagssprache setzen voraus, dass sich jede Befundart auf eine Erinnerungsfrage abbilden lässt; für seltene Arten bleibt „Empfehlung übernehmen / Weiß ich nicht“.
* Die Logik der Korrekturen ändert sich nicht – nur die Bedienung. Damit bleibt das Risiko für funktionierenden Code gering; die Expertenseiten bleiben unverändert.
* Zu klären vor 0.72.0: (1) Sollen „Hinweise“ (Stufe C) im Assistenten überhaupt erscheinen oder nur auf Wunsch? (2) Pause/Fortsetzen über Geräte hinweg (Fortschritt in der
  Datenbank statt im Browser) – nötig? (3) Wortwahl „Datenqualität prüfen“ vs. „Buchungen prüfen“.
