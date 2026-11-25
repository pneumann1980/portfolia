"""0.122 – Neue API-Vorgänge ohne unnötige Rückfrage: Bitpanda merkt sich den letzten belegten Saldo je Wallet, damit
die Gebühr eines einzeln eintreffenden Vorgangs (z. B. Reward) belegbar bleibt."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from app.datasources import bitpanda as B
from tests import test_bitpanda as TB
from tests.test_bitpanda import BTC_ID, ev_of, fetch, oper, page, txn

api = TB.api  # Fixture der Bitpanda-Tests (nachgebildete API)

NOW = datetime.now(UTC)
OLD = (NOW - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
NEW = (NOW + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")  # nach dem ersten Abruf gutgeschrieben


def _first(api):
    api.set_pages(page([oper(2, "passive_earn_reward", txn(2, "INCOMING", "1", BTC_ID, OLD,
                                                           ttype="passive_earn_reward", balance="11.53033632"))]))
    res = fetch()
    assert res.complete and res.cursor["bal"]
    return res.cursor


def _second(api, cursor, balance):
    api.set_pages(page([
        oper(2, "passive_earn_reward",  # liegt vor „from“ des Folgeabrufs → nicht im Abruf
             txn(2, "INCOMING", "1", BTC_ID, OLD, ttype="passive_earn_reward", balance="11.53033632")),
        oper(1, "passive_earn_reward", txn(1, "INCOMING", "0.96966368", BTC_ID, NEW, ttype="passive_earn_reward",
                                           fee="0.19393274", balance=balance))]))
    res = fetch(cursor)
    assert len(res.events) == 1  # der alte Reward liegt vor dem Beginn des Folgeabrufs
    (r,) = ev_of(res, 1)
    return r, res


def test_single_reward_fee_proven_with_balance_from_previous_fetch(api):
    cursor = _first(api)
    r, res = _second(api, cursor, "12.5")  # Saldo +0,9697 = Betrag → Gebühr steckt im Betrag
    assert r.review is None and r.fee_qty is None
    assert res.coverage["diagnostics"]["counts"]["Saldoverlauf: Vorgänger aus dem vorigen Abruf"] == 1
    r, _ = _second(api, cursor, "12.30606726")  # +0,7757 = Betrag − Gebühr → zusätzlich abgezogen
    assert r.review is None and r.fee_qty == D("0.19393274")


def test_chain_break_still_needs_review(api):
    cursor = _first(api)
    r, res = _second(api, cursor, "13.1")  # passt zu keiner Lesart → Vorgang fehlt dazwischen o. Ä.
    assert r.review and "nicht belegbar" in r.review
    assert res.coverage["diagnostics"]["counts"]["Saldoverlauf: Brüche"] == 1


def test_without_stored_balance_behaviour_unchanged(api):
    cursor = _first(api)
    r, _ = _second(api, {k: v for k, v in cursor.items() if k != "bal"}, "12.5")
    assert r.review and "nicht belegbar" in r.review  # alter Abrufstand ohne Salden: wie bisher prüfen


def test_cursor_balances_roundtrip_and_ignore_garbage():
    ts = datetime(2026, 11, 24, 8, 0, 0, 500000, tzinfo=UTC)
    last = {("w1", "a1"): (ts, D("11.53033632"))}
    raw = B._seed_to(last)
    assert B._seed_from({"bal": raw}) == last
    assert B._seed_from({"bal": {"kaputt": ["x"], "w|a": ["kein Datum", "1"], "w2|a2": [ts.isoformat(), "x"]}}) == {}
    assert B._seed_from({"bal": "nein"}) == {} and B._seed_from(None) == {}


# ---- Karte „Soll dieser Vorgang gebucht werden?“ mit Prüfhinweis der Quelle -------------------------------------

def test_card_shows_source_review_and_recommends_nothing(cfg_stack):
    import json

    from app.csvimport.service import csv_service
    from app.diagnosis import guide as G
    from tests.test_assistant_stack import text_of

    c, bid = cfg_stack
    ctx = c.app.state.ctx
    note = "Gebühr 0.19393274 VSN: ob der Betrag sie bereits enthält, ist nicht dokumentiert"
    for r in ctx.db.q("SELECT batch_id, idx, rec_json FROM csv_row WHERE batch_id=?", (bid,)):
        rec = json.loads(r["rec_json"])
        if "anderer Vorgang" in json.dumps(rec):
            rec["review"] = note
            ctx.db.x("UPDATE csv_row SET rec_json=? WHERE batch_id=? AND idx=?", (json.dumps(rec), bid, r["idx"]))
    csv_service(ctx).evaluate(bid)
    task = next(t for t in G.tasks(ctx) if t.kind == "zeile" and t.item and "1,5" in " ".join(
        G.detail(ctx, t).facts))
    d = G.detail(ctx, task)
    assert d.question == "Soll dieser Vorgang gebucht werden?" and f"Hinweis der Quelle: {note}" in d.context
    assert not any(a.recommended for a in d.answers)
    assert [a.id for a in d.answers][:2] == ["row:include", "go:open"]
    skip = ",".join(t.kid for t in G.tasks(ctx) if t.kid != task.kid)
    page = text_of(c.get(f"/quality/assistent?new=1&skip={skip}").text)
    assert "Hinweis der Quelle" in page and "empfohlen" not in page


@pytest.fixture
def cfg_stack(tmp_path):
    from app.config import Config, Secrets
    from tests.test_assistant_stack import stack as _stack

    imp = tmp_path / "import"
    imp.mkdir(parents=True, exist_ok=True)
    cfg = Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                 startup_jobs=False, log_format="text", secrets=Secrets())
    gen = _stack.__wrapped__(cfg)
    yield next(gen)
    gen.close()


# ---- Bindung: Schätzwert aus dem aktuellen Kurs blockiert die Antwort nicht --------------------------------------

def _rc(**kw):
    from types import SimpleNamespace

    base = {"status": "new", "decision": None, "value_in": None, "fee_in": None, "pair_ref": None,
            "pair_conf": None, "pair_ok": None, "tx_id": None,
            "row": {"to_qty": "0.96966368", "value_eur": "1.23", "fee_eur": "0.25", "fee_qty": "0.19"},
            "pair_alts": [], "counterpart": None, "pair_rev": None, "value_src": "aktueller Kurs × Menge",
            "fee_src": "Kurs des Vorgangs"}
    base.update(kw)
    return SimpleNamespace(**base)


def test_live_price_estimate_does_not_change_binding():
    from app.csvimport.batch import row_state

    a = _rc()
    b = _rc(row={**a.row, "value_eur": "1.31", "fee_eur": "0.27"})  # nächster Kursabruf
    assert row_state(a) == row_state(b)
    c = _rc(row={**a.row, "to_qty": "1.0"})  # Menge geändert → gebunden
    assert row_state(a) != row_state(c)
    d = _rc(value_src="Schlusskurs 24.11.2026 × Menge", fee_src="Kurs des Vorgangs")
    e = _rc(value_src="Schlusskurs 24.11.2026 × Menge", fee_src="Kurs des Vorgangs",
            row={**a.row, "value_eur": "1.31"})
    assert row_state(d) != row_state(e)  # Tageskurs: Wert bleibt gebunden
