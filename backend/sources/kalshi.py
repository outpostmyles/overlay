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
from ..sports import active

# soccer knockout games are listed as regulation-time markets ("Reg Time: Germany"); strip the
# wrapper from display labels (adapter-gated) so cards read "Germany", not "Reg Time: Germany"
_REG_TIME = re.compile(r"\breg(?:ular|ulation)?\.?\s*time\b\s*:?\s*", re.IGNORECASE)


def _clean(text: str) -> str:
    """Sport-aware label cleanup. The series registry itself lives on the adapter: v1 covers the
    cleanly-comparable markets (game moneyline + outright). Totals/spreads are nested per line so
    they aren't a mutually-exclusive set; de-vigging across them is invalid (Phase 2, per-line)."""
    text = text or ""
    if active().kalshi_strip_reg_time:
        text = _REG_TIME.sub("", text)
    return text


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


# market types parsed one-market-PER-LINE: each child (e.g. "Over 8.5 runs", "Judge: 2+ HR") is its own
# 2-way yes/no book. De-vig happens within that pair only; lines of one event are NOT mutually exclusive.
_PER_LINE_TYPES = ("total", "player_prop")


def _prop_pair(event_ticker: str, code_map: dict) -> tuple | None:
    """'KXMLBHIT-26JUL111610CLEMIA-...' -> the two team keys, split greedily against the code map the
    game series taught us (ticker team codes concatenate without a separator)."""
    parts = (event_ticker or "").split("-")
    if len(parts) < 2 or len(parts[1]) <= 11:
        return None
    seg = parts[1][11:]                       # after ddMONyyHHMM
    for c1 in sorted(code_map, key=len, reverse=True):
        if seg.startswith(c1) and seg[len(c1):] in code_map:
            return code_map[c1], code_map[seg[len(c1):]]
    return None


def _per_line_market(m: dict, series: str, mtype: str, group: str, code_map: dict) -> Market | None:
    """One Kalshi child (a single line) -> a 2-way Market (over/yes vs under/no)."""
    tkr = m.get("ticker") or ""
    ask = _prob(m.get("yes_ask_dollars"))
    bid = _prob(m.get("yes_bid_dollars"))
    last = _prob(m.get("last_price_dollars"))
    over = ask or last
    if not over or not tkr:
        return None                              # no executable price / no liquidity
    mid_over = (bid + ask) / 2.0 if (bid and ask) else (last or over)
    under = (1.0 - bid) if bid else (1.0 - mid_over)   # the no side's executable ask
    label = _clean(m.get("yes_sub_title") or m.get("title") or "").strip()
    line = m.get("floor_strike")

    def q(prob, mid):
        return Quote(source="kalshi", source_type="prediction_market", price_decimal=1.0 / prob,
                     implied_prob=prob, mid_prob=mid, fee=config.KALSHI_FEE_COEF,
                     volume=m.get("volume_fp"), link=f"https://kalshi.com/markets/{series.lower()}")

    ev_ticker = m.get("event_ticker") or tkr.rsplit("-", 1)[0]
    if mtype == "player_prop":                # pack the game pair in so props can join their game
        pair = _prop_pair(ev_ticker, code_map)
        if pair:
            group = f"{group}|{pair[0]}|{pair[1]}"
    return Market(
        market_id=f"kalshi:{tkr}",
        event=_clean(m.get("title") or "").rstrip("?").strip(),
        market_type=mtype,
        selections=[
            Selection(key=f"over_{line}", label=label, quotes=[q(over, mid_over)]),
            Selection(key=f"under_{line}", label=f"Under ({label})", quotes=[q(under, 1.0 - mid_over)]),
        ],
        commence_time=kalshi_ticker_date(ev_ticker),
        group=group,
    )


async def fetch(client: httpx.AsyncClient) -> list[Market]:
    markets: list[Market] = []
    code_map: dict = {}   # ticker team code -> team key, learned from the game series' child suffixes
    for series, (mtype, group) in active().kalshi_series.items():
        raw = await _fetch_series(client, series)
        if mtype in _PER_LINE_TYPES:
            for m in raw:
                built = _per_line_market(m, series, mtype, group, code_map)
                if built:
                    markets.append(built)
            continue
        # group the per-outcome markets by their parent event
        events: dict[str, list[dict]] = {}
        for m in raw:
            ev_ticker = m.get("event_ticker") or m.get("ticker", "").rsplit("-", 1)[0]
            events.setdefault(ev_ticker, []).append(m)

        for ev_ticker, children in events.items():
            selections: list[Selection] = []
            title = ""
            for m in children:
                label = _clean(m.get("yes_sub_title") or m.get("title") or "").strip()
                title = _clean(m.get("title") or title).replace(" Winner?", "").strip()
                if mtype == "moneyline" and label:      # teach the prop parser this team's ticker code
                    code = (m.get("ticker") or "").rsplit("-", 1)[-1]
                    if code:
                        code_map[code] = normalize_team(label)
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
            event_name = title if mtype != "winner_outright" else (active().kalshi_outright_event or title)
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
    """Settled per-game outcomes (the adapter's resolved series), for free auto-grading of
    favorite-ML paper picks.

    Each child market is a Yes/No on one outcome (team-to-win or Tie); a settled market's
    `result` is "yes"/"no". Returns one row per team outcome:
    {date, team_key, label, result: won|lost|void, match}.

    3-way (soccer): a team that drew or lost both resolve "no" -> lost (draw-is-a-loss reality).
    2-way (MLB/UFC): an event where NEITHER leg settled "yes" is a postponement/cancellation, not a
    loss; both legs grade VOID so a rainout can never poison the track record."""
    two_way = "draw" not in active().outcomes
    events: dict[str, list[dict]] = {}
    seen: set[str] = set()
    for status in ("settled", "closed"):  # Kalshi rejects status=finalized with a 400
        for m in await _fetch_series(client, active().kalshi_resolved_series, status=status):
            tkr = m.get("ticker") or ""
            if tkr in seen:
                continue
            seen.add(tkr)
            label = m.get("yes_sub_title") or ""
            res = (m.get("result") or "").lower()
            if not label or res not in ("yes", "no"):
                continue  # unresolved leg (no usable result yet)
            ev = m.get("event_ticker") or tkr.rsplit("-", 1)[0]
            events.setdefault(ev, []).append({"label": label, "res": res, "title": m.get("title") or ""})

    out: list[dict] = []
    for ev, legs in events.items():
        date = (kalshi_ticker_date(ev) or "")[:10] or None    # date-only: settlement matches on the day
        voided = two_way and len(legs) >= 2 and all(l["res"] == "no" for l in legs)
        for l in legs:
            out.append({
                "date": date,
                "team_key": normalize_team(l["label"]),
                "label": l["label"],
                "result": "void" if voided else ("won" if l["res"] == "yes" else "lost"),
                "match": l["title"].replace(" Winner?", "").strip(),
            })
    return out
