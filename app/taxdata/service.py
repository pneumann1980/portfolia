"""Steuerdaten je Jahr: eine zentrale Pipeline für Ordner und Upload.

Ablauf (für beide Wege gleich): Datei lesen → Format erkennen → Parser (:mod:`app.taxdata.parsers`) → Steuerjahr
bestimmen (Inhalt, sonst Dateiname; mehrdeutig → der Nutzer wählt) → prüfen → Vorschau („1.245 Datensätze erkannt“)
→ aktivieren. Für jedes Jahr ist **höchstens eine** Datei aktiv (Unique-Index ``ux_tax_file_active``). Gibt es für das
Jahr schon eine aktive Datei, wird nie still ersetzt: Die neue Datei wartet („pending“) mit Gegenüberstellung
(alte/neue Datei, Anzahlen, hinzugekommene/entfallene/geänderte Datensätze), bis der Nutzer ersetzt oder abbricht.
Ersetzte und entfernte Fassungen bleiben samt Datensätzen erhalten (Nachvollziehbarkeit, Rücknahme).

Zuordnung zu Portfolia-Buchungen (nur Verweis, es entstehen **nie** Buchungen): externe ID → Portfolia-Buchungs-ID →
Asset + Datum + Menge (+ Betrag) → sonst „nicht zugeordnet“; mehrere Kandidaten bzw. widersprüchliche Angaben →
„Konflikt“.

Ordner (``/data/tax``): geprüft beim Start, stündlich und auf Knopfdruck – ohne dauerhaften Dateiwächter. Unveränderte
Dateien (Größe, Änderungszeit) werden nicht erneut gelesen; gleicher Inhalt (SHA-256) wird nie doppelt importiert.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.taxdata.parsers import TaxRecord, detect
from app.util.timeutil import iso

log = logging.getLogger(__name__)

MAX_SIZE = 25 * 1024 * 1024
SUFFIXES = (".json", ".csv")
_YEAR = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_LOCK = threading.RLock()  # reentrant: scan() aktiviert innerhalb der Sperre


@dataclass
class Analysis:
    filename: str
    sha256: str
    size: int
    format: str | None = None
    parser: str | None = None
    records: list[TaxRecord] = field(default_factory=list)
    year: int | None = None  # eindeutig erkanntes Jahr
    year_candidates: list[int] = field(default_factory=list)
    year_source: str | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.records)


def analyze(data: bytes, filename: str) -> Analysis:
    a = Analysis(filename=filename, sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    if len(data) > MAX_SIZE:
        a.errors.append(f"Datei zu groß (höchstens {MAX_SIZE // 1024 // 1024} MB).")
        return a
    parser = detect(filename, data)
    if parser is None:
        a.errors.append("Format nicht erkannt – unterstützt: JSON (bevorzugt) und CSV.")
        return a
    a.format, a.parser = parser.format, parser.id
    res = parser.parse(data)
    a.records, a.warnings, a.errors = res.records, res.warnings[:200], res.errors
    if not a.errors and not a.records:
        a.errors.append("Keine Datensätze erkannt.")
    # Steuerjahr: Kopf → Datensätze (taxYear bzw. Veräußerungsdatum) → Dateiname
    from_records = {r.tax_year for r in a.records if r.tax_year} or {r.disposal_date.year for r in a.records
                                                                    if r.disposal_date}
    name_years = {int(y) for y in _YEAR.findall(filename)}
    if res.year:
        a.year, a.year_source = res.year, "Kopfdaten der Datei"
    elif len(from_records) == 1:
        a.year, a.year_source = next(iter(from_records)), "Datensätze"
    elif not from_records and len(name_years) == 1:
        a.year, a.year_source = next(iter(name_years)), "Dateiname"
    a.year_candidates = sorted(set(from_records) | name_years | ({res.year} if res.year else set()))
    if a.year is not None:
        other = sum(1 for r in a.records if (r.tax_year or (r.disposal_date.year if r.disposal_date else a.year))
                    != a.year)
        if other:
            a.warnings.insert(0, f"{other} Datensätze gehören laut Datum/Steuerjahr nicht zu {a.year}.")
    return a


def _key(r: Any) -> str:
    """Vergleichsschlüssel eines Datensatzes (für „hinzugekommen/entfallen/geändert“ beim Ersetzen)."""
    if r["external_id"] or r["transaction_id"]:
        return f"id:{r['external_id'] or ''}|{r['transaction_id'] or ''}"
    return f"{r['asset']}|{r['disposal_date']}|{r['acquisition_date']}|{r['quantity']}"


def _sig(r: Any) -> tuple[Any, ...]:
    return (r["asset"], r["quantity"], r["acquisition_date"], r["disposal_date"], r["acquisition_cost"],
            r["disposal_value"], r["gain_loss"], r["taxable"], r["tax_category"])


class TaxImportService:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.db = ctx.db
        self.dir = Path(ctx.config.tax_data_dir)

    # -- Lesen ----------------------------------------------------------------------------------------------
    def files(self) -> list[Any]:
        return self.db.q("SELECT * FROM tax_file ORDER BY COALESCE(tax_year, 9999) DESC, id DESC")

    def file(self, fid: int) -> Any:
        return self.db.q1("SELECT * FROM tax_file WHERE id=?", (fid,))

    def active(self, year: int) -> Any:
        return self.db.q1("SELECT * FROM tax_file WHERE tax_year=? AND status='active'", (year,))

    def years(self) -> list[dict[str, Any]]:
        """Je Steuerjahr: aktive Datei, wartende Dateien, Verlauf."""
        out: dict[Any, dict[str, Any]] = defaultdict(lambda: {"active": None, "pending": [], "history": []})
        for f in self.files():
            y = out[f["tax_year"]]
            y["year"] = f["tax_year"]
            if f["status"] == "active":
                y["active"] = f
            elif f["status"] == "pending":
                y["pending"].append(f)
            else:
                y["history"].append(f)
        return sorted(out.values(), key=lambda y: -(y["year"] or 9999))

    def pending(self) -> list[Any]:
        return self.db.q("SELECT * FROM tax_file WHERE status='pending' ORDER BY id")

    def records(self, fid: int, status: str = "") -> list[Any]:
        if status:
            return self.db.q("SELECT * FROM tax_record WHERE file_id=? AND match_status=? ORDER BY line", (fid, status))
        return self.db.q("SELECT * FROM tax_record WHERE file_id=? ORDER BY line", (fid,))

    # -- Aufnehmen ------------------------------------------------------------------------------------------
    def register(self, data: bytes, filename: str, origin: str, path: Path | None = None) -> tuple[int | None,
                                                                                                    Analysis]:
        """Datei prüfen und als „pending“ ablegen (Upload/Ordner gleich). Gleicher Inhalt → vorhandener Eintrag."""
        a = analyze(data, filename)
        dup = self.db.q1("SELECT id, status FROM tax_file WHERE sha256=? AND status IN ('pending', 'active') "
                         "ORDER BY id DESC LIMIT 1", (a.sha256,))
        if dup is not None:
            return int(dup["id"]), a
        if origin == "upload" and not a.ok:
            return None, a  # abgelehnter Upload: nur Fehlermeldung, kein Eintrag (Ordnerdateien bleiben vermerkt)
        if origin == "upload" and path is None and a.ok:
            up = self.dir / "uploads"
            up.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).name)[:80] or "steuerdaten"
            path = up / f"{a.sha256[:12]}_{safe}"
            path.write_bytes(data)
        status = "pending" if a.ok else "rejected"
        now = iso(datetime.now(UTC))
        with self.db.transaction() as c:
            cur = c.execute(
                "INSERT INTO tax_file(tax_year, filename, origin, path, sha256, size, format, parser, status, records, "
                "years_json, warnings_json, errors_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (a.year, Path(filename).name[:200], origin, str(path) if path else None, a.sha256, a.size,
                 a.format or "?", a.parser or "?", status, len(a.records),
                 json.dumps({"candidates": a.year_candidates, "source": a.year_source}),
                 json.dumps(a.warnings[:200], ensure_ascii=False), json.dumps(a.errors[:50], ensure_ascii=False),
                 now))
            fid = int(cur.lastrowid)  # type: ignore[arg-type]
            c.executemany(
                "INSERT INTO tax_record(file_id, line, tax_year, transaction_id, external_id, asset, quantity, "
                "acquisition_date, disposal_date, acquisition_cost, disposal_value, holding_period_days, taxable, "
                "gain_loss, tax_category, source, comment) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(fid, r.line, r.tax_year, r.transaction_id, r.external_id, r.asset, _s(r.quantity),
                  _d(r.acquisition_date), _d(r.disposal_date), _s(r.acquisition_cost), _s(r.disposal_value),
                  r.holding_period_days, None if r.taxable is None else int(r.taxable), _s(r.gain_loss),
                  r.tax_category, r.source, r.comment) for r in a.records])
        if a.ok:
            self.match(fid)
        log.info("Steuerdatei erkannt: %s (Jahr %s, %d Datensätze, %s)", filename, a.year or "?", len(a.records),
                 status)
        return fid, a

    def set_year(self, fid: int, year: int) -> list[str]:
        f = self.file(fid)
        if f is None or f["status"] != "pending":
            return ["Datei nicht gefunden bzw. bereits übernommen."]
        if not 2000 <= year <= datetime.now(UTC).year + 1:
            return ["Ungültiges Steuerjahr."]
        self.db.x("UPDATE tax_file SET tax_year=? WHERE id=?", (year, fid))
        return []

    def compare(self, fid: int) -> dict[str, Any] | None:
        """Gegenüberstellung mit der aktiven Datei desselben Jahres (vor dem Ersetzen)."""
        new = self.file(fid)
        if new is None or new["tax_year"] is None:
            return None
        old = self.active(int(new["tax_year"]))
        if old is None or int(old["id"]) == fid:
            return None
        a = {_key(r): r for r in self.records(int(old["id"]))}
        b = {_key(r): r for r in self.records(fid)}
        changed = [k for k in a.keys() & b.keys() if _sig(a[k]) != _sig(b[k])]
        sums = {}
        for name, rows in (("old", a.values()), ("new", b.values())):
            sums[name] = sum((Decimal(r["gain_loss"]) for r in rows if r["gain_loss"]), Decimal(0))
        return {"old": old, "new": new, "added": len(b.keys() - a.keys()), "removed": len(a.keys() - b.keys()),
                "changed": len(changed), "same": len(a.keys() & b.keys()) - len(changed),
                "gain_old": sums["old"], "gain_new": sums["new"],
                "examples": sorted(b.keys() - a.keys())[:5] + sorted(changed)[:5]}

    def activate(self, fid: int, replace: bool = False) -> dict[str, Any]:
        """„pending“ → aktiv. Gibt es schon eine aktive Datei für das Jahr, nur mit ``replace=True`` (bestätigt)."""
        with _LOCK:
            f = self.file(fid)
            if f is None or f["status"] != "pending":
                return {"errors": ["Datei nicht gefunden bzw. bereits übernommen."]}
            if f["tax_year"] is None:
                return {"errors": ["Steuerjahr nicht eindeutig – bitte wählen."]}
            year = int(f["tax_year"])
            old = self.active(year)
            if old is not None and old["sha256"] == f["sha256"]:
                self.db.x("UPDATE tax_file SET status='removed', note=? WHERE id=?",
                          ("gleicher Inhalt wie die aktive Datei", fid))
                return {"unchanged": True, "id": int(old["id"])}
            if old is not None and not replace:
                return {"needs_confirm": True, "existing": old}
            now = iso(datetime.now(UTC))
            with self.db.transaction() as c:
                if old is not None:
                    c.execute("UPDATE tax_file SET status='replaced', replaced_at=?, replaced_by=? WHERE id=?",
                              (now, fid, old["id"]))
                c.execute("UPDATE tax_file SET status='active', imported_at=? WHERE id=?", (now, fid))
            log.info("Steuerdaten %s aktiviert: %s%s", year, f["filename"],
                     f" (ersetzt {old['filename']})" if old is not None else "")
            return {"activated": True, "year": year, "replaced": int(old["id"]) if old is not None else None}

    def discard(self, fid: int) -> bool:
        """Wartende Datei verwerfen (Abbrechen) – bleibt als „entfernt“ im Verlauf."""
        return bool(self.db.x("UPDATE tax_file SET status='removed', note='verworfen' WHERE id=? AND "
                              "status IN ('pending', 'rejected')", (fid,)).rowcount)

    def remove(self, fid: int) -> bool:
        """Aktive Datei entfernen (nach Bestätigung): Status „entfernt“, Datensätze bleiben im Verlauf."""
        return bool(self.db.x("UPDATE tax_file SET status='removed', replaced_at=?, note='entfernt' WHERE id=? AND "
                              "status='active'", (iso(datetime.now(UTC)), fid)).rowcount)

    # -- Zuordnung ------------------------------------------------------------------------------------------
    def match(self, fid: int) -> dict[str, int]:
        """Datensätze Portfolia-Buchungen zuordnen (nur Verweis – es werden nie Buchungen angelegt)."""
        pf = self.ctx.portfolio()
        by_id: dict[str, Any] = {}
        by_ext: dict[str, list[Any]] = defaultdict(list)
        by_asset_day: dict[tuple[str, str], list[Any]] = defaultdict(list)
        if pf is not None:
            for t in pf.txs:
                by_id[t.tx_id] = t
                if t.source_ref:
                    by_ext[str(t.source_ref).strip().lower()].append(t)
                for asset in {t.from_asset, t.to_asset} - {None}:
                    by_asset_day[(str(asset).upper(), t.date.isoformat())].append(t)
            for r in self.db.q("SELECT tx_id, external_id, event_key FROM journal_tx WHERE status='active'"):
                t = by_id.get(r["tx_id"])
                for ext in (r["external_id"], r["event_key"]):
                    if t is not None and ext:
                        by_ext[str(ext).strip().lower()].append(t)
                        by_ext[str(ext).split(":", 1)[-1].strip().lower()].append(t)
        aliases = self._asset_aliases(pf)
        counts = {"matched": 0, "unmatched": 0, "conflict": 0}
        updates = []
        for r in self.records(fid):
            status, tx, method, note = self._match_one(r, by_id, by_ext, by_asset_day, aliases)
            counts[status] += 1
            updates.append((status, tx, method, note, r["id"]))
        with self.db.transaction() as c:
            c.executemany("UPDATE tax_record SET match_status=?, match_tx=?, match_method=?, match_note=? WHERE id=?",
                          updates)
            c.execute("UPDATE tax_file SET matched=?, unmatched=?, conflicts=? WHERE id=?",
                      (counts["matched"], counts["unmatched"], counts["conflict"], fid))
        return counts

    @staticmethod
    def _asset_aliases(pf: Any) -> dict[str, set[str]]:
        out: dict[str, set[str]] = defaultdict(set)
        if pf is None:
            return out
        for a in pf.assets.values():
            for name in {a.asset_id, a.symbol, *(a.aliases or [])}:
                if name:
                    out[str(name).upper()].add(a.asset_id.upper())
        return out

    @staticmethod
    def _match_one(r: Any, by_id: dict[str, Any], by_ext: dict[str, list[Any]],
                   by_asset_day: dict[tuple[str, str], list[Any]], aliases: dict[str, set[str]]) \
            -> tuple[str, str | None, str | None, str | None]:
        ext = (r["external_id"] or "").strip().lower()
        if ext and by_ext.get(ext):
            txs = {t.tx_id: t for t in by_ext[ext]}
            if len(txs) == 1:
                return "matched", next(iter(txs)), "external_id", None
            return "conflict", None, "external_id", f"externe ID passt zu {len(txs)} Buchungen"
        tid = (r["transaction_id"] or "").strip()
        if tid and tid in by_id:
            return "matched", tid, "transaction_id", None
        if not (r["asset"] and r["disposal_date"]):
            return "unmatched", None, None, "ohne Asset/Datum keine Zuordnung möglich"
        assets = aliases.get(r["asset"].upper(), {r["asset"].upper()})
        qty = Decimal(r["quantity"]) if r["quantity"] else None
        val = Decimal(r["disposal_value"]) if r["disposal_value"] else None
        cands = []
        for aid in assets:
            for t in by_asset_day.get((aid, r["disposal_date"]), []):
                if t.from_asset is None or t.from_asset.upper() != aid or t.type in ("transfer",):
                    continue
                if qty is not None and (t.from_qty is None or abs(t.from_qty - qty) > max(abs(qty) * Decimal("1e-6"),
                                                                                           Decimal("1e-12"))):
                    continue
                if val is not None and t.value_eur is not None and abs(t.value_eur - val) > max(abs(val) / 100,
                                                                                               Decimal(1)):
                    continue
                cands.append(t)
        uniq = {t.tx_id: t for t in cands}
        if len(uniq) == 1:
            return "matched", next(iter(uniq)), "heuristic", "Asset, Datum, Menge" + (", Betrag" if val else "")
        if len(uniq) > 1:
            return "conflict", None, "heuristic", f"{len(uniq)} Buchungen passen (Asset, Datum, Menge)"
        return "unmatched", None, None, None

    # -- Ordner ---------------------------------------------------------------------------------------------
    def scan(self) -> dict[str, Any]:
        """Ordner prüfen (kein Dateiwächter): neue bzw. geänderte Dateien aufnehmen; eindeutige neue Jahre direkt
        aktivieren, für Jahre mit aktiver Datei nur vormerken (Ersetzen bestätigt der Nutzer)."""
        if not _LOCK.acquire(blocking=False):
            return {"skipped": "Prüfung läuft bereits"}
        try:
            return self._scan()
        finally:
            _LOCK.release()

    def _scan(self) -> dict[str, Any]:
        self.dir.mkdir(parents=True, exist_ok=True)
        seen = {r["path"]: r for r in self.db.q("SELECT * FROM tax_scan")}
        out: dict[str, Any] = {"checked": 0, "new": [], "pending": [], "rejected": [], "unchanged": 0}
        now = iso(datetime.now(UTC))
        for p in sorted(self.dir.iterdir()):
            if not p.is_file() or p.suffix.lower() not in SUFFIXES or p.name.startswith("."):
                continue
            out["checked"] += 1
            st = p.stat()
            mtime = datetime.fromtimestamp(st.st_mtime, UTC).isoformat()
            prev = seen.get(str(p))
            if prev is not None and prev["size"] == st.st_size and prev["mtime"] == mtime:
                out["unchanged"] += 1
                continue
            if datetime.now(UTC).timestamp() - st.st_mtime < 5:
                continue  # wird evtl. noch geschrieben – nächster Durchlauf
            data = p.read_bytes()
            fid, a = self.register(data, p.name, "folder", p)
            msg = None
            f = self.file(fid) if fid else None
            if f is not None and f["status"] == "pending" and f["tax_year"] is not None \
                    and self.active(int(f["tax_year"])) is None:
                self.activate(int(fid))  # type: ignore[arg-type]
                out["new"].append({"year": f["tax_year"], "file": p.name})
                msg = f"aktiviert für {f['tax_year']}"
            elif f is not None and f["status"] == "pending":
                out["pending"].append({"year": f["tax_year"], "file": p.name, "id": fid})
                msg = "wartet auf Bestätigung"
            elif f is not None and f["status"] == "rejected":
                out["rejected"].append({"file": p.name, "errors": a.errors[:3]})
                msg = "abgelehnt: " + "; ".join(a.errors[:2])
            self.db.x("INSERT INTO tax_scan(path, size, mtime, sha256, file_id, seen_at, message) "
                      "VALUES (?,?,?,?,?,?,?) "
                      "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime, "
                      "sha256=excluded.sha256, file_id=excluded.file_id, seen_at=excluded.seen_at, "
                      "message=excluded.message", (str(p), st.st_size, mtime, a.sha256, fid, now, msg))
        if out["new"] or out["pending"]:
            log.info("Steuerdaten-Ordner: %d neu, %d wartend", len(out["new"]), len(out["pending"]))
        return out

    # -- Export ---------------------------------------------------------------------------------------------
    def export_csv(self, fid: int) -> str:
        import csv
        import io

        cols = ["line", "tax_year", "transaction_id", "external_id", "asset", "quantity", "acquisition_date",
                "disposal_date", "acquisition_cost", "disposal_value", "holding_period_days", "taxable", "gain_loss",
                "tax_category", "source", "comment", "match_status", "match_tx", "match_method", "match_note"]
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(cols)
        for r in self.records(fid):
            w.writerow(["" if r[c] is None else r[c] for c in cols])
        return buf.getvalue()


def _s(v: Decimal | None) -> str | None:
    return None if v is None else format(v.normalize(), "f")


def _d(v: Any) -> str | None:
    return v.isoformat() if v is not None else None


def tax_import_service(ctx: Any) -> TaxImportService:
    return TaxImportService(ctx)
