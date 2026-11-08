import sys, os, re
sys.path.insert(0, os.getcwd())
import collector as C
b_ = C._st_compact(C._st_get("/cotes/angers-sco-nice-8532712/"))
for hm in re.finditer(r"<span> Bookmaker </span>((?: <span>[^<]*</span>)+?) <span> Bonus </span>", b_):
    before = b_[max(0, hm.start() - 400):hm.start()]
    print("HEAD", re.findall(r"<span\s*>([^<]{2,80})</span>", before)[-1:], re.findall(r"<span>([^<]*)</span>", hm.group(1)), re.findall(r'data-panel-key="([A-Z_0-9]+)"', before)[-1:])
