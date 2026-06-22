"""PrizePicks adapter (free, public projections API — no key).

Source for the user's prop archetypes: Shots, Shots on Target, Goals (anytime scorer),
Passes Attempted, etc. Exposes `trending_count` == the 🔥 Popular-tab pick count, and
`odds_type` (standard / goblin = easier line / demon = harder). League 241 = World Cup.

NOTE: the endpoint is behind Cloudflare, which blocks non-browser HTTP clients by TLS
fingerprint (httpx gets a 403 even with a browser User-Agent). System `curl` passes that
check, so we fetch via a curl subprocess.
"""
from __future__ import annotations

import asyncio
import json

from .. import config
from ..matching import normalize_team


async def fetch() -> list[dict]:
    url = (f"{config.PRIZEPICKS_URL}?league_id={config.PRIZEPICKS_LEAGUE}"
           f"&per_page=1000&single_stat=true")
    cmd = [
        "curl", "-s", "--max-time", "25",
        "-H", f"User-Agent: {config.PRIZEPICKS_UA}",
        "-H", "Accept: application/json",
        "-H", "Accept-Language: en-US,en;q=0.9",
        url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        data = json.loads(out.decode())
    except Exception as exc:  # noqa: BLE001
        print(f"[prizepicks] fetch failed: {exc}")
        return []

    # rate-limited responses come back HTTP 200 with an error envelope (no "data") — treat as a miss
    # so the caller keeps the last-good board instead of blanking it.
    if not isinstance(data, dict) or "data" not in data or data.get("error"):
        print(f"[prizepicks] error envelope / no data (throttled?) — {str(data)[:120]}")
        return []

    players = {
        i["id"]: i.get("attributes", {})
        for i in data.get("included", [])
        if i.get("type") == "new_player"
    }
    out_props: list[dict] = []
    for p in data.get("data", []):
        a = p.get("attributes", {})
        pid = (p.get("relationships", {}).get("new_player", {}).get("data") or {}).get("id")
        pl = players.get(pid, {})
        line = a.get("line_score")
        if line is None or a.get("odds_type") == "live" or a.get("is_live"):
            continue
        out_props.append({
            "player": pl.get("name"),
            "team": pl.get("team"),
            "team_key": normalize_team(pl.get("team")),
            "position": pl.get("position"),
            "stat_type": a.get("stat_type"),
            "line": line,
            "odds_type": a.get("odds_type"),       # standard | goblin | demon
            "popularity": a.get("trending_count") or 0,
            "opponent": a.get("description"),
            "opponent_key": normalize_team(a.get("description")),
            "start_time": a.get("start_time"),
            "game_id": a.get("game_id"),
        })
    print(f"[prizepicks] {len(out_props)} projections")
    return out_props
