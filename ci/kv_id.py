"""Crée (si besoin) l'espace de stockage Cloudflare KV « COTESCOPE » et affiche son identifiant."""
import json, os, sys, urllib.error, urllib.request
acc, tok = os.environ["CF_ACCOUNT_ID"].strip(), os.environ["CF_API_TOKEN"].strip()
base = f"https://api.cloudflare.com/client/v4/accounts/{acc}/storage/kv/namespaces"
H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def call(url, data=None, method="GET"):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data else None, headers=H, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        sys.stderr.write(f"Cloudflare {e.code} sur {url.split('/client/v4')[1]} : {body}\n")
        if e.code in (401, 403):
            try:
                v = urllib.request.urlopen(urllib.request.Request(
                    "https://api.cloudflare.com/client/v4/user/tokens/verify", headers=H), timeout=20).read().decode()
                sys.stderr.write("Vérification de la clé : " + v[:300] + "\n")
            except urllib.error.HTTPError as e2:
                sys.stderr.write(f"Clé Cloudflare refusée ({e2.code}) : vérifie le secret CF_API_TOKEN (copie complète, sans espace).\n")
        sys.exit(1)


for ns in call(base + "?per_page=100").get("result", []):
    if ns["title"] == "COTESCOPE":
        print(ns["id"])
        sys.exit(0)
print(call(base, {"title": "COTESCOPE"}, "POST")["result"]["id"])
