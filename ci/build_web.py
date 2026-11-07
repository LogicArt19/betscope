# -*- coding: utf-8 -*-
"""Publie l'appli web (app/index.html) à partir de web/app.html.
Usage : python ci/build_web.py <dossier_sortie>

Sécurité ajoutée à la publication :
- politique CSP stricte : seul le script de l'appli (identifié par son empreinte SHA-256) peut s'exécuter
  dans la page, aucun script injecté ;
- empreinte (SRI) de pyodide.js : si le fichier du CDN était modifié, le navigateur refuserait de le charger.
"""
import base64
import hashlib
import os
import re
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "dist_web")
html = open(os.path.join(ROOT, "web", "app.html"), encoding="utf-8").read()

m = re.search(r'const PYODIDE = "([^"]+)";', html)
try:
    data = urllib.request.urlopen(m.group(1) + "pyodide.js", timeout=30).read()
    sri = "sha384-" + base64.b64encode(hashlib.sha384(data).digest()).decode()
    html = html.replace('const PYODIDE_SRI = "";', f'const PYODIDE_SRI = "{sri}";', 1)
    print("SRI pyodide.js :", sri)
except Exception as e:  # pas de réseau : l'appli marche quand même, sans cette vérification
    print("SRI pyodide.js indisponible :", e)

scripts = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
hashes = " ".join("'sha256-%s'" % base64.b64encode(hashlib.sha256(s.encode("utf-8")).digest()).decode() for s in scripts)

CSP = ("default-src 'self'; "
       f"script-src {hashes} 'unsafe-eval' 'wasm-unsafe-eval' https://cdn.jsdelivr.net; "
       "connect-src 'self' https://cdn.jsdelivr.net https://*.workers.dev https://api.cotescope.fr; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com data:; "
       "img-src 'self' data: https:; worker-src 'self' blob:; object-src 'none'; base-uri 'none'; "
       "form-action 'none'; upgrade-insecure-requests")
assert "<!--CSP-->" in html
html = html.replace("<!--CSP-->", f'<meta http-equiv="Content-Security-Policy" content="{CSP}">\n<meta name="referrer" content="no-referrer">', 1)
os.makedirs(out_dir, exist_ok=True)
with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
    f.write(html)
print("appli web ->", os.path.join(out_dir, "index.html"), len(html), "octets,", len(scripts), "script(s) autorisé(s)")
