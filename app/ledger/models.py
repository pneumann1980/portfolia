"""Fachliche Datenobjekte des Ledgers (unabhängig von DB und Web)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.util.timeutil import parse_iso


def D(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v))


@dataclass(slots=True)
class Tx:
    seq: int
    tx_id: str
    ts: datetime
    date: date
    date_only: bool
    type: str
    tag: str | None
    from_account: str | None
    from_asset: str | None
    from_qty: Decimal | None
    to_account: str | None
    to_asset: str | None
    to_qty: Decimal | None
    fee_asset: str | None
    fee_qty: Decimal | None
    fee_eur: Decimal | None
    value_eur: Decimal | None
    orig_price: str | None = None
    orig_ccy: str | None = None
    source: str | None = None
    source_ref: str | None = None
    flag: str | None = None
    note: str | None = None
    related_asset: str | None = None

    @classmethod
    def from_parsed(cls, r: dict[str, Any]) -> Tx:
        return cls(
            seq=r["seq"], tx_id=r["tx_id"], ts=r["ts_utc"], date=r["date_local"], date_only=bool(r["date_only"]),
            type=r["type"], tag=r["tag"],
            from_account=r["from_account"], from_asset=r["from_asset"], from_qty=r["from_qty"],
            to_account=r["to_account"], to_asset=r["to_asset"], to_qty=r["to_qty"],
            fee_asset=r["fee_asset"], fee_qty=r["fee_qty"], fee_eur=r["fee_eur"], value_eur=r["value_eur"],
            orig_price=r.get("orig_price"), orig_ccy=r.get("orig_ccy"), source=r.get("source"),
            source_ref=r.get("source_ref"), flag=r.get("flag"), note=r.get("note"),
            related_asset=r.get("related_asset"),
        )

    @classmethod
    def from_row(cls, r: Any) -> Tx:
        return cls(
            seq=r["seq"], tx_id=r["tx_id"], ts=parse_iso(r["ts_utc"]),  # type: ignore[arg-type]
            date=date.fromisoformat(r["date_local"]), date_only=bool(r["date_only"]),
            type=r["type"], tag=r["tag"],
            from_account=r["from_account"], from_asset=r["from_asset"], from_qty=D(r["from_qty"]),
            to_account=r["to_account"], to_asset=r["to_asset"], to_qty=D(r["to_qty"]),
            fee_asset=r["fee_asset"], fee_qty=D(r["fee_qty"]), fee_eur=D(r["fee_eur"]), value_eur=D(r["value_eur"]),
            orig_price=r["orig_price"], orig_ccy=r["orig_ccy"], source=r["source"], source_ref=r["source_ref"],
            flag=r["flag"], note=r["note"], related_asset=r["related_asset"],
        )


@dataclass(slots=True)
class AssetInfo:
    asset_id: str
    name: str
    asset_class: str  # security | crypto | fiat
    quote_source: str = "none"
    quote_id: str | None = None
    category: str | None = None
    aliases: list[str] = field(default_factory=list)
    wkn: str | None = None
    isin: str | None = None
    koinly_id: str | None = None
    status: str | None = None
    note: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_fiat(self) -> bool:
        return self.asset_class == "fiat"

    @property
    def is_crypto(self) -> bool:
        return self.asset_class == "crypto"

    @property
    def is_security(self) -> bool:
        return self.asset_class == "security"

    @property
    def segment(self) -> str:
        return {"security": "Aktien", "crypto": "Krypto", "fiat": "Cash"}.get(self.asset_class, "Sonstige")

    @property
    def category_label(self) -> str:
        if self.category:
            return self.category
        return {"security": "Aktien: Sonstige", "crypto": "Krypto: Sonstige", "fiat": "Cash"}[self.asset_class]

    @property
    def symbol(self) -> str:
        """Kurzbezeichnung für Tabellen (Ticker/Symbol)."""
        aid = self.asset_id
        if self.is_crypto:
            return aid.split("#", 1)[0]
        if self.quote_source == "yahoo" and self.quote_id:
            return self.quote_id.split(".", 1)[0]
        if aid.startswith("WKN:"):
            return aid[4:]
        return aid

    @classmethod
    def from_parsed(cls, r: dict[str, Any]) -> AssetInfo:
        aliases = [a.strip() for a in (r.get("aliases") or "").split(";") if a.strip()]
        return cls(asset_id=r["asset_id"], name=r.get("name") or r["asset_id"], asset_class=r["asset_class"],
                   quote_source=r.get("quote_source") or "none", quote_id=r.get("quote_id"),
                   category=r.get("category"), aliases=aliases, wkn=r.get("wkn"), isin=r.get("isin"),
                   koinly_id=r.get("koinly_id"), status=r.get("status"), note=r.get("note"),
                   extra=r.get("extra") or {})

    @classmethod
    def from_row(cls, r: Any) -> AssetInfo:
        d = dict(r)
        d["extra"] = json.loads(d.get("extra_json") or "{}")
        return cls.from_parsed(d)


@dataclass(slots=True)
class AccountInfo:
    account: str
    broker: str | None = None
    depot_group: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_parsed(cls, r: dict[str, Any]) -> AccountInfo:
        return cls(account=r["account"], broker=r.get("broker"), depot_group=r.get("depot_group"),
                   extra=r.get("extra") or {})

    @classmethod
    def from_row(cls, r: Any) -> AccountInfo:
        return cls(account=r["account"], broker=r["broker"], depot_group=r["depot_group"],
                   extra=json.loads(r["extra_json"] or "{}"))


@dataclass(slots=True)
class Portfolio:
    """Eingangsdaten eines Importstands."""

    import_id: int | None
    txs: list[Tx]
    assets: dict[str, AssetInfo]
    accounts: dict[str, AccountInfo]
    manual_prices: dict[str, list[tuple[date, float]]] = field(default_factory=dict)
    holdings_check: list[dict[str, Any]] = field(default_factory=list)
    valuation_date: date | None = None

    def asset(self, asset_id: str) -> AssetInfo:
        a = self.assets.get(asset_id)
        if a is None:
            a = AssetInfo(asset_id=asset_id, name=asset_id, asset_class="crypto")
            self.assets[asset_id] = a
        return a

    def all_accounts(self) -> list[str]:
        accs = set(self.accounts)
        for t in self.txs:
            for a in (t.from_account, t.to_account):
                if a:
                    accs.add(a)
        return sorted(accs)

    def depot_group(self, account: str) -> str:
        info = self.accounts.get(account)
        return (info.depot_group if info and info.depot_group else account)

    def split_events(self) -> dict[str, list[tuple[date, float]]]:
        """Aktiensplits aus Kapitalmaßnahmen (gleiches Asset, Verhältnis ≠ 1) je Asset, chronologisch."""
        out: dict[str, list[tuple[date, float]]] = {}
        for t in self.txs:
            if (t.type == "corporate_action" and t.from_asset and t.from_asset == t.to_asset and t.from_qty
                    and t.to_qty and t.from_qty != t.to_qty):
                out.setdefault(t.from_asset, []).append((t.date, float(t.to_qty / t.from_qty)))
        for v in out.values():
            v.sort()
        return out

    def split_factor_after(self, asset_id: str, d: date) -> float:
        """Kumuliertes Split-Verhältnis aller Splits *nach* Tag d (für Umrechnung auf heutige Stückbasis)."""
        f = 1.0
        for sd, ratio in self.split_events().get(asset_id, []):
            if sd > d:
                f *= ratio
        return f
