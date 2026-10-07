# FAB Management

Who in the DTF Club is best at the waiver wire? This ranks every manager on FAAB waiver claims and free-agent pickups since 2021, scored with the league's own points.

**Live site:** https://namkurd.github.io/FAB-Management/ (GitHub Pages, `main` branch, root)

## How it works

- `update_waivers.py` walks the Sleeper league chain from the current season back to 2021, pulls every transaction, matchup and weekly stat line, and writes `data.json`. Its only dependency is `requests`.
- `index.html` is a single static page (D3 from cdnjs) that loads `data.json?_=<timestamp>` and does all filtering in the browser.
- `.github/workflows/update.yml` reruns the script every hour and commits `data.json` only when the data actually changed. You can also trigger it from the Actions tab with **Run workflow**.

Only completed weeks are read or cached. Sleeper pre-generates future matchups with stale rosters and zero points, so the current week is never used.

## Metrics

| Metric | Definition |
|---|---|
| Started pts (headline) | Points the added player scored in the manager's starting lineup, from the add week until he left the roster, regular season only |
| Rostered pts | Same window, counting every rostered week (started or benched) |
| Playoff pts | Started points after the regular season, shown separately |
| Net | Started pts minus the regret of the player dropped in the same move |
| Regret | What a dropped player later scored in *other* managers' starting lineups that regular season |
| Hit rate | Share of adds with ≥ 30 started points |
| Pts / $ | Points from FAAB claims ÷ FAAB dollars spent |
| Claim win % | Completed FAAB claims ÷ all submitted claims; failures are split into *outbid* and *roster* (roster would be too full) |
| Early / Chase | Timing flags based on league-scoring points per game in the 3 weeks before and the 4 weeks after the add |

League scoring for players who were not on a roster (used for timing and post-drop production) comes from Sleeper's weekly stats endpoint multiplied by the league's `scoring_settings`. This matches Sleeper's own `players_points` to the cent.

## Data checks

The script prints a FAAB integrity check for each season: winning bids minus FAAB traded away should equal Sleeper's `waiver_budget_used`. Every season reconciles except three small leftovers (Ben and Tommy in 2021, Ben in 2024), which are probably commissioner adjustments.

In 2023, roster 9 has no `owner_id` because the owner later left the league. The script inherits that slot's previous-season owner, which is Tommy, and the SleeperAuction standings confirm it.

## Yearly maintenance

When Sleeper creates next season's league, set `LATEST_LEAGUE_ID` in `update_waivers.py` and add any new usernames to `USERNAME_TO_FIRST`. The script finds earlier seasons on its own through `previous_league_id`.

## Run locally

```bash
pip install -r requirements.txt
python update_waivers.py          # ~1 min cold, seconds once cache/ is warm
python -m http.server             # then open http://localhost:8000
```
