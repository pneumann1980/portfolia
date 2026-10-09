"""Kaspa – KAS über die Kaspa-REST-API, KRC-20 über den Kasplex-Indexer; nur lesend, ohne Key.

KAS (api.kaspa.org, Open-Source-Indexer „kaspa-rest-server“)
    ``/addresses/<a>/balance`` und ``/addresses/<a>/full-transactions-page?after=<ms>&limit=500
    &resolve_previous_outpoints=light&acceptance=accepted``: Seiten aufsteigend nach Blockzeit; der Server liefert an
    der Seitengrenze alle Transaktionen derselben Blockzeit mit (keine Lücke bei gleicher Zeit). Fortsetzungspunkt =
    höchste vollständig verarbeitete Blockzeit; nach dem Aufholen werden die letzten 30 Minuten erneut abgefragt
    (späte Akzeptanz), bekannte Vorgänge werden erkannt. Bilanz je Transaktion wie bei Bitcoin (UTXO): Eingang,
    Abgang ohne Wechselgeld mit Gebühr, nur Gebühr; Coinbase (Mining) und fremde Eingänge zur Prüfung.
    KRC-20-Inskriptionen (Commit an eine eigene Skript-Adresse, Reveal zurück) werden als ein Vorgang erkannt: der
    KAS-Anteil ist dann nur Gebühr.

KRC-20 (api.kasplex.org, Indexer der Protokollbetreiber „go-krc20d“)
    ``/v1/krc20/address/<a>/tokenlist`` (Bestände inkl. gesperrter Marktplatz-Mengen) und ``/v1/krc20/oplist?address=``
    (Operationen, 50 je Seite, absteigend nach opScore; ``prev`` liefert aufsteigend Neueres). Gebucht werden nur
    akzeptierte Operationen (``opAccept=1``) ab 10 Minuten Alter. Transfers als Zu-/Abgang; Mint, Marktplatz
    (send), Burn und Unklares zur Prüfung; „list“ sperrt nur (keine Bewegung). Meldet der Indexer „nicht synchron“
    oder ist er nicht erreichbar, bleibt der KAS-Abruf vollständig, KRC-20 wird als konkrete Lücke angezeigt –
    Ergänzung per CSV-Import möglich.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from typing import Any, ClassVar

from app.datasources import connector as K
from app.datasources.chainhttp import ENDPOINTS, ChainHttp, Stop, _HttpProblem
from app.datasources.chains.codec import kaspa_decode
from app.datasources.wallet import Move, TxView, WalletConnector, classify, event, short, token_key, ts_from_unix, units

SOMPI = 8
PAGE = 500
KRC_RETRY_S = 5.0  # Pause vor erneuter Anfrage bei vorübergehendem Indexer-Zustand (403 „unsynced“/„internal error“)
# vorübergehende Zustände des Indexers (HTTP 403 mit dieser Meldung) → Anzeige
KRC_TRANSIENT = {"unsynced": "Indexer vorübergehend nicht synchron",
                 "internal error": "Indexer meldet einen internen Fehler"}
# KRC-20-Indexer in Reihenfolge (Fallback-Kette). Ein zweiter öffentlicher, dokumentierter Indexer ist nicht bekannt
# (Stand Okt. 2026); freie URLs sind aus Sicherheitsgründen nicht vorgesehen. Danach: CSV-Import.
KRC20_INDEXERS = ("kasplex",)
OP_PAGE = 50
OVERLAP_MS = 30 * 60 * 1000
OP_MIN_AGE_MS = 10 * 60 * 1000
COINBASE_SUBNET = "0100000000000000000000000000000000000000"
_TXID = re.compile(r"^[0-9a-f]{64}$")
_TICK = re.compile(r"^[A-Za-z0-9]{1,10}$|^[0-9a-f]{64}$")


class KrcUnsynced(K.ConnectorError):
    """Indexer nicht synchron – vorübergehend, als Lücke (KAS bleibt vollständig)."""

    def __init__(self, message: str) -> None:
        super().__init__("unavailable", message)


@K.register
class KaspaConnector(WalletConnector):
    provider = "kaspa"
    label = "Kaspa (api.kaspa.org, KRC-20: Kasplex)"
    chain_label = "Kaspa"
    native = "KAS"
    endpoints = ("kaspa",)
    explorer_tx = "https://explorer.kaspa.org/txs/{}"
    explorer_addr = "https://explorer.kaspa.org/addresses/{}"
    limits = ("KAS-Historie über den Community-Indexer api.kaspa.org; Coinbase-/Mining-Eingänge zur Prüfung",
              "KRC-20 laut Kasplex-Indexer (Protokollbetreiber); Mint, Marktplatz (send) und Burn zur Prüfung; "
              "KRC-721 (NFTs) nicht erfasst")
    now_ms: ClassVar[Any] = staticmethod(lambda: int(time.time() * 1000))  # Tests: feste Uhr

    def coverage_limits(self, cfg: K.SourceConfig) -> list[str]:
        out = list(self.limits)
        if not self.watch(cfg).tokens:
            out[1] = ("KRC-20 nicht abgerufen (in den Einstellungen abgeschaltet) – KRC-20-Bestände und -Bewegungen "
                      "fehlen; Ergänzung per CSV-Import")
        return out

    def _addr(self, cfg: K.SourceConfig) -> str:
        a = (cfg.address or "").lower()
        try:
            kaspa_decode(a)
        except ValueError:
            raise K.ConnectorError("config", "Kaspa-Adresse ungültig – bitte neu eingeben.") from None
        return a

    def _krc_http(self, cfg: K.SourceConfig, http: ChainHttp, indexer: str = KRC20_INDEXERS[0]) -> ChainHttp:
        # eigenes Zeit-/Anfragebudget nach dem KAS-Teil: KRC-20-Historien mit vielen Mints brauchen sonst viele Etappen
        return ChainHttp(ENDPOINTS[indexer], transport=self.transport, sleep=self.sleep, clock=self.clock,
                         max_requests=max(http.max_requests, 40), deadline_s=self.deadline_s, usage=self.usage)

    # -- KAS ----------------------------------------------------------------------------------------------
    def _balance(self, http: ChainHttp, a: str) -> int:
        body = http.get(f"/addresses/{a}/balance", what="Bestand")
        try:
            return int(body["balance"])
        except (KeyError, TypeError, ValueError):
            raise K.ConnectorError("data", f"{http.ep.label}: Bestand nicht lesbar.") from None

    def _page(self, http: ChainHttp, a: str, after: int) -> list[dict[str, Any]]:
        body = http.get(f"/addresses/{a}/full-transactions-page",
                        {"limit": PAGE, "after": max(after, 1), "resolve_previous_outpoints": "light",
                         "acceptance": "accepted"}, what="Transaktionen")
        if not isinstance(body, list):
            raise K.ConnectorError("data", f"{http.ep.label}: Transaktionsliste nicht lesbar.")
        for t in body:
            if not _TXID.match(str(t.get("transaction_id") or "")) or not isinstance(t.get("block_time"), int):
                raise K.ConnectorError("data", f"{http.ep.label}: ungültige Transaktion in der Liste.")
        return body

    # -- Prüfen -------------------------------------------------------------------------------------------
    def check(self, cfg: K.SourceConfig, secret: K.Secret) -> K.CheckResult:
        a = self._addr(cfg)
        details: dict[str, Any] = {}
        balances: list[K.Balance] = []
        with self.http(cfg, secret, max_requests=8, deadline_s=60) as http:
            bal = units(self._balance(http, a), SOMPI)
            balances.append(K.Balance("KAS", bal, "Kaspa"))
            details["balance"] = {"ok": True, "text": f"Bestand {bal.normalize():f} KAS"}
            page = self._page(http, a, 1)
            details["history"] = {"ok": True, "text": "Historie abrufbar" + (
                f", erste Transaktion {ts_from_unix(min(t['block_time'] for t in page) // 1000).strftime('%d.%m.%Y')}"
                if page else " – noch keine Transaktion")}
            if self.watch(cfg).tokens:
                krc = self._krc_http(cfg, http)
                try:
                    synced, toks = self._krc_state(krc, a)
                    balances += toks
                    details["krc20"] = {"ok": synced, "text": (f"{len(toks)} KRC-20-Bestände" if synced else
                                                               "Kasplex-Indexer meldet „nicht synchron“ – KRC-20 "
                                                               "evtl. unvollständig")}
                except (K.ConnectorError, Stop) as e:
                    kind = getattr(e, "kind", "unavailable")
                    details["krc20"] = {"ok": False, "kind": kind,
                                        "text": f"KRC-20 nicht abrufbar ({K.ERROR_KINDS.get(kind, kind)}): "
                                                f"{getattr(e, 'message', e)} – KAS ist davon nicht betroffen"}
                finally:
                    krc.close()
        return K.CheckResult(True, f"Kaspa: Adresse {short(a, 10)} über {http.ep.label} lesbar.", details,
                             balances=balances)

    # -- Abrufen ------------------------------------------------------------------------------------------
    def fetch(self, cfg: K.SourceConfig, secret: K.Secret, cursor: dict[str, Any] | None) -> K.FetchResult:
        a = self._addr(cfg)
        cur = dict(cursor or {})
        after = int(cur.get("after", 0) or 0)
        res = K.FetchResult(complete=True)
        skipped: Counter[str] = Counter()
        with self.http(cfg, secret) as http:
            txs: list[dict[str, Any]] = []
            done_through = after
            caught_up = False
            stopped = None
            try:
                pos = after
                while True:
                    since = ts_from_unix(max(pos, 1) // 1000)
                    self.report("KAS", len(txs), None, f"Transaktionen ab {since:%d.%m.%Y}")
                    page = self._page(http, a, pos)
                    txs += page
                    if page:
                        pos = max(t["block_time"] for t in page)
                        done_through = pos
                    if len(page) < PAGE:
                        caught_up = True
                        break
            except Stop as e:
                stopped = str(e)
            events = self._kas_events(a, txs, skipped)
            res.events = events
            new_after = max(done_through - OVERLAP_MS, after) if caught_up else done_through
            res.cursor = {"v": 1, "after": new_after, "krc20": cur.get("krc20") or {}}
            res.coverage = {"mode": "historisch" if not after else "inkrementell", "operations": len(events),
                            "provider": http.ep.label, "kas_until_ms": done_through}
            bal: list[K.Balance] | None = None
            if stopped is None:
                try:
                    bal = [K.Balance("KAS", units(self._balance(http, a), SOMPI), "Kaspa")]
                except Stop:
                    bal = None  # Budget erschöpft: Historie vollständig, Bestand beim nächsten Lauf
            # KRC-20: eigener Fortsetzungspunkt – ein Ausfall blockiert KAS nicht, wird aber als Lücke gezeigt
            krc_more = False
            if self.watch(cfg).tokens and stopped is None:
                krc_more = self._krc_fetch(cfg, http, a, cur, res, bal, skipped)
            res.balances = bal
            res.skipped = dict(skipped)
            res.complete = stopped is None and not res.gaps and not krc_more
            res.resume = not res.complete
            res.more = stopped is not None or krc_more
            if stopped:
                res.warnings.append(f"{stopped} – Fortsetzung beim nächsten Lauf")
            res.coverage.update(http.stats())
        return res

    def _krc_fetch(self, cfg: K.SourceConfig, http: ChainHttp, a: str, cur: dict[str, Any], res: K.FetchResult,
                   bal: list[K.Balance] | None, skipped: Counter[str]) -> bool:
        """KRC-20 über die Indexer der Fallback-Kette (:data:`KRC20_INDEXERS`); ein Ausfall blockiert KAS nie, wird
        aber als konkrete Lücke mit Fehlerart gezeigt (``coverage.krc20``). Rückgabe: Fortsetzung nötig."""
        errors: list[str] = []
        for indexer in KRC20_INDEXERS:
            krc = self._krc_http(cfg, http, indexer)
            try:
                self.report("KRC-20", 0, None, f"Token-Bestände und -Vorgänge ({krc.ep.label})")
                _synced, toks = self._krc_state(krc, a)
                st, krc_events, more = self._krc_ops(krc, a, dict(cur.get("krc20") or {}), skipped)
                res.events += krc_events
                res.cursor["krc20"] = st
                if bal is not None:
                    bal += toks
                if more:
                    res.warnings.append("KRC-20: Budget des Laufs erreicht – Fortsetzung beim nächsten Lauf")
                res.coverage["krc20"] = {"status": "ok", "indexer": krc.ep.label}
                return more
            except Stop as e:
                res.gaps.append(f"KRC-20: {e} – Fortsetzung beim nächsten Lauf")
                res.coverage["krc20"] = {"status": "budget", "indexer": krc.ep.label}
                return True
            except K.ConnectorError as e:
                errors.append(e.message)
                res.coverage["krc20"] = {"status": e.kind, "indexer": krc.ep.label, "message": e.message[:300]}
            finally:
                res.coverage["krc20_requests"] = res.coverage.get("krc20_requests", 0) + krc.requests
                krc.close()
        kind = res.coverage.get("krc20", {}).get("status", "unavailable")
        dns = any("DNS" in e for e in errors)
        res.gaps.append(f"KRC-20 nicht abrufbar ({K.ERROR_KINDS.get(kind, kind)}: {'; '.join(errors)}) – KAS ist "
                        "vollständig, KRC-20-Bewegungen fehlen in diesem Lauf; Ergänzung per CSV-Import möglich"
                        + ("; tritt das wiederholt auf: Namensauflösung von api.kasplex.org im Container prüfen "
                           "(DNS des Servers, Werbe-/DNS-Filter wie Pi-hole oder AdGuard)" if dns else ""))
        return False

    def _kas_events(self, a: str, txs: list[dict[str, Any]], skipped: Counter[str]) -> list[K.SourceEvent]:
        uniq: dict[str, dict[str, Any]] = {}
        for t in txs:
            if t.get("is_accepted") is False:
                skipped["nicht akzeptierte Transaktionen"] += 1
                continue
            uniq.setdefault(t["transaction_id"], t)
        own = self._own_scripts(a, list(uniq.values()))
        events = []
        for tid, t in sorted(uniq.items(), key=lambda kv: (kv[1]["block_time"], kv[0])):
            view = self._view(a, own, t)
            if view is None:
                continue
            recs = classify(view)
            if recs:
                events.append(event(self.provider, tid, a, view.ts, recs, view.label))
        return events

    @staticmethod
    def _own_scripts(a: str, txs: list[dict[str, Any]]) -> set[str]:
        """Skript-Adressen (P2SH) eigener KRC-20-Inskriptionen: im Commit aus eigenen Mitteln angelegt und im Reveal
        mit Rückfluss an die eigene Adresse ausgegeben."""
        created: set[str] = set()
        for t in txs:
            ins = t.get("inputs") or []
            if ins and all(i.get("previous_outpoint_address") == a for i in ins):
                created |= {o.get("script_public_key_address") for o in t.get("outputs") or []
                            if str(o.get("script_public_key_address") or "").startswith("kaspa:p")}
        returned: set[str] = set()
        for t in txs:
            if any(o.get("script_public_key_address") == a for o in t.get("outputs") or []):
                returned |= {i.get("previous_outpoint_address") for i in t.get("inputs") or []
                             if str(i.get("previous_outpoint_address") or "").startswith("kaspa:p")}
        return created & returned

    def _view(self, a: str, own: set[str], t: dict[str, Any]) -> TxView | None:
        mine = {a, *own}
        ins = t.get("inputs") or []
        outs = t.get("outputs") or []
        unresolved = any(i.get("previous_outpoint_address") is None or i.get("previous_outpoint_amount") is None
                         for i in ins)
        ins_ours = sum(int(i.get("previous_outpoint_amount") or 0) for i in ins
                       if i.get("previous_outpoint_address") in mine)
        outs_ours = sum(int(o.get("amount") or 0) for o in outs if o.get("script_public_key_address") in mine)
        if not ins_ours and not outs_ours:
            return None
        fee = (sum(int(i.get("previous_outpoint_amount") or 0) for i in ins) - sum(int(o.get("amount") or 0)
                                                                                   for o in outs)) if ins else 0
        script = any(str(x).startswith("kaspa:p") for x in [*(i.get("previous_outpoint_address") for i in ins),
                                                             *(o.get("script_public_key_address") for o in outs)])
        krc = bool(own & {*(i.get("previous_outpoint_address") for i in ins),
                          *(o.get("script_public_key_address") for o in outs)})
        raw = {"block_time_ms": t.get("block_time"), "accepted": t.get("is_accepted"), "fee_sompi": fee,
               "in_ours_sompi": ins_ours, "out_ours_sompi": outs_ours,
               "accepting_block_blue_score": t.get("accepting_block_blue_score")}
        ts = ts_from_unix(int(t["block_time"]) // 1000)
        coinbase = not ins or t.get("subnetwork_id") == COINBASE_SUBNET
        if coinbase:
            return TxView(self.provider, t["transaction_id"], ts, [Move("KAS", units(outs_ours, SOMPI), "in")],
                          hint="Coinbase-Ausgang (Mining) – Art prüfen", raw=raw)
        if unresolved:
            return TxView(self.provider, t["transaction_id"], ts,
                          [Move("KAS", units(outs_ours - ins_ours, SOMPI), "in" if outs_ours > ins_ours else "out")],
                          hint="Eingänge beim Anbieter nicht auflösbar – Beträge und Gebühr prüfen", raw=raw)
        if ins_ours == 0:
            hint = "Eingang von einer Skript-Adresse (z. B. KRC-20/Marktplatz) – Art prüfen" if script else None
            return TxView(self.provider, t["transaction_id"], ts, [Move("KAS", units(outs_ours, SOMPI), "in")],
                          hint=hint, raw=raw)
        foreign = any(i.get("previous_outpoint_address") not in mine for i in ins)
        if not foreign:
            sent = ins_ours - outs_ours - fee
            label = "KRC-20 (Commit/Reveal)" if krc else None
            if sent < 0:
                return TxView(self.provider, t["transaction_id"], ts, [Move("KAS", units(-sent, SOMPI), "in")],
                              hint="Bilanz unerwartet – prüfen", raw=raw)
            hint = None
            if not sent:
                hint = ("KRC-20-Vorgang (Commit/Reveal) – KAS-Anteil nur Gebühr" if krc else
                        "Umbuchung an die eigene Adresse (z. B. UTXO-Zusammenfassung) – nur Gebühr")
            elif script and not krc:
                hint = "Abgang an eine Skript-Adresse (z. B. KRC-20-Commit, Marktplatz) – Art prüfen"
            moves = [Move("KAS", -units(sent, SOMPI), "out")] if sent else []
            return TxView(self.provider, t["transaction_id"], ts, moves, fee=units(fee, SOMPI), fee_asset="KAS",
                          initiated=True, hint=hint, label=label, raw=raw)
        net = outs_ours - ins_ours
        return TxView(self.provider, t["transaction_id"], ts, [Move("KAS", units(net, SOMPI), "in" if net > 0 else
                                                                      "out")], initiated=True,
                      hint="Transaktion mit fremden Eingängen – Saldo ohne Gebührenanteil, bitte prüfen", raw=raw)

    # -- KRC-20 -------------------------------------------------------------------------------------------
    @staticmethod
    def _krc_get(krc: ChainHttp, path: str, params: dict[str, Any] | None, what: str) -> dict[str, Any]:
        """Anfrage an den KRC-20-Indexer mit Auswertung seiner Statusmeldungen.

        go-krc20d (Kasplex, API v1) antwortet auf Anwendungsfehler mit **HTTP 403** und einer Meldung im JSON-Rumpf
        (Quelltext ``api/v1op.go``, ``v1address.go``, ``v1info.go``): ``unsynced`` (Indexer hinter dem Netz, > 99
        DAA ≈ 10 s), ``internal error`` (Speicherfehler), ``address invalid``, ``tick invalid``, ``data expired``
        (ältere Daten nicht mehr vorgehalten). Kasplex verlangt keinen Schlüssel – ein 403 ist nie ein
        Schlüsselproblem. Vorübergehende Zustände werden im Lauf bis zu zweimal nach kurzer Pause wiederholt."""
        for attempt in range(3):
            body = krc.get(path, params, what=what, allow_status=(403,))
            if not isinstance(body, _HttpProblem):
                break
            msg = (body.text or "").strip().lower()
            if body.blocked:
                raise K.ConnectorError("forbidden", f"{krc.ep.label} verweigert {what}: {body.blocked} (HTTP 403) – "
                                                    "später erneut versuchen.")
            if msg in KRC_TRANSIENT:
                if path == "/info" and msg == "unsynced":
                    return {"message": "unsynced", "result": None}  # Zustand, kein Fehler – Aufrufer entscheidet
                if attempt < 2 and krc.backoff(KRC_RETRY_S * (attempt + 1)):
                    continue
                raise K.ConnectorError("unavailable", f"{krc.ep.label}: {KRC_TRANSIENT[msg]} (HTTP 403 „{msg}“, "
                                                      f"{what}) – der nächste Lauf versucht es erneut.")
            if msg == "address invalid":
                raise K.ConnectorError("config", f"{krc.ep.label} lehnt die Adresse ab (HTTP 403 „address invalid“, "
                                                 f"{what}) – Adresse prüfen.")
            if msg == "data expired":
                raise K.ConnectorError("no_data", f"{krc.ep.label} hält diese älteren Vorgänge nicht mehr vor "
                                                  f"(HTTP 403 „data expired“, {what}) – ältere KRC-20-Vorgänge per "
                                                  "CSV-Import ergänzen.")
            raise K.ConnectorError("forbidden", f"{krc.ep.label} verweigert {what} (HTTP 403"
                                                + (f" „{body.text}“" if body.text else "") + ") – der Indexer "
                                                "verlangt keinen Schlüssel; die Anfrage wurde abgelehnt.")
        if not isinstance(body, dict):
            raise K.ConnectorError("data", f"{krc.ep.label}: Antwort nicht lesbar ({what}).")
        msg = str(body.get("message") or "")
        if msg not in ("successful", "synced", "unsynced") and not isinstance(body.get("result"), list | dict):
            raise K.ConnectorError("data", f"{krc.ep.label}: {msg[:80] or 'unerwartete Antwort'} ({what}).")
        return body

    def _krc_state(self, krc: ChainHttp, a: str) -> tuple[bool, list[K.Balance]]:
        info = self._krc_get(krc, "/info", None, "Status")
        synced = str(info.get("message") or "") != "unsynced"
        if not synced:
            # Indexer nicht synchron: seit API 3.x lehnt er dann jede Abfrage mit 403 ab – kurz warten, sonst Lücke
            for wait in (KRC_RETRY_S, 2 * KRC_RETRY_S):
                if not krc.backoff(wait):
                    break
                info = self._krc_get(krc, "/info", None, "Status")
                synced = str(info.get("message") or "") != "unsynced"
                if synced:
                    break
            if not synced:
                raise KrcUnsynced(f"{krc.ep.label} meldet „nicht synchron“ (Indexer hinter dem Netz) – KRC-20-"
                                  "Bewegungen können fehlen; der nächste Lauf versucht es erneut")
        out: list[K.Balance] = []
        nxt: str | None = None
        for _ in range(20):
            body = self._krc_get(krc, f"/krc20/address/{a}/tokenlist", {"next": nxt} if nxt else None,
                                 "KRC-20-Bestände")
            for r in body.get("result") or []:
                ident = str(r.get("ca") or r.get("tick") or "")
                if not _TICK.match(ident):
                    continue
                try:
                    dec = int(r.get("dec") or 8)
                    qty = units(int(r.get("balance") or 0) + int(r.get("locked") or 0), dec)
                except (TypeError, ValueError):
                    continue
                out.append(K.Balance(token_key(r.get("tick") or "KRC20", "KAS", ident.upper() if len(ident) <= 10
                                               else ident), qty, None,
                                     "inkl. für Marktplatz gesperrter Menge" if int(r.get("locked") or 0) else None))
            nxt = body.get("next")
            if not body.get("result") or not nxt or len(body.get("result") or []) < OP_PAGE:
                break
        return synced, out

    def _dec(self, krc: ChainHttp, tick: str, cache: dict[str, int]) -> int:
        if tick not in cache:
            body = self._krc_get(krc, f"/krc20/token/{tick}", None, "Token-Angaben")
            res = body.get("result") or [{}]
            info = res[0] if isinstance(res, list) and res else res
            cache[tick] = int((info or {}).get("dec") or 8)
        return cache[tick]

    def _krc_ops(self, krc: ChainHttp, a: str, st: dict[str, Any], skipped: Counter[str]) \
            -> tuple[dict[str, Any], list[K.SourceEvent], bool]:
        """Operationen der Adresse: Neues aufsteigend ab „head“ (prev), Erstabruf absteigend ab „back“ (next).
        Zeiger rücken seitenweise vor; Operationen jünger als 10 Minuten bleiben für den nächsten Lauf."""
        st = {"head": st.get("head"), "back": st.get("back"), "done": bool(st.get("done")),
              "dec": dict(st.get("dec") or {})}
        events: dict[str, K.SourceEvent] = {}
        limit_ms = self.now_ms() - OP_MIN_AGE_MS
        more = False
        try:
            if st["head"]:
                while True:
                    body = self._krc_get(krc, "/krc20/oplist", {"address": a, "prev": st["head"]}, "KRC-20-Vorgänge")
                    ops = sorted(body.get("result") or [], key=lambda o: int(o.get("opScore") or 0))
                    ready = [o for o in ops if int(o.get("mtsAdd") or 0) <= limit_ms]
                    for o in ready:
                        self._op(krc, a, o, st["dec"], events, skipped)
                        st["head"] = str(o.get("opScore"))
                    if len(ops) < OP_PAGE or len(ready) < len(ops):
                        break
            while not st["done"]:
                params = {"address": a, **({"next": st["back"]} if st["back"] else {})}
                body = self._krc_get(krc, "/krc20/oplist", params, "KRC-20-Vorgänge")
                ops = sorted(body.get("result") or [], key=lambda o: -int(o.get("opScore") or 0))
                for o in ops:
                    if int(o.get("mtsAdd") or 0) > limit_ms:
                        continue  # zu jung – kommt über „head“ im nächsten Lauf
                    if st["head"] is None or int(o.get("opScore") or 0) > int(st["head"]):
                        st["head"] = str(o.get("opScore"))
                    self._op(krc, a, o, st["dec"], events, skipped)
                if ops:
                    st["back"] = str(ops[-1].get("opScore"))
                if len(ops) < OP_PAGE:
                    st["done"] = True
        except Stop:
            more = True  # Zeiger stehen auf vollständig verarbeiteten Seiten – nächster Lauf setzt dort fort
        return st, sorted(events.values(), key=lambda e: (e.ts, e.event_key)), more

    def _op(self, krc: ChainHttp, a: str, o: dict[str, Any], dec_cache: dict[str, int],
            events: dict[str, K.SourceEvent], skipped: Counter[str]) -> None:
        h = str(o.get("hashRev") or "")
        if not _TXID.match(h):
            skipped["KRC-20-Operationen ohne gültige Transaktions-ID"] += 1
            return
        if str(o.get("opAccept") or "") != "1":
            skipped["abgelehnte KRC-20-Operationen"] += 1
            return
        op = str(o.get("op") or "").lower()
        ident = str(o.get("ca") or o.get("tick") or "")
        if not _TICK.match(ident):
            skipped["KRC-20-Operationen ohne gültiges Token"] += 1
            return
        tick = ident.upper() if len(ident) <= 10 else ident
        frm, to = str(o.get("from") or "").lower(), str(o.get("to") or "").lower()
        if op == "list" or (frm == a and to == a):
            skipped["KRC-20-Sperren/eigene Übertragungen (keine Bewegung)"] += 1
            return
        if op == "deploy" and not (to == a and int(o.get("pre") or 0)):
            skipped["KRC-20-Deploys ohne Zuteilung"] += 1
            return
        try:
            raw_amt = int(o.get("amt") or (o.get("pre") if op == "deploy" else 0) or 0)
            dec = int(o.get("dec")) if op == "deploy" and o.get("dec") else self._dec(krc, ident, dec_cache)
        except (TypeError, ValueError):
            skipped["KRC-20-Operationen ohne lesbare Menge"] += 1
            return
        if not raw_amt:
            return
        asset = token_key(str(o.get("tick") or "KRC20"), "KAS", tick)
        qty = units(raw_amt, dec)
        hint = {"mint": "KRC-20-Mint – Erwerb gegen Gebühr, Art prüfen",
                "send": "KRC-20-Marktplatz (send) – Gegenleistung in KAS prüfen",
                "burn": "KRC-20-Burn – Abgang ohne Gegenwert, prüfen",
                "deploy": "KRC-20-Deploy mit Vorabzuteilung – Art prüfen"}.get(op)
        if op not in ("transfer", "mint", "send", "burn", "deploy"):
            hint = f"KRC-20-Operation „{op[:20]}“ – Art prüfen"
        moves = []
        if to == a:
            moves.append(Move(asset, qty, f"t:{tick[:20]}:in"))
        if frm == a:
            moves.append(Move(asset, -qty, f"t:{tick[:20]}:out"))
        if not moves:
            return
        ts = ts_from_unix(int(o.get("mtsAdd") or 0) // 1000)
        raw = {"op": op, "tick": tick, "amt": str(raw_amt), "dec": dec, "from": frm, "to": to,
               "opScore": str(o.get("opScore")), "fee_sompi": str(o.get("feeRev") or ""), "indexer": "kasplex"}
        view = TxView(self.provider, h, ts, moves, hint=hint, label=f"KRC-20 {op}", raw=raw)
        recs = classify(view)
        if recs:
            events[f"krc:{h}"] = K.SourceEvent(f"{self.provider}:krc20-{h}:{a}", ts, recs, f"KRC-20 {op}")
