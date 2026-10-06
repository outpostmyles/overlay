"""College football: moneylines for the Power 4 and Notre Dame, and nothing else, by design.

Scope is the owner's: the SEC, Big Ten, Big 12 and ACC, plus Notre Dame. A game is in scope when EITHER
team is, so Notre Dame against Navy and a Power 4 team's non-conference games stay in. Membership
comes from ESPN's own 2026 conference standings, not memory, because realignment keeps moving it.

The scope also fixes the two problems the full Kalshi board has. Liquidity: across all 215 listed games
the median moneyline spread was 20c, which is noise, not a sharp line; across the in-scope games in
game week it was 1c, with every one at 3c or tighter. Next week's games are listed early and thin, so
max_lock_spread keeps any game wider than 10c off the ledger until its book tightens. Names: teams are
keyed on ESPN's SCHOOL name ("Ohio State"), not the full name ("Ohio State Buckeyes"), because Kalshi
labels schools and a Power 4 team's opponent changes weekly. Against all 262 labels Kalshi listed, 197
match ESPN's school name as-is, 51 more match once a trailing "St." reads "State", and the 13 left are
mapped explicitly below (UT Rio Grande Valley has no ESPN entry yet).

Tickers carry a bare date (KXNCAAFGAME-26OCT17NDBYU); a team plays once a week. Each game also locks its
main spread line ("Western Kentucky wins by over 3.5 points", graded off the final margin). No player
props: Kalshi lists none at game level for college football, and the owner can't bet them there anyway.
"""
from __future__ import annotations

from . import SportAdapter, register

# normalized Kalshi label -> normalized ESPN school name, where the two differ beyond "St." -> "State"
_EXPLICIT = {
    "miami fl": "miami",
    "appalachian st": "app state",
    "central connecticut st": "central connecticut",
    "grambling st": "grambling",
    "liu": "long island university",
    "louisiana monroe": "ul monroe",
    "nicholls st": "nicholls",
    "penn": "pennsylvania",
    "southeastern louisiana": "se louisiana",
    "southern university": "southern",
    "tennessee martin": "ut martin",
    "umass": "massachusetts",
    "university at albany": "ualbany",
}


class _SchoolNames(dict):
    """The alias map, plus one rule: Kalshi abbreviates "State" to "St." ("Ohio St."), ESPN does not.
    normalize_team only ever calls .get(), so the rule lives there. Explicit entries win, which keeps
    "Appalachian St." on ESPN's "App State" instead of the rule's "Appalachian State"."""

    def get(self, key, default=None):
        hit = dict.get(self, key)
        if hit is not None:
            return hit
        if isinstance(key, str) and key.endswith(" st"):
            return key[:-3] + " state"
        return default


# ESPN school names (normalized) of the in-scope teams, from ESPN's 2026 conference standings
POWER4_ND = frozenset({
    # SEC (16)
    "alabama", "arkansas", "auburn", "florida", "georgia", "kentucky", "lsu", "mississippi state",
    "missouri", "oklahoma", "ole miss", "south carolina", "tennessee", "texas", "texas a and m",
    "vanderbilt",
    # Big Ten (18)
    "illinois", "indiana", "iowa", "maryland", "michigan", "michigan state", "minnesota", "nebraska",
    "northwestern", "ohio state", "oregon", "penn state", "purdue", "rutgers", "ucla", "usc",
    "washington", "wisconsin",
    # Big 12 (16)
    "arizona", "arizona state", "baylor", "byu", "cincinnati", "colorado", "houston", "iowa state",
    "kansas", "kansas state", "oklahoma state", "tcu", "texas tech", "ucf", "utah", "west virginia",
    # ACC (17)
    "boston college", "california", "clemson", "duke", "florida state", "georgia tech", "louisville",
    "miami", "nc state", "north carolina", "pittsburgh", "smu", "stanford", "syracuse", "virginia",
    "virginia tech", "wake forest",
    # Notre Dame (1)
    "notre dame",
})


def _polymarket_classify(slug: str) -> None:
    return None


CFB = register(SportAdapter(
    key="cfb",
    display_name="College Football",
    outcomes=("a", "b"),
    espn_path="football/college-football",
    kalshi_series={
        "KXNCAAFGAME": ("moneyline", "Matches"),
        "KXNCAAFSPREAD": ("spread", "Lines"),         # the owner bets the spread sometimes
        "KXNCAAFTOTAL": ("total", "Lines"),
    },
    kalshi_resolved_series="KXNCAAFGAME",
    kalshi_strip_reg_time=False,
    kalshi_outright_event=None,
    polymarket_search_q="college football",
    polymarket_pinned_slugs=(),
    polymarket_classify=_polymarket_classify,
    polymarket_game_slug_prefix="cfb-",
    polymarket_series="12756",         # Polymarket's series of this league's games (the top bettors tab)
    odds_api_sport="americanfootball_ncaaf",
    prizepicks_league=15,              # community-reported, unverified (DataDome-blocked); bonus only
    capabilities=frozenset(),
    aliases=_SchoolNames(_EXPLICIT),
    results_window_days=10,
    pair_only_key=False,
    team_filter=POWER4_ND,
    espn_team_field="location",
    espn_scoreboard_params=(("groups", "80"), ("limit", "300")),   # every FBS game, not a default view
    max_lock_spread=0.10,
    kickoff_date_slack=1,              # Kalshi dated four Oct 17 games "Oct 16" (listed before kickoff was set)
    research="football",               # the Research % and its factor study (backend/research.py)
))
