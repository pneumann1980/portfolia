"""Vorschläge für unbekannte Symbole im Prüf-Stapel („Assets zuordnen“).

Abgeglichen wird ausschließlich mit lokalen Daten: vorhandene Assets (Import, App-Journal, aktive und offene
Kursquellen-Zuordnungen), gespeicherte Symbol-Zuordnungen früherer Importe, von der Wallet-Anbindung gemeldete
Token-Namen und der zwischengespeicherte CoinGecko-Katalog (``/coins/list`` mit Contract-Adressen je Chain – ein
allgemeiner Abruf höchstens alle 7 Tage, ohne Bezug zum Portfolio). An CoinGecko gehen dabei weder Symbole noch
Contracts, Mengen oder Konten.

Regeln (die erste zutreffende gewinnt). Vorschläge belegen das Formular nur vor; gespeichert wird erst mit
„Zuordnungen speichern“:

Tokens ``SYMBOL@CHAIN:Contract`` – bestimmt über Chain und Contract, nie über das Symbol allein (gefälschte Tokens
tragen gern bekannte Symbole):

1. Gleicher Contract bereits zugeordnet bzw. ignoriert (anderes Symbol, z. B. nach einer Umbenennung) → übernehmen.
2. Contract im Katalog → Coin. Vorhandenes Asset mit dieser CoinGecko-ID → zuordnen. Offener Kursquellen-Vorschlag
   mit dieser ID → zuordnen und Kursquelle bestätigen. Im selben Formular schon als neues Asset vorgesehen →
   dorthin zuordnen. Einziges vorhandenes Krypto-Asset mit gleichem Symbol ohne Kursquelle → zuordnen und die
   Kursquelle übernehmen („mittel“: dass das Asset denselben Token meint, belegt nur das Symbol). Sonst neu anlegen
   mit Name und CoinGecko-ID aus dem Katalog; nutzt ein Asset gleichen Symbols eine andere CoinGecko-ID, erhält das
   neue Asset eine eigene ID (``SYMBOL#2``) und einen Warnhinweis.
3. Contract nicht im Katalog: als möglicher Spam erkannt → ignorieren; nur erhalten, nie bewegt → ignorieren
   („mittel“, typisch für unaufgefordert zugesandte Werbe-Token); sonst offen lassen mit Hinweis.

Symbole ohne Contract (Börsen- und Steuertool-Dateien):

4. Mehrdeutig (mehrere Assets mit dem Symbol) → das einzige ohne Spam-Markierung bzw. mit Kursquelle.
5. Gleiches Symbol bei einem anderen Import bereits zugeordnet → zuordnen („mittel“).
6. Bekannter Coin bzw. einziger Coin mit dem Symbol im Katalog → neu anlegen mit CoinGecko-ID; bei mehreren Coins
   eine Auswahlliste. Ohne Auswahl entscheidet nach dem Anlegen die automatische Kursquellen-Suche anhand von
   Marktdaten (Datenqualität → Kursquellen).
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.csvimport import model as M
from app.importer import contract as C
from app.ledger.models import AssetInfo
from app.prices.sources import Catalog, chain_hints, is_spam, split_token

CONF_RANK = {"hoch": 3, "mittel": 2, "niedrig": 1}
CHAIN_LABEL = {"ETH": "Ethereum", "BSC": "BNB Chain", "AVAX": "Avalanche", "SOL": "Solana", "KAS": "Kaspa"}
MAX_OPTIONS = 8
_ID_CLEAN = re.compile(r"[^A-Za-z0-9._-]")


@dataclass
class Suggestion:
    action: str = ""  # "" (später) | new | map | ignore
    asset_id: str = ""  # map: Ziel-Asset
    new_id: str = ""  # new: ID des neuen Assets
    name: str = ""  # new: Name
    qid: str = ""  # new: CoinGecko-ID
    source_id: str = ""  # map: CoinGecko-ID als Kursquelle für das Ziel-Asset (bisher ohne Kursquelle)
    confidence: str = ""  # hoch | mittel | niedrig
    reason: str = ""
    warning: str = ""
    verified: bool = False  # Token: Contract im Katalog gefunden bzw. bereits zugeordnet
    options: list[tuple[str, str]] = field(default_factory=list)  # CoinGecko-Auswahl: (ID, Bezeichnung)

    @property
    def prefill(self) -> bool:
        """Aktion vorbelegen – nur bei „hoch“ und „mittel“; „niedrig“ füllt Felder und zeigt den Hinweis."""
        return bool(self.action) and CONF_RANK.get(self.confidence, 0) >= CONF_RANK["mittel"]


def _aliases(a: AssetInfo) -> set[str]:
    return {x.strip().upper() for x in a.aliases if x.strip()}


class Basis:
    """Vorhandene Datenbasis für den Abgleich (je Seitenaufruf einmal aufgebaut)."""

    def __init__(self, known: Mapping[str, AssetInfo], saved: Mapping[str, str | None],
                 source_rows: Mapping[str, Any]) -> None:
        self.known = known
        self.taken = {aid.upper() for aid in known}
        self.by_cg: dict[str, list[AssetInfo]] = defaultdict(list)
        self.crypto_by_symbol: dict[str, list[AssetInfo]] = defaultdict(list)
        for a in known.values():
            if not a.is_crypto:
                continue
            if a.quote_source == "coingecko" and a.quote_id:
                self.by_cg[a.quote_id].append(a)
            for s in {a.symbol.upper(), *_aliases(a)}:
                self.crypto_by_symbol[s].append(a)
        self.open_source: dict[str, list[str]] = defaultdict(list)  # CoinGecko-ID → Assets mit offenem Vorschlag
        for aid, r in source_rows.items():
            if r["status"] == "suggested" and r["quote_id"] and aid in known:
                self.open_source[r["quote_id"]].append(aid)
        self.saved_contract: dict[tuple[str, str], set[str | None]] = defaultdict(set)
        self.saved_base: dict[str, set[str]] = defaultdict(set)
        for key, aid in saved.items():
            tok = split_token(key)
            if tok is not None and (aid is None or aid in known):  # None = ignoriert
                self.saved_contract[(tok[1], tok[2].upper())].add(aid)
            if aid and aid in known:
                self.saved_base[key.split("@", 1)[0].split(";", 1)[0].strip().upper()].add(aid)

    def symbol_matches(self, base: str) -> list[AssetInfo]:
        """Assets aller Klassen mit diesem Symbol oder Alias (wie der Symbol-Abgleich des Imports)."""
        return sorted((a for a in self.known.values() if base == a.symbol.upper() or base in _aliases(a)),
                      key=lambda a: a.asset_id.lower())

    def free_id(self, symbol: str) -> str:
        """Freie Asset-ID aus dem Symbol; belegt → ``SYMBOL#2`` usw. (Symbolanzeige bleibt ``SYMBOL``)."""
        base = _ID_CLEAN.sub("", symbol).strip("._-")[:30] or "TOKEN"
        cand, n = base, 2
        while cand.upper() in self.taken or cand.upper() in C.ISO_CURRENCIES:
            cand, n = f"{base}#{n}", n + 1
        self.taken.add(cand.upper())
        return cand


def _label(a: AssetInfo) -> str:
    return a.asset_id if a.name in ("", a.asset_id) else f"{a.asset_id} ({a.name})"


def _coin_label(c: Mapping[str, Any]) -> str:
    return f"{c.get('name') or c['id']} ({str(c.get('symbol') or '').upper()})"


def _options(coins: Iterable[Mapping[str, Any]], symbol: str, hints: set[str]) -> list[tuple[str, str]]:
    def rank(c: Mapping[str, Any]) -> tuple[int, int, int, str]:
        plats = set(Catalog.platforms(dict(c)))
        return (0 if plats & hints else 1, 0 if c["id"] == symbol.lower() else 1, len(str(c.get("name") or "")),
                c["id"])

    return [(c["id"], _coin_label(c)) for c in sorted(coins, key=rank)[:MAX_OPTIONS]]


def _pick(assets: list[AssetInfo], symbol: str) -> AssetInfo:
    """Bestes Asset unter mehreren mit derselben Kursquelle: kein Spam, Symbol passt, kürzeste ID."""
    return sorted(assets, key=lambda a: (is_spam(a), a.symbol.upper() != symbol, len(a.asset_id), a.asset_id))[0]


def _only_received(u: Mapping[str, Any]) -> bool:
    return set(u.get("dirs") or ()) == {"in"} and set(u.get("kinds") or ()) <= {M.DEPOSIT}


class Suggester:
    def __init__(self, basis: Basis, catalog: Catalog | None, chain_names: Mapping[str, str] | None = None,
                 accounts: Iterable[str] = ()) -> None:
        self.basis = basis
        self.catalog = catalog
        self.names = {k.upper(): v for k, v in (chain_names or {}).items() if v}
        self.hints = chain_hints(sorted({a for a in accounts if a}))
        self.planned: dict[str, str] = {}  # CoinGecko-ID → im Formular neu anzulegende Asset-ID

    def run(self, unknown: Iterable[Mapping[str, Any]]) -> dict[str, Suggestion]:
        out: dict[str, Suggestion] = {}
        for u in unknown:
            tok = split_token(str(u.get("display") or u["symbol"]))
            if tok is not None:
                out[u["symbol"]] = self.token(u, *tok)
            elif u.get("hint") in ("security", "fiat"):
                out[u["symbol"]] = Suggestion()
            else:
                out[u["symbol"]] = self.plain(u)
        return out

    # -- Tokens ---------------------------------------------------------------------------------------
    def token(self, u: Mapping[str, Any], sym: str, chain: str, contract: str) -> Suggestion:
        b = self.basis
        prev = b.saved_contract.get((chain, contract.upper()), set())
        targets = sorted(a for a in prev if a)
        if len(targets) == 1 and None not in prev:
            a = b.known[targets[0]]
            return Suggestion("map", asset_id=a.asset_id, confidence="hoch", verified=True,
                              reason=f"Gleicher Contract ist bereits {_label(a)} zugeordnet (anderes Symbol).")
        if prev == {None}:
            return Suggestion("ignore", confidence="hoch", verified=True,
                              reason="Gleicher Contract wurde bereits ignoriert (anderes Symbol).")
        chain_name = self.names.get(str(u.get("display") or u["symbol"]).upper(), "")
        if self.catalog is None:
            if u.get("spam"):
                return Suggestion("ignore", confidence="mittel",
                                  reason="Möglicher Spam laut Wallet-Anbindung; CoinGecko-Katalog nicht geladen.")
            return Suggestion(name=chain_name, confidence="",
                              reason="CoinGecko-Katalog nicht geladen – Contract noch nicht prüfbar.")
        coins = self.catalog.for_token(chain, contract)
        where = CHAIN_LABEL.get(chain, chain)
        if len(coins) > 1:
            return Suggestion(confidence="niedrig",
                              options=_options(coins, sym, self.hints),
                              reason=f"Contract laut Katalog mehreren Coins zugeordnet "
                                     f"({', '.join(c['id'] for c in coins[:4])}) – Kurs-ID wählen.")
        if coins:
            s = self.listed(coins[0], sym, where)
            s.verified = True
            if u.get("spam"):
                s.confidence = "mittel" if s.confidence == "hoch" else s.confidence
                s.warning = " ".join(x for x in (s.warning, "Name/Symbol wirkt wie Spam – Contract prüfen.") if x)
            return s
        if u.get("spam"):
            return Suggestion("ignore", confidence="hoch",
                              reason=f"Nicht bei CoinGecko gelistet ({where}) und als möglicher Spam erkannt.")
        if _only_received(u):
            return Suggestion("ignore", name=chain_name, confidence="mittel",
                              reason=f"Nicht bei CoinGecko gelistet ({where}) und nur erhalten, nie bewegt – typisch "
                                     "für unaufgefordert zugesandte Werbe-Token. Echter Token: „neu anlegen“ "
                                     "(Kurs dann manuell).")
        return Suggestion(name=chain_name,
                          reason=f"Nicht bei CoinGecko gelistet ({where}) – keine automatische Kursquelle. Contract "
                                 "prüfen, dann neu anlegen (Kurs manuell) oder ignorieren.")

    def listed(self, coin: Mapping[str, Any], sym: str, where: str) -> Suggestion:
        b = self.basis
        cid = str(coin["id"])
        cname = str(coin.get("name") or cid)[:80]
        csym = str(coin.get("symbol") or sym).upper()
        what = f"Contract ({where}) laut CoinGecko-Katalog: {cname} ({cid})"
        users = b.by_cg.get(cid, [])
        if users:
            a = _pick(users, csym)
            return Suggestion("map", asset_id=a.asset_id, confidence="hoch",
                              reason=f"{what}; vorhandenes Asset {_label(a)} nutzt diese Kursquelle.")
        pending = [b.known[x] for x in b.open_source.get(cid, []) if x in b.known]
        if len(pending) == 1:
            a = pending[0]
            return Suggestion("map", asset_id=a.asset_id, source_id=cid, confidence="hoch", name=cname, qid=cid,
                              new_id=b.free_id(csym),
                              reason=f"{what}; bestätigt den offenen Kursquellen-Vorschlag für {_label(a)}.")
        if cid in self.planned:
            return Suggestion("map", asset_id=self.planned[cid], confidence="hoch",
                              reason=f"{what}; wird weiter oben als neues Asset {self.planned[cid]} angelegt.")
        same = [a for a in b.crypto_by_symbol.get(csym, []) if not is_spam(a)]
        free = [a for a in same if not (a.quote_source == "coingecko" and a.quote_id)]
        conflict = [a for a in same if a.quote_source == "coingecko" and a.quote_id and a.quote_id != cid]
        if len(free) == 1 and not conflict:
            a = free[0]
            # Felder für „neu anlegen“ vorbereitet, falls das vorhandene Asset doch ein anderer Token ist
            return Suggestion("map", asset_id=a.asset_id, source_id=cid, confidence="mittel", name=cname, qid=cid,
                              new_id=b.free_id(csym),
                              reason=f"{what}; vorhandenes Asset {_label(a)} mit gleichem Symbol hat noch keine "
                                     "Kursquelle – prüfen, ob derselbe Token gemeint ist.")
        new_id = b.free_id(csym)
        self.planned[cid] = new_id
        warning = ""
        if conflict:
            a = conflict[0]
            warning = (f"Vorhandenes Asset {_label(a)} mit Symbol {csym} nutzt eine andere Kursquelle "
                       f"({a.quote_id}) – anderer Coin, daher eigene ID.")
        elif len(free) > 1:
            warning = (f"Mehrere vorhandene Assets mit Symbol {csym} ohne Kursquelle "
                       f"({', '.join(a.asset_id for a in free[:4])}) – gegebenenfalls stattdessen zuordnen.")
        return Suggestion("new", new_id=new_id, name=cname, qid=cid, confidence="hoch",
                          reason=f"{what}.", warning=warning)

    # -- Symbole ohne Contract ------------------------------------------------------------------------
    def plain(self, u: Mapping[str, Any]) -> Suggestion:
        b = self.basis
        base = str(u["symbol"]).split("@", 1)[0].split(";", 1)[0].strip().upper()
        if u.get("ambiguous"):
            cands = b.symbol_matches(base)
            good = [a for a in cands if not is_spam(a)]
            priced = [a for a in good if a.quote_source in ("coingecko", "yahoo") and a.quote_id]
            names = ", ".join(_label(a) for a in cands[:4])
            for pool, why in ((priced, "einziges mit Kursquelle"), (good, "einziges ohne Spam-Markierung")):
                if len(pool) == 1:
                    return Suggestion("map", asset_id=pool[0].asset_id, confidence="mittel",
                                      reason=f"{len(cands)} Assets mit Symbol {base} ({names}) – "
                                             f"{pool[0].asset_id} ist das {why}.")
            return Suggestion("map", confidence="niedrig",
                              reason=f"{len(cands)} Assets mit Symbol {base}: {names} – bitte wählen.")
        prev = b.saved_base.get(base, set())
        if len(prev) == 1:
            a = b.known[next(iter(prev))]
            return Suggestion("map", asset_id=a.asset_id, confidence="mittel",
                              reason=f"Symbol {base} ist bei einem anderen Import {_label(a)} zugeordnet.")
        kc = M.KNOWN_COINS.get(base)
        if kc is not None:
            return self.coin({"id": kc[1], "name": kc[0], "symbol": base}, base, "hoch",
                             f"Bekannter Coin: {kc[0]} ({kc[1]}).")
        if self.catalog is None:
            return Suggestion("new", new_id=b.free_id(base), name=base, confidence="niedrig",
                              reason="CoinGecko-Katalog nicht geladen – ohne Kurs-ID anlegen; die "
                                     "Kursquellen-Suche ergänzt sie nach der Übernahme.")
        coins = self.catalog.candidates(base)
        used = [c for c in coins if c["id"] in b.by_cg]
        if len(used) == 1:
            a = _pick(b.by_cg[used[0]["id"]], base)
            return Suggestion("map", asset_id=a.asset_id, confidence="mittel",
                              reason=f"Einziger Coin mit Symbol {base}, den ein vorhandenes Asset nutzt: "
                                     f"{_label(a)} ({used[0]['id']}).")
        if len(coins) == 1:
            return self.coin(coins[0], base, "mittel",
                             f"Einziger Coin mit Symbol {base} im CoinGecko-Katalog: {_coin_label(coins[0])}.")
        if coins:
            return Suggestion("new", new_id=b.free_id(base), name=base, confidence="niedrig",
                              options=_options(coins, base, self.hints),
                              reason=f"{len(coins)} Coins mit Symbol {base} im CoinGecko-Katalog – Kurs-ID aus der "
                                     "Liste wählen oder leer lassen: die Kursquellen-Suche entscheidet nach der "
                                     "Übernahme anhand von Marktdaten.")
        return Suggestion("new", new_id=b.free_id(base), name=base, confidence="niedrig",
                          reason=f"Kein Coin mit Symbol {base} im CoinGecko-Katalog – Kurs manuell.")

    def coin(self, coin: Mapping[str, Any], base: str, conf: str, reason: str) -> Suggestion:
        """Neues Asset für einen Coin – oder Zuordnung, wenn ein Asset bzw. eine frühere Zeile ihn schon nutzt."""
        cid = str(coin["id"])
        users = self.basis.by_cg.get(cid, [])
        if users:
            a = _pick(users, base)
            return Suggestion("map", asset_id=a.asset_id, confidence=conf,
                              reason=f"{reason} Vorhandenes Asset {_label(a)} nutzt diese Kursquelle.")
        if cid in self.planned:
            return Suggestion("map", asset_id=self.planned[cid], confidence=conf,
                              reason=f"{reason} Wird weiter oben als neues Asset {self.planned[cid]} angelegt.")
        new_id = self.basis.free_id(base)
        self.planned[cid] = new_id
        return Suggestion("new", new_id=new_id, name=str(coin.get("name") or base)[:80], qid=cid, confidence=conf,
                          reason=reason)


def batch_suggestions(ctx: Any, batch: Mapping[str, Any], ov: Mapping[str, Any],
                      known: Mapping[str, AssetInfo]) -> tuple[dict[str, Suggestion], dict[str, Any]]:
    """Vorschläge für die unbekannten Symbole eines Stapels und Zustand des Katalogs (lädt ihn bei Bedarf im
    Hintergrund)."""
    from app.csvimport.service import csv_service
    from app.prices.sources import catalog_state, source_service

    unknown = [*(ov.get("unknown") or []), *(ov.get("unknown_old") or [])]
    wants = any(u.get("hint") not in ("security", "fiat") for u in unknown)
    state = catalog_state(ctx, start=wants)
    names: dict[str, str] = {}
    if batch["datasource_id"]:
        names = {r["asset_key"]: r["name"] for r in ctx.db.q(
            "SELECT asset_key, name FROM ds_balance WHERE source_id=? AND name IS NOT NULL", (batch["datasource_id"],))}
    basis = Basis(known, csv_service(ctx).saved_symbols(), source_service(ctx).rows())
    out = Suggester(basis, state["catalog"], names, [batch["account"], *(ov.get("accounts") or {})]).run(unknown)
    relevant = {u["symbol"] for u in ov.get("unknown") or []}
    state["prefilled"] = sum(1 for k, s in out.items() if s.prefill and k in relevant)
    state["tokens"] = sum(1 for u in unknown if split_token(str(u.get("display") or u["symbol"])) is not None)
    return out, state
