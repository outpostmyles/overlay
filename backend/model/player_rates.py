"""Measured per-player rates from accumulated API-Football fixture stats.

Reads the API-Football adapter's existing per-fixture cache (no extra requests) and aggregates each
player's shots / shots-on-target / passes into per-90 rates. These are OPPONENT-NEUTRAL baselines —
the opponent adjustment (possession for passes, attacking output for shots) is applied at projection
time in model/props.py. Thin early in a tournament, so props.py shrinks them toward positional
priors by games played; they sharpen as more matchdays bank.
"""
from __future__ import annotations

import json

from .. import config


def build_rates() -> dict:
    """{player_key: {shots90, sot90, passes90, games, minutes}} from cached finished-game stats.
    Returns {} if there's no cache yet (early tournament / no API-Football key)."""
    p = config.APIFOOTBALL_CACHE_PATH
    if not p.exists():
        return {}
    try:
        cache = json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return {}

    agg: dict = {}
    for entry in (cache.get("stats") or {}).values():
        # cache value is either {player_key: stat} (current) or {"players": {...}} (future-proof)
        players = entry.get("players") if isinstance(entry, dict) and "players" in entry else entry
        if not isinstance(players, dict):
            continue
        for pk, st in players.items():
            if not isinstance(st, dict):
                continue
            mins = st.get("minutes") or 0
            if not mins:
                continue
            a = agg.setdefault(pk, {"min": 0, "shots": 0, "sot": 0, "passes": 0, "games": 0})
            a["min"] += mins
            a["shots"] += st.get("shots") or 0
            a["sot"] += st.get("sot") or 0
            a["passes"] += st.get("passes") or 0
            a["games"] += 1

    out: dict = {}
    for pk, a in agg.items():
        if a["min"] <= 0:
            continue
        f = 90.0 / a["min"]
        out[pk] = {"shots90": a["shots"] * f, "sot90": a["sot"] * f, "passes90": a["passes"] * f,
                   "games": a["games"], "minutes": a["min"]}
    return out
