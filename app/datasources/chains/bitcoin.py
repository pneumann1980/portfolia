"""Bitcoin – Einzeladressen und öffentliche Kontoschlüssel (xpub/ypub/zpub), nur lesend über die Esplora-API.

Anbieter
    **mempool.space** (Standard) bzw. **Blockstream Esplora** – ohne Key; Portfolia fragt höchstens 1×/s.
    Genutzt: ``/blocks/tip/height``, ``/address/<a>`` (Statistik: Anzahl Transaktionen, bestätigte Summen),
    ``/address/<a>/txs/chain[/<letzte txid>]`` (25 je Seite, neueste zuerst, nur bestätigte).

Konto
    Alle Adressen eines Kontos (eingetragene Adressen und – beim Kontoschlüssel – abgeleitete Empfangs- und
    Wechselgeldadressen bis zum Gap-Limit) bilden *eine* Wallet. Je Transaktion zählt die Bilanz über alle eigenen
    Ein- und Ausgänge: Eingang = Summe eigener Ausgänge ohne eigene Eingänge; Abgang = eigene Eingänge − eigene
    Ausgänge (Wechselgeld) − Gebühr; nur Gebühr = Umbuchung innerhalb der Wallet (z. B. Konsolidierung). Stammen
    Eingänge auch von fremden Adressen (CoinJoin, PayJoin), geht der Vorgang mit Saldo und ohne Gebührenzuordnung in
    die Prüfung. Gebucht wird erst ab ``confirmations`` Bestätigungen; Unbestätigtes wird nur gezählt.

Grenzen
    Einzeladressen decken die Wallet nicht vollständig ab (Wechselgeld an nicht eingetragene Adressen erscheint als
    Abgang). Beim Kontoschlüssel endet die Suche nach ``gap`` ungenutzten Adressen in Folge. Lightning, Multisig,
    Ordinals/Runes werden nicht erfasst.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from app.datasources import connector as K
from app.datasources.chainhttp import ChainHttp, Stop
from app.datasources.chains.btckeys import Account, validate_address
from app.datasources.wallet import (
    SCRIPT_TYPES,
    Move,
    TxView,
    WalletConnector,
    classify,
    event,
    short,
    ts_from_unix,
    units,
)

PAGE = 25
SAT = 8
_TXID = re.compile(r"^[0-9a-f]{64}$")
MAX_DERIVED = 2000


class _Universe:
    """Eigene Adressen des Kontos (eingetragen + abgeleitet) mit Herkunft für die Anzeige."""

    def __init__(self) -> None:
        self.addrs: dict[str, str] = {}  # Adresse → Herkunft (z. B. „Empfang 3“, „eingetragen“)
        self.stats: dict[str, dict[str, Any]] = {}

    def add(self, a: str, origin: str) -> None:
        self.addrs.setdefault(a, origin)

    def __contains__(self, a: object) -> bool:
        return a in self.addrs


@K.register
class BitcoinConnector(WalletConnector):
    provider = "bitcoin"
    label = "Bitcoin (Esplora: mempool.space/Blockstream)"
    chain_label = "Bitcoin"
    native = "BTC"
    endpoints = ("mempool", "blockstream")
    confirmations: ClassVar[int] = 3
    explorer_tx = "https://mempool.space/tx/{}"
    explorer_addr = "https://mempool.space/address/{}"
    limits = ("Lightning, Multisig, Ordinals/Runes/BRC-20 werden nicht erfasst",
              "gebucht wird ab 3 Bestätigungen; unbestätigte Vorgänge werden nur gezählt")

    def coverage_limits(self, cfg: K.SourceConfig) -> list[str]:
        w = self.watch(cfg)
        out = list(self.limits)
        if w.xpubs:
            out.insert(0, f"Kontoschlüssel ({SCRIPT_TYPES.get(w.script or '', w.script)}): Empfangs- und "
                          f"Wechselgeldadressen bis {w.gap} ungenutzte Adressen in Folge – Adressen jenseits davon "
                          "werden nicht gefunden")
        if w.addresses or not w.xpubs:
            out.insert(0, "Einzeladressen decken die Wallet nicht vollständig ab: Wechselgeld an nicht eingetragene "
                          "Adressen erscheint als Abgang – für vollständige Abdeckung den Kontoschlüssel hinterlegen")
        return out

    # -- Hilfen -------------------------------------------------------------------------------------------
    def _owner(self, cfg: K.SourceConfig) -> str:
        w = self.watch(cfg)
        return w.watch_id or (cfg.address or "")

    def _explicit(self, cfg: K.SourceConfig) -> list[str]:
        w = self.watch(cfg)
        out = [validate_address(a) for a in w.addresses]
        if not out and not w.xpubs and cfg.address and cfg.address[1:4] != "pub":
            out = [validate_address(cfg.address)]
        return out

    def _account(self, cfg: K.SourceConfig) -> Account | None:
        w = self.watch(cfg)
        xpub = (w.xpubs or ([cfg.address] if cfg.address and cfg.address[1:4] == "pub" else []))[:1]
        if not xpub:
            return None
        try:
            return Account(xpub[0], w.script)
        except ValueError as e:
            raise K.ConnectorError("config", f"Kontoschlüssel ungültig: {e}") from None

    def _stats(self, http: ChainHttp, addr: str) -> dict[str, Any]:
        body = http.get(f"/address/{addr}", what="Adressstatistik")
        if not isinstance(body, dict) or not isinstance(body.get("chain_stats"), dict):
            raise K.ConnectorError("data", f"{http.ep.label}: Adressstatistik nicht lesbar.")
        return body

    @staticmethod
    def _count(st: dict[str, Any], key: str = "chain_stats") -> int:
        try:
            return int((st.get(key) or {}).get("tx_count") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _sum(st: dict[str, Any]) -> int:
        c = st.get("chain_stats") or {}
        return int(c.get("funded_txo_sum") or 0) - int(c.get("spent_txo_sum") or 0)

    def _tip(self, http: ChainHttp) -> int:
        v = http.get("/blocks/tip/height", what="Blockhöhe")
        try:
            return int(v)
        except (TypeError, ValueError):
            raise K.ConnectorError("data", f"{http.ep.label}: Blockhöhe nicht lesbar.") from None

    def _scan(self, http: ChainHttp, cfg: K.SourceConfig, uni: _Universe, cur: dict[str, Any]) -> dict[str, Any]:
        """Adressen ermitteln: eingetragene + abgeleitete bis zum Gap-Limit (Statistik je Adresse)."""
        w = self.watch(cfg)
        for a in self._explicit(cfg):
            uni.add(a, "eingetragen")
        for a in list(uni.addrs):
            uni.stats[a] = self._stats(http, a)
        acc = self._account(cfg)
        derive: dict[str, Any] = {}
        if acc is not None:
            for chain, label in ((0, "Empfang"), (1, "Wechselgeld")):
                used = -1
                i = 0
                while i - used <= w.gap:
                    if i >= MAX_DERIVED:
                        raise K.ConnectorError("data", f"Mehr als {MAX_DERIVED} Adressen abgeleitet – Gap-Limit "
                                                       "prüfen.")
                    a = acc.addr(chain, i)
                    uni.add(a, f"{label} {i}")
                    st = self._stats(http, a)
                    uni.stats[a] = st
                    if self._count(st) or self._count(st, "mempool_stats"):
                        used = i
                    self.report("Adressen", len(uni.addrs), None, f"{label}sadressen werden geprüft ({i + 1})")
                    i += 1
                derive[str(chain)] = {"used": used, "scanned": i}
        return derive

    def _history(self, http: ChainHttp, addr: str, known_top: str | None) -> tuple[list[dict], bool]:
        """Bestätigte Transaktionen der Adresse, neueste zuerst, bis zur zuletzt verarbeiteten. Rückgabe: (Liste,
        bekannte Transaktion erreicht) – ohne Treffer (erster Abruf, Reorg) ist die Liste die ganze Historie."""
        out: list[dict] = []
        last: str | None = None
        while True:
            path = f"/address/{addr}/txs/chain" + (f"/{last}" if last else "")
            page = http.get(path, what="Transaktionen")
            if not isinstance(page, list):
                raise K.ConnectorError("data", f"{http.ep.label}: Transaktionsliste nicht lesbar.")
            for tx in page:
                tid = str(tx.get("txid") or "")
                if not _TXID.match(tid):
                    raise K.ConnectorError("data", f"{http.ep.label}: ungültige Transaktions-ID.")
                if known_top and tid == known_top:
                    return out, True
                out.append(tx)
            if len(page) < PAGE:
                return out, False
            last = str(page[-1]["txid"])

    # -- Prüfen -------------------------------------------------------------------------------------------
    def check(self, cfg: K.SourceConfig, secret: K.Secret) -> K.CheckResult:
        details: dict[str, Any] = {}
        balances: list[K.Balance] | None = None
        with self.http(cfg, secret, max_requests=40, deadline_s=90) as http:
            tip = self._tip(http)
            details["chain"] = {"ok": True, "text": f"Bitcoin erreichbar, Block {tip:,}".replace(",", ".")}
            explicit = self._explicit(cfg)
            total = 0
            active = 0
            for a in explicit:
                st = self._stats(http, a)
                total += self._sum(st)
                active += 1 if self._count(st) else 0
            if explicit:
                details["addresses"] = {"ok": True, "text": f"{len(explicit)} Adresse(n), {active} mit Aktivität"}
                if not self._account(cfg):
                    balances = [K.Balance("BTC", units(total, SAT), "Bitcoin")]
            acc = self._account(cfg)
            if acc is not None:
                w = self.watch(cfg)
                first = acc.addr(0, 0)
                found = []
                for script in SCRIPT_TYPES:
                    probe = Account(w.xpubs[0] if w.xpubs else str(cfg.address), script).addr(0, 0)
                    if self._count(self._stats(http, probe)):
                        found.append(script)
                ok = acc.script in found or not found
                hint = (f"Aktivität gefunden für {', '.join(SCRIPT_TYPES[s] for s in found)}" if found else
                        "noch keine Aktivität auf der ersten Empfangsadresse")
                details["xpub"] = {"ok": ok, "text": f"erste Empfangsadresse {first} ({SCRIPT_TYPES[acc.script]}); "
                                                     f"{hint}" + ("" if ok else " – Adresstyp in den Einstellungen "
                                                                                 "anpassen")}
        msg = f"Bitcoin über {http.ep.label} lesbar." if all(d["ok"] for d in details.values()) else \
            "Bitcoin lesbar – Hinweise beachten."
        return K.CheckResult(True, msg, details, balances=balances)

    # -- Abrufen ------------------------------------------------------------------------------------------
    def fetch(self, cfg: K.SourceConfig, secret: K.Secret, cursor: dict[str, Any] | None) -> K.FetchResult:
        cur = dict(cursor or {})
        seen: dict[str, dict[str, Any]] = dict(cur.get("addr") or {})
        res = K.FetchResult(complete=True)
        uni = _Universe()
        owner = self._owner(cfg)
        with self.http(cfg, secret) as http:
            tip = self._tip(http)
            safe = tip - self.confirmations + 1  # höchste Blockhöhe mit ausreichend Bestätigungen
            try:
                derive = self._scan(http, cfg, uni, cur)
            except Stop as e:  # ohne vollständige Adressmenge keine sichere Bilanz → nichts buchen
                raise K.ConnectorError("unavailable", f"{e} bei der Adresssuche – der nächste Lauf versucht es "
                                                      "erneut.", retry_after_s=600) from None
            txs: dict[str, dict] = {}
            new_seen = dict(seen)
            pending = 0
            todo = []
            for a in uni.addrs:
                if not self._count(uni.stats[a]):
                    new_seen.setdefault(a, {"n": 0, "top": None})  # ungenutzt: keine Abfrage nötig
                elif self._count(uni.stats[a]) != int((seen.get(a) or {}).get("n", -1)):
                    todo.append(a)
            stopped = None
            for i, a in enumerate(todo):
                self.report("Abruf", i, len(todo), f"Transaktionen von {short(a)}")
                prev = seen.get(a) or {}
                try:
                    items, reached = self._history(http, a, prev.get("top"))
                except Stop as e:
                    stopped = str(e)
                    break
                deep = [t for t in items if int((t.get("status") or {}).get("block_height") or 10**12) <= safe]
                for t in deep:
                    txs.setdefault(str(t["txid"]), t)
                # junge Transaktionen (zu wenige Bestätigungen) zählen nicht als verarbeitet → nächster Lauf holt sie
                base = int(prev.get("n", 0) or 0) if reached else 0
                top = str(deep[0]["txid"]) if deep else (prev.get("top") if reached else None)
                new_seen[a] = {"n": base + len(deep), "top": top}
                pending += len(items) - len(deep)
            pending += sum(self._count(uni.stats[a], "mempool_stats") for a in uni.addrs)
            events = []
            skipped: dict[str, int] = {}
            for tid, t in sorted(txs.items(), key=lambda kv: (int(kv[1]["status"]["block_height"]), kv[0])):
                view = self._view(t, uni)
                if view is None:
                    skipped["Transaktionen ohne eigene Beträge"] = skipped.get("Transaktionen ohne eigene Beträge",
                                                                              0) + 1
                    continue
                recs = classify(view)
                if recs:
                    events.append(event(self.provider, tid, owner, view.ts, recs, view.label))
            res.events = events
            res.skipped = skipped
            res.complete = stopped is None
            res.resume = stopped is not None
            res.cursor = {"v": 1, "addr": new_seen, "derive": derive}
            if stopped:
                res.warnings.append(f"{stopped} – Fortsetzung beim nächsten Lauf")
            res.balances = [K.Balance("BTC", units(sum(self._sum(uni.stats[a]) for a in uni.addrs), SAT), "Bitcoin",
                                      "bestätigter Bestand aller Adressen des Kontos")]
            res.coverage = {"mode": "historisch" if not seen else "inkrementell", "tip": tip,
                            "confirmations": self.confirmations, "addresses": len(uni.addrs),
                            "used_addresses": sum(1 for a in uni.addrs if self._count(uni.stats[a])),
                            "derive": derive, "operations": len(events), "pending": pending,
                            "provider": http.ep.label, **http.stats()}
        return res

    def _view(self, t: dict[str, Any], uni: _Universe) -> TxView | None:
        tid = str(t["txid"])
        status = t.get("status") or {}
        ins_ours = ins_total = 0
        foreign_inputs = False
        coinbase = False
        for vin in t.get("vin") or []:
            if vin.get("is_coinbase"):
                coinbase = True
                continue
            prev = vin.get("prevout") or {}
            v = int(prev.get("value") or 0)
            ins_total += v
            if prev.get("scriptpubkey_address") in uni:
                ins_ours += v
            else:
                foreign_inputs = True
        outs_ours = sum(int(o.get("value") or 0) for o in t.get("vout") or []
                        if o.get("scriptpubkey_address") in uni)
        fee = int(t.get("fee") or 0)
        ours = sorted({o.get("scriptpubkey_address") for o in t.get("vout") or []
                       if o.get("scriptpubkey_address") in uni} |
                      {(v.get("prevout") or {}).get("scriptpubkey_address") for v in t.get("vin") or []
                       if (v.get("prevout") or {}).get("scriptpubkey_address") in uni})
        raw = {"block": status.get("block_height"), "confirmed": True, "fee_sat": fee, "in_ours_sat": ins_ours,
               "out_ours_sat": outs_ours, "own_addresses": [f"{a} ({uni.addrs[a]})" for a in ours][:8]}
        ts = ts_from_unix(status.get("block_time") or 0)
        if ins_ours == 0 and outs_ours == 0:
            return None
        if ins_ours == 0:  # Eingang
            hint = "Coinbase-Ausgang (Mining) – Art prüfen" if coinbase else None
            return TxView(self.provider, tid, ts, [Move("BTC", units(outs_ours, SAT), "in")], hint=hint, raw=raw)
        if not foreign_inputs:  # alle Eingänge eigen: Abgang an Dritte = Eingänge − Wechselgeld − Gebühr
            sent = ins_ours - outs_ours - fee
            if sent < 0:
                return TxView(self.provider, tid, ts, [Move("BTC", units(-sent, SAT), "in")],
                              hint="Bilanz unerwartet (Ausgänge > Eingänge − Gebühr) – prüfen", raw=raw)
            moves = [Move("BTC", -units(sent, SAT), "out")] if sent else []
            return TxView(self.provider, tid, ts, moves, fee=units(fee, SAT), fee_asset="BTC", initiated=True,
                          hint=None if sent else "Umbuchung innerhalb der Wallet (z. B. Konsolidierung) – nur Gebühr",
                          raw=raw)
        net = outs_ours - ins_ours  # fremde Eingänge (CoinJoin/PayJoin): nur Saldo, Gebühr nicht zuordenbar
        hint = "Transaktion mit fremden Eingängen (z. B. CoinJoin/PayJoin) – Saldo ohne Gebührenanteil, bitte prüfen"
        sub = "in" if net > 0 else "out"
        return TxView(self.provider, tid, ts, [Move("BTC", units(net, SAT), sub)], initiated=True, hint=hint,
                      raw=raw)
