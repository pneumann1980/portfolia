-- Portfolia – SQLite-Schema (Migration 1)
-- Portfolio-Daten (tx, assets, …) sind versioniert über import_id; abgeleitete Daten (Kurse, News,
-- Snapshots) sind importunabhängig bzw. werden nach jedem Import neu berechnet.

CREATE TABLE IF NOT EXISTS app_state (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS imports (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  filename       TEXT NOT NULL,
  file_sha256    TEXT NOT NULL,
  file_size      INTEGER,
  file_mtime     TEXT,
  processed_at   TEXT NOT NULL,
  status         TEXT NOT NULL,            -- active | archived | failed
  schema_version TEXT,
  generated_at   TEXT,
  valuation_date TEXT,
  notes          TEXT,
  counts_json    TEXT,
  report_json    TEXT,                     -- Validierungsmeldungen
  diff_json      TEXT,                     -- Diff zur Vorversion
  check_json     TEXT,                     -- Abgleich holdings_check
  duration_ms    INTEGER,
  data_retained  INTEGER NOT NULL DEFAULT 1,
  trigger        TEXT
);
CREATE INDEX IF NOT EXISTS ix_imports_sha ON imports(file_sha256);

CREATE TABLE IF NOT EXISTS tx (
  import_id     INTEGER NOT NULL,
  seq           INTEGER NOT NULL,
  tx_id         TEXT NOT NULL,
  ts_utc        TEXT NOT NULL,
  date_local    TEXT NOT NULL,
  date_only     INTEGER NOT NULL DEFAULT 0,
  type          TEXT NOT NULL,
  tag           TEXT,
  from_account  TEXT, from_asset TEXT, from_qty TEXT,
  to_account    TEXT, to_asset   TEXT, to_qty   TEXT,
  fee_asset     TEXT, fee_qty    TEXT, fee_eur  TEXT,
  value_eur     TEXT,
  orig_price    TEXT, orig_ccy   TEXT,
  source        TEXT, source_ref TEXT, flag TEXT, note TEXT,
  related_asset TEXT,
  row_hash      TEXT NOT NULL,
  raw_json      TEXT NOT NULL,
  PRIMARY KEY (import_id, tx_id)
);
CREATE INDEX IF NOT EXISTS ix_tx_seq ON tx(import_id, seq);

CREATE TABLE IF NOT EXISTS assets (
  import_id    INTEGER NOT NULL,
  asset_id     TEXT NOT NULL,
  name         TEXT,
  asset_class  TEXT NOT NULL,
  wkn          TEXT, isin TEXT, koinly_id TEXT,
  quote_source TEXT, quote_id TEXT,
  status       TEXT, note TEXT, aliases TEXT, category TEXT,
  extra_json   TEXT,
  PRIMARY KEY (import_id, asset_id)
);

CREATE TABLE IF NOT EXISTS accounts (
  import_id   INTEGER NOT NULL,
  account     TEXT NOT NULL,
  broker      TEXT,
  depot_group TEXT,
  extra_json  TEXT,
  PRIMARY KEY (import_id, account)
);

CREATE TABLE IF NOT EXISTS holdings_check (
  import_id  INTEGER NOT NULL,
  seq        INTEGER NOT NULL,
  asset_id   TEXT NOT NULL,
  account    TEXT,
  qty        TEXT NOT NULL,
  extra_json TEXT,
  PRIMARY KEY (import_id, seq)
);

CREATE TABLE IF NOT EXISTS issues (
  import_id INTEGER NOT NULL,
  seq       INTEGER NOT NULL,
  data_json TEXT NOT NULL,
  PRIMARY KEY (import_id, seq)
);

CREATE TABLE IF NOT EXISTS manual_prices (
  import_id INTEGER NOT NULL,
  asset_id  TEXT NOT NULL,
  date      TEXT NOT NULL,
  price_eur TEXT NOT NULL,
  source    TEXT,
  PRIMARY KEY (import_id, asset_id, date)
);

-- Kurse ------------------------------------------------------------------------------------------
-- series: 'yahoo:AAPL', 'cg:bitcoin', 'fx:yahoo:USD', 'fx:ecb:USD', 'demo:…'
CREATE TABLE IF NOT EXISTS price_daily (
  series       TEXT NOT NULL,
  date         TEXT NOT NULL,
  open REAL, high REAL, low REAL,
  close        REAL NOT NULL,              -- wie gehandelt (nicht split-adjustiert)
  volume       REAL,
  split_factor REAL NOT NULL DEFAULT 1,    -- kumulierter Split-Faktor nach diesem Tag
  ccy          TEXT,
  source       TEXT NOT NULL,
  fetched_at   TEXT NOT NULL,
  PRIMARY KEY (series, date)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS quote_latest (
  series         TEXT PRIMARY KEY,
  price          REAL NOT NULL,
  ccy            TEXT,
  prev_close     REAL,
  market_time    TEXT,
  fetched_at     TEXT NOT NULL,
  source         TEXT NOT NULL,
  change_pct     REAL,
  market_state   TEXT
);

CREATE TABLE IF NOT EXISTS quote_intraday (
  series TEXT NOT NULL,
  ts     TEXT NOT NULL,
  price  REAL NOT NULL,
  PRIMARY KEY (series, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS series_meta (
  series              TEXT PRIMARY KEY,
  history_from        TEXT,                -- frühestes angefragtes und abgedecktes Datum
  history_to          TEXT,
  last_history_fetch  TEXT,
  history_status      TEXT,
  history_error       TEXT,
  info_json           TEXT,
  info_fetched_at     TEXT,
  last_error          TEXT,
  last_error_at       TEXT
);

-- Snapshots --------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS snapshot_daily (
  date           TEXT PRIMARY KEY,
  import_id      INTEGER,
  value_eur      REAL NOT NULL,
  invested_eur   REAL NOT NULL,
  inflow_eur     REAL NOT NULL DEFAULT 0,
  outflow_eur    REAL NOT NULL DEFAULT 0,
  income_eur     REAL NOT NULL DEFAULT 0,
  fees_eur       REAL NOT NULL DEFAULT 0,
  unvalued_count INTEGER NOT NULL DEFAULT 0,
  estimated_count INTEGER NOT NULL DEFAULT 0,
  computed_at    TEXT NOT NULL,
  kind           TEXT NOT NULL DEFAULT 'backfill'   -- backfill | eod
);

CREATE TABLE IF NOT EXISTS snapshot_asset_daily (
  date        TEXT NOT NULL,
  asset_id    TEXT NOT NULL,
  qty         REAL NOT NULL,
  price_eur   REAL,
  value_eur   REAL NOT NULL,
  flow_eur    REAL NOT NULL DEFAULT 0,
  price_kind  TEXT,                        -- quote | ffill | tx | manual | none
  PRIMARY KEY (date, asset_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_snap_asset ON snapshot_asset_daily(asset_id, date);

-- Betrieb ----------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_usage (
  provider TEXT NOT NULL,
  period   TEXT NOT NULL,                  -- z. B. '2026-09' (Monat) oder '2026-09-26' (Tag)
  calls    INTEGER NOT NULL DEFAULT 0,
  units    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (provider, period)
);

CREATE TABLE IF NOT EXISTS event_log (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           TEXT NOT NULL,
  level        TEXT NOT NULL,
  logger       TEXT,
  message      TEXT,
  context_json TEXT
);

CREATE TABLE IF NOT EXISTS job_status (
  job           TEXT PRIMARY KEY,
  last_start    TEXT,
  last_end      TEXT,
  last_ok       INTEGER,
  last_error    TEXT,
  progress_json TEXT,
  running       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS source_status (
  source_id            TEXT PRIMARY KEY,
  kind                 TEXT,
  name                 TEXT,
  last_attempt         TEXT,
  last_success         TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  next_allowed         TEXT,
  last_error           TEXT,
  etag                 TEXT,
  last_modified        TEXT,
  items_last           INTEGER,
  verified             TEXT                -- ok | unreachable | pending
);

-- News & Videos ----------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS news_item (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  kind          TEXT NOT NULL,             -- article | video
  url           TEXT NOT NULL,
  url_norm      TEXT NOT NULL UNIQUE,
  title         TEXT NOT NULL,
  title_norm    TEXT NOT NULL,
  summary       TEXT,
  source_id     TEXT,
  source_name   TEXT,
  source_weight REAL NOT NULL DEFAULT 0.5,
  language      TEXT,
  published_at  TEXT NOT NULL,
  fetched_at    TEXT NOT NULL,
  image_url     TEXT,
  video_id      TEXT,
  channel_id    TEXT,
  channel_name  TEXT,
  duration_s    INTEGER,
  views         INTEGER,
  is_short      INTEGER,
  is_live       INTEGER,
  relevance     REAL NOT NULL DEFAULT 0,
  is_read       INTEGER NOT NULL DEFAULT 0,
  hidden_reason TEXT,
  dup_count     INTEGER NOT NULL DEFAULT 0,
  llm_summary   TEXT,
  llm_score     REAL
);
CREATE INDEX IF NOT EXISTS ix_news_pub ON news_item(published_at);
CREATE INDEX IF NOT EXISTS ix_news_kind ON news_item(kind, published_at);

CREATE TABLE IF NOT EXISTS news_asset (
  item_id  INTEGER NOT NULL,
  asset_id TEXT NOT NULL,
  score    REAL NOT NULL,
  matched  TEXT,
  PRIMARY KEY (item_id, asset_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_news_asset ON news_asset(asset_id);

CREATE TABLE IF NOT EXISTS yt_suggestion (
  channel_id   TEXT PRIMARY KEY,
  title        TEXT,
  handle       TEXT,
  subscribers  INTEGER,
  found_for    TEXT,
  sample_video TEXT,
  sample_title TEXT,
  first_seen   TEXT NOT NULL,
  last_seen    TEXT NOT NULL,
  hits         INTEGER NOT NULL DEFAULT 1,
  status       TEXT NOT NULL DEFAULT 'new'   -- new | subscribed | ignored | blocked
);

CREATE TABLE IF NOT EXISTS llm_usage (
  day           TEXT PRIMARY KEY,
  input_tokens  INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  calls         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS digest (
  day        TEXT PRIMARY KEY,
  content    TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS img_cache (
  key          TEXT PRIMARY KEY,
  path         TEXT NOT NULL,
  content_type TEXT NOT NULL,
  size         INTEGER NOT NULL,
  fetched_at   TEXT NOT NULL
);

-- Steuer -----------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tax_report (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  year         INTEGER NOT NULL,
  rulepack     TEXT NOT NULL,
  options_json TEXT NOT NULL,
  import_id    INTEGER,
  created_at   TEXT NOT NULL,
  file_path    TEXT NOT NULL,
  summary_json TEXT
);
