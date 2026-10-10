"""peaq (PEAQ) – Substrate-Konten (SS58) und EVM-Adressen (H160) über Subscan, nur lesend.

Belegt (Stand der Recherche)
    * Offizieller Explorer laut peaq-Doku: Subscan (``peaq.subscan.io``); ``peaqscan.xyz`` leitet dorthin um, der
      frühere Blockscout (``scout.peaq.xyz``) ist nicht mehr gelistet.
    * SS58-Registry (paritytech/ss58-registry): Netz ``peaq``, Präfix 1221, Symbol PEAQ, 18 Dezimalstellen.
    * Subscan verlangt immer einen Schlüssel. Das kostenlose PubFi-Gateway führt das Netz ``peaq`` für die
      Substrate-Routen (Überweisungen, Extrinsics, Bestände) – dieselben wie bei Polkadot.

Adressarten
    * **SS58** (Substrate-Wallets wie Talisman/SubWallet/Polkadot.js): Abruf wie Polkadot (ein Netz ``peaq``),
      Gebühren aus den eigenen Extrinsics; Staking-Rewards über die Reward-Route, falls Subscan sie für peaq liefert
      (sonst Hinweis, Differenz in der Bestandsprüfung). Das generische Format (Präfix 42) wird in Präfix 1221
      umgerechnet – dasselbe Konto.
    * **EVM (0x…)** (MetaMask, Ledger über MetaMask):
      - *PubFi (kostenlos):* Subscan löst die 0x-Adresse über ``v2/scan/search`` (dokumentiert: „Resolves one account
        from a Substrate address, EVM address …“, Feld ``data.account.address``) in das zugehörige Substrate-Konto auf;
        danach läuft der Substrate-Abruf (native PEAQ-Bewegungen). EVM-Gasgebühren und ERC-20-Tokens sind auf diesem
        Weg nicht enthalten (die EVM-Transaktionsliste ``evm/v2/transactions`` hat kein dokumentiertes
        Antwortschema) – die Bestandsprüfung zeigt Differenzen.
      - *Subscan direkt (kostenpflichtig):* Etherscan-kompatible API (``txlist``, ``txlistinternal``, ``tokentx``)
        mit dem vorhandenen EVM-Adapter – vollständig inkl. Gas und Tokens. Über PubFi lässt diese Route keine
        Abfrageparameter zu (``query: deny`` laut OpenAPI).

Grenze
    Portfolia rechnet EVM-Adresse und Substrate-Konto nicht selbst um, sondern übernimmt nur die Auflösung durch
    Subscan.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any, ClassVar

from app.datasources import connector as K
from app.datasources.chainhttp import ENDPOINTS, ChainHttp, Endpoint, Stop
from app.datasources.chains.addresses import PEAQ_SS58
from app.datasources.chains.codec import ss58_decode, ss58_encode
from app.datasources.chains.evm import PeaqEvmConnector
from app.datasources.chains.polkadot import PolkadotConnector
from app.datasources.wallet import WalletConnector, units

RPC_ENDPOINTS = ("peaq_rpc", "peaq_rpc_2", "peaq_rpc_3")


class UnknownAccount(K.ConnectorError):
    """Subscan kennt zur 0x-Adresse kein Substrate-Konto (noch nie gebucht bzw. nicht aufgelöst)."""

    def __init__(self, address: str) -> None:
        super().__init__("data", "Subscan kennt zu dieser 0x-Adresse (noch) kein peaq-Konto – erst nach einer "
                                 "Bewegung auf peaq abrufbar, oder Anbieter „Subscan direkt“ wählen.")
        self.address = address


class PeaqSubstrateConnector(PolkadotConnector):
    """Substrate-Seite von peaq (SS58) – Polkadot-Adapter mit peaq-Parametern."""

    provider = "peaq"
    label = "peaq (Subscan via PubFi bzw. direkt)"
    chain_label = "peaq"
    native = "PEAQ"
    nets: ClassVar[tuple[tuple[str, str, str], ...]] = (("peaq", "pq", "peaq"),)
    decimals = 18
    ss58_prefix = PEAQ_SS58
    token_tag = "PEAQ"  # noqa: S105 - Kennung für Tokens, kein Geheimnis
    rewards_optional = True
    explorer_tx = "https://peaq.subscan.io/extrinsic/{}"
    explorer_addr = "https://peaq.subscan.io/account/{}"
    limits = ("Einheiten der Subscan-Felder sind nicht dokumentiert: Beträge werden gegen amount_v2 (18 Dezimal"
              "stellen) geprüft, Gebühren/Bestände als kleinste Einheit gelesen – Abweichungen zeigt die "
              "Bestandsprüfung",
              "Collator-Staking, XCM, Proxy/Multisig und Unbekanntes zur Prüfung; Staking-Rewards nur, soweit Subscan "
              "sie für peaq liefert",
              "EVM-Seite (0x…) ist eine eigene Datenquelle; die Zuordnung EVM ↔ Substrate („Address Unification“) "
              "ist nicht dokumentiert und wird nicht selbst berechnet")


_EVM_LIMITS = (*PeaqEvmConnector.limits,
               "Mit direktem Subscan-Key (kostenpflichtig) vollständig über die Etherscan-kompatible Route")
_EVM_PUBFI_LIMITS = ("Über PubFi (kostenlos): Subscan löst die 0x-Adresse in das zugehörige Substrate-Konto auf; "
                     "abgerufen werden native PEAQ-Bewegungen dieses Kontos",
                     "EVM-Gasgebühren und ERC-20-Tokens fehlen auf diesem Weg (keine dokumentierte EVM-"
                     "Transaktionsliste über PubFi) – die Bestandsprüfung zeigt die Differenz; vollständig nur mit "
                     "direktem Subscan-Key",
                     *PeaqSubstrateConnector.limits[:2])
EVM_VIA_PUBFI_NOTE = ("0x-Adresse über PubFi: native PEAQ-Bewegungen des zugehörigen Substrate-Kontos; EVM-Gas und "
                      "ERC-20-Tokens nicht enthalten (Bestandsprüfung zeigt Differenzen)")


@K.register
class PeaqConnector(WalletConnector):
    """Anbieter „peaq“: wählt je Adresse die Substrate- (SS58) bzw. EVM-Seite (0x…)."""

    provider = "peaq"
    label = "peaq (Subscan)"
    chain_label = "peaq"
    native = "PEAQ"
    endpoints = ("pubfi", "subscan")
    explorer_tx = PeaqSubstrateConnector.explorer_tx
    explorer_addr = PeaqSubstrateConnector.explorer_addr
    limits = PeaqSubstrateConnector.limits
    rpc_endpoints = RPC_ENDPOINTS  # Ausweichweg für den Bestand (Service: Prüfung läuft auch ohne PubFi-Schlüssel)

    @staticmethod
    def is_evm(cfg: K.SourceConfig) -> bool:
        return (cfg.address or "").lower().startswith("0x")

    @staticmethod
    def via_subscan_evm(cfg: K.SourceConfig) -> bool:
        """0x-Adresse mit direktem Subscan-Key → Etherscan-kompatible Route; sonst Substrate-Weg (ggf. aufgelöst)."""
        return PeaqConnector.is_evm(cfg) and (cfg.watch or {}).get("provider") == "subscan"

    def _impl(self, cfg: K.SourceConfig) -> WalletConnector:
        impl: WalletConnector = PeaqEvmConnector() if self.via_subscan_evm(cfg) else PeaqSubstrateConnector()
        for attr in ("progress", "usage", "catalog"):
            setattr(impl, attr, getattr(self, attr, None))
        for attr in ("transport", "sleep", "clock", "max_requests", "deadline_s"):
            if attr in type(self).__dict__ or attr in self.__dict__:
                setattr(impl, attr, getattr(self, attr))
        return impl

    def endpoint(self, cfg: K.SourceConfig) -> Endpoint:
        if self.via_subscan_evm(cfg):
            return ENDPOINTS["subscan_evm"]
        return super().endpoint(cfg)

    def _substrate_cfg(self, impl: PeaqSubstrateConnector, cfg: K.SourceConfig, secret: K.Secret) -> K.SourceConfig:
        """0x-Adresse über PubFi: zugehöriges Substrate-Konto bei Subscan nachschlagen (``v2/scan/search``)."""
        if not self.is_evm(cfg):
            return cfg
        evm = (cfg.address or "").lower()
        with impl.http(cfg, secret, max_requests=4, deadline_s=60, network="peaq") as http:
            data = impl._call(http, "v2/scan/search", {"key": evm}, "Substrate-Konto zur EVM-Adresse")
        acct = data.get("account") if isinstance(data, dict) else None
        raw = acct.get("address") if isinstance(acct, dict) else None
        try:
            _prefix, pub = ss58_decode(str(raw or ""))
        except ValueError:
            raise UnknownAccount(evm) from None
        return replace(cfg, address=ss58_encode(pub, PEAQ_SS58))

    def _chain_state(self, addr: str) -> tuple[Decimal, int] | None:
        """(Bestand in PEAQ, Transaktionszähler) laut öffentlichem peaq-EVM-RPC (Standard-JSON-RPC
        ``eth_getBalance`` / ``eth_getTransactionCount``); ``None``, wenn kein RPC antwortet."""
        for eid in RPC_ENDPOINTS:
            try:
                with ChainHttp(ENDPOINTS[eid], transport=self.transport, sleep=self.sleep, clock=self.clock,
                               max_requests=4, deadline_s=30, usage=self.usage) as http:
                    bal = http.rpc("eth_getBalance", [addr, "latest"], what="Bestand laut Kette")
                    nonce = http.rpc("eth_getTransactionCount", [addr, "latest"], what="Transaktionszähler")
                return units(int(str(bal), 16), 18), int(str(nonce), 16)
            except (K.ConnectorError, Stop, ValueError, TypeError):
                continue
        return None

    def _unknown(self, e: UnknownAccount) -> tuple[Decimal, str, bool]:
        """Konto bei Subscan unbekannt: Kette fragen → (Bestand, Hinweis, Historie fehlt). Leeres Konto (Bestand 0, nie
        gesendet) → vollständig, nichts zu buchen. Mit Bestand: der Bestand laut Kette ist ein belegter Ist-Bestand
        (Abgleich mit den Buchungen), die Historie dagegen über PubFi nicht abrufbar → Lücke, kein Fehler. Antwortet
        kein RPC, bleibt es beim Fehler."""
        state = self._chain_state(e.address)
        if state is None:
            raise e
        qty, nonce = state
        if qty == 0 and nonce == 0:
            return qty, ("Subscan kennt das Konto nicht; laut öffentlichem peaq-RPC ist die Adresse leer (Bestand 0, "
                         "nie eine Transaktion gesendet) – es gibt nichts zu buchen. Sobald Bewegungen existieren, "
                         "übernimmt der nächste Abruf sie."), False
        return qty, (f"Bestand laut Kette (öffentlicher peaq-RPC): {qty.normalize():f} PEAQ, Transaktionszähler "
                     f"{nonce}. Subscan kennt die 0x-Adresse über PubFi nicht – die Historie ist darüber nicht "
                     "abrufbar. Der Bestand dient als Ist-Bestand für den Abgleich; Zugänge per Koinly-/CSV-Import "
                     "bzw. mit Anbieter „Subscan direkt“ (eigener Schlüssel) ergänzen."), True

    def _rpc_only(self, cfg: K.SourceConfig, err: K.ConnectorError) -> K.CheckResult | None:
        """Anbieter (Subscan/PubFi) nicht nutzbar: Bestand der 0x-Adresse laut öffentlichem RPC als Ist-Bestand –
        das Anbieterproblem bleibt als Fehler sichtbar."""
        state = self._chain_state((cfg.address or "").lower())
        if state is None:
            return None
        qty = state[0]
        text = f"Bestand laut öffentlichem RPC: {qty.normalize():f} PEAQ (nur Bestand, keine Historie)"
        return K.CheckResult(False, f"{err.message} – {text}",
                             {"api": {"ok": False, "text": err.message}, "balance": {"ok": True, "text": text}},
                             balances=[K.Balance("PEAQ", qty, "peaq", "laut öffentlichem EVM-RPC")])

    def check(self, cfg: K.SourceConfig, secret: K.Secret) -> K.CheckResult:
        impl = self._impl(cfg)
        if isinstance(impl, PeaqSubstrateConnector):
            try:
                sub = self._substrate_cfg(impl, cfg, secret)
            except UnknownAccount as e:
                qty, note, gap = self._unknown(e)
                head = (f"peaq: Adresse {(cfg.address or '')[:8]}… – Bestand laut Kette, Historie nicht abrufbar."
                        if gap else f"peaq: Adresse {(cfg.address or '')[:8]}… ist noch leer.")
                return K.CheckResult(True, head,
                                     {"balance": {"ok": True, "text": f"Bestand {qty.normalize():f} PEAQ laut Kette"},
                                      "account": {"ok": not gap, "text": note}},
                                     balances=[K.Balance("PEAQ", qty, "peaq", "laut öffentlichem EVM-RPC")])
            except K.ConnectorError as e:
                fb = self._rpc_only(cfg, e) if self.is_evm(cfg) and e.kind not in ("cancelled", "config") else None
                if fb is None:
                    raise
                return fb
            res = impl.check(sub, secret)
            if sub is not cfg:
                res.details["mapping"] = {"ok": True, "text": f"0x-Adresse gehört laut Subscan zu {sub.address[:8]}…"
                                                              f"{sub.address[-6:]} – {EVM_VIA_PUBFI_NOTE}"}
            return res
        return impl.check(cfg, secret)

    def fetch(self, cfg: K.SourceConfig, secret: K.Secret, cursor: dict[str, Any] | None) -> K.FetchResult:
        impl = self._impl(cfg)
        if isinstance(impl, PeaqSubstrateConnector):
            try:
                sub = self._substrate_cfg(impl, cfg, secret)
            except UnknownAccount as e:
                qty, note, gap = self._unknown(e)
                return K.FetchResult(complete=not gap, cursor=cursor, warnings=[note], gaps=[note] if gap else [],
                                     balances=[K.Balance("PEAQ", qty, "peaq", "laut öffentlichem EVM-RPC")],
                                     coverage={"mode": "nur Bestand laut Kette" if gap else "leeres Konto",
                                               "operations": 0, "provider": "peaq-RPC"})
            res = impl.fetch(sub, secret, cursor)
            if sub is not cfg:
                res.warnings.append(EVM_VIA_PUBFI_NOTE)
                res.coverage["evm_account"] = sub.address
            return res
        return impl.fetch(cfg, secret, cursor)

    def coverage_limits(self, cfg: K.SourceConfig) -> list[str]:
        if self.via_subscan_evm(cfg):
            return list(_EVM_LIMITS)
        return list(_EVM_PUBFI_LIMITS if self.is_evm(cfg) else PeaqSubstrateConnector.limits)
