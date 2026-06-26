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
from pathlib import Path

from .. import config
from ..matching import normalize_team

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
    game_over_at TEXT             -- stamped when the pick's game has a result (still pending = needs grading)
);
"""

# additive columns for DBs created before these features existed
_MIGRATE = [("odds_type", "TEXT"), ("popularity", "INTEGER"),
            ("on_favorite", "INTEGER"), ("agreement_pp", "REAL"),
            ("closing_locked_at", "TEXT"), ("real_money", "INTEGER"),
            ("stake_units", "REAL"), ("legs_json", "TEXT"), ("game_over_at", "TEXT"),
            ("model_prob", "REAL")]


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_paper() -> None:
    Path(config.DB_PATH).touch(exist_ok=True)
    with _conn() as c:
        c.executescript(_SCHEMA)
        for col, typ in _MIGRATE:
            try:
                c.execute(f"ALTER TABLE paper_picks ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass  # column already exists


def log_picks(rows: list[dict]) -> int:
    """Insert picks, ignoring duplicates (same match+archetype+selection on the same day)."""
    if not rows:
        return 0
    inserted = 0
    with _conn() as c:
        for r in rows:
            cur = c.execute(
                """INSERT OR IGNORE INTO paper_picks
                   (logged_at, match, archetype, selection, confidence, commence_time,
                    pick_fair_prob, pick_price_decimal, model_prob, dedup_key,
                    odds_type, popularity, on_favorite, agreement_pp, stake_units, legs_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r.get("logged_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
                 r["match"], r["archetype"], r["selection"], r.get("confidence"),
                 r.get("commence_time"), r.get("pick_fair_prob"), r.get("pick_price_decimal"),
                 r.get("model_prob"), r["dedup_key"], r.get("odds_type"), r.get("popularity"),
                 r.get("on_favorite"), r.get("agreement_pp"), r.get("stake_units") or 1.0,
                 r.get("legs_json")),
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
              for r in resolved if r.get("date") and r.get("result") in ("won", "lost")}
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


def capture_closing(fair_now: dict) -> None:
    """Update closing_fair_prob for pending ML picks whose match is still live in `fair_now`
    ({match: current_fair_prob}). Overwriting until the market disappears ≈ locking the close."""
    if not fair_now:
        return
    with _conn() as c:
        rows = c.execute(
            "SELECT id, match FROM paper_picks WHERE status='pending' "
            "AND archetype NOT IN ('parlay', 'total_corners') AND pick_fair_prob IS NOT NULL"
        ).fetchall()
        for r in rows:
            cur = fair_now.get(r["match"])
            if cur is not None:
                c.execute("UPDATE paper_picks SET closing_fair_prob=? WHERE id=?", (cur, r["id"]))


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
    with _conn() as c:
        rows = c.execute("SELECT * FROM paper_picks ORDER BY id DESC").fetchall()
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
    with _conn() as c:
        rows = c.execute(
            "SELECT archetype, model_prob, status FROM paper_picks "
            "WHERE model_prob IS NOT NULL AND status IN ('won','lost')"
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
