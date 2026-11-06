"""Stichtag-Korrekturen (0.99.0): Bestände zum Stichtag nur mit Beleg anpassen.

Grundsatz: Der berechnete Stand gilt. Eine Korrektur wird nur vorgeschlagen, wo ein Beleg zum Ende von ``Stichtag − 1``
(Referenzbestand, z. B. Depotauszug) vom berechneten Bestand abweicht. Sie ist eine gewöhnliche, gekennzeichnete
Buchung **vor** dem Stichtag (Archiv) – damit stimmen vollständige Berechnung und Eröffnungsstand weiter überein – und
einzeln rücknehmbar:

* **zu viel gebucht** → Abgang zum Ende des Vortags, Tag ``cutover_correction``: verbraucht die ältesten Lots (FIFO),
  ist aber weder Veräußerung noch Verlust noch Zahlungsstrom (kein Gewinn, kein Verlust).
* **zu wenig gebucht** → Zugang (Tag ``opening_balance``) mit **belegtem** Anschaffungsdatum und belegten Kosten. Nur
  ausdrücklich als Annahme gekennzeichnet („vorsichtig“: Anschaffung am Vortag zu 0 €, Haltefrist beginnt neu) oder
  gar nicht gebucht („offen lassen“).

Jede Korrektur zeigt vorab ihre Wirkung auf Bestand, Lots und realisierte Ergebnisse je Jahr.
"""

from __future__ import annotations

import dataclasses
import logging
import sqlite3
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app.cutover import after, guard
from app.cutover import core as CO
from app.ledger.engine import DUST, run_ledger
from app.ledger.models import Tx
from app.util.timeutil import iso, local_tz, to_local_date

log = logging.getLogger(__name__)
ZERO = Decimal(0)
MODES = {"settle": "Ausgleich auf den Bestand der Quelle (Zugang: Vortag, 0 €, Haltefrist beginnt neu)",
         "evidence": "belegt", "cautious": "vorsichtig (Annahme: Anschaffung am Vortag zu 0 €)",
         "derived": "abgeleitet"}


def _dec(v: Any) -> Decimal | None:
    try:
        d = Decimal(str(v).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def proposals(ctx: Any, m: CO.Model, refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Vorschläge aus Belegen zum Stichtag. Staub (Rundungsreste) wird nur ausgewiesen, nie korrigiert."""
    out = []
    for r in refs:
        if r["ok"]:
            continue
        diff = r["diff"]
        out.append({"account": r["account"], "asset": r["asset"], "diff": diff, "dust": bool(r["dust"]),
                    "kind": "remove" if diff < 0 else "add", "qty": abs(diff), "source": r["source"],
                    "evidence": f"Referenzbestand zum {(m.cutoff - timedelta(days=1)):%d.%m.%Y} "
                                f"({'Kontoauszug' if r['source'] == 'statement' else r['source']})"
                                + (f": {r['note']}" if r.get("note") else ""),
                    "reference": r["reference"], "opening": r["opening"]})
    return out


def _when(cutoff: date, kind: str, acq: date | None) -> tuple[date, str]:
    if kind == "remove":
        return cutoff - timedelta(days=1), "23:59:59"
    return (acq or cutoff - timedelta(days=1)), "12:00:00"


def validate(ctx: Any, m: CO.Model, *, account: str, asset: str, kind: str, qty: Any, mode: str, evidence: str,
             acq_date: str | None, cost_eur: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """Eingaben prüfen; Rückgabe (bereinigte Werte, Fehler)."""
    errs: list[str] = []
    pf = m.pf_full
    q = _dec(qty)
    if kind not in ("remove", "add"):
        errs.append("Art der Korrektur fehlt.")
    if mode not in ("evidence", "cautious", "derived", "settle"):
        errs.append("Bitte wählen: belegte Daten oder vorsichtige Annahme.")
    if account not in pf.all_accounts() and not (mode == "settle" and kind == "add" and account.strip()):
        errs.append("Konto unbekannt.")  # ein Ausgleich darf ein Konto ohne Buchungen (nur Datenquelle) anlegen
    if asset not in pf.assets or pf.asset(asset).is_fiat:
        errs.append("Asset unbekannt oder Fiat (wird nicht geführt).")
    if q is None or q <= 0:
        errs.append("Menge muss größer als 0 sein.")
    if errs or q is None:
        return None, errs
    ev = (evidence or "").strip()
    if mode == "derived" and ev and not ev.lower().startswith("abgeleitet"):
        ev = f"abgeleitet: {ev}"
    d: date | None = None
    cost: Decimal | None = None
    if kind == "remove":
        have = m.state.balances.get((account, asset), ZERO)
        if q > have + DUST:
            errs.append(f"Bestand zum Stichtag ({have.normalize():f}) reicht für den Abgang nicht aus.")
        if not ev:
            errs.append("Bitte den Beleg bzw. die Begründung angeben.")
        mode = "evidence" if mode not in ("derived", "settle") else mode
    else:
        if mode in ("cautious", "settle"):
            if mode == "settle" and not ev:
                errs.append("Bitte den Beleg (Bestand der Quelle) angeben.")
            d, cost = m.cutoff - timedelta(days=1), ZERO
        else:
            try:
                d = date.fromisoformat(str(acq_date or "").strip())
            except ValueError:
                errs.append("Anschaffungsdatum fehlt (Format JJJJ-MM-TT).")
            cost = _dec(cost_eur)
            if cost is None or cost < 0:
                errs.append("Anschaffungskosten (EUR) fehlen oder sind ungültig.")
            if not ev:
                errs.append("Bitte den Beleg der Anschaffungsdaten angeben.")
        if d is not None and d >= m.cutoff:
            errs.append("Das Anschaffungsdatum muss vor dem Stichtag liegen (später gehört die Buchung in den aktiven "
                        "Zeitraum).")
    if errs:
        return None, errs
    return {"account": account, "asset": asset, "kind": kind, "qty": q, "mode": mode, "evidence": ev, "acq": d,
            "cost": cost}, []


def _form(m: CO.Model, v: dict[str, Any]) -> dict[str, Any]:
    d, hhmm = _when(m.cutoff, v["kind"], v["acq"])
    base = {"kind": "expert", "date": d.isoformat(), "time": hhmm}
    c = f"{m.cutoff:%d.%m.%Y}"
    if v["mode"] == "settle":
        tail = "" if v["kind"] == "remove" else ", Haltefrist neu (0 €)"
        note = f"Stichtag-Ausgleich {c}: {v['evidence']}"
        note = note[:120 - len(tail)] + tail
        if v["kind"] == "remove":
            return {**base, "type": "withdrawal", "tag": "cutover_correction", "from_account": v["account"],
                    "from_asset": v["asset"], "from_qty": str(v["qty"]), "value_eur": "0", "note": note}
        return {**base, "type": "deposit", "tag": "opening_balance", "to_account": v["account"],
                "to_asset": v["asset"], "to_qty": str(v["qty"]), "value_eur": "0", "note": note}
    if v["kind"] == "remove":
        note = (f"Stichtag-Korrektur {c}, {v['evidence']}" if v["mode"] == "derived"
                else f"Stichtag-Korrektur {c}, Abgang laut Beleg: {v['evidence']}")
        return {**base, "type": "withdrawal", "tag": "cutover_correction", "from_account": v["account"],
                "from_asset": v["asset"], "from_qty": str(v["qty"]), "value_eur": "0", "note": note[:120]}
    if v["mode"] == "cautious":
        note = f"Stichtag-Korrektur {c}, Annahme (vorsichtig): Anschaffung am Vortag zu 0 €, kein Beleg"
    elif v["mode"] == "derived":
        note = f"Stichtag-Korrektur {c}, {v['evidence']}"
    else:
        note = f"Stichtag-Korrektur {c}, belegt: {v['evidence']}"
    return {**base, "type": "deposit", "tag": "opening_balance", "to_account": v["account"], "to_asset": v["asset"],
            "to_qty": str(v["qty"]), "value_eur": str(v["cost"]), "note": note[:120]}


def _synthetic(m: CO.Model, v: dict[str, Any]) -> Tx:
    d, hhmm = _when(m.cutoff, v["kind"], v["acq"])
    hh, mm, ss = (int(x) for x in hhmm.split(":"))
    ts = datetime.combine(d, time(hh, mm, ss), tzinfo=local_tz()).astimezone(UTC)
    rem = v["kind"] == "remove"
    return Tx(seq=10**9, tx_id="STICHTAG-SIM", ts=ts, date=to_local_date(ts), date_only=False,
              type="withdrawal" if rem else "deposit", tag="cutover_correction" if rem else "opening_balance",
              from_account=v["account"] if rem else None, from_asset=v["asset"] if rem else None,
              from_qty=v["qty"] if rem else None, to_account=None if rem else v["account"],
              to_asset=None if rem else v["asset"], to_qty=None if rem else v["qty"], fee_asset=None, fee_qty=None,
              fee_eur=None, value_eur=ZERO if rem else v["cost"], origin="journal")


def simulate(ctx: Any, m: CO.Model, v: dict[str, Any]) -> dict[str, Any]:
    """Wirkung der Korrektur (ohne zu buchen): Bestand zum Stichtag und heute, Lots und realisierte Ergebnisse je Jahr
    vorher/nachher."""
    pf2 = dataclasses.replace(m.pf_full, txs=[*m.pf_full.txs, _synthetic(m, v)])
    day = m.cutoff - timedelta(days=1)
    led0 = m.led_full
    led1 = run_ledger(pf2, dataclasses.replace(m.opts, snapshot_dates=(day,)))
    st1 = led1.state_snapshots.get(day)
    key = (v["account"], v["asset"])

    def years(led: Any) -> dict[int, tuple[Decimal, Decimal]]:
        out: dict[int, list[Decimal]] = defaultdict(lambda: [ZERO, ZERO])
        for d in led.disposals:
            if pf2.asset(d.asset).is_fiat:
                continue
            out[d.date.year][0] += d.proceeds
            out[d.date.year][1] += d.cost
        return {y: (a[0], a[1]) for y, a in out.items()}

    y0, y1 = years(led0), years(led1)
    rows = [{"year": y, "before": y0.get(y), "after": y1.get(y), "changed": y0.get(y) != y1.get(y)}
            for y in sorted(set(y0) | set(y1))]
    lots0 = [x for x in (m.state.lots) if (x.account, x.asset) == key]
    lots1 = [x for x in (st1.lots if st1 is not None else []) if (x.account, x.asset) == key]
    return {
        "balance_cutoff": (m.state.balances.get(key, ZERO), st1.balances.get(key, ZERO) if st1 else ZERO),
        "balance": (led0.balances.get(key, ZERO), led1.balances.get(key, ZERO)),
        "lots": (len(lots0), len(lots1)), "cost": (sum((x.cost for x in lots0), ZERO),
                                                    sum((x.cost for x in lots1), ZERO)),
        "years": [r for r in rows if r["changed"]], "years_all": rows,
    }


STALE = ("Der Datenstand hat sich seit der Anzeige geändert (z. B. durch eine andere Buchung, einen Abruf oder ein "
         "zweites Absenden) – es wurde nichts gebucht. Bitte die Vorschau neu prüfen.")


class _Abort(Exception):
    """Abbruch in der Schreibtransaktion: alles dieser Transaktion wird zurückgenommen."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def run_errors(db: Any, cutoff: date) -> list[str]:
    """Bindung an den Stichtagslauf (0.107.0, N37): Ist ein Stichtag festgeschrieben, gelten Korrekturen und ihre
    Rücknahme nur für **ihn** – ein veraltetes Formular mit anderem Datum darf die Archiv-Ausnahme nie öffnen. Ohne
    festgeschriebenen Stichtag (Vorbereitung) ist jedes Datum erlaubt."""
    act = guard.active(db)
    return _run_mismatch(act["cutoff"] if act is not None else None, cutoff)


def _run_mismatch(active: str | None, cutoff: date) -> list[str]:
    if active is None or active == cutoff.isoformat():
        return []
    return [f"Festgeschrieben ist der Stichtag {date.fromisoformat(active):%d.%m.%Y}; die Korrektur gehörte zum "
            f"{cutoff:%d.%m.%Y} (veraltete Seite). Es wurde nichts gebucht – bitte die Seite neu laden."]


def _check_in(c: Any, cutoff: date, version: str) -> None:
    """Als Erstes in der Schreibtransaktion: derselbe Datenstand wie bei der Prüfung (sonst hat ein gleichzeitiges
    Absenden, eine Buchung oder ein Abruf die Grundlage verändert) und derselbe Stichtagslauf."""
    from app.diagnosis import actions as A

    try:
        A.assert_version(c, version)
    except A.Conflict as e:
        raise _Abort([STALE]) from e
    act = c.execute("SELECT cutoff FROM cutover WHERE status='active'").fetchone()
    errs = _run_mismatch(act["cutoff"] if act is not None else None, cutoff)
    if errs:
        raise _Abort(errs)


def _write(c: Any, js: Any, m: CO.Model, v: dict[str, Any], cutoff: date, stamp: str) -> dict[str, Any]:
    """Buchung und Protokolleintrag in der laufenden Transaktion ``c`` (beides oder nichts)."""
    res = js.save_in(c, _form(m, v))
    if res.errors or not res.tx_ids:
        raise _Abort(list(res.errors) or ["Buchung nicht möglich."])
    cur = c.execute(
        "INSERT INTO cutover_correction(cutoff, account, asset_id, kind, qty, acq_date, cost_eur, mode, evidence, "
        "tx_id, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (cutoff.isoformat(), v["account"], v["asset"], v["kind"], str(v["qty"]),
         v["acq"].isoformat() if v["acq"] else None, str(v["cost"]) if v["cost"] is not None else None, v["mode"],
         v["evidence"], res.tx_ids[0], "active", stamp))
    return {"errors": [], "id": int(cur.lastrowid), "tx_id": res.tx_ids[0]}  # type: ignore[arg-type]


def book(ctx: Any, cutoff: date, m: CO.Model, items: list[dict[str, Any]], version: str, *,
         refresh: bool = True) -> dict[str, Any]:
    """Geprüfte Korrekturen ``items`` (aus :func:`validate` gegen das Modell ``m``) buchen – in **einer**
    Schreibtransaktion, die zuerst Datenstand ``version`` und Stichtagslauf prüft (0.107.0, N35/N37). Je Zeile sind
    Journalbuchung und Protokolleintrag atomar (N36): eine Zeile, die scheitert, hinterlässt nichts (Sicherungspunkt),
    die übrigen bleiben. Rückgabe: ``errors`` (Abbruch vor jeder Buchung) und je Zeile ``results``."""
    from app.journal.service import journal_service

    js = journal_service(ctx)
    stamp = iso(datetime.now(UTC)) or ""
    results: list[dict[str, Any]] = []
    try:
        with guard.allow(), ctx.db.transaction() as c:
            _check_in(c, cutoff, version)
            for i, v in enumerate(items):
                c.execute(f"SAVEPOINT cr{i}")
                try:
                    results.append(_write(c, js, m, v, cutoff, stamp))
                except _Abort as e:
                    c.execute(f"ROLLBACK TO cr{i}")
                    results.append({"errors": e.errors})
                except sqlite3.Error as e:  # z. B. Protokoll nicht schreibbar: die Zeile hinterlässt nichts
                    c.execute(f"ROLLBACK TO cr{i}")
                    log.warning("Stichtag-Korrektur %s %s auf %s nicht gespeichert: %s", v["qty"], v["asset"],
                                v["account"], e)
                    results.append({"errors": [f"Speichern fehlgeschlagen ({e}) – nichts gebucht."]})
                c.execute(f"RELEASE cr{i}")
    except _Abort as e:  # Zwischenspeicher veröffentlichen in einer Schreibtransaktion nichts (siehe context)
        return {"errors": e.errors, "results": []}
    if any(not r["errors"] for r in results):
        js.after_change()
        for v, r in zip(items, results, strict=True):
            if not r["errors"]:
                log.info("Stichtag-Korrektur %s: %s %s %s auf %s", cutoff, v["kind"], v["qty"], v["asset"],
                         v["account"])
        if refresh:  # Sammelwege (Ausgleich) aktualisieren einmal am Ende
            after.refresh_cases(ctx)
    return {"errors": [], "results": results}


def apply(ctx: Any, cutoff: date, *, account: str, asset: str, kind: str, qty: Any, mode: str, evidence: str = "",
          acq_date: str | None = None, cost_eur: Any = None, refresh: bool = True) -> dict[str, Any]:
    """Korrektur buchen (eine Buchung vor dem Stichtag, einzeln rücknehmbar) und mitprotokollieren."""
    from app.diagnosis import actions as A

    errs = run_errors(ctx.db, cutoff)
    if errs:
        return {"errors": errs}
    version = A.data_version(ctx.db)  # vor dem Modell lesen: ändert sich danach etwas, scheitert die Prüfung
    ms = CO.models(ctx, cutoff)
    if not ms:
        return {"errors": ["Keine Daten vorhanden."]}
    m = ms[0]
    v, errs = validate(ctx, m, account=account, asset=asset, kind=kind, qty=qty, mode=mode, evidence=evidence,
                       acq_date=acq_date, cost_eur=cost_eur)
    if errs or v is None:
        return {"errors": errs}
    out = book(ctx, cutoff, m, [v], version, refresh=refresh)
    if out["errors"]:
        return {"errors": out["errors"]}
    return out["results"][0]


def undo(ctx: Any, cid: int) -> dict[str, Any]:
    """Korrektur zurücknehmen: die erzeugte Buchung wird entfernt (umkehrbar wie jede gelöschte Buchung). Entfernen und
    Statusänderung des Protokolls liegen in einer Transaktion (0.107.0, N36); nur für den festgeschriebenen Stichtag."""
    from app.journal.service import journal_service

    row = ctx.db.q1("SELECT * FROM cutover_correction WHERE id=?", (cid,))
    if row is None or row["status"] != "active":
        return {"errors": ["Korrektur nicht gefunden oder bereits zurückgenommen."]}
    cutoff = date.fromisoformat(row["cutoff"])
    errs = run_errors(ctx.db, cutoff)
    if errs:
        return {"errors": errs}
    js = journal_service(ctx)
    try:
        with guard.allow(), ctx.db.transaction() as c:
            cur = c.execute("SELECT status, tx_id FROM cutover_correction WHERE id=?", (cid,)).fetchone()
            if cur is None or cur["status"] != "active":  # gleichzeitig zurückgenommen
                raise _Abort(["Korrektur nicht gefunden oder bereits zurückgenommen."])
            act = c.execute("SELECT cutoff FROM cutover WHERE status='active'").fetchone()
            errs = _run_mismatch(act["cutoff"] if act is not None else None, cutoff)
            if errs:
                raise _Abort(errs)
            own = c.execute("SELECT 1 FROM journal_tx WHERE tx_id=?", (cur["tx_id"],)).fetchone() is not None
            ok = js.delete_in(c, cur["tx_id"]) if own else js.delete_import_in(c, cur["tx_id"])  # sonst: Import
            if not ok:
                raise _Abort(["Die zugehörige Buchung ließ sich nicht entfernen."])
            c.execute("UPDATE cutover_correction SET status='undone', undone_at=? WHERE id=?",
                      (iso(datetime.now(UTC)), cid))
    except _Abort as e:
        return {"errors": e.errors}
    js.after_change()
    after.refresh_cases(ctx)
    return {"errors": []}


def listing(ctx: Any, cutoff: date) -> list[dict[str, Any]]:
    return [dict(r) for r in ctx.db.q("SELECT * FROM cutover_correction WHERE cutoff=? ORDER BY id DESC",
                                      (cutoff.isoformat(),))]
