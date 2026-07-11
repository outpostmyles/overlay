"""Normalization so the same team / match lines up across sources.

The hardest real-world part of any odds aggregator is deciding that Polymarket's "USA",
Kalshi's "United States" and DraftKings' "USA" are the same selection. We keep a small alias
map plus accent/punctuation stripping. Extend ALIASES as you spot mismatches.
"""
from __future__ import annotations

import re
import unicodedata

ALIASES = {
    "usa": "united states",
    "united states of america": "united states",
    "us": "united states",
    "south korea": "korea republic",
    "korea": "korea republic",
    "north korea": "korea dpr",
    "ivory coast": "cote divoire",
    "czech republic": "czechia",
    "congo dr": "dr congo",
    "democratic republic of the congo": "dr congo",
    "iran": "ir iran",
    "cape verde": "cabo verde",
    "turkey": "turkiye",
    "bosnia": "bosnia and herzegovina",
    "bosnia herzegovina": "bosnia and herzegovina",   # ESPN spells it "Bosnia-Herzegovina"
}

_MONTHS = {
    "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04", "MAY": "05", "JUN": "06",
    "JUL": "07", "AUG": "08", "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
}

DRAW_KEYS = {"draw", "tie", "the draw"}


def normalize_team(name: str | None) -> str:
    if not name:
        return ""
    from .sports import active   # late import: sports never imports matching, but callers vary
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.lower().strip()
    s = s.replace("-", " ").replace("&", " and ")   # "Bosnia-Herzegovina"/"X & Y" → spaced
    s = re.sub(r"[^a-z0-9 ]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    # knockout markets wrap each pick in regulation-time wording ("Reg. Time Argentina", "Reg Time
    # Tie") - strip it so the selection still normalizes to the team (or to a draw)
    s = re.sub(r"\b(reg|regular|regulation) time\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if s in DRAW_KEYS:
        return "draw"
    # the active sport's aliases win (e.g. Kalshi's "Los Angeles D" -> "los angeles dodgers"),
    # then the global soccer map; wc26 carries no adapter aliases so its behavior is unchanged
    sport_alias = active().aliases.get(s)
    return sport_alias or ALIASES.get(s, s)


def kalshi_ticker_date(event_ticker: str) -> str | None:
    """'KXWCGAME-26JUN27CODUZB' -> '2026-06-27'. Series whose tickers embed a start time
    ('KXMLBGAME-26JUL121610TORSD') come back time-qualified, '2026-07-12T16:10': the HHMM is the
    per-game discriminator that keeps a doubleheader from merging into one market."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})(\d{4})?", event_ticker or "")
    if not m:
        return None
    yy, mon, dd, hhmm = m.groups()
    mm = _MONTHS.get(mon)
    if not mm:
        return None
    date = f"20{yy}-{mm}-{dd}"
    return f"{date}T{hhmm[:2]}:{hhmm[2:]}" if hhmm else date


def iso_date(commence_time: str | None) -> str | None:
    if not commence_time or len(commence_time) < 10:
        return None
    return commence_time[:10]


def moneyline_key(commence: str | None, team_keys: list[str]) -> str:
    """Merge key for the same game across sources.

    wc26 keys on the team PAIR only: Kalshi derives its date from the UTC ticker while The Odds API
    uses US-local (often a day later), so keying on date would split one game into two unmerged
    markets, and a WC pair plays at most once within the slate. Daily sports (MLB) key on pair +
    date + start time instead: the same pair plays a 3-4 game series and doubleheaders share even
    the date, so pair-only would silently merge distinct games and corrupt de-vig/CLV/settlement."""
    from .sports import active
    teams = sorted(t for t in team_keys if t and t != "draw")
    if active().pair_only_key:
        return f"ml:{'|'.join(teams)}"
    return f"ml:{'|'.join(teams)}|{(commence or '')[:16]}"
