import sys, os, re
sys.path.insert(0, os.getcwd())
import collector as C
p = C._st_get("/cotes/football/")
links = sorted(set(re.findall(r'href="(?:https://www\.sportytrader\.com)?(/cotes/football/[a-z0-9-]+/[a-z0-9-]+-\d+/)"', p)))
print("LINKS", len(links)); [print(" L", l) for l in links if any(k in l for k in ("europe", "nation", "monde", "international", "qualif"))]
for sk in ("soccer_uefa_champs_league", "soccer_uefa_europa_league", "soccer_uefa_europa_conference_league"):
    L = C.st_list(C.ST_LEAGUES[sk]); print("ST", sk, len(L), L[:2])
