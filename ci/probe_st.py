import re, sys
sys.path.insert(0, "ci")
from probe_books import get  # noqa
st, b = get("https://www.sportytrader.com/cotes/angers-sco-nice-8532712/")
for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', b, re.S): print("LD", re.sub(r"\s+", " ", m)[:1500])
print("H1", re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "|", (re.search(r"<h1.*?</h1>", b, re.S) or [""])[0]))[:400])
print("TITLE", re.findall(r"<title>(.*?)</title>", b, re.S)[:1])
i = b.find("datetime"); print("DT", re.sub(r"\s+", " ", b[i-200:i+300]) if i > 0 else "none")
st, l = get("https://www.sportytrader.com/cotes/football/france/ligue-1-123/")
i = l.find("angers-sco-nice-8532712"); print("LIST", re.sub(r"\s+", " ", re.sub(r'\sclass="[^"]*"', "", l[i-1500:i+1500])))
