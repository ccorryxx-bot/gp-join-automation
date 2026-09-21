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

## Secrets to set before Phase 2 deploy

```
wrangler secret put TG_BOT_TOKEN
wrangler secret put TG_WEBHOOK_SECRET
wrangler secret put GH_PAT
```

## Secret to set before Phase 5 deploy (Cloudflare Worker)

```
wrangler secret put REPORT_SECRET   # shared with GitHub's REPORT_SECRET below — must match exactly
```

## Secrets to set before Phase 4 runs (GitHub repo secrets, not wrangler)

Needs a dedicated Telethon account (see Resources table — not yet created).
Once it exists, generate a StringSession locally (one-off, `telethon` installed
locally, log in once, print `StringSession.save(client.session)`) and add:

```
gh secret set TG_API_ID            # from my.telegram.org
gh secret set TG_API_HASH          # from my.telegram.org
gh secret set TG_SESSION_STRING    # generated locally, never commit this
gh secret set WORKER_REPORT_URL    # e.g. https://gp-join-automation.<sub>.workers.dev
gh secret set REPORT_SECRET        # must match Cloudflare's REPORT_SECRET exactly
```

## Deploy

Via GitHub Actions CI (wrangler deploy) - not via MCP cf_worker_deploy (account-wide overwrite risk, see roadmap section 6).
