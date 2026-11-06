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

# recherche web sans clé : données publiques ESPN (absents, compos, actus)
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
ESPN_SLUGS = {
    "soccer_france_ligue_one": "fra.1", "soccer_france_ligue_two": "fra.2", "soccer_epl": "eng.1",
    "soccer_efl_champ": "eng.2", "soccer_spain_la_liga": "esp.1", "soccer_italy_serie_a": "ita.1",
    "soccer_germany_bundesliga": "ger.1", "soccer_uefa_champs_league": "uefa.champions",
    "soccer_uefa_europa_league": "uefa.europa", "soccer_uefa_europa_conference_league": "uefa.europa.conf",
    "soccer_netherlands_eredivisie": "ned.1", "soccer_portugal_primeira_liga": "por.1",
    "soccer_belgium_first_div": "bel.1", "soccer_turkey_super_league": "tur.1",
}

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
                     "usage": {"day": "", "n": 0}, "h2h": {}, "scorers": {}}
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
                if not self._fresh("scorers", code, 12 * 3600):
                    body = self._fd_get(key, f"/competitions/{code}/scorers", {"limit": 60})
                    sc = []
                    for x in body.get("scorers", []):
                        try:
                            sc.append({"name": x["player"]["name"], "team_id": x["team"]["id"],
                                       "goals": x.get("goals") or 0, "assists": x.get("assists") or 0,
                                       "played": x.get("playedMatches") or 0})
                        except (KeyError, TypeError):
                            continue
                    self._put("scorers", code, sc, on_change)
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

    # ---------- recherche web (ESPN, sans clé) ----------
    def _web_get(self, url):
        wait = 1.0 - (time.time() - getattr(self, "_web_last", 0))
        if wait > 0:
            time.sleep(wait)
        self._web_last = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 BetScope", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    @staticmethod
    def _inj_type(status):
        st = str(status or "").lower()
        if any(k in st for k in ("doubt", "question", "day-to-day", "probable", "incertain")):
            return "Questionable"
        return "Missing Fixture"

    def update_web(self, sports, events, on_change=None):
        """Va chercher sur le web (ESPN) : absents, compos officielles et actus de chaque match à venir."""
        if getattr(self, "web_running", False):
            return
        self.web_running = True
        self.web_error = ""
        web = self.data.setdefault("web", {})
        board = self.data.setdefault("web_board", {})
        errors, n_ok = [], 0
        try:
            now = datetime.now(timezone.utc)
            by_sport = {}
            for ev in events:
                try:
                    t = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
                except (KeyError, ValueError):
                    continue
                if now - timedelta(hours=2) < t < now + timedelta(days=4):
                    by_sport.setdefault(ev.get("_sport_key") or ev.get("sport_key"), []).append((t, ev))
            for sk, lst in by_sport.items():
                slug = ESPN_SLUGS.get(sk)
                if not slug:
                    continue
                # calendrier ESPN sur la période (1 requête / ligue / 3 h)
                c = board.get(slug)
                if not c or time.time() - c["ts"] > 3 * 3600:
                    # ESPN n'accepte qu'un jour par requête -> 1 requête par jour de match
                    days = sorted({(t.astimezone(timezone.utc)).strftime("%Y%m%d") for t, _ in lst})
                    evs, ok = [], False
                    for day in days:
                        try:
                            body = self._web_get(f"{ESPN_BASE}/{slug}/scoreboard?dates={day}")
                            ok = True
                        except Exception as e:
                            err = f"recherche web ({slug}) : {e}"
                            continue
                        for e in body.get("events", []):
                            try:
                                comp = e["competitions"][0]
                                teams = {c2["homeAway"]: c2["team"] for c2 in comp["competitors"]}
                                evs.append({"id": e["id"], "date": e["date"],
                                            "home": teams["home"].get("displayName", ""),
                                            "home_short": teams["home"].get("shortDisplayName", ""),
                                            "away": teams["away"].get("displayName", ""),
                                            "away_short": teams["away"].get("shortDisplayName", "")})
                            except (KeyError, IndexError, TypeError):
                                continue
                    if not ok:
                        errors.append(err)
                        continue
                    board[slug] = c = {"ts": time.time(), "list": evs}
                # détail de chaque match : plus souvent quand le match approche (compos ~1 h avant)
                for t, ev in sorted(lst, key=lambda x: x[0]):
                    m = self._match_one(ev, c["list"])
                    if not m:
                        continue
                    mins = (t - now).total_seconds() / 60
                    ttl = 10 * 60 if mins < 120 else (45 * 60 if mins < 24 * 60 else 3 * 3600)
                    cur = web.get(ev["id"])
                    if cur and time.time() - cur["ts"] < ttl:
                        continue
                    try:
                        j = self._web_get(f"{ESPN_BASE}/{slug}/summary?event={m['id']}")
                        n_ok += 1
                    except Exception as e:
                        errors.append(f"recherche web : {e}")
                        continue
                    with self.lock:
                        web[ev["id"]] = {"ts": time.time(), "v": self._parse_summary(j)}
                        self._save()
                    if on_change:
                        on_change()
        except Exception as e:
            errors.append(f"recherche web : {e}")
        finally:
            # erreur affichée seulement si rien n'a marché (un championnat absent d'ESPN n'est pas grave)
            self.web_error = errors[0] if errors and not n_ok and not web else ""
            self.web_running = False
            with self.lock:
                self._save()
            if on_change:
                on_change()

    def _parse_summary(self, j):
        out = {"inj_home": [], "inj_away": [], "lineups": [], "news": []}
        side_by_team = {}
        try:
            for c in j["header"]["competitions"][0]["competitors"]:
                side_by_team[str(c.get("id") or c.get("team", {}).get("id"))] = c.get("homeAway")
        except (KeyError, IndexError, TypeError):
            pass
        for blk in j.get("injuries") or []:
            try:
                side = side_by_team.get(str(blk.get("team", {}).get("id")))
                for i in blk.get("injuries") or []:
                    name = (i.get("athlete") or {}).get("displayName") or ""
                    status = i.get("status") or (i.get("type") or {}).get("description") or ""
                    reason = ((i.get("details") or {}).get("type") or i.get("longComment") or "")
                    if name and side in ("home", "away"):
                        out[f"inj_{side}"].append({"player": name, "type": self._inj_type(status),
                                                   "reason": str(reason)[:60], "status": status})
            except (AttributeError, TypeError):
                continue
        for r in j.get("rosters") or []:
            try:
                xi = [p["athlete"]["displayName"] for p in r.get("roster", []) if p.get("starter")]
                if xi:
                    out["lineups"].append({"team": (r.get("team") or {}).get("displayName", ""),
                                           "formation": r.get("formation") or "", "xi": xi,
                                           "side": r.get("homeAway")})
            except (KeyError, TypeError):
                continue
        # forme récente, face-à-face et classement (ESPN, sans clé)
        out["form"] = {}
        for blk in ((j.get("boxscore") or {}).get("form") or []):
            try:
                side = side_by_team.get(str((blk.get("team") or {}).get("id")))
                games = []
                for e in blk.get("events") or []:
                    g = self._espn_game(e)
                    if g:
                        games.append(g)
                if side in ("home", "away") and games:
                    out["form"][side] = games
            except (AttributeError, TypeError):
                continue
        out["h2h"] = []
        for blk in (j.get("headToHeadGames") or [])[:1]:
            try:
                tname = (blk.get("team") or {}).get("displayName", "")
                for e in blk.get("events") or []:
                    g = self._espn_game(e)
                    if g:
                        out["h2h"].append({"date": g["date"], "home": tname if g["home"] else g["opp"],
                                           "away": g["opp"] if g["home"] else tname,
                                           "gh": g["gf"] if g["home"] else g["ga"], "ga": g["ga"] if g["home"] else g["gf"],
                                           "comp": e.get("leagueName") or e.get("leagueAbbreviation") or ""})
            except (AttributeError, TypeError):
                continue
        out["table"] = {}
        try:
            groups = (j.get("standings") or {}).get("groups") or []
            for gr in groups:
                for ent in ((gr.get("standings") or {}).get("entries") or []):
                    tid = str(ent.get("id") or (ent.get("team") or {}).get("id") or "")
                    side = side_by_team.get(tid)
                    if not side:
                        continue
                    st = {x.get("name") or x.get("type"): x.get("value") for x in ent.get("stats") or []}
                    out["table"][side] = {"rank": int(st.get("rank") or 0) or None,
                                          "points": int(st.get("points") or 0)}
        except (AttributeError, TypeError, ValueError):
            pass
        for a in ((j.get("news") or {}).get("articles") or [])[:6]:
            try:
                out["news"].append({"title": a.get("headline", ""), "desc": (a.get("description") or "")[:220],
                                    "date": (a.get("published") or "")[:10],
                                    "url": ((a.get("links") or {}).get("web") or {}).get("href", "")})
            except (AttributeError, TypeError):
                continue
        return out

    @staticmethod
    def _espn_game(e):
        res = str(e.get("gameResult") or "").upper()[:1]
        if res not in ("W", "D", "L"):
            return None
        nums = [int(x) for x in re.findall(r"\d+", str(e.get("score") or ""))[:2]]
        if len(nums) < 2:
            hs, as_ = e.get("homeTeamScore"), e.get("awayTeamScore")
            try:
                nums = [int(hs), int(as_)]
            except (TypeError, ValueError):
                return None
        hi, lo = max(nums), min(nums)
        gf, ga = (hi, lo) if res == "W" else ((lo, hi) if res == "L" else (nums[0], nums[1]))
        return {"res": {"W": "V", "D": "N", "L": "D"}[res], "gf": gf, "ga": ga,
                "opp": (e.get("opponent") or {}).get("displayName", ""),
                "home": str(e.get("atVs") or "vs").strip().lower() != "@",
                "date": str(e.get("gameDate") or "")[:10]}

    def _match_one(self, ev, cands):
        try:
            t = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            return None
        best, score = None, 0.0
        for fx in cands:
            try:
                ft = datetime.fromisoformat(fx["date"].replace("Z", "+00:00"))
            except ValueError:
                continue
            if abs((ft - t).total_seconds()) > 3 * 3600:
                continue
            sh = max(_sim(ev.get("home_team", ""), fx["home"]), _sim(ev.get("home_team", ""), fx.get("home_short", "")))
            sa = max(_sim(ev.get("away_team", ""), fx["away"]), _sim(ev.get("away_team", ""), fx.get("away_short", "")))
            if (sh + sa) / 2 > score:
                best, score = fx, (sh + sa) / 2
        return best if score >= 0.55 else None

    def web_for(self, event_id):
        with self.lock:
            return ((self.data.get("web") or {}).get(event_id) or {}).get("v")

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
                sc_all = []
                for c in (self.data.get("scorers") or {}).values():
                    sc_all.extend(c.get("v") or [])

                def team_scorers(tid):
                    best = {}
                    for x in sc_all:
                        if x["team_id"] == tid and x["goals"] >= best.get(x["name"], {}).get("goals", -1):
                            best[x["name"]] = x
                    return sorted(best.values(), key=lambda x: -x["goals"])[:4]

                def team_totals(tid):
                    g = m = 0
                    for r in all_results:
                        if r["home_id"] == tid:
                            g += r["gh"] or 0
                            m += 1
                        elif r["away_id"] == tid:
                            g += r["ga"] or 0
                            m += 1
                    return g, m
                res[eid] = {
                    "scorers_home": team_scorers(fx.get("home_id")), "scorers_away": team_scorers(fx.get("away_id")),
                    "goals_home": team_totals(fx.get("home_id")), "goals_away": team_totals(fx.get("away_id")),
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
                w = ((self.data.get("web") or {}).get(eid) or {}).get("v")
                if w:
                    if not inj_h and not inj_a and (w["inj_home"] or w["inj_away"]):
                        res[eid]["inj_home"], res[eid]["inj_away"] = w["inj_home"], w["inj_away"]
                        res[eid]["model"] = model.predict(ratings.get(lid), fx.get("home_id"), fx.get("away_id"),
                                                          w["inj_home"], w["inj_away"])
                    if not res[eid]["lineups"]:
                        res[eid]["lineups"] = w["lineups"]
                    res[eid]["news"] = w["news"]
                    self._fill_from_web(res[eid], w)
            # matchs sans stats (pas de clé) : on garde quand même les infos web
            for ev in events:
                eid = ev.get("id")
                w = ((self.data.get("web") or {}).get(eid) or {}).get("v")
                if eid in res or not w:
                    continue
                res[eid] = {"fixture_id": None, "round": "", "round_label": "", "home": ev.get("home_team", ""),
                            "away": ev.get("away_team", ""), "form_home": None, "form_away": None,
                            "table_home": None, "table_away": None, "inj_home": w["inj_home"],
                            "inj_away": w["inj_away"], "model": None, "pred": None, "h2h": [], "h2h_sum": None,
                            "lineups": w["lineups"], "news": w["news"]}
                self._fill_from_web(res[eid], w)
            for eid, r in res.items():
                r["sources"] = self._sources(eid, r)
        return res

    @staticmethod
    def _form_from_games(games):
        if not games:
            return None
        games = sorted(games, key=lambda g: g["date"], reverse=True)
        last = games[:5]
        pts = lambda gs: sum(3 if g["res"] == "V" else (1 if g["res"] == "N" else 0) for g in gs)
        first = games[0]["res"]
        streak = 0
        for g in games:
            if g["res"] != first:
                break
            streak += 1
        unbeaten = next((i for i, g in enumerate(games) if g["res"] == "D"), len(games))
        winless = next((i for i, g in enumerate(games) if g["res"] == "V"), len(games))
        txt = (f"{streak} victoires de suite" if first == "V" and streak >= 2 else
               f"{streak} défaites de suite" if first == "D" and streak >= 2 else
               f"invaincu depuis {unbeaten} matchs" if unbeaten >= 4 else
               f"aucune victoire depuis {winless} matchs" if winless >= 4 else "")
        rec = lambda gs: f"{sum(g['res'] == 'V' for g in gs)}V {sum(g['res'] == 'N' for g in gs)}N {sum(g['res'] == 'D' for g in gs)}D"
        hg, ag = [g for g in games if g["home"]], [g for g in games if not g["home"]]
        return {"last": [{"res": g["res"], "score": f"{g['gf']}-{g['ga']}", "opp": g["opp"], "home": g["home"],
                          "date": g["date"][8:10] + "/" + g["date"][5:7]} for g in last],
                "form": "".join(g["res"] for g in last), "ppg5": round(pts(last) / len(last), 2),
                "ppg": round(pts(games) / len(games), 2), "gf5": sum(g["gf"] for g in last),
                "ga5": sum(g["ga"] for g in last), "wins_streak": streak if first == "V" else 0,
                "loss_streak": streak if first == "D" else 0, "unbeaten": unbeaten, "winless": winless,
                "streak_txt": txt, "played": len(games), "home_rec": rec(hg), "away_rec": rec(ag),
                "home_ppg": round(pts(hg) / len(hg), 2) if hg else None,
                "away_ppg": round(pts(ag) / len(ag), 2) if ag else None, "rest_days": None,
                "over25": round(sum(g["gf"] + g["ga"] > 2.5 for g in last) / len(last), 2),
                "btts": round(sum(g["gf"] > 0 and g["ga"] > 0 for g in last) / len(last), 2),
                "scored_in": round(sum(g["gf"] > 0 for g in last) / len(last), 2), "src": "ESPN"}

    def _fill_from_web(self, r, w):
        f = w.get("form") or {}
        if not r.get("form_home") and f.get("home"):
            r["form_home"] = self._form_from_games(f["home"])
        if not r.get("form_away") and f.get("away"):
            r["form_away"] = self._form_from_games(f["away"])
        if not r.get("h2h") and w.get("h2h"):
            r["h2h"] = w["h2h"][:10]
            r["h2h_sum"] = model.h2h_summary(r["h2h"], r["home"], r["away"])
        t = w.get("table") or {}
        if not r.get("table_home") and t.get("home"):
            r["table_home"] = t["home"]
        if not r.get("table_away") and t.get("away"):
            r["table_away"] = t["away"]

    def _sources(self, eid, r):
        """Ce qui a été consulté pour ce match (affiché tel quel, pour la transparence)."""
        web = (self.data.get("web") or {}).get(eid)
        when = lambda ts: datetime.fromtimestamp(ts).strftime("%d/%m %H:%M") if ts else ""
        fsrc = (r.get("form_home") or {}).get("src") or ("football-data.org" if r.get("form_home") else "")
        n_inj = len(r.get("inj_home") or []) + len(r.get("inj_away") or [])
        return [
            {"name": "Forme récente des 2 équipes", "ok": bool(r.get("form_home") and r.get("form_away")),
             "detail": fsrc or "introuvable"},
            {"name": "Modèle de buts (saison complète)", "ok": bool(r.get("model")),
             "detail": "football-data.org" if r.get("model") else "nécessite la clé football-data.org"},
            {"name": "Classement", "ok": bool(r.get("table_home") and r.get("table_away")), "detail": ""},
            {"name": "Confrontations directes", "ok": bool(r.get("h2h")), "detail": f"{len(r.get('h2h') or [])} matchs"},
            {"name": "Absents (blessés / suspendus)", "ok": web is not None,
             "detail": (f"{n_inj} signalé{'s' if n_inj > 1 else ''}" if web else "pas encore vérifié") +
                       (f" · ESPN {when(web['ts'])}" if web else "")},
            {"name": "Compositions officielles", "ok": bool(r.get("lineups")),
             "detail": "publiées" if r.get("lineups") else "pas encore publiées (~1 h avant le match)"},
            {"name": "Actualités du match", "ok": bool(r.get("news")), "detail": f"{len(r.get('news') or [])} article(s)"},
        ]

    def status(self):
        with self.lock:
            used = self.data["usage"]["n"] if self.data["usage"].get("day") == \
                datetime.now(timezone.utc).strftime("%Y-%m-%d") else 0
        return {"running": self.running or getattr(self, "web_running", False),
                "error": self.last_error or getattr(self, "web_error", ""), "used_today": used,
                "web_matches": len(self.data.get("web") or {}),
                "remaining": self.remaining}
