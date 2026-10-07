#!/usr/bin/env python3
"""
FAB Management - waiver / FAAB / free-agent pickup analysis for the DTF Club Sleeper league.

Walks the league chain (2021 -> current season), pulls every waiver and free-agent
transaction, and scores each completed add with the league's OWN scoring:

  * Started points  - points the added player scored while in that manager's starting
                      lineup (Sleeper matchup `starters` / `players_points`). Headline metric.
  * Rostered points - points scored while on that manager's roster (started or benched).
  * Regret          - points the DROPPED player went on to score afterwards: started for
                      other managers (matchups) and total league-scoring points (stats).
  * Timing          - league-scoring points per game in the weeks before vs after the add,
                      computed for every NFL player from Sleeper's weekly stats endpoint and
                      the league's scoring_settings (verified to match players_points exactly).

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

HERE = Path(__file__).parent
CACHE_DIR = HERE / "cache"
CACHE_DIR.mkdir(exist_ok=True)
OUTPUT = HERE / "data.json"

LATEST_LEAGUE_ID = "1389416556617801728"   # 2026
FIRST_SEASON = 2021
LAST_FANTASY_WEEK = 17                     # nobody plays week 18
SLEEPER = "https://api.sleeper.app/v1"
GAMES_CSV = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
HIT_THRESHOLD = 30.0                       # started points for an add to count as a "hit"

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


def season_bundle(lg):
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

    scoring = lg.get("scoring_settings") or {}
    stats_pts = {}
    for wk in range(1, done + 1):
        def fetch_pts(wk=wk):
            raw = sleeper_get(f"/stats/nfl/regular/{season}/{wk}") or {}
            out = {}
            for pid, s in raw.items():
                if not s or not (s.get("gp") or s.get("gms_active")):
                    continue
                out[pid] = round(sum(v * scoring.get(k, 0) for k, v in s.items()
                                     if isinstance(v, (int, float))), 2)
            return out
        stats_pts[wk] = cached(f"pts_{season}_wk{wk}.json", fetch_pts)
        time.sleep(0.05)

    return dict(season=season, league_id=lid, done=done, finished=finished, reg_end=reg_end,
                budget=settings.get("waiver_budget") or 0, users=users or [],
                rosters=rosters or [], txns=txns, matchups=matchups, stats=stats_pts,
                playoff_start=reg_end + 1)


# --------------------------------------------------------------------------- analysis
FAIL_CATS = [("too many players", "roster"), ("claimed by another", "outbid"),
             ("budget", "budget"), ("no longer", "unavailable"), ("already", "unavailable")]


def fail_category(note):
    n = (note or "").lower()
    for needle, cat in FAIL_CATS:
        if needle in n:
            return cat
    return "other"


def txn_time(t):
    if t.get("type") == "waiver":
        return t.get("status_updated") or t.get("created") or 0
    return t.get("created") or t.get("status_updated") or 0


def analyze_season(b, players, uid_name, kickoffs, prev_rid_name):
    season, done, reg_end = b["season"], b["done"], b["reg_end"]
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
        tot, wks = 0.0, 0
        for wk in range(from_wk, min(done, reg_end) + 1):
            for r2, (pl, st, pts) in mx.get(wk, {}).items():
                if r2 != rid and pid in st:
                    tot += pts.get(pid) or 0
                    wks += 1
        return round(tot, 2), wks

    def stat_points(pid, weeks):
        vals = [b["stats"].get(w, {}).get(pid) for w in weeks if 1 <= w <= done]
        vals = [v for v in vals if v is not None]
        return vals

    adds, drops, fails = [], [], []
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
            if t.get("status") == "failed":
                for pid in (t.get("adds") or {}):
                    fails.append({"s": season, "w": wk, "m": mgr, "pid": str(pid),
                                  "pl": label(pid)["n"], "b": bid,
                                  "c": fail_category((t.get("metadata") or {}).get("notes"))})
            continue

        my_drops = [str(p) for p, r in (t.get("drops") or {}).items() if r == rid]
        drop_recs = []
        for pid in my_drops:
            after = range(wk + 1, reg_end + 1)
            sbo, sbo_w = started_by_others(pid, rid, wk + 1)
            vals = stat_points(pid, after)
            lb = label(pid)
            rec = {"s": season, "w": wk, "m": mgr, "pid": pid, "pl": lb["n"], "pos": lb["p"],
                   "tm": lb["t"], "ty": typ, "sbo": sbo, "sbow": sbo_w,
                   "aft": round(sum(vals), 2), "aftg": len(vals)}
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
            first = None
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
                first = first or w
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
            # timing: league-scoring PPG in up to 3 weeks before the add vs first 4 weeks after
            pre = stat_points(pid, range(wk - 3, wk))
            post_start = first or (wk + 1)
            post = stat_points(pid, range(post_start, post_start + 4))
            rec = {"s": season, "w": wk, "m": mgr, "ty": typ, "pid": pid, "pl": lb["n"],
                   "pos": lb["p"], "tm": lb["t"], "b": bid, "ts": ts,
                   "st": round(reg_st, 2), "po": round(po_st, 2), "ro": round(ros, 2),
                   "ws": wst, "wb": wbn, "end": how,
                   "pre": round(sum(pre) / len(pre), 1) if pre else None,
                   "post": round(sum(post) / len(post), 1) if post else None,
                   "preg": len(pre), "postg": len(post)}
            if i == 0 and drop_recs:
                rec["dr"] = [{"pid": d["pid"], "pl": d["pl"], "pos": d["pos"],
                              "sbo": d["sbo"], "aft": d["aft"]} for d in drop_recs]
            adds.append(rec)

    # FAAB integrity: sum of winning bids vs roster waiver_budget_used
    bids = {}
    for a in adds:
        if a["ty"] == "W":
            bids[a["m"]] = bids.get(a["m"], 0) + a["b"]
    # FAAB moved in trades: waiver_budget [{sender, receiver, amount}]
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
            "reg_end": reg_end, "budget": b["budget"], "teams": teams}
    return meta, adds, drops, fails


def main():
    players = load_players()
    kickoffs = load_week_kickoffs()
    chain = league_chain()
    bundles = []
    for lg in chain:
        print(f"Season {lg['season']} ({lg['league_id']})...")
        bundles.append(season_bundle(lg))

    uid_name = {}
    for b in bundles:
        for u in b["users"]:
            uid_name[u["user_id"]] = manager_name(u)

    seasons, adds, drops, fails = [], [], [], []
    prev_rid_name = {}
    for b in bundles:
        meta, a, d, f = analyze_season(b, players, uid_name, kickoffs, prev_rid_name)
        prev_rid_name = meta.pop("rid_name")
        seasons.append(meta)
        adds += a
        drops += d
        fails += f
        bad = [t for t in meta["teams"] if not t["faab_ok"]]
        print(f"  {meta['s']}: {len(a)} adds, {len(d)} drops, {len(f)} failed claims, "
              f"through wk {meta['done']}; FAAB mismatches: "
              + (", ".join(f"{t['m']} bids={t['bids']} traded={t['faab_traded']} used={t['used']}" for t in bad) or "none"))

    st = nfl_state()
    out = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nfl": {"season": st.get("season"), "week": st.get("week")},
        "hit_threshold": HIT_THRESHOLD,
        "managers": sorted({t["m"] for s in seasons for t in s["teams"]}),
        "seasons": seasons, "adds": adds, "drops": drops, "fails": fails,
    }
    OUTPUT.write_text(json.dumps(out, separators=(",", ":")))
    print(f"Wrote {OUTPUT.name}: {OUTPUT.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
