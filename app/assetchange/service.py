"""Ticker- und Token-Änderungen je Asset: erkennen, mit Vorschau übernehmen, rückgängig machen.

Zwei Arten – bewusst getrennt, weil sie wirtschaftlich Verschiedenes bedeuten:

* **Umbenennung** (``rename``): dasselbe Wertpapier bzw. derselbe Token, nur neues Kürzel, neuer Name oder neue
  Kurs-ID (z. B. Aktie mit neuem Börsenkürzel, Coin mit neuer CoinGecko-ID). Asset-ID, Buchungen, Lots und
  Haltefristen bleiben unverändert; die Änderung wirkt als Overlay über Import und Journal (:func:`apply_changes`).
  Tage ohne Kurs der neuen Quelle übernehmen die bisherigen Kurse (Herkunft ``prev:<reihe>``, als Ersatzkurs
  gekennzeichnet); Kurse der neuen Quelle haben immer Vorrang.
* **Umstellung** (``migration``): ein neuer Token ersetzt den bisherigen in einem festen Verhältnis (z. B. MATIC →
  POL 1:1). Je Konto mit Restbestand entsteht eine normale App-Buchung „Kapitalmaßnahme – Migration“ (gleiche
  Erfassung und Validierung wie im Journal; Anschaffungsdaten und Haltefristen gehen auf den neuen Bestand über).
  Gebucht wird der *verbliebene* Bestand je Konto, frühestens nach dessen letzter Bewegung – bereits umgestellte
  Bestände (z. B. von der Börse gemeldet oder per Wallet-Abruf vorgeschlagen) werden so nie doppelt umgestellt.

Nichts geschieht automatisch: Die Erkennung (:mod:`app.assetchange.detect`) liefert nur Hinweise, der Nutzer
bestätigt nach der Vorschau. Jede Änderung lässt sich rückgängig machen (Buchungen werden gelöscht, nicht entfernt).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app.ledger.models import AssetInfo
from app.util.timeutil import iso, local_tz, today_local

log = logging.getLogger(__name__)

KINDS = {"rename": "Umbenennung – gleiches Asset, neues Kürzel bzw. neue Kursquelle",
         "migration": "Umstellung auf ein neues Asset (Token-Migration, Umtausch)"}
DUST = Decimal("1e-12")
_CG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_YAHOO_RE = re.compile(r"^[A-Za-z0-9.^=_-]{1,24}$")
_ASSET_ID_RE = re.compile(r"^[A-Za-z0-9:._#/-]{1,40}$")
PREV = "prev:"  # Herkunft übernommener Kurse der bisherigen Kursreihe


@dataclass
class PlanRow:
    account: str
    qty_old: Decimal
    qty_new: Decimal
    when: datetime  # lokal

    @property
    def date(self) -> date:
        return self.when.date()


@dataclass
class Plan:
    kind: str
    old: AssetInfo | None
    effective: date | None = None
    ratio: Decimal = Decimal(1)
    new_asset: str | None = None
    new_exists: bool = False
    new_name: str | None = None
    quote_source: str | None = None
    quote_id: str | None = None
    new_ticker: str | None = None
    note: str = ""
    rows: list[PlanRow] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def form(self) -> dict[str, str]:
        """Eingaben für das erneute Absenden (Vorschau → Übernehmen)."""
        return {"kind": self.kind, "date": self.effective.isoformat() if self.effective else "",
                "ratio": _s(self.ratio), "new_asset": self.new_asset or "", "new_name": self.new_name or "",
                "quote_source": self.quote_source or "", "quote_id": self.quote_id or "",
                "new_ticker": self.new_ticker or "", "note": self.note}


def _s(v: Decimal | None) -> str:
    if v is None:
        return ""
    t = format(v.normalize(), "f")
    return t


def _dec(raw: str) -> Decimal | None:
    s = (raw or "").strip().replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    if ":" in s:  # „1:1000“ = 1 alt → 1000 neu
        a, b = s.split(":", 1)
        try:
            return Decimal(b) / Decimal(a)
        except (InvalidOperation, ZeroDivisionError):
            return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _date(raw: str) -> date | None:
    raw = (raw or "").strip()
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", raw)
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else date.fromisoformat(raw)
    except ValueError:
        return None


def series_of(prices: Any, a: AssetInfo) -> str | None:
    return prices.series_for(a)


class AssetChangeService:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.db = ctx.db

    # -- Lesen ----------------------------------------------------------------------------------------------
    def changes(self, asset_id: str | None = None) -> list[Any]:
        if asset_id:
            return self.db.q("SELECT * FROM asset_change WHERE kind<>'hint' AND (old_asset=? OR new_asset=?) "
                             "ORDER BY id DESC", (asset_id, asset_id))
        return self.db.q("SELECT * FROM asset_change WHERE kind<>'hint' ORDER BY id DESC")

    def get(self, cid: int) -> Any:
        return self.db.q1("SELECT * FROM asset_change WHERE id=?", (cid,))

    def dismissed_keys(self) -> set[str]:
        return {r["hint_key"] for r in self.db.q("SELECT hint_key FROM asset_change WHERE kind='hint' AND "
                                                 "status='dismissed' AND hint_key IS NOT NULL")}

    def dismiss(self, hint_key: str, old_asset: str) -> None:
        if hint_key in self.dismissed_keys():
            return
        self.db.x("INSERT INTO asset_change(kind, old_asset, status, origin, hint_key, created_at) "
                  "VALUES ('hint', ?, 'dismissed', 'user', ?, ?)", (old_asset[:60], hint_key[:200], _now()))
        _bump()

    def undismiss(self, hint_key: str) -> None:
        self.db.x("DELETE FROM asset_change WHERE kind='hint' AND hint_key=?", (hint_key,))
        _bump()

    # -- Vorschau -------------------------------------------------------------------------------------------
    def plan(self, asset_id: str, data: Mapping[str, Any]) -> Plan:
        def g(k: str) -> str:
            v = data.get(k)
            return str(v).strip() if v is not None else ""

        pf = self.ctx.portfolio()
        old = pf.assets.get(asset_id) if pf is not None else None
        kind = g("kind") if g("kind") in KINDS else "migration"
        p = Plan(kind=kind, old=old, note=g("note")[:200])
        if old is None or old.is_fiat:
            p.old = None
            p.errors.append("Asset nicht gefunden (Währungen lassen sich nicht umstellen).")
            return p
        p.effective = _date(g("date"))
        if p.effective is None:
            p.errors.append("Stichtag fehlt oder ist ungültig (TT.MM.JJJJ).")
        elif p.effective > today_local():
            p.errors.append("Stichtag liegt in der Zukunft – erst nach der Umstellung erfassen.")
        if kind == "rename":
            self._plan_rename(p, g)
        else:
            self._plan_migration(p, g, pf)
        return p

    def _quote(self, p: Plan, src: str, qid: str, asset_class: str) -> None:
        if not src or src == "keep":
            return
        if src not in ("coingecko", "yahoo"):
            p.errors.append("Unbekannte Kursquelle.")
            return
        if not qid:
            p.errors.append("Kurs-ID fehlt (CoinGecko-ID bzw. Yahoo-Symbol).")
            return
        if src == "coingecko":
            from app.prices.sources import cached_catalog, catalog_path, parse_coin_id

            cid = parse_coin_id(qid)
            if cid is None or not _CG_RE.match(cid):
                p.errors.append("Ungültige CoinGecko-ID – z. B. „polygon-ecosystem-token“ oder Link von "
                                "coingecko.com.")
                return
            cat = cached_catalog(catalog_path(self.ctx))
            if cat is not None and cid not in cat.by_id:
                p.errors.append(f"„{cid}“ ist im CoinGecko-Katalog nicht vorhanden.")
                return
            if asset_class == "security":
                p.warnings.append("Wertpapier mit CoinGecko-Kursquelle – ist das beabsichtigt?")
            p.quote_source, p.quote_id = "coingecko", cid
        else:
            sym = qid.upper()
            if not _YAHOO_RE.match(sym):
                p.errors.append("Yahoo-Symbol ungültig (z. B. „META“, „SAP.DE“, „EUNL.DE“).")
                return
            p.quote_source, p.quote_id = "yahoo", sym

    def _plan_rename(self, p: Plan, g: Any) -> None:
        a = p.old
        assert a is not None
        self._quote(p, g("quote_source"), g("quote_id"), a.asset_class)
        p.new_name = g("new_name")[:80] or None
        p.new_ticker = g("new_ticker").upper()[:40] or None
        if p.errors:
            return
        same_quote = (p.quote_source, p.quote_id) == (a.quote_source, a.quote_id) or p.quote_source is None
        if same_quote and not p.new_name and not p.new_ticker:
            p.errors.append("Keine Änderung angegeben (neue Kursquelle, Name oder Kürzel).")
            return
        known = self.ctx.portfolio().assets
        if p.new_ticker and p.new_ticker in known and p.new_ticker != a.asset_id:
            p.warnings.append(f"Es gibt bereits ein Asset „{p.new_ticker}“. Sind das zwei verschiedene Assets, ist "
                              "eine Umstellung statt einer Umbenennung gemeint.")
        if p.quote_source and not same_quote:
            old_series = self.ctx.prices.series_for(a)
            p.info.append(f"Kursquelle: {a.quote_source}:{a.quote_id or '–'} → {p.quote_source}:{p.quote_id}. "
                          "Die Kurshistorie der neuen Quelle wird nach dem Übernehmen geladen; Tage ohne deren Kurse "
                          "übernehmen die bisherigen Kurse" + (f" ({old_series})" if old_series else "")
                          + ", gekennzeichnet als Ersatzkurs.")
            for r in self.db.q("SELECT id, effective_date FROM asset_change WHERE kind='rename' AND "
                               "status='applied' AND old_asset=?", (a.asset_id,)):
                p.info.append(f"Ersetzt die Umbenennung vom {r['effective_date']}.")
        if p.new_ticker:
            p.info.append(f"Neues Kürzel „{p.new_ticker}“: CSV-Importe und Datenquellen mit diesem Kürzel werden "
                          f"„{a.asset_id}“ zugeordnet (sofern dafür keine andere Zuordnung besteht).")
        p.info.append(f"Asset-ID „{a.asset_id}“, Buchungen, Lots und Haltefristen bleiben unverändert.")

    def _plan_migration(self, p: Plan, g: Any, pf: Any) -> None:
        a = p.old
        assert a is not None
        ratio = _dec(g("ratio") or "1")
        if ratio is None or ratio <= 0:
            p.errors.append("Verhältnis ungültig (neue Einheiten je bisheriger Einheit, z. B. 1 oder 1:1000).")
        else:
            p.ratio = ratio
        from app.journal.forms import resolve_asset

        raw = g("new_asset")
        target = resolve_asset(raw, pf.assets) if raw else None
        if target is not None:
            if target == a.asset_id:
                p.errors.append("Neues und bisheriges Asset sind gleich – dafür „Umbenennung“ wählen.")
            elif pf.assets[target].is_fiat:
                p.errors.append("Ziel ist eine Währung – Umstellung nur auf Wertpapiere bzw. Kryptowerte.")
            p.new_asset, p.new_exists = target, True
            p.new_name = pf.assets[target].name
        elif raw:
            if not _ASSET_ID_RE.match(raw):
                p.errors.append("Kürzel des neuen Assets enthält unzulässige Zeichen (erlaubt: Buchstaben, Ziffern, "
                                ": . _ # / -, max. 40).")
            p.new_asset = raw
            p.new_name = g("new_name")[:80] or raw
            self._quote(p, g("quote_source") or ("coingecko" if a.is_crypto else "yahoo"), g("quote_id"),
                        a.asset_class)
            if not p.quote_id and not p.errors:
                p.warnings.append("Neues Asset ohne Kursquelle – ohne Kurse wird es mit Ersatzkursen bzw. 0 € "
                                  "bewertet.")
        else:
            p.errors.append("Neues Asset fehlt (vorhandenes wählen oder neues Kürzel eingeben).")
        if p.errors or p.effective is None:
            return
        led = self.ctx.ledger()
        if led is None:
            p.errors.append("Kein Portfolio geladen.")
            return
        last: dict[str, datetime] = {}
        for t in pf.txs:
            for acc, asset in ((t.from_account, t.from_asset), (t.to_account, t.to_asset),
                               (t.from_account, t.fee_asset)):
                if asset == a.asset_id and acc:
                    last[acc] = max(last.get(acc, t.ts), t.ts)
        noon = datetime.combine(p.effective, time(12, 0), tzinfo=local_tz())
        for (acc, asset), q in sorted(led.balances.items()):
            if asset != a.asset_id:
                continue
            if q < -DUST:
                p.skipped.append(f"„{acc}“: Bestand negativ ({_s(q)}) – nicht umgestellt; fehlt eine Buchung?")
                continue
            if q <= DUST:
                continue
            when = noon
            if acc in last and last[acc].astimezone(local_tz()) + timedelta(minutes=1) > when:
                when = (last[acc].astimezone(local_tz()) + timedelta(minutes=1)).replace(second=0, microsecond=0)
            if when.date() > today_local():
                p.skipped.append(f"„{acc}“: letzte Bewegung liegt in der Zukunft – nicht umgestellt.")
                continue
            p.rows.append(PlanRow(acc, q, (q * p.ratio).normalize(), when))
        if not p.rows:
            p.errors.append(f"Kein Restbestand von {a.asset_id} – nichts umzustellen (bereits umgestellt?).")
            return
        later = [r for r in p.rows if r.date > p.effective]
        if later:
            p.info.append("Gebucht nach der letzten Bewegung des Kontos (später als der Stichtag): "
                          + ", ".join(f"„{r.account}“ am {r.date:%d.%m.%Y}" for r in later) + ".")
        prev = self.db.q1("SELECT effective_date FROM asset_change WHERE kind='migration' AND status='applied' AND "
                          "old_asset=? ORDER BY id DESC LIMIT 1", (a.asset_id,))
        if prev is not None:
            p.info.append(f"Bereits am {prev['effective_date']} umgestellt – jetzt wird der seitdem hinzugekommene "
                          "Restbestand umgestellt.")
        p.info.append("Je Konto eine Buchung „Kapitalmaßnahme – Migration“: Anschaffungsdaten und Haltefristen gehen "
                      "auf den neuen Bestand über (keine Veräußerung). Die steuerliche Würdigung im Einzelfall bleibt "
                      "zu prüfen.")
        if not p.new_exists:
            p.info.append(f"Neues Asset „{p.new_asset}“ ({p.new_name}) wird angelegt"
                          + (f", Kurse über {p.quote_source} „{p.quote_id}“." if p.quote_id else "."))

    # -- Übernehmen -----------------------------------------------------------------------------------------
    def apply(self, asset_id: str, data: Mapping[str, Any], hint_key: str | None = None) -> tuple[int | None,
                                                                                                    Plan]:
        p = self.plan(asset_id, data)
        if not p.ok:
            return None, p
        return (self._apply_rename(p, hint_key) if p.kind == "rename" else self._apply_migration(p, hint_key)), p

    def _apply_migration(self, p: Plan, hint_key: str | None) -> int | None:
        from app.journal.service import journal_service

        js = journal_service(self.ctx)
        a = p.old
        assert a is not None and p.new_asset is not None
        created = False
        if not p.new_exists:
            res = js.save_asset({"asset_id": p.new_asset, "name": p.new_name or p.new_asset,
                                 "asset_class": a.asset_class, "quote_source": p.quote_source or "none",
                                 "quote_id": p.quote_id or ""})
            if res.errors:
                p.errors += res.errors
                return None
            created = True
        tx_ids: list[str] = []
        note = (f"Umstellung {a.asset_id} → {p.new_asset} (1 : {_s(p.ratio)})" + (f" – {p.note}" if p.note else ""))
        for r in p.rows:
            res = js.save({"kind": "corporate", "tag": "migration", "account": r.account, "date": r.date.isoformat(),
                           "time": r.when.strftime("%H:%M"), "from_asset": a.asset_id, "from_qty": _s(r.qty_old),
                           "to_asset": p.new_asset, "to_qty": _s(r.qty_new), "note": note[:200]})
            if res.errors:
                for tid in tx_ids:  # nichts halb übernehmen
                    js.delete(tid)
                p.errors += [f"„{r.account}“: {e}" for e in res.errors]
                return None
            tx_ids += res.tx_ids
            p.warnings += res.warnings
        cur = self.db.x(
            "INSERT INTO asset_change(kind, old_asset, new_asset, ratio, effective_date, old_quote, new_quote_source, "
            "new_quote_id, new_name, status, origin, hint_key, tx_ids_json, asset_created, note, created_at) "
            "VALUES ('migration',?,?,?,?,?,?,?,?,'applied',?,?,?,?,?,?)",
            (a.asset_id, p.new_asset, _s(p.ratio), p.effective.isoformat() if p.effective else None,
             f"{a.quote_source}:{a.quote_id or ''}", p.quote_source, p.quote_id, p.new_name,
             "hint" if hint_key else "user", hint_key, json.dumps(tx_ids), int(created), p.note or None, _now()))
        log.info("Umstellung %s → %s (1:%s) übernommen: %d Buchungen", a.asset_id, p.new_asset, _s(p.ratio),
                 len(tx_ids))
        _bump()
        return int(cur.lastrowid)  # type: ignore[arg-type]

    def _apply_rename(self, p: Plan, hint_key: str | None) -> int | None:
        a = p.old
        assert a is not None
        old_series = self.ctx.prices.series_for(a)
        alias = False
        if p.new_ticker:
            from app.csvimport.service import csv_service

            csv = csv_service(self.ctx)
            if p.new_ticker not in csv.saved_symbols() and p.new_ticker not in self.ctx.portfolio().assets:
                csv.set_symbol(p.new_ticker, a.asset_id, "umbenennung")
                alias = True
        stamp = _now()
        with self.db.transaction() as c:
            c.execute("UPDATE asset_change SET status='replaced', reverted_at=? WHERE kind='rename' AND "
                      "status='applied' AND old_asset=?", (stamp, a.asset_id))
            cur = c.execute(
                "INSERT INTO asset_change(kind, old_asset, effective_date, old_quote, new_quote_source, new_quote_id, "
                "new_name, new_ticker, status, origin, hint_key, alias_created, note, created_at) "
                "VALUES ('rename',?,?,?,?,?,?,?,'applied',?,?,?,?,?)",
                (a.asset_id, p.effective.isoformat() if p.effective else None,
                 f"{a.quote_source}:{a.quote_id or ''}", p.quote_source, p.quote_id, p.new_name, p.new_ticker,
                 "hint" if hint_key else "user", hint_key, int(alias), p.note or None, stamp))
        cid = int(cur.lastrowid)  # type: ignore[arg-type]
        self.ctx.invalidate_overlay()
        new = self.ctx.portfolio().assets.get(a.asset_id)
        new_series = self.ctx.prices.series_for(new) if new is not None else None
        if old_series and new_series and new_series != old_series and p.effective is not None:
            n = self._carry_prices(old_series, new_series, p.effective)
            log.info("Umbenennung %s: %d bisherige Tageskurse für %s übernommen", a.asset_id, n, new_series)
        _bump()
        sched = getattr(self.ctx, "scheduler", None)
        if sched is not None:
            sched.trigger("prices_crypto", 2, force=True)
            sched.trigger("prices_securities", 3, force=True)
            sched.trigger("history_backfill", 15)
        return cid

    def _carry_prices(self, old_series: str, new_series: str, until: date) -> int:
        """Bisherige Tageskurse bis zum Stichtag in die neue Reihe übernehmen, nur für Tage ohne eigenen Kurs."""
        rows = self.db.q("SELECT * FROM price_daily WHERE series=? AND date<=? ORDER BY date",
                         (old_series, until.isoformat()))
        have = {r["date"] for r in self.db.q("SELECT date FROM price_daily WHERE series=?", (new_series,))}
        now = iso(datetime.now(UTC))
        vals = [(new_series, r["date"], r["open"], r["high"], r["low"], r["close"], r["volume"], r["split_factor"],
                 r["ccy"], f"{PREV}{old_series}", now) for r in rows if r["date"] not in have]
        if vals:
            self.db.xmany("INSERT OR IGNORE INTO price_daily(series, date, open, high, low, close, volume, "
                          "split_factor, ccy, source, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", vals)
            self.ctx.prices._bump()
        return len(vals)

    # -- Rückgängig -----------------------------------------------------------------------------------------
    def revert(self, cid: int) -> list[str]:
        r = self.get(cid)
        if r is None or r["status"] != "applied":
            return ["Änderung nicht gefunden oder bereits zurückgenommen."]
        if r["kind"] == "migration":
            from app.journal.service import journal_service

            js = journal_service(self.ctx)
            for tid in json.loads(r["tx_ids_json"] or "[]"):
                js.delete(tid)
        else:
            if r["alias_created"] and r["new_ticker"]:
                self.db.x("DELETE FROM csv_symbol WHERE symbol=? AND asset_id=? AND origin='umbenennung'",
                          (r["new_ticker"], r["old_asset"]))
            if r["new_quote_source"] and r["new_quote_id"]:
                base = f"{'cg' if r['new_quote_source'] == 'coingecko' else 'yahoo'}:{r['new_quote_id']}"
                self.db.x("DELETE FROM price_daily WHERE source LIKE ? AND series IN (?, ?)",
                          (f"{PREV}%", base, f"demo:{base}"))
        self.db.x("UPDATE asset_change SET status='reverted', reverted_at=? WHERE id=?", (_now(), cid))
        self.ctx.invalidate_overlay()
        _bump()
        log.info("Ticker-/Token-Änderung %d (%s %s) zurückgenommen", cid, r["kind"], r["old_asset"])
        return []


def apply_changes(db: Any, assets: dict[str, AssetInfo]) -> dict[str, AssetInfo]:
    """Übernommene Umbenennungen auf die Assets anwenden (gleiches Objekt, wenn nichts zu tun ist)."""
    try:
        rows = db.q("SELECT * FROM asset_change WHERE kind='rename' AND status='applied' ORDER BY id")
    except Exception:  # Tabelle fehlt (DB vor Migration 17)
        return assets
    out = None
    for r in rows:
        a = assets.get(r["old_asset"])
        if a is None:
            continue
        kw: dict[str, Any] = {}
        if r["new_quote_source"] and r["new_quote_id"]:
            kw |= {"quote_source": r["new_quote_source"], "quote_id": r["new_quote_id"]}
        if r["new_name"]:
            kw["name"] = r["new_name"]
        if r["new_ticker"] and r["new_ticker"] not in a.aliases:
            kw["aliases"] = [*a.aliases, r["new_ticker"]]
        kw["extra"] = {**a.extra, "ticker_change": {"id": r["id"], "since": r["effective_date"],
                                                    "old_quote": r["old_quote"], "old_name": a.name,
                                                    "new_ticker": r["new_ticker"]}}
        if out is None:
            out = dict(assets)
        out[a.asset_id] = replace(a, **kw)
    return out if out is not None else assets


# Hinweis-Cache (Erkennung) bei jeder Änderung verwerfen
_VERSION = [0]


def _bump() -> None:
    _VERSION[0] += 1


def version() -> int:
    return _VERSION[0]


def _now() -> str:
    return iso(datetime.now(UTC)) or ""


def asset_change_service(ctx: Any) -> AssetChangeService:
    return AssetChangeService(ctx)
