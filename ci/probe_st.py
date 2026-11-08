import re, sys, html
sys.path.insert(0, "ci")
from probe_books import get  # noqa
st, body = get("https://www.sportytrader.com/cotes/football/france/ligue-1-123/")
links = sorted(set(re.findall(r'href="(https://www\.sportytrader\.com/cotes/[^"]+)"', body)) | set("https://www.sportytrader.com" + x for x in re.findall(r'href="(/cotes/[^"]+)"', body)))
print("LINKS", len(links)); [print("  L", l) for l in links[:40]]
def text(b): return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " | ", re.sub(r"<(script|style)[^>]*>.*?</\1>", "", b, flags=re.S))))
t = text(body); i = t.find("Betclic"); print("LISTTEXT", t[max(0,i-1500):i+1500])
cand = [l for l in links if l.count("/") >= 8 or re.search(r"-\d+/$", l) and "ligue-1-123" not in l]
for l in cand[:2]:
    st, b = get(l); t = text(b)
    print("MATCH", l, st, len(b), {n: b.lower().count(n) for n in ("betclic", "winamax", "unibet", "bet365", "pmu", "parions")})
    j = t.find("Betclic"); print("MTEXT", t[max(0, j-800):j+3500])
    print("JSONLD", re.findall(r'application/ld\+json">(.{0,600})', b)[:2])
