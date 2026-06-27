"""Monte Carlo tournament simulation.

The match model (ratings.py) prices a single game. The whole-tournament markets Polymarket actually
runs at the most volume - "win the World Cup", "win the group", "reach the semi-final" - depend on the
entire bracket, so we play it out many times using the model's Poisson goal rates and count how often
each team reaches each stage.

This is surfaced as a SECOND OPINION versus the de-vigged Polymarket futures line, consistent with the
rest of the tool: prediction markets are the sharp anchor, the simulation is a sanity check, not a
"the market is wrong, bet against it" engine.

Fidelity, stated honestly:
  - Group stage is exact: 12 groups of 4, full round-robin, top 2 advance plus the 8 best third-place
    teams, ranked by points then goal difference then goals for.
  - Knockout is a strength-seeded single-elimination approximation. The exact official bracket draw is
    NOT modeled; each round re-seeds and pairs strong-vs-weak, and ties are resolved 50/50 (penalties).
  - So group-stage probabilities (win group, advance) are robust; deep-run and winner probabilities are
    directional.
"""
from __future__ import annotations

import math
import random

from .. import config

_KO_STAGES = ["reach_r16", "reach_qf", "reach_sf", "reach_final", "win_cup"]
_KEYS = ["win_group", "advance"] + _KO_STAGES


def _poisson(lam: float) -> int:
    """Knuth sampler - fast for the small lambdas (~1-2 goals per side) in football."""
    target = math.exp(-min(max(lam, 0.05), 12.0))
    k, p = 0, 1.0
    while True:
        k += 1
        p *= random.random()
        if p <= target:
            return k - 1


def simulate(groups: dict, lambdas, strength, played: dict | None = None,
             n: int | None = None, seed: int | None = None) -> dict:
    """groups: {label: [team_key, ...]}; lambdas(a, b) -> (la, lb) expected goals or None;
    strength(team) -> float (knockout seeding); played: {frozenset({a,b}): {a: goals, b: goals}} of group
    matches ALREADY decided (their real result is locked in, not re-simulated, so mid-tournament the sim
    conditions on the standings so far). Returns
    {team: {win_group, advance, reach_r16, reach_qf, reach_sf, reach_final, win_cup}} as probabilities."""
    n = n or config.TOURNAMENT_SIMS
    if seed is not None:
        random.seed(seed)
    played = played or {}
    teams = [t for g in groups.values() for t in g]
    counts = {t: {k: 0 for k in _KEYS} for t in teams}
    base = config.TOURNAMENT_BASE_GOALS

    def lam(a, b):
        return lambdas(a, b) or (base, base)

    def play(a, b):
        done = played.get(frozenset((a, b)))
        if done is not None:                       # already-played group match → use the real score
            return done[a], done[b]
        la, lb = lam(a, b)
        return _poisson(la), _poisson(lb)

    for _ in range(n):
        qualifiers: list = []
        thirds: list = []   # (rank_score, team)
        for gteams in groups.values():
            gteams = list(gteams)
            if len(gteams) < 2:
                continue
            pts = {t: 0 for t in gteams}
            gd = {t: 0 for t in gteams}
            gf = {t: 0 for t in gteams}
            for i in range(len(gteams)):
                for j in range(i + 1, len(gteams)):
                    a, b = gteams[i], gteams[j]
                    ga, gb = play(a, b)
                    gf[a] += ga; gf[b] += gb
                    gd[a] += ga - gb; gd[b] += gb - ga
                    if ga > gb:
                        pts[a] += 3
                    elif gb > ga:
                        pts[b] += 3
                    else:
                        pts[a] += 1; pts[b] += 1
            rank = sorted(gteams, key=lambda t: (pts[t], gd[t], gf[t], random.random()), reverse=True)
            counts[rank[0]]["win_group"] += 1
            counts[rank[0]]["advance"] += 1
            counts[rank[1]]["advance"] += 1
            qualifiers += [rank[0], rank[1]]
            if len(rank) >= 3:
                thirds.append((pts[rank[2]] * 1000 + gd[rank[2]] + random.random(), rank[2]))

        thirds.sort(reverse=True)               # 8 best third-place teams also advance
        for _score, t in thirds[:8]:
            counts[t]["advance"] += 1
            qualifiers.append(t)

        field = qualifiers                      # knockout: strength-seeded single elimination
        for stage in _KO_STAGES:
            if len(field) < 2:
                break
            field.sort(key=strength, reverse=True)
            nxt = []
            lo, hi = 0, len(field) - 1
            if (hi - lo + 1) % 2 == 1:           # odd field (partial groups) → top seed gets a bye
                nxt.append(field[lo]); lo += 1
            while lo < hi:
                a, b = field[lo], field[hi]      # protect seeds: strong vs weak
                ga, gb = play(a, b)
                if ga > gb:
                    w = a
                elif gb > ga:
                    w = b
                else:
                    w = a if random.random() < 0.5 else b    # penalty shootout, 50/50
                nxt.append(w); lo += 1; hi -= 1
            field = nxt
            for t in field:
                counts[t][stage] += 1

    return {t: {k: round(v / n, 4) for k, v in c.items()} for t, c in counts.items()}
