# Overlay

Overlay is a sports-betting research engine that treats de-vigged prediction-market prices (Polymarket, Kalshi) as the sharp fair line and grades everything against it: sportsbook odds, player props, and its own forecasts. Its defining feature is that it keeps score on itself: every prediction is frozen before the game and auto-graded after, so the results below are what the ledger says, not what I claim.

It started as a World Cup 2026 tool, was re-architected mid-tournament into a multi-sport platform without disturbing the live board, and now runs MLB around the clock on a $6/month server with $0 in API spend.

## Results (all from the self-grading ledger)

**World Cup 2026, complete.** 27 knockout forecasts, each locked ~75 minutes before kickoff with the de-vigged market line frozen at the same instant as the benchmark:

- The independent model picked 23/27 winners (85.2%) vs the market favorite's 19/27 (70.4%), but lost on calibration: Brier 0.446 vs the market's 0.418 (-6.7% skill, closer than the market on only 7 of 27). Better at picking, worse at pricing.
- The skill gap moved from -19.9% at n=9 to -6.7% at n=27. Still a market win at a small sample, which is the finding: the project's thesis is that the de-vigged market is the sharp reference, and the ledger confirmed it against its own model.
- A separate bet ledger holds 112 logged picks, 71-33 settled (8 void/pending), each with its closing line frozen for CLV.

**MLB, live now (started July 12).** Anchor-only by design: no model, because the WC ledger just demonstrated the market wins the main line. Each game locks a full sheet (moneyline, total, first-5-innings call, the 3 most competitive player props) before first pitch and grades off free box scores. Five days in: 15 games graded (market Brier 0.443, favorite 10/15), totals 10/15, F5 calls 7/15, player props 25/41, and lock-vs-close Brier 0.443 vs 0.442 (n=15), meaning the line frozen 75 minutes early has lost essentially nothing to the close so far. All small-n and stated as such; the point is the machine that accumulates it (~90 graded predictions/day at full slate).

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

Every result above is small-n; nothing here is a profit claim, and the ledger explicitly must not be used to retune the model. Closing-line capture shipped late in the WC (n=6 there; complete for MLB). Doubleheader games never lock (by design, pending per-event kickoff data). Polymarket's MLB game markets aren't ingested yet (their named-outcome format needs its own parser); PrizePicks is bot-blocked and treated as permanently optional. Player-prop fair values come from thin books; they are displayed, not bet. Single-user tool: no auth, one shared dashboard per sport.

## Stack and scale

Python / FastAPI / httpx (async), NumPy + SciPy, SQLite, vanilla-JS SPA (no build step). ~8,400 lines, 160 tests (odds math, settlement edge cases, the golden harness, sport adapters), 38 commits. Deployed on a single DigitalOcean droplet: nginx + two systemd services, per-sport caches, sport-scoped ledger in one database.

*Screenshots worth adding here: the Model Ledger's pre-game prediction cards and a graded recap card (predicted vs actual with per-line ✓/✗), and the WC bracket view.*

## Setup

```bash
./run.sh                      # venv + deps + http://localhost:8000 (World Cup archive)
SPORT=mlb PORT=8010 ./run.sh  # the live MLB board
```

All API keys are optional; with none set it runs entirely on free sources. Key configuration is in `.env.example`; always-on deployment (systemd + nginx, keys-blank) is in `DEPLOY.md`.

## Disclaimer

Personal research and educational project. Not betting advice, not financial advice, no guarantee of profit. Sports betting and prediction-market trading carry real risk of loss and their legality varies by jurisdiction; check your local laws and gamble responsibly.
