import re, gzip, urllib.request, json
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36", "Accept-Language": "fr-FR,fr;q=0.9"}
def get(u):
    r = urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=25); b = r.read()
    if r.headers.get("Content-Encoding") == "gzip": b = gzip.decompress(b)
    return b.decode("utf-8", "replace")
for u in ("https://www.unibet.fr/sport/football/france/ligue-1-mcdonalds", "https://www.flashscore.fr/football/france/ligue-1/cotes/", "https://www.oddsportal.com/football/france/ligue-1/"):
    try: b = get(u)
    except Exception as e: print("X", u, e); continue
    for m in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', b, re.S)[:2]: print("LD", u[12:30], re.sub(r"\s+", " ", m)[:700])
    i = b.find("Marseille"); print("SN", u[12:30], re.sub(r"\s+", " ", re.sub(r'\s(class|style)="[^"]*"', "", b[max(0, i-500):i+1500]))[:1800])
    print("SCRIPTS", u[12:30], [s[:120] for s in re.findall(r"<script[^>]*>(.{0,120})", b) if "{" in s][:8])
