"""The lotto record: every weekend's tickets, frozen and graded whether or not they were bet.

Each weekend, Saturday at 15:00 UTC, the $2 tickets for every target ($1,000, $2,500, $5,000) and both
constructions ("favorites", "research") are frozen with every leg's probability at that moment, and each
leg then grades off the same finals as the forecast ledger (any sport: the legs reference forecast rows).
A ticket hits when every leg that played won; a postponed game voids its leg, the way a book drops it.
Grading continues after a ticket misses, so the record keeps every leg: that is the data that says which
legs belong on a ticket (a sport, a price band, a research flag) and which construction holds up.

Any board's process may freeze; the unique key makes the first one win and the rest no-ops.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone

from .. import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lotto_tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    weekend TEXT NOT NULL,             -- the weekend's end (the Tuesday after), e.g. 2026-10-13
    variant TEXT NOT NULL,             -- favorites | research
    stake REAL NOT NULL,
    target REAL NOT NULL,
    frozen_at TEXT NOT NULL,
    legs_json TEXT NOT NULL,           -- [{sport, dedup, kind, team|dir, line, p, flags, label, game, kickoff_iso, result}]
    p REAL NOT NULL,                   -- chance every leg wins, at the freeze
    fair_payout REAL,
    dk_payout REAL,
    reached INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'open',   -- open | hit | missed
    legs_won INTEGER DEFAULT 0, legs_lost INTEGER DEFAULT 0, legs_void INTEGER DEFAULT 0,
    settled_at TEXT,
    UNIQUE(weekend, variant, stake, target)
);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)


def freeze(weekend: str, tickets: list[dict], now_iso: str | None = None) -> int:
    """Store a weekend's tickets once. Returns how many were new."""
    now_iso = now_iso or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    keep = ("sport", "dedup", "kind", "team", "dir", "line", "p", "flags", "label", "game", "kickoff_iso", "dk")
    added = 0
    with _conn() as c:
        for t in tickets:
            if not t.get("legs"):
                continue
            legs = [{k: leg.get(k) for k in keep} for leg in t["legs"]]
            cur = c.execute(
                "INSERT OR IGNORE INTO lotto_tickets (weekend, variant, stake, target, frozen_at, legs_json, p, "
                "fair_payout, dk_payout, reached) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (weekend, t["variant"], t["stake"], t["target"], now_iso, json.dumps(legs), t["p"],
                 t.get("fair_payout"), t.get("dk_payout"), 1 if t.get("reached") else 0))
            added += cur.rowcount
    return added


def _leg_result(leg: dict, row) -> str | None:
    """won / lost / void for one leg against its forecast row, or None while the game is unplayed."""
    if row is None:
        return None
    if row["status"] == "void":
        return "void"
    if row["status"] != "settled" or row["actual_a"] is None:
        return None
    a, b = row["actual_a"], row["actual_b"]
    if leg["kind"] == "total":
        t = a + b
        return "void" if t == leg["line"] else "won" if (t > leg["line"]) == (leg["dir"] == "over") else "lost"
    if a == b:
        return "void"                                   # a tie refunds a moneyline
    winner = row["team_a"] if a > b else row["team_b"]
    return "won" if winner == leg["team"] else "lost"


def settle() -> int:
    """Grade every leg that has a final; mark a ticket missed at its first lost leg and hit when every
    played leg won. Returns how many tickets were newly decided."""
    decided = 0
    with _conn() as c:
        rows = c.execute("SELECT * FROM lotto_tickets WHERE settled_at IS NULL").fetchall()
        for r in rows:
            legs = json.loads(r["legs_json"])
            for leg in legs:
                if leg.get("result") in ("won", "lost", "void"):
                    continue
                f = c.execute("SELECT status, actual_a, actual_b, team_a, team_b FROM forecasts WHERE dedup_key=?",
                              (leg["dedup"],)).fetchone()
                res = _leg_result(leg, f)
                if res:
                    leg["result"] = res
            won = sum(1 for leg in legs if leg.get("result") == "won")
            lost = sum(1 for leg in legs if leg.get("result") == "lost")
            void = sum(1 for leg in legs if leg.get("result") == "void")
            done = won + lost + void == len(legs)
            status = "missed" if lost else ("hit" if done and won else r["status"])
            if status != r["status"] and status in ("hit", "missed"):
                decided += 1
            c.execute("UPDATE lotto_tickets SET legs_json=?, status=?, legs_won=?, legs_lost=?, legs_void=?, "
                      "settled_at=? WHERE id=?",
                      (json.dumps(legs), status, won, lost, void,
                       time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if done else None, r["id"]))
    return decided


def list_tickets(weekends: int = 8) -> list[dict]:
    """The most recent weekends' tickets, newest first."""
    with _conn() as c:
        rows = c.execute("SELECT * FROM lotto_tickets WHERE weekend IN (SELECT DISTINCT weekend FROM lotto_tickets "
                         "ORDER BY weekend DESC LIMIT ?) ORDER BY weekend DESC, target, variant", (weekends,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["legs"] = json.loads(d.pop("legs_json"))
        out.append(d)
    return out


def _band(p: float) -> str:
    for lo, label in ((0.90, "90%+"), (0.80, "80-90%"), (0.70, "70-80%"), (0.60, "60-70%")):
        if p >= lo:
            return label
    return "under 60%"


def study() -> dict:
    """What the tickets are teaching: tickets hit against the chance they had, every graded leg against
    its price (overall, by sport, by price band, by research flag, by construction), and the closest calls.
    The leg rows are the ones to act on: a group that keeps winning less often than priced does not belong
    on a ticket. The six tickets of a weekend share most of their legs, so a leg counts once per weekend
    (and once per construction in the construction rows); counting it per ticket would overstate the
    evidence."""
    with _conn() as c:
        rows = c.execute("SELECT * FROM lotto_tickets").fetchall()
    tickets = [dict(r) for r in rows]
    graded = [t for t in tickets if t["status"] in ("hit", "missed") or t["settled_at"]]
    groups: dict = {}

    def add(key: str, label: str, leg: dict) -> None:
        g = groups.setdefault(key, {"group": key.split(":")[0], "label": label, "n": 0, "won": 0, "exp": 0.0,
                                    "var": 0.0})
        g["n"] += 1
        g["won"] += 1 if leg["result"] == "won" else 0
        g["exp"] += leg["p"]
        g["var"] += leg["p"] * (1 - leg["p"])

    seen, seen_style = set(), set()
    for t in tickets:
        for leg in json.loads(t["legs_json"]):
            if leg.get("result") not in ("won", "lost"):
                continue
            ident = (t["weekend"], leg["dedup"], leg["kind"], leg.get("team") or leg.get("dir"))
            if (ident, t["variant"]) not in seen_style:
                seen_style.add((ident, t["variant"]))
                add(f"style:{t['variant']}", f"{t['variant']} tickets", leg)
            if ident in seen:
                continue
            seen.add(ident)
            add("all:all", "every leg", leg)
            add(f"sport:{leg['sport']}", (leg.get("sport") or "").upper(), leg)
            add(f"band:{_band(leg['p'])}", _band(leg["p"]), leg)
            for f in leg.get("flags") or []:
                add(f"flag:{f}", f, leg)
    rows_out = []
    for key, g in groups.items():
        n = g["n"]
        rows_out.append({"key": key, "group": g["group"], "label": g["label"], "n": n, "won": g["won"],
                         "actual": round(g["won"] / n * 100, 1), "priced": round(g["exp"] / n * 100, 1),
                         "z": round((g["won"] - g["exp"]) / g["var"] ** 0.5, 2) if g["var"] else None})
    order = {"all": 0, "style": 1, "sport": 2, "band": 3, "flag": 4}
    rows_out.sort(key=lambda r: (order.get(r["group"], 9), r["label"]))
    closest = sorted([t for t in graded if t["status"] == "missed"],
                     key=lambda t: (t["legs_lost"], -(t["legs_won"] or 0)))[:3]
    return {"tickets": len(tickets), "graded": len(graded), "hits": sum(1 for t in tickets if t["status"] == "hit"),
            "expected_hits": round(sum(t["p"] for t in graded), 3),
            "weekends": len({t["weekend"] for t in tickets}),
            "legs": rows_out,
            "closest": [{"weekend": t["weekend"], "target": t["target"], "variant": t["variant"],
                         "won": t["legs_won"], "lost": t["legs_lost"], "legs": t["legs_won"] + t["legs_lost"] + t["legs_void"]}
                        for t in closest]}


def maybe_freeze(pool_tickets: list[dict], weekend_end: datetime, freeze_at: datetime,
                 now: datetime | None = None) -> int:
    """Freeze this weekend's $2 tickets once the freeze time has passed (and the weekend is not over)."""
    now = now or datetime.now(timezone.utc)
    if not freeze_at <= now < weekend_end:
        return 0
    return freeze(weekend_end.date().isoformat(), pool_tickets, now.strftime("%Y-%m-%dT%H:%M:%SZ"))
