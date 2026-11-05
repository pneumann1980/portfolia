"""Stichtag: Eröffnungsstand, aktiver Stand und Gleichwertigkeitsprüfung (0.98.0).

Begriffe
--------
* **Stichtag** ``C``: erster aktiver Tag. Buchungen mit Datum ``< C`` bilden das **Archiv**, Buchungen ab ``C`` den
  **aktiven Stand**.
* **Eröffnungsstand**: offene Lots und Bestände zum Ende von ``C − 1``, aus der vollständigen Berechnung abgeleitet
  (Anschaffungsdatum, -kosten und Herkunft jedes Lots bleiben; der Stichtag ist kein neues Kaufdatum).
* **Aktiver Stand**: ``Portfolio`` mit Buchungen ab ``C`` und dem Eröffnungsstand. Der Ledger startet damit statt mit
  der gesamten Vorgeschichte.
* **Voller Stand**: Archiv + aktiv, wie bisher – Grundlage von Depotwert, eingesetztem Kapital und Verlauf ab
  Depotstart.

Pflichtprüfung (Gleichwertigkeit): Der aktive Stand muss dieselben Bestände, Lots, Veräußerungen, Erträge, Gebühren,
Steuerereignisse und Zahlungsströme ab ``C`` liefern wie die vollständige Berechnung. Nur dann ist der Stichtag
zulässig.
"""

from __future__ import annotations

import dataclasses
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from app.ledger.engine import DUST, LedgerResult, run_ledger
from app.ledger.models import OpeningState, Portfolio

log = logging.getLogger(__name__)

ZERO = Decimal(0)
# Quellen, die Korrekturen/Rekonstruktionen kennzeichnen (Gleichlauf mit dem Diagnosemodul)
CORR_SOURCES = frozenset({"reconstructed", "manual", "portfolia:manual", "portfolia:diagnose"})
STAUB_EUR = Decimal("0.01")  # Differenzen unter 1 Cent Gegenwert (bzw. Rundungsreste) nur ausweisen, nie korrigieren
STAUB_QTY = Decimal("1e-8")


@dataclass
class Check:
    key: str
    label: str
    ok: bool
    detail: str = ""


@dataclass
class Model:
    cutoff: date
    opts: Any
    pf_full: Portfolio
    led_full: LedgerResult
    state: OpeningState
    pf_active: Portfolio
    led_active: LedgerResult
    archive_txs: int
    active_txs: int
    checks: list[Check] = field(default_factory=list)
    label: str = ""

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


def is_correction(t: Any) -> bool:
    return (t.source in CORR_SOURCES) or t.tag == "opening_balance" or t.origin == "journal"


def split(pf: Portfolio, cutoff: date) -> tuple[list[Any], list[Any]]:
    arch, act = [], []
    for t in pf.txs:
        (arch if t.date < cutoff else act).append(t)
    return arch, act


def active_portfolio(pf: Portfolio, cutoff: date, state: OpeningState) -> Portfolio:
    """Portfolio ab dem Stichtag: Buchungen ab ``cutoff`` plus Eröffnungsstand (Archiv-Buchungen entfallen)."""
    _arch, act = split(pf, cutoff)
    return dataclasses.replace(pf, txs=act, opening=state)


def tax_engine_options(ctx: Any, cutoff: date) -> Any:
    """Rechenweise der Steuer (FIFO je Wallet bzw. global laut Steuer-Einstellung) – maßgeblich für den Eröffnungsstand
    der Steuer; die Anzeige der App kann davon abweichen (``ledger.scope``)."""
    from app.tax.service import tax_service

    ts = tax_service(ctx)
    pack = ts.pack()
    return pack.engine_options(ctx.engine_options(), ts.options(pack, cutoff.year), [])


def option_sets(ctx: Any, cutoff: date) -> list[tuple[str, Any]]:
    """Alle Rechenweisen, für die der Stichtag gelten muss: Steuer (zuerst, angezeigt) und App-Ansicht."""
    tax = dataclasses.replace(tax_engine_options(ctx, cutoff), snapshot_dates=())
    app = dataclasses.replace(ctx.engine_options(), snapshot_dates=())
    out = [("Steuer", tax)]
    if app != tax:
        out.append(("App-Ansicht", app))
    return out


def _scope_label(opts: Any) -> str:
    return "FIFO je Konto/Wallet" if opts.scope == "account" else "FIFO global je Asset"


def opening_state(ctx: Any, pf: Portfolio, cutoff: date, opts: Any = None) -> tuple[LedgerResult, OpeningState]:
    """Vollständige Berechnung mit Zustand zum Ende von ``cutoff − 1``."""
    base = opts if opts is not None else ctx.engine_options()
    led = ctx.ledger(opts=dataclasses.replace(base, snapshot_dates=(cutoff - timedelta(days=1),)))
    if led is None:
        raise ValueError("Keine Daten")
    st = led.state_snapshots.get(cutoff - timedelta(days=1))
    if st is None:  # keine Buchung vor dem Stichtag: leerer Eröffnungsstand
        st = OpeningState(cutoff, [], {}, frozenset(), dict(led.cash_tracked))
    return led, st


# -- Vergleich ----------------------------------------------------------------------------------------------------

def _lot_key(lot: Any) -> tuple[Any, ...]:
    return (lot.asset, lot.account, lot.qty, lot.cost, lot.acq_ts, lot.acq_tx, lot.origin, lot.income_tag, lot.via)


def _disp_key(d: Any) -> tuple[Any, ...]:
    return (d.tx_id, d.kind, d.asset, d.account, d.qty, d.proceeds, d.fee_eur,
            tuple(sorted(((p.account, p.qty, p.cost, p.proceeds, p.acq_date, p.origin, p.missing_basis, p.acq_tx, p.via)
                          for p in d.parts), key=repr)))


def _norm_bal(b: dict[Any, Decimal]) -> dict[Any, Decimal]:
    return {k: v for k, v in b.items() if abs(v) > DUST}


def compare(full: LedgerResult, active: LedgerResult, cutoff: date, tx_dates: dict[str, date]) -> list[Check]:
    """Gleichwertigkeit: aktiver Stand gegen vollständige Berechnung, jeweils ab ``cutoff`` bzw. am Ende."""
    out: list[Check] = []

    def add(key: str, label: str, a: Any, b: Any, what: str = "") -> None:
        ok = a == b
        out.append(Check(key, label, ok, "" if ok else what or "abweichend"))

    fb, ab = _norm_bal(full.balances), _norm_bal(active.balances)
    diff = sorted(k for k in set(fb) | set(ab) if fb.get(k) != ab.get(k))
    out.append(Check("balances", "Bestände heute (Konto · Asset)", not diff,
                     "" if not diff else f"{len(diff)} Position(en) weichen ab, z. B. {diff[0][0]} · {diff[0][1]}"))
    fl = sorted(map(_lot_key, full.lots), key=repr)
    al = sorted(map(_lot_key, active.lots), key=repr)
    add("lots", "offene Lots (Menge, Anschaffungskosten, Anschaffungsdatum, Herkunft)", fl, al,
        f"{len(fl)} Lots vollständig, {len(al)} aktiv")
    fd = sorted((_disp_key(d) for d in full.disposals if d.date >= cutoff), key=repr)
    ad = sorted((_disp_key(d) for d in active.disposals), key=repr)
    add("disposals", "Veräußerungen ab Stichtag (Erlös, Anschaffungskosten, Haltedauer)", fd, ad,
        f"{len(fd)} vollständig, {len(ad)} aktiv")
    for key, label, fa, aa in (
        ("income", "Erträge ab Stichtag", [x for x in full.income if x.date >= cutoff], active.income),
        ("fees", "Gebühren ab Stichtag", [x for x in full.fees if x.date >= cutoff], active.fees),
        ("taxes", "einbehaltene Steuern ab Stichtag", [x for x in full.taxes if x.date >= cutoff], active.taxes),
    ):
        add(key, label, sorted(map(dataclasses.astuple, fa), key=repr), sorted(map(dataclasses.astuple, aa), key=repr))
    ffl = sorted(((f.tx_id, f.kind, f.amount, f.account, f.asset, f.qty) for f in full.flows if f.date >= cutoff),
                 key=repr)
    afl = sorted(((f.tx_id, f.kind, f.amount, f.account, f.asset, f.qty) for f in active.flows), key=repr)
    add("flows", "Zahlungsströme ab Stichtag (eingesetztes Kapital)", ffl, afl)
    fi = sorted((i.code, i.tx_id) for i in full.issues if i.tx_id and tx_dates.get(i.tx_id, cutoff) >= cutoff)
    ai = sorted((i.code, i.tx_id) for i in active.issues if i.tx_id)
    # Hinweise zu Buchungen ab dem Stichtag: gleiche Meldungen (negativer Bestand wird nur einmal gemeldet)
    fi_set, ai_set = {x for x in fi if x[0] != "negative_balance"}, {x for x in ai if x[0] != "negative_balance"}
    out.append(Check("issues", "Meldungen des Ledgers zu Buchungen ab Stichtag", fi_set == ai_set,
                     "" if fi_set == ai_set else f"{len(fi_set ^ ai_set)} abweichend"))
    return out


def _invested_check(ctx: Any, m: Model) -> Check:
    """Eingesetztes Kapital ab Depotstart: Archiv (bis ``cutoff − 1``) + aktiver Stand == vollständige Berechnung."""
    from app.analytics.valuation import net_invested

    try:
        full = net_invested(m.led_full, m.pf_full, ctx.valuer)
        arch = net_invested(m.led_full, m.pf_full, ctx.valuer, until=m.cutoff - timedelta(days=1))
        act = net_invested(m.led_active, m.pf_active, ctx.valuer)
    except Exception as e:  # Prüfung darf die Seite nie sprengen, bleibt aber sichtbar
        log.warning("Stichtag: Prüfung des eingesetzten Kapitals nicht möglich: %s", e)
        return Check("invested", "eingesetztes Kapital ab Depotstart (Archiv + aktiv = vollständig)", False,
                     f"nicht berechenbar ({type(e).__name__})")
    ok = abs(full - (arch + act)) < 0.005
    return Check("invested", "eingesetztes Kapital ab Depotstart (Archiv + aktiv = vollständig)", ok,
                 "" if ok else f"Differenz {full - (arch + act):.2f} €")


def build(ctx: Any, cutoff: date, opts: Any = None, label: str = "") -> Model | None:
    """Modell zum Stichtag (gecacht): Eröffnungsstand, aktiver Stand, Gleichwertigkeitsprüfung – je Rechenweise
    (``opts``; Standard: App-Ansicht)."""
    base = dataclasses.replace(opts if opts is not None else ctx.engine_options(), snapshot_dates=())

    def make() -> Model | None:
        pf = ctx.portfolio()
        if pf is None:
            return None
        led_full, state = opening_state(ctx, pf, cutoff, base)
        pf_act = active_portfolio(pf, cutoff, state)
        led_act = run_ledger(pf_act, base)
        arch, act = split(pf, cutoff)
        m = Model(cutoff, base, pf, led_full, state, pf_act, led_act, len(arch), len(act), label=label)
        m.checks = compare(led_full, led_act, cutoff, {t.tx_id: t.date for t in pf.txs})
        m.checks.append(_invested_check(ctx, m))
        return m

    return ctx.derived("cutover.model", (cutoff.isoformat(), repr(base)), make)  # type: ignore[no-any-return]


def models(ctx: Any, cutoff: date) -> list[Model]:
    """Modelle für alle maßgeblichen Rechenweisen (Steuer zuerst)."""
    out = []
    for label, opts in option_sets(ctx, cutoff):
        m = build(ctx, cutoff, opts, label)
        if m is not None:
            out.append(m)
    return out


# -- Übersicht ----------------------------------------------------------------------------------------------------

def _is_fiat(pf: Portfolio, asset: str) -> bool:
    return pf.asset(asset).is_fiat


def overview(ctx: Any, m: Model) -> dict[str, Any]:
    """Eröffnungsstand je Konto und Asset samt Auffälligkeiten (rein lesend)."""
    pf = m.pf_full
    by: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for lot in m.state.lots:
        if lot.qty > DUST and not _is_fiat(pf, lot.asset):
            by[(lot.account, lot.asset)].append(lot)
    tx_by_id = {t.tx_id: t for t in pf.txs}
    rows: list[dict[str, Any]] = []
    zero_cost: list[dict[str, Any]] = []
    estimated: list[dict[str, Any]] = []
    for (acc, aid), lots in sorted(by.items()):
        qty = sum((x.qty for x in lots), ZERO)
        cost = sum((x.cost for x in lots), ZERO)
        bal = m.state.balances.get((acc, aid), ZERO)
        rows.append({"account": acc, "asset": aid, "lots": len(lots), "qty": qty, "balance": bal, "cost": cost,
                     "first": min(x.acq_date for x in lots), "last": max(x.acq_date for x in lots),
                     "lot_gap": bal - qty})
        for lot in lots:
            t = tx_by_id.get(lot.acq_tx)
            item = {"account": acc, "asset": aid, "qty": lot.qty, "cost": lot.cost, "date": lot.acq_date,
                    "tx": lot.acq_tx, "origin": lot.origin}
            if lot.origin == "phantom":
                zero_cost.append({**item, "why": "ohne Anschaffung (Fehlbestand/Übertrag ohne Herkunft)"})
            elif lot.cost <= 0:
                zero_cost.append({**item, "why": "Anschaffungskosten 0 €"})
            if t is not None and (t.source in ("reconstructed",) or t.flag in ("estimated", "reconstructed")
                                  or "Durchschnittskurs" in (t.note or "")):
                estimated.append({**item, "why": f"geschätzt/rekonstruiert ({t.source or t.flag})"})
    negatives = [{"account": a, "asset": x, "qty": q} for (a, x), q in sorted(m.state.balances.items())
                 if q < -DUST and not _is_fiat(pf, x)]
    arch, _act = split(pf, m.cutoff)
    corr = [t for t in arch if is_correction(t)]
    accounts = sorted({r["account"] for r in rows})
    return {
        "rows": rows, "zero_cost": zero_cost, "estimated": estimated, "negatives": negatives, "accounts": accounts,
        "n_lots": sum(r["lots"] for r in rows), "total_cost": sum((r["cost"] for r in rows), ZERO),
        "archive": {"txs": m.archive_txs, "corrections": len(corr), "active_txs": m.active_txs,
                    "first": min((t.date for t in arch), default=None),
                    "last": max((t.date for t in arch), default=None)},
        "crossing": crossing_transfers(m),
    }


def crossing_transfers(m: Model, days: int = 3) -> list[dict[str, Any]]:
    """Mögliche Übertragungen über den Stichtag: Abgang ohne Gegenbuchung kurz davor, Zugang ohne Gegenbuchung kurz
    danach, gleiches Asset, ähnliche Menge (± 2 %) – die Zugehörigkeit zur Berechnung bleibt dadurch unberührt."""
    c = m.cutoff
    outs = [f for f in m.led_full.flows if f.kind == "transfer_out_external" and c - timedelta(days=days) <= f.date < c]
    ins = [f for f in m.led_full.flows if f.kind == "transfer_in_external" and c <= f.date <= c + timedelta(days=days)]
    res = []
    for o in outs:
        for i in ins:
            if o.asset == i.asset and o.qty and i.qty and abs(abs(o.qty) - abs(i.qty)) <= abs(o.qty) * Decimal("0.02"):
                res.append({"asset": o.asset, "out_date": o.date, "out_account": o.account, "out_qty": abs(o.qty),
                            "in_date": i.date, "in_account": i.account, "in_qty": abs(i.qty)})
    return res


def reference_check(ctx: Any, m: Model) -> list[dict[str, Any]]:
    """Belege zum Ende von ``cutoff − 1`` (Referenzbestände, z. B. Depotauszüge) gegen den Eröffnungsstand."""
    d = (m.cutoff - timedelta(days=1)).isoformat()
    refs = ctx.db.q("SELECT account, asset_id, qty, source, note FROM reference_balance WHERE status='active' "
                    "AND as_of=? ORDER BY account, asset_id", (d,))
    out = []
    for r in refs:
        ref = Decimal(r["qty"])
        have = m.state.balances.get((r["account"], r["asset_id"]), ZERO)
        diff = ref - have
        out.append({"account": r["account"], "asset": r["asset_id"], "reference": ref, "opening": have, "diff": diff,
                    "ok": diff == 0, "dust": diff != 0 and abs(diff) <= STAUB_QTY, "source": r["source"],
                    "note": r["note"]})
    return out


def account_status(ctx: Any, m: Model, ov: dict[str, Any] | None = None,
                   refs: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Status je Konto für die Freigabe: ``belegt`` (alle Referenzen passen), ``abweichung`` (mindestens eine passt
    nicht), ``ohne_beleg`` (keine Referenz zum Stichtag). Dazu eine Prüfsumme des Eröffnungsstands je Konto."""
    import hashlib
    import json

    ov = ov or overview(ctx, m)
    refs = refs if refs is not None else reference_check(ctx, m)
    ref_by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in refs:
        ref_by[r["account"]].append(r)
    accounts = sorted(set(ov["accounts"]) | set(ref_by))
    out = []
    for acc in accounts:
        lots = sorted((x.asset, str(x.qty), str(x.cost), x.acq_date.isoformat(), x.acq_tx)
                       for x in m.state.lots if x.account == acc and x.qty > DUST and not _is_fiat(m.pf_full, x.asset))
        bal = sorted((a, str(q)) for (ac, a), q in m.state.balances.items()
                     if ac == acc and q and not _is_fiat(m.pf_full, a))
        fp = hashlib.sha256(json.dumps([lots, bal]).encode()).hexdigest()[:24]
        rs = ref_by.get(acc, [])
        bad = [r for r in rs if not r["ok"] and not r["dust"]]
        status = "abweichung" if bad else "belegt" if rs else "ohne_beleg"
        out.append({"account": acc, "status": status, "fingerprint": fp, "positions": sum(1 for r in ov["rows"]
                    if r["account"] == acc), "cost": sum((r["cost"] for r in ov["rows"] if r["account"] == acc), ZERO),
                    "refs": len(rs), "deviations": bad})
    return out


def tax_view(m: Model) -> list[dict[str, Any]]:
    """Realisierte Ergebnisse je Jahr ab dem Stichtag aus beiden Rechenwegen nebeneinander (Steuer-Prüfwert)."""
    def agg(led: LedgerResult, only_from: date | None) -> dict[int, tuple[int, Decimal, Decimal]]:
        out: dict[int, list[Any]] = defaultdict(lambda: [0, ZERO, ZERO])
        for d in led.disposals:
            if only_from and d.date < only_from:
                continue
            if m.pf_full.asset(d.asset).is_fiat:
                continue
            slot = out[d.date.year]
            slot[0] += 1
            slot[1] += d.proceeds
            slot[2] += d.cost
        return {y: (v[0], v[1], v[2]) for y, v in out.items()}

    f, a = agg(m.led_full, m.cutoff), agg(m.led_active, None)
    return [{"year": y, "full": f.get(y), "active": a.get(y), "equal": f.get(y) == a.get(y)}
            for y in sorted(set(f) | set(a))]


def impact_on_findings(ctx: Any, m: Model) -> dict[str, Any]:
    """Welche Diagnose-Befunde entfielen mit dem Stichtag? Hauptbefunde, deren Buchungen alle vor ``cutoff`` liegen,
    entfallen; Befunde ohne Buchungsbezug (Bestandsabweichungen u. ä.) bleiben. Rein lesend (volle Diagnose)."""
    from app.diagnosis.engine import report_for

    dates = {t.tx_id: t.date for t in m.pf_full.txs}
    split_: dict[str, int] = defaultdict(int)
    kinds: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for f in report_for(ctx).findings:
        if f.parent:
            continue
        ds = [dates[r.tx_id] for r in f.txs if getattr(r, "tx_id", None) in dates]
        zone = ("ohne_buchung" if not ds else "nur_ab_stichtag" if min(ds) >= m.cutoff
                else "nur_vor_stichtag" if max(ds) < m.cutoff else "beides")
        split_[zone] += 1
        kinds[zone][f"{f.kind}:{f.status}"] += 1
    return {"split": dict(split_), "kinds": {k: dict(v) for k, v in kinds.items()},
            "dropped": split_.get("nur_vor_stichtag", 0), "total": sum(split_.values())}
