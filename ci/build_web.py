# -*- coding: utf-8 -*-
"""Publie l'appli web (app/index.html) à partir de web/app.html.
Usage : python ci/build_web.py <dossier_sortie>"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "dist_web")
html = open(os.path.join(ROOT, "web", "app.html"), encoding="utf-8").read()

CSP = ("default-src 'self'; "
       "script-src 'self' 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval' https://cdn.jsdelivr.net; "
       "connect-src 'self' https://cdn.jsdelivr.net https://*.workers.dev https://api.cotescope.fr; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com data:; "
       "img-src 'self' data: https:; worker-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
assert "<!--CSP-->" in html
html = html.replace("<!--CSP-->", f'<meta http-equiv="Content-Security-Policy" content="{CSP}">\n<meta name="referrer" content="no-referrer">')
os.makedirs(out_dir, exist_ok=True)
with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
    f.write(html)
print("appli web ->", os.path.join(out_dir, "index.html"), len(html), "octets")
