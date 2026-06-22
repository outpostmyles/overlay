"""Calibration memory — learns slowly from the paper-trade ledger to nudge AI confidence.

SAFE BY DESIGN (the user's fear: discounting a bet type because a similar one lost):
  - HARD GATE: a context bucket gets n_eff >= 20 before it can affect ANYTHING. Below that its
    score is None -> zero effect. A short cold streak is structurally incapable of doing harm.
  - HEAVY SHRINKAGE toward a neutral baseline (K=40): even past the gate, the signal is pulled
    most of the way back to "no opinion" until a lot of data accrues.
  - CLV-PRIMARY (beating the close predicts profit better than noisy W/L); W/L only for props.
  - ASYMMETRIC CAP: a hot bucket can lift confidence at most +1.0 notch; a cold one cuts at most
    -0.5. Buckets a pick belongs to are AVERAGED, never summed.
  - It only nudges Haiku's advisory 1-5 confidence (re-ranking Picks/Parlay of the Day) and shows
    a note. It NEVER drops, hides, or flips a pick, and never edits a price/fair/CLV.
  - Recomputed every snapshot from the immutable ledger (no stored state); regrading self-heals.
  - PROVABLY INERT until real graded/closed data crosses the gate.
"""
from __future__ import annotations

from . import config
from .store import paper

LAMBDA = 0.34       # ~3 settled W/L observations ≈ 1 CLV observation
GATE = 20.0         # n_eff below this → bucket is NULL (zero effect)
K = 40.0            # shrinkage prior strength toward baseline
# fallback W/L baselines when a bucket has no priced fair to anchor to
_WL_BASELINE = {"anytime_goalscorer": 0.50, "shots_sot": 0.50,
                "popular_prop": 0.50, "team_total_over": 0.55, "favorite_ml": 0.65}


def _pop_band(p):
    p = p or 0
    return "hot" if p > 20000 else "warm" if p >= 5000 else "cold"


def _agree_band(pp):
    if pp is None:
        return None
    g = abs(pp)
    return "tight" if g < 3 else "mid" if g <= 7 else "wide"


def _pick_buckets(p: dict) -> list[tuple]:
    """The 1-D context buckets a logged pick belongs to (never crossed)."""
    keys = [("archetype", p.get("archetype"))]
    if p.get("odds_type"):
        keys.append(("odds_type", p["odds_type"]))
    if p.get("popularity") is not None:
        keys.append(("popularity", _pop_band(p["popularity"])))
    if p.get("on_favorite") is not None:
        keys.append(("on_favorite", "fav" if p["on_favorite"] else "dog"))
    b = _agree_band(p.get("agreement_pp"))
    if b:
        keys.append(("agreement", b))
    return [k for k in keys if k[1] is not None]


def compute() -> dict:
    """Aggregate the ledger into per-bucket calibration stats (gated + shrunk)."""
    if not getattr(config, "ENABLE_MEMORY", True):
        return {}
    acc: dict = {}
    for p in paper.list_picks():
        for key in _pick_buckets(p):
            d = acc.setdefault(key, {"clv": [], "settled": 0, "wins": 0, "fairs": []})
            if p.get("clv_pct") is not None:
                d["clv"].append(p["clv_pct"])
            if p.get("status") in ("won", "lost"):
                d["settled"] += 1
                d["wins"] += 1 if p["status"] == "won" else 0
            if p.get("pick_fair_prob") is not None:
                d["fairs"].append(p["pick_fair_prob"])

    stats = {}
    for key, d in acc.items():
        n_clv, n_settled = len(d["clv"]), d["settled"]
        n_eff = n_clv + LAMBDA * n_settled
        out = {"n_clv": n_clv, "n_settled": n_settled, "n_eff": round(n_eff, 1),
               "gated": False, "metric": None, "score": None, "deviation": 0.0}
        if n_eff >= GATE:
            if n_clv >= GATE:                       # CLV signal (favorite_ml today)
                mean_clv = sum(d["clv"]) / n_clv
                shrunk = (n_clv / (n_clv + K)) * mean_clv     # baseline 0 (assume no edge)
                out.update(gated=True, metric="clv", score=round(shrunk, 2),
                           deviation=shrunk / 5.0)            # +5% CLV ⇒ +1.0 notch
            elif n_settled >= GATE:                 # W/L signal (props, once graded)
                rate = d["wins"] / n_settled
                base = (sum(d["fairs"]) / len(d["fairs"])) if d["fairs"] else _WL_BASELINE.get(key[1], 0.5)
                post = (base * K + d["wins"]) / (K + n_settled)
                out.update(gated=True, metric="wl", score=round((post - base) * 100, 1),
                           deviation=(post - base) / 0.10)    # +10pp ⇒ +1.0 notch
        stats[key] = out
    return stats


def adjust(verdict: dict, bundle: dict, stats: dict) -> None:
    """Nudge ONE match verdict's confidence using its gated buckets. In-place; safe at n=0."""
    if not getattr(config, "ENABLE_MEMORY", True) or not stats:
        return
    keys = {("archetype", b.get("archetype")) for b in verdict.get("recommended_bets", [])}
    ff, fm = bundle.get("favorite_fair_pct"), bundle.get("favorite_model_pct")
    if ff is not None and fm is not None:
        b = _agree_band(ff - fm)
        if b:
            keys.add(("agreement", b))
    for bet in verdict.get("recommended_bets", []):
        if bet.get("archetype") in ("anytime_goalscorer", "shots_sot", "popular_prop"):
            pr = _match_prop(bet.get("selection", ""), bundle.get("props", []))
            if pr:
                if pr.get("type"):
                    keys.add(("odds_type", pr["type"]))
                if pr.get("popularity") is not None:
                    keys.add(("popularity", _pop_band(pr["popularity"])))
                keys.add(("on_favorite", "fav" if pr.get("team") == bundle.get("favorite") else "dog"))

    active = [k for k in keys if k in stats and stats[k]["gated"]]
    devs = [stats[k]["deviation"] for k in active]
    if not devs:
        return
    raw = sum(devs) / len(devs)
    capped = max(-0.5, min(1.0, raw))            # asymmetric: cold streaks move half as much
    conf = verdict.get("confidence") or 3
    adj = max(1, min(5, round(conf + capped)))
    if adj == conf:
        return
    verdict["confidence_raw"] = conf
    verdict["confidence"] = adj
    top = max(active, key=lambda k: abs(stats[k]["deviation"]))
    st = stats[top]
    detail = (f"{st['score']:+.1f}% CLV" if st["metric"] == "clv" else f"{st['score']:+.1f}pp vs base")
    n = st["n_clv"] if st["metric"] == "clv" else st["n_settled"]
    verdict["memory_note"] = f"Confidence {conf}→{adj}: {top[0]} '{top[1]}' {detail} over {n} tracked."


def panel(stats: dict) -> list[dict]:
    """Bucket rows for the Track-tab calibration panel (incl. below-gate 'learning' buckets)."""
    rows = []
    for (dim, val), st in sorted(stats.items()):
        rows.append({
            "dim": dim, "val": val, "n_eff": st["n_eff"], "gated": st["gated"],
            "metric": st["metric"], "score": st["score"],
            "status": "active" if st["gated"] else f"learning ({int(st['n_eff'])}/{int(GATE)})",
        })
    rows.sort(key=lambda r: (not r["gated"], -abs(r["score"] or 0)))
    return rows


def _match_prop(selection: str, props: list[dict]) -> dict | None:
    s = (selection or "").lower()
    for pr in props:
        name = (pr.get("player") or "").lower()
        if name and (name in s or any(tok in s for tok in name.split() if len(tok) > 3)):
            return pr
    return None
