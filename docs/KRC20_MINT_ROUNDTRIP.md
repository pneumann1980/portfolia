# KRC-20-Mint (Commit/Reveal) im Verlauf: Netto-Gebühr statt Verkauf + Neukauf

## Ursache

Eine KRC-20-Inskription (Mint) besteht aus zwei Kaspa-Transaktionen **derselben Wallet**: der *Commit* sendet KAS an eine
Skript-Adresse (P2SH), der *Reveal* spendet diese Ausgabe und gibt den Rest an die eigene Adresse zurück. Koinly kennt die
Skript-Adresse nicht und bucht beide Transaktionen als **Abgang ohne Gegenbuchung** und **neuen Zugang** mit verschiedenen
Hashes. Der Bestand stimmt danach (−Brutto + Rückfluss = −Gebühr), aber:

* jeder Commit ist ein Verkauf ohne Erlös (realisiertes Ergebnis, Verbrauch alter Lots),
* jeder Reveal ist eine neue Anschaffung (frisches Anschaffungsdatum, neuer Einstand) → Haltefrist/FIFO verfälscht,
* die Abgänge sind einzeln meist < 50 EUR und tauchen deshalb als „kleine Abgänge“ nirgends auf – es ist nur eine Zahl.

Im Export vom 17.10.2026 (Wegwerf-Kopie, nur lesend): ≈ 1,3 Tsd. Paare, typische Differenz 1,00003165 KAS (Mint-Gebühr), Median
7 s zwischen Commit und Reveal, ≈ 520 EUR scheinbarer realisierter Gewinn aus diesem Rauschen.

Die Datenquelle (`kaspa.py`) fasst Commit/Reveal bei **synchronisierten** Daten bereits zu einem Vorgang zusammen
(„KRC-20 (Commit/Reveal)“, KAS-Anteil nur Gebühr). Betroffen ist daher der **Koinly-Verlauf**.

## Erkennung (`audit.mint_roundtrips`, Befundart „Abgang mit Rückfluss auf demselben Konto“)

Je Konto und Asset, chronologisch: ein Zugang wird dem ältesten offenen Abgang zugeordnet, wenn

* Abgang und Zugang dasselbe Konto und Asset betreffen (nicht Fiat), beide ohne Tag/neutral und ohne eigene Gebührenangabe,
* der Zugang höchstens 10 Minuten nach dem Abgang liegt, kleiner als der Abgang ist und mindestens 90 % davon beträgt,
* alle in Frage kommenden Abgänge dieselbe Menge haben (austauschbar); sonst entscheidet nur eine deutlich passendste
  Differenz zur typischen Differenz des Kontos – sonst bleibt der Zugang **unzugeordnet** (mehrdeutig),
* weder Abgang noch Zugang einen identischen Zwilling haben (Dublette, anderer Befund).

Beleglage: **belegt**, wenn alle Paare denselben Transaktions-Hash tragen (eine Onchain-Transaktion mit Wechselgeld);
**wahrscheinlich**, wenn ≥ 3 Paare auf demselben Konto/Asset das Muster zeigen (Hashes verschieden – wie bei
Commit/Reveal); **Verdacht**, wenn es nur ein oder zwei Paare gibt. Ein Befund je Konto, Asset und Monat (stabile
Kennung beim Abarbeiten). Die Paare sind nicht mehr Teil von „Transfer“ oder „Ungeklärter Abgang“.

## Korrektur („Als Netzwerkgebühr buchen“)

Je Paar entsteht **eine Gebühren-Buchung** (`withdrawal`, Tag `fee`, Menge = Abgang − Zugang, Zeit des Abgangs, Wert
anteilig aus dem Wert des Abgangs, sonst leer); Abgang und Zugang werden ausgeblendet (Importzeilen: Überlagerung, die
Rohdaten bleiben). Vorschau rechnet Bestände, Lots, Steuer je Jahr durch; Rückgängig stellt beide Buchungen her. Der
Bestand bleibt gleich. Ab 40 Paaren gilt die Lösung für alle Paare des Befunds, bis 40 gibt es eine Auswahl.

## Automatisch (opt-in)

Autopilot → „Netzwerkgebühren aus Abgang + Rückfluss zusammenfassen“ (`autopilot.roundtrips`, Standard **aus**, nur mit
eingeschaltetem Autopilot, mit Bestätigung). Übernommen werden belegte und wahrscheinliche Befunde ohne Vorbehalt –
**nie „Verdacht“**; als eigene Gruppe mit denselben Sicherungen wie jede Sammelkorrektur (eine Transaktion, kein neuer
negativer Bestand, keine neuen Befunde, Rückgängig mit dem Durchgang). Die allgemeine Einstellung „Beweisbare Korrekturen“
berücksichtigt die Befundart nach der gewählten Sicherheitsstufe (Stufe „eindeutig“ nur bei gleichem Hash).

## Prüfstapel: Zeilen der Datenquelle (ab 0.34.1)

Die Datenquelle liefert je Transaktion **nur die Gebühr** („KRC-20 (Commit/Reveal)“, KAS-Anteil = Gebühr); Koinly führt den
Commit als Abgang und den Reveal als Zugang der vollen Menge. Der Hash-Abgleich verlangte gleiche Mengen und ließ diese Zeilen
deshalb als „teilweise vorhanden“ offen. Jetzt gilt eine solche Zeile als **vorhanden** (`hash_full`, mit Hinweis), wenn dieselbe
Blockchain-Transaktion in einer Buchung (Import oder App) mit einem Bein im selben Asset vorkommt – auch nach der Korrektur
oben: die Netto-Gebühr trägt die Hashes von Abgang und Zugang in der Notiz. Zeilen ohne Gegenbuchung (Hash unbekannt) bleiben
wie bisher in der Prüfung; die Quelle ohne die Kennzeichnung „Commit/Reveal“ wird nicht gelockert.

## Grenzen

* Verschiedene Hashes sind kein Kettenbeweis; ein Muster ≥ 3 Paare ist ein Indiz, kein Beleg. Zahlt jemand tatsächlich an
  Dritte und erhält binnen Minuten fast denselben Betrag von Dritten zurück, wäre die Zusammenfassung falsch (praktisch
  nur bei dem beschriebenen Muster).
* Der Zeitstempel der Gebühr ist der des Abgangs (Sekundengenauigkeit der Quelle); ein anderes Asset als Gebühr (KRC-20-Token)
  ist nicht Teil dieser Regel.
* Mehrdeutige Konstellationen (≈ 250 von ≈ 1,6 Tsd. im Beispiel) und Zwillinge bleiben offen; nach dem Abarbeiten können
  zuvor mehrdeutige Rest-Paare eindeutig werden – der nächste Durchgang zeigt sie als neuen Befund.
