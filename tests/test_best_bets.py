"""Best Bets and Value Now: the slate reaches a weekly sport's whole week, every game of a series gets its own
card and its own price, Kalshi is priced after its fee, a favorite only carries a stake when a real price
beats our number, and Value Now still works on a board with no research layer."""
from datetime import date, timedelta

from backend import aggregator, config, picks, sports
from backend.engine import edges, odds_math
from backend.models import Market, Quote, Selection

LAD, ATL = "los angeles dodgers", "atlanta braves"
DAL, TB = "dallas cowboys", "tampa bay buccaneers"


def _day(n: int) -> str:
    return (date.today() + timedelta(days=n)).isoformat()


def _kalshi(ask: float) -> Quote:
    return Quote(source="kalshi", source_type="prediction_market", price_decimal=1 / ask, implied_prob=ask,
                 mid_prob=ask - 0.005, fee=config.KALSHI_FEE_COEF, bid=ask - 0.01, ask=ask)


def _book(dec: float, source: str = "draftkings") -> Quote:
    return Quote(source=source, source_type="sportsbook", price_decimal=dec, implied_prob=1 / dec)


def _game(event, start, fav, fav_label, opp, opp_label, fair, ask, opp_ask=None, extra=()):
    """A de-vigged 2-way moneyline: the favorite at `fair` with a Kalshi ask, plus any extra quotes."""
    return Market(market_id=f"ml:{fav}|{opp}|{start}", event=event, market_type="moneyline", commence_time=start,
                  selections=[Selection(key=fav, label=fav_label, quotes=[_kalshi(ask), *extra], fair_prob=fair),
                              Selection(key=opp, label=opp_label, quotes=[_kalshi(opp_ask or 1 - ask + 0.02)],
                                        fair_prob=1 - fair)])


def _cards(markets, research_by_game=None, ai=None, model_loaded=False):
    board = picks.generate(markets, [], None, config)
    board["ai"] = ai or {}
    best = [r for m in markets for r in edges.best_lines(m, min_fair_prob=config.MIN_FAIR_PROB)]
    return aggregator._best_bets(board, best, research_by_game, model_loaded=model_loaded), board, best


# ---- 1. the slate horizon is the sport's ------------------------------------------------------------------ #

def test_weekly_sports_list_the_whole_week(monkeypatch):
    sunday = _game("Tampa Bay vs Dallas", _day(5), DAL, "Dallas", TB, "Tampa Bay", 0.70, 0.71)
    next_week = _game("Tampa Bay vs Dallas", _day(8), DAL, "Dallas", TB, "Tampa Bay", 0.70, 0.71)
    assert sports.get("nfl").slate_horizon_days == sports.get("cfb").slate_horizon_days == 6   # each weekday once
    assert sports.get("mlb").slate_horizon_days is None          # daily sports keep the global horizon

    monkeypatch.setattr(config, "SPORT", "nfl")
    assert picks.slate_horizon(config) == 6
    favs = picks.generate([sunday, next_week], [], None, config)["favorite_ml"]
    assert [f["days_out"] for f in favs] == [5]                  # Sunday shows on a Tuesday, next week does not
    assert [m["days_out"] for m in aggregator._slate_matchups([sunday, next_week])] == [5]

    monkeypatch.setattr(config, "SPORT", "mlb")
    monkeypatch.setattr(config, "SLATE_HORIZON_DAYS", 4)
    assert picks.slate_horizon(config) == 4
    assert picks.generate([sunday], [], None, config)["favorite_ml"] == []
    assert aggregator._slate_matchups([sunday]) == []


# ---- 2 + 3. one card per game, priced at its own game's line ---------------------------------------------- #

def test_each_game_of_a_series_gets_its_own_card_and_its_own_price(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "mlb")
    g1 = _game("Los Angeles D vs Atlanta", _day(0) + "T20:08", LAD, "Los Angeles D", ATL, "Atlanta", 0.56, 0.51)
    g2 = _game("Los Angeles D vs Atlanta", _day(1) + "T18:00", LAD, "Los Angeles D", ATL, "Atlanta", 0.58, 0.60)
    cards, board, best = _cards([g1, g2])
    dodgers = {c["days_out"]: c for c in cards if c["selection"] == "Los Angeles D ML"}
    assert sorted(dodgers) == [0, 1]                                # both games, not only game 1
    for days, ask in ((0, 0.51), (1, 0.60)):
        assert dodgers[days]["best_american"] == odds_math.prob_to_american(aggregator._kalshi_cost(ask))

    # the reasoning bundles share the series label too: each is priced at its own game
    bundles = board["match_bundles"]
    aggregator._enrich_bundles_price(bundles, best)
    by_day = {b["days_out"]: b["favorite_best_price_american"] for b in bundles}
    assert by_day == {0: dodgers[0]["best_american"], 1: dodgers[1]["best_american"]}

    # with no start to tell the games apart, the label alone is ambiguous: no price beats the wrong one
    line = aggregator._price_lookup(best)
    assert line("Los Angeles D vs Atlanta", "Los Angeles D") == {}
    assert line("Los Angeles D vs Atlanta", "Los Angeles D", _day(1) + "T18:00")["best_book"] == "kalshi"

    # an AI pick on game 1 logs game 1's price to the paper ledger, not game 2's
    verdicts = {"Los Angeles D vs Atlanta": {"confidence": 3, "recommended_bets": [
        {"archetype": "favorite_ml", "selection": "Los Angeles D ML"}]}}
    rows = aggregator._paper_rows(verdicts, [b for b in bundles if b["days_out"] == 0], best)
    assert rows[0]["pick_price_decimal"] == round(1 / 0.51, 4)


def test_a_card_reads_its_own_games_research(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "nhl")
    pair = frozenset((DAL, TB))
    g1 = _game("Tampa Bay vs Dallas", _day(0), DAL, "Dallas", TB, "Tampa Bay", 0.62, 0.63)
    g2 = _game("Tampa Bay vs Dallas", _day(2), DAL, "Dallas", TB, "Tampa Bay", 0.60, 0.61)
    research = {(pair, _day(2)): {"ml": {DAL: 0.58, TB: 0.42}, "cushion": {"game": 0.02}}}
    cards, _, _ = _cards([g1, g2], research)
    got = {c["days_out"]: c.get("research_prob") for c in cards}
    assert got == {0: None, 2: 0.58}                              # game 1 does not borrow game 2's research

    # college football: ESPN's date can sit a day off Kalshi's, and the slack finds the game's own row
    monkeypatch.setattr(config, "SPORT", "cfb")
    f = {"team_key": DAL, "opp_key": TB, "commence_time": _day(1)}
    assert aggregator._research_for(research, f)["ml"][DAL] == 0.58
    monkeypatch.setattr(config, "SPORT", "nhl")
    assert aggregator._research_for(research, f) is None


# ---- 4. Kalshi after its fee ------------------------------------------------------------------------------ #

def test_the_cards_kalshi_price_and_ev_are_after_the_fee(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "nfl")
    m = _game("Tampa Bay vs Dallas", _day(2), DAL, "Dallas", TB, "Tampa Bay", 0.795, 0.80)
    cards, _, _ = _cards([m])
    c = cards[0]
    cost = 0.80 + config.KALSHI_FEE_COEF * 0.80 * 0.20              # what Value Now and the lotto pay
    assert c["best_book"] == "kalshi" and c["best_american"] == odds_math.prob_to_american(cost) == -430
    assert c["ev_pct"] == round((0.795 / cost - 1) * 100, 1) == -2.0
    # the same ask through Value Now gives the same price
    vn = aggregator._value_now([{"dedup": "x", "team_a": DAL, "team_b": TB, "kalshi_ask": {DAL: 0.80},
                                 "research": {"ml": {DAL: 0.85, TB: 0.15}, "cushion": {"game": 0.02}}}])
    assert vn[0]["price"] == c["best_american"]


def test_the_best_venue_is_chosen_after_the_fee(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "nfl")
    # Kalshi's raw 80c is 1.25, ahead of the book's 1.24, but after the fee it pays 1.2327
    m = _game("Tampa Bay vs Dallas", _day(2), DAL, "Dallas", TB, "Tampa Bay", 0.795, 0.80, extra=[_book(1.24)])
    c = _cards([m])[0][0]
    assert c["best_book"] == "draftkings" and c["best_american"] == odds_math.decimal_to_american(1.24)


# ---- 5. no stake without value ---------------------------------------------------------------------------- #

def test_a_favorite_carries_a_stake_only_when_a_real_price_beats_our_number(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "nfl")
    teams = [("dallas cowboys", "Dallas"), ("buffalo bills", "Buffalo"), ("detroit lions", "Detroit"),
             ("kansas city chiefs", "Kansas City"), ("green bay packers", "Green Bay")]
    opps = [("tampa bay buccaneers", "Tampa Bay"), ("miami dolphins", "Miami"), ("chicago bears", "Chicago"),
            ("denver broncos", "Denver"), ("seattle seahawks", "Seattle")]
    day = _day(3)
    markets = [
        _game("Tampa Bay vs Dallas", day, *teams[0], *opps[0], 0.80, 0.81),         # every price short of fair
        _game("Miami vs Buffalo", day, *teams[1], *opps[1], 0.60, 0.61),            # DraftKings beats research
        _game("Chicago vs Detroit", day, *teams[2], *opps[2], 0.66, 0.62),          # Kalshi beats it after the fee
        _game("Denver vs Kansas City", day, *teams[3], *opps[3], 0.70, 0.72),       # the AI recommends it
        _game("Seattle vs Green Bay", day, *teams[4], *opps[4], 0.75, 0.76),        # nothing: a bare favorite
    ]
    research = {
        (frozenset((teams[1][0], opps[1][0])), day): {
            "ml": {teams[1][0]: 0.60, opps[1][0]: 0.40}, "cushion": {"game": 0.02},
            "dk": {"book": "DraftKings", "ml": {teams[1][0]: -140}, "ev": {teams[1][0]: 0.0286}}},
        (frozenset((teams[2][0], opps[2][0])), day): {
            "ml": {teams[2][0]: 0.70, opps[2][0]: 0.30}, "cushion": {"game": 0.04}},
    }
    ai = {"Denver vs Kansas City": {"commence_time": day, "confidence": 3, "recommended_bets": [
        {"archetype": "favorite_ml", "selection": "Kansas City ML (Kalshi -257)", "rationale": "rested"}]}}
    cards, _, _ = _cards(markets, research, ai)
    fav = {c["selection"]: c for c in cards if c["source"] != "ai"}
    stake = round(1.0 * config.BANKROLL * config.UNIT_PCT)

    no = fav["Dallas ML"]
    assert (no["value"], no["tier"], no["stake_units"], no["stake_dollars"]) == (False, "favorite", None, None)
    assert no["value_ev"] < 0
    dk = fav["Buffalo ML"]
    assert (dk["value"], dk["tier"], dk["stake_units"], dk["stake_dollars"]) == (True, "lean", 1.0, stake)
    assert (dk["value_venue"], dk["value_ev"]) == ("DraftKings", 0.0286)
    k = fav["Detroit ML"]                             # 70% research against 62c plus the fee clears its 4% cushion
    assert k["value"] and k["value_venue"] == "kalshi"
    assert k["value_ev"] == round(0.70 * round(1 / aggregator._kalshi_cost(0.62), 4) - 1, 4)
    assert fav["Kansas City ML"]["value"] and fav["Kansas City ML"]["stake_units"] == 1.0
    assert not fav["Green Bay ML"]["value"]

    # value first by EV (Kalshi's 10% edge, DraftKings' 2.9%, then the AI-backed one), then by probability
    order = [c["selection"] for c in cards if c["tier_rank"] == 1]
    assert order == ["Detroit ML", "Buffalo ML", "Kansas City ML", "Dallas ML", "Green Bay ML"]
    assert {c["source"] for c in fav.values()} == {"market"}       # no model on this board
    assert {c["source"] for c in _cards(markets[:1], model_loaded=True)[0]} == {"model"}
    ai_card = next(c for c in cards if c["source"] == "ai")
    assert ai_card["value"] is True and ai_card["best_book"] == "kalshi"


def test_a_cushion_short_edge_is_not_value(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "nfl")
    day = _day(2)
    m = _game("Tampa Bay vs Dallas", day, DAL, "Dallas", TB, "Tampa Bay", 0.70, 0.70)
    # DraftKings is 1.5% better than the research, under the 2% cushion: shown, not staked
    rs = {(frozenset((DAL, TB)), day): {"ml": {DAL: 0.70, TB: 0.30}, "cushion": {"game": 0.02},
                                        "dk": {"book": "DraftKings", "ml": {DAL: -225}, "ev": {DAL: 0.015}}}}
    c = _cards([m], rs)[0][0]
    assert c["value"] is False and c["stake_units"] is None and c["value_ev"] == 0.015


# ---- 6. Value Now without research ------------------------------------------------------------------------ #

def test_value_now_falls_back_to_the_market_on_a_board_without_research():
    g = {"dedup": "fc|x|a|b", "team_a": ATL, "team_b": LAD, "kickoff_iso": "2026-10-07T22:08Z",
         "market": (0.45, 0.0, 0.55), "research": None, "kalshi_ask": {ATL: 0.42, LAD: 0.56}}
    rows = aggregator._value_now([g])
    assert len(rows) == 1
    r = rows[0]
    cost = aggregator._kalshi_cost(0.42)
    assert (r["team"], r["venue"], r["basis"], r["cushion"], r["p"]) == (ATL, "Kalshi", "market", 0.02, 0.45)
    assert r["ev"] == round(0.45 / cost - 1, 4) and r["price"] == odds_math.prob_to_american(cost)
    assert r["value_at"] is not None

    # a board with research keeps scoring against it, and says so
    g2 = {**g, "research": {"ml": {ATL: 0.47, LAD: 0.53}, "cushion": {"game": 0.02}}}
    assert {x["basis"] for x in aggregator._value_now([g2])} == {"research"}


def test_a_kalshi_last_trade_is_not_a_price_anyone_can_take():
    """A Kalshi quote with no ask is a last trade: it can neither be the card's best price nor earn value."""
    assert aggregator._net_decimal({"source": "kalshi", "ask": None, "implied_prob": 0.55, "price_decimal": 1.818}) is None
    assert round(aggregator._net_decimal({"source": "kalshi", "ask": 0.55, "price_decimal": 1.818}), 3) == round(1 / (0.55 + 0.07 * 0.55 * 0.45), 3)
    line = aggregator._price_lookup([{"market_type": "moneyline", "event": "A vs B", "commence_time": "2026-10-11",
                                      "selection": "A", "fair_prob": 0.6, "ev": 0.05, "best_source": "kalshi",
                                      "best_price_decimal": 1.818,
                                      "all_quotes": [{"source": "kalshi", "ask": None, "implied_prob": 0.55,
                                                      "price_decimal": 1.818}]}])
    assert line("A vs B", "A", "2026-10-11") == {}
