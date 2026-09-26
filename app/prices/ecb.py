"""EZB-Referenzkurse als Devisen-Fallback: Frankfurter-API, bei Ausfall die EZB-Historiendatei direkt."""

from __future__ import annotations

import csv
import io
import logging
import zipfile
from datetime import date

import httpx

from app.prices.models import Bar
from app.util.http import HttpError, request_with_retry

log = logging.getLogger(__name__)


class EcbProvider:
    name = "ecb"

    def __init__(self, client: httpx.Client, base_url: str, fallback_url: str | None, hist_zip_url: str) -> None:
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.fallback_url = fallback_url.rstrip("/") if fallback_url else None
        self.hist_zip_url = hist_zip_url

    def _frankfurter(self, base: str, path: str, ccys: list[str]) -> dict:
        syms = ",".join(sorted(set(ccys)))
        resp = request_with_retry(self.client, "GET", f"{base}/{path}",
                                  params={"base": "EUR", "symbols": syms}, retries=2)
        return resp.json()

    def history(self, ccys: list[str], start: date, end: date) -> dict[str, list[Bar]]:
        ccys = [c.upper() for c in ccys if c.upper() != "EUR"]
        if not ccys:
            return {}
        errors = []
        for base in [self.base_url, self.fallback_url]:
            if not base:
                continue
            try:
                data = self._frankfurter(base, f"{start.isoformat()}..{end.isoformat()}", ccys)
                out: dict[str, list[Bar]] = {c: [] for c in ccys}
                for d, rates in sorted((data.get("rates") or {}).items()):
                    for c in ccys:
                        if rates.get(c):
                            out[c].append(Bar(date=date.fromisoformat(d), close=float(rates[c])))
                return out
            except (HttpError, httpx.HTTPError, ValueError) as e:
                errors.append(f"{base}: {e}")
        # Fallback: komplette Historie direkt von der EZB (ZIP mit CSV)
        try:
            return self._hist_zip(ccys, start, end)
        except (HttpError, httpx.HTTPError, ValueError, zipfile.BadZipFile, KeyError) as e:
            errors.append(f"ECB-ZIP: {e}")
        raise HttpError("; ".join(errors))

    def _hist_zip(self, ccys: list[str], start: date, end: date) -> dict[str, list[Bar]]:
        resp = request_with_retry(self.client, "GET", self.hist_zip_url, retries=2)
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
            text = zf.read(name).decode("utf-8")
        out: dict[str, list[Bar]] = {c: [] for c in ccys}
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            ds = (row.get("Date") or "").strip()
            if not ds:
                continue
            d = date.fromisoformat(ds)
            if d < start or d > end:
                continue
            for c in ccys:
                v = (row.get(c) or "").strip()
                if v and v != "N/A":
                    out[c].append(Bar(date=d, close=float(v)))
        for c in out:
            out[c].sort(key=lambda b: b.date)
        return out

    def latest(self, ccys: list[str]) -> dict[str, tuple[float, date]]:
        ccys = [c.upper() for c in ccys if c.upper() != "EUR"]
        if not ccys:
            return {}
        last_err: Exception | None = None
        for base in [self.base_url, self.fallback_url]:
            if not base:
                continue
            try:
                data = self._frankfurter(base, "latest", ccys)
                d = date.fromisoformat(data["date"])
                return {c: (float(v), d) for c, v in (data.get("rates") or {}).items()}
            except (HttpError, httpx.HTTPError, ValueError, KeyError) as e:
                last_err = e
        raise HttpError(str(last_err))
