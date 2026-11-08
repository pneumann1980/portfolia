"""Ausgleich zum Stichtag (0.102.0): alles „glattziehen“, ohne an Einzelfällen hängen zu bleiben.

Grundlage ist der Bestand laut Beleg nach dem Stichtag (Referenzbestand oder Bestand der Quelle). Je Konto und
Asset wird die Differenz zum berechneten Bestand **am Vortag des Stichtags** gebucht, sodass ab dem Stichtag
Berechnung und Beleg übereinstimmen:

* Beleg niedriger als berechnet → **Abgang** (Tag ``cutover_correction``): verbraucht die ältesten Lots, ist weder
  Veräußerung noch Verlust noch Zahlungsstrom (steuerlich neutral). Mehr als der Eröffnungsbestand kann nicht
  ausgeglichen werden; der Rest wird ausgewiesen (Ursache im aktiven Zeitraum).
* Beleg höher als berechnet → **Zugang** am Vortag zu 0 € (Tag ``opening_balance``). Die Haltefrist beginnt neu; ein
  Verkauf binnen eines Jahres wäre voll steuerpflichtig. Wo ab dem Stichtag aus diesem Konto/Asset verkauft wurde, weist
  die Vorschau darauf hin.

Es werden keine Ursachen unterdrückt: liegt eine Ursache nahe (offene Abgänge anderer Konten, Abgänge ohne Gebühr),
steht sie in der Vorschau. Jede Buchung ist gekennzeichnet („Stichtag-Ausgleich“) und einzeln rücknehmbar.

Entscheidung je Zeile (0.103.0): ``auto`` (Vorgabe nach Gewicht bzw. Kontoregel), ``settle`` (auf den Beleg
ausgleichen), ``keep`` (App behalten, nichts buchen) oder ``custom`` (eigener Zielbestand für heute).
Gewicht: ``small`` (Wert unter 1 €, pauschal),
``critical`` (Verkauf ab dem Stichtag, Rest, große oder nicht bewertbare Differenz: einzeln entscheiden, nicht
vorausgewählt) und ``normal``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app.cutover import after
from app.cutover import core as CO
from app.cutover import corrections as CR
from app.cutover import derive as DV
from app.util.timeutil import local_tz

log = logging.getLogger(__name__)
ZERO = Decimal(0)


SMALL_EUR = Decimal(1)
BIG_EUR = Decimal(100)
DECISIONS = ("auto", "settle", "keep", "custom")


def key_id(account: str, asset: str) -> str:
    return hashlib.sha256(f"{account}\x00{asset}".encode()).hexdigest()[:12]


def _evidence_text(ev: dict[str, Any]) -> str:
    when = ev["at"].astimezone(local_tz())
    return f"{ev['kind']} {ev['qty'].normalize():f} am {when:%d.%m.%Y}"[:90]


def _value(prices: dict[str, Any], asset: str, qty: Decimal) -> Decimal | None:
    pi = prices.get(asset)
    if pi is None or getattr(pi, "kind", "") in ("unvalued", "") or not pi.price_eur:
        return None
    return abs(qty) * Decimal(str(pi.price_eur))


def _tier(r: dict[str, Any]) -> str:
    v = r["value"]
    if r["rest"] > 0:  # nicht (vollständig) am Stichtag ausgleichbar: immer einzeln ansehen
        return "critical"
    if r.get("dust"):  # Rundungsrest: nie kritisch, auch ohne Kurs
        return "small"
    if r["sold"] or v is None or v >= BIG_EUR:
        return "critical"
    return "small" if v < SMALL_EUR else "normal"


def _row(m: CO.Model, prices: dict[str, Any], sold: set[tuple[str, str]], txs: list[Any], key: tuple[str, str],
         ev: dict[str, Any]) -> dict[str, Any] | None:
    """Eine Ausgleichszeile aus einem Beleg ``ev`` (Menge ``qty`` zum Zeitpunkt ``at``); ``None`` ohne Abweichung."""
    window = [t for t in txs if t.ts <= ev["at"] and DV._touches(t, key)]
    have_open = m.state.balances.get(key, ZERO)
    computed = have_open + sum((DV._effect(t, key) for t in window), ZERO)
    diff = ev["qty"] - computed
    if diff == 0:  # auch Rundungsreste (< 1e-8) werden ausgeglichen: jedes Asset soll zum Beleg „gesetzt“ sein
        return None
    kind = "remove" if diff < 0 else "add"
    qty = abs(diff)
    rest = ZERO
    if kind == "remove" and qty > have_open:
        qty, rest = have_open, qty - have_open
    cause = DV._causes(m, key, diff, ev["at"], window) if window else {"cause": "", "text": "", "tx_ids": []}
    now = m.led_full.balances.get(key, ZERO)
    signed = -qty if kind == "remove" else qty
    row = {
        "id": key_id(*key), "account": key[0], "asset": key[1], "kind": kind, "qty": qty, "rest": rest,
        "diff": diff, "reference": ev["qty"], "computed": computed, "opening": have_open, "at": ev["at"],
        "kind_label": ev["kind"], "source": ev["source"], "stale": bool(ev.get("stale")),
        "window": len(window), "cause": cause.get("cause", ""), "cause_text": cause.get("text", ""),
        "tx_ids": cause.get("tx_ids", []), "evidence": _evidence_text(ev),
        "sold": kind == "add" and key in sold, "restart": kind == "add",
        "now": now, "after": now + signed, "signed": signed,
        "since": sum(1 for t in txs if t.ts > ev["at"] and DV._touches(t, key)),
        "dust": abs(diff) <= CO.STAUB_QTY,
        "value": _value(prices, key[1], qty + rest), "custom": bool(ev.get("custom"))}
    row["tier"] = _tier(row)
    return row


def plan(ctx: Any, m: CO.Model, report: Any = None) -> dict[str, Any]:
    """Vorschau des Ausgleichs (rein lesend): je Konto/Asset mit Abweichung eine Zeile, nach Konto gegliedert."""
    txs = [t for t in m.pf_full.txs if t.date >= m.cutoff and t.flag != "estimated"]
    sold = {(d.account, d.asset) for d in m.led_full.disposals if d.date >= m.cutoff}
    try:
        prices = ctx.price_infos(m.pf_full, m.led_full)
    except Exception as e:  # Bewertung ist Zusatz – der Ausgleich funktioniert auch ohne Kurse
        log.debug("Kurse für den Ausgleich nicht verfügbar: %s", e)
        prices = {}
    rows: list[dict[str, Any]] = []
    from app.diagnosis.engine import report_for

    report = report if report is not None else report_for(ctx)
    for ev in DV.evidence(ctx, m, observed=True, stale=True, report=report):
        key = (ev["account"], ev["asset"])
        if m.pf_full.asset(ev["asset"]).is_fiat:
            continue
        r = _row(m, prices, sold, txs, key, ev)
        if r is not None and (r["qty"] > 0 or r["rest"] > 0):
            rows.append(r)
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in sorted(rows, key=lambda x: (x["account"], x["tier"] != "critical", x["asset"])):
        groups.setdefault(r["account"], []).append(r)
    cov = coverage(m, report, {(r["account"], r["asset"]) for r in rows})
    run = run_key(ctx.db, m.cutoff)
    for r in rows:
        r["fp"] = row_fp(r, run)
    return {"coverage": cov, "rows": rows, "_run": run,
            "groups": [{"account": a, "id": key_id(a, ""), "rows": rs} for a, rs in groups.items()],
            "n_add": sum(1 for r in rows if r["kind"] == "add"),
            "n_remove": sum(1 for r in rows if r["kind"] == "remove" and r["qty"] > 0),
            "n_rest": sum(1 for r in rows if r["rest"] > 0), "n_sold": sum(1 for r in rows if r["sold"]),
            "n_small": sum(1 for r in rows if r["tier"] == "small"),
            "n_critical": sum(1 for r in rows if r["tier"] == "critical"),
            "n_stale": sum(1 for r in rows if r["stale"]), "day": m.cutoff - timedelta(days=1),
            "_prices": prices, "_sold": sold, "_txs": txs}


def run_key(db: Any, cutoff: Any) -> str:
    """Stichtagslauf, zu dem eine Anzeige gehört: festgeschriebener Lauf (Kennung) bzw. Vorbereitung je Datum."""
    from app.cutover import guard

    act = guard.active(db)
    return f"lauf:{act['id']}:{act['cutoff']}" if act is not None else f"vorbereitung:{cutoff.isoformat()}"


def _n(d: Any) -> str:
    return format(Decimal(d).normalize(), "f") if d is not None else ""


def row_fp(r: dict[str, Any], run: str) -> str:
    """Fingerabdruck einer angezeigten Ausgleichszeile (0.107.0, N34): Lauf, Konto/Asset, Richtung, Menge, Rest, Beleg
    (Menge, Quelle, Zeitpunkt bzw. eigener Zielbestand) und Eröffnungsbestand. Was bestätigt wird, ist genau das."""
    when = "eigener Zielbestand" if r.get("custom") else (r["at"].isoformat() if r.get("at") else "")
    payload = "\x00".join([run, r["account"], r["asset"], r["kind"], _n(r["qty"]), _n(r["rest"]), _n(r["reference"]),
                           _n(r["opening"]), str(r.get("source") or ""), when])
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def digest(rows: list[dict[str, Any]]) -> str:
    """Bestätigungsschlüssel einer Menge von Zeilen (Reihenfolge egal)."""
    return hashlib.sha256("|".join(sorted(r["fp"] for r in rows)).encode()).hexdigest()[:24]


def coverage(m: CO.Model, report: Any, listed: set[tuple[str, str]]) -> dict[str, Any]:
    """Wie viele Bestände (Konto · Asset) sind belegt? ``matched``: Beleg stimmt (Quelle bzw. Referenz),
    ``deviating``: weicht ab (steht im Ausgleich), ``written_off``: Quelle zeigt bewusst ausgebuchte Menge,
    ``unverified``: kein Beleg – dort gilt der berechnete Stand (Quellen abrufen oder Referenzbestand erfassen)."""
    pf = m.pf_full
    held = {k for k, v in m.led_full.balances.items() if v > CO.DUST and not pf.asset(k[1]).is_fiat}
    status = {(h.account, h.asset): h.status for h in report.holdings}
    out = {"matched": 0, "deviating": 0, "written_off": 0, "unverified": 0, "unverified_list": []}
    for k in sorted(held):
        stt = status.get(k, "")
        if k in listed:
            out["deviating"] += 1
        elif stt in ("extern_ok", "ref_ok"):
            out["matched"] += 1
        elif stt == "extern_ausgebucht":
            out["written_off"] += 1
        else:
            out["unverified"] += 1
            out["unverified_list"].append(k)
    out["total"] = len(held)
    return out


def custom_row(m: CO.Model, pl: dict[str, Any], row: dict[str, Any], target: Decimal) -> dict[str, Any] | None:
    """Zeile für einen eigenen Zielbestand (heute): Differenz zum aktuellen Bestand der App."""
    ev = {"account": row["account"], "asset": row["asset"], "qty": target, "at": datetime.now(UTC),
          "kind": "Eigener Zielbestand", "source": "manuell", "stale": False, "custom": True}
    r = _row(m, pl["_prices"], pl["_sold"], pl["_txs"], (row["account"], row["asset"]), ev)
    if r is not None:
        r["fp"] = row_fp(r, pl.get("_run", ""))
    return r


def resolve(m: CO.Model, pl: dict[str, Any], form: Mapping[str, Any]) -> dict[str, Any]:
    """Entscheidungen aus dem Formular: ``dec_<id>`` (auto|settle|keep|custom), ``tgt_<id>`` (Zielbestand),
    ``acc_<kontoid>`` (default|all|remove|none), ``small_all`` (Kleinstbeträge pauschal). Rückgabe: ``chosen`` (Zeilen
    mit Buchung), ``skipped``, ``errors`` und die Zusammenfassung."""
    chosen: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[str] = []
    small_all = str(form.get("small_all", "")) in ("1", "on", "true")
    for g in pl["groups"]:
        rule = str(form.get(f"acc_{g['id']}", "default"))
        for r in g["rows"]:
            dec = str(form.get(f"dec_{r['id']}", "auto"))
            if dec not in DECISIONS:
                dec = "auto"
            if dec == "auto":
                if rule == "all":
                    dec = "settle"
                elif rule == "none":
                    dec = "keep"
                elif rule == "remove":
                    dec = "settle" if r["kind"] == "remove" else "keep"
                else:
                    dec = "settle" if (r["tier"] == "normal" or (r["tier"] == "small" and small_all)) else "keep"
            if dec == "keep":
                skipped.append(r)
            elif dec == "settle":
                (chosen if r["qty"] > 0 else skipped).append(r)
            else:  # custom
                raw = str(form.get(f"tgt_{r['id']}", "")).strip().replace(",", ".")
                try:
                    target = Decimal(raw)
                    if not target.is_finite() or target < 0:
                        raise ValueError(raw)
                except (InvalidOperation, ValueError):
                    errors.append(f"{r['account']} · {r['asset']}: Zielbestand „{raw}“ ungültig.")
                    skipped.append(r)
                    continue
                cr = custom_row(m, pl, r, target)
                if cr is None:
                    skipped.append(r)  # Zielbestand entspricht dem Bestand der App: nichts zu buchen
                elif cr["qty"] > 0:
                    chosen.append(cr)
                else:
                    errors.append(f"{r['account']} · {r['asset']}: Abgang nicht am Stichtag möglich "
                                  "(Bestand zum Stichtag zu klein).")
                    skipped.append(r)
    return {"chosen": chosen, "skipped": skipped, "errors": errors, "summary": summarize(chosen, skipped)}


def summarize(chosen: list[dict[str, Any]], skipped: list[dict[str, Any]]) -> dict[str, Any]:
    adds = [r for r in chosen if r["kind"] == "add"]
    rem = [r for r in chosen if r["kind"] == "remove"]

    def val(rs: list[dict[str, Any]]) -> Decimal:
        return sum((r["value"] for r in rs if r["value"] is not None), ZERO)

    return {"n": len(chosen), "n_add": len(adds), "n_remove": len(rem), "value_add": val(adds),
            "value_remove": val(rem), "n_unvalued": sum(1 for r in chosen if r["value"] is None),
            "n_sold": sum(1 for r in adds if r["sold"]), "n_rest": sum(1 for r in chosen if r["rest"] > 0),
            "n_skipped": len(skipped), "n_stale": sum(1 for r in chosen if r["stale"]),
            "accounts": sorted({r["account"] for r in chosen})}


CHANGED = ("Der Ausgleich hat sich seit der Anzeige geändert – es wurde nichts gebucht. Bitte die neue Vorschau "
           "prüfen und erneut bestätigen")
UNCONFIRMED = ("Diese Auswahl wurde so nicht angezeigt (eigener Zielbestand oder veraltete Seite) – es wurde nichts "
               "gebucht. Bitte zuerst „Auswahl prüfen“")


def _row_text(r: dict[str, Any]) -> str:
    from app.web.fmt import qty_short

    what = "Abgang" if r["kind"] == "remove" else "Zugang"
    return f"{r['asset']} auf {r['account']}: jetzt {what} {qty_short(r['qty'])} (Beleg {qty_short(r['reference'])})"


def binding_errors(chosen: list[dict[str, Any]], expected: Mapping[str, str]) -> list[str]:
    """Jede gewählte Zeile muss genau so bestätigt worden sein, wie sie jetzt gebucht würde (``expected``: Zeile →
    angezeigter Fingerabdruck). Sonst nichts buchen – auch nicht die unveränderten Zeilen derselben Bestätigung."""
    changed = [r for r in chosen if expected.get(r["id"]) and expected[r["id"]] != r["fp"]]
    unseen = [r for r in chosen if not expected.get(r["id"])]
    out = []
    if changed:
        out.append(f"{CHANGED}: " + "; ".join(_row_text(r) for r in changed[:4]) + ".")
    if unseen:
        out.append(f"{UNCONFIRMED}: " + "; ".join(_row_text(r) for r in unseen[:4]) + ".")
    return out


def _book(ctx: Any, cutoff: Any, rows: list[dict[str, Any]], *, reapprove: bool, m: CO.Model | None = None,
          version: str | None = None) -> dict[str, Any]:
    """Zeilen in **einer** Schreibtransaktion buchen, die zuerst den Datenstand ``version`` der Planung prüft
    (0.107.0, N35): zwei gleichzeitige Bestätigungen derselben Planung buchen nur einmal; die zweite meldet den
    geänderten Stand. Ohne ``m``/``version`` (Altaufrufe) wird hier geplant."""
    from app.cutover import fix as FX
    from app.diagnosis import actions as A

    if m is None or version is None:
        version = A.data_version(ctx.db)
        ms = CO.models(ctx, cutoff)
        if not ms:
            return {"errors": ["Keine Daten vorhanden."], "done": [], "failed": []}
        m = ms[0]
    errs = CR.run_errors(ctx.db, cutoff)
    if errs:
        return {"errors": errs, "done": [], "failed": []}
    done: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    items: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for r in rows:
        v, verrs = CR.validate(ctx, m, account=r["account"], asset=r["asset"], kind=r["kind"], qty=r["qty"],
                               mode="settle", evidence=r["evidence"], acq_date=None, cost_eur=None)
        if v is None:
            failed.append({**r, "errors": verrs, "tx_id": None})
        else:
            items.append((r, v))
    out = CR.book(ctx, cutoff, m, [v for _r, v in items], version, refresh=False) if items else {
        "errors": [], "results": []}
    if out["errors"]:
        return {"errors": out["errors"], "done": [], "failed": []}
    for (r, _v), res in zip(items, out["results"], strict=True):
        (failed if res["errors"] else done).append({**r, "errors": res["errors"], "tx_id": res.get("tx_id")})
    accounts = {d["account"] for d in done}
    re_ok = False
    if reapprove and accounts and FX.state(ctx) is not None:
        re_ok = not FX.reapprove(ctx, accounts)["errors"]
    if done and not re_ok:  # sonst hat die Neu-Freigabe bereits aktualisiert
        after.refresh_cases(ctx)
    log.info("Stichtag-Ausgleich %s: %d gebucht, %d fehlgeschlagen", cutoff, len(done), len(failed))
    return {"errors": [], "done": done, "failed": failed, "reapproved": re_ok}


def apply_form(ctx: Any, cutoff: Any, form: Mapping[str, Any], *, reapprove: bool = True,
               expected: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Entscheidungen des Formulars buchen (je Zeile eine Buchung; ein Fehler stoppt die übrigen Zeilen nicht).
    ``expected`` (Zeile → angezeigter Fingerabdruck): nur buchen, wenn jede gewählte Zeile genau so angezeigt wurde."""
    from app.diagnosis import actions as A

    errs = CR.run_errors(ctx.db, cutoff)
    if errs:
        return {"errors": errs, "done": [], "failed": []}
    version = A.data_version(ctx.db)  # vor der Planung lesen
    ms = CO.models(ctx, cutoff)
    if not ms:
        return {"errors": ["Keine Daten vorhanden."], "done": [], "failed": []}
    pl = plan(ctx, ms[0])
    res = resolve(ms[0], pl, form)
    if res["errors"]:
        return {"errors": res["errors"], "done": [], "failed": []}
    if not res["chosen"]:
        return {"errors": ["Nichts zum Ausgleichen ausgewählt."], "done": [], "failed": []}
    if expected is not None:
        berrs = binding_errors(res["chosen"], expected)
        if berrs:
            return {"errors": berrs, "done": [], "failed": [], "changed": True}
    return _book(ctx, cutoff, res["chosen"], reapprove=reapprove, m=ms[0], version=version)


# Bestätigte Anzeigen ohne Fingerabdruck im Formular (z. B. Zusammenfassung über „Auswahl prüfen“ in einem Formular
# ohne versteckte Felder): je Formularinhalt bzw. Aufgabe der angezeigte Stand, einmal verwendbar.
PREVIEW_KEY = "cutover.settle_previews"
SHOWN_KEY = "cutover.case_settle_shown"
_KEEP = 30


def form_key(cutoff: Any, form: Mapping[str, Any]) -> str:
    items = sorted((str(k), str(v)) for k, v in form.items()
                   if str(k).startswith(("dec_", "acc_", "tgt_")) or k == "small_all")
    return hashlib.sha256(json.dumps([str(cutoff), items]).encode()).hexdigest()[:24]


def _remember(db: Any, key: str, name: str, value: Any) -> None:
    try:
        cur = dict(db.get_state(key) or {})
        cur.pop(name, None)
        cur[name] = value
        db.set_state(key, dict(list(cur.items())[-_KEEP:]))
    except Exception as e:  # nur Komfort für Formulare ohne Fingerabdruck – die Bindung selbst bleibt
        log.debug("Angezeigter Ausgleich nicht gespeichert: %s", e)


def _take(db: Any, key: str, name: str) -> Any:
    cur = dict(db.get_state(key) or {})
    return cur.get(name)


def _forget(db: Any, key: str, name: str) -> None:
    cur = dict(db.get_state(key) or {})
    if cur.pop(name, None) is not None:
        db.set_state(key, cur)


def remember_preview(db: Any, cutoff: Any, form: Mapping[str, Any], chosen: list[dict[str, Any]]) -> None:
    _remember(db, PREVIEW_KEY, form_key(cutoff, form), {r["id"]: r["fp"] for r in chosen})


def expected_for(db: Any, cutoff: Any, form: Mapping[str, Any]) -> tuple[dict[str, str], bool]:
    """Angezeigter Stand für eine Buchung: Fingerabdrücke aus dem Formular (``fp_<id>``, Liste bzw. Zusammenfassung)
    oder – ohne solche Felder – die zuletzt mit genau diesem Formularinhalt gezeigte Zusammenfassung. Zweiter Wert:
    stammt aus dem gespeicherten Stand (nach dem Buchen verbraucht)."""
    fps = {str(k)[3:]: str(v) for k, v in form.items() if str(k).startswith("fp_") and v}
    if fps:
        return fps, False
    return dict(_take(db, PREVIEW_KEY, form_key(cutoff, form)) or {}), True


def forget_preview(db: Any, cutoff: Any, form: Mapping[str, Any]) -> None:
    _forget(db, PREVIEW_KEY, form_key(cutoff, form))


def remember_shown(db: Any, kid: str, value: str) -> None:
    _remember(db, SHOWN_KEY, kid, value)


def shown(db: Any, kid: str) -> str:
    return str(_take(db, SHOWN_KEY, kid) or "")


def forget_shown(db: Any, kid: str) -> None:
    _forget(db, SHOWN_KEY, kid)


def rows_for_case(ctx: Any, finding_ids: list[str], *, _m: Any = None, report: Any = None) -> list[dict[str, Any]]:
    """Ausgleichszeilen (nur buchbare) zu den Positionen eines Klärungsfalls – leer ohne festgeschriebenen Stichtag oder
    ohne Beleg, der vom berechneten Bestand abweicht."""
    from app.cutover import guard
    from app.diagnosis.engine import report_for

    cutoff = guard.cutoff_of(ctx.db)
    if cutoff is None:
        return []
    ms = [_m] if _m is not None else CO.models(ctx, cutoff)
    if not ms:
        return []
    report = report if report is not None else report_for(ctx)
    pos = {tuple(p) for fid in finding_ids if (f := report.by_id(fid)) is not None for p in f.positions}
    return [r for r in plan(ctx, ms[0], report)["rows"] if (r["account"], r["asset"]) in pos and r["qty"] > 0]


def settle_case(ctx: Any, finding_ids: list[str], *, expect: str | None = None) -> dict[str, Any]:
    """Alle abweichenden Positionen eines Klärungsfalls auf den Beleg ausgleichen (ausdrückliche Wahl der Person,
    auch bei Zeilen, die in der Liste „kritisch“ wären). ``expect``: Bestätigungsschlüssel der angezeigten Zeilen
    (:func:`digest`) – weicht der jetzige Ausgleich davon ab, wird nichts gebucht (0.107.0, N34)."""
    from app.cutover import guard
    from app.diagnosis import actions as A

    cutoff = guard.cutoff_of(ctx.db)
    if cutoff is None:
        return {"errors": ["Für diesen Fall gibt es keinen Beleg, auf den sich ausgleichen ließe."], "done": [],
                "failed": []}
    version = A.data_version(ctx.db)  # vor der Planung lesen
    ms = CO.models(ctx, cutoff)
    rows = rows_for_case(ctx, finding_ids, _m=ms[0]) if ms else []
    if not rows:
        return {"errors": ["Für diesen Fall gibt es keinen Beleg, auf den sich ausgleichen ließe."], "done": [],
                "failed": []}
    if expect is not None and digest(rows) != expect:
        return {"errors": [f"{CHANGED}: " + "; ".join(_row_text(r) for r in rows[:4]) + "."], "done": [],
                "failed": [], "changed": True}
    return _book(ctx, cutoff, rows, reapprove=True, m=ms[0], version=version)


def apply(ctx: Any, cutoff: Any, ids: set[str], *, reapprove: bool = True) -> dict[str, Any]:
    """Ausgewählte Zeilen auf den Beleg ausgleichen (``ids`` = Zeilenkennungen)."""
    return apply_form(ctx, cutoff, {f"dec_{i}": "settle" for i in ids}, reapprove=reapprove)
