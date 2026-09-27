"""Validierung einer Import-ZIP-Datei.

Ergebnis ist immer ein :class:`Report`. Nur wenn ``report.ok`` (keine Fehler) gilt, liefert
:func:`validate_zip` zusätzlich die geparsten Daten (:class:`ParsedImport`). Warnungen blockieren
den Import nicht, werden aber gespeichert und im UI angezeigt.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.importer import contract as C
from app.util.timeutil import parse_iso, parse_tx_datetime, to_local_date

_DEC_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_SHA_RE = re.compile(r"^(sha256:)?([0-9a-fA-F]{64})$")
MAX_MESSAGES = 2000


@dataclass
class Message:
    severity: str  # error | warning | info
    code: str
    message: str
    file: str | None = None
    line: int | None = None
    column: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class Report:
    messages: list[Message] = field(default_factory=list)
    truncated: int = 0

    def add(self, severity: str, code: str, message: str, file: str | None = None,
            line: int | None = None, column: str | None = None) -> None:
        if len(self.messages) >= MAX_MESSAGES:
            self.truncated += 1
            return
        self.messages.append(Message(severity, code, message, file, line, column))

    def error(self, code: str, message: str, **kw: Any) -> None:
        self.add("error", code, message, **kw)

    def warn(self, code: str, message: str, **kw: Any) -> None:
        self.add("warning", code, message, **kw)

    def info(self, code: str, message: str, **kw: Any) -> None:
        self.add("info", code, message, **kw)

    @property
    def errors(self) -> list[Message]:
        return [m for m in self.messages if m.severity == "error"]

    @property
    def warnings(self) -> list[Message]:
        return [m for m in self.messages if m.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_json(self) -> str:
        return json.dumps({
            "ok": self.ok,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "truncated": self.truncated,
            "messages": [m.to_dict() for m in self.messages],
        }, ensure_ascii=False)


@dataclass
class ParsedImport:
    file_sha256: str
    manifest: dict[str, Any]
    transactions: list[dict[str, Any]]
    assets: list[dict[str, Any]]
    accounts: list[dict[str, Any]]
    holdings_check: list[dict[str, Any]]
    issues: list[dict[str, Any]]
    manual_prices: list[dict[str, Any]]

    @property
    def counts(self) -> dict[str, int]:
        return {
            "transactions": len(self.transactions),
            "assets": len(self.assets),
            "accounts": len(self.accounts),
            "holdings_check": len(self.holdings_check),
            "issues": len(self.issues),
            "manual_prices": len(self.manual_prices),
        }


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    v = value.strip()
    if v == "":
        return None
    if not _DEC_RE.match(v):
        raise ValueError(f"keine gültige Dezimalzahl (Punkt als Trenner): {value!r}")
    try:
        d = Decimal(v)
    except InvalidOperation as e:  # pragma: no cover - Regex fängt das ab
        raise ValueError(f"ungültige Zahl: {value!r}") from e
    if not d.is_finite():
        raise ValueError(f"ungültige Zahl: {value!r}")
    return d


def _clean(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    return v if v != "" else None


# ----------------------------------------------------------------------------------------------
# ZIP-Ebene
# ----------------------------------------------------------------------------------------------

def _read_members(zf: zipfile.ZipFile, rep: Report) -> dict[str, bytes] | None:
    infos = [i for i in zf.infolist() if not i.is_dir()]
    names = [i.filename for i in infos]
    # Unsichere Pfade
    for n in names:
        if n.startswith("/") or ".." in Path(n).parts or "\\" in n:
            rep.error("zip_path", f"Unsicherer Pfad im ZIP: {n}")
    if rep.errors:
        return None
    # Toleranz: genau ein gemeinsamer Oberordner (z. B. 'export/manifest.json')
    prefix = ""
    tops = {n.split("/")[0] for n in names if "/" in n}
    if names and all("/" in n for n in names) and len(tops) == 1:
        prefix = tops.pop() + "/"
        rep.warn("zip_folder", f"Dateien liegen im Unterordner '{prefix}' – wird toleriert.")
    total = 0
    members: dict[str, bytes] = {}
    for info in infos:
        name = info.filename[len(prefix):] if prefix else info.filename
        if "/" in name:
            rep.warn("zip_extra", f"Unerwartete Datei im Unterordner ignoriert: {info.filename}")
            continue
        if name.startswith(".") or name.startswith("__MACOSX"):
            continue
        if info.file_size > C.MAX_MEMBER_BYTES:
            rep.error("zip_size", f"Datei zu groß: {name} ({info.file_size} Bytes)")
            continue
        if info.compress_size and info.file_size / max(info.compress_size, 1) > C.MAX_COMPRESSION_RATIO \
                and info.file_size > 10 * 1024 * 1024:
            rep.error("zip_bomb", f"Verdächtiges Kompressionsverhältnis bei {name}")
            continue
        total += info.file_size
        if total > C.MAX_ZIP_BYTES:
            rep.error("zip_size", "Entpackte Gesamtgröße überschreitet das Limit")
            return None
        if name not in C.ALL_FILES:
            rep.warn("zip_extra", f"Unerwartete Datei wird ignoriert: {name}")
            continue
        if name in members:
            rep.error("zip_duplicate", f"Datei mehrfach im ZIP: {name}")
            continue
        with zf.open(info) as f:
            data = f.read(C.MAX_MEMBER_BYTES + 1)
        members[name] = data
    return members


def _check_manifest(members: dict[str, bytes], rep: Report) -> dict[str, Any] | None:
    raw = members.get("manifest.json")
    if raw is None:
        rep.error("manifest_missing", "manifest.json fehlt")
        return None
    try:
        manifest = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        rep.error("manifest_json", f"manifest.json ist kein gültiges JSON: {e}", file="manifest.json")
        return None
    if not isinstance(manifest, dict):
        rep.error("manifest_json", "manifest.json muss ein Objekt sein", file="manifest.json")
        return None
    for key in ("schema_version", "generated_at", "valuation_date", "files"):
        if key not in manifest:
            rep.error("manifest_key", f"Pflichtfeld fehlt: {key}", file="manifest.json")
    if rep.errors:
        return None
    sv = str(manifest.get("schema_version"))
    m = re.match(r"^(\d+)(?:\.(\d+))?", sv)
    if not m:
        rep.error("schema_version", f"Ungültige schema_version: {sv}", file="manifest.json")
    else:
        major = int(m.group(1))
        minor = int(m.group(2) or 0)
        if major != C.SUPPORTED_SCHEMA_MAJOR:
            rep.error("schema_version", f"schema_version {sv} wird nicht unterstützt (erwartet 1.x)",
                      file="manifest.json")
        elif minor > C.SUPPORTED_SCHEMA_MINOR:
            rep.warn("schema_version", f"schema_version {sv}: neuere Minor-Version als {C.CURRENT_SCHEMA_VERSION}, "
                                       "unbekannte Felder werden ignoriert", file="manifest.json")
    try:
        parse_iso(str(manifest["generated_at"]))
    except ValueError:
        rep.error("generated_at", f"generated_at ist kein ISO-8601-Zeitpunkt: {manifest['generated_at']}",
                  file="manifest.json")
    try:
        date.fromisoformat(str(manifest["valuation_date"]))
    except ValueError:
        rep.error("valuation_date", f"valuation_date ist kein Datum (YYYY-MM-DD): {manifest['valuation_date']}",
                  file="manifest.json")
    files = manifest.get("files")
    if not isinstance(files, dict):
        rep.error("manifest_files", "'files' muss ein Objekt {dateiname: sha256} sein", file="manifest.json")
        return None
    # Prüfsummen
    for name, expected in files.items():
        if name == "manifest.json":
            continue
        m2 = _SHA_RE.match(str(expected).strip())
        if not m2:
            rep.error("checksum_format", f"Ungültige Prüfsumme für {name}", file="manifest.json")
            continue
        data = members.get(name)
        if data is None:
            rep.error("file_missing", f"Im Manifest gelistete Datei fehlt im ZIP: {name}", file=name)
            continue
        actual = hashlib.sha256(data).hexdigest()
        if actual.lower() != m2.group(2).lower():
            rep.error("checksum", f"Prüfsumme stimmt nicht: {name} (erwartet {m2.group(2)[:12]}…, "
                                  f"ist {actual[:12]}…)", file=name)
    for name in members:
        if name != "manifest.json" and name not in files:
            rep.error("checksum_missing", f"Datei ohne Prüfsumme im Manifest: {name}", file=name)
    for name in C.REQUIRED_FILES:
        if name not in members:
            rep.error("file_missing", f"Pflichtdatei fehlt: {name}", file=name)
    return manifest


def _read_csv(members: dict[str, bytes], name: str, required: tuple[str, ...], optional: tuple[str, ...],
              rep: Report, aliases: dict[str, str] | None = None) -> tuple[list[dict[str, str]], list[str]] | None:
    raw = members.get(name)
    if raw is None:
        return None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        rep.error("encoding", f"{name} ist nicht UTF-8-kodiert: {e}", file=name)
        return None
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=",", quotechar='"', strict=True)
    try:
        header = next(reader)
    except StopIteration:
        rep.error("csv_empty", f"{name} ist leer (Kopfzeile fehlt)", file=name)
        return None
    except csv.Error as e:
        rep.error("csv_format", f"{name}: {e}", file=name, line=1)
        return None
    header = [h.strip() for h in header]
    if aliases:
        header = [aliases.get(h.lower(), h) for h in header]
    if len(header) == 1 and (";" in header[0] or "\t" in header[0]):
        rep.error("csv_delimiter", f"{name}: Trennzeichen muss ein Komma sein", file=name, line=1)
        return None
    dupes = {h for h in header if header.count(h) > 1}
    if dupes:
        rep.error("csv_header", f"{name}: doppelte Spalten {sorted(dupes)}", file=name, line=1)
        return None
    missing = [c for c in required if c not in header]
    if missing:
        rep.error("missing_columns", f"{name}: Pflichtspalten fehlen: {', '.join(missing)}", file=name, line=1)
        return None
    for c in optional:
        if c not in header:
            rep.info("optional_column", f"{name}: optionale Spalte '{c}' fehlt (leer angenommen)", file=name)
    rows: list[dict[str, str]] = []
    line = 1
    try:
        for rec in reader:
            line += 1
            if not rec or all(not x.strip() for x in rec):
                continue
            if len(rec) != len(header):
                rep.error("csv_columns", f"{name}: Zeile hat {len(rec)} statt {len(header)} Felder",
                          file=name, line=line)
                continue
            row = dict(zip(header, rec, strict=True))
            row["__line__"] = str(line)
            rows.append(row)
    except csv.Error as e:
        rep.error("csv_format", f"{name}: {e}", file=name, line=line + 1)
        return None
    return rows, header


# ----------------------------------------------------------------------------------------------
# Tabellen
# ----------------------------------------------------------------------------------------------

def _parse_assets(rows: list[dict[str, str]], header: list[str], rep: Report) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    known = set(C.ASSET_REQUIRED) | set(C.ASSET_OPTIONAL)
    for r in rows:
        ln = int(r["__line__"])
        aid = _clean(r.get("asset_id"))
        if not aid:
            rep.error("asset_id", "asset_id fehlt", file="assets.csv", line=ln, column="asset_id")
            continue
        if aid in seen:
            rep.error("asset_dup", f"asset_id doppelt: {aid}", file="assets.csv", line=ln, column="asset_id")
            continue
        seen.add(aid)
        cls = (_clean(r.get("asset_class")) or "").lower()
        if cls not in C.ASSET_CLASSES:
            rep.error("asset_class", f"{aid}: asset_class '{cls}' unbekannt (security|crypto|fiat)",
                      file="assets.csv", line=ln, column="asset_class")
            continue
        qs = (_clean(r.get("quote_source")) or "none").lower()
        if qs not in C.QUOTE_SOURCES:
            rep.error("quote_source", f"{aid}: quote_source '{qs}' unbekannt", file="assets.csv", line=ln,
                      column="quote_source")
            continue
        qid = _clean(r.get("quote_id"))
        if qs in ("yahoo", "coingecko") and not qid:
            rep.warn("quote_id", f"{aid}: quote_id fehlt für Quelle {qs} – Asset bleibt unbewertet",
                     file="assets.csv", line=ln, column="quote_id")
            qs = "none"
        extra = {k: v for k, v in r.items() if k not in known and k != "__line__" and v != ""}
        out.append({
            "asset_id": aid,
            "name": _clean(r.get("name")) or aid,
            "asset_class": cls,
            "wkn": _clean(r.get("wkn")),
            "isin": _clean(r.get("isin")),
            "koinly_id": _clean(r.get("koinly_id")),
            "quote_source": qs,
            "quote_id": qid,
            "status": _clean(r.get("status")),
            "note": _clean(r.get("note")),
            "aliases": _clean(r.get("aliases")),
            "category": _clean(r.get("category")),
            "extra": extra,
        })
    return out


def _parse_accounts(rows: list[dict[str, str]], rep: Report) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    known = set(C.ACCOUNTS_REQUIRED) | set(C.ACCOUNTS_OPTIONAL)
    for r in rows:
        ln = int(r["__line__"])
        acc = _clean(r.get("account"))
        if not acc:
            rep.error("account", "account fehlt", file="accounts.csv", line=ln)
            continue
        if acc in seen:
            rep.error("account_dup", f"account doppelt: {acc}", file="accounts.csv", line=ln)
            continue
        seen.add(acc)
        extra = {k: v for k, v in r.items() if k not in known and k != "__line__" and v != ""}
        out.append({"account": acc, "broker": _clean(r.get("broker")), "depot_group": _clean(r.get("depot_group")),
                    "extra": extra})
    return out


def _dec(r: dict[str, str], col: str, rep: Report, ln: int, file: str, *, positive: bool = False,
         nonneg: bool = False) -> Decimal | None:
    try:
        v = parse_decimal(r.get(col))
    except ValueError as e:
        rep.error("number", f"{col}: {e}", file=file, line=ln, column=col)
        return None
    if v is not None:
        if positive and v <= 0:
            rep.error("number_sign", f"{col} muss > 0 sein (ist {v})", file=file, line=ln, column=col)
        elif nonneg and v < 0:
            rep.error("number_sign", f"{col} darf nicht negativ sein (ist {v})", file=file, line=ln, column=col)
    return v


def _parse_transactions(rows: list[dict[str, str]], header: list[str], assets: dict[str, dict[str, Any]],
                        accounts: set[str] | None, generated_at: datetime | None, rep: Report
                        ) -> list[dict[str, Any]]:
    F = "transactions.csv"
    out: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    unknown_tags: dict[str, int] = {}
    unknown_accounts: set[str] = set()
    known_cols = set(C.TX_REQUIRED) | set(C.TX_OPTIONAL)
    extra_cols = [h for h in header if h not in known_cols]
    if extra_cols:
        rep.info("extra_columns", f"Zusätzliche Spalten werden mitgespeichert: {', '.join(extra_cols)}", file=F)

    for seq, r in enumerate(rows):
        ln = int(r["__line__"])
        err_before = len(rep.errors)
        tx_id = _clean(r.get("tx_id"))
        if not tx_id:
            rep.error("tx_id", "tx_id fehlt", file=F, line=ln, column="tx_id")
            continue
        if tx_id in seen:
            rep.error("tx_dup", f"tx_id doppelt: {tx_id} (erstmals Zeile {seen[tx_id]})", file=F, line=ln,
                      column="tx_id")
            continue
        seen[tx_id] = ln
        raw_dt = _clean(r.get("datetime")) or ""
        try:
            ts, date_only = parse_tx_datetime(raw_dt)
        except ValueError:
            rep.error("datetime", f"{tx_id}: datetime ungültig: {raw_dt!r}", file=F, line=ln, column="datetime")
            continue
        typ = (_clean(r.get("type")) or "").lower()
        if typ not in C.TX_TYPES:
            rep.error("tx_type", f"{tx_id}: type '{typ}' unbekannt", file=F, line=ln, column="type")
            continue
        tag = (_clean(r.get("tag")) or "").lower() or None
        if tag and tag not in C.KNOWN_TAGS:
            unknown_tags[tag] = unknown_tags.get(tag, 0) + 1

        legs: dict[str, tuple[str | None, str | None, Decimal | None]] = {}
        for side in ("from", "to"):
            acc = _clean(r.get(f"{side}_account"))
            asset = _clean(r.get(f"{side}_asset"))
            qty = _dec(r, f"{side}_qty", rep, ln, F, nonneg=True)
            if asset and qty is None:
                rep.error("leg_qty", f"{tx_id}: {side}_qty fehlt für {side}_asset={asset}", file=F, line=ln,
                          column=f"{side}_qty")
            if qty is not None and qty != 0 and not asset:
                rep.error("leg_asset", f"{tx_id}: {side}_asset fehlt bei Menge {qty}", file=F, line=ln,
                          column=f"{side}_asset")
            if asset and not acc:
                rep.error("leg_account", f"{tx_id}: {side}_account fehlt für {asset}", file=F, line=ln,
                          column=f"{side}_account")
            if asset and asset not in assets:
                rep.error("asset_ref", f"{tx_id}: unbekannte asset_id '{asset}' in {side}_asset", file=F,
                          line=ln, column=f"{side}_asset")
            if acc and accounts is not None and acc not in accounts:
                unknown_accounts.add(acc)
            legs[side] = (acc, asset, qty if asset else None)

        fee_asset = _clean(r.get("fee_asset"))
        fee_qty = _dec(r, "fee_qty", rep, ln, F, nonneg=True)
        fee_eur = _dec(r, "fee_eur", rep, ln, F, nonneg=True)
        if fee_qty and not fee_asset:
            rep.error("fee_asset", f"{tx_id}: fee_asset fehlt bei fee_qty", file=F, line=ln, column="fee_asset")
        if fee_asset and fee_asset not in assets:
            rep.error("asset_ref", f"{tx_id}: unbekannte asset_id '{fee_asset}' in fee_asset", file=F, line=ln,
                      column="fee_asset")
        if fee_asset and fee_asset in assets and fee_qty:
            if assets[fee_asset]["asset_class"] == "fiat" and fee_eur is None and fee_asset == "EUR":
                fee_eur = fee_qty
            elif fee_eur is None:
                rep.warn("fee_eur", f"{tx_id}: fee_eur fehlt – Gebühr wird mit 0 € bewertet", file=F, line=ln,
                         column="fee_eur")
        value_eur = _dec(r, "value_eur", rep, ln, F, nonneg=True)
        related = _clean(r.get("related_asset"))
        if related and related not in assets:
            rep.warn("asset_ref", f"{tx_id}: related_asset '{related}' unbekannt – wird ignoriert", file=F, line=ln)
            related = None

        fa, fasset, fq = legs["from"]
        ta, tasset, tq = legs["to"]
        has_from = bool(fasset)
        has_to = bool(tasset)
        # Typabhängige Regeln
        if typ in ("buy", "trade", "deposit", "corporate_action") and not has_to:
            rep.error("leg_rule", f"{tx_id}: type {typ} benötigt ein Zugangsbein (to_*)", file=F, line=ln)
        if typ in ("sell", "trade", "withdrawal", "transfer") and not has_from:
            rep.error("leg_rule", f"{tx_id}: type {typ} benötigt ein Abgangsbein (from_*)", file=F, line=ln)
        if typ == "transfer" and not has_to:
            rep.error("leg_rule", f"{tx_id}: transfer benötigt ein Zugangsbein (to_*)", file=F, line=ln)
        if typ == "deposit" and has_from:
            rep.error("leg_rule", f"{tx_id}: deposit darf kein Abgangsbein haben", file=F, line=ln)
        if typ == "withdrawal" and has_to:
            rep.error("leg_rule", f"{tx_id}: withdrawal darf kein Zugangsbein haben", file=F, line=ln)
        if typ in ("buy", "sell", "trade") and value_eur is None:
            rep.error("value_eur", f"{tx_id}: value_eur ist Pflicht für {typ} (Einstand/Erlös)", file=F, line=ln,
                      column="value_eur")
        if typ in ("deposit", "withdrawal") and value_eur is None:
            asset_id = tasset if typ == "deposit" else fasset
            if asset_id in assets and assets[asset_id]["asset_class"] != "fiat":
                rep.warn("value_eur", f"{tx_id}: value_eur fehlt – Zu-/Abgang wird mit 0 € angesetzt", file=F,
                         line=ln, column="value_eur")
        if typ == "transfer" and has_from and has_to and fasset != tasset:
            rep.warn("transfer_assets", f"{tx_id}: transfer mit unterschiedlichen Assets ({fasset}→{tasset}) "
                                        "wird wie eine Kapitalmaßnahme (Lot-Übertrag) behandelt", file=F, line=ln)
        if typ == "transfer" and fq is not None and tq is not None and fasset == tasset:
            if tq > fq:
                rep.error("transfer_qty", f"{tx_id}: transfer erhält mehr ({tq}) als gesendet ({fq})", file=F,
                          line=ln)
            elif fee_qty and fee_asset == fasset and fq - tq == fee_qty and fee_qty > 0:
                rep.warn("transfer_fee_double", f"{tx_id}: from_qty − to_qty entspricht fee_qty – Gebühr "
                                                "möglicherweise doppelt erfasst (from_qty soll ohne Gebühr sein)",
                         file=F, line=ln)
        if typ in ("buy", "sell") and has_from and has_to:
            fcls = assets.get(fasset, {}).get("asset_class")
            tcls = assets.get(tasset, {}).get("asset_class")
            if typ == "buy" and fcls not in (None, "fiat"):
                rep.warn("type_mismatch", f"{tx_id}: buy mit Nicht-Fiat-Abgang ({fasset}) – als Tausch behandelt",
                         file=F, line=ln)
            if typ == "sell" and tcls not in (None, "fiat"):
                rep.warn("type_mismatch", f"{tx_id}: sell mit Nicht-Fiat-Zugang ({tasset}) – als Tausch behandelt",
                         file=F, line=ln)
        if generated_at and ts > generated_at:
            rep.warn("future_tx", f"{tx_id}: liegt nach generated_at des Manifests", file=F, line=ln)

        if len(rep.errors) > err_before:
            continue
        raw = {k: v for k, v in r.items() if k != "__line__"}
        row_hash = hashlib.sha1(json.dumps(raw, sort_keys=True).encode("utf-8"), usedforsecurity=False).hexdigest()
        out.append({
            "seq": seq,
            "line": ln,
            "tx_id": tx_id,
            "ts_utc": ts,
            "date_local": to_local_date(ts),
            "date_only": date_only,
            "type": typ,
            "tag": tag,
            "from_account": fa if fasset else None, "from_asset": fasset, "from_qty": fq,
            "to_account": ta if tasset else None, "to_asset": tasset, "to_qty": tq,
            "fee_asset": fee_asset if fee_qty else None, "fee_qty": fee_qty if fee_asset else None,
            "fee_eur": fee_eur,
            "value_eur": value_eur,
            "orig_price": _clean(r.get("orig_price")),
            "orig_ccy": _clean(r.get("orig_ccy")),
            "source": _clean(r.get("source")),
            "source_ref": _clean(r.get("source_ref")),
            "flag": _clean(r.get("flag")),
            "note": _clean(r.get("note")),
            "related_asset": related,
            "row_hash": row_hash,
            "raw": raw,
        })
    for tag, n in sorted(unknown_tags.items()):
        rep.warn("unknown_tag", f"Unbekannter tag '{tag}' ({n}×) – wird wie ohne Tag behandelt", file=F)
    if unknown_accounts:
        rep.warn("account_ref", f"Konten ohne Eintrag in accounts.csv: {', '.join(sorted(unknown_accounts))}",
                 file=F)
    return out


def _parse_holdings(rows: list[dict[str, str]], assets: dict[str, Any], rep: Report) -> list[dict[str, Any]]:
    F = "holdings_check.csv"
    out = []
    for seq, r in enumerate(rows):
        ln = int(r["__line__"])
        aid = _clean(r.get("asset_id"))
        if not aid:
            rep.error("asset_id", "asset_id fehlt", file=F, line=ln)
            continue
        qty = _dec(r, "qty", rep, ln, F)
        if qty is None:
            rep.error("qty", f"{aid}: qty fehlt", file=F, line=ln)
            continue
        if aid not in assets:
            rep.warn("asset_ref", f"holdings_check: unbekannte asset_id '{aid}'", file=F, line=ln)
        extra = {k: v for k, v in r.items() if k not in ("asset_id", "qty", "account", "__line__") and v != ""}
        out.append({"seq": seq, "asset_id": aid, "account": _clean(r.get("account")), "qty": qty, "extra": extra})
    return out


def _parse_manual(rows: list[dict[str, str]], assets: dict[str, Any], rep: Report) -> list[dict[str, Any]]:
    F = "manual_prices.csv"
    out = []
    seen: set[tuple[str, str]] = set()
    for r in rows:
        ln = int(r["__line__"])
        aid = _clean(r.get("asset_id"))
        d = _clean(r.get("date"))
        if not aid or not d:
            rep.error("manual_row", "asset_id und date sind Pflicht", file=F, line=ln)
            continue
        try:
            dd = date.fromisoformat(d[:10])
        except ValueError:
            rep.error("date", f"Datum ungültig: {d!r}", file=F, line=ln, column="date")
            continue
        price = _dec(r, "price_eur", rep, ln, F, nonneg=True)
        if price is None:
            rep.error("price_eur", "price_eur fehlt", file=F, line=ln)
            continue
        if aid not in assets:
            rep.warn("asset_ref", f"manual_prices: unbekannte asset_id '{aid}'", file=F, line=ln)
            continue
        key = (aid, dd.isoformat())
        if key in seen:
            rep.warn("manual_dup", f"Doppelter Kurs {aid} {dd} – letzter Wert gilt", file=F, line=ln)
            out = [o for o in out if (o["asset_id"], o["date"]) != key]
        seen.add(key)
        out.append({"asset_id": aid, "date": dd.isoformat(), "price_eur": price, "source": _clean(r.get("source"))})
    return out


def validate_zip(path: Path) -> tuple[Report, ParsedImport | None]:
    rep = Report()
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as e:
        rep.error("file", f"Datei nicht lesbar: {e}")
        return rep, None
    if size > C.MAX_ZIP_BYTES:
        rep.error("zip_size", f"ZIP zu groß ({size} Bytes)")
        return rep, None
    file_sha = sha256_file(path)
    try:
        with zipfile.ZipFile(path) as zf:
            members = _read_members(zf, rep)
    except zipfile.BadZipFile as e:
        rep.error("zip_invalid", f"Keine gültige ZIP-Datei: {e}")
        return rep, None
    if members is None:
        return rep, None
    manifest = _check_manifest(members, rep)
    if manifest is None or not rep.ok:
        return rep, None

    gen_at = parse_iso(str(manifest["generated_at"]))

    a = _read_csv(members, "assets.csv", C.ASSET_REQUIRED, C.ASSET_OPTIONAL, rep)
    assets_list = _parse_assets(*a, rep) if a else []
    acc_rows = _read_csv(members, "accounts.csv", C.ACCOUNTS_REQUIRED, C.ACCOUNTS_OPTIONAL, rep)
    accounts_list = _parse_accounts(acc_rows[0], rep) if acc_rows else []
    assets = {x["asset_id"]: x for x in assets_list}

    t = _read_csv(members, "transactions.csv", C.TX_REQUIRED, C.TX_OPTIONAL, rep)
    if t:
        # Implizite Fiat-Währungen ergänzen (mit Warnung), damit z. B. fehlendes 'EUR' kein Abbruch ist.
        used = set()
        for r in t[0]:
            for col in ("from_asset", "to_asset", "fee_asset"):
                v = _clean(r.get(col))
                if v:
                    used.add(v)
        for code in sorted(used - set(assets)):
            if code in C.ISO_CURRENCIES:
                assets[code] = {"asset_id": code, "name": code, "asset_class": "fiat", "wkn": None, "isin": None,
                                "koinly_id": None, "quote_source": "none", "quote_id": None, "status": None,
                                "note": "implizit ergänzt", "aliases": None, "category": "Cash", "extra": {}}
                assets_list.append(assets[code])
                rep.warn("implicit_fiat", f"Währung {code} fehlt in assets.csv – als Fiat ergänzt", file="assets.csv")
    accounts_set = {x["account"] for x in accounts_list} if acc_rows else None
    txs = _parse_transactions(t[0], t[1], assets, accounts_set, gen_at, rep) if t else []

    h = _read_csv(members, "holdings_check.csv", C.HOLDINGS_REQUIRED, C.HOLDINGS_OPTIONAL, rep,
                  aliases=C.HOLDINGS_ALIASES)
    holdings = _parse_holdings(h[0], assets, rep) if h else []

    i = _read_csv(members, "issues.csv", (), (), rep)
    issues = [{k: v for k, v in r.items() if k != "__line__"} for r in i[0]] if i else []

    mp = _read_csv(members, "manual_prices.csv", C.MANUAL_REQUIRED, C.MANUAL_OPTIONAL, rep)
    manual = _parse_manual(mp[0], assets, rep) if mp else []

    if not txs and rep.ok:
        rep.error("tx_empty", "transactions.csv enthält keine Transaktionen", file="transactions.csv")
    if not rep.ok:
        return rep, None
    return rep, ParsedImport(
        file_sha256=file_sha,
        manifest=manifest,
        transactions=txs,
        assets=assets_list,
        accounts=accounts_list,
        holdings_check=holdings,
        issues=issues,
        manual_prices=manual,
    )
