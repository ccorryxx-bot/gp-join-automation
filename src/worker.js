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
    await dispatchNextQueuedJoin(env);
  },
};

const GITHUB_REPO = "ccorryxx-bot/gp-join-automation";

// Cron fires every 1 min (Cloudflare's minimum granularity) and this processes
// exactly ONE queued row per tick — that alone gives ~60s natural pacing
// between joins, which is more conservative than the 45s target in the roadmap.
// No internal sleep/loop needed.
async function dispatchNextQueuedJoin(env) {
  const row = await env.DB.prepare(
    "SELECT id, url_normalized FROM join_queue WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
  ).first();

  if (!row) return; // nothing queued — no-op tick

  const joinUrl = denormalizeToJoinUrl(row.url_normalized);
  if (!joinUrl) {
    // Shouldn't happen given normalizeTelegramInviteUrl's output format, but guard anyway.
    await env.DB.prepare(
      "UPDATE join_queue SET status = 'failed', detail = ?, updated_at = ? WHERE id = ?"
    ).bind("denormalize_failed", Date.now(), row.id).run();
    return;
  }

  const dispatchRes = await fetch(
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

  if (dispatchRes.status === 204) {
    // GitHub returns 204 No Content on a successful dispatch.
    await env.DB.prepare(
      "UPDATE join_queue SET status = 'triggered', updated_at = ? WHERE id = ?"
    ).bind(Date.now(), row.id).run();
  } else {
    console.error("workflow_dispatch failed:", dispatchRes.status, await dispatchRes.text());
    // Leave status = 'queued' — next tick retries automatically. No change needed here.
  }
  // Once the Action finishes, it calls POST /report (see handleReport below)
  // to move this row out of 'triggered' into a terminal status. If the Action
  // crashes before calling /report, the row stays stuck at 'triggered' —
  // a stale-row timeout sweep is a reasonable future addition but out of
  // scope for now.
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
  const incomingSecret = request.headers.get("X-Report-Secret");
  if (!env.REPORT_SECRET || incomingSecret !== env.REPORT_SECRET) {
    return new Response("forbidden", { status: 403 });
  }

  let body;
  try {
    body = await request.json();
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

function reportMessage(status, detail) {
  if (status === "joined") return "✅ Group ထဲ join ဝင်ပြီးပါပြီ";
  if (status === "already_member") return "ℹ️ ဒီ group ထဲ join ဝင်ပြီးသားဖြစ်နေပါတယ်";
  return `❌ Join မအောင်မြင်ပါ (${detail || "unknown error"})`;
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
