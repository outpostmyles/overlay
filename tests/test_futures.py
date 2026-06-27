"""Knockout-futures helpers: group reconstruction from results, field validation, de-vigging."""
import itertools

from backend import aggregator


def _round_robin(group, date="20260620"):
    """All six intra-group games as finished 1-0 results (first-listed team wins)."""
    return [{"date": date, "goals": {a: 1, b: 0}} for a, b in itertools.combinations(group, 2)]


def _twelve_groups():
    return {f"grp{i}": [f"t{i}{j}" for j in range(4)] for i in range(12)}


def _all_group_games(field):
    out = []
    for teams in field.values():
        out += _round_robin(teams)
    return out


def test_reconstruct_two_complete_groups():
    A = ["a1", "a2", "a3", "a4"]
    B = ["b1", "b2", "b3", "b4"]
    results = _round_robin(A) + _round_robin(B)
    groups = aggregator._reconstruct_groups_from_results(results)
    comps = sorted(sorted(v) for v in groups.values())
    assert comps == sorted([sorted(A), sorted(B)])


def test_knockout_game_does_not_merge_groups():
    A = ["a1", "a2", "a3", "a4"]
    B = ["b1", "b2", "b3", "b4"]
    # both groups fully played (so each team already has its three group opponents), then a cross-group
    # knockout game - it is each side's 4th game, so it is not a first-three opponent and forms no edge
    results = _round_robin(A) + _round_robin(B)
    results.append({"date": "20260701", "goals": {"a1": 2, "b1": 1}})
    groups = aggregator._reconstruct_groups_from_results(results)
    assert len(groups) == 2
    comps = sorted(sorted(v) for v in groups.values())
    assert comps == sorted([sorted(A), sorted(B)])   # a1 and b1 stay in separate groups


def test_in_progress_group_still_reconstructs():
    # a group after two matchdays: every pair has NOT played, but the four are already one component
    A = ["a1", "a2", "a3", "a4"]
    results = [{"date": "20260620", "goals": {"a1": 1, "a2": 0}},
               {"date": "20260620", "goals": {"a3": 1, "a4": 0}},
               {"date": "20260624", "goals": {"a1": 1, "a3": 0}},
               {"date": "20260624", "goals": {"a2": 1, "a4": 0}}]
    groups = aggregator._reconstruct_groups_from_results(results)
    assert [sorted(v) for v in groups.values()] == [sorted(A)]


def test_incomplete_components_are_dropped():
    # only a partial pairing (two teams) -> not a size-4 group yet
    results = [{"date": "20260620", "goals": {"x": 1, "y": 0}}]
    assert aggregator._reconstruct_groups_from_results(results) == {}
    assert aggregator._reconstruct_groups_from_results(None) == {}


def test_valid_field_rejects_bad_shapes():
    assert aggregator._valid_field(_twelve_groups())
    assert not aggregator._valid_field({f"g{i}": ["a", "b", "c", "d"] for i in range(11)})  # only 11
    assert not aggregator._valid_field({"a": ["x", "y", "z"]})       # old letter-keyed 3-team cache
    assert not aggregator._valid_field({})


def test_field_complete_requires_full_round_robin():
    field = _twelve_groups()
    games = _all_group_games(field)
    assert aggregator._field_complete(field, games)
    assert not aggregator._field_complete(field, games[1:])          # one pairing missing
    # and a complete field round-trips back through reconstruction
    rec = aggregator._reconstruct_groups_from_results(games)
    assert sorted(sorted(v) for v in rec.values()) == sorted(sorted(v) for v in field.values())


class _Q:
    def __init__(self, p):
        self.source = "polymarket"; self.mid_prob = p; self.implied_prob = p


class _Sel:
    def __init__(self, key, p):
        self.key = key; self.quotes = [_Q(p)]


class _Mkt:
    def __init__(self, pairs):
        self.selections = [_Sel(k, p) for k, p in pairs]


def test_devig_scales_to_slot_count():
    m = _Mkt([("france", 0.55), ("spain", 0.33), ("brazil", 0.22)])     # vigged, sums to 1.10
    teams = {"france", "spain", "brazil"}
    fair = aggregator._devig_market(m, 1, teams)
    assert abs(sum(fair.values()) - 1.0) < 1e-9
    assert fair["france"] > fair["spain"] > fair["brazil"]
    fair4 = aggregator._devig_market(m, 4, teams)                       # 4 semi-finalist slots
    assert abs(sum(fair4.values()) - 4.0) < 1e-9


def test_devig_excludes_placeholder_selections():
    # a stray placeholder must not absorb part of the slot budget meant for real teams
    m = _Mkt([("france", 0.55), ("spain", 0.33), ("tbd", 0.50)])
    fair = aggregator._devig_market(m, 1, {"france", "spain"})
    assert "tbd" not in fair
    assert abs(sum(fair.values()) - 1.0) < 1e-9
    assert abs(fair["france"] - 0.55 / 0.88) < 1e-9                     # normalized over real teams only
