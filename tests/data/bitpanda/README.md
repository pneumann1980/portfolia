# Anonymisierte Bitpanda-Fixtures

Synthetische Antworten im Format der Bitpanda Public API (`/v1/operations`, `/v1/assets`, `/v1/currencies`,
`/v1/portfolio/holdings`). Alle IDs sind erfundene UUIDs, alle Beträge frei gewählt – keine echten Kontodaten.

Feldnamen nach dem Stand der Prüfung (siehe README → Bitpanda): Operation mit `id`, `type`, `timestamp` und
`transactions[]`; je Transaktion `transaction_id`, `asset_id` bzw. `currency_id`, `amount`, `fee_amount`,
`flow` (`INCOMING`/`OUTGOING`), `transaction_type`, `trade_id`, `compensates`. Der Connector liest daneben
camelCase-Varianten; unbekannte Strukturen landen als „ungeklärt“ in der Prüfung.
