"""0.83.0 Quellenübernahme mit Lücken der API: Der Saldoverlauf der Quelle (``asset_balance_after``) belegt fehlende
Bewegungen. Import-Buchungen ohne Gegenstück, die eine solche Lücke füllen, bleiben; ein verbleibender Rest wird nur
mit ausdrücklicher Zustimmung je Asset als gekennzeichnete Ausgleichsbuchung gebucht – zum Zeitpunkt der Lücke
(belegt) bzw. des Abrufs (nicht belegt). Danach exakter Bestand, rückgängig als Ganzes."""

# ruff: noqa: F811  (Fixtures aus test_bitpanda werden importiert und als Argumente verwendet)
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from app.datasources import bitpanda as BP
from app.datasources import takeover as TK
from tests.helpers import tx
from tests.test_bitpanda import api, client, ctx, master  # noqa: F401  (Fixtures)
from tests.test_takeover import holdings, ids, portfolio, started, sync_txs

D = Decimal


def leg(ts: str, side: str, amount: str, bal: str, wallet: str = "w1", ref: str = "eur", fee: str = "0") -> BP._Leg:
    return BP._Leg(id=None, side=side, amount=D(amount), ref=ref, kind="fiat", ttype="x",
                   ts=datetime.fromisoformat(ts).replace(tzinfo=UTC), fee=D(fee), fee_ref=ref, wallet=wallet,
                   balance_after=D(bal), balance_ref=ref)


def test_chain_gaps_locate_missing_movements_by_balance_jumps():
    ops = [SimpleNamespace(legs=[leg("2024-01-01T08:00:00", "in", "500", "500")]),
           SimpleNamespace(legs=[leg("2024-01-02T08:00:00", "out", "250", "250")]),
           # dazwischen fehlt eine Auszahlung über 200 € (Saldo springt von 250 auf 50 + …)
           SimpleNamespace(legs=[leg("2024-01-05T08:00:00", "out", "50", "0")]),
           # Gebühr zusätzlich abgezogen: kein Bruch
           SimpleNamespace(legs=[leg("2024-01-06T08:00:00", "in", "10", "9.5", fee="0.5")])]
    gaps, last = BP.chain_gaps(ops)  # type: ignore[arg-type]
    assert [(g["from"][:10], g["to"][:10], g["amount"]) for g in gaps["eur"]] == [("2024-01-02", "2024-01-05", D(-200))]
    assert last["eur"][1] == D("9.5")
    # fehlende Umbuchung zwischen zwei Wallets desselben Assets hebt sich auf – keine Lücke
    twin = [SimpleNamespace(legs=[leg("2024-01-01T08:00:00", "in", "5", "5", wallet="a")]),
            SimpleNamespace(legs=[leg("2024-01-01T08:00:00", "in", "5", "5", wallet="b")]),
            SimpleNamespace(legs=[leg("2024-01-03T08:00:00", "in", "1", "3", wallet="a"),
                                  leg("2024-01-03T08:00:00", "in", "1", "9", wallet="b")])]
    assert BP.chain_gaps(twin)[0] == {}  # type: ignore[arg-type]


def _setup(api, config, withdrawal: str | None):
    rows = [tx("K1", "2024-02-01T08:00:00Z", "deposit", to=("Bitpanda", "EUR", "500"), value="500"),
            tx("K2", "2024-02-02T09:01:00Z", "buy", frm=("Bitpanda", "EUR", "250"), to=("Bitpanda", "BTC", "0.0055"),
               value="251.5"),
            tx("K3", "2024-02-12T12:00:00Z", "withdrawal", frm=("Bitpanda", "BTC", "0.001"), value="55")]
    if withdrawal:  # Auszahlung aufs Bankkonto: nur im Import, die API kennt sie nicht
        rows.append(tx("K5", "2024-02-15T10:00:00Z", "withdrawal", frm=("Bitpanda", "EUR", withdrawal),
                       value=withdrawal))
    c, sid = started(config, api, rows=rows)
    api.portfolio = portfolio("50", "0.00445")  # Bitpanda meldet 50 € (nach der Auszahlung über 200 €)
    return c, sid


def test_import_withdrawal_that_fills_an_api_gap_stays_and_the_takeover_is_exact(config, master, api):
    c, sid = _setup(api, config, "200")
    try:
        TK.run_fetch(ctx(c), sid)
        a = TK.assess(ctx(c), sid)
        gaps = a.gaps["EUR"]
        assert [(g["to"], g["amount"]) for g in gaps] == [(None, D(-200))]  # Rest nach dem letzten Vorgang
        assert [t.tx_id for t, _g in a.fillers] == ["K5"] and "K5" not in {t.tx_id for t in a.hide}
        assert a.ok, [(x.key, x.detail) for x in a.blockers]  # ohne Ausgleich exakt
        res = TK.apply(ctx(c), sid, a.token)
        assert res["errors"] == [], res
        assert holdings(c) == {"EUR": D(50), "BTC": D("0.00445")} and "K5" in ids(c)
        t = TK.latest(ctx(c).db, sid)
        assert "füllen Lücken der API" in t["message"]
        assert TK.undo(ctx(c), sid)["errors"] == []
    finally:
        c.__exit__(None, None, None)


def test_rest_of_a_documented_gap_needs_consent_and_is_booked_dated_and_undone(config, master, api):
    c, sid = _setup(api, config, "150")  # Import kennt nur 150 der fehlenden 200 €
    try:
        TK.run_fetch(ctx(c), sid)
        a = TK.assess(ctx(c), sid)
        assert [t.tx_id for t, _g in a.fillers] == ["K5"]
        adj = a.adjustable["EUR"]
        assert adj["dated"] and adj["need"] == D(-50) and [x["amount"] for x in adj["items"]] == [D(-50)]
        assert not a.ok and not a.adjustments  # ohne Zustimmung gesperrt, nichts vorgemerkt
        p = next(x for x in a.problems if x["id"] == "bal:EUR")
        assert p["adjust"]["dated"]
        page = c.get(f"/settings/datasources/{sid}/uebernahme").text
        assert "Die Quelle belegt die fehlenden Bewegungen selbst" in page and 'name="adjust" value="EUR"' in page
        refused = TK.apply(ctx(c), sid, a.token)  # Token ohne Ausgleich: Beweis nicht erbracht
        assert refused["errors"] and not sync_txs(c)
        b = TK.assess(ctx(c), sid, adjust={"EUR"})
        assert b.ok and b.token != a.token and [x["amount"] for x in b.adjustments] == [D(-50)]
        assert "mit Ausgleich" in b.tier
        page = c.get(f"/settings/datasources/{sid}/uebernahme?adjust=EUR").text
        assert "Ausgleichsbuchung" in page and 'name="adjust" value="EUR"' in page and "API maßgeblich machen" in page
        assert TK.apply(ctx(c), sid, b.token)["errors"]  # Token mit Ausgleich, aber ohne Ausgleich ausgeführt
        res = TK.apply(ctx(c), sid, b.token, adjust=frozenset({"EUR"}))
        assert res["errors"] == [], res
        assert holdings(c) == {"EUR": D(50), "BTC": D("0.00445")} and "K5" in ids(c)
        adjrows = [r for r in sync_txs(c) if (r["external_id"] or "").startswith("ausgleich:")]
        assert len(adjrows) == 1 and adjrows[0]["type"] == "withdrawal" and D(adjrows[0]["from_qty"]) == D(50)
        # Lücke nach dem letzten API-Vorgang: Fensterende ist der Abruf; Vermerk nennt das belegte Fenster
        assert "Lücke laut Saldoverlauf" in adjrows[0]["note"] and "02.02.2024 bis zum Abruf" in adjrows[0]["note"]
        t = TK.latest(ctx(c).db, sid)
        assert "mit Ausgleich" in t["message"] and "1 Ausgleichsbuchungen" in t["message"]
        assert TK.undo(ctx(c), sid)["errors"] == []
        assert not sync_txs(c) and {"K1", "K2", "K3", "K5"} <= ids(c)
    finally:
        c.__exit__(None, None, None)


def test_undocumented_rest_is_offered_as_single_adjustment_at_fetch_time():
    a = SimpleNamespace(fetched_at="2026-11-02T14:39:00Z", delivered={"USD"}, adjust=frozenset({"USD"}),
                        gaps={"USD": [{"from": None, "to": None, "amount": D("-59.96")}]}, adjustable={},
                        adjustments=[])
    expected, observed, parts = {"USD": D(35)}, {}, {"USD": {}}
    TK._adjustable(a, expected, observed, parts)
    adj = a.adjustable["USD"]
    assert not adj["dated"] and adj["need"] == D(-35) and adj["gaps_total"] == D("-59.96")
    assert a.adjustments == [{"asset": "USD", "ts": "2026-11-02T14:39:00Z", "amount": D(-35),
                              "window": "zum Abrufzeitpunkt"}]
    assert expected["USD"] == 0  # mit Zustimmung exakt auf den gemeldeten Bestand
    b = SimpleNamespace(fetched_at="x", delivered=set(), adjust=frozenset(), gaps={}, adjustable={}, adjustments=[])
    TK._adjustable(b, {"XYZ": D(3)}, {}, {"XYZ": {}})
    assert b.adjustable == {}  # ohne gemeldeten Bestand der Quelle kein Ausgleich (Ziel unbekannt)
