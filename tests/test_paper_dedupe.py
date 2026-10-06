"""A pick is one bet per game: re-reading the same game on a later day must not log it again. The old
dedup key started with the day the pick was logged, so the WC ledger holds Spain ML v Saudi Arabia
twice (ids 3 and 83), Germany ML v Ivory Coast twice (48 and 65) and a Rodri passes prop twice."""
import importlib
import tempfile

from backend import aggregator, config


def _fresh_paper():
    config.DB_PATH = tempfile.mktemp(suffix=".db")
    from backend.store import paper
    importlib.reload(paper)
    paper.init_paper()
    return paper


def _spain(logged, game="2026-06-21", **kw):
    # the dedup_key the aggregator used to build: the LOG day first
    return {"logged_at": f"{logged} 04:42:54", "match": "Spain vs Saudi Arabia", "archetype": "favorite_ml",
            "selection": "Spain ML", "commence_time": game, "pick_fair_prob": 0.884,
            "pick_price_decimal": 1.1236, "dedup_key": f"{logged}:Spain vs Saudi Arabia:favorite_ml:Spain ML", **kw}


def test_same_game_logged_on_a_later_day_is_skipped(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "wc26")
    paper = _fresh_paper()
    assert paper.log_picks([_spain("2026-06-19")]) == 1
    assert paper.log_picks([_spain("2026-06-21")]) == 0          # the same bet, read again on game day
    assert len(paper.list_picks()) == 1
    # a different game of the same pair, or a different bet on this game, is a new pick
    assert paper.log_picks([_spain("2026-06-21", game="2026-07-02", dedup_key="rematch")]) == 1
    assert paper.log_picks([_spain("2026-06-21", archetype="team_total_over", dedup_key="total")]) == 1
    assert len(paper.list_picks()) == 3


def test_pick_without_a_game_time_dedupes_on_its_log_day(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "wc26")
    paper = _fresh_paper()
    assert paper.log_picks([_spain("2026-06-19", game=None)]) == 1
    assert paper.log_picks([_spain("2026-06-19", game=None, dedup_key="other key")]) == 0
    assert paper.log_picks([_spain("2026-06-20", game=None)]) == 1


def test_another_sports_pick_never_blocks_this_one(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "mlb")
    paper = _fresh_paper()
    assert paper.log_picks([_spain("2026-06-19", dedup_key="mlb-1")]) == 1
    monkeypatch.setattr(config, "SPORT", "wc26")
    assert paper.log_picks([_spain("2026-06-19", dedup_key="wc-1")]) == 1


def test_aggregator_keys_are_the_game_date_not_the_log_day():
    verdicts = {"Spain vs Saudi Arabia": {"confidence": 4, "recommended_bets": [
        {"archetype": "favorite_ml", "selection": "Spain ML"}]}}
    bundles = [{"match": "Spain vs Saudi Arabia", "favorite": "Spain", "favorite_fair_pct": 88.4,
                "commence_time": "2026-06-21", "props": []}]
    rows = aggregator._paper_rows(verdicts, bundles, [])
    assert rows[0]["dedup_key"] == "2026-06-21:Spain vs Saudi Arabia:favorite_ml:Spain ML"
    sgp = {"event": "Germany vs Paraguay", "commence_time": "2026-06-29T20:30:00Z",
           "pricing": {"joint_prob": 0.46, "payout": 3.0},
           "legs": [{"selection": "Germany ML"}, {"selection": "Germany Over 1.5"}]}
    assert aggregator._parlay_row(sgp)["dedup_key"] == "2026-06-29:parlay:Germany vs Paraguay"
