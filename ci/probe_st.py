import sys, os, json
sys.path.insert(0, os.getcwd())
import collector as C
for sk in ("soccer_france_ligue_one", "soccer_netherlands_eredivisie", "soccer_uefa_europa_league"):
    L = C.st_list(C.ST_LEAGUES[sk]); print("LIST", sk, len(L), L[:3])
    for link, h, a, t in L[:2]:
        r = C.st_match(link); print("MATCH", h, a, t, json.dumps(r)[:1500])
