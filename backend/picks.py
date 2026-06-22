"""Pick engine — generates candidate bets scoped to the user's archetypes ONLY.

The user's locked archetypes (from their own bet history):
  A favorite_ml        — heavy-favorite moneylines (single + SGP legs)
  B anytime_goalscorer — the favorite's main scorer (PrizePicks "Goals" 0.5)
  C shots_sot          — star attacker Shots / Shots on Target over
  D team_total_over    — favorite team total goals over 1.5 (from the model)
  E popular_props       — the high-popularity Popular-tab props

Everything else is deliberately ignored. This layer produces *candidates + the raw signals*
(favorite prob, model expected goals, popularity, goblin/demon). The reasoning layer
(reasoning.py, needs Anthropic key) turns these into bet/pass/fade verdicts with rationale.
"""
from __future__ import annotations

from datetime import date

from . import propread
from .matching import normalize_team
from .model import parlay as parlaymodel
from .model import props as propmodel
from .models import Market


def _days_out(commence_time) -> int | None:
    """Whole days from today until a match (0 = today). None if undated/unparseable."""
    if not commence_time:
        return None
    try:
        return (date.fromisoformat(str(commence_time)[:10]) - date.today()).days
    except ValueError:
        return None

# stat types that map to the user's archetypes (everything else is filtered out)
_SHOT_STATS = ("Shots", "Shots On Target")
_POPULAR_STATS = ("Passes Attempted", "Shots", "Shots On Target", "Goals", "Assists",
                  "Goal + Assist", "Tackles")


def _read_for(pp: dict, fav_keys: dict, tt_by_team: dict) -> tuple[dict, float | None, float | None]:
    """Deterministic read() for a prop + its match-side context (team-total %, favorite %)."""
    tk = pp.get("team_key")
    fav, tt = fav_keys.get(tk), tt_by_team.get(tk)
    favpct = round(fav["fair_prob"] * 100, 1) if fav else None
    ttover = round(tt["p_over"] * 100, 1) if tt else None
    m = pp.get("_model") or {}
    rd = propread.read({
        "position": pp.get("position"), "stat_type": pp.get("stat_type"),
        "line": pp.get("line"), "odds_type": pp.get("odds_type"),
        "popularity": pp.get("popularity", 0), "on_favorite": tk in fav_keys,
        "ttover": ttover, "fav_fair_pct": favpct,
        "model_prob": m.get("prob"), "model_value": m.get("value"),
    })
    return rd, ttover, favpct


def _prop_row(pp: dict, fav_keys: dict, tt_by_team: dict, **extra) -> dict:
    rd, ttover, favpct = _read_for(pp, fav_keys, tt_by_team)
    return {
        "player": pp.get("player"),
        "team": pp.get("team"),
        "position": pp.get("position"),
        "stat_type": pp.get("stat_type"),
        "line": pp.get("line"),
        "odds_type": pp.get("odds_type"),
        "popularity": pp.get("popularity", 0),
        "opponent": pp.get("opponent"),
        "start_time": pp.get("start_time"),
        "days_out": _days_out(pp.get("start_time")),
        "ttover": ttover, "fav_fair_pct": favpct,
        "model_prob": (pp.get("_model") or {}).get("prob"),
        "model_value": (pp.get("_model") or {}).get("value"),
        "read": rd,
        **extra,
    }


def _price_props(props: list[dict], model) -> None:
    """Stash an opponent-adjusted projection on each prop: pp['_model'] = {prob, lam, basis, value}.
    Shots/SOT/Goals scale with the team's expected goals vs THIS opponent; Passes scale with the
    team's projected POSSESSION share; all blend the player's measured per-90 rate (when banked) with
    a positional prior. Tackles/assists stay None (heuristic-only)."""
    from .model import player_rates
    rates = player_rates.build_rates()
    xg_cache: dict = {}

    def team_xg(team_key, opp_key):
        if not model or not team_key:
            return None
        key = (team_key, opp_key)
        if key not in xg_cache:
            try:
                eg = model.expected_goals(key[0], key[1])
            except Exception:  # noqa: BLE001
                eg = None
            xg_cache[key] = eg[0] if eg else None
        return xg_cache[key]

    def possession(team_key, opp_key):
        # possession proxy from relative attacking strength (more ball ≈ more passes)
        if not model or not team_key:
            return 0.5
        at = (model.attack or {}).get(team_key)
        ao = (model.attack or {}).get(opp_key)
        if not at or not ao:
            return 0.5
        return at / (at + ao)

    for pp in props:
        tk = pp.get("team_key")
        ok = normalize_team(pp.get("opponent") or "")
        pp["_model"] = propmodel.price(
            pp.get("stat_type"), pp.get("line"),
            team_xg=team_xg(tk, ok), possession=possession(tk, ok),
            position=pp.get("position"), measured=rates.get(normalize_team(pp.get("player") or "")),
        )


def generate(markets: list[Market], props: list[dict], model, cfg, smart_money: dict | None = None) -> dict:
    smart_money = smart_money or {}
    _price_props(props, model)   # model estimate on each prop (goals/shots), for the read + bundle
    # ---- favorites map from moneyline markets ----
    favorites: list[dict] = []
    fav_keys: dict[str, dict] = {}
    for m in markets:
        if m.market_type != "moneyline":
            continue
        ranked = sorted([s for s in m.selections if s.key != "draw"],
                        key=lambda s: (s.fair_prob or 0), reverse=True)
        if not ranked:
            continue
        top = ranked[0]
        if (top.fair_prob or 0) >= cfg.FAVORITE_MIN_PROB:
            opp = next((s for s in ranked if s.key != top.key), None)
            fav = {
                "team": top.label, "team_key": top.key,
                "opp": opp.label if opp else None, "opp_key": opp.key if opp else None,
                "fair_prob": round(top.fair_prob, 3),
                "model_prob": round(top.model_prob, 3) if top.model_prob is not None else None,
                "event": m.event, "commence_time": m.commence_time,
                "days_out": _days_out(m.commence_time),
                "chalk": round(top.fair_prob, 3) >= getattr(cfg, "CHALK_PROB", 0.80),
            }
            favorites.append(fav)
            fav_keys[top.key] = fav
    # daily bettor: keep today + the next few days, soonest first (not next week's chalk)
    horizon = getattr(cfg, "SLATE_HORIZON_DAYS", 4)
    favorites = [f for f in favorites
                 if f["days_out"] is None or 0 <= f["days_out"] <= horizon]
    favorites.sort(key=lambda f: (f["days_out"] if f["days_out"] is not None else 999, -f["fair_prob"]))
    fav_keys = {f["team_key"]: f for f in favorites}

    # ---- D: team total over (model) ----
    team_totals = []
    for f in favorites:
        p = model.team_total_over(f["team_key"], f["opp_key"], cfg.TEAM_TOTAL_LINE) if (model and f["opp_key"]) else None
        eg = model.expected_goals(f["team_key"], f["opp_key"]) if (model and f["opp_key"]) else None
        if p is not None:
            team_totals.append({**f, "line": cfg.TEAM_TOTAL_LINE,
                                "p_over": round(p, 3),
                                "exp_goals": round(eg[0], 2) if eg else None})
    team_totals.sort(key=lambda t: (t["days_out"] if t.get("days_out") is not None else 999, -t["p_over"]))
    tt_by_team = {t["team_key"]: t for t in team_totals}

    # ---- B: anytime goalscorer (PrizePicks "Goals"), favorites first ----
    goals = [pp for pp in props if pp.get("stat_type") == "Goals"]
    for pp in goals:
        pp["_fav"] = pp.get("team_key") in fav_keys
    goals.sort(key=lambda pp: (pp["_fav"], pp.get("popularity", 0)), reverse=True)
    goalscorers = [_prop_row(pp, fav_keys, tt_by_team, on_favorite=pp["_fav"]) for pp in goals[:12]]

    # ---- C: shots / SOT over ----
    shots = [pp for pp in props if pp.get("stat_type") in _SHOT_STATS]
    shots.sort(key=lambda pp: pp.get("popularity", 0), reverse=True)
    shots_sot = [_prop_row(pp, fav_keys, tt_by_team, on_favorite=pp.get("team_key") in fav_keys) for pp in shots[:15]]

    # ---- E: popular props (the Popular tab) ----
    popular = [pp for pp in props if pp.get("stat_type") in _POPULAR_STATS]
    popular.sort(key=lambda pp: pp.get("popularity", 0), reverse=True)
    popular_props = [_prop_row(pp, fav_keys, tt_by_team, on_favorite=pp.get("team_key") in fav_keys) for pp in popular[:20]]

    # ---- suggested SGP: top favorite + its best scorer + its team-total, PRICED as a PrizePicks
    # entry (joint prob w/ same-game correlation, payout, EV, stake) and graded structurally ----
    sgp = None
    if favorites:
        top = favorites[0]
        tt = next((t for t in team_totals if t["team_key"] == top["team_key"]), None)
        scorer = next((g for g in goalscorers if g.get("on_favorite")
                       and _team_key(g) == top["team_key"]), None)
        legs = [{"type": "moneyline", "selection": f"{top['team']} ML", "prob": top["fair_prob"],
                 "detail": f"{top['fair_prob']*100:.0f}% fair", "team_key": top["team_key"]}]
        if scorer and scorer.get("model_prob") is not None:   # only a leg we can price + grade
            legs.append({"type": "anytime_goalscorer", "selection": f"{scorer['player']} to score",
                         "prob": scorer["model_prob"], "detail": "PrizePicks Goals " + str(scorer["line"]),
                         "player": scorer["player"], "team_key": top["team_key"]})
        if tt:
            legs.append({"type": "team_total_over", "selection": f"{top['team']} Over {tt['line']}",
                         "prob": tt["p_over"], "detail": f"{tt['p_over']*100:.0f}% model",
                         "team_key": top["team_key"], "line": tt["line"]})
        if len(legs) >= 2:
            pricing = parlaymodel.price([leg.get("prob") for leg in legs])
            units = 1.0 if (pricing and pricing["ev"] > 0) else 0.5   # only size up a +EV stack
            sgp = {"event": top["event"], "commence_time": top["commence_time"],
                   "days_out": top.get("days_out"), "team_key": top["team_key"], "legs": legs,
                   "pricing": pricing, "stake_units": units,
                   "stake_dollars": round(units * cfg.BANKROLL * cfg.UNIT_PCT)}

    # per-match bundles for the AI reasoner (favorite + underdog + model + the match's props)
    bundles = []
    for f in favorites:
        keys = {f["team_key"], f.get("opp_key")}
        match_props = sorted(
            [pp for pp in props if pp.get("team_key") in keys],
            key=lambda x: x.get("popularity", 0), reverse=True)[:8]
        tt = tt_by_team.get(f["team_key"])

        def _sm(key):
            s = smart_money.get(key)
            if not s:
                return None
            return {"team": s.get("team"), "top_holder": s["top_holder"],
                    "top_holder_shares": s["top_holder_shares"], "num_holders": s["num_holders"],
                    "flow": s["flow"], "net_flow_shares": s["net_flow_shares"]}

        bundles.append({
            "match": f["event"],
            "commence_time": f["commence_time"],
            "days_out": f.get("days_out"),
            "favorite": f["team"],
            "underdog": f.get("opp"),
            "favorite_fair_pct": round(f["fair_prob"] * 100, 1),
            "favorite_model_pct": round(f["model_prob"] * 100, 1) if f["model_prob"] is not None else None,
            "team_total_over_1_5_model_pct": round(tt["p_over"] * 100, 1) if tt else None,
            "smart_money_match": {
                "favorite": _sm(f["team_key"]),
                "underdog": _sm(f.get("opp_key")),
            },
            "props": [{"player": pp["player"], "team": pp["team"], "stat": pp["stat_type"],
                       "line": pp["line"], "type": pp["odds_type"], "popularity": pp["popularity"],
                       "model_pct": round((pp.get("_model") or {}).get("prob") * 100)
                       if (pp.get("_model") or {}).get("prob") is not None else None,
                       "read": _read_for(pp, fav_keys, tt_by_team)[0]["lean"]
                       + ("/trap" if _read_for(pp, fav_keys, tt_by_team)[0]["trap_risk"] else "")}
                      for pp in match_props],
        })

    return {
        "favorite_ml": favorites[:10],
        "team_total_over": team_totals[:10],
        "anytime_goalscorer": goalscorers,
        "shots_sot": shots_sot,
        "popular_props": popular_props,
        "suggested_sgp": sgp,
        "match_bundles": bundles,
        "counts": {
            "favorites": len(favorites), "props_scanned": len(props),
        },
    }


def _team_key(prop_row: dict) -> str:
    from .matching import normalize_team
    return normalize_team(prop_row.get("team"))
