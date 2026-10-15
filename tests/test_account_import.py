"""Datei-Import je Börse bzw. Konto (Datenquellen) – nur synthetische Daten, temporäre Datenbank.

Abgedeckt: Börse mit Exportdatei anlegen (Vorgänge direkt dieser Börse zugeordnet), bestehende Konten ohne
Datenquelle erscheinen als „manuell · Datei-Import“, je Konto neuer Import mit Umfang „gesamte Historie“ bzw. „nur
neue Transaktionen“ (Stichtag aus der letzten Buchung), nichts wird ohne Prüfung gebucht.
"""

# ruff: noqa: F811
from __future__ import annotations

import re

from app.csvimport.service import csv_service
from app.datasources.service import datasource_service
from app.journal.service import journal_service
from tests.test_csvimport import post
from tests.test_datasources import client  # noqa: F401
from tests.test_universal_import import kraken_rows, make_xlsx

MEXC = "MEXC Global (MXC)"


def ctx(c):
    return c.app.state.ctx


def book(c, account: str, day: str, qty: str = "100") -> None:
    res = journal_service(ctx(c)).save({"kind": "expert", "type": "deposit", "tag": "", "date": day, "time": "09:00",
                                        "to_account": account, "to_asset": "EUR", "to_qty": qty, "value_eur": qty})
    assert not res.errors, res.errors


def xlsx_with_new_row() -> bytes:
    rows = [*kraken_rows(), ["L9", "R9", 45361 + 10 / 24, "deposit", "", "currency", "ZEUR", "spot / main", 75, 0,
                             1325]]  # 2024-03-10
    return make_xlsx(rows, date_cols=(2,))


def batch_of(r) -> int:
    assert r.status_code == 303 and "/journal/csv/" in r.headers["location"], r.headers.get("location")
    return int(re.search(r"/journal/csv/(\d+)", r.headers["location"]).group(1))


def test_exchange_created_with_file_assigns_bookings_to_it(client):
    c = client
    assert 'enctype="multipart/form-data"' in c.get("/settings/datasources/new?kind=exchange").text
    r = c.post("/settings/datasources", data={"csrf_token": c.token, "kind": "exchange", "provider": "mexc",
                                              "name": "MEXC", "account": "MEXC", "sync_interval_min": "0",
                                              "mode": "all"},
               files={"file": ("mexc.xlsx", make_xlsx(kraken_rows(), date_cols=(2,)), "application/octet-stream")},
               follow_redirects=False)
    bid = batch_of(r)
    b = csv_service(ctx(c)).batch(bid)
    sid = ctx(c).db.scalar("SELECT id FROM data_source WHERE account='MEXC'")
    assert b["datasource_id"] == sid and b["account"] == "MEXC"
    assert csv_service(ctx(c)).options(b).get("cutoff") == ""  # gesamte Historie: kein Stichtag
    assert ctx(c).db.scalar("SELECT COUNT(*) FROM journal_tx WHERE batch_id=?", (bid,)) == 0  # nichts ungeprüft gebucht


def test_existing_accounts_without_source_are_listed_as_file_import(client):
    c = client
    book(c, MEXC, "2024-02-01")
    book(c, MEXC, "2024-03-02")
    post(c, "/settings/datasources", kind="exchange", provider="kraken", name="Kraken", account="Kraken",
         sync_interval_min="0")
    book(c, "Kraken", "2024-01-05")
    rows = datasource_service(ctx(c)).unlinked_accounts()
    assert [a["account"] for a in rows] == [MEXC]
    a = rows[0]
    assert a["provider"] == "mexc" and a["n"] == 2 and str(a["last"]) == "2024-03-02"
    assert a["origins"] == {"manuell erfasst": 2}
    page = c.get("/settings/datasources").text
    assert "Konten ohne Datenquelle" in page and MEXC in page and "manuell · Datei-Import" in page
    assert "Nur neue Transaktionen" in page and "Gesamte Historie" in page and "02.03.2024" in page


def test_new_import_per_account_only_new_transactions(client):
    """„Nur neue“: Stichtag = Tag vor der letzten Buchung – ältere Zeilen sind Historie (nicht zu prüfen), der
    Überlappungstag und spätere Vorgänge werden abgeglichen bzw. vorgelegt; das Konto wird Datenquelle."""
    c = client
    book(c, MEXC, "2024-03-02", "250")
    booked = ctx(c).db.q("SELECT tx_id, status, to_qty FROM journal_tx ORDER BY tx_id")
    r = c.post("/settings/datasources/account-source", data={"csrf_token": c.token, "account": MEXC,
                                                              "provider": "mexc", "mode": "new"},
               files={"file": ("mexc.xlsx", xlsx_with_new_row(), "application/octet-stream")}, follow_redirects=False)
    bid = batch_of(r)
    svc = csv_service(ctx(c))
    b = svc.batch(bid)
    ds = datasource_service(ctx(c)).get(int(b["datasource_id"]))
    assert ds.account == MEXC and ds.provider == "mexc" and not ds.supported
    assert svc.options(b)["cutoff"] == "2024-03-01"
    st = [r["status"] for r in ctx(c).db.q("SELECT status FROM csv_row WHERE batch_id=? ORDER BY idx", (bid,))]
    assert st[0] == "before" and "before" not in st[1:]  # 01.03. Historie; 02.03. (Überlappung) und 10.03. geprüft
    assert ctx(c).db.q("SELECT tx_id, status, to_qty FROM journal_tx ORDER BY tx_id") == booked  # nichts gebucht
    assert not ctx(c).db.scalar("SELECT COUNT(*) FROM csv_row WHERE batch_id=? AND tx_id IS NOT NULL", (bid,))
    assert datasource_service(ctx(c)).unlinked_accounts() == []  # Konto ist jetzt Datenquelle
    assert "Letzter Datei-Import" in c.get("/settings/datasources").text


def test_new_import_full_history_and_adopt_without_file(client):
    c = client
    book(c, MEXC, "2024-03-05")
    r = post(c, "/settings/datasources/account-source", account=MEXC, provider="mexc")
    assert r.status_code == 303 and "#datei" in r.headers["location"]
    sid = int(re.search(r"/settings/datasources/(\d+)", r.headers["location"]).group(1))
    assert post(c, "/settings/datasources/account-source", account=MEXC).headers["location"].startswith(
        f"/settings/datasources/{sid}")  # idempotent: dieselbe Quelle
    page = c.get(f"/settings/datasources/{sid}").text
    assert "Datei-Import" in page and "05.03.2024" in page
    r = c.post(f"/settings/datasources/{sid}/upload", data={"csrf_token": c.token, "mode": "all"},
               files={"file": ("mexc.xlsx", xlsx_with_new_row(), "application/octet-stream")}, follow_redirects=False)
    bid = batch_of(r)
    assert csv_service(ctx(c)).options(csv_service(ctx(c)).batch(bid))["cutoff"] == ""
    st = [r["status"] for r in ctx(c).db.q("SELECT status FROM csv_row WHERE batch_id=?", (bid,))]
    assert "before" not in st and len(st) == 3
    assert "Konto" in post(c, "/settings/datasources/account-source", account="Unbekannt").headers["location"]
