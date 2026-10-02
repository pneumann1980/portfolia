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
    Cursor-Pagination mit Schleifen- und Seitenendeschutz (leere Seite = Ende, Cursor-Echo → letzte Kennung). Ist
    das Seitenende nicht eindeutig erkennbar oder bricht der Abruf ab, gilt er als unvollständig: Status
    „teilweise“, der Abrufstand rückt nicht vor.

Antwortformat
    Beträge und Gebühren als Text/Zahl oder als Objekt ``{"value", "currency_id"|"asset_id"}`` (aktuelles Format);
    Zeitpunkt am Vorgang oder an seinen Teilen (frühester), erkannt über übliche Feldnamen bzw. jedes Feld mit
    Zeitnamen (ohne Änderungs-/Ablaufzeiten). Verwendetes Feld und Originalantwort bleiben in den Rohdaten.

Abbildung – nur eindeutige Fälle, sonst „ungeklärt“ mit Grund (nie geraten, nie still verworfen)
    * Kauf: ein Fiat-Ausgang + ein Krypto-Eingang (auch Sparplan) · Verkauf: Krypto-Ausgang + Fiat-Eingang
    * Swap: Verkaufs- und Kauf-Paar über dieselbe Fiat-Währung → Verkauf + Kauf mit den Euro-Werten
    * Einzahlung/Auszahlung: ein Eingang bzw. Ausgang (Fiat oder Krypto) mit Vorgangsart „deposit“/„withdraw…“;
      Sparplan-Einzahlung (Transaktionsart „deposit“) → Zugang
    * Erträge: ein Krypto-Eingang mit Ertragsart (reward, staking reward, passive earn reward, onetime reward, …) →
      Zugang mit Ertrags-Tag
    * Token-Umstellung (merger, migration): Krypto-Ausgang + Krypto-Eingang → Umstellung, prüfbedürftig
    * Gebühren: eigene Gebühren-Teile (Transaktionsart „fee“) → Gebührenzeile; Gebühren an Haupt-Teilen (auch in
      eigener Währung) werden übernommen, aber als prüfbedürftig markiert (ob der Betrag sie enthält, ist nicht
      dokumentiert)
    * interne Umbuchungen (gleiches Asset, gleicher Betrag, Ein- und Ausgang) und Staking-Umbuchungen (stake,
      unstake) → ohne Buchung, gezählt
    * ungeklärt: Korrekturen/Stornos (``compensates``) samt storniertem Vorgang, Tausch Krypto↔Krypto ohne Euro-Teile,
      Aktien/ETFs, Edelmetalle, Indizes, unbekannte Assets oder Vorgangsarten, fehlende Richtung oder Zeitangabe
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
CURSOR_VERSION = 2  # 2: Beträge als Objekt, Zeitpunkt auch an den Teilen – ältere Abrufstände → Neuabruf
PREFIX = "bitpanda"
PAGE_SIZE = 100
MAX_PAGES = 2000
OVERLAP = timedelta(days=2)  # spät gutgeschriebene Vorgänge – bekannte werden erkannt
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
RETRIES = 3
WAIT_BUDGET_S = 120.0
MAX_RETRY_AFTER_S = 60.0
COMMON_PAGE_SIZES = frozenset({10, 20, 25, 50, 100, 200, 250, 500, 1000})
# Ertragsarten (normalisierte Vorgangsart → Tag des Datenvertrags); nur Krypto-Zugänge
INCOME_TYPES = {"reward": "reward", "rewards": "reward", "stakingreward": "staking", "stakingrewards": "staking",
                "passiveearnreward": "reward", "earnreward": "reward", "onetimereward": "bonus", "bonus": "bonus",
                "cashback": "cashback", "airdrop": "airdrop", "interest": "interest", "lendingreward": "lending"}
REWARD_TYPES = set(INCOME_TYPES)
STAKE_TYPES = {"stake", "unstake", "staking", "unstaking", "earnstake", "earnunstake"}
CONVERSION_WORDS = ("merger", "migration", "rename", "redenomination", "conversion")
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
    """Zeitpunkt aus ISO-8601, Unix-Zeit (Sekunden/Millisekunden, auch als Text) oder einem Zeitobjekt
    (z. B. ``{"date_iso8601": …, "unix": …}``)."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, dict):
        for k in ("date_iso8601", "iso8601", "iso", "datetime", "date_time", "value", "timestamp", "unix", "epoch"):
            if k in v and (dt := _ts(v[k])) is not None:
                return dt
        return None
    if isinstance(v, int | float | Decimal):
        n = float(v)
        if not (1e8 < n < 1e14):  # plausibler Bereich (1973 … 5138), sonst keine Zeitangabe
            return None
        try:
            return datetime.fromtimestamp(n / 1000 if n > 1e11 else n, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(v).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{9,14}(\.\d+)?", s):
        return _ts(Decimal(s))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC)


# Zeitfelder in fester Rangfolge: Ausführung/Gutschrift vor Anlage; Änderungs- oder Ablaufzeiten nie
TIME_KEYS = ("timestamp", "time", "ts", "occurred_at", "occurredAt", "executed_at", "executedAt", "credited_at",
             "creditedAt", "booked_at", "bookedAt", "settled_at", "settledAt", "completed_at", "completedAt",
             "processed_at", "processedAt", "effective_at", "effectiveAt", "transaction_time", "transactionTime",
             "operation_time", "operationTime", "date", "datetime", "created_at", "createdAt", "created")
_TIME_HINT = re.compile(r"(time|date|_at$|At$|stamp)", re.I)
_TIME_EXCLUDE = re.compile(r"(updated|modified|expir|valid|last|next|deadline|until)", re.I)


def _find_time(d: Any) -> tuple[datetime | None, str | None]:
    """(Zeitpunkt, Feldname) eines Vorgangs bzw. Teils: bekannte Felder in Rangfolge, sonst jedes Feld, dessen Name
    nach einer Zeitangabe klingt und dessen Wert sich als Zeitpunkt lesen lässt (Name wird mitgeliefert)."""
    if not isinstance(d, dict):
        return None, None
    for k in TIME_KEYS:
        if k in d and (dt := _ts(d[k])) is not None:
            return dt, k
    for k in sorted(d):
        if _TIME_HINT.search(k) and not _TIME_EXCLUDE.search(k) and (dt := _ts(d[k])) is not None:
            return dt, k
    return None, None


def _val(v: Any) -> tuple[Decimal | None, str | None]:
    """Betrag und Asset-/Währungs-ID: Text/Zahl oder Betragsobjekt ``{"value": …, "currency_id"|"asset_id": …}``."""
    if isinstance(v, dict):
        amount = _dec(_get(v, "value", "amount", "quantity", "qty"))
        ref = _get(v, "currency_id", "currencyId", "fiat_id", "fiatId", "asset_id", "assetId")
        return amount, _s(ref)
    return _dec(v), None


def _is_fiat_obj(v: Any) -> bool:
    return isinstance(v, dict) and _get(v, "currency_id", "currencyId", "fiat_id", "fiatId") is not None


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
    fee_ref: str | None = None  # Asset/Währung der Gebühr, falls angegeben (sonst wie der Betrag)
    fee_kind: str = "?"
    fee_symbol: str | None = None

    @property
    def is_fee(self) -> bool:
        return "fee" in self.ttype

    @property
    def fee_sym(self) -> str | None:
        return self.fee_symbol if self.fee_ref and self.fee_ref != self.ref else self.symbol


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
                r.fee_sym, r.fee_qty = lg.fee_sym, lg.fee
                if r.fee_sym is None:
                    r.review = "Gebühr in einem unbekannten Asset – bitte mit dem Bitpanda-Beleg vergleichen"
                r.review = r.review or (f"Gebühr {_s(lg.fee)} {lg.fee_sym}: ob der Betrag sie bereits enthält, ist "
                                        "nicht dokumentiert – bitte mit dem Bitpanda-Beleg vergleichen")
            return r

        trade_like = not otype or any(w in otype for w in _TRADE_WORDS)
        conflicting = any(w in otype for w in _NOT_TRADE)
        if otype in STAKE_TYPES and len(main) == 1 and main[0].kind == "crypto" and not fees:
            return None, "Umbuchung in bzw. aus Bitpanda Staking (kein Zu- oder Abgang)"
        if len(ins) == 2 and len(outs) == 2 and {lg.ttype for lg in main} == {"buy", "sell"}:
            pair = {(lg.ttype, lg.side): lg for lg in main}
            so, si, bo, bi = (pair.get(k) for k in (("sell", "out"), ("sell", "in"), ("buy", "out"), ("buy", "in")))
            if so and si and bo and bi and so.kind == "crypto" and si.kind == "fiat" and bo.kind == "fiat" \
                    and bi.kind == "crypto" and si.symbol == bo.symbol:
                note = f"Tausch {so.symbol} → {bi.symbol} über {si.symbol} (Bitpanda Swap)"
                sell = with_fee(rec(M.TRADE, out_sym=so.symbol, out_qty=so.amount, in_sym=si.symbol, in_qty=si.amount,
                                    value=si.amount, value_ccy=si.symbol, note=note), so, si)
                buy = with_fee(rec(M.TRADE, out_sym=bo.symbol, out_qty=bo.amount, in_sym=bi.symbol, in_qty=bi.amount,
                                   value=bo.amount, value_ccy=bo.symbol, note=note), bo, bi)
                return K.SourceEvent(op.key, ts, [sell, buy, *fee_lines], label), None
        if len(ins) == 1 and len(outs) == 1 and any(w in otype for w in CONVERSION_WORDS):
            i, o = ins[0], outs[0]
            if i.kind == "crypto" and o.kind == "crypto":
                r = with_fee(rec(M.CONVERSION, out_sym=o.symbol, out_qty=o.amount, in_sym=i.symbol, in_qty=i.amount,
                                 note=f"Token-Umstellung {o.symbol} → {i.symbol} ({op.type})"), o, i)
                r.review = r.review or ("Token-Umstellung: Einstand und Anschaffungsdatum gehen auf das neue Asset "
                                        "über – Verhältnis und Asset-Zuordnung bitte prüfen")
                return K.SourceEvent(op.key, ts, [r, *fee_lines], label), None
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
            if otype in INCOME_TYPES and i.kind == "crypto":
                r = with_fee(rec(M.DEPOSIT, in_sym=i.symbol, in_qty=i.amount, tag=INCOME_TYPES[otype]), i)
                return K.SourceEvent(op.key, ts, [r, *fee_lines], label), None
            if ("deposit" in otype or (i.ttype == "deposit" and "saving" in otype)) \
                    and not any(w in otype for w in _INCOME_WORDS):
                r = with_fee(rec(M.DEPOSIT, in_sym=i.symbol, in_qty=i.amount,
                                 note="Einzahlung für den Sparplan" if "saving" in otype else None), i)
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
        """Verworfene Vorgänge erneut liefern: vollständiger Neuabruf. Die Bitpanda-Historie ist klein (wenige
        Seiten), bekannte Vorgänge werden erkannt – und der Zeitpunkt eines verworfenen Vorgangs ist nicht immer
        verlässlich (ohne Zeitangabe trägt er den Abrufzeitpunkt)."""
        return None

    def fetch(self, cfg: K.SourceConfig, secret: K.Secret, cursor: dict[str, Any] | None) -> K.FetchResult:
        started = datetime.now(UTC)
        notes: list[str] = []
        if cursor and cursor.get("v") != CURSOR_VERSION:
            cursor = None  # Abrufstand einer älteren Auswertung → Vorgänge vollständig neu abrufen und auswerten
            notes.append("Auswertung der Bitpanda-Daten verbessert – alle Vorgänge werden neu abgerufen (bereits "
                         "übernommene werden erkannt)")
        since = _ts((cursor or {}).get("from"))
        coverage: dict[str, Any] = {"api": BASE_URL, "mode": "inkrementell" if since else "vollständig",
                                    "from": _iso_z(since) if since else None, "to": _iso_z(started)}
        with self._client() as client:
            api = _Api(secret.reveal(), client, self.sleep)
            items, complete, page_notes = self._operations(api, since, coverage)
            notes += page_notes
            ops = [self._parse(o) for o in items if isinstance(o, dict)]
            if len(ops) != len(items):
                notes.append(f"{len(items) - len(ops)} Einträge ohne erkennbare Struktur übersprungen")
                complete = False
            fields: dict[str, int] = defaultdict(int)
            for op in ops:
                fields[op.raw.get("time_field") or "–"] += 1
            coverage["time_fields"] = dict(fields)
            without = fields.get("–", 0)
            if without:
                sample = next((op.raw for op in ops if not op.raw.get("time_field")), {})
                notes.append(f"{without} Vorgänge ohne erkennbaren Zeitpunkt (Felder: "
                             + ", ".join(sample.get("fields") or []) + ")")
            self._resolve(api, ops, notes)
            events, skipped = self._map(ops)
            if complete and since is None:
                self._balances(api, ops, coverage, notes)
            coverage.update(requests=api.requests, throttled=api.throttled, waited_s=round(api.waited, 1))
        coverage["limits"] = self._limits(events)
        nxt = {"v": CURSOR_VERSION, "from": _iso_z(started - OVERLAP)} if complete else None
        # vollständige Historie: unbearbeitete offene Prüf-Stapel dieser Quelle durch die neue Auswertung ersetzen
        return K.FetchResult(events=events, cursor=nxt, complete=complete, warnings=notes, skipped=skipped,
                             coverage=coverage, refresh_open=complete and since is None)

    def _operations(self, api: _Api, since: datetime | None,
                    coverage: dict[str, Any]) -> tuple[list[Any], bool, list[str]]:
        """Alle Seiten von ``/operations``. Dokumentiert: ``cursor`` der Antwort als ``cursor`` der nächsten Anfrage,
        fehlt am Ende. Beobachtet: auch die letzte Seite kann einen Cursor tragen – eine leere Seite beendet den
        Abruf daher immer. Wiederholt die API den gesendeten Cursor, gilt die Kennung des letzten Vorgangs als
        Fortsetzungspunkt (der Cursor bezeichnet laut Doku ein Element der Liste); liefert eine Seite nur bereits
        bekannte Vorgänge, endet der Abruf als unvollständig (Schleifenschutz)."""
        base: dict[str, Any] = {"pageSize": PAGE_SIZE}
        if since is not None:
            base["from"] = _iso_z(since)
        variants = [base, {k: v for k, v in base.items() if k != "pageSize"}, {}]
        notes: list[str] = []
        items: list[Any] = []
        seen_ids: set[str] = set()
        seen_cursors: set[str] = set()
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
            pages += 1
            if not page:
                break  # leere Seite: Ende – auch wenn die Antwort noch einen Cursor trägt
            ids = [_s(_get(o, "id", "operation_id", "operationId")) if isinstance(o, dict) else None for o in page]
            fresh = [o for o, i in zip(page, ids, strict=True) if i is None or i not in seen_ids]
            if not fresh:
                notes.append("Pagination wiederholt bereits abgerufene Vorgänge – Abruf beendet, Abdeckung unklar")
                complete = False
                break
            items.extend(fresh)
            seen_ids.update(i for i in ids if i)
            nxt, known = _next(body)
            sent = params.get("cursor")
            if nxt and (nxt == sent or nxt in seen_cursors):
                last = next((i for i in reversed(ids) if i), None)
                nxt = last if last and last != sent and last not in seen_cursors else None
                if nxt is None:
                    notes.append("Pagination wiederholt denselben Cursor – Abruf beendet, Abdeckung unklar")
                    complete = False
                    break
                coverage["cursor_mode"] = "Kennung des letzten Vorgangs"
            if nxt:
                if pages >= MAX_PAGES:
                    notes.append(f"Mehr als {MAX_PAGES} Seiten – Rest folgt im nächsten Lauf")
                    complete = False
                    break
                seen_cursors.add(nxt)
                params = {**params, "cursor": nxt}
                continue
            if not known and (len(page) in COMMON_PAGE_SIZES or len(page) == requested):
                notes.append("Seitenende nicht eindeutig (volle Seite ohne Cursor) – Abdeckung unklar")
                complete = False
            break
        coverage.update(pages=pages, operations=len(items), page_size=requested or None)
        return items, complete, notes

    def _parse(self, o: dict[str, Any]) -> _Op:
        """Vorgang → Teile. Beträge als Text/Zahl oder als Objekt ``{"value", "currency_id"|"asset_id"}``; Zeitpunkt
        am Vorgang oder – fehlt er dort – an den Teilen (frühester Zeitpunkt); verwendete Felder in ``raw``."""
        op_id = _get(o, "id", "operation_id", "operationId")
        op_type = str(_get(o, "type", "operation_type", "operationType") or "")
        ts, ts_key = _find_time(o)
        txs = o.get("transactions")
        single = not isinstance(txs, list)
        if single:
            txs = [o]  # Vorgang ohne Teilliste: der Vorgang selbst ist der einzige Teil
        legs = []
        leg_times: list[tuple[datetime, str]] = []
        for t in txs:
            if not isinstance(t, dict):
                continue
            amount_obj = _get(t, "amount", "asset_amount", "assetAmount", "quantity")
            fee_obj = _get(t, "fee", "fee_amount", "feeAmount")
            amount, amount_ref = _val(amount_obj)
            fee, fee_ref = _val(fee_obj)
            flow = str(_get(t, "flow", "direction", "in_or_out", "inOrOut", "side") or "").lower()
            side = "in" if flow in ("incoming", "in", "credit") else "out" if flow in ("outgoing", "out", "debit") \
                else "?"
            if side == "?" and amount is not None and amount < 0:
                side = "out"
            if amount is not None:
                amount = abs(amount)
            cur_id = _s(_get(t, "currency_id", "currencyId", "fiat_id", "fiatId"))
            asset_id = _s(_get(t, "asset_id", "assetId"))
            ref = cur_id or asset_id or amount_ref
            fiat = bool(cur_id) or (not asset_id and _is_fiat_obj(amount_obj))
            fee_fiat = _is_fiat_obj(fee_obj)
            if not single:
                lt, lk = _find_time(t)
                if lt is not None:
                    leg_times.append((lt, f"transactions[].{lk}"))
            legs.append(_Leg(
                id=None if single else _s(_get(t, "transaction_id", "transactionId", "id")),
                side=side, amount=amount, fee=abs(fee or Decimal(0)),
                ref=ref, kind="fiat" if fiat else "?", symbol=None,
                trade_id=_s(_get(t, "trade_id", "tradeId")), compensates=_s(_get(t, "compensates")),
                ttype=_norm(_get(t, "transaction_type", "transactionType", "kind")),
                fee_ref=fee_ref if fee_ref and fee_ref != ref else None, fee_kind="fiat" if fee_fiat else "?",
                raw={"id": _s(_get(t, "transaction_id", "transactionId", "id")), "flow": flow or None,
                     "amount": _s(amount), "fee": _s(fee), "asset_id": asset_id, "currency_id": cur_id,
                     "ref": ref, "fee_ref": fee_ref, "trade_id": _s(_get(t, "trade_id", "tradeId")),
                     "transaction_type": _s(_get(t, "transaction_type", "transactionType")),
                     "compensates": _s(_get(t, "compensates"))}))
        if ts is None and leg_times:
            ts, ts_key = min(leg_times)
        fields = sorted(str(k) for k in o)
        leg_fields = sorted({str(k) for t in (txs if not single else []) if isinstance(t, dict) for k in t})
        raw = {"operation_id": _s(op_id), "type": op_type or None, "timestamp": _iso_z(ts) if ts else None,
               "time_field": ts_key, "fields": fields, "transaction_fields": leg_fields,
               "transactions": [lg.raw for lg in legs], "api": _plain(o)}
        return _Op(id=_s(op_id), type=op_type, ts=ts, legs=legs, raw=raw)

    def _resolve(self, api: _Api, ops: list[_Op], notes: list[str]) -> None:
        """Asset-/Währungs-UUIDs → Symbol und Art (auch der Gebühr); nur unbekannte IDs werden abgerufen
        (Zwischenspeicher)."""
        cat = self._catalog()
        state = {"currencies": False, "failed": None}

        def lookup(ref: str, fiat: bool) -> dict[str, Any] | None:
            meta = cat.get(ref)
            if meta is None and fiat and not state["currencies"]:
                state["currencies"] = True
                self._load_currencies(api, cat, notes)
                meta = cat.get(ref)
            if meta is None and not fiat and state["failed"] is None:
                try:
                    meta = self._load_asset(api, cat, ref)
                except K.ConnectorError as e:
                    if e.kind in ("auth", "scope", "expired"):
                        state["failed"] = e.message
                        notes.append("Asset-Stammdaten nicht abrufbar – betroffene Vorgänge sind „ungeklärt“ "
                                     f"({e.message})")
                    elif e.kind in ("rate_limit", "unavailable"):
                        state["failed"] = e.message
                        notes.append(f"Asset-Stammdaten vorübergehend nicht abrufbar ({e.message})")
                    meta = None
            return meta

        for op in ops:
            for lg in op.legs:
                if not lg.ref:
                    lg.kind = "unknown"
                else:
                    meta = lookup(lg.ref, lg.kind == "fiat")
                    if meta is None:
                        lg.kind = "unknown"
                    else:
                        lg.symbol = (meta.get("symbol") or "").upper() or None
                        lg.kind = "fiat" if lg.kind == "fiat" else _asset_kind(meta)
                        if lg.symbol is None:
                            lg.kind = "unknown"
                        lg.raw["symbol"] = lg.symbol
                if lg.fee_ref and lg.fee:
                    meta = lookup(lg.fee_ref, lg.fee_kind == "fiat")
                    lg.fee_symbol = (meta.get("symbol") or "").upper() or None if meta else None
                    lg.raw["fee_symbol"] = lg.fee_symbol

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
                    if lg.symbol:
                        sym[lg.ref] = lg.symbol
                if lg.fee and (lg.fee_ref or lg.ref):
                    net[lg.fee_ref or lg.ref] -= lg.fee  # type: ignore[index]
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
