"""College football: Power 4 + Notre Dame scope, school-name keys (ESPN `location`), the "St." rule,
explicit names that beat the rule, the liquidity gate, and the ESPN scoreboard params."""
from datetime import datetime, timezone

from backend import aggregator, config, sports
from backend.matching import normalize_team
from backend.models import Market, Quote, Selection
from backend.sources import espn


def test_cfb_adapter_scope_and_knobs():
    a = sports.get("cfb")
    assert len(a.team_filter) == 68                       # SEC 16, Big Ten 18, Big 12 16, ACC 17, ND
    assert {"notre dame", "ohio state", "miami", "texas a and m", "smu", "stanford"} <= a.team_filter
    assert "uconn" not in a.team_filter                    # the other FBS independent is out of scope
    assert a.espn_team_field == "location" and a.max_lock_spread == 0.10
    assert dict(a.espn_scoreboard_params) == {"groups": "80", "limit": "300"}
    assert a.kalshi_series["KXNCAAFSPREAD"] == ("spread", "Lines")    # the owner bets spreads here
    assert not any(v[0] == "player_prop" for v in a.kalshi_series.values())


def test_school_names_line_up(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "cfb")
    assert normalize_team("Ohio St.") == "ohio state"                 # the rule
    assert normalize_team("Mississippi St.") == "mississippi state"
    assert normalize_team("Appalachian St.") == "app state"           # explicit beats the rule
    assert normalize_team("Miami (FL)") == "miami"
    assert normalize_team("Miami (OH)") == "miami oh"                  # a different school, untouched
    assert normalize_team("Texas A&M") == "texas a and m"
    for key in sports.get("cfb").team_filter:                          # ESPN school names pass through
        assert normalize_team(key) == key, key


def test_espn_keys_on_school_for_cfb_only(monkeypatch):
    team = {"displayName": "Ohio State Buckeyes", "location": "Ohio State"}
    monkeypatch.setattr(config, "SPORT", "cfb")
    assert espn._team_key(team) == "ohio state"
    assert espn._sb_params("20261010") == {"dates": "20261010", "groups": "80", "limit": "300"}
    monkeypatch.setattr(config, "SPORT", "nfl")
    assert espn._team_key({"displayName": "Dallas Cowboys", "location": "Dallas"}) == "dallas cowboys"
    assert espn._sb_params("20261011") == {"dates": "20261011"}


def _ml(a, b, pa, spread, date="2026-10-10"):
    def sel(k, p):
        return Selection(key=k, label=k, fair_prob=p,
                         quotes=[Quote(source="kalshi", source_type="prediction_market",
                                       price_decimal=1 / p, implied_prob=p, mid_prob=p,
                                       bid=round(p - spread / 2, 3), ask=round(p + spread / 2, 3))])
    return Market(market_id=f"kalshi:{a}{b}", event=f"{a} vs {b}", market_type="moneyline",
                  selections=[sel(a, pa), sel(b, 1 - pa)], commence_time=date)


def test_scope_keeps_any_game_with_one_in_scope_team(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "cfb")
    assert aggregator._in_scope(_ml("georgia", "alabama", 0.6, 0.02))
    assert aggregator._in_scope(_ml("navy", "notre dame", 0.2, 0.02))     # ND's non-conference game
    assert not aggregator._in_scope(_ml("navy", "army", 0.5, 0.02))
    total = Market(market_id="kalshi:t", event="Over 51.5 points", market_type="total",
                   selections=[], commence_time="2026-10-10", group="Lines|navy|notre dame")
    assert aggregator._in_scope(total)
    spread = Market(market_id="kalshi:s", event="x", market_type="spread", selections=[],
                    commence_time="2026-10-10", group="Lines|navy|notre dame|notre dame")
    assert aggregator._in_scope(spread)                                    # spreads carry a 4th field
    total.group = "Lines"                                                  # no game attached: drop it
    assert not aggregator._in_scope(total)
    monkeypatch.setattr(config, "SPORT", "nfl")
    assert aggregator._in_scope(_ml("navy", "army", 0.5, 0.02))            # no filter outside CFB


def test_thin_books_wait_tight_books_lock(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "cfb")
    pair = frozenset(("georgia", "alabama"))
    kicks = {(pair, "2026-10-10"): "2026-10-10T23:30Z"}
    now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    thin = aggregator._forecast_board([_ml("georgia", "alabama", 0.55, 0.19)], None, kicks, 75, now)[1]
    tight = aggregator._forecast_board([_ml("georgia", "alabama", 0.55, 0.02)], None, kicks, 75, now)[1]
    assert thin == {} and len(tight) == 1


def test_misdated_game_finds_its_espn_date_and_takes_it(monkeypatch):
    """Kalshi listed Florida vs Texas as Oct 16; ESPN has it Oct 17. The row must lock and be dated the
    17th (so it settles against the real final), and MLB-style sports must never borrow a neighbor."""
    pair = frozenset(("florida", "texas"))
    kicks = {(pair, "2026-10-17"): "2026-10-17T16:00Z"}
    now = datetime(2026, 10, 17, 14, 50, tzinfo=timezone.utc)          # inside the 75-minute window
    mk = [_ml("florida", "texas", 0.3, 0.02, date="2026-10-16")]
    monkeypatch.setattr(config, "SPORT", "cfb")
    cands, board = aggregator._forecast_board(mk, None, kicks, 75, now)
    (key, entry), = board.items()
    assert key.startswith("fc|2026-10-17|") and entry["lock_now"] is True
    assert cands[0]["commence_time"] == "2026-10-17"
    monkeypatch.setattr(config, "SPORT", "nfl")                        # no slack: the 16th stays unfound
    assert aggregator._forecast_board(mk, None, kicks, 75, now)[1] == {}
