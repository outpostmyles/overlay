"""Weekend lotto parlays: only sensible legs reach the pool (favorites, no open questions, this weekend),
the research legs are marked, every running board feeds it, and a cross-sport ticket can be logged."""
import importlib
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from backend import config, lotto

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def _game(dedup, a, b, pa, ko="2026-10-11T17:00Z", uncertain=(), dk=None, top=None, legs=()):
    return {"dedup": dedup, "team_a": a, "team_b": b, "date": ko[:10], "kickoff_iso": ko,
            "market": (pa, 0.0, round(1 - pa, 4)), "legs": list(legs),
            "research": {"ml": {a: pa, b: round(1 - pa, 4)}, "uncertain": list(uncertain), "cushion": {"game": 0.02},
                         "top": top, "dk": dk}}


def _boards():
    return {
        "nfl": {"ts": time.time(), "names": {"kansas city chiefs": "Kansas City Chiefs"}, "games": [
            _game("g1", "kansas city chiefs", "las vegas raiders", 0.82, top="kansas city chiefs",
                  dk={"book": "DraftKings", "ml": {"kansas city chiefs": -400}, "ev": {"kansas city chiefs": 0.025}}),
            _game("g2", "buffalo bills", "new york jets", 0.55),                                   # coin flip: out
            _game("g3", "detroit lions", "chicago bears", 0.75, uncertain=["qb"]),                # open question
            _game("g4", "green bay packers", "minnesota vikings", 0.68, ko="2026-10-18T17:00Z"),  # next weekend
            _game("g5", "new england patriots", "miami dolphins", 0.64, ko="2026-10-09T11:00Z"),  # already started
            _game("g6", "seattle seahawks", "arizona cardinals", 0.30,                            # Arizona 70%
                  legs=[{"key": "total_goals", "side": "over", "line": 44.5, "prob": 0.52, "research_prob": 0.495}]),
        ]},
        "cfb": {"ts": time.time(), "names": {"texas a and m": "Texas A&M"}, "games": [
            _game("c1", "texas a and m", "arkansas", 0.91),
            _game("c2", "notre dame", "stanford", 0.97, uncertain=["no_report"])]},          # kept, labeled
    }


def test_the_pool_keeps_only_sensible_legs_and_marks_the_research():
    legs = lotto.candidates(_boards(), now=NOW)
    got = {(l["dedup"], l["kind"]): l for l in legs}
    assert set(got) == {("g1", "ml"), ("g6", "ml"), ("g6", "total"), ("c1", "ml"), ("c2", "ml")}
    assert [l["dedup"] for l in legs][:2] == ["c2", "c1"]              # most likely first
    assert got[("c2", "ml")]["flags"] == ["heavy", "no_report"]
    chiefs = got[("g1", "ml")]
    assert chiefs["label"] == "Kansas City Chiefs ML" and set(chiefs["flags"]) == {"heavy", "value", "top"}
    assert chiefs["dk"] == -400
    assert got[("g6", "ml")]["team"] == "arizona cardinals" and got[("g6", "ml")]["p"] == 0.7
    wind = got[("g6", "total")]
    assert (wind["dir"], wind["p"], wind["flags"], wind["label"]) == ("under", 0.505, ["research"], "Under 44.5")
    assert got[("c1", "ml")]["label"] == "Texas A&M ML" and got[("c1", "ml")]["sport"] == "cfb"


def test_the_window_always_covers_the_whole_coming_weekend():
    tue = datetime(2026, 10, 6, 9, tzinfo=timezone.utc)                   # Tuesday morning
    assert lotto.weekend_end(tue) == datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
    sat = datetime(2026, 10, 10, 18, tzinfo=timezone.utc)
    assert lotto.weekend_end(sat) == datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
    mon = datetime(2026, 10, 12, 20, tzinfo=timezone.utc)                  # Monday night: next weekend
    assert lotto.weekend_end(mon) == datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


def test_boards_share_their_games_through_fresh_files(monkeypatch):
    from backend.store import livelegs
    root = Path(tempfile.mkdtemp())
    monkeypatch.setattr(config, "ROOT", root)
    live = [{"sport": "nfl", "dedup": "g1", "team_a": "a", "team_b": "b", "date": "2026-10-11",
             "kickoff_iso": "2026-10-11T17:00Z", "market": (0.8, 0.0, 0.2),
             "legs": [{"key": "spread", "team": "a", "line": 7.5, "side": "cover", "prob": 0.5},
                      {"key": "player_prop", "stat": "receptions", "prob": 0.5}],
             "research": {"ml": {"a": 0.8, "b": 0.2}, "uncertain": [], "cushion": {"game": 0.02},
                          "factors": [{"key": "top_traders", "target": {"kind": "ml", "team": "a"}}], "dk": None}}]
    livelegs.write("nfl", live, {"a": "Team A"})
    boards = livelegs.read_all()
    g = boards["nfl"]["games"][0]
    assert boards["nfl"]["names"] == {"a": "Team A"} and g["research"]["top"] == "a"
    assert [leg["key"] for leg in g["legs"]] == ["spread"]                # props stay home
    stale = json.loads((root / "poly_live_nfl.json").read_text())
    stale["ts"] = time.time() - 3 * 3600
    (root / "poly_live_cfb.json").write_text(json.dumps({**stale, "sport": "cfb"}))
    assert set(livelegs.read_all()) == {"nfl"}                            # a board that stopped publishing drops out
    assert set(livelegs.games_by_dedup(livelegs.read_all())) == {"g1"}


def test_a_cross_sport_ticket_logs_as_multi_and_shows_on_every_board(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "nfl")
    config.DB_PATH = tempfile.mktemp(suffix=".db")
    from backend.store import mybets, paper
    importlib.reload(paper)
    importlib.reload(mybets)
    paper.init_paper()
    mybets.init()
    games = {g["dedup"]: {**g, "sport": s} for s, b in _boards().items() for g in b["games"]}
    bet = mybets.log_bet({"legs": [{"dedup": "g1", "kind": "ml", "team": "kansas city chiefs"},
                                   {"dedup": "c1", "kind": "ml", "team": "texas a and m"}],
                          "price": 150, "stake": 2, "book": "FanDuel"}, games)
    assert bet["sport"] == "multi" and {l["sport"] for l in bet["legs"]} == {"nfl", "cfb"}
    monkeypatch.setattr(config, "SPORT", "cfb")
    assert [b["id"] for b in mybets.list_bets()] == [bet["id"]]           # the college board sees it too
    single = mybets.log_bet({"legs": [{"dedup": "g1", "kind": "ml", "team": "kansas city chiefs"}],
                             "price": -400, "stake": 3}, games)
    assert single["sport"] == "nfl"
    assert [b["id"] for b in mybets.list_bets()] == [bet["id"]]           # an NFL-only bet stays on the NFL board
