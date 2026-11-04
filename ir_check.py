#!/usr/bin/env python3
"""
IR eligibility check for the DTF Club Sleeper league.

League rule: a player who is NEW to a team this week (waiver claim, free agent, trade,
commissioner move... anything that put him on that roster during the current week)
CANNOT be placed on IR in his first week there. From the next week on he can be.

This script walks every roster and reports players who are Doubtful / Out / COVID / IR
on the NFL side (see --statuses) and who arrived this week:

  VIOLATIONS - already sitting in an IR slot, but arrived this week (must come off IR)
  BLOCKED    - hurt, NOT in an IR slot, and arrived this week (can't be moved to IR yet)

Data: Sleeper API only (state, rosters, users, this week's transactions, players).
Only dependency: requests.

Usage:
  python3 ir_check.py                    # current week, print report
  python3 ir_check.py --week 9           # a specific week
  python3 ir_check.py --post             # also post to Slack (SLACK_WEBHOOK_URL env var)
  python3 ir_check.py --json             # machine readable

Env: SLEEPER_LEAGUE_ID (default: the 2026 DTF Club league), SLACK_WEBHOOK_URL.
"""
import argparse
import json
import os
import sys

import requests

SLEEPER = "https://api.sleeper.app/v1"
LEAGUE_ID = os.environ.get("SLEEPER_LEAGUE_ID", "1389416556617801728")  # 2026

# Sleeper injury_status values (lower-cased) that make a player IR-relevant under our rule.
DEFAULT_STATUSES = {"out", "doubtful", "ir", "cov", "covid", "covid-19"}


def get(path):
    r = requests.get(f"{SLEEPER}{path}", timeout=60)
    r.raise_for_status()
    return r.json()


def week_arrivals(transactions, week):
    """{(roster_id, player_id): transaction} for the latest completed add of each player to
    each roster during `week`. Later transactions overwrite earlier ones, so a player who is
    added, dropped and re-added is still 'new this week'."""
    arrivals = {}
    for t in sorted(transactions, key=lambda t: t.get("created", 0)):
        if t.get("status") != "complete" or t.get("leg", week) != week:
            continue
        for pid, rid in (t.get("adds") or {}).items():
            arrivals[(rid, pid)] = t
    return arrivals


def describe_arrival(txn, pid):
    kind = txn.get("type")
    if kind == "trade":
        src = (txn.get("drops") or {}).get(pid)
        return {"how": "trade", "from_roster": src}
    if kind == "waiver":
        return {"how": "waiver", "from_roster": None}
    if kind == "free_agent":
        return {"how": "free agent", "from_roster": None}
    return {"how": kind or "unknown", "from_roster": None}


def slot_of(roster, pid):
    if pid in (roster.get("reserve") or []):
        return "IR"
    if pid in (roster.get("taxi") or []):
        return "Taxi"
    if pid in (roster.get("starters") or []):
        return "Starter"
    return "Bench"


def build_report(rosters, arrivals, players, statuses):
    """Pure function (no network) so it can be unit tested."""
    out = {"violations": [], "blocked": []}
    for roster in rosters:
        rid = roster["roster_id"]
        for pid in roster.get("players") or []:
            txn = arrivals.get((rid, pid))
            if not txn:
                continue
            info = players.get(pid) or {}
            status = info.get("injury_status")
            if not status or status.lower() not in statuses:
                continue
            slot = slot_of(roster, pid)
            if slot == "Taxi":
                continue  # taxi squad isn't an IR question
            name = info.get("full_name") or (
                f"{info.get('first_name', '')} {info.get('last_name', '')}".strip()
            ) or (f"{pid} D/ST" if pid.isalpha() else pid)
            row = {
                "player_id": pid,
                "player": name,
                "pos": info.get("position") or ("DEF" if pid.isalpha() else "?"),
                "nfl_team": info.get("team") or "FA",
                "status": status,
                "slot": slot,
                "roster_id": rid,
                "owner_id": roster.get("owner_id"),
                "added_at_ms": txn.get("created"),
                **describe_arrival(txn, pid),
            }
            out["violations" if slot == "IR" else "blocked"].append(row)
    for k in out:
        out[k].sort(key=lambda r: (r["roster_id"], r["player"]))
    return out


def format_report(report, week, owners, league_name="DTF Club"):
    """Slack mrkdwn (also reads fine in a terminal)."""
    def who(rid):
        return owners.get(rid, f"Roster {rid}")

    def line(r):
        src = f"trade from {who(r['from_roster'])}" if r["how"] == "trade" and r["from_roster"] else r["how"]
        return f"• *{who(r['roster_id'])}*: {r['player']} ({r['pos']}, {r['nfl_team']}) is *{r['status']}* — added via {src}"

    v, b = report["violations"], report["blocked"]
    lines = [f"*{league_name} IR eligibility check — Week {week}*"]
    if not v and not b:
        lines.append("✅ No new-this-week players are hurt, so no IR issues.")
        return "\n".join(lines)
    if v:
        lines += ["", f"🚨 *On IR but not eligible this week ({len(v)})* — must come off IR:"]
        lines += [line(r) for r in v]
    else:
        lines += ["", "✅ Nobody ineligible is sitting on IR."]
    if b:
        lines += ["", f"⛔ *Hurt, but can't go to IR until next week ({len(b)})*:"]
        lines += [line(r) + f" (currently {r['slot'].lower()})" for r in b]
    return "\n".join(lines)


def owner_names(rosters, users):
    by_user = {u["user_id"]: u for u in users}
    names = {}
    for r in rosters:
        u = by_user.get(r.get("owner_id")) or {}
        team = (u.get("metadata") or {}).get("team_name")
        disp = u.get("display_name") or f"Roster {r['roster_id']}"
        names[r["roster_id"]] = f"{team} ({disp})" if team else disp
    return names


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--week", type=int, help="NFL week (default: current Sleeper week)")
    ap.add_argument("--statuses", default=",".join(sorted(DEFAULT_STATUSES)),
                    help="comma list of Sleeper injury_status values to treat as IR-relevant")
    ap.add_argument("--post", action="store_true", help="post to Slack via SLACK_WEBHOOK_URL")
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    ap.add_argument("--violations-only", action="store_true",
                    help="scheduled mode: report/post only players already on IR illegally; stay silent if none")
    args = ap.parse_args()

    state = get("/state/nfl")
    week = args.week or state.get("week")
    if not args.week and state.get("season_type") != "regular":
        print(f"Sleeper says season_type={state.get('season_type')}; pass --week to force.", file=sys.stderr)
        return 1

    league = get(f"/league/{LEAGUE_ID}")
    rosters = get(f"/league/{LEAGUE_ID}/rosters")
    users = get(f"/league/{LEAGUE_ID}/users")
    txns = get(f"/league/{LEAGUE_ID}/transactions/{week}")
    players = get("/players/nfl")

    statuses = {s.strip().lower() for s in args.statuses.split(",") if s.strip()}
    report = build_report(rosters, week_arrivals(txns, week), players, statuses)
    owners = owner_names(rosters, users)

    if args.json:
        print(json.dumps({"week": week, **report}, indent=2))
    if args.violations_only:
        report["blocked"] = []
        if not report["violations"]:
            print(f"Week {week}: no IR rule violations; nothing to post.")
            return 0
    text = format_report(report, week, owners, league.get("name", "DTF Club"))
    if not args.json:
        print(text)

    if args.post:
        hook = os.environ.get("SLACK_WEBHOOK_URL")
        if not hook:
            print("--post given but SLACK_WEBHOOK_URL is not set", file=sys.stderr)
            return 2
        requests.post(hook, json={"text": text}, timeout=30).raise_for_status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
