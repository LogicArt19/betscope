# -*- coding: utf-8 -*-
"""
Historique Cotescope : propositions de l'appli + tickets joués, vérifiés automatiquement
après les matchs (résultats ESPN, gratuits et sans clé), bilan et contrôle de fiabilité.
"""

import json
import os
import re
import threading
import time
import unicodedata
import urllib.request
from datetime import datetime, timedelta, timezone

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"


def _n(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z ]", " ", s).split()


def same_player(a, b):
    a, b = _n(a), _n(b)
    return bool(a and b) and a[-1] == b[-1]


def settle_key(key, res, home_name="", away_name=""):
    """'won' / 'lost' / 'void' (remboursé) / None (non vérifiable) pour une sélection."""
    h, a = res["h"], res["a"]
    t = h + a
    m = re.match(r"^r([1N2])$", key)
    if m:
        return "won" if {"1": h > a, "N": h == a, "2": a > h}[m.group(1)] else "lost"
    m = re.match(r"^dc(1N|N2|12)$", key)
    if m:
        ok = {"1N": h >= a, "N2": a >= h, "12": h != a}[m.group(1)]
        return "won" if ok else "lost"
    m = re.match(r"^dnb([12])$", key)
    if m:
        if h == a:
            return "void"
        return "won" if (h > a) == (m.group(1) == "1") else "lost"
    if key in ("bttsY", "bttsN"):
        y = h > 0 and a > 0
        return "won" if y == (key == "bttsY") else "lost"
    m = re.match(r"^([ou])([\d.]+)$", key)
    if m:
        line = float(m.group(2))
        return "won" if (t > line) == (m.group(1) == "o") else "lost"
    m = re.match(r"^t([01])([ou])([\d.]+)$", key)
    if m:
        g = h if m.group(1) == "0" else a
        return "won" if (g > float(m.group(3))) == (m.group(2) == "o") else "lost"
    hcp = {"h1m2": h - a >= 2, "h2m2": a - h >= 2, "h1p1": a - h <= 1, "h2p1": h - a <= 1}
    if key in hcp:
        return "won" if hcp[key] else "lost"
    m = re.match(r"^s(\d)(\d)$", key)
    if m:
        return "won" if (h, a) == (int(m.group(1)), int(m.group(2))) else "lost"
    m = re.match(r"^m([1N2])([1N2])$", key)
    if m:
        if res.get("ht_h") is None:
            return None
        hh, ha = res["ht_h"], res["ht_a"]
        ht = "1" if hh > ha else ("N" if hh == ha else "2")
        ft = "1" if h > a else ("N" if h == a else "2")
        return "won" if (ht, ft) == (m.group(1), m.group(2)) else "lost"
    if key.startswith("sc_") or key.startswith("de_"):
        name = key[3:]
        sc = res.get("scorers")
        if sc is None:
            return None
        if any(same_player(name, x) for x in sc):
            return "won"
        if key.startswith("de_"):
            return None if res.get("assists") is None else (
                "won" if any(same_player(name, x) for x in res["assists"]) else "lost")
        return "lost"
    return None


class History:
    def __init__(self, folder):
        self.path = os.path.join(folder, "history.json")
        self.lock = threading.RLock()
        self.data = {"app": [], "user": [], "results": {}}
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass
        self.running = False
        self.error = ""

    def save(self):
        with self.lock:
            try:
                with open(self.path, "w", encoding="utf-8") as f:
                    json.dump(self.data, f)
            except OSError:
                pass

    # ---------- enregistrement ----------
    @staticmethod
    def leg_of(s):
        return {"uid": s["uid"], "event_id": s["event_id"], "key": s.get("key"), "sport_key": s.get("sport_key"),
                "match": s["match"], "home": s.get("home"), "away": s.get("away"),
                "start": s["start"].isoformat() if hasattr(s["start"], "isoformat") else s["start"],
                "selection": s["selection"], "market": s["market"], "cat": s.get("cat"),
                "odds": s["odds"], "p": round(s["p"], 4), "estimated": bool(s.get("estimated")), "status": "pending"}

    def log_app(self, picks):
        """Mémorise les paris que Cotescope « jouerait » (1 fois par sélection)."""
        with self.lock:
            known = {e["uid"] for e in self.data["app"]}
            added = 0
            for s in picks:
                if s["uid"] in known or not s.get("key"):
                    continue
                e = self.leg_of(s)
                e["created"] = time.time()
                self.data["app"].append(e)
                added += 1
            self.data["app"] = self.data["app"][-2000:]
            if added:
                self.save()

    def log_ticket(self, legs, stake, label=""):
        with self.lock:
            t = {"id": f"t{int(time.time() * 1000)}", "created": time.time(), "stake": float(stake),
                 "label": label, "legs": legs, "status": "pending", "payout": None}
            self.data["user"].append(t)
            self.save()
            return t

    def log_manual(self, stake, odds, n_legs=1, status="pending", label="", when=None):
        """Pari pris ailleurs, saisi à la main (pour le coach : il compte comme les autres)."""
        with self.lock:
            t = {"id": f"m{int(time.time() * 1000)}", "created": float(when or time.time()), "stake": float(stake),
                 "label": label, "legs": [], "manual": True, "odds": float(odds), "n_legs": int(n_legs or 1),
                 "status": status, "payout": round(float(stake) * float(odds), 2) if status == "won" else (0.0 if status == "lost" else None)}
            self.data["user"].append(t)
            self.save()
            return t

    def set_result(self, tid, status):
        with self.lock:
            for t in self.data["user"]:
                if t["id"] == tid and t.get("manual") and status in ("won", "lost", "pending"):
                    t["status"] = status
                    t["payout"] = round(t["stake"] * t["odds"], 2) if status == "won" else (0.0 if status == "lost" else None)
            self.save()

    @staticmethod
    def ticket_odds(t):
        if t.get("manual"):
            return float(t.get("odds") or 1)
        o = 1.0
        for l in t.get("legs") or []:
            o *= float(l.get("odds") or 1)
        return o

    def coach(self):
        """Le coach : ce que tes propres paris disent de tes habitudes (heures, jours, combinés, cotes, mises
        après une perte). Rien n'est affiché sans assez de paris pour que ce soit fiable."""
        with self.lock:
            user = [dict(t) for t in self.data["user"]]
        done = sorted([t for t in user if t["status"] in ("won", "lost")], key=lambda t: t["created"])
        n = len(done)
        prof = lambda ts: sum((t["payout"] or 0) - t["stake"] for t in ts)
        stk = lambda ts: sum(t["stake"] for t in ts)
        roi = lambda ts: prof(ts) / stk(ts) if stk(ts) else 0.0
        out = {"n": n, "n_all": len(user), "enough": n >= 20, "insights": [], "good": [], "tilt": None}
        if not n:
            return out
        stakes = sorted(t["stake"] for t in done)
        med = stakes[len(stakes) // 2]
        cur = worst = 0
        for t in done:
            cur = cur + 1 if t["status"] == "lost" else 0
            worst = max(worst, cur)
        out.update(roi=roi(done), profit=round(prof(done), 2), staked=round(stk(done), 2), med_stake=med,
                   wins=sum(t["status"] == "won" for t in done), lose_streak=cur, worst_streak=worst)
        base = roi(done)

        def scan(label, groups, min_n=6):
            for name, ts in groups:
                if len(ts) < min_n:
                    continue
                r = roi(ts)
                if r < base - 0.15 and r < 0:
                    out["insights"].append({"kind": label, "text": f"Tes paris {name} : {r * 100:+.0f} % de rentabilité sur {len(ts)} paris, contre {base * 100:+.0f} % en moyenne.".replace(".", ",", 0)})
                elif r > base + 0.15 and r > 0 and len(ts) >= 15 and not any(k in name for k in ("4 ou plus", "grosse cote", "nuit")):
                    # jamais d'encouragement sur les paris risqués (gros combinés, grosses cotes, la nuit)
                    out["good"].append({"kind": label, "text": f"Tes paris {name} marchent mieux : {r * 100:+.0f} % sur {len(ts)} paris (avec peu de paris, ça peut aussi être de la chance)."})

        if n >= 20:
            hour = lambda t: datetime.fromtimestamp(t["created"]).hour
            scan("heure", [("pris la nuit (22 h – 6 h)", [t for t in done if hour(t) >= 22 or hour(t) < 6]),
                           ("pris le matin", [t for t in done if 6 <= hour(t) < 12]),
                           ("pris l'après-midi", [t for t in done if 12 <= hour(t) < 18]),
                           ("pris en soirée", [t for t in done if 18 <= hour(t) < 22])])
            JN = ["du lundi", "du mardi", "du mercredi", "du jeudi", "du vendredi", "du samedi", "du dimanche"]
            scan("jour", [(JN[d], [t for t in done if datetime.fromtimestamp(t["created"]).weekday() == d]) for d in range(7)])
            nl = lambda t: int(t.get("n_legs") or len(t.get("legs") or []) or 1)
            scan("combinés", [("en simple (1 sélection)", [t for t in done if nl(t) == 1]),
                              ("en combiné de 2 ou 3", [t for t in done if 2 <= nl(t) <= 3]),
                              ("en combiné de 4 ou plus", [t for t in done if nl(t) >= 4])])
            od = self.ticket_odds
            scan("cotes", [("à petite cote (moins de 1,8)", [t for t in done if od(t) < 1.8]),
                           ("à cote moyenne (1,8 à 3)", [t for t in done if 1.8 <= od(t) < 3]),
                           ("à grosse cote (3 à 10)", [t for t in done if 3 <= od(t) < 10]),
                           ("à très grosse cote (10 et plus)", [t for t in done if od(t) >= 10])])
        # se refaire après une perte : la mise grimpe juste après un ticket perdu
        after = [done[i + 1]["stake"] / max(0.01, med) for i in range(n - 1) if done[i]["status"] == "lost"]
        chase = sum(after) / len(after) if len(after) >= 4 else None
        if chase and chase >= 1.3:
            out["insights"].insert(0, {"kind": "tilt", "text": f"Après une perte, tu mises en moyenne {chase:.1f} fois ta mise habituelle. C'est le signe n° 1 du « tilt » : vouloir se refaire tout de suite.".replace(".", ",", 1)})
        # signaux des 7 derniers jours
        now = time.time()
        week = [t for t in user if now - t["created"] < 7 * 86400]
        today = [t for t in user if datetime.fromtimestamp(t["created"]).date() == datetime.now().date()]
        sig = []
        if len(today) >= 8:
            sig.append(f"{len(today)} paris aujourd'hui")
        if today and stk(today) > 3 * med * max(1, min(len(today), 3)):
            sig.append("mises du jour bien au-dessus de tes habitudes")
        if cur >= 4:
            sig.append(f"{cur} tickets perdus d'affilée")
        night = [t for t in week if datetime.fromtimestamp(t["created"]).hour < 6]
        if len(night) >= 3:
            sig.append(f"{len(night)} paris entre minuit et 6 h cette semaine")
        recent_after = [done[i + 1]["stake"] / max(0.01, med) for i in range(n - 1)
                        if done[i]["status"] == "lost" and now - done[i + 1]["created"] < 7 * 86400]
        if recent_after and max(recent_after) >= 2:
            sig.append("mise doublée juste après une perte cette semaine")
        out["tilt"] = {"level": "alerte" if len(sig) >= 2 else ("vigilance" if sig else "calme"), "signals": sig}
        return out

    def delete_ticket(self, tid):
        with self.lock:
            self.data["user"] = [t for t in self.data["user"] if t["id"] != tid]
            self.save()

    # ---------- vérification des résultats ----------
    def _get(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 Cotescope", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    def _results_for(self, slug, day):
        """Scores finaux ESPN d'un championnat pour un jour (mis en cache)."""
        k = f"{slug}|{day}"
        c = self.data["results"].get(k)
        if c and (c.get("final") or time.time() - c["ts"] < 1800):
            return c["v"]
        body = self._get(f"{ESPN_BASE}/{slug}/scoreboard?dates={day}")
        out, all_final = [], True
        for e in body.get("events", []):
            try:
                comp = e["competitions"][0]
                done = bool(((e.get("status") or {}).get("type") or {}).get("completed"))
                all_final &= done
                cs = {c["homeAway"]: c for c in comp["competitors"]}
                row = {"home": cs["home"]["team"].get("displayName", ""), "away": cs["away"]["team"].get("displayName", ""),
                       "home_short": cs["home"]["team"].get("shortDisplayName", ""),
                       "away_short": cs["away"]["team"].get("shortDisplayName", ""),
                       "date": e.get("date"), "done": done}
                if done:
                    row["h"], row["a"] = int(cs["home"].get("score")), int(cs["away"].get("score"))
                    lh = cs["home"].get("linescores") or []
                    la = cs["away"].get("linescores") or []
                    if lh and la:
                        row["ht_h"], row["ht_a"] = int(float(lh[0].get("value", 0))), int(float(la[0].get("value", 0)))
                    sc = []
                    for d in comp.get("details") or []:
                        txt = str(((d.get("type") or {}).get("text")) or "").lower()
                        if d.get("scoringPlay") or "goal" in txt:
                            if "own goal" in txt:
                                continue
                            for p in d.get("athletesInvolved") or []:
                                sc.append(p.get("displayName", ""))
                    row["scorers"] = sc if comp.get("details") is not None else None
                out.append(row)
            except (KeyError, TypeError, ValueError):
                continue
        self.data["results"][k] = {"ts": time.time(), "v": out, "final": bool(out) and all_final}
        return out

    @staticmethod
    def _sim(a, b):
        from difflib import SequenceMatcher
        na, nb = " ".join(_n(a)), " ".join(_n(b))
        if not na or not nb:
            return 0
        if na in nb or nb in na:
            return 1
        return SequenceMatcher(None, na, nb).ratio()

    def _find(self, leg, rows):
        best, sc = None, 0
        for r in rows:
            if not r.get("done"):
                continue
            s = (max(self._sim(leg["home"], r["home"]), self._sim(leg["home"], r["home_short"])) +
                 max(self._sim(leg["away"], r["away"]), self._sim(leg["away"], r["away_short"]))) / 2
            if s > sc:
                best, sc = r, s
        return best if sc >= 0.6 else None

    def settle_all(self, slugs):
        """Vérifie tous les paris en attente dont le match est fini (à lancer dans un thread)."""
        if self.running:
            return
        self.running = True
        self.error = ""
        try:
            now = datetime.now(timezone.utc)
            legs = [l for l in self.data["app"] if l["status"] == "pending"]
            for t in self.data["user"]:
                legs += [l for l in t["legs"] if l["status"] == "pending"]
            for l in legs:
                try:
                    st = datetime.fromisoformat(l["start"])
                except (TypeError, ValueError):
                    continue
                if st.tzinfo is None:
                    st = st.replace(tzinfo=timezone.utc)
                if now < st + timedelta(hours=2, minutes=15):
                    continue
                if now > st + timedelta(days=10):
                    l["status"] = "unknown"
                    continue
                slug = slugs.get(l.get("sport_key"))
                if not slug or not l.get("home"):
                    continue
                day = st.astimezone(timezone.utc).strftime("%Y%m%d")
                try:
                    rows = self._results_for(slug, day)
                except Exception as e:
                    self.error = f"vérification des résultats : {e}"
                    continue
                r = self._find(l, rows)
                if not r:
                    continue
                st_ = settle_key(l["key"], r)
                l["score"] = f"{r['h']}-{r['a']}"
                l["status"] = st_ or "unknown"
            for t in self.data["user"]:
                if t["status"] != "pending" or t.get("manual") or not t["legs"]:
                    continue
                sts = [l["status"] for l in t["legs"]]
                if "lost" in sts:
                    t["status"], t["payout"] = "lost", 0.0
                elif all(s in ("won", "void") for s in sts):
                    odds = 1.0
                    for l in t["legs"]:
                        odds *= l["odds"] if l["status"] == "won" else 1.0
                    t["status"], t["payout"] = "won", round(t["stake"] * odds, 2)
                elif all(s in ("won", "void", "unknown") for s in sts) and "unknown" in sts:
                    t["status"] = "unknown"
        finally:
            self.running = False
            self.save()

    # ---------- bilan ----------
    def summary(self):
        with self.lock:
            app = [e for e in self.data["app"] if e["status"] in ("won", "lost")]
            n = len(app)
            wins = sum(e["status"] == "won" for e in app)
            profit = sum((e["odds"] - 1) if e["status"] == "won" else -1 for e in app)
            buckets = []
            for lo, hi, lab in ((0, .4, "< 40 %"), (.4, .55, "40-55 %"), (.55, .7, "55-70 %"), (.7, 1.01, "> 70 %")):
                b = [e for e in app if lo <= e["p"] < hi]
                if b:
                    buckets.append({"label": lab, "n": len(b), "pred": sum(e["p"] for e in b) / len(b),
                                    "real": sum(e["status"] == "won" for e in b) / len(b)})
            user = self.data["user"]
            done = [t for t in user if t["status"] in ("won", "lost")]
            staked = sum(t["stake"] for t in done)
            won_amt = sum(t["payout"] or 0 for t in done)
            today = datetime.now().date()
            today_pl = sum((t["payout"] or 0) - t["stake"] for t in done
                           if datetime.fromtimestamp(t["created"]).date() == today)
            today_staked = sum(t["stake"] for t in user if datetime.fromtimestamp(t["created"]).date() == today)
            return {
                "app": {"n": n, "wins": wins, "rate": wins / n if n else None, "roi": profit / n if n else None,
                        "pending": sum(e["status"] == "pending" for e in self.data["app"]), "buckets": buckets},
                "user": {"n": len(done), "wins": sum(t["status"] == "won" for t in done), "staked": staked,
                         "profit": round(won_amt - staked, 2), "roi": (won_amt - staked) / staked if staked else None,
                         "pending": sum(t["status"] == "pending" for t in user),
                         "today_pl": round(today_pl, 2), "today_staked": round(today_staked, 2)},
                "tickets": sorted(user, key=lambda t: -t["created"])[:200],
                "app_list": sorted(self.data["app"], key=lambda e: e.get("start", ""), reverse=True)[:200],
                "running": self.running, "error": self.error,
            }
