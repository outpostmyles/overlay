"""Monte Carlo tournament simulation: valid probabilities, stage monotonicity, favorite dominance."""
from backend.model import tournament


def _toy():
    # real WC shape: 12 groups of 4 -> 24 top-2 + 8 best thirds = 32-team knockout (5 clean rounds)
    groups = {g: [f"{g}1", f"{g}2", f"{g}3", f"{g}4"] for g in "abcdefghijkl"}
    groups["a"][0] = "strong"   # one clearly dominant team in group a

    def lambdas(a, b):
        return (2.6 if a == "strong" else 1.0, 2.6 if b == "strong" else 1.0)

    def strength(t):
        return 5.0 if t == "strong" else 1.0

    return tournament.simulate(groups, lambdas, strength, n=600, seed=7)


def test_probabilities_in_range_and_monotonic():
    res = _toy()
    for t, d in res.items():
        for v in d.values():
            assert 0.0 <= v <= 1.0
        # each deeper stage is a subset of the shallower one
        assert d["advance"] >= d["reach_r16"] >= d["reach_qf"] >= d["reach_sf"] >= d["reach_final"] >= d["win_cup"]


def test_favorite_dominates():
    res = _toy()
    assert res["strong"]["win_group"] > 0.5                 # dominant team usually tops its group
    assert res["strong"]["win_cup"] > res["a2"]["win_cup"]  # and wins the cup far more than a weak side
    assert res["strong"]["advance"] > 0.8


def test_group_winner_probs_sum_to_one_per_group():
    groups = {g: [f"{g}1", f"{g}2", f"{g}3", f"{g}4"] for g in "ab"}
    res = tournament.simulate(groups, lambda a, b: (1.3, 1.3), lambda t: 1.0, n=400, seed=3)
    for g in "ab":
        s = sum(res[f"{g}{i}"]["win_group"] for i in range(1, 5))
        assert abs(s - 1.0) < 0.05            # exactly one winner per group


def test_played_results_are_locked_in():
    # a one-group, two-team toy where the underdog has ALREADY thrashed the favorite 5-0.
    groups = {"a": ["fav", "dog"]}

    def lambdas(a, b):                        # model still thinks "fav" is far stronger
        return (3.0 if a == "fav" else 0.4, 3.0 if b == "fav" else 0.4)

    # without conditioning the model favors "fav" to top the group
    fresh = tournament.simulate(groups, lambdas, lambda t: 1.0, n=400, seed=1)
    assert fresh["fav"]["win_group"] > 0.8

    # lock in the played 5-0 upset → "dog" must win the (single-match) group every time
    played = {frozenset(("fav", "dog")): {"fav": 0, "dog": 5}}
    conditioned = tournament.simulate(groups, lambdas, lambda t: 1.0, played=played, n=400, seed=1)
    assert conditioned["dog"]["win_group"] == 1.0
    assert conditioned["fav"]["win_group"] == 0.0


def test_bracket_path_real_draw_and_locked_ties():
    # 4-team fixed draw: slot0(strong) vs slot1(weak), slot2(mid) vs slot3(mid2). _KO_STAGES labels
    # rounds positionally, so round 1 = reach_r16. The strong team beats the weak team it actually faces.
    bracket = ["strong", "weak", "mid", "mid2"]

    def lambdas(a, b):
        return (2.6 if a == "strong" else 1.0, 2.6 if b == "strong" else 1.0)

    res = tournament.simulate({}, lambdas, lambda t: 1.0, bracket=bracket, n=2000, seed=3)
    assert res["strong"]["reach_r16"] > 0.7                 # wins its real first-round tie vs weak
    assert all(0.0 <= res[t]["reach_r16"] <= 1.0 for t in bracket)

    # a decided tie is locked to its real winner regardless of strength
    locked = tournament.simulate({}, lambdas, lambda t: 1.0, bracket=bracket,
                                 ko_played={frozenset(("strong", "weak")): "weak"}, n=400, seed=3)
    assert locked["weak"]["reach_r16"] == 1.0 and locked["strong"]["reach_r16"] == 0.0
