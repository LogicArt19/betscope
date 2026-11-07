"""Crée (si besoin) l'espace de stockage Cloudflare KV « COTESCOPE » et affiche son identifiant."""
import json, os, sys, urllib.request
acc, tok = os.environ["CF_ACCOUNT_ID"], os.environ["CF_API_TOKEN"]
base = f"https://api.cloudflare.com/client/v4/accounts/{acc}/storage/kv/namespaces"
H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

def call(url, data=None, method="GET"):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data else None, headers=H, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

for ns in call(base + "?per_page=100").get("result", []):
    if ns["title"] == "COTESCOPE":
        print(ns["id"]); sys.exit(0)
print(call(base, {"title": "COTESCOPE"}, "POST")["result"]["id"])
