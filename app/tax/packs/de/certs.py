"""Steuerbescheinigungen der Bank im Steuerreport: Kontrollwerte, Abgleich der Verkäufe, Hinweise."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from app.tax.base import ZERO, Column, Issue, Line, Table, TaxResult, fmt_eur, money

TOL_SAME = Decimal("0.05")
TOL_ROUND = Decimal(1)
TOL_WARN = Decimal(10)

_SUMMARY: tuple[tuple[str, str, str], ...] = (
    ("capital_total", "Höhe der Kapitalerträge", "kap_dom_total"),
    ("share_gains", "davon Gewinn aus Aktienveräußerungen", "kap_dom_share_gains"),
    ("pauschbetrag", "in Anspruch genommener Sparer-Pauschbetrag", "kap_dom_pauschbetrag"),
    ("kest", "Kapitalertragsteuer", "kap_dom_kest"),
    ("soli", "Solidaritätszuschlag", "kap_dom_soli"),
    ("wht_credited", "angerechnete ausländische Steuer", "kap_dom_wht"),
)


def _d(v: Any) -> Decimal:
    if v is None or v == "":
        return ZERO
    return v if isinstance(v, Decimal) else Decimal(str(v))


def cert_report(pack: Any, res: TaxResult, year: int, k: dict[str, Any], o: dict[str, Any], P: dict[str, Any],
                cert: dict[str, Any]) -> None:
    """Fügt Zusammenfassung, Hinweise und die Tabelle „Abgleich Verkäufe“ hinzu (nur wenn Belege hochgeladen sind)."""
    if not cert:
        return
    b, e = cert.get("bescheinigung") or {}, cert.get("ertraegnis") or {}
    bv = {key: _d(val) for key, val in (b.get("values") or {}).items()}
    if bv:
        _from_bescheinigung(pack, res, k, o, P, b, bv, e)
    if e:
        table, stats = sales_table(year, e, k)
        sk = res.section("kap")
        if sk is not None and table.rows:
            sk.tables.append(table)
        if stats["missing"] or stats["only_pf"] or stats["diff"]:
            res.issues.append(Issue(
                "warning" if stats["diff"] or stats["missing"] else "info", "cert_sales",
                f"Abgleich der Verkäufe mit der Erträgnisaufstellung: {stats['same']} übereinstimmend, "
                f"{stats['diff']} mit abweichendem Ergebnis, {stats['missing']} nur bei der Bank, "
                f"{stats['only_pf']} nur in Portfolia (siehe Tabelle „Abgleich Verkäufe“).",
                stats["diff"] + stats["missing"] + stats["only_pf"]))


def _from_bescheinigung(pack: Any, res: TaxResult, k: dict[str, Any], o: dict[str, Any], P: dict[str, Any],
                        b: dict[str, Any], bv: dict[str, Decimal], e: dict[str, Any]) -> None:
    forms = P.get("forms", {}).get("anlage_kap", {}).get("fields", {})
    S = res.summary
    S.append(Line("Steuerbescheinigung der Bank – maßgeblich für Depots mit inländischem Steuerabzug", None,
                  strong=True, kind="text"))
    for key, label, fid in _SUMMARY:
        if key in bv:
            ln = (forms.get(fid) or {}).get("line")
            S.append(Line(label, money(bv[key]), indent=1, note=f"Anlage KAP Zeile {ln}" if ln else ""))
    if bv.get("kist"):
        S.append(Line("Kirchensteuer", money(bv["kist"]), indent=1))
    if "share_gains_gross" in bv:
        S.append(Line("nachrichtlich: Aktiengewinne vor Verrechnung mit sonstigen Verlusten",
                      money(bv["share_gains_gross"]), indent=1))
    co = {key: _d(val) for key, val in (e.get("carry_out") or {}).items()}
    if co.get("shares") or co.get("other"):
        S.append(Line("ins neue Jahr vorgetragene Verlusttöpfe (Aktien / sonstige)", None, indent=1, kind="text",
                      text=f"{fmt_eur(money(co.get('shares')))} / {fmt_eur(money(co.get('other')))}"))
    for z in b.get("extra_lines") or []:
        res.issues.append(Issue("info", "cert_extra_line",
                                f"Die Steuerbescheinigung enthält weitere Angaben („Zeile {z} Anlage KAP“), die hier "
                                "nicht ausgewertet werden."))
    for n in b.get("notes") or []:
        res.issues.append(Issue("info", "cert_note", f"Steuerbescheinigung: {n}"))
    single = _d(P["capital"]["sparer_pauschbetrag_single"])
    if bv.get("pauschbetrag", ZERO) > single and not o.get("joint"):
        res.issues.append(Issue(
            "warning", "cert_joint",
            f"Die Steuerbescheinigung weist {fmt_eur(money(bv['pauschbetrag']))} Sparer-Pauschbetrag aus – das ist nur "
            "bei Zusammenveranlagung möglich. Option „Zusammenveranlagung“ aktivieren."))
    ci = {key: _d(val) for key, val in (e.get("carry_in") or {}).items()}
    extra = sum((_d((ev.get("amounts") or [0])[0]) for ev in e.get("events") or []
                 if ev.get("pot") and "Vorjahr" not in ev.get("kind", "")), ZERO)
    bd = k["B"]["domestic"]
    pf_share = bd["share_gain"] + bd["share_loss"]
    if "share_gains_gross" in bv:
        # Die Bank führt ihre Vorjahres-Verlusttöpfe in Spalte IV: ohne sie ist der Saldo mit den Verkäufen vergleichbar
        bank_sales = bv["share_gains_gross"] + ci.get("shares", ZERO)
        diff = pf_share - bank_sales
        res.issues.append(Issue(
            "warning" if abs(diff) >= TOL_WARN else "info", "cert_share_diff",
            f"Aktienverkäufe (Saldo) inländischer Depots: Bank {fmt_eur(money(bank_sales))} (ohne Verlusttopf aus dem "
            f"Vorjahr {fmt_eur(money(ci.get('shares', ZERO)))}), Portfolia {fmt_eur(money(pf_share))} (Abweichung "
            f"{fmt_eur(money(diff))}). Einzelheiten im Abgleich der Verkäufe."))
    if "capital_total" in bv:
        o2 = {**o, "loss_cf_kap_shares": ci.get("shares", ZERO), "loss_cf_kap_other": ci.get("other", ZERO)}
        dom = pack._pot(("domestic",), k, o2, P)
        diff = dom["base"] - bv["capital_total"]
        why = (f" Die Bank hat zusätzlich {fmt_eur(money(-extra))} Verlust „Einbuchung VT Sonstige“ (wertlose Anlagen) "
               "gebucht." if extra < 0 else "")
        res.issues.append(Issue(
            "warning" if abs(diff) >= TOL_WARN else "info", "cert_total_diff",
            "Kapitalerträge inländischer Depots nach Verlustverrechnung (mit den Vorjahres-Verlusttöpfen der "
            f"Bank): Bank {fmt_eur(money(bv['capital_total']))}, Portfolia {fmt_eur(money(dom['base']))} (Abweichung "
            f"{fmt_eur(money(diff))}).{why} Maßgeblich ist die Bescheinigung."))


def _is_isin(code: str) -> bool:
    return len(code) == 12 and code[:2].isalpha() and code[:2].isupper()


def sales_table(year: int, e: dict[str, Any], k: dict[str, Any]) -> tuple[Table, dict[str, int]]:
    """Verkäufe laut Erträgnisaufstellung ↔ Verkäufe inländischer Depots in Portfolia (ISIN, Datum ± 3 Tage)."""
    groups: dict[tuple[str, date], dict[str, Any]] = {}
    for r in k["rows_sales"]:
        if r["kind"] != "Inland":
            continue
        g = groups.setdefault((r["isin"], r["disp"]), {"name": r["asset"], "gain": ZERO, "used": False})
        g["gain"] += r["gain"]
    rows: list[dict[str, Any]] = []
    stats = {"same": 0, "diff": 0, "missing": 0, "only_pf": 0}
    for ev in e.get("events") or []:
        if not str(ev.get("kind", "")).lower().startswith("verkäufe") or not ev.get("amounts"):
            continue
        d = date.fromisoformat(ev["date"])
        bank = _d(ev["amounts"][0])
        near = sorted(groups.items(), key=lambda it: abs((it[0][1] - d).days))
        hit = next((g for (isin, gd), g in near
                    if isin == ev.get("isin") and abs((gd - d).days) <= 3 and not g["used"]), None)
        via = ""
        if hit is None:  # Wertpapier in Portfolia nur mit WKN geführt: über Datum und Betrag zuordnen
            tol = max(Decimal(10), abs(bank) * Decimal("0.02"))
            hit = next((g for (isin, gd), g in near if not _is_isin(isin) and abs((gd - d).days) <= 3
                        and not g["used"] and abs(g["gain"] - bank) <= tol), None)
            via = " (über Datum und Betrag, ISIN in Portfolia nicht hinterlegt)" if hit else ""
        base = {"date": d, "asset": ev.get("name", ""), "isin": ev.get("isin", ""), "bank": money(bank)}
        if hit is None:
            stats["missing"] += 1
            rows.append({**base, "pf": None, "diff": None, "status": "fehlt in Portfolia"})
            continue
        hit["used"] = True
        diff = money(hit["gain"] - bank)
        same = abs(diff) <= TOL_SAME
        stats["same" if same else "diff"] += 1
        status = "übereinstimmend" if same else ("Rundung" if abs(diff) <= TOL_ROUND else "abweichend")
        rows.append({**base, "pf": money(hit["gain"]), "diff": diff, "status": status + via})
    for (isin, gd), g in sorted(groups.items(), key=lambda it: (it[0][1], it[0][0])):
        if not g["used"] and gd.year == year:
            stats["only_pf"] += 1
            rows.append({"date": gd, "asset": g["name"], "isin": isin, "bank": None, "pf": money(g["gain"]),
                         "diff": None, "status": "nur in Portfolia (bei der Bank nicht als Verkauf geführt)"})
    rows.sort(key=lambda r: (r["date"], r["asset"]))
    table = Table(
        "k_cert", f"Abgleich Verkäufe {year}: Erträgnisaufstellung der Bank ↔ Portfolia (Inlandsdepots)",
        [Column("date", "Datum", "date", 0.8), Column("asset", "Wertpapier", "text", 1.9),
         Column("isin", "ISIN", "text", 1.1), Column("bank", "Ergebnis lt. Bank", "eur", 1.0, True),
         Column("pf", "Ergebnis Portfolia", "eur", 1.0, True), Column("diff", "Differenz", "eur", 0.8),
         Column("status", "Status", "text", 1.6)], rows, landscape=True,
        note="Die Bank weist je Verkauf nur das Ergebnis aus (Spalten III/IV); Portfolia summiert die Lots je "
             "Wertpapier und Tag.")
    return table, stats
