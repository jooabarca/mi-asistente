// Mi Asistente demo token server (Cloudflare Worker).
//
// The downloadable app has no Gemini key. Before each new conversation it asks this Worker for a Gemini
// session token; the real key stays here as the secret GEMINI_API_KEY and never leaves Cloudflare.
// To shut the demo down: set DISABLED = "true" in wrangler.toml and deploy, or delete the key in AI Studio.

const GOOGLE_TOKENS = "https://generativelanguage.googleapis.com/v1beta/auth_tokens";
// One token = one conversation. It can START a session only in the first 2 minutes (the app connects right
// after asking); after that it can only RESUME that conversation, which the app does every ~10 minutes when
// Google rotates the connection. Every connection counts as a use, so a 19 h conversation needs ~120.
// Google allows under 20 h. A stolen token is useless for starting new sessions.
const NEW_SESSION_MINUTES = 2;
const TOKEN_HOURS = 19;
const USES = 150;

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

export default {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);
    if (pathname === "/") {
      return new Response("Mi Asistente token server: ok\n");
    }
    if (pathname !== "/token" || request.method !== "POST") {
      return json({ error: "Not found." }, 404);
    }
    if (env.DISABLED === "true") {
      return json({ error: "The Mi Asistente demo is turned off." }, 503);
    }
    if (request.headers.get("X-Mi-Asistente") !== "1") {
      return json({ error: "Forbidden." }, 403); // not the app: keeps casual bots and browsers out
    }
    const ip = request.headers.get("CF-Connecting-IP") || "unknown";
    const { success } = await env.PER_IP.limit({ key: ip });
    if (!success) {
      return json({ error: "Too many new sessions from this computer. Wait a minute and try again." }, 429);
    }
    if (!env.GEMINI_API_KEY) {
      return json({ error: "The demo server has no Gemini key yet." }, 503);
    }

    const now = Date.now();
    const google = await fetch(GOOGLE_TOKENS, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-goog-api-key": env.GEMINI_API_KEY },
      body: JSON.stringify({
        uses: USES,
        expireTime: new Date(now + TOKEN_HOURS * 3600_000).toISOString(),
        newSessionExpireTime: new Date(now + NEW_SESSION_MINUTES * 60_000).toISOString(),
        // No bidiGenerateContentSetup (model lock): a locked setup replaces the app's own session settings,
        // so instructions, tools and resumption handles would be ignored.
      }),
    });
    if (!google.ok) {
      const detail = (await google.text()).replace(/\s+/g, " ").slice(0, 400);
      console.log(`Google refused a token: ${google.status} ${detail}`);
      return json({ error: "Google refused to create a session token." }, 502);
    }
    const token = await google.json();
    return json({ token: token.name });
  },
};
