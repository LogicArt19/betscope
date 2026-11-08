import json, urllib.request
def get(u):
    try:
        r = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=25); return r.status, json.loads(r.read())
    except Exception as e:
        return getattr(e, "code", type(e).__name__), None
st, j = get("https://site.api.espn.com/apis/site/v2/sports/tennis/atp/rankings")
rk = j["rankings"]; print("RK", type(rk).__name__, json.dumps(rk)[:1800])
aid = "3623"  # Sinner? test several
for a in ("10153", "3623"):
    for u in (f"https://site.web.api.espn.com/apis/common/v3/sports/tennis/atp/athletes/{a}",
              f"https://site.web.api.espn.com/apis/common/v3/sports/tennis/atp/athletes/{a}/overview",
              f"https://site.web.api.espn.com/apis/common/v3/sports/tennis/atp/athletes/{a}/results",
              f"https://site.web.api.espn.com/apis/common/v3/sports/tennis/atp/athletes/{a}/stats",
              f"https://sports.core.api.espn.com/v2/sports/tennis/leagues/atp/athletes/{a}/eventlog"):
        st, j = get(u); print("A", st, u, list(j)[:12] if isinstance(j, dict) else "")
        if j: print("  AJ", json.dumps(j)[:1500])
