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
import subprocess
import sys
import tempfile
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

APP_NAME = "BetScope"
APP_VERSION = "1.2.0"
API_BASE = "https://api.the-odds-api.com/v4"
GITHUB_REPO = "LogicArt19/betscope"

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
# Mises à jour automatiques (via les Releases GitHub)
# --------------------------------------------------------------------------

def _vtuple(v):
    out = []
    for part in v.lstrip("vV").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def check_update():
    """Renvoie (version, url_du_exe) si une version plus récente existe, sinon None."""
    data, _ = http_get_json(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest", timeout=15)
    tag = data.get("tag_name", "")
    if not tag or _vtuple(tag) <= _vtuple(APP_VERSION):
        return None
    for asset in data.get("assets", []):
        if asset.get("name", "").lower().endswith(".exe"):
            return tag.lstrip("vV"), asset["browser_download_url"]
    return None


def download_update(url, progress=None):
    dest = os.path.join(tempfile.gettempdir(), "BetScope_update.exe")
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(done / total)
    if os.path.getsize(dest) < 1_000_000:
        raise RuntimeError("fichier téléchargé incomplet")
    return dest


def install_update_and_restart(new_exe):
    """Remplace le .exe actuel par le nouveau puis relance l'appli (Windows)."""
    exe = sys.executable
    bat = os.path.join(tempfile.gettempdir(), "betscope_update.bat")
    script = f"""@echo off
set /a n=0
:retry
timeout /t 2 /nobreak >nul
copy /y "{new_exe}" "{exe}" >nul 2>nul
if errorlevel 1 (
  set /a n+=1
  if %n% lss 30 goto retry
  start "" "{exe}"
  exit /b 1
)
del "{new_exe}" >nul 2>nul
start "" "{exe}"
del "%~f0"
"""
    with open(bat, "w", encoding="mbcs" if os.name == "nt" else "utf-8") as f:
        f.write(script)
    flags = 0x08000000 | 0x00000008  # CREATE_NO_WINDOW | DETACHED_PROCESS
    subprocess.Popen(["cmd", "/c", bat], creationflags=flags, close_fds=True)


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




# --------------------------------------------------------------------------
# Sélection des paris, combinés, plan de mise
# --------------------------------------------------------------------------

MODES = {
    # nb simples, nb combinés, part du budget en combinés, proba mini, sélections max par combiné,
    # proba mini d'un combiné
    "prudent":   dict(n_singles=4, n_combos=0, combo_share=0.0, min_p=0.55, legs=2, combo_p=0.30),
    "equilibre": dict(n_singles=3, n_combos=1, combo_share=0.30, min_p=0.40, legs=2, combo_p=0.22),
    "agressif":  dict(n_singles=1, n_combos=2, combo_share=0.70, min_p=0.30, legs=3, combo_p=0.08),
}
WORST_EV = -0.09  # au-delà, la cote est vraiment mauvaise : jamais proposée


def is_value(b, cfg):
    return b["ev"] >= float(cfg["min_ev"]) and not b["suspect"]


def candidates(selections, cfg, min_p=0.0):
    lo, hi = float(cfg["min_odds"]), float(cfg["max_odds"])
    return [s for s in selections
            if lo <= s["odds"] <= hi and not s["suspect"] and s["p"] >= min_p and s["ev"] >= WORST_EV]


def rank_key(b, cfg):
    """Value d'abord (triée par Kelly), puis les autres par espérance."""
    return (1 if is_value(b, cfg) else 0, b["kelly"] if is_value(b, cfg) else b["ev"])


def top_picks(selections, cfg, limit=40, min_p=0.33):
    """Value bets + meilleurs paris disponibles pour compléter."""
    vb = [s for s in selections if is_value(s, cfg)
          and float(cfg["min_odds"]) <= s["odds"] <= float(cfg["max_odds"])]
    vb.sort(key=lambda s: s["ev"], reverse=True)
    seen = {s["uid"] for s in vb}
    rest = [s for s in candidates(selections, cfg, min_p) if s["uid"] not in seen]
    rest.sort(key=lambda s: s["ev"], reverse=True)
    return (vb + rest)[:max(limit, len(vb))]


def most_probable(selections, cfg, limit=60):
    pr = candidates(selections, cfg)
    pr.sort(key=lambda s: s["p"], reverse=True)
    return pr[:limit]


def build_combos(pool, cfg, max_legs=3, min_prob=0.15, limit=40, pool_size=14):
    """Combinés de 2..max_legs sélections, matchs différents, au MÊME bookmaker."""
    pool = sorted(pool, key=lambda b: rank_key(b, cfg), reverse=True)[:pool_size]
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
            combos.append({
                "uid": "+".join(l["uid"] for l in legs),
                "legs": legs,
                "leg_odds": [l["playable"][bkey][0] for l in legs],
                "p": p, "odds": odds, "ev": ev_,
                "kelly": ev_ / (odds - 1) if odds > 1 else 0,
                "book": btitle, "book_key": bkey,
                "value": ev_ >= float(cfg["min_ev"]),
            })
    combos.sort(key=lambda c: (c["value"], c["ev"]), reverse=True)
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


def _weight(p, ev):
    return p * (1 + 10 * max(ev, 0))


def _allocate(items, amount_cents, min_cents):
    """Répartit amount_cents selon le poids de chaque pari, avec une mise minimale."""
    items = list(items)
    while items:
        total = sum(i["_w"] for i in items)
        raw = [amount_cents * i["_w"] / total for i in items]
        if min(raw) >= min_cents or len(items) == 1:
            break
        items.pop(raw.index(min(raw)))
    if not items:
        return []
    total = sum(i["_w"] for i in items)
    stakes = [int(amount_cents * i["_w"] / total // 10 * 10) for i in items]
    stakes[0] += amount_cents - sum(stakes)
    return list(zip(items, stakes))


def stake_plan(budget, mode, selections, cfg, sims=20000, seed=None):
    """Meilleur plan possible pour un budget : TOUJOURS une proposition si des matchs existent."""
    rules = MODES.get(mode, MODES["equilibre"])
    budget_c = int(round(budget * 100))
    min_c = max(10, int(round(float(cfg["min_stake"]) * 100)))
    pool = candidates(selections, cfg, rules["min_p"])
    pool.sort(key=lambda b: rank_key(b, cfg), reverse=True)

    singles, seen = [], set()
    for b in pool:
        if len(singles) >= rules["n_singles"]:
            break
        if b["event_id"] in seen:
            continue
        seen.add(b["event_id"])
        singles.append(dict(b, _w=_weight(b["p"], b["ev"])))

    combos = []
    if rules["n_combos"]:
        cpool = candidates(selections, cfg, max(0.30, rules["min_p"] - 0.05))
        fresh = [s for s in cpool if s["event_id"] not in seen]
        found = build_combos(fresh, cfg, rules["legs"], rules["combo_p"], limit=rules["n_combos"])
        if not found:
            found = build_combos(cpool, cfg, rules["legs"], rules["combo_p"], limit=rules["n_combos"])
        for c in found:
            combos.append(dict(c, _w=_weight(c["p"], c["ev"])))

    share = rules["combo_share"] if combos else 0.0
    if not singles and combos:
        share = 1.0
    c_amount = int(budget_c * share)
    s_alloc = _allocate(singles, budget_c - c_amount, min_c) if budget_c - c_amount > 0 else []
    c_alloc = _allocate(combos, c_amount, min_c) if c_amount > 0 else []
    if not s_alloc and singles and c_amount and not c_alloc:
        s_alloc = _allocate(singles, budget_c, min_c)

    items = [{"type": "single", "bet": b, "stake": c / 100} for b, c in s_alloc] + \
            [{"type": "combo", "combo": cb, "stake": c / 100} for cb, c in c_alloc]
    plan = {"budget": budget, "mode": mode, "items": items}
    if not items:
        return plan

    def odds_p_ev(it):
        x = it["bet"] if it["type"] == "single" else it["combo"]
        return x["odds"], x["p"], x["ev"]

    plan["staked"] = sum(it["stake"] for it in items)
    plan["expected"] = sum(it["stake"] * odds_p_ev(it)[2] for it in items)
    plan["max_win"] = sum(it["stake"] * (odds_p_ev(it)[0] - 1) for it in items)
    plan["n_value"] = sum(1 for it in items
                          if (is_value(it["bet"], cfg) if it["type"] == "single" else it["combo"]["value"]))

    rnd = random.Random(seed)
    probs = {}
    for it in items:
        legs = [it["bet"]] if it["type"] == "single" else it["combo"]["legs"]
        for l in legs:
            probs[l["uid"]] = l["p"]
    uids = list(probs)
    pos = zero = 0
    for _ in range(sims):
        won = {u: rnd.random() < probs[u] for u in uids}
        ret = 0.0
        for it in items:
            legs = [it["bet"]] if it["type"] == "single" else it["combo"]["legs"]
            if all(won[l["uid"]] for l in legs):
                ret += it["stake"] * odds_p_ev(it)[0]
        if ret - plan["staked"] > 0.001:
            pos += 1
        if ret == 0:
            zero += 1
    plan["p_profit"] = pos / sims
    plan["p_lose_all"] = zero / sims
    return plan


# --------------------------------------------------------------------------
# Formatage / sérialisation pour l'interface
# --------------------------------------------------------------------------

JOURS = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]


def fmt_date(dt):
    loc = dt.astimezone()
    today = datetime.now().astimezone().date()
    if loc.date() == today:
        return f"Aujourd'hui {loc:%H:%M}"
    if loc.date() == today + timedelta(days=1):
        return f"Demain {loc:%H:%M}"
    return f"{JOURS[loc.weekday()].capitalize()} {loc:%d/%m %H:%M}"


def ser_bet(b, cfg):
    return {
        "uid": b["uid"], "match": b["match"], "sport": b["sport"], "date": fmt_date(b["start"]),
        "ts": b["start"].timestamp(), "selection": b["selection"], "market": b["market"],
        "p": round(b["p"], 4), "fair": round(b["fair"], 2), "odds": b["odds"], "book": b["book"],
        "ev": round(b["ev"], 4), "n_books": b["n_books"], "suspect": b["suspect"],
        "value": is_value(b, cfg),
        "prices": [{"book": t, "odds": o, "playable": k in b["playable"]}
                   for k, (o, t) in sorted(b["all_prices"].items(), key=lambda kv: -kv[1][0])],
    }


def ser_combo(c):
    return {
        "uid": c["uid"], "odds": round(c["odds"], 2), "p": round(c["p"], 4), "ev": round(c["ev"], 4),
        "book": c["book"], "value": c["value"],
        "legs": [{"selection": l["selection"], "match": l["match"], "date": fmt_date(l["start"]),
                  "sport": l["sport"], "odds": lo, "p": round(l["p"], 4)}
                 for l, lo in zip(c["legs"], c["leg_odds"])],
    }


def ser_plan(plan, cfg):
    out = {k: plan.get(k) for k in ("budget", "mode", "staked", "expected", "max_win", "n_value",
                                     "p_profit", "p_lose_all")}
    out["items"] = []
    for it in plan["items"]:
        if it["type"] == "single":
            d = ser_bet(it["bet"], cfg)
            d.update(type="single", stake=it["stake"], gain=round(it["stake"] * (it["bet"]["odds"] - 1), 2))
        else:
            d = ser_combo(it["combo"])
            d.update(type="combo", stake=it["stake"], gain=round(it["stake"] * (it["combo"]["odds"] - 1), 2))
        out["items"].append(d)
    return out


# --------------------------------------------------------------------------
# Pont Python <-> interface (pywebview)
# --------------------------------------------------------------------------

def resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


class Api:
    """Méthodes appelées depuis l'interface (window.pywebview.api.*)."""

    def __init__(self, start_loop=True):
        self._lock = threading.RLock()
        self._cfg = load_config()
        self._events, self._is_demo, self._quota = [], False, None
        self._errors, self._warning = [], ""
        self._last_fetch, self._next_refresh, self._fetching = 0.0, time.time(), False
        self._sel, self._picks, self._prob, self._combos = [], [], [], []
        self._rev = 0
        self._update = None          # {"version", "url"}
        self._update_state = ""      # "", "downloading", "error:..."
        self._dl_progress = 0.0
        self._window = None
        cache = load_cache()
        if cache and cache.get("events") and self._cfg["api_key"] and not self._cfg["demo"]:
            self._events, self._quota = cache["events"], cache.get("quota")
            self._last_fetch = cache.get("ts", 0)
            self._next_refresh = self._last_fetch + float(self._cfg["refresh_minutes"]) * 60
            self._reanalyze()
        if start_loop:
            threading.Thread(target=self._loop, daemon=True).start()

    # ---- boucle de fond ----
    def _loop(self):
        last_an, next_upd = time.time(), time.time() + 4
        while True:
            now = time.time()
            if not self._fetching and now >= self._next_refresh:
                self._do_refresh()
            if now - last_an > 60:
                last_an = now
                self._reanalyze()
            if now >= next_upd:
                next_upd = now + 6 * 3600
                self._check_update()
            time.sleep(1)

    def _do_refresh(self):
        with self._lock:
            if self._fetching:
                return
            self._fetching = True
            self._rev += 1
            cfg = dict(self._cfg)
        demo = cfg["demo"] or not cfg["api_key"].strip()
        try:
            if demo:
                time.sleep(0.5)
                events, quota, errors = demo_events(), None, []
            else:
                events, quota, errors = fetch_odds(cfg)
        except Exception as e:
            events, quota, errors = None, None, [str(e)]
        with self._lock:
            now = time.time()
            self._errors = errors
            if events is not None and (events or not errors):
                self._events, self._is_demo, self._last_fetch = events, demo, now
                if quota:
                    self._quota = quota
                if not demo:
                    save_cache(events, self._quota)
            self._next_refresh = now + (15 if demo else float(cfg["refresh_minutes"])) * 60
            self._fetching = False
            self._reanalyze()

    def _reanalyze(self):
        with self._lock:
            try:
                cfg = self._cfg
                present = {bk.get("key") for ev in self._events for bk in ev.get("bookmakers", [])}
                mine = {b.strip().lower() for b in cfg.get("bookmakers", "").split(",") if b.strip()}
                self._warning = ""
                if mine and present and not (mine & present):
                    cfg = dict(cfg, bookmakers="")
                    self._warning = ("Betclic n'a pas encore publié de cotes pour ces matchs : cotes d'autres "
                                     "bookmakers affichées à titre indicatif.")
                self._sel = analyze(self._events, cfg)
                self._picks = top_picks(self._sel, cfg)
                self._prob = most_probable(self._sel, cfg)
                vpool = [s for s in self._sel if is_value(s, cfg)]
                value_combos = build_combos(vpool, cfg, int(cfg["max_legs"]), float(cfg["min_combo_prob"]))
                other = build_combos(candidates(self._sel, cfg, 0.40), cfg, int(cfg["max_legs"]),
                                     float(cfg["min_combo_prob"]))
                seen = {c["uid"] for c in value_combos}
                self._combos = (value_combos + [c for c in other if c["uid"] not in seen])[:40]
            except Exception as e:
                self._errors = [f"Erreur d'analyse : {e!r}"]
            self._rev += 1

    def _check_update(self):
        try:
            res = check_update()
        except Exception:
            return None
        with self._lock:
            self._update = {"version": res[0], "url": res[1]} if res else None
            self._rev += 1
        return self._update

    # ---- appelées par l'interface ----
    def get_status(self):
        with self._lock:
            upcoming = 0
            now_utc = datetime.now(timezone.utc)
            for ev in self._events:
                try:
                    if parse_time(ev["commence_time"]) > now_utc:
                        upcoming += 1
                except (KeyError, ValueError):
                    pass
            return {
                "rev": self._rev, "version": APP_VERSION, "demo": self._is_demo,
                "fetching": self._fetching,
                "last_fetch": datetime.fromtimestamp(self._last_fetch).strftime("%d/%m à %H:%M")
                if self._last_fetch else None,
                "next_in": max(0, int(self._next_refresh - time.time())),
                "quota": (self._quota or {}).get("remaining"),
                "errors": self._errors[:3], "warning": self._warning,
                "n_events": len(self._events), "n_upcoming": upcoming,
                "n_analyzed": len({s["event_id"] for s in self._sel}),
                "n_value": sum(1 for s in self._picks if is_value(s, self._cfg)),
                "has_key": bool(self._cfg["api_key"].strip()),
                "update": self._update, "update_state": self._update_state,
                "dl_progress": self._dl_progress, "frozen": bool(getattr(sys, "frozen", False)),
            }

    def get_data(self):
        with self._lock:
            cfg = self._cfg
            return {
                "picks": [ser_bet(b, cfg) for b in self._picks],
                "probable": [ser_bet(b, cfg) for b in self._prob],
                "combos": [ser_combo(c) for c in self._combos],
            }

    def refresh(self):
        if not self._fetching:
            threading.Thread(target=self._do_refresh, daemon=True).start()
        return True

    def compute_plan(self, budget, mode):
        try:
            budget = float(str(budget).replace(",", ".").replace("€", "").strip())
        except ValueError:
            return {"error": "Montant invalide"}
        if budget <= 0:
            return {"error": "Montant invalide"}
        with self._lock:
            sel, cfg = list(self._sel), dict(self._cfg)
        if self._warning:
            cfg["bookmakers"] = ""
        return ser_plan(stake_plan(budget, mode, sel, cfg), cfg)

    def get_config(self):
        with self._lock:
            cfg = dict(self._cfg)
        cfg["_sports"] = [{"key": k, "label": v} for k, v in SPORTS.items()]
        cfg["_credits_month"] = credits_per_month(self._cfg)
        return cfg

    def estimate(self, cfg):
        try:
            c = dict(self._cfg)
            c.update({k: v for k, v in cfg.items() if k in DEFAULT_CONFIG})
            return {"per": credits_per_refresh(c), "month": credits_per_month(c)}
        except Exception:
            return {"per": 0, "month": 0}

    def save_config(self, new):
        cfg = dict(self._cfg)
        for k, default in DEFAULT_CONFIG.items():
            if k not in new:
                continue
            v = new[k]
            try:
                if isinstance(default, bool):
                    v = bool(v)
                elif isinstance(default, int):
                    v = int(float(str(v).replace(",", ".")))
                elif isinstance(default, float):
                    v = float(str(v).replace(",", "."))
                elif isinstance(default, list):
                    v = [x for x in v if isinstance(x, str)]
                else:
                    v = str(v).strip()
            except ValueError:
                return {"ok": False, "error": f"Valeur invalide : {k}"}
            cfg[k] = v
        source_changed = any(cfg[k] != self._cfg[k] for k in
                             ("api_key", "source_books", "regions", "markets", "sports", "extra_sports", "demo"))
        with self._lock:
            self._cfg = cfg
            save_config(cfg)
            if not source_changed:
                self._next_refresh = self._last_fetch + float(cfg["refresh_minutes"]) * 60
        if source_changed:
            self.refresh()
        else:
            self._reanalyze()
        return {"ok": True}

    def check_updates(self):
        res = self._check_update()
        return {"update": res, "version": APP_VERSION}

    def install_update(self):
        upd = self._update
        if not upd or self._update_state == "downloading":
            return False
        if not getattr(sys, "frozen", False):
            self._update_state = "error:mise à jour possible seulement depuis le .exe"
            return False

        def worker():
            try:
                self._update_state = "downloading"

                def prog(p):
                    self._dl_progress = p
                    self._rev += 1
                path = download_update(upd["url"], prog)
                install_update_and_restart(path)
                time.sleep(0.3)
                try:
                    if self._window:
                        self._window.destroy()
                finally:
                    os._exit(0)
            except Exception as e:
                self._update_state = f"error:{e}"
                self._rev += 1

        threading.Thread(target=worker, daemon=True).start()
        return True

    def open_url(self, url):
        import webbrowser
        if str(url).startswith("https://"):
            webbrowser.open(url)
        return True


def selftest():
    """Vérifie que tout est bien embarqué dans le .exe (utilisé par GitHub Actions)."""
    import webview  # noqa: F401
    assert os.path.exists(resource_path(os.path.join("ui", "index.html"))), "ui/index.html manquant"
    cfg = dict(DEFAULT_CONFIG)
    sel = analyze(demo_events(1), cfg)
    plan = stake_plan(10, "equilibre", sel, cfg, sims=500, seed=1)
    assert plan["items"], "plan vide"
    json.dumps(ser_plan(plan, cfg))
    return 0


def main():
    if "--selftest" in sys.argv:
        try:
            sys.exit(selftest())
        except Exception as e:
            with open(os.path.join(tempfile.gettempdir(), "betscope_selftest.txt"), "w") as f:
                f.write(repr(e))
            sys.exit(1)
    import webview
    api = Api()
    window = webview.create_window(
        f"{APP_NAME} {APP_VERSION}", url=resource_path(os.path.join("ui", "index.html")), js_api=api,
        width=1340, height=860, min_size=(1080, 700), background_color="#0a0c11", text_select=True)
    api._window = window
    webview.start(debug=False)


if __name__ == "__main__":
    main()
