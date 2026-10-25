# Steuer-Regelwerke

Das Steuermodul trennt **länderneutrale Infrastruktur** von **länderspezifischen Regeln** und diese
wiederum von **jahresabhängigen Zahlenwerten**. Damit lassen sich Regeln je Land austauschen und innerhalb
eines Landes nach Gesetzes- oder Formularänderungen aktualisieren – meist ohne Code.

```
app/tax/
  base.py        Datenmodell (TaxInput, TaxResult, Line, Meter, Table, FormField, Issue, Overview)
                 und die abstrakte Klasse RulePack
  params.py      Parameter je Jahr: mitgelieferte YAML + Override aus /data/tax_rules/<id>.yaml + Regelwerk-Datei
  rulesets.py    Regelwerk-Dateien (Parameterstände): Prüfung, Ablage, Vorschau, Vorlage
  registry.py    Findet Regelwerke unter app/tax/packs/<id>/ (PACK = Klasse), Auswahl „auto“
  classify.py    Einstufung von Wertpapieren (Aktie, Fondsart …) und Depots (Inland/Ausland)
  document.py    Dokumentmodell (Überschriften, Tabellen, Formularfelder, Hinweise)
  pdf.py         Länderneutraler PDF-Renderer (ReportLab, eingebettete Schrift, de-DE-Format)
  service.py     Eingangsdaten zusammenstellen, Optionen, Cache, Berichte erzeugen/archivieren
  module.py      Web-Routen der Seite „Steuern & Haltefristen“
  packs/
    de/          Deutschland: __init__.py (Regeln), documents.py (PDF-Aufbau), params.yaml
    neutral/     Länderneutral: realisierte Ergebnisse ohne Steuerregeln (Vorlage)
```

## Ablauf

1. `TaxService.build_input()` erzeugt aus Import, Ledger und Kursdaten ein `TaxInput`. Den Ledger rechnet
   es mit den Optionen des Regelwerks (`RulePack.engine_options`, z. B. FIFO je Wallet) inklusive
   Lot-Snapshots zu jedem 31.12. (für Stichtagsregeln wie die Vorabpauschale).
2. `RulePack.compute(input, jahr, optionen)` liefert ein `TaxResult`: Zusammenfassung, Frei-/Pauschbeträge
   (`Meter`), Schätzung, Formularfelder (`FormField`), Aufstellungen (`Table`), Hinweise (`Issue`) und
   Annahmen. Die Web-Ansicht zeigt das Ergebnis generisch an.
3. `RulePack.build_document(doc_id, ergebnis, meta)` baut ein `Doc`; `pdf.render()` setzt es als PDF.
4. Der Bericht wird mit Regelwerk-Version, Parameter-Version, Parameter-Fingerabdruck, Optionen und
   Importstand in der Tabelle `tax_report` archiviert (Dateien unter `/data/reports/tax/<jahr>/…`).

## Parameter aktualisieren (ohne Update)

`app/tax/packs/<id>/params.yaml` enthält:

* `pack`: `id`, `version`, `updated`, `reviewed_through` (bis zu welchem Jahr die Werte geprüft sind),
  `sources` (Rechtsgrundlagen, erscheinen im Bericht).
* `rules`: Werte **ab** einem Veranlagungsjahr (kumulativ fortgeschrieben), z. B. Freigrenzen, Sätze,
  Pauschbeträge, Teilfreistellungen.
* `per_year`: Werte **nur** für genau ein Jahr (z. B. `basiszins` für die Vorabpauschale).
* `forms`: Formularfelder mit Bezeichnung und optionaler Zeile (`line`), `default` plus Jahres-Overrides.
  Felder der Anlage KAP-INV haben je Fondsart eine eigene Zeile (`lines: {etf_equity: "4", …}`).
  Mitgeliefert sind nur übereinstimmend belegte Zeilen (2024: SO, KAP, KAP-INV; 2025: SO) – siehe Kommentar
  in `params.yaml`.

Eine Datei `/data/tax_rules/<id>.yaml` mit derselben Struktur wird tief darübergemischt. Beispiel für ein
neues Jahr:

```yaml
pack: {reviewed_through: 2027}
per_year:
  basiszins: {2027: 0.0300}          # Beispielwert – amtlichen Wert eintragen (BMF-Schreiben)
rules:
  2027:
    crypto: {freigrenze_23: 1000}    # nur bei Gesetzesänderung
forms:
  2026:
    anlage_so:
      fields:
        so_23_gain: {line: "51"}     # nur geprüfte Zeilennummern
    anlage_kap_inv:
      fields:
        inv_vp: {lines: {etf_equity: "9", etf_mixed: "10"}}   # Zeile je Fondsart
```

Zahlenfelder werden auf Tippfehler geprüft (z. B. `'1.000'` als Text); ein fehlerhafter Override wird
komplett ignoriert und auf der Steuerseite angezeigt. Für Jahre nach `reviewed_through` werden die Werte
des letzten Jahres fortgeschrieben und der Bericht enthält eine Warnung.

## Regelwerk-Dateien (Parameterstände, ab 0.52)

Neben dem lokalen Override (`/data/tax_rules/<id>.yaml`, gilt immer) lassen sich auf *Steuern → Einstellungen →
Regelwerk-Dateien* beliebig viele **Parameterstände** als YAML importieren und per Auswahl „Parameterstand“ (Kopf der
Steuerseite) aktivieren – z. B. eine frühere Rechtslage zum Vergleich oder ein Entwurf für ein künftiges Jahr.
Ablage: `/data/tax_rules/<id>/sets/<kennung>.yaml` (Teil von Sicherung und Export).

```yaml
ruleset:                       # Kopf (Pflicht: name)
  name: "BMF 2022 – frühere Rechtslage"
  id: bmf-2022                 # optional; sonst aus dem Namen
  valid_from: 2022             # gilt nur in diesen Veranlagungsjahren; sonst gilt der Standard
  valid_to: 2024
  status: draft                # draft = Entwurf (Berichte „vorläufig“) | valid = gültig
  source: "BMF-Schreiben vom 10.05.2022, Rz. …"
  description: "Vergleichsrechnung"
rules:    {2022: {crypto: {freigrenze_23: 600}}}     # wie params.yaml (Werte gelten „ab Jahr“)
per_year: {basiszins: {2027: 0.03}}
forms:    {2026: {anlage_so: {fields: {so_23_gain: {line: "51"}}}}}
```

* **Grenze:** Eine Datei ändert nur Werte und dokumentierte Schalter – keine Rechenlogik.
* **Prüfung beim Hochladen** (nichts wird bei Fehlern gespeichert): UTF-8, höchstens 256 KB, YAML-Syntax, Kopf,
  unbekannte Abschnitte/Schlüssel/Formulare/Felder, Typen (Zahl, ganze Zahl, true/false, Auswahl) und Wertebereiche
  (`app/tax/packs/<id>/schema.py`). **Warnungen:** starke Abweichung vom Standard (Faktor > 5), Status `valid` ohne
  Quelle, Einträge nach `valid_to`, und Werte „ab Jahr X“, die ein **späterer Standardeintrag überdeckt** (die Werte
  gelten ab dem Jahr, bis ein späteres sie ändert – im Zweifel das Jahr des Standardeintrags ebenfalls eintragen).
* **Vorschau:** Unterschiede der wirksamen Parameter zum Standard je Jahr (zu Bereichen zusammengefasst) und
  Plausibilitätshinweise; erst „Aktivieren“ macht die Datei wirksam. Aktivieren, Deaktivieren, Herunterladen, Löschen;
  „Vorlage herunterladen“ liefert eine Startdatei mit den aktuell wirksamen Werten.
* **Nachvollziehbarkeit:** Der Bericht nennt unter *Datengrundlage* den Parameterstand (Name, Jahre, Status, Quelle,
  SHA-256 der Datei); dasselbe steht im Berichtsdatensatz (`summary.ruleset`). Version und Cache-Schlüssel tragen die
  Kennung (`2026.3+bmf-2022`). Entwürfe erzeugen den Hinweis `ruleset_draft` („vorläufig“). Ist die Datei
  nicht lesbar, gilt der Standard und die Seite meldet es.

### Regelschalter (Standard = geltendes Recht)

| Schlüssel (`rules.<Jahr>.…`) | Wirkung |
|---|---|
| `crypto.holding_period_years` | Haltefrist in Jahren (nach dem **Anschaffungsjahr** der Lots) |
| `crypto.exempt_after_holding` | `false`: auch nach Fristablauf steuerpflichtig (Gesetzesänderung/Vergleich); Anschaffungsjahr maßgeblich |
| `crypto.freigrenze_23`, `freigrenze_22_3` | Beträge § 23 Abs. 3 S. 5 / § 22 Nr. 3 S. 2 EStG |
| `crypto.freigrenze_mode` | `freigrenze` (alles oder nichts) oder `freibetrag` (nur der übersteigende Teil) – nur § 23 |
| `capital.*` | Abgeltungsteuer-, Soli-Satz, Sparer-Pauschbetrag, Vorabpauschale-Faktor, Quellensteuer-Grenze, Teilfreistellung |
| `capital.derivative_loss_cap` | Verrechnungsgrenze für Verluste aus Termingeschäften (20.000 € für Verluste 2021–2023; `null` = keine). **Nur Hinweis**: Ein Überschreiten erzeugt die Warnung `deriv_loss_cap`; der übersteigende Teil wird nicht gesondert ausgewiesen. |

**Auslegungswahl (BMF 2022 / BMF 2025)** ist bewusst *kein* eigener Schalter: Ein rechenwirksamer Unterschied ist nur
bei der Verbrauchsfolge (wallet­bezogen oder global – bereits Option `scope`) und der Einstufung von Erträgen
(Staking, Lending, Airdrops – bereits Option `income_map`) belegt; beides ist je Jahr wählbar. Weitere Unterschiede
lassen sich über eine eigene Regelwerk-Datei abbilden, sobald sie rechtlich geprüft sind (siehe Rechtsgrundlagen-Prüfung
unten). Gesetzentwürfe (z. B. BT-Drs. 21/5752) sind ungeprüft und nicht hinterlegt.

## Steuerseite: Jahre, Reiter, Sammelerzeugung (ab 0.52)

* **Jahresauswahl** im Kopf: vom Jahr der ersten Buchung bis zwei Jahre nach dem laufenden Jahr, gruppiert in
  „Zukunft (vorläufig)“, „Mit Daten“, „Ohne Daten“; jedes Jahr 2000–2100 lässt sich über „Anderes Jahr“ eingeben. Berichte
  sind für jedes Jahr erzeugbar (leere und künftige Jahre enthalten die Hinweise bzw. die Kennzeichnung „vorläufig“).
* **Reiter:** Übersicht · Steuerjahr · Belege & Berichte · Einstellungen (`?tab=overview|year|docs|settings`).
  Das Jahresergebnis wird nur für „Steuerjahr“ und „Einstellungen“ berechnet. Hinweise sind je Seite in einem
  aufklappbaren Block zusammengefasst; seltene Optionen stehen unter „Erweitert“.
* **Alle Jahre erzeugen:** Hintergrundlauf mit Fortschritt (Standard-Dokumente je Jahr mit Daten; je Jahr ersetzt die neue
  Fassung die frühere); ein Fehler in einem Jahr stoppt den Lauf nicht. Ergebnis: ZIP (`<Jahr>/<Dokument>.pdf`) unter
  `reports/tax/all/` – es wird nur das jeweils neueste behalten.

## Neues Land hinzufügen

1. Ordner `app/tax/packs/<id>/` anlegen mit `params.yaml` (mindestens `pack` und `rules`) und
   `__init__.py`, das eine Unterklasse von `RulePack` als `PACK` exportiert.
2. Implementieren:
   * `option_specs()` – Wahlrechte (bool/choice/amount/percent/map; `per_year=True` für Jahreswerte wie
     Verlustvorträge). Die Seite rendert daraus das Formular.
   * `engine_options()` – z. B. Verbrauchsfolge (FIFO/LIFO/HIFO, je Konto oder global).
   * `holding_end(asset, anschaffung)` – erster steuerfreier Tag oder `None` (keine Haltefrist).
   * `compute()` – Ergebnis für ein Jahr; `overview()` – Kennzahlen und Tabellen für die Übersicht.
   * `documents()` und `build_document()` – PDF-Dokumente aus Bausteinen von `app/tax/document.py`.
3. Tests nach dem Muster `tests/test_tax_de.py` ergänzen (Fristen, Grenzen je Jahr, Verbrauchsfolge,
   Formularfelder). `packs/neutral` ist eine kompakte Vorlage.

Regelwerke dürfen keine Netzwerkzugriffe durchführen und erhalten nur lokale Daten über `TaxInput`.

## Regelwerk Deutschland – Details und Annahmen

| Bereich | Umsetzung |
|---|---|
| Haltefrist § 23 | `holding_end = Anschaffung + 1 Jahr + 1 Tag` (29.02. → 01.03.); Veräußerung am Jahrestag ist steuerpflichtig |
| Verbrauchsfolge | FIFO je Wallet/Konto (Standard) oder global |
| Freigrenze § 23 | Jahressaldo < Grenze → 0; sonst voll; Verlustvortrag leer = automatisch aus den Vorjahren (Eingabe überschreibt); Verluste nur mit § 23 verrechenbar; Rücktrag nicht berücksichtigt |
| Tausch | Veräußerung des abgegebenen und Anschaffung des erhaltenen Werts (neue Haltefrist) |
| Gebühren | Wert mindert den Erlös; Gebühren-Einheiten beim Handel als Veräußerung (Option), Transfergebühren nicht steuerbar (Option) |
| Erträge § 22 Nr. 3 | Wert bei Zufluss; Einstufung je Tag (Standard: Staking, Lending, Zinsen, Reward, Bonus, Mining, Airdrop, Sonstige → § 22 Nr. 3; Cashback, Fork → nicht steuerbar) |
| Datenlücken | fehlende Anschaffung → Anschaffungskosten 0 €, steuerpflichtig; Zugänge ohne Gegenbuchung → Zugangsdatum/-wert |
| Kapitalerträge | FIFO je Depot; Aktien-Topf; Derivate (Turbo, Optionsschein, Warrant) als Termingeschäft im Topf „Sonstige“ (20.000-€-Grenze entfällt seit JStG 2024); Fonds mit Teilfreistellung; Vorabpauschale (Kurse Jahresanfang/-ende, Ausschüttungen je Anteil, 1/12-Kürzung im Erwerbsjahr, Zufluss im Folgejahr, Abzug bei Veräußerung); Quellensteuer anrechenbar bis 15 % |
| Inland/Ausland | Depots mit inländischem Steuerabzug nur nachrichtlich (Steuerbescheinigung maßgeblich); Einstufung je Depot (Einstellung, Import-Spalte `tax_withholding`, sonst „Inland“ für Wertpapierdepots) |
| Schätzung | § 23 + § 22 Nr. 3 × Grenzsteuersatz (Option); Abgeltungsteuer mit Soli/KiSt nach Sparer-Pauschbetrag |

Nicht abgedeckt (im Bericht als Annahme genannt): Fremdwährungsgewinne (§ 23), Altanteile an Fonds vor
2018, gewerbliche Einkünfte (z. B. Mining in größerem Umfang), Günstigerprüfung,
Schenkungsteuer.

## Steuerbescheinigungen der Bank (ab 0.50)

Unter `/tax/certs` lassen sich Jahressteuerbescheinigung und Erträgnisaufstellung hochladen (`app/taxcert`). Das
Regelwerk bekommt sie über `TaxInput.certs` (Jahr → Dokumentart → Werte als Text) und

* zeigt die bescheinigten Beträge als maßgebliche Kontrollwerte (Summary, Übertragungshilfe Anlage KAP, Zeilen aus
  `params.yaml`: 7, 8, 16/17, 37, 38, 40),
* nutzt den bescheinigten Sparer-Pauschbetrag als „bereits genutzt“, wenn die Option leer ist (`auto`),
* gleicht die Verkäufe der Erträgnisaufstellung mit den Inlandsdepots in Portfolia ab (`packs/de/certs.py`).

Optionen mit `auto=True` (Verlustvortrag § 23, bereits genutzter Pauschbetrag): leer = automatisch; ein eingetragener
Wert – auch „0“ – gilt als Eingabe. Die frühere Standard-Null (Zahl 0) wird wie „leer“ behandelt.

## Rechtsgrundlagen-Prüfung (Stand 25.10.2026)

| Punkt | Ergebnis | Quelle |
|---|---|---|
| § 20 Abs. 6 EStG | Abs. 6 hat 5 Sätze (S. 1–3 Verlustverrechnung/-vortrag, S. 4 Aktienverluste nur mit Aktiengewinnen, S. 5 Bescheinigung nach § 43a Abs. 3 S. 4). Die frühere Verlustgrenze für Termingeschäfte (20.000 €) und die Totalausfallregel sind gestrichen (JStG 2024, BGBl. 05.12.2024; Anwendung nach § 52 Abs. 28 EStG auf noch nicht bestandskräftige Fälle). | gesetze-im-internet.de (Wortlaut Abs. 6), Fachbeiträge (CMS, NWB, DER BETRIEB) zur Anwendung |
| Derivate | Turbo-/Optionsscheine, Warrants: Termingeschäfte (§ 20 Abs. 2 S. 1 Nr. 3 EStG) → Topf „Sonstige“, nicht Aktien-Topf. Bestätigt durch die Töpfe der DKB-Erträgnisaufstellung (Aktien / Sonstige). | § 20 Abs. 2, DKB-Erträgnisaufstellung 2025 |
| Schenkung | § 23 Abs. 1 S. 3 EStG: Bei unentgeltlichem Erwerb sind dem Einzelrechtsnachfolger Anschaffung (und Veräußerung) des Rechtsvorgängers zuzurechnen – Haltefrist und Anschaffungskosten des Schenkers. § 11d EStDV regelt nur die Absetzung für Abnutzung (kein Anschaffungszeitpunkt). Zufluss aus Schenkung ohne Gegenleistung ist keine Leistung nach § 22 Nr. 3 EStG. | BFH IX R 3/21 (Zitat § 23 Abs. 1 S. 3), gesetze-im-internet.de (§ 11d EStDV) |
| Anlage KAP 2025, Zeilen | Belegt (Steuerbescheinigung DKB 2025 auf amtlichem Muster): 7, 8, 16/17, 37, 38, 40; Erläuterung derselben Bescheinigung: 41, 42. Auslandsfelder 19–23: Quellen widersprüchlich, nicht hinterlegt. | DKB-Steuerbescheinigung 2025; Formularauszüge von Drittseiten (nicht amtlich) |
| Gesetzgebung | Ein Gesetzentwurf (BT-Drs. 21/5752, Fraktion Bündnis 90/Die Grünen, Mai 2026) betrifft laut Suchtreffer die Haltefrist für Kryptowerte (§ 23 EStG). Inhalt und Stand nicht verifiziert – nicht berücksichtigt; beobachten. | dserver.bundestag.de (Titel/Metadaten) |
