"""0.121 – Tempo nach Datenänderungen: Ledger ohne O(Lots) je Abgang (Ergebnis identisch), Vorrechnen im Hintergrund,
Einstellungsseite ohne Ledger-Neuaufbau."""

from __future__ import annotations

import dataclasses
import itertools

import pytest

from app.ledger import engine as E
from tests.helpers import portfolio
from tests.synthetic import generate

# ---- C: Ledger-Engine – Ergebnis bitgleich zur bisherigen Logik -------------------------------------------------

class _Reference(E._Engine):
    """Bisheriges Verhalten (bis 0.120), wörtlich übernommen: nach jedem Abgang die ganze Lot-Liste filtern,
    Umbuchungen und Kapitalmaßnahmen über eine vorab gefilterte Kopie. Referenz für die Gleichheit."""

    def _compact(self, asset):
        self.lots[asset] = [lot for lot in self.lots[asset] if lot.qty > E.DUST]

    def _move(self, tx, asset, src, dst, qty, *, create_phantom):
        if qty <= 0 or self.is_fiat(asset):
            return E.ZERO
        remaining = qty
        new_lots = []
        for lot in [lot for lot in self.lots[asset] if lot.account == src]:
            if remaining <= E.DUST:
                break
            take = min(lot.qty, remaining)
            if take >= lot.qty:
                lot.account = dst
            else:
                cost = self._take(lot, take)
                new_lots.append(E.Lot(next(self.ids), lot.root_id, asset, dst, take, cost, lot.acq_ts, lot.acq_date,
                                      lot.acq_tx, lot.origin, lot.income_tag, lot.via))
            remaining -= take
        for lot in new_lots:
            self._insert(lot)
        moved = qty - max(remaining, E.ZERO)
        if remaining > E.DUST and create_phantom:
            self._new_lot(asset, dst, remaining, E.ZERO, tx.ts, tx.date, tx.tx_id, "phantom")
            self.issue("warning", "missing_lots",
                       f"Transfer {asset} von {src}: {remaining.normalize():f} ohne vorhandene Lots – "
                       "als Zugang ohne Einstand behandelt", tx, asset, src)
        return moved

    def convert(self, tx, src_asset, src_acc, src_qty, dst_asset, dst_acc, dst_qty):
        if src_qty <= 0:
            return
        with E.decimal.localcontext(E._CTX):
            ratio = dst_qty / src_qty
        remaining = src_qty
        consumed = []
        for lot in [lot for lot in self.lots[src_asset] if lot.account == src_acc]:
            if remaining <= E.DUST:
                break
            take = min(lot.qty, remaining)
            cost = self._take(lot, take)
            consumed.append((lot, take, cost))
            remaining -= take
        self._compact(src_asset)
        for lot, take, cost in consumed:
            with E.decimal.localcontext(E._CTX):
                q = take * ratio
            nl = E.Lot(next(self.ids), lot.root_id, dst_asset, dst_acc, q, cost, lot.acq_ts, lot.acq_date,
                       lot.acq_tx, lot.origin if lot.origin != "phantom" else "phantom", lot.income_tag,
                       (*lot.via, tx.tx_id) if dst_asset != src_asset else lot.via)
            self._insert(nl)
        if remaining > E.DUST:
            with E.decimal.localcontext(E._CTX):
                q = remaining * ratio
            self._new_lot(dst_asset, dst_acc, q, E.ZERO, tx.ts, tx.date, tx.tx_id, "phantom")
            self.issue("warning", "missing_lots",
                       f"Kapitalmaßnahme {src_asset}→{dst_asset}: {remaining.normalize():f} ohne Lots auf {src_acc}",
                       tx, src_asset, src_acc)


def _run(engine_cls, pf, opts):
    return engine_cls(pf, opts).run()


@pytest.fixture(scope="module")
def synthetic_pf():
    g = generate(7, 1)
    return portfolio(g["rows"], g["assets"])


@pytest.mark.parametrize("scope,method", list(itertools.product(("global", "account"), ("fifo", "lifo", "hifo"))))
def test_engine_result_identical_to_previous_logic(synthetic_pf, scope, method):
    opts = dataclasses.replace(E.EngineOptions(), scope=scope, method=method)
    new = dataclasses.astuple(E.run_ledger(synthetic_pf, opts))

    old = dataclasses.astuple(_run(_Reference, synthetic_pf, opts))
    assert repr(new) == repr(old)


def test_compact_skips_only_when_no_dust_possible(synthetic_pf):
    eng = E._Engine(synthetic_pf, E.EngineOptions())
    eng.run()
    for asset, lots in eng.lots.items():  # nach dem Lauf: jede Liste ohne Staub-Lots oder als „staubig“ markiert
        assert asset in eng._dusty or all(lot.qty > E.DUST for lot in lots)


def test_move_and_convert_scan_lazily_but_identically():
    rows = []
    from tests.helpers import tx

    for i in range(40):  # viele Lots auf zwei Konten, dann Umbuchungen und Teilverkäufe mit Staubresten
        acc = "Börse A" if i % 2 else "Wallet B"
        rows.append(tx(f"b{i}", f"2024-01-{1 + i % 28:02d}T10:{i % 60:02d}:00Z", "buy",
                       frm=(acc, "EUR", 100), to=(acc, "BTC", "0.010000001"), value=100))
    rows.append(tx("t1", "2024-03-01T10:00:00Z", "transfer", frm=("Börse A", "BTC", "0.15"),
                   to=("Wallet B", "BTC", "0.15")))
    rows.append(tx("s1", "2024-03-02T10:00:00Z", "sell", frm=("Wallet B", "BTC", "0.2000000005"),
                   to=("Wallet B", "EUR", 9000), value=9000))
    rows.append(tx("s2", "2024-03-03T10:00:00Z", "sell", frm=("Börse A", "BTC", "0.05"),
                   to=("Börse A", "EUR", 2000), value=2000))
    pf = portfolio(rows)
    for scope in ("global", "account"):
        opts = dataclasses.replace(E.EngineOptions(), scope=scope)
        assert repr(dataclasses.astuple(E.run_ledger(pf, opts))) == repr(
            dataclasses.astuple(_run(_Reference, pf, opts)))


# ---- A: Vorrechnen im Hintergrund -------------------------------------------------------------------------------

@pytest.fixture
def client(config):
    import os
    import shutil
    import time
    from pathlib import Path

    from tests.test_journal import _client

    dst = config.import_dir / "b.zip"
    shutil.copy(Path(__file__).resolve().parents[1] / "examples" / "beispiel-import.zip", dst)
    old = time.time() - 3600
    os.utime(dst, (old, old))
    with _client(config) as c:
        from app.importer.loader import check_import_dir

        ctx = c.app.state.ctx
        if ctx.portfolio() is None:
            assert check_import_dir(ctx.db, config.import_dir, ctx.engine_options()).status == "imported"
            ctx.invalidate_data()
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        yield c


class _FakeScheduler:
    def __init__(self):
        from app.jobs import warm as W

        self.jobs = {W.JOB: W.warm}
        self.calls: list[tuple[str, float]] = []

    def debounce(self, name, delay, **kw):
        self.calls.append((name, delay))
        return True

    def next_runs(self):
        return {}

    def shutdown(self, *a, **k):
        return None

    def trigger(self, *a, **k):
        return True


def _background(ctx, monkeypatch) -> _FakeScheduler:
    """Wie im Betrieb: Hintergrund-Jobs an (ohne echten Scheduler – Aufträge werden nur protokolliert)."""
    monkeypatch.setattr(ctx, "config", dataclasses.replace(ctx.config, scheduler_enabled=True))
    sched = _FakeScheduler()
    monkeypatch.setattr(ctx, "scheduler", sched)
    return sched


def test_data_change_triggers_debounced_warm_job(client, monkeypatch):
    from app.jobs import warm as W

    ctx = client.app.state.ctx
    sched = _background(ctx, monkeypatch)
    W.install(ctx)
    W.install(ctx)  # einmalig
    ctx.invalidate_data()
    ctx.invalidate_overlay()
    assert sched.calls == [(W.JOB, W.DEBOUNCE_S), (W.JOB, W.DEBOUNCE_S)]


def test_warm_job_fills_caches_so_pages_do_not_recompute(client, monkeypatch):
    from app.jobs import warm as W
    from app.news import held as H

    ctx = client.app.state.ctx
    ctx.invalidate_data()
    res = W.warm(ctx)
    assert res["sekunden"] >= 0 and "historie" in res
    runs: list[int] = []
    real = E.run_ledger
    monkeypatch.setattr("app.context.run_ledger", lambda *a, **k: runs.append(1) or real(*a, **k))
    for page in ("/", "/settings", "/news", "/positions"):
        assert client.get(page).status_code == 200
    assert runs == []  # alles aus dem vorgerechneten Stand
    assert H.held(ctx).options  # News-Positionen gleich mit aufgefrischt


# ---- B: Einstellungsseite ohne Ledger-Neuaufbau -----------------------------------------------------------------

def test_settings_page_uses_last_state_and_triggers_background(client, monkeypatch):
    from app.jobs import warm as W

    ctx = client.app.state.ctx
    _background(ctx, monkeypatch)
    first = client.get("/settings").text
    si, current = W.settings_inputs(ctx)
    assert current and si.accounts and all(a in first for a in si.accounts)
    ctx.settings.set("ledger.probe", 1)  # echte Änderung der Datenrevision (wie Import/Buchung)
    ctx.invalidate_data()
    triggered: list[str] = []
    monkeypatch.setattr(W, "trigger", lambda c: triggered.append("x") or True)

    def boom(*a, **k):
        raise AssertionError("Einstellungsseite darf das Ledger nicht neu aufbauen")

    monkeypatch.setattr(ctx, "ledger", boom)
    page = client.get("/settings").text
    assert triggered and all(a in page for a in si.accounts)


def test_settings_page_fresh_after_saving_ledger_card(client, monkeypatch):
    from app.jobs import warm as W

    ctx = client.app.state.ctx
    _background(ctx, monkeypatch)
    client.get("/settings")
    acc = W.settings_inputs(ctx)[0].accounts[0]
    before = W.settings_inputs(ctx)[0].detected_cash.get(acc, False)
    ctx.settings.set("ledger.cash_overrides", {acc: not before})  # ändert die Datenrevision (ledger.*)
    monkeypatch.setattr(W, "trigger", lambda c: True)
    assert W.settings_inputs(ctx)[1] is False  # ohne „saved=ledger“: letzter Stand, Hintergrund rechnet
    client.get("/settings?saved=ledger")
    si, current = W.settings_inputs(ctx)
    assert current and si.detected_cash.get(acc) is (not before)


def test_without_background_jobs_settings_compute_synchronously(client):
    from app.jobs import warm as W

    ctx = client.app.state.ctx
    client.get("/settings")
    ctx.invalidate_data()
    ctx.settings.set("ledger.cash_overrides", {})
    assert W.trigger(ctx) is False
    assert W.settings_inputs(ctx)[1] is True
