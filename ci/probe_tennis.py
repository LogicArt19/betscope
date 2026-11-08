import json, urllib.request
def get(u):
    r = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=25); return json.loads(r.read())
j = get("https://site.web.api.espn.com/apis/common/v3/sports/tennis/atp/athletes/3623/overview")
print("GL", json.dumps(j.get("gameLog"))[:3500])
print("NG", json.dumps(j.get("nextGame"))[:800])
r = get("https://site.api.espn.com/apis/site/v2/sports/tennis/wta/rankings")
print("NRANK", len(r["rankings"][0]["ranks"]), [ (x["current"], x["athlete"]["displayName"], x.get("points")) for x in r["rankings"][0]["ranks"][:3]], list(r["rankings"][0]["ranks"][0]))
a = get("https://site.web.api.espn.com/apis/common/v3/sports/tennis/atp/athletes/3623")["athlete"]
print("ATH", {k: a.get(k) for k in ("age", "displayHeight", "hand", "birthPlace", "citizenship", "turnedPro", "debutYear", "statsSummary", "rank")}, list(a))
