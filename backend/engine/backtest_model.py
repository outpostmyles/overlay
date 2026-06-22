"""Holdout backtest for the Poisson/Dixon-Coles match model.

Runs a TIME-BASED train/holdout split on the martj42 international dataset:
the model is rebuilt on training matches *strictly before* a cutoff date, then
scored on the holdout matches at/after it. This is an honest out-of-sample test
of the model's 1X2 (home / draw / away) probabilities — no holdout match ever
touches the ratings used to predict it (no leakage).

Run:
    /Users/mylesschenfield/poly/.venv/bin/python -m backend.engine.backtest_model
    /Users/mylesschenfield/poly/.venv/bin/python -m backend.engine.backtest_model --cutoff 2025-01-01 --output results.json

Metrics (lower is better for all three):
  - Brier score (multiclass): mean squared error of the 3-way prob vector vs the
    one-hot outcome. Range 0..2; ~0.6 is the uniform-guess floor for 3 classes.
  - Log loss: mean negative log of the probability assigned to the actual outcome.
  - RPS (ranked probability score): squared error on the *cumulative* distribution,
    which respects the ordering home > draw > away (a near-miss is punished less
    than a far-miss). The standard football 1X2 metric.

Each is compared against two naive baselines: a flat 1/3-1/3-1/3 guess and the
training-set base rate of home/draw/away. The model is only useful if it beats
these baselines out of sample.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone

from ..model.ratings import build_ratings, load_matches

DEFAULT_CUTOFF = "2025-01-01"
_EPS = 1e-15  # clamp for log loss

# 1X2 outcome order, used for RPS (ordinal) and consistent indexing everywhere.
OUTCOMES = ("home", "draw", "away")


def _outcome(hs: float, as_: float) -> str:
    if hs > as_:
        return "home"
    if hs == as_:
        return "draw"
    return "away"


def _probs_vector(model, home: str, away: str) -> list[float] | None:
    """Model probabilities as an ordered [home, draw, away] vector.

    match_probs() returns a dict keyed by team name (the model is venue-neutral),
    so we map the home team's key -> home prob and the away team's key -> away prob."""
    raw = model.match_probs(home, away)
    if not raw:
        return None
    # Keys are: home team name, "draw", away team name. If a team isn't in the
    # ratings the model returns None (handled above). Same-name edge case is fine
    # because identical teams never appear as opponents in real fixtures.
    p_home = raw.get(home)
    p_away = raw.get(away)
    p_draw = raw.get("draw")
    if p_home is None or p_away is None or p_draw is None:
        return None
    return [p_home, p_draw, p_away]


def _brier(probs: list[float], actual_idx: int) -> float:
    """Multiclass Brier = sum over classes of (p - onehot)^2."""
    return sum((p - (1.0 if i == actual_idx else 0.0)) ** 2 for i, p in enumerate(probs))


def _log_loss(probs: list[float], actual_idx: int) -> float:
    p = max(_EPS, min(1.0, probs[actual_idx]))
    return -math.log(p)


def _rps(probs: list[float], actual_idx: int) -> float:
    """Ranked probability score over the ordered outcomes.

    RPS = (1/(K-1)) * sum_{k=1..K-1} ( CDF_pred(k) - CDF_actual(k) )^2 .
    Lower is better; perfect = 0."""
    k = len(probs)
    cum_p = 0.0
    cum_a = 0.0
    total = 0.0
    for i in range(k - 1):
        cum_p += probs[i]
        cum_a += 1.0 if i == actual_idx else 0.0
        total += (cum_p - cum_a) ** 2
    return total / (k - 1)


def _score_set(prob_fn, test, base_rate=None):
    """Average Brier / log-loss / RPS over the test set.

    prob_fn(match) -> ordered [home, draw, away] list, or None to skip.
    Returns (metrics_dict, n_scored)."""
    n = 0
    s_brier = s_ll = s_rps = 0.0
    for m in test:
        probs = prob_fn(m)
        if probs is None:
            continue
        actual = _outcome(m["hs"], m["as_"])
        ai = OUTCOMES.index(actual)
        s_brier += _brier(probs, ai)
        s_ll += _log_loss(probs, ai)
        s_rps += _rps(probs, ai)
        n += 1
    if n == 0:
        return {"brier": None, "log_loss": None, "rps": None}, 0
    return {
        "brier": s_brier / n,
        "log_loss": s_ll / n,
        "rps": s_rps / n,
    }, n


def run_backtest(cutoff: str) -> dict:
    matches = load_matches()
    if not matches:
        raise SystemExit("[backtest] no match data available (fetch failed and no cache)")

    # Sort by date so the split is unambiguous, then split strictly by date.
    matches.sort(key=lambda m: m["date"])
    train = [m for m in matches if m["date"] < cutoff]
    test = [m for m in matches if m["date"] >= cutoff]

    if len(test) < 100:
        raise SystemExit(
            f"[backtest] only {len(test)} test matches at/after {cutoff}; "
            "choose an earlier --cutoff"
        )
    if len(train) < 200:
        raise SystemExit(
            f"[backtest] only {len(train)} training matches before {cutoff}; "
            "choose a later --cutoff"
        )

    # Train ONLY on the pre-cutoff subset. No holdout match contributes to ratings.
    model = build_ratings(train)
    if model is None:
        raise SystemExit("[backtest] failed to build model from training subset")

    # Training-set base rate of home/draw/away (a stronger naive baseline than uniform).
    counts = {o: 0 for o in OUTCOMES}
    for m in train:
        counts[_outcome(m["hs"], m["as_"])] += 1
    nt = sum(counts.values())
    base_rate = [counts[o] / nt for o in OUTCOMES]

    # Only score holdout matches the model can actually predict (both teams known
    # from the TRAINING data). We score the baselines on the *same* matches so the
    # comparison is apples-to-apples.
    def model_fn(m):
        return _probs_vector(model, m["home"], m["away"])

    scorable = [m for m in test if model_fn(m) is not None]
    n_unknown = len(test) - len(scorable)

    uniform = [1 / 3, 1 / 3, 1 / 3]
    model_metrics, n_model = _score_set(model_fn, scorable)
    uniform_metrics, _ = _score_set(lambda m: uniform, scorable)
    baserate_metrics, _ = _score_set(lambda m: base_rate, scorable)

    # Holdout outcome distribution (sanity / calibration context).
    test_counts = {o: 0 for o in OUTCOMES}
    for m in scorable:
        test_counts[_outcome(m["hs"], m["as_"])] += 1

    # Reliability/calibration: bucket every predicted (outcome, prob) pair and
    # compare mean predicted prob vs observed frequency. The model is
    # venue-neutral, so we expect under-prediction of home wins and over-
    # prediction of away wins — this table makes that visible.
    bucket_edges = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    bsum = [0.0] * (len(bucket_edges) - 1)
    bhit = [0] * (len(bucket_edges) - 1)
    bcnt = [0] * (len(bucket_edges) - 1)
    for m in scorable:
        probs = model_fn(m)
        ai = OUTCOMES.index(_outcome(m["hs"], m["as_"]))
        for i, p in enumerate(probs):
            bi = min(int(p * 10), len(bcnt) - 1)
            bsum[bi] += p
            bcnt[bi] += 1
            if i == ai:
                bhit[bi] += 1
    calibration = []
    for i in range(len(bcnt)):
        if bcnt[i] == 0:
            continue
        calibration.append({
            "bucket": f"{bucket_edges[i]:.1f}-{bucket_edges[i + 1]:.1f}",
            "n": bcnt[i],
            "mean_pred": bsum[i] / bcnt[i],
            "observed": bhit[i] / bcnt[i],
        })

    def beats(a, b):
        # lower is better for all three metrics
        return {k: bool(a[k] < b[k]) for k in ("brier", "log_loss", "rps")}

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cutoff": cutoff,
        "train_matches": len(train),
        "test_matches_total": len(test),
        "test_matches_scored": n_model,
        "test_matches_unknown_team": n_unknown,
        "train_base_rate": dict(zip(OUTCOMES, base_rate)),
        "test_outcome_dist": {
            o: (test_counts[o] / n_model if n_model else None) for o in OUTCOMES
        },
        "metrics": {
            "model": model_metrics,
            "baseline_uniform": uniform_metrics,
            "baseline_train_base_rate": baserate_metrics,
        },
        "model_beats_uniform": beats(model_metrics, uniform_metrics),
        "model_beats_base_rate": beats(model_metrics, baserate_metrics),
        "calibration": calibration,
    }
    return result


def _fmt(x) -> str:
    return f"{x:.4f}" if isinstance(x, (int, float)) else "n/a"


def _print_summary(r: dict) -> None:
    m = r["metrics"]
    print()
    print("=" * 68)
    print("  HOLDOUT BACKTEST — Poisson/Dixon-Coles 1X2 model")
    print("=" * 68)
    print(f"  Cutoff date         : {r['cutoff']}  (train < cutoff <= test)")
    print(f"  Train matches       : {r['train_matches']}")
    print(f"  Test matches        : {r['test_matches_total']} "
          f"(scored {r['test_matches_scored']}, "
          f"{r['test_matches_unknown_team']} skipped: unseen team)")
    br = r["train_base_rate"]
    td = r["test_outcome_dist"]
    print(f"  Train base rate     : home {br['home']:.3f} / draw {br['draw']:.3f} / away {br['away']:.3f}")
    print(f"  Holdout outcomes    : home {td['home']:.3f} / draw {td['draw']:.3f} / away {td['away']:.3f}")
    print("-" * 68)
    print(f"  {'metric':<12}{'MODEL':>12}{'uniform':>12}{'base-rate':>12}   beats?")
    for key, label in (("brier", "Brier"), ("log_loss", "LogLoss"), ("rps", "RPS")):
        bu = "Y" if r["model_beats_uniform"][key] else "n"
        bb = "Y" if r["model_beats_base_rate"][key] else "n"
        print(f"  {label:<12}{_fmt(m['model'][key]):>12}"
              f"{_fmt(m['baseline_uniform'][key]):>12}"
              f"{_fmt(m['baseline_train_base_rate'][key]):>12}"
              f"   uni:{bu} base:{bb}")
    print("-" * 68)
    beat_all_uni = all(r["model_beats_uniform"].values())
    beat_all_base = all(r["model_beats_base_rate"].values())
    verdict = ("PASS — beats both baselines on every metric"
               if beat_all_uni and beat_all_base else
               "PASS — beats the (stronger) base-rate baseline"
               if beat_all_base else
               "MIXED — does not beat base-rate on every metric (see above)")
    print(f"  Verdict: {verdict}")
    brier = m["model"]["brier"]
    if isinstance(brier, (int, float)):
        bar = "respectable (< ~0.23 equiv per the README bar)" if brier < 0.62 else "weak"
        # note: 0.23-style bars are usually quoted in the per-class (RPS-like) scale;
        # multiclass Brier here is ~3x that. See README for the framing.
        print(f"  Model Brier {brier:.4f} — {bar}.")
    cal = r.get("calibration") or []
    if cal:
        print("-" * 68)
        print("  Calibration (predicted prob bucket -> observed frequency):")
        print(f"    {'bucket':<12}{'n':>8}{'mean_pred':>12}{'observed':>12}")
        for c in cal:
            print(f"    {c['bucket']:<12}{c['n']:>8}"
                  f"{c['mean_pred']:>12.3f}{c['observed']:>12.3f}")
    print("=" * 68)
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description="Holdout backtest for the 1X2 match model.")
    ap.add_argument("--cutoff", default=DEFAULT_CUTOFF,
                    help=f"YYYY-MM-DD split date (default {DEFAULT_CUTOFF}). "
                         "Train < cutoff <= test.")
    ap.add_argument("--output", default=None,
                    help="Write full results to this JSON file (e.g. results.json).")
    args = ap.parse_args()

    # Validate cutoff format early.
    try:
        datetime.strptime(args.cutoff, "%Y-%m-%d")
    except ValueError:
        raise SystemExit(f"[backtest] --cutoff must be YYYY-MM-DD, got {args.cutoff!r}")

    result = run_backtest(args.cutoff)
    _print_summary(result)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(result, f, indent=2)
        print(f"[backtest] wrote {args.output}")


if __name__ == "__main__":
    main()
