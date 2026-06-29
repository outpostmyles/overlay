"""Central configuration. Loads .env (no external dependency) and exposes constants."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"
DB_PATH = ROOT / "poly.db"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip())


_load_dotenv(ROOT / ".env")

# --- API endpoints (prediction markets are free / no auth for read-only data) ---
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB = "https://clob.polymarket.com"
POLYMARKET_DATA = "https://data-api.polymarket.com"   # public, no key: /holders, /trades
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
ODDS_API = "https://api.the-odds-api.com/v4"

# --- The Odds API (optional). Set ODDS_API_KEY in .env to enable sportsbook lines. ---
# FREE TIER = 500 credits/month. Credit cost = (#markets) x (#regions) per call.
# We request ONLY h2h moneyline in the us region = 1 credit per refresh, and fetch it
# manually (never on the auto-refresh loop) so credits are never burned silently.
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()
ODDS_API_SPORT = "soccer_fifa_world_cup"
ODDS_API_REGIONS = "us"
ODDS_MARKETS = "h2h"                 # moneyline only -> 1 credit/refresh
ODDS_MIN_REFRESH_INTERVAL = 30       # h2h debounce: ignore manual re-fetch within 30s (1 credit)
ODDS_CREDIT_FLOOR = 5                 # stop ALL Odds spend below this many credits left → serve cache
ODDS_CACHE_PATH = ROOT / "poly_odds_cache.json"  # persist odds + remaining credits
# Corners are an event-level market => 1 credit PER GAME (pricier than h2h). They ride the normal
# Odds refresh, but gated to the near slate (today + N days) + persisted, to bound the credit spend.
CORNER_SLATE_DAYS = 1                 # pull corner lines for today + this many days out
CORNER_MAX_GAMES = 8                  # hard cap on games per refresh (~8 credits worst case)
CORNER_REFRESH_INTERVAL = 6 * 3600   # corners re-pull at most this often (decoupled from h2h's 30s so
                                     # repeated Odds clicks don't re-spend ~8 credits each)
CORNER_EDGE_MIN = 0.04               # min EV vs the de-vigged book line to surface/log a corner bet

# --- Engine defaults ---
# --- PrizePicks (free, public projections API) ---
PRIZEPICKS_URL = "https://api.prizepicks.com/projections"
PRIZEPICKS_LEAGUE = 241                      # World Cup
PRIZEPICKS_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                 "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# --- Anthropic API (optional). Set ANTHROPIC_API_KEY in .env to enable AI pick rationales. ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001").strip()

# --- Futures: Monte Carlo tournament simulation (second opinion vs the Polymarket futures line) ---
TOURNAMENT_SIMS = 6000                        # bracket simulations per run (cached + threaded)
TOURNAMENT_SEED = 20260611                    # fixed seed: reproducible reach-stage % across refreshes
                                              # (kills the +-1-2pp Monte Carlo jitter); not an accuracy claim
TOURNAMENT_BASE_GOALS = 1.35                  # fallback expected goals when the model can't price a pair
FUTURES_CACHE_TTL = 600                       # recompute the sim at most every 10 min
GROUPS_CACHE_PATH = ROOT / "poly_groups_cache.json"   # accumulated group comps from the group-winner futures

# --- Pick engine (scoped to the user's archetypes) ---
FAVORITE_MIN_PROB = 0.55                     # a "clear favorite" for moneyline picks
TEAM_TOTAL_LINE = 1.5
SLATE_HORIZON_DAYS = 4                        # only today + next N days (daily bettor focus)
CHALK_PROB = 0.80                            # >= this fair % = "obvious chalk" (de-emphasized)
PROPREAD_CACHE_PATH = ROOT / "poly_propread_cache.json"
FUTURESREAD_CACHE_PATH = ROOT / "poly_futuresread_cache.json"   # on-demand AI read per futures row
LEANS_PATH = ROOT / "poly_futures_leans.json"                   # user-logged futures leans (CLV-tracked)
ENABLE_MEMORY = True                         # calibration: learn from the paper ledger (gated, safe)

# --- Model Ledger: pre-kickoff forecast + calibration (model vs market, graded on the result) ---
# A frozen-before-kickoff 1X2 forecast for each upcoming game, with the de-vigged market line frozen at
# the SAME instant as the benchmark, then auto-graded against the ESPN result. Forward-only (never
# backfilled: the live model has ingested played results, so a backfill would be hindsight).
FORECAST_ENABLED = True
FORECAST_LOCK_BUFFER_MINUTES = 75            # lock at kickoff - 75 min, BEFORE ESPN posts the XI (~60 min
                                             # out) so a forecast can never absorb team news
FORECAST_MIN_N = 8                           # hide aggregate Brier/skill until this many settle (small KO
                                             # sample); raw rows always show, with a wide-CI caveat
# Lines for the per-game prediction sheet (the extra markets the model simulates alongside the 1X2).
FORECAST_TOTAL_LINE = 2.5                     # total match goals over/under
FORECAST_TEAM_LINE = 1.5                      # each team's goals over/under
FORECAST_CORNERS_LINE = 9.5                   # total corners over/under (graded only with an API-Football key)

# --- Server-side refresh heartbeat (for an always-on deployment) ---
# When the app runs headless on a server (no browser polling /api/snapshot), this in-process tick keeps
# data fresh so the Model Ledger locks forecasts before kickoff and settles finished games on its own. It
# runs the FREE-feed path only (force refresh, never refresh_odds or reason), so it cannot spend Odds
# credits or Anthropic tokens, and it is gentler than the 60s browser poll. Off by default; set
# HEARTBEAT_ENABLED=true in .env on the deployed host. See DEPLOY.md.
HEARTBEAT_ENABLED = os.getenv("HEARTBEAT_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "300") or 300)

# --- AI reasoning (manual-trigger, disk-cached — never on the auto-refresh) ---
REASONING_MAX_MATCHES = 10                   # cap matches reasoned per run (cost control)
REASONING_CACHE_PATH = ROOT / "poly_reasoning_cache.json"
REASONING_WEB_SEARCH = True                  # ground each verdict in a live web-search brief
WEB_SEARCH_TOOL = "web_search_20260209"
WEB_SEARCH_MAX_USES = 2                       # cap searches/match: 1 lineups+injuries, 1 form+situation

DEVIG_METHOD = "power"                      # "power" | "multiplicative"
SHARP_SOURCES = ("polymarket", "kalshi")    # used to build the "fair"/true line
MIN_EV = 0.02                               # surface +EV bets at/above 2%
MAX_EV = 0.50                               # above this it's stale/illiquid noise, not edge
MIN_FAIR_PROB = 0.02                        # ignore deep longshots (favorite-longshot noise)
BANKROLL = float(os.getenv("BANKROLL", "1000") or 1000)
KELLY_FRACTION = 0.25                        # quarter Kelly (variance control)
# Disciplined unit staking for the user's archetype bets (favorites/props aren't always +EV vs the
# book, so we size by CONVICTION in units of bankroll rather than pure Kelly). 1 unit = UNIT_PCT of
# bankroll; recommended stake scales with confidence and is hard-capped at MAX_UNITS.
UNIT_PCT = float(os.getenv("UNIT_PCT", "0.01") or 0.01)   # 1 unit = 1% of bankroll
MAX_UNITS = 3.0                              # never risk more than 3 units (3%) on one play
CACHE_TTL_SECONDS = 60                        # free feed (Polymarket/Kalshi/PrizePicks) cache this long
PROPS_CACHE_PATH = ROOT / "poly_props_cache.json"   # last-good PrizePicks board, so a restart during a
PROPS_MAX_STALE_DAYS = 2                       # Cloudflare/DataDome block still serves props (up to N days old)
LINEUP_CACHE_TTL = 300                        # confirmed-XI cache (5 min)
RESULTS_CACHE_TTL = 600                       # ESPN finished-game results cache (10 min)
RESOLVED_CACHE_TTL = 600                      # Kalshi resolved-outcome cache (10 min)
ESPN_CACHE_PATH = ROOT / "poly_espn_cache.json"   # memoize FINISHED games (terminal) so we don't re-summarize

# --- Smart money (Polymarket per-game whale positions + flow, free public data API) ---
SMARTMONEY_CACHE_TTL = 600                     # cache 10 min (free data; whale positions move slowly)

# --- API-Football (optional; per-player match stats to auto-grade shots/SOT/passes props) ---
# Free tier = 100 requests/day. We cache a finished game's player stats FOREVER (one fetch per game,
# ever) and hard-cap daily usage well under the limit. Set APIFOOTBALL_KEY in .env to enable.
APIFOOTBALL_KEY = os.getenv("APIFOOTBALL_KEY", "").strip()
APIFOOTBALL_BASE = "https://v3.football.api-sports.io"
APIFOOTBALL_DAILY_CAP = 90                       # leave headroom under the 100/day free limit
APIFOOTBALL_EMPTY_COOLDOWN = 1800                # don't re-fetch a fixture whose stats aren't posted yet
                                                 # more than once per this many seconds (anti-hammer)
APIFOOTBALL_CACHE_PATH = ROOT / "poly_apifootball_cache.json"


def has_apifootball() -> bool:
    return bool(APIFOOTBALL_KEY)


# --- Settlement windows ---
RESULTS_WINDOW_DAYS = 40       # cover the whole tournament: grading parlays/props AND letting the
                               # futures tab rebuild all 12 group comps from finished games deep into
                               # the knockouts (early group games must not age out). ESPN is free/uncapped.
UNGRADABLE_VOID_DAYS = 3       # void (stake-neutral) un-auto-gradable props (shots/passes) this many
                               # days after their game finished, so Awaiting stops accumulating

# --- Estimated fees, for net pricing on prediction markets ---
KALSHI_FEE_COEF = 0.07          # fee ~= coef * price * (1 - price) per contract
POLYMARKET_SPORTS_FEE = 0.03    # taker fee coefficient on sports markets


def has_sportsbooks() -> bool:
    return bool(ODDS_API_KEY)


def has_anthropic() -> bool:
    return bool(ANTHROPIC_API_KEY)
