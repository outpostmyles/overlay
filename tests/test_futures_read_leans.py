"""Futures Read heuristic + the leans store (add / live-drift enrich / remove) + record building."""
from backend import aggregator, config, futures_read
from backend.store import leans


def test_team_records_count_games_vs_unshown_opponents():
    # brazil's record must include games vs opponents NOT on the shown board, not just shown ones
    results = [
        {"date": "20260615", "goals": {"brazil": 3, "serbia": 0}},
        {"date": "20260620", "goals": {"brazil": 2, "switzerland": 1}},
        {"date": "20260624", "goals": {"brazil": 1, "argentina": 1}},
    ]
    recs = aggregator._team_records(results, {"brazil", "argentina"})   # serbia/switzerland not shown
    assert recs["brazil"].startswith("2-1-0 - ")                        # 2 wins, 1 draw, 0 losses
    assert "serbia 3-0" in recs["brazil"] and "switzerland 2-1" in recs["brazil"]
    assert "serbia" not in recs                                         # serbia not shown -> no record built


def test_futures_read_heuristic_is_market_led():
    # with no eye-test the honest default is always pass, framed by the model-vs-market gap
    over = futures_read.read({"team": "brazil", "kind": "Win World Cup", "market_pct": 5.5, "model_pct": 22.3})
    assert over["lean"] == "pass" and "above" in over["why"]
    under = futures_read.read({"team": "france", "kind": "Win World Cup", "market_pct": 21.0, "model_pct": 4.5})
    assert under["lean"] == "pass" and "below" in under["why"]
    agree = futures_read.read({"team": "spain", "kind": "Reach Quarter-final", "market_pct": 58.5, "model_pct": 57.8})
    assert agree["lean"] == "pass" and "agree" in agree["why"]


def test_leans_drift_signs_and_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LEANS_PATH", tmp_path / "leans.json")
    back = leans.add("argentina", "Win World Cup", "back", 17.2, "best defense in the field")
    fade = leans.add("spain", "Win World Cup", "fade", 12.1, "flat all tournament")
    assert back["direction"] == "back" and fade["direction"] == "fade"

    # market: Argentina drifts UP to 19 (toward the back -> +drift), Spain DOWN to 9.9 (toward the fade -> +drift)
    rows = [{"team": "argentina", "kind": "Win World Cup", "market_pct": 19.0},
            {"team": "spain", "kind": "Win World Cup", "market_pct": 9.9}]
    en = {e["team"]: e for e in leans.enrich(rows)}
    assert en["argentina"]["drift_pp"] == 1.8        # 19.0 - 17.2, in favor of the back
    assert en["spain"]["drift_pp"] == 2.2            # 12.1 - 9.9, in favor of the fade

    # a team no longer on the board -> current/drift are None, not a crash
    none_row = {e["team"]: e for e in leans.enrich([])}
    assert none_row["spain"]["current_pct"] is None and none_row["spain"]["drift_pp"] is None

    # re-logging the same (team, kind, direction) replaces, not duplicates
    leans.add("spain", "Win World Cup", "fade", 11.0, "still flat")
    spain_rows = [e for e in leans.enrich(rows) if e["team"] == "spain"]
    assert len(spain_rows) == 1 and spain_rows[0]["entry_pct"] == 11.0

    assert leans.remove(fade["id"]) is False         # old id was replaced
    cur_spain = next(e for e in leans.enrich(rows) if e["team"] == "spain")
    assert leans.remove(cur_spain["id"]) is True
    assert [e["team"] for e in leans.enrich(rows)] == ["argentina"]
