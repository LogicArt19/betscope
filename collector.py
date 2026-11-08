# -*- coding: utf-8 -*-
"""
Collecteur Cotescope (lancé par GitHub Actions, toutes les heures).

Récupère UNE fois pour tous les abonnés :
  - les cotes (The Odds API), seulement quand c'est dû pour rester dans le quota
  - les stats (football-data.org) : résultats, classements, buteurs, journées, face-à-face
  - les infos web (ESPN) : absents, compos, actus, forme, météo
  - les scores finaux des derniers jours (pour vérifier l'historique des paris)
puis envoie un « snapshot » JSON au serveur (Cloudflare KV), avec le moteur d'analyse
pour l'appli web.

Variables d'environnement : ODDS_KEY, FD_KEY (optionnelle), CF_ACCOUNT_ID, CF_API_TOKEN, CF_KV_ID
"""

import gzip
import json
import re
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.environ.get("COLLECT_CACHE", os.path.join(HERE, ".collect_cache"))
os.makedirs(CACHE, exist_ok=True)

import betscope as b  # noqa: E402
import stats as S  # noqa: E402

b.config_dir = lambda: CACHE

# championnats couverts pour tous les abonnés (le serveur paie, donc on reste raisonnable)
SPORTS = ["soccer_france_ligue_one", "soccer_epl", "soccer_spain_la_liga", "soccer_italy_serie_a",
          "soccer_germany_bundesliga", "soccer_uefa_champs_league"]
ENGINE_FILES = ["betscope.py", "stats.py", "model.py", "markets.py", "history.py", "webapi.py"]


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def load_state():
    try:
        with open(os.path.join(CACHE, "collector_state.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    with open(os.path.join(CACHE, "collector_state.json"), "w", encoding="utf-8") as f:
        json.dump(st, f)


def odds_due(state, cfg):
    remaining = state.get("quota", {}).get("remaining")
    iv = b.auto_interval_minutes(cfg, remaining)
    last = state.get("odds_ts", 0)
    return time.time() - last >= iv * 60 - 120, iv


def collect_scores(state, slugs):
    """Scores finaux ESPN des 4 derniers jours (pour le règlement des paris)."""
    out = state.get("scores", {})
    st = S.Stats(CACHE)
    now = datetime.now(timezone.utc)
    for slug in slugs:
        for d in range(0, 4):
            day = (now - timedelta(days=d)).strftime("%Y%m%d")
            k = f"{slug}|{day}"
            if out.get(k, {}).get("final"):
                continue
            try:
                body = st._web_get(f"{S.ESPN_BASE}/{slug}/scoreboard?dates={day}")
            except Exception as e:
                log("scores", k, e)
                continue
            rows, all_done = [], True
            for e in body.get("events", []):
                try:
                    comp = e["competitions"][0]
                    done = bool(((e.get("status") or {}).get("type") or {}).get("completed"))
                    all_done &= done
                    cs = {c["homeAway"]: c for c in comp["competitors"]}
                    row = {"home": cs["home"]["team"].get("displayName", ""),
                           "away": cs["away"]["team"].get("displayName", ""),
                           "home_short": cs["home"]["team"].get("shortDisplayName", ""),
                           "away_short": cs["away"]["team"].get("shortDisplayName", ""),
                           "date": e.get("date"), "done": done}
                    if done:
                        row["h"], row["a"] = int(cs["home"].get("score")), int(cs["away"].get("score"))
                        lh, la = cs["home"].get("linescores") or [], cs["away"].get("linescores") or []
                        if lh and la:
                            row["ht_h"], row["ht_a"] = int(float(lh[0].get("value", 0))), int(float(la[0].get("value", 0)))
                        sc = []
                        for dd in comp.get("details") or []:
                            txt = str(((dd.get("type") or {}).get("text")) or "").lower()
                            if (dd.get("scoringPlay") or "goal" in txt) and "own goal" not in txt:
                                sc += [p.get("displayName", "") for p in dd.get("athletesInvolved") or []]
                        row["scorers"] = sc if comp.get("details") is not None else None
                    rows.append(row)
                except (KeyError, TypeError, ValueError):
                    continue
            out[k] = {"ts": time.time(), "v": rows, "final": bool(rows) and all_done and d > 0}
    # on ne garde que 12 jours
    limit = (now - timedelta(days=12)).strftime("%Y%m%d")
    return {k: v for k, v in out.items() if k.split("|")[1] >= limit}


TIERS = ("gratuit", "essentiel", "pro", "elite")
FREE_SPORTS = {"soccer_france_ligue_one", "soccer_epl"}


FREE_SPORTS_NEW = {"soccer_france_ligue_one", "soccer_epl", "soccer_spain_la_liga"}


def tier_snapshot(snap, tier):
    """Chaque forfait ne reçoit que les données qu'il a payées (impossible de débloquer en trichant).
    - Gratuit (permanent) : Ligue 1, Premier League, Liga ; cotes 1N2, forme, classement, face-à-face, enjeu
    - Essentiel : tout sauf les actus de dernière minute
    - Pro (et l'ancien Elite) : tout"""
    out = dict(snap, tier=tier)
    if tier in ("pro", "elite"):
        return out
    st = dict(snap["stats"])
    web = {}
    for eid, w in (st.get("web") or {}).items():
        v = dict(w.get("v") or {})
        v["news"] = []
        v["gnews"] = []
        if tier == "gratuit":
            v.update(inj_home=[], inj_away=[], lineups=[], last_xi=[], weather=None, cards=None, referee="")
        web[eid] = dict(w, v=v)
    st["web"] = web
    if tier == "gratuit":
        for k in ("scorers", "inj_day", "pred", "lineups"):
            st[k] = {}
        out["events"] = [dict(e, bookmakers=[dict(bk, markets=[m for m in bk.get("markets", []) if m.get("key") == "h2h"])
                                             for bk in e.get("bookmakers", [])])
                         for e in snap["events"] if e.get("sport_key") in FREE_SPORTS_NEW]
        out["sports"] = [s for s in snap.get("sports", []) if s in FREE_SPORTS_NEW]
    out["stats"] = st
    return out


def cf_put(key, data, gz=True, meta=None):
    acc, tok, ns = os.environ.get("CF_ACCOUNT_ID"), os.environ.get("CF_API_TOKEN"), os.environ.get("CF_KV_ID")
    if not (acc and tok and ns):
        log("pas d'identifiants Cloudflare : snapshot écrit en local seulement")
        return False
    raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
    body = gzip.compress(raw) if gz else raw
    url = (f"https://api.cloudflare.com/client/v4/accounts/{acc}/storage/kv/namespaces/{ns}/values/"
           f"{urllib.parse.quote(key)}")
    req = urllib.request.Request(url, data=body, method="PUT",
                                 headers={"Authorization": f"Bearer {tok}",
                                          "Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=60) as r:
        ok = json.loads(r.read().decode()).get("success")
    log("KV", key, len(raw) // 1024, "Ko ->", len(body) // 1024, "Ko compressé", "OK" if ok else "ECHEC")
    return ok


def collect_odds(state, cfg):
    """Cotes à 0 € le plus souvent possible :
    - le calendrier des matchs est gratuit chez The Odds API : on ne paie que les championnats qui jouent bientôt ;
    - le 1N2 (1 crédit) est rafraîchi souvent, le nombre de buts (1 crédit de plus) une fois par jour ;
    - le reste du budget du mois est réparti sur les jours restants."""
    sched = state.setdefault("odds_sched", {})
    old = {e["id"]: e for e in state.get("events", [])}
    by_sport = {}
    for e in state.get("events", []):
        by_sport.setdefault(e.get("sport_key") or e.get("_sport_key"), []).append(e)
    now = datetime.now(timezone.utc)
    key = cfg["api_key"].strip()
    soon, week = [], []
    for sp in SPORTS:
        try:
            cal, _ = b.http_get_json(f"{b.API_BASE}/sports/{sp}/events?" + urllib.parse.urlencode({"apiKey": key, "dateFormat": "iso"}))
            kicks = [b.parse_time(e["commence_time"]) for e in cal]
        except Exception as e:
            log("calendrier", sp, e)
            kicks = [b.parse_time(e["commence_time"]) for e in by_sport.get(sp, [])]
        if any(now < k < now + timedelta(hours=36) for k in kicks):
            soon.append(sp)
        elif any(now < k < now + timedelta(days=7) for k in kicks):
            week.append(sp)
    remaining = (state.get("quota") or {}).get("remaining")
    try:
        remaining = float(remaining)
    except (TypeError, ValueError):
        remaining = None
    nxt = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=timezone.utc)
    days_left = max(1.0, (nxt - now).total_seconds() / 86400)
    per_day = (remaining - 20) / days_left if remaining is not None else 16
    totals_per_day = len(soon) + len(week)
    h2h_iv = 24 * 60
    if soon:
        refresh = (per_day - totals_per_day) / len(soon)
        h2h_iv = 24 * 60 if refresh <= 1 else max(60, 24 * 60 / refresh)
    log(f"cotes : {len(soon)} championnat(s) jouent sous 36 h, {len(week)} cette semaine, "
        f"{remaining if remaining is not None else '?'} crédits, 1N2 toutes les {int(h2h_iv)} min")
    t = time.time()
    for sp in soon + week:
        sc = sched.setdefault(sp, {})
        iv = h2h_iv if sp in soon else 24 * 60
        tot_due = t - sc.get("tot", 0) >= 23 * 3600
        h2h_due = t - sc.get("h2h", 0) >= iv * 60 - 120
        if not (tot_due or h2h_due):
            continue
        cost = 2 if tot_due else 1
        if remaining is not None and remaining - cost < 5 and sc.get("h2h"):
            log(f"  {sp} : quota du mois presque vide, on garde les dernières cotes")
            continue
        markets = "h2h,totals" if tot_due else "h2h"
        ev, quota, errors = b.fetch_odds(dict(cfg, sports=[sp], markets=markets))
        if errors:
            log("  erreurs", sp, errors)
            continue
        if quota and quota.get("remaining") is not None:
            state["quota"] = quota
            try:
                remaining = float(quota["remaining"])
            except (TypeError, ValueError):
                pass
        for e in ev:
            e["_odds_ts"] = t
            e.setdefault("sport_key", sp)
            if not tot_due and e["id"] in old:  # on garde le dernier « nombre de buts » connu
                prev = {bk["key"]: bk for bk in old[e["id"]].get("bookmakers", [])}
                for bk in e.get("bookmakers", []):
                    keep = [m for m in prev.get(bk["key"], {}).get("markets", []) if m.get("key") != "h2h"]
                    bk["markets"] = [m for m in bk.get("markets", []) if m.get("key") == "h2h"] + keep
        by_sport[sp] = ev
        sc["h2h"] = t
        if tot_due:
            sc["tot"] = t
        state["odds_ts"] = t
        log(f"  {sp} : {len(ev)} matchs ({markets})")
    return [e for sp in by_sport for e in by_sport[sp]]


def add_espn_odds(events, st):
    """Cotes publiques d'ESPN (déjà récupérées avec les compos) : un bookmaker de plus, rafraîchi chaque heure."""
    n = 0
    web = st.data.get("web") or {}
    for e in events:
        v = (web.get(e["id"]) or {}).get("v") or {}
        for o in v.get("odds") or []:
            k = "espn_" + "".join(ch for ch in o["book"].lower() if ch.isalnum())[:20]
            e["bookmakers"] = [bk for bk in e.get("bookmakers", []) if bk.get("key") != k]
            e["bookmakers"].append({"key": k, "title": o["book"], "markets": [{"key": "h2h", "outcomes": [
                {"name": e["home_team"], "price": o["h"]}, {"name": e["away_team"], "price": o["a"]},
                {"name": "Draw", "price": o["d"]}]}]})
            n += 1
    if n:
        log(f"cotes ESPN ajoutées : {n}")


# ---------------------------------------------------------------------------
# OddsPapi (gratuit : 250 requêtes / mois) : cotes Bet365 pour les grands championnats
# ---------------------------------------------------------------------------
OP_BASE = "https://api.oddspapi.io/v4"
OP_TOURNAMENTS = {"soccer_epl": 17, "soccer_spain_la_liga": 8, "soccer_italy_serie_a": 23,
                  "soccer_germany_bundesliga": 35, "soccer_france_ligue_one": 34}  # identifiants Sportradar
OP_MONTHLY = 250
OP_EVERY_H = 8   # 3 fois par jour x 2 requêtes ≈ 180 / mois


def _op_get(path, **params):
    params["apiKey"] = os.environ.get("ODDSPAPI_KEY", "").strip().strip('"').strip("'")
    url = f"{OP_BASE}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Cotescope/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def oddspapi_diag():
    """Diagnostic sans révéler la clé : longueur, caractères suspects, réponse brute de /account."""
    raw = os.environ.get("ODDSPAPI_KEY", "")
    k = raw.strip().strip('"').strip("'")
    log(f"OddsPapi diag : clé de {len(raw)} caractères (après nettoyage {len(k)}), "
        f"espaces={'oui' if raw != raw.strip() else 'non'}, guillemets={'oui' if k != raw.strip() else 'non'}, "
        f"identique à la clé The Odds API (ODDS_KEY) : {'OUI' if k == os.environ.get('ODDS_KEY', '').strip() else 'non'}")
    for label, url, headers in (("paramètre apiKey", f"{OP_BASE}/account?apiKey={urllib.parse.quote(k)}", {}),
                                ("en-tête x-api-key", f"{OP_BASE}/account", {"x-api-key": k}),
                                ("paramètre apikey", f"{OP_BASE}/account?apikey={urllib.parse.quote(k)}", {})):
        try:
            req = urllib.request.Request(url, headers=dict({"User-Agent": "Cotescope/1.0", "Accept": "application/json"}, **headers))
            with urllib.request.urlopen(req, timeout=30) as r:
                log(f"OddsPapi diag ({label}) : HTTP {r.status} (clé acceptée)")
        except urllib.error.HTTPError as e:
            code = ""
            try:
                code = json.loads(e.read().decode(errors="replace")).get("error", {}).get("code", "")
            except Exception:
                pass
            log(f"OddsPapi diag ({label}) : HTTP {e.code} {code}")
        except Exception as e:
            log(f"OddsPapi diag ({label}) : {e}")


def op_market_map(markets):
    """Repère dans le catalogue OddsPapi les marchés qu'on sait analyser, et traduit leurs issues
    en clés Cotescope (dc1N, bttsY, o1.5…). Match complet uniquement, pas de paris sur joueurs."""
    out = {}
    for m in markets if isinstance(markets, list) else (markets or {}).get("data", []):
        try:
            if m.get("sportId") not in (10, None) or m.get("playerProp") or str(m.get("period", "fulltime")).lower() not in ("fulltime", "full time", "ft", ""):
                continue
            name, typ = str(m.get("marketName") or "").lower(), str(m.get("marketType") or "").lower()
            outs = {str(o["outcomeId"]): str(o.get("outcomeName") or "").strip() for o in m.get("outcomes") or []}
            line = m.get("handicap")
            kind, mp = None, {}
            if "double chance" in name:
                kind = "dc"
                mp = {oid: {"1x": "dc1N", "12": "dc12", "x2": "dcN2"}.get(n.lower().replace(" ", ""), "") for oid, n in outs.items()}
            elif "both teams" in name and "score" in name and "half" not in name:
                kind = "btts"
                mp = {oid: {"yes": "bttsY", "no": "bttsN"}.get(n.lower(), "") for oid, n in outs.items()}
            elif "draw no bet" in name:
                kind = "dnb"
                mp = {oid: {"1": "dnb1", "2": "dnb2"}.get(n.lower(), "") for oid, n in outs.items()}
            elif (typ in ("totals", "total", "overunder", "over/under") or "over/under" in name or name.startswith("total goals")) \
                    and "team" not in name and "corner" not in name and "card" not in name and "half" not in name and line is not None:
                ln = float(line)
                if ln not in (0.5, 1.5, 2.5, 3.5, 4.5):
                    continue
                for oid, n in outs.items():
                    nl = n.lower()
                    side = "Over" if nl.startswith("over") or nl.startswith("+") else ("Under" if nl.startswith("under") or nl.startswith("-") else "")
                    mp[oid] = side if ln == 2.5 else ({"Over": f"o{ln:g}", "Under": f"u{ln:g}"}.get(side, "") if side else "")
                out[str(m["marketId"])] = {"kind": "total", "line": ln, "outcomes": {k: v for k, v in mp.items() if v}}
                continue
            if kind and any(mp.values()):
                out[str(m["marketId"])] = {"kind": kind, "line": line, "outcomes": {k: v for k, v in mp.items() if v}}
        except (KeyError, TypeError, ValueError):
            continue
    return out


# bookmakers français disponibles sur OddsPapi -> clé Cotescope et nom affiché (Betclic n'y est pas)
OP_BOOKS = {"bet365.fr": ("bet365", "Bet365"), "winamax.fr": ("winamax_fr", "Winamax"),
            "unibet.fr": ("unibet_fr", "Unibet (FDJ)"), "fdj": ("parionssport_fr", "Parions Sport (FDJ)")}


def _op_parse(f, slug, op):
    """Un match OddsPapi -> cotes 1N2, plus/moins de 2,5 et autres paris (clés Cotescope)."""
    mk = (((f.get("bookmakerOdds") or {}).get(slug) or {}).get("markets") or {})
    m = mk.get("101") or mk.get("10728")
    if not m:
        return None
    price = lambda oid: (((m.get("outcomes") or {}).get(oid) or {}).get("players") or {}).get("0", {}).get("price")
    h, d, a = price("101"), price("102"), price("103")
    if not (h and d and a):
        return None
    extra, tot25 = {}, None
    for mid, meta in (op.get("mk") or {}).items():
        mm = mk.get(mid)
        if not mm:
            continue
        for oid, lab in meta["outcomes"].items():
            pl = (((mm.get("outcomes") or {}).get(oid) or {}).get("players") or {}).get("0") or {}
            pr = pl.get("price")
            if not pr or pl.get("active") is False:
                continue
            if meta["kind"] == "total" and meta.get("line") == 2.5:
                tot25 = dict(tot25 or {}, **{lab: float(pr)})
            elif lab:
                extra[lab] = float(pr)
    return {"extra": extra, "tot25": tot25 if tot25 and len(tot25) == 2 else None,
            "start": f.get("startTime") or f.get("trueStartTime"),
            "home": f.get("participant1Name") or (op.get("names") or {}).get(str(f.get("participant1Id")), ""),
            "away": f.get("participant2Name") or (op.get("names") or {}).get(str(f.get("participant2Id")), ""),
            "h": float(h), "d": float(d), "a": float(a), "ts": time.time()}


def collect_oddspapi(events, state):
    """Vraies cotes de Bet365, Winamax, Unibet et Parions Sport (FDJ) pour tous les types de paris, via OddsPapi.
    Quota gratuit (250 requêtes / mois) : le rythme est calculé chaque jour selon ce qu'il reste, et seuls les
    championnats qui jouent dans les 48 h sont demandés."""
    if not os.environ.get("ODDSPAPI_KEY"):
        return 0
    import calendar
    op = state.setdefault("oddspapi", {})
    now = datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    if op.get("month") != month:
        op.update(month=month, used=0)
    soon = {e.get("sport_key") for e in events if now < b.parse_time(e["commence_time"]) < now + timedelta(hours=48)}
    tids = [OP_TOURNAMENTS[k] for k in OP_TOURNAMENTS if k in soon]
    chunks = [tids[i:i + 3] for i in range(0, len(tids), 3)]
    per_refresh = len(chunks) * len(OP_BOOKS)
    days_left = calendar.monthrange(now.year, now.month)[1] - now.day + 1
    left = OP_MONTHLY - 6 - op.get("used", 0)
    per_day = max(0.0, left / days_left)
    refreshes = max(1, int(per_day // per_refresh)) if per_refresh else 0  # au moins 1 fois par jour si possible
    every_h = 24 / refreshes if refreshes else 24
    have = op.get("by") or {}
    fresh = bool(have) and all(have.get(s) for s in OP_BOOKS) and time.time() - op.get("last", 0) < every_h * 3600
    if chunks and not fresh and left >= per_refresh:
        if not op.get("mk"):  # catalogue des marchés (1 seule requête, gardé)
            try:
                op["mk"] = op_market_map(_op_get("markets"))
                op["used"] += 1
            except Exception as e:
                log("OddsPapi (marchés) :", e)
        by, ok = {}, 0
        for slug in OP_BOOKS:
            rows = []
            for chunk in chunks:
                try:
                    for f in _op_get("odds-by-tournaments", tournamentIds=",".join(map(str, chunk)), bookmaker=slug, verbosity=3) or []:
                        x = _op_parse(f, slug, op)
                        if x:
                            rows.append(x)
                    op["used"] += 1
                    ok += 1
                    time.sleep(1.2)  # délai minimum entre deux appels
                except Exception as e:
                    log(f"OddsPapi ({slug}) :", e)
            by[slug] = rows or have.get(slug) or []
        if ok:
            op["last"] = time.time()
        op["by"] = by
        log("OddsPapi : " + ", ".join(f"{OP_BOOKS[s][1]} {len(r)} matchs" for s, r in by.items())
            + f" ({op['used']}/{OP_MONTHLY} requêtes ce mois, prochain relevé dans {every_h:.0f} h)")
    elif chunks and left < per_refresh:
        log(f"OddsPapi : quota du mois presque atteint ({op.get('used')}/{OP_MONTHLY}), on garde les dernières cotes")
    # on applique les dernières cotes connues de chaque bookmaker à chaque collecte
    n = 0
    for e in events:
        try:
            t = b.parse_time(e["commence_time"])
        except (KeyError, ValueError):
            continue
        real_all = {}
        for slug, (key, title) in OP_BOOKS.items():
            hit = None
            for k in (op.get("by") or {}).get(slug) or []:
                try:
                    kt = datetime.fromisoformat(str(k["start"]).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if abs((kt - t).total_seconds()) <= 20 * 60 and _best_team(e["home_team"], [k["home"]]) and _best_team(e["away_team"], [k["away"]]):
                    hit = k
                    break
            if not hit:
                continue
            mkts = [{"key": "h2h", "outcomes": [{"name": e["home_team"], "price": hit["h"]}, {"name": e["away_team"], "price": hit["a"]},
                                                {"name": "Draw", "price": hit["d"]}]}]
            if hit.get("tot25"):
                mkts.append({"key": "totals", "outcomes": [{"name": "Over", "price": hit["tot25"]["Over"], "point": 2.5},
                                                           {"name": "Under", "price": hit["tot25"]["Under"], "point": 2.5}]})
            # OddsPapi est plus récent que la source principale pour ces bookmakers : il remplace leur entrée
            e["bookmakers"] = [bk for bk in e.get("bookmakers", []) if bk.get("key") != key]
            e["bookmakers"].append({"key": key, "title": title, "markets": mkts, "_ts": hit.get("ts")})
            for lab, pr in (hit.get("extra") or {}).items():
                real_all.setdefault(lab, {})[key] = [pr, title]
            n += 1
        if real_all:
            e["_real_all"] = real_all
    if n:
        log(f"cotes OddsPapi ajoutées : {n} (match x bookmaker)")
    return n



# ---------------------------------------------------------------------------
# Comparateur de cotes public (SportyTrader) : vraies cotes de Betclic, Winamax, Unibet (FDJ) et Bet365, tous les
# types de paris utiles (1N2, double chance, remboursé si nul, plus/moins 0,5 à 4,5, les 2 marquent), lues
# directement sur les pages publiques. Gratuit, sans clé, pour tous les championnats couverts.
ST_BASE = "https://www.sportytrader.com"
ST_LEAGUES = {
    "soccer_france_ligue_one": "football/france/ligue-1-123", "soccer_france_ligue_two": "football/france/ligue-2-124",
    "soccer_epl": "football/angleterre/premier-league-49", "soccer_spain_la_liga": "football/espagne/laliga-108",
    "soccer_italy_serie_a": "football/italie/serie-a-79", "soccer_germany_bundesliga": "football/allemagne/bundesliga-65",
    "soccer_uefa_champs_league": "football/europe/ligue-des-champions-8", "soccer_uefa_europa_league": "football/europe/ligue-europa-481",
    "soccer_uefa_europa_conference_league": "football/europe/europa-conference-league-139206",
    "soccer_netherlands_eredivisie": "football/pays-bas/eredivisie-72", "soccer_portugal_primeira_liga": "football/portugal/primeira-liga-101",
    "soccer_belgium_first_div": "football/belgique/division-1a-32", "soccer_turkey_super_league": "football/turquie/super-lig-121",
}
ST_BOOKS = {"betclic": ("betclic_fr", "Betclic"), "winamax": ("winamax_fr", "Winamax"),
            "unibet": ("unibet_fr", "Unibet (FDJ)"), "bet365": ("bet365", "Bet365")}
ST_MONTHS = {"janv": 1, "févr": 2, "fevr": 2, "mars": 3, "avr": 4, "mai": 5, "juin": 6, "juil": 7, "août": 8, "aout": 8,
             "sept": 9, "oct": 10, "nov": 11, "déc": 12, "dec": 12}
_ST_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
          "Accept-Language": "fr-FR,fr;q=0.9"}


def _st_get(path):
    req = urllib.request.Request(ST_BASE + path, headers=_ST_UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


def _st_compact(x):
    x = re.sub(r"<svg.*?</svg>", "", x, flags=re.S)
    x = re.sub(r'\s(class|width|height|loading|rel|target|style|onclick|x-[\w-]+|@[\w.-]+|:[\w-]+|data-(?!panel-key)[\w-]+)="[^"]*"', "", x)
    return re.sub(r"\s+", " ", x)


def st_list(path):
    """Page d'un championnat -> [(lien, 'Équipe A', 'Équipe B', datetime naïve affichée)]."""
    page = _st_get(f"/cotes/{path}/")
    out, now = [], datetime.now()
    for m in re.finditer(r'<span[^>]*>\s*(\d{1,2}) ([a-zéû]+)\.? - (\d{1,2}):(\d{2})\s*</span>\s*<span[^>]*>\s*<a href="(/cotes/[a-z0-9-]+-\d+/)"[^>]*>\s*([^<]+?)\s*</a>', page):
        d, mon, hh, mm, link, name = m.groups()
        mo = ST_MONTHS.get(mon[:4].rstrip(".")) or ST_MONTHS.get(mon[:3])
        if not mo or " - " not in name:
            continue
        y = now.year + (1 if mo < now.month - 6 else 0)
        h, a = [html_unescape(x.strip()) for x in name.split(" - ", 1)]
        out.append((link, h, a, datetime(y, mo, int(d), int(hh), int(mm))))
    return out


def html_unescape(x):
    import html as _h
    return _h.unescape(x)


def st_match(link):
    """Page d'un match -> {book_key: {'h','d','a','tot25','extra'}} pour les bookmakers suivis."""
    b_ = _st_compact(_st_get(link))
    secs = [(m.start(), m.group(1)) for m in re.finditer(r'\sid="([A-Z_]+)"', b_)]
    res = {}
    heads = list(re.finditer(r"<span> Bookmaker </span>((?: <span>[^<]*</span>)+?) <span> Bonus </span>", b_))
    for i, hm in enumerate(heads):
        sec = next((sname for pos, sname in reversed(secs) if pos < hm.start()), "")
        if sec not in ("RESULT", "UNDER_OVER", "GOAL"):
            continue
        cols = [c.strip() for c in re.findall(r"<span>([^<]*)</span>", hm.group(1))]
        before = b_[max(0, hm.start() - 400):hm.start()]
        names = re.findall(r"<span\s*>([^<]{2,80})</span>", before)
        mname = (names[-1] if names else "").strip().lower()
        pk = re.findall(r'data-panel-key="([A-Z_0-9]+)"', before)
        end = heads[i + 1].start() if i + 1 < len(heads) else len(b_)
        block = b_[hm.end():end]
        kind = None
        if sec == "RESULT" and cols == ["1", "N", "2"] and "vainqueur" in mname and "mi-temps" not in mname:
            kind = "h2h"
        elif sec == "RESULT" and [c.upper() for c in cols] in (["1N", "12", "N2"], ["1X", "12", "X2"]):
            kind = "dc"
        elif sec == "RESULT" and cols == ["1", "2"] and ("rembours" in mname or "nul" in mname):
            kind = "dnb"
        elif sec == "GOAL" and cols == ["Oui", "Non"] and "2 équipes" in mname and "mi-temps" not in mname:
            kind = "btts"
        elif sec == "UNDER_OVER" and len(cols) == 2 and re.fullmatch(r"-\d\.5", cols[0]) and cols[1] == "+" + cols[0][1:] \
                and (not pk or pk[-1].startswith("UNDER_OVER_")):
            kind = "tot"
        if not kind:
            continue
        for row in block.split('<a href="/book/')[1:]:
            bk = row.split("/", 1)[0]
            if bk not in ST_BOOKS:
                continue
            odds = [float(x) for x in re.findall(r"<span\s*>\s*(\d{1,3}\.\d{1,2})\s*</span>", row)][:len(cols)]
            if len(odds) != len(cols) or min(odds) <= 1.0:
                continue
            r = res.setdefault(bk, {"extra": {}, "tot25": None})
            if kind == "h2h":
                r["h"], r["d"], r["a"] = odds
            elif kind == "dc":
                r["extra"].update(dc1N=odds[0], dc12=odds[1], dcN2=odds[2])
            elif kind == "dnb":
                r["extra"].update(dnb1=odds[0], dnb2=odds[1])
            elif kind == "btts":
                r["extra"].update(bttsY=odds[0], bttsN=odds[1])
            else:
                ln = float(cols[1][1:])
                if ln == 2.5:
                    r["tot25"] = {"Under": odds[0], "Over": odds[1]}
                else:
                    r["extra"].update({f"u{ln:g}": odds[0], f"o{ln:g}": odds[1]})
    return {k: v for k, v in res.items() if v.get("h")}


def collect_sportytrader(events, state, max_pages=130):
    """Va lire les cotes publiques de chaque match des 3 prochains jours, ajoute les bookmakers suivis aux matchs
    connus, et crée les matchs des championnats que la source principale ne couvre pas (même moteur, mêmes analyses)."""
    stc = state.setdefault("sportytrader", {})
    cache = stc.setdefault("m", {})
    now = datetime.now(timezone.utc)
    listing, n_pages, errors = [], 0, 0
    for sk, path in ST_LEAGUES.items():
        try:
            for link, h, a, t in st_list(path):
                listing.append((sk, link, h, a, t))
        except Exception as e:
            errors += 1
            log(f"SportyTrader ({b.SPORTS.get(sk, sk)}) :", str(e)[:80])
        time.sleep(0.6)
    # décalage horaire de la page (calculé sur les matchs connus, sinon heure de Paris)
    offs = []
    for sk, link, h, a, t in listing:
        for e in events:
            if e.get("sport_key") == sk and _best_team(e["home_team"], [h]) and _best_team(e["away_team"], [a]):
                d = (b.parse_time(e["commence_time"]).replace(tzinfo=None) - t).total_seconds() / 3600
                if abs(d) <= 14:
                    offs.append(round(d * 4) / 4)
    offs.sort()
    off = offs[len(offs) // 2] if offs else -1.0
    known_ids = {e["id"] for e in events}
    new_events, n_hit = [], 0
    for sk, link, h, a, t in sorted(listing, key=lambda x: x[4]):
        start = (t + timedelta(hours=off)).replace(tzinfo=timezone.utc)
        if not (now - timedelta(hours=2) < start < now + timedelta(hours=72)):
            continue
        c = cache.get(link)
        if (not c or time.time() - c.get("ts", 0) > 2.5 * 3600) and n_pages < max_pages:
            try:
                c = cache[link] = {"ts": time.time(), "books": st_match(link)}
                n_pages += 1
            except Exception as e:
                errors += 1
                if errors <= 5:
                    log("SportyTrader (match) :", str(e)[:80])
            time.sleep(0.8)
        if not c or not c.get("books"):
            continue
        ev = next((e for e in events if e.get("sport_key") == sk and abs((b.parse_time(e["commence_time"]) - start).total_seconds()) <= 6 * 3600
                   and _best_team(e["home_team"], [h]) and _best_team(e["away_team"], [a])), None)
        if ev is None:
            ev = next((e for e in new_events if e["id"] == "st" + link.rstrip("/").rsplit("-", 1)[-1]), None)
        if ev is None:
            eid = "st" + link.rstrip("/").rsplit("-", 1)[-1]
            if eid in known_ids:
                continue
            ev = {"id": eid, "sport_key": sk, "sport_title": b.SPORTS.get(sk, sk), "home_team": h, "away_team": a,
                  "commence_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "bookmakers": [], "_source": "sportytrader"}
            new_events.append(ev)
        real_all = dict(ev.get("_real_all") or {})
        for bk, hit in c["books"].items():
            key, title = ST_BOOKS[bk]
            mkts = [{"key": "h2h", "outcomes": [{"name": ev["home_team"], "price": hit["h"]}, {"name": ev["away_team"], "price": hit["a"]},
                                                {"name": "Draw", "price": hit["d"]}]}]
            if hit.get("tot25") and len(hit["tot25"]) == 2:
                mkts.append({"key": "totals", "outcomes": [{"name": "Over", "price": hit["tot25"]["Over"], "point": 2.5},
                                                           {"name": "Under", "price": hit["tot25"]["Under"], "point": 2.5}]})
            ev["bookmakers"] = [x for x in ev.get("bookmakers", []) if x.get("key") != key]
            ev["bookmakers"].append({"key": key, "title": title, "markets": mkts, "_ts": c["ts"],
                                     "last_update": datetime.fromtimestamp(c["ts"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
            for lab, pr in hit["extra"].items():
                real_all.setdefault(lab, {})[key] = [pr, title]
            n_hit += 1
        if real_all:
            ev["_real_all"] = real_all
    live = {x[1] for x in listing}
    stc["m"] = {k: v for k, v in cache.items() if k in live}
    events.extend(new_events)
    log(f"SportyTrader : {len(listing)} matchs listés, {n_pages} pages lues, {n_hit} cotes (match x bookmaker), "
        f"{len(new_events)} matchs ajoutés hors source principale, décalage horaire {off:+.2f} h, {errors} erreur(s)")
    return n_hit, new_events


def self_test(snap):
    """Auto-test : fait tourner le vrai moteur sur les vraies données (comme l'appli d'un abonné Pro) et écrit
    dans le journal ce qu'un client verrait, bookmaker par bookmaker."""
    import tempfile
    tmp = tempfile.mkdtemp()
    old_dir = b.config_dir
    b.config_dir = lambda: tmp
    try:
        api = b.Api(start_loop=False)
        api.load_snapshot(json.loads(json.dumps(tier_snapshot(snap, "pro"))))
        for books in (["betclic_fr", "winamax_fr", "parionssport_fr", "unibet_fr", "bet365"], ["betclic_fr"], ["winamax_fr"],
                      ["parionssport_fr", "unibet_fr"], ["bet365"]):
            api.set_books(books)
            real = sum(1 for x in api._sel if not x.get("estimated"))
            res = []
            for tgt in (15, 40, 200):
                r = api.compute_objective(10, tgt)
                res.append(f"{tgt} € : {len(r.get('options') or [])}")
            log(f"AUTO-TEST {'+'.join(books)} : {real} paris à vraie cote ; tickets (10 € ->) " + ", ".join(res))
        api.set_books(["betclic_fr", "winamax_fr", "parionssport_fr", "unibet_fr", "bet365"])
        r = api.compute_objective(10, 40)
        for o in (r.get("options") or [])[:2]:
            t = o["tickets"][0]
            log(f"   #{r['options'].index(o) + 1} {round(o['p_hit'] * 100, 1)} % : " + " | ".join(
                f"{l['selection']} @{l['odds']} ({round(l['p'] * 100)} %, {l.get('book')}) [{l.get('form_h') or '?'} / {l.get('form_a') or '?'}]"
                for l in t["legs"]))
    finally:
        b.config_dir = old_dir


def public_record(events, st, state=None):
    """Bilan public : on enregistre chaque pari que Cotescope « jouerait », puis on le vérifie
    avec le résultat officiel. Le résumé est publié sans connexion (preuve pour la vitrine)."""
    from history import History
    folder = os.path.join(CACHE, "public")
    os.makedirs(folder, exist_ok=True)
    h = History(folder)
    cfg = dict(b.DEFAULT_CONFIG, sports=SPORTS, bookmakers="betclic_fr,winamax_fr,unibet_fr,parionssport_fr,bet365")
    sel = b.analyze(events, cfg, info=st.info_for(events))
    picks = [x for x in b.top_picks(sel, cfg) if b.would_bet(x, cfg)]
    # + la « sélection du jour » : les 3 paris les plus sûrs (vraies cotes) des matchs des 30 prochaines heures
    soon = datetime.now(timezone.utc) + timedelta(hours=30)
    safe = [x for x in b.most_probable(sel, cfg) if not x.get("estimated") and x["start"] <= soon and x["p"] >= 0.6]
    picks += safe[:3]
    if state is not None:
        try:
            n_al = queue_alerts(sel, cfg, state)
            log("alertes Telegram préparées :", n_al)
        except Exception as e:
            log("alertes :", e)
    h.log_app(picks)
    h.settle_all(S.ESPN_SLUGS)
    summ = h.summary()["app"]
    done = [e for e in h.data["app"] if e["status"] in ("won", "lost")]
    done.sort(key=lambda e: e.get("start", ""), reverse=True)
    pub = {
        "built": time.time(), "n": summ["n"], "wins": summ["wins"], "rate": summ["rate"], "roi": summ["roi"],
        "pending": summ["pending"], "buckets": summ["buckets"],
        "last": [{k: e.get(k) for k in ("selection", "match", "odds", "p", "status", "start")} for e in done[:30]],
        "upcoming": [{k: e.get(k) for k in ("selection", "match", "odds", "p", "start")}
                     for e in h.data["app"] if e["status"] == "pending"][:12],
    }
    log(f"bilan public : {summ['n']} paris vérifiés, {summ['pending']} en attente")
    cf_put("public_stats", pub)
    try:
        n_new = write_ledger(h)
        log("registre public certifié :", n_new, "nouveau(x) pronostic(s)")
    except Exception as e:
        log("registre public :", e)


def queue_alerts(sel, cfg, state):
    """Alertes Elite envoyées sur Telegram par le serveur : paris value des prochaines 24 h et cotes Pinnacle
    qui chutent (l'argent des parieurs pros arrive). Chaque alerte n'est envoyée qu'une fois."""
    now = datetime.now(timezone.utc)
    hist = state.setdefault("pin_hist", {})
    sent = set(state.get("alerts_sent", []))
    out = []
    fr = lambda x: f"{x:.2f}".replace(".", ",")
    for x in sel:
        if x.get("estimated") or x.get("suspect") or not (now < x["start"] < now + timedelta(hours=48)):
            continue
        pin = (x.get("all_prices") or {}).get("pinnacle")
        if pin:
            h = hist.setdefault(x["uid"], {"f": pin[0], "t": now.timestamp()})
            drop = (pin[0] - h["f"]) / h["f"]
            aid = "drop:" + x["uid"]
            if drop <= -0.10 and aid not in sent:
                out.append({"id": aid, "ev": -drop, "text":
                    f"📉 <b>Cote qui chute</b> · {x['match']}\n{x['selection']} : Pinnacle passe de {fr(h['f'])} à {fr(pin[0])} ({drop * 100:.0f} %). "
                    f"L'argent des parieurs pros arrive sur ce pari.\nMeilleure cote chez toi : {fr(x['odds'])} ({x['book']}) · nos chances : {x['p'] * 100:.0f} %"})
        aid = "value:" + x["uid"]
        if b.is_value(x, cfg) and x["p"] >= 0.3 and x["start"] < now + timedelta(hours=24) and aid not in sent:
            out.append({"id": aid, "ev": x["ev"], "text":
                f"💎 <b>Pari value</b> · {x['match']}\n{x['selection']} à {fr(x['odds'])} chez {x['book']}. "
                f"Nos chances : {x['p'] * 100:.0f} %, juste prix {fr(1 / x['p'])} (+{x['ev'] * 100:.0f} % sur la durée)."})
    out.sort(key=lambda a: -a["ev"])
    out = out[:6]
    for a in out:
        sent.add(a["id"])
    state["alerts_sent"] = list(sent)[-800:]
    keep = {u for u in hist if any(u == x["uid"] for x in sel)}
    state["pin_hist"] = {u: hist[u] for u in keep}
    if out:
        cf_put("alerts_out", {"built": time.time(), "items": [{"id": a["id"], "text": a["text"]} for a in out]}, gz=False)
    return len(out)


LEDGER_URL = "https://raw.githubusercontent.com/LogicArt19/cotescope/main/bilan/registre.json"


def write_ledger(h):
    """Registre public inaltérable : chaque pronostic est ajouté AVANT le match avec une empreinte SHA-256
    chaînée à la précédente. Le fichier est publié dans le dépôt GitHub public du site : l'historique de
    GitHub date chaque ajout, et toute modification d'une ligne passée casserait la chaîne d'empreintes."""
    import hashlib
    path = os.path.join(CACHE, "public", "registre.json")
    led = None
    if os.path.exists(path):
        led = json.load(open(path, encoding="utf-8"))
    else:
        try:  # la source de vérité est le dépôt public
            with urllib.request.urlopen(LEDGER_URL, timeout=20) as r:
                led = json.loads(r.read().decode())
        except Exception:
            led = None
    led = led or {"about": "Registre public des pronostics Cotescope. Chaque ligne est ajoutée avant le coup d'envoi ; "
                           "hash = SHA-256(hash précédent + champs du pronostic). Résultats vérifiés automatiquement.",
                  "entries": []}
    E = led["entries"]
    known = {e["uid"]: e for e in E}
    fields = ("uid", "recorded", "match", "selection", "odds", "p", "start")
    now = datetime.now(timezone.utc)
    n_new = 0
    for a in sorted(h.data["app"], key=lambda x: x.get("created", 0)):
        e = known.get(a["uid"])
        if e is None:
            try:
                st_ = datetime.fromisoformat(a["start"])
                if st_.tzinfo is None:
                    st_ = st_.replace(tzinfo=timezone.utc)
            except (KeyError, ValueError, TypeError):
                continue
            if st_ <= now:  # jamais d'ajout après le coup d'envoi
                continue
            e = {"n": len(E) + 1, "uid": a["uid"], "recorded": now.isoformat(timespec="seconds"),
                 "match": a.get("match"), "selection": a.get("selection"), "odds": a.get("odds"),
                 "p": a.get("p"), "start": a.get("start")}
            prev = E[-1]["hash"] if E else "0" * 64
            e["hash"] = hashlib.sha256((prev + json.dumps({k: e[k] for k in fields}, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()
            E.append(e)
            known[e["uid"]] = e
            n_new += 1
        # le résultat est ajouté ensuite (hors empreinte : il se vérifie avec le score officiel)
        if a["status"] in ("won", "lost", "void") and e.get("status") != a["status"]:
            e["status"], e["score"] = a["status"], a.get("score")
    done = [e for e in E if e.get("status") in ("won", "lost")]
    prof = sum((e["odds"] - 1) if e["status"] == "won" else -1 for e in done)
    led["summary"] = {"updated": now.isoformat(timespec="seconds"), "n": len(done),
                      "wins": sum(e["status"] == "won" for e in done),
                      "roi": round(prof / len(done), 4) if done else None, "pending": sum(1 for e in E if "status" not in e)}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(led, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    # copie pour l'étape de publication du workflow
    json.dump(led, open(os.path.join(HERE, "public_ledger.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return n_new


import unicodedata as _ud

_TEAM_ALIAS = {"paris sg": "paris saint germain", "psg": "paris saint germain", "nottm forest": "nottingham forest",
               "wolves": "wolverhampton wanderers", "spurs": "tottenham", "athletic madrid": "atletico madrid",
               "athletic club": "athletic bilbao", "inter": "inter milan", "milan": "milan", "m gladbach": "borussia monchengladbach",
               "ein frankfurt": "eintracht frankfurt", "dortmund": "borussia dortmund", "leverkusen": "bayer leverkusen",
               "newcastle": "newcastle united", "brighton": "brighton and hove albion", "west ham": "west ham united",
               "espanol": "espanyol", "vallecano": "rayo vallecano", "alaves": "alaves", "celta": "celta vigo",
               "sociedad": "sociedad", "betis": "betis", "st etienne": "saint etienne"}


def _norm_team(t):
    t = _ud.normalize("NFD", str(t or "")).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"\b(fc|cf|ac|afc|sc|as|ss|ssc|us|rc|sv|vfb|vfl|tsg|ogc|stade|club|de|real|the|calcio|cd|ud|sd|rcd|ca)\b", " ", t)
    t = re.sub(r"\bman\b", "manchester", t)
    t = re.sub(r"\butd\b", "united", t)
    t = re.sub(r"\bst\b", "saint", t)
    t = re.sub(r"\bath\b", "athletic", t)
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return _norm_team(_TEAM_ALIAS[t]) if t in _TEAM_ALIAS and _TEAM_ALIAS[t] != t else t


def _team_score(name, cand):
    """0..1 : ressemblance entre deux noms d'équipe (« FSV Mainz 05 » / « Mainz » = 0,9)."""
    n0, n1 = _norm_team(name), _norm_team(cand)
    if not n0 or not n1:
        return 0.0
    if n0 == n1:
        return 1.0
    A, B = n0.split(), n1.split()
    hit = sum(1 for w in B if w in A or any(len(x) > 3 and len(w) > 3 and (x.startswith(w) or w.startswith(x)) for x in A))
    if not hit:
        return 0.0
    return hit / len(B) * (0.85 + 0.15 * min(1.0, hit / len(A)))


def _best_team(name, candidates, need=0.6):
    best, bs = None, 0.0
    for c in candidates:
        sc = _team_score(name, c)
        if sc > bs:
            best, bs = c, sc
    return best if bs >= need else None


def publish_lineup_watch(events, st):
    """Liste légère des matchs des 36 prochaines heures (identifiant ESPN + onze attendu) : le serveur
    Cloudflare la lit chaque minute pour récupérer les compos officielles dès leur publication."""
    now = datetime.now(timezone.utc)
    board = st.data.get("web_board") or {}
    web = st.data.get("web") or {}
    out = []
    for ev in events:
        try:
            t = b.parse_time(ev["commence_time"])
        except (KeyError, ValueError):
            continue
        if not (now - timedelta(hours=1) < t < now + timedelta(hours=36)):
            continue
        slug = S.ESPN_SLUGS.get(ev.get("sport_key"))
        c = board.get(slug) if slug else None
        if not c:
            continue
        m = st._match_one(ev, c["list"])
        if not m:
            continue
        w = (web.get(ev["id"]) or {}).get("v") or {}
        exp = {}
        for ro in (w.get("last_xi") or []):
            exp[ro.get("side")] = [p.get("full") or p.get("n") for p in ro.get("xi") or []]
        out.append({"eid": ev["id"], "espn": m["id"], "slug": slug, "start": t.isoformat(),
                    "home": ev.get("home_team"), "away": ev.get("away_team"), "expected": exp})
    cf_put("lineup_watch", {"built": time.time(), "list": out}, gz=False)
    return len(out)


def fd_enrich(w, rows, hn, an, code, ev):
    """Complète la fiche avec les données gratuites de football-data.co.uk quand ESPN ne les donne pas :
    modèle de buts de la saison (forces d'attaque et de défense), 5 derniers matchs, classement, enjeu."""
    import model as M
    import stats as SS
    today = datetime.now(timezone.utc).date()
    season_start = datetime(today.year if today.month >= 7 else today.year - 1, 7, 1).date()
    # 1) modèle de buts sur ~1 an de matchs (les plus récents comptent plus)
    if not w.get("model"):
        res = [{"date": r["d"].isoformat() + "T12:00:00+00:00", "home_id": r["h"], "away_id": r["a"], "gh": r["hg"], "ga": r["ag"]}
               for r in rows if (today - r["d"]).days <= 400]
        rt = M.fit_ratings(res)
        pr = M.predict(rt, hn, an) if rt else None
        if pr:
            w["model"] = pr
    for side, t in (("home", hn), ("away", an)):
        fd_enrich_side(w, side, rows, t, code, ev, table=False)
    # 3) classement de la saison en cours, et enjeu
    cur = [r for r in rows if r["d"] >= season_start]
    if cur:
        tab = {}
        for r in cur:
            for t, gf, ga in ((r["h"], r["hg"], r["ag"]), (r["a"], r["ag"], r["hg"])):
                x = tab.setdefault(t, [0, 0, 0, 0])
                x[0] += 3 if gf > ga else (1 if gf == ga else 0)
                x[1] += gf
                x[2] += ga
                x[3] += 1
        order = sorted(tab.items(), key=lambda kv: (-kv[1][0], -(kv[1][1] - kv[1][2]), -kv[1][1]))
        table_all = [{"tid": t, "name": t, "rank": i + 1, "pts": v[0], "played": v[3]} for i, (t, v) in enumerate(order)]
        rank = {t["tid"]: t for t in table_all}
        tb = w.setdefault("table", {})
        for side, t in (("home", hn), ("away", an)):
            if not tb.get(side) and t in rank:
                tb[side] = {"rank": rank[t]["rank"], "points": rank[t]["pts"]}
        if not w.get("stakes"):
            slug = {"F1": "fra.1", "E0": "eng.1", "SP1": "esp.1", "I1": "ita.1", "D1": "ger.1", "N1": "ned.1", "P1": "por.1"}.get(code, "")
            w["stakes"] = SS.season_stakes(table_all, {"home": hn, "away": an}, slug)


def _fd_table(rows):
    today = datetime.now(timezone.utc).date()
    season_start = datetime(today.year if today.month >= 7 else today.year - 1, 7, 1).date()
    tab = {}
    for r in rows:
        if r["d"] < season_start:
            continue
        for t, gf, ga in ((r["h"], r["hg"], r["ag"]), (r["a"], r["ag"], r["hg"])):
            x = tab.setdefault(t, [0, 0, 0, 0])
            x[0] += 3 if gf > ga else (1 if gf == ga else 0)
            x[1] += gf
            x[2] += ga
            x[3] += 1
    order = sorted(tab.items(), key=lambda kv: (-kv[1][0], -(kv[1][1] - kv[1][2]), -kv[1][1]))
    return [{"tid": t, "name": t, "rank": i + 1, "pts": v[0], "played": v[3]} for i, (t, v) in enumerate(order)]


def fd_enrich_side(w, side, rows, t, code, ev, table=True):
    """Forme (5 derniers matchs de championnat) et classement d'UNE équipe, depuis son propre championnat."""
    import sys as _s
    _s.path.insert(0, os.path.join(HERE, "ci"))
    import history_data as H
    lname = H.LEAGUES.get(code, (code,))[0]
    form = w.setdefault("form", {})
    if not form.get(side):
        g = [r for r in rows if t in (r["h"], r["a"])][-8:]
        games = []
        for r in g:
            home = r["h"] == t
            gf, ga = (r["hg"], r["ag"]) if home else (r["ag"], r["hg"])
            games.append({"res": "V" if gf > ga else ("N" if gf == ga else "D"), "gf": gf, "ga": ga,
                          "opp": r["a"] if home else r["h"], "home": home, "date": r["d"].isoformat(), "comp": lname})
        if games:
            form[side] = list(reversed(games))
    if table:
        tb = w.setdefault("table", {})
        if not tb.get(side):
            row = next((x for x in _fd_table(rows) if x["tid"] == t), None)
            if row:
                tb[side] = {"rank": row["rank"], "points": row["pts"], "league": lname}


def cards_profile(rows, hn, an, referee):
    """Cartons : moyenne des 2 équipes (pris et provoqués chez l'adversaire) sur leurs 20 derniers matchs,
    l'arbitre désigné (sa moyenne quand la source le donne), et les chances de dépasser 3,5 / 4,5 / 5,5 cartons
    (jaune = 1, rouge = 2, comme chez la plupart des bookmakers)."""
    import math
    valid = [r for r in rows if r.get("hy") is not None and r.get("ay") is not None]
    if len(valid) < 50:
        return None
    pts = lambda r, home: (r["hy"] + 2 * r["hr"]) if home else (r["ay"] + 2 * r["ar"])
    league = sum(pts(r, True) + pts(r, False) for r in valid[-380:]) / len(valid[-380:])

    def team(t):
        g = [r for r in valid if t in (r["h"], r["a"])][-20:]
        if len(g) < 5:
            return None
        own = sum(pts(r, r["h"] == t) for r in g) / len(g)
        opp = sum(pts(r, r["h"] != t) for r in g) / len(g)
        fouls = [r["hf"] if r["h"] == t else r["af"] for r in g if (r["hf"] if r["h"] == t else r["af"]) is not None]
        return {"own": round(own, 2), "opp": round(opp, 2), "fouls": round(sum(fouls) / len(fouls), 1) if fouls else None, "n": len(g)}

    th, ta = team(hn), team(an)
    if not th or not ta:
        return None
    exp = (th["own"] + ta["opp"]) / 2 + (ta["own"] + th["opp"]) / 2
    ref = None
    if referee:
        last = referee.split()[-1].lower()
        rg = [r for r in valid if r.get("ref") and r["ref"].split()[-1].lower() == last][-40:]
        if len(rg) >= 8:
            avg = sum(pts(r, True) + pts(r, False) for r in rg) / len(rg)
            ref = {"name": referee, "avg": round(avg, 2), "n": len(rg)}
            exp *= max(0.75, min(1.3, avg / league))  # un arbitre sévère fait monter l'espérance, sans excès
    pois_over = lambda k: 1 - sum(math.exp(-exp) * exp ** i / math.factorial(i) for i in range(int(k) + 1))
    return {"home": th, "away": ta, "league_avg": round(league, 2), "expected": round(exp, 2),
            "referee": ref or ({"name": referee, "avg": None, "n": 0} if referee else None),
            "over": {str(k + .5): round(pois_over(k), 3) for k in (3, 4, 5)}}


def add_fd_h2h(events, st):
    """Complète chaque match avec football-data.co.uk (gratuit, sans clé) : face-à-face sur toutes les divisions,
    forme, classement, modèle de buts et cartons. Marche aussi pour les coupes (chaque équipe est cherchée dans
    son propre championnat parmi les 16 couverts)."""
    sys.path.insert(0, os.path.join(HERE, "ci"))
    import history_data as H
    code_of = {v[1]: k for k, v in H.LEAGUES.items()}
    cdir = os.path.join(CACHE, "fd_csv")
    os.makedirs(cdir, exist_ok=True)
    seas = H.seasons()[-5:]
    cache = {}

    def load(code):
        if code not in cache:
            rows = []
            for se in seas:
                fp = os.path.join(cdir, f"{se}_{code}.csv")
                fresh = os.path.exists(fp) and (se != seas[-1] or time.time() - os.path.getmtime(fp) < 2 * 86400)
                if not fresh:
                    raw = H.fetch(code, se)
                    if raw:
                        open(fp, "wb").write(raw)
                if os.path.exists(fp):
                    rows += H.read_rows(open(fp, "rb").read())
            rows.sort(key=lambda r: r["d"])
            cache[code] = rows
        return cache[code]

    def teams_of(code):
        rows = load(code)
        return {r["h"] for r in rows[-500:]} | {r["a"] for r in rows[-500:]}

    def resolve(name, pref):
        # d'abord le championnat du match, sinon les 16 championnats couverts
        if pref:
            t = _best_team(name, teams_of(pref))
            if t:
                return pref, t
        best, bc, bs = None, None, 0.0
        for c in H.LEAGUES:
            for t in teams_of(c):
                sc = _team_score(name, t)
                if sc > bs:
                    best, bc, bs = t, c, sc
        return (bc, best) if bs >= 0.75 else (None, None)

    web = st.data.get("web") or {}
    n = 0
    for ev in events:
        eid, pref = ev.get("id"), code_of.get(ev.get("sport_key"))
        if eid not in web or not (web[eid] or {}).get("v"):
            # pas de fiche ESPN : on en crée une avec les données gratuites de football-data.co.uk
            web[eid] = {"ts": 0, "v": {"inj_home": [], "inj_away": [], "lineups": [], "news": [], "form": {}, "h2h": [],
                                       "table": {}, "fmt": 0, "src": "football-data.co.uk"}}
            st.data["web"] = web
        w = web[eid]["v"]
        ch, hn = resolve(ev.get("home_team"), pref)
        ca, an = resolve(ev.get("away_team"), pref)
        if not hn and not an:
            continue
        try:
            if ch and ch == ca and hn != an:   # même championnat : tout, y compris le modèle de buts et les cartons
                rows = load(ch)
                w["cards"] = cards_profile(rows, hn, an, w.get("referee") or "") or w.get("cards")
                fd_enrich(w, rows, hn, an, ch, ev)
            else:                              # coupe ou divisions différentes : forme et classement de chacun
                for side, c, t in (("home", ch, hn), ("away", ca, an)):
                    if c and t:
                        fd_enrich_side(w, side, load(c), t, c, ev)
        except Exception as e:
            log("enrichissement football-data :", e)
        if hn and an and len(w.get("h2h") or []) < 5:
            pool = []
            for c in {x for x in (ch, ca) if x}:
                pool += load(c)
            seen, meet = set(), []
            for r in sorted(pool, key=lambda r: r["d"], reverse=True):
                if {r["h"], r["a"]} == {hn, an} and (r["d"], r["h"]) not in seen:
                    seen.add((r["d"], r["h"]))
                    meet.append(r)
            if meet:
                w["h2h"] = [{"date": r["d"].isoformat(), "home": ev.get("home_team") if r["h"] == hn else ev.get("away_team"),
                             "away": ev.get("away_team") if r["h"] == hn else ev.get("home_team"),
                             "gh": r["hg"], "ga": r["ag"], "comp": "championnat"} for r in meet[:5]]
        n += 1
    if n:
        st._save()
    # rapport de couverture : ce qui manque encore, match par match (pour corriger les noms d'équipes)
    miss = []
    for ev in events:
        w = (web.get(ev.get("id")) or {}).get("v") or {}
        lacks = [k for k, ok in (("forme", (w.get("form") or {}).get("home") and (w.get("form") or {}).get("away")),
                                 ("modèle", w.get("model")), ("classement", (w.get("table") or {}).get("home")),
                                 ("face-à-face", w.get("h2h"))) if not ok]
        if lacks:
            miss.append(f"{ev.get('home_team')} - {ev.get('away_team')} ({', '.join(lacks)})")
    log(f"couverture : {len(events) - len(miss)}/{len(events)} matchs complets")
    for m in miss[:25]:
        log("   manque :", m)
    return n


# ---------------------------------------------------------------------------
# Veille web automatique : pour chaque match des 48 h, on cherche les articles récents (Google Actualités,
# flux RSS public et gratuit) et on repère les mots qui comptent pour un pari : blessé, forfait, suspendu…
# Ce n'est pas une IA payante : c'est une recherche automatique, triée et résumée par des règles simples.
NEWS_KEYS = {
    "absence": ("bless", "forfait", "absent", "incertain", "touché", "indisponible", "out ", "injur", "doubt", "ruled out",
                "lesion", "lesión", "baja", "infortun", "verletz", "fehlt", "ausfall", "blessure"),
    "suspension": ("suspen", "carton rouge", "exclu", "squalific", "sancion", "gesperrt", "red card", "ban "),
    "coach": ("entraîneur", "limog", "démission", "sacked", "nouvel entraîneur", "interim", "intérim", "esonero", "destitu"),
    "lineup": ("compo", "titulaire", "onze de départ", "lineup", "line-up", "starting xi", "rotation", "groupe convoqué", "convoqu"),
}
NEWS_LABEL = {"absence": "Blessure / absence", "suspension": "Suspension", "coach": "Banc / entraîneur", "lineup": "Compo / groupe"}


def _gnews(q, lang):
    hl, gl = {"fr": ("fr", "FR"), "en": ("en-GB", "GB"), "es": ("es", "ES"), "it": ("it", "IT"), "de": ("de", "DE"), "pt": ("pt-PT", "PT")}.get(lang, ("fr", "FR"))
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q + " when:4d") +
           f"&hl={hl}&gl={gl}&ceid={gl}:{hl.split('-')[0]}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; CotescopeBot/1.0; +https://cotescope.fr)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
    import xml.etree.ElementTree as ET
    out = []
    for it in ET.fromstring(raw).iter("item"):
        title = (it.findtext("title") or "").strip()
        src = it.find("source")
        source = (src.text or "").strip() if src is not None else ""
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        pub = it.findtext("pubDate") or ""
        try:
            ts = datetime.strptime(pub[:25], "%a, %d %b %Y %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            ts = 0
        out.append({"title": title, "url": (it.findtext("link") or "").strip(), "source": source, "ts": ts})
    return out


LEAGUE_LANG = {"soccer_france": "fr", "soccer_spain": "es", "soccer_italy": "it", "soccer_germany": "de",
               "soccer_portugal": "pt", "soccer_epl": "en", "soccer_efl": "en", "soccer_netherlands": "en"}


def add_web_news(events, st, state, max_matches=70):
    """Veille automatique par match (cache 3 h) : titres, source, lien, et ce que l'article signale."""
    now = datetime.now(timezone.utc)
    cache = state.setdefault("gnews", {})
    web = st.data.setdefault("web", {})
    soon = [e for e in events if now - timedelta(hours=2) < b.parse_time(e["commence_time"]) < now + timedelta(hours=48)]
    soon.sort(key=lambda e: b.parse_time(e["commence_time"]))
    n_req = n_items = n_flag = 0
    for e in soon[:max_matches]:
        eid, h, a = e["id"], e.get("home_team", ""), e.get("away_team", "")
        c = cache.get(eid)
        if not c or now.timestamp() - c.get("ts", 0) > 3 * 3600:
            items = []
            langs = ["fr"] + ([LEAGUE_LANG.get(next((k for k in LEAGUE_LANG if e.get("sport_key", "").startswith(k)), ""), "en")])
            for lang in dict.fromkeys(langs):
                try:
                    items += _gnews(f'"{h}" "{a}"', lang)
                    n_req += 1
                except Exception:
                    pass
                time.sleep(0.4)
            seen, keep = set(), []
            for it in sorted(items, key=lambda x: -x["ts"]):
                k = re.sub(r"\W+", "", it["title"].lower())[:60]
                if k in seen or not it["title"] or now.timestamp() - it["ts"] > 5 * 86400:
                    continue
                seen.add(k)
                low = " " + it["title"].lower() + " "
                it["tags"] = [NEWS_LABEL[t] for t, ws in NEWS_KEYS.items() if any(w in low for w in ws)]
                keep.append(it)
            keep.sort(key=lambda x: (-len(x["tags"]), -x["ts"]))
            c = cache[eid] = {"ts": now.timestamp(), "items": keep[:8]}
        if not c["items"]:
            continue
        w = web.setdefault(eid, {"ts": 0, "v": {"inj_home": [], "inj_away": [], "lineups": [], "news": [], "form": {}, "h2h": []}})
        v = w.setdefault("v", {})
        v["gnews"] = [{"title": it["title"], "url": it["url"], "source": it["source"], "date": datetime.fromtimestamp(it["ts"], timezone.utc).strftime("%Y-%m-%dT%H:%MZ") if it["ts"] else "",
                       "tags": it.get("tags", [])} for it in c["items"]]
        n_items += len(c["items"])
        n_flag += sum(1 for it in c["items"] if it.get("tags"))
    live = {e["id"] for e in events}
    state["gnews"] = {k: v for k, v in cache.items() if k in live}
    return n_req, n_items, n_flag


def main():
    state = load_state()
    cfg = dict(b.DEFAULT_CONFIG, api_key=os.environ.get("ODDS_KEY", ""), sports=SPORTS, demo=False)
    if not cfg["api_key"]:
        log("ODDS_KEY manquante")
        sys.exit(1)

    # 1) cotes : collecte intelligente pour rester dans le quota gratuit
    events = collect_odds(state, cfg)
    now = datetime.now(timezone.utc)
    events = [e for e in events if b.parse_time(e["commence_time"]) > now - timedelta(hours=3)]
    state["events"] = events

    # 2) stats + web (le module garde son propre cache sur disque)
    st = S.Stats(CACHE)
    fd = os.environ.get("FD_KEY", "")
    if fd:
        log("football-data.org…")
        st.update_fd(fd, SPORTS, events)
        if st.last_error:
            log("  ", st.last_error)
    log("ESPN (absents, compos, forme, météo)…")
    st.update_web(SPORTS, events)
    if getattr(st, "web_error", ""):
        log("  ", st.web_error)

    try:
        add_espn_odds(events, st)
    except Exception as e:
        log("cotes ESPN :", e)
    if False:  # diagnostic de clé : plus nécessaire (clé validée)
        try:
            oddspapi_diag()
        except Exception as e:
            log("OddsPapi diag :", e)
    try:
        collect_oddspapi(events, state)
    except Exception as e:
        log("OddsPapi :", e)
    try:  # en dernier : ses cotes sont les plus récentes et couvrent Betclic
        _, added = collect_sportytrader(events, state)
        if added:
            st.update_web(sorted({e["sport_key"] for e in added}), added)
    except Exception as e:
        log("SportyTrader :", e)
    try:
        n = publish_lineup_watch(events, st)
        log("surveillance des compos :", n, "matchs dans les 36 h")
    except Exception as e:
        log("surveillance des compos :", e)
    try:
        n = add_fd_h2h(events, st)
        log("face-à-face (football-data.co.uk) :", n, "matchs complétés")
    except Exception as e:
        log("face-à-face :", e)

    try:
        r = add_web_news(events, st, state)
        log("veille web (Google Actualités) : %d recherches, %d articles, %d signalent une absence / suspension / compo" % r)
    except Exception as e:
        log("veille web :", e)

    # 3) scores pour l'historique
    state["scores"] = collect_scores(state, sorted({S.ESPN_SLUGS[s] for s in SPORTS if s in S.ESPN_SLUGS}))
    save_state(state)

    keep = ("fixtures", "results", "standings", "scorers", "h2h", "web", "inj_day", "pred", "lineups")
    snap = {
        "ts": state.get("odds_ts", 0), "built": time.time(), "sports": sorted(set(SPORTS) | {e["sport_key"] for e in events}),
        "events": events, "quota": state.get("quota"), "stats": {k: st.data.get(k, {}) for k in keep},
        "scores": state["scores"], "app_version": b.APP_VERSION,
    }
    with open(os.path.join(CACHE, "snapshot.json"), "w", encoding="utf-8") as f:
        json.dump(snap, f)
    log(f"snapshot : {len(events)} matchs, {len(snap['stats'].get('web', {}))} fiches web")
    for tier in TIERS:
        cf_put("snapshot_" + tier, tier_snapshot(snap, tier))
    try:
        self_test(snap)
    except Exception as e:
        import traceback
        log("AUTO-TEST : plantage", e, traceback.format_exc()[-600:])

    try:
        public_record(events, st, state)
    except Exception as e:  # le bilan public ne doit jamais bloquer la collecte
        log("bilan public :", e)

    engine = {}
    for name in ENGINE_FILES:
        with open(os.path.join(HERE, name), encoding="utf-8") as f:
            engine[name] = f.read()
    import hashlib
    h = hashlib.sha256(json.dumps(engine, sort_keys=True).encode()).hexdigest()[:16]
    engine_pkg = {"version": b.APP_VERSION, "hash": h, "files": engine}
    if state.get("engine_hash") != h or "--engine" in sys.argv:
        if cf_put("engine", engine_pkg):
            state["engine_hash"] = h
            save_state(state)


if __name__ == "__main__":
    main()
