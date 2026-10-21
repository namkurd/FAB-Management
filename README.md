# FAB Management

Who in the DTF Club is best at the waiver wire? This ranks every manager on FAB waiver claims and free-agent pickups since 2021, scored with the league's own points.

**Live site:** https://namkurd.github.io/FAB-Management/ (GitHub Pages, `main` branch, root)

## How it works

- `update_waivers.py` walks the Sleeper league chain from the current season back to 2021, pulls every transaction, completed matchup and weekly stat line, and writes `data.json` (with week-by-week detail behind every number). Its only dependency is `requests`.
- `index.html` is a single static page with no libraries that loads `data.json?_=<timestamp>` and does all filtering in the browser.
- `.github/workflows/update.yml` reruns the script every hour and commits `data.json` only when the data actually changed. You can also trigger it from the Actions tab with **Run workflow**.

Only completed weeks are read or cached. Sleeper pre-generates future matchups with stale rosters and zero points, so the current week is never used.

## Metrics

Everything is measured in **points above replacement (PAR)**, using the exact definition and the same replacement tables as the Trade Network (`namkurd/SleeperTradeNodes`). See `par.py`.

Filters: season, **positions** (FLEX = RB/WR/TE, the default · QB · DEF/K · All), **PAR basis** (Started / Rostered) and pickup type (FAB claims / free agents). Hovering any number shows the pickups, bids or weeks behind it.

| Metric | Definition |
|---|---|
| PAR (headline) | Started: Σ over weeks in the starting lineup of (points − replacement PPG). Rostered: Σ over rostered weeks he played. Regular season, plus playoff weeks for teams that were in the playoffs (a first-round bye counts; membership comes from Sleeper's winners bracket, plus the league's recorded podium, SleeperAuction `config/playoffs.json`) |
| PAR / wk | PAR ÷ weeks played: regular-season weeks plus the playoff weeks the team was in (comparable across seasons, including the partial one) |
| Difference-makers | Pickups worth 25+ started PAR |
| Early calls | PAR/G ≤ 0 in the 3 weeks before the pickup counted, ≥ +3 in the 4 weeks after, and held ≥ 2 of those weeks |
| Chases | One spike game right before the pickup (≥ +8 PAR), the 3 weeks before that ≤ 0 PAR/G (or no games), ≤ 0 PAR/G in the 4 weeks after |
| FAB spent / PAR per $ | Sum of winning bids; PAR from claims that cost $1+ ÷ dollars spent on them ($0 claims left out) |
| Regret | Started PAR that dropped players produced in *other* managers' lineups, from the drop until you re-acquire him or a different manager drops him, after which it belongs to that manager (floored at 0 per drop; windows never overlap) |
| Net | PAR − regret |

**Replacement level** (per season × position): cutoff rank = average number of that position actually started per week + bench depth (½ per team at QB/TE/K/DEF, 1 at RB/WR); replacement PPG = PPG of the player at that rank (league scoring, all NFL players, ≥ 40% of games). Tables are read from the Trade Network's published `data.json`; they're rebuilt locally with the same code only if that's unreachable or behind.

Page sections: leaderboard → PAR by season (manager × season heatmap) → one sortable, scrollable pickups table (filter by timing: early calls / chases, by manager and by player; sort by PAR for best pickups, Regret for biggest regrets, Bid for FAB spends) → method with the replacement table.

## Sit / start (`lineup.html`)

A **miss** = in a completed regular-season week, a starter scored below replacement for his position while a bench player eligible for that same slot scored above replacement for his (and more than the starter). Each bench player fixes at most one slot per week, biggest hole first, so depth on its own is never penalised. Reported per manager: points left on the bench (total and per week), misses, **cost a win** (losses where that week's misses exceed the margin) **rumbles lost** (the league's standings: 9 for a win + 1 per team outscored; re-scored with that week's right calls, everyone else unchanged; weekly scores reproduce SleeperAuction's official Rumbles totals exactly) — counting only starts that weren't defensible going in. Each candidate is checked against Sleeper's pre-game projections (scored with league settings): calls are sorted into a 2 × 2 — started the lower projection by 2+ pts and lost the swap = **clear miss** (the headline), won it = **big-brain start**; projections within 2 pts = **toss-up** won / lost (lost flips join the headline with the "+ Toss-ups" toggle); started the higher projection by 2+ pts = defensible / expected, never counted. Winning calls mirror the miss rule: starter above replacement, the bench option below. Every number has a tooltip with both players, their points vs replacement, their projections and the matchup result.

## For the yearly report card

`data.json` → `summary[season][manager][flex|stream|all]` holds the FAB component per manager-season (`par`, `parWk`, `regret`, `net`, `fab`, `hits`, `early`, `chase`, `adds`, `weeks`) plus `lineup` / `lineupWithFlips` (`mistakes`, `lost`, `lostWk`, `winsLost`, `rumblesLost`) and `calls` (`bigBrain`, `bigBrainPts`, `flipsWon`, `flipsLost`), keyed by person and in PAR, ready to sit next to draft, trade, injury-luck and matchup-luck components.

## Data checks

The script prints a FAB integrity check for each season: winning bids minus FAB traded away should equal Sleeper's `waiver_budget_used`. Every season reconciles except three small leftovers (Ben and Tommy in 2021, Ben in 2024), which are probably commissioner adjustments.

In 2023, roster 9 has no `owner_id` because the owner later left the league. The script inherits that slot's previous-season owner, which is Tommy, and the SleeperAuction standings confirm it.

## Yearly maintenance

When Sleeper creates next season's league, set `LATEST_LEAGUE_ID` in `update_waivers.py` and add any new usernames to `USERNAME_TO_FIRST`. The script finds earlier seasons on its own through `previous_league_id`.

## Run locally

```bash
pip install -r requirements.txt
python update_waivers.py          # ~1 min cold, seconds once cache/ is warm
python -m http.server             # then open http://localhost:8000
```

## Season context on hover

Hovering a manager in a season (heatmap cells, or the leaderboard name when one season is selected) shows record, **PF+** (points for ÷ that season's league average × 100, so 120 = 20% above average), regular-season finish by Rumbles and how the season ended (champion, runner-up, 3rd, 4th, made the playoffs, missed the playoffs, Sacko). `data.json` → `seasons[].teams[]` carries `wins, losses, ties, pf, pfp, rank, rumbles, inPO, pw, weeks, finish`. Playoff teams for 2021 to 2025 are listed in seed order in `playoff_teams.json` (the league's own record, since Sleeper's brackets for those years don't match what was played); from 2026 they come from Sleeper's winners bracket, with a first-round bye counting as a playoff week. Podium and Sacko names come from SleeperAuction's `config/playoffs.json`. Podium teams and four-team brackets count both playoff weeks; in five-team years the two non-podium teams count one week, because the record doesn't say which of them lost in the opening round. Hover also shows the playoff seed.
