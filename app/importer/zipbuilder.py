"""Erzeugt Import-ZIPs im Datenvertragsformat (für Tests, Beispieldaten und Kurator-Tools)."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from app.importer import contract as C

TX_COLUMNS = [*C.TX_REQUIRED[:3], "tag", *C.TX_REQUIRED[3:],
              "orig_price", "orig_ccy", "source", "source_ref", "flag", "note", "related_asset"]
ASSET_COLUMNS = ["asset_id", "name", "asset_class", "wkn", "isin", "koinly_id", "quote_source", "quote_id", "status",
                 "note", "aliases", "category"]
HOLDINGS_COLUMNS = ["asset_id", "account", "qty", "as_of", "note"]
ISSUES_COLUMNS = ["issue_id", "severity", "asset_id", "tx_id", "description", "status"]
MANUAL_COLUMNS = ["asset_id", "date", "price_eur", "source"]
ACCOUNT_COLUMNS = ["account", "broker", "depot_group"]


def to_csv(rows: Iterable[dict[str, Any]], columns: list[str]) -> bytes:
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in columns})
    return buf.getvalue().encode("utf-8")


def build_zip(path: Path, *, transactions: list[dict[str, Any]], assets: list[dict[str, Any]],
              holdings_check: list[dict[str, Any]] | None = None, issues: list[dict[str, Any]] | None = None,
              manual_prices: list[dict[str, Any]] | None = None, accounts: list[dict[str, Any]] | None = None,
              generated_at: str = "2026-09-20T10:00:00Z", valuation_date: str = "2026-09-19",
              schema_version: str = C.CURRENT_SCHEMA_VERSION, notes: str = "", tx_columns: list[str] | None = None,
              extra_tx_columns: list[str] | None = None, mutate: dict[str, Any] | None = None,
              extras: dict[str, bytes] | None = None) -> Path:
    """Baut eine ZIP-Datei. ``mutate`` erlaubt gezielte Defekte für Tests:

    * ``{"bad_checksum": "transactions.csv"}``
    * ``{"drop_file": "issues.csv"}``
    * ``{"manifest": {...}}`` (überschreibt Manifest-Felder)
    """
    mutate = mutate or {}
    cols = tx_columns or TX_COLUMNS
    if extra_tx_columns:
        cols = cols + [c for c in extra_tx_columns if c not in cols]
    files: dict[str, bytes] = {
        "transactions.csv": to_csv(transactions, cols),
        "assets.csv": to_csv(assets, ASSET_COLUMNS + sorted({k for a in assets for k in a} - set(ASSET_COLUMNS))),
        "holdings_check.csv": to_csv(holdings_check or [], HOLDINGS_COLUMNS),
        "issues.csv": to_csv(issues or [], ISSUES_COLUMNS),
    }
    if manual_prices is not None:
        files["manual_prices.csv"] = to_csv(manual_prices, MANUAL_COLUMNS)
    if accounts is not None:
        files["accounts.csv"] = to_csv(accounts, ACCOUNT_COLUMNS + sorted({k for a in accounts for k in a}
                                                                          - set(ACCOUNT_COLUMNS)))
    checksums = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    if "bad_checksum" in mutate:
        checksums[mutate["bad_checksum"]] = "0" * 64
    if "drop_file" in mutate:
        files.pop(mutate["drop_file"], None)
    manifest: dict[str, Any] = {
        "schema_version": schema_version,
        "generated_at": generated_at,
        "valuation_date": valuation_date,
        "files": checksums,
        "notes": notes,
    }
    if extras:  # Portfolia-Zusatzdaten (Ordner portfolia/), eigene Prüfsummen – für andere Werkzeuge unsichtbar
        manifest["extra_files"] = {f"{C.SIDECAR_DIR}{n}": hashlib.sha256(d).hexdigest() for n, d in extras.items()}
    manifest.update(mutate.get("manifest", {}))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        for name, data in files.items():
            zf.writestr(name, data)
        for name, data in (extras or {}).items():
            zf.writestr(f"{C.SIDECAR_DIR}{name}", data)
    return path
