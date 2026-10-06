"""Settlement correctness: a forecast grades only against its own game, voids when ESPN calls that game
off, and never locks or misses off a placeholder start.

The doubleheader cases are the real ones from the 2026 MLB ledger. Nine nightcaps settled on game 1's
final because, while the nightcap was still being played, game 1 was the pair's only same-date final
and the old fallback took it. One nightcap had also moved after its lock (23:05Z to 23:30Z), so an
exact start-time match could not have found it either."""
import asyncio
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from backend import aggregator, config
from backend.models import Market, Quote, Selection
from backend.sources import espn
from backend.store import paper

LAD, NYY, BAL = "los angeles dodgers", "new york yankees", "baltimore orioles"


@pytest.fixture
def mlb(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SPORT", "mlb")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "ledger.db")
    paper.init_paper()
    return paper


def _lock(a, b, day, disc, kickoff, market=(0.55, 0.0, 0.45), legs=None):
    """Log and lock one MLB forecast row; returns its dedup key."""
    key = f"fc|{day}|{a}|{b}|{disc}"
    paper.log_forecasts([{"match": f"{a} vs {b}", "team_a": a, "team_b": b, "commence_time": day,
                          "stage": None, "dedup_key": key}], today=day)
    assert paper.lock_forecasts({key: {"lock_now": True, "missed": False, "kickoff_iso": kickoff,
                                       "model": None, "market": market, "sources": "kalshi",
                                       "legs": legs or []}}, f"{day}T12:00:00Z") == 1
    return key


def _row(key):
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    return c.execute("SELECT * FROM forecasts WHERE dedup_key=?", (key,)).fetchone()


def _final(day, start, goals, innings=None):
    winner = max(goals, key=goals.get)
    return {"date": day, "goals": goals, "winner": winner, "iso": start, "players": {},
            "innings": innings or {}}


# 2026-07-19: Dodgers 8-2 in game 1 (16:35Z), Yankees 2-1 in the 23:20Z nightcap
G1_0719 = _final("2026-07-19", "2026-07-19T16:35Z", {LAD: 8, NYY: 2},
                 {LAD: [3, 0, 2, 0, 1, 0, 2, 0, 0], NYY: [0, 1, 0, 0, 0, 1, 0, 0, 0]})
G2_0719 = _final("2026-07-19", "2026-07-19T23:20Z", {LAD: 1, NYY: 2},
                 {LAD: [0, 0, 0, 0, 0, 1, 0, 0, 0], NYY: [0, 0, 1, 1, 0, 0, 0, 0, 0]})
NIGHTCAP_LEGS = [{"key": "total_goals", "side": "under", "line": 9.5, "team": None, "prob": 0.52},
                 {"key": "f5", "side": LAD, "line": None, "team": None, "prob": 0.45}]


def test_nightcap_waits_while_game_one_is_the_only_final(mlb):
    key = _lock(LAD, NYY, "2026-07-19", "19:20", "2026-07-19T23:20Z", legs=NIGHTCAP_LEGS)
    # game 1 is final and the nightcap is still being played: game 1 started 6h45m before this row's
    # kickoff, so it is a different game, however alone it is on the date
    assert paper.settle_forecasts([G1_0719]) == 0
    assert _row(key)["status"] == "locked"
    # the nightcap's own final lands: it grades on that, not on game 1
    assert paper.settle_forecasts([G1_0719, G2_0719]) == 1
    r = _row(key)
    assert (r["status"], r["actual_a"], r["actual_b"], r["actual_outcome"]) == ("settled", 1, 2, "b")
    assert r["brier_market"] == paper._brier3((0.55, 0.0, 0.45), 2)
    legs = {leg["key"]: leg for leg in json.loads(r["legs_json"])}
    assert (legs["total_goals"]["actual"], legs["total_goals"]["result"]) == (3, "won")
    assert (legs["f5"]["actual"], legs["f5"]["result"]) == (NYY, "lost")      # 0-2 through five


def test_game_one_row_takes_game_one_even_with_both_finals_in(mlb):
    early = _lock(LAD, NYY, "2026-07-19", "12:35", "2026-07-19T16:35Z")
    late = _lock(LAD, NYY, "2026-07-19", "19:20", "2026-07-19T23:20Z")
    assert paper.settle_forecasts([G2_0719, G1_0719]) == 2
    assert (_row(early)["actual_a"], _row(early)["actual_b"]) == (8, 2)
    assert (_row(late)["actual_a"], _row(late)["actual_b"]) == (1, 2)


def test_start_nudged_after_the_lock_still_finds_its_game(mlb):
    """2026-09-25 BAL v NYY: locked for 23:05Z, ESPN later listed the nightcap at 23:30Z. Game 1 at 20:05Z
    is exactly three hours from the frozen kickoff, which is already too far to be the same game."""
    key = _lock(BAL, NYY, "2026-09-25", "19:05", "2026-09-25T23:05Z", market=(0.48, 0.0, 0.52))
    g1 = _final("2026-09-25", "2026-09-25T20:05Z", {BAL: 10, NYY: 2})
    g2 = _final("2026-09-25", "2026-09-25T23:30Z", {BAL: 3, NYY: 6})
    assert paper.settle_forecasts([g1]) == 0
    assert paper.settle_forecasts([g1, g2]) == 1
    r = _row(key)
    assert (r["actual_a"], r["actual_b"], r["actual_outcome"]) == (3, 6, "b")


def test_series_game_a_day_away_is_never_the_rows_game(mlb):
    key = _lock(LAD, NYY, "2026-07-19", "19:20", "2026-07-19T23:20Z")
    yesterday = _final("2026-07-18", "2026-07-18T23:20Z", {LAD: 5, NYY: 0})
    assert paper.settle_forecasts([yesterday]) == 0
    assert _row(key)["status"] == "locked"


def test_settle_writes_exactly_what_grade_forecast_returns(mlb):
    """The one-off regrade reuses grade_forecast, so it must be the whole of what settle writes."""
    key = _lock(LAD, NYY, "2026-07-19", "19:20", "2026-07-19T23:20Z", legs=NIGHTCAP_LEGS)
    expected = paper.grade_forecast(_row(key), G2_0719)
    paper.settle_forecasts([G1_0719, G2_0719])
    r = _row(key)
    assert r["status"] == "settled"
    assert {k: r[k] for k in expected} == expected


def test_settled_rows_regrade_legs_against_their_own_game(mlb):
    """A settled row's legs re-grade on every pass (a late corners count, a fixed box score). That pass
    must pick the same game the 1X2 did, never the doubleheader sibling."""
    key = _lock(LAD, NYY, "2026-07-19", "19:20", "2026-07-19T23:20Z", legs=NIGHTCAP_LEGS)
    paper.settle_forecasts([G1_0719, G2_0719])
    before = _row(key)["legs_json"]
    assert paper.settle_forecasts([G1_0719, G2_0719]) == 0
    assert _row(key)["legs_json"] == before
    assert paper.settle_forecasts([G1_0719]) == 0                     # game 1 alone never re-grades it
    assert _row(key)["legs_json"] == before


def test_a_listed_unfinished_nightcap_holds_its_row_even_when_game_one_moved_closer(mlb):
    """A traditional doubleheader: the nightcap locked for 20:05Z, and ESPN rewrote game 1's start from
    17:05Z to 17:40Z, inside three hours of it. While the scoreboard still lists the nightcap unfinished,
    the row's nearest game is that one, so it waits instead of taking game 1's final."""
    key = _lock(LAD, NYY, "2026-07-19", "16:05", "2026-07-19T20:05Z")
    g1 = _final("2026-07-19", "2026-07-19T17:40Z", {LAD: 8, NYY: 2})
    listed = {(frozenset((LAD, NYY)), "2026-07-19"): {"2026-07-19T17:40Z": True, "2026-07-19T20:05Z": False}}
    assert paper.settle_forecasts([g1], listed=listed) == 0
    assert _row(key)["status"] == "locked"
    g2 = _final("2026-07-19", "2026-07-19T20:05Z", {LAD: 1, NYY: 2})
    listed[(frozenset((LAD, NYY)), "2026-07-19")]["2026-07-19T20:05Z"] = True
    assert paper.settle_forecasts([g1, g2], listed=listed) == 1
    assert (_row(key)["actual_a"], _row(key)["actual_b"]) == (1, 2)


def test_a_lone_game_delayed_for_hours_still_grades(mlb):
    """One game that day, rain-delayed so its listed start moved 3h05m past the frozen kickoff: it is
    still the pair's only game, so its final grades the row."""
    key = _lock(LAD, NYY, "2026-07-19", "19:05", "2026-07-19T23:05Z")
    late = _final("2026-07-19", "2026-07-20T02:10Z", {LAD: 4, NYY: 3})
    listed = {(frozenset((LAD, NYY)), "2026-07-19"): {"2026-07-20T02:10Z": True}}
    assert paper.settle_forecasts([late]) == 0                         # without the scoreboard, it waits
    assert paper.settle_forecasts([late], listed=listed) == 1
    assert _row(key)["actual_a"] == 4


# --- canceled / postponed ------------------------------------------------------------------------- #
def test_canceled_game_voids_its_locked_row_at_once(mlb):
    """2026-09-27 BAL v NYY: ESPN STATUS_CANCELED, and the row sat locked for 9 days."""
    key = _lock(BAL, NYY, "2026-09-27", "15:20", "2026-09-27T17:05Z")
    off = {(frozenset((BAL, NYY)), "2026-09-27"): {"2026-09-27T17:05Z": "STATUS_CANCELED"}}
    assert paper.settle_forecasts([], None, called_off=off) == 0
    assert _row(key)["status"] == "void"


def test_postponed_game_one_never_voids_the_nightcap(mlb):
    first = _lock(BAL, NYY, "2026-09-25", "13:05", "2026-09-25T17:05Z")
    second = _lock(BAL, NYY, "2026-09-25", "19:05", "2026-09-25T23:05Z")
    off = {(frozenset((BAL, NYY)), "2026-09-25"): {"2026-09-25T17:05Z": "STATUS_POSTPONED"}}
    g2 = _final("2026-09-25", "2026-09-25T23:05Z", {BAL: 3, NYY: 6})
    assert paper.settle_forecasts([], None, called_off=off) == 0
    assert _row(first)["status"] == "void"
    assert _row(second)["status"] == "locked"          # its own game is still on
    assert paper.settle_forecasts([g2], None, called_off=off) == 1
    assert _row(second)["actual_outcome"] == "b"


def test_called_off_never_rewrites_a_settled_row(mlb):
    key = _lock(LAD, NYY, "2026-07-19", "19:20", "2026-07-19T23:20Z")
    paper.settle_forecasts([G2_0719])
    off = {(frozenset((LAD, NYY)), "2026-07-19"): {"2026-07-19T23:20Z": "STATUS_POSTPONED"}}
    paper.settle_forecasts([], None, called_off=off)
    assert _row(key)["status"] == "settled"


class _Resp:
    def __init__(self, data):
        self.status_code, self._data = 200, data

    def json(self):
        return self._data


class _Client:
    """Serves one canned scoreboard per date; anything else (a summary) is an empty body."""
    def __init__(self, boards):
        self.boards = boards

    async def get(self, url, params=None, timeout=None):
        if url.endswith("/scoreboard"):
            return _Resp(self.boards.get(params["dates"], {"events": []}))
        return _Resp({})


def _ev(eid, start, home, away, status="STATUS_SCHEDULED", time_valid=True, field="displayName"):
    done = status == "STATUS_FINAL"
    return {"id": eid, "date": start, "status": {"type": {"name": status, "completed": done}},
            "competitions": [{"timeValid": time_valid, "competitors": [
                {"homeAway": "home", "team": {field: home}, "score": "0"},
                {"homeAway": "away", "team": {field: away}, "score": "0"}]}]}


def test_results_sweep_records_called_off_games_per_date(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SPORT", "mlb")
    monkeypatch.setattr(config, "ESPN_CACHE_PATH", tmp_path / "poly_espn_cache.json")
    monkeypatch.setattr(espn, "CALLED_OFF", {})
    pair = frozenset((BAL, NYY))
    canceled = {"events": [_ev("1", "2026-09-27T17:05Z", "Baltimore Orioles", "New York Yankees",
                               "STATUS_CANCELED"),
                           _ev("2", "2026-09-27T17:10Z", "Boston Red Sox", "Tampa Bay Rays",
                               "STATUS_SUSPENDED")]}
    assert asyncio.run(espn.fetch_results(_Client({"20260927": canceled}), ["20260927"])) == []
    assert espn.CALLED_OFF == {(pair, "2026-09-27"): {"2026-09-27T17:05Z": "STATUS_CANCELED"}}
    # the next sweep rebuilds the date from what ESPN says now (the game was reinstated)
    back_on = {"events": [_ev("1", "2026-09-27T17:05Z", "Baltimore Orioles", "New York Yankees")]}
    asyncio.run(espn.fetch_results(_Client({"20260927": back_on}), ["20260927"]))
    assert espn.CALLED_OFF == {}


# --- date-only placeholder kickoffs (timeValid false) --------------------------------------------- #
def test_kickoffs_mark_a_placeholder_start(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "cfb")
    board = {"events": [
        _ev("401858494", "2026-10-17T04:00Z", "UCLA", "Wisconsin", time_valid=False, field="location"),
        _ev("401858000", "2026-10-17T16:00Z", "Florida", "Texas", field="location")]}
    kicks = asyncio.run(espn.fetch_kickoffs(_Client({"20261017": board}), ["20261017"]))
    tbd, real = frozenset(("ucla", "wisconsin")), frozenset(("florida", "texas"))
    assert kicks[(tbd, "2026-10-17")] == "2026-10-17T04:00Z"   # still there, for ordering
    assert kicks[espn.tbd_key(tbd, "2026-10-17")] is True
    assert espn.tbd_key(real, "2026-10-17") not in kicks


def _cfb_ml(a, b, date):
    def sel(k, p):
        return Selection(key=k, label=k, fair_prob=p,
                         quotes=[Quote(source="kalshi", source_type="prediction_market", price_decimal=1 / p,
                                       implied_prob=p, mid_prob=p, bid=round(p - 0.01, 3),
                                       ask=round(p + 0.01, 3))])
    return Market(market_id=f"kalshi:{a}{b}", event=f"{a} vs {b}", market_type="moneyline",
                  selections=[sel(a, 0.6), sel(b, 0.4)], commence_time=date)


@pytest.mark.parametrize("now", [datetime(2026, 10, 17, 3, 0, tzinfo=timezone.utc),    # "lock window"
                                 datetime(2026, 10, 17, 5, 0, tzinfo=timezone.utc)])   # "kicked off"
def test_placeholder_start_never_locks_or_misses(monkeypatch, now):
    monkeypatch.setattr(config, "SPORT", "cfb")
    pair = frozenset(("ucla", "wisconsin"))
    kicks = {(pair, "2026-10-17"): "2026-10-17T04:00Z", espn.tbd_key(pair, "2026-10-17"): True}
    cands, board = aggregator._forecast_board([_cfb_ml("ucla", "wisconsin", "2026-10-17")], None,
                                              kicks, 75, now)
    (entry,) = board.values()
    assert entry["lock_now"] is False and entry["missed"] is False
    assert entry["time_tbd"] is True and entry["kickoff_iso"] == "2026-10-17T04:00Z"
    assert len(cands) == 1                                   # still on the sheet, waiting for a real time


def test_confirmed_start_locks_as_before(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "cfb")
    pair = frozenset(("ucla", "wisconsin"))
    kicks = {(pair, "2026-10-17"): "2026-10-17T04:00Z"}       # the same time, but ESPN vouches for it
    now = datetime(2026, 10, 17, 3, 0, tzinfo=timezone.utc)
    (entry,) = aggregator._forecast_board([_cfb_ml("ucla", "wisconsin", "2026-10-17")], None,
                                          kicks, 75, now)[1].values()
    assert entry["lock_now"] is True and entry["time_tbd"] is False


def test_slip_game_carries_time_tbd():
    g = {"dedup": "fc|2026-10-17|ucla|wisconsin", "team_a": "ucla", "team_b": "wisconsin",
         "kickoff_iso": "2026-10-17T04:00Z", "time_tbd": True, "market": (0.6, 0.0, 0.4), "legs": []}
    assert aggregator._slip_game(g)["time_tbd"] is True
    assert aggregator._slip_game({**g, "time_tbd": False})["time_tbd"] is False
