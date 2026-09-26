"""Relevanzfilter für News: Wortgrenzen, mehrdeutige Kürzel nur mit Kontext, Ausschlüsse, Scoring."""

import pytest

from app.ledger.models import AssetInfo
from app.news.relevance import Matcher, is_clickbait

SETTINGS = {
    "ambiguous_terms": ["SUI", "TAP", "PI", "NIGHT", "ONE", "LINK", "KAS"],
    "context_words": ["crypto", "krypto", "coin", "token", "blockchain", "mainnet", "airdrop", "staking"],
    "exclude_patterns": {"SUI": ["sui generis"], "PI": ["raspberry pi"], "NIGHT": ["good night"]},
}


def asset(aid, name, cls, aliases, qs="coingecko", qid=None):
    return AssetInfo(aid, name, cls, qs, qid or aid.lower(), aliases=aliases)


ASSETS = [
    asset("SUI", "Sui", "crypto", ["SUI", "Sui Network"]),
    asset("PI#pi", "Pi Network", "crypto", ["PI", "Pi Network"]),
    asset("NIGHT#mn", "Midnight", "crypto", ["NIGHT", "Midnight Network"]),
    asset("TAP#tp", "TAP Protocol", "crypto", ["TAP", "TAP Protocol"]),
    asset("KAS", "Kaspa", "crypto", ["Kaspa", "KAS"]),
    asset("BTC", "Bitcoin", "crypto", ["Bitcoin", "BTC"]),
    asset("WKN:716460", "SAP SE", "security", ["SAP", "SAP SE"], "yahoo", "SAP.DE"),
    asset("WKN:865985", "Apple Inc.", "security", ["Apple", "AAPL"], "yahoo", "AAPL"),
]
WEIGHTS = {"BTC": 0.40, "KAS": 0.02, "SUI": 0.001}


@pytest.fixture(scope="module")
def m():
    return Matcher(ASSETS, WEIGHTS, SETTINGS)


def ids(matches):
    return sorted(x.asset_id for x in matches)


@pytest.mark.parametrize("title,expected", [
    ("Court calls the case sui generis", []),
    ("SUI and the art of saying no", []),                      # mehrdeutig, ohne Kontext
    ("SUI rallies 20% as token unlock looms", ["SUI"]),         # Kontextwort
    ("$SUI breaks out of range", ["SUI"]),                      # Cashtag
    ("Sui Network announces mainnet upgrade", ["SUI"]),         # eindeutiger Mehrwort-Alias
    ("Raspberry Pi 5 gets a new camera", []),
    ("PI coin listed on major exchange", ["PI#pi"]),
    ("Good night, markets: what to watch tomorrow", []),
    ("NIGHT token airdrop date announced", ["NIGHT#mn"]),
    ("Tap to pay arrives in more countries", []),
    ("TAP Protocol raises seed round", ["TAP#tp"]),
    ("Kaspa hits new all-time high", ["KAS"]),
    ("KAS miners report record hashrate", []),                  # KAS mehrdeutig, kein Kontextwort
    ("KAS staking yields rise", ["KAS"]),
    ("SAP beats estimates, cloud revenue up", ["WKN:716460"]),  # Aktienkürzel mit 3 Zeichen eindeutig
    ("Apple unveils new iPhone lineup", ["WKN:865985"]),
    ("Bitcoin and Kaspa lead crypto gains", ["BTC", "KAS"]),
    ("SAPPHIRE project launches", []),                          # Wortgrenzen
])
def test_title_matching(m, title, expected):
    assert ids(m.match(title)) == expected


def test_summary_only_and_implicit(m):
    r = m.match("Crypto market update", "Analysts say Kaspa could benefit from the upgrade.")
    assert [(x.asset_id, x.base, x.where) for x in r] == [("KAS", 0.55, "summary")]
    r = m.match("Weekly market wrap", "Nothing specific.", implicit=["WKN:716460"])
    assert [(x.asset_id, x.base, x.where) for x in r] == [("WKN:716460", 0.35, "implicit")]


def test_scores_prefer_title_source_weight_and_position(m):
    btc = m.match("Bitcoin rises")[0]
    kas = m.match("Kaspa rises")[0]
    s_btc = btc.score(0.8, m.weight_of("BTC"))
    s_kas = kas.score(0.8, m.weight_of("KAS"))
    assert s_btc > s_kas > 0
    assert btc.score(1.0, 1.0) > btc.score(0.4, 1.0)


def test_clickbait_filter():
    assert is_clickbait("This coin will 100x – LAST CHANCE!", ["100x", "last chance"]) == "100x"
    assert is_clickbait("Kaspa roadmap update", ["100x"]) is None
