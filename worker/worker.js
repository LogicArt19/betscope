// Serveur Cotescope (Cloudflare Worker)
// - comptes Cotescope (e-mail + mot de passe), sessions signées
// - abonnement : paiement Whop relié au compte (métadonnées du paiement), 3 forfaits
// - sert les données collectées (une version par forfait) et le moteur d'analyse aux abonnés
//
// Secrets : WHOP_API_KEY, WHOP_COMPANY_ID (biz_...), PLAN_IDS (JSON {"essentiel":"plan_..","pro":"plan_..","elite":"plan_.."}),
//           SESSION_SECRET, OWNER_EMAILS (e-mails avec accès Elite gratuit, séparés par des virgules),
//           WHOP_WEBHOOK_SECRET (optionnel), RESEND_KEY + MAIL_FROM (optionnels : e-mails de mot de passe oublié)
// KV : KV (comptes « u:<email> », données « snapshot_<forfait> », « engine »)

const ALLOWED_ORIGINS = [
  "https://cotescope.fr",
  "https://www.cotescope.fr",
  "https://logicart19.github.io",
];
const APP_URL = "https://cotescope.fr/app/";
const TIERS = ["essentiel", "pro", "elite"];
const FREE_DAYS = 7; // version gratuite : 7 jours après la création du compte, sans carte
const OK_STATUSES = new Set(["active", "trialing", "completed", "past_due"]);
const ACCESS_CACHE_SECONDS = 300;
const SESSION_DAYS = 60;
const PBKDF2_ITER = 60000;

/* ---------------- utilitaires ---------------- */
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
function json(req, data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff", ...cors(req),
    },
  });
}
const enc = new TextEncoder();
const b64u = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const fromB64u = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)), (c) => c.charCodeAt(0));
const rand = (n = 16) => b64u(crypto.getRandomValues(new Uint8Array(n)));

function eq(a, b) { // comparaison à temps constant
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}
async function hmac(secret, data) {
  const k = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return b64u(await crypto.subtle.sign("HMAC", k, enc.encode(data)));
}
async function hashPassword(pw, salt) {
  const k = await crypto.subtle.importKey("raw", enc.encode(pw), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt: fromB64u(salt), iterations: PBKDF2_ITER }, k, 256);
  return b64u(bits);
}
const cleanEmail = (e) => {
  e = String(e || "").trim().toLowerCase();
  return /^[^\s@]{1,64}@[^\s@]{1,190}\.[a-z]{2,}$/.test(e) ? e : "";
};
const plans = (env) => { try { return JSON.parse(env.PLAN_IDS || "{}"); } catch (e) { return {}; } };
const owners = (env) => String(env.OWNER_EMAILS || "").toLowerCase().split(",").map((s) => s.trim()).filter(Boolean);

/* ---------------- sessions ---------------- */
async function makeSession(env, user) {
  const body = b64u(enc.encode(JSON.stringify({ e: user.email, v: user.pv || 1, x: Date.now() + SESSION_DAYS * 864e5 })));
  return body + "." + (await hmac(env.SESSION_SECRET, body));
}
async function readSession(env, req) {
  const h = req.headers.get("Authorization") || "";
  const tok = h.startsWith("Bearer ") ? h.slice(7) : "";
  const [body, sig] = tok.split(".");
  if (!body || !sig || !env.SESSION_SECRET) return null;
  if (!eq(sig, await hmac(env.SESSION_SECRET, body))) return null;
  let p;
  try { p = JSON.parse(new TextDecoder().decode(fromB64u(body))); } catch (e) { return null; }
  if (!p.e || p.x < Date.now()) return null;
  const user = await getUser(env, p.e);
  if (!user || (user.pv || 1) !== p.v) return null; // mot de passe changé = sessions coupées
  return user;
}
const getUser = async (env, email) => env.KV.get("u:" + email, "json");
const putUser = (env, u) => env.KV.put("u:" + u.email, JSON.stringify(u));

/* ---------------- Whop ---------------- */
async function whop(env, path, opts = {}) {
  const r = await fetch("https://api.whop.com/api/v1" + path, {
    ...opts,
    headers: { Authorization: "Bearer " + env.WHOP_API_KEY, Accept: "application/json", "Content-Type": "application/json" },
  });
  if (!r.ok) throw new Error("whop_" + r.status);
  return r.json();
}
function tierOfPlan(env, planId) {
  const p = plans(env);
  for (const t of TIERS) if (p[t] && p[t] === planId) return t;
  return null;
}
// cherche les abonnements payés depuis ce compte (métadonnées « cotescope_account »)
async function findMemberships(env, email, sinceMs) {
  const q = new URLSearchParams({ first: "100", direction: "desc", order: "created_at" });
  if (env.WHOP_COMPANY_ID) q.set("account_id", env.WHOP_COMPANY_ID);
  if (sinceMs) q.set("created_after", new Date(sinceMs).toISOString());
  const r = await whop(env, "/memberships?" + q.toString());
  return (r.data || []).filter((m) => String((m.metadata || {}).cotescope_account || "").toLowerCase() === email).map((m) => m.id);
}
async function access(env, user, fresh = false) {
  if (owners(env).includes(user.email)) return { tier: "elite", status: "owner" };
  const cache = typeof caches !== "undefined" ? caches.default : null;
  const ck = new Request("https://cache.cotescope/acc/" + encodeURIComponent(user.email));
  if (cache && !fresh) {
    const hit = await cache.match(ck);
    if (hit) return hit.json();
  }
  let ids = user.memberships || [];
  // paiement récent pas encore relié : on le retrouve grâce aux métadonnées
  if (user.pending && Date.now() - user.pending < 3 * 864e5) {
    try {
      const found = await findMemberships(env, user.email, user.pending - 36e5);
      const add = found.filter((id) => !ids.includes(id));
      if (add.length) {
        ids = [...ids, ...add];
        user.memberships = ids;
        delete user.pending;
        await putUser(env, user);
      }
    } catch (e) { /* on réessaiera */ }
  }
  let best = { tier: null, status: ids.length ? "inactive" : "none" };
  for (const id of ids.slice(-5)) {
    try {
      const m = await whop(env, "/memberships/" + encodeURIComponent(id));
      const st = String(m.status || "").toLowerCase();
      const t = tierOfPlan(env, m.variant_id || m.plan_id || (m.plan || {}).id);
      if (OK_STATUSES.has(st) && t && (!best.tier || TIERS.indexOf(t) > TIERS.indexOf(best.tier))) {
        best = { tier: t, status: st, renews: m.current_period_end || m.renewal_period_end || null,
                 cancel: !!m.cancel_at_period_end, manage: m.manage_url || null };
      }
    } catch (e) {
      if (String(e.message).startsWith("whop_5")) return { tier: null, status: "whop_unreachable" };
    }
  }
  if (!best.tier && user.created && Date.now() - user.created < FREE_DAYS * 864e5) {
    best = { tier: "gratuit", status: "free", renews: new Date(user.created + FREE_DAYS * 864e5).toISOString() };
  } else if (!best.tier && best.status === "none" && user.created) best.status = "free_over";
  if (cache) await cache.put(ck, new Response(JSON.stringify(best), { headers: { "Cache-Control": "max-age=" + ACCESS_CACHE_SECONDS } }));
  return best;
}

/* ---------------- données ---------------- */
async function kvJson(env, name) {
  const cache = typeof caches !== "undefined" ? caches.default : null;
  const ck = new Request("https://cache.cotescope/kv/" + name);
  if (cache) {
    const hit = await cache.match(ck);
    if (hit) return hit.text();
  }
  const buf = await env.KV.get(name, "arrayBuffer");
  if (!buf) return null;
  const text = await new Response(new Blob([buf]).stream().pipeThrough(new DecompressionStream("gzip"))).text();
  if (cache) await cache.put(ck, new Response(text, { headers: { "Cache-Control": "max-age=60" } }));
  return text;
}

/* ---------------- limite d'essais (mots de passe) ---------------- */
async function tooMany(req, key) {
  const cache = typeof caches !== "undefined" ? caches.default : null;
  if (!cache) return false;
  const ip = req.headers.get("CF-Connecting-IP") || "x";
  const ck = new Request("https://cache.cotescope/rl/" + encodeURIComponent(key + "|" + ip));
  const hit = await cache.match(ck);
  const n = hit ? Number(await hit.text()) : 0;
  await cache.put(ck, new Response(String(n + 1), { headers: { "Cache-Control": "max-age=900" } }));
  return n >= 10;
}

// version gratuite : 3 comptes max par connexion internet et par jour (contre les comptes à la chaîne)
async function freeQuota(req, add) {
  const cache = typeof caches !== "undefined" ? caches.default : null;
  if (!cache) return false;
  const ip = req.headers.get("CF-Connecting-IP") || "x";
  const ck = new Request("https://cache.cotescope/free/" + encodeURIComponent(ip));
  const hit = await cache.match(ck);
  const n = hit ? Number(await hit.text()) : 0;
  if (add) await cache.put(ck, new Response(String(n + 1), { headers: { "Cache-Control": "max-age=86400" } }));
  return n >= 3;
}

async function sendMail(env, to, subject, html) {
  if (!env.RESEND_KEY) return false;
  const r = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: { Authorization: "Bearer " + env.RESEND_KEY, "Content-Type": "application/json" },
    body: JSON.stringify({ from: env.MAIL_FROM || "Cotescope <compte@cotescope.fr>", to: [to], subject, html }),
  });
  return r.ok;
}

async function verifyWebhook(env, req, raw) {
  const id = req.headers.get("webhook-id"), ts = req.headers.get("webhook-timestamp"), sig = req.headers.get("webhook-signature") || "";
  if (!env.WHOP_WEBHOOK_SECRET || !id || !ts) return false;
  if (Math.abs(Date.now() / 1000 - Number(ts)) > 300) return false;
  const k = await crypto.subtle.importKey("raw", enc.encode(env.WHOP_WEBHOOK_SECRET), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = btoa(String.fromCharCode(...new Uint8Array(await crypto.subtle.sign("HMAC", k, enc.encode(`${id}.${ts}.${raw}`)))));
  return sig.split(" ").some((s) => eq(s.replace(/^v1,/, ""), mac));
}

function pub(user, acc) {
  return { ok: true, email: user.email, name: user.name || "", tier: acc.tier, status: acc.status,
           renews: acc.renews || null, cancel: !!acc.cancel, manage: acc.manage || null,
           trial_used: !!(user.memberships || []).length };
}

/* ---------------- routes ---------------- */
export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const p = url.pathname;
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: cors(req) });
    if (p === "/api/health") return json(req, { ok: true, time: Date.now() });
    if (p === "/api/public/stats") { // bilan public des pronostics (sans connexion)
      const text = await kvJson(env, "public_stats");
      return new Response(text || '{"n":0}', { headers: { "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "public, max-age=600", ...cors(req) } });
    }
    if (!env.SESSION_SECRET && p !== "/api/whop/webhook") return json(req, { ok: false, reason: "server_not_configured" }, 503);
    let body = {};
    if (req.method === "POST" && p !== "/api/whop/webhook") { try { body = await req.json(); } catch (e) { /* vide */ } }

    // --- comptes ---
    if (p === "/api/signup" && req.method === "POST") {
      const email = cleanEmail(body.email), pw = String(body.password || "");
      if (!email) return json(req, { ok: false, reason: "bad_email" }, 400);
      if (pw.length < 8 || pw.length > 200) return json(req, { ok: false, reason: "weak_password" }, 400);
      if (await tooMany(req, "signup")) return json(req, { ok: false, reason: "too_many" }, 429);
      if (await getUser(env, email)) return json(req, { ok: false, reason: "email_taken" }, 409);
      if (await freeQuota(req, false)) return json(req, { ok: false, reason: "too_many_accounts" }, 429);
      const salt = rand(16);
      const user = { email, name: String(body.name || "").slice(0, 60), salt, hash: await hashPassword(pw, salt), pv: 1,
                     created: Date.now(), memberships: [] };
      await putUser(env, user);
      await freeQuota(req, true);
      return json(req, { ...pub(user, await access(env, user)), session: await makeSession(env, user) });
    }
    if (p === "/api/login" && req.method === "POST") {
      const email = cleanEmail(body.email), pw = String(body.password || "");
      if (await tooMany(req, "login:" + email)) return json(req, { ok: false, reason: "too_many" }, 429);
      const user = email && (await getUser(env, email));
      if (!user || !eq(await hashPassword(pw, user.salt), user.hash)) return json(req, { ok: false, reason: "bad_login" }, 401);
      return json(req, { ...pub(user, await access(env, user)), session: await makeSession(env, user) });
    }
    if (p === "/api/forgot" && req.method === "POST") {
      const email = cleanEmail(body.email);
      if (await tooMany(req, "forgot")) return json(req, { ok: false, reason: "too_many" }, 429);
      const user = email && (await getUser(env, email));
      if (!env.RESEND_KEY) return json(req, { ok: false, reason: "mail_not_configured" }, 503);
      if (user) {
        const code = rand(24);
        await env.KV.put("reset:" + code, email, { expirationTtl: 3600 });
        await sendMail(env, email, "Cotescope : nouveau mot de passe",
          `<p>Bonjour,</p><p>Pour choisir un nouveau mot de passe, clique ici (lien valable 1 heure) :</p>
           <p><a href="${APP_URL}#reset=${code}">Choisir un nouveau mot de passe</a></p>
           <p>Si tu n'as rien demandé, ignore cet e-mail.</p>`);
      }
      return json(req, { ok: true }); // même réponse que le compte existe ou non
    }
    if (p === "/api/reset" && req.method === "POST") {
      const code = String(body.code || ""), pw = String(body.password || "");
      if (pw.length < 8) return json(req, { ok: false, reason: "weak_password" }, 400);
      const email = code && (await env.KV.get("reset:" + code));
      const user = email && (await getUser(env, email));
      if (!user) return json(req, { ok: false, reason: "bad_code" }, 400);
      user.salt = rand(16); user.hash = await hashPassword(pw, user.salt); user.pv = (user.pv || 1) + 1;
      await putUser(env, user);
      await env.KV.delete("reset:" + code);
      return json(req, { ...pub(user, await access(env, user)), session: await makeSession(env, user) });
    }
    if (p === "/api/password" && req.method === "POST") {
      const user = await readSession(env, req);
      if (!user) return json(req, { ok: false, reason: "session" }, 401);
      if (!eq(await hashPassword(String(body.old || ""), user.salt), user.hash)) return json(req, { ok: false, reason: "bad_login" }, 401);
      if (String(body.password || "").length < 8) return json(req, { ok: false, reason: "weak_password" }, 400);
      user.salt = rand(16); user.hash = await hashPassword(String(body.password), user.salt); user.pv = (user.pv || 1) + 1;
      await putUser(env, user);
      return json(req, { ok: true, session: await makeSession(env, user) });
    }

    // --- compte connecté ---
    const user = await readSession(env, req);
    if (p === "/api/me") {
      if (!user) return json(req, { ok: false, reason: "session" }, 401);
      return json(req, pub(user, await access(env, user, url.searchParams.has("fresh"))));
    }
    if (p === "/api/checkout" && req.method === "POST") {
      if (!user) return json(req, { ok: false, reason: "session" }, 401);
      const tier = TIERS.includes(body.tier) ? body.tier : "";
      const plan = plans(env)[tier];
      if (!plan || !env.WHOP_API_KEY) return json(req, { ok: false, reason: "plans_not_configured" }, 503);
      const ret = ALLOWED_ORIGINS.some((o) => String(body.return || "").startsWith(o + "/")) ? body.return : APP_URL;
      let r;
      try {
        r = await whop(env, "/checkout_configurations", {
          method: "POST",
          body: JSON.stringify({ plan_id: plan, metadata: { cotescope_account: user.email }, redirect_url: ret }),
        });
      } catch (e) { return json(req, { ok: false, reason: "whop_unreachable" }, 502); }
      user.pending = Date.now();
      await putUser(env, user);
      return json(req, { ok: true, url: r.purchase_url });
    }
    if (p === "/api/snapshot" || p === "/api/engine") {
      if (!user) return json(req, { ok: false, reason: "session" }, 401);
      const acc = await access(env, user);
      if (!acc.tier) return json(req, { ok: false, reason: ["whop_unreachable", "free_over"].includes(acc.status) ? acc.status : "no_plan" }, 403);
      const text = await kvJson(env, p === "/api/engine" ? "engine" : "snapshot_" + acc.tier);
      if (!text) return json(req, { ok: false, reason: "no_data_yet" }, 503);
      return new Response(text, {
        headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "private, no-store",
                   "X-Content-Type-Options": "nosniff", "X-Cotescope-Tier": acc.tier, ...cors(req) },
      });
    }

    // --- Whop → compte (optionnel : sans ce webhook, le paiement est retrouvé à la connexion) ---
    if (p === "/api/whop/webhook" && req.method === "POST") {
      const raw = await req.text();
      if (!(await verifyWebhook(env, req, raw))) return new Response("bad signature", { status: 401 });
      let ev = {};
      try { ev = JSON.parse(raw); } catch (e) { /* vide */ }
      const m = ev.data || {};
      const id = String(ev.type || "").startsWith("payment.") ? m.membership_id || (m.membership || {}).id : m.id;
      const email = cleanEmail((m.metadata || {}).cotescope_account);
      if (id && email) {
        const u = await getUser(env, email);
        if (u && !(u.memberships || []).includes(id)) {
          u.memberships = [...(u.memberships || []), id];
          delete u.pending;
          await putUser(env, u);
          if (typeof caches !== "undefined") await caches.default.delete(new Request("https://cache.cotescope/acc/" + encodeURIComponent(email)));
        }
      }
      return new Response("ok");
    }
    return json(req, { ok: false, reason: "not_found" }, 404);
  },
};
