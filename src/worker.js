// gp-join-automation — Cloudflare Worker
// Phase 2: webhook-handler (dedup + enqueue + instant-ack)
// Phase 3: dispatcher (Cron Trigger -> GitHub workflow_dispatch)  [TODO]
// Phase 5: /report endpoint                                       [TODO]

const KV_DEDUP_TTL_SECONDS = 600; // 10 min — covers Telegram's webhook retry window

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (request.method !== "POST") {
      return new Response("gp-join-automation", { status: 200 });
    }

    if (url.pathname === "/report") {
      return new Response("not implemented yet", { status: 501 });
    }

    return handleTelegramWebhook(request, env, ctx);
  },

  async scheduled(event, env, ctx) {
    // TODO Phase 3: dispatcher tick — pop oldest queued row, trigger GitHub workflow_dispatch
  },
};

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
