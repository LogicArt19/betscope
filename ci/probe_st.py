import re, sys
sys.path.insert(0, "ci")
from probe_books import get  # noqa
st, b = get("https://www.sportytrader.com/cotes/angers-sco-nice-8532712/")
print("SIZE", len(b))
for m in re.finditer(r"window\.(__\w+)\s*=", b): print("WIN", m.group(1), b[m.end():m.end()+200].replace("\n"," "))
for kw in ("Plus de 2", "Les deux équipes", "Double chance", "data-odd", "x-data", "wire:", "market"):
    i = b.find(kw); print("KW", kw, b.count(kw), re.sub(r"\s+", " ", b[max(0,i-700):i+900]) if i >= 0 else "")
ws = [m.start() for m in re.finditer("winamax", b.lower())]
for i in ws[200:202]: print("WIN_OCC", re.sub(r"\s+", " ", b[i-1200:i+600]))
