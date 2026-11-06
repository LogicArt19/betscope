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
import math
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

import markets as mk
import model as modelmod
import stats as statsmod
from history import History
from datetime import datetime, timedelta, timezone

APP_NAME = "BetScope"
APP_VERSION = "2.1.0"
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
    "source_books": "",  # optionnel : Pinnacle + 9 bookmakers sont toujours utilisés
    "markets": "h2h,totals",
    "refresh_minutes": 720,
    "bookmakers": "betclic_fr",   # là où tu paries
    "cfg_version": 5,
    "min_ev": 0.02,
    "max_ev": 0.15,
    "min_odds": 1.30,
    "max_odds": 6.00,
    "min_books": 4,
    "horizon_hours": 168,
    "max_legs": 3,
    "min_combo_prob": 0.15,
    "min_stake": 0.50,
    "kelly_fraction": 0.25,
    "demo": False,
    "fd_key": "",            # clé football-data.org (gratuite : forme, classement, journée, face-à-face)
    "football_key": "",      # clé API-Football (payante pour la saison en cours : blessés, compos)
    "stats_weight": 0.25,    # part du modèle forme/stats dans la proba finale (0 = cotes seules)
    "day_filter": "all",
    "web_info": True,
    "auto_refresh": True,
    "bankroll": 0.0,         # capital de départ (0 = pas de suivi)
    "daily_max_pct": 10.0,   # mise max par jour, en % du capital actuel
    "stop_loss": 0.0,        # perte max par jour en € (0 = pas de limite)    # l'appli choisit seule la fréquence pour rester dans l'offre gratuite        # recherche web automatique (ESPN) : absents, compos, actus
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
    if cfg.get("cfg_version", 0) < 5:  # 1.6.3 : réglages automatiques, rien à configurer
        cfg["source_books"] = ""
        cfg["min_books"] = 4
        cfg["auto_refresh"] = True
        cfg["bookmakers"] = cfg.get("bookmakers") or "betclic_fr"
        cfg["cfg_version"] = 5
    if float(cfg.get("min_books", 4)) > 5:
        cfg["min_books"] = 4
    if cfg.get("cfg_version", 0) < 4:  # 1.4.0 : on regarde les 7 prochains jours
        cfg["horizon_hours"] = max(float(cfg.get("horizon_hours", 168)), 168)
        cfg["cfg_version"] = 4
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


CONSENSUS_BOOKS = ["pinnacle", "betclic_fr", "winamax_fr", "unibet_fr", "pmu_fr", "parionssport_fr",
                   "netbet_fr", "marathonbet", "onexbet", "sport888"]


def effective_source_books(cfg):
    """Toujours assez de bookmakers pour calculer la vraie proba : Pinnacle + tes bookmakers
    + la liste de référence, 10 max (= 1 crédit par marché)."""
    mine = [b.strip().lower() for b in cfg.get("bookmakers", "").split(",") if b.strip()]
    extra = [b.strip().lower() for b in cfg.get("source_books", "").split(",") if b.strip()]
    out = []
    for b in ["pinnacle"] + mine + extra + CONSENSUS_BOOKS:
        if b and b not in out:
            out.append(b)
    return out[:10]


def credits_per_refresh(cfg):
    books = effective_source_books(cfg)
    if books:
        n_regions = (len(books) + 9) // 10
    else:
        n_regions = len([r for r in cfg["regions"].split(",") if r.strip()]) or 1
    n_markets = len([m for m in cfg["markets"].split(",") if m.strip()]) or 1
    return len(sports_list(cfg)) * n_regions * n_markets


def auto_interval_minutes(cfg, remaining=None):
    """Intervalle de mise à jour pour ne pas dépasser le quota (500 crédits/mois, 20 gardés en réserve)."""
    per = max(1, credits_per_refresh(cfg))
    now = datetime.now()
    nxt = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1)
    days_left = max(1.0, (nxt - now).total_seconds() / 86400)
    try:
        budget = float(remaining) - 20 if remaining is not None else 480 * days_left / 30
    except (TypeError, ValueError):
        budget = 480 * days_left / 30
    if budget <= per:
        return 24 * 60  # quota presque vide : 1 fois par jour (les crédits reviennent le 1er du mois)
    refreshes = budget / per
    return int(min(72 * 60, max(60, days_left * 24 * 60 / refreshes)))


def refresh_minutes(cfg, remaining=None):
    if cfg.get("auto_refresh", True):
        return auto_interval_minutes(cfg, remaining)
    return float(cfg["refresh_minutes"])


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
            params["bookmakers"] = ",".join(effective_source_books(cfg))
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
set PYINSTALLER_RESET_ENVIRONMENT=1
set _MEIPASS2=
set _PYI_APPLICATION_HOME_DIR=
set _PYI_ARCHIVE_FILE=
set _PYI_PARENT_PROCESS_LEVEL=
set _PYI_SPLASH_IPC=
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
copy /b "{exe}"+,, "{exe}" >nul 2>nul
ie4uinit.exe -show >nul 2>nul
start "" "{exe}"
del "%~f0"
"""
    with open(bat, "w", encoding="mbcs" if os.name == "nt" else "utf-8") as f:
        f.write(script)
    flags = 0x08000000 | 0x00000008  # CREATE_NO_WINDOW | DETACHED_PROCESS
    # environnement propre : sinon le nouveau .exe croit être un sous-processus de l'ancien
    # et cherche ses fichiers dans un dossier temporaire déjà supprimé (« Failed to load Python DLL »)
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("_MEI", "_PYI"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    subprocess.Popen(["cmd", "/c", bat], creationflags=flags, close_fds=True, env=env)


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


def data_quality(st):
    """0..1 : part des données disponibles pour ce match (forme x2, modèle, classement, face-à-face, absents vérifiés)."""
    if not st:
        return 0.0
    fh, fa = st.get("form_home"), st.get("form_away")
    th, ta = st.get("table_home"), st.get("table_away")
    web_ok = any(x.get("ok") for x in (st.get("sources") or []) if x.get("name", "").startswith("Absents"))
    items = (fh, fa, st.get("model") or (fh and fa), th and ta, st.get("h2h_sum"), web_ok)
    return sum(bool(x) for x in items) / len(items)


def analyse_points(st, side, name, point, mkey, p_market, p_model, home, away):
    """Points pour / contre lisibles + score de confiance pour une sélection."""
    pros, cons = [], []
    score = 0.0
    fh, fa = st.get("form_home") or {}, st.get("form_away") or {}
    th, ta = st.get("table_home") or {}, st.get("table_away") or {}
    ih, ia = st.get("inj_home") or [], st.get("inj_away") or []
    out = lambda l: [i for i in l if "missing" in str(i.get("type", "")).lower()]

    def form_line(team, f):
        if not f:
            return None
        v = f["form"].count("V")
        return f"{team} : {v} victoire{'s' if v > 1 else ''} sur les {len(f['form'])} derniers ({f['form']})"

    if side in ("home", "away"):
        me, opp = (home, away) if side == "home" else (away, home)
        fm, fo = (fh, fa) if side == "home" else (fa, fh)
        tm, to = (th, ta) if side == "home" else (ta, th)
        im, io = (out(ih), out(ia)) if side == "home" else (out(ia), out(ih))
        if fm:
            d = fm["ppg5"] - (fo or {}).get("ppg5", fm["ppg5"])
            (pros if d >= 0.4 else cons if d <= -0.4 else pros if fm["ppg5"] >= 2 else cons if fm["ppg5"] <= 1 else []).append(
                form_line(me, fm)) if (abs(d) >= 0.4 or fm["ppg5"] >= 2 or fm["ppg5"] <= 1) else None
            score += 8 * d
            if fm.get("wins_streak", 0) >= 3:
                pros.append(f"{me} : {fm['wins_streak']} victoires de suite")
                score += 4
            if fm.get("loss_streak", 0) >= 2:
                cons.append(f"{me} : {fm['loss_streak']} défaites de suite")
                score -= 5
        if fo:
            if fo.get("wins_streak", 0) >= 3:
                cons.append(f"⚠ {opp} reste sur {fo['wins_streak']} victoires de suite")
                score -= 4 + 2 * min(4, fo["wins_streak"] - 3)
            elif fo.get("unbeaten", 0) >= 6:
                cons.append(f"⚠ {opp} invaincu depuis {fo['unbeaten']} matchs")
                score -= 4
            if fo.get("loss_streak", 0) >= 2:
                pros.append(f"{opp} : {fo['loss_streak']} défaites de suite")
                score += 4
            elif fo.get("winless", 0) >= 4:
                pros.append(f"{opp} sans victoire depuis {fo['winless']} matchs")
                score += 3
        if tm and to and tm.get("rank") and to.get("rank"):
            gap = to["rank"] - tm["rank"]
            if gap >= 6:
                pros.append(f"Classement : {me} {tm['rank']}e contre {opp} {to['rank']}e")
            elif gap <= -6:
                cons.append(f"Classement : {me} {tm['rank']}e contre {opp} {to['rank']}e")
            score += max(-6, min(6, gap * 0.5))
        # forme à domicile / à l'extérieur cette saison
        venue_me = (fm or {}).get("home_ppg" if side == "home" else "away_ppg")
        venue_opp = (fo or {}).get("away_ppg" if side == "home" else "home_ppg")
        if venue_me is not None and venue_opp is not None:
            lab_me = "à domicile" if side == "home" else "à l'extérieur"
            lab_opp = "à l'extérieur" if side == "home" else "à domicile"
            rec_me = fm["home_rec" if side == "home" else "away_rec"]
            rec_opp = fo["away_rec" if side == "home" else "home_rec"]
            d = venue_me - venue_opp
            txt = f"{me} {lab_me} : {rec_me} · {opp} {lab_opp} : {rec_opp}"
            if d >= 0.5:
                pros.append(txt)
            elif d <= -0.5:
                cons.append(txt)
            score += max(-6, min(6, 5 * d))
        # fatigue : jours de repos
        if fm and fo and fm.get("rest_days") is not None and fo.get("rest_days") is not None:
            if fm["rest_days"] < 3.5 <= fo["rest_days"]:
                cons.append(f"Fatigue : {me} a joué il y a {fm['rest_days']:.0f} jours, {opp} plus reposé")
                score -= 3
            elif fo["rest_days"] < 3.5 <= fm["rest_days"]:
                pros.append(f"Fatigue : {opp} a joué il y a {fo['rest_days']:.0f} jours, {me} plus reposé")
                score += 3
        # confrontations directes
        hs = st.get("h2h_sum")
        if hs and hs["n"] >= 3:
            w, l = (hs["w"], hs["l"]) if side == "home" else (hs["l"], hs["w"])
            txt = f"Face-à-face : {me} {w}V {hs['d']}N {l}D sur les {hs['n']} derniers"
            if w - l >= 2:
                pros.append(txt)
            elif l - w >= 2:
                cons.append(txt)
            score += max(-5, min(5, 1.5 * (w - l)))
        def inj_txt(lst):
            return ", ".join((f"⭐ {i['player']} ({i['goals']} buts)" if i.get("key") else i["player"]) for i in lst[:5])

        def inj_w(lst):
            return sum(i.get("weight", 0.025) for i in lst)
        if im:
            cons.append(f"{len(im)} absent{'s' if len(im) > 1 else ''} chez {me} : " + inj_txt(im))
            score -= min(14, 90 * inj_w(im))
        if io:
            pros.append(f"{len(io)} absent{'s' if len(io) > 1 else ''} chez {opp} : " + inj_txt(io))
            score += min(14, 90 * inj_w(io))
        # rotation (autre match à moins de 3,5 jours) et enjeu
        rm, ro = st.get("rotation_home" if side == "home" else "rotation_away"), \
            st.get("rotation_away" if side == "home" else "rotation_home")
        def rot_txt(t, r):
            quand = f"dans {r['days']:.0f} j" if r["days"] > 0 else f"il y a {-r['days']:.0f} j"
            return f"Rotation possible : {t} joue {r['comp']} {quand}"
        if rm:
            cons.append(rot_txt(me, rm))
            score -= 3
        if ro:
            pros.append(rot_txt(opp, ro))
            score += 3
        sm, so = st.get("stake_home" if side == "home" else "stake_away"), \
            st.get("stake_away" if side == "home" else "stake_home")
        if sm and not so:
            pros.append(f"Enjeu : {me} en {sm}")
            score += 2
        elif so and not sm:
            cons.append(f"Enjeu : {opp} en {so}")
            score -= 2
    elif side == "draw":
        if fh and fa:
            gap = abs(fh["ppg5"] - fa["ppg5"])
            (pros if gap < 0.5 else cons).append(
                f"Forme {'proche' if gap < 0.5 else 'très différente'} : {home} {fh['form']} / {away} {fa['form']}")
            score += 4 if gap < 0.5 else -4
    elif mkey == "totals" and fh and fa:
        g5 = (fh["gf5"] + fh["ga5"]) / max(1, len(fh["form"])) + (fa["gf5"] + fa["ga5"]) / max(1, len(fa["form"]))
        g5 /= 2
        txt = f"Moyenne de buts sur les 5 derniers matchs : {g5:.1f} par match".replace(".", ",")
        good = (g5 > float(point)) if name == "Over" else (g5 < float(point))
        (pros if good else cons).append(txt)
        score += 5 if good else -5
        if abs(float(point) - 2.5) < 0.01:
            o = (fh["over25"] + fa["over25"]) / 2
            txt = f"Plus de 2,5 buts dans {o * 100:.0f} % des 10 derniers matchs des deux équipes"
            good = o >= 0.55 if name == "Over" else o <= 0.45
            bad = o <= 0.45 if name == "Over" else o >= 0.55
            (pros if good else cons if bad else []).append(txt)
            score += 4 if good else (-4 if bad else 0)
        hs = st.get("h2h_sum")
        if hs and hs["n"] >= 3:
            good = hs["avg_goals"] > float(point) if name == "Over" else hs["avg_goals"] < float(point)
            (pros if good else cons).append(
                f"Face-à-face : {hs['avg_goals']:.1f} buts par match en moyenne ({hs['n']} matchs)".replace(".", ","))
            score += 3 if good else -3
    wx = st.get("weather")
    if mkey == "totals" and wx and (wx.get("rain", 0) >= 3 or wx.get("wind", 0) >= 40):
        txt = f"Météo à {wx['city']} : {wx['rain']:g} mm de pluie, vent {wx['wind']:.0f} km/h (moins de buts en général)"
        (pros if name == "Under" else cons).append(txt)
        score += 4 if name == "Under" else -4
    if p_model is not None:
        d = p_model - p_market
        txt = f"Modèle forme : {p_model * 100:.0f} % contre {p_market * 100:.0f} % pour le marché"
        if d >= 0.03:
            pros.append(txt)
        elif d <= -0.03:
            cons.append(txt)
        score += 120 * d
    # objectivité : moins on a de données, plus l'avis reste neutre
    cov = sum(bool(x) for x in (fh, fa, st.get("model"), th and ta, st.get("h2h_sum"), st.get("sources") is not None))
    score *= 0.35 + 0.65 * cov / 6
    mdl = st.get("model")
    contra = bool(p_model is not None and p_model < p_market - 0.07) or \
        any(c.startswith("⚠") for c in cons) and score < -6
    return {"q": round(data_quality(st), 2), "round": st.get("round_label", ""), "p_model": p_model,
            "pros": [p for p in pros if p],
            "cons": [c for c in cons if c], "score": score, "contra": contra,
            "xg": [round(mdl["xg_home"], 2), round(mdl["xg_away"], 2)] if mdl else None,
            "form_home": (fh or {}).get("form", ""), "form_away": (fa or {}).get("form", "")}


def form_proxy(st, side, mkey, name, point):
    """Proba approximative à partir de la forme récente (et du face-à-face) quand il n'y a pas de modèle."""
    fh, fa = st.get("form_home"), st.get("form_away")
    if not (fh and fa):
        return None
    if mkey == "h2h" and side:
        a, b = fh["ppg5"] + 0.4, fa["ppg5"] + 0.1
        d = 0.27
        probs = {"home": (1 - d) * a / (a + b), "draw": d, "away": (1 - d) * b / (a + b)}
        hs = st.get("h2h_sum")
        if hs and hs["n"] >= 3:
            n = hs["n"] + 3
            hp = {"home": (hs["w"] + 1) / n, "draw": (hs["d"] + 1) / n, "away": (hs["l"] + 1) / n}
            probs = {k: 0.8 * probs[k] + 0.2 * hp[k] for k in probs}
        return probs[side]
    if mkey == "totals" and point is not None and abs(float(point) - 2.5) < 0.01:
        o = (fh["over25"] + fa["over25"]) / 2
        o = 0.5 + 0.6 * (o - 0.5)  # 5 matchs : on reste prudent
        return o if name == "Over" else 1 - o
    return None


def neutral_view(st, sel):
    """Analyse objective du match : chaque source séparément, arguments des 2 équipes, fiabilité, risques."""
    h2h = {x["selection"]: x for x in sel if x.get("cat") == "1n2" and not x.get("estimated")}
    home = st.get("home") or ""
    away = st.get("away") or ""
    if sel and not home:
        home, away = sel[0]["match"].split(" - ", 1) if " - " in sel[0]["match"] else (sel[0]["match"], "")
    def trio(key):
        try:
            return [h2h[f"Victoire {home_o}"][key], h2h["Match nul"][key], h2h[f"Victoire {away_o}"][key]]
        except KeyError:
            return None
    # noms côté cotes (peuvent différer des noms des stats)
    names = [k[len("Victoire "):] for k in h2h if k.startswith("Victoire ")]
    m_home = sel[0]["match"].split(" - ")[0] if sel else home
    home_o = m_home
    away_o = next((n for n in names if n != home_o), away)
    rows = []
    mk_ = trio("p_market")
    if mk_:
        rows.append({"src": "Marché (Pinnacle + 9 bookmakers)", "p": mk_})
    mdl = st.get("model")
    if mdl:
        rows.append({"src": "Modèle forme (saison, récent pondéré)", "p": [mdl["home"], mdl["draw"], mdl["away"]]})
    fh, fa = st.get("form_home"), st.get("form_away")
    if fh and fa:
        # forme pure : points par match sur les 5 derniers -> répartition simple
        a, b = fh["ppg5"] + 0.4, fa["ppg5"] + 0.1
        d = 0.27
        rows.append({"src": "Forme des 5 derniers matchs", "p": [(1 - d) * a / (a + b), d, (1 - d) * b / (a + b)]})
    hs = st.get("h2h_sum")
    if hs and hs["n"] >= 3:
        n = hs["n"] + 3
        rows.append({"src": f"Confrontations directes ({hs['n']})",
                     "p": [(hs["w"] + 1) / n, (hs["d"] + 1) / n, (hs["l"] + 1) / n]})
    fin = trio("p")
    if fin:
        rows.append({"src": "BetScope (final)", "p": fin, "final": True})
    # arguments neutres pour chaque équipe
    adv_h = analyse_points(st, "home", home_o, None, "h2h", 0, None, home_o, away_o)["pros"] if st else []
    adv_a = analyse_points(st, "away", away_o, None, "h2h", 0, None, home_o, away_o)["pros"] if st else []
    srcs = st.get("sources") or []
    ok = sum(1 for x in srcs if x["ok"])
    rel = 25 + int(75 * ok / max(1, len(srcs))) if srcs else 25
    risks = []
    if mk_ and mdl and abs(mk_[0] - mdl["home"]) > 0.12:
        risks.append("Le marché et la forme ne sont pas d'accord : match plus incertain que d'habitude")
        rel -= 10
    if not st.get("lineups"):
        risks.append("Compos pas encore connues : un titulaire absent peut tout changer")
    if not (fh and fa):
        risks.append("Forme récente indisponible pour au moins une équipe")
    if fh and fh.get("played", 0) < 6 or fa and fa.get("played", 0) < 6:
        risks.append("Peu de matchs joués cette saison : stats moins fiables")
    if mk_ and max(mk_) < 0.45:
        risks.append("Aucun favori net : les 3 issues restent possibles")
    risks.append("Le football reste imprévisible : même à 70 %, un pari perd 3 fois sur 10")
    return {"rows": rows, "labels": [home_o, "Nul", away_o], "adv_home": adv_h, "adv_away": adv_a,
            "reliability": max(10, min(100, rel)), "risks": risks}


def analyze(events, cfg, now=None, info=None):
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
        st = (info or {}).get(ev.get("id"))
        mdl = (st or {}).get("model")
        w_stats = float(cfg.get("stats_weight", 0) or 0)

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
                p_market = a["pw"] / a["w"]
                p = p_market
                side = None
                if mkey == "h2h":
                    side = "home" if a["name"] == home else ("away" if a["name"] == away else "draw")
                p_model = None
                if mdl:
                    if side:
                        p_model = mdl.get(side)
                    elif mkey == "totals" and a["point"] is not None:
                        po = modelmod.p_over(mdl, float(a["point"]))
                        p_model = po if a["name"] == "Over" else (1 - po if po is not None else None)
                w_eff = w_stats
                if p_model is None and st:
                    # pas de modèle saison (pas de clé) : on utilise quand même la forme récente + face-à-face
                    p_model = form_proxy(st, side, mkey, a["name"], a["point"])
                    w_eff = w_stats * 0.6
                if p_model is not None and 0 < w_eff <= 0.6:
                    p = (1 - w_eff) * p_market + w_eff * p_model
                stat_sum = analyse_points(st, side, a["name"], a["point"], mkey, p_market, p_model, home, away) \
                    if st else None
                playable = {k: v for k, v in a["prices"].items()
                            if (k in allowed if allowed else k not in EXCHANGES)}
                if not playable:
                    continue
                best_key = max(playable, key=lambda k: playable[k][0])
                odds, book_title = playable[best_key]
                ev_ = p * odds - 1
                kelly = ev_ / (odds - 1) if odds > 1 else 0.0
                conf = 50 + 250 * ev_
                if stat_sum:
                    conf += stat_sum["score"]
                if mkey == "h2h":
                    skey = {"home": "r1", "draw": "rN", "away": "r2"}.get(side)
                elif mkey == "totals" and a["point"] is not None:
                    skey = f"{'o' if a['name'] == 'Over' else 'u'}{float(a['point']):g}"
                else:
                    skey = None
                out.append({
                    "key": skey, "sport_key": sport_key, "home": home, "away": away,
                    "cat": "1n2" if mkey == "h2h" else ("total" if mkey == "totals" else "hcp"),
                    "estimated": False,
                    "p_market": p_market,
                    "stats": stat_sum,
                    "conf": int(max(1, min(99, round(conf)))),
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
                    "suspect": p_market * odds - 1 > float(cfg["max_ev"]),
                })
        add_derived(out, ev, start, match, home, away, sport_key, st, mdl, w_stats, allowed)
    return out


def add_derived(out, ev, start, match, home, away, sport_key, st, mdl, w_stats, allowed):
    """Ajoute tous les autres paris Betclic du match (cotes estimées)."""
    eid = ev.get("id")
    mine = [s for s in out if s["event_id"] == eid and not s.get("estimated")]
    h2h = {s["selection"]: s for s in mine if s["cat"] == "1n2"}
    try:
        p1 = h2h[f"Victoire {home}"]["p_market"]
        p2 = h2h[f"Victoire {away}"]["p_market"]
        pn = h2h["Match nul"]["p_market"]
    except KeyError:
        return
    over = next((s for s in mine if s["cat"] == "total" and s["selection"].startswith("Plus de 2,5")), None)
    lh, la = mk.fit_lambdas(p1, pn, p2, over["p_market"] if over else None)
    lh_m, la_m = (mdl["xg_home"], mdl["xg_away"]) if mdl else (None, None)
    w = w_stats if mdl and 0 < w_stats <= 0.6 else 0.0
    fh, fa = (1 - w) * lh + w * (lh_m or lh), (1 - w) * la + w * (la_m or la)
    ref = mine[0]
    # bookmaker où l'on parie (Betclic) : même nom que sur les vrais marchés
    bkey = ref["book_key"]
    btitle = ref["book"]
    rows_m = {r[4]: r for r in mk.derive(lh, la, home, away, has_total25=over is not None)}
    rows_f = mk.derive(fh, fa, home, away, has_total25=over is not None)
    if st:
        inj_h = [i.get("player", "") for i in (st.get("inj_home") or [])]
        inj_a = [i.get("player", "") for i in (st.get("inj_away") or [])]
        gh, mh = st.get("goals_home") or (0, 0)
        ga, ma = st.get("goals_away") or (0, 0)
        rows_f += mk.player_markets(fh, st.get("scorers_home") or [], gh, mh, inj_h)
        rows_f += mk.player_markets(fa, st.get("scorers_away") or [], ga, ma, inj_a)
        for r in mk.player_markets(lh, st.get("scorers_home") or [], gh, mh, inj_h) + \
                mk.player_markets(la, st.get("scorers_away") or [], ga, ma, inj_a):
            rows_m[r[4]] = r
    for cat, market, sel, p, key in rows_f:
        if p <= 0.02 or p >= 0.985:
            continue
        p_market = rows_m.get(key, (None, None, None, p))[3]
        odds, min_odds = mk.estimated_odds(cat, p_market)
        ev_ = p * odds - 1
        dstat, conf = None, 50
        if st:
            q = data_quality(st)
            d = p - p_market  # ce que la forme / les stats ajoutent ou retirent au marché
            conf = int(max(1, min(99, round(50 + 250 * d * (0.35 + 0.65 * q)))))
            fh_, fa_ = st.get("form_home") or {}, st.get("form_away") or {}
            pros_, cons_ = [], []
            if d >= 0.02:
                pros_.append(f"Forme et stats : {p * 100:.0f} % contre {p_market * 100:.0f} % pour le marché")
            elif d <= -0.02:
                cons_.append(f"Forme et stats : {p * 100:.0f} % contre {p_market * 100:.0f} % pour le marché")
            dstat = {"q": round(q, 2), "round": st.get("round_label", ""), "p_model": p, "pros": pros_,
                     "cons": cons_, "score": conf - 50, "contra": d <= -0.05, "xg": None,
                     "form_home": fh_.get("form", ""), "form_away": fa_.get("form", "")}
        out.append({
            "key": key, "sport_key": sport_key, "home": home, "away": away,
            "cat": cat, "estimated": True, "min_odds": min_odds,
            "p_market": p_market, "stats": dstat, "conf": conf,
            "uid": f"{eid}|x|{key}", "event_id": eid, "match": match,
            "sport": SPORTS.get(sport_key, ev.get("sport_title", sport_key)), "start": start,
            "market": market, "selection": sel, "p": p, "fair": 1 / p, "odds": odds,
            "book": btitle, "book_key": bkey, "ev": ev_, "kelly": ev_ / (odds - 1) if odds > 1 else 0,
            "n_books": ref["n_books"], "playable": {bkey: (odds, btitle)}, "all_prices": {bkey: (odds, btitle)},
            "suspect": False,
        })




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
    return b["ev"] >= float(cfg["min_ev"]) and not b["suspect"] and not b.get("estimated")


def objective_ok(s, cfg):
    """Critères d'objectivité choisis par l'utilisateur (mode « Priorité à l'objectivité »)."""
    o = cfg.get("_obj")
    if not o or not o.get("on"):
        return True
    st = s.get("stats") or {}
    if s["p"] < float(o.get("min_p", 0.55)):
        return False
    if st.get("contra"):
        return False
    if s.get("conf", 50) < float(o.get("min_conf", 50)):
        return False
    if o.get("need_data", True) and st.get("q", 0) < 0.5:
        return False
    if o.get("no_est") and s.get("estimated"):
        return False
    return True


def candidates(selections, cfg, min_p=0.0):
    lo, hi = float(cfg["min_odds"]), float(cfg["max_odds"])
    types = cfg.get("_types")
    return [s for s in selections if (not types or s.get("cat") in types) and objective_ok(s, cfg)
            and lo <= s["odds"] <= hi and not s["suspect"] and s["p"] >= min_p
            and (s.get("estimated") or s["ev"] >= WORST_EV)]


def pro_score(b):
    """Gain attendu par euro misé, corrigé par la confiance de l'analyse."""
    sc = b["ev"] + (b.get("conf", 50) - 50) / 1000
    if (b.get("stats") or {}).get("contra"):
        sc -= 0.06
    return sc


def would_bet(b, cfg):
    """Ce que BetScope jouerait avec son propre argent : rentable ET confirmé par l'analyse."""
    st = b.get("stats") or {}
    return (is_value(b, cfg) and not st.get("contra") and b.get("conf", 50) >= 50
            and float(cfg["min_odds"]) <= b["odds"] <= float(cfg["max_odds"]))


def rank_key(b, cfg):
    """Value d'abord (triée par Kelly), puis les autres par espérance."""
    return (2 if would_bet(b, cfg) else (1 if is_value(b, cfg) else 0), pro_score(b))


def top_picks(selections, cfg, limit=40, min_p=0.33):
    """Value bets + meilleurs paris disponibles pour compléter."""
    vb = [s for s in selections if is_value(s, cfg)
          and float(cfg["min_odds"]) <= s["odds"] <= float(cfg["max_odds"])]
    vb.sort(key=lambda s: rank_key(s, cfg), reverse=True)
    seen = {s["uid"] for s in vb}
    rest = [s for s in candidates(selections, cfg, min_p) if s["uid"] not in seen]
    real = sorted([s for s in rest if not s.get("estimated")], key=pro_score, reverse=True)[:limit]
    est = sorted([s for s in rest if s.get("estimated")], key=pro_score, reverse=True)[:limit + 20]
    return vb + sorted(real + est, key=pro_score, reverse=True)


def most_probable(selections, cfg, limit=60):
    pr = candidates(selections, cfg)
    pr.sort(key=lambda s: s["p"], reverse=True)
    return pr[:limit]


def build_combos(pool, cfg, max_legs=3, min_prob=0.15, limit=40, pool_size=14, min_legs=2):
    """Combinés de 2..max_legs sélections, matchs différents, au MÊME bookmaker."""
    max_legs = max(2, min(8, max_legs))
    if max_legs > 4:
        pool_size = min(pool_size, 12)
    pool = sorted(pool, key=lambda b: rank_key(b, cfg), reverse=True)[:pool_size]
    combos = []
    for k in range(max(2, min_legs), max_legs + 1):
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
    for c in combos:
        c["conf"] = int(sum(l.get("conf", 50) for l in c["legs"]) / len(c["legs"]))
        c["contra"] = any((l.get("stats") or {}).get("contra") for l in c["legs"])
        c["would_bet"] = c["value"] and not c["contra"] and all(would_bet(l, cfg) or is_value(l, cfg) for l in c["legs"])
    combos.sort(key=lambda c: (c["would_bet"], c["value"], c["ev"] + (c["conf"] - 50) / 1000 - (0.06 if c["contra"] else 0)),
                reverse=True)
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
    ids = {x["uid"] for x in pool}
    # les paris que je jouerais moi-même passent en priorité, même un peu moins probables
    pool += [x for x in candidates(selections, cfg, rules["min_p"] - 0.12) if x["uid"] not in ids and would_bet(x, cfg)]
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
        legs_max = int(cfg.get("_max_legs") or rules["legs"])
        legs_min = int(cfg.get("_min_legs") or 2)
        found = build_combos(fresh, cfg, legs_max, rules["combo_p"] if legs_max <= 3 else 0.02,
                             limit=rules["n_combos"], min_legs=legs_min)
        if not found:
            found = build_combos(cpool, cfg, legs_max, rules["combo_p"] if legs_max <= 3 else 0.02,
                                 limit=rules["n_combos"], min_legs=legs_min)
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
    plan["n_mine"] = sum(1 for it in items
                         if (would_bet(it["bet"], cfg) if it["type"] == "single" else it["combo"].get("would_bet")))
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
# Mode objectif : "je mise X €, je veux gagner au moins Y €"
# --------------------------------------------------------------------------

def _book_items(selections, cfg, book, exclude_events=(), pool_size=22, min_p=0.25, by_prob=False):
    """Sélections jouables chez `book`, triées par intérêt. Les gros favoris (cote 1,02+) sont acceptés."""
    lo = 1.02
    items = []
    for s in selections:
        types = cfg.get("_types")
        if types and s.get("cat") not in types:
            continue
        if s["suspect"] or s["event_id"] in exclude_events or book not in s["playable"] or not objective_ok(s, cfg):
            continue
        o = s["playable"][book][0]
        if o < lo or s["p"] < min_p or (s["ev"] < WORST_EV and not s.get("estimated")):
            continue
        items.append((s, o))
    if by_prob:
        items.sort(key=lambda it: it[0]["p"] * _trust(it[0]), reverse=True)
    else:
        items.sort(key=lambda it: it[0]["p"] * it[1] * _trust(it[0]), reverse=True)
    return items  # tout est gardé : la recherche passe en revue chaque sélection de chaque match


def _trust(s):
    """Prudence : une cote estimée ou un pari contre la forme vaut un peu moins qu'il n'y paraît."""
    t = 1.0
    if s.get("estimated"):
        t *= 0.95
    st = s.get("stats") or {}
    if st.get("contra"):
        t *= 0.85
    c = s.get("conf", 50)
    t *= 1 + max(-0.08, min(0.05, (c - 50) / 600))
    return t


def best_ticket(items, need_odds, max_legs=6, max_odds=None, legs_limit=None, min_leg_p=0.0, min_legs=1):
    """Recherche exhaustive (programmation dynamique) sur TOUTES les sélections de TOUS les matchs :
    le ticket (1 sélection max par match) qui atteint la cote demandée avec la meilleure probabilité
    (pondérée par la fiabilité de l'analyse)."""
    lim = min(max_legs, legs_limit or max_legs)
    if lim < 1 or need_odds <= 1:
        return None
    L = math.log(need_odds)
    B = 400
    step = L / B
    by_ev = {}
    for s, o in items:
        if o <= 1.0 or (min_leg_p and s["p"] < min_leg_p):
            continue
        by_ev.setdefault(s["event_id"], []).append((s, o, math.log(max(1e-9, s["p"] * _trust(s))), math.log(o)))
    events = []
    for ev, opts in by_ev.items():
        # on ne garde que les options non dominées (pas une autre avec meilleure cote ET meilleure proba)
        opts.sort(key=lambda x: (-x[3], -x[2]))
        front, best_sc = [], -1e18
        for x in opts:
            if x[2] > best_sc + 1e-12:
                front.append(x)
                best_sc = x[2]
        events.append(front[:14])
    # état = (score, somme EXACTE des log-cotes, chemin) ; le seau ne sert qu'à comparer
    dp = [dict() for _ in range(lim + 1)]
    dp[0][0] = (0.0, 0.0, None)
    for opts in events:
        for k in range(lim - 1, -1, -1):
            if not dp[k]:
                continue
            nxt = dp[k + 1]
            for bkt, (sc, lo_sum, path) in list(dp[k].items()):
                for x in opts:
                    nl = lo_sum + x[3]
                    nb = min(B, int(nl / step + 1e-9))
                    nsc = sc + x[2]
                    cur = nxt.get(nb)
                    if cur is None or nsc > cur[0]:
                        nxt[nb] = (nsc, nl, (x, path))
    best = None
    for k in range(max(1, min_legs), lim + 1):
        cur = dp[k].get(B)
        if cur and (best is None or cur[0] > best[0] + 1e-12):
            best = cur
    if not best:
        return None
    legs, node = [], best[2]
    while node:
        legs.append(node[0])
        node = node[1]
    legs.reverse()
    o = math.prod(x[1] for x in legs)
    if max_odds and o > max_odds:
        return None
    p = math.prod(x[0]["p"] for x in legs)
    return {"legs": [x[0] for x in legs], "leg_odds": [x[1] for x in legs], "odds": o, "p": p, "ev": p * o - 1}


def favorites_ticket(items, need_odds, min_p, min_legs=1, max_legs=60, exclude_events=()):
    """1 favori par match, autant de matchs qu'il faut : on prend d'abord les favoris qui « coûtent »
    le moins de probabilité pour la cote qu'ils apportent, jusqu'à atteindre la cote nécessaire."""
    best = {}
    for s, o in items:
        if s["event_id"] in exclude_events or s["p"] < min_p or o <= 1.01:
            continue
        cost = -math.log(max(1e-9, s["p"] * _trust(s))) / math.log(o)
        cur = best.get(s["event_id"])
        if cur is None or cost < cur[0]:
            best[s["event_id"]] = (cost, s, o)
    order = sorted(best.values(), key=lambda x: x[0])
    L = math.log(need_odds)
    legs, tot = [], 0.0
    for cost, s, o in order:
        if tot >= L and len(legs) >= min_legs:
            break
        legs.append((s, o))
        tot += math.log(o)
    if tot < L - 1e-9 or len(legs) < min_legs or len(legs) > max_legs:
        return None
    # si la dernière sélection dépasse beaucoup la cote, on essaie une plus sûre pour la remplacer
    if len(legs) > 1:
        rest = tot - math.log(legs[-1][1])
        last_ev = legs[-1][0]["event_id"]
        alts = [(s, o) for s, o in items if s["event_id"] == last_ev and s["p"] >= min_p and rest + math.log(o) >= L]
        if alts:
            legs[-1] = max(alts, key=lambda x: x[0]["p"] * _trust(x[0]))
    o = math.prod(x[1] for x in legs)
    p = math.prod(x[0]["p"] for x in legs)
    return {"legs": [x[0] for x in legs], "leg_odds": [x[1] for x in legs], "odds": o, "p": p, "ev": p * o - 1}


def objective_plan(stake, target, selections, cfg):
    res = _objective_plan(stake, target, selections, cfg)
    o = cfg.get("_obj") or {}
    res["strict"] = bool(o.get("on"))
    if o.get("on"):
        # ce qui est atteignable en restant objectif
        ok = [s for s in selections if objective_ok(s, cfg) and not s["suspect"]]
        res["n_ok"] = len(ok)
        o2 = cfg["_obj"]
        why = {"proba trop basse": 0, "contre la forme": 0, "confiance trop basse": 0,
               "données manquantes": 0, "cote estimée": 0}
        for x in selections:
            st = x.get("stats") or {}
            if x["p"] < float(o2.get("min_p", 0.55)):
                why["proba trop basse"] += 1
            elif st.get("contra"):
                why["contre la forme"] += 1
            elif x.get("conf", 50) < float(o2.get("min_conf", 50)):
                why["confiance trop basse"] += 1
            elif o2.get("need_data", True) and st.get("q", 0) < 0.5:
                why["données manquantes"] += 1
            elif o2.get("no_est") and x.get("estimated"):
                why["cote estimée"] += 1
        res["why"] = {k: v for k, v in why.items() if v}
        best = {}
        for s in ok:
            if s["event_id"] not in best or s["odds"] > best[s["event_id"]]["odds"]:
                best[s["event_id"]] = s
        mx = 1.0
        lmax = int(cfg.get("_max_legs") or 10)
        for s in sorted(best.values(), key=lambda x: -x["odds"])[:lmax]:
            mx *= s["odds"]
        res["strict_max_gain"] = round(stake * (mx - 1), 2) if best else 0
        if not res["options"]:
            relaxed = dict(cfg, _obj=None)
            r2 = _objective_plan(stake, target, selections, relaxed)
            for op in r2["options"]:
                op["relaxed"] = True
            res["relaxed_options"] = r2["options"][:3]
    return res


def _objective_plan(stake, target, selections, cfg):
    """Propose plusieurs façons d'atteindre l'objectif de gain, de la plus sûre à la plus risquée."""
    total_needed = stake + target
    need1 = total_needed / stake
    lmin = max(1, int(cfg.get("_min_legs") or 1))
    lmax = max(lmin, min(10, int(cfg.get("_max_legs") or 10)))
    pool = 22 if lmax <= 6 else (18 if lmax <= 8 else 15)
    bt = lambda its, need, **kw: best_ticket(its, need, **dict(dict(max_legs=lmax, min_legs=lmin), **kw))
    books = set()
    allowed = {b.strip().lower() for b in cfg.get("bookmakers", "").split(",") if b.strip()}
    for s in selections:
        books |= set(s["playable"])
    if allowed:
        books &= allowed | (books if not (books & allowed) else set())
    options = []

    def add(kind, title, desc, tickets, book_title):
        if not tickets:
            return
        miss = 1.0
        for t in tickets:
            miss *= 1 - t["p"]
        options.append({"kind": kind, "title": title, "desc": desc, "book": book_title,
                        "tickets": tickets, "p_hit": 1 - miss})

    for book in books:
        items = _book_items(selections, cfg, book, pool_size=pool)
        if not items:
            continue
        btitle = items[0][0]["playable"][book][1]
        # 1 ticket : le plus sûr
        t = bt(items, need1)
        if t and len(t["legs"]) >= 3:  # à proba quasi égale, on préfère moins de matchs
            tl = bt(items, need1, legs_limit=len(t["legs"]) - 1)
            if tl and tl["p"] >= t["p"] * 0.99:
                tl["_fewer"] = True
                t = tl
        if t:
            t["stake"] = stake
            add("one", "1 ticket · le plus de chances au total",
                f"La meilleure probabilité globale pour une cote ≥ {need1:.2f}".replace(".", ","), [t], btitle)
        # 1 ticket : que des favoris (chaque sélection très probable), quitte à mettre plus de matchs
        # 1 favori par match : on cherche le seuil de sécurité le plus haut qui permet d'atteindre la cote
        fmax = int(cfg.get("_max_legs") or 60)
        allit = _book_items(selections, cfg, book)
        seen_sets = []
        for th in (0.85, 0.80, 0.75, 0.70, 0.65, 0.60, 0.55):
            tf = favorites_ticket(allit, need1, th, min_legs=lmin, max_legs=fmax)
            if not tf:
                continue
            key = frozenset(l["uid"] for l in tf["legs"])
            if key in seen_sets:
                continue
            seen_sets.append(key)
            tf["stake"] = stake
            add("fav", f"Favoris ≥ {th * 100:.0f} % · {len(tf['legs'])} matchs",
                f"1 favori par match, chacun avec au moins {th * 100:.0f} % de chances", [tf], btitle)
            options[-1]["safety"] = th
        if t:
            # moins de matchs (si le plus sûr en a 3+)
            if len(t["legs"]) >= 3:
                t2 = bt(items, need1, legs_limit=len(t["legs"]) - 1)
                if t2 and t2["p"] >= t["p"]:
                    t2 = None  # déjà meilleur avec moins de matchs : inutile de le proposer 2 fois
                if t2:
                    t2["stake"] = stake
                    add("few", "1 ticket · moins de matchs", "Moins de sélections, cote plus élevée par match",
                        [t2], btitle)
        # 1 ticket : gros gain (objectif x2)
        t3 = bt(items, (stake + 2 * target) / stake)
        if t3:
            t3["stake"] = stake
            add("big", "1 ticket · objectif x2", f"Pour viser {2 * target:.0f} € de gain".replace(".", ","),
                [t3], btitle)
        # plusieurs tickets sur des matchs différents : il suffit qu'UN passe
        for n in (2, 3):
            st = round(stake / n, 2)
            need = total_needed / st
            used, tickets = set(), []
            for _ in range(n):
                tk = bt(_book_items(selections, cfg, book, exclude_events=used, pool_size=pool), need)
                if not tk:
                    tickets = []
                    break
                tk["stake"] = st
                used |= {l["event_id"] for l in tk["legs"]}
                tickets.append(tk)
            add(f"split{n}", f"{n} tickets de {st:.2f} €".replace(".", ","),
                f"Matchs différents · un seul ticket gagnant suffit (cote ≥ {need:.2f} chacun)".replace(".", ","),
                tickets, btitle)

    # le ticket « favoris » passe en premier quand il existe (c'est le plus objectif), puis par chances
    # tickets « favoris » d'abord (du plus sûr par match au moins sûr), puis les autres par chances
    options.sort(key=lambda o: (o["kind"] != "fav", -o.get("safety", 0), -o["p_hit"]))
    seen, uniq = set(), []
    for o in options:
        key = tuple(sorted(l["uid"] for t in o["tickets"] for l in t["legs"]))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(o)
    # cote max atteignable (info si objectif impossible)
    max_odds = 0.0
    for book in books:
        items = _book_items(selections, cfg, book)
        evs = {}
        for s, o in items:
            evs[s["event_id"]] = max(evs.get(s["event_id"], 0), o)
        prod = 1.0
        for o in sorted(evs.values(), reverse=True)[:6]:
            prod *= o
        max_odds = max(max_odds, prod)
    return {"stake": stake, "target": target, "need_odds": need1, "options": uniq[:12], "max_odds": max_odds}


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
        "value": is_value(b, cfg), "event_id": b["event_id"], "conf": b.get("conf"),
        "mine": would_bet(b, cfg), "cat": b.get("cat"), "estimated": bool(b.get("estimated")),
        "user_odds": bool(b.get("user_odds")), "move": b.get("move"),
        "min_odds": b.get("min_odds"),
        "p_market": round(b.get("p_market", b["p"]), 4), "stats": b.get("stats"),
        "prices": [{"book": t, "odds": o, "playable": k in b["playable"]}
                   for k, (o, t) in sorted(b["all_prices"].items(), key=lambda kv: -kv[1][0])],
    }


def ser_combo(c):
    return {
        "uid": c["uid"], "odds": round(c["odds"], 2), "p": round(c["p"], 4), "ev": round(c["ev"], 4),
        "book": c["book"], "value": c["value"], "mine": c.get("would_bet", False),
        "legs": [{"uid": l["uid"], "selection": l["selection"], "match": l["match"], "date": fmt_date(l["start"]),
                  "sport": l["sport"], "odds": lo, "p": round(l["p"], 4), "estimated": bool(l.get("estimated")),
                  "min_odds": l.get("min_odds")}
                 for l, lo in zip(c["legs"], c["leg_odds"])],
    }


def ser_objective(res, cfg):
    out = {k: res.get(k) for k in ("stake", "target", "need_odds", "max_odds", "strict", "n_ok", "strict_max_gain", "why")}
    out["options"] = []
    for o in res["options"] + res.get("relaxed_options", []):
        tickets = []
        for t in o["tickets"]:
            tickets.append({
                "stake": t["stake"], "odds": round(t["odds"], 2), "p": round(t["p"], 4), "ev": round(t["ev"], 4),
                "ret": round(t["stake"] * t["odds"], 2),
                "value": t["ev"] >= float(cfg["min_ev"]),
                "legs": [{"uid": l["uid"], "selection": l["selection"], "match": l["match"], "date": fmt_date(l["start"]),
                          "conf": l.get("conf"), "q": (l.get("stats") or {}).get("q"),
                          "sport": l["sport"], "odds": lo, "p": round(l["p"], 4), "estimated": bool(l.get("estimated")),
                          "min_odds": l.get("min_odds"), "market": l["market"]}
                         for l, lo in zip(t["legs"], t["leg_odds"])],
            })
        out["options"].append({"relaxed": bool(o.get("relaxed")), "kind": o["kind"], "title": o["title"],
                               "desc": o["desc"], "book": o["book"],
                               "p_hit": round(o["p_hit"], 4), "tickets": tickets})
    return out


def ser_plan(plan, cfg):
    out = {k: plan.get(k) for k in ("budget", "mode", "staked", "expected", "max_win", "n_value", "n_mine",
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
# Filtre par jour / journée
# --------------------------------------------------------------------------

def _local_date(ev):
    try:
        return parse_time(ev["commence_time"]).astimezone().date()
    except (KeyError, ValueError):
        return None


def next_round_ids(events, info):
    """Matchs de la prochaine journée de chaque championnat."""
    now = datetime.now(timezone.utc)
    by_sport = {}
    for ev in events:
        try:
            t = parse_time(ev["commence_time"])
        except (KeyError, ValueError):
            continue
        if t <= now:
            continue
        by_sport.setdefault(ev.get("_sport_key") or ev.get("sport_key"), []).append((t, ev))
    keep = set()
    for lst in by_sport.values():
        lst.sort(key=lambda x: x[0])
        rounds = [(info.get(ev.get("id")) or {}).get("round") for _, ev in lst]
        first_round = next((r for r in rounds if r), None)
        if first_round:  # vraie journée connue grâce à API-Football
            keep |= {ev.get("id") for (_, ev), r in zip(lst, rounds) if r == first_round or
                     (r is None and (_ - lst[0][0]).total_seconds() < 4 * 86400)}
            continue
        prev = None  # sinon : matchs groupés sans trou de plus de 40 h
        for t, ev in lst:
            if prev is not None and (t - prev).total_seconds() > 40 * 3600:
                break
            keep.add(ev.get("id"))
            prev = t
    return keep


def filter_events(events, day, info):
    if not day or day == "all":
        return events
    today = datetime.now().astimezone().date()
    if day == "today":
        return [e for e in events if _local_date(e) == today]
    if day == "tomorrow":
        return [e for e in events if _local_date(e) == today + timedelta(days=1)]
    if day == "weekend":  # du vendredi 17 h au lundi soir
        wd = today.weekday()
        fri = today - timedelta(days=wd - 4) if wd in (4, 5, 6) else (
            today - timedelta(days=3) if wd == 0 else today + timedelta(days=4 - wd))
        start = datetime(fri.year, fri.month, fri.day, 17).astimezone()
        end = start + timedelta(days=3, hours=7)
        out = []
        for e in events:
            try:
                t = parse_time(e["commence_time"]).astimezone()
            except (KeyError, ValueError):
                continue
            if start <= t <= end:
                out.append(e)
        return out
    if day == "round":
        keep = next_round_ids(events, info)
        return [e for e in events if e.get("id") in keep]
    if day.startswith("d:"):
        return [e for e in events if str(_local_date(e)) == day[2:]]
    return events


def day_options(events, info):
    today = datetime.now().astimezone().date()
    now = datetime.now(timezone.utc)
    counts = {}
    for e in events:
        try:
            if parse_time(e["commence_time"]) <= now:
                continue
        except (KeyError, ValueError):
            continue
        d = _local_date(e)
        counts[d] = counts.get(d, 0) + 1
    opts = [{"v": "all", "label": "Tous les matchs", "n": sum(counts.values())}]
    rid = next_round_ids(events, info)
    has_rounds = any((info.get(i) or {}).get("round") for i in rid)
    opts.append({"v": "round", "label": "Prochaine journée" + (" ✓" if has_rounds else ""), "n": len(rid)})
    wk = filter_events([e for e in events if parse_time(e["commence_time"]) > now], "weekend", info)
    opts.append({"v": "weekend", "label": "Week-end", "n": len(wk)})
    for d in sorted(k for k in counts if k):
        if d == today:
            lab, v = "Aujourd'hui", "today"
        elif d == today + timedelta(days=1):
            lab, v = "Demain", "tomorrow"
        else:
            lab, v = f"{JOURS[d.weekday()].capitalize()} {d:%d/%m}", f"d:{d}"
        opts.append({"v": v, "label": lab, "n": counts[d]})
    return opts


# --------------------------------------------------------------------------
# Pont Python <-> interface (pywebview)
# --------------------------------------------------------------------------

def resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def _obj_opts(o):
    if not isinstance(o, dict):
        return None
    try:
        return {"on": bool(o.get("on")), "min_p": float(str(o.get("min_p", 55)).replace(",", ".")) / 100,
                "min_conf": float(str(o.get("min_conf", 50)).replace(",", ".")),
                "need_data": bool(o.get("need_data", True)), "no_est": bool(o.get("no_est"))}
    except ValueError:
        return None


def _int_or_none(v):
    try:
        v = int(float(str(v).replace(",", ".")))
        return max(1, min(10, v))
    except (TypeError, ValueError):
        return None


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
        self._stats = statsmod.Stats(config_dir(), f"{APP_NAME}/{APP_VERSION}")
        self._info = {}
        self._stats_dirty = False
        self._next_stats = time.time() + 5
        self._next_web = time.time() + 8
        self._days = []
        self._hist = History(config_dir())
        self._next_settle = time.time() + 20
        self._odds_hist = {}
        try:
            with open(os.path.join(config_dir(), "odds_history.json"), encoding="utf-8") as f:
                self._odds_hist = json.load(f)
        except (OSError, ValueError):
            pass
        self._overrides = {}  # cotes Betclic tapées par l'utilisateur {uid: cote}
        try:
            with open(os.path.join(config_dir(), "my_odds.json"), encoding="utf-8") as f:
                self._overrides = json.load(f)
        except (OSError, ValueError):
            pass
        cache = load_cache()
        if cache and cache.get("events") and self._cfg["api_key"] and not self._cfg["demo"]:
            self._events, self._quota = cache["events"], cache.get("quota")
            self._last_fetch = cache.get("ts", 0)
            self._next_refresh = self._last_fetch + refresh_minutes(self._cfg, (self._quota or {}).get("remaining")) * 60
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
            if now - last_an > 60 or (self._stats_dirty and now - last_an > 3):
                last_an = now
                self._stats_dirty = False
                self._reanalyze()
            if now >= self._next_settle:
                self._next_settle = now + 20 * 60
                threading.Thread(target=self._hist.settle_all, args=(statsmod.ESPN_SLUGS,), daemon=True).start()
            if now >= self._next_web and self._cfg.get("web_info", True) and self._events and not self._is_demo:
                self._next_web = now + 5 * 60
                with self._lock:
                    sports, events = sports_list(self._cfg), list(self._events)

                def web_job():
                    def changed():
                        self._stats_dirty = True
                    self._stats.update_web(sports, events, on_change=changed)
                threading.Thread(target=web_job, daemon=True).start()
            if now >= self._next_stats and self._stats_key() and self._events \
                    and not self._is_demo and not self._stats.running:
                self._next_stats = now + 20 * 60
                threading.Thread(target=self._stats_update, daemon=True).start()
            if now >= next_upd:
                next_upd = now + 30 * 60  # vérifie les mises à jour toutes les 30 min
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
                    self._next_stats = now + 2
                    self._next_web = now + 4
            self._next_refresh = now + (15 if demo else refresh_minutes(cfg, (self._quota or {}).get("remaining"))) * 60
            self._fetching = False
            self._reanalyze()

    def _apply_moves(self):
        """Mouvement de la cote Pinnacle depuis qu'on suit le match (l'argent « sérieux »)."""
        changed = False
        ts = self._last_fetch
        live = set()
        for x in self._sel:
            pin = (x.get("all_prices") or {}).get("pinnacle")
            if not pin or x.get("estimated"):
                continue
            live.add(x["uid"])
            h = self._odds_hist.get(x["uid"])
            if not h:
                self._odds_hist[x["uid"]] = {"f": pin[0], "ft": ts, "l": pin[0], "lt": ts}
                changed = True
                continue
            if ts > h["lt"] and pin[0] != h["l"]:
                h["l"], h["lt"] = pin[0], ts
                changed = True
            mv = (h["l"] - h["f"]) / h["f"]
            if abs(mv) < 0.05:
                continue
            st = x.get("stats")
            if st is None:
                st = x["stats"] = {"q": 0, "round": "", "p_model": None, "pros": [], "cons": [], "score": 0,
                                   "contra": False, "xg": None, "form_home": "", "form_away": ""}
            txt = (f"Cote Pinnacle passée de {h['f']:.2f} à {h['l']:.2f}".replace(".", ","))
            if mv < 0:
                st["pros"].append("📉 " + txt + " : l'argent des parieurs pros arrive sur ce pari")
                x["conf"] = min(99, x.get("conf", 50) + 6)
            else:
                st["cons"].append("📈 " + txt + " : les parieurs pros s'en détournent")
                x["conf"] = max(1, x.get("conf", 50) - 6)
            x["move"] = round(mv, 3)
        if changed:
            if len(self._odds_hist) > 6000:
                self._odds_hist = {k: v for k, v in self._odds_hist.items() if k in live}
            try:
                with open(os.path.join(config_dir(), "odds_history.json"), "w", encoding="utf-8") as f:
                    json.dump(self._odds_hist, f)
            except OSError:
                pass

    # ---- historique / bankroll ----
    def log_ticket(self, uids, stake, label=""):
        try:
            stake = float(str(stake).replace(",", ".").replace("€", "").strip())
        except ValueError:
            return {"ok": False, "error": "Mise invalide"}
        with self._lock:
            by = {x["uid"]: x for x in self._sel}
            legs = [History.leg_of(by[u]) for u in uids if u in by]
        if not legs or len(legs) != len(uids):
            return {"ok": False, "error": "Un des matchs n'est plus disponible"}
        self._hist.log_ticket(legs, stake, label)
        return {"ok": True}

    def delete_ticket(self, tid):
        self._hist.delete_ticket(tid)
        return True

    def get_history(self):
        out = self._hist.summary()
        out["bank"] = self.get_bank()
        return out

    def settle_now(self):
        threading.Thread(target=self._hist.settle_all, args=(statsmod.ESPN_SLUGS,), daemon=True).start()
        return True

    def get_bank(self):
        cfg = self._cfg
        start = float(cfg.get("bankroll") or 0)
        u = self._hist.summary()["user"]
        cur = start + u["profit"] if start else None
        day_max = round(cur * float(cfg.get("daily_max_pct") or 10) / 100, 2) if cur else None
        left = round(day_max - u["today_staked"], 2) if day_max is not None else None
        sl = float(cfg.get("stop_loss") or 0)
        return {"enabled": bool(start), "start": start, "current": round(cur, 2) if cur is not None else None,
                "day_max": day_max, "left_today": left, "today_pl": u["today_pl"],
                "stop": bool(sl and u["today_pl"] <= -sl), "stop_loss": sl}

    def _apply_overrides(self, cfg):
        """Remplace la cote estimée par la vraie cote Betclic tapée par l'utilisateur."""
        if not self._overrides:
            return
        for s in self._sel:
            o = self._overrides.get(s["uid"])
            if not o:
                continue
            s["odds"] = float(o)
            s["user_odds"] = True
            s["estimated"] = False
            s["playable"] = {s["book_key"]: (float(o), s["book"])}
            s["all_prices"] = dict(s["all_prices"], **{s["book_key"]: (float(o), s["book"])})
            s["ev"] = s["p"] * s["odds"] - 1
            s["kelly"] = s["ev"] / (s["odds"] - 1) if s["odds"] > 1 else 0
            s["suspect"] = s.get("p_market", s["p"]) * s["odds"] - 1 > float(cfg["max_ev"])

    def _save_overrides(self):
        try:
            with open(os.path.join(config_dir(), "my_odds.json"), "w", encoding="utf-8") as f:
                json.dump(self._overrides, f)
        except OSError:
            pass

    def set_odds(self, uid, odds):
        """L'utilisateur tape la cote vue sur Betclic -> verdict immédiat."""
        try:
            o = float(str(odds).replace(",", ".").strip())
        except ValueError:
            o = 0
        with self._lock:
            if o <= 1:
                self._overrides.pop(uid, None)
            else:
                self._overrides[uid] = o
            # nettoyage des cotes des matchs passés
            live = {s["uid"] for s in self._sel}
            if len(self._overrides) > 300:
                self._overrides = {k: v for k, v in self._overrides.items() if k in live}
            self._save_overrides()
        self._reanalyze()
        with self._lock:
            b = next((x for x in self._sel if x["uid"] == uid), None)
            if not b:
                return {"ok": False}
            out = ser_bet(b, self._cfg)
        out["ok"] = True
        out["user_odds"] = o > 1
        return out

    def _stats_key(self):
        return self._cfg.get("fd_key", "").strip() or self._cfg.get("football_key", "").strip()

    def _stats_update(self):
        with self._lock:
            fd, af = self._cfg.get("fd_key", "").strip(), self._cfg.get("football_key", "").strip()
            sports, events = sports_list(self._cfg), list(self._events)

        def changed():
            self._stats_dirty = True
        self._stats.last_error = ""
        if fd:
            self._stats.update_fd(fd, sports, events, on_change=changed)
        if af:  # optionnel (offre payante) : blessés, compos
            err = self._stats.last_error
            self._stats.update(af, sports, events, on_change=changed)
            if err and not self._stats.last_error:
                self._stats.last_error = err

    def _reanalyze(self):
        with self._lock:
            try:
                cfg = self._cfg
                use = (bool(self._stats_key()) or cfg.get("web_info", True)) and not self._is_demo
                self._info = self._stats.info_for(self._events) if use else {}
                self._days = day_options(self._events, self._info)
                events = filter_events(self._events, cfg.get("day_filter", "all"), self._info)
                present = {bk.get("key") for ev in self._events for bk in ev.get("bookmakers", [])}
                mine = {b.strip().lower() for b in cfg.get("bookmakers", "").split(",") if b.strip()}
                self._warning = ""
                if mine and present and not (mine & present):
                    cfg = dict(cfg, bookmakers="")
                    self._warning = ("Betclic n'a pas encore publié de cotes pour ces matchs : cotes d'autres "
                                     "bookmakers affichées à titre indicatif.")
                self._sel = analyze(events, cfg, info=self._info)
                self._apply_overrides(cfg)
                if not self._is_demo:
                    self._apply_moves()
                self._picks = top_picks(self._sel, cfg)
                self._prob = most_probable(self._sel, cfg)
                vpool = [s for s in self._sel if is_value(s, cfg)]
                value_combos = build_combos(vpool, cfg, int(cfg["max_legs"]), float(cfg["min_combo_prob"]))
                other = build_combos(candidates(self._sel, cfg, 0.40), cfg, int(cfg["max_legs"]),
                                     float(cfg["min_combo_prob"]))
                seen = {c["uid"] for c in value_combos}
                self._combos = (value_combos + [c for c in other if c["uid"] not in seen])[:40]
                if not self._is_demo:
                    self._hist.log_app([x for x in self._picks if would_bet(x, cfg)])
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
                "max_books": max((len(e.get("bookmakers", [])) for e in self._events), default=0),
                "min_books": int(self._cfg.get("min_books", 4)),
                "n_value": sum(1 for s in self._picks if is_value(s, self._cfg)),
                "has_key": bool(self._cfg["api_key"].strip()),
                "update": self._update, "update_state": self._update_state,
                "days": self._days, "day": self._cfg.get("day_filter", "all"),
                "stats": dict(self._stats.status(), enabled=bool(self._stats_key()),
                              af_set=bool(self._cfg.get("football_key", "").strip()),
                              matched=len(self._info)),
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

    def set_day(self, v):
        with self._lock:
            self._cfg["day_filter"] = str(v)
            save_config(self._cfg)
        self._reanalyze()
        return True

    def get_bet(self, uid):
        """Fiche complète d'une sélection (pour un clic sur une ligne de ticket)."""
        with self._lock:
            x = next((s for s in self._sel if s["uid"] == uid), None)
            return ser_bet(x, self._cfg) if x else None

    def get_match(self, event_id):
        with self._lock:
            st = self._info.get(event_id)
            sel = [x for x in self._sel if x["event_id"] == event_id]
        if not st and not sel:
            return None
        st = dict(st or {})
        st["neutral"] = neutral_view(st, sel)
        return st

    def refresh(self):
        if not self._fetching:
            threading.Thread(target=self._do_refresh, daemon=True).start()
        return True

    def compute_plan(self, budget, mode, types=None, min_legs=None, max_legs=None, obj=None):
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
        if types:
            cfg["_types"] = set(types)
        cfg["_min_legs"], cfg["_max_legs"] = _int_or_none(min_legs), _int_or_none(max_legs)
        cfg["_obj"] = _obj_opts(obj)
        out = ser_plan(stake_plan(budget, mode, sel, cfg), cfg)
        out["bank"] = self.get_bank()
        return out

    def compute_objective(self, stake, target, types=None, min_legs=None, max_legs=None, obj=None):
        try:
            stake = float(str(stake).replace(",", ".").replace("€", "").strip())
            target = float(str(target).replace(",", ".").replace("€", "").strip())
        except ValueError:
            return {"error": "Montant invalide"}
        if stake <= 0 or target <= 0:
            return {"error": "Mets une mise et un objectif supérieurs à 0"}
        with self._lock:
            sel, cfg = list(self._sel), dict(self._cfg)
        if self._warning:
            cfg["bookmakers"] = ""
        if types:
            cfg["_types"] = set(types)
        cfg["_min_legs"], cfg["_max_legs"] = _int_or_none(min_legs), _int_or_none(max_legs)
        cfg["_obj"] = _obj_opts(obj)
        if cfg["_min_legs"] and cfg["_max_legs"] and cfg["_min_legs"] > cfg["_max_legs"]:
            cfg["_min_legs"], cfg["_max_legs"] = cfg["_max_legs"], cfg["_min_legs"]
        out = ser_objective(objective_plan(stake, target, sel, cfg), cfg)
        out["bank"] = self.get_bank()
        return out

    def reset_settings(self):
        """Remet tous les réglages par défaut, en gardant les clés et les championnats."""
        keep = {k: self._cfg.get(k) for k in ("api_key", "fd_key", "football_key", "sports", "bookmakers")}
        cfg = dict(DEFAULT_CONFIG)
        cfg.update({k: v for k, v in keep.items() if v})
        with self._lock:
            self._cfg = cfg
            save_config(cfg)
        self._reanalyze()
        return {"ok": True}

    def get_config(self):
        with self._lock:
            cfg = dict(self._cfg)
        cfg["_cats"] = [{"key": k, "label": v} for k, v in mk.CATEGORIES.items()]
        cfg["_sports"] = [{"key": k, "label": v} for k, v in SPORTS.items()]
        cfg["_credits_month"] = credits_per_month(self._cfg)
        return cfg

    def estimate(self, cfg):
        try:
            c = dict(self._cfg)
            c.update({k: v for k, v in cfg.items() if k in DEFAULT_CONFIG})
            iv = refresh_minutes(c, (self._quota or {}).get("remaining"))
            return {"per": credits_per_refresh(c), "month": credits_per_month(c), "auto": bool(c.get("auto_refresh", True)),
                    "interval": int(iv), "remaining": (self._quota or {}).get("remaining")}
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
        if cfg.get("football_key") != self._cfg.get("football_key") or cfg.get("fd_key") != self._cfg.get("fd_key"):
            self._next_stats = time.time() + 1
        with self._lock:
            self._cfg = cfg
            save_config(cfg)
            if not source_changed:
                self._next_refresh = self._last_fetch + refresh_minutes(cfg, (self._quota or {}).get("remaining")) * 60
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
