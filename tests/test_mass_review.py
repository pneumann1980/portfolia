"""Massendaten (Phase 10): ≥ 100 000 synthetische Quellzeilen – Zählung, Warteschlange, Abdeckung, Dateilesen.

Gemessen werden die Pfade, die mit der Zeilenzahl wachsen: zentrale Prüfzählung (alle aktiven Stapel), Warteschlange
mit Filtern, Abdeckungsmatrix (eine Abfrage je Quelle) und das Einlesen einer 100 000-Zeilen-Datei. Die Grenzen sind
großzügig (CI-Rechner), die gemessenen Zeiten stehen im Bericht.
"""

# ruff: noqa: F811
from __future__ import annotations

import json
import time

from app.csvimport import queue as Q
from app.csvimport import review as RV
from app.csvimport.model import ParseOptions
from app.csvimport.profiles import detect, header_matcher
from app.csvimport.reader import read_table, zone
from app.diagnosis import coverage as CV
from tests.test_datasources import client  # noqa: F401

N_ROWS = 100_000
BATCHES = 20
STATUS = ("new", "known", "duplicate", "before", "linked", "committed", "invalid", "unclear")


def seed(db) -> None:
    per = N_ROWS // BATCHES
    with db.transaction() as c:
        for b in range(BATCHES):
            cur = c.execute("INSERT INTO csv_batch(filename, file_sha256, file_size, raw_gz, profile, account, "
                            "options_json, status, created_at, updated_at, kind, source) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (f"mass{b}.csv", f"sha{b}", 0, b"", "kraken", f"Konto{b % 5}", "{}",
                             "partial" if b % 2 else "preview", "2026-01-01", "2026-01-01", "csv", None))
            bid = cur.lastrowid
            rows = []
            for i in range(per):
                st = STATUS[i % len(STATUS)]
                month = 1 + (i % 12)
                rec = {"line": i, "ts": f"2024-{month:02d}-15T10:00:00Z", "kind": "deposit", "in_sym": "EUR",
                       "in_qty": "1", "account": f"Konto{b % 5}", "event_key": f"ev{b}-{i // 2}"}
                msgs = {"match": {"cat": "neu", "conf": "hoch", "action": "include"}} if st == "new" else {}
                rows.append((bid, i, i, json.dumps(rec), st, None, json.dumps(msgs), f"ev{b}-{i // 2}"))
            c.executemany("INSERT INTO csv_row(batch_id, idx, line, rec_json, status, decision, messages, event_key) "
                          "VALUES (?,?,?,?,?,?,?,?)", rows)


def test_mass_review_queue_and_coverage(client):
    db = client.app.state.ctx.db
    seed(db)
    assert db.scalar("SELECT COUNT(*) FROM csv_row") == N_ROWS
    t0 = time.perf_counter()
    s = RV.active_summary(db)
    t_summary = time.perf_counter() - t0
    assert s.total == N_ROWS // 2 and s.open > 0  # Vorgang = 2 Zeilen
    t0 = time.perf_counter()
    qu = Q.build(db, "offen", flt=Q.Filter(account="Konto1", frm="2024-03-01", to="2024-06-30"))
    t_queue = time.perf_counter() - t0
    assert qu.total > 0 and len(qu.items) <= Q.PAGE
    t0 = time.perf_counter()
    mx = CV.build(client.app.state.ctx)
    t_cov = time.perf_counter() - t0
    assert len(mx.rows) == 5 and mx.months[0] == "2024-01"
    print(f"\nMassentest {N_ROWS} Zeilen: Zählung {t_summary:.2f}s, Warteschlange {t_queue:.2f}s, "
          f"Abdeckung {t_cov:.2f}s")
    assert t_summary < 20 and t_queue < 30 and t_cov < 30


def test_mass_file_read_and_parse():
    head = '"txid","refid","time","type","subtype","aclass","asset","wallet","amount","fee","balance"\n'
    body = "".join(f'"L{i}","R{i}","2024-03-01 10:00:00","deposit","","currency","ZEUR","spot / main",1.0,0.0,0\n'
                   for i in range(N_ROWS))
    data = (head + body).encode()
    t0 = time.perf_counter()
    table = read_table(data, header_matcher())
    prof = detect(table.keys)
    assert prof is not None
    opts = ParseOptions(tz=zone(prof.tz), decimal=prof.decimal, account="Kraken", filename="mass.csv")
    res = prof.parse(table, opts)
    dt = time.perf_counter() - t0
    print(f"\nDatei {N_ROWS} Zeilen gelesen und ausgewertet: {dt:.2f}s")
    assert prof.id == "kraken" and res.rows_read == N_ROWS and len(res.recs) == N_ROWS
    assert dt < 60
