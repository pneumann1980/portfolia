"""Geführte Klärung („Buchungen prüfen“): jede offene Aufgabe als **eine Frage in Alltagssprache**.

Der Assistent (``/quality/assistent``) zeigt keine technischen Lösungen mehr („M1 ausblenden“), sondern fragt, was
der Mensch aus eigener Erinnerung oder einem genannten Beleg beantworten kann („Hast du am 12.03.2025 einmal oder
zweimal 0,5 BTC erhalten?“). Jede Antwort ist einer **vorhandenen** Aktion zugeordnet – es gibt hier keine eigene
Buchungslogik:

* Antwort mit Korrektur → :func:`app.diagnosis.actions.build_plan` / :func:`~app.diagnosis.actions.apply`
  (Vorschau-Token, Prüfsumme, Rückgängig wie überall);
* „Nein, ist in Ordnung“ / „Das ist zweimal passiert“ → :func:`~app.diagnosis.actions.mark` (``dismiss``/``reject``);
* „Weiß ich nicht“ → ``defer`` (nichts ändert sich, die Aufgabe kommt wieder).

Aufgaben stammen aus dem Arbeitsplan (:mod:`app.diagnosis.workplan`, Einzelbefunde), sortiert nach **Themen**, die
Ursachen vor Folgen behandeln: erst neue Vorgänge übernehmen, dann Doppelungen, Überweisungen, Bestände, Kurse.
Integritätsbefunde, die schon ein Diagnose-Befund sind, zählen nicht ein zweites Mal. Hinweise ohne Fehler erscheinen
nur auf Wunsch (``hints=True``).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app.diagnosis import workplan as WP
from app.util.timeutil import to_local_date
from app.web.fmt import qty_exact

log = logging.getLogger(__name__)

TITLE = "Buchungen prüfen"
MIN_PER_TASK = 1.5
DEFER_DAYS = 7  # „Weiß ich nicht“: so lange nicht erneut fragen
# (Schlüssel, Bezeichnung, Erklärung) – Reihenfolge = Bearbeitungsreihenfolge
THEMES: list[tuple[str, str, str]] = [
    ("import", "Neue Vorgänge übernehmen", "Abrufe und Importe, die noch nicht gebucht sind – sonst fehlen sie."),
    ("doppelt", "Doppelte Buchungen", "Derselbe Vorgang zweimal erfasst, z. B. im Import und im Abruf der Börse."),
    ("transfer", "Überweisungen zwischen deinen Konten", "Abgang auf einem Konto, Zugang auf einem anderen."),
    ("bestand", "Bestände abgleichen", "Was Börse oder Blockchain zeigen, passt nicht zu deinen Buchungen."),
    ("wert", "Werte und Kurse", "Fehlende Kurse, unklare Tokens, Umbenennungen."),
    ("pruefung", "Rechenprüfung", "Auffälligkeiten der Integritätsprüfung, die kein Einzelfall oben sind."),
]
THEME_LABEL = {k: v for k, v, _t in THEMES}
THEME_ORDER = {k: i for i, (k, _v, _t) in enumerate(THEMES)}
KIND_THEME = {
    "duplicate": "doppelt", "takeover": "doppelt", "document": "doppelt",
    "transfer": "transfer", "roundtrip": "transfer",
    "holdings": "bestand", "loss": "bestand", "inactive": "bestand", "reconstruction": "bestand",
    "history": "bestand", "estimated": "bestand",
    "price": "wert", "asset": "wert", "migration": "wert",
}
NO_CHANGE = "Es wird nichts geändert."
# Antworttext „Ja“, wenn es keine Empfehlung gibt, aber eine Korrektur mit Eingaben (z. B. eigene CoinGecko-ID)
YES_WITHOUT_RECOMMENDATION = {"price": "Ja – ich nenne die Kursquelle (CoinGecko-ID oder Link)"}
# Klärungsfall: was auf der nächsten Seite zu tun ist (nach dem Knopf des Arbeitsplans), in Alltagssprache
CASE_TEXT = {
    "Lösung prüfen": "Portfolia hat eine Lösung vorbereitet und zeigt dir vorher genau, was sich ändert – du "
                     "bestätigst nur.",
    "Bestand bestätigen": "Portfolia braucht einen Kontostand zu einem Zeitpunkt (z. B. aus der App der Börse), um "
                          "zwischen möglichen Erklärungen zu unterscheiden.",
    "Belege ansehen": "Für eine sichere Lösung fehlt noch ein Beleg; auf der nächsten Seite steht, welcher.",
    "Eingrenzen": "Zu viele Buchungen hängen zusammen – wähle auf der nächsten Seite Konto oder Asset.",
    "Prüfen": "Die vorbereitete Lösung hätte Nebenwirkungen; sieh sie dir auf der nächsten Seite an.",
}


def kid(key: str) -> str:
    """Kurzkennung einer Aufgabe (Cookie/URL)."""
    return hashlib.sha1(key.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]


# ----------------------------------------------------------------------------------------------------
# Modell
# ----------------------------------------------------------------------------------------------------

@dataclass
class Answer:
    id: str  # opt:<Lösung> | mark:<dismiss|reject|defer>
    text: str
    effect: str = ""
    recommended: bool = False
    params: list[Any] = field(default_factory=list)  # nötige Eingaben (recommend.Param)
    caution: str = ""
    token: str = ""  # Vorschau-Token (nur für Antworten ohne Eingaben, vorab berechnet)
    advanced: bool = False  # unter „Andere Möglichkeiten“

    @property
    def option(self) -> str:
        return self.id.split(":", 1)[1] if self.id.startswith("opt:") else ""

    @property
    def mark(self) -> str:
        return self.id.split(":", 1)[1] if self.id.startswith("mark:") else ""


@dataclass
class Task:
    key: str
    theme: str
    kind: str  # frage (Diagnose-Befund) | seite (eigene Seite, Rückkehr hierher)
    heading: str  # kurze Einordnung: Konto · Datum bzw. Thema
    question: str = ""
    context: str = ""
    answers: list[Answer] = field(default_factory=list)
    tip: str = ""  # wo man nachsehen kann („Weiß ich nicht“)
    href: str = ""
    cta: str = ""
    finding_id: str = ""
    weight: Decimal = Decimal(0)
    item: Any = None  # WorkItem
    finding: Any = None  # Diagnose-Befund (Belege unter „Details“)

    @property
    def kid(self) -> str:
        return kid(self.key)

    @property
    def theme_label(self) -> str:
        return THEME_LABEL.get(self.theme, self.theme)


@dataclass
class Step:
    task: Task | None
    position: int
    total: int
    skipped: int
    themes: list[dict[str, Any]]

    @property
    def minutes(self) -> int:
        n = self.total - self.position + 1 if self.task else 0
        return max(1, round(n * MIN_PER_TASK)) if n > 0 else 0


# ----------------------------------------------------------------------------------------------------
# Aufgabenliste (leicht: ohne Vorschau)
# ----------------------------------------------------------------------------------------------------

def _theme_of(item: Any, report: Any) -> str:
    if item.kind == "pruefstapel":
        return "import"
    if item.kind == "befund":
        return KIND_THEME.get(item.sub, "bestand")
    if item.kind == "fall":
        kinds = [f.kind for r in item.refs if (f := report.by_id(r)) is not None]
        return KIND_THEME.get(kinds[0], "bestand") if kinds else "bestand"
    if item.key == "erneut":
        return "doppelt"
    return "pruefung"


def tasks(ctx: Any, *, hints: bool = False) -> list[Task]:
    """Offene Aufgaben in Bearbeitungsreihenfolge (Thema, dann Wirkung). Ohne Vorschau – zum Zählen und Wählen."""
    from app.diagnosis.engine import report_for

    report = report_for(ctx)
    plan = WP.build(ctx, report, granular=True)
    later = _deferred(ctx, report)
    out: list[Task] = []
    tiers = ("A", "B", "C") if hints else ("A", "B")
    for it in plan.items:
        if it.tier not in tiers or it.kind in ("zurueck", "automatik") or (it.kind == "hinweise" and not hints):
            continue
        if it.key == "integritaet":  # kritische Integritätsbefunde: nur die, die kein Diagnose-Befund sind
            out += _integrity_tasks(ctx, report)
            continue
        theme = _theme_of(it, report)
        if it.kind == "befund" and it.refs:
            if it.refs[0] in later:
                continue  # „Weiß ich nicht“: kommt nach DEFER_DAYS wieder
            out.append(Task(key=it.key, theme=theme, kind="frage", heading=it.title, finding_id=it.refs[0],
                            weight=it.weight, item=it))
        else:
            out.append(_page_task(it, theme))
    out.sort(key=lambda t: (THEME_ORDER.get(t.theme, 99), -float(t.weight), t.key))
    return out


def _deferred(ctx: Any, report: Any) -> set[str]:
    """Befunde, auf die kürzlich mit „Weiß ich nicht“ geantwortet wurde (``defer`` jünger als :data:`DEFER_DAYS`) und
    deren Daten seitdem gleich sind (Prüfsumme) – ändern sich die Buchungen, wird sofort wieder gefragt."""
    from app.diagnosis import actions as A
    from app.util.timeutil import parse_iso

    limit = datetime.now(UTC) - timedelta(days=DEFER_DAYS)
    out = set()
    for fid, d in A.active_marks(ctx.db).items():
        at = parse_iso(d.created_at)
        f = report.by_id(fid)
        if d.action == "defer" and at is not None and at >= limit and f is not None \
                and d.fingerprint == A.fingerprint(f):
            out.add(fid)
    return out


def _integrity_tasks(ctx: Any, report: Any) -> list[Task]:
    from app.diagnosis import integrity as I

    run = I.load(ctx.db)
    if run is None:
        return []
    known = {f.id for f in report.findings}
    out = []
    for it in run.items:
        if it.severity != "kritisch" or it.status != "offen" or (it.finding_id and it.finding_id in known):
            continue
        out.append(Task(
            key=f"integritaet:{it.id}", theme="pruefung", kind="seite", heading="Rechenprüfung",
            question=f"Portfolia hat beim Nachrechnen etwas gefunden: {it.title}",
            context=(it.cause or it.recommendation or "")[:400]
            + (" Das ist meist kein Fehler deiner Daten, sondern eine Unstimmigkeit in der Berechnung – im Bericht "
               "steht die vorgeschlagene Lösung." if it.cause_kind == "rechenfehler" else ""),
            href=f"/quality/integrity?severity=kritisch#{it.id}", cta="Bericht ansehen",
            weight=Decimal(str(it.impact_eur or 0))))
    return out


def _page_task(it: Any, theme: str) -> Task:
    """Aufgaben mit eigener Seite (bis zu ihrer Frage-Fassung): klar erklärt, Rückkehr hierher nach der Aktion."""
    if it.kind == "pruefstapel":
        return Task(key=it.key, theme=theme, kind="seite", heading="Neue Vorgänge",
                    question=f"{it.n} Vorgänge aus deinen Abrufen und Importen sind noch nicht gebucht. Übernehmen?",
                    context="Solange sie offen sind, fehlen sie in Beständen, Kostenbasis und Steuer – darum zuerst. "
                            "Auf der nächsten Seite übernimmst du die eindeutigen gesammelt („Sichere übernehmen“), "
                            "unklare entscheidest du einzeln. Danach geht es hier weiter.",
                    href=it.href, cta="Vorgänge ansehen und übernehmen", item=it, weight=it.weight)
    if it.kind == "fall":
        return Task(key=it.key, theme=theme, kind="seite", heading="Zusammenhängende Buchungen",
                    question=f"Diese Buchungen gehören vermutlich zusammen: {it.title}",
                    context=(CASE_TEXT.get(it.cta, "Portfolia hat die Belege zusammengestellt.")
                             + " Danach geht es hier weiter."),
                    href=it.href, cta="Zusammenhang ansehen", item=it, weight=it.weight)
    return Task(key=it.key, theme=theme, kind="seite", heading=it.title, question=it.title,
                context=it.recommendation + (f" – {it.why}" if it.why else ""), href=it.href, cta=it.cta or "Öffnen",
                item=it, weight=it.weight)


# ----------------------------------------------------------------------------------------------------
# Frage zu einem Diagnose-Befund (mit Vorschau der empfohlenen Antwort)
# ----------------------------------------------------------------------------------------------------

def _d(t: Any) -> str:
    try:
        return to_local_date(t.ts).strftime("%d.%m.%Y")
    except (AttributeError, ValueError, TypeError):
        return "?"


def _q(v: Any) -> str:
    try:
        return qty_exact(Decimal(str(v)))
    except (InvalidOperation, ValueError):
        return str(v)


def _leg(t: Any) -> str:
    """„+0,5 BTC auf Bitpanda“ bzw. „−0,5 BTC von Bitpanda“ (erste Seite der Buchung)."""
    if t.inn:
        return f"+{_q(t.inn[2])} {t.inn[1]}" + (f" auf {t.inn[0]}" if t.inn[0] else "")
    if t.out:
        return f"−{_q(t.out[2])} {t.out[1]}" + (f" von {t.out[0]}" if t.out[0] else "")
    return t.type


def _desc(t: Any) -> str:
    src = {"import": "Import", "journal": "in der App"}.get(getattr(t, "origin", ""), "")
    return f"{_leg(t)} am {_d(t)}" + (f" ({src})" if src else "")


def _first_sentence(text: str, limit: int = 260) -> str:
    text = (text or "").strip()
    cut = text.find(". ")
    out = text[:cut + 1] if 0 < cut < limit else text[:limit]
    return out + ("…" if len(out) < len(text) and not out.endswith(".") else "")


def question_for(f: Any, rec: Any, report: Any) -> tuple[str, str, str, str, dict[str, str]]:
    """(Überschrift, Frage, Erklärung, Tipp, Antworttexte) je Befundart. Antworttexte: yes / no / no_mark."""
    d = f.data or {}
    acc = (f.accounts or [""])[0]
    heading = " · ".join(x for x in (acc, ", ".join(f.assets[:2])) if x) or f.kind_label
    texts = {"yes": "", "no": "Nein, das ist in Ordnung so", "no_mark": "dismiss"}
    tip = ""
    pair = f.pairs[0] if f.pairs else None
    if f.kind == "duplicate" and pair is not None:
        a, b, _why = pair
        heading = f"{acc or 'Konto'} · {_d(a)}"
        q = "Ist das derselbe Vorgang – also nur einmal passiert?"
        ctx = (f"Portfolia sieht zwei sehr ähnliche Buchungen: {_desc(a)} und {_desc(b)}. Sind es in Wahrheit eine, "
               "zählt sie im Moment doppelt.")
        texts.update(yes="Ja, nur einmal – eine Buchung ist doppelt", no="Nein, das ist wirklich zweimal passiert",
                     no_mark="reject")
        tip = f"In der App bzw. auf der Webseite von {acc or 'der Börse'} den Verlauf um den {_d(a)} ansehen."
        return heading, q, ctx, tip, texts
    if f.kind == "transfer" and pair is not None:
        a, b, _why = pair
        w, dep = (a, b) if a.out else (b, a)
        src, dst = (w.out[0] if w.out else "?"), (dep.inn[0] if dep.inn else "?")
        asset, qty = (w.out[1], w.out[2]) if w.out else ("", "")
        heading = f"{src} → {dst} · {_d(w)}"
        q = f"Hast du am {_d(w)} {_q(qty)} {asset} von {src} an {dst} geschickt – also an dich selbst?"
        ctx = (f"Portfolia sieht einen Abgang ({_desc(w)}) und einen passenden Zugang ({_desc(dep)}). Als "
               "Überweisung an dich selbst ist das kein Verkauf und kein Kauf – die Anschaffungsdaten wandern mit.")
        texts.update(yes="Ja, das war eine Überweisung an mich selbst",
                     no="Nein, das sind zwei verschiedene Vorgänge", no_mark="reject")
        tip = "Im Explorer bzw. im Verlauf beider Konten nach dem Datum suchen (Hash steht unter „Details“)."
        return heading, q, ctx, tip, texts
    if f.kind == "holdings" and d.get("account"):
        asset, acc = d.get("asset", ""), d["account"]
        obs, comp = d.get("observed"), d.get("computed")
        row = next((h for h in report.holdings if h.account == acc and h.asset == asset), None)
        src = (row.observed_by if row is not None and row.observed_by else "der Quelle")
        heading = f"{acc} · {asset}"
        if obs is not None:
            q = f"Wie viel {asset} liegt wirklich auf {acc}?"
            ctx = (f"Laut {src} sind es {_q(obs)} {asset}, nach deinen Buchungen {_q(comp)} {asset} "
                   f"(Differenz {_q(Decimal(obs) - Decimal(comp))}). Meist fehlt eine Buchung oder eine ist doppelt.")
            texts.update(yes=f"{src} hat recht ({_q(obs)} {asset})",
                         no=f"Meine Buchungen stimmen ({_q(comp)}) – die Anzeige ist veraltet")
        else:
            q = f"Stimmt der Bestand von {asset} auf {acc}?"
            ctx = _first_sentence(rec.text)
            texts.update(yes="Nein – bitte korrigieren", no="Ja, der Bestand stimmt")
        tip = f"Den aktuellen Bestand in der App bzw. auf der Webseite von {acc} nachsehen."
        return heading, q, ctx, tip, texts
    if f.kind == "loss":
        t = f.txs[0] if f.txs else None
        heading = f"{acc} · {_d(t)}" if t is not None else heading
        q = (f"Wohin sind {_q(t.out[2])} {t.out[1]} von {t.out[0]} am {_d(t)} gegangen?" if t is not None and t.out
             else f"Wohin ist der Abgang gegangen? ({f.title})")
        ctx = ("Für diesen Abgang ist kein Ziel bekannt. Ging er an ein eigenes Konto, fehlt dort der Zugang; war es "
               "eine Zahlung oder ein Verlust, ist alles richtig.")
        texts.update(yes="Bitte so korrigieren wie empfohlen",
                     no="Das ist in Ordnung (z. B. bezahlt, verschenkt, Gebühr)")
        tip = "Im Verlauf des Kontos bzw. im Explorer nachsehen, an welche Adresse er ging."
        return heading, q, ctx, tip, texts
    if f.kind == "price":
        asset = (f.assets or [d.get("asset", "")])[0]
        heading = f"{asset} · Kurs"
        q = f"Hat {asset} heute noch einen Wert?"
        ctx = _first_sentence(f.title + ". " + rec.text, 300)
        texts.update(yes="Ja – Kursquelle wie vorgeschlagen verwenden",
                     no="Nein, der Token ist wertlos (z. B. Spam, eingestellt)")
        tip = f"Auf CoinGecko bzw. der Börse nach „{asset}“ suchen."
        return heading, q, ctx, tip, texts
    if f.kind == "migration":
        old, new = d.get("old", ""), d.get("new", "")
        heading = f"{old} → {new}" if old and new else heading
        q = (f"Wurde {old} in {new} umgetauscht – gleiches Projekt, neuer Name?" if old and new
             else "Ist das ein Umtausch desselben Projekts (neuer Name)?")
        ctx = _first_sentence(rec.text)
        texts.update(yes="Ja, nur umbenannt bzw. umgetauscht", no="Nein, das war ein eigener Kauf/Tausch",
                     no_mark="reject")
        tip = "Mitteilung der Börse bzw. des Projekts zur Umstellung suchen."
        return heading, q, ctx, tip, texts
    q = "Soll Portfolia das so korrigieren?"
    return heading, q, f.title + ". " + _first_sentence(rec.text), "", {
        "yes": "Ja, bitte korrigieren", "no": "Nein, das ist in Ordnung so", "no_mark": "dismiss"}


def _effect(ctx: Any, report: Any, f: Any, opt: Any) -> tuple[str, str]:
    """(Wirkung als Satz, Vorschau-Token) einer Lösung ohne Eingaben – rechnet auf einer Kopie, ändert nichts."""
    from app.diagnosis import actions as A

    try:
        plan = A.build_plan(ctx, report, f, opt.key, {}, given=False)
        if plan.errors or not plan.ops:
            return opt.summary, ""
        eff = A.preview(ctx, report, plan)
    except Exception as e:  # Vorschau darf die Frage nie verhindern
        log.debug("Vorschau im Assistenten nicht möglich: %s", e)
        return opt.summary, ""
    parts = [f"Bestand {p.asset} auf {p.account}: {_q(p.bal[0])} → {_q(p.bal[1])}" for p in eff.positions[:2]]
    parts += [f"Steuerjahr {y.year}: realisiert {y.realized[0]:.2f} € → {y.realized[1]:.2f} €" for y in eff.years[:2]
              if y.realized[0] != y.realized[1]]
    if getattr(eff, "target_resolved", False):
        parts.append("die Aufgabe ist danach erledigt")
    for a, b in getattr(eff, "negative_new", [])[:1]:
        parts.append(f"⚠ danach negativer Bestand {b} auf {a}")
    return ("; ".join(parts) or opt.summary), plan.token


def detail(ctx: Any, task: Task) -> Task:
    """Frage, Antworten und Wirkung der empfohlenen Antwort für eine Befund-Aufgabe."""
    if task.kind != "frage":
        return task
    from app.diagnosis.engine import report_for
    from app.diagnosis.recommend import recommend

    report = report_for(ctx)
    f = report.by_id(task.finding_id)
    if f is None:
        return task
    rec = recommend(report, f)
    heading, q, context, tip, texts = question_for(f, rec, report)
    task.heading, task.question, task.context, task.tip, task.finding = heading, q, context, tip, f
    fixes = [o for o in rec.options if not o.dismiss]
    prim = rec.primary
    answers: list[Answer] = []
    if not fixes and f.kind not in ("duplicate", "transfer", "migration"):  # nichts zu korrigieren: nur einordnen
        task.question = "Ist das für dich in Ordnung?"
        task.context = f"{f.title}. {_first_sentence(rec.text)}"
        texts["no"], texts["no_mark"] = "Ja, das ist in Ordnung so", "dismiss"
    if prim is not None:
        eff, token = _effect(ctx, report, f, prim) if not prim.params else (prim.summary, "")
        answers.append(Answer(f"opt:{prim.key}", texts["yes"] or prim.label, eff, recommended=not rec.conditional,
                              params=list(prim.params), caution=prim.caution, token=token))
    elif fixes:  # ohne Empfehlung: die naheliegende Korrektur als „Ja“ (Wirkung erst nach den Eingaben)
        o = fixes[0]
        yes = YES_WITHOUT_RECOMMENDATION.get(f.kind) or (f"Ja: {o.label}" if texts["yes"].startswith("Ja, bitte")
                                                          else texts["yes"]) or o.label
        answers.append(Answer(f"opt:{o.key}", yes, o.summary, params=list(o.params), caution=o.caution))
    no_mark = texts.get("no_mark", "dismiss")
    answers.append(Answer(f"mark:{no_mark}", texts["no"],
                          NO_CHANGE + (" Portfolia fragt nicht wieder, solange sich diese Buchungen nicht ändern."
                                       if no_mark in ("reject", "dismiss") else ""),
                          ))
    answers.append(Answer("mark:defer", "Weiß ich nicht", NO_CHANGE + f" Die Frage kommt in {DEFER_DAYS} Tagen wieder "
                          "(oder früher, wenn sich die Buchungen ändern)."
                          + (f" Tipp: {tip}" if tip else "")))
    used = {a.option for a in answers if a.option}
    for o in fixes:
        if o.key not in used:
            answers.append(Answer(f"opt:{o.key}", o.label, o.summary, params=list(o.params), caution=o.caution,
                                  advanced=True))
    task.answers = answers
    return task


# ----------------------------------------------------------------------------------------------------
# Schritt
# ----------------------------------------------------------------------------------------------------

def parse_skip(raw: str) -> list[str]:
    return [x for x in (raw or "").split(",") if len(x) == 8 and all(c in "0123456789abcdef" for c in x)][:60]


def step(ctx: Any, skip: list[str], *, hints: bool = False) -> Step:
    ts = tasks(ctx, hints=hints)
    skipped = set(skip)
    todo = [t for t in ts if t.kid not in skipped]
    themes = []
    for key, label, text in THEMES:
        n = sum(1 for t in ts if t.theme == key)
        if n:
            themes.append({"key": key, "label": label, "text": text, "n": n,
                           "open": sum(1 for t in todo if t.theme == key)})
    cur = todo[0] if todo else None
    pos = ts.index(cur) + 1 if cur is not None else len(ts)
    return Step(detail(ctx, cur) if cur is not None else None, pos, len(ts), len(ts) - len(todo), themes)


def summary(ctx: Any) -> tuple[int, int, int]:
    """(Aufgaben, Themen, geschätzte Minuten) – dieselbe Zählung wie im Assistenten."""
    ts = tasks(ctx)
    n = len(ts)
    return n, len({t.theme for t in ts}), (max(1, round(n * MIN_PER_TASK)) if n else 0)


def theme_position(st: Step) -> tuple[int, int, int, int]:
    """(Thema n, von m Themen, Aufgabe k im Thema, von j) für die Kopfzeile."""
    if st.task is None:
        return 0, len(st.themes), 0, 0
    keys = [t["key"] for t in st.themes]
    ti = keys.index(st.task.theme) + 1 if st.task.theme in keys else 0
    th = next((t for t in st.themes if t["key"] == st.task.theme), {"n": 0, "open": 0})
    return ti, len(keys), th["n"] - th["open"] + 1, th["n"]

