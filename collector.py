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
            v.update(inj_home=[], inj_away=[], lineups=[], weather=None)
        web[eid] = dict(w, v=v)
    st["web"] = web
    if tier in ("gratuit", "essentiel"):
        for k in ("h2h", "scorers", "inj_day", "pred", "lineups"):
            st[k] = {}
        out["events"] = [dict(e, bookmakers=[dict(bk, markets=[m for m in bk.get("markets", []) if m.get("key") != "spreads"])
                                             for bk in e.get("bookmakers", [])]) for e in snap["events"]]
    if tier == "gratuit":  # version gratuite : Ligue 1 et Premier League, résultat du match
        out["events"] = [e for e in out["events"] if e.get("sport_key") in FREE_SPORTS]
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
    cfg = dict(b.DEFAULT_CONFIG, sports=SPORTS, bookmakers="betclic_fr,winamax_fr,unibet_fr,pmu_fr,parionssport_fr,netbet_fr")
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
