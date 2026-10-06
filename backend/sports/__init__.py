"""Sport adapters: the one place everything sport-specific lives.

Overlay's durable core is sport-agnostic (de-vig math, CLV ledger, the lock-and-grade forecast
lifecycle, caches, heartbeat, frontend shell). What varies by sport is discovery and shape: which
Kalshi series and Polymarket slugs hold the markets, which ESPN league serves results, how many
outcomes a game has, and which capabilities (model, futures bracket, corners...) even apply. A
SportAdapter captures exactly that surface so a new sport is a new adapter module, not a fork.

Phase 0 ships the seam plus the wc26 adapter with a golden-snapshot guarantee: behavior with
SPORT=wc26 (the default) is identical to the pre-seam code. MLB/UFC arrive as pure additions.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class SportAdapter:
    key: str                              # "wc26" | "mlb" | "ufc": namespaces ledger rows + caches
    display_name: str
    outcomes: tuple                       # ("a", "draw", "b") 3-way soccer; ("a", "b") for MLB/UFC
    espn_path: str                        # site.api.espn.com/apis/site/v2/sports/{espn_path}
    kalshi_series: dict                   # series_ticker -> (market_type, group)
    kalshi_resolved_series: str           # the per-game series whose settlements grade moneyline picks
    kalshi_strip_reg_time: bool           # soccer knockout markets wrap picks in "Reg Time ..." wording
    kalshi_outright_event: Optional[str]  # display name for the outright market (None: keep the title)
    polymarket_search_q: str              # public-search query that surfaces this sport's events
    polymarket_pinned_slugs: tuple        # slugs always fetched even if search relevance drops them
    polymarket_classify: Callable         # slug -> market_type | None (which events we actually ingest)
    polymarket_game_slug_prefix: str      # per-game event slugs (smart-money whale reads)
    odds_api_sport: str                   # The Odds API sport key (manual-only feed)
    prizepicks_league: int                # PrizePicks league id for props
    capabilities: frozenset               # {"model","futures","bracket","corners","lineups","smartmoney"}
    aliases: dict                         # sport-specific name aliases, consulted before the global map
    results_window_days: int              # how far back the ESPN results sweep looks (tiny for daily sports)
    pair_only_key: bool                   # merge moneylines on the team pair alone (WC) vs pair+date+time (MLB)
    # Optional knobs (added for football). Each default reproduces the pre-football behavior exactly.
    ledger_props: int = 3                 # prop legs locked per game: one line per player+stat, most competitive first
    team_filter: frozenset = frozenset()  # if set, keep only games that involve at least one of these team keys
    espn_team_field: str = "displayName"  # ESPN team field the keys come from ("location" = the school name)
    espn_scoreboard_params: tuple = ()    # extra ESPN scoreboard params, as (name, value) pairs
    max_lock_spread: float = 1.0          # a game whose Kalshi moneyline book is wider than this never locks
    kickoff_date_slack: int = 0           # days either side to find the game on ESPN (weekly sports only)
    ledger_prop_quota: tuple = ()         # (stat, slots) pairs: per-type prop slots, filled by role, not by odds
    research: str = ""                    # research layer profile ("football"): a graded Research % beside the line
    polymarket_series: str = ""           # Polymarket's sports series id for this league's games (top bettors tab)


_REGISTRY: dict[str, SportAdapter] = {}


def register(adapter: SportAdapter) -> SportAdapter:
    _REGISTRY[adapter.key] = adapter
    return adapter


def get(key: str) -> SportAdapter:
    return _REGISTRY[key]


def keys() -> tuple:
    return tuple(_REGISTRY)


def active() -> SportAdapter:
    """The active sport (config.SPORT, default wc26). Phase 0 runs one sport per process; the
    comma-list multi-sport loop lands with the MLB adapter."""
    from .. import config
    key = config.SPORT.split(",")[0].strip()
    if key not in _REGISTRY:
        raise ValueError(f"unknown SPORT={key!r} (from SPORT={config.SPORT!r}); registered: {keys()}")
    return _REGISTRY[key]


from . import wc26  # noqa: E402,F401  (import registers the adapter)
from . import mlb   # noqa: E402,F401
from . import nhl   # noqa: E402,F401
from . import nfl   # noqa: E402,F401
from . import cfb   # noqa: E402,F401
