-- Migration 003 (2026-09-23): Phase 8 "leave muted groups" automation.
-- Every 3 days, per account, leave.yml (MODE=scan) finds groups where this
-- account has been admin-restricted ("muted", not fully kicked), the admin
-- confirms Y/N in Telegram, then leave.yml (MODE=leave) actually leaves the
-- confirmed ones. Two new tables. Applied directly against production D1
-- (gp-join-queue-db, 23e72f68-d0b7-40ed-aa38-85b8ddd2a760) via the
-- Cloudflare D1 MCP tool, same convention as 002_multi_account.sql.

-- One row per scan attempt (one per account per ~3 days).
-- status lifecycle: scanning -> awaiting_confirm -> leaving -> done
--                                                 \-> cancelled
--                    scanning -> failed  (dispatch never reached GitHub)
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

-- One row per muted group a scan found; updated to left/failed once the
-- leave phase processes it.
CREATE TABLE IF NOT EXISTS leave_candidates (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_id    INTEGER NOT NULL REFERENCES leave_scans(id),
  peer_id    TEXT NOT NULL,     -- Telethon dialog id (marked/canonical form), stored as text
  peer_type  TEXT NOT NULL,     -- 'channel' (covers supergroups too, per dialog.is_channel)
  title      TEXT,
  status     TEXT NOT NULL DEFAULT 'pending', -- pending | left | failed
  detail     TEXT,
  updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_leave_candidates_scan ON leave_candidates(scan_id, status);
