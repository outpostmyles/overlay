"""NFL: moneylines, spreads, and the player props the owner actually bets, on the MLB/NHL template.

Anchor-only, like every sport since the WC ledger showed the market beats a model on calibration.
Each game's de-vigged moneyline, main spread, main total, and a full prop sheet lock ~75 minutes
before kickoff and grade off the free ESPN box score. Everything below was probed live first.

Settlement (probed live): KXNFLGAME resolves on the official final; a tie settles at 50c each side.
A 2-way ledger cannot grade a tie, so a tied game is left locked and voided by the window long-stop
(an NFL tie happens zero to two times a season). Tickers carry a bare date (KXNFLGAME-26OCT08TBDAL),
which is safe because a team plays once a week; the pair repeats (division rematches), so keys stay
date-qualified.

Props: one Kalshi series per stat, one yes/no market per line ("Dak Prescott: 175+", strike 174.5),
and each market's game comes from its ticker codes. A player who is active but never takes a snap
settles at the pre-game price, so the grader voids him; once he takes a snap he is graded on his
actual stat, including zero. ESPN lists a player only in groups where he recorded something, so
anyone appearing anywhere in the box score counts as having played (see espn._football_lines).
Anytime TD counts touchdowns a player SCORES, so a quarterback's TD passes are not his touchdowns.

Props are the five the owner bets: passing yards, rushing yards, receiving yards, receptions, and anytime
touchdown. Six more Kalshi series exist (attempts, completions, interceptions, rush+rec yards, passing
TDs, rushing attempts) and were dropped on purpose: when the sheet took the 15 most competitive lines
across all eleven, six of Thursday's fifteen went to stats nobody here bets, and touchdowns barely
appeared because their odds sit far from a coin flip. Now each type gets fixed slots filled by role: the
starting quarterbacks, the lead backs, the featured receivers, and the likeliest scorers.

Spreads (probed live): one market per line per team, "DAL Cowboys wins by over 7.5 points", ticker
...TBDAL-DAL8. Its no side is the opponent +7.5, so each line is a 2-way book. The covering team comes
from the ticker code, since the label's team wording differs from college's.
"""
from __future__ import annotations

from . import SportAdapter, register

# normalized Kalshi label -> normalized ESPN displayName (all 32 clubs, from a live label sweep)
_ALIASES = {
    "arizona": "arizona cardinals",
    "atlanta": "atlanta falcons",
    "baltimore": "baltimore ravens",
    "buffalo": "buffalo bills",
    "carolina": "carolina panthers",
    "chicago": "chicago bears",
    "cincinnati": "cincinnati bengals",
    "cleveland": "cleveland browns",
    "dallas": "dallas cowboys",
    "denver": "denver broncos",
    "detroit": "detroit lions",
    "green bay": "green bay packers",
    "houston": "houston texans",
    "indianapolis": "indianapolis colts",
    "jacksonville": "jacksonville jaguars",
    "kansas city": "kansas city chiefs",
    "las vegas": "las vegas raiders",
    "los angeles c": "los angeles chargers",
    "los angeles r": "los angeles rams",
    "miami": "miami dolphins",
    "minnesota": "minnesota vikings",
    "new england": "new england patriots",
    "new orleans": "new orleans saints",
    "new york g": "new york giants",
    "new york j": "new york jets",
    "philadelphia": "philadelphia eagles",
    "pittsburgh": "pittsburgh steelers",
    "san francisco": "san francisco 49ers",
    "seattle": "seattle seahawks",
    "tampa bay": "tampa bay buccaneers",
    "tennessee": "tennessee titans",
    "washington": "washington commanders",
}


def _polymarket_classify(slug: str) -> None:
    return None   # not ingested: per-game events need named-outcome parsing (same gap as MLB/NHL)


NFL = register(SportAdapter(
    key="nfl",
    display_name="NFL",
    site_path="nfl",
    code="NFL",
    outcomes=("a", "b"),
    espn_path="football/nfl",
    kalshi_series={
        "KXNFLGAME": ("moneyline", "Matches"),      # first: it teaches the ticker team codes
        "KXNFLTOTAL": ("total", "Lines"),
        "KXNFLSPREAD": ("spread", "Lines"),
        # player props: per-line yes/no markets; the group slot carries the stat label the grader uses
        "KXNFLPASSYDS": ("player_prop", "passing yards"),
        "KXNFLRSHYDS": ("player_prop", "rushing yards"),
        "KXNFLRECYDS": ("player_prop", "receiving yards"),
        "KXNFLREC": ("player_prop", "receptions"),
        "KXNFLTD": ("player_prop", "touchdowns"),
    },
    kalshi_resolved_series="KXNFLGAME",
    kalshi_strip_reg_time=False,
    kalshi_outright_event=None,
    polymarket_search_q="nfl",
    polymarket_pinned_slugs=(),
    polymarket_classify=_polymarket_classify,
    polymarket_game_slug_prefix="nfl-",
    polymarket_series="12185",         # Polymarket's series of this league's games (the top bettors tab)
    odds_api_sport="americanfootball_nfl",
    prizepicks_league=9,               # community-reported, unverified (DataDome-blocked); bonus only
    capabilities=frozenset(),          # anchor-only
    aliases=_ALIASES,
    results_window_days=10,            # a Thursday-to-Monday week plus slack
    pair_only_key=False,               # division rivals meet twice: key on pair + date
    # per game: both starting QBs, the lead backs, the featured receivers, and the likeliest scorers
    ledger_prop_quota=(("passing yards", 2), ("rushing yards", 4), ("receiving yards", 6),
                       ("receptions", 6), ("touchdowns", 6)),
    research="football",               # the Research % and its factor study (backend/research.py)
))
