import sys, os, re, json, urllib.request, gzip
sys.path.insert(0, os.getcwd())
import collector as C
req = urllib.request.Request("https://www.unibet.fr/sport/football/france/ligue-1-mcdonalds", headers=C._ST_UA)
r = urllib.request.urlopen(req, timeout=30); raw = r.read(); enc = r.headers.get("Content-Encoding")
print("ENC", enc, len(raw))
page = (gzip.decompress(raw) if enc == "gzip" else raw).decode("utf-8", "replace")
lds = re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page, re.S)
print("LDS", len(lds), [x[:80] for x in lds])
for x in lds:
    try: d = json.loads(x); print("OK", type(d).__name__, len(d) if isinstance(d, list) else list(d)[:3])
    except Exception as e: print("ERR", e, x[max(0, e.pos-60):e.pos+60] if hasattr(e, "pos") else "")
blks = page.split("<psel-event-main")[1:]; print("BLKS", len(blks))
if blks: print("B0", re.findall(r'href="([^"]+)"', blks[0])[:3], re.findall(r"<span[^>]*>\s*(\d{1,3},\d{2})\s*</span>", blks[0][:8000])[:4])
C.collect_unibet([])
