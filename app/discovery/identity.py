"""Unternehmensidentität: Ticker bzw. Name → eindeutiger, börsennotierter Emittent (SEC-CIK).

Regeln (konservativ – lieber „nicht aufgelöst“ als falsch zugeordnet):

* **Ticker** (Groß-/Kleinschreibung egal, „BRK.B“ = „BRK-B“): exakter Treffer im SEC-Verzeichnis.
* **Name**: normalisiert (Kleinbuchstaben, ohne Satzzeichen, ohne Rechtsform wie Inc/Corp/AG/plc). Gilt nur, wenn er
  genau **einem** Emittenten entspricht – exakt oder als Wortanfang des Firmennamens (mind. 4 Zeichen). Mehrere
  Treffer → mehrdeutig (Auswahl nötig), keiner → „nicht börsennotiert oder nicht bei der SEC registriert“.
* **Mehrere Listings** (z. B. GOOGL/GOOG): ein Emittent = ein Unternehmen; Hauptticker = erster Eintrag einer Börse
  (Nasdaq/NYSE vor OTC). Weitere Ticker werden als Alias gespeichert.
* **SPACs/Mantelgesellschaften** (SIC 6770 „Blank Checks“) werden erkannt und nicht als Kandidat geführt.
"""

from __future__ import annotations

import re
import threading
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from app.discovery.sec import Listing

_SUFFIX = re.compile(
    r"\b(incorporated|inc|corporation|corp|company|co|limited|ltd|plc|llc|lp|l\.p|sa|s\.a|ag|se|nv|n\.v|bv|ab|asa|"
    r"oyj|spa|s\.p\.a|kgaa|gmbh|holdings?|group|the|class [a-c]|cl [a-c]|adr|ads|/[a-z]{2})\b\.?")
EXCHANGE_RANK = {"Nasdaq": 0, "NYSE": 0, "CBOE": 1, "TXSE": 1, None: 2, "OTC": 3}
SPAC_SIC = {"6770"}
MIN_PREFIX = 4


def norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[,()'\"’]", " ", s)
    s = _SUFFIX.sub(" ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_ticker(s: str) -> str:
    return re.sub(r"[.\s/]", "-", (s or "").strip().upper())


@dataclass
class Issuer:
    cik: int
    name: str
    tickers: list[str] = field(default_factory=list)  # Hauptticker zuerst
    exchanges: list[str | None] = field(default_factory=list)

    @property
    def ticker(self) -> str:
        return self.tickers[0]

    @property
    def exchange(self) -> str | None:
        return self.exchanges[0] if self.exchanges else None

    @property
    def otc(self) -> bool:
        return self.exchange == "OTC"


@dataclass
class Resolution:
    status: str  # ok | ambiguous | not_found
    issuer: Issuer | None = None
    candidates: list[Issuer] = field(default_factory=list)
    method: str = ""  # ticker | name | prefix
    reason: str = ""


class Directory:
    """Index über das SEC-Verzeichnis (je Prozess einmal aufgebaut, bei neuer Liste ersetzt)."""

    def __init__(self, listings: list[Listing]) -> None:
        by_cik: dict[int, Issuer] = {}
        for li in listings:
            iss = by_cik.setdefault(li.cik, Issuer(li.cik, li.name))
            iss.tickers.append(li.ticker)
            iss.exchanges.append(li.exchange)
        for iss in by_cik.values():  # Börse vor OTC; innerhalb gleicher Stufe Reihenfolge der SEC beibehalten
            order = sorted(range(len(iss.tickers)), key=lambda i: (EXCHANGE_RANK.get(iss.exchanges[i], 2), i))
            iss.tickers = [iss.tickers[i] for i in order]
            iss.exchanges = [iss.exchanges[i] for i in order]
        self.by_cik = by_cik
        self.by_ticker = {norm_ticker(t): iss for iss in by_cik.values() for t in iss.tickers}
        self.by_name: dict[str, list[Issuer]] = {}
        for iss in by_cik.values():
            self.by_name.setdefault(norm_name(iss.name), []).append(iss)

    def resolve(self, *, ticker: str = "", name: str = "") -> Resolution:
        if ticker:
            iss = self.by_ticker.get(norm_ticker(ticker))
            if iss is not None:
                if name and norm_name(name) and not _compatible(name, iss.name):
                    return Resolution("ambiguous", None, [iss], "ticker",
                                      f"Ticker {ticker.upper()} gehört zu „{iss.name}“, nicht zu „{name}“")
                return Resolution("ok", iss, method="ticker")
            if not name:
                return Resolution("not_found", reason=f"Ticker {ticker.upper()} ist bei der SEC nicht registriert")
        n = norm_name(name)
        if not n:
            return Resolution("not_found", reason="kein Name angegeben")
        exact = self.by_name.get(n, [])
        if len(exact) == 1:
            return Resolution("ok", exact[0], method="name")
        if len(exact) > 1:
            return Resolution("ambiguous", None, exact, "name", f"„{name}“ passt zu {len(exact)} Emittenten")
        if len(n) >= MIN_PREFIX:
            pre = [i for k, lst in self.by_name.items() if k.startswith(n + " ") for i in lst]
            if len(pre) == 1:
                return Resolution("ok", pre[0], method="prefix")
            if len(pre) > 1:
                return Resolution("ambiguous", None, pre[:20], "prefix", f"„{name}“ passt zu {len(pre)} Emittenten")
        return Resolution("not_found", reason=f"„{name}“: kein börsennotierter Emittent im SEC-Verzeichnis (privat, "
                                              "nicht US-registriert oder anders benannt)")


def _compatible(given: str, official: str) -> bool:
    a, b = norm_name(given), norm_name(official)
    return bool(a and b) and (a == b or b.startswith(a + " ") or a.startswith(b + " ") or a.split()[0] == b.split()[0])


_CACHE: dict[str, Any] = {}
_LOCK = threading.Lock()


def directory(sec: Any) -> Directory:
    """Verzeichnis aus dem SEC-Client (Plattencache 7 Tage); im Prozess wiederverwendet, solange die Datei gleich."""
    key = sec.retrieved_at("tickers", "company_tickers_exchange")
    with _LOCK:
        if _CACHE.get("key") == key and key is not None and "dir" in _CACHE:
            return _CACHE["dir"]
    d = Directory(sec.listings())
    with _LOCK:
        _CACHE["key"], _CACHE["dir"] = sec.retrieved_at("tickers", "company_tickers_exchange"), d
    return d


def is_spac(submissions: dict[str, Any]) -> bool:
    return str(submissions.get("sic") or "") in SPAC_SIC
