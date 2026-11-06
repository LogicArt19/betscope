# -*- coding: utf-8 -*-
"""
Modèle « forme » de BetScope.

- Force d'attaque / de défense de chaque équipe calculée sur tous les matchs de la saison,
  avec un poids qui décroît avec l'ancienneté (demi-vie ~45 jours) : les derniers matchs
  comptent beaucoup plus -> c'est la forme du moment.
- Buts attendus via un modèle de Poisson (domicile / extérieur), ajustés selon les absents.
- Donne P(victoire dom.), P(nul), P(victoire ext.), P(plus/moins de X buts).
- Calcule aussi les séries et les infos de forme lisibles (5 derniers matchs, etc.).
"""

import math
from datetime import datetime, timezone

HALF_LIFE_DAYS = 45.0
PRIOR_MATCHES = 3.0       # rétrécit les notes en début de saison
INJ_OUT = 0.025           # -2,5 % de buts attendus par joueur absent
INJ_DOUBT = 0.01
INJ_CAP = 0.12


def _dt(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def fit_ratings(results, ref=None):
    """results : [{date, home_id, away_id, gh, ga}] -> (att, dfn, avg_home, avg_away, n_matchs)"""
    ref = ref or datetime.now(timezone.utc)
    rows = []
    for r in results:
        try:
            age = max(0.0, (ref - _dt(r["date"])).total_seconds() / 86400)
            rows.append((r["home_id"], r["away_id"], float(r["gh"]), float(r["ga"]),
                         0.5 ** (age / HALF_LIFE_DAYS)))
        except (KeyError, TypeError, ValueError):
            continue
    if len(rows) < 10:
        return None
    sw = sum(w for *_, w in rows)
    avg_h = sum(gh * w for _, _, gh, _, w in rows) / sw
    avg_a = sum(ga * w for _, _, _, ga, w in rows) / sw
    teams = {t for h, a, *_ in rows for t in (h, a)}
    att = {t: 1.0 for t in teams}
    dfn = {t: 1.0 for t in teams}
    n = {t: 0 for t in teams}
    for h, a, *_ in rows:
        n[h] += 1
        n[a] += 1
    for _ in range(40):
        num = {t: PRIOR_MATCHES * (avg_h + avg_a) / 2 for t in teams}
        den = {t: PRIOR_MATCHES * (avg_h + avg_a) / 2 for t in teams}
        for h, a, gh, ga, w in rows:
            num[h] += w * gh
            den[h] += w * avg_h * dfn[a]
            num[a] += w * ga
            den[a] += w * avg_a * dfn[h]
        att = {t: num[t] / den[t] for t in teams}
        num = {t: PRIOR_MATCHES * (avg_h + avg_a) / 2 for t in teams}
        den = {t: PRIOR_MATCHES * (avg_h + avg_a) / 2 for t in teams}
        for h, a, gh, ga, w in rows:
            num[h] += w * ga
            den[h] += w * avg_a * att[a]
            num[a] += w * gh
            den[a] += w * avg_h * att[h]
        dfn = {t: num[t] / den[t] for t in teams}
        ma = sum(att.values()) / len(att)
        md = sum(dfn.values()) / len(dfn)
        att = {t: v / ma for t, v in att.items()}
        dfn = {t: v / md for t, v in dfn.items()}
    return {"att": att, "dfn": dfn, "avg_h": avg_h, "avg_a": avg_a, "n": n}


def _inj_factor(injuries):
    out = sum(1 for i in injuries if "missing" in str(i.get("type", "")).lower())
    doubt = sum(1 for i in injuries if "question" in str(i.get("type", "")).lower())
    return 1 - min(INJ_CAP, out * INJ_OUT + doubt * INJ_DOUBT)


def _pois(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def predict(ratings, home_id, away_id, inj_home=(), inj_away=()):
    if not ratings:
        return None
    att, dfn = ratings["att"], ratings["dfn"]
    if home_id not in att or away_id not in att:
        return None
    if ratings["n"].get(home_id, 0) < 3 or ratings["n"].get(away_id, 0) < 3:
        return None
    lh = ratings["avg_h"] * att[home_id] * dfn[away_id] * _inj_factor(inj_home)
    la = ratings["avg_a"] * att[away_id] * dfn[home_id] * _inj_factor(inj_away)
    lh, la = max(0.15, min(lh, 5.0)), max(0.1, min(la, 5.0))
    ph = [_pois(i, lh) for i in range(11)]
    pa = [_pois(i, la) for i in range(11)]
    home = draw = away = 0.0
    totals = {}
    for i in range(11):
        for j in range(11):
            p = ph[i] * pa[j]
            if i > j:
                home += p
            elif i == j:
                draw += p
            else:
                away += p
            totals[i + j] = totals.get(i + j, 0) + p
    s = home + draw + away
    # léger ajustement des nuls (le Poisson simple les sous-estime un peu)
    draw *= 1.06
    s2 = home + draw + away
    return {"xg_home": lh, "xg_away": la, "home": home / s2, "draw": draw / s2, "away": away / s2,
            "totals": {k: v / s for k, v in totals.items()}}


def p_over(pred, point):
    if not pred:
        return None
    return sum(v for k, v in pred["totals"].items() if k > point)


def team_form(results, team_id, ref=None, n=5):
    """Infos lisibles sur la forme d'une équipe (toutes compétitions suivies)."""
    games = []
    for r in results:
        if team_id not in (r.get("home_id"), r.get("away_id")):
            continue
        try:
            d = _dt(r["date"])
        except (KeyError, ValueError):
            continue
        home = r["home_id"] == team_id
        gf, ga = (r["gh"], r["ga"]) if home else (r["ga"], r["gh"])
        if gf is None or ga is None:
            continue
        res = "V" if gf > ga else ("N" if gf == ga else "D")
        games.append({"date": d, "home": home, "gf": gf, "ga": ga, "res": res,
                      "opp": r["away"] if home else r["home"]})
    if not games:
        return None
    games.sort(key=lambda g: g["date"], reverse=True)
    last = games[:n]
    pts = lambda gs: sum(3 if g["res"] == "V" else (1 if g["res"] == "N" else 0) for g in gs)
    # séries
    first = games[0]["res"]
    streak = 0
    for g in games:
        if g["res"] == first:
            streak += 1
        else:
            break
    unbeaten = 0
    for g in games:
        if g["res"] == "D":
            break
        unbeaten += 1
    winless = 0
    for g in games:
        if g["res"] == "V":
            break
        winless += 1
    if first == "V" and streak >= 2:
        streak_txt = f"{streak} victoires de suite"
    elif first == "D" and streak >= 2:
        streak_txt = f"{streak} défaites de suite"
    elif unbeaten >= 4:
        streak_txt = f"invaincu depuis {unbeaten} matchs"
    elif winless >= 4:
        streak_txt = f"aucune victoire depuis {winless} matchs"
    else:
        streak_txt = ""
    home_g = [g for g in games if g["home"]]
    away_g = [g for g in games if not g["home"]]
    rec = lambda gs: f"{sum(g['res'] == 'V' for g in gs)}V {sum(g['res'] == 'N' for g in gs)}N {sum(g['res'] == 'D' for g in gs)}D"
    last10 = games[:10]
    ref = ref or datetime.now(timezone.utc)
    return {
        "home_rec": rec(home_g), "away_rec": rec(away_g),
        "home_ppg": round(pts(home_g) / len(home_g), 2) if home_g else None,
        "away_ppg": round(pts(away_g) / len(away_g), 2) if away_g else None,
        "rest_days": round((ref - games[0]["date"]).total_seconds() / 86400, 1),
        "over25": round(sum(g["gf"] + g["ga"] > 2.5 for g in last10) / len(last10), 2),
        "btts": round(sum(g["gf"] > 0 and g["ga"] > 0 for g in last10) / len(last10), 2),
        "scored_in": round(sum(g["gf"] > 0 for g in last10) / len(last10), 2),
        "last": [{"res": g["res"], "score": f"{g['gf']}-{g['ga']}", "opp": g["opp"], "home": g["home"],
                  "date": g["date"].strftime("%d/%m")} for g in last],
        "form": "".join(g["res"] for g in last),
        "ppg5": round(pts(last) / len(last), 2),
        "ppg": round(pts(games) / len(games), 2),
        "gf5": sum(g["gf"] for g in last), "ga5": sum(g["ga"] for g in last),
        "wins_streak": streak if first == "V" else 0,
        "loss_streak": streak if first == "D" else 0,
        "unbeaten": unbeaten, "winless": winless, "streak_txt": streak_txt,
        "played": len(games),
    }


def h2h_summary(h2h, home_name, away_name):
    """h2h : [{date, home, away, gh, ga}] -> bilan du point de vue de l'équipe à domicile."""
    from difflib import SequenceMatcher
    same = lambda x, y: SequenceMatcher(None, str(x).lower(), str(y).lower()).ratio() > 0.6
    w = d = l = goals = 0
    n = 0
    for m in h2h:
        if m.get("gh") is None or m.get("ga") is None:
            continue
        n += 1
        goals += m["gh"] + m["ga"]
        hg, ag = (m["gh"], m["ga"]) if same(m["home"], home_name) else (m["ga"], m["gh"])
        if hg > ag:
            w += 1
        elif hg == ag:
            d += 1
        else:
            l += 1
    if not n:
        return None
    return {"n": n, "w": w, "d": d, "l": l, "avg_goals": round(goals / n, 2)}
