"""EVM-Chains (Ethereum, BNB Smart Chain, Polygon PoS, Avalanche C-Chain) – ein Adapter mit expliziter Chain-ID.

Anbieter (Etherscan-kompatible Konto-API, nur lesend)
    * **Etherscan API V2** (``api.etherscan.io/v2/api?chainid=…``) – API-Key nötig. Kostenloser Plan: Ethereum;
      BNB Chain und Avalanche verlangen laut Etherscan einen kostenpflichtigen Plan.
    * **Routescan** (``api.routescan.io/v2/network/mainnet/evm/<chain-id>/etherscan/api``) – ohne Key nutzbar
      (2 Anfragen/s, 10.000/Tag), optional mit kostenlosem Key. Standard für Avalanche (Snowtrace).
    * **Blockscout** (``polygon.blockscout.com/api``, Etherscan-kompatibel) – ohne Key, nur Polygon. Meldet
      Blockscout für einen Bereich noch nicht verarbeitete interne Transaktionen (``status`` 2), wird das als Lücke
      angezeigt („vollständig synchronisiert“ gilt dann nicht).
    Welche Chain ein Anbieter tatsächlich liefert, zeigt „Verbindung testen“ (Fehlermeldung des Anbieters).

Polygon PoS: MATIC → POL
    Der native Coin von Polygon PoS ist seit dem 04.09.2024 POL (1:1 aus MATIC, automatisch, ohne Transaktion des
    Nutzers); on-chain wurde der Ticker mit dem Hardfork „Ahmedabad“ (Block 62.278.656, 26.09.2024, PIP-45)
    umbenannt. Portfolia bucht den nativen Coin bis zu diesem Block als ``MATIC``, danach als ``POL`` und schlägt am
    Hardfork-Block eine Umstellung (``conversion``, nie automatisch) über den aus der Historie berechneten Bestand
    vor. Polygon meldet native Überweisungen zusätzlich als Token-Transfer des Systemvertrags ``0x…1010`` – diese
    Spiegelung wird übersprungen (sonst doppelt gezählt).

Abruf
    ``txlist`` (normale Transaktionen inkl. tatsächlich bezahlter Gebühr ``gasUsed × gasPrice``), ``txlistinternal``
    (interne Wertbewegungen laut Trace des Indexers), ``tokentx`` (ERC-20/BEP-20-Transfers). Seiten zu höchstens
    1.000 Einträgen, aufsteigend nach Block; eine Seite endet nie mitten in einem Block (der letzte Block einer vollen
    Seite wird erneut vollständig abgefragt). Alle drei Listen werden im Gleichschritt geführt – der
    Fortsetzungspunkt zeigt nur so weit, wie *alle* lückenlos abgerufen sind. Abgefragt werden nur Blöcke mit
    ausreichend Bestätigungen (Reorg-Schutz); jüngere folgen im nächsten Lauf.

Kennungen
    Ereignis ``<chain>:<tx-hash>:<adresse>``; Bewegungen: ``n:in``/``n:out`` (nativ), ``i:<trace>`` (intern),
    ``t:<fingerabdruck>#<k>`` (Token-Transfer; ``k`` zählt gleichartige Transfers im selben Hash), ``fee``.
    Tokens werden über Chain und Contract identifiziert (``USDC@ETH:0xa0b8…``), nie über das Symbol.

Grenzen (werden angezeigt)
    NFTs (ERC-721/1155) werden nicht gebucht – ob es welche gibt, prüft „Verbindung testen“. Positionen in Verträgen
    (Staking, Liquidität, Bridges) sind nicht sichtbar, nur die Bewegungen der Adresse. Interne Bewegungen stammen
    aus den Traces des Indexers.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar

from app.csvimport import model as M
from app.csvimport.model import Rec
from app.datasources import connector as K
from app.datasources.chainhttp import ChainHttp, Stop
from app.datasources.wallet import (
    Move,
    TxView,
    WalletConnector,
    classify,
    event,
    short,
    spam_reason,
    sub_hash,
    token_key,
    ts_from_unix,
    units,
)

PAGE = 1000
MAX_SAME_BLOCK_PAGES = 10
MAX_TOKEN_BALANCES = 40
_HASH = re.compile(r"^0x[0-9a-f]{64}$")
_ADDR = re.compile(r"^0x[0-9a-f]{40}$")
ACTIONS = ("txlist", "txlistinternal", "tokentx")
_ACTION_LABEL = {"txlist": "Transaktionen", "txlistinternal": "interne Bewegungen", "tokentx": "Token-Transfers"}
_TRANSFER_SELECTORS = {"0xa9059cbb"}  # transfer(address,uint256)


class EvmConnector(WalletConnector):
    """Gemeinsamer Adapter; je Chain eine Unterklasse mit Chain-ID, Symbol und Bestätigungen."""

    chain_tag: ClassVar[str] = ""
    confirmations: ClassVar[int] = 12
    # Token-Verträge, deren Transfers nur die native Bewegung spiegeln (Polygon: MRC20 0x…1010) – nie doppelt zählen
    mirror_contracts: ClassVar[frozenset[str]] = frozenset()
    # Umbenennung des nativen Coins: (erster Block mit neuem Symbol, altes Symbol, Zeitpunkt des Blocks, Hinweis)
    native_switch: ClassVar[tuple[int, str, datetime, str] | None] = None
    first_block: ClassVar[int] = 0  # erster Block dieser Chain (PulseChain: davor Ethereum-Historie)
    network: ClassVar[str | None] = None  # Netz-Platzhalter des Endpunkts (Subscan: peaq)
    limits = ("NFTs (ERC-721/1155) werden nicht gebucht – „Verbindung testen“ zeigt, ob welche vorhanden sind",
              "Positionen in Verträgen (Staking, Liquidität, Bridges) sind nicht sichtbar – nur Bewegungen der Adresse",
              "Interne Bewegungen laut Trace des Indexers; Gebühren aus gasUsed × gasPrice der Transaktion")

    # -- Anbieter-API -------------------------------------------------------------------------------------
    def _call(self, http: ChainHttp, params: dict[str, Any], what: str) -> Any:
        """Etherscan-kompatible Abfrage mit Auswertung der Hülle (status/message/result)."""
        q = dict(params)
        if http.ep.id == "etherscan":
            q["chainid"] = self.chain_id
        for _attempt in range(6):
            body = http.get("", q, what=what)
            if not isinstance(body, dict):
                raise K.ConnectorError("data", f"Unerwartete Antwort von {http.ep.label} ({what}).")
            status, msg, result = str(body.get("status", "")), str(body.get("message") or ""), body.get("result")
            if "jsonrpc" in body and "error" not in body:
                return result  # proxy-Modul (JSON-RPC-Hülle)
            if status == "1":
                return result
            if status == "2" and isinstance(result, list):  # Blockscout: Bereich noch nicht vollständig verarbeitet
                self._incomplete.add(what)
                return result
            text = result if isinstance(result, str) else msg
            low = f"{msg} {text}".lower()
            if isinstance(result, list) and ("no transactions" in low or "no records" in low or not result):
                return []
            if "rate limit" in low and "daily" not in low:
                http.throttled += 1
                if not http._pause(1.2 * (_attempt + 1)):
                    raise K.ConnectorError("rate_limit", f"{http.ep.label} drosselt Anfragen ({what}).",
                                           retry_after_s=60)
                continue
            if "daily" in low and "limit" in low:
                now = datetime.now(UTC)
                tomorrow = datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(days=1, minutes=5)
                raise K.ConnectorError("rate_limit", f"Tageskontingent von {http.ep.label} erschöpft.",
                                       retry_after_s=int((tomorrow - now).total_seconds()))
            if "invalid api key" in low or ("missing" in low and "key" in low):
                raise K.ConnectorError("auth", f"{http.ep.label} lehnt den hinterlegten Schlüssel ab – unter "
                                               "„Anbieter-Schlüssel“ prüfen.")
            if "not supported for this chain" in low or "upgrade your api plan" in low or "paid" in low:
                raise K.ConnectorError("scope", f"{http.ep.label} liefert {self.chain_label} mit diesem Zugang nicht "
                                                "(laut Anbieter nur mit kostenpflichtigem Plan) – anderen Anbieter "
                                                "wählen oder Plan prüfen.")
            if "timeout" in low or "busy" in low or "temporarily" in low:
                raise K.ConnectorError("unavailable", f"{http.ep.label} ist überlastet ({what}) – der nächste Lauf "
                                                      "versucht es erneut.")
            raise K.ConnectorError("data", f"{http.ep.label} meldet bei {what}: {text[:120]}")
        raise K.ConnectorError("rate_limit", f"{http.ep.label} drosselt Anfragen ({what}).", retry_after_s=60)

    def _tip(self, http: ChainHttp) -> int:
        # Etherscan/Routescan: proxy-Modul; Blockscout: block/eth_block_number (beide JSON-RPC-Hülle)
        calls = ({"module": "block", "action": "eth_block_number"}, {"module": "proxy", "action": "eth_blockNumber"}) \
            if http.ep.id.startswith("blockscout") else ({"module": "proxy", "action": "eth_blockNumber"},)
        for q in calls:
            try:
                res = self._call(http, q, "Blockhöhe")
                if isinstance(res, str) and res.startswith("0x"):
                    return int(res, 16)
            except K.ConnectorError as e:
                if e.kind in ("auth", "scope", "rate_limit"):
                    raise
        res = self._call(http, {"module": "block", "action": "getblocknobytime",
                                "timestamp": int(datetime.now(UTC).timestamp()), "closest": "before"}, "Blockhöhe")
        if isinstance(res, dict):  # Blockscout: {"blockNumber": "…"}
            res = res.get("blockNumber")
        try:
            return int(str(res))
        except ValueError:
            raise K.ConnectorError("data", f"{http.ep.label}: Blockhöhe nicht lesbar.") from None

    def native_at(self, block: int) -> str:
        """Symbol des nativen Coins in einem Block (Polygon: MATIC vor, POL ab dem Hardfork-Block)."""
        sw = self.native_switch
        return sw[1] if sw is not None and 0 <= block < sw[0] else self.native

    @property
    def _incomplete(self) -> set[str]:
        """In diesem Lauf als unvollständig gemeldete Abfragen (Blockscout ``status`` 2) – je Instanz."""
        v = self.__dict__.get("_incomplete_set")
        if v is None:
            v = self.__dict__["_incomplete_set"] = set()
        return v

    def _page(self, http: ChainHttp, action: str, addr: str, start: int, end: int, page: int = 1) -> list[dict]:
        res = self._call(http, {"module": "account", "action": action, "address": addr, "startblock": start,
                                "endblock": end, "page": page, "offset": PAGE, "sort": "asc"},
                         _ACTION_LABEL[action])
        if not isinstance(res, list):
            raise K.ConnectorError("data", f"{http.ep.label}: {_ACTION_LABEL[action]} nicht lesbar.")
        return [r for r in res if isinstance(r, dict)]

    # -- Prüfen -------------------------------------------------------------------------------------------
    def check(self, cfg: K.SourceConfig, secret: K.Secret) -> K.CheckResult:
        addr = self._addr(cfg)
        details: dict[str, Any] = {}
        with self.http(cfg, secret, max_requests=12, deadline_s=60) as http:
            tip = self._tip(http)
            details["chain"] = {"ok": True, "text": f"{self.chain_label} erreichbar, Block {tip:,}".replace(",", ".")}
            bal = self._native_balance(http, addr)
            details["balance"] = {"ok": True, "text": f"Bestand {bal.normalize():f} {self.native}"}
            if self.native_switch is not None:
                details["native"] = {"ok": True, "text": self.native_switch[3]}
            last = self._call(http, {"module": "account", "action": "txlist", "address": addr,
                                     "startblock": self.first_block, "endblock": tip, "page": 1, "offset": 1,
                                     "sort": "desc"}, "Transaktionen")
            if isinstance(last, list) and last:
                when = ts_from_unix(last[0].get("timeStamp", 0)).strftime("%d.%m.%Y")
                details["history"] = {"ok": True, "text": f"Historie abrufbar, letzte Transaktion {when}"}
            else:
                details["history"] = {"ok": True, "text": "Historie abrufbar – noch keine eigene Transaktion"}
            nft = self._nft_activity(http, addr, tip)
            details["nft"] = {"ok": not nft, "text": "NFT-Bewegungen vorhanden – werden nicht gebucht (Abdeckungs"
                                                     "grenze)" if nft else "keine NFT-Bewegungen gefunden"}
        return K.CheckResult(True, f"{self.chain_label}: Adresse {short(addr)} über {http.ep.label} lesbar.", details,
                             balances=[K.Balance(self.native, bal, self.chain_label)])

    def _nft_activity(self, http: ChainHttp, addr: str, tip: int) -> bool:
        for action in ("tokennfttx", "token1155tx"):
            try:
                res = self._call(http, {"module": "account", "action": action, "address": addr, "startblock": 0,
                                        "endblock": tip, "page": 1, "offset": 1, "sort": "desc"}, "NFT-Transfers")
            except K.ConnectorError as e:
                if e.kind in ("auth", "rate_limit"):
                    raise
                continue  # nicht jeder Anbieter kennt beide Aktionen
            if isinstance(res, list) and res:
                return True
        return False

    def _native_balance(self, http: ChainHttp, addr: str) -> Decimal:
        res = self._call(http, {"module": "account", "action": "balance", "address": addr, "tag": "latest"},
                         "Bestand")
        try:
            return units(res, 18)
        except (ValueError, ArithmeticError):
            raise K.ConnectorError("data", f"{http.ep.label}: Bestand nicht lesbar.") from None

    def _addr(self, cfg: K.SourceConfig) -> str:
        a = (cfg.address or "").lower()
        if not _ADDR.match(a):
            raise K.ConnectorError("config", "Adresse ungültig – bitte neu eingeben.")
        return a

    # -- Abrufen ------------------------------------------------------------------------------------------
    def fetch(self, cfg: K.SourceConfig, secret: K.Secret, cursor: dict[str, Any] | None) -> K.FetchResult:
        addr = self._addr(cfg)
        w = self.watch(cfg)
        cur = dict(cursor or {})
        start = max(int(cur.get("block", 0) or 0), self.first_block)
        tokens_seen: dict[str, dict[str, Any]] = dict(cur.get("tokens") or {})
        switch = dict(cur.get("switch") or {}) if self.native_switch is not None else {}
        actions = ACTIONS if w.tokens else ACTIONS[:2]
        res = K.FetchResult(complete=True)
        with self.http(cfg, secret) as http:
            tip = self._tip(http)
            safe = tip - self.confirmations
            records: dict[str, list[dict]] = {a: [] for a in actions}
            done = dict.fromkeys(actions, start - 1)
            nxt = dict.fromkeys(actions, start)
            finished = dict.fromkeys(actions, start > safe)
            pages = 0
            stopped = None
            try:
                while not all(finished.values()):
                    action = min((a for a in actions if not finished[a]), key=lambda a: nxt[a])
                    self.report("Abruf", max(min(done.values()) - start + 1, 0), max(safe - start + 1, 1),
                                f"{_ACTION_LABEL[action]} ab Block {nxt[action]:,}".replace(",", "."))
                    rows = self._page(http, action, addr, nxt[action], safe)
                    pages += 1
                    if len(rows) < PAGE:
                        records[action] += rows
                        done[action], finished[action] = safe, True
                        continue
                    blocks = [int(r.get("blockNumber", 0)) for r in rows]
                    last = max(blocks)
                    if min(blocks) == last:  # eine volle Seite in einem einzigen Block
                        extra, complete_block = self._same_block(http, action, addr, last, rows)
                        records[action] += extra
                        if not complete_block:
                            res.gaps.append(f"Block {last}: mehr als {PAGE * MAX_SAME_BLOCK_PAGES} "
                                            f"{_ACTION_LABEL[action]} – nicht vollständig abrufbar")
                        done[action], nxt[action] = last, last + 1
                        finished[action] = last >= safe
                        continue
                    records[action] += [r for r, b in zip(rows, blocks, strict=True) if b < last]
                    done[action], nxt[action] = last - 1, last
            except Stop as e:
                stopped = str(e)
            through = min(done.values())
            if stopped and through < start:
                raise K.ConnectorError("unavailable", f"{stopped} ohne Fortschritt – der nächste Lauf versucht es "
                                                      "erneut.", retry_after_s=600)
            events, skipped, native_delta = self._events(addr, records, through, tokens_seen)
            if self.native_switch is not None:
                conv = self._switch_event(addr, start, through, native_delta, switch)
                if conv is not None:
                    events.append(conv)
            res.events = events
            res.skipped = dict(skipped)
            for what in sorted(self._incomplete):
                res.gaps.append(f"{http.ep.label} meldet {what} im abgefragten Bereich als noch nicht vollständig "
                                "verarbeitet – Bewegungen können fehlen (vollständig: anderen Anbieter wählen)")
            complete = through >= safe and stopped is None
            res.complete = complete
            extra = {"switch": switch} if switch else {}
            if through >= start:
                res.cursor = {"v": 1, "block": through + 1, "tokens": _trim_tokens(tokens_seen), **extra}
                res.resume = not complete
            elif not complete:
                res.cursor = None
            else:  # keine neuen bestätigten Blöcke
                res.cursor = {"v": 1, "block": start, "tokens": _trim_tokens(tokens_seen), **extra}
            if stopped:
                res.warnings.append(f"{stopped} – Fortsetzung ab Block {through + 1:,}".replace(",", "."))
            try:
                res.balances = self._balances(http, addr, tokens_seen)
            except Stop:
                res.balances = None
            res.coverage = {"mode": "historisch" if start <= self.first_block
                            else f"ab Block {start:,}".replace(",", "."),
                            "from_block": start, "to_block": through, "tip": tip, "confirmations": self.confirmations,
                            "pages": pages, "operations": len(events), "provider": http.ep.label,
                            "records": {a: len(v) for a, v in records.items()}, **http.stats()}
        return res

    def _same_block(self, http: ChainHttp, action: str, addr: str, block: int, first: list[dict]) \
            -> tuple[list[dict], bool]:
        out = list(first)
        for page in range(2, MAX_SAME_BLOCK_PAGES + 1):
            rows = self._page(http, action, addr, block, block, page)
            out += rows
            if len(rows) < PAGE:
                return out, True
        return out, False

    def _balances(self, http: ChainHttp, addr: str, tokens: dict[str, dict[str, Any]]) -> list[K.Balance]:
        out = [K.Balance(self.native, self._native_balance(http, addr), self.chain_label)]
        checked = 0
        for contract, meta in sorted(tokens.items(), key=lambda kv: (bool(kv[1].get("spam")), kv[0])):
            if meta.get("spam") or meta.get("dec") is None or checked >= MAX_TOKEN_BALANCES:
                continue
            res = self._call(http, {"module": "account", "action": "tokenbalance", "contractaddress": contract,
                                    "address": addr, "tag": "latest"}, "Token-Bestand")
            checked += 1
            try:
                qty = units(res, int(meta["dec"]))
            except (ValueError, ArithmeticError, TypeError):
                continue
            out.append(K.Balance(token_key(meta.get("sym"), self.chain_tag, contract), qty, meta.get("name")))
        return out

    # -- Einordnung ---------------------------------------------------------------------------------------
    def _switch_event(self, addr: str, start: int, through: int, delta: Decimal, state: dict[str, Any]) \
            -> K.SourceEvent | None:
        """Umstellung des nativen Coins (Polygon: MATIC → POL) als Vorschlag am Umstellungsblock.

        Der Bestand vor dem Block wird aus der abgerufenen Historie summiert (Zu-/Abgänge, interne Bewegungen,
        Gebühren) und im Fortsetzungspunkt mitgeführt; vorgeschlagen wird die Umstellung genau einmal – nur bei
        lückenlosem Abruf ab Block 0, sonst mit ausdrücklichem Hinweis. Nie automatisch übernommen."""
        sw = self.native_switch
        assert sw is not None
        block, old, when, why = sw
        if state.get("done") or start > block:
            state["done"] = True
            return None
        if start == 0 and "pre" not in state:
            state["pre"] = "0"
            state["from_zero"] = True
        try:
            pre = Decimal(str(state.get("pre") or "0")) + delta
        except ArithmeticError:
            pre = delta
        state["pre"] = format(pre.normalize(), "f") if pre else "0"
        if through < block:
            return None  # Umstellungsblock noch nicht erreicht (nächste Etappe)
        state["done"] = True
        if pre <= 0:
            return None
        partial = not state.get("from_zero")
        rec = Rec(line=0, ts=when, kind=M.CONVERSION, out_sym=old, out_qty=pre, in_sym=self.native, in_qty=pre,
                  tag="migration", label=f"{old} → {self.native}",
                  note=f"{why} – Bestand vor dem Block aus der abgerufenen Historie berechnet",
                  review=(f"Umstellung {old} → {self.native} 1:1 – Menge aus der abgerufenen Historie berechnet"
                          + (" (Historie nicht ab Block 0 abgerufen – Menge unsicher)" if partial else
                             "; Brücken-Einzahlungen ohne eigene Transaktion fehlen darin") + ". Mit dem Bestand laut "
                          "Explorer bzw. einer schon erfassten Umstellung vergleichen"),
                  raw={"chain": self.provider, "block": block, "computed_pre": format(pre.normalize(), "f"),
                       "from_block_0": not partial})
        rec.ext_id = "switch"
        return K.SourceEvent(f"{self.provider}:switch-{old.lower()}-{self.native.lower()}:{addr}", when, [rec],
                             f"{old} → {self.native}")

    def _events(self, addr: str, records: dict[str, list[dict]], through: int,
                tokens_seen: dict[str, dict[str, Any]]) -> tuple[list[K.SourceEvent], Counter[str], Decimal]:
        by_hash: dict[str, dict[str, Any]] = defaultdict(lambda: {"n": None, "i": [], "t": []})
        skipped: Counter[str] = Counter()
        for action, key in (("txlist", "n"), ("txlistinternal", "i"), ("tokentx", "t")):
            for r in records.get(action, []):
                h = str(r.get("hash") or r.get("transactionHash") or "").lower()
                try:
                    block = int(r.get("blockNumber", -1))
                except (TypeError, ValueError):
                    block = -1
                if not _HASH.match(h) or block < 0:
                    skipped["Einträge ohne gültigen Hash/Block"] += 1
                    continue
                if block > through:
                    continue  # nächste Etappe
                slot = by_hash[h]
                slot["block"] = block
                slot["ts"] = r.get("timeStamp")
                if key == "n":
                    slot["n"] = r
                else:
                    slot[key].append(r)
        events = []
        sw_block = self.native_switch[0] if self.native_switch is not None else -1
        native_delta = Decimal(0)  # Summe der nativen Bewegungen vor dem Umstellungsblock (Polygon)
        for h, slot in sorted(by_hash.items(), key=lambda kv: (kv[1]["block"], kv[0])):
            tx = self._view(addr, h, slot, skipped, tokens_seen)
            if tx is None:
                continue
            if slot["block"] < sw_block:
                native_delta += sum((m.qty for m in tx.moves if m.asset == tx.fee_asset), Decimal(0)) - tx.fee
            recs = classify(tx)
            if recs:
                events.append(event(self.provider, h, addr, tx.ts, recs, tx.label))
        return events, skipped, native_delta

    def _view(self, addr: str, h: str, slot: dict[str, Any], skipped: Counter[str],
              tokens_seen: dict[str, dict[str, Any]]) -> TxView | None:
        n = slot["n"]
        native = self.native_at(int(slot.get("block") or 0))
        try:
            ts = ts_from_unix(slot.get("ts") or 0)
        except (TypeError, ValueError, OverflowError):
            skipped["Einträge ohne Zeitstempel"] += 1
            return None
        moves: list[Move] = []
        hints: list[str] = []
        fee = Decimal(0)
        initiated = failed = False
        plain = True
        label = None
        raw_moves: list[dict[str, Any]] = []
        if n is not None:
            frm, to = str(n.get("from") or "").lower(), str(n.get("to") or "").lower()
            initiated = frm == addr
            failed = str(n.get("isError") or "0") == "1" or str(n.get("txreceipt_status") or "1") == "0"
            if initiated:
                try:
                    fee = units(int(n.get("gasUsed") or 0) * int(n.get("gasPrice") or 0), 18)
                except (TypeError, ValueError):
                    hints.append("Gebühr nicht lesbar – bitte prüfen")
            func = str(n.get("functionName") or "").split("(", 1)[0].strip()
            method = str(n.get("methodId") or (str(n.get("input") or "")[:10])).lower()
            data = str(n.get("input") or "0x").lower()
            label = func or (method if method not in ("0x", "") else None)
            if initiated and data not in ("0x", "", "deprecated"):
                plain = False
            if not failed:
                try:
                    value = units(n.get("value") or 0, 18)
                except (ValueError, ArithmeticError):
                    value = Decimal(0)
                    hints.append("Wert der Transaktion nicht lesbar")
                if value:
                    if frm == addr:
                        moves.append(Move(native, -value, "n:out"))
                    if to == addr:
                        moves.append(Move(native, value, "n:in"))
                    raw_moves.append({"kind": "native", "from": frm, "to": to, "value": str(n.get("value"))})
        if not failed:
            seen_i: Counter[str] = Counter()
            for r in slot["i"]:
                if str(r.get("isError") or "0") == "1":
                    continue
                try:
                    value = units(r.get("value") or 0, 18)
                except (ValueError, ArithmeticError):
                    hints.append("interne Bewegung nicht lesbar")
                    continue
                if not value:
                    continue
                frm, to = str(r.get("from") or "").lower(), str(r.get("to") or "").lower()
                tid = str(r.get("traceId") or "").strip()
                base = f"i:{tid}" if re.match(r"^[0-9_]{1,40}$", tid) else f"i:{sub_hash(frm, to, r.get('value'))}"
                k = seen_i[base]
                seen_i[base] += 1
                both = frm == addr and to == addr
                if to == addr:
                    moves.append(Move(native, value, _sub(base + (":in" if both else ""), k)))
                if frm == addr:
                    moves.append(Move(native, -value, _sub(base + (":out" if both else ""), k)))
                raw_moves.append({"kind": "internal", "from": frm, "to": to, "value": str(r.get("value")),
                                  "trace": tid or None})
            seen_t: Counter[str] = Counter()
            for r in slot["t"]:
                contract = str(r.get("contractAddress") or "").lower()
                frm, to = str(r.get("from") or "").lower(), str(r.get("to") or "").lower()
                raw_v = str(r.get("value") or "0").strip()
                if not _ADDR.match(contract):
                    hints.append("Token-Transfer ohne gültigen Contract")
                    continue
                if contract in self.mirror_contracts:
                    skipped["native Überweisung zusätzlich als Token-Transfer des Systemvertrags gemeldet (nur einmal "
                            "gezählt)"] += 1
                    continue
                if raw_v in ("0", ""):
                    skipped["Token-Transfers mit Menge 0 (u. a. Address-Poisoning)"] += 1
                    continue
                sym, name = r.get("tokenSymbol"), r.get("tokenName")
                dec_raw = str(r.get("tokenDecimal") or "").strip()
                spam = spam_reason(sym, name)
                meta = tokens_seen.setdefault(contract, {"sym": sym, "name": (name or "")[:60] or None,
                                                         "dec": int(dec_raw) if dec_raw.isdigit() else None,
                                                         "spam": bool(spam)})
                fp = sub_hash(contract, frm, to, raw_v)
                k = seen_t[fp]
                seen_t[fp] += 1
                if not dec_raw.isdigit() or int(dec_raw) > 36:
                    hints.append(f"Token {short(contract)} ohne gültige Dezimalangabe – Menge manuell prüfen")
                    continue
                qty = units(raw_v, int(dec_raw))
                asset = token_key(sym, self.chain_tag, contract)
                both = frm == addr and to == addr
                if to == addr:
                    moves.append(Move(asset, qty, f"t:{fp}{':in' if both else ''}#{k}", spam=spam))
                if frm == addr:
                    moves.append(Move(asset, -qty, f"t:{fp}{':out' if both else ''}#{k}"))
                if initiated and len(slot["t"]) == 1 and n is not None and \
                        str(n.get("to") or "").lower() == contract and \
                        str(n.get("methodId") or str(n.get("input") or "")[:10]).lower() in _TRANSFER_SELECTORS:
                    plain = True  # einfache Token-Überweisung
                raw_moves.append({"kind": "token", "contract": contract, "from": frm, "to": to, "value": raw_v,
                                  "decimals": dec_raw, "symbol": (sym or "")[:20]})
                meta["dec"] = meta.get("dec") if meta.get("dec") is not None else int(dec_raw)
        raw = {"block": slot.get("block"), "from": (n or {}).get("from"), "to": (n or {}).get("to"),
               "fee": f"{fee.normalize():f}" if fee else None, "status": "failed" if failed else "ok",
               "confirmed": True, "min_confirmations": self.confirmations, "moves": raw_moves[:20]}
        return TxView(self.provider, h, ts, moves, fee=fee, fee_asset=native, initiated=initiated,
                      failed=failed, plain=plain, hint="; ".join(dict.fromkeys(hints)) or None, label=label, raw=raw)


def _sub(base: str, k: int) -> str:
    return base if k == 0 else f"{base}#{k}"


def _trim_tokens(tokens: dict[str, dict[str, Any]], limit: int = 300) -> dict[str, dict[str, Any]]:
    """Gesehene Tokens im Fortsetzungspunkt (für die Bestandsprüfung) – begrenzt, Nicht-Spam zuerst."""
    items = sorted(tokens.items(), key=lambda kv: (bool(kv[1].get("spam")), kv[0]))[:limit]
    return {k: {kk: vv for kk, vv in v.items() if vv is not None} for k, v in items}


@K.register
class EthereumConnector(EvmConnector):
    provider = "ethereum"
    label = "Ethereum (Etherscan/Routescan)"
    chain_label = "Ethereum"
    chain_id = 1
    chain_tag = "ETH"
    native = "ETH"
    confirmations = 64  # ≈ 2 Epochen – finalisiert
    endpoints = ("etherscan", "routescan")
    explorer_tx = "https://etherscan.io/tx/{}"
    explorer_addr = "https://etherscan.io/address/{}"
    explorer_token = "https://etherscan.io/token/{}"  # noqa: S105 - Link-Muster


@K.register
class BscConnector(EvmConnector):
    provider = "bsc"
    label = "BNB Smart Chain (Etherscan/Routescan)"
    chain_label = "BNB Chain"
    chain_id = 56
    chain_tag = "BSC"
    native = "BNB"
    confirmations = 20
    endpoints = ("etherscan", "routescan")
    explorer_tx = "https://bscscan.com/tx/{}"
    explorer_addr = "https://bscscan.com/address/{}"
    explorer_token = "https://bscscan.com/token/{}"  # noqa: S105 - Link-Muster


@K.register
class AvalancheConnector(EvmConnector):
    provider = "avalanche"
    label = "Avalanche C-Chain (Routescan/Etherscan)"
    chain_label = "Avalanche C-Chain"
    chain_id = 43114
    chain_tag = "AVAX"
    native = "AVAX"
    confirmations = 6  # sofortige Finalität (Snowman); kleiner Puffer für den Indexer
    endpoints = ("routescan", "etherscan")
    explorer_tx = "https://snowtrace.io/tx/{}"
    explorer_addr = "https://snowtrace.io/address/{}"
    explorer_token = "https://snowtrace.io/token/{}"  # noqa: S105 - Link-Muster


POLYGON_NATIVE = "0x0000000000000000000000000000000000001010"  # MRC20-Systemvertrag des nativen Coins


@K.register
class PolygonConnector(EvmConnector):
    provider = "polygon"
    label = "Polygon PoS (Etherscan/Blockscout)"
    chain_label = "Polygon PoS"
    chain_id = 137
    chain_tag = "POLYGON"
    native = "POL"
    confirmations = 128  # Bor-Blöcke ≈ 2 s; Puffer bis zur Checkpoint-Finalität des Indexers
    endpoints = ("etherscan", "blockscout_polygon")
    mirror_contracts = frozenset({POLYGON_NATIVE})
    native_switch = (62_278_656, "MATIC", datetime(2024, 9, 26, 0, 42, 49, tzinfo=UTC),
                     "Polygon PoS: nativer Coin seit 04.09.2024 POL (1:1 aus MATIC); Ticker on-chain ab Block "
                     "62.278.656 (Hardfork Ahmedabad, 26.09.2024) – davor als MATIC gebucht")
    limits = (*EvmConnector.limits,
              "Natives MATIC/POL: bis Block 62.278.656 als MATIC, danach als POL; die Umstellung wird einmal zur "
              "Prüfung vorgeschlagen (Menge aus der Historie berechnet)",
              "Einzahlungen über die PoS-Bridge (State-Sync, ohne eigene Transaktion) sind in der Historie der "
              "Anbieter ggf. nicht enthalten – Abgleich über die Bestandsprüfung")
    explorer_tx = "https://polygonscan.com/tx/{}"
    explorer_addr = "https://polygonscan.com/address/{}"
    explorer_token = "https://polygonscan.com/token/{}"  # noqa: S105 - Link-Muster


PULSE_FORK_BLOCK = 17_233_000  # letzter Ethereum-Block der kopierten Historie (on-chain geprüft: Miner ≠ 0x…0369)
PULSE_FIRST_BLOCK = 17_233_001  # erster PulseChain-Block (11.05.2023 06:23:15 UTC, Miner 0x…0369)
PULSE_START = datetime(2023, 5, 11, 6, 23, 15, tzinfo=UTC)


@K.register
class PulseChainConnector(EvmConnector):
    """PulseChain (Chain-ID 369) über den offiziellen Explorer (Blockscout, Etherscan-kompatibel, ohne Key).

    PulseChain ist eine Kopie des Ethereum-Zustands am Block 17.233.000: Die Historie davor gehört zu Ethereum und wird
    nicht abgefragt (Start ab Block 17.233.001). Der beim Fork kopierte native Bestand wird einmalig per RPC
    (``eth_getBalance`` am Fork-Block) gelesen und als prüfpflichtige Eröffnung vorgelegt – nie automatisch gebucht.
    Kopierte Tokens (ERC-20-Kopien) werden nicht als Eröffnung angelegt; sie erscheinen in der Bestandsprüfung.
    """

    provider = "pulsechain"
    label = "PulseChain (PulseChain-Explorer/Blockscout)"
    chain_label = "PulseChain"
    chain_id = 369
    chain_tag = "PLS"
    native = "PLS"
    confirmations = 32  # Blöcke ≈ 10 s; Puffer bis zur Finalität des Indexers
    endpoints = ("blockscout_pulsechain",)
    first_block = PULSE_FIRST_BLOCK
    explorer_tx = "https://scan.pulsechain.com/tx/{}"
    explorer_addr = "https://scan.pulsechain.com/address/{}"
    explorer_token = "https://scan.pulsechain.com/token/{}"  # noqa: S105 - Link-Muster
    limits = (*EvmConnector.limits,
              "Historie ab dem ersten PulseChain-Block 17.233.001 (11.05.2023) – davor Ethereum-Historie",
              "Beim Fork kopierter PLS-Bestand: einmalige Eröffnung zur Prüfung (aus eth_getBalance am Fork-Block); "
              "kopierte Tokens werden nicht eröffnet, Abweichungen zeigt die Bestandsprüfung")

    def fetch(self, cfg: K.SourceConfig, secret: K.Secret, cursor: dict[str, Any] | None) -> K.FetchResult:
        res = super().fetch(cfg, secret, cursor)
        if int((cursor or {}).get("block", 0) or 0) > self.first_block:
            return res  # Eröffnung nur beim Erstabruf (bekannte Kennung wird ohnehin erkannt)
        addr = self._addr(cfg)
        try:
            qty = self._fork_balance(addr)
        except (K.ConnectorError, Stop) as e:
            res.warnings.append(f"Bestand am Fork-Block nicht abrufbar ({getattr(e, 'message', e)}) – Eröffnung "
                                "fehlt; die Bestandsprüfung zeigt die Differenz")
            return res
        if qty:
            rec = Rec(line=0, ts=PULSE_START, kind=M.DEPOSIT, in_sym=self.native, in_qty=qty, tag="fork",
                      label="PulseChain-Fork: kopierter Bestand",
                      note=f"Bestand laut Chain am Fork-Block {PULSE_FORK_BLOCK:,}".replace(",", "."),
                      review="Eröffnungsbestand aus dem PulseChain-Fork (beim Fork aus dem Ethereum-Zustand "
                             "übernommen) – steuerliche Einordnung (Fork-Zugang) und Wert prüfen",
                      raw={"chain": self.provider, "block": PULSE_FORK_BLOCK, "source": "eth_getBalance"})
            rec.ext_id = "fork"
            res.events.insert(0, K.SourceEvent(f"{self.provider}:fork-{PULSE_FORK_BLOCK}:{addr}", PULSE_START, [rec],
                                               rec.label))
        return res

    def _fork_balance(self, addr: str) -> Decimal:
        from app.datasources.chainhttp import ENDPOINTS

        with ChainHttp(ENDPOINTS["pulsechain_rpc"], transport=self.transport, sleep=self.sleep, clock=self.clock,
                       max_requests=3, deadline_s=30) as http:
            body = http.post("", {"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance",
                                  "params": [addr, hex(PULSE_FORK_BLOCK)]}, what="Bestand am Fork-Block")
        res = body.get("result") if isinstance(body, dict) else None
        if not isinstance(res, str) or not res.startswith("0x"):
            err = (body or {}).get("error") if isinstance(body, dict) else None
            raise K.ConnectorError("data", "RPC-Antwort ohne Bestand"
                                   + (f" ({str(err.get('message'))[:80]})" if isinstance(err, dict) else ""))
        return units(int(res, 16), 18)


class PeaqEvmConnector(EvmConnector):
    """peaq EVM (Chain-ID 3338, H160-Adressen) über Subscans Etherscan-kompatible API.

    Subscan unterstützt laut Routenbeschreibung ``balance``, ``txlist``, ``txlistinternal``, ``tokentx``,
    ``tokennfttx``, ``tokenbalance`` und ``getblocknobytime`` (kein ``proxy``-Modul). Nur mit direktem Subscan-Key
    – das kostenlose PubFi-Gateway lässt für diese Route keine Abfrageparameter zu. Wird über :class:`PeaqConnector`
    (Anbieter „peaq“) für 0x-Adressen verwendet, nicht eigenständig ausgewählt.
    """

    provider = "peaq"  # Ereignis-IDs „peaq:…“ (nicht registriert – Auswahl über PeaqConnector)
    label = "peaq EVM (Subscan, direkter Key)"
    chain_label = "peaq"
    chain_id = 3338
    chain_tag = "PEAQ"
    native = "PEAQ"
    confirmations = 12
    endpoints = ("subscan_evm",)
    network = "peaq"
    explorer_tx = "https://peaq.subscan.io/tx/{}"
    explorer_addr = "https://peaq.subscan.io/account/{}"
    explorer_token = "https://peaq.subscan.io/token/{}"  # noqa: S105 - Link-Muster

    def http(self, cfg: K.SourceConfig, secret: K.Secret, **kw: Any) -> ChainHttp:
        kw.setdefault("network", self.network)
        return super().http(cfg, secret, **kw)

    def _tip(self, http: ChainHttp) -> int:
        res = self._call(http, {"module": "block", "action": "getblocknobytime",
                                "timestamp": int(datetime.now(UTC).timestamp()), "closest": "before"}, "Blockhöhe")
        if isinstance(res, dict):
            res = res.get("blockNumber")
        try:
            return int(str(res))
        except ValueError:
            raise K.ConnectorError("data", f"{http.ep.label}: Blockhöhe nicht lesbar.") from None
