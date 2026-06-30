"""ESPN hidden API (free, no key) — structured CONFIRMED starting XI per match.

`site.api.espn.com`, league slug `fifa.world`. No auth, no key, no Cloudflare (plain httpx works).
Two steps: (1) the date scoreboard maps a matchup to an ESPN event id; (2) the event summary's
`rosters[]` carries each team's formation + starting XI.

IMPORTANT timing: ESPN only populates the official XI ~1 HOUR before kickoff (when the teamsheet
drops). Earlier than that, `formation` is null and `roster` is empty — so we only attempt today's
games, and when nothing is posted yet we return nothing and the AI falls back to a web-searched
PROJECTED lineup. There is no free structured injury/suspension feed — that stays on web search.
"""
from __future__ import annotations

import asyncio
import json

import httpx

from .. import config

_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world"


def _load_results_cache() -> dict:
    """{event_id: {date, goals, scorers:[...], played:[...]}} — FINISHED games are terminal, so once
    summarized we never re-fetch a game's /summary. Persisted so restarts don't re-summarize either."""
    p = config.ESPN_CACHE_PATH
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:  # noqa: BLE001
            pass
    return {}


def _save_results_cache(c: dict) -> None:
    try:
        config.ESPN_CACHE_PATH.write_text(json.dumps(c))
    except Exception as exc:  # noqa: BLE001
        print(f"[espn] results cache save failed: {exc}")


async def _scoreboard_events(client: httpx.AsyncClient, yyyymmdd: str) -> dict:
    """{frozenset(team_key, team_key): event_id} for one date."""
    from ..matching import normalize_team
    try:
        r = await client.get(f"{_BASE}/scoreboard", params={"dates": yyyymmdd}, timeout=15)
        events = r.json().get("events", []) if r.status_code == 200 else []
    except Exception as exc:  # noqa: BLE001
        print(f"[espn] scoreboard {yyyymmdd} failed: {exc}")
        return {}
    out = {}
    for ev in events:
        try:
            comps = ev["competitions"][0]["competitors"]
            keys = frozenset(normalize_team(c["team"]["displayName"]) for c in comps)
            if ev.get("id") and len(keys) >= 2:
                out[keys] = ev["id"]
        except (KeyError, IndexError, TypeError):
            continue
    return out


async def fetch_kickoffs(client: httpx.AsyncClient, dates: list[str]) -> dict:
    """{frozenset(team_key, team_key): kickoff_iso} for the given dates. ESPN's scoreboard carries the
    exact kickoff time (Kalshi/our markets only know the date), so this is what lets the ledger order
    same-day games by who actually plays first. dates are 'YYYYMMDD'."""
    from ..matching import normalize_team
    out: dict = {}
    for d in sorted(set(dates)):
        try:
            r = await client.get(f"{_BASE}/scoreboard", params={"dates": d}, timeout=15)
            events = r.json().get("events", []) if r.status_code == 200 else []
        except Exception as exc:  # noqa: BLE001
            print(f"[espn] kickoff scoreboard {d} failed: {exc}")
            continue
        for ev in events:
            try:
                comps = ev["competitions"][0]["competitors"]
                keys = frozenset(normalize_team(c["team"]["displayName"]) for c in comps)
                if ev.get("date") and len(keys) >= 2:
                    out[keys] = ev["date"]           # full ISO, e.g. 2026-06-21T16:00Z
            except (KeyError, IndexError, TypeError):
                continue
    return out


async def _summary_xi(client: httpx.AsyncClient, event_id: str) -> dict:
    """{team_key: {formation, xi:[names]}} — only teams whose official XI has actually posted."""
    from ..matching import normalize_team
    try:
        r = await client.get(f"{_BASE}/summary", params={"event": event_id}, timeout=15)
        data = r.json() if r.status_code == 200 else {}
    except Exception as exc:  # noqa: BLE001
        print(f"[espn] summary {event_id} failed: {exc}")
        return {}
    out = {}
    for tobj in (data.get("rosters") or []):
        formation = tobj.get("formation")
        starters = [p for p in (tobj.get("roster") or []) if p.get("starter")]
        if not formation or len(starters) < 11:
            continue  # XI not posted yet (or partial) — skip; AI uses projected lineup
        starters.sort(key=lambda p: int(p.get("formationPlace") or 99))
        team = ((tobj.get("team") or {}).get("displayName")) or ""
        xi = [(p.get("athlete") or {}).get("displayName") for p in starters]
        out[normalize_team(team)] = {"formation": formation,
                                     "xi": [n for n in xi if n]}
    return out


def _iso(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


_BOX_FIELDS = {"possessionPct": "possession", "wonCorners": "corners",
               "totalShots": "shots", "shotsOnTarget": "sot"}


def _box_stats(summary: dict) -> dict:
    """{team_key: {possession(0-1), corners, shots, sot}} from a match summary box score (or {}).
    Possession is stored as a fraction; the rest are raw counts. Free territory/volume signal for the
    corners + performance-aware projections (there is no xG anywhere in ESPN, so this is the dominance read)."""
    from ..matching import normalize_team
    out: dict = {}
    for tm in ((summary.get("boxscore") or {}).get("teams") or []):
        tk = normalize_team(((tm.get("team") or {}).get("displayName")) or "")
        if not tk:
            continue
        d: dict = {}
        for st in (tm.get("statistics") or []):
            key = _BOX_FIELDS.get(st.get("name"))
            if not key:
                continue
            try:
                v = float(st.get("displayValue"))
            except (TypeError, ValueError):
                continue
            d[key] = v / 100.0 if key == "possession" else v
        if d:
            out[tk] = d
    return out


async def fetch_results(client: httpx.AsyncClient, dates: list[str]) -> list[dict]:
    """Finished-game results for settling parlay legs: per game {date, goals{team_key:int},
    scorers:set(normalized names)}. Goals come from the final score; scorers from keyEvents
    (own goals excluded — they don't count for an anytime-scorer). dates are 'YYYYMMDD'."""
    from ..matching import normalize_team
    cache = _load_results_cache()
    new_cached = 0
    out: list[dict] = []
    for d in sorted(set(dates)):
        try:
            sb = (await client.get(f"{_BASE}/scoreboard", params={"dates": d}, timeout=15)).json()
        except Exception as exc:  # noqa: BLE001
            print(f"[espn] results scoreboard {d} failed: {exc}")
            continue
        for ev in sb.get("events", []):
            if not (((ev.get("status") or {}).get("type") or {}).get("completed")):
                continue
            eid = str(ev.get("id") or "")
            if eid in cache and "box" in cache[eid]:   # finished + box already extracted, no /summary re-fetch
                c = cache[eid]                          # (entries cached before box existed fall through to backfill)
                out.append({"date": c["date"], "goals": c["goals"], "winner": c.get("winner"),
                            "scorers": set(c.get("scorers") or []), "played": set(c.get("played") or []),
                            "box": c.get("box") or {}})
                continue
            try:
                comp = ev["competitions"][0]["competitors"]
            except (KeyError, IndexError, TypeError):
                continue
            goals: dict = {}
            winner = None                          # the advancing team (ESPN's flag includes ET/penalties)
            for cc in comp:
                tk = normalize_team((cc.get("team") or {}).get("displayName"))
                try:
                    goals[tk] = int(cc.get("score"))
                except (TypeError, ValueError):
                    goals[tk] = None
                if cc.get("winner"):
                    winner = tk
            if not goals or any(v is None for v in goals.values()):
                continue
            scorers: set = set()
            played: set = set()
            box: dict = {}
            try:
                s = (await client.get(f"{_BASE}/summary", params={"event": ev["id"]}, timeout=15)).json()
                for ke in (s.get("keyEvents") or []):
                    if not ke.get("scoringPlay") or ke.get("shootout"):
                        continue
                    if "own goal" in ((ke.get("type") or {}).get("text") or "").lower():
                        continue
                    ath = ke.get("athletesInvolved") or ke.get("participants") or []
                    nm = (ath[0].get("displayName") or (ath[0].get("athlete") or {}).get("displayName")) if ath else None
                    if nm:
                        scorers.add(normalize_team(nm))
                # who actually appeared (started or subbed in) — for voiding DNP player props
                for t in (s.get("rosters") or []):
                    for p in (t.get("roster") or []):
                        if p.get("starter") or p.get("subbedIn"):
                            nm = (p.get("athlete") or {}).get("displayName")
                            if nm:
                                played.add(normalize_team(nm))
                box = _box_stats(s)
                cache[eid] = {"date": _iso(d), "goals": goals, "winner": winner,
                              "scorers": sorted(scorers), "played": sorted(played), "box": box}
                new_cached += 1
            except Exception as exc:  # noqa: BLE001
                print(f"[espn] results summary {ev.get('id')} failed: {exc}")
            out.append({"date": _iso(d), "goals": goals, "winner": winner,
                        "scorers": scorers, "played": played, "box": box})
    if new_cached:
        _save_results_cache(cache)
        print(f"[espn] memoized {new_cached} finished game(s); {len(cache)} cached total")
    return out


async def fetch_lineups(client: httpx.AsyncClient, matchups: list[dict]) -> dict:
    """{team_key: {formation, xi[]}} for TODAY's games whose official XI has posted (~1h pre-KO)."""
    today = [mu for mu in matchups if mu.get("days_out") == 0 and mu.get("commence_time")]
    if not today:
        return {}
    dates = sorted({mu["commence_time"][:10].replace("-", "") for mu in today})
    index: dict = {}
    for d in dates:
        index.update(await _scoreboard_events(client, d))

    sem = asyncio.Semaphore(4)

    async def _one(mu):
        eid = index.get(frozenset({mu["fav_key"], mu["opp_key"]}))
        if not eid:
            return {}
        async with sem:
            return await _summary_xi(client, eid)

    out: dict = {}
    for res in await asyncio.gather(*[_one(mu) for mu in today]):
        out.update(res)
    if out:
        print(f"[espn] confirmed XI posted for {len(out)} team(s) on today's slate")
    return out
