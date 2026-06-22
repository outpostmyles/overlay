# Overlay

A self-hosted research tool for World Cup 2026 that anchors every read to prediction-market prices, measures sportsbooks, props, and an independent model against that anchor, and tracks closing line value to find out whether an edge is actually real.

It is a single-user research tool, not a tipster product. It does not sell picks or promise winners. It is built to test one hypothesis honestly: that durable betting edge comes from market structure (a sharp reference, line shopping, disciplined staking, and closing line value) rather than from "predicting" games.

## The thesis

Sharp books like Pinnacle are restricted or unavailable across most of the US, so there is no obvious "true price" to bet into. Overlay uses liquid prediction markets, Polymarket and Kalshi, as that sharp reference instead. It strips the vig out of their prices to recover a no-vig fair probability for each outcome, then grades three things against that fair line:

- sportsbook moneylines (best available price and the edge versus fair),
- player props and total-corner lines, and
- an independent Poisson match model.

Every surfaced bet is logged and scored on closing line value (CLV): did the price you took beat where the market closed. CLV is the most reliable leading indicator of long-run betting skill, so the entire app is built to compute it honestly, on free data, and let the record accumulate over time.

## What makes it more than a scraper

### De-vigging the sharp line
A raw market price embeds the operator's margin. Overlay removes it with the power method: it solves (via SciPy's `brentq`) for the exponent that makes the de-vigged probabilities across an outcome set sum to 1, recovering each selection's no-vig fair probability. It does this per market across the configured sharp sources (Polymarket + Kalshi) and folds the best sportsbook price plus the edge-versus-fair directly onto each pick, so line shopping happens at the point of decision.

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
  model/        ratings (Poisson 1X2) · props (opponent-adjusted props) · corners · parlay · player_rates
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
- archetype-scoped picks (favorite moneylines, anytime goalscorer, shots and shots on target, team totals, popular props, total corners),
- opponent-adjusted prop and corner projections,
- same-game parlay pricing,
- web-grounded AI match verdicts (manual trigger),
- a full settlement and CLV loop with a bankroll curve and by-archetype record.

Known limits: it is intentionally scoped to one bettor's archetypes; props and corners only surface as bets once enough measured history has banked for the teams or players involved (the model declines to bet a pure prior); and there is no auth or deployment tooling, since it is meant to run locally for a single user.

## Disclaimer

This is a personal research and educational project. It is not betting advice, not financial advice, and makes no guarantee of profit. Sports betting and prediction-market trading carry real risk of loss, and their legality varies by jurisdiction; check your local laws and gamble responsibly.
