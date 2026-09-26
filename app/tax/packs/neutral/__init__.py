"""Neutrales Regelwerk: realisierte Gewinne/Verluste und Erträge je Jahr ohne länderspezifische Regeln.

Dient als Vorlage für weitere Länder und als Aufstellung für Nutzer außerhalb Deutschlands.
Parameter: nur Metadaten (``params.yaml``); Verbrauchsfolge und Umfang sind Optionen.
"""

from __future__ import annotations

import dataclasses
from collections import defaultdict
from decimal import Decimal
from typing import Any

from app.ledger.engine import REALIZED_KINDS, EngineOptions
from app.tax import document as D
from app.tax.base import (
    ZERO,
    Column,
    DocumentSpec,
    Issue,
    Kpi,
    Line,
    OptionSpec,
    Overview,
    ReportMeta,
    RulePack,
    Section,
    Table,
    TaxInput,
    TaxResult,
    money,
)

KIND_LABEL = {"sell": "Verkauf", "trade": "Tausch", "fee": "Gebühr", "transfer_fee": "Transfergebühr",
              "spend": "Bezahlung", "lost": "Verlust"}


class NeutralPack(RulePack):
    id = "neutral"
    name = "Neutral (ohne Steuerregeln)"
    country = None
    code_version = "1"
    description = "Realisierte Gewinne/Verluste und Erträge je Kalenderjahr – ohne Fristen, Freigrenzen oder Formulare."

    def option_specs(self) -> list[OptionSpec]:
        return [
            OptionSpec("scope", "Lot-Zuordnung", "choice", "global",
                       (("global", "über alle Konten"), ("account", "je Konto")), group="Berechnung"),
            OptionSpec("method", "Verbrauchsfolge", "choice", "fifo",
                       (("fifo", "FIFO"), ("lifo", "LIFO"), ("hifo", "HIFO (höchster Einstand zuerst)")),
                       group="Berechnung"),
        ]

    def engine_options(self, base: EngineOptions, options: dict[str, Any], snapshot_years: list[int]) -> EngineOptions:
        scope = options.get("scope", "global")
        method = options.get("method", "fifo")
        return dataclasses.replace(base, scope=scope if scope in ("global", "account") else "global",
                                   method=method if method in ("fifo", "lifo", "hifo") else "fifo")

    def documents(self) -> list[DocumentSpec]:
        return [DocumentSpec("report", "Aufstellung realisierte Ergebnisse",
                             "Veräußerungen und Erträge des Jahres je Asset")]

    def compute(self, inp: TaxInput, year: int, options: dict[str, Any]) -> TaxResult:
        o = {**self.defaults(), **(options or {})}
        res = TaxResult(pack_id=self.id, pack_name=self.name, pack_version=self.version,
                        params_version=self.params.version, year=year, params={}, options=o)
        rows, inc = [], []
        for d in inp.ledger.disposals:
            if d.date.year != year or d.kind not in REALIZED_KINDS:
                continue
            a = inp.asset(d.asset)
            for p in d.parts:
                rows.append({"asset": a.symbol, "name": a.name, "account": d.account,
                             "kind": KIND_LABEL.get(d.kind, d.kind), "acq": p.acq_date, "disp": d.date,
                             "days": (d.date - p.acq_date).days if p.acq_date else None, "qty": p.qty,
                             "proceeds": money(p.proceeds), "cost": money(p.cost), "gain": money(p.proceeds - p.cost),
                             "segment": a.segment})
        for e in inp.ledger.income:
            if e.date.year != year:
                continue
            a = inp.asset(e.related_asset or e.asset)
            inc.append({"date": e.date, "asset": a.symbol, "account": e.account, "tag": e.tag, "qty": e.qty,
                        "value": money(e.value_eur), "segment": a.segment})
        by_seg: dict[str, dict[str, Decimal]] = defaultdict(lambda: {"gain": ZERO, "income": ZERO})
        for r in rows:
            by_seg[r["segment"]]["gain"] += r["gain"]
        for r in inc:
            by_seg[r["segment"]]["income"] += r["value"]
        for seg, v in sorted(by_seg.items()):
            res.summary.append(Line(f"{seg}: realisierter Gewinn/Verlust", money(v["gain"])))
            res.summary.append(Line(f"{seg}: Erträge", money(v["income"])))
        total = sum((v["gain"] + v["income"] for v in by_seg.values()), ZERO)
        res.summary.append(Line("Gesamt", money(total), strong=True))
        sec = Section("realized", "Realisierte Ergebnisse")
        sec.tables.append(Table("n_disposals", "Veräußerungen", [
            Column("asset", "Asset", "text", 0.9), Column("account", "Konto", "text", 1.2),
            Column("kind", "Vorgang", "text", 0.8), Column("acq", "Anschaffung", "date", 0.85),
            Column("disp", "Veräußerung", "date", 0.85), Column("days", "Tage", "int", 0.5),
            Column("qty", "Menge", "qty", 1.1), Column("proceeds", "Erlös", "eur", 1.0, True),
            Column("cost", "Einstand", "eur", 1.0, True), Column("gain", "Gewinn/Verlust", "eur", 1.0, True),
        ], sorted(rows, key=lambda r: (r["disp"], r["asset"])), landscape=True).with_totals("asset"))
        sec.tables.append(Table("n_income", "Erträge", [
            Column("date", "Datum", "date", 0.85), Column("asset", "Asset", "text", 0.9),
            Column("account", "Konto", "text", 1.2), Column("tag", "Art", "text", 0.8),
            Column("qty", "Menge", "qty", 1.1), Column("value", "Wert (EUR)", "eur", 1.0, True),
        ], sorted(inc, key=lambda r: (r["date"], r["asset"])), landscape=True).with_totals("asset"))
        res.sections.append(sec)
        res.has_activity = bool(rows or inc)
        res.assumptions = [f"Verbrauchsfolge {o['method'].upper()} "
                           + ("über alle Konten." if o["scope"] == "global" else "je Konto."),
                           "Werte in EUR laut Import; keine steuerlichen Fristen, Freigrenzen oder Formulare."]
        if year >= inp.today.year:
            res.issues.append(Issue("info", "year_open", f"Das Jahr {year} ist noch nicht abgeschlossen."))
        res.data = {"total": total}
        return res

    def overview(self, inp: TaxInput, options: dict[str, Any]) -> Overview:
        ov = Overview(has_holding_period=False)
        years = sorted({d.date.year for d in inp.ledger.disposals} | {e.date.year for e in inp.ledger.income})
        rows = []
        for y in years:
            r = self.compute(inp, y, options)
            rows.append({"year": y, "total": r.data["total"]})
        ov.years = Table("years", "Realisierte Ergebnisse je Jahr", [
            Column("year", "Jahr", "int", 0.6), Column("total", "Gewinn/Verlust inkl. Erträge", "eur")],
            list(reversed(rows)))
        ov.kpis = [Kpi("Jahre mit Vorgängen", len(years), kind="int")]
        return ov

    def build_document(self, doc_id: str, result: TaxResult, meta: ReportMeta) -> D.Doc:
        title = f"Aufstellung {result.year}"
        doc = D.Doc(title=title, subtitle="realisierte Ergebnisse", filename=f"Aufstellung_{result.year}.pdf",
                    footer=f"Portfolia · {title} · Regelwerk neutral · erstellt "
                           f"{meta.created_at.strftime('%d.%m.%Y %H:%M')}")
        doc.add(D.H1(title), D.P("Realisierte Gewinne/Verluste und Erträge ohne länderspezifische Steuerregeln.",
                                 "small"), D.Lines(result.summary), D.PageBreak("landscape"))
        for s in result.sections:
            for t in s.tables:
                doc.add(D.TableBlock(t))
        doc.add(D.PageBreak("portrait"), D.H2("Annahmen"), D.Bullets(result.assumptions))
        return doc


PACK = NeutralPack
