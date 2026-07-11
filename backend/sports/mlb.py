"""MLB: the first post-World-Cup sport, anchor-first by design.

The WC ledger's own forward-graded verdict (market beat the model on calibration) sets the shape:
NO independent model, no futures bracket, no soccer prop stack. MLB Phase 1 is the confirmed-durable
core only: Kalshi's per-game 2-way moneylines de-vigged into a sharp fair line, favorite cards, and
a market-only prediction ledger locked before first pitch and graded from free ESPN finals. The
capability set is deliberately empty; anything model-flavored arrives later as a forward-graded
variant, if ever. Polymarket game markets (named-outcome parsing) and derivative markets (F5,
totals, NRFI) are Phase 2.

Label reality (probed live): Kalshi yes_sub_titles are city-style ("Los Angeles D", "Chicago WS",
"A's"), ESPN displayNames are City+Nickname ("Los Angeles Dodgers"). The alias map below carries
every Kalshi label to the normalized ESPN name so keys line up across sources. Kalshi event tickers
embed the start time (KXMLBGAME-26JUL121610TORSD), which is what keeps a doubleheader's two games
from merging into one market.
"""
from __future__ import annotations

from . import SportAdapter, register

# normalized Kalshi label -> normalized ESPN displayName (all 30 clubs, from a live label sweep)
_ALIASES = {
    "arizona": "arizona diamondbacks",
    "atlanta": "atlanta braves",
    "baltimore": "baltimore orioles",
    "boston": "boston red sox",
    "chicago c": "chicago cubs",
    "chicago ws": "chicago white sox",
    "cincinnati": "cincinnati reds",
    "cleveland": "cleveland guardians",
    "colorado": "colorado rockies",
    "detroit": "detroit tigers",
    "houston": "houston astros",
    "kansas city": "kansas city royals",
    "los angeles a": "los angeles angels",
    "los angeles d": "los angeles dodgers",
    "miami": "miami marlins",
    "milwaukee": "milwaukee brewers",
    "minnesota": "minnesota twins",
    "new york m": "new york mets",
    "new york y": "new york yankees",
    "philadelphia": "philadelphia phillies",
    "pittsburgh": "pittsburgh pirates",
    "san diego": "san diego padres",
    "san francisco": "san francisco giants",
    "seattle": "seattle mariners",
    "st louis": "st louis cardinals",
    "tampa bay": "tampa bay rays",
    "texas": "texas rangers",
    "toronto": "toronto blue jays",
    "washington": "washington nationals",
    # the A's: Kalshi "A's" normalizes to "as"; ESPN styles them plain "Athletics" in 2026
    "as": "athletics",
    "oakland athletics": "athletics",
    "sacramento athletics": "athletics",
}


def _polymarket_classify(slug: str) -> None:
    return None   # Phase 2: per-game events need named-outcome parsing before they can be ingested


MLB = register(SportAdapter(
    key="mlb",
    display_name="MLB",
    outcomes=("a", "b"),               # no draws: extra innings settle every game
    espn_path="baseball/mlb",
    kalshi_series={
        "KXMLBGAME": ("moneyline", "Matches"),
        # per-LINE 2-way markets (over/under de-vigged within each line, never across lines)
        "KXMLBTOTAL": ("total", "Lines"),
        # first-5-innings winner is a genuine 3-way (team/tie/team): the grouped parser handles it
        "KXMLBF5": ("f5_moneyline", "Lines"),
        # player props, also per-line yes/no; the group slot carries the stat label
        "KXMLBHIT": ("player_prop", "hits"),
        "KXMLBHR": ("player_prop", "home runs"),
        "KXMLBOUTS": ("player_prop", "outs recorded"),
    },
    kalshi_resolved_series="KXMLBGAME",
    kalshi_strip_reg_time=False,
    kalshi_outright_event=None,
    polymarket_search_q="mlb",
    polymarket_pinned_slugs=(),
    polymarket_classify=_polymarket_classify,
    polymarket_game_slug_prefix="mlb-",
    odds_api_sport="baseball_mlb",
    prizepicks_league=2,               # community-reported, unverified (DataDome-blocked); bonus only
    capabilities=frozenset(),          # anchor-only: no model, futures, corners, lineups, smart money
    aliases=_ALIASES,
    results_window_days=10,            # 15 games/day: a tight window keeps the free ESPN sweep small
    pair_only_key=False,               # series + doubleheaders: key on pair + date + start time
))
