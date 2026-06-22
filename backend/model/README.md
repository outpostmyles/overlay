# Match model — Poisson / Dixon-Coles 1X2

`ratings.py` builds a deliberately simple, transparent "second opinion" on soccer
matches: empirical attack/defense strengths from the free [martj42 international
results](https://github.com/martj42/international_results) dataset, turned into
Poisson goal rates and a 1X2 (home / draw / away) probability via a Dixon-Coles
low-score correction.

It is **not** a market-beater on its own (even FiveThirtyEight's SPI lost to
Pinnacle's closing line over 36k matches). Its job is to flag matches where an
independent estimate disagrees with the market — an honest second opinion, not an
oracle.

## Public interface (`ratings.py`)

- `get_model()` — production entry point. Loads the full dataset (since 2017),
  builds ratings, caches in memory for 24h. Unchanged by the backtest work.
- `load_matches(since_year=2017)` — returns the normalized match list
  (`{date, home, away, hs, as_}`). Single source of truth for match data; the
  raw CSV is cached to `results_cache.csv` (24h TTL) so cold starts and the
  backtest don't re-download.
- `build_ratings(matches)` — builds a `MatchModel` from any already-loaded match
  list. Used by production (full data) and the backtest (a date-bounded training
  subset). This is the seam that makes a leak-free holdout test possible.
- `MatchModel.match_probs(a, b)` → `{a: p_home, "draw": p, b: p_away}`
- `MatchModel.expected_goals(a, b)`, `MatchModel.team_total_over(team, opp, line)`

Note: the model is **venue-neutral** — it has no explicit home-field advantage
term. The backtest accounts for this (see Calibration below).

## Running the backtest

```sh
# default cutoff 2025-01-01 (~7700 train / ~1340 test matches)
/Users/mylesschenfield/poly/.venv/bin/python -m backend.engine.backtest_model

# custom split + write full results to JSON
/Users/mylesschenfield/poly/.venv/bin/python -m backend.engine.backtest_model \
    --cutoff 2025-01-01 --output results.json
```

`backtest_model.py`:

1. Loads the same matches the production model uses (via `load_matches()` — reuses
   the on-disk cache, no separate download).
2. Splits **by date**: train on matches strictly *before* `--cutoff`, test on
   matches at/after it. No holdout match contributes to the ratings used to
   predict it — there is no leakage.
3. Rebuilds the model on the **training subset only** with `build_ratings(train)`.
4. For each holdout match, produces an ordered `[home, draw, away]` probability
   vector and scores it.

## What the metrics mean

All three are **lower-is-better**, averaged over the holdout set:

- **Brier (multiclass)** — mean squared error between the 3-way probability vector
  and the one-hot actual outcome. Range 0..2. The uniform-guess floor for three
  classes is ~0.667.
- **Log loss** — mean negative log of the probability assigned to the actual
  outcome. Punishes confident wrong calls hard. Uniform floor = ln 3 ≈ 1.0986.
- **RPS (ranked probability score)** — squared error on the *cumulative*
  distribution, respecting the order home > draw > away (a near-miss like
  predicting a draw when home wins is punished less than predicting an away win).
  This is the standard football 1X2 scoring rule.

Each is compared to two naive baselines, scored on the **same** holdout matches:

- **uniform** — flat 1/3-1/3-1/3.
- **train base-rate** — the home/draw/away frequencies observed in the training
  set (a stronger baseline; it already encodes home-field advantage).

## Acceptance bar

The real bar is **beating the naive baselines** — especially the train base-rate,
since it's harder. The model only needs to be an honest second opinion.

A Brier of `< ~0.23` is the commonly-quoted "respectable" bar for football 1X2,
but that figure is usually on a per-class / RPS-style scale. Our multiclass Brier
is roughly 3x that scale, so the practical reading is:

- **RPS < ~0.21** and clearly below the base-rate RPS → respectable, useful.
- Model must beat base-rate on **all three** metrics → pass.

### Last measured (cutoff 2025-01-01, 7719 train / 1339 scored test)

| metric  | model  | uniform | base-rate | beats? |
|---------|--------|---------|-----------|--------|
| Brier   | 0.5508 | 0.6667  | 0.6293    | yes    |
| LogLoss | 0.9299 | 1.0986  | 1.0448    | yes    |
| RPS     | 0.1896 | 0.2398  | 0.2267    | yes    |

Verdict: **PASS** — beats both baselines on every metric, and is stable across
cutoffs (2024-06-01 gives essentially the same numbers on ~2250 test matches).

## Calibration caveats

The backtest prints (and writes to JSON) a reliability table. Two known biases,
both expected given the model's design:

- **Underconfident at the top end.** When the model says ~75% it's right ~88% of
  the time. The shrinkage prior (`PRIOR=6.0`) pulls strong teams toward the mean,
  compressing extreme probabilities.
- **Venue-neutral.** Real holdout matches show a clear home edge (home win ~49%
  vs away ~28%), which the model can't express. It still beats the base-rate
  baseline because it gets the *relative* team strengths right; the magnitudes
  are just compressed.

These don't break the "second opinion" use case (relative disagreement with the
market is what matters), but a future upgrade — home-field term, Dixon-Coles MLE
fit, recency weighting — would tighten calibration. Treat the raw probabilities
as directional, not perfectly calibrated.
