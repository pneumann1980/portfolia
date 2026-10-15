"""Konten zusammenführen: dasselbe Konto bzw. Depot unter zwei Namen (z. B. „Wertpapiere Broker unbekannt“ aus dem
kuratierten Import und „DKB“ aus einer Datenquelle) wird auf **einen** Namen abgebildet.

Wie bei den Assets (:mod:`app.assetchange.merge`) ist das eine **Zuordnung** (``account_merge``), keine Buchung: Beim
Aufbau des Portfolios ersetzt sie in allen Buchungen den Alias-Kontonamen durch das Ziel-Konto. Rohdaten des Imports und
Journal-Einträge bleiben unverändert, Rückgängig löscht nur die Zuordnung. Bestände, Lots (bei Konto-bezogener FIFO),
Steuerwerte und Prüfungen ändern sich – deshalb: Vorschau (Bestände vorher/nachher, FIFO je Steuerjahr, mögliche
Doppelbuchungen, betroffene Datenquellen und Einstellungen) → Bestätigung gegen eine Prüfsumme → Protokoll → Rückgängig.

Gleichartige Buchungen in beiden Konten werden **nicht** automatisch als Dubletten entfernt: nach der Zusammenführung
erkennt sie die Diagnose wie gewohnt und legt sie zur Entscheidung vor (keine stille Veränderung von Beständen).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from app.assetchange.merge import YearRow, _issue_counts, _year_stats
from app.ledger.engine import run_ledger
from app.ledger.models import Portfolio, Tx
from app.util.timeutil import iso, utcnow

log = logging.getLogger(__name__)
ZERO = Decimal(0)


# ----------------------------------------------------------------------------------------------------
# Zuordnung (rein, ohne Web)
# ----------------------------------------------------------------------------------------------------

def active_map(db: Any) -> dict[str, str]:
    """Alias-Konto → endgültiges Ziel-Konto (Ketten werden aufgelöst, Zyklen ignoriert)."""
    try:
        rows = db.q("SELECT alias_account, target_account FROM account_merge WHERE status='active' ORDER BY id")
    except Exception:  # Tabelle fehlt (DB vor Migration 25)
        return {}
    direct = {r["alias_account"]: r["target_account"] for r in rows}
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


def canonical(db: Any, account: str | None) -> str | None:
    if not account:
        return account
    return active_map(db).get(account, account)


def map_txs(txs: list[Tx], m: dict[str, str]) -> list[Tx]:
    if not m:
        return txs
    out = []
    for t in txs:
        if t.from_account in m or t.to_account in m:
            t = replace(t, from_account=m.get(t.from_account or "", t.from_account),
                        to_account=m.get(t.to_account or "", t.to_account))
        out.append(t)
    return out


def map_portfolio(pf: Portfolio, m: dict[str, str]) -> Portfolio:
    """Portfolio mit ersetzten Kontonamen. Alias und Ziel müssen verschieden sein; Einträge ohne Wirkung entfallen."""
    m = {a: t for a, t in m.items() if a != t}
    if not m:
        return pf
    accounts = {name: info for name, info in pf.accounts.items() if name not in m}
    aliases: dict[str, list[str]] = defaultdict(list)
    for name in pf.accounts:
        if name in m:
            aliases[m[name]].append(name)
    for tgt, names in aliases.items():  # unabhängig von der Reihenfolge: Ziel zuerst, Aliase (sortiert) füllen Lücken
        base = accounts.get(tgt)
        for name in sorted(names):
            info = pf.accounts[name]
            if base is None:
                base = replace(info, account=tgt)
            else:
                base = replace(base, broker=base.broker or info.broker,
                               depot_group=base.depot_group or info.depot_group,
                               extra={**info.extra, **base.extra})
        assert base is not None
        accounts[tgt] = base
    # Soll-Bestände des kuratierten Imports: zwei Konten zu einem → Mengen je Asset addieren
    held: dict[tuple[Any, Any], dict[str, Any]] = {}
    for h in pf.holdings_check:
        key = (m.get(h.get("account"), h.get("account")), h.get("asset_id"))
        if key in held:
            prev = held[key]
            with contextlib.suppress(Exception):  # unlesbare Menge: den ersten Wert behalten, nicht raten
                held[key] = {**prev, "qty": Decimal(str(prev["qty"])) + Decimal(str(h["qty"]))}
        else:
            held[key] = {**h, "account": key[0]}
    return replace(pf, txs=map_txs(pf.txs, m), accounts=accounts, holdings_check=list(held.values()))


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
    n_alias: int = 0
    n_target: int = 0


def _counts(pf: Portfolio) -> Counter[str]:
    n: Counter[str] = Counter()
    for t in pf.txs:
        for a in {t.from_account, t.to_account}:
            if a:
                n[a] += 1
    return n


def _sig(t: Tx) -> tuple[Any, ...]:
    return (t.ts, t.type, t.from_asset, str(t.from_qty), t.to_asset, str(t.to_qty))


def candidates(ctx: Any) -> list[Candidate]:
    """Mögliche Zwillingskonten (nur Vorschläge): Namen, die bis auf Groß-/Kleinschreibung, Leerzeichen und Satzzeichen
    gleich sind, ein Name, der den anderen enthält, oder eine auffällige Menge identischer Buchungen in beiden Konten.
    Der Platzhalter „… unbekannt“ (Broker nicht bekannt) wird gegenüber benannten Konten als Alias vorgeschlagen."""
    pf = ctx.portfolio()
    if pf is None:
        return []
    n = _counts(pf)
    names = sorted(n)
    by_acc: dict[str, set[tuple[Any, ...]]] = defaultdict(set)
    for t in pf.txs:
        for a in {t.from_account, t.to_account}:
            if a:
                by_acc[a].add(_sig(t))

    def norm(s: str) -> str:
        return "".join(ch for ch in s.casefold() if ch.isalnum())

    out: list[Candidate] = []
    for i, x in enumerate(names):
        for y in names[i + 1:]:
            reasons = []
            nx, ny = norm(x), norm(y)
            if nx and nx == ny:
                reasons.append("Name gleich bis auf Schreibweise")
            elif nx and ny and (nx in ny or ny in nx) and min(len(nx), len(ny)) >= 3:
                reasons.append("ein Name enthält den anderen")
            common = len(by_acc[x] & by_acc[y])
            if common >= 3:
                reasons.append(f"{common} Buchungen mit identischem Zeitpunkt, Asset und Menge in beiden Konten")
            if not reasons and ("unbekannt" in nx or "unbekannt" in ny) and n[x] and n[y]:
                reasons.append("Platzhalter „unbekannt“ – Konto könnte ein bekanntes sein")
            if not reasons:
                continue
            # Alias = Platzhalter, sonst das kleinere Konto
            ranked = sorted((x, y), key=lambda a: ("unbekannt" not in norm(a), n[a], a))
            alias, target = ranked[0], ranked[1]
            if "unbekannt" not in norm(alias):
                alias, target = sorted((x, y), key=lambda a: (n[a], a))
            out.append(Candidate(alias, target, reasons, n[alias], n[target]))
    out.sort(key=lambda c: (-len(c.reasons), -(c.n_alias + c.n_target), c.alias))
    return out


# ----------------------------------------------------------------------------------------------------
# Vorschau
# ----------------------------------------------------------------------------------------------------

@dataclass
class BalanceRow:
    asset: str
    alias_qty: Decimal
    target_qty: Decimal
    after_qty: Decimal


@dataclass
class Preview:
    alias: str
    target: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    n_alias_txs: int = 0
    n_target_txs: int = 0
    n_between: int = 0
    n_twins: int = 0
    balances: list[BalanceRow] = field(default_factory=list)
    years: list[YearRow] = field(default_factory=list)
    issues_before: Counter[str] = field(default_factory=Counter)
    issues_after: Counter[str] = field(default_factory=Counter)
    sources: list[str] = field(default_factory=list)
    fp: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors


def _datasources(ctx: Any, account: str) -> list[str]:
    try:
        return [f"{r['name']} ({r['provider']})" for r in ctx.db.q(
            "SELECT name, provider FROM data_source WHERE account=? COLLATE NOCASE ORDER BY name", (account,))]
    except Exception:
        return []


def preview(ctx: Any, alias: str, target: str) -> Preview:
    """Vorschau (rein lesend): rechnet das Ledger mit und ohne Zuordnung und vergleicht."""
    p = Preview(alias=(alias or "").strip(), target=(target or "").strip())
    pf = ctx.portfolio()
    led = ctx.ledger()
    if pf is None or led is None:
        p.errors.append("Noch keine Buchungen vorhanden.")
        return p
    known = set(pf.all_accounts())
    cur = active_map(ctx.db)
    if not p.alias or not p.target or p.alias == p.target:
        p.errors.append("Zwei verschiedene Konten wählen.")
    for name in (p.alias, p.target):
        if name and name not in known:
            p.errors.append(f"Konto „{name}“ ist nicht (mehr) vorhanden – bereits zusammengeführt?")
    if cur.get(p.target) == p.alias:
        p.errors.append("Zirkuläre Zuordnung – das Ziel ist bereits auf dieses Konto abgebildet.")
    if p.errors:
        return p
    mine = [t for t in pf.txs if p.alias in (t.from_account, t.to_account)]
    theirs = [t for t in pf.txs if p.target in (t.from_account, t.to_account)]
    p.n_alias_txs, p.n_target_txs = len(mine), len(theirs)
    p.n_between = sum(1 for t in mine if {t.from_account, t.to_account} == {p.alias, p.target})
    ids_mine, ids_theirs = {t.tx_id for t in mine}, {t.tx_id for t in theirs}
    only_mine = [t for t in mine if t.tx_id not in ids_theirs]
    twin_sigs = {_sig(t) for t in theirs if t.tx_id not in ids_mine}
    p.n_twins = sum(1 for t in only_mine if _sig(t) in twin_sigs)
    if not mine:
        p.warnings.append(f"Auf „{p.alias}“ gibt es keine Buchungen – die Zuordnung ändert nur den Kontonamen.")
    if p.n_between:
        p.warnings.append(f"{p.n_between} Buchung(en) bewegen Bestände zwischen „{p.alias}“ und „{p.target}“ – nach "
                          "der Zusammenführung sind es Bewegungen innerhalb eines Kontos.")
    if p.n_twins:
        p.warnings.append(f"{p.n_twins} Buchung(en) von „{p.alias}“ haben im Ziel-Konto eine identische Entsprechung "
                          "(Zeitpunkt, Art, Asset, Menge). Wurde dasselbe Depot zweimal importiert, entstehen nach der "
                          "Zusammenführung Dubletten – die Diagnose legt sie zur Entscheidung vor, Portfolia entfernt "
                          "nichts selbst.")
    p.sources = [*(f"{s} → {p.alias}" for s in _datasources(ctx, p.alias)),
                 *(f"{s} → {p.target}" for s in _datasources(ctx, p.target))]
    if _datasources(ctx, p.alias) and _datasources(ctx, p.target):
        p.warnings.append("Beide Konten haben eine Datenquelle – nach der Zusammenführung bitte auch die Datenquellen "
                          "zusammenführen (Einstellungen → Datenquellen).")
    elif _datasources(ctx, p.alias):
        p.info.append(f"Die Datenquelle von „{p.alias}“ gilt künftig für „{p.target}“.")
    ai, ti = pf.accounts.get(p.alias), pf.accounts.get(p.target)
    if ai and ti and (ai.broker, ai.depot_group) != (ti.broker, ti.depot_group) and (ai.broker or ai.depot_group):
        p.warnings.append(f"Unterschiedliche Stammdaten: „{p.alias}“ Broker={ai.broker or '–'}, Depotgruppe="
                          f"{ai.depot_group or '–'}; „{p.target}“ Broker={ti.broker or '–'}, Depotgruppe="
                          f"{ti.depot_group or '–'}. Es gelten die Angaben des Ziel-Kontos.")
    s = ctx.settings
    for key, label in (("tax.account_withholding", "Quellensteuer-Art"), ("ledger.cash_overrides", "Cash-Führung")):
        d = s.get(key) or {}
        if p.alias in d and (p.target not in d or d[p.alias] != d.get(p.target)):
            p.warnings.append(f"Einstellung „{label}“ ist nur für „{p.alias}“ gesetzt – künftig gilt die Einstellung "
                              f"von „{p.target}“ (bzw. der Standard).")
    try:
        n_batches = ctx.db.scalar("SELECT COUNT(*) FROM csv_batch WHERE account=? AND status IN ('preview','partial')",
                                  (p.alias,)) or 0
    except Exception:
        n_batches = 0
    if n_batches:
        p.warnings.append(f"{n_batches} offene(r) Prüf-Stapel für „{p.alias}“ – sie werden weiter unter diesem Namen "
                          "abgeglichen; bitte zuerst abschließen.")
    after_pf = map_portfolio(pf, {p.alias: p.target})
    after = run_ledger(after_pf, ctx.engine_options())
    assets = {a for t in [*mine, *theirs] for a in (t.from_asset, t.to_asset, t.fee_asset) if a}
    keys = {k for k in [*led.balances, *after.balances] if k[0] in (p.alias, p.target) and k[1] in assets}
    for asset in sorted({k[1] for k in keys}):
        qa, qt = led.balances.get((p.alias, asset), ZERO), led.balances.get((p.target, asset), ZERO)
        p.balances.append(BalanceRow(asset, qa, qt, after.balances.get((p.target, asset), ZERO)))
    before_y, after_y = _year_stats(led, assets), _year_stats(after, assets)
    for y in sorted(set(before_y) | set(after_y)):
        b, c = before_y.get(y, (0, 0, 0, ZERO)), after_y.get(y, (0, 0, 0, ZERO))
        p.years.append(YearRow(y, b[0], c[0], b[1], c[1], b[2], c[2], b[3], c[3]))
    p.issues_before, p.issues_after = _issue_counts(led, assets), _issue_counts(after, assets)
    new = sum((p.issues_after - p.issues_before).values())
    gone = sum((p.issues_before - p.issues_after).values())
    if new:
        p.warnings.append(f"{new} neue Auffälligkeit(en) im Ledger nach der Zusammenführung (siehe Vergleich).")
    if gone:
        p.info.append(f"{gone} Auffälligkeit(en) des Ledgers verschwinden (z. B. fehlende Anschaffungsdaten).")
    tax_years = [y.year for y in p.years if y.changed]
    if tax_years:
        p.warnings.append("Steuerlich betroffene Jahre (FIFO-Zuordnung ändert sich): "
                          + ", ".join(str(y) for y in tax_years) + ". Abgegebene Steuererklärungen prüfen.")
    elif ctx.engine_options().scope != "account":
        p.info.append("FIFO läuft derzeit über alle Konten gemeinsam – die Steuerwerte ändern sich nicht.")
    p.fp = fingerprint(p)
    return p


def fingerprint(p: Preview) -> str:
    """Prüfsumme des gesamten entscheidungsrelevanten Stands der Vorschau: Buchungszahlen, Bestände, Jahreswerte,
    Auffälligkeiten, Warnungen/Hinweise und betroffene Datenquellen – ändert sich eines davon, ist neu zu bestätigen."""
    data = [p.alias, p.target, p.n_alias_txs, p.n_target_txs, p.n_between, p.n_twins,
            [(b.asset, str(b.alias_qty), str(b.target_qty), str(b.after_qty)) for b in p.balances],
            [(y.year, y.parts_before, y.parts_after, y.missing_before, y.missing_after, y.short_before, y.short_after,
              str(y.gain_before), str(y.gain_after)) for y in p.years],
            sorted(p.issues_before.items()), sorted(p.issues_after.items()),
            p.errors, p.warnings, p.info, p.sources]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:20]


# ----------------------------------------------------------------------------------------------------
# Übernehmen / Rückgängig
# ----------------------------------------------------------------------------------------------------

def apply(ctx: Any, alias: str, target: str, fp: str, note: str = "") -> tuple[int | None, list[str]]:
    """Zuordnung anlegen – nur wenn der Datenstand der Vorschau (``fp``) noch gilt. Idempotent."""
    p = preview(ctx, alias, target)
    if not p.ok:
        existing = ctx.db.q1("SELECT id FROM account_merge WHERE alias_account=? AND target_account=? AND "
                             "status='active'", (alias, target))
        return (int(existing["id"]), []) if existing is not None else (None, p.errors)
    if fp != p.fp:
        return None, ["Der Datenstand hat sich seit der Vorschau geändert – bitte erneut prüfen."]
    summary = {"txs": p.n_alias_txs, "twins": p.n_twins, "between": p.n_between, "warnings": p.warnings, "fp": p.fp,
               "years_changed": [y.year for y in p.years if y.changed]}
    with ctx.db.transaction() as c:
        row = c.execute("SELECT id FROM account_merge WHERE alias_account=? AND status='active'",
                        (p.alias,)).fetchone()
        if row is not None:
            return int(row["id"]), []
        cur = c.execute("INSERT INTO account_merge(alias_account, target_account, status, note, summary_json, "
                        "created_at) VALUES (?,?, 'active', ?,?,?)",
                        (p.alias, p.target, note[:200] or None, json.dumps(summary, ensure_ascii=False),
                         iso(utcnow())))
        mid = int(cur.lastrowid)  # type: ignore[arg-type]
    log.info("Konto zusammengeführt: %s → %s (Zuordnung %s, %s Buchungen)", p.alias, p.target, mid, p.n_alias_txs)
    ctx.invalidate_overlay()
    ctx.invalidate_data()
    return mid, []


def revert(ctx: Any, mid: int) -> list[str]:
    row = ctx.db.q1("SELECT * FROM account_merge WHERE id=?", (mid,))
    if row is None:
        return ["Zuordnung nicht gefunden."]
    if row["status"] != "active":
        return []
    ctx.db.x("UPDATE account_merge SET status='reverted', reverted_at=? WHERE id=? AND status='active'",
             (iso(utcnow()), mid))
    log.info("Konto-Zusammenführung zurückgenommen: %s → %s (Zuordnung %s)", row["alias_account"],
             row["target_account"], mid)
    ctx.invalidate_overlay()
    ctx.invalidate_data()
    return []


def history(db: Any, limit: int = 50) -> list[Any]:
    try:
        return db.q("SELECT * FROM account_merge ORDER BY id DESC LIMIT ?", (limit,))
    except Exception:
        return []
