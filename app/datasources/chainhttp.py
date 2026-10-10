"""Abrufe bei Chain-Anbietern (Explorer/Indexer) – nur geprüfte, fest hinterlegte Endpunkte, nur lesend.

Sicherheit (kein SSRF)
    Es gibt keine frei eingebbaren URLs. Jeder Anbieter ist hier mit festem HTTPS-Host und Basis-Pfad hinterlegt;
    Pfadteile stammen nur aus formatgeprüften Werten (Adressen, Hashes, Signaturen). Vor dem Senden wird jede Anfrage
    gegen Schema, Host und Basis-Pfad des Anbieters geprüft; Weiterleitungen werden nicht verfolgt. API-Keys gehen
    nur an den Anbieter, für den sie hinterlegt sind (Header bzw. der dort dokumentierte Parameter), und erscheinen
    nie in Logs oder Meldungen (URLs werden nie protokolliert). Netzwerk-Platzhalter (``{network}``, z. B. Polkadot
    Relay-Chain bzw. Asset Hub) werden nur mit Werten aus einer festen Liste (:data:`NETWORKS`) ersetzt.

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


# erlaubte Werte für den Platzhalter {network} (Subscan: Relay-Chain und Asset Hub von Polkadot)
NETWORKS = frozenset({"polkadot", "assethub-polkadot", "peaq"})


@dataclass(frozen=True)
class Endpoint:
    """Ein geprüfter Anbieter-Endpunkt (fester Host und Basis-Pfad)."""

    id: str
    label: str
    base: str  # https://host/pfad – Platzhalter {chain} (Routescan) bzw. {network} (Subscan)
    rps: float  # höchstens so viele Anfragen je Sekunde (konservativ unter dem dokumentierten Limit)
    auth: str = "none"  # none | query:<name> | header:<name> | bearer (Authorization: Bearer <Schlüssel>) | path
    key_provider: str | None = None  # Schlüssel in „Anbieter-Schlüssel“ (provider_secret)
    key_required: bool = False
    rps_with_key: float | None = None
    docs: str = ""
    terms: str = ""  # Kosten/Limits laut Anbieter (Stand der Recherche)
    read_timeout_s: float | None = None  # langsame Indexer (Kaltstart einer Adresse): längere Lesezeit
    # Etherscan-kompatible Blockbereich-Parameter zusätzlich als ``start_block``/``end_block`` senden (Blockscout-
    # Instanzen, die ``startblock``/``endblock`` live ignorieren – z. B. PulseChain-Explorer, Stand 10/2026)
    block_param_alias: bool = False

    @property
    def host(self) -> str:
        return urlsplit(self.base).hostname or ""

    def base_for(self, chain_id: int | None = None, network: str | None = None) -> str:
        base = self.base
        if "{chain}" in base:
            if chain_id is None or not 0 < int(chain_id) < 10**9:
                raise K.ConnectorError("config", "Chain-ID fehlt für diesen Anbieter.")
            base = base.replace("{chain}", str(int(chain_id)))
        if "{network}" in base:
            if network not in NETWORKS:
                raise K.ConnectorError("config", "Netzwerk fehlt bzw. ist für diesen Anbieter nicht freigegeben.")
            base = base.replace("{network}", str(network))
        return base


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
    Endpoint("nodereal_bsc", "NodeReal BSCTrace (MegaNode, kostenloser Key)", "https://bsc-mainnet.nodereal.io/v1",
             rps=4.0, auth="path", key_provider="nodereal", key_required=True,
             docs="https://docs.nodereal.io/reference/nr_getassettransfers",
             terms="kostenloser MegaNode-Key (nodereal.io), von BNB Chain als Ersatz für die BscScan-API empfohlen; "
                   "Abfragen in Blockfenstern ≤ 100.000; Kontingent in Compute Units laut NodeReal-Tarif"),
    Endpoint("kaspa", "Kaspa REST-API (api.kaspa.org)", "https://api.kaspa.org", rps=2.0,
             docs="https://api.kaspa.org/docs", terms="ohne Key; Limit nicht beziffert – höchstens 2×/s"),
    Endpoint("kasplex", "Kasplex KRC-20-Indexer", "https://api.kasplex.org/v1", rps=5.0,
             docs="https://docs-kasplex.gitbook.io/krc20",
             terms="ohne Key; Limit laut Antwort-Header x-ratelimit-limit 1000 je Zeitfenster (10/2026, nicht "
                   "dokumentiert) – Portfolia höchstens 5×/s"),
    Endpoint("blockscout_polygon", "Blockscout Polygon PoS (Etherscan-kompatibel, ohne Key)",
             "https://polygon.blockscout.com/api", rps=2.0, docs="https://docs.blockscout.com/devs/apis/rpc",
             terms="ohne Key; höchstens 10.000 Einträge je Abfrage; interne Transaktionen älterer Blöcke teils noch "
                   "nicht verarbeitet (wird als Lücke angezeigt) – Portfolia fragt höchstens 2×/s"),
    Endpoint("blockscout_pulsechain", "PulseChain-Explorer (Blockscout, Etherscan-kompatibel, ohne Key)",
             "https://api.scan.pulsechain.com/api", rps=2.0, docs="https://docs.blockscout.com/devs/apis/rpc",
             read_timeout_s=75.0, block_param_alias=True,
             terms="ohne Key; offizieller Explorer scan.pulsechain.com (Blockscout) – Limit nicht beziffert, "
                   "Portfolia fragt höchstens 2×/s"),
    Endpoint("pulsechain_rpc", "PulseChain RPC (rpc.pulsechain.com)", "https://rpc.pulsechain.com", rps=1.0,
             docs="https://pulsechain.com",
             terms="ohne Key; kopierter Bestand am Fork-Block (eine Anfrage je Adresse) und Bestandsabfrage als "
                   "Ausweichweg"),
    Endpoint("subscan_evm", "Subscan Etherscan-kompatible API (direkter Subscan-Key)",
             "https://{network}.api.subscan.io/api/scan/evm/etherscan", rps=4.0, auth="header:X-API-Key",
             key_provider="subscan", key_required=True, docs="https://support.subscan.io",
             terms="nur mit direktem (kostenpflichtigem) Subscan-Key; über das kostenlose PubFi-Gateway lässt die "
                   "Route keine Abfrageparameter zu"),
    Endpoint("peaq_rpc", "peaq öffentlicher EVM-RPC (OnFinality)", "https://peaq.api.onfinality.io/public", rps=1.0,
             docs="https://docs.peaq.xyz/build/getting-started/connecting-to-peaq",
             terms="ohne Key; nur Prüfabfragen (eth_getBalance, eth_getTransactionCount) für 0x-Adressen, die "
                   "Subscan nicht kennt"),
    Endpoint("peaq_rpc_3", "peaq öffentlicher EVM-RPC (PublicNode)", "https://peaq-rpc.publicnode.com", rps=1.0,
             docs="https://docs.peaq.xyz/build/getting-started/connecting-to-peaq",
             terms="ohne Key; Ausweichadresse für Prüfabfragen (eth_getBalance, eth_getTransactionCount)"),
    Endpoint("rpc_ethereum", "Ethereum öffentlicher RPC (PublicNode)", "https://ethereum-rpc.publicnode.com", rps=1.0,
             docs="https://www.publicnode.com", terms="ohne Key; nur Bestandsabfrage (eth_getBalance) als Ausweichweg"),
    Endpoint("rpc_bsc", "BNB Chain öffentlicher RPC (PublicNode)", "https://bsc-rpc.publicnode.com", rps=1.0,
             docs="https://www.publicnode.com", terms="ohne Key; nur Bestandsabfrage (eth_getBalance) als Ausweichweg"),
    Endpoint("rpc_avalanche", "Avalanche C-Chain öffentlicher RPC (PublicNode)",
             "https://avalanche-c-chain-rpc.publicnode.com", rps=1.0, docs="https://www.publicnode.com",
             terms="ohne Key; nur Bestandsabfrage (eth_getBalance) als Ausweichweg"),
    Endpoint("rpc_polygon", "Polygon öffentlicher RPC (PublicNode)", "https://polygon-bor-rpc.publicnode.com",
             rps=1.0, docs="https://www.publicnode.com",
             terms="ohne Key; nur Bestandsabfrage (eth_getBalance) als Ausweichweg"),
    Endpoint("peaq_rpc_2", "peaq öffentlicher EVM-RPC (quicknode1.peaq.xyz)", "https://quicknode1.peaq.xyz", rps=1.0,
             docs="https://docs.peaq.xyz/build/getting-started/connecting-to-peaq",
             terms="ohne Key; Ausweichadresse für Prüfabfragen (eth_getBalance, eth_getTransactionCount)"),
    Endpoint("xrplcluster", "XRPL Cluster (xrplcluster.com, vollständige Historie)", "https://xrplcluster.com",
             rps=2.0, docs="https://xrpl.org/docs/tutorials/public-servers",
             terms="ohne Key; öffentlicher Full-History-Cluster (Community) – Portfolia fragt höchstens 2×/s"),
    Endpoint("ripple_s2", "Ripple s2 (vollständige Historie)", "https://s2.ripple.com:51234", rps=1.0,
             docs="https://xrpl.org/docs/tutorials/public-servers",
             terms="ohne Key; öffentlicher Full-History-Server von Ripple (Port 51234) – höchstens 1×/s"),
    Endpoint("koios", "Koios (Cardano, api.koios.rest)", "https://api.koios.rest/api/v1", rps=1.5, auth="bearer",
             key_provider="koios", rps_with_key=4.0, docs="https://api.koios.rest",
             terms="ohne Key: öffentlicher Tarif (5.000 Anfragen/Tag, 100 je 10 s); kostenloser Key (Bearer-Token, "
                   "koios.rest): 50.000 Anfragen/Tag"),
    Endpoint("pubfi", "PubFi-Gateway für Subscan (kostenloser Key)",
             "https://api.pubfi.ai/v1/gateway/subscan/{network}", rps=1.5, auth="bearer", key_provider="pubfi",
             key_required=True, docs="https://support.subscan.io/doc-360177",
             terms="kostenloser PubFi-Key (pubfi.ai, Bearer); Free-Routen „:free“: 2 Anfragen/s, 20.000/Tag"),
    Endpoint("subscan", "Subscan direkt (kostenpflichtiger Key)", "https://{network}.api.subscan.io", rps=4.0,
             auth="header:X-API-Key", key_provider="subscan", key_required=True, docs="https://support.subscan.io",
             terms="nur mit kostenpflichtigem Subscan-Plan (neue kostenlose Keys gibt es nur über PubFi); Limit je "
                   "Plan"),
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
                 network: str | None = None,
                 transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] | None = None,
                 clock: Callable[[], float] = time.monotonic, max_requests: int = 3000,
                 wait_budget_s: float = 240.0, deadline_s: float | None = None,
                 usage: Callable[[int], None] | None = None) -> None:
        if ep.key_required and not key:
            raise K.ConnectorError("key_missing", f"{ep.label} verlangt einen Schlüssel des Anbieters – unter "
                                                  "„Anbieter-Schlüssel“ hinterlegen.")
        self.ep = ep
        self.network = network
        self.base = ep.base_for(chain_id, network)
        parts = urlsplit(self.base)
        if parts.scheme != "https" or not parts.hostname:
            raise K.ConnectorError("config", "Anbieter-Endpunkt ungültig.")  # pragma: no cover - fester Katalog
        self._host = parts.hostname
        self._base_path = parts.path.rstrip("/")
        self._key = key or None
        self._cancel = K.current_cancel()  # Abbruchsignal des Laufs – wirkt auch in Hilfsthreads (pmap)
        self.sleep = sleep if sleep is not None else (lambda s: K.interruptible_sleep(s, self._cancel))
        self.clock = clock
        self.max_requests = max_requests
        self.wait_budget = wait_budget_s
        self.deadline = clock() + deadline_s if deadline_s else None
        self.interval = 1.0 / ((ep.rps_with_key or ep.rps) if key else ep.rps)
        self._limiter = _limiter(f"{ep.id}:{'key' if key else 'anon'}")  # je Anbieter (auch über Netzwerke hinweg)
        self._usage = usage
        self._lock = threading.Lock()
        self._tls = threading.local()  # erlaubte Statuscodes des laufenden Aufrufs (thread-sicher bei pmap)
        self.requests = 0
        self.throttled = 0
        self.retries = 0
        self.waited = 0.0  # Wartezeit durch Drosselung/Backoff (Budget)
        self.paced = 0.0  # Wartezeit durch den Mindestabstand (nur Anzeige)
        timeout = httpx.Timeout(ep.read_timeout_s, connect=10.0) if ep.read_timeout_s else TIMEOUT
        self._client = httpx.Client(timeout=timeout, follow_redirects=False, transport=transport,
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
        K.check_cancel(self._cancel)
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
        K.check_cancel(self._cancel)
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
        elif self._key and self.ep.auth == "bearer":
            headers["Authorization"] = f"Bearer {self._key}"
        url = self.base + (path if path not in ("", "/") else "")
        if self.ep.auth == "path":  # Schlüssel als Pfadsegment (NodeReal: …/v1/<Key>)
            if not self._key or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", self._key):
                raise K.ConnectorError("auth", f"{self.ep.label}: Schlüssel fehlt oder hat ein ungültiges Format.")
            url = f"{self.base}/{self._key}" + (path if path not in ("", "/") else "")
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
                raw = resp.content[:MAX_EXCERPT_BODY]
                if code in self._allow:  # Connector wertet die Antwort selbst aus (z. B. Kasplex: 403 = Status)
                    return _HttpProblem(code, _excerpt(raw), _blocked(resp))
                raise self._denied(code, what, resp, raw)
            if code == 402:
                raise K.ConnectorError("scope", f"{self.ep.label} verlangt für {what} einen Tarif bzw. Kontingent "
                                                "(HTTP 402) – Kontingent oder Tarif beim Anbieter prüfen.")
            raw = resp.content
            if len(raw) > MAX_BODY:
                raise K.ConnectorError("data", f"Antwort von {self.ep.label} zu groß ({what}).")
            if code >= 400:
                return _HttpProblem(code, _excerpt(raw), _blocked(resp))
            try:
                return loads(raw)
            except ValueError:
                raise K.ConnectorError("data", f"Antwort von {self.ep.label} ist kein gültiges JSON ({what}).") \
                    from None

    def get(self, path: str = "", params: Mapping[str, Any] | None = None, *, what: str,
            allow_status: Iterable[int] = ()) -> Any:
        """GET → JSON. 4xx-Antworten sind Fehler, außer ``allow_status`` (dann :class:`_HttpProblem`, auch für
        401/403 – der Connector wertet dann selbst aus, z. B. Kasplex: 403 = „nicht synchron“)."""
        allow = frozenset(allow_status)
        out = self._with_allow(allow, lambda: self._send("GET", path, params, None, what))
        if isinstance(out, _HttpProblem):
            if out.status in allow:
                return out
            raise self._problem(out, what)
        return out

    def post(self, path: str = "", body: Any = None, params: Mapping[str, Any] | None = None, *, what: str,
             allow_status: Iterable[int] = ()) -> Any:
        """POST mit JSON-Rumpf → JSON (z. B. Koios, Subscan, XRPL). 4xx wie bei :meth:`get`."""
        allow = frozenset(allow_status)
        out = self._with_allow(allow, lambda: self._send("POST", path, params, body if body is not None else {},
                                                         what))
        if isinstance(out, _HttpProblem):
            if out.status in allow:
                return out
            raise self._problem(out, what)
        return out

    def _with_allow(self, allow: frozenset[int], fn: Callable[[], Any]) -> Any:
        prev = getattr(self._tls, "allow", frozenset())
        self._tls.allow = allow
        try:
            return fn()
        finally:
            self._tls.allow = prev

    @property
    def _allow(self) -> frozenset[int]:
        return getattr(self._tls, "allow", frozenset())

    def backoff(self, seconds: float) -> bool:
        """Kurz warten (im Warte- und Zeitbudget des Laufs) – für vom Connector erkannte vorübergehende Zustände."""
        return self._pause(seconds)

    def _problem(self, out: _HttpProblem, what: str) -> K.ConnectorError:
        if out.status in (404, 410):
            return K.ConnectorError("gone", f"{self.ep.label}: Endpunkt für {what} nicht gefunden (HTTP {out.status})"
                                            " – Schnittstelle des Anbieters geändert oder eingestellt.")
        return K.ConnectorError("data", f"{self.ep.label} antwortete bei {what} mit HTTP {out.status}"
                                        + (f": {out.text}" if out.text else "") + ".")

    def _denied(self, code: int, what: str, resp: httpx.Response, raw: bytes) -> K.ConnectorError:
        """401/403 unterscheiden: Schlüssel gesendet → abgelehnt; ohne Schlüssel → Zugriff verweigert (Grund laut
        Antwort bzw. Schutzsystem), nie ein Hinweis auf Schlüssel bei Anbietern ohne Schlüssel."""
        msg = _excerpt(raw)
        blocked = _blocked(resp)
        detail = f"HTTP {code}" + (f": „{msg}“" if msg and not blocked else "")
        rid = resp.headers.get("pubfi-request-id")
        if self.ep.key_provider == "pubfi" and self._key and not blocked:
            ref = f" (Anfrage-ID {rid[:36]})" if rid else ""
            if code == 401:
                return K.ConnectorError("auth", f"{self.ep.label} kennt den Schlüssel nicht ({what}, {detail}){ref} – "
                                                "Schlüssel unter „Anbieter-Schlüssel“ neu eintragen (Bearer-Schlüssel "
                                                "von pubfi.ai, nicht der Subscan-Schlüssel).")
            return K.ConnectorError("scope", f"Das PubFi-Konto ist für diese kostenlose Route nicht freigeschaltet "
                                             f"({what}, {detail}){ref} – der Schlüssel wird erkannt; Konto bzw. Tarif "
                                             "bei PubFi prüfen oder als Anbieter „Subscan direkt“ wählen.")
        if self._key:
            return K.ConnectorError("auth", f"{self.ep.label} lehnt den Schlüssel ab ({what}, {detail}) – Schlüssel "
                                            "unter „Anbieter-Schlüssel“ prüfen.")
        if blocked:
            return K.ConnectorError("forbidden", f"{self.ep.label} verweigert {what}: {blocked} (HTTP {code}) – "
                                                 "später erneut versuchen.")
        if self.ep.key_provider:
            return K.ConnectorError("forbidden", f"{self.ep.label} verweigert {what} ({detail}) – ohne Schlüssel "
                                                 "abgefragt; ein Schlüssel unter „Anbieter-Schlüssel“ kann helfen.")
        return K.ConnectorError("forbidden", f"{self.ep.label} verweigert {what} ({detail}) – der Anbieter "
                                             "verlangt keinen Schlüssel; die Anfrage wurde abgelehnt.")

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
    blocked: str | None = None  # Schutzsystem (z. B. Cloudflare) statt Antwort des Dienstes


MAX_EXCERPT_BODY = 64 * 1024


def _blocked(resp: httpx.Response) -> str | None:
    """HTML-Sperrseite eines vorgeschalteten Schutzsystems (Cloudflare/WAF) statt einer Antwort des Dienstes."""
    ctype = resp.headers.get("content-type", "").lower()
    if "html" not in ctype:
        return None
    server = resp.headers.get("server", "").lower()
    ray = resp.headers.get("cf-ray")
    if "cloudflare" in server or ray:
        return "Schutzsystem des Anbieters (Cloudflare) blockiert die Anfrage" + (f", Ray-ID {ray[:24]}" if ray
                                                                                   else "")
    return "Sperrseite statt Antwort des Dienstes (Schutzsystem/WAF)"


def _excerpt(raw: bytes) -> str:
    try:
        body = loads(raw)
    except ValueError:
        text = raw[:200].decode("utf-8", "replace")
    else:
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):  # z. B. PubFi: {"error": {"code": "pubfi.forbidden", "message": "…"}}
                code, msg = str(err.get("code") or ""), str(err.get("message") or "")
                text = f"{code}: {msg}" if code and msg and code != msg else (msg or code)
            else:
                text = str(body.get("detail") or body.get("message") or err or "")
        else:
            text = ""
    return re.sub(r"\s+", " ", text).strip()[:160]
