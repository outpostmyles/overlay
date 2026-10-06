"""Paper-trading proof engine — auto-logs every AI pick and tracks the track record.

Each AI-recommended bet is logged here automatically (when you hit Analyze) so the system builds
a verifiable record before any real money. The headline metric is **Closing Line Value (CLV)** —
the research's #1 predictor of long-run profit — which we compute purely from our own free feeds:
the no-vig fair price when the pick was logged vs. the fair price as it moves toward kickoff. No
results feed needed for CLV. Win/loss settlement is graded manually for now (status dropdown).

CLV %: (closing_fair_prob / pick_fair_prob - 1) * 100. Positive = the line moved toward our pick
(we got the better price) = beat the close. Only meaningful for favorite-ML picks, where we have a
clean market fair line on both sides.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from .. import config
from ..matching import normalize_team
from ..sports import active as _active_sport

_VS_RE = re.compile(r"\s+vs\.?\s+", re.IGNORECASE)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_picks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at TEXT NOT NULL,
    match TEXT NOT NULL,
    archetype TEXT NOT NULL,
    selection TEXT NOT NULL,
    confidence INTEGER,
    commence_time TEXT,
    pick_fair_prob REAL,          -- no-vig fair prob at pick time (ML only) — drives CLV
    pick_price_decimal REAL,      -- best available odds at pick time (ML only)
    model_prob REAL,              -- the model's OWN projected P(hit) at pick time (props + ML) — for calibration/Brier, NOT CLV
    closing_fair_prob REAL,       -- fair prob near kickoff (updated until the match starts)
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | won | lost | void
    dedup_key TEXT UNIQUE,
    odds_type TEXT,            -- prop line type (standard/goblin/demon) — calibration bucket
    popularity INTEGER,        -- prop popularity — calibration bucket
    on_favorite INTEGER,       -- 1 if the pick is on the match favorite
    agreement_pp REAL,         -- favorite fair% − model% at log time — agreement-band bucket
    closing_locked_at TEXT,    -- when the close was frozen (auto-settled) — stops CLV drift
    real_money INTEGER DEFAULT 0, -- 1 if the user actually placed this bet (absorbs the Bet Log)
    stake_units REAL DEFAULT 1.0, -- conviction-scaled units risked (for the bankroll curve)
    legs_json TEXT,               -- structured legs for a parlay entry (for settlement)
    game_over_at TEXT,            -- stamped when the pick's game has a result (still pending = needs grading)
    sport TEXT                    -- multi-sport namespace; NULL on legacy rows means wc26
);
"""

# additive columns for DBs created before these features existed
_MIGRATE = [("odds_type", "TEXT"), ("popularity", "INTEGER"),
            ("on_favorite", "INTEGER"), ("agreement_pp", "REAL"),
            ("closing_locked_at", "TEXT"), ("real_money", "INTEGER"),
            ("stake_units", "REAL"), ("legs_json", "TEXT"), ("game_over_at", "TEXT"),
            ("model_prob", "REAL"),
            ("sport", "TEXT")]   # multi-sport namespace; NULL on legacy rows means wc26


# --- Model Ledger: pre-kickoff 1X2 forecasts, model vs market, graded on the result ----------- #
# Separate table from the bet ledger: this grades the MODEL (calibration), not the user's bets (CLV).
# Each row freezes the model's 1X2 AND the de-vigged market's 1X2 at the SAME instant (the lock), then
# auto-grades both against the ESPN result. team_a/team_b are the two normalized keys in a canonical
# (sorted) order; the model is neutral-venue so the slot assignment is arbitrary but consistent.
_FORECAST_SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match TEXT NOT NULL,              -- display "Team A vs Team B"
    team_a TEXT NOT NULL,             -- normalized key, slot A
    team_b TEXT NOT NULL,             -- normalized key, slot B
    commence_time TEXT,               -- game date (YYYY-MM-DD)
    stage TEXT,                       -- round label if known
    logged_at TEXT NOT NULL,          -- when the pending row was first created
    lock_ts TEXT,                     -- when the forecast was frozen (NULL until locked)
    model_cutoff TEXT,                -- model trained on results through this date (game excluded, it is future)
    kickoff_iso TEXT,                 -- resolved real kickoff used for the lock buffer
    model_a REAL, model_draw REAL, model_b REAL,    -- model 1X2, frozen at lock
    market_a REAL, market_draw REAL, market_b REAL, -- de-vigged market 1X2, frozen at the SAME instant
    market_sources TEXT,             -- which sharp sources fed the benchmark
    actual_a INTEGER, actual_b INTEGER,  -- final goals (90+ET)
    actual_outcome TEXT,             -- 'a' | 'draw' | 'b'
    pens INTEGER DEFAULT 0,          -- level in 90+ET, decided on penalties (out of scope, flagged only)
    brier_model REAL, brier_market REAL,
    rps_model REAL, rps_market REAL,
    hit_model INTEGER,               -- model argmax == outcome
    legs_json TEXT,                  -- extra market predictions (total/team goals, BTTS, corners), frozen + graded
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | locked | settled | void
    dedup_key TEXT UNIQUE,
    sport TEXT,                      -- multi-sport namespace; NULL on legacy rows means wc26
    closing_a REAL, closing_draw REAL, closing_b REAL   -- the de-vigged line's CLOSE (last pre-start tick)
);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_paper() -> None:
    Path(config.DB_PATH).touch(exist_ok=True)
    def _alter(c, stmt: str) -> None:
        """Additive migration: a duplicate column is expected (already migrated); anything else
        (locked db, disk error) must crash startup LOUDLY, not surface later as per-request 502s."""
        try:
            c.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise

    with _conn() as c:
        c.executescript(_SCHEMA)
        c.executescript(_FORECAST_SCHEMA)
        for col, typ in _MIGRATE:
            _alter(c, f"ALTER TABLE paper_picks ADD COLUMN {col} {typ}")
        _alter(c, "ALTER TABLE forecasts ADD COLUMN legs_json TEXT")
        _alter(c, "ALTER TABLE forecasts ADD COLUMN sport TEXT")
        _alter(c, "ALTER TABLE forecasts ADD COLUMN closing_a REAL")
        _alter(c, "ALTER TABLE forecasts ADD COLUMN closing_draw REAL")
        _alter(c, "ALTER TABLE forecasts ADD COLUMN closing_b REAL")


def log_picks(rows: list[dict]) -> int:
    """Insert picks, ignoring duplicates (same match+archetype+selection on the same day)."""
    if not rows:
        return 0
    inserted = 0
    sport = _active_sport().key
    with _conn() as c:
        for r in rows:
            cur = c.execute(
                """INSERT OR IGNORE INTO paper_picks
                   (logged_at, match, archetype, selection, confidence, commence_time,
                    pick_fair_prob, pick_price_decimal, model_prob, dedup_key,
                    odds_type, popularity, on_favorite, agreement_pp, stake_units, legs_json, sport)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r.get("logged_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
                 r["match"], r["archetype"], r["selection"], r.get("confidence"),
                 r.get("commence_time"), r.get("pick_fair_prob"), r.get("pick_price_decimal"),
                 r.get("model_prob"), r["dedup_key"], r.get("odds_type"), r.get("popularity"),
                 r.get("on_favorite"), r.get("agreement_pp"), r.get("stake_units") or 1.0,
                 r.get("legs_json"), sport),
            )
            inserted += cur.rowcount
    return inserted


def _selection_team_key(selection: str) -> str:
    """'England ML' -> 'england' (normalized, alias-collapsed) for settlement matching."""
    return normalize_team((selection or "").replace(" ML", "").strip())


def settle_from_resolved(resolved: list[dict]) -> int:
    """Auto-grade pending favorite-ML picks from Kalshi resolved outcomes (free, no results feed).

    `resolved` = [{date, team_key, result: won|lost, ...}]. Match on (date, favorite team_key).
    Props (goalscorer/shots/etc.) have no free results source → stay manual. Freezing status away
    from 'pending' here also locks the closing line, since capture_closing only touches pendings."""
    if not resolved:
        return 0
    lookup = {(r["date"], r["team_key"]): r["result"]
              for r in resolved if r.get("date") and r.get("result") in ("won", "lost", "void")}
    if not lookup:
        return 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    settled = 0
    with _conn() as c:
        rows = c.execute(
            "SELECT id, selection, commence_time FROM paper_picks "
            "WHERE status='pending' AND archetype='favorite_ml'"
        ).fetchall()
        for r in rows:
            date = (r["commence_time"] or "")[:10]
            result = lookup.get((date, _selection_team_key(r["selection"])))
            if result:
                c.execute("UPDATE paper_picks SET status=?, closing_locked_at=? WHERE id=?",
                          (result, now, r["id"]))
                settled += 1
    return settled


def _num_in(s: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)", s or "")
    return float(m.group(1)) if m else None


_VAGUE = ("(", "likely", "equivalent", " or ", "unknown", "main ")


def _name_hit(name: str, text: str) -> bool:
    """Whole-word surname match — avoids 'son' matching 'jackson' and 'heung min son' missing 'son'.
    `name` and `text` are already normalized (lowercase, alnum + spaces)."""
    if not name or not text:
        return False
    toks = [t for t in name.split() if len(t) > 3]
    surname = toks[-1] if toks else (name.split() or [""])[-1]
    if len(surname) <= 3:
        return name in text   # very short name → fall back to substring
    return re.search(r"\b" + re.escape(surname) + r"\b", text) is not None


def settle_props(results: list[dict]) -> int:
    """Auto-grade standalone GOALSCORER + TEAM-TOTAL picks from finished-game results (scorer names +
    final goals). Shots/SOT/passes have no free per-player feed → left for manual grading. Conservative
    on losses: a goalscorer is graded lost only when the player is clearly named and the game returned
    scorer data (so an ESPN miss never falsely marks everyone lost)."""
    if not results:
        return 0
    idx = {}
    for g in results:
        for tk in g["goals"]:
            idx[(g["date"], tk)] = g
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    with _conn() as c:
        # pending picks of both kinds, PLUS already-'lost' goalscorers (to retro-fix DNP → void)
        rows = c.execute(
            "SELECT id, match, selection, commence_time, archetype, status FROM paper_picks "
            "WHERE archetype IN ('anytime_goalscorer','team_total_over') "
            "AND (status='pending' OR (archetype='anytime_goalscorer' AND status='lost'))"
        ).fetchall()
        for r in rows:
            date = (r["commence_time"] or "")[:10]
            teams = [normalize_team(t) for t in _VS_RE.split(r["match"] or "") if t.strip()]
            g = next((idx[(date, tk)] for tk in teams if (date, tk) in idx), None)
            if not g:
                continue
            sel = r["selection"] or ""
            seln = normalize_team(sel)
            result = None
            if r["archetype"] == "team_total_over":
                head = re.split(r"\s+(?:team\s+total|total|over)\b", sel, flags=re.IGNORECASE)[0]
                head_key = normalize_team(head)
                tk = next((t for t in g["goals"] if t and (t == head_key or t in seln)), None)
                line = _num_in(sel)
                if tk is not None and line is not None and g["goals"].get(tk) is not None:
                    result = "won" if g["goals"][tk] > line else "lost"
            else:  # anytime_goalscorer
                played = g.get("played") or set()
                if any(_name_hit(sc, seln) for sc in g["scorers"]):
                    result = "won"
                elif any(_name_hit(pl, seln) for pl in played):
                    result = "lost"   # the named player appeared but didn't score
                elif played and not any(v in sel.lower() for v in _VAGUE):
                    result = "void"   # clearly-named player never appeared (DNP) → stake returned
            if result and result != r["status"]:
                c.execute("UPDATE paper_picks SET status=?, closing_locked_at=? WHERE id=?",
                          (result, now, r["id"]))
                n += 1
    return n


def _stat_kind(sel: str) -> str | None:
    s = (sel or "").lower()
    if "on target" in s or "sot" in s:
        return "sot"
    if "shot" in s:
        return "shots"
    if "pass" in s:
        return "passes"
    if "tackle" in s:
        return "tackles"
    return None


def pending_player_prop_games() -> list[tuple]:
    """(date, frozenset team_keys) for finished games that still have ungraded shots/SOT/passes picks
    — the exact games worth spending an API-Football request on."""
    with _conn() as c:
        rows = c.execute(
            "SELECT DISTINCT match, commence_time FROM paper_picks "
            "WHERE status='pending' AND archetype IN ('shots_sot','popular_prop') "
            "AND game_over_at IS NOT NULL"
        ).fetchall()
    out = []
    for r in rows:
        date = (r["commence_time"] or "")[:10]
        teams = frozenset(normalize_team(t) for t in _VS_RE.split(r["match"] or "") if t.strip())
        if date and len(teams) >= 2:
            out.append((date, teams))
    return out


def settle_player_props(stats: dict) -> int:
    """Grade pending shots/SOT/passes/tackles props from API-Football per-player match stats.
    stats = {(date, frozenset team_keys): {player_key: {shots,sot,passes,tackles,minutes,played}}}.
    DNP (didn't appear) → void; else over hits if the stat value exceeds the line."""
    if not stats:
        return 0
    idx = {}
    for (date, teams), players in stats.items():
        for tk in teams:
            idx[(date, tk)] = players
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    with _conn() as c:
        rows = c.execute(
            "SELECT id, match, selection, commence_time FROM paper_picks "
            "WHERE status='pending' AND archetype IN ('shots_sot','popular_prop')"
        ).fetchall()
        for r in rows:
            date = (r["commence_time"] or "")[:10]
            teams = [normalize_team(t) for t in _VS_RE.split(r["match"] or "") if t.strip()]
            players = next((idx[(date, tk)] for tk in teams if (date, tk) in idx), None)
            if not players:
                continue
            sel = r["selection"] or ""
            kind, line = _stat_kind(sel), _num_in(sel)
            if not kind or line is None:
                continue
            seln = normalize_team(sel)
            pk = next((k for k in players if _name_hit(k, seln)), None)
            if pk is None:
                if any(v in sel.lower() for v in _VAGUE):
                    continue                       # too vague to match a player → leave manual
                result = "void"                    # clearly-named player not in the squad → DNP
            elif not players[pk].get("played"):
                result = "void"                    # on the bench / didn't appear → stake returned
            else:
                over = "under" not in sel.lower()  # honor under-phrased props (e.g. fading a hot line)
                val = players[pk].get(kind) or 0
                result = "won" if (val > line if over else val < line) else "lost"  # .5 lines → no push
            c.execute("UPDATE paper_picks SET status=?, closing_locked_at=? WHERE id=?",
                      (result, now, r["id"]))
            n += 1
    return n


def pending_corner_games() -> list[tuple]:
    """(date, frozenset team_keys) for finished games that still have ungraded total-corners picks."""
    with _conn() as c:
        rows = c.execute(
            "SELECT DISTINCT match, commence_time FROM paper_picks "
            "WHERE status='pending' AND archetype='total_corners' AND game_over_at IS NOT NULL"
        ).fetchall()
    out = []
    for r in rows:
        date = (r["commence_time"] or "")[:10]
        teams = frozenset(normalize_team(t) for t in _VS_RE.split(r["match"] or "") if t.strip())
        if date and len(teams) >= 2:
            out.append((date, teams))
    return out


def settle_corners(team_stats: dict) -> int:
    """Grade pending total-corners picks from API-Football team match stats. team_stats =
    {(date, frozenset teams): {team_key: {corners,...}}}. The total is both teams' corners; lines are
    .5 so there's no push. Both teams' corner counts must be present, else we wait."""
    if not team_stats:
        return 0
    idx = {}
    for (date, teams), tstats in team_stats.items():
        for tk in teams:
            idx[(date, tk)] = tstats
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    with _conn() as c:
        rows = c.execute(
            "SELECT id, match, selection, commence_time FROM paper_picks "
            "WHERE status='pending' AND archetype='total_corners'"
        ).fetchall()
        for r in rows:
            date = (r["commence_time"] or "")[:10]
            teams = [normalize_team(t) for t in _VS_RE.split(r["match"] or "") if t.strip()]
            tstats = next((idx[(date, tk)] for tk in teams if (date, tk) in idx), None)
            if not tstats:
                continue
            corners = [(v or {}).get("corners") for v in tstats.values()]
            line = _num_in(r["selection"])
            if line is None or len(corners) < 2 or any(cc is None for cc in corners):
                continue
            total = sum(corners)
            if total == line:                       # exact push (only possible on a whole-number line) → refund
                result = "void"
            else:
                over = "under" not in (r["selection"] or "").lower()
                result = "won" if (total > line if over else total < line) else "lost"
            c.execute("UPDATE paper_picks SET status=?, closing_locked_at=? WHERE id=?",
                      (result, now, r["id"]))
            n += 1
    return n


def expire_ungradable(days: int) -> int:
    """Void (stake-neutral) un-auto-gradable props (shots/SOT/passes) whose game finished > `days`
    ago and the user never graded — so 'Awaiting' doesn't accumulate dead rows forever. Skips
    real-money picks (those the user must grade themselves)."""
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * 86400))
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as c:
        cur = c.execute(
            "UPDATE paper_picks SET status='void', closing_locked_at=? "
            "WHERE status='pending' AND archetype IN ('shots_sot','popular_prop','total_corners') "
            "AND game_over_at IS NOT NULL AND game_over_at < ? "
            "AND (real_money IS NULL OR real_money=0)",
            (now, cutoff))
        return cur.rowcount


def mark_finished(finished: set) -> int:
    """Stamp game_over_at on pending picks whose game has a result, so the UI can move 'over but
    not yet graded' picks (mostly manual props) out of the upcoming list. finished = {(date, team_key)}."""
    if not finished:
        return 0
    n = 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as c:
        rows = c.execute("SELECT id, match, commence_time FROM paper_picks "
                         "WHERE status='pending' AND game_over_at IS NULL").fetchall()
        for r in rows:
            date = (r["commence_time"] or "")[:10]
            if not date:
                continue
            teams = [normalize_team(t) for t in _VS_RE.split(r["match"] or "") if t.strip()]
            if any((date, tk) in finished for tk in teams):
                c.execute("UPDATE paper_picks SET game_over_at=? WHERE id=?", (now, r["id"]))
                n += 1
    return n


def settle_parlays(results: list[dict]) -> int:
    """Grade pending parlay entries from finished-game results (ESPN). All legs are same-game, so
    one game result settles the whole entry: won if every leg hits, else lost. results = [{date,
    goals{team_key:int}, scorers:set(normalized names)}]."""
    if not results:
        return 0
    # index by (date, team_key) -> game result
    idx: dict = {}
    for g in results:
        gks = list(g["goals"].keys())
        for tk in gks:
            opp_goals = max((g["goals"][o] for o in gks if o != tk), default=0)
            idx[(g["date"], tk)] = {"team_goals": g["goals"][tk], "opp_goals": opp_goals,
                                    "scorers": g.get("scorers") or set(), "played": g.get("played") or set()}
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    settled = 0
    with _conn() as c:
        rows = c.execute(
            "SELECT id, commence_time, legs_json FROM paper_picks "
            "WHERE status='pending' AND archetype='parlay' AND legs_json IS NOT NULL"
        ).fetchall()
        for r in rows:
            date = (r["commence_time"] or "")[:10]
            try:
                legs = json.loads(r["legs_json"])
            except (TypeError, ValueError):
                continue
            tk = next((l.get("team_key") for l in legs if l.get("team_key")), None)
            g = idx.get((date, tk)) if tk else None
            if not g:
                continue  # game not finished / no result yet
            won, void, unresolved = True, False, False
            for leg in legs:
                t = leg.get("type")
                if t == "moneyline":
                    hit = g["team_goals"] > g["opp_goals"]
                elif t == "team_total_over":
                    hit = g["team_goals"] > float(leg.get("line") or 1.5)
                elif t == "anytime_goalscorer":
                    pl = normalize_team(leg.get("player") or "")
                    if pl and any(_name_hit(sc, pl) for sc in g["scorers"]):
                        hit = True
                    elif not g["played"]:
                        unresolved = True  # no roster data (e.g. ESPN summary failed) → can't tell
                        break              # "didn't score" from DNP — leave the parlay pending, don't guess lost
                    elif pl and not any(_name_hit(p2, pl) for p2 in g["played"]):
                        void = True        # leg player never appeared (DNP) → push the whole entry
                        break
                    else:
                        hit = False        # appeared, didn't score → leg lost
                else:
                    hit = True  # unknown leg type — don't fail the parlay on it
                won = won and hit
            if unresolved:
                continue                   # wait for a complete result before grading
            status = "void" if void else ("won" if won else "lost")
            c.execute("UPDATE paper_picks SET status=?, closing_locked_at=? WHERE id=?",
                      (status, now, r["id"]))
            settled += 1
    return settled


def _utc(s: str | None):
    """Parse an ISO stamp ('...Z' or '+00:00', with or without seconds) to an aware datetime, or None."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def capture_closing(fair_now: dict) -> None:
    """Update closing_fair_prob for pending favorite-ML picks while their game has not started.
    `fair_now` is {(match, game_date): (selection, current_fair_prob)}, built by the aggregator from
    the board's favorites and filtered to games whose verified kickoff is still in the future, so the
    last write before first pitch IS the close.

    The old contract ({match: prob}, overwritten until the market vanished) kept writing through the
    whole game, because a market trades in-play and a pick stays pending until it settles: 10 of the
    23 WC closes ended pinned at 0.989, and all 10 won. Keying by date also stops one game of a series
    overwriting another game's close (the matchup string repeats across a series), and a pick is only
    written while it backs the team that is still the favorite, so a flipped favorite never hands it
    the other side's price. A pick whose game has started keeps its last pre-start value."""
    if not fair_now:
        return
    with _conn() as c:
        rows = c.execute(
            "SELECT id, match, commence_time, selection FROM paper_picks WHERE status='pending' "
            "AND archetype='favorite_ml' AND pick_fair_prob IS NOT NULL"
        ).fetchall()
        for r in rows:
            hit = fair_now.get((r["match"], (r["commence_time"] or "")[:10]))
            if hit and hit[0] == r["selection"] and hit[1] is not None:
                c.execute("UPDATE paper_picks SET closing_fair_prob=? WHERE id=?", (hit[1], r["id"]))


def update_pick(pick_id: int, status: str | None = None, real_money=None) -> None:
    sets, vals = [], []
    if status:
        sets.append("status=?"); vals.append(status)
    if real_money is not None:
        sets.append("real_money=?"); vals.append(1 if real_money else 0)
    if not sets:
        return
    vals.append(pick_id)
    with _conn() as c:
        c.execute(f"UPDATE paper_picks SET {','.join(sets)} WHERE id=?", vals)


def delete_pick(pick_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM paper_picks WHERE id=?", (pick_id,))


def _clv(r: sqlite3.Row) -> float | None:
    if r["archetype"] == "parlay":
        return None  # a parlay has no single closing line — CLV doesn't apply
    if r["pick_fair_prob"] and r["closing_fair_prob"]:
        return round((r["closing_fair_prob"] / r["pick_fair_prob"] - 1) * 100, 2)
    return None


def _pnl(r: sqlite3.Row) -> float | None:
    """P/L in UNITS, stake-weighted (a 2u winner at +150 returns 2 × 1.5 = 3u)."""
    if r["pick_price_decimal"] is None:
        return None  # props have no odds → hit-rate only
    su = r["stake_units"] if r["stake_units"] is not None else 1.0
    if r["status"] == "won":
        return round(su * (r["pick_price_decimal"] - 1.0), 3)
    if r["status"] == "lost":
        return round(-su, 3)
    return 0.0


def list_picks() -> list[dict]:
    clause, params = _sport_clause()               # each sport sees only its own Track Record
    with _conn() as c:
        rows = c.execute(f"SELECT * FROM paper_picks WHERE {clause} ORDER BY id DESC", params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["clv_pct"] = _clv(r)
        d["units_pl"] = _pnl(r)
        d["game_over"] = bool(r["game_over_at"])
        out.append(d)
    return out


def _agg(picks: list[dict]) -> dict:
    settled = [p for p in picks if p["status"] in ("won", "lost")]
    graded_clv = [p["clv_pct"] for p in picks if p["clv_pct"] is not None]
    priced = [p for p in settled if p["units_pl"] is not None]
    staked = sum((p.get("stake_units") or 1.0) for p in priced)  # total units risked
    pl = sum(p["units_pl"] for p in priced)
    beat = sum(1 for c in graded_clv if c > 0)
    return {
        "picks": len(picks),
        "settled": len(settled),
        "wins": sum(1 for p in settled if p["status"] == "won"),
        "hit_rate": round(sum(1 for p in settled if p["status"] == "won") / len(settled) * 100, 1) if settled else None,
        "units_pl": round(pl, 2),
        "roi_pct": round(pl / staked * 100, 1) if staked else None,
        "avg_clv": round(sum(graded_clv) / len(graded_clv), 2) if graded_clv else None,
        "beat_close_pct": round(beat / len(graded_clv) * 100, 1) if graded_clv else None,
        "clv_tracked": len(graded_clv),
    }


def _bankroll_curve(picks: list[dict]) -> tuple[float, list[float]]:
    """Running bankroll ($) over settled, priced bets (favorite-ML for now) in kickoff order.
    Stake-weighted: each settled bet moves the bankroll by its units_pl × one unit's dollars."""
    unit = config.BANKROLL * config.UNIT_PCT
    settled = sorted(
        [p for p in picks if p["status"] in ("won", "lost") and p["units_pl"] is not None],
        key=lambda p: (p.get("commence_time") or "", p.get("logged_at") or ""))
    bank = config.BANKROLL
    curve = [round(bank, 2)]
    for p in settled:
        bank += p["units_pl"] * unit
        curve.append(round(bank, 2))
    return round(bank, 2), curve


def model_calibration() -> dict:
    """Per-archetype calibration of the MODEL's projected P(hit) against actual settled outcomes, for
    picks that logged a model_prob. Brier = mean (pred - outcome)^2 (lower is better, 0.25 = a coin
    flip). Comparing mean_pred to hit_rate exposes over-projection (mean_pred >> hit_rate) or under.
    Only populated for picks logged after model_prob shipped, so it fills going forward."""
    clause, params = _sport_clause()
    with _conn() as c:
        rows = c.execute(
            "SELECT archetype, model_prob, status FROM paper_picks "
            f"WHERE model_prob IS NOT NULL AND status IN ('won','lost') AND {clause}", params
        ).fetchall()
    agg: dict = {}
    for r in rows:
        d = agg.setdefault(r["archetype"], {"n": 0, "pred": 0.0, "wins": 0, "brier": 0.0})
        outcome = 1.0 if r["status"] == "won" else 0.0
        d["n"] += 1
        d["pred"] += r["model_prob"]
        d["wins"] += int(outcome)
        d["brier"] += (r["model_prob"] - outcome) ** 2
    out = {}
    for a, d in agg.items():
        mean_pred = d["pred"] / d["n"]
        hit = d["wins"] / d["n"]
        out[a] = {"n": d["n"], "mean_pred": round(mean_pred, 3), "hit_rate": round(hit, 3),
                  "gap_pp": round((mean_pred - hit) * 100, 1),   # +ve = model over-projects
                  "brier": round(d["brier"] / d["n"], 3)}
    return out


# --------------------------------------------------------------------------- #
# Model Ledger: log -> lock -> settle a pre-kickoff 1X2 forecast vs the market
# --------------------------------------------------------------------------- #
_OUTCOME_IDX = {"a": 0, "draw": 1, "b": 2}


def _brier3(p: tuple, idx: int) -> float:
    """3-way Brier: sum (p_i - o_i)^2 over {a, draw, b}. 0 best, 2 worst, ~0.667 = a flat guess."""
    o = [0.0, 0.0, 0.0]
    o[idx] = 1.0
    return round(sum((p[i] - o[i]) ** 2 for i in range(3)), 4)


def _rps3(p: tuple, idx: int) -> float:
    """Ranked probability score over the ORDERED outcomes {a-win, draw, b-win}: penalizes being far on
    the ordinal scale (calling a blowout the wrong way costs more than missing a draw). 0 best, 1 worst."""
    o = [0.0, 0.0, 0.0]
    o[idx] = 1.0
    cum_p = cum_o = s = 0.0
    for i in range(2):                       # first r-1 = 2 cumulative steps
        cum_p += p[i]
        cum_o += o[i]
        s += (cum_p - cum_o) ** 2
    return round(s / 2.0, 4)


def log_forecasts(candidates: list[dict], today: str) -> int:
    """Insert a PENDING forecast row per upcoming game (probabilities are NOT stored yet; they freeze at
    lock). Forward-only backstop: refuses any game dated before yesterday, so a played game can never be
    backfilled (the live model has ingested played results; a backfill would be hindsight). The primary
    kicked-off filter is kickoff-aware in the aggregator (_forecast_board excludes started games); the
    one-day tolerance here covers market dates that lag a post-midnight-UTC kickoff. Idempotent via
    dedup_key. candidates = [{match, team_a, team_b, commence_time, stage, dedup_key}]."""
    if not candidates:
        return 0
    try:
        floor = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    except ValueError:
        floor = today
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    inserted = 0
    sport = _active_sport().key
    with _conn() as c:
        for r in candidates:
            if not r.get("dedup_key") or (r.get("commence_time") or "")[:10] < floor:
                continue                     # forward-only backstop (kickoff-aware filter is upstream)
            cur = c.execute(
                """INSERT OR IGNORE INTO forecasts
                   (match, team_a, team_b, commence_time, stage, logged_at, dedup_key, sport)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (r["match"], r["team_a"], r["team_b"], r.get("commence_time"),
                 r.get("stage"), now, r["dedup_key"], sport),
            )
            inserted += cur.rowcount
    return inserted


def lock_forecasts(board: dict, now_iso: str) -> int:
    """Freeze the model + market 1X2 for any PENDING forecast inside its lock window, exactly once. The
    aggregator decides the timing (it owns the UTC kickoff math) and passes per-game flags:
    board = {dedup_key: {lock_now, missed, kickoff_iso, model:(a,draw,b), market:(a,draw,b), sources}}.
    lock_now -> freeze; missed (already kicked off, never locked) -> void; a stale pending row whose live
    market/kickoff has vanished and whose date is past -> void. now_iso is the UTC lock stamp."""
    if not now_iso:
        return 0
    cutoff = now_iso[:10]                     # model trained on results through ~now; this game is future
    try:
        # a boardless pending row is only STALE once its market date is 2+ days gone: market dates lag
        # post-midnight-UTC kickoffs by a day, so voiding at `cutoff` would kill tonight's late games
        stale_before = (date.fromisoformat(cutoff) - timedelta(days=1)).isoformat()
    except ValueError:
        stale_before = cutoff
    locked = 0
    with _conn() as c:
        # a LOCKED game whose final never arrived (postponed after the lock) can no longer grade once
        # its date falls outside the results window; void it so a future makeup or series game can
        # never masquerade as its result. wc26 games always complete well inside their 40-day window.
        clause, sparams = _sport_clause()
        window = _active_sport().results_window_days
        try:
            no_final_by = (date.fromisoformat(cutoff) - timedelta(days=max(window - 1, 2))).isoformat()
            c.execute(f"UPDATE forecasts SET status='void' WHERE status='locked' "
                      f"AND commence_time < ? AND {clause}", (no_final_by, *sparams))
        except ValueError:
            pass
        rows = c.execute(
            "SELECT id, dedup_key, commence_time FROM forecasts WHERE status='pending'"
        ).fetchall()
        for r in rows:
            b = board.get(r["dedup_key"])
            if not b:
                if (r["commence_time"] or "9999")[:10] < stale_before:
                    c.execute("UPDATE forecasts SET status='void' WHERE id=?", (r["id"],))
                continue
            if b.get("missed"):
                c.execute("UPDATE forecasts SET status='void' WHERE id=?", (r["id"],))
                continue
            if not b.get("lock_now"):
                continue
            m = b.get("model") or (None, None, None)   # anchor-only sports lock the market line alone
            k = b["market"]
            cur = c.execute(
                """UPDATE forecasts SET status='locked', lock_ts=?, kickoff_iso=?, model_cutoff=?,
                   model_a=?, model_draw=?, model_b=?, market_a=?, market_draw=?, market_b=?,
                   market_sources=?, legs_json=? WHERE id=? AND status='pending'""",
                (now_iso, b.get("kickoff_iso"), cutoff, m[0], m[1], m[2], k[0], k[1], k[2],
                 b.get("sources"), json.dumps(b.get("legs") or []), r["id"]),
            )
            locked += cur.rowcount
    return locked


def _ou_result(side: str, actual, line) -> str:
    """Grade an over/under leg. Lines are .5 so a push never happens, but it is handled for safety."""
    if actual is None or line is None:
        return "pending"
    if actual == line:
        return "push"
    over = actual > line
    return "won" if ((side == "over") == over) else "lost"


def _grade_legs(legs: list[dict], ga: int, gb: int, team_a: str, team_b: str, corners_total,
                players: dict | None = None, innings: dict | None = None) -> list[dict]:
    """Grade each extra-market leg against the result. Goals markets settle off the ESPN score; the
    corners leg needs an API-Football count; player props settle off the ESPN box-score player lines
    (a posted box score without the player = DNP = void, the standard prop convention)."""
    out = []
    for leg in legs:
        k = leg.get("key")
        side = leg.get("side")
        line = leg.get("line")
        actual, result = None, "pending"
        if k == "total_goals":
            actual = ga + gb
            result = _ou_result(side, actual, line)
        elif k == "team_total":
            actual = ga if leg.get("team") == team_a else gb
            result = _ou_result(side, actual, line)
        elif k == "btts":
            actual = "yes" if (ga > 0 and gb > 0) else "no"
            result = "won" if side == actual else "lost"
        elif k == "f5":
            ia, ib = (innings or {}).get(team_a), (innings or {}).get(team_b)
            if ia and ib and len(ia) >= 5 and len(ib) >= 5:
                fa, fb = sum(ia[:5]), sum(ib[:5])
                actual = team_a if fa > fb else team_b if fb > fa else "tie"
                result = "won" if side == actual else "lost"
        elif k == "spread":
            cover = leg.get("team")
            if cover in (team_a, team_b):
                actual = (ga - gb) if cover == team_a else (gb - ga)   # the covering team's final margin
                if actual == line:
                    result = "push"                                  # cannot happen on a .5 line
                else:
                    result = "won" if (side == "cover") == (actual > line) else "lost"
        elif k == "player_prop":
            pl = (players or {}).get(leg.get("player_key") or "")
            if pl is not None:
                actual = pl.get(leg.get("stat"))
                result = _ou_result(side, actual, line) if actual is not None else "pending"
            elif players:
                result = "void"          # box score posted, player never appeared (DNP)
        elif k == "corners":
            if corners_total is not None:
                actual = corners_total
                result = _ou_result(side, actual, line)
        graded = {**leg, "actual": actual, "result": result}
        # forward-graded performance-aware variant: grade its over/under against the same goals result
        if leg.get("perf_side") and k in ("total_goals", "team_total") and actual is not None:
            graded["perf_result"] = _ou_result(leg["perf_side"], actual, line)
        out.append(graded)
    return out


def _corners_total_index(team_stats: dict | None) -> dict:
    """{frozenset(team_keys): total corners} from the API-Football team-stats map (both teams must report)."""
    idx: dict = {}
    for (date, teams), d in (team_stats or {}).items():
        vals = [(v or {}).get("corners") for v in d.values() if isinstance(v, dict)]
        if vals and all(x is not None for x in vals):
            idx[teams] = sum(vals)
    return idx


def capture_forecast_close(board: dict, now_iso: str | None = None) -> None:
    """Overwrite-until-start closing capture for LOCKED forecasts (the ledger's CLV analog): every
    tick before the game begins rewrites closing_*, so the last pre-start write IS the close. Never
    written once the game has started (live prices are not a closing line).

    Two independent stops. The board flags a started game as `missed`, but only while it still knows
    the kickoff: if ESPN drops the game for a refresh, the board can keep emitting it, unflagged, with
    in-play prices. So when `now_iso` is given, the row's own kickoff_iso (frozen at lock) is checked
    too, and the close holds at first pitch whatever the board says."""
    if not board:
        return
    now = _utc(now_iso)
    with _conn() as c:
        rows = c.execute(
            "SELECT id, dedup_key, kickoff_iso FROM forecasts WHERE status='locked'").fetchall()
        for r in rows:
            b = board.get(r["dedup_key"])
            if not b or b.get("missed"):
                continue
            ko = _utc(r["kickoff_iso"])
            if now and ko and now >= ko:
                continue
            k = b["market"]
            c.execute("UPDATE forecasts SET closing_a=?, closing_draw=?, closing_b=? WHERE id=?",
                      (k[0], k[1], k[2], r["id"]))


def settle_forecasts(results: list[dict], team_stats: dict | None = None) -> int:
    """Grade forecasts whose game has an ESPN result. The 1X2 settles once (status locked -> settled):
    actual_outcome comes from the 90+ET goals, so a level knockout grades as a DRAW (penalties are flagged
    in `pens`, never graded). The extra-market legs grade alongside it, and a pending corners leg can fill
    in on a later pass once API-Football posts the count. Returns the number of newly-settled games."""
    if not results:
        return 0
    idx: dict = {}
    for g in results:
        ks = list((g.get("goals") or {}).keys())
        if len(ks) == 2:
            idx.setdefault(frozenset(ks), []).append(g)
    if not idx:
        return 0
    corners_idx = _corners_total_index(team_stats)
    settled = 0
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM forecasts WHERE status IN ('locked','settled') "
            "AND market_a IS NOT NULL"
        ).fetchall()
        for r in rows:
            # A knockout pair plays once, so pair-only matching is exact for wc26 (its Kalshi market
            # date can lag the ESPN result date, so a date check would regress late kickoffs there).
            # Daily sports replay the same pair across a series and a POSTPONED game's pair will
            # complete a different game later, so they may only grade against a same-date (or exact
            # scheduled-start) final; anything else stays unsettled rather than guessing.
            cands = idx.get(frozenset((r["team_a"], r["team_b"]))) or []
            pair_exact = _active_sport().pair_only_key
            if pair_exact and len(cands) == 1:
                g = cands[0]
            else:
                same_date = [x for x in cands if x.get("date") == (r["commence_time"] or "")[:10]]
                pool = same_date if not pair_exact else (same_date or cands)
                g = next((x for x in pool if x.get("iso") and r["kickoff_iso"]
                          and x["iso"] == r["kickoff_iso"]), None)
                if g is None and len(pool) == 1:
                    g = pool[0]
            if not g:
                continue
            ga, gb = g["goals"].get(r["team_a"]), g["goals"].get(r["team_b"])
            if ga is None or gb is None:
                continue
            if ga == gb and not (r["market_draw"] or 0):
                continue   # a level score in a 2-way sport is a suspended oddity, never a gradeable draw
            try:
                legs = json.loads(r["legs_json"]) if r["legs_json"] else []
            except (TypeError, ValueError):
                legs = []
            graded = _grade_legs(legs, ga, gb, r["team_a"], r["team_b"],
                                 corners_idx.get(frozenset((r["team_a"], r["team_b"]))),
                                 players=g.get("players"), innings=g.get("innings"))
            if r["status"] == "locked":
                outcome = "a" if ga > gb else "b" if gb > ga else "draw"
                oi = _OUTCOME_IDX[outcome]
                pens = 1 if (outcome == "draw" and g.get("winner")) else 0
                kp = (r["market_a"], r["market_draw"], r["market_b"])
                if r["model_a"] is not None:      # model column rides only where a model exists
                    mp = (r["model_a"], r["model_draw"], r["model_b"])
                    bm, rm = _brier3(mp, oi), _rps3(mp, oi)
                    hit = 1 if max(range(3), key=lambda i: mp[i]) == oi else 0
                else:
                    bm = rm = hit = None
                c.execute(
                    """UPDATE forecasts SET status='settled', actual_a=?, actual_b=?, actual_outcome=?,
                       pens=?, brier_model=?, brier_market=?, rps_model=?, rps_market=?, hit_model=?,
                       legs_json=? WHERE id=? AND status='locked'""",
                    (ga, gb, outcome, pens, bm, _brier3(kp, oi),
                     rm, _rps3(kp, oi), hit, json.dumps(graded), r["id"]),
                )
                settled += 1
            elif graded != legs:                 # already settled: only rewrite if a leg newly graded
                c.execute("UPDATE forecasts SET legs_json=? WHERE id=?", (json.dumps(graded), r["id"]))
    return settled


def _sport_clause(alias: str = "") -> tuple[str, tuple]:
    """SQL filter scoping ledger reads to the active sport; legacy NULL rows belong to wc26."""
    key = _active_sport().key
    return f"({alias}sport = ? OR (? = 'wc26' AND {alias}sport IS NULL))", (key, key)


def list_forecasts() -> list[dict]:
    """Locked (awaiting result) + settled forecasts for the ACTIVE sport, soonest-undecided first."""
    clause, params = _sport_clause()
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM forecasts WHERE status IN ('locked','settled') AND {clause} "
            "ORDER BY status='locked' DESC, commence_time DESC, id DESC", params
        ).fetchall()
        # A locked row whose pair has since played a LATER game that already settled was almost certainly
        # rained out: its own final is never arriving, and the same-date settlement pool means a makeup
        # can never grade it. Label it so the board stops promising a result, but leave the status alone.
        # Voiding here would be irreversible and would run before settle_forecasts, destroying the rare
        # suspended game whose final does legitimately land later; the sport-derived long-stop in
        # lock_forecasts stays the only locked -> void path.
        postponed = {
            r["id"] for r in c.execute(
                f"SELECT f.id FROM forecasts f WHERE f.status='locked' AND {_sport_clause('f.')[0]} "
                "AND EXISTS (SELECT 1 FROM forecasts f2 WHERE f2.team_a = f.team_a "
                "AND f2.team_b = f.team_b AND f2.status='settled' "
                f"AND f2.commence_time > f.commence_time AND {_sport_clause('f2.')[0]})",
                (*_sport_clause()[1], *_sport_clause()[1]),
            ).fetchall()
        }
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["legs"] = json.loads(r["legs_json"]) if r["legs_json"] else []
        except (TypeError, ValueError):
            d["legs"] = []
        d["likely_postponed"] = r["id"] in postponed
        out.append(d)
    return out


def forecast_calibration() -> dict:
    """Scorecard over the ACTIVE sport's SETTLED forecasts. Where a model exists (wc26) it is the
    paired model-vs-market comparison; anchor-only sports get the market's own calibration (Brier +
    favorite hit rate). Gated: aggregates are withheld until FORECAST_MIN_N settle, and even then it
    stays exploratory (wide CIs, do NOT retrain on it)."""
    min_n = getattr(config, "FORECAST_MIN_N", 8)
    clause, params = _sport_clause()
    with _conn() as c:
        mrows = c.execute(
            "SELECT brier_model, brier_market, rps_model, rps_market, hit_model "
            f"FROM forecasts WHERE status='settled' AND brier_model IS NOT NULL AND {clause}", params
        ).fetchall()
        krows = c.execute(
            "SELECT market_a, market_draw, market_b, actual_outcome, brier_market "
            f"FROM forecasts WHERE status='settled' AND brier_market IS NOT NULL AND {clause}", params
        ).fetchall()
        locked = c.execute(
            f"SELECT COUNT(*) FROM forecasts WHERE status='locked' AND {clause}", params
        ).fetchone()[0]
        crows = c.execute(
            "SELECT closing_a, closing_draw, closing_b, actual_outcome, brier_market "
            f"FROM forecasts WHERE status='settled' AND closing_a IS NOT NULL AND {clause}", params
        ).fetchall()
    n = len(mrows)
    out = {"n": n, "min_n": min_n, "ready": n >= min_n, "locked_pending": locked}
    # the market's own record (always available; for anchor-only sports it IS the ledger)
    kn = len(krows)
    out["market_n"] = kn
    if kn >= min_n:
        fav_hits = 0
        for r in krows:
            kp = (r["market_a"], r["market_draw"] or 0.0, r["market_b"])
            fav = max(range(3), key=lambda i: kp[i])
            fav_hits += 1 if fav == _OUTCOME_IDX.get(r["actual_outcome"], -1) else 0
        out["market_brier"] = round(sum(r["brier_market"] for r in krows) / kn, 3)
        out["market_hit_rate"] = round(fav_hits / kn * 100, 1)
    # lock vs close: does the T-75 line lose information to the closing line? (paired, same games)
    cn = len(crows)
    out["close_n"] = cn
    if cn >= min_n:
        cb = sum(_brier3((r["closing_a"], r["closing_draw"] or 0.0, r["closing_b"]),
                         _OUTCOME_IDX[r["actual_outcome"]]) for r in crows) / cn
        out["close_brier"] = round(cb, 3)
        out["lock_brier_on_closed"] = round(sum(r["brier_market"] for r in crows) / cn, 3)
    if n < min_n:
        return out                            # gate: withhold the model aggregate until the sample is real
    bm = sum(r["brier_model"] for r in mrows) / n
    bk = sum(r["brier_market"] for r in mrows) / n
    out.update({
        "brier_model": round(bm, 3), "brier_market": round(bk, 3),
        "rps_model": round(sum(r["rps_model"] for r in mrows) / n, 3),
        "rps_market": round(sum(r["rps_market"] for r in mrows) / n, 3),
        "hit_rate": round(sum(r["hit_model"] for r in mrows) / n * 100, 1),
        "skill_vs_market": round((1 - bm / bk) * 100, 1) if bk else None,  # +ve = model beats the market
        "beat_market": sum(1 for r in mrows if r["brier_model"] < r["brier_market"]),
    })
    return out


def summary() -> dict:
    picks = list_picks()
    by_arch = {}
    for arch in sorted({p["archetype"] for p in picks}):
        by_arch[arch] = _agg([p for p in picks if p["archetype"] == arch])
    real = [p for p in picks if p.get("real_money")]
    bankroll, curve = _bankroll_curve(picks)
    return {"overall": _agg(picks), "by_archetype": by_arch,
            "real": _agg(real), "real_count": len(real),
            "model_calibration": model_calibration(),
            "start_bankroll": round(config.BANKROLL, 2), "bankroll": bankroll,
            "bankroll_curve": curve, "unit_dollars": round(config.BANKROLL * config.UNIT_PCT, 2)}
