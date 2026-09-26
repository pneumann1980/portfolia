"""FIFO-Lots inkl. Teilverkäufen, Kapitalmaßnahmen, Transfers, Gebühren, Erträgen und Zahlungsströmen."""

from datetime import date
from decimal import Decimal

from app.ledger.engine import EngineOptions, run_ledger
from tests.helpers import portfolio, tx

D = Decimal


def test_fifo_partial_sales():
    pf = portfolio([
        tx("t1", "2023-01-10", "buy", frm=("Depot", "EUR", 1000), to=("Depot", "WKN:A0B1C2", 10), value=1000),
        tx("t2", "2023-06-01", "buy", frm=("Depot", "EUR", 2000), to=("Depot", "WKN:A0B1C2", 10), value=2000),
        tx("t3", "2024-01-05", "sell", frm=("Depot", "WKN:A0B1C2", 15), to=("Depot", "EUR", 4490), value=4500,
           fee=("EUR", 10, 10)),
        tx("t4", "2024-02-01", "sell", frm=("Depot", "WKN:A0B1C2", 2), to=("Depot", "EUR", 700), value=700),
    ])
    res = run_ledger(pf)
    d1, d2 = res.disposals
    assert d1.proceeds == D(4490)
    assert [(p.qty, p.cost, p.acq_date) for p in d1.parts] == [
        (D(10), D(1000), date(2023, 1, 10)), (D(5), D(1000), date(2023, 6, 1))]
    assert d1.gain == D(2490)
    assert d2.cost == D(400) and d2.gain == D(300)
    lots = res.lots_for("WKN:A0B1C2")
    assert len(lots) == 1 and lots[0].qty == D(3) and lots[0].cost == D(600)
    assert res.holdings_by_asset()["WKN:A0B1C2"] == D(3)


def test_buy_fee_is_part_of_cost_basis_and_flows_for_depot_without_cash():
    pf = portfolio([
        tx("t1", "2023-01-10T10:00:00Z", "buy", frm=("Depot", "EUR", 1000), to=("Depot", "WKN:A0B1C2", 10),
           value=1000, fee=("EUR", 5, 5)),
        tx("t2", "2023-03-10T10:00:00Z", "sell", frm=("Depot", "WKN:A0B1C2", 4), to=("Depot", "EUR", 600), value=600,
           fee=("EUR", 5, 5)),
    ])
    res = run_ledger(pf)
    assert res.cash_tracked["Depot"] is False
    assert res.lots_for("WKN:A0B1C2")[0].cost == D(1005) * 6 / 10
    # Konto ohne Cash-Führung: Kauf = Einzahlung (inkl. Gebühr), Verkauf = Auszahlung (netto)
    assert [(f.kind, f.amount) for f in res.flows] == [("buy_external", D(1005)), ("sell_external", D(-595))]
    assert ("Depot", "EUR") not in res.balances
    assert res.disposals[0].gain == D(595) - D(1005) * 4 / 10


def test_cash_tracked_account_has_internal_buys():
    pf = portfolio([
        tx("d1", "2023-01-01", "deposit", to=("Exchange", "EUR", 5000), value=5000),
        tx("b1", "2023-01-02", "buy", frm=("Exchange", "EUR", 3000), to=("Exchange", "BTC", "0.15"), value=3000,
           fee=("EUR", "4.5", "4.5")),
        tx("w1", "2023-05-01", "withdrawal", frm=("Exchange", "EUR", 1000), value=1000),
    ])
    res = run_ledger(pf)
    assert res.cash_tracked["Exchange"] is True
    assert [(f.kind, f.amount) for f in res.flows] == [("deposit", D(5000)), ("withdrawal", D(-1000))]
    assert res.balances[("Exchange", "EUR")] == D("995.5")
    assert res.lots_for("BTC")[0].cost == D("3004.5")


def test_split_transfers_lots_with_basis_and_date():
    pf = portfolio([
        tx("b1", "2021-03-01", "buy", frm=("Depot", "EUR", 4000), to=("Depot", "WKN:US0001", 10), value=4000),
        tx("b2", "2022-03-01", "buy", frm=("Depot", "EUR", 1000), to=("Depot", "WKN:US0001", 2), value=1000),
        tx("s1", "2022-08-25", "corporate_action", tag="split", frm=("Depot", "WKN:US0001", 12),
           to=("Depot", "WKN:US0001", 48)),
        tx("v1", "2023-01-10", "sell", frm=("Depot", "WKN:US0001", 44), to=("Depot", "EUR", 6600), value=6600),
    ])
    res = run_ledger(pf)
    lots = res.lots_for("WKN:US0001")
    assert len(lots) == 1
    assert lots[0].qty == D(4) and lots[0].cost == D(1000) * 4 / 8 and lots[0].acq_date == date(2022, 3, 1)
    d = res.disposals[0]
    assert [(p.qty, p.acq_date) for p in d.parts] == [(D(40), date(2021, 3, 1)), (D(4), date(2022, 3, 1))]
    assert d.cost == D(4000) + D(500)
    assert res.holdings_by_asset()["WKN:US0001"] == D(4)


def test_transfer_moves_lots_and_keeps_acquisition_date():
    pf = portfolio([
        tx("b1", "2022-01-01", "buy", frm=("Exchange", "EUR", 1000), to=("Exchange", "ETH", 1), value=1000),
        tx("b2", "2022-06-01", "buy", frm=("Exchange", "EUR", 1500), to=("Exchange", "ETH", 1), value=1500),
        tx("t1", "2022-07-01", "transfer", frm=("Exchange", "ETH", "1.5"), to=("Ledger", "ETH", "1.5"),
           fee=("ETH", "0.01", "12")),
    ], )
    res = run_ledger(pf, EngineOptions(scope="account"))
    ledger_lots = res.lots_for("ETH", "Ledger")
    assert [(lot.qty, lot.acq_date) for lot in ledger_lots] == [(D(1), date(2022, 1, 1)),
                                                                   (D("0.5"), date(2022, 6, 1))]
    assert res.balances[("Exchange", "ETH")] == D("0.49")
    fee = next(d for d in res.disposals if d.kind == "transfer_fee")
    assert fee.qty == D("0.01") and fee.proceeds == D(12)
    assert fee.parts[0].acq_date == date(2022, 6, 1)  # Kontosicht: ältestes Lot auf Exchange
    assert sum(lot.qty for lot in res.lots_for("ETH", "Exchange")) == D("0.49")
    assert not [f for f in res.flows if f.tx_id == "t1"]  # Transfers sind intern


def test_global_vs_account_scope_and_relabelling():
    rows = [
        tx("b1", "2021-01-01", "buy", frm=("A", "EUR", 100), to=("A", "BTC", 1), value=100),
        tx("b2", "2022-01-01", "buy", frm=("B", "EUR", 900), to=("B", "BTC", 1), value=900),
        tx("s1", "2022-06-01", "sell", frm=("B", "BTC", 1), to=("B", "EUR", 500), value=500),
    ]
    glob = run_ledger(portfolio(rows), EngineOptions(scope="global"))
    acct = run_ledger(portfolio(rows), EngineOptions(scope="account"))
    assert glob.disposals[0].gain == D(400)  # global FIFO: ältestes Lot (A)
    assert acct.disposals[0].gain == D(-400)  # kontobezogen: Lot von B
    # Kontenzuordnung bleibt konsistent: A hält weiterhin 1 BTC (jetzt mit Einstand 900)
    a_lots = glob.lots_for("BTC", "A")
    assert [(lot.qty, lot.cost) for lot in a_lots] == [(D(1), D(900))]
    assert glob.lots_for("BTC", "B") == []


def test_trade_with_fee_in_third_asset_creates_fee_disposal():
    pf = portfolio([
        tx("b1", "2023-01-01", "buy", frm=("Binance", "EUR", 1000), to=("Binance", "BTC", "0.05"), value=1000),
        tx("b2", "2023-01-02", "buy", frm=("Binance", "EUR", 250), to=("Binance", "BNB", 1), value=250),
        tx("x1", "2023-02-01", "trade", frm=("Binance", "BTC", "0.05"), to=("Binance", "ETH", "0.8"), value=1200,
           fee=("BNB", "0.01", "3")),
    ])
    res = run_ledger(pf)
    sale, fee = res.disposals
    assert sale.kind == "trade" and sale.proceeds == D(1197) and sale.gain == D(197)
    assert fee.kind == "fee" and fee.asset == "BNB" and fee.proceeds == D(3) and fee.cost == D("2.5")
    assert res.lots_for("ETH")[0].cost == D(1200)
    assert res.balances[("Binance", "BNB")] == D("0.99")
    # Positions-Flüsse summieren sich zu 0 (Tausch ist intern)
    assert sum(f.amount for f in res.asset_flows if f.tx_id == "x1") == 0


def test_reward_creates_income_lot():
    pf = portfolio([
        tx("r1", "2023-05-01T08:00:00Z", "deposit", tag="reward", to=("Wallet", "KAS", 100), value="12.5"),
        tx("r2", "2023-05-02T08:00:00Z", "deposit", tag="staking", to=("Wallet", "KAS", 50), value="7"),
    ])
    res = run_ledger(pf)
    assert [(e.tag, e.value_eur) for e in res.income] == [("reward", D("12.5")), ("staking", D(7))]
    assert [(lot.origin, lot.cost, lot.income_tag) for lot in res.lots_for("KAS")] == [
        ("income", D("12.5"), "reward"), ("income", D(7), "staking")]
    assert not res.flows  # Erträge sind Rendite, keine externen Flüsse


def test_unmatched_crypto_deposit_is_external_flow_at_market_value():
    pf = portfolio([
        tx("d1", "2023-01-01", "deposit", to=("Wallet", "BTC", "0.1"), value=1500),
        tx("w1", "2023-06-01", "withdrawal", frm=("Wallet", "BTC", "0.05"), value=1250),
        tx("l1", "2023-07-01", "withdrawal", tag="lost", frm=("Wallet", "BTC", "0.01"), value=250),
    ])
    res = run_ledger(pf)
    assert [(f.kind, f.amount) for f in res.flows] == [("transfer_in_external", D(1500)),
                                                       ("transfer_out_external", D(-1250))]
    lost = next(d for d in res.disposals if d.kind == "lost")
    assert lost.proceeds == 0 and lost.gain == D(-150)
    assert any(i.code == "unmatched_transfers" for i in res.issues)


def test_missing_lots_produce_phantom_with_warning():
    pf = portfolio([
        tx("s1", "2023-01-10", "sell", frm=("Exchange", "ETH", 1), to=("Exchange", "EUR", 1000), value=1000),
    ])
    res = run_ledger(pf)
    d = res.disposals[0]
    assert d.missing_basis and d.cost == 0 and d.parts[0].acq_date is None
    codes = {i.code for i in res.issues}
    assert "missing_lots" in codes


def test_dividend_on_depot_without_cash_is_distribution_outflow():
    pf = portfolio([
        tx("b1", "2023-01-10", "buy", frm=("Depot", "EUR", 1000), to=("Depot", "WKN:A0B1C2", 10), value=1000),
        tx("dv", "2023-05-10", "deposit", tag="dividend", to=("Depot", "EUR", 30), value=30, related="WKN:A0B1C2"),
    ])
    res = run_ledger(pf)
    assert [(f.kind, f.amount) for f in res.flows] == [("buy_external", D(1000)), ("income_paid_out", D(-30))]
    assert res.income[0].related_asset == "WKN:A0B1C2"
    assert res.income_by_asset()["WKN:A0B1C2"] == D(30)


def test_lifo_and_hifo_methods():
    rows = [
        tx("b1", "2021-01-01", "buy", frm=("A", "EUR", 100), to=("A", "ETH", 1), value=100),
        tx("b2", "2021-02-01", "buy", frm=("A", "EUR", 300), to=("A", "ETH", 1), value=300),
        tx("b3", "2021-03-01", "buy", frm=("A", "EUR", 200), to=("A", "ETH", 1), value=200),
        tx("s1", "2021-04-01", "sell", frm=("A", "ETH", 1), to=("A", "EUR", 250), value=250),
    ]
    assert run_ledger(portfolio(rows), EngineOptions(method="lifo")).disposals[0].cost == D(200)
    assert run_ledger(portfolio(rows), EngineOptions(method="hifo")).disposals[0].cost == D(300)
    assert run_ledger(portfolio(rows), EngineOptions(method="fifo")).disposals[0].cost == D(100)


def test_token_migration_via_corporate_action_keeps_dates():
    pf = portfolio([
        tx("b1", "2022-01-01", "buy", frm=("Ex", "EUR", 500), to=("Ex", "BNB", 2), value=500),
        tx("m1", "2023-01-01", "corporate_action", tag="migration", frm=("Ex", "BNB", 2), to=("Ex", "ETH", 20)),
    ])
    res = run_ledger(pf)
    lot = res.lots_for("ETH")[0]
    assert lot.qty == D(20) and lot.cost == D(500) and lot.acq_date == date(2022, 1, 1)
    assert not res.lots_for("BNB")
    assert not res.disposals
