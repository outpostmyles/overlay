"""The edge engine: turn a Market full of Quotes into actionable signals.

  consensus_fair_line  -> the de-vigged "true" probability per selection (from sharp sources)
  best_lines           -> the best price for each selection + where, folded onto Best Bets cards
"""
from __future__ import annotations

from ..models import Market
from . import odds_math as om


# --------------------------------------------------------------------------- #
# 1. Consensus fair line
# --------------------------------------------------------------------------- #
def consensus_fair_line(
    market: Market,
    sharp_sources: tuple[str, ...],
    method: str = "power",
) -> None:
    """Set selection.fair_prob in-place.

    For each sharp source, collect its mid probabilities across the market's selections,
    de-vig that vector, then average the de-vigged probs across sources. Prediction markets
    (Polymarket/Kalshi) are the sharp reference; sportsbooks are deliberately excluded so we
    can measure them against the sharp line.
    """
    # source -> {selection_key: mid_prob}
    per_source: dict[str, dict[str, float]] = {}
    for sel in market.selections:
        for q in sel.quotes:
            if q.source not in sharp_sources:
                continue
            p = q.mid_prob if q.mid_prob is not None else q.implied_prob
            if p and p > 0:
                per_source.setdefault(q.source, {})[sel.key] = p

    # de-vig each source's vector across the selections it priced
    devigged: dict[str, dict[str, float]] = {}
    for source, probs in per_source.items():
        keys = list(probs.keys())
        fair = om.devig([probs[k] for k in keys], method=method)
        devigged[source] = dict(zip(keys, fair))

    # average across sources
    for sel in market.selections:
        vals = [d[sel.key] for d in devigged.values() if sel.key in d]
        sel.fair_prob = sum(vals) / len(vals) if vals else None


# --------------------------------------------------------------------------- #
# 2. Best lines (line shopping)
# --------------------------------------------------------------------------- #
def best_lines(market: Market, min_fair_prob: float = 0.02) -> list[dict]:
    """For each selection, the single best (highest decimal) executable price and where.

    The EV column is shown only when the fair probability is above min_fair_prob — deep
    longshots produce meaningless triple-digit "edges" from favorite-longshot price noise.
    """
    rows = []
    for sel in market.selections:
        if not sel.quotes:
            continue
        best = max(sel.quotes, key=lambda q: q.price_decimal)
        edge_vs_fair = None
        if sel.fair_prob and sel.fair_prob >= min_fair_prob:
            edge_vs_fair = om.expected_value(sel.fair_prob, best.price_decimal)
        rows.append(
            {
                "event": market.event,
                "market_type": market.market_type,
                "selection": sel.label,
                "fair_prob": round(sel.fair_prob, 4) if sel.fair_prob else None,
                "best_price_decimal": round(best.price_decimal, 4),
                "best_american": om.decimal_to_american(best.price_decimal),
                "best_source": best.source,
                "ev": round(edge_vs_fair, 4) if edge_vs_fair is not None else None,
                "all_quotes": [q.to_dict() for q in sorted(
                    sel.quotes, key=lambda q: q.price_decimal, reverse=True)],
                "commence_time": market.commence_time,
            }
        )
    return rows
