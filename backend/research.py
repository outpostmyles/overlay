"""The research layer: what published betting research says about each game, applied on top of the
de-vigged market line (the crowd) as a separate, graded Research percentage.

The rule that decides what may move the number: a factor ADJUSTS the Research % only where studies show
the betting market itself misprices it. Everything else the market already prices (a quarterback ruled
out, home field, rest), so adding it again would count it twice and make the number worse. Those factors
are still shown on the card and graded in the ledger, so the study can say over time whether any of them
deserves weight. Adjustments are about half of the published gap: the evidence is real but was measured
on other markets and years, and the ledger grades Research against the crowd on every game it touches.

  adjust : moves the Research % (favorite bias on moneylines; wind and rain on NFL totals)
  track  : graded in the study with a direction, no adjustment (QB status, rest, night travel, big
           spreads, college weather, passing-yard unders)
  caution: raises the cushion a price must clear (an unsettled QB, no availability report, early season)
           or flags line shopping (a spread next to 3 or 7); never graded, since it carries no direction

Pure functions only: the aggregator gathers the game context (ESPN, Open-Meteo) and calls evaluate().
"""
from __future__ import annotations

import math
from datetime import datetime
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

# --- the evidence behind every factor (shown in the study and on chip tooltips) --------------------- #
SOURCES = {
    "kalshi_flb": "https://mpra.ub.uni-muenchen.de/126350/",
    "nfl_weather": "https://www.sharpfootballanalysis.com/betting/nfl-weather-betting/",
    "qb_value": "https://www.actionnetwork.com/nfl/nfl-playoffs-qb-starters-backups-spreads-most-valuable-2021",
    "rest": "https://arxiv.org/abs/2408.10867v1",
    "circadian": "https://aasm.org/nfl-teams-on-west-coast-may-have-circadian-edge-in-night-games/",
    "key_numbers": "https://www.actionnetwork.com/nfl/nfl-key-betting-numbers-spread-margins-of-victory-line-value",
    "cfb_spreads": "https://www.aeaweb.org/conference/2010/retrieve.php?pdfid=406",
    "availability": "https://www.espn.com/college-football/story/_/id/45968809/big-12-issue-player-availability-reports-first",
    "prop_unders": "https://pff.com/news/nfl-betting-2022-midseason-player-prop-performance-review",
    "leaderboard": "https://polymarket.com/leaderboard",
}

# key -> (name, what the research says and what we do with it, source)
EVIDENCE = {
    "fav_bias": ("Favorite bias",
                 "Favorites at 60%+ have won more often than priced: about +2 points across this ledger's "
                 "settled favorites, and the same pattern across Kalshi (Burgi, Deng & Whelan 2025) and in "
                 "college moneylines (Berkowitz, Depken & Gandar 2017). Research adds about half that gap.",
                 "kalshi_flb"),
    "wind_calm": ("Calm wind",
                  "Outdoor NFL games under 8 mph at kickoff went over 52.3% (2,345 games) against a 48.6% "
                  "outdoor baseline. NFL totals get half that gap.", "nfl_weather"),
    "wind_8_11": ("Wind 8-11 mph",
                  "NFL unders hit 55% at 8-11 mph at kickoff (1,194 games). NFL totals get half that gap.",
                  "nfl_weather"),
    "wind_12_14": ("Wind 12-14 mph",
                   "NFL unders hit 59% at 12-14 mph at kickoff (499 games). NFL totals get half that gap.",
                   "nfl_weather"),
    "wind_15": ("Wind 15+ mph",
                "Books already cut totals about 1.5 points at 15-19 mph and the results are mixed, so there "
                "is no adjustment. Tracked.", "nfl_weather"),
    "rain": ("Rain",
             "NFL unders hit 57.4% with measurable rain at kickoff (343 games). NFL totals get half that gap.",
             "nfl_weather"),
    "pass_weather": ("Passing in wind or rain",
                     "Team passing yards fall about 8% at 12+ mph and 15% in rain. Tracked: do passing "
                     "unders beat the price in bad weather?", "nfl_weather"),
    "pass_under": ("Passing unders",
                   "At sportsbooks, passing-yard unders were the one blanket prop bet that made money from "
                   "2020 to mid-2022. Kalshi's MLB props showed no such lean, so it is tracked here.",
                   "prop_unders"),
    "qb": ("QB on the injury report",
           "A starting QB is worth 1 to 7 points. The market prices the news once it is out, so this is "
           "tracked, not added, and bets wait for a bigger cushion until his status is settled.", "qb_value"),
    "rest": ("Rest edge",
             "A bye was worth about 2 points before the 2011 labor deal and nothing significant since. "
             "Tracked to see whether that holds.", "rest"),
    "night_west": ("Night game, West team",
                   "West Coast teams covered 66% of East-vs-West night games over 40 years (Sleep, 2013), "
                   "with no daytime effect. Old, and probably priced in now. Tracked.", "circadian"),
    "key_number": ("Key number",
                   "About 15% of NFL games end on 3 and 9% on 7, so the half point next to them is worth "
                   "more than any trend. Shop for the better side of it.", "key_numbers"),
    "big_spread": ("Big spread",
                   "College favorites were overpriced against the spread (11,000+ games, 1985-2003), and "
                   "starters sit in blowouts. Tracked: does the underdog cover big numbers?", "cfb_spreads"),
    "no_report": ("No availability report",
                  "Conference availability reports cover conference games only, so a non-conference or "
                  "Notre Dame game can hide an absence. Bets wait for a bigger cushion.", "availability"),
    "early_season": ("Early season",
                     "September lines carry the most roster uncertainty (transfers, new starters). Bets "
                     "wait for a bigger cushion.", "availability"),
    "top_traders": ("Top bettors' side",
                    "The moneyline side holding most of the money from the most profitable sports traders on "
                    "Polymarket (and Kalshi, where they share holdings). Following big traders is unproven: "
                    "a position can be a hedge or a late entry at a worse price, so it is tracked, not added.",
                    "leaderboard"),
}

# Favorite bias: about half of the favorites' measured edge at each price (floor, added probability)
FAV_BIAS = ((0.70, 0.015), (0.60, 0.010))
# NFL total adjustments to P(over): about half the published gap (Sharp Football, Oct 2026)
WEATHER_ADJ = {"wind_calm": 0.010, "wind_8_11": -0.025, "wind_12_14": -0.045, "wind_15": 0.0,
               "rain": -0.035}
WEATHER_CAP = -0.05                       # wind and rain together never take more than 5 points off
RAIN_INCHES = 0.01                        # measurable precipitation in the kickoff hour or the next
# the edge a price has to clear before it counts as value, and the bump per open question (capped)
CUSHION = {"game": 0.02, "prop": 0.04, "bump": 0.02, "max_bump": 0.04}
BIG_SPREAD = 20.0                         # college main spreads this big get the garbage-time track
KEY_NUMBERS = (3, 7)

# NFL home time zones by normalized team key. Arizona keeps standard time, so it sits on Pacific time
# all season and counts as a West team.
_NFL_WEST = {"seattle seahawks", "san francisco 49ers", "los angeles rams", "los angeles chargers",
             "las vegas raiders", "arizona cardinals"}
_NFL_EAST = {"buffalo bills", "miami dolphins", "new england patriots", "new york jets", "baltimore ravens",
             "cincinnati bengals", "cleveland browns", "pittsburgh steelers", "indianapolis colts",
             "jacksonville jaguars", "new york giants", "philadelphia eagles", "washington commanders",
             "detroit lions", "atlanta falcons", "carolina panthers", "tampa bay buccaneers"}


def catalog(sport: str) -> list[dict]:
    """Every factor the research layer knows, with what it does in `sport` (adjusts the Research %,
    tracks it in the study, or only raises the cushion), for the ledger's explainer."""
    adjusts = {"fav_bias"} | ({"wind_calm", "wind_8_11", "wind_12_14", "rain"} if sport == "nfl" else set())
    caution = {"key_number", "no_report", "early_season"}
    nfl_only = {"key_number", "night_west", "qb", "pass_under", "pass_weather"}
    cfb_only = {"big_spread", "no_report", "early_season"}
    out = []
    for key, (name, detail, src) in EVIDENCE.items():
        if (sport == "cfb" and key in nfl_only) or (sport == "nfl" and key in cfb_only):
            continue
        mode = "adjusts" if key in adjusts else "caution" if key in caution else "tracks"
        out.append({"key": key, "name": name, "detail": detail, "source": SOURCES.get(src), "mode": mode})
    return out


def _clamp(p: float) -> float:
    return min(max(p, 0.02), 0.98)


def value_at(p: float | None, cushion: float) -> int | None:
    """The worst American price that still clears `cushion` of expected value at probability p. Anything
    at this price or better (shorter for a favorite, longer for an underdog) is value."""
    if not p or p <= 0 or p >= 1:
        return None
    dec = (1 + cushion) / p
    if dec <= 1:
        return None
    if dec >= 2:
        return math.ceil(round((dec - 1) * 100, 6))        # +141.3 -> +142: +141 falls short
    return -int(round(100 / (dec - 1), 6))                 # -175.7 -> -175: -176 falls short


def american_ev(p: float | None, american: float | None) -> float | None:
    """Expected value per unit staked of an American price at probability p (0.021 = +2.1%)."""
    if p is None or not american:
        return None
    dec = 1 + (american / 100 if american > 0 else 100 / -american)
    return round(p * dec - 1, 4)


def _fav_bias(p: float) -> float:
    for floor, adj in FAV_BIAS:
        if p >= floor:
            return adj
    return 0.0


def _wind_band(mph: float) -> str:
    if mph < 8:
        return "wind_calm"
    if mph < 12:
        return "wind_8_11"
    if mph < 15:
        return "wind_12_14"
    return "wind_15"


def _et_hour(iso: str | None) -> int | None:
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).astimezone(_ET).hour
    except ValueError:
        return None


def _factor(key: str, label: str, kind: str, *, scope: str = "game", target: dict | None = None,
            adj: float = 0.0, p_crowd: float | None = None, p_research: float | None = None,
            note: str = "") -> dict:
    name, detail, src = EVIDENCE[key]
    return {"key": key, "name": name, "label": label, "kind": kind, "scope": scope,
            "detail": detail + (f" {note}" if note else ""), "source": SOURCES.get(src),
            "target": target, "adj": round(adj, 4),
            "p_crowd": None if p_crowd is None else round(p_crowd, 4),
            "p_research": None if p_research is None else round(p_research, 4)}


def _side_prob(leg: dict, side: str) -> float:
    """A two-way leg's probability for `side` (the leg stores its own side's)."""
    return leg["prob"] if leg.get("side") == side else 1 - leg["prob"]


def evaluate(sport: str, ctx: dict | None, market: tuple, legs: list[dict], team_a: str, team_b: str) -> dict:
    """Research view of one game. `market` is the de-vigged (a, draw, b) line; `legs` are the board's legs,
    each annotated in place with `research_prob` (the Research % for the leg's own side), `value_at`, and,
    where a factor touches it, `factors`. `ctx` is the gathered game context (ESPN + weather); with none,
    only the market-only factor (favorite bias) can apply. Returns the research 1X2, the factors, the
    cushions and value-at prices, the book comparison, and the context worth freezing for the study."""
    ctx = ctx or {}
    pa, pd, pb = (float(x or 0.0) for x in market)
    factors: list[dict] = []
    nfl = sport == "nfl"

    # -- moneyline: favorite bias ----------------------------------------------------------------- #
    fav, pf = (team_a, pa) if pa >= pb else (team_b, pb)
    # never more than half the favorite's remaining distance to certainty: a 99% favorite moves a hair
    bias = min(_fav_bias(pf), (1 - pf) / 2) if not pd else 0.0
    ra, rb = pa, pb
    if bias:
        ra, rb = (pa + bias, pb - bias) if fav == team_a else (pa - bias, pb + bias)
        factors.append(_factor("fav_bias", f"Favorite bias +{bias * 100:.1f}", "adjust",
                               target={"kind": "ml", "team": fav}, adj=bias, p_crowd=pf, p_research=pf + bias))
    research = (round(ra, 4), round(pd, 4), round(rb, 4))

    def r_of(team: str) -> float:
        return research[0] if team == team_a else research[2]

    def c_of(team: str) -> float:
        return pa if team == team_a else pb

    # -- weather on the total (NFL adjusts, college tracks first) -------------------------------------- #
    uncertain: list[str] = []
    wx = ctx.get("weather") or {}
    outdoor = ctx.get("indoor") is False
    weather_keys: list[str] = []
    if outdoor and wx.get("wind_mph") is not None:
        weather_keys.append(_wind_band(float(wx["wind_mph"])))
    if outdoor and (wx.get("precip_in") or 0) >= RAIN_INCHES:
        weather_keys.append("rain")
    total_leg = next((l for l in legs if l.get("key") == "total_goals"), None)
    if total_leg is not None:
        p_over = _side_prob(total_leg, "over")
        adj_total = max(sum(WEATHER_ADJ[k] for k in weather_keys), WEATHER_CAP) if nfl else 0.0
        r_over = _clamp(p_over + adj_total) if adj_total else p_over
        total_leg["research_prob"] = round(r_over if total_leg.get("side") == "over" else 1 - r_over, 4)
        for k in weather_keys:
            direction = "over" if WEATHER_ADJ[k] > 0 else "under"
            label = f"Wind {round(float(wx['wind_mph']))} mph" if k.startswith("wind") else "Rain"
            adjusts = nfl and WEATHER_ADJ[k] != 0
            factors.append(_factor(
                k, label, "adjust" if adjusts else "track",
                target={"kind": "leg", "key": "total_goals", "dir": direction},
                adj=abs(WEATHER_ADJ[k]) if adjusts else 0.0,
                p_crowd=p_over if direction == "over" else 1 - p_over,
                p_research=r_over if direction == "over" else 1 - r_over,
                note="" if nfl else "College: tracked first, no adjustment yet."))
        if weather_keys:
            total_leg["factors"] = list(weather_keys)
    elif weather_keys:
        # no total leg to grade against: still show the weather on the card
        for k in weather_keys:
            label = f"Wind {round(float(wx['wind_mph']))} mph" if k.startswith("wind") else "Rain"
            factors.append(_factor(k, label, "track"))

    # -- passing props: the book-history under, and the same under in real wind or rain ----------------- #
    bad_weather = bool({"wind_12_14", "wind_15", "rain"} & set(weather_keys))
    for leg in legs:
        if leg.get("key") != "player_prop" or leg.get("stat") != "passing yards":
            continue
        p_under = _side_prob(leg, "under")
        tgt = {"kind": "leg", "key": "player_prop", "player_key": leg.get("player_key"),
               "stat": "passing yards", "dir": "under"}
        factors.append(_factor("pass_under", f"{leg.get('player', '')} under".strip(), "track", scope="leg",
                               target=tgt, p_crowd=p_under, p_research=p_under))
        tags = ["pass_under"]
        if bad_weather:
            factors.append(_factor("pass_weather", f"{leg.get('player', '')} under".strip(), "track",
                                   scope="leg", target=tgt, p_crowd=p_under, p_research=p_under))
            tags.append("pass_weather")
        leg["factors"] = tags

    # -- quarterbacks (NFL): the team's passing leader on the injury report ------------------------------ #
    for team in (team_a, team_b):
        qb = (ctx.get("qb") or {}).get(team)
        if not qb:
            continue
        opp = team_b if team == team_a else team_a
        status = (qb.get("status") or "").lower()
        factors.append(_factor("qb", f"QB {qb.get('name', '')}: {status}", "track",
                               target={"kind": "ml", "team": opp}, p_crowd=c_of(opp), p_research=r_of(opp)))
        if status in ("questionable", "doubtful"):   # "out" is settled news the market has priced
            uncertain.append("qb")

    # -- the top bettors' moneyline side (Polymarket + opted-in Kalshi leaders), when it is clear --------- #
    top = ctx.get("top") or {}
    if top.get("team") in (team_a, team_b):
        t, money = top["team"], top.get("stake") or 0
        cash = f"${money / 1000:.1f}k" if money >= 1000 else f"${money:.0f}"
        name = " ".join(w[:1].upper() + w[1:] for w in t.split())     # "49ers" stays "49ers"
        factors.append(_factor("top_traders", f"Top bettors: {name} {cash}", "track",
                               target={"kind": "ml", "team": t}, p_crowd=c_of(t), p_research=r_of(t),
                               note=f"{top.get('traders', 0)} of the leaders, {round((top.get('share') or 0) * 100)}% "
                                    f"of their moneyline money on this game."))

    # -- rest: a 3+ day gap in days since each team last played ------------------------------------------ #
    rest = ctx.get("rest") or {}
    da, db = rest.get(team_a), rest.get(team_b)
    if da is not None and db is not None and abs(da - db) >= 3:
        rested = team_a if da > db else team_b
        label = f"Rest +{abs(da - db)}d" + (" (bye)" if max(da, db) >= 11 else "")
        factors.append(_factor("rest", label, "track", target={"kind": "ml", "team": rested},
                               p_crowd=c_of(rested), p_research=r_of(rested)))

    # -- night game, a West team against an East team (NFL) --------------------------------------------- #
    hour = _et_hour(ctx.get("kickoff_iso"))
    if nfl and hour is not None and (hour >= 20 or hour < 4):
        west = [t for t in (team_a, team_b) if t in _NFL_WEST]
        east = [t for t in (team_a, team_b) if t in _NFL_EAST]
        if len(west) == 1 and len(east) == 1:
            factors.append(_factor("night_west", "Night game: West team", "track",
                                   target={"kind": "ml", "team": west[0]},
                                   p_crowd=c_of(west[0]), p_research=r_of(west[0])))

    # -- spreads: key numbers (NFL) and big college numbers --------------------------------------------- #
    spread_leg = next((l for l in legs if l.get("key") == "spread"), None)
    if spread_leg is not None and spread_leg.get("line") is not None:
        line = float(spread_leg["line"])
        near = next((k for k in KEY_NUMBERS if abs(line - k) == 0.5), None)
        if nfl and near:
            factors.append(_factor("key_number", f"Spread next to {near}", "caution"))
        if not nfl and line >= BIG_SPREAD:
            p_dog = 1 - _side_prob(spread_leg, "cover")
            factors.append(_factor("big_spread", f"Big spread {line:g}", "track",
                                   target={"kind": "leg", "key": "spread", "dir": "dog"},
                                   p_crowd=p_dog, p_research=p_dog))
            spread_leg["factors"] = ["big_spread"]

    # -- college information gaps -------------------------------------------------------------------- #
    if sport == "cfb":
        if ctx.get("conference_game") is False:
            factors.append(_factor("no_report", "No availability report", "caution"))
            uncertain.append("no_report")
        week = ctx.get("week")
        if week is not None and week <= 4:
            factors.append(_factor("early_season", f"Week {week}", "caution"))
            uncertain.append("early_season")

    # -- cushions and the price each pick needs ---------------------------------------------------------- #
    bump = min(CUSHION["bump"] * len(set(uncertain)), CUSHION["max_bump"])
    game_cushion, prop_cushion = CUSHION["game"] + bump, CUSHION["prop"] + bump
    for leg in legs:
        leg.setdefault("research_prob", leg.get("prob"))   # no factor moved it: Research = the crowd
        leg["value_at"] = value_at(leg.get("research_prob"),
                                   prop_cushion if leg.get("key") == "player_prop" else game_cushion)
    value = {team_a: value_at(research[0], game_cushion), team_b: value_at(research[2], game_cushion)}

    # -- a real book's price (DraftKings via ESPN), scored against Research ------------------------------ #
    dk = ctx.get("dk") or {}
    dk_out = None
    if dk.get("ml"):
        dk_out = {"book": dk.get("book") or "DraftKings", "ml": {}, "ev": {}}
        for team, price in dk["ml"].items():
            if team in (team_a, team_b) and price:
                dk_out["ml"][team] = price
                dk_out["ev"][team] = american_ev(r_of(team), price)
        sp = dk.get("spread") or {}
        if spread_leg is not None and sp.get("team") == spread_leg.get("team") \
                and sp.get("line") == spread_leg.get("line"):
            p_cover = _side_prob({**spread_leg, "prob": spread_leg["research_prob"]}, "cover")
            dk_out["spread"] = {"team": sp["team"], "line": sp["line"], "cover": sp.get("cover"),
                                "dog": sp.get("dog"), "ev_cover": american_ev(p_cover, sp.get("cover")),
                                "ev_dog": american_ev(1 - p_cover, sp.get("dog"))}
        tot = dk.get("total") or {}
        if total_leg is not None and tot.get("line") == total_leg.get("line"):
            p_over = _side_prob({**total_leg, "prob": total_leg["research_prob"]}, "over")
            dk_out["total"] = {"line": tot["line"], "over": tot.get("over"), "under": tot.get("under"),
                               "ev_over": american_ev(p_over, tot.get("over")),
                               "ev_under": american_ev(1 - p_over, tot.get("under"))}

    context = {k: ctx.get(k) for k in ("venue", "indoor", "neutral", "conference_game", "week")
               if ctx.get(k) is not None}
    if wx:
        context["weather"] = {k: wx.get(k) for k in ("wind_mph", "gust_mph", "precip_in", "temp_f")}
    if rest:
        context["rest"] = {k: v for k, v in rest.items() if k in (team_a, team_b)}
    if ctx.get("qb"):
        context["qb"] = {k: v for k, v in ctx["qb"].items() if k in (team_a, team_b)}
    return {"probs": research, "ml": {team_a: research[0], team_b: research[2]},
            "factors": factors, "uncertain": sorted(set(uncertain)),
            "cushion": {"game": round(game_cushion, 3), "prop": round(prop_cushion, 3)},
            "value_at": value, "dk": dk_out, "context": context}


def target_hit(target: dict | None, row: dict, legs: list[dict]) -> int | None:
    """Did a factor's directional claim come true on a settled game? 1 or 0, or None when it cannot be
    graded (a push, a void prop, a leg that never graded)."""
    if not target:
        return None
    if target.get("kind") == "ml":
        out = row.get("actual_outcome")
        if out not in ("a", "b"):
            return None
        winner = row.get("team_a") if out == "a" else row.get("team_b")
        return 1 if winner == target.get("team") else 0
    key = target.get("key")
    leg = next((l for l in legs or [] if l.get("key") == key
                and (key != "player_prop" or (l.get("player_key") == target.get("player_key")
                                              and l.get("stat") == target.get("stat")))), None)
    if not leg or leg.get("result") not in ("won", "lost") or leg.get("actual") is None:
        return None
    actual, line = leg["actual"], leg.get("line")
    if line is None or actual == line:
        return None
    if key == "spread":
        return 1 if actual < line else 0          # the covering team's margin fell short: the dog covered
    under = actual < line
    return 1 if (under if target.get("dir") == "under" else not under) else 0
