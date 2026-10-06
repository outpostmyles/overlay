"""Each sport's live games, shared across the sport processes through small files.

Every board runs in its own process, so a weekend parlay or a logged bet that spans the NFL, college
football, the NHL and MLB needs the other boards' games. Each process writes its own file every snapshot
(poly_live_<sport>.json, gitignored), so there is never write contention, and a reader takes any file
fresher than two hours. A board that is not running simply contributes nothing.
"""
from __future__ import annotations

import json
import os
import time

from .. import config

FRESH_SECONDS = 2 * 3600


def _path(sport: str):
    return config.ROOT / f"poly_live_{sport}.json"


def _trim(g: dict, sport: str) -> dict:
    """What a parlay or a bet needs from one live game: teams, kickoff, the line, the main spread and
    total, and the research's moneyline, DraftKings prices, open questions and top-bettor side."""
    rs = g.get("research") or {}
    top = next((f["target"]["team"] for f in rs.get("factors") or []
                if f.get("key") == "top_traders" and (f.get("target") or {}).get("team")), None)
    return {"sport": sport, "dedup": g["dedup"], "team_a": g["team_a"], "team_b": g["team_b"],
            "date": g.get("date"), "kickoff_iso": g.get("kickoff_iso"), "market": g.get("market"),
            "legs": [{k: leg.get(k) for k in ("key", "team", "opp", "line", "side", "prob", "research_prob")}
                     for leg in g.get("legs") or [] if leg.get("key") in ("spread", "total_goals")],
            "research": {"ml": rs.get("ml"), "uncertain": rs.get("uncertain") or [],
                         "cushion": rs.get("cushion"), "top": top,
                         "dk": {k: (rs.get("dk") or {}).get(k) for k in ("book", "ml", "ev")} if rs.get("dk") else None}
            if rs else None}


def write(sport: str, games: list[dict], names: dict) -> None:
    """Publish this board's live games (atomic replace, so a reader never sees half a file)."""
    path = _path(sport)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps({"ts": time.time(), "sport": sport, "names": names,
                                   "games": [_trim(g, sport) for g in games]}))
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[livelegs] {sport} write failed: {exc}")


def read_all() -> dict:
    """{sport: {"ts", "names", "games"}} for every board that has published in the last two hours."""
    out = {}
    for path in config.ROOT.glob("poly_live_*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if time.time() - (data.get("ts") or 0) <= FRESH_SECONDS and data.get("sport"):
            out[data["sport"]] = data
    return out


def games_by_dedup(boards: dict) -> dict:
    """Every published game, keyed the way bets reference them."""
    return {g["dedup"]: g for b in boards.values() for g in b.get("games") or []}
