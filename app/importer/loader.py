"""Import in die Datenbank: atomar, versioniert (import_id), Diff zur Vorversion, Soll-Ist-Abgleich."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.db import Database
from app.importer.validate import ParsedImport, Report, sha256_file, validate_zip
from app.ledger.engine import DUST, EngineOptions, LedgerResult, run_ledger
from app.ledger.models import AccountInfo, AssetInfo, Portfolio, Tx
from app.util.timeutil import iso

log = logging.getLogger(__name__)

KEEP_IMPORTS = 10
MIN_FILE_AGE_S = 15
_import_lock = threading.Lock()


@dataclass
class ImportOutcome:
    status: str  # imported | failed | unchanged | none | busy | pending
    message: str
    import_id: int | None = None
    filename: str | None = None
    report: dict[str, Any] | None = None
    diff: dict[str, Any] | None = None
    check: dict[str, Any] | None = None
    duration_ms: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _dec_str(d: Decimal | None) -> str | None:
    if d is None:
        return None
    return format(d.normalize(), "f") if d != 0 else "0"


# ----------------------------------------------------------------------------------------------
# Portfolio laden
# ----------------------------------------------------------------------------------------------

def portfolio_from_parsed(p: ParsedImport, import_id: int | None = None) -> Portfolio:
    from datetime import date

    manual: dict[str, list[tuple[date, float]]] = {}
    for m in p.manual_prices:
        manual.setdefault(m["asset_id"], []).append((date.fromisoformat(m["date"]), float(m["price_eur"])))
    for v in manual.values():
        v.sort()
    return Portfolio(
        import_id=import_id,
        txs=[Tx.from_parsed(r) for r in p.transactions],
        assets={a["asset_id"]: AssetInfo.from_parsed(a) for a in p.assets},
        accounts={a["account"]: AccountInfo.from_parsed(a) for a in p.accounts},
        manual_prices=manual,
        holdings_check=[{"asset_id": h["asset_id"], "account": h["account"], "qty": h["qty"],
                         "extra": h.get("extra", {})} for h in p.holdings_check],
        valuation_date=date.fromisoformat(str(p.manifest["valuation_date"])),
    )


def portfolio_from_db(db: Database, import_id: int) -> Portfolio:
    from datetime import date

    txs = [Tx.from_row(r) for r in db.q("SELECT * FROM tx WHERE import_id=? ORDER BY seq", (import_id,))]
    assets = {r["asset_id"]: AssetInfo.from_row(r)
              for r in db.q("SELECT * FROM assets WHERE import_id=?", (import_id,))}
    accounts = {r["account"]: AccountInfo.from_row(r)
                for r in db.q("SELECT * FROM accounts WHERE import_id=?", (import_id,))}
    manual: dict[str, list[tuple[date, float]]] = {}
    for r in db.q("SELECT asset_id, date, price_eur FROM manual_prices WHERE import_id=? ORDER BY date", (import_id,)):
        manual.setdefault(r["asset_id"], []).append((date.fromisoformat(r["date"]), float(r["price_eur"])))
    hc = [{"asset_id": r["asset_id"], "account": r["account"], "qty": Decimal(r["qty"]),
           "extra": json.loads(r["extra_json"] or "{}")}
          for r in db.q("SELECT * FROM holdings_check WHERE import_id=? ORDER BY seq", (import_id,))]
    vd = db.scalar("SELECT valuation_date FROM imports WHERE id=?", (import_id,))
    return Portfolio(import_id=import_id, txs=txs, assets=assets, accounts=accounts, manual_prices=manual,
                     holdings_check=hc, valuation_date=date.fromisoformat(vd) if vd else None)


def active_import_id(db: Database) -> int | None:
    v = db.get_state("active_import_id")
    return int(v) if v is not None else None


# ----------------------------------------------------------------------------------------------
# Diff & Abgleich
# ----------------------------------------------------------------------------------------------

def _raw_map_db(db: Database, import_id: int) -> dict[str, tuple[str, dict[str, str]]]:
    return {r["tx_id"]: (r["row_hash"], json.loads(r["raw_json"]))
            for r in db.q("SELECT tx_id, row_hash, raw_json FROM tx WHERE import_id=?", (import_id,))}


def compute_diff(old_raw: dict[str, tuple[str, dict[str, str]]] | None, new: ParsedImport,
                 old_hold: dict[str, Decimal] | None, new_hold: dict[str, Decimal],
                 assets: dict[str, AssetInfo], limit: int = 500) -> dict[str, Any]:
    if old_raw is None:
        return {"first_import": True, "new": len(new.transactions), "changed": 0, "removed": 0,
                "new_list": [], "changed_list": [], "removed_list": [], "holdings": []}
    new_map = {t["tx_id"]: t for t in new.transactions}
    added = [tid for tid in new_map if tid not in old_raw]
    removed = [tid for tid in old_raw if tid not in new_map]
    changed = []
    for tid, t in new_map.items():
        if tid in old_raw and old_raw[tid][0] != t["row_hash"]:
            old = old_raw[tid][1]
            fields = []
            for k in sorted(set(old) | set(t["raw"])):
                ov, nv = (old.get(k) or "").strip(), (t["raw"].get(k) or "").strip()
                if ov != nv:
                    fields.append({"field": k, "old": ov, "new": nv})
            changed.append({"tx_id": tid, "fields": fields})

    def brief(t: dict[str, Any]) -> dict[str, Any]:
        return {"tx_id": t["tx_id"], "datetime": iso(t["ts_utc"]), "type": t["type"], "tag": t["tag"],
                "from": f'{_dec_str(t["from_qty"]) or ""} {t["from_asset"] or ""}'.strip(),
                "to": f'{_dec_str(t["to_qty"]) or ""} {t["to_asset"] or ""}'.strip()}

    holdings = []
    old_hold = old_hold or {}
    for a in sorted(set(old_hold) | set(new_hold)):
        o, n = old_hold.get(a, Decimal(0)), new_hold.get(a, Decimal(0))
        if abs(n - o) > DUST:
            holdings.append({"asset_id": a, "name": assets[a].name if a in assets else a,
                             "old": _dec_str(o), "new": _dec_str(n), "delta": _dec_str(n - o)})
    return {
        "first_import": False,
        "new": len(added), "changed": len(changed), "removed": len(removed),
        "new_list": [brief(new_map[t]) for t in added[:limit]],
        "changed_list": changed[:limit],
        "removed_list": [{"tx_id": t, **{k: old_raw[t][1].get(k) for k in ("datetime", "type", "from_asset",
                                                                              "to_asset")}} for t in removed[:limit]],
        "holdings": holdings,
    }


def check_holdings(result: LedgerResult, pf: Portfolio) -> dict[str, Any]:
    """Abgleich Ledger-Bestand vs. holdings_check (nur Warnungen, keine Übernahme)."""
    rows = pf.holdings_check
    by_asset = result.holdings_by_asset()
    deviations = []
    ok = 0
    checked_assets: set[str] = set()
    for r in rows:
        asset, account, expected = r["asset_id"], r.get("account"), Decimal(r["qty"])
        checked_assets.add(asset)
        if account:
            actual = result.balances.get((account, asset), Decimal(0))
        else:
            actual = by_asset.get(asset, Decimal(0))
        diff = actual - expected
        tol = max(Decimal("1e-8"), abs(expected) * Decimal("1e-6"))
        if abs(diff) <= tol:
            ok += 1
            continue
        name = pf.assets[asset].name if asset in pf.assets else asset
        deviations.append({"asset_id": asset, "name": name, "account": account, "expected": _dec_str(expected),
                           "actual": _dec_str(actual), "diff": _dec_str(diff)})
    missing = []
    if rows:
        for a, q in sorted(by_asset.items()):
            if a not in checked_assets and a in pf.assets and not pf.assets[a].is_fiat:
                missing.append({"asset_id": a, "name": pf.assets[a].name, "actual": _dec_str(q)})
    return {"checked": len(rows), "ok": ok, "deviations": deviations, "not_in_check": missing}


# ----------------------------------------------------------------------------------------------
# Import
# ----------------------------------------------------------------------------------------------

def find_candidate(import_dir: Path) -> tuple[Path | None, str | None]:
    """Neueste ZIP-Datei (mtime, dann Name). Zu junge Dateien (werden evtl. noch kopiert) → pending."""
    if not import_dir.exists():
        return None, f"Importverzeichnis {import_dir} existiert nicht"
    zips = [p for p in import_dir.iterdir()
            if p.is_file() and p.suffix.lower() == ".zip" and not p.name.startswith(".")]
    if not zips:
        return None, "Keine ZIP-Datei im Importverzeichnis"
    zips.sort(key=lambda p: (p.stat().st_mtime, p.name))
    newest = zips[-1]
    if time.time() - newest.stat().st_mtime < MIN_FILE_AGE_S:
        return newest, "pending"
    return newest, None


def _store(db: Database, path: Path, parsed: ParsedImport, report: Report, diff: dict[str, Any],
           check: dict[str, Any], duration_ms: int, trigger: str) -> int:
    now = iso(datetime.now(UTC))
    st = path.stat()
    with db.transaction() as c:
        cur = c.execute(
            """INSERT INTO imports(filename, file_sha256, file_size, file_mtime, processed_at, status, schema_version,
                   generated_at, valuation_date, notes, counts_json, report_json, diff_json, check_json, duration_ms,
                   trigger)
               VALUES (?,?,?,?,?, 'importing', ?,?,?,?,?,?,?,?,?,?)""",
            (path.name, parsed.file_sha256, st.st_size, iso(datetime.fromtimestamp(st.st_mtime, UTC)), now,
             str(parsed.manifest.get("schema_version")), str(parsed.manifest.get("generated_at")),
             str(parsed.manifest.get("valuation_date")), str(parsed.manifest.get("notes") or "") or None,
             json.dumps(parsed.counts), report.to_json(), json.dumps(diff, ensure_ascii=False),
             json.dumps(check, ensure_ascii=False), duration_ms, trigger),
        )
        iid = cur.lastrowid
        c.executemany(
            """INSERT INTO tx(import_id, seq, tx_id, ts_utc, date_local, date_only, type, tag, from_account, from_asset,
                   from_qty, to_account, to_asset, to_qty, fee_asset, fee_qty, fee_eur, value_eur, orig_price, orig_ccy,
                   source, source_ref, flag, note, related_asset, row_hash, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(iid, t["seq"], t["tx_id"], iso(t["ts_utc"]), t["date_local"].isoformat(), int(t["date_only"]), t["type"],
              t["tag"], t["from_account"], t["from_asset"], _dec_str(t["from_qty"]), t["to_account"], t["to_asset"],
              _dec_str(t["to_qty"]), t["fee_asset"], _dec_str(t["fee_qty"]), _dec_str(t["fee_eur"]),
              _dec_str(t["value_eur"]), t["orig_price"], t["orig_ccy"], t["source"], t["source_ref"], t["flag"],
              t["note"], t["related_asset"], t["row_hash"], json.dumps(t["raw"], ensure_ascii=False))
             for t in parsed.transactions],
        )
        c.executemany(
            """INSERT INTO assets(import_id, asset_id, name, asset_class, wkn, isin, koinly_id, quote_source, quote_id,
                   status, note, aliases, category, extra_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(iid, a["asset_id"], a["name"], a["asset_class"], a["wkn"], a["isin"], a["koinly_id"], a["quote_source"],
              a["quote_id"], a["status"], a["note"], a["aliases"], a["category"], json.dumps(a["extra"]))
             for a in parsed.assets],
        )
        c.executemany(
            "INSERT INTO accounts(import_id, account, broker, depot_group, extra_json) VALUES (?,?,?,?,?)",
            [(iid, a["account"], a["broker"], a["depot_group"], json.dumps(a["extra"])) for a in parsed.accounts],
        )
        c.executemany(
            "INSERT INTO holdings_check(import_id, seq, asset_id, account, qty, extra_json) VALUES (?,?,?,?,?,?)",
            [(iid, h["seq"], h["asset_id"], h["account"], _dec_str(h["qty"]), json.dumps(h["extra"]))
             for h in parsed.holdings_check],
        )
        c.executemany(
            "INSERT INTO issues(import_id, seq, data_json) VALUES (?,?,?)",
            [(iid, i, json.dumps(row, ensure_ascii=False)) for i, row in enumerate(parsed.issues)],
        )
        c.executemany(
            "INSERT INTO manual_prices(import_id, asset_id, date, price_eur, source) VALUES (?,?,?,?,?)",
            [(iid, m["asset_id"], m["date"], _dec_str(m["price_eur"]), m["source"]) for m in parsed.manual_prices],
        )
        # Umschalten
        c.execute("UPDATE imports SET status='archived' WHERE status='active'")
        c.execute("UPDATE imports SET status='active' WHERE id=?", (iid,))
        c.execute("INSERT INTO app_state(key, value) VALUES ('active_import_id', ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(iid),))
        # Aufbewahrung: Daten der letzten KEEP_IMPORTS erfolgreichen Importe behalten
        keep = [r[0] for r in c.execute(
            "SELECT id FROM imports WHERE status IN ('active','archived') ORDER BY id DESC LIMIT ?", (KEEP_IMPORTS,))]
        placeholders = ",".join("?" * len(keep))
        for table in ("tx", "assets", "accounts", "holdings_check", "issues", "manual_prices"):
            c.execute(f"DELETE FROM {table} WHERE import_id NOT IN ({placeholders})", keep)
        c.execute(f"UPDATE imports SET data_retained=0 WHERE id NOT IN ({placeholders}) AND data_retained=1", keep)
    return int(iid)  # type: ignore[arg-type]


def _store_failure(db: Database, path: Path, sha: str, report: Report, duration_ms: int, trigger: str) -> int:
    st = path.stat()
    cur = db.x(
        """INSERT INTO imports(filename, file_sha256, file_size, file_mtime, processed_at, status, report_json,
               duration_ms, data_retained, trigger) VALUES (?,?,?,?,?, 'failed', ?, ?, 0, ?)""",
        (path.name, sha, st.st_size, iso(datetime.fromtimestamp(st.st_mtime, UTC)), iso(datetime.now(UTC)),
         report.to_json(), duration_ms, trigger),
    )
    return int(cur.lastrowid)  # type: ignore[arg-type]


def import_file(db: Database, path: Path, opts: EngineOptions, trigger: str = "manual",
                force: bool = False) -> ImportOutcome:
    if not _import_lock.acquire(blocking=False):
        return ImportOutcome("busy", "Ein Import läuft bereits.")
    try:
        t0 = time.perf_counter()
        sha = sha256_file(path)
        prev = db.q1("SELECT id, status FROM imports WHERE file_sha256=? ORDER BY id DESC LIMIT 1", (sha,))
        if prev is not None and not force:
            if prev["status"] == "active":
                return ImportOutcome("unchanged", "Die neueste Datei ist bereits importiert.", prev["id"], path.name)
            if prev["status"] == "failed":
                row = db.q1("SELECT report_json FROM imports WHERE id=?", (prev["id"],))
                return ImportOutcome("failed", "Diese Datei wurde bereits geprüft und abgelehnt.", prev["id"],
                                     path.name, report=json.loads(row["report_json"]) if row else None)
        report, parsed = validate_zip(path)
        if parsed is None:
            ms = int((time.perf_counter() - t0) * 1000)
            iid = _store_failure(db, path, sha, report, ms, trigger)
            log.warning("Import abgelehnt: %s (%d Fehler)", path.name, len(report.errors),
                        extra={"import_id": iid, "file": path.name})
            return ImportOutcome("failed", f"Import abgelehnt: {len(report.errors)} Fehler", iid, path.name,
                                 report=json.loads(report.to_json()), duration_ms=ms)
        new_pf = portfolio_from_parsed(parsed)
        new_res = run_ledger(new_pf, opts)
        old_id = active_import_id(db)
        old_raw = old_hold = None
        if old_id is not None and db.scalar("SELECT data_retained FROM imports WHERE id=?", (old_id,)):
            old_raw = _raw_map_db(db, old_id)
            old_pf = portfolio_from_db(db, old_id)
            old_hold = run_ledger(old_pf, opts).holdings_by_asset()
        diff = compute_diff(old_raw, parsed, old_hold, new_res.holdings_by_asset(), new_pf.assets)
        check = check_holdings(new_res, new_pf)
        for dev in check["deviations"]:
            report.warn("holdings_check", f"Bestandsabweichung {dev['asset_id']}"
                        f"{' (' + dev['account'] + ')' if dev['account'] else ''}: Ledger {dev['actual']} vs. "
                        f"Soll {dev['expected']}", file="holdings_check.csv")
        for iss in new_res.issues:
            if iss.severity == "warning":
                report.warn(f"ledger_{iss.code}", iss.message, file="transactions.csv")
        ms = int((time.perf_counter() - t0) * 1000)
        iid = _store(db, path, parsed, report, diff, check, ms, trigger)
        log.info("Import aktiviert: %s (id=%s, %d Tx, %d ms)", path.name, iid, len(parsed.transactions), ms,
                 extra={"import_id": iid})
        return ImportOutcome("imported", f"Import erfolgreich ({len(parsed.transactions)} Transaktionen)", iid,
                             path.name, report=json.loads(report.to_json()), diff=diff, check=check, duration_ms=ms)
    finally:
        _import_lock.release()


def check_import_dir(db: Database, import_dir: Path, opts: EngineOptions, trigger: str = "poll",
                     force: bool = False) -> ImportOutcome:
    path, note = find_candidate(import_dir)
    if path is None:
        return ImportOutcome("none", note or "Keine Datei gefunden")
    if note == "pending":
        return ImportOutcome("pending", f"{path.name} wurde gerade geändert – Import beim nächsten Durchlauf "
                                        f"(Datei wird evtl. noch kopiert).", filename=path.name)
    return import_file(db, path, opts, trigger=trigger, force=force)
