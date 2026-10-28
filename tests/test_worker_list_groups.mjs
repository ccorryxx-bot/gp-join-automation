// Phase 19 Worker tests: /listgroups_py (src/worker.js). No network, no Cloudflare.
// Telegram and GitHub are faked at fetch(); sendDocument's multipart body is read back
// so the TEXT FILE the admin would receive is checked byte for byte.
// Run:  node tests/test_worker_list_groups.mjs
//
// What it proves: the command/picker/dispatch wiring, the exact .txt format, that a
// failed or malformed run never produces a file that looks like a result, and that a
// list run never clears the leave/scan pause flag.
// What it cannot prove: what Telegram returns for a real account (see README).
import worker from "../src/worker.js";

let failed = 0;
const check = (name, ok, detail = "") => {
  console.log((ok ? "PASS  " : "FAIL  ") + name + (ok ? "" : `   [${detail}]`));
  if (!ok) failed++;
};

function makeWorld({ githubStatus = 204, documentStatus = 200 } = {}) {
  const kv = new Map();
  const deleted = [];
  const log = { messages: [], documents: [], gh: [] };
  const env = {
    REPORT_SECRET: "s", ADMIN_TG_ID: "1", TG_BOT_TOKEN: "t", TG_WEBHOOK_SECRET: "w", GH_PAT: "p",
    DEDUP_KV: {
      get: async (k) => kv.get(k) ?? null,
      put: async (k, v) => { kv.set(k, v); },
      delete: async (k) => { deleted.push(k); kv.delete(k); },
    },
    DB: { prepare: () => { throw new Error("list mode must not touch D1"); } },
  };
  globalThis.fetch = async (url, init) => {
    const u = String(url);
    if (u.includes("api.github.com")) {
      log.gh.push({ url: u, ...JSON.parse(init.body) });
      return new Response(githubStatus === 204 ? null : "", { status: githubStatus });
    }
    const method = u.split("/").pop();
    if (method === "sendDocument") {
      const form = init.body;
      const file = form.get("document");
      log.documents.push({
        chat_id: form.get("chat_id"), caption: form.get("caption"),
        filename: file.name, content: await file.text(), isFormData: form instanceof FormData,
      });
      return new Response(documentStatus === 200 ? "{}" : "nope", { status: documentStatus });
    }
    log.messages.push({ method, ...JSON.parse(init.body) });
    return new Response(JSON.stringify({ ok: true, result: { message_id: 7 } }), { status: 200 });
  };
  return { env, kv, deleted, log };
}

let updateId = 1;
const post = (w, path, payload, headers) =>
  worker.fetch(new Request(`https://w${path}`, { method: "POST", headers, body: JSON.stringify(payload) }), w.env, {});
const report = (w, payload) => post(w, "/leave-report", payload, { "X-Report-Secret": "s" });
const tap = (w, data, from = 1) =>
  post(w, "/", { update_id: updateId++, callback_query: { id: `cq${updateId}`, from: { id: from }, message: { chat: { id: from }, message_id: 5 }, data } },
    { "X-Telegram-Bot-Api-Secret-Token": "w" });
const say = (w, text, from = 1) =>
  post(w, "/", { update_id: updateId++, message: { message_id: 9, chat: { id: from }, from: { id: from }, text } },
    { "X-Telegram-Bot-Api-Secret-Token": "w" });

const IDS = [-1002471129333, -1003773390942, -1003900362963, -1004297726040, -1003891075913, -1003789288457];
const base = { mode: "list", account: "CH", scan_id: "0", total: 9, restricted: 1, paid_messages: 0, read_only: 1, broadcast: 1, unchecked: 0,
  excluded: { channel: 0, monoforum: 0, paid_subscription: 0 } };

// ---------- the .txt file ----------
{
  const w = makeWorld();
  const r = await report(w, { ...base, group_ids: IDS, active: IDS.length });
  const d = w.log.documents[0];
  check("list report -> 200 and exactly one document, no plain-text message", r.status === 200 && w.log.documents.length === 1 && w.log.messages.length === 0, JSON.stringify(w.log));
  check("document goes to the admin as a multipart upload", d && d.isFormData && d.chat_id === "1");
  check("file name is <ACCOUNT>_groupsids.txt", d?.filename === "CH_groupsids.txt", d?.filename);
  const expected = "[-1002471129333,\n    -1003773390942,\n    -1003900362963,\n    -1004297726040,\n    -1003891075913,\n    -1003789288457]";
  check("file content is EXACTLY the owner's format (first id after '[', others on indented lines, ']' on the last)", d?.content === expected, JSON.stringify(d?.content));
  check("caption shows the count and the breakdown", /Active group IDs - 6/.test(d?.caption) && /Total groups - 9/.test(d?.caption) && /restricted 1/.test(d?.caption), d?.caption);
  check("no 'unchecked' / 'ချန်ထား' lines when there is nothing to warn about", !/unchecked|ချန်ထား/.test(d?.caption), d?.caption);
}
{
  const w = makeWorld();
  await report(w, { ...base, account: "NKST_NADI", group_ids: [-1001], active: 1 });
  check("a single id -> '[-1001]'; account with underscore -> NKST_NADI_groupsids.txt",
    w.log.documents[0]?.content === "[-1001]" && w.log.documents[0]?.filename === "NKST_NADI_groupsids.txt", JSON.stringify(w.log.documents));
}
{
  const w = makeWorld();
  const big = Array.from({ length: 2000 }, (_, i) => -1000000000000 - i);
  await report(w, { ...base, group_ids: big, active: big.length });
  const d = w.log.documents[0];
  check("2000 ids (far over Telegram's 4096-char message limit) still go out as ONE file, all ids intact",
    d && d.content.length > 4096 && d.content.split(",\n    ").length === 2000 && d.content.startsWith("[-1000000000000,") && d.content.endsWith("-1000000001999]") && d.caption.length <= 1024, `${d?.content.length}`);
}
{
  const w = makeWorld();
  await report(w, { ...base, group_ids: IDS, active: 6, unchecked: 4, excluded: { channel: 3, monoforum: 1, paid_subscription: 2 } });
  const c = w.log.documents[0].caption;
  check("caption warns about unchecked groups and lists what was left out", /⚠️ unchecked 4/.test(c) && /admin channel 3/.test(c) && /paid subscription 2/.test(c) && /monoforum 1/.test(c), c);
}

// ---------- failures never look like a result ----------
{
  const w = makeWorld();
  const r = await report(w, { mode: "list", account: "CH", scan_id: "0", error: "RuntimeError: missing_secrets:CH_TG_API_ID" });
  check("error report -> 200, one ❌ notice naming the error, NO file", r.status === 200 && w.log.documents.length === 0 && w.log.messages.length === 1
    && w.log.messages[0].text.startsWith("❌ [CH]") && w.log.messages[0].text.includes("missing_secrets"), JSON.stringify(w.log));
}
{
  const w = makeWorld();
  await report(w, { ...base, group_ids: [], active: 0 });
  check("0 active groups -> a text notice, not a '[]' file", w.log.documents.length === 0 && w.log.messages.length === 1 && /Active group IDs - 0/.test(w.log.messages[0].text), JSON.stringify(w.log));
}
for (const [label, bad] of [["group_ids missing", undefined], ["group_ids not an array", "-1001"], ["a non-numeric id", [-1001, "abc"]], ["an id with a comma/newline in it", ["-1001,\n    -1002"]]]) {
  const w = makeWorld();
  const r = await report(w, { ...base, ...(bad === undefined ? {} : { group_ids: bad }) });
  check(`malformed report (${label}) -> 400 + ❌ notice, no file`, r.status === 400 && w.log.documents.length === 0 && w.log.messages[0]?.text.startsWith("❌ [CH]"), `${r.status} ${JSON.stringify(w.log)}`);
}
{
  const w = makeWorld({ documentStatus: 400 });
  const r = await report(w, { ...base, group_ids: IDS, active: 6 });
  check("Telegram refuses the file -> 502 (run goes red) + ⚠️ notice, never a silent success", r.status === 502 && w.log.messages[0]?.text.startsWith("⚠️ [CH]"), `${r.status} ${JSON.stringify(w.log.messages)}`);
}
{
  const w = makeWorld();
  const r = await post(w, "/leave-report", { ...base, group_ids: IDS }, { "X-Report-Secret": "nope" });
  check("wrong report secret -> 403, nothing sent", r.status === 403 && w.log.documents.length === 0 && w.log.messages.length === 0);
}

// ---------- KV flags ----------
{
  const w = makeWorld();
  w.kv.set("listgroups:running:CH", "1");
  w.kv.set("leave:running:CH", "1");
  await report(w, { ...base, group_ids: IDS, active: 6 });
  check("list report clears its OWN running guard", !w.kv.has("listgroups:running:CH"));
  check("list report does NOT clear leave:running (a queued /leavescan keeps its join-pause)", w.kv.get("leave:running:CH") === "1" && !w.deleted.includes("leave:running:CH"), w.deleted.join(","));
}
{
  const w = makeWorld();
  w.kv.set("leave:running:CH", "1");
  w.kv.set("status:running:CH", "1");
  await report(w, { mode: "status", account: "CH", scan_id: "0", total: 1, active: 1, restricted: 0, paid_messages: 0, read_only: 0, broadcast: 0, unchecked: 0 });
  check("regression: a STATUS report still clears leave:running as before", !w.kv.has("leave:running:CH") && !w.kv.has("status:running:CH"));
}

// ---------- command -> picker -> dispatch ----------
{
  const w = makeWorld();
  await say(w, "/listgroups_py");
  const m = w.log.messages.find((x) => x.method === "sendMessage");
  const labels = m?.reply_markup?.inline_keyboard.flat() ?? [];
  const data = labels.map((b) => b.callback_data);
  check("/listgroups_py shows one button per account plus an All button",
    ["CH", "JL", "HHNC", "NAWTHON", "NKST_NADI"].every((a) => data.includes(`listgroups:${a}`)) && data.includes("listgroups:BOTH") && labels.length === 6, JSON.stringify(data));
  check("every picker callback_data fits Telegram's 64-byte limit", data.every((d) => new TextEncoder().encode(d).length <= 64));
}
{
  const w = makeWorld();
  await say(w, "/listgroups_py@SomeBot");
  check("/listgroups_py@BotName works too", w.log.messages.some((x) => x.reply_markup));
  const w2 = makeWorld();
  await say(w2, "/listgroups_py", 999);
  check("a non-admin typing the command gets nothing at all", w2.log.messages.length === 0 && w2.log.gh.length === 0);
}
{
  const w = makeWorld();
  await tap(w, "listgroups:CH");
  check("tapping one account dispatches leave.yml with mode=list, that account, scan_id 0, no scope",
    w.log.gh.length === 1 && w.log.gh[0].url.endsWith("/workflows/leave.yml/dispatches")
    && w.log.gh[0].inputs.mode === "list" && w.log.gh[0].inputs.account === "CH" && w.log.gh[0].inputs.scan_id === "0" && w.log.gh[0].inputs.scope === undefined, JSON.stringify(w.log.gh));
  check("...and sets the running guard", w.kv.has("listgroups:running:CH"));
  await tap(w, "listgroups:CH");
  check("double-tap while running -> friendly reply, NO second dispatch", w.log.gh.length === 1 && w.log.messages.some((x) => /အရင်ကတည်းက run နေ/.test(x.text ?? "")), JSON.stringify(w.log.messages));
}
{
  const w = makeWorld();
  await tap(w, "listgroups:BOTH");
  const accts = w.log.gh.map((g) => g.inputs.account).sort();
  check("All -> one independent list run per account (5), none merged",
    JSON.stringify(accts) === JSON.stringify(["CH", "HHNC", "JL", "NAWTHON", "NKST_NADI"]) && w.log.gh.every((g) => g.inputs.mode === "list"), JSON.stringify(accts));
}
{
  const w = makeWorld({ githubStatus: 422 });
  await tap(w, "listgroups:JL");
  check("GitHub dispatch fails -> guard released so the admin can just try again", !w.kv.has("listgroups:running:JL") && w.log.messages.some((x) => /Dispatch မအောင်မြင်/.test(x.text ?? "")), JSON.stringify(w.log.messages));
}
{
  const w = makeWorld();
  await tap(w, "listgroups:NOPE");
  check("unknown account in callback_data -> rejected, nothing dispatched", w.log.gh.length === 0);
}
{
  const w = makeWorld();
  await tap(w, "listgroups:CH", 999);
  check("a non-admin tapping the button dispatches nothing", w.log.gh.length === 0);
}
{
  const w = makeWorld();
  await say(w, "/help");
  const text = w.log.messages.find((x) => x.method === "sendMessage")?.text ?? "";
  check("/help lists the new command (built from BOT_COMMANDS)", text.includes("/listgroups_py"), text.slice(0, 200));
}

console.log(failed ? `\n${failed} FAILED` : "\nall passed");
process.exit(failed ? 1 : 0);
