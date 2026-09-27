"""Sparpläne: Erkennung speichern, Ausführungen seit dem Importstand schätzen, mit neuen Importen abgleichen.

Schätz-Buchungen liegen ausschließlich in der App-Datenbank (Tabelle ``tx_estimate``) – der Import bleibt
unverändert und ist weiterhin die maßgebliche Quelle. Lebenszyklus einer Schätzung:

* ``estimated`` – im Portfolio enthalten und als „geschätzt“ markiert,
* ``confirmed`` – vom Nutzer geprüft (ggf. angepasst) und freigegeben, ohne Markierung,
* ``superseded`` – der nächste Import enthält die echte Buchung (Datum ±7 Tage, Betrag/Stück ±20 %),
* ``missing`` – der Import deckt den Termin ab, enthält aber keine Ausführung (Schätzung entfällt),
* ``dismissed`` – vom Nutzer verworfen bzw. Sparplan deaktiviert/pausiert.
"""

from __future__ import annotations

import csv
import io
import logging
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

from app.db import Database
from app.importer.zipbuilder import TX_COLUMNS
from app.ledger.models import Portfolio, Tx
from app.plans.detect import FREQS, GRACE_DAYS, WEEKDAYS, Plan, buy_series, detect_plans
from app.util.numbers import parse_number
from app.util.timeutil import fmt_de_date, iso, local_tz, parse_iso, to_local_date, today_local

log = logging.getLogger(__name__)
ACTIVE = ("estimated", "confirmed")
MAX_PER_PLAN = 60
MATCH_DAYS = 7
MATCH_TOL = Decimal("0.20")
CENT = Decimal("0.01")
# Schätzkurs weicht um mehr als diesen Faktor vom letzten Kauf laut Import ab → Hinweis „Kurs prüfen“
# (typische Ursachen: falsche Kursreihe, nicht berücksichtigter Split, GBp statt GBP)
PRICE_JUMP = Decimal(2)
STATUS_LABEL = {"estimated": "geschätzt", "confirmed": "bestätigt", "superseded": "durch Import ersetzt",
                "missing": "nicht im Import – entfernt", "dismissed": "verworfen"}


def cutoff_of(pf: Portfolio) -> date:
    """Importstand: bis hierhin gilt der Import als vollständig (holdings_check-Stichtag)."""
    if pf.valuation_date:
        return pf.valuation_date
    dates = [t.date for t in pf.txs if not t.flag]
    return max(dates) if dates else today_local()


def _dec(v: Any) -> Decimal:
    return Decimal(str(v)) if v not in (None, "") else Decimal(0)


# ----------------------------------------------------------------------------------------------------
# Overlay: Schätzungen als Transaktionen für Ledger und Bewertung
# ----------------------------------------------------------------------------------------------------

def overlay_txs(db: Database, base: Portfolio) -> list[Tx]:
    out: list[Tx] = []
    for r in db.q("SELECT * FROM tx_estimate WHERE status IN ('estimated','confirmed') ORDER BY ts_utc, id"):
        if r["asset_id"] not in base.assets:
            continue
        ts = parse_iso(r["ts_utc"])
        if ts is None:
            continue
        value, qty, fee = _dec(r["value_eur"]), _dec(r["qty"]), _dec(r["fee_eur"])
        external = r["funding"] == "external" or not r["funding_asset"]
        no_fee_leg = external or not fee
        out.append(Tx(
            seq=1_000_000 + int(r["id"]), tx_id=r["tx_id"], ts=ts, date=to_local_date(ts),
            date_only=bool(r["date_only"]), type="buy", tag=None,
            from_account=None if external else r["account"], from_asset=None if external else r["funding_asset"],
            from_qty=None if external else value,
            to_account=r["account"], to_asset=r["asset_id"], to_qty=qty,
            fee_asset=None if no_fee_leg else r["funding_asset"], fee_qty=None if no_fee_leg else fee,
            fee_eur=fee or None, value_eur=value, orig_price=r["price_eur"], orig_ccy="EUR", source="sparplan",
            source_ref=r["plan_key"], flag=r["status"], note=r["price_source"],
        ))
    return out


def pending_count(db: Database) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM tx_estimate WHERE status='estimated'", default=0) or 0)


def missing_confirmed_count(db: Database) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM tx_estimate WHERE status='confirmed' AND missing_import_id IS NOT NULL",
                         default=0) or 0)


def estimated_assets(db: Database) -> set[str]:
    return {r[0] for r in db.q("SELECT DISTINCT asset_id FROM tx_estimate WHERE status='estimated'")}


# ----------------------------------------------------------------------------------------------------
# Service
# ----------------------------------------------------------------------------------------------------

class PlanService:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.db: Database = ctx.db
        self._refs: tuple[Portfolio, dict[tuple[str, str], tuple[Decimal, date]]] | None = None

    def _ref_prices(self, base: Portfolio) -> dict[tuple[str, str], tuple[Decimal, date]]:
        """Letzter Kaufkurs (EUR) je Konto und Asset laut Import – Referenz für die Plausibilitätsprüfung."""
        if self._refs is not None and self._refs[0] is base:
            return self._refs[1]
        out = {k: (ex[-1].amount / ex[-1].qty, ex[-1].date) for k, ex in buy_series(base).items() if ex[-1].qty > 0}
        self._refs = (base, out)
        return out

    # -- Kurse ------------------------------------------------------------------------------------------
    def _to_eur(self, price: Decimal, ccy: str | None, d: date) -> Decimal | None:
        if not ccy or ccy.upper() == "EUR":
            return price
        fx = self.ctx.store.fx_on_or_before(ccy, d)
        if not fx or not fx[0]:
            return None
        return price / Decimal(str(fx[0]))

    def price_on(self, pf: Portfolio, asset_id: str, d: date, today: date) -> tuple[Decimal, str, bool] | None:
        """(Kurs in EUR, Quelle, endgültig) für den Ausführungstag."""
        a = pf.asset(asset_id)
        series = self.ctx.prices.series_for(a)
        if series:
            row = self.ctx.store.close_on_or_before(series, d)
            if row is not None and row["date"] == d.isoformat():
                p = self._to_eur(_dec(row["close"]), row["ccy"], d)
                if p:
                    return p, f"Schlusskurs {fmt_de_date(d)}", True
            if d >= today - timedelta(days=1):
                q = self.ctx.store.latest(series)
                if q is not None and q["price"]:
                    p = self._to_eur(_dec(q["price"]), q["ccy"], d)
                    if p:
                        return p, "aktueller Kurs (vorläufig)", False
            if row is not None and (d - date.fromisoformat(row["date"])).days <= 5:
                p = self._to_eur(_dec(row["close"]), row["ccy"], d)
                if p:
                    return p, f"Schlusskurs {fmt_de_date(row['date'])} (letzter verfügbarer)", False
        manual = [x for x in pf.manual_prices.get(asset_id, []) if x[0] <= d]
        if manual:
            md, mp = max(manual)
            return _dec(mp), f"manueller Kurs {fmt_de_date(md)}", False
        return None

    # -- Erkennung & Schätzungen ------------------------------------------------------------------------
    def _plan_enabled(self, row: Any) -> bool:
        if row["enabled"] is not None:
            return bool(row["enabled"])
        return row["confidence"] in ("hoch", "mittel")

    def _store_plans(self, plans: list[Plan], now: str) -> dict[str, Any]:
        seen = set()
        with self.db.transaction() as c:
            for p in plans:
                seen.add(p.key)
                c.execute(
                    """INSERT INTO plan(key, account, asset_id, freq, days_json, weekday, time_local, date_only,
                           amount_eur, fee_eur, qty_decimals, funding_asset, funding, weekend_shift, executions,
                           first_date, last_date, next_due, confidence, status, detected_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(key) DO UPDATE SET freq=excluded.freq, days_json=excluded.days_json,
                           weekday=excluded.weekday, time_local=excluded.time_local, date_only=excluded.date_only,
                           amount_eur=excluded.amount_eur, fee_eur=excluded.fee_eur,
                           qty_decimals=excluded.qty_decimals, funding_asset=excluded.funding_asset,
                           funding=excluded.funding, weekend_shift=excluded.weekend_shift,
                           executions=excluded.executions, first_date=excluded.first_date,
                           last_date=excluded.last_date, next_due=excluded.next_due, confidence=excluded.confidence,
                           status=excluded.status, updated_at=excluded.updated_at""",
                    (p.key, p.account, p.asset_id, p.freq, str(list(p.days)), p.weekday, p.time_local,
                     int(p.date_only), str(p.amount), str(p.fee), p.qty_decimals, p.funding_asset, p.funding,
                     int(p.weekend_shift), len(p.executions), p.first_date.isoformat(), p.last_date.isoformat(),
                     p.next_due.isoformat() if p.next_due else None, p.confidence, p.status, now, now),
                )
            if seen:
                ph = ",".join("?" * len(seen))
                c.execute(f"UPDATE plan SET status='ended', updated_at=? WHERE status!='ended' AND key NOT IN ({ph})",
                          (now, *seen))
            else:
                c.execute("UPDATE plan SET status='ended', updated_at=? WHERE status!='ended'", (now,))
        return {r["key"]: r for r in self.db.q("SELECT * FROM plan")}

    def _estimate_values(self, amount: Decimal, price: Decimal, decimals: int) -> tuple[Decimal, Decimal]:
        q = Decimal(1).scaleb(-decimals)
        qty = (amount / price).quantize(q, rounding=ROUND_DOWN)
        return qty, amount

    def update(self, today: date | None = None, now: datetime | None = None) -> dict[str, Any]:
        base = self.ctx.base_portfolio()
        if base is None:
            return {"skipped": "kein Import"}
        tz = local_tz()
        now = now or datetime.now(UTC)
        today = today or now.astimezone(tz).date()
        cut = cutoff_of(base)
        plans = detect_plans(base, cut)
        stamp = iso(datetime.now(UTC))
        rows = self._store_plans(plans, stamp)
        created = refreshed = dismissed = 0
        no_price: list[str] = []
        for p in plans:
            row = rows[p.key]
            if p.status != "active" or not self._plan_enabled(row):
                continue
            amount = _dec(row["user_amount"]) if row["user_amount"] else p.amount
            grace = GRACE_DAYS[p.freq]
            hh, mm = (int(x) for x in p.time_local.split(":"))
            for d in p.schedule(max(p.last_date, cut - timedelta(days=grace)), today, limit=MAX_PER_PLAN):
                when = datetime.combine(d, time(12, 0) if p.date_only else time(hh, mm), tzinfo=tz)
                if when > now:
                    continue  # Ausführungszeitpunkt noch nicht erreicht
                ex = self.db.q1("SELECT * FROM tx_estimate WHERE plan_key=? AND due_date=?", (p.key, d.isoformat()))
                if ex is not None:
                    if ex["status"] == "estimated" and not ex["user_edited"] and not ex["price_final"]:
                        pr = self.price_on(base, p.asset_id, d, today)
                        if pr and (pr[2] or pr[1] != ex["price_source"]):
                            qty, value = self._estimate_values(_dec(ex["value_eur"]), pr[0], p.qty_decimals)
                            self.db.x("UPDATE tx_estimate SET qty=?, price_eur=?, price_source=?, price_final=?, "
                                      "updated_at=? WHERE id=?",
                                      (str(qty), str(pr[0].quantize(Decimal("0.000001"))), pr[1], int(pr[2]), stamp,
                                       ex["id"]))
                            refreshed += 1
                    continue
                pr = self.price_on(base, p.asset_id, d, today)
                if pr is None:
                    no_price.append(f"{p.asset_id} {d.isoformat()}")
                    continue
                price, source, final = pr
                qty, value = self._estimate_values(amount, price, p.qty_decimals)
                if qty <= 0:
                    continue
                self.db.x(
                    """INSERT INTO tx_estimate(plan_key, tx_id, due_date, ts_utc, date_only, account, asset_id, qty,
                           price_eur, value_eur, fee_eur, funding_asset, funding, price_source, price_final, status,
                           import_id, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'estimated',?,?,?)""",
                    (p.key, f"SP-{row['id']}-{d.strftime('%Y%m%d')}", d.isoformat(), iso(when.astimezone(UTC)),
                     int(p.date_only), p.account, p.asset_id, str(qty), str(price.quantize(Decimal("0.000001"))),
                     str(value.quantize(CENT)), str(p.fee.quantize(CENT)), p.funding_asset, p.funding, source,
                     int(final), base.import_id, stamp, stamp),
                )
                created += 1
        # Unbestätigte Schätzungen deaktivierter oder pausierter Pläne zurückziehen
        for key, row in rows.items():
            if row["status"] != "active" or not self._plan_enabled(row):
                note = ("Sparplan deaktiviert" if not self._plan_enabled(row)
                        else "Sparplan laut Import pausiert/beendet")
                dismissed += self.db.x("UPDATE tx_estimate SET status='dismissed', note=?, updated_at=? "
                                       "WHERE plan_key=? AND status='estimated'", (note, stamp, key)).rowcount
        if created or refreshed or dismissed:
            self.ctx.invalidate_overlay()
        res = {"plans": len(plans), "active": sum(1 for p in plans if p.status == "active"), "created": created,
               "refreshed": refreshed, "dismissed": dismissed, "no_price": no_price[:20]}
        if created or dismissed:
            log.info("Sparpläne: %s", res)
        return res

    # -- Abgleich mit neuem Import ----------------------------------------------------------------------
    def reconcile(self) -> dict[str, Any]:
        base = self.ctx.base_portfolio()
        if base is None:
            return {"skipped": "kein Import"}
        cut = cutoff_of(base)
        freq_of = {r["key"]: r["freq"] for r in self.db.q("SELECT key, freq FROM plan")}
        buys: dict[tuple[str, str], list[Tx]] = defaultdict(list)
        for t in base.txs:
            if not t.flag and t.type == "buy" and t.to_asset and t.to_account and t.to_qty:
                buys[(t.to_account, t.to_asset)].append(t)
        used = {r[0] for r in self.db.q("SELECT matched_tx_id FROM tx_estimate WHERE matched_tx_id IS NOT NULL")}
        stamp = iso(datetime.now(UTC))
        superseded = missing = flagged = 0
        for r in self.db.q("SELECT * FROM tx_estimate WHERE status IN ('estimated','confirmed') ORDER BY ts_utc"):
            ts = parse_iso(r["ts_utc"])
            est_date = to_local_date(ts) if ts else date.fromisoformat(r["due_date"])
            value, qty, fee = _dec(r["value_eur"]), _dec(r["qty"]), _dec(r["fee_eur"])
            best: tuple[tuple[int, Decimal], Tx] | None = None
            for t in buys.get((r["account"], r["asset_id"]), []):
                if t.tx_id in used:
                    continue
                dd = abs((t.date - est_date).days)
                if dd > MATCH_DAYS:
                    continue
                tot_t = (t.value_eur or Decimal(0)) + (t.fee_eur or Decimal(0))
                tot_e = value + fee
                ok_value = tot_e > 0 and abs(tot_t - tot_e) <= tot_e * MATCH_TOL
                ok_qty = qty > 0 and abs(t.to_qty - qty) <= qty * MATCH_TOL
                if ok_value or ok_qty:
                    score = (dd, abs(tot_t - tot_e))
                    if best is None or score < best[0]:
                        best = (score, t)
            if best is not None:
                used.add(best[1].tx_id)
                self.db.x("UPDATE tx_estimate SET status='superseded', matched_tx_id=?, missing_import_id=NULL, "
                          "note=?, updated_at=? WHERE id=?",
                          (best[1].tx_id, f"ersetzt durch Import-Buchung {best[1].tx_id}", stamp, r["id"]))
                superseded += 1
                continue
            grace = GRACE_DAYS.get(freq_of.get(r["plan_key"], "monthly"), 5)
            if max(est_date, date.fromisoformat(r["due_date"])) <= cut - timedelta(days=grace):
                if r["status"] == "estimated":
                    self.db.x("UPDATE tx_estimate SET status='missing', note=?, updated_at=? WHERE id=?",
                              (f"Import (Stand {fmt_de_date(cut)}) enthält keine Ausführung – Schätzung entfernt",
                               stamp, r["id"]))
                    missing += 1
                elif r["missing_import_id"] != base.import_id:
                    self.db.x("UPDATE tx_estimate SET missing_import_id=?, updated_at=? WHERE id=?",
                              (base.import_id, stamp, r["id"]))
                    flagged += 1
        if superseded or missing or flagged:
            self.ctx.invalidate_overlay()
        res = {"superseded": superseded, "missing": missing, "confirmed_missing": flagged}
        if superseded or missing or flagged:
            log.info("Sparplan-Abgleich mit Import: %s", res)
        return res

    # -- Nutzeraktionen ---------------------------------------------------------------------------------
    def _get(self, eid: int) -> Any:
        return self.db.q1("SELECT * FROM tx_estimate WHERE id=?", (eid,))

    def confirm(self, eid: int) -> bool:
        stamp = iso(datetime.now(UTC))
        n = self.db.x("UPDATE tx_estimate SET status='confirmed', confirmed_at=?, updated_at=? "
                      "WHERE id=? AND status='estimated'", (stamp, stamp, eid)).rowcount
        if n:
            self.ctx.invalidate_overlay()
        return bool(n)

    def confirm_all(self) -> int:
        stamp = iso(datetime.now(UTC))
        n = self.db.x("UPDATE tx_estimate SET status='confirmed', confirmed_at=?, updated_at=? "
                      "WHERE status='estimated'", (stamp, stamp)).rowcount
        if n:
            self.ctx.invalidate_overlay()
        return int(n)

    def dismiss(self, eid: int) -> bool:
        n = self.db.x("UPDATE tx_estimate SET status='dismissed', note='vom Nutzer verworfen', updated_at=? "
                      "WHERE id=? AND status IN ('estimated','confirmed')", (iso(datetime.now(UTC)), eid)).rowcount
        if n:
            self.ctx.invalidate_overlay()
        return bool(n)

    def edit(self, eid: int, form: dict[str, Any], confirm: bool) -> list[str]:
        """Werte übernehmen (und optional freigeben). Rückgabe: Fehlermeldungen (leer = gespeichert)."""
        r = self._get(eid)
        if r is None or r["status"] not in ACTIVE:
            return ["Buchung nicht gefunden oder bereits abgeglichen."]
        errors: list[str] = []
        tz = local_tz()
        try:
            d = date.fromisoformat(str(form.get("date") or "").strip())
        except ValueError:
            d = None
            errors.append("Datum ungültig (TT.MM.JJJJ bzw. JJJJ-MM-TT).")
        t_raw = str(form.get("time") or "").strip() or "12:00"
        try:
            hh, mm = (int(x) for x in t_raw.split(":")[:2])
            t_val = time(hh, mm)
        except ValueError:
            t_val = None
            errors.append("Uhrzeit ungültig (HH:MM).")
        price = parse_number(form.get("price"))
        qty = parse_number(form.get("qty"))
        amount = parse_number(form.get("amount"))
        fee = parse_number(form.get("fee")) or Decimal(0)
        if price is None or price <= 0:
            errors.append("Kurs muss größer als 0 sein.")
        if qty is None and amount is not None and price and price > 0:
            qty = amount / price
        if qty is None or qty <= 0:
            errors.append("Stückzahl muss größer als 0 sein.")
        if fee < 0:
            errors.append("Gebühr darf nicht negativ sein.")
        base = self.ctx.base_portfolio()
        if d is not None and base is not None:
            cut = cutoff_of(base)
            if d > today_local() + timedelta(days=1):
                errors.append("Datum liegt in der Zukunft.")
            if d < cut - timedelta(days=10):
                errors.append(f"Datum liegt vor dem Importstand ({fmt_de_date(cut)}) – solche Buchungen gehören in den "
                              "Import.")
        if errors:
            return errors
        assert d is not None and t_val is not None and price is not None and qty is not None
        qty = qty.quantize(Decimal("1e-8")).normalize()
        value = (qty * price).quantize(CENT)
        when = datetime.combine(d, t_val, tzinfo=tz).astimezone(UTC)
        stamp = iso(datetime.now(UTC))
        self.db.x(
            """UPDATE tx_estimate SET ts_utc=?, date_only=0, qty=?, price_eur=?, value_eur=?, fee_eur=?, user_edited=1,
                   price_source='vom Nutzer angepasst', price_final=1, updated_at=?,
                   status=CASE WHEN ? THEN 'confirmed' ELSE status END,
                   confirmed_at=CASE WHEN ? THEN ? ELSE confirmed_at END
               WHERE id=?""",
            (iso(when), format(qty, "f"), str(price), str(value), str(fee.quantize(CENT)), stamp, int(confirm),
             int(confirm), stamp, eid),
        )
        self.ctx.invalidate_overlay()
        return []

    def set_enabled(self, key: str, value: str) -> None:
        enabled = None if value == "auto" else (1 if value == "on" else 0)
        self.db.x("UPDATE plan SET enabled=?, updated_at=? WHERE key=?", (enabled, iso(datetime.now(UTC)), key))
        row = self.db.q1("SELECT * FROM plan WHERE key=?", (key,))
        if row is not None and self._plan_enabled(row):
            # zuvor wegen Deaktivierung zurückgezogene Schätzungen neu erzeugen lassen
            self.db.x("DELETE FROM tx_estimate WHERE plan_key=? AND status='dismissed' AND note='Sparplan deaktiviert'",
                      (key,))
        self.update()

    def set_amount(self, key: str, amount: Decimal | None) -> None:
        stamp = iso(datetime.now(UTC))
        self.db.x("UPDATE plan SET user_amount=?, updated_at=? WHERE key=?",
                  (str(amount.quantize(CENT)) if amount else None, stamp, key))
        row = self.db.q1("SELECT * FROM plan WHERE key=?", (key,))
        if row is None:
            return
        new_amount = _dec(row["user_amount"]) if row["user_amount"] else _dec(row["amount_eur"])
        for r in self.db.q("SELECT * FROM tx_estimate WHERE plan_key=? AND status='estimated' AND user_edited=0",
                           (key,)):
            qty, value = self._estimate_values(new_amount, _dec(r["price_eur"]), int(row["qty_decimals"]))
            self.db.x("UPDATE tx_estimate SET qty=?, value_eur=?, updated_at=? WHERE id=?",
                      (str(qty), str(value.quantize(CENT)), stamp, r["id"]))
        self.ctx.invalidate_overlay()

    # -- Anzeige & Export -------------------------------------------------------------------------------
    def plans(self) -> list[dict[str, Any]]:
        out = []
        for r in self.db.q("SELECT * FROM plan ORDER BY status='ended', status='paused', account, asset_id"):
            d = dict(r)
            days = [int(x) for x in (r["days_json"] or "[]").strip("[]").split(",") if x.strip()]
            if r["freq"] in ("weekly", "biweekly") and r["weekday"] is not None:
                d["label"] = f"{FREQS.get(r['freq'], r['freq'])} ({WEEKDAYS[int(r['weekday'])]})"
            elif days:
                d["label"] = f"{FREQS.get(r['freq'], r['freq'])} am " + " und ".join(f"{x}." for x in days)
            else:
                d["label"] = FREQS.get(r["freq"], r["freq"])
            d["effective_enabled"] = self._plan_enabled(r)
            d["amount"] = _dec(r["user_amount"]) if r["user_amount"] else _dec(r["amount_eur"])
            out.append(d)
        return out

    def estimates(self, statuses: tuple[str, ...], limit: int = 500) -> list[dict[str, Any]]:
        ph = ",".join("?" * len(statuses))
        rows = self.db.q(f"SELECT * FROM tx_estimate WHERE status IN ({ph}) ORDER BY ts_utc DESC, id DESC LIMIT ?",
                         (*statuses, limit))
        out = []
        tz = local_tz()
        refs: dict[tuple[str, str], tuple[Decimal, date]] | None = None
        for r in rows:
            d = dict(r)
            ts = parse_iso(r["ts_utc"])
            d["local"] = ts.astimezone(tz) if ts else None
            d["qty_d"], d["price_d"] = _dec(r["qty"]), _dec(r["price_eur"])
            d["value_d"], d["fee_d"] = _dec(r["value_eur"]), _dec(r["fee_eur"])
            d["status_label"] = STATUS_LABEL.get(r["status"], r["status"])
            d["price_warn"] = None
            if r["status"] in ACTIVE and not r["user_edited"] and d["price_d"] > 0:
                if refs is None:
                    base = self.ctx.base_portfolio()
                    refs = self._ref_prices(base) if base is not None else {}
                ref = refs.get((r["account"], r["asset_id"]))
                if ref is not None and ref[0] > 0:
                    ratio = d["price_d"] / ref[0]
                    if ratio > PRICE_JUMP or ratio < 1 / PRICE_JUMP:
                        d["price_warn"] = {"ref": ref[0], "date": ref[1], "ratio": ratio}
            out.append(d)
        return out

    def export_csv(self, statuses: tuple[str, ...] = ("confirmed",)) -> str:
        """Buchungen im Format von transactions.csv (zur Übernahme in den kuratierten Import)."""
        cols = TX_COLUMNS
        buf = io.StringIO(newline="")
        w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        for r in reversed(self.estimates(statuses, limit=10000)):
            external = r["funding"] == "external" or not r["funding_asset"]
            fee = r["fee_d"]
            w.writerow({
                "tx_id": r["tx_id"], "datetime": r["ts_utc"], "type": "buy", "tag": "",
                "from_account": "" if external else r["account"], "from_asset": "" if external else r["funding_asset"],
                "from_qty": "" if external else r["value_eur"],
                "to_account": r["account"], "to_asset": r["asset_id"], "to_qty": r["qty"],
                "fee_asset": r["funding_asset"] if (fee and not external) else "",
                "fee_qty": str(fee) if (fee and not external) else "",
                "fee_eur": str(fee) if fee else "", "value_eur": r["value_eur"], "orig_price": r["price_eur"],
                "orig_ccy": "EUR", "source": "portfolia-sparplan", "source_ref": r["plan_key"], "flag": "",
                "note": f"Sparplan ({r['status_label']})", "related_asset": "",
            })
        return buf.getvalue()


def plan_service(ctx: Any) -> PlanService:
    svc = getattr(ctx, "_plan_service", None)
    if svc is None:
        svc = PlanService(ctx)
        ctx._plan_service = svc
    return svc
