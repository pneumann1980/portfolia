"""0.94.2: PulseChain-Übernahme ohne Sackgasse bei Sammelvorgängen („Mehrere Zu- und Abgänge“).

Fehlerbild: Ein Tausch mit beigelegtem Spam-Token (−PLS, +HEX, +Spam) wurde ein ungeklärter Sammelvorgang. In der
Quellenübernahme war er „nicht buchbar“, die Problemkarte hatte keine Bedienung, der Spam-Token darin bekam keine
Zuordnungskarte – die Übernahme blieb trotz „Als Spam ignorieren“ der übrigen Token gesperrt."""

# ruff: noqa: F811  (Fixtures werden importiert und als Argumente verwendet)
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.csvimport import model as M
from app.csvimport.service import Valuer, csv_service
from app.datasources import takeover as TK
from app.datasources.wallet import Move, TxView, classify, reclassify
from tests.test_takeover import wait
from tests.test_wallets_pulse_peaq import FORK, WEI, A, X, client, pulse, tx  # noqa: F401  (Fixtures)
from tests.wallet_fakes import create_wallet, ctx, post

D = Decimal
R = "0x4444444444444444444444444444444444444444"
HEX = "0x" + "cd" * 20
LP = "0x" + "12" * 20
SP1 = "0x" + "ab" * 20
SP2 = "0x" + "ef" * 20
TS = datetime(2024, 1, 1, tzinfo=UTC)


@pytest.fixture
def priced(monkeypatch):
    """Kurse für alle Krypto-Assets (im Test gibt es keine Kursquelle) – Vorgänge mit EUR-Wert."""
    real = Valuer._price

    def price(self, aid, d):
        a = self.assets.get(aid)
        return real(self, aid, d) if a is None or a.is_fiat else (D("0.0001"), "Testkurs")

    monkeypatch.setattr(Valuer, "_price", price)


def _tok(block: int, n: int, frm: str, to: str, qty: int, c: str, sym: str, name: str) -> dict:
    return {**tx(block, n, frm, to, qty * 10**18), "contractAddress": c, "tokenSymbol": sym, "tokenName": name,
            "tokenDecimal": "18"}


def _swap_call(block: int, n: int, value: int) -> dict:
    return {**tx(block, n, A, R, value), "input": "0x38ed1739", "methodId": "0x38ed1739",
            "functionName": "swapExactETHForTokens"}


def _maps(a) -> list[str]:
    return sorted(p["symbol"].split("@")[0] for p in a.problems if p["kind"] == "map")


def _resolve_maps(c, sid, spam: tuple[str, ...]) -> None:
    for p in [p for p in TK.assess(ctx(c), sid).problems if p["kind"] == "map"]:
        base = p["symbol"].split("@")[0]
        form = {"action": "spam"} if base in spam else {"action": "new", "new_id": base, "new_name": base,
                                                          "new_class": "crypto"}
        r = post(c, f"/settings/datasources/{sid}/uebernahme/zuordnen", symbol=p["symbol"], **form)
        assert "msg=" in r.headers["location"], r.headers["location"]


def _decide_rows(c, sid, prefer: str = "include") -> int:
    """Wie ein Nutzer: jede vom Probelauf verlangte Einzelentscheidung treffen (bevorzugt ``prefer``)."""
    for n in range(20):
        a = TK.assess(ctx(c), sid, confirm=True)
        todo = [p for p in a.problems if p["kind"] == "row" and p.get("options")]
        if not todo:
            return n
        p = todo[0]
        choice = prefer if prefer in p["options"] else p["options"][-1]
        r = post(c, f"/settings/datasources/{sid}/uebernahme/entscheidung", key=p["key"], choice=choice,
                 token=a.token, confirm="1")
        assert "msg=" in r.headers["location"], r.headers["location"]
    raise AssertionError("Entscheidungen enden nicht")


def _sync_txs(c) -> list[dict]:
    return [dict(r) for r in ctx(c).db.q("SELECT * FROM journal_tx WHERE source='sync:pulsechain' AND "
                                         "status='active' ORDER BY id")]


def _apply_and_undo(c, sid) -> list[dict]:
    ok = TK.assess(ctx(c), sid, confirm=True)
    assert ok.ok, [(b.key, b.detail) for b in ok.blockers]
    r = post(c, f"/settings/datasources/{sid}/uebernahme/uebernehmen", token=ok.token, confirm="1", confirmed="1")
    assert r.status_code == 303
    wait(sid)
    last = dict(TK.latest(ctx(c).db, sid))
    assert last["status"] == "applied", last
    booked = _sync_txs(c)
    back = post(c, f"/settings/datasources/{sid}/uebernahme/zurueck")
    assert back.status_code == 303 and not _sync_txs(c)
    assert TK.latest(ctx(c).db, sid)["status"] == "undone"
    return booked


# -- Einordnung (Connector) ---------------------------------------------------------------------------------------

def test_spam_beside_a_swap_is_its_own_line_and_the_swap_stays_a_trade():
    t = TxView("pulsechain", "0xh", TS, [Move("PLS", D(-2), "n:out"), Move("HEX@PLS:0xc", D(10), "t:1#0"),
                                         Move("SP@PLS:0xs", D(99), "t:2#1", spam="Link im Namen")],
               fee=D("0.01"), fee_asset="PLS", initiated=True, plain=False)
    recs = classify(t)
    kinds = sorted((r.kind, r.out_sym or "", r.in_sym or "") for r in recs)
    assert kinds == [(M.DEPOSIT, "", "SP@PLS:0xs"), (M.TRADE, "PLS", "HEX@PLS:0xc")]
    trade = next(r for r in recs if r.kind == M.TRADE)
    assert (trade.out_qty, trade.in_sym, trade.in_qty, trade.fee_qty) == (D(2), "HEX@PLS:0xc", D(10), D("0.01"))
    spam = next(r for r in recs if r.kind == M.DEPOSIT)
    assert "Spam" in spam.review and spam.ext_id == "t:2#1"
    assert len({r.ext_id for r in recs}) == len(recs)


def test_only_spam_moved_keeps_the_paid_fee_as_its_own_line():
    t = TxView("pulsechain", "0xh", TS, [Move("SP@PLS:0xs", D(5), "t:1#0", spam="Werbung")], fee=D("0.02"),
               fee_asset="PLS", initiated=True, plain=False)
    recs = classify(t)
    assert sorted(r.kind for r in recs) == sorted([M.DEPOSIT, M.FEE])
    assert next(r for r in recs if r.kind == M.FEE).fee_qty == D("0.02")


def test_complex_transaction_keeps_its_moves_and_reclassifies_without_ignored_tokens():
    t = TxView("pulsechain", "0xh", TS, [Move("PLS", D(-2), "n:out"), Move("HEX@PLS:0xc", D(10), "t:1#0"),
                                         Move("X@PLS:0xx", D(99), "t:2#1")],
               fee=D("0.01"), fee_asset="PLS", initiated=True, plain=False, label="swap")
    (review, _fee) = sorted(classify(t), key=lambda r: r.kind != M.REVIEW)
    assert review.kind == M.REVIEW and review.raw["moves"]["v"] == 1
    assert reclassify(review, set()) is None and reclassify(review, {"NOPE"}) is None
    again = reclassify(review, {"X@PLS:0xx"})
    assert [(r.kind, r.out_sym, r.in_sym, r.fee_qty) for r in again] == [(M.TRADE, "PLS", "HEX@PLS:0xc", D("0.01"))]
    # älterer Abruf ohne gespeicherte Bewegungen: keine Neueinordnung (kein Raten)
    review.raw = {k: v for k, v in review.raw.items() if k != "moves"}
    assert reclassify(review, {"X@PLS:0xx"}) is None


# -- Bedienweg Quellenübernahme ---------------------------------------------------------------------------------

def _swap_with_spam(c, fake, spam_name: str) -> int:
    d = fake.chains[369]
    d["txlist"].append(_swap_call(FORK + 400, 20, 2 * WEI))
    d["tokentx"] = [_tok(FORK + 400, 20, R, A, 10, HEX, "HEX", "HEX"),
                    _tok(FORK + 400, 20, R, A, 99, SP2, "SPM2", spam_name),
                    _tok(FORK + 500, 21, X, A, 1000, SP1, "SPM1", "Spam Token")]
    d["tokenbalance"] = {HEX: str(10 * 10**18), SP1: str(1000 * 10**18), SP2: str(99 * 10**18)}
    sid = create_wallet(c, "pulsechain", A, name="Ledger PLS")
    assert not TK.run_fetch(ctx(c), sid)["errors"]
    return sid


def test_user_marked_spam_inside_a_swap_gets_a_map_card_and_unblocks_the_takeover(client, pulse, priced):
    c = client
    sid = _swap_with_spam(c, pulse, "Totally Normal")  # unauffälliger Name: nur der Nutzer erkennt den Spam
    a = TK.assess(ctx(c), sid)
    assert _maps(a) == ["HEX", "PLS", "SPM1", "SPM2"]  # auch der Token im Sammelvorgang ist einzeln zuordenbar
    _resolve_maps(c, sid, spam=("SPM1", "SPM2"))
    b = TK.assess(ctx(c), sid, confirm=True)
    assert not _maps(b) and {x.key for x in b.blockers} <= {"rows"}, [(x.key, x.detail) for x in b.blockers]
    assert _decide_rows(c, sid) == 2  # Fork-Eröffnung und Tausch: prüfbedürftig, je eine Entscheidung
    b = TK.assess(ctx(c), sid, confirm=True)
    assert b.ok, [(x.key, x.detail) for x in b.blockers]
    assert not [p for p in b.problems if p["kind"] == "line"]
    booked = _apply_and_undo(c, sid)
    trade = [t for t in booked if t["from_asset"] == "PLS" and t["to_asset"] == "HEX"]
    assert len(trade) == 1 and D(trade[0]["from_qty"]) == 2 and D(trade[0]["to_qty"]) == 10
    assert not [t for t in booked if "SPM" in (t["from_asset"] or "") + (t["to_asset"] or "")]
    # Spam-Markierung zurücknehmen: der Vorgang ist wieder ein Sammelvorgang mit Zuordnungskarte
    key = next(k for k in csv_service(ctx(c)).saved_symbols() if k.startswith("SPM2@"))
    assert not TK.run_fetch(ctx(c), sid)["errors"]  # nach der Rücknahme: neuer Vollabruf
    post(c, f"/settings/datasources/{sid}/uebernahme/zuordnung-entfernen", key=key)
    again = TK.assess(ctx(c), sid)
    assert "SPM2" in _maps(again)
    assert "SPM2" in next(b.detail for b in again.blockers if b.key == "lines")  # wieder Sammelvorgang


def test_spam_detected_by_name_inside_a_swap_is_split_off_at_fetch(client, pulse, priced):
    c = client
    sid = _swap_with_spam(c, pulse, "visit www.claim-now.io")
    a = TK.assess(ctx(c), sid)
    assert not [p for p in a.problems if p["kind"] == "line"]  # kein Sammelvorgang mehr
    _resolve_maps(c, sid, spam=("SPM1", "SPM2"))
    _decide_rows(c, sid)
    assert TK.assess(ctx(c), sid, confirm=True).ok


def test_genuinely_complex_transaction_can_be_left_out_for_this_run(client, pulse, priced):
    c = client
    d = pulse.chains[369]
    d["txlist"].append(_swap_call(FORK + 400, 20, 2 * WEI))  # z. B. Liquidität: PLS hinein, zwei Token heraus
    d["tokentx"] = [_tok(FORK + 400, 20, R, A, 10, HEX, "HEX", "HEX"), _tok(FORK + 400, 20, R, A, 3, LP, "LP", "LP")]
    d["tokenbalance"] = {HEX: str(10 * 10**18), LP: str(3 * 10**18)}
    sid = create_wallet(c, "pulsechain", A, name="Ledger PLS")
    assert not TK.run_fetch(ctx(c), sid)["errors"]
    _resolve_maps(c, sid, spam=())
    a = TK.assess(ctx(c), sid, confirm=True)
    (line,) = [p for p in a.problems if p["kind"] == "line"]
    assert line["options"] == ["skip"] and "Mehrere Zu- und Abgänge" in line["detail"]
    assert "lines" in {b.key for b in a.blockers}
    page = c.get(f"/settings/datasources/{sid}/uebernahme?confirm=1").text
    assert 'name="choice" value="skip"' in page  # die Karte hat eine Bedienung
    r = post(c, f"/settings/datasources/{sid}/uebernahme/entscheidung", key=line["key"], choice="skip",
             token=a.token, confirm="1")
    assert "msg=" in r.headers["location"], r.headers["location"]
    b = TK.assess(ctx(c), sid, confirm=True)
    assert "lines" not in {x.key for x in b.blockers}
    assert line["key"] in b.decisions and b.decisions[line["key"]]["choice"] == "skip"
    assert not [p for p in b.problems if p["kind"] == "line"]
    # Entscheidung zurücknehmen: die Sperre kehrt zurück; erneut entscheiden
    stored = TK.load_decisions(ctx(c).db, sid, b.rid)[line["key"]]
    assert not TK.undecide(ctx(c), sid, line["key"], b.rid, TK.decision_stamp(stored))["errors"]
    again = TK.assess(ctx(c), sid, confirm=True)
    assert "lines" in {x.key for x in again.blockers}
    r = post(c, f"/settings/datasources/{sid}/uebernahme/entscheidung", key=line["key"], choice="skip",
             token=again.token, confirm="1")
    assert "msg=" in r.headers["location"], r.headers["location"]
    _decide_rows(c, sid)
    b = TK.assess(ctx(c), sid, confirm=True)
    # bestätigter Modus: ohne den Vorgang fehlen HEX und LP im erklärten Bestand, die Abweichung wird aber nicht
    # größer als vorher (nichts gebucht) – die Übernahme ist möglich, der Vorgang wird sichtbar nicht gebucht
    assert b.ok, [(x.key, x.detail) for x in b.blockers]
    booked = _apply_and_undo(c, sid)
    assert booked and not [t for t in booked if t["to_asset"] in ("HEX", "LP")]

def test_rows_without_eur_value_are_never_left_out_silently(client, pulse):
    """Ohne Kurs (EUR-Wert fehlt) bucht der Prüfstapel einen Tausch nicht. Früher meldete die Übernahme trotzdem
    „übernommen“ und ließ ihn offen liegen; jetzt nennt der Probelauf ihn vorher mit Entscheidung je Vorgang."""
    c = client
    sid = _swap_with_spam(c, pulse, "visit www.claim-now.io")
    _resolve_maps(c, sid, spam=("SPM1", "SPM2"))
    a = TK.assess(ctx(c), sid, confirm=True)
    assert not a.ok and "rows" in {b.key for b in a.blockers}, [(b.key, b.detail) for b in a.blockers]
    rows = [p for p in a.problems if p["kind"] == "row"]
    assert rows and all(p["options"] == ["skip"] for p in rows)
    assert all("EUR-Wert fehlt" in p["detail"] for p in rows), [p["detail"] for p in rows]
    assert not _sync_txs(c)  # Probelauf: nichts gebucht
    # direkt übernehmen: Abbruch vor jeder Buchung, nichts bleibt halb übernommen
    out = TK.apply(ctx(c), sid, a.token, True)
    assert out["errors"] and not _sync_txs(c)
    assert TK.latest(ctx(c).db, sid)["status"] == "staged"
    # bewusst nicht übernehmen (nur dieser Lauf) – danach ist die Übernahme möglich, die Vorgänge fehlen sichtbar
    for p in rows:
        cur = TK.assess(ctx(c), sid, confirm=True)
        r = post(c, f"/settings/datasources/{sid}/uebernahme/entscheidung", key=p["key"], choice="skip",
                 token=cur.token, confirm="1")
        assert "msg=" in r.headers["location"], r.headers["location"]
    b = TK.assess(ctx(c), sid, confirm=True)
    assert "rows" not in {x.key for x in b.blockers}, [(x.key, x.detail) for x in b.blockers]
    assert {p["key"] for p in rows} <= set(b.decisions)


def test_one_answer_for_all_offered_decisions_in_one_step(client, pulse, priced):
    """Viele prüfbedürftige Tauschvorgänge: eine ausdrückliche Antwort für alle angebotenen statt Klick je Vorgang."""
    c = client
    d = pulse.chains[369]
    d["tokentx"] = []
    for i in range(4):  # vier Tauschvorgänge PLS → HEX
        d["txlist"].append(_swap_call(FORK + 400 + i, 30 + i, WEI // 10))
        d["tokentx"].append(_tok(FORK + 400 + i, 30 + i, R, A, 1, HEX, "HEX", "HEX"))
    d["tokenbalance"] = {HEX: str(4 * 10**18)}
    sid = create_wallet(c, "pulsechain", A, name="Ledger PLS")
    assert not TK.run_fetch(ctx(c), sid)["errors"]
    _resolve_maps(c, sid, spam=())
    a = TK.assess(ctx(c), sid, confirm=True)
    offered = [p for p in a.problems if p["kind"] == "row" and "include" in p["options"]]
    assert len(offered) == 5  # vier Tauschvorgänge und die Fork-Eröffnung
    page = c.get(f"/settings/datasources/{sid}/uebernahme?confirm=1").text
    assert "entscheidung-alle" in page and "Alle 5: Einzeln buchen" in page
    stale = post(c, f"/settings/datasources/{sid}/uebernahme/entscheidung-alle", choice="include", token="x" * 32,
                 confirm="1")
    assert "error=" in stale.headers["location"] and not TK.load_decisions(ctx(c).db, sid, a.rid)
    r = post(c, f"/settings/datasources/{sid}/uebernahme/entscheidung-alle", choice="include", token=a.token,
             confirm="1")
    assert "msg=" in r.headers["location"], r.headers["location"]
    assert len(TK.load_decisions(ctx(c).db, sid, a.rid)) == 5
    b = TK.assess(ctx(c), sid, confirm=True)
    assert b.ok, [(x.key, x.detail) for x in b.blockers]
    booked = _apply_and_undo(c, sid)
    assert len([t for t in booked if t["to_asset"] == "HEX"]) == 4
