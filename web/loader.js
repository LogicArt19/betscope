/* Cotescope – appli web.
   Même moteur d'analyse que le logiciel (Python), exécuté dans le navigateur avec Pyodide.
   Le moteur et les données ne sont envoyés par le serveur qu'aux comptes abonnés,
   et chaque forfait ne reçoit que les données qu'il a payées. */
(function () {
  "use strict";
  const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/";
  const LS = "cotescope_session";
  const SNAP_EVERY = 10 * 60 * 1000;
  const $ = s => document.querySelector(s);
  let CFG = {}, SESSION = "", ACCOUNT = null, py = null, web = null, nextSnap = 0, saveT = null, booted = false;

  function lsGet() { try { return localStorage.getItem(LS) || ""; } catch (e) { return ""; } }
  function lsSet(v) { try { v ? localStorage.setItem(LS, v) : localStorage.removeItem(LS); } catch (e) { /* navigation privée */ } }

  async function server(path, method, body, raw) {
    if (!CFG.api) return { ok: false, reason: "server_not_configured" };
    let r;
    try {
      r = await fetch(CFG.api.replace(/\/$/, "") + path, {
        method: method || "GET", cache: "no-store",
        headers: Object.assign(SESSION ? { Authorization: "Bearer " + SESSION } : {}, body ? { "Content-Type": "application/json" } : {}),
        body: body ? JSON.stringify(body) : undefined,
      });
    } catch (e) { return { ok: false, reason: "network" }; }
    if (raw && r.ok) return r;
    let j = {};
    try { j = await r.json(); } catch (e) { j = { ok: false, reason: "http_" + r.status }; }
    if (!r.ok && j.ok === undefined) j.ok = false;
    if (j.reason === "session") { lsSet(""); SESSION = ""; }
    return j;
  }
  function keep(r) {
    if (r && r.session) { SESSION = r.session; lsSet(SESSION); }
    if (r && r.ok && r.email) ACCOUNT = { email: r.email, name: r.name, tier: r.tier, status: r.status, renews: r.renews, cancel: r.cancel, manage: r.manage };
    return r;
  }

  /* ---------- écran de chargement ---------- */
  function loading(text, error) {
    const g = $("#cs-load"); g.style.display = text ? "grid" : "none";
    if (!text) return;
    $("#cs-step").textContent = text;
    $("#cs-retry").style.display = error ? "" : "none";
    $("#cs-hint").style.display = error ? "none" : "";
    $("#cs-spin").style.display = error ? "none" : "";
  }

  /* ---------- compte : l'interface de Cotescope s'occupe des écrans ---------- */
  const AUTH = {
    canSkip: false,
    login: async (e, p) => keep(await server("/api/login", "POST", { email: e, password: p })),
    signup: async (e, p) => keep(await server("/api/signup", "POST", { email: e, password: p })),
    forgot: e => server("/api/forgot", "POST", { email: e }),
    reset: async (code, p) => { const r = keep(await server("/api/reset", "POST", { code, password: p })); if (r.ok) history.replaceState(null, "", location.pathname); return r; },
    account: async fresh => keep(await server("/api/me" + (fresh ? "?fresh=1" : ""))),
    checkout: async tier => {
      const r = await server("/api/checkout", "POST", { tier, return: location.origin + location.pathname + "#paid" });
      if (r.ok && r.url) { location.href = r.url; r.redirect = true; }
      return r;
    },
    logout: async () => { lsSet(""); SESSION = ""; ACCOUNT = null; try { call("logout", []); } catch (e) { /* pas encore démarré */ } location.reload(); },
    done: async r => {
      window.CotescopeAuth.close();
      if (!booted) await start();
      else { call("set_account", [ACCOUNT]); await loadSnapshot(); }
    },
  };

  /* ---------- moteur ---------- */
  function loadScript(src) {
    return new Promise((ok, ko) => {
      const s = document.createElement("script");
      s.src = src; s.crossOrigin = "anonymous"; s.onload = ok; s.onerror = () => ko(new Error("network"));
      document.head.appendChild(s);
    });
  }
  function persist() {
    clearTimeout(saveT);
    saveT = setTimeout(() => { try { py.FS.syncfs(false, () => {}); } catch (e) { /* ignore */ } }, 400);
  }
  async function loadSnapshot() {
    const r = await server("/api/snapshot", "GET", null, true);
    if (!(r instanceof Response)) throw Object.assign(new Error(r.reason || "network"), { reason: r.reason });
    web.load_snapshot(await r.text());
    nextSnap = Date.now() + SNAP_EVERY;
  }

  async function start() {
    try {
      loading("Téléchargement des données du jour…");
      const [eng, snap] = await Promise.all([
        server("/api/engine", "GET", null, true),
        server("/api/snapshot", "GET", null, true),
        window.loadPyodide ? null : loadScript(PYODIDE + "pyodide.js"),
      ]);
      for (const r of [eng, snap]) if (!(r instanceof Response)) throw Object.assign(new Error(r.reason), { reason: r.reason });
      const files = (await eng.json()).files || {}, snapText = await snap.text();
      loading("Démarrage du moteur d'analyse…");
      if (!py) {
        py = await window.loadPyodide({ indexURL: PYODIDE });
        py.FS.mkdirTree("/data");
        py.FS.mount(py.FS.filesystems.IDBFS, {}, "/data");
        await new Promise(ok => py.FS.syncfs(true, () => ok()));
        py.FS.mkdirTree("/engine");
      }
      for (const [name, code] of Object.entries(files)) py.FS.writeFile("/engine/" + name, code);
      py.runPython("import sys\nif '/engine' not in sys.path: sys.path.insert(0, '/engine')\nimport webapi\nwebapi.boot()");
      web = py.pyimport("webapi");
      call("set_account", [ACCOUNT]);
      loading("Analyse de tous les matchs…");
      await new Promise(r => setTimeout(r, 30));
      web.load_snapshot(snapText);
      nextSnap = Date.now() + SNAP_EVERY;
      persist();
      loading("");
      booted = true;
      window.dispatchEvent(new CustomEvent("cotescope-api-ready", { detail: API }));
      setInterval(tick, 60 * 1000);
      setTimeout(() => window.CotescopeAuth.books(), 1500);
    } catch (e) {
      loading("");
      const r = e.reason || e.message;
      if (r === "no_plan" || r === "inactive" || r === "free_over") window.CotescopeAuth.open("plans");
      else if (r === "session") window.CotescopeAuth.open("login");
      else loading(({ no_data_yet: "Les données du jour arrivent, réessaie dans quelques minutes.", network: "Pas de connexion au serveur Cotescope.", whop_unreachable: "Le service de paiement ne répond pas, réessaie dans un instant." })[r] || ("Erreur : " + r), true);
    }
  }

  const READ_ONLY = /^(get_|builder_get|estimate|compute_)/;
  function call(name, args) {
    const out = JSON.parse(web.call(name, JSON.stringify(args || [])));
    if (!READ_ONLY.test(name)) persist();
    return out;
  }
  async function tick() {
    try {
      if (Date.now() >= nextSnap) await loadSnapshot();
      call("tick", []);
      if (Math.random() < 0.1) { const a = await AUTH.account(); if (a && a.ok) call("set_account", [ACCOUNT]); else if (a && a.reason === "session") location.reload(); }
    } catch (e) {
      if (["session", "no_plan", "inactive", "free_over"].includes(e.reason)) location.reload();
    }
  }

  /* ---------- objet « api » utilisé par l'interface ---------- */
  const SPECIAL = {
    login: AUTH.login, signup: AUTH.signup, forgot: AUTH.forgot, account: AUTH.account,
    checkout: AUTH.checkout, logout: AUTH.logout,
    async refresh() { try { await loadSnapshot(); return true; } catch (e) { return false; } },
    async open_url(u) { if (/^https:\/\//.test(u)) window.open(u, "_blank", "noopener"); return true; },
    async get_status() {
      const s = call("get_status", []);
      s.next_in = Math.max(0, Math.round((nextSnap - Date.now()) / 1000));
      s.license = true; s.frozen = false;
      return s;
    },
    async check_updates() { return { update: null, version: (call("get_status", []) || {}).version }; },
    async install_update() { return false; },
  };
  const API = new Proxy({}, {
    get(_, name) {
      if (typeof name !== "string") return undefined;
      if (SPECIAL[name]) return SPECIAL[name];
      return async (...args) => call(name, args);
    },
  });

  /* ---------- démarrage ---------- */
  async function init() {
    try { CFG = await (await fetch("../config.json", { cache: "no-store" })).json(); } catch (e) { CFG = {}; }
    window.CotescopeAuth.set(AUTH);
    const h = new URLSearchParams(location.hash.slice(1));
    if (h.get("reset")) { window.CotescopeAuth.open("reset", { code: h.get("reset") }); return; }
    const paid = location.hash === "#paid";
    if (paid) history.replaceState(null, "", location.pathname);
    SESSION = lsGet();
    if (!SESSION) { window.CotescopeAuth.open(h.has("signup") ? "signup" : "login"); return; }
    loading("Connexion à ton compte…");
    const me = await AUTH.account(true);
    loading("");
    if (!me.ok) { window.CotescopeAuth.open("login"); return; }
    if (!me.tier) { window.CotescopeAuth.open(paid ? "wait" : "plans"); return; }
    await start();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
