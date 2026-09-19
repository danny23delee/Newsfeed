/**
 * Cloudflare Worker: claim verification proxy for the Morning Brief site.
 *
 * The published GitHub Pages site is public, so it can never hold a real
 * Anthropic API key in its own JavaScript - anyone could open dev tools and
 * steal it. This Worker sits in between: the page sends it a plain-text
 * claim, the Worker calls the Anthropic API using a key that lives only in
 * Cloudflare's encrypted secret store, and returns the answer. The key is
 * never sent to the browser at any point.
 *
 * Setup (see README section "Claim verification search"):
 *   1. Create a Worker in the Cloudflare dashboard, paste this file in.
 *   2. Settings -> Variables and Secrets -> add ANTHROPIC_API_KEY (Secret)
 *      and ALLOWED_ORIGIN (plain text, e.g. https://yourname.github.io).
 *   3. Deploy. Copy the Worker's URL (https://<name>.<subdomain>.workers.dev).
 *   4. Add that URL as the VERIFY_WORKER_URL secret in your GitHub repo.
 */

const CLAUDE_MODEL = "claude-sonnet-4-5";
const MAX_QUERY_LENGTH = 500;

function corsHeaders(env) {
  return {
    "Access-Control-Allow-Origin": env.ALLOWED_ORIGIN || "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  };
}

function jsonResponse(body, status, env) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...corsHeaders(env), "Content-Type": "application/json" },
  });
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders(env) });
    }
    if (request.method !== "POST") {
      return jsonResponse({ error: "Only POST is supported" }, 405, env);
    }

    let body;
    try {
      body = await request.json();
    } catch (e) {
      return jsonResponse({ error: "Invalid JSON body" }, 400, env);
    }

    const query = (body.query || "").trim();
    if (!query) {
      return jsonResponse({ error: "Missing 'query'" }, 400, env);
    }
    if (query.length > MAX_QUERY_LENGTH) {
      return jsonResponse({ error: `Query too long (${MAX_QUERY_LENGTH} character max)` }, 400, env);
    }

    const prompt = `A user saw or heard the following claim, possibly from a short-form video app like TikTok, and wants to know if it's true, checked against real news sources. Search the web and give a clear, factual verdict.

Claim: "${query}"

Respond in this format, plain text, no markdown headers:
1. One-line verdict: True, False, Partly true / Misleading, or Unverified (not enough reliable coverage).
2. A short paragraph (3-5 sentences) explaining the actual facts, based on what you find.
3. A short list of the real news sources you found, with their names, so the user can look further.

Be direct about uncertainty - if you can't find reliable coverage either way, say so clearly rather than guessing or inventing details.`;

    try {
      const anthropicResp = await fetch("https://api.anthropic.com/v1/messages", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-api-key": env.ANTHROPIC_API_KEY,
          "anthropic-version": "2023-06-01",
        },
        body: JSON.stringify({
          model: CLAUDE_MODEL,
          max_tokens: 1024,
          messages: [{ role: "user", content: prompt }],
          tools: [{ type: "web_search_20250305", name: "web_search" }],
        }),
      });

      if (!anthropicResp.ok) {
        const errText = await anthropicResp.text();
        return jsonResponse(
          { error: `Anthropic API error (${anthropicResp.status})`, detail: errText.slice(0, 300) },
          502,
          env
        );
      }

      const data = await anthropicResp.json();
      const text = (data.content || [])
        .filter((block) => block.type === "text")
        .map((block) => block.text)
        .join("\n");

      return jsonResponse({ result: text || "No answer returned." }, 200, env);
    } catch (exc) {
      return jsonResponse({ error: String(exc) }, 500, env);
    }
  },
};
