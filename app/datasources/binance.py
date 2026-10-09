"""Binance (binance.com) – Spot-Konto über die offizielle REST-API, nur lesend (HMAC-SHA256).

Vertrag (developers.binance.com, Stand der Recherche 10/2026)
    Signierte Anfragen (``USER_DATA``): Parameter als Query-String, ``timestamp`` (ms) und ``recvWindow`` (≤ 60000),
    ``signature`` = HMAC-SHA256(Secret, Query-String) hex; Schlüssel im Header ``X-MBX-APIKEY``. Grenzen je IP-Gewicht;
    429 → zurückhalten (``Retry-After``), 418 → automatische Sperre. Fehler als ``{"code", "msg"}``: -1021 Zeitstempel
    außerhalb ``recvWindow``, -1022 Signatur ungültig, -2014 Key-Format, -2015 Key/IP/Rechte.

Abgerufen (nur ``GET``)
    * ``/api/v3/time`` – Uhrzeit des Servers (Zeitversatz), ``/api/v3/exchangeInfo`` – Paare (Basis/Quote)
    * ``/api/v3/account`` (``omitZeroBalances``) – Spot-Bestände (Bestandsprüfung, Asset-Liste)
    * ``/api/v3/myTrades`` – Fills je Paar, ``fromId``-Paging (≤ 1000), Gebühr ``commission``/``commissionAsset``
    * ``/sapi/v1/capital/deposit/hisrec`` und ``…/withdraw/history`` – Ein-/Auszahlungen (Fenster < 90 Tage, offset)
    * ``/sapi/v1/asset/assetDividend`` – Ausschüttungen (Fenster ≤ 180 Tage, ≤ 500 je Abfrage)
    * ``/sapi/v1/asset/dribblet`` – Staubumtausch (nur letzte 100 Vorgänge ab 01.12.2020)
    * ``/sapi/v1/convert/tradeFlow`` – Convert (Fenster ≤ 30 Tage, ``moreData``)
    * ``/sapi/v1/fiat/payments`` – Kauf/Verkauf mit Karte/Bank, ``/sapi/v1/fiat/orders`` – Fiat-Ein-/Auszahlungen

Kennungen
    ``binance:trade:<PAAR>:<id>``, ``binance:dep:<id>``, ``binance:wd:<id>``, ``binance:div:<tranId>``,
    ``binance:dust:<transId>``, ``binance:convert:<orderId>``, ``binance:fiatpay:<orderNo>``,
    ``binance:fiat:<orderNo>``.
    Das CSV-Profil (Kontoauszug) kennt keine nativen IDs – frühere CSV-Importe erkennt der Abgleich über Menge/Zeit
    bzw. den Tx-Hash (Ein-/Auszahlungen).

Annahmen (dokumentiert, Bestandsprüfung macht Fehler sichtbar)
    * ``myTrades``: ``qty``/``quoteQty`` brutto, die ``commission`` wird zusätzlich vom ``commissionAsset`` abgezogen.
    * Auszahlung: Ob ``amount`` die ``transactionFee`` enthält, ist nicht dokumentiert → Gebühr „offen“ (Prüfung).
    * ``applyTime``/``completeTime`` der Auszahlungen ohne Zonenangabe (Beispiel ``2019-10-12 11:12:02``) → UTC.
    * Fiat- und Staubvorgänge: Gebührenbezug nicht dokumentiert → Prüfung, wenn eine Gebühr > 0 vorliegt.

Grenzen
    Paare werden aus bekannten Assets gebildet (Bestände, Ein-/Auszahlungen, Ausschüttungen, Convert, Fiat, frühere
    Trades): Ein Handel in ein Asset, das nie gehalten, ein-/ausgezahlt oder anders bewegt wurde, ist über die
    Spot-API nicht auffindbar – dafür den CSV-Kontoauszug abgleichen. Earn/Staking-Umbuchungen, Futures, Margin, P2P,
    Pay und Unterkonten werden nicht abgerufen; Earn-Bestände fehlen in der Bestandsprüfung.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar
from urllib.parse import urlencode

import httpx

from app import __version__
from app.csvimport import model as M
from app.csvimport.model import Rec
from app.datasources import connector as K

BASE_URL = "https://api.binance.com"
PREFIX = "binance"
PARSER_VERSION = 1
CURSOR_VERSION = 1
LAUNCH = datetime(2017, 7, 14, tzinfo=UTC)  # Start von Binance – frühester sinnvoller Abrufbeginn
RECV_WINDOW = 10000
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
DAY_MS = 86_400_000
WIN_DEPOSIT = 89 * DAY_MS  # „less than 90 days“
WIN_DIVIDEND = 180 * DAY_MS
WIN_CONVERT = 30 * DAY_MS
WIN_FIAT = 89 * DAY_MS  # Höchstfenster nicht dokumentiert – wie Ein-/Auszahlungen
OVERLAP_MS = 2 * DAY_MS  # spät gebuchte Vorgänge – bekannte Kennungen werden erkannt
TRADE_LIMIT = 1000
DIV_LIMIT = 500
FIAT_ROWS = 500
WEIGHT_SOFT = 4800  # von 6000 je Minute (IP) – darüber bis zur nächsten Minute warten
MAX_REQUESTS = 900
DEADLINE_S = 240.0
MAX_PAIR_REQUESTS = 220  # myTrades-Abfragen je Etappe (Gewicht 20 je Abfrage)
HOLD_MAX_MS = 30 * DAY_MS  # länger „offene“ Vorgänge halten den Abrufstand nicht mehr zurück
# Mindestabstand je Route (Sekunden) – konservativ zu den UID-Gewichten laut Doku (Auszahlungen 18000, Convert 3000,
# Fiat-Aufträge 45000); das UID-Limit selbst ist in den gelesenen Seiten nicht beziffert (Annahme: 180000/Minute)
PACE = {"/sapi/v1/capital/withdraw/history": 7.0, "/sapi/v1/convert/tradeFlow": 1.2,
        "/sapi/v1/fiat/orders": 16.0}
PACE_DEFAULT = 0.25
FIAT = frozenset({"EUR", "USD", "GBP", "CHF", "TRY", "BRL", "AUD", "PLN", "RON", "CZK", "UAH", "ZAR", "JPY", "ARS",
                  "MXN", "NGN", "RUB", "CAD", "SEK", "NOK", "DKK", "HUF", "KZT", "IDR", "VND", "INR"})
_SYMBOL = re.compile(r"^[A-Z0-9]{2,30}$")
_HEXHASH = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
ZERO = Decimal(0)

DEPOSIT_DONE = {1, 6}  # 1 Success, 6 Credited but cannot withdraw (gutgeschrieben)
DEPOSIT_OPEN = {0, 8}  # 0 Pending, 8 Waiting for user confirmation – Abrufstand nicht darüber hinaus
WITHDRAW_DONE = {6}  # 6 Completed
WITHDRAW_OPEN = {0, 2, 4}  # Email Sent, Awaiting Approval, Processing
FIAT_DONE = {"successful", "finished", "completed"}
FIAT_OPEN = {"processing", "refunding"}
INCOME_WORDS = (("staking", "staking"), ("eth 2.0", "staking"), ("savings", "interest"),
                ("simple earn", "interest"), ("interest", "interest"), ("launchpool", "airdrop"),
                ("airdrop", "airdrop"), ("megadrop", "airdrop"), ("distribution", "airdrop"),
                ("commission", "bonus"), ("referral", "bonus"), ("rebate", "cashback"), ("cashback", "cashback"),
                ("mining", "mining"))


def _dec(v: Any) -> Decimal | None:
    try:
        d = Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _ms(v: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(v) / 1000, UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _utc_text(v: Any) -> datetime | None:
    """``2019-10-12 11:12:02`` (Auszahlungen) – ohne Zone, laut Annahme UTC."""
    try:
        return datetime.strptime(str(v).strip()[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return _ms(v)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _q(v: Decimal) -> str:
    return format(v.normalize(), "f")


def _sym(v: Any) -> str | None:
    s = str(v or "").strip().upper()
    return s if _SYMBOL.match(s) else None


def _tag_for(info: str) -> str | None:
    low = info.lower()
    return next((tag for word, tag in INCOME_WORDS if word in low), None)


# ----------------------------------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------------------------------

def split_secret(raw: str) -> tuple[str, str]:
    """Gespeichert als ``API-Key:Secret`` (beide ohne Leerzeichen)."""
    key, sep, secret = (raw or "").strip().partition(":")
    if not sep or not key or not secret:
        raise K.ConnectorError("config", "Binance-Zugang unvollständig – API-Key und Secret Key hinterlegen.")
    return key.strip(), secret.strip()


class _Api:
    """Signierte GETs mit Zeitabgleich, Gewichts-/Routen-Taktung, Retry-After und Budget."""

    def __init__(self, key: str, secret: str, client: httpx.Client, sleep: Callable[[float], None],
                 clock: Callable[[], float], max_requests: int = MAX_REQUESTS, deadline_s: float = DEADLINE_S) -> None:
        self._key = key
        self._secret = secret.encode()
        self.client = client
        self.sleep = sleep
        self.clock = clock
        self.max_requests = max_requests
        self.deadline = clock() + deadline_s
        self.offset_ms = 0
        self.requests = 0
        self.throttled = 0
        self.waited = 0.0
        self.weight = 0
        self._last: dict[str, float] = {}

    def __repr__(self) -> str:  # nie Schlüssel ausgeben
        return f"_Api(requests={self.requests})"

    def budget_left(self) -> bool:
        return self.requests < self.max_requests and self.clock() < self.deadline

    def _pace(self, path: str) -> None:
        gap = PACE.get(path, PACE_DEFAULT)
        last = self._last.get(path)
        if last is not None:
            wait = gap - (self.clock() - last)
            if wait > 0:
                self._sleep(wait)
        if self.weight >= WEIGHT_SOFT:
            self._sleep(61 - (time.time() % 60))
            self.weight = 0

    def _sleep(self, s: float) -> None:
        if self.clock() + s > self.deadline:
            raise _Stop("Zeitbudget der Etappe erschöpft")
        self.waited += s
        self.sleep(s)

    def sync_time(self) -> None:
        body = self.get("/api/v3/time", None, "Serverzeit", signed=False)
        st = body.get("serverTime") if isinstance(body, dict) else None
        if isinstance(st, int):
            self.offset_ms = st - _now_ms()

    def get(self, path: str, params: dict[str, Any] | None, what: str, *, signed: bool = True) -> Any:
        for attempt in range(4):
            if not self.budget_left():
                raise _Stop("Anfrage-/Zeitbudget der Etappe erschöpft")
            K.check_cancel()
            self._pace(path)
            q = {k: v for k, v in (params or {}).items() if v is not None}
            headers = {}
            if signed:
                q["recvWindow"] = RECV_WINDOW
                q["timestamp"] = _now_ms() + self.offset_ms
                qs = urlencode(q)
                q_sig = hmac.new(self._secret, qs.encode(), hashlib.sha256).hexdigest()
                url = f"{path}?{qs}&signature={q_sig}"
                headers["X-MBX-APIKEY"] = self._key
            else:
                url = f"{path}?{urlencode(q)}" if q else path
            self._last[path] = self.clock()
            try:
                resp = self.client.get(url, headers=headers)
            except httpx.TimeoutException:
                if attempt < 2:
                    continue
                raise K.ConnectorError("unavailable", f"Binance antwortet nicht ({what}).") from None
            except httpx.HTTPError:
                raise K.ConnectorError("unavailable", f"Binance nicht erreichbar ({what}).") from None
            self.requests += 1
            used = resp.headers.get("x-mbx-used-weight-1m")
            if used and used.isdigit():
                self.weight = int(used)
            if resp.status_code in (429, 418):
                self.throttled += 1
                ra = resp.headers.get("retry-after")
                wait = float(ra) if ra and ra.replace(".", "", 1).isdigit() else 60.0
                if resp.status_code == 429 and wait <= 30 and attempt < 2:
                    self._sleep(wait)
                    continue
                raise K.ConnectorError("rate_limit", "Binance drosselt Anfragen" + (" (IP vorübergehend gesperrt)"
                                       if resp.status_code == 418 else "") + f" – {what}.", retry_after_s=int(wait) + 5)
            try:
                body = resp.json(parse_float=Decimal)
            except ValueError:
                body = None
            if resp.status_code == 200:
                return body
            code = body.get("code") if isinstance(body, dict) else None
            msg = str(body.get("msg") or "")[:160] if isinstance(body, dict) else ""
            if code == -1021 and attempt == 0 and signed:
                self.sync_time()
                continue
            raise _error(resp.status_code, code, msg, what)
        raise K.ConnectorError("unavailable", f"Binance: {what} nach mehreren Versuchen nicht abrufbar.")


class _Stop(Exception):
    pass


def _error(status: int, code: Any, msg: str, what: str) -> K.ConnectorError:
    if code in (-2014, -1022):
        return K.ConnectorError("auth", f"Binance lehnt den Zugang ab ({what}: "
                                        + ("Signatur ungültig – Secret Key prüfen" if code == -1022
                                           else "Format des API-Keys ungültig") + ").")
    if code == -2015 or status == 401:
        return K.ConnectorError("auth", f"Binance lehnt API-Key, IP oder Rechte ab ({what}) – Key prüfen, "
                                        "„Enable Reading“ aktivieren und ggf. die IP-Freigabe anpassen.")
    if code == -1021:
        return K.ConnectorError("config", "Zeitstempel außerhalb des Zeitfensters (Uhr des Servers stark abweichend?).")
    if status == 403:
        return K.ConnectorError("forbidden", f"Binance verweigert den Zugriff ({what}, HTTP 403) – Region oder "
                                             "Rechte des API-Keys.")
    if status >= 500:
        return K.ConnectorError("unavailable", f"Binance vorübergehend gestört ({what}, HTTP {status}).")
    return K.ConnectorError("data", f"Binance meldet bei {what}: {msg or f'HTTP {status}'}"
                                    + (f" (Code {code})" if code is not None else "") + ".")


# ----------------------------------------------------------------------------------------------------
# Connector
# ----------------------------------------------------------------------------------------------------

def _event(key: str, ts: datetime, recs: list[Rec], label: str) -> K.SourceEvent:
    for i, r in enumerate(recs):
        r.line, r.event_line = 0, i
    return K.SourceEvent(f"{PREFIX}:{key}", ts, recs, label)


@K.register
class BinanceConnector(K.Connector):
    provider = "binance"
    label = "Binance (Spot-API, nur lesend)"
    needs_credentials = True
    parser_version = PARSER_VERSION
    base_url: ClassVar[str] = BASE_URL
    transport: ClassVar[httpx.BaseTransport | None] = None  # nur Tests
    sleep: ClassVar[Callable[[float], None]] = staticmethod(K.interruptible_sleep)
    clock: ClassVar[Callable[[], float]] = staticmethod(time.monotonic)
    max_requests: ClassVar[int] = MAX_REQUESTS
    deadline_s: ClassVar[float] = DEADLINE_S

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=TIMEOUT, follow_redirects=False, transport=self.transport,
                            headers={"Accept": "application/json",
                                     "User-Agent": f"Portfolia/{__version__} (read-only)"})

    def _api(self, client: httpx.Client, secret: K.Secret, **kw: Any) -> _Api:
        key, sec = split_secret(secret.reveal())
        return _Api(key, sec, client, self.sleep, self.clock, **({"max_requests": self.max_requests,
                                                                   "deadline_s": self.deadline_s} | kw))

    # -- Verbindung prüfen --------------------------------------------------------------------------------
    def check(self, cfg: K.SourceConfig, secret: K.Secret) -> K.CheckResult:
        details: dict[str, Any] = {}
        with self._client() as client:
            api = self._api(client, secret, max_requests=12, deadline_s=60)
            api.sync_time()
            try:
                acc = api.get("/api/v3/account", {"omitZeroBalances": "true"}, "Konto (/api/v3/account)")
            except K.ConnectorError as e:
                details["balances"] = {"ok": False, "text": e.message}
                return K.CheckResult(False, e.message, details)
            balances = _balances(acc)
            perms = acc.get("permissions") if isinstance(acc, dict) else None
            details["balances"] = {"ok": True, "text": f"Spot-Bestände lesbar ({len(balances)} Assets)"}
            if isinstance(acc, dict) and acc.get("canTrade") is True:
                details["readonly"] = {"ok": False, "text": "Der API-Key darf handeln – Portfolia braucht nur „Enable "
                                                            "Reading“; Handelsrechte bei Binance abschalten"}
            else:
                details["readonly"] = {"ok": True, "text": "keine Handelsfreigabe erkennbar"}
            if perms:
                details["permissions"] = {"ok": True,
                                          "text": "Kontoberechtigungen: " + ", ".join(map(str, perms))[:120]}
            for name, path, params, what in (
                    ("deposits", "/sapi/v1/capital/deposit/hisrec", {"limit": 1}, "Einzahlungen"),
                    ("trades", "/api/v3/myTrades", {"symbol": "BNBUSDT", "limit": 1}, "Trades")):
                try:
                    api.get(path, params, what)
                    details[name] = {"ok": True, "text": f"{what} lesbar"}
                except K.ConnectorError as e:
                    if name == "trades" and e.kind == "data" and "-1121" in e.message:  # Prüfpaar nicht handelbar
                        details[name] = {"ok": True, "text": "Trades-Endpunkt erreichbar (Prüfpaar nicht verfügbar)"}
                        continue
                    details[name] = {"ok": False, "text": e.message}
        ok = all(d["ok"] for k, d in details.items() if k in ("balances", "deposits", "trades"))
        msg = "Verbindung in Ordnung – Bestände, Einzahlungen und Trades lesbar." if ok else \
            "Verbindung eingeschränkt – Details unten."
        return K.CheckResult(ok, msg, details, balances=[K.Balance(a, q, a) for a, q in sorted(balances.items())])

    # -- Abrufen ------------------------------------------------------------------------------------------
    def fetch(self, cfg: K.SourceConfig, secret: K.Secret, cursor: dict[str, Any] | None) -> K.FetchResult:
        cur = dict(cursor or {}) if (cursor or {}).get("v") == CURSOR_VERSION else {}
        launch = int(LAUNCH.timestamp() * 1000)
        streams: dict[str, int] = {k: int(v) for k, v in (cur.get("streams") or {}).items() if k in _STREAM_LABEL}
        from_id: dict[str, int] = {k: int(v) for k, v in (cur.get("trades") or {}).items() if _sym(k)}
        assets: set[str] = {a for a in cur.get("assets") or [] if _sym(a)}
        before = (dict(streams), dict(from_id))
        sweep_after: str | None = cur.get("sweep_after") if isinstance(cur.get("sweep_after"), str) else None
        res = K.FetchResult(complete=True)
        skipped: Counter[str] = Counter()
        events: list[K.SourceEvent] = []
        done: set[str] = set()
        problem = False
        with self._client() as client:
            api = self._api(client, secret)
            run = _Run(api, streams, launch, events, assets, skipped, res)
            try:
                api.sync_time()
                acc = api.get("/api/v3/account", {"omitZeroBalances": "true"}, "Bestände")
                held = _balances(acc)
                assets |= set(held)
                res.balances = [K.Balance(a, q, a, "nur Spot (ohne Earn/Funding)") for a, q in sorted(held.items())]
                run.now = _now_ms() + api.offset_ms
                for name in PHASE_A:
                    self.report("Abruf", len(events), None, _STREAM_LABEL[name])
                    getattr(run, _STREAM_FN[name])(name)
                    done.add(name)
                checked: set[str] = set()
                marker = self._sweep(api, self._symbols(api, assets, from_id), from_id, sweep_after, checked, run)
                sweep_after = marker
                if marker is None:
                    done.add("trades")
                for name in PHASE_C:
                    self.report("Abruf", len(events), None, _STREAM_LABEL[name])
                    getattr(run, _STREAM_FN[name])(name)
                    done.add(name)
                new = [t for t in self._symbols(api, assets, from_id) if t[0] not in checked and t[0] not in from_id]
                if new and self._sweep(api, new, from_id, None, checked, run) is not None:
                    done.discard("trades")
            except _Stop as e:
                res.warnings.append(f"{e} – Fortsetzung im nächsten Lauf")
                problem = True
            except K.ConnectorError as e:
                if e.kind in ("auth", "scope", "config", "forbidden") or (streams, from_id) == before:
                    raise
                res.warnings.append(f"{e.message} – Fortsetzung im nächsten Lauf")
                problem = True
            res.events = sorted(events, key=lambda ev: ev.ts)
            res.skipped = dict(skipped)
            res.complete = not problem and done >= {*PHASE_A, *PHASE_C, "trades"}
            res.resume = not res.complete
            res.cursor = {"v": CURSOR_VERSION, "streams": streams, "trades": from_id, "assets": sorted(assets)[:2000]}
            if sweep_after is not None and not res.complete:
                res.cursor["sweep_after"] = sweep_after
            res.coverage = {"mode": "inkrementell" if cur else "historisch", "operations": len(res.events),
                            "provider": "api.binance.com", "requests": api.requests, "throttled": api.throttled,
                            "waited_s": round(api.waited, 1), "pairs": len(from_id), "assets": len(assets)}
        return res

    # -- Trades -------------------------------------------------------------------------------------------
    def _symbols(self, api: _Api, assets: set[str], known: dict[str, int]) -> list[tuple[str, str, str]]:
        """Paare mit Basis und Quote unter den bekannten Assets plus bereits geprüfte Paare (nach Name sortiert)."""
        info = self.catalog.get("exchangeInfo") if isinstance(self.catalog, dict) else None
        if not isinstance(info, list):
            body = api.get("/api/v3/exchangeInfo", None, "Handelspaare", signed=False)
            raw = (body or {}).get("symbols") if isinstance(body, dict) else None
            triples = ((_sym(x.get("symbol")), _sym(x.get("baseAsset")), _sym(x.get("quoteAsset")))
                       for x in raw or [] if isinstance(x, dict))
            info = [(s, b, q) for s, b, q in triples if s and b and q]
            if not info:
                raise K.ConnectorError("data", "Binance liefert keine Handelspaare (/api/v3/exchangeInfo).")
            if isinstance(self.catalog, dict):
                self.catalog["exchangeInfo"] = info
        return sorted(t for t in info if t[0] in known or (t[1] in assets and t[2] in assets))

    def _sweep(self, api: _Api, symbols: list[tuple[str, str, str]], from_id: dict[str, int], after: str | None,
               checked: set[str], run: _Run) -> str | None:
        """Trades je Paar ab ``fromId`` – Rückgabe: zuletzt vollständig geprüftes Paar, wenn die Etappe endet."""
        last = after
        todo = [t for t in symbols if after is None or t[0] > after]
        for i, (sym, base, quote) in enumerate(todo):
            if run.pair_requests >= MAX_PAIR_REQUESTS or not api.budget_left():
                return last
            self.report("Trades", i, len(todo), f"Trades {sym}")
            nxt = from_id.get(sym, 0)
            while True:
                try:
                    rows = api.get("/api/v3/myTrades", {"symbol": sym, "fromId": nxt, "limit": TRADE_LIMIT},
                                   f"Trades {sym}")
                except K.ConnectorError as e:
                    if e.kind == "data" and "-1121" in e.message:  # Paar unbekannt (z. B. entfernt)
                        run.skipped["Paare ohne Handelsdaten (bei Binance unbekannt)"] += 1
                        break
                    raise
                run.pair_requests += 1
                rows = rows if isinstance(rows, list) else []
                for r in rows:
                    ev = _trade(r, sym, base, quote, run.skipped)
                    if ev is not None:
                        run.events.append(ev)
                ids = [r["id"] for r in rows if isinstance(r, dict) and isinstance(r.get("id"), int)]
                if ids:
                    nxt = max(ids) + 1
                    run.assets |= {base, quote}
                from_id[sym] = nxt  # 0 = geprüft, noch ohne Trades
                if len(rows) < TRADE_LIMIT or not ids:
                    break
                if run.pair_requests >= MAX_PAIR_REQUESTS or not api.budget_left():
                    return last  # Paar unvollständig – nächster Lauf ab ``fromId`` desselben Paars
            checked.add(sym)
            last = sym
        return None


class _Run:
    """Zeitfenster-Streams eines Laufs; der Abrufstand je Stream rückt nach jedem vollständigen Fenster vor."""

    def __init__(self, api: _Api, streams: dict[str, int], launch: int, events: list[K.SourceEvent],
                 assets: set[str], skipped: Counter[str], res: K.FetchResult) -> None:
        self.api, self.streams, self.launch = api, streams, launch
        self.events, self.assets, self.skipped, self.res = events, assets, skipped, res
        self.now = _now_ms()
        self.pair_requests = 0

    def _add(self, ev: K.SourceEvent | None) -> None:
        if ev is not None:
            self.events.append(ev)
            for ln in ev.lines:
                self.assets |= {s for s in (ln.out_sym, ln.in_sym, ln.fee_sym) if s}

    def _windows(self, name: str, span: int, fetch: Callable[[int, int], int | None]) -> None:
        """``fetch(a, b)`` holt ein Fenster und liefert den frühesten offenen Vorgang (oder None)."""
        start = self.streams.get(name, self.launch)
        hold: int | None = None
        a = start
        while a < self.now:
            b = min(a + span, self.now)
            h = fetch(a, b)
            if h is not None and h >= self.now - HOLD_MAX_MS:  # ältere „offene“ Vorgänge halten nicht ewig auf
                hold = h if hold is None else min(hold, h)
            self.streams[name] = _next(b, hold, start)
            a = b

    def deposits(self, name: str) -> None:
        def window(a: int, b: int) -> int | None:
            hold, offset = None, 0
            while True:
                rows = self.api.get("/sapi/v1/capital/deposit/hisrec", {"startTime": a, "endTime": b,
                                                                        "offset": offset, "limit": 1000},
                                    "Einzahlungen")
                rows = rows if isinstance(rows, list) else []
                for r in rows:
                    ev, pending = _deposit(r, self.skipped)
                    hold = _min(hold, pending)
                    self._add(ev)
                if len(rows) < 1000:
                    return hold
                offset += len(rows)
        self._windows(name, WIN_DEPOSIT, window)

    def withdrawals(self, name: str) -> None:
        def window(a: int, b: int) -> int | None:
            hold, offset = None, 0
            while True:
                rows = self.api.get("/sapi/v1/capital/withdraw/history", {"startTime": a, "endTime": b,
                                                                          "offset": offset, "limit": 1000},
                                    "Auszahlungen")
                rows = rows if isinstance(rows, list) else []
                for r in rows:
                    ev, pending = _withdrawal(r, self.skipped)
                    hold = _min(hold, pending)
                    self._add(ev)
                if len(rows) < 1000:
                    return hold
                offset += len(rows)
        self._windows(name, WIN_DEPOSIT, window)

    def _split(self, a: int, b: int, page: Callable[[int, int], tuple[list[Any], bool]],
               add: Callable[[Any], None], what: str) -> None:
        """Fenster abrufen; liefert die API „mehr“ (ohne dokumentiertes Blättern), wird das Fenster halbiert."""
        todo = [(a, b)]
        while todo:
            x, y = todo.pop(0)
            rows, more = page(x, y)
            if more and y - x > 60_000:
                mid = x + (y - x) // 2
                todo[:0] = [(x, mid), (mid, y)]
                continue
            if more:
                self.res.gaps.append(f"{what} {_day(x)}: zu viele Vorgänge in einer Minute – nicht vollständig")
            for r in rows:
                add(r)

    def dividends(self, name: str) -> None:
        def page(a: int, b: int) -> tuple[list[Any], bool]:
            body = self.api.get("/sapi/v1/asset/assetDividend", {"startTime": a, "endTime": b, "limit": DIV_LIMIT},
                                "Ausschüttungen")
            rows = body.get("rows") if isinstance(body, dict) else None
            rows = rows if isinstance(rows, list) else []
            return rows, len(rows) >= DIV_LIMIT

        def window(a: int, b: int) -> None:
            self._split(a, b, page, lambda r: self._add(_dividend(r, self.skipped)), "Ausschüttungen")
        self._windows(name, WIN_DIVIDEND, window)

    def converts(self, name: str) -> None:
        def page(a: int, b: int) -> tuple[list[Any], bool]:
            body = self.api.get("/sapi/v1/convert/tradeFlow", {"startTime": a, "endTime": b, "limit": 1000},
                                "Convert")
            rows = body.get("list") if isinstance(body, dict) else None
            rows = rows if isinstance(rows, list) else []
            return rows, bool(body.get("moreData")) if isinstance(body, dict) else False

        def window(a: int, b: int) -> None:
            self._split(a, b, page, lambda r: self._add(_convert(r, self.skipped)), "Convert")
        self._windows(name, WIN_CONVERT, window)

    def dust(self, name: str) -> None:
        body = self.api.get("/sapi/v1/asset/dribblet", {"startTime": self.streams.get(name, self.launch),
                                                        "endTime": self.now}, "Staubumtausch")
        rows = body.get("userAssetDribblets") if isinstance(body, dict) else None
        rows = rows if isinstance(rows, list) else []
        total = body.get("total") if isinstance(body, dict) else None
        if isinstance(total, int) and total > len(rows):
            self.res.gaps.append(f"Staubumtausch: Binance liefert nur die letzten 100 Vorgänge ({total} insgesamt)")
        for r in rows:
            self._add(_dust(r, self.skipped))
        self.streams[name] = _next(self.now, None, 0)

    def fiat_payments(self, name: str) -> None:
        self._fiat(name, "/sapi/v1/fiat/payments", "0" if name.endswith("buy") else "1", _fiat_payment)

    def fiat_orders(self, name: str) -> None:
        self._fiat(name, "/sapi/v1/fiat/orders", "0" if name == "fiat_dep" else "1", _fiat_order)

    def _fiat(self, name: str, path: str, ttype: str, parse: Callable[..., Any]) -> None:
        def window(a: int, b: int) -> int | None:
            hold, page = None, 1
            while True:
                body = self.api.get(path, {"transactionType": ttype, "beginTime": a, "endTime": b, "page": page,
                                           "rows": FIAT_ROWS}, "Fiat-Vorgänge")
                rows = body.get("data") if isinstance(body, dict) else None
                rows = rows if isinstance(rows, list) else []
                for r in rows:
                    ev, pending = parse(r, ttype, self.skipped)
                    hold = _min(hold, pending)
                    self._add(ev)
                if len(rows) < FIAT_ROWS:
                    return hold
                page += 1
        self._windows(name, WIN_FIAT, window)


PHASE_A = ("deposit", "fiatpay_buy", "fiatpay_sell", "dividend", "dust")
PHASE_C = ("withdraw", "convert", "fiat_dep", "fiat_wd")
_STREAM_FN = {"deposit": "deposits", "fiatpay_buy": "fiat_payments", "fiatpay_sell": "fiat_payments",
              "dividend": "dividends", "dust": "dust", "withdraw": "withdrawals", "convert": "converts",
              "fiat_dep": "fiat_orders", "fiat_wd": "fiat_orders"}
_STREAM_LABEL = {"deposit": "Einzahlungen", "fiatpay_buy": "Käufe mit Karte/Bank", "fiatpay_sell": "Verkäufe an "
                 "Karte/Bank", "dividend": "Ausschüttungen", "dust": "Staubumtausch", "withdraw": "Auszahlungen",
                 "convert": "Convert", "fiat_dep": "Fiat-Einzahlungen", "fiat_wd": "Fiat-Auszahlungen"}


def _next(end: int, hold: int | None, start: int) -> int:
    """Fortsetzungspunkt: nie über offene (ausstehende) Vorgänge hinaus; mit Überlappung, nicht hinter ``start``."""
    point = end if hold is None else min(end, hold)
    return max(point - OVERLAP_MS, min(start, point), 0)


def _min(a: int | None, b: int | None) -> int | None:
    return b if a is None else a if b is None else min(a, b)


def _day(ms: int) -> str:
    d = _ms(ms)
    return d.strftime("%d.%m.%Y") if d else "?"


def _balances(acc: Any) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for b in (acc or {}).get("balances") or [] if isinstance(acc, dict) else []:
        if not isinstance(b, dict):
            continue
        a = _sym(b.get("asset"))
        q = (_dec(b.get("free")) or ZERO) + (_dec(b.get("locked")) or ZERO)
        if a and q:
            out[a] = q
    return out


# ----------------------------------------------------------------------------------------------------
# Abbildung
# ----------------------------------------------------------------------------------------------------

def _raw(r: dict[str, Any], kind: str) -> dict[str, Any]:
    return {"source": PREFIX, "kind": kind, "parser": PARSER_VERSION,
            **{k: (str(v) if isinstance(v, Decimal) else v) for k, v in r.items()
               if k not in ("address", "addressTag", "sourceAddress") and not isinstance(v, (dict, list))}}


def _trade(r: Any, sym: str, base: str, quote: str, skipped: Counter[str]) -> K.SourceEvent | None:
    if not isinstance(r, dict):
        return None
    qty, quote_qty = _dec(r.get("qty")), _dec(r.get("quoteQty"))
    ts, tid = _ms(r.get("time")), r.get("id")
    if qty is None or quote_qty is None or ts is None or not isinstance(tid, int):
        skipped["Trades mit unlesbaren Angaben"] += 1
        return None
    buyer = r.get("isBuyer") is True
    rec = Rec(line=0, ts=ts, kind=M.TRADE, label=f"{'Kauf' if buyer else 'Verkauf'} {base}/{quote}",
              raw=_raw(r, "trade"))
    if buyer:
        rec.out_sym, rec.out_qty, rec.in_sym, rec.in_qty = quote, quote_qty, base, qty
    else:
        rec.out_sym, rec.out_qty, rec.in_sym, rec.in_qty = base, qty, quote, quote_qty
    fee, fee_sym = _dec(r.get("commission")), _sym(r.get("commissionAsset"))
    if fee and fee_sym:
        rec.fee_sym, rec.fee_qty, rec.fee_basis = fee_sym, fee, "extra"
        rec.note = f"Gebühr {_q(fee)} {fee_sym} zusätzlich (Binance commission)"
    if quote in ("EUR",):
        rec.value, rec.value_ccy = quote_qty, "EUR"
    rec.ext_id = f"{sym}:{tid}"
    return _event(f"trade:{sym}:{tid}", ts, [rec], rec.label or "Trade")


def _deposit(r: Any, skipped: Counter[str]) -> tuple[K.SourceEvent | None, int | None]:
    if not isinstance(r, dict):
        return None, None
    status, ts = r.get("status"), _ms(r.get("insertTime"))
    coin, qty, rid = _sym(r.get("coin")), _dec(r.get("amount")), r.get("id")
    if status in DEPOSIT_OPEN:
        skipped["Einzahlungen noch ausstehend (folgen im nächsten Lauf)"] += 1
        return None, int(r.get("insertTime") or 0) or None
    if status not in DEPOSIT_DONE:
        skipped["Einzahlungen abgelehnt bzw. fehlerhaft (nicht gebucht)"] += 1
        return None, None
    if not coin or not qty or ts is None or rid in (None, ""):
        skipped["Einzahlungen mit unlesbaren Angaben"] += 1
        return None, None
    tx = str(r.get("txId") or "")
    rec = Rec(line=0, ts=ts, kind=M.DEPOSIT, in_sym=coin, in_qty=qty, label=f"Einzahlung {coin}",
              raw=_raw(r, "deposit"), note=f"Netzwerk {r.get('network')}" if r.get("network") else None)
    if _HEXHASH.match(tx):
        rec.txhash = tx.lower() if tx.startswith("0x") else tx
    if status == 6:
        rec.note = ((rec.note + " · ") if rec.note else "") + "gutgeschrieben, noch nicht auszahlbar"
    rec.ext_id = str(rid)
    return _event(f"dep:{rid}", ts, [rec], rec.label or "Einzahlung"), None


def _withdrawal(r: Any, skipped: Counter[str]) -> tuple[K.SourceEvent | None, int | None]:
    if not isinstance(r, dict):
        return None, None
    status = r.get("status")
    ts = _utc_text(r.get("completeTime")) or _utc_text(r.get("applyTime"))
    coin, qty, rid = _sym(r.get("coin")), _dec(r.get("amount")), r.get("id")
    if status in WITHDRAW_OPEN:
        skipped["Auszahlungen noch in Bearbeitung (folgen im nächsten Lauf)"] += 1
        apply = _utc_text(r.get("applyTime"))
        return None, int(apply.timestamp() * 1000) if apply else None
    if status not in WITHDRAW_DONE:
        skipped["Auszahlungen abgelehnt (nicht gebucht)"] += 1
        return None, None
    if not coin or not qty or ts is None or not rid:
        skipped["Auszahlungen mit unlesbaren Angaben"] += 1
        return None, None
    rec = Rec(line=0, ts=ts, kind=M.WITHDRAWAL, out_sym=coin, out_qty=qty, label=f"Auszahlung {coin}",
              raw=_raw(r, "withdrawal"))
    fee = _dec(r.get("transactionFee"))
    if fee:
        rec.fee_sym, rec.fee_qty, rec.fee_basis = coin, fee, "open"
        rec.review = (f"Auszahlungsgebühr {_q(fee)} {coin}: ob „amount“ sie enthält, dokumentiert Binance nicht – mit "
                      "dem Eingang auf der Zieladresse vergleichen")
    tx = str(r.get("txId") or "")
    if _HEXHASH.match(tx):
        rec.txhash = tx.lower() if tx.startswith("0x") else tx
    rec.note = "Zeit laut Binance ohne Zonenangabe – als UTC gelesen" + (f" · Netzwerk {r.get('network')}"
                                                                         if r.get("network") else "")
    rec.ext_id = str(rid)
    return _event(f"wd:{rid}", ts, [rec], rec.label or "Auszahlung"), None


def _dividend(r: Any, skipped: Counter[str]) -> K.SourceEvent | None:
    if not isinstance(r, dict):
        return None
    asset, qty, ts = _sym(r.get("asset")), _dec(r.get("amount")), _ms(r.get("divTime"))
    rid = r.get("tranId") or r.get("id")
    if not asset or not qty or ts is None or rid in (None, ""):
        skipped["Ausschüttungen mit unlesbaren Angaben"] += 1
        return None
    info = str(r.get("enInfo") or "")
    tag = _tag_for(info)
    direction = r.get("direction")
    if qty < 0 or (isinstance(direction, int) and direction < 0):
        rec = Rec(line=0, ts=ts, kind=M.WITHDRAWAL, out_sym=asset, out_qty=abs(qty), tag="cost",
                  label=f"Ausschüttung (Abgang) {asset}", raw=_raw(r, "dividend"),
                  review=f"Ausschüttungsbuchung mit Richtung „Abgang“ ({info or 'ohne Text'}) – Art prüfen")
    else:
        rec = Rec(line=0, ts=ts, kind=M.DEPOSIT, in_sym=asset, in_qty=qty, tag=tag or "other_income",
                  label=info or f"Ausschüttung {asset}", raw=_raw(r, "dividend"))
        if tag is None:
            rec.review = f"Ausschüttung „{info or 'ohne Text'}“ – Ertragsart prüfen"
    rec.ext_id = str(rid)
    return _event(f"div:{rid}", ts, [rec], rec.label or "Ausschüttung")


def _dust(r: Any, skipped: Counter[str]) -> K.SourceEvent | None:
    if not isinstance(r, dict):
        return None
    tid, ts = r.get("transId"), _ms(r.get("operateTime"))
    details = [d for d in r.get("userAssetDribbletDetails") or [] if isinstance(d, dict)]
    if tid in (None, "") or ts is None or not details:
        skipped["Staubumtausch mit unlesbaren Angaben"] += 1
        return None
    recs: list[Rec] = []
    for d in sorted(details, key=lambda x: str(x.get("fromAsset") or "")):
        src, amt = _sym(d.get("fromAsset")), _dec(d.get("amount"))
        tgt, got = "BNB", _dec(d.get("transferedAmount"))  # Staubumtausch laut Doku immer in BNB
        fee = _dec(d.get("serviceChargeAmount")) or ZERO
        if not src or amt is None or got is None:
            skipped["Staubumtausch-Teile unlesbar"] += 1
            continue
        rec = Rec(line=0, ts=ts, kind=M.TRADE, out_sym=src, out_qty=amt, in_sym=tgt, in_qty=got,
                  label=f"Staubumtausch {src} → {tgt}", raw=_raw(d, "dust"))
        if fee:
            rec.note = f"Servicegebühr {_q(fee)} {tgt}"
            rec.review = (f"Staubumtausch: ob „transferedAmount“ die Servicegebühr {_q(fee)} {tgt} bereits abzieht, "
                          "dokumentiert Binance nicht – gutgeschriebene Menge prüfen")
        recs.append(rec)
    if not recs:
        return None
    return _event(f"dust:{tid}", ts, recs, f"Staubumtausch ({len(recs)} Assets)")


def _convert(r: Any, skipped: Counter[str]) -> K.SourceEvent | None:
    if not isinstance(r, dict):
        return None
    if str(r.get("orderStatus") or "").upper() != "SUCCESS":
        skipped["Convert nicht ausgeführt"] += 1
        return None
    src, dst = _sym(r.get("fromAsset")), _sym(r.get("toAsset"))
    a, b, ts, oid = _dec(r.get("fromAmount")), _dec(r.get("toAmount")), _ms(r.get("createTime")), r.get("orderId")
    if not src or not dst or a is None or b is None or ts is None or oid in (None, ""):
        skipped["Convert mit unlesbaren Angaben"] += 1
        return None
    rec = Rec(line=0, ts=ts, kind=M.TRADE, out_sym=src, out_qty=a, in_sym=dst, in_qty=b, label=f"Convert {src} → {dst}",
              raw=_raw(r, "convert"), note="Convert (Gebühr im Kurs enthalten)")
    if src == "EUR":
        rec.value, rec.value_ccy = a, "EUR"
    elif dst == "EUR":
        rec.value, rec.value_ccy = b, "EUR"
    rec.ext_id = str(oid)
    return _event(f"convert:{oid}", ts, [rec], rec.label or "Convert")


def _fiat_status(r: dict[str, Any]) -> str:
    return str(r.get("status") or "").strip().lower()


def _fiat_payment(r: Any, ttype: str, skipped: Counter[str]) -> tuple[K.SourceEvent | None, int | None]:
    if not isinstance(r, dict):
        return None, None
    st, ts, no = _fiat_status(r), _ms(r.get("createTime")), str(r.get("orderNo") or "")
    if st in FIAT_OPEN:
        return None, int(r.get("createTime") or 0) or None
    if st not in FIAT_DONE:
        skipped[f"Fiat-Käufe/-Verkäufe mit Status „{st or '?'}“ (nicht gebucht)"] += 1
        return None, None
    fiat, crypto = _sym(r.get("fiatCurrency")), _sym(r.get("cryptoCurrency"))
    src, got, fee = _dec(r.get("sourceAmount")), _dec(r.get("obtainAmount")), _dec(r.get("totalFee"))
    if not fiat or not crypto or src is None or got is None or ts is None or not no:
        skipped["Fiat-Käufe/-Verkäufe mit unlesbaren Angaben"] += 1
        return None, None
    buy = ttype == "0"
    # Kauf: sourceAmount in Fiat → obtainAmount Krypto; Verkauf: sourceAmount Krypto → obtainAmount Fiat (Annahme
    # nach Feldnamen; Bestandsprüfung zeigt Abweichungen)
    via = r.get("paymentMethod") or "Fiat"
    rec = Rec(line=0, ts=ts, kind=M.TRADE, label=f"{'Kauf' if buy else 'Verkauf'} {crypto} ({via})",
              raw=_raw(r, "fiat_payment"))
    if buy:
        rec.out_sym, rec.out_qty, rec.in_sym, rec.in_qty = fiat, src, crypto, got
    else:
        rec.out_sym, rec.out_qty, rec.in_sym, rec.in_qty = crypto, src, fiat, got
    if fiat == "EUR":
        rec.value, rec.value_ccy = (src if buy else got), "EUR"
    if fee:
        rec.fee_sym, rec.fee_qty, rec.fee_basis = fiat, fee, "open"
        rec.review = f"Gebühr {_q(fee)} {fiat}: ob der Betrag sie enthält, dokumentiert Binance nicht – Beleg prüfen"
    rec.ext_id = no
    return _event(f"fiatpay:{no}", ts, [rec], rec.label or "Fiat-Kauf"), None


def _fiat_order(r: Any, ttype: str, skipped: Counter[str]) -> tuple[K.SourceEvent | None, int | None]:
    if not isinstance(r, dict):
        return None, None
    st, ts, no = _fiat_status(r), _ms(r.get("createTime")), str(r.get("orderNo") or "")
    if st in FIAT_OPEN:
        return None, int(r.get("createTime") or 0) or None
    if st not in FIAT_DONE:
        skipped[f"Fiat-Ein-/Auszahlungen mit Status „{st or '?'}“ (nicht gebucht)"] += 1
        return None, None
    fiat, amt, fee = _sym(r.get("fiatCurrency")), _dec(r.get("amount")), _dec(r.get("totalFee"))
    if not fiat or amt is None or ts is None or not no:
        skipped["Fiat-Ein-/Auszahlungen mit unlesbaren Angaben"] += 1
        return None, None
    dep = ttype == "0"
    rec = Rec(line=0, ts=ts, kind=M.DEPOSIT if dep else M.WITHDRAWAL, label=f"Fiat-{'Ein' if dep else 'Aus'}zahlung "
              f"{fiat}" + (f" ({r.get('method')})" if r.get("method") else ""), raw=_raw(r, "fiat_order"))
    if dep:
        rec.in_sym, rec.in_qty = fiat, amt
    else:
        rec.out_sym, rec.out_qty = fiat, amt
    if fee:
        rec.fee_sym, rec.fee_qty, rec.fee_basis = fiat, fee, "open"
        rec.review = (f"Gebühr {_q(fee)} {fiat}: ob „amount“ sie enthält, dokumentiert Binance nicht – mit dem "
                      "Bankbeleg vergleichen")
    rec.ext_id = no
    return _event(f"fiat:{no}", ts, [rec], rec.label or "Fiat"), None


