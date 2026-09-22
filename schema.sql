-- gp-join-queue-db schema
-- Applied directly via Cloudflare D1 on 2026-09-22 (see roadmap Phase 6 prep).
-- Kept here for version control / disaster recovery — re-apply with:
--   wrangler d1 execute gp-join-queue-db --remote --file=schema.sql

CREATE TABLE IF NOT EXISTS processed_urls (
  url_normalized TEXT PRIMARY KEY,
  first_seen_at  INTEGER NOT NULL,
  last_status    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS join_queue (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id           TEXT NOT NULL,
  url_normalized    TEXT NOT NULL,
  status            TEXT NOT NULL,
  detail            TEXT,
  created_at        INTEGER NOT NULL,
  updated_at        INTEGER,
  dispatch_attempts INTEGER NOT NULL DEFAULT 0  -- added 2026-09-22: bounds GitHub-dispatch retries (see MAX_DISPATCH_ATTEMPTS)
);

CREATE INDEX IF NOT EXISTS idx_join_queue_status ON join_queue(status, created_at);
