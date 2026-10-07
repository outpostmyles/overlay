"""Paper-trading proof engine — auto-logs every AI pick and tracks the track record.

Each AI-recommended bet is logged here automatically (when you hit Analyze) so the system builds
a verifiable record before any real money. The headline metric is **Closing Line Value (CLV)** —
the research's #1 predictor of long-run profit — which we compute purely from our own free feeds:
the no-vig fair price when the pick was logged vs. the fair price as it moves toward kickoff. No
results feed needed for CLV. Win/loss settlement is graded manually for now (status dropdown).

CLV %: (closing_fair_prob / pick_fair_prob - 1) * 100. Positive = the line moved toward our pick
(we got the better price) = beat the close. Only meaningful for favorite-ML picks, where we have a
clean market fair line on both sides, and only for a close taken BEFORE kickoff (see _close_flag): a
price written once the game is under way tracks the score, not the market's view.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
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
    sport TEXT,                   -- multi-sport namespace; NULL on legacy rows means wc26
    closing_at TEXT               -- UTC time the close was last written by the pre-start capture
);
"""

# additive columns for DBs created before these features existed
_MIGRATE = [("odds_type", "TEXT"), ("popularity", "INTEGER"),
            ("on_favorite", "INTEGER"), ("agreement_pp", "REAL"),
            ("closing_locked_at", "TEXT"), ("real_money", "INTEGER"),
            ("stake_units", "REAL"), ("legs_json", "TEXT"), ("game_over_at", "TEXT"),
            ("model_prob", "REAL"),
            ("sport", "TEXT"),   # multi-sport namespace; NULL on legacy rows means wc26
            ("closing_at", "TEXT")]   # NULL on a close written before the capture stopped at kickoff


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
        # the research layer (football): the Research % frozen beside the market at the same lock, its
        # Brier once graded, and the factors + context it was built from (for the study)
        for col, typ in (("research_a", "REAL"), ("research_draw", "REAL"), ("research_b", "REAL"),
                         ("brier_research", "REAL"), ("research_json", "TEXT")):
            _alter(c, f"ALTER TABLE forecasts ADD COLUMN {col} {typ}")


def game_day(commence_time: str | None, logged_at: str | None = None) -> str:
    """The date a pick's game is played (YYYY-MM-DD), or, when the feed gave no game time, the day the
    pick was logged. It is the date a pick is deduped on, so a game read again on a later day is the
    same game, not a new one."""
    return (commence_time or logged_at or time.strftime("%Y-%m-%d"))[:10]


def log_picks(rows: list[dict]) -> int:
    """Insert picks, skipping any the ledger already holds for the same game: same sport, game date,
    match, archetype and selection. The caller's dedup_key used to start with the day the pick was
    LOGGED, so reading a game on two different days logged the same bet twice (Spain ML v Saudi
    Arabia went in on Jun 19 and again on Jun 21). Checking the columns, not just the key, also
    catches a pick whose earlier copy was keyed the old way."""
    if not rows:
        return 0
    inserted = 0
    sport = _active_sport().key
    clause, params = _sport_clause()
    with _conn() as c:
        for r in rows:
            held = c.execute(
                f"SELECT 1 FROM paper_picks WHERE {clause} AND match=? AND archetype=? AND selection=? "
                "AND COALESCE(NULLIF(substr(commence_time, 1, 10), ''), substr(logged_at, 1, 10)) = ? LIMIT 1",
                (*params, r["match"], r["archetype"], r["selection"],
                 game_day(r.get("commence_time"), r.get("logged_at")))).fetchone()
            if held:
                continue
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
    """Parse an ISO stamp ('...Z' or '+00:00', with or without seconds) to an aware datetime, or None. A
    stamp with no offset is read as UTC, so two stamps can always be compared or subtracted."""
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


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
    the other side's price. A pick whose game has started keeps its last pre-start value.

    Each write stamps closing_at, the record that this close was taken before kickoff: CLV counts a
    close only with that stamp (see _close_flag), since the history written before this cutoff kept
    going through the game."""
    if not fair_now:
        return
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _conn() as c:
        rows = c.execute(
            "SELECT id, match, commence_time, selection FROM paper_picks WHERE status='pending' "
            "AND archetype='favorite_ml' AND pick_fair_prob IS NOT NULL"
        ).fetchall()
        for r in rows:
            hit = fair_now.get((r["match"], (r["commence_time"] or "")[:10]))
            if hit and hit[0] == r["selection"] and hit[1] is not None:
                c.execute("UPDATE paper_picks SET closing_fair_prob=?, closing_at=? WHERE id=?",
                          (hit[1], now, r["id"]))


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


# The top of a book: a 99 cent contract de-vigs to about 0.989, which is where a decided game trades. A
# pre-game favorite that short is rare and its close could move at most 1.5 points, so leaving one out
# costs nothing, while counting the pinned ones leaks the result (10 of the 23 WC closes sat at 0.989,
# and all 10 won).
CLOSE_CLAMP = 0.985


def _close_flag(r: sqlite3.Row) -> str | None:
    """Why a pick's stored close can not be used as its closing line, or None when it can (or when the
    pick has no close). A closing line is the last price BEFORE the game starts:
      'void'    the pick was voided (a duplicate, a push), so it is not a bet and its close is not counted
      'clamp'   the price is pinned at the top of the book (CLOSE_CLAMP), where a decided game trades
      'untimed' there is no record that the close was taken before kickoff. capture_closing stamps
                closing_at, and it only ever receives games whose verified kickoff is still ahead, so a
                stamped close is pre-start by construction. An unstamped one predates that cutoff
                (2026-10-06): the old capture kept overwriting through the game until the pick settled,
                so it may be any price up to the final whistle (France v Sweden kicked off at 21:00Z and
                its close was frozen at 22:58 on settlement)."""
    if r["closing_fair_prob"] is None:
        return None
    if r["status"] == "void":
        return "void"
    if r["closing_fair_prob"] >= CLOSE_CLAMP:
        return "clamp"
    if not r["closing_at"]:
        return "untimed"
    return None


def _clv(r: sqlite3.Row) -> float | None:
    """CLV % against a real pre-game close, else None. Every aggregate (the Track Record headline, the
    per-archetype rows, calibration memory) reads this, so a close _close_flag rejects is left out of
    all of them at once rather than filtered in each."""
    if r["archetype"] == "parlay":
        return None  # a parlay has no single closing line — CLV doesn't apply
    if r["pick_fair_prob"] and r["closing_fair_prob"] and _close_flag(r) is None:
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
        d["close_flag"] = _close_flag(r)   # why a stored close is not counted (the UI says so beside it)
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
        "priced": len(priced),                       # P/L and ROI count only these (props carry no price)
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
        # (A game ESPN marks canceled or postponed voids sooner, in settle_forecasts; this is the
        # backstop for one ESPN never flags, or moves off its date.)
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
            rs = b.get("research") or {}
            rp = rs.get("probs") or (None, None, None)
            rj = json.dumps({x: rs.get(x) for x in ("factors", "uncertain", "cushion", "value_at", "dk",
                                                     "context")}) if rs else None
            cur = c.execute(
                """UPDATE forecasts SET status='locked', lock_ts=?, kickoff_iso=?, model_cutoff=?,
                   model_a=?, model_draw=?, model_b=?, market_a=?, market_draw=?, market_b=?,
                   market_sources=?, legs_json=?, research_a=?, research_draw=?, research_b=?,
                   research_json=? WHERE id=? AND status='pending'""",
                (now_iso, b.get("kickoff_iso"), cutoff, m[0], m[1], m[2], k[0], k[1], k[2],
                 b.get("sources"), json.dumps(b.get("legs") or []), rp[0], rp[1], rp[2], rj, r["id"]),
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


# A doubleheader's other game starts three or more hours from this one (game 1 has to finish first), and
# ESPN can nudge a start after the lock (a nightcap rescheduled into a doubleheader moved 23:05Z to
# 23:30Z), so a final within this window of the frozen kickoff is the row's game and nothing else is.
_SAME_GAME = timedelta(hours=3)


def _start_gap(r, x: dict) -> timedelta | None:
    """How far ESPN game `x` started from the kickoff row `r` froze at lock (None if either is unknown)."""
    ko, start = _utc(r["kickoff_iso"]), _utc(x.get("iso"))
    return abs(start - ko) if ko and start else None


def _same_game(r, cands: list[dict], pair_exact: bool, listed_n: int | None = None) -> dict | None:
    """The ESPN game in `cands` (the pair's finals, plus any start ESPN called off) that IS forecast row
    `r`'s game, or None to leave the row waiting rather than guess.

    A knockout pair plays once, so pair-only matching is exact for wc26 (its Kalshi market date can lag
    the ESPN result date, so a date check would regress late kickoffs there). Daily sports replay the
    same pair across a series, and a POSTPONED game's pair completes a different game later, so they
    only grade against a same-date game. Within that, a row that froze a kickoff takes the game that
    started nearest it, and only inside _SAME_GAME. It never takes a lone same-date final from further
    away: while a nightcap is still being played, game 1 is the pair's only final that day, and nine MLB
    nightcaps settled on game 1's score that way. Only a row or final with no start on record (rows
    frozen before kickoffs were kept, finals cached before starts were) falls back to the lone final.

    `cands` can also hold starts ESPN lists that are not final yet ("pending"): when the nearest start is
    one of those, the row's own game is still to come, so it waits even if a nudged game 1 sits inside
    the window. `listed_n` is how many games the pair has on that date's scoreboard: with exactly one,
    its final is the row's game however far a rain delay moved the start."""
    day = (r["commence_time"] or "")[:10]
    same_date = [x for x in cands if x.get("date") == day]
    pool = (same_date or cands) if pair_exact else same_date
    near = [(gap, x) for x in pool if (gap := _start_gap(r, x)) is not None and gap < _SAME_GAME]
    if near:
        best = min(near, key=lambda t: t[0])[1]
        return None if best.get("pending") else best
    finals = [x for x in pool if not x.get("called_off") and not x.get("pending")]
    if len(finals) == 1 and len(pool) == 1 and (listed_n == 1 or _start_gap(r, finals[0]) is None):
        return finals[0]
    return None


def _row_legs(r) -> list[dict]:
    """A forecast row's stored legs (graded or not), [] when absent or unreadable."""
    try:
        return json.loads(r["legs_json"]) if r["legs_json"] else []
    except (TypeError, ValueError):
        return []


def grade_forecast(r, g: dict, corners_total=None) -> dict | None:
    """Every graded column of forecast row `r` settled against ESPN final `g`, as {column: value}, or None
    when `g` cannot grade it (a side missing from the score, or a level score in a 2-way sport). Pure, so
    settle_forecasts writes exactly what a one-off regrade of a mis-settled row would.

    actual_outcome comes from the 90+ET goals, so a level knockout grades as a DRAW (penalties are flagged
    in `pens`, never graded). The extra-market legs grade off the same final; the corners leg needs the
    API-Football count in `corners_total`."""
    goals = g.get("goals") or {}
    ga, gb = goals.get(r["team_a"]), goals.get(r["team_b"])
    if ga is None or gb is None:
        return None
    if ga == gb and not (r["market_draw"] or 0):
        return None    # a level score in a 2-way sport is a suspended oddity, never a gradeable draw
    graded = _grade_legs(_row_legs(r), ga, gb, r["team_a"], r["team_b"], corners_total,
                         players=g.get("players"), innings=g.get("innings"))
    outcome = "a" if ga > gb else "b" if gb > ga else "draw"
    oi = _OUTCOME_IDX[outcome]
    kp = (r["market_a"], r["market_draw"], r["market_b"])
    if r["model_a"] is not None:      # model column rides only where a model exists
        mp = (r["model_a"], r["model_draw"], r["model_b"])
        bm, rm = _brier3(mp, oi), _rps3(mp, oi)
        hit = 1 if max(range(3), key=lambda i: mp[i]) == oi else 0
    else:
        bm = rm = hit = None
    rsp = (r["research_a"], r["research_draw"] or 0.0, r["research_b"])
    return {"actual_a": ga, "actual_b": gb, "actual_outcome": outcome,
            "pens": 1 if (outcome == "draw" and g.get("winner")) else 0,
            "brier_model": bm, "brier_market": _brier3(kp, oi), "rps_model": rm, "rps_market": _rps3(kp, oi),
            "hit_model": hit, "legs_json": json.dumps(graded),
            "brier_research": _brier3(rsp, oi) if r["research_a"] is not None else None}


def settle_forecasts(results: list[dict], team_stats: dict | None = None,
                     called_off: dict | None = None, listed: dict | None = None) -> int:
    """Grade forecasts whose game has an ESPN result. The 1X2 settles once (status locked -> settled) with
    the columns grade_forecast returns. The extra-market legs grade alongside it, and a pending corners
    leg can fill in on a later pass once API-Football posts the count.

    called_off is espn.CALLED_OFF's shape, {(pair, date): {start_iso: status}}: a locked game whose own
    start ESPN reports canceled or postponed voids now, instead of sitting "awaiting result" until the
    results-window long-stop in lock_forecasts. It is matched like a final (nearest start inside
    _SAME_GAME), so a doubleheader's called-off game never voids its sibling, and a played final at the
    row's own start always wins. listed is espn.LISTED's shape, {(pair, date): {start_iso: completed}}:
    every start the scoreboard shows, so an unfinished game can hold its row (see _same_game).
    Returns the number of newly-settled games."""
    idx: dict = {}
    for g in results or []:
        ks = list((g.get("goals") or {}).keys())
        if len(ks) == 2:
            idx.setdefault(frozenset(ks), []).append(g)
    for (pair, day), starts in (called_off or {}).items():
        for iso, status in starts.items():
            idx.setdefault(pair, []).append({"date": day, "iso": iso, "called_off": status})
    for (pair, day), starts in (listed or {}).items():
        for iso, completed in starts.items():
            if not completed and iso not in ((called_off or {}).get((pair, day)) or {}):
                idx.setdefault(pair, []).append({"date": day, "iso": iso, "pending": True})
    if not idx:
        return 0
    corners_idx = _corners_total_index(team_stats)
    pair_exact = _active_sport().pair_only_key
    settled = voided = 0
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM forecasts WHERE status IN ('locked','settled') "
            "AND market_a IS NOT NULL"
        ).fetchall()
        for r in rows:
            pair = frozenset((r["team_a"], r["team_b"]))
            listed_n = len((listed or {}).get((pair, (r["commence_time"] or "")[:10])) or {}) or None
            g = _same_game(r, idx.get(pair) or [], pair_exact, listed_n)
            if not g:
                continue
            if g.get("called_off"):
                if r["status"] == "locked":       # a settled row is history; only a regrade rewrites it
                    voided += c.execute("UPDATE forecasts SET status='void' WHERE id=? AND status='locked'",
                                        (r["id"],)).rowcount
                continue
            f = grade_forecast(r, g, corners_idx.get(pair))
            if f is None:
                continue
            if r["status"] == "locked":
                sets = ", ".join(f"{k}=?" for k in f)
                c.execute(f"UPDATE forecasts SET status='settled', {sets} WHERE id=? AND status='locked'",
                          (*f.values(), r["id"]))
                settled += 1
            elif json.loads(f["legs_json"]) != _row_legs(r):   # settled: rewrite only a newly graded leg
                c.execute("UPDATE forecasts SET legs_json=? WHERE id=?", (f["legs_json"], r["id"]))
    if voided:
        print(f"[ledger] voided {voided} locked game(s) ESPN reports canceled or postponed")
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
        # suspended game whose final does legitimately land later. The only locked -> void paths are
        # ESPN's own canceled/postponed status (settle_forecasts) and the sport-derived long-stop in
        # lock_forecasts.
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
        try:
            d["research"] = json.loads(r["research_json"]) if r["research_json"] else None
        except (TypeError, ValueError):
            d["research"] = None
        out.append(d)
    return out


# Price bands for the favorites tracker, labelled the way the bets are placed (American odds). A
# three-way favorite can sit under 50% (a soccer game with a live draw: 11 World Cup favorites did), so
# it gets the first band instead of falling through every band while still counting toward n.
_FAV_BANDS = ((0.50, 0.60, "-100 to -150"),
              (0.60, 0.70, "-150 to -233"), (0.70, 0.75, "-233 to -300"), (0.75, 1.01, "-300 or shorter"))


def favorites_by_price() -> dict:
    """How the market's favorite has done at each price, pooled across every live board, not just the board
    asking. A 75% favorite is a 75% favorite in any sport, and pooling is the only way the heavy end gets
    a usable sample. An archived sport (the World Cup) stays saved in the ledger but is left out here,
    since the site no longer shows it. One row per settled game: the side the market favored at lock.
    Every counted game sits in exactly one band, and n is their sum, so the table always adds up."""
    from ..sports import get, keys
    archived = {k for k in keys() if get(k).archived}
    with _conn() as c:
        rows = c.execute("SELECT COALESCE(sport, 'wc26') s, market_a, market_draw, market_b, actual_outcome "
                         "FROM forecasts WHERE status='settled' AND actual_outcome IS NOT NULL").fetchall()
    rows = [r for r in rows if r["s"] not in archived]
    games = []
    for r in rows:
        opts = [("a", r["market_a"]), ("b", r["market_b"])] + ([("draw", r["market_draw"])] if r["market_draw"] else [])
        side, p = max(opts, key=lambda x: x[1] or 0)
        if side == "draw" or not p:
            continue
        games.append((r["s"], p, 1 if r["actual_outcome"] == side else 0))

    def band(lo, hi, label):
        g = [x for x in games if lo <= x[1] < hi]
        n = len(g)
        won = sum(x[2] for x in g)
        exp = sum(x[1] for x in g)
        var = sum(x[1] * (1 - x[1]) for x in g)
        sports: dict = {}
        for x in g:
            sports[x[0]] = sports.get(x[0], 0) + 1
        return {"label": label, "lo": round(lo * 100), "n": n, "won": won, "expected": round(exp, 1),
                "said": round(exp / n * 100, 1) if n else None, "actual": round(won / n * 100, 1) if n else None,
                "z": round((won - exp) / var ** 0.5, 2) if var else None, "sports": sports}

    bands = [band(*b) for b in _FAV_BANDS]
    return {"bands": bands, "heavy": band(0.70, 1.01, "-233 or shorter"), "n": sum(b["n"] for b in bands)}


def research_study() -> dict:
    """The research layer's report card, pooled across every sport that runs it (NFL + college today).

    Two questions. First, is the Research % better than the crowd's? Paired Brier on the moneyline for
    every settled game that froze both, and on every leg the research actually moved. Second, factor by
    factor: when the research made a directional claim (the favorite at 70%, the under in 13 mph wind,
    the rested team), how often did it come true against what the crowd priced? A z-score near zero
    means the market already prices that factor; a factor that keeps beating its price is a candidate
    to start adjusting, and an adjusting factor that does not is one to switch off."""
    from .. import research as rsch
    with _conn() as c:
        rows = c.execute(
            "SELECT COALESCE(sport, 'wc26') s, team_a, team_b, actual_outcome, market_a, market_draw, "
            "market_b, research_a, research_draw, research_b, brier_market, brier_research, legs_json, "
            "research_json FROM forecasts WHERE status='settled' AND research_json IS NOT NULL").fetchall()
    ml_n = ml_closer = 0
    ml_bk = ml_br = 0.0
    leg_n = 0
    leg_bk = leg_br = 0.0
    factors: dict = {}
    for r in rows:
        if r["brier_research"] is not None and r["brier_market"] is not None:
            ml_n += 1
            ml_bk += r["brier_market"]
            ml_br += r["brier_research"]
            ml_closer += 1 if r["brier_research"] < r["brier_market"] else 0
        try:
            legs = json.loads(r["legs_json"]) if r["legs_json"] else []
            rj = json.loads(r["research_json"]) or {}
        except (TypeError, ValueError):
            continue
        for leg in legs:   # legs the research moved: Brier on the leg's own side, crowd vs research
            if leg.get("result") not in ("won", "lost") or leg.get("research_prob") is None \
                    or abs(leg["research_prob"] - (leg.get("prob") or 0)) < 1e-9:
                continue
            y = 1.0 if leg["result"] == "won" else 0.0
            leg_n += 1
            leg_bk += (leg["prob"] - y) ** 2
            leg_br += (leg["research_prob"] - y) ** 2
        row = {"team_a": r["team_a"], "team_b": r["team_b"], "actual_outcome": r["actual_outcome"]}
        for f in rj.get("factors") or []:
            hit = rsch.target_hit(f.get("target"), row, legs)
            if hit is None or f.get("p_crowd") is None:
                continue
            a = factors.setdefault(f["key"], {"key": f["key"], "name": f.get("name") or f["key"],
                                              "kind": f.get("kind"), "detail": f.get("detail"),
                                              "source": f.get("source"), "n": 0, "hits": 0,
                                              "crowd": 0.0, "research": 0.0, "var": 0.0, "sports": {}})
            a["n"] += 1
            a["hits"] += hit
            a["crowd"] += f["p_crowd"]
            a["research"] += f.get("p_research") if f.get("p_research") is not None else f["p_crowd"]
            a["var"] += f["p_crowd"] * (1 - f["p_crowd"])
            a["sports"][r["s"]] = a["sports"].get(r["s"], 0) + 1
            if f.get("kind") == "adjust":
                a["kind"] = "adjust"
    out_f = []
    for a in factors.values():
        n = a["n"]
        out_f.append({"key": a["key"], "name": a["name"], "kind": a["kind"], "detail": a["detail"],
                      "source": a["source"], "n": n, "hits": a["hits"],
                      "crowd": round(a["crowd"] / n * 100, 1), "research": round(a["research"] / n * 100, 1),
                      "actual": round(a["hits"] / n * 100, 1),
                      "z": round((a["hits"] - a["crowd"]) / a["var"] ** 0.5, 2) if a["var"] else None,
                      "sports": a["sports"]})
    out_f.sort(key=lambda x: (x["kind"] != "adjust", -x["n"]))
    return {"ml": {"n": ml_n, "brier_market": round(ml_bk / ml_n, 4) if ml_n else None,
                   "brier_research": round(ml_br / ml_n, 4) if ml_n else None,
                   "skill": round((1 - ml_br / ml_bk) * 100, 2) if ml_n and ml_bk else None,
                   "closer": ml_closer},
            "legs": {"n": leg_n, "brier_market": round(leg_bk / leg_n, 4) if leg_n else None,
                     "brier_research": round(leg_br / leg_n, 4) if leg_n else None,
                     "skill": round((1 - leg_br / leg_bk) * 100, 2) if leg_n and leg_bk else None},
            "factors": out_f, "games": len(rows),
            "min_n": getattr(config, "FORECAST_MIN_N", 8)}


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


def count_picks() -> int:
    """How many paper picks this board has logged (the page hides Track Record where there are none)."""
    clause, params = _sport_clause()
    with _conn() as c:
        return c.execute(f"SELECT COUNT(*) FROM paper_picks WHERE {clause}", params).fetchone()[0]
