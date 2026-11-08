"""Sonde : sites publics de cotes joignables depuis le serveur de collecte (codes, tailles, comptes seulement)."""
import re, gzip, urllib.request
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36", "Accept-Language": "fr-FR,fr;q=0.9"}
def get(u):
    try:
        r = urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=25); b = r.read()
        if r.headers.get("Content-Encoding") == "gzip": b = gzip.decompress(b)
        return r.status, b.decode("utf-8", "replace")
    except Exception as e:
        return getattr(e, "code", "ERR " + type(e).__name__), ""
U = [
 "https://www.ruedesjoueurs.com/cotes/football.html", "https://www.ruedesjoueurs.com/cotes/football/ligue-1.html",
 "https://www.pronosoft.com/fr/parions_sport/", "https://www.pronosoft.com/fr/parions_sport/liste-parions-sport-plein-ecran.htm",
 "https://www.betexplorer.com/football/france/ligue-1/", "https://www.oddsportal.com/football/france/ligue-1/",
 "https://www.cotes.fr/football/ligue-1-ed3", "https://www.cotes.fr/", "https://www.comparateur-de-cotes.fr/comparateur/football",
 "https://www.enligne.parionssport.fdj.fr/paris-football/france/ligue-1-mcdonald-s", "https://www.enligne.parionssport.fdj.fr/paris-football",
 "https://www.zebet.fr/fr/competition/96-ligue_1", "https://www.netbet.fr/football/france/ligue-1",
 "https://www.oddspedia.com/fr/football/france/ligue-1", "https://www.matchendirect.fr/paris-sportifs/comparateur-cotes/",
 "https://www.coteur.com/cotes/foot/france/ligue-1", "https://www.winamax.fr/paris-sportifs/sports/1/7/4",
 "https://www.unibet.fr/sport/football/france/ligue-1-mcdonalds", "https://www.footmercato.net/paris-sportifs/cotes/",
 "https://www.lequipe.fr/Paris-sportifs/", "https://www.flashscore.fr/football/france/ligue-1/cotes/",
 "https://www.parisportifs.fr/cotes/football/", "https://www.betclic.fr/football-s1/ligue-1-mcdonald-s-c4",
]
for u in U:
    st, b = get(u); low = b.lower()
    names = {n: low.count(n) for n in ("betclic", "winamax", "unibet", "bet365", "parions", "pmu")}
    nums = len(re.findall(r"(?<![\d.,])\d{1,2}[.,]\d{2}(?![\d.,])", b))
    js = [m for m in ("__NEXT_DATA__", "__NUXT__", "PRELOADED_STATE", "__INITIAL_STATE__", "application/ld+json") if m in b]
    print(f"S {st} {len(b):8d} dec={nums:5d} {names} {js} {u}")
    if "parionssport" in u and b:
        i = b.find("Lens"); print("   SNIP", re.sub(r"\s+", " ", re.sub(r'\s(class|style)="[^"]*"', "", b[max(0, i-600):i+900]))[:1400])
    if "pronosoft" in u and b:
        i = b.find("Lens"); print("   SNIP", re.sub(r"\s+", " ", re.sub(r'\s(class|style)="[^"]*"', "", b[max(0, i-600):i+900]))[:1400])
