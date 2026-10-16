"""Konten zusammenführen: Konten von Datenquellen (auch ohne Buchungen), Adresse im Kontonamen, Vorschläge unter
„Konten ohne Datenquelle“, Auswahlfelder (synthetische Daten, temporäre DB)."""

from __future__ import annotations

import os
import re
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import app.datasources.chains.cardano
import app.datasources.chains.evm
import app.datasources.chains.xrpl  # noqa: F401
from app.assetchange import account_merge as AM
from app.config import Config, Secrets
from app.datasources.service import datasource_service
from app.importer.zipbuilder import build_zip
from app.jobs import tasks
from app.main import build_app
from tests.helpers import ASSETS, tx
from tests.test_diagnosis import COLS, asset, make_ctx

D = Decimal
ADA_ACC = "Cardano (ADA) - addr1q...47"
ADA_ADDR = "addr1qxtyhk3vd6ewz9mq7dx4q0n5lr8j2uppsc0ggwlhfhxl95mdp8q6zk9ex6dks0qa4tw6msqyvhgyetyek4vsj2nz47"
ETH_ACC = "Ethereum (ETH) - 0x20B7...09"
ETH_ADDR = "0x20b7c3a1e5d94f08aa11bc22cc33dd44ee550009"
HACK_ACC = "Nova Wallet (gehackt: 0x375A.../5Fq31...)"
XRP_ACC = "XRPL (XRP)"
ASSETS_W = [*ASSETS, asset("ADA", "Cardano", "coingecko", "cardano"), asset("XRP", "XRP", "coingecko", "ripple"),
            asset("DOT", "Polkadot", "coingecko", "polkadot")]
ROWS = [
    tx("A1", "2022-12-13T10:00:00Z", "deposit", to=(ADA_ACC, "ADA", "100"), value=40),
    tx("E1", "2024-04-07T10:00:00Z", "deposit", to=(ETH_ACC, "ETH", "1"), value=2000),
    tx("H1", "2024-04-07T11:00:00Z", "deposit", to=(HACK_ACC, "ETH", "2"), value=4000),
    tx("X1", "2023-09-20T10:00:00Z", "deposit", to=(XRP_ACC, "XRP", "500"), value=250),
    tx("K0", "2024-01-02T08:00:00Z", "deposit", to=("Kraken", "EUR", "100"), value=100),
]


@pytest.fixture
def cfg(tmp_path) -> Config:
    imp = tmp_path / "import"
    imp.mkdir(parents=True, exist_ok=True)
    return Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                  startup_jobs=False, log_format="text", secrets=Secrets())


def add_source(ctx, provider: str, name: str, account: str, address: str | None = None, kind: str = "wallet") -> int:
    cur = ctx.db.x("INSERT INTO data_source(kind, provider, name, account, address, status, created_at, updated_at) "
                   "VALUES (?,?,?,?,?, 'synced', '2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z')",
                   (kind, provider, name, account, address))
    return int(cur.lastrowid)


def test_fragments_in_account_names():
    assert AM.address_fragments(ADA_ACC) == [("addr1q", "47")]
    assert AM.address_fragments(ETH_ACC) == [("0x20B7", "09")]
    assert AM.address_fragments(HACK_ACC) == [("0x375A", ""), ("5Fq31", "")]
    assert AM.address_fragments("Kraken ... später") == [] and AM.address_fragments("Konto 1...2") == []


def test_source_account_without_bookings_is_known_and_matched_by_address(cfg):
    ctx = make_ctx(cfg, ROWS, ASSETS_W)
    add_source(ctx, "cardano", "ADA", "Ledger (ADA)", ADA_ADDR)  # frisch angelegt: noch keine Buchungen
    assert "Ledger (ADA)" in AM.known_accounts(ctx) and "Ledger (ADA)" not in ctx.portfolio().all_accounts()
    c = next(c for c in AM.candidates(ctx) if ADA_ACC in (c.alias, c.target))
    # das Konto mit Datenquelle bleibt (Ziel), das gebuchte Konto ohne Datenquelle verschwindet (Alias)
    assert (c.alias, c.target, c.target_source, c.strong) == (ADA_ACC, "Ledger (ADA)", "ADA", True)
    assert "Adresse stimmt überein: addr1q…47" in c.reasons[0]
    p = AM.preview(ctx, ADA_ACC, "Ledger (ADA)")
    assert p.ok and p.n_alias_txs == 1 and p.n_target_txs == 0
    assert any("Datenquelle" in i for i in p.info)
    mid, errors = AM.apply(ctx, ADA_ACC, "Ledger (ADA)", p.fp)
    assert mid and not errors
    ctx.invalidate_data()
    assert ctx.ledger().balances[("Ledger (ADA)", "ADA")] == D("100")
    # das Konto ist jetzt mit der Datenquelle verknüpft und taucht nicht mehr als „ohne Datenquelle“ auf
    assert not any(u["account"] in (ADA_ACC, "Ledger (ADA)") for u in datasource_service(ctx).unlinked_accounts())
    assert AM.revert(ctx, mid) == []


def test_no_match_for_other_address_or_too_short_hint(cfg):
    ctx = make_ctx(cfg, ROWS, ASSETS_W)
    add_source(ctx, "cardano", "Fremd", "Fremd (ADA)", "addr1q" + "z" * 50 + "99")
    assert not [c for c in AM.candidates(ctx) if ADA_ACC in (c.alias, c.target) and c.strong]
    add_source(ctx, "ethereum", "Andere", "Andere (ETH)", "0x" + "ab" * 20)
    assert not [c for c in AM.candidates(ctx) if ETH_ACC in (c.alias, c.target)]


def test_prefix_only_fragment_matches_with_note(cfg):
    ctx = make_ctx(cfg, ROWS, ASSETS_W)
    add_source(ctx, "ethereum", "Nova ETH", "Nova ETH-Konto", "0x375a" + "1" * 36)
    c = next(c for c in AM.candidates(ctx) if HACK_ACC in (c.alias, c.target))
    assert c.alias == HACK_ACC and c.target == "Nova ETH-Konto" and c.strong
    assert "nur der Anfang ist bekannt" in c.reasons[0]


def test_same_chain_hint_only_when_unambiguous(cfg):
    ctx = make_ctx(cfg, ROWS, ASSETS_W)
    add_source(ctx, "xrp", "XRP Wallet", "Ledger XRP", "r" + "A" * 30)
    c = next(c for c in AM.candidates(ctx) if XRP_ACC in (c.alias, c.target))
    assert (c.alias, c.target, c.strong) == (XRP_ACC, "Ledger XRP", False)
    assert "nicht über die Adresse belegt" in c.reasons[0]
    add_source(ctx, "xrp", "XRP Zweit", "Zweit XRP", "r" + "B" * 30)  # zwei Quellen derselben Chain → nicht eindeutig
    assert not [c for c in AM.candidates(ctx) if XRP_ACC in (c.alias, c.target)]


def test_suggestions_for_accounts_without_source_and_none_for_exchange_names(cfg):
    ctx = make_ctx(cfg, ROWS, ASSETS_W)
    add_source(ctx, "cardano", "ADA", "Ledger (ADA)", ADA_ADDR)
    add_source(ctx, "ethereum", "ETH", "Ledger (ETH)", ETH_ADDR)
    rows = {u["account"]: u for u in datasource_service(ctx).unlinked_accounts()}
    assert [s.target for s in rows[ADA_ACC]["suggestions"]] == ["Ledger (ADA)"]
    assert [s.target for s in rows[ETH_ACC]["suggestions"]] == ["Ledger (ETH)"]
    assert rows["Kraken"]["suggestions"] == []


def test_web_dropdowns_exclude_each_other_and_show_source_accounts(cfg):
    dst = cfg.import_dir / "kuratiert.zip"
    build_zip(dst, transactions=ROWS, assets=ASSETS_W, generated_at="2025-06-30T20:00:00Z",
              valuation_date="2025-06-30", extra_tx_columns=COLS)
    old = time.time() - 3600
    os.utime(dst, (old, old))
    with TestClient(build_app(cfg, start_scheduler=False)) as c:
        ctx = c.app.state.ctx
        assert tasks.import_check(ctx, "test").status == "imported"
        add_source(ctx, "cardano", "ADA", "Ledger (ADA)", ADA_ADDR)
        page = c.get("/accounts/merge", params={"alias": ADA_ACC})
        assert page.status_code == 200
        alias_sel, target_sel = re.findall(r'<select name="(?:alias|target)".*?</select>', page.text, re.S)[:2]
        assert "Ledger (ADA)" in alias_sel and "Ledger (ADA)" in target_sel  # Konto ohne Buchungen wählbar
        assert "keine Buchungen · Datenquelle ADA" in target_sel
        assert ADA_ACC in alias_sel and ADA_ACC not in target_sel  # das Alias erscheint nicht als Ziel
        assert 'data-peer="merge-alias"' in target_sel and 'data-peer="merge-target"' in alias_sel
        assert "Adresse passt" in page.text  # Vorschlag mit Adressbeleg
        ds = c.get("/settings/datasources")
        assert "Mögliche Datenquelle:" in ds.text and "Mit „ADA“ zusammenführen" in ds.text
        assert "davon mit passender Datenquelle" in ds.text
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        prev = c.post("/accounts/merge/preview", data={"csrf_token": token, "alias": ADA_ACC,
                                                       "target": "Ledger (ADA)"})
        assert prev.status_code == 200 and "Bestände je Asset" in prev.text


def test_full_address_in_account_name_matches(cfg):
    rows = [tx("A1", "2022-12-13T10:00:00Z", "deposit", to=(f"Cardano {ADA_ADDR}", "ADA", "5"), value=2),
            tx("K0", "2024-01-02T08:00:00Z", "deposit", to=("Kraken", "EUR", "100"), value=100)]
    ctx = make_ctx(cfg, rows, ASSETS_W)
    add_source(ctx, "cardano", "ADA", "Ledger (ADA)", ADA_ADDR)
    c = next(c for c in AM.candidates(ctx) if c.target == "Ledger (ADA)")
    assert c.strong and "vollständige Adresse" in c.reasons[0]
