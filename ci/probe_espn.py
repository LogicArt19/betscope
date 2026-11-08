import json, urllib.request
def get(u):
    try:
        r = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=25)
        return r.status, json.loads(r.read())
    except Exception as e:
        return getattr(e, "code", type(e).__name__), None
U = ["https://site.api.espn.com/apis/site/v2/sports/soccer/fra.1/statistics",
     "https://site.web.api.espn.com/apis/site/v2/sports/soccer/fra.1/statistics",
     "https://site.web.api.espn.com/apis/common/v3/sports/soccer/fra.1/statistics/byathlete?limit=10&sort=offensive.totalGoals:desc",
     "https://sports.core.api.espn.com/v2/sports/soccer/leagues/fra.1/seasons/2026/types/1/leaders",
     "https://site.api.espn.com/apis/site/v2/sports/soccer/fra.1/teams/176/statistics",
     "https://site.api.espn.com/apis/site/v2/sports/soccer/fra.1/teams/176/roster",
     "https://site.api.espn.com/apis/site/v2/sports/soccer/fra.1/teams/176/schedule"]
for u in U:
    st, j = get(u)
    print("U", st, u, (list(j)[:12] if isinstance(j, dict) else type(j).__name__))
    if j: print("   J", json.dumps(j)[:1500])
st, s = get("https://site.api.espn.com/apis/site/v2/sports/soccer/fra.1/summary?event=401876412")
print("SUMKEYS", st, list(s or {}))
if s:
    for k in ("leaders", "keyEvents", "boxscore"):
        print("   ", k, json.dumps(s.get(k))[:1200])
    bx = (s.get("boxscore") or {}).get("lastFiveGames") or []
    if bx: print("   L5", json.dumps(bx[0].get("events", [])[:1])[:1500])
