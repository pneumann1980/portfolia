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
import re
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
    kind: str  # frage | zeile | stapel | fall | vorrang | erneut | rechnung | seite (Wahl „ansehen“)
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
    ref: str = ""  # Bezug der Antwort: Befund-ID, „<Stapel>:<Vorgang>“, Fallschlüssel, „sicher“
    payload: dict[str, str] = field(default_factory=dict)  # versteckte Felder der Antwort (Prüfsummen, Token)
    facts: list[str] = field(default_factory=list)  # Wirkung bzw. Inhalt als Stichpunkte (unter der Frage)

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
        if it.kind == "pruefstapel":  # neue Vorgänge: sichere gesammelt, übrige einzeln als Frage
            out += _stack_tasks(ctx)
            continue
        if it.key == "erneut":  # selbst vorgemerkte Buchungen: je Buchung eine Frage
            out += _recheck_tasks(ctx)
            continue
        if it.kind == "fall" and it.cta == "Lösung prüfen":  # Klärungsfall mit vorbereiteter Lösung → Frage
            out.append(Task(key=it.key, theme=_theme_of(it, report), kind="fall", heading="Zusammenhängende Buchungen",
                            ref=it.key.split(":", 1)[1], weight=it.weight, item=it, href=it.href))
            continue
        theme = _theme_of(it, report)
        if it.kind == "befund" and it.refs:
            if it.refs[0] in later:
                continue  # „Weiß ich nicht“: kommt nach DEFER_DAYS wieder
            out.append(Task(key=it.key, theme=theme, kind="frage", heading=it.title, finding_id=it.refs[0],
                            weight=it.weight, item=it, ref=it.refs[0]))
        else:
            out.append(_page_task(it, theme))
    out += _takeover_tasks(ctx, report)
    out.sort(key=lambda t: (THEME_ORDER.get(t.theme, 99), t.kind != "stapel", t.kind != "vorrang",
                            -float(t.weight), t.key))
    return out


# ----------------------------------------------------------------------------------------------------
# Quellenvorrang je Konto als eine Frage
# ----------------------------------------------------------------------------------------------------

DECLINED_KEY = "guide.takeover_declined"


def _takeover_tasks(ctx: Any, report: Any) -> list[Task]:
    """„Soll Bitpanda für dein Konto maßgeblich sein?“ – nur für Konten mit Abweichung bzw. mehreren Doppelungen, deren
    Quelle die Übernahme unterstützt, die noch nicht übernommen und nicht abgelehnt ist."""
    try:
        from app.datasources import takeover as TK
        from app.datasources.service import datasource_service
    except ImportError:  # pragma: no cover - Modul optional
        return []
    declined = set(ctx.db.get_state(DECLINED_KEY) or [])
    holdings = {f.data.get("account") for f in report.findings
                if f.kind == "holdings" and (f.data or {}).get("status") == "extern_diff"}
    dups: dict[str, int] = {}
    for f in report.findings:
        if f.kind == "duplicate" and f.parent is None:
            for acc in f.accounts:
                dups[acc] = dups.get(acc, 0) + 1
    out = []
    for ds in datasource_service(ctx).list():
        sid = int(ds.id)
        if sid in declined or not TK.supported(ds.provider):
            continue
        if ds.account not in holdings and dups.get(ds.account, 0) < 2:
            continue
        last = TK.latest(ctx.db, sid)
        if last is not None and last["status"] == "applied":
            continue
        mode = TK.mode(ds.provider)
        out.append(Task(
            key=f"vorrang:{sid}", theme="doppelt", kind="vorrang", heading=f"{ds.account} · {ds.name}",
            ref=str(sid),
            question=f"Soll {ds.provider_label} für dein Konto „{ds.account}“ maßgeblich sein?",
            context=("Für dieses Konto gibt es Abweichungen bzw. doppelte Buchungen zwischen dem kuratierten Import "
                     f"und dem Abruf von {ds.provider_label}. Mit „Ja“ ersetzt Portfolia die Buchungen des Imports "
                     "ab dem ersten Ereignis der Quelle durch die der Quelle – eine Entscheidung statt vieler "
                     "Einzelfragen. "
                     + ("Vorher wird alles abgerufen und am Bestand bewiesen."
                        if mode == "proof" else "Vorher wird alles abgerufen; du bestätigst, dass die Historie "
                                                "vollständig ist, und nichts darf schlechter werden.")),
            href=f"/settings/datasources/{sid}/uebernahme", weight=Decimal(10_000),
            answers=[Answer("vr:yes", "Ja – prüfen und übernehmen",
                            "Öffnet die Übernahme mit Vorschau (Abruf dauert einige Minuten); rückgängig als Ganzes.",
                            recommended=True),
                     Answer("vr:no", "Nein, der Import ist für dieses Konto richtig",
                            NO_CHANGE + " Portfolia schlägt das für dieses Konto nicht wieder vor."),
                     Answer("skip", "Weiß ich nicht", NO_CHANGE)]))
    return out


def decline_takeover(ctx: Any, sid: int) -> None:
    cur = set(ctx.db.get_state(DECLINED_KEY) or [])
    cur.add(int(sid))
    ctx.db.set_state(DECLINED_KEY, sorted(cur))


# ----------------------------------------------------------------------------------------------------
# „Für alle gleich beantworten“
# ----------------------------------------------------------------------------------------------------

BATCH_LIMIT = 50


def signature(f: Any) -> str:
    """Art des Befunds für „gleiche Fragen“: Befundart und Muster (z. B. ``duplicate:same_qty``)."""
    return f"{f.kind}:{(f.data or {}).get('type', '')}"


def similar(ctx: Any, sig: str, aid: str, limit: int = BATCH_LIMIT) -> list[str]:
    """Offene Befund-Fragen mit gleichem Muster, auf die dieselbe Antwort passt. Korrekturen nur, wenn sie dort die
    unbedingte Empfehlung ohne Eingaben sind; Entscheidungen ohne Datenänderung passen immer."""
    from app.diagnosis.engine import report_for
    from app.diagnosis.recommend import recommend

    report = report_for(ctx)
    out: list[str] = []
    key = aid.split(":", 1)[1] if aid.startswith("opt:") else ""
    for t in tasks(ctx):
        if t.kind != "frage":
            continue
        f = report.by_id(t.finding_id)
        if f is None or signature(f) != sig:
            continue
        if key:
            rec = recommend(report, f)
            prim = rec.primary
            if prim is None or prim.key != key or prim.params or rec.conditional:
                continue
        out.append(f.id)
        if len(out) >= limit:
            break
    return out


# ----------------------------------------------------------------------------------------------------
# Prüfstapel: sichere Vorgänge als eine Frage, übrige einzeln
# ----------------------------------------------------------------------------------------------------

LINK_CATS = ("dublette", "ergaenzung", "widerspruch", "komplex")


def _stack_tasks(ctx: Any) -> list[Task]:
    from app.csvimport import queue as Q

    out: list[Task] = []
    safe = Q.build(ctx.db, "sicher", page=0).all_items
    if safe:
        out.append(Task(key="stapel:sicher", theme="import", kind="stapel", heading="Neue Vorgänge", ref="sicher",
                        weight=Decimal(len(safe))))
    for it in Q.build(ctx.db, "offen", page=0).all_items:
        if it.state == "zurueckgestellt":
            continue  # „Weiß ich nicht“ bzw. bewusst zurückgestellt
        key = f"zeile:{it.batch_id}:{it.event}"
        out.append(Task(key=key, theme="import", kind="zeile", heading="Neuer Vorgang",
                        ref=f"{it.batch_id}:{it.event}", item=it, href=f"/journal/csv/{it.batch_id}"))
    return out


def _date_of(iso_ts: str) -> str:
    try:
        return datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).strftime("%d.%m.%Y")
    except ValueError:
        return ""


def _stack_detail(ctx: Any, task: Task) -> Task:
    from app.csvimport import bulk as BU
    from app.csvimport import queue as Q
    from app.csvimport.service import csv_service

    items = Q.build(ctx.db, "sicher", page=0).all_items
    sel = {}
    for it in items:
        sel.setdefault(it.batch_id, set()).update(it.safe_idxs)
    mp = BU.preview(csv_service(ctx), sel)
    links = sum(1 for it in items if not it.books)
    news = len(items) - links
    n = len(items)
    task.question = (f"{n} neue Vorgänge sind eindeutig zugeordnet. Übernehmen?" if n != 1 else
                     "1 neuer Vorgang ist eindeutig zugeordnet. Übernehmen?")
    task.context = ("Sie kommen aus deinen Abrufen und Importen. "
                    + (f"{links} {'ist' if links == 1 else 'sind'} schon gebucht und "
                       f"{'wird' if links == 1 else 'werden'} nur verknüpft (am Bestand ändert sich nichts). "
                       if links else "")
                    + (f"{news} {'ist' if news == 1 else 'sind'} neu und {'wird' if news == 1 else 'werden'} gebucht."
                       if news else ""))
    facts = [f"Bestand {x['asset']} auf {x['account']}: {_q(x['now'])} → {_q(x['after'])}" if x["now"] is not None
             else f"{x['asset']} auf {x['account']}: {'+' if x['delta'] > 0 else ''}{_q(x['delta'])}"
             for x in mp.impact[:4]]
    if len(mp.impact) > 4:
        facts.append(f"… und {len(mp.impact) - 4} weitere Bestände")
    for x in mp.fifo:
        if x.get("retro"):
            facts.append(f"⚠ wirkt rückwirkend auf Steuerjahr(e) {', '.join(str(y) for y in x['retro'])} "
                         f"({x['asset']} auf {x['account']})")
    if any(x["negative"] for x in mp.impact):
        facts.append("⚠ danach wäre ein Bestand negativ – bitte „Einzeln ansehen“")
    if mp.excluded:
        facts.append(f"{mp.excluded} Zeile(n) sind inzwischen nicht mehr sicher und bleiben offen")
    task.facts = facts
    parts = [{"batch_id": int(p.batch["id"]), "token": p.token, "fingerprint": p.fingerprint, "idxs": p.idxs}
             for p in mp.parts]
    import json

    task.payload = {"parts": json.dumps(parts)}
    ok = bool(parts) and not any(x["negative"] for x in mp.impact)
    task.answers = [
        Answer("stk:all", "Ja, alle übernehmen", "Je Stapel in einem Schritt, danach mit einem Klick rückgängig.",
               recommended=ok),
        Answer("stk:look", "Einzeln ansehen", "Öffnet die Liste der sicheren Vorgänge; danach geht es hier weiter."),
        Answer("skip", "Später", NO_CHANGE)]
    return task


def _fmt_leg(text: str) -> str:
    """„+50123.45 TOKA“ → „+50.123,45 TOKA“ (Zahlen der Prüfzeile im deutschen Format)."""
    m = re.match(r"^(Gebühr |[+−-]?)([0-9.]+)(\s.*)$", text)
    return f"{m.group(1)}{_q(m.group(2))}{m.group(3)}" if m else text


def _tx_text(ctx: Any, tx_id: str) -> str:
    """Vorhandene Buchung in Worten: „Kauf am 12.03.2025: −250 EUR, +0,0055 BTC (Import)“."""
    from app.journal.forms import TYPE_LABEL

    pf = ctx.recorded_portfolio()
    t = next((x for x in pf.txs if x.tx_id == tx_id), None) if pf is not None else None
    if t is None:
        return tx_id
    legs = [f"−{_q(t.from_qty)} {t.from_asset}" if t.from_qty and t.from_asset else "",
            f"+{_q(t.to_qty)} {t.to_asset}" if t.to_qty and t.to_asset else ""]
    src = {"import": "Import", "journal": "in der App"}.get(t.origin, "")
    return (f"{TYPE_LABEL.get(t.type, t.type)} am {_d(t)}: " + ", ".join(x for x in legs if x)
            + (f" ({src})" if src else ""))


_REASONS = [(re.compile(r"\b(?:from|to|fee)_qty fehlt"), "Die Menge fehlt."),
            (re.compile(r"Asset für „([^“]+)“ zuordnen"), "Portfolia kennt das Asset „{0}“ noch nicht."),
            (re.compile(r"\b(?:from|to)_account fehlt"), "Das Konto fehlt."),
            (re.compile(r"value_eur|EUR-Wert"), "Der Wert in Euro fehlt.")]


def _plain_reason(reason: str) -> str:
    """Grund einer unvollständigen Importzeile in Alltagssprache (Feldnamen wie ``to_qty`` nur unter Details)."""
    for rx, text in _REASONS:
        m = rx.search(reason)
        if m:
            return text.format(*m.groups())
    return "Eine Angabe fehlt oder ist ungültig." if "_" in reason else _first_sentence(reason)


def _row_detail(ctx: Any, task: Task) -> Task:
    from app.csvimport import model as M
    from app.csvimport import queue as Q
    from app.csvimport.service import csv_service

    it = task.item
    Q._details(ctx.db, [it])
    it.legs = [_fmt_leg(x) for x in it.legs]
    when = _date_of(it.ts)
    what = (f"{M.KIND_LABEL.get(it.label, it.label) or 'Vorgang'}" + (f" am {when}" if when else "")
            + (f" auf {it.account}" if it.account else ""))
    task.heading = f"Neuer Vorgang · {it.account or 'Konto'}" + (f" · {when}" if when else "")
    task.facts = list(it.legs)
    rows = [rc for rc in csv_service(ctx).rows(it.batch_id) if rc.idx in set(it.idxs)]
    target = next((str(t) for rc in rows for t in [(rc.match or {}).get("target"), *rc.dup_of] if t), "")
    target = _tx_text(ctx, target) if target else ""
    link_like = (it.cat or "") in LINK_CATS or it.status == "duplicate"
    if it.status in ("invalid", "unclear"):  # fehlende Angabe bzw. Deutung: ergänzen geht nur auf der Importseite
        missing = it.status == "invalid"
        why = next((str(m) for rc in rows for m in ((rc.errors if missing else rc.warnings) or [])), it.reason)
        task.question = ("Bei diesem Vorgang fehlt eine Angabe. Gehört er in dein Depot?" if missing else
                         "Portfolia versteht diesen Vorgang nicht sicher. Gehört er in dein Depot?")
        task.context = (f"{what}. " + (_plain_reason(why) + " " if why else "")
                        + ("Zum Buchen braucht Portfolia z. B. das Asset, das Konto oder den Wert in Euro."
                           if missing else "Zum Buchen musst du ihn einmal selbst einordnen."))
        task.answers = [
            Answer("go:open", "Ja – Angabe selbst ergänzen" if missing else "Ja – selbst einordnen",
                   "Öffnet den Import mit diesem Vorgang (für Fortgeschrittene). Danach geht es hier weiter."),
            Answer("row:skip", "Nein, gehört nicht in mein Depot",
                   "Wird nicht gebucht; Portfolia fragt bei diesem Vorgang nicht wieder."),
            Answer("row:defer", "Weiß ich nicht", NO_CHANGE + " Der Vorgang wird zurückgestellt.")]
        return task
    if link_like:
        task.question = "Ist dieser Vorgang schon gebucht?"
        task.context = (f"{what}. Portfolia hat eine ähnliche vorhandene Buchung gefunden"
                        + (f": {target}" if target else "") + ". Ist es derselbe Vorgang, wird er nur verknüpft – "
                        "dann zählt er nicht doppelt.")
        task.answers = [
            Answer("row:link", "Ja, ist schon gebucht – nur verknüpfen", "Bestand ändert sich nicht; die Angaben der "
                   "Quelle hängen an der vorhandenen Buchung.", recommended=it.action == "link"),
            Answer("row:include", "Nein, das ist ein eigener Vorgang – zusätzlich buchen",
                   "Wird gebucht: " + (", ".join(it.legs) or what) + ".", recommended=it.action == "include")]
    else:
        task.question = "Soll dieser Vorgang gebucht werden?"
        task.context = f"{what}. Ein Gegenstück in deinen Buchungen gibt es nicht."
        task.answers = [Answer("row:include", "Ja, buchen", "Wird gebucht: " + (", ".join(it.legs) or what) + ".",
                               recommended=True)]
    task.answers += [
        Answer("row:skip", "Nicht buchen (gehört nicht in mein Depot)",
               "Wird nicht gebucht; Portfolia fragt bei diesem Vorgang nicht wieder."),
        Answer("row:defer", "Weiß ich nicht", NO_CHANGE + " Der Vorgang wird zurückgestellt.")]
    return task


# ----------------------------------------------------------------------------------------------------
# Klärungsfall mit vorbereiteter Lösung
# ----------------------------------------------------------------------------------------------------

def _case_detail(ctx: Any, task: Task) -> Task:
    import json

    from app.diagnosis import clarify as K

    case = next(iter(K.analyse(ctx, only=task.ref)), None)
    dec = case.decision if case is not None else None
    hyp = dec.hyp if dec is not None else None
    if case is None or hyp is None or dec is None or dec.kind != "apply":
        return _page_task(task.item, task.theme) if task.item is not None else task
    from app.diagnosis.engine import report_for
    from app.diagnosis.recommend import recommend

    report = report_for(ctx)
    f0 = case.findings[0] if case.findings else None
    yes, no, tip = "Ja, so übernehmen", "Nein, das passt nicht", ""
    task.heading = "Zusammenhängende Buchungen · " + ", ".join(case.accounts[:2])
    if f0 is not None:  # die Frage des wichtigsten Befunds – die Lösung des Falls beantwortet sie für alle
        heading, q, context, tip, texts = question_for(f0, recommend(report, f0), report)
        task.heading, task.question, task.context = heading, q, context
        yes, no = texts.get("yes") or yes, texts.get("no") or no
    else:
        task.question = "Passt diese Erklärung?"
        task.context = f"{case.title}. {dec.reason}".rstrip(". ") + "."
    task.tip = tip
    facts = [f"Bestand {asset} auf {acc} danach: {_q(q)}" for (acc, asset), q in list(hyp.balances.items())[:3]]
    if len(case.findings) > 1:
        facts.append(f"Die Antwort klärt {len(case.findings)} zusammenhängende Fragen auf einmal")
    if hyp.resolved:
        n = len(hyp.resolved)
        facts.append(f"danach erledigt: {n} Frage{'n' if n != 1 else ''}")
    task.facts = facts
    task.payload = {"hyp": hyp.id, "version": case.version,
                    "tokens": json.dumps({c.key: c.plan.token for c in hyp.cands})}
    task.answers = [
        Answer("case:apply", yes, "Portfolia korrigiert alles Zusammengehörige in einem Schritt; rückgängig mit "
               "einem Klick.", recommended=True),
        Answer("case:defer", no, NO_CHANGE + " Portfolia fragt erst wieder, wenn neue Belege vorliegen."),
        Answer("skip", "Weiß ich nicht", NO_CHANGE + (f" Tipp: {tip}" if tip else ""))]
    return task


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


INTEGRITY_ACK_KEY = "guide.integrity_ack"  # Befund-ID → Prüfsumme („zur Kenntnis genommen“)


def _integrity_fp(it: Any) -> str:
    raw = f"{it.title}|{it.cause}|{it.impact_eur}|{','.join(sorted(it.tx_ids))}"
    return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


def _integrity_tasks(ctx: Any, report: Any) -> list[Task]:
    from app.diagnosis import integrity as I

    run = I.load(ctx.db)
    if run is None:
        return []
    known = {f.id for f in report.findings}
    acked = ctx.db.get_state(INTEGRITY_ACK_KEY) or {}
    out = []
    for it in run.items:
        if it.severity != "kritisch" or it.status != "offen" or (it.finding_id and it.finding_id in known):
            continue
        if acked.get(it.id) == _integrity_fp(it):
            continue  # zur Kenntnis genommen und unverändert
        calc = it.cause_kind == "rechenfehler"
        where = " · ".join(x for x in (", ".join(it.accounts[:2]), ", ".join(it.assets[:3])) if x)
        out.append(Task(
            key=f"integritaet:{it.id}", theme="pruefung", kind="rechnung", ref=it.id,
            heading="Nachrechnen" + (f" · {where}" if where else ""),
            question="Beim Nachrechnen passt etwas nicht zusammen. Nimmst du das zur Kenntnis?",
            context=(f"{it.title}. " + _first_sentence(it.cause or it.recommendation or "", 300)
                     + (" Das liegt meist nicht an deinen Buchungen, sondern an der Berechnung." if calc else "")),
            facts=[it.impact] if it.impact else [],
            href=f"/quality/integrity?severity=kritisch#{it.id}", weight=Decimal(str(it.impact_eur or 0)),
            answers=[
                Answer("int:ack", "Zur Kenntnis genommen", NO_CHANGE + " Portfolia fragt nicht wieder, solange es "
                       "gleich bleibt; im Prüfbericht bleibt es sichtbar.", recommended=calc),
                Answer("skip", "Weiß ich nicht – später", NO_CHANGE),
                Answer("go:open", "Prüfbericht mit Lösungsvorschlag ansehen", "Öffnet den Prüfbericht (für "
                       "Fortgeschrittene). Danach geht es hier weiter.", advanced=True)]))
    return out


def ack_integrity(ctx: Any, iid: str) -> bool:
    from app.diagnosis import integrity as I

    run = I.load(ctx.db)
    it = next((x for x in (run.items if run is not None else []) if x.id == iid), None)
    if it is None:
        return False
    cur = dict(ctx.db.get_state(INTEGRITY_ACK_KEY) or {})
    cur[iid] = _integrity_fp(it)
    ctx.db.set_state(INTEGRITY_ACK_KEY, cur)
    return True


def unack_integrity(ctx: Any, iid: str) -> bool:
    cur = dict(ctx.db.get_state(INTEGRITY_ACK_KEY) or {})
    if cur.pop(iid, None) is None:
        return False
    ctx.db.set_state(INTEGRITY_ACK_KEY, cur)
    return True


# ----------------------------------------------------------------------------------------------------
# Selbst vorgemerkte Buchungen („erneut prüfen“)
# ----------------------------------------------------------------------------------------------------

RECHECK_KEPT_KEY = "guide.recheck_kept"  # Buchung → entfernte Vormerkung (für Rückgängig)


def _recheck_tasks(ctx: Any) -> list[Task]:
    from app.diagnosis import recheck as RC

    out = []
    marks = RC.marks(ctx.db)
    for tx_id, mk in sorted(marks.items(), key=lambda kv: str(kv[1].get("at") or "")):
        i = RC.info(ctx, tx_id, marks)
        answers = [Answer("re:keep", "Ja, sie stimmt – Vormerkung entfernen", NO_CHANGE + " Die Buchung bleibt.")]
        if i is not None and i["can_reopen"]:
            answers.append(Answer("re:reopen", "Nein – Buchung zurücknehmen und neu entscheiden",
                                  "Die Buchung zählt vorerst nicht; der Vorgang erscheint wieder bei den neuen "
                                  "Vorgängen und Portfolia fragt dich dort neu."))
        answers.append(Answer("skip", "Weiß ich nicht – später", NO_CHANGE))
        out.append(Task(
            key=f"erneut:{tx_id}", theme="doppelt", kind="erneut", ref=tx_id, heading="Von dir vorgemerkt",
            question="Du hast diese Buchung zum erneuten Prüfen vorgemerkt. Stimmt sie?",
            context=(f"Grund: {mk.get('reason')}. " if mk.get("reason") else "")
            + (f"Herkunft: {i['file'] or 'Abruf'}." if i is not None else ""),
            facts=[_tx_text(ctx, tx_id)], answers=answers, weight=Decimal(1)))
    return out


def recheck_keep(ctx: Any, tx_id: str) -> bool:
    from app.diagnosis import recheck as RC

    mk = RC.marks(ctx.db).get(tx_id)
    if mk is None or RC.unmark(ctx, tx_id)["errors"]:
        return False
    kept = dict(ctx.db.get_state(RECHECK_KEPT_KEY) or {})
    kept[tx_id] = mk
    ctx.db.set_state(RECHECK_KEPT_KEY, dict(list(kept.items())[-100:]))
    return True


def recheck_restore(ctx: Any, tx_id: str) -> bool:
    """„Ja, sie stimmt“ zurücknehmen: die Vormerkung wieder setzen (so, wie sie war)."""
    from app.diagnosis import recheck as RC

    kept = dict(ctx.db.get_state(RECHECK_KEPT_KEY) or {})
    mk = kept.pop(tx_id, None)
    if mk is None:
        return False
    m = RC.marks(ctx.db)
    m[tx_id] = mk
    ctx.db.set_state(RC.MARKS_KEY, m)
    ctx.db.set_state(RECHECK_KEPT_KEY, kept)
    return True


def _page_task(it: Any, theme: str) -> Task:
    """Aufgaben, die nur auf einer eigenen Seite zu lösen sind (z. B. ein Kontostand als Beleg): erklärt, als
    ausdrückliche Wahl „ansehen“ – nach der Aktion dort geht es hier weiter. Nie automatisch weitergeleitet."""
    if it.kind == "pruefstapel":
        q, ctxt, cta = (f"{it.n} Vorgänge aus deinen Abrufen und Importen sind noch nicht gebucht. Jetzt ansehen?",
                        "Solange sie offen sind, fehlen sie in Beständen und Steuer – darum zuerst.",
                        "Ja – Vorgänge ansehen und übernehmen")
    elif it.kind == "fall":
        q, ctxt, cta = (f"Diese Buchungen gehören vermutlich zusammen: {it.title}. Jetzt klären?",
                        CASE_TEXT.get(it.cta, "Portfolia hat die Belege zusammengestellt."),
                        "Ja – Zusammenhang ansehen")
    else:
        q, ctxt, cta = (f"{it.title} – jetzt ansehen?", it.recommendation + (f" – {it.why}" if it.why else ""),
                        f"Ja – {it.cta or 'ansehen'}")
    return Task(key=it.key, theme=theme, kind="seite", heading=THEME_LABEL.get(theme, theme), question=q,
                context=ctxt, href=it.href, cta=cta, item=it, weight=it.weight,
                answers=[Answer("go:open", cta, "Öffnet die passende Seite. Danach geht es hier weiter.",
                                recommended=True),
                         Answer("skip", "Später", NO_CHANGE)])


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


def _chain_answer_useful(rec: Any, f: Any, report: Any) -> bool:
    from app.diagnosis.recommend import facts_for

    d = f.data or {}
    if not str(d.get("status", "")).startswith("extern") or rec.option("chain_steps") is not None:
        return False
    facts = facts_for(report)
    return facts.chain_capable(d.get("account", ""), d.get("asset", ""))


def detail(ctx: Any, task: Task) -> Task:
    """Frage, Antworten und Wirkung der empfohlenen Antwort einer Aufgabe."""
    if task.kind == "stapel":
        return _stack_detail(ctx, task)
    if task.kind == "zeile":
        return _row_detail(ctx, task)
    if task.kind == "fall":
        return _case_detail(ctx, task)
    if task.kind != "frage":
        if not task.answers:  # jede Aufgabe ist im Assistenten beantwortbar – mindestens „später“
            task.answers = [Answer("skip", "Später", NO_CHANGE)]
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
    if f.kind == "holdings" and _chain_answer_useful(rec, f, report):
        answers.insert(0 if prim is None else 1, Answer(
            "chain:scan", "Auf der Blockchain nachsehen lassen",
            "Portfolia fragt den Bestand bei früheren Blöcken ab (ohne Schlüssel, 1–5 Minuten) und findet den Tag, an "
            "dem er ohne Buchung gesprungen ist. Danach kommt diese Frage mit dem Ergebnis wieder.",
            recommended=prim is None))
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


def overview(ctx: Any) -> tuple[int, list[dict[str, Any]], int]:
    """(Aufgaben, offene Aufgaben je Thema in Bearbeitungsreihenfolge, geschätzte Minuten) – für die Startseite."""
    ts = tasks(ctx)
    themes = [{"key": k, "label": label, "text": text, "n": n} for k, label, text in THEMES
              if (n := sum(1 for t in ts if t.theme == k))]
    return len(ts), themes, (max(1, round(len(ts) * MIN_PER_TASK)) if ts else 0)


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

