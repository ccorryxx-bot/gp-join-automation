-- Migration 005 (2026-09-28): Phase 10 -- batched join notifications.
--
-- Before this: every URL in a bulk-join batch got its OWN "🔄 in progress"
-- message (dispatchNextQueuedJoinForAccount) AND its own ✅/❌/ℹ️ result
-- message (handleReport / sweepStaleTriggered) -- a 10-link batch produced
-- ~20 separate Telegram messages. This groups every join_queue row created
-- by one account-picker tap into a single "batch" so the admin instead gets
-- exactly ONE "in progress" message and ONE final count summary per batch,
-- no matter how many URLs it contains.
--
-- Applied directly against production D1 (gp-join-queue-db,
-- 23e72f68-d0b7-40ed-aa38-85b8ddd2a760) via the Cloudflare D1 MCP tool,
-- same convention as 002/003/004.

CREATE TABLE IF NOT EXISTS join_batches (
  id                        INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id                   TEXT NOT NULL,
  account                   TEXT NOT NULL,
  total_entries             INTEGER NOT NULL DEFAULT 0, -- every link in the original message (valid + invalid + unsupported)
  to_process                INTEGER NOT NULL DEFAULT 0, -- join_queue rows actually created for this batch (queued + retry_queued) -- resolved_count counts up to this
  already_joined_before     INTEGER NOT NULL DEFAULT 0, -- skipped at intake: already joined/already_member from an earlier batch
  already_requested_before  INTEGER NOT NULL DEFAULT 0, -- skipped at intake: pending_approval from an earlier batch
  in_progress_skip          INTEGER NOT NULL DEFAULT 0, -- skipped at intake: already queued/triggered elsewhere
  invalid_count             INTEGER NOT NULL DEFAULT 0, -- bad link format, never became a join_queue row
  unsupported_count         INTEGER NOT NULL DEFAULT 0, -- t.me/c/... deep link, can't auto-join
  resolved_count            INTEGER NOT NULL DEFAULT 0, -- how many of `to_process` have reported a final outcome so far
  joined_count              INTEGER NOT NULL DEFAULT 0,
  already_member_count      INTEGER NOT NULL DEFAULT 0,
  pending_approval_count    INTEGER NOT NULL DEFAULT 0,
  failed_count              INTEGER NOT NULL DEFAULT 0,
  progress_notified         INTEGER NOT NULL DEFAULT 0, -- 0/1 -- whether the one "🔄 in progress" message has been sent yet
  progress_message_id       INTEGER,                    -- that message's Telegram id, so the final summary EDITS it in place instead of sending a new message
  status                    TEXT NOT NULL DEFAULT 'in_progress', -- 'in_progress' | 'done'
  created_at                INTEGER NOT NULL,
  updated_at                INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_join_batches_status ON join_batches(status);

-- join_queue: tag each row with the batch that created it. NULL for any row
-- already in flight at migration time (pre-existing 'queued'/'triggered'
-- rows) -- the worker code treats a NULL batch_id as "no batch" and falls
-- back to the old one-message-per-row behavior for just that row, so an
-- in-flight join at deploy time still resolves cleanly.
ALTER TABLE join_queue ADD COLUMN batch_id INTEGER;
CREATE INDEX IF NOT EXISTS idx_join_queue_batch_id ON join_queue(batch_id);
