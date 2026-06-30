"""Corner-kick projection — total and per-team, opponent-and-dominance adjusted.

Corners are a dominance market: the team that controls the ball and attacks more wins more corners.
That's the same signal the match model already estimates, so corners ride the same opponent logic as
shots:

    team_corners = corners_for_rate × (opponent_corners_conceded / league_avg) × possession_mult
    total        = team_A_corners + team_B_corners
    P(total > line) = Poisson survival at the line.

`corners_for_rate` / `corners_conceded` come from the API-Football team-stats cache (corners for and
against, per team, accumulated over the tournament) and are shrunk toward a league prior by games
played — robust early, sharper as matchdays bank. `possession_mult` lets the match model's view of who
will dominate nudge the projection when we have it. Everything is an estimate, surfaced honestly.
"""
from __future__ import annotations

import json
import math

from .. import config
from ..matching import normalize_team

_AVG_CORNERS = 5.0   # league-average corners per team per game (~10 total)
_SHRINK_K = 3.0      # games of prior weight — measured rate dominates after a few games
_MIN_LAM = 0.5       # floor so a Poisson is always well-defined


def build_team_corner_rates() -> dict:
    """{team_key: {cf, ca, games}} — corners FOR / corners AGAINST per game, from the API-Football
    team-stats cache. Returns {} if there's no cache yet (early tournament / no key)."""
    p = config.APIFOOTBALL_CACHE_PATH
    if not p.exists():
        return {}
    try:
        cache = json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return {}

    agg: dict = {}
    for teams in (cache.get("teamstats") or {}).values():
        if not isinstance(teams, dict) or len(teams) != 2:
            continue
        keys = list(teams.keys())
        for tk in keys:
            opp = keys[1] if keys[0] == tk else keys[0]
            cf = (teams[tk] or {}).get("corners") or 0
            ca = (teams[opp] or {}).get("corners") or 0
            a = agg.setdefault(tk, {"cf": 0, "ca": 0, "games": 0})
            a["cf"] += cf
            a["ca"] += ca
            a["games"] += 1

    out: dict = {}
    for tk, a in agg.items():
        if a["games"] <= 0:
            continue
        out[tk] = {"cf": a["cf"] / a["games"], "ca": a["ca"] / a["games"], "games": a["games"]}
    return out


def rates_from_results(results: list) -> dict:
    """{team_key: {cf, ca, games}} from ESPN box-score corners across finished games. ESPN reports
    wonCorners for EVERY team (the API-Football cache covers only the teams it has a key+budget for), so
    this is the broader, free source. Merged over the API-Football rates in the aggregator."""
    agg: dict = {}
    for g in (results or []):
        box = g.get("box") or {}
        teams = [t for t, b in box.items() if isinstance(b, dict) and b.get("corners") is not None]
        if len(teams) != 2:
            continue
        x, y = teams
        for tk, opp in ((x, y), (y, x)):
            a = agg.setdefault(tk, {"cf": 0.0, "ca": 0.0, "games": 0})
            a["cf"] += box[tk].get("corners") or 0
            a["ca"] += box[opp].get("corners") or 0
            a["games"] += 1
    return {tk: {"cf": a["cf"] / a["games"], "ca": a["ca"] / a["games"], "games": a["games"]}
            for tk, a in agg.items() if a["games"] > 0}


def _shrink(measured, games) -> float:
    """Shrink a measured per-game rate toward the league prior by games played."""
    if measured is None or games <= 0:
        return _AVG_CORNERS
    return (measured * games + _AVG_CORNERS * _SHRINK_K) / (games + _SHRINK_K)


def project_team(team_key, opp_key, rates, possession=None) -> float:
    """Projected corners for `team_key` vs `opp_key`: attack rate × opponent leakiness × possession."""
    tr = rates.get(team_key) or {}
    orr = rates.get(opp_key) or {}
    cf = _shrink(tr.get("cf"), tr.get("games", 0))
    opp_conceded = _shrink(orr.get("ca"), orr.get("games", 0))
    lam = cf * (opp_conceded / _AVG_CORNERS)
    if possession is not None:
        lam *= max(0.7, min(1.3, possession / 0.5))
    return max(_MIN_LAM, lam)


def project_total(a_key, b_key, rates, poss_a=None) -> tuple[float, float, float]:
    """(team_a_corners, team_b_corners, total) for the matchup. poss_a = team A possession share (0-1)."""
    la = project_team(a_key, b_key, rates, poss_a)
    lb = project_team(b_key, a_key, rates, (1.0 - poss_a) if poss_a is not None else None)
    return la, lb, la + lb


def over_prob(lam, line) -> float:
    """P(corners > line) under Poisson(lam). Handles the .5 lines books use (no push)."""
    try:
        lam = float(lam)
        line = float(line)
    except (TypeError, ValueError):
        return 0.0
    if lam <= 0:
        return 0.0
    k = int(math.floor(line))
    cdf, term = 0.0, math.exp(-lam)
    for i in range(0, k + 1):
        if i:
            term *= lam / i
        cdf += term
    return max(0.0, min(1.0, 1.0 - cdf))


def confidence(games_a, games_b) -> str:
    """How much measured history backs the projection (drives display + whether to log a bet)."""
    g = min(games_a, games_b)
    if g >= 3:
        return "high"
    if g >= 1:
        return "medium"
    return "prior"   # both teams unseen → pure league prior, projection-only
