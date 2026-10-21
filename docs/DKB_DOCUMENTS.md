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

## Grenzen

* **Gegen die echte DKB nicht getestet.** Geprüft sind: Installation mit echtem dkb-robo 0.31.2 (venv + pip), der Fetcher mit
  Attrappen der Bibliothek, der gesamte Ablauf in Portfolia mit einem Ersatz-Fetcher. Ob Postbox-Liste und Download mit dem
  aktuellen DKB-Login funktionieren, zeigt erst ein echter Lauf.
* **Auswertung der Abrechnungen:** Es gilt der allgemeine Dokumentimport (Schlüsselwörter, Tabellen, Plausibilitätsprüfung
  Stück × Kurs ± Kosten). Er ist nicht an Originaldokumenten der DKB verifiziert; Dokumente mit unbekanntem Layout, Stornos und
  Korrekturabrechnungen sind besonders zu prüfen. Was nicht eindeutig lesbar ist, bleibt „ungeklärt“ im Prüfstapel.
* **Zweiter Faktor:** nur Freigabe in der DKB-App (kein chipTAN). Die DKB kann ihre Schnittstelle ändern – dann meldet Portfolia
  „Schnittstelle geändert“; bereits abgelegte Dokumente bleiben unberührt, ein Update des Fetchers (*Aktualisieren*) behebt es.
* **Bedingungen der Bank:** Das Verfahren nutzt die internen Schnittstellen des Online-Bankings (keine offizielle Freigabe).
  Ob das zu den Bedingungen der DKB passt, prüfen Sie selbst.
* **Depotauszüge** werden abgelegt, aber noch nicht als Bestandsnachweis ausgewertet (möglicher Ausbau).
