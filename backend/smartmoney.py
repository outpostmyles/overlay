"""Smart money — Polymarket whale positions + money flow on the actual game markets.

Scoped to the user's bets: he plays match moneylines, not tournament/group winners. So instead of
the World Cup *winner* market (where whales just scalp heavy favorites), this tracks Polymarket's
per-game markets (`fifwc-{abbr}-{abbr}-{date}`) for the favorites on the slate — answering "who are
the big wallets backing in tonight's actual games?"

Uses Polymarket's free public data API (no key): `/holders` (top position holders per token) and
`/trades` (recent fills). For each game we pull the favorite's and the underdog's "Yes" (team-to-win)
outcome: largest backer, holder count, and recent net flow (BUY − SELL shares).

Honest framing: large positions signal conviction, not a guarantee — a whale is not necessarily a
*winning* whale, and game-market volume is thinner than the headline futures market.
"""
from __future__ import annotations

import asyncio
import json

import httpx

from . import config
from .matching import normalize_team


def _parse(val):
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            return []
    return []


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


async def _outcome_signal(client: httpx.AsyncClient, team: str, cond: str, price: float) -> dict:
    """Whale holders + recent net flow on one team's 'Yes' (team-to-win) game-market outcome."""
    # top holders (per-token; outcomeIndex 0 == "Yes" = team wins)
    yes_holders = []
    try:
        r = await client.get(f"{config.POLYMARKET_DATA}/holders",
                             params={"market": cond, "limit": 50}, timeout=15)
        for tok in (r.json() if r.status_code == 200 else []):
            if not isinstance(tok, dict):
                continue
            for h in tok.get("holders", []):
                if h.get("outcomeIndex") == 0:
                    yes_holders.append(h)
    except Exception as exc:  # noqa: BLE001
        print(f"[smartmoney] holders failed for {team}: {exc}")
    yes_holders.sort(key=lambda x: x.get("amount") or 0, reverse=True)
    top = yes_holders[0] if yes_holders else {}

    # recent flow on the Yes side
    net = 0.0
    yes_trades = []
    try:
        r = await client.get(f"{config.POLYMARKET_DATA}/trades",
                             params={"market": cond, "limit": 100}, timeout=15)
        for tr in (r.json() if r.status_code == 200 else []):
            if not isinstance(tr, dict) or str(tr.get("outcome", "")).lower() != "yes":
                continue
            yes_trades.append(tr)
            size = _num(tr.get("size"))
            if tr.get("side") == "BUY":
                net += size
            elif tr.get("side") == "SELL":
                net -= size
    except Exception as exc:  # noqa: BLE001
        print(f"[smartmoney] trades failed for {team}: {exc}")
    yes_trades.sort(key=lambda x: _num(x.get("size")), reverse=True)

    return {
        "team": team,
        "team_key": normalize_team(team),
        "implied_pct": round(price * 100, 1),
        "top_holder": (top.get("name") or top.get("pseudonym") or None),
        "top_holder_shares": round(top.get("amount") or 0),
        "num_holders": len(yes_holders),
        "net_flow_shares": round(net),
        "flow": "buying" if net > 50 else "selling" if net < -50 else "flat",
        "big_trades": [
            {"side": tr.get("side"), "shares": round(_num(tr.get("size"))),
             "price": tr.get("price"), "who": (tr.get("name") or tr.get("pseudonym") or "anon")}
            for tr in yes_trades[:3]
        ],
    }


_slug_cache: dict[str, str] = {}   # "fav|opp" -> polymarket fifwc slug (stable per fixture; resolve once)


async def _game_event(client: httpx.AsyncClient, fav_team: str, opp_team: str) -> dict | None:
    """Find the Polymarket per-game event (slug `fifwc-...`) for a matchup, with full markets. The
    slug→event resolution is stable, so we cache it and skip the public-search call after the first
    hit; only the /events fetch (fresh markets/prices) and holders/trades re-run each cycle."""
    ckey = "|".join(sorted([fav_team, opp_team]))
    slug = _slug_cache.get(ckey)
    if not slug:
        try:
            r = await client.get(f"{config.POLYMARKET_GAMMA}/public-search",
                                 params={"q": f"{fav_team} {opp_team}", "limit_per_type": 20,
                                         "events_status": "active"},
                                 headers={"Accept": "application/json"}, timeout=20)
            slug = next((e.get("slug") for e in (r.json().get("events", []) if r.status_code == 200 else [])
                         if (e.get("slug") or "").startswith("fifwc-")), None)
        except Exception as exc:  # noqa: BLE001
            print(f"[smartmoney] search failed for {fav_team} v {opp_team}: {exc}")
            return None
        if not slug:
            return None
        _slug_cache[ckey] = slug
    try:  # fetch full event by slug — search results carry light market data
        r = await client.get(f"{config.POLYMARKET_GAMMA}/events", params={"slug": slug},
                             headers={"Accept": "application/json"}, timeout=20)
        evs = r.json() if r.status_code == 200 else []
        return evs[0] if evs else None
    except Exception as exc:  # noqa: BLE001
        print(f"[smartmoney] event fetch failed for {slug}: {exc}")
        return None


async def _match_signal(client: httpx.AsyncClient, mu: dict) -> dict | None:
    """For one slate matchup, return whale signals on the favorite and underdog game outcomes."""
    ev = await _game_event(client, mu["fav_team"], mu["opp_team"])
    if not ev:
        return None
    legs: dict[str, tuple] = {}  # team_key -> (label, conditionId, yes_price)
    for m in ev.get("markets") or []:
        team = m.get("groupItemTitle") or m.get("question")
        cond = m.get("conditionId")
        if not team or not cond:
            continue
        key = normalize_team(team)
        if key.startswith("draw"):  # skip the Draw leg
            continue
        prices = _parse(m.get("outcomePrices"))
        legs[key] = (team, cond, _num(prices[0]) if prices else 0.0)

    match = (ev.get("title") or mu.get("event") or "").strip()
    out = {"favorite": None, "underdog": None}
    for role, tkey, opp_label in (("favorite", mu["fav_key"], mu["opp_team"]),
                                  ("underdog", mu["opp_key"], mu["fav_team"])):
        leg = legs.get(tkey)
        if not leg:
            continue
        label, cond, price = leg
        sig = await _outcome_signal(client, label, cond, price)
        if not sig["num_holders"] and not sig["big_trades"]:
            continue  # no whale data on this side — don't surface an empty row
        sig.update(match=match, is_favorite=(role == "favorite"), opponent=opp_label,
                   commence_time=mu.get("commence_time"), days_out=mu.get("days_out"))
        out[role] = sig
    return out if (out["favorite"] or out["underdog"]) else None


async def fetch(client: httpx.AsyncClient, matchups: list[dict]) -> dict:
    """Return {team_key: signal} for the favorite/underdog of each slate game with whale data."""
    sem = asyncio.Semaphore(4)

    async def _one(mu):
        async with sem:
            try:
                return await _match_signal(client, mu)
            except Exception as exc:  # noqa: BLE001
                print(f"[smartmoney] match errored ({mu.get('event')}): {exc}")
                return None

    results = await asyncio.gather(*[_one(mu) for mu in matchups])
    out: dict[str, dict] = {}
    games = 0
    for res in results:
        if not res:
            continue
        games += 1
        for role in ("favorite", "underdog"):
            sig = res.get(role)
            if sig and sig.get("team_key"):
                out[sig["team_key"]] = sig
    print(f"[smartmoney] {games}/{len(matchups)} games with game-market whale data "
          f"({len(out)} sides)")
    return out
