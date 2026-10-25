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
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app import __version__
from app.context import AppContext
from app.prices.isin import isin_service
from app.progress import job_progress, view
from app.tax import registry
from app.tax.base import Overview, ReportMeta, RulePack, TaxInput, TaxResult
from app.tax.classify import classify_account, classify_asset, securities_accounts
from app.tax.pdf import render
from app.tax.rulesets import RulesetStore
from app.taxcert.service import taxcert_service
from app.util.timeutil import iso, now_local, today_local

log = logging.getLogger(__name__)
KEEP_REPORTS = 60
FUTURE_YEARS = 2  # Jahresauswahl: so viele Jahre nach dem laufenden Jahr
MIN_YEAR, MAX_YEAR = 2000, 2100
ALL_JOB = "tax_all"
ALL_ZIP_KEY = "tax_all_last"
ALL_LABEL = "Alle Steuerberichte"
_ALL_LOCK = threading.Lock()
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
    def pack_id(self) -> str:
        return registry.resolve_id(self.ctx.settings.get("tax.rulepack", "auto"), self.ctx.config.tz)

    def ruleset_slug(self) -> str:
        """Kennung der gewählten Regelwerk-Datei (Parameterstand); leer = Standard. Eine gelöschte Datei gilt nicht."""
        slug = str((self.ctx.settings.get("tax.ruleset") or {}).get(self.pack_id()) or "")
        return slug if slug and self.rulesets().path(slug) is not None else ""

    def rulesets(self) -> RulesetStore:
        pid = self.pack_id()
        cls = next((c for c in registry.available() if c.id == pid), None)
        schema = None
        if cls is not None:
            inst = registry.get(pid, self.ctx.config.tax_rules_dir)
            schema = inst.params_schema() if inst is not None else None
        return RulesetStore(self.ctx.config.tax_rules_dir, pid, registry.bundled_params(pid), schema)

    def set_ruleset(self, slug: str) -> bool:
        """Wählt den Parameterstand (``""`` = Standard). Unbekannte Kennungen werden abgelehnt."""
        if slug and self.rulesets().path(slug) is None:
            return False
        cur = dict(self.ctx.settings.get("tax.ruleset") or {})
        cur[self.pack_id()] = slug
        self.ctx.settings.set("tax.ruleset", cur)
        return True

    def save_ruleset(self, raw: bytes) -> Any:
        """Prüft und speichert eine Regelwerk-Datei; ein Neustand verwirft zwischengespeicherte Ergebnisse."""
        v = self.rulesets().save(raw, today_local().year)
        if v.ok:
            self.ctx.settings.set("tax.ruleset_rev", iso(datetime.now(UTC)))
        return v

    def delete_ruleset(self, slug: str) -> bool:
        if not self.rulesets().delete(slug):
            return False
        cur = dict(self.ctx.settings.get("tax.ruleset") or {})
        if cur.get(self.pack_id()) == slug:
            cur[self.pack_id()] = ""
            self.ctx.settings.set("tax.ruleset", cur)
        self.ctx.settings.set("tax.ruleset_rev", iso(datetime.now(UTC)))
        return True

    def pack(self) -> RulePack:
        slug = self.ruleset_slug()
        path = self.rulesets().path(slug) if slug else None
        pack = registry.resolve(self.ctx.settings.get("tax.rulepack", "auto"), self.ctx.config.tz,
                                self.ctx.config.tax_rules_dir, path)
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
        from app.assetchange.account_merge import active_map as account_map

        am = account_map(self.ctx.db)  # zusammengeführte Konten: Einstellung des Ziel-Kontos hat Vorrang
        if am:
            acc_ov = {**{am[k]: v for k, v in acc_ov.items() if k in am},
                      **{k: v for k, v in acc_ov.items() if k not in am}}
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
            certs=taxcert_service(self.ctx).for_tax(), isin_map=isin_service(self.ctx).mapping(),
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

    def year_choices(self, inp: TaxInput | None, selected: int) -> dict[str, Any]:
        """Auswahl der Steuerjahre: vom ersten Buchungsjahr bis zwei Jahre nach dem laufenden Jahr (und bis zum
        gewählten Jahr). Gruppen: mit Daten · ohne Daten · Zukunft (vorläufig)."""
        today = today_local()
        data = self.data_years(inp) if inp is not None else []
        first = data[0] if data else today.year
        if inp is not None and inp.ledger.first_date is not None:
            first = min(first, inp.ledger.first_date.year)
        last = max(today.year + FUTURE_YEARS, selected)
        lo = min(first, selected)  # ein früher gewähltes Jahr bleibt sichtbar
        have = set(data)
        past_with = [y for y in range(today.year, lo - 1, -1) if y in have]
        past_without = [y for y in range(today.year, lo - 1, -1) if y not in have]
        future = list(range(last, today.year, -1))
        return {"first": first, "last": last, "with_data": past_with, "without_data": past_without, "future": future}

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
            return inp, (self._year_result(pack, inp, year) if inp is not None else None)

        inp, res = self._cached(("year", pack.id, pack.version, year), run)
        return pack, inp, res

    def _year_result(self, pack: RulePack, inp: TaxInput, year: int) -> TaxResult:
        """Ergebnis eines Jahres mit den Vorträgen der berechneten Vorjahre (Verlustvortrag § 23: leere Eingabe =
        automatisch). Die Kette nutzt dieselbe Eingangsdatenbasis und je Vorjahr dessen eigene Eingaben."""

        def run() -> TaxResult:
            o = dict(self.options(pack, year))
            prev = [y for y in self.data_years(inp) if y < year]
            if prev:
                for key, val in pack.carry_out(self._year_result(pack, inp, prev[-1])).items():
                    o[f"{key}_auto"] = val
            return pack.compute(inp, year, o)

        return self._cached(("res", pack.id, pack.version, year), run)

    # -- Berichte ----------------------------------------------------------------------------------------------
    def generate(self, year: int, doc_ids: list[str] | None = None, keep_previous: bool = False) -> dict[str, Any]:
        """Erzeugt die gewählten PDFs. Standard: Die Berichte desselben Jahres und Regelwerks werden **ersetzt** (erst
        nach erfolgreicher Erzeugung); ``keep_previous`` behält sie."""
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
                          valuation_date=inp.import_meta.get("valuation_date"), profile=inp.profile,
                          ruleset=pack.params.ruleset_info(year) if pack.params.ruleset_applies(year) else None)
        stem = f"{created.strftime('%Y%m%d-%H%M%S')}-{_SAFE.sub('_', pack.id)}"
        rel_dir = Path("tax") / str(year) / stem
        n = 1
        while (self.ctx.config.reports_dir / rel_dir).exists():  # zwei Berichte in derselben Sekunde
            n += 1
            rel_dir = Path("tax") / str(year) / f"{stem}-{n}"
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
        summary.update({"docs": docs, "params_fingerprint": pack.params.fingerprint(year), "ruleset": meta.ruleset,
                        "created": iso(datetime.now(UTC))})
        rid = self.ctx.db.x(
            "INSERT INTO tax_report(year, rulepack, options_json, import_id, created_at, file_path, summary_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (year, f"{pack.id}@{pack.version}", json.dumps(res.options, default=str, sort_keys=True), inp.pf.import_id,
             iso(datetime.now(UTC)), rel_dir.as_posix(), json.dumps(summary, default=str)),
        ).lastrowid
        log.info("Steuerbericht %s erzeugt (%s, %d Dokumente)", year, pack.id, len(docs))
        replaced = 0 if keep_previous else self._replace_older(year, pack.id, rid)
        self._prune()
        return {"id": rid, "docs": docs, "year": year, "replaced": replaced}

    # -- Alle Jahre ----------------------------------------------------------------------------------------
    def report_years(self) -> list[int]:
        """Jahre, für die „Alle Jahre erzeugen“ Berichte anlegt: alle Jahre mit Daten (bis einschließlich laufendes)."""
        _, inp, _ = self.overview()
        return self.data_years(inp) if inp is not None else []

    def generate_all(self, prog: Any | None = None, doc_ids: list[str] | None = None) -> dict[str, Any]:
        """Erzeugt die Berichte aller Jahre mit Daten (je Jahr ersetzt die neue Fassung die alte) und packt sie in ein
        ZIP. Ein fehlerhaftes Jahr stoppt den Lauf nicht; es wird im Ergebnis genannt."""
        years = self.report_years()
        if not years:
            raise ValueError("Keine Jahre mit Daten – bitte zuerst Daten importieren.")
        prog = prog or job_progress(self.ctx, ALL_JOB, ALL_LABEL, unit="Jahre", phases=["tax_years", "tax_zip"])
        prog.phase("tax_years", total=len(years), text=f"{years[0]}–{years[-1]}")
        created: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for y in years:
            try:
                created.append(self.generate(y, doc_ids, keep_previous=False))
            except Exception as e:
                log.exception("Steuerbericht %s fehlgeschlagen", y)
                failed.append({"year": y, "error": f"{type(e).__name__}: {e}"[:200]})
            prog.advance(1, text=f"{y} erledigt")
        prog.phase("tax_zip", text="ZIP-Archiv erstellen")
        zip_rel = self._build_zip(created)
        out = {"years": [c["year"] for c in created], "failed": failed, "zip": zip_rel,
               "created": iso(datetime.now(UTC))}
        self.ctx.db.set_state(ALL_ZIP_KEY, out)
        prog.finish(not failed, result=f"{len(created)} Jahre erzeugt" + (f", {len(failed)} fehlgeschlagen"
                                                                          if failed else ""))
        return out

    def _build_zip(self, created: list[dict[str, Any]]) -> str:
        out_dir = self.ctx.config.reports_dir / "tax" / "all"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = now_local().strftime("%Y%m%d-%H%M%S")
        path = out_dir / f"Steuerberichte-{stamp}.zip"
        tmp = path.with_suffix(".tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for c in sorted(created, key=lambda c: c["year"]):
                for d in c["docs"]:
                    found = self.report_file(c["id"], d["id"])
                    if found is not None:
                        zf.write(found[0], f"{c['year']}/{found[1]}")
        tmp.replace(path)
        for old in out_dir.glob("Steuerberichte-*.zip"):  # nur das neueste Archiv behalten
            if old != path:
                old.unlink(missing_ok=True)
        return (Path("tax") / "all" / path.name).as_posix()

    def all_zip(self) -> tuple[Path, str] | None:
        """Neuestes ZIP aller Berichte (nur innerhalb des Berichtsordners)."""
        st = self.ctx.db.get_state(ALL_ZIP_KEY)
        rel = st.get("zip") if isinstance(st, dict) else None
        if not rel:
            return None
        base = self.ctx.config.reports_dir.resolve()
        path = (base / rel).resolve()
        if base not in path.parents or not path.is_file():
            return None
        return path, path.name

    def all_state(self) -> dict[str, Any]:
        st = self.ctx.db.get_state(ALL_ZIP_KEY)
        return st if isinstance(st, dict) else {}

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
        shared = self.ctx.db.q1("SELECT 1 FROM tax_report WHERE file_path=? AND id<>?", (r["file_path"], rid))
        if base in path.parents and path.is_dir() and shared is None:
            shutil.rmtree(path, ignore_errors=True)
        self.ctx.db.x("DELETE FROM tax_report WHERE id=?", (rid,))
        return True

    def _replace_older(self, year: int, pack_id: str, keep_id: int) -> int:
        """Löscht frühere Berichte desselben Jahres und Regelwerks (die neue Fassung ersetzt sie)."""
        n = 0
        for r in self.ctx.db.q("SELECT id FROM tax_report WHERE year=? AND rulepack LIKE ? AND id<>?",
                               (year, f"{pack_id}@%", keep_id)):
            n += self.delete_report(r["id"])
        return n

    def delete_year(self, year: int) -> int:
        """Löscht alle Berichte eines Jahres; Rückgabe = Anzahl."""
        n = 0
        for r in self.ctx.db.q("SELECT id FROM tax_report WHERE year=?", (year,)):
            n += self.delete_report(r["id"])
        return n

    def _prune(self) -> None:
        rows = self.ctx.db.q("SELECT id FROM tax_report ORDER BY id DESC LIMIT -1 OFFSET ?", (KEEP_REPORTS,))
        for r in rows:
            self.delete_report(r["id"])


def all_running(ctx: AppContext) -> dict[str, Any] | None:
    """Anzeige des laufenden Sammellaufs (oder None)."""
    st = ctx.job_status().get(ALL_JOB) or {}
    if not st.get("running"):
        return None
    p = view({**(st.get("progress") or {}), "running": True, "label": ALL_LABEL})
    return None if p.get("stale") else p


def start_all(ctx: AppContext, *, wait: bool = False) -> bool:
    """Startet „Alle Jahre erzeugen“ im Hintergrund; ``False``, wenn bereits einer läuft."""
    if not _ALL_LOCK.acquire(blocking=False):
        return False
    svc = tax_service(ctx)
    prog = job_progress(ctx, ALL_JOB, ALL_LABEL, unit="Jahre", phases=["tax_years", "tax_zip"])

    def work() -> None:
        try:
            svc.generate_all(prog)
        except Exception as e:
            log.exception("Alle Steuerberichte fehlgeschlagen")
            prog.finish(False, result=f"{type(e).__name__}: {e}"[:300])
        finally:
            _ALL_LOCK.release()

    if wait:
        work()
    else:
        threading.Thread(target=work, name="tax-all", daemon=True).start()
    return True


_services: dict[int, TaxService] = {}


def tax_service(ctx: AppContext) -> TaxService:
    svc = _services.get(id(ctx))
    if svc is None:
        svc = TaxService(ctx)
        _services[id(ctx)] = svc
    return svc
