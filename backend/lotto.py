"""Lotto parlays: long, small-stakes weekend parlays (10 to 20 legs for $2 or $3) built only from legs that
make sense on their own.

A long parlay is a lottery ticket. Fifteen 85% favorites all win about 9% of the time, and a book pays less
than that is worth because every leg's margin compounds. So the point is not an edge; it is keeping the
ticket sensible:
  - favorites only (60% or better): no upset picks
  - one leg per game, so no two legs move together
  - nothing with a questionable starting quarterback; a college game with no availability report stays
    in but is labeled, since a 95% favorite is still a sensible leg
  - the legs the research favors marked, so a ticket can lean on them: a DraftKings price that beats our
    number ("value"), an NFL total the wind moved ("research"), the top bettors' side ("top")

This builds the candidate pool across every board that is running; the page assembles the tickets.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

MIN_FAV = 0.60                       # the floor for a moneyline leg: a real favorite, never an upset pick
HEAVY = 0.70
RESEARCH_MOVE = 0.02                 # a total the research moved this far is a research leg


def weekend_end(now: datetime) -> datetime:
    """The end of the coming football weekend: the first Tuesday 12:00 UTC (after Monday night's game)
    at least two days out. Checked on a Tuesday, that is next Tuesday, so the whole weekend is in."""
    t = now.replace(hour=12, minute=0, second=0, microsecond=0)
    t += timedelta(days=(1 - t.weekday()) % 7)            # weekday 1 = Tuesday
    while t - now < timedelta(days=2):
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
