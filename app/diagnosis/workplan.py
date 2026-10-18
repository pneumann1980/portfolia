"""Arbeitsplan der Datenqualität (rein lesend): was ist zu tun – und was nicht.

Die Seiten der Datenqualität zeigen viele Befunde, aber keine Rangfolge. Der Arbeitsplan stuft alles Offene in drei
Stufen ein und nennt je Zeile Empfehlung, Begründung, Wirkung und Aufwand:

* **A · Muss ich tun** – ohne Entscheidung sind Bestände, Kostenbasis oder Steuer falsch bzw. unvollständig: eine
  belegte Lösung liegt bereit, importierte Vorgänge sind noch nicht gebucht, das Ledger meldet einen Fehler.
* **B · Kann ich tun** – verbessert die Sicherheit (Bestand bestätigen, Beleg nachtragen), blockiert aber nichts.
* **C · Nichts zu tun** – automatisch erklärt, ausgebucht, bewusst zurückgestellt oder ein Hinweis ohne Fehler.

Die Einstufung nutzt nur vorhandene Größen (Entscheidung und Status der Klärungsfälle, Befundstatus und Empfehlung der
Diagnose, Zähler des Prüfstapels, gespeicherte Integritätsprüfung). Sie rechnet nichts neu, ruft nichts ab und
schreibt nichts. Die steuerliche Wirkung einer Lösung steht erst in der Simulation des Falls (sie braucht eine
Berechnung je Fall) – deshalb entscheidet die Einstufung nicht über Beträge, sondern über Beleglage und Zustand.
Fehlende Nachweise gelten als unbekannt, nie als 0: ein Fall ohne Bestandsnachweis ist höchstens Stufe B.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.diagnosis import actions as A
from app.diagnosis import autopilot as AP
from app.diagnosis import clarify as K
from app.diagnosis import integrity as I
from app.diagnosis import triage as TR
from app.diagnosis.engine import report_for
from app.diagnosis.model import KINDS, STATUS, Finding, Report
from app.diagnosis.recommend import recommend

log = logging.getLogger(__name__)

TIERS = {
    "A": ("Muss ich tun", "crit", "Ohne Entscheidung sind Bestände, Kostenbasis oder Steuer falsch/unvollständig."),
    "B": ("Kann ich tun", "warn", "Verbessert die Sicherheit; nichts ist blockiert."),
    "C": ("Nichts zu tun", "", "Automatisch erklärt, zurückgestellt oder nur ein Hinweis ohne Fehler."),
}
WHY_A = ("Stufe A, wenn mindestens eines zutrifft: eine belegte Lösung liegt bereit (Entscheidung „eindeutig“), "
         "importierte Vorgänge sind noch nicht gebucht, eine belegte Diagnose hat eine unbedingte Empfehlung oder die "
         "Integritätsprüfung meldet einen kritischen Befund. Alles ohne Beleg oder mit fehlendem Nachweis ist "
         "höchstens "
         "Stufe B.")

# Empfehlung je Befundart (Gruppenzeile), sofern keine konkrete Empfehlung vorliegt
KIND_ACTION = {
    "duplicate": "Mögliche Doppelbuchungen prüfen; belegte Paare gesammelt ausblenden",
    "transfer": "Transfers zuordnen oder als unabhängig bestätigen",
    "holdings": "Bestand mit dem Kontostand bestätigen oder die Ursache der Differenz klären",
    "loss": "Ungeklärte Abgänge einordnen (Buchungsfehler, Verlust oder echter Verkauf)",
    "price": "Kursquelle zuordnen oder Ersatzkurs hinterlegen",
    "asset": "Asset-Zuordnung prüfen",
    "estimated": "Geschätzte Buchungen durch Belege ersetzen oder bewusst belassen",
    "history": "Fehlende Historie nachladen oder als Lücke bestätigen",
    "migration": "Möglichen Token-Wechsel prüfen",
    "reconstruction": "Rekonstruierten Vorgang prüfen und freigeben oder verwerfen",
    "inactive": "Inaktives Konto mit Restbestand einordnen",
    "document": "Beleg mit der Buchung abgleichen",
}
CASE_EFFORT = {
    "bereit": "Simulation prüfen, 1 Klick · atomar und rückgängig zu machen",
    "wartet_bestand": "1 Eingabe: Kontostand mit Zeitpunkt ablesen",
    "wartet_info": "Beleg beschaffen (Explorer, Kontoauszug, Anbieterdaten)",
    "teilweise": "Restdifferenz mit einem Beleg erklären",
    "blockiert": "Folgeprobleme der Simulation prüfen",
    "umfang": "Prüfumfang eingrenzen (Konto/Asset wählen)",
}


@dataclass
class WorkItem:
    key: str
    tier: str  # A | B | C
    kind: str  # fall | gruppe | befund | pruefstapel | hinweise | automatik | zurueck
    title: str
    recommendation: str
    why: str = ""
    effect: str = ""
    effort: str = ""
    n: int = 1
    href: str = ""
    cta: str = "Öffnen"
    weight: Decimal = Decimal(0)
    sub: str = ""  # Befundart (Filter)
    refs: list[str] = field(default_factory=list)  # Befund-IDs

    @property
    def tier_label(self) -> str:
        return TIERS[self.tier][0]


@dataclass
class WorkPlan:
    items: list[WorkItem]
    state: str  # ungeprueft | offen | fertig
    checked_at: str = ""
    integrity: dict[str, Any] = field(default_factory=dict)
    stale: bool = False
    autopilot: dict[str, Any] = field(default_factory=dict)

    def tier(self, t: str) -> list[WorkItem]:
        return [i for i in self.items if i.tier == t]

    @property
    def counts(self) -> dict[str, int]:
        return {t: sum(i.n for i in self.items if i.tier == t) for t in TIERS}

    @property
    def next(self) -> WorkItem | None:
        for t in ("A", "B"):
            xs = self.tier(t)
            if xs:
                return xs[0]
        return None


def _case_item(c: dict[str, Any]) -> WorkItem:
    st = c["state"] or {}
    dec = st.get("decision") or {}
    status = c["status"]
    reason = (dec.get("reason") or "").strip()
    n_tx = 0
    effect = ""
    hyp = next((h for h in st.get("hyps") or [] if h.get("id") == dec.get("hyp")), None)
    if hyp and hyp.get("resolved"):
        effect = f"danach erledigt: {hyp['resolved']} Befund(e)"
    if status == "bereit":
        tier, rec, cta = "A", "Belegte Lösung prüfen und übernehmen", "Lösung prüfen"
    elif status == "zurueck":
        tier, rec, cta = "C", "Bewusst zurückgestellt – gilt, solange keine neue Evidenz vorliegt", "Ansehen"
    elif status == "wartet_bestand":
        tier, rec, cta = "B", "Bestand zu einem Zeitpunkt bestätigen", "Bestand bestätigen"
        effect = effect or f"unterscheidet {len(st.get('cands') or [])} mögliche Lösung(en)"
    elif status == "umfang":
        tier, rec, cta = "B", "Prüfumfang eingrenzen", "Eingrenzen"
    elif status == "blockiert":
        tier, rec, cta = "B", "Folgeprobleme der Simulation prüfen", "Prüfen"
    else:
        tier, rec, cta = "B", "Fehlenden Beleg ergänzen", "Belege ansehen"
        miss = dec.get("missing") or []
        if miss:
            effect = effect or "fehlt: " + "; ".join(str(m) for m in miss[:2])
    return WorkItem(key=f"fall:{c['key']}", tier=tier, kind="zurueck" if tier == "C" else "fall",
                    title=c["title"], recommendation=rec, why=reason, effect=effect,
                    effort=CASE_EFFORT.get(status, ""), n=max(1, n_tx), href=f"/quality/klaerung/{c['key']}",
                    cta=cta, refs=[f["id"] for f in st.get("findings") or []])


def _finding_tier(report: Report, f: Finding) -> tuple[str, str, str]:
    """(Stufe, Empfehlung, Begründung) eines Diagnose-Befunds."""
    if f.status == "hinweis":
        return "C", "Kein Handlungsbedarf – zur Einordnung", STATUS["hinweis"]
    rec = recommend(report, f)
    prim = rec.primary
    why = STATUS.get(f.status, "")
    if f.status == "belegt" and prim is not None and not rec.conditional and not prim.dismiss:
        return "A", prim.label, why
    return "B", (prim.label if prim is not None else KIND_ACTION.get(f.kind, rec.text[:120])), why


def _group_items(report: Report, findings: list[Finding], *, granular: bool) -> list[WorkItem]:
    out: list[WorkItem] = []
    groups: dict[tuple[str, str], list[tuple[Finding, str, str]]] = {}
    for f in findings:
        tier, rec, why = _finding_tier(report, f)
        if granular:
            out.append(WorkItem(key=f"befund:{f.id}", tier=tier, kind="befund", title=f.title, recommendation=rec,
                                why=why, effect=f"betroffener Wert ≈ {f.weight:.2f} EUR" if f.weight > 0 else "",
                                href=f"/quality/diagnose/case/{f.id}", cta="Prüfen", weight=f.weight, sub=f.kind,
                                refs=[f.id]))
        else:
            groups.setdefault((f.kind, tier), []).append((f, rec, why))
    for (kind, tier), rows in groups.items():
        if tier == "C":
            continue  # Hinweise werden gesammelt in einer Zeile geführt
        weight = sum((f.weight for f, _r, _w in rows), Decimal(0))
        _f0, rec, why = rows[0]
        label = KINDS.get(kind, kind)
        out.append(WorkItem(
            key=f"gruppe:{kind}:{tier}", tier=tier, kind="gruppe", title=f"{label} ({len(rows)})",
            recommendation=KIND_ACTION.get(kind, rec),
            why=why if len({s for _f, _r, s in rows}) == 1 else "gemischte Beleglage – die belegten zuerst",
            effect=f"betroffener Wert ≈ {weight:.2f} EUR" if weight > 0 else "",
            effort="je Vorgang eine Entscheidung; Dubletten/Transfers auch gesammelt", n=len(rows),
            href=f"/quality/faelle?kind={kind}&tier={tier}", cta="Ansehen", weight=weight, sub=kind,
            refs=[f.id for f, _r, _w in rows]))
    return out


def build(ctx: Any, report: Report | None = None, *, granular: bool = False) -> WorkPlan:
    """Arbeitsplan aus dem gespeicherten Stand der Klärung, dem (zwischengespeicherten) Diagnosebericht, dem
    Prüfstapel und der gespeicherten Integritätsprüfung. ``granular``: jeder Befund einzeln (Fallliste)."""
    report = report or report_for(ctx)
    items: list[WorkItem] = []
    cases = K.stored_cases(ctx.db)
    open_cases = [c for c in cases if c["status"] in K.OPEN_STATUSES or c["status"] == "zurueck"]
    covered = {f["id"] for c in open_cases for f in ((c["state"] or {}).get("findings") or [])}
    items += [_case_item(c) for c in open_cases]
    tasks, hints = TR.split([f for f in TR.open_findings(ctx, report) if f.id not in covered])
    items += _group_items(report, tasks, granular=granular)
    if hints:
        items.append(WorkItem(key="hinweise", tier="C", kind="hinweise", title=f"{len(hints)} Hinweise ohne Fehler",
                              recommendation="Gesammelt als geprüft markieren (rückgängig zu machen)",
                              why=STATUS["hinweis"], n=len(hints), href="/quality/diagnose/hints", cta="Ansehen",
                              refs=[f.id for f in hints]))
    n_aus = sum(1 for h in report.holdings if h.status == "extern_ausgebucht")
    if n_aus:
        items.append(WorkItem(key="ausgebucht", tier="C", kind="automatik",
                              title=f"{n_aus} Bestandsabweichung(en) automatisch erklärt",
                              recommendation="Nichts zu tun – die Quelle zeigt die ausgebuchte Menge weiter", n=n_aus,
                              href="/quality/diagnose#bestand", cta="Ansehen"))

    try:
        from app.csvimport import review as RV

        s = RV.active_summary(ctx.db)
        if s.open:
            items.append(WorkItem(key="pruefstapel", tier="A", kind="pruefstapel",
                                  title=f"{s.open} importierte Vorgänge im Prüfstapel",
                                  recommendation="Prüfen und freigeben – sichere Vorschläge gesammelt, Rest einzeln",
                                  why="Solange sie offen sind, fehlen sie in Beständen, Kostenbasis und Steuer.",
                                  effect=f"davon manuell zu entscheiden: {s.to_review}" if s.to_review else "",
                                  effort="Sammelfreigabe mit Vorschau; rückgängig über den Stapelverlauf", n=s.open,
                                  href="/journal/pruefung", cta="Prüfstapel öffnen"))
    except Exception as e:  # Prüfstapel nicht verfügbar (sehr alte Datenbank)
        log.debug("Prüfstapel nicht lesbar: %s", e)

    run = I.load(ctx.db)
    integ: dict[str, Any] = {}
    stale = False
    if run is not None:
        run = I.refresh_status(ctx, run)
        c = run.counts()
        integ = {"finished_at": run.finished_at, "open": c["open"], "critical": c["severity"].get("kritisch", 0),
                 "ok": run.ok}
        stale = bool(run.stale) or (run.version and run.version != A.data_version(ctx.db))
        if c["severity"].get("kritisch", 0):
            items.append(WorkItem(key="integritaet", tier="A", kind="gruppe",
                                  title=f"{c['severity']['kritisch']} kritische Befunde der Integritätsprüfung",
                                  recommendation="Prüfbericht öffnen und die bevorzugte Lösung je Befund prüfen",
                                  why="kritisch = nachgewiesener Rechenfehler oder Abweichung mit Wirkung",
                                  n=c["severity"]["kritisch"], href="/quality/integrity?severity=kritisch",
                                  cta="Prüfbericht"))
    ap = (AP.last_runs(ctx.db, 1) or [None])[0]
    checked = max([x for x in (integ.get("finished_at") or "", *(c["updated_at"] or "" for c in cases)) if x],
                  default="")
    order = {"A": 0, "B": 1, "C": 2}
    # Stufe A: konkret ausführbare Punkte (Klärungsfall mit Lösung, Prüfstapel) vor Sammelmeldungen
    items.sort(key=lambda i: (order[i.tier], 0 if i.tier == "A" and i.kind in ("fall", "pruefstapel") else 1,
                              -float(i.weight), -i.n, i.title))
    state = "ungeprueft" if run is None and not cases else ("offen" if any(i.tier != "C" for i in items) else "fertig")
    return WorkPlan(items=items, state=state, checked_at=checked, integrity=integ, stale=stale, autopilot=ap or {})


def dumps(plan: WorkPlan) -> str:  # für Tests/Protokoll
    return json.dumps({"state": plan.state, "counts": plan.counts, "items": [i.key for i in plan.items]})
