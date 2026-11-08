"""Sonde : quelles sources publiques de cotes (gratuites) répondent depuis le serveur de collecte ?
N'écrit que des codes HTTP, des tailles et des comptes (aucune donnée personnelle)."""
import json, re, urllib.request, gzip
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
      "Accept-Language": "fr-FR,fr;q=0.9", "Accept": "*/*"}
def get(u):
    try:
        r = urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=25)
        b = r.read()
        if r.headers.get("Content-Encoding") == "gzip": b = gzip.decompress(b)
        return r.status, b.decode("utf-8", "replace")
    except Exception as e:
        return getattr(e, "code", "ERR " + type(e).__name__), ""
T = {
 "kambi_ubfr": "https://eu-offering-api.kambicdn.com/offering/v2018/ubfr/listView/football/all/all/all/matches.json?lang=fr_FR&market=FR&client_id=2&channel_id=1&ncid=1&useCombined=true",
 "winamax_l1": "https://www.winamax.fr/paris-sportifs/sports/1/7/4",
 "sportytrader_l1": "https://www.sportytrader.com/cotes/football/france/ligue-1-123/",
 "coteur_l1": "https://www.coteur.com/cotes/foot/france/ligue-1",
 "winamax": "https://www.winamax.fr/paris-sportifs/sports/1",
 "betclic_v4": "https://offer.cdn.betclic.fr/api/pub/v4/events?application=2&countrycode=fr&language=fr&sitecode=frfr&sportIds=1&limit=50",
 "betclic_v2": "https://offer.cdn.betclic.fr/api/pub/v2/sports/1?application=2&countrycode=fr&language=fr&sitecode=frfr",
 "betclic_html": "https://www.betclic.fr/football-s1",
 "pmu": "https://paris.pmu.fr/",
 "parionssport": "https://www.enligne.parionssport.fdj.fr/paris-football",
 "sportytrader": "https://www.sportytrader.com/cotes/football/",
 "coteur": "https://www.coteur.com/",
 "matchendirect": "https://www.matchendirect.fr/paris-sportifs/",
}
for k, u in T.items():
    st, body = get(u)
    import os; os.makedirs("dump", exist_ok=True); open(f"dump/{k}.html", "w").write(body)
    info = ""
    if k == "kambi_ubfr" and body:
        try:
            j = json.loads(body); ev = j.get("events", [])
            info = f"events={len(ev)} with_offers={sum(1 for e in ev if e.get('betOffers'))}"
            if ev: info += " ex=" + ev[0]["event"].get("name", "")[:40]
        except Exception as e: info = "json? " + str(e)[:60]
    elif k.startswith("winamax"):
        i = body.find("PRELOADED_STATE")
        m = i >= 0
        if m:
            try:
                j, _ = json.JSONDecoder().raw_decode(body[body.index("{", i):]); info = f"matches={len(j.get('matches', {}))} odds={len(j.get('odds', {}))} outcomes={len(j.get('outcomes', {}))}"
            except Exception as e: info = "state? " + str(e)[:60]
        else: info = "no state"
    elif body:
        low = body.lower()
        names = {n: low.count(n) for n in ("betclic", "winamax", "unibet", "bet365", "parions", "pmu")}
        marks = [m for m in ("__NEXT_DATA__", "__NUXT__", "window.__", "application/ld+json", "data-odd", "data-cote", "odds") if m in body]
        nums = re.findall(r"(?<![\d.,])\d{1,2}[.,]\d{2}(?![\d.,])", body)
        info = f"names={names} marks={marks} decimals={len(nums)}"
        for n in ("betclic", "winamax"):
            for mm in re.finditer(n, low):
                seg = body[mm.start():mm.start() + 600]
                if re.search(r"\d[.,]\d\d", seg):
                    print("   SNIP", k, n, re.sub(r"\s+", " ", body[max(0, mm.start() - 300):mm.start() + 500])[:800])
                    break
    print(f"{k:14s} {st} size={len(body)} {info}")
