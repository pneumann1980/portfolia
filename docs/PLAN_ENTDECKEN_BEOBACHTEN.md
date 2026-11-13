# Plan 0.114 – Projektreview und „Entdecken & Beobachten“

Stand der Analyse: Branch `claude/portfolia-dashboard-s9p6zr`, Commit `39479ea` (0.113.0), 12.11.2026.
**Status: umgesetzt in 0.114.0.** Abweichungen vom Plan nach Rückfrage: Entfernen aus der letzten Liste setzt den
Status `zurueckgestellt` (statt `neu`); zusätzlich umgesetzt wurden Listenverwaltung (B9), Sammelabfragen (B10),
Mobilmenü aus der Menüliste (B5), die Kachel „Beobachtet“ auf der Übersicht und SEC-Kennzahlen für US-Aktien im
Bestand. Maßgeblich ist `docs/DISCOVERY_INTELLIGENCE.md`, Abschnitt 0.10; der Backend-Patch ist eingearbeitet und
entfernt. **Seit 0.115** ersetzt eine einzige Tabelle „Watchlist“ die beiden Reiter (Abschnitt 0.11 dort);
`partials/watch_tabs.html` entfällt.

Lesereihenfolge für die Umsetzung: Abschnitt 4 (Entscheidungen) → Abschnitt 5 (Arbeitspakete, strikt der Reihe nach)
→ Abschnitt 6 (Abnahme). Abschnitt 2 und 3 sind Begründung und Zielbild.

---

## 1. Ausgangsstand

| Punkt | Wert |
|---|---|
| Version | 0.113.0 (`app/__init__.py`) |
| Stack | FastAPI, Jinja2, HTMX, SQLite (`app/db.py`, Migrationen 1–44), APScheduler |
| Tests | `TZ=Europe/Berlin .venv/bin/python -m pytest -q -p no:cacheprovider` – 2848 Tests, 0 Fehler, 3 übersprungen, ca. 25 min |
| Lint | `.venv/bin/ruff check app tests scripts` – grün (CI prüft nur `ruff check`, nicht `ruff format`) |
| Typen | `.venv/bin/mypy app/news app/jobs app/web app/discovery app/health.py app/logging_setup.py --ignore-missing-imports` – 344 Fehler (Altbestand, blockiert CI nicht; darf nicht steigen) |
| CI | `.github/workflows/ci.yml`: Jobs `test`, `checks`, `image` – auf `39479ea` grün |

**Tiefe der Prüfung in diesem Durchgang**

- *Vollständig nachvollzogen (Oberfläche → Service → Datenbank → Jobs → Export):* Discovery, Watchlist, Navigation,
  KI-Status.
- *Gemessen:* Seitenzeiten und Datenbankabfragen der Discovery-/Watchlist-Seiten (Testdatenbestand mit 24
  Unternehmen, KI nachgebildet).
- *Nicht erneut im Detail geprüft:* Import, Dublettenprüfung, Ledger, Buchungsprüfung, Steuer, Docker/Unraid. Diese
  Bereiche sind durch die Gesamttestsuite und frühere Audits abgedeckt (`docs/PROJEKTANALYSE.md`,
  `docs/BUCHUNGEN_PRUEFEN_ABNAHME.md`, `docs/FINANCIAL_INTEGRITY_IMPLEMENTATION_REPORT.md`); neue Befunde dort gibt es
  aus diesem Durchgang nicht. Mit echten Portfoliodaten wurde nicht gemessen (der Testbestand enthält keine
  Buchungen).
- *Nicht prüfbar:* echte KI-Anbieter (kein Schlüssel), reale Yahoo-/CoinGecko-Antworten (Tests nutzen den
  Demo-Anbieter).

---

## 2. Review-Befunde (priorisiert)

Status: **bestätigt** = reproduziert oder gemessen; **Codebeleg** = aus dem Code eindeutig, nicht ausgeführt.
„Paket“ verweist auf Abschnitt 5.

| # | Prio | Befund und Auswirkung | Fundstelle | Beleg | Kleinste Korrektur | Status |
|---|---|---|---|---|---|---|
| B1 | hoch | **Falscher Unternehmenssteckbrief aus der Watchlist.** Der Link „Analyse“ schneidet das Börsenkürzel ab: `SAP.DE` → `/discovery/t/SAP`. Für einen Titel an einem anderen Börsenplatz öffnet sich die Analyse des gleichnamigen US-Tickers – im Zweifel ein anderes Unternehmen. | `app/templates/watchlist.html:36`, `app/watchlist/service.py:136` (`symbol = sym.split(".")[0]`), `app/templates/partials/asset_detail.html:10` | bestätigt: Watchlist-Eintrag `ALPH.DE` → Link `/discovery/t/ALPH` → 303 auf das US-Unternehmen | Verknüpfung nur bei exakt gleichem Yahoo-Symbol; Suchlink nur für Symbole ohne Börsenkürzel | P1, P3, P7 |
| B2 | hoch | **Zwei getrennte „Beobachten“-Zustände.** „Beobachten“ setzt nur `disc_company.status`; „Zur Watchlist“ legt nur einen `watchlist_item` an. Ein beobachtetes Unternehmen fehlt in der Watchlist, ein Watchlist-Titel gilt in Discovery nicht als beobachtet. | `app/discovery/web.py:320-345`, `app/templates/discovery_company.html:73-75`, `app/templates/discovery.html:83` | bestätigt: nach „Zur Watchlist“ bleibt Status `neu`; Status `beobachten` erzeugt keinen Listeneintrag | eine Aktion „Beobachten“ (Status + Liste), abgeleitete Verknüpfung | P1, P4, P5 |
| B3 | mittel | **Zweiter Klick meldet einen Fehler.** „Zur Watchlist“ ein zweites Mal → rote Meldung „Bereits in der Watchlist.“ statt stiller Bestätigung. | `app/discovery/web.py:343-345`, `app/watchlist/service.py:170-172` | bestätigt | „Beobachten“ wiederholbar machen (kein zweiter Eintrag, neutrale Meldung) | P1 |
| B4 | mittel | **Discovery-Liste baut alle Karten zweimal.** `/discovery` ruft `SV.radar()` zweimal auf (Liste und Zähler). Je Unternehmen fallen rund 8 Einzelabfragen an. | `app/discovery/web.py:112-113`, `app/discovery/service.py:995-1030` | gemessen: 769 Abfragen bei 24 Unternehmen; mit Patch 579 (Karten einmal) | Karten einmal bauen (`all_cards`), danach filtern | P1 |
| B5 | mittel | **Zwei Menüpunkte für einen Arbeitsablauf** („Watchlist“, „Discovery“), mobil zusätzlich fest im Mehr-Menü verdrahtet – jede Menüänderung braucht drei Stellen. | `app/web/app.py:28-41`, `app/templates/base.html:49-50, 85-96` | Codebeleg | ein Menüpunkt, Alias für die aktive Markierung | P1, P2 |
| B6 | mittel | **Nachrichten-Discovery ist nicht manuell startbar.** Die Route kennt `news`, die Seite hat keinen Knopf. | `app/discovery/web.py:36-37` gegenüber `app/templates/discovery.html:11-23` | bestätigt (`start/news` kommt in keinem Template vor) | Knopf unter „Weitere Suchläufe“ | P4 |
| B7 | niedrig | **Wartegrund und letzter Fehler stehen nur in eingeklappten Bereichen.** Wartet ein Lauf oder schlug der letzte KI-Lauf fehl, zeigt der Kopf des KI-Status das nicht. | `app/templates/partials/discovery_status.html:5-20, 24, 37` | Codebeleg | eine Hinweiszeile im Kopf | P6 |
| B8 | niedrig | **Kein eindeutiger Index auf `disc_company.yahoo_symbol`.** Zwei Unternehmen können dasselbe Symbol tragen (praktisch selten, da aus dem eindeutigen Ticker abgeleitet). | `app/db.py:1116-1117` | Codebeleg | keine Migration; mehrdeutige Symbole werden ausdrücklich nicht verknüpft | P1 |
| B9 | niedrig | **Listen lassen sich nur anlegen**, nicht umbenennen, löschen oder als Standard setzen. | `app/watchlist/module.py:127-134` | Codebeleg | eigene kleine Erweiterung (nicht Teil von 0.114) | offen, Abschnitt 8 |
| B10 | niedrig | **Rest-N+1 in der Discovery-Liste.** Auch nach B4 rund 8 Abfragen je Unternehmen; bei 300 Unternehmen etwa 2 400 Abfragen je Aufruf. | `app/discovery/service.py:1003-1017`, `_cats` | gemessen (198 Abfragen für 24 Unternehmen, 27 ms) | Sammelabfragen für Signale, Kandidaten, Vorschau – erst bei Bedarf | offen, Abschnitt 8 |

Geprüft und **ohne Befund**: Der Wochenjob „beobachtet“ (`app/discovery/engine.py:474-489`) verhungert nicht – er
sortiert nach `updated_at`, und jede Analyse setzt `updated_at` neu (`app/discovery/service.py:607`). Watchlists und
Discovery-Daten samt Status sind im Gesamtexport enthalten (`app/fullexport.py:175-180`, `app/discovery/backup.py`)
und werden ohne Dubletten wiederhergestellt. Die Rücksprungadresse der Statusroute lässt nur Discovery-Pfade zu.

---

## 3. Zielbild

Ein Menüpunkt **„Entdecken & Beobachten“** mit zwei Ansichten (Reiterleiste oben auf beiden Seiten):

| Ansicht | Adresse (unverändert) | Inhalt |
|---|---|---|
| **Beobachtet** (Standardeinstieg) | `/watchlist`, `/watchlist?list=…&sort=…` | Listen, Titel, Kurse; bei verknüpften Unternehmen Analysestand und Portfolia-Score; Hauptaktionen „Titel hinzufügen“ und „Details öffnen“; Abschnitt „Ohne Liste“ für Altbestand |
| **Entdecken** | `/discovery`, `/discovery?cat=…&sort=…&q=…&alle=1` | offene Kandidaten mit Fundgrund und Analysestand; ein primärer Suchstart, übrige unter „Weitere Suchläufe“; öffnen, beobachten, Vorschlag verwerfen |

Nutzerfluss: Kandidat entdecken → „Beobachten“ (ein Klick, Standardliste; andere Liste wählbar) → Titel steht unter
„Beobachtet“ mit Kurs und Analysestand → „Details öffnen“ führt zum Unternehmenssteckbrief → „Position erstellen“
führt wie bisher in die Buchungserfassung.

---

## 4. Entscheidungen (verbindlich für die Umsetzung)

**E1 – Keine neue Tabelle, keine Migration.** Die Verknüpfung Unternehmen ↔ Watchlist-Eintrag wird abgeleitet:
ein Eintrag mit `quote_source='yahoo'` gehört zu genau dem Unternehmen, dessen Yahoo-Symbol
(`yahoo_symbol`, ersatzweise `ticker`) **exakt** der `quote_id` entspricht (Großschreibung egal).
Folgen: `SAP.DE` ist nie das US-Listing `SAP`; CoinGecko-Einträge sind nie verknüpft; Export und Wiederherstellung
bleiben unverändert. Verworfene Alternative: Spalte `company_id` in `watchlist_item` – bräuchte Migration,
Exporterweiterung und ID-Umschreibung bei der Wiederherstellung, ohne Zusatznutzen.

**E2 – Mehrdeutigkeit wird nicht geraten.** Tragen mehrere Unternehmen dasselbe Symbol, wird nicht verknüpft; die
Zeile zeigt „Zuordnung mehrdeutig“. „Beobachten“ setzt dann nur den Status (Titel erscheint unter „Ohne Liste“).

**E3 – Was „beobachtet“ heißt.** Ein Unternehmen gilt als beobachtet, wenn Status `beobachten` **oder** mindestens
eine Listenmitgliedschaft besteht. Der Status steuert unverändert die Jobs (`beobachtet`, `refresh_plan`); die
Mitgliedschaft steuert Listen und Kurse.

**E4 – Aktionen.**

| Aktion | Wirkung |
|---|---|
| „Beobachten“ (Karte, Steckbrief, alte Adressen) | Status → `beobachten`; Eintrag in gewählter Liste (Standardliste, wenn nichts gewählt). Wiederholung: kein zweiter Eintrag, neutrale Meldung. Ist die Liste voll (200), Fehlermeldung ohne Statusänderung. |
| Aus einer Liste entfernen | nur dieser Eintrag. War es die **letzte** Liste und der Status `beobachten`, wird der Status `neu`. Unternehmen, Analysen, Signale bleiben. |
| „Nicht mehr beobachten“ im Abschnitt „Ohne Liste“ | Status → `neu` (bestehende Statusroute). |
| „Vorschlag verwerfen“ | nur Status → `verworfen`. Listenmitgliedschaften bleiben; der Titel bleibt unter „Beobachtet“, verschwindet aus den offenen Kandidaten. |
| Titel manuell zur Watchlist hinzufügen | wie bisher; ändert keinen Discovery-Status. Passt das Symbol exakt, ist die Zeile automatisch verknüpft. |

Begründung für `neu` statt `zurueckgestellt` beim Entfernen der letzten Liste: Der Nutzer hat die Beobachtung
beendet, den Vorschlag aber nicht bewertet; `neu` hält die tägliche Datenaktualisierung (wie für jeden offenen
Kandidaten) und lässt „Vorschlag verwerfen“ als getrennte, bewusste Entscheidung.

**E5 – Adressen bleiben.** Keine neue URL, keine Weiterleitung. `/watchlist` ist „Beobachtet“, `/discovery` ist
„Entdecken“. `POST /discovery/c/{id}/watchlist` und `POST …/status` mit `status=beobachten` bleiben gültig und
verhalten sich wie „Beobachten“. Neu ist nur `POST /discovery/c/{id}/beobachten` (Felder `list`, `back`).

**E6 – Kein KI- oder Netzaufruf** durch Öffnen, Beobachten, Entfernen. „Beobachten“ ruft bewusst keinen Kursabruf
auf; die Seite „Beobachtet“ aktualisiert fällige Kurse wie bisher im Hintergrund.

**E7 – Analysestand unter „Beobachtet“ nur aus gespeicherten Daten** (`SV.briefs`): Bearbeitungsstand
(`SV.STAGES`), Portfolia-Score (Datenteil /65 bzw. gesamt /125), Datum. Kein Abruf, keine KI.

**E8 – Nicht Teil von 0.114:** Änderungen an Scoring, Tom-Nash-Rekonstruktion, Valuation, Discovery-Algorithmen,
Ledger, Steuer, Import, API-Schlüsseln; Listen umbenennen/löschen (B9); Sammelabfragen (B10).

---

## 5. Arbeitspakete

Regeln für alle Pakete:

1. Branch `feature/0114-entdecken-beobachten` von `claude/portfolia-dashboard-s9p6zr` anlegen.
2. Vor jeder Änderung an sichtbarem Text: `grep -rn "<alter Text>" tests/` – betroffene Tests anpassen, nie löschen.
3. In Templates gilt die Projektregel aus `tests/test_takeover_wallet_mapping.py::test_check_labels_wrap_mixed_text_in_one_element`:
   In `<label class="check">` muss gemischter Text mit `<b>`/`<a>` in **einem** `<span>` stehen.
4. Zeilenlänge Python ≤ 120 Zeichen (Ruff E501). Keine Modellnamen in Commit-Texten oder Code.
5. Nach jedem Paket: `ruff check app tests scripts` und die im Paket genannten Tests.
6. Keine echten KI-Aufrufe in Tests; nur die Fixtures aus `tests/test_discovery.py` (`make_client`) verwenden.

### P0 – Vorbereitung

```bash
git fetch origin claude/portfolia-dashboard-s9p6zr
git checkout -B feature/0114-entdecken-beobachten origin/claude/portfolia-dashboard-s9p6zr
TZ=Europe/Berlin .venv/bin/python -m pytest -q -p no:cacheprovider -rA tests/test_discovery.py tests/test_discovery2.py \
  tests/test_discovery3.py tests/test_portfolio_ux.py tests/test_0113.py 2>&1 | grep -E "^(FAILED|ERROR )"   # erwartet: leer
```

### P1 – Backend-Patch anwenden (bereits geprüft)

```bash
git apply --3way docs/plan_0114/backend.patch
.venv/bin/ruff check app tests scripts
TZ=Europe/Berlin .venv/bin/python -m pytest -q -p no:cacheprovider -rA tests/test_0114.py 2>&1 | grep -cE "^PASSED"   # erwartet: 10
```

Der Patch wurde auf `39479ea` angewendet und geprüft: 10 neue Tests grün, dazu `test_discovery*`,
`test_portfolio_ux`, `test_restore_roundtrip`, `test_full_export`, `test_web`, `test_accounts_hub`, `test_0112`,
`test_0113` ohne Fehler. Lässt er sich nicht anwenden (Basis hat sich bewegt), die Änderungen von Hand nachziehen –
Inhalt:

| Datei | Änderung |
|---|---|
| `app/discovery/watch.py` (neu) | `symbol_of`, `symbol_index`, `company_for`, `memberships`, `watched_index`, `unlisted`, `count_watched`, `observe`, `after_item_removed` – Regeln E1–E4 |
| `app/watchlist/service.py` | öffentlicher Name `YAHOO_SYMBOL` für das vorhandene Muster `_YAHOO` |
| `app/discovery/service.py` | `all_cards(ctx)` (Karten einmal bauen, mit `watched`, `lists`), `radar(..., base=None)`, `_cats(..., watched)`, `briefs(db, cids)` |
| `app/discovery/web.py` | `_safe_back`, `_with_msg`, `_observe_msg`; Route `POST /discovery/c/{cid}/beobachten`; Statusroute und alte Watchlist-Route rufen `W.observe`; Liste baut Karten einmal; Kontext `watch_count`, `watch_members`, `watch_lists`, `watched` |
| `app/watchlist/module.py` | Seitenkontext `link`, `briefs`, `unlisted`, `ambiguous`, `stages`, `watch_count`; Entfernen ruft `after_item_removed` und nimmt `back` an; Detailseite erhält `company_id` |
| `app/web/app.py`, `app/templates/base.html` | ein Menüpunkt „Entdecken & Beobachten“ (`/watchlist`), `NAV_ALIASES = {"discovery": "watchlist"}` für die aktive Markierung, Mehr-Menü mit einem Eintrag |
| `tests/test_0114.py` (neu), `tests/test_discovery.py:627` | Regressionstests Teil A; Menü-Erwartung angepasst |

**Wichtig:** Nach P1 ist „Entdecken“ nur über die Adresse erreichbar, bis P2 die Reiter einbaut. P1 und P2 nicht
getrennt ausliefern.

Kontextvariablen, die die Templates nach P1 erhalten:

| Template | Variable | Inhalt |
|---|---|---|
| `watchlist.html` | `link` | `{item_id: company_id oder None}` (nur Yahoo-Einträge) |
| | `briefs` | `{company_id: {id, ticker, name, status, stage, analysed_at, nash}}` – `nash` wie in den Discovery-Karten (`nash.quali_open`, `nash.data.pts`, `nash.total`) oder `None` |
| | `unlisted` | Liste `{id, ticker, name, yahoo_symbol}` – Status `beobachten` ohne Liste |
| | `ambiguous` | Menge von `item_id` mit mehrdeutigem Symbol |
| | `stages` | `SV.STAGES` (Schlüssel → Beschriftung) |
| | `watch_count` | Anzahl beobachteter Titel |
| `discovery.html` | `watch_count`; je Karte `c.watched` (bool), `c.lists` (Listennamen) | |
| `discovery_company.html` | `watched` (bool), `watch_members` (`[{item_id, list_id, list_name}]`), `watch_lists` (alle Listen: `id`, `name`, `is_default`) | |
| `watchlist_item.html` | `company_id` (oder `None`) | |

### P2 – Reiterleiste und Überschriften

Neue Datei `app/templates/partials/watch_tabs.html`:

```jinja
{# Reiter „Entdecken & Beobachten“ (0.114). Erwartet: watch_tab ('beobachtet' | 'entdecken'), watch_count. #}
<nav class="tabs" aria-label="Entdecken &amp; Beobachten">
  <a class="{{ 'active' if watch_tab == 'beobachtet' }}" href="/watchlist"{% if watch_tab == 'beobachtet' %} aria-current="page"{% endif %}>Beobachtet{% if watch_count %} <span class="badge">{{ watch_count }}</span>{% endif %}</a>
  <a class="{{ 'active' if watch_tab == 'entdecken' }}" href="/discovery"{% if watch_tab == 'entdecken' %} aria-current="page"{% endif %}>Entdecken</a>
</nav>
```

Einbinden als **erstes Element** im Inhaltsblock:

- `app/templates/watchlist.html`: direkt nach `{% block content %}`:
  `{% with watch_tab = 'beobachtet' %}{% include "partials/watch_tabs.html" %}{% endwith %}`
- `app/templates/discovery.html`: direkt nach `<div class="stack disc-page" data-keep-scroll>`:
  `{% with watch_tab = 'entdecken' %}{% include "partials/watch_tabs.html" %}{% endwith %}`

Überschriften:

| Datei | `{% block title %}` | `{% block heading %}` |
|---|---|---|
| `watchlist.html` | `Beobachtet` | `Entdecken &amp; Beobachten` |
| `discovery.html` | `Entdecken` | `Entdecken &amp; Beobachten` |
| `discovery_company.html` | unverändert | `<a href="/discovery" class="hint">Entdecken</a> › {{ co.ticker }}` |
| `watchlist_item.html` | `{{ it.sym }} – Beobachtet` | unverändert |

In `watchlist_item.html` den Linktext `Watchlist` (Zeile 8, `href="/watchlist?list=…"`) in `Beobachtet` ändern und,
wenn `company_id` gesetzt ist, daneben ergänzen:
` · <a href="/discovery/c/{{ company_id }}">Unternehmenssteckbrief</a>`.

Die Klasse `.tabs` existiert bereits (siehe `app/templates/settings.html:7`) – kein neues CSS nötig.

### P3 – Ansicht „Beobachtet“ (`app/templates/watchlist.html`)

1. Zeile 2 ergänzen: `{% from "discovery_macros.html" import pts with context %}`.
2. Kartenkopf: in das `<div class="row" …>` rechts als erstes Element
   `<a class="btn sm primary" href="#add">Titel hinzufügen</a>`.
3. In der Zeilenschleife (`{% for it in items %}`), direkt nach `<tr id="wl-{{ it.id }}">`:

```jinja
{% set cid = link.get(it.id) %}{% set b = briefs.get(cid) if cid else none %}
{% set detail_url = ('/discovery/c/' ~ cid) if cid else ('/watchlist/item/' ~ it.id) %}
```

4. Erste Zelle (`<td data-label="Wert">`): den Link um das Symbol von `href="/watchlist/item/{{ it.id }}"` auf
   `href="{{ detail_url }}"` umstellen. In der vorhandenen `<div class="hint">`-Zeile am Ende ergänzen:
   `{% if it.id in ambiguous %} · <span class="badge warn" title="Mehrere Discovery-Unternehmen tragen dieses Symbol – keine automatische Zuordnung">Zuordnung mehrdeutig</span>{% endif %}`.
   Danach, noch in der Zelle, eine neue Zeile:

```jinja
{% if b %}<div class="hint wl-brief"><span class="badge {{ {'analysiert': 'good', 'veraltet': 'warn', 'vorschau': 'info'}.get(b.stage, '') }}">{{ stages[b.stage] }}</span>{% if b.nash %} · Portfolia-Score {% if b.nash.quali_open %}<b>{{ pts(b.nash.data.pts) }}</b>/65 Daten · qualitativ offen{% else %}<b>{{ pts(b.nash.total) }}</b>/125{% endif %}{% endif %}{% if b.status == 'verworfen' %} · Vorschlag verworfen{% endif %}</div>{% endif %}
```

5. Aktionszelle: den Link `Details` und den Link `Analyse` (Zeilen 35–36) ersetzen durch:

```jinja
<a class="btn sm ghost" href="{{ detail_url }}">Details öffnen</a>
{% if cid %}<a class="btn sm ghost" href="/watchlist/item/{{ it.id }}">Kursverlauf</a>
{% elif it.quote_source == 'yahoo' and not it.is_index and '.' not in it.quote_id and '=' not in it.quote_id and it.id not in ambiguous %}<a class="btn sm ghost" href="/discovery/t/{{ it.quote_id|urlencode }}" title="Im SEC-Verzeichnis suchen und als Kandidat aufnehmen">Analyse suchen</a>{% endif %}
```

   „Position erstellen“, Verschieben und Entfernen bleiben unverändert. Im Entfernen-Formular nur den Text
   `aus der Watchlist entfernen?` in `aus dieser Liste entfernen?` ändern.
6. Leerer Zustand (Zeile 46): Text **beginnt weiterhin mit „Noch keine Einträge“** (ein Bestandstest prüft das):
   `Noch keine Einträge – unten einen Titel hinzufügen oder unter <a href="/discovery">Entdecken</a> Kandidaten ansehen.`
7. Neuer Abschnitt direkt **nach** der ersten `</section>` (vor `<section class="card" id="add">`):

```jinja
{% if unlisted %}
<section class="card" id="ohne-liste">
  <div class="card-head"><h2>Ohne Liste <span class="badge">{{ unlisted|length }}</span></h2><span class="hint">in Discovery beobachtet, noch in keiner Liste</span></div>
  <div class="table-wrap cards-wrap"><table class="compact cards"><thead><tr><th>Unternehmen</th><th>Analysestand</th><th></th></tr></thead><tbody>
  {% for u in unlisted %}{% set b = briefs.get(u.id) %}
    <tr><td data-label="Unternehmen"><a href="/discovery/c/{{ u.id }}"><b>{{ u.ticker }}</b></a> <span class="hint">{{ u.name }}</span></td>
      <td data-label="Analysestand">{% if b %}{{ stages[b.stage] }}{% if b.nash %} · Portfolia-Score {% if b.nash.quali_open %}{{ pts(b.nash.data.pts) }}/65 Daten{% else %}{{ pts(b.nash.total) }}/125{% endif %}{% endif %}{% else %}–{% endif %}</td>
      <td class="nowrap actions">
        <a class="btn sm ghost" href="/discovery/c/{{ u.id }}">Details öffnen</a>
        <form method="post" action="/discovery/c/{{ u.id }}/beobachten" class="inline"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><input type="hidden" name="list" value="{{ list_id }}"><input type="hidden" name="back" value="/watchlist?list={{ list_id }}"><button class="btn sm" type="submit">In diese Liste aufnehmen</button></form>
        <form method="post" action="/discovery/c/{{ u.id }}/status" class="inline" data-confirm="{{ u.ticker }} nicht mehr beobachten? Unternehmen und Analysen bleiben erhalten."><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><input type="hidden" name="status" value="neu"><input type="hidden" name="back" value="/watchlist?list={{ list_id }}"><button class="btn sm ghost" type="submit">Nicht mehr beobachten</button></form>
      </td></tr>
  {% endfor %}
  </tbody></table></div>
  <p class="hint">Diese Unternehmen wurden vor 0.114 nur in Discovery beobachtet oder haben kein eindeutiges Kurssymbol. Die automatische Datenaktualisierung läuft für sie weiter.</p>
</section>
{% endif %}
```

8. Kartenüberschrift „Hinzufügen“ → „Titel hinzufügen“. Der Hinweistext am Ende der ersten Karte bleibt.

Hinweis: Die Seite zeigt Fehlermeldungen nur über `errors`. Meldungen der Discovery-Routen kommen als
`?msg=…`/`?err=…` an `/watchlist`. Deshalb in `app/watchlist/module.py`:

- Route `page`: Signatur um `msg: str = "", err: str = ""` erweitern und an `_page(...)` durchreichen
  (`_page` um die Parameter `msg: str = ""`, `err: str = ""` erweitern; `errors = (errors or []) + ([err] if err else [])`;
  `msg` zusätzlich an `render(...)` übergeben).
- In `watchlist.html` direkt unter der Reiterleiste:
  `{% if msg %}<div class="alert good" data-flash><span class="icon">✓</span><div>{{ msg }}</div></div>{% endif %}`.

### P4 – Ansicht „Entdecken“ (`app/templates/discovery.html`)

1. Rücksprungadresse mit Filtern – direkt nach der Reiterleiste einfügen:

```jinja
{% set bq = [] %}{% if cat %}{% set _ = bq.append('cat=' ~ cat|urlencode) %}{% endif %}{% if sort %}{% set _ = bq.append('sort=' ~ sort|urlencode) %}{% endif %}{% if q %}{% set _ = bq.append('q=' ~ q|urlencode) %}{% endif %}{% if alle %}{% set _ = bq.append('alle=1') %}{% endif %}
{% set back_url = '/discovery' ~ ('?' ~ bq|join('&') if bq else '') %}
```

2. Karte „Research starten“ (`<section class="card" id="aktionen">`, Zeilen 11–26) neu aufbauen. Überschrift:
   „Kandidaten suchen“. Die drei vorhandenen Formulare bleiben wörtlich erhalten (Texte, `disabled`-Logik), nur die
   Anordnung ändert sich:

   - **Primärer Start** (ein Formular, sichtbar): wenn `st.startable` das Formular „KI-Discovery starten“, sonst das
     Formular „Fundamentalscreening starten“. Das `<form>`-Element erhält zusätzlich
     `data-primary-start="ki_discovery"` bzw. `data-primary-start="screening"`; der Knopf hat `class="btn primary"`.
   - Das Formular „Unternehmen direkt aufnehmen“ (`action="/discovery/add"`) bleibt **sichtbar** direkt darunter.
   - Danach `<details class="box" id="weitere-suchlaeufe"><summary>Weitere Suchläufe</summary> … </details>` mit
     den übrigen Formularen in `<div class="disc-start">`: das jeweils nicht primäre aus
     KI-Discovery/Fundamentalscreening, „Vollständigen Discovery-Lauf starten“ und **neu**:

```jinja
<form method="post" action="/discovery/start/news" class="disc-start-item"><input type="hidden" name="csrf_token" value="{{ csrf_token }}">
  <button class="btn" type="submit">Nachrichten-Discovery starten</button>
  <p class="hint">Sucht in den bereits analysierten Meldungen der letzten {{ cfg.news_days }} Tage nach börsennotierten Unternehmen – ohne KI-Aufruf.</p></form>
```

   Ist KI nicht startbar, bleibt der KI-Knopf unter „Weitere Suchläufe“ sichtbar, aber `disabled` (wie heute).

3. Karten (`<article class="card disc-card" id="karte-{{ c.id }}">`):
   - Am `<article>` **direkt hinter** `id="karte-{{ c.id }}"` ergänzen: `{% if c.watched %} data-watched="1"{% endif %}`
     (ergibt `id="karte-7" data-watched="1"`).
   - In der `hint`-Zeile die Bedingung `{% if c.status != 'neu' %}` so ändern, dass der Status `beobachten` nicht
     doppelt erscheint: `{% if c.status not in ('neu', 'beobachten') %}`. Davor ergänzen:
     `{% if c.watched %} · <span class="badge good">beobachtet</span>{% if c.lists %} <span class="hint">({{ c.lists|join(', ') }})</span>{% endif %}{% endif %}`.
   - Aktionen: die beiden Statusformulare (Zeilen 83–84) ersetzen durch:

```jinja
{% if c.watched %}<a class="btn sm ghost" href="/watchlist">Beobachtet ✓</a>
{% else %}<form method="post" action="/discovery/c/{{ c.id }}/beobachten"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><input type="hidden" name="back" value="{{ back_url }}"><button class="btn sm ghost" type="submit">Beobachten</button></form>{% endif %}
{% if c.status != 'verworfen' %}<form method="post" action="/discovery/c/{{ c.id }}/status"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><input type="hidden" name="status" value="verworfen"><input type="hidden" name="back" value="{{ back_url }}"><button class="btn sm ghost" type="submit" title="Nur der Vorschlag wird verworfen – Listen unter „Beobachtet“ bleiben unverändert">Vorschlag verwerfen</button></form>{% endif %}
```

   - Der Knopf „Mit KI analysieren“ und „Öffnen“ bleiben unverändert.
4. Kategorie-Leiste: Die Kategorie `beobachten` bleibt (alte Links). Keine weitere Änderung.
5. Text des leeren Zustands (Zeile 90) unverändert lassen.

### P5 – Unternehmenssteckbrief (`app/templates/discovery_company.html`)

Im Block `<div class="disc-actions">` die Zeilen 73–75 (Statusschleife und Formular „Zur Watchlist“) ersetzen durch:

```jinja
{% if not watched %}
<form method="post" action="/discovery/c/{{ co.id }}/beobachten" class="row" style="gap:6px"><input type="hidden" name="csrf_token" value="{{ csrf_token }}">
  {% if watch_lists|length > 1 %}<label class="sr-only" for="beob-liste">Liste</label><select id="beob-liste" name="list">{% for l in watch_lists %}<option value="{{ l.id }}" {{ 'selected' if l.is_default }}>{{ l.name }}</option>{% endfor %}</select>{% endif %}
  <button class="btn" type="submit">Beobachten</button></form>
{% endif %}
{% for stx, label in [('verworfen', 'Vorschlag verwerfen'), ('neu', 'Status zurücksetzen')] if stx != co.status and not (stx == 'neu' and co.status == 'beobachten') %}
<form method="post" action="/discovery/c/{{ co.id }}/status"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><input type="hidden" name="status" value="{{ stx }}"><button class="btn ghost" type="submit">{{ label }}</button></form>{% endfor %}
```

Direkt **nach** dem schließenden `</div>` von `disc-actions` (vor dem `<p class="hint">` mit dem KI-Hinweis) einfügen:

```jinja
{% if watched %}
<div class="disc-watch" id="beobachtung">
  {% if watch_members %}<span><b>Beobachtet in:</b></span>
    {% for m in watch_members %}<form method="post" action="/watchlist/item/{{ m.item_id }}/remove" class="inline" data-confirm="{{ co.ticker }} aus der Liste „{{ m.list_name }}“ entfernen?"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><input type="hidden" name="back" value="/discovery/c/{{ co.id }}"><a href="/watchlist?list={{ m.list_id }}">{{ m.list_name }}</a> <button class="icon-btn" type="submit" aria-label="aus „{{ m.list_name }}“ entfernen" title="aus dieser Liste entfernen">×</button></form>{% endfor %}
  {% else %}<span><b>Beobachtet (ohne Liste)</b></span>
    <form method="post" action="/discovery/c/{{ co.id }}/status" class="inline"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><input type="hidden" name="status" value="neu"><button class="btn sm ghost" type="submit">Nicht mehr beobachten</button></form>{% endif %}
  {% set member_ids = watch_members|map(attribute='list_id')|list %}{% set rest = watch_lists|rejectattr('id', 'in', member_ids)|list %}
  {% if rest %}<form method="post" action="/discovery/c/{{ co.id }}/beobachten" class="inline row" style="gap:6px"><input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    {% if rest|length > 1 %}<label class="sr-only" for="beob-weitere">Weitere Liste</label><select id="beob-weitere" name="list">{% for l in rest %}<option value="{{ l.id }}">{{ l.name }}</option>{% endfor %}</select>{% else %}<input type="hidden" name="list" value="{{ rest[0].id }}">{% endif %}
    <button class="btn sm ghost" type="submit">{% if rest|length > 1 %}Auch in diese Liste{% else %}Auch in „{{ rest[0].name }}“{% endif %}</button></form>{% endif %}
</div>
{% endif %}
```

In der Kopfzeile des Steckbriefs (Zeile 18) `Status: <b>{{ statuses[co.status] }}</b>` unverändert lassen.
Alles Übrige der Seite (Portfolia-Score / 125, Datenteil / qualitativer Teil, Herkunft Tom-Nash-Rekonstruktion,
Belege, Historie, KI-Knöpfe) bleibt unangetastet.

### P6 – KI-Status kompakt (`app/templates/partials/discovery_status.html`)

Direkt nach dem schließenden `</div>` von `class="card-head"` (Zeile 11) und vor den vorhandenen `alert`-Zeilen eine
Hinweiszeile einfügen. Sie erscheint nur, wenn ein Lauf wartet oder der jüngste KI-Aufruf fehlschlug:

```jinja
{% set w = st.waiting[0] if st.waiting else none %}
{% set failed_last = st.last_fail and (not st.last_ok or st.last_fail.started_at > st.last_ok.started_at) %}
{% if w or failed_last %}<p class="hint" id="ki-hinweis">
  {% if w %}<span class="badge warn">wartet</span> {{ w.kind_label }} wartet bis {{ w.wait_until|datetime_de }}{% if w.wait_reason %} – {{ w.wait_reason }}{% endif %}{% if st.waiting|length > 1 %} · +{{ st.waiting|length - 1 }} weitere{% endif %}. {% endif %}
  {% if failed_last %}<span class="badge crit">Fehler</span> Letzter KI-Aufruf am {{ st.last_fail.started_at|datetime_de }} fehlgeschlagen: {{ st.last_fail.step }} – {{ st.last_fail.error or st.last_fail.status }}. {% endif %}
  <a href="/discovery/protokoll">Protokoll</a></p>{% endif %}
```

Vor dem Einbau prüfen, dass die Einträge in `st.waiting` die Felder `kind_label`, `wait_until`, `wait_reason`
tragen (`app/discovery/jobs.py`, Funktion `waiting`/`get`); die Tabelle „Warteschlange“ weiter unten in derselben
Datei nutzt dieselben Felder. Alles andere im Partial bleibt.

### P7 – Falschen Analyse-Link im Asset-Detail beheben (`app/templates/partials/asset_detail.html:10`)

Alt: Bedingung `… and not a.quote_id.startswith('^')` mit Ziel `/discovery/t/{{ a.quote_id.split('.')[0]|urlencode }}`.
Neu: Bedingung um `and '.' not in a.quote_id and '=' not in a.quote_id` erweitern, Ziel
`/discovery/t/{{ a.quote_id|urlencode }}`. Für `SAP.DE` entfällt der Link damit (Discovery kennt nur SEC-Emittenten),
für `AAPL` bleibt er.

### P8 – CSS (`app/static/css/app.css`, am Ende anhängen)

```css
/* 0.114 Entdecken & Beobachten */
.wl-brief { margin-top: 2px; }
.disc-watch { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; margin-top: 8px; }
.disc-watch form.inline { display: inline-flex; align-items: center; gap: 4px; }
```

Nur vorhandene Farbvariablen verwenden (keine festen Farben – Hell/Dunkel muss ohne Zusatzregeln stimmen).

### P9 – Oberflächentests (an `tests/test_0114.py` anhängen)

Die folgenden Tests sind die Abnahme für P2–P7. Sie sind gegen die oben vorgegebenen Marker geschrieben, aber
**noch nicht gegen fertige Templates gelaufen**: Stimmt ein Marker wegen eines Tippfehlers im Test nicht, den Test
korrigieren – die geprüfte Aussage darf nicht abgeschwächt werden.

```python
# ------------------------------------------------------------------------------------------- Teil B: Oberfläche

def row_of(page: str, item_id: int) -> str:
    return page.split(f'id="wl-{item_id}"')[1].split("</tr>")[0]


def card_of(page: str, cid: int) -> str:
    return page.split(f'id="karte-{cid}"')[1].split("</article>")[0]


def test_tabs_on_both_views(c):
    cid_of(c, "ALPH")
    for url, active in (("/watchlist", "/watchlist"), ("/discovery", "/discovery")):
        page = c.get(url).text
        assert '<nav class="tabs" aria-label="Entdecken &amp; Beobachten">' in page, url
        tabs = page.split('aria-label="Entdecken &amp; Beobachten">')[1].split("</nav>")[0]
        assert 'href="/watchlist"' in tabs and 'href="/discovery"' in tabs
        assert re.search(rf'class="active" href="{active}" aria-current="page"', tabs), url


def test_watched_view_shows_lists_links_and_analysis_state(c):
    ctx = c.app.state.ctx
    ws = watchlist_service(ctx)
    alph, beta = cid_of(c, "ALPH"), cid_of(c, "BETA")
    post(c, f"/discovery/c/{alph}/analyse", mode="daten")
    post(c, f"/discovery/c/{alph}/beobachten")
    SV.set_status(ctx.db, beta, "beobachten")  # Altbestand ohne Liste
    for sym in ("ALPH.DE", "ZZZZ"):  # anderer Börsenplatz / US-Symbol ohne Discovery-Eintrag
        entry, _e, _c = ws.resolve("security", sym)
        ws.add(ws.default_id(), entry)
    ids = {r["quote_id"]: int(r["id"]) for r in ctx.db.q("SELECT id, quote_id FROM watchlist_item")}
    page = c.get("/watchlist").text
    assert 'href="#add">Titel hinzufügen</a>' in page
    linked = row_of(page, ids["ALPH"])
    assert f'href="/discovery/c/{alph}"' in linked and "Details öffnen" in linked and "Kursverlauf" in linked
    assert SV.STAGES["daten"] in linked and "Portfolia-Score" in linked and "/65 Daten" in linked
    other = row_of(page, ids["ALPH.DE"])
    assert "/discovery/" not in other and f'href="/watchlist/item/{ids["ALPH.DE"]}"' in other  # kein US-Steckbrief
    plain = row_of(page, ids["ZZZZ"])
    assert 'href="/discovery/t/ZZZZ"' in plain and "Analyse suchen" in plain
    section = page.split('id="ohne-liste"')[1].split("</section>")[0]
    assert "Beta Photonics" in section and f'action="/discovery/c/{beta}/beobachten"' in section
    assert 'name="status" value="neu"' in section and "Nicht mehr beobachten" in section
    r = post(c, f"/discovery/c/{beta}/status", status="neu", back="/watchlist?list=1")
    assert r.headers["location"] == "/watchlist?list=1" and 'id="ohne-liste"' not in c.get("/watchlist").text
    ctx.db.x("UPDATE disc_company SET yahoo_symbol='ALPH' WHERE id=?", (beta,))  # zwei Unternehmen, ein Symbol
    assert "Zuordnung mehrdeutig" in row_of(c.get("/watchlist").text, ids["ALPH"])


def test_empty_watched_view_points_to_discover(c):
    page = c.get("/watchlist").text
    assert "Noch keine Einträge" in page
    assert 'href="/discovery">Entdecken</a>' in page.split("Noch keine Einträge")[1][:200]  # Hinweis im Leertext


def test_discover_view_primary_start_and_cards(c, monkeypatch):
    ctx = c.app.state.ctx
    alph, beta = cid_of(c, "ALPH"), cid_of(c, "BETA")
    page = c.get("/discovery?sort=signal").text
    assert 'data-primary-start="screening"' in page and 'data-primary-start="ki_discovery"' not in page  # KI aus
    more = page.split('id="weitere-suchlaeufe"')[1].split("</details>")[0]
    assert "Weitere Suchläufe" in more
    for kind in ("ki_discovery", "voll", "news"):
        assert f'action="/discovery/start/{kind}"' in more, kind
    assert 'action="/discovery/add"' in page.split('id="weitere-suchlaeufe"')[0]  # manuelle Aufnahme sichtbar
    card = card_of(page, alph)
    assert f'action="/discovery/c/{alph}/beobachten"' in card and "Vorschlag verwerfen" in card
    assert 'name="back" value="/discovery?sort=signal"' in card and 'data-watched="1"' not in page
    post(c, f"/discovery/c/{alph}/beobachten")
    page = c.get("/discovery").text
    assert f'id="karte-{alph}" data-watched="1"' in page
    card = card_of(page, alph)
    assert f'action="/discovery/c/{alph}/beobachten"' not in card and "Beobachtet ✓" in card and "(Watchlist)" in card
    assert f'action="/discovery/c/{beta}/beobachten"' in card_of(page, beta)
    from app.news import ai as NAI

    ctx.settings.set("llm.enabled", True)
    monkeypatch.setattr(NAI.AiGateway, "key_present", lambda self, provider=None: True)
    assert 'data-primary-start="ki_discovery"' in c.get("/discovery").text
    r = post(c, "/discovery/start/news")
    assert r.status_code == 303 and "Nachrichten-Discovery" in loc(r)


def test_company_page_has_one_observe_control(c):
    ctx = c.app.state.ctx
    alph = cid_of(c, "ALPH")
    page = c.get(f"/discovery/c/{alph}").text
    assert "Zur Watchlist" not in page and f'action="/discovery/c/{alph}/watchlist"' not in page
    assert page.count(f'action="/discovery/c/{alph}/beobachten"') == 1 and '<select id="beob-liste"' not in page
    assert "Vorschlag verwerfen" in page and 'id="beobachtung"' not in page
    second = int(post(c, "/watchlist/lists", name="Langfristig").headers["location"].split("list=")[1])
    assert '<select id="beob-liste" name="list">' in c.get(f"/discovery/c/{alph}").text  # Liste wählbar
    post(c, f"/discovery/c/{alph}/beobachten", list=str(second))
    page = c.get(f"/discovery/c/{alph}").text
    block = page.split('id="beobachtung"')[1].split("</div>")[0]
    item = W.memberships(ctx.db, SV.company(ctx.db, alph))[0]["item_id"]
    assert "Beobachtet in:" in block and "Langfristig" in block
    assert f'action="/watchlist/item/{item}/remove"' in block and f'name="back" value="/discovery/c/{alph}"' in block
    assert "Auch in „Watchlist“" in block and page.count(f'action="/discovery/c/{alph}/beobachten"') == 1
    assert "Portfolia-Score" in page  # Steckbrief unverändert vorhanden
    post(c, f"/watchlist/item/{item}/remove", back=f"/discovery/c/{alph}")
    SV.set_status(ctx.db, alph, "beobachten")
    assert "Beobachtet (ohne Liste)" in c.get(f"/discovery/c/{alph}").text


def test_ai_status_shows_waiting_and_failure_reason(c, monkeypatch):
    from app.discovery import jobs as J
    from app.news import ai as NAI

    ctx = c.app.state.ctx
    assert "KI deaktiviert" in c.get("/discovery").text and 'id="ki-hinweis"' not in c.get("/discovery").text
    ctx.settings.set("llm.enabled", True)
    monkeypatch.setattr(NAI.AiGateway, "key_present", lambda self, provider=None: True)
    assert "KI bereit" in c.get("/discovery").text
    jid, _ = J.create(ctx, "ki_discovery")
    ctx.db.x("UPDATE disc_job SET status='wartet', wait_until='2999-01-01T00:00:00+00:00', wait_reason='Tageslimit' "
             "WHERE id=?", (jid,))
    hint = c.get("/discovery").text.split('id="ki-hinweis"')[1].split("</p>")[0]
    assert "wartet bis" in hint and "Tageslimit" in hint and 'href="/discovery/protokoll"' in hint
    ctx.db.x("UPDATE disc_job SET status='abgebrochen', wait_until=NULL WHERE id=?", (jid,))
    ctx.db.x("INSERT INTO disc_ai_call(job_id, step, status, error, started_at) VALUES (?,?,?,?,?)",
             (jid, "themen", "fehler", "Zeitüberschreitung beim Anbieter", "2026-11-11T10:00:00+00:00"))
    hint = c.get("/discovery").text.split('id="ki-hinweis"')[1].split("</p>")[0]
    assert "fehlgeschlagen" in hint and "Zeitüberschreitung beim Anbieter" in hint
    assert "KI-Discovery starten" in c.get("/discovery").text  # manueller Start bleibt sichtbar
```

Zusätzlich in `tests/test_portfolio_ux.py` (dort gibt es die Fixture `client` mit Beispielportfolio; die Assets
`WKN:716460` = `SAP.DE` und `WKN:865985` = `AAPL` sind im Beispielimport enthalten):

```python
def test_asset_detail_links_analysis_only_for_exact_us_symbols(client):
    assert "/discovery/t/" not in client.get("/asset/WKN:716460").text  # SAP.DE: anderer Börsenplatz
    assert 'href="/discovery/t/AAPL"' in client.get("/asset/WKN:865985").text
```

Falls `/asset/<id>` in diesem Projekt eine andere Adresse hat, die Route in `app/web/routes/pages.py` nachsehen
(`grep -n '"/asset' app/web/routes/*.py`) und den Test anpassen.

### P10 – Dokumentation und Version

1. `app/__init__.py`: `__version__ = "0.114.0"`.
2. `docs/DISCOVERY_INTELLIGENCE.md`: Titelzeile auf 0.114.0; neuer Abschnitt „0.10 Neu in 0.114: Entdecken &
   Beobachten“ mit: zwei Ansichten und Adressen, Entscheidungen E1–E7 (Tabelle aus E4 übernehmen), Grenzen
   (nur exakte Yahoo-Symbole werden verknüpft; Titel anderer Börsenplätze haben keinen Steckbrief).
3. `README.md`: im Discovery-Absatz ein Satz zu „Entdecken & Beobachten“.
4. `docs/plan_0114/backend.patch` nach erfolgreicher Umsetzung löschen; in diesem Plan oben den Status auf
   „umgesetzt in 0.114“ setzen.

### P11 – Prüfung

```bash
.venv/bin/ruff check app tests scripts
.venv/bin/mypy app/news app/jobs app/web app/discovery app/health.py app/logging_setup.py --ignore-missing-imports | tail -1   # ≤ 344
TZ=Europe/Berlin .venv/bin/python -m pytest -q -p no:cacheprovider -rA tests/test_0114.py tests/test_0113.py tests/test_0112.py \
  tests/test_discovery.py tests/test_discovery2.py tests/test_discovery3.py tests/test_portfolio_ux.py \
  tests/test_restore_roundtrip.py tests/test_full_export.py tests/test_web.py tests/test_takeover_wallet_mapping.py \
  2>&1 | grep -E "^(FAILED|ERROR )"          # erwartet: leer
TZ=Europe/Berlin .venv/bin/python -m pytest -q -p no:cacheprovider    # Gesamtsuite, ca. 25 min, vor dem Push
.venv/bin/python -m app validate examples/beispiel-import.zip
```

Sichtprüfung mit Playwright (vorinstalliert; kein `playwright install`): App mit Testdaten starten (nie produktive
Daten), dann je Seite `/watchlist`, `/discovery`, `/discovery/c/<id>` in vier Varianten aufnehmen:
Desktop 1280 px und 390 px Breite, jeweils hell und dunkel. Dunkelmodus per
`page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))` nach dem Laden. Je Aufnahme
prüfen: `document.documentElement.scrollWidth <= window.innerWidth` (kein waagerechter Überlauf), Reiterleiste
sichtbar, Knöpfe umbrechen statt abzuschneiden.

### P12 – Auslieferung

Erst nach grüner Gesamtsuite pushen. Ein Commit je logischem Schritt (Backend, Oberfläche, Doku) oder ein
gemeinsamer; Pull Request gegen `claude/portfolia-dashboard-s9p6zr` nur auf ausdrücklichen Wunsch.

---

## 6. Abnahme: Anforderung → Test

| Anforderung aus dem Auftrag | Test |
|---|---|
| Alte Watchlist mit mehreren Listen und nur in Discovery beobachteter Titel bleiben sichtbar | `test_legacy_status_only_company_stays_visible_and_jobs_unchanged`, `test_watched_view_shows_lists_links_and_analysis_state`, `test_old_links_and_single_navigation_entry` |
| Beobachten, erneut beobachten, Details öffnen – genau einmal je Liste | `test_observe_is_idempotent_on_every_entry_point`, `test_lists_remove_one_then_last_keeps_company_and_analysis` |
| Aus einer von mehreren und aus der letzten Liste entfernen | `test_lists_remove_one_then_last_keeps_company_and_analysis` |
| Verwerfen löscht keine Listenmitgliedschaft | `test_discarding_a_suggestion_keeps_list_membership` |
| Börsenplätze, mehrdeutige Symbole, Titel ohne Discovery-Unterstützung | `test_other_exchange_crypto_and_ambiguous_symbols_are_not_linked`, `test_watched_view_shows_lists_links_and_analysis_state`, `test_asset_detail_links_analysis_only_for_exact_us_symbols` |
| Alte Links funktionieren | `test_old_links_and_single_navigation_entry`, `test_back_target_is_restricted_and_keeps_filters` |
| KI aus, wartend, fehlgeschlagen verständlich | `test_ai_status_shows_waiting_and_failure_reason`, bestehend `tests/test_0113.py::test_status_compact_ready_off_and_busy` |
| Öffnen und Beobachten starten keine KI | `test_open_observe_and_remove_never_call_ai_or_start_jobs` |
| Ein Einstieg, zwei Ansichten | `test_tabs_on_both_views`, `test_old_links_and_single_navigation_entry` |
| Ein primärer Suchstart, übrige gebündelt, manuell und Screening ohne KI | `test_discover_view_primary_start_and_cards` |
| Ein „Beobachten“ im Steckbrief, Liste wählbar | `test_company_page_has_one_observe_control` |
| Export und Wiederherstellung | bestehend: `tests/test_restore_roundtrip.py`, `tests/test_full_export.py`, `tests/test_discovery3.py` (keine Schemaänderung) |
| Karten einmal bauen (B4) | `test_radar_builds_cards_once_per_request` |

---

## 7. Stolperfallen

- **`{% set %}` in Jinja-Schleifen** gilt nur innerhalb des Schleifendurchlaufs – `cid`, `b`, `detail_url` deshalb
  innerhalb der Zeile setzen, nicht davor.
- **`&` in Attributwerten** wird von Jinja zu `&amp;` – im Test steht daher
  `value="/discovery?sort=signal"` nur für einen einzelnen Parameter; bei mehreren Parametern `&amp;` erwarten.
- **`↑` darf in der sortierten Watchlist nicht vorkommen** (`tests/test_portfolio_ux.py` prüft das) – keine Pfeile in
  neuen Texten.
- **`id="wl-{id}"` an der Tabellenzeile bleibt** – Tests und die Diagramm-ID in `watchlist_item.html` hängen daran.
- **`W.observe` nie aus `app/watchlist/service.py` aufrufen** – das Watchlist-Modul darf Discovery nur verzögert
  (innerhalb von Funktionen) importieren, sonst entsteht ein Importkreis.
- **Kein Kursabruf in `observe`** – sonst laufen Tests ins Netz und E6 ist verletzt.
- **Statusroute:** `status=beobachten` läuft über `W.observe`; andere Statuswerte ändern nie Listen.
- **Meldungen an `/watchlist`:** ohne die in P3 beschriebene Erweiterung von `page`/`_page` um `msg`/`err` gehen sie
  verloren (FastAPI ignoriert unbekannte Abfrageparameter stillschweigend).
- **Tests mit Fixture `c`** haben kein Portfolio; Asset-Seiten nur mit der Fixture `client` aus
  `tests/test_portfolio_ux.py` prüfen.

---

## 8. Vorschläge außerhalb von 0.114

| Vorschlag | Nutzen | Aufwand | Nächster Schritt |
|---|---|---|---|
| Listen umbenennen, löschen, Standard wechseln (B9) | Listenverwaltung vollständig | klein (3 Routen, 1 Formular, Tests) | eigenes Paket 0.115 |
| Sammelabfragen in `SV.all_cards` (B10) | Discovery-Liste bleibt bei mehreren hundert Unternehmen schnell | mittel (5 Abfragen statt 8 je Unternehmen, Vergleichstest alt/neu) | erst messen, wenn > 150 Unternehmen im Bestand |
| „Beobachtet“ als Kachel auf der Übersicht (Top-Bewegungen der beobachteten Titel) | täglicher Blick ohne Seitenwechsel | klein | nach Rückmeldung zur neuen Ansicht |
| Titel anderer Börsenplätze einem Discovery-Unternehmen **manuell** zuordnen | Steckbrief auch für `SAP.DE` → SAP SE | mittel (braucht gespeicherte Zuordnung, Export) | nur falls häufig gebraucht – bewusst nicht automatisch (B1) |
| Mehr-Menü mobil aus `NAV` erzeugen statt fest verdrahtet (B5) | Menüänderungen an einer Stelle | klein | mit der nächsten Menüänderung |
