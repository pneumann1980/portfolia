"""Journal: in der App erfasste Buchungen und Assets (manuell oder per CSV-Import aus Börsen/Wallets).

Journal-Buchungen sind – anders als Sparplan-Schätzungen – vollwertige Buchungen ohne Markierung. Sie liegen in
``journal_tx`` und werden mit dem aktiven Import zu einem Portfolio zusammengeführt (``AppContext.recorded_portfolio``):

* enthält der Import eine Buchung mit derselben ``tx_id`` (z. B. nach Übernahme des Gesamtexports in den kuratierten
  Import), gilt die Import-Buchung und die Journal-Buchung wird nicht mehr gezählt,
* Assets aus dem Import haben Vorrang vor gleichnamigen Journal-Assets,
* Buchungen werden mit denselben Regeln geprüft wie der Import (gemeinsamer Validator),
* jede Änderung wird protokolliert (``journal_log``); Löschen ist umkehrbar.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app import __version__
from app.importer import contract as C
from app.importer.validate import validate_tx_rows
from app.importer.zipbuilder import build_zip
from app.journal import forms
from app.ledger.engine import DUST, run_ledger
from app.ledger.models import AssetInfo, Portfolio, Tx
from app.prices.lookup import fx_to_eur, price_eur_on
from app.util.timeutil import iso, local_tz, parse_iso, to_local_date, today_local

log = logging.getLogger(__name__)

SOURCE_LABEL = {"manual": "manuell", "transfer": "Transfer-Abgleich"}
TX_PREFIX = {"manual": "PF-M-", "csv": "PF-C-", "transfer": "PF-T-"}
EDITABLE_SOURCES = ("manual", "transfer")
SEQ_BASE = 2_000_000
TAX_TYPES = {"share": "Aktie", "etf_equity": "Aktienfonds (≥ 51 % Aktien)", "etf_mixed": "Mischfonds (≥ 25 % Aktien)",
             "etf_other": "sonstiger Fonds", "fund_realestate": "Immobilienfonds",
             "fund_realestate_foreign": "Auslands-Immobilienfonds", "bond": "Anleihe", "other": "Sonstiges"}
_ASSET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._#/-]{0,39}$")
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
_WKN_RE = re.compile(r"^[A-Z0-9]{6}$")
_CG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_YAHOO_RE = re.compile(r"^[A-Za-z0-9.^=_-]{1,24}$")
_PREFIX_RE = re.compile(r"^[A-Za-z0-9-]+: ")


def source_label(source: str | None) -> str:
    """Anzeigename der Herkunft einer Journal-Buchung (manuell, CSV-Profil, Transfer-Abgleich)."""
    src = source or ""
    if src in SOURCE_LABEL:
        return SOURCE_LABEL[src]
    if src.startswith("csv:"):
        try:
            from app.csvimport.profiles import PROFILES

            p = PROFILES.get(src[4:])
            name = p.label.split(" (", 1)[0] if p else "eigenes Format"
        except ImportError:  # pragma: no cover
            name = src[4:]
        return f"CSV · {name}"
    return src


def tx_prefix(source: str) -> str:
    return TX_PREFIX["csv"] if source.startswith("csv:") else TX_PREFIX.get(source, "PF-X-")


def editable(row: Any) -> bool:
    src = row["source"] or ""
    return row["status"] == "active" and not row["group_ref"] and (src in EDITABLE_SOURCES or src.startswith("csv:"))


def _d(v: Any) -> Decimal | None:
    return Decimal(str(v)) if v not in (None, "") else None


def _now() -> str:
    return iso(datetime.now(UTC)) or ""


@dataclass
class SaveResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tx_ids: list[str] = field(default_factory=list)
    asset_id: str | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


# ----------------------------------------------------------------------------------------------------
# Overlay: Journal → Portfolio
# ----------------------------------------------------------------------------------------------------

def journal_asset_infos(db: Any) -> dict[str, AssetInfo]:
    out: dict[str, AssetInfo] = {}
    for r in db.q("SELECT * FROM journal_asset ORDER BY asset_id"):
        out[r["asset_id"]] = AssetInfo(
            asset_id=r["asset_id"], name=r["name"], asset_class=r["asset_class"], quote_source=r["quote_source"],
            quote_id=r["quote_id"], category=r["category"],
            aliases=[a.strip() for a in (r["aliases"] or "").split(";") if a.strip()], wkn=r["wkn"], isin=r["isin"],
            note=r["note"], extra=json.loads(r["extra_json"] or "{}"))
    return out


def row_to_tx(r: Any) -> Tx:
    ts = parse_iso(r["ts_utc"])
    assert ts is not None
    return Tx(seq=SEQ_BASE + int(r["id"]), tx_id=r["tx_id"], ts=ts, date=to_local_date(ts),
              date_only=bool(r["date_only"]), type=r["type"], tag=r["tag"], from_account=r["from_account"],
              from_asset=r["from_asset"], from_qty=_d(r["from_qty"]), to_account=r["to_account"],
              to_asset=r["to_asset"], to_qty=_d(r["to_qty"]), fee_asset=r["fee_asset"], fee_qty=_d(r["fee_qty"]),
              fee_eur=_d(r["fee_eur"]), value_eur=_d(r["value_eur"]), orig_price=r["orig_price"],
              orig_ccy=r["orig_ccy"], source=r["source"], source_ref=r["external_id"], flag=None, note=r["note"],
              related_asset=r["related_asset"], origin="journal")


def overlay(db: Any, base: Portfolio | None) -> tuple[dict[str, AssetInfo], list[Tx]]:
    """Journal-Assets (sofern nicht im Import definiert) und aktive Journal-Buchungen (sofern nicht im Import)."""
    base_assets = base.assets if base is not None else {}
    base_ids = {t.tx_id for t in base.txs} if base is not None else set()
    assets = {aid: a for aid, a in journal_asset_infos(db).items() if aid not in base_assets}
    txs = [row_to_tx(r) for r in db.q("SELECT * FROM journal_tx WHERE status='active' ORDER BY ts_utc, id")
           if r["tx_id"] not in base_ids]
    known = set(base_assets) | set(assets)
    for t in txs:
        for aid in (t.from_asset, t.to_asset, t.fee_asset):
            if aid and aid not in known and aid in C.ISO_CURRENCIES:
                assets[aid] = AssetInfo(asset_id=aid, name=aid, asset_class="fiat", category="Cash",
                                        note="implizit ergänzt")
                known.add(aid)
    return assets, txs


def _strip(msg: str) -> str:
    """Validator-Meldung ohne vorangestellte (temporäre) tx_id."""
    return _PREFIX_RE.sub("", msg, count=1)


def _sig_match(a: Tx, b: Tx) -> bool:
    if (a.type, a.from_account, a.from_asset, a.to_account, a.to_asset) != \
            (b.type, b.from_account, b.from_asset, b.to_account, b.to_asset):
        return False
    if abs((a.date - b.date).days) > 2:
        return False
    for qa, qb in ((a.from_qty, b.from_qty), (a.to_qty, b.to_qty)):
        if qa is None and qb is None:
            continue
        if qa is None or qb is None or abs(qa - qb) > max(abs(qb) * Decimal("0.01"), Decimal("1e-8")):
            return False
    return True


# ----------------------------------------------------------------------------------------------------
# Service
# ----------------------------------------------------------------------------------------------------

class JournalService:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.db = ctx.db

    # -- Stammdaten fürs Formular -----------------------------------------------------------------------
    def known_assets(self) -> dict[str, AssetInfo]:
        pf = self.ctx.recorded_portfolio()
        assets: dict[str, AssetInfo] = dict(pf.assets) if pf is not None else {}
        for aid, a in journal_asset_infos(self.db).items():
            assets.setdefault(aid, a)
        return assets

    def known_accounts(self) -> list[str]:
        pf = self.ctx.recorded_portfolio()
        return pf.all_accounts() if pf is not None else []

    def _valuers(self) -> tuple[forms.PriceFn, forms.FxFn]:
        pf = self.ctx.recorded_portfolio() or Portfolio(import_id=None, txs=[], assets=self.known_assets(),
                                                         accounts={})
        today = today_local()

        def price(asset_id: str, d: date) -> tuple[Decimal, str] | None:
            p = price_eur_on(self.ctx, pf, asset_id, d, today)
            return (p[0], p[1]) if p else None

        def fx(amount: Decimal, ccy: str, d: date) -> tuple[Decimal, str] | None:
            if ccy.upper() == "EUR":
                return amount, "Eingabe"
            return fx_to_eur(self.ctx, amount, ccy, d)

        return price, fx

    # -- Lesen ------------------------------------------------------------------------------------------
    def get(self, tx_id: str) -> Any:
        return self.db.q1("SELECT * FROM journal_tx WHERE tx_id=?", (tx_id,))

    def in_import(self, tx_id: str) -> bool:
        base = self.ctx.base_portfolio()
        return base is not None and any(t.tx_id == tx_id for t in base.txs)

    def members(self, tx_id: str, status: str = "active") -> list[Any]:
        return self.db.q("SELECT * FROM journal_tx WHERE group_ref=? AND status=? ORDER BY id", (tx_id, status))

    def form_data(self, row: Any) -> dict[str, str]:
        """Formularwerte einer gespeicherten Buchung (zum Bearbeiten/Kopieren)."""
        data = json.loads(row["form_json"] or "{}")
        if not data:  # ohne Formular (z. B. synchronisiert) → Expertenmodus
            data = tx_form_data(row_to_tx(row))
        return {k: str(v) for k, v in data.items() if v is not None}

    def deleted(self, limit: int = 50) -> list[Any]:
        return self.db.q("SELECT * FROM journal_tx WHERE status='deleted' AND group_ref IS NULL "
                         "AND source <> 'transfer' ORDER BY updated_at DESC LIMIT ?", (limit,))

    def assets(self) -> list[dict[str, Any]]:
        pf = self.ctx.recorded_portfolio()
        base = self.ctx.base_portfolio()
        imported = set(base.assets) if base is not None else set()
        used = {a for t in (pf.txs if pf else []) for a in (t.from_asset, t.to_asset, t.fee_asset) if a}
        out = []
        for aid, a in journal_asset_infos(self.db).items():
            out.append({"a": a, "in_import": aid in imported, "used": aid in used,
                        "tax_type": TAX_TYPES.get(a.extra.get("tax_type", ""), "")})
        return out

    def duplicates(self, pf: Portfolio | None = None) -> dict[str, list[str]]:
        """Manuelle Buchungen, die einer Import-Buchung stark ähneln (gleiche Konten/Assets, ±2 Tage, Menge ±1 %).

        CSV-Importe prüfen Dubletten bereits in der Vorschau (strenger: gleicher Zeitpunkt ± Zeitzonenversatz) –
        die grobe Prüfung hier würde regelmäßige Erträge (z. B. tägliche Staking-Rewards) fälschlich markieren.
        """
        pf = pf or self.ctx.recorded_portfolio()
        if pf is None:
            return {}
        index: dict[tuple[Any, ...], list[Tx]] = defaultdict(list)
        for t in pf.txs:
            if t.origin == "import":
                index[(t.type, t.from_asset, t.to_asset)].append(t)
        out: dict[str, list[str]] = {}
        if not index:
            return out
        for j in pf.txs:
            if j.origin != "journal" or (j.source or "manual") != "manual":
                continue
            hits = [t.tx_id for t in index.get((j.type, j.from_asset, j.to_asset), []) if _sig_match(j, t)]
            if hits:
                out[j.tx_id] = hits
        return out

    def listing(self, *, account: str = "", asset: str = "", origin: str = "", typ: str = "", year: str = "",
                q: str = "", limit: int = 200, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        pf = self.ctx.portfolio()
        if pf is None:
            return [], 0
        q_low = q.strip().lower()
        sel = []
        for t in pf.txs:
            if account and account not in (t.from_account, t.to_account):
                continue
            if asset and asset not in (t.from_asset, t.to_asset, t.fee_asset, t.related_asset):
                continue
            kind = "plan_est" if t.flag == "estimated" else ("plan" if t.origin == "plan" else t.origin)
            if kind == "journal" and ((t.source or "").startswith("csv:") or t.source == "transfer"):
                kind = "csv"
            if origin and origin != kind and not (origin == "plan" and kind == "plan_est"):
                continue
            if typ and t.type != typ:
                continue
            if year and str(t.date.year) != year:
                continue
            if q_low and q_low not in t.tx_id.lower() and q_low not in (t.note or "").lower():
                continue
            sel.append((t, kind))
        sel.sort(key=lambda x: (x[0].ts, x[0].seq), reverse=True)
        dups = self.duplicates(self.ctx.recorded_portfolio())
        rows = [{"t": t, "kind": k, "label": forms.label_for(t), "dups": dups.get(t.tx_id, [])}
                for t, k in sel[offset:offset + limit]]
        return rows, len(sel)

    def meta(self, tx_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Journal-Eigenschaften (Quelle, Gruppe, CSV-Import) für die Anzeige einer Seite von Buchungen."""
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(tx_ids), 500):
            chunk = tx_ids[i:i + 500]
            ph = ",".join("?" * len(chunk))
            sql = f"SELECT tx_id, source, group_ref, batch_id, status FROM journal_tx WHERE tx_id IN ({ph})"
            for r in self.db.q(sql, chunk):
                out[r["tx_id"]] = {"source": r["source"], "group_ref": r["group_ref"], "batch_id": r["batch_id"],
                                   "editable": editable(r)}
        return out

    def years(self) -> list[int]:
        pf = self.ctx.portfolio()
        return sorted({t.date.year for t in pf.txs}, reverse=True) if pf else []

    # -- Buchungen speichern ----------------------------------------------------------------------------
    def save(self, data: Mapping[str, Any], tx_id: str | None = None) -> SaveResult:
        kind = str(data.get("kind") or "")
        existing = self.get(tx_id) if tx_id else None
        if tx_id and (existing is None or not editable(existing)):
            return SaveResult(errors=["Buchung nicht gefunden oder nicht bearbeitbar."])
        if tx_id and self.in_import(tx_id):
            return SaveResult(errors=["Diese Buchung ist inzwischen im Import enthalten (gleiche ID) – Änderungen "
                                      "bitte im Import vornehmen."])
        assets = self.known_assets()
        price, fx = self._valuers()
        draft = forms.build(kind, data, assets, price, fx, today_local())
        if draft.errors:
            return SaveResult(errors=draft.errors)
        classes = {aid: {"asset_class": a.asset_class} for aid, a in assets.items()}
        for r in draft.rows:
            for col in ("from_asset", "to_asset", "fee_asset", "related_asset"):
                code = r.get(col)
                if code and code not in classes and code in C.ISO_CURRENCIES:
                    classes[code] = {"asset_class": "fiat"}
        rep, parsed = validate_tx_rows([{**r, "tx_id": f"PF-PRUEFUNG-{i}"} for i, r in enumerate(draft.rows)],
                                       classes)
        if rep.errors:
            return SaveResult(errors=list(dict.fromkeys(_strip(m.message) for m in rep.errors)))
        # „fee_eur fehlt“ des Validators ist bereits als eigener Hinweis (kein Kurs für die Gebühr) enthalten
        res = SaveResult(warnings=draft.warnings + [_strip(m.message) for m in rep.warnings
                                                    if not (m.code == "fee_eur" and draft.warnings)])
        form_json = json.dumps({k: str(data.get(k)).strip() for k in forms.FORM_FIELDS
                                if data.get(k) not in (None, "")}, ensure_ascii=False)
        stamp = _now()
        with self.db.transaction() as c:
            if existing is None:
                main_id = self._insert(c, parsed[0], draft.value_sources[0], form_json, stamp, None)
            else:
                main_id = existing["tx_id"]
                self._update(c, existing, parsed[0], draft.value_sources[0], form_json, stamp)
            res.tx_ids.append(main_id)
            members = [] if existing is None else [dict(m) for m in c.execute(
                "SELECT * FROM journal_tx WHERE group_ref=? AND status='active' ORDER BY id", (main_id,))]
            for i, p in enumerate(parsed[1:], start=1):
                if members:
                    m = members.pop(0)
                    self._update(c, m, p, draft.value_sources[i], None, stamp)
                    res.tx_ids.append(m["tx_id"])
                else:
                    res.tx_ids.append(self._insert(c, p, draft.value_sources[i], None, stamp, main_id))
            for m in members:  # nicht mehr benötigte Teilbuchungen (z. B. Quellensteuer entfernt)
                c.execute("UPDATE journal_tx SET status='replaced', updated_at=? WHERE id=?", (stamp, m["id"]))
                self._log(c, "delete", m["tx_id"], m, None, stamp)
        self._after_change()
        dups = self.duplicates()
        for tid in res.tx_ids:
            if tid in dups:
                res.warnings.append(f"{tid} ähnelt der Import-Buchung {', '.join(dups[tid])} – bitte auf Dublette "
                                    "prüfen.")
        res.warnings += self._negative_balances(draft.rows, assets)
        return res

    def _negative_balances(self, rows: list[dict[str, str]], assets: Mapping[str, AssetInfo]) -> list[str]:
        """Hinweis, wenn ein Abgang den Bestand eines Kontos ins Minus drückt (fehlende frühere Buchung?)."""
        led = self.ctx.ledger()
        if led is None:
            return []
        out = []
        for r in rows:
            acc_from = r.get("from_account")
            for acc, asset in ((acc_from, r.get("from_asset")), (acc_from, r.get("fee_asset"))):
                if not acc or not asset or (asset in assets and assets[asset].is_fiat) or asset in C.ISO_CURRENCIES:
                    continue
                q = led.balances.get((acc, asset))
                if q is not None and q < -DUST:
                    out.append(f"Bestand {asset} auf „{acc}“ ist negativ ({forms.s(q)}) – fehlt eine frühere Buchung "
                               "(Kauf, Einzahlung, Übertrag)?")
        return list(dict.fromkeys(out))

    def _values(self, p: dict[str, Any], value_source: str | None) -> dict[str, Any]:
        def s(v: Decimal | None) -> str | None:
            return forms.s(v) if v is not None else None

        return {
            "ts_utc": iso(p["ts_utc"]), "date_only": int(p["date_only"]), "type": p["type"], "tag": p["tag"],
            "from_account": p["from_account"], "from_asset": p["from_asset"], "from_qty": s(p["from_qty"]),
            "to_account": p["to_account"], "to_asset": p["to_asset"], "to_qty": s(p["to_qty"]),
            "fee_asset": p["fee_asset"], "fee_qty": s(p["fee_qty"]), "fee_eur": s(p["fee_eur"]),
            "value_eur": s(p["value_eur"]), "value_source": value_source, "orig_price": p["orig_price"],
            "orig_ccy": p["orig_ccy"], "related_asset": p["related_asset"], "note": p["note"],
        }

    def _insert(self, c: Any, p: dict[str, Any], value_source: str | None, form_json: str | None, stamp: str,
                group_ref: str | None, source: str = "manual", *, external_id: str | None = None,
                batch_id: int | None = None, status: str = "active", pair_refs: str | None = None,
                log: bool = True) -> str:
        vals = self._values(p, value_source)
        prefix = tx_prefix(source)
        tmp = f"{prefix}NEU-{datetime.now(UTC).timestamp()}-{id(p)}"
        cols = ["tx_id", "source", "external_id", "group_ref", "status", *vals, "form_json", "created_at",
                "updated_at", "batch_id", "pair_refs"]
        cur = c.execute(f"INSERT INTO journal_tx({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        (tmp, source, external_id, group_ref, status, *vals.values(), form_json, stamp, stamp,
                         batch_id, pair_refs))
        tx_id = f"{prefix}{cur.lastrowid:06d}"
        c.execute("UPDATE journal_tx SET tx_id=? WHERE id=?", (tx_id, cur.lastrowid))
        if log:
            self._log(c, "create", tx_id, None, {"tx_id": tx_id, **vals}, stamp)
        return tx_id

    def _update(self, c: Any, row: Any, p: dict[str, Any], value_source: str | None, form_json: str | None,
                stamp: str) -> None:
        vals = self._values(p, value_source)
        before = {k: row[k] for k in ("tx_id", *vals)}
        sets = ", ".join(f"{k}=?" for k in vals)
        c.execute(f"UPDATE journal_tx SET {sets}, form_json=COALESCE(?, form_json), updated_at=? WHERE id=?",
                  (*vals.values(), form_json, stamp, row["id"]))
        self._log(c, "update", row["tx_id"], before, {"tx_id": row["tx_id"], **vals}, stamp)

    @staticmethod
    def _log(c: Any, action: str, ref: str, before: Any, after: Any, stamp: str) -> None:
        c.execute("INSERT INTO journal_log(at, action, ref, before_json, after_json) VALUES (?,?,?,?,?)",
                  (stamp, action, ref, json.dumps(dict(before), ensure_ascii=False, default=str) if before else None,
                   json.dumps(after, ensure_ascii=False, default=str) if after else None))

    def delete(self, tx_id: str) -> bool:
        row = self.get(tx_id)
        if row is not None and row["source"] == "transfer":
            return self.unpair(tx_id)
        return self._set_status(tx_id, "active", "deleted", "delete")

    def unpair(self, tx_id: str) -> bool:
        """Abgeglichenen Transfer auflösen: die beiden Einzelbuchungen (Ab- und Zugang) gelten wieder."""
        row = self.get(tx_id)
        if row is None or row["source"] != "transfer" or row["status"] not in ("active", "deleted"):
            return False
        stamp = _now()
        with self.db.transaction() as c:
            self.unpair_in(c, row, stamp)
        self._after_change()
        return True

    def unpair_in(self, c: Any, row: Any, stamp: str, keep: frozenset[int] = frozenset()) -> None:
        """Transfer ``row`` zurücknehmen; Einzelbuchungen (außer aus Stapeln in ``keep``) wieder aktiv."""
        c.execute("UPDATE journal_tx SET status='reverted', updated_at=? WHERE id=?", (stamp, row["id"]))
        self._log(c, "unpair", row["tx_id"], None, None, stamp)
        for ref in (row["pair_refs"] or "").split(","):
            ref = ref.strip()
            if not ref:
                continue
            side = c.execute("SELECT * FROM journal_tx WHERE tx_id=?", (ref,)).fetchone()
            if side is None or side["status"] != "merged" or side["batch_id"] in keep:
                continue
            c.execute("UPDATE journal_tx SET status='active', merged_into=NULL, updated_at=? WHERE id=?",
                      (stamp, side["id"]))
            c.execute("UPDATE csv_row SET status='committed' WHERE tx_id=? AND status='merged'", (side["tx_id"],))
            self._log(c, "restore", side["tx_id"], None, None, stamp)

    def restore(self, tx_id: str) -> bool:
        return self._set_status(tx_id, "deleted", "active", "restore")

    def _set_status(self, tx_id: str, old: str, new: str, action: str) -> bool:
        row = self.get(tx_id)
        if row is None or row["status"] != old or row["group_ref"]:
            return False
        stamp = _now()
        with self.db.transaction() as c:
            for r in [row, *c.execute("SELECT * FROM journal_tx WHERE group_ref=? AND status=?", (tx_id, old))]:
                c.execute("UPDATE journal_tx SET status=?, updated_at=? WHERE id=?", (new, stamp, r["id"]))
                self._log(c, action, r["tx_id"], None, None, stamp)
        self._after_change()
        return True

    def log(self, limit: int = 100) -> list[Any]:
        return self.db.q("SELECT * FROM journal_log ORDER BY id DESC LIMIT ?", (limit,))

    # -- Assets -----------------------------------------------------------------------------------------
    def save_asset(self, data: Mapping[str, Any], asset_id: str | None = None) -> SaveResult:
        def g(k: str) -> str:
            v = data.get(k)
            return str(v).strip() if v is not None else ""

        errors: list[str] = []
        existing = self.db.q1("SELECT * FROM journal_asset WHERE asset_id=?", (asset_id,)) if asset_id else None
        if asset_id and existing is None:
            return SaveResult(errors=["Asset nicht gefunden oder aus dem Import (dort pflegen)."])
        aid = asset_id or g("asset_id")
        if not asset_id:
            if not _ASSET_ID_RE.match(aid):
                errors.append("Kürzel/ID fehlt oder enthält unzulässige Zeichen (erlaubt: Buchstaben, Ziffern, : . _ "
                              "# / -, max. 40).")
            elif aid in self.known_assets():
                errors.append(f"Ein Asset mit der ID „{aid}“ existiert bereits.")
            elif aid.upper() in C.ISO_CURRENCIES and g("asset_class") != "fiat":
                errors.append(f"„{aid}“ ist ein Währungscode – für Wertpapiere und Kryptowerte eine andere ID wählen.")
        name = g("name")
        if not name or len(name) > 80:
            errors.append("Name fehlt (max. 80 Zeichen).")
        cls = g("asset_class")
        if cls not in C.ASSET_CLASSES:
            errors.append("Klasse fehlt (Wertpapier, Krypto oder Währung).")
        qs = g("quote_source") or "none"
        qid = g("quote_id") or None
        if qs not in C.QUOTE_SOURCES:
            errors.append("Unbekannte Kursquelle.")
        elif qs in ("yahoo", "coingecko") and not qid:
            errors.append("Kurs-ID fehlt (Yahoo-Symbol bzw. CoinGecko-ID).")
        elif qs == "coingecko" and qid and not _CG_RE.match(qid):
            errors.append("CoinGecko-ID besteht aus Kleinbuchstaben, Ziffern und Bindestrichen (z. B. „bitcoin“).")
        elif qs == "yahoo" and qid and not _YAHOO_RE.match(qid):
            errors.append("Yahoo-Symbol ungültig (z. B. „SAP.DE“, „EUNL.DE“, „AAPL“).")
        isin = g("isin").upper() or None
        if isin and not _ISIN_RE.match(isin):
            errors.append("ISIN ungültig (12 Zeichen, z. B. IE00B4L5Y983).")
        wkn = g("wkn").upper() or None
        if wkn and not _WKN_RE.match(wkn):
            errors.append("WKN ungültig (6 Zeichen).")
        tax_type = g("tax_type") or None
        if tax_type and tax_type not in TAX_TYPES:
            errors.append("Unbekannte Steuerart.")
        for key, label in (("category", "Kategorie"), ("aliases", "Aliasse"), ("note", "Notiz")):
            if len(g(key)) > 200:
                errors.append(f"{label} ist zu lang (max. 200 Zeichen).")
        if errors:
            return SaveResult(errors=errors)
        extra = json.loads(existing["extra_json"] or "{}") if existing else {}
        if tax_type:
            extra["tax_type"] = tax_type
        else:
            extra.pop("tax_type", None)
        vals = {"name": name, "asset_class": cls, "quote_source": qs if qid or qs in ("manual", "none") else "none",
                "quote_id": qid, "wkn": wkn, "isin": isin, "category": g("category") or None,
                "aliases": g("aliases") or None, "note": g("note") or None,
                "extra_json": json.dumps(extra, ensure_ascii=False)}
        stamp = _now()
        with self.db.transaction() as c:
            if existing is None:
                cols = ["asset_id", *vals, "created_at", "updated_at"]
                c.execute(f"INSERT INTO journal_asset({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                          (aid, *vals.values(), stamp, stamp))
                self._log(c, "asset_create", aid, None, vals, stamp)
            else:
                sets = ", ".join(f"{k}=?" for k in vals)
                c.execute(f"UPDATE journal_asset SET {sets}, updated_at=? WHERE asset_id=?",
                          (*vals.values(), stamp, aid))
                self._log(c, "asset_update", aid, existing, vals, stamp)
        self._after_change(new_asset=True)
        return SaveResult(asset_id=aid)

    # -- Folgeschritte ----------------------------------------------------------------------------------
    def after_change(self, new_asset: bool = False) -> None:
        self._after_change(new_asset)

    def _after_change(self, new_asset: bool = False) -> None:
        self.ctx.invalidate_overlay()
        try:  # Sparplan-Schätzungen, die jetzt durch echte Buchungen belegt sind, ersetzen
            from app.plans.service import plan_service

            plan_service(self.ctx).reconcile()
        except Exception as e:
            log.warning("Sparplan-Abgleich nach Journal-Änderung fehlgeschlagen: %s", e)
        sched = getattr(self.ctx, "scheduler", None)
        if sched is not None:
            if new_asset:
                sched.trigger("prices_crypto", force=True)
                sched.trigger("prices_securities", force=True)
            sched.trigger("history_backfill", 15)
            sched.trigger("plans_update", 20)

    # -- Gesamtexport -----------------------------------------------------------------------------------
    def export_zip(self) -> bytes:
        """Aktueller Datenstand (Import + Journal + freigegebene Sparplan-Ausführungen) als Import-ZIP (Schema 1.1).

        Der Export eignet sich als Sicherung, zum Wechsel des Werkzeugs und als neuer kuratierter Import – dabei
        werden Journal-Buchungen über ihre tx_id erkannt und nicht doppelt gezählt.
        """
        pf = self.ctx.recorded_portfolio()
        if pf is None:
            raise ValueError("Keine Buchungen vorhanden.")
        today = today_local()
        txs = sorted(pf.txs, key=lambda t: (t.ts, t.seq))
        # steuerliche Einstufungen aus den Einstellungen mitnehmen (Import-Spalten tax_type / tax_withholding)
        tax_types = self.ctx.settings.get("tax.asset_types") or {}
        withholding = self.ctx.settings.get("tax.account_withholding") or {}
        assets = []
        for aid, a in sorted(pf.assets.items()):
            row = asset_row(a)
            if tax_types.get(aid) in TAX_TYPES:
                row["tax_type"] = tax_types[aid]
            assets.append(row)
        led = run_ledger(pf, self.ctx.engine_options())
        holdings = [{"asset_id": asset, "account": acc, "qty": forms.s(q), "as_of": today.isoformat(),
                     "note": "Export"} for (acc, asset), q in sorted(led.balances.items()) if abs(q) > DUST]
        manual = [{"asset_id": aid, "date": d.isoformat(), "price_eur": str(p), "source": "manual_prices"}
                  for aid, vals in sorted(pf.manual_prices.items()) for d, p in vals]
        accounts = [{"account": a.account, "broker": a.broker or "", "depot_group": a.depot_group or "",
                     **{k: v for k, v in (a.extra or {}).items() if isinstance(v, str)}}
                    for a in pf.accounts.values()]
        accounts += [{"account": acc, "broker": "", "depot_group": ""} for acc in pf.all_accounts()
                     if acc not in pf.accounts]
        for row in accounts:
            if withholding.get(row["account"]) in ("domestic", "foreign"):
                row["tax_withholding"] = withholding[row["account"]]
        with tempfile.TemporaryDirectory() as td:
            path = build_zip(Path(td) / "export.zip", transactions=[tx_row(t) for t in txs], assets=assets,
                             holdings_check=holdings, issues=[], manual_prices=manual, accounts=accounts,
                             generated_at=iso(datetime.now(UTC)) or "", valuation_date=today.isoformat(),
                             notes=f"Gesamtexport aus Portfolia {__version__} (Import + in der App erfasste "
                                   "Buchungen + freigegebene Sparplan-Ausführungen)")
            return path.read_bytes()


def tx_row(t: Tx) -> dict[str, str]:
    """Buchung → Zeile in transactions.csv (Schema 1.1)."""
    source = t.source or ""
    if t.origin == "journal":
        source = f"portfolia:{source or 'manual'}"
    elif t.origin == "plan":
        source = "portfolia:sparplan"
    return {
        "tx_id": t.tx_id,
        "datetime": to_local_date(t.ts).isoformat() if t.date_only else t.ts.astimezone(UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "type": t.type, "tag": t.tag or "",
        "from_account": t.from_account or "", "from_asset": t.from_asset or "", "from_qty": forms.s(t.from_qty),
        "to_account": t.to_account or "", "to_asset": t.to_asset or "", "to_qty": forms.s(t.to_qty),
        "fee_asset": t.fee_asset or "", "fee_qty": forms.s(t.fee_qty), "fee_eur": forms.s(t.fee_eur),
        "value_eur": forms.s(t.value_eur), "orig_price": t.orig_price or "", "orig_ccy": t.orig_ccy or "",
        "source": source, "source_ref": t.source_ref or "", "flag": "" if t.origin == "plan" else (t.flag or ""),
        "note": t.note or "", "related_asset": t.related_asset or "",
    }


def asset_row(a: AssetInfo) -> dict[str, str]:
    row = {"asset_id": a.asset_id, "name": a.name, "asset_class": a.asset_class, "wkn": a.wkn or "",
           "isin": a.isin or "", "koinly_id": a.koinly_id or "", "quote_source": a.quote_source or "none",
           "quote_id": a.quote_id or "", "status": a.status or "", "note": a.note or "",
           "aliases": ";".join(a.aliases), "category": a.category or ""}
    for k, v in (a.extra or {}).items():
        if isinstance(v, str) and k not in row:
            row[k] = v
    return row


def tx_form_data(t: Tx) -> dict[str, str]:
    """Buchung → Formularwerte im Expertenmodus (Kopieren, synchronisierte Buchungen bearbeiten)."""
    local = t.ts.astimezone(local_tz())
    data = {"kind": "expert", "type": t.type, "tag": t.tag or "", "date": local.date().isoformat(),
            "time": "" if t.date_only else local.strftime("%H:%M"), "note": t.note or "",
            "related_asset": t.related_asset or "", "fee_asset": t.fee_asset or "",
            "fee_qty": forms.s(t.fee_qty), "fee_eur": forms.s(t.fee_eur), "value_eur": forms.s(t.value_eur)}
    for side in ("from", "to"):
        data[f"{side}_account"] = getattr(t, f"{side}_account") or ""
        data[f"{side}_asset"] = getattr(t, f"{side}_asset") or ""
        data[f"{side}_qty"] = forms.s(getattr(t, f"{side}_qty"))
    return data


def journal_service(ctx: Any) -> JournalService:
    svc = getattr(ctx, "_journal_service", None)
    if svc is None:
        svc = JournalService(ctx)
        ctx._journal_service = svc
    return svc
