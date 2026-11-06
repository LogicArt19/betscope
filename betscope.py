# -*- coding: utf-8 -*-
"""
BetScope - Analyseur de paris sportifs (value bets, combinés, gestion de mise)

Principe :
  1. Récupère les cotes de dizaines de bookmakers (The Odds API).
  2. Retire la marge de chaque bookmaker et fait le consensus du marché
     (les bookmakers "sharp" comme Pinnacle pèsent plus lourd)
     -> on obtient la VRAIE probabilité estimée de chaque issue.
  3. Compare à la meilleure cote dispo chez TES bookmakers.
     Si cote x probabilité > 1  -> pari "value" (rentable sur la durée).
  4. Construit les combinés (au même bookmaker, matchs différents).
  5. Répartit ton budget avec le critère de Kelly fractionné
     et simule 20 000 fois le résultat.

Aucun logiciel ne peut garantir un gain : un value bet perd souvent,
il est simplement rentable en moyenne sur un grand nombre de paris.
"""

import itertools
import json
import os
import queue
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

APP_NAME = "BetScope"
APP_VERSION = "1.0.3"
API_BASE = "https://api.the-odds-api.com/v4"

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

SPORTS = {  # 100 % foot
    "soccer_france_ligue_one": "Ligue 1",
    "soccer_france_ligue_two": "Ligue 2",
    "soccer_epl": "Premier League",
    "soccer_spain_la_liga": "Liga",
    "soccer_italy_serie_a": "Serie A",
    "soccer_germany_bundesliga": "Bundesliga",
    "soccer_uefa_champs_league": "Ligue des champions",
    "soccer_uefa_europa_league": "Ligue Europa",
    "soccer_uefa_europa_conference_league": "Conference League",
    "soccer_netherlands_eredivisie": "Eredivisie (Pays-Bas)",
    "soccer_portugal_primeira_liga": "Liga Portugal",
    "soccer_belgium_first_div": "Jupiler Pro League (Belgique)",
    "soccer_turkey_super_league": "Süper Lig (Turquie)",
    "soccer_efl_champ": "Championship (Angleterre D2)",
}

FRENCH_BOOKS = "betclic_fr,winamax_fr,unibet_fr,pmu_fr,parionssport_fr,netbet_fr"

DEFAULT_CONFIG = {
    "api_key": "",
    "sports": ["soccer_france_ligue_one", "soccer_epl", "soccer_spain_la_liga", "soccer_uefa_champs_league"],
    "extra_sports": "",
    "regions": "eu",
    # Liste de bookmakers demandés à l'API (max 10 = prix d'une seule région).
    # Pinnacle donne la vraie proba, les FR sont ceux où tu peux parier.
    "source_books": "pinnacle,betclic_fr,winamax_fr,unibet_fr,pmu_fr,parionssport_fr,netbet_fr,"
                    "marathonbet,onexbet,sport888",
    "markets": "h2h,totals",
    "refresh_minutes": 720,
    "bookmakers": "betclic_fr",   # là où tu paries
    "cfg_version": 3,
    "min_ev": 0.02,
    "max_ev": 0.15,
    "min_odds": 1.30,
    "max_odds": 6.00,
    "min_books": 4,
    "horizon_hours": 72,
    "max_legs": 3,
    "min_combo_prob": 0.15,
    "min_stake": 0.50,
    "kelly_fraction": 0.25,
    "demo": False,
}

# Bookmakers dont les cotes reflètent le mieux la vraie probabilité
SHARP_WEIGHTS = {"pinnacle": 3.0, "betfair_ex_eu": 2.0, "betfair_ex_uk": 2.0,
                 "matchbook": 1.5, "smarkets": 1.5}
# Exchanges : utiles pour le consensus, mais pas comme "endroit où parier"
EXCHANGES = {"betfair_ex_eu", "betfair_ex_uk", "betfair_ex_au", "matchbook", "smarkets"}

MARKET_LABELS = {"h2h": "Résultat", "totals": "Total", "spreads": "Handicap"}


def config_dir():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(os.path.join(config_dir(), "config.json"), encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    if cfg.get("cfg_version", 0) < 3:  # mise à jour : on ne parie que sur Betclic
        cfg["bookmakers"] = "betclic_fr"
        cfg["cfg_version"] = 3
    cfg["sports"] = [k for k in cfg.get("sports", []) if k.startswith("soccer")] or list(DEFAULT_CONFIG["sports"])
    return cfg


def save_config(cfg):
    with open(os.path.join(config_dir(), "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def save_cache(events, quota):
    try:
        with open(os.path.join(config_dir(), "cache.json"), "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "events": events, "quota": quota}, f)
    except OSError:
        pass


def load_cache():
    try:
        with open(os.path.join(config_dir(), "cache.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def sports_list(cfg):
    keys = list(cfg.get("sports", []))
    keys += [s.strip() for s in cfg.get("extra_sports", "").split(",") if s.strip().startswith("soccer")]
    seen, out = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def credits_per_refresh(cfg):
    books = [b for b in cfg.get("source_books", "").split(",") if b.strip()]
    if books:
        n_regions = (len(books) + 9) // 10
    else:
        n_regions = len([r for r in cfg["regions"].split(",") if r.strip()]) or 1
    n_markets = len([m for m in cfg["markets"].split(",") if m.strip()]) or 1
    return len(sports_list(cfg)) * n_regions * n_markets


def credits_per_month(cfg):
    per_day = 24 * 60 / max(5, float(cfg["refresh_minutes"]))
    return int(credits_per_refresh(cfg) * per_day * 30)


# --------------------------------------------------------------------------
# Récupération des cotes
# --------------------------------------------------------------------------

def http_get_json(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
        headers = {k.lower(): v for k, v in r.headers.items()}
    return data, headers


def fetch_odds(cfg):
    """Renvoie (events, quota, erreurs)."""
    events, quota, errors = [], None, []
    for sport in sports_list(cfg):
        params = {
            "apiKey": cfg["api_key"].strip(),
            "markets": cfg["markets"],
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        if cfg.get("source_books", "").strip():
            params["bookmakers"] = cfg["source_books"].replace(" ", "")
        else:
            params["regions"] = cfg["regions"]
        url = f"{API_BASE}/sports/{sport}/odds?{urllib.parse.urlencode(params)}"
        try:
            data, h = http_get_json(url)
            for ev in data:
                ev["_sport_key"] = sport
            events.extend(data)
            quota = {"remaining": h.get("x-requests-remaining"), "used": h.get("x-requests-used")}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:160]
            if e.code == 401:
                errors.append("Clé API refusée ou quota du mois épuisé (401).")
                break
            if e.code == 422:
                errors.append(f"{sport} : compétition inconnue ou sans match (422).")
            elif e.code == 429:
                errors.append("Trop de requêtes en même temps (429), réessaie dans 1 min.")
            else:
                errors.append(f"{sport} : erreur HTTP {e.code} {body}")
        except Exception as e:  # réseau coupé, timeout...
            errors.append(f"{sport} : {e}")
    return events, quota, errors


# --------------------------------------------------------------------------
# Données de démonstration (sans clé API)
# --------------------------------------------------------------------------

def demo_events(seed=None):
    rnd = random.Random(seed)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    leagues = {
        "soccer_france_ligue_one": ("Ligue 1", ["PSG", "Marseille", "Lyon", "Monaco", "Lille", "Lens",
                                                 "Rennes", "Nice", "Nantes", "Strasbourg", "Brest", "Toulouse"]),
        "soccer_epl": ("EPL", ["Arsenal", "Liverpool", "Man City", "Chelsea", "Tottenham", "Newcastle",
                               "Aston Villa", "Brighton"]),
        "soccer_spain_la_liga": ("Liga", ["Real Madrid", "Barcelone", "Atlético", "Bilbao", "Villarreal",
                                          "Betis", "Séville", "Real Sociedad"]),
    }
    books = [("pinnacle", "Pinnacle"), ("betclic_fr", "Betclic (FR)"), ("winamax_fr", "Winamax (FR)"),
             ("unibet_fr", "Unibet (FR)"), ("pmu_fr", "PMU (FR)"), ("parionssport_fr", "Parions Sport (FR)"),
             ("netbet_fr", "NetBet (FR)"), ("williamhill", "William Hill"), ("marathonbet", "Marathon Bet"),
             ("sport888", "888sport"), ("betsson", "Betsson"), ("onexbet", "1xBet")]
    events = []
    for skey, (title, teams) in leagues.items():
        teams = teams[:]
        rnd.shuffle(teams)
        soccer = skey.startswith("soccer")
        for i in range(0, len(teams) - 1, 2):
            home, away = teams[i], teams[i + 1]
            start = now + timedelta(hours=rnd.randint(3, 70), minutes=rnd.choice([0, 15, 30, 45]))
            if soccer:
                ph = rnd.uniform(0.25, 0.65)
                pd = rnd.uniform(0.22, 0.30)
                pa = max(0.07, 1 - ph - pd)
                s = ph + pd + pa
                true = {home: ph / s, "Draw": pd / s, away: pa / s}
                tot_point, p_over = 2.5, rnd.uniform(0.40, 0.62)
            else:
                ph = rnd.uniform(0.25, 0.78)
                true = {home: ph, away: 1 - ph}
                tot_point, p_over = rnd.choice([218.5, 224.5, 229.5]), rnd.uniform(0.45, 0.55)
            bms = []
            for bkey, btitle in rnd.sample(books, rnd.randint(7, len(books))):
                margin = 1.025 if bkey == "pinnacle" else rnd.uniform(1.04, 1.09)
                generous = rnd.random() < 0.12  # parfois un bookmaker "se trompe"

                def price(p):
                    noise = rnd.uniform(0.97, 1.03) if bkey != "pinnacle" else rnd.uniform(0.995, 1.005)
                    o = 1 / (p * margin) * noise
                    if generous:
                        o *= rnd.uniform(1.06, 1.12)
                    return round(max(1.01, o), 2)

                markets = [{"key": "h2h", "outcomes": [{"name": n, "price": price(p)} for n, p in true.items()]},
                           {"key": "totals", "outcomes": [
                               {"name": "Over", "price": price(p_over), "point": tot_point},
                               {"name": "Under", "price": price(1 - p_over), "point": tot_point}]}]
                bms.append({"key": bkey, "title": btitle, "markets": markets})
            events.append({"id": f"demo-{skey}-{i}", "sport_key": skey, "sport_title": title,
                           "_sport_key": skey, "commence_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "home_team": home, "away_team": away, "bookmakers": bms})
    return events


# --------------------------------------------------------------------------
# Moteur d'analyse
# --------------------------------------------------------------------------

def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def selection_label(mkey, name, point, home, away, sport_key):
    unit = "buts" if sport_key.startswith("soccer") else "pts"
    if mkey == "h2h":
        if name == "Draw":
            return "Match nul"
        return f"Victoire {name}"
    if mkey == "totals":
        return f"{'Plus' if name == 'Over' else 'Moins'} de {point:g} {unit}".replace(".", ",")
    if mkey == "spreads":
        return f"{name} ({point:+g})"
    return f"{name} {point if point is not None else ''}".strip()


def analyze(events, cfg, now=None):
    """Renvoie la liste de TOUTES les sélections analysées (une par issue)."""
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(hours=float(cfg["horizon_hours"]))
    allowed = {b.strip().lower() for b in cfg.get("bookmakers", "").split(",") if b.strip()}
    min_books = int(cfg["min_books"])
    out = []

    for ev in events:
        try:
            start = parse_time(ev["commence_time"])
        except (KeyError, ValueError):
            continue
        if start <= now + timedelta(minutes=2) or start > horizon:
            continue
        home, away = ev.get("home_team") or "", ev.get("away_team") or ""
        sport_key = ev.get("_sport_key") or ev.get("sport_key", "")
        match = f"{home} - {away}" if home else ev.get("sport_title", "?")

        # (marché, ligne) -> liste de cotes par bookmaker
        raw = {}
        for bk in ev.get("bookmakers", []):
            bkey = bk.get("key", "")
            for m in bk.get("markets", []):
                mkey = m.get("key")
                by_line = {}
                for o in m.get("outcomes", []):
                    pt = o.get("point")
                    line = None if mkey == "h2h" or pt is None else abs(float(pt))
                    by_line.setdefault(line, []).append(o)
                for line, outs in by_line.items():
                    raw.setdefault((mkey, line), []).append((bkey, bk.get("title", bkey), outs))

        for (mkey, line), books in raw.items():
            n_out = max(len(b[2]) for b in books)
            books = [b for b in books if len(b[2]) == n_out and n_out >= 2]
            if len(books) < min_books:
                continue
            agg = {}
            for bkey, btitle, outs in books:
                try:
                    inv = [1.0 / float(o["price"]) for o in outs]
                except (KeyError, ValueError, ZeroDivisionError):
                    continue
                overround = sum(inv)
                if not 0.97 <= overround <= 1.25:  # cotes incohérentes -> ignorées
                    continue
                w = SHARP_WEIGHTS.get(bkey, 1.0)
                for o, i in zip(outs, inv):
                    pt = o.get("point")
                    oid = o["name"] if pt is None or mkey == "h2h" else f"{o['name']}|{float(pt):g}"
                    a = agg.setdefault(oid, {"pw": 0.0, "w": 0.0, "n": 0, "prices": {}, "name": o["name"],
                                             "point": None if mkey == "h2h" else pt})
                    a["pw"] += (i / overround) * w
                    a["w"] += w
                    a["n"] += 1
                    a["prices"][bkey] = (float(o["price"]), btitle)

            for oid, a in agg.items():
                if a["n"] < min_books or a["w"] <= 0:
                    continue
                p = a["pw"] / a["w"]
                playable = {k: v for k, v in a["prices"].items()
                            if (k in allowed if allowed else k not in EXCHANGES)}
                if not playable:
                    continue
                best_key = max(playable, key=lambda k: playable[k][0])
                odds, book_title = playable[best_key]
                ev_ = p * odds - 1
                kelly = ev_ / (odds - 1) if odds > 1 else 0.0
                out.append({
                    "uid": f"{ev.get('id')}|{mkey}|{oid}",
                    "event_id": ev.get("id"),
                    "match": match,
                    "sport": SPORTS.get(sport_key, ev.get("sport_title", sport_key)),
                    "start": start,
                    "market": MARKET_LABELS.get(mkey, mkey),
                    "selection": selection_label(mkey, a["name"], a["point"], home, away, sport_key),
                    "p": p,
                    "fair": 1 / p if p > 0 else 0,
                    "odds": odds,
                    "book": book_title,
                    "book_key": best_key,
                    "ev": ev_,
                    "kelly": kelly,
                    "n_books": a["n"],
                    "playable": playable,          # {book_key: (cote, nom)}
                    "all_prices": a["prices"],
                    "suspect": ev_ > float(cfg["max_ev"]),
                })
    return out


def value_bets(selections, cfg):
    lo, hi, min_ev = float(cfg["min_odds"]), float(cfg["max_odds"]), float(cfg["min_ev"])
    vb = [s for s in selections if lo <= s["odds"] <= hi and s["ev"] >= min_ev]
    vb.sort(key=lambda s: s["ev"], reverse=True)
    return vb


def most_probable(selections, cfg, limit=60):
    lo, hi = float(cfg["min_odds"]), float(cfg["max_odds"])
    pr = [s for s in selections if lo <= s["odds"] <= hi and not s["suspect"]]
    pr.sort(key=lambda s: s["p"], reverse=True)
    return pr[:limit]


def build_combos(vbets, cfg, pool_size=16, limit=40):
    """Combinés de 2..max_legs sélections, matchs différents, MÊME bookmaker."""
    pool = [b for b in vbets if not b["suspect"]][:pool_size]
    max_legs = int(cfg["max_legs"])
    min_prob = float(cfg["min_combo_prob"])
    combos = []
    for k in range(2, max_legs + 1):
        for legs in itertools.combinations(pool, k):
            if len({l["event_id"] for l in legs}) < k:
                continue
            p = 1.0
            for l in legs:
                p *= l["p"]
            if p < min_prob:
                continue
            common = set(legs[0]["playable"])
            for l in legs[1:]:
                common &= set(l["playable"])
            best = None
            for bkey in common:
                o = 1.0
                for l in legs:
                    o *= l["playable"][bkey][0]
                if best is None or o > best[0]:
                    best = (o, bkey, legs[0]["playable"][bkey][1])
            if not best:
                continue
            odds, bkey, btitle = best
            ev_ = p * odds - 1
            if ev_ <= 0:
                continue
            combos.append({
                "uid": "+".join(l["uid"] for l in legs),
                "legs": legs,
                "leg_odds": [l["playable"][bkey][0] for l in legs],
                "p": p, "odds": odds, "ev": ev_,
                "kelly": ev_ / (odds - 1),
                "book": btitle, "book_key": bkey,
            })
    combos.sort(key=lambda c: c["kelly"], reverse=True)
    # évite d'afficher un combiné déjà contenu dans un meilleur
    out, used = [], []
    for c in combos:
        key = frozenset(l["uid"] for l in c["legs"])
        if any(key <= u or u <= key for u in used):
            continue
        used.append(key)
        out.append(c)
        if len(out) >= limit:
            break
    return out


def _allocate(items, amount_cents, min_cents):
    """Répartit amount_cents proportionnellement au Kelly, avec mise minimale."""
    items = [i for i in items if i["kelly"] > 0]
    while items:
        total_k = sum(i["kelly"] for i in items)
        raw = [amount_cents * i["kelly"] / total_k for i in items]
        if min(raw) >= min_cents or len(items) == 1:
            break
        items = [i for i, r in zip(items, raw) if r != min(raw)]
    if not items:
        return []
    total_k = sum(i["kelly"] for i in items)
    stakes = [int(amount_cents * i["kelly"] / total_k // 10 * 10) for i in items]
    stakes[0] += amount_cents - sum(stakes)
    return list(zip(items, stakes))


def stake_plan(budget, mode, vbets, combos, cfg, sims=20000, seed=None):
    """Construit le meilleur plan de mise pour un budget donné."""
    budget_c = int(round(budget * 100))
    min_c = int(round(float(cfg["min_stake"]) * 100))
    singles, seen = [], set()
    for b in vbets:
        if b["suspect"] or b["event_id"] in seen:
            continue
        if mode == "prudent" and b["p"] < 0.40:
            continue
        seen.add(b["event_id"])
        singles.append(b)
    if mode == "prudent":
        s_pick, c_pick, c_share = singles[:6], [], 0.0
    elif mode == "equilibre":
        s_pick, c_pick, c_share = singles[:5], combos[:1], 0.25
    else:
        s_pick, c_pick, c_share = singles[:3], combos[:2], 0.60
    if not c_pick:
        c_share = 0.0
    if not s_pick:
        c_share = 1.0 if c_pick else 0.0

    c_amount = int(budget_c * c_share)
    s_amount = budget_c - c_amount
    s_alloc = _allocate(s_pick, s_amount, min_c) if s_amount else []
    c_alloc = _allocate(c_pick, c_amount, min_c) if c_amount else []

    plan = {"budget": budget, "mode": mode, "singles": [], "combos": []}
    for b, c in s_alloc:
        plan["singles"].append({"bet": b, "stake": c / 100})
    for cb, c in c_alloc:
        plan["combos"].append({"combo": cb, "stake": c / 100})
    if not plan["singles"] and not plan["combos"]:
        return plan

    # Espérance exacte
    plan["expected"] = sum(x["stake"] * x["bet"]["ev"] for x in plan["singles"]) + \
        sum(x["stake"] * x["combo"]["ev"] for x in plan["combos"])
    plan["max_win"] = sum(x["stake"] * (x["bet"]["odds"] - 1) for x in plan["singles"]) + \
        sum(x["stake"] * (x["combo"]["odds"] - 1) for x in plan["combos"])

    # Simulation Monte-Carlo (matchs indépendants)
    rnd = random.Random(seed)
    probs = {}
    for x in plan["singles"]:
        probs[x["bet"]["uid"]] = x["bet"]["p"]
    for x in plan["combos"]:
        for l in x["combo"]["legs"]:
            probs[l["uid"]] = l["p"]
    uids = list(probs)
    staked = sum(x["stake"] for x in plan["singles"]) + sum(x["stake"] for x in plan["combos"])
    pos = zero = 0
    results = []
    for _ in range(sims):
        won = {u: rnd.random() < probs[u] for u in uids}
        ret = 0.0
        for x in plan["singles"]:
            if won[x["bet"]["uid"]]:
                ret += x["stake"] * x["bet"]["odds"]
        for x in plan["combos"]:
            if all(won[l["uid"]] for l in x["combo"]["legs"]):
                ret += x["stake"] * x["combo"]["odds"]
        net = ret - staked
        results.append(net)
        if net > 0:
            pos += 1
        if ret == 0:
            zero += 1
    results.sort()
    plan["p_profit"] = pos / sims
    plan["p_lose_all"] = zero / sims
    plan["median"] = results[sims // 2]
    plan["staked"] = staked
    return plan


# --------------------------------------------------------------------------
# Formatage
# --------------------------------------------------------------------------

JOURS = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]


def fmt_date(dt):
    loc = dt.astimezone()
    return f"{JOURS[loc.weekday()]} {loc:%d/%m %H:%M}"


def eur(x):
    return f"{x:,.2f} €".replace(",", " ").replace(".", ",")


def pct(x, sign=False):
    s = f"{x * 100:+.1f} %" if sign else f"{x * 100:.0f} %"
    return s.replace(".", ",")


def odd(x):
    return f"{x:.2f}".replace(".", ",")


def plan_text(plan, updated):
    modes = {"prudent": "Prudent", "equilibre": "Équilibré", "agressif": "Agressif"}
    L = []
    L.append(f"PLAN POUR {eur(plan['budget'])}  ·  mode {modes.get(plan['mode'], plan['mode'])}")
    L.append(f"Cotes mises à jour : {updated}")
    L.append("")
    if not plan["singles"] and not plan["combos"]:
        L.append("Aucun pari rentable trouvé avec tes réglages pour le moment.")
        L.append("Ne pas parier est aussi une décision gagnante : attends la prochaine mise à jour,")
        L.append("ou élargis les compétitions / bookmakers dans Paramètres.")
        return "\n".join(L)
    n = 1
    if plan["singles"]:
        L.append("━━ PARIS SIMPLES ━━")
        for x in plan["singles"]:
            b = x["bet"]
            L.append(f"{n}. Mise {eur(x['stake']):>9}  →  {b['selection']}")
            L.append(f"     {b['match']}  ·  {b['sport']}  ·  {fmt_date(b['start'])}")
            L.append(f"     Cote {odd(b['odds'])} chez {b['book']}  ·  proba réelle {pct(b['p'])}"
                     f"  ·  value {pct(b['ev'], True)}  ·  gain si gagné {eur(x['stake'] * (b['odds'] - 1))}")
            L.append("")
            n += 1
    if plan["combos"]:
        L.append("━━ COMBINÉS ━━")
        for x in plan["combos"]:
            c = x["combo"]
            L.append(f"{n}. Mise {eur(x['stake']):>9}  →  Combiné x{len(c['legs'])} chez {c['book']}"
                     f"  ·  cote {odd(c['odds'])}")
            for l, lo in zip(c["legs"], c["leg_odds"]):
                L.append(f"     • {l['selection']}  ({l['match']}, {fmt_date(l['start'])})  @ {odd(lo)}")
            L.append(f"     Proba réelle {pct(c['p'])}  ·  value {pct(c['ev'], True)}"
                     f"  ·  gain si gagné {eur(x['stake'] * (c['odds'] - 1))}")
            L.append("")
            n += 1
    L.append("━━ CE QUE DIT LA SIMULATION (20 000 scénarios) ━━")
    L.append(f"Total misé                : {eur(plan['staked'])}")
    L.append(f"Gain moyen espéré         : {'+' if plan['expected'] >= 0 else ''}{eur(plan['expected'])}")
    L.append(f"Chance de finir gagnant   : {pct(plan['p_profit'])}")
    L.append(f"Chance de tout perdre     : {pct(plan['p_lose_all'])}")
    L.append(f"Gain max (tout passe)     : +{eur(plan['max_win'])}")
    L.append("")
    L.append("À retenir : un value bet n'est PAS un pari sûr. Il est rentable en moyenne,")
    L.append("sur des dizaines/centaines de paris. Sur une seule journée, perdre est normal.")
    L.append("Vérifie la cote sur le site avant de valider : elle peut avoir bougé.")
    return "\n".join(L)


# --------------------------------------------------------------------------
# Interface graphique (Tkinter)
# --------------------------------------------------------------------------

BG, PANEL, PANEL2 = "#111318", "#1a1d24", "#232731"
FG, MUTED, ACCENT, WARN = "#e7e9ee", "#8b93a1", "#22c55e", "#f59e0b"


def run_gui():
    import tkinter as tk
    from tkinter import ttk, messagebox

    class Table(ttk.Frame):
        """Treeview triable. columns = [(id, titre, largeur, affichage, tri, ancre)]"""

        def __init__(self, parent, columns, on_double=None):
            super().__init__(parent)
            self.columns = columns
            self.tree = ttk.Treeview(self, columns=[c[0] for c in columns], show="headings")
            for cid, title, w, _, _, anchor in columns:
                self.tree.heading(cid, text=title, command=lambda c=cid: self.sort_by(c))
                self.tree.column(cid, width=w, minwidth=40, anchor=anchor, stretch=(cid == columns[0][0]))
            vsb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=vsb.set)
            self.tree.pack(side="left", fill="both", expand=True)
            vsb.pack(side="right", fill="y")
            self.tree.tag_configure("hot", foreground="#4ade80")
            self.tree.tag_configure("good", foreground=FG)
            self.tree.tag_configure("warn", foreground=WARN)
            self.tree.tag_configure("neg", foreground=MUTED)
            self.rows, self.shown, self.tagfunc = [], [], None
            self.sort_col, self.sort_rev = None, True
            if on_double:
                self.tree.bind("<Double-1>", lambda e: self._double(on_double))

        def set_rows(self, rows, tagfunc=None):
            self.rows, self.tagfunc = rows, tagfunc
            self.render()

        def sort_by(self, cid):
            self.sort_rev = not self.sort_rev if self.sort_col == cid else True
            self.sort_col = cid
            self.render()

        def render(self):
            self.tree.delete(*self.tree.get_children())
            rows = self.rows
            if self.sort_col:
                col = next(c for c in self.columns if c[0] == self.sort_col)
                rows = sorted(rows, key=col[4], reverse=self.sort_rev)
            self.shown = rows
            for i, r in enumerate(rows):
                tags = (self.tagfunc(r),) if self.tagfunc else ()
                self.tree.insert("", "end", iid=str(i), values=[c[3](r) for c in self.columns], tags=tags)

        def _double(self, cb):
            sel = self.tree.selection()
            if sel:
                cb(self.shown[int(sel[0])])

    class App:
        def __init__(self, root):
            self.root = root
            self.cfg = load_config()
            self.events, self.selections, self.vbets, self.probable, self.combos = [], [], [], [], []
            self.quota, self.errors, self.is_demo = None, [], False
            self.last_fetch, self.next_refresh = 0.0, time.time()
            self.fetching, self.last_analysis = False, 0.0
            self.q = queue.Queue()

            root.title(f"{APP_NAME} – Analyseur de paris sportifs")
            root.geometry("1280x760")
            root.minsize(980, 600)
            root.configure(bg=BG)
            self._style()
            self._build()

            cache = load_cache()
            if cache and cache.get("events") and self.cfg["api_key"] and not self.cfg["demo"]:
                self.events, self.quota = cache["events"], cache.get("quota")
                self.last_fetch = cache.get("ts", 0)
                self.next_refresh = self.last_fetch + float(self.cfg["refresh_minutes"]) * 60
                self.reanalyze()
            self.tick()

        # ---------- style ----------
        def _style(self):
            st = ttk.Style(self.root)
            st.theme_use("clam")
            st.configure(".", background=BG, foreground=FG, fieldbackground=PANEL, bordercolor=PANEL2,
                         font=("Segoe UI", 10))
            st.configure("TFrame", background=BG)
            st.configure("Card.TFrame", background=PANEL)
            st.configure("TLabel", background=BG, foreground=FG)
            st.configure("Card.TLabel", background=PANEL, foreground=FG)
            st.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
            st.configure("Title.TLabel", background=BG, foreground=FG, font=("Segoe UI Semibold", 18))
            st.configure("Badge.TLabel", background=WARN, foreground="#111", font=("Segoe UI Semibold", 9),
                         padding=(8, 2))
            st.configure("Live.TLabel", background=ACCENT, foreground="#06240f", font=("Segoe UI Semibold", 9),
                         padding=(8, 2))
            st.configure("TButton", background=PANEL2, foreground=FG, padding=(12, 6), borderwidth=0)
            st.map("TButton", background=[("active", "#2f3542")])
            st.configure("Accent.TButton", background=ACCENT, foreground="#06240f",
                         font=("Segoe UI Semibold", 10))
            st.map("Accent.TButton", background=[("active", "#16a34a")])
            st.configure("TNotebook", background=BG, borderwidth=0)
            st.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(16, 7))
            st.map("TNotebook.Tab", background=[("selected", PANEL2)], foreground=[("selected", FG)])
            st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG, rowheight=26,
                         borderwidth=0)
            st.configure("Treeview.Heading", background=PANEL2, foreground=FG, relief="flat",
                         font=("Segoe UI Semibold", 9))
            st.map("Treeview", background=[("selected", "#1f5135")])
            st.configure("TEntry", fieldbackground=PANEL2, foreground=FG, insertcolor=FG)
            st.configure("TCheckbutton", background=BG, foreground=FG)
            st.map("TCheckbutton", background=[("active", BG)])
            st.configure("TRadiobutton", background=BG, foreground=FG)
            st.map("TRadiobutton", background=[("active", BG)])
            st.configure("TSpinbox", fieldbackground=PANEL2, foreground=FG, arrowcolor=FG)

        # ---------- construction ----------
        def _build(self):
            top = ttk.Frame(self.root, padding=(16, 12, 16, 4))
            top.pack(fill="x")
            ttk.Label(top, text="BetScope", style="Title.TLabel").pack(side="left")
            self.mode_badge = ttk.Label(top, text="", style="Live.TLabel")
            self.mode_badge.pack(side="left", padx=12)
            ttk.Button(top, text="⟳  Actualiser maintenant", style="Accent.TButton",
                       command=self.refresh).pack(side="right")
            self.countdown = ttk.Label(top, text="", style="Muted.TLabel")
            self.countdown.pack(side="right", padx=12)

            self.nb = ttk.Notebook(self.root)
            self.nb.pack(fill="both", expand=True, padx=16, pady=8)

            # Onglet 1 : value bets
            f1 = ttk.Frame(self.nb, padding=8)
            self.nb.add(f1, text="  Meilleurs paris  ")
            self.info1 = ttk.Label(f1, text="", style="Muted.TLabel")
            self.info1.pack(anchor="w", pady=(0, 6))
            cols = [
                ("match", "Match", 230, lambda b: b["match"], lambda b: b["match"], "w"),
                ("sport", "Compétition", 150, lambda b: b["sport"], lambda b: b["sport"], "w"),
                ("date", "Date", 105, lambda b: fmt_date(b["start"]), lambda b: b["start"], "w"),
                ("pari", "Pari", 180, lambda b: b["selection"], lambda b: b["selection"], "w"),
                ("cote", "Cote", 60, lambda b: odd(b["odds"]), lambda b: b["odds"], "center"),
                ("book", "Où parier", 120, lambda b: b["book"], lambda b: b["book"], "w"),
                ("juste", "Cote juste", 75, lambda b: odd(b["fair"]), lambda b: b["fair"], "center"),
                ("proba", "Proba", 60, lambda b: pct(b["p"]), lambda b: b["p"], "center"),
                ("value", "Value", 70, lambda b: pct(b["ev"], True) + (" ⚠" if b["suspect"] else ""),
                 lambda b: b["ev"], "center"),
                ("nb", "Books", 55, lambda b: b["n_books"], lambda b: b["n_books"], "center"),
            ]
            self.t_value = Table(f1, cols, on_double=self.show_detail)
            self.t_value.pack(fill="both", expand=True)

            # Onglet 2 : plus probables
            f2 = ttk.Frame(self.nb, padding=8)
            self.nb.add(f2, text="  Plus probables  ")
            ttk.Label(f2, text="Les issues les plus probables selon le marché. Une value négative = "
                               "pari perdant sur la durée, même s'il passe souvent.",
                      style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
            self.t_prob = Table(f2, cols, on_double=self.show_detail)
            self.t_prob.pack(fill="both", expand=True)

            # Onglet 3 : combinés
            f3 = ttk.Frame(self.nb, padding=8)
            self.nb.add(f3, text="  Combinés  ")
            ttk.Label(f3, text="Combinés de value bets sur des matchs différents, au même bookmaker "
                               "(double-clic pour le détail).", style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
            ccols = [
                ("legs", "Sélections", 520,
                 lambda c: "  +  ".join(f"{l['selection']} ({l['match']})" for l in c["legs"]),
                 lambda c: len(c["legs"]), "w"),
                ("n", "Nb", 40, lambda c: len(c["legs"]), lambda c: len(c["legs"]), "center"),
                ("cote", "Cote", 70, lambda c: odd(c["odds"]), lambda c: c["odds"], "center"),
                ("book", "Où parier", 120, lambda c: c["book"], lambda c: c["book"], "w"),
                ("proba", "Proba", 65, lambda c: pct(c["p"]), lambda c: c["p"], "center"),
                ("value", "Value", 75, lambda c: pct(c["ev"], True), lambda c: c["ev"], "center"),
            ]
            self.t_combo = Table(f3, ccols, on_double=self.show_combo)
            self.t_combo.pack(fill="both", expand=True)

            # Onglet 4 : budget
            f4 = ttk.Frame(self.nb, padding=12)
            self.nb.add(f4, text="  Mon budget  ")
            row = ttk.Frame(f4)
            row.pack(fill="x")
            ttk.Label(row, text="Je veux miser :", font=("Segoe UI Semibold", 12)).pack(side="left")
            self.budget_var = tk.StringVar(value="10")
            e = ttk.Entry(row, textvariable=self.budget_var, width=8, font=("Segoe UI", 12))
            e.pack(side="left", padx=6)
            ttk.Label(row, text="€", font=("Segoe UI", 12)).pack(side="left")
            self.mode_var = tk.StringVar(value="equilibre")
            for val, txt in (("prudent", "Prudent (simples, favoris)"),
                             ("equilibre", "Équilibré (simples + 1 combiné)"),
                             ("agressif", "Agressif (gros combinés)")):
                ttk.Radiobutton(row, text=txt, value=val, variable=self.mode_var).pack(side="left", padx=10)
            ttk.Button(row, text="Trouver le meilleur plan", style="Accent.TButton",
                       command=self.compute_plan).pack(side="left", padx=10)
            e.bind("<Return>", lambda _e: self.compute_plan())
            self.plan_box = tk.Text(f4, bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
                                    font=("Consolas", 10), wrap="word", padx=12, pady=10)
            self.plan_box.pack(fill="both", expand=True, pady=(12, 0))
            self.plan_box.insert("1.0", "Entre un montant puis clique sur « Trouver le meilleur plan ».")

            # Onglet 5 : paramètres
            self._build_settings()

            # Barre de statut
            bottom = ttk.Frame(self.root, padding=(16, 2, 16, 8))
            bottom.pack(fill="x")
            self.status = ttk.Label(bottom, text="Démarrage…", style="Muted.TLabel")
            self.status.pack(side="left")
            ttk.Label(bottom, text="18+ · Jouer comporte des risques : endettement, dépendance… "
                                   "Appelez le 09 74 75 13 13 (appel non surtaxé)",
                      style="Muted.TLabel").pack(side="right")

        def _build_settings(self):
            f = ttk.Frame(self.nb, padding=12)
            self.nb.add(f, text="  Paramètres  ")
            self.v = {}
            left = ttk.Frame(f)
            left.pack(side="left", fill="both", expand=True)
            right = ttk.Frame(f)
            right.pack(side="left", fill="both", expand=True, padx=(24, 0))

            def field(parent, key, label, width=40, hint=None):
                ttk.Label(parent, text=label).pack(anchor="w", pady=(8, 2))
                var = tk.StringVar(value=str(self.cfg[key]))
                ttk.Entry(parent, textvariable=var, width=width).pack(anchor="w", fill="x")
                if hint:
                    ttk.Label(parent, text=hint, style="Muted.TLabel", wraplength=480,
                              justify="left").pack(anchor="w")
                self.v[key] = var

            field(left, "api_key", "Clé API The Odds API (gratuite sur the-odds-api.com)")
            ttk.Label(left, text="Sans clé, l'appli tourne en mode DÉMO avec des matchs fictifs.",
                      style="Muted.TLabel").pack(anchor="w")
            ttk.Label(left, text="Championnats de foot suivis", font=("Segoe UI Semibold", 10)).pack(anchor="w", pady=(12, 2))
            grid = ttk.Frame(left)
            grid.pack(anchor="w", fill="x")
            self.sport_vars = {}
            for i, (k, label) in enumerate(SPORTS.items()):
                var = tk.BooleanVar(value=k in self.cfg["sports"])
                ttk.Checkbutton(grid, text=label, variable=var, command=self.update_estimate).grid(
                    row=i // 2, column=i % 2, sticky="w", padx=(0, 18), pady=1)
                self.sport_vars[k] = var
            field(left, "extra_sports", "Autres compétitions (clés séparées par des virgules)",
                  hint="Clés commençant par soccer_ (ex : soccer_france_coupe_de_france). Liste sur "
                       "the-odds-api.com/sports-odds-data/sports-apis.html")
            field(left, "bookmakers", "Où je parie (par défaut : betclic_fr)",
                  hint="Clés : betclic_fr, winamax_fr, unibet_fr, pmu_fr, parionssport_fr, netbet_fr… "
                       "Vide = tous. Les autres bookmakers servent quand même au calcul de la proba.")

            field(right, "source_books", "Bookmakers analysés (10 max = 1 crédit par marché)",
                  hint="Servent à calculer la vraie proba. Garde pinnacle dedans. "
                       "Vide = on utilise les régions ci-dessous.")
            field(right, "regions", "Régions (si la liste ci-dessus est vide)", hint="eu, fr, uk…")
            field(right, "markets", "Marchés", hint="h2h = 1N2 (victoire / nul), totals = plus/moins de buts, "
                                                     "spreads = handicap.")
            field(right, "refresh_minutes", "Mise à jour automatique toutes les (minutes)")
            nums = ttk.Frame(right)
            nums.pack(anchor="w", fill="x", pady=(8, 0))
            small = [("min_ev", "Value mini (0,02 = 2 %)"), ("max_ev", "Value max avant ⚠"),
                     ("min_odds", "Cote mini"), ("max_odds", "Cote maxi"),
                     ("min_books", "Nb bookmakers mini"), ("horizon_hours", "Matchs dans les (heures)"),
                     ("max_legs", "Sélections max / combiné"), ("min_combo_prob", "Proba mini combiné"),
                     ("min_stake", "Mise mini (€)"), ("kelly_fraction", "Fraction de Kelly")]
            for i, (k, label) in enumerate(small):
                ttk.Label(nums, text=label).grid(row=i // 2, column=(i % 2) * 2, sticky="w", pady=3, padx=(0, 6))
                var = tk.StringVar(value=str(self.cfg[k]))
                ttk.Entry(nums, textvariable=var, width=8).grid(row=i // 2, column=(i % 2) * 2 + 1,
                                                                sticky="w", padx=(0, 18))
                self.v[k] = var
            self.demo_var = tk.BooleanVar(value=self.cfg["demo"])
            ttk.Checkbutton(right, text="Forcer le mode démo (données fictives)",
                            variable=self.demo_var).pack(anchor="w", pady=(10, 0))
            self.estimate = ttk.Label(right, text="", style="Muted.TLabel", wraplength=480, justify="left")
            self.estimate.pack(anchor="w", pady=(10, 0))
            ttk.Button(right, text="Enregistrer", style="Accent.TButton",
                       command=self.save_settings).pack(anchor="w", pady=12)
            for var in self.v.values():
                var.trace_add("write", lambda *_: self.update_estimate())
            self.update_estimate()

        def _read_settings(self):
            cfg = dict(self.cfg)
            for k, var in self.v.items():
                val = var.get().strip()
                if isinstance(DEFAULT_CONFIG[k], (int, float)) and not isinstance(DEFAULT_CONFIG[k], bool):
                    val = float(val.replace(",", "."))
                    if isinstance(DEFAULT_CONFIG[k], int):
                        val = int(val)
                cfg[k] = val
            cfg["sports"] = [k for k, var in self.sport_vars.items() if var.get()]
            cfg["demo"] = bool(self.demo_var.get())
            return cfg

        def update_estimate(self):
            try:
                cfg = self._read_settings()
            except (ValueError, KeyError):
                self.estimate.config(text="Valeur invalide dans un champ.")
                return
            per = credits_per_refresh(cfg)
            month = credits_per_month(cfg)
            warn = "  ⚠ dépasse l'offre gratuite (500/mois) : augmente l'intervalle ou réduis les " \
                   "compétitions/marchés." if month > 500 else "  ✓ OK pour l'offre gratuite (500/mois)."
            self.estimate.config(text=f"Coût : {per} crédits par mise à jour ≈ {month} crédits/mois.{warn}")

        def save_settings(self):
            try:
                cfg = self._read_settings()
            except ValueError:
                messagebox.showerror(APP_NAME, "Un champ numérique est invalide.")
                return
            changed_source = (cfg["api_key"], cfg["source_books"], cfg["regions"], cfg["markets"], sports_list(cfg), cfg["demo"]) != \
                (self.cfg["api_key"], self.cfg["source_books"], self.cfg["regions"], self.cfg["markets"], sports_list(self.cfg),
                 self.cfg["demo"])
            self.cfg = cfg
            save_config(cfg)
            if changed_source:
                self.refresh()
            else:
                self.next_refresh = self.last_fetch + float(cfg["refresh_minutes"]) * 60
                self.reanalyze()
            self.status.config(text="Paramètres enregistrés.")

        # ---------- données ----------
        def refresh(self):
            if self.fetching:
                return
            self.fetching = True
            demo = self.cfg["demo"] or not self.cfg["api_key"].strip()
            self.status.config(text="Récupération des cotes…")

            def worker():
                try:
                    if demo:
                        time.sleep(0.4)
                        self.q.put(("data", demo_events(), None, [], True))
                    else:
                        ev, quota, errs = fetch_odds(self.cfg)
                        self.q.put(("data", ev, quota, errs, False))
                except Exception as e:
                    self.q.put(("data", None, None, [str(e)], demo))

            threading.Thread(target=worker, daemon=True).start()

        def on_data(self, events, quota, errors, demo):
            self.fetching = False
            self.errors = errors
            now = time.time()
            if events is not None and (events or not errors):
                self.events, self.is_demo = events, demo
                self.last_fetch = now
                if quota:
                    self.quota = quota
                if not demo:
                    save_cache(events, self.quota)
            interval = 10 if demo else float(self.cfg["refresh_minutes"])
            self.next_refresh = now + interval * 60
            self.reanalyze()

        def reanalyze(self):
            self.last_analysis = time.time()
            try:
                self._reanalyze()
            except Exception as e:  # ne jamais laisser l'écran vide sans explication
                import traceback
                traceback.print_exc()
                self.status.config(text=f"⚠ Erreur d'analyse : {e!r}  ·  envoie une capture au développeur")

        def _reanalyze(self):
            self.warning = ""
            present = {bk.get("key") for ev in self.events for bk in ev.get("bookmakers", [])}
            mine = {b.strip().lower() for b in self.cfg.get("bookmakers", "").split(",") if b.strip()}
            cfg = self.cfg
            if mine and not (mine & present) and present:
                cfg = dict(self.cfg, bookmakers="")
                self.warning = ("Betclic n'a pas encore publié de cotes pour ces matchs : les cotes affichées viennent "
                                "d'autres bookmakers, à titre indicatif seulement.")
            self.selections = analyze(self.events, cfg)
            self.vbets = value_bets(self.selections, self.cfg)
            self.probable = most_probable(self.selections, self.cfg)
            self.combos = build_combos(self.vbets, self.cfg)

            def tag(b):
                if b["suspect"]:
                    return "warn"
                if b["ev"] >= 0.05:
                    return "hot"
                return "good" if b["ev"] >= 0 else "neg"

            self.t_value.set_rows(self.vbets, tag)
            self.t_prob.set_rows(self.probable, tag)
            self.t_combo.set_rows(self.combos, lambda c: "hot" if c["ev"] >= 0.08 else "good")
            n_events = len({s["event_id"] for s in self.selections})
            now_utc = datetime.now(timezone.utc)
            upcoming = 0
            for ev in self.events:
                try:
                    if parse_time(ev["commence_time"]) > now_utc:
                        upcoming += 1
                except (KeyError, ValueError):
                    pass
            if self.selections and not self.vbets:
                self.info1.config(text=f"Aucun value bet en ce moment sur {n_events} matchs : les cotes sont "
                                       f"justes partout. Regarde « Plus probables », ou baisse « Value mini » "
                                       f"dans Paramètres. Ne pas parier est aussi un bon choix.")
            else:
                self.info1.config(text=f"{len(self.vbets)} value bets sur {n_events} matchs analysés  ·  "
                                       f"Vert = value ≥ 5 %  ·  ⚠ = écart anormal, souvent une cote "
                                       f"pas encore corrigée, vérifie avant  ·  Double-clic = détail des cotes")
            if self.is_demo:
                self.mode_badge.config(text="MODE DÉMO · données fictives", style="Badge.TLabel")
            else:
                self.mode_badge.config(text="EN DIRECT", style="Live.TLabel")
            upd = datetime.fromtimestamp(self.last_fetch).strftime("%d/%m %H:%M") if self.last_fetch else "—"
            parts = [f"Dernière mise à jour : {upd}",
                     f"{len(self.events)} matchs reçus ({upcoming} à venir), {n_events} analysés"]
            if self.warning:
                parts.append("ℹ " + self.warning)
            if not self.events and not self.errors and not self.is_demo and self.last_fetch:
                parts.append("ℹ Aucun match à venir dans tes compétitions : coches-en d'autres dans Paramètres")
            if self.quota and self.quota.get("remaining") is not None:
                parts.append(f"Crédits API restants : {self.quota['remaining']}")
            if self.errors:
                parts.append("⚠ " + " | ".join(self.errors[:2]))
            self.status.config(text="   ·   ".join(parts))

        def tick(self):
            try:
                while True:
                    msg = self.q.get_nowait()
                    if msg[0] == "data":
                        self.on_data(*msg[1:])
            except queue.Empty:
                pass
            now = time.time()
            if not self.fetching and now >= self.next_refresh:
                self.refresh()
            if now - self.last_analysis > 60 and self.events:
                self.reanalyze()  # retire les matchs qui ont commencé (gratuit)
            left = max(0, int(self.next_refresh - now))
            h, rem = divmod(left, 3600)
            m, s = divmod(rem, 60)
            self.countdown.config(text="Mise à jour en cours…" if self.fetching
                                  else f"Prochaine mise à jour dans {h:d}h{m:02d}m{s:02d}s")
            self.root.after(1000, self.tick)

        # ---------- actions ----------
        def compute_plan(self):
            try:
                budget = float(self.budget_var.get().replace(",", ".").replace("€", "").strip())
                if budget <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror(APP_NAME, "Entre un montant valide, par exemple 10.")
                return
            plan = stake_plan(budget, self.mode_var.get(), self.vbets, self.combos, self.cfg)
            upd = datetime.fromtimestamp(self.last_fetch).strftime("%d/%m/%Y %H:%M") if self.last_fetch else "—"
            text = plan_text(plan, upd + ("  (DÉMO – données fictives)" if self.is_demo else ""))
            self.plan_box.delete("1.0", "end")
            self.plan_box.insert("1.0", text)

        def _popup(self, title, lines):
            w = tk.Toplevel(self.root)
            w.title(title)
            w.configure(bg=BG)
            w.geometry("620x480")
            t = tk.Text(w, bg=PANEL, fg=FG, relief="flat", font=("Consolas", 10), padx=12, pady=10)
            t.pack(fill="both", expand=True, padx=10, pady=10)
            t.insert("1.0", "\n".join(lines))
            t.config(state="disabled")

        def show_detail(self, b):
            lines = [b["match"], f"{b['sport']} · {fmt_date(b['start'])}", "",
                     f"Pari : {b['selection']}  ({b['market']})",
                     f"Probabilité estimée : {pct(b['p'])}  →  cote juste {odd(b['fair'])}",
                     f"Meilleure cote jouable : {odd(b['odds'])} chez {b['book']}  ·  value {pct(b['ev'], True)}",
                     "", "Cotes de tous les bookmakers :"]
            for k, (price, title) in sorted(b["all_prices"].items(), key=lambda kv: -kv[1][0]):
                mark = "  ← jouable" if k in b["playable"] else ""
                lines.append(f"  {odd(price):>6}  {title}{mark}")
            if b["suspect"]:
                lines += ["", "⚠ Value anormalement haute : souvent une cote périmée ou une erreur.",
                          "  Vérifie sur le site du bookmaker avant de parier."]
            self._popup(b["match"], lines)

        def show_combo(self, c):
            lines = [f"Combiné x{len(c['legs'])} chez {c['book']}", ""]
            for l, lo in zip(c["legs"], c["leg_odds"]):
                lines.append(f"• {l['selection']}  @ {odd(lo)}")
                lines.append(f"    {l['match']} · {fmt_date(l['start'])} · proba {pct(l['p'])}")
            lines += ["", f"Cote totale : {odd(c['odds'])}", f"Proba que tout passe : {pct(c['p'])}",
                      f"Value : {pct(c['ev'], True)}"]
            self._popup("Combiné", lines)

    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    run_gui()
