"""Bitpanda – Connector für die Bitpanda Public API (ausschließlich lesend).

Schnittstelle
    Basis ``https://api.public.bitpanda.com/v1``, Authentifizierung per Header ``x-api-key``, nur ``GET``:

    ========================  ======================================  =============================================
    Endpunkt                  Zweck                                   Leserecht des API-Keys
    ========================  ======================================  =============================================
    ``/operations``           Vorgänge (Historie und inkrementell)    „Transaction“ – erforderlich
    ``/assets?id=<uuid>``     Asset-Stammdaten (Symbol, Typ, ISIN)    wird bei „Verbindung testen“ ermittelt
    ``/currencies``           Fiat-Währungen (ID → Code)              wird bei „Verbindung testen“ ermittelt
    ``/portfolio/holdings``   Bestände – nur Plausibilitätsprüfung    „Balance“ – optional
    ========================  ======================================  =============================================

    Keine schreibenden Aufrufe (RFQ/Trade, Earn) und kein stiller Rückgriff auf die ältere API
    ``api.bitpanda.com``. Umleitungen werden nicht verfolgt (der Schlüssel geht nur an den dokumentierten Host).

Robustheit
    Beträge ausschließlich als ``Decimal`` (JSON mit ``parse_float=Decimal``), Zeitpunkte in UTC, Timeouts,
    ``429`` mit ``Retry-After`` (begrenztes Wartebudget), vorübergehende Fehler mit wenigen Wiederholungen,
    Cursor-Pagination mit Schleifen- und Seitenendeschutz. Ist das Seitenende nicht eindeutig erkennbar oder bricht
    der Abruf ab, gilt er als unvollständig: Status „teilweise“, der Abrufstand rückt nicht vor.

Abbildung – nur eindeutige Fälle, sonst „ungeklärt“ mit Grund (nie geraten, nie still verworfen)
    * Kauf: ein Fiat-Ausgang + ein Krypto-Eingang (auch Sparplan) · Verkauf: Krypto-Ausgang + Fiat-Eingang
    * Einzahlung/Auszahlung: ein Eingang bzw. Ausgang (Fiat oder Krypto) mit Vorgangsart „deposit“/„withdraw…“
    * Rewards/Staking: ein Krypto-Eingang mit Vorgangsart genau „reward“/„staking reward“ → Zugang mit Ertrags-Tag
    * Gebühren: eigene Gebühren-Teile (Transaktionsart „fee“) → Gebührenzeile; Gebühren an Haupt-Teilen werden
      übernommen, aber als prüfbedürftig markiert (ob der Betrag sie enthält, ist nicht dokumentiert)
    * interne Umbuchungen (gleiches Asset, gleicher Betrag, Ein- und Ausgang) → ohne Buchung, gezählt
    * ungeklärt: Korrekturen/Stornos (``compensates``) samt storniertem Vorgang, Tausch Krypto↔Krypto, Aktien/ETFs,
      Edelmetalle, Indizes, unbekannte Assets oder Vorgangsarten, fehlende Richtung oder Zeitangabe
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any, ClassVar

import httpx

from app import __version__
from app.csvimport import model as M
from app.csvimport.events import uuid_key
from app.csvimport.model import Rec
from app.datasources import connector as K
from app.datasources.catalog import MemoryCatalog

log = logging.getLogger(__name__)

BASE_URL = "https://api.public.bitpanda.com/v1"
PREFIX = "bitpanda"
PAGE_SIZE = 100
MAX_PAGES = 2000
OVERLAP = timedelta(days=2)  # spät gutgeschriebene Vorgänge – bekannte werden erkannt
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
RETRIES = 3
WAIT_BUDGET_S = 120.0
MAX_RETRY_AFTER_S = 60.0
COMMON_PAGE_SIZES = frozenset({10, 20, 25, 50, 100, 200, 250, 500, 1000})
REWARD_TYPES = {"reward", "rewards", "stakingreward", "stakingrewards"}
_TRADE_WORDS = ("buy", "sell", "trade", "order", "saving", "instant")
_NOT_TRADE = ("reward", "staking", "deposit", "withdraw", "transfer", "airdrop", "fee", "bonus", "interest")
_CORRECTION = ("cancel", "revers", "compensat", "refund", "chargeback", "storno", "correct")
_INCOME_WORDS = ("reward", "staking", "airdrop", "bonus", "interest")


# ----------------------------------------------------------------------------------------------------
# Hilfen: JSON, Zeit, Beträge
# ----------------------------------------------------------------------------------------------------

def _body(resp: httpx.Response) -> Any:
    try:
        return json.loads(resp.content or b"null", parse_float=Decimal)
    except ValueError:
        return None


def _get(d: Any, *names: str) -> Any:
    if not isinstance(d, dict):
        return None
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    return None


def _dec(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = v if isinstance(v, Decimal) else Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _ts(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, int | Decimal) and not isinstance(v, bool):
        n = float(v)
        return datetime.fromtimestamp(n / 1000 if n > 1e11 else n, UTC)
    s = str(v).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC)


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _s(v: Any) -> str | None:
    if v is None:
        return None
    return format(v, "f") if isinstance(v, Decimal) else str(v)


# ----------------------------------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------------------------------

def _error_text(body: Any) -> str:
    if isinstance(body, dict):
        errs = body.get("errors")
        if isinstance(errs, list) and errs and isinstance(errs[0], dict):
            e = errs[0]
            return " – ".join(str(x) for x in (e.get("code"), e.get("title") or e.get("detail")) if x)
        for k in ("message", "error", "detail", "title"):
            if isinstance(body.get(k), str):
                return str(body[k])
    return ""


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


def _http_error(resp: httpx.Response, what: str) -> K.ConnectorError:
    code = resp.status_code
    text = _error_text(_body(resp))[:160]
    low = text.lower()
    if code == 401:
        if "expir" in low or "abgelaufen" in low:
            return K.ConnectorError("expired", "Bitpanda meldet den API-Key als abgelaufen – neuen Schlüssel erstellen "
                                               "und unter „API-Key ersetzen“ eintragen.")
        return K.ConnectorError("auth", f"Bitpanda lehnt den API-Key für {what} ab (HTTP 401"
                                        + (f": {text}" if text else "") + ") – ungültig, widerrufen oder abgelaufen; "
                                        "bei gültigem Schlüssel fehlt das Leserecht.")
    if code == 403:
        return K.ConnectorError("scope", f"Bitpanda verweigert den Zugriff auf {what} (HTTP 403"
                                         + (f": {text}" if text else "") + ") – dem API-Key fehlt das Leserecht.")
    if code == 429:
        ra = _retry_after(resp)
        return K.ConnectorError("rate_limit", "Bitpanda drosselt Anfragen (HTTP 429).",
                                retry_after_s=int(ra) if ra else 60)
    if code >= 500:
        return K.ConnectorError("unavailable", f"Bitpanda ist vorübergehend gestört (HTTP {code}).")
    if 300 <= code < 400:
        return K.ConnectorError("data", f"Bitpanda leitet {what} um (HTTP {code}) – aus Sicherheitsgründen nicht "
                                        "gefolgt.")
    return K.ConnectorError("data", f"Bitpanda antwortete bei {what} mit HTTP {code}" + (f": {text}" if text else "")
                            + ".")


class _Api:
    """GET mit Wiederholungen: 429 (Retry-After, Wartebudget), 5xx und Zeitüberschreitung (wenige Versuche)."""

    def __init__(self, key: str, client: httpx.Client, sleep: Callable[[float], None],
                 budget_s: float = WAIT_BUDGET_S) -> None:
        self._key = key
        self.client = client
        self.sleep = sleep
        self.budget = budget_s
        self.waited = 0.0
        self.requests = 0
        self.throttled = 0

    def __repr__(self) -> str:
        return f"_Api(requests={self.requests})"

    def _wait(self, s: float) -> bool:
        if self.waited + s > self.budget:
            return False
        self.waited += s
        self.sleep(s)
        return True

    def get(self, path: str, params: dict[str, Any] | None, what: str) -> Any:
        attempt = 0
        while True:
            attempt += 1
            self.requests += 1
            try:
                resp = self.client.get(path, params=params, headers={"x-api-key": self._key})
            except httpx.TimeoutException:
                if attempt >= RETRIES or not self._wait(2.0 * attempt):
                    raise K.ConnectorError("unavailable", f"Zeitüberschreitung beim Abruf von {what} – der nächste "
                                                          "Lauf versucht es erneut.") from None
                continue
            except httpx.TransportError:
                if attempt >= RETRIES or not self._wait(2.0 * attempt):
                    raise K.ConnectorError("unavailable", "Bitpanda nicht erreichbar (Netzwerk/DNS) – der nächste "
                                                          "Lauf versucht es erneut.") from None
                continue
            code = resp.status_code
            if code == 429:
                self.throttled += 1
                ra = _retry_after(resp)
                wait = ra if ra is not None else 5.0 * attempt
                # nie früher als verlangt erneut fragen: längere Wartezeiten übernimmt der Zeitplan (retry_after_s)
                if wait > MAX_RETRY_AFTER_S or attempt >= RETRIES + 2 or not self._wait(wait):
                    raise _http_error(resp, what)
                continue
            if code >= 500 and attempt < RETRIES and self._wait(2.0 * attempt):
                continue
            if code >= 300:
                raise _http_error(resp, what)
            body = _body(resp)
            if body is None:
                raise K.ConnectorError("data", f"Antwort von {what} ist kein gültiges JSON.")
            return body


def _items(body: Any) -> list[Any] | None:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for k in ("data", "items", "results", "operations"):
            v = body.get(k)
            if isinstance(v, list):
                return v
    return None


def _next(body: Any) -> tuple[str | None, bool]:
    """(nächster Cursor, Pagination eindeutig erkannt). Dokumentiert: ``cursor`` in der Antwort, fehlt am Ende."""
    if not isinstance(body, dict):
        return None, False
    for scope in (body, body.get("meta"), body.get("pagination"), body.get("page")):
        if not isinstance(scope, dict):
            continue
        for k in ("next_cursor", "nextCursor", "cursor"):
            if k in scope:
                v = scope.get(k)
                return (v if isinstance(v, str) and v else None), True
        if "has_next_page" in scope or "hasNextPage" in scope:
            has = scope.get("has_next_page", scope.get("hasNextPage"))
            end = scope.get("end_cursor") or scope.get("endCursor")
            if has is True and isinstance(end, str) and end:
                return end, True
            return None, has is False
    return None, False


# ----------------------------------------------------------------------------------------------------
# Vorgänge → Zeilen
# ----------------------------------------------------------------------------------------------------

@dataclass
class _Leg:
    id: str | None
    side: str  # in | out | ?
    amount: Decimal | None
    fee: Decimal
    ref: str | None  # Asset- bzw. Währungs-UUID
    kind: str  # fiat | crypto | security | metal | index | unknown
    symbol: str | None
    trade_id: str | None
    compensates: str | None
    ttype: str
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_fee(self) -> bool:
        return "fee" in self.ttype


def _asset_kind(meta: dict[str, Any] | None) -> str:
    if not meta:
        return "unknown"
    if meta.get("kind") == "currency":
        return "fiat"
    t = f"{meta.get('type') or ''} {meta.get('group') or ''}".lower()
    if "fiat" in t:
        return "fiat"
    if any(w in t for w in ("stock", "etf", "etc", "security", "equity", "share", "fund")):
        return "security"
    if any(w in t for w in ("metal", "commodit", "gold", "silver", "platin", "palladium")):
        return "metal"
    if "index" in t:
        return "index"
    if any(w in t for w in ("crypto", "coin", "token")):
        return "crypto"
    return "unknown"


_KIND_TEXT = {"security": "Aktie/ETF (Bitpanda Stocks)", "metal": "Edelmetall", "index": "Kryptoindex",
              "unknown": "unbekanntes Asset"}


@dataclass
class _Op:
    id: str | None
    type: str
    ts: datetime | None
    legs: list[_Leg]
    raw: dict[str, Any]

    @property
    def key(self) -> str:
        k = uuid_key(PREFIX, self.id) if self.id else None
        if k:
            return k
        if self.id and re.fullmatch(r"[A-Za-z0-9._:-]{1,150}", self.id):
            return f"{PREFIX}:{self.id}"
        digest = hashlib.sha256(json.dumps(self.raw, sort_keys=True, default=str).encode()).hexdigest()[:24]
        return f"{PREFIX}:h{digest}"

    def aliases(self) -> list[str]:
        vals = [self.id, *(leg.id for leg in self.legs), *(leg.trade_id for leg in self.legs)]
        return sorted({k for v in vals if v and (k := uuid_key(PREFIX, v))})


class _Mapper:
    def __init__(self, compensated: set[str]) -> None:
        self.compensated = compensated

    def event(self, op: _Op) -> tuple[K.SourceEvent | None, str | None]:
        """(Ereignis, Grund ohne Buchung)."""
        ts = op.ts or datetime.now(UTC)
        label = f"Bitpanda: {op.type or 'Vorgang'}"
        raw = op.raw

        def rec(kind: str, **kw: Any) -> Rec:
            return Rec(line=0, ts=ts, kind=kind, label=label, aliases=op.aliases(), raw=raw, **kw)

        def review(reason: str) -> K.SourceEvent:
            ins = [lg for lg in op.legs if lg.side == "in"]
            outs = [lg for lg in op.legs if lg.side == "out"]
            r = rec(M.REVIEW, note=f"Ungeklärt: {reason}",
                    in_sym=ins[0].symbol if ins else None, in_qty=ins[0].amount if ins else None,
                    out_sym=outs[0].symbol if outs else None, out_qty=outs[0].amount if outs else None)
            return K.SourceEvent(op.key, ts, [r], label)

        otype = _norm(op.type)
        legs = [lg for lg in op.legs if (lg.amount or 0) != 0 or lg.fee]
        if op.ts is None:
            return review("Zeitpunkt fehlt in den API-Daten"), None
        if not legs:
            return review("Vorgang ohne Beträge"), None
        comp = sorted({lg.compensates for lg in legs if lg.compensates})
        if comp or any(w in otype for w in _CORRECTION):
            refs = ", ".join(comp[:2]) + (" …" if len(comp) > 2 else "")
            return review("Korrektur/Storno" + (f" zu {refs}" if refs else "") + " – wird nicht automatisch "
                          "gebucht; eine bereits übernommene ursprüngliche Buchung bitte prüfen"), None
        own = {x for x in (op.id, *(lg.id for lg in legs)) if x}
        if own & self.compensated:
            return review("durch eine spätere Korrektur storniert – wird nicht automatisch gebucht"), None
        if any(lg.side == "?" for lg in legs):
            return review("Richtung (Ein- oder Ausgang) nicht angegeben"), None
        special = sorted({lg.kind for lg in legs if lg.kind in _KIND_TEXT})
        if special:
            what = ", ".join(_KIND_TEXT[k] for k in special)
            return review(f"{what} – Asset und steuerliche Einordnung nicht eindeutig; bitte prüfen oder per "
                          "CSV/manuell erfassen"), None
        fees = [lg for lg in legs if lg.is_fee]
        main = [lg for lg in legs if not lg.is_fee]
        ins = [lg for lg in main if lg.side == "in"]
        outs = [lg for lg in main if lg.side == "out"]
        fee_lines = [rec(M.FEE, fee_sym=lg.symbol, fee_qty=abs(lg.amount or lg.fee)) for lg in
                     sorted(fees, key=lambda x: (x.symbol or "", x.id or ""))]
        if len(ins) == 1 and len(outs) == 1 and ins[0].ref == outs[0].ref and ins[0].amount == outs[0].amount \
                and not fees and not ins[0].fee and not outs[0].fee:
            return None, "interne Umbuchung zwischen Bitpanda-Wallets"

        def with_fee(r: Rec, *parts: _Leg) -> Rec:
            charged = [lg for lg in parts if lg.fee]
            if len(charged) > 1:
                r.review = "Gebühren an mehreren Teilen – Zuordnung prüfen"
            if charged:
                lg = charged[0]
                r.fee_sym, r.fee_qty = lg.symbol, lg.fee
                r.review = r.review or (f"Gebühr {_s(lg.fee)} {lg.symbol}: ob der Betrag sie bereits enthält, ist "
                                        "nicht dokumentiert – bitte mit dem Bitpanda-Beleg vergleichen")
            return r

        trade_like = not otype or any(w in otype for w in _TRADE_WORDS)
        conflicting = any(w in otype for w in _NOT_TRADE)
        if len(ins) == 1 and len(outs) == 1:
            i, o = ins[0], outs[0]
            if o.kind == "fiat" and i.kind == "crypto" and trade_like and not conflicting:
                r = with_fee(rec(M.TRADE, out_sym=o.symbol, out_qty=o.amount, in_sym=i.symbol, in_qty=i.amount,
                                 value=o.amount, value_ccy=o.symbol,
                                 note="Sparplan" if "saving" in otype else None), o, i)
                return K.SourceEvent(op.key, ts, [r, *fee_lines], label), None
            if o.kind == "crypto" and i.kind == "fiat" and trade_like and not conflicting:
                r = with_fee(rec(M.TRADE, out_sym=o.symbol, out_qty=o.amount, in_sym=i.symbol, in_qty=i.amount,
                                 value=i.amount, value_ccy=i.symbol), o, i)
                return K.SourceEvent(op.key, ts, [r, *fee_lines], label), None
            if o.kind == "crypto" and i.kind == "crypto":
                return review("Tausch Krypto → Krypto: Gegenwert in EUR nicht in den API-Daten – bitte prüfen"), None
            if o.kind == "fiat" and i.kind == "fiat":
                return review("Währungstausch Fiat → Fiat"), None
        if len(ins) == 1 and not outs:
            i = ins[0]
            if otype in REWARD_TYPES and i.kind == "crypto":
                r = with_fee(rec(M.DEPOSIT, in_sym=i.symbol, in_qty=i.amount,
                                 tag="staking" if "staking" in otype else "reward"), i)
                return K.SourceEvent(op.key, ts, [r, *fee_lines], label), None
            if "deposit" in otype and not any(w in otype for w in _INCOME_WORDS):
                r = with_fee(rec(M.DEPOSIT, in_sym=i.symbol, in_qty=i.amount), i)
                return K.SourceEvent(op.key, ts, [r, *fee_lines], label), None
        if len(outs) == 1 and not ins and "withdraw" in otype:
            o = outs[0]
            r = with_fee(rec(M.WITHDRAWAL, out_sym=o.symbol, out_qty=o.amount), o)
            return K.SourceEvent(op.key, ts, [r, *fee_lines], label), None
        if not main and fees:
            return K.SourceEvent(op.key, ts, fee_lines, label), None
        return review(f"Vorgangsart „{op.type or '–'}“ mit {len(ins)} Eingang/Eingängen und {len(outs)} Ausgang/"
                      "Ausgängen – Deutung nicht eindeutig"), None


# ----------------------------------------------------------------------------------------------------
# Connector
# ----------------------------------------------------------------------------------------------------

@K.register
class BitpandaConnector(K.Connector):
    provider = "bitpanda"
    label = "Bitpanda (Public API)"
    needs_credentials = True
    base_url: ClassVar[str] = BASE_URL
    transport: ClassVar[httpx.BaseTransport | None] = None  # nur Tests (anonymisierte Fixtures)
    sleep: ClassVar[Callable[[float], None]] = staticmethod(time.sleep)

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=TIMEOUT, follow_redirects=False,
                            transport=self.transport,
                            headers={"Accept": "application/json",
                                     "User-Agent": f"Portfolia/{__version__} (read-only)"})

    def _catalog(self) -> Any:
        if self.catalog is None:
            self.catalog = MemoryCatalog(PREFIX)
        return self.catalog

    # -- Verbindung prüfen --------------------------------------------------------------------------------
    def check(self, cfg: K.SourceConfig, secret: K.Secret) -> K.CheckResult:
        details: dict[str, Any] = {}
        with self._client() as client:
            api = _Api(secret.reveal(), client, self.sleep, budget_s=20.0)

            def probe(name: str, path: str, params: dict[str, Any] | None, what: str,
                      ok_text: str) -> K.ConnectorError | None:
                for p in ((params, None) if params else (None,)):
                    try:
                        api.get(path, p, what)
                        details[name] = {"ok": True, "text": ok_text}
                        return None
                    except K.ConnectorError as e:
                        if e.kind == "data" and "HTTP 400" in e.message and p:
                            continue  # Parameter nicht akzeptiert → ohne erneut
                        details[name] = {"ok": False, "text": e.message}
                        return e
                return None

            e_ops = probe("transaction", "/operations", {"pageSize": 1}, "Vorgänge (/operations)",
                          "Vorgänge lesbar – Leserecht „Transaction“ vorhanden.")
            e_bal = probe("balances", "/portfolio/holdings", None, "Bestände (/portfolio/holdings)",
                          "Bestände lesbar – Leserecht „Balance“ vorhanden (optional, für die Bestandsprüfung).")
            e_assets = probe("assets", "/assets", {"pageSize": 1}, "Asset-Stammdaten (/assets)",
                             "Asset-Stammdaten lesbar.")
        if e_ops is None:
            msg = "Verbindung in Ordnung – Vorgänge lesbar."
            if e_assets is not None:
                msg += (" Asset-Stammdaten sind nicht abrufbar – betroffene Vorgänge landen als „ungeklärt“ in der "
                        "Prüfung.")
            return K.CheckResult(True, msg, details)
        if e_ops.kind in ("rate_limit", "unavailable"):
            raise e_ops
        if e_ops.kind == "expired":
            return K.CheckResult(False, e_ops.message, details)
        if e_ops.kind == "scope" or (e_ops.kind == "auth" and e_bal is None):
            return K.CheckResult(False, "Der API-Key ist gültig, ihm fehlt aber das Leserecht „Transaction“ – bei "
                                        "Bitpanda einen Schlüssel mit „Transaction“ erstellen und ersetzen.", details)
        return K.CheckResult(False, "Bitpanda lehnt den API-Key ab – ungültig, widerrufen, abgelaufen oder ohne "
                                    "Leserecht „Transaction“.", details)

    # -- Abrufen ------------------------------------------------------------------------------------------
    def rewind(self, cursor: dict[str, Any] | None, before: datetime) -> dict[str, Any] | None:
        if not cursor or not cursor.get("from"):
            return None  # ohnehin vollständiger Abruf
        cur = _ts(cursor.get("from"))
        new = before - OVERLAP
        return {**cursor, "from": _iso_z(min(cur, new) if cur else new)}

    def fetch(self, cfg: K.SourceConfig, secret: K.Secret, cursor: dict[str, Any] | None) -> K.FetchResult:
        started = datetime.now(UTC)
        since = _ts((cursor or {}).get("from"))
        coverage: dict[str, Any] = {"api": BASE_URL, "mode": "inkrementell" if since else "vollständig",
                                    "from": _iso_z(since) if since else None, "to": _iso_z(started)}
        with self._client() as client:
            api = _Api(secret.reveal(), client, self.sleep)
            items, complete, notes = self._operations(api, since, coverage)
            ops = [self._parse(o) for o in items if isinstance(o, dict)]
            if len(ops) != len(items):
                notes.append(f"{len(items) - len(ops)} Einträge ohne erkennbare Struktur übersprungen")
                complete = False
            self._resolve(api, ops, notes)
            events, skipped = self._map(ops)
            if complete and since is None:
                self._balances(api, ops, coverage, notes)
            coverage.update(requests=api.requests, throttled=api.throttled, waited_s=round(api.waited, 1))
        coverage["limits"] = self._limits(events)
        nxt = {"v": 1, "from": _iso_z(started - OVERLAP)} if complete else None
        return K.FetchResult(events=events, cursor=nxt, complete=complete, warnings=notes, skipped=skipped,
                             coverage=coverage)

    def _operations(self, api: _Api, since: datetime | None,
                    coverage: dict[str, Any]) -> tuple[list[Any], bool, list[str]]:
        base: dict[str, Any] = {"pageSize": PAGE_SIZE}
        if since is not None:
            base["from"] = _iso_z(since)
        variants = [base, {k: v for k, v in base.items() if k != "pageSize"}, {}]
        notes: list[str] = []
        items: list[Any] = []
        seen: set[str] = set()
        pages = 0
        complete = True
        params = dict(base)
        requested = PAGE_SIZE
        while True:
            try:
                body = api.get("/operations", params, "Vorgänge (/operations)")
            except K.ConnectorError as e:
                if pages == 0 and e.kind == "data" and "HTTP 400" in e.message and variants:
                    variants.pop(0)
                    if variants and variants[0] != params:
                        params = dict(variants[0])
                        requested = int(params.get("pageSize") or 0)
                        if "from" not in params and since is not None:
                            notes.append("Zeitfilter nicht akzeptiert – vollständiger Abruf (bekannte Vorgänge werden "
                                         "erkannt)")
                            coverage["mode"] = "vollständig"
                        continue
                if pages > 0 and e.kind in ("rate_limit", "unavailable"):
                    notes.append(f"Abruf nach {pages} Seiten abgebrochen: {e.message}")
                    complete = False
                    break
                raise
            page = _items(body)
            if page is None:
                raise K.ConnectorError("data", "Antwort von /operations enthält keine Vorgangsliste.")
            items.extend(page)
            pages += 1
            nxt, known = _next(body)
            if nxt:
                if nxt in seen:
                    notes.append("Pagination wiederholt denselben Cursor – Abruf beendet, Abdeckung unklar")
                    complete = False
                    break
                if pages >= MAX_PAGES:
                    notes.append(f"Mehr als {MAX_PAGES} Seiten – Rest folgt im nächsten Lauf")
                    complete = False
                    break
                seen.add(nxt)
                params = {**params, "cursor": nxt}
                continue
            if not known and page and (len(page) in COMMON_PAGE_SIZES or len(page) == requested):
                notes.append("Seitenende nicht eindeutig (volle Seite ohne Cursor) – Abdeckung unklar")
                complete = False
            break
        coverage.update(pages=pages, operations=len(items), page_size=requested or None)
        return items, complete, notes

    def _parse(self, o: dict[str, Any]) -> _Op:
        op_id = _get(o, "id", "operation_id", "operationId")
        op_type = str(_get(o, "type", "operation_type", "operationType") or "")
        ts = _ts(_get(o, "timestamp", "created_at", "createdAt", "executed_at", "executedAt", "time", "credited_at",
                      "creditedAt"))
        txs = o.get("transactions")
        single = not isinstance(txs, list)
        if single:
            txs = [o]  # Vorgang ohne Teilliste: der Vorgang selbst ist der einzige Teil
        legs = []
        for t in txs:
            if not isinstance(t, dict):
                continue
            amount = _dec(_get(t, "amount", "asset_amount", "assetAmount", "quantity"))
            flow = str(_get(t, "flow", "direction", "in_or_out", "inOrOut", "side") or "").lower()
            side = "in" if flow in ("incoming", "in", "credit") else "out" if flow in ("outgoing", "out", "debit") \
                else "?"
            if side == "?" and amount is not None and amount < 0:
                side = "out"
            if amount is not None:
                amount = abs(amount)
            cur_id = _get(t, "currency_id", "currencyId", "fiat_id", "fiatId")
            asset_id = _get(t, "asset_id", "assetId")
            legs.append(_Leg(
                id=None if single else _s(_get(t, "transaction_id", "transactionId", "id")),
                side=side, amount=amount, fee=abs(_dec(_get(t, "fee_amount", "feeAmount", "fee")) or Decimal(0)),
                ref=_s(cur_id or asset_id), kind="fiat" if cur_id else "?", symbol=None,
                trade_id=_s(_get(t, "trade_id", "tradeId")), compensates=_s(_get(t, "compensates")),
                ttype=_norm(_get(t, "transaction_type", "transactionType", "kind")),
                raw={"id": _s(_get(t, "transaction_id", "transactionId", "id")), "flow": flow or None,
                     "amount": _s(_get(t, "amount", "asset_amount", "assetAmount", "quantity")),
                     "fee": _s(_get(t, "fee_amount", "feeAmount", "fee")), "asset_id": _s(asset_id),
                     "currency_id": _s(cur_id), "trade_id": _s(_get(t, "trade_id", "tradeId")),
                     "transaction_type": _s(_get(t, "transaction_type", "transactionType")),
                     "compensates": _s(_get(t, "compensates"))}))
        raw = {"operation_id": _s(op_id), "type": op_type or None,
               "timestamp": _s(_get(o, "timestamp", "created_at", "createdAt", "executed_at", "time")),
               "transactions": [lg.raw for lg in legs]}
        return _Op(id=_s(op_id), type=op_type, ts=ts, legs=legs, raw=raw)

    def _resolve(self, api: _Api, ops: list[_Op], notes: list[str]) -> None:
        """Asset-/Währungs-UUIDs → Symbol und Art; nur unbekannte IDs werden abgerufen (Zwischenspeicher)."""
        cat = self._catalog()
        currencies_loaded = False
        failed: str | None = None
        for op in ops:
            for lg in op.legs:
                if not lg.ref:
                    lg.kind = "unknown"
                    continue
                meta = cat.get(lg.ref)
                if meta is None and lg.kind == "fiat" and not currencies_loaded:
                    currencies_loaded = True
                    self._load_currencies(api, cat, notes)
                    meta = cat.get(lg.ref)
                if meta is None and lg.kind != "fiat" and failed is None:
                    try:
                        meta = self._load_asset(api, cat, lg.ref)
                    except K.ConnectorError as e:
                        if e.kind in ("auth", "scope", "expired"):
                            failed = e.message
                            notes.append("Asset-Stammdaten nicht abrufbar – betroffene Vorgänge sind „ungeklärt“ "
                                         f"({e.message})")
                        elif e.kind in ("rate_limit", "unavailable"):
                            failed = e.message
                            notes.append(f"Asset-Stammdaten vorübergehend nicht abrufbar ({e.message})")
                        else:
                            meta = None
                if meta is None:
                    lg.kind = "unknown"
                    continue
                lg.symbol = (meta.get("symbol") or "").upper() or None
                lg.kind = "fiat" if lg.kind == "fiat" else _asset_kind(meta)
                if lg.symbol is None:
                    lg.kind = "unknown"
                lg.raw["symbol"] = lg.symbol

    def _load_currencies(self, api: _Api, cat: Any, notes: list[str]) -> None:
        try:
            body = api.get("/currencies", None, "Währungen (/currencies)")
        except K.ConnectorError as e:
            notes.append(f"Währungsliste nicht abrufbar ({e.message})")
            return
        for c in _items(body) or []:
            if not isinstance(c, dict):
                continue
            cid = _s(_get(c, "id", "currency_id", "currencyId"))
            code = _get(c, "symbol", "code", "iso_code", "isoCode")
            if cid and code:
                cat.put(cid, "currency", str(code).upper(), _s(_get(c, "name")), "FIAT", None)

    def _load_asset(self, api: _Api, cat: Any, ref: str) -> dict[str, Any] | None:
        try:
            body = api.get("/assets", {"id": ref}, "Asset-Stammdaten (/assets)")
        except K.ConnectorError as e:
            if e.kind != "data":
                raise
            body = api.get(f"/assets/{ref}", None, "Asset-Stammdaten (/assets)")
        data = _items(body)
        a = data[0] if data else (body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict)
                                  else body if isinstance(body, dict) and body.get("id") else None)
        if not isinstance(a, dict):
            return None
        sym = _get(a, "symbol")
        typ = " ".join(str(x) for x in (_get(a, "type", "asset_type", "assetType"), _get(a, "group")) if x)
        cat.put(ref, "asset", str(sym).upper() if sym else None, _s(_get(a, "name")), typ or None,
                _s(_get(a, "isin")))
        return cat.get(ref)

    def _map(self, ops: list[_Op]) -> tuple[list[K.SourceEvent], dict[str, int]]:
        compensated = {lg.compensates for op in ops for lg in op.legs if lg.compensates}
        mapper = _Mapper({c for c in compensated if c})
        events: list[K.SourceEvent] = []
        skipped: dict[str, int] = defaultdict(int)
        for op in ops:
            ev, why = mapper.event(op)
            if ev is not None:
                events.append(ev)
            elif why:
                skipped[f"Bitpanda: {why}"] += 1
        return events, dict(skipped)

    def _balances(self, api: _Api, ops: list[_Op], coverage: dict[str, Any], notes: list[str]) -> None:
        """Bestände laut Bitpanda gegen die Summe aller abgerufenen Vorgänge – nur Hinweis, nie Buchungsersatz."""
        try:
            body = api.get("/portfolio/holdings", None, "Bestände (/portfolio/holdings)")
        except K.ConnectorError as e:
            coverage["balances"] = {"checked": False,
                                    "note": "Bestandsprüfung übersprungen" + (
                                        " (Leserecht „Balance“ fehlt – optional)" if e.kind in ("auth", "scope")
                                        else f" ({e.message})")}
            return
        net: dict[str, Decimal] = defaultdict(Decimal)
        sym: dict[str, str] = {}
        for op in ops:
            for lg in op.legs:
                if lg.ref and lg.amount is not None and lg.side in ("in", "out"):
                    net[lg.ref] += lg.amount if lg.side == "in" else -lg.amount
                    net[lg.ref] -= lg.fee
                    if lg.symbol:
                        sym[lg.ref] = lg.symbol
        diffs = []
        checked = 0
        for h in _items(body) or []:
            if not isinstance(h, dict):
                continue
            ref = _s(_get(h, "assetId", "asset_id", "currencyId", "currency_id"))
            qty = _dec(_get(h, "quantity", "balance", "amount"))
            if not ref or qty is None:
                continue
            checked += 1
            have = net.get(ref, Decimal(0))
            if abs(have - qty) > max(abs(qty) * Decimal("0.000001"), Decimal("0.00000001")):
                diffs.append(f"{sym.get(ref, ref[:8])}: Vorgänge {_s(have)}, Bestand {_s(qty)}")
        coverage["balances"] = {"checked": True, "assets": checked, "differences": len(diffs), "examples": diffs[:5]}
        if diffs:
            notes.append(f"Bestandsprüfung: {len(diffs)} Asset(s) weichen von der Vorgangssumme ab (Hinweis, keine "
                         "Buchung) – z. B. " + "; ".join(diffs[:2]))

    @staticmethod
    def _limits(events: list[K.SourceEvent]) -> list[str]:
        out = []
        n_review = sum(1 for ev in events if ev.lines and ev.lines[0].kind == M.REVIEW)
        if n_review:
            out.append(f"{n_review} Vorgänge ohne eindeutige Abbildung („ungeklärt“)")
        return out
