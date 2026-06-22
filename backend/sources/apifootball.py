"""API-Football (api-sports.io v3) — per-player match stats to auto-grade shots/SOT/passes/tackles.

The free tier is 100 requests/day, so we spend it carefully:
  • ONE `/fixtures/players?fixture=<id>` call returns EVERY player's stats for a game — it grades all
    of that game's player props at once (logged or not).
  • A finished game's stats are final, so each fixture is fetched exactly ONCE, ever (cached to disk).
  • Fixture-id lookups (`/fixtures?date=`) are cached per date (past dates never change).
  • A persisted daily counter hard-caps usage at APIFOOTBALL_DAILY_CAP (<100) — it physically can't
    blow the budget. No key set → this whole module is a graceful no-op.

Header auth: `x-apisports-key`. Completed-match status codes: FT / AET / PEN.
"""
from __future__ import annotations

import json
import time
from datetime import date as _date, timedelta

import httpx

from .. import config
from ..matching import normalize_team

_DONE = {"FT", "AET", "PEN"}


def _load() -> dict:
    p = config.APIFOOTBALL_CACHE_PATH
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:  # noqa: BLE001
            pass
    return {"day": "", "count": 0, "fixtures": {}, "stats": {}}


def _save(c: dict) -> None:
    try:
        config.APIFOOTBALL_CACHE_PATH.write_text(json.dumps(c))
    except Exception as exc:  # noqa: BLE001
        print(f"[apifootball] cache save failed: {exc}")


def _pair(a: str, b: str) -> str:
    return "|".join(sorted([a, b]))


def _shift(dstr: str, n: int) -> str:
    try:
        y, m, d = (int(x) for x in dstr.split("-"))
        return (_date(y, m, d) + timedelta(days=n)).isoformat()
    except Exception:  # noqa: BLE001
        return dstr


def _parse_players(resp: dict) -> dict:
    """{player_key: {shots, sot, passes, tackles, minutes, played}} from a /fixtures/players body."""
    out: dict = {}
    for team in resp.get("response", []) or []:
        for p in team.get("players", []) or []:
            name = (p.get("player") or {}).get("name")
            stats = (p.get("statistics") or [{}])[0] or {}
            if not name:
                continue
            g = stats.get("games") or {}
            sh = stats.get("shots") or {}
            ps = stats.get("passes") or {}
            tk = stats.get("tackles") or {}
            mins = g.get("minutes")
            out[normalize_team(name)] = {
                "shots": sh.get("total") or 0,
                "sot": sh.get("on") or 0,
                "passes": ps.get("total") or 0,
                "tackles": tk.get("total") or 0,
                "minutes": mins,
                "played": bool(mins and mins > 0),
            }
    return out


def _parse_team_stats(resp: dict) -> dict:
    """{team_key: {corners, possession(0-1), shots, sot}} from a /fixtures/statistics body. Only emits
    a team whose statistics array actually carried a Corner Kicks reading, and returns {} unless BOTH
    teams did — so a finished-but-stats-not-posted body (which API-Football returns with empty stats)
    reads as a miss (trips the empty-cooldown / stays pending) instead of caching phantom all-zeros."""
    out: dict = {}
    for team in resp.get("response", []) or []:
        name = ((team.get("team") or {}).get("name"))
        if not name:
            continue
        rec = {"corners": 0, "possession": None, "shots": 0, "sot": 0}
        has_corners = False
        for s in team.get("statistics", []) or []:
            t = (s.get("type") or "").strip().lower()
            v = s.get("value")
            if t == "corner kicks":
                rec["corners"] = v or 0
                has_corners = v is not None
            elif t == "ball possession":
                try:
                    rec["possession"] = float(str(v).rstrip("%")) / 100.0 if v not in (None, "") else None
                except ValueError:
                    rec["possession"] = None
            elif t == "total shots":
                rec["shots"] = v or 0
            elif t == "shots on goal":
                rec["sot"] = v or 0
        if has_corners:                       # a real reading (0 corners is fine; missing is not)
            out[normalize_team(name)] = rec
    return out if len(out) >= 2 else {}       # need both teams' real stats, else treat as not-yet-posted


async def _fetch_per_fixture(client, games, store_key, path, parse_fn) -> dict:
    """Generic per-fixture fetch: resolve each game→fixture id (±1 day, cached), GET <path>?fixture=,
    parse, cache forever. Shares the daily budget counter. Returns {(date, frozenset teams): parsed}."""
    if not config.has_apifootball() or not games:
        return {}
    cache = _load()
    today = time.strftime("%Y-%m-%d", time.gmtime())   # UTC day — match api-sports.io's 00:00 UTC quota reset
    if cache.get("day") != today:
        cache["day"], cache["count"] = today, 0
    store = cache.setdefault(store_key, {})
    empty = cache.setdefault("empty", {})              # {store_key:fid -> epoch} of unposted-stats attempts
    now_ep = time.time()
    headers = {"x-apisports-key": config.APIFOOTBALL_KEY}
    out: dict = {}

    def can_spend() -> bool:
        return cache["count"] < config.APIFOOTBALL_DAILY_CAP

    async def _get(p, params):
        if not can_spend():
            return None
        try:
            r = await client.get(f"{config.APIFOOTBALL_BASE}{p}", params=params, headers=headers, timeout=20)
            cache["count"] += 1
            if r.status_code != 200:
                print(f"[apifootball] {p} HTTP {r.status_code}")
                return None
            return r.json()
        except Exception as exc:  # noqa: BLE001
            print(f"[apifootball] {p} failed: {exc}")
            return None

    for date, teams in games:
        tlist = list(teams)
        if len(tlist) < 2:
            continue
        pair = _pair(tlist[0], tlist[1])
        fid = None
        for d in (date, _shift(date, -1), _shift(date, 1)):
            daymap = cache["fixtures"].get(d)
            if daymap is None:
                body = await _get("/fixtures", {"date": d})
                if body is None:
                    continue
                daymap = {}
                for fx in body.get("response", []) or []:
                    tm = fx.get("teams") or {}
                    h = normalize_team((tm.get("home") or {}).get("name"))
                    a = normalize_team((tm.get("away") or {}).get("name"))
                    fxid = (fx.get("fixture") or {}).get("id")
                    if h and a and fxid:
                        daymap[_pair(h, a)] = fxid
                cache["fixtures"][d] = daymap
            if pair in daymap:
                fid = daymap[pair]
                break
        if not fid:
            continue
        fid = str(fid)
        if fid in store:
            out[(date, teams)] = store[fid]
            continue
        ekey = f"{store_key}:{fid}"
        if (now_ep - empty.get(ekey, 0)) < config.APIFOOTBALL_EMPTY_COOLDOWN:
            continue   # stats weren't posted last time we looked — don't re-spend until the cooldown passes
        body = await _get(path, {"fixture": fid})
        if body is None:
            continue
        parsed = parse_fn(body)
        if parsed:
            store[fid] = parsed
            empty.pop(ekey, None)
            out[(date, teams)] = parsed
        else:
            empty[ekey] = now_ep   # finished but stats not posted yet → back off (anti-hammer)

    _save(cache)
    if out:
        print(f"[apifootball] {store_key} for {len(out)} game(s); {cache['count']}/{config.APIFOOTBALL_DAILY_CAP} req today")
    return out


async def fetch_team_stats(client: httpx.AsyncClient, games: list[tuple]) -> dict:
    """{(date, frozenset): {team_key: {corners, possession, shots, sot}}} — team-level match stats."""
    return await _fetch_per_fixture(client, games, "teamstats", "/fixtures/statistics", _parse_team_stats)


async def fetch_player_stats(client: httpx.AsyncClient, games: list[tuple]) -> dict:
    """{(date, frozenset): {player_key: stats}} — shots/SOT/passes/tackles/minutes per player, for the
    games we can resolve within the daily budget. (Shares the budget, per-fixture-forever cache, and
    unposted-stats cooldown with team-stats via _fetch_per_fixture.)"""
    return await _fetch_per_fixture(client, games, "stats", "/fixtures/players", _parse_players)
