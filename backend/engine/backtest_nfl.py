"""NFL history backtest for the research layer: does each factor beat the CLOSING line?

The research layer started from published studies and added about half of each published gap. This runs
the same questions on every NFL game since 1999 from nflverse's free game file (closing spread and total
for every game, moneylines from 2006, kickoff wind and temperature for outdoor games, days of rest, and
each team's starting quarterback), against the closing price, so each factor is sized from data instead
of borrowed:

  favorite bias   de-vigged (power) closing moneyline vs how often the favorite actually won
  wind            over/under rate against the closing total, by wind band at kickoff
  rest            the more-rested team (3+ days) against the spread, before and after the 2011 labor deal
  night travel    a West Coast team at an East Coast team at night, against the spread and the price
  QB change       a team starting a different QB than its previous game, against the spread
  key numbers     how often final margins land on 3, 7, 10 since the 2015 extra-point change
  home field      the home team's average margin, by era

Every rate is compared to what the closing price implied (de-vigged where both sides' odds exist, 50% for
spreads and totals at standard juice otherwise), with a z-score and a split of the seasons into halves:
a real edge points the same way in both halves.

Run (writes backend/research_history.json, which the app shows beside each factor):
    /Users/mylesschenfield/poly/.venv/bin/python -m backend.engine.backtest_nfl
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
from datetime import date
from pathlib import Path

from .odds_math import american_to_decimal, devig_multiplicative, devig_power

SOURCE = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"
OUT = Path(__file__).resolve().parent.parent / "research_history.json"

# nflverse team codes by home time zone (franchises that moved keep their old codes: STL Rams were
# Central, SD Chargers and OAK Raiders Pacific). Arizona keeps standard time, so it plays on Pacific time.
WEST = {"SEA", "SF", "OAK", "LV", "SD", "LAC", "LA", "ARI"}
EAST = {"ATL", "BAL", "BUF", "CAR", "CIN", "CLE", "DET", "IND", "JAX", "MIA", "NE", "NYG", "NYJ", "PHI",
        "PIT", "TB", "WAS"}
WIND_BANDS = (("wind_calm", 0, 8), ("wind_8_11", 8, 12), ("wind_12_14", 12, 15), ("wind_15", 15, 20),
              ("wind_20", 20, 99))
FAV_BANDS = ((0.50, 0.60), (0.60, 0.70), (0.70, 0.75), (0.75, 0.80), (0.80, 0.90), (0.90, 1.01))


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load(path: str | None = None) -> list[dict]:
    """Completed games, numeric fields parsed. Reads `path`, or downloads nflverse's file."""
    if path:
        text = Path(path).read_text()
    else:
        import httpx
        text = httpx.get(SOURCE, follow_redirects=True, timeout=60).text
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        if _f(r.get("result")) is None:
            continue
        g = dict(r)
        for k in ("season", "week", "away_score", "home_score", "result", "total", "away_rest", "home_rest",
                  "away_moneyline", "home_moneyline", "spread_line", "away_spread_odds", "home_spread_odds",
                  "total_line", "under_odds", "over_odds", "temp", "wind"):
            g[k] = _f(r.get(k))
        out.append(g)
    return out


def fair_pair(a, b, method: str = "power") -> tuple | None:
    """De-vig two American prices -> (p_a, p_b). The power method is the app's own; the multiplicative
    one (split the vig in proportion) is the textbook baseline the favorite-bias studies compare against."""
    if a is None or b is None:
        return None
    raw = [1 / american_to_decimal(a), 1 / american_to_decimal(b)]
    pa, pb = devig_power(raw) if method == "power" else devig_multiplicative(raw)
    return pa, pb


def tally(rows: list[tuple]) -> dict:
    """rows = (hit 0/1, expected probability, season). Hit rate against the expected rate, the gap in
    probability points, its z-score, and the same gap in each half of the seasons."""
    n = len(rows)
    if not n:
        return {"n": 0}
    hits = sum(r[0] for r in rows)
    exp = sum(r[1] for r in rows)
    var = sum(r[1] * (1 - r[1]) for r in rows)
    seasons = sorted({r[2] for r in rows})
    mid = seasons[len(seasons) // 2] if seasons else 0

    def gap(sub):
        m = len(sub)
        return round((sum(r[0] for r in sub) - sum(r[1] for r in sub)) / m * 100, 2) if m else None

    return {"n": n, "hit": round(hits / n * 100, 2), "expected": round(exp / n * 100, 2),
            "gap": round((hits - exp) / n * 100, 2), "z": round((hits - exp) / math.sqrt(var), 2) if var else None,
            "ci95": round(1.96 * math.sqrt(var) / n * 100, 2) if var else None,
            "early": gap([r for r in rows if r[2] < mid]), "late": gap([r for r in rows if r[2] >= mid]),
            "seasons": f"{seasons[0]}-{seasons[-1]}" if seasons else ""}


def ats(g: dict, home: bool) -> int | None:
    """Did the home (or away) team cover the closing spread? None on a push."""
    margin = g["result"] - g["spread_line"]          # spread_line > 0: the home team was favored by it
    if margin == 0:
        return None
    return int((margin > 0) == home)


def favorite_bias(games: list[dict], method: str = "power") -> dict:
    out = {}
    rows_all = []
    for g in games:
        fp = fair_pair(g["home_moneyline"], g["away_moneyline"], method)
        if not fp or g["result"] == 0:
            continue
        home_fav = fp[0] >= fp[1]
        p = max(fp)
        won = int((g["result"] > 0) == home_fav)
        rows_all.append((won, p, int(g["season"])))
    for lo, hi in FAV_BANDS:
        out[f"{round(lo * 100)}-{round(min(hi, 1) * 100)}"] = tally([r for r in rows_all if lo <= r[1] < hi])
    out["60+"] = tally([r for r in rows_all if r[1] >= 0.60])
    out["70+"] = tally([r for r in rows_all if r[1] >= 0.70])
    out["60-70"] = tally([r for r in rows_all if 0.60 <= r[1] < 0.70])
    out["all"] = tally(rows_all)
    return out


def wind(games: list[dict]) -> dict:
    out = {}
    for key, lo, hi in WIND_BANDS:
        rows = []
        for g in games:
            if g["roof"] not in ("outdoors", "open") or g["wind"] is None or g["total_line"] is None:
                continue
            if not (lo <= g["wind"] < hi) or g["total"] == g["total_line"]:
                continue
            fp = fair_pair(g["over_odds"], g["under_odds"])
            rows.append((int(g["total"] < g["total_line"]), fp[1] if fp else 0.5, int(g["season"])))
        out[key] = tally(rows)                       # hit = the UNDER, against its (de-vigged) price
    cold = [(int(g["total"] < g["total_line"]), 0.5, int(g["season"])) for g in games
            if g["roof"] in ("outdoors", "open") and g["temp"] is not None and g["temp"] <= 32
            and g["total_line"] is not None and g["total"] != g["total_line"]]
    out["cold_32"] = tally(cold)
    out["wind_15plus"] = tally([(int(g["total"] < g["total_line"]),
                                 (fair_pair(g["over_odds"], g["under_odds"]) or (0.5, 0.5))[1], int(g["season"]))
                                for g in games if g["roof"] in ("outdoors", "open") and g["wind"] is not None
                                and g["wind"] >= 15 and g["total_line"] is not None and g["total"] != g["total_line"]])
    return out


def rest(games: list[dict]) -> dict:
    def rows(pred):
        out = []
        for g in games:
            if g["home_rest"] is None or g["away_rest"] is None or not pred(g):
                continue
            d = g["home_rest"] - g["away_rest"]
            if abs(d) < 3:
                continue
            hit = ats(g, home=d > 0)
            if hit is not None:
                out.append((hit, 0.5, int(g["season"])))
        return out
    return {"rest_pre2011": tally(rows(lambda g: g["season"] < 2011)),
            "rest_2011on": tally(rows(lambda g: g["season"] >= 2011)),
            "bye_2011on": tally(rows(lambda g: g["season"] >= 2011
                                     and max(g["home_rest"], g["away_rest"]) >= 13))}


def night_west(games: list[dict]) -> dict:
    spread_rows, ml_rows = [], []
    for g in games:
        t = g.get("gametime") or ""
        if g.get("location") != "Home" or not t or t < "20:00":
            continue
        h, a = g["home_team"], g["away_team"]
        if not ((h in WEST and a in EAST) or (a in WEST and h in EAST)):
            continue
        west_home = h in WEST
        hit = ats(g, home=west_home)
        if hit is not None:
            spread_rows.append((hit, 0.5, int(g["season"])))
        fp = fair_pair(g["home_moneyline"], g["away_moneyline"])
        if fp and g["result"] != 0:
            ml_rows.append((int((g["result"] > 0) == west_home), fp[0] if west_home else fp[1], int(g["season"])))
    return {"ats": tally(spread_rows), "ats_2013on": tally([r for r in spread_rows if r[2] >= 2013]),
            "ml": tally(ml_rows)}


def qb_change(games: list[dict]) -> dict:
    """A team starting a different quarterback than in its previous game the same season."""
    last: dict = {}
    rows, ml_rows = [], []
    for g in sorted(games, key=lambda x: (x["season"], x["gameday"], x["game_id"])):
        for side in ("home", "away"):
            team, qb = g[f"{side}_team"], g.get(f"{side}_qb_name")
            prev = last.get((g["season"], team))
            if prev and qb and prev != qb:
                hit = ats(g, home=side == "home")
                if hit is not None:
                    rows.append((hit, 0.5, int(g["season"])))
                fp = fair_pair(g["home_moneyline"], g["away_moneyline"])
                if fp and g["result"] != 0:
                    ml_rows.append((int((g["result"] > 0) == (side == "home")),
                                    fp[0] if side == "home" else fp[1], int(g["season"])))
            if qb:
                last[(g["season"], team)] = qb
    return {"ats": tally(rows), "ml": tally(ml_rows)}


def key_numbers(games: list[dict]) -> dict:
    out = {}
    for label, pred in (("1999-2014", lambda s: s < 2015), ("2015-on", lambda s: s >= 2015)):
        margins = [abs(int(g["result"])) for g in games if pred(g["season"])]
        n = len(margins)
        if not n:
            continue
        out[label] = {"n": n, **{str(k): round(sum(1 for m in margins if m == k) / n * 100, 1)
                                 for k in (1, 2, 3, 4, 6, 7, 10, 14)}}
    return out


def home_field(games: list[dict]) -> dict:
    out = {}
    for lo, hi in ((1999, 2009), (2010, 2019), (2020, 2030)):
        gs = [g for g in games if lo <= g["season"] <= hi and g.get("location") == "Home"]
        if gs:
            out[f"{lo}-{min(hi, max(int(g['season']) for g in gs))}"] = {
                "n": len(gs), "margin": round(sum(g["result"] for g in gs) / len(gs), 2),
                "spread": round(sum(g["spread_line"] for g in gs) / len(gs), 2)}
    return out


def big_spreads(games: list[dict]) -> dict:
    rows = []
    for g in games:
        if abs(g["spread_line"]) < 10:
            continue
        hit = ats(g, home=g["spread_line"] < 0)        # the underdog's side
        if hit is not None:
            rows.append((hit, 0.5, int(g["season"])))
    return {"dog_10plus": tally(rows)}


def verdict(t: dict, sign: int) -> str:
    """'holds' when the gap points the claimed way at the 95% level (z >= 1.96) and in both halves of the
    seasons; 'weak' when it points that way without that support; 'fails' otherwise."""
    if not t.get("n") or t.get("z") is None:
        return "no data"
    z, early, late = t["z"] * sign, (t.get("early") or 0) * sign, (t.get("late") or 0) * sign
    if z >= 1.96 and early > 0 and late > 0:
        return "holds"
    if z > 0:
        return "weak"
    return "fails"


def run(path: str | None = None) -> dict:
    games = load(path)
    fav = favorite_bias(games)
    w = wind(games)
    rs = rest(games)
    nw = night_west(games)
    qb = qb_change(games)
    seasons = sorted({int(g["season"]) for g in games})
    return {
        "source": SOURCE, "generated": date.today().isoformat(), "games": len(games),
        "seasons": f"{seasons[0]}-{seasons[-1]}",
        "favorite_bias": {**fav, "verdict_60": verdict(fav["60-70"], 1), "verdict_70": verdict(fav["70+"], 1)},
        "favorite_bias_multiplicative": {k: v for k, v in favorite_bias(games, "multiplicative").items()
                                         if k in ("60-70", "70+", "60+", "all")},
        "wind": {**w, **{f"verdict_{k}": verdict(w[k], -1 if k == "wind_calm" else 1)
                         for k in ("wind_calm", "wind_8_11", "wind_12_14", "wind_15", "wind_20", "wind_15plus")}},
        "rest": {**rs, "verdict": verdict(rs["rest_2011on"], 1)},
        "night_west": {**nw, "verdict": verdict(nw["ats_2013on"], 1)},
        "qb_change": {**qb, "verdict_ats": verdict(qb["ats"], -1)},
        "key_numbers": key_numbers(games),
        "home_field": home_field(games),
        "big_spreads": {**big_spreads(games), "verdict": verdict(big_spreads(games)["dog_10plus"], 1)},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--games", help="a local copy of nflverse games.csv (default: download it)")
    ap.add_argument("--output", default=str(OUT))
    args = ap.parse_args()
    res = run(args.games)
    Path(args.output).write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
