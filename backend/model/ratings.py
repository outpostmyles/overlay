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
from collections import defaultdict
from pathlib import Path

import httpx

from ..matching import normalize_team

DATA_URL = "https://raw.githubusercontent.com/martj42/international_results/master/results.csv"
SINCE_YEAR = 2017
PRIOR = 6.0          # shrinkage strength toward league average (low-sample teams)
RATING_ITERS = 60    # opponent-adjustment fixed-point passes (ratings converge by ~40; 60 is margin)
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

    Used by both production (full dataset) and the backtest (a date-bounded training subset).

    Ratings are OPPONENT-ADJUSTED: attack[t] and defense[t] solve, by a synchronous fixed-point
    iteration, expected goals(t vs o) = base * attack[t] * defense[o]. A team's attack is its goals
    scored divided by what an average attack would have scored against the SAME defenses it faced, so
    routing minnows no longer inflates a rating the way a raw goals-per-game average does (the flaw that
    made the flat model over-credit padded qualifying records). Each rating is shrunk toward 1.0 by a
    PRIOR-game pseudo-count so thin-sample teams stay near league average. The fit is deterministic
    (Jacobi updates, no randomness), which the leak-free holdout backtest relies on."""
    teams: set[str] = set()
    total_goals = 0.0
    count = 0
    for m in matches:
        teams.add(m["home"])
        teams.add(m["away"])
        total_goals += m["hs"] + m["as_"]
        count += 1

    if count < 100:
        print("[model] insufficient data; model disabled")
        return None

    base = total_goals / (2 * count)  # avg goals per team per match
    attack: dict[str, float] = {t: 1.0 for t in teams}
    defense: dict[str, float] = {t: 1.0 for t in teams}
    for _ in range(RATING_ITERS):
        num_a: dict[str, float] = defaultdict(float)   # goals scored
        den_a: dict[str, float] = defaultdict(float)   # goals an average attack would score vs same defenses
        num_d: dict[str, float] = defaultdict(float)   # goals conceded
        den_d: dict[str, float] = defaultdict(float)   # goals an average attack would score vs this defense
        for m in matches:
            h, a, hs, as_ = m["home"], m["away"], m["hs"], m["as_"]
            num_a[h] += hs; den_a[h] += base * defense[a]
            num_a[a] += as_; den_a[a] += base * defense[h]
            num_d[h] += as_; den_d[h] += base * attack[a]
            num_d[a] += hs; den_d[a] += base * attack[h]
        for t in teams:
            attack[t] = (num_a[t] + PRIOR * base) / (den_a[t] + PRIOR * base)    # shrinks toward 1.0
            defense[t] = (num_d[t] + PRIOR * base) / (den_d[t] + PRIOR * base)
        # Pin the scale degeneracy: attack[a]*defense[o] is invariant under attack*=k, defense/=k, so
        # without this the absolute ratings drift forever (predictions converge, the numbers don't).
        # Normalize mean attack to 1.0 and rescale defense inversely (products, and predictions, unchanged).
        mean_a = sum(attack.values()) / len(attack)
        for t in teams:
            attack[t] /= mean_a
            defense[t] *= mean_a
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
