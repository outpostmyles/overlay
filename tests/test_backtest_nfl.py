"""The NFL history backtest: closing-line math on a hand-made slate, so every rate it reports is checked."""
import tempfile

from backend.engine import backtest_nfl as bt

HEADER = ("game_id,season,game_type,week,gameday,weekday,gametime,away_team,away_score,home_team,home_score,"
          "location,result,total,overtime,away_rest,home_rest,away_moneyline,home_moneyline,spread_line,"
          "away_spread_odds,home_spread_odds,total_line,under_odds,over_odds,div_game,roof,surface,temp,wind,"
          "away_qb_name,home_qb_name")


def _row(gid, season, day, time, away, ascore, home, hscore, *, ml=("NA", "NA"), spread=3.0, total_line=44.5,
         rest=(7, 7), roof="outdoors", wind="NA", qbs=("QA", "QH"), loc="Home"):
    return (f"{gid},{season},REG,1,{day},Sunday,{time},{away},{ascore},{home},{hscore},{loc},{hscore - ascore},"
            f"{ascore + hscore},0,{rest[0]},{rest[1]},{ml[0]},{ml[1]},{spread},-110,-110,{total_line},-110,-110,"
            f"0,{roof},grass,60,{wind},{qbs[0]},{qbs[1]}")


def _games(rows):
    path = tempfile.mktemp(suffix=".csv")
    with open(path, "w") as f:
        f.write(HEADER + "\n" + "\n".join(rows) + "\n")
    return bt.load(path)


def test_cover_and_de_vig_math():
    g = _games([_row("g1", 2020, "2020-09-13", "13:00", "NYJ", 10, "BUF", 17, spread=3.0)])[0]
    assert bt.ats(g, home=True) == 1 and bt.ats(g, home=False) == 0     # won by 7 laying 3
    push = _games([_row("g2", 2020, "2020-09-13", "13:00", "NYJ", 14, "BUF", 17, spread=3.0)])[0]
    assert bt.ats(push, home=True) is None
    pa, pb = bt.fair_pair(-150, 130)
    assert abs(pa + pb - 1) < 1e-9 and pa > 0.58
    assert bt.fair_pair(None, 130) is None


def test_tally_reports_the_gap_its_z_and_both_halves():
    rows = [(1, 0.5, 2010)] * 30 + [(0, 0.5, 2010)] * 20 + [(1, 0.5, 2020)] * 30 + [(0, 0.5, 2020)] * 20
    t = bt.tally(rows)
    assert (t["n"], t["hit"], t["expected"], t["gap"]) == (100, 60.0, 50.0, 10.0)
    assert t["z"] == 2.0 and t["early"] == 10.0 and t["late"] == 10.0
    assert bt.verdict(t, 1) == "holds" and bt.verdict(t, -1) == "fails"
    split = bt.tally([(1, 0.5, 2010)] * 50 + [(0, 0.5, 2020)] * 30 + [(1, 0.5, 2020)] * 20)
    assert bt.verdict(split, 1) == "weak"                    # the later half points the other way


def test_favorites_are_banded_by_their_de_vigged_price():
    games = _games([
        _row("a", 2010, "2010-09-12", "13:00", "NYJ", 10, "BUF", 20, ml=(250, -300)),   # ~74% home fav won
        _row("b", 2010, "2010-09-12", "13:00", "MIA", 24, "NE", 20, ml=(140, -160)),    # ~60% home fav lost
    ])
    fb = bt.favorite_bias(games)
    assert fb["70-75"]["n"] == 1 and fb["70-75"]["hit"] == 100.0
    assert fb["60-70"]["n"] == 1 and fb["60-70"]["hit"] == 0.0
    assert fb["all"]["n"] == 2


def test_wind_counts_outdoor_unders_against_the_close_by_band():
    games = _games([
        _row("w1", 2015, "2015-11-01", "13:00", "NYJ", 10, "BUF", 13, wind="16"),    # 23 under 44.5
        _row("w2", 2015, "2015-11-01", "13:00", "MIA", 30, "NE", 27, wind="16"),     # 57 over
        _row("w3", 2015, "2015-11-01", "13:00", "DAL", 10, "DET", 13, wind="16", roof="dome"),
        _row("w4", 2015, "2015-11-01", "13:00", "SEA", 10, "CHI", 13, wind="5"),
    ])
    w = bt.wind(games)
    assert w["wind_15"]["n"] == 2 and w["wind_15"]["hit"] == 50.0      # the dome game is left out
    assert w["wind_calm"]["n"] == 1 and w["wind_15plus"]["n"] == 2


def test_rest_night_travel_and_qb_changes():
    games = _games([
        # the home team had 13 days (a bye) against 6, laid 3 and won by 7
        _row("r1", 2015, "2015-10-04", "13:00", "NYJ", 10, "BUF", 17, rest=(6, 13)),
        # a West team (SF) at an East team (NYG) at 20:20 ET, getting 3 and losing by 3: a push
        _row("n1", 2015, "2015-10-04", "20:20", "SF", 17, "NYG", 20, spread=3.0),
        # Buffalo's quarterback changes from QH to QX in its next game; it covers
        _row("q2", 2015, "2015-10-11", "13:00", "MIA", 10, "BUF", 20, qbs=("QM", "QX")),
    ])
    rs = bt.rest(games)
    assert rs["rest_2011on"]["n"] == 1 and rs["rest_2011on"]["hit"] == 100.0
    assert rs["bye_2011on"]["n"] == 1
    nw = bt.night_west(games)
    assert nw["ats"]["n"] == 0                              # a push is not graded
    qb = bt.qb_change(games)
    assert qb["ats"]["n"] == 1 and qb["ats"]["hit"] == 100.0


def test_key_numbers_and_home_field():
    games = _games([_row("k1", 2016, "2016-09-11", "13:00", "NYJ", 10, "BUF", 13),
                    _row("k2", 2016, "2016-09-11", "13:00", "MIA", 10, "NE", 17)])
    k = bt.key_numbers(games)["2015-on"]
    assert (k["n"], k["3"], k["7"]) == (2, 50.0, 50.0)
    assert bt.home_field(games)["2010-2016"] == {"n": 2, "margin": 5.0, "spread": 3.0}
