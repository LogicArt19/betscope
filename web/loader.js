/* Cotescope – appli web.
   Même moteur d'analyse que le logiciel (Python), exécuté dans le navigateur avec Pyodide.
   Le moteur et les données ne sont envoyés par le serveur qu'aux abonnés (clé de licence). */
(function () {
  "use strict";
  const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/";
  const LS = "cotescope_license";
  const SNAP_EVERY = 10 * 60 * 1000;
  const MSG = {
    invalid: "Clé invalide.", unknown_key: "Clé inconnue. Vérifie que tu l'as bien copiée.",
    inactive: "Abonnement expiré ou annulé.", whop_unreachable: "Serveur de paiement injoignable, réessaie dans un instant.",
    server_not_configured: "Le serveur n'est pas encore prêt.", no_data_yet: "Les premières données arrivent, réessaie dans quelques minutes.",
    network: "Pas de connexion au serveur Cotescope.",
  };
  const msg = r => MSG[r] || ("Erreur : " + r);
  const $ = s => document.querySelector(s);
  let CFG = {}, KEY = "", py = null, web = null, nextSnap = 0, saveT = null;

  function lsGet() { try { return localStorage.getItem(LS) || ""; } catch (e) { return ""; } }
  function lsSet(v) { try { v ? localStorage.setItem(LS, v) : localStorage.removeItem(LS); } catch (e) { /* navigation privée */ } }

  async function server(path, method, body) {
    if (!CFG.api) throw new Error("server_not_configured");
    let r;
    try {
      r = await fetch(CFG.api.replace(/\/$/, "") + path, {
        method: method || "GET",
        headers: Object.assign({ Authorization: "Bearer " + KEY }, body ? { "Content-Type": "application/json" } : {}),
        body: body ? JSON.stringify(body) : undefined, cache: "no-store",
      });
    } catch (e) { throw new Error("network"); }
    if (r.status === 403 || r.status === 503) {
      let j = {}; try { j = await r.json(); } catch (e) { /* vide */ }
      const err = new Error(j.reason || "inactive"); err.license = r.status === 403; throw err;
    }
    if (!r.ok) throw new Error("http_" + r.status);
    return r;
  }

  /* ---------- écran d'accès ---------- */
  function gate(state, text) {
    const g = $("#cs-gate");
    g.style.display = "grid";
    $("#cs-form").style.display = state === "form" ? "" : "none";
    $("#cs-load").style.display = state === "load" ? "" : "none";
    if (state === "load") $("#cs-step").textContent = text || "";
    $("#cs-err").textContent = state === "form" ? (text || "") : "";
    if (state === "form") setTimeout(() => $("#cs-key").focus(), 50);
  }
  function hideGate() { $("#cs-gate").style.display = "none"; }

  async function tryKey(k) {
    KEY = String(k || "").trim();
    if (!KEY) return gate("form", "Colle ta clé de licence.");
    gate("load", "Vérification de ta licence…");
    try {
      await server("/api/check", "POST", { license: KEY });
      lsSet(KEY);
      await boot();
    } catch (e) {
      if (e.license) lsSet("");
      gate("form", msg(e.message));
    }
  }

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
    const r = await server("/api/snapshot");
    web.load_snapshot(await r.text());
    nextSnap = Date.now() + SNAP_EVERY;
  }

  async function boot() {
    gate("load", "Téléchargement des données du jour…");
    const [eng, snap] = await Promise.all([
      server("/api/engine").then(r => r.json()),
      server("/api/snapshot").then(r => r.text()),
      window.loadPyodide ? null : loadScript(PYODIDE + "pyodide.js"),
    ]);
    gate("load", "Démarrage du moteur d'analyse…");
    if (!py) {
      py = await window.loadPyodide({ indexURL: PYODIDE });
      py.FS.mkdirTree("/data");
      py.FS.mount(py.FS.filesystems.IDBFS, {}, "/data");
      await new Promise(ok => py.FS.syncfs(true, () => ok()));
      py.FS.mkdirTree("/engine");
    }
    for (const [name, code] of Object.entries(eng.files || {})) py.FS.writeFile("/engine/" + name, code);
    py.runPython("import sys\nif '/engine' not in sys.path: sys.path.insert(0, '/engine')\nimport webapi\nwebapi.boot()");
    web = py.pyimport("webapi");
    call("activate", [KEY]);
    gate("load", "Analyse de tous les matchs…");
    await new Promise(r => setTimeout(r, 30));
    web.load_snapshot(snap);
    nextSnap = Date.now() + SNAP_EVERY;
    persist();
    hideGate();
    window.dispatchEvent(new CustomEvent("cotescope-api-ready", { detail: API }));
    setInterval(tick, 60 * 1000);
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
    } catch (e) {
      if (e.license) { lsSet(""); location.reload(); }
    }
  }

  /* ---------- objet « api » utilisé par l'interface ---------- */
  const SPECIAL = {
    async activate(k) {
      const old = KEY; KEY = String(k || "").trim();
      try { await server("/api/check", "POST", { license: KEY }); }
      catch (e) { KEY = old; return { ok: false, reason: e.message }; }
      lsSet(KEY); call("activate", [KEY]); await loadSnapshot();
      return { ok: true };
    },
    async deactivate() { lsSet(""); call("deactivate", []); await new Promise(r => setTimeout(r, 300)); location.reload(); return true; },
    async refresh() { try { await loadSnapshot(); return true; } catch (e) { return false; } },
    async open_url(u) { if (/^https:\/\//.test(u)) window.open(u, "_blank", "noopener"); return true; },
    async get_status() {
      const s = call("get_status", []);
      s.next_in = Math.max(0, Math.round((nextSnap - Date.now()) / 1000));
      s.subscribe_url = CFG.whop || "";
      s.frozen = false;
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
    window.__COTESCOPE_CFG__ = CFG;
    $("#cs-trial").onclick = () => CFG.whop ? window.open(CFG.whop, "_blank", "noopener") : ($("#cs-err").textContent = "Les inscriptions ouvrent très bientôt.");
    $("#cs-go").onclick = () => tryKey($("#cs-key").value);
    $("#cs-key").onkeydown = e => { if (e.key === "Enter") $("#cs-go").click(); };
    const fromUrl = new URLSearchParams(location.hash.slice(1)).get("key");
    if (fromUrl) history.replaceState(null, "", location.pathname);
    const k = fromUrl || lsGet();
    if (k) tryKey(k); else gate("form");
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
