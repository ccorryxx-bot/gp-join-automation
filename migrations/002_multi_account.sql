-- Migration 002 (2026-09-22): Phase 7 multi-account (CH / JL) support.
--
-- Applied ONCE directly against production D1 (gp-join-queue-db,
-- 23e72f68-d0b7-40ed-aa38-85b8ddd2a760) via the Cloudflare D1 MCP tool,
-- not through this file (D1 has no migration runner) — kept here purely
-- as a record of what was run and why, matching the existing project
-- convention of applying schema changes directly and documenting them.
--
-- Pre-migration snapshot: join_queue = 6 rows, processed_urls = 6 rows,
-- all pre-existing joins were made by the CH account (the only account
-- that existed before this migration), so every row backfills as 'CH'.
--
-- 1. join_queue: plain ADD COLUMN, defaults existing rows to 'CH'.
ALTER TABLE join_queue ADD COLUMN account TEXT NOT NULL DEFAULT 'CH';
CREATE INDEX IF NOT EXISTS idx_join_queue_account_status ON join_queue(account, status, created_at);

-- 2. processed_urls: needs a real rebuild, not just ADD COLUMN — its
--    PRIMARY KEY was url_normalized alone, which can't allow the same
--    group to be tracked as joined by CH and separately by JL. Rebuilt
--    with a composite (url_normalized, account) key, backfilling existing
--    rows as 'CH'.
CREATE TABLE processed_urls_new (
  url_normalized TEXT NOT NULL,
  account        TEXT NOT NULL,
  first_seen_at  INTEGER NOT NULL,
  last_status    TEXT NOT NULL,
  PRIMARY KEY (url_normalized, account)
);
INSERT INTO processed_urls_new (url_normalized, account, first_seen_at, last_status)
  SELECT url_normalized, 'CH', first_seen_at, last_status FROM processed_urls;
DROP TABLE processed_urls;
ALTER TABLE processed_urls_new RENAME TO processed_urls;

-- Post-migration verification: SELECT url_normalized, account, last_status
-- FROM processed_urls — confirmed all 6 rows present with account='CH'.
