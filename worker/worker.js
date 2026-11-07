// Serveur Cotescope (Cloudflare Worker)
// - vérifie l'abonnement (clé de licence Whop) sans jamais exposer la clé API Whop
// - sert les données collectées (snapshot) et le moteur d'analyse aux abonnés seulement
//
// Secrets (wrangler secret) : WHOP_API_KEY, OWNER_KEYS (clés d'accès gratuites, séparées par des virgules)
// KV : KV (clés « snapshot » et « engine », JSON compressé gzip)

const ALLOWED_ORIGINS = [
  "https://cotescope.fr",
  "https://www.cotescope.fr",
  "https://logicart19.github.io",
];
const OK_STATUSES = new Set(["active", "trialing", "completed"]);
const LICENSE_CACHE_SECONDS = 600;

function cors(req) {
  const o = req.headers.get("Origin");
  const h = {
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Max-Age": "86400",
    "Vary": "Origin",
  };
  if (o && ALLOWED_ORIGINS.includes(o)) h["Access-Control-Allow-Origin"] = o;
  return h;
}

function json(req, data, status = 200, extra = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
      ...cors(req),
      ...extra,
    },
  });
}

async function sha256(text) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function cleanKey(k) {
  k = String(k || "").trim();
  return /^[A-Za-z0-9_\-]{6,80}$/.test(k) ? k : "";
}

async function checkLicense(key, env) {
  key = cleanKey(key);
  if (!key) return { ok: false, reason: "invalid" };
  const owners = String(env.OWNER_KEYS || "").split(",").map((s) => s.trim()).filter(Boolean);
  if (owners.includes(key)) return { ok: true, status: "owner", plan: "owner" };

  const cache = typeof caches !== "undefined" ? caches.default : null;
  const cacheKey = new Request("https://cache.cotescope/lic/" + (await sha256(key)));
  if (cache) {
    const hit = await cache.match(cacheKey);
    if (hit) return hit.json();
  }
  if (!env.WHOP_API_KEY) return { ok: false, reason: "server_not_configured" };

  let res;
  try {
    const r = await fetch("https://api.whop.com/api/v1/memberships/" + encodeURIComponent(key), {
      headers: { Authorization: "Bearer " + env.WHOP_API_KEY, Accept: "application/json" },
    });
    if (r.status === 404 || r.status === 400) res = { ok: false, reason: "unknown_key" };
    else if (!r.ok) return { ok: false, reason: "whop_error_" + r.status }; // non mis en cache
    else {
      const m = await r.json();
      const status = String(m.status || "").toLowerCase();
      const ok = OK_STATUSES.has(status) || m.valid === true;
      res = {
        ok,
        status,
        reason: ok ? "" : "inactive",
        renews: m.renewal_period_end || m.expires_at || null,
        manage: m.manage_url || null,
      };
    }
  } catch (e) {
    return { ok: false, reason: "whop_unreachable" };
  }
  if (cache) {
    await cache.put(cacheKey, new Response(JSON.stringify(res), {
      headers: { "Cache-Control": "max-age=" + LICENSE_CACHE_SECONDS },
    }));
  }
  return res;
}

async function kvJson(env, name) {
  const cache = typeof caches !== "undefined" ? caches.default : null;
  const cacheKey = new Request("https://cache.cotescope/kv/" + name);
  if (cache) {
    const hit = await cache.match(cacheKey);
    if (hit) return hit.text();
  }
  const buf = await env.KV.get(name, "arrayBuffer");
  if (!buf) return null;
  const stream = new Blob([buf]).stream().pipeThrough(new DecompressionStream("gzip"));
  const text = await new Response(stream).text();
  if (cache) {
    await cache.put(cacheKey, new Response(text, { headers: { "Cache-Control": "max-age=60" } }));
  }
  return text;
}

function bearer(req) {
  const h = req.headers.get("Authorization") || "";
  return h.startsWith("Bearer ") ? h.slice(7) : "";
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: cors(req) });

    if (url.pathname === "/api/health") return json(req, { ok: true, time: Date.now() });

    if (url.pathname === "/api/check" && req.method === "POST") {
      let body = {};
      try { body = await req.json(); } catch (e) { /* vide */ }
      const r = await checkLicense(body.license || bearer(req), env);
      return json(req, r, r.ok ? 200 : 403);
    }

    if (url.pathname === "/api/snapshot" || url.pathname === "/api/engine") {
      const lic = await checkLicense(bearer(req), env);
      if (!lic.ok) return json(req, lic, 403);
      const text = await kvJson(env, url.pathname === "/api/snapshot" ? "snapshot" : "engine");
      if (!text) return json(req, { ok: false, reason: "no_data_yet" }, 503);
      return new Response(text, {
        headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "private, no-store",
                   "X-Content-Type-Options": "nosniff", ...cors(req) },
      });
    }
    return json(req, { ok: false, reason: "not_found" }, 404);
  },
};
