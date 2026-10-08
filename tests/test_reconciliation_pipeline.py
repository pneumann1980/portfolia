"""M24/AP3.11 – Mehrquellen-Szenarien über Prüf-Stapel (CSV-Datei, Bitpanda-API, kuratierter Import).

Erwartungen (Buchungen, Bestände, Lots, Kostenbasis) sind aus den Eingabedaten nachgerechnet. Die Bitpanda-API ist
der strenge Mock aus ``test_bitpanda`` (dokumentierte Felder), CSV-Dateien sind synthetisch.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.csvimport import batch as BA
from app.csvimport.service import csv_service
from app.datasources.service import datasource_service
from app.jobs import tasks
from tests.test_bitpanda import BP_CSV_HEAD, U, create_bitpanda, ctx, op_key, rows_by_key
from tests.test_csvimport import create_unknown_assets, journal, post, upload
from tests.test_importcheck import (  # noqa: F401
    HASH,
    KOINLY_HEAD,
    WALLET,
    _import,
    _upload_csv,
    _usdt,
    api,
    client,
    koinly_rows,
    scenario,
)

D = Decimal
BUY_CSV = (BP_CSV_HEAD + f"T{U(0x2001)},2024-02-02T10:00:00+01:00,buy,outgoing,250.00,EUR,0.0055,BTC,-,-,"
           "Cryptocurrency,1,-,-,-,-,0.00\n")


def lots_of(c, asset: str, acc: str | None = None) -> list[tuple[str, Decimal, Decimal, date]]:
    x = ctx(c)
    x.invalidate_data()
    return sorted((lot.account, lot.qty, lot.cost.quantize(D("0.01")), lot.acq_date) for lot in x.ledger().lots
                  if lot.asset == asset and lot.qty > 0 and (acc is None or lot.account == acc))


def acquired(c, asset: str, day: date) -> dict[str, Decimal]:
    """Anschaffungen eines Tages je anschaffender Buchung: offene Lots + bereits veräußerte Anteile."""
    x = ctx(c)
    x.invalidate_data()
    led = x.ledger()
    out: dict[str, Decimal] = {}
    for lot in led.lots:
        if lot.asset == asset and lot.acq_date == day:
            out[lot.acq_tx] = out.get(lot.acq_tx, D(0)) + lot.qty
    for d in led.disposals:
        for p in d.parts:
            if d.asset == asset and p.acq_date == day and p.acq_tx:
                out[p.acq_tx] = out.get(p.acq_tx, D(0)) + p.qty
    return out


def bal(c, acc: str, asset: str) -> Decimal:
    x = ctx(c)
    x.invalidate_data()
    return x.ledger().balances.get((acc, asset), D(0))


# ----------------------------------------------------------------------------------------------------------------------
# 1 · identische Buchung aus CSV und API (Stufe A: automatisch verknüpft)  /  12 · wiederholte Synchronisierung
# 2 · derselbe Kauf zusätzlich im Steuertool-Import
# ----------------------------------------------------------------------------------------------------------------------

def test_same_trade_from_csv_api_and_tax_tool_counts_once(client, api):  # noqa: F811
    c = client
    b, _ = upload(c, "bitpanda.csv", BUY_CSV.encode(), account="Bitpanda")
    create_unknown_assets(c, b)
    post(c, f"/journal/csv/{b}/commit")
    (buy,) = journal(c, "source='csv:bitpanda'")
    sid = create_bitpanda(c, auto_commit="1")
    res = datasource_service(ctx(c)).sync(sid)
    db = ctx(c).db
    # Soll: der Kauf (0,0055 BTC für 250 €, 02.02.2024) existiert genau einmal; die API-Zeile ist nur verknüpft
    assert res["linked"] >= 1 and not journal(c, f"event_key='{op_key(1)}'")
    link = db.q1("SELECT * FROM tx_link WHERE tx_id=? AND status='active'", (buy["tx_id"],))
    assert link is not None and link["source"] == "sync:bitpanda"
    act = db.q1("SELECT * FROM import_action WHERE params_json LIKE '%\"stage\": \"A\"%'")
    assert act is not None and act["status"] == "active"  # protokolliert und rückgängig zu machen
    assert acquired(c, "BTC", date(2024, 2, 2)) == {buy["tx_id"]: D("0.0055")}
    # 12: erneute Synchronisierung – nichts doppelt, nichts erneut verknüpft
    n_j, n_l = len(journal(c)), db.scalar("SELECT COUNT(*) FROM tx_link")
    res2 = datasource_service(ctx(c)).sync(sid)
    assert res2.get("linked", 0) == 0 and len(journal(c)) == n_j and db.scalar("SELECT COUNT(*) FROM tx_link") == n_l
    # 2: Steuertool-Import enthält denselben Kauf (Bitpanda-ID) → App-Buchung gilt als im Import enthalten
    from app.importer.zipbuilder import build_zip
    from tests.helpers import ASSETS, tx

    r1 = tx("IMP-1", "2024-02-02T09:00:00Z", "buy", frm=("Bitpanda", "EUR", "250"), to=("Bitpanda", "BTC", "0.0055"),
            value="250")
    r1["source"], r1["source_ref"] = "bitpanda", f"T{U(0x2001)}"
    dst = c.app.state.ctx.config.import_dir / "steuertool.zip"
    build_zip(dst, transactions=[r1], assets=ASSETS, generated_at="2024-03-02T00:00:00Z",
              valuation_date="2024-03-01")
    import os
    import time

    os.utime(dst, (time.time() - 3600,) * 2)
    assert tasks.import_check(ctx(c), "test").status == "imported"
    got = acquired(c, "BTC", date(2024, 2, 2))
    assert sum(got.values()) == D("0.0055") and len(got) == 1  # weiterhin genau eine Anschaffung


# ----------------------------------------------------------------------------------------------------------------------
# 4 · drei Quellen für denselben Transfer (Steuertool-Import, Börsen-API, Wallet-Export)
# ----------------------------------------------------------------------------------------------------------------------

LEDGER_HEAD = ("Operation Date,Status,Currency Ticker,Operation Type,Operation Amount,Operation Fees,Operation Hash,"
               "Account Name,Account xpub,Countervalue Ticker,Countervalue at Operation Date,"
               "Countervalue at CSV Export\n")


def test_three_sources_for_one_transfer_never_book_twice(client, config, api):  # noqa: F811
    c = client
    bid, _rows = scenario(c, config, api, "extra")  # Koinly-Transfer im Import + Bitpanda-Auszahlung (API)
    before = len(journal(c))
    # Bitpanda-Seite: Transferseite mit passender Gebühr – „hoch“, keine technische Identität → keine Automatik
    # (Stufe A), sondern Vorschlag mit Bestätigung (Stufe B: Sammelvorschau des Prüf-Stapels)
    assert not BA.safe_link_rows(csv_service(ctx(c)).rows(bid))
    from tests.test_importcheck import _execute, _preview

    _html, form = _preview(c, bid)
    assert _execute(c, bid, form).status_code == 303
    assert {rc.status for rc in csv_service(ctx(c)).rows(bid)} == {"linked"}
    # Wallet-Export (Ledger Live): Eingang mit demselben Hash
    csv = LEDGER_HEAD + (f"2024-03-19T09:52:00.000Z,Confirmed,USDT,IN,734.512861,0,{HASH},Ethereum 1,xpub1,EUR,"
                         "675.93,675.93\n")
    b2 = _upload_csv(c, "ledger.csv", csv, WALLET)
    (row,) = csv_service(ctx(c)).rows(b2)
    assert row.status in ("known", "duplicate") and not row.include()
    assert (row.match or {}).get("target") == "KOI-T"
    BA.auto_link(csv_service(ctx(c)), b2)
    post(c, f"/journal/csv/{b2}/commit")
    # Soll: keine zusätzliche Buchung; Wallet 734,512861 USDT; Bitpanda 900 − 734,512861 − 11,28465017
    assert len(journal(c)) == before
    assert bal(c, WALLET, "USDT") == D("734.512861")
    assert bal(c, "Bitpanda", "USDT") == D("154.20248883")
    # Anschaffung des Wallet-Bestands bleibt die der Einzahlung (01.03.2024), nicht der Transfer-Tag
    assert {x[3] for x in lots_of(c, "USDT", WALLET)} == {date(2024, 3, 1)}


# ----------------------------------------------------------------------------------------------------------------------
# 9 · unterschiedliche Gebühreninformationen: nie automatisch verknüpft
# ----------------------------------------------------------------------------------------------------------------------

def test_conflicting_fee_information_is_not_linked_automatically(client, config, api):  # noqa: F811
    c = client
    bid, rows = scenario(c, config, api, "inside")  # Saldo der Börse belegt „Gebühr im Betrag“ ≠ Import
    w = rows[f"bitpanda:{U(0x701)}"]
    assert w.match["cat"] in ("widerspruch", "komplex") or (w.match.get("fee") or {}).get("state") == "conflict"
    before = len(journal(c))
    assert w.idx not in BA.safe_link_rows(csv_service(ctx(c)).rows(bid))
    BA.auto_link(csv_service(ctx(c)), bid)
    w2 = {rc.rec.event_key: rc for rc in csv_service(ctx(c)).rows(bid)}[f"bitpanda:{U(0x701)}"]
    assert w2.status != "linked" and len(journal(c)) == before
    assert bal(c, "Bitpanda", "USDT") == D("154.20248883")  # Import gilt unverändert


# ----------------------------------------------------------------------------------------------------------------------
# 11 · wiederholter Import derselben CSV-Datei
# ----------------------------------------------------------------------------------------------------------------------

def test_repeated_csv_import_books_nothing_twice(client, config):  # noqa: F811
    c = client
    csv = (KOINLY_HEAD
           + "2024-04-01 10:00:00 UTC,deposit,,,,,,Ledger,0.5,ETH,,,,0,1500,0,,,0x" + "11" * 32 + ",\n"
           + "2024-04-02 10:00:00 UTC,deposit,,,,,,Ledger,0.25,ETH,,,,0,800,0,,,0x" + "22" * 32 + ",\n")
    b1 = _upload_csv(c, "koinly.csv", csv, "Ledger")
    create_unknown_assets(c, b1)
    post(c, f"/journal/csv/{b1}/commit")
    assert bal(c, "Ledger", "ETH") == D("0.75")
    n = len(journal(c))
    b2 = _upload_csv(c, "koinly-kopie.csv", csv, "Ledger")
    st = {rc.status for rc in csv_service(ctx(c)).rows(b2)}
    assert st <= {"known"}
    post(c, f"/journal/csv/{b2}/commit")
    assert len(journal(c)) == n and bal(c, "Ledger", "ETH") == D("0.75")
    assert lots_of(c, "ETH", "Ledger") == [("Ledger", D("0.25"), D("800.00"), date(2024, 4, 2)),
                                           ("Ledger", D("0.5"), D("1500.00"), date(2024, 4, 1))]
    # Stufe A greift bei gleicher Quelle nicht (nichts zu verknüpfen – bereits übernommen)
    assert BA.auto_link(csv_service(ctx(c)), b2) == 0


def test_rows_with_own_user_decision_are_never_auto_linked(client, api):  # noqa: F811
    c = client
    b, _ = upload(c, "bitpanda.csv", BUY_CSV.encode(), account="Bitpanda")
    create_unknown_assets(c, b)
    post(c, f"/journal/csv/{b}/commit")
    sid = create_bitpanda(c)  # ohne automatische Übernahme
    bid = datasource_service(ctx(c)).sync(sid)["batch_id"]
    row = rows_by_key(c, bid)[f"{op_key(1)}#0"]
    assert row.idx in BA.safe_link_rows(csv_service(ctx(c)).rows(bid))  # technische Identität (Trade-ID)
    ctx(c).db.x("UPDATE csv_row SET decision='skip' WHERE batch_id=? AND idx=?", (bid, row.idx))
    assert row.idx not in BA.safe_link_rows(csv_service(ctx(c)).rows(bid))  # eigene Entscheidung hat Vorrang
