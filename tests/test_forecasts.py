"""Model Ledger: the pre-kickoff 1X2 forecast store (log -> lock -> settle) and its honesty guards.

The point of this feature is to grade the MODEL against the de-vigged MARKET on the result, so the tests
defend the properties that make that grading honest: forward-only (no backfill), a single immutable lock
before kickoff, a benchmark frozen at the same instant, penalties out of scope, and a small-sample gate.
"""
import importlib
import os
import tempfile

from backend import config


def _fresh_paper():
    config.DB_PATH = tempfile.mktemp(suffix=".db")
    from backend.store import paper
    importlib.reload(paper)
    paper.init_paper()
    return paper


def _cand(a, b, date):
    return {"match": f"{a.title()} vs {b.title()}", "team_a": a, "team_b": b,
            "commence_time": date, "stage": None, "dedup_key": f"fc|{date}|{a}|{b}"}


def _board(key, *, lock_now=False, missed=False, ko="2026-07-04T19:00Z",
           model=(0.55, 0.27, 0.18), market=(0.50, 0.30, 0.20), legs=None):
    return {key: {"lock_now": lock_now, "missed": missed, "kickoff_iso": ko,
                  "model": model, "market": market, "sources": "kalshi", "legs": legs or []}}


# --- scoring math --------------------------------------------------------- #
def test_brier_and_rps_math():
    paper = _fresh_paper()
    # a perfect call scores zero on both
    assert paper._brier3((1.0, 0.0, 0.0), 0) == 0.0
    assert paper._rps3((1.0, 0.0, 0.0), 0) == 0.0
    # a flat guess: Brier 2/3, RPS 1/9
    assert abs(paper._brier3((1 / 3, 1 / 3, 1 / 3), 1) - 0.6667) < 1e-3
    assert abs(paper._rps3((1 / 3, 1 / 3, 1 / 3), 1) - 0.1111) < 1e-3
    # RPS is ordinal: confidently calling A when the answer is a draw (adjacent) must cost LESS than
    # when the answer is B (two steps away). Brier cannot tell those apart on its own.
    p = (0.8, 0.15, 0.05)
    assert paper._rps3(p, 1) < paper._rps3(p, 2)
    os.unlink(config.DB_PATH)


# --- forward-only --------------------------------------------------------- #
def test_forward_only_refuses_a_past_game():
    paper = _fresh_paper()
    cands = [_cand("england", "senegal", "2026-07-04"),   # future -> logged
             _cand("brazil", "japan", "2026-06-20")]       # past   -> refused
    assert paper.log_forecasts(cands, today="2026-06-29") == 1
    # idempotent: a second pass logs nothing new
    assert paper.log_forecasts(cands, today="2026-06-29") == 0
    os.unlink(config.DB_PATH)


# --- lock-once + buffer --------------------------------------------------- #
def test_lock_only_in_window_and_is_immutable():
    paper = _fresh_paper()
    key = "fc|2026-07-04|england|senegal"
    paper.log_forecasts([_cand("england", "senegal", "2026-07-04")], today="2026-06-29")

    # before the window: nothing freezes, and list_forecasts hides the still-pending row
    assert paper.lock_forecasts(_board(key, lock_now=False), "2026-07-04T12:00:00Z") == 0
    assert paper.list_forecasts() == []

    # inside the window: it freezes exactly once
    assert paper.lock_forecasts(_board(key, lock_now=True), "2026-07-04T17:45:00Z") == 1
    assert paper.lock_forecasts(_board(key, lock_now=True), "2026-07-04T17:50:00Z") == 0

    r = paper.list_forecasts()[0]
    assert r["status"] == "locked" and r["model_a"] == 0.55 and r["market_a"] == 0.50
    assert r["model_cutoff"] == "2026-07-04"          # model trained through ~lock day, game excluded

    # a later board with DIFFERENT probabilities must NOT overwrite the frozen line
    drift = _board(key, lock_now=True, model=(0.9, 0.05, 0.05), market=(0.8, 0.1, 0.1))
    paper.lock_forecasts(drift, "2026-07-04T18:00:00Z")
    r = paper.list_forecasts()[0]
    assert r["model_a"] == 0.55 and r["market_a"] == 0.50
    os.unlink(config.DB_PATH)


def test_a_forecast_seen_only_after_kickoff_is_voided_not_graded():
    paper = _fresh_paper()
    key = "fc|2026-07-04|england|senegal"
    paper.log_forecasts([_cand("england", "senegal", "2026-07-04")], today="2026-06-29")
    # we never caught the lock window; the game has already kicked off -> void, never locked
    paper.lock_forecasts(_board(key, missed=True), "2026-07-04T19:30:00Z")
    assert paper.list_forecasts() == []                # void rows are not part of the record
    # and a result must not resurrect it
    results = [{"date": "2026-07-04", "goals": {"england": 2, "senegal": 0}, "winner": "england"}]
    assert paper.settle_forecasts(results) == 0
    os.unlink(config.DB_PATH)


# --- grading -------------------------------------------------------------- #
def test_level_knockout_grades_as_draw_with_pens_flag():
    paper = _fresh_paper()
    key = "fc|2026-07-04|england|senegal"
    paper.log_forecasts([_cand("england", "senegal", "2026-07-04")], today="2026-06-29")
    paper.lock_forecasts(_board(key, lock_now=True), "2026-07-04T17:45:00Z")
    # 1-1 after regulation/ET; Senegal advance on penalties. The 1X2 grades a DRAW, not a Senegal win.
    results = [{"date": "2026-07-04", "goals": {"england": 1, "senegal": 1}, "winner": "senegal"}]
    assert paper.settle_forecasts(results) == 1
    r = paper.list_forecasts()[0]
    assert r["status"] == "settled" and r["actual_outcome"] == "draw" and r["pens"] == 1
    # graded against the draw indicator, identically for model and market
    assert r["brier_model"] == paper._brier3((0.55, 0.27, 0.18), 1)
    assert r["brier_market"] == paper._brier3((0.50, 0.30, 0.20), 1)
    assert r["hit_model"] == 0                          # model's argmax was England, not the draw
    os.unlink(config.DB_PATH)


def test_decisive_result_grades_the_winner():
    paper = _fresh_paper()
    key = "fc|2026-07-04|england|senegal"
    paper.log_forecasts([_cand("england", "senegal", "2026-07-04")], today="2026-06-29")
    paper.lock_forecasts(_board(key, lock_now=True), "2026-07-04T17:45:00Z")
    results = [{"date": "2026-07-04", "goals": {"england": 2, "senegal": 0}, "winner": "england"}]
    paper.settle_forecasts(results)
    r = paper.list_forecasts()[0]
    assert r["actual_outcome"] == "a" and r["pens"] == 0 and r["hit_model"] == 1
    # settling twice is a no-op (status guard)
    assert paper.settle_forecasts(results) == 0
    os.unlink(config.DB_PATH)


# --- aggregate gating ----------------------------------------------------- #
# --- prediction sheet: the extra-market legs ------------------------------ #
def test_market_probs_consistent_with_match_probs():
    from backend.model import ratings
    m = ratings.MatchModel({"x": 1.3, "y": 0.9}, {"x": 0.95, "y": 1.1}, 1.35)
    mp = m.match_probs("x", "y")
    mk = m.market_probs("x", "y")
    # the 1X2 derived in market_probs must equal match_probs (same DC-corrected matrix)
    assert abs(mp["x"] - mk["home"]) < 1e-9
    assert abs(mp["draw"] - mk["draw"]) < 1e-9
    assert abs(mp["y"] - mk["away"]) < 1e-9
    for k in ("total_over", "a_over", "b_over", "btts"):
        assert 0.0 <= mk[k] <= 1.0
    # a higher-scoring matchup must have a higher P(over 2.5)
    hi = ratings.MatchModel({"x": 1.8, "y": 1.7}, {"x": 1.2, "y": 1.2}, 1.6)
    assert hi.market_probs("x", "y")["total_over"] > mk["total_over"]


def _legs():
    return [
        {"key": "total_goals", "side": "over", "line": 2.5, "team": None, "prob": 0.60, "proj": 2.8},
        {"key": "team_total", "side": "under", "line": 1.5, "team": "japan", "prob": 0.62, "proj": 0.9},
        {"key": "btts", "side": "no", "line": None, "team": None, "prob": 0.55, "proj": None},
        {"key": "corners", "side": "over", "line": 9.5, "team": None, "prob": 0.55, "proj": 10.0},
    ]


def test_legs_grade_against_the_result():
    paper = _fresh_paper()
    key = "fc|2026-07-04|brazil|japan"
    paper.log_forecasts([_cand("brazil", "japan", "2026-07-04")], today="2026-06-29")
    paper.lock_forecasts(_board(key, lock_now=True, legs=_legs()), "2026-07-04T17:45:00Z")
    # Brazil 3-0 Japan (3 total goals, Japan blanked), 8 corners
    results = [{"date": "2026-07-04", "goals": {"brazil": 3, "japan": 0}, "winner": "brazil"}]
    stats = {("2026-07-04", frozenset({"brazil", "japan"})): {"brazil": {"corners": 5}, "japan": {"corners": 3}}}
    paper.settle_forecasts(results, stats)
    legs = {l["key"] + (l.get("team") or ""): l for l in paper.list_forecasts()[0]["legs"]}
    assert legs["total_goals"]["result"] == "won"        # 3 > 2.5, picked over
    assert legs["team_totaljapan"]["result"] == "won"    # Japan 0 < 1.5, picked under
    assert legs["btts"]["result"] == "won"               # Japan didn't score -> No -> correct
    assert legs["corners"]["result"] == "lost"           # 8 < 9.5, picked over
    os.unlink(config.DB_PATH)


def test_corners_leg_fills_in_after_settlement():
    paper = _fresh_paper()
    key = "fc|2026-07-04|brazil|japan"
    paper.log_forecasts([_cand("brazil", "japan", "2026-07-04")], today="2026-06-29")
    paper.lock_forecasts(_board(key, lock_now=True, legs=_legs()), "2026-07-04T17:45:00Z")
    results = [{"date": "2026-07-04", "goals": {"brazil": 3, "japan": 0}, "winner": "brazil"}]
    # first pass: no corner data (e.g. keyless host) -> goals legs grade, corners stays pending
    assert paper.settle_forecasts(results, None) == 1
    corners = next(l for l in paper.list_forecasts()[0]["legs"] if l["key"] == "corners")
    assert corners["result"] == "pending"
    # later pass: API-Football posts the count -> corners fills, no game re-counted as newly settled
    stats = {("2026-07-04", frozenset({"brazil", "japan"})): {"brazil": {"corners": 7}, "japan": {"corners": 4}}}
    assert paper.settle_forecasts(results, stats) == 0
    corners = next(l for l in paper.list_forecasts()[0]["legs"] if l["key"] == "corners")
    assert corners["result"] == "won" and corners["actual"] == 11   # 11 > 9.5, picked over
    os.unlink(config.DB_PATH)


# --- UTC-midnight regression: late kickoffs whose market date lags a day ------------------ #
def test_late_kickoff_survives_utc_midnight():
    """A game with commence_time 07-02 but kickoff 03:00Z on 07-03 must stay pending past UTC midnight,
    lock inside its window, and never be voided pre-kickoff (the market date lags the kickoff by a day)."""
    from datetime import datetime, timezone
    from backend import aggregator
    from backend.models import Market, Selection, Quote

    paper = _fresh_paper()

    def mk():
        def sel(key, prob):
            s = Selection(key=key, label=key, quotes=[Quote(source="kalshi", source_type="prediction",
                          price_decimal=1 / prob, implied_prob=prob, mid_prob=prob)])
            s.fair_prob = prob
            return s
        return Market(market_id="m1", event="A vs B", market_type="moneyline",
                      selections=[sel("aaa", 0.45), sel("bbb", 0.28), sel("draw", 0.27)],
                      commence_time="2026-07-02")

    class M:
        def match_probs(self, a, b): return {a: 0.4, "draw": 0.27, b: 0.33}
        def market_probs(self, a, b, tl, teaml): return None

    kicks = {frozenset(("aaa", "bbb")): "2026-07-03T03:00Z"}
    cands, _ = aggregator._forecast_board([mk()], M(), kicks, 75,
                                          datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc))
    assert len(cands) == 1
    paper.log_forecasts(cands, "2026-07-02")

    # 00:30Z on 07-03: past UTC midnight, pre-window -> must NOT void, must NOT lock
    now = datetime(2026, 7, 3, 0, 30, tzinfo=timezone.utc)
    c2, b2 = aggregator._forecast_board([mk()], M(), kicks, 75, now)
    assert len(c2) == 1                              # still a live candidate
    paper.lock_forecasts(b2, now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert paper.list_forecasts() == []              # still pending (not locked, not void)

    # 02:00Z: inside the 01:45-03:00 window -> locks
    now = datetime(2026, 7, 3, 2, 0, tzinfo=timezone.utc)
    _, b3 = aggregator._forecast_board([mk()], M(), kicks, 75, now)
    assert paper.lock_forecasts(b3, now.strftime("%Y-%m-%dT%H:%M:%SZ")) == 1

    # kicked off: no NEW candidate is ever logged for a started game
    now = datetime(2026, 7, 3, 3, 30, tzinfo=timezone.utc)
    c4, b4 = aggregator._forecast_board([mk()], M(), kicks, 75, now)
    assert c4 == [] and b4["fc|2026-07-02|aaa|bbb"]["missed"] is True
    os.unlink(config.DB_PATH)


# --- performance-quality upgrade: ESPN box stats, possession, perf variant ---------------- #
def test_box_stats_extraction():
    from backend.sources import espn
    summary = {"boxscore": {"teams": [
        {"team": {"displayName": "Germany"}, "statistics": [
            {"name": "possessionPct", "displayValue": "75.3"},
            {"name": "wonCorners", "displayValue": "16"},
            {"name": "totalShots", "displayValue": "21"},
            {"name": "shotsOnTarget", "displayValue": "6"},
            {"name": "foulsCommitted", "displayValue": "18"}]},
        {"team": {"displayName": "Paraguay"}, "statistics": [
            {"name": "possessionPct", "displayValue": "24.7"},
            {"name": "wonCorners", "displayValue": "6"}]}]}}
    box = espn._box_stats(summary)
    assert box["germany"] == {"possession": 0.753, "corners": 16.0, "shots": 21.0, "sot": 6.0}
    assert box["paraguay"]["possession"] == 0.247 and box["paraguay"]["corners"] == 6.0


def test_corner_rates_from_espn_box():
    from backend.model import corners
    results = [
        {"box": {"spain": {"corners": 9}, "austria": {"corners": 3}}},
        {"box": {"spain": {"corners": 7}, "germany": {"corners": 5}}},
    ]
    rates = corners.rates_from_results(results)
    assert rates["spain"]["games"] == 2
    assert rates["spain"]["cf"] == 8.0          # (9 + 7) / 2 corners for
    assert rates["spain"]["ca"] == 4.0          # (3 + 5) / 2 conceded


def test_perf_mult_regresses_finishing_toward_volume():
    from backend import aggregator
    # x out-scored its shot volume (clinical) -> nudged DOWN; y under-scored its volume -> nudged UP
    results = [{"goals": {"x": 3, "y": 0}, "box": {"x": {"sot": 3.0}, "y": {"sot": 6.0}}}]
    pm = aggregator._perf_mult(results)
    assert pm["x"] < 1.0          # 3 goals on 3 SOT is way over the 0.359/SOT rate -> regress down
    assert pm["y"] > 1.0          # 0 goals on 6 SOT is way under -> regress up


def test_perf_variant_graded_alongside_goals_only():
    paper = _fresh_paper()
    key = "fc|2026-07-04|brazil|japan"
    paper.log_forecasts([_cand("brazil", "japan", "2026-07-04")], today="2026-06-29")
    legs = [{"key": "total_goals", "side": "over", "line": 2.5, "team": None, "prob": 0.55, "proj": 2.8,
             "perf_side": "under", "perf_prob": 0.56, "perf_proj": 2.3}]
    paper.lock_forecasts(_board(key, lock_now=True, legs=legs), "2026-07-04T17:45:00Z")
    # actual 2 goals: Under wins, Over loses -> goals-only lost, perf-aware won
    paper.settle_forecasts([{"date": "2026-07-04", "goals": {"brazil": 2, "japan": 0}, "winner": "brazil"}], None)
    leg = paper.list_forecasts()[0]["legs"][0]
    assert leg["result"] == "lost" and leg["perf_result"] == "won"
    os.unlink(config.DB_PATH)


def test_aggregate_scores_are_gated_until_min_n():
    paper = _fresh_paper()
    config.FORECAST_MIN_N = 3
    importlib.reload(paper)        # pick up the patched floor
    paper.init_paper()

    def settle_one(a, b, ga, gb):
        key = f"fc|2026-07-04|{a}|{b}"
        paper.log_forecasts([_cand(a, b, "2026-07-04")], today="2026-06-29")
        paper.lock_forecasts(_board(key, lock_now=True), "2026-07-04T17:45:00Z")
        paper.settle_forecasts([{"date": "2026-07-04", "goals": {a: ga, b: gb}, "winner": a if ga > gb else b}])

    settle_one("england", "senegal", 2, 0)
    settle_one("brazil", "japan", 1, 0)
    cal = paper.forecast_calibration()
    assert cal["n"] == 2 and cal["ready"] is False and "skill_vs_market" not in cal

    settle_one("spain", "austria", 3, 1)
    cal = paper.forecast_calibration()
    assert cal["n"] == 3 and cal["ready"] is True
    assert "skill_vs_market" in cal and "brier_model" in cal and 0 <= cal["hit_rate"] <= 100
    os.unlink(config.DB_PATH)
    config.FORECAST_MIN_N = 8
