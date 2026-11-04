// Cloudflare Worker: Slack slash command (/ircheck [week]) -> GitHub workflow_dispatch.
// Slack needs an HTTPS endpoint that answers within 3s, and GitHub Actions can't be one,
// so this verifies Slack's signature, kicks off .github/workflows/ir-check.yml, and replies.
// The workflow then posts the real report to the channel via SLACK_WEBHOOK_URL.
//
// Worker secrets/vars: SLACK_SIGNING_SECRET, GITHUB_TOKEN (fine-grained, Actions: read+write
// on this repo only), GH_REPO ("namkurd/fab-management"), GH_REF (default "main").
const enc = (s) => new TextEncoder().encode(s);
const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") return new Response("ok");
    const body = await request.text();
    const ts = request.headers.get("x-slack-request-timestamp");
    const sig = request.headers.get("x-slack-signature") || "";
    if (!ts || Math.abs(Date.now() / 1000 - Number(ts)) > 300) return new Response("stale", { status: 401 });

    const key = await crypto.subtle.importKey("raw", enc(env.SLACK_SIGNING_SECRET),
      { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    const expected = "v0=" + hex(await crypto.subtle.sign("HMAC", key, enc(`v0:${ts}:${body}`)));
    let diff = expected.length ^ sig.length;               // constant-time compare
    for (let i = 0; i < expected.length; i++) diff |= expected.charCodeAt(i) ^ (sig.charCodeAt(i) || 0);
    if (diff !== 0) return new Response("bad signature", { status: 401 });

    const text = (new URLSearchParams(body).get("text") || "").trim();
    const week = /^\d{1,2}$/.test(text) ? text : "";
    ctx.waitUntil(fetch(`https://api.github.com/repos/${env.GH_REPO}/actions/workflows/ir-check.yml/dispatches`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ircheck-slack-relay",
      },
      body: JSON.stringify({ ref: env.GH_REF || "main", inputs: week ? { week } : {} }),
    }));
    return Response.json({ response_type: "ephemeral", text: "Running the IR check… the report will post in the channel in about a minute." });
  },
};
