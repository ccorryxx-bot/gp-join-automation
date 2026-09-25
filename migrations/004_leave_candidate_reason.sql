-- Migration 004 (2026-09-26): Phase 9 -- leave-scan now also flags groups
-- with fewer than MIN_MEMBERS_THRESHOLD (default 50) total members, not
-- just muted (admin-restricted) ones. This column records which reason(s)
-- a candidate was flagged for so the scan-result message can break the
-- count down instead of lumping everything under "muted". Existing rows
-- predate this feature and were all muted-only candidates, hence the
-- DEFAULT. Applied directly against production D1 (gp-join-queue-db,
-- 23e72f68-d0b7-40ed-aa38-85b8ddd2a760) via the Cloudflare D1 MCP tool,
-- same convention as 002/003.

ALTER TABLE leave_candidates ADD COLUMN reason TEXT NOT NULL DEFAULT 'muted';
