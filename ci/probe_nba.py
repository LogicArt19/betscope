import sys, os, json
sys.path.insert(0, os.getcwd())
import stats as S
st = S.Stats.__new__(S.Stats)
for tid in ("4", "20", "8", "13", "6", "25"):
    try:
        g = st._nba_last_games(tid); print("T", tid, len(g), g[-2:])
    except Exception as e:
        print("T", tid, "ERR", repr(e))
j = st._web_get(f"{S.ESPN_BASKET}/nba/teams/13/schedule")
e = j["events"][0]; c = e["competitions"][0]
print("E", json.dumps({"status": c.get("status"), "comp0": c["competitors"][0]})[:1500])
