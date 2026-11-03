"""0.87.0 Quellenübernahme: bleibende Buchungen gegen die (vollständige) API prüfen.

* Transfer mit anderem Konto ↔ Einzahlung der API mit Zeitversatz (SEPA/Sparplan) → gedeckt, nicht doppelt gezählt.
* In der App erfasste Buchung, die auch in der API steht → als doppelt gezeigt, mit Zustimmung ausgeblendet.
* Ausgleich nur für eine Lücke, die der Saldoverlauf belegt – sonst Ursachen statt Ausgleichsbuchung."""

# ruff: noqa: F811  (Fixtures aus test_bitpanda werden importiert und als Argumente verwendet)
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from app.datasources import takeover as TK
from tests.helpers import tx
from tests.test_bitpanda import EUR_ID, U, api, client, ctx, master, oper, page, post, txn  # noqa: F401
from tests.test_takeover import holdings, ops, portfolio, started

D = Decimal
T0 = datetime(2024, 3, 1, 14, 45, tzinfo=UTC)
BASE = [tx("K1", "2024-02-01T08:00:00Z", "deposit", to=("Bitpanda", "EUR", "500"), value="500"),
        tx("K2", "2024-02-02T09:01:00Z", "buy", frm=("Bitpanda", "EUR", "250"), to=("Bitpanda", "BTC", "0.0055"),
           value="251.5"),
        tx("K3", "2024-02-12T12:00:00Z", "withdrawal", frm=("Bitpanda", "BTC", "0.001"), value="55")]


def iso(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def deposit_op(n: int, ts: datetime, amount: str, balance: str, typ: str = "deposit") -> dict:
    return oper(n, typ, txn(n, "INCOMING", amount, EUR_ID, iso(ts), ttype="deposit", balance=balance, wallet=U(0xF0)))


def test_transfer_with_lagged_api_deposit_is_covered_not_double_counted(config, master, api):
    # Sparplan-Einzahlungen im Import als Transfer vom Bankkonto; die API führt sie 3,8 Tage später (Gutschrift)
    rows = BASE + [tx(f"S{i}", iso(T0 + timedelta(days=7 * i)), "transfer", frm=("DKB Giro", "EUR", "50"),
                      to=("Bitpanda", "EUR", "50"), value="50") for i in range(3)]
    c, sid = started(config, api, rows=rows)
    api.set_pages(page(ops(1, 3, 4) + [deposit_op(50 + i, T0 + timedelta(days=7 * i + 3, hours=18), "50.00",
                                                  str(300 + 50 * i), "savings_plan") for i in range(3)]))
    api.portfolio = portfolio("400", "0.00445")
    try:
        TK.run_fetch(ctx(c), sid)
        a = TK.assess(ctx(c), sid)
        eur = {b["asset"]: b for b in a.balances}["EUR"]
        assert eur["after"] == eur["observed"] == D(400), eur  # vorher: 550 (Transfer + API-Einzahlung doppelt)
        assert a.covered == 3 and len(a.lagged_kept) == 3 and not a.kept_free and not a.adjustable
        assert a.ok, [(x.key, x.detail) for x in a.blockers]
        assert TK.apply(ctx(c), sid, a.token)["errors"] == []
        assert holdings(c) == {"EUR": D(400), "BTC": D("0.00445")}
        assert TK.undo(ctx(c), sid)["errors"] == []
    finally:
        c.__exit__(None, None, None)


def test_app_booking_also_in_api_is_shown_and_hidden_only_with_consent(config, master, api):
    c, sid = started(config, api, rows=BASE)
    api.set_pages(page([*ops(1, 3, 4), deposit_op(60, datetime(2024, 2, 25, 10, tzinfo=UTC), "10.00", "260")]))
    api.portfolio = portfolio("260", "0.00445")
    try:
        r = post(c, "/journal/new", kind="deposit", date="2024-02-25", account="Bitpanda", asset="EUR", qty="10")
        assert r.status_code == 303, r.text[:300]
        manual = ctx(c).db.q1("SELECT tx_id FROM journal_tx WHERE source='manual' AND status='active'")["tx_id"]
        TK.run_fetch(ctx(c), sid)
        a = TK.assess(ctx(c), sid)
        assert [d["tx"].tx_id for d in a.dups] == [manual] and a.dups[0]["hideable"] and not a.dups[0]["drop"]
        assert not a.ok and not a.adjustable  # Saldo lückenlos: kein Ausgleich, sondern Ursache
        cause = a.causes["EUR"]
        assert cause["deviation"] == D(10) and cause["complete"] and [d["qty"] for d in cause["dups"]] == [D(10)]
        page_ = c.get(f"/settings/datasources/{sid}/uebernahme").text
        assert "auch in der API" in page_ and f'value="drop:{manual}"' in page_ and "Mit Ausgleich prüfen" not in page_
        b = TK.assess(ctx(c), sid, adjust=frozenset({f"drop:{manual}"}))
        assert b.ok and manual in {t.tx_id for t in b.hide} and b.token != a.token
        page_ = c.get(f"/settings/datasources/{sid}/uebernahme?adjust=drop:{manual}").text
        assert f'type="hidden" name="adjust" value="drop:{manual}"' in page_  # Zustimmung bleibt bis zur Übernahme
        assert TK.apply(ctx(c), sid, b.token)["errors"]  # Token mit Ausblenden, ohne Zustimmung ausgeführt
        assert TK.apply(ctx(c), sid, b.token, adjust=frozenset({f"drop:{manual}"}))["errors"] == []
        assert holdings(c) == {"EUR": D(260), "BTC": D("0.00445")}
        assert ctx(c).db.q1("SELECT status FROM journal_tx WHERE tx_id=?", (manual,))["status"] != "active"
        assert TK.undo(ctx(c), sid)["errors"] == []
        assert ctx(c).db.q1("SELECT status FROM journal_tx WHERE tx_id=?", (manual,))["status"] == "active"
    finally:
        c.__exit__(None, None, None)


def test_kept_transfer_without_api_counterpart_is_named_as_cause(config, master, api):
    rows = [*BASE, tx("T1", "2024-02-20T10:00:00Z", "transfer", frm=("DKB Giro", "EUR", "70"),
                      to=("Bitpanda", "EUR", "70"), value="70")]
    c, sid = started(config, api, rows=rows)
    api.set_pages(page(ops(1, 3, 4)))
    api.portfolio = portfolio("250", "0.00445")
    try:
        TK.run_fetch(ctx(c), sid)
        a = TK.assess(ctx(c), sid)
        assert [t.tx_id for t in a.kept_free] == ["T1"] and not a.adjustable and not a.ok
        cause = a.causes["EUR"]
        assert cause["complete"] and [r["qty"] for r in cause["kept_free"]] == [D(70)]
        page_ = c.get(f"/settings/datasources/{sid}/uebernahme").text
        assert "nicht in der API" in page_ and "T1" in page_ and "vollständig erklärt" in page_
        gaps = c.get(f"/settings/datasources/{sid}/luecken").text
        assert "API vollständig (Saldoverlauf lückenlos)" in gaps and "Transfers ohne Gegenstück in der API" in gaps
        assert "bleibt, nicht in der API;2024-02-20T10:00:00Z;EUR;transfer;70" in c.get(
            f"/settings/datasources/{sid}/luecken.csv").text
    finally:
        c.__exit__(None, None, None)


def test_dup_of_diagnosis_correction_is_not_hideable():
    class Db:
        def __init__(self, row):
            self.row = row

        def q1(self, sql, args):
            return self.row

    t = SimpleNamespace(tx_id="PF-D-1")
    d = TK._dup(Db({"source": "diagnose", "group_ref": None}), t, [], None)
    assert not d["hideable"] and "Datenqualität" in d["why"]
    assert TK._dup(Db({"source": "manual", "group_ref": None}), t, [], None)["hideable"]
    assert not TK._dup(Db({"source": "manual", "group_ref": "G1"}), t, [], None)["hideable"]
