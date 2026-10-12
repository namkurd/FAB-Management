#!/usr/bin/env python3
"""
FAB Management - waiver / FAB / free-agent pickup analysis for the DTF Club Sleeper league.

Walks the league chain (2021 -> current season), pulls every waiver and free-agent
transaction, and scores each completed add with the league's OWN scoring:

  * Started points  - points the added player scored while in that manager's starting
                      lineup (Sleeper matchup `starters` / `players_points`). Headline metric.
  * Rostered points - points scored while on that manager's roster (started or benched).
  * Regret          - points the DROPPED player (same transaction) went on to score in
                      OTHER managers' starting lineups for the rest of that regular season.
  * Every number keeps its week-by-week detail (`wk`, `sbl`) so the page can show exactly
    what rolls up into it.

Writes data.json (consumed by index.html). Only COMPLETED weeks are read or cached:
Sleeper pre-generates future-week matchups with stale rosters and zero points.

Run:  python3 update_waivers.py        (requests is the only dependency)

ONE-TIME-PER-YEAR MAINTENANCE: nothing. The script discovers the current league from
LATEST_LEAGUE_ID and walks `previous_league_id` back. When Sleeper creates next season's
league, update LATEST_LEAGUE_ID (and add any new managers to USERNAME_TO_FIRST).
"""
import csv
import io
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import par

HERE = Path(__file__).parent
CACHE_DIR = HERE / "cache"
CACHE_DIR.mkdir(exist_ok=True)
OUTPUT = HERE / "data.json"

LATEST_LEAGUE_ID = "1389416556617801728"   # 2026
FIRST_SEASON = 2021
LAST_FANTASY_WEEK = 17                     # nobody plays week 18
SLEEPER = "https://api.sleeper.app/v1"
GAMES_CSV = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
HIT_THRESHOLD = 25.0                       # started PAR for an add to be a "difference-maker"
# Timing (early / chase), in PAR per game = a player's PPG minus his position's replacement PPG
TIMING_PRE_WEEKS, TIMING_POST_WEEKS = 3, 4   # look-back before the add / look-ahead from first counted week
TIMING_EDGE = 3.0                            # "startable" = at least this many PAR per game
CHASE_SPIKE = 8.0                            # a "spike" = one game at least this far above replacement
TOSS_UP = 2.0      # sit/start: starter projected within this many pts of the bench option = a toss-up

USERNAME_TO_FIRST = {
    "greenbayblay": "Joe", "haanrolo": "Haan", "hellerch": "Christian", "legendaly": "Aidan",
    "ozviagin": "Oleg", "stevster77": "Steven", "ilovelamp917": "Alex", "lalu101": "Ankit",
    "namkurd": "Ben", "rrakower": "Ryan", "thehebrewhammer24": "Jake", "tkitaev": "Tommy",
    "kohogan18": "Kaitlyn", "kohagan18": "Kaitlyn", "slondon1": "Stephanie",
    "rohaan": "Haan",
}

session = requests.Session()
session.headers["User-Agent"] = "FAB-Management/1.0"


# --------------------------------------------------------------------------- HTTP / cache
def sleeper_get(path, retries=4):
    for attempt in range(retries):
        try:
            r = session.get(f"{SLEEPER}{path}", timeout=30)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


def cached(name, fetch, cacheable=True):
    """Return cached JSON if present, else fetch; write to cache only when cacheable."""
    p = CACHE_DIR / name
    if cacheable and p.exists():
        return json.loads(p.read_text())
    data = fetch()
    if cacheable and data:
        p.write_text(json.dumps(data, separators=(",", ":")))
    return data


def load_players():
    p = CACHE_DIR / "players_nfl.json"
    if p.exists() and time.time() - p.stat().st_mtime < 20 * 3600:
        return json.loads(p.read_text())
    print("Fetching /players/nfl (~5 MB, at most once a day)...")
    raw = sleeper_get("/players/nfl") or {}
    slim = {pid: {"n": f"{(x.get('first_name') or '').strip()} {(x.get('last_name') or '').strip()}".strip(),
                  "p": x.get("position") or "", "t": x.get("team") or ""}
            for pid, x in raw.items()}
    p.write_text(json.dumps(slim, separators=(",", ":")))
    return slim


def manager_name(user):
    for key in ("username", "display_name"):
        v = (user.get(key) or "").lower()
        if v in USERNAME_TO_FIRST:
            return USERNAME_TO_FIRST[v]
    return user.get("display_name") or user.get("username") or f"user{user.get('user_id')}"


# --------------------------------------------------------------------------- calendar
_STATE = {}


def nfl_state():
    if not _STATE:
        _STATE.update(sleeper_get("/state/nfl") or {})
    return _STATE


def last_complete_week(season):
    """Highest fantasy week whose games are final. Sleeper's week counter rolls over after
    Monday night, so while state says week N, weeks 1..N-1 are complete."""
    st = nfl_state()
    try:
        cur = int(st.get("season"))
    except (TypeError, ValueError):
        return LAST_FANTASY_WEEK
    if season < cur:
        return LAST_FANTASY_WEEK
    if season > cur or st.get("season_type") == "pre":
        return 0
    if st.get("season_type") == "off":
        return LAST_FANTASY_WEEK
    return min(LAST_FANTASY_WEEK, max(0, int(st.get("week") or 0) - 1))


def load_week_kickoffs():
    """{(season, week): first kickoff of that NFL week as UTC epoch ms} from nflverse games.csv."""
    p = CACHE_DIR / "games.csv"
    if not p.exists() or time.time() - p.stat().st_mtime > 7 * 86400:
        try:
            r = session.get(GAMES_CSV, timeout=60)
            r.raise_for_status()
            p.write_text(r.text)
        except requests.RequestException as e:
            print(f"warning: games.csv unavailable ({e})")
            if not p.exists():
                return {}
    et = ZoneInfo("America/New_York")
    out = {}
    for row in csv.DictReader(io.StringIO(p.read_text())):
        if row["game_type"] != "REG":
            continue
        s, w = int(row["season"]), int(row["week"])
        if s < FIRST_SEASON:
            continue
        t = row.get("gametime") or "13:00"
        dt = datetime.strptime(f"{row['gameday']} {t}", "%Y-%m-%d %H:%M").replace(tzinfo=et)
        ms = int(dt.astimezone(timezone.utc).timestamp() * 1000)
        out[(s, w)] = min(out.get((s, w), ms), ms)
    return out


# --------------------------------------------------------------------------- per-season data
def league_chain():
    chain, lid = [], LATEST_LEAGUE_ID
    while lid and lid != "0":
        lg = cached(f"league_{lid}.json", lambda: sleeper_get(f"/league/{lid}"),
                    cacheable=False)
        if not lg:
            break
        season = int(lg["season"])
        if season < FIRST_SEASON:
            break
        chain.append(lg)
        lid = lg.get("previous_league_id")
    return sorted(chain, key=lambda l: int(l["season"]))


def season_bundle(lg, players):
    season = int(lg["season"])
    lid = lg["league_id"]
    done = last_complete_week(season)
    finished = done >= LAST_FANTASY_WEEK
    settings = lg.get("settings") or {}
    pws = settings.get("playoff_week_start") or 0
    reg_end = pws - 1 if pws else (14 if season >= 2026 else 15)

    users = cached(f"users_{lid}.json", lambda: sleeper_get(f"/league/{lid}/users"), finished)
    rosters = cached(f"rosters_{lid}.json", lambda: sleeper_get(f"/league/{lid}/rosters"), finished)

    txns = []
    for wk in range(1, 19):
        # a week's transactions are settled once that week's games are final
        t = cached(f"txns_{lid}_wk{wk}.json",
                   lambda wk=wk: sleeper_get(f"/league/{lid}/transactions/{wk}") or [],
                   cacheable=wk <= done)
        for x in t or []:
            x["_wk"] = wk
        txns.extend(t or [])
        time.sleep(0.05)

    matchups = {}
    for wk in range(1, done + 1):
        matchups[wk] = cached(f"matchups_{lid}_wk{wk}.json",
                              lambda wk=wk: sleeper_get(f"/league/{lid}/matchups/{wk}") or [])
        time.sleep(0.05)

    # league-scoring points for EVERY NFL player each completed week (Sleeper stat lines x this
    # season's scoring_settings - matches Sleeper's own players_points exactly). Used for the
    # replacement baseline, rostered-PAR "did he play" checks and early/chase timing.
    scoring = lg.get("scoring_settings") or {}
    pts = {}
    for wk in range(1, min(done, LAST_FANTASY_WEEK) + 1):
        pts[wk] = cached(f"lpts_{season}_wk{wk}.json",
                         lambda wk=wk: par.league_points(sleeper_get(f"/stats/nfl/regular/{season}/{wk}"),
                                                         scoring, players))
        time.sleep(0.05)

    # Sleeper's pre-game projections, scored with the same settings (what a manager could know at lock)
    proj = {}
    for wk in range(1, min(done, reg_end) + 1):
        proj[wk] = cached(f"lproj_{season}_wk{wk}.json",
                          lambda wk=wk: {pid: round(sum(v * scoring.get(k, 0) for k, v in st.items()
                                                        if isinstance(v, (int, float))), 2)
                                         for pid, st in (sleeper_get(f"/projections/nfl/regular/{season}/{wk}") or {}).items()
                                         if st and pid in players})
        time.sleep(0.05)

    return dict(season=season, league_id=lid, done=done, finished=finished, reg_end=reg_end, proj=proj,
                budget=settings.get("waiver_budget") or 0, users=users or [],
                rosters=rosters or [], txns=txns, matchups=matchups, pts=pts,
                slots=[x for x in (lg.get("roster_positions") or []) if x not in ("BN", "IR", "TAXI")],
                teams=lg.get("total_rosters") or len(rosters or []), playoff_start=reg_end + 1)


# --------------------------------------------------------------------------- analysis
def txn_time(t):
    if t.get("type") == "waiver":
        return t.get("status_updated") or t.get("created") or 0
    return t.get("created") or t.get("status_updated") or 0


def analyze_season(b, players, uid_name, kickoffs, prev_rid_name, repl):
    season, done, reg_end = b["season"], b["done"], b["reg_end"]
    week_pts = b["pts"]                                   # {week: {pid: league pts}} for players who played
    rppg = {pos: row["ppg"] for pos, row in ((repl or {}).get("pos") or {}).items()}

    def ppg_over(pid, weeks):
        vals = [week_pts[w][pid] for w in weeks if w in week_pts and pid in week_pts[w]]
        return (sum(vals) / len(vals) if vals else None), len(vals)
    rid_name = {}
    for r in b["rosters"]:
        name = uid_name.get(r.get("owner_id"))
        if not name:
            # owner left the league afterwards (e.g. 2023 roster 9 = Tommy): Sleeper blanks
            # owner_id, so inherit whoever held this roster slot the season before
            name = prev_rid_name.get(r["roster_id"], f"Team {r['roster_id']}")
            print(f"  note: {season} roster {r['roster_id']} has no owner - using {name}")
        rid_name[r["roster_id"]] = name

    # matchup index: wk -> rid -> (players set, starters set, points dict)
    mx = {}
    for wk, rows in b["matchups"].items():
        mx[wk] = {e["roster_id"]: (set(e.get("players") or []), set(e.get("starters") or []),
                                   e.get("players_points") or {}) for e in rows}

    def label(pid):
        p = players.get(str(pid))
        if not p:
            return {"n": f"Player {pid}", "p": "?", "t": ""}
        return {"n": p["n"] or str(pid), "p": p["p"] or ("DEF" if not str(pid).isdigit() else "?"),
                "t": p["t"]}

    txns = sorted((t for t in b["txns"] if t.get("type") in ("waiver", "free_agent", "trade", "commissioner")),
                  key=txn_time)

    # departures: (rid, pid) -> sorted [(ts, wk, type)]
    departures = {}
    for t in txns:
        if t.get("status") != "complete":
            continue
        for pid, rid in (t.get("drops") or {}).items():
            departures.setdefault((rid, str(pid)), []).append((txn_time(t), t["_wk"], t["type"]))

    def started_by_others(pid, rid, from_wk):
        """Regular-season weeks the dropped player spent in ANOTHER manager's starting
        lineup: [[week, points, manager], ...]."""
        out = []
        for wk in range(from_wk, min(done, reg_end) + 1):
            for r2, (pl, st, pts) in mx.get(wk, {}).items():
                if r2 != rid and pid in st:
                    out.append([wk, round(pts.get(pid) or 0, 2), rid_name.get(r2, f"Team {r2}")])
        return out

    adds, drops = [], []
    for t in txns:
        if t["type"] in ("trade", "commissioner"):
            continue
        rid = (t.get("roster_ids") or [None])[0]
        mgr = rid_name.get(rid, f"Team {rid}")
        wk = t["_wk"]
        if wk > LAST_FANTASY_WEEK:
            continue
        ts = txn_time(t)
        bid = (t.get("settings") or {}).get("waiver_bid") or 0
        typ = "W" if t["type"] == "waiver" else "F"

        if t.get("status") != "complete":
            continue

        my_drops = [str(p) for p, r in (t.get("drops") or {}).items() if r == rid]
        drop_recs = []
        for pid in my_drops:
            sbl = started_by_others(pid, rid, wk + 1)
            lb = label(pid)
            rp = rppg.get(lb["p"])
            rec = {"s": season, "w": wk, "m": mgr, "pid": pid, "pl": lb["n"], "pos": lb["p"],
                   "tm": lb["t"], "ty": typ, "rp": rp, "sbo": round(sum(x[1] for x in sbl), 2),
                   "sbp": round(sum(x[1] - rp for x in sbl), 1) if rp is not None else None, "sbl": sbl}
            drops.append(rec)
            drop_recs.append(rec)

        add_pids = [str(p) for p, r in (t.get("adds") or {}).items() if r == rid]
        for i, pid in enumerate(add_pids):
            lb = label(pid)
            # departure that ends this stint: first drop of (rid,pid) after this add
            dep = next(((dts, dwk, dty) for dts, dwk, dty in departures.get((rid, pid), [])
                        if dts > ts), None)
            end_wk = min(dep[1] if dep else LAST_FANTASY_WEEK, done)
            kick = kickoffs.get((season, wk))
            reg_st = po_st = ros = 0.0
            wst = wbn = 0
            weeks = []                       # [week, points, started 1/0, played 1/0] per counted week
            appeared = False
            for w in range(wk, end_wk + 1):
                row = mx.get(w, {}).get(rid)
                if row is None:              # e.g. eliminated from playoffs - no matchup row
                    if w > reg_end:
                        break
                    continue
                pl, st, pts = row
                if pid not in pl:
                    if appeared or w > wk + 1:
                        break
                    continue
                appeared = True
                p = pts.get(pid) or 0.0
                is_started = pid in st
                if w == wk and not is_started and kick and ts >= kick:
                    continue                 # added after the week began and not started
                weeks.append([w, round(p, 2), 1 if is_started else 0, 1 if pid in week_pts.get(w, {}) else 0])
                if is_started:
                    wst += 1
                    if w <= reg_end:
                        reg_st += p
                    else:
                        po_st += p
                else:
                    wbn += 1
                if w <= reg_end:
                    ros += p
            if dep:
                how = "traded" if dep[2] == "trade" else "dropped"
            elif b["finished"] or done >= reg_end:
                how = "kept"
            else:
                how = "active"
            # points above replacement (same definition as the Trade Network - see par.py)
            rp = rppg.get(lb["p"])
            reg = [x for x in weeks if x[0] <= reg_end]
            par_st = sum(x[1] - rp for x in reg if x[2]) if rp is not None else None
            par_ro = sum(x[1] - rp for x in reg if x[3]) if rp is not None else None
            # timing: PAR per game in the weeks just before the first counted week vs. right after
            first = weeks[0][0] if weeks else wk + 1
            last = min(done, reg_end)
            pre, pre_n = ppg_over(pid, range(max(1, first - TIMING_PRE_WEEKS), first))
            post, post_n = ppg_over(pid, range(first, min(first + TIMING_POST_WEEKS, last + 1)))
            pre_g = round(pre - rp, 1) if pre is not None and rp is not None else None
            post_g = round(post - rp, 1) if post is not None and rp is not None else None
            # chase inputs: the single game right before the pickup vs. the weeks before that
            spike = week_pts.get(first - 1, {}).get(pid) if first > 1 else None
            spk = round(spike - rp, 1) if spike is not None and rp is not None else None
            base, base_n = ppg_over(pid, range(max(1, first - 1 - TIMING_PRE_WEEKS), first - 1))
            base_g = round(base - rp, 1) if base is not None and rp is not None else None
            tim = None
            if post_g is not None and post_n >= 2:
                held = sum(1 for x in weeks if first <= x[0] < first + TIMING_POST_WEEKS)
                if pre_g is not None and post_g >= TIMING_EDGE and pre_g <= 0 and held >= 2:
                    tim = "early"                # picked up while replacement-level, held, then startable
                elif (spk is not None and spk >= CHASE_SPIKE and (base_g is None or base_g <= 0)
                      and post_g <= 0):
                    tim = "chase"                # one spike game right before the pickup, nothing
                                                 # sustained before it, and nothing after
            rec = {"s": season, "w": wk, "m": mgr, "ty": typ, "pid": pid, "pl": lb["n"],
                   "pos": lb["p"], "tm": lb["t"], "b": bid, "ts": ts,
                   "st": round(reg_st, 2), "po": round(po_st, 2), "ro": round(ros, 2),
                   "rp": rp, "par": round(par_st, 1) if par_st is not None else None,
                   "parR": round(par_ro, 1) if par_ro is not None else None,
                   "pre": pre_g, "post": post_g, "pren": pre_n, "postn": post_n, "tim": tim,
                   "spk": spk, "spkw": first - 1 if spk is not None else None, "base": base_g, "basen": base_n,
                   "ws": wst, "wb": wbn, "end": how, "wk": weeks}
            if i == 0 and drop_recs:
                rec["dr"] = [{"pid": d["pid"], "pl": d["pl"], "pos": d["pos"],
                              "sbo": d["sbo"], "sbp": d["sbp"], "rp": d["rp"], "sbl": d["sbl"]} for d in drop_recs]
            adds.append(rec)

    # FAB integrity: sum of winning bids vs roster waiver_budget_used
    bids = {}
    for a in adds:
        if a["ty"] == "W":
            bids[a["m"]] = bids.get(a["m"], 0) + a["b"]
    # FAB moved in trades: waiver_budget [{sender, receiver, amount}]
    traded = {}
    for t in txns:
        if t.get("status") == "complete":
            for wb in t.get("waiver_budget") or []:
                s_, r_, amt = rid_name.get(wb.get("sender")), rid_name.get(wb.get("receiver")), wb.get("amount", 0)
                traded[s_] = traded.get(s_, 0) - amt
                traded[r_] = traded.get(r_, 0) + amt
    teams = []
    for r in b["rosters"]:
        n = rid_name[r["roster_id"]]
        s = r.get("settings") or {}
        teams.append({"m": n, "rid": r["roster_id"], "used": s.get("waiver_budget_used", 0),
                      "bids": bids.get(n, 0), "faab_traded": traded.get(n, 0),
                      "faab_ok": bids.get(n, 0) - traded.get(n, 0) == s.get("waiver_budget_used", 0),
                      "wins": s.get("wins", 0), "losses": s.get("losses", 0),
                      "fpts": round((s.get("fpts") or 0) + (s.get("fpts_decimal") or 0) / 100, 2)})

    meta = {"rid_name": rid_name, "s": season, "league_id": b["league_id"], "done": done, "finished": b["finished"],
            "reg_end": reg_end, "budget": b["budget"], "teams": teams, "repl": repl}
    return meta, adds, drops


GROUPS = {"flex": {"RB", "WR", "TE"}, "stream": {"QB", "K", "DEF"}}


def summarize(seasons, adds, drops):
    """Per manager x season x position group: the FAB component of the future yearly report card.
    {season: {manager: {group: {...}}}} with started PAR, regret PAR, net, FAB spent, early/chase."""
    out = {}
    for meta in seasons:
        s = meta["s"]
        weeks = min(meta["done"], meta["reg_end"])
        for t in meta["teams"]:
            m = t["m"]
            for g in ("flex", "stream", "all"):
                ok = (lambda pos: True) if g == "all" else (lambda pos, g=g: pos in GROUPS[g])
                A = [a for a in adds if a["s"] == s and a["m"] == m and ok(a["pos"])]
                D = [d for d in drops if d["s"] == s and d["m"] == m and ok(d["pos"])]
                par_ = round(sum(a["par"] or 0 for a in A), 1)
                reg = round(sum(max(0, d["sbp"] or 0) for d in D), 1)
                out.setdefault(str(s), {}).setdefault(m, {})[g] = {
                    "par": par_, "parWk": round(par_ / weeks, 2) if weeks else None,
                    "regret": reg, "net": round(par_ - reg, 1),
                    "fab": sum(a["b"] for a in A if a["ty"] == "W"),
                    "hits": sum(1 for a in A if (a["par"] or 0) >= HIT_THRESHOLD),
                    "early": sum(1 for a in A if a["tim"] == "early"),
                    "chase": sum(1 for a in A if a["tim"] == "chase"),
                    "adds": len(A), "weeks": weeks}
    return out


# --------------------------------------------------------------------------- sit / start
SLOT_ELIGIBLE = {
    "QB": {"QB"}, "RB": {"RB"}, "WR": {"WR"}, "TE": {"TE"}, "K": {"K"}, "DEF": {"DEF"},
    "FLEX": {"RB", "WR", "TE"}, "REC_FLEX": {"WR", "TE"}, "WRRB_FLEX": {"WR", "RB"},
    "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
}


def lineup_decisions(b, repl, rid_name, players):
    """Sit/start mistakes that don't punish depth: a starter who scored BELOW replacement while a
    bench player eligible for that same slot scored ABOVE replacement (and more than the starter).
    Each bench player can fix only one slot per week (greedy: biggest gap first).
    Returns a list of [{s, w, m, slot, out:{pid,pl,pos,pts,rp,played}, inn:{...}, lost,
    my, opp, oppm, flipped}] where `flipped` = the swap alone would have turned a loss into a win."""
    season, reg_end, done = b["season"], b["reg_end"], b["done"]
    rp = {pos: row["ppg"] for pos, row in ((repl or {}).get("pos") or {}).items()}
    pos_of = lambda pid: (players.get(pid) or {}).get("p") or ("DEF" if not pid.isdigit() else "?")
    name_of = lambda pid: (players.get(pid) or {}).get("n") or pid
    def tier(proj_start, proj_bench):
        """How the start looked pre-game. gap = bench projection - starter projection.
        'against' = the manager started the lower-projected player by more than TOSS_UP;
        'flip'    = projections within TOSS_UP of each other;
        None      = the starter was projected clearly higher (the expected call)."""
        if proj_bench is None:
            return None
        gap = proj_bench - (proj_start or 0.0)   # no projection for the starter ~ not expected to play
        if gap > TOSS_UP:
            return "against"
        if abs(gap) <= TOSS_UP:
            return "flip"
        return None

    out, calls = [], []
    for w in range(1, min(done, reg_end) + 1):
        rows = b["matchups"].get(w) or []
        played = b["pts"].get(w, {})
        pj = b["proj"].get(w, {})
        by_mid = {}
        for e in rows:
            by_mid.setdefault(e.get("matchup_id"), []).append(e)
        for e in rows:
            rid = e["roster_id"]
            starters = e.get("starters") or []
            pts = e.get("players_points") or {}
            bench = [p for p in (e.get("players") or []) if p not in starters]
            opp = next((o for o in by_mid.get(e.get("matchup_id"), []) if o["roster_id"] != rid), None) \
                if e.get("matchup_id") is not None else None
            bad = []
            for i, pid in enumerate(starters):
                if not pid or pid == "0" or i >= len(b["slots"]):
                    continue
                pos = pos_of(pid)
                if pos not in rp:
                    continue
                score = pts.get(pid) or 0.0
                if score < rp[pos]:
                    bad.append((i, pid, pos, score))
            used = set()
            # fix the worst holes first
            for i, pid, pos, score in sorted(bad, key=lambda x: x[3] - rp[x[2]]):
                slot = b["slots"][i]
                elig = SLOT_ELIGIBLE.get(slot, {slot})
                # only bench options a manager could have justified pre-game (clear or toss-up)
                cands = [(pts.get(q) or 0.0, q, tier(pj.get(pid), pj.get(q))) for q in bench
                         if q not in used and pos_of(q) in elig and pos_of(q) in rp
                         and (pts.get(q) or 0.0) > rp[pos_of(q)] and (pts.get(q) or 0.0) > score]
                cands = [c for c in cands if c[2]]
                if not cands:
                    continue
                best_pts, q, tr = max(cands, key=lambda c: (c[2] == "against", c[0]))
                tr = "clear" if tr == "against" else tr     # going against projections and losing
                used.add(q)
                out.append({"s": season, "w": w, "m": rid_name.get(rid, f"Team {rid}"), "slot": slot,
                            "out": {"pid": pid, "pl": name_of(pid), "pos": pos, "pts": round(score, 2),
                                    "rp": rp[pos], "played": 1 if pid in played else 0, "proj": pj.get(pid)},
                            "inn": {"pid": q, "pl": name_of(q), "pos": pos_of(q), "pts": round(best_pts, 2),
                                    "rp": rp[pos_of(q)], "proj": pj.get(q)},
                            "tier": tr,
                            "lost": round(best_pts - score, 2),
                            "my": round(e.get("points") or 0, 2),
                            "opp": round(opp.get("points") or 0, 2) if opp else None,
                            "oppm": rid_name.get(opp["roster_id"]) if opp else None})

            # the mirror image - calls that paid off: a starter who finished ABOVE replacement while
            # the bench option for that slot finished BELOW it. 'brain' = the starter was projected
            # lower by more than TOSS_UP (big-brain start); 'flip' = projections within TOSS_UP
            # (a won toss-up). The most highly projected eligible bench option is the comparison.
            used_w = set()
            good = []
            for i, pid in enumerate(starters):
                if not pid or pid == "0" or i >= len(b["slots"]):
                    continue
                pos = pos_of(pid)
                score = pts.get(pid) or 0.0
                if pos in rp and score >= rp[pos]:
                    good.append((i, pid, pos, score))
            for i, pid, pos, score in sorted(good, key=lambda x: -(pj.get(x[1]) or 0)):
                slot = b["slots"][i]
                elig = SLOT_ELIGIBLE.get(slot, {slot})
                cands = [(pj.get(q), q, tier(pj.get(pid), pj.get(q))) for q in bench
                         if q not in used_w and pos_of(q) in elig and pos_of(q) in rp
                         and (pts.get(q) or 0.0) < rp[pos_of(q)] and (pts.get(q) or 0.0) < score]
                cands = [c for c in cands if c[2]]
                if not cands:
                    continue
                prj, q, tr = max(cands, key=lambda c: (c[2] == "against", c[0]))
                used_w.add(q)
                calls.append({"s": season, "w": w, "m": rid_name.get(rid, f"Team {rid}"), "slot": slot,
                              "tier": "brain" if tr == "against" else "flip",
                              "out": {"pid": pid, "pl": name_of(pid), "pos": pos, "pts": round(score, 2),
                                      "rp": rp[pos], "proj": pj.get(pid)},          # the player started
                              "inn": {"pid": q, "pl": name_of(q), "pos": pos_of(q),
                                      "pts": round(pts.get(q) or 0.0, 2), "rp": rp[pos_of(q)], "proj": prj},
                              "gain": round(score - (pts.get(q) or 0.0), 2),
                              "my": round(e.get("points") or 0, 2),
                              "opp": round(opp.get("points") or 0, 2) if opp else None,
                              "oppm": rid_name.get(opp["roster_id"]) if opp else None})
    # did the week's swaps (together) flip a loss into a win?
    wk_lost = {}
    for d in out:
        wk_lost[(d["m"], d["w"])] = wk_lost.get((d["m"], d["w"]), 0) + d["lost"]
    for d in out:
        tot = wk_lost[(d["m"], d["w"])]
        d["flipped"] = 1 if d["opp"] is not None and d["my"] < d["opp"] <= d["my"] + tot else 0
        d["wkLost"] = round(tot, 2)
    return out, calls


RUMBLE_BONUS = 9      # league standings: 9 for a head-to-head win + 1 per team outscored that week


def rumbles(week_scores, mgr, my_pts=None):
    me = next(x for x in week_scores if x[0] == mgr)
    p = me[1] if my_pts is None else my_pts
    opp = next((x for x in week_scores if x[0] != mgr and x[2] is not None and x[2] == me[2]), None)
    won = opp is not None and p > opp[1]
    return sum(1 for x in week_scores if x[0] != mgr and p > x[1]) + (RUMBLE_BONUS if won else 0)


def lost_flip(week_scores, mgr, pts_left):
    """True if the manager lost the matchup and pts_left would have won it."""
    me = next(x for x in week_scores if x[0] == mgr)
    opp = next((x for x in week_scores if x[0] != mgr and x[2] is not None and x[2] == me[2]), None)
    return opp is not None and me[1] < opp[1] < me[1] + pts_left


def rumbles_lost(week_scores, mgr, pts_left):
    """Rumbles the week's lineup misses cost: re-score the manager with the right calls, everyone
    else's score unchanged."""
    me = next(x for x in week_scores if x[0] == mgr)
    return rumbles(week_scores, mgr, me[1] + pts_left) - rumbles(week_scores, mgr)


def main():
    players = load_players()
    kickoffs = load_week_kickoffs()
    chain = league_chain()
    bundles = []
    for lg in chain:
        print(f"Season {lg['season']} ({lg['league_id']})...")
        bundles.append(season_bundle(lg, players))

    uid_name = {}
    for b in bundles:
        for u in b["users"]:
            uid_name[u["user_id"]] = manager_name(u)

    published = par.published_tables(session)
    seasons, adds, drops, lineup, calls, scores = [], [], [], [], [], {}
    prev_rid_name = {}
    for b in bundles:
        repl = published.get(str(b["season"]))
        src = "trade-network"
        if not repl or (not b["finished"] and repl.get("weeks", 0) < min(b["done"], b["reg_end"])):
            local = par.build_table(b["pts"], b["matchups"], b["reg_end"], b["teams"], players)
            if local and (not repl or local["weeks"] > repl.get("weeks", 0)):
                repl, src = local, "local"
        if repl:
            repl = {**repl, "src": src}
            print(f"  {b['season']} replacement ({src}, {repl['weeks']} wks): " +
                  ", ".join(f"{p} {r['rank']}@{r['ppg']}" for p, r in repl["pos"].items()))
        meta, a, d = analyze_season(b, players, uid_name, kickoffs, prev_rid_name, repl)
        prev_rid_name = meta.pop("rid_name")
        miss, good = lineup_decisions(b, repl, prev_rid_name, players)
        lineup += miss
        calls += good
        # every team's regular-season score each completed week (for Rumbles: 9 for a head-to-head
        # win + 1 per team outscored, the league's standings system)
        scores[str(b["season"])] = {
            str(w): [[prev_rid_name.get(e["roster_id"], f"Team {e['roster_id']}"), round(e.get("points") or 0, 2),
                      e.get("matchup_id")] for e in (b["matchups"].get(w) or [])]
            for w in range(1, min(b["done"], b["reg_end"]) + 1) if b["matchups"].get(w)}
        seasons.append(meta)
        adds += a
        drops += d
        bad = [t for t in meta["teams"] if not t["faab_ok"]]
        print(f"  {meta['s']}: {len(a)} adds, {len(d)} drops, "
              f"through wk {meta['done']}; FAB mismatches: "
              + (", ".join(f"{t['m']} bids={t['bids']} traded={t['faab_traded']} used={t['used']}" for t in bad) or "none"))

    st = nfl_state()
    out_summary = summarize(seasons, adds, drops)
    for meta in seasons:                                   # lineup component of the report card
        for t in meta["teams"]:
            weeks = min(meta["done"], meta["reg_end"])
            for key, tiers in (("lineup", {"clear"}), ("lineupWithFlips", {"clear", "flip"})):
                L = [d for d in lineup if d["s"] == meta["s"] and d["m"] == t["m"] and d["tier"] in tiers]
                lost_by_wk = {}
                for d in L:
                    lost_by_wk[d["w"]] = lost_by_wk.get(d["w"], 0) + d["lost"]
                ws = scores[str(meta["s"])]
                out_summary[str(meta["s"])][t["m"]][key] = {
                    "mistakes": len(L), "lost": round(sum(lost_by_wk.values()), 1),
                    "lostWk": round(sum(lost_by_wk.values()) / weeks, 2) if weeks else None,
                    "winsLost": sum(1 for w, x in lost_by_wk.items() if lost_flip(ws[str(w)], t["m"], x)),
                    "rumblesLost": sum(rumbles_lost(ws[str(w)], t["m"], x) for w, x in lost_by_wk.items()),
                    "weeks": weeks}
            C = [c for c in calls if c["s"] == meta["s"] and c["m"] == t["m"]]
            F = [d for d in lineup if d["s"] == meta["s"] and d["m"] == t["m"] and d["tier"] == "flip"]
            out_summary[str(meta["s"])][t["m"]]["calls"] = {
                "bigBrain": sum(1 for c in C if c["tier"] == "brain"),
                "bigBrainPts": round(sum(c["gain"] for c in C if c["tier"] == "brain"), 1),
                "flipsWon": sum(1 for c in C if c["tier"] == "flip"), "flipsLost": len(F)}
    out = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nfl": {"season": st.get("season"), "week": st.get("week")},
        "hit_threshold": HIT_THRESHOLD, "timing_edge": TIMING_EDGE, "chase_spike": CHASE_SPIKE,
        "managers": sorted({t["m"] for s in seasons for t in s["teams"]}),
        "seasons": seasons, "summary": out_summary, "adds": adds, "drops": drops,
    }
    out["lineup"] = lineup
    out["calls"] = calls
    out["scores"] = scores
    out["rumble_bonus"] = RUMBLE_BONUS
    OUTPUT.write_text(json.dumps(out, separators=(",", ":")))
    print(f"Wrote {OUTPUT.name}: {OUTPUT.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
