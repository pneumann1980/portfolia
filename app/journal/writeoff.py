"""Ausbuchen: Bestände als Verlust buchen (Totalverlust, Diebstahl, Burn) – einzeln oder gesammelt.

Jede Ausbuchung ist eine gewöhnliche manuelle Buchung („Kosten / Verlust“, Wert 0 €): im Journal sichtbar,
bearbeit- und löschbar, im ZIP-Export enthalten. Ausgebucht wird der gesamte Bestand eines Kontos; das Datum
darf nicht vor der letzten Buchung dieses Kontos/Assets liegen, sonst stimmte die Menge zu diesem Zeitpunkt nicht.
Gebucht wird um 23:59 Uhr, also nach allen übrigen Buchungen des Tages.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.importer.validate import validate_tx_rows
from app.journal import forms
from app.journal.service import SaveResult, _now, _strip, journal_service
from app.ledger.engine import DUST, LedgerResult, run_ledger
from app.ledger.models import AssetInfo, Portfolio
from app.prices.models import PriceInfo
from app.util.timeutil import parse_iso, to_local_date, today_local

log = logging.getLogger(__name__)

TAGS = {"lost": "Verlust / Totalverlust", "stolen": "Diebstahl", "burn": "Burn (vernichtet)"}
TIME = "23:59"
MAX_ROWS = 500


def row_key(account: str, asset_id: str) -> str:
    return hashlib.sha256(f"{account}\0{asset_id}".encode()).hexdigest()[:16]


@dataclass
class Candidate:
    key: str
    account: str
    asset: AssetInfo
    qty: Decimal
    cost: float
    price: PriceInfo | None
    last_date: date | None

    @property
    def unvalued(self) -> bool:
        return self.price is None or not self.price.valued

    @property
    def value(self) -> float:
        return 0.0 if self.unvalued else float(self.qty) * self.price.price_eur  # type: ignore[union-attr]


class WriteOffService:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.db = ctx.db

    def _data(self) -> tuple[Portfolio | None, LedgerResult | None]:
        """Erfasste Buchungen (ohne Sparplan-Schätzungen) – ausgebucht wird nur, was tatsächlich gebucht ist."""
        pf = self.ctx.recorded_portfolio()
        if pf is None:
            return None, None
        led = self.ctx.ledger() if self.ctx.portfolio() is pf else run_ledger(pf, self.ctx.engine_options())
        return pf, led

    def candidates(self) -> list[Candidate]:
        pf, led = self._data()
        if pf is None or led is None:
            return []
        last: dict[tuple[str, str], date] = {}
        for t in pf.txs:
            for acc, aid in ((t.from_account, t.from_asset), (t.to_account, t.to_asset),
                             (t.from_account or t.to_account, t.fee_asset)):
                if acc and aid and (last.get((acc, aid)) is None or t.date > last[(acc, aid)]):
                    last[(acc, aid)] = t.date
        cost: dict[tuple[str, str], float] = defaultdict(float)
        for lot in led.lots:
            cost[(lot.account, lot.asset)] += float(lot.cost)
        held = [(acc, aid, q) for (acc, aid), q in led.balances.items()
                if q > DUST and not pf.asset(aid).is_fiat]
        assets = {aid: pf.asset(aid) for _, aid, _ in held}
        prices = self.ctx.prices.latest_eur_many(assets.values(), pf) if assets else {}
        out = [Candidate(row_key(acc, aid), acc, assets[aid], q, cost.get((acc, aid), 0.0), prices.get(aid),
                         last.get((acc, aid))) for acc, aid, q in held]
        out.sort(key=lambda c: (not c.unvalued, c.value, c.asset.name.lower(), c.account.lower()))
        return out

    def book(self, keys: list[str], day: str, tag: str, note: str) -> SaveResult:
        if tag not in TAGS:
            return SaveResult(errors=["Unbekannte Art der Ausbuchung."])
        keys = list(dict.fromkeys(k for k in keys if k))[:MAX_ROWS]
        if not keys:
            return SaveResult(errors=["Keine Position ausgewählt."])
        cands = {c.key: c for c in self.candidates()}
        js = journal_service(self.ctx)
        assets = js.known_assets()
        price, fx = js._valuers()
        today = today_local()
        drafts: list[tuple[Candidate, forms.Draft, dict[str, str]]] = []
        errors: list[str] = []
        for k in keys:
            c = cands.get(k)
            if c is None:
                errors.append("Eine ausgewählte Position ist nicht mehr im Bestand – bitte die Seite neu laden.")
                continue
            data = {"kind": "cost", "date": day, "time": TIME, "account": c.account, "asset": c.asset.asset_id,
                    "qty": forms.s_de(c.qty), "tag": tag, "value_eur": "0", "note": note}
            d = forms.build("cost", data, assets, price, fx, today)
            if d.errors:
                errors += d.errors
                continue
            if d.local_date and d.local_date > today:
                errors.append("Das Datum liegt in der Zukunft.")
                continue
            if c.last_date and d.local_date and d.local_date < c.last_date:
                errors.append(f"{c.asset.name} ({c.account}): Das Datum liegt vor der letzten Buchung vom "
                              f"{c.last_date.strftime('%d.%m.%Y')}.")
                continue
            drafts.append((c, d, data))
        if errors:
            return SaveResult(errors=list(dict.fromkeys(errors)))
        rows = [r for _, d, _ in drafts for r in d.rows]
        classes = {aid: {"asset_class": a.asset_class} for aid, a in assets.items()}
        rep, parsed = validate_tx_rows([{**r, "tx_id": f"PF-PRUEFUNG-{i}"} for i, r in enumerate(rows)], classes)
        if rep.errors:
            return SaveResult(errors=list(dict.fromkeys(_strip(m.message) for m in rep.errors)))
        assert len(parsed) == len(drafts)
        stamp = _now()
        res = SaveResult()
        with self.db.transaction() as conn:
            for (_, d, data), p in zip(drafts, parsed, strict=True):
                form_json = json.dumps({**data, "writeoff": stamp}, ensure_ascii=False)
                res.tx_ids.append(js._insert(conn, p, d.value_sources[0], form_json, stamp, None))
        js.after_change()
        log.info("%d Position(en) ausgebucht (%s): %s", len(res.tx_ids), tag, ", ".join(res.tx_ids))
        return res

    def recent(self, limit: int = 300) -> list[dict[str, Any]]:
        """Aktive Ausbuchungen im Journal (auch einzeln erfasste Verluste), neueste zuerst."""
        rows = self.db.q(
            "SELECT * FROM journal_tx WHERE status='active' AND source='manual' AND type='withdrawal' "
            f"AND tag IN ({', '.join('?' * len(TAGS))}) ORDER BY id DESC LIMIT ?", (*TAGS, limit))
        pf = self.ctx.portfolio()
        out = []
        for r in rows:
            mark = None
            with contextlib.suppress(ValueError, AttributeError):
                mark = json.loads(r["form_json"] or "{}").get("writeoff")
            ts = parse_iso(r["ts_utc"])
            out.append({"tx_id": r["tx_id"], "date": to_local_date(ts) if ts else None, "account": r["from_account"],
                        "asset": pf.asset(r["from_asset"]).name if pf else r["from_asset"],
                        "qty": Decimal(r["from_qty"] or "0"), "tag": TAGS.get(r["tag"], r["tag"]),
                        "note": r["note"] or "", "mark": mark})
        return out

    def undo(self, tx_ids: list[str]) -> int:
        """Ausbuchungen zurücknehmen (Status „gelöscht“, im Journal wiederherstellbar)."""
        ids = list(dict.fromkeys(t for t in tx_ids if t))[:MAX_ROWS]
        if not ids:
            return 0
        js = journal_service(self.ctx)
        stamp = _now()
        n = 0
        with self.db.transaction() as conn:
            for tid in ids:
                r = conn.execute("SELECT * FROM journal_tx WHERE tx_id=?", (tid,)).fetchone()
                if (r is None or r["status"] != "active" or r["source"] != "manual" or r["type"] != "withdrawal"
                        or r["tag"] not in TAGS or r["group_ref"]):
                    continue
                conn.execute("UPDATE journal_tx SET status='deleted', updated_at=? WHERE id=?", (stamp, r["id"]))
                js._log(conn, "delete", tid, None, None, stamp)
                n += 1
        if n:
            js.after_change()
            log.info("%d Ausbuchung(en) zurückgenommen", n)
        return n


def writeoff_service(ctx: Any) -> WriteOffService:
    return WriteOffService(ctx)
