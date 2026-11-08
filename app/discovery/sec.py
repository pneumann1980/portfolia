"""SEC EDGAR (https://www.sec.gov/edgar/sec-api-documentation): Ticker-Verzeichnis, ``submissions``, ``companyfacts``
und der Abschnitt „Item 1. Business“ des jüngsten Geschäftsberichts.

Nutzungsregeln der SEC (Fair Access): höchstens 10 Anfragen/s, aussagekräftiger ``User-Agent`` mit Kontakt. Portfolia
fragt höchstens **eine** Anfrage je ``MIN_INTERVAL_S`` an, speichert Antworten auf der Platte (``CACHE_TTL``) und
überträgt nichts außer der öffentlichen Kennung (CIK/Pfad). Abgefragt werden nur feste Hosts der SEC – keine
nutzergesteuerten Adressen (kein SSRF-Pfad). Kostenlos, ohne Schlüssel; Daten sind gemeinfrei (US-Bundesbehörde).
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import __version__
from app.util.http import HttpError, RateLimiter, request_with_retry

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/{doc}"
ALLOWED_HOSTS = ("www.sec.gov", "data.sec.gov")
MIN_INTERVAL_S = 0.5
CACHE_TTL = {"tickers": 7 * 86400, "submissions": 86400, "facts": 86400, "doc": 30 * 86400}
MAX_DOC_BYTES = 12_000_000
DEFAULT_UA = f"Portfolia/{__version__} self-hosted research (contact not configured)"

_LIMITER = RateLimiter(MIN_INTERVAL_S)
_LOCK = threading.Lock()


class SecError(Exception):
    pass


@dataclass
class Listing:
    cik: int
    ticker: str
    name: str
    exchange: str | None


def user_agent(settings: Any) -> str:
    ua = str(settings.get("discovery.sec_user_agent", "") or "").strip()
    return ua[:200] if ua else DEFAULT_UA


class SecClient:
    def __init__(self, ctx: Any, cache_dir: Path | None = None) -> None:
        self.ctx = ctx
        self.http = ctx.http
        self.cache = cache_dir or (ctx.config.data_dir / "cache" / "sec")

    # -- Transport ------------------------------------------------------------------------------------------------
    def _get(self, url: str, kind: str, key: str, *, binary: bool = False) -> Any:
        path = self.cache / kind / (re.sub(r"[^A-Za-z0-9._-]", "_", key) + (".bin" if binary else ".json.gz"))
        ttl = CACHE_TTL[kind]
        if path.exists() and time.time() - path.stat().st_mtime < ttl:
            return self._read(path, binary)
        host = url.split("/")[2]
        if host not in ALLOWED_HOSTS:
            raise SecError(f"Host nicht erlaubt: {host}")
        if self.ctx.config.demo_mode and not self.ctx.settings.get("discovery.sec_in_demo", False):
            if path.exists():
                return self._read(path, binary)
            raise SecError("Demo-Modus: SEC-Abruf aus (Einstellung discovery.sec_in_demo)")
        try:
            with _LOCK:
                r = request_with_retry(self.http, "GET", url, retries=2, limiter=_LIMITER,
                                       headers={"User-Agent": user_agent(self.ctx.settings),
                                                "Accept-Encoding": "gzip, deflate"}, timeout=30.0)
        except HttpError as e:
            if path.exists():  # veraltete Kopie ist besser als keine – Alter steht im Bericht
                log.info("SEC %s nicht erreichbar (%s) – verwende gespeicherte Kopie", kind, e)
                return self._read(path, binary)
            raise SecError(f"SEC nicht erreichbar: {e}") from e
        body = r.content
        if len(body) > MAX_DOC_BYTES:
            raise SecError("SEC-Antwort zu groß")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(body if binary else gzip.compress(body))
        tmp.replace(path)
        return body if binary else json.loads(body)

    @staticmethod
    def _read(path: Path, binary: bool) -> Any:
        raw = path.read_bytes()
        return raw if binary else json.loads(gzip.decompress(raw))

    def retrieved_at(self, kind: str, key: str) -> float | None:
        p = self.cache / kind / (re.sub(r"[^A-Za-z0-9._-]", "_", key) + ".json.gz")
        return p.stat().st_mtime if p.exists() else None

    # -- Verzeichnis ----------------------------------------------------------------------------------------------
    def listings(self) -> list[Listing]:
        """Alle bei der SEC registrierten börsennotierten Emittenten mit Ticker und Börse."""
        data = self._get(TICKERS_URL, "tickers", "company_tickers_exchange")
        fields = data.get("fields") or []
        idx = {f: i for i, f in enumerate(fields)}
        out = []
        for row in data.get("data") or []:
            try:
                out.append(Listing(int(row[idx["cik"]]), str(row[idx["ticker"]]).upper(), str(row[idx["name"]]),
                                   row[idx["exchange"]] or None))
            except (KeyError, IndexError, TypeError, ValueError):
                continue
        return out

    def submissions(self, cik: int) -> dict[str, Any]:
        return self._get(SUBMISSIONS_URL.format(cik=cik), "submissions", f"CIK{cik:010d}")

    def facts(self, cik: int) -> dict[str, Any]:
        return self._get(FACTS_URL.format(cik=cik), "facts", f"CIK{cik:010d}")

    # -- Geschäftsbericht -----------------------------------------------------------------------------------------
    def latest_annual(self, cik: int) -> dict[str, Any] | None:
        """Jüngster Jahresbericht (10-K oder 20-F; Änderungen /A nur ohne Original) aus ``submissions``."""
        rec = (self.submissions(cik).get("filings") or {}).get("recent") or {}
        forms = rec.get("form") or []
        for i, f in enumerate(forms):
            if f in ("10-K", "20-F", "40-F"):
                accn = rec["accessionNumber"][i]
                return {"form": f, "accn": accn, "filed": rec["filingDate"][i],
                        "period": (rec.get("reportDate") or [""] * len(forms))[i],
                        "doc": rec["primaryDocument"][i],
                        "url": ARCHIVE_URL.format(cik=cik, accn=accn.replace("-", ""), doc=rec["primaryDocument"][i])}
        return None

    def business_section(self, cik: int, limit: int = 14000) -> tuple[str, dict[str, Any]] | None:
        """Text von „Item 1. Business“ des jüngsten Jahresberichts (gekürzt) samt Fundstelle."""
        filing = self.latest_annual(cik)
        if filing is None:
            return None
        raw = self._get(filing["url"], "doc", f"{cik}_{filing['accn']}", binary=True)
        text = business_text(html_to_text(raw))
        if not text:
            return None
        return text[:limit], filing


def html_to_text(raw: bytes) -> str:
    from lxml import html as lh

    try:
        doc = lh.fromstring(raw)
    except Exception:
        return ""
    for el in doc.xpath("//script|//style|//ix:header", namespaces={"ix": "http://www.xbrl.org/2013/inlineXBRL"}):
        el.getparent().remove(el)
    for el in doc.iter("p", "div", "tr", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "table"):
        el.tail = "\n" + (el.tail or "")  # Blockelemente als Zeilen (für „Item 1. Business“ am Zeilenanfang)
    text = doc.text_content()
    text = re.sub(r"[   ]", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


_ITEM1 = re.compile(r"(?im)^\s*item\s*1\.?\s*[\-–—:]?\s*business\b")
_ITEM1A = re.compile(r"(?im)^\s*item\s*1a\.?\s*[\-–—:]?\s*risk\s+factors\b")


def business_text(text: str) -> str:
    """„Item 1. Business“ bis „Item 1A. Risk Factors“ – das Inhaltsverzeichnis (kurzer Treffer) wird übersprungen."""
    starts = [m.end() for m in _ITEM1.finditer(text)]
    for s in starts:
        end = _ITEM1A.search(text, s)
        chunk = text[s:end.start()] if end else text[s:s + 40000]
        if len(chunk) > 1500:
            return chunk.strip()
    return ""
