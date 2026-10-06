"""NFL: label alignment against the live Kalshi + ESPN sets, real Kalshi prop payloads through the
parser, one-line-per-player-and-stat prop selection, and grading from a football box score (where a
player absent from a stat group still played, and a quarterback's TD passes are not his touchdowns)."""
import asyncio
import importlib
import os
import tempfile
from datetime import datetime, timezone

from backend import aggregator, config, sports
from backend.models import Market, Quote, Selection
from backend.matching import normalize_team
from backend.sources import espn, kalshi

# both exactly as returned live on 2026-10-06 (Kalshi KXNFLGAME yes_sub_title, ESPN displayName)
KALSHI = {"ARI": "Arizona", "ATL": "Atlanta", "BAL": "Baltimore", "BUF": "Buffalo", "CAR": "Carolina",
          "CHI": "Chicago", "CIN": "Cincinnati", "CLE": "Cleveland", "DAL": "Dallas", "DEN": "Denver",
          "DET": "Detroit", "GB": "Green Bay", "HOU": "Houston", "IND": "Indianapolis",
          "JAC": "Jacksonville", "KC": "Kansas City", "LAC": "Los Angeles C", "LAR": "Los Angeles R",
          "LV": "Las Vegas", "MIA": "Miami", "MIN": "Minnesota", "NE": "New England",
          "NO": "New Orleans", "NYG": "New York G", "NYJ": "New York J", "PHI": "Philadelphia",
          "PIT": "Pittsburgh", "SEA": "Seattle", "SF": "San Francisco", "TB": "Tampa Bay",
          "TEN": "Tennessee", "WAS": "Washington"}
ESPN = {"ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens",
        "BUF": "Buffalo Bills", "CAR": "Carolina Panthers", "CHI": "Chicago Bears",
        "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns", "DAL": "Dallas Cowboys",
        "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
        "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAC": "Jacksonville Jaguars",
        "KC": "Kansas City Chiefs", "LAC": "Los Angeles Chargers", "LAR": "Los Angeles Rams",
        "LV": "Las Vegas Raiders", "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings",
        "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
        "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
        "SEA": "Seattle Seahawks", "SF": "San Francisco 49ers", "TB": "Tampa Bay Buccaneers",
        "TEN": "Tennessee Titans", "WAS": "Washington Commanders"}


def _fresh_paper():
    config.DB_PATH = tempfile.mktemp(suffix=".db")
    from backend.store import paper
    importlib.reload(paper)
    paper.init_paper()
    return paper


def test_nfl_adapter_registered():
    a = sports.get("nfl")
    assert a.outcomes == ("a", "b") and a.espn_path == "football/nfl"
    assert list(a.kalshi_series)[0] == "KXNFLGAME"           # first, so it teaches the ticker codes
    props = {v[1] for v in a.kalshi_series.values() if v[0] == "player_prop"}
    assert props == {"passing yards", "rushing yards", "receiving yards", "receptions", "touchdowns"}
    assert a.kalshi_series["KXNFLSPREAD"] == ("spread", "Lines")
    assert dict(a.ledger_prop_quota) == {"passing yards": 2, "rushing yards": 4, "receiving yards": 6,
                                          "receptions": 6, "touchdowns": 6}
    assert a.capabilities == frozenset() and a.pair_only_key is False


def test_every_kalshi_label_lands_on_its_espn_team(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "nfl")
    for code, label in KALSHI.items():
        espn_key = normalize_team(ESPN[code])
        assert normalize_team(label) == espn_key, (code, label, espn_key)
        assert espn_key == ESPN[code].lower(), (code, espn_key)   # no global alias captures a club
    assert len({normalize_team(v) for v in ESPN.values()}) == 32


def _m(ticker, title, sub, bid, ask, strike=None, event=None):
    return {"ticker": ticker, "event_ticker": event or ticker.rsplit("-", 1)[0], "title": title,
            "yes_sub_title": sub, "yes_bid_dollars": bid, "yes_ask_dollars": ask,
            "last_price_dollars": ask, "floor_strike": strike, "volume_fp": "100"}


def test_real_prop_payloads_join_their_game(monkeypatch):
    """Exact live shapes from the TB @ DAL Thursday game: the moneyline teaches the codes, and every
    prop and total then knows its game from the ticker alone."""
    monkeypatch.setattr(config, "SPORT", "nfl")
    ev = "KXNFLPASSYDS-26OCT08TBDAL"
    raw = {
        "KXNFLGAME": [_m("KXNFLGAME-26OCT08TBDAL-DAL", "Dallas wins", "Dallas", "0.8000", "0.8100"),
                      _m("KXNFLGAME-26OCT08TBDAL-TB", "Tampa Bay wins", "Tampa Bay", "0.1900", "0.2000")],
        "KXNFLTOTAL": [_m("KXNFLTOTAL-26OCT08TBDAL-47", "Full Game: over 46.5 points scored?",
                          "Over 46.5 points", "0.4900", "0.5000", 46.5)],
        "KXNFLPASSYDS": [_m(f"{ev}-DALDPRESCOTT4-250", "Dak Prescott: 250+ passing yards",
                            "Dak Prescott: 250+", "0.5100", "0.5300", 249.5, ev)],
    }

    async def fake_series(client, series, status="open"):
        return raw.get(series, [])

    monkeypatch.setattr(kalshi, "_fetch_series", fake_series)
    mk = asyncio.run(kalshi.fetch(None))
    ml = next(m for m in mk if m.market_type == "moneyline")
    assert ml.event == "Tampa Bay vs Dallas" and ml.commence_time == "2026-10-08"
    tot = next(m for m in mk if m.market_type == "total")
    pr = next(m for m in mk if m.market_type == "player_prop")
    assert tot.group == "Lines|tampa bay buccaneers|dallas cowboys"
    assert pr.group == "passing yards|tampa bay buccaneers|dallas cowboys"
    assert pr.selections[0].label == "Dak Prescott: 250+" and pr.selections[0].key == "over_249.5"


def _ml(a_key, b_key, date, pa, pb):
    def sel(k, p):
        return Selection(key=k, label=k, fair_prob=p,
                         quotes=[Quote(source="kalshi", source_type="prediction_market",
                                       price_decimal=1 / p, implied_prob=p, mid_prob=p,
                                       bid=round(p - 0.01, 2), ask=round(p + 0.01, 2))])
    return Market(market_id=f"kalshi:{a_key}{b_key}", event="x", market_type="moneyline",
                  selections=[sel(a_key, pa), sel(b_key, pb)], commence_time=date)


def _board_props(props, spreads=None):
    dal, tb = "dallas cowboys", "tampa bay buccaneers"
    pair = frozenset((dal, tb))
    board = aggregator._forecast_board(
        [_ml(dal, tb, "2026-10-08", 0.80, 0.20)], None, {(pair, "2026-10-08"): "2026-10-09T00:15Z"}, 75,
        datetime(2026, 10, 8, 12, tzinfo=timezone.utc), props={(pair, "2026-10-08"): props},
        spreads={(pair, "2026-10-08"): spreads} if spreads else None)[1]
    return next(iter(board.values()))["legs"]


def _p(player, stat, line, fair):
    return {"stat": stat, "player": player, "player_key": player.lower(), "line": line, "fair": fair}


def test_prop_sheet_one_line_per_player_and_slots_by_type(monkeypatch):
    """A receiving-yards ladder is one bet several times: only the rung nearest 50% may lock. Each prop
    type then fills its own slots by role (biggest line first), so receptions cannot crowd out yards."""
    monkeypatch.setattr(config, "SPORT", "nfl")
    ladder = [_p("CeeDee Lamb", "receiving yards", ln, f)
              for ln, f in ((49.5, 0.81), (64.5, 0.62), (79.5, 0.47), (99.5, 0.28))]
    others = [_p(f"WR{i}", "receiving yards", 10.5 + i * 5, 0.5) for i in range(9)]
    recs = [_p(f"RB{i}", "receptions", 1.5 + i, 0.5) for i in range(9)]
    legs = [l for l in _board_props(ladder + others + recs) if l["key"] == "player_prop"]
    lamb = [l for l in legs if l["player_key"] == "ceedee lamb"]
    assert len(lamb) == 1 and lamb[0]["line"] == 79.5            # the 47% rung, not 62% or 81%
    yds = [l for l in legs if l["stat"] == "receiving yards"]
    assert len(yds) == 6 and yds[0]["line"] == 79.5              # 6 slots, biggest role first
    rec = [l for l in legs if l["stat"] == "receptions"]
    assert len(rec) == 6 and [l["line"] for l in rec] == [9.5, 8.5, 7.5, 6.5, 5.5, 4.5]


def test_anytime_td_is_the_1plus_line_and_the_likeliest_scorers(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "nfl")
    tds = [_p("Star Back", "touchdowns", 0.5, 0.74), _p("Star Back", "touchdowns", 1.5, 0.36)]  # 2+ nearer 50%
    tds += [_p(f"Scorer{i}", "touchdowns", 0.5, 0.10 + i * 0.05) for i in range(8)]
    legs = [l for l in _board_props(tds) if l["stat"] == "touchdowns"]
    star = [l for l in legs if l["player_key"] == "star back"]
    assert len(star) == 1 and star[0]["line"] == 0.5 and star[0]["side"] == "over"
    assert len(legs) == 6 and legs[0]["player_key"] == "star back"   # likeliest scorers fill the slots


def _group(name, keys, rows):
    return {"name": name, "keys": keys,
            "athletes": [{"athlete": {"displayName": n}, "stats": st} for n, st in rows]}


# the real ESPN NFL box-score shape (keys exactly as ESPN returns them)
BOX = {"boxscore": {"players": [{"statistics": [
    _group("passing", ["completions/passingAttempts", "passingYards", "yardsPerPassAttempt",
                       "passingTouchdowns", "interceptions", "sacks-sackYardsLost", "adjQBR", "QBRating"],
           [("Daniel Jones", ["19/34", "143", "4.2", "2", "1", "2-14", "19.6", "53.9"])]),
    _group("rushing", ["rushingAttempts", "rushingYards", "yardsPerRushAttempt", "rushingTouchdowns",
                       "longRushing"],
           [("Daniel Jones", ["4", "2", "0.5", "1", "3"]), ("Jonathan Taylor", ["20", "95", "4.8", "2", "12"])]),
    _group("receiving", ["receptions", "receivingYards", "yardsPerReception", "receivingTouchdowns",
                         "longReception", "receivingTargets"],
           [("Jonathan Taylor", ["1", "2", "2.0", "0", "2", "1"])]),
    _group("kickReturns", ["kickReturns", "kickReturnYards", "yardsPerKickReturn", "longKickReturn",
                           "kickReturnTouchdowns"],
           [("Seth McGowan", ["4", "102", "25.5", "29", "0"])]),
]}]}}


def test_football_lines_zero_fill_and_score_only_touchdowns(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "nfl")
    lines = espn._player_lines(BOX)
    dj = lines["daniel jones"]
    assert (dj["passing completions"], dj["passing attempts"], dj["passing yards"]) == (19, 34, 143)
    assert dj["passing touchdowns"] == 2 and dj["interceptions thrown"] == 1
    assert dj["touchdowns"] == 1                     # his rushing TD scores; his two TD passes do not
    jt = lines["jonathan taylor"]
    assert jt["rushing and receiving yards"] == 97 and jt["touchdowns"] == 2
    assert lines["seth mcgowan"]["receptions"] == 0  # played (returns), caught nothing: zero, not absent


def test_prop_grading_zero_is_a_loss_and_absence_is_a_void(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "nfl")
    paper = _fresh_paper()
    players = espn._player_lines(BOX)
    legs = [{"key": "player_prop", "stat": "receptions", "player": "Seth McGowan",
             "player_key": "seth mcgowan", "line": 1.5, "side": "over", "prob": 0.5},
            {"key": "player_prop", "stat": "receiving yards", "player": "Not Here",
             "player_key": "not here", "line": 20.5, "side": "over", "prob": 0.5},
            {"key": "player_prop", "stat": "touchdowns", "player": "Daniel Jones",
             "player_key": "daniel jones", "line": 0.5, "side": "over", "prob": 0.2}]
    out = paper._grade_legs(legs, 30, 20, "x", "y", None, players=players)
    assert [l["result"] for l in out] == ["lost", "void", "won"]
    os.unlink(config.DB_PATH)


def test_spread_payload_names_its_covering_team_from_the_ticker(monkeypatch):
    """Exact live shapes: "DAL Cowboys wins by over 7.5 points" is ...TBDAL-DAL8. The code, not the
    label, names the team (college labels read "Western Kentucky" instead)."""
    monkeypatch.setattr(config, "SPORT", "nfl")
    ev = "KXNFLSPREAD-26OCT08TBDAL"
    raw = {
        "KXNFLGAME": [_m("KXNFLGAME-26OCT08TBDAL-DAL", "Dallas wins", "Dallas", "0.8000", "0.8100"),
                      _m("KXNFLGAME-26OCT08TBDAL-TB", "Tampa Bay wins", "Tampa Bay", "0.1900", "0.2000")],
        "KXNFLSPREAD": [_m(f"{ev}-DAL8", "DAL Cowboys wins by over 7.5 points?",
                           "DAL Cowboys wins by over 7.5 points", "0.5100", "0.5200", 7.5, ev),
                        _m(f"{ev}-TB2", "TB Buccaneers wins by over 1.5 points?",
                           "TB Buccaneers wins by over 1.5 points", "0.1700", "0.1800", 1.5, ev),
                        _m(f"{ev}-XX9", "?", "?", "0.5000", "0.5100", 8.5, ev)],   # unknown code: dropped
    }

    async def fake_series(client, series, status="open"):
        return raw.get(series, [])

    monkeypatch.setattr(kalshi, "_fetch_series", fake_series)
    sp = [m for m in asyncio.run(kalshi.fetch(None)) if m.market_type == "spread"]
    assert sorted(m.group for m in sp) == ["Lines|tampa bay buccaneers|dallas cowboys|dallas cowboys",
                                           "Lines|tampa bay buccaneers|dallas cowboys|tampa bay buccaneers"]


def test_spread_leg_takes_the_main_line_and_grades_off_the_margin(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "nfl")
    dal, tb = "dallas cowboys", "tampa bay buccaneers"
    lines = [(dal, 3.5, 0.68), (dal, 7.5, 0.515), (dal, 10.5, 0.42), (tb, 1.5, 0.175)]
    leg = next(l for l in _board_props([], spreads=lines) if l["key"] == "spread")
    assert (leg["team"], leg["opp"], leg["line"], leg["side"]) == (dal, tb, 7.5, "cover")
    paper = _fresh_paper()
    g = lambda ga, gb, side: paper._grade_legs([{**leg, "side": side}], ga, gb, dal, tb, None)[0]
    assert g(31, 20, "cover")["result"] == "won" and g(31, 20, "cover")["actual"] == 11
    assert g(24, 20, "cover")["result"] == "lost"                # won by 4: Tampa Bay +7.5 covers
    assert g(24, 20, "not")["result"] == "won"
    assert g(17, 20, "cover")["actual"] == -3                     # an outright loss is a negative margin
    os.unlink(config.DB_PATH)
