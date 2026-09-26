"""Regelwerk Deutschland (EStG / InvStG) – informativ, keine Steuerberatung.

Umfang:

* **Kryptowerte (§ 23 Abs. 1 S. 1 Nr. 2 EStG):** Veräußerungen/Tausch innerhalb der Haltefrist (1 Jahr)
  steuerpflichtig; Fristberechnung nach §§ 187, 188 BGB (steuerfrei ab dem Tag nach dem Jahrestag,
  29.02. → Fristende 28.02.). Verbrauchsfolge FIFO je Wallet (walletbezogene Betrachtung) oder global.
  Freigrenze (600 € bis 2023, 1.000 € ab 2024) auf den Jahressaldo, Verlustvortrag als Eingabe.
* **Leistungen (§ 22 Nr. 3 EStG):** Staking, Lending u. Ä. mit dem Wert bei Zufluss; Freigrenze 256 €.
  Zugeflossene Einheiten gelten als angeschafft (neue Haltefrist).
* **Kapitalerträge (§ 20 EStG, InvStG):** Aktien-Topf (Verluste nur mit Aktiengewinnen verrechenbar),
  sonstige Kapitalerträge, Investmentfonds mit Teilfreistellung, Vorabpauschale (Zufluss im Folgejahr,
  anteilig im Erwerbsjahr, Abzug bei Veräußerung), anrechenbare Quellensteuer (Deckel lt. Parameter).
  Konten mit inländischem Steuerabzug werden nachrichtlich ausgewiesen.

Alle Zahlenwerte stammen aus ``params.yaml`` (bzw. Override), alle Wahlrechte sind Optionen.
"""

from __future__ import annotations

import bisect
import dataclasses
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from app.ledger.engine import Disposal, EngineOptions
from app.ledger.models import AssetInfo
from app.tax import document as D
from app.tax.base import (
    ZERO,
    Column,
    DocumentSpec,
    FormField,
    Issue,
    Kpi,
    Line,
    Meter,
    OptionSpec,
    Overview,
    Release,
    ReportMeta,
    RulePack,
    Section,
    Table,
    TaxInput,
    TaxResult,
    fmt_eur,
    fmt_rate,
    money,
)
from app.tax.classify import ASSET_TYPES, FUND_TYPES
from app.util.timeutil import add_years

KIND_LABEL = {"sell": "Verkauf", "trade": "Tausch", "fee": "Gebühr", "transfer_fee": "Transfergebühr",
              "spend": "Bezahlung", "lost": "Verlust", "gift": "Schenkung", "withdrawal": "Abgang"}
ORIGIN_LABEL = {"buy": "Kauf", "trade": "Tausch", "income": "Ertrag", "deposit": "Zugang ohne Gegenbuchung",
                "corporate_action": "Kapitalmaßnahme", "phantom": "ohne Anschaffung"}
TAG_LABEL = {"staking": "Staking", "lending": "Lending", "interest": "Zinsen", "reward": "Reward", "bonus": "Bonus",
             "other_income": "Sonstiger Ertrag", "mining": "Mining", "airdrop": "Airdrop", "cashback": "Cashback",
             "fork": "Fork", "dividend": "Dividende"}
INCOME_DEFAULTS = {"staking": "22_3", "lending": "22_3", "interest": "22_3", "reward": "22_3", "bonus": "22_3",
                   "other_income": "22_3", "mining": "22_3", "airdrop": "22_3", "cashback": "none", "fork": "none"}
INCOME_CHOICES = (("22_3", "Sonstige Einkünfte § 22 Nr. 3 EStG"), ("kap", "Kapitalerträge § 20 EStG"),
                  ("none", "nicht steuerbar"))
FIRST_VP_YEAR = 2018
KINDS = ("foreign", "domestic")


def _d(v: Any) -> Decimal:
    if v is None or v == "":
        return ZERO
    return v if isinstance(v, Decimal) else Decimal(str(v))


class _Vp:
    """Vorabpauschale je Fondsanteil und Jahr (§ 18 InvStG), mit Cache."""

    def __init__(self, pack: GermanyPack, inp: TaxInput) -> None:
        self.pack = pack
        self.inp = inp
        self.units: dict[tuple[str, int], Decimal | None] = {}
        self.missing: dict[tuple[str, int], str] = {}
        self._qty: dict[str, tuple[list[date], list[Decimal]]] = {}

    def _qty_series(self, asset: str) -> tuple[list[date], list[Decimal]]:
        s = self._qty.get(asset)
        if s is None:
            evs = sorted((e for e in self.inp.ledger.qty_events if e.asset == asset), key=lambda e: e.date)
            dates, cum, run = [], [], ZERO
            for e in evs:
                run += e.delta
                if dates and dates[-1] == e.date:
                    cum[-1] = run
                else:
                    dates.append(e.date)
                    cum.append(run)
            s = (dates, cum)
            self._qty[asset] = s
        return s

    def qty_at(self, asset: str, d: date) -> Decimal:
        dates, cum = self._qty_series(asset)
        i = bisect.bisect_right(dates, d) - 1
        return cum[i] if i >= 0 else ZERO

    def dist_per_unit(self, asset: str, year: int) -> Decimal:
        total = ZERO
        for e in self.inp.ledger.income:
            if e.date.year != year or (e.related_asset or e.asset) != asset:
                continue
            q = self.qty_at(asset, e.date - timedelta(days=1)) or self.qty_at(asset, e.date)
            if q > 0:
                total += _d(e.value_eur) / q
        return total

    def unit(self, asset: str, year: int) -> Decimal | None:
        key = (asset, year)
        if key in self.units:
            return self.units[key]
        val: Decimal | None
        if year < FIRST_VP_YEAR:
            val = ZERO
        else:
            p = self.pack.p(year)
            bz = p.get("basiszins")
            if bz is None:
                val = None
                self.missing[key] = f"Basiszins {year} nicht hinterlegt"
            elif _d(bz) <= 0:
                val = ZERO
            else:
                first, last = self.inp.year_prices(asset, year) if self.inp.year_prices else (None, None)
                if not first or not last:
                    val = None
                    self.missing[key] = f"Kurse {year} fehlen (Anfang/Ende des Jahres)"
                else:
                    p0, p1 = first[1], last[1]
                    dist = self.dist_per_unit(asset, year)
                    basis = p0 * _d(bz) * _d(p["capital"]["vp_factor"])
                    cap = max(ZERO, p1 - p0 + dist)
                    val = max(ZERO, min(basis, cap) - dist)
        self.units[key] = val
        return val

    @staticmethod
    def factor(acq: date, year: int) -> Decimal:
        if acq.year < year:
            return Decimal(1)
        if acq.year == year:
            return Decimal(13 - acq.month) / Decimal(12)
        return ZERO

    def accumulated(self, asset: str, acq: date, sale_year: int) -> Decimal:
        """Summe der während der Besitzzeit angesetzten Vorabpauschalen je Anteil (Abzug nach § 19 InvStG)."""
        total = ZERO
        for y in range(max(acq.year, FIRST_VP_YEAR), sale_year):
            u = self.unit(asset, y)
            if u:
                total += u * self.factor(acq, y)
        return total


class GermanyPack(RulePack):
    id = "de"
    name = "Deutschland (EStG / InvStG)"
    country = "DE"
    code_version = "1"
    description = ("Private Veräußerungsgeschäfte mit Kryptowerten (§ 23 EStG), Leistungen (§ 22 Nr. 3 EStG), "
                   "Kapitalerträge inkl. Investmentfonds (§ 20 EStG, InvStG).")

    # -- Parameter & Optionen -----------------------------------------------------------------------
    def p(self, year: int) -> dict[str, Any]:
        years = self.years()
        if years:
            year = max(year, years[0])
        return self.year_params(year)

    def option_specs(self) -> list[OptionSpec]:
        return [
            OptionSpec("scope", "Verbrauchsfolge Kryptowerte", "choice", "wallet",
                       (("wallet", "FIFO je Wallet/Konto (BMF, walletbezogen)"), ("global", "FIFO über alle Wallets")),
                       "Das BMF-Schreiben zu Kryptowerten sieht eine walletbezogene Betrachtung vor.",
                       group="Kryptowerte"),
            OptionSpec("crypto_fee", "Gebühren in Kryptowerten beim Handel", "choice", "taxable",
                       (("taxable", "als Veräußerung behandeln (Tausch gegen Leistung)"),
                        ("ignore", "nicht steuerbar")),
                       "Der Gebührenwert mindert in beiden Fällen den Erlös des zugehörigen Geschäfts.",
                       group="Kryptowerte"),
            OptionSpec("transfer_fee", "Netzwerkgebühren bei Transfers zwischen eigenen Wallets", "choice", "ignore",
                       (("ignore", "nicht steuerbar und nicht abziehbar"), ("taxable", "als Veräußerung behandeln")),
                       group="Kryptowerte"),
            OptionSpec("lost", "Verlust/Diebstahl von Kryptowerten", "choice", "ignore",
                       (("ignore", "nicht berücksichtigen (keine Veräußerung)"),
                        ("loss", "als Veräußerung zu 0 € (Verlust)")), group="Kryptowerte"),
            OptionSpec("income_map", "Einstufung von Erträgen (je Tag im Import)", "map", dict(INCOME_DEFAULTS),
                       INCOME_CHOICES, "Zugeflossene Kryptowerte gelten unabhängig davon als angeschafft.",
                       group="Erträge"),
            OptionSpec("wk_22_3", "Werbungskosten zu Leistungen (§ 22 Nr. 3 EStG)", "amount", 0, per_year=True,
                       group="Erträge"),
            OptionSpec("joint", "Zusammenveranlagung (Sparer-Pauschbetrag doppelt)", "bool", False,
                       group="Kapitalerträge"),
            OptionSpec("pauschbetrag_used", "Bereits über Freistellungsaufträge genutzter Sparer-Pauschbetrag",
                       "amount", 0, per_year=True, group="Kapitalerträge"),
            OptionSpec("church_tax", "Kirchensteuersatz", "choice", "0",
                       (("0", "keine"), ("8", "8 %"), ("9", "9 %")), group="Kapitalerträge"),
            OptionSpec("include_domestic", "Konten mit inländischem Steuerabzug in Schätzung einbeziehen", "bool",
                       False, "Nur für Günstigerprüfung oder Verlustverrechnung zwischen Banken; maßgeblich sind "
                              "die Steuerbescheinigungen der Banken.", group="Kapitalerträge"),
            OptionSpec("marginal_rate", "Persönlicher Grenzsteuersatz für die Schätzung (%)", "percent", None,
                       per_year=True, group="Schätzung"),
            OptionSpec("loss_cf_23", "Verlustvortrag private Veräußerungsgeschäfte (§ 23) aus Vorjahren", "amount", 0,
                       per_year=True, group="Verlustvorträge"),
            OptionSpec("loss_cf_kap_shares", "Verlustvortrag Aktienveräußerungen aus Vorjahren", "amount", 0,
                       per_year=True, group="Verlustvorträge"),
            OptionSpec("loss_cf_kap_other", "Verlustvortrag sonstige Kapitalerträge aus Vorjahren", "amount", 0,
                       per_year=True, group="Verlustvorträge"),
        ]

    def engine_options(self, base: EngineOptions, options: dict[str, Any], snapshot_years: list[int]) -> EngineOptions:
        scope = "account" if options.get("scope", "wallet") == "wallet" else "global"
        return dataclasses.replace(base, scope=scope, method="fifo",
                                   snapshot_dates=tuple(date(y, 12, 31) for y in snapshot_years))

    def holding_end(self, asset: AssetInfo, acq: date) -> date | None:
        if not asset.is_crypto:
            return None
        years = int(self.p(acq.year)["crypto"]["holding_period_years"])
        return add_years(acq, years) + timedelta(days=1)

    def documents(self) -> list[DocumentSpec]:
        return [
            DocumentSpec("report", "Steuerreport (vollständig)",
                         "Zusammenfassung, Übertragungshilfe, alle Aufstellungen, Annahmen und Datenqualität"),
            DocumentSpec("anlage_so", "Aufstellung zur Anlage SO",
                         "Private Veräußerungsgeschäfte mit Kryptowerten und Leistungen nach § 22 Nr. 3 EStG – "
                         "als Beleg zur Steuererklärung"),
            DocumentSpec("anlage_kap", "Aufstellung zur Anlage KAP / KAP-INV",
                         "Veräußerungen, Dividenden/Ausschüttungen, Vorabpauschalen je Depot – als Beleg"),
        ]

    # -- Berechnung ------------------------------------------------------------------------------------
    def compute(self, inp: TaxInput, year: int, options: dict[str, Any]) -> TaxResult:
        o = {**self.defaults(), **(options or {})}
        o["income_map"] = {**INCOME_DEFAULTS, **(o.get("income_map") or {})}
        P = self.p(year)
        res = TaxResult(pack_id=self.id, pack_name=self.name, pack_version=self.version,
                        params_version=self.params.version, year=year,
                        params={k: v for k, v in P.items() if k != "forms"}, options=o)
        res.params["basiszins_prev"] = self.p(year - 1).get("basiszins")
        vp = _Vp(self, inp)
        c = self._crypto(inp, year, o, P, res)
        i = self._income(inp, year, o, P, res)
        k = self._capital(inp, year, o, P, res, vp)
        build_sections(self, res, c, i, k)
        res.has_activity = bool(c["rows_tax"] or c["rows_free"] or i["rows"] or k["activity"])
        self._summary(res, c, i, k, o, P)
        self._fields(res, c, i, k, o, P)
        self._estimate(res, c, i, k, o, P)
        self._assumptions(res, o, P)
        self._quality(inp, res, year, c, i, k, vp, P)
        res.data = {"crypto": {kk: vv for kk, vv in c.items() if not kk.startswith("rows")},
                    "income": {kk: vv for kk, vv in i.items() if kk != "rows"},
                    "capital": {kk: vv for kk, vv in k.items() if kk not in ("rows_sales", "rows_income", "rows_vp")}}
        return res

    # Kryptowerte § 23 ---------------------------------------------------------------------------------
    def _crypto_treatment(self, d: Disposal, o: dict[str, Any]) -> bool:
        if d.kind in ("gift", "withdrawal"):
            return False
        if d.kind == "lost":
            return o.get("lost") == "loss"
        if d.kind == "fee":
            return o.get("crypto_fee", "taxable") == "taxable"
        if d.kind == "transfer_fee":
            return o.get("transfer_fee", "ignore") == "taxable"
        return True

    def _crypto(self, inp: TaxInput, year: int, o: dict[str, Any], P: dict[str, Any],
                res: TaxResult) -> dict[str, Any]:
        rows_tax: list[dict[str, Any]] = []
        rows_free: list[dict[str, Any]] = []
        excluded: Counter[str] = Counter()
        origins: Counter[str] = Counter()
        missing = 0
        for d in inp.ledger.disposals:
            if d.date.year != year:
                continue
            a = inp.asset(d.asset)
            if not a.is_crypto:
                continue
            if not self._crypto_treatment(d, o):
                excluded[d.kind] += 1
                continue
            for p in d.parts:
                share = p.qty / d.qty if d.qty else ZERO
                wk = d.fee_eur * share
                gain = p.proceeds - p.cost
                if p.missing_basis or p.acq_date is None:
                    free_from, taxable = None, True
                    missing += 1
                else:
                    free_from = self.holding_end(a, p.acq_date)
                    taxable = free_from is None or d.date < free_from
                if p.origin in ("deposit", "phantom"):
                    origins[p.origin] += 1
                row = {"asset": a.symbol, "asset_id": a.asset_id, "name": a.name, "account": d.account,
                       "kind": KIND_LABEL.get(d.kind, d.kind), "acq": p.acq_date, "disp": d.date,
                       "days": (d.date - p.acq_date).days if p.acq_date else None, "qty": p.qty,
                       "price": money(p.proceeds + wk), "cost": money(p.cost), "wk": money(wk), "gain": money(gain),
                       "origin": ORIGIN_LABEL.get(p.origin, p.origin), "free_from": free_from, "tx": d.tx_id}
                (rows_tax if taxable else rows_free).append(row)
        net = sum((r["gain"] for r in rows_tax), ZERO)
        fg = _d(P["crypto"]["freigrenze_23"])
        if net <= 0 or net < fg:
            taxable = ZERO
        else:
            taxable = net
        lcf = _d(o.get("loss_cf_23"))
        after = max(ZERO, taxable - lcf) if taxable > 0 else ZERO
        return {
            "rows_tax": rows_tax, "rows_free": rows_free, "excluded": dict(excluded), "origins": dict(origins),
            "missing": missing, "net": net, "freigrenze": fg, "taxable": taxable, "after_lcf": after,
            "lcf_used": min(lcf, taxable) if taxable > 0 else ZERO,
            "gains": sum((r["gain"] for r in rows_tax if r["gain"] > 0), ZERO),
            "losses": sum((r["gain"] for r in rows_tax if r["gain"] < 0), ZERO),
            "price": sum((r["price"] for r in rows_tax), ZERO), "cost": sum((r["cost"] for r in rows_tax), ZERO),
            "wk": sum((r["wk"] for r in rows_tax), ZERO),
            "free_gain": sum((r["gain"] for r in rows_free), ZERO),
            "free_price": sum((r["price"] for r in rows_free), ZERO),
            "count_tax": len(rows_tax), "count_free": len(rows_free),
        }

    # Leistungen § 22 Nr. 3 --------------------------------------------------------------------------
    def _is_capital_income(self, inp: TaxInput, e: Any) -> bool:
        rel = inp.asset(e.related_asset) if e.related_asset else None
        a = inp.asset(e.asset)
        return bool((rel and rel.is_security) or a.is_security or (e.fiat and e.tag in ("dividend", "interest")))

    def _income_value(self, inp: TaxInput, e: Any) -> tuple[Decimal, str]:
        v = _d(e.value_eur)
        if v > 0 or not e.qty:
            return v, "Import"
        a = inp.asset(e.asset)
        if a.is_fiat and inp.fx_eur is not None:
            rate = inp.fx_eur(a.asset_id, e.date)
            if rate:
                return e.qty * rate, "EZB/Devisenkurs"
        elif inp.price_eur is not None:
            px = inp.price_eur(a.asset_id, e.date)
            if px:
                return e.qty * px, "Tageskurs"
        return ZERO, "fehlt"

    def _income(self, inp: TaxInput, year: int, o: dict[str, Any], P: dict[str, Any],
                res: TaxResult) -> dict[str, Any]:
        imap = o["income_map"]
        rows: list[dict[str, Any]] = []
        not_taxable: Counter[str] = Counter()
        kap_crypto: list[dict[str, Any]] = []
        valued: Counter[str] = Counter()
        for e in inp.ledger.income:
            if e.date.year != year or self._is_capital_income(inp, e):
                continue
            cat = imap.get(e.tag, "22_3")
            value, how = self._income_value(inp, e)
            valued[how] += 1
            a = inp.asset(e.asset)
            row = {"date": e.date, "asset": a.symbol, "name": a.name, "account": e.account, "qty": e.qty,
                   "value": money(value), "tag": TAG_LABEL.get(e.tag, e.tag), "tag_id": e.tag, "valued": how,
                   "tx": e.tx_id}
            if cat == "none":
                not_taxable[e.tag] += 1
            elif cat == "kap":
                kap_crypto.append(row)
            else:
                rows.append(row)
        total = sum((r["value"] for r in rows), ZERO)
        wk = _d(o.get("wk_22_3"))
        einkuenfte = total - wk
        fg = _d(P["crypto"]["freigrenze_22_3"])
        taxable = ZERO if einkuenfte < fg else einkuenfte
        by_tag: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for r in rows:
            by_tag[r["tag"]] += r["value"]
        return {"rows": rows, "total": total, "wk": wk, "einkuenfte": einkuenfte, "freigrenze": fg,
                "taxable": max(taxable, ZERO), "not_taxable": dict(not_taxable), "kap_rows": kap_crypto,
                "by_tag": dict(by_tag), "valued": dict(valued),
                "mining": any(r["tag_id"] == "mining" for r in rows)}

    # Kapitalerträge ---------------------------------------------------------------------------------
    def _capital(self, inp: TaxInput, year: int, o: dict[str, Any], P: dict[str, Any], res: TaxResult,
                 vp: _Vp) -> dict[str, Any]:
        B: dict[str, dict[str, Any]] = {k: {"share_gain": ZERO, "share_loss": ZERO, "other_gain": ZERO,
                                            "other_loss": ZERO, "dividends": ZERO, "interest": ZERO,
                                            "wht_raw": ZERO, "wht_credit": ZERO, "kest": ZERO,
                                            "fund_dist": defaultdict(lambda: ZERO),
                                            "fund_vp": defaultdict(lambda: ZERO),
                                            "fund_gain": defaultdict(lambda: ZERO)} for k in KINDS}
        rows_sales: list[dict[str, Any]] = []
        rows_income: list[dict[str, Any]] = []
        rows_vp: list[dict[str, Any]] = []
        heuristic_assets: set[str] = set()
        default_accounts: set[str] = set()
        old_fund_lots = 0
        foreign_div_without_wht: set[str] = set()
        valued: Counter[str] = Counter()
        cap = _d(P["capital"]["wht_credit_cap"])

        def kind_of(acc: str | None) -> str:
            k = inp.account_kinds.get(acc or "", "domestic")
            if inp.account_kind_source.get(acc or "") == "default" and acc:
                default_accounts.add(acc)
            return k if k in KINDS else "domestic"

        def type_of(asset_id: str) -> str:
            t = inp.asset_types.get(asset_id) or "share"
            if inp.asset_type_source.get(asset_id) == "heuristic":
                heuristic_assets.add(asset_id)
            return t

        # Veräußerungen
        for d in inp.ledger.disposals:
            if d.date.year != year:
                continue
            a = inp.asset(d.asset)
            if not a.is_security or d.kind in ("gift", "withdrawal"):
                continue
            t = type_of(a.asset_id)
            k = kind_of(d.account)
            for p in d.parts:
                share = p.qty / d.qty if d.qty else ZERO
                wk = d.fee_eur * share
                gain = p.proceeds - p.cost
                ded = ZERO
                if t in FUND_TYPES and p.acq_date is not None:
                    if p.acq_date.year < FIRST_VP_YEAR:
                        old_fund_lots += 1
                    ded = p.qty * vp.accumulated(a.asset_id, p.acq_date, year)
                    gain -= ded
                pot = "Aktien" if t == "share" else ("Fonds" if t in FUND_TYPES else "Sonstige")
                rows_sales.append({"account": d.account, "kind": "Ausland" if k == "foreign" else "Inland",
                                   "asset": a.name, "isin": a.isin or a.wkn or "", "type": ASSET_TYPES.get(t, t),
                                   "acq": p.acq_date, "disp": d.date, "qty": p.qty,
                                   "price": money(p.proceeds + wk), "cost": money(p.cost), "wk": money(wk),
                                   "vp": money(ded), "gain": money(gain), "pot": pot,
                                   "note": "Ausbuchung/Verlust" if d.kind == "lost" else ""})
                b = B[k]
                if t == "share":
                    b["share_gain" if gain >= 0 else "share_loss"] += gain
                elif t in FUND_TYPES:
                    b["fund_gain"][t] += gain
                else:
                    b["other_gain" if gain >= 0 else "other_loss"] += gain

        # Steuerereignisse (Quellensteuer, einbehaltene KapESt)
        wht_index: dict[tuple[str | None, str | None], list[Any]] = defaultdict(list)
        for te in inp.ledger.taxes:
            if te.date.year != year:
                continue
            if te.tag == "withholding_tax":
                wht_index[(te.account, te.related_asset)].append(te)
            else:
                B[kind_of(te.account)]["kest"] += _d(te.eur)
        used_wht: set[str] = set()

        # Dividenden, Ausschüttungen, Zinsen
        for e in inp.ledger.income:
            if e.date.year != year or not self._is_capital_income(inp, e):
                continue
            rel_id = e.related_asset if (e.related_asset and inp.asset(e.related_asset).is_security) else (
                e.asset if inp.asset(e.asset).is_security else None)
            value, how = self._income_value(inp, e)
            valued[how] += 1
            k = kind_of(e.account)
            b = B[k]
            if rel_id is None:  # Zinsen o. Ä. ohne Wertpapierbezug
                b["interest"] += value
                rows_income.append({"date": e.date, "account": e.account,
                                    "kind": "Ausland" if k == "foreign" else "Inland",
                                    "asset": "–", "type": TAG_LABEL.get(e.tag, e.tag), "gross": money(value),
                                    "wht": ZERO, "credit": ZERO, "valued": how})
                continue
            ra = inp.asset(rel_id)
            t = type_of(rel_id)
            wht = ZERO
            for te in wht_index.get((e.account, rel_id), []):
                if te.tx_id not in used_wht and abs((te.date - e.date).days) <= 7:
                    wht += _d(te.eur)
                    used_wht.add(te.tx_id)
            credit = min(wht, value * cap) if wht > 0 else ZERO
            b["wht_raw"] += wht
            b["wht_credit"] += credit
            if t in FUND_TYPES:
                b["fund_dist"][t] += value
            else:
                b["dividends"] += value
            if wht == 0 and (ra.isin or "")[:2] not in ("", "DE"):
                foreign_div_without_wht.add(ra.name)
            rows_income.append({"date": e.date, "account": e.account, "kind": "Ausland" if k == "foreign" else "Inland",
                                "asset": ra.name, "type": ("Ausschüttung" if t in FUND_TYPES else "Dividende"),
                                "gross": money(value), "wht": money(wht), "credit": money(credit), "valued": how})
        for te_list in wht_index.values():  # nicht zuordenbare Quellensteuer
            for te in te_list:
                if te.tx_id not in used_wht:
                    kk = kind_of(te.account)
                    B[kk]["wht_raw"] += _d(te.eur)
                    B[kk]["wht_credit"] += _d(te.eur)
                    res.issues.append(Issue(
                        "warning", "wht_unmatched",
                        f"Quellensteuer {te.tx_id} ohne zugehörige Dividende – voll als anrechenbar angesetzt, "
                        "bitte prüfen"))

        # Vorabpauschale des Vorjahres (gilt am ersten Werktag von `year` als zugeflossen)
        vy = year - 1
        for lot in inp.snapshot(date(vy, 12, 31)):
            a = inp.asset(lot.asset)
            if not a.is_security:
                continue
            t = type_of(a.asset_id)
            if t not in FUND_TYPES:
                continue
            unit = vp.unit(a.asset_id, vy)
            if unit is None:
                continue
            f = vp.factor(lot.acq_date, vy)
            amount = lot.qty * unit * f
            if amount <= 0:
                continue
            k = kind_of(lot.account)
            B[k]["fund_vp"][t] += amount
            rows_vp.append({"account": lot.account, "kind": "Ausland" if k == "foreign" else "Inland",
                            "asset": a.name, "type": ASSET_TYPES.get(t, t), "year": vy, "acq": lot.acq_date,
                            "qty": lot.qty, "unit": unit.quantize(Decimal("0.000001")), "factor": f,
                            "amount": money(amount)})

        activity = bool(rows_sales or rows_income or rows_vp or any(B[k]["kest"] for k in KINDS))
        for b in B.values():
            b["fund_dist"] = dict(b["fund_dist"])
            b["fund_vp"] = dict(b["fund_vp"])
            b["fund_gain"] = dict(b["fund_gain"])
        return {"B": B, "rows_sales": rows_sales, "rows_income": rows_income, "rows_vp": rows_vp,
                "activity": activity, "heuristic_assets": sorted(heuristic_assets),
                "default_accounts": sorted(default_accounts), "old_fund_lots": old_fund_lots,
                "foreign_div_without_wht": sorted(foreign_div_without_wht), "valued": dict(valued)}

    # -- Zusammenfassung, Formularfelder, Schätzung ----------------------------------------------------------
    @staticmethod
    def _fund_sum(b: dict[str, Any], key: str) -> Decimal:
        return sum(b[key].values(), ZERO)

    def _pot(self, kinds: tuple[str, ...], k: dict[str, Any], o: dict[str, Any], P: dict[str, Any]) -> dict[str, Any]:
        tf = P["capital"]["teilfreistellung"]
        share = other = fund_taxable = fund_gross = wht = ZERO
        for kind in kinds:
            b = k["B"][kind]
            share += b["share_gain"] + b["share_loss"]
            other += b["dividends"] + b["interest"] + b["other_gain"] + b["other_loss"]
            for t in FUND_TYPES:
                gross = b["fund_dist"].get(t, ZERO) + b["fund_vp"].get(t, ZERO) + b["fund_gain"].get(t, ZERO)
                fund_gross += gross
                fund_taxable += gross * (1 - _d(tf.get(t, 0)))
            wht += b["wht_credit"]
        share_after = share - _d(o.get("loss_cf_kap_shares"))
        share_carry = -share_after if share_after < 0 else ZERO
        total = other + fund_taxable + max(share_after, ZERO) - _d(o.get("loss_cf_kap_other"))
        other_carry = -total if total < 0 else ZERO
        base = max(total, ZERO)
        pb_total = _d(P["capital"]["sparer_pauschbetrag_joint" if o.get("joint") else "sparer_pauschbetrag_single"])
        pb_left = max(ZERO, pb_total - _d(o.get("pauschbetrag_used")))
        pb_used = min(pb_left, base)
        taxable = base - pb_used
        kist = _d(o.get("church_tax") or 0) / 100
        est = max(ZERO, (taxable - 4 * wht) / (4 + kist)) if taxable > 0 else ZERO
        return {"share": share, "other": other, "fund_gross": fund_gross, "fund_taxable": fund_taxable,
                "share_carry": share_carry, "other_carry": other_carry, "base": base, "pb_left": pb_left,
                "pb_used": pb_used, "taxable": taxable, "wht": wht, "est": est,
                "soli": est * _d(P["capital"]["soli_rate"]), "kist": est * kist}

    def _summary(self, res: TaxResult, c: dict[str, Any], i: dict[str, Any], k: dict[str, Any], o: dict[str, Any],
                 P: dict[str, Any]) -> None:
        S = res.summary
        S.append(Line("Private Veräußerungsgeschäfte mit Kryptowerten (§ 23 EStG)", None, strong=True, kind="text"))
        S.append(Line("Veräußerungen innerhalb der Haltefrist – Saldo", money(c["net"]), indent=1,
                      note=f"{len(c['rows_tax'])} Teilvorgänge"))
        S.append(Line("davon Gewinne", money(c["gains"]), indent=2))
        S.append(Line("davon Verluste", money(c["losses"]), indent=2))
        S.append(Line("steuerpflichtig nach Freigrenze", money(c["taxable"]), indent=1, strong=True,
                      note=f"Freigrenze {fmt_eur(c['freigrenze'])} (Gesamtgewinn unter der Grenze bleibt steuerfrei)"))
        if c["lcf_used"]:
            S.append(Line("nach Verlustvortrag", money(c["after_lcf"]), indent=1))
        S.append(Line("Steuerfreie Veräußerungen nach Ablauf der Haltefrist (nachrichtlich)", money(c["free_gain"]),
                      indent=1, note=f"{len(c['rows_free'])} Teilvorgänge, Erlös {fmt_eur(c['free_price'])}"))
        S.append(Line("Leistungen (§ 22 Nr. 3 EStG) – z. B. Staking, Lending", None, strong=True, kind="text"))
        S.append(Line("Einnahmen", money(i["total"]), indent=1, note=f"{len(i['rows'])} Zuflüsse"))
        if i["wk"]:
            S.append(Line("Werbungskosten", money(-i["wk"]), indent=1))
        S.append(Line("steuerpflichtig nach Freigrenze", money(i["taxable"]), indent=1, strong=True,
                      note=f"Freigrenze {fmt_eur(i['freigrenze'])}"))
        if k["activity"]:
            for kind, title in (("foreign", "Kapitalerträge ohne inländischen Steuerabzug (Ausland)"),
                                ("domestic", "Kapitalerträge mit inländischem Steuerabzug (nachrichtlich)")):
                b = k["B"][kind]
                vals = [b["share_gain"], b["share_loss"], b["dividends"], b["interest"], b["other_gain"],
                        b["other_loss"], self._fund_sum(b, "fund_dist"), self._fund_sum(b, "fund_vp"),
                        self._fund_sum(b, "fund_gain"), b["wht_raw"], b["kest"]]
                if not any(vals):
                    continue
                S.append(Line(title, None, strong=True, kind="text"))
                if b["share_gain"] or not b["share_loss"]:
                    S.append(Line("Aktienveräußerungen – Gewinne", money(b["share_gain"]), indent=1))
                if b["share_loss"]:
                    S.append(Line("Aktienveräußerungen – Verluste", money(b["share_loss"]), indent=1))
                if b["dividends"]:
                    S.append(Line("Dividenden (brutto)", money(b["dividends"]), indent=1))
                if b["interest"]:
                    S.append(Line("Zinsen", money(b["interest"]), indent=1))
                if b["other_gain"] or b["other_loss"]:
                    S.append(Line("Sonstige Veräußerungen – Saldo", money(b["other_gain"] + b["other_loss"]), indent=1))
                if any(b["fund_dist"].values()) or any(b["fund_vp"].values()) or any(b["fund_gain"].values()):
                    S.append(Line("Investmentfonds: Ausschüttungen", money(self._fund_sum(b, "fund_dist")), indent=1,
                                  note="vor Teilfreistellung"))
                    S.append(Line("Investmentfonds: Vorabpauschalen", money(self._fund_sum(b, "fund_vp")), indent=1,
                                  note=f"für {res.year - 1}, zugeflossen Anfang {res.year}"))
                    S.append(Line("Investmentfonds: Veräußerungen – Saldo", money(self._fund_sum(b, "fund_gain")),
                                  indent=1, note="nach Abzug früherer Vorabpauschalen, vor Teilfreistellung"))
                if b["wht_raw"]:
                    S.append(Line("Ausländische Quellensteuer (einbehalten / anrechenbar)", money(b["wht_raw"]),
                                  indent=1, note=f"anrechenbar {fmt_eur(b['wht_credit'])}"))
                if b["kest"]:
                    S.append(Line("Einbehaltene Kapitalertragsteuer lt. Daten", money(b["kest"]), indent=1))
        res.meters.append(Meter("fg23", "Freigrenze private Veräußerungsgeschäfte (§ 23 EStG)",
                                money(max(c["net"], ZERO)), money(c["freigrenze"]),
                                note="nur Kryptowerte aus diesem Import"))
        res.meters.append(Meter("fg22", "Freigrenze Leistungen (§ 22 Nr. 3 EStG)", money(max(i["einkuenfte"], ZERO)),
                                money(i["freigrenze"])))
        if k["activity"]:
            pot = self._pot(("foreign", "domestic") if o.get("include_domestic") else ("foreign",), k, o, P)
            pb_total = _d(P["capital"]["sparer_pauschbetrag_joint" if o.get("joint")
                                         else "sparer_pauschbetrag_single"])
            res.meters.append(Meter("pb", "Sparer-Pauschbetrag (§ 20 Abs. 9 EStG)",
                                    money(pot["base"] + _d(o.get("pauschbetrag_used"))), money(pb_total),
                                    kind="freibetrag", note="inkl. bereits genutzter Freistellungsaufträge"))

    def _field(self, P: dict[str, Any], form: str, fid: str, amount: Decimal | None, note: str = "",
               text: str | None = None, label: str | None = None, section: str | None = None) -> FormField:
        spec = P.get("forms", {}).get(form, {})
        f = spec.get("fields", {}).get(fid, {})
        line = f.get("line")
        return FormField(form=spec.get("title", form), section=section or f.get("section", ""), field_id=fid,
                         label=label or f.get("label", fid), amount=money(amount) if amount is not None else None,
                         line=str(line) if line not in (None, "") else None, note=note, text=text)

    def _fields(self, res: TaxResult, c: dict[str, Any], i: dict[str, Any], k: dict[str, Any], o: dict[str, Any],
                P: dict[str, Any]) -> None:
        F = res.fields
        if c["rows_tax"]:
            F.append(self._field(P, "anlage_so", "so_23_desc", None, text="Kryptowerte lt. beigefügter Aufstellung"))
            F.append(self._field(P, "anlage_so", "so_23_acq", None, text="diverse (lt. Aufstellung)"))
            F.append(self._field(P, "anlage_so", "so_23_disp", None, text="diverse (lt. Aufstellung)"))
            F.append(self._field(P, "anlage_so", "so_23_price", c["price"]))
            F.append(self._field(P, "anlage_so", "so_23_cost", c["cost"]))
            F.append(self._field(P, "anlage_so", "so_23_wk", c["wk"]))
            F.append(self._field(P, "anlage_so", "so_23_gain", c["net"],
                                 note="Saldo aller steuerpflichtigen Veräußerungen; Freigrenze prüft das Finanzamt"))
        if i["rows"]:
            F.append(self._field(P, "anlage_so", "so_22_3_desc", None,
                                 text="Erträge aus Kryptowerten (Staking/Lending u. Ä.) lt. Aufstellung"))
            F.append(self._field(P, "anlage_so", "so_22_3_income", i["total"]))
            if i["wk"]:
                F.append(self._field(P, "anlage_so", "so_22_3_wk", i["wk"]))
        b = k["B"]["foreign"]
        total = b["dividends"] + b["interest"] + b["share_gain"] + b["share_loss"] + b["other_gain"] + b["other_loss"]
        if any((total, b["share_gain"], b["share_loss"], b["other_loss"], b["wht_credit"])):
            F.append(self._field(P, "anlage_kap", "kap_foreign_total", total,
                                 note="Saldo aus Dividenden, Zinsen, Veräußerungsgewinnen und -verlusten"))
            if b["share_gain"]:
                F.append(self._field(P, "anlage_kap", "kap_foreign_share_gains", b["share_gain"]))
            if b["other_loss"]:
                F.append(self._field(P, "anlage_kap", "kap_foreign_losses_other", -b["other_loss"],
                                     note="als positiver Betrag"))
            if b["share_loss"]:
                F.append(self._field(P, "anlage_kap", "kap_foreign_losses_shares", -b["share_loss"],
                                     note="als positiver Betrag"))
            if b["wht_credit"]:
                F.append(self._field(P, "anlage_kap", "kap_wht", b["wht_credit"]))
        labels = P.get("forms", {}).get("anlage_kap_inv", {}).get("fund_labels", {})
        for t in sorted(FUND_TYPES):
            for key, fid in (("fund_dist", "inv_dist"), ("fund_vp", "inv_vp"), ("fund_gain", "inv_gain")):
                v = b[key].get(t, ZERO)
                if v:
                    spec = P.get("forms", {}).get("anlage_kap_inv", {}).get("fields", {}).get(fid, {})
                    F.append(self._field(P, "anlage_kap_inv", fid, v,
                                         label=f"{spec.get('label', fid)} – {labels.get(t, t)}"))
        if o.get("include_domestic"):
            bd = k["B"]["domestic"]
            dom_total = (bd["dividends"] + bd["interest"] + bd["share_gain"] + bd["share_loss"] + bd["other_gain"]
                         + bd["other_loss"])
            if dom_total or bd["kest"]:
                sec = "Kapitalerträge mit inländischem Steuerabzug – Kontrollwerte (Steuerbescheinigung maßgeblich)"
                F.append(self._field(P, "anlage_kap", "kap_dom_total", dom_total, section=sec,
                                     label="Kapitalerträge lt. Daten (Saldo)"))
                F.append(self._field(P, "anlage_kap", "kap_dom_kest", bd["kest"], section=sec,
                                     label="Einbehaltene Kapitalertragsteuer lt. Daten"))

    def _estimate(self, res: TaxResult, c: dict[str, Any], i: dict[str, Any], k: dict[str, Any], o: dict[str, Any],
                  P: dict[str, Any]) -> None:
        E = res.estimate
        rate = o.get("marginal_rate")
        base = c["after_lcf"] + i["taxable"]
        if rate not in (None, ""):
            r = _d(rate) / 100
            E.append(Line("Steuerpflichtig nach § 23 und § 22 Nr. 3 EStG", money(base)))
            E.append(Line(f"× Grenzsteuersatz {fmt_rate(_d(rate) / 100)} (Einkommensteuer)", money(base * r),
                          strong=True,
                          note="ohne Solidaritätszuschlag/Kirchensteuer, Progressionseffekte vereinfacht"))
        elif base > 0:
            E.append(Line("Steuerpflichtig nach § 23 und § 22 Nr. 3 EStG", money(base),
                          note="für eine Steuerschätzung Grenzsteuersatz in den Optionen angeben"))
        if k["activity"]:
            kinds = ("foreign", "domestic") if o.get("include_domestic") else ("foreign",)
            pot = self._pot(kinds, k, o, P)
            if pot["base"] or pot["share_carry"] or pot["other_carry"] or pot["wht"]:
                E.append(Line("Kapitalerträge (Abgeltungsteuer)" + (" inkl. Inland" if len(kinds) == 2 else
                                                                    " – nur Konten ohne Steuerabzug"),
                              None, kind="text", strong=True))
                share_net = pot["share"] - _d(o.get("loss_cf_kap_shares"))
                E.append(Line("Aktien-Topf (nach Verlustvortrag)", money(share_net),
                              indent=1, note="Verluste nur mit Aktiengewinnen verrechenbar"))
                E.append(Line("Sonstige Kapitalerträge", money(pot["other"]), indent=1))
                E.append(Line("Investmentfonds nach Teilfreistellung", money(pot["fund_taxable"]), indent=1,
                              note=f"vor Teilfreistellung {fmt_eur(pot['fund_gross'])}"))
                E.append(Line("Summe nach Verlustverrechnung", money(pot["base"]), indent=1))
                E.append(Line("abzgl. verbleibender Sparer-Pauschbetrag", money(-pot["pb_used"]), indent=1))
                E.append(Line("Bemessungsgrundlage", money(pot["taxable"]), indent=1, strong=True))
                E.append(Line("Abgeltungsteuer nach Anrechnung Quellensteuer", money(pot["est"]), indent=1,
                              note=f"anrechenbare Quellensteuer {fmt_eur(pot['wht'])}"))
                E.append(Line("Solidaritätszuschlag", money(pot["soli"]), indent=1))
                if pot["kist"]:
                    E.append(Line("Kirchensteuer", money(pot["kist"]), indent=1))
                if pot["share_carry"]:
                    E.append(Line("Verbleibender Verlust Aktien (Vortrag)", money(pot["share_carry"]), indent=1))
                if pot["other_carry"]:
                    E.append(Line("Verbleibender Verlust sonstige (Vortrag)", money(pot["other_carry"]), indent=1))
        if c["net"] < 0:
            E.append(Line("Verlust § 23 EStG (nur mit Gewinnen aus § 23 verrechenbar, Rück-/Vortrag)",
                          money(-c["net"])))

    def _assumptions(self, res: TaxResult, o: dict[str, Any], P: dict[str, Any]) -> None:
        opts = {s.key: s for s in self.option_specs()}

        def choice(key: str) -> str:
            spec = opts[key]
            return dict(spec.choices).get(str(o.get(key)), str(o.get(key)))

        A = res.assumptions
        A.append(f"Verbrauchsfolge Kryptowerte: {choice('scope')}.")
        A.append("Haltefrist: Veräußerung steuerfrei, wenn zwischen Anschaffung und Veräußerung mehr als "
                 f"{P['crypto']['holding_period_years']} Jahr liegt; Fristberechnung nach §§ 187, 188 BGB – steuerfrei "
                 "ab dem Tag nach dem Jahrestag der Anschaffung.")
        A.append("Veräußerungspreis = Gegenwert laut Import (value_eur); Werbungskosten = Gebühren des Vorgangs; "
                 "Anschaffungskosten inkl. Anschaffungsnebenkosten.")
        A.append(f"Gebühren in Kryptowerten beim Handel: {choice('crypto_fee')}. Transfergebühren: "
                 f"{choice('transfer_fee')}. Verlust/Diebstahl: {choice('lost')}.")
        A.append("Tausch Kryptowert gegen Kryptowert ist Veräußerung und Anschaffung zugleich (neue Haltefrist).")
        imap = {**INCOME_DEFAULTS, **(o.get("income_map") or {})}
        cats = dict(INCOME_CHOICES)
        A.append("Erträge: " + "; ".join(f"{TAG_LABEL.get(t, t)}: {cats.get(v, v)}" for t, v in sorted(imap.items()))
                 + ". Zugeflossene Einheiten gelten mit dem Wert bei Zufluss als angeschafft.")
        A.append("Schenkungen und Abgänge ohne Gegenbuchung sind keine Veräußerung; Zugänge ohne Gegenbuchung "
                 "werden mit Zugangsdatum und -wert als Anschaffung behandelt (Anschaffungsdaten Dritter sind "
                 "nicht bekannt).")
        A.append("Fehlende Anschaffungsdaten: konservativ steuerpflichtig mit Anschaffungskosten 0 €.")
        A.append("Wertpapiere: FIFO je Depot (§ 20 Abs. 4 S. 7 EStG). Dividenden: value_eur als Bruttobetrag, "
                 "wenn Quellensteuer separat gebucht ist (tag withholding_tax); anrechenbar höchstens "
                 f"{fmt_rate(P['capital']['wht_credit_cap'])} der Bruttodividende.")
        A.append("Vorabpauschale mit Börsenschlusskursen (erster/letzter Handelstag) statt Rücknahmepreisen; "
                 "Ausschüttungen je Anteil aus den gebuchten Ausschüttungen abgeleitet.")
        A.append("Nicht ermittelt: Fremdwährungsgewinne (§ 23 EStG), Altanteile an Investmentfonds vor 2018 "
                 "(fiktive Veräußerung 31.12.2017), gewerbliche Tätigkeit (z. B. Mining im größeren Umfang), "
                 "Termingeschäfte/Derivate, Günstigerprüfung.")
        A.append("Freigrenzen gelten je Person und für den Gesamtbetrag der jeweiligen Einkunftsart – andere private "
                 "Veräußerungsgeschäfte oder Leistungen außerhalb dieses Imports sind nicht enthalten.")

    def _quality(self, inp: TaxInput, res: TaxResult, year: int, c: dict[str, Any], i: dict[str, Any],
                 k: dict[str, Any], vp: _Vp, P: dict[str, Any]) -> None:
        issues = res.issues
        if not P.get("_meta", {}).get("reviewed", True):
            issues.append(Issue("warning", "params_unreviewed",
                           f"Steuerparameter für {year} sind nicht geprüft – Werte des Vorjahres fortgeschrieben. "
                           "Update oder /data/tax_rules/de.yaml prüfen."))
        if year >= inp.today.year:
            issues.append(Issue("info", "year_open",
                                f"Das Jahr {year} ist noch nicht abgeschlossen – vorläufige Werte."))
        if c["missing"]:
            issues.append(Issue("warning", "missing_basis",
                           f"{c['missing']} Veräußerungsteile ohne Anschaffungsdaten (Fehlbestand) – konservativ als "
                           "steuerpflichtig mit Anschaffungskosten 0 € angesetzt.", c["missing"]))
        if c["origins"].get("deposit"):
            n = c["origins"]["deposit"]
            issues.append(Issue("warning", "deposit_basis",
                           f"{n} Veräußerungsteile stammen aus Zugängen ohne Gegenbuchung – Anschaffungsdatum/-wert = "
                           "Zugang; bei Übertrag von eigener Wallet fehlen die ursprünglichen Daten.", n))
        for kind, n in sorted(c["excluded"].items()):
            issues.append(Issue("info", f"excluded_{kind}",
                           f"{n} × {KIND_LABEL.get(kind, kind)} nicht als Veräußerung berücksichtigt "
                           "(Einstellung/Regel).",
                           n))
        if i["valued"].get("fehlt"):
            n = i["valued"]["fehlt"]
            issues.append(Issue("warning", "income_no_value",
                                f"{n} Erträge ohne EUR-Wert und ohne Kurs – mit 0 € angesetzt.",
                           n))
        for how in ("Tageskurs", "EZB/Devisenkurs"):
            n = i["valued"].get(how, 0) + k["valued"].get(how, 0)
            if n:
                issues.append(Issue("info", f"income_valued_{how}",
                                    f"{n} Erträge ohne value_eur – mit {how} bewertet.", n))
        if i["mining"]:
            issues.append(Issue("info", "mining",
                                "Mining-Erträge: bei nachhaltiger Tätigkeit mit Gewinnerzielungsabsicht liegen "
                                "gewerbliche Einkünfte vor – hier als § 22 Nr. 3 angesetzt."))
        for aid in k["heuristic_assets"]:
            issues.append(Issue("warning", "fund_type_guess",
                           f"Fondsart für „{inp.asset(aid).name}“ automatisch angenommen "
                           f"({ASSET_TYPES.get(inp.asset_types.get(aid, ''), '?')}) – bitte unter Zuordnung prüfen."))
        if k["default_accounts"]:
            issues.append(Issue("info", "account_kind_default",
                           "Als Konto mit inländischem Steuerabzug angenommen: " + ", ".join(k["default_accounts"])
                           + " – bei ausländischen Brokern unter Zuordnung umstellen."))
        if k["foreign_div_without_wht"]:
            issues.append(Issue("info", "div_without_wht",
                           "Dividenden ausländischer Wertpapiere ohne gebuchte Quellensteuer: "
                           + ", ".join(k["foreign_div_without_wht"][:6])
                           + " – falls der Betrag netto ist, fehlen Bruttobetrag und anrechenbare Steuer."))
        if k["old_fund_lots"]:
            issues.append(Issue("warning", "fund_pre2018",
                           f"{k['old_fund_lots']} Fondsanteile mit Erwerb vor 2018 veräußert – Übergangsregeln "
                           "(fiktive Veräußerung 31.12.2017, Bestandsschutz) nicht berücksichtigt."))
        for (aid, y), why in sorted(vp.missing.items()):
            issues.append(Issue("warning", "vp_missing",
                           f"Vorabpauschale {inp.asset(aid).name} {y}: {why} – mit 0 € angesetzt."))
        year_tx = {d.tx_id for d in inp.ledger.disposals if d.date.year == year}
        n_led = sum(1 for li in inp.ledger.issues if li.severity == "warning" and li.tx_id in year_tx)
        if n_led:
            issues.append(Issue("warning", "ledger", f"{n_led} Ledger-Warnungen zu Veräußerungen dieses Jahres "
                                                "(siehe Datenqualität).", n_led))

    # -- Übersicht --------------------------------------------------------------------------------------
    def overview(self, inp: TaxInput, options: dict[str, Any]) -> Overview:
        o = {**self.defaults(), **(options or {})}
        ov = Overview(has_holding_period=True)
        today = inp.today
        horizon = add_years(today, 1)
        free_value = pend_value = pend_gain = pend_cost = ZERO
        next_rel: Release | None = None
        per_pos: dict[tuple[str, str], dict[str, Any]] = {}
        months: dict[str, dict[str, Any]] = {}
        unpriced: set[str] = set()
        for lot in inp.ledger.lots:
            a = inp.asset(lot.asset)
            if not a.is_crypto:
                continue
            price = inp.current_prices.get(a.asset_id)
            if price is None:
                unpriced.add(a.symbol)
            value = lot.qty * price if price is not None else ZERO
            end = self.holding_end(a, lot.acq_date) if lot.origin != "phantom" else None
            free = end is not None and end <= today
            pos = per_pos.setdefault((a.asset_id, lot.account), {
                "asset": a.symbol, "name": a.name, "asset_id": a.asset_id, "account": lot.account,
                "qty_free": ZERO, "qty_pending": ZERO, "value_free": ZERO, "value_pending": ZERO,
                "gain_pending": ZERO, "next": None, "priced": price is not None})
            if free:
                free_value += value
                pos["qty_free"] += lot.qty
                pos["value_free"] += value
            else:
                pend_value += value
                gain = value - lot.cost if price is not None else ZERO
                pend_gain += gain
                pend_cost += lot.cost
                pos["qty_pending"] += lot.qty
                pos["value_pending"] += value
                pos["gain_pending"] += gain
                if end is not None and (pos["next"] is None or end < pos["next"]):
                    pos["next"] = end
                if end is not None and end <= horizon:
                    rel = Release(end, a.asset_id, lot.account, lot.qty, value, gain)
                    ov.releases.append(rel)
                    mk = end.strftime("%Y-%m")
                    m = months.setdefault(mk, {"month": mk, "value": ZERO, "gain": ZERO, "count": 0})
                    m["value"] += value
                    m["gain"] += gain
                    m["count"] += 1
                    if next_rel is None or end < next_rel.date:
                        next_rel = rel
        ov.releases.sort(key=lambda r: (r.date, r.asset_id))
        ov.release_months = [months[k] for k in sorted(months)]
        ov.kpis = [
            Kpi("Steuerfrei veräußerbar", money(free_value), sub="Kryptowerte nach Ablauf der Haltefrist",
                hint="Aktueller Wert aller Lots, deren Haltefrist abgelaufen ist."),
            Kpi("In Haltefrist", money(pend_value), sub=f"Einstand {fmt_eur(pend_cost)}",
                hint="Aktueller Wert der Lots, deren Veräußerung heute steuerpflichtig wäre."),
            Kpi("Unrealisiert in Haltefrist", money(pend_gain), tone="up" if pend_gain > 0 else (
                "down" if pend_gain < 0 else ""), sub="bei Verkauf heute steuerpflichtig (Saldo)"),
            Kpi("Nächste Freigabe", next_rel.date if next_rel else None, kind="date",
                sub=(f"{inp.asset(next_rel.asset_id).symbol} · {fmt_eur(next_rel.value)}" if next_rel else
                     "keine in den nächsten 12 Monaten")),
        ]
        rows = sorted(per_pos.values(), key=lambda r: -(r["value_free"] + r["value_pending"]))
        ov.positions = Table("positions", "Haltefristen je Kryptowert und Wallet", [
            Column("asset", "Kryptowert", "text", 1.2), Column("account", "Wallet/Konto", "text", 1.4),
            Column("qty_free", "Menge steuerfrei", "qty", 1.2), Column("value_free", "Wert steuerfrei", "eur", 1.2),
            Column("qty_pending", "Menge in Frist", "qty", 1.2), Column("value_pending", "Wert in Frist", "eur", 1.2),
            Column("gain_pending", "G/V in Frist", "eur", 1.1), Column("next", "Nächste Freigabe", "date", 1.1),
        ], rows)
        # Jahresübersicht
        years = sorted({d.date.year for d in inp.ledger.disposals} | {e.date.year for e in inp.ledger.income}
                       | ({today.year} if inp.ledger.lots else set()))
        yrows = []
        for y in years:
            r = self.compute(inp, y, o)
            cd, idd, kd = r.data["crypto"], r.data["income"], r.data["capital"]
            bf, bd = kd["B"]["foreign"], kd["B"]["domestic"]
            share = sum((b["share_gain"] + b["share_loss"] for b in (bf, bd)), ZERO)
            other = sum((b["dividends"] + b["interest"] + b["other_gain"] + b["other_loss"]
                         + sum(b["fund_dist"].values(), ZERO) + sum(b["fund_vp"].values(), ZERO)
                         + sum(b["fund_gain"].values(), ZERO) for b in (bf, bd)), ZERO)
            yrows.append({"year": y, "c_net": money(cd["net"]), "c_gains": money(cd["gains"]),
                          "c_losses": money(cd["losses"]), "c_free": money(cd["free_gain"]),
                          "c_taxable": money(cd["taxable"]), "i_total": money(idd["total"]),
                          "i_taxable": money(idd["taxable"]), "k_share": money(share), "k_other": money(other),
                          "issues": len([x for x in r.issues if x.severity == "warning"])})
            ov.meters_by_year[y] = r.meters
        ov.years = Table("years", "Realisierte Ergebnisse je Jahr", [
            Column("year", "Jahr", "int", 0.6), Column("c_net", "Krypto ≤ 1 Jahr (Saldo)", "eur"),
            Column("c_free", "Krypto > 1 Jahr (steuerfrei)", "eur"),
            Column("c_taxable", "§ 23 steuer­pflichtig", "eur"),
            Column("i_total", "§ 22 Nr. 3 Einnahmen", "eur"), Column("i_taxable", "§ 22 Nr. 3 steuer­pflichtig", "eur"),
            Column("k_share", "Aktien-Topf", "eur"), Column("k_other", "Sonstige Kapital­erträge", "eur"),
        ], list(reversed(yrows)))
        ov.notes.append("Haltefristen und Freigaben nach FIFO " + ("je Wallet" if o.get("scope", "wallet") == "wallet"
                                                                  else "über alle Wallets")
                        + "; Werte zu aktuellen Kursen.")
        if unpriced:
            ov.issues.append(Issue("warning", "unpriced", "Ohne aktuellen Kurs (Wert 0 €): "
                                   + ", ".join(sorted(unpriced)[:10]) + (" …" if len(unpriced) > 10 else "")))
        return ov

    # -- Dokumente ------------------------------------------------------------------------------------
    def build_document(self, doc_id: str, result: TaxResult, meta: ReportMeta) -> D.Doc:
        return build_document(self, doc_id, result, meta)


def build_sections(pack: GermanyPack, result: TaxResult, c: dict[str, Any], i: dict[str, Any],
                   k: dict[str, Any]) -> None:
    """Tabellen der Aufstellungen (für Web-Ansicht und PDF)."""
    cols23 = [
        Column("nr", "Nr.", "int", 0.45), Column("asset", "Kryptowert", "text", 0.9),
        Column("account", "Wallet/Konto", "text", 1.2), Column("kind", "Vorgang", "text", 0.8),
        Column("acq", "Anschaffung", "date", 0.85), Column("disp", "Veräußerung", "date", 0.85),
        Column("days", "Tage", "int", 0.5), Column("qty", "Menge", "qty", 1.1),
        Column("price", "Veräußerungspreis", "eur", 1.05, True),
        Column("cost", "Anschaffungskosten", "eur", 1.05, True),
        Column("wk", "Werbungskosten", "eur", 0.85, True), Column("gain", "Gewinn/Verlust", "eur", 1.0, True),
    ]
    rows_tax = [dict(r, nr=n) for n, r in enumerate(sorted(c["rows_tax"], key=lambda r: (r["disp"], r["asset"])), 1)]
    rows_free = [dict(r, nr=n) for n, r in enumerate(sorted(c["rows_free"], key=lambda r: (r["disp"], r["asset"])), 1)]
    per_asset: dict[str, dict[str, Any]] = {}
    for r in rows_tax:
        pa = per_asset.setdefault(r["asset"], {"asset": r["asset"], "name": r["name"], "count": 0, "qty": ZERO,
                                               "price": ZERO, "cost": ZERO, "wk": ZERO, "gain": ZERO})
        pa["count"] += 1
        for key in ("qty", "price", "cost", "wk", "gain"):
            pa[key] += r[key]
    sec = Section("crypto23", "Private Veräußerungsgeschäfte mit Kryptowerten (§ 23 EStG)")
    sec.tables.append(Table("c23_assets", "Zusammenfassung je Kryptowert (steuerpflichtig)", [
        Column("asset", "Kryptowert", "text", 0.9), Column("name", "Bezeichnung", "text", 1.6),
        Column("count", "Vorgänge", "int", 0.7), Column("qty", "Menge", "qty", 1.2),
        Column("price", "Veräußerungspreis", "eur", 1.1, True), Column("cost", "Anschaffungskosten", "eur", 1.1, True),
        Column("wk", "Werbungskosten", "eur", 1.0, True), Column("gain", "Gewinn/Verlust", "eur", 1.1, True),
    ], sorted(per_asset.values(), key=lambda r: r["asset"])).with_totals())
    sec.tables.append(Table("c23_taxable", "Steuerpflichtige Veräußerungen (Haltedauer bis 1 Jahr)", cols23, rows_tax,
                            note="Je Zeile ein veräußerter Anschaffungsposten (Lot) nach FIFO. Beträge in EUR.",
                            landscape=True).with_totals("asset"))
    sec.tables.append(Table("c23_free", "Nachrichtlich: steuerfreie Veräußerungen (Haltedauer über 1 Jahr)", cols23,
                            rows_free, landscape=True).with_totals("asset"))
    result.sections.append(sec)
    s22 = Section("income22", "Leistungen nach § 22 Nr. 3 EStG")
    rows22 = [dict(r, nr=n) for n, r in enumerate(sorted(i["rows"], key=lambda r: (r["date"], r["asset"])), 1)]
    s22.tables.append(Table("i22", "Zuflüsse (Wert bei Zufluss)", [
        Column("nr", "Nr.", "int", 0.45), Column("date", "Datum", "date", 0.85),
        Column("asset", "Kryptowert", "text", 0.9), Column("account", "Wallet/Konto", "text", 1.3),
        Column("tag", "Art", "text", 0.9), Column("qty", "Menge", "qty", 1.2),
        Column("value", "Wert (EUR)", "eur", 1.0, True), Column("valued", "Bewertung", "text", 0.9),
    ], rows22, landscape=True).with_totals("asset"))
    by_tag = [{"tag": t, "value": v} for t, v in sorted(i["by_tag"].items())]
    s22.tables.append(Table("i22_tags", "Summe je Art",
                            [Column("tag", "Art", "text", 2), Column("value", "Wert", "eur", 1, True)],
                            by_tag).with_totals())
    result.sections.append(s22)
    sk = Section("kap", "Kapitalerträge (Wertpapiere, Investmentfonds)")
    sk.tables.append(Table("k_sales", "Veräußerungen von Wertpapieren", [
        Column("account", "Depot", "text", 1.1), Column("kind", "Steuerabzug", "text", 0.8),
        Column("asset", "Wertpapier", "text", 1.8), Column("isin", "ISIN/WKN", "text", 1.0),
        Column("pot", "Topf", "text", 0.7), Column("acq", "Anschaffung", "date", 0.85),
        Column("disp", "Veräußerung", "date", 0.85), Column("qty", "Stück", "qty", 0.8),
        Column("price", "Erlös", "eur", 1.0, True), Column("cost", "Anschaffungskosten", "eur", 1.0, True),
        Column("wk", "Veräußerungskosten", "eur", 0.8, True), Column("vp", "Abzug VP", "eur", 0.8, True),
        Column("gain", "Gewinn/Verlust", "eur", 1.0, True),
    ], sorted(k["rows_sales"], key=lambda r: (r["account"], r["disp"])), landscape=True,
        note="Gewinn/Verlust = Erlös − Anschaffungskosten − Veräußerungskosten; bei Fonds zusätzlich abzüglich der "
             "während der Besitzzeit angesetzten Vorabpauschalen (VP), vor Teilfreistellung.").with_totals("asset"))
    sk.tables.append(Table("k_income", "Dividenden, Ausschüttungen, Zinsen", [
        Column("date", "Datum", "date", 0.8), Column("account", "Depot/Konto", "text", 1.1),
        Column("kind", "Steuerabzug", "text", 0.8), Column("asset", "Wertpapier", "text", 1.9),
        Column("type", "Art", "text", 0.9), Column("gross", "Brutto", "eur", 0.9, True),
        Column("wht", "Quellensteuer", "eur", 0.9, True), Column("credit", "anrechenbar", "eur", 0.9, True),
    ], sorted(k["rows_income"], key=lambda r: (r["date"], r["asset"])), landscape=True).with_totals("asset"))
    sk.tables.append(Table("k_vp", f"Vorabpauschalen für {result.year - 1} (zugeflossen Anfang {result.year})", [
        Column("account", "Depot", "text", 1.1), Column("kind", "Steuerabzug", "text", 0.8),
        Column("asset", "Fonds", "text", 1.9), Column("type", "Fondsart", "text", 1.4),
        Column("acq", "Anschaffung", "date", 0.8), Column("qty", "Anteile", "qty", 0.8),
        Column("unit", "VP je Anteil", "qty", 0.8), Column("factor", "Anteil Jahr", "qty", 0.7),
        Column("amount", "Vorabpauschale", "eur", 0.9, True),
    ], k["rows_vp"], landscape=True).with_totals("asset"))
    if i["kap_rows"]:
        sk.tables.append(Table("k_crypto", "Erträge aus Kryptowerten, als Kapitalerträge eingestuft", [
            Column("date", "Datum", "date", 0.8), Column("asset", "Kryptowert", "text", 0.9),
            Column("account", "Wallet/Konto", "text", 1.3), Column("tag", "Art", "text", 0.9),
            Column("value", "Wert", "eur", 1.0, True)], i["kap_rows"]).with_totals("asset"))
    result.sections.append(sk)


def build_document(pack: GermanyPack, doc_id: str, result: TaxResult, meta: ReportMeta) -> D.Doc:
    from app.tax.packs.de.documents import build

    return build(pack, doc_id, result, meta)


PACK = GermanyPack
