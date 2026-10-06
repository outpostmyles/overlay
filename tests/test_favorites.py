"""The favorites-by-price tracker pools every sport in the ledger (whichever board asks), buckets the
market's favorite by its price, and skips games where the market favored a draw."""
import importlib
import tempfile

from backend import config


def _fresh_paper():
    config.DB_PATH = tempfile.mktemp(suffix=".db")
    from backend.store import paper
    importlib.reload(paper)
    paper.init_paper()
    return paper


def _settled(paper, sport, a, draw, b, outcome):
    with paper._conn() as c:
        c.execute("INSERT INTO forecasts (match, team_a, team_b, commence_time, logged_at, status, sport, "
                  "market_a, market_draw, market_b, actual_outcome) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  ("x", "a", "b", "2026-07-20", "2026-07-20T00:00:00Z", "settled", sport, a, draw, b, outcome))


def test_favorites_pool_every_sport_and_bucket_by_price(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "nhl")              # the asking board must not narrow the pool
    paper = _fresh_paper()
    _settled(paper, "mlb", 0.55, 0.0, 0.45, "a")             # 50-60%, favorite won
    _settled(paper, "mlb", 0.40, 0.0, 0.60, "a")             # 60-70%, favorite (b) lost
    _settled(paper, "cfb", 0.80, 0.0, 0.20, "a")             # -300 or shorter, won
    _settled(paper, None, 0.72, 0.18, 0.10, "a")             # legacy World Cup row, -233 to -300, won
    _settled(paper, "wc26", 0.30, 0.45, 0.25, "draw")        # the market favored the draw: skipped
    fp = paper.favorites_by_price()
    assert fp["n"] == 4
    by = {b["label"]: b for b in fp["bands"]}
    assert (by["-100 to -150"]["n"], by["-100 to -150"]["won"]) == (1, 1)
    assert (by["-150 to -233"]["n"], by["-150 to -233"]["won"]) == (1, 0)
    assert (by["-233 to -300"]["n"], by["-300 or shorter"]["n"]) == (1, 1)
    h = fp["heavy"]
    assert (h["n"], h["won"], h["expected"]) == (2, 2, 1.5)
    assert h["sports"] == {"wc26": 1, "cfb": 1}


def test_three_way_favorite_under_50_has_a_band_and_n_adds_up():
    """A World Cup favorite can be under 50% with the draw live: 11 were, and they counted toward n (843)
    while falling into no band (832 shown)."""
    paper = _fresh_paper()
    _settled(paper, "wc26", 0.45, 0.30, 0.25, "a")           # three-way favorite at 45%, won
    _settled(paper, "wc26", 0.25, 0.32, 0.43, "draw")        # three-way favorite at 43%, did not
    _settled(paper, "mlb", 0.55, 0.0, 0.45, "a")
    fp = paper.favorites_by_price()
    assert fp["n"] == 3 == sum(b["n"] for b in fp["bands"])
    under = fp["bands"][0]
    assert under["label"] == "Under 50% (three-way)"
    assert (under["n"], under["won"], under["expected"], under["sports"]) == (2, 1, 0.9, {"wc26": 2})
