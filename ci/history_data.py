# -*- coding: utf-8 -*-
"""Base historique pour le simulateur de stratégies (backtesting).

Source : football-data.co.uk (gratuit) — résultats, tirs et cotes d'avant-match de chaque match
depuis 2020/21, pour une quinzaine de championnats. Téléchargé une fois par semaine par GitHub Actions,
transformé en base compacte avec, pour chaque match, la forme des deux équipes AVANT le coup d'envoi
(aucune information du futur), puis envoyé au serveur Cotescope (KV « bt_data »).

Usage : python ci/history_data.py [dossier_csv_local]
"""
import csv
import gzip
import io
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict, deque
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

LEAGUES = {  # code football-data -> (nom affiché, clé The Odds API si on la suit)
    "F1": ("Ligue 1", "soccer_france_ligue_one"), "F2": ("Ligue 2", "soccer_france_ligue_two"),
    "E0": ("Premier League", "soccer_epl"), "E1": ("Championship", "soccer_efl_champ"),
    "SP1": ("Liga", "soccer_spain_la_liga"), "SP2": ("Liga 2", "soccer_spain_segunda_division"),
    "I1": ("Serie A", "soccer_italy_serie_a"), "I2": ("Serie B", "soccer_italy_serie_b"),
    "D1": ("Bundesliga", "soccer_germany_bundesliga"), "D2": ("2. Bundesliga", "soccer_germany_bundesliga2"),
    "N1": ("Eredivisie", "soccer_netherlands_eredivisie"), "P1": ("Liga Portugal", "soccer_portugal_primeira_liga"),
    "B1": ("Jupiler Pro League", "soccer_belgium_first_div"), "T1": ("Süper Lig", "soccer_turkey_super_league"),
    "G1": ("Super League (Grèce)", "soccer_greece_super_league"), "SC0": ("Premiership (Écosse)", "soccer_spl"),
}
FIRST_SEASON = 2020  # 2020/21


def seasons():
    today = date.today()
    last = today.year if today.month >= 7 else today.year - 1
    return [f"{y % 100:02d}{(y + 1) % 100:02d}" for y in range(FIRST_SEASON, last + 1)]


def fetch(code, season, local=None):
    if local:
        p = os.path.join(local, f"{season}_{code}.csv")
        return open(p, "rb").read() if os.path.exists(p) else None
    url = f"https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Cotescope-backtest/1.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read()
        except Exception as e:  # championnat/saison absent ou réseau : on passe
            if "404" in str(e):
                return None
            time.sleep(2 + attempt * 3)
    return None


def fnum(row, *keys):
    for k in keys:
        v = (row.get(k) or "").strip()
        if v:
            try:
                x = float(v)
                if x > 0:
                    return round(x, 2)
            except ValueError:
                pass
    return None


def parse_date(s):
    s = (s or "").strip()
    try:
        d, m, y = s.split("/")
        y = int(y)
        if y < 100:
            y += 2000
        return date(y, int(m), int(d))
    except (ValueError, AttributeError):
        return None


def read_rows(raw):
    text = raw.decode("utf-8-sig", errors="replace")
    if "�" in text[:2000]:
        text = raw.decode("latin-1")
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        row = {(k or "").strip(): v for k, v in row.items()}
        d = parse_date(row.get("Date"))
        h, a = (row.get("HomeTeam") or "").strip(), (row.get("AwayTeam") or "").strip()
        try:
            hg, ag = int(row.get("FTHG")), int(row.get("FTAG"))
        except (TypeError, ValueError):
            continue
        if not (d and h and a):
            continue
        out.append({
            "d": d, "h": h, "a": a, "hg": hg, "ag": ag,
            "hst": fnum(row, "HST"), "ast": fnum(row, "AST"),
            # cotes d'avant-match : moyenne du marché, meilleure cote, Pinnacle (repli Bet365)
            "avg": [fnum(row, "AvgH", "BbAvH", "B365H"), fnum(row, "AvgD", "BbAvD", "B365D"), fnum(row, "AvgA", "BbAvA", "B365A")],
            "max": [fnum(row, "MaxH", "BbMxH"), fnum(row, "MaxD", "BbMxD"), fnum(row, "MaxA", "BbMxA")],
            "ps": [fnum(row, "PSH"), fnum(row, "PSD"), fnum(row, "PSA")],
            "ou_avg": [fnum(row, "Avg>2.5", "BbAv>2.5", "B365>2.5"), fnum(row, "Avg<2.5", "BbAv<2.5", "B365<2.5")],
            "ou_max": [fnum(row, "Max>2.5", "BbMx>2.5"), fnum(row, "Max<2.5", "BbMx<2.5")],
            "ou_ps": [fnum(row, "P>2.5"), fnum(row, "P<2.5")],
        })
    out.sort(key=lambda r: r["d"])
    return out


class TeamState:
    """Ce que l'on sait d'une équipe avant chacun de ses matchs."""

    def __init__(self):
        self.all = deque(maxlen=5)      # (points, buts pour, contre, tirs cadrés)
        self.home = deque(maxlen=5)
        self.away = deque(maxlen=5)
        self.last_date = None

    @staticmethod
    def agg(q):
        n = len(q)
        if not n:
            return None
        pts = sum(x[0] for x in q)
        gf = sum(x[1] for x in q)
        ga = sum(x[2] for x in q)
        o25 = sum(1 for x in q if x[1] + x[2] > 2)
        btts = sum(1 for x in q if x[1] > 0 and x[2] > 0)
        sots = [x[3] for x in q if x[3] is not None]
        sot = round(10 * sum(sots) / len(sots)) if sots else None
        return n, pts, gf, ga, o25, btts, sot

    def add(self, d, gf, ga, sot, at_home):
        pts = 3 if gf > ga else (1 if gf == ga else 0)
        x = (pts, gf, ga, sot)
        self.all.append(x)
        (self.home if at_home else self.away).append(x)
        self.last_date = d


COLS = ["lg", "season", "day", "h", "a", "hg", "ag",
        # forme sur les 5 derniers matchs (toutes compétitions de la base), puis sur les 5 derniers au même endroit
        "h_n", "h_pts5", "h_gf5", "h_ga5", "h_o255", "h_btts5", "h_sot5", "h_vpts5", "h_vgf5", "h_vga5",
        "a_n", "a_pts5", "a_gf5", "a_ga5", "a_o255", "a_btts5", "a_sot5", "a_vpts5", "a_vgf5", "a_vga5",
        "h_rank", "a_rank", "played", "h_rest", "a_rest",
        "avgH", "avgD", "avgA", "maxH", "maxD", "maxA", "psH", "psD", "psA",
        "avgO", "avgU", "maxO", "maxU", "psO", "psU"]


def snapshot(st, venue_home):
    a = TeamState.agg(st.all)
    v = TeamState.agg(st.home if venue_home else st.away)
    if not a:
        return [0, None, None, None, None, None, None, None, None, None]
    return [a[0], a[1], a[2], a[3], a[4], a[5], a[6],
            v[1] if v else None, v[2] if v else None, v[3] if v else None]


def table_ranks(table):
    order = sorted(table.items(), key=lambda kv: (-kv[1][0], -(kv[1][1] - kv[1][2]), -kv[1][1], kv[0]))
    return {t: i + 1 for i, (t, _) in enumerate(order)}


def build(local=None):
    teams, team_idx = [], {}

    def tid(name):
        if name not in team_idx:
            team_idx[name] = len(teams)
            teams.append(name)
        return team_idx[name]

    rows, now_state, leagues_used = [], {}, []
    epoch = date(2000, 1, 1)
    for code, (lname, skey) in LEAGUES.items():
        state = defaultdict(TeamState)
        got = False
        for season in seasons():
            raw = fetch(code, season, local)
            if not raw:
                continue
            matches = read_rows(raw)
            if not matches:
                continue
            got = True
            table = defaultdict(lambda: [0, 0, 0, 0])  # points, buts pour, contre, joués
            for m in matches:
                hs, as_ = state[m["h"]], state[m["a"]]
                ranks = table_ranks(table) if table else {}
                played = round((sum(v[3] for v in table.values()) / max(1, len(table)))) if table else 0
                rest = lambda s: min(30, (m["d"] - s.last_date).days) if s.last_date else None
                r = [code, int("20" + season[:2]), (m["d"] - epoch).days, tid(m["h"]), tid(m["a"]), m["hg"], m["ag"]]
                r += snapshot(hs, True) + snapshot(as_, False)
                r += [ranks.get(m["h"]), ranks.get(m["a"]), played, rest(hs), rest(as_)]
                r += m["avg"] + m["max"] + m["ps"] + m["ou_avg"] + m["ou_max"] + m["ou_ps"]
                rows.append(r)
                # après le match : on met à jour la forme et le classement
                hs.add(m["d"], m["hg"], m["ag"], m["hst"], True)
                as_.add(m["d"], m["ag"], m["hg"], m["ast"], False)
                for t, gf, ga in ((m["h"], m["hg"], m["ag"]), (m["a"], m["ag"], m["hg"])):
                    tb = table[t]
                    tb[0] += 3 if gf > ga else (1 if gf == ga else 0)
                    tb[1] += gf
                    tb[2] += ga
                    tb[3] += 1
            last_table, last_season = table, season
        if not got:
            continue
        leagues_used.append(code)
        # état actuel de chaque équipe de la saison en cours (pour repérer les matchs du jour)
        if last_season in seasons()[-2:]:
            ranks = table_ranks(last_table)
            played = round(sum(v[3] for v in last_table.values()) / max(1, len(last_table)))
            for t in last_table:
                s = state[t]
                now_state[str(tid(t))] = {"lg": code, "rank": ranks.get(t), "played": played,
                                          "home": snapshot(s, True), "away": snapshot(s, False),
                                          "last": (s.last_date - epoch).days if s.last_date else None}
        print(f"{code} {lname}: ok", flush=True)
    return {"v": 1, "built": int(time.time()), "epoch": "2000-01-01", "cols": COLS, "teams": teams,
            "leagues": {c: {"name": LEAGUES[c][0], "key": LEAGUES[c][1]} for c in leagues_used},
            "rows": rows, "now": now_state}


def main():
    local = sys.argv[1] if len(sys.argv) > 1 else None
    data = build(local)
    print(len(data["rows"]), "matchs,", len(data["teams"]), "équipes,", len(data["leagues"]), "championnats")
    if not data["rows"]:
        print("::warning::aucune donnée historique récupérée")
        return
    out = os.path.join(ROOT, ".collect_cache", "bt_data.json.gz")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    raw = json.dumps(data, separators=(",", ":")).encode()
    with open(out, "wb") as f:
        f.write(gzip.compress(raw))
    print("taille :", len(raw) // 1024, "Ko ->", os.path.getsize(out) // 1024, "Ko compressé")
    if os.environ.get("CF_API_TOKEN"):
        import collector
        collector.cf_put("bt_data", data)


if __name__ == "__main__":
    main()
