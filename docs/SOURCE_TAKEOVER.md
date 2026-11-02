# Quellenübernahme: API maßgeblich für ein Konto (ab 0.35.0, Pilot Bitpanda)

## Ausgangslage

Der kuratierte Import (z. B. Koinly) und die Synchronisierung mit der Börse beschreiben dieselben Vorgänge unabhängig
voneinander. Jede Abweichung (Gebühr, Zeitpunkt, Menge, fehlende oder zusätzliche Zeile) wurde bisher als Einzelkonflikt
entschieden. Wenn die API einer Börse die **gesamte** Historie liefert, ist das unnötig: Dann ersetzt die API den Import
für dieses Konto – in einer Entscheidung, mit Beweis und Rückgängig.

## Warum nur Bitpanda

Die Übernahme setzt voraus, dass der Abruf *belegt*, vollständig zu sein:

| Kriterium | Bitpanda | Binance |
| --- | --- | --- |
| Seitenende laut API (`has_next_page=false`) | ja | nein (Handelspaare aus bekannten Assets, Zeitfenster) |
| Bestand der Börse abrufbar | `/portfolio` | Spot ja, Earn/Futures/Margin/P2P/Unterkonten nicht |
| Historie lückenlos belegbar | ja (Pagination bis zum Anfang) | nein (Connector beschreibt eigene Grenzen) |

Seit 0.68.0 gibt es zwei Stufen: *belegt* (`takeover.PROOF`, Bitpanda) und *bestätigt* (`takeover.CONFIRM`: Binance, dazu alle Blockchain-Wallets) – dort erklärt der Nutzer die Historie für vollständig, und die Sicherung lautet „Bestand je Asset nicht schlechter als vorher“. Buchungen vor dem ersten Ereignis der Quelle bleiben; Steuerjahre werden vor/nach gemessen. Siehe `docs/MILESTONES.md` (0.68.0).

## Ablauf

1. **Vollabruf** (nur lesend) – alle Vorgänge ab Beginn und der beobachtete Bestand. Kein Abrufstand, kein Prüf-Stapel, keine
   Buchung. Ergebnis in `ds_takeover` (Migration 29; vorübergehender Zustand, nicht im Export).
2. **Beweis** – alle Punkte müssen erfüllt sein:
   * Abruf vollständig: Seitenende laut API, keine Lücke, Modus „vollständig“;
   * Auswertung der Quelle aktuell (Parser-Version), Abruf nicht älter als 24 h;
   * beobachteter Bestand vollständig und allen Assets zugeordnet;
   * alle Vorgänge eindeutig und buchbar (keine ungeklärten, keine ohne Zeitpunkt);
   * **die Vorgänge der API ergeben den beobachteten Bestand je Asset exakt** (Toleranz: Ledger-Staub 1e-12). Die
     Bestandswirkung wird aus denselben Zeilen berechnet, die der Import bucht (`CsvImportService._build`), nicht aus
     einer zweiten Nachbildung; bleibende Buchungen des Kontos zählen mit;
   * alle zu ersetzenden Buchungen lassen sich ausblenden.
   Die eigene Bestandsprüfung des Connectors (Vorgänge ↔ `/portfolio`) wird als Hinweis angezeigt.
3. **Vorschau** – Beweis, Bestand (jetzt · nach Übernahme · beobachtet), Wirkung: ausgeblendete und bleibende Buchungen,
   Import-Buchungen **ohne** Gegenstück in der API und API-Vorgänge ohne Gegenstück im Import (Warnsignale: Fehler im
   Import, Dublette, Lücke).
4. **Übernehmen** (Bestätigung nötig, läuft im Hintergrund):
   1. Import- und Dateibuchungen des Kontos ausblenden – Korrektur der Diagnose (`diag_decision`, Art „Quellenübernahme“);
   2. API-Vorgänge in einem eigenen Stapel auswerten (ohne Stichtag) und als Stapelaktion buchen;
   3. durch bleibende Transfers gedeckte API-Zeilen mit der bleibenden Buchung verknüpfen bzw. bewusst nicht übernehmen;
   4. **Nachprüfung:** Bestand des Kontos == beobachteter Bestand, sonst **vollständige Rücknahme** (Stapelaktion,
      Stapel, Ausblendung) – es bleibt nie ein halber Zustand.
5. **Rückgängig** – Übernahme als Ganzes: gebuchte API-Vorgänge zurücknehmen, ausgeblendete Buchungen wiederherstellen
   (verweigert, wenn spätere Korrekturen dieselben Buchungen berühren).

## Was ersetzt wird und was bleibt

| Buchung des Kontos | Behandlung |
| --- | --- |
| Import (kuratierter Import) und Dateiimporte der App (`csv:…`) | ausgeblendet, durch API-Vorgänge ersetzt |
| bereits aus der API gebucht (`sync:<anbieter>`) | bleibt (ist der Beleg selbst) |
| Transfer mit anderem Konto | bleibt (die Gegenseite liegt dort); die API-Seite ist durch ihn gedeckt und wird nicht doppelt gebucht |
| manuelle Buchungen, Transfers der App, Korrekturen der Diagnose | bleiben; müssen durch den Bestandsnachweis gedeckt sein |
| nicht ausblendbare (z. B. Teil einer Gruppe) | bleiben, mit Grund in der Vorschau |
| Import ohne API-Gegenstück, der eine belegte Lücke der API füllt (ab 0.83.0) | bleibt, mit Grund „füllt eine Lücke der API“ |

## Lücken der API (ab 0.83.0)

Bitpanda liefert je Vorgang den Saldo danach (`asset_balance_after`). `bitpanda.chain_gaps` rechnet ihn je Wallet und Asset
nach; springt der Saldo stärker, als die Vorgänge erklären, fehlt dort eine Bewegung (Fenster zwischen den beiden Vorgängen,
Betrag). Der Rest zwischen letztem Saldo und gemeldetem Bestand ist eine Lücke „bis zum Abruf“.

1. **Lückenfüller:** Import-Buchungen ohne Gegenstück mit gleichem Vorzeichen im Fenster (±36 h), deren Betrag den offenen
   Rest nicht übersteigt, bleiben stehen.
2. **Ausgleich:** Bleibt eine Differenz, wird sie nur mit Zustimmung je Asset gebucht (Parameter `adjust`): *belegt*, wenn
   die offenen Lücken sie genau erklären (je Lücke eine Buchung am Fensterende), sonst *nicht belegt* (eine Buchung zum
   Abruf). Kennung `ausgleich:<übernahme>:<asset>:<n>`, Bezeichnung „Ausgleich: Bewegung fehlt in der API“.
3. Zustimmung und Füller gehen in den Beweis-Token ein; Nachprüfung exakt; Rückgängig nimmt den Ausgleich mit zurück.

**Lücken der API prüfen (ab 0.84.0, `app/datasources/bitpanda_gaps.py`):** eigene Seite mit dem Befund aus dem
gespeicherten Vollabruf (Lücken je Asset, Import/API ohne Gegenstück nach Art und Jahr) und einer optionalen
Gegenprüfung mit der alten API (`/fiatwallets/transactions`, `/wallets/transactions`; laut Bitpanda bis 31.01.2027).
Nur lesend; das Ergebnis liegt in `app_state` (`bp_gapcheck:<id>`), ein einmalig eingegebener Schlüssel wird nicht
gespeichert.

**Spender:** Wo die API einen Wert nicht liefert, übernehmen die API-Zeilen EUR-Wert, Gebührenwert und Einordnung (Tag, nur bei
gleicher Buchungsart) aus der zugeordneten Import-Buchung (`CsvImportService._donate`). Wo die API selbst einen Wert liefert
(z. B. Kauf gegen EUR), gilt der Wert der API. Zuordnung der Gegenstücke: Anbieterkennung (UUID in Notiz/Quellkennung, Alias),
sonst Asset, Richtung, Menge (±2 %, Gebühren werden je Quelle verschieden ausgewiesen) und Zeit (±36 h).

## Grenzen (bewusst, in der Oberfläche sichtbar)

* Bewiesen werden **Mengen**, nicht Werte. EUR-Werte stammen aus der API, dem ersetzten Import oder aus Kursen – steuerlich
  relevante Unterschiede entstehen dort.
* Realisierte Ergebnisse je Jahr werden **vorab nicht simuliert** (die Neubuchung wird erst bei der Übernahme ausgewertet).
  Nach der Übernahme unter „Steuern“ prüfen; Rückgängig stellt den Vorzustand her.
* Ein exakter Bestand schließt zwei sich aufhebende Fehler nicht aus; die Listen ohne Gegenstück sind die Stichprobe dafür.
* Die Zuordnung der Gegenstücke (Spender, „ohne Gegenstück“) ist heuristisch und steuert nur Anzeige und Spender – nie den Beweis.
* Verifiziert mit dem Mock der Referenz (`tests/test_takeover.py`), nicht mit einem echten Konto; die erste Übernahme am
  echten Konto sollte mit der Vorschau und einer Sicherung beginnen.
