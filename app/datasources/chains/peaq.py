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
    * **EVM (0x…)** (MetaMask, Ledger über MetaMask): Subscans Etherscan-kompatible API (``txlist``,
      ``txlistinternal``, ``tokentx``) mit dem vorhandenen EVM-Adapter – **nur mit direktem Subscan-Key**: Das
      kostenlose PubFi-Gateway lässt für diese Route keine Abfrageparameter zu (``query: deny`` laut OpenAPI).

Nicht belegt (Grenze)
    Wie EVM-Adresse und Substrate-Konto bei peaq zusammenhängen („Address Unification“), beschreibt die Doku nicht.
    Portfolia rechnet deshalb nicht selbst um: Wer beide Seiten nutzt, legt beide Adressen als eigene Datenquellen an
    (gleiche Wallet-Gruppe); Umbuchungen zwischen ihnen erkennt der Transfer-Abgleich über den Hash.
"""

from __future__ import annotations

from typing import Any, ClassVar

from app.datasources import connector as K
from app.datasources.chainhttp import ENDPOINTS, Endpoint
from app.datasources.chains.addresses import PEAQ_SS58
from app.datasources.chains.evm import PeaqEvmConnector
from app.datasources.chains.polkadot import PolkadotConnector
from app.datasources.wallet import WalletConnector


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
               "Nur mit direktem Subscan-Key (kostenpflichtig) – das kostenlose PubFi-Gateway lässt für die "
               "Etherscan-kompatible Route keine Abfrageparameter zu",
               "Substrate-Konto (SS58) derselben Person ist eine eigene Datenquelle")


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

    @staticmethod
    def is_evm(cfg: K.SourceConfig) -> bool:
        return (cfg.address or "").lower().startswith("0x")

    def _impl(self, cfg: K.SourceConfig) -> WalletConnector:
        impl: WalletConnector = PeaqEvmConnector() if self.is_evm(cfg) else PeaqSubstrateConnector()
        for attr in ("progress", "usage", "catalog"):
            setattr(impl, attr, getattr(self, attr, None))
        for attr in ("transport", "sleep", "clock", "max_requests", "deadline_s"):
            if attr in type(self).__dict__ or attr in self.__dict__:
                setattr(impl, attr, getattr(self, attr))
        return impl

    def endpoint(self, cfg: K.SourceConfig) -> Endpoint:
        if self.is_evm(cfg):
            return ENDPOINTS["subscan_evm"]
        return super().endpoint(cfg)

    def _guard(self, cfg: K.SourceConfig) -> None:
        if self.is_evm(cfg) and (cfg.watch or {}).get("provider") == "pubfi":
            raise K.ConnectorError("config", "peaq-EVM-Adressen (0x…) lassen sich nur mit einem direkten Subscan-Key "
                                             "abrufen – das kostenlose PubFi-Gateway lässt für die Etherscan-"
                                             "kompatible Route keine Parameter zu. Anbieter auf „Subscan direkt“ "
                                             "stellen und den Subscan-Key hinterlegen.")

    def check(self, cfg: K.SourceConfig, secret: K.Secret) -> K.CheckResult:
        self._guard(cfg)
        return self._impl(cfg).check(cfg, secret)

    def fetch(self, cfg: K.SourceConfig, secret: K.Secret, cursor: dict[str, Any] | None) -> K.FetchResult:
        self._guard(cfg)
        return self._impl(cfg).fetch(cfg, secret, cursor)

    def coverage_limits(self, cfg: K.SourceConfig) -> list[str]:
        return list(_EVM_LIMITS if self.is_evm(cfg) else PeaqSubstrateConnector.limits)
