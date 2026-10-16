"""Prüf-Stapel: gleich große Vorgänge ↔ gleich viele vorhandene Buchungen werden einzeln zugeordnet (0.29.3).

Auslöser (Beobachtung des Auftraggebers, strukturgleich mit anderen Werten nachgebildet): zwei gleich große Einzahlungen
der Börse im Minutenabstand, im Bestand zwei gleich große Buchungen der Steuersoftware. Beide Zeilen passten auf beide
Buchungen und standen deshalb als „Komplexe Zuordnung“ ohne Lösung da. Synthetische Daten, temporäre Datenbanken.
"""

from __future__ import annotations

import pytest

from app.csvimport.service import csv_service
from app.jobs import tasks
from tests import test_bitpanda as TB
from tests.helpers import tx
from tests.test_bitpanda import ctx
from tests.test_csvimport import post
from tests.test_importcheck import KOINLY_HEAD, _import, _upload_csv, scenario


@pytest.fixture
def api(monkeypatch):
    """Bitpanda-API laut Referenz (strenger Mock aus test_bitpanda)."""
    import httpx

    fake = TB.FakeBitpanda()
    monkeypatch.setattr(TB.B.BitpandaConnector, "transport", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(TB.B.BitpandaConnector, "sleep", staticmethod(lambda _s: None))
    return fake


@pytest.fixture
def client(config, monkeypatch):
    monkeypatch.setenv("PORTFOLIA_MASTER_KEY", TB.MASTER)
    with TB.make_client(config) as c:
        c.get("/settings")
        c.token = c.cookies.get("portfolia_csrf")
        yield c


def _row(ts: str, qty: str = "0.4321") -> str:
    return f"{ts} UTC,deposit,,,,,,Ledger,{qty},ETH,,,,0,1234,0,,,,\n"


def _setup(c, config, existing: list[tuple[str, str]], rows: list[str]) -> int:
    c.get("/settings")
    imp = []
    for tid, ts in existing:
        t = tx(tid, ts, "deposit", to=("Ledger", "ETH", "0.4321"), value="1234")
        t["source"] = "koinly"
        imp.append(t)
    _import(config, imp, valuation="2024-01-01")
    assert tasks.import_check(ctx(c), "test").status == "imported"
    return _upload_csv(c, "koinly.csv", KOINLY_HEAD + "".join(rows), "Ledger")


def _rows(c, bid):
    return sorted(csv_service(ctx(c)).rows(bid), key=lambda rc: rc.line)


def test_two_rows_two_bookings_are_assigned_one_to_one(client, config):
    c = client
    bid = _setup(c, config, [("IMP-A", "2024-04-01T10:46:00Z"), ("IMP-B", "2024-04-01T10:47:02Z")],
                 [_row("2024-04-01 10:47:00"), _row("2024-04-01 10:48:00")])
    r1, r2 = _rows(c, bid)
    # beide Zeilen passen auf beide Buchungen – die Zuordnung nach kleinstem Gesamtabstand: 60 s + 58 s
    assert r1.match["target"] == "IMP-A" and r2.match["target"] == "IMP-B"
    for rc in (r1, r2):
        assert rc.match["cat"] in ("dublette", "ergaenzung"), rc.match["cat"]
        assert rc.match["action"] == "link" and rc.match["conf"] == "hoch"
        assert rc.match["group"]["n"] == 2 and sorted(rc.match["group"]["targets"]) == ["IMP-A", "IMP-B"]
        assert not any("weitere Zeile" in d["t"] or "mehrere Kandidaten" in d["t"] for d in rc.match["diff"])
        assert rc.dup_of[0] == rc.match["target"]
    # jede vorhandene Buchung höchstens einmal
    assert len({r1.match["target"], r2.match["target"]}) == 2


def test_assignment_is_independent_of_row_order_and_minimises_total_gap(client, config):
    c = client
    # Zeilen 10:00:10 und 10:20:00; Buchungen 10:00:00 und 10:19:00 → (10 s + 60 s), nicht (19 min + 20 min)
    bid = _setup(c, config, [("IMP-X", "2024-04-01T10:19:00Z"), ("IMP-Y", "2024-04-01T10:00:00Z")],
                 [_row("2024-04-01 10:20:00"), _row("2024-04-01 10:00:10")])
    by_line = {rc.line: rc.match["target"] for rc in _rows(c, bid)}
    assert by_line == {2: "IMP-X", 3: "IMP-Y"}


def test_linking_one_row_keeps_the_other_assigned_to_the_free_booking(client, config):
    c = client
    bid = _setup(c, config, [("IMP-A", "2024-04-01T10:46:00Z"), ("IMP-B", "2024-04-01T10:47:02Z")],
                 [_row("2024-04-01 10:47:00"), _row("2024-04-01 10:48:00")])
    first = _rows(c, bid)[0]
    r = post(c, f"/journal/csv/{bid}/row-action", row_action=f"link:{first.idx}")
    assert r.status_code == 303
    (left,) = [rc for rc in _rows(c, bid) if rc.open]
    # die vergebene Buchung ist kein Kandidat mehr; die andere Zeile gehört zur freien Buchung
    assert left.match["target"] == "IMP-B" and left.dup_of == ["IMP-B"]
    assert left.match["cat"] in ("dublette", "ergaenzung") and left.match["action"] == "link"
    r = post(c, f"/journal/csv/{bid}/row-action", row_action=f"link:{left.idx}")
    assert r.status_code == 303
    assert not [rc for rc in _rows(c, bid) if rc.open]
    # keine Buchung entstanden: nur zwei verknüpfte Datensätze
    assert ctx(c).db.scalar("SELECT COUNT(*) FROM tx_link WHERE status='active'") == 2


def test_more_rows_than_bookings_stays_manual_with_suggestion(client, config):
    c = client
    bid = _setup(c, config, [("IMP-A", "2024-04-01T10:00:00Z")],
                 [_row("2024-04-01 10:00:00"), _row("2024-04-01 10:00:40")])
    r1, r2 = _rows(c, bid)
    for rc in (r1, r2):
        assert rc.match["cat"] == "komplex" and rc.match["action"] == "review"
        assert rc.match["conf"] not in ("sicher", "hoch")
        assert any("weitere Zeile" in d["t"] for d in rc.match["diff"])
    # Vorschlag nach Zeitabstand, nie automatisch: die nähere Zeile ↔ Buchung, die andere ist ein eigener Vorgang
    assert r1.match["suggest"] == {"act": "link", "target": "IMP-A"}
    assert r2.match["suggest"] == {"act": "include"}


def test_row_with_all_similar_bookings_already_taken_is_a_separate_booking(client, config):
    c = client
    bid = _setup(c, config, [("IMP-A", "2024-04-01T10:00:00Z")],
                 [_row("2024-04-01 10:00:00"), _row("2024-04-01 10:00:40")])
    first = _rows(c, bid)[0]
    assert post(c, f"/journal/csv/{bid}/row-action", row_action=f"link:{first.idx}").status_code == 303
    (left,) = [rc for rc in _rows(c, bid) if rc.open]
    assert left.match["cat"] == "neu" and left.match["action"] == "include"
    assert left.match["conf"] == "mittel" and any("anderen Datensatz" in h for h in left.match["hints"])


@pytest.mark.parametrize("n_rows", [3, 4])
def test_larger_equal_groups_get_distinct_targets(client, config, n_rows):
    c = client
    mins = [0, 3, 6, 9][:n_rows]
    bid = _setup(c, config, [(f"IMP-{i}", f"2024-04-01T10:{m:02d}:00Z") for i, m in enumerate(mins)],
                 [_row(f"2024-04-01 10:{m:02d}:30") for m in reversed(mins)])
    targets = [rc.match["target"] for rc in _rows(c, bid)]
    assert sorted(targets) == sorted(f"IMP-{i}" for i in range(n_rows))


def test_recommendation_is_plain_and_one_click(client, config):
    c = client
    bid = _setup(c, config, [("IMP-A", "2024-04-01T10:46:00Z"), ("IMP-B", "2024-04-01T10:47:02Z")],
                 [_row("2024-04-01 10:47:00"), _row("2024-04-01 10:48:00")])
    r1, _r2 = _rows(c, bid)
    rec = r1.match["rec"]
    assert rec["act"] == "link" and rec["target"] == "IMP-A" and rec["button"] == "Mit IMP-A verknüpfen"
    assert "bereits als IMP-A gebucht" in rec["text"] and "2 gleich große Vorgänge" in rec["text"]
    page = c.get(f"/journal/csv/{bid}").text
    assert "Empfehlung: Mit IMP-A verknüpfen" in page and "Empfehlung: Mit IMP-B verknüpfen" in page
    # ein Klick: der Knopf führt die Empfehlung per Einzelaktion aus
    assert f'name="row_action" value="link:{r1.idx}"' in page and "Mit IMP-A verknüpfen</button>" in page
    assert "zugeordnet" in page  # Vergleich: die zugeordnete Buchung ist gekennzeichnet
    assert "Andere Entscheidung" in page and "Technische Hinweise" in page


def test_surplus_rows_recommend_link_and_separate_booking(client, config):
    c = client
    bid = _setup(c, config, [("IMP-A", "2024-04-01T10:00:00Z")],
                 [_row("2024-04-01 10:00:00"), _row("2024-04-01 10:00:40")])
    r1, r2 = _rows(c, bid)
    assert r1.match["rec"]["act"] == "link" and r1.match["rec"]["tone"] == "info"
    assert r2.match["rec"]["act"] == "include" and "eigener Vorgang" in r2.match["rec"]["head"]


def test_single_action_stays_on_rows_until_batch_is_done_then_returns_to_overview(client, config):
    c = client
    bid = _setup(c, config, [("IMP-A", "2024-04-01T10:46:00Z"), ("IMP-B", "2024-04-01T10:47:02Z")],
                 [_row("2024-04-01 10:47:00"), _row("2024-04-01 10:48:00")])
    r1, r2 = _rows(c, bid)
    loc = post(c, f"/journal/csv/{bid}/row-action", row_action=f"link:{r1.idx}").headers["location"]
    assert loc.startswith(f"/journal/csv/{bid}") and loc.endswith("#zeilen")  # an der Zeilenliste, nicht oben
    loc = post(c, f"/journal/csv/{bid}/row-action", row_action=f"link:{r2.idx}").headers["location"]
    assert loc == "/journal/csv"  # alles geklärt → zurück zur Übersicht
    assert csv_service(ctx(c)).batch(bid)["status"] == "done"


def test_datasource_batch_returns_to_datasource_overview_when_done(client, config, api):
    c = client
    bid, rows = scenario(c, config, api, "extra")
    first, last = sorted(rows.values(), key=lambda rc: rc.line or 0)
    sid = csv_service(ctx(c)).batch(bid)["datasource_id"]
    loc = post(c, f"/journal/csv/{bid}/row-action", row_action=f"link:{first.idx}").headers["location"]
    assert loc.startswith(f"/journal/csv/{bid}") and loc.endswith("#zeilen")
    loc = post(c, f"/journal/csv/{bid}/row-action", row_action=f"link:{last.idx}").headers["location"]
    assert loc.startswith("/settings/datasources?msg=") and loc.endswith(f"#ds-{sid}")
    page = c.get(loc)
    assert page.status_code == 200 and "Prüfung abgeschlossen" in page.text
