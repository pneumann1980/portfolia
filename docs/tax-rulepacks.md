# Steuer-Regelwerke

Das Steuermodul trennt **länderneutrale Infrastruktur** von **länderspezifischen Regeln** und diese
wiederum von **jahresabhängigen Zahlenwerten**. Damit lassen sich Regeln je Land austauschen und innerhalb
eines Landes nach Gesetzes- oder Formularänderungen aktualisieren – meist ohne Code.

```
app/tax/
  base.py        Datenmodell (TaxInput, TaxResult, Line, Meter, Table, FormField, Issue, Overview)
                 und die abstrakte Klasse RulePack
  params.py      Parameter je Jahr: mitgelieferte YAML + Override aus /data/tax_rules/<id>.yaml
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
| Freigrenze § 23 | Jahressaldo < Grenze → 0; sonst voll; Verlustvortrag als Option; Verluste nur mit § 23 verrechenbar |
| Tausch | Veräußerung des abgegebenen und Anschaffung des erhaltenen Werts (neue Haltefrist) |
| Gebühren | Wert mindert den Erlös; Gebühren-Einheiten beim Handel als Veräußerung (Option), Transfergebühren nicht steuerbar (Option) |
| Erträge § 22 Nr. 3 | Wert bei Zufluss; Einstufung je Tag (Standard: Staking, Lending, Zinsen, Reward, Bonus, Mining, Airdrop, Sonstige → § 22 Nr. 3; Cashback, Fork → nicht steuerbar) |
| Datenlücken | fehlende Anschaffung → Anschaffungskosten 0 €, steuerpflichtig; Zugänge ohne Gegenbuchung → Zugangsdatum/-wert |
| Kapitalerträge | FIFO je Depot; Aktien-Topf; Fonds mit Teilfreistellung; Vorabpauschale (Kurse Jahresanfang/-ende, Ausschüttungen je Anteil, 1/12-Kürzung im Erwerbsjahr, Zufluss im Folgejahr, Abzug bei Veräußerung); Quellensteuer anrechenbar bis 15 % |
| Inland/Ausland | Depots mit inländischem Steuerabzug nur nachrichtlich (Steuerbescheinigung maßgeblich); Einstufung je Depot (Einstellung, Import-Spalte `tax_withholding`, sonst „Inland“ für Wertpapierdepots) |
| Schätzung | § 23 + § 22 Nr. 3 × Grenzsteuersatz (Option); Abgeltungsteuer mit Soli/KiSt nach Sparer-Pauschbetrag |

Nicht abgedeckt (im Bericht als Annahme genannt): Fremdwährungsgewinne (§ 23), Altanteile an Fonds vor
2018, gewerbliche Einkünfte (z. B. Mining in größerem Umfang), Termingeschäfte/Derivate, Günstigerprüfung,
Schenkungsteuer.
