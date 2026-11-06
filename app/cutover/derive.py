"""Abgeleitete Vorschläge (0.101.0): Beleg **nach** dem Stichtag, Korrektur zum Stichtag nur wo sie belegt ist.

Ein Beleg (Referenzbestand oder vollständig abgerufener Bestand der Quelle) zum Zeitpunkt ``D ≥ Stichtag`` weicht vom
berechneten Bestand ab. Wo die Abweichung entstand, sagt der Beleg allein nicht – deshalb gilt:

* **Keine Buchung** auf diesem Konto/Asset zwischen Stichtag und ``D``: Die Abweichung kann nur schon am Stichtag
  bestanden haben (Annahme: die Quelle ist vollständig). Das ist ein *abgeleiteter Vorschlag* für eine Korrektur zum
  Stichtag (Abgang bzw. Zugang) – ausdrücklich als „abgeleitet“ gekennzeichnet, nie automatisch gebucht.
* **Buchungen dazwischen**: Die Abweichung kann auch erst danach entstanden sein. Es gibt keinen Vorschlag zum Stichtag,
  nur einen Hinweis auf mögliche Ursachen *im aktiven Zeitraum* (offene Abgänge ohne Gegenbuchung, die die Differenz
  erklären; Abgänge ohne Gebühr bei zu wenig Bestand bei der Quelle). Die Behebung läuft über die gewohnten Wege
  (Transfer verknüpfen, Gebühr nachtragen).

Rein lesend; ohne Beleg nach dem Stichtag entsteht nichts.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, time
from decimal import Decimal
from typing import Any

from app.cutover import core as CO
from app.util.timeutil import local_tz

ZERO = Decimal(0)
TOL = Decimal("0.01")  # relative Toleranz beim Vergleich von Summen (1 %)
OUTFLOW_TYPES = ("withdrawal", "transfer", "sell", "trade")


def _effect(t: Any, key: tuple[str, str]) -> Decimal:
    acc, aid = key
    q = ZERO
    if t.from_account == acc and t.from_asset == aid and t.from_qty:
        q -= t.from_qty
    if t.to_account == acc and t.to_asset == aid and t.to_qty:
        q += t.to_qty
    if t.fee_asset == aid and t.fee_qty and (t.from_account or t.to_account) == acc:
        q -= t.fee_qty
    return q


def _touches(t: Any, key: tuple[str, str]) -> bool:
    acc, aid = key
    return ((t.from_account == acc and t.from_asset == aid) or (t.to_account == acc and t.to_asset == aid)
            or (t.fee_asset == aid and (t.from_account or t.to_account) == acc))


def evidence(ctx: Any, m: CO.Model, *, observed: bool = True, stale: bool = False) -> list[dict[str, Any]]:
    """Belege nach dem Stichtag: Referenzbestände (``as_of ≥ Stichtag``) und – falls gewünscht – vollständig
    abgerufene Bestände der Quellen mit Abweichung (Diagnose, ``extern_diff``). Je (Konto, Asset) der jüngste Beleg.
    ``stale``: auch Bestände älterer bzw. nicht bestätigter Abrufe (``extern_unsicher``), als solche gekennzeichnet."""
    from app.diagnosis.collect import references

    out: dict[tuple[str, str], dict[str, Any]] = {}
    for r in references(ctx.db):
        if r.as_of < m.cutoff or r.scope == "combined":
            continue
        at = r.at or datetime.combine(r.as_of, time(23, 59, 59), tzinfo=local_tz()).astimezone(UTC)
        key = (r.account, r.asset_id)
        if key not in out or at > out[key]["at"]:
            out[key] = {"account": r.account, "asset": r.asset_id, "qty": r.qty, "at": at,
                        "kind": "Referenzbestand", "source": r.source, "note": r.note or ""}
    if observed:
        from app.diagnosis.engine import report_for

        for h in report_for(ctx).holdings:
            if h.observed is None or h.observed_at is None:
                continue
            if h.status != "extern_diff" and not (stale and h.status == "extern_unsicher"):
                continue
            at = h.observed_at if h.observed_at.tzinfo else h.observed_at.replace(tzinfo=UTC)
            key = (h.account, h.asset)
            if at.astimezone(local_tz()).date() < m.cutoff:
                continue
            if key not in out or at > out[key]["at"]:
                out[key] = {"account": h.account, "asset": h.asset, "qty": h.observed, "at": at,
                            "kind": "Bestand laut Quelle", "source": h.observed_by or "Datenquelle",
                            "note": h.observed_state or "", "stale": h.status != "extern_diff"}
    return sorted(out.values(), key=lambda e: (e["account"], e["asset"]))


def derive(ctx: Any, m: CO.Model, *, observed: bool = True) -> dict[str, list[dict[str, Any]]]:
    """``{"proposals": …, "hints": …}`` aus den Belegen nach dem Stichtag."""
    proposals: list[dict[str, Any]] = []
    hints: list[dict[str, Any]] = []
    txs = [t for t in m.pf_full.txs if t.date >= m.cutoff and t.flag != "estimated"]
    for ev in evidence(ctx, m, observed=observed):
        key = (ev["account"], ev["asset"])
        if m.pf_full.asset(ev["asset"]).is_fiat:
            continue
        window = [t for t in txs if t.ts <= ev["at"] and _touches(t, key)]
        have = m.state.balances.get(key, ZERO) + sum((_effect(t, key) for t in window), ZERO)
        diff = ev["qty"] - have
        if diff == 0 or abs(diff) <= CO.STAUB_QTY:
            continue
        base = {"account": key[0], "asset": key[1], "diff": diff, "qty": abs(diff), "reference": ev["qty"],
                "computed": have, "at": ev["at"], "kind_label": ev["kind"], "source": ev["source"]}
        if not window:
            proposals.append({
                **base, "kind": "remove" if diff < 0 else "add", "mode": "derived",
                "evidence": (f"abgeleitet: {ev['kind']} {ev['qty'].normalize():f} am "
                             f"{ev['at'].astimezone(local_tz()):%d.%m.%Y}, seit dem Stichtag keine Buchung")[:100],
                "text": "Seit dem Stichtag gibt es auf diesem Konto/Asset keine Buchung – die Abweichung bestand "
                        "(unter der Annahme einer vollständigen Quelle) schon am Stichtag."})
            continue
        hints.append({**base, "window": len(window), **_causes(m, key, diff, ev["at"], window)})
    return {"proposals": proposals, "hints": hints}


def _causes(m: CO.Model, key: tuple[str, str], diff: Decimal, at: datetime, window: list[Any]) -> dict[str, Any]:
    """Mögliche Ursachen im aktiven Zeitraum (nur Hinweise, keine Buchung)."""
    if diff > 0:  # Beleg höher als berechnet: Gegenbuchung zu offenen Abgängen anderer Konten?
        outs = [f for f in m.led_full.flows
                if f.kind == "transfer_out_external" and f.asset == key[1] and m.cutoff <= f.date and f.ts <= at
                and f.account != key[0]]
        by_acc: dict[str, list[Any]] = defaultdict(list)
        for f in outs:
            by_acc[f.account or ""].append(f)
        groups = sorted(by_acc.items()) + ([("alle Konten", outs)] if len(by_acc) > 1 else [])
        for acc, fs in groups:
            total = sum((abs(f.qty) for f in fs), ZERO)
            if fs and abs(total - diff) <= diff * TOL:
                return {"cause": "transfer", "tx_ids": [f.tx_id for f in fs],
                        "text": (f"{len(fs)} offene Abgang/Abgänge von „{acc}“ (Summe {total.normalize():f}) ohne "
                                 "Gegenbuchung erklären die Differenz: vermutlich fehlt der Zugang auf diesem Konto "
                                 "bzw. die Verknüpfung als Transfer.")}
        return {"cause": "unknown", "tx_ids": [],
                "text": "Zu wenig gebucht; keine offenen Abgänge anderer Konten erklären die Differenz."}
    out = [t for t in window if t.type in OUTFLOW_TYPES and t.from_account == key[0] and t.from_asset == key[1]
           and not t.fee_qty]
    if out:
        per = abs(diff) / len(out)
        return {"cause": "fees", "tx_ids": [t.tx_id for t in out],
                "text": (f"{len(out)} Abgang/Abgänge ohne Gebühr im aktiven Zeitraum; läge die Differenz an fehlenden "
                         f"Gebühren, wären es ≈ {per.normalize():f} je Abgang – bei der Quelle prüfen.")}
    return {"cause": "unknown", "tx_ids": [],
            "text": "Zu viel gebucht; keine Abgänge ohne Gebühr, die als Ursache in Frage kommen."}

