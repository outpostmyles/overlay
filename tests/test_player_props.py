"""Settlement of shots/SOT/passes player props from API-Football stats (mocked — no live key).
Covers the parser, the stat-kind detector, and won/lost/void(DNP) grading."""
import tempfile
import os

from backend import config


def _fresh_paper():
    config.DB_PATH = tempfile.mktemp(suffix=".db")
    from backend.store import paper
    import importlib
    importlib.reload(paper)
    paper.init_paper()
    return paper


def test_parse_players_shape():
    from backend.sources import apifootball
    body = {"response": [
        {"team": {"name": "Germany"}, "players": [
            {"player": {"name": "Kai Havertz"},
             "statistics": [{"games": {"minutes": 90}, "shots": {"total": 4, "on": 2},
                             "passes": {"total": 31}, "tackles": {"total": 1}}]},
            {"player": {"name": "Bench Guy"},
             "statistics": [{"games": {"minutes": None}, "shots": {"total": None, "on": None},
                             "passes": {"total": None}, "tackles": {"total": None}}]},
        ]},
    ]}
    p = apifootball._parse_players(body)
    assert p["kai havertz"] == {"shots": 4, "sot": 2, "passes": 31, "tackles": 1,
                                "minutes": 90, "played": True}
    assert p["bench guy"]["played"] is False and p["bench guy"]["shots"] == 0


def test_stat_kind():
    from backend.store import paper
    assert paper._stat_kind("Kai Havertz Over 0.5 Shots On Target") == "sot"
    assert paper._stat_kind("Kai Havertz Shots Over 3") == "shots"
    assert paper._stat_kind("Pedri Passes Attempted Over 73.5") == "passes"
    assert paper._stat_kind("Casemiro Tackles Over 1.5") == "tackles"
    assert paper._stat_kind("Someone Goals 0.5") is None


def test_settle_player_props_won_lost_void():
    paper = _fresh_paper()
    stats = {("2026-06-20", frozenset({"germany", "spain"})): {
        "kai havertz": {"shots": 4, "sot": 2, "passes": 30, "tackles": 1, "minutes": 90, "played": True},
        "benched guy": {"shots": 0, "sot": 0, "passes": 0, "tackles": 0, "minutes": None, "played": False},
    }}
    rows = [
        ("sot_win", "Kai Havertz Over 0.5 Shots On Target"),   # 2 > 0.5 → won
        ("shots_win", "Kai Havertz Shots Over 3"),             # 4 > 3 → won
        ("shots_lose", "Kai Havertz Shots Over 4.5"),          # 4 > 4.5? no → lost
        ("dnp_void", "Benched Guy Shots Over 0.5"),            # didn't play → void
        ("absent_void", "Nonexistent Player Shots Over 1.5"),  # clean name not in squad → void
    ]
    paper.log_picks([{"match": "Germany vs Spain", "archetype": "shots_sot", "selection": sel,
                      "commence_time": "2026-06-20", "dedup_key": k} for k, sel in rows]
                    + [{"match": "Other vs Game", "archetype": "shots_sot",
                        "selection": "Player Shots Over 1.5", "commence_time": "2026-06-20",
                        "dedup_key": "no_data"}])
    n = paper.settle_player_props(stats)
    got = {p["dedup_key"]: p["status"] for p in paper.list_picks()}
    assert got == {"sot_win": "won", "shots_win": "won", "shots_lose": "lost",
                   "dnp_void": "void", "absent_void": "void", "no_data": "pending"}
    assert n == 5
    os.unlink(config.DB_PATH)


def test_model_prob_logs_and_calibration_detects_overprojection():
    """Props now log the model's projected P(hit); model_calibration grades it (Brier + over/under)."""
    paper = _fresh_paper()
    paper.log_picks([{"match": "A vs B", "archetype": "shots_sot", "selection": f"Player{i} Shots Over 1.5",
                      "commence_time": "2026-06-20", "model_prob": 0.60, "dedup_key": f"m{i}"}
                     for i in range(4)])
    assert paper.list_picks()[0]["model_prob"] == 0.60          # round-trips through the new column
    with paper._conn() as c:                                    # 1 of 4 hits -> model over-projected
        for i, st in enumerate(["won", "lost", "lost", "lost"]):
            c.execute("UPDATE paper_picks SET status=? WHERE dedup_key=?", (st, f"m{i}"))
    cal = paper.model_calibration()["shots_sot"]
    assert cal["n"] == 4 and cal["mean_pred"] == 0.6 and cal["hit_rate"] == 0.25
    assert cal["gap_pp"] == 35.0          # +35pp = the model is too optimistic on shots
    assert cal["brier"] == 0.31           # mean of [(.6-1)^2, .6^2, .6^2, .6^2]
    os.unlink(config.DB_PATH)


def test_settle_player_props_honors_under():
    """Under-phrased props (e.g. fading a hot line) must grade on the UNDER, not silently as an over."""
    paper = _fresh_paper()
    stats = {("2026-06-20", frozenset({"germany", "spain"})): {
        "kai havertz": {"shots": 1, "sot": 0, "passes": 40, "tackles": 1, "minutes": 90, "played": True},
    }}
    rows = [("under_win", "Kai Havertz Under 2.5 Shots"),    # 1 < 2.5 → won
            ("under_lose", "Kai Havertz Under 0.5 Shots")]   # 1 < 0.5? no → lost
    paper.log_picks([{"match": "Germany vs Spain", "archetype": "shots_sot", "selection": sel,
                      "commence_time": "2026-06-20", "dedup_key": k} for k, sel in rows])
    paper.settle_player_props(stats)
    got = {p["dedup_key"]: p["status"] for p in paper.list_picks()}
    assert got == {"under_win": "won", "under_lose": "lost"}
    os.unlink(config.DB_PATH)
