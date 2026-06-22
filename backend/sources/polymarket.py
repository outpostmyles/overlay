"""Polymarket adapter (free, public Gamma API — no key needed).

Polymarket is our deep *futures* source: tournament winner (60 teams), all 12 group winners,
and advancement (reach R16/QF/SF). Each team is its own Yes/No market whose Yes price == the
implied probability that team wins / advances.

Enumeration: public-search returns ~40-100 World Cup events with nested, priced markets in a
single call. We classify by slug (robust) and keep only the markets that align cross-source.
"""
from __future__ import annotations

import json
import re

import httpx

from .. import config
from ..matching import normalize_team
from ..models import Market, Quote, Selection

_GROUP_RE = re.compile(r"^world-cup-group-[a-l]-winner$")


def _classify(slug: str) -> str | None:
    if slug == "world-cup-winner":
        return "winner_outright"
    if _GROUP_RE.match(slug):
        return "group_winner"
    if slug.startswith("world-cup-nation-to-reach-round-of-16"):
        return "advance_r16"
    if slug.startswith("world-cup-nation-to-reach-quarterfinals"):
        return "advance_qf"
    if slug.startswith("world-cup-nation-to-reach-semifinals"):
        return "advance_sf"
    return None


def _num(v) -> float | None:
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _parse(val):
    """Polymarket returns some array fields as JSON-encoded strings."""
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            return []
    return []


async def _enumerate_events(client: httpx.AsyncClient) -> list[dict]:
    events: dict[str, dict] = {}
    try:
        resp = await client.get(
            f"{config.POLYMARKET_GAMMA}/public-search",
            params={"q": "world cup", "limit_per_type": 100, "events_status": "active"},
            headers={"Accept": "application/json"},
            timeout=25,
        )
        resp.raise_for_status()
        for e in resp.json().get("events", []):
            if e.get("slug"):
                events[e["slug"]] = e
    except Exception as exc:  # noqa: BLE001
        print(f"[polymarket] search failed: {exc}")
    # guarantee the winner market even if search relevance drops it
    if "world-cup-winner" not in events:
        try:
            resp = await client.get(
                f"{config.POLYMARKET_GAMMA}/events",
                params={"slug": "world-cup-winner"},
                headers={"Accept": "application/json"},
                timeout=25,
            )
            resp.raise_for_status()
            for e in resp.json():
                if e.get("slug"):
                    events[e["slug"]] = e
        except Exception as exc:  # noqa: BLE001
            print(f"[polymarket] winner fetch failed: {exc}")
    return list(events.values())


async def fetch(client: httpx.AsyncClient) -> list[Market]:
    markets: list[Market] = []
    for ev in await _enumerate_events(client):
        mtype = _classify(ev.get("slug", ""))
        if not mtype:
            continue
        slug = ev.get("slug", "")
        link = f"https://polymarket.com/event/{slug}" if slug else None
        selections: list[Selection] = []
        for m in ev.get("markets") or []:
            team = m.get("groupItemTitle") or m.get("question")
            if not team:
                continue
            key = normalize_team(team)
            # Skip undecided playoff placeholders ("Team AM"/"Team AO") and "Other" — these
            # default to 0.50 with no trading and would corrupt the de-vig.
            if not key or key.startswith("team ") or key == "other":
                continue
            outcomes = _parse(m.get("outcomes"))
            prices = _parse(m.get("outcomePrices"))
            # The "Yes" outcomePrice is Polymarket's mark price — the reliable probability.
            # (lastTradePrice can be a stale glitch, e.g. a resolved-longshot showing 1.0.)
            mark = None
            if outcomes and prices and len(outcomes) == len(prices):
                for o, p in zip(outcomes, prices):
                    if str(o).lower() == "yes":
                        mark = _num(p)
            ask = _num(m.get("bestAsk"))
            bid = _num(m.get("bestBid"))
            back_price = ask or mark            # executable price to buy Yes
            if not back_price:
                continue
            # mid (for the fair line): prefer the mark, else order-book midpoint — never last.
            if mark:
                mid = mark
            elif bid and ask:
                mid = (bid + ask) / 2.0
            else:
                mid = back_price
            selections.append(
                Selection(
                    key=key,
                    label=team,
                    quotes=[
                        Quote(
                            source="polymarket",
                            source_type="prediction_market",
                            price_decimal=1.0 / back_price,
                            implied_prob=back_price,
                            mid_prob=mid,
                            fee=config.POLYMARKET_SPORTS_FEE,
                            bid=bid,
                            ask=ask,
                            volume=_num(m.get("volume")),
                            link=link,
                        )
                    ],
                )
            )
        if len(selections) >= 2:
            markets.append(
                Market(
                    market_id=f"pm:{slug}",
                    event=ev.get("title", "").strip(),
                    market_type=mtype,
                    selections=selections,
                    commence_time=None,  # futures have no kickoff; startDate is creation date
                    group="Futures",
                )
            )
    return markets
