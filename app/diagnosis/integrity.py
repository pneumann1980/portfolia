"""Finanzielle Integritätsprüfung: ein Prüflauf über Bestände, Lots/Kostenbasis, Dubletten/Transfers, Kurse und
steuerliche Datenqualität – ausschließlich lesend.

Die Prüfung ist keine zweite Diagnose: Sie führt die Befunde der Diagnose (:mod:`app.diagnosis.engine`, inklusive
Abgleich, Empfehlungen und Korrekturwegen aus :mod:`app.diagnosis.recommend` / :mod:`app.diagnosis.actions`) mit
**Invarianten** zusammen, die die Diagnose nicht prüft, weil sie für ein korrekt rechnendes Ledger immer gelten
müssen (Lots = Bestand, Veräußerung = Summe ihrer Lot-Anteile, Anschaffung vor Veräußerung …). Ein Verstoß dagegen
ist ein *nachgewiesener Rechenfehler*; Befunde aus unvollständigen Quelldaten sind als *Datenlücke* bzw.
*Verdacht* gekennzeichnet.

Alles läuft auf **einem** Schnappschuss (:func:`app.diagnosis.collect.collect`): ein Ledger, eine Kursliste, ein
Datenstand. Ändert sich der Datenstand während des Laufs, wird das Ergebnis als „überholt“ markiert. Gespeichert
wird nur das Ergebnis des Laufs (``app_state``, administrative Metadaten) – nie Buchungen, Bestände oder
Zuordnungen. Korrekturen laufen ausschließlich über die vorhandenen Diagnose-Aktionen (Vorschau, Bestätigung,
Rückgängig).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.diagnosis import actions as A
from app.diagnosis.model import Finding, Report
from app.ledger.engine import DUST, ZERO
from app.util.timeutil import iso, parse_iso

log = logging.getLogger(__name__)

STATE_KEY = "integrity.last"
JOB = "integrity_check"
CENT = Decimal("0.01")
MAX_TAX_YEARS = 12
JUMP_FACTOR = 8.0  # Tageskurs mehr als 8-fach bzw. weniger als 1/8 des Vortags → ungewöhnlicher Kurssprung
MAX_ITEMS_PER_CHECK = 200

CATEGORIES: dict[str, str] = {
    "bestand": "Bestand",
    "fifo": "FIFO & Kostenbasis",
    "dublette": "Dubletten & Transfers",
    "kurs": "Kurse & Bewertung",
    "steuer": "Steuerliche Datenqualität",
    "quelle": "Historie & Datenquellen",
}
SEVERITIES: dict[str, str] = {"kritisch": "crit", "warnung": "warn", "information": "info"}
SEVERITY_ORDER = {k: i for i, k in enumerate(SEVERITIES)}
CAUSES = {"rechenfehler": "nachgewiesener Rechenfehler", "nachgewiesen": "nachgewiesen (aus den Daten belegt)",
          "datenluecke": "Abweichung aus unvollständigen Quelldaten", "verdacht": "Verdacht",
          "hinweis": "Hinweis zur Einordnung"}
CONFIDENCE = ("eindeutig", "hoch plausibel", "prüfbedürftig", "widersprüchlich")
STATUSES = {"offen": "", "geprüft": "good", "korrigiert": "info", "verworfen": ""}
RECON_KINDS = frozenset({"duplicate", "transfer"})  # Befunde des Abgleichs (Konfidenzstufe, Sammelbearbeitung)
INDEPENDENT_NOTE = "Unabhängige Buchung bestätigt"  # „verworfen“: Befund trifft nicht zu (Nutzerentscheidung)

_KIND_CAT = {"duplicate": "dublette", "transfer": "dublette", "asset": "kurs", "holdings": "bestand",
             "history": "bestand", "estimated": "quelle", "price": "kurs", "migration": "bestand",
             "document": "quelle"}
_LOCK = threading.Lock()
# Steuer-Hinweise, die schon als eigener Befund erscheinen (Diagnose: Schätzungen, Ledger-Warnungen, Kurse) –
# nicht doppelt zeigen
_TAX_COVERED = frozenset({"estimated_tx", "ledger", "unpriced"})


@dataclass
class Item:
    """Ein Befund der Integritätsprüfung (Anzeige, Export)."""

    id: str
    category: str
    severity: str
    title: str
    cause_kind: str
    cause: str
    impact: str
    impact_eur: float | None = None
    recommendation: str = ""
    preferred: str = ""  # bevorzugte Lösung (Bezeichnung)
    preferred_key: str = ""  # Schlüssel der Lösung in der Diagnose (Vorschau/Übernahme)
    alternatives: list[str] = field(default_factory=list)
    confidence: str = ""
    assets: list[str] = field(default_factory=list)
    accounts: list[str] = field(default_factory=list)
    tx_ids: list[str] = field(default_factory=list)
    finding_id: str = ""  # Diagnose-Befund (Vorschau/Übernehmen/geprüft) – leer bei Invarianten
    source: str = "diagnose"  # diagnose | invariante | steuer | kurse | historie
    status: str = "offen"


@dataclass
class Run:
    started_at: str
    finished_at: str = ""
    duration_ms: int = 0
    ok: bool = True
    error: str = ""
    stale: bool = False  # Datenstand hat sich während des Laufs geändert
    version: str = ""
    txs: int = 0
    assets: int = 0
    checks: dict[str, int] = field(default_factory=dict)  # geprüfte Objekte je Prüfung
    timings: dict[str, int] = field(default_factory=dict)  # ms je Abschnitt
    items: list[Item] = field(default_factory=list)

    def counts(self) -> dict[str, Any]:
        by_sev: dict[str, int] = defaultdict(int)
        by_cat: dict[str, int] = defaultdict(int)
        for it in self.items:
            if it.status in ("offen",):
                by_sev[it.severity] += 1
                by_cat[it.category] += 1
        return {"open": sum(by_sev.values()), "severity": dict(by_sev), "category": dict(by_cat),
                "total": len(self.items)}


# ----------------------------------------------------------------------------------------------------------------------
# Diagnose-Befunde → Prüfbefunde
# ----------------------------------------------------------------------------------------------------------------------

def _severity(f: Finding) -> str:
    if f.status == "hinweis" or f.priority >= 3:
        return "information"
    if f.priority == 1 and f.status in ("belegt", "wahrscheinlich"):
        return "kritisch"
    return "warnung"


def _cause_kind(f: Finding) -> str:
    if f.status == "hinweis":
        return "hinweis"
    if f.kind in ("history", "estimated") or (f.kind == "holdings" and f.status != "belegt"):
        return "datenluecke"
    return "nachgewiesen" if f.status == "belegt" else "verdacht"


def confidence_of(f: Finding, has_primary: bool, conditional: bool) -> str:
    """Erklärbare Stufe (keine Wahrscheinlichkeit): belegt → eindeutig, mehrere unabhängige Belege → hoch plausibel,
    Muster ohne entscheidenden Beleg bzw. Empfehlung unter Vorbehalt → prüfbedürftig, keine Empfehlung möglich →
    widersprüchlich."""
    if not has_primary:
        return "widersprüchlich" if f.kind in ("duplicate", "transfer") else "prüfbedürftig"
    if conditional or f.status == "verdacht":
        return "prüfbedürftig"
    return "eindeutig" if f.status == "belegt" else "hoch plausibel"


def _value_of(report: Report, f: Finding) -> float | None:
    """Betroffener Wert (Sortierung): Summe der EUR-Werte der betroffenen Buchungen bzw. Wert der Positionen."""
    seen: set[str] = set()
    total = ZERO
    for r in [*f.txs, *(x for a, b, _w in f.pairs for x in (a, b))]:
        if r.tx_id in seen:
            continue
        seen.add(r.tx_id)
        if r.value_eur is not None:
            total += abs(r.value_eur)
    if total:
        return float(total.quantize(CENT))
    snap = report.snapshot
    if snap is None or snap.ledger is None:
        return None
    for acc, aid in f.positions:
        p = snap.prices.get(aid)
        q = snap.ledger.balances.get((acc, aid))
        if p is not None and p.valued and q:
            total += abs(q) * Decimal(str(p.price_eur))
    return float(total.quantize(CENT)) if total else None


def from_findings(report: Report) -> list[Item]:
    from app.diagnosis.recommend import recommend

    out: list[Item] = []
    for f in report.findings:
        rec = recommend(report, f)
        primary = rec.primary
        alts = [o.label for o in rec.options if not o.dismiss and o is not primary][:3]
        impact = (f.scenario.text if f.scenario is not None else
                  (f.suspected[0] if f.suspected else (f.known[0] if f.known else "")))
        cat = _KIND_CAT.get(f.kind, "bestand")
        if f.kind == "history" and "Anschaffung" in f.title:
            cat = "fifo"
        if f.kind == "history" and f.title.startswith("Datenquelle"):
            cat = "quelle"
        out.append(Item(
            id=f.id, category=cat, severity=_severity(f), title=f.title, cause_kind=_cause_kind(f),
            cause=" ".join(f.known[:2])[:600], impact=impact[:400], impact_eur=_value_of(report, f),
            recommendation=rec.text[:600], preferred=primary.label if primary else "",
            preferred_key=primary.key if primary else "", alternatives=alts,
            confidence=confidence_of(f, primary is not None, rec.conditional) if f.kind in RECON_KINDS else "",
            assets=list(f.assets), accounts=list(f.accounts),
            tx_ids=list(dict.fromkeys([r.tx_id for r in f.txs] + [x.tx_id for a, b, _w in f.pairs for x in (a, b)])),
            finding_id=f.id, source="diagnose"))
    return out


# ----------------------------------------------------------------------------------------------------------------------
# Invarianten (nachgewiesene Rechenfehler, falls verletzt)
# ----------------------------------------------------------------------------------------------------------------------

def _iid(*parts: Any) -> str:
    return "i-" + hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12]


def check_ledger(report: Report, checks: dict[str, int]) -> list[Item]:
    snap = report.snapshot
    led, pf = snap.ledger, snap.pf
    out: list[Item] = []
    if led is None or pf is None:
        return out
    fiat = {aid for aid, a in pf.assets.items() if a.is_fiat}
    negative = {aid for (acc, aid), q in led.balances.items() if q < -DUST}
    gap_assets = {i.asset for i in led.issues if i.code == "attribution_gap"}
    # 1) verbleibende Lots = Bestand (je Asset; je Konto, sofern die Kontozuordnung vollständig ist)
    lot_pos: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    lot_asset: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for lot in led.lots:
        lot_pos[(lot.account, lot.asset)] += lot.qty
        lot_asset[lot.asset] += lot.qty
        if lot.qty < -DUST or lot.cost < -CENT:
            out.append(Item(
                id=_iid("lotneg", lot.id), category="fifo", severity="kritisch",
                title=f"Lot mit negativer Menge oder Kostenbasis: {lot.asset} auf {lot.account}",
                cause_kind="rechenfehler", cause=f"Lot aus {lot.acq_tx} ({lot.acq_date:%d.%m.%Y}): Menge "
                                                 f"{lot.qty.normalize():f}, Kosten {lot.cost.quantize(CENT)} €.",
                impact="Kostenbasis und Gewinne dieses Assets sind nicht verlässlich.",
                recommendation="Fehler melden; betroffene Buchungen prüfen. Portfolia korrigiert Lots nie "
                               "automatisch.", assets=[lot.asset], accounts=[lot.account], tx_ids=[lot.acq_tx],
                source="invariante"))
        if lot.acq_date > snap.today:
            out.append(Item(
                id=_iid("lotfuture", lot.id), category="fifo", severity="warnung",
                title=f"Anschaffungsdatum in der Zukunft: {lot.asset} auf {lot.account}",
                cause_kind="nachgewiesen", cause=f"Lot aus {lot.acq_tx}: Anschaffung am {lot.acq_date:%d.%m.%Y}.",
                impact="Haltefristen und Performance ab diesem Datum sind nicht belastbar.",
                recommendation="Datum der Buchung prüfen und im Journal korrigieren.", assets=[lot.asset],
                accounts=[lot.account], tx_ids=[lot.acq_tx], source="invariante"))
    bal_asset: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for (_acc, aid), q in led.balances.items():
        bal_asset[aid] += q
    checks["positions"] = len(led.balances)
    for aid in sorted(set(bal_asset) | set(lot_asset)):
        if aid in fiat or aid in negative:
            continue  # Fiat ohne Lots; negativer Bestand ist bereits ein eigener Befund (Historie)
        diff = lot_asset[aid] - bal_asset[aid]
        if abs(diff) > max(DUST * 1000, abs(bal_asset[aid]) * Decimal("1e-9")):
            out.append(Item(
                id=_iid("lotsum", aid), category="fifo", severity="kritisch",
                title=f"Lots ≠ Bestand: {aid}",
                cause_kind="rechenfehler",
                cause=f"Summe der verbleibenden Lots {lot_asset[aid].normalize():f}, Bestand "
                      f"{bal_asset[aid].normalize():f} (Differenz {diff.normalize():f}).",
                impact="Kostenbasis, unrealisierte Gewinne und Haltefristen dieses Assets sind nicht verlässlich.",
                recommendation="Fehler melden (Ledger-Invariante verletzt); betroffene Buchungen prüfen.",
                assets=[aid], source="invariante"))
    for (acc, aid), q in sorted(led.balances.items()):
        if aid in fiat or aid in negative or aid in gap_assets:
            continue
        diff = lot_pos[(acc, aid)] - q
        if abs(diff) > max(DUST * 1000, abs(q) * Decimal("1e-9")) and abs(lot_asset[aid] - bal_asset[aid]) <= DUST:
            out.append(Item(
                id=_iid("lotpos", acc, aid), category="fifo", severity="warnung",
                title=f"Lots eines Kontos ≠ Bestand: {aid} auf {acc}",
                cause_kind="rechenfehler",
                cause=f"Lots {lot_pos[(acc, aid)].normalize():f} gegenüber Bestand {q.normalize():f} – die "
                      "Kontozuordnung der Lots weicht ab (Summe über alle Konten stimmt).",
                impact="Haltefristen je Wallet können falsch angezeigt werden; die Steuer je Asset stimmt.",
                recommendation="Transfers dieses Assets prüfen (fehlende Gegenbuchung, falsches Konto).",
                assets=[aid], accounts=[acc], source="invariante"))
    # 2) Veräußerungen: Summe der Lot-Anteile, Anschaffung vor Veräußerung, Gewinn = Erlös − Kosten
    checks["disposals"] = len(led.disposals)
    n = 0
    for d in led.disposals:
        parts_qty = sum((p.qty for p in d.parts), ZERO)
        parts_proceeds = sum((p.proceeds for p in d.parts), ZERO)
        bad: list[str] = []
        if d.parts and abs(parts_qty - d.qty) > DUST * 1000:
            bad.append(f"Lot-Anteile {parts_qty.normalize():f} ≠ Menge {d.qty.normalize():f}")
        if d.parts and abs(parts_proceeds - d.proceeds) > CENT:
            bad.append(f"Erlös-Anteile {parts_proceeds.quantize(CENT)} € ≠ Erlös {d.proceeds.quantize(CENT)} €")
        late = [p for p in d.parts if p.acq_date is not None and p.acq_date > d.date]
        if late:
            bad.append(f"Anschaffung am {late[0].acq_date:%d.%m.%Y} nach der Veräußerung")
        if any(p.qty < -DUST or p.cost < -CENT for p in d.parts):
            bad.append("negativer Lot-Anteil")
        if bad and n < MAX_ITEMS_PER_CHECK:
            n += 1
            out.append(Item(
                id=_iid("disp", d.tx_id, d.asset, d.account), category="fifo", severity="kritisch",
                title=f"Inkonsistente Veräußerung: {d.asset} am {d.date:%d.%m.%Y}",
                cause_kind="rechenfehler", cause="; ".join(bad),
                impact="Realisierter Gewinn/Verlust und Haltefrist dieser Veräußerung sind nicht verlässlich.",
                recommendation="Fehler melden; Buchung und vorherige Zugänge prüfen.", assets=[d.asset],
                accounts=[d.account], tx_ids=[d.tx_id], source="invariante"))
    return out


def check_migrations(ctx: Any, report: Report, checks: dict[str, int]) -> list[Item]:
    """Token-Umstellungen: Buchungen noch wirksam, kein Restbestand des bisherigen Assets."""
    out: list[Item] = []
    snap = report.snapshot
    try:
        rows = ctx.db.q("SELECT id, old_asset, new_asset, tx_ids_json, created_at FROM asset_change WHERE "
                        "kind='migration' "
                        "AND status='applied'")
    except Exception:  # Tabelle fehlt (ältere Datenbank)
        return out
    checks["migrations"] = len(rows)
    active = {t.tx_id for t in snap.pf.txs} if snap.pf is not None else set()
    for r in rows:
        try:
            ids = [str(x) for x in json.loads(r["tx_ids_json"] or "[]")]
        except ValueError:
            ids = []
        gone = [t for t in ids if t not in active]
        rest = sum((q for (acc, aid), q in (snap.ledger.balances.items() if snap.ledger else []) if aid ==
                    r["old_asset"] and q > DUST), ZERO)
        problems = []
        if gone:
            problems.append(f"{len(gone)} Umstellungsbuchung(en) nicht mehr wirksam ({', '.join(gone[:3])})")
        if rest > DUST:
            problems.append(f"Restbestand {rest.normalize():f} {r['old_asset']} nach der Umstellung")
        if problems:
            out.append(Item(
                id=_iid("mig", r["id"]), category="bestand", severity="warnung",
                title=f"Token-Umstellung unvollständig: {r['old_asset']} → {r['new_asset']}",
                cause_kind="nachgewiesen", cause="; ".join(problems),
                impact="Bestände beider Assets und die Kostenbasis des neuen Assets können doppelt bzw. fehlend sein.",
                recommendation="Unter Datenqualität → Ticker/Umstellungen prüfen (ggf. rückgängig machen und neu "
                               "umstellen).", assets=[r["old_asset"], r["new_asset"]], tx_ids=ids[:20],
                source="invariante"))
    return out


def check_tax(ctx: Any, report: Report, checks: dict[str, int]) -> list[Item]:
    """Steuerliche Datenqualität aus dem Regelpaket (je Jahr mit Veräußerungen/Erträgen) und den Steuerdaten je
    Jahr. Keine steuerrechtliche Beurteilung – nur, wo Daten fehlen oder sich widersprechen."""
    out: list[Item] = []
    snap = report.snapshot
    led = snap.ledger
    if led is None:
        return out
    years = sorted({d.date.year for d in led.disposals} | {e.date.year for e in led.income})[-MAX_TAX_YEARS:]
    try:
        from app.tax.service import tax_service

        svc = tax_service(ctx)
        pack = svc.pack()
        inp = svc.build_input(pack, svc.options(pack))
    except Exception as e:  # Steuerteil darf die Prüfung nie verhindern
        log.warning("Integritätsprüfung: Steuerdaten nicht prüfbar: %s", e)
        out.append(Item(id=_iid("taxerr"), category="steuer", severity="warnung",
                        title="Steuerliche Datenqualität nicht prüfbar", cause_kind="hinweis",
                        cause=f"Regelpaket nicht ausführbar ({type(e).__name__}).",
                        impact="Steuerliche Befunde fehlen in dieser Prüfung.", source="steuer"))
        return out
    if inp is None:
        return out
    checks["tax_years"] = len(years)
    for y in years:
        try:
            res = pack.compute(inp, y, svc.options(pack, y))
        except Exception as e:
            out.append(Item(id=_iid("taxyear", y), category="steuer", severity="warnung",
                            title=f"Steuerjahr {y} nicht berechenbar", cause_kind="hinweis",
                            cause=f"{type(e).__name__}", impact="Steuerwerte dieses Jahres fehlen.", source="steuer"))
            continue
        for i in res.issues:
            if i.severity == "info" or i.code in _TAX_COVERED:
                continue
            out.append(Item(
                id=_iid("tax", y, i.code), category="steuer",
                severity="kritisch" if i.severity == "critical" else "warnung",
                title=f"Steuerjahr {y}: {i.text[:120]}", cause_kind="datenluecke" if i.code in (
                    "missing_basis", "missing_lots", "no_value") else "verdacht",
                cause=i.text[:600] + (f" ({i.count}×)" if i.count > 1 else ""),
                impact="Die Steueraufstellung dieses Jahres beruht hier auf Annahmen bzw. unvollständigen Daten.",
                recommendation="Unter Steuern & Haltefristen prüfen; maßgeblich bleibt der Beleg. Keine "
                               "Steuerberatung.",
                source="steuer"))
    try:
        files = ctx.db.q("SELECT id, tax_year, filename, records, matched, unmatched, conflicts FROM tax_file "
                         "WHERE status='active'")
    except Exception:
        files = []
    checks["tax_files"] = len(files)
    for f in files:
        if int(f["conflicts"] or 0) or int(f["unmatched"] or 0):
            out.append(Item(
                id=_iid("taxfile", f["id"], f["conflicts"], f["unmatched"]), category="steuer",
                severity="warnung" if int(f["conflicts"] or 0) else "information",
                title=f"Steuerdaten {f['tax_year']}: Abweichungen zum Journal ({f['filename']})",
                cause_kind="verdacht",
                cause=f"{f['conflicts'] or 0} Widersprüche, {f['unmatched'] or 0} nicht zugeordnete von "
                      f"{f['records'] or 0} Datensätzen.",
                impact="Externer Steuerbericht und Portfolia-Buchungen stimmen hier nicht überein.",
                recommendation="Unter Steuern → Steuerdaten je Jahr die Datensätze prüfen.", source="steuer"))
    return out


def check_prices(ctx: Any, report: Report, checks: dict[str, int]) -> list[Item]:
    """Ungewöhnliche Kurssprünge in gespeicherten Tageskursen gehaltener bzw. gehandelter Assets (z. B. nicht
    splitbereinigte Reihe, falsche Kursidentität) – nur gespeicherte Kurse, keine Abfrage."""
    out: list[Item] = []
    snap = report.snapshot
    pf = snap.pf
    if pf is None:
        return out
    series: dict[str, str] = {}
    for aid, a in pf.assets.items():
        try:
            key = ctx.prices.series_for(a)
        except Exception:
            key = None
        if key:
            series[key] = aid
    if not series:
        return out
    marks = ",".join("?" * len(series))
    try:
        rows = ctx.db.q(f"""SELECT series, date, close, prev, prev_date FROM (
                              SELECT series, date, close, LAG(close) OVER w AS prev, LAG(date) OVER w AS prev_date
                              FROM price_daily WHERE series IN ({marks})
                              WINDOW w AS (PARTITION BY series ORDER BY date))
                            WHERE prev > 0 AND close > 0 AND (close / prev > ? OR prev / close > ?)""",
                        (*series, JUMP_FACTOR, JUMP_FACTOR))
    except Exception as e:  # SQLite ohne Fensterfunktionen o. Ä.
        log.debug("Kurssprünge nicht prüfbar: %s", e)
        return out
    checks["price_series"] = len(series)
    splits: dict[str, set[str]] = defaultdict(set)
    for t in pf.txs:
        if t.type == "corporate_action" and (t.tag or "") in ("split", "reverse_split"):
            for aid in (t.from_asset, t.to_asset):
                if aid:
                    splits[aid].add(t.date.isoformat())
    by_series: dict[str, list[Any]] = defaultdict(list)
    for r in rows:
        by_series[r["series"]].append(r)
    for s, hits in sorted(by_series.items()):
        aid = series.get(s, s)
        first = hits[0]
        at_split = any(h["date"] in splits.get(aid, set()) for h in hits)
        ratio = first["close"] / first["prev"]
        out.append(Item(
            id=_iid("jump", s, len(hits)), category="kurs", severity="warnung",
            title=f"Ungewöhnlicher Kurssprung: {aid}",
            cause_kind="verdacht",
            cause=f"{len(hits)} Tag(e) mit Faktor > {JUMP_FACTOR:g} zum Vortag, erstmals {first['prev_date']} → "
                  f"{first['date']} (×{ratio:.4g})." + (" Fällt auf eine Split-Buchung – Kursreihe vermutlich nicht "
                                                        "splitbereinigt." if at_split else ""),
            impact="Historische Bewertung, TTWROR und Drawdown dieses Assets können verzerrt sein.",
            recommendation="Kursquelle bzw. Kurs-ID prüfen (Datenqualität → Kursquellen); bei Splits die bereinigte "
                           "Reihe verwenden.", assets=[aid], source="kurse"))
    return out


def check_history(ctx: Any, report: Report, checks: dict[str, int]) -> list[Item]:
    """Bewertbarkeit der Historie (vollständig / teilweise geschätzt / unvollständig) aus der vorhandenen
    Tageshistorie – Bewertungslücken zählen nie als Verlust."""
    out: list[Item] = []
    try:
        hist = ctx.history()
    except Exception as e:
        log.debug("Historie für die Integritätsprüfung nicht verfügbar: %s", e)
        return out
    if hist is None or not hist.n:
        return out
    if any((f.data or {}).get("type") == "price_history" for f in report.findings):
        return out  # Diagnose zeigt die Kurslücken der Historie bereits je Asset (kein Doppelbefund)
    from app.analytics import periods as P

    state = P.valuation_state(hist, 0, hist.n - 1)
    checks["history_days"] = hist.n
    code = str(state.get("state") or "")
    if code and code != "complete":
        out.append(Item(
            id=_iid("hist", code), category="kurs",
            severity="warnung" if code == "incomplete" else "information",
            title=f"Historische Bewertung: {state.get('label') or code}",
            cause_kind="datenluecke",
            cause=f"Positions-Tage ohne Kurs: {state.get('missing_days', 0)}, mit geschätztem Kurs "
                  f"(fortgeschrieben, Transaktions-, manueller Kurs): {state.get('estimated_days', 0)}.",
            impact="Tage ohne Kurs werden in TTWROR/IRR/G/V neutral behandelt (kein Scheinverlust); Kennzahlen "
                   "beruhen dort auf weniger Positionen.",
            recommendation="Fehlende Kurse ergänzen (Kursquellen, Ersatzkurse) – siehe Diagnose „Fehlender oder "
                           "veralteter Kurs“.", source="historie"))
    return out


# ----------------------------------------------------------------------------------------------------------------------
# Lauf
# ----------------------------------------------------------------------------------------------------------------------

def _status(items: list[Item], report: Report, db: Any) -> None:
    """Status aus den Entscheidungen der Diagnose: geprüft (gleiche Befunddaten), verworfen (als unabhängig
    bestätigt), korrigiert (aktive Korrektur, Befund besteht in anderer Form weiter)."""
    marks = A.active_dismissals(db)
    fixes = A.active_fixes(db)
    by_id = {f.id: f for f in report.findings}
    for it in items:
        d = marks.get(it.finding_id) if it.finding_id else None
        f = by_id.get(it.finding_id)
        if d is not None and f is not None and d.fingerprint == A.fingerprint(f):
            it.status = "verworfen" if (d.note or "").startswith(INDEPENDENT_NOTE) else "geprüft"
        elif it.finding_id and fixes.get(it.finding_id):
            it.status = "korrigiert"


def _corrected(db: Any, present: set[str]) -> list[Item]:
    """Aktive Korrekturen, deren Befund nicht mehr besteht (Nachweis im Prüfbericht)."""
    out = []
    for fid, decs in A.active_fixes(db).items():
        if fid in present:
            continue
        d = decs[-1]
        out.append(Item(id=fid, category=_KIND_CAT.get(d.kind, "bestand"), severity="information", title=d.title,
                        cause_kind="hinweis", cause=f"Korrigiert am {d.created_at[:10]}: {d.option_label or ''}",
                        impact="Befund durch die übernommene Korrektur behoben.", finding_id=fid, status="korrigiert",
                        source="diagnose"))
    return out


def run(ctx: Any, progress: Any | None = None) -> Run:
    """Vollständige Prüfung (rein lesend bis auf das gespeicherte Ergebnis). Läuft nie doppelt gleichzeitig."""
    if not _LOCK.acquire(blocking=False):
        raise RuntimeError("Eine Integritätsprüfung läuft bereits.")
    try:
        return _run(ctx, progress)
    finally:
        _LOCK.release()


def _phase(progress: Any, key: str, text: str) -> None:
    if progress is not None:
        progress.phase(key, text=text)


def _run(ctx: Any, progress: Any | None) -> Run:
    from app.diagnosis.collect import collect
    from app.diagnosis.engine import diagnose

    t0 = time.monotonic()
    result = Run(started_at=iso(datetime.now(UTC)) or "")
    version = A.data_version(ctx.db)
    timings: dict[str, int] = {}

    def timed(name: str, fn: Callable[[], Any]) -> Any:
        t = time.monotonic()
        try:
            return fn()
        finally:
            timings[name] = int((time.monotonic() - t) * 1000)

    try:
        _phase(progress, "prepare", "Schnappschuss: Buchungen, Ledger, Kurse")
        snap = timed("snapshot", lambda: collect(ctx))
        _phase(progress, "process", "Diagnose-Regeln und Invarianten")
        report = timed("diagnose", lambda: diagnose(snap))
        items: list[Item] = []
        items += timed("findings", lambda: from_findings(report))
        items += timed("ledger", lambda: check_ledger(report, result.checks))
        items += timed("migrations", lambda: check_migrations(ctx, report, result.checks))
        _phase(progress, "reconcile", "Steuerliche Datenqualität")
        items += timed("tax", lambda: check_tax(ctx, report, result.checks))
        _phase(progress, "prices", "Kurse und Bewertbarkeit")
        items += timed("prices", lambda: check_prices(ctx, report, result.checks))
        items += timed("history", lambda: check_history(ctx, report, result.checks))
        _status(items, report, ctx.db)
        items += _corrected(ctx.db, {it.finding_id for it in items if it.finding_id})
        seen: set[str] = set()
        result.items = [it for it in items if not (it.id in seen or seen.add(it.id))]  # type: ignore[func-returns-value]
        result.items.sort(key=sort_key)
        result.txs = len(snap.pf.txs) if snap.pf is not None else 0
        result.assets = len(snap.pf.assets) if snap.pf is not None else 0
        result.version = version
        result.stale = A.data_version(ctx.db) != version
    except Exception as e:
        log.exception("Integritätsprüfung fehlgeschlagen")
        result.ok = False
        result.error = f"{type(e).__name__}: {e}"[:500]
    result.timings = timings
    result.duration_ms = int((time.monotonic() - t0) * 1000)
    result.finished_at = iso(datetime.now(UTC)) or ""
    _phase(progress, "save", "Ergebnis speichern")
    save(ctx.db, result)
    if progress is not None:
        c = result.counts()
        progress.finish(result.ok, result=f"{c['open']} offene Befunde" if result.ok else result.error)
    return result


def sort_key(it: Item) -> tuple[Any, ...]:
    return (it.status != "offen", SEVERITY_ORDER.get(it.severity, 9), -(it.impact_eur or 0), it.category, it.title)


def save(db: Any, r: Run) -> None:
    db.set_state(STATE_KEY, asdict(r))


def load(db: Any) -> Run | None:
    raw = db.get_state(STATE_KEY)
    if not isinstance(raw, dict):
        return None
    items = [Item(**{k: v for k, v in it.items() if k in Item.__dataclass_fields__}) for it in raw.get("items") or []]
    data = {k: v for k, v in raw.items() if k in Run.__dataclass_fields__ and k != "items"}
    return Run(**data, items=items)


def refresh_status(ctx: Any, r: Run) -> Run:
    """Status gespeicherter Befunde mit den aktuellen Entscheidungen abgleichen (ohne neuen Prüflauf)."""
    marks = A.active_dismissals(ctx.db)
    fixes = A.active_fixes(ctx.db)
    for it in r.items:
        if not it.finding_id or it.status == "korrigiert":
            continue
        d = marks.get(it.finding_id)
        if fixes.get(it.finding_id):
            it.status = "korrigiert"
        elif d is not None:
            it.status = "verworfen" if (d.note or "").startswith(INDEPENDENT_NOTE) else "geprüft"
        elif it.status in ("geprüft", "verworfen"):
            it.status = "offen"
    r.stale = r.stale or (bool(r.version) and A.data_version(ctx.db) != r.version)
    return r


def filtered(r: Run, *, category: str = "", severity: str = "", asset: str = "", account: str = "",
             status: str = "", sort: str = "") -> list[Item]:
    a, acc = asset.strip().upper(), account.strip().lower()
    out = [it for it in r.items
           if (not category or it.category == category) and (not severity or it.severity == severity)
           and (not a or any(x.upper() == a for x in it.assets))
           and (not acc or any(acc in x.lower() for x in it.accounts))
           and (not status or it.status == status)]
    if sort == "impact":
        out.sort(key=lambda it: (-(it.impact_eur or 0), SEVERITY_ORDER.get(it.severity, 9), it.title))
    elif sort == "confidence":
        rank = {c: i for i, c in enumerate(CONFIDENCE)}
        out.sort(key=lambda it: (rank.get(it.confidence, 9), -(it.impact_eur or 0), it.title))
    return out


EXPORT_COLS = ("id", "category", "severity", "status", "title", "cause_kind", "cause", "impact", "impact_eur",
               "recommendation", "preferred", "alternatives", "confidence", "assets", "accounts", "tx_ids",
               "finding_id", "source")


def export_csv(r: Run, items: list[Item]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(EXPORT_COLS)
    for it in items:
        d = asdict(it)
        w.writerow(["|".join(d[k]) if isinstance(d[k], list) else ("" if d[k] is None else d[k]) for k in EXPORT_COLS])
    return buf.getvalue().encode("utf-8-sig")


def export_json(r: Run, items: list[Item]) -> bytes:
    meta = {k: v for k, v in asdict(r).items() if k != "items"}
    return json.dumps({"run": meta, "counts": r.counts(), "items": [asdict(it) for it in items]},
                      ensure_ascii=False, indent=1, default=str).encode("utf-8")


def age_text(r: Run) -> str:
    t = parse_iso(r.finished_at)
    if t is None:
        return ""
    mins = int((datetime.now(UTC) - t).total_seconds() // 60)
    return "gerade eben" if mins < 1 else f"vor {mins} min" if mins < 120 else f"vor {mins // 60} h"
