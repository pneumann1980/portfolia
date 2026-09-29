"""Zwischenspeicher für Stammdaten eines Anbieters (Asset-/Währungs-IDs → Symbol, Typ, ISIN).

Liegt in ``ds_asset_cache`` und spart wiederholte Abrufe: je ID höchstens ein Abruf pro ``TTL_DAYS``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.util.timeutil import iso, parse_iso

TTL_DAYS = 30


class Catalog:
    def __init__(self, db: Any, provider: str) -> None:
        self.db = db
        self.provider = provider
        self._mem: dict[str, dict[str, Any]] = {}

    def get(self, remote_id: str) -> dict[str, Any] | None:
        if remote_id in self._mem:
            return self._mem[remote_id]
        r = self.db.q1("SELECT kind, symbol, name, asset_type, isin, fetched_at FROM ds_asset_cache WHERE provider=? "
                       "AND remote_id=?", (self.provider, remote_id))
        if r is None:
            return None
        fetched = parse_iso(r["fetched_at"])
        if fetched is None or datetime.now(UTC) - fetched > timedelta(days=TTL_DAYS):
            return None
        out = {"kind": r["kind"], "symbol": r["symbol"], "name": r["name"], "type": r["asset_type"], "isin": r["isin"]}
        self._mem[remote_id] = out
        return out

    def put(self, remote_id: str, kind: str, symbol: str | None, name: str | None = None,
            asset_type: str | None = None, isin: str | None = None) -> None:
        self._mem[remote_id] = {"kind": kind, "symbol": symbol, "name": name, "type": asset_type, "isin": isin}
        self.db.x("INSERT INTO ds_asset_cache(provider, remote_id, kind, symbol, name, asset_type, isin, fetched_at) "
                  "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(provider, remote_id) DO UPDATE SET kind=excluded.kind, "
                  "symbol=excluded.symbol, name=excluded.name, asset_type=excluded.asset_type, isin=excluded.isin, "
                  "fetched_at=excluded.fetched_at",
                  (self.provider, remote_id, kind, symbol, name, asset_type, isin, iso(datetime.now(UTC))))


class MemoryCatalog(Catalog):
    """Ohne Datenbank (Tests, Einzelaufrufe)."""

    def __init__(self, provider: str = "") -> None:  # bewusst ohne Datenbank
        self.provider = provider
        self._mem = {}

    def get(self, remote_id: str) -> dict[str, Any] | None:
        return self._mem.get(remote_id)

    def put(self, remote_id: str, kind: str, symbol: str | None, name: str | None = None,
            asset_type: str | None = None, isin: str | None = None) -> None:
        self._mem[remote_id] = {"kind": kind, "symbol": symbol, "name": name, "type": asset_type, "isin": isin}
