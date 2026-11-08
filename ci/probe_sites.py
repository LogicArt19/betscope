import re, gzip, urllib.request
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36", "Accept-Language": "fr-FR,fr;q=0.9"}
def get(u):
    r = urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=25); b = r.read()
    if r.headers.get("Content-Encoding") == "gzip": b = gzip.decompress(b)
    return b.decode("utf-8", "replace")
L = get("https://www.unibet.fr/sport/football/france/ligue-1-mcdonalds")
links = sorted(set(re.findall(r'href="(/paris-football/[^"#]+/\d+/[^"#]+)"', L)))
print("LINKS", len(links), links[:5])
for comp in ("https://www.unibet.fr/sport/football/pays-bas/eredivisie", "https://www.unibet.fr/sport/football/angleterre/premier-league", "https://www.unibet.fr/sport/football"):
    try: x = get(comp); print("COMP", comp, len(x), len(re.findall(r'href="(/paris-football/[^"#]+/\d+/[^"#]+)"', x)))
    except Exception as e: print("COMP", comp, e)
b = get("https://www.unibet.fr" + links[-1])
t = re.sub(r"<!---->", "", b); t = re.sub(r'\s(class|style|aria-[\w-]+|type|pseloddvariation|originevent)="[^"]*"', "", t); t = re.sub(r"\s+", " ", t)
print("SIZE", len(b))
txt = re.sub(r"\s*\|\s*(\|\s*)+", " | ", re.sub(r"<[^>]+>", " | ", re.sub(r"<(script|style)[^>]*>.*?</\1>", "", t, flags=re.S)))
i = txt.find("Double"); print("TXT", txt[max(0, i-2500):i+3500])
for kw in ("Les deux équipes", "Plus de", "Double chance", "Nombre de buts"):
    j = t.find(kw); print("RAW", kw, t[max(0, j-300):j+700] if j > 0 else "-")
