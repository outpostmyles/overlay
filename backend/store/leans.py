"""User-logged futures leans, with live market drift.

A "lean" is the user's own call on a futures market - back (the team is more likely than the market
price) or fade (less likely) - captured at the de-vigged market % at the moment they log it. Each
refresh we re-read the team's current de-vigged market % from the live futures board and show how far
the sharp line has drifted toward the user's side since they logged it: positive = the market is moving
your way, a CLV-style leading signal that your read was early. It is NOT the ledger's settled-bet CLV
(paper.py, a closing-vs-entry PRICE on a graded bet) and not a guarantee - it is a points move in an
unbetted probability, suggestive not conclusive.

Leans also SETTLE to win/loss with a realized closing line: each refresh captures the moving de-vigged
% as the closing line, and when the stage is decided (the team reaches it -> a back wins; the team is
eliminated before it -> a fade wins) the lean freezes with that closing line, so the Futures track
record can go red, not just green. Stored as a flat JSON list (gitignored, like the other caches)."""
from __future__ import annotations

import json
import time
import uuid

from .. import config

# kind -> knockout wins a team needs to have REACHED that stage (R32->R16 is 1 win, ... final win is 5)
_STAGE_WINS = {"Reach Round of 16": 1, "Reach Quarter-final": 2, "Reach Semi-final": 3, "Win World Cup": 5}


def _load() -> list[dict]:
    p = config.LEANS_PATH
    if p.exists():
        try:
            data = json.loads(p.read_text())
            return data if isinstance(data, list) else []
        except Exception:  # noqa: BLE001
            return []
    return []


def _save(leans: list[dict]) -> None:
    try:
        config.LEANS_PATH.write_text(json.dumps(leans))
    except Exception as exc:  # noqa: BLE001
        print(f"[leans] save failed: {exc}")


def add(team: str, kind: str, direction: str, entry_pct: float, note: str = "") -> dict:
    """Log a lean. direction is 'back' or 'fade'; entry_pct is the de-vigged market % at log time."""
    direction = "fade" if str(direction).lower() == "fade" else "back"
    lean = {
        "id": uuid.uuid4().hex[:8],
        "team": (team or "").strip().lower(),
        "kind": (kind or "").strip(),
        "direction": direction,
        "entry_pct": round(float(entry_pct), 1),
        "note": (note or "").strip()[:200],
        "ts": time.strftime("%Y-%m-%d %H:%M"),
    }
    # one lean per (team, kind, direction): re-logging the same call replaces it rather than cluttering
    leans = [x for x in _load()
             if not (x.get("team") == lean["team"] and x.get("kind") == lean["kind"]
                     and x.get("direction") == lean["direction"])]
    leans.append(lean)
    _save(leans)
    return lean


def remove(lean_id: str) -> bool:
    leans = _load()
    kept = [x for x in leans if x.get("id") != lean_id]
    if len(kept) == len(leans):
        return False
    _save(kept)
    return True


def _knockout_progress(results: list[dict] | None, field: dict) -> tuple[dict, set]:
    """From finished CROSS-group games (= knockout games, since group games are within a group), using
    ESPN's penalties-aware winner flag: {team: knockout_wins} and {teams that have been eliminated}."""
    team_group = {t: g for g, ts in (field or {}).items() for t in ts}
    wins: dict = {}
    eliminated: set = set()
    for game in results or []:
        goals = game.get("goals") or {}
        teams = [t for t in goals if t in team_group]
        if len(teams) != 2 or team_group.get(teams[0]) == team_group.get(teams[1]):
            continue                                   # not a cross-group (knockout) game
        a, b = teams
        w = game.get("winner")
        if w not in (a, b):                            # no winner flag -> fall back to goals (no draws in KO)
            if goals.get(a) == goals.get(b):
                continue                               # undecided (KO draw, winner not yet posted) -> skip
            w = a if goals[a] > goals[b] else b
        wins[w] = wins.get(w, 0) + 1
        eliminated.add(b if w == a else a)
    return wins, eliminated


def settle(rows: list[dict], results: list[dict] | None, field: dict) -> int:
    """Capture the moving closing line on every open lean, and resolve any whose stage is now decided.
    A lean is decided when the team has REACHED the stage (enough knockout wins -> a back wins, a fade
    loses) or has been ELIMINATED before reaching it (a back loses, a fade wins). On resolution the lean
    freezes with the last-captured closing line; realized CLV is read off it in enrich()."""
    by_key = {(r.get("team"), r.get("kind")): r for r in (rows or [])}
    wins, eliminated = _knockout_progress(results, field)
    leans = _load()
    changed = 0
    for lean in leans:
        if lean.get("status", "open") != "open":
            continue
        row = by_key.get((lean["team"], lean["kind"]))
        if row and row.get("market_pct") is not None:
            lean["closing_pct"] = row["market_pct"]    # capture the moving close, like paper.capture_closing
            changed += 1
        need = _STAGE_WINS.get(lean["kind"])
        if need is None:
            continue
        reached = wins.get(lean["team"], 0) >= need
        if not (reached or lean["team"] in eliminated):
            continue                                   # still alive and short of the stage -> stay open
        won = (lean["direction"] == "back") == reached
        lean.update(status=("won" if won else "lost"), reached=reached,
                    settled_ts=time.strftime("%Y-%m-%d %H:%M"))
        changed += 1
    if changed:
        _save(leans)
    return sum(1 for x in leans if x.get("status", "open") != "open")


def enrich(rows: list[dict]) -> list[dict]:
    """Open leans carry the live de-vigged % and the drift-so-far (signed toward the lean: up for a back,
    down for a fade). Settled leans carry status (won/lost) and realized CLV (closing line minus entry,
    signed toward the lean). Open first, then settled; newest first within each."""
    by_key = {(r.get("team"), r.get("kind")): r for r in (rows or [])}
    out = []
    for lean in _load():
        status = lean.get("status", "open")
        row = by_key.get((lean["team"], lean["kind"]))
        cur = row.get("market_pct") if row else None
        sign = 1 if lean["direction"] == "back" else -1
        if status == "open":
            drift = round(sign * (cur - lean["entry_pct"]), 1) if cur is not None else None
            out.append({**lean, "status": "open", "current_pct": cur, "drift_pp": drift, "realized_clv": None})
        else:
            close = lean.get("closing_pct")
            realized = round(sign * (close - lean["entry_pct"]), 1) if close is not None else None
            out.append({**lean, "current_pct": close, "drift_pp": None, "realized_clv": realized})
    out.sort(key=lambda x: (x["status"] != "open", -_ts_ord(x)))
    return out


def _ts_ord(lean: dict) -> float:
    """Sort key: most recent first. Uses settled_ts for settled leans, else the log ts."""
    s = lean.get("settled_ts") or lean.get("ts") or ""
    return float(s.replace("-", "").replace(":", "").replace(" ", "") or 0)
