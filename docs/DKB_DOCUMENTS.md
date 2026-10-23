# DKB-Dokumente: Abruf per Knopfdruck (ab 0.36.0)

## Ziel

Alle Dokumente der DKB-Postbox (Kontoauszüge, Kreditkartenabrechnungen, Wertpapierdokumente …) per Knopfdruck abrufen und in
Ordner nach Kategorie und Konto ablegen. **Ausgewertet werden nur die Depot-Abrechnungen** (Wertpapierabrechnung,
Ertragsabrechnung): Sie gehen als Vorschläge in den Prüfstapel, wie ein Beleg-Upload. Kontoumsätze werden nicht gebucht.

## Bedienung

1. *Einstellungen → Datenquellen →* DKB-Konto öffnen (Anbieter „DKB“ oder ein bestehendes Konto, dessen Name mit „DKB“ beginnt) →
   **DKB-Abruf einrichten**. Auf der Seite *Einrichtung* auf **DKB-Abruf einrichten** klicken (etwa 1–2 Minuten, Internetzugang
   zu PyPI und GitHub nötig).
2. *Ablageordner* prüfen: In Unraid in der Container-Vorlage den Pfad **„Bankunterlagen“** (`/documents`) einer Freigabe zuordnen
   (Schreibzugriff). Die Seite meldet, wenn der Ordner nicht beschreibbar ist oder nicht auf einer eigenen Freigabe liegt.
3. Benutzername speichern (optional), dann **Abrufen und auswerten**: Benutzername und Passwort eingeben, **die Anmeldung in der
   DKB-App freigeben** (Wartezeit etwa 50 s). Fortschritt und Abbruch auf der Seite.
4. Depot-Abrechnungen erscheinen unter *Journal → Prüfung*; dort wie jede andere Zeile prüfen und übernehmen.

Ordnerstruktur: `<Bankunterlagen>/<Unterordner oder Datenquellenname>/<Kategorie>/<Konto|Depot|Karte>/<Datum>_<Name>.pdf`.
Neue Läufe laden nur, was noch nicht im Index steht; vorhandene Dateien werden nie überschrieben.

## Architektur und Lizenz

```
Portfolia (MIT, app/)  ── stdin: {"user","password"} ──▶  portfolia-dkb-fetch (GPL-3.0, eigene venv)  ──▶  dkb-robo ──▶ DKB
       ▲                   ◀── stdout: JSON-Zeilen ──          schreibt PDFs in den Zielordner
       └── prüft Pfad + Prüfsumme, führt Index (bank_doc), schickt Depot-PDFs in den Dokumentimport
```

* **Kein zweiter Container.** Der Fetcher wird in `<DATA_DIR>/plugins/dkb/venv` installiert (`python -m venv`, `pip install`
  mit festen Versionen der Abhängigkeiten). Die Umgebung bleibt über Image-Updates erhalten; *Entfernen* löscht den Ordner.
  Eine fehlgeschlagene Aktualisierung lässt die bestehende Installation unberührt.
* **Lizenz.** dkb-robo steht unter GPL-3.0, Portfolia unter MIT. Deshalb liegt der Fetcher als eigenes Programm unter
  `contrib/portfolia-dkb-fetch/` (GPL-3.0-or-later, eigener Lizenztext), wird **nicht** ins Image kopiert (`Dockerfile` kopiert nur
  `app/`) und läuft als getrennter Prozess; Austausch nur über JSON-Zeilen und Dateien. Es gibt keinen Import von GPL-Code in
  Portfolia. (Technische Einschätzung, keine Rechtsauskunft.) Wer den Fetcher in ein eigenes Repository verschiebt, setzt die Quelle
  über `PORTFOLIA_DKB_FETCH_SPEC` (pip-Anforderung, z. B. `portfolia-dkb-fetch==0.1.0`, ein Wheel oder eine URL).
* **Standardquelle:** `https://github.com/pneumann1980/portfolia/archive/<Commit des Images>.tar.gz#subdirectory=contrib/portfolia-dkb-fetch`
  (ohne Commit im Image: das Tag `v<Version>`). dkb-robo ist auf `0.31.2` festgelegt.
* **Umgebung des Unterprozesses** ist bereinigt (nur PATH, Proxy- und Zertifikatsvariablen): keine Schlüssel und kein
  Master-Key der App.

### Schnittstelle (Fetcher)

Aufruf: `python -m portfolia_dkb_fetch --out DIR --known DATEI` (stdin: eine JSON-Zeile mit `user`, `password`, `mfa_device`);
`--version` gibt `{"version", "libs"}` aus. Ereignisse (JSON-Zeilen): `hello`, `login`, `awaiting_2fa`, `logged_in`, `listed`,
`document`, `document_error`, `done`, `error` (`kind`: `dependency`, `login`, `2fa_timeout`, `2fa_canceled`, `network`, `changed`,
`other`). Einzelheiten: `contrib/portfolia-dkb-fetch/README.md`.

## Sicherheit

* **Passwort nie gespeichert:** nur im Formular des Abrufs, per stdin an den Fetcher, danach verworfen; in Meldungen und Logs
  maskiert; nicht in Argumenten oder Umgebungsvariablen. Gespeichert wird höchstens der Benutzername.
* **Ein Anmeldeversuch je Klick**, keine automatischen Wiederholungen (Schutz vor Kontosperre). Kein Zeitplan.
* **Nur lesend:** Dokumente werden nicht als gelesen markiert; es werden nur Anmeldung, Liste und Download ausgeführt.
  Die Freigabe in der App wird nicht umgangen.
* **Pfadsicherheit:** Pfade aus der Meldung des Fetchers müssen relativ sein, ohne `..`, unterhalb des Zielordners liegen und
  existieren; die Prüfsumme berechnet Portfolia selbst. Der Fetcher säubert Kategorie- und Kontonamen zusätzlich.
* **Persönliche Daten:** Die PDFs liegen im Klartext in der Freigabe – Zugriff und Sicherung entsprechend wählen. Für die
  Auswertung legt der Dokumentimport (wie bei Beleg-Uploads) eine Kopie im Datenordner ab, solange
  „Originale behalten“ aktiv ist.

## Bekanntes Problem: Anmeldung durch Bot-Schutz blockiert (Stand 22.10.2026)

Die DKB schützt die Anmeldung seit November 2025 mit einem Captcha (Friendly Captcha) und einer Bot-Erkennung (Myra). Die
Bibliothek dkb-robo 0.31.2 (neueste Version) kann das nicht lösen: Die Anmeldung scheitert beim ersten Schritt mit
`1st factor authentication failed` und HTTP 400/502/503 – **noch bevor** die Freigabe in der App angefragt wird und unabhängig
von Benutzername und Passwort (siehe dkb-robo, Issue #92: Fix nur in einem Entwicklungszweig, braucht zusätzlich einen
Browser für das Captcha-Token, noch nicht stabil). Portfolia zeigt das als „Bot-Schutz/Captcha“ an. **Der automatische Abruf
funktioniert deshalb derzeit nicht;** ein Umgehen des Captchas per Browser-Automatisierung ist bewusst nicht vorgesehen
(Sicherheitsmaßnahme der Bank, schwergewichtig, brüchig, Bedingungen).

## ZIP-Import der Wertpapierdokumente (Phase 1: erkennen und ablegen)

Solange der automatische Abruf blockiert ist (siehe oben), lädt man die Wertpapierdokumente selbst aus der Postbox (ein ZIP mit
den PDFs genügt, Ordnerstruktur und Dateinamen sind beliebig) und gibt sie auf der Seite der DKB-Datenquelle unter
**ZIP-Import: Wertpapierdokumente** ein. Alles läuft in der Oberfläche; es gibt keinen Kommandozeilenweg.

1. **Hochladen** (höchstens 100 MB je ZIP; bei mehr Dokumenten in mehrere ZIPs aufteilen). Das Archiv wird sofort geprüft
   (Signatur statt Dateiendung, Größe, Eintragszahl, Kompressionsverhältnis, Verschlüsselung).
2. **Entpacken:** Portfolia liest jedes PDF im Speicher und legt es **nie unter dem Pfad aus dem Archiv** ab (kein Zip-Slip).
   Der Zielpfad entsteht aus dem Inhalt: `<Ablageordner>/<Datenquelle>/Wertpapierdokumente/Depot-<letzte 4 Ziffern>/
   <JJJJ-MM-TT>_<Dokumentart>_<Kennung>.pdf`. Vorhandene Dateien werden nie überschrieben (gleicher Inhalt = übersprungen,
   anderer Inhalt = Zusatz `_2`). Das ZIP selbst wird nicht aufbewahrt – nur als Zwischendatei (Rechte 0600) bis zum Ende des Laufs;
   bei Abbruch oder Fehler bleibt sie für „Fortsetzen“ liegen (Verwerfen löscht sie, ungenutzte Dateien verfallen nach 7 Tagen).
3. **Erkennen:** Die Dokumentart kommt aus dem **Inhalt** (nicht aus dem Dateinamen; ein widersprechender Name wird vermerkt):
   Abrechnung Kauf/Verkauf (auch Festpreisgeschäft), Ertragsabrechnung (Dividende, Kapitalrückzahlung, Liquidationszahlung),
   Kapitalmaßnahmen (Reverse Split, Storno, Gratisausgabe, Neubuchung nach Storno, Namensänderung, Entgeltbelastung, wertlose Titel
   u. a.), ersatzlose Ausbuchung, Depotauszug, Ausführungsanzeige, Auftragsbestätigung, Streichungsbestätigung,
   Kosteninformation (zur Order, Jahresübersicht), Hauptversammlung, Verlustschwellenreport. Dazu die Kennungen (Auftragsnummer nur
   Ziffern, Belegnummer, Abrechnungsnummer, Depot, Datum, ISIN/WKN). Was keine Regel eindeutig trifft (z. B. Kontoauszug), wird
   **gezählt, aufgelistet und nicht abgelegt** – nichts wird geraten.
4. **Fortschritt:** Balken mit Phase, Zähler („Dokument 12 von 47“), **geschätzter Restzeit** („noch ca. 2 Min.“; erst nach drei
   Dokumenten oder 10 s, gerundet, nie 0 und nie negativ, höchstens ein Sprung um den Faktor 2) und bisheriger Dauer. Der Lauf
   geht im Hintergrund weiter; die Seite darf verlassen werden (die globale Anzeige zeigt ihn). **Abbrechen** wirkt zwischen zwei
   Dokumenten, **Fortsetzen** liest den Rest derselben Zwischendatei, bereits abgelegte Dokumente werden übersprungen. Nach einem
   Neustart der App erscheint der Lauf als „Unterbrochen“ mit „Fortsetzen“. Die gemessenen Geschwindigkeiten dienen beim nächsten
   Lauf als Startschätzung.
5. **Bericht:** Seite mit Zählern je Dokumentart und Unterart, Depots, Dubletten (bereits vorhanden / doppelt im Archiv),
   übersprungenen, nicht erkannten und nicht lesbaren Dateien; zusätzlich als CSV (ohne Dateiinhalte).

Erkennung und Ablage verwenden das gleiche lokale Textlesen wie der Dokumentimport (isolierter Kindprozess, kein Netzwerk,
ohne OCR); in Protokollen stehen keine Dateiinhalte. Ausgewertet werden derzeit **Kauf- und Verkaufsabrechnungen** (nächster
Abschnitt); alle anderen Dokumentarten stehen mit dem Stand „erkannt (noch nicht ausgewertet)“ im Index – die Auswertung
(Erträge, Kapitalmaßnahmen, Depotauszug, Kosteninformationen) folgt in den nächsten Stufen.

## Kauf- und Verkaufsabrechnungen (Phase 2)

**Dedizierte Auswertung mit Rechenprobe** (`app/documentimport/dkb_trade.py`, vor den generischen Regeln des Dokumentimports): Alle
Beträge der Abrechnung stehen in EUR (auch bei US-Titeln; nur Kurs und Devisenkurs nennen die Fremdwährung), Vorzeichen als Suffix.

* **Kosten:** Summe über *alle* Zeilen zwischen *Kurswert* und *Ausmachender Betrag* (Provision, Eigene Spesen, Fremde Auslagen,
  Transaktionsentgelt Börse; unbekannte Beschriftungen werden als Gebühr geführt, nie verworfen). Die Einzelwerte stehen in der Notiz
  der Buchung. *Finanztransaktionssteuer* und *Stückzinsen* sind keine Gebühr → „ungeklärt“.
* **Steuern** (Verkauf): nur Zeilen *Kapitalertragsteuer/Solidaritätszuschlag/Kirchensteuer … % auf …* sind Beträge; „Bemessungs-/
  Berechnungsgrundlage für die Kapitalertragsteuer“ und die Verlusttöpfe sind es nicht. Mehrere *Ausmachender Betrag* (Verlust­
  verrechnung, Seite 2 mit positiven Steuerzeilen = Erstattung) werden je Block geprüft. Steuern und Erstattung sind eigene
  Vorschläge (`withdrawal`/`tax`, `deposit`/`refund`) mit dem Wertpapier als Bezug.
* **Rechenprobe** (Toleranz 0,01 €): Σ Zeilen eines Blocks = Ausmachender Betrag; Menge × Kurs ÷ Devisenkurs ≈ Kurswert (mit der
  Rundung des gedruckten Kurses); Richtung aus Titel, Vorzeichen und „zu Lasten/Gunsten“. **Nicht bestanden → „ungeklärt“**, mit
  Grund im Bericht, nie buchbar. An allen Beispiel-Abrechnungen (Kauf USD mit drei Kostenarten, Zertifikate, Sparplan-Bruchteil,
  Verkauf mit KapESt/Soli, Verlustverkauf mit Erstattung, Festpreisgeschäft, wertloser Verkauf) bestanden.
* **Zeitpunkt:** *Schlusstag/-Zeit* als deutsche Ortszeit; ohne Schlusstag (Festpreisgeschäft) das Belegdatum ohne Uhrzeit.

**Zuordnung zu vorhandenen Buchungen** (Regeln Z0–Z5 der Analyse): Konto der DKB-Datenquelle, Kauf bzw. Verkauf des Assets (ISIN/WKN),
Datum ± 3 Tage, gleiche Menge. Genau ein Treffer → die Abrechnung ist **verknüpft, es entsteht keine zweite Buchung** (nur ihre
Steuerzeilen bleiben Vorschläge im Prüf-Stapel). Mehrere gleichwertige Kandidaten, andere Menge, anderes Konto oder unbekanntes Asset →
keine automatische Zuordnung; der Vorgang kommt als Vorschlag in den Prüf-Stapel (vor dem Stichtag des kuratierten Imports nur auf
ausdrücklichen Wunsch zu übernehmen). Sparplan-**Schätzungen** werden nie zugeordnet oder geändert.

**Ergänzen (Sammelaktion):** Auf der Ergebnisseite listet *Gebühr, Kurswert und Uhrzeit ergänzen …* je zugeordneter Buchung die
Änderungen mit Vorher/Nachher (Beispiel: Kauf 580,00 € → Kurswert 570,00 € und Gebühr 12,50 € statt „Wert inkl. Gebühren“). Vorschau mit
Wirkung auf Bestände, Einstand und Steuer, dann *Übernehmen* als **eine** Entscheidung, als Ganzes rückgängig unter *Datenqualität →
Entscheidungen und Korrekturen* (stellt den Ausgangszustand exakt her). **Nie** geändert: Menge, Asset, Konto, Vorgangsart.
Die Abrechnung hat Vorrang vor allen anderen Quellen.

**Von Hand erfasste oder bearbeitete Buchungen sind nicht gesperrt:** Die Vorschau warnt („von Hand geändert, jetzt: Wert …, Gebühr …“),
übernommen wird erst nach ausdrücklichem *Trotzdem ändern* je Buchung auf der Einzelseite des Belegs – nie in der Sammelaktion.
Gesperrt bleibt nur die Sparplan-Schätzung.

**Beleg-Link (`tx_document`):** Beleg ↔ Buchung (n:m, Rolle, Quelle `auto`/`amend`/`backfill`/`manual`). Geschrieben bei der Zuordnung, beim
Ergänzen und per Rückfüllung (übernommene Beleg-Zeilen, Notizen mit „SHA …“). Angezeigt in der Buchungsliste und der Positionsansicht
(*Abrechnung* → Beleg, *PDF* → Original; ist das Original nicht gespeichert: „Originaldatei nicht gespeichert“). Die Verknüpfung ist
Metadaten in der Datenbank (Teil der Sicherung, nicht des Gesamtexports – die Belege selbst sind es auch nicht).

## Ertragsabrechnungen (Phase 3)

Dividendengutschrift, *Gutschrift – Kapitalrückzahlung* und *Gutschrift – Liquidationszahlung* gehen beim ZIP-Import durch dasselbe
Verfahren wie die Abrechnungen (`dkb_income.py`, Rechenprobe, Prüf-Stapel, nichts ohne Bestätigung gebucht).

| Beleg | Buchung (Konto = Depot der Datenquelle) |
|---|---|
| Brutto in EUR (`Dividendengutschrift … 3,71+ EUR` bzw. `Kurswert … EUR`) | `deposit`, Tag `dividend`, EUR, `related_asset` = Asset, `value_eur` |
| `Einbehaltene Quellensteuer <p> % auf …` | `withdrawal`, Tag `withholding_tax` – **nur** die einbehaltene |
| `Kapitalertragsteuer`, `Solidaritätszuschlag`, `Kirchensteuer` (`<p> % auf <Basis> EUR`) | **eine** `withdrawal`, Tag `tax` (Summe, Einzelwerte im Beleg) |

Keine Abzüge sind `Anrechenbare Quellensteuer`, `… % rückforderbare Quellensteuer`, `Verrechnete anrechenbare ausländische Quellensteuer`
(auch bei Zeilenumbruch), `Kapitalertragsteuerpflichtige Dividende`, `Berechnungsgrundlage …` und `Eingebuchte sonstige negative Kapitalerträge`.
Seite 2 (Verrechnungs- und Steuertöpfe) wird nicht gelesen.

**Rechenprobe** (≤ 0,01 €, sonst „ungeklärt“, keine Buchung): EUR-Brutto + Σ Abzüge = Ausmachender Betrag · Brutto in Fremdwährung ÷
Devisenkurs ≈ EUR-Brutto · Stück × Betrag je Stück ≈ Brutto in Fremdwährung · Quellensteuer ≈ Satz × Brutto (Fremdwährung) ÷ Devisenkurs ·
KapESt/Soli ≈ Satz × Basis laut Beleg (Toleranz 0,02 €, weil die Bank abschneidet: Soli 5,5 % × 0,36 = 0,0198 → gedruckt 0,01) · Richtung
(Gutschrift, „zu Gunsten“) · Pflichtangaben und ISIN-Prüfziffer. Positive Abzugszeilen (Erstattung) und mehrere Ausmachende Beträge sind
bei Erträgen nicht belegt und bleiben „ungeklärt“.

**Datum:** Zahlbarkeitstag (die Wertstellung steht als Valuta daneben; liegen beide in verschiedenen Jahren, weist der Prüf-Stapel auf das
Steuerjahr hin). **Kennung:** Abrechnungsnummer (`doc:dkb:<Nr>`); zwei Belege mit gleichem Titel, Asset, Menge und Tag, aber
verschiedener Nummer (Dividende und Kapitalrückzahlung am 19.03.) bleiben zwei Vorgänge.

**Entscheidung E3 (Kapitalrückzahlung/Liquidationszahlung):** Die Bank besteuert beides wie einen Ertrag (KapESt auf den vollen Betrag). Portfolia
erfasst es als `dividend`, schreibt die Ertragsart in die Notiz und markiert den Vorschlag mit einem Prüfhinweis. Ob steuerlich eine
Kapitalrückzahlung (mindert die Anschaffungskosten) vorliegt, entscheiden Sie bzw. Ihre Beratung.

**Zuordnung:** Eine Ertragsabrechnung gehört zu einer **bereits erfassten Dividende**, wenn Konto, Asset, Datum (± 3 Tage um Zahlbarkeitstag/
Wertstellung) und Betrag (Brutto, ersatzweise netto; ± 0,01 €) passen und es genau einen besten Treffer gibt. Dann wird nur der Beleg
verknüpft (`tx_document`, Rolle *Ertragsabrechnung*) und keine zweite Dividende vorgeschlagen; Steuerzeilen bleiben Vorschläge. Die vorhandene
Dividende wird nicht verändert. Mehrere gleichwertige Treffer, anderes Konto oder anderer Betrag → keine Zuordnung (neuer Vorschlag).

**Stand an Ihrem Export:** Der kuratierte Import enthält für das DKB-Depot keine Dividenden; alle fünf Beispiel-Ertragsabrechnungen erscheinen
deshalb als Vorschläge (12 Zeilen). Da sie vor dem Stichtag des Imports liegen, sind sie „vor Stichtag“ und nur auf ausdrücklichen Wunsch zu übernehmen.

## Kapitalmaßnahmen und Ausbuchungen (Phase 4)

`dkb_capital.py` liest Kapitalmaßnahmen (Belegnummer, Maßnahmenart als Überschrift) und die ersatzlose Ausbuchung (Dateiname „Depot_bertrag“).
Gebucht wird **nur**, was der Beleg eindeutig als Bestandsänderung ausweist und die Rechenprobe besteht. Alles andere ist Information oder „ungeklärt“.

| Beleg | Ergebnis | Buchung / Prüfung |
|---|---|---|
| Reverse Split / Split, **Stufe 2** (Ausbuchung ≠ Einbuchung) | buchen | `corporate_action`, Tag `reverse_split`/`split`, alt → neu, **Datum = Valuta/Ex-Tag**, Einstand bleibt. Probe: Ausbuchung × b / a = Einbuchung (Bruchteil < 1 Stück erlaubt, Hinweis „separat reguliert“) |
| Reverse Split, **Stufe 1** (1 : 1, gleiche ISIN und Menge, „Sperre“) | Information | keine Buchung (sonst doppelter Bestand) |
| Gratisausgabe mit Einbuchung / „Neubuchung nach Storno“ | buchen | `corporate_action`, Tag `spinoff` (wie der vorhandene GME-Optionsschein-Eintrag), Zugang des neuen Wertpapiers, `value_eur` = unbarer Ertrag (Einstand = Bewertungskurs × Menge), dazu **eine** `tax`-Zeile (KapESt + Soli). Probe: Menge × Bewertungskurs = unbarer Ertrag, Satz × Basis, Σ Steuern = Ausmachender Betrag |
| Entgeltbelastung | buchen | `withdrawal`, Tag `fee`, EUR, Bezug auf das Wertpapier. Probe: Stück × Entgelt ÷ Devisenkurs = Ausmachender Betrag |
| Wertlose / ersatzlose Ausbuchung | buchen | `withdrawal`, Tag `lost`, Menge, **Erlös 0 €** (Entscheidung E4, wie die vorhandenen Ausbuchungen); Verlust und Käufe stehen in der Notiz. Probe: Σ Menge und Σ Anschaffungskosten der „berücksichtigten Anschaffungsgeschäfte“ = Ausbuchung / Verlust |
| Storno | Information | hebt die frühere Einbuchung mit der im Text genannten Belegnummer auf (siehe unten) |
| Namensänderung, Ankündigungen (Gratisausgabe, Liquidationszahlung), Chapter VII, Ablauf der Optionsfrist | Information | keine Buchung; Namensänderung zeigt alt → neu im Bericht |
| andere Arten (Umtausch, Fusion, Spin-off, Bezugsrechte …) | ungeklärt | nie geraten |

**Storno-Regel:** Der Stornobeleg trägt dieselbe Belegnummer wie die Einbuchung, die er aufhebt. Ist die Einbuchung noch nicht ausgewertet, wird sie nicht
ausgewertet (Status *Information*, „storniert durch Beleg …“); übrig bleibt genau **ein** Zugang aus der Neubuchung. War die Einbuchung bereits ausgewertet
(früherer Lauf), steht ein Hinweis im Bericht – eine übernommene Buchung wird nie still zurückgenommen. Die Kennung der Stornobelege lautet `<Belegnr>-storno`,
damit sie im Stapel nicht mit der Einbuchung verschmelzen.

**Zuordnung zu vorhandenen Buchungen:** Konto der Datenquelle, Asset, Art (Tag `reverse_split`/`split`, Zugang des neuen Assets bei Gratisausgabe, `lost` mit gleicher
Menge, `fee` mit gleichem Betrag) und Datum ± 3 Tage; genau ein bester Treffer. Weicht die Menge ab (z. B. 50 → 5 statt 100 → 5), ist es dieselbe Maßnahme:
Beleg verknüpft, Abweichung als Hinweis im Bericht, **keine zweite Maßnahme** vorgeschlagen. Steuerzeilen bleiben Vorschläge.

**Neu im Dokumentindex:** `bank_doc.meta` (Migration 32, JSON): Unterart, Belegnummer, Bezug, Auftragsnummer, ISIN … aus dem Inhalt – Grundlage für Storno und die
späteren Zuordnungen von Order-Belegen und Kosteninformationen.

**Stand an Ihrem Export:** 15 Kapitalmaßnahmen-Belege: 1 Gratis-Neubuchung dem vorhandenen Optionsschein-Zugang zugeordnet (Steuer 1,30 € bleibt Vorschlag),
5 Vorschläge (Canoo-Zusammenlegung 86 → 4,3, Oatly-Zusammenlegung 100 → 5, Entgelt 1,91 €, Navya- und Aker-Ausbuchung), 8 nur Information. Alle liegen vor dem Stichtag des
Imports („vor Stichtag“, nur auf ausdrücklichen Wunsch zu übernehmen). Die alten Wertpapiere der Zusammenlegungen sind in Ihrem Bestand teils nicht als Asset
vorhanden – dort fragt der Prüf-Stapel nach der Zuordnung.

## Depotauszüge (Phase 5)

`dkb_depot.py` liest die „Aufstellung über Kundenfinanzinstrumente per …“ (Quartals-/Stichtagsdepotauszug).

* **Bestandsnachweis:** je Position (ISIN, Menge, Kurswert, ggf. „ohne Bewertung“) ein Beleg-Vorgang `holdings`. Er erzeugt **nie** eine Buchung. Der Lauf legt daraus
  **Referenzbestände** an (`reference_balance`, Quelle *Kontoauszug/Depotauszug*, idempotent je Konto/Asset/Stichtag/Menge) und vergleicht sie mit dem Bestand der Buchungen
  **zum Stichtag**: *stimmt*, *weicht ab* (Differenz), *unbekannt* (kein Asset zur ISIN) und *nur in Portfolia* (im Auszug nicht enthalten). Die Referenzbestände
  nutzt die Diagnose als Prüfwert; geschätzte Sparplan-Buchungen zählen im Bestand mit (Hinweis im Bericht).
* **Depotentgelt:** `netto + 19 % USt = brutto` für den Zeitraum; gebucht wird der **Bruttobetrag** als `withdrawal`/`fee` auf Konto-Ebene (kein Wertpapierbezug) mit der
  **Valuta der Belastung** als Datum, Netto/USt in der Notiz. Kennung `Depotentgelt:<Depot>:<Quartalsende>`. Eine vorhandene Gebührenbuchung (Datum ± 3 Tage, gleicher
  Betrag) wird nur verknüpft (Rolle *Depotauszug*), nicht doppelt vorgeschlagen.
* **Rechenprobe** (sonst „ungeklärt“, **keine** Referenzbestände, keine Buchung): Anzahl der Positionen = „n Posten“; Σ Kurswerte = „Summe Kurswerte“ (± 0,01 €); ISIN-Prüfziffer;
  netto + USt = brutto, USt ≈ 19 % von netto.

## Kosteninformationen und Order-Belege (Phase 6)

`dkb_costinfo.py` liest Kosteninformationen (Layout *neu* ab 2023 und *Legacy*) sowie Ausführungsanzeige, Auftragsbestätigung und Streichungsbestätigung.

* **Kosteninformation = Order-Spur.** Sie belegt nicht, dass die Order ausgeführt wurde, und nennt den Kurs zur Eingabe. Es entsteht **nie** eine Buchung: Der Beleg wird einer
  **vorhandenen** Buchung zugeordnet und mit der Rolle *Kosteninformation* verknüpft. Zuordnung (Z0–Z6): Konto, Asset, Richtung, Menge (Teilausführungen werden summiert) und
  Zeitfenster; Ergebnis je Beleg: *zugeordnet*, *ohne Treffer*, *Menge weicht ab*, *Richtungskonflikt*, *mehrdeutig*, *Asset unbekannt*. Sparplan-Einrichtungen sind nur Information.
* **Gebühr aus der Kosteninformation** (Dienstleistungskosten + Fremdwährungskosten der Ein-/Ausstiegsspalte; Produktkosten = Spread, keine Gebühr) ist **geschätzt**. Sie wird nur über
  *Ergänzen → Quelle „Kosteninformation“* nach Vorschau und Bestätigung eingetragen, ist rückgängig machbar, trägt die Notiz „Gebühr aus Kosteninformation vom …, geschätzt“ und wird
  **nie** angeboten, wenn eine Abrechnung die Gebühr belegt (Abrechnung hat immer Vorrang). Rechenprobe: Menge × Kurs ≈ Anlagebetrag (1 %), sonst „ungeklärt“.
* **Order-Belege** je Auftragsnummer: Bericht *ausgeführt / vorgemerkt / gestrichen*; sie hängen an die Buchung der Abrechnung derselben Auftragsnummer – auch, wenn diese erst später
  gebucht wird (`txdocs.backfill`).
* **Belege-Download pro Jahr** (`Journal → Dokumente`, Jahresfilter gesetzt, Knopf *Belege des Jahres als ZIP*; `GET /journal/documents/export?year=`): ZIP mit den Originalen der Buchungen
  des Jahres in Ordnern `01_Abrechnungen … 06_Sonstige` (+ `ohne_buchung/`), Dateinamen aus Datum/Art/ISIN/Kennung (nie aus dem Originalnamen), dazu `manifest.csv`
  (UTF-8 mit BOM, `;`, Dezimalkomma; Buchung, Beleg, Rolle, Gebührenquelle *belegt/geschätzt*, Datei, SHA-256, Status `ok`/`original_fehlt`). Grenzen: 2.000 Dateien / 500 MB je
  Download; die PDFs enthalten Namen und Kontodaten – der Download läuft nur über die geschützte Oberfläche.

## Steuervergleich DKB ↔ Portfolia (Phase 7)

*Einstellungen → Datenquellen → DKB → Steuervergleich* (`/settings/datasources/<id>/dkb/steuer`). **Reine Abweichungsanzeige:** Es wird keine Buchung und keine Steuereinstellung
verändert (einzige Ausnahme: die ausdrücklich bestätigte Vorbelegung, siehe unten). Die Seite ersetzt weder Steuerberatung noch die Jahressteuerbescheinigung der Bank – die DKB-Seite
stammt **ausschließlich aus den Verkaufs- und Ertragsabrechnungen**.

* **Einlesen:** Knopf *Steuerangaben einlesen* liest die abgelegten Originale lokal (höchstens 300 je Klick) und trägt die Steuerangaben in `bank_doc.meta["tax"]` ein (`dkb_tax.py`):
  Veräußerungsgewinn/-verlust, „Berücksichtigte Anschaffungsgeschäfte“ (Auftragsnr., Datum, Stück, Kosten, Erlös, Ergebnis), KapESt/Soli/KiSt **netto nach Erstattung**, bei Erträgen
  Brutto und Quellensteuer, dazu die Zeilen *Vorher/Nachher* der Verlustverrechnungstöpfe. Belege mit gescheiterter Rechenprobe liefern keine Steuerangaben.
* **Portfolia-Seite:** `GermanyPack.capital_for_accounts` rechnet die Kapitalerträge des Jahres **nur für das Konto dieser Datenquelle** mit `include_domestic = True`; Ihre Steueroptionen bleiben
  unverändert. Die Zuordnung Beleg ↔ Buchung folgt der vorhandenen Beleg-Verknüpfung (`tx_document`).
* **Je Verkauf:** Veräußerungsgewinn DKB ↔ realisierter Gewinn der Buchung (Erlös abzüglich Gebühr − Einstand inkl. Gebühren), einbehaltene Steuer ↔ Steuerbuchungen des Wertpapiers am
  Verkaufstag, **Lot-Auswahl** (Kaufdatum und Menge der DKB-Tabelle gegen die Lot-Teile von Portfolia). Wahrscheinliche Ursachen: Einstand fehlt, Gebühr aus Kosteninformation geschätzt,
  Sparplan-Schätzung, Kapitalmaßnahme seit der Anschaffung, andere Lot-Auswahl.
* **Je Ertrag:** Brutto, Quellensteuer, KapESt + Soli gegen die Ertragsbuchung und die Steuerzeilen (±7 Tage, jede Zeile höchstens einmal). Eine fehlende Steuerzeile in Portfolia fällt als
  Differenz auf.
* **Je Jahr:** Summen (Gewinne, Erträge, Quellensteuer, einbehaltene Steuer) und **Topf-Endstände** (Aktien, Sonstige, Sparer-Pauschbetrag) der letzten Abrechnung gegen die Portfolia-Rechnung.
* **Schwellen:** unter 0,05 € *Rundung*, ab 1,00 € *Hinweis*, ab 10,00 € *Warnung* (Hinweis/Warnung auf der Seite einstellbar).
* **Vorbelegung (nur mit Bestätigung):** Aus *Vorher* der ersten Abrechnung des Jahres werden Verlustvortrag Aktien/Sonstige (je Jahr) und – wenn der Sparer-Pauschbetrag genau 1.000 € bzw. 2.000 €
  beträgt – die Zusammenveranlagung (gilt für alle Jahre) vorgeschlagen. Übernommen werden nur ausgewählte Werte, **serverseitig neu berechnet**; zurücknehmbar unter *Steuer → Optionen*.
* **Grenzen:** Die DKB rechnet **laufend je Transaktion** (Verlusttöpfe, sofortige Erstattung), Portfolia **am Jahresende** – die Steuer je Verkauf ist deshalb nur über die Topfstände wirklich
  vergleichbar. Kapitalmaßnahmen mit eigener Steuerbehandlung, Teilfreistellung (Fonds) und Einstände vor dem Archiv führen zu erwartbaren Abweichungen. Ohne alle Abrechnungen des Jahres im Archiv sind
  Summen und Topfstände unvollständig (die Seite weist darauf hin). Steuern aus Kapitalmaßnahmen werden nicht verglichen.

## Grenzen

* **Gegen die echte DKB nicht getestet.** Geprüft sind: Installation mit echtem dkb-robo 0.31.2 (venv + pip), der Fetcher mit
  Attrappen der Bibliothek, der gesamte Ablauf in Portfolia mit einem Ersatz-Fetcher. Ob Postbox-Liste und Download mit dem
  aktuellen DKB-Login funktionieren, zeigt erst ein echter Lauf.
* **Auswertung der Abrechnungen:** Das DKB-Profil beruht auf den Beispielen der Analyse (Layout ab 2024); Abrechnungen vor 2024,
  Teilausführungen, Stornoabrechnungen, Fonds/ETF, Anleihen (Stückzinsen), Finanztransaktionssteuer und Kirchensteuer sind nicht belegt –
  sie bestehen die Rechenprobe oder bleiben „ungeklärt“, werden aber nie geraten. Was nicht eindeutig lesbar ist, bleibt „ungeklärt“.
* **Zweiter Faktor:** nur Freigabe in der DKB-App (kein chipTAN). Die DKB kann ihre Schnittstelle ändern – dann meldet Portfolia
  „Schnittstelle geändert“; bereits abgelegte Dokumente bleiben unberührt, ein Update des Fetchers (*Aktualisieren*) behebt es.
* **Bedingungen der Bank:** Das Verfahren nutzt die internen Schnittstellen des Online-Bankings (keine offizielle Freigabe).
  Ob das zu den Bedingungen der DKB passt, prüfen Sie selbst.
* **Erträge:** Fonds-/ETF-Ausschüttungen, Vorabpauschale, Zinsen und Erstattungen von Steuern sind nicht belegt (→ „ungeklärt“).
* **Kosteninformationen** zu Trades, die erst später gebucht werden, bleiben „ohne Treffer“ (kein erneutes Zuordnen); eine Legacy-Kosteninformation, deren Rechenprobe scheitert, bleibt „ungeklärt“.
* **Depotauszug-Vergleich:** Er ist nur so genau wie die Buchungen zum Stichtag (geschätzte Sparplan-Einträge zählen mit).
