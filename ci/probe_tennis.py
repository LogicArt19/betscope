import sys, os, re
sys.path.insert(0, os.getcwd())
import collector as C
p = C._st_get("/cotes/tennis/")
links = sorted(set(re.findall(r'href="(?:https://www\.sportytrader\.com)?(/cotes/tennis/[a-z0-9-]+/[a-z0-9-]+-\d+/)"', p)))
print("TOURN", len(links), links[:40])
rows = re.findall(r'<span[^>]*>\s*(\d{1,2} [a-zéû]+\.? - \d{1,2}:\d{2})\s*</span>\s*<span[^>]*>\s*<a href="(/cotes/[a-z0-9-]+-\d+/)"[^>]*>\s*([^<]+?)\s*</a>', p)
print("ROWS_MAIN", len(rows), rows[:5])
for l in links[:4]:
    q = C._st_get(l)
    r = re.findall(r'<span[^>]*>\s*(\d{1,2} [a-zéû]+\.? - \d{1,2}:\d{2})\s*</span>\s*<span[^>]*>\s*<a href="(/cotes/[a-z0-9-]+-\d+/)"[^>]*>\s*([^<]+?)\s*</a>', q)
    print("T", l, len(r), r[:3])
    if r:
        b_ = C._st_compact(C._st_get(r[0][1]))
        for hm in re.finditer(r"<span> Bookmaker </span>((?: <span>[^<]*</span>)+?) <span> Bonus </span>", b_):
            before = b_[max(0, hm.start() - 400):hm.start()]
            sec = [m.group(1) for m in re.finditer(r'\sid="([A-Z_]+)"', b_[:hm.start()])][-1:]
            print("  HEAD", sec, re.findall(r"<span\s*>([^<]{2,80})</span>", before)[-1:], re.findall(r"<span>([^<]*)</span>", hm.group(1)))
        print("  BOOKS", sorted(set(re.findall(r'<a href="/book/([a-z0-9]+)/odds', b_))))
