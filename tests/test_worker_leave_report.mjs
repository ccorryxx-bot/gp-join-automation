// Offline test for src/worker.js handleLeaveReport (no network, no Cloudflare).
// Run:  node tests/test_worker_leave_report.mjs
// Covers the 2026-10-17 change: a failed status/scan run ({error}) must show an
// explicit failure, never "Total groups - 0" / "0 candidates" as if it were a result.
import worker from "../src/worker.js";

let failed = 0;
const check = (name, ok, detail = "") => {
  console.log((ok ? "PASS  " : "FAIL  ") + name + (ok ? "" : `   [${detail}]`));
  if (!ok) failed++;
};

function harness() {
  const sent = [], sql = [];
  globalThis.fetch = async (_url, init) => {
    sent.push(JSON.parse(init.body).text);
    return new Response("{}", { status: 200 });
  };
  const env = {
    REPORT_SECRET: "s", ADMIN_TG_ID: "1", TG_BOT_TOKEN: "t",
    DEDUP_KV: { delete: async () => {}, get: async () => null, put: async () => {} },
    DB: {
      prepare: (q) => ({
        bind: (...a) => ({
          run: async () => { sql.push([q, a]); return { meta: {} }; },
          first: async () => null,
          q, a,
        }),
      }),
      batch: async (stmts) => { sql.push(["batch", stmts.length]); },
    },
  };
  const post = (payload) =>
    worker.fetch(new Request("https://w/leave-report", {
      method: "POST", headers: { "X-Report-Secret": "s" }, body: JSON.stringify(payload) }), env, {});
  return { sent, sql, post };
}

{ // status: failed run
  const h = harness();
  const r = await h.post({ mode: "status", account: "CH", scan_id: "1", error: "RuntimeError: missing_secrets:CH_TG_API_ID" });
  check("status error -> 200 + one ❌ notice naming the error",
    r.status === 200 && h.sent.length === 1 && h.sent[0].startsWith("❌ [CH]") && h.sent[0].includes("missing_secrets"), h.sent.join("|"));
  check("status error -> no counts shown", !/Total groups|active groups/.test(h.sent[0] || ""), h.sent[0]);
}
{ // status: normal run unchanged
  const h = harness();
  await h.post({ mode: "status", account: "CH", scan_id: "1", total: 10, active: 6, restricted: 2, paid_messages: 1, read_only: 1, broadcast: 0, unchecked: 0 });
  check("status normal -> counts message unchanged",
    h.sent[0].includes("Total groups - 10") && h.sent[0].includes("active groups - 6") && h.sent[0].includes("restricted groups - 2"), h.sent[0]);
}
{ // scan: failed run
  const h = harness();
  const r = await h.post({ mode: "scan", account: "JL", scan_id: "7", error: "FloodWaitError: 3000s" });
  const marked = h.sql.some(([q, a]) => /SET status = 'failed'/.test(q) && a[1] === 7);
  check("scan error -> scan row marked failed, ❌ notice, NOT '0 candidates'",
    r.status === 200 && marked && h.sent[0].startsWith("❌ [JL]") && !h.sent[0].includes("မတွေ့ပါဘူး 🔍") && !h.sql.some(([q]) => q === "batch"), JSON.stringify({ sent: h.sent, sql: h.sql }));
}
{ // scan: genuine clean result unchanged
  const h = harness();
  await h.post({ mode: "scan", account: "CH", scan_id: "8", total_dialogs: 42, candidates: [] });
  check("scan genuine 0 candidates -> still the normal 'Scan ပြီးပါပြီ' notice",
    h.sent[0].startsWith("🔍 [CH] Scan ပြီးပါပြီ") && h.sent[0].includes("42"), h.sent[0]);
}
{ // wrong secret still rejected
  const h = harness();
  const r = await worker.fetch(new Request("https://w/leave-report", { method: "POST", headers: { "X-Report-Secret": "nope" }, body: "{}" }), { REPORT_SECRET: "s" }, {});
  check("bad secret -> 403", r.status === 403, String(r.status));
}

console.log(failed ? `\n${failed} FAILED` : "\nall passed");
process.exit(failed ? 1 : 0);
