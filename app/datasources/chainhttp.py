"""Abrufe bei Chain-Anbietern (Explorer/Indexer) – nur geprüfte, fest hinterlegte Endpunkte, nur lesend.

Sicherheit (kein SSRF)
    Es gibt keine frei eingebbaren URLs. Jeder Anbieter ist hier mit festem HTTPS-Host und Basis-Pfad hinterlegt;
    Pfadteile stammen nur aus formatgeprüften Werten (Adressen, Hashes, Signaturen). Vor dem Senden wird jede Anfrage
    gegen Schema, Host und Basis-Pfad des Anbieters geprüft; Weiterleitungen werden nicht verfolgt. API-Keys gehen
    nur an den Anbieter, für den sie hinterlegt sind (Header bzw. der dort dokumentierte Parameter), und erscheinen
    nie in Logs oder Meldungen (URLs werden nie protokolliert).

Genauigkeit
    JSON wird mit ``Decimal`` statt ``float`` gelesen – Beträge werden nie gerundet.

Robustheit
    Mindestabstand zwischen Anfragen je Anbieter (thread-sicher, auch bei begrenzt parallelen Abrufen), Wiederholung
    bei 429/5xx/Zeitüberschreitung mit exponentiellem Backoff und ``Retry-After`` (gedeckelt; längere Sperren
    übernimmt der Zeitplan), Anfrage-, Warte- und Zeitbudget je Lauf. Ist ein Budget erschöpft, endet der Lauf
    geordnet (:class:`Stop`) – der Connector liefert dann alles lückenlos Abgerufene mit Fortsetzungspunkt.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar
from urllib.parse import urlsplit

import httpx

from app import __version__
from app.datasources import connector as K

T = TypeVar("T")
R = TypeVar("R")

TIMEOUT = httpx.Timeout(25.0, connect=10.0)
RETRIES = 4  # weitere Versuche nach dem ersten
MAX_RETRY_AFTER_S = 60.0  # längere Sperren übernimmt der Zeitplan (retry_after_s)
MAX_BODY = 24 * 1024 * 1024
_PATH_RE = re.compile(r"^(/[A-Za-z0-9_.:\-]+)*/?$")


@dataclass(frozen=True)
class Endpoint:
    """Ein geprüfter Anbieter-Endpunkt (fester Host und Basis-Pfad)."""

    id: str
    label: str
    base: str  # https://host/pfad – bei Routescan mit Platzhalter {chain}
    rps: float  # höchstens so viele Anfragen je Sekunde (konservativ unter dem dokumentierten Limit)
    auth: str = "none"  # none | query:<name> | header:<name>
    key_provider: str | None = None  # Schlüssel in „Anbieter-Schlüssel“ (provider_secret)
    key_required: bool = False
    rps_with_key: float | None = None
    docs: str = ""
    terms: str = ""  # Kosten/Limits laut Anbieter (Stand der Recherche)

    @property
    def host(self) -> str:
        return urlsplit(self.base).hostname or ""

    def base_for(self, chain_id: int | None = None) -> str:
        if "{chain}" in self.base:
            if chain_id is None or not 0 < int(chain_id) < 10**9:
                raise K.ConnectorError("config", "Chain-ID fehlt für diesen Anbieter.")
            return self.base.replace("{chain}", str(int(chain_id)))
        return self.base


ENDPOINTS: dict[str, Endpoint] = {e.id: e for e in (
    Endpoint("etherscan", "Etherscan API V2", "https://api.etherscan.io/v2/api", rps=3.0, auth="query:apikey",
             key_provider="etherscan", key_required=True, docs="https://docs.etherscan.io",
             terms="kostenloser API-Key: bis 3–5 Anfragen/s, 100.000/Tag, höchstens 1.000 Einträge je Anfrage; "
                   "BNB Chain und Avalanche nur mit kostenpflichtigem Plan (ab ca. 49 USD/Monat)"),
    Endpoint("routescan", "Routescan (Etherscan-kompatibel)",
             "https://api.routescan.io/v2/network/mainnet/evm/{chain}/etherscan/api", rps=1.5, auth="query:apikey",
             key_provider="routescan", rps_with_key=4.0, docs="https://routescan.io/documentation",
             terms="ohne Key: 2 Anfragen/s, 10.000/Tag; kostenloser Key: 5/s, 100.000/Tag"),
    Endpoint("mempool", "mempool.space (Esplora-API)", "https://mempool.space/api", rps=1.0,
             docs="https://mempool.space/docs/api/rest",
             terms="ohne Key; öffentliches Limit nicht beziffert – Portfolia fragt höchstens 1×/s"),
    Endpoint("blockstream", "Blockstream Esplora", "https://blockstream.info/api", rps=1.0,
             docs="https://github.com/Blockstream/esplora/blob/master/API.md",
             terms="ohne Key; öffentliches Limit nicht beziffert – Portfolia fragt höchstens 1×/s"),
    Endpoint("solana", "Solana öffentlicher RPC (Solana Foundation)", "https://api.mainnet-beta.solana.com",
             rps=3.0, docs="https://solana.com/docs/references/clusters",
             terms="ohne Key; 100 Anfragen/10 s je IP, 40/10 s je Methode; laut Betreiber nicht für Dauerbetrieb "
                   "gedacht"),
    Endpoint("helius", "Helius RPC", "https://mainnet.helius-rpc.com", rps=8.0, auth="query:api-key",
             key_provider="helius", key_required=True, docs="https://www.helius.dev/docs",
             terms="kostenloser Plan mit API-Key: 10 RPC-Anfragen/s"),
    Endpoint("kaspa", "Kaspa REST-API (api.kaspa.org)", "https://api.kaspa.org", rps=2.0,
             docs="https://api.kaspa.org/docs", terms="ohne Key; Limit nicht beziffert – höchstens 2×/s"),
    Endpoint("kasplex", "Kasplex KRC-20-Indexer", "https://api.kasplex.org/v1", rps=2.0,
             docs="https://docs-kasplex.gitbook.io/krc20",
             terms="ohne Key; Verbindungslimit des Betreibers nicht beziffert – höchstens 2×/s"),
)}


class Stop(Exception):
    """Budget eines Laufs erschöpft – geordnet beenden und mit Fortsetzungspunkt weitermachen."""


class _Limiter:
    """Mindestabstand zwischen Anfragen (thread-sicher, gemeinsam für alle Läufe desselben Anbieters)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self, interval: float, sleep: Callable[[float], None], clock: Callable[[], float]) -> float:
        with self._lock:
            now = clock()
            delay = self._next - now
            if delay > 0:
                sleep(delay)
                now += delay
            self._next = now + interval
            return max(delay, 0.0)


_LIMITERS: dict[str, _Limiter] = {}
_LIMITERS_LOCK = threading.Lock()


def _limiter(key: str) -> _Limiter:
    with _LIMITERS_LOCK:
        if key not in _LIMITERS:
            _LIMITERS[key] = _Limiter()
        return _LIMITERS[key]


def loads(text: str | bytes) -> Any:
    """JSON mit exakten Dezimalzahlen (nie ``float``)."""
    return json.loads(text, parse_float=Decimal)


def _retry_after(resp: httpx.Response) -> float | None:
    v = resp.headers.get("retry-after")
    if not v:
        return None
    try:
        return max(0.0, float(v))
    except ValueError:
        try:
            return max(0.0, (parsedate_to_datetime(v) - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError):
            return None


class ChainHttp:
    """HTTP-Zugriff auf genau einen Anbieter-Endpunkt (GET/JSON und JSON-RPC)."""

    def __init__(self, ep: Endpoint, *, key: str | None = None, chain_id: int | None = None,
                 transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, max_requests: int = 3000,
                 wait_budget_s: float = 240.0, deadline_s: float | None = None,
                 usage: Callable[[int], None] | None = None) -> None:
        if ep.key_required and not key:
            raise K.ConnectorError("config", f"{ep.label} verlangt einen Schlüssel des Anbieters – unter "
                                             "„Anbieter-Schlüssel“ hinterlegen.")
        self.ep = ep
        self.base = ep.base_for(chain_id)
        parts = urlsplit(self.base)
        if parts.scheme != "https" or not parts.hostname:
            raise K.ConnectorError("config", "Anbieter-Endpunkt ungültig.")  # pragma: no cover - fester Katalog
        self._host = parts.hostname
        self._base_path = parts.path.rstrip("/")
        self._key = key or None
        self.sleep = sleep
        self.clock = clock
        self.max_requests = max_requests
        self.wait_budget = wait_budget_s
        self.deadline = clock() + deadline_s if deadline_s else None
        self.interval = 1.0 / ((ep.rps_with_key or ep.rps) if key else ep.rps)
        self._limiter = _limiter(f"{ep.id}:{'key' if key else 'anon'}")
        self._usage = usage
        self._lock = threading.Lock()
        self.requests = 0
        self.throttled = 0
        self.retries = 0
        self.waited = 0.0  # Wartezeit durch Drosselung/Backoff (Budget)
        self.paced = 0.0  # Wartezeit durch den Mindestabstand (nur Anzeige)
        self._client = httpx.Client(timeout=TIMEOUT, follow_redirects=False, transport=transport,
                                    headers={"Accept": "application/json",
                                             "User-Agent": f"Portfolia/{__version__} (read-only)"})

    def __repr__(self) -> str:  # nie den Schlüssel ausgeben
        return f"ChainHttp({self.ep.id}, requests={self.requests})"

    def __enter__(self) -> ChainHttp:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @property
    def secrets(self) -> list[str]:
        return [self._key] if self._key else []

    def stats(self) -> dict[str, Any]:
        return {"requests": self.requests, "throttled": self.throttled, "retries": self.retries,
                "waited_s": round(self.waited + self.paced, 1)}

    # -- Budgets ------------------------------------------------------------------------------------------
    def _count(self) -> None:
        with self._lock:
            if self.requests >= self.max_requests:
                raise Stop(f"Anfragebudget des Laufs erreicht ({self.max_requests})")
            if self.deadline is not None and self.clock() >= self.deadline:
                raise Stop("Zeitbudget des Laufs erreicht")
            self.requests += 1
        if self._usage is not None:
            self._usage(1)

    def _pause(self, seconds: float) -> bool:
        with self._lock:
            if self.waited + seconds > self.wait_budget:
                return False
            if self.deadline is not None and self.clock() + seconds >= self.deadline:
                return False
            self.waited += seconds
        self.sleep(seconds)
        return True

    # -- Anfragen -----------------------------------------------------------------------------------------
    def _request(self, method: str, path: str, params: Mapping[str, Any] | None, body: Any) -> httpx.Request:
        if not _PATH_RE.match(path or "") or ".." in path:
            raise K.ConnectorError("data", "Interner Fehler: unzulässiger Pfad für den Anbieter.")
        q = {k: str(v) for k, v in (params or {}).items() if v is not None}
        headers: dict[str, str] = {}
        if self._key and self.ep.auth.startswith("query:"):
            q[self.ep.auth.split(":", 1)[1]] = self._key
        elif self._key and self.ep.auth.startswith("header:"):
            headers[self.ep.auth.split(":", 1)[1]] = self._key
        url = self.base + (path if path not in ("", "/") else "")
        if body is not None:
            headers["Content-Type"] = "application/json"
            req = self._client.build_request(method, url, params=q, headers=headers,
                                             content=json.dumps(body, default=str).encode())
        else:
            req = self._client.build_request(method, url, params=q, headers=headers)
        u = req.url
        if u.scheme != "https" or u.host != self._host or not str(u.path).startswith(self._base_path):
            raise K.ConnectorError("data", "Anfrage an einen nicht hinterlegten Endpunkt verweigert.")
        return req

    def _send(self, method: str, path: str, params: Mapping[str, Any] | None, body: Any, what: str) -> Any:
        attempt = 0
        while True:
            attempt += 1
            self._count()
            req = self._request(method, path, params, body)
            self.paced += self._limiter.wait(self.interval, self.sleep, self.clock)
            try:
                resp = self._client.send(req)
            except httpx.TimeoutException:
                if attempt > RETRIES or not self._pause(min(30.0, 2.0 ** (attempt - 1))):
                    raise K.ConnectorError("unavailable", f"{self.ep.label}: Zeitüberschreitung bei {what} – der "
                                                          "nächste Lauf versucht es erneut.") from None
                self.retries += 1
                continue
            except httpx.TransportError:
                if attempt > RETRIES or not self._pause(min(30.0, 2.0 ** (attempt - 1))):
                    raise K.ConnectorError("unavailable", f"{self.ep.label} nicht erreichbar (Netzwerk/DNS) – der "
                                                          "nächste Lauf versucht es erneut.") from None
                self.retries += 1
                continue
            code = resp.status_code
            if code == 429:
                self.throttled += 1
                ra = _retry_after(resp)
                wait = ra if ra is not None else min(30.0, 2.0 ** attempt)
                if wait > MAX_RETRY_AFTER_S or attempt > RETRIES or not self._pause(wait):
                    raise K.ConnectorError("rate_limit", f"{self.ep.label} drosselt Anfragen (HTTP 429).",
                                           retry_after_s=int(max(wait, 60)))
                self.retries += 1
                continue
            if code >= 500:
                if attempt <= RETRIES and self._pause(min(30.0, 2.0 ** (attempt - 1))):
                    self.retries += 1
                    continue
                raise K.ConnectorError("unavailable", f"{self.ep.label} ist vorübergehend gestört (HTTP {code}).")
            if 300 <= code < 400:
                raise K.ConnectorError("data", f"{self.ep.label} leitet {what} um (HTTP {code}) – aus "
                                               "Sicherheitsgründen nicht gefolgt.")
            if code in (401, 403):
                raise K.ConnectorError("auth", f"{self.ep.label} verweigert {what} (HTTP {code}) – Schlüssel unter "
                                               "„Anbieter-Schlüssel“ prüfen.")
            raw = resp.content
            if len(raw) > MAX_BODY:
                raise K.ConnectorError("data", f"Antwort von {self.ep.label} zu groß ({what}).")
            if code >= 400:
                return _HttpProblem(code, _excerpt(raw))
            try:
                return loads(raw)
            except ValueError:
                raise K.ConnectorError("data", f"Antwort von {self.ep.label} ist kein gültiges JSON ({what}).") \
                    from None

    def get(self, path: str = "", params: Mapping[str, Any] | None = None, *, what: str,
            allow_status: Iterable[int] = ()) -> Any:
        """GET → JSON. 4xx-Antworten sind Fehler, außer ``allow_status`` (dann :class:`_HttpProblem`)."""
        out = self._send("GET", path, params, None, what)
        if isinstance(out, _HttpProblem):
            if out.status in set(allow_status):
                return out
            raise K.ConnectorError("data", f"{self.ep.label} antwortete bei {what} mit HTTP {out.status}"
                                           + (f": {out.text}" if out.text else "") + ".")
        return out

    def rpc(self, method: str, params: list[Any], *, what: str) -> Any:
        """JSON-RPC 2.0 (POST). Drosselung des Anbieters (Fehlercodes −32005/−32429, 429) wird wiederholt."""
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        attempt = 0
        while True:
            attempt += 1
            out = self._send("POST", "", None, payload, what)
            if isinstance(out, _HttpProblem):
                raise K.ConnectorError("data", f"{self.ep.label} antwortete bei {what} mit HTTP {out.status}"
                                               + (f": {out.text}" if out.text else "") + ".")
            if not isinstance(out, dict):
                raise K.ConnectorError("data", f"Unerwartete Antwort von {self.ep.label} ({what}).")
            err = out.get("error")
            if err is None:
                return out.get("result")
            code = err.get("code") if isinstance(err, dict) else None
            msg = str(err.get("message") if isinstance(err, dict) else err)[:160]
            if code in (-32005, -32429, 429) or "rate limit" in msg.lower() or "too many" in msg.lower():
                self.throttled += 1
                if attempt <= RETRIES and self._pause(min(30.0, 2.0 ** attempt)):
                    self.retries += 1
                    continue
                raise K.ConnectorError("rate_limit", f"{self.ep.label} drosselt Anfragen ({what}).",
                                       retry_after_s=120)
            raise RpcError(code, msg)

    def pmap(self, fn: Callable[[T], R], items: Iterable[T], workers: int = 2) -> list[R]:
        """Begrenzt parallel abrufen (Reihenfolge der Ergebnisse wie die Eingabe). Der Mindestabstand je Anbieter
        gilt weiter; der erste Fehler bzw. :class:`Stop` bricht ab."""
        seq = list(items)
        if workers <= 1 or len(seq) <= 1:
            return [fn(x) for x in seq]
        with ThreadPoolExecutor(max_workers=min(workers, 4), thread_name_prefix=f"chain-{self.ep.id}") as pool:
            return list(pool.map(fn, seq))


class RpcError(K.ConnectorError):
    def __init__(self, code: Any, message: str) -> None:
        super().__init__("data", f"Anbieter meldet Fehler {code}: {message}")
        self.code = code
        self.rpc_message = message


@dataclass(frozen=True)
class _HttpProblem:
    status: int
    text: str


def _excerpt(raw: bytes) -> str:
    try:
        body = loads(raw)
    except ValueError:
        text = raw[:200].decode("utf-8", "replace")
    else:
        if isinstance(body, dict):
            text = str(body.get("detail") or body.get("message") or body.get("error") or "")
        else:
            text = ""
    return re.sub(r"\s+", " ", text).strip()[:160]
