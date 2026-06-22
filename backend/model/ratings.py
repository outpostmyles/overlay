"""Independent match model: empirical attack/defense strengths -> Poisson 1X2 probabilities.

This is a deliberately simple, transparent "second opinion" — NOT a market-beater on its own
(recall: even FiveThirtyEight's SPI lost to Pinnacle's closing line over 36k matches). It exists
to flag matches where an independent estimate disagrees with the market, which is worth a look.

Data: martj42 international results (free, ~50k matches, goals included). Cached in memory.
Phase 3+ upgrade path: Dixon-Coles MLE fit + xG inputs + tournament simulation for futures.
"""
from __future__ import annotations

import csv
import io
import math
import time
from pathlib import Path

import httpx

from ..matching import normalize_team

DATA_URL = "https://raw.githubusercontent.com/martj42/international_results/master/results.csv"
SINCE_YEAR = 2017
PRIOR = 6.0          # shrinkage strength toward league average (low-sample teams)
MAX_GOALS = 10
DC_RHO = -0.05       # Dixon-Coles low-score correction (fixed default)

# Raw CSV is cached to disk so cold starts (and the backtest) don't re-download.
_DATA_CACHE_PATH = Path(__file__).resolve().parent / "results_cache.csv"
_DATA_CACHE_TTL = 24 * 3600

_model_cache: tuple[float, "MatchModel | None"] | None = None
_TTL = 24 * 3600


class MatchModel:
    def __init__(self, attack: dict[str, float], defense: dict[str, float], base: float):
        self.attack = attack
        self.defense = defense
        self.base = base  # avg goals per team per match

    def _lambdas(self, a: str, b: str) -> tuple[float, float] | None:
        if a not in self.attack or b not in self.attack:
            return None
        la = self.base * self.attack[a] * self.defense[b]
        lb = self.base * self.attack[b] * self.defense[a]
        return max(0.15, min(la, 6.0)), max(0.15, min(lb, 6.0))

    def expected_goals(self, a: str, b: str) -> tuple[float, float] | None:
        """Model's expected goals for (team a, team b) on a neutral venue."""
        return self._lambdas(a, b)

    def team_total_over(self, team: str, opp: str, line: float = 1.5) -> float | None:
        """P(team scores strictly more than `line` goals) via the Poisson rate."""
        lam = self._lambdas(team, opp)
        if not lam:
            return None
        lt = lam[0]
        k = int(line)  # e.g. line 1.5 -> count P(0)+P(1), return the complement
        cdf = sum(_poisson(i, lt) for i in range(0, k + 1))
        return max(0.0, min(1.0, 1.0 - cdf))

    def match_probs(self, a: str, b: str) -> dict[str, float] | None:
        lam = self._lambdas(a, b)
        if not lam:
            return None
        la, lb = lam
        pa = [_poisson(i, la) for i in range(MAX_GOALS + 1)]
        pb = [_poisson(j, lb) for j in range(MAX_GOALS + 1)]
        p_home = p_draw = p_away = 0.0
        for i in range(MAX_GOALS + 1):
            for j in range(MAX_GOALS + 1):
                p = pa[i] * pb[j] * _dc_tau(i, j, la, lb, DC_RHO)
                if i > j:
                    p_home += p
                elif i == j:
                    p_draw += p
                else:
                    p_away += p
        total = p_home + p_draw + p_away
        if total <= 0:
            return None
        return {a: p_home / total, "draw": p_draw / total, b: p_away / total}


def _poisson(k: int, lam: float) -> float:
    return math.exp(-lam) * lam ** k / math.factorial(k)


def _dc_tau(i: int, j: int, la: float, lb: float, rho: float) -> float:
    if i == 0 and j == 0:
        return 1.0 - la * lb * rho
    if i == 0 and j == 1:
        return 1.0 + la * rho
    if i == 1 and j == 0:
        return 1.0 + lb * rho
    if i == 1 and j == 1:
        return 1.0 - rho
    return 1.0


def _fetch_csv_text() -> str | None:
    """Return the raw results CSV text, using a fresh-enough on-disk cache when present."""
    try:
        if _DATA_CACHE_PATH.exists():
            age = time.time() - _DATA_CACHE_PATH.stat().st_mtime
            if age < _DATA_CACHE_TTL:
                return _DATA_CACHE_PATH.read_text()
    except OSError:
        pass
    try:
        resp = httpx.get(DATA_URL, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        text = resp.text
    except Exception as exc:  # noqa: BLE001
        print(f"[model] data fetch failed ({exc}); model disabled")
        # Fall back to a stale cache rather than nothing.
        try:
            if _DATA_CACHE_PATH.exists():
                return _DATA_CACHE_PATH.read_text()
        except OSError:
            pass
        return None
    try:
        _DATA_CACHE_PATH.write_text(text)
    except OSError:
        pass
    return text


def load_matches(since_year: int = SINCE_YEAR) -> list[dict]:
    """Load and normalize historical matches from the (cached) martj42 dataset.

    Each returned dict has keys: date (str 'YYYY-MM-DD'), home, away (normalized
    team names), hs, as_ (int goals). Rows that can't be parsed are skipped.
    This is the single source of truth for match data — the backtest reuses it.
    """
    text = _fetch_csv_text()
    if text is None:
        return []
    rows = list(csv.DictReader(io.StringIO(text)))
    out: list[dict] = []
    for r in rows:
        date = r.get("date", "")
        if not date or date[:4].isdigit() is False or int(date[:4]) < since_year:
            continue
        try:
            hs, as_ = float(r["home_score"]), float(r["away_score"])
        except (ValueError, KeyError, TypeError):
            continue
        h = normalize_team(r.get("home_team"))
        a = normalize_team(r.get("away_team"))
        if not h or not a:
            continue
        out.append({"date": date, "home": h, "away": a, "hs": hs, "as_": as_})
    return out


def build_ratings(matches: list[dict]) -> "MatchModel | None":
    """Build a MatchModel from an already-loaded, normalized list of matches.

    Used by both production (full dataset) and the backtest (a date-bounded
    training subset). Identical math to the original _build()."""
    gf: dict[str, float] = {}
    ga: dict[str, float] = {}
    n: dict[str, int] = {}
    total_goals = 0.0
    count = 0
    for m in matches:
        h, a, hs, as_ = m["home"], m["away"], m["hs"], m["as_"]
        gf[h] = gf.get(h, 0) + hs
        ga[h] = ga.get(h, 0) + as_
        n[h] = n.get(h, 0) + 1
        gf[a] = gf.get(a, 0) + as_
        ga[a] = ga.get(a, 0) + hs
        n[a] = n.get(a, 0) + 1
        total_goals += hs + as_
        count += 1

    if count < 100:
        print("[model] insufficient data; model disabled")
        return None

    base = total_goals / (2 * count)  # avg goals per team per match
    attack: dict[str, float] = {}
    defense: dict[str, float] = {}
    for t in n:
        att_rate = (gf[t] + PRIOR * base) / (n[t] + PRIOR)
        def_rate = (ga[t] + PRIOR * base) / (n[t] + PRIOR)
        attack[t] = att_rate / base
        defense[t] = def_rate / base
    return MatchModel(attack, defense, base)


def _build() -> "MatchModel | None":
    matches = load_matches()
    if not matches:
        return None
    model = build_ratings(matches)
    if model is not None:
        print(f"[model] built from {len(matches)} matches since {SINCE_YEAR}; "
              f"{len(model.attack)} teams; base={model.base:.2f}")
    return model


def get_model() -> "MatchModel | None":
    global _model_cache
    if _model_cache and (time.monotonic() - _model_cache[0]) < _TTL:
        return _model_cache[1]
    model = _build()
    _model_cache = (time.monotonic(), model)
    return model
