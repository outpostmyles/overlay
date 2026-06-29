"""Kalshi adapter (free, public market-data API — no key needed for reads).

Kalshi is our deep *per-match* source: every World Cup game has a 3-way moneyline
(team / tie / team), plus spreads and totals. It also lists the tournament winner.
Prices are in cents (0-100) == probability * 100.
"""
from __future__ import annotations

import re

import httpx

from .. import config
from ..matching import kalshi_ticker_date, normalize_team
from ..models import Market, Quote, Selection

# knockout games are listed as regulation-time markets ("Reg Time: Germany"); strip the wrapper from
# the display label so cards read "Germany", not "Reg Time: Germany"
_REG_TIME = re.compile(r"\breg(?:ular|ulation)?\.?\s*time\b\s*:?\s*", re.IGNORECASE)

# series_ticker -> (market_type, group)
# v1 covers the cleanly-comparable markets: 3-way moneyline + tournament winner.
# Totals/spreads are nested (over 5.5 / over 6.5 …) so they aren't a mutually-exclusive set —
# de-vigging across them is invalid. They'll return in Phase 2 with per-line handling.
_SERIES = {
    "KXWCGAME": ("moneyline", "Matches"),
    "KXMENWORLDCUP": ("winner_outright", "Futures"),
}


def _prob(dollars) -> float | None:
    """Kalshi prices are in dollars (0.00-1.00) per $1 contract == probability directly."""
    try:
        d = float(dollars)
        return d if 0.0 < d < 1.0 else None
    except (TypeError, ValueError):
        return None


async def _fetch_series(client: httpx.AsyncClient, series: str, status: str = "open") -> list[dict]:
    out: list[dict] = []
    cursor = None
    for _ in range(20):  # safety cap on pagination
        params = {"series_ticker": series, "limit": 1000, "status": status}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = await client.get(
                f"{config.KALSHI_API}/markets",
                params=params,
                headers={"Accept": "application/json"},
                timeout=25,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            print(f"[kalshi] {series} fetch failed: {exc}")
            break
        out.extend(data.get("markets") or [])
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


async def fetch(client: httpx.AsyncClient) -> list[Market]:
    markets: list[Market] = []
    for series, (mtype, group) in _SERIES.items():
        raw = await _fetch_series(client, series)
        # group the per-outcome markets by their parent event
        events: dict[str, list[dict]] = {}
        for m in raw:
            ev_ticker = m.get("event_ticker") or m.get("ticker", "").rsplit("-", 1)[0]
            events.setdefault(ev_ticker, []).append(m)

        for ev_ticker, children in events.items():
            selections: list[Selection] = []
            title = ""
            for m in children:
                label = _REG_TIME.sub("", m.get("yes_sub_title") or m.get("title") or "").strip()
                title = _REG_TIME.sub("", (m.get("title") or title)).replace(" Winner?", "").strip()
                ask = _prob(m.get("yes_ask_dollars"))
                bid = _prob(m.get("yes_bid_dollars"))
                last = _prob(m.get("last_price_dollars"))
                back = ask or last
                if not back:
                    continue  # no executable price / no liquidity yet
                mid = (bid + ask) / 2.0 if (bid and ask) else (last or back)
                selections.append(
                    Selection(
                        key=normalize_team(label),
                        label=label,
                        quotes=[
                            Quote(
                                source="kalshi",
                                source_type="prediction_market",
                                price_decimal=1.0 / back,
                                implied_prob=back,
                                mid_prob=mid,
                                fee=config.KALSHI_FEE_COEF,
                                bid=bid,
                                ask=ask,
                                volume=m.get("volume_fp"),
                                link=f"https://kalshi.com/markets/{series.lower()}",
                            )
                        ],
                    )
                )
            if len(selections) < 2:
                continue
            event_name = title if mtype != "winner_outright" else "World Cup Winner"
            markets.append(
                Market(
                    market_id=f"kalshi:{ev_ticker}",
                    event=event_name,
                    market_type=mtype,
                    selections=selections,
                    commence_time=kalshi_ticker_date(ev_ticker),
                    group=group,
                )
            )
    return markets


async def fetch_resolved(client: httpx.AsyncClient) -> list[dict]:
    """Settled per-match (KXWCGAME) outcomes, for free auto-grading of favorite-ML paper picks.

    Each child market is a Yes/No on one outcome (team-to-win or Tie); a settled market's
    `result` is "yes"/"no". Returns one row per team outcome:
    {date, team_key, label, result: won|lost|void, match}. A favorite-ML pick on a team that
    drew or lost both resolve "no" → lost (the draw-is-a-loss reality of a 3-way ML)."""
    out: list[dict] = []
    seen: set[str] = set()
    for status in ("settled", "closed"):  # Kalshi rejects status=finalized with a 400
        for m in await _fetch_series(client, "KXWCGAME", status=status):
            tkr = m.get("ticker") or ""
            if tkr in seen:
                continue
            seen.add(tkr)
            label = m.get("yes_sub_title") or ""
            res = (m.get("result") or "").lower()
            if not label or res not in ("yes", "no"):
                continue  # unresolved/void or the Tie leg with no usable result
            ev_ticker = m.get("event_ticker") or tkr.rsplit("-", 1)[0]
            out.append({
                "date": kalshi_ticker_date(ev_ticker),
                "team_key": normalize_team(label),
                "label": label,
                "result": "won" if res == "yes" else "lost",
                "match": (m.get("title") or "").replace(" Winner?", "").strip(),
            })
    return out
