# Overlay

Overlay is a sports-betting research engine that treats de-vigged prediction-market prices (Polymarket, Kalshi) as the sharp fair line and grades everything against it: sportsbook odds, player props, and its own forecasts. Its defining feature is that it keeps score on itself: every prediction is frozen before the game and auto-graded after, so the results below are what the ledger says, not what I claim.

It started as a World Cup 2026 tool, was re-architected mid-tournament into a multi-sport platform without disturbing the live board, and now runs MLB around the clock on a $6/month server with $0 in API spend.

## Results (all from the self-grading ledger)

**World Cup 2026, complete.** 27 knockout forecasts, each locked ~75 minutes before kickoff with the de-vigged market line frozen at the same instant as the benchmark:

- The independent model picked 23/27 winners (85.2%) vs the market favorite's 19/27 (70.4%), but lost on calibration: Brier 0.446 vs the market's 0.418 (-6.7% skill, closer than the market on only 7 of 27). Better at picking, worse at pricing.
- The skill gap moved from -19.9% at n=9 to -6.7% at n=27. Still a market win at a small sample, which is the finding: the project's thesis is that the de-vigged market is the sharp reference, and the ledger confirmed it against its own model.
- A separate bet ledger holds 112 logged picks, 71-33-8 settled. Only the 23 moneyline picks carry a captured closing line; props, team totals and parlays are logged without one, and that capture has a bug described under limitations, so no closing-line-value figure is quoted here.

**MLB, live and unattended since July 19.** Anchor-only by design: no model, because the WC ledger just demonstrated the market wins the main line. Each game locks a full sheet (moneyline, total, first-5-innings call, the 3 most competitive player props) before first pitch and grades off free box scores. **As of 2026-10-01: 807 games settled** across a full regular season (2026-07-19 to 2026-09-30), plus 3,334 graded prop, total and first-five legs, for 4,141 auto-graded predictions in all.

The headline is a null, and that is the point: market favorites won **476 of 807 (59.0%)** against **462.7 expected** from the market's own de-vigged probabilities, z = +0.95. On the standard 2-outcome scale the Brier is **0.238** (95% CI +/- 0.006), against a 0.242 perfect-calibration floor and 0.250 for a coin flip. The prices do almost exactly what they claim to do. A correctly functioning anchor should look like this, and no edge is claimed.

The per-leg results are the better demonstration, because the ledger corrected itself. At n=197 in August, totals appeared to beat their own frozen line by 3 percentage points. At n=3,334 that gap is gone: player props 1,210/2,330 (51.9% against 52.8% predicted), totals 192/351 (54.7% against 52.8%), first-five 320/653 (49.0% against 48.2%). Every one is inside noise, all |z| < 1. Legs are compared against their own frozen lines rather than against 50%, because they are deliberately selected near a coin flip and 50% was never the right baseline.

## How it works, briefly

Free public APIs (Kalshi, Polymarket, ESPN) feed a normalize → de-vig → grade pipeline. De-vigging uses the power method (SciPy `brentq` solves for the exponent that makes outcome probabilities sum to 1). Forecasts write to a SQLite ledger with a strict lifecycle (pending → locked at T-75, before lineup feeds → settled), and aggregate metrics are withheld until n ≥ 8 so a couple of games can't masquerade as a verdict. A 300-second in-process heartbeat keeps two systemd services (WC archive, MLB live) grading with no browser attached.

## Decisions that required judgment

- **Kill your own model when the data says so.** The forecast ledger exists to test the model against the market. It lost. MLB therefore ships with *no* model (the de-vigged line is the forecast) instead of a rebuilt one. The architecture encodes the finding.
- **De-vig validity over coverage.** Kalshi lists totals as one market per line; the lines of one game are not mutually exclusive, so de-vigging across them is statistically invalid. Each line is parsed as its own two-way book. Same discipline filters placeholder quotes (a far-out first-5 market quoting every leg equal de-vigs to exact thirds; that is noise, and it never locks).
- **Refactor a live system with proof, not confidence.** The multi-sport seam landed under a record-and-replay golden harness: all network inputs pickled once, time frozen, and the full pipeline replayed before and after, with byte-identical output required, while the WC board was in production.
- **Settlement is where money-adjacent code dies quietly.** Baseball added failure modes soccer never had, each with a mechanism: doubleheaders get distinct identities from the ticker's embedded start time (and honestly never lock rather than guess which game is which); a rainout voids, never loses; a game postponed after its lock can only grade against a same-date final; an adversarial review caught that it would otherwise silently settle against the makeup game and poison the record.
- **Adversarial audits over self-belief.** Multi-agent review passes ran against each major change and found real bugs before they cost data: forecasts for post-midnight-UTC kickoffs being voided hours before their lock window; a date-only market twin from a second source falsely flagging every MLB game as a doubleheader (nothing would ever have locked); an ESPN sweep wasting 40 requests per refresh (now 3).
- **Cost as a constraint, not an afterthought.** The deployed server runs keys-blank, hard-guaranteed $0, because the free core (de-vig, ledger, grading) is the product. Paid feeds (Anthropic, The Odds API) fire only on manual actions, behind debounces and credit floors.

## Honest limitations

Nothing here is a profit claim, and the ledger explicitly must not be used to retune the model. A material coverage caveat: 153 MLB forecasts (16% of the slate) were voided rather than graded, and 149 of those never locked at all. That attrition is not random. 93% of the dropped games start at 21:00 or later against 0.6% of the graded ones, so late West Coast matchups are systematically under-represented. Read the 807 as a near-complete sample of games starting before 21:00 rather than of all MLB games. The cause is under active investigation and is tracked as a known defect, not a data-cleaning choice. The WC sample (n=27) is small enough that its model-vs-market gap is indicative, not conclusive. Brier figures use this project's internal 3-outcome sum convention (range 0 to 2), so they are comparable across rows here but not directly against a textbook 0-to-1 Brier. Closing-line capture shipped late in the WC (n=6 there). It runs for all 807 MLB games, but it is still NOT evidence that locking early is free: the close is re-captured on every heartbeat while a game sits locked, so on 495 of the 807 the stored close is identical to the lock and the two Briers match by construction. On the 312 games where the line actually moved, close 0.2420 vs lock 0.2424, which is no detectable cost to freezing 75 minutes early, but it is a measurement artifact away from being a real test. Separately, the bet ledger's own closing capture has no kickoff cutoff, so it kept overwriting during play: 10 of its 23 priced rows sit pinned at 0.989 and all 10 won, and the closing price correlates +0.36 with the result where the pick price correlates -0.06. That is in-play leakage, so the pre-fix closing-line history is unusable and no CLV number is published. Doubleheader games never lock (by design, pending per-event kickoff data), and a known consequence is that they can never settle either. Polymarket's MLB game markets aren't ingested yet (their named-outcome format needs its own parser); PrizePicks is bot-blocked and treated as permanently optional. Player-prop fair values come from thin books; they are displayed, not bet. Single-user tool: no auth, one shared dashboard per sport.

## Stack and scale

Python / FastAPI / httpx (async), NumPy + SciPy, SQLite, vanilla-JS SPA (no build step). About 11,000 lines of source across backend, tests, and frontend (roughly 8,300 excluding blanks and comments; 65 tracked files), **161 tests, all passing** (odds math, settlement edge cases, the golden harness, sport adapters), 41 commits. Deployed on a single DigitalOcean droplet: nginx + two systemd services, per-sport caches, sport-scoped ledger in one database, running keys-blank at $0 API spend.

*Screenshots worth adding here: the Model Ledger's pre-game prediction cards and a graded recap card (predicted vs actual with per-line ✓/✗), and the WC bracket view.*

## Setup

```bash
./run.sh                      # venv + deps + http://localhost:8000 (World Cup archive)
SPORT=mlb PORT=8010 ./run.sh  # the live MLB board
```

All API keys are optional; with none set it runs entirely on free sources. Key configuration is in `.env.example`; always-on deployment (systemd + nginx, keys-blank) is in `DEPLOY.md`.

## Disclaimer

Personal research and educational project. Not betting advice, not financial advice, no guarantee of profit. Sports betting and prediction-market trading carry real risk of loss and their legality varies by jurisdiction; check your local laws and gamble responsibly.
