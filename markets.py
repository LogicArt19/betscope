# -*- coding: utf-8 -*-
"""
Tous les marchés Betclic d'un match de foot, calculés à partir des buts attendus.

1. On retrouve les buts attendus (λ domicile / λ extérieur) qui reproduisent exactement
   les cotes du marché (1N2 + plus/moins 2,5), puis on les mélange avec le modèle forme.
2. Avec ces λ (loi de Poisson), on calcule la probabilité de n'importe quel pari :
   double chance, remboursé si nul, les 2 marquent, nombre de buts, buts par équipe,
   handicaps, score exact, mi-temps / fin de match, buteurs, joueurs décisifs.
3. Cote Betclic estimée = cote juste / marge habituelle de Betclic sur ce type de pari.
"""

import math

# marge habituelle de Betclic par type de pari (cote affichée ≈ cote juste / marge)
MARGINS = {"1n2": 1.06, "dc": 1.06, "dnb": 1.06, "btts": 1.07, "total": 1.07, "team": 1.09,
           "hcp": 1.08, "exact": 1.25, "mtft": 1.20, "scorer": 1.25, "decisive": 1.25}

CATEGORIES = {
    "1n2": "Résultat (1N2)", "dc": "Double chance", "dnb": "Remboursé si nul", "total": "Nombre de buts",
    "team": "Buts par équipe", "btts": "Les 2 équipes marquent", "hcp": "Handicap",
    "exact": "Score exact", "mtft": "Mi-temps / Fin de match", "scorer": "Buteurs", "decisive": "Joueurs décisifs",
}

N = 11


def _pmf(lam):
    return [math.exp(-lam) * lam ** k / math.factorial(k) for k in range(N)]


def grid(lh, la):
    ph, pa = _pmf(lh), _pmf(la)
    return [[ph[i] * pa[j] for j in range(N)] for i in range(N)]


def prob(g, cond):
    return sum(g[i][j] for i in range(N) for j in range(N) if cond(i, j))


def _p_over(T, line):
    return 1 - sum(math.exp(-T) * T ** k / math.factorial(k) for k in range(int(line) + 1))


def fit_lambdas(ph, pd, pa, pover=None, line=2.5):
    """λ domicile / extérieur qui reproduisent le marché."""
    if pover is not None and 0.03 < pover < 0.97:
        lo, hi = 0.2, 7.0
        for _ in range(50):
            mid = (lo + hi) / 2
            if _p_over(mid, line) < pover:
                lo = mid
            else:
                hi = mid
        T = (lo + hi) / 2
    else:
        T = 2.6
    target = ph - pa
    lo, hi = 0.03, 0.97
    for _ in range(50):
        r = (lo + hi) / 2
        g = grid(T * r, T * (1 - r))
        d = prob(g, lambda i, j: i > j) - prob(g, lambda i, j: i < j)
        if d < target:
            lo = r
        else:
            hi = r
    r = (lo + hi) / 2
    return T * r, T * (1 - r)


def _ht_ft(lh, la):
    """Probabilités mi-temps / fin de match (45 % des buts en 1re mi-temps)."""
    g1 = grid(0.45 * lh, 0.45 * la)
    g2 = grid(0.55 * lh, 0.55 * la)
    res = {}
    for i1 in range(7):
        for j1 in range(7):
            p1 = g1[i1][j1]
            if p1 < 1e-6:
                continue
            ht = "1" if i1 > j1 else ("N" if i1 == j1 else "2")
            for i2 in range(7):
                for j2 in range(7):
                    p = p1 * g2[i2][j2]
                    if p < 1e-8:
                        continue
                    fi, fj = i1 + i2, j1 + j2
                    ft = "1" if fi > fj else ("N" if fi == fj else "2")
                    res[(ht, ft)] = res.get((ht, ft), 0) + p
    return res


def derive(lh, la, home, away, has_total25=True):
    """Liste de (cat, marché, sélection, proba, uid_suffixe)."""
    g = grid(lh, la)
    P = lambda c: prob(g, c)
    p1, pn, p2 = P(lambda i, j: i > j), P(lambda i, j: i == j), P(lambda i, j: i < j)
    out = [
        ("dc", "Double chance", f"{home} ou nul (1N)", p1 + pn, "dc1N"),
        ("dc", "Double chance", f"Nul ou {away} (N2)", pn + p2, "dcN2"),
        ("dc", "Double chance", f"{home} ou {away} (12)", p1 + p2, "dc12"),
        ("dnb", "Remboursé si nul", f"{home} (remboursé si nul)", p1 / max(1e-9, p1 + p2), "dnb1"),
        ("dnb", "Remboursé si nul", f"{away} (remboursé si nul)", p2 / max(1e-9, p1 + p2), "dnb2"),
        ("btts", "Les 2 équipes marquent", "Les 2 équipes marquent : Oui", P(lambda i, j: i > 0 and j > 0), "bttsY"),
        ("btts", "Les 2 équipes marquent", "Les 2 équipes marquent : Non", P(lambda i, j: i == 0 or j == 0), "bttsN"),
        ("hcp", "Handicap", f"{home} gagne avec 2 buts d'écart ou plus", P(lambda i, j: i - j >= 2), "h1m2"),
        ("hcp", "Handicap", f"{away} gagne avec 2 buts d'écart ou plus", P(lambda i, j: j - i >= 2), "h2m2"),
        ("hcp", "Handicap", f"{home} ne perd pas de plus d'un but", P(lambda i, j: j - i <= 1), "h1p1"),
        ("hcp", "Handicap", f"{away} ne perd pas de plus d'un but", P(lambda i, j: i - j <= 1), "h2p1"),
    ]
    for line in (0.5, 1.5, 2.5, 3.5, 4.5):
        if line == 2.5 and has_total25:
            continue
        po = P(lambda i, j, L=line: i + j > L)
        lab = f"{line:g}".replace(".", ",")
        out.append(("total", "Nombre de buts", f"Plus de {lab} buts", po, f"o{line}"))
        out.append(("total", "Nombre de buts", f"Moins de {lab} buts", 1 - po, f"u{line}"))
    for team, side in ((home, 0), (away, 1)):
        for line in (0.5, 1.5, 2.5):
            po = P(lambda i, j, L=line, s=side: (i if s == 0 else j) > L)
            lab = f"{line:g}".replace(".", ",")
            out.append(("team", "Buts par équipe", f"{team} : plus de {lab} but{'s' if line > 1 else ''}", po,
                        f"t{side}o{line}"))
            if line <= 1.5:
                out.append(("team", "Buts par équipe", f"{team} : moins de {lab} but{'s' if line > 1 else ''}",
                            1 - po, f"t{side}u{line}"))
    scores = sorted(((g[i][j], i, j) for i in range(6) for j in range(6)), reverse=True)[:5]
    for p, i, j in scores:
        out.append(("exact", "Score exact", f"Score exact {i}-{j}", p, f"s{i}{j}"))
    names = {"1": home, "N": "Nul", "2": away}
    for (ht, ft), p in sorted(_ht_ft(lh, la).items(), key=lambda kv: -kv[1])[:4]:
        out.append(("mtft", "Mi-temps / Fin de match", f"Mi-temps {names[ht]} / Fin {names[ft]}", p, f"m{ht}{ft}"))
    return out


def player_markets(lam_team, scorers, team_goals, team_matches, injured_names=()):
    """Buteurs et joueurs décisifs (but ou passe décisive) d'une équipe."""
    out = []
    inj = {n.lower().split()[-1] for n in injured_names if n}
    for s in scorers[:4]:
        name = s.get("name") or ""
        if not name or name.lower().split()[-1] in inj:
            continue  # blessé / suspendu : pas de pari sur lui
        goals, assists, played = s.get("goals") or 0, s.get("assists") or 0, s.get("played") or 0
        if team_goals <= 0 or goals <= 0:
            continue
        presence = min(1.0, (played / team_matches) if team_matches else 1.0)
        presence = max(presence, 0.6)
        share = min(0.7, goals / team_goals / presence)
        p_goal = 1 - math.exp(-lam_team * share * 0.92)
        share_ga = min(0.85, (goals + 0.8 * assists) / team_goals / presence)
        p_dec = 1 - math.exp(-lam_team * share_ga * 0.92)
        out.append(("scorer", "Buteur", f"{name} marque", p_goal, f"sc_{name}"))
        if assists:
            out.append(("decisive", "Joueur décisif", f"{name} décisif (but ou passe)", p_dec, f"de_{name}"))
    return out


def estimated_odds(cat, p):
    fair = 1 / max(p, 1e-6)
    return max(1.01, round(fair / MARGINS.get(cat, 1.08), 2)), round(fair * 1.02, 2)
