"""Zwischenformat der CSV-Profile und Stammdaten für die Zuordnung von Symbolen.

Jedes Profil übersetzt Zeilen seines Exportformats in :class:`Rec` – einen Vorgang mit Abgang, Zugang und Gebühr
in *Symbolen der Quelle*. Der Dienst ordnet die Symbole anschließend Assets zu, bewertet in EUR, gleicht Transfers
ab und erzeugt daraus Buchungen im einheitlichen Format des Datenvertrags (``transactions.csv``).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

# Vorgangsarten
TRADE = "trade"  # Abgang gegen Zugang (Kauf, Verkauf, Tausch); Abgang fehlt = Kauf von außen (Karte)
DEPOSIT = "deposit"  # Zugang (mit Tag: Ertrag)
WITHDRAWAL = "withdrawal"  # Abgang (mit Tag: Kosten, Verlust, Schenkung)
TRANSFER = "transfer"  # zwischen zwei Konten der Datei (z. B. Koinly-Wallets)
FEE = "fee"  # nur Gebühr (fehlgeschlagene Transaktion, Staking-Aktion)
CONVERSION = "conversion"  # Token-Umstellung ohne Veräußerung (Rebranding, Migration)
DIRECT = "direct"  # Zeile bereits im einheitlichen Format (Portfolia-CSV)
REVIEW = "review"  # Vorgang einer Datenquelle ohne eindeutige Abbildung („ungeklärt“, Grund in ``note``)

KIND_LABEL = {TRADE: "Handel", DEPOSIT: "Zugang", WITHDRAWAL: "Abgang", TRANSFER: "Übertrag", FEE: "Gebühr",
              CONVERSION: "Umstellung", DIRECT: "Buchung", REVIEW: "ungeklärt"}


@dataclass
class Rec:
    line: int
    ts: datetime
    kind: str
    out_sym: str | None = None
    out_qty: Decimal | None = None
    in_sym: str | None = None
    in_qty: Decimal | None = None
    fee_sym: str | None = None
    fee_qty: Decimal | None = None
    tag: str | None = None
    value: Decimal | None = None  # Gegenwert laut Datei (Betrag in value_ccy)
    value_ccy: str | None = None
    fee_value: Decimal | None = None
    fee_value_ccy: str | None = None
    account: str | None = None  # Konto laut Datei (Mehrkonten-Exporte)
    to_account: str | None = None  # Zielkonto (nur TRANSFER)
    ext_id: str | None = None  # Kennung in der Quelle (idempotent je Quelle)
    txhash: str | None = None  # Blockchain-Transaktion (Transfer-Abgleich)
    event_key: str | None = None  # stabile Ereignis-ID „anbieter:id“ (Datenquellen; ein Ereignis → n Zeilen)
    event_line: int | None = None  # Zeile innerhalb des Ereignisses
    aliases: list[str] = field(default_factory=list)  # weitere Anbieter-IDs desselben Ereignisses („anbieter:id“)
    review: str | None = None  # abbildbar, aber prüfbedürftig – nie automatisch übernehmen (Grund)
    raw: dict[str, Any] | None = None  # Originaldaten des Anbieters (Beträge als Text, IDs) zur Nachprüfung
    note: str | None = None
    label: str | None = None  # Vorgangsbezeichnung der Quelle (Anzeige)
    date_only: bool = False
    class_hint: dict[str, str] = field(default_factory=dict)  # Symbol → security|crypto|fiat (z. B. Bitpanda)
    row: dict[str, str] | None = None  # DIRECT: Zeile im einheitlichen Format

    def symbols(self) -> list[str]:
        if self.row is not None:
            return [s for s in (self.row.get("from_asset"), self.row.get("to_asset"), self.row.get("fee_asset"),
                                self.row.get("related_asset")) if s]
        return [s for s in (self.out_sym, self.in_sym, self.fee_sym) if s]


@dataclass
class ParseResult:
    recs: list[Rec] = field(default_factory=list)
    errors: list[tuple[int, str]] = field(default_factory=list)  # (Zeile, Meldung)
    skipped: Counter[str] = field(default_factory=Counter)  # Grund → Anzahl
    notes: list[str] = field(default_factory=list)
    rows_read: int = 0

    def skip(self, reason: str, n: int = 1) -> None:
        self.skipped[reason] += n

    def error(self, line: int, msg: str) -> None:
        if len(self.errors) < 2000:
            self.errors.append((line, msg))


@dataclass
class ParseOptions:
    tz: Any = None  # ZoneInfo | timezone
    decimal: str = "."
    dayfirst: bool | None = None
    account: str = ""
    default_asset: str = ""  # Wallet-Exporte ohne Währungsspalte (Electrum, ältere Trezor-Exporte)
    filename: str = ""
    mapping: dict[str, Any] | None = None  # Zuordnung für eigene Formate


# Stablecoins: Ersatzbewertung zum Anker, falls kein Marktkurs gespeichert ist
USD_STABLE = frozenset({"USDT", "USDC", "BUSD", "DAI", "TUSD", "USDP", "FDUSD", "PYUSD", "GUSD", "LUSD", "USDS"})
EUR_STABLE = frozenset({"EURC", "EUROC", "EURT", "EURS", "EURI", "EURCV", "AEUR", "EURE"})

# Gängige Kryptowerte: Vorschlag für neue Assets (Name, CoinGecko-ID). Nur Vorschläge – im Formular prüfbar.
KNOWN_COINS: dict[str, tuple[str, str]] = {
    "BTC": ("Bitcoin", "bitcoin"), "ETH": ("Ethereum", "ethereum"), "USDT": ("Tether", "tether"),
    "USDC": ("USD Coin", "usd-coin"), "BNB": ("BNB", "binancecoin"), "XRP": ("XRP", "ripple"),
    "ADA": ("Cardano", "cardano"), "SOL": ("Solana", "solana"), "DOGE": ("Dogecoin", "dogecoin"),
    "DOT": ("Polkadot", "polkadot"), "TRX": ("TRON", "tron"), "MATIC": ("Polygon (MATIC)", "matic-network"),
    "POL": ("Polygon Ecosystem Token", "polygon-ecosystem-token"), "LTC": ("Litecoin", "litecoin"),
    "BCH": ("Bitcoin Cash", "bitcoin-cash"), "AVAX": ("Avalanche", "avalanche-2"), "LINK": ("Chainlink", "chainlink"),
    "XLM": ("Stellar", "stellar"), "ATOM": ("Cosmos Hub", "cosmos"), "UNI": ("Uniswap", "uniswap"),
    "ETC": ("Ethereum Classic", "ethereum-classic"), "XMR": ("Monero", "monero"), "ALGO": ("Algorand", "algorand"),
    "VET": ("VeChain", "vechain"), "FIL": ("Filecoin", "filecoin"), "ICP": ("Internet Computer", "internet-computer"),
    "AAVE": ("Aave", "aave"), "XTZ": ("Tezos", "tezos"), "NEAR": ("NEAR Protocol", "near"),
    "APT": ("Aptos", "aptos"), "ARB": ("Arbitrum", "arbitrum"), "OP": ("Optimism", "optimism"),
    "SHIB": ("Shiba Inu", "shiba-inu"), "DAI": ("Dai", "dai"), "EURC": ("EURC", "euro-coin"),
    "SAND": ("The Sandbox", "the-sandbox"), "MANA": ("Decentraland", "decentraland"),
    "GRT": ("The Graph", "the-graph"), "CRO": ("Cronos", "crypto-com-chain"),
    "BEST": ("Bitpanda Ecosystem Token", "bitpanda-ecosystem-token"), "KSM": ("Kusama", "kusama"),
    "EGLD": ("MultiversX", "elrond-erd-2"), "HBAR": ("Hedera", "hedera-hashgraph"), "MKR": ("Maker", "maker"),
    "RUNE": ("THORChain", "thorchain"), "ZEC": ("Zcash", "zcash"), "DASH": ("Dash", "dash"),
    "IOTA": ("IOTA", "iota"), "CHZ": ("Chiliz", "chiliz"), "BAT": ("Basic Attention", "basic-attention-token"),
    "CRV": ("Curve DAO", "curve-dao-token"), "LDO": ("Lido DAO", "lido-dao"), "SUI": ("Sui", "sui"),
    "TIA": ("Celestia", "celestia"), "PEPE": ("Pepe", "pepe"), "INJ": ("Injective", "injective-protocol"),
    "FET": ("Artificial Superintelligence Alliance (FET)", "fetch-ai"), "IMX": ("Immutable", "immutable-x"),
    "STX": ("Stacks", "blockstack"), "TON": ("Toncoin", "the-open-network"), "KAS": ("Kaspa", "kaspa"),
    "THETA": ("Theta Network", "theta-token"), "APE": ("ApeCoin", "apecoin"), "COMP": ("Compound",
                                                                                   "compound-governance-token"),
    "SNX": ("Synthetix", "havven"), "1INCH": ("1inch", "1inch"), "ENJ": ("Enjin Coin", "enjincoin"),
    "KAVA": ("Kava", "kava"), "FLOW": ("Flow", "flow"), "MINA": ("Mina", "mina-protocol"),
    "GALA": ("GALA", "gala"), "QNT": ("Quant", "quant-network"), "EOS": ("EOS", "eos"),
    "NEO": ("NEO", "neo"), "WBTC": ("Wrapped Bitcoin", "wrapped-bitcoin"), "STETH": ("Lido Staked Ether",
                                                                                    "staked-ether"),
}

# Kraken: historische Kürzel → übliche Symbole; Staking-/Earn-Varianten tragen Suffixe
KRAKEN_ALIASES = {"XXBT": "BTC", "XBT": "BTC", "XBT.M": "BTC", "XETH": "ETH", "XXDG": "DOGE", "XDG": "DOGE",
                  "XXRP": "XRP", "XLTC": "LTC", "XXLM": "XLM", "XETC": "ETC", "XZEC": "ZEC", "XXMR": "XMR",
                  "XREP": "REP", "XMLN": "MLN", "XICN": "ICN", "XXVN": "XVN", "ZEUR": "EUR", "ZUSD": "USD",
                  "ZGBP": "GBP", "ZCAD": "CAD", "ZJPY": "JPY", "ZAUD": "AUD", "ZCHF": "CHF", "ETH2": "ETH",
                  "ETH2.S": "ETH"}
# „DOT.S“, „DOT28.S“ (gebunden, 28 Tage), „USDC.M“, „ETH.F“, „XBT.M“ → Basissymbol
_KRAKEN_STAKED = re.compile(r"^([A-Z0-9]+?)(03|04|07|14|21|28)?\.(S|M|P|F|B|HOLD|CORE|INK)$")


def kraken_symbol(s: str) -> str:
    s = s.strip().upper()
    if s in KRAKEN_ALIASES:
        return KRAKEN_ALIASES[s]
    m = _KRAKEN_STAKED.match(s)
    if m:
        s = m.group(1)
    return KRAKEN_ALIASES.get(s, s)


# Kennzeichnungen in Exporten → Tags des Datenvertrags (Kleinbuchstaben, Leerzeichen/Bindestriche → „_“)
INCOME_WORDS: dict[str, str] = {
    "staking": "staking", "stake_reward": "staking", "staking_reward": "staking", "staking_income": "staking",
    "reward": "reward", "rewards": "reward", "rewards_income": "reward", "reward_income": "reward",
    "interest": "interest", "lending_interest": "lending", "lending": "lending", "loan_interest": "lending",
    "airdrop": "airdrop", "fork": "fork", "hardfork": "fork", "mining": "mining", "masternode": "mining",
    "bonus": "bonus", "reward/bonus": "bonus", "referral": "bonus", "bounty": "bonus", "bounties": "bonus",
    "cashback": "cashback", "fee_refund": "cashback", "rebate": "cashback", "income": "other_income",
    "other_income": "other_income", "salary": "other_income", "gift_received": "gift_received",
    "gift/tip": "gift_received", "tip": "gift_received", "dividend": "dividend", "distribution": "airdrop",
}
OUT_WORDS: dict[str, str] = {
    "gift": "gift", "donation": "donation", "charity": "donation", "lost": "lost", "stolen": "stolen",
    "hack": "stolen", "cost": "cost", "spend": "cost", "payment": "cost", "purchase": "cost", "fee": "fee",
    "fees": "fee", "margin_fee": "fee", "loan_fee": "fee", "other_fee": "fee", "burn": "burn",
}


def norm_label(v: str | None) -> str:
    return (v or "").strip().lower().replace(" ", "_").replace("-", "_")


def income_tag(label: str | None) -> str | None:
    n = norm_label(label)
    if not n:
        return None
    if n in INCOME_WORDS:
        return INCOME_WORDS[n]
    for k, tag in (("staking", "staking"), ("stake", "staking"), ("interest", "interest"), ("zins", "interest"),
                   ("airdrop", "airdrop"), ("mining", "mining"), ("reward", "reward"), ("bonus", "bonus"),
                   ("referral", "bonus"), ("cashback", "cashback"), ("lending", "lending"), ("fork", "fork")):
        if k in n:
            return tag
    return None


def out_tag(label: str | None) -> str | None:
    n = norm_label(label)
    if not n:
        return None
    if n in OUT_WORDS:
        return OUT_WORDS[n]
    for k, tag in (("gift", "gift"), ("geschenk", "gift"), ("donat", "donation"), ("spende", "donation"),
                   ("lost", "lost"), ("verlust", "lost"), ("stolen", "stolen"), ("fee", "fee"), ("gebühr", "fee"),
                   ("cost", "cost"), ("kosten", "cost"), ("spend", "cost"), ("payment", "cost"), ("burn", "burn")):
        if k in n:
            return tag
    return None
