"""Importprüfung: Abgleich je Zeile (Ergebnis, Sicherheit, Belege, Gebührenprüfung), Stapelaktionen mit Vorschau,
Protokoll und Rückgängig, Verknüpfen statt Verwerfen, Quellenvorrang, Bitpanda-Vollständigkeitsbericht.

Nur synthetische Daten. Zentraler Regressionsfall (Vorgabe des Auftraggebers) strukturgleich nachgebildet – andere
Zahlen, damit keine echten Buchungsdaten ins Repository gelangen: Steuertool-Transfer Bitpanda → eigene Wallet mit
Hash, Gebühr separat (USDT, 6 bzw. 8 Nachkommastellen), und Bitpanda-Auszahlung 6 Minuten früher mit gleicher Menge
und Gebühr, EUR-Wert um < 0,1 % abweichend, ohne Empfänger und Hash.
"""

from __future__ import annotations

import os
import re
import time
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.csvimport.service import csv_service
from app.importer.zipbuilder import build_zip
from app.jobs import tasks
from tests import test_bitpanda as TB
from tests.helpers import ASSETS, tx
from tests.test_bitpanda import U, batch_of, create_bitpanda, ctx, oper, page, sync, txn
from tests.test_csvimport import journal, post


@pytest.fixture
def api(monkeypatch):
    """Bitpanda-API laut Referenz (strenger Mock aus test_bitpanda)."""
    fake = TB.FakeBitpanda()
    monkeypatch.setattr(TB.B.BitpandaConnector, "transport", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(TB.B.BitpandaConnector, "sleep", staticmethod(lambda _s: None))
    return fake


@pytest.fixture
def client(config, monkeypatch):
    monkeypatch.setenv("PORTFOLIA_MASTER_KEY", TB.MASTER)
    with TB.make_client(config) as c:
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        yield c

D = Decimal
USDT_ID = U(0x7D)
HASH = "0x" + "ab" * 32
W_USDT = U(0xF7D)
# ohne Kursquelle: bewertet wird mit dem manuellen Kurs des Imports (Demo-Kurse wären zufällig)
ASSETS_USDT = [*ASSETS, {"asset_id": "USDT", "name": "Tether", "asset_class": "crypto", "quote_source": "none",
                         "quote_id": "", "category": "Krypto: Stablecoins", "aliases": "Tether;USDT"}]


def _usdt(api: Any) -> None:
    api.assets[USDT_ID] = {"id": USDT_ID, "name": "Tether", "symbol": "USDT", "isin": None, "group": "CRYPTOCOIN",
                           "type": "CRYPTO", "buy_active": True, "sell_active": True, "withdrawal_active": True,
                           "deposit_active": True}


def _import(config: Any, rows: list[dict[str, Any]], valuation: str = "2024-03-02",
            prices: list[dict[str, Any]] | None = None) -> None:
    dst = config.import_dir / "imp.zip"
    build_zip(dst, transactions=rows, assets=ASSETS_USDT, generated_at="2024-03-02T00:00:00Z",
              valuation_date=valuation, manual_prices=prices)
    old = time.time() - 3600
    os.utime(dst, (old, old))


WALLET = "Hardware-Wallet (Ethereum)"


def koinly_rows() -> list[dict[str, Any]]:
    dep = tx("KOI-D", "2024-03-01T09:00:00Z", "deposit", to=("Bitpanda", "USDT", "900"), value="828")
    dep["source"], dep["source_ref"] = "koinly", "K0001"
    tr = tx("KOI-T", "2024-03-19T09:47:00Z", "transfer", frm=("Bitpanda", "USDT", "734.512861"),
            to=(WALLET, "USDT", "734.512861"), fee=("USDT", "11.28465017", ""), value="675.93")
    tr["source"], tr["source_ref"], tr["note"] = "koinly", "K0002", f"txhash {HASH}"
    return [dep, tr]


def bitpanda_ops(fee_mode: str) -> list[dict[str, Any]]:
    """Einzahlung (vor dem Stichtag) und Auszahlung mit Gebühr; ``fee_mode`` legt den Saldoverlauf fest:
    extra (Saldo sinkt um Betrag + Gebühr), inside (nur um den Betrag), open (kein Saldo – nicht belegbar)."""
    bal = {"extra": "154.20248883", "inside": "165.487139", "open": None}[fee_mode]
    return [
        oper(0, "deposit", txn(0, "INCOMING", "900", USDT_ID, "2024-03-01T09:00:00Z", balance="900",
                               wallet=W_USDT)),
        oper(1, "withdrawal", txn(1, "OUTGOING", "734.512861", USDT_ID, "2024-03-19T09:41:00Z", fee="11.28465017",
                                  balance=bal, wallet=W_USDT if bal else U(0xF99))),
    ]


def scenario(c: Any, config: Any, api: Any, fee_mode: str = "extra", cutoff: str | None = "") -> tuple[int, Any]:
    _usdt(api)
    _import(config, koinly_rows(), prices=[{"asset_id": "USDT", "date": "2024-03-19", "price_eur": "0.91962",
                                            "source": "test"}])
    assert tasks.import_check(ctx(c), "test").status == "imported"
    api.set_pages(page(bitpanda_ops(fee_mode)))
    sid = create_bitpanda(c)
    bid = batch_of(sync(c, sid))
    if cutoff is not None:
        r = post(c, f"/journal/csv/{bid}/options", cutoff=cutoff)
        assert r.status_code == 303, r.text[:500]
    rows = {rc.rec.event_key: rc for rc in csv_service(ctx(c)).rows(bid)}
    return bid, rows


def _w(rows: dict[str, Any]) -> Any:
    return rows[f"bitpanda:{U(0x701)}"]


# ----------------------------------------------------------------------------------------------------
# Zentraler Regressionsfall
# ----------------------------------------------------------------------------------------------------

def test_koinly_transfer_vs_bitpanda_withdrawal_fee_extra(client, config, api):
    c = client
    before = len(journal(c))
    _bid, rows = scenario(c, config, api, "extra")
    w = _w(rows)
    assert w.status == "duplicate" and w.dup_of == ["KOI-T"] and not w.include()  # nie still zusätzlich gebucht
    m = w.match
    assert m["cat"] == "ergaenzung" and m["conf"] == "hoch" and m["action"] == "link" and m["role"] == "out"
    assert m["basis"] == "transfer_leg"
    text = " | ".join([*m["ok"], *(d["t"] for d in m["diff"]), *(a["t"] for a in m["add"])])
    assert "Abgang 734,512861 USDT" in text and "Konto Bitpanda" in text
    assert f"Abgangsseite des Transfers Bitpanda → {WALLET}" in text
    assert f"Empfänger {WALLET} nur in der vorhandenen Buchung" in text
    # Zeit und EUR-Wert: geringe Abweichungen, beide Werte mit Herkunft
    ts = next(d for d in m["diff"] if d["f"] == "ts")
    assert ts["sev"] == "minor" and "6 min" in ts["t"] and "10:47" in ts["t"] and "10:41" in ts["t"]
    val = next(d for d in m["diff"] if d["f"] == "value")
    assert val["sev"] == "minor" and "675,47" in val["t"] and "675,93" in val["t"]
    assert "manueller Kurs" in val["t"]  # Herkunft des neuen Werts (hier kein Börsenwert, sondern Kurs × Menge)
    # Gebührenprüfung: zusätzlich belastet (Saldoverlauf) – wie die vorhandene Buchung
    assert m["fee"]["state"] == "ok" and "zusätzlich zum Betrag" in m["fee"]["t"] and "745,79751117" in m["fee"]["t"]
    # Zeitpunkt der Börse als Ergänzung mit Vorrang, keine Überschreibung
    assert any(a["f"] == "ts" and a.get("pref") == "new" for a in m["add"])
    assert len(journal(c)) == before  # nichts gebucht
    # Einzahlung vor dem Stichtag? Hier ohne Stichtag: gleiche Menge auf demselben Konto → als vorhanden erkannt
    dep = rows[f"bitpanda:{U(0x700)}"]
    assert dep.status == "duplicate" and dep.match["cat"] in ("dublette", "ergaenzung")


def test_koinly_transfer_vs_bitpanda_withdrawal_fee_not_provable(client, config, api):
    _bid, rows = scenario(client, config, api, "open")
    m = _w(rows).match
    assert m["cat"] == "ergaenzung" and m["conf"] == "hoch"
    assert m["fee"]["state"] == "open" and "belegen ihre Daten nicht" in m["fee"]["t"]


def test_koinly_transfer_vs_bitpanda_withdrawal_fee_inside_is_contradiction(client, config, api):
    _bid, rows = scenario(client, config, api, "inside")
    w = _w(rows)
    # Netto 723,22821083 + Gebühr = 734,512861 = Transferbetrag laut Steuertool → derselbe Vorgang, Gebühr strittig
    assert w.status == "duplicate" and w.dup_of == ["KOI-T"] and not w.include()
    m = w.match
    assert m["cat"] == "widerspruch" and m["fee"]["state"] == "conflict"
    assert "im Betrag 734,512861 enthalten" in m["fee"]["t"] and "11,28465017" in m["fee"]["t"]
    assert m["fix"] and "723,22821083" in m["fix"][0]
    assert not any(d["f"] == "out" for d in m["diff"])  # Mengenabweichung durch die Gebühr erklärt


def test_rows_before_cutoff_are_matched_without_status_change(client, config, api):
    _bid, rows = scenario(client, config, api, "extra", cutoff=None)  # Stichtag = Bewertungstag des Imports
    dep = rows[f"bitpanda:{U(0x700)}"]
    assert dep.status == "before" and dep.dup_of == ["KOI-D"]
    assert dep.match["cat"] in ("dublette", "ergaenzung") and dep.match["action"] == "link"


# ----------------------------------------------------------------------------------------------------
# Stapelaktionen: Übersicht, Auswahl, Vorschau, Ausführen, Wiedererkennen, Rückgängig
# ----------------------------------------------------------------------------------------------------

def _preview(c: Any, bid: int, action: str = "suggest", force: bool = False) -> tuple[str, dict[str, str]]:
    r = c.get(f"/journal/csv/{bid}/preview?action={action}" + ("&force=1" if force else ""))
    assert r.status_code == 200, r.text[:1500]
    form = dict(re.findall(r'name="(token|fingerprint|action|force)" value="([^"]*)"', r.text))
    return r.text, form


def _execute(c: Any, bid: int, form: dict[str, str]) -> Any:
    return post(c, f"/journal/csv/{bid}/execute", **form)


def test_overview_preview_execute_link_and_undo(client, config, api):
    c = client
    bid, _rows = scenario(c, config, api, "extra")
    page_ = c.get(f"/journal/csv/{bid}").text
    assert "Vorschlag: Importprüfung" in page_ and "Ergänzungen verknüpfen (1)" in page_
    assert "Sichere Dubletten verknüpfen" in page_ and 'class="sel-count">2<' in page_  # Vorauswahl: beide sicher
    assert "Gebührenprüfung" in page_ and "Abgangsseite des Transfers" in page_
    html, form = _preview(c, bid)
    assert "2</b><span>verknüpfen (nicht buchen)" in html and "Schutz vor Doppelbuchung" not in html
    before_tx = len(journal(c))
    before_pf = sorted((t.tx_id, str(t.from_qty), str(t.fee_qty)) for t in ctx(c).recorded_portfolio().txs)
    r = _execute(c, bid, form)
    assert r.status_code == 303 and "msg=action" in r.headers["location"]
    # nichts gebucht, vorhandene Buchungen unverändert
    assert len(journal(c)) == before_tx
    assert sorted((t.tx_id, str(t.from_qty), str(t.fee_qty)) for t in ctx(c).recorded_portfolio().txs) == before_pf
    db = ctx(c).db
    w = _w({rc.rec.event_key: rc for rc in csv_service(ctx(c)).rows(bid)})
    assert w.status == "linked" and w.tx_id == "KOI-T"
    link = db.q1("SELECT * FROM tx_link WHERE tx_id='KOI-T' AND status='active'")
    assert link is not None and link["source"] == "sync:bitpanda" and link["role"] == "out"
    rec = __import__("json").loads(link["record_json"])
    assert rec["value_eur"] == "675.47" and rec["fee"] == ["USDT", "11.28465017"] and rec["fee_basis"] == "extra"
    assert rec["ts"].startswith("2024-03-19T09:41")  # Zeitpunkt der Börse bleibt erhalten
    keys = {r["key"] for r in db.q("SELECT key FROM journal_event_alias WHERE tx_id='KOI-T'")}
    assert any(k.startswith("row:sync:bitpanda|") for k in keys) and f"bitpanda:{U(0x701)}" in keys
    # zweites Absenden derselben Vorschau: keine zweite Ausführung
    assert _execute(c, bid, form).status_code == 303
    assert db.scalar("SELECT COUNT(*) FROM import_action") == 1 and db.scalar("SELECT COUNT(*) FROM tx_link") == 2
    # Verlauf und Rückgängig
    assert "Verlauf der Stapelaktionen" in c.get(f"/journal/csv/{bid}").text
    aid = db.scalar("SELECT id FROM import_action")
    r = post(c, f"/journal/csv/{bid}/actions/{aid}/undo")
    assert r.status_code == 303 and "r=2" in r.headers["location"]
    w = _w({rc.rec.event_key: rc for rc in csv_service(ctx(c)).rows(bid)})
    assert w.status == "duplicate" and w.tx_id is None and w.match["cat"] == "ergaenzung"
    assert not db.q("SELECT key FROM journal_event_alias WHERE tx_id='KOI-T'")
    assert db.scalar("SELECT COUNT(*) FROM tx_link WHERE status='active'") == 0
    assert db.scalar("SELECT status FROM import_action WHERE id=?", (aid,)) == "undone"


def test_linked_event_is_recognised_after_refetch(client, config, api):
    c = client
    bid, _rows = scenario(c, config, api, "extra")
    _html, form = _preview(c, bid)
    _execute(c, bid, form)
    sid = ctx(c).db.scalar("SELECT id FROM data_source")
    post(c, f"/settings/datasources/{sid}/refetch")  # vollständig neu abrufen
    for b in ctx(c).db.q("SELECT id FROM csv_batch WHERE id <> ?", (bid,)):
        for rc in csv_service(ctx(c)).rows(int(b["id"])):
            assert rc.status in ("known", "before"), (rc.status, rc.warnings)
    assert not journal(c, "source='sync:bitpanda'")


def test_stale_preview_is_rejected(client, config, api):
    c = client
    bid, _rows = scenario(c, config, api, "extra")
    _html, form = _preview(c, bid)
    post(c, f"/journal/csv/{bid}/select", op="none")  # Auswahl geändert → andere Wirkung
    r = _execute(c, bid, form)
    assert r.status_code == 409 and "nicht mehr aktuell" in r.text
    assert ctx(c).db.scalar("SELECT COUNT(*) FROM import_action") == 0


def test_selection_toggle_page_filtered_and_groups(client, config, api):
    c = client
    bid, rows = scenario(c, config, api, "inside")  # Auszahlung: Widerspruch → nicht in der Vorauswahl
    dep, w = rows[f"bitpanda:{U(0x700)}"], _w(rows)
    from app.csvimport import batch as BA

    sel = BA.selection(ctx(c).db, bid, csv_service(ctx(c)).rows(bid))
    assert sel == {dep.idx}
    r = c.post(f"/journal/csv/{bid}/select", data={"op": "toggle", "idx": str(w.idx), "on": "1"},
               headers={"X-CSRF-Token": c.token, "Accept": "application/json"})
    assert r.status_code == 200 and r.json() == {"selected": 2}
    html, _form = _preview(c, bid)
    assert "einzeln prüfen" in html and "Gebühr unterschiedlich gebucht" in html  # ausgeschlossen mit Grund
    html, _form = _preview(c, bid, "link")
    assert "nur mit „auch abweichende Fälle verknüpfen“" in html
    html, _form = _preview(c, bid, "link", force=True)
    assert "2</b><span>verknüpfen" in html and "723,22821083" in html  # Korrekturvorschlag, nicht ausgeführt
    post(c, f"/journal/csv/{bid}/select", op="filtered", on="0", cat="widerspruch")
    assert BA.selection(ctx(c).db, bid, csv_service(ctx(c)).rows(bid)) == {dep.idx}
    post(c, f"/journal/csv/{bid}/select", op="group", grp="manuell", on="1")
    assert BA.selection(ctx(c).db, bid, csv_service(ctx(c)).rows(bid)) == {dep.idx, w.idx}
    page_ = c.get(f"/journal/csv/{bid}?cat=widerspruch").text
    assert f'value="{w.idx}" data-select-url' in page_ and "Gebühr: Widerspruch" in page_


def test_linked_sources_shown_in_journal_and_survive_full_export(client, config, api, tmp_path):
    import io
    import json
    import zipfile
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from app.main import build_app

    c = client
    bid, _rows = scenario(c, config, api, "extra")
    _html, form = _preview(c, bid)
    _execute(c, bid, form)
    page_ = c.get("/journal?q=KOI-T").text
    assert "+1 Quelle" in page_ and "Weitere Quellen dieses Vorgangs (1)" in page_ and "675,47" in page_
    assert "zusätzlich belastet, belegt" in page_
    body = c.get("/journal/export.zip").content
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        state = json.loads(z.read("portfolia/state.json"))
    links = state["tx_links"]
    assert {x["tx_id"] for x in links} == {"KOI-D", "KOI-T"}
    assert any(a["key"].startswith("row:sync:bitpanda|") for a in state["event_aliases"])
    # Neueinrichtung aus dem Export: Herkunft und Wiedererkennung bleiben
    (tmp_path / "b" / "import").mkdir(parents=True)
    cfg = replace(config, data_dir=tmp_path / "b" / "data", import_dir=tmp_path / "b" / "import")
    dst = cfg.import_dir / "portfolia-export.zip"
    dst.write_bytes(body)
    old = time.time() - 3600
    os.utime(dst, (old, old))
    with TestClient(build_app(cfg, start_scheduler=False)) as c2:
        ctx2 = c2.app.state.ctx
        assert tasks.import_check(ctx2, "test").status == "imported"
        assert ctx2.db.scalar("SELECT COUNT(*) FROM tx_link WHERE status='active'") == 2
        rec = json.loads(ctx2.db.scalar("SELECT record_json FROM tx_link WHERE tx_id='KOI-T'"))
        assert rec["value_eur"] == "675.47" and rec["fee_basis"] == "extra"


# ----------------------------------------------------------------------------------------------------
# Bitpanda: Vollständigkeitsbericht (nachgewiesen / plausibel / nicht verifizierbar)
# ----------------------------------------------------------------------------------------------------

def _eur_deposits() -> list[dict[str, Any]]:
    """Je zwei Einzahlungen Jan, Feb, Apr, Mai 2024 (März ohne Vorgänge), Saldoverlauf mit einem Bruch im Mai."""
    from tests.test_bitpanda import EUR_ID

    ops, n, bal = [], 0, D(0)
    for month in (1, 2, 4, 5):
        for day in (3, 17):
            bal += D(100)
            shown = bal + (D(5) if (month, day) == (5, 17) else D(0))  # Bruch: Saldo passt nicht zum Betrag
            ops.append(oper(20 + n, "deposit", txn(40 + n, "INCOMING", "100", EUR_ID,
                                                    f"2024-{month:02d}-{day:02d}T10:00:00Z", balance=str(shown),
                                                    wallet=U(0xF0E))))
            n += 1
    return ops


def test_completeness_report_proven_plausible_unverifiable(client, config, api):
    c = client
    _usdt(api)
    extra = tx("KOI-X", "2024-03-10T10:00:00Z", "deposit", to=("Bitpanda", "EUR", "250"))
    extra["source"] = "koinly"
    _import(config, [extra], valuation="2024-01-01")
    assert tasks.import_check(ctx(c), "test").status == "imported"
    ops = _eur_deposits()
    api.set_pages(page(ops))
    sid = create_bitpanda(c)
    bid = batch_of(sync(c, sid))
    post(c, f"/journal/csv/{bid}/commit")  # Einzahlungen übernehmen
    assert len(journal(c, "source='sync:bitpanda'")) == 8
    from app.datasources.quality import report
    from app.datasources.service import datasource_service

    ds = datasource_service(ctx(c)).get(sid)
    rep = report(ctx(c), ds)
    assert rep is not None and rep.history and rep.history["events"] == 8
    titles = " | ".join(f.title for f in rep.proven)
    assert "1 Brüche im Saldoverlauf" in titles
    plaus = " | ".join(f.title + " " + " ".join(f.examples) for f in rep.plausible)
    assert "Monat(e) ohne Vorgänge" in plaus and "Mär 2024" in plaus
    assert "KOI-X" in plaus  # Import-Buchung auf dem Bitpanda-Konto ohne API-Gegenstück
    unv = " | ".join(f.title for f in rep.unverifiable)
    assert "Vor dem 03.01.2024" in unv
    assert dict(rep.years)[2024][2] == 0 and dict(rep.years)[2024][0] == 2  # März leer, Januar 2
    # die API liefert einen übernommenen Vorgang nicht mehr → nachgewiesen (Buchung bleibt unverändert)
    api.set_pages(page(ops[1:]))
    post(c, f"/settings/datasources/{sid}/refetch")
    rep = report(ctx(c), datasource_service(ctx(c)).get(sid))
    proven = {f.title: f for f in rep.proven}
    key = next(k for k in proven if "fehlen im letzten vollständigen Abruf" in k)
    assert key.startswith("1 übernommene") and f"bitpanda:{U(0x714)}" in proven[key].examples
    assert len(journal(c, "source='sync:bitpanda' AND status='active'")) == 8
    page_ = c.get(f"/settings/datasources/{sid}").text
    assert "Vollständigkeit der Historie" in page_ and "Nachgewiesene Fehler" in page_
    assert "Plausible Datenlücken" in page_ and "Vorgänge je Monat" in page_


# ----------------------------------------------------------------------------------------------------
# Weitere Regressionsfälle
# ----------------------------------------------------------------------------------------------------

KOINLY_HEAD = ("Date,Type,Label,Sending Wallet,Sent Amount,Sent Currency,Sent Cost Basis,Receiving Wallet,"
               "Received Amount,Received Currency,Received Cost Basis,Fee Amount,Fee Currency,Gain (EUR),"
               "Net Value (EUR),Fee Value (EUR),TxSrc,TxDest,TxHash,Description\n")


def _upload_csv(c: Any, name: str, text: str, account: str) -> int:
    from tests.test_csvimport import upload

    bid, _loc = upload(c, name, text.encode(), account=account)
    post(c, f"/journal/csv/{bid}/options", cutoff="")
    return bid


def test_identical_amounts_with_different_hashes_are_distinct(client, config):
    c = client
    c.get("/settings")
    imp = tx("IMP-E", "2024-04-01T10:00:00Z", "deposit", to=("Ledger", "ETH", "0.5"), value="1500")
    imp["source"], imp["note"] = "koinly", "txhash 0x" + "11" * 32
    _import(config, [imp], valuation="2024-01-01")
    assert tasks.import_check(ctx(c), "test").status == "imported"
    csv = (KOINLY_HEAD
           + "2024-04-01 10:00:00 UTC,deposit,,,,,,Ledger,0.5,ETH,,,,0,1500,0,,,0x" + "22" * 32 + ",\n"
           + "2024-04-01 10:00:00 UTC,deposit,,,,,,Ledger,0.5,ETH,,,,0,1500,0,,,0x" + "11" * 32 + ",\n")
    bid = _upload_csv(c, "koinly.csv", csv, "Ledger")
    rows = sorted(csv_service(ctx(c)).rows(bid), key=lambda rc: rc.line)
    other, same = rows
    # gleicher Betrag, gleiche Zeit, gleiches Konto – aber eine andere Blockchain-Transaktion → eigener Vorgang
    assert other.status == "new" and not other.dup_of and other.match["cat"] == "neu"
    assert same.status == "known" and same.dup_of == ["IMP-E"] and same.match["conf"] == "sicher"


def test_manual_deposit_and_api_deposit_are_never_booked_twice(client, config, api):
    from tests.test_bitpanda import EUR_ID

    c = client
    _import(config, [tx("IMP-0", "2024-01-02T10:00:00Z", "deposit", to=("Bank", "EUR", "10"))], valuation="2024-01-01")
    assert tasks.import_check(ctx(c), "test").status == "imported"  # Assets (u. a. BTC) für die manuelle Buchung
    r = post(c, "/journal/new", kind="deposit", date="2024-04-02", time="12:00", account="Bitpanda", asset="EUR",
             qty="250")
    assert r.status_code == 303
    manual = journal(c, "source='manual'")[0]["tx_id"]
    api.set_pages(page([oper(30, "deposit", txn(60, "INCOMING", "250", EUR_ID, "2024-04-02T15:30:00Z"))]))
    sid = create_bitpanda(c)
    bid = batch_of(sync(c, sid))
    (rc,) = csv_service(ctx(c)).rows(bid)
    # Fiat ist bei „gleicher Menge“ ausgenommen; die Einzahlung ist ein eigener Vorgang, bis entschieden ist
    assert rc.status in ("new", "duplicate")
    from app.csvimport import batch as BA

    if rc.status == "duplicate":
        assert rc.match["cat"] in ("widerspruch", "dublette") and BA.group_of(rc) != "neu"
    # Krypto: manuell erfasste Einzahlung + gleiche Einzahlung aus der API 3 Stunden später
    r = post(c, "/journal/new", kind="deposit", date="2024-04-03", time="09:00", account="Bitpanda", asset="BTC",
             qty="0.01234567", value_eur="750")
    assert r.status_code == 303
    from tests.test_bitpanda import BTC_ID

    api.set_pages(page([oper(31, "deposit", txn(61, "INCOMING", "0.01234567", BTC_ID, "2024-04-03T10:37:00Z"))]))
    post(c, f"/settings/datasources/{sid}/refetch")
    bid2 = ctx(c).db.scalar("SELECT MAX(id) FROM csv_batch")
    from tests.test_csvimport import create_unknown_assets

    create_unknown_assets(c, bid2)
    (btc,) = [x for x in csv_service(ctx(c)).rows(bid2) if x.rec.in_sym == "BTC"]
    assert btc.status == "duplicate" and btc.basis == "same_qty" and not btc.include()
    m = btc.match
    assert m["cat"] == "widerspruch" and m["conf"] in ("mittel", "niedrig") and m["src"]["old_manual"]
    assert BA.group_of(btc) == "manuell"
    # „Übernehmen“ per Stapel schließt die Zeile aus – kein zweiter Zugang
    p = BA.plan(csv_service(ctx(c)), bid2, "include", {btc.idx})
    assert not p.todo and "nur einzeln" in p.items[0].note
    assert manual  # manuelle Buchung bleibt unverändert
    assert journal(c, f"tx_id='{manual}'")[0]["status"] == "active"


def test_wrong_price_mapping_is_a_contradiction(client, config, api):
    c = client
    _usdt(api)
    assets = [a if a["asset_id"] != "USDT" else {**a, "quote_source": "coingecko", "quote_id": "tether"}
              for a in ASSETS_USDT]
    dst = config.import_dir / "imp.zip"
    build_zip(dst, transactions=koinly_rows(), assets=assets, generated_at="2024-03-02T00:00:00Z",
              valuation_date="2024-03-02")
    old = time.time() - 3600
    os.utime(dst, (old, old))
    assert tasks.import_check(ctx(c), "test").status == "imported"
    series = ctx(c).prices.series_for(ctx(c).recorded_portfolio().assets["USDT"])
    ctx(c).db.x("INSERT OR REPLACE INTO price_daily(series, date, close, ccy, source, fetched_at) VALUES "
                "(?, '2024-03-19', 45.0, 'EUR', 'test', '2024-03-20T00:00:00Z')", (series,))
    api.set_pages(page(bitpanda_ops("extra")))
    sid = create_bitpanda(c)
    bid = batch_of(sync(c, sid))
    post(c, f"/journal/csv/{bid}/options", cutoff="")
    w = _w({rc.rec.event_key: rc for rc in csv_service(ctx(c)).rows(bid)})
    m = w.match
    val = next(d for d in m["diff"] if d["f"] == "value")
    assert val["sev"] == "relevant" and "Kurs- bzw. Asset-Zuordnung prüfen" in val["t"]
    assert m["cat"] == "widerspruch" and not w.include()


def test_wrong_asset_mapping_same_qty_and_time_is_flagged(client, config):
    c = client
    c.get("/settings")
    imp = tx("IMP-K", "2024-05-01T10:00:00Z", "deposit", to=("Ledger", "KAS", "1234.5678"), value="150")
    imp["source"] = "koinly"
    _import(config, [imp], valuation="2024-01-01")
    assert tasks.import_check(ctx(c), "test").status == "imported"
    csv = KOINLY_HEAD + "2024-05-01 10:02:00 UTC,deposit,,,,,,Ledger,1234.5678,BNB,,,,0,150,0,,,,\n"
    bid = _upload_csv(c, "koinly.csv", csv, "Ledger")
    (rc,) = csv_service(ctx(c)).rows(bid)
    assert rc.status == "duplicate" and rc.basis == "asset_mismatch" and not rc.include()
    m = rc.match
    assert m["cat"] == "widerspruch" and any("anderes Asset (BNB statt KAS)" in d["t"] for d in m["diff"])


def test_own_wallet_transfer_deposit_side_is_linked_not_booked(client, config, api):
    c = client
    scenario(c, config, api, "extra")
    csv = ("tx_id,datetime,type,tag,from_account,from_asset,from_qty,to_account,to_asset,to_qty,fee_asset,fee_qty,"
           "fee_eur,value_eur,note\n"
           f"HW-1,2024-03-19T09:53:00Z,deposit,,,,,{WALLET},USDT,734.512861,,,,675.93,"
           f"Eingang {HASH}\n")
    b2 = _upload_csv(c, "wallet.csv", csv, WALLET)
    (rc,) = csv_service(ctx(c)).rows(b2)
    assert rc.status == "duplicate" and rc.dup_of == ["KOI-T"] and rc.basis == "transfer_leg"
    m = rc.match
    assert m["role"] == "in" and m["cat"] in ("dublette", "ergaenzung") and m["action"] == "link"
    assert any("Zugangsseite des Transfers" in t for t in m["ok"])
    assert any("Gebühr gehört zur Abgangsseite" in t for t in m["ok"])


def test_transfer_pair_in_one_batch_is_only_booked_together(client, config):
    c = client
    _import(config, [tx("IMP-0", "2024-01-02T10:00:00Z", "deposit", to=("Bank", "EUR", "10"))], valuation="2024-01-01")
    assert tasks.import_check(ctx(c), "test").status == "imported"
    csv = ("tx_id,datetime,type,tag,from_account,from_asset,from_qty,to_account,to_asset,to_qty,fee_asset,fee_qty,"
           "fee_eur,value_eur,note\n"
           "P-1,2024-06-01T10:00:00Z,withdrawal,,Börse A,ETH,1.0,,,,,,,3000,\n"
           "P-2,2024-06-01T10:20:00Z,deposit,,,,,Wallet B,ETH,0.999,,,,2997,\n")
    bid = _upload_csv(c, "own.csv", csv, "Börse A")
    rows = sorted(csv_service(ctx(c)).rows(bid), key=lambda rc: rc.line)
    w, d = rows
    assert w.pair_ref == f"b:{d.idx}" and w.pair_conf == "hoch" and w.match["cat"] == "neu"
    from app.csvimport import batch as BA

    p = BA.plan(csv_service(ctx(c)), bid, "include", {w.idx})
    assert not p.todo and "nur gemeinsam" in p.items[0].note
    p = BA.plan(csv_service(ctx(c)), bid, "include", {w.idx, d.idx})
    assert len(p.todo) == 2
    impact = {(x["account"], x["asset"]): x["delta"] for x in p.impact}
    assert impact[("Börse A", "ETH")] == D("-1.0") and impact[("Wallet B", "ETH")] == D("0.999")
    r = post(c, f"/journal/csv/{bid}/select", op="page", on="1", idx=[str(w.idx), str(d.idx)])
    assert r.status_code == 303
    html, form = _preview(c, bid, "include")
    assert "Wirkung auf die Bestände" in html
    r = _execute(c, bid, form)
    assert r.status_code == 303, r.text[:500]
    assert journal(c, "source='transfer'"), "Transfer zusammengeführt"
    aid = ctx(c).db.scalar("SELECT id FROM import_action")
    r = post(c, f"/journal/csv/{bid}/actions/{aid}/undo")
    assert "t=" in r.headers["location"]
    assert not journal(c, "status='active' AND source <> 'transfer' AND batch_id IS NOT NULL")
    assert not journal(c, "source='transfer' AND status='active'")


def test_link_to_vanished_booking_reopens_the_row(client, config, api):
    """Verschwindet die verknüpfte Buchung (neuer kuratierter Import ohne sie), erscheint die Zeile wieder zur
    Prüfung – eine Verknüpfung ohne Ziel darf keinen Vorgang verdecken."""
    c = client
    bid, _rows = scenario(c, config, api, "extra")
    _html, form = _preview(c, bid)
    _execute(c, bid, form)
    dep = koinly_rows()[0]
    dst = config.import_dir / "imp2.zip"
    build_zip(dst, transactions=[dep], assets=ASSETS_USDT, generated_at="2024-03-03T00:00:00Z",
              valuation_date="2024-03-02")
    old = time.time() - 3600
    os.utime(dst, (old, old))
    assert tasks.import_check(ctx(c), "test").status == "imported"  # KOI-T fehlt im neuen Import
    rows = {rc.rec.event_key: rc for rc in csv_service(ctx(c)).rows(bid)}
    csv_service(ctx(c)).evaluate(bid)
    w = _w({rc.rec.event_key: rc for rc in csv_service(ctx(c)).rows(bid)})
    assert w.status != "linked" and w.tx_id is None and w.open
    assert any("Verknüpfung aufgehoben: Buchung KOI-T" in x for x in w.warnings)
    assert rows[f"bitpanda:{U(0x700)}"].status == "linked"  # KOI-D gibt es weiterhin
    assert ctx(c).db.scalar("SELECT status FROM tx_link WHERE tx_id='KOI-T'") == "undone"


def test_partial_batch_include_keeps_csv_batch_open(client, config):
    c = client
    _import(config, [tx("IMP-0", "2024-01-02T10:00:00Z", "deposit", to=("Bank", "EUR", "10"))], valuation="2024-01-01")
    assert tasks.import_check(ctx(c), "test").status == "imported"
    csv = ("tx_id,datetime,type,tag,from_account,from_asset,from_qty,to_account,to_asset,to_qty,fee_asset,fee_qty,"
           "fee_eur,value_eur,note\n"
           "N-1,2024-06-01T10:00:00Z,deposit,,,,,Wallet B,ETH,0.25,,,,750,\n"
           "N-2,2024-06-02T10:00:00Z,deposit,,,,,Wallet B,ETH,0.5,,,,1500,\n")
    bid = _upload_csv(c, "two.csv", csv, "Wallet B")
    first = sorted(csv_service(ctx(c)).rows(bid), key=lambda rc: rc.line)[0]
    post(c, f"/journal/csv/{bid}/select", op="page", on="1", idx=str(first.idx))
    _html, form = _preview(c, bid, "include")
    assert _execute(c, bid, form).status_code == 303
    assert csv_service(ctx(c)).batch(bid)["status"] == "partial"  # die zweite Zeile wartet weiter
    assert "1 Buchung übernehmen" in c.get(f"/journal/csv/{bid}").text


def test_two_equal_new_rows_never_both_linked_to_one_booking(client, config):
    """AP3-Regression: zwei gleich große Vorgänge kurz nacheinander (ohne Kennung/Hash), einer davon bereits
    gebucht – beide Zeilen passten unscharf auf dieselbe Buchung und wurden je als „Dublette, hoch“ zum Verknüpfen
    vorgeschlagen; ein echter Vorgang wäre verloren gegangen."""
    c = client
    c.get("/settings")
    _import(config, [tx("IMP-D", "2024-04-01T10:00:00Z", "deposit", to=("Ledger", "ETH", "0.4321"), value="1234")],
            valuation="2024-01-01")
    assert tasks.import_check(ctx(c), "test").status == "imported"
    csv = (KOINLY_HEAD
           + "2024-04-01 10:00:00 UTC,deposit,,,,,,Ledger,0.4321,ETH,,,,0,1234,0,,,,\n"
           + "2024-04-01 10:00:40 UTC,deposit,,,,,,Ledger,0.4321,ETH,,,,0,1234,0,,,,\n")
    bid = _upload_csv(c, "koinly.csv", csv, "Ledger")
    rows = sorted(csv_service(ctx(c)).rows(bid), key=lambda rc: rc.line)
    assert all(rc.dup_of == ["IMP-D"] for rc in rows)  # beide passen unscharf auf dieselbe Buchung
    actions = [rc.match["action"] for rc in rows]
    assert actions.count("link") == 0 and all(a == "review" for a in actions)
    assert all(rc.match["conf"] not in ("sicher", "hoch") for rc in rows)  # nicht für Stapelaktionen vorausgewählt
    assert any("weitere Zeile" in d["t"] for d in rows[0].match["diff"])
