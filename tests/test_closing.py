"""Closing-line capture must only ever record PRE-start prices. A market trades in-play, so a close
written after first pitch tracks the score and leaks the result into CLV (before the cutoff, 10 of the
23 WC moneyline closes were pinned at 0.989 and all 10 won)."""
import asyncio
import importlib
import tempfile
from datetime import datetime

from backend import aggregator, config


def _fresh_paper():
    config.DB_PATH = tempfile.mktemp(suffix=".db")
    from backend.store import paper
    importlib.reload(paper)
    paper.init_paper()
    return paper


def _pick(paper, match, date, selection, archetype="favorite_ml", fair=0.6):
    with paper._conn() as c:
        return c.execute(
            "INSERT INTO paper_picks (logged_at, match, archetype, selection, commence_time, "
            "pick_fair_prob) VALUES (?,?,?,?,?,?)",
            ("2026-07-20T12:00:00Z", match, archetype, selection, date, fair)).lastrowid


def _close(paper, pid):
    with paper._conn() as c:
        return c.execute("SELECT closing_fair_prob FROM paper_picks WHERE id=?", (pid,)).fetchone()[0]


def test_bet_close_is_keyed_per_game_not_per_matchup():
    """A series repeats the matchup string, so game 2's price must never become game 1's close."""
    paper = _fresh_paper()
    g1 = _pick(paper, "Pittsburgh vs New York Y", "2026-07-21", "New York Y ML")
    g2 = _pick(paper, "Pittsburgh vs New York Y", "2026-07-22", "New York Y ML")
    paper.capture_closing({("Pittsburgh vs New York Y", "2026-07-22"): ("New York Y ML", 0.64)})
    assert _close(paper, g1) is None
    assert _close(paper, g2) == 0.64


def test_bet_close_skips_flipped_favorite_and_other_archetypes():
    paper = _fresh_paper()
    backed_a = _pick(paper, "A vs B", "2026-07-21", "A ML")
    prop = _pick(paper, "A vs B", "2026-07-21", "A ML", archetype="team_total_over")
    # the board's favorite is now B: B's price must not become the close of a pick that backed A
    paper.capture_closing({("A vs B", "2026-07-21"): ("B ML", 0.55)})
    assert _close(paper, backed_a) is None
    paper.capture_closing({("A vs B", "2026-07-21"): ("A ML", 0.58)})
    assert _close(paper, backed_a) == 0.58
    assert _close(paper, prop) is None     # only favorite-ML picks carry a moneyline close


def _freeze(monkeypatch, iso, kicks, sport="mlb"):
    now = datetime.fromisoformat(iso)

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now

    async def fake_kicks(dates):
        return kicks

    monkeypatch.setattr(aggregator, "datetime", _DT)
    monkeypatch.setattr(aggregator, "get_kickoffs", fake_kicks)
    monkeypatch.setattr(config, "SPORT", sport)


def _fav(event, team, opp, date, fair):
    return {"event": event, "team": team.title(), "team_key": team, "opp_key": opp,
            "commence_time": date, "fair_prob": fair}


def test_pre_start_closes_drop_started_and_unknown_games(monkeypatch):
    pit, nyy, bos, bal = "pittsburgh pirates", "new york yankees", "boston red sox", "baltimore orioles"
    kicks = {(frozenset((nyy, pit)), "2026-07-21"): "2026-07-21T23:05Z",   # 19:05 ET, not started yet
             (frozenset((bos, bal)), "2026-07-21"): "2026-07-21T17:05Z"}   # already under way
    _freeze(monkeypatch, "2026-07-21T22:00:00+00:00", kicks)
    favs = [_fav("PIT vs NYY", nyy, pit, "2026-07-21T19:05", 0.61),
            _fav("BOS vs BAL", bos, bal, "2026-07-21T13:05", 0.97),          # in-play price, must drop
            _fav("CIN vs SEA", "seattle mariners", "cincinnati reds", "2026-07-21T21:40", 0.55)]
    out = asyncio.run(aggregator._pre_start_closes(favs))
    assert out == {("PIT vs NYY", "2026-07-21"): ("New York Yankees ML", 0.61)}


def test_pre_start_closes_skip_ambiguous_doubleheader(monkeypatch):
    nyy, tor = "new york yankees", "toronto blue jays"
    _freeze(monkeypatch, "2026-07-21T12:00:00+00:00",
            {(frozenset((nyy, tor)), "2026-07-21"): "2026-07-21T23:05Z"})
    favs = [_fav("TOR vs NYY", nyy, tor, "2026-07-21T13:05", 0.58),
            _fav("TOR vs NYY", nyy, tor, "2026-07-21T19:05", 0.60)]
    assert asyncio.run(aggregator._pre_start_closes(favs)) == {}


def test_forecast_close_holds_at_first_pitch_even_if_board_forgets_kickoff(monkeypatch):
    """If ESPN drops a game on a refresh, the board can keep emitting it unflagged with in-play prices.
    The row's own frozen kickoff must still stop the close at first pitch."""
    monkeypatch.setattr(config, "SPORT", "mlb")
    paper = _fresh_paper()
    key = "fc|2026-07-21|cincinnati reds|seattle mariners|21:40"
    paper.log_forecasts([{"match": "x", "team_a": "cincinnati reds", "team_b": "seattle mariners",
                          "commence_time": "2026-07-21", "stage": None, "dedup_key": key}],
                        today="2026-07-21")
    base = {"lock_now": True, "missed": False, "kickoff_iso": "2026-07-22T01:40Z",
            "model": None, "market": (0.45, 0.0, 0.55), "sources": "kalshi", "legs": []}
    paper.lock_forecasts({key: base}, "2026-07-22T00:26:00Z")
    paper.capture_forecast_close({key: {**base, "market": (0.47, 0.0, 0.53)}}, "2026-07-22T01:35:00Z")
    assert paper.list_forecasts()[0]["closing_a"] == 0.47          # pre-start tick: recorded
    # 20 minutes into the game, the board (missing its kickoff) still says not missed
    paper.capture_forecast_close({key: {**base, "market": (0.88, 0.0, 0.12)}}, "2026-07-22T02:00:00Z")
    assert paper.list_forecasts()[0]["closing_a"] == 0.47          # in-play price refused
