"""Kalshi rewords its prose titles freely; the ticker structure is what stays put. These fixtures are
the exact shapes returned live, across three wordings, run through the real parser. Each of the August
and September 2026 rewordings silently zeroed a ledger leg (totals from Aug 19, first-five Sep 14-28)
until the joins moved onto ticker codes."""
import asyncio

from backend import config
from backend.sources import kalshi


def _m(ticker, title, sub, bid="0.4800", ask="0.4900", strike=None):
    return {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "title": title,
            "yes_sub_title": sub, "yes_bid_dollars": bid, "yes_ask_dollars": ask,
            "last_price_dollars": ask, "floor_strike": strike, "volume_fp": "100"}


def _run(monkeypatch, sport, raw_by_series):
    monkeypatch.setattr(config, "SPORT", sport)

    async def fake_series(client, series, status="open"):
        return raw_by_series.get(series, [])

    monkeypatch.setattr(kalshi, "_fetch_series", fake_series)
    return asyncio.run(kalshi.fetch(None))


def test_nhl_formats_join_on_ticker_codes(monkeypatch):
    raw = {
        "KXNHLGAME": [_m("KXNHLGAME-26OCT06CARMTL-CAR", "Carolina wins", "Carolina", "0.5200", "0.5300"),
                      _m("KXNHLGAME-26OCT06CARMTL-MTL", "Montreal wins", "Montreal", "0.4700", "0.4800")],
        "KXNHLTOTAL": [_m("KXNHLTOTAL-26OCT06CARMTL-7", "Full Game: Over 6.5 goals scored",
                          "Over 6.5 goals scored", "0.4700", "0.4800", 6.5)],
    }
    mk = _run(monkeypatch, "nhl", raw)
    ml = next(m for m in mk if m.market_type == "moneyline")
    assert ml.event == "Carolina vs Montreal"            # rebuilt from the ticker, not "Montreal wins"
    assert ml.commence_time == "2026-10-06"              # NHL tickers carry no start time
    assert {s.key for s in ml.selections} == {"carolina hurricanes", "montreal canadiens"}
    tot = next(m for m in mk if m.market_type == "total")
    assert tot.group == "Lines|carolina hurricanes|montreal canadiens"


def test_mlb_august_rewording_no_longer_drops_totals(monkeypatch):
    """Post-Aug-19 shape: the total's title is just 'Over 8.5 runs scored', so the old title regex found
    no game; the ticker still names it."""
    from backend.aggregator import _totals_by_game
    raw = {
        "KXMLBGAME": [_m("KXMLBGAME-26OCT072000TBNYY-TB", "Tampa Bay wins", "Tampa Bay", "0.4200", "0.4300"),
                      _m("KXMLBGAME-26OCT072000TBNYY-NYY", "New York Y wins", "New York Y", "0.5700", "0.5800")],
        "KXMLBTOTAL": [_m("KXMLBTOTAL-26OCT072000TBNYY-8", "Over 8.5 runs scored", "Over 8.5 runs scored",
                          "0.4900", "0.5000", 8.5)],
    }
    mk = _run(monkeypatch, "mlb", raw)
    from backend.engine import edges
    for m in mk:                                          # the de-vig step the aggregator runs next
        edges.consensus_fair_line(m, config.SHARP_SOURCES, config.DEVIG_METHOD)
    ml = next(m for m in mk if m.market_type == "moneyline")
    assert ml.event == "Tampa Bay vs New York Y"
    assert ml.commence_time == "2026-10-07T20:00"        # MLB tickers keep their start time
    joined = _totals_by_game(mk)
    pair = frozenset(("tampa bay rays", "new york yankees"))
    assert list(joined) == [(pair, "2026-10-07T20:00")]  # same key the moneyline looks up
    assert joined[(pair, "2026-10-07T20:00")][0][0] == 8.5


def test_mlb_first_five_tie_survives_every_wording(monkeypatch):
    """'Tie' (Aug, Oct) and 'Tie first 5 innings' (Sep 14-28) must both land on the draw leg."""
    for tie_sub, team_sub in (("Tie", "Miami wins first 5 innings"),
                              ("Tie first 5 innings", "Miami wins first 5 innings"),
                              ("Tie", "Miami")):
        raw = {"KXMLBGAME": [_m("KXMLBGAME-26SEP152140MIAAZ-MIA", "Miami wins", "Miami"),
                             _m("KXMLBGAME-26SEP152140MIAAZ-AZ", "Arizona wins", "Arizona")],
               "KXMLBF5": [_m("KXMLBF5-26SEP152140MIAAZ-MIA", "Miami first 5 innings winner", team_sub,
                              "0.4200", "0.4400"),
                           _m("KXMLBF5-26SEP152140MIAAZ-AZ", "Arizona first 5 innings winner",
                              team_sub.replace("Miami", "Arizona"), "0.3800", "0.4000"),
                           _m("KXMLBF5-26SEP152140MIAAZ-TIE", "first 5 innings tie", tie_sub,
                              "0.1600", "0.1800")]}
        f5 = next(m for m in _run(monkeypatch, "mlb", raw) if m.market_type == "f5_moneyline")
        assert {s.key for s in f5.selections} == {"miami marlins", "arizona diamondbacks", "draw"}, tie_sub


def test_matchup_name_refuses_to_guess():
    kids = [{"ticker": "KXNHLGAME-26OCT06CARMTL-CAR", "yes_sub_title": "Carolina"},
            {"ticker": "KXNHLGAME-26OCT06CARMTL-MTL", "yes_sub_title": "Montreal"}]
    assert kalshi._matchup_name("KXNHLGAME-26OCT06CARMTL", kids) == "Carolina vs Montreal"
    assert kalshi._matchup_name("KXNHLGAME-26OCT06MTLCAR", kids) == "Montreal vs Carolina"
    assert kalshi._matchup_name("KXNHLGAME-26OCT06XXXYYY", kids) is None   # codes don't fit: no guess


def test_prop_pair_reads_tickers_with_and_without_a_start_time():
    codes = {"CAR": "carolina hurricanes", "MTL": "montreal canadiens",
             "CLE": "cleveland guardians", "MIA": "miami marlins"}
    assert kalshi._prop_pair("KXNHLTOTAL-26OCT06CARMTL", codes) == ("carolina hurricanes", "montreal canadiens")
    assert kalshi._prop_pair("KXMLBHIT-26JUL111610CLEMIA", codes) == ("cleveland guardians", "miami marlins")


def test_silent_join_watchdog_logs_breaks_once_and_recovery(monkeypatch, capsys):
    from backend import aggregator
    from backend.models import Market
    monkeypatch.setattr(aggregator, "_join_state", {})
    listed = [Market(market_id="kalshi:x", event="Over 8.5 runs scored", market_type="total",
                     selections=[], commence_time="2026-08-20T19:05", group="Lines")]
    aggregator._warn_silent_joins(listed, {"total": {}})
    aggregator._warn_silent_joins(listed, {"total": {}})            # still broken: no repeat
    aggregator._warn_silent_joins(listed, {"total": {("k",): [1]}})  # recovered
    out = capsys.readouterr().out
    assert out.count("WARNING") == 1 and "1 total markets listed but 0 joined" in out
    assert "total joins recovered: 1 game(s)" in out
    aggregator._warn_silent_joins([], {"total": {}})                # nothing listed: never a warning
    assert "WARNING" not in capsys.readouterr().out


def test_every_parsed_market_type_gets_a_fair_price():
    """A type the parser emits but the de-vig step skips has no fair price, so it joins no game, and
    nothing errors. Spreads shipped that way for one smoke-test run before the watchdog caught it."""
    from backend import aggregator
    emitted = set(kalshi._PER_LINE_TYPES) | {"moneyline", "f5_moneyline"}
    assert emitted <= set(aggregator._PRICED_TYPES), emitted - set(aggregator._PRICED_TYPES)
