-- Migration 006 (2026-09-28): Phase 12 -- "Extract muted group urls" button
-- on the scan-result message. Stores the t.me URL captured for free during
-- scan (dialog.entity.username, no extra API call) alongside each leave
-- candidate, so the button can read it straight back out of D1 instead of
-- re-scanning or spinning up a second Telethon session. NULL for
-- legacy/private/invite-only groups, which never have a stable username --
-- see scan_leave_candidates()'s comment in scripts/leave_groups.py for why
-- those are left NULL rather than attempted via ExportChatInviteRequest.
-- Existing rows predate this column and get NULL, same convention as
-- 004's DEFAULT for pre-Phase-9 rows. Applied directly against production
-- D1 (gp-join-queue-db, 23e72f68-d0b7-40ed-aa38-85b8ddd2a760) via the
-- Cloudflare D1 MCP tool, same convention as 002/003/004/005.

ALTER TABLE leave_candidates ADD COLUMN url TEXT;
