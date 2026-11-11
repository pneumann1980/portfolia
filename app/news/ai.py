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
* **Modell je Aufgabe (0.111):** Jede Aufgabe (Nachrichtenpakete, Tagesbericht, Ereignis-Grenzfälle, KI-Discovery,
  Video-Kernaussagen, Firmenanalyse) nutzt ein Modell laut Profil (``llm.gemini_profile``: ein Modell für alle =
  bisheriges Verhalten, Free Tier nach Aufgabe, eigene Zuordnung). Anfragen, Tokens, lokale Grenzen und Pausen gelten
  **je Modell**; Aufgaben mit demselben Modell teilen sich dessen Kontingent. Ein Minuten-/Tageslimit pausiert nur das
  betroffene Modell; Fehler, die den ganzen Zugang betreffen (Schlüssel ungültig, Berechtigung, Netz), pausieren alle.
  Manuelle Starts haben Vorrang (Rücklage vom Tageskontingent, Vortritt beim Minutentakt).
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import httpx

from app.context import AppContext
from app.util.timeutil import iso, local_tz, parse_iso, today_local

log = logging.getLogger(__name__)

PACIFIC = ZoneInfo("America/Los_Angeles")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
LEARNED_DAYS = 30  # von Google gemeldete Grenzen gelten so lange (danach wieder Einstellung/Startwert)
# Kostenlos nutzbare Flash-/Flash-Lite-Modelle laut https://ai.google.dev/gemini-api/docs/pricing (Stand der Prüfung:
# 2026-10). Standard ist ein Flash-Lite-Modell – günstigstes Free-Tier-Modell mit ausreichender Qualität für Einordnung.
GEMINI_MODELS = ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite", "gemini-3.8-flash",
                 "gemini-3.6-flash", "gemini-2.5-flash")
GEMINI_DEFAULT = "gemini-3.5-flash-lite"
_MODEL_RE = re.compile(r"^gemini-[a-z0-9][a-z0-9.\-]{2,40}$")
_PAID_HINTS = ("pro", "image", "tts", "live", "audio", "embedding", "robotics", "omni", "argon", "nano", "lyria",
               "transcribe", "translate")

# Aufgaben (Schlüssel → Bezeichnung). Aufrufer übergeben ``task``; ohne Angabe gilt „news“ (bisheriges Verhalten).
TASKS: dict[str, str] = {
    "news": "Nachrichtenpakete",
    "events": "Ereignis-Grenzfälle",
    "report": "Tagesbericht",
    "discovery": "KI-Discovery: Themen und erste Thesen",
    "video": "Video-Kernaussagen",
    "company": "Firmenanalyse: Bericht, Disruption, Portfolia-Score",
}
# Profile: „einzeln“ = ein Modell für alle (Übernahme bestehender Installationen), „free_tier“ = Empfehlung für einen
# Gemini-Schlüssel im Free Tier (je Aufgabe ein eigenes Modellkontingent), „eigen“ = Zuordnung in ``llm.task_models``.
PROFILES: dict[str, dict[str, Any]] = {
    "einzeln": {"label": "Ein Modell für alles"},
    "free_tier": {"label": "Kostenbewusst optimiert", "models": {
        "news": "gemini-3.5-flash-lite", "events": "gemini-3.5-flash-lite", "report": "gemini-3.5-flash-lite",
        "discovery": "gemini-3.5-flash-lite", "video": "gemini-3.6-flash", "company": "gemini-3.8-flash"}},
    "eigen": {"label": "Individuell"},
}
# Vorsichtige lokale Startwerte je Modell (eigene Annahmen – die gültigen Free-Tier-Werte zeigt Google AI Studio je
# Projekt; bitte dort ablesen und in den Einstellungen eintragen). tpd 0 = ohne lokale Token-Tagesgrenze.
LIMIT_DEFAULTS = {"lite": {"rpm": 10, "tpm": 250_000, "rpd": 200, "tpd": 0},
                  "flash": {"rpm": 5, "tpm": 250_000, "rpd": 50, "tpd": 0}}
LIMIT_LABELS = {"rpm": "Anfragen/Minute", "tpm": "Tokens/Minute", "rpd": "Anfragen/Tag", "tpd": "Tokens/Tag"}
MANUAL_RESERVE_PCT = 10  # Anteil des Tageskontingents je Modell, den Hintergrundaufgaben nicht verbrauchen

AI_PRIORITY: contextvars.ContextVar[str] = contextvars.ContextVar("ai_priority", default="background")


@contextlib.contextmanager
def manual_priority() -> Iterator[None]:
    """Aufrufe in diesem Block gelten als manuell gestartet (Vorrang vor Hintergrundaufgaben)."""
    tok = AI_PRIORITY.set("manual")
    try:
        yield
    finally:
        AI_PRIORITY.reset(tok)


class AiError(Exception):
    """Basisklasse; ``code`` = stabiler Kurzcode, ``text`` = bereinigte Meldung für Protokoll und Oberfläche."""

    code = "error"

    def __init__(self, text: str = "") -> None:
        super().__init__(text or self.code)
        self.text = text or self.code


class QuotaExceeded(AiError):
    code = "quota"

    def __init__(self, text: str, retry_after: float | None = None, scope: str = "unknown", model: str = "",
                 limits: dict[str, int] | None = None) -> None:
        super().__init__(text)
        self.retry_after = retry_after
        self.scope = scope  # minute | day | unknown
        self.model = model
        self.limits = limits or {}  # von Google gemeldete Grenzen (``quotaValue``), z. B. {"rpd": 20}


class AiUnavailable(AiError):
    code = "unavailable"

    def __init__(self, text: str = "", scope: str = "access") -> None:
        super().__init__(text)
        self.scope = scope  # access (Netz) | model (HTTP 5xx bzw. Zeitüberschreitung des Modells)


class AiRejected(AiError):
    """Dauerhaft abgelehnt (Schlüssel, Modell, Sicherheitsfilter): Wiederholen ohne Änderung ist sinnlos."""

    code = "rejected"

    def __init__(self, text: str = "", scope: str = "request") -> None:
        super().__init__(text)
        self.scope = scope  # request (Inhalt) | model (Modell unbekannt) | access (Schlüssel, Berechtigung)


class BudgetExceeded(AiError):
    code = "budget"

    def __init__(self, text: str = "", until: datetime | None = None, model: str = "", scope: str = "local") -> None:
        super().__init__(text)
        self.until = until
        self.model = model
        self.scope = scope  # local (lokale Tagesgrenze) | reserve (Rücklage für manuelle Starts)


class Paused(AiError):
    code = "paused"

    def __init__(self, text: str, until: datetime | None = None, model: str = "", scope: str = "") -> None:
        super().__init__(text)
        self.until = until
        self.model = model
        self.scope = scope  # minute | day | model | access | local


@dataclass
class AiResult:
    text: str
    input_tokens: int
    output_tokens: int
    provider: str
    model: str


def _quota_key(quota_id: str) -> str:
    """Google-``quotaId`` → lokale Grenze: Anfragen/Tokens je Minute/Tag (``rpm``, ``rpd``, ``tpm``, ``tpd``)."""
    q = quota_id.lower()
    unit = "t" if "token" in q else "r"
    per = "d" if "perday" in q else "m" if "perminute" in q else ""
    return f"{unit}p{per}" if per else ""


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

    def list_models(self) -> list[dict[str, Any]]:
        """Für diesen Schlüssel verfügbare Modelle mit ``generateContent`` (``models.list``, ohne Kontingente)."""
        out: list[dict[str, Any]] = []
        token = ""
        for _ in range(10):
            try:
                params: dict[str, Any] = {"pageSize": 1000, **({"pageToken": token} if token else {})}
                r = self.client.get(GEMINI_MODELS_URL, params=params, timeout=30.0,
                                    headers={"x-goog-api-key": self.key})
            except httpx.HTTPError as e:
                raise AiUnavailable(f"Gemini: Modellliste nicht erreichbar ({type(e).__name__})", scope="access") from e
            if r.status_code != 200:
                self._raise(r)
            data = r.json() or {}
            for m in data.get("models") or []:
                if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                    continue
                out.append({"id": str(m.get("name") or "").removeprefix("models/"),
                            "label": str(m.get("displayName") or ""), "input": int(m.get("inputTokenLimit") or 0),
                            "output": int(m.get("outputTokenLimit") or 0), "thinking": bool(m.get("thinking"))})
            token = str(data.get("nextPageToken") or "")
            if not token:
                break
        return out

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
            raise AiUnavailable(f"Gemini ({self.model}): Zeitüberschreitung", scope="model") from e
        except httpx.HTTPError as e:
            raise AiUnavailable(f"Gemini: nicht erreichbar ({type(e).__name__})", scope="access") from e
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
            limits: dict[str, int] = {}
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
                    key, val = _quota_key(qid), str((v or {}).get("quotaValue", "")).strip()
                    if key and val.isdigit():  # nicht offiziell dokumentiert – nur übernehmen, wenn vorhanden
                        limits[key] = int(val)
            if scope == "unknown" and re.search(r"(?i)per day|daily|requests per day", msg):
                scope = "day"
            if limits and all(v == 0 for v in limits.values()):
                # Kontingent 0: Modell ist für dieses Projekt im Free Tier nicht freigegeben – Warten hilft nicht
                raise AiRejected(f"Gemini: Google meldet Kontingent 0 für {self.model} – für dieses Projekt im "
                                 "kostenlosen Kontingent nicht verfügbar", scope="model")
            label = {"day": "Tageslimit", "minute": "Minutenlimit"}.get(scope, "Limit")
            told = ", ".join(f"{LIMIT_LABELS.get(k, k)} {v}" for k, v in sorted(limits.items()) if v)
            raise QuotaExceeded(f"Gemini: Kontingent erschöpft – {label} von {self.model} ({scope})"
                                + (f"; Google meldet {told}" if told else ""), retry, scope, self.model,
                                {k: v for k, v in limits.items() if v > 0})
        if status in (500, 502, 503, 504) or gstatus in ("UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED"):
            raise AiUnavailable(f"Gemini: {self.model} vorübergehend nicht verfügbar (HTTP {status})", scope="model")
        if status == 404 or gstatus == "NOT_FOUND":
            raise AiRejected(f"Gemini: Modell {self.model} nicht verfügbar (HTTP {status} {gstatus}) {msg}".strip(),
                             scope="model")
        if status in (401, 403) or gstatus in ("UNAUTHENTICATED", "PERMISSION_DENIED") or \
                re.search(r"(?i)api key|api_key|billing|location is not supported", msg):
            raise AiRejected(f"Gemini: Zugang abgelehnt (HTTP {status} {gstatus}) {msg}".strip(), scope="access")
        if status == 400:
            raise AiRejected(f"Gemini: Anfrage abgelehnt (HTTP {status} {gstatus}) {msg}".strip())
        raise AiUnavailable(f"Gemini: HTTP {status}", scope="model")


# ----------------------------------------------------------------------------------------------------
# Gateway: Anbieterwahl, Budgets, Takt, Pausen, Nutzungsprotokoll
# ----------------------------------------------------------------------------------------------------

class AiGateway:
    _lock = threading.Lock()
    # Modell (bzw. Anbieter) → [Zeitpunkt, Tokens] je Anfrage der letzten 60 s (prozessweit, von allen Gateways geteilt)
    _recent: ClassVar[dict[str, deque[list[float]]]] = {}
    _inflight: ClassVar[dict[str, int]] = {}       # laufende Anfragen je Modell (Tageslimit bei parallelen Aufrufen)
    _manual_waiting: ClassVar[dict[str, int]] = {}  # wartende manuelle Anfragen je Modell (Vortritt beim Takt)

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

    def base_model(self) -> str:
        """Bisheriges (einziges) Gemini-Modell aus ``llm.gemini_model``."""
        m = str(self.s.get("llm.gemini_model") or GEMINI_DEFAULT)
        return m if model_allowed(m) else GEMINI_DEFAULT

    def profile(self) -> str:
        p = str(self.s.get("llm.gemini_profile") or "einzeln")
        return p if p in PROFILES else "einzeln"

    def model_for(self, task: str = "news") -> str:
        """Modell einer Aufgabe laut Profil (Gemini); bei Claude das konfigurierte Claude-Modell."""
        if self.provider_name() != "gemini":
            return str(self.s.get("llm.model") or "claude-opus-5")
        prof = self.profile()
        if prof == "free_tier":
            m = str(PROFILES["free_tier"]["models"].get(task) or "")
        elif prof == "eigen":
            m = str((self.s.get("llm.task_models") or {}).get(task) or "")
        else:
            m = ""
        return m if m and model_allowed(m) else self.base_model()

    def model(self, task: str = "news") -> str:
        return self.model_for(task)

    def task_models(self) -> dict[str, str]:
        return {t: self.model_for(t) for t in TASKS}

    def models_in_use(self) -> list[str]:
        out: list[str] = []
        for m in self.task_models().values():
            if m not in out:
                out.append(m)
        return out

    def limits(self, model: str | None = None, *, learned: bool = True) -> dict[str, int]:
        """Lokale Grenzen eines Modells: eigene Werte (``llm.model_limits``), für das bisherige Modell die bisherigen
        Einstellungen (Übernahme), sonst vorsichtige Startwerte (Annahmen, in AI Studio prüfen)."""
        m = model or self.model_for("news")
        g = lambda k, d: max(0, int(self.s.get(k, d) or 0))  # noqa: E731
        own = (self.s.get("llm.model_limits") or {}).get(m)
        if isinstance(own, dict):
            lim = {k: max(0, int(own.get(k) or 0)) for k in ("rpm", "tpm", "rpd", "tpd")}
        elif m == self.base_model():
            unlimited = bool(self.s.get("llm.gemini_tokens_unlimited", False))  # 0 = ohne Token-Tagesgrenze
            lim = {"rpm": g("llm.gemini_rpm", 5), "tpm": g("llm.gemini_tpm", 100_000),
                   "rpd": g("llm.gemini_rpd", 100), "tpd": 0 if unlimited else g("llm.gemini_daily_token_budget",
                                                                                 200_000)}
        else:
            lim = dict(LIMIT_DEFAULTS["lite" if "lite" in m else "flash"])
        for k, v in (self.learned(m) if learned else {}).items():  # von Google gemeldet: nie darüber hinaus
            if k in lim and v > 0:
                lim[k] = min(lim[k], v) if lim[k] else v
        return lim

    def learned(self, model: str) -> dict[str, int]:
        """Von Google gemeldete Grenzen eines Modells (aus der letzten Kontingent-Meldung, höchstens
        :data:`LEARNED_DAYS` alt) – ``{}``, wenn keine bekannt."""
        st = (self.db.get_state("ai.learned_limits") or {}).get(model)
        if not isinstance(st, dict):
            return {}
        at = parse_iso(str(st.get("at") or ""))
        if at is None or datetime.now(UTC) - at > timedelta(days=LEARNED_DAYS):
            return {}
        return {k: int(v) for k, v in st.items() if k in LIMIT_LABELS and str(v).isdigit()}

    def learned_all(self) -> dict[str, dict[str, Any]]:
        out = {}
        for m, st in (self.db.get_state("ai.learned_limits") or {}).items():
            if self.learned(m):
                out[m] = {**self.learned(m), "at": st.get("at")}
        return out

    def _learn(self, model: str, limits: dict[str, int]) -> None:
        if not model or not limits:
            return
        with self._lock:
            allst = dict(self.db.get_state("ai.learned_limits") or {})
            cur = dict(allst.get(model) or {}) if self.learned(model) else {}
            cur.update({k: int(v) for k, v in limits.items() if k in LIMIT_LABELS and v > 0})
            cur["at"] = iso(datetime.now(UTC))
            allst[model] = cur
            self.db.set_state("ai.learned_limits", allst)

    def forget_learned(self) -> None:
        self.db.set_state("ai.learned_limits", None)

    def refresh_models(self) -> dict[str, Any]:
        """Modellliste bei Google abrufen und speichern (Einstellungen → KI). Kein Kontingent, kein Prompt."""
        if self.provider_name() != "gemini" or not self.key_present():
            raise AiRejected("Gemini ist nicht gewählt oder der Schlüssel fehlt", scope="access")
        models = GeminiProvider(self.ctx.http, self.ctx.config.secrets.gemini_api_key or "", "").list_models()
        st = {"at": iso(datetime.now(UTC)), "models": models}
        self.db.set_state("ai.models_available", st)
        return st

    def models_available(self) -> dict[str, Any] | None:
        st = self.db.get_state("ai.models_available")
        return st if isinstance(st, dict) and st.get("models") is not None else None

    # -- Nutzung ---------------------------------------------------------------------------------------
    def day_key(self, provider: str | None = None) -> str:
        return pacific_day() if (provider or self.provider_name()) == "gemini" else today_local().isoformat()

    def usage(self, provider: str | None = None, day: str | None = None) -> dict[str, int]:
        p = provider or self.provider_name()
        r = self.db.q1("SELECT * FROM ai_usage WHERE day=? AND provider=?", (day or self.day_key(p), p))
        return {k: int(r[k]) if r else 0 for k in ("requests", "input_tokens", "output_tokens", "errors", "quota_hits")}

    def usage_model(self, model: str, day: str | None = None) -> dict[str, int]:
        """Verbrauch eines Modells heute (Pacific-Tag) – Summe über alle Aufgaben, die es nutzen. Im Profil „ein
        Modell für alle“ zählt der bisherige Anbieterzähler (lückenlose Übernahme am Tag der Umstellung)."""
        if self.profile() == "einzeln" and model == self.base_model():
            return self.usage("gemini", day)
        r = self.db.q1("SELECT COALESCE(SUM(requests),0) AS requests, COALESCE(SUM(input_tokens),0) AS input_tokens, "
                       "COALESCE(SUM(output_tokens),0) AS output_tokens, COALESCE(SUM(errors),0) AS errors, "
                       "COALESCE(SUM(quota_hits),0) AS quota_hits FROM ai_model_usage WHERE day=? AND model=?",
                       (day or self.day_key("gemini"), model))
        return {k: int(r[k]) if r else 0 for k in ("requests", "input_tokens", "output_tokens", "errors", "quota_hits")}

    def usage_task(self, task: str, day: str | None = None) -> dict[str, int]:
        r = self.db.q1("SELECT COALESCE(SUM(requests),0) AS requests, COALESCE(SUM(input_tokens),0) AS input_tokens, "
                       "COALESCE(SUM(output_tokens),0) AS output_tokens, COALESCE(SUM(errors),0) AS errors, "
                       "COALESCE(SUM(quota_hits),0) AS quota_hits FROM ai_model_usage WHERE day=? AND task=?",
                       (day or self.day_key("gemini"), task))
        return {k: int(r[k]) if r else 0 for k in ("requests", "input_tokens", "output_tokens", "errors", "quota_hits")}

    def tokens_left(self, task: str = "news") -> int | None:
        """Restliche Tokens heute für das Modell der Aufgabe; ``None`` = ohne lokale Token-Tagesgrenze."""
        p = self.provider_name()
        if p == "claude":
            from app.news.llm import LlmService

            return LlmService(self.ctx).budget_left()
        m = self.model_for(task)
        tpd = self.limits(m)["tpd"]
        if not tpd:
            return None
        u = self.usage_model(m)
        return tpd - u["input_tokens"] - u["output_tokens"]

    def requests_left(self, task: str = "news") -> int | None:
        """Restliche Anfragen heute für das Modell der Aufgabe; ``None`` = ohne Tageslimit (Claude bzw. rpd 0)."""
        if self.provider_name() != "gemini":
            return None
        m = self.model_for(task)
        rpd = self.limits(m)["rpd"]
        return rpd - self.usage_model(m)["requests"] if rpd else None

    def _record(self, provider: str, inp: int = 0, out: int = 0, *, ok: bool = True, quota: bool = False,
                model: str = "", task: str = "") -> None:
        self.db.x(
            """INSERT INTO ai_usage(day, provider, requests, input_tokens, output_tokens, errors, quota_hits)
               VALUES (?,?,?,?,?,?,?) ON CONFLICT(day, provider) DO UPDATE SET requests=requests+excluded.requests,
                   input_tokens=input_tokens+excluded.input_tokens, output_tokens=output_tokens+excluded.output_tokens,
                   errors=errors+excluded.errors, quota_hits=quota_hits+excluded.quota_hits""",
            (self.day_key(provider), provider, 1, inp, out, 0 if ok else 1, 1 if quota else 0))
        if model:
            self.db.x(
                """INSERT INTO ai_model_usage(day, model, task, requests, input_tokens, output_tokens, errors,
                       quota_hits) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(day, model, task) DO UPDATE SET
                       requests=requests+excluded.requests, input_tokens=input_tokens+excluded.input_tokens,
                       output_tokens=output_tokens+excluded.output_tokens, errors=errors+excluded.errors,
                       quota_hits=quota_hits+excluded.quota_hits""",
                (self.day_key(provider), model, task or "news", 1, inp, out, 0 if ok else 1, 1 if quota else 0))

    # -- Pause -------------------------------------------------------------------------------------------
    def _pause_state(self, key: str) -> tuple[datetime | None, str]:
        st = self.db.get_state(key)
        until = parse_iso(st.get("until")) if isinstance(st, dict) else None
        if until and until > datetime.now(UTC):
            return until, str(st.get("reason") or "")  # type: ignore[union-attr]
        return None, ""

    def access_paused(self, provider: str | None = None) -> tuple[datetime | None, str]:
        """Pause des ganzen Zugangs (Schlüssel, Berechtigung, Netz) – betrifft alle Modelle."""
        return self._pause_state(f"ai.pause.{provider or self.provider_name()}")

    def model_paused(self, model: str) -> tuple[datetime | None, str]:
        return self._pause_state(f"ai.pause.gemini:{model}")

    def blocked(self, task: str = "news") -> dict[str, Any] | None:
        """Wartegrund einer Aufgabe (Zugang oder Modell pausiert): ``{until, reason, scope, model}`` oder ``None``."""
        p = self.provider_name()
        until, why = self.access_paused(p)
        if until:
            return {"until": until, "reason": why or "Zugang pausiert", "scope": "access", "model": ""}
        if p != "gemini":
            return None
        m = self.model_for(task)
        until, why = self.model_paused(m)
        if until:
            st = self.db.get_state(f"ai.pause.gemini:{m}")
            return {"until": until, "reason": why, "scope": str((st or {}).get("scope") or "model"), "model": m}
        return None

    def paused_until(self, provider: str | None = None, task: str = "news") -> datetime | None:
        """Wartezeit der Aufgabe (Standard: Nachrichtenpakete) – Zugang oder ihr Modell pausiert."""
        b = self.blocked(task) if (provider or self.provider_name()) == self.provider_name() else None
        if b is None and provider and provider != self.provider_name():
            return self.access_paused(provider)[0]
        return b["until"] if b else None

    def _pause(self, provider: str, until: datetime, reason: str, model: str = "", scope: str = "") -> None:
        key = f"ai.pause.{provider}:{model}" if model else f"ai.pause.{provider}"
        self.db.set_state(key, {"until": iso(until), "reason": reason, "scope": scope or ("model" if model else
                                                                                           "access")})

    def clear_pauses(self) -> None:
        """Pausen aufheben (z. B. nach Änderung von Schlüssel, Profil oder Grenzen in den Einstellungen)."""
        for p in ("gemini", "claude"):
            self.db.set_state(f"ai.pause.{p}", None)
        for m in set(GEMINI_MODELS) | set(self.models_in_use()):
            self.db.set_state(f"ai.pause.gemini:{m}", None)

    def last_error(self) -> str:
        st = self.db.get_state("ai.last_error")
        return str(st.get("text", "")) if isinstance(st, dict) else ""

    def _error(self, text: str) -> None:
        self.db.set_state("ai.last_error", {"text": text[:300], "at": iso(datetime.now(UTC))})

    def _ok(self) -> None:
        self.db.set_state("ai.last_ok", iso(datetime.now(UTC)))

    def health(self, window_min: int = 30, task: str = "news") -> tuple[bool, str]:
        """(erreichbar, Grund). „Nicht erreichbar“ heißt: aus/ohne Schlüssel, Zugang oder Modell der Aufgabe pausiert
        oder der jüngste Aufruf der letzten ``window_min`` Minuten ist fehlgeschlagen und danach gab es keinen
        Erfolg."""
        if not self.enabled():
            return False, "Die KI ist ausgeschaltet oder nicht konfiguriert."
        b = self.blocked(task)
        if b is not None:
            who = f"Das Modell {b['model']}" if b["model"] else "Der KI-Anbieter"
            return False, f"{who} pausiert bis {b['until'].astimezone(local_tz()):%H:%M} Uhr."
        err = self.db.get_state("ai.last_error")
        at = parse_iso(err.get("at")) if isinstance(err, dict) else None
        if at is not None and datetime.now(UTC) - at < timedelta(minutes=window_min):
            ok = parse_iso(self.db.get_state("ai.last_ok") or None)
            if ok is None or ok < at:
                return False, f"Der letzte KI-Aufruf ist fehlgeschlagen ({str(err.get('text', ''))[:120]})."
        return True, ""

    # -- Aufruf --------------------------------------------------------------------------------------------
    def generate(self, system: str, user: str, schema: dict[str, Any] | None, max_tokens: int,
                 json_mode: bool = False, *, task: str = "news", priority: str | None = None) -> AiResult:
        """Ein Aufruf mit allen Kontrollen. ``schema`` (nur Claude: erzwungenes JSON-Schema) oder ``json_mode``
        (Gemini: JSON-MIME-Typ; die Prüfung gegen das Schema macht der Aufrufer). ``task`` wählt das Modell (Profil),
        ``priority`` „manual“ hat Vorrang (Standard: aus :data:`AI_PRIORITY`)."""
        if not self.enabled():
            raise AiRejected("KI ist nicht aktiviert oder der Schlüssel fehlt", scope="access")
        provider = self.provider_name()
        b = self.blocked(task)
        if b is not None:
            who = f"Modell {b['model']}" if b["model"] else "Anbieter"
            raise Paused(f"{who} pausiert: {b['reason']}".strip(": "), b["until"], b["model"], b["scope"])
        if provider == "gemini":
            return self._gemini(system, user, json_mode or schema is not None, max_tokens, task,
                                priority or AI_PRIORITY.get())
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
        self._ok()
        return AiResult(text, after[0] - before[0], after[1] - before[1], "claude", self.model_for("news"))

    def reserve(self, model: str) -> int:
        """Rücklage für manuelle Starts: Anfragen, die Hintergrundaufgaben vom Tageslimit des Modells nicht nutzen."""
        rpd = self.limits(model)["rpd"]
        pct = max(0, min(50, int(self.s.get("llm.manual_reserve_pct", MANUAL_RESERVE_PCT) or 0)))
        return rpd * pct // 100 if rpd and pct else 0  # abgerundet: kleine Grenzen bleiben voll nutzbar

    def _admit(self, model: str, lim: dict[str, int], est_in: int, max_tokens: int, priority: str) -> None:
        """Tageslimits prüfen und einen Platz belegen – unter Sperre, damit parallele Aufrufe am Limit es nicht
        überschreiten (laufende Anfragen zählen mit)."""
        with self._lock:
            u = self.usage_model(model)
            used = u["requests"] + self._inflight.get(model, 0)
            if lim["rpd"] and used >= lim["rpd"]:
                raise BudgetExceeded(f"Lokales Tageslimit von {model} erreicht ({lim['rpd']} Anfragen)",
                                     next_pacific_midnight(), model, "local")
            if priority != "manual" and lim["rpd"] and used >= lim["rpd"] - self.reserve(model):
                raise BudgetExceeded(f"Rest des Tageskontingents von {model} ist für manuelle Starts reserviert",
                                     next_pacific_midnight(), model, "reserve")
            if lim["tpd"] and u["input_tokens"] + u["output_tokens"] + est_in + max_tokens > lim["tpd"]:
                raise BudgetExceeded(f"Lokales Token-Tagesbudget von {model} reicht nicht", next_pacific_midnight(),
                                     model, "local")
            self._inflight[model] = self._inflight.get(model, 0) + 1

    def _gemini(self, system: str, user: str, json_mode: bool, max_tokens: int, task: str = "news",
                priority: str = "background") -> AiResult:
        model = self.model_for(task)
        lim = self.limits(model)
        est_in = (len(system) + len(user)) // 3
        self._admit(model, lim, est_in, max_tokens, priority)
        held = [True]  # Platz belegt, bis der Verbrauch gespeichert ist (siehe _settle)

        def settle(inp: int = 0, out: int = 0, **kw: Any) -> None:
            self._settle(model, task, inp, out, **kw)
            held[0] = False

        try:
            slot = self._throttle(model, lim, est_in + max_tokens, priority)
            prov = GeminiProvider(self.ctx.http, self.ctx.config.secrets.gemini_api_key or "", model)
            try:
                res = prov.generate(system, user, json_mode, max_tokens)
            except QuotaExceeded as e:
                slot[1] = 0
                settle(ok=False, quota=True)
                self._learn(model, e.limits)
                if e.scope == "day":
                    until = next_pacific_midnight()
                else:
                    until = datetime.now(UTC) + timedelta(seconds=min(900.0, max(30.0, (e.retry_after or 60.0) + 2)))
                self._pause("gemini", until, e.text, model=model, scope="day" if e.scope == "day" else "minute")
                self._error(e.text)
                raise
            except AiError as e:
                slot[1] = 0
                settle(ok=False)
                self._error(e.text)
                if isinstance(e, AiRejected) and e.scope == "access":
                    self._pause("gemini", datetime.now(UTC) + timedelta(minutes=30), e.text, scope="access")
                elif isinstance(e, AiRejected) and e.scope == "model":
                    self._pause("gemini", datetime.now(UTC) + timedelta(hours=6), e.text, model=model, scope="model")
                elif isinstance(e, AiUnavailable):  # anhaltende Störung: Abstand verdoppeln statt jedes Mal anfragen
                    self._backoff("gemini", e.text, model if e.scope == "model" else "")
                raise
            slot[1] = res.input_tokens + res.output_tokens
            settle(res.input_tokens, res.output_tokens)
        finally:
            if held[0]:  # z. B. Abbruch im Takt (Paused) oder unerwarteter Fehler: Platz ohne Verbrauch freigeben
                with self._lock:
                    self._inflight[model] = max(0, self._inflight.get(model, 0) - 1)
        self.db.set_state(f"ai.fail.gemini:{model}", 0)
        self.db.set_state("ai.fail.gemini", 0)
        self._ok()
        return res

    def _settle(self, model: str, task: str, inp: int = 0, out: int = 0, *, ok: bool = True,
                quota: bool = False) -> None:
        """Verbrauch speichern und den belegten Platz freigeben – in **einem** Schritt unter der Sperre, unter der
        :meth:`_admit` zählt. Getrennt gäbe es ein Fenster, in dem ein paralleler Aufruf denselben Aufruf entweder
        gar nicht (Platz frei, Verbrauch fehlt → Limit überschritten) oder doppelt (gespeichert und noch belegt →
        zu früh abgewiesen) zählt."""
        with self._lock:
            try:
                self._record("gemini", inp, out, ok=ok, quota=quota, model=model, task=task)
            finally:
                self._inflight[model] = max(0, self._inflight.get(model, 0) - 1)

    def _backoff(self, provider: str, reason: str, model: str = "") -> None:
        key = f"ai.fail.{provider}:{model}" if model else f"ai.fail.{provider}"
        n = int(self.db.get_state(key, 0) or 0) + 1
        self.db.set_state(key, n)
        if n >= 2:  # einzelne Aussetzer bleiben folgenlos; ab dem zweiten Fehler in Folge 5, 10, 20 … max. 120 min
            self._pause(provider, datetime.now(UTC) + timedelta(minutes=min(120, 5 * 2 ** (n - 2))), reason,
                        model=model, scope="model" if model else "access")

    def _throttle(self, model: str, lim: dict[str, int], est: int, priority: str = "background") -> list[float]:
        """Takt je Minute und Modell (Anfragen und Tokens) einhalten – kurz warten, bei längerer Wartezeit abbrechen.
        Manuelle Anfragen haben Vortritt: Hintergrundanfragen warten, solange eine manuelle auf einen Platz wartet.
        Gibt den reservierten Eintrag zurück (``[Zeit, Tokens]``); die Tokens werden nach der Antwort ergänzt."""
        waited = 0.0
        manual = priority == "manual"
        if manual:
            with self._lock:
                self._manual_waiting[model] = self._manual_waiting.get(model, 0) + 1
        try:
            while True:
                with self._lock:
                    dq = self._recent.setdefault(model, deque())
                    now = time.monotonic()
                    while dq and now - dq[0][0] > 60:
                        dq.popleft()
                    n, toks = len(dq), int(sum(e[1] for e in dq))
                    over_r = lim["rpm"] and n >= lim["rpm"]
                    over_t = lim["tpm"] and n and toks + est > lim["tpm"]
                    yield_to_manual = not manual and self._manual_waiting.get(model, 0) > 0
                    if not (over_r or over_t or yield_to_manual):
                        slot = [now, float(est)]
                        dq.append(slot)
                        return slot
                    wait = 0.5 if yield_to_manual and not (over_r or over_t) else \
                        max(0.5, 60 - (now - dq[0][0]) + 0.2)
                if waited + wait > 75:
                    raise Paused(f"Minutenlimit von {model} lokal erreicht", datetime.now(UTC) +
                                 timedelta(seconds=wait), model, "minute")
                time.sleep(min(wait, 30))
                waited += min(wait, 30)
        finally:
            if manual:
                with self._lock:
                    self._manual_waiting[model] = max(0, self._manual_waiting.get(model, 0) - 1)

    # -- Anzeige -------------------------------------------------------------------------------------------------
    def task_status(self) -> list[dict[str, Any]]:
        """Je Aufgabe: Modell, Verbrauch (Aufgabe und Modell), Grenzen, Rest, Rücklage und konkreter Wartegrund."""
        out = []
        gem = self.provider_name() == "gemini"
        for task, label in TASKS.items():
            m = self.model_for(task)
            row: dict[str, Any] = {"task": task, "label": label, "model": m}
            if gem:
                lim, um = self.limits(m), self.usage_model(m)
                row.update(limits=lim, usage_model=um, usage_task=self.usage_task(task),
                           shared=[TASKS[t] for t, mm in self.task_models().items() if mm == m and t != task],
                           requests_left=(lim["rpd"] - um["requests"]) if lim["rpd"] else None,
                           learned=self.learned(m),
                           tokens_left=(lim["tpd"] - um["input_tokens"] - um["output_tokens"]) if lim["tpd"] else None,
                           reserve=self.reserve(m))
            b = self.blocked(task) if self.enabled() else None
            if b is None and gem and row.get("requests_left") is not None and row["requests_left"] <= 0:
                b = {"until": next_pacific_midnight(), "reason": f"Lokales Tageslimit von {m} erreicht",
                     "scope": "local", "model": m}
            row["wait"] = b
            out.append(row)
        return out

    def status(self) -> dict[str, Any]:
        p = self.provider_name()
        u = self.usage(p)
        lim = self.limits()
        return {"provider": p, "model": self.model_for("news"), "enabled": self.enabled(), "key": self.key_present(),
                "usage": u, "limits": lim, "tokens_left": self.tokens_left() if self.enabled() else None,
                "requests_left": self.requests_left(), "paused_until": self.paused_until(p),
                "last_error": self.last_error(), "day": self.day_key(p), "profile": self.profile(),
                "profile_label": PROFILES[self.profile()]["label"], "tasks": self.task_status(),
                "access_paused": self.access_paused(p)}


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
    "AI_PRIORITY",
    "GEMINI_DEFAULT",
    "GEMINI_MODELS",
    "PROFILES",
    "TASKS",
    "AiError",
    "AiGateway",
    "AiRejected",
    "AiResult",
    "AiUnavailable",
    "BudgetExceeded",
    "GeminiProvider",
    "Paused",
    "QuotaExceeded",
    "manual_priority",
    "model_allowed",
    "pacific_day",
    "parse_json",
]
