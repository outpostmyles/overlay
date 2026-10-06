"""Lotto parlays: small-stakes weekend parlays ($2 or $3) built to pay $1,000 or more, from legs that make
sense on their own.

A $1,000 ticket on $2 is a 1-in-500 shot at fair odds, whatever legs it uses: the payout sets the odds. So the
builder's job is to reach the payout with the most sensible legs it can: the most likely favorites that,
together, pay the target, sliding down to riskier favorites (never below 60%) only as far as the target
needs, and never past 20 legs. A book pays less than fair because every leg's margin compounds. Every
weekend's tickets are frozen and graded whether or not they are bet (store/lottotrack.py), so the record
shows which legs and which construction hold up. The legs:
  - favorites only (60% or better): no upset picks
  - one leg per game, so no two legs move together
  - nothing with a questionable starting quarterback; a college game with no availability report stays
    in but is labeled, since a 95% favorite is still a sensible leg
  - the legs the research favors marked, so a ticket can lean on them: a DraftKings price that beats our
    number ("value"), an NFL total the wind moved ("research"), the top bettors' side ("top")

Two constructions are built and tracked side by side: "favorites" (the most likely legs only) and
"research" (up to five legs the research favors first, then the most likely legs), so the record can say
whether the research legs earn their place.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .engine.odds_math import american_to_decimal

MIN_FAV = 0.60                       # the floor for a moneyline leg: a real favorite, never an upset pick
HEAVY = 0.70
RESEARCH_MOVE = 0.02                 # a total the research moved this far is a research leg
MAX_LEGS = 20
MAX_SEED = 5                         # a research-first ticket opens with at most this many research legs
SEED_FLAGS = {"value", "research", "top"}
STAKES = (2, 3)
TARGETS = (1000, 2500, 5000)
VARIANTS = ("favorites", "research")
TRACK_STAKE = 2                      # the tracked tickets are the $2 ones (the legs are what is studied)


def weekend_end(now: datetime) -> datetime:
    """The end of the current or coming football weekend: the next Tuesday 12:00 UTC, after Monday night's
    game. Inside its last 12 hours (Monday night's game is underway) it rolls to the following week, so
    from Tuesday on the page is already building next weekend's tickets."""
    t = now.replace(hour=12, minute=0, second=0, microsecond=0)
    t += timedelta(days=(1 - t.weekday()) % 7)            # weekday 1 = Tuesday
    if t <= now:
        t += timedelta(days=7)
    if t - now < timedelta(hours=12):
        t += timedelta(days=7)
    return t


def _parse(iso: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _p_over(leg: dict, key: str) -> float | None:
    p = leg.get(key)
    if p is None:
        return None
    return p if leg.get("side") == "over" else 1 - p


def candidates(boards: dict, now: datetime | None = None) -> list[dict]:
    """Every sensible leg through the coming weekend, most likely first. `boards` is livelegs.read_all():
    each sport's live games plus its team display names."""
    now = now or datetime.now(timezone.utc)
    end = weekend_end(now)
    out = []
    for sport, board in boards.items():
        names = board.get("names") or {}

        def show(team: str) -> str:
            return names.get(team) or " ".join(w[:1].upper() + w[1:] for w in team.split())

        for g in board.get("games") or []:
            ko = _parse(g.get("kickoff_iso"))
            if not ko or not now < ko <= end:
                continue
            rs = g.get("research") or {}
            if "qb" in (rs.get("uncertain") or []):
                continue                     # a questionable starting QB: no lottery ticket needs that
            m = g.get("market") or (0, 0, 0)
            ml = rs.get("ml") or {g["team_a"]: m[0], g["team_b"]: m[2]}
            base = {"sport": sport, "dedup": g["dedup"], "kickoff_iso": g.get("kickoff_iso"),
                    "team_a": g["team_a"], "team_b": g["team_b"],
                    "game": f"{show(g['team_a'])} v {show(g['team_b'])}"}
            fav = max(ml, key=lambda t: ml[t] or 0)
            p = ml[fav] or 0
            if p >= MIN_FAV:
                dk = rs.get("dk") or {}
                ev = (dk.get("ev") or {}).get(fav)
                cushion = (rs.get("cushion") or {}).get("game") or 0.02
                flags = (["heavy"] if p >= HEAVY else []) + (["value"] if ev is not None and ev >= cushion else []) \
                    + (["top"] if rs.get("top") == fav else []) \
                    + (["no_report"] if "no_report" in (rs.get("uncertain") or []) else [])
                out.append({**base, "kind": "ml", "team": fav, "p": round(p, 4), "label": f"{show(fav)} ML",
                            "dk": (dk.get("ml") or {}).get(fav), "dk_ev": ev, "flags": flags})
            for leg in g.get("legs") or []:
                if leg.get("key") != "total_goals":
                    continue
                r_over, m_over = _p_over(leg, "research_prob"), _p_over(leg, "prob")
                if r_over is None or m_over is None or abs(r_over - m_over) < RESEARCH_MOVE:
                    continue
                d = "over" if r_over > m_over else "under"
                out.append({**base, "kind": "total", "dir": d, "line": leg["line"],
                            "p": round(r_over if d == "over" else 1 - r_over, 4),
                            "label": f"{d.title()} {leg['line']:g}", "dk": None, "dk_ev": None, "flags": ["research"]})
    out.sort(key=lambda x: -x["p"])
    return out


def freeze_at(end: datetime) -> datetime:
    """When a weekend's tickets freeze for the record: Saturday 15:00 UTC (11 a.m. Eastern), an hour before
    the first Saturday kickoffs, with the week's injury news in."""
    return end - timedelta(days=3) + timedelta(hours=3)


def _product(xs) -> float:
    out = 1.0
    for x in xs:
        out *= x
    return out


def _one_per_game(legs: list[dict]) -> list[dict]:
    seen, out = set(), []
    for leg in legs:
        if leg["dedup"] not in seen:
            seen.add(leg["dedup"])
            out.append(leg)
    return out


def build_ticket(pool: list[dict], stake: float, target: float, variant: str = "favorites",
                 max_legs: int = MAX_LEGS) -> dict:
    """The most likely legs that pay at least `target` on `stake` at fair odds. Takes favorites from the top
    of the pool until the payout is reached; when even the 20 most likely are too safe, slides the 20-leg
    window down one favorite at a time until it pays. "research" opens with up to five flagged legs.
    `reached` is False when no 20 sensible legs pay the target this weekend (the ticket is then the
    biggest one available)."""
    target_p = stake / target
    seed = _one_per_game([leg for leg in pool if SEED_FLAGS & set(leg["flags"])])[:MAX_SEED] \
        if variant == "research" else []
    used = {leg["dedup"] for leg in seed}
    rest = [leg for leg in pool if leg["kind"] == "ml" and leg["dedup"] not in used]
    seed_p = _product(leg["p"] for leg in seed)
    need = max(max_legs - len(seed), 0)
    chosen = None
    for k in range(0, max(1, len(rest) - need + 1)):
        window, p = [], seed_p
        for leg in rest[k:]:
            if p <= target_p or len(window) == need:
                break
            window.append(leg)
            p *= leg["p"]
        if p <= target_p:
            chosen = window
            break
    reached = chosen is not None
    if not reached:
        chosen = rest[-need:] if need else []
    legs = sorted(seed + chosen, key=lambda leg: leg.get("kickoff_iso") or "")
    p = _product(leg["p"] for leg in legs)
    priced = bool(legs) and all(leg.get("dk") for leg in legs)
    dk_dec = _product(american_to_decimal(leg["dk"]) for leg in legs) if priced else None
    return {"variant": variant, "stake": stake, "target": target, "legs": legs, "p": round(p, 8),
            "reached": reached, "fair_payout": round(stake / p, 2) if p else None,
            "dk_payout": round(stake * dk_dec, 2) if dk_dec else None}


def tickets(pool: list[dict]) -> list[dict]:
    """Every stake, target and construction for the live pool (the page picks one to show)."""
    return [build_ticket(pool, s, t, v) for s in STAKES for t in TARGETS for v in VARIANTS]
