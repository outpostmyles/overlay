"""The Odds API adapter (optional — needs a free key in .env).

CREDIT-CONSCIOUS: the free tier is 500 credits/month. This adapter makes ONE call requesting
only the h2h (moneyline) market in the us region == 1 credit, and reports the remaining-credit
count from the response header so the dashboard can show it. It is only ever invoked on an
explicit manual refresh (see aggregator.get_odds_markets), never on the auto-refresh loop.

The moneyline markets it returns merge with Kalshi's by (date + teams), so a sportsbook price
gets measured against the sharp prediction-market fair line -> real per-match +EV.
"""
from __future__ import annotations

import httpx

from .. import config
from ..matching import iso_date, normalize_team
from ..models import Market, Quote, Selection


def _decimal(price) -> float | None:
    try:
        d = float(price)
        return d if d > 1.0 else None
    except (TypeError, ValueError):
        return None


async def fetch(client: httpx.AsyncClient) -> dict:
    """Returns {markets, credits_remaining, ok, error}. Costs 1 credit when it reaches the API."""
    if not config.ODDS_API_KEY:
        return {"markets": [], "credits_remaining": None, "ok": False, "error": "no key"}
    try:
        resp = await client.get(
            f"{config.ODDS_API}/sports/{config.ODDS_API_SPORT}/odds",
            params={
                "apiKey": config.ODDS_API_KEY,
                "regions": config.ODDS_API_REGIONS,
                "markets": config.ODDS_MARKETS,
                "oddsFormat": "decimal",
            },
            headers={"Accept": "application/json"},
            timeout=25,
        )
        credits = resp.headers.get("x-requests-remaining")
        credits = int(credits) if credits not in (None, "") else None
        if resp.status_code in (404, 422):
            # sport key not active yet (e.g. between tournaments) — don't treat as data
            return {"markets": [], "credits_remaining": credits, "ok": False,
                    "error": f"status {resp.status_code}"}
        resp.raise_for_status()
        events = resp.json()
    except Exception as exc:  # noqa: BLE001
        print(f"[theoddsapi] fetch failed: {exc}")
        return {"markets": [], "credits_remaining": None, "ok": False, "error": str(exc)}

    markets = _parse_events(events)
    print(f"[theoddsapi] fetched {len(markets)} h2h markets; credits remaining: {credits}")
    return {"markets": markets, "credits_remaining": credits, "ok": True, "error": None}


async def fetch_corners(client: httpx.AsyncClient, targets=None, max_games: int = 10) -> dict:
    """Total-corner lines per slate game from The Odds API `alternate_totals_corners`.

    Corners are an event-level market, so this is one (free) /events call to list games + ONE CREDIT
    PER GAME for the corner odds — pricier than h2h. `targets` (a set/iterable of frozenset(team_keys))
    restricts the spend to the games the caller actually wants (the near slate), which both caps
    credits and guarantees the lines line up with our matchups. Returns
    ({(date, frozenset team_keys): {line, over_price, under_price, over_book, under_book, fair_over}},
    credits_remaining); the chosen line is the most balanced (most "main") point offered, de-vigged."""
    if not config.ODDS_API_KEY:
        return {}, None
    base, sport, key = config.ODDS_API, config.ODDS_API_SPORT, config.ODDS_API_KEY
    target_sets = set(targets) if targets is not None else None
    out: dict = {}
    credits = None
    try:
        ev = await client.get(f"{base}/sports/{sport}/events", params={"apiKey": key}, timeout=25)
        credits = ev.headers.get("x-requests-remaining") or credits
        events = ev.json() if ev.status_code == 200 else []
    except Exception as exc:  # noqa: BLE001
        print(f"[theoddsapi] corner events failed: {exc}")
        return {}, None

    # keep only the games we actually want a corner line for, soonest first, capped
    wanted = []
    for e in events:
        teams = frozenset(normalize_team(t) for t in (e.get("home_team"), e.get("away_team")) if t)
        if len(teams) < 2 or not e.get("id"):
            continue
        if target_sets is not None and teams not in target_sets:
            continue
        wanted.append(e)
    wanted.sort(key=lambda e: e.get("commence_time") or "")

    for e in wanted[:max_games]:
        eid = e.get("id")
        teams = frozenset(normalize_team(t) for t in (e.get("home_team"), e.get("away_team")) if t)
        date = iso_date(e.get("commence_time"))
        try:
            r = await client.get(
                f"{base}/sports/{sport}/events/{eid}/odds",
                params={"apiKey": key, "regions": config.ODDS_API_REGIONS,
                        "markets": "alternate_totals_corners", "oddsFormat": "decimal"},
                timeout=25,
            )
            credits = r.headers.get("x-requests-remaining") or credits
            if r.status_code != 200:
                continue
            body = r.json()
        except Exception as exc:  # noqa: BLE001
            print(f"[theoddsapi] corners {eid} failed: {exc}")
            continue

        # best over/under decimal per point across books
        ladder: dict = {}   # point -> {"over": (dec, book), "under": (dec, book)}
        for bk in body.get("bookmakers", []):
            src = bk.get("key", "book")
            for mk in bk.get("markets", []):
                if mk.get("key") != "alternate_totals_corners":
                    continue
                for oc in mk.get("outcomes", []):
                    pt = oc.get("point")
                    side = (oc.get("name") or "").lower()
                    dec = _decimal(oc.get("price"))
                    if pt is None or dec is None or side not in ("over", "under"):
                        continue
                    slot = ladder.setdefault(pt, {})
                    if side not in slot or dec > slot[side][0]:
                        slot[side] = (dec, src)

        # "main" line = the most balanced two-way (implied probs closest). Prefer HALF-point lines
        # (no push); only fall back to a whole-number point if no .5 line is offered.
        two_way = [(pt, slot) for pt, slot in ladder.items() if "over" in slot and "under" in slot]
        half = [(pt, slot) for pt, slot in two_way if pt != int(pt)]
        candidates = half or two_way
        best = None
        for pt, slot in candidates:
            bal = abs(1.0 / slot["over"][0] - 1.0 / slot["under"][0])
            if best is None or bal < best[0]:
                best = (bal, pt, slot)
        if not best:
            continue
        _, pt, slot = best
        io, iu = 1.0 / slot["over"][0], 1.0 / slot["under"][0]
        out[(date, teams)] = {
            "line": pt,
            "over_price": slot["over"][0], "over_book": slot["over"][1],
            "under_price": slot["under"][0], "under_book": slot["under"][1],
            "fair_over": round(io / (io + iu), 4),
        }
    credits = int(credits) if credits not in (None, "") else None
    if out:
        print(f"[theoddsapi] corner lines for {len(out)} game(s); credits remaining: {credits}")
    return out, credits


def _parse_events(events: list[dict]) -> list[Market]:
    out: list[Market] = []
    for ev in events:
        commence = iso_date(ev.get("commence_time"))
        event_name = f"{ev.get('home_team', '?')} vs {ev.get('away_team', '?')}"
        sels: dict[str, Selection] = {}
        for book in ev.get("bookmakers", []):
            src = book.get("key", "book")
            for mk in book.get("markets", []):
                if mk.get("key") != "h2h":
                    continue
                for oc in mk.get("outcomes", []):
                    name = oc.get("name", "")
                    dec = _decimal(oc.get("price"))
                    if not dec:
                        continue
                    key = normalize_team(name)   # "Draw" -> "draw", matches Kalshi "Tie"
                    sel = sels.get(key) or Selection(key=key, label=name)
                    sel.quotes.append(
                        Quote(
                            source=src,
                            source_type="sportsbook",
                            price_decimal=dec,
                            implied_prob=1.0 / dec,
                            mid_prob=1.0 / dec,
                        )
                    )
                    sels[key] = sel
        if len(sels) >= 2:
            out.append(
                Market(
                    market_id=f"odds:{ev.get('id', event_name)}",
                    event=event_name,
                    market_type="moneyline",
                    selections=list(sels.values()),
                    commence_time=commence,
                    group="Matches",
                )
            )
    return out
