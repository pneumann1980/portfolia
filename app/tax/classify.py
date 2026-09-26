"""Steuerliche Einstufung von Assets (Aktie, Fondsart …) und Konten (Steuerabzug im Inland ja/nein).

Reihenfolge: Einstellung in der App > Spalte aus dem Import (``tax_type`` bzw. ``tax_withholding``) >
Heuristik (nur Fonds/ETF-Erkennung, sichtbar markiert) > Standard.
"""

from __future__ import annotations

import re
from typing import Any

from app.ledger.models import AssetInfo, Portfolio

ASSET_TYPES: dict[str, str] = {
    "share": "Aktie",
    "etf_equity": "Aktienfonds/-ETF (mind. 51 % Aktien)",
    "etf_mixed": "Mischfonds (mind. 25 % Aktien)",
    "fund_realestate": "Immobilienfonds (mind. 51 % Immobilien)",
    "fund_realestate_foreign": "Auslands-Immobilienfonds (mind. 51 % ausl. Immobilien)",
    "etf_other": "Sonstiger Investmentfonds (z. B. Renten-/Geldmarkt-ETF)",
    "bond": "Anleihe / Kapitalforderung",
    "other": "Sonstiges Wertpapier (z. B. Zertifikat, ETN/ETC)",
}
FUND_TYPES = frozenset({"etf_equity", "etf_mixed", "fund_realestate", "fund_realestate_foreign", "etf_other"})
ACCOUNT_KINDS: dict[str, str] = {
    "domestic": "Inland – Bank führt Steuerabzug durch",
    "foreign": "Ausland – kein Steuerabzug (Angabe in der Steuererklärung)",
}

_FUND_RE = re.compile(r"\b(ETF|UCITS|Fonds|Fund|iShares|Xtrackers|Vanguard|Amundi|SPDR|Lyxor|Invesco|"
                      r"Indexfonds|Thesaurierend|Ausschüttend)\b", re.IGNORECASE)
_ETN_RE = re.compile(r"\b(ETN|ETC|Zertifikat|Certificate|Tracker)\b", re.IGNORECASE)


def classify_asset(a: AssetInfo, overrides: dict[str, Any] | None = None) -> tuple[str, str]:
    """(Typ, Quelle) mit Quelle ∈ settings | import | heuristic | default."""
    if a.is_crypto:
        return "crypto", "default"
    if a.is_fiat:
        return "fiat", "default"
    ov = (overrides or {}).get(a.asset_id)
    if ov in ASSET_TYPES:
        return ov, "settings"
    imp = str(a.extra.get("tax_type") or "").strip().lower()
    if imp in ASSET_TYPES:
        return imp, "import"
    text = " ".join([a.name or "", a.category or "", *a.aliases])
    if _ETN_RE.search(text):
        return "other", "heuristic"
    if _FUND_RE.search(text):
        return "etf_equity", "heuristic"
    return "share", "default"


def classify_account(pf: Portfolio, account: str, overrides: dict[str, Any] | None = None) -> tuple[str, str]:
    ov = (overrides or {}).get(account)
    if ov in ACCOUNT_KINDS:
        return ov, "settings"
    info = pf.accounts.get(account)
    if info is not None:
        imp = str(info.extra.get("tax_withholding") or info.extra.get("withholding") or "").strip().lower()
        if imp in ACCOUNT_KINDS:
            return imp, "import"
    return "domestic", "default"


def securities_accounts(pf: Portfolio) -> list[str]:
    """Konten, auf denen Wertpapiere gebucht wurden (nur diese brauchen eine Einstufung)."""
    accs: set[str] = set()
    for t in pf.txs:
        for acc, asset in ((t.from_account, t.from_asset), (t.to_account, t.to_asset)):
            if acc and asset and pf.asset(asset).is_security:
                accs.add(acc)
        if t.related_asset and pf.asset(t.related_asset).is_security:
            for acc in (t.from_account, t.to_account):
                if acc:
                    accs.add(acc)
    return sorted(accs)
