// gp-join-automation — Cloudflare Worker
// TODO Phase 2: webhook-handler (dedup + enqueue + instant-ack)
// TODO Phase 3: dispatcher (Cron Trigger -> GitHub workflow_dispatch)
// TODO Phase 5: /report endpoint

export default {
  async fetch(request, env, ctx) {
    return new Response("gp-join-automation: not implemented yet", { status: 501 });
  },
  async scheduled(event, env, ctx) {
    // TODO Phase 3: dispatcher tick
  }
};
