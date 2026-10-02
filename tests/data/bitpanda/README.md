# Synthetische Bitpanda-Fixtures

Antworten im Aufbau der offiziellen Referenz der Bitpanda Public API (docs.public.bitpanda.com, Stand 02.10.2026):
`/v1/operations` (`operations_page1..3.json`), `/v1/assets` (`assets.json`), `/v1/currencies` (`currencies.json`) und
`/v1/portfolio` (`portfolio.json`). Alle IDs sind erfundene UUIDs, alle Beträge frei gewählt – keine echten Kontodaten.

* Seiten mit `data`, `self_cursor`, `next_cursor`, `has_next_page`; die letzte Seite trägt einen `next_cursor`
  („c-4“) bei `has_next_page=false` – der Test-Server kennt diesen Cursor nicht.
* Vorgänge mit `operation_id`, `operation_type`, `transactions[]`; Teile mit `transaction_id`, `asset_id` bzw.
  `currency_id`, `wallet_id`, `asset_amount`/`fee_amount`/`asset_balance_after` als Betragsobjekt, `flow`,
  `credited_at`, `transaction_type`, `compensates`(`_info`), `trade` (`trade_id`, `fee`, `rate`, `rate_with_fee` …).
* Werte von `operation_type`/`transaction_type` sind nicht dokumentiert; verwendet werden beobachtete Schreibweisen
  (z. B. `buy`, `savings_plan`, `passive_earn_reward`, `stake`). Vorgang 17 hat kein `credited_at`.
* `asset_balance_after` ist je Wallet so berechnet, dass `fee_amount` zusätzlich abgezogen wird; `portfolio.json`
  enthält die Summen der Vorgänge (BTC nach Gebühr, ETH einschließlich der gestakten Menge, XAU ohne Vorgänge).
