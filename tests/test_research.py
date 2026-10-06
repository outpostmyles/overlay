"""The research layer: a Research % beside the market line, moved only by factors with evidence that the
market misprices them, with every other factor shown, graded, and left out of the number."""
import importlib
import json
import tempfile
from datetime import datetime, timezone

from backend import aggregator, config, research
from backend.models import Market, Quote, Selection
from backend.sources import espn, weather


def _legs(total_side="over", total_p=0.52, total_line=44.5, spread_line=2.5, spread_p=0.53):
    return [{"key": "total_goals", "side": total_side, "line": total_line, "team": None, "prob": total_p},
            {"key": "spread", "team": "dallas cowboys", "opp": "new york giants", "line": spread_line,
             "side": "cover", "prob": spread_p},
            {"key": "player_prop", "stat": "passing yards", "player": "Dak Prescott",
             "player_key": "dak prescott", "line": 249.5, "side": "over", "prob": 0.53}]


def _outdoor(**wx):
    return {"indoor": False, "weather": {"wind_mph": 3, "gust_mph": 5, "precip_in": 0.0, "temp_f": 60, **wx}}


# --- prices ------------------------------------------------------------------------------------- #
def test_value_at_is_the_worst_price_that_still_clears_the_cushion():
    assert research.value_at(0.65, 0.02) == -175        # exact -175.7: -175 clears 2%, -176 does not
    assert research.value_at(0.40, 0.02) == 155
    assert research.value_at(0.50, 0.02) == 104
    assert research.value_at(0.50, 0.0) == 100
    assert research.american_ev(0.65, -175) > 0.02 > research.american_ev(0.65, -177)
    assert research.value_at(None, 0.02) is None and research.value_at(1.0, 0.02) is None


# --- favorite bias -------------------------------------------------------------------------------- #
def test_favorite_bias_moves_only_favorites_of_60_percent_and_up():
    r = research.evaluate("nfl", None, (0.75, 0.0, 0.25), [], "a", "b")
    assert r["probs"] == (0.765, 0.0, 0.235)
    f = r["factors"][0]
    assert (f["key"], f["kind"], f["target"], f["adj"]) == ("fav_bias", "adjust", {"kind": "ml", "team": "a"}, 0.015)
    r = research.evaluate("cfb", None, (0.35, 0.0, 0.65), [], "a", "b")
    assert r["probs"] == (0.34, 0.0, 0.66)                  # the favorite in slot B gains, A gives it up
    r = research.evaluate("nfl", None, (0.55, 0.0, 0.45), [], "a", "b")
    assert r["probs"] == (0.55, 0.0, 0.45) and r["factors"] == []


def test_favorite_bias_never_pushes_a_near_certain_favorite_past_certainty():
    r = research.evaluate("cfb", None, (0.989, 0.0, 0.011), [], "notre dame", "stanford")
    pa, _, pb = r["probs"]
    assert 0.989 < pa < 1 and 0 < pb < 0.011


# --- weather ------------------------------------------------------------------------------------ #
def test_nfl_wind_moves_the_total_and_college_wind_is_only_tracked():
    legs = _legs(total_side="over", total_p=0.52)
    r = research.evaluate("nfl", _outdoor(wind_mph=13), (0.5, 0.0, 0.5), legs, "a", "b")
    assert legs[0]["research_prob"] == 0.475                # P(over) 0.52 - 0.045
    f = next(x for x in r["factors"] if x["key"] == "wind_12_14")
    assert f["kind"] == "adjust" and f["target"]["dir"] == "under"
    assert (f["p_crowd"], f["p_research"]) == (0.48, 0.525)
    legs = _legs(total_side="over", total_p=0.52)
    r = research.evaluate("cfb", _outdoor(wind_mph=13), (0.5, 0.0, 0.5), legs, "a", "b")
    assert legs[0]["research_prob"] == 0.52                 # college: graded first, not moved
    f = next(x for x in r["factors"] if x["key"] == "wind_12_14")
    assert f["kind"] == "track" and f["adj"] == 0 and f["p_research"] == f["p_crowd"]


def test_wind_and_rain_together_are_capped_and_domes_have_no_weather():
    legs = _legs(total_side="under", total_p=0.51)
    research.evaluate("nfl", _outdoor(wind_mph=13, precip_in=0.05), (0.5, 0.0, 0.5), legs, "a", "b")
    assert legs[0]["research_prob"] == 0.56                 # P(over) 0.49 - 0.05 cap -> under 0.56
    legs = _legs()
    r = research.evaluate("nfl", {"indoor": True, "weather": {"wind_mph": 20}}, (0.5, 0.0, 0.5), legs, "a", "b")
    assert not [f for f in r["factors"] if f["key"].startswith("wind") or f["key"] == "rain"]
    assert legs[0]["research_prob"] == legs[0]["prob"]


def test_passing_props_are_tracked_toward_the_under_and_flagged_in_bad_weather():
    legs = _legs()
    r = research.evaluate("nfl", _outdoor(wind_mph=16), (0.5, 0.0, 0.5), legs, "a", "b")
    keys = [f["key"] for f in r["factors"] if f["scope"] == "leg"]
    assert keys == ["pass_under", "pass_weather"]
    assert legs[2]["factors"] == ["pass_under", "pass_weather"]
    assert legs[2]["research_prob"] == 0.53                 # tracked, never moved
    assert next(f for f in r["factors"] if f["key"] == "pass_under")["p_crowd"] == 0.47


# --- tracked and caution factors ---------------------------------------------------------------- #
def test_qb_status_is_tracked_against_his_team_and_only_an_open_question_raises_the_cushion():
    ctx = {"qb": {"a": {"name": "Joe Burrow", "status": "Questionable"}}}
    r = research.evaluate("nfl", ctx, (0.6, 0.0, 0.4), _legs(), "a", "b")
    f = next(x for x in r["factors"] if x["key"] == "qb")
    assert f["target"] == {"kind": "ml", "team": "b"} and f["kind"] == "track"
    assert r["cushion"] == {"game": 0.04, "prop": 0.06}
    ctx = {"qb": {"a": {"name": "Joe Burrow", "status": "Out"}}}
    r = research.evaluate("nfl", ctx, (0.6, 0.0, 0.4), _legs(), "a", "b")
    assert r["cushion"] == {"game": 0.02, "prop": 0.04}     # ruled out is settled news


def test_rest_night_travel_and_key_numbers():
    ctx = {"rest": {"dallas cowboys": 4, "new york giants": 11}}
    r = research.evaluate("nfl", ctx, (0.5, 0.0, 0.5), _legs(), "dallas cowboys", "new york giants")
    f = next(x for x in r["factors"] if x["key"] == "rest")
    assert f["target"]["team"] == "new york giants" and f["label"] == "Rest +7d (bye)"
    assert any(x["key"] == "key_number" and x["label"] == "Spread next to 3" for x in r["factors"])
    ctx = {"kickoff_iso": "2026-10-12T00:20Z"}               # 8:20 PM Eastern
    r = research.evaluate("nfl", ctx, (0.5, 0.0, 0.5), [], "new york giants", "san francisco 49ers")
    assert next(x for x in r["factors"] if x["key"] == "night_west")["target"]["team"] == "san francisco 49ers"
    ctx = {"kickoff_iso": "2026-10-11T17:00Z"}               # 1 PM: no night factor
    r = research.evaluate("nfl", ctx, (0.5, 0.0, 0.5), [], "new york giants", "san francisco 49ers")
    assert not any(x["key"] == "night_west" for x in r["factors"])


def test_college_big_spreads_and_information_gaps():
    legs = _legs(spread_line=24.5, spread_p=0.52)
    r = research.evaluate("cfb", {"conference_game": False, "week": 3}, (0.95, 0.0, 0.05), legs, "a", "b")
    f = next(x for x in r["factors"] if x["key"] == "big_spread")
    assert f["target"] == {"kind": "leg", "key": "spread", "dir": "dog"} and f["p_crowd"] == 0.48
    assert {"no_report", "early_season"} <= {x["key"] for x in r["factors"]}
    assert r["cushion"] == {"game": 0.06, "prop": 0.08}     # two open questions, capped bump
    assert not any(x["key"] == "key_number" for x in r["factors"])   # key numbers are an NFL flag


def test_draftkings_prices_are_scored_against_research_only_on_matching_lines():
    ctx = {"dk": {"book": "DraftKings", "ml": {"a": -192, "b": 160},
                  "spread": {"team": "dallas cowboys", "line": 2.5, "cover": -110, "dog": -110},
                  "total": {"line": 47.5, "over": -105, "under": -115}}}
    legs = _legs()
    r = research.evaluate("nfl", ctx, (0.65, 0.0, 0.35), legs, "a", "b")
    dk = r["dk"]
    assert dk["ml"] == {"a": -192, "b": 160}
    assert dk["ev"]["a"] == research.american_ev(0.66, -192)
    assert "spread" in dk and "total" not in dk             # 47.5 is not the locked 44.5 total


def test_every_leg_carries_a_research_prob_and_a_value_price():
    legs = _legs()
    research.evaluate("nfl", None, (0.5, 0.0, 0.5), legs, "a", "b")
    assert all(l["research_prob"] == l["prob"] for l in legs)
    assert legs[2]["value_at"] == research.value_at(0.53, 0.04)   # props clear the 4% prop cushion


# --- grading a factor's claim --------------------------------------------------------------------- #
def test_target_hit_grades_each_kind_of_claim():
    row = {"team_a": "a", "team_b": "b", "actual_outcome": "b"}
    legs = [{"key": "total_goals", "line": 44.5, "actual": 41, "result": "lost", "side": "over"},
            {"key": "spread", "line": 7.5, "actual": 3, "result": "lost", "side": "cover"},
            {"key": "player_prop", "player_key": "x", "stat": "passing yards", "line": 249.5,
             "actual": 260, "result": "won"},
            {"key": "player_prop", "player_key": "y", "stat": "passing yards", "line": 199.5,
             "actual": None, "result": "void"}]
    assert research.target_hit({"kind": "ml", "team": "b"}, row, legs) == 1
    assert research.target_hit({"kind": "ml", "team": "a"}, row, legs) == 0
    assert research.target_hit({"kind": "leg", "key": "total_goals", "dir": "under"}, row, legs) == 1
    assert research.target_hit({"kind": "leg", "key": "spread", "dir": "dog"}, row, legs) == 1
    prop = {"kind": "leg", "key": "player_prop", "player_key": "x", "stat": "passing yards", "dir": "under"}
    assert research.target_hit(prop, row, legs) == 0
    assert research.target_hit({**prop, "player_key": "y"}, row, legs) is None   # void: not graded
    assert research.target_hit(None, row, legs) is None


# --- the ledger: freeze, grade, study ------------------------------------------------------------- #
def _fresh_paper():
    config.DB_PATH = tempfile.mktemp(suffix=".db")
    from backend.store import paper
    importlib.reload(paper)
    paper.init_paper()
    return paper


def test_research_freezes_at_lock_grades_and_feeds_the_study(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "nfl")
    paper = _fresh_paper()
    key = "fc|2026-10-11|a|b"
    paper.log_forecasts([{"match": "A vs B", "team_a": "a", "team_b": "b", "commence_time": "2026-10-11",
                          "stage": None, "dedup_key": key}], today="2026-10-10")
    legs = [{"key": "total_goals", "side": "over", "line": 44.5, "team": None, "prob": 0.52}]
    rs = research.evaluate("nfl", _outdoor(wind_mph=13), (0.75, 0.0, 0.25), legs, "a", "b")
    board = {key: {"lock_now": True, "missed": False, "kickoff_iso": "2026-10-11T17:00Z", "model": None,
                   "market": (0.75, 0.0, 0.25), "sources": "kalshi", "legs": legs, "research": rs}}
    assert paper.lock_forecasts(board, "2026-10-11T15:45:00Z") == 1
    row = paper.list_forecasts()[0]
    assert (row["research_a"], row["research_b"]) == (0.765, 0.235)
    assert {f["key"] for f in row["research"]["factors"]} == {"fav_bias", "wind_12_14"}
    # A wins 20-17: the favorite came through and the total went under 44.5
    results = [{"date": "2026-10-11", "goals": {"a": 20, "b": 17}, "iso": "2026-10-11T17:00Z"}]
    assert paper.settle_forecasts(results) == 1
    row = paper.list_forecasts()[0]
    assert row["brier_research"] < row["brier_market"]
    st = paper.research_study()
    assert st["ml"]["n"] == 1 and st["ml"]["closer"] == 1 and st["legs"]["n"] == 1
    by = {f["key"]: f for f in st["factors"]}
    assert (by["fav_bias"]["n"], by["fav_bias"]["hits"], by["fav_bias"]["kind"]) == (1, 1, "adjust")
    assert (by["wind_12_14"]["hits"], by["wind_12_14"]["crowd"], by["wind_12_14"]["research"]) == (1, 48.0, 52.5)


def test_rows_without_research_still_lock_and_settle(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "mlb")
    paper = _fresh_paper()
    key = "fc|2026-10-01|a|b"
    paper.log_forecasts([{"match": "A vs B", "team_a": "a", "team_b": "b", "commence_time": "2026-10-01",
                          "stage": None, "dedup_key": key}], today="2026-10-01")
    board = {key: {"lock_now": True, "missed": False, "kickoff_iso": "2026-10-01T23:00Z", "model": None,
                   "market": (0.6, 0.0, 0.4), "sources": "kalshi", "legs": []}}
    assert paper.lock_forecasts(board, "2026-10-01T21:45:00Z") == 1
    assert paper.settle_forecasts([{"date": "2026-10-01", "goals": {"a": 3, "b": 2}}]) == 1
    row = paper.list_forecasts()[0]
    assert row["research_a"] is None and row["brier_research"] is None and row["research"] is None
    assert paper.research_study()["games"] == 0


def test_migration_adds_research_columns_without_touching_old_rows():
    import sqlite3
    path = tempfile.mktemp(suffix=".db")
    with sqlite3.connect(path) as c:      # a ledger from before the research layer
        c.execute("CREATE TABLE forecasts (id INTEGER PRIMARY KEY AUTOINCREMENT, match TEXT NOT NULL, "
                  "team_a TEXT NOT NULL, team_b TEXT NOT NULL, commence_time TEXT, stage TEXT, "
                  "logged_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', dedup_key TEXT UNIQUE, "
                  "market_a REAL, market_draw REAL, market_b REAL, actual_outcome TEXT)")
        c.execute("INSERT INTO forecasts (match, team_a, team_b, logged_at, status, market_a, market_draw, "
                  "market_b, actual_outcome) VALUES ('x','a','b','t','settled',0.6,0,0.4,'a')")
    config.DB_PATH = path
    from backend.store import paper
    importlib.reload(paper)
    paper.init_paper()
    paper.init_paper()                    # idempotent
    with sqlite3.connect(path) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(forecasts)")}
        old = c.execute("SELECT market_a, status, research_a FROM forecasts").fetchone()
    assert {"research_a", "research_draw", "research_b", "brier_research", "research_json"} <= cols
    assert old == (0.6, "settled", None)


# --- the board -------------------------------------------------------------------------------------- #
def _ml(a, b, pa, date="2026-10-11"):
    def sel(k, p):
        return Selection(key=k, label=k, fair_prob=p,
                         quotes=[Quote(source="kalshi", source_type="prediction_market", price_decimal=1 / p,
                                       implied_prob=p, mid_prob=p, bid=round(p - 0.01, 3), ask=round(p + 0.01, 3))])
    return Market(market_id=f"kalshi:{a}{b}", event=f"{a} vs {b}", market_type="moneyline",
                  selections=[sel(a, pa), sel(b, 1 - pa)], commence_time=date)


def test_the_board_carries_research_for_football_only(monkeypatch):
    pair = frozenset(("dallas cowboys", "new york giants"))
    kicks = {(pair, "2026-10-11"): "2026-10-11T17:00Z"}
    now = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
    ctx = {(pair, "2026-10-11"): _outdoor(wind_mph=10)}
    monkeypatch.setattr(config, "SPORT", "nfl")
    board = aggregator._forecast_board([_ml("dallas cowboys", "new york giants", 0.7)], None, kicks, 75, now,
                                       research_ctx=ctx)[1]
    rs = next(iter(board.values()))["research"]
    assert rs["probs"][0] == 0.715
    by = {f["key"]: f for f in rs["factors"]}
    assert set(by) == {"fav_bias", "wind_8_11"}
    assert by["wind_8_11"]["target"] is None                # no total listed: shown, nothing to grade
    monkeypatch.setattr(config, "SPORT", "mlb")
    board = aggregator._forecast_board([_ml("a", "b", 0.7, "2026-10-01")], None,
                                       {(frozenset(("a", "b")), "2026-10-01"): "2026-10-01T23:00Z"}, 75,
                                       datetime(2026, 10, 1, 12, tzinfo=timezone.utc))[1]
    assert next(iter(board.values()))["research"] is None


# --- the free feeds --------------------------------------------------------------------------------- #
def test_espn_context_and_extras(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "nfl")
    ev = {"id": "401", "date": "2026-10-11T17:00Z", "week": {"number": 5},
          "competitions": [{"neutralSite": False, "conferenceCompetition": False,
                            "venue": {"fullName": "Gillette Stadium", "indoor": False,
                                      "address": {"city": "Foxborough", "state": "MA", "country": "USA"}},
                            "competitors": [
                                {"homeAway": "home", "team": {"id": "17", "displayName": "New England Patriots"},
                                 "leaders": [{"name": "passingLeader",
                                              "leaders": [{"athlete": {"displayName": "Drake Maye"}}]}]},
                                {"homeAway": "away", "team": {"id": "13", "displayName": "Las Vegas Raiders"},
                                 "leaders": [{"name": "passingLeader",
                                              "leaders": [{"athlete": {"displayName": "Geno Smith"}}]}]}]}]}
    ctx = espn.game_context(ev)
    assert (ctx["home"], ctx["away"], ctx["city"], ctx["indoor"], ctx["week"]) == (
        "new england patriots", "las vegas raiders", "Foxborough", False, 5)
    assert ctx["passers"]["las vegas raiders"] == "Geno Smith"
    summary = {
        "injuries": [{"team": {"displayName": "Las Vegas Raiders"}, "injuries": [
            {"athlete": {"displayName": "Geno Smith", "position": {"abbreviation": "QB"}}, "status": "Questionable"},
            {"athlete": {"displayName": "Backup Guy", "position": {"abbreviation": "QB"}}, "status": "Out"}]},
            {"team": {"displayName": "New England Patriots"}, "injuries": [
                {"athlete": {"displayName": "Drake Maye", "position": {"abbreviation": "QB"}},
                 "status": "Injured Reserve"}]}],
        "pickcenter": [{"provider": {"name": "DraftKings"}, "spread": -3.5, "overUnder": 45.5,
                        "overOdds": -105, "underOdds": -115,
                        "homeTeamOdds": {"teamId": "17", "moneyLine": -192, "spreadOdds": -110},
                        "awayTeamOdds": {"teamId": "13", "moneyLine": 160, "spreadOdds": -110}}]}
    ex = espn.game_extras(summary, ctx)
    assert ex["qb"] == {"las vegas raiders": {"name": "Geno Smith", "status": "Questionable"}}
    assert ex["dk"]["ml"] == {"new england patriots": -192, "las vegas raiders": 160}
    assert ex["dk"]["spread"] == {"team": "new england patriots", "line": 3.5, "cover": -110, "dog": -110}
    assert ex["dk"]["total"] == {"line": 45.5, "over": -105, "under": -115}
    covered = {**ev, "competitions": [{**ev["competitions"][0], "venue": {"fullName": "SoFi Stadium", "indoor": False}}]}
    assert espn.game_context(covered)["indoor"] is True    # a fixed roof keeps the weather out


def test_weather_reading_and_place_matching():
    hourly = {"time": ["2026-10-11T16:00", "2026-10-11T17:00", "2026-10-11T18:00"],
              "wind_speed_10m": [9.0, 14.1, 13.8], "wind_gusts_10m": [15, 24.4, 24.2],
              "precipitation": [0.0, 0.0, 0.03], "temperature_2m": [70, 72.3, 72.5]}
    r = weather.reading_at(hourly, datetime(2026, 10, 11, 17, 25, tzinfo=timezone.utc))
    assert r == {"wind_mph": 14.1, "gust_mph": 24.4, "precip_in": 0.03, "temp_f": 72.3}
    assert weather.reading_at(hourly, datetime(2026, 10, 12, 17, tzinfo=timezone.utc)) is None
    results = [{"admin1": "Georgia", "latitude": 30.9, "longitude": -83.3},
               {"admin1": "Massachusetts", "latitude": 42.07, "longitude": -71.25}]
    assert weather.pick_place(results, "MA", "USA") == (42.07, -71.25)
    assert weather.pick_place(results, "OR", "USA") is None   # never a same-named town in another state
    assert weather.pick_place(results, None, "England") == (30.9, -83.3)
    json.dumps(research.catalog("nfl"))                       # the explainer ships as JSON
    assert {f["key"] for f in research.catalog("cfb")} >= {"big_spread", "no_report", "fav_bias"}
    assert not any(f["key"] == "key_number" for f in research.catalog("cfb"))
