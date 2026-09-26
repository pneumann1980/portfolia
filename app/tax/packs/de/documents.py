"""PDF-Dokumente des Regelwerks Deutschland (Aufbau; gerendert von :mod:`app.tax.pdf`)."""

from __future__ import annotations

from typing import Any

from app.tax import document as D
from app.tax.base import Line, ReportMeta, Table, TaxResult, fmt_date, fmt_eur, fmt_rate, money

DISCLAIMER = ("Informative Aufstellung – keine Steuerberatung. Die Werte beruhen auf den importierten Daten und den "
              "dokumentierten Annahmen. Maßgeblich sind die amtlichen Vordrucke bzw. ELSTER sowie die "
              "Steuerbescheinigungen der Banken. Bitte vor der Abgabe prüfen.")


def _table(result: TaxResult, tid: str) -> Table | None:
    for s in result.sections:
        for t in s.tables:
            if t.id == tid:
                return t
    return None


def _person(meta: ReportMeta, year: int) -> list[tuple[str, str]]:
    p = meta.profile or {}
    rows = [("Steuerpflichtige Person", p.get("name") or "–")]
    if p.get("tax_id"):
        rows.append(("Steuer-Identifikationsnummer", p["tax_id"]))
    if p.get("tax_number"):
        rows.append(("Steuernummer", p["tax_number"]))
    rows.append(("Veranlagungsjahr", str(year)))
    return rows


def _data_rows(result: TaxResult, meta: ReportMeta) -> list[tuple[str, str]]:
    src = meta.import_file or "–"
    if meta.import_sha:
        src += f" (SHA-256 {meta.import_sha[:12]}…)"
    return [
        ("Regelwerk", f"{result.pack_name} – Version {result.pack_version}"),
        ("Datenstand", f"Import #{meta.import_id or '–'}: {src}"),
        ("Bewertungsstichtag Import", fmt_date(meta.valuation_date)),
        ("Erstellt", f"{meta.created_at.strftime('%d.%m.%Y %H:%M')} mit Portfolia {meta.app_version}"),
    ]


def _footer(title: str, result: TaxResult, meta: ReportMeta) -> str:
    return (f"Portfolia · {title} · Regelwerk {result.pack_id} {result.pack_version} · erstellt "
            f"{meta.created_at.strftime('%d.%m.%Y %H:%M')} · keine Steuerberatung")


def _params_rows(result: TaxResult) -> list[tuple[str, str]]:
    P = result.params
    c, k = P.get("crypto", {}), P.get("capital", {})
    tf = k.get("teilfreistellung", {})
    rows = [
        ("Haltefrist Kryptowerte", f"{c.get('holding_period_years', '–')} Jahr"),
        ("Freigrenze § 23 EStG", fmt_eur(c.get("freigrenze_23"))),
        ("Freigrenze § 22 Nr. 3 EStG", fmt_eur(c.get("freigrenze_22_3"))),
        ("Sparer-Pauschbetrag", f"{fmt_eur(k.get('sparer_pauschbetrag_single'))} / "
                                f"{fmt_eur(k.get('sparer_pauschbetrag_joint'))} (Zusammenveranlagung)"),
        ("Abgeltungsteuer / Solidaritätszuschlag", f"{fmt_rate(k.get('flat_rate'))} / {fmt_rate(k.get('soli_rate'))}"),
        ("Teilfreistellung", ", ".join(f"{name} {fmt_rate(tf.get(key, 0))}" for key, name in (
            ("etf_equity", "Aktienfonds"), ("etf_mixed", "Mischfonds"), ("fund_realestate", "Immobilienfonds"),
            ("fund_realestate_foreign", "Auslands-Immobilienfonds"), ("etf_other", "sonstige")))),
    ]
    bz = P.get("basiszins")
    rows.append((f"Basiszins {result.year}", fmt_rate(bz) if bz is not None else "nicht hinterlegt"))
    prev = result.params.get("basiszins_prev")
    if prev is not None:
        rows.append((f"Basiszins {result.year - 1} (Vorabpauschale)", fmt_rate(prev)))
    return rows


def _issues(result: TaxResult) -> list[str]:
    order = {"critical": 0, "warning": 1, "info": 2}
    label = {"critical": "Kritisch", "warning": "Warnung", "info": "Hinweis"}
    return [f"{label.get(i.severity, i.severity)}: {i.text}"
            for i in sorted(result.issues, key=lambda i: order.get(i.severity, 3))]


def _crypto_lines(result: TaxResult) -> list[Line]:
    c = result.data["crypto"]
    return [
        Line("Veräußerungspreis (Summe)", money(c["price"])),
        Line("Anschaffungskosten (Summe)", money(c["cost"])),
        Line("Werbungskosten (Summe)", money(c["wk"])),
        Line("Gewinn/Verlust (Saldo)", money(c["net"]), strong=True),
    ]


def build(pack: Any, doc_id: str, result: TaxResult, meta: ReportMeta) -> D.Doc:
    y = result.year
    if doc_id == "anlage_so":
        return _anlage_so(pack, result, meta)
    if doc_id == "anlage_kap":
        return _anlage_kap(pack, result, meta)
    title = f"Steuerreport {y}"
    doc = D.Doc(title=title, subtitle="Kryptowerte und Wertpapiere", filename=f"Steuerreport_{y}.pdf",
                subject=f"Steuerreport {y} (informativ)", keywords="Steuer, Anlage SO, Anlage KAP, Kryptowerte",
                footer=_footer(title, result, meta))
    doc.add(D.H1(title), D.P("Aufstellung der steuerlich relevanten Vorgänge aus Kryptowerten und Wertpapieren mit "
                             "Übertragungshilfe für die Einkommensteuererklärung.", "normal"),
            D.KV(_person(meta, y) + _data_rows(result, meta)), D.P(DISCLAIMER, "warn"),
            D.H2("Zusammenfassung"), D.Lines(result.summary), D.H2("Frei- und Pauschbeträge"),
            D.Meters(result.meters))
    if result.estimate:
        doc.add(D.H2("Schätzung der Steuer (vereinfacht)"), D.Lines(result.estimate))
    doc.add(D.H2("Hinweise zur Abgabe"), D.Bullets([
        "Beträge in ELSTER bzw. in die amtlichen Vordrucke übertragen (siehe Übertragungshilfe). Zeilennummern ändern "
        "sich jährlich – maßgeblich ist die Feldbezeichnung.",
        "Die Aufstellungen dieses Berichts (oder die separaten Aufstellungen zu Anlage SO/KAP) als Beleg beifügen "
        "bzw. auf Anforderung nachreichen (ELSTER: Belegnachreichung).",
        "Bei Depots mit inländischem Steuerabzug sind die Steuerbescheinigungen der Banken maßgeblich; die Werte "
        "hier dienen der Kontrolle.",
    ]))
    doc.add(D.PageBreak(), D.H1("Übertragungshilfe"),
            D.P("Summen für die amtlichen Formulare. Eine Zeilenangabe erscheint nur, wenn sie in den "
                "Steuerparametern hinterlegt und geprüft ist.", "small"))
    if result.fields:
        doc.add(D.Fields(result.fields))
    else:
        doc.add(D.P("Für dieses Jahr ergeben sich aus den Daten keine Eintragungen.", "normal"))
    for tid in ("c23_assets",):
        t = _table(result, tid)
        if t is not None and t.rows:
            doc.add(D.TableBlock(t))
    landscape_tables = ["c23_taxable", "c23_free", "i22", "i22_tags", "k_sales", "k_income", "k_vp", "k_crypto"]
    tables = [t for tid in landscape_tables if (t := _table(result, tid)) is not None and t.rows]
    if tables:
        doc.add(D.PageBreak("landscape"), D.H1("Aufstellungen"))
        for t in tables:
            doc.add(D.TableBlock(t))
    doc.add(D.PageBreak("portrait"), D.H1("Hinweise und Datenqualität"))
    iss = _issues(result)
    doc.add(D.Bullets(iss) if iss else D.P("Keine Auffälligkeiten.", "normal"))
    doc.add(D.Spacer(6), D.H1("Methodik und Annahmen"), D.Bullets(result.assumptions), D.H2("Verwendete Parameter"),
            D.KV(_params_rows(result)), D.H2("Rechtsgrundlagen"),
            D.Bullets(list(pack.params.meta.get("sources", [])), "small"), D.Spacer(4), D.P(DISCLAIMER, "note"))
    return doc


def _anlage_so(pack: Any, result: TaxResult, meta: ReportMeta) -> D.Doc:
    y = result.year
    title = f"Aufstellung zur Anlage SO {y}"
    doc = D.Doc(title=title, subtitle="Kryptowerte", filename=f"Anlage_SO_Aufstellung_{y}.pdf",
                subject=f"Aufstellung private Veräußerungsgeschäfte und Leistungen {y}",
                keywords="Anlage SO, § 23 EStG, § 22 Nr. 3 EStG, Kryptowerte", footer=_footer(title, result, meta))
    c, i = result.data["crypto"], result.data["income"]
    doc.add(D.H1(title), D.KV(_person(meta, y)),
            D.P("Private Veräußerungsgeschäfte mit Kryptowerten (§ 23 Abs. 1 S. 1 Nr. 2 EStG) und Einkünfte aus "
                "Leistungen (§ 22 Nr. 3 EStG). Verbrauchsfolge und Bewertung siehe Methodik am Ende.", "small"),
            D.H2("Private Veräußerungsgeschäfte – Summen (Haltedauer bis 1 Jahr)"), D.Lines(_crypto_lines(result)))
    t = _table(result, "c23_assets")
    if t is not None and t.rows:
        doc.add(D.TableBlock(t))
    doc.add(D.H2("Einkünfte aus Leistungen (§ 22 Nr. 3 EStG) – Summen"), D.Lines([
        Line("Einnahmen", money(i["total"])), Line("Werbungskosten", money(i["wk"])),
        Line("Einkünfte", money(i["einkuenfte"]), strong=True)]))
    t = _table(result, "i22_tags")
    if t is not None and t.rows:
        doc.add(D.TableBlock(t))
    detail = [t for tid in ("c23_taxable", "i22") if (t := _table(result, tid)) is not None and t.rows]
    if detail:
        doc.add(D.PageBreak("landscape"))
        for t in detail:
            doc.add(D.TableBlock(t))
    doc.add(D.PageBreak("portrait"), D.H2("Methodik"), D.Bullets(result.assumptions[:8], "small"),
            D.P(f"Nachrichtlich: {c['count_free']} steuerfreie Veräußerungen nach Ablauf der Haltefrist mit "
                f"Gewinn/Verlust {fmt_eur(c['free_gain'])} sind nicht enthalten.", "small"),
            D.P(DISCLAIMER, "note"))
    return doc


def _anlage_kap(pack: Any, result: TaxResult, meta: ReportMeta) -> D.Doc:
    y = result.year
    title = f"Aufstellung zur Anlage KAP / KAP-INV {y}"
    doc = D.Doc(title=title, subtitle="Wertpapiere", filename=f"Anlage_KAP_Aufstellung_{y}.pdf",
                subject=f"Aufstellung Kapitalerträge {y}", keywords="Anlage KAP, KAP-INV, Kapitalerträge",
                footer=_footer(title, result, meta))
    doc.add(D.H1(title), D.KV(_person(meta, y)),
            D.P("Kapitalerträge aus Wertpapieren und Investmentfonds je Depot. Für Depots mit inländischem "
                "Steuerabzug sind die Steuerbescheinigungen maßgeblich (Werte hier zur Kontrolle).", "small"))
    kap_fields = [f for f in result.fields if f.form.startswith("Anlage KAP")]
    if kap_fields:
        doc.add(D.H2("Übertragungshilfe"), D.Fields(kap_fields))
    tables = [t for tid in ("k_sales", "k_income", "k_vp") if (t := _table(result, tid)) is not None and t.rows]
    if tables:
        doc.add(D.PageBreak("landscape"))
        for t in tables:
            doc.add(D.TableBlock(t))
    doc.add(D.PageBreak("portrait"), D.H2("Methodik"),
            D.Bullets([a for a in result.assumptions if "Wertpapiere" in a or "Vorabpauschale" in a
                       or "Nicht ermittelt" in a], "small"),
            D.P(f"Erstellt am {fmt_date(meta.created_at)}.", "small"), D.P(DISCLAIMER, "note"))
    return doc
