"""Steuer-Service: stellt Eingangsdaten zusammen, verwaltet Optionen, erzeugt und archiviert PDF-Berichte.

Die Berechnung selbst liegt vollständig im gewählten Regelwerk (``app.tax.packs.*``).
Es werden keine externen Dienste angefragt – nur lokale Kurs- und Importdaten.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import threading
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app import __version__
from app.context import AppContext
from app.tax import registry
from app.tax.base import Overview, ReportMeta, RulePack, TaxInput, TaxResult
from app.tax.classify import classify_account, classify_asset, securities_accounts
from app.tax.pdf import render
from app.util.timeutil import iso, now_local, today_local

log = logging.getLogger(__name__)
KEEP_REPORTS = 60
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _dec(v: float | None) -> Decimal | None:
    if v is None:
        return None
    return Decimal(str(v))


class TaxService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._lock = threading.Lock()
        self._cache: dict[Any, Any] = {}

    # -- Regelwerk & Optionen ------------------------------------------------------------------------
    def pack(self) -> RulePack:
        pack = registry.resolve(self.ctx.settings.get("tax.rulepack", "auto"), self.ctx.config.tz,
                                self.ctx.config.tax_rules_dir)
        assert pack is not None
        return pack

    def options(self, pack: RulePack, year: int | None = None) -> dict[str, Any]:
        stored = (self.ctx.settings.get("tax.options") or {}).get(pack.id, {})
        out = {**pack.defaults(), **(stored.get("global") or {})}
        if year is not None:
            out.update((stored.get("years") or {}).get(str(year)) or {})
        return out

    def save_options(self, pack: RulePack, year: int, values: dict[str, Any]) -> None:
        all_opts = self.ctx.settings.get("tax.options") or {}
        mine = all_opts.get(pack.id) or {}
        glob = dict(mine.get("global") or {})
        years = dict(mine.get("years") or {})
        per = dict(years.get(str(year)) or {})
        for spec in pack.option_specs():
            if spec.key not in values:
                continue
            (per if spec.per_year else glob)[spec.key] = values[spec.key]
        years[str(year)] = per
        all_opts[pack.id] = {"global": glob, "years": years}
        self.ctx.settings.set("tax.options", all_opts)

    # -- Eingangsdaten -------------------------------------------------------------------------------------
    def _series_price(self, asset_id: str, d: date) -> Decimal | None:
        pf = self.ctx.portfolio()
        if pf is None:
            return None
        a = pf.asset(asset_id)
        series = self.ctx.prices.series_for(a)
        if series:
            row = self.ctx.store.close_on_or_before(series, d)
            if row is not None:
                return self._to_eur(Decimal(str(row["close"])), row["ccy"], d)
        manual = [p for p in pf.manual_prices.get(asset_id, []) if p[0] <= d]
        if manual:
            return Decimal(str(max(manual)[1]))
        return None

    def _to_eur(self, price: Decimal, ccy: str | None, d: date) -> Decimal | None:
        if not ccy or ccy.upper() == "EUR":
            return price
        fx = self.ctx.store.fx_on_or_before(ccy, d)
        if not fx or not fx[0]:
            return None
        return price / Decimal(str(fx[0]))

    def _year_prices(self, asset_id: str, year: int) -> tuple[tuple[date, Decimal] | None, tuple[date, Decimal] | None]:
        pf = self.ctx.portfolio()
        if pf is None:
            return None, None
        series = self.ctx.prices.series_for(pf.asset(asset_id))
        if not series:
            return None, None
        rows = self.ctx.store.daily_range(series, date(year, 1, 1), date(year, 12, 31))
        if not rows:
            return None, None

        def conv(r: Any) -> tuple[date, Decimal] | None:
            d = date.fromisoformat(r["date"])
            v = self._to_eur(Decimal(str(r["close"])), r["ccy"], d)
            return (d, v) if v is not None else None

        first, last = conv(rows[0]), conv(rows[-1])
        # Nur vollständige Jahre (bzw. Kurse ab Jahresbeginn) sind für die Vorabpauschale belastbar
        if first and first[0] > date(year, 1, 15):
            first = None
        if last and last[0] < date(year, 12, 15) and year < today_local().year:
            last = None
        return first, last

    def _fx(self, ccy: str, d: date) -> Decimal | None:
        fx = self.ctx.store.fx_on_or_before(ccy, d)
        if not fx or not fx[0]:
            return None
        return Decimal(1) / Decimal(str(fx[0]))

    def ledger(self, pack: RulePack, options: dict[str, Any]) -> Any:
        """Ledger mit den Optionen des Regelwerks (inkl. Jahresend-Snapshots) – geteilt von Steuer- und Detailseite."""
        base_led = self.ctx.ledger()
        if base_led is None:
            return None
        today = today_local()
        first = base_led.first_date.year if base_led.first_date else today.year
        return self.ctx.ledger(opts=pack.engine_options(self.ctx.engine_options(), options,
                                                        list(range(first, today.year))))

    def build_input(self, pack: RulePack, options: dict[str, Any]) -> TaxInput | None:
        pf = self.ctx.portfolio()
        led = self.ledger(pack, options)
        if pf is None or led is None:
            return None
        today = today_local()
        s = self.ctx.settings
        types_ov = s.get("tax.asset_types") or {}
        acc_ov = s.get("tax.account_withholding") or {}
        asset_types, type_src = {}, {}
        for aid, a in pf.assets.items():
            asset_types[aid], type_src[aid] = classify_asset(a, types_ov)
        sec_accounts = set(securities_accounts(pf))
        kinds, kind_src = {}, {}
        for acc in pf.all_accounts():
            k, src = classify_account(pf, acc, acc_ov)
            if src == "default" and acc not in sec_accounts:
                k = "foreign"  # Krypto-Börsen/Wallets: kein inländischer Steuerabzug
            kinds[acc], kind_src[acc] = k, src
        prices = {aid: _dec(pi.price_eur) for aid, pi in self.ctx.price_infos(pf, led).items() if pi.valued}
        imp = self.ctx.db.q1("SELECT id, filename, file_sha256, valuation_date FROM imports WHERE id=?",
                             (pf.import_id,)) if pf.import_id else None
        return TaxInput(
            pf=pf, ledger=led, today=today, asset_types=asset_types, asset_type_source=type_src,
            account_kinds=kinds, account_kind_source=kind_src,
            current_prices={k: v for k, v in prices.items() if v is not None},
            price_eur=self._series_price, year_prices=self._year_prices, fx_eur=self._fx,
            import_meta=dict(imp) if imp else {}, profile=s.get("tax.profile") or {},
        )

    # -- Berechnung (gecacht) ------------------------------------------------------------------------------
    def _base(self) -> tuple:
        return (self.ctx.active_import_id(), self.ctx.prices.version, self.ctx.settings.version,
                self.ctx.history_version, self.ctx.data_version, today_local())

    def _cached(self, key: tuple, fn: Any) -> Any:
        """Cache nur für den aktuellen Datenstand – ändert sich Import, Kurse oder Einstellung, wird alles verworfen
        (begrenzt den Speicher auf einen Ledger-Stand)."""
        base = self._base()
        with self._lock:
            if self._cache.get("__base__") != base:
                self._cache = {"__base__": base}
            if key in self._cache:
                return self._cache[key]
        val = fn()
        with self._lock:
            if self._cache.get("__base__") == base and len(self._cache) < 40:
                self._cache[key] = val
        return val

    def data_years(self, inp: TaxInput) -> list[int]:
        ys = {d.date.year for d in inp.ledger.disposals} | {e.date.year for e in inp.ledger.income}
        ys |= {t.date.year for t in inp.ledger.taxes}
        if inp.ledger.lots:
            ys.add(inp.today.year)
        return sorted(ys)

    def overview(self) -> tuple[RulePack, TaxInput | None, Overview | None]:
        pack = self.pack()
        opts = self.options(pack)

        def run() -> tuple[TaxInput | None, Overview | None]:
            inp = self.build_input(pack, opts)
            return inp, (pack.overview(inp, opts) if inp is not None else None)

        inp, ov = self._cached(("overview", pack.id, pack.version), run)
        return pack, inp, ov

    def compute(self, year: int) -> tuple[RulePack, TaxInput | None, TaxResult | None]:
        pack = self.pack()
        opts = self.options(pack, year)

        def run() -> tuple[TaxInput | None, TaxResult | None]:
            inp = self.build_input(pack, opts)
            return inp, (pack.compute(inp, year, opts) if inp is not None else None)

        inp, res = self._cached(("year", pack.id, pack.version, year), run)
        return pack, inp, res

    # -- Berichte ----------------------------------------------------------------------------------------------
    def generate(self, year: int, doc_ids: list[str] | None = None) -> dict[str, Any]:
        pack, inp, res = self.compute(year)
        if inp is None or res is None:
            raise ValueError("Kein aktiver Import – bitte zuerst Daten importieren.")
        specs = {d.id: d for d in pack.documents()}
        wanted = [d for d in (doc_ids or list(specs)) if d in specs]
        if not wanted:
            raise ValueError("Kein Dokument ausgewählt.")
        created = now_local()
        meta = ReportMeta(created_at=created, app_version=__version__, import_id=inp.pf.import_id,
                          import_file=inp.import_meta.get("filename"), import_sha=inp.import_meta.get("file_sha256"),
                          valuation_date=inp.import_meta.get("valuation_date"), profile=inp.profile)
        rel_dir = Path("tax") / str(year) / f"{created.strftime('%Y%m%d-%H%M%S')}-{_SAFE.sub('_', pack.id)}"
        out_dir = self.ctx.config.reports_dir / rel_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        docs = []
        try:
            for did in wanted:
                doc = pack.build_document(did, res, meta)
                path = render(doc, out_dir / f"{_SAFE.sub('_', did)}.pdf", header_right=f"Portfolia · {year}")
                blob = path.read_bytes()
                docs.append({"id": did, "title": specs[did].title, "file": path.name, "download": doc.filename,
                             "bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()})
        except Exception:
            shutil.rmtree(out_dir, ignore_errors=True)
            raise
        summary = res.summary_json()
        summary.update({"docs": docs, "params_fingerprint": pack.params.fingerprint(year),
                        "created": iso(datetime.now(UTC))})
        rid = self.ctx.db.x(
            "INSERT INTO tax_report(year, rulepack, options_json, import_id, created_at, file_path, summary_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (year, f"{pack.id}@{pack.version}", json.dumps(res.options, default=str, sort_keys=True), inp.pf.import_id,
             iso(datetime.now(UTC)), rel_dir.as_posix(), json.dumps(summary, default=str)),
        ).lastrowid
        log.info("Steuerbericht %s erzeugt (%s, %d Dokumente)", year, pack.id, len(docs))
        self._prune()
        return {"id": rid, "docs": docs, "year": year}

    def reports(self, limit: int = 30) -> list[dict[str, Any]]:
        out = []
        for r in self.ctx.db.q("SELECT * FROM tax_report ORDER BY id DESC LIMIT ?", (limit,)):
            d = dict(r)
            d["summary"] = json.loads(r["summary_json"] or "{}")
            out.append(d)
        return out

    def report_file(self, rid: int, doc_id: str) -> tuple[Path, str] | None:
        r = self.ctx.db.q1("SELECT file_path, summary_json FROM tax_report WHERE id=?", (rid,))
        if r is None:
            return None
        summary = json.loads(r["summary_json"] or "{}")
        doc = next((d for d in summary.get("docs", []) if d.get("id") == doc_id), None)
        if doc is None:
            return None
        base = self.ctx.config.reports_dir.resolve()
        path = (base / r["file_path"] / doc["file"]).resolve()
        if base not in path.parents or not path.is_file():  # nur Dateien innerhalb des Berichtsordners
            return None
        return path, doc.get("download") or doc["file"]

    def delete_report(self, rid: int) -> bool:
        r = self.ctx.db.q1("SELECT file_path FROM tax_report WHERE id=?", (rid,))
        if r is None:
            return False
        base = self.ctx.config.reports_dir.resolve()
        path = (base / r["file_path"]).resolve()
        if base in path.parents and path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        self.ctx.db.x("DELETE FROM tax_report WHERE id=?", (rid,))
        return True

    def _prune(self) -> None:
        rows = self.ctx.db.q("SELECT id FROM tax_report ORDER BY id DESC LIMIT -1 OFFSET ?", (KEEP_REPORTS,))
        for r in rows:
            self.delete_report(r["id"])


_services: dict[int, TaxService] = {}


def tax_service(ctx: AppContext) -> TaxService:
    svc = _services.get(id(ctx))
    if svc is None:
        svc = TaxService(ctx)
        _services[id(ctx)] = svc
    return svc
