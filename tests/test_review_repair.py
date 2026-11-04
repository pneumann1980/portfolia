"""0.82.0 Prüfablauf: Quellenübernahme mit einzeln bearbeitbaren Problemen (Gründe je Zeile, Asset-Zuordnung nur für
den Anbieter, „nicht geliefert“ ≠ 0, Zählung, gleiche Kürzel unter mehreren Kennungen), Rückweg/Überspringen mit
Aufgabenbezug und die Nachprüfung der Klärung mit derselben Vergleichsbasis wie die Vorschau."""

# ruff: noqa: F811  (Fixtures aus test_bitpanda werden importiert und als Argumente verwendet)
from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from app.csvimport.service import SymbolResolver, csv_service
from app.datasources import takeover as TK
from app.diagnosis import clarify as K
from app.diagnosis.model import Finding, Report
from app.web import flow
from tests.helpers import tx
from tests.test_bitpanda import U, api, client, ctx, make_client, master  # noqa: F401  (Fixtures)
from tests.test_takeover import ops, started, sync_txs

D = Decimal
LUNC = U(0xC1)


def lunc_ops(n: int = 3) -> list[dict]:
    """Drei Vorgänge mit einem Asset, das Portfolia nicht kennt (Bitpanda-Kürzel LUNC, „Terra Classic“)."""
    base = ops(5)[0]  # Prämie in einem Krypto-Asset
    out = []
    for i in range(n):
        o = json.loads(json.dumps(base).replace(U(0xE1), LUNC).replace(base["operation_id"], U(0x1C0 + i)))
        for j, t in enumerate(o["transactions"]):
            t["transaction_id"] = U(0x2C00 + 16 * i + j)
        out.append(o)
    return out


def with_lunc(api_) -> None:
    api_.assets[LUNC] = {"id": LUNC, "name": "Terra Classic", "symbol": "LUNC", "isin": None, "group": "CRYPTOCOIN",
                         "type": "CRYPTO", "buy_active": True, "sell_active": True, "withdrawal_active": True,
                         "deposit_active": True}
    api_.set_pages({"data": ops(1, 3, 4) + lunc_ops(), "self_cursor": None, "next_cursor": None,
                    "has_next_page": False})


# ---- SOL-Muster: „alle Kriterien erfüllt“, beim Übernehmen trotzdem zurück -------------------------------------

def test_rows_needing_a_decision_are_refused_before_booking_with_reason_and_block_until_data_changes(config, master,
                                                                                                     api):
    rows = [tx("K1", "2024-02-01T08:00:00Z", "deposit", to=("Bitpanda", "EUR", "500"), value="500"),
            tx("K2", "2024-02-02T09:01:00Z", "buy", frm=("Bitpanda", "EUR", "250"), to=("Bitpanda", "BTC", "0.0055"),
               value="251.5"),
            tx("K3", "2024-02-12T12:00:00Z", "withdrawal", frm=("Bitpanda", "BTC", "0.001"), value="55"),
            # Gegenstück auf einem anderen Konto: die API-Auszahlung ist ein möglicher Transfer
            tx("L1", "2024-02-12T12:30:00Z", "deposit", to=("Ledger", "BTC", "0.001"), value="55")]
    c, sid = started(config, api, rows=rows)
    try:
        TK.run_fetch(ctx(c), sid)
        a = TK.assess(ctx(c), sid)
        assert a.ok  # die Vorschau sieht keinen Grund – erst das Buchen klassifiziert die Zeile
        before = {t.tx_id for t in ctx(c).recorded_portfolio().txs}
        res = TK.apply(ctx(c), sid, a.token)
        assert res["errors"] and "Einzelentscheidung" in res["errors"][0] and "Zurückgenommen" in res["errors"][0]
        assert any("Zeile 3" in e and "withdrawal" in e for e in res["errors"])  # Grund je Zeile, nicht leer
        assert not sync_txs(c) and {t.tx_id for t in ctx(c).recorded_portfolio().txs} == before  # nichts gebucht
        t = TK.latest(ctx(c).db, sid)
        doc = json.loads(t["result_json"])
        assert t["status"] == "staged" and doc["rolled_back"] and doc["rows"][0]["key"].endswith("104#0")
        # unveränderter Datenstand: die Vorschau sperrt jetzt mit genau diesem Problem (stabile Kennung)
        a2 = TK.assess(ctx(c), sid)
        assert not a2.ok and [b.key for b in a2.blockers] == ["rows"]
        assert [p["id"] for p in a2.problems] == ["row:" + doc["rows"][0]["key"]]
        page = c.get(f"/settings/datasources/{sid}/uebernahme").text
        assert "braucht eine Einzelentscheidung" in page and "Prüfstapel" in page
        # Datenstand ändert sich (hier: irgendeine Zuordnung) → neuer Versuch wieder möglich, nichts verloren
        csv_service(ctx(c)).set_symbol("XYZ@KRAKEN", None)
        assert TK.assess(ctx(c), sid).ok
    finally:
        c.__exit__(None, None, None)


# ---- Bitpanda: LUNC zuordnen, ohne erneuten Abruf, nur für diesen Anbieter ------------------------------------------

def test_lunc_mapping_fixes_all_three_rows_without_refetch_or_booking_and_survives_restart(config, master, api):
    c, sid = started(config, api)
    with_lunc(api)
    try:
        TK.run_fetch(ctx(c), sid)
        a = TK.assess(ctx(c), sid)
        maps = [p for p in a.problems if p["kind"] == "map"]
        assert [p["id"] for p in maps] == ["map:LUNC@BITPANDA"] and len(maps[0]["events"]) == 3
        assert all(e["event"].startswith("bitpanda:") and len(e["event"]) > 30 for e in maps[0]["events"])  # volle ID
        assert "lines" in [b.key for b in a.blockers]
        page = c.get(f"/settings/datasources/{sid}/uebernahme?kid=abc12345").text
        assert "Asset für „LUNC“ zuordnen" in page and "LUNC@BITPANDA" in page and "LUNA" in page
        calls = len(api.calls)
        r = c.post(f"/settings/datasources/{sid}/uebernahme/zuordnen",
                   data={"csrf_token": c.token, "symbol": "LUNC", "action": "new", "new_id": "TERRA-CLASSIC",
                         "new_name": "Terra Classic", "new_class": "crypto", "kid": "abc12345"}, follow_redirects=False)
        assert r.status_code == 303 and "kid=abc12345" in r.headers["location"] and "msg=" in r.headers["location"]
        assert len(api.calls) == calls  # kein erneuter Abruf
        assert not sync_txs(c)  # Zuordnen bucht nichts
        a2 = TK.assess(ctx(c), sid)
        assert not [p for p in a2.problems if p["kind"] == "map"]
        assert next(x for x in a2.checks if x.key == "lines").ok
        assert not a2.ok and any(p["kind"] == "balance" for p in a2.problems)  # Bestand bleibt offen und sichtbar
        lunc = next(b for b in a2.balances if b["asset"] == "TERRA-CLASSIC")
        assert not lunc["known"]  # /portfolio meldet LUNC nicht – „nicht geliefert“, nicht „0“
        # Geltungsbereich: nur Bitpanda – derselbe Kürzel bei einem anderen Anbieter bleibt ungeklärt
        res = SymbolResolver(csv_service(ctx(c)).known_assets(), csv_service(ctx(c)).saved_symbols())
        assert res.resolve_for("LUNC", "bitpanda")[0] == "TERRA-CLASSIC"
        assert res.resolve_for("LUNC", "kraken")[0] is None
        # Zuordnung für ein unbekanntes Kürzel wird abgelehnt
        r = c.post(f"/settings/datasources/{sid}/uebernahme/zuordnen",
                   data={"csrf_token": c.token, "symbol": "BTC", "action": "map", "asset": "EUR"},
                   follow_redirects=False)
        assert "error=" in r.headers["location"]
    finally:
        c.__exit__(None, None, None)
    c2 = make_client(config)  # Neustart: Zuordnung und gespeicherter Abruf bleiben
    c2.__enter__()
    try:
        a3 = TK.assess(ctx(c2), sid)
        assert a3 is not None and not [p for p in a3.problems if p["kind"] == "map"]
        c2.get("/settings")
        c2.token = c2.cookies.get("portfolia_csrf")
        r = c2.post(f"/settings/datasources/{sid}/uebernahme/zuordnung-entfernen",
                    data={"csrf_token": c2.token, "key": "LUNC@BITPANDA"}, follow_redirects=False)
        assert "msg=" in r.headers["location"]
        assert [p["id"] for p in TK.assess(ctx(c2), sid).problems if p["kind"] == "map"] == ["map:LUNC@BITPANDA"]
    finally:
        c2.__exit__(None, None, None)


def test_observed_missing_is_not_zero_and_counts_are_explained():
    class R:
        def resolve_for(self, raw, provider):
            return ({"EUR": "EUR", "USD": "USD", "BTC": "BTC"}.get(raw), "symbol", raw)

    obs, unmapped, delivered = TK._observed({"balances": [{"key": "EUR", "qty": "0"}, {"key": "BTC", "qty": "1"},
                                                          {"key": "XXX", "qty": "2"}]}, R(), "bitpanda")
    assert obs == {"BTC": D(1)} and unmapped == ["XXX"] and delivered == {"EUR", "BTC"}  # USD: nicht geliefert
    a = SimpleNamespace(events=315, lines=323, skipped={"Bitpanda: stake": 2})
    assert TK._counts({"operations": 317}, a) == {"api": 317, "events": 315, "lines": 323, "skipped": 2,
                                                  "explained": True, "gap": 0}
    assert TK._counts({"operations": 318}, a)["gap"] == 1


def test_same_symbol_under_two_ids_is_shown_as_identity_question_without_merging():
    bal = [{"asset": "ACN", "name": "ACN", "now": D("1134.26738992"), "after": D("2268.53477984"),
            "observed": D("1134.26738992"), "ok": False, "not_worse": False, "negative": False, "known": True},
           {"asset": "ACN#16721599", "name": "ACN (ACN#16721599)", "now": D(0), "after": D("-1134.26738992"),
            "observed": D(0), "ok": False, "not_worse": False, "negative": True, "known": False}]
    a = SimpleNamespace(balances=bal, mode="proof", confirm=False, breakdown={})
    probs = TK._problems(a, [], [], "bitpanda", None)
    ident = next(p for p in probs if p["kind"] == "ident")
    assert ident["id"] == "ident:ACN" and ident["assets"] == ["ACN", "ACN#16721599"]
    assert "1.134,26738992" in ident["detail"] and "nur bei belegter gleicher Identität" in ident["detail"]
    neg = next(p for p in probs if p["id"] == "bal:ACN#16721599")
    assert "würde negativ" in neg["detail"] and "nicht geliefert" in neg["detail"]  # nicht durch Summe verdeckt


# ---- Rückweg, Überspringen, Aufgabenbezug ------------------------------------------------------------------------

def test_takeover_page_has_return_and_targeted_skip_with_and_without_cookie(config, master, api):
    c, sid = started(config, api)
    with_lunc(api)
    try:
        TK.run_fetch(ctx(c), sid)
        for cookie in ("", "1"):
            c.cookies.set("dq_flow", cookie)
            page = c.get(f"/settings/datasources/{sid}/uebernahme?kid=tab-one").text
            assert "Zurück zu Buchungen prüfen" in page and "/quality/assistent?skip=tab-one" in page
            assert 'name="kid" value="tab-one"' in page and "Übernahme vorerst überspringen" in page
        c.cookies.set("dq_cur", "other-tab")  # ein anderer Tab: das Überspringen trifft trotzdem die eigene Aufgabe
        page = c.get(f"/settings/datasources/{sid}/uebernahme?kid=tab-one").text
        assert "skip=tab-one" in page and "skip=other-tab" not in page
        assert "skip=evil" not in c.get(f"/settings/datasources/{sid}/uebernahme?kid=evil%22%3E").text
        r = c.post(f"/settings/datasources/{sid}/uebernahme/verwerfen", data={"csrf_token": c.token, "kid": "tab-one"},
                   follow_redirects=False)
        assert "kid=tab-one" in r.headers["location"]
        # Verwerfen trifft nur den Abruf; Zuordnungen und Quelle bleiben
        assert TK.latest(ctx(c).db, sid) is None
    finally:
        c.__exit__(None, None, None)


def test_assistant_hands_the_task_to_the_takeover_page(config, master, api):
    c, sid = started(config, api)
    try:
        r = c.post("/quality/assistent/antwort", data={"csrf_token": c.token, "typ": "vorrang", "f": str(sid),
                                                       "a": "vr:yes", "kid": "vr-123"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == f"/settings/datasources/{sid}/uebernahme?kid=vr-123"
        from app.diagnosis.assistant_web import with_kid

        assert with_kid("/quality/klaerung/k1#weg", "ab:1") == "/quality/klaerung/k1?kid=ab%3A1#weg"
        assert with_kid("/x?y=1", "bad kid!") == "/x?y=1"
    finally:
        c.__exit__(None, None, None)


def test_flow_keeps_errors_and_partial_progress_at_the_problem():
    assert flow.rewrite("/quality/klaerung/k1?err=Datenstand+ge%C3%A4ndert") is None
    assert flow.rewrite("/quality/klaerung/k1?msg=1+Korrektur&stay=1") is None
    assert flow.rewrite("/quality/klaerung/k1?msg=Fertig") == "/quality/assistent?msg=Fertig"
    assert not flow._DONE.match("/quality/klaerung/k1/bestand")  # Nachweis speichern ist ein Zwischenschritt


# ---- Klärung: Nachprüfung mit derselben Vergleichsbasis wie die Vorschau -------------------------------------------

def _f(key: str, kind: str, status: str = "belegt", title: str = "") -> Finding:
    return Finding(kind=kind, status=status, title=title or key, key=key, positions=[("SOL1", "SOL")])


def _verify(monkeypatch, before: list[Finding], after: list[Finding], case_findings: list[Finding],
            targets: list[Finding], *, with_base: bool = True):
    monkeypatch.setattr(K, "report_for", lambda ctx: Report(after, [], datetime.now(UTC).date()))
    case = SimpleNamespace(findings=case_findings, positions=[("SOL1", "SOL")],
                           baseline=SimpleNamespace(stock_diffs={}))
    hyp = SimpleNamespace(cands=[SimpleNamespace(finding_id=t.id, plan=SimpleNamespace(ops=[])) for t in targets])
    return K.verify(None, case, hyp, Report(before, [], datetime.now(UTC).date()) if with_base else None)


def test_unchanged_old_finding_outside_the_group_does_not_undo_a_correct_fix(monkeypatch):
    fee = _f("sol-fee", "duplicate", title="SOL-Transfer: Gebühr doppelt")
    old = _f("sol-stock", "holdings", title="Bereits bekannte Bestandsabweichung")
    ok, eff = _verify(monkeypatch, [fee, old], [old], [fee], [fee])
    assert ok and eff["behoben"] == 1 and eff["probleme"] == []
    # alte Vergleichsbasis (nur die Befunde der Gruppe) hätte den Altbefund als „neu“ gewertet → Rücknahme
    ok_old, eff_old = _verify(monkeypatch, [fee, old], [old], [fee], [fee], with_base=False)
    assert not ok_old and "neuer Befund: Bereits bekannte Bestandsabweichung" in eff_old["probleme"][0]


def test_really_new_or_worse_findings_still_block(monkeypatch):
    fee = _f("sol-fee", "duplicate")
    old = _f("sol-stock", "holdings", status="verdacht")
    new = _f("sol-new", "holdings", title="Neu verursachte Abweichung")
    ok, eff = _verify(monkeypatch, [fee, old], [old, new], [fee], [fee])
    assert not ok and eff["probleme"] == ["neuer Befund: Neu verursachte Abweichung"]
    worse = _f("sol-stock", "holdings", status="belegt", title="Bestandsabweichung")
    ok, eff = _verify(monkeypatch, [fee, old], [worse], [fee], [fee])
    assert not ok and eff["probleme"] == ["verschlechtert: Bestandsabweichung"]  # gleiche Kennung, schlimmer
    ok, eff = _verify(monkeypatch, [fee], [fee], [fee], [fee])
    assert not ok and "bestehen weiter" in eff["probleme"][0]


def test_untargeted_findings_of_the_case_stay_open_and_are_reported(monkeypatch):
    fee = _f("sol-fee", "duplicate")
    rest = _f("sol-transfer", "transfer", title="Transfer ohne Gegenstück")
    ok, eff = _verify(monkeypatch, [fee, rest], [rest], [fee, rest], [fee])
    assert ok and eff["offen"] == ["Transfer ohne Gegenstück"]  # Korrektur bleibt, Fall ist nicht „gelöst“


def test_finish_screen_never_claims_all_checked_with_skipped_tasks(config, master, api, monkeypatch):
    from app.diagnosis import guide as G

    c, _sid = started(config, api)
    try:
        monkeypatch.setattr(G, "step", lambda ctx, skipped, hints=False, focus="": SimpleNamespace(
            task=None, skipped=len(skipped), themes=[], position=0, total=0, minutes=0))
        c.cookies.set("dq_skip", "abcdef01,12345678")
        page = c.get("/quality/assistent").text
        assert "Für diesen Durchgang fertig" in page and "Alles geprüft" not in page and "2 Aufgaben" in page
        c.cookies.set("dq_skip", "")
        assert "Alles geprüft" in c.get("/quality/assistent").text
    finally:
        c.__exit__(None, None, None)


def test_two_bitpanda_ops_skipped_explain_the_difference_on_the_page(config, master, api):
    c, sid = started(config, api)
    try:
        api.set_pages({"data": ops(1, 3, 4, 0x10), "self_cursor": None, "next_cursor": None, "has_next_page": False})
        TK.run_fetch(ctx(c), sid)
        a = TK.assess(ctx(c), sid)
        assert a.counts["api"] == a.counts["events"] + a.counts["skipped"] and a.counts["explained"]
        page = c.get(f"/settings/datasources/{sid}/uebernahme").text
        assert "API-Vorgänge = " in page and "vollständig erklärt" in page
    finally:
        c.__exit__(None, None, None)


def test_page_offers_fiat_investigation_with_separate_levels(config, master, api):
    c, sid = started(config, api)
    try:
        api.portfolio = copy.deepcopy(api.portfolio)
        for h in api.portfolio["data"]:
            if h.get("asset_id") is None:
                h["balance"]["value"] = h["available_balance"]["value"] = "0"  # Quelle meldet EUR ausdrücklich 0
        TK.run_fetch(ctx(c), sid)
        a = TK.assess(ctx(c), sid)
        eur = next(p for p in a.problems if p["id"] == "bal:EUR")
        assert eur["parts"] and any(k.startswith("API: ") for k, _v in eur["parts"])
        assert next(b for b in a.balances if b["asset"] == "EUR")["known"]  # 0 gemeldet ≠ nicht geliefert
        assert "EUR" in a.raw_check and a.raw_check["EUR"][0]["portfolio"] == "0"
        page = c.get(f"/settings/datasources/{sid}/uebernahme").text
        assert "Abweichung untersuchen" in page and "Letzter Saldo laut Vorgängen" in page
        assert "keine Ausgleichsbuchung" in page
    finally:
        c.__exit__(None, None, None)
