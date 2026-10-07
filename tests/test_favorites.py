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


def test_favorites_pool_every_live_board_and_bucket_by_price(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "nhl")              # the asking board must not narrow the pool
    paper = _fresh_paper()
    _settled(paper, "mlb", 0.55, 0.0, 0.45, "a")             # 50-60%, favorite won
    _settled(paper, "mlb", 0.40, 0.0, 0.60, "a")             # 60-70%, favorite (b) lost
    _settled(paper, "cfb", 0.80, 0.0, 0.20, "a")             # -300 or shorter, won
    _settled(paper, "nhl", 0.72, 0.0, 0.28, "a")             # -233 to -300, won
    fp = paper.favorites_by_price()
    assert fp["n"] == 4 == sum(b["n"] for b in fp["bands"])
    by = {b["label"]: b for b in fp["bands"]}
    assert (by["-100 to -150"]["n"], by["-100 to -150"]["won"]) == (1, 1)
    assert (by["-150 to -233"]["n"], by["-150 to -233"]["won"]) == (1, 0)
    assert (by["-233 to -300"]["n"], by["-300 or shorter"]["n"]) == (1, 1)
    h = fp["heavy"]
    assert (h["n"], h["won"], h["expected"]) == (2, 2, 1.5)           # 0.72 + 0.80, to one place
    assert h["sports"] == {"nhl": 1, "cfb": 1}


def test_the_saved_world_cup_stays_out_of_the_pooled_table():
    """The finished World Cup is saved in the ledger, not shown: its rows (legacy NULL-sport ones too)
    never reach the table every live board shows."""
    paper = _fresh_paper()
    _settled(paper, "wc26", 0.45, 0.30, 0.25, "a")
    _settled(paper, None, 0.72, 0.18, 0.10, "a")
    _settled(paper, "mlb", 0.55, 0.0, 0.45, "a")
    fp = paper.favorites_by_price()
    assert fp["n"] == 1 and all("wc26" not in b["sports"] for b in fp["bands"])
