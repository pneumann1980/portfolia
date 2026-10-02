"""Bitpanda – Connector für die Bitpanda Public API (ausschließlich lesend).

Vertrag laut offizieller Referenz (docs.public.bitpanda.com, geprüft am 02.10.2026; Details: README → Bitpanda)
    Basis ``https://api.public.bitpanda.com/v1``, Header ``x-api-key``, nur ``GET``:

    ``/operations``  Vorgänge. Parameter ``page_size`` (Standard 25), ``cursor``, ``from``, ``to``. Antwort ``data[]``,
                     ``self_cursor``, ``next_cursor``, ``has_next_page``. Je Vorgang ``operation_id``,
                     ``operation_type``, ``transactions[]``; je Teil u. a. ``transaction_id``, ``flow``
                     (INCOMING/OUTGOING), ``credited_at``, ``transaction_type``, ``wallet_id``, die
                     Betragsobjekte ``asset_amount``, ``fee_amount`` und ``asset_balance_after`` (je
                     ``{value, asset_id|currency_id}``), ``compensates`` und ``trade`` (``trade_id``, ``fee``,
                     ``rate``, ``rate_with_fee`` …). Leserecht „Transaction“.
    ``/assets``      Stammdaten; ``id`` als kommagetrennte Liste, ``page_size``, ``cursor`` (Pagination wie oben).
    ``/currencies``  Fiat-Währungen (ID → Code).
    ``/portfolio``   Bestände ``data[].balance.value`` – Leserecht „Balances“, optional (Bestandsprüfung).

    Nicht dokumentiert und deshalb nie vorausgesetzt: Wertebereich von ``operation_type``/``transaction_type``
    (beobachtet u. a. buy, sell, swap, deposit, withdrawal, savings_plan, stake, passive_earn_reward), der Höchstwert
    von ``page_size``, ob Gebühren im Betrag enthalten sind, ob ``balance`` gestakte Mengen enthält. Keine
    schreibenden Aufrufe, kein Rückgriff auf die ältere API ``api.bitpanda.com``, Umleitungen werden nicht verfolgt.

Pagination
    Erst wird die Seite vollständig verarbeitet, dann entscheidet ``has_next_page``: ``false`` beendet den Abruf –
    auch wenn ``next_cursor`` gesetzt ist; ``true`` setzt mit ``next_cursor`` unverändert fort. ``self_cursor`` ist
    nie Fortsetzungspunkt, eine Vorgangskennung nie Cursor. Fehlende oder widersprüchliche Angaben, ein wiederholter
    Cursor, eine leere Folgeseite oder nur bereits gelieferte Vorgänge machen den Abruf sichtbar unvollständig
    (Status „teilweise“, der Abrufstand rückt nicht vor). Die Seitenlänge entscheidet nie über das Ende.

Zeitpunkt
    Ausschließlich ``transactions[].credited_at`` (frühester Teil). Fehlt er, bleibt der Vorgang „ungeklärt“ und als
    „Zeitpunkt fehlt“ gekennzeichnet – nie gebucht, nie durch den Abrufzeitpunkt ersetzt.

Gebühren – Bedeutung nicht dokumentiert; übernommen wird nur, was die Daten selbst belegen
    * ``fee_amount``: Der Saldoverlauf (``asset_balance_after`` desselben Wallets, Vorgänger im selben Abruf) zeigt,
      ob die Gebühr zusätzlich abgezogen wurde oder im Betrag steckt. Ohne Beleg: Gebühr übernommen, Zeile
      prüfbedürftig.
    * ``trade.fee``: Betrag ≈ Menge × ``rate_with_fee`` → im Betrag enthalten (Einstand bzw. Erlös stimmen ohne
      zusätzliche Gebühr); Betrag ≈ Menge × ``rate`` → zusätzlich; sonst prüfbedürftig.

Abbildung – nur eindeutige Fälle, sonst „ungeklärt“ mit Grund (nie geraten, nie still verworfen)
    * Kauf: Fiat-Ausgang + Krypto-Eingang (auch Sparplan) · Verkauf: Krypto-Ausgang + Fiat-Eingang
    * Swap: Verkaufs- und Kauf-Paar über dieselbe Fiat-Währung → Verkauf + Kauf mit den Fiat-Beträgen
    * Einzahlung/Auszahlung: ein Eingang bzw. Ausgang mit Vorgangsart deposit/withdraw…; Sparplan-Einzahlung →
      Zugang
    * Erträge: ein Krypto-Eingang mit Ertragsart (reward, staking reward, passive earn reward, onetime reward …)
    * Token-Umstellung (merger, migration): Krypto-Ausgang + Krypto-Eingang → Umstellung, prüfbedürftig
    * eigene Gebühren-Teile (Transaktionsart „fee“) → Gebührenzeile
    * ohne Buchung, gezählt: interne Umbuchungen (gleiches Asset, gleicher Betrag) und Staking-Umbuchungen
    * ungeklärt: Korrekturen/Stornos (``compensates``) samt storniertem Vorgang, Krypto↔Krypto ohne Fiat-Teile,
      Aktien/ETFs, Edelmetalle, Indizes, unbekannte Assets oder Vorgangsarten, fehlende Richtung oder Zeitangabe

Diagnose (datensparsam: Feldnamen und Zähler, nie Werte, Kennungen, Cursor oder Schlüssel)
    Parser-Version, Zeitquelle je Vorgang, Ende der Pagination, nicht dokumentierte bzw. fehlende Pflichtfelder,
    Saldoverlauf (stimmig/Brüche) und Bestandsabgleich ``/portfolio`` ↔ Vorgänge.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
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
PARSER_VERSION = 3  # 3: Vertrag laut Referenz (credited_at, Betragsobjekte, trade, has_next_page)
CURSOR_VERSION = 3  # Abrufstand einer älteren Auswertung → vollständiger Neuabruf
PREFIX = "bitpanda"
PAGE_SIZE = 100  # Höchstwert nicht dokumentiert – lehnt Bitpanda ab (HTTP 400), gilt der Standard
PAGE_SIZE_DEFAULT = 25  # dokumentierter Standard
ASSET_CHUNK = 25
MAX_PAGES = 2000
MAX_EMPTY_PAGES = 3  # leere Seiten in Folge trotz has_next_page=true – danach unvollständig
MAX_ASSET_PAGES = 20
OVERLAP = timedelta(days=2)  # spät gutgeschriebene Vorgänge – bekannte werden erkannt
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
RETRIES = 3
WAIT_BUDGET_S = 120.0
MAX_RETRY_AFTER_S = 60.0
TOL = Decimal("0.00000001")
ZERO = Decimal(0)

# Ertragsarten (normalisierte Vorgangsart → Tag des Datenvertrags); nur Krypto-Zugänge
INCOME_TYPES = {"reward": "reward", "rewards": "reward", "stakingreward": "staking", "stakingrewards": "staking",
                "passiveearnreward": "reward", "earnreward": "reward", "onetimereward": "bonus", "bonus": "bonus",
                "cashback": "cashback", "airdrop": "airdrop", "interest": "interest", "lendingreward": "lending"}
STAKE_TYPES = {"stake", "unstake", "staking", "unstaking", "earnstake", "earnunstake"}
CONVERSION_WORDS = ("merger", "migration", "rename", "redenomination", "conversion")
_TRADE_WORDS = ("buy", "sell", "trade", "order", "saving", "instant")
_NOT_TRADE = ("reward", "staking", "deposit", "withdraw", "transfer", "airdrop", "fee", "bonus", "interest")
_CORRECTION = ("cancel", "revers", "compensat", "refund", "chargeback", "storno", "correct")
_INCOME_WORDS = ("reward", "staking", "airdrop", "bonus", "interest")

# Felder laut Referenz – für die Diagnose (nicht dokumentierte Felder, fehlende Pflichtfelder; nur Namen)
DOC_FIELDS = {
    "page": frozenset({"data", "self_cursor", "next_cursor", "has_next_page"}),
    "operation": frozenset({"operation_id", "operation_type", "transactions"}),
    "transaction": frozenset({"transaction_id", "asset_id", "currency_id", "wallet_id", "wallet_owner", "asset_amount",
                              "fee_amount", "transaction_type", "flow", "order_id", "credited_at",
                              "asset_balance_after", "compensates", "compensates_info", "index_asset_id", "trade"}),
    "trade": frozenset({"trade_id", "fee", "fee_percentage", "rate", "rate_with_fee", "to_eur_rate"}),
    "holding": frozenset({"asset_id", "currency_id", "balance", "available_balance", "invested_amount",
                          "average_buy_price", "currency_balance", "total_return", "total_return_percent"}),
}
REQUIRED = {"page": ("data", "has_next_page"), "operation": ("operation_id", "operation_type", "transactions"),
            "transaction": ("transaction_id", "flow", "asset_amount", "credited_at"), "holding": ("balance",)}
TIME_SOURCE = "transactions[].credited_at"


# ----------------------------------------------------------------------------------------------------
# Hilfen: JSON, Zeit, Beträge
# ----------------------------------------------------------------------------------------------------

def _body(resp: httpx.Response) -> Any:
    try:
        return json.loads(resp.content or b"null", parse_float=Decimal)
    except ValueError:
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
    """Zeitpunkt aus ISO-8601 (``date-time`` laut Referenz); ohne Zone gilt UTC."""
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC)


def _amount(v: Any) -> tuple[Decimal | None, str | None, bool | None]:
    """Betragsobjekt ``{"value": "<Text>", "asset_id"|"currency_id": "<UUID>"}`` → (Betrag, Referenz, Fiat?).
    Fiat, wenn die Referenz eine ``currency_id`` ist (``/currencies`` listet Fiat-Währungen). Sind beide gesetzt,
    gilt das Asset (Annahme – nicht dokumentiert)."""
    if not isinstance(v, dict):
        return None, None, None
    value = _dec(v.get("value"))
    aid, cid = _s(v.get("asset_id")), _s(v.get("currency_id"))
    if aid:
        return value, aid, False
    if cid:
        return value, cid, True
    return value, None, None


def _plain(v: Any, depth: int = 0) -> Any:
    """Originaldaten JSON-tauglich (Decimal als Text) für die Nachprüfung im Prüf-Stapel."""
    if depth > 6:
        return None
    if isinstance(v, dict):
        return {str(k): _plain(x, depth + 1) for k, x in list(v.items())[:60]}
    if isinstance(v, list):
        return [_plain(x, depth + 1) for x in v[:40]]
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, str):
        return v[:300]
    return v


def _iso_ms(dt: datetime) -> str:
    """Format der Referenz für ``from``/``to``: ``2024-01-01T00:00:00.000Z``."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.astimezone(UTC).microsecond // 1000:03d}Z"


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _s(v: Any) -> str | None:
    if v is None or v == "":
        return None
    return format(v, "f") if isinstance(v, Decimal) else str(v)


def _q(v: Decimal | None) -> str:
    return format(v.normalize(), "f") if isinstance(v, Decimal) and v else "0"


def _close(a: Decimal, b: Decimal, tol: Decimal = TOL) -> bool:
    return abs(a - b) <= tol


class _Diag:
    """Datensparsame Diagnose eines Abrufs: Feldnamen, Zähler, Zeitquellen – nie Werte, Kennungen oder Cursor."""

    def __init__(self) -> None:
        self.fields: dict[str, Counter[str]] = defaultdict(Counter)
        self.objects: Counter[str] = Counter()
        self.missing: Counter[str] = Counter()
        self.counts: Counter[str] = Counter()
        self.time_sources: Counter[str] = Counter()

    def seen(self, level: str, obj: dict[str, Any]) -> None:
        self.objects[level] += 1
        for k in obj:
            self.fields[level][str(k)[:40]] += 1
        for f in REQUIRED.get(level, ()):
            if obj.get(f) in (None, "", [], {}):
                self.missing[f"{level}.{f}"] += 1

    def count(self, what: str, n: int = 1) -> None:
        self.counts[what] += n

    def summary(self) -> dict[str, Any]:
        undocumented = {lvl: sorted(k for k in c if k not in DOC_FIELDS.get(lvl, frozenset()))[:12]
                        for lvl, c in self.fields.items()}
        return {"parser": PARSER_VERSION, "objects": dict(self.objects), "missing": dict(self.missing),
                "undocumented": {k: v for k, v in undocumented.items() if v}, "counts": dict(self.counts),
                "time_sources": dict(self.time_sources)}


# ----------------------------------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------------------------------

def _error_text(body: Any) -> str:
    """Fehlertext ohne Geheimnisse: dokumentiert ``{"error": {"code": …}}``."""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return " – ".join(str(err[k])[:80] for k in ("code", "message") if err.get(k))
        if isinstance(err, str):
            return err
        for k in ("message", "detail", "title"):
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


@dataclass
class _Paging:
    cursor: str | None = None  # Fortsetzung – nur bei has_next_page=true
    end: bool = False  # has_next_page=false
    problem: str | None = None  # fehlende oder widersprüchliche Angaben


def _paging(body: dict[str, Any]) -> _Paging:
    """Seitenende laut Referenz: ``has_next_page`` entscheidet, ``next_cursor`` ist nur bei ``true`` maßgeblich."""
    has = body.get("has_next_page")
    nxt = body.get("next_cursor")
    nxt = nxt if isinstance(nxt, str) and nxt.strip() else None
    if has is False:
        return _Paging(end=True)
    if has is True:
        if nxt is None:
            return _Paging(problem="has_next_page=true ohne next_cursor")
        if nxt == body.get("self_cursor"):
            return _Paging(problem="next_cursor gleich self_cursor bei has_next_page=true")
        return _Paging(cursor=nxt)
    if "has_next_page" not in body:
        return _Paging(problem="has_next_page fehlt in der Antwort")
    return _Paging(problem="has_next_page ist kein Wahrheitswert")


def _check_page(body: Any, what: str) -> str | None:
    """Antwortformat einer Listenantwort prüfen (Verbindungstest) – Problem als Text."""
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        return f"Antwort von {what} ohne Liste „data“"
    if not isinstance(body.get("has_next_page"), bool):
        return f"Antwort von {what} ohne Wahrheitswert „has_next_page“"
    return None


# ----------------------------------------------------------------------------------------------------
# Vorgänge → Teile
# ----------------------------------------------------------------------------------------------------

@dataclass
class _Leg:
    id: str | None
    side: str  # in | out | ?
    amount: Decimal | None
    ref: str | None  # Asset- bzw. Währungs-UUID des Betrags
    kind: str  # fiat | crypto | security | metal | index | unknown | ? (vor der Auflösung)
    ttype: str  # transaction_type, normalisiert
    ts: datetime | None  # credited_at
    fee: Decimal = ZERO  # fee_amount.value
    fee_ref: str | None = None
    fee_kind: str = "?"
    trade_id: str | None = None
    trade_fee: Decimal = ZERO  # trade.fee.value
    trade_fee_ref: str | None = None
    trade_fee_kind: str = "?"
    rate: Decimal | None = None
    rate_with_fee: Decimal | None = None
    compensates: str | None = None
    wallet: str | None = None
    balance_after: Decimal | None = None
    balance_ref: str | None = None
    index_asset: str | None = None
    symbol: str | None = None
    fee_symbol: str | None = None
    trade_fee_symbol: str | None = None
    fee_mode: str | None = None  # fee_amount laut Saldoverlauf: extra (zusätzlich) | inside (im Betrag/ohne Wirkung)
    trade_fee_extra: bool = False  # trade.fee laut Saldoverlauf zusätzlich abgebucht
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_fee(self) -> bool:
        return "fee" in self.ttype

    @property
    def fee_same(self) -> bool:
        """Gebühr im Asset des Betrags (nur dann sagt der Saldoverlauf etwas über sie)."""
        return self.fee_ref in (None, self.ref)

    @property
    def fee_sym(self) -> str | None:
        return self.symbol if self.fee_same else self.fee_symbol


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
        digest = hashlib.sha256(json.dumps(self.raw.get("api"), sort_keys=True, default=str).encode()).hexdigest()
        return f"{PREFIX}:h{digest[:24]}"

    def aliases(self) -> list[str]:
        vals = [self.id, *(leg.id for leg in self.legs), *(leg.trade_id for leg in self.legs)]
        return sorted({k for v in vals if v and (k := uuid_key(PREFIX, v))})


def _parse_leg(t: dict[str, Any], diag: _Diag) -> _Leg:
    amount, ref, fiat = _amount(t.get("asset_amount"))
    if ref is None:  # Referenz am Teil selbst (laut Referenz ebenfalls asset_id bzw. currency_id)
        aid, cid = _s(t.get("asset_id")), _s(t.get("currency_id"))
        ref, fiat = (aid, False) if aid else ((cid, True) if cid else (None, None))
    fee, fee_ref, fee_fiat = _amount(t.get("fee_amount"))
    trade = t.get("trade") if isinstance(t.get("trade"), dict) else {}
    if trade:
        diag.seen("trade", trade)
    tfee, tfee_ref, tfee_fiat = _amount(trade.get("fee"))
    flow = str(t.get("flow") or "").strip().upper()
    side = {"INCOMING": "in", "OUTGOING": "out"}.get(flow, "?")
    if amount is not None and amount < 0:
        diag.count("negativer Betrag")
        amount = -amount
    credited = t.get("credited_at")
    ts = _ts(credited)
    if credited not in (None, "") and ts is None:
        diag.count("credited_at nicht lesbar")
    bal, bal_ref, _ = _amount(t.get("asset_balance_after"))
    leg = _Leg(id=_s(t.get("transaction_id")), side=side, amount=amount, ref=ref, kind="fiat" if fiat else "?",
               ttype=_norm(t.get("transaction_type")), ts=ts, fee=abs(fee or ZERO), fee_ref=fee_ref,
               fee_kind="fiat" if fee_fiat else "?", trade_id=_s(trade.get("trade_id")),
               trade_fee=abs(tfee or ZERO), trade_fee_ref=tfee_ref, trade_fee_kind="fiat" if tfee_fiat else "?",
               rate=_dec(trade.get("rate")), rate_with_fee=_dec(trade.get("rate_with_fee")),
               compensates=_s(t.get("compensates")), wallet=_s(t.get("wallet_id")), balance_after=bal,
               balance_ref=bal_ref or ref, index_asset=_s(t.get("index_asset_id")))
    leg.raw = {k: v for k, v in {
        "transaction_id": leg.id, "flow": flow or None, "transaction_type": _s(t.get("transaction_type")),
        "credited_at": _iso(ts) if ts else None, "amount": _s(amount), "ref": ref, "fiat": fiat,
        "fee": _s(fee) if fee else None, "fee_ref": fee_ref, "trade_id": leg.trade_id,
        "trade_fee": _s(tfee) if tfee else None, "trade_fee_ref": tfee_ref, "rate": _s(leg.rate),
        "rate_with_fee": _s(leg.rate_with_fee), "wallet_id": leg.wallet, "balance_after": _s(bal),
        "compensates": leg.compensates, "index_asset_id": leg.index_asset}.items() if v is not None}
    return leg


def _parse(o: dict[str, Any], diag: _Diag) -> _Op:
    """Vorgang laut Referenz → Teile. Zeitpunkt: frühestes ``credited_at`` der Teile (Quelle in ``raw``)."""
    diag.seen("operation", o)
    op_id = _s(o.get("operation_id"))
    op_type = str(o.get("operation_type") or "")
    txs = o.get("transactions")
    legs: list[_Leg] = []
    for t in txs if isinstance(txs, list) else []:
        if not isinstance(t, dict):
            diag.count("Teil ohne Objektstruktur")
            continue
        diag.seen("transaction", t)
        legs.append(_parse_leg(t, diag))
    times = [lg.ts for lg in legs if lg.ts is not None]
    ts = min(times) if times else None
    diag.time_sources[TIME_SOURCE if ts else "fehlt"] += 1
    raw: dict[str, Any] = {"parser": PARSER_VERSION, "operation_id": op_id, "operation_type": op_type or None,
                           "credited_at": _iso(ts) if ts else None, "time_source": TIME_SOURCE if ts else None}
    if times and len(times) < len(legs):
        raw["time_partial"] = True  # nicht alle Teile tragen credited_at
    if times and max(times) != min(times):
        raw["time_spread_s"] = int((max(times) - min(times)).total_seconds())
    raw["transactions"] = [lg.raw for lg in legs]
    raw["api"] = _plain(o)
    return _Op(id=op_id, type=op_type, ts=ts, legs=legs, raw=raw)


def _rate_mode(fiat: Decimal | None, qty: Decimal | None, rate: Decimal | None,
               rate_with_fee: Decimal | None) -> str | None:
    """Enthält der Fiat-Betrag eines Handels die Gebühr? ``inside``: Betrag ≈ Menge × rate_with_fee, ``extra``:
    Betrag ≈ Menge × rate; ``None``: nicht eindeutig (fehlende Kurse oder beide bzw. keiner passend)."""
    if not fiat or not qty or not rate or not rate_with_fee:
        return None
    with_fee, without = qty * rate_with_fee, qty * rate
    gap = abs(with_fee - without)
    if gap < Decimal("0.005"):
        return "inside"  # Gebühr unter einem halben Cent – ohne Wirkung auf Einstand und Erlös
    d_with, d_without = abs(fiat - with_fee), abs(fiat - without)
    if d_with <= gap / 4 and d_without >= gap * 3 / 4:
        return "inside"
    if d_without <= gap / 4 and d_with >= gap * 3 / 4:
        return "extra"
    return None


def _chain(ops: list[_Op], diag: _Diag) -> None:
    """Saldoverlauf je Wallet und Asset (``asset_balance_after``): Differenz zum zeitlich vorhergehenden Teil
    desselben Wallets gegen den Betrag. Belegt, ob eine Gebühr zusätzlich abgezogen wurde (``extra``) oder im
    Betrag steckt bzw. den Bestand nicht berührt (``inside``); Brüche deuten auf fehlende Vorgänge."""
    groups: dict[tuple[str, str], list[_Leg]] = defaultdict(list)
    for op in ops:
        for lg in op.legs:
            if lg.wallet and lg.balance_ref and lg.ts is not None and lg.balance_after is not None \
                    and lg.amount is not None and lg.side in ("in", "out"):
                groups[(lg.wallet, lg.balance_ref)].append(lg)
    for legs in groups.values():
        legs.sort(key=lambda x: x.ts)  # type: ignore[arg-type, return-value]
        for i, lg in enumerate(legs):
            if i == 0:
                continue
            prev = legs[i - 1]
            same_time = prev.ts == lg.ts or (i + 1 < len(legs) and legs[i + 1].ts == lg.ts)
            if same_time:
                diag.count("Saldoverlauf: Reihenfolge nicht eindeutig")
                continue
            delta = lg.balance_after - prev.balance_after  # type: ignore[operator]
            signed = lg.amount if lg.side == "in" else -lg.amount  # type: ignore[operator]
            fee = lg.fee if lg.fee_same else ZERO
            tfee = lg.trade_fee if lg.trade_fee_ref == lg.ref else ZERO
            diag.count("Saldoverlauf: Übergänge")
            if _close(delta, signed):
                diag.count("Saldoverlauf: stimmig")
                if fee:
                    lg.fee_mode = "inside"
            elif fee and _close(delta, signed - fee):
                diag.count("Saldoverlauf: stimmig")
                lg.fee_mode = "extra"
            elif tfee and _close(delta, signed - tfee):
                diag.count("Saldoverlauf: stimmig")
                lg.trade_fee_extra = True
            else:
                diag.count("Saldoverlauf: Brüche")


# ----------------------------------------------------------------------------------------------------
# Teile → Zeilen
# ----------------------------------------------------------------------------------------------------

class _Mapper:
    def __init__(self, compensated: set[str], now: datetime) -> None:
        self.compensated = compensated
        self.now = now

    def event(self, op: _Op) -> tuple[K.SourceEvent | None, str | None]:
        """(Ereignis, Grund ohne Buchung)."""
        label = f"Bitpanda: {op.type or 'Vorgang'}"
        ts = op.ts or self.now  # ohne Zeitpunkt nur zur Sortierung – Zeile trägt „Zeitpunkt fehlt“, nie gebucht

        def rec(kind: str, **kw: Any) -> Rec:
            return Rec(line=0, ts=ts, kind=kind, label=label, aliases=op.aliases(), raw=op.raw, **kw)

        def review(reason: str) -> K.SourceEvent:
            ins = [lg for lg in op.legs if lg.side == "in"]
            outs = [lg for lg in op.legs if lg.side == "out"]
            r = rec(M.REVIEW, note=f"Ungeklärt: {reason}", ts_missing=op.ts is None,
                    in_sym=ins[0].symbol if ins else None, in_qty=ins[0].amount if ins else None,
                    out_sym=outs[0].symbol if outs else None, out_qty=outs[0].amount if outs else None)
            return K.SourceEvent(op.key, ts, [r], label)

        otype = _norm(op.type)
        legs = [lg for lg in op.legs if (lg.amount or 0) != 0 or lg.fee]
        if op.ts is None:
            return review("Zeitpunkt fehlt in den API-Daten (transactions[].credited_at leer) – nicht gebucht; ein "
                          "späterer Abruf ersetzt die Zeile, sobald Bitpanda ihn liefert"), None
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
            return review("Richtung (flow INCOMING/OUTGOING) nicht angegeben"), None
        if any(lg.index_asset for lg in legs):
            return review(f"{_KIND_TEXT['index']} – Asset und steuerliche Einordnung nicht eindeutig; bitte prüfen "
                          "oder per CSV/manuell erfassen"), None
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
        if otype in STAKE_TYPES and len(main) == 1 and main[0].kind == "crypto" and not fees and not main[0].fee:
            return None, "Umbuchung in bzw. aus Bitpanda Staking (kein Zu- oder Abgang)"

        trade_like = not otype or any(w in otype for w in _TRADE_WORDS)
        conflicting = any(w in otype for w in _NOT_TRADE)
        if len(ins) == 2 and len(outs) == 2 and {lg.ttype for lg in main} == {"buy", "sell"}:
            pair = {(lg.ttype, lg.side): lg for lg in main}
            so, si, bo, bi = (pair.get(k) for k in (("sell", "out"), ("sell", "in"), ("buy", "out"), ("buy", "in")))
            if so and si and bo and bi and so.kind == "crypto" and si.kind == "fiat" and bo.kind == "fiat" \
                    and bi.kind == "crypto" and si.symbol == bo.symbol:
                note = f"Tausch {so.symbol} → {bi.symbol} über {si.symbol} (Bitpanda Swap)"
                sell = _trade(rec(M.TRADE, out_sym=so.symbol, out_qty=so.amount, in_sym=si.symbol, in_qty=si.amount,
                                  value=si.amount, value_ccy=si.symbol, note=note), fiat=si, crypto=so)
                buy = _trade(rec(M.TRADE, out_sym=bo.symbol, out_qty=bo.amount, in_sym=bi.symbol, in_qty=bi.amount,
                                 value=bo.amount, value_ccy=bo.symbol, note=note), fiat=bo, crypto=bi)
                return K.SourceEvent(op.key, ts, [sell, buy, *fee_lines], label), None
        if len(ins) == 1 and len(outs) == 1 and any(w in otype for w in CONVERSION_WORDS):
            i, o = ins[0], outs[0]
            if i.kind == "crypto" and o.kind == "crypto":
                r = _leg_fees(rec(M.CONVERSION, out_sym=o.symbol, out_qty=o.amount, in_sym=i.symbol, in_qty=i.amount,
                                  note=f"Token-Umstellung {o.symbol} → {i.symbol} ({op.type})"), (o, i))
                r.review = r.review or ("Token-Umstellung: Einstand und Anschaffungsdatum gehen auf das neue Asset "
                                        "über – Verhältnis und Asset-Zuordnung bitte prüfen")
                return K.SourceEvent(op.key, ts, [r, *fee_lines], label), None
        if len(ins) == 1 and len(outs) == 1:
            i, o = ins[0], outs[0]
            if o.kind == "fiat" and i.kind == "crypto" and trade_like and not conflicting:
                r = _trade(rec(M.TRADE, out_sym=o.symbol, out_qty=o.amount, in_sym=i.symbol, in_qty=i.amount,
                               value=o.amount, value_ccy=o.symbol, note="Sparplan" if "saving" in otype else None),
                           fiat=o, crypto=i)
                return K.SourceEvent(op.key, ts, [r, *fee_lines], label), None
            if o.kind == "crypto" and i.kind == "fiat" and trade_like and not conflicting:
                r = _trade(rec(M.TRADE, out_sym=o.symbol, out_qty=o.amount, in_sym=i.symbol, in_qty=i.amount,
                               value=i.amount, value_ccy=i.symbol), fiat=i, crypto=o)
                return K.SourceEvent(op.key, ts, [r, *fee_lines], label), None
            if o.kind == "crypto" and i.kind == "crypto":
                return review("Tausch Krypto → Krypto: Gegenwert in EUR nicht in den API-Daten – bitte prüfen"), None
            if o.kind == "fiat" and i.kind == "fiat":
                return review("Währungstausch Fiat → Fiat"), None
        if len(ins) == 1 and not outs:
            i = ins[0]
            if otype in INCOME_TYPES and i.kind == "crypto":
                r = _leg_fees(rec(M.DEPOSIT, in_sym=i.symbol, in_qty=i.amount, tag=INCOME_TYPES[otype]), (i,))
                return K.SourceEvent(op.key, ts, [r, *fee_lines], label), None
            if ("deposit" in otype or (i.ttype == "deposit" and "saving" in otype)) \
                    and not any(w in otype for w in _INCOME_WORDS):
                r = _leg_fees(rec(M.DEPOSIT, in_sym=i.symbol, in_qty=i.amount,
                                  note="Einzahlung für den Sparplan" if "saving" in otype else None), (i,))
                return K.SourceEvent(op.key, ts, [r, *fee_lines], label), None
        if len(outs) == 1 and not ins and "withdraw" in otype:
            o = outs[0]
            r = _leg_fees(rec(M.WITHDRAWAL, out_sym=o.symbol, out_qty=o.amount), (o,))
            return K.SourceEvent(op.key, ts, [r, *fee_lines], label), None
        if not main and fees:
            return K.SourceEvent(op.key, ts, fee_lines, label), None
        return review(f"Vorgangsart „{op.type or '–'}“ mit {len(ins)} Eingang/Eingängen und {len(outs)} Ausgang/"
                      "Ausgängen – Deutung nicht eindeutig"), None


def _add_note(r: Rec, text: str) -> None:
    r.note = f"{r.note} · {text}" if r.note else text


def _set_review(r: Rec, text: str) -> None:
    r.review = f"{r.review}; {text}" if r.review else text


def _leg_fees(r: Rec, parts: Iterable[_Leg]) -> Rec:
    """``fee_amount`` der Teile übernehmen – so, wie der Saldoverlauf sie belegt; ohne Beleg prüfbedürftig."""
    charged = [lg for lg in parts if lg.fee]
    if not charged:
        return r
    if len(charged) > 1:
        _set_review(r, "Gebühren an mehreren Teilen – Zuordnung prüfen")
    lg = charged[0]
    sym = lg.fee_sym
    if sym is None:
        r.fee_sym, r.fee_qty = None, None
        _set_review(r, f"Gebühr {_q(lg.fee)} in einem unbekannten Asset – bitte mit dem Bitpanda-Beleg vergleichen")
        return r
    if lg.fee_mode == "extra":
        r.fee_sym, r.fee_qty = sym, lg.fee
        _add_note(r, f"Gebühr {_q(lg.fee)} {sym} zusätzlich abgezogen (laut Saldoverlauf)")
    elif lg.fee_mode == "inside" and lg.side == "out" and r.out_qty is not None and r.out_qty > lg.fee:
        r.out_qty = r.out_qty - lg.fee  # Abgang ohne Gebühr; zusammen verlässt der Betrag das Wallet
        r.fee_sym, r.fee_qty = sym, lg.fee
        _add_note(r, f"Betrag {_q(lg.amount)} {lg.symbol} enthält die Gebühr {_q(lg.fee)} (laut Saldoverlauf)")
    elif lg.fee_mode == "inside" and lg.side == "in":
        _add_note(r, f"Gebühr {_q(lg.fee)} {sym} ohne Wirkung auf den Bestand (laut Saldoverlauf) – nur Hinweis")
    else:
        r.fee_sym, r.fee_qty = sym, lg.fee
        _set_review(r, f"Gebühr {_q(lg.fee)} {sym}: ob der Betrag sie bereits enthält, ist nicht dokumentiert und hier "
                       "nicht belegbar – bitte mit dem Bitpanda-Beleg vergleichen")
    return r


def _trade(r: Rec, *, fiat: _Leg, crypto: _Leg) -> Rec:
    """Handel: ``fee_amount`` der Teile wie belegt, dazu ``trade.fee`` (je Handel einmal) – im Fiat-Betrag
    enthalten, zusätzlich oder prüfbedürftig, je nachdem, was Kurs und Saldoverlauf belegen."""
    r = _leg_fees(r, (fiat, crypto))
    trades = {lg.trade_id or f"#{i}": lg for i, lg in enumerate((fiat, crypto)) if lg.trade_fee}
    if not trades:
        return r
    fees = {(lg.trade_fee, lg.trade_fee_ref) for lg in trades.values()}
    if len(fees) > 1:
        _set_review(r, "unterschiedliche Handelsgebühren an den Teilen – bitte mit dem Bitpanda-Beleg vergleichen")
        return r
    src = fiat if fiat.trade_fee else crypto
    sym = src.trade_fee_symbol or (fiat.symbol if src.trade_fee_ref in (None, fiat.ref) else None)
    text = f"{_q(src.trade_fee)} {sym or '?'}"
    mode = _rate_mode(fiat.amount, crypto.amount, src.rate, src.rate_with_fee)
    if mode == "inside":
        _add_note(r, f"Gebühr {text} laut Bitpanda im Betrag enthalten (Kurs mit Gebühr)")
        return r
    if r.fee_qty:  # zweite Gebühr ohne eigenes Feld – nicht still zusammenfassen
        _set_review(r, f"zusätzlich Handelsgebühr {text} – bitte mit dem Bitpanda-Beleg vergleichen")
        return r
    if mode == "extra" and sym is not None:
        r.fee_sym, r.fee_qty = sym, src.trade_fee
        if fiat.trade_fee_extra:
            _add_note(r, f"Gebühr {text} zusätzlich zum Betrag (laut Kurs und Saldoverlauf)")
        else:
            _set_review(r, f"Gebühr {text} laut Kurs zusätzlich zum Betrag, Abbuchung nicht belegt – bitte mit dem "
                           "Bitpanda-Beleg vergleichen")
        return r
    _set_review(r, f"Gebühr {text} laut Bitpanda (trade.fee): ob der Betrag sie enthält, ist nicht dokumentiert und "
                   "aus Kurs und Saldo nicht ableitbar – bitte mit dem Bitpanda-Beleg vergleichen")
    return r


# ----------------------------------------------------------------------------------------------------
# Connector
# ----------------------------------------------------------------------------------------------------

@K.register
class BitpandaConnector(K.Connector):
    provider = "bitpanda"
    label = "Bitpanda (Public API)"
    needs_credentials = True
    parser_version = PARSER_VERSION
    base_url: ClassVar[str] = BASE_URL
    transport: ClassVar[httpx.BaseTransport | None] = None  # nur Tests (synthetische Fixtures)
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

            def probe(name: str, path: str, params: dict[str, Any] | None, what: str, ok_text: str,
                      validate: Callable[[Any], str | None]) -> K.ConnectorError | None:
                try:
                    body = api.get(path, params, what)
                except K.ConnectorError as e:
                    details[name] = {"ok": False, "text": e.message}
                    return e
                problem = validate(body)
                details[name] = {"ok": problem is None, "text": ok_text + (f" – {problem}" if problem else "")}
                return None

            e_ops = probe("transaction", "/operations", {"page_size": 1}, "Vorgänge (/operations)",
                          "Vorgänge lesbar – Leserecht „Transaction“ vorhanden", self._check_operations)
            e_bal = probe("balances", "/portfolio", None, "Bestände (/portfolio)",
                          "Bestände lesbar – Leserecht „Balances“ vorhanden (optional, für die Bestandsprüfung)",
                          self._check_portfolio)
            e_assets = probe("assets", "/assets", {"page_size": 1}, "Asset-Stammdaten (/assets)",
                             "Asset-Stammdaten lesbar", lambda b: _check_page(b, "/assets"))
        if e_ops is None:
            msg = "Verbindung in Ordnung – Vorgänge lesbar."
            if not details["transaction"]["ok"]:
                msg += " Das Antwortformat weicht von der Referenz ab – Details unten."
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

    @staticmethod
    def _check_operations(body: Any) -> str | None:
        problem = _check_page(body, "/operations")
        if problem or not body["data"]:
            return problem
        o = body["data"][0]
        if not isinstance(o, dict):
            return "Vorgang ohne Objektstruktur"
        missing = [f for f in REQUIRED["operation"] if o.get(f) in (None, "", [])]
        txs = [t for t in o.get("transactions") or [] if isinstance(t, dict)]
        missing += sorted({f"transactions[].{f}" for t in txs for f in REQUIRED["transaction"]
                           if t.get(f) in (None, "", {})})
        return ("Felder fehlen im ersten Vorgang: " + ", ".join(missing)) if missing else None

    @staticmethod
    def _check_portfolio(body: Any) -> str | None:
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            return "Antwort ohne Liste „data“ – Bestandsprüfung nicht möglich"
        parsed = sum(1 for h in data if isinstance(h, dict) and _amount(h.get("balance"))[0] is not None)
        if not parsed:
            return "keine auswertbare Position (data[].balance.value) – Bestandsprüfung nicht möglich"
        return None

    # -- Abrufen ------------------------------------------------------------------------------------------
    def rewind(self, cursor: dict[str, Any] | None, before: datetime) -> dict[str, Any] | None:
        """Verworfene Vorgänge erneut liefern: vollständiger Neuabruf (bekannte Vorgänge werden erkannt; ein
        verworfener Vorgang ohne Zeitpunkt hätte keinen verlässlichen Zeitpunkt zum Zurücksetzen)."""
        return None

    def fetch(self, cfg: K.SourceConfig, secret: K.Secret, cursor: dict[str, Any] | None) -> K.FetchResult:
        started = datetime.now(UTC)
        notes: list[str] = []
        if cursor and cursor.get("v") != CURSOR_VERSION:
            cursor = None  # Abrufstand einer älteren Auswertung → vollständig neu abrufen und auswerten
            notes.append(f"Auswertung aktualisiert (Parser v{PARSER_VERSION}) – alle Vorgänge werden neu abgerufen; "
                         "unbearbeitete Prüfzeilen älterer Auswertungen werden ersetzt, übernommene Buchungen bleiben")
        since = _ts((cursor or {}).get("from"))
        diag = _Diag()
        coverage: dict[str, Any] = {"api": BASE_URL, "parser": PARSER_VERSION,
                                    "mode": "inkrementell" if since else "vollständig",
                                    "from": _iso(since) if since else None, "to": _iso(started)}
        with self._client() as client:
            api = _Api(secret.reveal(), client, self.sleep)
            items, complete, page_notes = self._operations(api, since, coverage, diag)
            notes += page_notes
            ops = [_parse(o, diag) for o in items if isinstance(o, dict)]
            if len(ops) != len(items):
                notes.append(f"{len(items) - len(ops)} Einträge ohne Objektstruktur übersprungen – Abdeckung unklar")
                complete = False
            self._resolve(api, ops, notes)
            _chain(ops, diag)
            events, skipped = self._map(ops, started)
            balances = self._balances(api, ops, complete and since is None, coverage, notes, diag)
            coverage.update(requests=api.requests, throttled=api.throttled, waited_s=round(api.waited, 1))
        missing = diag.time_sources.get("fehlt", 0)
        if missing:
            notes.append(f"{missing} Vorgang/Vorgänge ohne Zeitpunkt ({TIME_SOURCE} leer) – als „ungeklärt“ "
                         "vorgelegt, nicht gebucht")
        coverage["time_fields"] = dict(diag.time_sources)
        coverage["diagnostics"] = diag.summary()
        coverage["limits"] = self._limits(events)
        nxt = {"v": CURSOR_VERSION, "from": _iso_ms(started - OVERLAP)} if complete else None
        return K.FetchResult(events=events, cursor=nxt, complete=complete, warnings=notes, skipped=skipped,
                             coverage=coverage, balances=balances)

    def _operations(self, api: _Api, since: datetime | None, coverage: dict[str, Any],
                    diag: _Diag) -> tuple[list[Any], bool, list[str]]:
        """Alle Seiten von ``/operations`` (siehe Modulbeschreibung → Pagination)."""
        variants: list[dict[str, Any]] = [{"page_size": PAGE_SIZE}, {"page_size": PAGE_SIZE_DEFAULT}]
        if since is not None:
            variants = [{**v, "from": _iso_ms(since)} for v in variants] + [{"page_size": PAGE_SIZE_DEFAULT}]
        notes: list[str] = []
        items: list[Any] = []
        seen_ids: set[str] = set()
        seen_cursors: set[str] = set()
        pages = empty = 0
        complete = True
        params = dict(variants[0])
        info: dict[str, Any] = {"page_size": params["page_size"], "end": None, "next_cursor_on_last_page": False}

        def stop(text: str) -> None:
            nonlocal complete
            notes.append(f"Pagination: {text} – Abruf beendet, Abdeckung unklar")
            info["end"] = text
            complete = False

        while True:
            try:
                body = api.get("/operations", params, "Vorgänge (/operations)")
            except K.ConnectorError as e:
                if pages == 0 and e.kind == "data" and "HTTP 400" in e.message and len(variants) > 1:
                    variants.pop(0)
                    if "from" in params and "from" not in variants[0]:
                        notes.append("Zeitfilter nicht akzeptiert – vollständiger Abruf (bekannte Vorgänge werden "
                                     "erkannt)")
                        coverage["mode"] = "vollständig"
                    elif params.get("page_size") != variants[0]["page_size"]:
                        notes.append(f"page_size={params['page_size']} nicht akzeptiert – dokumentierter Standard "
                                     f"{variants[0]['page_size']}")
                    params = dict(variants[0])
                    info["page_size"] = params["page_size"]
                    continue
                if pages > 0 and e.kind in ("rate_limit", "unavailable"):
                    notes.append(f"Abruf nach {pages} Seite(n) abgebrochen: {e.message}")
                    info["end"] = "abgebrochen"
                    complete = False
                    break
                raise
            if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                raise K.ConnectorError("data", "Antwort von /operations ohne Liste „data“ (laut Referenz: data[], "
                                               "has_next_page) – Abruf abgebrochen.")
            diag.seen("page", body)
            page = body["data"]
            pages += 1
            fresh = []
            for o in page:  # erst die ganze Seite verarbeiten, dann über das Ende entscheiden
                oid = _s(o.get("operation_id")) if isinstance(o, dict) else None
                if oid is not None and oid in seen_ids:
                    diag.count("Vorgang mehrfach geliefert")
                    continue
                if oid is not None:
                    seen_ids.add(oid)
                fresh.append(o)
            items.extend(fresh)
            pg = _paging(body)
            info["next_cursor_on_last_page"] = isinstance(body.get("next_cursor"), str) and bool(body["next_cursor"])
            if pg.end:
                info["end"] = "has_next_page=false"
                break
            if pg.problem:
                stop(pg.problem)
                break
            if not page:  # dem Cursor folgen, aber nicht endlos – und nie als Beleg für das Ende
                empty += 1
                diag.count("leere Seite mit has_next_page=true")
                if empty >= MAX_EMPTY_PAGES:
                    stop(f"{empty} leere Seiten in Folge trotz has_next_page=true")
                    break
            elif not fresh:
                stop("Seite enthält nur bereits gelieferte Vorgänge trotz has_next_page=true")
                break
            else:
                empty = 0
            if pg.cursor == params.get("cursor") or pg.cursor in seen_cursors:
                stop("Pagination wiederholt denselben Cursor trotz has_next_page=true")
                break
            if pages >= MAX_PAGES:
                notes.append(f"Mehr als {MAX_PAGES} Seiten – Rest folgt im nächsten Lauf")
                info["end"] = "Seitenlimit"
                complete = False
                break
            seen_cursors.add(pg.cursor)  # type: ignore[arg-type]
            params = {**params, "cursor": pg.cursor}
        coverage.update(pages=pages, operations=len(items), page_size=info["page_size"], pagination=info)
        return items, complete, notes

    def _resolve(self, api: _Api, ops: list[_Op], notes: list[str], extra: Iterable[tuple[str, bool]] = ()) -> None:
        """Asset-/Währungs-UUIDs → Symbol und Art (auch der Gebühren); nur unbekannte IDs werden abgerufen
        (Zwischenspeicher), Assets gesammelt über ``/assets?id=…``."""
        cat = self._catalog()
        refs: dict[str, bool] = {}
        for op in ops:
            for lg in op.legs:
                for ref, fiat in ((lg.ref, lg.kind == "fiat"), (lg.fee_ref, lg.fee_kind == "fiat"),
                                  (lg.trade_fee_ref, lg.trade_fee_kind == "fiat")):
                    if ref:
                        refs[ref] = refs.get(ref, False) or fiat
        for ref, fiat in extra:
            refs[ref] = refs.get(ref, False) or fiat
        unknown_fiat = [r for r, f in refs.items() if f and cat.get(r) is None]
        if unknown_fiat:
            self._load_currencies(api, cat, notes)
        unknown = [r for r, f in refs.items() if not f and cat.get(r) is None]
        if unknown:
            self._load_assets(api, cat, unknown, notes)

        def meta(ref: str | None) -> dict[str, Any] | None:
            return cat.get(ref) if ref else None

        for op in ops:
            for lg in op.legs:
                m = meta(lg.ref)
                lg.symbol = ((m.get("symbol") or "").upper() or None) if m else None
                lg.kind = ("fiat" if lg.kind == "fiat" else _asset_kind(m)) if m and lg.symbol else "unknown"
                if lg.symbol:
                    lg.raw["symbol"] = lg.symbol
                if lg.fee and lg.fee_ref:
                    fm = meta(lg.fee_ref)
                    lg.fee_symbol = ((fm.get("symbol") or "").upper() or None) if fm else None
                if lg.trade_fee and lg.trade_fee_ref:
                    tm = meta(lg.trade_fee_ref)
                    lg.trade_fee_symbol = ((tm.get("symbol") or "").upper() or None) if tm else None

    def _load_currencies(self, api: _Api, cat: Any, notes: list[str]) -> None:
        try:
            body = api.get("/currencies", None, "Währungen (/currencies)")
        except K.ConnectorError as e:
            notes.append(f"Währungsliste nicht abrufbar ({e.message})")
            return
        for c in (body.get("data") if isinstance(body, dict) else None) or []:
            if isinstance(c, dict) and _s(c.get("id")) and c.get("symbol"):
                cat.put(_s(c["id"]), "currency", str(c["symbol"]).upper(), _s(c.get("name")), "FIAT", None)

    def _load_assets(self, api: _Api, cat: Any, refs: list[str], notes: list[str]) -> None:
        """Stammdaten gesammelt: ``/assets?id=<uuid>,<uuid>…`` mit Pagination wie bei ``/operations``."""
        for i in range(0, len(refs), ASSET_CHUNK):
            chunk = refs[i:i + ASSET_CHUNK]
            params: dict[str, Any] = {"id": ",".join(chunk), "page_size": ASSET_CHUNK}
            for _page in range(MAX_ASSET_PAGES):
                try:
                    body = api.get("/assets", params, "Asset-Stammdaten (/assets)")
                except K.ConnectorError as e:
                    notes.append(f"Asset-Stammdaten nicht abrufbar – betroffene Vorgänge sind „ungeklärt“ "
                                 f"({e.message})")
                    return
                data = body.get("data") if isinstance(body, dict) else None
                for a in data if isinstance(data, list) else []:
                    if isinstance(a, dict) and _s(a.get("id")):
                        typ = " ".join(str(x) for x in (a.get("type"), a.get("group")) if x)
                        cat.put(_s(a["id"]), "asset", str(a["symbol"]).upper() if a.get("symbol") else None,
                                _s(a.get("name")), typ or None, _s(a.get("isin")))
                pg = _paging(body) if isinstance(body, dict) else _Paging(problem="keine Liste")
                if pg.end:
                    break
                if pg.problem or pg.cursor == params.get("cursor"):
                    notes.append(f"Asset-Stammdaten: Pagination unklar ({pg.problem or 'Cursor wiederholt'}) – "
                                 "fehlende Assets sind „ungeklärt“")
                    break
                params = {**params, "cursor": pg.cursor}

    def _map(self, ops: list[_Op], now: datetime) -> tuple[list[K.SourceEvent], dict[str, int]]:
        compensated = {lg.compensates for op in ops for lg in op.legs if lg.compensates}
        mapper = _Mapper({c for c in compensated if c}, now)
        events: list[K.SourceEvent] = []
        skipped: dict[str, int] = defaultdict(int)
        for op in ops:
            ev, why = mapper.event(op)
            if ev is not None:
                events.append(ev)
            elif why:
                skipped[f"Bitpanda: {why}"] += 1
        return events, dict(skipped)

    # -- Bestände ---------------------------------------------------------------------------------------
    def _balances(self, api: _Api, ops: list[_Op], full_history: bool, coverage: dict[str, Any],
                  notes: list[str], diag: _Diag) -> list[K.Balance] | None:
        """Bestände laut ``/portfolio`` (``data[].balance.value``) – Grundlage des Bestandsabgleichs mit den
        Portfolia-Buchungen; nach vollständigem Abruf zusätzlich gegen die Summe aller Vorgänge (alle Asset-IDs
        beider Seiten). Nur Hinweis, nie Buchungsersatz; ohne auswertbare Position gilt nichts als geprüft."""
        try:
            body = api.get("/portfolio", None, "Bestände (/portfolio)")
        except K.ConnectorError as e:
            coverage["balances"] = {"checked": False, "note": "Bestandsprüfung übersprungen" + (
                " (Leserecht „Balances“ fehlt – optional)" if e.kind in ("auth", "scope") else f" ({e.message})")}
            return None
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            coverage["balances"] = {"checked": False, "note": "Antwort von /portfolio ohne Liste „data“ – "
                                                              "Bestandsprüfung nicht möglich"}
            notes.append(coverage["balances"]["note"])
            return None
        held: dict[str, Decimal] = defaultdict(Decimal)
        avail: dict[str, Decimal] = defaultdict(Decimal)
        fiat_ref: dict[str, bool] = {}
        unparsed = 0
        for h in data:
            if not isinstance(h, dict):
                unparsed += 1
                continue
            diag.seen("holding", h)
            val, ref, fiat = _amount(h.get("balance"))
            if ref is None:
                aid, cid = _s(h.get("asset_id")), _s(h.get("currency_id"))
                ref, fiat = (aid, False) if aid else ((cid, True) if cid else (None, None))
            if val is None or ref is None:
                unparsed += 1
                continue
            held[ref] += val
            fiat_ref[ref] = bool(fiat)
            av = _amount(h.get("available_balance"))[0]
            if av is not None:
                avail[ref] += av
        if not held:
            note = ("/portfolio ohne auswertbare Position (data[].balance.value)"
                    + (f", {unparsed} Einträge mit anderem Aufbau" if unparsed else "")
                    + " – Bestandsprüfung nicht möglich")
            coverage["balances"] = {"checked": False, "note": note}
            if any(lg.amount for op in ops for lg in op.legs):
                notes.append(note)
            return None
        self._resolve(api, [], notes, extra=fiat_ref.items())
        cat = self._catalog()

        def sym(ref: str) -> str:
            m = cat.get(ref)
            return ((m.get("symbol") or "").upper() if m else "") or f"{PREFIX}:{ref}"

        rows = self._reconcile(ops, held, sym) if full_history else {}
        by_key: dict[str, list[str]] = defaultdict(list)
        for ref in held:
            by_key[sym(ref)].append(ref)
        out: list[K.Balance] = []
        for key, refs in sorted(by_key.items()):
            qty = sum((held[r] for r in refs), ZERO)
            parts = [rows[r] for r in refs if r in rows]
            locked = sum((held[r] - avail[r] for r in refs if r in avail and avail[r] != held[r]), ZERO)
            if locked:
                parts.append(f"davon nicht verfügbar {_q(locked)} (z. B. Staking/Sperre)")
            if len(refs) > 1:
                parts.append(f"Summe aus {len(refs)} Bitpanda-Assets")
            m = cat.get(refs[0])
            out.append(K.Balance(key, qty, (m or {}).get("name"), "; ".join(parts)[:300] or None))
        diffs = [t for t in rows.values() if not t.startswith("Vorgänge stimmen")]
        coverage["balances"] = {"checked": True, "assets": len(held), "unparsed": unparsed,
                                "compared": len(rows) if full_history else 0, "differences": len(diffs),
                                "examples": diffs[:6],
                                **({} if full_history else {"note": "Vergleich mit der Summe der Vorgänge nur nach "
                                                                    "vollständigem Abruf der Historie"})}
        if diffs:
            notes.append(f"Bestandsprüfung: {len(diffs)} Asset(s) weichen von der Summe der Vorgänge ab (Hinweis, "
                         "keine Buchung) – Details unter „Bestände“")
        return out

    @staticmethod
    def _reconcile(ops: list[_Op], held: dict[str, Decimal], sym: Callable[[str], str]) -> dict[str, str]:
        """Bestand laut ``/portfolio`` gegen die Vorgänge, je Asset-ID beider Seiten. Gebühren und Staking sind nicht
        dokumentiert – geprüft wird, welche Lesart den Bestand erklärt (nur Hinweis)."""
        net: dict[str, Decimal] = defaultdict(Decimal)
        stake: dict[str, Decimal] = defaultdict(Decimal)
        fees: dict[str, Decimal] = defaultdict(Decimal)
        tfees: dict[str, Decimal] = defaultdict(Decimal)
        last: dict[tuple[str, str], tuple[datetime, Decimal]] = {}
        seen_trades: set[str] = set()
        for op in ops:
            staking = _norm(op.type) in STAKE_TYPES
            for lg in op.legs:
                if lg.ref and lg.amount is not None and lg.side in ("in", "out"):
                    signed = lg.amount if lg.side == "in" else -lg.amount
                    net[lg.ref] += signed
                    if staking:
                        stake[lg.ref] += signed
                if lg.fee:
                    fees[lg.fee_ref or lg.ref or ""] += lg.fee
                if lg.trade_fee and (lg.trade_id or "") not in seen_trades:
                    seen_trades.add(lg.trade_id or "")
                    if _rate_mode(*_trade_amounts(op, lg)) != "inside":
                        tfees[lg.trade_fee_ref or ""] += lg.trade_fee
                if lg.wallet and lg.balance_ref and lg.ts is not None and lg.balance_after is not None:
                    k = (lg.wallet, lg.balance_ref)
                    if k not in last or last[k][0] < lg.ts:
                        last[k] = (lg.ts, lg.balance_after)
        latest: dict[str, Decimal] = defaultdict(Decimal)
        for (_w, ref), (_t, bal) in last.items():
            latest[ref] += bal
        out: dict[str, str] = {}
        for ref in sorted(set(held) | {r for r, v in net.items() if v}):
            api_qty = held.get(ref, ZERO)
            tol = max(TOL, abs(api_qty) * Decimal("0.000000001"))
            variants = [("", net[ref]), ("Gebühren (fee_amount) zusätzlich abgezogen", net[ref] - fees[ref]),
                        ("Gebühren und Handelsgebühren zusätzlich abgezogen", net[ref] - fees[ref] - tfees[ref])]
            if stake[ref]:
                variants += [(f"{t + ', ' if t else ''}ohne Staking-Umbuchungen", v - stake[ref]) for t, v in
                             list(variants)]
            hit = next((t for t, v in variants if abs(v - api_qty) <= tol), None)
            where = "" if ref in held else " (nicht in /portfolio)"
            if hit is not None:
                out[ref] = "Vorgänge stimmen mit /portfolio überein" + (f" – Lesart: {hit}" if hit else "")
                continue
            text = f"{sym(ref)}: /portfolio {_q(api_qty)}{where}, Vorgänge {_q(net[ref])} (Δ {_q(api_qty - net[ref])})"
            if ref in latest:
                text += f", letzter Saldo laut Vorgängen {_q(latest[ref])}"
            out[ref] = text
        return out

    @staticmethod
    def _limits(events: list[K.SourceEvent]) -> list[str]:
        out = []
        n_review = sum(1 for ev in events if ev.lines and ev.lines[0].kind == M.REVIEW)
        if n_review:
            out.append(f"{n_review} Vorgänge ohne eindeutige Abbildung („ungeklärt“)")
        return out


def _trade_amounts(op: _Op, src: _Leg) -> tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None]:
    """(Fiat-Betrag, Menge, rate, rate_with_fee) des Handels, zu dem ``src`` gehört (gleiche trade_id)."""
    legs = [lg for lg in op.legs if lg.trade_id == src.trade_id] if src.trade_id else op.legs
    fiat = next((lg.amount for lg in legs if lg.kind == "fiat"), None)
    qty = next((lg.amount for lg in legs if lg.kind != "fiat"), None)
    return fiat, qty, src.rate, src.rate_with_fee
