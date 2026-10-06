"""Kalshi adapter (free, public market-data API — no key needed for reads).

Kalshi is our deep *per-match* source: every World Cup game has a 3-way moneyline
(team / tie / team), plus spreads and totals. It also lists the tournament winner.
Prices are in cents (0-100) == probability * 100.
"""
from __future__ import annotations

import asyncio
import re
import time

import httpx

from .. import config
from ..matching import kalshi_ticker_date, normalize_team
from ..models import Market, Quote, Selection
from ..sports import active

# soccer knockout games are listed as regulation-time markets ("Reg Time: Germany"); strip the
# wrapper from display labels (adapter-gated) so cards read "Germany", not "Reg Time: Germany"
_REG_TIME = re.compile(r"\breg(?:ular|ulation)?\.?\s*time\b\s*:?\s*", re.IGNORECASE)
# F5 legs phrase the pick as a sentence ("Pittsburgh wins first 5 innings"); strip to the team
# "X wins first 5 innings" (summer 2026), "Tie first 5 innings" (Sep 14-28 2026), plain "Tie" (now):
# the wording churns, so strip the phrase with or without "wins". Missing the bare form turned the
# tie leg into a fake team for two weeks and silently dropped every first-five call.
_F5_WORDS = re.compile(r"\b(?:wins?\s+)?first\s+5\s+innings(?:\s+winner)?\b", re.IGNORECASE)
# ticker date prefix: ddMONyy plus an optional HHMM (MLB tickers carry a start time, NHL tickers do not)
_DATE_PREFIX = re.compile(r"^\d{2}[A-Z]{3}\d{2}(?:\d{4})?")


def _clean(text: str) -> str:
    """Sport-aware label cleanup. The series registry itself lives on the adapter: v1 covers the
    cleanly-comparable markets (game moneyline + outright). Totals/spreads are nested per line so
    they aren't a mutually-exclusive set; de-vigging across them is invalid (Phase 2, per-line)."""
    text = text or ""
    if active().kalshi_strip_reg_time:
        text = _REG_TIME.sub("", text)
    return _F5_WORDS.sub("", text).strip()


def _prob(dollars) -> float | None:
    """Kalshi prices are in dollars (0.00-1.00) per $1 contract == probability directly."""
    try:
        d = float(dollars)
        return d if 0.0 < d < 1.0 else None
    except (TypeError, ValueError):
        return None


class _Rows(list):
    """A series' markets, and whether every page arrived (a partial pull is not the market)."""
    ok = True


_RETRY_STATUS = {429, 500, 502, 503, 504}
_RETRY_DELAYS = (1.5, 4.0)                 # then give up; the board keeps the series' last good pull
# the last complete pull of each series, so a refused request (Kalshi rate-limits bursts: four boards
# share one IP) shows the board's markets as of a few minutes ago instead of dropping them for a cycle
_LAST_GOOD: dict = {}
LAST_GOOD_SECONDS = 15 * 60
SERIES_PAUSE = 0.25


async def _get_page(client: httpx.AsyncClient, params: dict) -> dict:
    """One page of markets, retried on a rate limit or a server error (honoring Retry-After, capped)."""
    for attempt in range(len(_RETRY_DELAYS) + 1):
        resp = await client.get(f"{config.KALSHI_API}/markets", params=params,
                                headers={"Accept": "application/json"}, timeout=25)
        if resp.status_code in _RETRY_STATUS and attempt < len(_RETRY_DELAYS):
            try:
                wait = min(float(resp.headers.get("retry-after") or _RETRY_DELAYS[attempt]), 8.0)
            except ValueError:
                wait = _RETRY_DELAYS[attempt]
            await asyncio.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("unreachable")


async def _fetch_series(client: httpx.AsyncClient, series: str, status: str = "open") -> _Rows:
    out = _Rows()
    cursor = None
    await asyncio.sleep(SERIES_PAUSE)          # pace the pulls: a burst is what draws a 429
    for _ in range(20):  # safety cap on pagination
        params = {"series_ticker": series, "limit": 1000, "status": status}
        if cursor:
            params["cursor"] = cursor
        try:
            data = await _get_page(client, params)
        except Exception as exc:  # noqa: BLE001
            print(f"[kalshi] {series} fetch failed: {exc}")
            out.ok = False
            break
        out.extend(data.get("markets") or [])
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


async def _series_or_last_good(client: httpx.AsyncClient, series: str) -> list[dict]:
    """A series' open markets; when the pull fails, its last complete pull if that is recent."""
    raw = await _fetch_series(client, series)
    if getattr(raw, "ok", True):
        _LAST_GOOD[series] = (time.monotonic(), list(raw))
        return raw
    kept = _LAST_GOOD.get(series)
    if kept and time.monotonic() - kept[0] <= LAST_GOOD_SECONDS:
        print(f"[kalshi] {series}: keeping the last good pull ({int(time.monotonic() - kept[0])}s old)")
        return kept[1]
    return raw


# market types parsed one-market-PER-LINE: each child (e.g. "Over 8.5 runs", "Judge: 2+ HR") is its own
# 2-way yes/no book. De-vig happens within that pair only; lines of one event are NOT mutually exclusive.
_PER_LINE_TYPES = ("total", "player_prop", "spread")


def _prop_pair(event_ticker: str, code_map: dict) -> tuple | None:
    """'KXMLBHIT-26JUL111610CLEMIA-...' or 'KXNHLTOTAL-26OCT06CARMTL' -> the two team keys, split
    greedily against the code map the game series taught us (ticker team codes concatenate without a
    separator). This is the durable way to know a market's game: Kalshi's prose titles get reworded
    (they changed in August 2026 and broke every title-based join), the ticker structure does not."""
    parts = (event_ticker or "").split("-")
    if len(parts) < 2:
        return None
    seg = _DATE_PREFIX.sub("", parts[1], count=1)   # after ddMONyy[HHMM]
    if not seg or seg == parts[1]:
        return None
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
    if mtype in ("player_prop", "total", "spread"):   # pack the game pair from the ticker so the leg can join
        pair = _prop_pair(ev_ticker, code_map)
        if pair:
            group = f"{group}|{pair[0]}|{pair[1]}"
    if mtype == "spread":
        # "DAL Cowboys wins by over 7.5 points" is ticker ...TBDAL-DAL8: the suffix's letters are the
        # covering team's code. The label's wording differs by sport ("DAL Cowboys" vs "Western Kentucky"),
        # the code does not. A spread that cannot name its game and its team cannot be graded: drop it.
        cover = code_map.get(re.sub(r"\d+$", "", tkr.rsplit("-", 1)[-1]))
        if not pair or not cover or cover not in pair:
            return None
        group = f"{group}|{cover}"
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


def _matchup_name(ev_ticker: str, children: list[dict]) -> str | None:
    """'A vs B' for a game whose child titles no longer say it. Kalshi moved from 'A vs B Winner?' to
    'A wins' in August 2026, which left every MLB card titled after one team. The order comes from the
    event ticker, which concatenates the two team codes (KXNHLGAME-26OCT06CARMTL: Carolina, Montreal)."""
    by_code: dict = {}
    for m in children:
        code = (m.get("ticker") or "").rsplit("-", 1)[-1]
        label = _clean(m.get("yes_sub_title") or "").strip()
        if code and label and normalize_team(label) != "draw":
            by_code[code] = label
    if len(by_code) != 2:
        return None
    parts = (ev_ticker or "").split("-")
    seg = _DATE_PREFIX.sub("", parts[1], count=1) if len(parts) > 1 else ""
    c1, c2 = by_code
    if seg == c1 + c2:
        return f"{by_code[c1]} vs {by_code[c2]}"
    if seg == c2 + c1:
        return f"{by_code[c2]} vs {by_code[c1]}"
    return None


# ticker team code -> team key, learned from the game series' child suffixes on every fetch. Module-level
# so other readers (the top-bettors holdings map) can name the team behind a ticker like ...TBDAL-DAL.
TEAM_CODES: dict = {}


async def fetch(client: httpx.AsyncClient) -> list[Market]:
    markets: list[Market] = []
    code_map: dict = TEAM_CODES
    for series, (mtype, group) in active().kalshi_series.items():
        raw = await _series_or_last_good(client, series)
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
            if mtype == "moneyline" and " vs " not in event_name:
                event_name = _matchup_name(ev_ticker, children) or event_name
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
