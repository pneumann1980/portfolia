"""Testhilfen: kompakte Transaktions-/Asset-Definitionen und Portfolio-Aufbau."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.ledger.models import AccountInfo, AssetInfo, Portfolio, Tx
from app.util.timeutil import parse_tx_datetime, to_local_date

ASSETS = [
    {"asset_id": "EUR", "name": "Euro", "asset_class": "fiat", "quote_source": "none", "quote_id": "",
     "category": "Cash"},
    {"asset_id": "USD", "name": "US-Dollar", "asset_class": "fiat", "quote_source": "none", "quote_id": "",
     "category": "Cash"},
    {"asset_id": "WKN:A0B1C2", "name": "Muster AG", "asset_class": "security", "wkn": "A0B1C2",
     "quote_source": "yahoo", "quote_id": "MUS.DE", "category": "Aktien: Einzeltitel", "aliases": "Muster AG;MUS"},
    {"asset_id": "WKN:US0001", "name": "Example Corp", "asset_class": "security", "wkn": "US0001",
     "quote_source": "yahoo", "quote_id": "EXMP", "category": "Aktien: Einzeltitel", "aliases": "Example Corp;EXMP"},
    {"asset_id": "BTC", "name": "Bitcoin", "asset_class": "crypto", "quote_source": "coingecko", "quote_id": "bitcoin",
     "category": "Krypto: BTC/ETH", "aliases": "Bitcoin;BTC"},
    {"asset_id": "ETH", "name": "Ethereum", "asset_class": "crypto", "quote_source": "coingecko",
     "quote_id": "ethereum", "category": "Krypto: BTC/ETH", "aliases": "Ethereum;ETH;Ether"},
    {"asset_id": "BNB", "name": "BNB", "asset_class": "crypto", "quote_source": "coingecko", "quote_id": "binancecoin",
     "category": "Krypto: Altcoins", "aliases": "BNB"},
    {"asset_id": "KAS", "name": "Kaspa", "asset_class": "crypto", "quote_source": "coingecko", "quote_id": "kaspa",
     "category": "Krypto: Small Caps", "aliases": "Kaspa;KAS"},
]


def tx(tx_id: str, dt: str, typ: str, *, tag: str | None = None, frm: tuple | None = None, to: tuple | None = None,
       fee: tuple | None = None, value: str | float | None = None, related: str | None = None) -> dict[str, Any]:
    """Transaktion als CSV-Zeile (Strings). frm/to = (account, asset, qty); fee = (asset, qty, eur)."""
    r: dict[str, Any] = {"tx_id": tx_id, "datetime": dt, "type": typ, "tag": tag or ""}
    for side, leg in (("from", frm), ("to", to)):
        acc, asset, qty = leg if leg else ("", "", "")
        r[f"{side}_account"], r[f"{side}_asset"], r[f"{side}_qty"] = acc, asset, str(qty) if qty != "" else ""
    fa, fq, fe = fee if fee else ("", "", "")
    r["fee_asset"], r["fee_qty"], r["fee_eur"] = fa, str(fq) if fq != "" else "", str(fe) if fe != "" else ""
    r["value_eur"] = "" if value is None else str(value)
    if related:
        r["related_asset"] = related
    return r


def _d(v: Any) -> Decimal | None:
    return None if v in (None, "") else Decimal(str(v))


def portfolio(rows: list[dict[str, Any]], assets: list[dict[str, Any]] | None = None,
              accounts: list[dict[str, Any]] | None = None) -> Portfolio:
    txs = []
    for seq, r in enumerate(rows):
        ts, date_only = parse_tx_datetime(r["datetime"])
        txs.append(Tx(
            seq=seq, tx_id=r["tx_id"], ts=ts, date=to_local_date(ts), date_only=date_only, type=r["type"],
            tag=r.get("tag") or None,
            from_account=r.get("from_account") or None, from_asset=r.get("from_asset") or None,
            from_qty=_d(r.get("from_qty")),
            to_account=r.get("to_account") or None, to_asset=r.get("to_asset") or None, to_qty=_d(r.get("to_qty")),
            fee_asset=r.get("fee_asset") or None, fee_qty=_d(r.get("fee_qty")), fee_eur=_d(r.get("fee_eur")),
            value_eur=_d(r.get("value_eur")), related_asset=r.get("related_asset") or None,
        ))
    return Portfolio(
        import_id=1, txs=txs,
        assets={a["asset_id"]: AssetInfo.from_parsed({**a, "aliases": a.get("aliases")}) for a in (assets or ASSETS)},
        accounts={a["account"]: AccountInfo.from_parsed(a) for a in (accounts or [])},
    )
