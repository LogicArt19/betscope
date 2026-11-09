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
const TIERS = ["essentiel", "pro", "elite"]; // « elite » : ancien forfait, plus vendu (traité comme Pro)
const FREE_DAYS = 7; // version gratuite : 7 jours après la création du compte, sans carte
const OK_STATUSES = new Set(["active", "trialing", "completed", "past_due"]);
const ACCESS_CACHE_SECONDS = 300;
const SESSION_DAYS = 60;
const PBKDF2_ITER = 100000;     // maximum accepté par Cloudflare ; les anciens mots de passe sont re-hachés à la connexion
const MAX_DEVICES = 5;          // appareils connectés en même temps sur un compte (au-delà, le plus ancien est déconnecté)
const MAX_BODY = 8192;          // taille max d'une requête
const LOCK_FAILS = 8;           // échecs avant blocage du compte (toutes connexions confondues)
const LOCK_MINUTES = 30;

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
// en-têtes de sécurité sur toutes les réponses de l'API
const SEC = {
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Referrer-Policy": "no-referrer",
  "Strict-Transport-Security": "max-age=63072000; includeSubDomains; preload",
  "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
  "Cross-Origin-Resource-Policy": "cross-origin",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
};
function json(req, data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", ...SEC, ...cors(req) },
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
async function hashPassword(pw, salt, iter = PBKDF2_ITER) {
  const k = await crypto.subtle.importKey("raw", enc.encode(pw), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt: fromB64u(salt), iterations: iter }, k, 256);
  return b64u(bits);
}
// le « poivre » (SESSION_SECRET, jamais stocké avec les comptes) : une fuite de la base seule ne suffit pas à tester des mots de passe
const pepper = (env, pw) => pw + "\u0000" + (env.SESSION_SECRET || "");
async function checkPassword(env, user, pw) {
  if (user.iter === PBKDF2_ITER && user.pep) return eq(await hashPassword(pepper(env, pw), user.salt), user.hash);
  const ok = eq(await hashPassword(pw, user.salt, user.iter || 60000), user.hash);
  if (ok) await setPassword(env, user, pw, false); // mise à niveau silencieuse de l'ancien hachage
  return ok;
}
async function setPassword(env, user, pw, newVersion = true) {
  user.salt = rand(16);
  user.hash = await hashPassword(pepper(env, pw), user.salt);
  user.iter = PBKDF2_ITER;
  user.pep = 1;
  if (newVersion) { user.pv = (user.pv || 1) + 1; user.sids = []; } // nouveau mot de passe = tous les appareils déconnectés
}
// faux calcul quand le compte n'existe pas : même durée de réponse, on ne peut pas deviner quels e-mails ont un compte
const DUMMY_SALT = "c290c2NvcGUtZHVtbXk";
const burn = (pw) => hashPassword(String(pw || ""), DUMMY_SALT);

// mot de passe déjà fuité ? (Have I Been Pwned, k-anonymat : seuls les 5 premiers caractères du SHA-1 sortent)
async function pwned(pw) {
  try {
    const h = [...new Uint8Array(await crypto.subtle.digest("SHA-1", enc.encode(pw)))].map((b) => b.toString(16).padStart(2, "0")).join("").toUpperCase();
    const r = await fetch("https://api.pwnedpasswords.com/range/" + h.slice(0, 5), { headers: { "Add-Padding": "true" }, cf: { cacheTtl: 86400 } });
    if (!r.ok) return 0;
    const line = (await r.text()).split("\n").find((l) => l.startsWith(h.slice(5)));
    return line ? Number(line.split(":")[1]) || 0 : 0;
  } catch (e) { return 0; } // service indisponible : on ne bloque pas l'inscription
}
const COMMON = new Set(["cotescope", "motdepasse", "azertyuiop", "password", "12345678", "123456789", "1234567890", "football", "betclic", "winamax", "qwertyuiop", "11111111", "00000000", "abcdefgh", "iloveyou", "doudou123", "marseille", "allezlom", "parisfc", "soleil123"]);
async function weakReason(email, pw) {
  if (pw.length < 8) return "weak_password";
  if (pw.length > 200) return "weak_password";
  const low = pw.toLowerCase();
  if (COMMON.has(low) || low.includes(email.split("@")[0]) && email.split("@")[0].length >= 4) return "common_password";
  if (new Set(pw).size < 5) return "common_password";
  if ((await pwned(pw)) > 0) return "pwned_password";
  return "";
}
const cleanEmail = (e) => {
  e = String(e || "").trim().toLowerCase();
  return /^[^\s@]{1,64}@[^\s@]{1,190}\.[a-z]{2,}$/.test(e) ? e : "";
};
const plans = (env) => { try { return JSON.parse(env.PLAN_IDS || "{}"); } catch (e) { return {}; } };
const owners = (env) => String(env.OWNER_EMAILS || "").toLowerCase().split(",").map((s) => s.trim()).filter(Boolean);

/* ---------------- sessions ---------------- */
function deviceName(req) {
  const ua = req.headers.get("User-Agent") || "";
  const os = /iPhone|iPad/.test(ua) ? "iPhone / iPad" : /Android/.test(ua) ? "Android" : /Windows/.test(ua) ? "Windows" : /Mac OS/.test(ua) ? "Mac" : /Linux/.test(ua) ? "Linux" : "Appareil";
  const br = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : "navigateur";
  return `${br} · ${os}`;
}
// chaque connexion = un appareil ; MAX_DEVICES au plus (contre le partage de compte et pour pouvoir couper un appareil volé)
async function makeSession(env, user, req) {
  const sid = rand(9);
  const country = (req && req.cf && req.cf.country) || "";
  user.sids = [...(user.sids || []), { id: sid, t: Date.now(), d: req ? deviceName(req) : "", c: country }].slice(-MAX_DEVICES);
  await putUser(env, user);
  const body = b64u(enc.encode(JSON.stringify({ e: user.email, v: user.pv || 1, s: sid, x: Date.now() + SESSION_DAYS * 864e5 })));
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
  if (user.sids && !user.sids.some((d) => d.id === p.s)) return null; // appareil déconnecté (ou remplacé par un plus récent)
  user._sid = p.s;
  return user;
}
const getUser = async (env, email) => env.KV.get("u:" + email, "json");
const putUser = (env, u) => { const { _sid, ...rest } = u; return env.KV.put("u:" + u.email, JSON.stringify(rest)); };

/* ---------------- Whop ---------------- */
async function whop(env, path, opts = {}) {
  const r = await fetch("https://api.whop.com/api/v1" + path, {
    ...opts,
    headers: { Authorization: "Bearer " + env.WHOP_API_KEY, Accept: "application/json", "Content-Type": "application/json" },
  });
  if (!r.ok) {
    const detail = (await r.text().catch(() => "")).slice(0, 300);
    console.log("Whop", opts.method || "GET", path.split("?")[0], r.status, detail);
    throw Object.assign(new Error("whop_" + r.status), { status: r.status, detail });
  }
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
  // version gratuite permanente (Ligue 1, Premier League, Liga, 1 ticket Objectif par jour)
  if (!best.tier) best = { tier: "gratuit", status: "free", renews: null, prev: best.status };
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

// blocage du compte après trop d'échecs, quelle que soit la connexion utilisée (contre les attaques réparties)
async function lockState(env, email) {
  const v = await env.KV.get("lock:" + email, "json");
  return v || { n: 0, until: 0 };
}
async function failLogin(env, email) {
  const st = await lockState(env, email);
  st.n += 1;
  if (st.n >= LOCK_FAILS) { st.until = Date.now() + LOCK_MINUTES * 6e4; st.n = 0; }
  await env.KV.put("lock:" + email, JSON.stringify(st), { expirationTtl: 86400 });
  return st;
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

function mailLayout(title, body, btnUrl, btnText) {
  return `<!doctype html><html><body style="margin:0;background:#060A12;font-family:Arial,Helvetica,sans-serif;color:#E8EEF8">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#060A12;padding:32px 12px"><tr><td align="center">
  <table width="520" cellpadding="0" cellspacing="0" style="max-width:520px;background:#0C1220;border:1px solid #1B2740;border-radius:16px">
  <tr><td style="padding:28px 32px 8px;font-size:22px;font-weight:bold;color:#26E0B8">Cotescope</td></tr>
  <tr><td style="padding:8px 32px 0;font-size:20px;font-weight:bold;color:#ffffff">${title}</td></tr>
  <tr><td style="padding:14px 32px 0;font-size:15px;line-height:1.6;color:#C9D3E3">${body}</td></tr>
  ${btnUrl ? `<tr><td style="padding:22px 32px 6px"><a href="${btnUrl}" style="display:inline-block;background:#26E0B8;color:#06101A;font-weight:bold;text-decoration:none;padding:13px 22px;border-radius:10px">${btnText}</a></td></tr>
  <tr><td style="padding:8px 32px 0;font-size:12px;color:#7F8BA3">Si le bouton ne marche pas, copie ce lien : ${btnUrl}</td></tr>` : ""}
  <tr><td style="padding:26px 32px 28px;font-size:12px;color:#7F8BA3">Une question ? Réponds simplement à cet e-mail ou écris à ${SUPPORT_EMAIL}.<br><br>Jouer comporte des risques : endettement, isolement, dépendance. Pour être aidé, appelez le 09 74 75 13 13 (appel non surtaxé). Réservé aux 18 ans et plus.</td></tr>
  </table></td></tr></table></body></html>`;
}
async function sendVerify(env, user, origin) {
  const t = rand(24);
  await env.KV.put("verify:" + t, user.email, { expirationTtl: 7 * 86400 });
  return sendMail(env, user.email, "Bienvenue sur Cotescope : confirme ton adresse e-mail",
    mailLayout("Bienvenue sur Cotescope 👋",
      "Ton compte est créé. Il reste une étape : confirmer que cette adresse e-mail est bien la tienne. C'est elle qui te permettra de récupérer ton compte si tu oublies ton mot de passe.<br><br>Le lien est valable 7 jours.",
      `${origin}/api/verify?t=${t}`, "Confirmer mon adresse e-mail"));
}
// Envoi des e-mails : Resend (avec le domaine cotescope.fr) ou, sans toucher au domaine, un relais Gmail
// (petit script Google Apps Script sur le compte Gmail du propriétaire, ~100 e-mails par jour, gratuit).
const mailOn = (env) => !!(env.RESEND_KEY || (env.MAIL_RELAY_URL && env.MAIL_RELAY_KEY));
const SUPPORT_EMAIL = "cotescopeofficial@gmail.com";
async function sendMail(env, to, subject, html, replyTo) {
  if (!env.RESEND_KEY && env.MAIL_RELAY_URL && env.MAIL_RELAY_KEY) {
    try {
      const r = await fetch(env.MAIL_RELAY_URL, { method: "POST", headers: { "Content-Type": "text/plain;charset=utf-8" },
        body: JSON.stringify({ key: env.MAIL_RELAY_KEY, to, subject, html }), redirect: "follow" });
      const t = await r.text();
      return r.ok && t.includes('"ok":true');
    } catch (e) { return false; }
  }
  if (!env.RESEND_KEY) return false;
  const r = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: { Authorization: "Bearer " + env.RESEND_KEY, "Content-Type": "application/json" },
    body: JSON.stringify({ from: env.MAIL_FROM || "Cotescope <compte@cotescope.fr>", to: [to], subject, html, reply_to: replyTo || SUPPORT_EMAIL }),
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
           trial_used: !!(user.memberships || []).length, pause: user.pause && user.pause > Date.now() ? user.pause : null, tg: !!user.tg,
           verified: user.verified !== false };
}
// alerte e-mail quand le compte est ouvert depuis un nouveau pays (si les e-mails sont configurés)
async function newPlaceAlert(env, user, req) {
  const c = (req.cf && req.cf.country) || "";
  if (!c) return;
  const known = user.countries || [];
  if (!known.includes(c)) {
    user.countries = [...known, c].slice(-10);
    if (known.length) {
      await sendMail(env, user.email, "Cotescope : connexion depuis un nouveau pays",
        `<p>Bonjour,</p><p>Ton compte Cotescope vient d'être ouvert depuis un nouveau pays (${c}) sur ${deviceName(req)}.</p>
         <p>Si c'est toi, tout va bien. Sinon, change ton mot de passe tout de suite dans l'appli (Mon compte) : tous les appareils seront déconnectés.</p>`).catch(() => {});
    }
  }
}

/* ---------------- alertes Telegram ---------------- */
const tgToken = (env) => String(env.TELEGRAM_BOT_TOKEN || "").replace(/\s/g, "");
const tgName = (env) => String(env.TELEGRAM_BOT_NAME || "").replace(/[\s@]/g, "");
async function sha256hex(t) { return [...new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(t)))].map((b) => b.toString(16).padStart(2, "0")).join(""); }
async function tgSend(env, chat, text) {
  if (!tgToken(env)) return false;
  try {
    const r = await fetch(`https://api.telegram.org/bot${tgToken(env)}/sendMessage`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chat, text, parse_mode: "HTML", disable_web_page_preview: true }) });
    return r.ok;
  } catch (e) { return false; }
}
// abonnés : { email: { chat, tier } } (une seule clé KV, écrite seulement quand quelqu'un se relie / se délie / change de forfait)
const tgSubs = async (env) => (await env.KV.get("tg_subs", "json")) || {};
async function tgBroadcast(env, minTier, text) {
  const subs = await tgSubs(env), rank = { gratuit: 0, essentiel: 1, pro: 2, elite: 2 }; // alertes = forfait Pro
  let n = 0;
  for (const s of Object.values(subs)) {
    if ((rank[s.tier] || 0) >= rank[minTier] && n < 40) { if (await tgSend(env, s.chat, text)) n++; }
  }
  if (n) { // petit compteur pour l'état visible par le propriétaire (une écriture par envoi groupé)
    const st = (await env.KV.get("tg_stats", "json")) || { sent: 0, msgs: 0 };
    st.sent += n; st.msgs += 1; st.last = Date.now(); st.last_text = String(text).replace(/<[^>]+>/g, "").slice(0, 140);
    await env.KV.put("tg_stats", JSON.stringify(st));
  }
  return n;
}
const escH = (s) => String(s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

// alertes préparées par la collecte horaire (paris value, cotes qui chutent) : envoyées une seule fois aux abonnés Elite
// Surveillance : toutes les 15 min, si la dernière collecte a plus de 3 h ou ne contient presque plus de matchs,
// les propriétaires reçoivent un message Telegram (au plus un toutes les 6 h), puis un message quand tout repart.
async function watchCollect(env) {
  if (new Date().getUTCMinutes() % 15 !== 0) return;
  const h = await env.KV.get("collect_health", "json");
  const st = (await env.KV.get("collect_watch", "json")) || {};
  const now = Date.now();
  const age = h && h.built ? (now - h.built * 1000) / 3600000 : 99;
  let bad = null;
  if (age > 3) bad = `la dernière collecte date de ${age >= 99 ? "?" : Math.round(age * 10) / 10} h`;
  else if ((h.with_odds || 0) < 20) bad = `seulement ${h.with_odds || 0} matchs avec des cotes dans la dernière collecte`;
  let text = null;
  if (bad && (!st.alert || now - st.alert > 6 * 3600000)) {
    text = `⚠️ <b>Cotescope : problème de données</b>\n${escH(bad)}.\nLes clients voient des cotes qui ne se mettent plus à jour. Regarde l'onglet Actions du dépôt betscope.`;
    st.alert = now;
  } else if (!bad && st.alert) {
    text = `✅ <b>Cotescope : données de nouveau à jour</b>\n${h.events} matchs, dont ${h.with_odds} avec des cotes.`;
    st.alert = 0;
  }
  if (!text) return;
  await env.KV.put("collect_watch", JSON.stringify(st));
  for (const o of owners(env)) {
    const ou = await getUser(env, o);
    if (ou && ou.tg) await tgSend(env, ou.tg, text);
  }
}

async function sendQueuedAlerts(env) {
  if (!tgToken(env)) return 0;
  const q = await env.KV.get("alerts_out", "json");
  if (!q || !q.items || !q.items.length) return 0;
  const done = new Set((await env.KV.get("alerts_done", "json")) || []);
  const todo = q.items.filter((a) => !done.has(a.id));
  if (!todo.length) return 0;
  for (const a of todo) { await tgBroadcast(env, "pro", a.text + "\n\nJoue de façon responsable."); done.add(a.id); }
  await env.KV.put("alerts_done", JSON.stringify([...done].slice(-800)));
  return todo.length;
}

/* ---------------- compos officielles en 1 à 2 minutes ---------------- */
// Chaque minute (déclencheur Cloudflare), pour les matchs qui commencent dans l'heure : on interroge ESPN,
// et dès que les compos tombent on les garde (KV « lu:<match> ») avec les changements par rapport au dernier onze.
const ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer/";
function espnRosters(j) {
  const out = [];
  for (const r of j.rosters || []) {
    const xi = [], bench = [];
    for (const p of r.roster || []) {
      const a = p.athlete || {}, n = a.shortName || a.displayName;
      if (!n) continue;
      const it = { n, full: a.displayName || n, num: String(p.jersey || ""), pos: String((p.position || {}).abbreviation || "").toUpperCase(),
                   place: Number(p.formationPlace) || 0 };
      (p.starter ? xi : bench).push(it);
    }
    if (xi.length >= 11) out.push({ team: (r.team || {}).displayName || "", formation: r.formation || "", xi, bench: bench.slice(0, 12), side: r.homeAway });
  }
  return out;
}
const lastName = (s) => String(s || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().trim().split(/\s+/).pop();
async function checkLineups(env) {
  const w = await env.KV.get("lineup_watch", "json");
  if (!w || !w.list) return 0;
  const now = Date.now();
  const due = w.list.filter((m) => { const t = Date.parse(m.start); return t - 80 * 6e4 <= now && now <= t + 10 * 6e4; }).slice(0, 15);
  let n = 0;
  for (const m of due) {
    if (await env.KV.get("lu:" + m.eid)) continue;
    try {
      const r = await fetch(`${ESPN}${m.slug}/summary?event=${encodeURIComponent(m.espn)}`, { cf: { cacheTtl: 30 } });
      if (!r.ok) continue;
      const lu = espnRosters(await r.json());
      if (lu.length < 2) continue;
      for (const l of lu) { // qui entre / qui sort par rapport au onze du dernier match
        const exp = (m.expected || {})[l.side] || [];
        if (exp.length >= 11) {
          const now_ = new Set(l.xi.map((p) => lastName(p.full)));
          const was = new Set(exp.map(lastName));
          l.out = exp.filter((x) => !now_.has(lastName(x)));
          l.in = l.xi.filter((p) => !was.has(lastName(p.full))).map((p) => p.full);
        }
      }
      await env.KV.put("lu:" + m.eid, JSON.stringify({ ts: now, lineups: lu, home: m.home, away: m.away }), { expirationTtl: 2 * 86400 });
      n++;
      const ch = lu.filter((l) => (l.in && l.in.length) || (l.out && l.out.length));
      const lines = ch.map((l) => `<b>${escH(l.team)}</b> : ${l.in.length ? "entrent " + escH(l.in.join(", ")) : ""}${l.in.length && l.out.length ? " · " : ""}${l.out.length ? "sortent " + escH(l.out.join(", ")) : ""}`);
      await tgBroadcast(env, "pro", `📋 <b>Compos officielles</b> : ${escH(m.home)} – ${escH(m.away)}\n` +
        (lines.length ? lines.join("\n") + "\n\nRegarde si les cotes ont bougé avant de parier." : "Pas de changement par rapport au dernier match.") +
        `\n${APP_URL}#/match/${encodeURIComponent(m.eid)}`);
    } catch (e) { /* on réessaiera à la minute suivante */ }
  }
  return n;
}

/* ---------------- vote du public ---------------- */
// Un seul objet « Votes » (stockage SQLite intégré) : un vote par compte et par match, modifiable jusqu'au coup d'envoi.
export class Votes {
  constructor(state) { this.st = state.storage; }
  async fetch(req) {
    const b = await req.json();
    if (b.op === "vote") {
      const key = `v:${b.eid}:${b.email}`, prev = await this.st.get(key);
      const c = (await this.st.get("c:" + b.eid)) || { "1": 0, "N": 0, "2": 0 };
      if (prev && c[prev] > 0) c[prev]--;
      c[b.pick]++;
      await this.st.put({ [key]: b.pick, ["c:" + b.eid]: c });
      return Response.json({ ok: true, counts: c, mine: b.pick });
    }
    if (b.op === "get") {
      const out = {};
      for (const eid of b.eids.slice(0, 120)) {
        const c = await this.st.get("c:" + eid);
        const mine = b.email ? await this.st.get(`v:${eid}:${b.email}`) : null;
        if (c || mine) out[eid] = { counts: c || { "1": 0, "N": 0, "2": 0 }, mine: mine || null };
      }
      return Response.json({ ok: true, votes: out });
    }
    return Response.json({ ok: false }, { status: 400 });
  }
}
const votesStub = (env) => env.VOTES.get(env.VOTES.idFromName("global"));

/* ---------------- routes ---------------- */
export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(Promise.all([checkLineups(env), sendQueuedAlerts(env), watchCollect(env)]));
  },
  async fetch(req, env) {
    const url = new URL(req.url);
    const p = url.pathname;
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: { ...SEC, ...cors(req) } });
    if (!["GET", "POST"].includes(req.method)) return json(req, { ok: false, reason: "method" }, 405);
    if (p === "/api/health") return json(req, { ok: true, time: Date.now() });
    if (p === "/api/telegram/webhook" && req.method === "POST") {
      // seul Telegram connaît ce secret (posé au déploiement) : les fausses requêtes sont refusées
      const want = (await sha256hex(tgToken(env))).slice(0, 48);
      if (!tgToken(env) || !eq(req.headers.get("X-Telegram-Bot-Api-Secret-Token") || "", want)) return new Response("no", { status: 401 });
      let up = {};
      try { up = await req.json(); } catch (e) { /* vide */ }
      const msg = up.message || {}, chat = msg.chat && msg.chat.id, text = String(msg.text || "").trim();
      if (!chat) return new Response("ok");
      const m = text.match(/^\/start\s+([A-Za-z0-9_-]{10,64})$/);
      if (m) {
        const email = await env.KV.get("tglink:" + m[1]);
        const u = email && (await getUser(env, email));
        if (!u) { await tgSend(env, chat, "Ce lien a expiré. Retourne dans Cotescope → Mon compte → Alertes Telegram."); return new Response("ok"); }
        await env.KV.delete("tglink:" + m[1]);
        u.tg = chat; await putUser(env, u);
        const acc = await access(env, u), subs = await tgSubs(env);
        subs[u.email] = { chat, tier: acc.tier || "gratuit" };
        await env.KV.put("tg_subs", JSON.stringify(subs));
        await tgSend(env, chat, `✅ <b>Compte Cotescope relié</b> (${escH(u.email)}).\nTu recevras ici les alertes de ton forfait : compos officielles, paris value et cotes qui chutent (forfait Pro).\nEnvoie /stop pour arrêter.\n\nJoue de façon responsable. Joueurs Info Service : 09 74 75 13 13.`);
      } else if (/^\/stop/.test(text)) {
        const subs = await tgSubs(env);
        for (const [e, s] of Object.entries(subs)) if (s.chat === chat) { delete subs[e]; const u = await getUser(env, e); if (u) { delete u.tg; await putUser(env, u); } }
        await env.KV.put("tg_subs", JSON.stringify(subs));
        await tgSend(env, chat, "🔕 Alertes arrêtées. Tu peux les réactiver depuis Cotescope → Mon compte.");
      } else {
        await tgSend(env, chat, "Pour recevoir les alertes, relie ton compte depuis Cotescope → Mon compte → Alertes Telegram.");
      }
      return new Response("ok");
    }
    if (p === "/api/public/stats") { // bilan public des pronostics (sans connexion)
      const text = await kvJson(env, "public_stats");
      return new Response(text || '{"n":0}', { headers: { "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "public, max-age=600", ...SEC, ...cors(req) } });
    }
    if (!env.SESSION_SECRET && p !== "/api/whop/webhook") return json(req, { ok: false, reason: "server_not_configured" }, 503);
    let body = {};
    if (req.method === "POST" && p !== "/api/whop/webhook") {
      // les actions ne sont acceptées que depuis le site Cotescope
      const o = req.headers.get("Origin");
      if (o && !ALLOWED_ORIGINS.includes(o)) return json(req, { ok: false, reason: "forbidden" }, 403);
      if (Number(req.headers.get("Content-Length") || 0) > MAX_BODY) return json(req, { ok: false, reason: "too_big" }, 413);
      const raw = await req.text();
      if (raw.length > MAX_BODY) return json(req, { ok: false, reason: "too_big" }, 413);
      try { body = JSON.parse(raw || "{}"); } catch (e) { /* vide */ }
      if (typeof body !== "object" || body === null || Array.isArray(body)) body = {};
    }

    // --- comptes ---
    if (p === "/api/signup" && req.method === "POST") {
      const email = cleanEmail(body.email), pw = String(body.password || "");
      if (!email) return json(req, { ok: false, reason: "bad_email" }, 400);
      if (await tooMany(req, "signup")) return json(req, { ok: false, reason: "too_many" }, 429);
      const weak = await weakReason(email, pw);
      if (weak) return json(req, { ok: false, reason: weak }, 400);
      if (await getUser(env, email)) return json(req, { ok: false, reason: "email_taken" }, 409);
      if (await freeQuota(req, false)) return json(req, { ok: false, reason: "too_many_accounts" }, 429);
      const user = { email, name: String(body.name || "").slice(0, 60), pv: 1, created: Date.now(), memberships: [],
                     countries: req.cf && req.cf.country ? [req.cf.country] : [], verified: false };
      await setPassword(env, user, pw, false);
      await freeQuota(req, true);
      try { await sendVerify(env, user, url.origin); } catch (e) { /* l'inscription marche même si l'envoi échoue */ }
      return json(req, { ...pub(user, await access(env, user)), session: await makeSession(env, user, req) });
    }
    if (p === "/api/login" && req.method === "POST") {
      const email = cleanEmail(body.email), pw = String(body.password || "");
      if (pw.length > 200) return json(req, { ok: false, reason: "bad_login" }, 401);
      if (await tooMany(req, "login:" + email)) return json(req, { ok: false, reason: "too_many" }, 429);
      const lk = email ? await lockState(env, email) : { n: 0, until: 0 };
      if (email) {
        if (lk.until > Date.now()) return json(req, { ok: false, reason: "locked", minutes: Math.ceil((lk.until - Date.now()) / 6e4) }, 429);
      }
      const user = email && (await getUser(env, email));
      if (!user) { await burn(pw); return json(req, { ok: false, reason: "bad_login" }, 401); }
      if (!(await checkPassword(env, user, pw))) {
        const st = await failLogin(env, email);
        if (st.until > Date.now()) return json(req, { ok: false, reason: "locked", minutes: LOCK_MINUTES }, 429);
        return json(req, { ok: false, reason: "bad_login" }, 401);
      }
      if (lk.n || lk.until) await env.KV.delete("lock:" + email);
      await newPlaceAlert(env, user, req);
      return json(req, { ...pub(user, await access(env, user)), session: await makeSession(env, user, req) });
    }
    // assistant d'aide : message transmis au propriétaire (Telegram + e-mail si configuré)
    if (p === "/api/support" && req.method === "POST") {
      if (await tooMany(req, "support")) return json(req, { ok: false, reason: "too_many" }, 429);
      const from = cleanEmail(body.email), msg = String(body.message || "").trim().slice(0, 1500), page = String(body.page || "").slice(0, 120);
      if (!from || msg.length < 3) return json(req, { ok: false, reason: "bad_request" }, 400);
      const item = { ts: Date.now(), from, msg, page, country: (req.cf && req.cf.country) || "" };
      const list = (await env.KV.get("support", "json")) || [];
      list.unshift(item);
      await env.KV.put("support", JSON.stringify(list.slice(0, 200)));
      const text = `🆘 <b>Message d'aide Cotescope</b>\nDe : ${escH(from)}${item.country ? " (" + escH(item.country) + ")" : ""}\nPage : ${escH(page)}\n\n${escH(msg)}`;
      let sent = 0;
      if (mailOn(env)) await sendMail(env, SUPPORT_EMAIL, "Cotescope : message d'aide de " + from, mailLayout("Nouveau message d'aide",
        `<b>De :</b> ${escH(from)}<br><b>Page :</b> ${escH(page)}<br><br>${escH(msg).replace(/\n/g, "<br>")}<br><br>Clique sur « Répondre » pour lui écrire directement.`), from);
      for (const o of owners(env)) {
        const ou = await getUser(env, o);
        if (ou && ou.tg && (await tgSend(env, ou.tg, text))) sent++;
        if (mailOn(env) && o !== SUPPORT_EMAIL) await sendMail(env, o, "Cotescope : message d'aide de " + from, mailLayout("Nouveau message d'aide",
          `<b>De :</b> ${escH(from)}<br><b>Page :</b> ${escH(page)}<br><br>${escH(msg).replace(/\n/g, "<br>")}`));
      }
      return json(req, { ok: true, delivered: sent });
    }
    // confirmation de l'adresse e-mail (lien reçu par e-mail)
    if (p === "/api/verify" && req.method === "GET") {
      const t = String(url.searchParams.get("t") || "").slice(0, 64);
      const email = /^[A-Za-z0-9_-]{20,64}$/.test(t) && (await env.KV.get("verify:" + t));
      const u = email && (await getUser(env, email));
      if (!u) return Response.redirect(APP_URL + "#verified=0", 302);
      u.verified = Date.now();
      await putUser(env, u);
      await env.KV.delete("verify:" + t);
      return Response.redirect(APP_URL + "#verified=1", 302);
    }
    if (p === "/api/forgot" && req.method === "POST") {
      const email = cleanEmail(body.email);
      if (await tooMany(req, "forgot")) return json(req, { ok: false, reason: "too_many" }, 429);
      const user = email && (await getUser(env, email));
      if (!mailOn(env)) return json(req, { ok: false, reason: "mail_not_configured" }, 503);
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
      const code = String(body.code || "").slice(0, 64), pw = String(body.password || "");
      if (await tooMany(req, "reset")) return json(req, { ok: false, reason: "too_many" }, 429);
      const email = /^[A-Za-z0-9_-]{20,64}$/.test(code) && (await env.KV.get("reset:" + code));
      const user = email && (await getUser(env, email));
      if (!user) return json(req, { ok: false, reason: "bad_code" }, 400);
      const weak = await weakReason(email, pw);
      if (weak) return json(req, { ok: false, reason: weak }, 400);
      await env.KV.delete("reset:" + code); // lien à usage unique
      await env.KV.delete("lock:" + email);
      await setPassword(env, user, pw);
      return json(req, { ...pub(user, await access(env, user)), session: await makeSession(env, user, req) });
    }
    if (p === "/api/password" && req.method === "POST") {
      const user = await readSession(env, req);
      if (!user) return json(req, { ok: false, reason: "session" }, 401);
      if (await tooMany(req, "password:" + user.email)) return json(req, { ok: false, reason: "too_many" }, 429);
      if (!(await checkPassword(env, user, String(body.old || "")))) return json(req, { ok: false, reason: "bad_login" }, 401);
      const weak = await weakReason(user.email, String(body.password || ""));
      if (weak) return json(req, { ok: false, reason: weak }, 400);
      await setPassword(env, user, String(body.password));
      return json(req, { ok: true, session: await makeSession(env, user, req) });
    }

    // --- compte connecté ---
    const user = await readSession(env, req);
    if (p === "/api/verify/resend" && req.method === "POST") {
      if (!user) return json(req, { ok: false, reason: "session" }, 401);
      if (user.verified !== false) return json(req, { ok: true, already: true });
      if (await tooMany(req, "verify:" + user.email)) return json(req, { ok: false, reason: "too_many" }, 429);
      if (!mailOn(env)) return json(req, { ok: false, reason: "mail_not_configured" }, 503);
      const ok = await sendVerify(env, user, url.origin);
      return json(req, { ok });
    }
    if (p === "/api/me") {
      if (!user) return json(req, { ok: false, reason: "session" }, 401);
      const acc = await access(env, user, url.searchParams.has("fresh"));
      if (user.tg) { // garde le forfait des abonnés Telegram à jour (pour savoir quelles alertes leur envoyer)
        const subs = await tgSubs(env), cur = subs[user.email];
        if (!cur || cur.tier !== (acc.tier || "gratuit")) { subs[user.email] = { chat: user.tg, tier: acc.tier || "gratuit" }; await env.KV.put("tg_subs", JSON.stringify(subs)); }
      }
      return json(req, { ...pub(user, acc), tg_bot: tgName(env) || null });
    }
    if (p === "/api/telegram/link" && req.method === "POST") {
      if (!user) return json(req, { ok: false, reason: "session" }, 401);
      if (!tgToken(env) || !tgName(env)) return json(req, { ok: false, reason: "telegram_off" }, 503);
      const code = rand(18);
      await env.KV.put("tglink:" + code, user.email, { expirationTtl: 3600 });
      return json(req, { ok: true, url: `https://t.me/${tgName(env)}?start=${code}` });
    }
    if (p === "/api/telegram/unlink" && req.method === "POST") {
      if (!user) return json(req, { ok: false, reason: "session" }, 401);
      const subs = await tgSubs(env);
      if (subs[user.email]) { await tgSend(env, subs[user.email].chat, "🔕 Alertes Cotescope arrêtées depuis ton compte."); delete subs[user.email]; await env.KV.put("tg_subs", JSON.stringify(subs)); }
      delete user.tg; await putUser(env, user);
      return json(req, { ok: true });
    }
    // alerte de test : envoie un message sur le Telegram relié du compte connecté
    if (p === "/api/telegram/test" && req.method === "POST") {
      if (!user) return json(req, { ok: false, reason: "session" }, 401);
      if (!user.tg) return json(req, { ok: false, reason: "not_linked" }, 400);
      const ok = await tgSend(env, user.tg, "✅ <b>Test Cotescope</b> : les alertes arrivent bien ici. Tu recevras les paris value, les grosses chutes de cotes et les compos officielles (forfait Pro).");
      return json(req, { ok });
    }
    // état des alertes et des collectes, réservé au propriétaire
    if (p === "/api/admin/status") {
      if (!user || !owners(env).includes(user.email)) return json(req, { ok: false, reason: "forbidden" }, 403);
      const subs = await tgSubs(env), q = (await env.KV.get("alerts_out", "json")) || {}, w = (await env.KV.get("lineup_watch", "json")) || {};
      const lus = await env.KV.list({ prefix: "lu:", limit: 200 });
      const byTier = {};
      for (const x of Object.values(subs)) byTier[x.tier || "gratuit"] = (byTier[x.tier || "gratuit"] || 0) + 1;
      let bot = null;
      if (tgToken(env)) {
        try { const r = await fetch(`https://api.telegram.org/bot${tgToken(env)}/getWebhookInfo`); const j = await r.json();
              bot = { ok: !!j.ok, webhook: !!(j.result && j.result.url), pending: (j.result || {}).pending_update_count || 0, error: (j.result || {}).last_error_message || null }; } catch (e) { bot = { ok: false }; }
      }
      return json(req, { ok: true, mail: { configured: mailOn(env), via: env.RESEND_KEY ? "resend" : (mailOn(env) ? "gmail" : null) }, telegram: { configured: !!tgToken(env), bot, subscribers: Object.keys(subs).length, by_tier: byTier,
        stats: (await env.KV.get("tg_stats", "json")) || { sent: 0, msgs: 0 }, queued: (q.items || []).length, queue_ts: q.built ? q.built * 1000 : null },
        lineups: { watched: (w.list || []).length, stored: lus.keys.length },
        support: ((await env.KV.get("support", "json")) || []).slice(0, 10) });
    }
    // pause volontaire (jeu responsable) : 1, 7 ou 30 jours, impossible à raccourcir une fois lancée
    if (p === "/api/pause" && req.method === "POST") {
      if (!user) return json(req, { ok: false, reason: "session" }, 401);
      const days = [1, 7, 30].includes(Number(body.days)) ? Number(body.days) : 0;
      if (!days) return json(req, { ok: false, reason: "bad_days" }, 400);
      user.pause = Math.max(user.pause || 0, Date.now() + days * 864e5);
      await putUser(env, user);
      return json(req, { ok: true, pause: user.pause });
    }
    // appareils connectés : voir, déconnecter un appareil, ou tous
    if (p === "/api/devices") {
      if (!user) return json(req, { ok: false, reason: "session" }, 401);
      return json(req, { ok: true, max: MAX_DEVICES, devices: (user.sids || []).map((d) => ({ id: d.id, since: d.t, name: d.d || "Appareil", country: d.c || "", me: d.id === user._sid })).reverse() });
    }
    if (p === "/api/logout" && req.method === "POST") {
      if (!user) return json(req, { ok: true });
      const target = body.all ? null : String(body.id || user._sid);
      user.sids = body.all ? [] : (user.sids || []).filter((d) => d.id !== target);
      if (body.all) user.pv = (user.pv || 1) + 1; // les anciennes sessions sans appareil sont coupées aussi
      await putUser(env, user);
      return json(req, { ok: true });
    }
    if (p === "/api/checkout" && req.method === "POST") {
      if (!user) return json(req, { ok: false, reason: "session" }, 401);
      const tier = ["essentiel", "pro"].includes(body.tier) ? body.tier : "";
      const plan = plans(env)[tier];
      if (!plan || !env.WHOP_API_KEY) return json(req, { ok: false, reason: "plans_not_configured" }, 503);
      const ret = ALLOWED_ORIGINS.some((o) => String(body.return || "").startsWith(o + "/")) ? body.return : APP_URL;
      let r;
      const base = { metadata: { cotescope_account: user.email }, redirect_url: ret };
      try {
        try { // versions récentes de l'API Whop : variant_id ; anciennes : plan_id
          r = await whop(env, "/checkout_configurations", { method: "POST", body: JSON.stringify({ ...base, variant_id: plan }) });
        } catch (e) {
          if (e.status !== 400) throw e;
          r = await whop(env, "/checkout_configurations", { method: "POST", body: JSON.stringify({ ...base, plan_id: plan }) });
        }
      } catch (e) {
        return json(req, { ok: false, reason: e.status ? "whop_error" : "whop_unreachable", status: e.status || 0,
                           detail: String(e.detail || e.message || e).slice(0, 200) }, 502);
      }
      user.pending = Date.now();
      await putUser(env, user);
      return json(req, { ok: true, url: r.purchase_url });
    }
    // base historique du simulateur de stratégies (Elite)
    if (p === "/api/history") {
      if (!user) return json(req, { ok: false, reason: "session" }, 401);
      const acc = await access(env, user);
      if (!["pro", "elite"].includes(acc.tier)) return json(req, { ok: false, reason: "locked", need: "pro" }, 403);
      // le fichier est déjà compressé : envoyé tel quel (aucun calcul côté serveur, le navigateur le décompresse)
      const buf = await env.KV.get("bt_data", "arrayBuffer");
      if (!buf) return json(req, { ok: false, reason: "no_data_yet" }, 503);
      return new Response(buf, { encodeBody: "manual", headers: { "Content-Type": "application/json; charset=utf-8",
        "Content-Encoding": "gzip", "Cache-Control": "private, max-age=3600", ...SEC, ...cors(req) } });
    }
    // vote du public : ouvert à tous les comptes (gratuits compris), seulement avant le coup d'envoi
    if (p === "/api/vote" && req.method === "POST") {
      if (!user) return json(req, { ok: false, reason: "session" }, 401);
      const eid = String(body.eid || "").slice(0, 80), pick = String(body.pick || "");
      if (!eid || !["1", "N", "2"].includes(pick)) return json(req, { ok: false, reason: "bad_vote" }, 400);
      const w = await env.KV.get("lineup_watch", "json"), m = w && (w.list || []).find((x) => x.eid === eid);
      if (!m) return json(req, { ok: false, reason: "vote_closed" }, 400); // vote ouvert dans les 36 h avant le match
      if (Date.parse(m.start) <= Date.now()) return json(req, { ok: false, reason: "vote_closed" }, 400);
      if (await tooMany(req, "vote:" + user.email + ":" + eid)) return json(req, { ok: false, reason: "too_many" }, 429);
      const r = await votesStub(env).fetch("https://votes/", { method: "POST", body: JSON.stringify({ op: "vote", eid, pick, email: user.email }) });
      return json(req, await r.json());
    }
    if (p === "/api/votes") {
      if (!user) return json(req, { ok: false, reason: "session" }, 401);
      const eids = String(url.searchParams.get("eids") || "").split(",").map((x) => x.trim().slice(0, 80)).filter(Boolean);
      const r = await votesStub(env).fetch("https://votes/", { method: "POST", body: JSON.stringify({ op: "get", eids, email: user.email }) });
      return json(req, await r.json());
    }
    // compos officielles fraîches d'un match (Pro et Elite)
    if (p === "/api/lineup") {
      if (!user) return json(req, { ok: false, reason: "session" }, 401);
      const acc = await access(env, user);
      if (!["essentiel", "pro", "elite"].includes(acc.tier)) return json(req, { ok: false, reason: "locked", need: "essentiel" }, 403);
      const v = await env.KV.get("lu:" + String(url.searchParams.get("eid") || "").slice(0, 80), "json");
      return json(req, v ? { ok: true, ...v } : { ok: false, reason: "not_yet" });
    }
    if (p === "/api/snapshot" || p === "/api/engine") {
      if (!user) return json(req, { ok: false, reason: "session" }, 401);
      const acc = await access(env, user);
      if (!acc.tier) return json(req, { ok: false, reason: ["whop_unreachable", "free_over"].includes(acc.status) ? acc.status : "no_plan" }, 403);
      const text = await kvJson(env, p === "/api/engine" ? "engine" : "snapshot_" + acc.tier);
      if (!text) return json(req, { ok: false, reason: "no_data_yet" }, 503);
      return new Response(text, {
        headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "private, no-store",
                   "X-Cotescope-Tier": acc.tier, ...SEC, ...cors(req) },
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
