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

import hashlib
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar

from app.csvimport import model as M
from app.csvimport.model import Rec
from app.datasources import connector as K
from app.datasources.chainhttp import ENDPOINTS, ChainHttp, Stop
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
ROW_STEPS = (100, 50, 20, 10, 5, 1)  # Zeilen je Anfrage: Rückfallstufen, wenn eine Route weniger zulässt
_ROW_CAP: dict[str, int] = {}  # Endpunkt → zuletzt zulässige Zeilenzahl (je Prozess; spart Fehlversuche)
# System.Account = twox128("System") ‖ twox128("Account") – feste Speicher-Präfix aller Substrate-Chains
_SYSTEM_ACCOUNT = "26aa394eea5630e07c48ae0c9558cef7b99d880ec681799c0cf30e8886371da9"
MAX_PAGES = 100  # 10.000 Einträge je Liste und Lauf – danach Fortsetzung im nächsten Lauf
MIN_WINDOW = 2_000  # Blöcke (rund 3,5 h): ein Fensterlimit darunter ist keine Breiten-, sondern eine Rückblick-Grenze
_WINDOW_CAP: dict[str, int] = {}  # Endpunkt:Netz → zuletzt zulässige Fensterbreite in Blöcken (je Prozess)
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


class _RowReduced(Exception):
    """Die Route lässt weniger Zeilen je Anfrage zu: eine laufende Seitenfolge muss mit neuer Größe neu beginnen."""


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
        self.notes: list[str] = []
        self.stopped: str | None = None
        self.requests = 0
        self.gap_from: int | None = None  # Rückblick-Grenze: frühester abrufbarer Block (davor fehlt der Verlauf)


@K.register
class PolkadotConnector(WalletConnector):
    provider = "polkadot"
    label = "Polkadot (Subscan via PubFi bzw. direkt)"
    chain_label = "Polkadot"
    native = "DOT"
    endpoints = ("pubfi", "subscan")
    # öffentliche RPCs (Endpunkt, Anzeige) für den Bestand, wenn der Anbieter nichts liefert (peaq: eigener Weg)
    rpc_nets: ClassVar[tuple[tuple[str, str], ...]] = (("polkadot_rpc", "Relay Chain"),
                                                       ("polkadot_assethub_rpc", "Asset Hub"))
    # Netzparameter (Unterklassen, z. B. peaq): Netze (Subscan-Name, Kürzel, Anzeige), Dezimalstellen, SS58-Präfix
    nets: ClassVar[tuple[tuple[str, str, str], ...]] = NETS
    decimals: ClassVar[int] = DOT_DECIMALS
    ss58_prefix: ClassVar[int] = 0
    token_tag: ClassVar[str] = ASSET_HUB_TAG
    rewards_optional: ClassVar[bool] = False  # Reward-Route nicht für jedes Netz belegt → Fehler nur als Hinweis
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

    @staticmethod
    def _row(http: ChainHttp) -> int:
        return min(ROW, _ROW_CAP.get(http.ep.id, ROW))

    def _call(self, http: ChainHttp, route: str, body: dict[str, Any], what: str) -> Any:
        """Eine Route aufrufen. Lehnt der Anbieter die Zeilenzahl ab (PubFi „row_limit_exceeded“), wird die nächst
        kleinere Stufe versucht und gemerkt; eine Seitenfolge (``page`` > 0) beginnt dann neu (:class:`_RowReduced`)."""
        while True:
            try:
                out = http.post(self._path(http, route), body, what=what)
                break
            except K.ConnectorError as e:
                cur = int(body.get("row") or 0)
                lower = next((r for r in ROW_STEPS if r < cur), None)
                if e.kind != "row_limit" or lower is None:
                    if e.kind == "row_limit":
                        raise K.ConnectorError("scope", f"{http.ep.label} lehnt selbst eine einzelne Zeile je Anfrage "
                                                        f"ab ({what}) – Konto bzw. Tarif beim Anbieter prüfen oder "
                                                        "„Subscan direkt“ wählen.") from None
                    raise
                _ROW_CAP[http.ep.id] = lower
                if int(body.get("page") or 0):
                    raise _RowReduced from None
                body = {**body, "row": lower}
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
            prefix, acc = ss58_decode(a)
        except ValueError:
            raise K.ConnectorError("config", f"{self.chain_label}-Adresse ungültig – bitte neu eingeben.") from None
        if prefix != self.ss58_prefix:
            raise K.ConnectorError("config", f"Keine {self.chain_label}-Adresse (SS58-Präfix {self.ss58_prefix}).")
        self._me = acc
        return a

    def _is_me(self, v: Any, addr: str) -> bool:
        """Eigenes Konto? Vergleich über den öffentlichen Schlüssel – unabhängig vom SS58-Format der Antwort."""
        if not isinstance(v, str) or not v:
            return False
        if v == addr:
            return True
        try:
            return ss58_decode(v)[1] == getattr(self, "_me", None)
        except ValueError:
            return False

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
        return [(key, sh, label, self.http(cfg, secret, network=key, max_requests=max(n // len(self.nets), 10),
                                           deadline_s=deadline_s)) for key, sh, label in self.nets]

    # -- Prüfen -------------------------------------------------------------------------------------------
    @property
    def rpc_endpoints(self) -> tuple[str, ...]:
        """Endpunkte des RPC-Ausweichwegs (der Dienst prüft damit auch ohne Anbieter-Schlüssel)."""
        return tuple(eid for eid, _label in self.rpc_nets)

    def _rpc_balance(self, addr: str) -> tuple[Decimal, list[str]] | None:
        """Bestand (frei + reserviert) laut öffentlichem RPC über alle Netze – ``System.Account`` per
        ``state_getStorage``; ``None``, wenn kein RPC antwortet. Eine Abfrage je Netz, keine Historie."""
        _prefix, pub = ss58_decode(addr)
        key = "0x" + _SYSTEM_ACCOUNT + hashlib.blake2b(pub, digest_size=16).hexdigest() + pub.hex()
        total, parts = Decimal(0), []
        for eid, label in self.rpc_nets:
            try:
                with ChainHttp(ENDPOINTS[eid], transport=self.transport, sleep=self.sleep, clock=self.clock,
                               max_requests=3, deadline_s=30, usage=self.usage) as http:
                    raw = http.rpc("state_getStorage", [key], what="Bestand laut Kette")
            except (K.ConnectorError, Stop):
                return None
            if raw is None:  # Konto existiert in diesem Netz nicht
                continue
            try:
                data = bytes.fromhex(str(raw)[2:])
                free, reserved = int.from_bytes(data[16:32], "little"), int.from_bytes(data[32:48], "little")
            except ValueError:
                return None
            if len(data) < 48:
                return None
            qty = units(free + reserved, self.decimals)
            total += qty
            if qty:
                parts.append(f"{label} {qty.normalize():f}")
        return total, parts

    def check(self, cfg: K.SourceConfig, secret: K.Secret) -> K.CheckResult:
        """Verbindung prüfen. Scheitert der Anbieter (Schlüssel/Tarif/Ausfall), liefert ein öffentlicher RPC wenigstens
        den aktuellen Bestand als Ist-Bestand für den Abgleich (nur Bestand, keine Historie); der Fehler des Anbieters
        bleibt sichtbar."""
        try:
            return self._check_api(cfg, secret)
        except (K.ConnectorError, Stop) as e:
            if not self.rpc_nets or (isinstance(e, K.ConnectorError) and e.kind in ("cancelled", "config")):
                raise
            try:
                fb = self._rpc_balance(self._addr(cfg))
            except (K.ConnectorError, ValueError):
                fb = None
            if fb is None:
                raise
            qty, parts = fb
            reason = getattr(e, "message", None) or str(e)
            text = (f"Bestand laut öffentlichem RPC: {qty.normalize():f} {self.native}"
                    + (f" ({' + '.join(parts)})" if len(parts) > 1 else "") + " (nur Bestand, keine Historie)")
            return K.CheckResult(False, f"{reason} – {text}",
                                 {"api": {"ok": False, "text": reason}, "balance": {"ok": True, "text": text}},
                                 balances=[K.Balance(self.native, qty, self.chain_label, "laut öffentlichem RPC")])

    def _check_api(self, cfg: K.SourceConfig, secret: K.Secret) -> K.CheckResult:
        addr = self._addr(cfg)
        details: dict[str, Any] = {}
        nets = self._nets(cfg, secret, 24, 60)
        balances: list[K.Balance] = []
        try:
            for key, _sh, label, http in nets:
                tip = self._tip(http)
                details[f"chain_{key}"] = {"ok": True, "text": f"{label} erreichbar, Block {tip:,}".replace(",", ".")}
                body = {"address": addr, "row": 1, "page": 0}
                try:
                    last = self._call(http, "v2/scan/transfers", body, "Überweisungen") or {}
                    n = int(last.get("count") or 0) if isinstance(last, dict) else 0
                    text = f"{label}: {n} Überweisung(en) laut Indexer"
                except K.ConnectorError as e:
                    if e.kind != "history_window":
                        raise
                    # kein Gesamtverlauf in einer Anfrage (kostenlose Route): ein Fenster der letzten Blöcke genügt
                    for w in (MIN_WINDOW, MIN_WINDOW // 4, MIN_WINDOW // 20):
                        try:
                            self._call(http, "v2/scan/transfers",
                                       {**body, "block_range": f"{max(tip - w, 0)}-{tip}"}, "Überweisungen")
                            break
                        except K.ConnectorError as e2:
                            if e2.kind != "history_window" or w == MIN_WINDOW // 20:
                                raise
                    text = f"{label}: Verlauf nur in Blockfenstern abrufbar (Anbieter begrenzt die Breite)"
                details[f"history_{key}"] = {"ok": True, "text": text}
            balances = self._balances(nets, addr)
        finally:
            for *_rest, http in nets:
                http.close()
        main = balances[0] if balances else None
        if main is not None:
            details["balance"] = {"ok": True, "text": f"Bestand {main.qty.normalize():f} {self.native}"
                                                      + (f" ({main.note})" if main.note else "")}
        return K.CheckResult(True, f"{self.chain_label}: Adresse {short(addr)} über {nets[0][3].ep.label} lesbar.",
                             details, balances=balances)

    def _balances(self, nets: list[tuple[str, str, str, ChainHttp]], addr: str) -> list[K.Balance]:
        total = Decimal(0)
        parts: list[str] = []
        locked: dict[str, Decimal] = defaultdict(Decimal)
        tokens: dict[str, list[Any]] = {}
        for _key, _sh, label, http in nets:
            data = self._call(http, "v2/scan/account/tokens", {"address": addr, "row": self._row(http), "page": 0},
                              "Bestand") or {}
            rows = data.get("list") if isinstance(data, dict) else None
            for r in rows or []:
                if not isinstance(r, dict):
                    continue
                sym = str(r.get("symbol") or "")
                try:
                    dec = int(r.get("decimals") if r.get("decimals") is not None else self.decimals)
                    bal = amount(r.get("balance"), dec)
                except (TypeError, ValueError):
                    continue
                uid = str(r.get("unique_id") or sym)
                if sym == self.native and uid in (self.native, "", "native"):
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
        out = [K.Balance(self.native, total, self.chain_label, note)]
        for uid, (qty, sym) in sorted(tokens.items()):
            out.append(K.Balance(token_key(sym, self.token_tag, uid), qty, sym))
        return out

    # -- Abrufen ------------------------------------------------------------------------------------------
    def _list(self, http: ChainHttp, route: str, field: str, addr: str, net: _Net, what: str, lo: int, hi: int,
              extra: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], int, bool]:
        """Alle Einträge im Blockbereich ``lo``–``hi`` (aufsteigend): (Einträge, vollständig bis Block, gekappt)."""
        while True:
            row = self._row(http)
            out: list[dict[str, Any]] = []
            try:
                for page in range(MAX_PAGES):
                    body = {"address": addr, "row": row, "page": page, "order": "asc",
                            "block_range": f"{lo}-{hi}", **(extra or {})}
                    self.report("Abruf", len(out), None, f"{net.label}: {what} ab Block {lo:,}".replace(",", "."))
                    data = self._call(http, route, body, what) or {}
                    rows = [r for r in (data.get(field) if isinstance(data, dict) else None) or []
                            if isinstance(r, dict)]
                    out += rows
                    if len(rows) < row:
                        return out, hi, False
            except _RowReduced:
                net.notes.append(f"{http.ep.label} begrenzt die Zeilen je Anfrage – jetzt {self._row(http)}")
                continue  # Seitenfolge mit der kleineren Zeilenzahl neu beginnen
            break
        blocks = [int(r.get("block_num") or 0) for r in out]
        end = max(blocks) - 1 if blocks else lo - 1
        return [r for r, b in zip(out, blocks, strict=True) if b <= end], end, True

    def _window(self, http: ChainHttp, net: _Net, addr: str, lo: int, hi: int) -> tuple[int, bool]:
        """Alle Listen für ``lo``–``hi`` abrufen und im Netz ablegen → (vollständig bis Block, gekappt). Nichts wird
        abgelegt, wenn eine der Listen scheitert (ein Fenster gilt ganz oder gar nicht)."""
        t, t_end, t_cap = self._list(http, "v2/scan/transfers", "transfers", addr, net, "Überweisungen", lo, hi,
                                     {"filter_nft": True})
        x, x_end, x_cap = self._list(http, "v2/scan/extrinsics", "extrinsics", addr, net, "Extrinsics", lo, hi)
        note = ""
        try:
            r, r_end, r_cap = self._list(http, "v2/scan/account/reward_slash", "list", addr, net, "Rewards/Slashes",
                                         lo, hi)
        except K.ConnectorError as e:
            if not self.rewards_optional or e.kind != "data":
                raise
            r, r_end, r_cap = [], x_end, False
            note = (f"Staking-Rewards nicht abrufbar ({e.message}) – Rewards fehlen ggf.; die Bestandsprüfung zeigt "
                    "Differenzen")
        net.transfers += t
        net.extrinsics += x
        net.rewards += r
        if note and note not in net.notes:
            net.notes.append(note)
        for cap, what in ((t_cap, "Überweisungen"), (x_cap, "Extrinsics"), (r_cap, "Rewards/Slashes")):
            if cap and what not in net.capped:
                net.capped.append(what)
        return min(t_end, x_end, r_end), t_cap or x_cap or r_cap

    def _recent_start(self, http: ChainHttp, net: _Net, addr: str, rejected: int) -> int | None:
        """Frühester Block, ab dem die Route das kleinste Fenster zulässt (Rückblick-Grenze der kostenlosen Route):
        Halbierung zwischen dem abgelehnten ``rejected`` und der Spitze, blockgenau (rund 25 Anfragen). ``None``, wenn
        selbst das jüngste Fenster abgelehnt wird – dann gibt es über diese Route keinen Verlauf."""

        def accepted(lo: int) -> bool:
            body = {"address": addr, "row": 1, "page": 0, "block_range": f"{lo}-{min(lo + MIN_WINDOW - 1, net.safe)}"}
            try:
                self._call(http, "v2/scan/transfers", body, "Überweisungen")
            except K.ConnectorError as e:
                if e.kind != "history_window":
                    raise
                return False
            return True

        good = max(net.safe - MIN_WINDOW + 1, rejected + 1)
        if good <= rejected or not accepted(good):
            return None
        bad = rejected
        while good - bad > 1:  # blockgenau: Vorgänge zwischen Grenze und Fensterrand gingen sonst verloren
            mid = (bad + good) // 2
            if accepted(mid):
                good = mid
            else:
                bad = mid
        return good

    def _scan(self, http: ChainHttp, net: _Net, addr: str, width: int | None) -> int | None:
        """Blockbereich des Netzes in Fenstern abrufen. Ohne Fensterlimit ist das ein einziges Fenster (``start`` bis
        Spitze). Meldet der Anbieter ``history_window_exceeded`` (kostenlose PubFi-Route), halbiert sich die Breite bis
        zum zulässigen Wert; sie wird gemerkt. Scheitert schon das kleinste Fenster, ist es eine Rückblick-Grenze → der
        Fehler geht weiter. Rückgabe: zuletzt zulässige Breite (``None`` = unbegrenzt)."""
        wkey = f"{http.ep.id}:{net.key}"
        width = width or _WINDOW_CAP.get(wkey)
        pos = net.start
        while pos <= net.safe:
            hi = net.safe if not width else min(net.safe, pos + width - 1)
            try:
                end, capped = self._window(http, net, addr, pos, hi)
            except K.ConnectorError as e:
                span = hi - pos + 1
                if e.kind != "history_window":
                    raise
                if span <= MIN_WINDOW:  # Rückblick- statt Breitengrenze: frühesten abrufbaren Block suchen
                    first = self._recent_start(http, net, addr, pos)
                    if first is None:
                        raise
                    net.gap_from = first
                    pos = first
                    continue
                width = max(MIN_WINDOW, span // 2)
                _WINDOW_CAP[wkey] = width
                note = f"{http.ep.label} begrenzt den Verlauf je Anfrage – Abruf in Blockfenstern"
                if note not in net.notes:
                    net.notes.append(note)
                continue
            net.through = end
            if capped:
                break
            pos = hi + 1
        return width

    def fetch(self, cfg: K.SourceConfig, secret: K.Secret, cursor: dict[str, Any] | None) -> K.FetchResult:
        """Abruf über den Anbieter. Lässt die kostenlose Route selbst das kleinste Fenster nicht zu (Rückblick-Grenze,
        ``history_window_exceeded``), bleibt wenigstens der Bestand laut öffentlichem RPC als Ist-Bestand – die
        Historie fehlt dann als Lücke (kein Fehler, nichts wird erfunden)."""
        try:
            return self._fetch_api(cfg, secret, cursor)
        except K.ConnectorError as e:
            if e.kind != "history_window" or not self.rpc_nets:
                raise
            try:
                fb = self._rpc_balance(self._addr(cfg))
            except (K.ConnectorError, ValueError):
                fb = None
            if fb is None:
                raise
            qty, parts = fb
            note = (f"{e.message} Bestand laut öffentlichem RPC: {qty.normalize():f} {self.native}"
                    + (f" ({' + '.join(parts)})" if len(parts) > 1 else "") + ". Die Historie ist über diese Route "
                    "nicht abrufbar – Zugänge per CSV-/Koinly-Import oder mit einem Subscan-Schlüssel ergänzen.")
            return K.FetchResult(complete=False, cursor=cursor, gaps=[note],
                                 warnings=["Nur Bestand abgerufen – Verlauf nicht abrufbar"],
                                 balances=[K.Balance(self.native, qty, self.chain_label, "laut öffentlichem RPC")],
                                 coverage={"mode": "nur Bestand laut Kette", "operations": 0, "provider": "RPC"})

    def _fetch_api(self, cfg: K.SourceConfig, secret: K.Secret, cursor: dict[str, Any] | None) -> K.FetchResult:
        addr = self._addr(cfg)
        cur = dict(cursor or {})
        state = dict(cur.get("nets") or {})
        res = K.FetchResult(complete=True)
        skipped: Counter[str] = Counter()
        nets = self._nets(cfg, secret)
        done: list[_Net] = []
        wins: dict[str, int | None] = {}
        try:
            for key, sh, label, http in nets:
                net = _Net(key, sh, label, int((state.get(key) or {}).get("block") or 0))
                net.gap_from = int((state.get(key) or {}).get("from") or 0) or None
                done.append(net)
                try:
                    net.safe = self._tip(http)
                    if net.start > net.safe:
                        net.through = net.start - 1
                        continue
                    wins[key] = self._scan(http, net, addr, int((state.get(key) or {}).get("win") or 0) or None)
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
            res.cursor = {"v": 1, "nets": {n.key: {"block": max(n.through + 1, n.start),
                                                  **({"win": wins[n.key]} if wins.get(n.key) else {}),
                                                  **({"from": n.gap_from} if n.gap_from else {})} for n in done}}
            res.resume = not res.complete
            for n in done:
                if n.gap_from:
                    res.gaps.append(f"{n.label}: Verlauf vor Block {n.gap_from:,} ist über {nets[0][3].ep.label} nicht "
                                    "abrufbar (Rückblick-Grenze der kostenlosen Route) – frühere Vorgänge per CSV-/"
                                    "Koinly-Import oder mit einem Subscan-Schlüssel ergänzen".replace(",", "."))
                res.warnings += n.notes
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
            sym = str(t.get("asset_symbol") or self.native)
            uid = str(t.get("asset_unique_id") or "")
            native = sym == self.native and uid in ("", self.native, "native")
            try:
                qty = Decimal(str(t.get("amount") or "0"))
            except InvalidOperation:
                hints.append("Betrag nicht lesbar")
                continue
            v2 = str(t.get("amount_v2") or "")
            if native and _INT.match(v2) and units(v2, self.decimals) != qty:
                hints.append(f"Betrag nicht eindeutig (amount {qty} ≠ amount_v2/10^{self.decimals}) – Menge prüfen")
            if not qty:
                continue
            asset = self.native if native else token_key(clean_symbol(sym, "TOKEN"), self.token_tag, uid or sym)
            ev = t.get("event_idx")
            sub = f"tr:{int(ev)}" if isinstance(ev, int) else f"tr:{sub_hash(t.get('from'), t.get('to'), qty)}"
            frm, to = t.get("from"), t.get("to")
            frm = addr if self._is_me(frm, addr) else frm
            to = addr if self._is_me(to, addr) else to
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
                fee = amount(fee_raw, self.decimals)
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
            hints.append(f"Bewegung aus Staking/Nomination Pool – Art prüfen (gebundene {self.native} bleiben im "
                         "Konto)")
        plain = not (modules - _PLAIN_CALLS - {""})
        if x is None and any(self._is_me(t.get("from"), addr) for t in transfers):
            hints.append("Abgang ohne eigene Signatur (Proxy/Multisig?) – Art prüfen")
        raw = {"network": net.key, "extrinsic": idx, "call": call or None, "fee_raw": str((x or {}).get("fee_used")
                                                                                         or (x or {}).get("fee") or "")
               or None, "moves": raw_moves[:20]}
        return TxView(self.provider, txid, ts, moves, fee=fee, fee_asset=self.native, initiated=initiated,
                      failed=failed, plain=plain, hint="; ".join(dict.fromkeys(hints)) or None, label=call or None,
                      raw={k: v for k, v in raw.items() if v not in (None, [])})

    def _reward(self, addr: str, net: _Net, r: dict[str, Any], skipped: Counter[str]) -> K.SourceEvent | None:
        idx = str(r.get("event_index") or "")
        if not _IDX.match(idx):
            skipped["Rewards ohne Ereigniskennung"] += 1
            return None
        try:
            qty = amount(r.get("amount"), self.decimals)
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
            rec = Rec(line=0, ts=ts, kind=M.WITHDRAWAL, out_sym=self.native, out_qty=qty, label="Staking-Slash",
                      review="Slash (Strafe des Validators) – Abgang prüfen", raw=raw,
                      note=f"Slash Ära {era}" if era is not None else "Slash")
        else:
            rec = Rec(line=0, ts=ts, kind=M.DEPOSIT, in_sym=self.native, in_qty=qty, tag="staking",
                      label="Staking-Reward", raw=raw,
                      note=f"Staking-Reward Ära {era}" if era is not None else "Staking-Reward")
        rec.txhash = None
        rec.ext_id = "slash" if slash else "reward"
        return K.SourceEvent(f"{self.provider}:reward-{net.short}-{idx}:{addr}", ts, [rec], rec.label)


def _order(idx: str) -> tuple[int, str]:
    head, _, tail = idx.partition("-")
    try:
        return int(head), tail.zfill(8)
    except ValueError:
        return 0, idx

