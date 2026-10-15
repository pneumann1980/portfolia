"""Assets zusammenführen: dieselbe Anlage unter zwei Kennungen (z. B. ``PEAQ`` und ``PEAQ#29184678`` aus dem
Koinly-Import) wird auf **eine** Kennung abgebildet.

Warum nicht „Umstellung“ (:mod:`app.assetchange.service`)? Die Umstellung bucht eine Kapitalmaßnahme (Token-Migration,
Umtausch) und ist steuerlich eine eigene Bewertung. Hier sind es *dieselben* Einheiten – FIFO arbeitet aber je
Kennung: ein Verkauf von ``PEAQ`` kann Käufe von ``PEAQ#…`` nicht verbrauchen, es entstehen Scheinfälle „ohne
Anschaffungsdaten“ und aufgeteilte Bestände.

Die Zusammenführung ist deshalb eine **Zuordnung** (``asset_merge``): beim Aufbau des Portfolios werden alle Verweise
auf die Zwillingskennung durch das Haupt-Asset ersetzt. Keine Buchung wird erzeugt oder verändert, die Rohdaten des
Imports bleiben unberührt, Rückgängig löscht nur die Zuordnung. Weil Lots und damit FIFO/Steuer betroffen sind, gilt:
Vorschau (Bestände je Konto, FIFO-Wirkung je Steuerjahr, neue Auffälligkeiten) → ausdrückliche Bestätigung (Prüfsumme
des Datenstands) → Protokoll → Rückgängig. Kandidaten werden nur vorgeschlagen, nie automatisch zusammengeführt:
gleiche Kursquelle beweist keine Identität (Bridge-/Wrapped-Token, Forks).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import Any

from app.ledger.engine import run_ledger
from app.ledger.models import AssetInfo, Portfolio, Tx
from app.util.timeutil import iso, utcnow

log = logging.getLogger(__name__)
ZERO = Decimal(0)
SHORT_HOLD_DAYS = 365  # grobe Anzeige „Haltefrist ≤ 1 Jahr“ – maßgeblich bleibt der Steuerbericht


# ----------------------------------------------------------------------------------------------------
# Zuordnung (rein, ohne Web)
# ----------------------------------------------------------------------------------------------------

def active_map(db: Any) -> dict[str, str]:
    """Zwillingskennung → endgültiges Haupt-Asset (Ketten werden aufgelöst, Zyklen ignoriert)."""
    try:
        rows = db.q("SELECT alias_asset, target_asset FROM asset_merge WHERE status='active' ORDER BY id")
    except Exception:  # Tabelle fehlt (DB vor Migration 23)
        return {}
    direct = {r["alias_asset"]: r["target_asset"] for r in rows}
    out: dict[str, str] = {}
    for alias in direct:
        seen = {alias}
        cur = direct[alias]
        while cur in direct and cur not in seen:
            seen.add(cur)
            cur = direct[cur]
        if cur not in seen:
            out[alias] = cur
    return out


def canonical(db: Any, asset_id: str | None) -> str | None:
    if not asset_id:
        return asset_id
    return active_map(db).get(asset_id, asset_id)


def _map_tx(t: Tx, m: dict[str, str]) -> Tx:
    if not any(a in m for a in (t.from_asset, t.to_asset, t.fee_asset, t.related_asset)):
        return t
    return replace(t, from_asset=m.get(t.from_asset or "", t.from_asset), to_asset=m.get(t.to_asset or "", t.to_asset),
                   fee_asset=m.get(t.fee_asset or "", t.fee_asset),
                   related_asset=m.get(t.related_asset or "", t.related_asset))


def map_txs(txs: list[Tx], m: dict[str, str]) -> list[Tx]:
    return [_map_tx(t, m) for t in txs] if m else txs


def _merged_asset(target: AssetInfo, aliases: list[AssetInfo]) -> AssetInfo:
    """Haupt-Asset behält seine Stammdaten; fehlende Angaben und die Kennungen der Zwillinge kommen dazu (damit
    Importe, die die alte Kennung oder ihr Symbol nennen, beim Haupt-Asset landen)."""
    names = list(target.aliases)
    for a in aliases:
        for n in (a.asset_id, *a.aliases):
            if n and n != target.asset_id and n not in names:
                names.append(n)
    kw: dict[str, Any] = {"aliases": names, "extra": {**target.extra, "merged": [a.asset_id for a in aliases]}}
    for a in aliases:
        for f in ("wkn", "isin", "koinly_id"):
            if not getattr(target, f) and getattr(a, f):
                kw[f] = getattr(a, f)
        if target.quote_source in ("none", "", None) and a.quote_source not in ("none", "", None) and a.quote_id:
            kw.update(quote_source=a.quote_source, quote_id=a.quote_id)
    return replace(target, **kw)


def map_portfolio(pf: Portfolio, m: dict[str, str]) -> Portfolio:
    """Portfolio mit ersetzten Kennungen. Einträge, deren Haupt-Asset fehlt, werden nicht angewendet."""
    m = {a: t for a, t in m.items() if t in pf.assets}
    if not m:
        return pf
    by_target: dict[str, list[AssetInfo]] = defaultdict(list)
    for alias, target in m.items():
        if alias in pf.assets:
            by_target[target].append(pf.assets[alias])
    assets = {k: v for k, v in pf.assets.items() if k not in m}
    for target, aliases in by_target.items():
        assets[target] = _merged_asset(pf.assets[target], aliases)
    prices: dict[str, list[tuple[date, float]]] = {}
    for aid, lst in pf.manual_prices.items():
        prices.setdefault(m.get(aid, aid), []).extend(lst)
    for aid, lst in prices.items():
        seen: dict[date, float] = {}
        for d, v in lst:  # bei gleichem Tag gilt der erste (Haupt-Asset zuerst, siehe Reihenfolge unten)
            seen.setdefault(d, v)
        prices[aid] = sorted(seen.items())
    holdings = [{**h, "asset_id": m.get(h.get("asset_id"), h.get("asset_id"))} for h in pf.holdings_check]
    return replace(pf, txs=map_txs(pf.txs, m), assets=assets, manual_prices=prices, holdings_check=holdings)


def apply_merges(db: Any, pf: Portfolio | None) -> Portfolio | None:
    if pf is None:
        return pf
    return map_portfolio(pf, active_map(db))


# ----------------------------------------------------------------------------------------------------
# Kandidaten
# ----------------------------------------------------------------------------------------------------

@dataclass
class Candidate:
    alias: str
    target: str
    reasons: list[str]
    strength: str  # stark | mittel
    n_alias: int = 0
    n_target: int = 0
    notes: list[str] = field(default_factory=list)


def _tx_counts(pf: Portfolio) -> Counter[str]:
    n: Counter[str] = Counter()
    for t in pf.txs:
        for a in {t.from_asset, t.to_asset, t.fee_asset}:
            if a:
                n[a] += 1
    return n


def _linked_by_change(db: Any) -> set[frozenset[str]]:
    try:
        rows = db.q("SELECT old_asset, new_asset FROM asset_change WHERE kind='migration' AND status='applied'")
    except Exception:
        return set()
    return {frozenset((r["old_asset"], r["new_asset"])) for r in rows if r["old_asset"] and r["new_asset"]}


def candidates(ctx: Any) -> list[Candidate]:
    """Mögliche Zwillinge (nur Vorschläge): gleiche Kursquelle, gleiche ISIN/WKN, gleiche Basis-Kennung vor ``#``.
    Paare, die über eine gebuchte Umstellung verbunden sind, gehören bewusst zu verschiedenen Beständen."""
    pf = ctx.portfolio()
    if pf is None:
        return []
    n = _tx_counts(pf)
    skip = _linked_by_change(ctx.db)
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for a in pf.assets.values():
        if a.is_fiat or not n.get(a.asset_id):
            continue
        if a.quote_id and a.quote_source not in ("none", ""):
            groups[("quote", f"{a.quote_source}:{a.quote_id}")].append(a.asset_id)
        if a.isin:
            groups[("isin", a.isin.upper())].append(a.asset_id)
        if a.wkn and a.is_security:
            groups[("wkn", a.wkn.upper())].append(a.asset_id)
        if a.is_crypto:
            groups[("base", a.asset_id.split("#", 1)[0].upper())].append(a.asset_id)
    pairs: dict[frozenset[str], list[str]] = defaultdict(list)
    label = {"quote": "gleiche Kursquelle", "isin": "gleiche ISIN", "wkn": "gleiche WKN",
             "base": "gleiche Kennung vor „#“ (Koinly-Zusatz)"}
    for (kind, key), ids in groups.items():
        ids = sorted(set(ids))
        for i, x in enumerate(ids):
            for y in ids[i + 1:]:
                pair = frozenset((x, y))
                if pair in skip or pf.assets[x].asset_class != pf.assets[y].asset_class:
                    continue
                pairs[pair].append(f"{label[kind]} ({key})" if kind != "base" else f"{label[kind]}: {key}")
    out: list[Candidate] = []
    for pair, reasons in pairs.items():
        x, y = sorted(pair)
        # Haupt-Asset: ohne „#“, sonst mehr Buchungen, sonst kürzere Kennung
        target, alias = sorted((x, y), key=lambda a: ("#" in a, -n[a], len(a), a))
        strong = any(r.startswith(("gleiche ISIN", "gleiche WKN")) for r in reasons) or len(reasons) > 1
        out.append(Candidate(alias, target, reasons, "stark" if strong else "mittel", n[alias], n[target]))
    out.sort(key=lambda c: (c.strength != "stark", -(c.n_alias + c.n_target), c.alias))
    return out


# ----------------------------------------------------------------------------------------------------
# Vorschau
# ----------------------------------------------------------------------------------------------------

@dataclass
class BalanceRow:
    account: str
    alias_qty: Decimal
    target_qty: Decimal
    after_qty: Decimal


@dataclass
class YearRow:
    year: int
    parts_before: int = 0
    parts_after: int = 0
    missing_before: int = 0
    missing_after: int = 0
    short_before: int = 0
    short_after: int = 0
    gain_before: Decimal = ZERO
    gain_after: Decimal = ZERO

    @property
    def changed(self) -> bool:
        return (self.missing_before, self.short_before, self.gain_before) != (
            self.missing_after, self.short_after, self.gain_after)


@dataclass
class Preview:
    alias: str
    target: str
    alias_info: AssetInfo | None = None
    target_info: AssetInfo | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    n_alias_txs: int = 0
    n_same_asset: int = 0
    balances: list[BalanceRow] = field(default_factory=list)
    years: list[YearRow] = field(default_factory=list)
    issues_before: Counter[str] = field(default_factory=Counter)
    issues_after: Counter[str] = field(default_factory=Counter)
    fp: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors


def _year_stats(led: Any, assets: set[str]) -> dict[int, tuple[int, int, int, Decimal]]:
    out: dict[int, list[Any]] = defaultdict(lambda: [0, 0, 0, ZERO])
    for d in led.disposals:
        if d.asset not in assets:
            continue
        row = out[d.date.year]
        for p in d.parts:
            row[0] += 1
            row[1] += 1 if p.missing_basis else 0
            held = (d.date - p.acq_date).days if p.acq_date else None
            row[2] += 1 if (held is not None and held <= SHORT_HOLD_DAYS and not p.missing_basis) else 0
            row[3] += p.gain
    return {y: (v[0], v[1], v[2], v[3]) for y, v in out.items()}


def _issue_counts(led: Any, assets: set[str]) -> Counter[str]:
    return Counter(i.code for i in led.issues if i.asset in assets and i.severity in ("warning", "info"))


def preview(ctx: Any, alias: str, target: str) -> Preview:
    """Vorschau (rein lesend): rechnet das Ledger mit und ohne Zuordnung und vergleicht."""
    p = Preview(alias=(alias or "").strip(), target=(target or "").strip())
    pf = ctx.portfolio()
    led = ctx.ledger()
    if pf is None or led is None:
        p.errors.append("Noch keine Buchungen vorhanden.")
        return p
    a, t = pf.assets.get(p.alias), pf.assets.get(p.target)
    p.alias_info, p.target_info = a, t
    cur = active_map(ctx.db)
    if not p.alias or not p.target or p.alias == p.target:
        p.errors.append("Zwei verschiedene Assets wählen.")
    if a is None:
        p.errors.append(f"„{p.alias}“ ist nicht (mehr) als eigenes Asset vorhanden – bereits zusammengeführt?")
    if t is None:
        p.errors.append(f"„{p.target}“ ist nicht als Asset vorhanden.")
    if p.errors:
        return p
    assert a is not None and t is not None
    if a.is_fiat or t.is_fiat:
        p.errors.append("Währungen lassen sich nicht zusammenführen.")
    if a.asset_class != t.asset_class:
        p.errors.append(f"Verschiedene Klassen ({a.segment} / {t.segment}) – das sind keine Zwillinge.")
    if a.isin and t.isin and a.isin.upper() != t.isin.upper():
        p.errors.append(f"Verschiedene ISIN ({a.isin} / {t.isin}) – verschiedene Wertpapiere.")
    if cur.get(p.target) == p.alias:
        p.errors.append("Zirkuläre Zuordnung – das Ziel ist bereits auf diese Kennung abgebildet.")
    if p.errors:
        return p
    p.n_alias_txs = sum(1 for x in pf.txs if p.alias in (x.from_asset, x.to_asset, x.fee_asset, x.related_asset))
    if not p.n_alias_txs:
        p.warnings.append(f"Auf „{p.alias}“ gibt es keine Buchungen – die Zuordnung ändert nur die Stammdaten.")
    after_pf = map_portfolio(pf, {p.alias: p.target})
    after = run_ledger(after_pf, ctx.engine_options())
    p.n_same_asset = sum(1 for x in pf.txs if x.type in ("trade", "buy", "sell") and
                         {x.from_asset, x.to_asset} <= {p.alias, p.target} and x.from_asset != x.to_asset)
    if p.n_same_asset:
        p.warnings.append(f"{p.n_same_asset} Buchung(en) tauschen „{p.alias}“ gegen „{p.target}“ – nach der "
                          "Zusammenführung tauschen sie ein Asset gegen sich selbst. Bitte vorher prüfen "
                          "(vermutlich eine reine Umbenennung des Imports).")
    if a.quote_id and t.quote_id and (a.quote_source, a.quote_id) != (t.quote_source, t.quote_id):
        p.warnings.append(f"Verschiedene Kursquellen ({a.quote_source}:{a.quote_id} / {t.quote_source}:{t.quote_id}) "
                          "– künftig gilt die Kursquelle des Haupt-Assets.")
    elif a.quote_source in ("none", "", None) and t.quote_source in ("none", "", None):
        p.info.append("Keines der beiden Assets hat eine Kursquelle.")
    linked = _linked_by_change(ctx.db)
    if frozenset((p.alias, p.target)) in linked:
        p.warnings.append("Zwischen beiden Assets ist eine Umstellung (Token-Migration) gebucht – sie würde zu einem "
                          "Tausch gegen sich selbst. Eher keine Zwillinge.")
    names = {p.alias, p.target}
    accounts = sorted({acc for (acc, aid) in led.balances if aid in names} |
                      {acc for (acc, aid) in after.balances if aid in names})
    for acc in accounts:
        qa, qt = led.balances.get((acc, p.alias), ZERO), led.balances.get((acc, p.target), ZERO)
        p.balances.append(BalanceRow(acc, qa, qt, after.balances.get((acc, p.target), ZERO)))
    before_y, after_y = _year_stats(led, names), _year_stats(after, {p.target})
    for y in sorted(set(before_y) | set(after_y)):
        b, c = before_y.get(y, (0, 0, 0, ZERO)), after_y.get(y, (0, 0, 0, ZERO))
        p.years.append(YearRow(y, b[0], c[0], b[1], c[1], b[2], c[2], b[3], c[3]))
    p.issues_before, p.issues_after = _issue_counts(led, names), _issue_counts(after, {p.target})
    gone = sum((p.issues_before - p.issues_after).values())
    new = sum((p.issues_after - p.issues_before).values())
    if new:
        p.warnings.append(f"{new} neue Auffälligkeit(en) im Ledger nach der Zusammenführung (siehe Vergleich).")
    if gone:
        p.info.append(f"{gone} Auffälligkeit(en) des Ledgers verschwinden (z. B. fehlende Anschaffungsdaten).")
    fixed = sum(max(y.missing_before - y.missing_after, 0) for y in p.years)
    if fixed:
        p.info.append(f"{fixed} Veräußerungsteil(e) „ohne Anschaffungsdaten“ finden jetzt ihre Anschaffung.")
    tax_years = [y.year for y in p.years if y.changed]
    if tax_years:
        p.warnings.append("Steuerlich betroffene Jahre (FIFO-Zuordnung ändert sich): "
                          + ", ".join(str(y) for y in tax_years) + ". Abgegebene Steuererklärungen prüfen.")
    p.fp = fingerprint(p)
    return p


def fingerprint(p: Preview) -> str:
    """Prüfsumme des gesamten entscheidungsrelevanten Stands der Vorschau (Bestätigung nur für genau diesen Stand):
    Zahlen, Bestände, Jahreswerte, Auffälligkeiten sowie Warnungen und Hinweise."""
    data = [p.alias, p.target, p.n_alias_txs, p.n_same_asset,
            [(b.account, str(b.alias_qty), str(b.target_qty), str(b.after_qty)) for b in p.balances],
            [(y.year, y.parts_before, y.parts_after, y.missing_before, y.missing_after, y.short_before, y.short_after,
              str(y.gain_before), str(y.gain_after)) for y in p.years],
            sorted(p.issues_before.items()), sorted(p.issues_after.items()), p.errors, p.warnings, p.info]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:20]


# ----------------------------------------------------------------------------------------------------
# Übernehmen / Rückgängig
# ----------------------------------------------------------------------------------------------------

def _summary(p: Preview) -> dict[str, Any]:
    return {"txs": p.n_alias_txs, "accounts": len(p.balances), "years_changed": [y.year for y in p.years if y.changed],
            "warnings": p.warnings, "fp": p.fp}


def apply(ctx: Any, alias: str, target: str, fp: str, note: str = "") -> tuple[int | None, list[str]]:
    """Zuordnung anlegen – nur wenn der Datenstand der Vorschau (``fp``) noch gilt. Idempotent: dieselbe aktive
    Zuordnung wird nicht doppelt angelegt."""
    p = preview(ctx, alias, target)
    if not p.ok:
        existing = ctx.db.q1("SELECT id FROM asset_merge WHERE alias_asset=? AND target_asset=? AND status='active'",
                             (alias, target))
        return (int(existing["id"]), []) if existing is not None else (None, p.errors)
    if fp != p.fp:
        return None, ["Der Datenstand hat sich seit der Vorschau geändert – bitte erneut prüfen."]
    with ctx.db.transaction() as c:
        row = c.execute("SELECT id FROM asset_merge WHERE alias_asset=? AND status='active'", (p.alias,)).fetchone()
        if row is not None:
            return int(row["id"]), []
        cur = c.execute("INSERT INTO asset_merge(alias_asset, target_asset, status, note, summary_json, created_at) "
                        "VALUES (?,?, 'active', ?,?,?)", (p.alias, p.target, note[:200] or None,
                                                          json.dumps(_summary(p), ensure_ascii=False),
                                                          iso(utcnow())))
        mid = int(cur.lastrowid)  # type: ignore[arg-type]
    log.info("Asset zusammengeführt: %s → %s (Zuordnung %s, %s Buchungen)", p.alias, p.target, mid, p.n_alias_txs)
    ctx.invalidate_data()
    return mid, []


def revert(ctx: Any, mid: int) -> list[str]:
    row = ctx.db.q1("SELECT * FROM asset_merge WHERE id=?", (mid,))
    if row is None:
        return ["Zuordnung nicht gefunden."]
    if row["status"] != "active":
        return []  # bereits zurückgenommen (idempotent)
    ctx.db.x("UPDATE asset_merge SET status='reverted', reverted_at=? WHERE id=? AND status='active'",
             (iso(utcnow()), mid))
    log.info("Zusammenführung zurückgenommen: %s → %s (Zuordnung %s)", row["alias_asset"], row["target_asset"], mid)
    ctx.invalidate_data()
    return []


def history(db: Any, limit: int = 50) -> list[Any]:
    try:
        return db.q("SELECT * FROM asset_merge ORDER BY id DESC LIMIT ?", (limit,))
    except Exception:
        return []
