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
 "kambi_ubfr": "https://eu-offering-api.kambicdn.com/offering/v2018/ubfr/listView/football.json?lang=fr_FR&market=FR&useCombined=true",
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
    info = ""
    if k == "kambi_ubfr" and body:
        try:
            j = json.loads(body); ev = j.get("events", [])
            info = f"events={len(ev)} with_offers={sum(1 for e in ev if e.get('betOffers'))}"
            if ev: info += " ex=" + ev[0]["event"].get("name", "")[:40]
        except Exception as e: info = "json? " + str(e)[:60]
    elif k == "winamax":
        m = re.search(r"PRELOADED_STATE\s*=\s*(\{.*?\});\s*</script>", body, re.S)
        if m:
            try:
                j = json.loads(m.group(1)); info = f"matches={len(j.get('matches', {}))} odds={len(j.get('odds', {}))} outcomes={len(j.get('outcomes', {}))}"
            except Exception as e: info = "state? " + str(e)[:60]
        else: info = "no state"
    elif body:
        info = f"cotes-like={len(re.findall(r'[>\"\\s]\\d,\\d\\d<', body))} json={body[:1] in '[{'}"
    print(f"{k:14s} {st} size={len(body)} {info}")
