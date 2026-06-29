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
           model=(0.55, 0.27, 0.18), market=(0.50, 0.30, 0.20)):
    return {key: {"lock_now": lock_now, "missed": missed, "kickoff_iso": ko,
                  "model": model, "market": market, "sources": "kalshi"}}


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
