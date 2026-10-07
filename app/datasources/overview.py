"""Wallet-Übersicht: Identität und Überschneidung von Konten, Wert je Konto und Gruppe, Zustände, Suche/Sortierung.

Identität eines Wallet-Kontos
    Je Chain die öffentlichen Kennungen, über die es Vorgänge abruft: Adressen (EVM klein geschrieben), bei Bitcoin
    zusätzlich der Kontoschlüssel und die daraus abgeleiteten Adressen (bis zum zuletzt geprüften Index bzw. dem
    Gap-Limit), bei Cardano die Stake-Adresse (aus Basisadressen abgeleitet). Zwei Konten **derselben Chain** mit
    gemeinsamer Kennung würden dieselben Vorgänge zweimal buchen – das verhindert die Prüfung beim Anlegen/Ändern;
    bestehende Überschneidungen werden angezeigt und in Summen nur einmal gezählt. Dieselbe 0x-Adresse auf
    verschiedenen Chains ist dagegen kein Konflikt (eigene Konten, eigene Vorgänge, keine Zusammenlegung).

Wert
    Beobachteter Bestand (letzter erfolgreicher Abruf, ``ds_balance``) × aktueller EUR-Kurs des zugeordneten Assets.
    Fehlt für ein Asset mit Bestand die Zuordnung oder der Kurs, ist der Wert „unvollständig“ (bekannter Teil als
    Mindestwert); ohne beobachteten Bestand „unbekannt“ – nie 0,00 €. Ein fehlgeschlagener Abruf löscht nichts: es
    gilt der letzte bekannte Bestand mit seinem Zeitpunkt.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

from app.util.timeutil import parse_iso

SORTS = {"name": "Name", "value": "Wert", "created": "Hinzugefügt", "synced": "Letzte erfolgreiche Synchronisierung"}
STATE_LABEL = {"never": "noch nicht synchronisiert", "running": "läuft", "ok": "erfolgreich", "partial": "teilweise",
               "error": "Fehler", "manual": "manuell (CSV)", "disabled": "deaktiviert"}
STATE_BADGE = {"never": "", "running": "info", "ok": "good", "partial": "warn", "error": "crit", "manual": "",
               "disabled": ""}
MAX_DERIVE = 400  # je Adresskette höchstens so viele abgeleitete Adressen für die Überschneidungsprüfung


# ----------------------------------------------------------------------------------------------------
# Identität und Überschneidung
# ----------------------------------------------------------------------------------------------------

@lru_cache(maxsize=64)
def _derived(xpub: str, script: str | None, n0: int, n1: int) -> frozenset[str]:
    from app.datasources.chains.btckeys import Account

    try:
        acc = Account(xpub, script)
    except ValueError:
        return frozenset()
    return frozenset([acc.addr(0, i) for i in range(n0)] + [acc.addr(1, i) for i in range(n1)])


def is_xpub(v: str) -> bool:
    """Bitcoin-Kontoschlüssel (xpub/ypub/zpub/tpub …) – nur für Bitcoin-Konten auswerten."""
    return len(v) > 100 and v[1:4] == "pub" and v[0] in "xyzXYZtuv"


def identity(provider: str, watch: Any, address: str | None, cursor_json: str | None = None) \
        -> tuple[frozenset[str], frozenset[str]]:
    """(eingetragene Kennungen, abgeleitete Adressen) eines Wallet-Kontos – normalisiert, je Chain vergleichbar."""
    addrs = list(getattr(watch, "addresses", None) or [])
    xpubs = list(getattr(watch, "xpubs", None) or [])
    if not addrs and not xpubs and address:
        (xpubs if provider == "bitcoin" and is_xpub(address) else addrs).append(address)
    explicit: set[str] = set()
    derived: frozenset[str] = frozenset()
    if provider == "cardano":
        from app.datasources.chains.codec import cardano_stake_of

        for a in addrs:
            try:
                explicit.add(cardano_stake_of(a.lower()) or a.lower())
            except ValueError:
                explicit.add(a.lower())
        return frozenset(explicit), derived
    for a in addrs:
        explicit.add(a.lower() if a.startswith(("0x", "bc1", "kaspa:")) else a)
    if provider == "bitcoin" and xpubs:
        explicit.update(xpubs)
        gap = int(getattr(watch, "gap", 20) or 20)
        n = {"0": gap, "1": gap}
        try:
            der = (json.loads(cursor_json or "{}") or {}).get("derive") or {}
            for k in n:
                n[k] = max(n[k], int((der.get(k) or {}).get("scanned") or 0))
        except (ValueError, TypeError, AttributeError):
            pass
        derived = _derived(xpubs[0], getattr(watch, "script", None), min(n["0"], MAX_DERIVE),
                           min(n["1"], MAX_DERIVE))
    return frozenset(explicit), derived


def conflict(a: tuple[frozenset[str], frozenset[str]], b: tuple[frozenset[str], frozenset[str]]) -> str | None:
    """Begründung, falls zwei Konten derselben Chain dieselben Vorgänge erfassen würden."""
    ea, da = a
    eb, db = b
    if ea & eb:
        return "gleiche Adresse bzw. gleicher Kontoschlüssel"
    if ea & db:
        return "Adresse wird bereits über den Kontoschlüssel des anderen Kontos abgedeckt"
    if da & eb:
        return "der Kontoschlüssel deckt eine dort eingetragene Einzeladresse ab"
    if da & db:
        return "abgeleitete Adressen überschneiden sich"
    return None


def overlaps(wallets: Iterable[Any]) -> dict[int, list[dict[str, Any]]]:
    """Bestehende Überschneidungen je Konto: ``{id: [{"other": id, "name": …, "why": …, "counted": bool}]}`` –
    ``counted`` False für das jüngere Konto (wird in Summen nicht noch einmal gezählt)."""
    items = [w for w in wallets if getattr(w, "is_wallet", False)]
    ids = {int(w.id): identity(w.provider, w.watch, w.address, w.row["cursor_json"]) for w in items}
    out: dict[int, list[dict[str, Any]]] = {}
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            if a.provider != b.provider:
                continue
            why = conflict(ids[int(a.id)], ids[int(b.id)])
            if why is None:
                continue
            older, newer = sorted((a, b), key=lambda w: (str(w.created_at or ""), int(w.id)))
            out.setdefault(int(older.id), []).append({"other": int(newer.id), "name": newer.name, "why": why,
                                                      "counted": True})
            out.setdefault(int(newer.id), []).append({"other": int(older.id), "name": older.name, "why": why,
                                                      "counted": False})
    return out


# ----------------------------------------------------------------------------------------------------
# Wert je Konto
# ----------------------------------------------------------------------------------------------------

@dataclass
class AccountValue:
    state: str  # ok | partial | unknown
    value: Decimal | None = None  # bekannter Teil in EUR (bei „partial“ Mindestwert)
    missing: list[str] = field(default_factory=list)  # Assets mit Bestand ohne Zuordnung bzw. Kurs
    stale_prices: list[str] = field(default_factory=list)
    observed_at: datetime | None = None
    positions: int = 0

    @property
    def label(self) -> str:
        return {"ok": "", "partial": "mind.", "unknown": "unbekannt"}.get(self.state, "")


def account_values(ctx: Any, wallets: list[Any]) -> dict[int, AccountValue]:
    """Wert der beobachteten Bestände je Wallet-Konto (Zuordnung wie im Prüf-Stapel, Kurse wie im Dashboard)."""
    out: dict[int, AccountValue] = {}
    if not wallets:
        return out
    from app.csvimport.service import SymbolResolver, csv_service

    db = ctx.db
    rows: dict[int, list[Any]] = {}
    marks = ",".join("?" * len(wallets))
    for r in db.q(f"SELECT source_id, asset_key, qty, observed_at FROM ds_balance WHERE source_id IN ({marks})",
                  [int(w.id) for w in wallets]):
        rows.setdefault(int(r["source_id"]), []).append(r)
    csv = csv_service(ctx)
    resolver = SymbolResolver(csv.known_assets(), csv.saved_symbols())
    pf = ctx.recorded_portfolio() if hasattr(ctx, "recorded_portfolio") else None
    if pf is None:  # ohne Import und ohne Buchungen: Asset-Stammdaten der App genügen für die Bewertung
        from app.journal.service import journal_asset_infos
        from app.ledger.models import Portfolio

        pf = Portfolio(None, [], journal_asset_infos(db), {})
    resolved: dict[int, list[tuple[str, Decimal, str | None, str]]] = {}
    wanted: set[str] = set()
    for w in wallets:
        lst = []
        for r in rows.get(int(w.id), []):
            try:
                q = Decimal(str(r["qty"]))
            except (InvalidOperation, TypeError):
                continue
            if not q:
                continue
            aid, how = resolver.resolve(r["asset_key"])
            if how == "ignored":
                continue
            lst.append((r["asset_key"], q, aid, r["observed_at"]))
            if aid:
                wanted.add(aid)
        resolved[int(w.id)] = lst
    prices: dict[str, Any] = {}
    if wanted:
        try:
            prices = ctx.prices.latest_eur_many([pf.asset(a) for a in sorted(wanted)], pf)
        except Exception:  # Kurse sind Zusatzinformation – die Übersicht muss immer laden
            prices = {}
    for w in wallets:
        lst = resolved[int(w.id)]
        obs = [parse_iso(o) for *_x, o in lst if o]
        has_rows = bool(rows.get(int(w.id)))
        if not has_rows:
            out[int(w.id)] = AccountValue("unknown")
            continue
        total = Decimal(0)
        missing: list[str] = []
        stale: list[str] = []
        for key, q, aid, _o in lst:
            p = prices.get(aid) if aid else None
            if p is None or not getattr(p, "valued", False) or not p.price_eur:
                missing.append(_short_key(key) + ("" if aid else " (nicht zugeordnet)"))
                continue
            total += q * Decimal(str(p.price_eur))
            if getattr(p, "stale", False):
                stale.append(aid)  # type: ignore[arg-type]
        when = max((o for o in obs if o is not None), default=None)
        if when is None:
            when = max((parse_iso(r["observed_at"]) for r in rows[int(w.id)] if r["observed_at"]),
                       default=None)
        out[int(w.id)] = AccountValue("partial" if missing else "ok", total.quantize(Decimal("0.01")), missing,
                                      stale, when, len(lst))
    return out


def _short_key(key: str) -> str:
    sym, at, rest = key.partition("@")
    return f"{sym} ({rest.split(':', 1)[0]})" if at else key


# ----------------------------------------------------------------------------------------------------
# Zustand, Hinweise, Suche, Sortierung, Gruppen
# ----------------------------------------------------------------------------------------------------

def sync_state(ds: Any) -> str:
    """Technisches Ergebnis des Abrufs: never | running | ok | partial | error | manual | disabled."""
    if not ds.supported:
        return "manual"
    if ds.progress.get("running"):
        return "running"
    st = ds.row["status"]
    if st == "error":
        return "error"
    if not ds.last_success_at and st in ("created", "connected"):
        return "never" if ds.enabled else "disabled"
    cov = ds.coverage
    if st == "partial" or cov.get("resume") or cov.get("gaps") or (cov and not cov.get("complete", True)):
        return "partial"
    return "ok" if ds.last_success_at else "never"


def data_notes(ds: Any, value: AccountValue | None, opens: Mapping[str, int], holdings: Mapping[str, Any] | None,
               overlap: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """Datenhinweise getrennt vom technischen Abruf: ungeklärte/unvollständige Vorgänge, fehlende Kurse bzw.
    Zuordnungen, Bestandsabweichungen, Überschneidungen."""
    out: list[dict[str, str]] = []

    def n(k: str, one: str, many: str) -> str:
        return f"{opens[k]} {one if opens[k] == 1 else many}"

    if opens.get("unclear"):
        out.append({"kind": "unclear", "text": n("unclear", "ungeklärter bzw. nicht unterstützter Vorgang",
                                                 "ungeklärte bzw. nicht unterstützte Vorgänge") + " zur Prüfung"})
    if opens.get("invalid"):
        out.append({"kind": "invalid", "text": n("invalid", "unvollständiger Vorgang", "unvollständige Vorgänge")
                                               + " (z. B. Zuordnung oder EUR-Wert fehlt)"})
    if opens.get("duplicate"):
        out.append({"kind": "duplicate", "text": n("duplicate", "mögliche Dublette", "mögliche Dubletten")
                                                 + " zur Entscheidung"})
    if opens.get("new"):
        out.append({"kind": "new", "text": n("new", "neuer Vorgang", "neue Vorgänge") + " noch nicht übernommen"})
    if value is not None and value.missing:
        out.append({"kind": "price", "text": "ohne Kurs bzw. Zuordnung: " + ", ".join(value.missing[:5])
                                             + (f" (+{len(value.missing) - 5})" if len(value.missing) > 5 else "")})
    if value is not None and value.stale_prices:
        out.append({"kind": "stale", "text": "Kurs veraltet: " + ", ".join(value.stale_prices[:5])})
    if holdings and holdings.get("diffs"):
        d = int(holdings["diffs"])
        out.append({"kind": "diff", "text": f"{d} Bestandsabweichung{'' if d == 1 else 'en'} zwischen Chain und "
                                            "Buchungen (Details: Bestände)"})
    for o in overlap or []:
        out.append({"kind": "overlap", "text": f"überschneidet sich mit „{o['name']}“ ({o['why']})"
                                               + ("" if o["counted"] else " – in Summen nicht noch einmal gezählt")})
    return out


def matches(ds: Any, q: str) -> bool:
    """Suche über Konto-, Gruppen- und Portfolia-Kontoname, Netzwerk und Adresse (ohne Groß-/Kleinschreibung)."""
    q = (q or "").strip().lower()
    if not q:
        return True
    hay = " ".join([str(ds.name or ""), str(ds.group or ""), str(ds.account or ""), str(ds.provider_label or ""),
                    str(ds.provider or ""), *[str(a) for a in ds.addresses]]).lower()
    return all(part in hay for part in q.split())


def sort_key(sort: str, values: Mapping[int, AccountValue]) -> Any:
    def key(ds: Any) -> Any:
        if sort == "value":
            v = values.get(int(ds.id))
            return (v is None or v.value is None, -(v.value or 0) if v else 0, str(ds.name).lower())
        if sort == "created":
            return (str(ds.created_at or ""), int(ds.id))
        if sort == "synced":
            return (ds.last_success_at is None, _neg_ts(ds.last_success_at), str(ds.name).lower())
        return (str(ds.name).lower(), int(ds.id))
    return key


def _neg_ts(v: str | None) -> float:
    t = parse_iso(v) if v else None
    return -(t.timestamp() if t else 0.0)


def group_summary(accounts: list[Any], values: Mapping[int, AccountValue],
                  overlap: Mapping[int, list[dict[str, Any]]]) -> dict[str, Any]:
    """Gesamtwert einer Gruppe: Summe der Kontowerte; überschneidende jüngere Konten zählen nicht noch einmal.
    ``state`` partial, sobald ein Konto unbekannt oder unvollständig ist (dann Mindestwert)."""
    total = Decimal(0)
    state = "ok"
    unknown = 0
    skipped = 0
    for ds in accounts:
        if any(not o["counted"] for o in overlap.get(int(ds.id), [])):
            skipped += 1
            continue
        v = values.get(int(ds.id))
        if v is None or v.value is None:
            unknown += 1
            state = "partial"
            continue
        total += v.value
        if v.state != "ok":
            state = "partial"
    if unknown == len(accounts) - skipped and accounts:
        state = "unknown"
    return {"value": total if state != "unknown" else None, "state": state, "unknown": unknown,
            "skipped": skipped, "count": len(accounts)}


def age_text(ts: datetime | None, now: datetime | None = None) -> str:
    if ts is None:
        return ""
    s = ((now or datetime.now(UTC)) - ts).total_seconds()
    if s < 90:
        return "gerade eben"
    if s < 3600:
        return f"vor {int(s // 60)} Min."
    if s < 2 * 86400:
        return f"vor {int(s // 3600)} Std."
    return f"vor {int(s // 86400)} Tagen"
