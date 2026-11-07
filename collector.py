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


def tier_snapshot(snap, tier):
    """Chaque forfait ne reçoit que les données qu'il a payées (impossible de débloquer en trichant).
    - Essentiel : cotes, résultats, classements, forme (pas d'absents, compos, face-à-face, buteurs, météo, actus)
    - Pro : tout sauf les actus de dernière minute
    - Elite : tout"""
    out = dict(snap, tier=tier)
    if tier == "elite":
        return out
    st = dict(snap["stats"])
    web = {}
    for eid, w in (st.get("web") or {}).items():
        v = dict(w.get("v") or {})
        v["news"] = []
        if tier in ("gratuit", "essentiel"):
            v.update(inj_home=[], inj_away=[], lineups=[], last_xi=[], weather=None)
        web[eid] = dict(w, v=v)
    st["web"] = web
    if tier in ("gratuit", "essentiel"):
        for k in ("h2h", "scorers", "inj_day", "pred", "lineups"):
            st[k] = {}
        out["events"] = [dict(e, bookmakers=[dict(bk, markets=[m for m in bk.get("markets", []) if m.get("key") != "spreads"])
                                             for bk in e.get("bookmakers", [])]) for e in snap["events"]]
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


def public_record(events, st):
    """Bilan public : on enregistre chaque pari que Cotescope « jouerait », puis on le vérifie
    avec le résultat officiel. Le résumé est publié sans connexion (preuve pour la vitrine)."""
    from history import History
    folder = os.path.join(CACHE, "public")
    os.makedirs(folder, exist_ok=True)
    h = History(folder)
    cfg = dict(b.DEFAULT_CONFIG, sports=SPORTS, bookmakers="betclic_fr,winamax_fr,unibet_fr,parionssport_fr")
    sel = b.analyze(events, cfg, info=st.info_for(events))
    picks = [x for x in b.top_picks(sel, cfg) if b.would_bet(x, cfg)]
    # + la « sélection du jour » : les 3 paris les plus sûrs (vraies cotes) des matchs des 30 prochaines heures
    soon = datetime.now(timezone.utc) + timedelta(hours=30)
    safe = [x for x in b.most_probable(sel, cfg) if not x.get("estimated") and x["start"] <= soon and x["p"] >= 0.6]
    picks += safe[:3]
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


def _best_team(name, candidates):
    n0 = _norm_team(name)
    best, bs = None, 0.0
    A = set(n0.split())
    for c in candidates:
        n1 = _norm_team(c)
        if n1 == n0:
            return c
        B = n1.split()
        hit = sum(1 for w in B if w in A or any(len(a) > 3 and (a.startswith(w) or w.startswith(a)) for a in A))
        sc = hit / max(len(B), len(A), 1)
        if sc > bs:
            best, bs = c, sc
    return best if bs >= 0.5 else None


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


def add_fd_h2h(events, st):
    """Les 5 dernières confrontations en championnat (football-data.co.uk, gratuit, sans clé) pour chaque match
    qui a une fiche, quand ESPN ne les donne pas."""
    sys.path.insert(0, os.path.join(HERE, "ci"))
    import history_data as H
    code_of = {v[1]: k for k, v in H.LEAGUES.items()}
    cdir = os.path.join(CACHE, "fd_csv")
    os.makedirs(cdir, exist_ok=True)
    seas = H.seasons()[-5:]
    games_by_code = {}
    web = st.data.get("web") or {}
    n = 0
    for ev in events:
        eid, code = ev.get("id"), code_of.get(ev.get("sport_key"))
        w = (web.get(eid) or {}).get("v")
        if not code or w is None or len(w.get("h2h") or []) >= 5:
            continue
        if code not in games_by_code:
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
            games_by_code[code] = rows
        rows = games_by_code[code]
        if not rows:
            continue
        teams = {r["h"] for r in rows[-400:]} | {r["a"] for r in rows[-400:]}
        hn, an = _best_team(ev.get("home_team"), teams), _best_team(ev.get("away_team"), teams)
        if not hn or not an or hn == an:
            continue
        meet = [r for r in rows if {r["h"], r["a"]} == {hn, an}]
        meet.sort(key=lambda r: r["d"], reverse=True)
        if not meet:
            continue
        lname = H.LEAGUES[code][0]
        w["h2h"] = [{"date": r["d"].isoformat(), "home": ev.get("home_team") if r["h"] == hn else ev.get("away_team"),
                     "away": ev.get("away_team") if r["h"] == hn else ev.get("home_team"),
                     "gh": r["hg"], "ga": r["ag"], "comp": lname} for r in meet[:5]]
        n += 1
    if n:
        st._save()
    return n


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

    # 3) scores pour l'historique
    state["scores"] = collect_scores(state, sorted({S.ESPN_SLUGS[s] for s in SPORTS if s in S.ESPN_SLUGS}))
    save_state(state)

    keep = ("fixtures", "results", "standings", "scorers", "h2h", "web", "inj_day", "pred", "lineups")
    snap = {
        "ts": state.get("odds_ts", 0), "built": time.time(), "sports": SPORTS,
        "events": events, "quota": state.get("quota"), "stats": {k: st.data.get(k, {}) for k in keep},
        "scores": state["scores"], "app_version": b.APP_VERSION,
    }
    with open(os.path.join(CACHE, "snapshot.json"), "w", encoding="utf-8") as f:
        json.dump(snap, f)
    log(f"snapshot : {len(events)} matchs, {len(snap['stats'].get('web', {}))} fiches web")
    for tier in TIERS:
        cf_put("snapshot_" + tier, tier_snapshot(snap, tier))

    try:
        public_record(events, st)
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
