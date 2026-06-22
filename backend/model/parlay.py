"""PrizePicks-style parlay pricing — joint hit probability, payout multiplier, and EV.

PrizePicks is parlay-only: a single prop isn't bettable, so the honest unit is the ENTRY. We take
the model's per-leg probabilities, combine them — with a positive-correlation bump for same-game
legs that move together (favorite wins ↔ its striker scores ↔ its team-total goes over) — apply
PrizePicks' power-play payout multiplier, and report EV vs that payout. The leg probs are model
sanity-grades, so treat the EV as directional, not gospel.
"""
from __future__ import annotations

# Standard PrizePicks power-play multipliers (all legs must hit).
POWER_PAYOUT = {2: 3.0, 3: 5.0, 4: 10.0, 5: 20.0, 6: 37.5}
# Same-game legs are positively correlated; nudge the joint prob this fraction toward the weakest
# leg (a crude but bounded correlation model — independence would understate a same-game stack).
CORRELATION = 0.15


def price(leg_probs: list, payout: float | None = None, same_game: bool = True) -> dict | None:
    """Price a parlay from its per-leg model probabilities. Returns None if < 2 priceable legs."""
    probs = [p for p in leg_probs if p is not None and 0 < p < 1]
    n = len(probs)
    if n < 2:
        return None
    indep = 1.0
    for p in probs:
        indep *= p
    joint = indep
    if same_game:
        joint = indep + CORRELATION * (min(probs) - indep)   # positive correlation lifts the joint
    mult = payout if payout else POWER_PAYOUT.get(n)
    if not mult:
        return None
    ev = joint * mult - 1.0
    return {
        "n": n,
        "joint_prob": round(joint, 4),
        "indep_prob": round(indep, 4),
        "payout": mult,
        "breakeven": round(1.0 / mult, 4),
        "ev": round(ev, 4),
        "ev_pct": round(ev * 100, 1),
    }
