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
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.lower().strip()
    s = s.replace("-", " ").replace("&", " and ")   # "Bosnia-Herzegovina"/"X & Y" → spaced
    s = re.sub(r"[^a-z0-9 ]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    if s in DRAW_KEYS:
        return "draw"
    return ALIASES.get(s, s)


def kalshi_ticker_date(event_ticker: str) -> str | None:
    """'KXWCGAME-26JUN27CODUZB' -> '2026-06-27'."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", event_ticker or "")
    if not m:
        return None
    yy, mon, dd = m.groups()
    mm = _MONTHS.get(mon)
    if not mm:
        return None
    return f"20{yy}-{mm}-{dd}"


def iso_date(commence_time: str | None) -> str | None:
    if not commence_time or len(commence_time) < 10:
        return None
    return commence_time[:10]


def moneyline_key(date: str | None, team_keys: list[str]) -> str:
    # Key on the team PAIR only, not the date: Kalshi derives its date from the UTC ticker while The
    # Odds API uses US-local (often a day later), so keying on date splits the same game into two
    # unmerged markets (killing line-shopping + leaving the fair line undefined). A pair plays at most
    # once within the active slate, so the pair alone is a safe merge key.
    teams = sorted(t for t in team_keys if t and t != "draw")
    return f"ml:{'|'.join(teams)}"
