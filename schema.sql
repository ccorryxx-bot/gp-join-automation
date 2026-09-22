-- gp-join-queue-db schema
-- Applied directly via Cloudflare D1 (see README Live Verification Log for
-- dates). Kept here for version control / disaster recovery — re-apply
-- against a FRESH database with:
--   wrangler d1 execute gp-join-queue-db --remote --file=schema.sql
-- NOTE: this file is the target end-state, not a migration script. The
-- live DB was already populated when the Phase 7 columns were added, so
-- that change was applied as ALTER/rebuild statements directly (see
-- migrations/002_multi_account.sql) rather than by re-running this file.

CREATE TABLE IF NOT EXISTS processed_urls (
  url_normalized TEXT NOT NULL,
  account        TEXT NOT NULL,  -- added 2026-09-22 (Phase 7): 'CH' / 'JL' —
                                  -- same group tracked independently per account
  first_seen_at  INTEGER NOT NULL,
  last_status    TEXT NOT NULL,
  PRIMARY KEY (url_normalized, account)
);

CREATE TABLE IF NOT EXISTS join_queue (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id           TEXT NOT NULL,
  url_normalized    TEXT NOT NULL,
  account           TEXT NOT NULL DEFAULT 'CH',  -- added 2026-09-22 (Phase 7)
  status            TEXT NOT NULL,
  detail            TEXT,
  created_at        INTEGER NOT NULL,
  updated_at        INTEGER,
  dispatch_attempts INTEGER NOT NULL DEFAULT 0  -- bounds GitHub-dispatch retries (see MAX_DISPATCH_ATTEMPTS)
);

CREATE INDEX IF NOT EXISTS idx_join_queue_account_status ON join_queue(account, status, created_at);

-- TEMP (added 2026-09-22): captures every request that reaches /report,
-- pass or fail, while debugging stale_no_report_timeout. Never stores the
-- actual secret values -- only presence/length/match booleans. Drop this
-- table once the root cause is confirmed fixed:
--   DROP TABLE debug_log;
CREATE TABLE IF NOT EXISTS debug_log (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                    INTEGER NOT NULL,
  method                TEXT,
  has_secret_header     INTEGER,
  secret_len            INTEGER,
  secret_matches        INTEGER,
  has_report_secret_env INTEGER,
  body_raw              TEXT
);

-- added 2026-09-23 (Phase 8): "leave muted groups" automation — see
-- migrations/003_leave_scans.sql for the full write-up.
CREATE TABLE IF NOT EXISTS leave_scans (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  account           TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'scanning',
  total_dialogs     INTEGER,
  muted_count       INTEGER,
  left_count        INTEGER NOT NULL DEFAULT 0,
  failed_count      INTEGER NOT NULL DEFAULT 0,
  dispatch_attempts INTEGER NOT NULL DEFAULT 0,
  created_at        INTEGER NOT NULL,
  updated_at        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_leave_scans_account_status ON leave_scans(account, status, created_at);

CREATE TABLE IF NOT EXISTS leave_candidates (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_id    INTEGER NOT NULL REFERENCES leave_scans(id),
  peer_id    TEXT NOT NULL,
  peer_type  TEXT NOT NULL,
  title      TEXT,
  status     TEXT NOT NULL DEFAULT 'pending',
  detail     TEXT,
  updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_leave_candidates_scan ON leave_candidates(scan_id, status);
