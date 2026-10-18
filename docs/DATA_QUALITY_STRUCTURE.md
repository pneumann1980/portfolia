# Datenqualität: Arbeitsplan, Reiter, Prüflauf

Die Datenqualität hatte ~17 Einstiegspunkte (Diagnose, Integritätsprüfung, Konfliktklärung, Entscheidungsliste, Prüfstapel,
Sammelbearbeitung, Cockpit, Quellenabdeckung …) und keine Rangfolge. Seit 0.33.0 gilt:

## Vier Reiter

| Reiter | Inhalt |
|---|---|
| **Heute** (`/quality`) | Arbeitsplan Stufe A/B/C, „Alles prüfen“, Import, Technische Einzelheiten (eingeklappt) |
| **Fälle** (`/quality/faelle`) | Eine Liste für alles Offene; Unterseiten: Der Reihe nach entscheiden, Klärungsfälle, Diagnose-Befunde, Prüfstapel, Sammelbearbeitung, Hinweise |
| **Belege** (`/quality/belege`) | Bestände und Nachweise, Abweichungsentwicklung, Quellenabdeckung, Anschaffungsdaten, Cockpit, Prüfbericht |
| **Automatik** (`/quality/automatik`) | Autopilot, Regeln, Einstellungen der Klärung |

Alle bisherigen Adressen bleiben erreichbar (als Unterseiten, mit demselben Reiterkopf); nichts wurde entfernt. Die Zeile
mit elf Schaltflächen in der Diagnose ist durch den Reiterkopf ersetzt.

## Stufen

* **A · Muss ich tun** – ohne Entscheidung sind Bestände, Kostenbasis oder Steuer falsch bzw. unvollständig:
  Klärungsfall mit belegter Lösung („Lösung eindeutig“), importierte Vorgänge im Prüfstapel, belegte Diagnose mit
  unbedingter Empfehlung, kritische Befunde der Integritätsprüfung.
* **B · Kann ich tun** – verbessert die Sicherheit (Bestand bestätigen, Beleg nachtragen, Umfang eingrenzen), blockiert
  nichts. **Fehlende Nachweise gelten als unbekannt, nie als 0** – ein Fall, der nur einen Bestandsnachweis braucht, ist
  höchstens Stufe B.
* **C · Nichts zu tun** – automatisch erklärt (ausgebucht), zurückgestellt, Hinweise ohne Fehler.

Je Zeile: Empfehlung, Warum, Wirkung, Aufwand, ein Primärknopf. Oben steht **ein** nächster Schritt; sind A und B leer
und es wurde geprüft: „Nichts mehr zu tun“. Ohne bisherigen Prüflauf steht dort „Noch nicht geprüft“ – nie „fertig“.

Umsetzung: `app/diagnosis/workplan.py` (rein lesend). Quellen: gespeicherter Stand der Klärung, Diagnosebericht,
Prüfstapel-Zähler, gespeicherte Integritätsprüfung. **Abweichung vom Entwurf:** Die steuerliche Wirkung einer Lösung
braucht eine Berechnung je Fall und steht deshalb erst in der Simulation des Falls; die Einstufung entscheidet nach
Beleglage und Zustand, nicht nach Beträgen. Jede Steueränderung ist damit nicht pauschal Stufe A – sie ist es, wenn eine
belegte Lösung bereitliegt (deren Simulation sie ausweist).

## Prüflauf „Alles prüfen“

`POST /quality/pruefen` startet als Hintergrundjob (`app/diagnosis/checkrun.py`): Diagnose berechnen →
Integritätsprüfung → Fälle untersuchen (`clarify.run`) → Arbeitsplan erstellen. **Lokal, ohne Abrufe** bei Börsen, Wallets
oder Anbietern (`research=False`); die Klärung übernimmt nur, was die bestehenden Einstellungen ohnehin erlauben. Der
Lauf endet mit einem Bericht (neu / erledigt seit dem letzten Lauf, Zähler A/B/C), gespeichert unter `app_state`
`dq_check_last`. „Aktuelle Bestände abfragen“ (extern, lesend) bleibt eine eigene, ausdrückliche Aktion.

## Warte-Balken

* **Prüflauf:** Phasenbalken mit Prozent, aktuellem Schritt und Hinweis „arbeitet lokal“ (gemeinsame Fortschrittsanzeige
  `app.progress`, Abfrage alle 2 s, danach Neuladen); auf jeder Seite der Datenqualität im Kopf sichtbar, solange er läuft.
  Ein Taktgeber lässt den Balken in Schritten ohne Zählung langsam, aber nie bis zum Ende vorrücken.
* **Arbeitsplan:** wird nach dem Seitenaufbau nachgeladen (unbestimmter Balken „wird berechnet“).
* **Alle Seiten/Formulare der Datenqualität** (`/quality…`, `/journal/pruefung…`): ein Balken am oberen Rand ab dem Klick
  bis die Seite da ist (`app.js`, ohne Auswirkung auf Tastatur- und No-JS-Nutzung).

## Fall-Seite

Reihenfolge: nächster Schritt → Geldweg → Wirkung → Bestandsfenster → Bestandsnachweis → **„Technik und Belege“**
(eingeklappt: Dimensionen, Regelprüfung, Dossier, Kandidaten/Hypothesen) → Verlauf. Anker in eingeklappten Bereichen
öffnen den Bereich.

## Grenzen

* Die Fallseite rechnet die Klärung für diesen Fall neu (bis ca. 18 s bei sehr großen Fällen, bestehendes Verhalten); der
  Warte-Balken macht das sichtbar, beschleunigt es aber nicht.
* Der Arbeitsplan zeigt den Stand der letzten Prüfung; die Integritätsprüfung wird nicht bei jedem Aufruf neu gerechnet
  (Hinweis „älter als die Daten“, wenn sich der Datenstand geändert hat).
* Sammelbearbeitung und Prüfstapel bleiben eigene Aktionen mit eigener Vorschau; die Fallliste führt sie an, ersetzt sie
  nicht.
