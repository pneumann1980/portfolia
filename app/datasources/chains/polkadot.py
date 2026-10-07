"""Polkadot – DOT (Relay Chain und Asset Hub) über den Subscan-Indexer, nur lesend; API-Key erforderlich.

Zugang (laut Subscan-Dokumentation, Stand der Recherche)
    Subscan verlangt für jede Anfrage einen Schlüssel (anonymer Zugriff ist abgeschaltet). Kostenlos: Schlüssel des
    **PubFi-Gateways** (``https://api.pubfi.ai/v1/gateway/subscan/<netz>/api/…:free``, ``Authorization: Bearer``;
    2 Anfragen/s, 20.000/Tag). Alternativ ein kostenpflichtiger Subscan-Schlüssel (``https://<netz>.api.subscan.io``,
    Header ``X-API-Key``). Ohne Schlüssel zeigt Portfolia „Schlüssel fehlt“ – es wird nichts abgerufen.

Netze
    Seit der Asset-Hub-Migration (04.11.2025) liegen DOT-Bestände, Überweisungen und Staking auf Polkadot Asset Hub;
    ältere Vorgänge auf der Relay Chain. Portfolia fragt beide Netze ab (``polkadot``, ``assethub-polkadot``) und
    führt je Netz einen eigenen Fortschritt. Ereignisse der Migration selbst (Module „…migrator“) werden nie als
    Ein-/Auszahlung gebucht, sondern als ungeklärt angezeigt.

Abruf (dokumentierte Routen, POST mit JSON)
    ``api/scan/metadata`` (Blockhöhe), ``api/v2/scan/transfers`` (Überweisungen, ohne NFTs), ``api/v2/scan/extrinsics``
    (vom Konto signierte Extrinsics – einzige Quelle der Gebühr: ``fee_used`` bzw. ``fee``),
    ``api/v2/scan/account/reward_slash`` (Staking-Rewards/-Slashes), ``api/v2/scan/account/tokens`` (Bestände mit
    ``decimals``, gebunden/gesperrt/reserviert). Seiten zu 100 Einträgen aufsteigend innerhalb eines Blockbereichs.

Einheiten (Annahme, wird je Vorgang geprüft)
    Das Schema nennt die Einheiten nicht. ``amount`` wird als DOT-Betrag gelesen und gegen ``amount_v2`` (Planck,
    10 Dezimalstellen) geprüft – weichen beide ab, geht der Vorgang mit Begründung in die Prüfung. Ganzzahlige
    Gebühren-, Reward- und Bestandsfelder gelten als Planck, Werte mit Dezimalpunkt als DOT. Die Bestandsprüfung
    (beobachtet vs. gebucht) macht eine falsche Annahme sichtbar.

Einordnung
    Je Extrinsic ein Ereignis ``polkadot:<rc|ah>-<extrinsic>:<adresse>``: Überweisungen (``balances``) als Zu-/Abgang,
    Gebühr nur bei eigener Signatur; Staking (bond/unbond/nominate …) bewegt keinen Bestand aus dem Konto → nur
    Gebühr; Nomination Pools, XCM, Proxy/Multisig und Unbekanntes zur Prüfung. Rewards als Staking-Ertrag, Slashes
    zur Prüfung.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.csvimport import model as M
from app.csvimport.model import Rec
from app.datasources import connector as K
from app.datasources.chainhttp import ChainHttp, Stop
from app.datasources.chains.codec import ss58_decode
from app.datasources.wallet import (
    Move,
    TxView,
    WalletConnector,
    classify,
    clean_symbol,
    short,
    sub_hash,
    token_key,
    units,
)

DOT_DECIMALS = 10
ROW = 100
MAX_PAGES = 100  # 10.000 Einträge je Liste und Lauf – danach Fortsetzung im nächsten Lauf
NETS = (("polkadot", "rc", "Relay Chain"), ("assethub-polkadot", "ah", "Asset Hub"))
ASSET_HUB_TAG = "DOTAH"
_IDX = re.compile(r"^\d{1,12}-\d{1,6}$")
_INT = re.compile(r"^-?\d{1,40}$")
_PLAIN_CALLS = {"balances"}
_STAKING = {"staking", "nominationpools", "fastunstake", "delegatedstaking"}


def amount(raw: Any, decimals: int = DOT_DECIMALS) -> Decimal:
    """Betrag eines Subscan-Feldes: ganzzahlig = kleinste Einheit (Planck), mit Dezimalpunkt = DOT."""
    s = str(raw if raw is not None else "").strip()
    if not s:
        return Decimal(0)
    if _INT.match(s):
        return units(s, decimals)
    try:
        return Decimal(s)
    except InvalidOperation:
        raise ValueError("Betrag nicht lesbar") from None


class _Net:
    """Abrufstand eines Netzes (Relay Chain bzw. Asset Hub)."""

    def __init__(self, key: str, short_: str, label: str, start: int) -> None:
        self.key, self.short, self.label, self.start = key, short_, label, start
        self.through = start - 1
        self.safe = -1
        self.transfers: list[dict[str, Any]] = []
        self.extrinsics: list[dict[str, Any]] = []
        self.rewards: list[dict[str, Any]] = []
        self.capped: list[str] = []
        self.stopped: str | None = None
        self.requests = 0


@K.register
class PolkadotConnector(WalletConnector):
    provider = "polkadot"
    label = "Polkadot (Subscan via PubFi bzw. direkt)"
    chain_label = "Polkadot"
    native = "DOT"
    endpoints = ("pubfi", "subscan")
    explorer_tx = "https://assethub-polkadot.subscan.io/extrinsic/{}"
    explorer_addr = "https://assethub-polkadot.subscan.io/account/{}"
    limits = ("Relay Chain und Asset Hub getrennt abgefragt; Ereignisse der Asset-Hub-Migration werden nicht gebucht, "
              "sondern als ungeklärt angezeigt",
              "Einheiten der Subscan-Felder sind nicht dokumentiert: Beträge werden gegen amount_v2 geprüft, "
              "Gebühren/Rewards/Bestände als Planck gelesen – Abweichungen zeigt die Bestandsprüfung",
              "Nomination Pools, XCM, Proxy/Multisig und Vesting zur Prüfung; NFTs werden nicht gebucht; "
              "Parachain-Konten (außer Asset Hub) sind nicht enthalten")

    # -- Anbieter-API -------------------------------------------------------------------------------------
    @staticmethod
    def _path(http: ChainHttp, route: str) -> str:
        return f"/api/{route}" + (":free" if http.ep.id == "pubfi" else "")

    def _call(self, http: ChainHttp, route: str, body: dict[str, Any], what: str) -> Any:
        out = http.post(self._path(http, route), body, what=what)
        if not isinstance(out, dict):
            raise K.ConnectorError("data", f"Unerwartete Antwort von {http.ep.label} ({what}).")
        code = out.get("code")
        if code in (0, "0"):
            return out.get("data")
        msg = str(out.get("message") or "")[:160]
        low = msg.lower()
        if "not found" in low:
            return None
        if "rate limit" in low or "too many" in low:
            raise K.ConnectorError("rate_limit", f"{http.ep.label} drosselt Anfragen ({what}).", retry_after_s=120)
        if "api key" in low or "unauthorized" in low or "forbidden" in low:
            raise K.ConnectorError("auth", f"{http.ep.label} lehnt den Schlüssel ab ({what}) – unter "
                                           "„Anbieter-Schlüssel“ prüfen.")
        raise K.ConnectorError("data", f"{http.ep.label} meldet bei {what}: {msg or 'Fehler'} (Code {code}).")

    def _addr(self, cfg: K.SourceConfig) -> str:
        a = (cfg.address or "").strip()
        try:
            prefix, _ = ss58_decode(a)
        except ValueError:
            raise K.ConnectorError("config", "Polkadot-Adresse ungültig – bitte neu eingeben.") from None
        if prefix != 0:
            raise K.ConnectorError("config", "Keine Polkadot-Adresse (SS58-Präfix 0).")
        return a

    def _tip(self, http: ChainHttp) -> int:
        data = self._call(http, "scan/metadata", {}, "Blockhöhe")
        if not isinstance(data, dict):
            raise K.ConnectorError("data", f"{http.ep.label}: Blockhöhe nicht lesbar.")
        for k, lag in (("finalized_blockNum", 0), ("blockNum", 20)):
            try:
                return int(str(data[k])) - lag
            except (KeyError, TypeError, ValueError):
                continue
        raise K.ConnectorError("data", f"{http.ep.label}: Blockhöhe nicht lesbar.")

    def _nets(self, cfg: K.SourceConfig, secret: K.Secret, budget: int | None = None,
              deadline_s: float | None = None) -> list[tuple[str, str, str, ChainHttp]]:
        n = budget or self.max_requests
        return [(key, sh, label, self.http(cfg, secret, network=key, max_requests=max(n // len(NETS), 10),
                                           deadline_s=deadline_s)) for key, sh, label in NETS]

    # -- Prüfen -------------------------------------------------------------------------------------------
    def check(self, cfg: K.SourceConfig, secret: K.Secret) -> K.CheckResult:
        addr = self._addr(cfg)
        details: dict[str, Any] = {}
        nets = self._nets(cfg, secret, 24, 60)
        balances: list[K.Balance] = []
        try:
            for key, _sh, label, http in nets:
                tip = self._tip(http)
                details[f"chain_{key}"] = {"ok": True, "text": f"{label} erreichbar, Block {tip:,}".replace(",", ".")}
                last = self._call(http, "v2/scan/transfers", {"address": addr, "row": 1, "page": 0},
                                  "Überweisungen") or {}
                n = int(last.get("count") or 0) if isinstance(last, dict) else 0
                details[f"history_{key}"] = {"ok": True, "text": f"{label}: {n} Überweisung(en) laut Indexer"}
            balances = self._balances(nets, addr)
        finally:
            for *_rest, http in nets:
                http.close()
        dot = balances[0] if balances else None
        if dot is not None:
            details["balance"] = {"ok": True, "text": f"Bestand {dot.qty.normalize():f} DOT"
                                                      + (f" ({dot.note})" if dot.note else "")}
        return K.CheckResult(True, f"Polkadot: Adresse {short(addr)} über {nets[0][3].ep.label} lesbar.", details,
                             balances=balances)

    def _balances(self, nets: list[tuple[str, str, str, ChainHttp]], addr: str) -> list[K.Balance]:
        total = Decimal(0)
        parts: list[str] = []
        locked: dict[str, Decimal] = defaultdict(Decimal)
        tokens: dict[str, list[Any]] = {}
        for _key, _sh, label, http in nets:
            data = self._call(http, "v2/scan/account/tokens", {"address": addr, "row": ROW, "page": 0},
                              "Bestand") or {}
            rows = data.get("list") if isinstance(data, dict) else None
            for r in rows or []:
                if not isinstance(r, dict):
                    continue
                sym = str(r.get("symbol") or "")
                try:
                    dec = int(r.get("decimals") if r.get("decimals") is not None else DOT_DECIMALS)
                    bal = amount(r.get("balance"), dec)
                except (TypeError, ValueError):
                    continue
                uid = str(r.get("unique_id") or sym)
                if sym == "DOT" and uid in ("DOT", "", "native"):
                    total += bal
                    if bal:
                        parts.append(f"{label} {bal.normalize():f}")
                    for f, lbl in (("bonded", "gebunden"), ("unbonding", "in Entbindung"), ("lock", "gesperrt"),
                                   ("reserved", "reserviert")):
                        try:
                            v = amount(r.get(f), dec)
                        except ValueError:
                            continue
                        if v:
                            locked[lbl] += v
                elif bal:
                    cur = tokens.setdefault(uid, [Decimal(0), clean_symbol(sym, "TOKEN")])
                    cur[0] += bal
        note = "; ".join([" + ".join(parts)] if len(parts) > 1 else []
                         + [f"davon {lbl} {v.normalize():f}" for lbl, v in locked.items()]) or None
        out = [K.Balance("DOT", total, "Polkadot", note)]
        for uid, (qty, sym) in sorted(tokens.items()):
            out.append(K.Balance(token_key(sym, ASSET_HUB_TAG, uid), qty, sym))
        return out

    # -- Abrufen ------------------------------------------------------------------------------------------
    def _list(self, http: ChainHttp, route: str, field: str, addr: str, net: _Net, what: str,
              extra: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], int]:
        """Alle Einträge im Blockbereich (aufsteigend); (Einträge, vollständig bis Block)."""
        out: list[dict[str, Any]] = []
        end = net.safe
        for page in range(MAX_PAGES):
            body = {"address": addr, "row": ROW, "page": page, "order": "asc",
                    "block_range": f"{net.start}-{net.safe}", **(extra or {})}
            self.report("Abruf", len(out), None, f"{net.label}: {what} ab Block {net.start:,}".replace(",", "."))
            data = self._call(http, route, body, what) or {}
            rows = [r for r in (data.get(field) if isinstance(data, dict) else None) or [] if isinstance(r, dict)]
            out += rows
            if len(rows) < ROW:
                return out, end
        blocks = [int(r.get("block_num") or 0) for r in out]
        net.capped.append(what)
        end = max(blocks) - 1 if blocks else net.start - 1
        return [r for r, b in zip(out, blocks, strict=True) if b <= end], end

    def fetch(self, cfg: K.SourceConfig, secret: K.Secret, cursor: dict[str, Any] | None) -> K.FetchResult:
        addr = self._addr(cfg)
        cur = dict(cursor or {})
        state = dict(cur.get("nets") or {})
        res = K.FetchResult(complete=True)
        skipped: Counter[str] = Counter()
        nets = self._nets(cfg, secret)
        done: list[_Net] = []
        try:
            for key, sh, label, http in nets:
                net = _Net(key, sh, label, int((state.get(key) or {}).get("block") or 0))
                done.append(net)
                try:
                    net.safe = self._tip(http)
                    if net.start > net.safe:
                        net.through = net.start - 1
                        continue
                    net.transfers, t_end = self._list(http, "v2/scan/transfers", "transfers", addr, net,
                                                      "Überweisungen", {"filter_nft": True})
                    net.extrinsics, x_end = self._list(http, "v2/scan/extrinsics", "extrinsics", addr, net,
                                                       "Extrinsics")
                    net.rewards, r_end = self._list(http, "v2/scan/account/reward_slash", "list", addr, net,
                                                    "Rewards/Slashes")
                    net.through = min(t_end, x_end, r_end)
                except Stop as e:
                    net.stopped = str(e)
                net.requests = http.requests
            events: list[K.SourceEvent] = []
            for net in done:
                events += self._events(addr, net, skipped)
            res.events = sorted(events, key=lambda e: e.ts)
            res.skipped = dict(skipped)
            stopped = [n for n in done if n.stopped]
            if any(n.stopped and n.through < n.start for n in done) and not events:
                raise K.ConnectorError("unavailable", f"{stopped[0].stopped} ohne Fortschritt – der nächste Lauf "
                                                      "versucht es erneut.", retry_after_s=600)
            res.complete = all(n.through >= n.safe and not n.stopped and not n.capped for n in done)
            res.cursor = {"v": 1, "nets": {n.key: {"block": max(n.through + 1, n.start)} for n in done}}
            res.resume = not res.complete
            for n in done:
                if n.stopped:
                    res.warnings.append(f"{n.label}: {n.stopped} – Fortsetzung ab Block {n.through + 1:,}"
                                        .replace(",", "."))
                for what in n.capped:
                    res.warnings.append(f"{n.label}: mehr als {ROW * MAX_PAGES} {what} – Fortsetzung im nächsten Lauf")
            try:
                res.balances = self._balances(nets, addr)
            except (Stop, K.ConnectorError):
                res.balances = None
            first = nets[0][3]
            res.coverage = {"mode": "historisch" if not state else "inkrementell", "operations": len(events),
                            "provider": first.ep.label,
                            "networks": {n.label: {"from_block": n.start, "to_block": n.through, "tip": n.safe,
                                                   "transfers": len(n.transfers), "extrinsics": len(n.extrinsics),
                                                   "rewards": len(n.rewards)} for n in done},
                            "requests": sum(h.requests for *_r, h in nets),
                            "throttled": sum(h.throttled for *_r, h in nets),
                            "waited_s": round(sum(h.waited + h.paced for *_r, h in nets), 1)}
        finally:
            for *_rest, http in nets:
                http.close()
        return res

    # -- Einordnung ---------------------------------------------------------------------------------------
    def _events(self, addr: str, net: _Net, skipped: Counter[str]) -> list[K.SourceEvent]:
        groups: dict[str, dict[str, Any]] = defaultdict(lambda: {"t": [], "x": None})
        for t in net.transfers:
            block = int(t.get("block_num") or 0)
            if block > net.through:
                continue
            idx = str(t.get("extrinsic_index") or "")
            if not _IDX.match(idx):
                ev = t.get("event_idx")
                idx = f"{block}-e{int(ev)}" if isinstance(ev, int) else f"{block}-t{sub_hash(t.get('transfer_id'))}"
            groups[idx]["t"].append(t)
        for x in net.extrinsics:
            if int(x.get("block_num") or 0) > net.through:
                continue
            idx = str(x.get("extrinsic_index") or "")
            if not _IDX.match(idx):
                skipped["Extrinsics ohne Kennung"] += 1
                continue
            groups[idx]["x"] = x
        out: list[K.SourceEvent] = []
        for idx, g in sorted(groups.items(), key=lambda kv: _order(kv[0])):
            tx = self._view(addr, net, idx, g["t"], g["x"], skipped)
            if tx is None:
                continue
            if tx.hint and tx.hint.startswith("Asset-Hub-Migration"):
                rec = Rec(line=0, ts=tx.ts, kind=M.REVIEW, txhash=tx.txid if tx.txid.startswith("0x") else None,
                          note=tx.hint, label=tx.label, raw={**tx.raw, "chain": self.provider})
                rec.ext_id = "migration"
                recs = [rec]
            else:
                recs = classify(tx)
            if recs:
                out.append(K.SourceEvent(f"{self.provider}:{net.short}-{idx}:{addr}", tx.ts, recs, tx.label))
        for r in net.rewards:
            if int(r.get("block_num") or r.get("block") or 0) > net.through:
                continue
            ev = self._reward(addr, net, r, skipped)
            if ev is not None:
                out.append(ev)
        return out

    def _view(self, addr: str, net: _Net, idx: str, transfers: list[dict[str, Any]], x: dict[str, Any] | None,
              skipped: Counter[str]) -> TxView | None:
        hints: list[str] = []
        moves: list[Move] = []
        modules: set[str] = set()
        ts_v = (x or {}).get("block_timestamp") or next((t.get("block_timestamp") for t in transfers), None)
        try:
            ts = datetime.fromtimestamp(int(ts_v), UTC)
        except (TypeError, ValueError, OverflowError):
            skipped["Vorgänge ohne Zeitstempel"] += 1
            return None
        raw_moves: list[dict[str, Any]] = []
        for t in transfers:
            if t.get("success") is False:
                continue
            mod = str(t.get("module") or "").lower()
            modules.add(mod)
            sym = str(t.get("asset_symbol") or "DOT")
            uid = str(t.get("asset_unique_id") or "")
            native = sym == "DOT" and uid in ("", "DOT", "native")
            try:
                qty = Decimal(str(t.get("amount") or "0"))
            except InvalidOperation:
                hints.append("Betrag nicht lesbar")
                continue
            v2 = str(t.get("amount_v2") or "")
            if native and _INT.match(v2) and units(v2, DOT_DECIMALS) != qty:
                hints.append(f"Betrag nicht eindeutig (amount {qty} ≠ amount_v2/10¹⁰) – Menge prüfen")
            if not qty:
                continue
            asset = "DOT" if native else token_key(clean_symbol(sym, "TOKEN"), ASSET_HUB_TAG, uid or sym)
            ev = t.get("event_idx")
            sub = f"tr:{int(ev)}" if isinstance(ev, int) else f"tr:{sub_hash(t.get('from'), t.get('to'), qty)}"
            frm, to = t.get("from"), t.get("to")
            if frm == addr and to == addr and "xcm" in mod:
                # XCM an das eigene Konto auf einer anderen Chain (z. B. Relay Chain → Asset Hub): auf der Chain mit
                # eigener Signatur ein Abgang, auf der Ziel-Chain (ohne Signatur) ein Zugang – nie zu null saldiert
                frm, to = (addr, None) if x is not None else (None, addr)
            if frm == addr:
                moves.append(Move(asset, -qty, sub + (":out" if to == addr else "")))
            if to == addr:
                moves.append(Move(asset, qty, sub + (":in" if frm == addr else "")))
            raw_moves.append({"from": frm, "to": to, "amount": str(t.get("amount")), "amount_v2": v2 or None,
                              "symbol": sym, "module": mod or None, "event_idx": ev})
        initiated = x is not None
        failed = bool(x is not None and x.get("success") is False)
        fee = Decimal(0)
        call = ""
        if x is not None:
            call = f"{x.get('call_module') or ''}.{x.get('call_module_function') or ''}".strip(".")
            modules.add(str(x.get("call_module") or "").lower())
            fee_raw = x.get("fee_used") if str(x.get("fee_used") or "0") not in ("", "0") else x.get("fee")
            try:
                fee = amount(fee_raw)
            except ValueError:
                hints.append("Gebühr nicht lesbar")
        txid = str((x or {}).get("extrinsic_hash") or next((t.get("hash") for t in transfers if t.get("hash")), "")
                   or f"{net.short}-{idx}")
        if any("migrat" in m for m in modules):
            return TxView(self.provider, txid, ts, [], hint="Asset-Hub-Migration (automatische Übertragung durch das "
                                                            "Netzwerk) – keine Ein-/Auszahlung; prüfen bzw. ignorieren",
                          label=call or "Migration", raw={"network": net.key, "extrinsic": idx, "moves": raw_moves})
        if any("xcm" in m for m in modules):
            hints.append("XCM-Übertragung (z. B. Relay Chain ↔ Asset Hub) – bei eigenem Konto eine Umbuchung, Art "
                         "prüfen")
        elif modules & _STAKING and moves:
            hints.append("Bewegung aus Staking/Nomination Pool – Art prüfen (gebundene DOT bleiben im Konto)")
        plain = not (modules - _PLAIN_CALLS - {""})
        if x is None and any(t.get("from") == addr for t in transfers):
            hints.append("Abgang ohne eigene Signatur (Proxy/Multisig?) – Art prüfen")
        raw = {"network": net.key, "extrinsic": idx, "call": call or None, "fee_raw": str((x or {}).get("fee_used")
                                                                                         or (x or {}).get("fee") or "")
               or None, "moves": raw_moves[:20]}
        return TxView(self.provider, txid, ts, moves, fee=fee, fee_asset="DOT", initiated=initiated, failed=failed,
                      plain=plain, hint="; ".join(dict.fromkeys(hints)) or None, label=call or None,
                      raw={k: v for k, v in raw.items() if v not in (None, [])})

    def _reward(self, addr: str, net: _Net, r: dict[str, Any], skipped: Counter[str]) -> K.SourceEvent | None:
        idx = str(r.get("event_index") or "")
        if not _IDX.match(idx):
            skipped["Rewards ohne Ereigniskennung"] += 1
            return None
        try:
            qty = amount(r.get("amount"))
            ts = datetime.fromtimestamp(int(r.get("block_timestamp")), UTC)
        except (TypeError, ValueError, OverflowError):
            skipped["Rewards mit unlesbaren Angaben"] += 1
            return None
        if not qty:
            return None
        slash = str(r.get("event_id") or "").lower() in ("slash", "slashed") or r.get("category") == "Slash"
        era = r.get("era")
        raw = {"chain": self.provider, "network": net.key, "event_index": idx, "event_id": r.get("event_id"),
               "era": era, "validator": r.get("validator_stash"), "amount_raw": str(r.get("amount"))}
        if slash:
            rec = Rec(line=0, ts=ts, kind=M.WITHDRAWAL, out_sym="DOT", out_qty=qty, label="Staking-Slash",
                      review="Slash (Strafe des Validators) – Abgang prüfen", raw=raw,
                      note=f"Slash Ära {era}" if era is not None else "Slash")
        else:
            rec = Rec(line=0, ts=ts, kind=M.DEPOSIT, in_sym="DOT", in_qty=qty, tag="staking", label="Staking-Reward",
                      note=f"Staking-Reward Ära {era}" if era is not None else "Staking-Reward", raw=raw)
        rec.txhash = None
        rec.ext_id = "slash" if slash else "reward"
        return K.SourceEvent(f"{self.provider}:reward-{net.short}-{idx}:{addr}", ts, [rec], rec.label)


def _order(idx: str) -> tuple[int, str]:
    head, _, tail = idx.partition("-")
    try:
        return int(head), tail.zfill(8)
    except ValueError:
        return 0, idx

