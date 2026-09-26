"""Optionale Zusammenfassungen und Tagesdigest über die Anthropic Claude API (standardmäßig aus).

Datenschutz: Gesendet werden ausschließlich Asset-Namen sowie Titel/Teaser der Meldungen – niemals
Stückzahlen, Werte, Gewichte oder Kontonamen. Ein hartes Tages-Token-Budget begrenzt die Kosten.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.context import AppContext
from app.util.timeutil import iso, today_local

log = logging.getLogger(__name__)

# Modelle, für die serverseitige Refusal-Fallbacks ("default") verfügbar sind
FALLBACK_MODELS = ("claude-opus-5", "claude-opus-5-5", "claude-fable-5", "claude-fable-5-1")
# Modelle ohne effort-Parameter
NO_EFFORT_PREFIXES = ("claude-haiku", "claude-sonnet-4-5", "claude-3")

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "summary": {"type": "string"},
                    "relevance": {"type": "number"},
                },
                "required": ["id", "summary", "relevance"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

SYSTEM_SUMMARY = (
    "Du fasst Finanz- und Krypto-Nachrichten für einen privaten Anleger auf Deutsch zusammen. "
    "Für jede Meldung: höchstens zwei sachliche Sätze ohne Übertreibung und ohne Anlageempfehlung. "
    "Bewerte außerdem, wie relevant die Meldung für die genannten Assets ist (0 = irrelevant, 1 = sehr relevant). "
    "Nutze nur die gelieferten Texte; erfinde keine Fakten."
)
SYSTEM_DIGEST = (
    "Du erstellst einen kurzen deutschen Tagesüberblick zu Nachrichten über die genannten Assets. "
    "Gliedere nach Asset, je 1–3 Stichpunkte, sachlich, ohne Anlageempfehlung, nur aus den gelieferten Texten. "
    "Beginne direkt mit dem Inhalt."
)


class LlmService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.db = ctx.db
        self._client: Any = None

    # -- Voraussetzungen --------------------------------------------------------------------------
    def enabled(self) -> bool:
        return bool(self.ctx.config.secrets.anthropic_api_key) and bool(self.ctx.settings.get("llm.enabled", False))

    def usage_today(self) -> tuple[int, int, int]:
        row = self.db.q1("SELECT input_tokens, output_tokens, calls FROM llm_usage WHERE day=?",
                         (today_local().isoformat(),))
        return (row["input_tokens"], row["output_tokens"], row["calls"]) if row else (0, 0, 0)

    def budget_left(self) -> int:
        i, o, _ = self.usage_today()
        return int(self.ctx.settings.get("llm.daily_token_budget", 60000)) - i - o

    def _record(self, inp: int, out: int) -> None:
        self.db.x(
            """INSERT INTO llm_usage(day, input_tokens, output_tokens, calls) VALUES (?,?,?,1)
               ON CONFLICT(day) DO UPDATE SET input_tokens=input_tokens+excluded.input_tokens,
                   output_tokens=output_tokens+excluded.output_tokens, calls=calls+1""",
            (today_local().isoformat(), inp, out),
        )

    @property
    def client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self.ctx.config.secrets.anthropic_api_key, timeout=90.0,
                                               max_retries=2)
        return self._client

    # -- Aufruf -------------------------------------------------------------------------------------
    def _call(self, system: str, payload: str, max_tokens: int, schema: dict[str, Any] | None) -> str | None:
        model = str(self.ctx.settings.get("llm.model") or "claude-opus-5")
        est = len(payload) // 3 + len(system) // 3 + max_tokens
        if est > self.budget_left():
            log.info("LLM-Tagesbudget reicht nicht (geschätzt %s Token) – übersprungen", est)
            return None
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": payload}],
        }
        output_config: dict[str, Any] = {}
        if not model.startswith(NO_EFFORT_PREFIXES):
            output_config["effort"] = "low"
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        if output_config:
            kwargs["output_config"] = output_config
        if model in FALLBACK_MODELS:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        import anthropic

        try:
            resp = (self.client.beta.messages.create(**kwargs) if "betas" in kwargs
                    else self.client.messages.create(**kwargs))
        except anthropic.RateLimitError as e:
            log.warning("LLM: Rate-Limit (%s)", e.status_code)
            return None
        except anthropic.APIStatusError as e:
            log.warning("LLM-Fehler %s: %s", e.status_code, str(e.message)[:200])
            return None
        except anthropic.APIConnectionError as e:
            log.warning("LLM nicht erreichbar: %s", e)
            return None
        usage = getattr(resp, "usage", None)
        self._record(int(getattr(usage, "input_tokens", 0) or 0), int(getattr(usage, "output_tokens", 0) or 0))
        if resp.stop_reason == "refusal":
            log.info("LLM hat die Anfrage abgelehnt (refusal) – übersprungen")
            return None
        return next((b.text for b in resp.content if getattr(b, "type", "") == "text"), None)

    # -- Anwendungsfälle ------------------------------------------------------------------------------
    def candidates(self, limit: int = 12) -> list[Any]:
        """Top-Meldungen der letzten 48 h je Position (max. 3 je Asset), noch ohne Zusammenfassung."""
        since = iso(datetime.now(UTC) - timedelta(hours=48))
        min_rel = float(self.ctx.settings.get("news.min_relevance", 0.2))
        rows = self.db.q(
            """SELECT n.id, n.title, n.summary, na.asset_id, na.score FROM news_item n
               JOIN news_asset na ON na.item_id = n.id
               WHERE n.published_at >= ? AND n.llm_summary IS NULL AND n.hidden_reason IS NULL AND na.score >= ?
               ORDER BY na.score DESC""", (since, min_rel))
        per_asset: dict[str, int] = {}
        picked: dict[int, Any] = {}
        for r in rows:
            if r["id"] in picked or per_asset.get(r["asset_id"], 0) >= 3:
                continue
            per_asset[r["asset_id"]] = per_asset.get(r["asset_id"], 0) + 1
            picked[r["id"]] = r
            if len(picked) >= limit:
                break
        return list(picked.values())

    def build_payload(self, rows: list[Any]) -> str:
        pf = self.ctx.portfolio()
        items = []
        for r in rows:
            names = [pf.asset(a).name for a in self._assets_of(r["id"])] if pf else []
            items.append({"id": r["id"], "assets": names, "title": r["title"], "text": (r["summary"] or "")[:700]})
        return json.dumps({"meldungen": items}, ensure_ascii=False)

    def _assets_of(self, item_id: int) -> list[str]:
        return [r["asset_id"] for r in self.db.q("SELECT asset_id FROM news_asset WHERE item_id=?", (item_id,))]

    def summarize(self) -> dict[str, Any]:
        if not self.enabled():
            return {"skipped": "LLM deaktiviert"}
        rows = self.candidates()
        if not rows:
            return {"summarized": 0}
        text = self._call(SYSTEM_SUMMARY, self.build_payload(rows), 4000, SUMMARY_SCHEMA)
        if not text:
            return {"summarized": 0}
        try:
            data = json.loads(text)
        except ValueError:
            log.warning("LLM-Antwort ist kein gültiges JSON")
            return {"summarized": 0}
        ids = {r["id"] for r in rows}
        n = 0
        for it in data.get("items") or []:
            if it.get("id") in ids and it.get("summary"):
                rel = max(0.0, min(1.0, float(it.get("relevance", 0.5))))
                self.db.x("UPDATE news_item SET llm_summary=?, llm_score=? WHERE id=?",
                          (str(it["summary"])[:600], rel, it["id"]))
                n += 1
        return {"summarized": n, "budget_left": self.budget_left()}

    def digest(self) -> dict[str, Any]:
        if not self.enabled() or not self.ctx.settings.get("llm.digest", True):
            return {"skipped": "LLM/Digest deaktiviert"}
        day = today_local().isoformat()
        if self.db.q1("SELECT 1 FROM digest WHERE day=?", (day,)):
            return {"skipped": "bereits erstellt"}
        since = iso(datetime.now(UTC) - timedelta(hours=24))
        rows = self.db.q(
            """SELECT id, title, COALESCE(llm_summary, summary) AS summary FROM news_item
               WHERE published_at>=? AND hidden_reason IS NULL AND relevance>=? ORDER BY relevance DESC LIMIT 20""",
            (since, float(self.ctx.settings.get("news.min_relevance", 0.2))))
        if not rows:
            return {"skipped": "keine relevanten Meldungen"}
        text = self._call(SYSTEM_DIGEST, self.build_payload(rows), 3000, None)
        if not text:
            return {"digest": False}
        self.db.x("INSERT OR REPLACE INTO digest(day, content, created_at) VALUES (?,?,?)",
                  (day, text.strip()[:6000], iso(datetime.now(UTC))))
        return {"digest": True, "budget_left": self.budget_left()}
