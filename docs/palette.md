# Farbpalette

Die Farben sind als CSS-Variablen in `app/static/css/app.css` definiert (hell unter `:root`, dunkel über
`prefers-color-scheme` bzw. `data-theme="dark"`). Diagramme lesen sie zur Laufzeit (`charts.js → tok()`),
serverseitige Asset-Farben stehen in `app/analytics/colors.py`.

## Grundsätze

* **Farbe nach Aufgabe:** Kategorial (Identität) für Segmente, divergierend (positiv/negativ) für
  Gewinne/Verluste und Renditen, Status (gut/Warnung/kritisch) nur für Zustände – nie als Serienfarbe.
* **Feste Reihenfolge** der Kategorialfarben: Aktien = Serie 1, Krypto = Serie 2, Cash = Serie 3.
  Innerhalb eines Segments unterscheiden sich Assets über vier stabile Abstufungen (Hash der `asset_id`),
  damit ein Asset in allen Ansichten dieselbe Farbe hat; Identität trägt primär die Beschriftung.
* **Text trägt Textfarben**, nie die Serienfarbe; Kennzahlen mit Richtung nutzen `--up`/`--down`
  zusätzlich zu Pfeil und Vorzeichen (nie Farbe allein).
* Dunkelmodus ist eine **eigene, validierte Stufe** derselben Farbtöne, keine automatische Invertierung.

## Kategorial

| Token | Hell | Dunkel | Verwendung |
|---|---|---|---|
| `--series-1` | `#2a78d6` | `#3987e5` | Aktien, Portfolio-Linie |
| `--series-2` | `#eb6834` | `#d95926` | Krypto, Benchmark 1 |
| `--series-3` | `#1baf7a` | `#199e70` | Cash, Benchmark 2 |
| `--series-4` | `#eda100` | `#c98500` | weitere Vergleichslinie |

Abstufungen je Segment (dunkel → hell) siehe `TINTS` in `app/analytics/colors.py`.

## Divergierend und Richtung

| Token | Hell | Dunkel | Verwendung |
|---|---|---|---|
| `--div-pos` | `#2a78d6` | `#3987e5` | positive Balken (Jahresrenditen, Beiträge) |
| `--div-neg` | `#e34948` | `#e66767` | negative Balken |
| `--div-mid` | `#f0efec` | `#383835` | Nulllinie/Neutral |
| `--up` | `#006300` | `#0ca30c` | Text: Kursgewinn |
| `--down` | `#d03b3b` | `#e66767` | Text: Kursverlust |

## Status

| Zustand | Hintergrund hell/dunkel | Text hell/dunkel |
|---|---|---|
| gut | `#e5f4e5` / `#12301a` | `#0b4f0b` / `#8fd88f` |
| Warnung | `#fff4d6` / `#3a2e0c` | `#6b4a00` / `#fad27a` |
| kritisch | `#fde8e7` / `#3d1717` | `#8a1f1f` / `#f4a3a3` |
| Info | `#e7f0fb` / `#13263d` | `#184f95` / `#9ec5f4` |

Status-Badges tragen immer zusätzlich Text (z. B. „veraltet“, „Freigrenze erreicht“).

## Flächen

| Token | Hell | Dunkel |
|---|---|---|
| `--page` | `#f9f9f7` | `#0d0d0d` |
| `--surface` | `#fcfcfb` | `#1a1a19` |
| `--grid` | `#e1e0d9` | `#2c2c2a` |
| `--axis` | `#c3c2b7` | `#383835` |

Die Kategorialfarben wurden gegen beide Flächen auf Helligkeitsband, Chroma, Farbfehlsichtigkeits-Abstand
benachbarter Paare und Kontrast geprüft. Beschriftungen in farbigen Flächen wählen Weiß oder Tinte nach
dem höheren Kontrast (`onFill` in `charts.js`).

## PDF-Berichte

PDFs sind bewusst druckfreundlich und farbarm: Tinte `#1d2330`, Sekundärtext `#566074`, Linien `#d5dae1`,
Kopfzeilen `#eef1f5`, Zebra `#f7f8fa`; Statusflächen für Freigrenzen `#e7f6ee` / `#fff4e0` / `#fde8e8`
(immer mit Statustext).
