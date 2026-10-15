"""Doppelte Börsen: Schutz beim Anlegen, Erkennung, Zusammenführen – nur synthetische Daten."""

# ruff: noqa: F811
from __future__ import annotations

import re

from app.csvimport.service import csv_service
from app.datasources.service import datasource_service
from tests.test_csvimport import post
from tests.test_datasources import client, create_source  # noqa: F401
from tests.test_universal_import import kraken_rows, make_xlsx


def svc(c):
    return datasource_service(c.app.state.ctx)


def upload(c, sid, name):
    r = c.post(f"/settings/datasources/{sid}/upload", data={"csrf_token": c.token, "mode": "all"},
               files={"file": (name, make_xlsx(kraken_rows(), date_cols=(2,)), "application/octet-stream")},
               follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    return int(re.search(r"/journal/csv/(\d+)", r.headers["location"]).group(1))


def two_sources(c):
    a = create_source(c, name="Kraken A", account="Kraken", credential_ref="PORTFOLIA_DS_A")
    b = create_source(c, name="Kraken B", account="Kraken B", credential_ref="PORTFOLIA_DS_B")
    return a, b


def test_creating_the_same_exchange_account_twice_is_refused(client):
    c = client
    a = create_source(c, name="Kraken A", account="Kraken")
    r = post(c, "/settings/datasources", kind="exchange", provider="kraken", name="Kraken zweimal",
             account="kraken", sync_interval_min="0")
    assert r.status_code == 400 and "gibt es bereits die Datenquelle" in r.text and "Kraken A" in r.text
    assert len(svc(c).list()) == 1
    assert 'name="allow_duplicate" value="1"' in r.text  # ausdrückliche Bestätigung möglich
    ok = post(c, "/settings/datasources", kind="exchange", provider="kraken", name="Kraken Zweitzugang",
              account="Kraken", sync_interval_min="0", allow_duplicate="1")
    assert ok.status_code == 303 and len(svc(c).list()) == 2 and len(svc(c).duplicates()) == 1
    c.app.state.ctx.db.x("DELETE FROM data_source WHERE name='Kraken Zweitzugang'")
    # anderes Konto derselben Börse bleibt möglich; ebenso dasselbe Konto bei anderer Börse
    assert post(c, "/settings/datasources", kind="exchange", provider="kraken", name="Kraken 2", account="Kraken 2",
                sync_interval_min="0").status_code == 303
    assert post(c, "/settings/datasources", kind="exchange", provider="mexc", name="MEXC", account="Kraken",
                sync_interval_min="0").status_code == 303
    assert a and svc(c).duplicates() == []


def test_duplicates_are_listed_previewed_and_merged(client):
    c = client
    a, b = two_sources(c)
    ba, bb = upload(c, a, "a.xlsx"), upload(c, b, "b.xlsx")
    assert svc(c).duplicates() == []  # verschiedene Konten
    p = svc(c).merge_preview(a, b)
    assert any("Verschiedene Konten" in e for e in p["errors"])
    # Konto angleichen (Bearbeiten) – nur beim Anlegen wird geprüft
    form = {"name": "Kraken B", "provider": "kraken", "account": "Kraken", "sync_interval_min": "0",
            "credential_ref": "PORTFOLIA_DS_B"}
    assert post(c, f"/settings/datasources/{b}", **form).status_code == 303
    d = svc(c).duplicates()
    assert len(d) == 1 and {int(d[0]["a"].id), int(d[0]["b"].id)} == {a, b}
    assert "Doppelte Datenquellen" in c.get("/settings/datasources").text
    page = c.get(f"/settings/datasources/merge?keep={a}&drop={b}").text
    assert "überführen" in page and "keine Buchung" in page
    fp = re.search(r'name="fp" value="([0-9a-f]+)"', page).group(1)
    before_bookings = c.app.state.ctx.db.scalar("SELECT COUNT(*) FROM journal_tx")
    # ohne Bestätigung bzw. mit falscher Prüfsumme passiert nichts
    assert "error=" in post(c, "/settings/datasources/merge", keep=a, drop=b, fp=fp).headers["location"]
    assert "error=" in post(c, "/settings/datasources/merge", keep=a, drop=b, fp="x", confirm="1").headers["location"]
    assert svc(c).get(b) is not None
    r = post(c, "/settings/datasources/merge", keep=a, drop=b, fp=fp, confirm="1")
    assert "msg=" in r.headers["location"] and svc(c).get(b) is None and svc(c).get(a) is not None
    db = c.app.state.ctx.db
    assert {r["datasource_id"] for r in db.q("SELECT datasource_id FROM csv_batch WHERE id IN (?,?)", (ba, bb))} == {a}
    assert db.scalar("SELECT COUNT(*) FROM journal_tx") == before_bookings  # Buchungen unberührt
    assert svc(c).duplicates() == []
    assert svc(c).merge(a, b, fp) != []  # idempotent: zweite Quelle gibt es nicht mehr
    assert csv_service(c.app.state.ctx).batch(bb) is not None  # Prüf-Stapel bleiben erhalten


def test_merge_refuses_other_providers_and_wallets_without_overlap(client):
    c = client
    k = create_source(c, name="Kraken", account="Konto", credential_ref="PORTFOLIA_DS_K")
    m = create_source(c, provider="mexc", name="MEXC", account="Konto", credential_ref="")
    assert any("desselben Anbieters" in e for e in svc(c).merge_preview(k, m)["errors"])
    assert svc(c).merge_preview(k, k)["errors"] and svc(c).merge(k, 9999, "x")
    w1 = post(c, "/settings/datasources", kind="wallet", provider="ethereum", name="W1", account="Wallet",
              address="0x" + "1" * 40, sync_interval_min="0", tokens="1", gap="20")
    w2 = post(c, "/settings/datasources", kind="wallet", provider="ethereum", name="W2", account="Wallet",
              address="0x" + "2" * 40, sync_interval_min="0", tokens="1", gap="20")
    assert w1.status_code == 303 and w2.status_code == 303
    ids = [int(x.id) for x in svc(c).list() if x.is_wallet]
    assert any("verschiedene Adressen" in e for e in svc(c).merge_preview(ids[0], ids[1])["errors"])
    assert svc(c).duplicates() == []
