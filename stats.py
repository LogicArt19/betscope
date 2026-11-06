# -*- coding: utf-8 -*-
"""
Module stats BetScope : relie les matchs (The Odds API) aux données API-Football
(journée, blessés/suspendus, compos officielles, forme, confrontations, pronostic).

Plan gratuit API-Football : 100 requêtes / jour et ~10 / minute.
-> tout est mis en cache sur disque et les requêtes sont espacées.
"""

import difflib
import json
import os
import re
import threading
import time

import model
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

AF_BASE = "https://v3.football.api-sports.io"

# clé The Odds API -> id de ligue API-Football
LEAGUE_IDS = {
    "soccer_france_ligue_one": 61,
    "soccer_france_ligue_two": 62,
    "soccer_epl": 39,
    "soccer_efl_champ": 40,
    "soccer_spain_la_liga": 140,
    "soccer_italy_serie_a": 135,
    "soccer_germany_bundesliga": 78,
    "soccer_uefa_champs_league": 2,
    "soccer_uefa_europa_league": 3,
    "soccer_uefa_europa_conference_league": 848,
    "soccer_netherlands_eredivisie": 88,
    "soccer_portugal_primeira_liga": 94,
    "soccer_belgium_first_div": 144,
    "soccer_turkey_super_league": 203,
}

# clé The Odds API -> code de compétition football-data.org (gratuit, saison en cours)
FD_BASE = "https://api.football-data.org/v4"
FD_CODES = {
    "soccer_france_ligue_one": "FL1",
    "soccer_epl": "PL",
    "soccer_efl_champ": "ELC",
    "soccer_spain_la_liga": "PD",
    "soccer_italy_serie_a": "SA",
    "soccer_germany_bundesliga": "BL1",
    "soccer_uefa_champs_league": "CL",
    "soccer_netherlands_eredivisie": "DED",
    "soccer_portugal_primeira_liga": "PPL",
}
FD_STAGES = {"LAST_16": "Round of 16", "QUARTER_FINALS": "Quarter-finals", "SEMI_FINALS": "Semi-finals",
             "FINAL": "Final", "PLAYOFFS": "Knockout Round Play-offs", "PLAYOFF_ROUND": "Knockout Round Play-offs"}

DAILY_BUDGET = 100
KEEP_SPARE = 4            # requêtes gardées en réserve
MIN_GAP = 6.5             # secondes entre 2 requêtes (≈ 9 / minute)
TTL_FIXTURES = 6 * 3600
TTL_PRED = 24 * 3600
TTL_RESULTS = 12 * 3600
TTL_INJ = 4 * 3600
TTL_LINEUP = 20 * 60


def _norm(name):
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ").replace("saint", "st").replace("st.", "st")
    s = re.sub(r"\b(fc|afc|cf|sc|ac|as|ssc|us|rc|cd|ud|sd|fk|sk|bk|club|calcio|de|la|the|1\.|olympique|stade)\b", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(s.split())


def _sim(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b or a in b or b in a:
        return 1.0
    ta, tb = set(a.split()), set(b.split())
    jac = len(ta & tb) / max(1, len(ta | tb))
    return max(difflib.SequenceMatcher(None, a, b).ratio(), jac)


def season_for(dt):
    return dt.year if dt.month >= 7 else dt.year - 1


def round_label(rnd):
    """'Regular Season - 12' -> '12e journée'."""
    if not rnd:
        return ""
    m = re.search(r"(\d+)\s*$", rnd)
    if m and ("Regular Season" in rnd or "League Stage" in rnd or "Group" in rnd):
        n = int(m.group(1))
        return f"{n}{'re' if n == 1 else 'e'} journée"
    trans = {"Round of 16": "8es de finale", "Quarter-finals": "Quarts de finale", "Semi-finals": "Demi-finales",
             "Final": "Finale", "Knockout Round Play-offs": "Barrages"}
    for k, v in trans.items():
        if k in rnd:
            return v
    return rnd


def _pct(x):
    try:
        return float(str(x).strip().rstrip("%")) / 100
    except (TypeError, ValueError):
        return None


class Stats:
    def __init__(self, cache_dir, user_agent="BetScope"):
        self.path = os.path.join(cache_dir, "stats_cache.json")
        self.ua = user_agent
        self.lock = threading.RLock()
        self.running = False
        self.last_error = ""
        self.remaining = None
        self.last_call = 0.0
        self.data = {"fixtures": {}, "results": {}, "standings": {}, "inj_day": {}, "pred": {}, "lineups": {},
                     "usage": {"day": "", "n": 0}, "h2h": {}}
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass

    # ---------- bas niveau ----------
    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f)
        except OSError:
            pass

    def _budget_left(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        u = self.data["usage"]
        if u.get("day") != today:
            u["day"], u["n"] = today, 0
        left = DAILY_BUDGET - u["n"]
        if self.remaining is not None:
            left = min(left, self.remaining)
        return left - KEEP_SPARE

    def _get(self, key, path, params):
        if self._budget_left() <= 0:
            raise RuntimeError("quota API-Football du jour atteint (reset à minuit UTC)")
        wait = MIN_GAP - (time.time() - self.last_call)
        if wait > 0:
            time.sleep(wait)
        url = f"{AF_BASE}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"x-apisports-key": key, "User-Agent": self.ua})
        self.last_call = time.time()
        with self.lock:
            self.data["usage"]["n"] += 1
        with urllib.request.urlopen(req, timeout=25) as r:
            body = json.loads(r.read().decode("utf-8"))
            rem = r.headers.get("x-ratelimit-requests-remaining")
            if rem is not None:
                try:
                    self.remaining = int(rem)
                except ValueError:
                    pass
        errs = body.get("errors")
        if errs and (isinstance(errs, dict) and errs or isinstance(errs, list) and errs):
            msg = "; ".join(f"{v}" for v in (errs.values() if isinstance(errs, dict) else errs))
            raise RuntimeError(msg[:200])
        return body.get("response", [])

    # ---------- mise à jour ----------
    def _fresh(self, kind, k, ttl):
        c = self.data.setdefault(kind, {}).get(str(k))
        return bool(c) and time.time() - c["ts"] < ttl

    def _put(self, kind, k, value, on_change=None):
        with self.lock:
            self.data.setdefault(kind, {})[str(k)] = {"ts": time.time(), "v": value}
            self._save()
        if on_change:
            on_change()

    def _fd_get(self, key, path, params=None):
        wait = MIN_GAP - (time.time() - self.last_call)
        if wait > 0:
            time.sleep(wait)
        url = f"{FD_BASE}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        req = urllib.request.Request(url, headers={"X-Auth-Token": key, "User-Agent": self.ua})
        self.last_call = time.time()
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            msg = ""
            try:
                msg = json.loads(e.read().decode("utf-8")).get("message", "")
            except Exception:
                pass
            if e.code in (401, 403):
                raise RuntimeError(msg or "clé football-data.org refusée")
            if e.code == 429:
                time.sleep(30)
                raise RuntimeError("trop de requêtes, nouvel essai dans quelques minutes")
            raise RuntimeError(f"erreur HTTP {e.code} {msg}"[:160])

    def update_fd(self, key, sports, events, on_change=None):
        """Source gratuite football-data.org : calendrier, résultats, classements, journées, face-à-face."""
        if not key or self.running:
            return
        self.running = True
        self.last_error = ""
        try:
            now = datetime.now(timezone.utc)
            codes = [FD_CODES[s] for s in sports if s in FD_CODES]
            for code in codes:
                # 1 seule requête = tous les matchs de la saison (joués + à venir)
                if not self._fresh("results", code, 3 * 3600):
                    body = self._fd_get(key, f"/competitions/{code}/matches")
                    res, fx = [], []
                    for m in body.get("matches", []):
                        try:
                            h, a = m["homeTeam"], m["awayTeam"]
                            if not h.get("id") or not a.get("id"):
                                continue
                            stage = m.get("stage") or ""
                            if stage in FD_STAGES:
                                rnd = FD_STAGES[stage]
                            elif m.get("matchday"):
                                rnd = ("League Stage - " if code == "CL" else "Regular Season - ") + str(m["matchday"])
                            else:
                                rnd = ""
                            ft = (m.get("score") or {}).get("fullTime") or {}
                            row = {"date": m["utcDate"], "home_id": h["id"], "away_id": a["id"],
                                   "home": h.get("name") or "", "away": a.get("name") or "",
                                   "home_short": h.get("shortName") or "", "away_short": a.get("shortName") or ""}
                            if m.get("status") == "FINISHED" and ft.get("home") is not None:
                                res.append(dict(row, gh=ft["home"], ga=ft["away"]))
                            elif m.get("status") in ("SCHEDULED", "TIMED", "POSTPONED"):
                                fx.append(dict(row, id=m["id"], round=rnd, league=code))
                        except (KeyError, TypeError):
                            continue
                    with self.lock:
                        self.data["fixtures"][code] = {"ts": time.time(), "list": fx}
                    self._put("results", code, res, on_change)
                if code != "CL" and not self._fresh("standings", code, 6 * 3600):
                    body = self._fd_get(key, f"/competitions/{code}/standings")
                    table = {}
                    for grp in body.get("standings", []):
                        if grp.get("type") != "TOTAL":
                            continue
                        for t in grp.get("table", []):
                            try:
                                table[str(t["team"]["id"])] = {
                                    "rank": t.get("position"), "points": t.get("points"),
                                    "gd": t.get("goalDifference"), "played": t.get("playedGames"),
                                    "form": (t.get("form") or "").replace(",", ""), "desc": "", "group": ""}
                            except (KeyError, TypeError):
                                continue
                    self._put("standings", code, table, on_change)
            # face-à-face (10 derniers, toutes compétitions) pour les matchs des 4 prochains jours
            mapping = self.match_all(events)
            soon = []
            for fx in mapping.values():
                try:
                    st = datetime.fromisoformat(fx["date"].replace("Z", "+00:00"))
                except ValueError:
                    continue
                if now < st < now + timedelta(days=4):
                    soon.append((st, fx))
            for st, fx in sorted(soon, key=lambda x: x[0]):
                k = f"{min(fx['home_id'], fx['away_id'])}-{max(fx['home_id'], fx['away_id'])}"
                if self._fresh("h2h", k, 7 * 86400):
                    continue
                body = self._fd_get(key, f"/matches/{fx['id']}/head2head", {"limit": 10})
                lst = []
                for m in body.get("matches", []):
                    try:
                        ft = m["score"]["fullTime"]
                        if ft.get("home") is None:
                            continue
                        lst.append({"date": m["utcDate"][:10], "home": m["homeTeam"]["name"],
                                    "away": m["awayTeam"]["name"], "gh": ft["home"], "ga": ft["away"],
                                    "comp": (m.get("competition") or {}).get("name", "")})
                    except (KeyError, TypeError):
                        continue
                lst.sort(key=lambda m: m["date"], reverse=True)
                self._put("h2h", k, lst, on_change)
        except Exception as e:
            self.last_error = f"football-data.org : {e}"
        finally:
            self.running = False
            with self.lock:
                self._save()
            if on_change:
                on_change()

    def update(self, key, sports, events, on_change=None, lineup_window_min=75):
        """Récupère ce qui manque en respectant le quota (≈ 30-45 requêtes / jour). À lancer dans un thread."""
        if not key or self.running:
            return
        self.running = True
        self.last_error = ""
        try:
            now = datetime.now(timezone.utc)
            season = season_for(now)
            leagues = [LEAGUE_IDS[s] for s in sports if s in LEAGUE_IDS]
            # 1) calendrier à venir (1 req / ligue / 6 h)
            for lid in leagues:
                if self._fresh("fixtures", lid, TTL_FIXTURES):
                    continue
                resp = self._get(key, "/fixtures", {
                    "league": lid, "season": season, "timezone": "UTC",
                    "from": now.strftime("%Y-%m-%d"), "to": (now + timedelta(days=9)).strftime("%Y-%m-%d")})
                fx = []
                for r in resp:
                    try:
                        fx.append({"id": r["fixture"]["id"], "date": r["fixture"]["date"],
                                   "round": r["league"].get("round", ""), "league": lid,
                                   "home": r["teams"]["home"]["name"], "away": r["teams"]["away"]["name"],
                                   "home_id": r["teams"]["home"]["id"], "away_id": r["teams"]["away"]["id"]})
                    except (KeyError, TypeError):
                        continue
                with self.lock:
                    self.data["fixtures"][str(lid)] = {"ts": time.time(), "list": fx}
                self._put("_dummy", 0, None, on_change)
            # 2) tous les résultats de la saison (1 req / ligue / 12 h) -> forme + modèle
            for lid in leagues:
                if self._fresh("results", lid, TTL_RESULTS):
                    continue
                resp = self._get(key, "/fixtures", {"league": lid, "season": season, "status": "FT-AET-PEN"})
                res = []
                for r in resp:
                    try:
                        res.append({"date": r["fixture"]["date"], "home_id": r["teams"]["home"]["id"],
                                    "away_id": r["teams"]["away"]["id"], "home": r["teams"]["home"]["name"],
                                    "away": r["teams"]["away"]["name"],
                                    "gh": r["goals"]["home"], "ga": r["goals"]["away"]})
                    except (KeyError, TypeError):
                        continue
                self._put("results", lid, res, on_change)
            # 3) classements (1 req / ligue / 12 h)
            for lid in leagues:
                if self._fresh("standings", lid, TTL_RESULTS):
                    continue
                resp = self._get(key, "/standings", {"league": lid, "season": season})
                table = {}
                try:
                    for group in resp[0]["league"]["standings"]:
                        for t in group:
                            table[str(t["team"]["id"])] = {
                                "rank": t.get("rank"), "points": t.get("points"), "gd": t.get("goalsDiff"),
                                "played": (t.get("all") or {}).get("played"), "form": t.get("form") or "",
                                "desc": t.get("description") or "", "group": t.get("group") or ""}
                except (IndexError, KeyError, TypeError):
                    pass
                self._put("standings", lid, table, on_change)
            # 4) blessés / suspendus par ligue et par jour de match (1 req / ligue / jour / 4 h)
            mapping = self.match_all(events)
            days = {}
            for fx in mapping.values():
                try:
                    st = datetime.fromisoformat(fx["date"].replace("Z", "+00:00"))
                except ValueError:
                    continue
                if now < st < now + timedelta(days=4):
                    days.setdefault((fx.get("league"), st.strftime("%Y-%m-%d")), []).append(fx)
            for (lid, day), _ in sorted(days.items(), key=lambda kv: kv[0][1]):
                if lid is None or self._fresh("inj_day", f"{lid}|{day}", TTL_INJ):
                    continue
                resp = self._get(key, "/injuries", {"league": lid, "season": season, "date": day})
                by_fx = {}
                for r in resp:
                    try:
                        by_fx.setdefault(str(r["fixture"]["id"]), []).append({
                            "team_id": r["team"]["id"], "team": r["team"]["name"], "player": r["player"]["name"],
                            "type": r["player"].get("type", ""), "reason": r["player"].get("reason", "")})
                    except (KeyError, TypeError):
                        continue
                self._put("inj_day", f"{lid}|{day}", by_fx, on_change)
            # 5) confrontations + avis (predictions) pour les matchs des 48 h, puis compos 1 h avant
            soon = []
            for fx in mapping.values():
                try:
                    st = datetime.fromisoformat(fx["date"].replace("Z", "+00:00"))
                except ValueError:
                    continue
                if now < st < now + timedelta(hours=48):
                    soon.append((st, fx))
            soon.sort(key=lambda x: x[0])
            soon_h2h = []
            for fx in mapping.values():
                try:
                    st = datetime.fromisoformat(fx["date"].replace("Z", "+00:00"))
                except ValueError:
                    continue
                if now < st < now + timedelta(hours=96):
                    soon_h2h.append((st, fx))
            for st, fx in soon:
                fid = str(fx["id"])
                mins = (st - now).total_seconds() / 60
                if mins <= lineup_window_min:
                    lu = self.data["lineups"].get(fid)
                    if not lu or (not lu.get("list") and time.time() - lu["ts"] > TTL_LINEUP):
                        resp = self._get(key, "/fixtures/lineups", {"fixture": fid})
                        self._store("lineups", fid, [self._lineup(r) for r in resp])
                        if on_change:
                            on_change()
            for st, fx in sorted(soon_h2h, key=lambda x: x[0]):
                k = f"{min(fx['home_id'], fx['away_id'])}-{max(fx['home_id'], fx['away_id'])}"
                if self._fresh("h2h", k, 7 * 86400) or self._budget_left() <= 15:
                    continue
                resp = self._get(key, "/fixtures/headtohead", {"h2h": k, "last": 10})
                lst = []
                for r in resp:
                    try:
                        lst.append({"date": r["fixture"]["date"][:10], "home": r["teams"]["home"]["name"],
                                    "away": r["teams"]["away"]["name"], "gh": r["goals"]["home"],
                                    "ga": r["goals"]["away"], "comp": r["league"]["name"]})
                    except (KeyError, TypeError):
                        continue
                lst.sort(key=lambda m: m["date"], reverse=True)
                self._put("h2h", k, lst, on_change)
            for st, fx in soon:
                fid = str(fx["id"])
                if self._fresh("pred", fid, TTL_PRED) or self._budget_left() <= 12:
                    continue
                resp = self._get(key, "/predictions", {"fixture": fid})
                self._put("pred", fid, self._pred(resp[0]) if resp else None, on_change)
        except urllib.error.HTTPError as e:
            self.last_error = f"API-Football : erreur HTTP {e.code}"
        except Exception as e:
            self.last_error = f"API-Football : {e}"
        finally:
            self.running = False
            with self.lock:
                self._save()
            if on_change:
                on_change()

    def _store(self, kind, fid, value):
        with self.lock:
            if kind == "lineups":
                self.data[kind][fid] = {"ts": time.time(), "list": value}
            else:
                self.data[kind][fid] = {"ts": time.time(), "v": value}
            self._save()

    @staticmethod
    def _lineup(r):
        return {"team": r.get("team", {}).get("name", ""), "formation": r.get("formation", ""),
                "xi": [p["player"]["name"] for p in r.get("startXI", []) if p.get("player")]}

    @staticmethod
    def _pred(r):
        try:
            pr = r.get("predictions", {})
            teams = r.get("teams", {})

            def side(t):
                l5 = (t or {}).get("last_5", {}) or {}
                lg = (t or {}).get("league", {}) or {}
                g = (l5.get("goals") or {})
                return {"form": (lg.get("form") or "")[-5:],
                        "gf": (g.get("for") or {}).get("average"), "ga": (g.get("against") or {}).get("average")}

            h2h = []
            for m in (r.get("h2h") or [])[:5]:
                try:
                    h2h.append({"date": m["fixture"]["date"][:10], "home": m["teams"]["home"]["name"],
                                "away": m["teams"]["away"]["name"], "gh": m["goals"]["home"], "ga": m["goals"]["away"]})
                except (KeyError, TypeError):
                    pass
            return {
                "home": _pct(pr.get("percent", {}).get("home")), "draw": _pct(pr.get("percent", {}).get("draw")),
                "away": _pct(pr.get("percent", {}).get("away")),
                "advice": pr.get("advice", ""), "under_over": pr.get("under_over"),
                "home_side": side(teams.get("home")), "away_side": side(teams.get("away")),
                "cmp_total": {k: _pct(v) for k, v in ((r.get("comparison") or {}).get("total") or {}).items()},
                "h2h": h2h,
            }
        except Exception:
            return None

    # ---------- correspondance matchs ----------
    def match_all(self, events):
        fixtures = []
        with self.lock:
            for c in self.data["fixtures"].values():
                fixtures.extend(c.get("list", []))
        out = {}
        for ev in events:
            try:
                t = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            best, score = None, 0.0
            for fx in fixtures:
                try:
                    ft = datetime.fromisoformat(fx["date"].replace("Z", "+00:00"))
                except ValueError:
                    continue
                if abs((ft - t).total_seconds()) > 3 * 3600:
                    continue
                sh = max(_sim(ev.get("home_team", ""), fx["home"]), _sim(ev.get("home_team", ""), fx.get("home_short", "")))
                sa = max(_sim(ev.get("away_team", ""), fx["away"]), _sim(ev.get("away_team", ""), fx.get("away_short", "")))
                sc = (sh + sa) / 2
                if sc > score:
                    best, score = fx, sc
            if best and score >= 0.55:
                out[ev.get("id")] = best
        return out

    def info_for(self, events):
        """event_id -> analyse complète (forme, classement, absents, modèle, confrontations, compos)."""
        mapping = self.match_all(events)
        res = {}
        with self.lock:
            all_results = []
            for c in self.data["results"].values():
                all_results.extend(c.get("v") or [])
            ratings = {}
            for lid, c in self.data["results"].items():
                ratings[lid] = model.fit_ratings(c.get("v") or [])
            inj_by_fx = {}
            for c in self.data["inj_day"].values():
                for fid, lst in (c.get("v") or {}).items():
                    inj_by_fx[fid] = lst
            for eid, fx in mapping.items():
                fid = str(fx["id"])
                lid = str(fx.get("league"))
                inj = inj_by_fx.get(fid, [])
                inj_h = [i for i in inj if i.get("team_id") == fx.get("home_id")]
                inj_a = [i for i in inj if i.get("team_id") == fx.get("away_id")]
                table = (self.data["standings"].get(lid) or {}).get("v") or {}
                pred_api = (self.data["pred"].get(fid) or {}).get("v")
                mdl = model.predict(ratings.get(lid), fx.get("home_id"), fx.get("away_id"), inj_h, inj_a)
                hk = f"{min(fx['home_id'], fx['away_id'])}-{max(fx['home_id'], fx['away_id'])}"
                h2h = ((self.data.get("h2h") or {}).get(hk) or {}).get("v") or (pred_api or {}).get("h2h") or [
                    {"date": r["date"][:10], "home": r["home"], "away": r["away"], "gh": r["gh"], "ga": r["ga"]}
                    for r in all_results if {r["home_id"], r["away_id"]} == {fx.get("home_id"), fx.get("away_id")}]
                res[eid] = {
                    "fixture_id": fx["id"], "round": fx.get("round", ""), "round_label": round_label(fx.get("round", "")),
                    "home": fx["home"], "away": fx["away"],
                    "form_home": model.team_form(all_results, fx.get("home_id")),
                    "form_away": model.team_form(all_results, fx.get("away_id")),
                    "table_home": table.get(str(fx.get("home_id"))), "table_away": table.get(str(fx.get("away_id"))),
                    "inj_home": inj_h, "inj_away": inj_a,
                    "model": mdl, "pred": pred_api, "h2h": h2h[:10],
                    "h2h_sum": model.h2h_summary(h2h[:10], fx["home"], fx["away"]),
                    "lineups": (self.data["lineups"].get(fid) or {}).get("list") or [],
                }
        return res

    def status(self):
        with self.lock:
            used = self.data["usage"]["n"] if self.data["usage"].get("day") == \
                datetime.now(timezone.utc).strftime("%Y-%m-%d") else 0
        return {"running": self.running, "error": self.last_error, "used_today": used,
                "remaining": self.remaining}
