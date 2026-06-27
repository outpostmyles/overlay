# Overlay

Overlay is a closing-line-value engine for sports betting. It anchors every read to prediction-market prices, Polymarket and Kalshi, as the sharp reference, de-vigs them into a no-vig fair probability for each outcome, and grades sportsbook odds, player props, and an independent model against that fair line. Every surfaced bet is logged and scored on closing line value (CLV), the most reliable leading indicator of long-run betting skill, to test whether an edge is actually real.

It currently runs on the 2026 FIFA World Cup. The tournament is the live dataset the engine is pointed at right now; the prediction-market and stats sources, the team model, and the bet types are all World Cup specific (see Scope below).

It is a single-user research tool, not a tipster product. It does not sell picks or promise winners. It exists to test one hypothesis honestly: that durable edge comes from market structure (a sharp reference, line shopping, disciplined staking, and CLV) rather than from "predicting" games.

## The Polymarket surface

Overlay is built directly on Polymarket data, not just on a price scrape:

- **Direct ingestion.** It reads Polymarket's public Gamma and Data APIs (no key): tournament futures and, where they exist, the per-game win markets. Each is normalized into the same internal market object as every other source and de-vigged into the sharp fair line.
- **Whale flow and net position.** For each game's market it reads Polymarket holder data and recent trades and computes, per side (favorite and underdog): the largest backer, the holder count, and net money flow as BUY minus SELL shares, classified buying, selling, or flat. This whale read is surfaced as a conviction tiebreaker in the AI match verdict: heavy buying on a side is supporting evidence, selling or a thin holder base is a caution flag. It is weighted as a tiebreaker, not a driver, because game-market volume is thin.

## What it measures and shows

For each selection on the slate, drawn straight from the engine and the UI:

- **De-vigged fair probability vs best price, with EV.** The no-vig fair probability from the sharp prediction-market sources, the best available price across books, and the edge of that price versus fair.
- **Polymarket whale-flow and net-position read**, used as the tiebreaker described above.
- **A closing-line-value track record.** Every logged pick freezes its closing fair line at settlement, and the Track Record reports average CLV, beat-close rate (how often the price taken beat the close), units and ROI, a bankroll curve, and a by-archetype breakdown. A confidence-calibration layer can nudge pick confidence by context bucket, but only after a bucket reaches 20 settled picks, so it stays inert until there is enough data to mean anything. These are descriptions of what the tool computes, not performance or profit claims.

## The thesis

Sharp books like Pinnacle are restricted or unavailable across most of the US, so there is no obvious "true price" to bet into. Overlay uses liquid prediction markets, Polymarket and Kalshi, as that sharp reference instead. It strips the vig out of their prices to recover a no-vig fair probability for each outcome, then grades three things against that fair line:

- sportsbook moneylines (best available price and the edge versus fair),
- player props and total-corner lines, and
- an independent Poisson match model.

Every surfaced bet is logged and scored on closing line value: did the price you took beat where the market closed. The entire app is built to compute this honestly, on free data, and let the record accumulate over time.

## What makes it more than a scraper

### De-vigging the sharp line
A raw market price embeds the operator's margin. Overlay removes it with the power method: it solves (via SciPy's `brentq`) for the exponent that makes the de-vigged probabilities across an outcome set sum to 1, recovering each selection's no-vig fair probability. It does this per market across the configured sharp sources (Polymarket + Kalshi) and folds the best sportsbook price plus the edge versus fair directly onto each pick, so line shopping happens at the point of decision.

### A closed CLV and settlement loop on free data
The proof loop runs end to end without any paid data feed:

1. a recommended bet is auto-logged to a SQLite ledger,
2. its closing fair line is frozen at settlement,
3. results are graded from free sources: Kalshi resolved markets for moneylines, ESPN key events and final scores for goalscorers and team totals, and API-Football per-player and per-team stats for shots, shots on target, passes, and corners,
4. CLV, a bankroll curve, and by-archetype hit rate are computed from the graded ledger.

The grading is conservative where it has to be: a player who did not appear voids the bet (DNP, stake returned) rather than scoring it a loss, surname matching is whole-word to avoid false hits, an exact-line total is a push/void, and an under-phrased prop is graded on the under.

### Opponent-adjusted prop projection with Bayesian shrinkage
Props are not priced off a flat average. The model is unified:

```
projected_per_game = player_per90_rate * matchup_context * (expected_minutes / 90)
P(over line)       = Poisson survival at the line
```

The per-90 rate is the player's measured rate (accumulated from API-Football) shrunk toward a positional prior by games played, so it is robust early in the tournament and sharpens as games bank. The matchup context is where the opponent enters: shots, shots on target, and goals scale with the team's expected goals versus that specific defense; passes scale with projected possession share; corners scale with projected dominance against the opponent's corners-conceded rate. Pass volume is deliberately not priced until there is real measured data, because the coarse positional prior cannot tell a deep metronome from an attacking midfielder.

### An honestly backtested match model
An independent Poisson / Dixon-Coles rating model is trained on roughly 9,000 international results since 2017. It is validated on a time-split holdout and scored on Brier score, log loss, and ranked probability score against naive baselines, with documented avoidance of data leakage. It is presented as a second opinion, not a market-beater; the model README is candid that a simple ratings model does not beat a sharp closing line. See `backend/model/README.md`.

### A de-vigged knockout futures board, with a Monte Carlo bracket as the second opinion
The markets Polymarket runs at the most volume are whole-tournament: win the cup, reach the semi-final, reach the quarter-final, reach the round of 16. The Futures tab leads with the de-vigged version of these: each market is normalized so the probabilities sum to the number of teams that actually reach the stage (1 for the winner, 4 for the semi, 8 for the quarter, 16 for the round of 16), which strips the book margin and yields a clean vig-free probability for every team. That sharp number is the headline.

Alongside it rides an independent second opinion: a Monte Carlo simulation that reconstructs the twelve group compositions from finished games alone (union-find over who-played-whom, with component size capped at four, so it needs no group-winner markets and stays correct once cross-group knockout games begin), plays the bracket out thousands of times from the match model's Poisson goal rates conditioned on every group result so far, and reports each team's reach-stage and win-cup frequency. The honesty is the point: a simple ratings model under-separates elite teams, so in the open knockout the simulation runs systematically more conservative than a sharp market, and the tab says so and shows the gap as neutral context rather than a betting edge. The group stage is exact (full round robin, top two plus the eight best thirds, ranked on points then goal difference then goals for); the knockout is a strength-seeded single-elimination approximation, the official bracket draw is not modeled, each round re-seeds strong-vs-weak and ties go 50/50 on penalties, so deep-run numbers are directional. The 12-group field is reconstructed from finished games (so it survives the group-winner markets closing), frozen to disk once every group is a complete round robin, and the simulation runs off the event loop because it is CPU-bound. Surfaced on the Futures tab.

### Hard API-budget engineering
Free, no-key feeds (Polymarket, Kalshi, PrizePicks, ESPN) ride a 60-second auto-refresh behind short caches. The two paid or metered feeds are gated so they cannot run away:

- Anthropic (per token) fires only on a manual Analyze action or an on-demand per-prop Read, and both are disk-cached so repeats cost nothing. Nothing on the auto-refresh loop can spend tokens.
- The Odds API (500 credits per month) is manual-only with a 30-second debounce, a credit floor that serves cache instead of re-hitting an exhausted account, and a separate longer refresh interval for the per-game corner market so repeated clicks do not re-spend.
- API-Football (100 requests per day) has a hard daily cap below the limit, a UTC-aligned day counter, per-fixture results cached forever, and a cooldown so a finished-but-unposted game does not re-fetch every cycle.
- ESPN finished games are memoized by event id (a final result is terminal), and the smart-money slug lookups are cached.

The result runs all month without nearing any limit.

## Tech stack

- Backend: Python, FastAPI, Uvicorn, httpx (async), NumPy and SciPy (de-vig and Poisson math), the Anthropic SDK.
- Frontend: a vanilla-JS single-page app (no framework, no build step), served by the same FastAPI process.
- Storage: SQLite for the pick ledger; JSON and CSV files on disk for source caches.
- Tests: a pytest suite covering the odds math, edge detection, prop projection, settlement, ledger ordering, and the rate-limit guards.

## Architecture

```
backend/
  sources/      polymarket · kalshi · prizepicks · theoddsapi · espn · apifootball  (adapters -> normalized markets)
  engine/       odds_math (convert / de-vig / Kelly) · edges (fair line + best price)
  model/        ratings (Poisson 1X2) · props (opponent-adjusted props) · corners · tournament (Monte Carlo bracket) · parlay · player_rates
  store/        paper (SQLite ledger, settlement, CLV, P/L)
  matching.py   team and market normalization across sources
  smartmoney.py Polymarket per-game whale positions and flow
  reasoning.py  web-grounded AI match verdicts (manual, cached)
  aggregator.py orchestration: fetch -> merge -> compute boards -> settle
  main.py       FastAPI API + serves the dashboard
frontend/       index.html · app.js · styles.css   (Best Bets · Research · Track Record)
```

The frontend polls a single `/api/snapshot` endpoint that returns the full board; manual flags on that endpoint (refresh free feeds, refresh sportsbook lines, run AI analysis) are the only paths that spend money or credits.

## Setup

```bash
./run.sh
# first run creates a virtualenv, installs requirements, and serves http://localhost:8000
```

All API keys are optional. With none set, the app runs fully on the free prediction-market and ESPN sources. To enable the paid or metered features, copy `.env.example` to `.env` and fill in any of:

- `ODDS_API_KEY` - The Odds API, sportsbook moneyline and total-corner lines
- `ANTHROPIC_API_KEY` - Anthropic, AI match research and per-prop reads
- `ANTHROPIC_MODEL` - model id override (defaults to a fast, cheap model)
- `APIFOOTBALL_KEY` - API-Football, per-player and per-team stats for settlement
- `BANKROLL` - bankroll used for unit and Kelly sizing
- `UNIT_PCT` - one unit as a fraction of bankroll

## Status

Working and in active personal use:

- multi-source ingest with a de-vigged consensus fair line and best-price line shopping,
- the Polymarket whale-flow and net-position read,
- archetype-scoped picks (favorite moneylines, anytime goalscorer, shots and shots on target, team totals, popular props, total corners),
- opponent-adjusted prop and corner projections,
- same-game parlay pricing,
- web-grounded AI match verdicts (manual trigger),
- a full settlement and CLV loop with a bankroll curve and by-archetype record.

Scope: the 2026 FIFA World Cup only. The prediction-market and stats sources, the team model, and the bet archetypes are all World Cup specific; the tool has not been pointed at any other event or sport.

Other known limits: it is intentionally scoped to one bettor's archetypes; props and corners only surface as bets once enough measured history has banked for the teams or players involved (the model declines to bet a pure prior); and there is no auth or deployment tooling, since it is meant to run locally for a single user.

## Disclaimer

This is a personal research and educational project. It is not betting advice, not financial advice, and makes no guarantee of profit. Sports betting and prediction-market trading carry real risk of loss, and their legality varies by jurisdiction; check your local laws and gamble responsibly.
