import sys, os, re, json, urllib.request
sys.path.insert(0, os.getcwd())
import collector as C
for path in ("/cotes/basketball/usa/nba-306/", "/cotes/basketball/", "/cotes/basketball/etats-unis/nba-306/"):
    try:
        p = C._st_get(path); links = sorted(set(re.findall(r'href="(/cotes/[a-z0-9-]+/)"', p)))
        print("P", path, len(p), [l for l in links if "nba" in l or "basket" in l][:15])
        rows = re.findall(r'<span[^>]*>\s*(\d{1,2} [a-zéû]+\.? - \d{1,2}:\d{2})\s*</span>\s*<span[^>]*>\s*<a href="(/cotes/[a-z0-9-]+-\d+/)"[^>]*>\s*([^<]+?)\s*</a>', p)
        print("ROWS", len(rows), rows[:3])
        if rows:
            b_ = C._st_compact(C._st_get(rows[0][1]))
            print("IDS", sorted(set(re.findall(r'\sid="([A-Z_]+)"', b_))))
            for hm in re.finditer(r"<span> Bookmaker </span>((?: <span>[^<]*</span>)+?) <span> Bonus </span>", b_):
                before = b_[max(0, hm.start() - 400):hm.start()]
                sec = [m.group(1) for m in re.finditer(r'\sid="([A-Z_]+)"', b_[:hm.start()])][-1:]
                print("HEAD", sec, re.findall(r"<span\s*>([^<]{2,80})</span>", before)[-1:], re.findall(r"<span>([^<]*)</span>", hm.group(1)), re.findall(r'data-panel-key="([A-Z_0-9.]+)"', before)[-1:])
            i = b_.find("Bookmaker"); j = b_.find('<a href="/book/', i); print("ROW", b_[j:j+900])
            break
    except Exception as e:
        print("P", path, e)
def get(u): return json.loads(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=25).read())
sb = get("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard")
e = sb["events"][0]; print("ESPN", e["id"], e["name"], e["date"])
s = get(f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={e['id']}")
print("SUMKEYS", list(s))
print("L5", json.dumps(s.get("lastFiveGames"))[:700])
print("INJ", json.dumps(s.get("injuries"))[:700])
print("STAND", json.dumps(s.get("standings"))[:500])
print("PICK", json.dumps(s.get("pickcenter"))[:600])
print("LEAD", json.dumps(s.get("leaders"))[:600])
