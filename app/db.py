"""SQLite-Zugriff (WAL) mit thread-lokalen Verbindungen und einfachen Migrationen."""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA_FILE = Path(__file__).with_name("schema.sql")

# Weitere Migrationen werden hier angehängt: (version, sql)
MIGRATIONS: list[tuple[int, str]] = [
    (1, "__schema__"),
    (2, """
CREATE TABLE IF NOT EXISTS yt_channel (
  handle      TEXT PRIMARY KEY,
  channel_id  TEXT,
  title       TEXT,
  subscribers INTEGER,
  status      TEXT NOT NULL,          -- ok | unresolvable | pending
  error       TEXT,
  method      TEXT,                   -- api | page | pinned
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_news_channel ON news_item(channel_id);
CREATE INDEX IF NOT EXISTS ix_news_video ON news_item(video_id);
"""),
    (3, """
-- Erkannte Sparpläne (abgeleitet aus dem Import) und geschätzte Ausführungen seit dem Importstand.
CREATE TABLE IF NOT EXISTS plan (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  key           TEXT NOT NULL UNIQUE,       -- Konto|Asset
  account       TEXT NOT NULL,
  asset_id      TEXT NOT NULL,
  freq          TEXT NOT NULL,              -- weekly | biweekly | semimonthly | monthly | bimonthly | quarterly
  days_json     TEXT,                       -- Ausführungstag(e) im Monat
  weekday       INTEGER,                    -- 0 = Montag (wöchentlich/zweiwöchentlich)
  time_local    TEXT,
  date_only     INTEGER NOT NULL DEFAULT 0,
  amount_eur    TEXT NOT NULL,
  fee_eur       TEXT NOT NULL DEFAULT '0',
  qty_decimals  INTEGER NOT NULL DEFAULT 6,
  funding_asset TEXT,
  funding       TEXT NOT NULL DEFAULT 'cash', -- cash (vom Kontoguthaben) | external (Lastschrift)
  weekend_shift INTEGER NOT NULL DEFAULT 1,
  executions    INTEGER NOT NULL DEFAULT 0,
  first_date    TEXT,
  last_date     TEXT,
  next_due      TEXT,
  confidence    TEXT NOT NULL,              -- hoch | mittel | niedrig
  status        TEXT NOT NULL,              -- active | paused | ended
  enabled       INTEGER,                    -- NULL = automatisch nach Sicherheit, 1/0 = Wahl des Nutzers
  user_amount   TEXT,                       -- vom Nutzer geänderte Sparrate
  detected_at   TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tx_estimate (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_key          TEXT NOT NULL,
  tx_id             TEXT NOT NULL UNIQUE,
  due_date          TEXT NOT NULL,          -- planmäßiger Termin (Idempotenz)
  ts_utc            TEXT NOT NULL,
  date_only         INTEGER NOT NULL DEFAULT 0,
  account           TEXT NOT NULL,
  asset_id          TEXT NOT NULL,
  qty               TEXT NOT NULL,
  price_eur         TEXT NOT NULL,
  value_eur         TEXT NOT NULL,
  fee_eur           TEXT NOT NULL DEFAULT '0',
  funding_asset     TEXT,
  funding           TEXT NOT NULL DEFAULT 'cash',
  price_source      TEXT,
  price_final       INTEGER NOT NULL DEFAULT 0,
  status            TEXT NOT NULL,          -- estimated | confirmed | superseded | missing | dismissed
  user_edited       INTEGER NOT NULL DEFAULT 0,
  matched_tx_id     TEXT,
  missing_import_id INTEGER,
  import_id         INTEGER,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  confirmed_at      TEXT,
  note              TEXT,
  UNIQUE(plan_key, due_date)
);
CREATE INDEX IF NOT EXISTS ix_tx_estimate_status ON tx_estimate(status);
"""),
    (4, """
-- In der App erfasste Buchungen (manuell, später Synchronisation mit Börsen/Wallets). Anders als Kurse, News
-- oder Snapshots sind das Primärdaten: sie gehen nur über die Sicherungen bzw. den Gesamtexport nicht verloren.
CREATE TABLE IF NOT EXISTS journal_tx (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  tx_id         TEXT NOT NULL UNIQUE,          -- PF-M-000001 (manuell)
  source        TEXT NOT NULL,                 -- manual | später z. B. binance, bitpanda, wallet
  external_id   TEXT,                          -- Kennung in der Quelle (idempotente Synchronisation)
  group_ref     TEXT,                          -- zusammengehörige Buchungen (z. B. Dividende + Quellensteuer)
  status        TEXT NOT NULL DEFAULT 'active',-- active | deleted | replaced (Teilbuchung per Bearbeiten entfernt)
  ts_utc        TEXT NOT NULL,
  date_only     INTEGER NOT NULL DEFAULT 0,
  type          TEXT NOT NULL,
  tag           TEXT,
  from_account  TEXT, from_asset TEXT, from_qty TEXT,
  to_account    TEXT, to_asset TEXT, to_qty TEXT,
  fee_asset     TEXT, fee_qty TEXT, fee_eur TEXT,
  value_eur     TEXT,
  value_source  TEXT,                          -- Herkunft des EUR-Werts (Eingabe, Schlusskurs, Devisenkurs …)
  orig_price    TEXT, orig_ccy TEXT,
  related_asset TEXT,
  note          TEXT,
  form_json     TEXT,                          -- Formulareingaben (zum Bearbeiten)
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE(source, external_id)
);
CREATE INDEX IF NOT EXISTS ix_journal_tx_status ON journal_tx(status);
CREATE TABLE IF NOT EXISTS journal_asset (
  asset_id      TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  asset_class   TEXT NOT NULL,                 -- security | crypto | fiat
  quote_source  TEXT NOT NULL DEFAULT 'none',  -- yahoo | coingecko | manual | none
  quote_id      TEXT,
  wkn           TEXT,
  isin          TEXT,
  category      TEXT,
  aliases       TEXT,
  note          TEXT,
  extra_json    TEXT,                          -- z. B. {"tax_type": "etf_equity"}
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journal_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  at          TEXT NOT NULL,
  action      TEXT NOT NULL,                   -- create | update | delete | restore | asset_create | asset_update
  ref         TEXT NOT NULL,                   -- tx_id bzw. asset_id
  before_json TEXT,
  after_json  TEXT
);
"""),
    (5, """
-- CSV-Importe (Börsen, Wallets, Steuertools): Datei, Vorschau je Zeile, Zuordnungen, eigene Formate.
CREATE TABLE IF NOT EXISTS csv_batch (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  filename     TEXT NOT NULL,
  file_sha256  TEXT NOT NULL,
  file_size    INTEGER NOT NULL,
  raw_gz       BLOB NOT NULL,                  -- Originaldatei (gzip) – Nachvollziehbarkeit, erneute Analyse
  profile      TEXT NOT NULL,                  -- binance | bitpanda | … | mapping:<id> | unknown
  account      TEXT NOT NULL,
  options_json TEXT,                           -- Zeitzone, Dezimaltrenner, Standardwährung, Stichtag
  status       TEXT NOT NULL,                  -- mapping | preview | partial | committed | reverted
  summary_json TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  committed_at TEXT,
  reverted_at  TEXT
);
CREATE TABLE IF NOT EXISTS csv_row (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id   INTEGER NOT NULL REFERENCES csv_batch(id) ON DELETE CASCADE,
  idx        INTEGER NOT NULL,
  line       INTEGER,
  rec_json   TEXT NOT NULL,                    -- Vorgang laut Datei (Zwischenformat)
  row_json   TEXT,                             -- Buchung im einheitlichen Format
  status     TEXT NOT NULL,     -- new | known | duplicate | before | ignored | invalid | committed | merged
  decision   TEXT,                             -- include | skip (Wahl des Nutzers)
  value_in   TEXT,                             -- eingegebener EUR-Wert
  fee_in     TEXT,                             -- eingegebener EUR-Wert der Gebühr
  pair_ref   TEXT,                             -- Transfer-Gegenbuchung: b:<idx> (Datei) | j:<tx_id> (Journal)
  pair_conf  TEXT,                             -- hoch | mittel
  pair_ok    INTEGER,                          -- NULL = Vorschlag, 1 = bestätigt, 0 = abgelehnt
  messages   TEXT,
  tx_id      TEXT,
  UNIQUE(batch_id, idx)
);
CREATE INDEX IF NOT EXISTS ix_csv_row_batch ON csv_row(batch_id, status);
CREATE TABLE IF NOT EXISTS csv_symbol (
  symbol     TEXT PRIMARY KEY,                 -- Symbol laut Datei (Großbuchstaben, ggf. mit Koinly-ID)
  asset_id   TEXT,                             -- NULL = Zeilen mit diesem Symbol ignorieren
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS csv_account (
  name       TEXT PRIMARY KEY,                 -- Konto/Wallet laut Datei
  account    TEXT NOT NULL,                    -- Konto in Portfolia
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS csv_mapping (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  spec_json  TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
-- journal_tx: Bezug zum CSV-Import, Transfer-Abgleich; Quellkennung nur für nicht zurückgenommene Buchungen
-- eindeutig (nach „Rückgängig“ kann dieselbe Datei erneut importiert werden).
CREATE TABLE journal_tx_v5 (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  tx_id         TEXT NOT NULL UNIQUE,          -- PF-M-… manuell, PF-C-… CSV, PF-T-… abgeglichener Transfer
  source        TEXT NOT NULL,                 -- manual | csv:<profil> | transfer
  external_id   TEXT,                          -- Kennung in der Quelle (idempotenter Import)
  group_ref     TEXT,
  status        TEXT NOT NULL DEFAULT 'active',-- active | deleted | replaced | merged | reverted
  ts_utc        TEXT NOT NULL,
  date_only     INTEGER NOT NULL DEFAULT 0,
  type          TEXT NOT NULL,
  tag           TEXT,
  from_account  TEXT, from_asset TEXT, from_qty TEXT,
  to_account    TEXT, to_asset TEXT, to_qty TEXT,
  fee_asset     TEXT, fee_qty TEXT, fee_eur TEXT,
  value_eur     TEXT,
  value_source  TEXT,
  orig_price    TEXT, orig_ccy TEXT,
  related_asset TEXT,
  note          TEXT,
  form_json     TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  batch_id      INTEGER,                       -- CSV-Import
  merged_into   TEXT,                          -- status merged: tx_id des Transfers
  pair_refs     TEXT                           -- Transfer: tx_ids der beiden Einzelbuchungen (kommagetrennt)
);
INSERT INTO journal_tx_v5(id, tx_id, source, external_id, group_ref, status, ts_utc, date_only, type, tag,
  from_account, from_asset, from_qty, to_account, to_asset, to_qty, fee_asset, fee_qty, fee_eur, value_eur,
  value_source, orig_price, orig_ccy, related_asset, note, form_json, created_at, updated_at)
SELECT id, tx_id, source, external_id, group_ref, status, ts_utc, date_only, type, tag,
  from_account, from_asset, from_qty, to_account, to_asset, to_qty, fee_asset, fee_qty, fee_eur, value_eur,
  value_source, orig_price, orig_ccy, related_asset, note, form_json, created_at, updated_at FROM journal_tx;
DROP TABLE journal_tx;
ALTER TABLE journal_tx_v5 RENAME TO journal_tx;
CREATE INDEX IF NOT EXISTS ix_journal_tx_status ON journal_tx(status);
CREATE INDEX IF NOT EXISTS ix_journal_tx_batch ON journal_tx(batch_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_journal_tx_ext ON journal_tx(source, external_id)
  WHERE external_id IS NOT NULL AND status <> 'reverted';
"""),
    (6, """
-- Kursquellen-Zuordnung für Assets ohne Kursquelle (z. B. Token laut Import „none“): gilt über dem Import.
CREATE TABLE IF NOT EXISTS asset_source (
  asset_id        TEXT PRIMARY KEY,
  quote_source    TEXT NOT NULL DEFAULT 'coingecko',
  quote_id        TEXT,                          -- zugeordnete ID (active) bzw. bester Vorschlag
  status          TEXT NOT NULL,                 -- active | suggested | none | rejected
  origin          TEXT NOT NULL,                 -- auto | user
  confidence      TEXT,                          -- hoch | mittel | niedrig
  reason          TEXT,
  candidates_json TEXT,
  checked_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_asset_source_status ON asset_source(status);
"""),
    (7, """
-- Datenquellen: Börsenkonten und öffentliche Wallet-Adressen, synchronisiert über Connectoren. Zugangsdaten
-- werden nie gespeichert – nur der Name einer Umgebungsvariable (credential_ref).
CREATE TABLE IF NOT EXISTS data_source (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  kind              TEXT NOT NULL,                  -- exchange | wallet
  provider          TEXT NOT NULL,                  -- Börse (kraken, …) bzw. Chain (bitcoin, ethereum, …)
  name              TEXT NOT NULL,                  -- frei wählbar
  account           TEXT NOT NULL,                  -- Konto in Portfolia, auf das gebucht wird
  address           TEXT,                           -- öffentliche Adresse / xpub (nur Wallets, normalisiert)
  credential_ref    TEXT,                           -- Umgebungsvariable mit Zugangsdaten (nur der Name)
  enabled           INTEGER NOT NULL DEFAULT 1,
  status            TEXT NOT NULL DEFAULT 'created',-- created | connected | synced | partial | error
  sync_interval_min INTEGER NOT NULL DEFAULT 0,     -- 0 = nur manuell
  auto_commit       INTEGER NOT NULL DEFAULT 0,     -- Abrufe ohne Überschneidung/unvollständige Zeile übernehmen
  last_run_at       TEXT,
  last_success_at   TEXT,
  last_error        TEXT,                           -- bereinigt (ohne Geheimnisse)
  last_error_at     TEXT,
  next_run_at       TEXT,
  cursor_json       TEXT,                           -- Fortsetzungspunkt des Connectors
  note              TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  UNIQUE(kind, provider, address)
);
CREATE INDEX IF NOT EXISTS ix_data_source_due ON data_source(enabled, next_run_at);
CREATE TABLE IF NOT EXISTS data_source_run (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id      INTEGER NOT NULL REFERENCES data_source(id) ON DELETE CASCADE,
  trigger        TEXT NOT NULL,                     -- manual | schedule | check
  started_at     TEXT NOT NULL,
  finished_at    TEXT,
  status         TEXT NOT NULL,                     -- running | ok | partial | error
  events         INTEGER NOT NULL DEFAULT 0,
  rows_new       INTEGER NOT NULL DEFAULT 0,
  rows_known     INTEGER NOT NULL DEFAULT 0,
  rows_overlap   INTEGER NOT NULL DEFAULT 0,
  rows_committed INTEGER NOT NULL DEFAULT 0,
  batch_id       INTEGER,
  message        TEXT
);
CREATE INDEX IF NOT EXISTS ix_data_source_run ON data_source_run(source_id, id);
-- Herkunft je Buchung: stabile externe Ereignis-ID (anbieter:id), Zeile im Ereignis, Blockchain-Hash, Datenquelle.
ALTER TABLE journal_tx ADD COLUMN event_key TEXT;
ALTER TABLE journal_tx ADD COLUMN event_line INTEGER;
ALTER TABLE journal_tx ADD COLUMN tx_hash TEXT;
ALTER TABLE journal_tx ADD COLUMN datasource_id INTEGER;
CREATE INDEX IF NOT EXISTS ix_journal_tx_event ON journal_tx(event_key);
CREATE INDEX IF NOT EXISTS ix_journal_tx_hash ON journal_tx(tx_hash);
-- Stapel: csv (Datei) | sync (Datenquelle); Quelle der Buchungen (NULL = csv:<profil>).
ALTER TABLE csv_batch ADD COLUMN kind TEXT NOT NULL DEFAULT 'csv';
ALTER TABLE csv_batch ADD COLUMN source TEXT;
ALTER TABLE csv_batch ADD COLUMN datasource_id INTEGER;
ALTER TABLE csv_row ADD COLUMN event_key TEXT;
ALTER TABLE csv_row ADD COLUMN event_line INTEGER;
"""),
    (8, """
-- Zugangsdaten einer Datenquelle, verschlüsselt (AES-256-GCM); der Master-Key liegt nie in der Datenbank.
CREATE TABLE IF NOT EXISTS data_source_secret (
  source_id   INTEGER PRIMARY KEY REFERENCES data_source(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL DEFAULT 'api_key',
  ciphertext  BLOB NOT NULL,                      -- PFC1 ‖ Key-ID ‖ Nonce ‖ Chiffrat+Tag
  key_id      TEXT NOT NULL,                      -- Kennung des Master-Keys (kein Rückschluss auf ihn)
  hint        TEXT,                               -- letzte 4 Zeichen zur Wiedererkennung
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
ALTER TABLE data_source ADD COLUMN key_expires_on TEXT;   -- Ablaufdatum laut Anbieter (Angabe des Nutzers)
ALTER TABLE data_source ADD COLUMN last_check_json TEXT;  -- Ergebnis der letzten Verbindungsprüfung je Recht
ALTER TABLE data_source ADD COLUMN coverage_json TEXT;    -- Abdeckung des letzten Abrufs (Zeitraum, Seiten, Grenzen)
ALTER TABLE data_source_run ADD COLUMN rows_unclear INTEGER NOT NULL DEFAULT 0;
ALTER TABLE data_source_run ADD COLUMN rows_ignored INTEGER NOT NULL DEFAULT 0;
ALTER TABLE data_source_run ADD COLUMN detail_json TEXT;
-- Entscheidungen je Anbieter-Ereignis (z. B. „dauerhaft ignorieren“) – unabhängig von Stapel und Datenquelle.
CREATE TABLE IF NOT EXISTS event_decision (
  event_key  TEXT PRIMARY KEY,
  decision   TEXT NOT NULL,                       -- ignore
  reason     TEXT,
  batch_id   INTEGER,
  decided_at TEXT NOT NULL
);
-- Weitere Anbieter-IDs je Buchung (Bitpanda: Operation, Trade, Transaktionen) für den exakten Abgleich.
CREATE TABLE IF NOT EXISTS journal_event_alias (
  key    TEXT NOT NULL,
  tx_id  TEXT NOT NULL,
  PRIMARY KEY (key, tx_id)
);
-- Abgleich kuratierter Import ↔ Journal: Entscheidung je Paar (covered = Import-Buchung gilt | distinct).
CREATE TABLE IF NOT EXISTS journal_import_link (
  journal_tx_id TEXT NOT NULL,
  import_tx_id  TEXT NOT NULL,
  decision      TEXT NOT NULL,
  decided_at    TEXT NOT NULL,
  PRIMARY KEY (journal_tx_id, import_tx_id)
);
-- Stammdaten entfernter Assets/Währungen (z. B. Bitpanda-UUID → Symbol) – spart wiederholte Abrufe.
CREATE TABLE IF NOT EXISTS ds_asset_cache (
  provider   TEXT NOT NULL,
  remote_id  TEXT NOT NULL,
  kind       TEXT NOT NULL,                       -- asset | currency
  symbol     TEXT,
  name       TEXT,
  asset_type TEXT,
  isin       TEXT,
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (provider, remote_id)
);
"""),
    (9, """
-- Änderungen und Löschungen an Buchungen des kuratierten Imports (Overlay – die Import-Datei bleibt unverändert).
CREATE TABLE IF NOT EXISTS tx_override (
  tx_id       TEXT PRIMARY KEY,
  action      TEXT NOT NULL,                      -- edit | delete
  row_json    TEXT,                               -- bearbeitete Buchung (Spalten wie transactions.csv)
  form_json   TEXT,                               -- Formularwerte (Expertenmodus) zum erneuten Bearbeiten
  base_json   TEXT NOT NULL,                      -- Import-Fassung beim Ändern (Hinweis, wenn ein Import sie ändert)
  import_id   INTEGER,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
-- Zusatzdaten eines Portfolia-Exports (Ordner portfolia/ der Import-ZIP): Einstellungen, Zuordnungen, Kurshistorie.
CREATE TABLE IF NOT EXISTS import_extra (
  import_id  INTEGER NOT NULL,
  name       TEXT NOT NULL,
  data       BLOB NOT NULL,
  PRIMARY KEY (import_id, name)
);
"""),
    (10, """
-- Wallets (nur lesend): Gruppe (z. B. „Ledger“), Beobachtungsdaten (öffentliche Adressen bzw. Kontoschlüssel, Anbieter,
-- stabile Kontokennung – nie private Schlüssel), Fortschritt des laufenden/letzten Abrufs.
ALTER TABLE data_source ADD COLUMN wallet_group TEXT;
ALTER TABLE data_source ADD COLUMN watch_json TEXT;
ALTER TABLE data_source ADD COLUMN progress_json TEXT;
-- Beobachtete Bestände laut Anbieter (Plausibilitätsprüfung gegen die Portfolia-Buchungen des Kontos).
CREATE TABLE IF NOT EXISTS ds_balance (
  source_id   INTEGER NOT NULL REFERENCES data_source(id) ON DELETE CASCADE,
  asset_key   TEXT NOT NULL,                      -- Symbol bzw. Token-Schlüssel wie in den Vorgängen (USDC@ETH:0x…)
  qty         TEXT NOT NULL,                      -- exakt (Decimal als Text)
  name        TEXT,
  note        TEXT,
  observed_at TEXT NOT NULL,
  PRIMARY KEY (source_id, asset_key)
);
-- API-Keys je Anbieter (Etherscan, Routescan, Helius …), verschlüsselt wie data_source_secret.
CREATE TABLE IF NOT EXISTS provider_secret (
  provider    TEXT PRIMARY KEY,
  ciphertext  BLOB NOT NULL,
  key_id      TEXT NOT NULL,
  hint        TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
"""),
    (11, """
-- Herkunft einer Symbol-Zuordnung: NULL = vom Nutzer, 'abgleich' = aus gleicher Blockchain-Transaktion im
-- kuratierten Import bzw. in App-Buchungen abgeleitet (Anzeige, jederzeit löschbar)
ALTER TABLE csv_symbol ADD COLUMN origin TEXT;
"""),
    (12, """
-- Entscheidungen zu Befunden der Diagnose: übernommene Korrektur (fix, mit Vorher-Zustand je Änderung für
-- „Rückgängig“) oder „geprüft, kein Handlungsbedarf“ (dismiss, gilt nur solange die Befunddaten gleich bleiben).
CREATE TABLE IF NOT EXISTS diag_decision (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  finding_id   TEXT NOT NULL,
  kind         TEXT NOT NULL,                     -- Befundart
  title        TEXT NOT NULL,                     -- Titel des Befunds beim Entscheiden
  action       TEXT NOT NULL,                     -- fix | dismiss
  option       TEXT,                              -- gewählte Lösung (fix)
  option_label TEXT,
  params_json  TEXT,
  ops_json     TEXT,                              -- ausgeführte Änderungen mit Vorher-Zustand
  fingerprint  TEXT,                              -- Prüfsumme der Befunddaten beim Entscheiden
  note         TEXT,
  status       TEXT NOT NULL DEFAULT 'active',    -- active | undone
  created_at   TEXT NOT NULL,
  undone_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_diag_decision_finding ON diag_decision(finding_id, status);
"""),
    (13, """
-- Importprüfung (additiv, bestehende Tabellen und Daten bleiben unverändert):
-- Stapelaktionen mit Vorher-Zustand je Änderung (Protokoll, Rückgängig, Schutz vor doppelter Ausführung).
CREATE TABLE IF NOT EXISTS import_action (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  token        TEXT NOT NULL UNIQUE,              -- aus der Vorschau: dieselbe Aktion wird nie zweimal ausgeführt
  batch_id     INTEGER NOT NULL,                  -- Prüf-Stapel (ohne Fremdschlüssel: Protokoll bleibt)
  action       TEXT NOT NULL,                     -- suggest | link | skip | include | ignore | reset
  label        TEXT NOT NULL,
  params_json  TEXT,                              -- Auswahl und Filter beim Ausführen
  ops_json     TEXT NOT NULL,                     -- ausgeführte Änderungen mit Vorher-Zustand
  summary_json TEXT,                              -- Anzahl je Wirkung, Bestandswirkung
  status       TEXT NOT NULL DEFAULT 'active',    -- active | undone
  created_at   TEXT NOT NULL,
  undone_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_import_action_batch ON import_action(batch_id, id);
-- Verknüpfte Quelldatensätze je Buchung: derselbe Vorgang aus einer weiteren Quelle, mit seinen Werten und der
-- Bewertung beim Verknüpfen – Herkunft statt Zusammenführung; die Buchung selbst bleibt unverändert.
CREATE TABLE IF NOT EXISTS tx_link (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  tx_id           TEXT NOT NULL,                  -- vorhandene Buchung (Import oder App)
  source          TEXT NOT NULL,                  -- Quelle des Datensatzes (sync:bitpanda, csv:koinly …)
  ext_id          TEXT,                           -- Kennung des Datensatzes in der Quelle
  event_key       TEXT,
  role            TEXT,                           -- same | out | in | part
  batch_id        INTEGER,
  row_idx         INTEGER,
  action_id       INTEGER,
  record_json     TEXT NOT NULL,                  -- Werte der Quelle (Zeit, Art, Beine, Gebühr, EUR-Wert, Hash, IDs)
  assessment_json TEXT,                           -- Abgleich beim Verknüpfen (Ergebnis, Belege, Abweichungen)
  status          TEXT NOT NULL DEFAULT 'active', -- active | undone
  created_at      TEXT NOT NULL,
  undone_at       TEXT
);
CREATE INDEX IF NOT EXISTS ix_tx_link_tx ON tx_link(tx_id, status);
CREATE INDEX IF NOT EXISTS ix_tx_link_src ON tx_link(source, ext_id);
"""),
    (14, """
-- Kursqualität der Historie (additiv): Ersatzanbieter für die Zeit vor dem Fenster des Hauptanbieters (z. B. CoinGecko-
-- Demo: 365 Tage) mit Ergebnis der Identitätsprüfung im Überlappungszeitraum.
ALTER TABLE series_meta ADD COLUMN alt_series TEXT;      -- z. B. yahoo:ADA-EUR
ALTER TABLE series_meta ADD COLUMN alt_status TEXT;      -- ok | rejected | none | error
ALTER TABLE series_meta ADD COLUMN alt_note TEXT;
ALTER TABLE series_meta ADD COLUMN alt_checked_at TEXT;
-- Abschnitte gehaltener Positionen ohne Marktkurs des Hauptanbieters (bei jeder vollständigen Neuberechnung der
-- Historie ersetzt): alternativer Anbieter, fortgeschrieben, Schätzung (Transaktions-/manueller/erster Marktkurs),
-- kein Kurs.
CREATE TABLE IF NOT EXISTS price_gap (
  asset_id    TEXT NOT NULL,
  kind        TEXT NOT NULL,                     -- alt | interp | tx | manual | first | none
  method      TEXT NOT NULL,                     -- Anzeige der Ersatzmethode
  source      TEXT,                              -- Datenquelle (z. B. yahoo:ADA-EUR, Transaktionen)
  date_from   TEXT NOT NULL,
  date_to     TEXT NOT NULL,
  days        INTEGER NOT NULL,                  -- gehaltene Tage im Abschnitt
  computed_at TEXT NOT NULL,
  PRIMARY KEY (asset_id, date_from, kind)
);
"""),
    (15, """
-- Watchlists (mehrere möglich, eine Standardliste) – beobachtete Werte ohne Bestand; Kurse über dieselbe
-- Marktdaten-Ablage wie das Portfolio (quote_latest, price_daily, series_meta).
CREATE TABLE IF NOT EXISTS watchlist (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,
  position    INTEGER NOT NULL DEFAULT 0,
  is_default  INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watchlist_item (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  list_id      INTEGER NOT NULL REFERENCES watchlist(id) ON DELETE CASCADE,
  quote_source TEXT NOT NULL,                     -- coingecko | yahoo
  quote_id     TEXT NOT NULL,                     -- CoinGecko-ID bzw. Yahoo-Symbol
  asset_class  TEXT NOT NULL DEFAULT 'crypto',    -- crypto | security
  asset_id     TEXT,                              -- zugehöriges Portfolio-Asset (falls vorhanden)
  symbol       TEXT,
  name         TEXT,
  position     INTEGER NOT NULL DEFAULT 0,
  note         TEXT,
  added_at     TEXT NOT NULL,
  UNIQUE (list_id, quote_source, quote_id)
);
CREATE INDEX IF NOT EXISTS ix_watchlist_item_list ON watchlist_item(list_id, position);
"""),
    (16, """
-- Steuerdaten je Steuerjahr (externe Steuerberichte, z. B. Blockpit/Koinly/eigene JSON/CSV): genau eine aktive Datei
-- je Jahr (Unique-Index), ersetzte bzw. entfernte Fassungen bleiben zur Nachvollziehbarkeit erhalten.
CREATE TABLE IF NOT EXISTS tax_file (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  tax_year      INTEGER,                           -- NULL, solange das Jahr nicht eindeutig ist (pending)
  filename      TEXT NOT NULL,
  origin        TEXT NOT NULL,                     -- folder | upload
  path          TEXT,                              -- gespeicherte Datei (Ordner bzw. Upload-Ablage)
  sha256        TEXT NOT NULL,
  size          INTEGER NOT NULL,
  format        TEXT NOT NULL,                     -- json | csv
  parser        TEXT NOT NULL,
  status        TEXT NOT NULL,                     -- pending | active | replaced | removed | rejected
  records       INTEGER NOT NULL DEFAULT 0,
  matched       INTEGER NOT NULL DEFAULT 0,
  unmatched     INTEGER NOT NULL DEFAULT 0,
  conflicts     INTEGER NOT NULL DEFAULT 0,
  years_json    TEXT,                              -- erkannte Jahre (Inhalt, Dateiname)
  warnings_json TEXT,
  errors_json   TEXT,
  created_at    TEXT NOT NULL,                     -- erkannt bzw. hochgeladen
  imported_at   TEXT,                              -- aktiviert
  replaced_at   TEXT,
  replaced_by   INTEGER,
  seen_at       TEXT,                              -- Hinweis „neue Steuerdatei erkannt“ gesehen
  note          TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_tax_file_active ON tax_file(tax_year) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS ix_tax_file_sha ON tax_file(sha256);
CREATE TABLE IF NOT EXISTS tax_record (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  file_id             INTEGER NOT NULL REFERENCES tax_file(id) ON DELETE CASCADE,
  line                INTEGER,
  tax_year            INTEGER,
  transaction_id      TEXT,
  external_id         TEXT,
  asset               TEXT,
  quantity            TEXT,                        -- exakt (Decimal als Text)
  acquisition_date    TEXT,
  disposal_date       TEXT,
  acquisition_cost    TEXT,
  disposal_value      TEXT,
  holding_period_days INTEGER,
  taxable             INTEGER,                     -- 1 | 0 | NULL (unbekannt)
  gain_loss           TEXT,
  tax_category        TEXT,
  source              TEXT,
  comment             TEXT,
  match_status        TEXT NOT NULL DEFAULT 'unmatched',  -- matched | unmatched | conflict
  match_tx            TEXT,
  match_method        TEXT,                        -- external_id | transaction_id | heuristic
  match_note          TEXT
);
CREATE INDEX IF NOT EXISTS ix_tax_record_file ON tax_record(file_id, match_status);
-- Ordnerprüfung: bekannte Dateien (kein erneutes Einlesen bei unveränderter Größe/Änderungszeit)
CREATE TABLE IF NOT EXISTS tax_scan (
  path     TEXT PRIMARY KEY,
  size     INTEGER NOT NULL,
  mtime    TEXT NOT NULL,
  sha256   TEXT,
  file_id  INTEGER,
  seen_at  TEXT NOT NULL,
  message  TEXT
);
"""),
    (17, """
-- Ticker-/Token-Änderungen je Asset: Umbenennung (gleiches Asset, neue Kursquelle/Bezeichnung – wirkt als Overlay)
-- bzw. Umstellung auf ein neues Asset (Kapitalmaßnahme „migration“ je Konto als App-Buchungen, IDs in tx_ids_json).
-- Ausgeblendete Hinweise der Erkennung stehen hier mit kind='hint' und status='dismissed'.
CREATE TABLE IF NOT EXISTS asset_change (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  kind             TEXT NOT NULL,                  -- rename | migration | hint
  old_asset        TEXT NOT NULL,
  new_asset        TEXT,                           -- migration: Ziel-Asset
  ratio            TEXT,                           -- neue Menge je bisheriger Einheit (Decimal als Text)
  effective_date   TEXT,
  old_quote        TEXT,                           -- bisherige Kursquelle (source:id) zur Nachvollziehbarkeit
  new_quote_source TEXT,
  new_quote_id     TEXT,
  new_name         TEXT,
  new_ticker       TEXT,
  status           TEXT NOT NULL,                  -- applied | reverted | dismissed
  origin           TEXT NOT NULL,                  -- user | hint
  hint_key         TEXT,
  tx_ids_json      TEXT,
  alias_created    INTEGER NOT NULL DEFAULT 0,     -- Symbol-Zuordnung (csv_symbol) von dieser Änderung angelegt
  asset_created    INTEGER NOT NULL DEFAULT 0,     -- Ziel-Asset von dieser Änderung angelegt
  note             TEXT,
  created_at       TEXT NOT NULL,
  reverted_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_asset_change_old ON asset_change(old_asset, status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_asset_change_created ON asset_change(kind, old_asset, created_at);
"""),
    (18, """
-- Dokumentimport (M25): hochgeladene Belege (PDF/Bild) mit Herkunft je Feld. Das Original liegt – sofern gespeichert –
-- unter /data/documents/<sha[:2]>/<sha>.<typ>; extrahierter Text (analysis_json) und Original lassen sich löschen,
-- die abgeleiteten Feldbelege (result_json, ohne Volltext) bleiben für die Nachvollziehbarkeit der Buchungen.
CREATE TABLE IF NOT EXISTS document (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  sha256         TEXT NOT NULL UNIQUE,
  filename       TEXT NOT NULL,
  file_type      TEXT NOT NULL,                  -- pdf | png | jpeg | webp
  size           INTEGER NOT NULL,
  pages          INTEGER,
  stored         INTEGER NOT NULL DEFAULT 0,     -- Original lokal gespeichert
  status         TEXT NOT NULL,                  -- queued | analysed | staged | failed
  doc_type       TEXT,
  provider       TEXT,
  ocr            INTEGER NOT NULL DEFAULT 0,
  stack_id       TEXT,                           -- Upload-Stapel
  batch_id       INTEGER,                        -- Prüf-Stapel (csv_batch) der Vorgänge
  analysis_json  TEXT,                           -- Zeilen mit Belegstellen (Volltext; löschbar)
  result_json    TEXT,                           -- Vorgänge, Feldbelege, Recherche-Protokoll (ohne Volltext)
  overrides_json TEXT,                           -- Korrekturen des Nutzers je Vorgang und Feld
  supersedes     INTEGER,                        -- frühere Fassung desselben Belegs (gleiche Kennung, andere Datei)
  error          TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  text_deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_document_stack ON document(stack_id);
CREATE TABLE IF NOT EXISTS document_stack (
  id           TEXT PRIMARY KEY,
  status       TEXT NOT NULL,                    -- running | done | failed | cancelled
  options_json TEXT,
  summary_json TEXT,
  created_at   TEXT NOT NULL,
  finished_at  TEXT
);
"""),
]


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._local = threading.local()
        self._write_lock = threading.RLock()

    # -- Verbindungen --------------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-8000")  # ~8 MB pro Verbindung
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._connect()
            self._local.conn = c
        return c

    def close_thread_conn(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            with contextlib.suppress(Exception):
                c.close()
            self._local.conn = None

    # -- Migration ---------------------------------------------------------------------------
    def migrate(self, target: int | None = None) -> None:
        """Schema auf den neuesten Stand (bzw. bis ``target`` – für Migrationstests) bringen."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        c = self.conn
        current = c.execute("PRAGMA user_version").fetchone()[0]
        for version, sql in MIGRATIONS:
            if version <= current or (target is not None and version > target):
                continue
            script = SCHEMA_FILE.read_text(encoding="utf-8") if sql == "__schema__" else sql
            log.info("DB-Migration auf Version %s", version)
            c.executescript("BEGIN;" + script + f";PRAGMA user_version={version};COMMIT;")
        # Laufende Jobs aus einem vorherigen Prozess sind nicht mehr aktiv.
        c.execute("UPDATE job_status SET running=0 WHERE running=1")

    # -- Helfer -----------------------------------------------------------------------------
    def q(self, sql: str, params: Iterable[Any] | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def q1(self, sql: str, params: Iterable[Any] | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Iterable[Any] | dict = (), default: Any = None) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return default if row is None or row[0] is None else row[0]

    def x(self, sql: str, params: Iterable[Any] | dict = ()) -> sqlite3.Cursor:
        with self._write_lock:
            return self.conn.execute(sql, params)

    def xmany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._write_lock, self.transaction() as c:
            c.executemany(sql, rows)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Schreibtransaktion (BEGIN IMMEDIATE). Verschachtelte Aufrufe laufen in der äußeren mit."""
        c = self.conn
        if c.in_transaction:
            yield c
            return
        with self._write_lock:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            else:
                c.execute("COMMIT")

    # -- app_state ---------------------------------------------------------------------------
    def get_state(self, key: str, default: Any = None) -> Any:
        row = self.q1("SELECT value FROM app_state WHERE key=?", (key,))
        if row is None or row[0] is None:
            return default
        try:
            return json.loads(row[0])
        except (TypeError, ValueError):
            return row[0]

    def set_state(self, key: str, value: Any) -> None:
        self.x(
            "INSERT INTO app_state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def backup_to(self, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        dst = sqlite3.connect(target)
        try:
            self.conn.backup(dst)
        finally:
            dst.close()
