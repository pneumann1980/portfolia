"""Konfiguration ausschließlich aus Umgebungsvariablen.

API-Keys werden hier gelesen und nur über :class:`Secrets` herausgegeben. Sie erscheinen nie im
UI oder in Logs (siehe ``logging_setup.SecretRedactor``).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on", "ja"}


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    if val is None:
        return default
    val = val.strip()
    return val if val != "" else default


def _env_bool(name: str, default: bool = False) -> bool:
    val = _env(name)
    if val is None:
        return default
    return val.lower() in _TRUE


def _env_int(name: str, default: int) -> int:
    val = _env(name)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        log.warning("Ungültiger Integer für %s=%r, verwende %s", name, val, default)
        return default


@dataclass(frozen=True)
class Secrets:
    coingecko_api_key: str | None = None
    finnhub_api_key: str | None = None
    youtube_api_key: str | None = None
    anthropic_api_key: str | None = None
    cryptopanic_api_key: str | None = None

    def values(self) -> list[str]:
        return [v for v in (self.coingecko_api_key, self.finnhub_api_key, self.youtube_api_key,
                            self.anthropic_api_key, self.cryptopanic_api_key) if v]

    def status(self) -> dict[str, bool]:
        """Nur 'gesetzt' / 'nicht gesetzt' – niemals den Wert."""
        return {
            "COINGECKO_API_KEY": bool(self.coingecko_api_key),
            "FINNHUB_API_KEY": bool(self.finnhub_api_key),
            "YOUTUBE_API_KEY": bool(self.youtube_api_key),
            "ANTHROPIC_API_KEY": bool(self.anthropic_api_key),
            "CRYPTOPANIC_API_KEY": bool(self.cryptopanic_api_key),
        }


@dataclass(frozen=True)
class Config:
    data_dir: Path = Path("/data")
    import_dir: Path = Path("/import")
    port: int = 8080
    tz: str = "Europe/Berlin"
    base_currency: str = "EUR"
    log_level: str = "INFO"
    log_format: str = "json"
    auth_mode: str = "none"  # none | basic
    auth_user: str | None = None
    auth_password_hash: str | None = None
    root_path: str = ""
    coingecko_plan: str = "demo"  # demo | pro
    demo_mode: bool = False
    scheduler_enabled: bool = True
    startup_jobs: bool = True
    secrets: Secrets = field(default_factory=Secrets)
    export_path: Path | None = None  # EXPORT_DIR; Standard: <DATA_DIR>/exports
    fx_frankfurter_url: str = "https://api.frankfurter.dev/v1"
    fx_frankfurter_fallback_url: str = "https://api.frankfurter.app"
    ecb_hist_url: str = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"
    user_agent: str = "Portfolia/0.5 (+self-hosted; LAN)"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.sqlite"

    @property
    def sources_path(self) -> Path:
        return self.data_dir / "sources.yaml"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def export_dir(self) -> Path:
        """Datierte ZIP-Sicherungen im Import-Format und Archiv der importierten ZIP-Dateien."""
        return self.export_path or self.data_dir / "exports"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def tax_rules_dir(self) -> Path:
        return self.data_dir / "tax_rules"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @classmethod
    def from_env(cls) -> Config:
        base = _env("BASE_CURRENCY", "EUR") or "EUR"
        if base.upper() != "EUR":
            log.error("BASE_CURRENCY=%s wird nicht unterstützt – es wird EUR verwendet.", base)
        auth_mode = (_env("AUTH_MODE", "none") or "none").lower()
        if auth_mode not in {"none", "basic"}:
            log.error("AUTH_MODE=%s unbekannt – verwende 'basic' (sicherer Fallback).", auth_mode)
            auth_mode = "basic"
        plan = (_env("COINGECKO_PLAN", "demo") or "demo").lower()
        return cls(
            data_dir=Path(_env("DATA_DIR", "/data") or "/data"),
            import_dir=Path(_env("IMPORT_DIR", "/import") or "/import"),
            port=_env_int("PORT", 8080),
            tz=_env("TZ", "Europe/Berlin") or "Europe/Berlin",
            base_currency="EUR",
            log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
            log_format=(_env("LOG_FORMAT", "json") or "json").lower(),
            auth_mode=auth_mode,
            auth_user=_env("AUTH_USER"),
            auth_password_hash=_env("AUTH_PASSWORD_HASH"),
            root_path=(_env("ROOT_PATH", "") or "").rstrip("/"),
            coingecko_plan=plan if plan in {"demo", "pro"} else "demo",
            demo_mode=_env_bool("DEMO_MODE", False),
            scheduler_enabled=_env_bool("SCHEDULER_ENABLED", True),
            startup_jobs=_env_bool("STARTUP_JOBS", True),
            secrets=Secrets(
                coingecko_api_key=_env("COINGECKO_API_KEY"),
                finnhub_api_key=_env("FINNHUB_API_KEY"),
                youtube_api_key=_env("YOUTUBE_API_KEY"),
                anthropic_api_key=_env("ANTHROPIC_API_KEY"),
                cryptopanic_api_key=_env("CRYPTOPANIC_API_KEY"),
            ),
            fx_frankfurter_url=_env("FX_FRANKFURTER_URL", cls.fx_frankfurter_url) or cls.fx_frankfurter_url,
            export_path=Path(v) if (v := _env("EXPORT_DIR")) else None,
        )

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.backup_dir, self.cache_dir, self.reports_dir, self.tax_rules_dir,
                  self.cache_dir / "img", self.cache_dir / "yfinance", self.log_dir):
            p.mkdir(parents=True, exist_ok=True)
        try:
            (self.export_dir / "import-archiv").mkdir(parents=True, exist_ok=True)
        except OSError as e:  # z. B. nicht beschreibbares EXPORT_DIR – Export meldet den Fehler später
            log.error("Exportverzeichnis %s nicht beschreibbar: %s", self.export_dir, e)
