"""Wirtschaftliche Ereignisgruppen über Quellen (Financial Integrity, Phase 6) – nur synthetische Daten.

Abgedeckt: Gruppensicht einer verknüpften Buchung (Bitpanda-API + Koinly-Import), Gebühr brutto/netto widersprüchlich,
Mengenwiderspruch vs. durch Gebühr erklärte Differenz, 1:n, n:1 aus derselben Quelle, teilweise verknüpftes Ereignis,
doppelt gebuchtes Ereignis, Nur-Lese-Garantie.
"""

# ruff: noqa: F811
from __future__ import annotations

import json
import re

from app.csvimport import groups as G
from app.csvimport.service import csv_service
from tests.test_csvimport import post
from tests.test_importcheck import api, client, scenario  # noqa: F401
from tests.test_review_workflow import snapshot


def ctx(c):
    return c.app.state.ctx


def link_all(c, bid: int) -> None:
    r = c.get(f"/journal/csv/{bid}/preview?action=suggest")
    form = dict(re.findall(r'name="(token|fingerprint|action|force)" value="([^"]*)"', r.text))
    assert post(c, f"/journal/csv/{bid}/execute", **form).status_code == 303


def add_link(c, tx_id: str, source: str, ext: str, record: dict, role: str = "same") -> None:
    ctx(c).db.x("INSERT INTO tx_link(tx_id, source, ext_id, role, record_json, status, created_at) VALUES "
                "(?,?,?,?,?,'active','2026-01-01T00:00:00Z')", (tx_id, source, ext, role, json.dumps(record)))


def test_group_view_shows_all_sources_of_one_booking(client, config, api):
    c = client
    bid, _rows = scenario(c, config, api, "extra")
    link_all(c, bid)
    g = G.group(ctx(c), "KOI-T")
    assert g.booking is not None and g.booking["origin"] == "import"
    assert g.sources == ["sync:bitpanda"] and g.members[0].record["fee_basis"] == "extra"
    assert not g.conflicts
    page = c.get("/journal/ereignis/KOI-T").text
    assert "Quellen dieses Vorgangs" in page and "zusätzlich (brutto)" in page and "stimmig" in page
    assert c.get("/journal/ereignis/GIBT-ES-NICHT").status_code == 404


def test_fee_basis_and_quantity_conflicts(client, config, api):
    c = client
    bid, _rows = scenario(c, config, api, "extra")
    link_all(c, bid)
    # Steuertool-Export behauptet: Gebühr im Betrag enthalten (netto) → Widerspruch zur Börse (brutto)
    add_link(c, "KOI-T", "csv:koinly", "K-T", {"fee_basis": "inside", "out": ["Bitpanda", "USDT", "734.512861"]},
             role="out")
    # Differenz genau um die Gebühr: brutto/netto, kein Mengenwiderspruch
    add_link(c, "KOI-T", "doc:beleg", "B-1", {"out": ["Bitpanda", "USDT", "745.79751117"]}, role="out")
    g = G.group(ctx(c), "KOI-T")
    kinds = [k for k, _t in g.conflicts]
    assert kinds == ["fee_basis"], g.conflicts
    add_link(c, "KOI-T", "doc:beleg2", "B-2", {"out": ["Bitpanda", "USDT", "700"]}, role="out")
    g = G.group(ctx(c), "KOI-T")
    assert [k for k, _t in g.conflicts] == ["fee_basis", "qty"]
    assert "nicht durch die Gebühr erklärt" in g.conflicts[1][1]


def test_one_to_many_and_many_to_one_and_partial_and_double(client, config, api):
    c = client
    bid, _rows = scenario(c, config, api, "extra")
    db = ctx(c).db
    add_link(c, "KOI-D", "csv:x", "R-1", {})
    add_link(c, "KOI-T", "csv:x", "R-1", {})  # 1:n
    add_link(c, "KOI-T", "csv:y", "P-1", {}, role="part")
    add_link(c, "KOI-D", "csv:y", "P-1", {}, role="part")  # Teil-Zuordnung: kein 1:n-Widerspruch
    add_link(c, "KOI-D", "csv:z", "Z-1", {})
    add_link(c, "KOI-D", "csv:z", "Z-2", {})  # n:1 aus derselben Quelle
    # teilweise verknüpftes Ereignis: eine Zeile verknüpft, eine offen
    rows = csv_service(ctx(c)).rows(bid)
    db.x("UPDATE csv_row SET event_key='EV-SPLIT' WHERE batch_id=?", (bid,))
    db.x("UPDATE csv_row SET status='linked', tx_id='KOI-T' WHERE batch_id=? AND idx=?", (bid, rows[0].idx))
    # doppelt gebuchtes Ereignis
    for tx in ("PF-X-1", "PF-X-2"):
        db.x("INSERT INTO journal_tx(tx_id, source, external_id, status, ts_utc, type, to_account, to_asset, to_qty, "
             "created_at, updated_at, event_key, event_line) VALUES (?, 'sync:kraken', ?, 'active', "
             "'2024-01-01T00:00:00Z', 'deposit', 'Kraken', 'EUR', '1', 'x', 'x', 'kraken:E1', 0)", (tx, tx))
    before = snapshot(c)
    found = G.scan(ctx(c))
    kinds = sorted({x.kind for x in found})
    assert kinds == ["1:n", "double", "n:1", "partial"], [(x.kind, x.text) for x in found]
    one_n = next(x for x in found if x.kind == "1:n")
    assert one_n.tx_ids == ["KOI-D", "KOI-T"] and "csv:x R-1" in one_n.text
    assert next(x for x in found if x.kind == "partial").batch_id == bid
    assert next(x for x in found if x.kind == "double").tx_ids == ["PF-X-1", "PF-X-2"]
    page = c.get("/journal/ereignisse").text
    assert "Widersprüche zwischen Quellen" in page and "Ereignis doppelt gebucht" in page
    assert c.get("/journal/ereignisse?kind=1:n").status_code == 200
    assert snapshot(c) == before  # nur lesend
