"""Opponent-adjusted prop projection — the honest core of prop pricing.

A raw average describes the past; a projection adjusts it for THIS matchup. The model is unified:

    projected_per_game = player_per90_rate × matchup_context × (expected_minutes / 90)
    P(over line) = Poisson survival at the line.

The `player_per90_rate` is the player's measured rate (from accumulated API-Football stats) shrunk
toward a positional prior by games played — so it's robust early and sharpens as games bank. The
`matchup_context` is where the OPPONENT enters, and it differs per stat:

  • Shots / SOT / Goals → attacking output vs THIS defence ≈ team expected goals / league-avg xG.
  • Passes            → POSSESSION share (a dominant team sees more of the ball ⇒ more passes),
                         estimated from the model's relative team strength.

So a deep midfielder projects to ~84 passes against a weak side his team will dominate, ~52 against a
strong one — and a striker's shots scale with how many chances his team will create against that
specific opponent. Everything is an estimate (sanity-grade), surfaced honestly.
"""
from __future__ import annotations

import math

# Per-90 positional priors (average-matchup baselines). Measured rates shrink toward these.
_GOAL90 = {"F": 0.45, "M": 0.16, "D": 0.04, "G": 0.004}
_SHOT90 = {"F": 2.4, "M": 1.2, "D": 0.55, "G": 0.0}
_PASS90 = {"F": 34.0, "M": 58.0, "D": 62.0, "G": 26.0}
_SOT_RATE = 0.36      # ~36% of shots are on target (prior; measured sot90 used when available)
_SHRINK_K = 3.0       # games of prior weight — measured rate dominates after a few games
_AVG_XG = 1.35        # league-average team xG (normalizes the shots/goals context multiplier)
_BREAKEVEN = 0.55     # a lenient "real edge" bar (power-play legs actually need ~57%+)


def _pos_key(position) -> str:
    """Map PrizePicks position strings to F/M/D/G. 'midfield' before 'defen' (Defensive Mid → M),
    any '... Back' → defender."""
    p = (position or "").strip().lower()
    if "goal" in p or "keeper" in p or p == "gk":
        return "G"
    if "midfield" in p or p in ("cm", "dm", "am", "mid"):
        return "M"
    if "defen" in p or "back" in p or p in ("cb", "lb", "rb", "fb", "wb"):
        return "D"
    if "attack" in p or "forward" in p or "strik" in p or "winger" in p or p in ("st", "cf", "lw", "rw", "fw"):
        return "F"
    return "M"


def _poisson_sf(k: int, lam: float) -> float:
    """P(X > k) for integer k under Poisson(lam)."""
    if lam <= 0:
        return 0.0
    cdf, term = 0.0, math.exp(-lam)
    for i in range(0, k + 1):
        if i:
            term *= lam / i
        cdf += term
    return max(0.0, min(1.0, 1.0 - cdf))


def _label(prob: float) -> str:
    if prob >= _BREAKEVEN + 0.05:
        return "value"
    if prob >= _BREAKEVEN:
        return "lean"
    if prob <= 0.38:
        return "fade"
    return "none"


def _blend(measured, prior, games) -> float:
    """Shrink a measured rate toward the positional prior by games played (Bayesian-ish)."""
    if measured is None:
        return prior
    return (measured * games + prior * _SHRINK_K) / (games + _SHRINK_K)


def price(stat_type, line, *, team_xg=None, possession=None, exp_minutes=90,
          position=None, measured=None) -> dict | None:
    """Project a prop and price the over. team_xg = model expected goals for the player's team vs this
    opponent; possession = the team's projected possession share (0-1); measured = the player's
    {shots90, sot90, passes90, games} or None. Returns {prob, lam, basis, value} or None."""
    if line is None:
        return None
    try:
        line = float(line)
    except (TypeError, ValueError):
        return None
    pos = _pos_key(position)
    st = (stat_type or "").strip()
    g = (measured or {}).get("games", 0) if measured else 0
    mins = max(0.1, min(1.0, (exp_minutes or 90) / 90.0))

    if st == "Goals":
        if team_xg is None:
            return None
        per90 = _GOAL90.get(pos, 0.16)                       # goals aren't in the stats feed → prior
        lam = per90 * max(0.45, min(2.4, team_xg / _AVG_XG)) * mins
        what = "goals"
    elif st in ("Shots", "Shots On Target"):
        if team_xg is None:
            return None
        sot = st == "Shots On Target"
        prior = _SHOT90.get(pos, 1.2) * (_SOT_RATE if sot else 1.0)
        per90 = _blend((measured or {}).get("sot90" if sot else "shots90"), prior, g)
        lam = per90 * max(0.45, min(2.4, team_xg / _AVG_XG)) * mins
        what = "SOT" if sot else "shots"
    elif st in ("Passes Attempted", "Passes"):
        # Pass volume is hugely role-specific (a deep metronome ~110/game vs an attacking mid ~45) —
        # the coarse positional prior can't tell them apart, so projecting from it would wrongly fade
        # high-volume players. Only price passes once we have the player's MEASURED rate (≥2 games);
        # otherwise leave it to the neutral heuristic. The opponent (possession) adjustment then rides
        # on a real baseline.
        mpass = (measured or {}).get("passes90")
        if mpass is None or g < 2:
            return None
        per90 = _blend(mpass, _PASS90.get(pos, 55.0), g)
        poss_mult = max(0.6, min(1.4, (possession if possession is not None else 0.5) / 0.5))
        lam = per90 * poss_mult * mins
        what = "passes"
    else:
        return None  # tackles / assists / etc. — not projected here

    prob = _poisson_sf(int(math.floor(line)), lam)
    src = "measured" if g else "model"
    return {"prob": round(prob, 3), "lam": round(lam, 2),
            "basis": f"proj ~{lam:.1f} {what} ({src})", "value": _label(prob)}
