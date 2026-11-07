"""Réserve l'adresse *.workers.dev du compte Cloudflare (une seule fois)."""
import json, os, sys, urllib.error, urllib.request
acc, tok = os.environ["CF_ACCOUNT_ID"].strip(), os.environ["CF_API_TOKEN"].strip()
url = f"https://api.cloudflare.com/client/v4/accounts/{acc}/workers/subdomain"
H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def call(method, data=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data else None, headers=H, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"success": False, "status": e.code, "body": e.read().decode(errors="replace")[:300]}


cur = call("GET")
if cur.get("success") and (cur.get("result") or {}).get("subdomain"):
    print(cur["result"]["subdomain"]); sys.exit(0)
for name in ("cotescope", "cotescope-api", "cotescope-fr", "cotescope-app"):
    r = call("PUT", {"subdomain": name})
    if r.get("success"):
        print(name); sys.exit(0)
    sys.stderr.write(f"{name} : {r}\n")
sys.exit(1)
