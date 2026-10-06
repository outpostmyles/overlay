"""NHL adapter: label alignment against the live Kalshi + ESPN sets, date-only tickers, the totals
join, and grading of overtime/shootout finals (ESPN credits a shootout winner with one goal)."""
import importlib
import os
import tempfile

from backend import aggregator, config, sports
from backend.matching import kalshi_ticker_date, moneyline_key, normalize_team

# both lists exactly as returned live on 2026-10-06 (Kalshi KXNHLGAME yes_sub_title, ESPN displayName)
KALSHI = {"ANA": "Anaheim", "BOS": "Boston", "BUF": "Buffalo", "CAR": "Carolina", "CBJ": "Columbus",
          "CGY": "Calgary", "CHI": "Chicago", "COL": "Colorado", "DAL": "Dallas", "DET": "Detroit",
          "EDM": "Edmonton", "FLA": "Florida", "LA": "Los Angeles", "MIN": "Minnesota",
          "MTL": "Montreal", "NJ": "New Jersey", "NSH": "Nashville", "NYI": "New York I",
          "NYR": "New York R", "OTT": "Ottawa", "PHI": "Philadelphia", "PIT": "Pittsburgh",
          "SEA": "Seattle", "SJ": "San Jose", "STL": "St. Louis", "TB": "Tampa Bay", "TOR": "Toronto",
          "UTA": "Utah", "VAN": "Vancouver", "VGK": "Vegas", "WPG": "Winnipeg", "WSH": "Washington"}
ESPN = {"ANA": "Anaheim Ducks", "BOS": "Boston Bruins", "BUF": "Buffalo Sabres",
        "CAR": "Carolina Hurricanes", "CBJ": "Columbus Blue Jackets", "CGY": "Calgary Flames",
        "CHI": "Chicago Blackhawks", "COL": "Colorado Avalanche", "DAL": "Dallas Stars",
        "DET": "Detroit Red Wings", "EDM": "Edmonton Oilers", "FLA": "Florida Panthers",
        "LA": "Los Angeles Kings", "MIN": "Minnesota Wild", "MTL": "Montreal Canadiens",
        "NJ": "New Jersey Devils", "NSH": "Nashville Predators", "NYI": "New York Islanders",
        "NYR": "New York Rangers", "OTT": "Ottawa Senators", "PHI": "Philadelphia Flyers",
        "PIT": "Pittsburgh Penguins", "SEA": "Seattle Kraken", "SJ": "San Jose Sharks",
        "STL": "St. Louis Blues", "TB": "Tampa Bay Lightning", "TOR": "Toronto Maple Leafs",
        "UTA": "Utah Mammoth", "VAN": "Vancouver Canucks", "VGK": "Vegas Golden Knights",
        "WPG": "Winnipeg Jets", "WSH": "Washington Capitals"}


def _fresh_paper():
    config.DB_PATH = tempfile.mktemp(suffix=".db")
    from backend.store import paper
    importlib.reload(paper)
    paper.init_paper()
    return paper


def test_nhl_adapter_registered():
    a = sports.get("nhl")
    assert a.outcomes == ("a", "b")                  # OT and the shootout settle every game
    assert a.espn_path == "hockey/nhl"
    assert a.kalshi_series == {"KXNHLGAME": ("moneyline", "Matches"), "KXNHLTOTAL": ("total", "Lines")}
    assert a.capabilities == frozenset()             # anchor-only by design
    assert a.pair_only_key is False                  # a pair meets several times a season
    assert "nhl" in sports.keys()


def test_every_kalshi_label_lands_on_its_espn_team(monkeypatch):
    """All 32 clubs: the Kalshi label and the ESPN displayName must normalize to the same key, and the
    ESPN name must pass through unchanged (no global soccer alias may capture an NHL club)."""
    monkeypatch.setattr(config, "SPORT", "nhl")
    assert set(KALSHI) == set(ESPN)
    for code, label in KALSHI.items():
        espn_key = normalize_team(ESPN[code])
        assert normalize_team(label) == espn_key, (code, label, espn_key)
        assert espn_key == ESPN[code].lower().replace(".", ""), (code, espn_key)
    assert len({normalize_team(v) for v in ESPN.values()}) == 32   # no two clubs collapse together


def test_nhl_aliases_do_not_leak_into_mlb(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "mlb")
    assert normalize_team("Pittsburgh") == "pittsburgh pirates"
    monkeypatch.setattr(config, "SPORT", "nhl")
    assert normalize_team("Pittsburgh") == "pittsburgh penguins"


def test_nhl_tickers_carry_a_bare_date_and_keys_stay_date_qualified(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "nhl")
    assert kalshi_ticker_date("KXNHLGAME-26OCT06CARMTL") == "2026-10-06"   # no HHMM, unlike MLB
    pair = ("carolina hurricanes", "montreal canadiens")
    assert moneyline_key("2026-10-06", pair) != moneyline_key("2026-10-08", pair)


def test_totals_title_regex_covers_nhl_and_still_mlb():
    m = aggregator._TOTAL_TITLE_RE.match("Carolina vs Montreal: Total Goals")
    assert m and m.group(1) == "Carolina"
    m = aggregator._TOTAL_TITLE_RE.match("Pittsburgh vs New York Y: Total Runs")   # MLB, unchanged
    assert m and m.group(1) == "Pittsburgh"
    assert aggregator._TOTAL_TITLE_RE.match("Carolina vs Montreal: 1st Period Winner") is None


def test_shootout_final_grades_winner_and_total(monkeypatch):
    """ESPN's Final/SO of PHI 4, NYI 3 credits the shootout goal: the winner is clean (no tie) and the
    total of 7 clears a 6.5 line, exactly as the Kalshi moneyline and totals markets settle."""
    monkeypatch.setattr(config, "SPORT", "nhl")
    paper = _fresh_paper()
    key = "fc|2026-10-25|new york islanders|philadelphia flyers"
    paper.log_forecasts([{"match": "NYI vs PHI", "team_a": "new york islanders",
                          "team_b": "philadelphia flyers", "commence_time": "2026-10-25",
                          "stage": None, "dedup_key": key}], today="2026-10-25")
    legs = [{"key": "total_goals", "side": "over", "line": 6.5, "team": None, "prob": 0.47, "proj": None}]
    board = {key: {"lock_now": True, "missed": False, "kickoff_iso": "2026-10-25T23:00Z",
                   "model": None, "market": (0.46, 0.0, 0.54), "sources": "kalshi", "legs": legs}}
    assert paper.lock_forecasts(board, "2026-10-25T21:45:00Z") == 1
    results = [{"date": "2026-10-25", "goals": {"new york islanders": 3, "philadelphia flyers": 4},
                "winner": "philadelphia flyers", "iso": "2026-10-25T23:00Z"}]
    assert paper.settle_forecasts(results, None) == 1
    r = paper.list_forecasts()[0]
    assert r["actual_outcome"] == "b" and r["sport"] == "nhl"
    assert r["legs"][0]["actual"] == 7 and r["legs"][0]["result"] == "won"
    os.unlink(config.DB_PATH)
