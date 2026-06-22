"""Pure unit tests for backend.engine.edges.

No network, no file IO, no DB. Builds in-memory Market/Selection/Quote objects
and exercises the real engine functions.
"""
from __future__ import annotations

import pytest

from backend.engine import edges
from backend.engine import odds_math as om
from backend.models import Market, Quote, Selection


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def make_quote(source, price_decimal, source_type="prediction_market", mid_prob=None):
    return Quote(
        source=source,
        source_type=source_type,
        price_decimal=price_decimal,
        implied_prob=1.0 / price_decimal,
        mid_prob=mid_prob,
    )


def make_selection(key, label, quotes=None, fair_prob=None, model_prob=None):
    return Selection(
        key=key,
        label=label,
        quotes=quotes or [],
        fair_prob=fair_prob,
        model_prob=model_prob,
    )


def make_market(selections, market_type="moneyline"):
    return Market(
        market_id="m1",
        event="USA vs Australia",
        market_type=market_type,
        selections=selections,
        commence_time="2026-06-20T00:00:00Z",
    )


def prob_quote(source, prob, source_type="prediction_market"):
    """A quote whose mid_prob is `prob`, priced at the fair decimal for that prob."""
    return Quote(
        source=source,
        source_type=source_type,
        price_decimal=1.0 / prob,
        implied_prob=prob,
        mid_prob=prob,
    )


# --------------------------------------------------------------------------- #
# consensus_fair_line
# --------------------------------------------------------------------------- #
class TestConsensusFairLine:
    def _three_way_with_vig(self):
        """3-way market priced by two sharp sources, each with a small vig."""
        # raw probs per source sum to >1 (a vig). De-vigged should sum to ~1.
        sel_home = make_selection("home", "USA", [
            prob_quote("polymarket", 0.58),
            prob_quote("kalshi", 0.57),
        ])
        sel_draw = make_selection("draw", "Draw", [
            prob_quote("polymarket", 0.33),
            prob_quote("kalshi", 0.34),
        ])
        sel_away = make_selection("away", "Australia", [
            prob_quote("polymarket", 0.18),
            prob_quote("kalshi", 0.17),
        ])
        return make_market([sel_home, sel_draw, sel_away])

    def test_three_way_devig_sums_to_one(self):
        market = self._three_way_with_vig()
        edges.consensus_fair_line(market, sharp_sources=("polymarket", "kalshi"))
        total = sum(s.fair_prob for s in market.selections)
        assert total == pytest.approx(1.0, abs=1e-6)
        assert all(0 < s.fair_prob < 1 for s in market.selections)

    def test_three_way_draw_has_a_fair_prob(self):
        market = self._three_way_with_vig()
        edges.consensus_fair_line(market, sharp_sources=("polymarket", "kalshi"))
        draw = next(s for s in market.selections if s.key == "draw")
        assert draw.fair_prob is not None
        assert 0.30 < draw.fair_prob < 0.36

    def test_order_independent_selection_order(self):
        m1 = self._three_way_with_vig()
        m2 = self._three_way_with_vig()
        m2.selections = list(reversed(m2.selections))

        edges.consensus_fair_line(m1, sharp_sources=("polymarket", "kalshi"))
        edges.consensus_fair_line(m2, sharp_sources=("polymarket", "kalshi"))

        by_key_1 = {s.key: s.fair_prob for s in m1.selections}
        by_key_2 = {s.key: s.fair_prob for s in m2.selections}
        for key in by_key_1:
            assert by_key_1[key] == pytest.approx(by_key_2[key])

    def test_order_independent_quote_order(self):
        m1 = self._three_way_with_vig()
        m2 = self._three_way_with_vig()
        for s in m2.selections:
            s.quotes = list(reversed(s.quotes))

        edges.consensus_fair_line(m1, sharp_sources=("polymarket", "kalshi"))
        edges.consensus_fair_line(m2, sharp_sources=("polymarket", "kalshi"))

        by_key_1 = {s.key: s.fair_prob for s in m1.selections}
        by_key_2 = {s.key: s.fair_prob for s in m2.selections}
        for key in by_key_1:
            assert by_key_1[key] == pytest.approx(by_key_2[key])

    def test_idempotent(self):
        market = self._three_way_with_vig()
        edges.consensus_fair_line(market, sharp_sources=("polymarket", "kalshi"))
        first = {s.key: s.fair_prob for s in market.selections}
        # run again - must not accumulate or drift
        edges.consensus_fair_line(market, sharp_sources=("polymarket", "kalshi"))
        second = {s.key: s.fair_prob for s in market.selections}
        for key in first:
            assert first[key] == pytest.approx(second[key])

    def test_sportsbook_excluded_from_fair_line(self):
        # A sportsbook quote with a wild price must not move the fair line.
        sel_home = make_selection("home", "USA", [
            prob_quote("polymarket", 0.55),
            prob_quote("draftkings", 0.90, source_type="sportsbook"),
        ])
        sel_away = make_selection("away", "Australia", [
            prob_quote("polymarket", 0.50),
            prob_quote("draftkings", 0.40, source_type="sportsbook"),
        ])
        market = make_market([sel_home, sel_away])
        edges.consensus_fair_line(market, sharp_sources=("polymarket",))
        # only polymarket counts: raw 0.55/0.50 -> devig power, sums to 1
        total = sum(s.fair_prob for s in market.selections)
        assert total == pytest.approx(1.0, abs=1e-6)
        home = next(s for s in market.selections if s.key == "home")
        # home should be ~just above 0.5, nowhere near 0.9
        assert 0.5 < home.fair_prob < 0.6

    def test_no_sharp_quotes_leaves_fair_prob_none(self):
        sel = make_selection("home", "USA", [
            prob_quote("draftkings", 0.55, source_type="sportsbook"),
        ])
        market = make_market([sel])
        edges.consensus_fair_line(market, sharp_sources=("polymarket", "kalshi"))
        assert market.selections[0].fair_prob is None

    def test_falls_back_to_implied_when_no_mid(self):
        # If mid_prob is None, the function uses implied_prob.
        q = make_quote("polymarket", price_decimal=1.0 / 0.55)  # implied 0.55, mid None
        sel_home = make_selection("home", "USA", [q])
        q2 = make_quote("polymarket", price_decimal=1.0 / 0.50)
        sel_away = make_selection("away", "Australia", [q2])
        market = make_market([sel_home, sel_away])
        edges.consensus_fair_line(market, sharp_sources=("polymarket",))
        assert sum(s.fair_prob for s in market.selections) == pytest.approx(1.0, abs=1e-6)


# --------------------------------------------------------------------------- #
# best_lines + min_fair_prob longshot filter
# --------------------------------------------------------------------------- #
class TestBestLines:
    def test_picks_highest_decimal(self):
        sel = make_selection("home", "USA", [
            make_quote("a", 2.0),
            make_quote("b", 2.4),  # best
            make_quote("c", 1.9),
        ], fair_prob=0.55)
        market = make_market([sel])
        rows = edges.best_lines(market)
        assert len(rows) == 1
        assert rows[0]["best_price_decimal"] == pytest.approx(2.4)
        assert rows[0]["best_source"] == "b"

    def test_ev_computed_above_min_fair_prob(self):
        sel = make_selection("home", "USA", [make_quote("a", 2.0)], fair_prob=0.55)
        market = make_market([sel])
        rows = edges.best_lines(market, min_fair_prob=0.02)
        assert rows[0]["ev"] is not None
        assert rows[0]["ev"] == pytest.approx(om.expected_value(0.55, 2.0), abs=1e-4)

    def test_longshot_filter_drops_ev(self):
        # fair prob below min_fair_prob -> ev must be None (no meaningless edge).
        sel = make_selection("longshot", "Country X", [make_quote("a", 100.0)],
                             fair_prob=0.005)
        market = make_market([sel])
        rows = edges.best_lines(market, min_fair_prob=0.02)
        assert rows[0]["ev"] is None

    def test_selection_without_quotes_skipped(self):
        sel = make_selection("home", "USA", [], fair_prob=0.55)
        market = make_market([sel])
        assert edges.best_lines(market) == []

