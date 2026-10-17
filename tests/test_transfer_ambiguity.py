"""Transfer-Paare: Kandidatensuche und Freigabe sind getrennt (Visuelle Fallklärung, Paket 1, Punkt 3.1).

Synthetische Daten. Regeln: automatisch angenommen („hoch“) wird nur mit Identitätsbeleg (gleicher Onchain-Hash,
Menge ohne ungeklärte Differenz) und ohne konkurrierende Ansprüche; Menge und Zeit allein ergeben nur einen Vorschlag;
mehrdeutige Konstellationen und leer ausgegangene Zeilen bleiben offen – und werden nie still als Einzelvorgang gebucht.
"""

from __future__ import annotations

import csv
import io
from typing import Any

import pytest

from app.csvimport.service import csv_service
from app.datasources.service import DataSourceService
from tests.test_csvimport import create_unknown_assets, post
from tests.test_importcheck import KOINLY_HEAD, _upload_csv
from tests.test_journal import _client

HEAD = [c.strip() for c in KOINLY_HEAD.strip().split(",")]


def line(date: str, typ: str, *, wallet: str, qty: str, cur: str = "ETH", txhash: str = "") -> list[str]:
    row = dict.fromkeys(HEAD, "")
    row["Date"], row["Type"], row["Net Value (EUR)"] = date, typ, "3000"
    row["Gain (EUR)"] = row["Fee Value (EUR)"] = "0"
    if typ == "withdrawal":
        row["Sending Wallet"], row["Sent Amount"], row["Sent Currency"] = wallet, qty, cur
    else:
        row["Receiving Wallet"], row["Received Amount"], row["Received Currency"] = wallet, qty, cur
    row["TxHash"] = txhash
    return [row[c] for c in HEAD]


def sheet(*rows: list[str]) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(HEAD)
    w.writerows(rows)
    return out.getvalue()


H1, H2, H3 = ("0x" + c * 64 for c in "abc")
T0, T1 = "2024-06-01 10:00:00 UTC", "2024-06-01 10:20:00 UTC"


@pytest.fixture
def c(config):
    with _client(config) as client:
        client.get("/settings")
        client.token = client.cookies.get("portfolia_csrf")
        yield client


def pairs(c: Any, text: str, account: str = "Sammelkonto") -> dict[str, Any]:
    bid = _upload_csv(c, "koinly.csv", text, account)
    create_unknown_assets(c, bid)
    return {"bid": bid, "rows": sorted(csv_service(c.app.state.ctx).rows(bid), key=lambda rc: rc.line)}


def test_two_by_two_without_identity_is_not_paired_automatically(c):
    """U,V gehen je 1 ETH ab, W,X kommen je 1 ETH an, gleiche Zeit, kein Hash: mehrere Zuordnungen möglich – keine
    darf allein durch die Reihenfolge angenommen werden."""
    out = pairs(c, sheet(line(T0, "withdrawal", wallet="Börse U", qty="1"),
                         line(T0, "withdrawal", wallet="Börse V", qty="1"),
                         line(T1, "deposit", wallet="Wallet W", qty="1"),
                         line(T1, "deposit", wallet="Wallet X", qty="1")))
    rows = out["rows"]
    assert len(rows) == 4
    from app.csvimport.service import pair_accepted

    for rc in rows:
        assert rc.pair_conf == "mittel" and not pair_accepted(rc), rc.line
        assert rc.transfer_unclear and rc.pair_alts, rc.line  # die jeweils andere Möglichkeit bleibt sichtbar
        assert "mehrdeutig" in (rc.pair_why or "")
    assert not DataSourceService._auto_eligible(rows)  # nie automatisch gebucht


def test_unmatched_row_in_a_competition_stays_open_instead_of_being_booked_as_single_movement(c):
    """Drei Abgänge, zwei Zugänge: die leer ausgegangene Zeile hat konkurrierende Kandidaten – sie bleibt offen."""
    out = pairs(c, sheet(line(T0, "withdrawal", wallet="Börse U", qty="1"),
                         line(T0, "withdrawal", wallet="Börse V", qty="1"),
                         line(T0, "withdrawal", wallet="Börse Y", qty="1"),
                         line(T1, "deposit", wallet="Wallet W", qty="1"),
                         line(T1, "deposit", wallet="Wallet X", qty="1")))
    unpaired = [rc for rc in out["rows"] if rc.pair_ref is None]
    assert len(unpaired) == 1 and unpaired[0].pair_alts and unpaired[0].transfer_unclear
    assert unpaired[0].line not in DataSourceService._auto_eligible(out["rows"]) and \
        unpaired[0].idx not in DataSourceService._auto_eligible(out["rows"])
    # offen heißt entscheidbar: als echter Einzelvorgang buchbar, wenn der Nutzer das so entscheidet
    assert post(c, f"/journal/csv/{out['bid']}/rows", **{f"dec_{unpaired[0].idx}": "include"}).status_code == 303


def test_equal_hash_with_exact_amount_is_the_only_automatic_case(c):
    out = pairs(c, sheet(line(T0, "withdrawal", wallet="Börse U", qty="1", txhash=H1),
                         line(T1, "deposit", wallet="Wallet W", qty="1", txhash=H1)))
    w, d = out["rows"]
    assert w.pair_conf == d.pair_conf == "hoch" and not w.pair_alts and not w.transfer_unclear
    assert "gleiche Blockchain-Transaktion" in (w.pair_why or "")


def test_two_by_two_is_resolved_by_hashes_not_by_order(c):
    """Gleiche Menge, gleiche Zeit – aber jeder Abgang trägt den Hash seines Zugangs (über Kreuz geliefert)."""
    out = pairs(c, sheet(line(T0, "withdrawal", wallet="Börse U", qty="1", txhash=H1),
                         line(T0, "withdrawal", wallet="Börse V", qty="1", txhash=H2),
                         line(T1, "deposit", wallet="Wallet X", qty="1", txhash=H2),
                         line(T1, "deposit", wallet="Wallet W", qty="1", txhash=H1)))
    u, v, x, w = out["rows"]
    assert u.pair_ref == f"b:{w.idx}" and v.pair_ref == f"b:{x.idx}"
    assert all(rc.pair_conf == "hoch" and not rc.pair_alts for rc in out["rows"])


def test_different_verified_hashes_exclude_a_pairing(c):
    """Dieselbe behauptete Onchain-Bewegung hat einen Hash – zwei verschiedene verifizierte Hashes schließen die
    Verknüpfung aus (ohne zusätzlichen Beleg bleibt es offen, z. B. bei einer Bridge)."""
    out = pairs(c, sheet(line(T0, "withdrawal", wallet="Börse U", qty="1", txhash=H1),
                         line(T1, "deposit", wallet="Wallet W", qty="1", txhash=H3)))
    w, d = out["rows"]
    assert w.pair_ref is None and d.pair_ref is None and not w.pair_alts


def test_equal_hash_but_unexplained_difference_is_only_a_suggestion(c):
    """Eine ungeklärte Mengendifferenz ist keine nachgewiesene Gebühr: gleicher Hash, aber nur 60 % → Vorschlag."""
    out = pairs(c, sheet(line(T0, "withdrawal", wallet="Börse U", qty="1", txhash=H1),
                         line(T1, "deposit", wallet="Wallet W", qty="0.6", txhash=H1)))
    w, d = out["rows"]
    assert w.pair_ref == f"b:{d.idx}" and w.pair_conf == "mittel" and w.transfer_unclear
    assert "ungeklärt" in (w.pair_why or "")


def test_similar_amount_and_time_alone_is_only_a_suggestion(c):
    out = pairs(c, sheet(line(T0, "withdrawal", wallet="Börse U", qty="1"),
                         line(T1, "deposit", wallet="Wallet W", qty="0.999")))
    w, d = out["rows"]
    assert w.pair_ref == f"b:{d.idx}" and w.pair_conf == "mittel" and not w.pair_alts
    assert "ohne Identitätsbeleg" in (w.pair_why or "") and w.transfer_unclear
    assert not DataSourceService._auto_eligible(out["rows"])
    # bestätigt wird er erst durch eine Entscheidung
    post(c, f"/journal/csv/{out['bid']}/rows", **{f"pair_{w.idx}": "1"})
    w2 = next(rc for rc in csv_service(c.app.state.ctx).rows(out["bid"]) if rc.idx == w.idx)
    assert w2.pair_ok == 1 and not w2.transfer_unclear


def test_unrelated_movements_stay_independent_and_automatic(c):
    """Kein Kandidat (andere Menge, anderes Asset): unverbundene echte Vorgänge bleiben unverändert automatisch."""
    out = pairs(c, sheet(line(T0, "withdrawal", wallet="Börse U", qty="1"),
                         line(T1, "deposit", wallet="Wallet W", qty="0.1")))
    assert all(rc.pair_ref is None and not rc.pair_alts and not rc.transfer_unclear for rc in out["rows"])
