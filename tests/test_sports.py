"""SportAdapter seam: the wc26 adapter must mirror the pre-seam constants exactly."""
from backend import config, sports


def test_wc26_registered_and_active(monkeypatch):
    assert "wc26" in sports.keys()
    monkeypatch.setattr(config, "SPORT", "wc26")     # pin: an exported SPORT env must not flake this
    assert sports.active().key == "wc26"


def test_unknown_sport_fails_loudly(monkeypatch):
    import pytest
    monkeypatch.setattr(config, "SPORT", "not_a_sport")   # "nhl" served here until NHL became real
    with pytest.raises(ValueError, match="unknown SPORT"):
        sports.active()


def test_wc26_mirrors_the_preseam_constants():
    a = sports.get("wc26")
    assert a.outcomes == ("a", "draw", "b")
    assert a.espn_path == "soccer/fifa.world"
    assert a.kalshi_series["KXWCGAME"] == ("moneyline", "Matches")
    assert a.kalshi_series["KXMENWORLDCUP"] == ("winner_outright", "Futures")
    assert a.kalshi_resolved_series == "KXWCGAME"
    assert a.kalshi_strip_reg_time is True
    assert a.kalshi_outright_event == "World Cup Winner"
    assert a.polymarket_search_q == "world cup"
    assert a.polymarket_pinned_slugs == ("world-cup-winner",)
    assert a.polymarket_game_slug_prefix == "fifwc-"
    assert a.odds_api_sport == "soccer_fifa_world_cup"
    assert a.prizepicks_league == 241
    assert "model" in a.capabilities and "bracket" in a.capabilities


def test_wc26_polymarket_classifier():
    c = sports.get("wc26").polymarket_classify
    assert c("world-cup-winner") == "winner_outright"
    assert c("world-cup-group-b-winner") == "group_winner"
    assert c("world-cup-nation-to-reach-round-of-16-japan") == "advance_r16"
    assert c("world-cup-nation-to-reach-quarterfinals-brazil") == "advance_qf"
    assert c("world-cup-nation-to-reach-semifinals-france") == "advance_sf"
    assert c("world-cup-group-m-winner") is None      # anchored group regex: only groups a-l exist
    assert c("fifwc-bra-jpn-2026-06-29") is None      # per-game events ride smart money, not futures
    assert c("mlb-nyy-bos-2026-07-11") is None
