"""Datenvertrag der Import-Datei (Schema-Version 1.x).

Die Tabellen sind bewusst deklarativ gehalten – README und Validierung beziehen sich darauf.

Versionen (Minor-Versionen sind abwärtskompatibel, neue Spalten sind stets optional):

* **1.0** – Grundformat.
* **1.1** – ``related_asset`` in ``transactions.csv`` offiziell: verknüpft Dividenden, Ausschüttungen und
  Quellensteuer mit dem auslösenden Wertpapier (Zuordnung zur Position, Fondsart, Quellensteuer-Abgleich).
"""

from __future__ import annotations

SUPPORTED_SCHEMA_MAJOR = 1
SUPPORTED_SCHEMA_MINOR = 1
CURRENT_SCHEMA_VERSION = f"{SUPPORTED_SCHEMA_MAJOR}.{SUPPORTED_SCHEMA_MINOR}"

REQUIRED_FILES = ("transactions.csv", "assets.csv", "holdings_check.csv", "issues.csv")
OPTIONAL_FILES = ("manual_prices.csv", "accounts.csv")
ALL_FILES = ("manifest.json", *REQUIRED_FILES, *OPTIONAL_FILES)

# Zusatzdaten eines Portfolia-Exports (Einstellungen, Zuordnungen, Kurshistorie) – nicht Teil des Datenvertrags,
# andere Werkzeuge ignorieren den Ordner; Prüfsummen stehen im Manifest unter "extra_files".
SIDECAR_DIR = "portfolia/"

MAX_ZIP_BYTES = 200 * 1024 * 1024
MAX_MEMBER_BYTES = 150 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200

TX_REQUIRED = (
    "tx_id", "datetime", "type",
    "from_account", "from_asset", "from_qty",
    "to_account", "to_asset", "to_qty",
    "fee_asset", "fee_qty", "fee_eur",
    "value_eur",
)
TX_OPTIONAL = ("tag", "orig_price", "orig_ccy", "source", "source_ref", "flag", "note", "related_asset")

TX_TYPES = ("buy", "sell", "trade", "deposit", "withdrawal", "transfer", "corporate_action")

# Bekannte Tags; unbekannte Tags sind erlaubt (Warnung), damit der Kurator erweitern kann.
INCOME_TAGS = ("reward", "airdrop", "mining", "bonus", "other_income", "staking", "interest", "lending",
               "dividend", "cashback", "fork")
LOSS_TAGS = ("lost", "stolen", "cost", "fee", "burn")
GIFT_OUT_TAGS = ("gift", "donation")
GIFT_IN_TAGS = ("gift_received",)
CORPORATE_TAGS = ("split", "reverse_split", "merger", "spinoff", "migration", "rename", "swap")
OTHER_TAGS = ("exchange", "internal", "withholding_tax", "tax", "margin", "realized_gain", "realized_loss",
              "bridge", "wrap", "unwrap", "liquidity_in", "liquidity_out", "refund")
KNOWN_TAGS = frozenset(INCOME_TAGS + LOSS_TAGS + GIFT_OUT_TAGS + GIFT_IN_TAGS + CORPORATE_TAGS + OTHER_TAGS)

ASSET_REQUIRED = ("asset_id", "name", "asset_class", "quote_source", "quote_id")
ASSET_OPTIONAL = ("wkn", "isin", "koinly_id", "status", "note", "aliases", "category")
ASSET_CLASSES = ("security", "crypto", "fiat")
QUOTE_SOURCES = ("yahoo", "coingecko", "manual", "none")

HOLDINGS_REQUIRED = ("asset_id", "qty")
HOLDINGS_OPTIONAL = ("account", "as_of", "source", "note")
# Toleranz bei Spaltennamen (Kurator-Varianten) → kanonischer Name
HOLDINGS_ALIASES = {"quantity": "qty", "expected_qty": "qty", "balance": "qty", "amount": "qty",
                    "wallet": "account", "asset": "asset_id"}

MANUAL_REQUIRED = ("asset_id", "date", "price_eur")
MANUAL_OPTIONAL = ("source",)

ACCOUNTS_REQUIRED = ("account",)
ACCOUNTS_OPTIONAL = ("broker", "depot_group")

# Währungen, die bei fehlender Definition in assets.csv implizit als Fiat angelegt werden (mit Warnung).
ISO_CURRENCIES = frozenset({
    "EUR", "USD", "GBP", "CHF", "JPY", "AUD", "CAD", "NZD", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF",
    "HKD", "SGD", "CNY", "KRW", "TRY", "ZAR", "BRL", "MXN", "INR", "ILS", "RON", "BGN", "ISK", "IDR",
    "MYR", "PHP", "THB",
})
