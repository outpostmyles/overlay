"""User-logged futures leans, with live market drift.

A "lean" is the user's own call on a futures market - back (the team is more likely than the market
price) or fade (less likely) - captured at the de-vigged market % at the moment they log it. Each
refresh we re-read the team's current de-vigged market % from the live futures board and show how far
the sharp line has drifted toward the user's side since they logged it: positive = the market is moving
your way, a CLV-style leading signal that your read was early. It is NOT the ledger's settled-bet CLV
(paper.py, a closing-vs-entry PRICE on a graded bet) and not a guarantee - it is a points move in an
unbetted probability, suggestive not conclusive.

Final win/loss settlement (did the team actually reach the stage) is a deferred follow-up. Stored as a
flat JSON list (gitignored, like the other caches)."""
from __future__ import annotations

import json
import time
import uuid

from .. import config


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


def enrich(rows: list[dict]) -> list[dict]:
    """Attach the current de-vigged market % (from the live futures board rows) and the drift-so-far in
    points, signed toward the lean (up for a back, down for a fade). Newest first."""
    by_key = {(r.get("team"), r.get("kind")): r for r in (rows or [])}
    out = []
    for lean in _load():
        row = by_key.get((lean["team"], lean["kind"]))
        cur = row.get("market_pct") if row else None
        drift = None
        if cur is not None:
            move = cur - lean["entry_pct"]
            drift = round(move if lean["direction"] == "back" else -move, 1)
        out.append({**lean, "current_pct": cur, "drift_pp": drift})
    out.sort(key=lambda x: x.get("ts", ""), reverse=True)
    return out
