"""KI-Zugang für News-Analyse und Tagesbericht: Anbieter (Claude, Google Gemini), Kontingent- und Budgetkontrolle.

Grundsätze
----------
* **Nur öffentliche Daten:** Gesendet werden Artikeltexte, Titel, Quellen und Asset-Namen – nie Stückzahlen, Werte,
  Gewichte, Kontonamen, Transaktionen oder Wallet-Adressen (Aufrufer sind dafür verantwortlich; die Tests prüfen es).
* **Gemini (Standard: Free Tier):** Das Modell wird aus einer Liste kostenlos nutzbarer Flash-/Flash-Lite-Modelle
  gewählt. Der Zugriff läuft über die dokumentierte REST-Schnittstelle ``generateContent`` (kein SDK, kein Schlüssel in
  der URL). Lokale Zähler (Anfragen/Minute, Tokens/Minute, Anfragen/Tag, Tokens/Tag) halten die Last **unter** den
  konfigurierten Grenzen; sie sind **keine Garantie**, dass Google die Nutzung als kostenlos wertet – Kontingent und
  Abrechnung gelten je Google-Projekt (ein Schlüssel eines Projekts mit Abrechnung wird abgerechnet). Meldet Google
  ``429``/``RESOURCE_EXHAUSTED``, pausiert Portfolia den Anbieter kontrolliert (Minutenlimit: kurz, Tageslimit: bis
  Mitternacht Pacific Time) und wechselt **nie** selbst auf ein anderes Modell oder einen anderen Anbieter.
* **Antworten sind Daten:** JSON wird angefordert und vom Aufrufer gegen ein Schema geprüft.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import httpx

from app.context import AppContext
from app.util.timeutil import iso, parse_iso, today_local

log = logging.getLogger(__name__)

PACIFIC = ZoneInfo("America/Los_Angeles")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# Kostenlos nutzbare Flash-/Flash-Lite-Modelle laut https://ai.google.dev/gemini-api/docs/pricing (Stand der Prüfung:
# 2026-10). Standard ist ein Flash-Lite-Modell – günstigstes Free-Tier-Modell mit ausreichender Qualität für Einordnung.
GEMINI_MODELS = ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite", "gemini-3.8-flash",
                 "gemini-3.6-flash", "gemini-2.5-flash")
GEMINI_DEFAULT = "gemini-3.5-flash-lite"
_MODEL_RE = re.compile(r"^gemini-[a-z0-9][a-z0-9.\-]{2,40}$")
_PAID_HINTS = ("pro", "image", "tts", "live", "audio", "embedding", "robotics", "omni", "argon", "nano", "lyria",
               "transcribe", "translate")


class AiError(Exception):
    """Basisklasse; ``code`` = stabiler Kurzcode, ``text`` = bereinigte Meldung für Protokoll und Oberfläche."""

    code = "error"

    def __init__(self, text: str = "") -> None:
        super().__init__(text or self.code)
        self.text = text or self.code


class QuotaExceeded(AiError):
    code = "quota"

    def __init__(self, text: str, retry_after: float | None = None, scope: str = "unknown") -> None:
        super().__init__(text)
        self.retry_after = retry_after
        self.scope = scope  # minute | day | unknown


class AiUnavailable(AiError):
    code = "unavailable"


class AiRejected(AiError):
    """Dauerhaft abgelehnt (Schlüssel, Modell, Sicherheitsfilter): Wiederholen ohne Änderung ist sinnlos."""

    code = "rejected"


class BudgetExceeded(AiError):
    code = "budget"


class Paused(AiError):
    code = "paused"

    def __init__(self, text: str, until: datetime | None = None) -> None:
        super().__init__(text)
        self.until = until


@dataclass
class AiResult:
    text: str
    input_tokens: int
    output_tokens: int
    provider: str
    model: str


def model_allowed(model: str) -> bool:
    """Gemini-Modell: nur Flash/Flash-Lite ohne Spezialvarianten (Bild, Audio, Pro …)."""
    m = model.strip().lower()
    return bool(_MODEL_RE.match(m)) and "flash" in m and not any(h in m for h in _PAID_HINTS)


def pacific_day(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(PACIFIC).date().isoformat()


def next_pacific_midnight(now: datetime | None = None) -> datetime:
    n = (now or datetime.now(UTC)).astimezone(PACIFIC)
    nxt = datetime.combine(n.date() + timedelta(days=1), datetime.min.time(), tzinfo=PACIFIC)
    return nxt.astimezone(UTC)


def _redact(text: str, *secrets: str | None) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return re.sub(r"(?i)(key|token)=[A-Za-z0-9_\-]{12,}", r"\1=***", text)[:300]


class GeminiProvider:
    name = "gemini"

    def __init__(self, client: httpx.Client, api_key: str, model: str, timeout: float = 90.0) -> None:
        self.client = client
        self.key = api_key
        self.model = model
        self.timeout = timeout

    def generate(self, system: str, user: str, json_mode: bool, max_tokens: int) -> AiResult:
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"maxOutputTokens": int(max_tokens), "temperature": 0.2},
        }
        if json_mode:
            # Die Struktur steht im Systemtext (siehe Aufrufer); der MIME-Typ erzwingt gültiges JSON. Die Prüfung
            # gegen das Schema erfolgt clientseitig – unabhängig von Schema-Dialekten einzelner Modellgenerationen.
            body["generationConfig"]["responseMimeType"] = "application/json"
        try:
            r = self.client.post(GEMINI_URL.format(model=self.model), json=body, timeout=self.timeout,
                                 headers={"x-goog-api-key": self.key, "Content-Type": "application/json"})
        except httpx.TimeoutException as e:
            raise AiUnavailable("Gemini: Zeitüberschreitung") from e
        except httpx.HTTPError as e:
            raise AiUnavailable(f"Gemini: nicht erreichbar ({type(e).__name__})") from e
        return self._parse(r)

    def _parse(self, r: httpx.Response) -> AiResult:
        if r.status_code != 200:
            self._raise(r)
        try:
            data = r.json()
        except ValueError as e:
            raise AiUnavailable("Gemini: Antwort ist kein JSON") from e
        fb = (data.get("promptFeedback") or {}).get("blockReason")
        cands = data.get("candidates") or []
        if fb or not cands:
            raise AiRejected(f"Gemini: Anfrage abgelehnt ({fb or 'keine Antwort'})")
        cand = cands[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict) and not p.get("thought"))
        finish = str(cand.get("finishReason") or "")
        u = data.get("usageMetadata") or {}
        inp = int(u.get("promptTokenCount") or 0)
        out = int(u.get("candidatesTokenCount") or 0) + int(u.get("thoughtsTokenCount") or 0)
        if finish == "MAX_TOKENS":
            raise AiUnavailable("Gemini: Antwort abgeschnitten (max. Ausgabelänge erreicht)")
        if finish in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION") or not text.strip():
            raise AiRejected(f"Gemini: Antwort blockiert ({finish or 'leer'})")
        return AiResult(text, inp, out, self.name, self.model)

    def _raise(self, r: httpx.Response) -> None:
        status = r.status_code
        err: dict[str, Any] = {}
        with contextlib.suppress(ValueError):
            err = (r.json() or {}).get("error") or {}
        msg = _redact(str(err.get("message") or ""), self.key)
        gstatus = str(err.get("status") or "")
        if status == 429 or gstatus == "RESOURCE_EXHAUSTED":
            retry, scope = None, "unknown"
            for d in err.get("details") or []:
                if not isinstance(d, dict):
                    continue
                delay = d.get("retryDelay")
                if isinstance(delay, str) and delay.endswith("s"):
                    with contextlib.suppress(ValueError):
                        retry = float(delay[:-1])
                for v in d.get("violations") or []:
                    qid = str((v or {}).get("quotaId", ""))
                    if "PerDay" in qid:
                        scope = "day"
                    elif "PerMinute" in qid and scope != "day":
                        scope = "minute"
            if scope == "unknown" and re.search(r"(?i)per day|daily|requests per day", msg):
                scope = "day"
            raise QuotaExceeded(f"Gemini: Kontingent erschöpft ({scope})", retry, scope)
        if status in (500, 502, 503, 504) or gstatus in ("UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED"):
            raise AiUnavailable(f"Gemini: vorübergehend nicht verfügbar (HTTP {status})")
        if status in (400, 401, 403, 404):
            raise AiRejected(f"Gemini: Anfrage abgelehnt (HTTP {status} {gstatus}) {msg}".strip())
        raise AiUnavailable(f"Gemini: HTTP {status}")


# ----------------------------------------------------------------------------------------------------
# Gateway: Anbieterwahl, Budgets, Takt, Pausen, Nutzungsprotokoll
# ----------------------------------------------------------------------------------------------------

class AiGateway:
    _lock = threading.Lock()
    # Anbieter → [Zeitpunkt, Tokens] je Anfrage der letzten 60 s (prozessweit, von allen Gateways geteilt)
    _recent: ClassVar[dict[str, deque[list[float]]]] = {}

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.db = ctx.db
        self.s = ctx.settings

    # -- Konfiguration ---------------------------------------------------------------------------------
    def provider_name(self) -> str:
        return "gemini" if str(self.s.get("llm.provider", "claude")) == "gemini" else "claude"

    def key_present(self, provider: str | None = None) -> bool:
        sec = self.ctx.config.secrets
        return bool(sec.gemini_api_key if (provider or self.provider_name()) == "gemini" else sec.anthropic_api_key)

    def enabled(self) -> bool:
        return bool(self.s.get("llm.enabled", False)) and self.key_present()

    def model(self) -> str:
        if self.provider_name() == "gemini":
            m = str(self.s.get("llm.gemini_model") or GEMINI_DEFAULT)
            return m if model_allowed(m) else GEMINI_DEFAULT
        return str(self.s.get("llm.model") or "claude-opus-5")

    def limits(self) -> dict[str, int]:
        g = lambda k, d: max(0, int(self.s.get(k, d) or 0))  # noqa: E731
        unlimited = bool(self.s.get("llm.gemini_tokens_unlimited", False))  # 0 = ohne Token-Tagesgrenze
        return {"rpm": g("llm.gemini_rpm", 5), "tpm": g("llm.gemini_tpm", 100_000), "rpd": g("llm.gemini_rpd", 100),
                "tpd": 0 if unlimited else g("llm.gemini_daily_token_budget", 200_000)}

    # -- Nutzung ---------------------------------------------------------------------------------------
    def day_key(self, provider: str | None = None) -> str:
        return pacific_day() if (provider or self.provider_name()) == "gemini" else today_local().isoformat()

    def usage(self, provider: str | None = None, day: str | None = None) -> dict[str, int]:
        p = provider or self.provider_name()
        r = self.db.q1("SELECT * FROM ai_usage WHERE day=? AND provider=?", (day or self.day_key(p), p))
        return {k: int(r[k]) if r else 0 for k in ("requests", "input_tokens", "output_tokens", "errors", "quota_hits")}

    def tokens_left(self) -> int | None:
        """Restliche Tokens heute; ``None`` = unbegrenzt (nur Gemini, ausdrücklich gewählt)."""
        p = self.provider_name()
        if p == "claude":
            from app.news.llm import LlmService

            return LlmService(self.ctx).budget_left()
        tpd = self.limits()["tpd"]
        if not tpd:
            return None
        u = self.usage(p)
        return tpd - u["input_tokens"] - u["output_tokens"]

    def requests_left(self) -> int | None:
        if self.provider_name() != "gemini":
            return None
        return self.limits()["rpd"] - self.usage("gemini")["requests"]

    def _record(self, provider: str, inp: int = 0, out: int = 0, *, ok: bool = True, quota: bool = False) -> None:
        self.db.x(
            """INSERT INTO ai_usage(day, provider, requests, input_tokens, output_tokens, errors, quota_hits)
               VALUES (?,?,?,?,?,?,?) ON CONFLICT(day, provider) DO UPDATE SET requests=requests+excluded.requests,
                   input_tokens=input_tokens+excluded.input_tokens, output_tokens=output_tokens+excluded.output_tokens,
                   errors=errors+excluded.errors, quota_hits=quota_hits+excluded.quota_hits""",
            (self.day_key(provider), provider, 1, inp, out, 0 if ok else 1, 1 if quota else 0))

    # -- Pause -------------------------------------------------------------------------------------------
    def paused_until(self, provider: str | None = None) -> datetime | None:
        st = self.db.get_state(f"ai.pause.{provider or self.provider_name()}")
        until = parse_iso(st.get("until")) if isinstance(st, dict) else None
        return until if until and until > datetime.now(UTC) else None

    def _pause(self, provider: str, until: datetime, reason: str) -> None:
        self.db.set_state(f"ai.pause.{provider}", {"until": iso(until), "reason": reason})

    def last_error(self) -> str:
        st = self.db.get_state("ai.last_error")
        return str(st.get("text", "")) if isinstance(st, dict) else ""

    def _error(self, text: str) -> None:
        self.db.set_state("ai.last_error", {"text": text[:300], "at": iso(datetime.now(UTC))})

    # -- Aufruf --------------------------------------------------------------------------------------------
    def generate(self, system: str, user: str, schema: dict[str, Any] | None, max_tokens: int,
                 json_mode: bool = False) -> AiResult:
        """Ein Aufruf mit allen Kontrollen. ``schema`` (nur Claude: erzwungenes JSON-Schema) oder ``json_mode``
        (Gemini: JSON-MIME-Typ; die Prüfung gegen das Schema macht der Aufrufer)."""
        if not self.enabled():
            raise AiRejected("KI ist nicht aktiviert oder der Schlüssel fehlt")
        provider = self.provider_name()
        until = self.paused_until(provider)
        if until:
            raise Paused("Anbieter pausiert", until)
        if provider == "gemini":
            return self._gemini(system, user, json_mode or schema is not None, max_tokens)
        return self._claude(system, user, schema, max_tokens)

    def _claude(self, system: str, user: str, schema: dict[str, Any] | None, max_tokens: int) -> AiResult:
        from app.news.llm import LlmService

        llm = LlmService(self.ctx)
        before = llm.usage_today()
        est = len(user) // 3 + len(system) // 3 + max_tokens
        if est > llm.budget_left():
            raise BudgetExceeded("Claude: Tagesbudget reicht nicht")
        text = llm._call(system, user, max_tokens, schema)
        after = llm.usage_today()
        if not text:
            self._error("Claude: keine verwertbare Antwort (Fehler, Rate-Limit oder Ablehnung)")
            raise AiUnavailable("Claude: keine Antwort")
        return AiResult(text, after[0] - before[0], after[1] - before[1], "claude", self.model())

    def _gemini(self, system: str, user: str, json_mode: bool, max_tokens: int) -> AiResult:
        lim = self.limits()
        u = self.usage("gemini")
        if lim["rpd"] and u["requests"] >= lim["rpd"]:
            raise BudgetExceeded("Gemini: Tageslimit für Anfragen erreicht")
        est_in = (len(system) + len(user)) // 3
        if lim["tpd"] and u["input_tokens"] + u["output_tokens"] + est_in + max_tokens > lim["tpd"]:
            raise BudgetExceeded("Gemini: Token-Tagesbudget reicht nicht")
        slot = self._throttle("gemini", lim, est_in + max_tokens)
        prov = GeminiProvider(self.ctx.http, self.ctx.config.secrets.gemini_api_key or "", self.model())
        try:
            res = prov.generate(system, user, json_mode, max_tokens)
        except QuotaExceeded as e:
            slot[1] = 0
            self._record("gemini", ok=False, quota=True)
            if e.scope == "day":
                until = next_pacific_midnight()
            else:
                until = datetime.now(UTC) + timedelta(seconds=min(900.0, max(30.0, (e.retry_after or 60.0) + 2)))
            self._pause("gemini", until, e.text)
            self._error(e.text)
            raise
        except AiError as e:
            slot[1] = 0
            self._record("gemini", ok=False)
            self._error(e.text)
            if isinstance(e, AiUnavailable):  # anhaltende Störung: Abstand verdoppeln statt jeden Lauf neu anzufragen
                self._backoff("gemini", e.text)
            raise
        self.db.set_state("ai.fail.gemini", 0)
        slot[1] = res.input_tokens + res.output_tokens
        self._record("gemini", res.input_tokens, res.output_tokens)
        return res

    def _backoff(self, provider: str, reason: str) -> None:
        n = int(self.db.get_state(f"ai.fail.{provider}", 0) or 0) + 1
        self.db.set_state(f"ai.fail.{provider}", n)
        if n >= 2:  # einzelne Aussetzer bleiben folgenlos; ab dem zweiten Fehler in Folge 5, 10, 20 … max. 120 min
            self._pause(provider, datetime.now(UTC) + timedelta(minutes=min(120, 5 * 2 ** (n - 2))), reason)

    def _throttle(self, provider: str, lim: dict[str, int], est: int) -> list[float]:
        """Takt je Minute (Anfragen und Tokens) einhalten – kurz warten, bei längerer Wartezeit abbrechen.
        Gibt den reservierten Eintrag zurück (``[Zeit, Tokens]``); die Tokens werden nach der Antwort ergänzt."""
        waited = 0.0
        while True:
            with self._lock:
                dq = self._recent.setdefault(provider, deque())
                now = time.monotonic()
                while dq and now - dq[0][0] > 60:
                    dq.popleft()
                n, toks = len(dq), int(sum(e[1] for e in dq))
                over_r = lim["rpm"] and n >= lim["rpm"]
                over_t = lim["tpm"] and n and toks + est > lim["tpm"]
                if not (over_r or over_t):
                    slot = [now, float(est)]
                    dq.append(slot)
                    return slot
                wait = max(0.5, 60 - (now - dq[0][0]) + 0.2)
            if waited + wait > 75:
                raise Paused("Minutenlimit lokal erreicht", datetime.now(UTC) + timedelta(seconds=wait))
            time.sleep(min(wait, 30))
            waited += min(wait, 30)

    # -- Anzeige -------------------------------------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        p = self.provider_name()
        u = self.usage(p)
        lim = self.limits()
        return {"provider": p, "model": self.model(), "enabled": self.enabled(), "key": self.key_present(),
                "usage": u, "limits": lim, "tokens_left": self.tokens_left() if self.enabled() else None,
                "requests_left": self.requests_left(), "paused_until": self.paused_until(p),
                "last_error": self.last_error(), "day": self.day_key(p)}


def parse_json(text: str) -> Any:
    """JSON aus einer Modellantwort (ggf. mit ```json-Zaun)."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        a, b = t.find("{"), t.rfind("}")
        if a >= 0 and b > a:
            return json.loads(t[a:b + 1])
        raise


__all__ = [
    "GEMINI_DEFAULT",
    "GEMINI_MODELS",
    "AiError",
    "AiGateway",
    "AiRejected",
    "AiResult",
    "AiUnavailable",
    "BudgetExceeded",
    "GeminiProvider",
    "Paused",
    "QuotaExceeded",
    "model_allowed",
    "pacific_day",
    "parse_json",
]
