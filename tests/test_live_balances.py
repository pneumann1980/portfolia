"""Aktuelle Bestände problematischer Positionen abfragen (nur lesend, im Hintergrund) und Quellenrang bei Dubletten:
Börsen-/Wallet-API vor Börsen-CSV vor Steuertool-Import vor manuell. Fakes ohne Netz, synthetische Daten."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.datasources import connector as K
from app.datasources.service import datasource_service
from app.diagnosis import audit as AU
from app.diagnosis import live as L
from app.diagnosis.recommend import pair_choice
from tests.test_datasource_concurrency import wait_until
from tests.test_datasources import FakeKraken, client, create_source, fake, source  # noqa: F401


def test_source_rank_orders_api_before_csv_before_tax_tool_before_manual():
    ranks = [AU.source_rank(f) for f in ("sync:bitpanda", "csv:binance", "doc:beleg", "koinly", "import", "manual")]
    assert ranks == [0, 1, 2, 3, 3, 4]
    assert AU.source_rank("sparplan") == AU.source_rank("diagnose") == 4


def _tx(origin: str, tx_id: str) -> Any:
    return SimpleNamespace(origin=origin, tx_id=tx_id)


def test_hash_pair_api_booking_wins_over_import_manual_booking_does_not():
    api, manual, imp = _tx("journal", "PF-S-1"), _tx("journal", "PF-M-1"), _tx("import", "K1")
    facts = SimpleNamespace(is_api=lambda t: t.tx_id.startswith("PF-S"), hideable=lambda t: True)
    assert pair_choice(facts, api, imp)[0:3] == ("hide", imp, api)  # Import-Buchung entfällt, API bleibt
    assert pair_choice(facts, imp, api)[0:3] == ("hide", imp, api)
    assert pair_choice(facts, manual, imp)[0:3] == ("cover", manual, imp)  # manuelle Buchung: wie bisher


class BalanceKraken(FakeKraken):
    """check() liefert Bestände (wie Börsen- und Wallet-Anbindungen), fetch() darf in dieser Betriebsart nie laufen."""

    fetched = 0

    def check(self, cfg: K.SourceConfig, secret: K.Secret) -> K.CheckResult:
        secret.reveal()
        return K.CheckResult(True, "ok", balances=[K.Balance("EUR", Decimal("123.45"), "Euro")])

    def fetch(self, cfg: K.SourceConfig, secret: K.Secret, cursor: dict[str, Any] | None) -> K.FetchResult:
        BalanceKraken.fetched += 1
        return super().fetch(cfg, secret, cursor)


@pytest.fixture
def balance_connector(fake):  # noqa: F811
    BalanceKraken.fetched = 0
    K.register(BalanceKraken)
    yield BalanceKraken
    K.register(FakeKraken)


def test_live_balance_query_reads_only_balances_and_returns_to_the_diagnosis(client, balance_connector):  # noqa: F811
    c = client
    ctx = c.app.state.ctx
    svc = datasource_service(ctx)
    sid = create_source(c)
    assert [s["id"] for s in L.sources_for(ctx, ["Kraken"])] == [sid] and not L.sources_for(ctx, ["Unbekannt"])
    assert L.start(ctx, ["Unbekannt"], "/quality/diagnose")["error"]
    before = ctx.db.scalar("SELECT COUNT(*) FROM journal_tx")
    res = L.start(ctx, ["Kraken"], "/quality/diagnose#bestand")
    assert res["started"] and res["count"] == 1
    assert wait_until(lambda: not svc.batch_progress().get("running"))
    st = svc.batch_progress()
    assert st["mode"] == "check" and st["back"] == "/quality/diagnose#bestand" and st["done"] == 1
    assert [(r["asset_key"], str(r["qty"])) for r in svc.balances(sid)] == [("EUR", "123.45")]
    assert balance_connector.fetched == 0 and ctx.db.scalar("SELECT COUNT(*) FROM journal_tx") == before
    assert source(c, sid)["cursor_json"] in (None, "")  # kein Abrufstand verändert
    # Fortschrittsanzeige kehrt nach dem Ende zur Diagnose zurück (nicht zur Datenquellen-Liste)
    r = c.get("/settings/datasources/batch-progress")
    assert r.status_code == 204 and r.headers["HX-Redirect"] == "/quality/diagnose#bestand"


def test_back_url_must_be_local(client, balance_connector):  # noqa: F811
    c = client
    svc = datasource_service(c.app.state.ctx)
    sid = create_source(c)
    assert svc.start_sync_many([sid], "x", mode="check", back="https://evil.example/")["started"]
    assert wait_until(lambda: not svc.batch_progress().get("running"))
    assert svc.batch_progress()["back"] is None
