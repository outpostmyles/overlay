"""Kalshi thin books. A game's main total and spread come only from tight rungs (both sides quoted, at most
FORECAST_MAX_LINE_WIDTH wide) that agree with each other, and a one-cent moneyline book (a 99c favorite)
is a real game on the ledger, as it already was on Best Bets. Ladders and books are the exact ones Kalshi
returned live on 2026-10-06."""
import asyncio
from datetime import datetime, timezone

import pytest

from backend import aggregator, config, picks, sports
from backend.engine import edges
from backend.models import Market, Quote, Selection
from backend.sources import kalshi


def _m(ticker, sub, bid, ask, last="0.0000", strike=None, title=None):
    return {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "title": title or f"{sub} wins",
            "yes_sub_title": sub, "yes_bid_dollars": bid, "yes_ask_dollars": ask,
            "last_price_dollars": last, "floor_strike": strike, "volume_fp": "0.00"}


def _ladder(event, rungs):
    """NHL total rungs as Kalshi lists them: KXNHLTOTAL-...-4 is "Over 3.5 goals scored"."""
    return [_m(f"{event}-{int(line + 0.5)}", f"Over {line} goals scored", bid, ask, strike=line,
               title=f"Full Game: Over {line} goals scored") for line, bid, ask in rungs]


def _priced(monkeypatch, sport, raw):
    """The live path up to the ledger: parse, merge, de-vig."""
    monkeypatch.setattr(config, "SPORT", sport)

    async def fake_series(client, series, status="open"):
        return raw.get(series, [])

    monkeypatch.setattr(kalshi, "_fetch_series", fake_series)
    mk = aggregator._merge(asyncio.run(kalshi.fetch(None)))
    for m in mk:
        if m.market_type in aggregator._PRICED_TYPES:
            edges.consensus_fair_line(m, config.SHARP_SOURCES, config.DEVIG_METHOD)
    return mk


# two days out: almost nothing quoted. "Nearest a coin flip" picked 3.5 off its 0.11/0.95 book (53%).
VANCAR_OCT08 = [(1.5, "0.7700", "0.9700"), (2.5, "0.7600", "0.9500"), (3.5, "0.1100", "0.9500"),
                (4.5, "0.7600", "0.9500"), (5.5, "0.4800", "0.9400"), (6.5, "0.4700", "0.9400"),
                (7.5, "0.0400", "0.8800"), (8.5, "0.0300", "0.8500"), (9.5, "0.0200", "0.8400")]
# the same night, one market maker on the 4.5 rung and nobody else
MINTB_OCT08 = [(1.5, "0.3000", "0.9700"), (2.5, "0.1900", "0.9500"), (3.5, "0.1100", "0.7700"),
               (4.5, "0.7500", "0.8000"), (5.5, "0.2000", "0.9400"), (6.5, "0.3600", "0.5100"),
               (7.5, "0.1800", "0.3500"), (8.5, "0.0200", "0.8400"), (9.5, "0.0200", "0.8400")]
# game day: every rung a cent wide
CARMTL_OCT06 = [(1.5, "0.9800", "0.9900"), (2.5, "0.9600", "0.9700"), (3.5, "0.8600", "0.8800"),
                (4.5, "0.7900", "0.8000"), (5.5, "0.5700", "0.5800"), (6.5, "0.4500", "0.4600"),
                (7.5, "0.2500", "0.2600"), (8.5, "0.1700", "0.1800"), (9.5, "0.0700", "0.0800")]


def test_book_width_reads_an_empty_side_as_its_edge():
    assert kalshi.book_width(0.99, None) == 0.01        # Ohio State: bid 0.99, ask 1.00 (no sellers)
    assert kalshi.book_width(None, 0.01) == 0.01        # Maryland: bid 0.00 (no buyers), ask 0.01
    assert kalshi.book_width(None, 0.30) == 0.30        # no buyers under a 30c ask is thin, not tight
    assert kalshi.book_width(None, None) == 1.0         # nothing quoted at all
    assert kalshi.book_width(0.45, 0.55) == 0.10        # exactly 10c, not 0.10000000000000009


def test_rungs_carry_their_book_and_the_join_carries_its_width(monkeypatch):
    raw = {"KXNHLGAME": [_m("KXNHLGAME-26OCT08VANCAR-VAN", "Vancouver", "0.2400", "0.2700", "0.2800"),
                         _m("KXNHLGAME-26OCT08VANCAR-CAR", "Carolina", "0.7200", "0.7500", "0.7100")],
           "KXNHLTOTAL": _ladder("KXNHLTOTAL-26OCT08VANCAR", [(3.5, "0.1100", "0.9500")])}
    tot = next(m for m in _priced(monkeypatch, "nhl", raw) if m.market_type == "total")
    over, under = (s.quotes[0] for s in tot.selections)
    assert (over.bid, over.ask) == (0.11, 0.95)
    assert (under.bid, under.ask) == (pytest.approx(0.05), pytest.approx(0.89))   # the same book, mirrored
    (rungs,) = aggregator._totals_by_game([tot]).values()
    assert rungs == [(3.5, pytest.approx(0.53), 0.84)]


def test_nhl_main_total_comes_only_from_tight_rungs(monkeypatch):
    raw = {"KXNHLGAME": [_m("KXNHLGAME-26OCT08VANCAR-VAN", "Vancouver", "0.2400", "0.2700", "0.2800"),
                         _m("KXNHLGAME-26OCT08VANCAR-CAR", "Carolina", "0.7200", "0.7500", "0.7100"),
                         _m("KXNHLGAME-26OCT08MINTB-MIN", "Minnesota", "0.4400", "0.4800", "0.4700"),
                         _m("KXNHLGAME-26OCT08MINTB-TB", "Tampa Bay", "0.5200", "0.5500", "0.5600"),
                         _m("KXNHLGAME-26OCT06CARMTL-CAR", "Carolina", "0.5400", "0.5500", "0.5500"),
                         _m("KXNHLGAME-26OCT06CARMTL-MTL", "Montreal", "0.4500", "0.4600", "0.4500")],
           "KXNHLTOTAL": (_ladder("KXNHLTOTAL-26OCT08VANCAR", VANCAR_OCT08)
                          + _ladder("KXNHLTOTAL-26OCT08MINTB", MINTB_OCT08)
                          + _ladder("KXNHLTOTAL-26OCT06CARMTL", CARMTL_OCT06))}
    mk = _priced(monkeypatch, "nhl", raw)
    kicks = {(frozenset((a, b)), d): f"{d}T23:00Z" for a, b, d in (
        ("vancouver canucks", "carolina hurricanes", "2026-10-08"),
        ("minnesota wild", "tampa bay lightning", "2026-10-08"),
        ("carolina hurricanes", "montreal canadiens", "2026-10-06"))}
    board = aggregator._forecast_board(mk, None, kicks, 75, datetime(2026, 10, 6, 12, tzinfo=timezone.utc),
                                       totals=aggregator._totals_by_game(mk))[1]

    def total(key):
        legs = [l for l in board[key]["legs"] if l["key"] == "total_goals"]
        return (legs[0]["side"], legs[0]["line"], legs[0]["prob"]) if legs else None

    assert len(board) == 3                                               # every game still on the board
    assert total("fc|2026-10-08|carolina hurricanes|vancouver canucks") is None   # no tight rung: no leg
    # its one tight rung is real but 78%: a side bet, not the game's total, so no line until the ladder fills
    assert total("fc|2026-10-08|minnesota wild|tampa bay lightning") is None
    assert total("fc|2026-10-06|carolina hurricanes|montreal canadiens") == ("under", 6.5, 0.545)   # unchanged


def test_main_line_sets_aside_a_tight_rung_out_of_order():
    """A stale 8.5 rung at 49% is the closest to a coin flip, but the over cannot be likelier at 8.5 than at
    7.5: the ordered run of the other four is kept, and 6.5 is the line."""
    ladder = [(4.5, 0.80, 0.01), (5.5, 0.58, 0.01), (6.5, 0.45, 0.01), (7.5, 0.25, 0.01), (8.5, 0.49, 0.02)]
    assert aggregator._main_line([(t, p, w, t) for t, p, w in ladder]) == 6.5


def test_main_line_width_gate_and_ties():
    gate = config.FORECAST_MAX_LINE_WIDTH
    assert aggregator._main_line([(5.5, 0.50, gate + 0.01, "thin"), (6.5, 0.40, gate, "edge")]) == "edge"
    assert aggregator._main_line([(5.5, 0.50, None, "one side empty")]) is None
    assert aggregator._main_line([]) is None
    # equally near a coin flip: the feed's order decides, as it did before (no churn on a tight ladder)
    assert aggregator._main_line([(40.5, 0.475, 0.01, 40.5), (39.5, 0.525, 0.01, 39.5)]) == 40.5
    assert aggregator._main_line([(39.5, 0.525, 0.01, 39.5), (40.5, 0.475, 0.01, 40.5)]) == 39.5
    # a flat stretch (two rungs at the same price) is still in order
    assert aggregator._main_line([(51.5, 0.485, 0.01, 51.5), (52.5, 0.485, 0.01, 52.5)]) == 51.5


def _spread_leg(monkeypatch, rungs):
    monkeypatch.setattr(config, "SPORT", "nfl")
    dal, tb = "dallas cowboys", "tampa bay buccaneers"
    pair = frozenset((dal, tb))

    def sel(k, p):
        return Selection(key=k, label=k, fair_prob=p, quotes=[Quote(
            source="kalshi", source_type="prediction_market", price_decimal=1 / p, implied_prob=p,
            mid_prob=p, bid=round(p - 0.005, 3), ask=round(p + 0.005, 3))])
    ml = Market(market_id="kalshi:tbdal", event="Tampa Bay vs Dallas", market_type="moneyline",
                selections=[sel(dal, 0.805), sel(tb, 0.195)], commence_time="2026-10-08")
    board = aggregator._forecast_board([ml], None, {(pair, "2026-10-08"): "2026-10-09T00:15Z"}, 75,
                                       datetime(2026, 10, 8, 12, tzinfo=timezone.utc),
                                       spreads={(pair, "2026-10-08"): rungs})[1]
    leg = next((l for l in next(iter(board.values()))["legs"] if l["key"] == "spread"), None)
    return leg and (leg["team"], leg["line"], leg["side"])


def test_spread_ladder_spans_both_teams_and_skips_thin_and_stale_rungs(monkeypatch):
    dal, tb = "dallas cowboys", "tampa bay buccaneers"
    live = [(dal, 3.5, 0.68, 0.01), (dal, 7.5, 0.515, 0.01), (dal, 10.5, 0.42, 0.01), (tb, 1.5, 0.175, 0.01)]
    assert _spread_leg(monkeypatch, live) == (dal, 7.5, "cover")
    # a thin rung at an exact coin flip never sets the line
    assert _spread_leg(monkeypatch, live + [(dal, 6.5, 0.50, 0.30)]) == (dal, 7.5, "cover")
    # "Tampa Bay wins by over 6.5" at 49% says Dallas's margin tops -6.5 only 51% of the time, against 82.5%
    # for topping -1.5 on Tampa Bay's own 1.5 rung: out of order in Dallas's margin, so it is set aside
    assert _spread_leg(monkeypatch, live + [(tb, 6.5, 0.49, 0.02)]) == (dal, 7.5, "cover")
    assert _spread_leg(monkeypatch, [(dal, 7.5, 0.515, 0.25)]) is None   # no tight rung: no spread leg


# Ohio State v Maryland as Kalshi listed it: every contract a cent from its edge
MDOSU = [_m("KXNCAAFGAME-26OCT10MDOSU-OSU", "Ohio St.", "0.9900", "1.0000", "0.9900"),
         _m("KXNCAAFGAME-26OCT10MDOSU-MD", "Maryland", "0.0000", "0.0100", "0.0100")]


def _cfb_board(monkeypatch, raw):
    mk = _priced(monkeypatch, "cfb", raw)
    kicks = {(frozenset(("maryland", "ohio state")), "2026-10-10"): "2026-10-10T19:30Z"}
    now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    return mk, aggregator._forecast_board(mk, None, kicks, 75, now)[1]


def test_one_cent_book_is_a_game_on_the_ledger_and_on_best_bets(monkeypatch):
    mk, board = _cfb_board(monkeypatch, {"KXNCAAFGAME": MDOSU})
    (key, entry), = board.items()                        # was skipped: neither side had both bid and ask
    assert key == "fc|2026-10-10|maryland|ohio state"
    assert entry["market"][2] == pytest.approx(0.99)
    assert entry["kalshi_ask"] == {"maryland": 0.01}     # nobody sells Ohio State under $1
    monkeypatch.setattr(picks, "_days_out", lambda ct: 0)
    favs = picks.generate(mk, [], None, config, {})["favorite_ml"]
    assert [f["team_key"] for f in favs] == ["ohio state"]   # the same game on both paths


def test_one_sided_thin_books_still_wait(monkeypatch):
    """Reading an empty side as its edge keeps one-cent books, not thin ones: an Ohio State book with no
    sellers under $1 and a top bid of 0.80 is 20c wide, and stays off the ledger like any 20c book."""
    thin = [_m("KXNCAAFGAME-26OCT10MDOSU-OSU", "Ohio St.", "0.8000", "1.0000", "0.9000"),
            _m("KXNCAAFGAME-26OCT10MDOSU-MD", "Maryland", "0.0000", "0.1200", "0.1000")]
    assert _cfb_board(monkeypatch, {"KXNCAAFGAME": thin})[1] == {}


def test_nhl_moneyline_gate_keeps_ten_cent_books(monkeypatch):
    """The NHL gate is college football's 10c. Live two days out the widest books were exactly 10c
    (Buffalo 0.45/0.55), which floating point measured as 0.10000000000000009 and would have dropped."""
    assert sports.get("nhl").max_lock_spread == 0.10
    raw = {"KXNHLGAME": [_m("KXNHLGAME-26OCT12FLABUF-BUF", "Buffalo", "0.4500", "0.5500"),
                         _m("KXNHLGAME-26OCT12FLABUF-FLA", "Florida", "0.4700", "0.5300", "0.5300")]}
    mk = _priced(monkeypatch, "nhl", raw)
    kicks = {(frozenset(("buffalo sabres", "florida panthers")), "2026-10-12"): "2026-10-12T23:00Z"}
    now = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
    assert len(aggregator._forecast_board(mk, None, kicks, 75, now)[1]) == 1
    raw["KXNHLGAME"][0]["yes_bid_dollars"] = "0.4400"                # 11c: not a sharp line yet
    mk = _priced(monkeypatch, "nhl", raw)
    assert aggregator._forecast_board(mk, None, kicks, 75, now)[1] == {}


def test_a_half_cent_inversion_is_noise_not_a_stale_rung():
    """Tight live ladders carry tiny inversions (a 1.5 spread at 0.57 next to 2.5 at 0.575). Read as
    staleness, one at the coin-flip rung would jump the line: here it must stay 45.5."""
    ladder = [(44.5, 0.60, 0.02, 44.5), (45.5, 0.505, 0.02, 45.5), (46.5, 0.51, 0.02, 46.5), (47.5, 0.40, 0.02, 47.5)]
    assert aggregator._main_line(ladder) == 45.5
    assert aggregator._main_line([(1.5, 0.975, 0.01, 1.5)]) is None      # far from a coin flip: no main line
