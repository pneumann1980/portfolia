"""Cardano – ADA, native Assets und Staking-Rewards über Koios (api.koios.rest), nur lesend; Key optional.

Konto statt Einzeladresse (UTXO-Modell)
    Eine Cardano-Wallet (Ledger, Yoroi, Eternl …) verwendet viele Empfangs- und Wechselgeld-Adressen, die alle
    denselben Stake-Teil tragen. Portfolia führt das Konto deshalb über die **Stake-Adresse** (``stake1…``) – direkt
    angegeben oder aus einer Basisadresse (``addr1q…``) abgeleitet (CIP-19). Eigene Ein-/Ausgänge sind alle UTXOs
    mit diesem Stake-Teil; Wechselgeld an eine neue Adresse derselben Wallet ist damit kein Abgang. Adressen ohne
    Stake-Teil (Enterprise ``addr1v…``) werden einzeln geführt („Adressmodus“) – Wechselgeld an andere Adressen
    erscheint dann als Abgang (Abdeckungsgrenze, wird angezeigt).

Abruf (Koios v1, dokumentierte Endpunkte)
    ``/tip``; ``/account_txs`` (bzw. ``/address_txs``) aufsteigend nach Blockhöhe in Seiten zu 1.000 (``offset``/
    ``limit``); ``/tx_info`` in Gruppen (Ein-/Ausgänge mit Assets, Reward-Abhebungen, Zertifikate);
    ``/account_reward_history``; Bestände über ``/account_info`` + ``/account_assets`` (bzw. ``/address_info`` +
    ``/address_assets``). Abgefragt werden nur Blöcke mit mindestens 15 Bestätigungen.

Bilanz je Transaktion (Lovelace, exakt)
    Δ = Σ eigene Ausgänge − Σ eigene Eingänge. Externe ADA-Bewegung = Δ + Gebühr + Pfand − eigene
    Reward-Abhebung: Abhebungen sind eine Umbuchung vom Reward-Konto ins Wallet (kein Zugang, die Rewards selbst
    werden je Epoche gebucht). Gebühr und Pfand trägt das Konto nur, wenn alle Eingänge eigene sind; fremde Eingänge
    (DEX-Batcher, gemeinsame Transaktionen) gehen ohne Gebühr in die Prüfung. Pfand (Stake-Registrierung, DRep,
    Governance) und seine Erstattung erscheinen als eigene, prüfbedürftige Bewegung. Native Assets je Policy +
    Name, identifiziert über den CIP-14-Fingerabdruck (``asset1…``), nie über den Namen.

Rewards
    ``member``/``leader`` als Staking-Ertrag, ``treasury``/``reserves`` (MIR) als Ertrag, ``refund`` (Pool-Pfand)
    zur Prüfung – je Epoche ein Ereignis, Zeitpunkt = Beginn der Epoche, ab der sie verfügbar sind (Shelley:
    Epoche 208 begann am 29.07.2020 21:44:51 UTC, 5 Tage je Epoche).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

from app.csvimport import model as M
from app.csvimport.model import Rec
from app.datasources import connector as K
from app.datasources.chainhttp import ChainHttp, Stop
from app.datasources.chains.codec import cardano_decode, cardano_stake_of
from app.datasources.wallet import (
    Move,
    TxView,
    WalletConnector,
    classify,
    clean_symbol,
    event,
    short,
    spam_reason,
    token_key,
    units,
)

LOVELACE = 6
PAGE = 1000
TX_BATCH = 25
CONFIRMATIONS = 15
CHAIN_TAG = "CARDANO"
SHELLEY_EPOCH, SHELLEY_START, EPOCH_S = 208, 1_596_059_091, 432_000
_HASH = re.compile(r"^[0-9a-f]{64}$")
_FP = re.compile(r"^asset1[0-9a-z]{38}$")
INCOME = {"member": "staking", "leader": "staking", "treasury": "reward", "reserves": "reward"}


def epoch_start(epoch: int) -> datetime:
    """Beginn einer Shelley-Epoche (feste Länge 432.000 s laut Shelley-Genesis)."""
    return datetime.fromtimestamp(SHELLEY_START + (int(epoch) - SHELLEY_EPOCH) * EPOCH_S, UTC)


def asset_symbol(name_hex: str | None) -> str:
    try:
        text = bytes.fromhex(name_hex or "").decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return "TOKEN"
    return clean_symbol(text, "TOKEN") if text.isprintable() else "TOKEN"


def _is_script(addr: str | None) -> bool:
    """Zahlungsteil ist ein Skript (CIP-19 Typ 1/3/5/7) – z. B. DEX-, Staking- oder Vertragsadresse."""
    if not addr or not addr.startswith("addr1"):
        return False
    try:
        _, typ, _, _ = cardano_decode(addr)
    except ValueError:
        return False
    return typ in (1, 3, 5, 7)


@K.register
class CardanoConnector(WalletConnector):
    provider = "cardano"
    label = "Cardano (Koios)"
    chain_label = "Cardano"
    native = "ADA"
    endpoints = ("koios",)
    explorer_tx = "https://cardanoscan.io/transaction/{}"
    explorer_addr = "https://cardanoscan.io/address/{}"
    explorer_token = "https://cardanoscan.io/token/{}"  # noqa: S105 - Link-Muster
    limits = ("Konto über die Stake-Adresse: alle Adressen mit demselben Stake-Teil inkl. Wechselgeld; Rewards je "
              "Epoche als Ertrag",
              "Smart-Contract-Vorgänge (DEX, Lending, NFTs) und Transaktionen mit fremden Eingängen zur Prüfung; "
              "Pfand (Stake-Registrierung, Governance) als prüfbedürftige Bewegung",
              "Ungültige Plutus-Transaktionen (nur Sicherheitsleistung belastet) werden von Koios nicht gesondert "
              "gekennzeichnet – Abgleich über die Bestandsprüfung")

    def coverage_limits(self, cfg: K.SourceConfig) -> list[str]:
        try:
            stake, _ = self._scope(cfg)
        except K.ConnectorError:
            return list(self.limits)
        if stake:
            return list(self.limits)
        return ["Adressmodus (Adressen ohne Stake-Teil): nur die angegebenen Adressen – Wechselgeld an andere "
                "Adressen der Wallet erscheint als Abgang; keine Rewards", *self.limits[1:]]

    # -- Konto --------------------------------------------------------------------------------------------
    def _scope(self, cfg: K.SourceConfig) -> tuple[str | None, list[str]]:
        """(Stake-Adresse oder None, weitere eigene Zahlungsadressen ohne Stake-Teil)."""
        w = self.watch(cfg)
        items = [a.lower() for a in (w.addresses or ([cfg.address] if cfg.address else []))]
        stakes: set[str] = set()
        extra: list[str] = []
        for a in items:
            try:
                st = cardano_stake_of(a)
            except ValueError:
                raise K.ConnectorError("config", "Cardano-Adresse ungültig – bitte neu eingeben.") from None
            if st:
                stakes.add(st)
            elif a not in extra:
                extra.append(a)
        if len(stakes) > 1:
            raise K.ConnectorError("config", "Adressen verschiedener Cardano-Konten (verschiedene Stake-Teile) – bitte "
                                             "je Konto ein eigenes Wallet-Konto anlegen.")
        return (next(iter(stakes)) if stakes else None), extra

    # -- Anbieter-API -------------------------------------------------------------------------------------
    @staticmethod
    def _list(body: Any, what: str, http: ChainHttp) -> list[dict[str, Any]]:
        if not isinstance(body, list):
            raise K.ConnectorError("data", f"Unerwartete Antwort von {http.ep.label} ({what}).")
        return [r for r in body if isinstance(r, dict)]

    def _paged(self, http: ChainHttp, path: str, what: str, *, body: dict[str, Any] | None = None,
               params: dict[str, Any] | None = None, order: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            q = {**(params or {}), "offset": offset, "limit": PAGE}
            if order:
                q["order"] = order
            page = self._list(http.post(path, body, q, what=what) if body is not None
                              else http.get(path, q, what=what), what, http)
            out += page
            if len(page) < PAGE:
                return out
            offset += PAGE

    def _tip(self, http: ChainHttp) -> dict[str, Any]:
        rows = self._list(http.get("/tip", what="Kettenspitze"), "Kettenspitze", http)
        if not rows:
            raise K.ConnectorError("unavailable", f"{http.ep.label}: Kettenspitze nicht verfügbar.")
        try:
            return {"height": int(rows[0]["block_height"]), "epoch": int(rows[0]["epoch_no"])}
        except (KeyError, TypeError, ValueError):
            raise K.ConnectorError("data", f"{http.ep.label}: Kettenspitze nicht lesbar.") from None

    # -- Prüfen -------------------------------------------------------------------------------------------
    def check(self, cfg: K.SourceConfig, secret: K.Secret) -> K.CheckResult:
        stake, extra = self._scope(cfg)
        details: dict[str, Any] = {}
        with self.http(cfg, secret, max_requests=20, deadline_s=60) as http:
            tip = self._tip(http)
            details["chain"] = {"ok": True, "text": f"Cardano erreichbar, Block {tip['height']:,}, Epoche "
                                                    f"{tip['epoch']}".replace(",", ".")}
            bals = self._balances(http, stake, extra)
            ada = next((b for b in bals if b.asset_key == "ADA"), None)
            details["balance"] = {"ok": True, "text": (f"Bestand {ada.qty.normalize():f} ADA" if ada else
                                                       "kein ADA-Bestand") + (f" ({ada.note})" if ada and ada.note
                                                                              else "")}
            if len(bals) > 1:
                details["tokens"] = {"ok": True, "text": f"{len(bals) - 1} native Asset(s) mit Bestand"}
            if stake:
                addrs = self._list(http.post("/account_addresses", {"_stake_addresses": [stake], "_empty": True},
                                             what="Adressen"), "Adressen", http)
                n = sum(len(r.get("addresses") or []) for r in addrs)
                details["addresses"] = {"ok": True, "text": f"Konto {short(stake, 8)}: {n} benutzte Adresse(n)"}
        mode = f"Konto {short(stake, 8)}" if stake else f"{len(extra)} Adresse(n) ohne Stake-Teil"
        return K.CheckResult(True, f"Cardano: {mode} über {http.ep.label} lesbar.", details, balances=bals)

    def _balances(self, http: ChainHttp, stake: str | None, extra: list[str]) -> list[K.Balance]:
        lovelace = 0
        note: list[str] = []
        assets: dict[str, list[Any]] = {}

        def add_asset(r: dict[str, Any]) -> None:
            fp = str(r.get("fingerprint") or "")
            if not _FP.match(fp):
                return
            try:
                q = int(str(r.get("quantity") or "0"))
                dec = int(r.get("decimals") or 0)
            except (TypeError, ValueError):
                return
            cur = assets.setdefault(fp, [0, dec, r.get("asset_name"), r.get("policy_id")])
            cur[0] += q

        if stake:
            info = self._list(http.post("/account_info", {"_stake_addresses": [stake]}, what="Bestand"), "Bestand",
                              http)
            if info:
                r = info[0]
                utxo, avail = int(str(r.get("utxo") or 0)), int(str(r.get("rewards_available") or 0))
                lovelace += utxo + avail
                if avail:
                    note.append(f"inkl. {units(avail, LOVELACE).normalize():f} ADA verfügbare Rewards")
                if r.get("delegated_pool"):
                    note.append(f"delegiert an {short(str(r['delegated_pool']), 8)}")
                if int(str(r.get("deposit") or 0)):
                    note.append(f"Pfand {units(int(str(r['deposit'])), LOVELACE).normalize():f} ADA nicht enthalten")
            for r in self._paged(http, "/account_assets", "Assets", body={"_stake_addresses": [stake]}):
                add_asset(r)
        if extra:
            for r in self._list(http.post("/address_info", {"_addresses": extra}, what="Bestand"), "Bestand", http):
                lovelace += int(str(r.get("balance") or 0))
            for r in self._paged(http, "/address_assets", "Assets", body={"_addresses": extra}):
                add_asset(r)
        out = [K.Balance("ADA", units(lovelace, LOVELACE), "Cardano", "; ".join(note) or None)]
        for fp, (q, dec, name, _policy) in sorted(assets.items()):
            if q:
                out.append(K.Balance(token_key(asset_symbol(name), CHAIN_TAG, fp), units(q, dec),
                                     asset_symbol(name)))
        return out

    # -- Abrufen ------------------------------------------------------------------------------------------
    def fetch(self, cfg: K.SourceConfig, secret: K.Secret, cursor: dict[str, Any] | None) -> K.FetchResult:
        stake, extra = self._scope(cfg)
        cur = dict(cursor or {})
        start = int(cur.get("height") or 0)
        reward_epoch = int(cur.get("reward_epoch") if cur.get("reward_epoch") is not None else -1)
        res = K.FetchResult(complete=True)
        skipped: Counter[str] = Counter()
        owner = stake or (extra[0] if extra else "")
        with self.http(cfg, secret) as http:
            tip = self._tip(http)
            safe = tip["height"] - CONFIRMATIONS
            stopped = None
            listed: dict[str, int] = {}
            details: list[dict[str, Any]] = []
            through = start - 1
            try:
                if start <= safe:
                    self.report("Abruf", 0, None, "Transaktionsliste")
                    rows: list[dict[str, Any]] = []
                    if stake:
                        rows += self._paged(http, "/account_txs", "Transaktionen",
                                            params={"_stake_address": stake, "_after_block_height": start},
                                            order="block_height.asc,tx_hash.asc")
                    if extra:
                        rows += self._paged(http, "/address_txs", "Transaktionen",
                                            body={"_addresses": extra, "_after_block_height": start},
                                            order="block_height.asc,tx_hash.asc")
                    for r in rows:
                        h, height = str(r.get("tx_hash") or ""), r.get("block_height")
                        if not _HASH.match(h) or not isinstance(height, int):
                            skipped["Einträge ohne gültigen Hash/Block"] += 1
                            continue
                        if start <= height <= safe:
                            listed[h] = height
                    order = sorted(listed, key=lambda x: (listed[x], x))
                    through = safe if not order else start - 1
                    for i in range(0, len(order), TX_BATCH):
                        part = order[i:i + TX_BATCH]
                        self.report("Abruf", i, len(order), f"Transaktionsdetails {i + 1}–{i + len(part)}")
                        body = {"_tx_hashes": part, "_inputs": True, "_metadata": False, "_assets": True,
                                "_withdrawals": True, "_certs": True, "_scripts": False, "_bytecode": False}
                        got = {str(t.get("tx_hash")): t for t in self._list(
                            http.post("/tx_info", body, what="Transaktionsdetails"), "Transaktionsdetails", http)}
                        missing = [h for h in part if h not in got]
                        if missing:
                            raise K.ConnectorError("data", f"{http.ep.label}: {len(missing)} Transaktion(en) ohne "
                                                           "Details – der nächste Lauf versucht es erneut.")
                        details += [got[h] for h in part]
                        nxt = order[i + TX_BATCH] if i + TX_BATCH < len(order) else None
                        through = (listed[nxt] - 1) if nxt is not None else safe
                else:
                    through = start - 1
            except Stop as e:
                stopped = str(e)
            if stopped and through < start and start <= safe:
                raise K.ConnectorError("unavailable", f"{stopped} ohne Fortschritt – der nächste Lauf versucht es "
                                                      "erneut.", retry_after_s=600)
            events: list[K.SourceEvent] = []
            for t in details:
                if int(t.get("block_height") or 0) > through:
                    continue
                tx = self._view(stake, set(extra), t, skipped)
                if tx is None:
                    continue
                recs = classify(tx)
                if recs:
                    events.append(event(self.provider, tx.txid, owner, tx.ts, recs, tx.label))
            new_reward_epoch = reward_epoch
            if stake and stopped is None:
                try:
                    revs, new_reward_epoch = self._rewards(http, stake, reward_epoch, tip["epoch"])
                    events += revs
                except Stop as e:
                    stopped = str(e)
            res.events = sorted(events, key=lambda e: e.ts)
            res.skipped = dict(skipped)
            res.complete = stopped is None and through >= safe
            res.cursor = {"v": 1, "height": max(through + 1, start), "reward_epoch": new_reward_epoch}
            res.resume = not res.complete
            if stopped:
                res.warnings.append(f"{stopped} – Fortsetzung ab Block {max(through + 1, start):,}".replace(",", "."))
            try:
                res.balances = self._balances(http, stake, extra)
            except (Stop, K.ConnectorError):
                res.balances = None
            res.coverage = {"mode": "historisch" if start == 0 else f"ab Block {start:,}".replace(",", "."),
                            "from_block": start, "to_block": through, "tip": tip["height"],
                            "confirmations": CONFIRMATIONS, "pages": -(-len(details) // TX_BATCH),
                            "operations": len(events),
                            "provider": http.ep.label, "account": "Stake-Konto" if stake else "Adressmodus",
                            **http.stats()}
        return res

    def _rewards(self, http: ChainHttp, stake: str, after_epoch: int, epoch_now: int) \
            -> tuple[list[K.SourceEvent], int]:
        rows = self._paged(http, "/account_reward_history", "Rewards", body={"_stake_addresses": [stake]})
        out: list[K.SourceEvent] = []
        pending: list[int] = []
        emitted = after_epoch
        seen: Counter[str] = Counter()
        for r in sorted(rows, key=lambda r: (int(r.get("earned_epoch") or 0), str(r.get("type")),
                                             str(r.get("pool_id_bech32") or ""))):
            try:
                earned, spendable = int(r["earned_epoch"]), int(r["spendable_epoch"])
                amount = int(str(r.get("amount") or "0"))
            except (KeyError, TypeError, ValueError):
                continue
            if earned <= after_epoch or not amount:
                continue
            if spendable > epoch_now:
                pending.append(earned)
                continue
            typ = str(r.get("type") or "member")
            pool = str(r.get("pool_id_bech32") or "")
            key = f"reward-{earned}-{typ}-{pool[-8:] or 'x'}"
            k = seen[key]
            seen[key] += 1
            ts = epoch_start(spendable)
            qty = units(amount, LOVELACE)
            tag = INCOME.get(typ)
            rec = Rec(line=0, ts=ts, kind=M.DEPOSIT, in_sym="ADA", in_qty=qty, tag=tag or None,
                      label=f"Staking-Reward Epoche {earned} ({typ})",
                      note=f"Reward Epoche {earned}, verfügbar ab Epoche {spendable}"
                           + (f", Pool {short(pool, 8)}" if pool else ""),
                      review=None if tag else "Erstattung eines Pool-Pfands bzw. unbekannte Reward-Art – prüfen",
                      raw={"chain": self.provider, "stake": stake, "earned_epoch": earned,
                           "spendable_epoch": spendable, "type": typ, "pool": pool or None, "lovelace": str(amount)})
            rec.ext_id = "reward" if k == 0 else f"reward#{k}"
            out.append(K.SourceEvent(f"{self.provider}:{key}:{stake}", ts, [rec], rec.label))
            emitted = max(emitted, earned)
        if pending:
            emitted = min(emitted, min(pending) - 1)
        return out, emitted

    # -- Einordnung ---------------------------------------------------------------------------------------
    def _view(self, stake: str | None, extra: set[str], t: dict[str, Any], skipped: Counter[str]) -> TxView | None:
        h = str(t.get("tx_hash") or "")
        if not _HASH.match(h):
            skipped["Einträge ohne gültigen Hash"] += 1
            return None

        def mine(io: dict[str, Any]) -> bool:
            pa = (io.get("payment_addr") or {}).get("bech32")
            return bool((stake and io.get("stake_addr") == stake) or (pa and pa in extra))

        ins, outs = t.get("inputs") or [], t.get("outputs") or []
        own_in = [i for i in ins if mine(i)]
        own_out = [o for o in outs if mine(o)]
        hints: list[str] = []
        try:
            delta = sum(int(str(o.get("value") or 0)) for o in own_out) - sum(int(str(i.get("value") or 0))
                                                                             for i in own_in)
            fee_l = int(str(t.get("fee") or 0))
            deposit_l = int(str(t.get("deposit") or 0))
            donation_l = int(str(t.get("treasury_donation") or 0))
        except ValueError:
            skipped["Transaktionen mit unlesbaren Beträgen"] += 1
            return None
        withdrawn = sum(int(str(w.get("amount") or 0)) for w in t.get("withdrawals") or []
                        if stake and w.get("stake_addr") == stake)
        all_mine = bool(own_in) and len(own_in) == len(ins)
        if own_in and not all_mine:
            hints.append("Transaktion mit fremden Eingängen (z. B. DEX-Batcher oder gemeinsame Transaktion) – Gebühr "
                         "nicht zugeordnet, Art prüfen")
        fee = fee_l if all_mine else 0
        deposit = deposit_l if all_mine else 0
        ext = delta + fee + deposit - withdrawn
        certs = [c for c in t.get("certificates") or [] if isinstance(c, dict)]
        scripts = any(_is_script((io.get("payment_addr") or {}).get("bech32")) for io in [*ins, *outs])
        moves: list[Move] = []
        if ext:
            moves.append(Move("ADA", units(ext, LOVELACE), "ada"))
        if deposit:
            moves.append(Move("ADA", -units(deposit, LOVELACE), "deposit",
                              note="Pfand (Stake-Registrierung/Governance)" if deposit > 0 else "Pfand-Erstattung"))
            hints.append("Pfand für Stake-Registrierung bzw. Governance (erstattbar) – als Abgang buchen oder "
                         "ignorieren" if deposit > 0 else "Erstattung eines Pfands – als Zugang buchen oder ignorieren")
        if donation_l and all_mine:
            hints.append("Spende an die Treasury enthalten")
        # native Assets je Fingerabdruck
        q: dict[str, int] = defaultdict(int)
        meta: dict[str, tuple[int, str | None, str | None]] = {}
        for sign, group in ((1, own_out), (-1, own_in)):
            for io in group:
                for a in io.get("asset_list") or []:
                    fp = str(a.get("fingerprint") or "")
                    if not _FP.match(fp):
                        hints.append("Asset ohne gültigen Fingerabdruck")
                        continue
                    try:
                        q[fp] += sign * int(str(a.get("quantity") or "0"))
                        meta.setdefault(fp, (int(a.get("decimals") or 0), a.get("asset_name"), a.get("policy_id")))
                    except (TypeError, ValueError):
                        hints.append("Asset-Menge nicht lesbar")
        for fp, qty in sorted(q.items()):
            if not qty:
                continue
            dec, name, _ = meta[fp]
            sym = asset_symbol(name)
            try:
                text = bytes.fromhex(name or "").decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                text = ""
            moves.append(Move(token_key(sym, CHAIN_TAG, fp), units(qty, dec), f"a:{fp}",
                              spam=spam_reason(sym, text) if qty > 0 else None))
        if not moves and not fee and not own_in and not own_out and not withdrawn:
            return None  # z. B. nur Zertifikat eines fremden Absenders
        if withdrawn:
            hints_note = f"Reward-Abhebung {units(withdrawn, LOVELACE).normalize():f} ADA (Umbuchung, kein Zugang)"
            for m in moves:
                m.note = m.note or hints_note
        label = ", ".join(dict.fromkeys(str(c.get("type") or "Zertifikat") for c in certs)) or (
            "Reward-Abhebung" if withdrawn and not ext else None)
        plain = not certs and not scripts and not withdrawn and not t.get("assets_minted")
        raw = {"block": t.get("block_height"), "epoch": t.get("epoch_no"), "fee_lovelace": str(fee_l),
               "deposit_lovelace": str(deposit_l), "withdrawn_lovelace": str(withdrawn) if withdrawn else None,
               "own_inputs": len(own_in), "inputs": len(ins), "own_outputs": len(own_out), "outputs": len(outs),
               "certificates": [str(c.get("type")) for c in certs][:10],
               "assets": {fp: {"policy": meta[fp][2], "name": meta[fp][1], "qty": str(v)} for fp, v in q.items()
                          if v}}
        try:
            ts = datetime.fromtimestamp(int(t.get("tx_timestamp") or 0), UTC)
        except (TypeError, ValueError, OverflowError):
            skipped["Transaktionen ohne Zeitstempel"] += 1
            return None
        return TxView(self.provider, h, ts, moves, fee=units(fee, LOVELACE), fee_asset="ADA", initiated=bool(own_in),
                      plain=plain, hint="; ".join(dict.fromkeys(hints)) or None, label=label,
                      raw={k: v for k, v in raw.items() if v not in (None, [], {})})

