"""Public, synthetic regression for the restore collision found in the private export."""

# ruff: noqa: F401, F811
from __future__ import annotations

import pytest
from app.datasources.service import datasource_service
from app.jobs import tasks
from tests.test_full_export import _client, _other, _place
from tests.test_review_fixes import c, cfg


@pytest.mark.parametrize("same_name", [False, True])
def test_restore_preserves_distinct_wallet_addresses_even_with_the_same_name(
    c, tmp_path, same_name
):
    cx = c.app.state.ctx
    source = datasource_service(cx)
    for i in [1, 2]:
        sid, errors = source.create(
            {
                "kind": "wallet",
                "provider": "ethereum",
                "name": "Wallet" if same_name else f"Wallet {i}",
                "account": "Wallet K",
                "address": "0x" + str(i) * 40,
                "sync_interval_min": "1440",
                "auto_commit": "0",
            }
        )
        assert sid and not errors, errors
    assert cx.db.scalar("SELECT COUNT(*) FROM data_source") == 2
    body = c.get("/journal/export.zip").content
    config = _other(cx.config, tmp_path, "restore-wallets")
    _place(config, body, "wallets.zip")
    with _client(config) as restored:
        other = restored.app.state.ctx
        result = tasks.import_check(other, "review")
        assert result.status == "imported"
        addresses = {r[0] for r in other.db.q("SELECT address FROM data_source")}
        assert addresses == {"0x" + "1" * 40, "0x" + "2" * 40}, {
            "restored_addresses": len(addresses)
        }
