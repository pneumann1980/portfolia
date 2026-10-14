"""Historische Quellenabdeckung (Financial Integrity, Phase 5) – nur synthetische Dateien.

MEXC-Fall nachgebildet: zwei Exportdateien derselben Börse decken Januar–März und Juni–August ab; April und Mai sind
eine Lücke (kein Export, keine Buchung). Ein Monat innerhalb eines Dateizeitraums ohne Vorgang ist „abgedeckt, ohne
Vorgänge“, nicht Lücke. Eine andere Quelle desselben Kontos macht die Lücke zu „anderweitig belegt“.
"""

# ruff: noqa: F811
from __future__ import annotations

import json
import re

from app.diagnosis import coverage as CV
from tests.test_csvimport import create_unknown_assets, post
from tests.test_datasources import client  # noqa: F401
from tests.test_review_workflow import snapshot

HEAD = '"txid","refid","time","type","subtype","aclass","asset","wallet","amount","fee","balance"\n'


def ctx(c):
    return c.app.state.ctx


def kraken_csv(days: list[str], prefix: str) -> bytes:
    rows = [f'"{prefix}{i}","R{prefix}{i}","{d} 10:00:00","deposit","","currency","ZEUR","spot / main",100.0,0.0,0'
            for i, d in enumerate(days)]
    return (HEAD + "\n".join(rows) + "\n").encode()


def upload(c, name: str, data: bytes, account: str = "MEXC") -> int:
    r = c.post("/journal/csv", data={"csrf_token": c.token, "profile": "kraken", "account": account, "cutoff": "",
                                     "cutoff_set": "1"},
               files={"file": (name, data, "text/csv")}, follow_redirects=False)
    assert r.status_code == 303, r.text[:400]
    return int(re.search(r"/journal/csv/(\d+)", r.headers["location"]).group(1))


def row(mx: CV.Matrix, account: str) -> CV.Row:
    return next(r for r in mx.rows if r.account == account)


def test_gap_between_two_export_files_is_a_gap_quiet_month_is_covered(client):
    c = client
    upload(c, "mexc_q1.csv", kraken_csv(["2024-01-05", "2024-03-20"], "A"))
    upload(c, "mexc_q3.csv", kraken_csv(["2024-06-02", "2024-08-30"], "B"))
    mx = CV.build(ctx(c))
    r = row(mx, "MEXC")
    states = {m: cell.state for m, cell in r.cells.items()}
    assert states["2024-01"] == states["2024-03"] == "staged"  # eingelesen, nicht gebucht
    assert states["2024-02"] == "abgedeckt"  # Datei Jan–Mär deckt Februar ab: echter ruhiger Monat
    assert states["2024-04"] == states["2024-05"] == "luecke"
    assert states["2024-07"] == "abgedeckt"
    assert r.gaps == [("2024-04", "2024-05")]
    assert [(a, b, k) for a, b, k in r.periods] == [("2024-01", "2024-03", "Datei"), ("2024-06", "2024-08", "Datei")]


def test_booked_months_and_other_source_on_same_account(client):
    c = client
    bid = upload(c, "mexc_q1.csv", kraken_csv(["2024-01-05", "2024-03-20"], "A"))
    create_unknown_assets(c, bid)
    assert post(c, f"/journal/csv/{bid}/commit").status_code == 303
    upload(c, "mexc_q3.csv", kraken_csv(["2024-06-02"], "B"))
    mx = CV.build(ctx(c))
    r = row(mx, "MEXC")
    assert r.cells["2024-01"].state == "gebucht" and r.cells["2024-01"].booked == 1
    assert r.cells["2024-01"].areas == {"transfer": 1}
    assert r.gaps == [("2024-04", "2024-05")]
    # Bereichsfilter: nur Erträge → keine Buchung dieser Quelle
    assert not [x for x in CV.build(ctx(c), area="ertrag").rows if x.account == "MEXC" and x.cells.get("2024-01")
                and x.cells["2024-01"].booked]
    # Zeitraumfilter
    mx2 = CV.build(ctx(c), frm="2024-02", to="2024-04")
    assert mx2.months == ["2024-02", "2024-03", "2024-04"]


def test_api_complete_history_covers_months_and_enables_research(client):
    c = client
    r = post(c, "/settings/datasources", kind="exchange", provider="mexc", name="MEXC API", account="MEXC",
             sync_interval_min="0")
    sid = int(re.search(r"/settings/datasources/(\d+)", r.headers["location"]).group(1))
    upload(c, "mexc_q1.csv", kraken_csv(["2024-01-05"], "A"))
    upload(c, "mexc_q3.csv", kraken_csv(["2024-06-02"], "B"))
    # synthetischer, vollständiger Abruf der Historie ab 2024-04 (z. B. API liefert erst ab dort)
    ctx(c).db.x("UPDATE data_source SET coverage_json=?, last_success_at=? WHERE id=?",
                (json.dumps({"complete": True, "mode": "vollständig", "history": {"first": "2024-04-01T00:00:00Z"}}),
                 "2024-09-01T00:00:00+00:00", sid))
    mx = CV.build(ctx(c))
    api = next(x for x in mx.rows if x.account == "MEXC" and "MEXC" in x.source)
    assert api.cells["2024-04"].state == "abgedeckt" and api.datasource_id == sid and api.research
    file_row = next(x for x in mx.rows if x.account == "MEXC" and x is not api)
    assert file_row.cells["2024-04"].state == "anderweitig"  # die Datei-Quelle schweigt, der Abruf deckt ab
    assert file_row.gaps == [("2024-02", "2024-03")]  # vor dem API-Zeitraum bleibt die Lücke bestehen


def test_coverage_page_is_read_only_and_offers_next_steps(client):
    c = client
    upload(c, "mexc_q1.csv", kraken_csv(["2024-01-05"], "A"))
    upload(c, "mexc_q3.csv", kraken_csv(["2024-06-02"], "B"))
    before = snapshot(c)
    page = c.get("/quality/abdeckung").text
    assert "Historische Quellenabdeckung" in page and "Lücken und nächste Schritte" in page
    assert "2024-02 bis 2024-05" in page and "CSV-Import" in page
    assert c.get("/quality/abdeckung?frm=2024-03&to=kaputt&area=unbekannt").status_code == 200
    assert snapshot(c) == before
    assert CV.month_end("2024-12") == "2025-01-01T00:00:00+00:00" and CV.month_end("2024-02") == \
        "2024-03-01T00:00:00+00:00"
