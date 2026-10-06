"""My bets (logged by hand, graded off the same finals, scored against the close) and the value-now list."""
import importlib
import tempfile

import pytest

from backend import aggregator, config

DAL, TB = "dallas cowboys", "tampa bay buccaneers"
KEY = f"fc|2026-10-08|{DAL}|{TB}"


def _fresh(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "nfl")
    config.DB_PATH = tempfile.mktemp(suffix=".db")
    from backend.store import mybets, paper
    importlib.reload(paper)
    importlib.reload(mybets)
    paper.init_paper()
    mybets.init()
    return paper, mybets


def _legs():
    return [{"key": "total_goals", "side": "over", "line": 47.5, "team": None, "prob": 0.52, "research_prob": 0.495},
            {"key": "spread", "team": DAL, "opp": TB, "line": 7.5, "side": "cover", "prob": 0.53}]


def _game():
    return {"dedup": KEY, "team_a": DAL, "team_b": TB, "date": "2026-10-08", "kickoff_iso": "2026-10-09T00:15Z",
            "market": (0.8, 0.0, 0.2), "legs": _legs(),
            "research": {"ml": {DAL: 0.8, TB: 0.2}}}


def test_bets_are_validated_against_the_board(monkeypatch):
    _, mybets = _fresh(monkeypatch)
    games = {KEY: _game()}
    ok = {"legs": [{"dedup": KEY, "kind": "ml", "team": DAL}], "price": -400, "stake": 50, "book": "FanDuel"}
    for bad, why in ((dict(ok, price=-50), "American"), (dict(ok, stake=0), "stake"),
                     (dict(ok, legs=[]), "1 to"), (dict(ok, legs=[{"dedup": "nope", "kind": "ml", "team": DAL}]), "board"),
                     (dict(ok, legs=[{"dedup": KEY, "kind": "ml", "team": "x"}]), "teams"),
                     (dict(ok, legs=[{"dedup": KEY, "kind": "total", "dir": "sideways", "line": 47.5}]), "over or under")):
        with pytest.raises(ValueError, match=why):
            mybets.validate(bad, games)
    legs, price, stake, book = mybets.validate(ok, games)
    assert (price, stake, book, legs[0]["team"]) == (-400, 50.0, "FanDuel", DAL)


def test_leg_probabilities_read_the_research_and_the_board_lines(monkeypatch):
    _, mybets = _fresh(monkeypatch)
    g = _game()
    assert mybets.leg_prob(g, {"kind": "ml", "team": TB}) == 0.2
    assert mybets.leg_prob(g, {"kind": "total", "dir": "under", "line": 47.5}) == 0.505   # research moved it
    assert mybets.leg_prob(g, {"kind": "total", "dir": "under", "line": 47.5}, research=False) == 0.48
    assert mybets.leg_prob(g, {"kind": "spread", "team": DAL, "line": -7.5}) == 0.53
    assert mybets.leg_prob(g, {"kind": "spread", "team": TB, "line": 7.5}) == 0.47
    assert mybets.leg_prob(g, {"kind": "spread", "team": TB, "line": 6.5}) is None        # not the board's line


def test_a_logged_parlay_settles_off_the_final_and_scores_its_close(monkeypatch):
    paper, mybets = _fresh(monkeypatch)
    paper.log_forecasts([{"match": "Tampa Bay vs Dallas", "team_a": DAL, "team_b": TB, "commence_time": "2026-10-08",
                          "stage": None, "dedup_key": KEY}], today="2026-10-07")
    bet = mybets.log_bet({"legs": [{"dedup": KEY, "kind": "ml", "team": DAL},
                                   {"dedup": KEY, "kind": "total", "dir": "under", "line": 47.5}],
                          "price": 200, "stake": 10, "book": "DK"}, {KEY: _game()})
    assert bet["status"] == "open" and bet["p_log"] == round(0.8 * 0.505, 4)
    assert bet["ev_log"] == round(0.404 * 3.0 - 1, 4)
    board = {KEY: {"lock_now": True, "missed": False, "kickoff_iso": "2026-10-09T00:15Z", "model": None,
                   "market": (0.8, 0.0, 0.2), "sources": "kalshi", "legs": _legs()}}
    paper.lock_forecasts(board, "2026-10-08T23:00:00Z")
    paper.capture_forecast_close({KEY: {**board[KEY], "market": (0.82, 0.0, 0.18)}}, "2026-10-08T23:30:00Z")
    assert mybets.settle() == 0                              # locked, not played: still open, close known
    open_bet = mybets.list_bets()[0]
    assert open_bet["status"] == "open" and open_bet["p_close"] == round(0.82 * 0.48, 4)
    paper.settle_forecasts([{"date": "2026-10-08", "goals": {DAL: 24, TB: 17}, "iso": "2026-10-09T00:15Z"}])
    assert mybets.settle() == 1
    b = mybets.list_bets()[0]
    assert b["status"] == "won" and b["profit"] == 20.0      # 41 total points: under 47.5, Dallas won
    assert b["clv"] == round(0.82 * 0.48 * 3.0 - 1, 4)
    s = mybets.summary(mybets.list_bets())
    assert (s["won"], s["lost"], s["profit"], s["roi"], s["clv_n"]) == (1, 0, 20.0, 200.0, 1)
    assert mybets.delete(b["id"]) and mybets.list_bets() == []


def test_spreads_push_and_lose_like_a_book(monkeypatch):
    paper, mybets = _fresh(monkeypatch)
    row = {"status": "settled", "actual_a": 24, "actual_b": 17, "team_a": DAL, "team_b": TB}
    assert mybets._grade({"kind": "spread", "team": DAL, "line": -7.0}, row) == "push"
    assert mybets._grade({"kind": "spread", "team": DAL, "line": -7.5}, row) == "lost"
    assert mybets._grade({"kind": "spread", "team": TB, "line": 7.5}, row) == "won"
    assert mybets._grade({"kind": "ml", "team": TB}, row) == "lost"
    assert mybets._grade({"kind": "total", "dir": "over", "line": 41.0}, row) == "push"


def test_value_now_lists_only_prices_that_clear_the_cushion():
    game = {"dedup": KEY, "team_a": DAL, "team_b": TB, "kickoff_iso": "2026-10-09T00:15Z",
            "kalshi_ask": {DAL: 0.81, TB: 0.17},
            "research": {"ml": {DAL: 0.8, TB: 0.2}, "cushion": {"game": 0.02}, "value_at": {DAL: -377, TB: 291},
                         "dk": {"book": "DraftKings", "ml": {DAL: -350, TB: 330}, "ev": {DAL: 0.0286, TB: -0.14},
                                "total": {"line": 47.5, "over": -105, "under": -115, "ev_over": 0.01, "ev_under": 0.03}}}}
    rows = aggregator._value_now([game])
    got = {(r["venue"], r["kind"], r.get("team") or r.get("dir")) for r in rows}
    assert ("DraftKings", "ml", DAL) in got and ("DraftKings", "total", "under") in got
    assert ("DraftKings", "ml", TB) not in got and ("DraftKings", "total", "over") not in got
    # Kalshi's Tampa Bay ask of 17c plus its fee is under the 20% Research %: value on the exchange too
    k = next(r for r in rows if r["venue"] == "Kalshi")
    assert k["team"] == TB and k["ev"] > 0.02
    assert rows == sorted(rows, key=lambda r: -r["ev"])
