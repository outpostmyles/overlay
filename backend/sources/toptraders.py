"""Top bettors: what the most profitable sports traders on Polymarket and Kalshi hold on the board's games.

Polymarket runs on-chain, so every wallet's positions are public. Its data API serves the sports
leaderboard (profit this month and all-time) and each wallet's open positions, and the gamma API lists
each league's game events. Kalshi is a regulated exchange whose profiles are private by default: its
public leaderboard names the top sports traders, and only the minority who opt in show their holdings.
No key is needed for any of it.

A position is a bet someone holds right now, not a recommendation. Market makers hold inventory on both
sides, so a wallet holding both outcomes of one market is netted and marked, and a wallet whose profit is
a sliver of a huge volume is flagged; neither counts toward the consensus side.

One cache file serves every sport's process (poly_toptraders.json): leaderboards refresh every six hours,
positions and holdings every 20 minutes, and Kalshi profiles that hide their holdings twice a day.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import date

import httpx

from .. import config
from ..matching import normalize_team

CACHE_PATH = config.ROOT / "poly_toptraders.json"
LEADERS = 100                         # traders taken from each leaderboard window
TTL_LEADERBOARD = 6 * 3600
TTL_POSITIONS = 20 * 60
TTL_EVENTS = 20 * 60
TTL_HIDDEN = 12 * 3600                # a Kalshi profile that hides its holdings is rechecked twice a day
MM_VOLUME, MM_MARGIN = 50_000_000, 0.02   # $50M+ traded at under 2% profit: a market maker, not a picker
GAME_PREFIXES = ("nfl-", "cfb-", "nhl-", "mlb-")
CONSENSUS_MIN, CONSENSUS_SHARE = 1000.0, 0.60
MIN_STAKE = 50                        # smaller positions are noise on a card

_VS = re.compile(r"\s+vs\.?\s+", re.IGNORECASE)
_SPREAD = re.compile(r"^Spread:\s*(.+?)\s*\(([-+]?\d+(?:\.\d+)?)\)\s*$")
_TOTAL = re.compile(r"^(?:.+?\s+vs\.?\s+.+?:\s*)?O/U\s+(\d+(?:\.\d+)?)\s*$")
_SLUG_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})$")


# --- the shared cache ---------------------------------------------------------------------------- #
def _load() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}
    except (OSError, ValueError):
        return {}


def _save(cache: dict) -> None:
    """Atomic replace, so another sport's process never reads half a file."""
    tmp = CACHE_PATH.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(cache))
        os.replace(tmp, CACHE_PATH)
    except OSError as exc:
        print(f"[toptraders] cache save failed: {exc}")


def _stale(entry: dict | None, ttl: float) -> bool:
    return not entry or time.time() - (entry.get("ts") or 0) > ttl


# --- Polymarket ---------------------------------------------------------------------------------- #
async def _poly_leaders(client: httpx.AsyncClient) -> dict:
    """{wallet: {name, ranks: {month, all}, pnl: {...}, vol: {...}}} from the sports profit leaderboard."""
    out: dict = {}
    for period in ("MONTH", "ALL"):
        for offset in range(0, LEADERS, 50):
            r = await client.get(f"{config.POLYMARKET_DATA}/v1/leaderboard", timeout=20,
                                 params={"category": "SPORTS", "timePeriod": period, "orderBy": "PNL",
                                         "limit": 50, "offset": offset})
            r.raise_for_status()
            for t in r.json():
                w = (t.get("proxyWallet") or "").lower()
                if not w:
                    continue
                e = out.setdefault(w, {"name": t.get("userName") or w[:10], "ranks": {}, "pnl": {}, "vol": {}})
                k = period.lower()
                e["ranks"][k], e["pnl"][k], e["vol"][k] = int(t.get("rank") or 0), t.get("pnl"), t.get("vol")
    return out


def trim_positions(rows: list) -> list:
    """Keep only open game positions, and only what is shown. A game event's slug carries the league and
    ends in its date (nfl-tb-dal-2026-10-09); a season future (mlb-world-series-champion-2026) does not."""
    keep = []
    for p in rows if isinstance(rows, list) else []:
        cur, slug = p.get("curPrice"), p.get("eventSlug") or ""
        if not slug.startswith(GAME_PREFIXES) or not _SLUG_DATE.search(slug) or p.get("redeemable"):
            continue
        if cur is None or cur <= 0 or cur >= 1:
            continue
        keep.append({k: p.get(k) for k in ("eventSlug", "title", "outcome", "outcomeIndex", "conditionId",
                                           "size", "avgPrice", "curPrice", "initialValue")})
    return keep


async def _poly_positions(client: httpx.AsyncClient, wallet: str) -> list | None:
    try:
        r = await client.get(f"{config.POLYMARKET_DATA}/positions", timeout=20,
                             params={"user": wallet, "limit": 500, "sizeThreshold": 1})
        return trim_positions(r.json()) if r.status_code == 200 else None
    except Exception as exc:  # noqa: BLE001
        print(f"[toptraders] positions {wallet[:10]} failed: {exc}")
        return None


async def _poly_events(client: httpx.AsyncClient, series: str) -> list:
    """This league's upcoming game events, light (no markets): slug, title, start."""
    r = await client.get(f"{config.POLYMARKET_GAMMA}/events", timeout=20,
                         params={"series_id": series, "closed": "false", "limit": 200, "include_markets": "false",
                                 "end_date_min": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 6 * 3600))})
    r.raise_for_status()
    return [{"slug": e.get("slug"), "title": e.get("title"), "start": e.get("endDate")}
            for e in r.json() if (e.get("slug") or "").startswith(GAME_PREFIXES)]


# --- Kalshi -------------------------------------------------------------------------------------- #
async def _kalshi_leaders(client: httpx.AsyncClient) -> dict:
    """{nickname: {ranks: {all, 30d}, pnl: {...}}} from the public sports profit leaderboard."""
    out: dict = {}
    for window, key in ((None, "all"), (30, "30d")):
        params = {"metric_name": "projected_pnl", "limit": LEADERS, "category": "Sports"}
        if window:
            params["since_day_before"] = window
        r = await client.get(f"{config.KALSHI_SOCIAL}/leaderboard", params=params, timeout=20)
        r.raise_for_status()
        for t in r.json().get("rank_list") or []:
            n = t.get("nickname")
            if n:
                e = out.setdefault(n, {"ranks": {}, "pnl": {}})
                e["ranks"][key], e["pnl"][key] = t.get("rank"), t.get("value")
    return out


async def _kalshi_holdings(client: httpx.AsyncClient, nickname: str) -> dict | None:
    """{visible, rows: [{ticker, pos}]}: open positions, signed (+ yes contracts, - no contracts)."""
    try:
        r = await client.get(f"{config.KALSHI_SOCIAL}/profile/holdings", timeout=20,
                             params={"nickname": nickname, "limit": 100})
        if r.status_code != 200:
            return None
        body = r.json()
    except Exception as exc:  # noqa: BLE001
        print(f"[toptraders] kalshi holdings {nickname} failed: {exc}")
        return None
    rows = [{"ticker": mh.get("market_ticker"), "pos": mh.get("signed_open_position")}
            for ev in body.get("holdings") or [] for mh in ev.get("market_holdings") or []
            if mh.get("market_ticker") and mh.get("signed_open_position")]
    return {"visible": body.get("visibility_state") == "visible", "rows": rows}


# --- refresh --------------------------------------------------------------------------------------- #
async def refresh(sport: str, series: str) -> dict:
    """Bring the shared cache up to date for one sport's process and return it. Each piece refreshes on
    its own clock and keeps its last good copy through a failed call."""
    cache = _load()
    changed = False
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": "overlay/1.0"}) as client:
        for key, fn in (("poly_leaders", _poly_leaders), ("kalshi_leaders", _kalshi_leaders)):
            if _stale(cache.get(key), TTL_LEADERBOARD):
                try:
                    cache[key] = {"ts": time.time(), "rows": await fn(client)}
                    changed = True
                except Exception as exc:  # noqa: BLE001
                    print(f"[toptraders] {key} failed: {exc}")
        events = cache.setdefault("events", {})
        if series and _stale(events.get(sport), TTL_EVENTS):
            try:
                events[sport] = {"ts": time.time(), "rows": await _poly_events(client, series)}
                changed = True
            except Exception as exc:  # noqa: BLE001
                print(f"[toptraders] {sport} events failed: {exc}")
        gate = asyncio.Semaphore(6)
        positions = cache.setdefault("positions", {})

        async def poly_one(w: str) -> None:
            async with gate:
                got = await _poly_positions(client, w)
                if got is not None:
                    positions[w] = {"ts": time.time(), "rows": got}

        wallets = [w for w in (cache.get("poly_leaders") or {}).get("rows", {})
                   if _stale(positions.get(w), TTL_POSITIONS)]
        holdings = cache.setdefault("holdings", {})

        async def kalshi_one(n: str) -> None:
            async with gate:
                got = await _kalshi_holdings(client, n)
                if got is not None:
                    holdings[n] = {"ts": time.time(), **got}

        nicks = [n for n in (cache.get("kalshi_leaders") or {}).get("rows", {})
                 if _stale(holdings.get(n), TTL_POSITIONS if (holdings.get(n) or {}).get("visible") else TTL_HIDDEN)]
        if wallets or nicks:
            await asyncio.gather(*(poly_one(w) for w in wallets), *(kalshi_one(n) for n in nicks))
            changed = True
    leaders = (cache.get("poly_leaders") or {}).get("rows", {})
    for w in [w for w in positions if w not in leaders]:
        positions.pop(w)                                  # dropped off the leaderboard
    if changed:
        _save(cache)
    return cache


# --- matching the platforms' games and lines to the board ------------------------------------------ #
def match_team(name: str | None, keys) -> str | None:
    """A platform's team name -> the board's team key: exact after normalizing, else a nickname ("Cowboys"
    -> "dallas cowboys") or a bare city or school ("Utah" -> "utah mammoth") that names exactly one."""
    n = normalize_team(name or "")
    if not n:
        return None
    if n in keys:
        return n
    hits = [k for k in keys if k.endswith(" " + n) or k.startswith(n + " ")]
    return hits[0] if len(hits) == 1 else None


def map_events(events: list, games: list) -> dict:
    """{event slug: game} for this league's Polymarket events that are games on the board. A game is
    {pair, date, team_a, team_b, ...}; an event matches when both its teams map and its date is the
    game's or a day either side (Polymarket dates by UTC kickoff, the board by the US date)."""
    out = {}
    for ev in events or []:
        names = _VS.split(ev.get("title") or "")
        m = _SLUG_DATE.search(ev.get("slug") or "")
        if len(names) != 2 or not m:
            continue
        for g in games:
            keys = (g["team_a"], g["team_b"])
            ta, tb = match_team(names[0], keys), match_team(names[1], keys)
            if not ta or not tb or ta == tb:
                continue
            try:
                gap = abs((date.fromisoformat(m.group(1)) - date.fromisoformat(g["date"])).days)
            except ValueError:
                continue
            if gap <= 1:
                out[ev["slug"]] = {**g, "names": {names[0]: ta, names[1]: tb}}
                break
    return out


def classify(title: str | None, outcome: str | None, names: dict) -> dict | None:
    """A Polymarket game position -> the line it backs: {kind: ml|spread|total, team, dir, line}. Lines
    beyond the full-game moneyline, spread and total (halves, quarters, team totals) are left out."""
    title, outcome = (title or "").strip(), (outcome or "").strip()
    keys = list(names.values())
    m = _SPREAD.match(title)
    if m:
        named = match_team(m.group(1), keys)
        line = float(m.group(2))
        if not named:
            return None
        other = next((k for k in keys if k != named), None)
        mine = match_team(outcome, keys)
        if mine == named:
            return {"kind": "spread", "team": named, "line": line}
        return {"kind": "spread", "team": other, "line": -line} if mine == other else None
    m = _TOTAL.match(title)
    if m:
        d = outcome.lower()
        return {"kind": "total", "dir": d, "line": float(m.group(1))} if d in ("over", "under") else None
    if ":" not in title and len(_VS.split(title)) == 2:
        team = match_team(outcome, keys)
        return {"kind": "ml", "team": team} if team else None
    return None


def kalshi_ticker_map(markets: list, codes: dict) -> dict:
    """{ticker: {pair, date, yes: line, no: line, p_yes}} for the board's Kalshi game, spread and total
    markets, so an opted-in Kalshi trader's holding can be read as a side of a line."""
    by_team = {v: k for k, v in (codes or {}).items()}
    out: dict = {}
    for m in markets:
        mid = getattr(m, "market_id", "") or ""
        if not mid.startswith("kalshi:"):
            continue
        tkr, d = mid[len("kalshi:"):], (m.commence_time or "")[:10]
        if m.market_type == "moneyline":
            teams = [s for s in m.selections if s.key != "draw"]
            if len(teams) != 2:
                continue
            pair = frozenset(s.key for s in teams)
            for s in teams:
                code = by_team.get(s.key)
                other = next(x.key for x in teams if x.key != s.key)
                if code:
                    p = s.quotes[0].mid_prob if s.quotes else None
                    out[f"{tkr}-{code}"] = {"pair": pair, "date": d, "p_yes": p,
                                            "yes": {"kind": "ml", "team": s.key}, "no": {"kind": "ml", "team": other}}
        elif m.market_type in ("spread", "total"):
            parts = (m.group or "").split("|")
            if len(parts) < 3 or not m.selections:
                continue
            pair = frozenset(parts[1:3])
            try:
                line = float(m.selections[0].key.split("_", 1)[1])
            except (IndexError, ValueError):
                continue
            p = m.selections[0].quotes[0].mid_prob if m.selections[0].quotes else None
            if m.market_type == "spread" and len(parts) >= 4:
                cover = parts[3]
                other = next((k for k in parts[1:3] if k != cover), None)
                out[tkr] = {"pair": pair, "date": d, "p_yes": p,
                            "yes": {"kind": "spread", "team": cover, "line": -line},
                            "no": {"kind": "spread", "team": other, "line": line}}
            elif m.market_type == "total":
                out[tkr] = {"pair": pair, "date": d, "p_yes": p,
                            "yes": {"kind": "total", "dir": "over", "line": line},
                            "no": {"kind": "total", "dir": "under", "line": line}}
    return out


# --- assembling the board ------------------------------------------------------------------------ #
def _poly_trader(e: dict) -> dict:
    """A Polymarket leader as shown: best rank (and its window), that window's profit, and the
    market-maker flag judged on the longest record available."""
    ranks, pnl, vol = e.get("ranks") or {}, e.get("pnl") or {}, e.get("vol") or {}
    window, rank = min(ranks.items(), key=lambda x: x[1]) if ranks else (None, None)
    span = "all" if vol.get("all") else "month"
    v, p = vol.get(span) or 0, pnl.get(span) or 0
    return {"name": e.get("name"), "platform": "Polymarket", "rank": rank,
            "window": "this month" if window == "month" else "all-time", "pnl": pnl.get(window),
            "mm": bool(v >= MM_VOLUME and p / v < MM_MARGIN)}


def _kalshi_trader(n: str, e: dict) -> dict:
    ranks, pnl = e.get("ranks") or {}, e.get("pnl") or {}
    best = min(ranks.items(), key=lambda x: x[1]) if ranks else (None, None)
    return {"name": n, "platform": "Kalshi", "rank": best[1],
            "window": "30 days" if best[0] == "30d" else "all-time",
            "pnl": pnl.get(best[0]) if best[0] else None, "mm": False}


def _line_key(line: dict) -> tuple:
    return (line.get("kind"), line.get("team") or line.get("dir"), line.get("line"))


def assemble(cache: dict, sport: str, games: list, ticker_map: dict) -> dict:
    """Per board game: every line a top trader holds, with who holds it and how much, plus the moneyline
    consensus (directional traders only); and the leaders holding anything on the board."""
    pm_events = map_events(((cache.get("events") or {}).get(sport) or {}).get("rows") or [], games)
    pm_leaders = (cache.get("poly_leaders") or {}).get("rows") or {}
    k_leaders = (cache.get("kalshi_leaders") or {}).get("rows") or {}
    by_game: dict = {}

    def add(game_key, line, row):
        g = by_game.setdefault(game_key, {})
        g.setdefault(_line_key(line), {**line, "rows": []})["rows"].append(row)

    for w, entry in ((cache.get("positions") or {}).items()):
        if w not in pm_leaders:
            continue
        who = _poly_trader(pm_leaders[w])
        held: dict = {}                                   # condition -> {outcomeIndex: position}
        for p in entry.get("rows") or []:
            if p.get("eventSlug") in pm_events:
                held.setdefault(p.get("conditionId"), {})[p.get("outcomeIndex")] = p
        for cond, sides in held.items():
            two = len(sides) > 1
            if two:                                       # both outcomes: net the shares, keep the bigger side
                (ia, pa), (ib, pb) = sorted(sides.items(), key=lambda x: -(x[1].get("size") or 0))[:2]
                net = (pa.get("size") or 0) - (pb.get("size") or 0)
                p, stake = pa, net * (pa.get("avgPrice") or 0)
            else:
                p = next(iter(sides.values()))
                stake = p.get("initialValue") or (p.get("size") or 0) * (p.get("avgPrice") or 0)
            g = pm_events[p["eventSlug"]]
            line = classify(p.get("title"), p.get("outcome"), g["names"])
            if not line or stake < MIN_STAKE:
                continue
            add((g["pair"], g["date"]), line, {**who, "stake": round(stake), "price": p.get("avgPrice"),
                                               "two_sided": two})
    k_visible = 0
    for n, h in (cache.get("holdings") or {}).items():
        if n not in k_leaders:
            continue
        k_visible += 1 if h.get("visible") else 0
        who = _kalshi_trader(n, k_leaders[n])
        for r in h.get("rows") or []:
            t = ticker_map.get(r.get("ticker"))
            if not t:
                continue
            yes = (r.get("pos") or 0) > 0
            contracts = abs(r.get("pos") or 0)
            p = t.get("p_yes")
            price = (p if yes else 1 - p) if p is not None else None
            if (contracts * price if price is not None else contracts) < MIN_STAKE:
                continue
            add((t["pair"], t["date"]), t["yes"] if yes else t["no"],
                {**who, "stake": round(contracts * price) if price is not None else None, "price": price,
                 "contracts": contracts, "two_sided": False})
    out_games, leaders = [], {}
    for g in games:
        gk = (frozenset((g["team_a"], g["team_b"])), g["date"])
        lines = list((by_game.get(gk) or {}).values())
        if not lines:
            continue
        for ln in lines:
            ln["rows"].sort(key=lambda r: -(r.get("stake") or 0))
            ln["traders"] = len({(r["platform"], r["name"]) for r in ln["rows"]})
            ln["stake"] = sum(r.get("stake") or 0 for r in ln["rows"])
            for r in ln["rows"]:
                k = (r["platform"], r["name"])
                L = leaders.setdefault(k, {**{x: r[x] for x in ("name", "platform", "rank", "window", "pnl", "mm")},
                                           "lines": 0, "stake": 0})
                L["lines"] += 1
                L["stake"] += r.get("stake") or 0
        lines.sort(key=lambda ln: ({"ml": 0, "spread": 1, "total": 2}.get(ln["kind"], 3), -ln["stake"]))
        out_games.append({**{k: g.get(k) for k in ("team_a", "team_b", "date", "kickoff_iso", "market")},
                          "lines": lines, "stake": sum(ln["stake"] for ln in lines),
                          "consensus": consensus(lines)})
    return {"games": out_games,
            "leaders": sorted(leaders.values(), key=lambda x: -x["stake"])[:40],
            "meta": {"poly_traders": len(pm_leaders), "kalshi_traders": len(k_leaders),
                     "kalshi_visible": k_visible, "games_listed": len(games), "events_matched": len(pm_events),
                     "updated": max([(v or {}).get("ts") or 0 for v in (cache.get("positions") or {}).values()] or [0])}}


def consensus(lines: list) -> dict | None:
    """The moneyline side holding most of the directional top-trader money, when it is clear: at least
    $1,000 in all and 60% on one side. Two-sided holders and flagged market makers do not count."""
    money: dict = {}
    traders: dict = {}
    for ln in lines:
        if ln.get("kind") != "ml":
            continue
        for r in ln["rows"]:
            if r.get("two_sided") or r.get("mm") or not r.get("stake"):
                continue
            money[ln["team"]] = money.get(ln["team"], 0) + r["stake"]
            traders.setdefault(ln["team"], set()).add((r["platform"], r["name"]))
    total = sum(money.values())
    if total < CONSENSUS_MIN:
        return None
    team, stake = max(money.items(), key=lambda x: x[1])
    share = stake / total
    if share < CONSENSUS_SHARE:
        return None
    return {"team": team, "stake": round(stake), "share": round(share, 3), "traders": len(traders[team]),
            "total": round(total)}
