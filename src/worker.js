// gp-join-automation — Cloudflare Worker
// Phase 2: webhook-handler (dedup + enqueue + instant-ack)
// Phase 3: dispatcher (Cron Trigger -> GitHub workflow_dispatch)
// Phase 4: join.yml Telethon join logic — see scripts/join_telegram.py
// Phase 5: /report endpoint (join_queue update + processed_urls sync + user notify)

const KV_DEDUP_TTL_SECONDS = 600; // 10 min — covers Telegram's webhook retry window
const REPORT_STATUSES = new Set(["joined", "already_member", "failed"]);

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (request.method !== "POST") {
      return new Response("gp-join-automation", { status: 200 });
    }

    if (url.pathname === "/report") {
      return handleReport(request, env);
    }

    return handleTelegramWebhook(request, env, ctx);
  },

  async scheduled(event, env, ctx) {
    // Order matters: sweep stale rows first so a freshly-dispatched row from
    // *this* tick is never accidentally caught by the same sweep.
    await sweepStaleTriggered(env);
    await dispatchNextQueuedJoin(env);
  },
};

const GITHUB_REPO = "ccorryxx-bot/gp-join-automation";

// A dispatch is retried on the next tick if it fails (GitHub API down, bad
// token, network blip). After this many attempts we stop retrying silently
// and tell the user instead — no user request should retry forever with no
// feedback.
const MAX_DISPATCH_ATTEMPTS = 3;

// join.yml has `timeout-minutes: 10`. This sweep threshold is a generous
// buffer on top of that (GitHub-hosted runner queue delay, /report call
// itself failing, etc.) before we give up waiting for the Action to report
// back and tell the user something went wrong instead of leaving them with
// no answer at all.
const STALE_TRIGGERED_TIMEOUT_MS = 15 * 60 * 1000; // 15 min

// Cron fires every 1 min (Cloudflare's minimum granularity) and this processes
// exactly ONE queued row per tick — that alone gives ~60s natural pacing
// between joins, which is more conservative than the 45s target in the roadmap.
// No internal sleep/loop needed.
async function dispatchNextQueuedJoin(env) {
  const row = await env.DB.prepare(
    "SELECT id, chat_id, url_normalized, dispatch_attempts FROM join_queue WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
  ).first();

  if (!row) return; // nothing queued — no-op tick

  const joinUrl = denormalizeToJoinUrl(row.url_normalized);
  if (!joinUrl) {
    // Shouldn't happen given normalizeTelegramInviteUrl's output format, but guard anyway.
    await env.DB.prepare(
      "UPDATE join_queue SET status = 'failed', detail = ?, updated_at = ? WHERE id = ?"
    ).bind("denormalize_failed", Date.now(), row.id).run();
    await replyToUser(env, row.chat_id, reportMessage("failed", "denormalize_failed"));
    return;
  }

  let dispatchRes = null;
  let dispatchErrText = "";
  try {
    dispatchRes = await fetch(
      `https://api.github.com/repos/${GITHUB_REPO}/actions/workflows/join.yml/dispatches`,
      {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${env.GH_PAT}`,
          "Accept": "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "gp-join-automation-dispatcher",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          ref: "main",
          inputs: { group_url: joinUrl, queue_id: String(row.id) },
        }),
      }
    );
  } catch (err) {
    // Network-level failure (fetch threw) — treat the same as a bad status below.
    dispatchErrText = `fetch_threw: ${err}`;
  }

  if (dispatchRes && dispatchRes.status === 204) {
    // GitHub returns 204 No Content on a successful dispatch.
    await env.DB.prepare(
      "UPDATE join_queue SET status = 'triggered', updated_at = ? WHERE id = ?"
    ).bind(Date.now(), row.id).run();
    // Immediate feedback — the user's last message was "✅ Queued" up to ~60s
    // ago; without this, the chat looks dead until the Action finishes.
    await replyToUser(env, row.chat_id, "🔄 လုပ်ဆောင်နေပါပြီ — Group ထဲ join ဝင်ဖို့ ကြိုးစားနေပါတယ်...");
    return;
  }

  if (dispatchRes) {
    dispatchErrText = `${dispatchRes.status} ${await dispatchRes.text()}`;
  }
  console.error("workflow_dispatch failed:", dispatchErrText);

  const attempts = (row.dispatch_attempts || 0) + 1;
  if (attempts >= MAX_DISPATCH_ATTEMPTS) {
    // Stop retrying silently — this is exactly the kind of failure that used
    // to loop forever with the row stuck at 'queued' and no one ever told.
    await env.DB.prepare(
      "UPDATE join_queue SET status = 'failed', detail = ?, dispatch_attempts = ?, updated_at = ? WHERE id = ?"
    ).bind(`github_dispatch_failed:${dispatchErrText}`.slice(0, 200), attempts, Date.now(), row.id).run();
    await replyToUser(env, row.chat_id, reportMessage("failed", "github_dispatch_failed"));
  } else {
    // Still under budget — stays 'queued', next tick retries automatically.
    await env.DB.prepare(
      "UPDATE join_queue SET dispatch_attempts = ?, updated_at = ? WHERE id = ?"
    ).bind(attempts, Date.now(), row.id).run();
  }
}

// Catches the case the roadmap flagged as a known gap: the Action crashes,
// times out, or its /report call itself fails to reach us, leaving a row
// stuck at 'triggered' forever with the user never told either way.
async function sweepStaleTriggered(env) {
  const cutoff = Date.now() - STALE_TRIGGERED_TIMEOUT_MS;
  const { results } = await env.DB.prepare(
    "SELECT id, chat_id FROM join_queue WHERE status = 'triggered' AND updated_at < ?"
  ).bind(cutoff).all();

  for (const row of results) {
    await env.DB.prepare(
      "UPDATE join_queue SET status = 'failed', detail = 'stale_no_report_timeout', updated_at = ? WHERE id = ?"
    ).bind(Date.now(), row.id).run();
    await replyToUser(env, row.chat_id, reportMessage("failed", "stale_no_report_timeout"));
  }
}

// Inverse of normalizeTelegramInviteUrl — deterministic, so no raw-URL column needed.
function denormalizeToJoinUrl(normalized) {
  if (normalized.startsWith("invite:")) {
    return `https://t.me/+${normalized.slice("invite:".length)}`;
  }
  if (normalized.startsWith("username:")) {
    return `https://t.me/${normalized.slice("username:".length)}`;
  }
  return null;
}

// Phase 5: called by scripts/join_telegram.py after the Action attempts a join.
// Body: { queue_id, status: "joined"|"already_member"|"failed", detail? }
async function handleReport(request, env) {
  const ts = Date.now();
  const incomingSecret = request.headers.get("X-Report-Secret");
  const hasSecretHeader = incomingSecret !== null;
  const secretLen = incomingSecret ? incomingSecret.length : 0;
  const hasEnvSecret = !!env.REPORT_SECRET;
  const secretMatches = hasEnvSecret && incomingSecret === env.REPORT_SECRET;
  const rawBody = await request.text();

  // TEMP DIAGNOSTIC (added while debugging stale_no_report_timeout): log
  // every request that reaches this handler -- pass or fail -- so a secret
  // mismatch, a malformed body, or "the request never arrived at all" is
  // measured from D1 afterward instead of guessed at. Never logs the actual
  // secret values, only presence/length/match booleans. Remove once the
  // root cause is confirmed fixed.
  try {
    await env.DB.prepare(
      "INSERT INTO debug_log (ts, method, has_secret_header, secret_len, secret_matches, has_report_secret_env, body_raw) VALUES (?, ?, ?, ?, ?, ?, ?)"
    ).bind(ts, request.method, hasSecretHeader ? 1 : 0, secretLen, secretMatches ? 1 : 0, hasEnvSecret ? 1 : 0, rawBody.slice(0, 500)).run();
  } catch (e) {
    console.error("debug_log insert failed:", e);
  }

  if (!secretMatches) {
    return new Response("forbidden", { status: 403 });
  }

  let body;
  try {
    body = JSON.parse(rawBody);
  } catch {
    return new Response("bad request", { status: 400 });
  }

  const id = Number(body?.queue_id);
  const status = body?.status;
  const detail = typeof body?.detail === "string" ? body.detail : "";

  if (!Number.isInteger(id) || !REPORT_STATUSES.has(status)) {
    return new Response("bad request", { status: 400 });
  }

  const row = await env.DB.prepare(
    "SELECT chat_id, url_normalized, status AS current_status FROM join_queue WHERE id = ?"
  ).bind(id).first();

  if (!row) {
    return new Response("not found", { status: 404 });
  }

  // Idempotent: a retried/duplicate /report call for an already-closed row
  // is ack'd without re-updating state or re-notifying the user.
  if (row.current_status !== "triggered") {
    return new Response("OK", { status: 200 });
  }

  const now = Date.now();
  await env.DB.batch([
    env.DB
      .prepare("UPDATE join_queue SET status = ?, detail = ?, updated_at = ? WHERE id = ?")
      .bind(status, detail, now, id),
    env.DB
      .prepare("UPDATE processed_urls SET last_status = ? WHERE url_normalized = ?")
      .bind(status, row.url_normalized),
  ]);

  await replyToUser(env, row.chat_id, reportMessage(status, detail));

  return new Response("OK", { status: 200 });
}

// Maps scripts/join_telegram.py's `detail` strings (and this file's own
// failure codes) to a plain-language reason — matches by prefix since a few
// codes carry a dynamic suffix (e.g. floodwait_45s_exceeded_budget,
// rpc_error_SomeRpcName, unexpected_error:TypeError:...).
const FRIENDLY_DETAIL_PREFIXES = [
  ["floodwait_", "Telegram rate limit ကြောင့် ခဏစောင့်ဖို့ လိုအပ်ပါတယ် (FloodWait)"],
  ["invite_hash_invalid_or_expired", "Invite link ကုန်သွားပြီ (သို့) မမှန်ကန်တော့ပါ"],
  ["account_joined_too_many_channels", "Account က group အများဆုံး ဝင်ပြီးသား ဖြစ်နေပါတယ်"],
  ["channel_private_or_kicked", "Group က private ဖြစ်နေတယ် (သို့) ဒီ account ကို ထုတ်ထားပါတယ်"],
  ["user_banned_in_channel", "ဒီ account ကို group ထဲက banned ဖြစ်ထားပါတယ်"],
  ["rpc_error_", "Telegram API ကနေ error ပြန်ပေးလိုက်ပါတယ်"],
  ["denormalize_failed", "Link format ကို ပြန်ပြင်လို့ မရဘူး (internal bug — dev ကို report ပါ)"],
  ["github_dispatch_failed", "GitHub Action ကို trigger လုပ်လို့ မရဘူး (GH_PAT / network ပြဿနာ ဖြစ်နိုင်ပါတယ်)"],
  ["stale_no_report_timeout", "Action run ပြီးလား အတည်မပြုနိုင်ဘဲ အချိန်ကုန်သွားပါပြီ"],
  ["unexpected_error:", "မမျှော်လင့်ထားတဲ့ error တစ်ခု ဖြစ်ပွားခဲ့ပါတယ်"],
];

function friendlyDetail(detail) {
  if (!detail) return "အကြောင်းအရင်း မသိရပါ";
  const match = FRIENDLY_DETAIL_PREFIXES.find(([prefix]) => detail.startsWith(prefix));
  // Always keep the raw code alongside the friendly text — useful for
  // support/debugging without needing to dig through Action logs.
  return match ? `${match[1]} [${detail}]` : detail;
}

function reportMessage(status, detail) {
  if (status === "joined") return "✅ Group ထဲ join ဝင်ပြီးပါပြီ";
  if (status === "already_member") return "ℹ️ ဒီ group ထဲ join ဝင်ပြီးသားဖြစ်နေပါတယ်";
  return `❌ Join မအောင်မြင်ပါ — ${friendlyDetail(detail)}`;
}

async function handleTelegramWebhook(request, env, ctx) {
  const incomingSecret = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
  if (incomingSecret !== env.TG_WEBHOOK_SECRET) {
    return new Response("forbidden", { status: 403 });
  }

  let update;
  try {
    update = await request.json();
  } catch {
    return new Response("OK", { status: 200 });
  }

  const message = update.message ?? update.channel_post;
  const chatId = message?.chat?.id;
  const text = message?.text;

  if (!update.update_id || !chatId || !text) {
    return new Response("OK", { status: 200 });
  }

  try {
    const dedupKey = `upd:${update.update_id}`;
    const alreadySeen = await env.DEDUP_KV.get(dedupKey);
    if (alreadySeen) {
      return new Response("OK", { status: 200 });
    }
    await env.DEDUP_KV.put(dedupKey, "1", { expirationTtl: KV_DEDUP_TTL_SECONDS });

    const normalized = normalizeTelegramInviteUrl(text);
    if (!normalized) {
      await replyToUser(env, chatId, "⚠️ Valid Telegram group link ပို့ပါ (t.me/... or t.me/+...)");
      return new Response("OK", { status: 200 });
    }

    const now = Date.now();
    const insertResult = await env.DB
      .prepare(
        "INSERT OR IGNORE INTO processed_urls (url_normalized, first_seen_at, last_status) VALUES (?, ?, 'queued')"
      )
      .bind(normalized, now)
      .run();

    if (insertResult.meta.changes === 0) {
      await replyToUser(env, chatId, "⚠️ ဒီ link ကို queue ထဲ ရှိပြီးသားပါ");
      return new Response("OK", { status: 200 });
    }

    await env.DB
      .prepare(
        "INSERT INTO join_queue (chat_id, url_normalized, status, created_at) VALUES (?, ?, 'queued', ?)"
      )
      .bind(String(chatId), normalized, now)
      .run();

    await replyToUser(env, chatId, "✅ Queued — join automation ဆက်လက်လုပ်ဆောင်ပါမည်");
    return new Response("OK", { status: 200 });
  } catch (err) {
    console.error("webhook-handler error:", err);
    return new Response("OK", { status: 200 });
  }
}

function normalizeTelegramInviteUrl(text) {
  const match = text.match(
    /(?:https?:\/\/)?(?:www\.)?t\.me\/(\+|joinchat\/)?([A-Za-z0-9_-]+)/i
  );
  if (!match) return null;

  const [, invitePrefix, identifier] = match;

  if (invitePrefix) {
    return `invite:${identifier}`;
  }

  return `username:${identifier.toLowerCase()}`;
}

async function replyToUser(env, chatId, text) {
  const res = await fetch(`https://api.telegram.org/bot${env.TG_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text }),
  });
  if (!res.ok) {
    console.error("sendMessage failed:", res.status, await res.text());
  }
}
