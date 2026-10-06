"""Top bettors: Polymarket and Kalshi leaderboard traders' positions, read as sides of the board's lines,
with two-sided holders netted, market makers flagged, and a moneyline consensus only when it is clear."""
from backend import config, research
from backend.models import Market, Quote, Selection
from backend.sources import toptraders as tt

DAL, TB = "dallas cowboys", "tampa bay buccaneers"
NAMES = {"Buccaneers": TB, "Cowboys": DAL}


def test_positions_classify_into_the_full_game_lines_only(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "nfl")
    assert tt.classify("Buccaneers vs. Cowboys", "Cowboys", NAMES) == {"kind": "ml", "team": DAL}
    assert tt.classify("Spread: Cowboys (-9.5)", "Cowboys", NAMES) == {"kind": "spread", "team": DAL, "line": -9.5}
    assert tt.classify("Spread: Cowboys (-9.5)", "Buccaneers", NAMES) == {"kind": "spread", "team": TB, "line": 9.5}
    assert tt.classify("Buccaneers vs. Cowboys: O/U 47.5", "Under", NAMES) == {"kind": "total", "dir": "under", "line": 47.5}
    assert tt.classify("O/U 45.5", "Over", NAMES) == {"kind": "total", "dir": "over", "line": 45.5}
    for title, outcome in (("1H Spread: Buccaneers (-1.5)", "Buccaneers"),
                           ("Buccaneers vs. Cowboys: 1H O/U 16.5", "Over"),
                           ("Buccaneers vs. Cowboys: 2H Moneyline", "Cowboys"),
                           ("Buccaneers Team Total: O/U 10.5", "Over"),
                           ("Buccaneers vs. Cowboys: Total Two-Point Conversions O/U 0.5", "Over")):
        assert tt.classify(title, outcome, NAMES) is None, title


def test_team_names_match_nicknames_and_schools(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "nhl")
    keys = ("toronto maple leafs", "utah mammoth")
    assert tt.match_team("Maple Leafs", keys) == "toronto maple leafs"
    assert tt.match_team("Utah", keys) == "utah mammoth"
    assert tt.match_team("Rangers", ("new york rangers", "new york islanders")) == "new york rangers"
    assert tt.match_team("New York", ("new york rangers", "new york islanders")) is None   # ambiguous
    monkeypatch.setattr(config, "SPORT", "cfb")
    assert tt.match_team("Notre Dame", ("notre dame", "stanford")) == "notre dame"
    assert tt.match_team("Miami (FL)", ("miami", "louisville")) == "miami"


def test_events_map_to_board_games_within_a_day(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "nfl")
    games = [{"team_a": DAL, "team_b": TB, "pair": frozenset((DAL, TB)), "date": "2026-10-08",
              "kickoff_iso": "2026-10-09T00:15Z"}]
    got = tt.map_events([{"slug": "nfl-tb-dal-2026-10-09", "title": "Buccaneers vs. Cowboys"},
                         {"slug": "nfl-tb-dal-2026-12-20", "title": "Buccaneers vs. Cowboys"}], games)
    assert list(got) == ["nfl-tb-dal-2026-10-09"]               # the December rematch is a different game
    assert got["nfl-tb-dal-2026-10-09"]["names"] == {"Buccaneers": TB, "Cowboys": DAL}


def test_trim_keeps_open_game_positions_only():
    rows = [{"eventSlug": "nfl-tb-dal-2026-10-09", "title": "Buccaneers vs. Cowboys", "outcome": "Cowboys",
             "curPrice": 0.8, "size": 100, "avgPrice": 0.8, "initialValue": 80, "conditionId": "c", "outcomeIndex": 1,
             "cashPnl": 1, "icon": "x"},
            {"eventSlug": "nfl-tb-dal-2026-10-09", "curPrice": 1, "size": 5},       # resolved
            {"eventSlug": "nfl-mia-min-2026-10-04", "curPrice": 0.4, "redeemable": True},
            {"eventSlug": "mlb-world-series-champion-2026", "curPrice": 0.1},     # a future, not a game
            {"eventSlug": "epl-liv-ful-2026-09-12", "curPrice": 0.5}]
    kept = tt.trim_positions(rows)
    assert [(k["eventSlug"], k["outcome"]) for k in kept] == [("nfl-tb-dal-2026-10-09", "Cowboys")]
    assert "icon" not in kept[0] and kept[0]["initialValue"] == 80


def _q(p):
    return Quote(source="kalshi", source_type="prediction_market", price_decimal=1 / p, implied_prob=p, mid_prob=p)


def _kalshi_markets():
    ml = Market(market_id="kalshi:KXNFLGAME-26OCT08TBDAL", event="Tampa Bay vs Dallas", market_type="moneyline",
                selections=[Selection(key=DAL, label="Dallas", quotes=[_q(0.8)]),
                            Selection(key=TB, label="Tampa Bay", quotes=[_q(0.2)])], commence_time="2026-10-08")
    sp = Market(market_id="kalshi:KXNFLSPREAD-26OCT08TBDAL-DAL8", event="x", market_type="spread",
                selections=[Selection(key="over_7.5", label="x", quotes=[_q(0.52)]),
                            Selection(key="under_7.5", label="x", quotes=[_q(0.48)])],
                commence_time="2026-10-08", group=f"Lines|{TB}|{DAL}|{DAL}")
    tot = Market(market_id="kalshi:KXNFLTOTAL-26OCT08TBDAL-47", event="x", market_type="total",
                 selections=[Selection(key="over_47.5", label="x", quotes=[_q(0.5)]),
                             Selection(key="under_47.5", label="x", quotes=[_q(0.5)])],
                 commence_time="2026-10-08", group=f"Lines|{TB}|{DAL}")
    return [ml, sp, tot]


def test_kalshi_tickers_read_as_sides_of_the_lines():
    m = tt.kalshi_ticker_map(_kalshi_markets(), {"DAL": DAL, "TB": TB})
    assert m["KXNFLGAME-26OCT08TBDAL-DAL"]["yes"] == {"kind": "ml", "team": DAL}
    assert m["KXNFLGAME-26OCT08TBDAL-DAL"]["no"] == {"kind": "ml", "team": TB}
    assert m["KXNFLSPREAD-26OCT08TBDAL-DAL8"]["yes"] == {"kind": "spread", "team": DAL, "line": -7.5}
    assert m["KXNFLSPREAD-26OCT08TBDAL-DAL8"]["no"] == {"kind": "spread", "team": TB, "line": 7.5}
    assert m["KXNFLTOTAL-26OCT08TBDAL-47"]["no"] == {"kind": "total", "dir": "under", "line": 47.5}


def _cache():
    def pos(slug, title, outcome, idx, size, avg, cond):
        return {"eventSlug": slug, "title": title, "outcome": outcome, "outcomeIndex": idx, "conditionId": cond,
                "size": size, "avgPrice": avg, "curPrice": avg, "initialValue": size * avg}
    slug = "nfl-tb-dal-2026-10-09"
    return {
        "events": {"nfl": {"rows": [{"slug": slug, "title": "Buccaneers vs. Cowboys"}]}},
        "poly_leaders": {"rows": {
            "0xa": {"name": "sharp", "ranks": {"month": 3}, "pnl": {"month": 900000}, "vol": {"month": 9000000}},
            "0xb": {"name": "maker", "ranks": {"all": 2}, "pnl": {"all": 1000000}, "vol": {"all": 900000000}},
            "0xc": {"name": "hedger", "ranks": {"month": 9}, "pnl": {"month": 100000}, "vol": {"month": 1000000}},
            "0xd": {"name": "tiny", "ranks": {"month": 40}, "pnl": {"month": 1000}, "vol": {"month": 9000}}}},
        "positions": {
            "0xa": {"rows": [pos(slug, "Buccaneers vs. Cowboys", "Cowboys", 1, 2000, 0.8, "ml")]},
            "0xb": {"rows": [pos(slug, "Buccaneers vs. Cowboys", "Buccaneers", 0, 5000, 0.2, "ml")]},
            "0xc": {"rows": [pos(slug, "Spread: Cowboys (-9.5)", "Buccaneers", 1, 3000, 0.52, "sp"),
                             pos(slug, "Spread: Cowboys (-9.5)", "Cowboys", 0, 1000, 0.48, "sp")]},
            "0xd": {"rows": [pos(slug, "Buccaneers vs. Cowboys", "Buccaneers", 0, 10, 0.2, "ml")]}},
        "kalshi_leaders": {"rows": {"kpro": {"ranks": {"all": 5}, "pnl": {"all": 2000000}},
                                    "shy": {"ranks": {"30d": 1}, "pnl": {"30d": 50000}}}},
        "holdings": {"kpro": {"visible": True, "rows": [{"ticker": "KXNFLGAME-26OCT08TBDAL-DAL", "pos": -1500}]},
                     "shy": {"visible": False, "rows": []}},
    }


def test_assemble_nets_flags_and_finds_the_consensus(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "nfl")
    games = [{"team_a": DAL, "team_b": TB, "pair": frozenset((DAL, TB)), "date": "2026-10-08",
              "kickoff_iso": "2026-10-09T00:15Z"}]
    out = tt.assemble(_cache(), "nfl", games, tt.kalshi_ticker_map(_kalshi_markets(), {"DAL": DAL, "TB": TB}))
    g = out["games"][0]
    lines = {(l["kind"], l.get("team") or l.get("dir"), l.get("line")): l for l in g["lines"]}
    dal = lines[("ml", DAL, None)]
    assert [r["name"] for r in dal["rows"]] == ["sharp"] and dal["stake"] == 1600
    tb = lines[("ml", TB, None)]
    # the maker's $1,000 and Kalshi kpro's 1,500 no-contracts (Tampa Bay at 20c = $300) back Tampa Bay
    assert {r["name"]: r["stake"] for r in tb["rows"]} == {"maker": 1000, "kpro": 300}
    assert next(r for r in tb["rows"] if r["name"] == "maker")["mm"] is True
    assert next(r for r in tb["rows"] if r["name"] == "kpro")["contracts"] == 1500
    hedge = lines[("spread", TB, 9.5)]["rows"][0]
    assert hedge["two_sided"] and hedge["stake"] == round(2000 * 0.52)    # 3,000 - 1,000 shares, net
    assert "tiny" not in {r["name"] for l in g["lines"] for r in l["rows"]}     # a $2 position is noise
    # consensus counts directional money only: sharp $1,600 on Dallas vs kpro $300 on Tampa Bay
    assert g["consensus"] == {"team": DAL, "stake": 1600, "share": 0.842, "traders": 1, "total": 1900}
    assert out["meta"]["kalshi_visible"] == 1 and out["meta"]["events_matched"] == 1
    assert out["leaders"][0]["name"] == "sharp"


def test_no_consensus_when_the_money_is_small_or_split():
    small = [{"kind": "ml", "team": "a", "rows": [{"platform": "Polymarket", "name": "x", "stake": 900}]}]
    assert tt.consensus(small) is None
    split = [{"kind": "ml", "team": "a", "rows": [{"platform": "Polymarket", "name": "x", "stake": 1100}]},
             {"kind": "ml", "team": "b", "rows": [{"platform": "Polymarket", "name": "y", "stake": 900}]}]
    assert tt.consensus(split) is None                                  # 55% is not a clear side


def test_the_top_bettors_side_is_a_tracked_research_factor():
    ctx = {"top": {"team": "san francisco 49ers", "stake": 3791, "share": 1.0, "traders": 1, "total": 3791}}
    r = research.evaluate("nfl", ctx, (0.42, 0.0, 0.58), [], "san francisco 49ers", "seattle seahawks")
    f = next(x for x in r["factors"] if x["key"] == "top_traders")
    assert f["kind"] == "track" and f["label"] == "Top bettors: San Francisco 49ers $3.8k"
    assert f["target"] == {"kind": "ml", "team": "san francisco 49ers"} and f["p_crowd"] == 0.42
    assert r["probs"][0] == 0.42                                         # tracked: the number does not move
