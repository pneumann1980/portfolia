"""Prüfworkflow (Financial Integrity, Phase 1): zentrale Statuslogik, Stapelabschluss, nachvollziehbare Nullzähler,
manuelle Prüfung aller Fälle, Bestandsentscheidungen über Abrufzustände hinweg.

Nur synthetische Daten und temporäre Datenbanken. Jeder Test benennt den gemeldeten Fehler, den er festhält.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from app.csvimport import batch as BA
from app.csvimport import model as M
from app.csvimport import queue as Q
from app.csvimport import review as RV
from app.csvimport.model import Rec
from app.csvimport.service import csv_service
from app.datasources import connector as K
from app.datasources.service import datasource_service
from app.diagnosis import actions as DA
from app.diagnosis.engine import _holding_finding
from app.diagnosis.model import HoldingRow
from app.main import build_app
from tests.test_csvimport import create_unknown_assets, journal, post, upload
from tests.test_datasources import batch_of, buy, client, create_source, deposit, fake, sync, ts  # noqa: F401

D = Decimal


def ctx(c):
    return c.app.state.ctx


def status(c, bid):
    b = csv_service(ctx(c)).batch(bid)
    return None if b is None else b["status"]


def unknown(key: str, when: str, sym: str = "ZZQX", qty: str = "5") -> K.SourceEvent:
    """Zugang eines Assets ohne Zuordnung → „unvollständig“ (manuell zu prüfen)."""
    return K.SourceEvent(key, ts(when), [Rec(line=0, ts=ts(when), kind=M.DEPOSIT, in_sym=sym, in_qty=D(qty),
                                             label="deposit")])


def snapshot(c) -> str:
    """Fachlicher Datenstand: Buchungen, Entscheidungen, Verknüpfungen, Diagnose-Entscheidungen."""
    db = ctx(c).db
    parts = []
    for sql in ("SELECT tx_id, status, from_qty, to_qty, value_eur FROM journal_tx ORDER BY tx_id",
                "SELECT batch_id, idx, decision, tx_id FROM csv_row ORDER BY batch_id, idx",
                "SELECT * FROM tx_link ORDER BY id", "SELECT id, action, status FROM diag_decision ORDER BY id",
                "SELECT id, status FROM import_action ORDER BY id"):
        parts.append(json.dumps([list(r) for r in db.q(sql)], default=str))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def synced_batch(c, fake, events) -> tuple[int, int]:  # noqa: F811
    sid = create_source(c)
    fake.events = events
    bid = batch_of(sync(c, sid))
    create_unknown_assets(c, bid)
    return sid, bid


# ----------------------------------------------------------------------------------------------------
# 1–3: Abschluss, Historie, Teilübernahme
# ----------------------------------------------------------------------------------------------------

def test_01_closed_batch_leaves_active_review(client, fake):  # noqa: F811
    """Fehler: Stapel blieben nach vollständiger Bearbeitung „teilweise übernommen“ in der aktiven Prüfung."""
    c = client
    sid, bid = synced_batch(c, fake, [buy("kraken:T1", "2024-06-01T10:00:00", "500", "0.01"),
                                      deposit("kraken:L1", "2024-05-30T09:00:00", "EUR", "1000")])
    assert RV.source_summary(ctx(c).db, sid).open == 2
    assert post(c, f"/journal/csv/{bid}/commit").status_code == 303
    assert status(c, bid) == "committed"
    assert RV.active_summary(ctx(c).db).open == 0
    assert datasource_service(ctx(c)).pending_batches(sid) == []
    assert datasource_service(ctx(c)).open_counts(sid) == {}
    page = c.get("/journal/pruefung").text
    assert "Keine Vorgänge zu prüfen" in page


def test_02_closed_batch_stays_in_history(client, fake):  # noqa: F811
    """Abgeschlossene Stapel verschwinden nicht: Reiter „Abgeschlossen“ und Stapelseite bleiben erreichbar."""
    c = client
    _sid, bid = synced_batch(c, fake, [buy("kraken:T1", "2024-06-01T10:00:00", "500", "0.01")])
    post(c, f"/journal/csv/{bid}/commit")
    qu = Q.build(ctx(c).db, "abgeschlossen")
    assert qu.total == 1 and qu.items[0].batch_id == bid and qu.items[0].state == "uebernommen"
    page = c.get(f"/journal/csv/{bid}").text
    assert "Prüfstand" in page and "abgeschlossen" in page
    assert f"/journal/csv/{bid}" in c.get("/journal/pruefung?tab=abgeschlossen").text


def test_03_partial_batch_shows_only_open_events(client, fake):  # noqa: F811
    """Teilübernahme: nur der zurückgestellte Vorgang bleibt offen; nicht übernommene sind abgeschlossen."""
    c = client
    _sid, bid = synced_batch(c, fake, [buy("kraken:T1", "2024-06-01T10:00:00", "500", "0.01"),
                                       buy("kraken:T2", "2024-06-02T10:00:00", "300", "0.005"),
                                       deposit("kraken:L1", "2024-05-30T09:00:00", "EUR", "1000")])
    rs = {rc.rec.event_key: rc for rc in csv_service(ctx(c)).rows(bid)}
    r = post(c, f"/journal/csv/{bid}/rows", **{f"dec_{rs['kraken:T2'].idx}": "defer",
                                                f"dec_{rs['kraken:L1'].idx}": "skip"})
    assert r.status_code == 303
    post(c, f"/journal/csv/{bid}/commit")
    assert status(c, bid) == "partial"
    s = RV.batch_summary(ctx(c).db, bid)
    assert (s.open, s.events["zurueckgestellt"], s.events["uebernommen"], s.events["nicht_uebernommen"]) == (1, 1, 1, 1)
    qu = Q.build(ctx(c).db, "offen")
    assert [(i.event, i.state, i.reason) for i in qu.items] == [("kraken:T2", "zurueckgestellt", "zurückgestellt")]
    assert len(journal(c, "source='sync:kraken'")) == 1


# ----------------------------------------------------------------------------------------------------
# 4–7: Zähler, Nullgründe, manuelle Prüfung, „alles bekannt“
# ----------------------------------------------------------------------------------------------------

def test_04_counts_agree_across_all_views(client, fake):  # noqa: F811
    """Fehler: Stapelseite, Datenquelle, Warteschlange und Stapelabschluss zählten dieselben Zeilen verschieden."""
    c = client
    sid = create_source(c)
    fake.events = [deposit("kraken:L1", "2024-05-30T09:00:00", "EUR", "1000"),  # sicher (neu, hohe Sicherheit)
                   unknown("kraken:U1", "2024-06-03T10:00:00"), unknown("kraken:U2", "2024-06-04T10:00:00", sym="ZZQY")]
    bid = batch_of(sync(c, sid))  # Assets bewusst nicht angelegt → zwei Vorgänge ohne Asset-Zuordnung
    db = ctx(c).db
    rows = csv_service(ctx(c)).rows(bid)
    b = csv_service(ctx(c)).batch(bid)
    f = RV.funnel(b, rows, json.loads(b["summary_json"] or "{}"))
    src = RV.source_summary(db, sid)
    qu = Q.build(db, "offen", source=sid)
    safe = Q.build(db, "sicher", source=sid)
    assert f["events_open"] == src.open == qu.counts["offen"] + qu.counts["sicher"] == qu.total + safe.total == 3
    assert sum(datasource_service(ctx(c)).open_counts(sid).values()) == src.open
    assert (qu.total, safe.total, f["safe_new_rows"]) == (2, 1, 1)
    assert BA.overview(rows)["total"] == f["events_open"]  # Gruppen der Stapelseite (je Vorgang eine Zeile)
    assert f["manual_events"] == 2 and f["missing_asset"] == 2, rows[1].errors


def test_05_zero_safe_actions_explains_manual_review(client, fake):  # noqa: F811
    """Fehler: „0 sichere Aktionen“ ohne Grund – obwohl Vorgänge offen waren."""
    c = client
    sid = create_source(c)
    fake.events = [unknown("kraken:U1", "2024-06-03T10:00:00")]
    bid = batch_of(sync(c, sid))
    rows = csv_service(ctx(c)).rows(bid)
    b = csv_service(ctx(c)).batch(bid)
    f = RV.funnel(b, rows, json.loads(b["summary_json"] or "{}"))
    assert f["safe_link_rows"] == f["safe_new_rows"] == 0 and f["events_open"] == 1
    assert f["zero_reason"].startswith("Keine sicheren Vorschläge") and "ohne Asset-Zuordnung" in f["zero_reason"]
    page = c.get(f"/journal/csv/{bid}").text
    assert "Keine sicheren Vorschläge" in page and "1 Vorgang offen" in page


def test_06_manual_review_reaches_every_open_case(client, fake):  # noqa: F811
    """Fehler: „Jetzt prüfen“ öffnete nur den neuesten Stapel; offene Fälle älterer Stapel waren unerreichbar."""
    c = client
    sid = create_source(c)
    fake.events = [unknown("kraken:U1", "2024-06-03T10:00:00")]
    b1 = batch_of(sync(c, sid))
    rs = csv_service(ctx(c)).rows(b1)
    post(c, f"/journal/csv/{b1}/rows", **{f"dec_{rs[0].idx}": "defer"})  # bearbeitet → neuer Abruf, neuer Stapel
    fake.events = [*fake.events, unknown("kraken:U2", "2024-06-04T10:00:00", sym="ZZQY")]
    b2 = batch_of(sync(c, sid))
    assert b2 != b1
    qu = Q.build(ctx(c).db, "offen", source=sid)
    assert {(i.batch_id, i.event) for i in qu.items} == {(b1, "kraken:U1"), (b2, "kraken:U2")}
    page = c.get(f"/journal/pruefung?source={sid}").text
    assert f"/journal/csv/{b1}?ev=kraken%3AU1" in page and f"/journal/csv/{b2}?ev=kraken%3AU2" in page
    focused = c.get(f"/journal/csv/{b1}?ev=kraken%3AU1").text
    assert "ZZQX" in focused and "ZZQY" not in focused
    form = c.get(f"/settings/datasources/{sid}").text
    assert f"/journal/pruefung?source={sid}" in form and "Jetzt prüfen" in form


def test_07_fully_known_file_closes_immediately(client):  # noqa: F811
    """Eine Datei, deren Vorgänge alle schon vorhanden bzw. entschieden sind, ist sofort abgeschlossen (Historie)."""
    c = client
    bid, _ = upload(c, "binance.csv")
    create_unknown_assets(c, bid)
    rs = {rc.line: rc for rc in csv_service(ctx(c)).rows(bid)}
    post(c, f"/journal/csv/{bid}/rows", **{f"val_{rs[12].idx}": "0,46"})
    post(c, f"/journal/csv/{bid}/commit")
    assert status(c, bid) in ("committed", "partial")
    left = RV.batch_summary(ctx(c).db, bid)
    if left.open:  # z. B. ungeklärte Zeilen der Datei: bewusst nicht übernehmen → Stapel schließt
        idxs = [rc.idx for rc in csv_service(ctx(c)).rows(bid) if RV.is_open(RV.row_state(rc))]
        post(c, f"/journal/csv/{bid}/rows", **{f"dec_{i}": "skip" for i in idxs})
    assert status(c, bid) == "committed"
    bid2, _ = upload(c, "binance.csv")
    assert status(c, bid2) == "done", RV.batch_summary(ctx(c).db, bid2)
    assert RV.active_summary(ctx(c).db).open == 0
    assert "Alle Vorgänge" in c.get(f"/journal/csv/{bid2}").text


# ----------------------------------------------------------------------------------------------------
# 8–9: Wiederholter Abruf, Rückgängig
# ----------------------------------------------------------------------------------------------------

def test_08_repeated_sync_does_not_reopen_closed_cases(client, fake):  # noqa: F811
    """Fehler: Ein erneuter Abruf legte nicht übernommene Vorgänge wieder als offen vor. Neue Evidenz (geänderte
    Lieferung) öffnet dagegen erneut."""
    c = client
    sid, bid = synced_batch(c, fake, [buy("kraken:T1", "2024-06-01T10:00:00", "500", "0.01"),
                                      buy("kraken:T2", "2024-06-02T10:00:00", "300", "0.005")])
    rs = {rc.rec.event_key: rc for rc in csv_service(ctx(c)).rows(bid)}
    post(c, f"/journal/csv/{bid}/rows", **{f"dec_{rs['kraken:T2'].idx}": "skip"})
    post(c, f"/journal/csv/{bid}/commit")
    assert status(c, bid) == "committed"
    n_batches = ctx(c).db.scalar("SELECT COUNT(*) FROM csv_batch")
    fake.use_cursor = False  # überlappender Abruf: alles erneut
    sync(c, sid)
    sync(c, sid)
    assert ctx(c).db.scalar("SELECT COUNT(*) FROM csv_batch") == n_batches
    assert RV.source_summary(ctx(c).db, sid).open == 0 and status(c, bid) == "committed"
    assert len(journal(c, "source='sync:kraken'")) == 1
    # geänderte Lieferung des Anbieters = neue Evidenz → erneut zur Prüfung
    fake.events = [fake.events[0], buy("kraken:T2", "2024-06-02T10:00:00", "310", "0.005")]
    sync(c, sid)
    qu = Q.build(ctx(c).db, "offen", source=sid)
    safe = Q.build(ctx(c).db, "sicher", source=sid)
    assert [i.event for i in [*qu.items, *safe.items]] == ["kraken:T2"]


def test_09_undo_reopens_the_batch(client, fake):  # noqa: F811
    """Rückgängig einer abschließenden Stapelaktion öffnet den Stapel wieder (und nur dann)."""
    c = client
    _sid, bid = synced_batch(c, fake, [unknown("kraken:U1", "2024-06-03T10:00:00")])
    svc = csv_service(ctx(c))
    rows = svc.rows(bid)
    p = BA.plan(svc, bid, "skip", {rows[0].idx}, False, rows)
    res = BA.execute(svc, bid, "skip", {rows[0].idx}, token=BA.new_token(), fingerprint=p.fingerprint())
    assert not res.get("errors"), res
    assert status(c, bid) == "done" and RV.active_summary(ctx(c).db).open == 0
    aid = int(ctx(c).db.scalar("SELECT MAX(id) FROM import_action WHERE batch_id=?", (bid,)))
    out = BA.undo(svc, aid)
    assert not out.get("errors"), out
    assert status(c, bid) == "preview" and RV.batch_summary(ctx(c).db, bid).open == 1


# ----------------------------------------------------------------------------------------------------
# 10–12: gemeldete Fälle (Bitpanda, Binance, BNB) – synthetisch nachgebildet
# ----------------------------------------------------------------------------------------------------

def test_10_bitpanda_like_counts_over_several_batches(client, fake):  # noqa: F811
    """Bitpanda: Datenquelle meldete „5 offen“, „Jetzt prüfen“ zeigte 0 – die offenen Vorgänge lagen in einem
    älteren Stapel. Jetzt: dieselbe Zahl in Datenquelle, Warteschlange und je Stapel summiert; Order + Gebühr = 1."""
    c = client
    sid = create_source(c)
    t0 = ts("2024-06-01T10:00:00")
    order_with_fee = K.SourceEvent("kraken:O1", t0, [
        Rec(line=0, ts=t0, kind=M.TRADE, out_sym="EUR", out_qty=D(100), in_sym="ZZQX", in_qty=D("1"), label="trade"),
        Rec(line=0, ts=t0, kind=M.FEE, fee_sym="ZZQX", fee_qty=D("0.01"), label="trade")])
    fake.events = [order_with_fee, unknown("kraken:U1", "2024-06-02T10:00:00"),
                   unknown("kraken:U2", "2024-06-03T10:00:00")]
    b1 = batch_of(sync(c, sid))
    rs = csv_service(ctx(c)).rows(b1)
    post(c, f"/journal/csv/{b1}/rows", **{f"dec_{rs[-1].idx}": "defer"})
    fake.events = [*fake.events, unknown("kraken:U3", "2024-06-04T10:00:00"),
                   unknown("kraken:U4", "2024-06-05T10:00:00")]
    b2 = batch_of(sync(c, sid))
    assert b1 != b2
    src = RV.source_summary(ctx(c).db, sid)
    assert src.open == 5 and src.rows["offen"] + src.rows["zurueckgestellt"] == 6  # 6 Zeilen, 5 Vorgänge
    assert src.by_batch[b1] == 3 and src.by_batch[b2] == 2
    assert Q.build(ctx(c).db, "offen", source=sid).total == 5
    page = c.get("/settings/datasources").text
    assert "Zur Prüfung offen: <b>5 Vorgänge</b>" in page and f"/journal/pruefung?source={sid}" in page


def test_11_binance_rows_before_cutoff_are_history_not_open(client, fake):  # noqa: F811
    """Binance: Zeilen vor dem Stichtag galten als offen und die Vorauswahl bot dafür 79 Verknüpfungen an."""
    c = client
    sid = create_source(c)
    fake.events = [buy(f"kraken:H{i}", f"2023-01-{i + 1:02d}T10:00:00", "100", "0.002") for i in range(6)]
    bid = batch_of(sync(c, sid))
    create_unknown_assets(c, bid)
    assert post(c, f"/journal/csv/{bid}/options", cutoff="2024-01-01").status_code == 303
    rows = csv_service(ctx(c)).rows(bid)
    assert {rc.status for rc in rows} == {"before"}
    assert status(c, bid) == "done"
    s = RV.source_summary(ctx(c).db, sid)
    assert s.open == 0 and Q.build(ctx(c).db, "offen").total == 0
    assert Q.build(ctx(c).db, "historie").total == 6
    assert BA.selection(ctx(c).db, bid, rows) == set()
    plan = BA.plan(csv_service(ctx(c)), bid, "suggest", {rc.idx for rc in rows}, False, rows)
    assert not [it for it in plan.items if it.act in ("link", "include")]


def test_12_bnb_holding_decision_survives_fetch_aging(client):  # noqa: F811
    """BNB: „ungeklärt“ markierte Bestandsabweichung erschien nach 48 h (Abruf nicht mehr bestätigt) und auf der
    Datenquellenseite wieder offen. Schlüssel und Prüfsumme hängen nicht mehr vom Abrufzustand ab."""
    c = client
    db = ctx(c).db
    row = HoldingRow(account="BNB Wallet", asset="BNB", name="BNB", computed=D("1.50"), observed=D("1.2"),
                     expected=None, status="extern_diff")
    f = _holding_finding(None, row)  # type: ignore[arg-type]
    with db.transaction() as cx:
        cx.execute("INSERT INTO diag_decision(finding_id, kind, title, action, fingerprint, status, created_at) "
                   "VALUES (?, ?, ?, 'dismiss', ?, 'active', ?)", (f.id, f.kind, f.title, DA.fingerprint(f),
                                                                  datetime.now(UTC).isoformat()))
    aged = HoldingRow(account="BNB Wallet", asset="BNB", name="BNB", computed=D("1.5"), observed=D("1.20"),
                      expected=None, status="extern_unsicher")
    f2 = _holding_finding(None, aged)  # type: ignore[arg-type]
    assert f2.id == f.id and DA.fingerprint(f2) == DA.fingerprint(f)
    assert DA.mark_of(f2, DA.active_marks(db))[1] is True
    assert DA.holding_mark(db, "BNB Wallet", "BNB", D("1.5"), D("1.2")) == "ungeklaert"
    # neue Evidenz (anderer beobachteter Bestand) → Entscheidung überholt, Fall wieder sichtbar
    assert DA.holding_mark(db, "BNB Wallet", "BNB", D("1.5"), D("1.1")) == "ueberholt"
    # Entscheidung nach dem alten Schlüssel (mit Abrufzustand) gilt weiter
    legacy = DA._legacy_marks(f)
    assert legacy, "Altschlüssel müssen erkannt werden"


# ----------------------------------------------------------------------------------------------------
# 13–15: Ende-zu-Ende über HTTP, Persistenz, Nur-Lese-Ansichten
# ----------------------------------------------------------------------------------------------------

def test_13_http_end_to_end_import_to_closed(client):  # noqa: F811
    """Importieren → erkennen → entscheiden → übernehmen → Stapel abgeschlossen, alles über die Oberfläche."""
    c = client
    bid, loc = upload(c, "binance.csv")
    assert "Prüfstand" in c.get(loc).text
    create_unknown_assets(c, bid)
    rs = {rc.line: rc for rc in csv_service(ctx(c)).rows(bid)}
    post(c, f"/journal/csv/{bid}/rows", **{f"val_{rs[12].idx}": "0,46"})
    r = post(c, f"/journal/csv/{bid}/commit")
    assert r.status_code == 303
    left = [rc for rc in csv_service(ctx(c)).rows(bid) if RV.is_open(RV.row_state(rc))]
    if left:
        post(c, f"/journal/csv/{bid}/rows", **{f"dec_{rc.idx}": "skip" for rc in left})
    assert status(c, bid) == "committed"
    page = c.get(f"/journal/csv/{bid}").text
    assert "Prüfstand" in page and ">abgeschlossen<" in page
    assert "Keine Vorgänge zu prüfen" in c.get("/journal/pruefung").text
    assert "Prüfung" in c.get("/journal").text


def test_14_decisions_persist_across_restart(config, fake):  # noqa: F811
    """Entscheidungen (zurückgestellt, nicht übernommen) und der Stapelstatus überstehen einen Neustart."""
    with TestClient(build_app(config, start_scheduler=False)) as c:
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        _sid, bid = synced_batch(c, fake, [unknown("kraken:U1", "2024-06-03T10:00:00"),
                                           unknown("kraken:U2", "2024-06-04T10:00:00", sym="ZZQY")])
        rs = csv_service(ctx(c)).rows(bid)
        post(c, f"/journal/csv/{bid}/rows", **{f"dec_{rs[0].idx}": "defer", f"dec_{rs[1].idx}": "skip"})
        before = RV.batch_summary(ctx(c).db, bid)
    with TestClient(build_app(config, start_scheduler=False)) as c2:
        after = RV.batch_summary(ctx(c2).db, bid)
        assert after.events == before.events and after.events["zurueckgestellt"] == 1
        assert status(c2, bid) == "preview"
        assert "zurückgestellt" in c2.get("/journal/pruefung").text


def test_15_preview_queue_and_diagnosis_are_read_only(client, fake):  # noqa: F811
    """Ansehen ändert nichts: Vorschau, Warteschlange, Stapelseite, Datenquellen und Diagnose."""
    c = client
    sid, bid = synced_batch(c, fake, [buy("kraken:T1", "2024-06-01T10:00:00", "500", "0.01"),
                                      unknown("kraken:U1", "2024-06-03T10:00:00")])
    before = snapshot(c)
    st = status(c, bid)
    for url in (f"/journal/csv/{bid}/preview?action=suggest", "/journal/pruefung", "/journal/pruefung?tab=sicher",
                f"/journal/pruefung?source={sid}", f"/journal/csv/{bid}", "/settings/datasources",
                f"/settings/datasources/{sid}", "/quality/diagnose", "/journal"):
        assert c.get(url).status_code == 200, url
    assert snapshot(c) == before and status(c, bid) == st


