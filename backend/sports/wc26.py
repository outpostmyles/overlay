"""World Cup 2026: the founding sport, extracted verbatim from the pre-adapter constants.

Everything here mirrors the values that were hardwired in sources/ before the seam existed; the
golden-snapshot test holds this adapter to byte-identical output. The soccer-specific model stack
(ratings, tournament sim, corners, props) stays where it is and is gated by the capability flags.
"""
from __future__ import annotations

import re

from . import SportAdapter, register

_GROUP_RE = re.compile(r"^world-cup-group-[a-l]-winner$")


def _polymarket_classify(slug: str) -> str | None:
    if slug == "world-cup-winner":
        return "winner_outright"
    if _GROUP_RE.match(slug):
        return "group_winner"
    if slug.startswith("world-cup-nation-to-reach-round-of-16"):
        return "advance_r16"
    if slug.startswith("world-cup-nation-to-reach-quarterfinals"):
        return "advance_qf"
    if slug.startswith("world-cup-nation-to-reach-semifinals"):
        return "advance_sf"
    return None


WC26 = register(SportAdapter(
    key="wc26",
    display_name="World Cup 2026",
    site_path="wc",
    code="WC",
    outcomes=("a", "draw", "b"),
    espn_path="soccer/fifa.world",
    kalshi_series={
        "KXWCGAME": ("moneyline", "Matches"),
        "KXMENWORLDCUP": ("winner_outright", "Futures"),
    },
    kalshi_resolved_series="KXWCGAME",
    kalshi_strip_reg_time=True,
    kalshi_outright_event="World Cup Winner",
    polymarket_search_q="world cup",
    polymarket_pinned_slugs=("world-cup-winner",),
    polymarket_classify=_polymarket_classify,
    polymarket_game_slug_prefix="fifwc-",
    odds_api_sport="soccer_fifa_world_cup",
    prizepicks_league=241,
    capabilities=frozenset({"model", "futures", "bracket", "corners", "lineups", "smartmoney"}),
    aliases={},                    # the global soccer map in matching.py already covers the WC
    results_window_days=40,        # the whole tournament (futures field reconstruction needs it)
    pair_only_key=True,            # a WC pair plays at most once in the slate; dates skew UTC/US-local
    archived=True,                 # the final was played 2026-07-19: the board is the tournament's record
))
