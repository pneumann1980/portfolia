"""Ursachen von Bestandsabweichungen, die sich rechnerisch belegen lassen (0.31) – synthetisch nachgebildet.

* Zugang einer Transaktion doppelt gebucht (Einzahlung **und** Zielseite eines Tauschs, Rundung der Quelle): ausblenden
  würde die reine Einzahlung; der Tausch behält sein Abgangsbein.
* Zwei Kennungen derselben Anlage auf einem Konto (Börse meldet 100.000, Buchungen 80.000 + 20.000 unter Zwilling):
  Empfehlung Zusammenführen statt Ausgleichsbuchung.
* Rundungsrest der Quelle (12 gegenüber 9 Nachkommastellen) zählt in der Klärung als erfüllt.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from app.config import Config, Secrets
from app.diagnosis import clarify as K
from app.diagnosis.collect import Observed, SourceState, collect
from app.diagnosis.engine import diagnose, report_for
from app.diagnosis.recommend import recommend
from app.util.timeutil import utcnow
from tests.helpers import ASSETS, tx
from tests.test_diagnosis import H, asset, by_kind, make_ctx


@pytest.fixture
def cfg(tmp_path) -> Config:
    imp = tmp_path / "import"
    imp.mkdir(parents=True, exist_ok=True)
    return Config(data_dir=tmp_path / "data", import_dir=imp, demo_mode=True, scheduler_enabled=False,
                  startup_jobs=False, log_format="text", secrets=Secrets())


def _src(rows: list[dict]) -> list[dict]:
    for r in rows:
        r["source"] = "koinly"
        r["source_ref"] = f"REF{r['tx_id']}"
    return rows


def _meat_rows() -> list[dict]:
    """Eine Transaktion, zwei Zugänge: Einzahlung 13.124.799,493892 und Tausch COQ → MEAT 13.124.799,494."""
    fund = tx("F1", "2024-01-01T10:00:00Z", "deposit", to=("Wallet A", "COQ", "107631574.703346833"), value="1")
    dep = tx("D1", "2024-01-08T07:12:01Z", "deposit", to=("Wallet A", "MEAT", "13124799.493892"), value="0")
    swap = tx("S1", "2024-01-08T07:12:01Z", "trade", frm=("Wallet A", "COQ", "107631574.703346833"),
              to=("Wallet A", "MEAT", "13124799.494"), value="0")
    dep["note"] = swap["note"] = f"txhash=0x{H(7)}"
    return _src([fund, dep, swap])


MEAT_ASSETS = [*ASSETS, asset("MEAT"), asset("COQ")]


def test_credit_of_one_transaction_booked_twice_is_found_and_the_plain_deposit_is_the_surplus(cfg):
    ctx = make_ctx(cfg, _meat_rows(), MEAT_ASSETS)
    rep = report_for(ctx)
    (f,) = [x for x in by_kind(rep, "duplicate") if "MEAT" in x.title]
    assert f.status == "wahrscheinlich" and f.data["type"] == "same_qty" and f.data["twin"] is True
    assert (f.data["weak"], f.data["strong"]) == ("D1", "S1")  # die Einzahlung ohne Gegenleistung ist zu viel
    assert any("hat ein Gegenstück" in e and "COQ" in e for e in f.evidence)
    assert f.data["hashes"] == [H(7)]
    r = recommend(rep, f)
    assert "Dieselbe Transaktion ist zweimal als Zugang gebucht" in r.text and r.options[0].key == "hide_weak"


def test_two_credits_with_different_ids_of_one_hash_are_not_flagged_when_quantities_differ(cfg):
    rows = _meat_rows()
    rows[2]["to_qty"] = "9000000"  # eine andere Bewegung derselben Transaktion
    rep = report_for(make_ctx(cfg, rows, MEAT_ASSETS))
    assert not [x for x in by_kind(rep, "duplicate") if "MEAT" in x.title]


def test_round_quantities_alone_are_no_evidence(cfg):
    rows = _meat_rows()
    rows[1]["to_qty"] = rows[2]["to_qty"] = "13000000"
    rep = report_for(make_ctx(cfg, rows, MEAT_ASSETS))
    assert not [x for x in by_kind(rep, "duplicate") if "MEAT" in x.title]


def test_rounding_residue_of_the_source_counts_as_fulfilled_in_the_clarification():
    assert K._rounded(Decimal("0.000108"), Decimal("45247147.176537")) == 0  # 2e-12 relativ (Koinly 3 Stellen)
    assert K._rounded(Decimal("1.4e-11"), Decimal("32.258636955014")) == 0
    assert K._rounded(Decimal("0.5"), Decimal("45247147.176537")) == Decimal("0.5")  # echte Differenz bleibt
    assert K._rounded(Decimal("1e-7"), Decimal("1")) == Decimal("1e-7")


def test_twin_asset_with_the_missing_quantity_recommends_merging_instead_of_an_adjustment(cfg):
    qid = "threshold-network-token"
    assets = [*ASSETS, asset("TT", qs="coingecko", qid=qid), asset("TH", qs="coingecko", qid=qid)]
    rows = _src([tx("B1", "2026-09-01T10:00:00Z", "buy", frm=("Exchange", "EUR", "400"),
                    to=("Exchange", "TT", "80000"), value="400"),
                 tx("B2", "2026-09-24T07:27:45Z", "buy", frm=("Exchange", "EUR", "104.29"),
                    to=("Exchange", "TH", "20000"), value="104.29"),
                 tx("F1", "2026-08-01T10:00:00Z", "deposit", to=("Exchange", "EUR", "600"), value="600")])
    ctx = make_ctx(cfg, rows, assets, valuation="2026-10-01")
    # keine Datenquelle in der Testumgebung: Beobachtung der Börse (Bitpanda meldet „TH“ → Asset TT) einspeisen
    state = SourceState(id=1, name="Exchange", kind="exchange", provider="bitpanda", provider_label="Bitpanda",
                        account="Exchange", state="vollständig synchronisiert", complete=True,
                        last_success_at=utcnow())
    obs = Observed(source_id=1, key="TH@BITPANDA", name=None, qty=Decimal("100000"), observed_at=utcnow(),
                   asset_id="TT", how="saved")
    rep = diagnose(replace(collect(ctx), sources=[state], observed=[obs]))
    (f,) = [x for x in rep.findings if x.kind == "holdings" and "TT" in x.title and "externen Quelle" in x.title]
    r = recommend(rep, f)
    assert "Vermutlich dieselbe Anlage unter zwei Kennungen" in r.text and "TH" in r.text
    assert any(link.url == "/assets/merge?alias=TH&target=TT" for link in r.links)
    assert "doppelt zählen" in r.text
