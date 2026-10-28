// Phase 18 Worker tests. Runs src/worker.js against a REAL SQLite database built
// from schema.sql (node:sqlite, Node >= 22) -- not a fake that returns canned
// rows -- so the actual SQL (scope narrowing, rollback, counts) is exercised.
// Telegram and GitHub are faked at fetch(). Run:  node tests/test_worker_leave_scopes.mjs
//
// What it proves: the two scoped confirm buttons hand the Python run EXACTLY the
// candidates they promise; the extract button exists for every account; a failed
// dispatch rolls back cleanly; big scans stay under Telegram's message limit.
// What it cannot prove: what Telegram/GitHub do in production.
import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import worker from "../src/worker.js";

let failed = 0;
const check = (name, ok, detail = "") => {
  console.log((ok ? "PASS  " : "FAIL  ") + name + (ok ? "" : `   [${detail}]`));
  if (!ok) failed++;
};

function makeWorld({ githubStatus = 204 } = {}) {
  const db = new DatabaseSync(":memory:");
  db.exec(readFileSync(new URL("../schema.sql", import.meta.url), "utf8"));
  const batches = [];
  const wrap = (q) => ({
    bind: (...a) => ({
      run: async () => { db.prepare(q).run(...a); return { meta: {} }; },
      first: async () => db.prepare(q).get(...a) ?? null,
      all: async () => ({ results: db.prepare(q).all(...a) }),
      _q: q, _a: a,
    }),
  });
  const D1 = {
    prepare: wrap,
    batch: async (stmts) => {
      batches.push(stmts.length);
      db.exec("BEGIN");
      try { for (const st of stmts) db.prepare(st._q).run(...st._a); db.exec("COMMIT"); }
      catch (e) { db.exec("ROLLBACK"); throw e; }
    },
  };
  const kv = new Map();
  const log = { tg: [], gh: [] };
  let msgId = 100;
  const world = {
    db, batches, log, kv, github: { status: githubStatus },
    env: {
      REPORT_SECRET: "s", ADMIN_TG_ID: "1", TG_BOT_TOKEN: "t", TG_WEBHOOK_SECRET: "w", GH_PAT: "p",
      DEDUP_KV: {
        get: async (k) => kv.get(k) ?? null,
        put: async (k, v) => { kv.set(k, v); },
        delete: async (k) => { kv.delete(k); },
      },
      DB: D1,
    },
  };
  globalThis.fetch = async (url, init) => {
    const body = init?.body ? JSON.parse(init.body) : {};
    if (String(url).includes("api.github.com")) {
      log.gh.push(body);
      return new Response(world.github.status === 204 ? null : "", { status: world.github.status });
    }
    log.tg.push({ method: String(url).split("/").pop(), ...body });
    return new Response(JSON.stringify({ ok: true, result: { message_id: msgId++ } }), { status: 200 });
  };
  return world;
}

let updateId = 1;
const post = (w, path, payload, headers) =>
  worker.fetch(new Request(`https://w${path}`, { method: "POST", headers, body: JSON.stringify(payload) }), w.env, {});
const report = (w, payload) => post(w, "/leave-report", payload, { "X-Report-Secret": "s" });
const tap = (w, data, from = 1) =>
  post(w, "/", { update_id: updateId++, callback_query: { id: `cq${updateId}`, from: { id: from }, message: { chat: { id: from }, message_id: 5 }, data } },
    { "X-Telegram-Bot-Api-Secret-Token": "w" });
const candidatesFor = async (w, scanId) =>
  (await (await worker.fetch(new Request(`https://w/leave-candidates?scan_id=${scanId}`, { headers: { "X-Report-Secret": "s" } }), w.env, {})).json()).candidates;
const status = (w, id) => w.db.prepare("SELECT status FROM leave_scans WHERE id = ?").get(id).status;
const rowStatuses = (w, id) =>
  Object.fromEntries(w.db.prepare("SELECT peer_id, status FROM leave_candidates WHERE scan_id = ?").all(id).map((r) => [r.peer_id, r.status]));
const lastScanMessage = (w) => [...w.log.tg].reverse().find((m) => m.method === "sendMessage" && m.reply_markup);
const buttons = (m) => m.reply_markup.inline_keyboard.flat();

let nextScan = 1;
async function scan(w, account, candidates, extra = {}) {
  const id = nextScan++;
  w.db.prepare("INSERT INTO leave_scans (id, account, status, created_at) VALUES (?, ?, 'scanning', ?)").run(id, account, Date.now());
  const r = await report(w, { mode: "scan", account, scan_id: String(id), total_dialogs: extra.total ?? 200, candidates, unchecked: extra.unchecked ?? 0 });
  return { id, status: r.status };
}
const cand = (n, reason, o = {}) => ({
  peer_id: String(-1000000000000 - n), peer_type: "channel", title: o.title ?? `${reason} group ${n}`, reason,
  url: o.url === undefined ? `https://t.me/${reason}_${n}` : o.url, ...(o.member_count != null ? { member_count: o.member_count } : {}),
});
const ids = (list) => list.map((c) => c.peer_id).sort();

// ---------------------------------------------------------------------------
{ // 0. schema really loads, and a scan with every reason is stored
  const w = makeWorld();
  const cs = [cand(1, "muted"), cand(2, "read_only"), cand(3, "paid_messages"), cand(4, "under_50_members", { member_count: 9 }), cand(5, "view_only", { member_count: 800 })];
  const s = await scan(w, "CH", cs);
  check("0 scan with all 5 reasons is stored, scan -> awaiting_confirm", s.status === 200 && status(w, s.id) === "awaiting_confirm"
    && w.db.prepare("SELECT COUNT(*) c FROM leave_candidates WHERE scan_id = ?").get(s.id).c === 5);
  const m = lastScanMessage(w);
  check("0 message lists every section incl. the new view-only ones", ["🔇", "🔒", "⭐", "👥", "📺"].every((e) => m.text.includes(e)), m.text);
}

{ // 1. THE ORIGINAL BUG: extract button for accounts with no muted candidate
  const w = makeWorld();
  await scan(w, "JL", [cand(1, "read_only"), cand(2, "paid_messages")]);
  let b = buttons(lastScanMessage(w)).map((x) => x.callback_data);
  check("1 account with NO muted candidates (read-only + paid only) still gets the Extract button", b.some((d) => d.startsWith("extracturls:JL:")), b.join(","));
  await scan(w, "HHNC", [cand(1, "view_only", { member_count: 300 })]);
  b = buttons(lastScanMessage(w)).map((x) => x.callback_data);
  check("1b view-only-only account: Extract + rvp button, NO 'Yes' (nothing in the original scope)",
    b.some((d) => d.startsWith("extracturls:")) && b.some((d) => d.endsWith(":rvp")) && !b.some((d) => d.endsWith(":yes")) && b.some((d) => d.endsWith(":no")), b.join(","));
  await scan(w, "NAWTHON", [cand(1, "muted")]);
  b = buttons(lastScanMessage(w)).map((x) => x.callback_data);
  check("1c muted-only account: Extract + Yes + No, NO rvp button (nothing for it to leave)",
    b.some((d) => d.startsWith("extracturls:")) && b.some((d) => d.endsWith(":yes")) && !b.some((d) => d.endsWith(":rvp")), b.join(","));
}

{ // 2. rvp tap: exactly read-only + paid + view-only + under-50, never muted
  const w = makeWorld();
  const cs = [cand(1, "muted"), cand(2, "muted"), cand(3, "read_only"), cand(4, "paid_messages"), cand(5, "under_50_members", { member_count: 4 }), cand(6, "view_only", { member_count: 900 }), cand(7, "view_only", { member_count: 51 })];
  const s = await scan(w, "NKST_NADI", cs); // underscore account name goes through callback parsing
  const rvp = buttons(lastScanMessage(w)).find((x) => x.callback_data.endsWith(":rvp"));
  check("2 rvp button label shows the right count (5) and callback carries account + scan id", rvp.text.includes("(5)") && rvp.callback_data === `leaveconfirm:NKST_NADI:${s.id}:rvp`, JSON.stringify(rvp));
  const r = await tap(w, rvp.callback_data);
  const got = await candidatesFor(w, s.id);
  check("2 after rvp tap, /leave-candidates returns exactly the 5 in-scope groups, and carries `reason`",
    r.status === 200 && got.length === 5 && got.every((c) => ["read_only", "paid_messages", "under_50_members", "view_only"].includes(c.reason)), JSON.stringify(got));
  const st = rowStatuses(w, s.id);
  check("2 the two MUTED groups are 'not_selected' (not pending, not left)", st["-1000000000001"] === "not_selected" && st["-1000000000002"] === "not_selected", JSON.stringify(st));
  check("2 GitHub was dispatched once: mode=leave, scope=rvp, right account", w.log.gh.length === 1
    && w.log.gh[0].inputs.scope === "rvp" && w.log.gh[0].inputs.mode === "leave" && w.log.gh[0].inputs.account === "NKST_NADI" && w.log.gh[0].inputs.scan_id === String(s.id), JSON.stringify(w.log.gh));
  check("2 scan -> 'leaving', account's leave:running flag set", status(w, s.id) === "leaving" && w.kv.get("leave:running:NKST_NADI") === "1");
  const edit = w.log.tg.find((m) => m.method === "editMessageText");
  check("2 the message is rewritten to say what is leaving (5 ခု, rvp wording)", edit && edit.text.includes("5 ခု") && edit.text.includes("read-only + view-only + paid"), edit?.text);
}

{ // 3. yes tap keeps the original semantics: no big view-only channel
  const w = makeWorld();
  const cs = [cand(1, "muted"), cand(2, "read_only"), cand(3, "paid_messages"), cand(4, "under_50_members", { member_count: 4 }), cand(5, "view_only", { member_count: 900 })];
  const s = await scan(w, "CH", cs);
  await tap(w, `leaveconfirm:CH:${s.id}:yes`);
  const got = await candidatesFor(w, s.id);
  check("3 original 'Yes' leaves muted/read-only/paid/<50 (4) and NOT the 900-member view-only channel",
    got.length === 4 && !got.some((c) => c.reason === "view_only") && rowStatuses(w, s.id)["-1000000000005"] === "not_selected", JSON.stringify(got));
  check("3 dispatched with scope=all", w.log.gh[0].inputs.scope === "all", JSON.stringify(w.log.gh));
}

{ // 4. dispatch failure rolls EVERYTHING back
  const w = makeWorld({ githubStatus: 500 });
  const s = await scan(w, "CH", [cand(1, "muted"), cand(2, "read_only"), cand(3, "view_only", { member_count: 99 })]);
  await tap(w, `leaveconfirm:CH:${s.id}:rvp`);
  const st = rowStatuses(w, s.id);
  check("4 failed dispatch: every candidate back to 'pending', none stuck 'not_selected'", Object.values(st).every((v) => v === "pending"), JSON.stringify(st));
  check("4 failed dispatch: scan back to awaiting_confirm, leave flag cleared, admin told", status(w, s.id) === "awaiting_confirm"
    && !w.kv.has("leave:running:CH") && w.log.tg.some((m) => m.method === "answerCallbackQuery" && String(m.text).includes("Dispatch")));
  w.github.status = 204; // the admin taps again -> works
  await tap(w, `leaveconfirm:CH:${s.id}:rvp`);
  check("4 ...and tapping again after GitHub recovers works", status(w, s.id) === "leaving" && (await candidatesFor(w, s.id)).length === 2);
}

{ // 5. double tap / other button after the first = no second dispatch
  const w = makeWorld();
  const s = await scan(w, "CH", [cand(1, "read_only"), cand(2, "muted")]);
  await tap(w, `leaveconfirm:CH:${s.id}:rvp`);
  await tap(w, `leaveconfirm:CH:${s.id}:rvp`);
  await tap(w, `leaveconfirm:CH:${s.id}:yes`);
  check("5 second tap on either button is a no-op (exactly 1 dispatch, scope stays rvp)", w.log.gh.length === 1 && w.log.gh[0].inputs.scope === "rvp", JSON.stringify(w.log.gh));
  check("5 the muted group was NOT pulled back in by the later 'Yes' tap", rowStatuses(w, s.id)["-1000000000002"] === "not_selected");
}

{ // 6. forged / stale callbacks
  const w = makeWorld();
  const s = await scan(w, "CH", [cand(1, "muted")]);
  await tap(w, `leaveconfirm:CH:${s.id}:rvp`); // nothing in rvp scope
  check("6 rvp tap with nothing in scope: alert, no dispatch, scan still awaiting_confirm, muted untouched",
    w.log.gh.length === 0 && status(w, s.id) === "awaiting_confirm" && rowStatuses(w, s.id)["-1000000000001"] === "pending");
  await tap(w, `leaveconfirm:CH:${s.id}:everything`);
  check("6b unknown option value: rejected, no dispatch, nothing changed", w.log.gh.length === 0 && status(w, s.id) === "awaiting_confirm" && rowStatuses(w, s.id)["-1000000000001"] === "pending");
  await tap(w, `leaveconfirm:CH:${s.id}:yes`, 999); // not the admin
  check("6c a non-admin's tap does nothing", w.log.gh.length === 0 && status(w, s.id) === "awaiting_confirm");
  await tap(w, `leaveconfirm:NOPE:${s.id}:yes`);
  check("6d unknown account in callback_data: rejected", w.log.gh.length === 0);
  await tap(w, `leaveconfirm:CH:${s.id}:no`);
  check("6e No still cancels", status(w, s.id) === "cancelled" && w.log.gh.length === 0);
}

{ // 7. messages already in the admin's chat (before this deploy) still work
  const w = makeWorld();
  w.db.prepare("INSERT INTO leave_scans (id, account, status, created_at) VALUES (900, 'CH', 'awaiting_confirm', ?)").run(Date.now());
  for (const [i, r] of [[1, "muted"], [2, "read_only"], [3, "under_50_members"]])
    w.db.prepare("INSERT INTO leave_candidates (scan_id, peer_id, peer_type, title, reason, status, updated_at) VALUES (900, ?, 'channel', 't', ?, 'pending', 1)").run(String(i), r);
  await tap(w, "leaveconfirm:CH:900:yes");
  check("7 an OLD scan message's 'Yes' (pre-Phase-18 rows, no url column data) leaves all 3, scope=all",
    (await candidatesFor(w, 900)).length === 3 && w.log.gh[0].inputs.scope === "all");
}

{ // 8. unknown reason is normalized once, consistently
  const w = makeWorld();
  const s = await scan(w, "CH", [{ ...cand(1, "muted"), reason: "something_new" }, { ...cand(2, "muted"), reason: undefined }]);
  const rows = w.db.prepare("SELECT reason FROM leave_candidates WHERE scan_id = ?").all(s.id).map((r) => r.reason);
  check("8 unknown / missing reason is stored as 'muted' (the column default), so lists and scopes agree", rows.every((x) => x === "muted"), rows.join(","));
}

{ // 9. big scan: message stays sendable, buttons intact, D1 written in chunks
  const w = makeWorld();
  const cs = [];
  for (let i = 0; i < 400; i++) cs.push(cand(i, ["muted", "read_only", "paid_messages", "under_50_members", "view_only"][i % 5], { title: "Ж".repeat(60) + i, member_count: i }));
  const s = await scan(w, "CH", cs, { unchecked: 3 });
  const m = lastScanMessage(w);
  check("9 400 candidates: message <= 4096 chars (Telegram limit) with all 5 sections and the unchecked note",
    m.text.length <= 4096 && m.text.includes("400 ခု") && m.text.includes("Group 3 ခု") && ["🔇", "🔒", "⭐", "👥", "📺"].every((e) => m.text.includes(e)), `len=${m.text.length}`);
  check("9 400 candidates: all 4 buttons rows still present with counts", buttons(m).length === 4 && buttons(m).find((x) => x.callback_data.endsWith(":rvp")).text.includes("(320)")
    && buttons(m).find((x) => x.callback_data.endsWith(":yes")).text.includes("(320)"), JSON.stringify(buttons(m).map((b) => b.text)));
  check("9 400 rows written, in D1 batches of <= 50", w.db.prepare("SELECT COUNT(*) c FROM leave_candidates WHERE scan_id = ?").get(s.id).c === 400
    && w.batches.length >= 8 && Math.max(...w.batches) <= 50, JSON.stringify(w.batches));
}

{ // 9b. a message that WOULD overflow even at the default listing size: the guard must shrink the list, never drop the buttons
  const w = makeWorld();
  const long = (n) => "Ж".repeat(60) + n;
  const cs = [];
  for (let i = 0; i < 10; i++) {
    cs.push({ ...cand(i * 5, "muted", { title: long(i) }), muted_until: "2026-12-31T00:00:00+00:00" });
    cs.push(cand(i * 5 + 1, "read_only", { title: long(i) }));
    cs.push({ ...cand(i * 5 + 2, "paid_messages", { title: long(i) }), stars_price: 123456 });
    cs.push(cand(i * 5 + 3, "under_50_members", { title: long(i), member_count: 49 }));
    cs.push(cand(i * 5 + 4, "view_only", { title: long(i), member_count: 9999999 }));
  }
  const s = await scan(w, "CH", cs, { unchecked: 2 });
  const m = lastScanMessage(w);
  const lines = m.text.split("\n").filter((l) => /^\d+\. /.test(l)).length;
  check("9b overflowing scan: listing is shrunk (<= 3 titles/section, '...နောက်ထပ် 7 ခု'), message <= 4096, buttons + unchecked note intact",
    m.text.length <= 4096 && m.text.includes("နောက်ထပ် 7 ခု") && lines <= 15 && buttons(m).length === 4 && m.text.includes("Group 2 ခု"),
    `len=${m.text.length} titleLines=${lines}`);
}

{ // 10. extract: every account, all reasons, chunked, nothing lost or duplicated
  const w = makeWorld();
  const cs = [];
  for (let i = 0; i < 150; i++) cs.push(cand(i, ["read_only", "paid_messages", "view_only"][i % 3], { url: i % 10 === 0 ? null : `https://t.me/group_number_${i}_with_a_long_name` }));
  const s = await scan(w, "NAWTHON", cs); // no muted at all -> this is the case that had no button
  const before = w.log.tg.length;
  await tap(w, `extracturls:NAWTHON:${s.id}`);
  const msgs = w.log.tg.slice(before).filter((m) => m.method === "sendMessage").map((m) => m.text);
  const all = msgs.join("\n");
  const urls = cs.filter((c) => c.url).map((c) => c.url);
  check("10 extract for an account with no muted groups works and is split into several messages, each < 4096",
    msgs.length > 1 && msgs.every((t) => t.length <= 4096), `n=${msgs.length} max=${Math.max(...msgs.map((t) => t.length))}`);
  check("10 every public url appears exactly once; private groups are counted, not dropped",
    urls.every((u) => all.split(u).length === 2) && all.includes(`Private/invite-only`), `missing=${urls.filter((u) => all.split(u).length !== 2).length}`);
  check("10 header counts are right", msgs[0].includes(`150 ခုထဲက ${urls.length} ခု url`), msgs[0].split("\n")[0]);
  const w2 = makeWorld();
  const s2 = await scan(w2, "CH", [cand(1, "view_only"), cand(2, "muted"), cand(3, "read_only")]);
  await tap(w2, `extracturls:CH:${s2.id}`);
  const t2 = w2.log.tg.filter((m) => m.method === "sendMessage").pop().text;
  check("10b muted section comes first (the original use of this button)", t2.indexOf("🔇") > -1 && t2.indexOf("🔇") < t2.indexOf("🔒") && t2.indexOf("🔒") < t2.indexOf("📺"), t2);
  const w3 = makeWorld();
  await tap(w3, "extracturls:CH:424242");
  check("10c extract on an unknown/old scan: friendly message, no crash", w3.log.tg.some((m) => m.method === "sendMessage" && m.text.includes("မရှိတော့ပါဘူး")));
}

{ // 11. leave report: not_selected note, honest 'remaining' wording, results chunked
  const w = makeWorld();
  const s = await scan(w, "CH", [cand(1, "muted"), cand(2, "read_only"), cand(3, "view_only", { member_count: 99 }), cand(4, "paid_messages")]);
  await tap(w, `leaveconfirm:CH:${s.id}:rvp`);
  w.log.tg.length = 0;
  const r = await report(w, { mode: "leave", account: "CH", scan_id: String(s.id), aborted_early: true, results: [
    { peer_id: "-1000000000002", status: "left", detail: "" },
    { peer_id: "-1000000000003", status: "skipped", detail: "outside_selected_scope:muted" } ] });
  const t = w.log.tg.find((m) => m.method === "sendMessage").text;
  check("11 finish notice: 1 left, 1 skipped, the muted one 'not selected', 1 still pending -> re-run /leavescan (no stale '3 days cycle')",
    r.status === 200 && t.includes("1 ခု ထွက်ပြီး") && t.includes("1 ခု ကျော်") && t.includes("ရွေးထားတဲ့ option ထဲမပါလို့") && t.includes("/leavescan") && !t.includes("၃ရက်"), t);
  check("11 flag cleared and scan 'done'", status(w, s.id) === "done" && !w.kv.has("leave:running:CH"));
  const big = makeWorld();
  const cs = Array.from({ length: 130 }, (_, i) => cand(i, "view_only", { member_count: 100 }));
  const bs = await scan(big, "CH", cs);
  await tap(big, `leaveconfirm:CH:${bs.id}:rvp`);
  big.batches.length = 0;
  await report(big, { mode: "leave", account: "CH", scan_id: String(bs.id), results: cs.map((c) => ({ peer_id: c.peer_id, status: "left", detail: "" })) });
  check("11b 130 leave results are written in chunks <= 50 and all end 'left'",
    Math.max(...big.batches) <= 50 && big.db.prepare("SELECT COUNT(*) c FROM leave_candidates WHERE scan_id = ? AND status = 'left'").get(bs.id).c === 130, JSON.stringify(big.batches));
}

{ // 12. unchecked is never silent
  const w = makeWorld();
  await scan(w, "CH", [], { total: 50, unchecked: 4 });
  const t = w.log.tg.find((m) => m.method === "sendMessage").text;
  check("12 zero candidates + 4 unchecked: 'no candidates' notice still mentions the 4 left untouched", t.startsWith("🔍 [CH] Scan ပြီးပါပြီ") && t.includes("Group 4 ခု"), t);
  await report(w, { mode: "status", account: "CH", scan_id: "0", total: 10, active: 5, restricted: 1, paid_messages: 1, read_only: 1, broadcast: 1, unchecked: 1 });
  const st = w.log.tg.filter((m) => m.method === "sendMessage").pop().text;
  check("12b status still adds up and labels unchecked honestly", st.includes("Total groups - 10") && st.includes("unchecked") && !st.includes("Telegram error"), st);
}

console.log(failed ? `\n${failed} FAILED` : "\nall passed");
process.exit(failed ? 1 : 0);
