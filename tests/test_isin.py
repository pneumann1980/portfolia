"""Automatische ISIN-Ermittlung (Bankbelege, Onvista, DE-WKN-Regel) und Einbindung in die Steuerauswertung."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from app.prices.isin import KEY, IsinService, de_isin_from_wkn, isin_check_digit, onvista_lookup
from app.util.http import HttpError
from tests.helpers import portfolio
from tests.test_tax_de import TAX_ASSETS, buy, run, sell

D = Decimal
NO_WAIT = None


def test_check_digit_and_german_wkn_rule():
    assert isin_check_digit("US037833100") == "5"  # Apple
    assert de_isin_from_wkn("A0TGJ5") == "DE000A0TGJ55"  # VARTA AG
    assert de_isin_from_wkn("716460") == "DE0007164600"  # SAP SE
    assert de_isin_from_wkn("zu kurz") is None and de_isin_from_wkn("") is None


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _hit(wkn, isin, name="Test AG"):
    return {"type": "Instrument", "name": name, "isin": isin, "wkn": wkn}


def _search(items):
    return lambda request: httpx.Response(200, json={"searchValue": request.url.params["searchValue"], "list": items})


def test_onvista_lookup_accepts_exact_unique_hits_only():
    ok = _client(_search([_hit("A14Y6F", "US02079K3059", "Alphabet A (Google)"), _hit("ZZZZZZ", "US0378331005")]))
    assert onvista_lookup(ok, "a14y6f") == ("US02079K3059", "Alphabet A (Google)")
    assert onvista_lookup(_client(_search([_hit("A14Y6F", "US02079K3058")])), "A14Y6F") is None  # Prüfziffer falsch
    assert onvista_lookup(_client(_search([_hit("A14Y6F", "US02079K3059"), _hit("A14Y6F", "US0378331005")])),
                          "A14Y6F") is None  # mehrdeutig
    assert onvista_lookup(_client(_search([])), "A14Y6F") is None
    same = _client(_search([_hit("A14Y6F", "US02079K3059", "A"), _hit("A14Y6F", "US02079K3059", "B")]))
    assert onvista_lookup(same, "A14Y6F")[0] == "US02079K3059"  # gleiche ISIN an mehreren Plätzen
    with pytest.raises(HttpError):
        onvista_lookup(_client(lambda r: httpx.Response(403)), "A14Y6F")


class _Settings:
    version = 1

    def __init__(self):
        self.v = {}

    def get(self, key, default=None):
        return self.v.get(key, default)

    def set(self, key, value):
        self.v[key] = json.loads(json.dumps(value))


def _ctx(assets, docs=(), demo=False):
    pf = portfolio([], [{"quote_source": "none", "quote_id": "", **a} for a in assets])
    db = SimpleNamespace(q=lambda sql, *a: [{"meta": m} for m in docs])
    return SimpleNamespace(settings=_Settings(), portfolio=lambda: pf, db=db, http=None,
                           config=SimpleNamespace(demo_mode=demo))


SEC = [{"asset_id": "WKN:A14Y6F", "name": "Alphabet A", "asset_class": "security", "wkn": "A14Y6F"},
       {"asset_id": "WKN:865985", "name": "Apple", "asset_class": "security", "wkn": "865985"},
       {"asset_id": "WKN:DFZ11D", "name": "ENDLOS-TURBO CALL AUF SAP SE", "asset_class": "security", "wkn": "DFZ11D"},
       {"asset_id": "WKN:ZZZ999", "name": "Unbekannt AG", "asset_class": "security", "wkn": "ZZZ999"},
       {"asset_id": "ETH", "name": "Ethereum", "asset_class": "crypto"},
       {"asset_id": "WKN:A0AAAA", "name": "Hat ISIN", "asset_class": "security", "wkn": "A0AAAA",
        "isin": "DE000A0AAAA3"}]


def test_resolve_sources_in_order_and_storage():
    answers = {"A14Y6F": [_hit("A14Y6F", "US02079K3059", "Alphabet A (Google)")], "DFZ11D": [], "ZZZ999": []}
    calls = []

    def handler(request):
        w = request.url.params["searchValue"]
        calls.append(w)
        return httpx.Response(200, json={"list": answers[w]})

    docs = [json.dumps({"isin": "US0378331005", "wkn": "865985", "side": "buy"}), "{kaputt", json.dumps({"isin": "X"})]
    ctx = _ctx(SEC, docs)
    st = IsinService(ctx).resolve(client=_client(handler), sleep_s=0)
    assert st["found"] == 3 and st["local"] == 1 and st["online"] == 1 and st["computed"] == 1 and st["none"] == 1
    assert calls == ["A14Y6F", "DFZ11D", "ZZZ999"]  # Apple kommt aus dem Bankbeleg, keine Anfrage
    e = ctx.settings.get(KEY)
    assert e["WKN:865985"]["isin"] == "US0378331005" and e["WKN:865985"]["source"] == "Bankbeleg"
    assert e["WKN:A14Y6F"]["isin"] == "US02079K3059" and e["WKN:A14Y6F"]["source"] == "Onvista"
    assert e["WKN:DFZ11D"]["isin"] == "DE000DFZ11D6" and "berechnet" in e["WKN:DFZ11D"]["source"]
    assert e["WKN:ZZZ999"]["isin"] is None
    assert "ETH" not in e and "WKN:A0AAAA" not in e  # Krypto und Assets mit ISIN nicht
    assert IsinService(ctx).mapping() == {"WKN:865985": "US0378331005", "WKN:A14Y6F": "US02079K3059",
                                          "WKN:DFZ11D": "DE000DFZ11D6"}
    last = IsinService(ctx).last()
    assert last["found"] == 3 and last["open"] == 0
    # zweiter Lauf: nichts mehr offen (negatives Ergebnis wird erst nach 14 Tagen erneut gesucht)
    calls.clear()
    assert IsinService(ctx).resolve(client=_client(handler), sleep_s=0)["found"] == 0 and calls == []
    old = (datetime.now(UTC) - timedelta(days=15)).isoformat()
    e["WKN:ZZZ999"]["checked"] = old
    ctx.settings.set(KEY, e)
    IsinService(ctx).resolve(client=_client(handler), sleep_s=0)
    assert calls == ["ZZZ999"]


def test_network_errors_are_not_stored_as_no_hit():
    def down(request):
        raise httpx.ConnectError("offline")

    ctx = _ctx(SEC[:2])
    svc = IsinService(ctx)
    st = svc.resolve(client=_client(down), sleep_s=0)
    assert st["errors"] == 1 and st["found"] == 0 and st["none"] == 0
    assert ctx.settings.get(KEY) is None and len(svc.pending()) == 2  # beim nächsten Lauf erneut
    demo = _ctx(SEC[:2], demo=True)  # Demo-Modus: kein Netzwerk
    assert IsinService(demo).resolve(sleep_s=0)["skipped"] == 2


def test_isin_map_feeds_tax_rows_and_derivative_flag():
    from app.tax.base import TaxInput

    rows = [buy("a1", "2024-01-10", "US1", 10, 1000, acc="DKB"), sell("a2", "2025-03-11", "US1", 10, 1650, acc="DKB")]
    _, res = run(rows, 2025, kinds={"DKB": "domestic"}, isin_map={"US1": "US0000000001"})
    sale = next(t for s in res.sections for t in s.tables if t.id == "k_sales").rows[0]
    assert sale["isin"] == "US0000000001"  # US1 hat in den Teststammdaten schon eine ISIN – bleibt maßgeblich
    pf = portfolio([], [*TAX_ASSETS, {"asset_id": "NOISIN", "name": "Ohne ISIN AG", "asset_class": "security",
                                      "wkn": "A1B2C3", "quote_source": "none", "quote_id": ""}])
    inp = TaxInput(pf=pf, ledger=None, today=None, isin_map={"NOISIN": "DE000A1B2C31"})  # type: ignore[arg-type]
    assert inp.isin_of(pf.asset("NOISIN")) == "DE000A1B2C31" and inp.isin_of(pf.asset("US1")) == "US0000000001"
    assert inp.isin_of(pf.asset("US2")) == "US0000000002" and inp.isin_of(pf.asset("ETH")) == ""
