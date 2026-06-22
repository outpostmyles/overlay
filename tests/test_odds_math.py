"""Pure unit tests for backend.engine.odds_math.

No network, no file IO, no DB. Tests exercise the real function names and
signatures as they exist in the source module.
"""
from __future__ import annotations

import math

import pytest

from backend.engine import odds_math as om


# --------------------------------------------------------------------------- #
# Conversions: american <-> decimal <-> implied probability
# --------------------------------------------------------------------------- #
class TestAmericanDecimal:
    def test_positive_american_to_decimal(self):
        # +150 means win 150 on 100 staked => 2.5 total return per 1 unit
        assert om.american_to_decimal(150) == pytest.approx(2.5)
        assert om.american_to_decimal(250) == pytest.approx(3.5)

    def test_negative_american_to_decimal(self):
        # -200 means stake 200 to win 100 => 1.5 decimal
        assert om.american_to_decimal(-200) == pytest.approx(1.5)
        assert om.american_to_decimal(-110) == pytest.approx(1.0 + 100.0 / 110.0)

    def test_even_money(self):
        # +100 and -100 are both even money == decimal 2.0
        assert om.american_to_decimal(100) == pytest.approx(2.0)
        assert om.american_to_decimal(-100) == pytest.approx(2.0)

    def test_decimal_to_american_favorite_and_underdog(self):
        # dec >= 2.0 => positive american; dec < 2.0 => negative american
        assert om.decimal_to_american(2.5) == 150
        assert om.decimal_to_american(3.5) == 250
        assert om.decimal_to_american(1.5) == -200

    def test_decimal_to_american_invalid_returns_zero(self):
        # dec <= 1.0 is not a valid payout (you can't lose money on a win)
        assert om.decimal_to_american(1.0) == 0
        assert om.decimal_to_american(0.5) == 0

    @pytest.mark.parametrize("american", [150, -200, 250, -110, 130, -150, 500, -500])
    def test_round_trip_american_decimal(self, american):
        dec = om.american_to_decimal(american)
        assert om.decimal_to_american(dec) == american

    def test_round_trip_even_money_collapses_to_plus(self):
        # +100 and -100 both map to decimal 2.0, which rounds back to +100.
        # This is expected: even-money has a single decimal representation.
        assert om.decimal_to_american(om.american_to_decimal(100)) == 100
        assert om.decimal_to_american(om.american_to_decimal(-100)) == 100


class TestProbabilityConversions:
    @pytest.mark.parametrize("prob", [0.55, 0.30, 0.15, 0.50, 0.80, 0.10])
    def test_decimal_prob_round_trip(self, prob):
        dec = om.prob_to_decimal(prob)
        assert om.decimal_to_prob(dec) == pytest.approx(prob)

    def test_decimal_to_prob(self):
        assert om.decimal_to_prob(2.0) == pytest.approx(0.5)
        assert om.decimal_to_prob(4.0) == pytest.approx(0.25)

    def test_prob_to_decimal(self):
        assert om.prob_to_decimal(0.5) == pytest.approx(2.0)
        assert om.prob_to_decimal(0.25) == pytest.approx(4.0)

    def test_zero_and_negative_inputs_are_safe(self):
        # No division-by-zero crashes; documented sentinel values.
        assert om.decimal_to_prob(0) == 0.0
        assert om.decimal_to_prob(-1) == 0.0
        assert om.prob_to_decimal(0) == float("inf")
        assert om.prob_to_decimal(-0.1) == float("inf")

    def test_prob_to_american(self):
        # 0.5 fair prob -> decimal 2.0 -> +100
        assert om.prob_to_american(0.5) == 100
        # 0.8 fair prob -> decimal 1.25 -> -400
        assert om.prob_to_american(0.8) == om.decimal_to_american(1.25)

    def test_full_chain_american_to_prob(self):
        # +150 -> dec 2.5 -> implied prob 0.4
        dec = om.american_to_decimal(150)
        assert om.decimal_to_prob(dec) == pytest.approx(0.4)


class TestFormatAmerican:
    def test_positive_gets_plus(self):
        assert om.format_american(150) == "+150"

    def test_negative_keeps_sign(self):
        assert om.format_american(-200) == "-200"

    def test_zero_no_plus(self):
        # zero is not > 0, so no leading '+'
        assert om.format_american(0) == "0"


# --------------------------------------------------------------------------- #
# De-vigging
# --------------------------------------------------------------------------- #
class TestDevig:
    def test_multiplicative_sums_to_one(self):
        raw = [0.58, 0.33, 0.18]  # overround ~1.09
        fair = om.devig_multiplicative(raw)
        assert sum(fair) == pytest.approx(1.0)
        assert all(0.0 <= p <= 1.0 for p in fair)

    def test_power_sums_to_one(self):
        raw = [0.58, 0.33, 0.18]
        fair = om.devig_power(raw)
        assert sum(fair) == pytest.approx(1.0, abs=1e-6)
        assert all(0.0 <= p <= 1.0 for p in fair)

    def test_power_preserves_ordering(self):
        raw = [0.58, 0.33, 0.18]
        fair = om.devig_power(raw)
        # favorite stays the favorite, longshot stays the longshot
        assert fair[0] > fair[1] > fair[2]

    def test_multiplicative_no_vig_is_identity(self):
        # raw already sums to 1.0 -> de-vig returns the same probabilities
        raw = [0.55, 0.30, 0.15]
        fair = om.devig_multiplicative(raw)
        assert fair == pytest.approx(raw)

    def test_power_no_vig_is_identity(self):
        # When overround ~1.0 the power method early-returns the raw probs unchanged.
        raw = [0.55, 0.30, 0.15]
        fair = om.devig_power(raw)
        assert fair == pytest.approx(raw)

    def test_power_converges_to_raw_as_vig_shrinks(self):
        # As the overround shrinks toward 1.0, de-vigged ~ raw.
        base = [0.55, 0.30, 0.15]
        small_vig = [p * 1.001 for p in base]  # overround ~0.1%
        fair = om.devig_power(small_vig)
        assert fair == pytest.approx(base, abs=2e-3)

    def test_multiplicative_converges_to_raw_as_vig_shrinks(self):
        base = [0.55, 0.30, 0.15]
        small_vig = [p * 1.001 for p in base]
        fair = om.devig_multiplicative(small_vig)
        assert fair == pytest.approx(base, abs=2e-3)

    def test_devig_dispatch_default_is_power(self):
        raw = [0.58, 0.33, 0.18]
        assert om.devig(raw) == pytest.approx(om.devig_power(raw))

    def test_devig_dispatch_multiplicative(self):
        raw = [0.58, 0.33, 0.18]
        assert om.devig(raw, method="multiplicative") == pytest.approx(
            om.devig_multiplicative(raw)
        )

    def test_devig_dispatch_unknown_method_falls_back_to_power(self):
        raw = [0.58, 0.33, 0.18]
        # any non-'multiplicative' string routes to power
        assert om.devig(raw, method="nonsense") == pytest.approx(om.devig_power(raw))

    def test_two_way_devig(self):
        raw = [0.55, 0.52]  # ~7% overround two-way
        for fn in (om.devig_power, om.devig_multiplicative):
            fair = fn(raw)
            assert sum(fair) == pytest.approx(1.0, abs=1e-6)

    def test_devig_zero_sum_returns_input(self):
        # Degenerate guard: sum<=0 returns the list unchanged (no crash).
        raw = [0.0, 0.0]
        assert om.devig_multiplicative(raw) == raw
        assert om.devig_power(raw) == raw

    def test_power_underround_normalizes_up(self):
        # sum < 1 (negative overround) still normalizes to ~1.0 via power method.
        raw = [0.45, 0.25, 0.10]  # sum 0.8
        fair = om.devig_power(raw)
        assert sum(fair) == pytest.approx(1.0, abs=1e-6)


class TestOverround:
    def test_positive_overround(self):
        assert om.overround([0.58, 0.33, 0.18]) == pytest.approx(0.09)

    def test_zero_overround_at_fair(self):
        assert om.overround([0.55, 0.30, 0.15]) == pytest.approx(0.0)

    def test_negative_overround_when_underround(self):
        assert om.overround([0.45, 0.25, 0.10]) == pytest.approx(-0.2)


# --------------------------------------------------------------------------- #
# Expected value
# --------------------------------------------------------------------------- #
class TestExpectedValue:
    def test_positive_ev_when_price_beats_fair(self):
        # fair prob 0.55, even money (dec 2.0) -> +0.10 EV
        assert om.expected_value(0.55, 2.0) == pytest.approx(0.10)

    def test_zero_ev_at_fair_price(self):
        # fair prob 0.5 at dec 2.0 is exactly break-even
        assert om.expected_value(0.5, 2.0) == pytest.approx(0.0)

    def test_negative_ev_when_price_worse(self):
        # fair prob 0.45 at even money -> -0.10 EV
        assert om.expected_value(0.45, 2.0) == pytest.approx(-0.10)


# --------------------------------------------------------------------------- #
# Kelly staking
# --------------------------------------------------------------------------- #
class TestKelly:
    def test_kelly_fraction_with_edge(self):
        # p=0.6 at even money (b=1): f = (1*0.6 - 0.4)/1 = 0.2
        assert om.kelly_fraction(0.6, 2.0) == pytest.approx(0.2)

    def test_kelly_fraction_no_edge_is_zero(self):
        # p*dec == 1 exactly: no edge -> stake 0
        assert om.kelly_fraction(0.5, 2.0) == pytest.approx(0.0)

    def test_kelly_fraction_negative_edge_clamped_to_zero(self):
        # p=0.4 at even money is -EV; Kelly must not go negative.
        assert om.kelly_fraction(0.4, 2.0) == 0.0

    def test_kelly_fraction_dec_odds_at_or_below_one_is_zero(self):
        # b = dec-1 <= 0 -> guard returns 0, no crash, no division by zero.
        assert om.kelly_fraction(0.9, 1.0) == 0.0
        assert om.kelly_fraction(0.9, 0.5) == 0.0

    def test_kelly_fraction_prob_zero(self):
        assert om.kelly_fraction(0.0, 3.0) == 0.0

    def test_kelly_stake_applies_fraction(self):
        # full kelly fraction 0.2, bankroll 1000, quarter kelly -> 1000*0.25*0.2 = 50
        assert om.kelly_stake(0.6, 2.0, 1000.0, fraction=0.25) == pytest.approx(50.0)

    def test_kelly_stake_default_fraction_is_quarter(self):
        # default fraction is 0.25
        assert om.kelly_stake(0.6, 2.0, 1000.0) == pytest.approx(50.0)

    def test_kelly_stake_full_kelly(self):
        assert om.kelly_stake(0.6, 2.0, 1000.0, fraction=1.0) == pytest.approx(200.0)

    def test_kelly_stake_no_edge_is_zero(self):
        assert om.kelly_stake(0.4, 2.0, 1000.0, 0.25) == 0.0

    def test_kelly_stake_never_negative(self):
        # negative edge / bad odds must produce a non-negative stake.
        for prob, dec in [(0.4, 2.0), (0.3, 1.5), (0.9, 1.0), (0.5, 0.8)]:
            assert om.kelly_stake(prob, dec, 1000.0, 0.25) >= 0.0

    def test_kelly_stake_rounded_to_cents(self):
        stake = om.kelly_stake(0.55, 2.05, 1234.0, 0.3)
        assert stake == round(stake, 2)

    def test_kelly_fraction_monotonic_in_prob(self):
        # higher true prob (more edge) at fixed odds -> larger Kelly fraction
        f1 = om.kelly_fraction(0.55, 2.0)
        f2 = om.kelly_fraction(0.65, 2.0)
        f3 = om.kelly_fraction(0.75, 2.0)
        assert f1 < f2 < f3
