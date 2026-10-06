"""NHL: the third sport, built on the MLB template and started as MLB's season ends.

Same shape as MLB, for the same reason: the WC ledger showed the market beats a model on calibration,
so this is anchor-only. Kalshi's per-game 2-way moneylines are de-vigged into a sharp fair line, and a
market-only ledger locks each game ~75 minutes before puck drop and grades it from free ESPN finals.
Phase 1 also locks the main total line. Everything below was probed live before it was written.

Settlement (probed live): KXNHLGAME resolves on the official final, overtime and shootout included,
and ESPN credits a shootout winner with one goal (a Final/SO of 4-3 carries the shootout as a fifth
linescore entry). So the ESPN final never ties, it names the same winner Kalshi settles on, and the
total counts the shootout goal exactly as the totals market does. No special case is needed for either.

Ticker shape: KXNHLGAME-26OCT06CARMTL. Unlike MLB there is no start time in the ticker, so markets
carry a bare date. That is safe because a club never plays twice in one day, so there are no
doubleheaders to tell apart. The pair repeats across a season, though, so keys stay date-qualified.

Liquidity (probed live, Oct 6 2026): on game day every moneyline and every total rung is a cent or two
wide. Further out the moneylines run 2 to 10 cents wide, but two days out most total ladders are nearly
empty (bids near 0.02 and asks near 0.84 on most rungs; some games have one tight rung, some none). So the
main total comes only from tight rungs (aggregator._main_line), and max_lock_spread keeps a moneyline wider
than 10c off the ledger, as in college football. No listed game was wider than 10c when the gate went in;
it is there for the day one is.

Deliberately out of Phase 1, each for a measured reason:
- 1st period winner (KXNHL1P): a 3-way market like MLB's first five, but thin (29c bid / 38c ask) and
  honestly priced near a three-way coin flip, which the placeholder gate correctly rejects as not sharp.
- Puck line (KXNHLSPREAD): spreads are new code and will be built once, properly, for the NFL.
- Player props (goals, points, assists, saves): none were posted when this was built. They get wired
  once their real market structure can be probed, not guessed.
"""
from __future__ import annotations

from . import SportAdapter, register

# normalized Kalshi label -> normalized ESPN displayName (all 32 clubs, from a live label sweep)
_ALIASES = {
    "anaheim": "anaheim ducks",
    "boston": "boston bruins",
    "buffalo": "buffalo sabres",
    "calgary": "calgary flames",
    "carolina": "carolina hurricanes",
    "chicago": "chicago blackhawks",
    "colorado": "colorado avalanche",
    "columbus": "columbus blue jackets",
    "dallas": "dallas stars",
    "detroit": "detroit red wings",
    "edmonton": "edmonton oilers",
    "florida": "florida panthers",
    "los angeles": "los angeles kings",
    "minnesota": "minnesota wild",
    "montreal": "montreal canadiens",
    "nashville": "nashville predators",
    "new jersey": "new jersey devils",
    "new york i": "new york islanders",
    "new york r": "new york rangers",
    "ottawa": "ottawa senators",
    "philadelphia": "philadelphia flyers",
    "pittsburgh": "pittsburgh penguins",
    "san jose": "san jose sharks",
    "seattle": "seattle kraken",
    "st louis": "st louis blues",
    "tampa bay": "tampa bay lightning",
    "toronto": "toronto maple leafs",
    "utah": "utah mammoth",
    "vancouver": "vancouver canucks",
    "vegas": "vegas golden knights",
    "washington": "washington capitals",
    "winnipeg": "winnipeg jets",
    # Utah's previous name, in case an older label or feed still carries it
    "utah hockey club": "utah mammoth",
}


def _polymarket_classify(slug: str) -> None:
    return None   # not ingested: per-game events need named-outcome parsing (same gap as MLB)


NHL = register(SportAdapter(
    key="nhl",
    display_name="NHL",
    site_path="nhl",
    code="NHL",
    outcomes=("a", "b"),               # no draws: overtime and the shootout settle every game
    espn_path="hockey/nhl",
    kalshi_series={
        "KXNHLGAME": ("moneyline", "Matches"),
        # per-LINE 2-way markets (over/under de-vigged within each line, never across lines)
        "KXNHLTOTAL": ("total", "Lines"),
    },
    kalshi_resolved_series="KXNHLGAME",
    kalshi_strip_reg_time=False,
    kalshi_outright_event=None,
    polymarket_search_q="nhl",
    polymarket_pinned_slugs=(),
    polymarket_classify=_polymarket_classify,
    polymarket_game_slug_prefix="nhl-",
    polymarket_series="10346",         # Polymarket's series of this league's games (the top bettors tab)
    odds_api_sport="icehockey_nhl",
    prizepicks_league=8,               # community-reported, unverified (DataDome-blocked); bonus only
    capabilities=frozenset(),          # anchor-only: no model, futures, corners, lineups, smart money
    aliases=_ALIASES,
    results_window_days=10,            # daily slates, like MLB: a tight window keeps the ESPN sweep small
    pair_only_key=False,               # the pair repeats across a season: key on pair + date
    max_lock_spread=0.10,              # a moneyline book wider than 10c is not a sharp line yet
    research="hockey",                 # moneyline factors tracked first (backend/research.py)
))
