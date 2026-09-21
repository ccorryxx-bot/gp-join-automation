# gp-join-automation

Telegram group-join automation: Bot -> Cloudflare Worker (dedup/queue) -> GitHub Action (Telethon MTProto join).

Full architecture: see roadmap.md (shared separately with Kyaw Gyi - add a copy here if you want it version-controlled).

## Resources (created)

| Resource | Name | ID |
|---|---|---|
| Cloudflare D1 | gp-join-queue-db | 23e72f68-d0b7-40ed-aa38-85b8ddd2a760 |
| Cloudflare KV | GP_JOIN_DEDUP_KV | 4e2d56caaddc44ac958c3c229a43e164 |
| GitHub Action runner | GitHub-hosted | - |
| Telethon account | dedicated (new) - not yet created | - |

## Phase Status

- [x] Phase 1 - repo + D1/KV resource creation
- [x] Phase 2 - webhook-handler Worker (dedup + enqueue)
- [x] Phase 3 - dispatcher Worker (Cron + GitHub dispatch)
- [x] Phase 4 - join.yml GitHub Action (Telethon join + FloodWait handling)
- [x] Phase 5 - /report endpoint + user notification
- [ ] Phase 6 - end-to-end test
- [ ] Phase 7 - production cutover

## Secrets

### Cloudflare Worker (`wrangler secret put <name>`)

| Secret | Used by | Notes |
|---|---|---|
| `TG_BOT_TOKEN` | webhook-handler, /report notify | Telegram Bot API token |
| `TG_WEBHOOK_SECRET` | webhook-handler | Telegram's `secret_token` for webhook spoof protection |
| `GH_PAT` | dispatcher | needs `workflow` scope, triggers join.yml |
| `REPORT_SECRET` | /report | must match GitHub's `REPORT_SECRET` exactly |

### GitHub Actions repo secrets (`gh secret set <name>`)

| Secret | Used by | Notes |
|---|---|---|
| `CLOUDFLARE_API_TOKEN` | deploy.yml | Cloudflare dashboard → My Profile → API Tokens → "Edit Cloudflare Workers" template |
| `CLOUDFLARE_ACCOUNT_ID` | deploy.yml | Cloudflare dashboard sidebar, or `wrangler whoami` |
| `TG_API_ID` | join.yml | from my.telegram.org |
| `TG_API_HASH` | join.yml | from my.telegram.org |
| `TG_SESSION_STRING` | join.yml | generated locally once, dedicated account (see Resources — not yet created), never commit |
| `WORKER_REPORT_URL` | join.yml | Worker's deployed URL, e.g. `https://gp-join-automation.<sub>.workers.dev` |
| `REPORT_SECRET` | join.yml | must match Cloudflare's `REPORT_SECRET` exactly |

## Deploy

Via GitHub Actions CI (`.github/workflows/deploy.yml`, `wrangler deploy`) — not via MCP `cf_worker_deploy` (account-wide overwrite risk, see roadmap section 6). Push to `main` (or run the workflow manually) once `CLOUDFLARE_API_TOKEN`/`CLOUDFLARE_ACCOUNT_ID` are set — the Worker itself has never been deployed yet.
