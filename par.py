"""Points Above Replacement (PAR) — the same definition the Trade Network uses
(namkurd/SleeperTradeNodes, par.py), so both tools speak one language.

  PAR (started)   = Σ over weeks the player was STARTED:  points − replacement PPG
                    (a started dud or an injured starter counts against you)
  PAR (rostered)  = Σ over weeks he was ON THE ROSTER and actually played:  points − replacement PPG
                    (bye / injury weeks on the bench are skipped, not penalised)

Replacement level, per season and position (identical to the Trade Network):
  1. Cutoff rank R = average number of that position the league's managers actually STARTED per
     week (measured from real lineups, so flex / superflex count automatically)
     + bench depth × number of teams (QB/TE/K/DEF 0.5 per team, RB/WR 1.0 per team).
  2. Every NFL player at the position is ranked by points per game (this league's scoring,
     completed regular-season weeks, ≥ 40% of those weeks played).
  3. Replacement PPG = the PPG of the player ranked R.

Single source of truth: the tables are read from the Trade Network's published data.json
(`replacement`). Only if that is unreachable, or lacks a season, are they rebuilt here with
the exact same algorithm and constants.
"""
import json
import math
from collections import Counter, defaultdict

BUFFER = {"QB": 0.5, "RB": 1.0, "WR": 1.0, "TE": 0.5, "K": 0.5, "DEF": 0.5}   # bench depth per team
POSITIONS = list(BUFFER)
MIN_GP_FRAC = 0.4
TRADE_NETWORK_DATA = "https://raw.githubusercontent.com/namkurd/SleeperTradeNodes/main/data.json"


def league_points(stats_raw, scoring, players):
    """{pid: league points} for one week from Sleeper's raw stat lines (players who played)."""
    out = {}
    for pid, s in (stats_raw or {}).items():
        if not s or pid.startswith("TEAM_") or pid not in players:
            continue
        if (s.get("gp") or 0) <= 0 and not (s.get("gms_active") or 0):
            continue
        out[pid] = round(sum(v * scoring[k] for k, v in s.items()
                             if k in scoring and isinstance(v, (int, float))), 2)
    return out


def build_table(season_pts, matchups, reg_end, teams, players):
    """Replacement table for one season. season_pts = {week: {pid: pts}}, matchups = {week: rows}."""
    pos_of = lambda pid: (players.get(pid) or {}).get("p")
    weeks, starters = [], Counter()
    pts = defaultdict(dict)
    for w in range(1, reg_end + 1):
        m = matchups.get(w)
        if not m or not any(e.get("points") for e in m) or not season_pts.get(w):
            continue
        weeks.append(w)
        for e in m:
            for s in e.get("starters") or []:
                if s and s != "0":
                    starters[pos_of(s)] += 1
        for pid, p in season_pts[w].items():
            pts[pid][w] = p
    nw = len(weeks)
    if not nw:
        return None
    min_gp = max(1, math.ceil(MIN_GP_FRAC * nw))
    table = {}
    for pos in POSITIONS:
        n_start = starters[pos] / nw
        rank = max(1, int(round(n_start + BUFFER[pos] * teams)))
        cand = sorted(((sum(wk.values()) / len(wk), len(wk), (players.get(pid) or {}).get("n") or pid)
                       for pid, wk in pts.items() if pos_of(pid) == pos and len(wk) >= min_gp),
                      reverse=True)
        if not cand:
            continue
        ppg, gp, who = cand[min(rank, len(cand)) - 1]
        table[pos] = {"st": round(n_start, 1), "buf": round(BUFFER[pos] * teams, 1), "rank": rank,
                      "ppg": round(ppg, 2), "who": who, "gp": gp, "n": len(cand)}
    return {"weeks": nw, "regWeeks": reg_end, "teams": teams, "minGp": min_gp, "pos": table}


def published_tables(session):
    """The Trade Network's replacement tables ({season: table}), or {} if unreachable."""
    try:
        r = session.get(TRADE_NETWORK_DATA, timeout=60)
        r.raise_for_status()
        return r.json().get("replacement") or {}
    except Exception as e:  # noqa: BLE001
        print(f"warning: Trade Network replacement tables unavailable ({e}); rebuilding locally")
        return {}
