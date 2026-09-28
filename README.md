# gp-join-automation

Telegram group-join automation: Bot -> Cloudflare Worker (dedup/queue) -> GitHub Action (Telethon MTProto join).

Full architecture: see roadmap.md (shared separately with Kyaw Gyi - add a copy here if you want it version-controlled).

## Resources (created)

| Resource | Name | ID / URL |
|---|---|---|
| Cloudflare D1 | gp-join-queue-db | `23e72f68-d0b7-40ed-aa38-85b8ddd2a760` — schema applied (`schema.sql`): `join_queue`, `processed_urls` tables live |
| Cloudflare KV | GP_JOIN_DEDUP_KV | `4e2d56caaddc44ac958c3c229a43e164` |
| Cloudflare Worker | gp-join-automation | **https://gp-join-automation.ccorryxx.workers.dev** — deployed via CI, live |
| GitHub Action runner | GitHub-hosted | - |
| Telethon account | dedicated ("CH" account — see naming convention below) | credentials set as `CH_TG_*` secrets |

## Phase Status

- [x] Phase 1 - repo + D1/KV resource creation
- [x] Phase 2 - webhook-handler Worker (dedup + enqueue)
- [x] Phase 3 - dispatcher Worker (Cron + GitHub dispatch)
- [x] Phase 4 - join.yml GitHub Action (Telethon join + FloodWait handling)
- [x] Phase 5 - /report endpoint + user notification
- [x] Phase 6a - bulk-URL intake + one-at-a-time pacing
- [x] Phase 6b - end-to-end test (join loop confirmed live after the telethon.sync + report-UA fix)
- [x] Phase 7 - multi-account (CH / JL) + admin gating (live-tested)
- [x] Phase 8 - leave muted/admin-restricted groups automation (repurposed from the original "production cutover" plan — see leave.yml)
- [x] Phase 9 - leave-scan also flags low-member (<50) groups; request-to-join + read-only/paid-message group detection (9d)
- [x] Phase 10 - batched join notifications (2026-09-28) — see below
- [x] Phase 11 - `/account_status` command — per-account group-count snapshot (2026-09-28) — see below
- [x] Phase 12 - "Extract muted group urls" button on the scan-result message (2026-09-28) — see below
- [x] Phase 13 - `/leavescan` "Scan Both" option (2026-09-28) — see below

## Bulk-URL intake (added 2026-09-22)

The bot now accepts a variable-length list in one message instead of exactly one URL:

```
[https://t.me/groupA,https://t.me/+inviteHashB,https://t.me/groupC]
```

A bare single URL (no brackets) still works as before. Behavior:

- **Still strictly one-at-a-time**, enforced at TWO independent layers now:
  1. App-level: `dispatchNextQueuedJoin` refuses to dispatch a new join while another row is `status = 'triggered'` (in flight). This was a real gap before bulk intake existed — the old cron just popped 1 `queued` row/tick with no in-flight check, which only happened to be safe because there was never more than one row queued at a time.
  2. GitHub-level: `.github/workflows/join.yml` now has `concurrency: { group: telegram-join-CH, cancel-in-progress: false }` — a second, independent guarantee that catches what the app-level check can't (e.g. a manual re-run from the Actions tab). `cancel-in-progress` is deliberately `false`: cancelling mid-join can leave Telethon in an inconsistent state, so a run that lands while one is in progress *queues* instead of killing the running one.
- **PeerFlood detection + account-wide pause.** `scripts/join_telegram.py` catches `PeerFloodError` (Telegram's account-level anti-spam signal, distinct from `FloodWaitError`) and reports `detail='peer_flood_detected'`. The Worker responds by pausing the **entire** queue (not just that URL) for `PEER_FLOOD_PAUSE_HOURS` (default 24h, overridable via env var without a redeploy) via the same `dispatch:next_allowed_at` KV key used for normal inter-join pacing.
- **1–3 min random delay between joins** (normal case). After a join resolves (via `/report`, or via the stale-timeout sweep), the Worker writes a random `next_allowed_at` timestamp (KV, key `dispatch:next_allowed_at`) that `dispatchNextQueuedJoin` must clear before picking up the next `queued` row.
- **No queue → workflow just idles.** The cron tick no-ops when `join_queue` has no `queued` rows; when a new URL arrives it's inserted as `queued` and picked up on a later tick — no separate "resume" step needed.
- **Duplicate / already-joined handling** (`enqueueOneUrl` in `src/worker.js`), checked against `processed_urls.last_status`:
  - not seen before → insert + queue normally
  - `joined` / `already_member` → **skipped**, no new Action run triggered (this is the "don't waste an API call on a duplicate" guard)
  - `queued` / `triggered` → **skipped**, already moving through the pipeline
  - `failed` → **re-queued** (worth another attempt)
  - a URL repeated within the *same* incoming message is also deduped before touching the DB a second time
- **Storage:** still plain D1 — no new table. Each URL is one `join_queue` row exactly as before; bulk just means a message can now insert several rows instead of one. `processed_urls` remains the one-row-per-URL dedup/history table.
- **One summary reply per message** (not one reply per URL) — e.g. "✅ Queued — 7 link / ⏭️ Skip (already joined) — 2 link" — so a 10-link batch doesn't flood the chat.
- **FloodWait handling** — already existed in `scripts/join_telegram.py` since Phase 4 (`join_with_floodwait_handling`, auto-sleeps floods up to 120s across up to 3 retries, fails fast past that). No change needed here for bulk intake.

**Not yet live-tested** — needs a real multi-URL message sent to the bot once deployed, watching `join_queue` to confirm rows go `queued → triggered → joined/failed` one at a time with the expected gap between them.

## Multi-account (CH / JL) + admin gating (added 2026-09-22, Phase 7)

**Second Telethon account.** A dedicated account "JL" joins the "CH" naming
convention above (see Secrets). Its credentials live as GitHub Actions
secrets only — `JL_API_ID`, `JL_API_HASH`, `JL_STRING_SESSION` — the
Cloudflare Worker never sees them, it only ever passes an `account` string
("CH" or "JL") through to `join.yml`, which picks the matching secret set.

**Account picker, not auto bulk-join across both accounts.** Sending a URL
(or `[url,url,...]` list) to the bot no longer queues it immediately. The
bot replies with an inline-keyboard prompt — "Account CH" / "Account JL" —
and only enqueues the URLs once the admin taps one. A batch always goes to
ONE account, chosen once per message; it is never automatically split
across both. The parked URL list lives in KV (`pending_urls:{chatId}`, 5 min
TTL) between the prompt and the tap.

**Fully independent per-account queues.** `join_queue` and `processed_urls`
both gained an `account` column (`processed_urls`' primary key is now
`(url_normalized, account)` — see `migrations/002_multi_account.sql` for the
exact statements run against the live DB). This means:
- The same group can be tracked as joined by CH and separately by JL —
  dedup is scoped per account, not global.
- Dispatch pacing (the 1-3 min inter-join cooldown) and PeerFlood pauses are
  keyed per account (`dispatch:next_allowed_at:CH` / `:JL` in KV) — a pause
  on one account never blocks the other.
- `join.yml`'s `concurrency.group` is now `telegram-join-${{ inputs.account }}`
  instead of a single fixed lane, so CH and JL can have Actions runs
  in-flight at the same time without queuing behind each other.

**Admin-only gating.** Every message, `/command`, and inline-button tap is
checked against `ADMIN_TG_ID` (plain var in `wrangler.toml`, not a secret —
it's just a numeric Telegram user ID) via `isAdmin()` in `src/worker.js`.
Anyone else's message is silently dropped — no reply at all.

**Commands (admin-only):**
- `/status` — queue counts (`queued` / `triggered` / `joined` /
  `already_member` / `failed`) broken out per account
- `/help` (also `/start`) — short usage reminder

## Batched join notifications (added 2026-09-28, Phase 10)

Before this, every URL in a batch got its own Telegram message twice over —
once when it started ("🔄 [CH] လုပ်ဆောင်နေပါပြီ...") and once when it
resolved (✅ / ❌ / ℹ️). A 10-link batch meant ~20 separate messages. Now
every join_queue row created by one account-picker tap shares a `batch_id`
(`join_batches` table, `migrations/005_join_batches.sql`), and the batch as
a whole gets exactly **two** messages regardless of size:

1. **One "in progress" message**, sent the first time any row in the batch
   actually dispatches to GitHub Actions (`notifyBatchStarted` — guarded by
   `join_batches.progress_notified` so a 10-row batch only fires this once).
2. **One final summary**, sent once every row in the batch has resolved
   (`resolveBatchForRow`, called from `/report`, the stale-timeout sweep,
   and a dispatch-failure both — every terminal outcome a row can reach):

   ```
   🏁 [CH] ပြီးသွားပါပြီ!

   Total Url - 10
   Already joined - 2
   Join Success - 6
   Join Failed - 1
   Invalid - 1
   ```

   Optional extra lines (pending-approval / unsupported `t.me/c/...` links /
   duplicate-in-flight skips) only appear when their count is non-zero, and
   a failed batch also gets a short list of the actual failed links + reason
   (capped at 10) appended below the counts — so nothing that used to be in
   the per-URL ❌ messages is lost, it's just gathered into the one summary
   instead of spread across N messages.

   The final summary **edits the "in progress" message in place** rather
   than sending a 3rd message (same `editMessageText` pattern already used
   for the account-picker prompt) — falls back to a fresh message only if a
   batch never got as far as dispatching (e.g. every row in it failed
   GitHub's dispatch call itself).

A row with no `batch_id` (only possible for a pre-Phase-10 row still
in-flight at deploy time) transparently falls back to the old
one-message-per-row behavior for just that row.

## `/account_status` command (added 2026-09-28, Phase 11)

Admin-only, read-only group-count snapshot per account. `/account_status`
prompts CH / JL / Both (same inline-keyboard style as `/leavescan`), then
dispatches `leave.yml` with `mode: status` for each chosen account —
`scripts/leave_groups.py`'s `scan_account_status()` reuses the same
muted/read-only/paid-messages detection `scan_leave_candidates()` already
had for the leave-scan flow, but every group lands in exactly one bucket
instead of building a leave-candidate list, and the member-count fallback
(the one check that could cost an extra `get_participants` call per group)
is skipped entirely, since this command has no use for it. Reports back to
`/leave-report` (`mode: status`) once per account run:

```
🏁 [CH] ပြီးသွားပါပြီ!

Total groups - 42
active groups - 35
restricted groups - 2
paid message groups - 1
read only groups - 4
```

"Both" dispatches CH and JL independently — each still gets its own
message when its own run finishes, there's no merged/combined count.
Stateless by design: unlike `/leavescan`, nothing is written to D1 for a
status run (no `leave_scans` row) — there's nothing to confirm or act on
later, so there's nothing to persist beyond a short-lived
`status:running:{account}` KV guard against a double-tap.

## "Extract muted group urls" button (added 2026-09-28, Phase 12)

A `🔗 Extract muted group urls` button now appears on the scan-result
message (above Yes/No) whenever a scan finds at least one `muted`
candidate. Tapping it is a **plain D1 read, nothing else** — no GitHub
Action, no Telethon session, no Telegram API call:

- Every candidate's `url` (`https://t.me/{username}`) is captured **for
  free during the scan itself** (`dialog.entity.username`, already on the
  entity from `iter_dialogs()` — same "already have it, no extra call"
  pattern as the `read_only`/`paid_messages` checks) and stored in
  `leave_candidates.url` (`migrations/006_leave_candidate_url.sql`).
- The button handler (`handleExtractUrlsCallback`) just selects
  `title, url FROM leave_candidates WHERE scan_id = ? AND reason = 'muted'`
  and formats it back to the admin — no re-scan, no second session spin-up.
- Legacy groups and private/invite-only channels never have a username, so
  their `url` is `NULL` — getting a real link for those would mean
  `ExportChatInviteRequest`, which needs "invite users" rights a
  muted/restricted account never has (it would just be a wasted, failing
  call). Those are listed separately by title instead of silently dropped:

  ```
  🔗 [CH] Muted group urls — 4 ခုထဲက 2 ခု url ရပါတယ်:

  1. https://t.me/groupA
  2. https://t.me/groupB

  ⚠️ Private/invite-only group 2 ခု — url မရနိုင်ပါ (username မရှိလို့, join ဖို့ invite link ကိုယ်တိုင်ရှာရပါလိမ့်မယ်):
  1. Group C
  2. Group D
  ```

Sent as a separate message (not an edit) so the original scan-result
message's Yes/No buttons stay intact and tappable afterward.

## `/leavescan` "Scan Both" (added 2026-09-28, Phase 13)

`/leavescan` now offers **Scan CH / Scan JL / Scan Both**, same as
`/account_status`. "Both" just calls `triggerLeaveScan()` once per account —
each account gets its own `leave_scans` row, its own running/awaiting-confirm
guard, its own scan-result message and its own Yes/No confirm (nothing is
merged). CH and JL use different `leave.yml` concurrency groups, so the two
scans run in parallel. If one account is busy (already scanning, or a
previous scan is still awaiting Yes/No), only that account reports "busy" in
the reply; the other still starts.

## Live Verification Log

| Date | Check | Result |
|---|---|---|
| 2026-09-22 | Worker deploy via CI (`deploy.yml`) | ✅ success — live at the workers.dev URL above |
| 2026-09-22 | D1 schema (`join_queue`, `processed_urls`) | ✅ applied directly against production D1, confirmed via `sqlite_master` query |
| 2026-09-22 | Telegram `setWebhook` → Worker URL | ✅ confirmed live — sent `/start` to the bot, got back the "valid group link" rejection reply, proving webhook fired → Worker ran → `TG_BOT_TOKEN` reply succeeded |
| 2026-09-22 | Real invite link → queued → dispatcher → Telethon join → `/report` → user notified (full loop) | ❌ 5/5 attempts (id 1–5) ended `stale_no_report_timeout` — root-caused below, fix pushed, **re-test is next step** |
| 2026-09-22 | Root cause of the 5 failures | ✅ two independent bugs, both confirmed against live data (not guessed): **(1)** `join_telegram.py` imported plain `telethon.TelegramClient` — no `await`/`telethon.sync`, so the join call built a coroutine and silently dropped it, never actually joining. **(2)** `/report` POSTs never reached Worker code — confirmed via direct D1 query, `debug_log` (written at the top of `handleReport()`, pre-auth) had **0 rows** across all 5 attempts, consistent with Cloudflare's Browser Integrity Check 403'ing `urllib`'s default `Python-urllib/3.x` User-Agent at the edge |

**Fix applied:** `scripts/join_telegram.py` now uses `telethon.sync`, sends a browser-style `User-Agent` on the `/report` call, and treats a failed report as a job failure (`exit 1`) even if the join itself succeeded, so silent "green but user never told" runs can't happen again. `debug_log` table is being kept (not dropped yet) until a live re-test confirms the fix.

**Next test step:** send a **new** `t.me/...` or `t.me/+...` group invite link to the bot (not `/start` — this would be attempt #6, first one against the fix), confirm the "✅ Queued" reply, then watch the `join.yml` run in the Actions tab within ~1 minute (cron dispatcher tick). Check `join_queue` for `status='joined'` (or `already_member`) instead of `stale_no_report_timeout`.

## Secrets

### Naming convention (added 2026-09-22)
Telegram-account-specific secrets are prefixed per account — `CH_` for the first/current dedicated account. This is deliberate prep for adding a second Telethon account later (e.g. `ACC2_TG_API_ID`, `ACC2_TG_SESSION_STRING`) without touching existing secrets. Secrets that describe the **pipeline** rather than a specific Telegram account (`WORKER_REPORT_URL`, `REPORT_SECRET`) stay unprefixed — they don't change when a second account is added, since there's still only one Worker and one `/report` trust boundary.

### Cloudflare Worker (`wrangler secret put <name>`) — all set ✅

| Secret | Used by | Notes |
|---|---|---|
| `TG_BOT_TOKEN` | webhook-handler, /report notify | Telegram Bot API token |
| `TG_WEBHOOK_SECRET` | webhook-handler | Telegram's `secret_token` for webhook spoof protection — confirmed matching (webhook test passed) |
| `GH_PAT` | dispatcher | needs `workflow` scope, triggers join.yml |
| `REPORT_SECRET` | /report | must match GitHub's `REPORT_SECRET` exactly — intentionally **not** account-prefixed (see naming convention above) |

### GitHub Actions repo secrets (`gh secret set <name>`) — all 7 set ✅

| Secret | Used by | Notes |
|---|---|---|
| `CLOUDFLARE_API_TOKEN` | deploy.yml | Cloudflare dashboard → My Profile → API Tokens → "Edit Cloudflare Workers" template |
| `CLOUDFLARE_ACCOUNT_ID` | deploy.yml | Cloudflare dashboard sidebar, or `wrangler whoami` |
| `CH_TG_API_ID` | join.yml | from my.telegram.org, dedicated "CH" account |
| `CH_TG_API_HASH` | join.yml | from my.telegram.org, dedicated "CH" account |
| `CH_TG_SESSION_STRING` | join.yml | generated locally once, dedicated "CH" account — never commit |
| `JL_API_ID` | join.yml | from my.telegram.org, dedicated "JL" account — **added to secrets manually 2026-09-22, not via CI** |
| `JL_API_HASH` | join.yml | from my.telegram.org, dedicated "JL" account — **added manually** |
| `JL_STRING_SESSION` | join.yml | generated locally once, dedicated "JL" account — **added manually**, never commit |
| `WORKER_REPORT_URL` | join.yml | `https://gp-join-automation.ccorryxx.workers.dev` |
| `REPORT_SECRET` | join.yml | must match Cloudflare's `REPORT_SECRET` exactly |

> `join.yml` picks between the CH_* and JL_* secret sets based on the `account` workflow input (`inputs.account == 'JL' && secrets.JL_API_ID || secrets.CH_TG_API_ID`, same pattern for the other two) and maps the result to the plain `TG_API_ID` / `TG_API_HASH` / `TG_SESSION_STRING` env vars `scripts/join_telegram.py` expects. Note JL's secret names deliberately don't carry the same `_TG_` / `SESSION_STRING` naming as CH's — they were added as `JL_API_ID` / `JL_API_HASH` / `JL_STRING_SESSION`, and the workflow routes to those exact names.

### Cloudflare Worker plain vars (`wrangler.toml` `[vars]`, deployed via CI)

| Var | Used by | Notes |
|---|---|---|
| `ADMIN_TG_ID` | webhook-handler, callback handler | `7699538187` — every command/message/button tap is gated on this ID |

## Deploy

Via GitHub Actions CI (`.github/workflows/deploy.yml`, `wrangler deploy`) — not via MCP `cf_worker_deploy` (account-wide overwrite risk, see roadmap section 6). Push to `main` (or run the workflow manually) triggers a redeploy on changes to `src/**`, `wrangler.toml`, or the deploy workflow itself. **Worker is live** as of 2026-09-22.
