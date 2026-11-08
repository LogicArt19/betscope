import json, urllib.request
def get(u):
    try:
        r = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=25); return r.status, json.loads(r.read())
    except Exception as e:
        return getattr(e, "code", type(e).__name__), None
B = "https://site.api.espn.com/apis/site/v2/sports/tennis"
for u in (f"{B}/atp/scoreboard", f"{B}/wta/scoreboard", f"{B}/atp/rankings", "https://site.api.espn.com/apis/site/v2/sports/tennis/atp/athletes",
          "https://sports.core.api.espn.com/v2/sports/tennis/leagues/atp/rankings", "https://site.web.api.espn.com/apis/site/v2/sports/tennis/atp/rankings"):
    st, j = get(u); print("U", st, u, list(j)[:10] if isinstance(j, dict) else "")
    if j: print("  J", json.dumps(j)[:1200])
st, sb = get(f"{B}/atp/scoreboard")
try:
    ev = sb["events"][0]; print("EV", ev.get("name"), [g.get("grouping", {}).get("displayName") for g in ev.get("groupings", [])][:5])
    c = ev["groupings"][0]["competitions"][0]
    print("COMP", json.dumps(c)[:2500])
    aid = c["competitors"][0]["athlete"]["id"] if "athlete" in c["competitors"][0] else c["competitors"][0]["id"]
    for u in (f"https://site.web.api.espn.com/apis/common/v3/sports/tennis/atp/athletes/{aid}", f"https://site.web.api.espn.com/apis/common/v3/sports/tennis/atp/athletes/{aid}/overview",
              f"https://site.web.api.espn.com/apis/common/v3/sports/tennis/atp/athletes/{aid}/results"):
        st, j = get(u); print("A", st, u, list(j)[:12] if isinstance(j, dict) else "")
        if j: print("  AJ", json.dumps(j)[:2000])
except Exception as e:
    print("ERR", repr(e))
