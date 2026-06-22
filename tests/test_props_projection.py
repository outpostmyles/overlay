"""Opponent-adjusted prop projection (model/props.py): the opponent/possession/minutes adjustments
and measured-rate shrinkage that turn a raw average into a real projection."""
from backend.model import props


def test_shots_scale_with_opponent_xg():
    # a striker faces a weak defence (high team xG) vs a strong one (low) — more shots vs the weak team
    weak = props.price("Shots", 1.5, team_xg=2.7, position="Attacker")
    strong = props.price("Shots", 1.5, team_xg=0.9, position="Attacker")
    assert weak["prob"] > strong["prob"]


def test_passes_scale_with_possession():
    # the user's example: a midfielder gets more passes when his team will dominate the ball.
    # passes only price once we have a measured rate (coarse prior can't tell roles apart).
    m = {"passes90": 75.0, "games": 4}
    dominant = props.price("Passes Attempted", 60.5, possession=0.66, position="Midfielder", measured=m)
    pressed = props.price("Passes Attempted", 60.5, possession=0.40, position="Midfielder", measured=m)
    assert dominant["prob"] > pressed["prob"]


def test_passes_unpriced_without_measured_data():
    assert props.price("Passes Attempted", 60.5, possession=0.66, position="Midfielder") is None


def test_measured_rate_lifts_a_high_volume_player():
    base = props.price("Shots", 1.5, team_xg=1.35, position="Attacker")
    sharp = props.price("Shots", 1.5, team_xg=1.35, position="Attacker",
                        measured={"shots90": 5.0, "games": 6})
    assert sharp["prob"] > base["prob"]


def test_minutes_reduce_projection():
    full = props.price("Shots", 1.5, team_xg=2.0, position="Attacker", exp_minutes=90)
    half = props.price("Shots", 1.5, team_xg=2.0, position="Attacker", exp_minutes=45)
    assert half["prob"] < full["prob"]


def test_defender_shots_stay_low():
    d = props.price("Shots", 1.5, team_xg=2.0, position="Defender")
    assert d["value"] in ("fade", "none") and d["prob"] < 0.5


def test_goals_scale_with_xg_and_unpriceable_returns_none():
    hi = props.price("Goals", 0.5, team_xg=2.6, position="Attacker")
    lo = props.price("Goals", 0.5, team_xg=0.9, position="Attacker")
    assert hi["prob"] > lo["prob"]
    assert props.price("Tackles", 1.5, team_xg=2.0, position="Midfielder") is None
