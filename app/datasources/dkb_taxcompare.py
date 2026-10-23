"""Steuervergleich DKB ↔ Portfolia (Phase 7, reine Abweichungsanzeige).

Die **DKB-Seite** stammt ausschließlich aus den Verkaufs- und Ertragsabrechnungen (``dkb_tax``, ohne
Steuerbescheinigung); die **Portfolia-Seite** aus dem Steuer-Regelwerk (``capital_for_accounts``,
``include_domestic`` **nur** für das Konto dieser Datenquelle) und den verknüpften Buchungen. Es wird nichts
verändert; die einzige Schreibaktion ist die ausdrücklich bestätigte Vorbelegung von Verlustvortrag und
Zusammenveranlagung (``apply_proposal``).

Drei Ebenen: je **Verkauf** (Veräußerungsgewinn, einbehaltene Steuer, Lot-Auswahl), je **Ertrag** (Brutto,
Quellensteuer, KapESt/Soli) und je **Jahr** (Summen, Topf-Endstände). Schwellen: unter ``round`` Rundung, ab
``info`` Hinweis, ab ``warn`` Warnung. Grenzen: Die DKB rechnet laufend je Transaktion (Erstattung sofort),
Portfolia am Jahresende – einzelne Steuerbeträge sind deshalb nur über die Topfstände vergleichbar;
Kapitalmaßnahmen mit eigener Steuer, Teilfreistellung und Einstände vor dem Archiv führen zu erwartbaren
Abweichungen.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from app.datasources import dkb as D
from app.documentimport.dkb_tax import TAX_KINDS, TaxInfo, read_tax
from app.documentimport.extract import extract_document
from app.tax.service import tax_service

log = logging.getLogger(__name__)

ZERO = Decimal(0)
CENT = Decimal("0.01")
THRESHOLDS = {"round": Decimal("0.05"), "info": Decimal("1.00"), "warn": Decimal("10.00")}
DOCTYPES = ("abrechnung_verkauf", "ertrag")
TAX_V = 1  # Version der Einleseregeln (bei Änderung werden die Belege erneut gelesen)
MAX_READ = 300
LEVEL_LABEL = {"gleich": "gleich", "rundung": "Rundung", "klein": "klein", "info": "Hinweis", "warn": "Warnung",
               "offen": "nicht vergleichbar"}


def _meta(raw: str | None) -> dict[str, Any]:
    try:
        return json.loads(raw or "{}")
    except ValueError:
        return {}


def level(diff: Decimal | None, th: dict[str, Decimal]) -> str:
    if diff is None:
        return "offen"
    a = abs(diff)
    if a == 0:
        return "gleich"
    if a < th["round"]:
        return "rundung"
    if a < th["info"]:
        return "klein"
    return "warn" if a >= th["warn"] else "info"


def thresholds(params: dict[str, str] | None = None) -> dict[str, Decimal]:
    """Schwellen aus Abfrageparametern (``hinweis``, ``warnung``, ``rundung``, Dezimalkomma erlaubt), begrenzt."""
    th = dict(THRESHOLDS)
    for key, name in (("round", "rundung"), ("info", "hinweis"), ("warn", "warnung")):
        raw = (params or {}).get(name)
        if raw:
            try:
                v = Decimal(raw.replace(",", "."))
            except ArithmeticError:
                continue
            if Decimal(0) <= v <= Decimal(100000):
                th[key] = v
    if not th["round"] <= th["info"] <= th["warn"]:
        return dict(THRESHOLDS)
    return th


# -- Einlesen der Steuerangaben -------------------------------------------------------------------------------------

def pending(ctx: Any, sid: int) -> int:
    n = 0
    for r in ctx.db.q(f"SELECT meta FROM bank_doc WHERE source_id=? AND status!='error' AND doctype IN "
                      f"({','.join('?' * len(DOCTYPES))})", (sid, *DOCTYPES)):
        if _meta(r["meta"]).get("tax_v") != TAX_V:
            n += 1
    return n


def read_missing(ctx: Any, sid: int, ds_name: str, limit: int = MAX_READ) -> dict[str, int]:
    """Steuerangaben der noch nicht gelesenen Verkaufs-/Ertragsabrechnungen aus den abgelegten Originalen in
    ``bank_doc.meta`` eintragen (höchstens ``limit`` je Aufruf). Protokolliert werden nur Zähler."""
    db = ctx.db
    root = D.root_dir(ctx, sid, ds_name)
    rows = db.q(f"SELECT id, rel_path, meta FROM bank_doc WHERE source_id=? AND status!='error' AND doctype IN "
                f"({','.join('?' * len(DOCTYPES))}) ORDER BY doc_date, id", (sid, *DOCTYPES))
    todo = [r for r in rows if _meta(r["meta"]).get("tax_v") != TAX_V]
    out = {"read": 0, "without": 0, "failed": 0, "left": max(0, len(todo) - limit)}
    for r in todo[:limit]:
        p = D._inside(root, r["rel_path"])
        if p is None:
            out["failed"] += 1
            continue
        try:
            info = read_tax(extract_document(p.read_bytes(), ocr=False), p.name)
        except Exception as exc:  # defekte PDF o. Ä. darf den Rest nicht aufhalten
            log.warning("Steuerangaben: Beleg %s nicht lesbar (%s)", r["id"], type(exc).__name__)
            out["failed"] += 1
            continue
        meta = _meta(r["meta"])
        meta["tax_v"] = TAX_V
        meta.pop("tax", None)
        if info is not None:
            meta["tax"] = info.to_json()
            out["read"] += 1
        else:
            out["without"] += 1
        db.x("UPDATE bank_doc SET meta=? WHERE id=?", (json.dumps(meta, ensure_ascii=False), r["id"]))
    log.info("Steuerangaben gelesen: %s, ohne Angaben: %s, fehlgeschlagen: %s, offen: %s", out["read"],
             out["without"], out["failed"], out["left"])
    return out


def years(ctx: Any, sid: int) -> list[int]:
    return sorted({int(r["y"]) for r in ctx.db.q(
        f"SELECT DISTINCT substr(doc_date,1,4) AS y FROM bank_doc WHERE source_id=? AND doc_date IS NOT NULL AND "
        f"doctype IN ({','.join('?' * len(DOCTYPES))})", (sid, *DOCTYPES)) if r["y"] and str(r["y"]).isdigit()},
        reverse=True)


# -- Portfolia-Seite --------------------------------------------------------------------------------------------------

def _near(txs: list[Any], account: str, day: date, tags: tuple[str, ...], window: int, used: set[str]) -> tuple[
        Decimal, list[str]]:
    """Steuerzeilen (EUR) desselben Wertpapiers und Kontos in der Nähe des Datums: Abzüge positiv, Erstattungen
    (Einzahlung ``refund``) negativ. Jede Zeile zählt höchstens einmal (``used``)."""
    amount, ids = ZERO, []
    for t in sorted(txs, key=lambda x: (abs((x.date - day).days), x.tx_id)):
        if t.tx_id in used or abs((t.date - day).days) > window:
            continue
        if t.type == "withdrawal" and t.tag in tags and t.from_account == account and t.from_asset == "EUR":
            amount += t.from_qty or ZERO
        elif t.type == "deposit" and t.tag == "refund" and "tax" in tags and t.to_account == account \
                and t.to_asset == "EUR":
            amount -= t.to_qty or ZERO
        else:
            continue
        used.add(t.tx_id)
        ids.append(t.tx_id)
    return amount, ids


def _lots_state(info: TaxInfo, parts: list[Any]) -> tuple[str, str]:
    """Lot-Auswahl der DKB (Anschaffungsgeschäfte) gegen die Lot-Teile von Portfolia (Anschaffungsdatum und Menge)."""
    if not info.lots:
        return "offen", "Die Abrechnung nennt keine Anschaffungsgeschäfte."
    dkb: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for lot in info.lots:
        dkb[lot["date"]] += lot["qty"]
    mine: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for p in parts:
        if p.acq_date is not None:
            mine[p.acq_date.isoformat()] += p.qty
    q = Decimal("0.0001")
    a = {k: v.quantize(q) for k, v in dkb.items()}
    b = {k: v.quantize(q) for k, v in mine.items()}
    if a == b:
        return "gleich", "gleiche Käufe (Datum und Menge)"

    def fmt(m: dict[str, Decimal]) -> str:
        return "; ".join(f"{date.fromisoformat(k):%d.%m.%Y} {v.normalize():f} Stk." for k, v in sorted(m.items()))

    if sum(a.values(), ZERO) == sum(b.values(), ZERO):
        return "abweichend", f"andere Lot-Auswahl – DKB: {fmt(a)}; Portfolia: {fmt(b)}"
    return "abweichend", f"andere Mengen – DKB: {fmt(a)}; Portfolia: {fmt(b)}"


def _causes(disposal: Any, pf_txs: dict[str, Any], lots_state: str, diff: Decimal | None, th: dict[str, Decimal]
            ) -> list[str]:
    out: list[str] = []
    if diff is None or abs(diff) < th["round"]:
        return out
    if disposal.missing_basis:
        out.append("Einstand fehlt in Portfolia")
    acq = [pf_txs[p.acq_tx] for p in disposal.parts if p.acq_tx and p.acq_tx in pf_txs]
    if any("geschätzt" in (t.note or "") for t in acq):
        out.append("Gebühr geschätzt (Kosteninformation)")
    if any(t.origin == "plan" for t in acq):
        out.append("Einstand aus Sparplan-Schätzung")
    if any(p.via for p in disposal.parts):
        out.append("Kapitalmaßnahme seit der Anschaffung")
    if lots_state == "abweichend":
        out.append("andere Lot-Auswahl")
    return out or ["Ursache nicht erkennbar (Einstand/Gebühren prüfen)"]


def _sale_rows(infos: list[tuple[Any, TaxInfo]], links: dict[str, list[str]], inp: Any, account: str,
               th: dict[str, Decimal], names: dict[str, str]) -> list[dict[str, Any]]:
    pf_txs = {t.tx_id: t for t in inp.pf.txs}
    disp = {d.tx_id: d for d in inp.ledger.disposals}
    taxes_by_asset: dict[str, list[Any]] = defaultdict(list)
    for t in inp.pf.txs:
        if t.related_asset and t.tag in ("tax", "refund"):
            taxes_by_asset[t.related_asset].append(t)
    used: set[str] = set()
    rows = []
    for b, info in infos:
        if info.kind != "verkauf":
            continue
        row: dict[str, Any] = {"doc_id": b["id"], "date": b["doc_date"], "isin": info.isin,
                               "name": names.get(info.isin, info.isin), "dkb_gain": info.gain, "dkb_tax": info.tax,
                               "pf_gain": None, "pf_tax": None, "gain_diff": None, "tax_diff": None, "tx": "",
                               "causes": [], "lots": "", "lots_state": "offen", "lots_dkb": info.lots}
        txs = [pf_txs[t] for t in links.get(b["sha256"], []) if t in pf_txs and pf_txs[t].type == "sell"]
        d = disp.get(txs[0].tx_id) if txs else None
        if not txs:
            row["note"] = "keine Verkaufsbuchung zugeordnet"
        elif d is None:
            row["tx"] = txs[0].tx_id
            row["note"] = "Buchung liegt nicht im Steuerlauf (Konto/Jahr)"
        else:
            row["tx"] = d.tx_id
            row["pf_gain"] = d.gain
            row["gain_diff"] = d.gain - (info.gain or ZERO) if info.gain is not None else None
            row["lots_state"], row["lots"] = _lots_state(info, d.parts)
            row["pf_tax"], _ = _near(taxes_by_asset.get(txs[0].from_asset or "", []), account, txs[0].date,
                                     ("tax",), 1, used)
            row["tax_diff"] = row["pf_tax"] - info.tax
            row["causes"] = _causes(d, pf_txs, row["lots_state"], row["gain_diff"], th)
        row["gain_level"] = level(row["gain_diff"], th)
        row["tax_level"] = level(row["tax_diff"], th)
        rows.append(row)
    return rows


def _income_rows(infos: list[tuple[Any, TaxInfo]], links: dict[str, list[str]], inp: Any, account: str,
                 th: dict[str, Decimal], names: dict[str, str]) -> list[dict[str, Any]]:
    pf_txs = {t.tx_id: t for t in inp.pf.txs}
    inc = {e.tx_id: e for e in inp.ledger.income}
    wht: dict[str, list[Any]] = defaultdict(list)
    tax: dict[str, list[Any]] = defaultdict(list)
    for t in inp.pf.txs:
        if t.related_asset and t.tag == "withholding_tax":
            wht[t.related_asset].append(t)
        elif t.related_asset and t.tag in ("tax", "refund"):
            tax[t.related_asset].append(t)
    used_w: set[str] = set()
    used_t: set[str] = set()
    rows = []
    for b, info in infos:
        if info.kind != "ertrag":
            continue
        row: dict[str, Any] = {"doc_id": b["id"], "date": b["doc_date"], "isin": info.isin,
                               "name": names.get(info.isin, info.isin), "dkb_gross": info.gross, "dkb_qst": info.qst,
                               "dkb_tax": info.tax, "pf_gross": None, "pf_qst": None, "pf_tax": None, "tx": "",
                               "gross_diff": None, "qst_diff": None, "tax_diff": None}
        tx = next((pf_txs[t] for t in links.get(b["sha256"], []) if t in pf_txs and t in inc), None)
        if tx is None:
            row["note"] = "keine Ertragsbuchung zugeordnet"
        else:
            e = inc[tx.tx_id]
            rel = e.related_asset or e.asset
            row["tx"] = tx.tx_id
            row["pf_gross"] = e.value_eur
            row["pf_qst"], _ = _near(wht.get(rel, []), account, tx.date, ("withholding_tax",), 7, used_w)
            row["pf_tax"], _ = _near(tax.get(rel, []), account, tx.date, ("tax",), 7, used_t)
            row["gross_diff"] = e.value_eur - (info.gross or ZERO) if info.gross is not None else None
            row["qst_diff"] = row["pf_qst"] - info.qst
            row["tax_diff"] = row["pf_tax"] - info.tax
        for k in ("gross", "qst", "tax"):
            row[f"{k}_level"] = level(row[f"{k}_diff"], th)
        row["worst"] = max((row[f"{k}_level"] for k in ("gross", "qst", "tax")), key=_rank)
        rows.append(row)
    return rows


def _rank(lv: str) -> int:
    return ("gleich", "rundung", "klein", "offen", "info", "warn").index(lv)


# -- Jahr -----------------------------------------------------------------------------------------------------------

def _pots(infos: list[tuple[Any, TaxInfo]]) -> dict[str, Any]:
    chain = [i for _b, i in infos if i.before and i.after]
    return {"first": chain[0] if chain else None, "last": chain[-1] if chain else None, "count": len(chain)}


def _year_summary(infos: list[tuple[Any, TaxInfo]], cap: dict[str, Any], th: dict[str, Decimal]) -> dict[str, Any]:
    k, pot = cap["capital"], cap["pot"]
    b = k["B"]
    pf_gain = sum((b[x]["share_gain"] + b[x]["share_loss"] + b[x]["other_gain"] + b[x]["other_loss"]
                   for x in b), ZERO)
    pf_div = sum((b[x]["dividends"] for x in b), ZERO)
    pf_wht = sum((b[x]["wht_raw"] for x in b), ZERO)
    pf_kest = sum((b[x]["kest"] for x in b), ZERO)
    sales = [i for _b, i in infos if i.kind == "verkauf"]
    incs = [i for _b, i in infos if i.kind == "ertrag"]
    dkb_gain = sum((i.gain or ZERO for i in sales), ZERO)
    dkb_gross = sum((i.gross or ZERO for i in incs), ZERO)
    dkb_qst = sum((i.qst for i in incs), ZERO)
    dkb_tax = sum((i.tax for i in sales + incs), ZERO)
    est = pot["est"] + pot["soli"] + pot["kist"]
    lines = [
        {"label": "Veräußerungsgewinne/-verluste (Summe)", "dkb": dkb_gain, "pf": pf_gain,
         "hint": "Portfolia: alle Verkäufe des Kontos im Jahr, auch ohne DKB-Abrechnung im Archiv"},
        {"label": "Erträge brutto", "dkb": dkb_gross, "pf": pf_div, "hint": ""},
        {"label": "Quellensteuer einbehalten", "dkb": dkb_qst, "pf": pf_wht, "hint": ""},
        {"label": "Kapitalertragsteuer + Soli + Kirchensteuer – gebucht", "dkb": dkb_tax, "pf": pf_kest,
         "hint": "DKB netto nach Erstattungen; Portfolia: Steuerbuchungen des Kontos im Jahr"},
        {"label": "Abgeltungsteuer + Soli + Kirchensteuer – Schätzung zum Jahresende", "dkb": dkb_tax, "pf": est,
         "hint": "Die DKB rechnet laufend und erstattet sofort; Portfolia rechnet am Jahresende"},
    ]
    for ln in lines:
        ln["diff"] = ln["pf"] - ln["dkb"]
        ln["level"] = level(ln["diff"], th)
    p = _pots(infos)
    last = p["last"]
    pb_end = pot["pb_left"] - pot["pb_used"]
    pots = []
    if last is not None:
        for key, label, mine in (("aktien", "Verlusttopf Aktien", pot["share_carry"]),
                                 ("sonstige", "Verlusttopf Sonstige", pot["other_carry"]),
                                 ("pauschbetrag", "Sparer-Pauschbetrag (Rest)", pb_end)):
            diff = mine - last.after[key]
            pots.append({"label": label, "dkb": last.after[key], "pf": mine, "diff": diff, "level": level(diff, th)})
    return {"lines": lines, "pots": pots, "pots_from": p["last"].doc_date if last else "", "pots_count": p["count"],
            "issues": cap["issues"]}


def proposal(infos: list[tuple[Any, TaxInfo]], options: dict[str, Any], cap: dict[str, Any]) -> dict[str, Any]:
    """Vorschlag aus „Vorher“ des ersten Belegs des Jahres – nur Werte, die von den Optionen abweichen."""
    first = _pots(infos)["first"]
    items: list[dict[str, Any]] = []
    note = ""
    if first is None:
        return {"items": items, "note": "Keine Topfstände in den Abrechnungen dieses Jahres."}
    b = first.before
    for key, label, val in (("loss_cf_kap_shares", "Verlustvortrag Aktien", b["aktien"]),
                            ("loss_cf_kap_other", "Verlustvortrag Sonstige", b["sonstige"])):
        cur = Decimal(str(options.get(key) or 0))
        if val != cur:
            items.append({"key": key, "label": label, "current": cur, "value": val, "scope": "für dieses Jahr"})
    pb = cap["pauschbetrag"]
    joint = options.get("joint")
    if b["pauschbetrag"] == pb["joint"] and not joint:
        items.append({"key": "joint", "label": "Zusammenveranlagung", "current": "nein", "value": True,
                      "scope": "gilt für alle Jahre"})
    elif b["pauschbetrag"] == pb["single"] and joint:
        items.append({"key": "joint", "label": "Zusammenveranlagung", "current": "ja", "value": False,
                      "scope": "gilt für alle Jahre"})
    elif b["pauschbetrag"] not in (pb["single"], pb["joint"]):
        note = (f"Sparer-Pauschbetrag laut DKB zu Jahresbeginn {b['pauschbetrag']:,.2f} € – entspricht weder Einzel- "
                "noch Zusammenveranlagung (ggf. Freistellungsauftrag teilweise erteilt oder früher im Jahr schon "
                "verbraucht); keine Vorbelegung.")
    return {"items": items, "note": note, "from_doc": first.doc_date}


def build(ctx: Any, sid: int, ds: Any, year: int, params: dict[str, str] | None = None) -> dict[str, Any]:
    """Vergleichsdaten eines Jahres (nur lesend)."""
    th = thresholds(params)
    db = ctx.db
    account = ds.account or ds.name
    rows = db.q(f"SELECT id, doc_date, sha256, meta FROM bank_doc WHERE source_id=? AND substr(doc_date,1,4)=? AND "
                f"status!='error' AND doctype IN ({','.join('?' * len(DOCTYPES))}) ORDER BY doc_date, id",
                (sid, str(year), *DOCTYPES))
    infos: list[tuple[Any, TaxInfo]] = []
    unread = unusable = 0
    for r in rows:
        m = _meta(r["meta"])
        if m.get("tax_v") != TAX_V:
            unread += 1
        elif "tax" in m:
            infos.append((r, TaxInfo.from_json(m["tax"])))
        else:
            unusable += 1
    out: dict[str, Any] = {"year": year, "thresholds": th, "account": account, "docs": len(rows), "unread": unread,
                           "unusable": unusable, "sales": [], "income": [], "summary": None, "proposal": None}
    svc = tax_service(ctx)
    pack = svc.pack()
    if not hasattr(pack, "capital_for_accounts"):
        out["error"] = "Das aktive Steuer-Regelwerk unterstützt den Vergleich nicht (nur Deutschland)."
        return out
    if not infos:
        return out
    opts = svc.options(pack, year)

    def run() -> tuple[Any, dict[str, Any]] | None:
        inp = svc.build_input(pack, opts)
        return None if inp is None else (inp, pack.capital_for_accounts(inp, year, opts, {account}))

    got = svc._cached(("dkbtaxcmp", pack.id, pack.version, year, account), run)
    if got is None:
        out["error"] = "Noch keine Buchungen vorhanden."
        return out
    inp, cap = got
    links: dict[str, list[str]] = defaultdict(list)
    for r in db.q("SELECT d.sha256, t.tx_id FROM tx_document t JOIN document d ON d.id=t.document_id "
                  "WHERE t.role IN ('abrechnung','ertrag')"):
        links[r["sha256"]].append(r["tx_id"])
    names = {a.isin: a.name for a in inp.pf.assets.values() if a.isin}
    out["sales"] = _sale_rows(infos, links, inp, account, th, names)
    out["income"] = _income_rows(infos, links, inp, account, th, names)
    out["summary"] = _year_summary(infos, cap, th)
    out["proposal"] = proposal(infos, opts, cap)
    out["levels"] = LEVEL_LABEL
    return out


def apply_proposal(ctx: Any, sid: int, ds: Any, year: int, keys: list[str]) -> tuple[bool, str]:
    """Bestätigte Vorbelegung: Der Vorschlag wird serverseitig neu berechnet, übernommen werden nur ausgewählte
    Schlüssel aus diesem Vorschlag (nie Werte aus dem Formular)."""
    data = build(ctx, sid, ds, year)
    prop = data.get("proposal") or {}
    chosen = {i["key"]: i["value"] for i in prop.get("items", []) if i["key"] in keys}
    if not chosen:
        return False, "Nichts zu übernehmen."
    svc = tax_service(ctx)
    pack = svc.pack()
    values = {k: (format(v.normalize(), "f") if isinstance(v, Decimal) else v) for k, v in chosen.items()}
    svc.save_options(pack, year, values)
    log.info("Steuer-Vorbelegung aus DKB-Abrechnungen übernommen (%s, %s)", year, ", ".join(sorted(chosen)))
    return True, "Übernommen: " + ", ".join(i["label"] for i in prop["items"] if i["key"] in chosen) + "."


__all__ = ["DOCTYPES", "LEVEL_LABEL", "TAX_KINDS", "THRESHOLDS", "apply_proposal", "build", "level", "pending",
           "proposal", "read_missing", "thresholds", "years"]
