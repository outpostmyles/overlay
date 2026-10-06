"""Lotto parlays: small-stakes weekend parlays ($1 to $5) built to pay $1,000 or more at the book, from legs
that make sense on their own.

The payout sets the odds, and the book takes a cut of every leg. At fair prices a ticket paying 200 times
its stake is a 1-in-200 shot whatever its legs. A sportsbook pays a little under fair on every leg and the
cut compounds (US books hold 20 to 30% on parlays, against about 5% on straight bets), so the same $1,000 at
DraftKings is more like 1 in 250 to 1 in 400. How much a ticket gives up depends on its legs. A leg's cost
is the book's margin per unit of payout it adds (the log of its decimal price), and heavy favorites cost
the most, since it takes so many of them to build a payout: in NFL closing moneylines since 2016, legs
priced 80% or more carried about 2 to 5 times the margin per unit of payout of legs from 35% to 70%
(depending on how the margin is split between the two sides), and DraftKings' college favorites run
higher still. So every ticket is built to reach its payout at DraftKings' prices (a leg without one is
estimated at the weekend's typical margin for its price), three ways, tracked side by side:

  - "efficient": the legs that lose the least to the book per unit of payout, every one priced at
    DraftKings, with up to two moderate underdogs among them. A leg a real price pays more than it is
    worth costs less than nothing and goes first.
  - "favorites": the most likely favorites, sliding to riskier ones only as far as the payout needs.
  - "research": up to five research-flagged favorites and totals first (a price that beats our number,
    an NFL total the wind moved, the top bettors' side), then the most likely favorites.

The legs:
  - favorites at 60% or better, and moderate underdogs (35% to 50%, about +185 or shorter) with a real
    price on them; never a long shot
  - one leg per game, so no two legs move together
  - nothing with a questionable starting quarterback; a college game with no availability report stays
    in but is labeled, since a 95% favorite is still a sensible leg
Every weekend's tickets are frozen and graded whether or not they are bet (store/lottotrack.py), so the
record shows which legs and constructions hold up.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from statistics import median

from . import config
from .engine.odds_math import american_to_decimal, decimal_to_american, prob_to_american

MIN_FAV = 0.60                       # the floor for a moneyline leg: a real favorite, never an upset pick
HEAVY = 0.70
DOG_MIN = 0.35                       # a moderate underdog: about +185 or shorter, never a long shot
RESEARCH_MOVE = 0.02                 # a total the research moved this far is a research leg
MAX_LEGS = 20
MAX_SEED = 5                         # a research-first ticket opens with at most this many research legs
MAX_DOGS = 2                         # a ticket mixes in at most this many moderate underdogs
SEED_FLAGS = {"value", "research", "top"}
STAKES = (1, 2, 3, 4, 5)
TARGETS = (1000, 2500, 5000)
VARIANTS = ("efficient", "favorites", "research")
TRACK_STAKE = 5                      # the tracked tickets are the $5 ones: the best odds at a $1,000 payout
TYPICAL_COST = 0.06                  # margin per unit of payout when no leg on the board has a book price


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


def leg_cost(p: float, american: int | None) -> float | None:
    """The book's margin per unit of payout on a leg: -ln(p x d) / ln(d) for our probability p and the
    book's decimal price d. Negative when the price beats our number (the leg adds value)."""
    if not american or not 0 < p < 1:
        return None
    d = american_to_decimal(american)
    if d <= 1:
        return None
    return round(-math.log(p * d) / math.log(d), 4)


def typical_cost(pool: list[dict], p: float | None = None) -> float:
    """The weekend's median margin per unit of payout across priced legs, from the legs priced near `p` when
    there are at least three (heavy favorites carry far more margin per unit than moderate prices): the
    estimate for a leg the book has no price on yet."""
    costs = [(leg["p"], leg["cost"]) for leg in pool if leg.get("cost") is not None]
    near = [c for q, c in costs if p is not None and abs(q - p) <= 0.08]
    pick = near if len(near) >= 3 else [c for _, c in costs]
    return min(max(median(pick), 0.02), 0.20) if pick else TYPICAL_COST


def book_decimal(leg: dict, cost: float) -> float:
    """The leg's DraftKings decimal price, or an estimate at its own estimated margin (`est_cost`, set by
    candidates) or else `cost` per unit of payout, from -ln(p) = (1 + cost) x ln(d)."""
    if leg.get("dk"):
        return american_to_decimal(leg["dk"])
    c = leg.get("est_cost")
    return (1 / leg["p"]) ** (1 / (1 + (cost if c is None else c)))


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
            dog = next((t for t in ml if t != fav), None)
            dk = rs.get("dk") or {}
            cushion = (rs.get("cushion") or {}).get("game") or 0.02
            report = ["no_report"] if "no_report" in (rs.get("uncertain") or []) else []

            def edge(team: str) -> dict | None:
                """The best real price on `team` that beats our number by the cushion, if any."""
                p_t, best = ml.get(team) or 0, None
                ev = (dk.get("ev") or {}).get(team)
                if ev is not None and ev >= cushion:
                    best = {"venue": dk.get("book") or "DraftKings", "price": (dk.get("ml") or {}).get(team), "ev": ev}
                ask = (g.get("kalshi_ask") or {}).get(team)
                if p_t and ask and 0 < ask < 1:
                    cost = ask + config.KALSHI_FEE_COEF * ask * (1 - ask)       # Kalshi's taker fee
                    k_ev = round(p_t / cost - 1, 4)
                    if k_ev >= cushion and (best is None or k_ev > best["ev"]):
                        best = {"venue": "Kalshi", "price": prob_to_american(cost), "ev": k_ev}
                return best

            def ml_leg(team: str, p_t: float, flags: list, e: dict | None) -> dict:
                price = (dk.get("ml") or {}).get(team)
                return {**base, "kind": "ml", "team": team, "p": round(p_t, 4), "label": f"{show(team)} ML",
                        "dk": price, "dk_ev": (dk.get("ev") or {}).get(team), "cost": leg_cost(p_t, price),
                        "edge": e, "flags": flags}

            p = ml[fav] or 0
            if p >= MIN_FAV:
                e = edge(fav)
                out.append(ml_leg(fav, p, (["heavy"] if p >= HEAVY else []) + (["value"] if e else [])
                                  + (["top"] if rs.get("top") == fav else []) + report, e))
            p_dog = (ml.get(dog) or 0) if dog else 0
            if dog and DOG_MIN <= p_dog < 0.5:
                e = edge(dog)
                if e or (dk.get("ml") or {}).get(dog):   # a real price on it: DraftKings', or one that beats ours
                    out.append(ml_leg(dog, p_dog, ["dog"] + (["value"] if e else [])
                                      + (["top"] if rs.get("top") == dog else []) + report, e))
            for leg in g.get("legs") or []:
                if leg.get("key") != "total_goals":
                    continue
                r_over, m_over = _p_over(leg, "research_prob"), _p_over(leg, "prob")
                if r_over is None or m_over is None or abs(r_over - m_over) < RESEARCH_MOVE:
                    continue
                d = "over" if r_over > m_over else "under"
                out.append({**base, "kind": "total", "dir": d, "line": leg["line"],
                            "p": round(r_over if d == "over" else 1 - r_over, 4),
                            "label": f"{d.title()} {leg['line']:g}", "dk": None, "dk_ev": None, "cost": None,
                            "flags": ["research"]})
    for leg in out:
        if leg.get("cost") is None:
            leg["est_cost"] = typical_cost(out, leg["p"])
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


def _dog(leg: dict) -> bool:
    return "dog" in leg["flags"]


def _tighten(chosen: list[dict], options: list[dict], need: float, units, fixed: list[dict] = ()) -> list[dict]:
    """Tighten a ticket that reached `need` units of payout, so it pays the target with as little to spare
    as the legs allow (every unit over the target is chance thrown away): swap its last leg for the
    likeliest unused option that still reaches, then drop any leg the payout no longer needs, likeliest to
    lose first. `fixed` legs (a research seed) stay; the underdog limit holds throughout."""
    total = sum(units(leg) for leg in list(fixed) + chosen)
    last = chosen.pop()
    total -= units(last)
    games = {leg["dedup"] for leg in list(fixed) + chosen}
    dogs = sum(map(_dog, list(fixed) + chosen))
    fits = [leg for leg in options if leg["dedup"] not in games and total + units(leg) >= need
            and not (_dog(leg) and dogs >= MAX_DOGS)]
    best = max(fits + [last], key=lambda leg: leg["p"])
    chosen.append(best)
    total += units(best)
    for leg in sorted(chosen, key=lambda leg: leg["p"]):
        if total - units(leg) >= need:
            chosen.remove(leg)
            total -= units(leg)
    return chosen


def _efficient(pool: list[dict], need: float, max_legs: int) -> tuple[list[dict], bool]:
    """The DraftKings-priced legs that reach `need` units of payout (the log of payout over stake) losing the
    least to the book: cheapest per unit first, which also puts any leg priced above our number first, at
    most two underdogs, then tightened."""
    priced = sorted([leg for leg in pool if leg["kind"] == "ml" and leg.get("dk") and leg.get("cost") is not None],
                    key=lambda leg: (leg["cost"], -leg["p"]))
    units = lambda leg: math.log(american_to_decimal(leg["dk"]))              # noqa: E731
    chosen, total = [], 0.0
    for leg in priced:
        if total >= need or len(chosen) == max_legs:
            break
        if any(c["dedup"] == leg["dedup"] for c in chosen) or (_dog(leg) and sum(map(_dog, chosen)) == MAX_DOGS):
            continue
        chosen.append(leg)
        total += units(leg)
    if total < need:
        return chosen, False
    return _tighten(chosen, priced, need, units), True


def build_ticket(pool: list[dict], stake: float, target: float, variant: str = "favorites",
                 max_legs: int = MAX_LEGS, cost: float | None = None) -> dict:
    """A ticket that pays at least `target` on `stake` at DraftKings' prices (a leg without one estimated at
    margin `cost`, the weekend's typical margin by default). "efficient" takes the legs that lose the least
    to the book (see _efficient). "favorites" takes the most likely favorites from the top of the pool
    until the payout is reached; when even the 20 most likely are too safe, it slides the 20-leg window
    down one favorite at a time until it pays, then tightens it (see _tighten). "research" does the same
    after up to five flagged legs. `reached` is False when no 20 sensible legs pay the target this weekend (the ticket is then the
    biggest one available)."""
    cost = typical_cost(pool) if cost is None else cost
    need = math.log(target / stake)
    units = lambda leg: math.log(book_decimal(leg, cost))                       # noqa: E731
    if variant == "efficient":
        legs, reached = _efficient(pool, need, max_legs)
    else:
        seed = []
        if variant == "research":
            seed = _one_per_game([leg for leg in pool if SEED_FLAGS & set(leg["flags"])
                                  and "dog" not in leg["flags"]])[:MAX_SEED]
        used = {leg["dedup"] for leg in seed}
        rest = [leg for leg in pool if leg["kind"] == "ml" and "dog" not in leg["flags"] and leg["dedup"] not in used]
        room = max(max_legs - len(seed), 0)
        chosen = None
        for k in range(0, max(1, len(rest) - room + 1)):
            window, total = [], sum(units(leg) for leg in seed)
            for leg in rest[k:]:
                if total >= need or len(window) == room:
                    break
                window.append(leg)
                total += units(leg)
            if total >= need:
                chosen = _tighten(window, rest, need, units, fixed=seed) if window else window
                break
        reached = chosen is not None
        if not reached:
            chosen = rest[-room:] if room else []
        legs = seed + chosen
    legs = sorted(legs, key=lambda leg: leg.get("kickoff_iso") or "")
    p = _product(leg["p"] for leg in legs)
    book = _product(book_decimal(leg, cost) for leg in legs) if legs else None
    priced = sum(1 for leg in legs if leg.get("dk"))
    return {"variant": variant, "stake": stake, "target": target, "legs": legs, "p": round(p, 8),
            "reached": reached, "fair_payout": round(stake / p, 2) if p else None,
            "dk_payout": round(stake * book, 2) if book else None, "dk_priced": priced,
            "dk_price": decimal_to_american(book) if book and legs and priced == len(legs) else None,
            "dogs": sum(1 for leg in legs if "dog" in leg["flags"])}


def tickets(pool: list[dict]) -> list[dict]:
    """Every stake, target and construction for the live pool (the page picks one to show)."""
    cost = typical_cost(pool)
    return [build_ticket(pool, s, t, v, cost=cost) for s in STAKES for t in TARGETS for v in VARIANTS]
