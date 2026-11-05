"""An unchanged curated transaction must not become new merely because it has a source link."""

# ruff: noqa: F401, F811
from __future__ import annotations

import pytest
from app.csvimport import batch as BA
from app.csvimport.service import csv_service
from tests.helpers import ASSETS, tx
from tests.test_diagnosis import PF_HEADER, client_with_import, upload
from tests.test_review_fixes import cfg, form_values, post


def _execute(client, batch, action):
    cx = client.app.state.ctx
    svc = csv_service(cx)
    BA.set_selection(cx.db, batch, {r.idx for r in svc.rows(batch) if r.open})
    preview = client.get(f"/journal/csv/{batch}/preview?action={action}")
    assert preview.status_code == 200
    values = form_values(preview.text, f"/journal/csv/{batch}/execute")
    return post(client, f"/journal/csv/{batch}/execute", values)


@pytest.mark.parametrize("already_linked", [False, True])
def test_default_proposal_does_not_rebook_an_unchanged_import_transaction(
    cfg, already_linked
):
    client = client_with_import(
        cfg,
        [
            tx(
                "D1",
                "2025-01-01T10:00:00Z",
                "deposit",
                to=("Wallet K", "KAS", "10"),
                value="1",
            )
        ],
        ASSETS,
    )
    try:
        cx = client.app.state.ctx
        line = ",2025-01-01T10:00:00Z,deposit,,,,,Wallet K,KAS,10,,,,1,\n"
        if already_linked:
            first = upload(client, "first-source.csv", PF_HEADER + "SOURCE1" + line)
            assert _execute(client, first, "link").status_code == 303
            assert (
                cx.db.scalar("SELECT COUNT(*) FROM tx_link WHERE status='active'") == 1
            )
        replay = upload(client, "unchanged-curated-row.csv", PF_HEADER + "D1" + line)
        before = len(cx.recorded_portfolio().txs)
        response = _execute(client, replay, "suggest")
        after = len(cx.recorded_portfolio().txs)
        assert after == before, {
            "http": response.status_code,
            "before": before,
            "after": after,
        }
    finally:
        client.__exit__(None, None, None)
