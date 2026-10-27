"""Benutzereinstellungen (in der App-DB) mit Standardwerten.

Portfolio-Daten sind read-only; hier liegen nur Darstellungs-, Berechnungs- und Betriebsparameter.
"""

from __future__ import annotations

import copy
import json
import threading
from datetime import UTC, datetime
from typing import Any

from app.db import Database

DEFAULTS: dict[str, Any] = {
    # Darstellung
    "allocation.other_threshold_pct": 1.0,
    "ui.default_range": "1J",
    # Ledger / Performance
    "ledger.scope": "global",  # global | account
    "ledger.unmatched_transfers_as_flows": True,
    "ledger.cash_overrides": {},  # {konto: true|false}
    "performance.benchmarks": [
        {"id": "msci_world", "name": "MSCI World (iShares Core, EUNL)", "series": "yahoo:EUNL.DE"},
        {"id": "btc", "name": "Bitcoin (EUR)", "series": "yahoo:BTC-EUR"},
    ],
    # Kurse
    "prices.stale_crypto_minutes": 60,  # Krypto: Alter des letzten erfolgreichen Abrufs
    "prices.stale_crypto_market_hours": 24,  # Krypto: letzte Kursänderung laut Quelle (wenig Handel)
    "prices.stale_security_hours": 24,
    # „Kurs veraltet“ für diese Assets nicht in der Übersicht melden (Datenqualität zeigt sie weiter, umkehrbar)
    "prices.stale_ignored": [],
    # Ersatzkurse (manuell/Transaktionskurs) ohne Marktkurse: Gültigkeit nach dem letzten Kurs, 0 = unbegrenzt
    "prices.fallback_max_age_crypto_days": 30,
    "prices.fallback_max_age_security_days": 365,
    # Kursquellen-Suche (CoinGecko) für Kryptowerte ohne Quelle: automatisch übernehmen bis Sicherheit hoch|mittel|aus
    "prices.auto_map": "hoch",
    "prices.crypto_interval_min": 10,
    "prices.crypto_throttled_interval_min": 30,
    "prices.stock_interval_min": 15,
    "prices.coingecko_monthly_limit": 10000,
    "prices.coingecko_throttle_pct": 80,
    "prices.coingecko_history_days": 365,  # Demo-API: Historie max. 365 Tage
    # Yahoo-Symbole für Krypto-Historie vor dem CoinGecko-Fenster (ausdrückliche Zuordnung, Vorrang)
    "prices.crypto_history_fallback": {"BTC": "BTC-EUR", "ETH": "ETH-EUR"},
    # übrige Krypto-Historie automatisch über Yahoo (SYMBOL-EUR/-USD) – nur nach bestandenem Abgleich mit CoinGecko
    "prices.crypto_history_auto": True,
    # Autopilot nach Importen (Bericht immer; Aktionen nur bei eingeschaltetem Autopilot, Standard aus)
    "autopilot.enabled": False,
    "autopilot.rules": True,  # Entscheidungsregeln (Hinweise/Verdacht, ohne Datenänderung) anwenden
    "autopilot.fixes": False,  # beweisbare Korrekturen (Ausblenden/Verknüpfen/Transfer aus zwei vorhandenen Buchungen)
    "autopilot.fixes_level": "eindeutig",  # eindeutig | hoch plausibel
    # Abgang + Rückfluss auf demselben Konto (z. B. KRC-20-Mint) als Netzwerkgebühr zusammenfassen – eigene Freigabe,
    # unabhängig von ``autopilot.fixes`` (Standard aus; nur mit eingeschaltetem Autopilot)
    "autopilot.roundtrips": False,
    # Konfliktklärung: untersucht offene Befunde nach Importen/Abrufen/Nachweisen (ändert ohne Freigabe nichts; die
    # automatische Übernahme folgt ``autopilot.enabled`` + ``autopilot.fixes``)
    "clarify.enabled": True,
    "clarify.research": True,  # fehlende Informationen gezielt (lesend, begrenzt) bei der Datenquelle nachladen
    "clarify.limit": 64,  # Hypothesen je Gruppe (darüber: „Prüfumfang überschritten“)
    "clarify.budget": 400,  # Simulationen je Durchgang
    # News
    "news.min_relevance": 0.2,
    "news.dashboard_count": 5,
    # LLM (optional, standardmäßig aus)
    "llm.enabled": False,
    "llm.daily_token_budget": 60000,
    "llm.model": "claude-opus-5",
    "llm.digest": True,
    # KI-Anbieter: claude | gemini. Gemini nutzt nur kostenlos verfügbare Flash-/Flash-Lite-Modelle; die lokalen
    # Grenzen (je Minute/Tag) sind bewusst konservativ und ersetzen keine Prüfung des eigenen Google-Kontingents.
    "llm.provider": "claude",
    "llm.gemini_model": "gemini-3.5-flash-lite",
    "llm.gemini_rpm": 5,
    "llm.gemini_tpm": 100000,
    "llm.gemini_rpd": 100,
    "llm.gemini_daily_token_budget": 200000,
    "llm.gemini_tokens_unlimited": False,  # Token-Tagesgrenze ausdrücklich aufheben (Anfragen-Limits bleiben)
    # News-Intelligence: Artikelinhalt, Analyse, Ereignisse, Tagesbericht
    "news.enrich": True,  # Volltext nachladen (Hintergrundjob, getrennt vom Feed-Import)
    "news.enrich_per_run": 40,
    "news.enrich_days": 3,
    "news.content_mode": "full",  # full = bereinigter Volltext | excerpt = nur Auszug (~1500 Zeichen)
    "news.analysis": True,  # strukturierte Einzelanalyse (nur mit aktivierter KI)
    "news.analysis_max_per_run": 400,  # Obergrenze je Lauf; der Rückstau wird in Runden abgearbeitet
    "news.analysis_hours": 48,
    "news.analysis_wait_min": 60,  # so lange (Min. ab Import) wartet die Analyse höchstens auf den Artikeltext
    "news.analysis_protect": True,  # Rückstau-Schutz: bei nicht rechtzeitig schaffbarem Rückstau nur frische Meldungen
    "news.analysis_protect_hours": 6,  # … ab dieser geschätzten Abarbeitungsdauer (Std.)
    "news.analysis_protect_age_h": 24,  # … und nur Meldungen bis zu diesem Alter (Std.), neueste zuerst
    "news.group": True,  # Ereignisgruppierung (lokal; KI nur für Grenzfälle)
    "news.group_hours": 72,
    "news.group_llm": True,
    "news.report": True,  # täglicher Finanzmarktbericht (nur mit aktivierter KI)
    "news.report_hour": 7,
    "news.report_minute": 30,
    "news.report_hours": 24,
    # Steuer
    "tax.rulepack": "auto",
    "tax.account_withholding": {},  # {konto: domestic|foreign}
    "tax.asset_types": {},  # {asset_id: share|etf_equity|etf_mixed|etf_other|fund_realestate|bond|other}
    "tax.options": {},  # je Regelwerk/Jahr, siehe tax.service
    "tax.profile": {"name": "", "tax_id": "", "tax_number": ""},
    "taxdata.auto_scan": True,  # Steuerdaten-Ordner alle 15 Minuten prüfen (zusätzlich beim Start und auf Knopfdruck)
    # Dokumentimport (M25)
    "documents.keep_originals": True,  # Originalbelege lokal unter /data/documents aufbewahren
    "documents.ocr": True,  # lokale OCR (Tesseract) für Scans und Screenshots
    "documents.language": "deu+eng",
    "documents.public_lookup": False,  # öffentliche Explorer (nur Tx-Hash) – ausdrücklich freigeben
    # Betrieb
    "backup.keep": 14,
    "backup.hour": 3,
    "export.auto": True,  # datierte ZIP-Sicherung (Import-Format) nach Änderungen
    "export.keep": 30,
    "export.import_keep": 20,  # Kopien importierter ZIP-Dateien
}


class Settings:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._cache: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self.version = 0

    def _load(self) -> dict[str, Any]:
        with self._lock:
            if self._cache is None:
                vals = copy.deepcopy(DEFAULTS)
                for r in self.db.q("SELECT key, value_json FROM settings"):
                    try:
                        vals[r["key"]] = json.loads(r["value_json"])
                    except ValueError:
                        continue
                self._cache = vals
            return self._cache

    def get(self, key: str, default: Any = None) -> Any:
        vals = self._load()
        if key in vals:
            return copy.deepcopy(vals[key])
        return default

    def all(self) -> dict[str, Any]:
        return copy.deepcopy(self._load())

    def set(self, key: str, value: Any) -> None:
        self.db.x(
            "INSERT INTO settings(key, value_json, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), datetime.now(UTC).isoformat(timespec="seconds")),
        )
        with self._lock:
            self._cache = None
            self.version += 1

    def reload(self) -> None:
        """Zwischenspeicher verwerfen (nach direktem Schreiben in die Tabelle, z. B. Neueinrichtung aus Export)."""
        with self._lock:
            self._cache = None
            self.version += 1

    def reset(self, key: str) -> None:
        self.db.x("DELETE FROM settings WHERE key=?", (key,))
        with self._lock:
            self._cache = None
            self.version += 1
