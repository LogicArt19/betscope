import re, sys
sys.path.insert(0, "ci")
from probe_books import get  # noqa
st, b = get("https://www.sportytrader.com/cotes/angers-sco-nice-8532712/")
def compact(x):
    x = re.sub(r"<svg.*?</svg>", "", x, flags=re.S)
    x = re.sub(r'\s(class|width|height|loading|rel|target|style|x-\w+|@\w+|:\w+|data-trans\w*)="[^"]*"', "", x)
    return re.sub(r"\s+", " ", x)
tabs = re.findall(r'href="#([A-Z0-9_]+)"[^>]*>\s*([^<]{1,60})<', b)
print("TABS", tabs)
ids = [(m.start(), m.group(1)) for m in re.finditer(r'\sid="([A-Z0-9_]+)"', b)]
print("IDS", [i for _, i in ids][:80])
for pos, i in ids[:6]:
    print("SEC", i, compact(b[pos:pos + 9000])[:3500])
