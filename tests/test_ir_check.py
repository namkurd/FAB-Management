import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from ir_check import week_arrivals, week_window, build_report, format_report, DEFAULT_STATUSES

PLAYERS = {
    "1": {"full_name": "Hurt Guy", "position": "RB", "team": "KC", "injury_status": "Out"},
    "2": {"full_name": "Doubt Guy", "position": "WR", "team": "BUF", "injury_status": "Doubtful"},
    "3": {"full_name": "Healthy Guy", "position": "TE", "team": "DAL", "injury_status": None},
    "4": {"full_name": "Old IR Guy", "position": "QB", "team": "MIA", "injury_status": "IR"},
    "5": {"full_name": "Quest Guy", "position": "WR", "team": "SF", "injury_status": "Questionable"},
}
ROSTERS = [
    {"roster_id": 1, "owner_id": "a", "players": ["1", "2", "3", "4", "5"], "reserve": ["1", "4"], "starters": ["3"], "taxi": None},
    {"roster_id": 2, "owner_id": "b", "players": ["2"], "reserve": None, "starters": [], "taxi": None},
]
TXNS = [
    {"status": "complete", "type": "waiver", "leg": 9, "created": 1, "adds": {"1": 1}, "drops": None},
    {"status": "complete", "type": "trade", "leg": 9, "created": 2, "adds": {"2": 2}, "drops": {"2": 1}},
    {"status": "failed", "type": "waiver", "leg": 9, "created": 3, "adds": {"4": 1}, "drops": None},
    {"status": "complete", "type": "free_agent", "leg": 9, "created": 4, "adds": {"5": 1, "3": 1}, "drops": None},
    {"status": "complete", "type": "free_agent", "leg": 8, "created": 0, "adds": {"4": 1}, "drops": None},
]

def test_report():
    arr = week_arrivals(TXNS, 9)
    assert (1, "4") not in arr            # failed claim and last-week add don't count
    rep = build_report(ROSTERS, arr, PLAYERS, DEFAULT_STATUSES)
    assert [r["player"] for r in rep["violations"]] == ["Hurt Guy"]     # new + on IR
    assert [(r["player"], r["roster_id"], r["how"], r["from_roster"]) for r in rep["blocked"]] == [("Doubt Guy", 2, "trade", 1)]
    # healthy / questionable new players and long-time IR player are not flagged
    assert "Old IR Guy" not in str(rep) and "Quest Guy" not in str(rep) and "Healthy" not in str(rep)
    assert "Hurt Guy" in format_report(rep, 9, {1: "A", 2: "B"})

def test_readd_same_week_still_new():
    t = [{"status": "complete", "type": "waiver", "leg": 9, "created": 1, "adds": {"1": 1}},
         {"status": "complete", "type": "free_agent", "leg": 9, "created": 2, "drops": {"1": 1}, "adds": None},
         {"status": "complete", "type": "free_agent", "leg": 9, "created": 3, "adds": {"1": 1}}]
    assert (1, "1") in week_arrivals(t, 9)

def test_tuesday_night_waiver_counts_for_the_new_week():
    state = {"season_start_date": "2026-09-09"}
    start, end = week_window(state, 9)                                   # Tue Nov 3 05:00 UTC .. Tue Nov 10 05:00 UTC
    assert (start, end) == (1793682000000, 1794286800000)
    t = [{"status": "complete", "type": "waiver", "leg": 8, "created": 1793753865037, "adds": {"1": 5}, "drops": None},   # Spears: Tue night, Sleeper says leg 8
         {"status": "complete", "type": "free_agent", "leg": 8, "created": start - 1, "adds": {"2": 5}},                  # Monday night: last week
         {"status": "complete", "type": "free_agent", "leg": 9, "created": end + 1, "adds": {"3": 5}}]                    # next Tuesday: next week
    arr = week_arrivals(t, 9, (start, end))
    assert (5, "1") in arr and (5, "2") not in arr and (5, "3") not in arr
    assert (5, "1") not in week_arrivals(t, 9)                           # the old leg-based rule is what missed him

if __name__ == "__main__":
    test_report(); test_readd_same_week_still_new(); test_tuesday_night_waiver_counts_for_the_new_week(); print("ok")
