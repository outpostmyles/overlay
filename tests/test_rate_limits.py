"""Credit-safety / anti-hammer guards from the API audit: Odds credit floor, decoupled corner refresh
interval, API-Football unposted-stats cooldown, and ESPN finished-game memoization."""
import asyncio

from backend import aggregator, config


class _Resp:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._p = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._p


class _Client:
    """Minimal async httpx stand-in: handler(url, params) -> _Resp; records every call."""
    def __init__(self, handler):
        self._h = handler
        self.calls = []

    async def get(self, url, params=None, **kw):
        self.calls.append((url, params or {}))
        return self._h(url, params or {})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def test_odds_credit_floor_serves_cache(monkeypatch):
    calls = {"n": 0}

    async def fake_fetch(client):
        calls["n"] += 1
        return {"ok": True, "markets": [], "credits_remaining": 1, "error": None}

    monkeypatch.setattr(aggregator.theoddsapi, "fetch", fake_fetch)
    monkeypatch.setattr(config, "ODDS_API_KEY", "x")
    aggregator._odds_state.update(loaded=True, fetched_at=0.0,
                                  credits_remaining=config.ODDS_CREDIT_FLOOR - 1)
    _, meta = asyncio.run(aggregator.get_odds_markets(refresh=True))
    assert calls["n"] == 0                 # below floor → no spend, serve cache
    assert meta["credits_low"] is True


def test_corner_refresh_interval_debounces(monkeypatch):
    calls = {"n": 0}

    async def fake_corners(client, targets=None, max_games=8):
        calls["n"] += 1
        return {}, 400

    monkeypatch.setattr(aggregator.theoddsapi, "fetch_corners", fake_corners)
    monkeypatch.setattr(config, "ODDS_API_KEY", "x")
    aggregator._odds_state.update(loaded=True, credits_remaining=400)
    aggregator._corner_state.update(lines={}, fetched_at=0.0)
    mus = [{"fav_key": "spain", "opp_key": "saudi arabia", "days_out": 0}]
    asyncio.run(aggregator.get_corner_lines(refresh=True, matchups=mus))   # 1st → fetch
    asyncio.run(aggregator.get_corner_lines(refresh=True, matchups=mus))   # within 6h → skip
    assert calls["n"] == 1


def test_corners_back_off_before_h2h_near_limit(monkeypatch):
    calls = {"n": 0}

    async def fake_corners(client, targets=None, max_games=8):
        calls["n"] += 1
        return {}, 1

    monkeypatch.setattr(aggregator.theoddsapi, "fetch_corners", fake_corners)
    monkeypatch.setattr(config, "ODDS_API_KEY", "x")
    aggregator._odds_state.update(loaded=True,
                                  credits_remaining=config.ODDS_CREDIT_FLOOR + config.CORNER_MAX_GAMES - 1)
    aggregator._corner_state.update(lines={}, fetched_at=0.0)
    mus = [{"fav_key": "spain", "opp_key": "saudi arabia", "days_out": 0}]
    asyncio.run(aggregator.get_corner_lines(refresh=True, matchups=mus))
    assert calls["n"] == 0                 # corner floor (= odds floor + max games) reached first


def test_apifootball_empty_stats_cooldown(monkeypatch, tmp_path):
    from backend.sources import apifootball
    monkeypatch.setattr(config, "APIFOOTBALL_KEY", "x")
    monkeypatch.setattr(config, "APIFOOTBALL_CACHE_PATH", tmp_path / "af.json")

    def handler(url, params):
        if url.endswith("/fixtures"):
            return _Resp(payload={"response": [
                {"teams": {"home": {"name": "Spain"}, "away": {"name": "Saudi Arabia"}},
                 "fixture": {"id": 1}}]})
        return _Resp(payload={"response": []})       # players come back EMPTY (stats not posted yet)

    games = [("2026-06-21", frozenset({"spain", "saudi arabia"}))]
    c = _Client(handler)
    asyncio.run(apifootball.fetch_player_stats(c, games))
    asyncio.run(apifootball.fetch_player_stats(c, games))   # within cooldown → must NOT re-spend
    players_calls = sum(1 for u, _ in c.calls if u.endswith("/fixtures/players"))
    assert players_calls == 1


def test_unsettleable_prop_guard():
    """Vague/compound props (no line, or 'A or B') are dropped at log time; valid ones pass."""
    u = aggregator._unsettleable_prop
    assert u("shots_sot", "Cody Gakpo shots or shots-on-target over") is True   # compound
    assert u("shots_sot", "Kane shots over or Saka shots over") is True          # two players
    assert u("shots_sot", "Pedri shots over") is True                            # no numeric line
    assert u("shots_sot", "Pedri Over 2.5 Shots") is False                       # gradeable
    assert u("popular_prop", "Lukaku Shots Over 3.0") is False
    assert u("anytime_goalscorer", "Messi to score") is False                    # name-graded, no line needed
    assert u("favorite_ml", "Belgium ML") is False


def test_props_survive_restart_via_disk(monkeypatch, tmp_path):
    """A restart that lands during a PrizePicks block still serves the last-good board from disk."""
    import json, time as _t
    pf = tmp_path / "props.json"
    monkeypatch.setattr(config, "PROPS_CACHE_PATH", pf)
    pf.write_text(json.dumps({"saved_at": _t.time(), "props": [{"player": "X"}, {"player": "Y"}, {"player": "Z"}]}))
    aggregator._free_cache.update(markets=[], props=[], ts=0.0, loaded=False)   # fresh process

    async def throttled():
        return [], []   # PrizePicks empty

    monkeypatch.setattr(aggregator, "_fetch_free", throttled)
    _, props = asyncio.run(aggregator.get_free(force=True))
    assert len(props) == 3


def test_stale_disk_props_ignored(monkeypatch, tmp_path):
    import json, time as _t
    pf = tmp_path / "props.json"
    monkeypatch.setattr(config, "PROPS_CACHE_PATH", pf)
    old = _t.time() - (config.PROPS_MAX_STALE_DAYS + 1) * 86400
    pf.write_text(json.dumps({"saved_at": old, "props": [{"player": "stale"}]}))
    aggregator._free_cache.update(markets=[], props=[], ts=0.0, loaded=False)

    async def throttled():
        return [], []

    monkeypatch.setattr(aggregator, "_fetch_free", throttled)
    _, props = asyncio.run(aggregator.get_free(force=True))
    assert props == []   # too old -> not served


def test_fresh_props_persist_to_disk(monkeypatch, tmp_path):
    import json
    pf = tmp_path / "props.json"
    monkeypatch.setattr(config, "PROPS_CACHE_PATH", pf)
    aggregator._free_cache.update(markets=[], props=[], ts=0.0, loaded=False)

    async def good():
        return [], [{"player": "A"}, {"player": "B"}]

    monkeypatch.setattr(aggregator, "_fetch_free", good)
    asyncio.run(aggregator.get_free(force=True))
    saved = json.loads(pf.read_text())
    assert len(saved["props"]) == 2 and "saved_at" in saved


def test_espn_memoizes_finished_games(monkeypatch, tmp_path):
    from backend.sources import espn
    monkeypatch.setattr(config, "ESPN_CACHE_PATH", tmp_path / "espn.json")

    def handler(url, params):
        if url.endswith("/scoreboard"):
            return _Resp(payload={"events": [{
                "id": "evt1", "status": {"type": {"completed": True}},
                "competitions": [{"competitors": [
                    {"team": {"displayName": "Spain"}, "score": "2"},
                    {"team": {"displayName": "Saudi Arabia"}, "score": "0"}]}]}]})
        return _Resp(payload={"keyEvents": [], "rosters": []})   # /summary

    c = _Client(handler)
    asyncio.run(espn.fetch_results(c, ["20260621"]))
    r2 = asyncio.run(espn.fetch_results(c, ["20260621"]))       # 2nd: finished game served from cache
    summary_calls = sum(1 for u, _ in c.calls if u.endswith("/summary"))
    assert summary_calls == 1                                   # only the FIRST call hit /summary
    assert r2 and r2[0]["goals"]["spain"] == 2
