"""Solana – SOL und SPL-Tokens (Token-Programm und Token-2022), nur lesend über JSON-RPC.

Anbieter
    **Öffentlicher RPC der Solana Foundation** (Standard, ohne Key; 100 Anfragen/10 s je IP, 40/10 s je Methode, laut
    Betreiber nicht für Dauerbetrieb) bzw. **Helius** (kostenloser Plan mit API-Key). Alle Abfragen mit
    ``commitment=finalized`` – nur finalisierte Transaktionen (kein Reorg-Risiko).

Abdeckung der Historie
    Eingehende Token-Transfers enthalten oft nur das Token-Konto des Empfängers, nicht die Wallet-Adresse. Portfolia
    fragt deshalb die Signaturen der Wallet **und** aller ihrer Token-Konten ab: aktuelle (``getTokenAccountsByOwner``,
    beide Token-Programme) und frühere, inzwischen geschlossene, die in Transaktionen der Wallet auftauchen (Besitzer
    laut Vor-/Nach-Beständen). Neu entdeckte Konten werden im selben Lauf mit abgefragt.

Bewegungen
    Je Transaktion aus Vor-/Nach-Beständen: SOL = Änderung der Wallet plus ihrer Token-Konten (Miete/Rent bleibt
    Eigentum der Wallet und ist kein Abgang; gewrapptes SOL zählt als SOL), abzüglich der Gebühr, wenn die Wallet
    sie bezahlt hat. Tokens je Mint (``USDC@SOL:<mint>``). Programme außer System-, Token-, ATA-, Compute-Budget- und
    Memo-Programm (DEX, Staking, Bridges …) führen zur Prüfung; Tausch ebenso. NFTs (0 Dezimalstellen, Menge 1)
    werden gezählt, nicht gebucht.

Grenzen
    Native Staking (Stake-Konten, Inflations-Rewards) erscheint nicht als Vorgang der Wallet. Mehrere Bewegungen
    desselben Tokens in einer Transaktion werden saldiert. Token-Konten, die der Wallet nur per Autoritätswechsel
    gehörten und heute einer anderen Adresse gehören, sind nicht auffindbar.
"""

from __future__ import annotations

import re
from collections import Counter
from decimal import Decimal
from typing import Any, ClassVar

from app.datasources import connector as K
from app.datasources.chainhttp import ChainHttp, RpcError, Stop
from app.datasources.wallet import Move, TxView, WalletConnector, classify, event, short, token_key, ts_from_unix, units

LAMPORTS = 9
SIG_PAGE = 1000
CHUNK = 25  # Transaktionen je Block (Fortsetzungspunkt rückt blockweise vor)
TOKEN_PROGRAMS = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
WSOL = "So11111111111111111111111111111111111111112"
SIMPLE_PROGRAMS = {"11111111111111111111111111111111", *TOKEN_PROGRAMS,
                   "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",  # Associated Token Account
                   "ComputeBudget111111111111111111111111111111",
                   "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr", "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo"}
# bekannte Mints (nur zur Beschriftung – die Zuordnung zum Asset bestätigt der Nutzer im Prüf-Stapel)
KNOWN_MINTS = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
               "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
               "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": "JUP",
               "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": "BONK", WSOL: "WSOL"}
_B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_SIG = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{64,90}$")
CFG = {"commitment": "finalized"}


@K.register
class SolanaConnector(WalletConnector):
    provider = "solana"
    label = "Solana (öffentlicher RPC/Helius)"
    chain_label = "Solana"
    native = "SOL"
    endpoints = ("solana", "helius")
    explorer_tx = "https://solscan.io/tx/{}"
    explorer_addr = "https://solscan.io/account/{}"
    explorer_token = "https://solscan.io/token/{}"  # noqa: S105 - Link-Muster
    workers: ClassVar[int] = 2
    limits = ("Native Staking (Stake-Konten, Inflations-Rewards) erscheint nicht als Vorgang – ggf. per CSV ergänzen",
              "Saldo je Asset und Transaktion aus Vor-/Nach-Beständen; mehrere Bewegungen desselben Tokens in einer "
              "Transaktion werden zusammengefasst",
              "NFTs werden gezählt, nicht gebucht; frühere Token-Konten nur, soweit sie in Transaktionen der Wallet "
              "auftauchen",
              "Öffentlicher RPC laut Betreiber nicht für Dauerbetrieb – bei Drosselung Helius-Key hinterlegen")

    def _addr(self, cfg: K.SourceConfig) -> str:
        a = cfg.address or ""
        if not _B58.match(a):
            raise K.ConnectorError("config", "Adresse ungültig – bitte neu eingeben.")
        return a

    # -- RPC ----------------------------------------------------------------------------------------------
    def _token_accounts(self, http: ChainHttp, owner: str) -> list[dict[str, Any]]:
        out = []
        for prog in TOKEN_PROGRAMS:
            res = http.rpc("getTokenAccountsByOwner", [owner, {"programId": prog},
                                                       {"encoding": "jsonParsed", **CFG}], what="Token-Konten")
            for item in (res or {}).get("value") or []:
                info = (((item.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
                amt = info.get("tokenAmount") or {}
                if not _B58.match(str(item.get("pubkey") or "")) or not _B58.match(str(info.get("mint") or "")):
                    continue
                out.append({"pubkey": item["pubkey"], "mint": info["mint"], "amount": str(amt.get("amount") or "0"),
                            "decimals": int(amt.get("decimals") or 0),
                            "lamports": int((item.get("account") or {}).get("lamports") or 0)})
        return out

    def _sigs(self, http: ChainHttp, addr: str, before: str | None, until: str | None) -> list[dict[str, Any]]:
        opts: dict[str, Any] = {"limit": SIG_PAGE, **CFG}
        if before:
            opts["before"] = before
        if until:
            opts["until"] = until
        res = http.rpc("getSignaturesForAddress", [addr, opts], what="Signaturen")
        if not isinstance(res, list):
            raise K.ConnectorError("data", f"{http.ep.label}: Signaturliste nicht lesbar.")
        out = [r for r in res if isinstance(r, dict) and _SIG.match(str(r.get("signature") or ""))]
        if len(out) != len(res):
            raise K.ConnectorError("data", f"{http.ep.label}: ungültige Signatur in der Liste.")
        return out

    def _tx(self, http: ChainHttp, sig: str) -> dict[str, Any] | None:
        try:
            return http.rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                                     **CFG}], what="Transaktion")
        except RpcError as e:
            if e.code in (-32009, -32011, -32004, -32007):  # nicht (mehr) im Langzeitspeicher bzw. ausgelassen
                return None
            raise

    # -- Prüfen -------------------------------------------------------------------------------------------
    def check(self, cfg: K.SourceConfig, secret: K.Secret) -> K.CheckResult:
        w = self._addr(cfg)
        details: dict[str, Any] = {}
        with self.http(cfg, secret, max_requests=10, deadline_s=60) as http:
            slot = http.rpc("getSlot", [CFG], what="Slot")
            details["chain"] = {"ok": True, "text": f"Solana erreichbar, Slot {int(slot):,}".replace(",", ".")}
            balances = self._balances(http, w)
            sol = next((b.qty for b in balances if b.asset_key == "SOL"), Decimal(0))
            details["balance"] = {"ok": True, "text": f"Bestand {sol.normalize():f} SOL (inkl. Miete der "
                                                      "Token-Konten)"}
            details["tokens"] = {"ok": True, "text": f"{len(balances) - 1} Token-Bestände"}
            last = self._sigs(http, w, None, None)[:1]
            details["history"] = {"ok": True, "text": "Historie abrufbar" + (
                f", letzte Signatur {ts_from_unix(last[0].get('blockTime') or 0).strftime('%d.%m.%Y')}"
                if last else " – noch keine Transaktion")}
        return K.CheckResult(True, f"Solana: Adresse {short(w)} über {http.ep.label} lesbar.", details,
                             balances=balances)

    def _balances(self, http: ChainHttp, w: str) -> list[K.Balance]:
        res = http.rpc("getBalance", [w, CFG], what="Bestand")
        lamports = int((res or {}).get("value") or 0)
        tas = self._token_accounts(http, w)
        lamports += sum(t["lamports"] for t in tas)
        out = [K.Balance("SOL", units(lamports, LAMPORTS), "Solana", "inkl. Miete (Rent) der Token-Konten")]
        by_mint: dict[str, tuple[int, int]] = {}
        for t in tas:
            if t["mint"] == WSOL:
                continue  # gewrapptes SOL steckt in den Lamports des Token-Kontos
            amt, dec = by_mint.get(t["mint"], (0, t["decimals"]))
            by_mint[t["mint"]] = (amt + int(t["amount"]), dec)
        for mint, (amt, dec) in sorted(by_mint.items()):
            if dec == 0 and amt <= 1:
                continue  # NFT
            out.append(K.Balance(token_key(KNOWN_MINTS.get(mint, "SPL"), "SOL", mint), units(amt, dec)))
        return out

    # -- Abrufen ------------------------------------------------------------------------------------------
    def fetch(self, cfg: K.SourceConfig, secret: K.Secret, cursor: dict[str, Any] | None) -> K.FetchResult:
        w = self._addr(cfg)
        cur = dict(cursor or {})
        state: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (cur.get("addrs") or {}).items()}
        res = K.FetchResult(complete=True)
        skipped: Counter[str] = Counter()
        with self.http(cfg, secret) as http:
            current = self._token_accounts(http, w)
            known_tas = set(cur.get("tas") or []) | {t["pubkey"] for t in current}
            queue = [w, *sorted(known_tas)]
            processed: set[str] = set()
            events: dict[str, K.SourceEvent] = {}
            stopped = None
            try:
                while queue:
                    a = queue.pop(0)
                    if a in processed:
                        continue
                    processed.add(a)
                    st = state.setdefault(a, {"head": None, "back": None, "done": False})
                    self._address(http, w, a, st, known_tas, events, skipped, res, len(processed),
                                  len(processed) + len(queue))
                    for ta in sorted(known_tas - processed - set(queue)):
                        queue.append(ta)  # im Lauf entdeckte (auch geschlossene) Token-Konten
            except Stop as e:
                stopped = str(e)
            res.events = sorted(events.values(), key=lambda e: (e.ts, e.event_key))
            res.skipped = dict(skipped)
            backfill = stopped is not None or any(not s.get("done") for s in state.values())
            res.complete = not backfill and not res.gaps
            res.resume = not res.complete  # Zeiger decken nur Verarbeitetes ab → immer sicherer Fortsetzungspunkt
            res.more = backfill  # nur bei offenem Erstabruf zügig fortsetzen (Lücken: regulärer nächster Lauf)
            res.cursor = {"v": 1, "addrs": state, "tas": sorted(known_tas)}
            if stopped:
                res.warnings.append(f"{stopped} – Fortsetzung beim nächsten Lauf")
            try:
                res.balances = self._balances(http, w)
            except (Stop, K.ConnectorError):
                res.balances = None
            res.coverage = {"mode": "inkrementell" if cur else "historisch", "addresses": len(processed),
                            "token_accounts": len(known_tas), "operations": len(res.events),
                            "provider": http.ep.label, **http.stats()}
        return res

    def _address(self, http: ChainHttp, w: str, a: str, st: dict[str, Any], known_tas: set[str],
                 events: dict[str, K.SourceEvent], skipped: Counter[str], res: K.FetchResult, n: int,
                 total: int) -> None:
        """Signaturen einer Adresse abarbeiten – in kleinen Blöcken, Zeiger rücken nur über lückenlos verarbeitete
        Signaturen vor: „head“ = neueste verarbeitete (alles darunter bis „back“ ist erledigt), „back“ = älteste
        verarbeitete des Erstabrufs, „done“ = Erstabruf bis zur ersten Signatur der Adresse abgeschlossen."""
        # 1) Neues seit „head“: vom ältesten zum neuesten, „head“ wandert blockweise mit
        if st.get("head"):
            newer: list[dict[str, Any]] = []
            before = None
            while True:
                page = self._sigs(http, a, before, st["head"])
                newer += page
                if len(page) < SIG_PAGE:
                    break
                before = page[-1]["signature"]
            newer.reverse()
            for i in range(0, len(newer), CHUNK):
                chunk = newer[i:i + CHUNK]
                self.report("Transaktionen", i, len(newer), f"neue Vorgänge von {short(a)} ({n}/{total})")
                if not self._process(http, w, chunk, known_tas, events, skipped, res):
                    break  # Lücke: „head“ bleibt vor ihr
                st["head"] = chunk[-1]["signature"]
        # 2) Erstabruf: ältere Seiten ab „back“ (bzw. von ganz oben), „back“ wandert blockweise mit
        while not st.get("done"):
            page = self._sigs(http, a, st.get("back"), None)
            if page and st.get("head") is None:
                st["head"] = page[0]["signature"]
            for i in range(0, len(page), CHUNK):
                chunk = page[i:i + CHUNK]
                self.report("Erstabruf", i, len(page), f"{short(a)}: ältere Vorgänge ({n}/{total})")
                if not self._process(http, w, chunk, known_tas, events, skipped, res):
                    return  # Lücke: „back“ bleibt, nächster Lauf versucht es erneut
                st["back"] = chunk[-1]["signature"]
            if len(page) < SIG_PAGE:
                st["done"] = True

    def _process(self, http: ChainHttp, w: str, sigs: list[dict[str, Any]], known_tas: set[str],
                 events: dict[str, K.SourceEvent], skipped: Counter[str], res: K.FetchResult) -> bool:
        """Transaktionen laden (begrenzt parallel) und einordnen. False, wenn eine nicht abrufbar war (Lücke)."""
        todo = [s["signature"] for s in sigs if f"solana:{s['signature']}:{w}" not in events]
        txs = http.pmap(lambda sig: (sig, self._tx(http, sig)), todo, workers=self.workers)
        ok = True
        for sig, tx in txs:
            if tx is None:
                ok = False
                res.gaps.append(f"Transaktion {short(sig)} beim Anbieter nicht abrufbar – Historie unvollständig")
                continue
            view = self._view(w, sig, tx, known_tas, skipped)
            if view is None:
                continue
            recs = classify(view)
            if recs:
                events[f"solana:{sig}:{w}"] = event(self.provider, sig, w, view.ts, recs, view.label)
        res.gaps[:] = list(dict.fromkeys(res.gaps))[:20]
        return ok

    def _view(self, w: str, sig: str, tx: dict[str, Any], known_tas: set[str], skipped: Counter[str]) \
            -> TxView | None:
        meta = tx.get("meta") or {}
        msg = (tx.get("transaction") or {}).get("message") or {}
        keys = [k.get("pubkey") if isinstance(k, dict) else k for k in msg.get("accountKeys") or []]
        loaded = meta.get("loadedAddresses") or {}
        pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
        if len(keys) < len(pre):
            keys += [*(loaded.get("writable") or []), *(loaded.get("readonly") or [])]
        if not keys or len(pre) != len(post) or len(keys) < len(pre):
            skipped["Transaktionen ohne lesbare Kontenliste"] += 1
            return None
        signers = {k.get("pubkey") for k in msg.get("accountKeys") or [] if isinstance(k, dict) and k.get("signer")}
        # eigene Token-Konten: Besitzer laut Token-Beständen (auch geschlossene) bzw. bereits bekannt
        owned: set[str] = set()
        tok: dict[str, dict[str, Any]] = {}
        for side, entries in (("pre", meta.get("preTokenBalances") or []),
                              ("post", meta.get("postTokenBalances") or [])):
            for e in entries:
                i = int(e.get("accountIndex", -1))
                if not 0 <= i < len(keys):
                    continue
                acct = keys[i]
                if e.get("owner") == w or acct in known_tas:
                    owned.add(acct)
                    if e.get("owner") == w:
                        known_tas.add(acct)
                    mint = str(e.get("mint") or "")
                    ui = e.get("uiTokenAmount") or {}
                    slot = tok.setdefault(mint, {"pre": 0, "post": 0, "dec": int(ui.get("decimals") or 0)})
                    slot[side] += int(ui.get("amount") or 0)
        lam = 0
        for i, k in enumerate(keys[:len(pre)]):
            if k == w or k in owned:
                lam += int(post[i]) - int(pre[i])
        fee = int(meta.get("fee") or 0) if keys[0] == w else 0
        failed = meta.get("err") is not None
        ts = ts_from_unix(tx.get("blockTime") or 0)
        initiated = w in signers or keys[0] == w
        programs = {str(ins.get("programId") or "") for ins in msg.get("instructions") or []}
        plain = programs <= SIMPLE_PROGRAMS
        moves: list[Move] = []
        sol = lam + fee  # Gebühr separat
        if sol and not failed:
            moves.append(Move("SOL", units(sol, LAMPORTS), "sol"))
        for mint, s in sorted(tok.items()):
            if mint == WSOL or failed:
                continue  # gewrapptes SOL ist in den Lamports enthalten
            delta = s["post"] - s["pre"]
            if not delta:
                continue
            if s["dec"] == 0 and abs(delta) == 1:
                skipped["NFT-Bewegungen (nicht gebucht)"] += 1
                continue
            spam = None if mint in KNOWN_MINTS or initiated else "unbekannter Token ohne eigene Aktion"
            moves.append(Move(token_key(KNOWN_MINTS.get(mint, "SPL"), "SOL", mint), units(delta, s["dec"]),
                              f"spl:{mint}", spam=spam))
        if not moves and not fee:
            return None
        label = ", ".join(sorted(p[:8] for p in programs - SIMPLE_PROGRAMS)) or None
        raw = {"slot": tx.get("slot"), "confirmed": "finalized", "fee_lamports": fee, "sol_delta_lamports": lam,
               "token_accounts": sorted(owned)[:10], "programs": sorted(programs)[:10], "failed": failed}
        return TxView(self.provider, sig, ts, moves, fee=units(fee, LAMPORTS), fee_asset="SOL", initiated=initiated,
                      failed=failed, plain=plain, label=("Programm " + label) if label else None, raw=raw)
