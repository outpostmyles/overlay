"""MLB adapter (Phase 1): 2-way market-only ledger, doubleheader/series safety, aliases, voids."""
import asyncio
import importlib
import os
import tempfile

import pytest

from backend import config, sports
from backend.matching import kalshi_ticker_date, moneyline_key, normalize_team


def _fresh_paper():
    config.DB_PATH = tempfile.mktemp(suffix=".db")
    from backend.store import paper
    importlib.reload(paper)
    paper.init_paper()
    return paper


def test_mlb_adapter_registered():
    a = sports.get("mlb")
    assert a.outcomes == ("a", "b")
    assert a.espn_path == "baseball/mlb"
    assert a.kalshi_series["KXMLBGAME"] == ("moneyline", "Matches")
    assert a.kalshi_series["KXMLBTOTAL"] == ("total", "Lines")
    assert a.kalshi_series["KXMLBHIT"] == ("player_prop", "hits")
    assert a.kalshi_series["KXMLBOUTS"] == ("player_prop", "outs recorded")
    assert a.capabilities == frozenset()          # anchor-only by design
    assert a.pair_only_key is False
    assert a.results_window_days == 10


def test_mlb_aliases_line_up_kalshi_and_espn(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "mlb")
    # Kalshi city-style labels land on the normalized ESPN displayName
    assert normalize_team("Los Angeles D") == "los angeles dodgers"
    assert normalize_team("Chicago WS") == "chicago white sox"
    assert normalize_team("A's") == "athletics"
    assert normalize_team("St. Louis") == "st louis cardinals"
    # ESPN displayNames pass through to the same keys
    assert normalize_team("Los Angeles Dodgers") == "los angeles dodgers"
    assert normalize_team("New York Yankees") == "new york yankees"


def test_wc26_normalization_unchanged_under_mlb_registry(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "wc26")
    assert normalize_team("USA") == "united states"    # global soccer aliases still apply
    assert normalize_team("Reg Time: Germany") == "germany"


def test_kalshi_ticker_date_with_embedded_time():
    assert kalshi_ticker_date("KXWCGAME-26JUN27CODUZB") == "2026-06-27"
    assert kalshi_ticker_date("KXMLBGAME-26JUL121610TORSD") == "2026-07-12T16:10"


def test_moneyline_key_time_aware_for_mlb(monkeypatch):
    teams = ["toronto blue jays", "san diego padres"]
    monkeypatch.setattr(config, "SPORT", "wc26")
    assert moneyline_key("2026-07-12T16:10", teams) == moneyline_key("2026-07-13T16:10", teams)
    monkeypatch.setattr(config, "SPORT", "mlb")
    k1 = moneyline_key("2026-07-12T13:05", teams)      # doubleheader game 1
    k2 = moneyline_key("2026-07-12T19:05", teams)      # doubleheader game 2
    k3 = moneyline_key("2026-07-13T13:05", teams)      # next game of the series
    assert len({k1, k2, k3}) == 3


def test_market_only_forecast_lifecycle(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "mlb")
    paper = _fresh_paper()
    key = "fc|2026-07-12|athletics|chicago white sox|14:10"
    paper.log_forecasts([{"match": "A's vs White Sox", "team_a": "athletics",
                          "team_b": "chicago white sox", "commence_time": "2026-07-12",
                          "stage": None, "dedup_key": key}], today="2026-07-12")
    board = {key: {"lock_now": True, "missed": False, "kickoff_iso": "2026-07-12T18:10Z",
                   "model": None, "market": (0.55, 0.0, 0.45), "sources": "kalshi", "legs": []}}
    assert paper.lock_forecasts(board, "2026-07-12T16:55:00Z") == 1
    r = paper.list_forecasts()[0]
    assert r["model_a"] is None and r["market_a"] == 0.55 and r["sport"] == "mlb"
    # settle from a 2-way final: market graded, model columns stay NULL
    results = [{"date": "2026-07-12", "goals": {"athletics": 5, "chicago white sox": 2},
                "winner": "athletics", "iso": "2026-07-12T18:10Z"}]
    assert paper.settle_forecasts(results, None) == 1
    r = paper.list_forecasts()[0]
    assert r["actual_outcome"] == "a" and r["brier_market"] is not None
    assert r["brier_model"] is None and r["hit_model"] is None
    # market-only calibration appears once market_n reaches the gate
    cal = paper.forecast_calibration()
    assert cal["n"] == 0 and cal["market_n"] == 1
    os.unlink(config.DB_PATH)


def test_series_settle_disambiguates_by_date(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "mlb")
    paper = _fresh_paper()
    key = "fc|2026-07-12|colorado rockies|san francisco giants|16:05"
    paper.log_forecasts([{"match": "COL vs SF", "team_a": "colorado rockies",
                          "team_b": "san francisco giants", "commence_time": "2026-07-12",
                          "stage": None, "dedup_key": key}], today="2026-07-12")
    paper.lock_forecasts({key: {"lock_now": True, "missed": False, "kickoff_iso": "2026-07-12T20:05Z",
                                "model": None, "market": (0.4, 0.0, 0.6), "sources": "kalshi",
                                "legs": []}}, "2026-07-12T18:50:00Z")
    # two finals for the SAME pair in the window (a series): only the same-date game may grade this row
    results = [
        {"date": "2026-07-11", "goals": {"colorado rockies": 7, "san francisco giants": 1},
         "winner": "colorado rockies", "iso": "2026-07-11T20:05Z"},
        {"date": "2026-07-12", "goals": {"colorado rockies": 2, "san francisco giants": 9},
         "winner": "san francisco giants", "iso": "2026-07-12T20:05Z"},
    ]
    assert paper.settle_forecasts(results, None) == 1
    r = paper.list_forecasts()[0]
    assert r["actual_outcome"] == "b"                  # the Jul 12 result, not the Jul 11 blowout
    os.unlink(config.DB_PATH)


def test_kalshi_resolved_voids_a_rainout(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "mlb")
    from backend.sources import kalshi

    async def fake_series(client, series, status="open"):
        if status != "settled":
            return []
        return [
            # normal game: one yes, one no
            {"ticker": "KXMLBGAME-26JUL101305NYYWSH-NYY", "event_ticker": "KXMLBGAME-26JUL101305NYYWSH",
             "yes_sub_title": "New York Y", "result": "yes", "title": "New York vs Washington Winner?"},
            {"ticker": "KXMLBGAME-26JUL101305NYYWSH-WSH", "event_ticker": "KXMLBGAME-26JUL101305NYYWSH",
             "yes_sub_title": "Washington", "result": "no", "title": "New York vs Washington Winner?"},
            # rainout: BOTH legs settle no -> void, never lost
            {"ticker": "KXMLBGAME-26JUL101910BOSTB-BOS", "event_ticker": "KXMLBGAME-26JUL101910BOSTB",
             "yes_sub_title": "Boston", "result": "no", "title": "Boston vs Tampa Bay Winner?"},
            {"ticker": "KXMLBGAME-26JUL101910BOSTB-TB", "event_ticker": "KXMLBGAME-26JUL101910BOSTB",
             "yes_sub_title": "Tampa Bay", "result": "no", "title": "Boston vs Tampa Bay Winner?"},
        ]

    monkeypatch.setattr(kalshi, "_fetch_series", fake_series)
    rows = asyncio.run(kalshi.fetch_resolved(None))
    by = {(r["team_key"], r["result"]) for r in rows}
    assert ("new york yankees", "won") in by
    assert ("washington nationals", "lost") in by
    assert ("boston red sox", "void") in by and ("tampa bay rays", "void") in by
    assert all(r["date"] == "2026-07-10" for r in rows)   # date-only, time stripped for settlement


def test_settle_from_resolved_voids_the_pick(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "mlb")
    paper = _fresh_paper()
    paper.log_picks([{"match": "Boston vs Tampa Bay", "archetype": "favorite_ml",
                      "selection": "Boston Red Sox ML", "commence_time": "2026-07-10T19:10",
                      "pick_fair_prob": 0.6, "pick_price_decimal": 1.6, "dedup_key": "x1"}])
    n = paper.settle_from_resolved([{"date": "2026-07-10", "team_key": "boston red sox",
                                     "result": "void"}])
    assert n == 1
    assert paper.list_picks()[0]["status"] == "void"
    os.unlink(config.DB_PATH)


def test_dateonly_odds_twin_cannot_fabricate_a_doubleheader(monkeypatch):
    """A date-only Odds API market for the SAME game must not DH-flag the slate (review finding)."""
    monkeypatch.setattr(config, "SPORT", "mlb")
    from datetime import datetime, timezone
    from backend import aggregator
    from backend.models import Market, Quote, Selection

    def mk(commence, source="kalshi"):
        def sel(key, prob):
            s = Selection(key=key, label=key, quotes=[Quote(source=source, source_type="x",
                          price_decimal=1 / prob, implied_prob=prob, mid_prob=prob)])
            s.fair_prob = prob
            return s
        return Market(market_id=f"m-{source}", event="TOR vs SD", market_type="moneyline",
                      selections=[sel("toronto blue jays", 0.52), sel("san diego padres", 0.48)],
                      commence_time=commence)

    kicks = {(frozenset(("toronto blue jays", "san diego padres")), "2026-07-12"): "2026-07-12T20:10Z"}
    now = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    markets = [mk("2026-07-12T16:10"), mk("2026-07-12", source="draftkings")]   # kalshi + date-only twin
    cands, board = aggregator._forecast_board(markets, None, kicks, 75, now)
    row = board["fc|2026-07-12|san diego padres|toronto blue jays|16:10"]
    assert row["kickoff_iso"] == "2026-07-12T20:10Z"     # NOT doubleheader-suppressed; can lock


def test_locked_rainout_never_settles_against_a_makeup_game(monkeypatch):
    """A postponed-after-lock MLB game must not grade against the pair's next final (review finding)."""
    monkeypatch.setattr(config, "SPORT", "mlb")
    paper = _fresh_paper()
    key = "fc|2026-07-12|boston red sox|tampa bay rays|19:10"
    paper.log_forecasts([{"match": "BOS vs TB", "team_a": "boston red sox",
                          "team_b": "tampa bay rays", "commence_time": "2026-07-12",
                          "stage": None, "dedup_key": key}], today="2026-07-12")
    paper.lock_forecasts({key: {"lock_now": True, "missed": False, "kickoff_iso": "2026-07-12T23:10Z",
                                "model": None, "market": (0.5, 0.0, 0.5), "sources": "kalshi",
                                "legs": []}}, "2026-07-12T21:55:00Z")
    # the game rains out; the pair's NEXT meeting completes two days later
    makeup = [{"date": "2026-07-14", "goals": {"boston red sox": 4, "tampa bay rays": 1},
               "winner": "boston red sox", "iso": "2026-07-14T23:10Z"}]
    assert paper.settle_forecasts(makeup, None) == 0      # stays locked, never mis-graded
    # once its date falls outside the results window, the locked row voids
    paper.lock_forecasts({}, "2026-07-25T12:00:00Z")      # window=10: 07-12 < 07-25 minus 9
    import sqlite3
    c = sqlite3.connect(config.DB_PATH); c.row_factory = sqlite3.Row
    assert c.execute("SELECT status FROM forecasts").fetchone()["status"] == "void"
    os.unlink(config.DB_PATH)


def test_track_record_reads_are_sport_scoped(monkeypatch):
    paper = _fresh_paper()
    monkeypatch.setattr(config, "SPORT", "wc26")
    paper.log_picks([{"match": "France vs Spain", "archetype": "favorite_ml",
                      "selection": "France ML", "dedup_key": "wc-1"}])
    monkeypatch.setattr(config, "SPORT", "mlb")
    paper.log_picks([{"match": "NYY vs BOS", "archetype": "favorite_ml",
                      "selection": "New York Yankees ML", "dedup_key": "mlb-1"}])
    assert [p["dedup_key"] for p in paper.list_picks()] == ["mlb-1"]
    monkeypatch.setattr(config, "SPORT", "wc26")
    assert [p["dedup_key"] for p in paper.list_picks()] == ["wc-1"]
    os.unlink(config.DB_PATH)


def test_espn_player_lines_extraction():
    from backend.sources import espn
    summary = {"boxscore": {"players": [
        {"statistics": [
            {"type": "batting", "keys": ["hits-atBats", "atBats", "runs", "hits", "RBIs", "homeRuns"],
             "athletes": [{"athlete": {"displayName": "Trea Turner"},
                           "stats": ["2-4", "4", "1", "2", "0", "1"]}]},
            {"type": "pitching", "keys": ["fullInnings.partInnings", "hits", "runs"],
             "athletes": [{"athlete": {"displayName": "Aaron Nola"},
                           "stats": ["5.2", "3", "2"]}]},
        ]}]}}
    pl = espn._player_lines(summary)
    assert pl["trea turner"] == {"hits": 2, "home runs": 1}
    assert pl["aaron nola"]["outs recorded"] == 17          # 5.2 innings = 17 outs


def test_prop_legs_grade_from_player_lines(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "mlb")
    paper = _fresh_paper()
    key = "fc|2026-07-12|athletics|chicago white sox|14:10"
    paper.log_forecasts([{"match": "x", "team_a": "athletics", "team_b": "chicago white sox",
                          "commence_time": "2026-07-12", "stage": None, "dedup_key": key}],
                        today="2026-07-12")
    legs = [
        {"key": "player_prop", "stat": "outs recorded", "player": "Joe Ryan",
         "player_key": "joe ryan", "line": 17.5, "team": None, "side": "over", "prob": 0.55, "proj": None},
        {"key": "player_prop", "stat": "hits", "player": "Ghost Guy",
         "player_key": "ghost guy", "line": 1.5, "team": None, "side": "over", "prob": 0.5, "proj": None},
    ]
    paper.lock_forecasts({key: {"lock_now": True, "missed": False, "kickoff_iso": "2026-07-12T18:10Z",
                                "model": None, "market": (0.55, 0.0, 0.45), "sources": "kalshi",
                                "legs": legs}}, "2026-07-12T16:55:00Z")
    results = [{"date": "2026-07-12", "goals": {"athletics": 5, "chicago white sox": 2},
                "winner": "athletics", "iso": "2026-07-12T18:10Z",
                "players": {"joe ryan": {"outs recorded": 18}}}]
    paper.settle_forecasts(results, None)
    got = {l["player_key"]: l for l in paper.list_forecasts()[0]["legs"]}
    assert got["joe ryan"]["result"] == "won" and got["joe ryan"]["actual"] == 18
    assert got["ghost guy"]["result"] == "void"             # box score posted, player absent = DNP
    os.unlink(config.DB_PATH)


def test_prop_pair_split_via_learned_codes():
    from backend.sources.kalshi import _prop_pair
    codes = {"CLE": "cleveland guardians", "MIA": "miami marlins",
             "ATH": "athletics", "CWS": "chicago white sox", "AZ": "arizona diamondbacks",
             "LAD": "los angeles dodgers"}
    assert _prop_pair("KXMLBHIT-26JUL111610CLEMIA", codes) == ("cleveland guardians", "miami marlins")
    assert _prop_pair("KXMLBHR-26JUL111410ATHCWS", codes) == ("athletics", "chicago white sox")
    assert _prop_pair("KXMLBOUTS-26JUL121610AZLAD", codes) == ("arizona diamondbacks", "los angeles dodgers")
    assert _prop_pair("KXMLBHIT-26JUL111610XXYY", codes) is None


def test_f5_leg_grades_from_linescores(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "mlb")
    paper = _fresh_paper()
    key = "fc|2026-07-12|boston red sox|new york yankees|19:05"
    paper.log_forecasts([{"match": "x", "team_a": "boston red sox", "team_b": "new york yankees",
                          "commence_time": "2026-07-12", "stage": None, "dedup_key": key}],
                        today="2026-07-12")
    legs = [{"key": "f5", "side": "new york yankees", "line": None, "team": None,
             "prob": 0.44, "proj": None}]
    paper.lock_forecasts({key: {"lock_now": True, "missed": False, "kickoff_iso": "2026-07-12T23:05Z",
                                "model": None, "market": (0.5, 0.0, 0.5), "sources": "kalshi",
                                "legs": legs}}, "2026-07-12T21:50:00Z")
    # Yankees lead 3-1 after five, Boston wins 6-5 late: the F5 call still WINS while the ML pick loses
    results = [{"date": "2026-07-12", "goals": {"boston red sox": 6, "new york yankees": 5},
                "winner": "boston red sox", "iso": "2026-07-12T23:05Z",
                "innings": {"boston red sox": [0, 1, 0, 0, 0, 2, 0, 3, 0],
                            "new york yankees": [2, 0, 1, 0, 0, 0, 1, 1, 0]}}]
    paper.settle_forecasts(results, None)
    leg = paper.list_forecasts()[0]["legs"][0]
    assert leg["result"] == "won"
    os.unlink(config.DB_PATH)


def test_forecast_close_capture_stops_at_start(monkeypatch):
    """The ledger's CLV analog: closing_* overwrites each pre-start tick, never after start."""
    monkeypatch.setattr(config, "SPORT", "mlb")
    paper = _fresh_paper()
    key = "fc|2026-07-12|athletics|chicago white sox|14:10"
    paper.log_forecasts([{"match": "x", "team_a": "athletics", "team_b": "chicago white sox",
                          "commence_time": "2026-07-12", "stage": None, "dedup_key": key}],
                        today="2026-07-12")
    base = {"lock_now": True, "missed": False, "kickoff_iso": "2026-07-12T18:10Z",
            "model": None, "market": (0.55, 0.0, 0.45), "sources": "kalshi", "legs": []}
    paper.lock_forecasts({key: base}, "2026-07-12T16:55:00Z")
    # two pre-start ticks: the later one wins (overwrite-until-start = the close)
    paper.capture_forecast_close({key: {**base, "market": (0.58, 0.0, 0.42)}})
    paper.capture_forecast_close({key: {**base, "market": (0.61, 0.0, 0.39)}})
    r = paper.list_forecasts()[0]
    assert r["closing_a"] == 0.61 and r["market_a"] == 0.55   # lock frozen, close drifted
    # after first pitch the board flags missed: the close must stop moving
    paper.capture_forecast_close({key: {**base, "missed": True, "market": (0.90, 0.0, 0.10)}})
    assert paper.list_forecasts()[0]["closing_a"] == 0.61


def test_likely_postponed_flag_marks_rainouts_without_voiding(monkeypatch):
    """A locked row whose pair later played a SETTLED game is labelled, not voided: the status must
    survive so a suspended game's late final can still grade."""
    monkeypatch.setattr(config, "SPORT", "mlb")
    paper = _fresh_paper()

    def add(a, b, when, status, sport="mlb"):
        with paper._conn() as c:
            return c.execute(
                "INSERT INTO forecasts (match, team_a, team_b, commence_time, status, sport, "
                "logged_at, market_a, market_b) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"{a} v {b}", a, b, when, status, sport, "2026-07-20T00:00:00Z", 0.5, 0.5)).lastrowid

    rained = add("baltimore orioles", "boston red sox", "2026-07-21", "locked")
    add("baltimore orioles", "boston red sox", "2026-07-22", "settled")     # the makeup, settled
    alone = add("chicago cubs", "detroit tigers", "2026-07-21", "locked")   # no later game: not flagged
    add("chicago white sox", "texas rangers", "2026-07-21", "locked")
    add("chicago white sox", "texas rangers", "2026-07-22", "void")         # void is NOT evidence

    rows = {r["id"]: r for r in paper.list_forecasts()}
    assert rows[rained]["likely_postponed"] is True
    assert rows[alone]["likely_postponed"] is False
    # a later VOID row proves nothing was played, so it must not flag the earlier row
    assert not any(r["likely_postponed"] for i, r in rows.items()
                   if r["team_a"] == "chicago white sox")
    # the flag is display-only: every status is untouched, so a late final can still settle
    with paper._conn() as c:
        got = [r["status"] for r in c.execute("SELECT status FROM forecasts WHERE status='locked'")]
    assert len(got) == 3
