import json, urllib.request
def get(u):
    try:
        r = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=25); return json.loads(r.read())
    except Exception as e:
        return {"_err": str(e)}
B = "https://site.api.espn.com/apis/site/v2/sports/soccer"
for u in (f"{B}/eng.1/teams/359/schedule?season=2024", f"{B}/all/teams/359/schedule?season=2024", f"{B}/eng.1/teams/359/schedule?season=2024&fixture=false",
          f"{B}/uefa.nations/teams/478/schedule?season=2024", f"{B}/all/teams/478/schedule?season=2024", f"{B}/fifa.friendly/teams/478/schedule?season=2024"):
    j = get(u); ev = j.get("events") or []
    print("U", u[len(B):], j.get("_err", ""), len(ev), sorted({(e.get("league") or {}).get("abbreviation") or (e.get("seasonType") or {}).get("name", "") for e in ev})[:8] if ev else "")
    if ev: print("  E", json.dumps(ev[0])[:600])
nb = get("https://site.api.espn.com/apis/v2/sports/basketball/nba/standings")
print("NBA", nb.get("_err", ""), [c.get("name") for c in nb.get("children") or []])
