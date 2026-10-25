"""Steuerbescheinigungen der Bank: Parser (synthetische Belege im DKB-Layout), Upload, Abgleich im Steuerreport."""

import io
from datetime import date
from decimal import Decimal

from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas

from app.taxcert import parse as P
from app.taxcert.service import taxcert_service
from tests.helpers import tx
from tests.test_tax_de import buy, run, sell
from tests.test_tax_web import client  # noqa: F401  (Fixture)

D = Decimal

BESCH = [
    "Herrn", "Max Mustermann", "Musterstr. 1", "12345 Musterstadt",
    "(Bezeichnung der auszahlenden Stelle/des Schuldners der Kapitalerträge)", "Beispielbank AG", "10000 Berlin",
    "Steuerbescheinigung", "x Bescheinigung für alle Privatkonten und/oder -depots",
    "werden für das Kalenderjahr 2025 folgende Angaben bescheinigt:", "Betrag EUR",
    "Höhe der Kapitalerträge", "Zeile 7 Anlage KAP", "5.910,00",
    "davon: Gewinn aus Aktienveräußerungen im Sinne des § 20 Abs. 2 Satz 1 Nr. 1 EStG", "Zeile 8 Anlage KAP",
    "5.900,00", "Höhe des in Anspruch genommenen Sparer-Pauschbetrages", "Zeile 16 oder 17 Anlage KAP", "2.000,00",
    "Kapitalertragsteuer", "Zeile 37 Anlage KAP", "976,00", "Solidaritätszuschlag", "Zeile 38 Anlage KAP", "53,68",
    "Summe der angerechneten ausländischen Steuer", "Zeile 40 Anlage KAP", "1,50", "nur nachrichtlich:",
    "Gewinn aus Aktienveräußerungen im Sinne des § 20 Abs. 2 Satz 1 Nr. 1 EStG vor Verrechnung mit sonstigen",
    "Verlusten im Sinne des § 20 Abs. 2 EStG", "5.900,00",
    "Diese Bescheinigung wird nicht unterschrieben, da maschinell erstellt (per 31.01. des Folgejahres).",
]
ERTRAG = [
    "Beispielbank AG", "Erläuterungen zur Steuerbescheinigung für das Kalenderjahr 2025 10000 Berlin",
    "Max Mustermann, Musterstr. 1, 12345 Musterstadt 99999999", "I II III IV V VI VII VIII", "(Depot-)Konto WKN/ISIN",
    "Geschäftsvorfall", "Datum", "31.12.2017", "anrechenbare", "ausländische", "Quellensteuer", "Fonds1",
    "Aktientopf aus Vorjahr", "eingebucht", "03.02.2025 -100,00",
    "123456789 US0000000001", "Verkäufe Wertpapier", "ALPHA INC.", "10.02.2025 6.500,00",
    "123456789 US0000000002", "Verkäufe Wertpapier", "BETA CORP.", "05.06.2025 -500,00",
    "123456789 US9999999999", "Verkäufe Wertpapier", "GAMMA PLC", "01.07.2025 30,00",
    "123456789 US0000000001", "Erträge", "ALPHA INC.", "18.03.2025 10,00 1,50",
    "1Bei Fonds ggf. nach Abzug", "Seite 1 von 2",
    "Summen 10,00 0,00 6.530,00 -600,00 0,00 0,00 1,50 0,00", "Gesamtsumme Spalte I – VI 5.940,00",
    "Höhe der Kapitalerträge (Zwischensummen III der Spalten I, III, V und VI)", "5.940,00",
    "Nachrichtliche Hinweise:",
    "in Anspruch genommener Sparer-Pauschbetrag 2.000,00 einbehaltene Kapitalertragsteuer 985,50",
    "ins neue Jahr vorgetragener Verlusttopf \"sonstige Verluste\" 0,00 falls kein Antrag auf Verlust- einbeh.",
    "ins neue Jahr vorgetragener Verlusttopf \"Aktien\" 0,00 bescheinigung gestellt",
    "einbehaltener Solidaritätszuschlag 54,20", "Seite 2 von 2",
]


def _pdf(lines, landscape_page=False):
    buf = io.BytesIO()
    size = landscape(A4) if landscape_page else A4
    c = canvas.Canvas(buf, pagesize=size)
    y = size[1] - 40
    for ln in lines:
        if y < 40:
            c.showPage()
            y = size[1] - 40
        c.setFont("Helvetica", 8)
        c.drawString(30, y, ln)
        y -= 11
    c.save()
    return buf.getvalue()


def test_classify_and_parse_bescheinigung():
    assert P.classify([BESCH]) == P.KIND_BESCHEINIGUNG and P.classify([ERTRAG]) == P.KIND_ERTRAEGNIS
    assert P.classify([["Irgendein Brief", "ohne Bezug"]]) is None
    b = P.parse_bescheinigung([BESCH])
    assert b.ok and b.year == 2025 and b.issuer == "Beispielbank AG"
    assert b.values == {"capital_total": D("5910.00"), "share_gains": D("5900.00"), "pauschbetrag": D("2000.00"),
                        "kest": D("976.00"), "soli": D("53.68"), "wht_credited": D("1.50"),
                        "share_gains_gross": D("5900.00")}
    assert not b.notes and not b.extra_lines  # Rechenprobe: (5.910 − 2.000 − 4 × 1,50) / 4 = 976,00
    assert "Mustermann" not in repr(b)  # keine Personendaten


def test_bescheinigung_probe_and_extra_lines():
    wrong = [ln.replace("976,00", "900,00") for ln in BESCH]
    assert any("Rechenprobe" in n for n in P.parse_bescheinigung([wrong]).notes)
    extra = [*BESCH, "Verlust ohne Aktien", "Zeile 12 Anlage KAP", "10,00"]
    assert P.parse_bescheinigung([extra]).extra_lines == ["12"]
    broken = P.parse_bescheinigung([["werden für das Kalenderjahr 2025 folgende Angaben bescheinigt:"]])
    assert not broken.ok and broken.notes


def test_parse_ertraegnis_totals_pots_and_events():
    e = P.parse_ertraegnis([ERTRAG])
    assert e.ok and e.year == 2025 and e.column_probe is True and not e.notes
    assert e.totals["share_gains"] == D("6530.00") and e.totals["share_losses"] == D("-600.00")
    assert e.totals["wht"] == D("1.50") and e.capital_total == D("5940.00")
    assert e.carry_in() == {"shares": D("100.00"), "other": D("0")} and e.carry_out == {"other": D("0.00"),
                                                                                        "shares": D("0.00")}
    assert e.hints["pauschbetrag"] == D("2000.00") and e.hints["kest"] == D("985.50")
    sales = [x for x in e.events if x.kind.startswith("Verkäufe")]
    assert [(x.date, x.isin, x.amounts) for x in sales] == [
        (date(2025, 2, 10), "US0000000001", [D("6500.00")]), (date(2025, 6, 5), "US0000000002", [D("-500.00")]),
        (date(2025, 7, 1), "US9999999999", [D("30.00")])]
    assert any(x.pot for x in e.events) and e.events[-1].amounts == [D("10.00"), D("1.50")]


def test_ertraegnis_column_probe_flags_mismatch():
    bad = [ln.replace("Summen 10,00 0,00 6.530,00", "Summen 10,00 0,00 6.000,00") for ln in ERTRAG]
    e = P.parse_ertraegnis([bad])
    assert e.column_probe is False and any("Summenzeile" in n for n in e.notes)


def _upload(client, *files):  # noqa: F811
    return client.post("/tax/certs/upload", files=[("file", f) for f in files])


def test_upload_pages_replace_and_delete(client):  # noqa: F811
    ctx = client.app.state.ctx
    b, e = _pdf(BESCH), _pdf(ERTRAG, landscape_page=True)
    r = _upload(client, ("bescheinigung.pdf", b, "application/pdf"), ("ertraege.pdf", e, "application/pdf"),
                ("notiz.pdf", _pdf(["Hallo Welt"]), "application/pdf"),
                ("kaputt.pdf", b"%PDF-1.4 kaputt", "application/pdf"))
    assert r.status_code == 200
    assert "Jahressteuerbescheinigung 2025" in r.text and "Erträgnisaufstellung 2025" in r.text
    assert "Dokumentart nicht erkannt" in r.text
    assert "Zusammenveranlagung wurde aktiviert" in r.text  # 2.000 € > Einzel-Pauschbetrag
    svc = taxcert_service(ctx)
    assert svc.years() == [2025]
    entry = svc.for_tax()[2025]
    assert entry["bescheinigung"]["values"]["kest"] == "976.00" and len(entry["ertraegnis"]["events"]) == 5
    assert svc.checks(2025)  # Kapitalertragsteuer 976,00 ≠ 985,50 in der Erträgnisaufstellung
    folder = ctx.config.tax_data_dir / "certs" / "2025"
    assert len(list(folder.glob("*.pdf"))) == 2
    # gleicher Inhalt: nur gemeldet; geänderter Inhalt ersetzt die Fassung und löscht die alte Datei
    assert "unverändert" in _upload(client, ("b.pdf", b, "application/pdf")).text
    b2 = _pdf([ln.replace("5.910,00", "5.911,00") for ln in BESCH])
    assert "Frühere Fassung ersetzt" in _upload(client, ("b2.pdf", b2, "application/pdf")).text
    assert len(list(folder.glob("*.pdf"))) == 2
    page = client.get("/tax/certs")
    assert page.status_code == 200 and "5.911,00" in page.text and "Zeile 16 oder 17" in page.text
    assert client.get("/tax/certs/2025/bescheinigung.pdf").content.startswith(b"%PDF")
    assert client.get("/tax/certs/2025/unbekannt.pdf").status_code == 404
    assert client.post("/tax/certs/2025/bescheinigung/delete", follow_redirects=False).status_code == 303
    assert len(list(folder.glob("*.pdf"))) == 1 and "bescheinigung" not in svc.for_tax()[2025]
    client.post("/tax/certs/2025/ertraegnis/delete")
    assert svc.years() == [] and not list(folder.glob("*.pdf"))
    assert _upload(client, ("x.txt", b"nur text", "text/plain")).status_code == 200


def test_cert_values_flow_into_tax_result(client):  # noqa: F811
    from app.tax.service import tax_service

    ctx = client.app.state.ctx
    _upload(client, ("b.pdf", _pdf(BESCH), "application/pdf"), ("e.pdf", _pdf(ERTRAG, True), "application/pdf"))
    _, _, res = tax_service(ctx).compute(2025)
    assert res.options["pauschbetrag_used"] == D("2000.00") and res.data["pauschbetrag_from_cert"]
    assert res.options["joint"] is True
    assert {"bescheinigung", "ertraegnis"} == set(res.data["certs"])
    lines = {ln.label: ln.amount for ln in res.summary}
    assert lines["Höhe der Kapitalerträge"] == D("5910.00") and lines["Kapitalertragsteuer"] == D("976.00")
    f = {x.field_id: x for x in res.fields}
    assert f["kap_dom_total"].amount == D("5910.00") and f["kap_dom_total"].line == "7"
    assert f["kap_dom_kest"].line == "37" and f["kap_dom_pauschbetrag"].line == "16/17"
    codes = {i.code for i in res.issues}
    assert {"cert_sales", "cert_share_diff", "cert_total_diff"} <= codes
    # eingetragener Wert (auch „0“) ersetzt die Bescheinigung
    client.post("/tax/options", data={"year": "2025", "pauschbetrag_used": "0", "joint": "1"})
    _, _, res = tax_service(ctx).compute(2025)
    assert D(res.options["pauschbetrag_used"]) == 0 and not res.data["pauschbetrag_from_cert"]


def test_sales_comparison_matches_isin_and_date():
    rows = [buy("a1", "2024-01-10", "US1", 10, 1000, acc="DKB"), sell("a2", "2025-02-11", "US1", 10, 7500, acc="DKB"),
            buy("b1", "2024-01-10", "US2", 10, 1000, acc="DKB"), sell("b2", "2025-06-05", "US2", 10, 480, acc="DKB"),
            buy("c1", "2024-01-10", "BOND", 1, 1000, acc="DKB"), sell("c2", "2025-08-01", "BOND", 1, 900, acc="DKB")]
    events = [{"kind": "Verkäufe Wertpapier", "date": "2025-02-10", "name": "ALPHA INC.", "isin": "US0000000001",
               "amounts": ["6500.00"], "pot": False},
              {"kind": "Verkäufe Wertpapier", "date": "2025-06-05", "name": "BETA CORP.", "isin": "US0000000002",
               "amounts": ["-500.00"], "pot": False},
              {"kind": "Verkäufe Wertpapier", "date": "2025-07-01", "name": "GAMMA PLC", "isin": "US9999999999",
               "amounts": ["30.00"], "pot": False},
              {"kind": "Erträge", "date": "2025-03-18", "name": "ALPHA INC.", "isin": "US0000000001",
               "amounts": ["10.00"], "pot": False}]
    certs = {2025: {"ertraegnis": {"events": events, "filename": "e.pdf", "sha256": "ab" * 32, "uploaded": "2026-10-25",
                                   "issuer": "Beispielbank AG", "carry_out": {}}}}
    _, res = run(rows, 2025, kinds={"DKB": "domestic"}, certs=certs)
    t = next(tb for s in res.sections for tb in s.tables if tb.id == "k_cert")
    by = {r["isin"]: r for r in t.rows}
    assert by["US0000000001"]["status"] == "übereinstimmend" and by["US0000000001"]["pf"] == D("6500.00")  # ±1 Tag
    assert by["US0000000002"]["status"] == "abweichend" and by["US0000000002"]["diff"] == D("-20.00") + D("0")
    assert by["US9999999999"]["status"] == "fehlt in Portfolia"
    assert any(r["status"].startswith("nur in Portfolia") for r in t.rows)
    issue = next(i for i in res.issues if i.code == "cert_sales")
    assert "1 übereinstimmend" in issue.text and "1 nur bei der Bank" in issue.text
    assert "1 nur in Portfolia" in issue.text
    assert len(t.rows) == 4 and "Erträge" not in [r["asset"] for r in t.rows]


def test_sales_comparison_falls_back_to_date_and_amount_for_wkn_only_assets():
    # in Portfolia nur WKN (keine ISIN) hinterlegt: Zuordnung über Datum ± 3 Tage und Betrag (±2 %, mind. 10 €)
    from tests.test_tax_de import TAX_ASSETS

    TAX_ASSETS.append({"asset_id": "WKNONLY", "name": "Delta SE", "asset_class": "security", "wkn": "A1B2C3",
                       "quote_source": "none", "quote_id": ""})
    try:
        rows = [buy("a1", "2024-01-10", "WKNONLY", 10, 1000, acc="DKB"),
                sell("a2", "2025-03-11", "WKNONLY", 10, 2650, acc="DKB")]
        events = [{"kind": "Verkäufe Wertpapier", "date": "2025-03-10", "name": "DELTA SE", "isin": "DE000A1B2C31",
                   "amounts": ["1651.40"], "pot": False}]
        certs = {2025: {"ertraegnis": {"events": events, "carry_in": {"shares": "0", "other": "0"}}}}
        _, res = run(rows, 2025, kinds={"DKB": "domestic"}, certs=certs)
        t = next(tb for s in res.sections for tb in s.tables if tb.id == "k_cert")
        assert len(t.rows) == 1 and t.rows[0]["status"].startswith("abweichend")
        assert "über Datum und Betrag" in t.rows[0]["status"] and t.rows[0]["pf"] == D("1650.00")
    finally:
        TAX_ASSETS.pop()


def test_bank_sale_vs_later_write_off_in_portfolia():
    # Bank: Verkauf 05.06.2025 mit Verlust; Portfolia: stattdessen „Totalverlust“-Ausbuchung im September 2026
    rows = [buy("b1", "2024-01-10", "US2", 10, 1000, acc="DKB"),
            tx("w1", "2026-09-28 12:00", "withdrawal", tag="lost", frm=("DKB", "US2", 10), value="0"),
            buy("c1", "2024-01-10", "BOND", 1, 400, acc="DKB"),
            tx("w2", "2025-09-28 12:00", "withdrawal", tag="lost", frm=("DKB", "BOND", 1), value="0")]
    events = [{"kind": "Verkäufe Wertpapier", "date": "2025-06-05", "name": "BETA CORP.", "isin": "US0000000002",
               "amounts": ["-150.33"], "pot": False}]
    certs = {2025: {"ertraegnis": {"events": events, "carry_in": {"shares": "0", "other": "0"}}}}
    _, res = run(rows, 2025, kinds={"DKB": "domestic"}, certs=certs)
    t = next(tb for s in res.sections for tb in s.tables if tb.id == "k_cert")
    by = {r["isin"]: r for r in t.rows}
    miss = by["US0000000002"]
    assert miss["status"].startswith("fehlt in Portfolia – dort stattdessen als Ausbuchung (Totalverlust) am "
                                     "28.09.2026")
    assert "−1.000,00 €" in miss["status"] and "durch den Verkauf ersetzen" in miss["status"]
    only = by["DE0000000003"]
    assert only["status"].startswith("nur in Portfolia – Ausbuchung (Totalverlust) ohne Bankbeleg")
    assert only["pf"] == D("-400.00")
