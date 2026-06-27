"""Orchestration: fetch sources, merge the same market across sources, compute boards.

Two feeds with very different cost profiles:
  • FREE  — Polymarket + Kalshi. Cached CACHE_TTL_SECONDS, refreshed on the 60s auto-loop.
  • METERED — The Odds API (500 credits/month). Fetched ONLY on an explicit manual refresh,
    debounced, and persisted to disk so a server restart never re-spends a credit.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import date, timedelta

import httpx

from . import config, memory, picks, reasoning, smartmoney
from .engine import edges, odds_math
from .matching import moneyline_key, normalize_team
from .model import corners, ratings, tournament
from .models import Market, Quote, Selection
from .sources import apifootball, espn, kalshi, polymarket, prizepicks, theoddsapi
from .store import paper

_free_cache: dict = {"markets": [], "props": [], "ts": 0.0, "loaded": False}
_sm_cache: dict = {"data": {}, "ts": 0.0}
_resolved_cache: dict = {"data": [], "ts": 0.0}
_lineup_cache: dict = {"data": {}, "ts": 0.0}
_results_cache: dict = {"data": [], "ts": 0.0}
_odds_state: dict = {"markets": [], "fetched_at": 0.0, "credits_remaining": None,
                     "ok": False, "loaded": False}
# corner total lines (The Odds API, pricey/manual) — kept teams-keyed, persisted in the odds cache file
_corner_state: dict = {"lines": {}, "fetched_at": 0.0}
# futures: Monte Carlo tournament sim vs de-vigged Polymarket futures (expensive → cached + threaded)
_futures_cache: dict = {"data": {"rows": [], "groups_covered": 0, "sims": 0}, "ts": 0.0}


# --------------------------------------------------------------------------- #
# Free feed (Polymarket + Kalshi) — cached, auto-refreshed
# --------------------------------------------------------------------------- #
async def _fetch_free() -> tuple[list[Market], list[dict]]:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        pm, ks, pp = await asyncio.gather(
            polymarket.fetch(client), kalshi.fetch(client), prizepicks.fetch(),
            return_exceptions=True,
        )
    markets: list[Market] = []
    for name, res in (("polymarket", pm), ("kalshi", ks)):
        if isinstance(res, Exception):
            print(f"[aggregator] {name} errored: {res}")
            continue
        markets.extend(res)
    props = [] if isinstance(pp, Exception) else pp
    if isinstance(pp, Exception):
        print(f"[aggregator] prizepicks errored: {pp}")
    print(f"[aggregator] free: {len(markets)} markets, {len(props)} props")
    return markets, props


def _load_props_disk() -> list[dict]:
    """Last-good PrizePicks board persisted from a prior run, so a restart that lands during a
    Cloudflare/DataDome block still serves props instead of a blank board. Ignored if too stale."""
    p = config.PROPS_CACHE_PATH
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text())
        age_days = (time.time() - (d.get("saved_at") or 0)) / 86400.0
        if isinstance(d.get("props"), list) and age_days <= config.PROPS_MAX_STALE_DAYS:
            print(f"[aggregator] seeded {len(d['props'])} last-good props from disk ({age_days:.1f}d old)")
            return d["props"]
    except Exception as exc:  # noqa: BLE001
        print(f"[aggregator] props cache load failed: {exc}")
    return []


def _save_props_disk(props: list[dict]) -> None:
    if not props:
        return
    try:
        config.PROPS_CACHE_PATH.write_text(json.dumps({"saved_at": time.time(), "props": props}))
    except Exception as exc:  # noqa: BLE001
        print(f"[aggregator] props cache save failed: {exc}")


async def get_free(force: bool = False) -> tuple[list[Market], list[dict]]:
    if not _free_cache["loaded"]:
        _free_cache["props"] = _load_props_disk()   # survive a restart during a PrizePicks block
        _free_cache["loaded"] = True
    if (not force and _free_cache["ts"]
            and (time.monotonic() - _free_cache["ts"]) < config.CACHE_TTL_SECONDS):
        return _free_cache["markets"], _free_cache["props"]
    markets, props = await _fetch_free()
    # keep last-good props if this pull came back empty (PrizePicks throttles intermittently) — don't
    # blank the whole prop board + SGP on a transient miss. A fresh board persists to disk so the
    # last-good survives a process restart too.
    if not props and _free_cache["props"]:
        print("[aggregator] props empty this pull — keeping last-good prop board")
        props = _free_cache["props"]
    elif props:
        _save_props_disk(props)
    _free_cache.update(markets=markets, props=props, ts=time.monotonic())
    return markets, props


async def get_smart_money(matchups: list[dict], force: bool = False) -> dict:
    """Polymarket whale positions + flow on the slate's match markets — free, cached 5 min.
    Keyed by team_key; each signal carries its match/opponent/side context."""
    if not matchups:
        return {}
    if (not force and _sm_cache["ts"]
            and (time.monotonic() - _sm_cache["ts"]) < config.SMARTMONEY_CACHE_TTL):
        return _sm_cache["data"]
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            data = await smartmoney.fetch(client, matchups)
        except Exception as exc:  # noqa: BLE001
            print(f"[aggregator] smartmoney errored: {exc}")
            data = _sm_cache["data"]
    _sm_cache.update(data=data, ts=time.monotonic())
    return data


async def get_lineups(matchups: list[dict], force: bool = False) -> dict:
    """Confirmed starting XIs for today's games (ESPN, free) — cached 5 min. Empty until ~1h pre-KO."""
    if not matchups:
        return {}
    if (not force and _lineup_cache["ts"]
            and (time.monotonic() - _lineup_cache["ts"]) < config.LINEUP_CACHE_TTL):
        return _lineup_cache["data"]
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            data = await espn.fetch_lineups(client, matchups)
        except Exception as exc:  # noqa: BLE001
            print(f"[aggregator] lineups errored: {exc}")
            data = _lineup_cache["data"]
    _lineup_cache.update(data=data, ts=time.monotonic())
    return data


def _enrich_bundles_lineup(bundles: list[dict], lineups: dict) -> None:
    """Attach the OFFICIAL confirmed XI (when posted) to each bundle so the AI treats who's starting
    as ground truth — a benched star kills his goalscorer/shots props; a confirmed start is a green
    light. Absent = not posted yet (rely on the research brief's projected lineup)."""
    if not lineups:
        return
    for b in bundles:
        fav = lineups.get(normalize_team(b.get("favorite") or ""))
        dog = lineups.get(normalize_team(b.get("underdog") or ""))
        if fav or dog:
            b["confirmed_lineup"] = {"favorite": fav, "underdog": dog}


async def get_player_stats() -> dict:
    """API-Football per-player stats for finished games with ungraded shots/SOT/passes picks (free
    tier, budget-capped + cached inside the adapter). No key → {} (those props stay manual)."""
    if not config.has_apifootball():
        return {}
    games = paper.pending_player_prop_games()
    if not games:
        return {}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            return await apifootball.fetch_player_stats(client, games)
        except Exception as exc:  # noqa: BLE001
            print(f"[aggregator] player stats errored: {exc}")
            return {}


async def get_results(force: bool = False) -> list[dict]:
    """ESPN finished-game results (scorers + goals) for settling parlay legs — free, cached 10 min."""
    if (not force and _results_cache["ts"]
            and (time.monotonic() - _results_cache["ts"]) < config.RESULTS_CACHE_TTL):
        return _results_cache["data"]
    dates = [(date.today() - timedelta(days=i)).strftime("%Y%m%d")
             for i in range(config.RESULTS_WINDOW_DAYS)]
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            data = await espn.fetch_results(client, dates)
        except Exception as exc:  # noqa: BLE001
            print(f"[aggregator] results fetch errored: {exc}")
            data = _results_cache["data"]
    _results_cache.update(data=data, ts=time.monotonic())
    return data


def _parlay_row(sgp: dict | None) -> dict | None:
    """Turn the suggested same-game parlay into a priced paper entry (payout = the 'price', joint
    prob = the 'fair'), so it settles + moves the bankroll like any other graded bet."""
    if not sgp or not sgp.get("pricing"):
        return None
    pr = sgp["pricing"]
    today = time.strftime("%Y-%m-%d")
    sel = " + ".join(l["selection"] for l in sgp["legs"])
    return {
        "match": sgp["event"], "archetype": "parlay", "selection": sel[:120],
        "commence_time": sgp.get("commence_time"),
        "pick_fair_prob": pr["joint_prob"], "pick_price_decimal": pr["payout"],
        "dedup_key": f"{today}:parlay:{sgp['event']}",
        "stake_units": sgp.get("stake_units", 1.0),
        "legs_json": json.dumps(sgp["legs"]),
    }


async def get_resolved(force: bool = False) -> list[dict]:
    """Settled Kalshi match outcomes for auto-grading paper picks — free, cached 10 min."""
    if (not force and _resolved_cache["ts"]
            and (time.monotonic() - _resolved_cache["ts"]) < config.RESOLVED_CACHE_TTL):
        return _resolved_cache["data"]
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            data = await kalshi.fetch_resolved(client)
        except Exception as exc:  # noqa: BLE001
            print(f"[aggregator] resolved fetch errored: {exc}")
            data = _resolved_cache["data"]
    _resolved_cache.update(data=data, ts=time.monotonic())
    return data


_kickoff_cache: dict = {"map": {}, "ts": 0.0, "dates": set()}
_VS_RE = re.compile(r"\s+vs\.?\s+", re.IGNORECASE)


async def get_kickoffs(dates: list[str]) -> dict:
    """{frozenset(team_keys): kickoff_iso} for the given YYYYMMDD dates (ESPN scoreboard, free, cached).
    Our markets only know the game DATE (Kalshi), so this supplies the real kickoff TIME used to order
    same-day picks. Refetched when stale or when a not-yet-cached date is requested."""
    need = {d for d in dates if d}
    now = time.monotonic()
    stale = (now - _kickoff_cache["ts"]) > config.RESULTS_CACHE_TTL
    if need and (stale or not need.issubset(_kickoff_cache["dates"])):
        async with httpx.AsyncClient(follow_redirects=True) as client:
            try:
                fresh = await espn.fetch_kickoffs(client, sorted(need))
            except Exception as exc:  # noqa: BLE001
                print(f"[aggregator] kickoffs errored: {exc}")
                fresh = {}
        if stale:
            _kickoff_cache["map"], _kickoff_cache["dates"] = {}, set()   # drop expired entries
        _kickoff_cache["map"].update(fresh)
        _kickoff_cache["ts"] = now
        covered = {iso[:10].replace("-", "") for iso in fresh.values() if iso}
        _kickoff_cache["dates"] |= (need & covered)   # only mark a date done once ESPN actually returned it
    return _kickoff_cache["map"]


async def attach_kickoffs(picks: list[dict]) -> None:
    """Stamp each ledger pick with a `kickoff` (real ISO time when ESPN has it, else the stored date at
    end-of-day) so the Track Record can order same-day games by who actually plays first."""
    dates = {(p.get("commence_time") or "").replace("-", "")[:8] for p in picks}
    kicks = await get_kickoffs([d for d in dates if len(d) == 8])
    for p in picks:
        teams = frozenset(normalize_team(t) for t in _VS_RE.split(p.get("match") or "") if t.strip())
        ko = kicks.get(teams)
        p["kickoff"] = ko or ((p.get("commence_time") or "9999") + "T23:59:59")   # unknown → end of its day


def _slate_matchups(markets: list[Market]) -> list[dict]:
    """Favorite-vs-underdog pairs for moneyline games on the slate (today + horizon), soonest
    first. Feeds match-level smart money. Mirrors the favorite logic in picks.generate."""
    horizon = getattr(config, "SLATE_HORIZON_DAYS", 4)
    out = []
    for m in markets:
        if m.market_type != "moneyline":
            continue
        ranked = sorted([s for s in m.selections if s.key != "draw"],
                        key=lambda s: (s.fair_prob or 0), reverse=True)
        if len(ranked) < 2 or (ranked[0].fair_prob or 0) < config.FAVORITE_MIN_PROB:
            continue
        d = picks._days_out(m.commence_time)
        if d is not None and not (0 <= d <= horizon):
            continue
        top, opp = ranked[0], ranked[1]
        out.append({
            "fav_team": top.label, "fav_key": top.key,
            "opp_team": opp.label, "opp_key": opp.key,
            "event": m.event, "commence_time": m.commence_time, "days_out": d,
        })
    out.sort(key=lambda x: (x["days_out"] if x["days_out"] is not None else 999))
    return out


# --------------------------------------------------------------------------- #
# Metered feed (The Odds API) — manual-only, debounced, disk-persisted
# --------------------------------------------------------------------------- #
def _rebuild_markets(dicts: list[dict]) -> list[Market]:
    out: list[Market] = []
    for d in dicts:
        sels = []
        for s in d.get("selections", []):
            quotes = [
                Quote(
                    source=q["source"], source_type=q["source_type"],
                    price_decimal=q["price_decimal"], implied_prob=q["implied_prob"],
                    mid_prob=q.get("mid_prob"), link=q.get("link"),
                )
                for q in s.get("quotes", [])
            ]
            sels.append(Selection(key=s["key"], label=s["label"], quotes=quotes))
        out.append(Market(
            market_id=d["market_id"], event=d["event"], market_type=d["market_type"],
            selections=sels, commence_time=d.get("commence_time"), group=d.get("group")))
    return out


def _load_odds_disk() -> None:
    p = config.ODDS_CACHE_PATH
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text())
        _odds_state.update(
            markets=_rebuild_markets(data.get("markets", [])),
            fetched_at=data.get("fetched_at", 0.0),
            credits_remaining=data.get("credits_remaining"),
            ok=bool(data.get("markets")),
        )
        _corner_state.update(
            lines={frozenset(d["teams"]): {k: v for k, v in d.items() if k != "teams"}
                   for d in data.get("corner_lines", []) if d.get("teams")},
            fetched_at=data.get("corner_fetched_at", 0.0),
        )
        print(f"[aggregator] loaded {len(_odds_state['markets'])} cached odds markets, "
              f"{len(_corner_state['lines'])} corner lines "
              f"({_odds_state['credits_remaining']} credits left)")
    except Exception as exc:  # noqa: BLE001
        print(f"[aggregator] odds cache load failed: {exc}")


def _save_odds_disk() -> None:
    try:
        config.ODDS_CACHE_PATH.write_text(json.dumps({
            "fetched_at": _odds_state["fetched_at"],
            "credits_remaining": _odds_state["credits_remaining"],
            "markets": [m.to_dict() for m in _odds_state["markets"]],
            "corner_fetched_at": _corner_state["fetched_at"],
            "corner_lines": [{"teams": sorted(teams), **data}
                             for teams, data in _corner_state["lines"].items()],
        }))
    except Exception as exc:  # noqa: BLE001
        print(f"[aggregator] odds cache save failed: {exc}")


async def get_odds_markets(refresh: bool = False) -> tuple[list[Market], dict]:
    if not config.has_sportsbooks():
        return [], {"enabled": False, "credits_remaining": None, "last_fetched": None, "markets": 0}
    if not _odds_state["loaded"]:
        _load_odds_disk()
        _odds_state["loaded"] = True

    now = time.time()
    age = now - (_odds_state["fetched_at"] or 0)
    credits = _odds_state["credits_remaining"]
    if refresh and age > config.ODDS_MIN_REFRESH_INTERVAL:
        if credits is not None and credits < config.ODDS_CREDIT_FLOOR:
            # at/near the monthly limit — serve cached lines instead of re-hitting an exhausted API
            _odds_state["fetched_at"] = now
            print(f"[aggregator] odds refresh skipped — {credits} credits left (floor {config.ODDS_CREDIT_FLOOR})")
        else:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                res = await theoddsapi.fetch(client)
            if res["credits_remaining"] is not None:
                _odds_state["credits_remaining"] = res["credits_remaining"]
            _odds_state["fetched_at"] = now      # advance debounce even on failure — no hammer-on-exhaustion
            if res["ok"]:
                _odds_state.update(markets=res["markets"], ok=True)
            else:
                print(f"[aggregator] odds refresh not ok: {res.get('error')}")
            _save_odds_disk()

    cr = _odds_state["credits_remaining"]
    meta = {
        "enabled": True,
        "credits_remaining": cr,
        "last_fetched": _odds_state["fetched_at"] or None,
        "markets": len(_odds_state["markets"]),
        "credits_low": cr is not None and cr < config.ODDS_CREDIT_FLOOR,
    }
    return _odds_state["markets"], meta


async def get_corner_lines(refresh: bool = False, matchups: list[dict] | None = None) -> dict:
    """Total-corner lines keyed by frozenset(team_keys). Rides the normal Odds refresh: when
    refresh=True (the user pulled book lines) and the cache is stale, it spends 1 credit per NEAR-slate
    game (today + CORNER_SLATE_DAYS, capped at CORNER_MAX_GAMES) and persists. Cached otherwise."""
    if not config.has_sportsbooks():
        return {}
    if not _odds_state["loaded"]:
        _load_odds_disk()
        _odds_state["loaded"] = True
    now = time.time()
    age = now - (_corner_state["fetched_at"] or 0)
    credits = _odds_state["credits_remaining"]
    corner_floor = config.ODDS_CREDIT_FLOOR + config.CORNER_MAX_GAMES   # corners back off BEFORE cheap h2h
    if refresh and age > config.CORNER_REFRESH_INTERVAL:
        if credits is not None and credits < corner_floor:
            _corner_state["fetched_at"] = now
            print(f"[aggregator] corner pull skipped — {credits} credits left (corner floor {corner_floor})")
            return _corner_state["lines"]
        targets = {frozenset({mu["fav_key"], mu["opp_key"]}) for mu in (matchups or [])
                   if mu.get("days_out") is not None and mu["days_out"] <= config.CORNER_SLATE_DAYS}
        if targets:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                try:
                    fetched, credits = await theoddsapi.fetch_corners(
                        client, targets=targets, max_games=config.CORNER_MAX_GAMES)
                except Exception as exc:  # noqa: BLE001
                    print(f"[aggregator] corner lines errored: {exc}")
                    fetched, credits = {}, None
            if credits is not None:
                _odds_state["credits_remaining"] = credits
            for (d, teams), data in fetched.items():    # merge; keep cached lines for games not refetched
                _corner_state["lines"][teams] = {**data, "date": d}
            # advance the debounce + persist even on an EMPTY pull (books often haven't posted corner
            # lines yet) — otherwise the next Odds click would re-spend credits indefinitely.
            _corner_state["fetched_at"] = now
            _save_odds_disk()
    return _corner_state["lines"]


async def get_team_stats(results: list[dict]) -> dict:
    """API-Football team match stats (corners/possession) for finished games — both to accumulate
    corner rates and to settle pending corner picks. Cached forever + budget-capped in the adapter."""
    if not config.has_apifootball():
        return {}
    games = list(paper.pending_corner_games())                 # settle these
    seen = set(games)
    for g in results:                                          # + recent finished games → build rates
        teams = frozenset(t for t in (g.get("goals") or {}).keys() if t)
        key = (g.get("date"), teams)
        if g.get("date") and len(teams) == 2 and key not in seen:
            games.append(key)
            seen.add(key)
    if not games:
        return {}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            return await apifootball.fetch_team_stats(client, games)
        except Exception as exc:  # noqa: BLE001
            print(f"[aggregator] team stats errored: {exc}")
            return {}


# --------------------------------------------------------------------------- #
# Merge across sources
# --------------------------------------------------------------------------- #
def _canonical_key(m: Market) -> str:
    if m.market_type == "winner_outright":
        return "futures:winner"
    if m.market_type == "moneyline":
        return moneyline_key(m.commence_time, [s.key for s in m.selections])
    if m.market_type.startswith("advance_"):
        return f"futures:{m.market_type}"
    if m.market_type == "group_winner":
        return f"group:{m.event.lower()}"
    return f"{m.market_type}:{m.market_id}"


def _merge(markets: list[Market]) -> list[Market]:
    """Combine markets that refer to the same thing. Creates fresh Selections so repeated
    calls never accumulate duplicate quotes on the cached source objects."""
    merged: dict[str, Market] = {}
    sel_index: dict[str, dict[str, Selection]] = {}
    order: list[str] = []
    for m in markets:
        k = _canonical_key(m)
        if k not in merged:
            merged[k] = Market(
                market_id=k, event=m.event, market_type=m.market_type,
                commence_time=m.commence_time, group=m.group, selections=[],
            )
            sel_index[k] = {}
            order.append(k)
        tgt, sidx = merged[k], sel_index[k]
        if not tgt.commence_time and m.commence_time:
            tgt.commence_time = m.commence_time
        for s in m.selections:
            if s.key not in sidx:
                ns = Selection(key=s.key, label=s.label, quotes=[])
                tgt.selections.append(ns)
                sidx[s.key] = ns
            sidx[s.key].quotes.extend(s.quotes)
    return [merged[k] for k in order]


def _paper_rows(verdicts: dict, bundles: list[dict], best: list[dict]) -> list[dict]:
    """Turn AI recommended_bets into paper-ledger rows. Favorite-ML picks get a fair prob + best
    price (→ CLV trackable); other archetypes are logged for hit-rate (no odds/CLV)."""
    today = time.strftime("%Y-%m-%d")
    bundle_by = {b["match"]: b for b in bundles}
    ml_price = {(r["event"], r["selection"]): r["best_price_decimal"]
                for r in best if r["market_type"] == "moneyline"}
    rows = []
    for match, v in verdicts.items():
        b = bundle_by.get(match, {})
        for bet in v.get("recommended_bets", []):
            arch = bet.get("archetype")
            if _is_pass(bet.get("selection")):
                continue  # the model sometimes phrases a pass inside recommended_bets

            odds_type = popularity = None
            on_favorite = agreement_pp = mprob = None
            if arch == "favorite_ml" and b.get("favorite"):
                fav = b["favorite"]
                fair = (b.get("favorite_fair_pct") or 0) / 100 or None
                price = ml_price.get((match, fav)) or (round(1 / fair, 3) if fair else None)
                sel = f"{fav} ML"
                on_favorite = 1
                if b.get("favorite_model_pct") is not None:
                    mprob = b["favorite_model_pct"] / 100.0
                if b.get("favorite_fair_pct") is not None and b.get("favorite_model_pct") is not None:
                    agreement_pp = round(b["favorite_fair_pct"] - b["favorite_model_pct"], 1)
            else:
                fair, price, sel = None, None, (bet.get("selection") or "")[:90]
                pr = memory._match_prop(sel, b.get("props", []))  # capture prop dims for buckets
                if pr:
                    odds_type = pr.get("type")
                    popularity = pr.get("popularity")
                    on_favorite = 1 if pr.get("team") == b.get("favorite") else 0
                    if pr.get("model_pct") is not None:        # the model's projected P(hit) on this prop
                        mprob = pr["model_pct"] / 100.0
                if mprob is None and arch == "team_total_over" and b.get("team_total_over_1_5_model_pct") is not None:
                    mprob = b["team_total_over_1_5_model_pct"] / 100.0
            if _unsettleable_prop(arch, sel):
                continue   # vague/compound prop the auto-grader can't resolve — don't log it at all
            rows.append({
                "match": match, "archetype": arch, "selection": sel,
                "confidence": v.get("confidence"), "commence_time": b.get("commence_time"),
                "pick_fair_prob": fair, "pick_price_decimal": price, "model_prob": mprob,
                "dedup_key": f"{today}:{match}:{arch}:{sel}",
                "odds_type": odds_type, "popularity": popularity,
                "on_favorite": on_favorite, "agreement_pp": agreement_pp,
                "stake_units": _stake_units(v.get("confidence")),
            })
    return rows


def _unsettleable_prop(arch: str, sel: str) -> bool:
    """A prop the auto-grader can never resolve: a compound 'A or B' selection (no single gradeable
    outcome), or a shots/SOT/passes prop with no numeric line. Dropped at log time so the ledger does
    not accrue picks that sit in Awaiting until the 3-day void."""
    s = (sel or "").lower()
    if " or " in s:                                              # "Kane or Saka", "shots or shots-on-target"
        return True
    if arch in ("shots_sot", "popular_prop") and not re.search(r"\d", s):   # no line to grade against
        return True
    return False


_PASS_RE = re.compile(r"\b(pass|fade|avoid|no bet)\b")  # \bpass\b ignores "Passes Attempted"


def _is_pass(sel: str) -> bool:
    return bool(_PASS_RE.search((sel or "").lower()))


def _daily_card(verdicts: dict, bundles: list[dict]) -> tuple[list, dict | None]:
    """From the AI verdicts, build Picks of the Day (top recommended bets by confidence, soonest
    first) and a Parlay of the Day (the highest-confidence match with 2+ real bets as a same-game
    parlay; else a small cross-match parlay of the top picks)."""
    bundle_by = {b["match"]: b for b in bundles}
    pod = []
    for match, v in verdicts.items():
        b = bundle_by.get(match, {})
        for bet in v.get("recommended_bets", []):
            if _is_pass(bet.get("selection")):
                continue
            pod.append({
                "match": match, "archetype": bet.get("archetype"), "selection": bet.get("selection"),
                "rationale": bet.get("rationale"), "confidence": v.get("confidence") or 0,
                "commence_time": b.get("commence_time"), "days_out": b.get("days_out"),
            })
    pod.sort(key=lambda x: (-x["confidence"], x["days_out"] if x["days_out"] is not None else 99))
    picks_of_day = pod[:3]

    parlay = None
    best = None
    for match, v in verdicts.items():
        real = [bt for bt in v.get("recommended_bets", []) if not _is_pass(bt.get("selection"))]
        conf = v.get("confidence") or 0
        if len(real) >= 2 and (best is None or conf > best[0]):
            best = (conf, match, real, bundle_by.get(match, {}))
    if best:
        conf, match, real, b = best
        parlay = {"type": "Same-game parlay", "match": match, "confidence": conf,
                  "commence_time": b.get("commence_time"), "days_out": b.get("days_out"),
                  "legs": [{"archetype": bt["archetype"], "selection": bt["selection"], "match": match}
                           for bt in real[:3]]}
    elif len(pod) >= 2:
        legs = pod[:3]
        parlay = {"type": "Parlay", "match": None,
                  "confidence": round(sum(p["confidence"] for p in legs) / len(legs)),
                  "legs": [{"archetype": p["archetype"], "selection": p["selection"], "match": p["match"]}
                           for p in legs]}
    return picks_of_day, parlay


def _enrich_bundles_price(bundles: list[dict], best_lines: list[dict]) -> None:
    """Attach the favorite's best executable price + EV-vs-sharp-fair to each reasoning bundle, so
    the AI verdict can reason on the REAL edge (and honestly say 'no edge, market's efficient' when
    the best price doesn't beat fair — common on heavy favorites)."""
    ml = {(r["event"], r["selection"]): r
          for r in best_lines if r.get("market_type") == "moneyline"}
    for b in bundles:
        r = ml.get((b.get("match"), b.get("favorite")))
        if not r:
            continue
        b["favorite_best_price_american"] = r["best_american"]
        b["favorite_best_book"] = r["best_source"]
        b["favorite_market_ev_pct"] = round(r["ev"] * 100, 1) if r.get("ev") is not None else None


def _stake_units(confidence, tier=None, source=None) -> float:
    """Conviction → units of bankroll. Disciplined flat-ish staking (not pure Kelly, since the
    user's favorites/props often aren't +EV vs the book). Hard-capped at MAX_UNITS."""
    if confidence:                                   # AI verdict confidence 1–5
        u = {5: 3.0, 4: 2.0, 3: 1.5, 2: 1.0, 1: 0.5}.get(int(confidence), 1.0)
    elif tier == "strong":
        u = 1.5
    elif tier == "lean":
        u = 1.0
    else:                                            # bare model favorite
        u = 1.0
    return min(u, config.MAX_UNITS)


def _stake_fields(confidence, tier=None) -> dict:
    units = _stake_units(confidence, tier)
    return {"stake_units": units, "stake_dollars": round(units * config.BANKROLL * config.UNIT_PCT)}


def _price_lookup(best_lines: list[dict]):
    """(match, team label) -> best moneyline price row, for folding line-shopping onto cards."""
    ml = {(r["event"], r["selection"]): r
          for r in best_lines if r.get("market_type") == "moneyline"}

    def line(match, team):
        r = ml.get((match, team))
        if not r:
            return {}
        return {"best_price_decimal": r["best_price_decimal"], "best_american": r["best_american"],
                "best_book": r["best_source"],
                "ev_pct": round(r["ev"] * 100, 1) if r.get("ev") is not None else None}
    return line


def _best_bets(picks_board: dict, best_lines: list[dict]) -> list[dict]:
    """One ranked, cards-first feed scoped to the user's archetypes. Sorted by SOURCE TIER first
    (AI-reasoned > strong/lean heuristic reads > live non-chalk favorites) then within-tier score,
    so credibility — not a brittle mixed scale — drives the order. The best available price + book
    + EV-vs-fair is folded onto every moneyline card (line-shopping at the point of decision)."""
    ai = picks_board.get("ai", {})
    price_line = _price_lookup(best_lines)
    cards, seen = [], set()
    ai_players = " ".join(
        (bet.get("selection") or "") for v in ai.values() for bet in v.get("recommended_bets", [])
    ).lower()

    def add(key, card):
        if key not in seen:
            seen.add(key)
            cards.append(card)

    for match, v in ai.items():                       # tier 3 — AI-reasoned bets (the gold)
        for bet in v.get("recommended_bets", []):
            arch, sel = bet.get("archetype"), (bet.get("selection") or "")
            card = {
                "source": "ai", "archetype": arch, "selection": sel, "match": match,
                "days_out": v.get("days_out"), "confidence": v.get("confidence"),
                "reasoning": bet.get("rationale"), "risk": v.get("key_risk"),
                "research": v.get("research"), "memory_note": v.get("memory_note"),
                "tier_rank": 3, "score": v.get("confidence") or 3,
                **_stake_fields(v.get("confidence")),
            }
            if arch == "favorite_ml":
                mt = re.match(r"(.+?)\s+ML\b", sel)            # AI may decorate: "Belgium ML (Kalshi -213)"
                team = (mt.group(1) if mt else sel.replace(" ML", "")).strip()
                card.update(price_line(match, team))
            add((arch, sel.lower()), card)

    for sect in ("anytime_goalscorer", "shots_sot", "popular_props"):   # tier 2 — strong/lean reads
        for r in picks_board.get(sect, []):
            rd = r.get("read") or {}
            player = (r.get("player") or "")
            if rd.get("lean") not in ("over", "under") or rd.get("tier") not in ("strong", "lean"):
                continue
            if player and player.lower() in ai_players:   # already an AI card → skip dup
                continue
            verb = "Over" if rd["lean"] == "over" else "Under"
            add(("prop", player.lower() + (r.get("stat_type") or "").lower()), {
                "source": "read", "archetype": sect,
                "selection": f"{player} {r.get('stat_type')} {verb} {r.get('line')}",
                "match": f"{r.get('team', '')} vs {r.get('opponent', '')}", "days_out": r.get("days_out"),
                "confidence": None, "tier": rd.get("tier"), "reasoning": rd.get("rationale"),
                "trap": rd.get("trap_risk"), "model_prob": r.get("model_prob"), "model_value": r.get("model_value"),
                "tier_rank": 2, "score": rd.get("read_score", 50) + (12 if rd.get("tier") == "strong" else 0),
                **_stake_fields(None, rd.get("tier")),
            })

    for r in picks_board.get("corners", []):            # tier 2 — +EV corner totals (a normal bet)
        if r.get("line") is None or r.get("ev", -9) < config.CORNER_EDGE_MIN or r.get("confidence") == "prior":
            continue
        tier = "strong" if r["ev"] >= 0.08 else "lean"
        add(("corners", (r.get("selection") or "").lower()), {
            "source": "model", "archetype": "total_corners", "selection": r["selection"],
            "match": r["event"], "days_out": r.get("days_out"), "confidence": None, "tier": tier,
            "reasoning": f"model projects {r['proj_total']} corners ({r['proj_fav']}–{r['proj_opp']}) "
                         f"vs the {r['line']} line — {r['model_prob'] * 100:.0f}% to hit "
                         f"vs {(r.get('fair_side') or 0) * 100:.0f}% book-fair",
            "best_american": odds_math.decimal_to_american(r["price"]), "best_book": r.get("book"),
            "ev_pct": round(r["ev"] * 100, 1),
            "model_prob": r["model_prob"], "model_value": "value" if r["ev"] >= 0.08 else "lean",
            "tier_rank": 2, "score": 60 + r["ev"] * 100,
            **_stake_fields(None, tier),
        })

    for f in picks_board.get("favorite_ml", []):        # tier 1 — live (non-chalk) favorites
        if f.get("chalk"):
            continue
        model_txt = f", model {f['model_prob'] * 100:.0f}%" if f.get("model_prob") is not None else ""
        card = {
            "source": "model", "archetype": "favorite_ml", "selection": f"{f['team']} ML",
            "match": f.get("event"), "days_out": f.get("days_out"), "confidence": None, "tier": "lean",
            "reasoning": f"{f['fair_prob'] * 100:.0f}% sharp fair{model_txt}",
            "tier_rank": 1, "score": f["fair_prob"] * 100,
            **_stake_fields(None, "lean"),
        }
        card.update(price_line(f.get("event"), f.get("team")))
        add(("ml", f.get("team_key")), card)

    cards.sort(key=lambda c: (c["tier_rank"], c["score"]), reverse=True)
    return cards[:16]


def _fades(picks_board: dict) -> list[dict]:
    """Explicit 'avoid' cards — the fade-the-crowd thesis (archetype E). From the AI verdicts'
    fades plus high-trap heuristic prop reads (hot demons / script traps). A clear 'don't bet'."""
    fades, seen = [], set()

    def add(key, card):
        if key not in seen:
            seen.add(key)
            fades.append(card)

    for match, v in picks_board.get("ai", {}).items():
        for fd in (v.get("fades") or []):
            sel = (fd.get("selection") or "")
            if sel:
                add(("ai", sel.lower()), {"source": "ai", "selection": sel, "match": match,
                    "days_out": v.get("days_out"), "reasoning": fd.get("why")})

    for sect in ("anytime_goalscorer", "shots_sot", "popular_props"):
        for r in picks_board.get(sect, []):
            rd = r.get("read") or {}
            if not rd.get("trap_risk") and rd.get("lean") != "avoid":
                continue
            player = (r.get("player") or "")
            sel = f"{player} {r.get('stat_type')} Over {r.get('line')}"
            add(("read", sel.lower()), {"source": "read", "selection": sel,
                "match": f"{r.get('team', '')} vs {r.get('opponent', '')}", "days_out": r.get("days_out"),
                "reasoning": rd.get("rationale"), "trap_kind": rd.get("trap_kind")})

    return fades[:8]


def _attach_model(markets: list[Market], model) -> None:
    if not model:
        return
    for m in markets:
        if m.market_type != "moneyline":
            continue
        teams = [s.key for s in m.selections if s.key != "draw"]
        if len(teams) != 2:
            continue
        probs = model.match_probs(teams[0], teams[1])
        if not probs:
            continue
        for s in m.selections:
            s.model_prob = probs.get(s.key)


def _possession_proxy(a: str, b: str, model) -> float | None:
    """Team A's projected possession share from the match model's expected goals (a dominant attack
    tends to hold the ball more). None when the model can't price the pair → corners fall back to
    the rate-only opponent adjustment."""
    if not model:
        return None
    xg = model.expected_goals(a, b)
    if not xg or (xg[0] + xg[1]) <= 0:
        return None
    return max(0.3, min(0.7, xg[0] / (xg[0] + xg[1])))


def _corners_board(matchups: list[dict], rates: dict, corner_lines: dict, model) -> list[dict]:
    """Per slate game: projected total + per-team corners (opponent + dominance adjusted), plus the
    de-vigged book total line and the better side's EV once corner lines have been pulled.
    confidence='prior' = no measured corner history yet (pure league baseline → read only, not bet)."""
    out = []
    for mu in matchups:
        a, b = mu["fav_key"], mu["opp_key"]
        poss_a = _possession_proxy(a, b, model)
        la, lb, total = corners.project_total(a, b, rates, poss_a)
        row = {
            "event": mu["event"], "commence_time": mu["commence_time"], "days_out": mu["days_out"],
            "fav_team": mu["fav_team"], "opp_team": mu["opp_team"],
            "proj_total": round(total, 1), "proj_fav": round(la, 1), "proj_opp": round(lb, 1),
            "confidence": corners.confidence((rates.get(a) or {}).get("games", 0),
                                             (rates.get(b) or {}).get("games", 0)),
        }
        ld = corner_lines.get(frozenset({a, b}))
        if ld and ld.get("line") is not None and ld.get("over_price") and ld.get("under_price"):
            line = ld["line"]
            p_over = corners.over_prob(total, line)
            ev_over = p_over * ld["over_price"] - 1.0
            ev_under = (1.0 - p_over) * ld["under_price"] - 1.0
            if ev_over >= ev_under:
                side, price, prob, ev, bk = "Over", ld["over_price"], p_over, ev_over, ld.get("over_book")
            else:
                side, price, prob, ev, bk = "Under", ld["under_price"], 1.0 - p_over, ev_under, ld.get("under_book")
            fo = ld.get("fair_over") or 0.0
            fair_side = fo if side == "Over" else 1.0 - fo   # book-fair for the SIDE we're taking
            row.update(line=line, side=side, price=price, book=bk,
                       model_prob=round(prob, 3), fair_side=round(fair_side, 4),
                       ev=round(ev, 3), selection=f"{side} {line} corners")
        out.append(row)
    out.sort(key=lambda r: (r["days_out"] if r["days_out"] is not None else 999, -(r.get("ev") or -9)))
    return out


def _corner_paper_row(r: dict) -> dict | None:
    """A +EV corner read becomes a priced bankroll entry (settles + tracks like any other bet).
    Dedup is per-game-per-day (ignores the moving line/side) so an intraday line move can't double-log
    the same game. Stake matches the Best Bets card (EV tier). No CLV on corners → no pick_fair_prob,
    so the ledger shows hit-rate/ROI like props rather than a perpetual 'close —'."""
    if not r.get("selection") or r.get("line") is None or r.get("price") is None:
        return None
    today = time.strftime("%Y-%m-%d")
    return {
        "match": r["event"], "archetype": "total_corners", "selection": r["selection"],
        "commence_time": r.get("commence_time"), "pick_price_decimal": r.get("price"),
        "model_prob": r.get("model_prob"),   # model P(side hits) — for calibration/Brier, not CLV
        "dedup_key": f"{today}:corners:{r['event']}",
        "stake_units": 1.5 if r.get("ev", 0) >= 0.08 else 1.0,
    }


# --------------------------------------------------------------------------- #
# Futures: tournament simulation vs the de-vigged Polymarket futures line
# --------------------------------------------------------------------------- #
def _pm_prob(sel) -> float | None:
    for q in sel.quotes:
        if q.source == "polymarket":
            return q.mid_prob or q.implied_prob
    return None


def _load_groups_disk() -> dict:
    p = config.GROUPS_CACHE_PATH
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:  # noqa: BLE001
            pass
    return {}


def _save_groups_disk(g: dict) -> None:
    try:
        config.GROUPS_CACHE_PATH.write_text(json.dumps(g))
    except Exception as exc:  # noqa: BLE001
        print(f"[aggregator] groups cache save failed: {exc}")


def _team_game_counts(results: list[dict] | None) -> dict:
    counts: dict = {}
    for game in results or []:
        for t in (game.get("goals") or {}):
            counts[t] = counts.get(t, 0) + 1
    return counts


def _reconstruct_groups_from_results(results: list[dict] | None) -> dict:
    """Rebuild the 12 group compositions from finished games alone. Two teams are groupmates iff they are
    among each other's FIRST THREE opponents (every team plays its three groupmates first, then knockout
    games), so cross-group knockout fixtures - which are each side's 4th+ game - never create an edge.
    Returns {label: [team, ...]} for the size-4 connected components. During the group stage there are no
    cross-group games at all, so the components are exactly the groups; the caller only trusts a live
    reconstruction while no knockout game has been played (else it leans on the frozen disk field), which
    closes the one hole this has: a stray cross-group edge into a not-yet-complete group. Needs no
    markets, so it survives the group-winner markets closing as the stage ends."""
    opp: dict = {}
    for game in sorted(results or [], key=lambda g: g.get("date") or ""):
        teams = list((game.get("goals") or {}).keys())
        if len(teams) != 2:
            continue
        for x, y in ((teams[0], teams[1]), (teams[1], teams[0])):
            lst = opp.setdefault(x, [])
            if y not in lst and len(lst) < 3:      # only a team's first three opponents (its group games)
                lst.append(y)

    adj: dict = {}
    for a, lst in opp.items():
        for b in lst:
            if a in opp.get(b, []):                # mutual: both list each other -> a real group pairing
                adj.setdefault(a, set()).add(b)
                adj.setdefault(b, set()).add(a)

    seen: set = set()
    out: dict = {}
    idx = 0
    for start in adj:
        if start in seen:
            continue
        stack, comp = [start], []
        while stack:
            u = stack.pop()
            if u in seen:
                continue
            seen.add(u)
            comp.append(u)
            stack.extend(adj[u] - seen)
        if len(comp) == 4:
            out[f"grp{idx}"] = sorted(comp)
            idx += 1
    return out


def _valid_field(field) -> bool:
    """A trustworthy group field is exactly 12 groups of 4 teams (rejects the old letter-keyed cache)."""
    return (isinstance(field, dict) and len(field) == 12
            and all(isinstance(v, list) and len(v) == 4 for v in field.values()))


def _field_complete(field: dict, results: list[dict] | None) -> bool:
    """True once every one of the 12 groups is a complete round robin (all six pairings played) - the
    point at which the composition is final and safe to freeze to disk."""
    if not _valid_field(field):
        return False
    pairs = {frozenset(list((g.get("goals") or {}).keys()))
             for g in (results or []) if len((g.get("goals") or {})) == 2}
    for teams in field.values():
        for i in range(4):
            for j in range(i + 1, 4):
                if frozenset((teams[i], teams[j])) not in pairs:
                    return False
    return True


def _played_in_groups(results: list[dict] | None, groups: dict) -> dict:
    """{frozenset({a,b}): {a: goals, b: goals}} for finished games between two same-group teams, so the
    sim locks in the standings so far instead of re-playing decided matches (otherwise mid-tournament it
    would price a nearly-settled group as if it were kickoff and show phantom edges). Earliest game per
    pair wins, so a later knockout rematch of a group pairing cannot overwrite the real group result."""
    if not results:
        return {}
    team_group = {t: g for g, ts in groups.items() for t in ts}
    played: dict = {}
    for game in sorted(results, key=lambda g: g.get("date") or ""):
        goals = game.get("goals") or {}
        pair = [t for t in goals if t in team_group]
        if len(pair) == 2 and team_group[pair[0]] == team_group[pair[1]]:
            key = frozenset(pair)
            if key not in played:                  # keep the group-stage game, ignore a knockout rematch
                a, b = pair
                played[key] = {a: goals[a], b: goals[b]}
    return played


def _devig_market(market, target: float, valid_teams: set) -> dict:
    """{team: probability} from a Polymarket futures market, scaled so the probabilities sum to `target`
    (the number of teams that reach the stage: 16 for the R16, 8 for the QF, 4 for the SF, 1 for the
    winner). Only genuine teams (those in the reconstructed field) share the slot budget, so a placeholder
    selection cannot quietly absorb part of it and understate every real team. Normalizing to the known
    slot count removes the book's margin."""
    probs: dict = {}
    for s in market.selections:
        p = _pm_prob(s)
        if s.key and p and s.key in valid_teams:
            probs[s.key] = p
    tot = sum(probs.values()) or 1.0
    return {k: v * target / tot for k, v in probs.items()}


# (sim stage key, Polymarket market_type, teams reaching the stage, display label, rows to show)
_FUTURES_STAGES = [
    ("win_cup",   "winner_outright", 1,  "Win World Cup",        18),
    ("reach_sf",  "advance_sf",      4,  "Reach Semi-final",     12),
    ("reach_qf",  "advance_qf",      8,  "Reach Quarter-final",  12),
    ("reach_r16", "advance_r16",     16, "Reach Round of 16",    16),
]


def _compute_futures(markets: list[Market], model, results: list[dict] | None = None) -> dict:
    """Market-led knockout futures board. The 12 group compositions are reconstructed from the free ESPN
    results feed (no dependence on the now-closed group-winner markets); the qualifiers, bracket, and
    deep-run/winner probabilities come from playing the tournament out thousands of times, conditioned on
    every group game already decided. The HEADLINE per row is the de-vigged Polymarket price (the sharp,
    vig-free probability); the model rides alongside as an openly-conservative second opinion (a simple
    ratings model under-separates elite teams, so in the open knockout it systematically trails the market
    on favorites, which is model caution rather than betting value). Runs on a thread (sim is a few sec)."""
    live = _reconstruct_groups_from_results(results)
    disk = _load_groups_disk()
    disk = disk if _valid_field(disk) else {}     # ignore the old letter-keyed / malformed cache
    counts = _team_game_counts(results)
    knockouts_started = bool(counts) and max(counts.values()) > 3
    if _field_complete(live, results):            # all 12 groups done -> final, freeze it
        field = live
        _save_groups_disk(field)
    elif disk:                                    # trust the frozen field once group games age out
        field = disk
    elif not knockouts_started:                   # group stage in progress: no cross-group games yet
        field = live
    else:                                         # mid-knockout cold start, no good cache -> degrade
        field = {}
    covered = len(field)
    market_by_type = {m.market_type: m for m in markets if m.market_type}
    have_market = any(mt in market_by_type for _, mt, _, _, _ in _FUTURES_STAGES)
    if covered < 12 or not model or not have_market:
        return {"rows": [], "groups_covered": covered, "sims": 0, "games_locked": 0}

    def lambdas(a, b):
        return model.expected_goals(a, b)

    def strength(t):
        return model.attack.get(t, 1.0) - model.defense.get(t, 1.0)

    field_teams = {t for ts in field.values() for t in ts}
    played = _played_in_groups(results, field)
    sim = tournament.simulate(field, lambdas, strength, played=played, n=config.TOURNAMENT_SIMS)

    rows = []
    for order, (simkey, mtype, target, label, topn) in enumerate(_FUTURES_STAGES):
        m = market_by_type.get(mtype)
        if not m:
            continue
        fair = _devig_market(m, target, field_teams)
        for team, mk in sorted(fair.items(), key=lambda kv: -kv[1])[:topn]:
            md = sim.get(team, {}).get(simkey)
            if md is None:
                continue
            rows.append({"team": team, "kind": label, "_sort": (order, -mk),
                         "market_pct": round(mk * 100, 1),     # de-vigged Polymarket - the headline
                         "model_pct": round(md * 100, 1),      # conservative second opinion
                         "gap_pp": round((md - mk) * 100, 1)}) # model minus market, shown neutral
    rows.sort(key=lambda r: r["_sort"])
    for r in rows:
        r.pop("_sort", None)
    return {"rows": rows, "groups_covered": covered, "sims": config.TOURNAMENT_SIMS,
            "games_locked": len(played)}


async def get_futures(markets: list[Market], model, results: list[dict] | None = None) -> dict:
    """Cached tournament-sim board (recomputed at most every FUTURES_CACHE_TTL, off the event loop)."""
    if not model:
        return _futures_cache["data"]
    now = time.monotonic()
    if _futures_cache["ts"] and (now - _futures_cache["ts"]) < config.FUTURES_CACHE_TTL:
        return _futures_cache["data"]
    try:
        data = await asyncio.to_thread(_compute_futures, markets, model, results)
        _futures_cache.update(data=data, ts=now)
    except Exception as exc:  # noqa: BLE001
        print(f"[aggregator] futures sim errored: {exc}")
    return _futures_cache["data"]


# --------------------------------------------------------------------------- #
# Build the snapshot served to the dashboard
# --------------------------------------------------------------------------- #
async def build_snapshot(force: bool = False, refresh_odds: bool = False,
                         reason: bool = False) -> dict:
    free_markets, props = await get_free(force=force)
    odds, odds_meta = await get_odds_markets(refresh=refresh_odds)
    markets = _merge(free_markets + odds)

    model = await asyncio.to_thread(ratings.get_model)
    _attach_model(markets, model)

    # The user only bets match moneylines (+ props). We compute the sharp fair line for every
    # moneyline market and the best available price per selection — the best price + book + EV-vs-fair
    # is folded directly onto the Best Bets cards (line-shopping at the point of decision), so there
    # are no standalone +EV / Best Lines / Arbitrage tabs anymore. Futures are never surfaced.
    best = []
    for m in markets:
        if m.market_type != "moneyline":
            continue
        edges.consensus_fair_line(m, config.SHARP_SOURCES, config.DEVIG_METHOD)
        best.extend(edges.best_lines(m, min_fair_prob=config.MIN_FAIR_PROB))

    # smart money — now match-level: whale backing on the actual games on the slate, not the
    # tournament-winner market. Derive the matchups (favorites scoped to the horizon) first.
    matchups = _slate_matchups(markets)
    smart = await get_smart_money(matchups, force=force)
    lineups = await get_lineups(matchups, force=force)   # confirmed XIs for today's games (free)
    picks_board = picks.generate(markets, props, model, config, smart)

    # corners — a dominance market handled like any other bet: project total + per-team from
    # accumulated team rates (opponent + possession adjusted), priced against the de-vigged book total
    # line. The lines ride the normal Odds refresh (near slate only, to bound credits); +EV reads then
    # flow into Best Bets and auto-log to the ledger just like favorites/props.
    corner_rates = corners.build_team_corner_rates()
    corner_lines = await get_corner_lines(refresh=refresh_odds, matchups=matchups)
    odds_meta["credits_remaining"] = _odds_state["credits_remaining"]   # corners spent after odds_meta was built
    odds_meta["credits_low"] = (odds_meta["credits_remaining"] is not None
                                and odds_meta["credits_remaining"] < config.ODDS_CREDIT_FLOOR)
    picks_board["corners"] = _corners_board(matchups, corner_rates, corner_lines, model)

    # Futures: tournament sim (second opinion) vs the de-vigged Polymarket winner/group-winner line.
    # Fetch results up front so the sim locks in already-played group games (cached, reused below).
    results = await get_results(force=force)
    picks_board["futures"] = await get_futures(markets, model, results)

    # AI reasoning — manual-trigger (reason=True) spends; otherwise reuse the disk cache
    bundles = picks_board.get("match_bundles", [])
    _enrich_bundles_price(bundles, best)      # real best price + EV-vs-fair
    _enrich_bundles_lineup(bundles, lineups)  # official confirmed XI when posted (~1h pre-KO)
    if reason:
        picks_board["ai"] = await reasoning.run(bundles, config.REASONING_MAX_MATCHES)
    else:
        picks_board["ai"] = reasoning.attach_cached(bundles)
    picks_board.pop("match_bundles", None)  # don't ship raw bundles to the client

    # enrich verdicts with kickoff date + drop pass-phrased "recommended" bets, then calibrate
    cal_stats = memory.compute()
    bundle_by = {b["match"]: b for b in bundles}
    for match, v in picks_board["ai"].items():
        b = bundle_by.get(match)
        if b:
            v["commence_time"] = b.get("commence_time")
            v["days_out"] = b.get("days_out")
        v["recommended_bets"] = [bt for bt in (v.get("recommended_bets") or [])
                                 if not _is_pass(bt.get("selection"))]
        v["smart_money"] = (b or {}).get("smart_money_match")  # whale tiebreaker, shown in Research
        memory.adjust(v, b or {}, cal_stats)   # gated; provably inert until buckets fill
    picks_board["calibration"] = memory.panel(cal_stats)
    picks_board["best_bets"] = _best_bets(picks_board, best)
    picks_board["fades"] = _fades(picks_board)
    pod, parlay = _daily_card(picks_board["ai"], bundles)
    picks_board["picks_of_day"] = pod
    picks_board["parlay_of_day"] = parlay

    # paper-trading proof: auto-log AI picks (on Analyze), auto-settle finished games from Kalshi,
    # then keep CLV current. Settle BEFORE capture_closing so graded picks freeze their closing line.
    if reason and picks_board["ai"]:
        paper.log_picks(_paper_rows(picks_board["ai"], bundles, best))
        prow = _parlay_row(picks_board.get("suggested_sgp"))
        if prow:
            paper.log_picks([prow])   # the Parlay of the Day, as a priced bankroll entry
    if reason:
        # +EV corner totals auto-log like any other bet (model-driven, so independent of AI)
        paper.log_picks([row for r in picks_board["corners"]
                         if r.get("ev", -9) >= config.CORNER_EDGE_MIN and r.get("confidence") != "prior"
                         for row in [_corner_paper_row(r)] if row])
    resolved = await get_resolved(force=force)
    settled_n = paper.settle_from_resolved(resolved)
    settled_p = paper.settle_parlays(results)
    settled_x = paper.settle_props(results)   # standalone goalscorer + team-total from ESPN
    # flag remaining pending picks whose game already has a result (shots/passes → "Awaiting")
    finished = {(g["date"], tk) for g in results for tk in g["goals"]}
    finished |= {(r["date"], r["team_key"]) for r in resolved
                 if r.get("date") and r.get("team_key") != "draw"}
    paper.mark_finished(finished)
    team_stats = await get_team_stats(results)                         # corners (rates + settlement)
    settled_pp = paper.settle_player_props(await get_player_stats())   # shots/SOT/passes (API-Football)
    settled_c = paper.settle_corners(team_stats)                       # total corners (API-Football)
    voided = paper.expire_ungradable(config.UNGRADABLE_VOID_DAYS)      # void any still-ungradable leftovers
    if settled_n or settled_p or settled_x or settled_pp or settled_c or voided:
        print(f"[aggregator] settled {settled_n} ML + {settled_p} parlay + {settled_x} prop "
              f"+ {settled_pp} player-prop + {settled_c} corners; voided {voided}")
    paper.capture_closing({f["event"]: f["fair_prob"] for f in picks_board["favorite_ml"]})

    # liveness from ALL markets (best is moneyline-only now; Polymarket only supplies futures)
    def _live(name: str) -> bool:
        return any(q.source == name for m in markets for s in m.selections for q in s.quotes)
    sources_live = {
        "polymarket": _live("polymarket"),
        "kalshi": _live("kalshi"),
        "sportsbooks": odds_meta["enabled"] and odds_meta["markets"] > 0,
    }
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "meta": {
            "model_loaded": model is not None,
            "sources_live": sources_live,
            "odds": odds_meta,
            "ai_enabled": config.has_anthropic(),
            "ai_count": len(picks_board.get("ai", {})),
            "props_scanned": picks_board["counts"]["props_scanned"],
            "bankroll": config.BANKROLL,
            "kelly_fraction": config.KELLY_FRACTION,
        },
        "picks": picks_board,
    }
