"""0.96.0: Nachprüfung 0.95.0 – ergänzende Repository-Nachweise zu den externen Gegenproben (``tests/external_096``):
N29 (Quellbelegung und Kennung im normalen Vorschlag), N28 (technische Identität der Datenquellen bei der
Wiederherstellung), N26 (Lesestand und prozessweite Caches), N27 (Ausgleichsregel überlappender abgelehnter
Einzelaktionen), N21-Rest (Abhängigkeiten aus den Lesepfaden) und historische Aktionen ohne Besitzkennung."""

# ruff: noqa: F811  (Fixtures werden importiert und als Argumente verwendet)
from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import fullexport as FE
from app.accounts import service as ACC
from app.csvimport import assess as AS
from app.csvimport import batch as BA
from app.csvimport.service import BOOKING_TABLES, NON_BOOKING_REVS, csv_service
from app.datasources.service import datasource_service
from app.db import REV_TABLES
from app.diagnosis import actions as DA
from app.jobs import tasks
from app.main import build_app
from tests.helpers import ASSETS, tx
from tests.test_bitpanda import ctx
from tests.test_diagnosis import PF_HEADER, client_with_import, upload
from tests.test_full_export import _client, _other, _place
from tests.test_review_090 import _edit_target
from tests.test_review_091 import _duplicate, _journal_transfer
from tests.test_review_092 import _run_editor, _two_rows
from tests.test_review_fixes import c, cfg, form_values, post  # noqa: F401

LINE = ",2025-01-01T10:00:00Z,deposit,,,,,Wallet K,KAS,10,,,,1,\n"


def _execute(client, batch, action):
    cx = client.app.state.ctx
    svc = csv_service(cx)
    BA.set_selection(cx.db, batch, {r.idx for r in svc.rows(batch) if r.open})
    preview = client.get(f"/journal/csv/{batch}/preview?action={action}")
    assert preview.status_code == 200
    return post(client, f"/journal/csv/{batch}/execute", form_values(preview.text, f"/journal/csv/{batch}/execute"))


def _equal_deposits(cfg, n):
    rows = [tx(f"D{i:02d}", "2025-01-01T10:00:00Z", "deposit", to=("Wallet K", "KAS", "10"), value="1")
            for i in range(n)]
    return client_with_import(cfg, rows, ASSETS)


# -- N29: Quellbelegung, Kennung, viele gleich große Kandidaten ------------------------------------------------------

@pytest.mark.parametrize("n", [6, 12])  # über den früheren Kürzungen ``[:5]`` bzw. Anzeigegrenzen
def test_replay_with_many_equal_candidates_books_nothing(cfg, n):
    """n gleich große Importbuchungen, jede schon mit einem Datensatz anderer Darstellung verknüpft: der unveränderte
    Rückimport (Portfolia-Kennungen) wird vollständig verknüpft, ein Datensatz anderer Herkunft ohne Kennung bleibt in
    der Prüfung – in keinem Fall eine zusätzliche Buchung."""
    c = _equal_deposits(cfg, n)
    try:
        cx = c.app.state.ctx
        svc = csv_service(cx)
        first = upload(c, "quelle.csv", PF_HEADER + "".join(f"S{i:02d}" + LINE for i in range(n)))
        assert _execute(c, first, "link").status_code == 303
        assert cx.db.scalar("SELECT COUNT(*) FROM tx_link WHERE status='active'") == n
        before = len(cx.recorded_portfolio().txs)
        replay = upload(c, "rueckimport.csv", PF_HEADER + "".join(f"D{i:02d}" + LINE for i in range(n)))
        assert {(r.match or {}).get("target") for r in svc.rows(replay)} == {f"D{i:02d}" for i in range(n)}
        assert _execute(c, replay, "suggest").status_code == 303
        assert len(cx.recorded_portfolio().txs) == before
        other = upload(c, "fremd.csv", PF_HEADER + "".join(f"X{i:02d}" + LINE for i in range(n)))
        assert all((r.match or {}).get("action") == "review" for r in svc.rows(other))
        _execute(c, other, "suggest")  # nichts Sicheres auswählbar → abgelehnt bzw. ohne Buchung
        assert len(cx.recorded_portfolio().txs) == before
    finally:
        c.__exit__(None, None, None)


def test_distinct_equal_event_beside_its_twin_stays_bookable(cfg):
    """Zwei verschiedene Portfolia-Kennungen gleicher Größe: D1 ist die vorhandene Buchung, N2 ein eigener Vorgang."""
    c = _equal_deposits(cfg, 1)
    try:
        cx = c.app.state.ctx
        bid = upload(c, "zwei.csv", PF_HEADER + "D00" + LINE + "N2" + LINE)
        acts = {r.rec.ext_id: ((r.match or {}).get("action"), (r.match or {}).get("target"))
                for r in csv_service(cx).rows(bid)}
        assert acts == {"pf:D00": ("link", "D00"), "pf:N2": ("include", None)}
        before = len(cx.recorded_portfolio().txs)
        assert _execute(c, bid, "suggest").status_code == 303
        assert len(cx.recorded_portfolio().txs) == before + 1
    finally:
        c.__exit__(None, None, None)


def test_more_rows_than_bookings_without_identity_stays_open(cfg):
    """Zwei gleich große Zeilen ohne Kennung, eine Buchung: höchstens eine ist sie – der Nutzer entscheidet."""
    c = _equal_deposits(cfg, 1)
    try:
        cx = c.app.state.ctx
        bid = upload(c, "ohne-kennung.csv", PF_HEADER + LINE + LINE.replace(",1,\n", ",1,zweite\n"))
        rows = csv_service(cx).rows(bid)
        assert len(rows) == 2 and all((r.match or {}).get("action") != "include" for r in rows)
    finally:
        c.__exit__(None, None, None)


def test_replay_after_restart_books_nothing(cfg):
    c = _equal_deposits(cfg, 2)
    try:
        cx = c.app.state.ctx
        first = upload(c, "quelle.csv", PF_HEADER + "S00" + LINE + "S01" + LINE)
        assert _execute(c, first, "link").status_code == 303
        replay = upload(c, "rueck.csv", PF_HEADER + "D00" + LINE + "D01" + LINE)
        assert _execute(c, replay, "suggest").status_code == 303
        before = len(cx.recorded_portfolio().txs)
    finally:
        c.__exit__(None, None, None)
    with TestClient(build_app(cfg, start_scheduler=False)) as again:  # Neustart: kalte Caches, gleiche Datenbank
        again.get("/settings")
        again.token = again.cookies.get("portfolia_csrf")
        cx = again.app.state.ctx
        assert len(cx.recorded_portfolio().txs) == before
        replay = upload(again, "rueck2.csv", PF_HEADER + "D00" + LINE + "D01" + LINE)
        _execute(again, replay, "suggest")
        assert len(cx.recorded_portfolio().txs) == before


def test_claim_origin_rules():
    claims_same_batch = [("csv:portfolia", 7)]
    assert AS._own_record(7, "csv:portfolia", claims_same_batch)
    assert not AS._own_record(8, "csv:portfolia", claims_same_batch)  # generisches Format: kein Herkunftsbeweis
    assert AS._own_record(None, "sync:bitpanda", [("sync:bitpanda", None)])  # anbieterstabile Quelle
    assert not AS._own_record(None, "sync:bitpanda", [("csv:portfolia", 3)])
    assert not AS._own_record(None, "sync:bitpanda", [])  # keine Belegung ist kein Beweis


# -- N28: technische Identität der Datenquellen -------------------------------------------------------------------

def _wallets(cx, same_name=True, extra=()):
    source = datasource_service(cx)
    for i in (1, 2, *extra):
        sid, errors = source.create({"kind": "wallet", "provider": "ethereum",
                                     "name": "Wallet" if same_name else f"Wallet {i}", "account": "Wallet K",
                                     "address": "0x" + str(i) * 40, "sync_interval_min": "1440", "auto_commit": "0"})
        assert sid and not errors, errors
        cx.db.x("INSERT INTO ds_balance(source_id, asset_key, qty, observed_at) VALUES (?,?,?,?)",
                (sid, "ETH", str(i), "2025-01-01T00:00:00Z"))


def _identities(cx):
    return sorted(FE.ds_identity(dict(r)) for r in cx.db.q(FE._DS_IDENT_SQL))


def _balances(cx):
    return sorted((r[0], r[1]) for r in cx.db.q(
        "SELECT d.address, b.qty FROM ds_balance b JOIN data_source d ON d.id = b.source_id"))


def test_export_restore_export_restore_keeps_every_source_identity(c, tmp_path):
    cx = c.app.state.ctx
    _wallets(cx)
    want, balances = _identities(cx), _balances(cx)
    body = c.get("/journal/export.zip").content
    for step in ("eins", "zwei"):
        config = _other(cx.config, tmp_path, step)
        _place(config, body, f"{step}.zip")
        with _client(config) as restored:
            other = restored.app.state.ctx
            assert tasks.import_check(other, "review").status == "imported"
            assert _identities(other) == want
            assert _balances(other) == balances  # Bestände hängen an der richtigen gleichnamigen Quelle
            restored.get("/settings")
            restored.headers["X-CSRF-Token"] = restored.cookies.get("portfolia_csrf")
            body = restored.get("/journal/export.zip").content


def test_repeated_restore_into_the_same_database_adds_nothing(c):
    cx = c.app.state.ctx
    _wallets(cx)
    rows = [{k: r[k] for k in FE.DS_COLS} for r in cx.db.q("SELECT * FROM data_source ORDER BY id")]
    with cx.db.transaction() as conn:
        assert FE._datasources(conn, rows, "2026-01-01T00:00:00Z") == 0
        assert FE._datasources(conn, [{**rows[0], "address": rows[0]["address"].upper().replace("0X", "0x")}],
                               "2026-01-01T00:00:00Z") == 0  # EVM-Adresse ohne Groß-/Kleinschreibung
        assert FE._datasources_missing(conn, rows) == 0
        third = {**rows[0], "address": "0x" + "3" * 40}
        assert FE._datasources_missing(conn, [*rows, third]) == 1  # sichtbar gezählt, nie still
        assert FE._datasources(conn, [third], "2026-01-01T00:00:00Z") == 1
    assert cx.db.scalar("SELECT COUNT(*) FROM data_source") == 3


def test_older_export_without_source_identity_attaches_only_when_unique(c):
    cx = c.app.state.ctx
    _wallets(cx, same_name=False)
    datasource_service(cx).create({"kind": "wallet", "provider": "ethereum", "name": "Doppelt", "account": "A",
                                   "address": "0x" + "4" * 40, "sync_interval_min": "1440", "auto_commit": "0"})
    datasource_service(cx).create({"kind": "wallet", "provider": "ethereum", "name": "Doppelt", "account": "A",
                                   "address": "0x" + "5" * 40, "sync_interval_min": "1440", "auto_commit": "0"})
    conn = cx.db.conn
    unique = FE._source_of(conn, {"provider": "ethereum", "account": "Wallet K", "name": "Wallet 1"})
    assert unique == cx.db.scalar("SELECT id FROM data_source WHERE name='Wallet 1'")
    assert FE._source_of(conn, {"provider": "ethereum", "account": "A", "name": "Doppelt"}) is None  # mehrdeutig
    exact = FE._source_of(conn, {"provider": "ethereum", "account": "A", "name": "Doppelt", "source_kind": "wallet",
                                 "source_address": "0x" + "5" * 40})
    assert exact == cx.db.scalar("SELECT id FROM data_source WHERE address=?", ("0x" + "5" * 40,))


def test_identity_covers_watch_configuration_but_never_credentials():
    base = {"provider": "bitcoin", "kind": "wallet", "account": "BTC", "name": "Cold", "address": None}
    a = FE.ds_identity({**base, "watch_json": json.dumps({"xpubs": ["xpubA"], "addresses": []})})
    b = FE.ds_identity({**base, "watch_json": json.dumps({"xpubs": ["xpubB"], "addresses": []})})
    w = FE.ds_identity({**base, "watch_json": json.dumps({"watch_id": "w1", "addresses": ["bc1q"]})})
    assert len({a, b, w}) == 3
    api = FE.ds_identity({"provider": "bitpanda", "kind": "api", "account": "BP", "name": "Bitpanda",
                          "credential_ref": "secret-ref-1"})
    assert api == FE.ds_identity({"provider": "bitpanda", "kind": "api", "account": "BP", "name": "Bitpanda",
                                  "credential_ref": "secret-ref-2"})
    assert not any("secret" in part for part in api)


# -- N26: Lesestand und Caches ------------------------------------------------------------------------------------

def _value(cx, target):
    return next(t.value_eur for t in cx.recorded_portfolio().txs if t.tx_id == target)


def test_portfolio_built_inside_a_rolled_back_write_is_never_published(c):
    cx, _svc, _bid, rc = _duplicate(c)
    target = str(rc.match["target"])
    assert _value(cx, target) == Decimal(1)
    with pytest.raises(RuntimeError, match="zurück"), cx.db.transaction():
        _edit_target(cx, target, "555")  # läuft in der äußeren Transaktion mit
        assert _value(cx, target) == Decimal(555)  # eigener, ungeschriebener Stand – privat
        raise RuntimeError("zurückrollen")
    assert _value(cx, target) == Decimal(1)
    assert cx.ledger() is cx.ledger()  # danach wieder gemeinsam zwischengespeichert


def test_snapshot_reader_uses_matching_cache_and_never_publishes_an_old_state(c):
    cx, _svc, _bid, rc = _duplicate(c)
    target = str(rc.match["target"])
    shared = cx.recorded_portfolio()
    with cx.db.snapshot():
        assert cx.recorded_portfolio() is shared  # gleicher Stand → gemeinsamer Eintrag gilt
        assert not cx.may_publish(cx.cache_rev())
        _run_editor(cx, lambda: (_edit_target(cx, target, "777"), cx.ledger()))  # anderer Thread: neuer Stand, warm
        led_old = cx.ledger()
        assert _value(cx, target) == Decimal(1)
    assert _value(cx, target) == Decimal(777)
    assert cx.ledger() is not led_old


def test_tax_and_hint_caches_follow_the_same_rule(c):
    cx, _svc, _bid, rc = _duplicate(c)
    target = str(rc.match["target"])
    rev = cx.cache_rev()
    assert cx.may_publish(rev)
    _edit_target(cx, target, "9")
    assert cx.cache_rev() != rev and not cx.may_publish(rev)  # Revision während der Berechnung bewegt → privat


# -- N27: Ausgleichsregel überlappender abgelehnter Einzelaktionen ----------------------------------------------

def _val(cx, row):
    return cx.db.scalar("SELECT value_in FROM csv_row WHERE id=?", (row.id,))


@pytest.mark.parametrize("order", ["b_first", "a_first"])
def test_two_rejected_actions_restore_the_original_in_any_order(c, order):
    cx, svc, bid, _mine, other = _two_rows(c)
    a = svc.set_rows(bid, {f"val_{other.idx}": "50"})["undo"]
    b = svc.set_rows(bid, {f"val_{other.idx}": "99"})["undo"]
    first, second = (b, a) if order == "b_first" else (a, b)
    svc.restore_inputs(bid, first)
    svc.restore_inputs(bid, second)
    assert _val(cx, other) is None
    assert svc.restore_inputs(bid, a) == 0 and svc.restore_inputs(bid, b) == 0  # wiederholt: nichts mehr
    assert _val(cx, other) is None


def test_a_rejected_b_succeeded_keeps_b(c):
    cx, svc, bid, _mine, other = _two_rows(c)
    a = svc.set_rows(bid, {f"val_{other.idx}": "50"})["undo"]
    svc.set_rows(bid, {f"val_{other.idx}": "99"})
    svc.restore_inputs(bid, a)
    assert _val(cx, other) == "99"


def test_a_succeeded_b_rejected_keeps_a(c):
    cx, svc, bid, _mine, other = _two_rows(c)
    svc.set_rows(bid, {f"val_{other.idx}": "50"})
    b = svc.set_rows(bid, {f"val_{other.idx}": "99"})["undo"]
    svc.restore_inputs(bid, b)
    assert _val(cx, other) == "50"


@pytest.mark.parametrize("later", ["same_value", "a_b_a"])
def test_late_restore_never_removes_a_later_successful_input(c, later):
    cx, svc, bid, _mine, other = _two_rows(c)
    a = svc.set_rows(bid, {f"val_{other.idx}": "50"})["undo"]
    if later == "a_b_a":
        svc.set_rows(bid, {f"val_{other.idx}": "99"})
    svc.set_rows(bid, {f"val_{other.idx}": "50"})  # ausdrücklich (gleicher Wert): eigene Kennung
    assert svc.restore_inputs(bid, a) == 0
    assert _val(cx, other) == "50"


def test_rejected_b_over_a_rejected_earlier_follows_the_revocation_chain(c):
    """A abgelehnt und zurückgenommen, *danach* B (hatte A's Wert gesehen) abgelehnt → Ausgangswert."""
    cx, svc, bid, _mine, other = _two_rows(c)
    a = svc.set_rows(bid, {f"val_{other.idx}": "50"})["undo"]
    b = svc.set_rows(bid, {f"val_{other.idx}": "99"})["undo"]
    assert svc.restore_inputs(bid, a) == 0  # B hält die Gruppe – A vermerkt nur den Widerruf
    assert svc.restore_inputs(bid, b) == 1
    assert _val(cx, other) is None


def test_restore_of_an_input_without_stored_previous_state_still_works(c):
    """Eingabe vor 0.96.0 (kein abgelegter Stand davor): Rücknahme wie bisher, mit neuer Kennung."""
    cx, svc, bid, _mine, other = _two_rows(c)
    a = svc.set_rows(bid, {f"val_{other.idx}": "50"})["undo"]
    cx.db.x("DELETE FROM csv_input_hist")
    assert svc.restore_inputs(bid, a) == 1
    assert _val(cx, other) is None


def test_decision_stays_changeable_after_the_batch_is_complete(cfg):
    """Echtdaten-Abnahme 0.96.0: nach der letzten Entscheidung („done“) zeigt die Seite die Auswahl weiter an – sie
    muss den Eingabestand mitliefern, sonst wird jede Änderung als Formular ohne Eingabestand abgelehnt."""
    c = _equal_deposits(cfg, 1)
    try:
        cx = c.app.state.ctx
        bid = upload(c, "einzeln.csv", PF_HEADER + "NEU1,2025-02-01T10:00:00Z,deposit,,,,,Wallet K,KAS,3,,,,1,\n")
        idx = csv_service(cx).rows(bid)[0].idx
        fields = form_values(c.get(f"/journal/csv/{bid}").text, f"/journal/csv/{bid}/rows")
        fields[f"dec_{idx}"] = ["skip"]
        assert post(c, f"/journal/csv/{bid}/rows", fields).status_code == 303
        assert cx.db.scalar("SELECT status FROM csv_batch WHERE id=?", (bid,)) == "done"
        fresh = form_values(c.get(f"/journal/csv/{bid}").text, f"/journal/csv/{bid}/rows")
        assert f"iv_{idx}" in fresh
        fresh[f"dec_{idx}"] = [""]
        assert post(c, f"/journal/csv/{bid}/rows", fresh).status_code == 303
        assert cx.db.scalar("SELECT decision FROM csv_row WHERE batch_id=? AND idx=?", (bid, idx)) is None
    finally:
        c.__exit__(None, None, None)


# -- historische Aktionen ohne Besitzkennung -------------------------------------------------------------------------

def _legacy_skip(cx, svc, bid, mine, ver_after):
    p = BA.plan(svc, bid, "skip", {mine.idx})
    res = BA.execute(svc, bid, "skip", {mine.idx}, token=BA.new_token(), fingerprint=p.fingerprint())
    ops = json.loads(cx.db.scalar("SELECT ops_json FROM import_action WHERE id=?", (res["action_id"],)))
    for st in ops["rows"].values():  # Format vor 0.95.0: ohne Besitzkennung
        st["set"].pop("ver_dec", None)
    cx.db.x("UPDATE import_action SET ops_json=? WHERE id=?", (json.dumps(ops), res["action_id"]))
    cx.db.x("UPDATE csv_row SET ver_dec=? WHERE id=?", (ver_after, mine.id))
    return res["action_id"]


def test_legacy_action_is_only_undone_with_solid_evidence(c):
    cx, svc, bid, mine, _other = _two_rows(c)
    aid = _legacy_skip(cx, svc, bid, mine, 0)  # nie wieder geschrieben seit Einführung der Kennungen
    res = BA.undo(svc, aid)
    assert res["restored"] == 1 and not res["unproven"]
    assert cx.db.scalar("SELECT decision FROM csv_row WHERE id=?", (mine.id,)) is None


def test_legacy_action_without_evidence_is_rejected_with_reason(c):
    cx, svc, bid, mine, _other = _two_rows(c)
    aid = _legacy_skip(cx, svc, bid, mine, 4711)  # Kennung vorhanden, aber nicht protokolliert: Herkunft offen
    res = BA.undo(svc, aid)
    assert res["unproven"] == 1 and res["restored"] == 0
    assert cx.db.scalar("SELECT decision FROM csv_row WHERE id=?", (mine.id,)) == "skip"
    page = c.get(f"/journal/csv/{bid}?msg=undo&r=0&c=0&t=0&u=1").text
    assert "vor Version 0.95.0" in page and "nicht belegbar" in page


# -- N21-Rest: Abhängigkeiten aus den Lesepfaden -------------------------------------------------------------------

def test_booking_dependencies_are_derived_from_all_portfolio_overlays():
    for t in ("account_merge", "asset_merge", "account_remove", "account_meta", "asset_change", "tx_estimate",
              "tx_link", "journal_event_alias", "event_decision", "journal_import_link", "csv_account", "csv_symbol",
              "journal_asset", "journal_tx", "tx_override", "imports"):
        assert t in BOOKING_TABLES, t
    assert set(BOOKING_TABLES) | set(NON_BOOKING_REVS) == set(REV_TABLES)


def test_revision_triggers_count_only_effective_changes(c):
    cx = ctx(c)
    db = cx.db
    v0 = DA.data_version(db)
    db.x("INSERT INTO asset_change(kind, old_asset, status, origin, hint_key, created_at) "
         "VALUES ('hint', 'KAS', 'open', 'test', 'h1', '2026-01-01')")
    assert DA.data_version(db) == v0  # reiner Hinweis
    cols = {r[1]: r for r in db.q("PRAGMA table_info(plan)")}
    vals = {"key": "Wallet K|KAS", "account": "Wallet K", "asset_id": "KAS", "freq": "monthly", "amount_eur": "50",
            "confidence": "hoch", "status": "active"}
    vals.update({k: "2026-01-01" for k, r in cols.items() if r[3] and r[4] is None and k not in vals and k != "id"})
    db.x(f"INSERT INTO plan({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})", list(vals.values()))
    rev = cx.cache_rev()
    assert DA.data_version(db) == v0 and "pf:plan:1" in rev  # nur Cache-Identität
    db.x("UPDATE asset_change SET kind='rename' WHERE hint_key='h1'")
    assert DA.data_version(db) != v0


@pytest.mark.parametrize("change", [None, "meta", "remove"])
def test_account_overlays_are_dependencies_until_the_booking_transaction(c, monkeypatch, change):
    cx, _svc, bid, _rc, _target = _journal_transfer(c, "1")
    shown = form_values(c.get(f"/journal/csv/{bid}/preview?action=include&force=1").text,
                        f"/journal/csv/{bid}/execute")
    real = BA._execute_tx

    def before_commit(*args, **kwargs):
        if change == "meta":
            _run_editor(cx, lambda: ACC.update(cx, "Sender", "", "", "Notiz geändert"))
        elif change == "remove":
            _run_editor(cx, lambda: cx.db.x(
                "INSERT INTO account_remove(account, status, tx_ids_json, created_at) VALUES ('Unbeteiligt', "
                "'active', '[]', '2026-01-01')"))
        return real(*args, **kwargs)

    monkeypatch.setattr(BA, "_execute_tx", before_commit)
    response = post(c, f"/journal/csv/{bid}/execute", shown)
    monkeypatch.undo()
    created = cx.db.scalar("SELECT COUNT(*) FROM import_action WHERE batch_id=?", (bid,))
    assert (response.status_code, created) == ((303, 1) if change is None else (409, 0))


def test_migration_39_upgrades_an_existing_database(tmp_path):
    from app.db import Database

    db = Database(tmp_path / "alt.db")
    db.migrate(target=38)
    db.migrate()
    names = {r[0] for r in db.q("SELECT name FROM data_rev")}
    assert {"asset_change", "tx_estimate", "pf:plan", "pf:tx_estimate"} <= names
    with pytest.raises(sqlite3.IntegrityError):  # Ausgleichstabelle mit Fremdschlüssel auf die Prüfzeile
        db.x("INSERT INTO csv_input_hist(row_id, grp, token, before_json, before_ver) VALUES (999, 'val', 1, "
             "'[]', 0)")
