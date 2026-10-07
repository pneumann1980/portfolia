"""XRP Ledger – XRP und Trustline-Tokens über öffentliche Full-History-Server (JSON-RPC), nur lesend, ohne Key.

Anbieter
    ``xrplcluster.com`` (Standard) bzw. ``s2.ripple.com:51234`` – beide mit vollständiger Historie. Methoden laut
    XRPL-Dokumentation (``api_version`` 2): ``server_info`` (letzter validierter Ledger, Reserve), ``account_tx``
    (aufsteigend mit ``forward``, Seiten über ``marker``), ``account_info`` und ``account_lines`` (Bestände).

Abruf
    Nur validierte Ledger (``ledger_index_max`` = letzter validierter Ledger laut ``server_info``). Fortsetzungspunkt
    = erster noch nicht vollständig gelesener Ledger: endet ein Lauf mitten in einem Ledger (Budget), wird dieser
    Ledger im nächsten Lauf vollständig erneut abgefragt (bekannte Vorgänge werden erkannt). Beim Erstabruf wird
    geprüft, ob die Historie mit der Kontoeröffnung beginnt – sonst wird eine Lücke angezeigt.

Einordnung (Saldoänderungen statt Betragsfeldern)
    Je Transaktion zählen die tatsächlichen Saldoänderungen des Kontos laut ``meta.AffectedNodes``: ``AccountRoot``
    (XRP in Drops; die Gebühr wird herausgerechnet und separat gebucht) und ``RippleState`` (Trustline-Saldo aus
    Sicht des Kontos, Token = Währung + Gegenpartei/Emittent). Damit sind Teilzahlungen (``tfPartialPayment``),
    DEX-Ausführungen und fehlgeschlagene Transaktionen (``tec…``: nur Gebühr) korrekt. Zahlung/CheckCash/
    AccountDelete gelten als einfache Überweisung; DEX (OfferCreate), AMM, Escrow, Payment Channels, NFTs, MPTs und
    Unbekanntes gehen mit Begründung in die Prüfung. Destination/Source Tags stehen in Notiz und Rohdaten (Abgleich
    mit Börsen-Ein-/Auszahlungen).

Kennungen
    Ereignis ``xrp:<hash>:<adresse>``; Bewegungen ``xrp``, ``t:<währung>.<emittent>``, ``fee``.

Reserve
    Die Kontoreserve (Basis + je Objekt) ist gebundenes Eigentum: sie bleibt im Bestand und wird in der
    Bestandsprüfung als gesperrter Anteil ausgewiesen (Werte laut ``server_info``).
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.datasources import connector as K
from app.datasources.chainhttp import ChainHttp, Stop
from app.datasources.chains.codec import xrpl_decode
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

PAGE = 200
MAX_LINES_PAGES = 20
RIPPLE_EPOCH = 946_684_800  # 2000-01-01T00:00:00Z
CHAIN_TAG = "XRPL"
_HASH = re.compile(r"^[0-9A-F]{64}$")
_PLAIN_TYPES = {"Payment", "CheckCash", "AccountDelete"}
_HINTS = {
    "OfferCreate": "DEX-Angebot (OfferCreate) – Tausch prüfen",
    "OfferCancel": None,
    "AMMDeposit": "Einlage in einen AMM-Pool (LP-Token) – Art prüfen",
    "AMMWithdraw": "Entnahme aus einem AMM-Pool – Art prüfen",
    "AMMCreate": "AMM-Pool angelegt – Art prüfen",
    "AMMBid": "AMM-Auktion – Art prüfen",
    "EscrowCreate": "XRP in Escrow hinterlegt (bleibt Eigentum bis Finish/Cancel) – Art prüfen",
    "EscrowFinish": "Escrow ausgelöst – Art prüfen",
    "EscrowCancel": "Escrow zurückgegeben – Art prüfen",
    "PaymentChannelCreate": "Payment Channel – Art prüfen",
    "PaymentChannelFund": "Payment Channel – Art prüfen",
    "PaymentChannelClaim": "Payment Channel – Art prüfen",
    "NFTokenAcceptOffer": "NFT-Handel – Art prüfen",
    "NFTokenCreateOffer": "NFT-Angebot – Art prüfen",
    "Clawback": "Rückforderung durch den Emittenten (Clawback) – prüfen",
}


class _NotFound(Exception):
    """Konto (noch) nicht auf dem Ledger (``actNotFound``)."""


def _drops(v: Any) -> int:
    s = str(v).strip()
    if not re.fullmatch(r"-?\d{1,20}", s):
        raise ValueError("keine Drops")
    return int(s)


def currency_symbol(code: str) -> str:
    """Anzeige-Symbol einer Währung: Standardcode (3 Zeichen) bzw. 40-Hex-Code als ASCII; AMM-LP-Tokens → „LP“."""
    c = str(code or "")
    if re.fullmatch(r"[0-9A-Fa-f]{40}", c):
        raw = bytes.fromhex(c)
        if raw[0] == 0x03:
            return "LP"
        text = raw.rstrip(b"\x00").decode("ascii", "replace")
        return clean_symbol(text, "TOKEN")
    return clean_symbol(c, "TOKEN")


def _amount(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError):
        raise ValueError("Betrag nicht lesbar") from None


@K.register
class XrplConnector(WalletConnector):
    provider = "xrp"
    label = "XRP Ledger (öffentliche Full-History-Server)"
    chain_label = "XRP Ledger"
    native = "XRP"
    endpoints = ("xrplcluster", "ripple_s2")
    explorer_tx = "https://livenet.xrpl.org/transactions/{}"
    explorer_addr = "https://livenet.xrpl.org/accounts/{}"
    explorer_token = "https://livenet.xrpl.org/token/{}"  # noqa: S105 - Link-Muster
    limits = ("Bewegungen aus den Saldoänderungen validierter Ledger (XRP und Trustline-Tokens); DEX, AMM, Escrow, "
              "Payment Channels und NFTs zur Prüfung",
              "Kontoreserve bleibt im Bestand (als gesperrt ausgewiesen); Multi-Purpose-Tokens (MPT) werden nicht "
              "gebucht, sondern als ungeklärt angezeigt")

    # -- Anbieter-API -------------------------------------------------------------------------------------
    def _cmd(self, http: ChainHttp, method: str, params: dict[str, Any], what: str) -> dict[str, Any]:
        body = {"method": method, "params": [{**params, "api_version": 2}]}
        for attempt in range(4):
            out = http.post("", body, what=what)
            res = out.get("result") if isinstance(out, dict) else None
            if not isinstance(res, dict):
                raise K.ConnectorError("data", f"Unerwartete Antwort von {http.ep.label} ({what}).")
            if res.get("status") != "error" and "error" not in res:
                return res
            err = str(res.get("error") or "")
            if err == "actNotFound":
                raise _NotFound
            if err in ("slowDown", "tooBusy", "noCurrent", "noNetwork", "notSynced"):
                http.throttled += 1
                if attempt < 3 and http._pause(2.0 * (attempt + 1)):
                    continue
                raise K.ConnectorError("rate_limit" if err == "slowDown" else "unavailable",
                                       f"{http.ep.label} meldet „{err}“ ({what}) – der nächste Lauf versucht es "
                                       "erneut.", retry_after_s=120)
            msg = str(res.get("error_message") or err)[:120]
            raise K.ConnectorError("data", f"{http.ep.label} meldet bei {what}: {msg}")
        raise K.ConnectorError("unavailable", f"{http.ep.label} antwortet nicht ({what}).")  # pragma: no cover

    def _server(self, http: ChainHttp) -> dict[str, Any]:
        info = self._cmd(http, "server_info", {}, "Serverstatus").get("info") or {}
        vl = info.get("validated_ledger") or {}
        try:
            seq = int(vl.get("seq"))
        except (TypeError, ValueError):
            raise K.ConnectorError("unavailable", f"{http.ep.label}: kein validierter Ledger gemeldet.") from None
        return {"seq": seq, "complete": str(info.get("complete_ledgers") or ""),
                "reserve_base": vl.get("reserve_base_xrp"), "reserve_inc": vl.get("reserve_inc_xrp")}

    def _addr(self, cfg: K.SourceConfig) -> str:
        a = (cfg.address or "").strip()
        try:
            xrpl_decode(a)
        except ValueError:
            raise K.ConnectorError("config", "XRP-Ledger-Adresse ungültig – bitte neu eingeben.") from None
        return a

    # -- Prüfen -------------------------------------------------------------------------------------------
    def check(self, cfg: K.SourceConfig, secret: K.Secret) -> K.CheckResult:
        addr = self._addr(cfg)
        details: dict[str, Any] = {}
        with self.http(cfg, secret, max_requests=20, deadline_s=60) as http:
            srv = self._server(http)
            details["chain"] = {"ok": True, "text": f"XRP Ledger erreichbar, validierter Ledger {srv['seq']:,}"
                                                    .replace(",", ".") + (f" · Historie {srv['complete']}"
                                                                          if srv["complete"] else "")}
            balances = self._balances(http, addr, srv)
            xrp = balances[0]
            details["balance"] = {"ok": True, "text": f"Bestand {xrp.qty.normalize():f} XRP"
                                                      + (f" ({xrp.note})" if xrp.note else "")}
            if len(balances) > 1:
                details["tokens"] = {"ok": True, "text": f"{len(balances) - 1} Trustline(s) mit Bestand"}
            try:
                last = self._cmd(http, "account_tx", {"account": addr, "ledger_index_min": -1,
                                                      "ledger_index_max": -1, "limit": 1, "forward": False},
                                 "Transaktionen").get("transactions") or []
            except _NotFound:
                last = []
            if last:
                details["history"] = {"ok": True, "text": f"Historie abrufbar, letzte Transaktion "
                                                          f"{self._ts(last[0]).strftime('%d.%m.%Y')}"}
            else:
                details["history"] = {"ok": True, "text": "Historie abrufbar – noch keine Transaktion"}
        return K.CheckResult(True, f"XRP Ledger: Adresse {short(addr)} über {http.ep.label} lesbar.", details,
                             balances=balances)

    def _balances(self, http: ChainHttp, addr: str, srv: dict[str, Any]) -> list[K.Balance]:
        try:
            acc = self._cmd(http, "account_info", {"account": addr, "ledger_index": "validated"}, "Bestand")
        except _NotFound:
            return [K.Balance("XRP", Decimal(0), "XRP", "Konto nicht aktiviert")]
        data = acc.get("account_data") or {}
        try:
            bal = units(_drops(data.get("Balance")), 6)
        except (ValueError, ArithmeticError):
            raise K.ConnectorError("data", f"{http.ep.label}: Bestand nicht lesbar.") from None
        note = None
        try:
            reserve = Decimal(str(srv["reserve_base"])) + Decimal(str(srv["reserve_inc"])) * int(
                data.get("OwnerCount") or 0)
            note = f"davon {reserve.normalize():f} XRP Kontoreserve (gesperrt)"
        except (InvalidOperation, TypeError, ValueError, KeyError):
            pass
        out = [K.Balance("XRP", bal, "XRP", note)]
        marker = None
        for _ in range(MAX_LINES_PAGES):
            q: dict[str, Any] = {"account": addr, "ledger_index": "validated", "limit": 400}
            if marker is not None:
                q["marker"] = marker
            res = self._cmd(http, "account_lines", q, "Trustlines")
            for ln in res.get("lines") or []:
                try:
                    qty = _amount(ln.get("balance"))
                except ValueError:
                    continue
                cur, peer = str(ln.get("currency") or ""), str(ln.get("account") or "")
                if qty <= 0 or not cur or not peer:
                    continue  # eigene Ausgabe (negativ) bzw. leer
                out.append(K.Balance(token_key(currency_symbol(cur), CHAIN_TAG, f"{cur}.{peer}"), qty,
                                     f"{currency_symbol(cur)} (Emittent {short(peer)})"))
            marker = res.get("marker")
            if not marker:
                break
        return out

    # -- Abrufen ------------------------------------------------------------------------------------------
    def fetch(self, cfg: K.SourceConfig, secret: K.Secret, cursor: dict[str, Any] | None) -> K.FetchResult:
        addr = self._addr(cfg)
        cur = dict(cursor or {})
        start = int(cur.get("ledger") or -1)
        res = K.FetchResult(complete=True)
        skipped: Counter[str] = Counter()
        with self.http(cfg, secret) as http:
            srv = self._server(http)
            tip = srv["seq"]
            entries: list[dict[str, Any]] = []
            marker = None
            pages = 0
            stopped = None
            done = False
            first_server_ledger = None
            try:
                while start <= tip:
                    q: dict[str, Any] = {"account": addr, "ledger_index_min": start, "ledger_index_max": tip,
                                         "forward": True, "limit": PAGE}
                    if marker is not None:
                        q["marker"] = marker
                    self.report("Abruf", len(entries), None, f"Transaktionen ab Ledger {max(start, 0):,}"
                                .replace(",", "."))
                    try:
                        page = self._cmd(http, "account_tx", q, "Transaktionen")
                    except _NotFound:
                        page = {"transactions": []}
                    pages += 1
                    if first_server_ledger is None:
                        first_server_ledger = page.get("ledger_index_min")
                    entries += [t for t in page.get("transactions") or [] if isinstance(t, dict)]
                    marker = page.get("marker")
                    if not marker:
                        break
                done = True
            except Stop as e:
                stopped = str(e)
            # vollständig gelesen bis: Spitze (fertig) bzw. Ledger vor dem zuletzt gesehenen (Seitengrenze)
            if done:
                through = tip
            else:
                seen = [self._ledger(t) for t in entries]
                through = (max(seen) - 1) if seen else (start - 1 if start > 0 else -1)
            if stopped and through < max(start, 0):
                raise K.ConnectorError("unavailable", f"{stopped} ohne Fortschritt – der nächste Lauf versucht es "
                                                      "erneut.", retry_after_s=600)
            events = []
            first_tx = None
            for t in entries:
                if not t.get("validated", True) or self._ledger(t) > through:
                    continue
                if first_tx is None:
                    first_tx = t
                tx = self._view(addr, t, skipped)
                if tx is None:
                    continue
                recs = classify(tx)
                if recs:
                    events.append(event(self.provider, tx.txid, addr, tx.ts, recs, tx.label))
            if start <= 0 and entries and first_tx is not None and not self._creates(first_tx, addr):
                res.gaps.append("Historie beginnt nicht mit der Kontoeröffnung (frühere Ledger beim Anbieter nicht "
                                f"verfügbar, erster Ledger des Servers: {first_server_ledger}) – ältere Bewegungen "
                                "fehlen")
            res.events = events
            res.skipped = dict(skipped)
            res.complete = done and stopped is None
            res.cursor = {"v": 1, "ledger": through + 1}
            res.resume = not res.complete and res.cursor is not None
            if stopped:
                res.warnings.append(f"{stopped} – Fortsetzung ab Ledger {through + 1:,}".replace(",", "."))
            try:
                res.balances = self._balances(http, addr, srv)
            except (Stop, K.ConnectorError):
                res.balances = None
            res.coverage = {"mode": "historisch" if start <= 0 else f"ab Ledger {start:,}".replace(",", "."),
                            "from_block": max(start, 0), "to_block": through, "tip": tip, "confirmations": 0,
                            "pages": pages, "operations": len(events), "provider": http.ep.label,
                            "ledgers": srv["complete"], **http.stats()}
        return res

    @staticmethod
    def _ledger(t: dict[str, Any]) -> int:
        tj = t.get("tx_json") or t.get("tx") or {}
        try:
            return int(t.get("ledger_index") or tj.get("ledger_index") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _ts(t: dict[str, Any]) -> datetime:
        iso_v = t.get("close_time_iso")
        if iso_v:
            try:
                return datetime.fromisoformat(str(iso_v).replace("Z", "+00:00")).astimezone(UTC)
            except ValueError:
                pass
        tj = t.get("tx_json") or t.get("tx") or {}
        return datetime.fromtimestamp(RIPPLE_EPOCH + int(tj.get("date") or 0), UTC)

    @staticmethod
    def _creates(t: dict[str, Any], addr: str) -> bool:
        for n in (t.get("meta") or {}).get("AffectedNodes") or []:
            c = n.get("CreatedNode") if isinstance(n, dict) else None
            if c and c.get("LedgerEntryType") == "AccountRoot" and (c.get("NewFields") or {}).get("Account") == addr:
                return True
        return False

    # -- Einordnung ---------------------------------------------------------------------------------------
    def _view(self, addr: str, t: dict[str, Any], skipped: Counter[str]) -> TxView | None:
        tj = t.get("tx_json") or t.get("tx") or {}
        meta = t.get("meta")
        h = str(t.get("hash") or tj.get("hash") or "").upper()
        if not _HASH.match(h) or not isinstance(meta, dict):
            skipped["Einträge ohne gültigen Hash/Metadaten"] += 1
            return None
        ttype = str(tj.get("TransactionType") or "")
        result = str(meta.get("TransactionResult") or "")
        initiated = tj.get("Account") == addr
        hints: list[str] = []
        fee = Decimal(0)
        if initiated:
            try:
                fee = units(_drops(tj.get("Fee") or 0), 6)
            except (ValueError, ArithmeticError):
                hints.append("Gebühr nicht lesbar")
        moves: list[Move] = []
        xrp_drops = 0
        touched = False
        raw_moves: list[dict[str, Any]] = []
        for node in meta.get("AffectedNodes") or []:
            if not isinstance(node, dict) or not node:
                continue
            kind, body = next(iter(node.items()))
            if not isinstance(body, dict):
                continue
            et = body.get("LedgerEntryType")
            final = body.get("FinalFields") or body.get("NewFields") or {}
            prev = body.get("PreviousFields") or {}
            if et == "AccountRoot" and final.get("Account") == addr:
                touched = True
                try:
                    after = _drops(final.get("Balance", 0)) if kind != "DeletedNode" or "Balance" in final else 0
                    before = 0 if kind == "CreatedNode" else _drops(prev["Balance"]) if "Balance" in prev else after
                except (ValueError, KeyError):
                    hints.append("XRP-Saldo der Transaktion nicht lesbar")
                    continue
                xrp_drops += after - before
            elif et == "RippleState":
                low = (final.get("LowLimit") or {}).get("issuer")
                high = (final.get("HighLimit") or {}).get("issuer")
                if addr not in (low, high):
                    continue
                touched = True
                bal_f = final.get("Balance") or {}
                cur = str(bal_f.get("currency") or "")
                try:
                    after = _amount(bal_f.get("value", "0"))
                    before = Decimal(0) if kind == "CreatedNode" else (
                        _amount((prev.get("Balance") or {}).get("value")) if "Balance" in prev else after)
                except ValueError:
                    hints.append("Trustline-Saldo nicht lesbar")
                    continue
                delta = after - before
                if addr == high:
                    delta = -delta  # Saldo steht aus Sicht des „low“-Kontos
                if not delta:
                    continue
                peer = high if addr == low else low
                sym = currency_symbol(cur)
                asset = token_key(sym, CHAIN_TAG, f"{cur}.{peer}")
                moves.append(Move(asset, delta, f"t:{cur}.{peer}"[:100], spam=spam_reason(sym, None)))
                raw_moves.append({"kind": "trustline", "currency": cur, "issuer": peer, "delta": f"{delta:f}"})
            elif et in ("MPToken", "MPTokenIssuance") and (final.get("Account") == addr
                                                           or final.get("Issuer") == addr):
                touched = True
                hints.append("Multi-Purpose-Token (MPT) bewegt – wird nicht gebucht")
        failed = result != "tesSUCCESS"
        if not touched and not initiated:
            return None
        if xrp_drops or initiated:
            # Gebühr ist im Saldo enthalten – herausrechnen und separat buchen
            xrp_move = units(xrp_drops, 6) + fee if not failed else Decimal(0)
            if xrp_move:
                moves.append(Move("XRP", xrp_move, "xrp"))
                raw_moves.append({"kind": "xrp", "delta_drops": str(xrp_drops)})
        if failed:
            moves = []
        tags = {k: tj.get(k) for k in ("DestinationTag", "SourceTag") if tj.get(k) is not None}
        if any(m.asset == "XRP" and 0 < m.qty < Decimal("0.01") for m in moves) and tj.get("Memos") \
                and not initiated:
            hints.append("Kleinstbetrag mit Memo (häufig Werbung/Spam) – zuordnen oder ignorieren")
        hint = _HINTS.get(ttype, None if ttype in _PLAIN_TYPES or ttype in ("TrustSet", "AccountSet", "SetRegularKey",
                                                                             "SignerListSet", "TicketCreate",
                                                                             "DepositPreauth", "OfferCancel")
                          else f"Transaktionstyp {ttype or 'unbekannt'} – Art prüfen")
        if hint and moves:
            hints.insert(0, hint)
        if ttype == "Payment" and not initiated and (int(tj.get("Flags") or 0) & 0x00020000):
            raw_moves.append({"partial_payment": True})
        note_tags = ", ".join(f"{'Destination' if k == 'DestinationTag' else 'Source'} Tag {v}" for k, v in
                              tags.items())
        for m in moves:
            if note_tags and not m.note:
                m.note = note_tags
        raw = {"ledger": self._ledger(t), "type": ttype, "result": result, "account": tj.get("Account"),
               "destination": tj.get("Destination"), **tags, "fee_drops": str(tj.get("Fee")) if initiated else None,
               "moves": raw_moves[:20]}
        return TxView(self.provider, h, self._ts(t), moves, fee=fee, fee_asset="XRP", initiated=initiated,
                      failed=failed, plain=ttype in _PLAIN_TYPES, hint="; ".join(dict.fromkeys(hints)) or None,
                      label=ttype or None, raw=raw)

    def rewind(self, cursor: dict[str, Any] | None, before: datetime) -> dict[str, Any] | None:
        return None  # vollständig neu – bekannte Vorgänge werden erkannt

