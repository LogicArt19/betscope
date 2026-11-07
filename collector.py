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


def main():
    state = load_state()
    cfg = dict(b.DEFAULT_CONFIG, api_key=os.environ.get("ODDS_KEY", ""), sports=SPORTS, demo=False)
    if not cfg["api_key"]:
        log("ODDS_KEY manquante")
        sys.exit(1)

    # 1) cotes, seulement quand c'est dû
    due, iv = odds_due(state, cfg)
    events = state.get("events", [])
    if due or not events:
        log(f"cotes : mise à jour (intervalle {iv} min)")
        ev, quota, errors = b.fetch_odds(cfg)
        if errors:
            log("erreurs cotes :", errors)
        if ev or not errors:
            events, state["odds_ts"] = ev, time.time()
            if quota:
                state["quota"] = quota
    else:
        log(f"cotes : pas encore dû (toutes les {iv} min)")
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
    cf_put("snapshot", snap)

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
