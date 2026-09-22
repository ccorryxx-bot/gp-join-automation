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
- [ ] Phase 6 - end-to-end test **(in progress — webhook leg verified live, see Live Verification Log)**
- [ ] Phase 7 - production cutover

## Live Verification Log

| Date | Check | Result |
|---|---|---|
| 2026-09-22 | Worker deploy via CI (`deploy.yml`) | ✅ success — live at the workers.dev URL above |
| 2026-09-22 | D1 schema (`join_queue`, `processed_urls`) | ✅ applied directly against production D1, confirmed via `sqlite_master` query |
| 2026-09-22 | Telegram `setWebhook` → Worker URL | ✅ confirmed live — sent `/start` to the bot, got back the "valid group link" rejection reply, proving webhook fired → Worker ran → `TG_BOT_TOKEN` reply succeeded |
| 2026-09-22 | Real invite link → queued → dispatcher → Telethon join → `/report` → user notified (full loop) | ⏳ not yet tested — **next step** |

**Next test step:** send an actual `t.me/...` or `t.me/+...` group invite link to the bot (not `/start`), confirm the "✅ Queued" reply, then watch the `join.yml` workflow run appear in the Actions tab within ~1 minute (cron dispatcher tick).

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
| `WORKER_REPORT_URL` | join.yml | `https://gp-join-automation.ccorryxx.workers.dev` |
| `REPORT_SECRET` | join.yml | must match Cloudflare's `REPORT_SECRET` exactly |

> `join.yml` reads these via `secrets.CH_TG_API_ID` / `secrets.CH_TG_API_HASH` / `secrets.CH_TG_SESSION_STRING` and maps them to the plain `TG_API_ID` / `TG_API_HASH` / `TG_SESSION_STRING` env vars that `scripts/join_telegram.py` expects — the account prefix lives only in the secret name, not in the script.

## Deploy

Via GitHub Actions CI (`.github/workflows/deploy.yml`, `wrangler deploy`) — not via MCP `cf_worker_deploy` (account-wide overwrite risk, see roadmap section 6). Push to `main` (or run the workflow manually) triggers a redeploy on changes to `src/**`, `wrangler.toml`, or the deploy workflow itself. **Worker is live** as of 2026-09-22.
