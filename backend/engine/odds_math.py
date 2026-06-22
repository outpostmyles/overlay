"""Pure odds math: conversions, de-vigging, Kelly staking. No I/O, fully unit-testable.

The whole tool rests on three ideas:
  1. Any price (American, decimal, prediction-market cents) -> an implied probability.
  2. A book's prices sum to >100% (the vig). Stripping the vig -> a "fair" probability.
  3. Bet only when a price beats the fair probability (+EV), and size with fractional Kelly.
"""
from __future__ import annotations

from typing import Sequence

from scipy.optimize import brentq


# --------------------------------------------------------------------------- #
# Conversions
# --------------------------------------------------------------------------- #
def american_to_decimal(american: float) -> float:
    if american > 0:
        return 1.0 + american / 100.0
    return 1.0 + 100.0 / abs(american)


def decimal_to_american(dec: float) -> int:
    if dec <= 1.0:
        return 0
    if dec >= 2.0:
        return round((dec - 1.0) * 100)
    return round(-100.0 / (dec - 1.0))


def decimal_to_prob(dec: float) -> float:
    return 1.0 / dec if dec and dec > 0 else 0.0


def prob_to_decimal(p: float) -> float:
    return 1.0 / p if p and p > 0 else float("inf")


def prob_to_american(p: float) -> int:
    return decimal_to_american(prob_to_decimal(p))


def format_american(american: int) -> str:
    return f"+{american}" if american > 0 else str(american)


# --------------------------------------------------------------------------- #
# De-vigging: turn raw implied probabilities (which sum to >1) into fair ones.
# --------------------------------------------------------------------------- #
def devig_multiplicative(raw_probs: Sequence[float]) -> list[float]:
    """Simplest method: divide each by the overround. Fine for balanced, low-vig markets."""
    s = sum(raw_probs)
    if s <= 0:
        return list(raw_probs)
    return [p / s for p in raw_probs]


def devig_power(raw_probs: Sequence[float]) -> list[float]:
    """Power method: find k so that sum(p_i**k) == 1.

    Handles favorite-longshot bias better than multiplicative and always stays in [0,1].
    Recommended default for 2- and 3-way markets.
    """
    s = sum(raw_probs)
    if s <= 0 or abs(s - 1.0) < 1e-9:
        return list(raw_probs)
    pos = [p for p in raw_probs if p > 0]

    def f(k: float) -> float:
        return sum(p ** k for p in pos) - 1.0

    try:
        lo, hi = (1.0, 100.0) if s > 1.0 else (1e-6, 1.0)
        k = brentq(f, lo, hi, maxiter=200)
    except Exception:
        return devig_multiplicative(raw_probs)
    return [(p ** k) if p > 0 else 0.0 for p in raw_probs]


def devig(raw_probs: Sequence[float], method: str = "power") -> list[float]:
    if method == "multiplicative":
        return devig_multiplicative(raw_probs)
    return devig_power(raw_probs)


def overround(raw_probs: Sequence[float]) -> float:
    """The book's hold / vig as a fraction, e.g. 0.05 == 5%."""
    return sum(raw_probs) - 1.0


# --------------------------------------------------------------------------- #
# Expected value & Kelly staking
# --------------------------------------------------------------------------- #
def expected_value(fair_prob: float, dec_odds: float) -> float:
    """EV per $1 staked. Positive => +EV bet. e.g. 0.05 == +5% expected return."""
    return fair_prob * dec_odds - 1.0


def kelly_fraction(prob: float, dec_odds: float) -> float:
    """Full-Kelly fraction of bankroll. b = net decimal odds; returns 0 if no edge."""
    b = dec_odds - 1.0
    if b <= 0:
        return 0.0
    f = (b * prob - (1.0 - prob)) / b
    return max(0.0, f)


def kelly_stake(prob: float, dec_odds: float, bankroll: float, fraction: float = 0.25) -> float:
    """Recommended stake using fractional (default quarter) Kelly."""
    return round(bankroll * fraction * kelly_fraction(prob, dec_odds), 2)
