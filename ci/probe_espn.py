import json, urllib.request
def get(u):
    r = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=25); return json.loads(r.read())
L = get("https://sports.core.api.espn.com/v2/sports/soccer/leagues/fra.1/seasons/2026/types/1/leaders?limit=100")
for c in L["categories"]: print("CAT", c["name"], len(c["leaders"]), c["leaders"][0]["shortDisplayValue"] if c["leaders"] else "")
R = get("https://site.api.espn.com/apis/site/v2/sports/soccer/fra.1/teams/176/roster")
a = R["athletes"][0]; print("ROSTERKEYS", list(a)); print("STATS", json.dumps(a.get("statistics"))[:800])
s = get("https://site.api.espn.com/apis/site/v2/sports/soccer/fra.1/summary?event=401876412")
l5 = s.get("lastFiveGames"); print("L5TOP", type(l5).__name__, json.dumps(l5)[:1500])
print("BXL5", "lastFiveGames" in (s.get("boxscore") or {}), "form" in (s.get("boxscore") or {}))
