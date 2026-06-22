"""Corner-kick projection (model/corners.py), the team-stats parser, and total-corners settlement."""
import importlib
import json
import os
import tempfile
import time

from backend import config
from backend.model import corners


def test_over_prob_monotonic():
    # a higher projected total lifts P(over a fixed line); a higher line lowers it; stays a probability
    assert corners.over_prob(11, 9.5) > corners.over_prob(8, 9.5)
    assert corners.over_prob(10, 8.5) > corners.over_prob(10, 12.5)
    assert 0.0 <= corners.over_prob(10, 9.5) <= 1.0


def test_project_team_opponent_leakiness():
    # the user's thesis: same attack wins MORE corners against a team that concedes more corners
    rates = {
        "spain": {"cf": 7.0, "ca": 3.0, "games": 5},
        "leaky": {"cf": 4.0, "ca": 8.0, "games": 5},
        "stingy": {"cf": 4.0, "ca": 2.0, "games": 5},
    }
    assert corners.project_team("spain", "leaky", rates) > corners.project_team("spain", "stingy", rates)


def test_possession_lifts_projection():
    rates = {"a": {"cf": 6.0, "ca": 4.0, "games": 4}, "b": {"cf": 5.0, "ca": 5.0, "games": 4}}
    assert corners.project_team("a", "b", rates, possession=0.65) > \
           corners.project_team("a", "b", rates, possession=0.40)


def test_unknown_team_falls_back_to_prior():
    la, lb, total = corners.project_total("x", "y", {})   # no history → league baseline (~10 total)
    assert 9.0 <= total <= 11.0
    assert corners.confidence(0, 0) == "prior"


def test_confidence_tiers():
    assert corners.confidence(5, 4) == "high"
    assert corners.confidence(2, 1) == "medium"
    assert corners.confidence(3, 0) == "prior"   # one team unseen → projection-only


def test_build_team_corner_rates(monkeypatch, tmp_path):
    cache = {"teamstats": {
        "1": {"spain": {"corners": 8}, "morocco": {"corners": 2}},
        "2": {"spain": {"corners": 6}, "france": {"corners": 4}},
    }}
    p = tmp_path / "af.json"
    p.write_text(json.dumps(cache))
    monkeypatch.setattr(config, "APIFOOTBALL_CACHE_PATH", p)
    rates = corners.build_team_corner_rates()
    assert rates["spain"]["games"] == 2
    assert rates["spain"]["cf"] == 7.0    # (8 + 6) / 2 corners won
    assert rates["spain"]["ca"] == 3.0    # (2 + 4) / 2 corners conceded


def test_parse_team_stats_unposted_is_miss():
    from backend.sources import apifootball
    # both teams present but stats not posted yet (empty arrays) → miss ({}) so it doesn't cache zeros
    body = {"response": [{"team": {"name": "Germany"}, "statistics": []},
                         {"team": {"name": "Scotland"}, "statistics": []}]}
    assert apifootball._parse_team_stats(body) == {}
    # a genuine 0-corner reading is still kept (corners present with value 0)
    body2 = {"response": [
        {"team": {"name": "Germany"}, "statistics": [{"type": "Corner Kicks", "value": 0}]},
        {"team": {"name": "Scotland"}, "statistics": [{"type": "Corner Kicks", "value": 0}]}]}
    out = apifootball._parse_team_stats(body2)
    assert len(out) == 2 and out["germany"]["corners"] == 0


def test_parse_team_stats():
    from backend.sources import apifootball
    body = {"response": [
        {"team": {"name": "Germany"}, "statistics": [
            {"type": "Corner Kicks", "value": 8}, {"type": "Ball Possession", "value": "59%"},
            {"type": "Total Shots", "value": 16}, {"type": "Shots on Goal", "value": 7}]},
        {"team": {"name": "Scotland"}, "statistics": [
            {"type": "Corner Kicks", "value": 3}, {"type": "Ball Possession", "value": "41%"}]},
    ]}
    out = apifootball._parse_team_stats(body)
    assert out["germany"] == {"corners": 8, "possession": 0.59, "shots": 16, "sot": 7}
    assert out["scotland"]["corners"] == 3


def test_corner_paper_row_dedup_and_stake():
    from backend import aggregator
    base = {"event": "Spain vs Saudi Arabia", "line": 9.5, "price": 1.8, "selection": "Over 9.5 corners",
            "commence_time": "2026-06-21T16:00:00Z", "model_prob": 0.62, "ev": 0.117, "confidence": "high"}
    row = aggregator._corner_paper_row(base)
    # dedup is per-game-per-day, independent of the moving line/side (no intraday double-log)
    assert row["dedup_key"] == time.strftime("%Y-%m-%d") + ":corners:Spain vs Saudi Arabia"
    assert "pick_fair_prob" not in row                       # corners have no CLV → no fair-prob entry
    assert row["stake_units"] == 1.5                         # ev >= 0.08 → strong, matches the card tier
    assert aggregator._corner_paper_row(dict(base, ev=0.05))["stake_units"] == 1.0
    moved = aggregator._corner_paper_row(dict(base, selection="Under 10.5 corners", line=10.5))
    assert moved["dedup_key"] == row["dedup_key"]            # a moved line/side won't log a 2nd row


def _fresh_paper():
    config.DB_PATH = tempfile.mktemp(suffix=".db")
    from backend.store import paper
    importlib.reload(paper)
    paper.init_paper()
    return paper


def test_settle_corners_won_lost():
    paper = _fresh_paper()
    stats = {("2026-06-21", frozenset({"spain", "saudi arabia"})):
             {"spain": {"corners": 9}, "saudi arabia": {"corners": 3}}}   # total = 12
    rows = [
        ("over_win", "Over 9.5 corners"),     # 12 > 9.5  → won
        ("over_lose", "Over 12.5 corners"),   # 12 > 12.5 → lost
        ("under_win", "Under 12.5 corners"),  # 12 < 12.5 → won
        ("under_lose", "Under 9.5 corners"),  # 12 < 9.5  → lost
    ]
    paper.log_picks([{"match": "Spain vs Saudi Arabia", "archetype": "total_corners",
                      "selection": sel, "commence_time": "2026-06-21",
                      "pick_fair_prob": 0.55, "pick_price_decimal": 1.9, "dedup_key": k}
                     for k, sel in rows])
    n = paper.settle_corners(stats)
    got = {p["dedup_key"]: p["status"] for p in paper.list_picks()}
    assert got == {"over_win": "won", "over_lose": "lost", "under_win": "won", "under_lose": "lost"}
    assert n == 4
    os.unlink(config.DB_PATH)


def test_settle_corners_exact_line_pushes_to_void():
    paper = _fresh_paper()
    stats = {("2026-06-21", frozenset({"spain", "saudi arabia"})):
             {"spain": {"corners": 6}, "saudi arabia": {"corners": 4}}}   # total = 10, line = 10 → push
    rows = [("over_push", "Over 10 corners"), ("under_push", "Under 10 corners")]
    paper.log_picks([{"match": "Spain vs Saudi Arabia", "archetype": "total_corners", "selection": sel,
                      "commence_time": "2026-06-21", "pick_price_decimal": 1.9, "dedup_key": k}
                     for k, sel in rows])
    paper.settle_corners(stats)
    got = {p["dedup_key"]: p["status"] for p in paper.list_picks()}
    assert got == {"over_push": "void", "under_push": "void"}    # refund, not a double loss
    os.unlink(config.DB_PATH)


def test_parlay_goalscorer_unresolved_when_no_roster():
    import json as _json
    paper = _fresh_paper()
    legs = [{"type": "anytime_goalscorer", "player": "Harry Kane", "team_key": "england"}]
    paper.log_picks([{"match": "England vs Wales", "archetype": "parlay", "selection": "Kane to score",
                      "commence_time": "2026-06-20", "pick_fair_prob": 0.5, "pick_price_decimal": 2.0,
                      "dedup_key": "plx", "legs_json": _json.dumps(legs)}])
    # finished game (goals present) but ESPN /summary failed → scorers + played empty
    results = [{"date": "2026-06-20", "goals": {"england": 2, "wales": 0}, "scorers": set(), "played": set()}]
    paper.settle_parlays(results)
    assert paper.list_picks()[0]["status"] == "pending"   # left pending, not wrongly graded lost
    os.unlink(config.DB_PATH)
