// gp-join-automation — Cloudflare Worker
// Phase 2: webhook-handler (dedup + enqueue + instant-ack)
// Phase 3: dispatcher (Cron Trigger -> GitHub workflow_dispatch)
// Phase 4: join.yml Telethon join logic — see scripts/join_telegram.py
// Phase 5: /report endpoint (join_queue update + processed_urls sync + user notify)
// Phase 7 (2026-09-22): multi-account (CH / JL) support — inline-keyboard
// account picker before queueing, per-account dedup/dispatch/pacing so both
// accounts can join in parallel without blocking each other, and admin-only
// gating (ADMIN_TG_ID) on every command and message this bot accepts.

const KV_DEDUP_TTL_SECONDS = 600; // 10 min — covers Telegram's webhook retry window
const REPORT_STATUSES = new Set(["joined", "already_member", "failed"]);

// Add a 3rd entry here (+ its GitHub secrets + join.yml choice option) to
// extend to another Telethon account later — everything else in this file
// (dedup, dispatch, pacing) is already generic over this list.
const ACCOUNTS = ["CH", "JL"];

// How long an account-selection prompt stays valid. If the admin doesn't tap
// a button within this window, the pending URLs are dropped from KV and the
// admin has to resend the link(s) — deliberately short so a stale prompt
// tapped days later can't silently queue an old batch.
const PENDING_SELECTION_TTL_SECONDS = 300; // 5 min

function pendingSelectionKvKey(chatId) {
  return `pending_urls:${chatId}`;
}

function dispatchPacingKvKey(account) {
  return `dispatch:next_allowed_at:${account}`;
}

// Every message and callback this bot processes is gated on this single ID
// — set as a plain (non-secret) `ADMIN_TG_ID` var in wrangler.toml. Anyone
// else's messages are silently ignored (no reply at all), so the bot's
// existence isn't confirmed to a stranger who stumbles onto it.
function isAdmin(env, userId) {
  return !!env.ADMIN_TG_ID && String(userId) === String(env.ADMIN_TG_ID);
}

// Phase 6: bulk-URL intake. A message can now carry a variable-length list
// like "[https://t.me/a,https://t.me/b,https://t.me/c]" instead of exactly
// one URL. Each URL still becomes its own join_queue row and is still joined
// ONE AT A TIME — see dispatchNextQueuedJoin's in-flight check below. This is
// deliberate: Telegram accounts get FloodWait'd or banned fast if joins fire
// back-to-back, so "accept N URLs at once" must never mean "join N at once".
const MAX_URLS_PER_MESSAGE = 50; // sanity cap, not a target batch size

// Random pacing delay applied AFTER each join attempt reports back (success
// or failure) and BEFORE the next queued URL is dispatched. Stored in KV
// (not D1) because it's ephemeral scheduling state, not queue data.
const MIN_DISPATCH_DELAY_MS = 60 * 1000; // 1 min
const MAX_DISPATCH_DELAY_MS = 3 * 60 * 1000; // 3 min

// PeerFloodError is Telegram's account-level anti-spam signal, not a
// per-URL problem (see scripts/join_telegram.py). Telegram does not publish
// an exact cooldown for it; 24h is a commonly-cited, conservative estimate
// from the wider Telethon/MTProto community, not a number Telegram itself
// guarantees -- treat it as a tunable default, not a proven fact.
// Overridable without a redeploy via a `PEER_FLOOD_PAUSE_HOURS` var/secret.
const DEFAULT_PEER_FLOOD_PAUSE_HOURS = 24;

function peerFloodPauseMs(env) {
  const hours = Number(env.PEER_FLOOD_PAUSE_HOURS) || DEFAULT_PEER_FLOOD_PAUSE_HOURS;
  return hours * 60 * 60 * 1000;
}

function randomDispatchDelayMs() {
  return (
    MIN_DISPATCH_DELAY_MS +
    Math.floor(Math.random() * (MAX_DISPATCH_DELAY_MS - MIN_DISPATCH_DELAY_MS))
  );
}

// delayMs defaults to the normal 1-3 min inter-join pacing, but callers pass
// a much larger value for a PeerFlood pause. TTL is derived from delayMs
// (with a buffer) rather than a fixed 600s -- a fixed short TTL would let a
// 24h pause silently expire from KV after 10 minutes and defeat the pause.
// Keyed per-account (Phase 7) — a PeerFlood pause or normal cooldown on CH
// must never block JL's independent queue, and vice versa.
async function armDispatchPacingDelay(env, account, delayMs = randomDispatchDelayMs()) {
  const ttlSeconds = Math.max(60, Math.ceil(delayMs / 1000) + 300);
  await env.DEDUP_KV.put(dispatchPacingKvKey(account), String(Date.now() + delayMs), {
    expirationTtl: ttlSeconds,
  });
}

// Phase 8 (2026-09-23, admin-triggered as of 2026-09-26): "leave muted
// groups" automation. Admin sends /leavescan and picks CH or JL — that scan
// finds groups where this account has been admin-restricted ("muted", not
// fully kicked), asks the admin a Y/N confirm in Telegram, then leave.yml
// actually leaves the confirmed ones. No cron schedule any more — a scan
// only ever starts because the admin asked for one.
// leave:running:{account} is the pause flag dispatchNextQueuedJoinForAccount
// checks below — same session used from two GitHub runners (two IPs) at
// once risks a Telegram-side revoke, so joins and leave-runs for ONE
// account must never overlap. CH and JL are independent sessions, so one
// account's leave-run never pauses the other's joins.
const LEAVE_SCAN_RUNNING_TTL_SECONDS = 10 * 60; // scan-phase safety cap
const LEAVE_EXEC_RUNNING_TTL_SECONDS = 20 * 60; // leave-phase safety cap (matches leave.yml timeout-minutes)

function leaveRunningKvKey(account) {
  return `leave:running:${account}`;
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // GET, so it must be checked before the blanket non-POST branch below.
    if (url.pathname === "/leave-candidates") {
      return handleLeaveCandidates(request, env);
    }

    if (request.method !== "POST") {
      return new Response("gp-join-automation", { status: 200 });
    }

    if (url.pathname === "/report") {
      return handleReport(request, env);
    }

    if (url.pathname === "/leave-report") {
      return handleLeaveReport(request, env);
    }

    return handleTelegramWebhook(request, env, ctx);
  },

  async scheduled(event, env, ctx) {
    // Order matters: sweep stale rows first so a freshly-dispatched row from
    // *this* tick is never accidentally caught by the same sweep.
    await sweepStaleTriggered(env);
    await dispatchNextQueuedJoin(env);
    // Leave-scan is no longer cron-driven — see triggerLeaveScan(), fired
    // only from the admin's /leavescan command.
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

// Cron fires every 1 min (Cloudflare's minimum granularity). Phase 7: CH and
// JL are fully independent lanes now, so each gets its own in-flight check,
// pacing cooldown, and queued row — one account being paused (PeerFlood,
// FloodWait, mid-join) never blocks the other from joining.
async function dispatchNextQueuedJoin(env) {
  for (const account of ACCOUNTS) {
    await dispatchNextQueuedJoinForAccount(env, account);
  }
}

// Processes exactly ONE queued row per tick, for ONE account — that alone
// gives ~60s natural pacing between joins on that account, more conservative
// than the 45s target in the roadmap. No internal sleep/loop needed.
async function dispatchNextQueuedJoinForAccount(env, account) {
  // Guard 0 (Phase 8): never dispatch a new join for this account while a
  // leave-scan or leave-execute run is using its session — see the Phase 8
  // comment above armDispatchPacingDelay for why.
  if (await env.DEDUP_KV.get(leaveRunningKvKey(account))) {
    return;
  }

  // Guard 1: never dispatch a new join for this account while one of its
  // rows is already in flight. With bulk intake, several rows can be
  // 'queued' at once — this is what turns "N queued" into "join them one at
  // a time" instead of a burst.
  const inFlight = await env.DB.prepare(
    "SELECT id FROM join_queue WHERE account = ? AND status = 'triggered' LIMIT 1"
  ).bind(account).first();
  if (inFlight) return; // previous join on this account still running / awaiting /report

  // Guard 2: pacing delay between one join finishing and the next starting
  // on THIS account. Set by armDispatchPacingDelay() from
  // handleReport/sweepStaleTriggered.
  const nextAllowedRaw = await env.DEDUP_KV.get(dispatchPacingKvKey(account));
  const nextAllowedAt = nextAllowedRaw ? Number(nextAllowedRaw) : 0;
  if (Date.now() < nextAllowedAt) return; // still inside the 1-3 min cooldown

  const row = await env.DB.prepare(
    "SELECT id, chat_id, url_normalized, dispatch_attempts FROM join_queue WHERE account = ? AND status = 'queued' ORDER BY created_at ASC LIMIT 1"
  ).bind(account).first();

  // Nothing queued for this account: no special state to reset — the moment
  // a new URL is confirmed via the account picker it's inserted as 'queued'
  // and the very next cron tick picks it up again.
  if (!row) return; // nothing queued for this account — no-op tick

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
          inputs: { group_url: joinUrl, queue_id: String(row.id), account },
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
    await replyToUser(env, row.chat_id, `🔄 [${account}] လုပ်ဆောင်နေပါပြီ — Group ထဲ join ဝင်ဖို့ ကြိုးစားနေပါတယ်...`);
    return;
  }

  if (dispatchRes) {
    dispatchErrText = `${dispatchRes.status} ${await dispatchRes.text()}`;
  }
  console.error(`workflow_dispatch failed [${account}]:`, dispatchErrText);

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

// Fired only from the admin's /leavescan command (see handleLeaveScanCommand
// / the leavescan: callback below) — no cron, no interval. One attempt per
// tap: if the dispatch fails the admin just sends /leavescan again, rather
// than a background retry loop running unattended.
// Returns a short status code the caller turns into a Telegram reply:
//   "running"          - a scan or leave is already in progress for this account
//   "awaiting_confirm"  - a previous scan is still waiting on a Yes/No tap
//   "started"           - scan.yml was dispatched successfully
//   "dispatch_failed"   - GitHub Actions dispatch call failed
async function triggerLeaveScan(env, account) {
  if (await env.DEDUP_KV.get(leaveRunningKvKey(account))) {
    return "running";
  }

  const pending = await env.DB.prepare(
    "SELECT id FROM leave_scans WHERE account = ? AND status = 'awaiting_confirm' LIMIT 1"
  ).bind(account).first();
  if (pending) {
    return "awaiting_confirm";
  }

  const now = Date.now();
  const inserted = await env.DB.prepare(
    "INSERT INTO leave_scans (account, status, created_at) VALUES (?, 'scanning', ?)"
  ).bind(account, now).run();
  const scanId = inserted.meta.last_row_id;

  await env.DEDUP_KV.put(leaveRunningKvKey(account), "1", {
    expirationTtl: LEAVE_SCAN_RUNNING_TTL_SECONDS,
  });

  const ok = await dispatchLeaveWorkflow(env, account, "scan", scanId);
  if (ok) return "started";

  await env.DEDUP_KV.delete(leaveRunningKvKey(account));
  await env.DB.prepare("UPDATE leave_scans SET status = 'failed', updated_at = ? WHERE id = ?")
    .bind(Date.now(), scanId).run();
  return "dispatch_failed";
}

// Kept separate from dispatchNextQueuedJoinForAccount's inline fetch call
// rather than unified into one shared helper — that function is already
// live/tested in production; not touching it for a DRY-only change.
async function dispatchLeaveWorkflow(env, account, mode, scanId) {
  try {
    const res = await fetch(
      `https://api.github.com/repos/${GITHUB_REPO}/actions/workflows/leave.yml/dispatches`,
      {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${env.GH_PAT}`,
          "Accept": "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "gp-join-automation-dispatcher",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ ref: "main", inputs: { account, mode, scan_id: String(scanId) } }),
      }
    );
    if (res.status === 204) return true;
    console.error(`leave.yml dispatch failed [${account}/${mode}]:`, res.status, await res.text());
    return false;
  } catch (err) {
    console.error(`leave.yml dispatch threw [${account}/${mode}]:`, err);
    return false;
  }
}

// Catches the case the roadmap flagged as a known gap: the Action crashes,
// times out, or its /report call itself fails to reach us, leaving a row
// stuck at 'triggered' forever with the user never told either way.
async function sweepStaleTriggered(env) {
  const cutoff = Date.now() - STALE_TRIGGERED_TIMEOUT_MS;
  const { results } = await env.DB.prepare(
    "SELECT id, chat_id, account FROM join_queue WHERE status = 'triggered' AND updated_at < ?"
  ).bind(cutoff).all();

  for (const row of results) {
    await env.DB.prepare(
      "UPDATE join_queue SET status = 'failed', detail = 'stale_no_report_timeout', updated_at = ? WHERE id = ?"
    ).bind(Date.now(), row.id).run();
    await armDispatchPacingDelay(env, row.account);
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
    "SELECT chat_id, url_normalized, account, status AS current_status FROM join_queue WHERE id = ?"
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
      .prepare("UPDATE processed_urls SET last_status = ? WHERE url_normalized = ? AND account = ?")
      .bind(status, row.url_normalized, row.account),
  ]);

  // This join is now resolved (joined / already_member / failed) — start the
  // cooldown, scoped to THIS account, before dispatchNextQueuedJoinForAccount
  // is allowed to pick up that account's next queued URL, if any. Normally
  // 1-3 min; PeerFlood is a signal about the ACCOUNT, not this one URL, so it
  // pauses that account's whole queue much longer — the other account is
  // untouched.
  const isPeerFlood = detail === "peer_flood_detected";
  await armDispatchPacingDelay(env, row.account, isPeerFlood ? peerFloodPauseMs(env) : undefined);

  await replyToUser(env, row.chat_id, reportMessage(status, detail));

  return new Response("OK", { status: 200 });
}

function checkReportSecret(request, env) {
  const incoming = request.headers.get("X-Report-Secret");
  return !!env.REPORT_SECRET && incoming === env.REPORT_SECRET;
}

// GET /leave-candidates?scan_id=123 — leave.yml's MODE=leave step calls this
// to fetch the admin-confirmed peer list rather than taking it as a
// workflow_dispatch input, so an arbitrarily long muted-group list never
// has to fit inside GitHub's input size limits.
async function handleLeaveCandidates(request, env) {
  if (!checkReportSecret(request, env)) {
    return new Response("forbidden", { status: 403 });
  }
  const url = new URL(request.url);
  const scanId = Number(url.searchParams.get("scan_id"));
  if (!Number.isInteger(scanId)) {
    return new Response("bad request", { status: 400 });
  }
  const { results } = await env.DB.prepare(
    "SELECT peer_id, peer_type, title FROM leave_candidates WHERE scan_id = ? AND status = 'pending'"
  ).bind(scanId).all();
  return new Response(JSON.stringify({ candidates: results }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

// POST /leave-report — called once by leave.yml's Python step, either after
// a scan (mode: "scan") or after a leave run (mode: "leave"). One aggregate
// call per script run, not one per group, matching join_telegram.py's
// "report once per run" cadence and keeping the "leaving started" /
// "leaving finished" notices to exactly the two the admin asked for.
async function handleLeaveReport(request, env) {
  if (!checkReportSecret(request, env)) {
    return new Response("forbidden", { status: 403 });
  }
  let body;
  try {
    body = await request.json();
  } catch {
    return new Response("bad request", { status: 400 });
  }

  const { mode, account } = body;
  const scanId = Number(body.scan_id);
  if (!ACCOUNTS.includes(account) || !Number.isInteger(scanId)) {
    return new Response("bad request", { status: 400 });
  }

  // Whichever phase just finished, the session is free again — clear the
  // pause before doing anything else so a slow D1/Telegram call below never
  // extends the join-pause past what actually happened on GitHub's side.
  await env.DEDUP_KV.delete(leaveRunningKvKey(account));

  if (mode === "scan") {
    const candidates = Array.isArray(body.candidates) ? body.candidates : [];
    const totalDialogs = Number(body.total_dialogs) || 0;
    const now = Date.now();

    if (candidates.length > 0) {
      await env.DB.batch(
        candidates.map((c) =>
          env.DB
            .prepare(
              "INSERT INTO leave_candidates (scan_id, peer_id, peer_type, title, reason, status, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)"
            )
            .bind(scanId, String(c.peer_id), c.peer_type || "channel", c.title || "", c.reason || "muted", now)
        )
      );
    }

    const status = candidates.length > 0 ? "awaiting_confirm" : "done";
    // muted_count predates Phase 9 and is only ever written, never read
    // elsewhere -- kept as the combined candidate count (muted +
    // under_50_members) rather than adding a migration just to rename it.
    await env.DB.prepare(
      "UPDATE leave_scans SET status = ?, total_dialogs = ?, muted_count = ?, updated_at = ? WHERE id = ?"
    ).bind(status, totalDialogs, candidates.length, now, scanId).run();

    if (candidates.length === 0) {
      await notifyAdmin(
        env,
        `🔍 [${account}] Scan ပြီးပါပြီ — Group ${totalDialogs} ခုထဲမှာ Leave candidate (muted / 50 အောက် member) မတွေ့ပါဘူး`
      );
      return new Response("OK", { status: 200 });
    }

    // Phase 9: candidates now come in two reasons -- split them for the
    // admin instead of lumping everything under "muted" like before.
    // Anything that isn't explicitly "under_50_members" is treated as
    // "muted" here, matching the DB column's DEFAULT 'muted' for old rows.
    const mutedCandidates = candidates.filter((c) => c.reason !== "under_50_members");
    const smallGroupCandidates = candidates.filter((c) => c.reason === "under_50_members");

    // Show the actual group titles (not just a count) so the admin can eyeball
    // the candidate list before confirming — capped per section to keep the
    // Telegram message short even when a scan flags a large batch at once.
    const MAX_LISTED_PER_SECTION = 10;
    const MAX_TITLE_LENGTH = 60;
    const formatSection = (list) => {
      const lines = list
        .slice(0, MAX_LISTED_PER_SECTION)
        .map((c, i) => {
          const memberNote = c.reason === "under_50_members" && c.member_count != null
            ? ` (${c.member_count} members)`
            : "";
          return `${i + 1}. ${(c.title || "(no title)").slice(0, MAX_TITLE_LENGTH)}${memberNote}`;
        })
        .join("\n");
      const more = list.length > MAX_LISTED_PER_SECTION
        ? `\n...နောက်ထပ် ${list.length - MAX_LISTED_PER_SECTION} ခု`
        : "";
      return lines + more;
    };

    const mutedSection = mutedCandidates.length
      ? `\n\n🔇 Muted (admin-restricted) — ${mutedCandidates.length} ခု တွေ့တယ်။\n${formatSection(mutedCandidates)}`
      : "";
    const smallSection = smallGroupCandidates.length
      ? `\n\n👥 50 under member — ${smallGroupCandidates.length} ခု တွေ့တယ်။\n${formatSection(smallGroupCandidates)}`
      : "";

    await notifyAdmin(
      env,
      `🔍 [${account}] Scan ပြီးပါပြီ — Group ${totalDialogs} ခုထဲက Leave candidate ${candidates.length} ခု တွေ့ပါတယ်။${mutedSection}${smallSection}\n\nYes ဆို ၂ မျိုးစလုံး (mute + 50 အောက်) တစ်ခါထဲ Leave မယ် — Confirm ပါ 👇`,
      {
        inline_keyboard: [
          [
            { text: "✅ Yes, Leave", callback_data: `leaveconfirm:${account}:${scanId}:yes` },
            { text: "❌ No, Cancel", callback_data: `leaveconfirm:${account}:${scanId}:no` },
          ],
        ],
      }
    );
    return new Response("OK", { status: 200 });
  }

  if (mode === "leave") {
    const results = Array.isArray(body.results) ? body.results : [];
    const now = Date.now();

    if (results.length > 0) {
      await env.DB.batch(
        results.map((r) =>
          env.DB
            .prepare(
              "UPDATE leave_candidates SET status = ?, detail = ?, updated_at = ? WHERE scan_id = ? AND peer_id = ?"
            )
            .bind(r.status === "left" ? "left" : "failed", r.detail || "", now, scanId, String(r.peer_id))
        )
      );
    }

    const leftCount = results.filter((r) => r.status === "left").length;
    const failedCount = results.length - leftCount;
    await env.DB.prepare(
      "UPDATE leave_scans SET status = 'done', left_count = ?, failed_count = ?, updated_at = ? WHERE id = ?"
    ).bind(leftCount, failedCount, now, scanId).run();

    const remaining = await env.DB.prepare(
      "SELECT COUNT(*) AS c FROM leave_candidates WHERE scan_id = ? AND status = 'pending'"
    ).bind(scanId).first();
    const remainingNote = remaining?.c
      ? `\n⏭️ ${remaining.c} ခု ကျန်နေပါတယ် (peer-flood ကြောင့် early-stop ဖြစ်လို့) — နောက် ၃ရက် cycle မှာ ပြန်စမှာပါ`
      : "";

    await notifyAdmin(
      env,
      `✅ [${account}] Groups leaving ပြီးသွားပါပြီ — ${leftCount} ခု ထွက်ပြီး, ${failedCount} ခု fail (Auto-join ပြန်စပါပြီ)${remainingNote}`
    );
    return new Response("OK", { status: 200 });
  }

  return new Response("bad request", { status: 400 });
}

// Phase 8's system notices (scan result, leave-started, leave-finished) go
// straight to the admin, not tied to any inbound chat_id — reuses
// TG_BOT_TOKEN like replyToUser but targets ADMIN_TG_ID directly.
async function notifyAdmin(env, text, replyMarkup) {
  if (!env.ADMIN_TG_ID) return;
  const body = { chat_id: env.ADMIN_TG_ID, text };
  if (replyMarkup) body.reply_markup = replyMarkup;
  const res = await fetch(`https://api.telegram.org/bot${env.TG_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    console.error("notifyAdmin failed:", res.status, await res.text());
  }
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
  ["peer_flood_detected", "Telegram ရဲ့ Anti-spam system က ဒီ account ကို ယာယီ flag လုပ်လိုက်ပါတယ် — Automation တစ်ခုလုံးကို ခဏရပ်ထားပါမယ် (queue ထဲက link တွေ မပျက်ပါဘူး၊ ပြန်စမှာပါ)"],
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

  if (!update.update_id) {
    return new Response("OK", { status: 200 });
  }

  try {
    const dedupKey = `upd:${update.update_id}`;
    const alreadySeen = await env.DEDUP_KV.get(dedupKey);
    if (alreadySeen) {
      return new Response("OK", { status: 200 });
    }
    await env.DEDUP_KV.put(dedupKey, "1", { expirationTtl: KV_DEDUP_TTL_SECONDS });

    // Phase 7: account-picker button taps arrive as callback_query, not
    // message — handled entirely separately (its own admin check inside).
    if (update.callback_query) {
      await handleCallbackQuery(env, update.callback_query);
      return new Response("OK", { status: 200 });
    }

    const message = update.message ?? update.channel_post;
    const chatId = message?.chat?.id;
    const text = message?.text;
    const senderId = message?.from?.id;

    if (!chatId || !text) {
      return new Response("OK", { status: 200 });
    }

    // Phase 7: admin-only bot. Anyone else's message is silently dropped —
    // no reply at all, so the bot's presence isn't confirmed to a stranger.
    if (!isAdmin(env, senderId)) {
      return new Response("OK", { status: 200 });
    }

    if (text.startsWith("/")) {
      await handleCommand(env, chatId, text);
      return new Response("OK", { status: 200 });
    }

    // Phase 6: message can be "[https://t.me/a,https://t.me/b,...]" (any
    // count) or still a single bare link — both flow through the same path.
    // Phase 7: instead of queueing immediately, valid URLs are parked in KV
    // and the admin is asked which account (CH / JL) should run them —
    // enqueueOneUrl only runs after that choice comes back via callback_query.
    const rawEntries = extractUrlEntries(text);
    const seenInMessage = new Set();
    const normalizedList = [];
    let invalidCount = 0;
    let unsupportedCount = 0; // t.me/c/... deep links -- recognized, but can't auto-join

    for (const rawEntry of rawEntries) {
      const normalized = normalizeTelegramInviteUrl(rawEntry);
      if (!normalized) {
        invalidCount++;
        continue;
      }
      if (normalized.startsWith("channelref:")) {
        unsupportedCount++;
        continue;
      }
      // A pasted list can repeat a link by accident — dedupe within this one
      // message too, without a second DB round trip for the repeat.
      if (seenInMessage.has(normalized)) continue;
      seenInMessage.add(normalized);
      normalizedList.push(normalized);
    }

    if (normalizedList.length === 0) {
      await replyToUser(env, chatId, buildNoValidLinksMessage(unsupportedCount));
      return new Response("OK", { status: 200 });
    }

    await env.DEDUP_KV.put(
      pendingSelectionKvKey(chatId),
      JSON.stringify({ urls: normalizedList, invalidCount, unsupportedCount }),
      { expirationTtl: PENDING_SELECTION_TTL_SECONDS }
    );
    await sendAccountPrompt(env, chatId, normalizedList.length, invalidCount, unsupportedCount);
    return new Response("OK", { status: 200 });
  } catch (err) {
    console.error("webhook-handler error:", err);
    return new Response("OK", { status: 200 });
  }
}

// Phase 7: admin taps "CH" or "JL" on the prompt sent above. Pulls the
// parked URL list back out of KV, enqueues each one tagged with the chosen
// account, then edits the original prompt message to show the result
// (rather than leaving stale buttons on screen or sending a 2nd message).
async function handleCallbackQuery(env, cq) {
  const senderId = cq.from?.id;
  const chatId = cq.message?.chat?.id;
  const messageId = cq.message?.message_id;
  const data = cq.data || "";

  if (!isAdmin(env, senderId)) {
    await answerCallbackQuery(env, cq.id);
    return;
  }

  if (!chatId) {
    await answerCallbackQuery(env, cq.id);
    return;
  }

  if (data.startsWith("leaveconfirm:")) {
    await handleLeaveConfirmCallback(env, cq, data, chatId, messageId);
    return;
  }

  if (data.startsWith("leavescan:")) {
    const account = data.slice("leavescan:".length);
    if (!ACCOUNTS.includes(account)) {
      await answerCallbackQuery(env, cq.id, "⚠️ မသိတဲ့ account");
      return;
    }
    const result = await triggerLeaveScan(env, account);
    const RESULT_TEXT = {
      running: `⏳ [${account}] Scan/Leave တစ်ခု အရင်ကတည်းက run နေပါတယ် — အဲဒါ ပြီးမှ ထပ်ခေါ်ပါ`,
      awaiting_confirm: `⌛ [${account}] ရှေ့က scan ရလဒ်ကို Yes/No confirm မလုပ်ရသေးပါ — အဲဒီ message ပေါ်ကို အရင် action ယူပါ`,
      started: `✅ [${account}] Scan စပါပြီ — ပြီးရင် muted group list ကို ဒီမှာ ပြောပေးပါမယ်`,
      dispatch_failed: `⚠️ [${account}] Scan dispatch မအောင်မြင်ပါ — /leavescan ပြန်ခေါ်ကြည့်ပါ`,
    };
    await answerCallbackQuery(env, cq.id);
    if (messageId) {
      await editMessageText(env, chatId, messageId, RESULT_TEXT[result] || "⚠️ Internal error");
    } else {
      await replyToUser(env, chatId, RESULT_TEXT[result] || "⚠️ Internal error");
    }
    return;
  }

  if (!data.startsWith("acct:")) {
    await answerCallbackQuery(env, cq.id);
    return;
  }

  const account = data.slice("acct:".length);
  if (!ACCOUNTS.includes(account)) {
    await answerCallbackQuery(env, cq.id, "⚠️ မသိတဲ့ account");
    return;
  }

  const pendingRaw = await env.DEDUP_KV.get(pendingSelectionKvKey(chatId));
  if (!pendingRaw) {
    // Prompt expired (PENDING_SELECTION_TTL_SECONDS) or already used by a
    // previous tap — nothing left to enqueue.
    await answerCallbackQuery(env, cq.id, "⌛ Session ကုန်သွားပါပြီ — URL ပြန်ပို့ပါ", true);
    return;
  }
  await env.DEDUP_KV.delete(pendingSelectionKvKey(chatId));

  let pending;
  try {
    pending = JSON.parse(pendingRaw);
  } catch {
    await answerCallbackQuery(env, cq.id, "⚠️ Internal error");
    return;
  }

  const { urls, invalidCount, unsupportedCount } = pending;
  const now = Date.now();
  const tally = {
    queued: 0,
    retry_queued: 0,
    already_done: 0,
    in_progress: 0,
    invalid: invalidCount || 0,
    unsupported: unsupportedCount || 0,
  };

  for (const normalized of urls) {
    const outcome = await enqueueOneUrl(env, chatId, normalized, now, account);
    tally[outcome]++;
  }

  await answerCallbackQuery(env, cq.id, `✅ ${account} ရွေးပြီးပါပြီ`);

  const summary = `Account: ${account}\n\n${buildIntakeSummary(tally, urls.length + (invalidCount || 0) + (unsupportedCount || 0))}`;
  if (messageId) {
    await editMessageText(env, chatId, messageId, summary);
  } else {
    await replyToUser(env, chatId, summary);
  }
}

// Phase 8: admin tapped Yes/No on the leave-confirm prompt sent from
// handleLeaveReport. Idempotent against double-taps via the scan's status
// column — a second tap on an already-actioned prompt is a no-op, not a
// second dispatch.
async function handleLeaveConfirmCallback(env, cq, data, chatId, messageId) {
  const [, account, scanIdRaw, choice] = data.split(":");
  const scanId = Number(scanIdRaw);
  if (!ACCOUNTS.includes(account) || !Number.isInteger(scanId)) {
    await answerCallbackQuery(env, cq.id, "⚠️ Internal error");
    return;
  }

  const scan = await env.DB.prepare("SELECT status FROM leave_scans WHERE id = ?").bind(scanId).first();
  if (!scan || scan.status !== "awaiting_confirm") {
    await answerCallbackQuery(env, cq.id, "⌛ ဒီ scan ကို action ယူပြီးသားပါ", true);
    return;
  }

  if (choice === "no") {
    await env.DB.prepare("UPDATE leave_scans SET status = 'cancelled', updated_at = ? WHERE id = ?")
      .bind(Date.now(), scanId).run();
    await answerCallbackQuery(env, cq.id, "❌ Cancelled");
    if (messageId) {
      await editMessageText(env, chatId, messageId, `❌ [${account}] Leave ကို cancel လုပ်လိုက်ပါပြီ`);
    }
    return;
  }

  if (choice === "yes") {
    await env.DEDUP_KV.put(leaveRunningKvKey(account), "1", {
      expirationTtl: LEAVE_EXEC_RUNNING_TTL_SECONDS,
    });
    await env.DB.prepare("UPDATE leave_scans SET status = 'leaving', updated_at = ? WHERE id = ?")
      .bind(Date.now(), scanId).run();

    const ok = await dispatchLeaveWorkflow(env, account, "leave", scanId);
    if (!ok) {
      // Roll back so the admin can just tap the button again.
      await env.DEDUP_KV.delete(leaveRunningKvKey(account));
      await env.DB.prepare("UPDATE leave_scans SET status = 'awaiting_confirm', updated_at = ? WHERE id = ?")
        .bind(Date.now(), scanId).run();
      await answerCallbackQuery(env, cq.id, "⚠️ Dispatch မအောင်မြင်ပါ — ပြန်နှိပ်ကြည့်ပါ", true);
      return;
    }

    await answerCallbackQuery(env, cq.id, `✅ ${account} leaving စပါပြီ`);
    if (messageId) {
      await editMessageText(
        env,
        chatId,
        messageId,
        `🚪 [${account}] Groups leaving စတင်ပါပြီ — auto-join ကို ခေတ္တ ရပ်ထားပါမယ် (~10-15 min)`
      );
    }
  }
}

// Single source of truth for every admin command. /help text AND the
// Telegram-native "/" menu (pushed via /sync_menu -> setMyCommands) both
// read from this one list, so they can no longer drift apart like before —
// add a command here once and both places pick it up. Telegram never syncs
// the native menu on its own; it only ever shows whatever the last
// setMyCommands call sent, so /sync_menu has to be re-run by hand after
// this list changes.
const BOT_COMMANDS = [
  { command: "start", description: "Bot စတင်ရန် — usage message ပြရန်" },
  { command: "status", description: "Queue status (account တစ်ခုချင်းစီ)" },
  { command: "leavescan", description: "Muted group scan စတင်ရန် (account ရွေးရမယ်, admin ကိုယ်တိုင် ခေါ်မှ run)" },
  { command: "sync_menu", description: "ဒီ Bot Menu ခလုတ်ကို command list အသစ်နဲ့ sync ပြန်ရန်" },
  { command: "help", description: "ဒီ usage message ပြန်ပြရန်" },
];

const HELP_TEXT = [
  "🤖 gp-join-automation",
  "",
  "Group link ပို့ပါ — line တစ်ကြောင်းကို link တစ်ခုစီ (numbered list ဖြစ်နိုင်တယ်):",
  "1. https://t.me/+xxxxxxxxxxxx",
  "2. https://t.me/+yyyyyyyyyyyy",
  "URL ဘေးနားမှာ extra စာသား မကပ်ပါစေနဲ့ — ဘယ် account (CH / JL) နဲ့ join မလဲ ခလုတ်တွေ ပြပေးပါမယ်။",
  "",
  ...BOT_COMMANDS.map((c) => `/${c.command} — ${c.description}`),
].join("\n");

async function handleCommand(env, chatId, text) {
  const cmd = text.trim().split(/\s+/)[0].split("@")[0].toLowerCase();

  if (cmd === "/status") {
    await replyToUser(env, chatId, await buildStatusMessage(env));
    return;
  }
  if (cmd === "/leavescan") {
    await sendLeaveScanPrompt(env, chatId);
    return;
  }
  if (cmd === "/sync_menu") {
    const ok = await syncBotCommands(env);
    await replyToUser(
      env,
      chatId,
      ok
        ? "✅ Menu sync ပြီးပါပြီ — chat ကနေ ထွက်ပြီး ပြန်ဝင် (ဒါမှမဟုတ် app restart) လုပ်ရင် Menu ခလုတ်ထဲမှာ command list အသစ် မြင်ရပါလိမ့်မယ်"
        : "⚠️ Menu sync မအောင်မြင်ပါ — Worker logs ထဲ setMyCommands error ကြည့်ပါ"
    );
    return;
  }
  if (cmd === "/help" || cmd === "/start") {
    await replyToUser(env, chatId, HELP_TEXT);
    return;
  }
  await replyToUser(env, chatId, "❓ မသိတဲ့ command ပါ — /help လို့ ပို့ကြည့်ပါ");
}

async function buildStatusMessage(env) {
  const { results } = await env.DB.prepare(
    "SELECT account, status, COUNT(*) AS c FROM join_queue GROUP BY account, status"
  ).all();

  const byAccount = {};
  for (const r of results) {
    byAccount[r.account] ??= {};
    byAccount[r.account][r.status] = r.c;
  }

  const lines = ["📊 Queue Status"];
  for (const account of ACCOUNTS) {
    const c = byAccount[account] || {};
    lines.push(
      `\n${account}: queued=${c.queued || 0}  triggered=${c.triggered || 0}  joined=${c.joined || 0}  already_member=${c.already_member || 0}  failed=${c.failed || 0}`
    );
  }
  return lines.join("\n");
}

// Decides what happens to one URL: brand-new (queue it), a retry of a
// previously-failed attempt (worth another shot), already successfully
// joined before (skip — this is the "no wasted API call on a duplicate"
// guard), or already mid-flight (queued/triggered right now — skip, it's
// already moving through the pipeline).
async function enqueueOneUrl(env, chatId, normalized, now, account) {
  const existing = await env.DB.prepare(
    "SELECT last_status FROM processed_urls WHERE url_normalized = ? AND account = ?"
  ).bind(normalized, account).first();

  if (!existing) {
    await env.DB.batch([
      env.DB.prepare(
        "INSERT INTO processed_urls (url_normalized, account, first_seen_at, last_status) VALUES (?, ?, ?, 'queued')"
      ).bind(normalized, account, now),
      env.DB.prepare(
        "INSERT INTO join_queue (chat_id, url_normalized, account, status, created_at) VALUES (?, ?, ?, 'queued', ?)"
      ).bind(String(chatId), normalized, account, now),
    ]);
    return "queued";
  }

  if (existing.last_status === "joined" || existing.last_status === "already_member") {
    return "already_done";
  }

  if (existing.last_status === "queued" || existing.last_status === "triggered") {
    return "in_progress";
  }

  // last_status === 'failed': re-arm it rather than skipping forever.
  await env.DB.batch([
    env.DB.prepare(
      "UPDATE processed_urls SET last_status = 'queued' WHERE url_normalized = ? AND account = ?"
    ).bind(normalized, account),
    env.DB.prepare(
      "INSERT INTO join_queue (chat_id, url_normalized, account, status, created_at) VALUES (?, ?, ?, 'queued', ?)"
    ).bind(String(chatId), normalized, account, now),
  ]);
  return "retry_queued";
}

// (2026-09-26) Describes the new required format: one link per line,
// nothing else on the line. Replaces the old "[a,b,c]" bracket-format hint.
function buildNoValidLinksMessage(unsupportedCount) {
  const lines = [
    "⚠️ Valid Telegram invite link ပို့ပါ — line တစ်ကြောင်းကို link တစ်ခုစီ ဖြစ်ရပါမယ်:",
    "",
    "1. https://t.me/+xxxxxxxxxxxx",
    "2. https://t.me/+yyyyyyyyyyyy",
    "",
    "URL ဘေးနားမှာ extra စာသား မကပ်ပါစေနဲ့။",
  ];
  if (unsupportedCount) {
    lines.push(
      "",
      `🚫 t.me/c/... format ${unsupportedCount} ခု ပါလာပါတယ် — ဒါက invite link မဟုတ်လို့ auto-join မရပါဘူး (Account ကိုယ်တိုင် member ဖြစ်ပြီးသားမှသာ ဖွင့်ကြည့်လို့ ရပါမယ်)`
    );
  }
  return lines.join("\n");
}

// One summary reply per incoming message instead of one reply per URL --
// a 10-link batch shouldn't produce 10 separate Telegram messages.
function buildIntakeSummary(tally, totalEntries) {
  const handled = tally.queued + tally.retry_queued + tally.already_done + tally.in_progress;
  if (totalEntries === 0 || (handled === 0 && tally.invalid + tally.unsupported === totalEntries)) {
    return buildNoValidLinksMessage(tally.unsupported);
  }

  const lines = [];
  if (tally.queued) lines.push(`✅ Queue ထဲ ထည့်ပြီးပါပြီ — ${tally.queued} link`);
  if (tally.retry_queued) lines.push(`🔁 ပြန်ကြိုးစားမည် (အရင်တစ်ခါ fail ဖြစ်ခဲ့တာ) — ${tally.retry_queued} link`);
  if (tally.already_done) lines.push(`⏭️ Skip (join ဝင်ပြီးသား) — ${tally.already_done} link`);
  if (tally.in_progress) lines.push(`⏳ Skip (queue ထဲမှာ လုပ်ဆောင်နေဆဲ) — ${tally.in_progress} link`);
  if (tally.invalid) lines.push(`❌ Link format မမှန်လို့ ကျော်လိုက်ပါတယ် — ${tally.invalid} link`);
  if (tally.unsupported) lines.push(`🚫 t.me/c/... link — auto-join မရပါ — ${tally.unsupported} link`);
  if (tally.queued + tally.retry_queued > 0) {
    lines.push("\nတစ်ခုစီကို ၁-၃ မိနစ် ခြားပြီး တစ်ခုချင်းစီ join ဝင်သွားပါမယ်");
  }
  return lines.join("\n");
}

// Accepts a numbered/bulleted list, one link per line -- e.g.
//   1. https://t.me/+RdIqky7oiOs0MzI1
//   2. https://t.me/+_M2ixP_4Pp1iOWRl
//   3. https://t.me/c/2617024293/60
// (2026-09-26 format change) The old behavior only split on commas, so a
// pasted multi-line list with no commas collapsed into a single raw entry
// and normalizeTelegramInviteUrl's loose regex.match() would silently grab
// just the first URL out of it -- the rest vanished with no error. Now
// every line is its own entry. A leading "1.", "1)", "-", or "•" marker
// (with the following space) is stripped; normalizeTelegramInviteUrl below
// then requires the *rest* of the entry to be nothing but the link, start
// to end -- so "text glued next to a url" is rejected as invalid instead of
// silently pattern-matched out of the noise. The legacy single-line
// "[a,b,c]" / "a,b,c" form still works too (comma-split within a line).
function extractUrlEntries(text) {
  const rawEntries = [];

  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;

    const bracketless =
      line.startsWith("[") && line.endsWith("]") ? line.slice(1, -1) : line;

    for (const part of bracketless.split(",")) {
      const entry = part.trim().replace(/^(?:\d+[.)]|[-•*])\s+/, "");
      if (entry) rawEntries.push(entry);
    }
  }

  return rawEntries.slice(0, MAX_URLS_PER_MESSAGE);
}

// Normalizes ONE already-isolated entry (extractUrlEntries has already
// stripped any "1. " / "-" marker off it). Anchored start-to-end on purpose
// -- the whole entry must be nothing but the link, or this returns null.
// The old version used a bare regex.match() with no anchors, which happily
// pulled a URL out of a sentence; that's the exact "text glued to the url"
// ambiguity the 2026-09-26 format change is meant to reject outright,
// rather than silently tolerate.
function normalizeTelegramInviteUrl(text) {
  const inviteMatch = text.match(
    /^(?:https?:\/\/)?(?:www\.)?t\.me\/(\+|joinchat\/)([A-Za-z0-9_-]+)\/?$/i
  );
  if (inviteMatch) {
    return `invite:${inviteMatch[2]}`;
  }

  // t.me/c/<internal_channel_id>/<message_id> -- a deep link to a message
  // inside a channel, NOT an invite link. It carries no invite hash, so
  // Telethon's ImportChatInviteRequest/JoinChannelRequest has nothing to
  // join with -- Telegram only allows joining blind via a public @username
  // or an invite hash, and this is neither. Recognized here as its own
  // category so it's reported to the admin as "can't auto-join" instead of
  // silently landing in the generic "invalid format" bucket.
  const channelRefMatch = text.match(
    /^(?:https?:\/\/)?(?:www\.)?t\.me\/c\/(\d+)(?:\/\d+)?\/?$/i
  );
  if (channelRefMatch) {
    return `channelref:${channelRefMatch[1]}`;
  }

  const usernameMatch = text.match(
    /^(?:https?:\/\/)?(?:www\.)?t\.me\/([A-Za-z0-9_-]+)\/?$/i
  );
  if (usernameMatch) {
    return `username:${usernameMatch[1].toLowerCase()}`;
  }

  return null;
}

// Pushes BOT_COMMANDS to Telegram's native "/" menu (setMyCommands). This
// is the only thing that ever updates that menu — there is no auto-sync on
// deploy, on purpose, so a broken command list can't ship silently. Run by
// hand via /sync_menu whenever BOT_COMMANDS changes.
async function syncBotCommands(env) {
  const res = await fetch(`https://api.telegram.org/bot${env.TG_BOT_TOKEN}/setMyCommands`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ commands: BOT_COMMANDS }),
  });
  if (!res.ok) {
    console.error("setMyCommands failed:", res.status, await res.text());
    return false;
  }
  return true;
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

// Phase 7: sends the CH / JL picker as an inline keyboard. callback_data is
// "acct:CH" / "acct:JL", read back in handleCallbackQuery.
async function sendAccountPrompt(env, chatId, validCount, invalidCount, unsupportedCount) {
  const invalidNote = invalidCount ? `\n❌ Format မမှန်လို့ ကျော်လိုက်တာ — ${invalidCount} link` : "";
  const unsupportedNote = unsupportedCount
    ? `\n🚫 t.me/c/... link (auto-join မရ) — ${unsupportedCount} link`
    : "";
  const text = `📋 ${validCount} link ရပါတယ်${invalidNote}${unsupportedNote}\n\nဘယ် Account နဲ့ join မလဲ ရွေးပါ 👇`;

  const res = await fetch(`https://api.telegram.org/bot${env.TG_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      chat_id: chatId,
      text,
      reply_markup: {
        inline_keyboard: [ACCOUNTS.map((a) => ({ text: `Account ${a}`, callback_data: `acct:${a}` }))],
      },
    }),
  });
  if (!res.ok) {
    console.error("sendAccountPrompt failed:", res.status, await res.text());
  }
}

// /leavescan: admin-triggered replacement for the old 3-day cron. Sends the
// same CH/JL picker style as sendAccountPrompt; the tap is read back in
// handleCallbackQuery via the "leavescan:" prefix, not "acct:".
async function sendLeaveScanPrompt(env, chatId) {
  const res = await fetch(`https://api.telegram.org/bot${env.TG_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      chat_id: chatId,
      text: "🔍 Muted group scan — ဘယ် Account ကို scan မလဲ ရွေးပါ 👇",
      reply_markup: {
        inline_keyboard: [ACCOUNTS.map((a) => ({ text: `Scan ${a}`, callback_data: `leavescan:${a}` }))],
      },
    }),
  });
  if (!res.ok) {
    console.error("sendLeaveScanPrompt failed:", res.status, await res.text());
  }
}

// Telegram requires every callback_query to be answered (even with empty
// text) or the tapped button keeps showing a loading spinner client-side.
// show_alert pops a modal instead of a toast — used for the expired-session case.
async function answerCallbackQuery(env, callbackQueryId, text = "", show_alert = false) {
  const res = await fetch(`https://api.telegram.org/bot${env.TG_BOT_TOKEN}/answerCallbackQuery`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ callback_query_id: callbackQueryId, text, show_alert }),
  });
  if (!res.ok) {
    console.error("answerCallbackQuery failed:", res.status, await res.text());
  }
}

// Rewrites the original account-picker message in place with the outcome —
// cleaner than leaving stale buttons on screen plus a separate summary message.
async function editMessageText(env, chatId, messageId, text) {
  const res = await fetch(`https://api.telegram.org/bot${env.TG_BOT_TOKEN}/editMessageText`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, message_id: messageId, text }),
  });
  if (!res.ok) {
    console.error("editMessageText failed:", res.status, await res.text());
  }
}
