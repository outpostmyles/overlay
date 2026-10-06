"""My bets: the bets the owner actually places, logged by hand and graded against the closing line.

The research says the best long-run measure of betting skill is closing line value: whether the price
you took beat the price the market settled on just before the game. Win-loss records take hundreds of
bets to mean anything; CLV shows up on every bet. So each logged bet (a single or a parlay of up to 12
legs, on any line of a game on the board) keeps three numbers:

  p_log   the Research % of the whole bet when it was logged (the product of its legs)
  p_close the market's fair probability at the close: the moneyline's last pre-start de-vigged line, or
          the spread or total frozen at the lock 75 minutes out (the README shows the two barely differ)
  clv     p_close x the bet's decimal price - 1: what the bet was worth at the close

and settles off the same ESPN final as the forecast ledger. A leg that pushes voids a single; a parlay
with a pushed leg is marked push too, since the book reprices it in ways this ledger cannot see.
"""
from __future__ import annotations

import json
import sqlite3
import time

from .. import config
from ..sports import active

_SCHEMA = """
CREATE TABLE IF NOT EXISTS my_bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sport TEXT NOT NULL,
    logged_at TEXT NOT NULL,
    book TEXT,
    stake REAL NOT NULL,
    price INTEGER NOT NULL,            -- American odds of the whole bet
    legs_json TEXT NOT NULL,           -- [{dedup, team_a, team_b, date, kind, team|dir, line, p_log, ...}]
    status TEXT NOT NULL DEFAULT 'open',   -- open | won | lost | push
    settled_at TEXT,
    profit REAL,
    p_log REAL, p_close REAL, clv REAL
);
"""
KINDS = ("ml", "spread", "total")
MAX_LEGS = 12


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)


def decimal(price: int) -> float:
    return 1 + (price / 100 if price > 0 else 100 / -price)


def _side_p(leg_row: dict, side: str) -> float | None:
    """A board/ledger leg's probability for `side` (each leg stores its own side's)."""
    p = leg_row.get("prob")
    if p is None:
        return None
    return p if leg_row.get("side") == side else 1 - p


def leg_prob(entry: dict | None, leg: dict, research: bool = True) -> float | None:
    """The probability of one bet leg from a board entry ({market, legs, research}) or a ledger row
    rebuilt the same way. research=True uses the Research % (logging); False the market's (the close)."""
    if not entry:
        return None
    a, b = entry.get("team_a"), entry.get("team_b")
    if leg["kind"] == "ml":
        rs = entry.get("research") if research else None
        if rs and rs.get("ml"):
            return rs["ml"].get(leg["team"])
        m = entry.get("market")
        if not m:
            return None
        return m[0] if leg["team"] == a else m[2] if leg["team"] == b else None
    key = "spread" if leg["kind"] == "spread" else "total_goals"
    for row in entry.get("legs") or []:
        if row.get("key") != key:
            continue
        r = {**row, "prob": row.get("research_prob")} if research and row.get("research_prob") is not None else row
        if key == "total_goals":
            if row.get("line") == leg.get("line"):
                return _side_p(r, leg["dir"])
        else:                                        # the board's spread is the cover team at -line
            cover, line = row.get("team"), row.get("line")
            if leg["team"] == cover and leg.get("line") == -line:
                return _side_p(r, "cover")
            if leg["team"] != cover and leg["team"] in (a, b) and leg.get("line") == line:
                return _side_p(r, "not")
    return None


def _product(ps: list) -> float | None:
    if not ps or any(p is None for p in ps):
        return None
    out = 1.0
    for p in ps:
        out *= p
    return out


def validate(payload: dict, games: dict) -> tuple[list, int, float, str]:
    """Check a bet before it is stored. `games` maps each board game's dedup key to its entry. Raises
    ValueError with a plain-English reason."""
    try:
        price = int(payload.get("price"))
        stake = float(payload.get("stake"))
    except (TypeError, ValueError):
        raise ValueError("price (American odds) and stake are required")
    if abs(price) < 100 or abs(price) > 100000:
        raise ValueError("price must be American odds, like -150 or +240")
    if not 0 < stake <= 1_000_000:
        raise ValueError("stake must be a positive amount")
    book = str(payload.get("book") or "").strip()[:30]
    raw = payload.get("legs") or []
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_LEGS:
        raise ValueError(f"a bet needs 1 to {MAX_LEGS} legs")
    legs = []
    for x in raw:
        g = games.get((x or {}).get("dedup"))
        if not g:
            raise ValueError("every leg must be a game on this board")
        kind = x.get("kind")
        if kind not in KINDS:
            raise ValueError("a leg is a moneyline, spread or total")
        leg = {"dedup": x["dedup"], "team_a": g["team_a"], "team_b": g["team_b"], "date": g.get("date"),
               "kickoff_iso": g.get("kickoff_iso"), "kind": kind, "sport": g.get("sport") or active().key}
        if kind in ("ml", "spread"):
            if x.get("team") not in (g["team_a"], g["team_b"]):
                raise ValueError("pick one of the two teams")
            leg["team"] = x["team"]
        if kind in ("spread", "total"):
            try:
                leg["line"] = float(x.get("line"))
            except (TypeError, ValueError):
                raise ValueError("spreads and totals need a line")
        if kind == "total":
            if x.get("dir") not in ("over", "under"):
                raise ValueError("a total is over or under")
            leg["dir"] = x["dir"]
        legs.append(leg)
    if len({(l["dedup"], l["kind"]) for l in legs}) < len(legs):
        raise ValueError("one leg per line type per game")
    return legs, price, stake, book


def log_bet(payload: dict, games: dict) -> dict:
    """Store a bet the owner placed. `games` = {dedup: entry} from the live board (entries carry
    team_a, team_b, date, kickoff_iso, market, legs, research)."""
    legs, price, stake, book = validate(payload, games)
    for leg in legs:
        leg["p_log"] = leg_prob(games[leg["dedup"]], leg, research=True)
    p_log = _product([l["p_log"] for l in legs])
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    leg_sports = {l["sport"] for l in legs}
    sport = leg_sports.pop() if len(leg_sports) == 1 else "multi"     # a cross-sport ticket shows on every board
    with _conn() as c:
        cur = c.execute("INSERT INTO my_bets (sport, logged_at, book, stake, price, legs_json, p_log) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (sport, now, book, stake, price, json.dumps(legs),
                         round(p_log, 4) if p_log is not None else None))
        bet_id = cur.lastrowid
    return list_bets(bet_id=bet_id)[0]          # by id: a ticket of another board's games files under that board


def _grade(leg: dict, row: sqlite3.Row) -> str | None:
    """won / lost / push for one leg against its settled forecast row, or None if not graded yet."""
    if row is None or row["status"] != "settled" or row["actual_a"] is None:
        return None
    sa, sb = row["actual_a"], row["actual_b"]
    if leg["kind"] == "total":
        t = sa + sb
        return "push" if t == leg["line"] else "won" if (t > leg["line"]) == (leg["dir"] == "over") else "lost"
    mine, theirs = (sa, sb) if leg["team"] == row["team_a"] else (sb, sa)
    handicap = leg["line"] if leg["kind"] == "spread" else 0.0     # the team's own line: -7.5 or +7.5
    margin = mine - theirs + handicap
    return "push" if margin == 0 else "won" if margin > 0 else "lost"


def _close_entry(row: sqlite3.Row) -> dict:
    """A settled or locked ledger row as a board entry for leg_prob: the moneyline at its close (the last
    pre-start tick, else the lock) and the legs as frozen at the lock."""
    if row["closing_a"] is not None:
        market = (row["closing_a"], row["closing_draw"] or 0.0, row["closing_b"])
    else:
        market = (row["market_a"], row["market_draw"] or 0.0, row["market_b"])
    try:
        legs = json.loads(row["legs_json"]) if row["legs_json"] else []
    except (TypeError, ValueError):
        legs = []
    return {"team_a": row["team_a"], "team_b": row["team_b"], "market": market, "legs": legs}


def settle() -> int:
    """Grade open bets whose games have settled, and fill each bet's closing probability and CLV as
    soon as all its games have locked. Returns the number of bets newly settled."""
    done = 0
    with _conn() as c:
        bets = c.execute("SELECT * FROM my_bets WHERE status='open' OR clv IS NULL").fetchall()
        for b in bets:
            legs = json.loads(b["legs_json"])
            rows = {l["dedup"]: c.execute("SELECT * FROM forecasts WHERE dedup_key=?", (l["dedup"],)).fetchone()
                    for l in legs}
            p_close = None
            if all(r is not None and r["market_a"] is not None for r in rows.values()):
                p_close = _product([leg_prob(_close_entry(rows[l["dedup"]]), l, research=False) for l in legs])
            clv = round(p_close * decimal(b["price"]) - 1, 4) if p_close is not None else None
            if b["status"] == "open":
                grades = [_grade(l, rows.get(l["dedup"])) for l in legs]
                if any(g == "lost" for g in grades):
                    status = "lost"
                elif all(g is not None for g in grades):
                    status = "push" if any(g == "push" for g in grades) else "won"
                else:
                    status = "open"
                if status != "open":
                    profit = {"won": b["stake"] * (decimal(b["price"]) - 1), "lost": -b["stake"], "push": 0.0}[status]
                    c.execute("UPDATE my_bets SET status=?, settled_at=?, profit=?, p_close=?, clv=? WHERE id=?",
                              (status, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), round(profit, 2),
                               round(p_close, 4) if p_close is not None else None, clv, b["id"]))
                    done += 1
                    continue
            if clv is not None:
                c.execute("UPDATE my_bets SET p_close=?, clv=? WHERE id=?", (round(p_close, 4), clv, b["id"]))
    return done


def list_bets(bet_id: int | None = None) -> list[dict]:
    """This board's bets plus every cross-sport ticket, open first (or just the bet `bet_id`)."""
    with _conn() as c:
        if bet_id is not None:
            rows = c.execute("SELECT * FROM my_bets WHERE id=?", (bet_id,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM my_bets WHERE sport IN (?, 'multi') "
                             "ORDER BY status='open' DESC, logged_at DESC", (active().key,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["legs"] = json.loads(d.pop("legs_json") or "[]")
        d["ev_log"] = round(r["p_log"] * decimal(r["price"]) - 1, 4) if r["p_log"] is not None else None
        out.append(d)
    return out


def summary(bets: list[dict]) -> dict:
    """Record, profit, ROI on settled bets, and closing line value across every bet that has a close."""
    settled = [b for b in bets if b["status"] in ("won", "lost", "push")]
    staked = sum(b["stake"] for b in settled if b["status"] != "push")
    profit = sum(b["profit"] or 0 for b in settled)
    clvs = [b["clv"] for b in bets if b.get("clv") is not None]
    return {"bets": len(bets), "open": sum(1 for b in bets if b["status"] == "open"),
            "won": sum(1 for b in settled if b["status"] == "won"),
            "lost": sum(1 for b in settled if b["status"] == "lost"),
            "push": sum(1 for b in settled if b["status"] == "push"),
            "profit": round(profit, 2), "roi": round(profit / staked * 100, 1) if staked else None,
            "clv_n": len(clvs), "clv_avg": round(sum(clvs) / len(clvs) * 100, 2) if clvs else None,
            "beat_close": round(sum(1 for x in clvs if x > 0) / len(clvs) * 100, 1) if clvs else None}


def delete(bet_id: int) -> bool:
    with _conn() as c:
        return c.execute("DELETE FROM my_bets WHERE id=? AND sport IN (?, 'multi')",
                         (bet_id, active().key)).rowcount > 0
