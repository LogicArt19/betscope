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
<div id="cs-load" style="position:fixed;inset:0;z-index:95;display:none;place-items:center;
  background:radial-gradient(1200px 600px at 50% -10%,rgba(34,197,94,.16),transparent 60%),#070b10;color:#e8eef5">
  <div style="text-align:center">
    <img src="../img/logo.svg" alt="" style="width:60px;height:60px;margin:0 auto 14px;display:block" onerror="this.remove()">
    <div id="cs-spin" style="width:40px;height:40px;margin:0 auto 14px;border-radius:50%;border:3px solid rgba(34,197,94,.2);
      border-top-color:#22c55e;animation:csspin .9s linear infinite"></div>
    <div id="cs-step" style="color:#9fb0c2;font-size:14px"></div>
    <div id="cs-hint" style="color:#6b7c8f;font-size:12px;margin-top:8px">Le premier chargement prend quelques secondes.</div>
    <button id="cs-retry" class="btn primary" style="display:none;margin:16px auto 0" onclick="location.reload()">Réessayer</button>
  </div>
</div>
<style>@keyframes csspin{to{transform:rotate(360deg)}} #authov{background:radial-gradient(1200px 600px at 50% -10%,rgba(34,197,94,.14),transparent 60%),rgba(7,11,16,.97)}</style>
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
