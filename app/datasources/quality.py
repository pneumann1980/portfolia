"""Vollständigkeitsbericht einer Börsen-Datenquelle (vor allem Bitpanda): nachgewiesene Fehler, plausible Lücken,
nicht verifizierbare Zeiträume.

Grundlage sind ausschließlich Belege aus den Abrufen (``coverage_json``: Pagination, Diagnose der Felder, Saldoverlauf
``asset_balance_after``, Abgleich mit ``/v1/portfolio``, Kennzahlen des letzten vollständigen Abrufs der Historie) und
die Prüf-Stapel bzw. Buchungen in Portfolia. Eine erfolgreiche Antwort der API gilt nicht als Beleg für Vollständigkeit.

* **nachgewiesen** – die Daten widersprechen sich nachweislich: Abruf unvollständig, Pflichtfelder fehlen, Brüche im
  Saldoverlauf, Bestand laut ``/portfolio`` ≠ Summe der Vorgänge, übernommene Vorgänge fehlen im vollständigen Abruf.
* **plausibel** – Hinweise ohne Beweis: Monate ohne Vorgänge innerhalb aktiver Zeiträume, Buchungen des kuratierten
  Imports auf dem Konto ohne Gegenstück in der API, Vorgänge mit älterer Auswertung.
* **nicht verifizierbar** – Zeiträume bzw. Angaben, die sich mit den API-Daten nicht prüfen lassen: vor dem ersten
  Vorgang, Vorgänge ohne Zeitpunkt, Zeitraum seit dem letzten vollständigen Abruf (nur inkrementell), Bestand ohne
  vollständige Historie.

Nur Anzeige; nichts wird automatisch korrigiert.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.util.timeutil import fmt_de_date, parse_iso, to_local_date

MONTHS = ("Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez")
MAX_EXAMPLES = 8


@dataclass
class Finding:
    title: str
    detail: str = ""
    examples: list[str] = field(default_factory=list)


@dataclass
class Report:
    proven: list[Finding] = field(default_factory=list)
    plausible: list[Finding] = field(default_factory=list)
    unverifiable: list[Finding] = field(default_factory=list)
    history: dict[str, Any] | None = None
    # Jahr → Vorgänge je Monat (None = außerhalb des abgerufenen Zeitraums)
    years: list[tuple[int, list[int | None]]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.proven or self.plausible or self.unverifiable)


def _month_grid(months: dict[str, int], first: date, last: date) -> list[tuple[int, list[int | None]]]:
    out = []
    for y in range(first.year, last.year + 1):
        row: list[int | None] = []
        for m in range(1, 13):
            inside = (y, m) >= (first.year, first.month) and (y, m) <= (last.year, last.month)
            row.append(int(months.get(f"{y:04d}-{m:02d}", 0)) if inside else None)
        out.append((y, row))
    return out


def _quiet_months(months: dict[str, int], first: date, last: date) -> list[str]:
    """Monate ohne Vorgänge, deren Nachbarmonate (je ± 2) im Mittel mindestens zwei Vorgänge haben – bei regelmäßiger
    Aktivität (z. B. Sparplan) ein Hinweis auf fehlende Vorgänge, kein Beweis."""
    keys = []
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        keys.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    out = []
    for i, k in enumerate(keys):
        if months.get(k, 0):
            continue
        around = [months.get(keys[j], 0) for j in range(max(0, i - 2), min(len(keys), i + 3)) if j != i]
        if around and sum(around) / len(around) >= 2:
            out.append(f"{MONTHS[int(k[5:]) - 1]} {k[:4]}")
    return out


def _referenced_imports(ctx: Any, sid: int) -> set[str]:
    """Buchungen des kuratierten Imports, denen ein Vorgang dieser Quelle zugeordnet ist (Prüfzeilen, Verknüpfungen,
    abgedeckte App-Buchungen)."""
    db = ctx.db
    out: set[str] = set()
    for r in db.q("SELECT r.messages, r.tx_id, r.status FROM csv_row r JOIN csv_batch b ON b.id = r.batch_id WHERE "
                  "b.datasource_id=?", (sid,)):
        try:
            msgs = json.loads(r["messages"] or "{}")
        except ValueError:
            continue
        out.update(str(x) for x in msgs.get("dup_of") or [])
        if r["status"] == "linked" and r["tx_id"]:
            out.add(r["tx_id"])
    for r in db.q("SELECT t.tx_id FROM tx_link t JOIN csv_batch b ON b.id = t.batch_id WHERE b.datasource_id=? AND "
                  "t.status='active'", (sid,)):
        out.add(r["tx_id"])
    try:
        from app.journal.reconcile import coverage

        own = {r["tx_id"] for r in db.q("SELECT tx_id FROM journal_tx WHERE datasource_id=?", (sid,))}
        base, _ = ctx.effective_base()
        for jid, cov in coverage(db, base).items():
            if jid in own:
                out.add(cov.import_tx_id)
    except (ImportError, AttributeError, ValueError):
        pass
    return out


def report(ctx: Any, ds: Any) -> Report | None:
    """Bericht für eine Börsen-Datenquelle (``None`` bei Wallets bzw. ohne Abruf)."""
    if ds is None or ds.is_wallet or not ds.supported:
        return None
    cov = ds.coverage or {}
    if not cov:
        return None
    rep = Report()
    hist = cov.get("history")
    rep.history = hist
    # -- nachgewiesen ------------------------------------------------------------------------------------
    if not cov.get("complete", True):
        rep.proven.append(Finding("Letzter Abruf unvollständig", "Die Vorgangsliste wurde nicht bis zum Ende "
                                  "gelesen (Pagination bzw. Fehler) – fehlende Vorgänge sind möglich, bis ein Abruf "
                                  "vollständig ist.", list(cov.get("gaps") or [])[:MAX_EXAMPLES]))
    dg = cov.get("diagnostics") or {}
    if dg.get("missing"):
        rep.proven.append(Finding("Pflichtfelder fehlen in API-Antworten", "Diese laut Referenz dokumentierten Felder "
                                  "fehlten – betroffene Vorgänge sind ungeklärt bzw. ohne Zeitpunkt.",
                                  [f"{k}: {n}×" for k, n in dg["missing"].items()][:MAX_EXAMPLES]))
    dc = dg.get("counts") or {}
    if dc.get("Saldoverlauf: Brüche"):
        rep.proven.append(Finding(
            f"{dc['Saldoverlauf: Brüche']} Brüche im Saldoverlauf",
            f"Bei {dc['Saldoverlauf: Brüche']} von {dc.get('Saldoverlauf: Übergänge', 0)} Übergängen erklärt der "
            "Betrag die Änderung von asset_balance_after gegenüber dem vorigen Vorgang desselben Wallets nicht – "
            "dazwischen fehlen Vorgänge oder Bitpanda stellt sie anders dar (z. B. interne Umbuchungen)."))
    bal = cov.get("balances") or {}
    if bal.get("checked") and bal.get("differences"):
        rep.proven.append(Finding(f"Bestand laut /portfolio weicht bei {bal['differences']} Asset(s) von der Summe der "
                                  "Vorgänge ab", "Geprüft wurden alle dokumentierten Lesarten (Gebühren zusätzlich, "
                                  "Staking-Umbuchungen); keine erklärt die Abweichung.",
                                  list(bal.get("examples") or [])[:MAX_EXAMPLES]))
    if hist and hist.get("missing"):
        ex = [str(k) for k in hist.get("missing_examples") or []]
        rep.proven.append(Finding(
            f"{hist['missing']} übernommene bzw. verknüpfte Vorgänge fehlen im letzten vollständigen Abruf",
            f"Abruf vom {fmt_de_date(hist.get('at'))}: Diese Vorgänge sind in Portfolia gebucht"
            f"{' bzw. verknüpft' if hist.get('missing_linked') else ''}, die API liefert sie nicht mehr (z. B. "
            "storniert, umgestellt oder Antwort unvollständig). Buchungen bleiben unverändert – bitte prüfen.", ex))
    # -- plausibel -----------------------------------------------------------------------------------------
    first = to_local_date(parse_iso(hist["first"])) if hist and hist.get("first") else None  # type: ignore[arg-type]
    last = to_local_date(parse_iso(hist["last"])) if hist and hist.get("last") else None  # type: ignore[arg-type]
    if hist and first and last:
        months = {k: int(v) for k, v in (hist.get("months") or {}).items()}
        rep.years = _month_grid(months, first, last)
        quiet = _quiet_months(months, first, last)
        if quiet:
            rep.plausible.append(Finding(f"{len(quiet)} Monat(e) ohne Vorgänge in sonst aktiven Zeiträumen",
                                         "Kein Beweis – bei regelmäßigen Vorgängen (z. B. Sparplan) aber ein Hinweis "
                                         "auf fehlende Daten.", quiet[:12]))
        base, _ = ctx.effective_base()
        if base is not None and ds.account:
            ref = _referenced_imports(ctx, int(ds.id))
            orphans = [t for t in base.txs if ds.account in (t.from_account, t.to_account) and first <= t.date <= last
                       and t.tx_id not in ref]
            if orphans:
                orphans.sort(key=lambda t: t.ts)
                ex = [f"{t.tx_id} · {fmt_de_date(t.date)} · {t.type}"
                      + (f" −{t.from_qty.normalize():f} {t.from_asset}" if t.from_qty and t.from_asset else "")
                      + (f" +{t.to_qty.normalize():f} {t.to_asset}" if t.to_qty and t.to_asset else "")
                      for t in orphans[:MAX_EXAMPLES]]
                rep.plausible.append(Finding(
                    f"{len(orphans)} Buchungen des kuratierten Imports auf „{ds.account}“ ohne Gegenstück in der API",
                    "Im abgerufenen Zeitraum, aber keinem API-Vorgang zugeordnet – möglich sind fehlende Vorgänge in "
                    "der API, anders zusammengefasste Buchungen (z. B. Steuertool) oder ein anderes Konto.", ex))
    try:
        from app.datasources.service import datasource_service

        outdated = datasource_service(ctx).outdated(int(ds.id))
    except (ImportError, AttributeError):
        outdated = 0
    if outdated:
        rep.plausible.append(Finding(f"{outdated} offene Vorgänge mit älterer Auswertung",
                                     "Zeilen einer früheren Version der Anbindung (z. B. ohne Zeitpunkt) – "
                                     "„Vollständig neu abrufen“ ersetzt unbearbeitete."))
    # -- nicht verifizierbar ---------------------------------------------------------------------------------
    if not hist:
        rep.unverifiable.append(Finding("Kein vollständiger Abruf der Historie protokolliert",
                                        "Vollständigkeit erst beurteilbar nach „Vollständig neu abrufen“ (seit 0.17.0 "
                                        "werden dabei Zeitraum, Monate und fehlende Vorgänge festgehalten)."))
    else:
        if first:
            rep.unverifiable.append(Finding(f"Vor dem {fmt_de_date(first)}",
                                            "Erster Vorgang laut API – davor lässt sich nichts über die API prüfen "
                                            "(z. B. Konto später eröffnet oder ältere Vorgänge nicht geliefert)."))
        if hist.get("no_ts"):
            rep.unverifiable.append(Finding(f"{hist['no_ts']} Vorgänge ohne Zeitpunkt",
                                            "transactions[].credited_at fehlt – Datum und Zeitraum nicht prüfbar; "
                                            "diese Vorgänge werden nie gebucht."))
        if cov.get("mode") != "vollständig" or cov.get("at") != hist.get("at"):
            rep.unverifiable.append(Finding(f"Seit dem {fmt_de_date(hist.get('at'))} nur inkrementell abgerufen",
                                            "Spätere Läufe lesen ab dem Abrufstand (mit Überlappung); Stornos älterer "
                                            "Vorgänge erkennt erst der nächste vollständige Abruf."))
    if not bal.get("checked") or not bal.get("compared"):
        rep.unverifiable.append(Finding("Bestand nicht gegen die Vorgänge geprüft",
                                        bal.get("note") or "Der Vergleich mit /portfolio erfolgt nur nach "
                                                           "vollständigem Abruf der Historie."))
    return rep


def summary_counts(rep: Report | None) -> dict[str, int]:
    if rep is None:
        return {}
    return {"proven": len(rep.proven), "plausible": len(rep.plausible), "unverifiable": len(rep.unverifiable)}


__all__ = ["Finding", "Report", "report", "summary_counts"]
