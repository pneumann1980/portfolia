"""Konto entfernen: alle Buchungen eines Kontos aus Bestand, Ledger und Steuer ausblenden – reversibel.

Wie beim Zusammenführen (:mod:`app.assetchange.account_merge`) wird nichts gelöscht: Eine Überlagerung
(``account_remove``) merkt sich die **Buchungskennungen**, die zum Zeitpunkt der Bestätigung zum Konto gehörten, und
blendet genau diese beim Aufbau des Portfolios aus. Rohdaten des Imports, Journal-Einträge und Datenquellen bleiben
unverändert; Rückgängig stellt alles wieder her. Später neu eintreffende Buchungen desselben Kontonamens sind nicht
betroffen (sie werden normal vorgelegt).

Nur Konten, deren Buchungen **ausschließlich dieses Konto** betreffen, lassen sich entfernen: Eine Buchung zwischen zwei
Konten (Transfer) zu verbergen, würde den Bestand des anderen Kontos still verändern. Dann zuerst zusammenführen oder
die Transfers prüfen. Vorschau (Bestände, FIFO/Steuer je Jahr, Auffälligkeiten, Datenquellen) → Bestätigung gegen eine
Prüfsumme → Protokoll → Rückgängig.
"""

from __future__ import annotations

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
# Überlagerung
# ----------------------------------------------------------------------------------------------------

def hidden_ids(db: Any) -> dict[str, set[str]]:
    """Konto → ausgeblendete Buchungskennungen (aktive Überlagerungen)."""
    out: dict[str, set[str]] = defaultdict(set)
    try:
        rows = db.q("SELECT account, tx_ids_json FROM account_remove WHERE status='active' ORDER BY id")
    except Exception:  # Tabelle fehlt (DB vor Migration 27)
        return {}
    for r in rows:
        try:
            out[r["account"]].update(str(x) for x in json.loads(r["tx_ids_json"] or "[]"))
        except ValueError:
            continue
    return out


def filter_txs(txs: list[Tx], ids: set[str]) -> list[Tx]:
    return [t for t in txs if t.tx_id not in ids] if ids else txs


def apply_removals(db: Any, pf: Portfolio | None) -> Portfolio | None:
    """Portfolio ohne die ausgeblendeten Buchungen; Konten ohne verbleibende Buchungen verlieren Stammdaten und
    Soll-Bestände (sie wären sonst „Geisterkonten“)."""
    if pf is None:
        return pf
    hid = hidden_ids(db)
    if not hid:
        return pf
    ids = set().union(*hid.values())
    txs = filter_txs(pf.txs, ids)
    if len(txs) == len(pf.txs):
        return pf
    used = {a for t in txs for a in (t.from_account, t.to_account) if a}
    gone = {a for a in hid if a not in used}
    accounts = {k: v for k, v in pf.accounts.items() if k not in gone}
    held = [h for h in pf.holdings_check if h.get("account") not in gone]
    return replace(pf, txs=txs, accounts=accounts, holdings_check=held)


# ----------------------------------------------------------------------------------------------------
# Vorschau
# ----------------------------------------------------------------------------------------------------

@dataclass
class BalanceRow:
    asset: str
    qty: Decimal


@dataclass
class Preview:
    account: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    tx_ids: list[str] = field(default_factory=list)
    first: Any = None
    last: Any = None
    origins: Counter[str] = field(default_factory=Counter)
    balances: list[BalanceRow] = field(default_factory=list)
    years: list[YearRow] = field(default_factory=list)
    issues_before: Counter[str] = field(default_factory=Counter)
    issues_after: Counter[str] = field(default_factory=Counter)
    sources: list[tuple[int, str, str]] = field(default_factory=list)  # (Kennung, Name, Anbieter)
    cross: list[str] = field(default_factory=list)
    fp: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def n_txs(self) -> int:
        return len(self.tx_ids)


def _sources(ctx: Any, account: str) -> list[tuple[int, str, str]]:
    from app.assetchange.account_merge import source_accounts

    return [(int(d.id), str(d.name), str(d.provider_label)) for d in source_accounts(ctx).get(account, [])]


def _origin(t: Tx) -> str:
    src = t.source or ""
    return ("Datei-Import" if src.startswith(("csv:", "doc:")) else "Abruf" if src.startswith("sync:")
            else "manuell erfasst" if t.origin == "journal" else "kuratierter Import")


def preview(ctx: Any, account: str) -> Preview:
    """Vorschau (rein lesend): rechnet das Ledger mit und ohne die Buchungen des Kontos."""
    p = Preview(account=(account or "").strip())
    pf = ctx.portfolio()
    led = ctx.ledger()
    p.sources = _sources(ctx, p.account)
    if pf is None or led is None:
        p.errors.append("Noch keine Buchungen vorhanden.")
        return p
    if not p.account:
        p.errors.append("Bitte ein Konto wählen.")
        return p
    mine = [t for t in pf.txs if p.account in (t.from_account, t.to_account)]
    p.tx_ids = sorted({t.tx_id for t in mine if t.origin != "plan"})
    plans = [t for t in mine if t.origin == "plan"]
    if not mine:
        known = set(pf.all_accounts())
        if p.account not in known and not p.sources:
            p.errors.append(f"Konto „{p.account}“ ist nicht (mehr) vorhanden – bereits entfernt oder zusammengeführt?")
        else:
            p.errors.append(f"Auf „{p.account}“ gibt es keine Buchungen – es gibt nichts auszublenden."
                            + (" Eine Datenquelle des Kontos lässt sich unten entfernen." if p.sources else ""))
        return p
    cross = [t for t in mine if {a for a in (t.from_account, t.to_account) if a} != {p.account}]
    p.cross = [f"{t.date:%d.%m.%Y} {t.type} {t.from_account or '–'} → {t.to_account or '–'} ({t.tx_id})"
               for t in cross[:8]]
    if cross:
        p.errors.append(f"{len(cross)} Buchung(en) bewegen Bestände zwischen „{p.account}“ und anderen Konten "
                        "(Transfers). Ausblenden würde den Bestand der anderen Konten still verändern – bitte "
                        "zuerst zusammenführen oder die Transfers in der Diagnose prüfen.")
    p.first, p.last = min(t.date for t in mine), max(t.date for t in mine)
    p.origins = Counter(_origin(t) for t in mine)
    if plans:
        p.info.append(f"{len(plans)} Sparplan-Schätzung(en) des Kontos bleiben bestehen (werden unter Sparpläne "
                      "verwaltet).")
    if p.sources:
        p.warnings.append("Datenquelle(n) des Kontos bleiben bestehen: " + ", ".join(f"{n} ({prov})" for _i, n, prov
                                                                                    in p.sources)
                          + ". Ein späterer Abruf legt neue Vorgänge wieder zur Prüfung vor – gegebenenfalls die "
                            "Datenquelle zusätzlich entfernen.")
    for key, label in (("tax.account_withholding", "Quellensteuer-Art"), ("ledger.cash_overrides", "Cash-Führung")):
        if p.account in (ctx.settings.get(key) or {}):
            p.info.append(f"Einstellung „{label}“ für „{p.account}“ bleibt gespeichert.")
    try:
        n_batches = ctx.db.scalar("SELECT COUNT(*) FROM csv_batch WHERE account=? AND status IN ('preview','partial')",
                                  (p.account,)) or 0
    except Exception:
        n_batches = 0
    if n_batches:
        p.warnings.append(f"{n_batches} offene(r) Prüf-Stapel für „{p.account}“ – bitte zuerst abschließen oder "
                          "verwerfen.")
    ids = set(p.tx_ids)
    after_pf = replace(pf, txs=filter_txs(pf.txs, ids))
    after = run_ledger(after_pf, ctx.engine_options())
    assets = {a for t in mine for a in (t.from_asset, t.to_asset, t.fee_asset) if a}
    for asset in sorted({k[1] for k in led.balances if k[0] == p.account and k[1] in assets}):
        q = led.balances.get((p.account, asset), ZERO)
        if q:
            p.balances.append(BalanceRow(asset, q))
    if p.balances:
        p.warnings.append("Das Konto enthält laut Buchungen noch Bestände – nach dem Ausblenden fehlen sie im "
                          "Portfolio (Spalte „Bestand“).")
    before_y, after_y = _year_stats(led, assets), _year_stats(after, assets)
    for y in sorted(set(before_y) | set(after_y)):
        b, c = before_y.get(y, (0, 0, 0, ZERO)), after_y.get(y, (0, 0, 0, ZERO))
        p.years.append(YearRow(y, b[0], c[0], b[1], c[1], b[2], c[2], b[3], c[3]))
    p.issues_before, p.issues_after = _issue_counts(led, assets), _issue_counts(after, assets)
    new = sum((p.issues_after - p.issues_before).values())
    if new:
        p.warnings.append(f"{new} neue Auffälligkeit(en) im Ledger nach dem Ausblenden (siehe Vergleich).")
    tax_years = [y.year for y in p.years if y.changed]
    if tax_years:
        p.warnings.append("Steuerlich betroffene Jahre (FIFO-Zuordnung ändert sich): "
                          + ", ".join(str(y) for y in tax_years) + ". Abgegebene Steuererklärungen prüfen.")
    p.fp = fingerprint(p)
    return p


def fingerprint(p: Preview) -> str:
    data = [p.account, p.tx_ids, [(b.asset, str(b.qty)) for b in p.balances],
            [(y.year, y.parts_before, y.parts_after, y.missing_before, y.missing_after, y.short_before, y.short_after,
              str(y.gain_before), str(y.gain_after)) for y in p.years],
            sorted(p.issues_before.items()), sorted(p.issues_after.items()), p.errors, p.warnings, p.info,
            [s[0] for s in p.sources]]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:20]


# ----------------------------------------------------------------------------------------------------
# Übernehmen / Rückgängig
# ----------------------------------------------------------------------------------------------------

def apply(ctx: Any, account: str, fp: str, note: str = "") -> tuple[int | None, list[str]]:
    """Buchungen des Kontos ausblenden – nur wenn der Datenstand der Vorschau (``fp``) noch gilt."""
    p = preview(ctx, account)
    if not p.ok:
        return None, p.errors
    if fp != p.fp:
        return None, ["Der Datenstand hat sich seit der Vorschau geändert – bitte erneut prüfen."]
    summary = {"txs": p.n_txs, "warnings": p.warnings, "fp": p.fp, "years_changed": [y.year for y in p.years
                                                                                     if y.changed]}
    with ctx.db.transaction() as c:
        cur = c.execute("INSERT INTO account_remove(account, status, tx_ids_json, note, summary_json, created_at) "
                        "VALUES (?, 'active', ?,?,?,?)",
                        (p.account, json.dumps(p.tx_ids), note[:200] or None,
                         json.dumps(summary, ensure_ascii=False), iso(utcnow())))
        rid = int(cur.lastrowid)  # type: ignore[arg-type]
    log.info("Konto entfernt (ausgeblendet): %s (%s Buchungen, Überlagerung %s)", p.account, p.n_txs, rid)
    ctx.invalidate_overlay()
    ctx.invalidate_data()
    return rid, []


def revert(ctx: Any, rid: int) -> list[str]:
    row = ctx.db.q1("SELECT * FROM account_remove WHERE id=?", (rid,))
    if row is None:
        return ["Eintrag nicht gefunden."]
    if row["status"] != "active":
        return []
    ctx.db.x("UPDATE account_remove SET status='reverted', reverted_at=? WHERE id=? AND status='active'",
             (iso(utcnow()), rid))
    log.info("Konto-Entfernung zurückgenommen: %s (Überlagerung %s)", row["account"], rid)
    ctx.invalidate_overlay()
    ctx.invalidate_data()
    return []


def history(db: Any, limit: int = 50) -> list[Any]:
    try:
        return db.q("SELECT id, account, status, note, created_at, reverted_at, summary_json FROM account_remove "
                    "ORDER BY id DESC LIMIT ?", (limit,))
    except Exception:
        return []
