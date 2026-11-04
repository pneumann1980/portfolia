"""Regressionstests zur Nachprüfung „Buchungen prüfen“ vom 04.11.2026 (N01–N04, R03-Entscheidungen je Übernahmelauf).

Soll-Verhalten; synthetische Datenbanken und Anbieterantworten aus den Fixtures. Fehler werden an den tatsächlichen
Commitgrenzen eingespeist (vor bzw. nach der jeweiligen Transaktion), nicht nur an den Phasen-Haken von 0.89.0.
"""

# ruff: noqa: F811  (Fixtures aus anderen Testmodulen werden importiert und als Argumente verwendet)
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from app.csvimport import batch as BA
from app.csvimport.service import csv_service
from app.datasources import takeover as TK
from app.diagnosis import actions as A
from tests.test_bitpanda import api, ctx, master  # noqa: F401  (Fixtures)
from tests.test_review_fixes import c, cfg, economic_state, post  # noqa: F401  (Fixtures/Helfer)
from tests.test_takeover import staged, started


class ProcessStopped(BaseException):
    """Prozessabbruch (kein ``Exception``: wird von keiner Fehlerbehandlung abgefangen)."""


def _sandbox() -> bool:
    return bool(getattr(TK._SANDBOX, "on", False))


def _fault_hide_before(monkeypatch, exc):
    real = A._exec

    def boom(*a, **k):
        if not _sandbox():
            raise exc("vor dem Commit der Ausblendung")
        return real(*a, **k)

    monkeypatch.setattr(A, "_exec", boom)


def _fault_hide_after(monkeypatch, exc):
    real = A._after

    def boom(*a, **k):
        if not _sandbox():
            raise exc("nach dem Commit der Ausblendung")
        return real(*a, **k)

    monkeypatch.setattr(A, "_after", boom)


def _fault_batch_after(monkeypatch, exc, cx):
    svc = csv_service(cx)
    real = svc.evaluate
    state = {"done": False}

    def boom(bid):
        owned = cx.db.scalar("SELECT batch_id FROM ds_takeover WHERE batch_id=?", (bid,))
        if owned is not None and not state["done"]:
            state["done"] = True
            raise exc("nach dem Commit des Stapels")
        return real(bid)

    monkeypatch.setattr(svc, "evaluate", boom)


def _fault_action_before(monkeypatch, exc, cx):
    svc = csv_service(cx)
    real = svc.commit_apply

    def boom(*a, **k):
        raise exc("vor dem Commit der Buchungsaktion")

    monkeypatch.setattr(svc, "commit_apply", boom)
    return real


def _fault_action_after(monkeypatch, exc):
    real = BA.set_selection

    def boom(*a, **k):
        if not _sandbox():
            raise exc("nach dem Commit der Buchungsaktion")
        return real(*a, **k)

    monkeypatch.setattr(BA, "set_selection", boom)


FAULTS = ["hide_before", "hide_after", "batch_after", "action_before", "action_after"]


def _arm(name, monkeypatch, exc, cx):
    if name == "hide_before":
        _fault_hide_before(monkeypatch, exc)
    elif name == "hide_after":
        _fault_hide_after(monkeypatch, exc)
    elif name == "batch_after":
        _fault_batch_after(monkeypatch, exc, cx)
    elif name == "action_before":
        _fault_action_before(monkeypatch, exc, cx)
    else:
        _fault_action_after(monkeypatch, exc)


# ---- N01 · Wirtschaftliche Änderung und Rücknahmebezug in derselben Transaktion ----------------------------------

@pytest.mark.parametrize("where", FAULTS)
def test_N01_error_at_a_commit_boundary_restores_the_old_state(config, master, api, monkeypatch, where):
    client, sid = started(config, api)
    try:
        cx = ctx(client)
        preview = staged(client, sid)
        before = economic_state(client)
        _arm(where, monkeypatch, RuntimeError, cx)
        res = TK.apply(cx, sid, preview.token)
        assert res["errors"]
        monkeypatch.undo()
        cx.invalidate_data()
        last = TK.latest(cx.db, sid)
        assert economic_state(client) == before, (where, dict(last))
        assert last["status"] == "staged" and TK.needs_recovery(cx.db, sid) is None
        assert not cx.db.q("SELECT 1 FROM diag_decision WHERE kind='takeover' AND status='active'")
        assert TK.apply(cx, sid, TK.assess(cx, sid).token)["errors"] == []  # danach normal übernehmbar
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("where", FAULTS)
def test_N01_process_stop_at_a_commit_boundary_is_recoverable(config, master, api, monkeypatch, where):
    client, sid = started(config, api)
    try:
        cx = ctx(client)
        preview = staged(client, sid)
        before = economic_state(client)
        _arm(where, monkeypatch, ProcessStopped, cx)
        with pytest.raises(ProcessStopped):
            TK.apply(cx, sid, preview.token)
        monkeypatch.undo()
        cx.invalidate_data()
        if economic_state(client) != before:  # Teilzustand: muss vollständig referenziert und wiederherstellbar sein
            r = TK.needs_recovery(cx.db, sid)
            assert r is not None and (r["decision_id"] or r["batch_id"] or r["action_id"]), where
            assert "Unterbrochene Übernahme" in client.get(f"/settings/datasources/{sid}/uebernahme").text
            assert TK.start_apply(cx, sid, preview.token)["errors"] == [TK.RECOVER_FIRST]
            assert TK.recover(cx, sid)["errors"] == []
            cx.invalidate_data()
        assert economic_state(client) == before
        assert TK.needs_recovery(cx.db, sid) is None
        assert not cx.db.q("SELECT 1 FROM diag_decision WHERE kind='takeover' AND status='active'")
        assert not cx.db.q("SELECT 1 FROM import_action WHERE status='active'")
    finally:
        client.__exit__(None, None, None)


# ---- N03 · Gemeinsame Rücknahme fortsetzbar --------------------------------------------------------------------

def _applied(config, api):
    client, sid = started(config, api)
    cx = ctx(client)
    before = economic_state(client)
    assert TK.apply(cx, sid, staged(client, sid).token)["errors"] == []
    assert economic_state(client) != before
    return client, sid, cx, before


@pytest.mark.parametrize("mode", ["result", "exception", "process"])
@pytest.mark.parametrize("step", ["action", "hide"])
def test_N03_takeover_undo_resumes_after_a_failed_step(config, master, api, monkeypatch, mode, step):
    client, sid, cx, before = _applied(config, api)
    try:
        target = (BA, "undo") if step == "action" else (A, "undo")
        real = getattr(*target)

        def fail(*a, **k):
            if mode == "exception":
                raise RuntimeError("vorübergehender Fehler")
            if mode == "process":
                raise ProcessStopped
            return {"errors": ["vorübergehend abgelehnt"]} if step == "action" else A.Result(
                errors=["vorübergehend abgelehnt"])

        monkeypatch.setattr(*target, fail)
        if mode == "process":
            with pytest.raises(ProcessStopped):
                TK.undo(cx, sid)
        else:
            assert TK.undo(cx, sid)["errors"]
        monkeypatch.setattr(*target, real)
        last = TK.latest(cx.db, sid)
        assert last["status"] == "undoing" and TK.needs_recovery(cx.db, sid) is not None
        assert "Unterbrochene Übernahme" in client.get(f"/settings/datasources/{sid}/uebernahme").text
        # separate Rücknahme der Ausblendung bleibt gesperrt, solange API-Buchungen des Laufs bestehen
        if step == "action":
            assert A.undo(cx, int(last["decision_id"])).errors
        retry = TK.undo(cx, sid) if mode != "process" else TK.recover(cx, sid)
        assert retry["errors"] == [], retry
        cx.invalidate_data()
        assert economic_state(client) == before and TK.latest(cx.db, sid)["status"] == "undone"
        third = TK.undo(cx, sid)  # dritter Aufruf ändert nichts
        assert third["errors"] and economic_state(client) == before
    finally:
        client.__exit__(None, None, None)


def test_N03_undo_takes_back_every_action_of_the_run(config, master, api):
    """Auch Verknüpfen/Nicht-übernehmen-Aktionen auf dem Stapel des Laufs gehören zur gemeinsamen Rücknahme."""
    client, sid, cx, before = _applied(config, api)
    try:
        bid = int(TK.latest(cx.db, sid)["batch_id"])
        acts = [r["id"] for r in cx.db.q("SELECT id FROM import_action WHERE batch_id=? AND status='active'", (bid,))]
        assert acts
        assert TK.undo(cx, sid)["errors"] == []
        cx.invalidate_data()
        assert economic_state(client) == before
        assert not cx.db.q("SELECT 1 FROM import_action WHERE batch_id=? AND status='active'", (bid,))
    finally:
        client.__exit__(None, None, None)


# ---- N02 · Transferbestätigung gilt für die gesamte angezeigte Wirkungsmenge --------------------------------------

def _partner(svc, bid, task):
    event = task.ref.partition(":")[2]
    rows = svc.rows(bid)
    mine = [r for r in rows if (r.rec.event_key or f"#{r.idx}") == event]
    return mine, next(r for r in rows if r.idx == int(mine[0].pair_ref[2:]))


def _booked(cx, bid):
    return cx.db.scalar("SELECT COUNT(*) FROM journal_tx WHERE batch_id=? AND status='active'", (bid,))


@pytest.mark.parametrize("change", ["account", "value", "fee", "decision"])
def test_N02_change_of_the_counterpart_requires_a_new_confirmation(c, monkeypatch, change):
    from tests.test_review_fixes import _xfer_sheet, _xfer_task, form_values

    cx = ctx(c)
    svc = csv_service(cx)
    out = _xfer_sheet(c)
    bid = out["bid"]
    task, shown = _xfer_task(c, bid, monkeypatch)
    mine, partner = _partner(svc, bid, task)
    pair_before = {r.idx: r.pair_ok for r in svc.rows(bid)}
    mine_digest = BA.rows_digest(mine)
    if change == "account":
        svc.set_account(partner.row["to_account"] or partner.row["from_account"], "Changed after preview")
    elif change == "value":
        svc.set_rows(bid, {f"val_{partner.idx}": "1234"})
    elif change == "fee":
        svc.set_rows(bid, {f"fee_{partner.idx}": "0.5"})
    else:
        svc.set_rows(bid, {f"dec_{partner.idx}": "defer"})
    svc.evaluate(bid)
    if change == "account":  # die eigene Zeile ist unverändert – nur die Gegenseite hat sich geändert
        assert BA.rows_digest([r for r in svc.rows(bid) if r.idx in {m.idx for m in mine}]) == mine_digest
    shown["a"] = ["row:pair"]
    loc = post(c, "/quality/assistent/antwort", shown).headers["location"]
    err = parse_qs(urlsplit(loc).query).get("err", [""])[0]
    assert "Gegenbuchung" in err and _booked(cx, bid) == 0  # alte Bestätigung: keine wirtschaftliche Änderung
    assert {r.idx: r.pair_ok for r in svc.rows(bid)} == pair_before  # nichts halb gespeichert
    if change == "decision":
        return  # Gegenseite zurückgestellt: sie wird nicht stillschweigend mitgebucht
    fresh = form_values(c.get("/quality/assistent?new=1").text)
    fresh["a"] = ["row:pair"]
    first = post(c, "/quality/assistent/antwort", fresh).headers["location"]
    assert "err=" not in first, first
    again = post(c, "/quality/assistent/antwort", fresh).headers["location"]  # zweites Absenden
    assert "err=" in again and _booked(cx, bid) == 1  # genau einmal
    if change == "account":
        assert cx.db.scalar("SELECT to_account FROM journal_tx WHERE batch_id=? AND status='active'",
                            (bid,)) == "Changed after preview"  # bestätigt wurde der neu angezeigte Stand


def test_N02_link_answer_is_bound_to_the_target_booking(c, monkeypatch):
    """Antwort „ist schon gebucht – verknüpfen“: ändert sich die vorhandene Buchung, gilt die alte Antwort nicht."""
    from tests.test_diagnosis import PF_HEADER, upload
    from tests.test_review_fixes import form_values

    cx = ctx(c)
    from app.diagnosis import guide as G

    bid = upload(c, "dup.csv", PF_HEADER + "N1,2025-01-01T10:00:00Z,deposit,,,,,Wallet K,KAS,10,,,,1,dup\n")
    task = next(t for t in G.tasks(cx) if t.kind == "zeile" and t.ref.startswith(f"{bid}:"))
    monkeypatch.setattr(G, "tasks", lambda ctx, hints=False: [task])
    shown = form_values(c.get("/quality/assistent?new=1").text)
    shown["a"] = ["row:link"]
    target = next(str(t) for rc in csv_service(cx).rows(bid) for t in [(rc.match or {}).get("target")] if t)
    cx.db.x("UPDATE tx SET value_eur='7' WHERE tx_id=?", (target,))  # vorhandene Buchung geändert
    cx.invalidate_data()
    loc = post(c, "/quality/assistent/antwort", shown).headers["location"]
    assert "err=" in loc
    assert not cx.db.q("SELECT 1 FROM tx_link WHERE batch_id=?", (bid,))


# ---- N04 · Integritätsbestätigungen im gemeinsamen Protokoll -----------------------------------------------------

def _ack_item(c, iid="ack-1", title="Bestand weicht von der Summe der Bewegungen ab"):
    from app.diagnosis import guide as G
    from app.diagnosis import integrity as I

    cx = ctx(c)
    item = I.Item(id=iid, category="bestand", severity="kritisch", title=title, cause_kind="rechenfehler",
                  cause="Synthetische Abweichung.", impact="5 EUR", accounts=["Wallet K"], assets=["KAS"])
    I.save(cx.db, I.Run(started_at="2025-01-01T00:00:00Z", items=[item]))
    task = next(t for t in G.tasks(cx) if t.kind == "rechnung" and t.ref == iid)
    loc = post(c, "/quality/assistent/antwort", {"typ": "rechnung", "ref": iid, "kid": task.kid,
                                                 "a": "int:ack"}).headers["location"]
    assert f"ia={iid}" in loc
    return cx, item


def test_N04_acknowledged_integrity_finding_is_listed_and_undoable_from_the_protocol(c):
    from app.diagnosis import guide as G

    cx, item = _ack_item(c)
    assert not any(t.kind == "rechnung" and t.ref == item.id for t in G.tasks(cx))
    page = c.get("/quality/assistent/protokoll").text  # unabhängiger Aufruf ohne Seitenkontext
    assert item.title in page and "Wallet K" in page and f'name="ia" value="{item.id}"' in page
    r = post(c, "/quality/assistent/rueckgaengig", {"ia": item.id, "back": "protokoll"})
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert r.headers["location"].startswith("/quality/assistent/protokoll") and "wieder offen" in q["msg"][0]
    assert any(t.kind == "rechnung" and t.ref == item.id for t in G.tasks(cx))  # Befund wieder offen
    assert item.title not in c.get("/quality/assistent/protokoll").text
    again = post(c, "/quality/assistent/rueckgaengig", {"ia": item.id, "back": "protokoll"})
    assert "schon zurückgenommen" in parse_qs(urlsplit(again.headers["location"]).query)["err"][0]


def test_N04_acknowledgement_stays_understandable_after_a_newer_integrity_run(c):
    from app.diagnosis import guide as G
    from app.diagnosis import integrity as I

    cx, item = _ack_item(c)
    I.save(cx.db, I.Run(started_at="2025-02-01T00:00:00Z", items=[]))  # neuer Lauf meldet den Befund nicht mehr
    acks = G.integrity_acks(cx)
    assert acks[0]["title"] == item.title and "nicht mehr gemeldet" in acks[0]["note"]
    # ältere Einträge ohne gespeicherte Bezeichnung werden weiter gelesen
    cx.db.set_state(G.INTEGRITY_ACK_INFO_KEY, {})
    assert G.integrity_acks(cx)[0]["title"].startswith("Integritätsbefund ")


# ---- R03 · Entscheidungen je Übernahmelauf im Zustand nach dem Ausblenden -----------------------------------------

def _row(a):
    return next(p for p in a.problems if p["kind"] == "row")


def _all_state(c):
    """Wirtschaftlicher Stand über alle Konten: Buchungen, Bestände je Konto, Ergebnisse je Jahr, Lots."""
    cx = ctx(c)
    led = cx.ledger()
    lots = sorted((lot.account, lot.asset, str(lot.qty), str(lot.cost), str(lot.acq_date), lot.acq_tx, lot.origin)
                  for lot in led.lots if lot.qty)  # Lots: Menge, Einstand, Anschaffung je Konto
    accounts = sorted({a for t in cx.recorded_portfolio().txs for a in (t.from_account, t.to_account) if a})
    return (economic_state(c), {a: dict(led.holdings_by_account(a)) for a in accounts}, lots,
            # wirksame Herkunft/Aliase und Journal (zurückgenommene Buchungen bleiben nur als Nachweis stehen)
            sorted(tuple(r) for r in cx.db.q("SELECT a.key, a.tx_id FROM journal_event_alias a JOIN journal_tx t ON "
                                             "t.tx_id=a.tx_id WHERE t.status='active'")),
            sorted(tuple(r) for r in cx.db.q("SELECT tx_id, status, merged_into, from_account, from_qty, to_account, "
                                             "to_qty, fee_qty, value_eur FROM journal_tx WHERE status <> 'reverted'")))


def _refusing(config, api, deposit_qty="0.001"):
    from tests.helpers import tx

    rows = [tx("K1", "2024-02-01T08:00:00Z", "deposit", to=("Bitpanda", "EUR", "500"), value="500"),
            tx("K2", "2024-02-02T09:01:00Z", "buy", frm=("Bitpanda", "EUR", "250"), to=("Bitpanda", "BTC", "0.0055"),
               value="251.5"),
            tx("K3", "2024-02-12T12:00:00Z", "withdrawal", frm=("Bitpanda", "BTC", "0.001"), value="55"),
            tx("L1", "2024-02-12T12:30:00Z", "deposit", to=("Ledger", "BTC", deposit_qty), value="55")]
    return started(config, api, rows=rows)


@pytest.mark.parametrize("deposit_qty", ["0.001", "0.00098"])  # ohne bzw. mit Netzwerkgebühr am Zugangskonto
def test_R03_original_case_decided_in_takeover_context_full_lifecycle(config, master, api, deposit_qty):
    client, sid = _refusing(config, api, deposit_qty)
    try:
        cx = ctx(client)
        before = _all_state(client)
        a = staged(client, sid)
        assert not a.ok and [b.key for b in a.blockers] == ["rows"]
        p = _row(a)
        assert p["options"] == ["pair", "nopair", "skip"] and p["partner"]["kind"] == "imp"
        assert p["partner"]["tx"] == "L1"
        page = client.get(f"/settings/datasources/{sid}/uebernahme").text
        assert "Transfer mit dieser Gegenbuchung bestätigen" in page and "Jetzt entscheiden" not in page
        r = post(client, f"/settings/datasources/{sid}/uebernahme/entscheidung",
                 {"key": p["key"], "choice": "pair", "token": a.token})
        assert "Entscheidung+gespeichert" in r.headers["location"]
        assert TK.apply(cx, sid, a.token)["errors"]  # alte Vorschau gilt nicht mehr
        assert _all_state(client) == before
        b = TK.assess(cx, sid)
        assert b.ok and b.token != a.token and [t.tx_id for t in b.replaced] == ["L1"], b.blockers
        assert TK.apply(cx, sid, b.token)["errors"] == []
        cx.invalidate_data()
        led = cx.ledger()
        assert dict(led.holdings_by_account("Bitpanda")) == {k: v for k, v in b.observed.items() if v}
        assert dict(led.holdings_by_account("Ledger")) == before[1]["Ledger"]  # Gegenkonto exakt wie vorher
        tr = cx.db.q1("SELECT * FROM journal_tx WHERE type='transfer' AND status='active'")
        assert tr["from_account"] == "Bitpanda" and tr["to_account"] == "Ledger" and tr["to_qty"] == deposit_qty
        assert "L1" not in {t.tx_id for t in cx.recorded_portfolio().txs}  # durch den Transfer ersetzt
        ledger_lots = [lot for lot in led.lots if lot.account == "Ledger" and lot.qty]
        assert ledger_lots and all(lot.origin != "deposit" for lot in ledger_lots)  # Einstand fortgeführt (Kauf)
        assert {str(lot.acq_date) for lot in ledger_lots} == {"2024-02-02"}  # Anschaffungsdatum des Kaufs
        assert TK.undo(cx, sid)["errors"] == []
        cx.invalidate_data()
        assert _all_state(client) == before  # vollständige Rücknahme über alle Konten
        assert TK.undo(cx, sid)["errors"] and _all_state(client) == before  # wiederholt: keine Änderung
    finally:
        client.__exit__(None, None, None)


def test_R03_skip_and_standalone_decisions_keep_the_balance_proof(config, master, api):
    client, sid = _refusing(config, api)
    try:
        cx = ctx(client)
        before = _all_state(client)
        a = staged(client, sid)
        assert TK.decide(cx, sid, _row(a)["key"], "skip", a.token)["errors"] == []
        b = TK.assess(cx, sid)
        assert not b.ok and "rows" not in [x.key for x in b.blockers]  # entschieden – aber Bestand stimmt nicht
        assert TK.apply(cx, sid, b.token)["errors"] and _all_state(client) == before
        k = _row(a)["key"]
        assert TK.undecide(cx, sid, k, b.rid, b.decisions[k]["stamp"])["errors"] == []
        c2 = TK.assess(cx, sid)
        assert TK.decide(cx, sid, _row(c2)["key"], "nopair", c2.token)["errors"] == []
        d = TK.assess(cx, sid)
        assert d.ok, d.blockers  # eigener Abgang: Bestand der Quelle stimmt, L1 bleibt als Zugang
        assert TK.apply(cx, sid, d.token)["errors"] == []
        cx.invalidate_data()
        assert "L1" in {t.tx_id for t in cx.recorded_portfolio().txs}
        assert not cx.db.q("SELECT 1 FROM journal_tx WHERE type='transfer' AND status='active'")
        assert TK.undo(cx, sid)["errors"] == []
        cx.invalidate_data()
        assert _all_state(client) == before
    finally:
        client.__exit__(None, None, None)


def test_R03_stale_decision_is_rejected_visibly_and_never_applied(config, master, api):
    client, sid = _refusing(config, api)
    try:
        cx = ctx(client)
        a = staged(client, sid)
        key = _row(a)["key"]
        assert TK.decide(cx, sid, key, "pair", a.token)["errors"] == []
        cx.db.x("UPDATE tx SET to_account='Ledger 2' WHERE tx_id='L1'")  # Gegenbuchung nach der Entscheidung geändert
        cx.invalidate_data()
        b = TK.assess(cx, sid)
        assert not b.decisions and b.stale and "Gegenbuchung" in b.stale[0]["why"]
        assert not b.ok and "verworfen" in client.get(f"/settings/datasources/{sid}/uebernahme").text
        before = _all_state(client)
        assert TK.apply(cx, sid, b.token)["errors"] and _all_state(client) == before
    finally:
        client.__exit__(None, None, None)


def test_R03_decisions_need_the_shown_preview_and_a_new_fetch_discards_them(config, master, api):
    client, sid = _refusing(config, api)
    try:
        cx = ctx(client)
        a = staged(client, sid)
        key = _row(a)["key"]
        assert TK.decide(cx, sid, key, "pair", "veraltet")["errors"]  # parallele alte Vorschau
        assert TK.decide(cx, sid, key, "include", a.token)["errors"]  # nicht angebotene Antwort
        assert TK.decide(cx, sid, key, "pair", a.token)["errors"] == []
        assert TK.decide(cx, sid, key, "skip", a.token)["errors"]  # Token hat sich mit der Entscheidung geändert
        assert TK.assess(cx, sid).decisions
        staged(client, sid)  # neuer Vollabruf
        n = TK.assess(cx, sid)
        assert not n.decisions and not n.stale and [b.key for b in n.blockers] == ["rows"]
    finally:
        client.__exit__(None, None, None)


def test_R03_probe_cache_follows_the_decision(config, master, api, monkeypatch):
    client, sid = _refusing(config, api)
    try:
        cx = ctx(client)
        a = staged(client, sid)
        runs = []
        real = TK._apply_steps
        monkeypatch.setattr(TK, "_apply_steps", lambda *x, **k: runs.append(1) or real(*x, **k))
        TK.assess(cx, sid)
        assert runs == []  # unveränderter Stand: Probelauf aus dem Zwischenspeicher
        TK.decide(cx, sid, _row(a)["key"], "pair", a.token)
        TK.assess(cx, sid)
        assert runs == [1]  # die Entscheidung erzwingt einen neuen Probelauf (neues Token)
        TK.assess(cx, sid)
        assert runs == [1]
    finally:
        client.__exit__(None, None, None)


def _journal_deposits(client, *specs):
    """Zugänge auf anderen Konten als echte Journalbuchungen (über den Prüf-Stapel gebucht)."""
    from tests.test_diagnosis import PF_HEADER, upload

    cx = ctx(client)
    svc = csv_service(cx)
    lines = "".join(f"J{i},{ts},deposit,,,,,{acc},BTC,{qty},,,,55,j\n" for i, (ts, acc, qty) in enumerate(specs))
    bid = upload(client, "journal.csv", PF_HEADER + lines)
    svc.evaluate(bid)
    rows = svc.rows(bid)
    idxs = [r.idx for r in rows]
    own = dict.fromkeys(idxs, "nopair")  # ausdrücklich kein Transfer mit der (noch sichtbaren) Import-Auszahlung K3
    p = BA.plan(svc, bid, "include", idxs, True, approved=own)
    assert p.todo, [i.note for i in p.items]
    assert BA.execute(svc, bid, "include", idxs, token=BA.new_token(), fingerprint=p.fingerprint(), force=True,
                      approved=own)["errors"] == []
    cx.invalidate_data()
    return [r["tx_id"] for r in cx.db.q("SELECT tx_id FROM journal_tx WHERE batch_id=? AND status='active' "
                                        "ORDER BY tx_id", (bid,))]


def _without_counterpart(config, api):
    from tests.helpers import tx

    rows = [tx("K1", "2024-02-01T08:00:00Z", "deposit", to=("Bitpanda", "EUR", "500"), value="500"),
            tx("K2", "2024-02-02T09:01:00Z", "buy", frm=("Bitpanda", "EUR", "250"), to=("Bitpanda", "BTC", "0.0055"),
               value="251.5"),
            tx("K3", "2024-02-12T12:00:00Z", "withdrawal", frm=("Bitpanda", "BTC", "0.001"), value="55")]
    return started(config, api, rows=rows)


def test_R03_confirmed_journal_partner_is_merged_and_fully_undone(config, master, api):
    client, sid = _without_counterpart(config, api)
    try:
        cx = ctx(client)
        (jtx,) = _journal_deposits(client, ("2024-02-12T12:30:00Z", "Ledger", "0.001"))
        before = _all_state(client)
        a = staged(client, sid)
        p = _row(a)
        assert p["partner"]["kind"] == "j" and p["partner"]["tx"] == jtx and "pair" in p["options"]
        assert TK.decide(cx, sid, p["key"], "pair", a.token)["errors"] == []
        b = TK.assess(cx, sid)
        assert b.ok, b.blockers
        assert TK.apply(cx, sid, b.token)["errors"] == []
        cx.invalidate_data()
        assert cx.db.scalar("SELECT status FROM journal_tx WHERE tx_id=?", (jtx,)) == "merged"
        assert dict(cx.ledger().holdings_by_account("Ledger")) == before[1]["Ledger"]
        assert TK.undo(cx, sid)["errors"] == []
        cx.invalidate_data()
        assert _all_state(client) == before  # Journalpartner wieder aktiv, nicht mehr zusammengeführt
    finally:
        client.__exit__(None, None, None)


def test_R03_journal_partner_change_after_decision_makes_it_stale(config, master, api):
    client, sid = _without_counterpart(config, api)
    try:
        cx = ctx(client)
        (jtx,) = _journal_deposits(client, ("2024-02-12T12:30:00Z", "Ledger", "0.001"))
        a = staged(client, sid)
        assert TK.decide(cx, sid, _row(a)["key"], "pair", a.token)["errors"] == []
        cx.db.x("UPDATE journal_tx SET value_eur='99' WHERE tx_id=?", (jtx,))
        cx.invalidate_data()
        b = TK.assess(cx, sid)
        assert not b.decisions and b.stale and not b.ok
    finally:
        client.__exit__(None, None, None)


def test_R03_competing_partners_offer_no_silent_choice(config, master, api):
    client, sid = _without_counterpart(config, api)
    try:
        cx = ctx(client)
        _journal_deposits(client, ("2024-02-12T12:30:00Z", "Ledger A", "0.001"),
                          ("2024-02-12T12:31:00Z", "Ledger B", "0.001"))
        before = _all_state(client)
        a = staged(client, sid)
        p = _row(a)
        assert p["partner"]["kind"] == "alts" and p["options"] == ["nopair", "skip"]
        assert TK.decide(cx, sid, p["key"], "pair", a.token)["errors"]  # kein Partner wird still gewählt
        assert TK.decide(cx, sid, p["key"], "nopair", a.token)["errors"] == []
        b = TK.assess(cx, sid)
        assert b.ok, b.blockers
        assert TK.apply(cx, sid, b.token)["errors"] == []
        assert TK.undo(cx, sid)["errors"] == []
        cx.invalidate_data()
        assert _all_state(client) == before
    finally:
        client.__exit__(None, None, None)
