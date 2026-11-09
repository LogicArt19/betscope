import json, urllib.request
def get(u):
    try:
        r = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=25); return json.loads(r.read())
    except Exception as e:
        return {"_err": str(e)}
for slug in ("eng.1", "uefa.nations"):
    sb = get(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard?dates=" + ("20261122" if slug == "eng.1" else "20261115"))
    evs = sb.get("events") or []
    print("SB", slug, len(evs), sb.get("_err", ""))
    if evs:
        s = get(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/summary?event={evs[0]['id']}")
        print("KEYS", slug, list(s))
        h = s.get("headToHeadGames")
        print("H2H", json.dumps(h)[:1200])
    st = get(f"https://site.api.espn.com/apis/v2/sports/soccer/{slug}/standings")
    ch = st.get("children") or []
    print("ST", slug, st.get("_err", ""), len(ch), [c.get("name") for c in ch][:8])
    if ch:
        e = ch[0]["standings"]["entries"][0]
        print("  ENTRY", e["team"]["displayName"], [(x.get("name"), x.get("displayValue")) for x in e["stats"]][:16])
