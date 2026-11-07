# -*- coding: utf-8 -*-
"""Assemble l'appli web (app/index.html) à partir de l'interface du logiciel + le chargeur web.
Usage : python ci/build_web.py <dossier_sortie>"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "dist_web")

ui = open(os.path.join(ROOT, "ui", "index.html"), encoding="utf-8").read()
loader = open(os.path.join(ROOT, "web", "loader.js"), encoding="utf-8").read()

CSP = ("default-src 'self'; "
       "script-src 'self' 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval' https://cdn.jsdelivr.net; "
       "connect-src 'self' https://cdn.jsdelivr.net https://*.workers.dev https://api.cotescope.fr; "
       "style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; font-src 'self' data:; "
       "worker-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")

HEAD = f"""<meta http-equiv="Content-Security-Policy" content="{CSP}">
<meta name="referrer" content="no-referrer">
<meta name="robots" content="noindex">
<link rel="icon" href="../img/logo.svg" type="image/svg+xml">
<script>window.__WEB__ = true;</script>
"""

GATE = """
<div id="cs-gate" style="position:fixed;inset:0;z-index:100;display:none;place-items:center;
  background:radial-gradient(1200px 600px at 50% -10%,rgba(34,197,94,.18),transparent 60%),#070b10;color:#e8eef5;
  font-family:inherit">
  <div style="width:min(440px,92vw);padding:30px;border:1px solid rgba(255,255,255,.08);border-radius:18px;
    background:rgba(15,21,29,.85);box-shadow:0 30px 80px rgba(0,0,0,.5);text-align:center">
    <img src="../img/logo.svg" alt="" style="width:64px;height:64px;margin:0 auto 10px;display:block" onerror="this.remove()">
    <div style="font-size:24px;font-weight:800;letter-spacing:-.02em">Cotescope</div>
    <div id="cs-form">
      <p style="color:#9fb0c2;font-size:14px;margin:8px 0 18px">L'analyse complète de tous les matchs Betclic, dans ton navigateur.<br>
        <b style="color:#e8eef5">7 jours gratuits</b>, puis 10 €/mois, sans engagement.</p>
      <button id="cs-trial" class="btn primary" style="width:100%;justify-content:center;padding:13px">Démarrer l'essai gratuit</button>
      <div style="color:#6b7c8f;font-size:12px;margin:16px 0 8px">J'ai déjà ma clé de licence</div>
      <div style="display:flex;gap:8px"><input id="cs-key" class="in" placeholder="Colle ta clé ici" autocomplete="off" spellcheck="false">
        <button id="cs-go" class="btn">Entrer</button></div>
      <div id="cs-err" style="color:#fca5a5;font-size:13px;min-height:18px;margin-top:8px"></div>
      <a href="../" style="color:#6b7c8f;font-size:12.5px">← Retour au site</a>
    </div>
    <div id="cs-load" style="display:none;padding:18px 0 6px">
      <div style="width:42px;height:42px;margin:0 auto 14px;border-radius:50%;border:3px solid rgba(34,197,94,.2);
        border-top-color:#22c55e;animation:csspin 0.9s linear infinite"></div>
      <div id="cs-step" style="color:#9fb0c2;font-size:14px"></div>
      <div style="color:#6b7c8f;font-size:12px;margin-top:8px">Le premier chargement prend quelques secondes.</div>
    </div>
  </div>
</div>
<style>@keyframes csspin{to{transform:rotate(360deg)}}</style>
"""

assert "<head>" in ui and "</body>" in ui
html = ui.replace("<head>", "<head>\n" + HEAD, 1)
html = html.replace("<body>", "<body>" + GATE, 1) if "<body>" in html else html.replace("</body>", GATE + "</body>", 1)
html = html.replace("</body>", "<script>\n" + loader + "\n</script>\n</body>", 1)
html = html.replace("<title>Cotescope</title>", "<title>Cotescope · Appli web</title>")

os.makedirs(out_dir, exist_ok=True)
with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
    f.write(html)
print("appli web ->", os.path.join(out_dir, "index.html"), len(html), "octets")
