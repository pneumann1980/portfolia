"""Zuordnung von Meldungen zu gehaltenen Assets und Relevanz-Score.

Regeln:
* Begriffe je Asset: aliases (assets.csv), Name, Symbol/Ticker – Treffer nur an Wortgrenzen.
* Kurze Großbuchstaben-Kürzel (≤ 5 Zeichen) werden case-sensitiv gesucht („SUI“ ≠ „sui generis“).
* Mehrdeutige Begriffe (Konfiguration ``ambiguous_terms`` sowie alle Kürzel ≤ 3 Zeichen) zählen nur,
  wenn im Text ein Kontextwort vorkommt (Krypto: crypto/coin/token …, Aktien: Aktie/stock/shares …),
  ein eindeutiger Begriff desselben Assets trifft oder das Kürzel als Cashtag ($SUI) auftritt.
* Ausschlussmuster (``exclude_patterns``) entfernen Textstellen vor der Suche („sui generis“).

Score je (Meldung, Asset) = Basis × Quellgewicht × Positionsfaktor
  Basis: Treffer im Titel 1,0 · nur in Beschreibung 0,55 · implizit (Feed je Asset) 0,5
  Positionsfaktor: 0,6 + 0,4 · min(1, √(Gewicht/10 %))
Bei der Anzeige wird zusätzlich die Aktualität berücksichtigt (Halbwertszeit 48 h).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.ledger.models import AssetInfo

SECURITY_CONTEXT = ["aktie", "aktien", "stock", "stocks", "shares", "share", "konzern", "unternehmen", "earnings",
                    "quartal", "quarter", "umsatz", "revenue", "dividende", "dividend", "analyst", "kursziel", "nyse",
                    "nasdaq", "xetra", "börse", "ceo"]

# Häufige Wörter, die als Asset-Kürzel mehrdeutig sind (zusätzlich zur Konfiguration)
COMMON_WORDS = {"ONE", "NEAR", "GAS", "SAND", "FLOW", "LINK", "DOT", "MASK", "NIGHT", "ROSE", "HOT", "TAP", "PI",
                "OP", "SUI", "GALA", "CAKE", "ATOM", "BAND", "KEY", "SUN", "MOON", "STORY", "ACE", "AI", "APE",
                "BEAM", "CHZ", "DEGEN", "ID", "IO", "JUP", "LOOM", "MAGIC", "MANTA", "NOT", "ORDI", "PEOPLE",
                "RARE", "SAFE", "SPELL", "STEP", "TIME", "TRUMP", "WAVES", "WIN", "ZERO", "TON", "SEI", "APT",
                "ARB", "BOME", "WIF", "BONK", "POL", "S", "USUAL", "ME", "MOVE", "HYPE", "VIRTUAL", "PENGU"}


@dataclass
class Term:
    text: str
    regex: re.Pattern[str]
    ambiguous: bool
    cashtag: re.Pattern[str] | None = None


@dataclass
class AssetTerms:
    asset: AssetInfo
    terms: list[Term]
    weight: float  # Portfolio-Gewicht 0..1
    excludes: list[re.Pattern[str]] = field(default_factory=list)

    @property
    def position_factor(self) -> float:
        return 0.6 + 0.4 * min(1.0, math.sqrt(max(self.weight, 0.0) / 0.10))


@dataclass
class Match:
    asset_id: str
    base: float
    where: str  # title | summary | implicit
    terms: list[str]

    def score(self, source_weight: float, position_factor: float) -> float:
        return round(self.base * source_weight * position_factor, 4)


def _is_short_upper(t: str) -> bool:
    return t.isupper() and len(t.replace(".", "")) <= 5 and not any(c.isspace() for c in t)


def _term_regex(t: str) -> re.Pattern[str]:
    esc = re.escape(t)
    flags = 0 if _is_short_upper(t) else re.IGNORECASE
    return re.compile(rf"(?<![\w$@#/.-]){esc}(?![\w-]|\.\w)", flags)


# Kurze Krypto-Kürzel, die praktisch eindeutig sind (sonst gelten Kürzel ≤ 3 Zeichen als mehrdeutig)
UNAMBIGUOUS_TICKERS = {"BTC", "ETH", "XRP", "BNB", "LTC", "XMR", "USDT", "USDC", "BCH", "XLM", "TRX", "DOGE",
                       "SHIB", "AVAX", "HBAR", "NVDA", "PLTR", "AAPL", "MSFT", "TSLA", "AMZN"}


class Matcher:
    def __init__(self, assets: list[AssetInfo], weights: dict[str, float], settings: dict[str, Any]) -> None:
        amb_cfg = {str(x).upper() for x in (settings.get("ambiguous_terms") or [])}
        self.crypto_ctx = [str(w).lower() for w in (settings.get("context_words") or [])]
        self.sec_ctx = SECURITY_CONTEXT
        excl = {str(k).upper(): [re.compile(p, re.IGNORECASE) for p in (v or [])]
                for k, v in (settings.get("exclude_patterns") or {}).items()}
        self.entries: list[AssetTerms] = []
        for a in assets:
            if a.is_fiat:
                continue
            raw = set()
            for t in [*a.aliases, a.name, a.symbol]:
                t = (t or "").strip()
                if len(t) >= 2 or (a.is_security and t.isupper() and len(t) >= 1):
                    raw.add(t)
            if a.quote_source == "yahoo" and a.quote_id:
                raw.add(a.quote_id.split(".")[0].upper())
            terms = [self._term(t, a, amb_cfg) for t in sorted(raw, key=len, reverse=True)]
            excludes = []
            for t in raw:
                excludes += excl.get(t.upper(), [])
            excludes += excl.get(a.asset_id.upper(), [])
            self.entries.append(AssetTerms(a, terms, float(weights.get(a.asset_id, 0.0)), excludes))

    @staticmethod
    def _term(t: str, a: AssetInfo, amb_cfg: set[str]) -> Term:
        up = t.upper()
        multiword = " " in t.strip()
        if multiword:
            ambiguous = False
        elif a.is_crypto:
            ambiguous = (up in amb_cfg or up in COMMON_WORDS
                         or (len(t) <= 3 and up not in UNAMBIGUOUS_TICKERS))
        else:
            ambiguous = up in amb_cfg or up in COMMON_WORDS or len(t) <= 2
        cashtag = (re.compile(rf"\$(?:{re.escape(up)})(?![\w])", re.IGNORECASE)
                   if not multiword and len(t) <= 6 else None)
        return Term(t, _term_regex(t), ambiguous, cashtag)

    def _context(self, text_lower: str, asset: AssetInfo) -> bool:
        words = self.crypto_ctx if asset.is_crypto else self.sec_ctx
        return any(re.search(rf"(?<![\w]){re.escape(w)}", text_lower) for w in words)

    @staticmethod
    def _found(t: Term, text: str) -> bool:
        return bool(t.regex.search(text) or (t.cashtag and t.cashtag.search(text)))

    def match(self, title: str, summary: str = "", implicit: list[str] | None = None) -> list[Match]:
        title = title or ""
        summary = summary or ""
        implicit = implicit or []
        full_lower = f"{title}\n{summary}".lower()
        out: list[Match] = []
        for e in self.entries:
            aid = e.asset.asset_id
            t_title, t_sum = title, summary
            for ex in e.excludes:
                t_title = ex.sub(" ", t_title)
                t_sum = ex.sub(" ", t_sum)
            in_title = {t.text for t in e.terms if self._found(t, t_title)}
            in_sum = {t.text for t in e.terms if self._found(t, t_sum)}
            hits = [t for t in e.terms if t.text in in_title or t.text in in_sum]
            valid = False
            if hits:
                both = f"{t_title}\n{t_sum}"
                strong = [t for t in hits if not t.ambiguous or (t.cashtag and t.cashtag.search(both))]
                valid = bool(strong) or self._context(full_lower, e.asset)
            if valid:
                base = 1.0 if in_title else 0.55
                out.append(Match(aid, base, "title" if in_title else "summary", sorted(in_title | in_sum)))
            elif aid in implicit:
                out.append(Match(aid, 0.35, "implicit", []))
        return out

    def weight_of(self, asset_id: str) -> float:
        for e in self.entries:
            if e.asset.asset_id == asset_id:
                return e.position_factor
        return 0.6


def recency_factor(published: datetime, now: datetime | None = None, half_life_h: float = 48.0) -> float:
    now = now or datetime.now(UTC)
    age_h = max(0.0, (now - published).total_seconds() / 3600)
    return 0.5 ** (age_h / half_life_h)


def is_clickbait(title: str, blocklist: list[str]) -> str | None:
    t = (title or "").lower()
    for pat in blocklist:
        p = str(pat).lower().strip()
        if p and p in t:
            return p
    return None
