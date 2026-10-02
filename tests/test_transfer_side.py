"""Zu- bzw. Abgang ↔ Seite eines erfassten Transfers – verzögerte Auszahlung, anderer Kontoname.

Regressionsfall (Vorgabe des Auftraggebers, strukturgleich mit anderen Zahlen – keine echten Buchungsdaten): Der
kuratierte Import führt eine Börsen-Auszahlung als Transfer „Börse → Bitcoin (BTC)“ (Wallet-Name des Steuertools,
ohne Hash, Notiz mit dem Zeitpunkt der Gutschrift). Die Wallet-Datenquelle „Ledger BTC“ liefert denselben Vorgang
als Zugang – 27,5 h später, weil die Börse verzögert ausgezahlt hat, und unter ihrem eigenen Kontonamen. Bisher
wurde der Zugang als neu übernommen (bei automatischer Übernahme still) und die Menge zählte doppelt.

Abgedeckt: Regeln des Abgleichs (Zeitfenster, exakte/unverwechselbare Menge, Konto, Hash, Datenquelle, Fiat,
Vergabe je Seite, Beleg aus der Notiz), Prüf-Stapel einer Wallet-Datenquelle mit automatischer Übernahme,
Verknüpfen, bereits gebuchte App-Buchung (Buchungsliste, Abgleich mit dem Import, Datenqualität mit Vorschau,
Übernehmen und Rückgängig).
"""

from __future__ import annotations

import os
import re
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from app.csvimport import transfer_side as TS
from app.csvimport.service import csv_service
from app.datasources import chainhttp as CH
from app.datasources.chains.btckeys import Account
from app.datasources.service import datasource_service
from app.datasources.wallet import WalletConnector
from app.importer.zipbuilder import build_zip
from app.jobs import tasks
from app.ledger.models import Tx
from tests.helpers import ASSETS, tx
from tests.wallet_fakes import MASTER, FakeEsplora, all_rows, create_wallet, ctx, make_client, post

D = Decimal
ZPUB = ("zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868RvUUkgDKf31mGDtKsAYz2oz2AGut"
        "ZYs")  # öffentlicher BIP84-Testvektor (Mnemonic „abandon … about“), keine echten Mittel
R0 = Account(ZPUB).addr(0, 0)
EXT = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"  # BIP173-Beispieladresse (fremd)
QTY = "0.27182818"
SENT = datetime(2022, 12, 15, 8, 20, tzinfo=UTC)  # Auszahlung laut Börse (Zeitpunkt des Transfers im Import)
RECV = SENT + timedelta(hours=27, minutes=30)  # Gutschrift im Wallet (verzögerte Auszahlung)
WALLET_IMPORT = "Bitcoin (BTC)"  # Wallet-Name im Steuertool
WALLET_DS = "Ledger BTC"  # dasselbe Wallet in der Datenquelle
ASSETS_BTC = [a if a["asset_id"] != "BTC" else {**a, "quote_source": "none", "quote_id": ""} for a in ASSETS]


# ----------------------------------------------------------------------------------------------------
# Regeln (ohne App)
# ----------------------------------------------------------------------------------------------------

def _t(tx_id: str, ts: datetime, frm: str, to: str, qty: str, *, fee: str | None = None, note: str | None = None,
       asset: str = "BTC", origin: str = "import", source: str | None = "boerse") -> Tx:
    q = D(qty)
    return Tx(seq=0, tx_id=tx_id, ts=ts, date=ts.date(), date_only=False, type="transfer", tag=None,
              from_account=frm, from_asset=asset, from_qty=q, to_account=to, to_asset=asset, to_qty=q,
              fee_asset=asset if fee else None, fee_qty=D(fee) if fee else None, fee_eur=None, value_eur=None,
              source=source, note=note, origin=origin)


def _index(*txs: Tx, managed: tuple[str, ...] = (), hashes: dict[str, set[str]] | None = None) -> TS.TransferIndex:
    return TS.TransferIndex(txs, lambda t: (hashes or {}).get(t.tx_id, set()), managed)


def _in(acc: str, qty: str, ts: datetime, h: str | None = None, asset: str = "BTC") -> TS.Probe:
    return TS.Probe("in", acc, asset, D(qty), ts, None, h)


def test_rules_delayed_deposit_under_other_account_name():
    note = f"Zugang in Steuertool {WALLET_IMPORT} am {RECV.strftime('%Y-%m-%dT%H:%M:%SZ')} (27.5 h später)"
    idx = _index(_t("IMP-T", SENT, "Börse", WALLET_IMPORT, QTY, note=note))
    hit = idx.find(_in(WALLET_DS, QTY, RECV))
    assert hit is not None and hit.basis == "transfer_leg_acc" and hit.role == "in" and hit.exact
    assert hit.account == WALLET_IMPORT and hit.delayed and hit.note_time == RECV
    text = hit.text(WALLET_DS)
    assert "Zugangsseite des Transfers IMP-T (Börse → Bitcoin (BTC))" in text and "27,5 h nach dem Transfer" in text
    assert "Konto „Ledger BTC“ statt „Bitcoin (BTC)“" in text and "Notiz" in text
    # gleiche Seite nicht zweimal
    idx.claim(hit)
    assert idx.find(_in(WALLET_DS, QTY, RECV)) is None


def test_rules_time_window_and_quantity():
    idx = _index(_t("IMP-T", SENT, "Börse", WALLET_IMPORT, QTY), _t("IMP-R", SENT, "Börse", WALLET_IMPORT, "0.5"))
    # unverwechselbare, exakte Menge: bis 7 Tage später; nicht 8 Tage, nicht 3 h vorher
    assert idx.find(_in(WALLET_DS, QTY, SENT + timedelta(days=5))) is not None
    assert idx.find(_in(WALLET_DS, QTY, SENT + timedelta(days=8))) is None
    assert idx.find(_in(WALLET_DS, QTY, SENT - timedelta(hours=3))) is None
    assert idx.find(_in(WALLET_DS, QTY, SENT - timedelta(hours=1))) is not None
    # runde Menge: nur im üblichen Fenster (72 h)
    assert idx.find(_in(WALLET_DS, "0.5", SENT + timedelta(hours=30))) is not None
    assert idx.find(_in(WALLET_DS, "0.5", SENT + timedelta(days=4))) is None
    # anderes Konto: nur exakt gleiche Menge; gleiches Konto: ± 0,5 % wie die Dublettenprüfung
    near = str(D(QTY) * D("0.997"))
    assert idx.find(_in(WALLET_DS, near, RECV)) is None
    hit = idx.find(_in(WALLET_IMPORT, near, RECV))
    assert hit is not None and hit.same_account and not hit.exact


def test_rules_exclusions():
    t = _t("IMP-T", SENT, "Börse", WALLET_IMPORT, QTY)
    # verschiedene Blockchain-Transaktionen
    idx = _index(t, hashes={"IMP-T": {"aa" * 32}})
    assert idx.find(_in(WALLET_DS, QTY, RECV, h="bb" * 32)) is None
    assert idx.find(_in(WALLET_DS, QTY, RECV, h="aa" * 32)) is not None
    # Zielkonto des Transfers mit eigener Datenquelle: der Vorgang wäre dort zu erwarten
    assert _index(t, managed=(WALLET_IMPORT,)).find(_in(WALLET_DS, QTY, RECV)) is None
    # Zugang auf dem Absenderkonto ist keine Zugangsseite; anderes Asset; Fiat unter anderem Konto
    assert _index(t).find(_in("Börse", QTY, RECV)) is None
    assert _index(t).find(_in(WALLET_DS, QTY, RECV, asset="ETH")) is None
    eur = _t("IMP-E", SENT, "Bank", "Börse", "1234.56", asset="EUR")
    assert _index(eur).find(_in("Börse (API)", "1234.56", RECV, asset="EUR")) is None
    assert _index(eur).find(_in("Börse", "1234.56", RECV, asset="EUR")) is not None
    # von Portfolia gebildete Transfers (PF-T) zählen nicht
    pft = _t("PF-T-000001", SENT, "Börse", WALLET_IMPORT, QTY, origin="journal", source="transfer")
    assert _index(pft).find(_in(WALLET_DS, QTY, RECV)) is None


def test_rules_withdrawal_side_and_preference():
    t = _t("IMP-W", SENT, WALLET_IMPORT, "Börse", "0.1", fee="0.0001")
    idx = _index(t)
    # Abgang der Datenquelle: ± 2 h, Gebühr anders dargestellt (Gesamtabgang gleich)
    p = TS.Probe("out", WALLET_DS, "BTC", D("0.0999"), SENT + timedelta(minutes=40), D("0.0002"))
    hit = idx.find(p)
    assert hit is not None and hit.role == "out" and hit.basis == "transfer_leg_acc"
    assert idx.find(TS.Probe("out", WALLET_DS, "BTC", D("0.1"), SENT + timedelta(hours=3))) is None
    # gleiches Konto vor anderem Konto
    a = _t("IMP-A", SENT, "Börse", WALLET_IMPORT, QTY)
    b = _t("IMP-B", SENT + timedelta(hours=20), "Börse", WALLET_DS, QTY)
    hit = _index(a, b).find(_in(WALLET_DS, QTY, RECV))
    assert hit is not None and hit.tx.tx_id == "IMP-B" and hit.same_account


# ----------------------------------------------------------------------------------------------------
# App: Wallet-Datenquelle mit automatischer Übernahme
# ----------------------------------------------------------------------------------------------------

def _btc_tx(n: int, height: int, when: datetime, sats: int) -> dict:
    return {"txid": f"{n:064x}", "version": 2, "locktime": 0, "fee": 1_000,
            "status": {"confirmed": True, "block_height": height, "block_hash": "00" * 32,
                       "block_time": int(when.timestamp())},
            "vin": [{"txid": f"{10_000 + n:064x}", "vout": 0, "is_coinbase": False,
                     "prevout": {"scriptpubkey_address": EXT, "value": sats + 51_000}}],
            "vout": [{"scriptpubkey_address": R0, "value": sats}, {"scriptpubkey_address": EXT, "value": 50_000}]}


@pytest.fixture
def esplora(monkeypatch):
    fake = FakeEsplora([_btc_tx(1, 770_000, RECV, int(D(QTY) * 100_000_000))], tip=770_100)
    monkeypatch.setattr(WalletConnector, "transport", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(WalletConnector, "sleep", staticmethod(lambda _s: None))
    monkeypatch.setattr(CH, "_LIMITERS", {})
    return fake


@pytest.fixture
def client(config, monkeypatch):
    monkeypatch.setenv("PORTFOLIA_MASTER_KEY", MASTER)
    with make_client(config) as c:
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        yield c


def _import(config, valuation: str = "2022-12-15") -> None:
    note = (f"Zugang in Steuertool {WALLET_IMPORT} am {RECV.strftime('%Y-%m-%dT%H:%M:%SZ')} (27.5 h später), "
            "Steuertool-ID 0F1E2D3C4B5A69788796A5B4C3D2E1F0")
    dep = tx("IMP-D", "2022-12-01T09:00:00Z", "deposit", to=("Börse", "BTC", "1"), value="16000")
    trf = tx("IMP-T", SENT.strftime("%Y-%m-%dT%H:%M:%SZ"), "transfer", frm=("Börse", "BTC", QTY),
             to=(WALLET_IMPORT, "BTC", QTY), value="4350.00")
    trf["source"], trf["source_ref"], trf["note"] = "boerse", "auszahlung-4711", note
    dst = config.import_dir / "imp.zip"
    build_zip(dst, transactions=[dep, trf], assets=ASSETS_BTC, generated_at="2022-12-16T00:00:00Z",
              valuation_date=valuation, manual_prices=[{"asset_id": "BTC", "date": "2022-12-16", "price_eur": "16000",
                                                        "note": "Testkurs"}])
    old = time.time() - 3600
    os.utime(dst, (old, old))


def _btc(c) -> dict[str, Decimal]:
    led = ctx(c).ledger()
    return {acc: q for (acc, aid), q in led.balances.items() if aid == "BTC" and q}


def _wallet_row(c, sid):
    (rc,) = [v for v in all_rows(c, sid).values() if v.rec.kind == "deposit"]
    return rc


def test_delayed_wallet_deposit_is_not_booked_twice(client, config, esplora):
    c = client
    _import(config)
    assert tasks.import_check(ctx(c), "test").status == "imported"
    sid = create_wallet(c, "bitcoin", ZPUB, name=WALLET_DS, script="p2wpkh", auto_commit="1")
    res = datasource_service(ctx(c)).sync(sid, "manual")
    assert res["status"] == "synced", res
    rc = _wallet_row(c, sid)
    # erkannt als Zugangsseite des Import-Transfers – trotz 27,5 h Verzögerung und anderem Kontonamen
    assert rc.status == "duplicate" and rc.basis == "transfer_leg_acc" and rc.dup_of == ["IMP-T"]
    assert not rc.include() and res["committed"] == 0
    assert "Zugangsseite des Transfers IMP-T" in rc.warnings[0] and "Konto „Ledger BTC“ statt" in rc.warnings[0]
    m = rc.match
    assert m["cat"] == "widerspruch" and m["conf"] == "mittel" and m["role"] == "in" and m["action"] == "review"
    assert any("Konto Ledger BTC statt Bitcoin (BTC)" in d["t"] and d["sev"] == "relevant" for d in m["diff"])
    ts = next(d for d in m["diff"] if d["f"] == "ts")
    assert ts["sev"] == "minor" and "verzögerte Auszahlung" in ts["t"]
    assert any("laut Notiz von IMP-T" in t for t in m["ok"])
    assert any("Konten angleichen" in f and "„Bitcoin (BTC)“" in f for f in m["fix"])
    assert any("verknüpfen" in f for f in m["fix"])
    # zweiter Lauf: offene Stapel werden erneut automatisch übernommen, soweit eindeutig – dieser Zugang nicht
    datasource_service(ctx(c)).sync(sid, "manual")
    assert not ctx(c).db.q("SELECT tx_id FROM journal_tx WHERE status='active'")
    assert _btc(c) == {"Börse": D("1") - D(QTY), WALLET_IMPORT: D(QTY)}
    # verknüpfen: nichts wird gebucht, die Angaben der Datenquelle bleiben am Transfer
    bid = int(ctx(c).db.scalar("SELECT batch_id FROM csv_row WHERE id=?", (rc.id,)))
    r = post(c, f"/journal/csv/{bid}/row-action", row_action=f"link:{rc.idx}")
    assert r.status_code == 303, r.text[:800]
    (rc2,) = [v for v in csv_service(ctx(c)).rows(bid) if v.idx == rc.idx]
    assert rc2.status == "linked" and rc2.tx_id == "IMP-T"
    assert _btc(c) == {"Börse": D("1") - D(QTY), WALLET_IMPORT: D(QTY)}


def test_already_booked_wallet_deposit_is_found_and_resolved(client, config, esplora):
    """Zustand vor der Korrektur: Der Zugang wurde bereits gebucht (z. B. automatisch übernommen). Er wird in der
    Buchungsliste, im Abgleich mit dem Import und in der Datenqualität als Transferseite erkannt; entschieden wird
    nur auf Wunsch – mit Vorschau und Rückgängig."""
    c = client
    _import(config)
    assert tasks.import_check(ctx(c), "test").status == "imported"
    sid = create_wallet(c, "bitcoin", ZPUB, name=WALLET_DS, script="p2wpkh")
    datasource_service(ctx(c)).sync(sid, "manual")
    rc = _wallet_row(c, sid)
    bid = int(ctx(c).db.scalar("SELECT batch_id FROM csv_row WHERE id=?", (rc.id,)))
    r = post(c, f"/journal/csv/{bid}/row-action", row_action=f"include:{rc.idx}")  # ausdrücklich gebucht
    assert r.status_code == 303, r.text[:800]
    jid = ctx(c).db.scalar("SELECT tx_id FROM journal_tx WHERE status='active' AND type='deposit'")
    assert jid and jid.startswith("PF-S-")
    assert _btc(c) == {"Börse": D("1") - D(QTY), WALLET_IMPORT: D(QTY), WALLET_DS: D(QTY)}  # doppelt gezählt
    # Buchungsliste: Hinweis, Vergleich, Entscheidung
    html = c.get("/journal").text
    assert "Transferseite?" in html and f'id="cmp-{jid}"' in html and "Import-Transfer gilt" in html
    assert "Zugangsseite des Transfers IMP-T" in html
    # Abgleich mit dem Import
    html = c.get("/journal/abgleich").text
    assert jid in html and "IMP-T" in html and "Transferseite" in html and "Konten angleichen" in html
    # Datenqualität: Befund mit Empfehlung „im Import enthalten“ – ohne „Import ausblenden“ (entfernte die Auszahlung)
    from app.diagnosis import actions as A
    from app.diagnosis.engine import report_for
    from app.diagnosis.recommend import recommend

    rep = report_for(ctx(c))
    (f,) = [f for f in rep.findings if f.key == f"tside|{jid}|IMP-T"]
    assert f.kind == "duplicate" and f.status == "wahrscheinlich" and f.data["side"]["same"] is False
    rec = recommend(rep, f)
    assert [o.key for o in rec.options] == ["cover", "hide_custom", "dismiss"] and rec.conditional
    assert "Konten angleichen" in rec.text
    plan = A.build_plan(ctx(c), rep, f, "cover")
    assert not plan.errors and [op.kind for op in plan.ops] == ["cover"]
    eff = A.preview(ctx(c), rep, plan)
    assert any(p.account == WALLET_DS and p.asset == "BTC" and p.delta == -D(QTY) for p in eff.positions)
    res = A.apply(ctx(c), f.id, "cover", plan.params, plan.token)
    assert res.ok, res.errors
    assert _btc(c) == {"Börse": D("1") - D(QTY), WALLET_IMPORT: D(QTY)}
    assert "Transferseite?" not in c.get("/journal").text
    assert report_for(ctx(c)).by_id(f.id) is None
    # Rückgängig: die App-Buchung zählt wieder, der Befund ist wieder da
    assert A.undo(ctx(c), res.decision_id).ok
    assert _btc(c)[WALLET_DS] == D(QTY)
    assert report_for(ctx(c)).by_id(f.id) is not None


def test_journal_decision_distinct_removes_hint(client, config, esplora):
    c = client
    _import(config)
    assert tasks.import_check(ctx(c), "test").status == "imported"
    sid = create_wallet(c, "bitcoin", ZPUB, name=WALLET_DS, script="p2wpkh")
    datasource_service(ctx(c)).sync(sid, "manual")
    rc = _wallet_row(c, sid)
    bid = int(ctx(c).db.scalar("SELECT batch_id FROM csv_row WHERE id=?", (rc.id,)))
    post(c, f"/journal/csv/{bid}/row-action", row_action=f"include:{rc.idx}")
    jid = ctx(c).db.scalar("SELECT tx_id FROM journal_tx WHERE status='active' AND type='deposit'")
    r = post(c, "/journal/abgleich", journal_tx_id=jid, import_tx_id="IMP-T", decision="distinct")
    assert r.status_code == 303
    assert "Transferseite?" not in c.get("/journal").text
    assert re.search(r"Zur Entscheidung <span class=\"badge[^\"]*\">0<", c.get("/journal/abgleich").text)
    # „Import-Transfer gilt“ direkt aus der Buchungsliste (nach Aufheben der Entscheidung)
    post(c, "/journal/abgleich", journal_tx_id=jid, import_tx_id="IMP-T", decision="undo")
    assert "Transferseite?" in c.get("/journal").text
    post(c, "/journal/abgleich", journal_tx_id=jid, import_tx_id="IMP-T", decision="covered")
    assert _btc(c) == {"Börse": D("1") - D(QTY), WALLET_IMPORT: D(QTY)}


def test_account_hint_from_transfer_side_is_suggestion_only(client, config, esplora):
    """Transferseiten unter anderem Kontonamen sind ein Beleg für dasselbe Wallet – nur als Vorschlag (eine Umstellung
    per Klick, zurücknehmbar), nie als automatische Umstellung."""
    c = client
    _import(config)
    assert tasks.import_check(ctx(c), "test").status == "imported"
    sid = create_wallet(c, "bitcoin", ZPUB, name=WALLET_DS, script="p2wpkh")
    svc = datasource_service(ctx(c))
    svc.sync(sid, "manual")
    ds = svc.get(sid)
    assert ds.account == WALLET_DS and svc.account_switch(sid) is None  # keine automatische Umstellung
    sug = svc.account_suggestion(ds)
    assert sug is not None and sug["account"] == WALLET_IMPORT and sug["sides"] == 1 and not sug["auto"]
    html = c.get(f"/settings/datasources/{sid}").text
    assert "Konto laut Abgleich" in html and "als Seite eines Transfers" in html
    rc = _wallet_row(c, sid)
    bid = int(ctx(c).db.scalar("SELECT batch_id FROM csv_row WHERE id=?", (rc.id,)))
    assert "davon 1 als Seite eines Transfers" in c.get(f"/journal/csv/{bid}").text
    # nach „verknüpfen“ bleibt der Beleg erhalten
    post(c, f"/journal/csv/{bid}/row-action", row_action=f"link:{rc.idx}")
    assert svc.account_suggestion(svc.get(sid))["account"] == WALLET_IMPORT
    # Umstellung per Klick und zurück – Buchungen bleiben unberührt
    r = post(c, f"/settings/datasources/{sid}/account-adopt", account=WALLET_IMPORT)
    assert r.status_code == 303
    assert svc.get(sid).account == WALLET_IMPORT and svc.account_suggestion(svc.get(sid)) is None
    assert "Rückgängig" in c.get(f"/settings/datasources/{sid}").text
    post(c, f"/settings/datasources/{sid}/account-undo")
    assert svc.get(sid).account == WALLET_DS
    assert not ctx(c).db.q("SELECT 1 FROM journal_tx")
