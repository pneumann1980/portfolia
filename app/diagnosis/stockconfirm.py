"""Abschließende Bestandsbestätigung: gezielte Frage, Nachweis speichern, bestätigen und übernehmen in einem Schritt.

Rolle und Grenzen
* Ein Bestandsnachweis (Referenzbestand, :mod:`app.diagnosis.references`) ist **keine Buchung** und erzeugt nie eine
  Ausgleichsbuchung. Er wählt höchstens zwischen Hypothesen, die bereits aus Belegen entstanden sind.
* Die Frage wird vorab berechnet: Es wird nur nach einer Position gefragt, auf der die verbleibenden zulässigen
  Hypothesen **verschiedene** Bestände ergeben. Bestände, die alle Varianten gleichermaßen erfüllen, werden nicht
  abgefragt; liegt bereits ein Nachweis vor, der die Varianten nicht unterscheidet, wird nicht erneut gefragt.
* Passt der Nachweis zu genau einer belegten Hypothese, kann der Nutzer Nachweis und Übernahme in einem Schritt
  bestätigen (:func:`confirm_and_apply`) – ausdrücklich, mit Vorschau; sonst wird nur der Nachweis gespeichert, die
  Menge als bestätigt gekennzeichnet und die verbleibende historische Frage benannt.
* Ein bestätigter Endbestand ersetzt weder fehlende Anschaffungskosten noch eine Gebührenursache noch eine
  Verlustursache (siehe Qualitätsdimensionen im Fall).
* Spätere Buchungen werden berücksichtigt: Der Vergleich erfolgt zum angegebenen Zeitpunkt; ein fortgeschriebener
  Bestand gilt nie als heute extern bestätigt.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.diagnosis import actions as A
from app.diagnosis import clarify as K
from app.diagnosis import references as R
from app.diagnosis.model import Report

ZERO = Decimal(0)


def question(report: Report, case: K.Case) -> dict[str, Any] | None:
    """Unterscheidende Bestandsfrage zu einem Fall (rein lesend). ``None``: keine Frage nötig bzw. möglich."""
    ok = [h for h in case.hyps if h.ok]
    pos = K.discriminating_positions(ok)
    if not pos:
        return None
    # Position wählen, die die meisten Hypothesen trennt (Anzahl verschiedener Bestände); Gleichstand: Schlüssel
    def parts(k: tuple[str, str]) -> int:
        return len({format(h.balances.get(k, ZERO).normalize(), "f") for h in ok})

    pos = sorted(pos, key=lambda k: (-parts(k), k))
    already = {tuple(p.split(" · ", 1)) for p in case.baseline.stock_diffs}
    for k in pos:
        if k in already and case.baseline.stock != "kein_nachweis":
            continue  # dazu liegt ein Nachweis vor (er unterscheidet die Varianten bereits oder nicht)
        acc, asset = k
        expect = [{"hyp": h.id, "label": h.label[:160],
                   "qty": format(h.balances.get(k, ZERO).normalize(), "f")} for h in ok]
        return {"account": acc, "asset": asset,
                "text": f"Bitte prüfe den {asset}-Bestand auf deinem Konto {acc}. Welcher Bestand wird mit Stand und "
                        "Uhrzeit angezeigt?",
                "expect": expect, "scopes": [{"key": k_, "label": v} for k_, v in R.SCOPES.items()
                                             if k_ != "combined"],
                "why": f"{len({e['qty'] for e in expect})} Deutungen ergeben hier verschiedene Bestände – "
                       "der Nachweis unterscheidet sie. Ein Bestandsnachweis bucht nie etwas; er wählt nur zwischen "
                       "den belegten Deutungen."}
    return None


def submit(ctx: Any, case_key: str, account: str, asset: str, qty: str, as_of: str, time_of_day: str = "",
           tz: str = "", source: str = "api", note: str = "", *, subaccount: str = "", scope: str = "",
           qty_locked: str = "", qty_margin: str = "", qty_pnl: str = "", apply: bool = False,
           hyp_id: str = "") -> K.Result:
    """Nachweis speichern; danach wird der Fall neu bewertet. ``apply``: passt der Nachweis zu genau einer belegten
    Hypothese (``hyp_id`` = die im Dialog gezeigte), wird sie in demselben Schritt übernommen. Ohne Treffer bleibt es
    beim Nachweis: Menge bestätigt, historische Frage offen, keine Ausgleichsbuchung."""
    # nur ein Nachweis zu einer Position des Falls
    with A._LOCK:
        report = A.report_for(ctx)
        cases = K.analyse(ctx, report, only=case_key)
        case = next((c for c in cases if c.key == case_key), None)
        if case is None:
            return K.Result(errors=["Der Fall besteht nicht mehr."])
        if (account, asset) not in set(case.positions):
            return K.Result(errors=["Der Nachweis gehört nicht zu einer Position dieses Falls."])
        res = R.add(ctx, account, asset, qty, as_of, source, note, time_of_day, tz, subaccount=subaccount,
                    scope=scope, qty_locked=qty_locked, qty_margin=qty_margin, qty_pnl=qty_pnl, case_key=case_key)
        if not res.ok:
            return K.Result(errors=res.errors)
        ctx.invalidate_data()
    K.notify_change()
    # neu bewerten (Nachweis ist jetzt Teil der Evidenz)
    report2 = A.report_for(ctx)
    cases2 = K.analyse(ctx, report2, only=case_key)
    case2 = next((c for c in cases2 if c.key == case_key), None)
    if case2 is None:
        ctx.db.x("UPDATE clarify_case SET status='geloest', resolved_at=?, updated_at=? WHERE case_key=?",
                 (A._now(), A._now(), case_key))
        return K.Result(ok=True, message=res.message + " Der Fall besteht nicht mehr.")
    K.persist(ctx, cases2, complete=False)
    dec = case2.decision
    if apply and dec is not None and dec.kind == "apply" and dec.hyp is not None:
        if hyp_id and dec.hyp.id != hyp_id:
            return K.Result(ok=True, message=res.message + " Die gewählte Lösung wird vom Nachweis nicht eindeutig "
                                                           "bestätigt – es wurde nichts übernommen.")
        out = K.apply_hypothesis(ctx, case_key, dec.hyp.id, "bestand", authorization="user",
                                 expect_version=case2.version)
        if out.ok:
            out.message = res.message + " " + out.message
        return out
    msg = res.message
    if dec is not None and dec.kind == "confirm_partial":
        msg += " Menge bestätigt – " + dec.reason
    elif dec is not None and dec.kind == "apply":
        msg += " Der Nachweis bestätigt genau eine Lösung – sie kann jetzt übernommen werden."
    elif dec is not None:
        msg += " " + dec.reason
    return K.Result(ok=True, message=msg)
