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


def _closed(paper, fair, close, status="won", closing_at=None, locked="2026-06-30 22:58:57"):
    with paper._conn() as c:
        return c.execute(
            "INSERT INTO paper_picks (logged_at, match, archetype, selection, commence_time, pick_fair_prob, "
            "pick_price_decimal, closing_fair_prob, status, closing_locked_at, closing_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-06-29 03:13:55", "France vs Sweden", "favorite_ml", "France ML", "2026-06-30", fair,
             round(1 / fair, 3), close, status, locked, closing_at)).lastrowid


def test_capture_stamps_the_close_as_pre_start():
    paper = _fresh_paper()
    pid = _pick(paper, "A vs B", "2026-07-21", "A ML")
    paper.capture_closing({("A vs B", "2026-07-21"): ("A ML", 0.62)})
    with paper._conn() as c:
        stamp = c.execute("SELECT closing_at FROM paper_picks WHERE id=?", (pid,)).fetchone()[0]
    assert stamp and stamp.endswith("Z")
    p = paper.list_picks()[0]
    assert (p["close_flag"], p["clv_pct"]) == (None, 3.33)


def test_track_record_ignores_in_play_and_pinned_closes(monkeypatch):
    """The WC headline (Avg CLV +15.39%, beat close 73.9%) came from closes written during or after the
    game. Only a close stamped by the pre-start capture, and not pinned at the top of the book, counts."""
    monkeypatch.setattr(config, "SPORT", "wc26")
    paper = _fresh_paper()
    pinned = _closed(paper, 0.781, 0.989)                            # France v Sweden: frozen 22:58, won
    in_play = _closed(paper, 0.801, 0.653, status="lost")            # unstamped: written until settlement
    stamped_pin = _closed(paper, 0.90, 0.99, closing_at="2026-10-06T16:00:00Z")
    void = _closed(paper, 0.60, 0.70, status="void", closing_at="2026-10-06T16:00:00Z")
    good = _closed(paper, 0.60, 0.63, status="pending", closing_at="2026-10-06T16:00:00Z", locked=None)
    flags = {p["id"]: (p["close_flag"], p["clv_pct"]) for p in paper.list_picks()}
    assert flags[pinned] == ("clamp", None) and flags[stamped_pin] == ("clamp", None)
    assert flags[in_play] == ("untimed", None) and flags[void] == ("void", None)
    assert flags[good] == (None, 5.0)
    s = paper.summary()
    for agg in (s["overall"], s["by_archetype"]["favorite_ml"]):
        assert (agg["clv_tracked"], agg["avg_clv"], agg["beat_close_pct"]) == (1, 5.0, 100.0)
    assert s["overall"]["settled"] == 3                              # W/L grading is untouched


def test_calibration_memory_never_learns_from_an_in_play_close(monkeypatch):
    monkeypatch.setattr(config, "SPORT", "wc26")
    paper = _fresh_paper()
    from backend import memory
    for _ in range(25):                                              # enough to clear the gate if counted
        _closed(paper, 0.80, 0.989)
    _closed(paper, 0.60, 0.63, status="pending", closing_at="2026-10-06T16:00:00Z", locked=None)
    st = memory.compute()[("archetype", "favorite_ml")]
    assert st["n_clv"] == 1 and st["metric"] != "clv"
