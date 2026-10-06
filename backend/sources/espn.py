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
from datetime import datetime, timedelta, timezone

import httpx

from .. import config
from ..sports import active


def _base() -> str:
    """ESPN's hidden site API shares one URL shape across sports; the league path is the adapter's."""
    return f"https://site.api.espn.com/apis/site/v2/sports/{active().espn_path}"

# cache key holding dates whose scoreboard is fully summarized (every game finished + cached), so the
# 40-day results window doesn't re-hit ESPN for long-past dates on every refresh
_DONE_KEY = "_done"


def _cache_path():
    """Per-sport results cache: MLB games land on the same DATES as WC games, so a shared file's
    done-date markers would wrongly skip the other sport's scoreboard. wc26 keeps the legacy name."""
    a = active()
    if a.key == "wc26":
        return config.ESPN_CACHE_PATH
    return config.ESPN_CACHE_PATH.with_name(f"poly_espn_cache_{a.key}.json")


def _load_results_cache() -> dict:
    """{event_id: {date, goals, scorers:[...], played:[...]}} — FINISHED games are terminal, so once
    summarized we never re-fetch a game's /summary. Persisted so restarts don't re-summarize either."""
    p = _cache_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:  # noqa: BLE001
            pass
    return {}


def _save_results_cache(c: dict) -> None:
    try:
        _cache_path().write_text(json.dumps(c))
    except Exception as exc:  # noqa: BLE001
        print(f"[espn] results cache save failed: {exc}")



def _team_key(team: dict | None) -> str:
    """The ledger key for an ESPN team object. Most sports key on the full name ("Dallas Cowboys");
    college football keys on the school ("Ohio State"), because Kalshi labels schools, and a Power 4
    team's opponent changes every week, so no hand-written alias map could cover them all."""
    from ..matching import normalize_team
    t = team or {}
    return normalize_team(t.get(active().espn_team_field) or t.get("displayName") or "")


def _sb_params(yyyymmdd: str) -> dict:
    """Scoreboard query: the date plus any sport-specific params. College football asks explicitly
    for every FBS game, since ESPN's default view narrows to ranked games at some points in a season."""
    return {"dates": yyyymmdd, **dict(active().espn_scoreboard_params)}

async def _scoreboard_events(client: httpx.AsyncClient, yyyymmdd: str) -> dict:
    """{frozenset(team_key, team_key): event_id} for one date."""
    from ..matching import normalize_team
    try:
        r = await client.get(f"{_base()}/scoreboard", params=_sb_params(yyyymmdd), timeout=15)
        events = r.json().get("events", []) if r.status_code == 200 else []
    except Exception as exc:  # noqa: BLE001
        print(f"[espn] scoreboard {yyyymmdd} failed: {exc}")
        return {}
    out = {}
    for ev in events:
        try:
            comps = ev["competitions"][0]["competitors"]
            keys = frozenset(_team_key(c["team"]) for c in comps)
            if ev.get("id") and len(keys) >= 2:
                out[keys] = ev["id"]
        except (KeyError, IndexError, TypeError):
            continue
    return out


def tbd_key(pair: frozenset, date_iso: str) -> tuple:
    """The kickoff-map key that marks a game whose ESPN start is a date-only placeholder. It rides in the
    same map as the start times, so it is cached, pruned and refreshed exactly like them."""
    return (pair, date_iso, "tbd")


async def fetch_kickoffs(client: httpx.AsyncClient, dates: list[str]) -> dict:
    """{frozenset(team_key, team_key): kickoff_iso} for the given dates. ESPN's scoreboard carries the
    exact kickoff time (Kalshi/our markets only know the date), so this is what lets the ledger order
    same-day games by who actually plays first. dates are 'YYYYMMDD'.

    Not every listed time is real: a college game waiting on its TV window is filed at local midnight
    (04:00Z) with competitions[0].timeValid false. Its start is still returned, for ordering, and
    tbd_key(pair, date) is set beside it, so the ledger never locks or misses a game off a placeholder.

    The same scoreboard also carries each game's venue, roof, neutral-site and conference flags; those
    are kept in GAME_CONTEXT under the same (pair, date) key for the research layer, at no extra call."""
    from ..matching import normalize_team
    out: dict = {}
    for d in sorted(set(dates)):
        try:
            r = await client.get(f"{_base()}/scoreboard", params=_sb_params(d), timeout=15)
            events = r.json().get("events", []) if r.status_code == 200 else []
        except Exception as exc:  # noqa: BLE001
            print(f"[espn] kickoff scoreboard {d} failed: {exc}")
            continue
        for ev in events:
            try:
                comps = ev["competitions"][0]["competitors"]
                keys = frozenset(_team_key(c["team"]) for c in comps)
                if ev.get("date") and len(keys) >= 2:
                    out[keys] = ev["date"]           # pair-keyed (WC: a pair plays once in the slate)
                    # date-qualified key for daily sports: a series repeats the same pair across days,
                    # so the pair alone would smear one game's start time over the whole series
                    out[(keys, _iso(d))] = ev["date"]
                    # the marker follows the start it describes: a doubleheader's later listing replaces both
                    if ev["competitions"][0].get("timeValid") is False:
                        out[tbd_key(keys, _iso(d))] = True
                    else:
                        out.pop(tbd_key(keys, _iso(d)), None)
                    GAME_CONTEXT[(keys, _iso(d))] = game_context(ev)
            except (KeyError, IndexError, TypeError):
                continue
    return out


# (pair, date) -> the scoreboard's game context; refreshed whenever the kickoffs are
GAME_CONTEXT: dict = {}

# ESPN venues it marks open-air that have a fixed roof over the field (SoFi's canopy), so wind and rain
# never reach the game
_COVERED_VENUES = {"SoFi Stadium"}


def game_context(ev: dict) -> dict:
    """One scoreboard event's research context: event id, home/away keys, venue and roof, neutral site,
    conference game, week, each team's ESPN id and season passing leader (the starting QB)."""
    comp = ev["competitions"][0]
    venue = comp.get("venue") or {}
    addr = venue.get("address") or {}
    teams, ids, passers, names = {}, {}, {}, {}
    for c in comp.get("competitors") or []:
        key = _team_key(c.get("team"))
        teams[c.get("homeAway")] = key
        t = c.get("team") or {}
        if t.get(active().espn_team_field) or t.get("displayName"):     # "Texas A&M", "Dallas Cowboys"
            names[key] = t.get(active().espn_team_field) or t.get("displayName")
        if (c.get("team") or {}).get("id"):
            ids[str(c["team"]["id"])] = key
        for lead in c.get("leaders") or []:
            if lead.get("name") == "passingLeader" and lead.get("leaders"):
                ath = lead["leaders"][0].get("athlete") or {}
                if ath.get("displayName"):
                    passers[key] = ath["displayName"]
    indoor = venue.get("indoor")
    if venue.get("fullName") in _COVERED_VENUES:
        indoor = True
    return {"event_id": str(ev.get("id") or ""), "kickoff_iso": ev.get("date"),
            "home": teams.get("home"), "away": teams.get("away"), "team_ids": ids, "passers": passers,
            "names": names,
            "venue": venue.get("fullName"), "city": addr.get("city"), "state": addr.get("state"),
            "country": addr.get("country"), "indoor": indoor if isinstance(indoor, bool) else None,
            "neutral": comp.get("neutralSite"), "conference_game": comp.get("conferenceCompetition"),
            "week": (ev.get("week") or {}).get("number")}


_INJURY_STATUSES = {"out", "doubtful", "questionable"}


def game_extras(summary: dict, ctx: dict) -> dict:
    """From an event summary: the starting QB's injury status per team (the passing leader listed out,
    doubtful or questionable) and DraftKings' moneyline, spread and total (ESPN's pickcenter). Injured
    reserve is left out on purpose: a QB on IR is old news the line has carried for weeks."""
    qb: dict = {}
    for t in summary.get("injuries") or []:
        key = _team_key(t.get("team"))
        starter = (ctx.get("passers") or {}).get(key)
        for inj in t.get("injuries") or []:
            ath = inj.get("athlete") or {}
            pos = (ath.get("position") or {}).get("abbreviation")
            status = (inj.get("status") or "").strip()
            if pos == "QB" and status.lower() in _INJURY_STATUSES and ath.get("displayName") == starter:
                qb[key] = {"name": starter, "status": status}
    dk = None
    ids = ctx.get("team_ids") or {}
    for p in summary.get("pickcenter") or []:
        home, away = p.get("homeTeamOdds") or {}, p.get("awayTeamOdds") or {}
        hk, ak = ids.get(str(home.get("teamId"))), ids.get(str(away.get("teamId")))
        if not hk or not ak:
            continue
        dk = {"book": (p.get("provider") or {}).get("name") or "DraftKings",
              "ml": {hk: home.get("moneyLine"), ak: away.get("moneyLine")}}
        spread = p.get("spread")
        if spread:                               # home perspective: -3.5 means the home team gives 3.5
            fav, dog = (hk, ak) if spread < 0 else (ak, hk)
            dk["spread"] = {"team": fav, "line": abs(spread),
                            "cover": (home if fav == hk else away).get("spreadOdds"),
                            "dog": (away if fav == hk else home).get("spreadOdds")}
        if p.get("overUnder"):
            dk["total"] = {"line": p["overUnder"], "over": p.get("overOdds"), "under": p.get("underOdds")}
        break
    return {"qb": qb, "dk": dk}


async def fetch_game_extras(client: httpx.AsyncClient, ctx: dict) -> dict | None:
    """The event summary's QB injury status and DraftKings line for one game (None if ESPN fails)."""
    if not ctx.get("event_id"):
        return None
    try:
        r = await client.get(f"{_base()}/summary", params={"event": ctx["event_id"]}, timeout=15)
        if r.status_code != 200:
            return None
        return game_extras(r.json(), ctx)
    except Exception as exc:  # noqa: BLE001
        print(f"[espn] extras {ctx.get('event_id')} failed: {exc}")
        return None


async def fetch_week_starts(client: httpx.AsyncClient, week: int, seasontype: int = 2) -> dict:
    """{team_key: [kickoff_iso, ...]} for every game in one scoreboard week, for days-of-rest math."""
    params = {"seasontype": seasontype, "week": week, **dict(active().espn_scoreboard_params)}
    try:
        r = await client.get(f"{_base()}/scoreboard", params=params, timeout=15)
        events = r.json().get("events", []) if r.status_code == 200 else []
    except Exception as exc:  # noqa: BLE001
        print(f"[espn] week {week} scoreboard failed: {exc}")
        return {}
    out: dict = {}
    for ev in events:
        try:
            for c in ev["competitions"][0]["competitors"]:
                out.setdefault(_team_key(c["team"]), []).append(ev["date"])
        except (KeyError, IndexError, TypeError):
            continue
    return out


async def _summary_xi(client: httpx.AsyncClient, event_id: str) -> dict:
    """{team_key: {formation, xi:[names]}} — only teams whose official XI has actually posted."""
    from ..matching import normalize_team
    try:
        r = await client.get(f"{_base()}/summary", params={"event": event_id}, timeout=15)
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


_FOOTBALL_GROUPS = {"passing", "rushing", "receiving", "fumbles", "defensive", "interceptions",
                    "kickreturns", "puntreturns", "kicking", "punting"}


def _int(x) -> int:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return 0                      # ESPN prints "--" for an empty cell


def _football_lines(summary: dict) -> dict:
    """{player_key: {stat label: n}} from an NFL box score, in the stat labels the NFL adapter's prop
    series carry. ESPN lists a player under a group only when he records something there, so a receiver
    who played and caught nothing is simply absent from "receiving". Anyone who appears in ANY group
    played, so every stat he did not record is 0, which is how Kalshi settles a player who took a snap.
    A player in no group at all is left out, and his props void (Kalshi settles a no-snap player at
    the pre-game price). "touchdowns" counts every way of SCORING one; a passer's TD throws are not
    his touchdowns, so passing TDs stay out, exactly as Kalshi's anytime-TD rule reads."""
    from ..matching import normalize_team
    raw: dict = {}
    for tm in ((summary.get("boxscore") or {}).get("players") or []):
        for grp in (tm.get("statistics") or []):
            gtype = (grp.get("type") or grp.get("name") or "").lower()
            if gtype not in _FOOTBALL_GROUPS:
                continue
            keys = grp.get("keys") or []
            for a in (grp.get("athletes") or []):
                nm = normalize_team(((a.get("athlete") or {}).get("displayName")) or "")
                if not nm:
                    continue
                st = a.get("stats") or []
                v = lambda k: _int(st[keys.index(k)]) if k in keys and keys.index(k) < len(st) else 0
                d = raw.setdefault(nm, {})
                if gtype == "passing":
                    ca = keys.index("completions/passingAttempts") if "completions/passingAttempts" in keys else -1
                    comp, _, att = (str(st[ca]) if 0 <= ca < len(st) else "").partition("/")
                    d.update({"passing completions": _int(comp), "passing attempts": _int(att),
                              "passing yards": v("passingYards"), "passing touchdowns": v("passingTouchdowns"),
                              "interceptions thrown": v("interceptions")})
                elif gtype == "rushing":
                    d.update({"rushing attempts": v("rushingAttempts"), "rushing yards": v("rushingYards"),
                              "_rush_td": v("rushingTouchdowns")})
                elif gtype == "receiving":
                    d.update({"receptions": v("receptions"), "receiving yards": v("receivingYards"),
                              "_rec_td": v("receivingTouchdowns")})
                elif gtype == "kickreturns":
                    d["_kr_td"] = v("kickReturnTouchdowns")
                elif gtype == "puntreturns":
                    d["_pr_td"] = v("puntReturnTouchdowns")
                elif gtype == "defensive":
                    d["_def_td"] = v("defensiveTouchdowns")
                elif gtype == "interceptions":
                    d["_int_td"] = v("interceptionTouchdowns")
    out: dict = {}
    for nm, d in raw.items():
        g = lambda k: d.get(k, 0)
        out[nm] = {
            "passing yards": g("passing yards"), "passing touchdowns": g("passing touchdowns"),
            "passing attempts": g("passing attempts"), "passing completions": g("passing completions"),
            "interceptions thrown": g("interceptions thrown"),
            "rushing yards": g("rushing yards"), "rushing attempts": g("rushing attempts"),
            "receiving yards": g("receiving yards"), "receptions": g("receptions"),
            "rushing and receiving yards": g("rushing yards") + g("receiving yards"),
            # a defensive return TD can show under both "defensive" and "interceptions": count it once
            "touchdowns": (g("_rush_td") + g("_rec_td") + g("_kr_td") + g("_pr_td")
                           + max(g("_def_td"), g("_int_td"))),
        }
    return out


def _player_lines(summary: dict) -> dict:
    """{player_key: {"hits": n, "home runs": n, "outs recorded": n}} from an MLB box score, or the NFL
    stat lines from a football box score; {} for sports with neither (soccer, hockey). Outs come from
    innings pitched: '5.2' = 17."""
    from ..matching import normalize_team
    groups = {(g.get("type") or g.get("name") or "").lower()
              for tm in ((summary.get("boxscore") or {}).get("players") or [])
              for g in (tm.get("statistics") or [])}
    if groups & {"passing", "rushing", "receiving"}:
        return _football_lines(summary)
    out: dict = {}
    for tm in ((summary.get("boxscore") or {}).get("players") or []):
        for grp in (tm.get("statistics") or []):
            gtype = (grp.get("type") or grp.get("name") or "").lower()
            keys = grp.get("keys") or []
            if gtype == "batting" and "hits" in keys and "homeRuns" in keys:
                hi, hri = keys.index("hits"), keys.index("homeRuns")
                for a in (grp.get("athletes") or []):
                    nm = normalize_team(((a.get("athlete") or {}).get("displayName")) or "")
                    st = a.get("stats") or []
                    if not nm or len(st) <= max(hi, hri):
                        continue
                    try:
                        d = out.setdefault(nm, {})
                        d["hits"] = int(st[hi])
                        d["home runs"] = int(st[hri])
                    except (TypeError, ValueError):
                        continue
            elif gtype == "pitching" and "fullInnings.partInnings" in keys:
                ii = keys.index("fullInnings.partInnings")
                for a in (grp.get("athletes") or []):
                    nm = normalize_team(((a.get("athlete") or {}).get("displayName")) or "")
                    st = a.get("stats") or []
                    if not nm or len(st) <= ii:
                        continue
                    full, _, part = str(st[ii]).partition(".")
                    try:
                        out.setdefault(nm, {})["outs recorded"] = int(full) * 3 + int(part or 0)
                    except (TypeError, ValueError):
                        continue
    return out


def _box_stats(summary: dict) -> dict:
    """{team_key: {possession(0-1), corners, shots, sot}} from a match summary box score (or {}).
    Possession is stored as a fraction; the rest are raw counts. Free territory/volume signal for the
    corners + performance-aware projections (there is no xG anywhere in ESPN, so this is the dominance read)."""
    from ..matching import normalize_team
    out: dict = {}
    for tm in ((summary.get("boxscore") or {}).get("teams") or []):
        tk = _team_key(tm.get("team"))
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


# (pair, date) -> {start_iso: ESPN status} for the games the results sweep found canceled or postponed.
# Each sweep rebuilds a date from its fresh scoreboard, and a date holding such a game is never marked
# done (the game never completes), so it is re-read until it leaves the window. settle_forecasts voids
# the forecast locked on that start at once: a canceled MLB game sat "awaiting result" for 9 days
# waiting on the window's long-stop. Suspended games are left out on purpose, they resume and finish.
CALLED_OFF: dict = {}
_CALLED_OFF_STATUSES = {"STATUS_CANCELED", "STATUS_POSTPONED"}
# Every start a re-read scoreboard lists, {(pair, date): {start_iso: completed}}. Settlement needs the
# games that are NOT final too: a doubleheader's nightcap must wait while game 1 is the only final,
# even when ESPN has nudged game 1's start toward it, and a lone game delayed hours past its frozen
# start is still that pair's only game that day.
LISTED: dict = {}


def _record_called_off(date_iso: str, events: list) -> None:
    """Replace one scoreboard date's entries in CALLED_OFF and LISTED with what that scoreboard says now."""
    for book in (CALLED_OFF, LISTED):
        for k in [k for k in book if k[1] == date_iso]:
            del book[k]
    for ev in events:
        stype = (ev.get("status") or {}).get("type") or {}
        if not ev.get("date"):
            continue
        try:
            keys = frozenset(_team_key(c["team"]) for c in ev["competitions"][0]["competitors"])
        except (KeyError, IndexError, TypeError):
            continue
        if len(keys) < 2:
            continue
        LISTED.setdefault((keys, date_iso), {})[ev["date"]] = bool(stype.get("completed"))
        if stype.get("name") in _CALLED_OFF_STATUSES:
            CALLED_OFF.setdefault((keys, date_iso), {})[ev["date"]] = stype["name"]


async def fetch_results(client: httpx.AsyncClient, dates: list[str]) -> list[dict]:
    """Finished-game results for settling parlay legs: per game {date, goals{team_key:int},
    scorers:set(normalized names)}. Goals come from the final score; scorers from keyEvents
    (own goals excluded — they don't count for an anytime-scorer). dates are 'YYYYMMDD'.

    Dates whose scoreboard is fully summarized get a done marker: their games are served straight from
    the cache with no scoreboard request, so the 40-day results window costs a handful of calls per
    refresh instead of 40."""
    from ..matching import normalize_team
    cache = _load_results_cache()
    done: set = set(cache.get(_DONE_KEY) or [])
    settle_cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y%m%d")
    new_cached = 0
    new_done = 0
    out: list[dict] = []
    for d in sorted(set(dates)):
        if d in done:                              # every game that day is finished + cached
            iso = _iso(d)
            for eid, c in cache.items():
                if eid != _DONE_KEY and c.get("date") == iso:
                    out.append({"date": c["date"], "goals": c["goals"], "winner": c.get("winner"),
                                "scorers": set(c.get("scorers") or []), "played": set(c.get("played") or []),
                                "box": c.get("box") or {}, "iso": c.get("iso"), "players": c.get("players") or {},
                                "innings": c.get("innings") or {}})
            continue
        try:
            sb = (await client.get(f"{_base()}/scoreboard", params=_sb_params(d), timeout=15)).json()
        except Exception as exc:  # noqa: BLE001
            print(f"[espn] results scoreboard {d} failed: {exc}")
            continue
        events = sb.get("events", [])
        if "events" in sb:                         # a real scoreboard envelope, not an error body
            _record_called_off(_iso(d), events)
        # a date at least 2 days past is terminal once every game is completed + summarized, INCLUDING a
        # genuinely game-free date (empty events in a real scoreboard envelope, e.g. pre-tournament days)
        if d < settle_cutoff and "events" in sb and all(
                (((ev.get("status") or {}).get("type") or {}).get("completed"))
                and str(ev.get("id") or "") in cache and "box" in cache.get(str(ev.get("id") or ""), {})
                for ev in events):
            done.add(d)
            new_done += 1
        for ev in events:
            if not (((ev.get("status") or {}).get("type") or {}).get("completed")):
                continue
            eid = str(ev.get("id") or "")
            need_players = any(mt == "player_prop" for mt, _ in active().kalshi_series.values())
            hit = cache.get(eid)
            if hit and "box" in hit and ("players" in hit or not need_players):
                c = hit                                 # (pre-box / pre-players entries fall through to backfill)
                inn = c.get("innings") or {}
                if not inn:                             # linescores ride the scoreboard we already hold
                    try:
                        for cc in ev["competitions"][0]["competitors"]:
                            ls = cc.get("linescores") or []
                            if ls:
                                inn[_team_key(cc.get("team"))] = [
                                    int(float(x.get("value", 0) or 0)) for x in ls]
                    except (KeyError, IndexError, TypeError, ValueError):
                        inn = {}
                    if inn:
                        c["innings"] = inn
                        new_cached += 1                 # persist the backfill
                out.append({"date": c["date"], "goals": c["goals"], "winner": c.get("winner"),
                            "scorers": set(c.get("scorers") or []), "played": set(c.get("played") or []),
                            "box": c.get("box") or {}, "iso": c.get("iso"), "players": c.get("players") or {},
                            "innings": inn})
                continue
            try:
                comp = ev["competitions"][0]["competitors"]
            except (KeyError, IndexError, TypeError):
                continue
            goals: dict = {}
            innings: dict = {}                     # per-inning runs when the scoreboard carries linescores
            winner = None                          # the advancing team (ESPN's flag includes ET/penalties)
            for cc in comp:
                tk = _team_key(cc.get("team"))
                try:
                    goals[tk] = int(cc.get("score"))
                except (TypeError, ValueError):
                    goals[tk] = None
                if cc.get("winner"):
                    winner = tk
                ls = cc.get("linescores") or []
                if ls:
                    try:
                        innings[tk] = [int(float(x.get("value", 0) or 0)) for x in ls]
                    except (TypeError, ValueError):
                        pass
            if not goals or any(v is None for v in goals.values()):
                continue
            scorers: set = set()
            played: set = set()
            box: dict = {}
            try:
                s = (await client.get(f"{_base()}/summary", params={"event": ev["id"]}, timeout=15)).json()
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
                players = _player_lines(s)
                cache[eid] = {"date": _iso(d), "goals": goals, "winner": winner,
                              "scorers": sorted(scorers), "played": sorted(played), "box": box,
                              "iso": ev.get("date"),    # scheduled start: the doubleheader disambiguator
                              "players": players, "innings": innings}
                new_cached += 1
            except Exception as exc:  # noqa: BLE001
                print(f"[espn] results summary {ev.get('id')} failed: {exc}")
                players = {}
            out.append({"date": _iso(d), "goals": goals, "winner": winner,
                        "scorers": scorers, "played": played, "box": box, "iso": ev.get("date"),
                        "players": players, "innings": innings})
    if new_cached or new_done:
        cache[_DONE_KEY] = sorted(done)
        _save_results_cache(cache)
        if new_cached:
            print(f"[espn] memoized {new_cached} finished game(s); {len(cache) - 1} cached total")
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
