"""Normalized data model shared across all sources and the engine.

Every source (Polymarket, Kalshi, sportsbooks) is adapted into Markets -> Selections -> Quotes
so the engine never has to care where a price came from.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Quote:
    """One price for one selection from one source."""
    source: str                      # 'polymarket' | 'kalshi' | 'draftkings' | ...
    source_type: str                 # 'prediction_market' | 'sportsbook'
    price_decimal: float             # executable decimal odds to BACK this selection
    implied_prob: float              # 1 / price_decimal (raw, includes vig/spread)
    mid_prob: Optional[float] = None  # best no-vig-ish estimate from this source (mid of bid/ask)
    fee: float = 0.0                 # est. fee fraction on winnings (prediction markets)
    bid: Optional[float] = None      # prediction-market bid (probability)
    ask: Optional[float] = None      # prediction-market ask (probability)
    volume: Optional[float] = None
    link: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "source_type": self.source_type,
            "price_decimal": round(self.price_decimal, 4),
            "american": _american(self.price_decimal),
            "implied_prob": round(self.implied_prob, 4),
            "mid_prob": round(self.mid_prob, 4) if self.mid_prob is not None else None,
            "fee": self.fee,
            "volume": self.volume,
            "link": self.link,
        }


@dataclass
class Selection:
    key: str                         # normalized key (team name, 'over_2.5', etc.)
    label: str
    quotes: list[Quote] = field(default_factory=list)
    fair_prob: Optional[float] = None  # consensus de-vigged true probability
    model_prob: Optional[float] = None  # our model's probability (optional)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "fair_prob": round(self.fair_prob, 4) if self.fair_prob is not None else None,
            "model_prob": round(self.model_prob, 4) if self.model_prob is not None else None,
            "quotes": [q.to_dict() for q in self.quotes],
        }


@dataclass
class Market:
    market_id: str                   # normalized id
    event: str                       # 'USA vs Australia' | 'World Cup Winner'
    market_type: str                 # 'winner_outright'|'moneyline'|'totals'|'spread'|'advance_*'
    selections: list[Selection] = field(default_factory=list)
    commence_time: Optional[str] = None
    group: Optional[str] = None      # 'Futures' | 'Matches'

    def to_dict(self) -> dict:
        return {
            "market_id": self.market_id,
            "event": self.event,
            "market_type": self.market_type,
            "commence_time": self.commence_time,
            "group": self.group,
            "selections": [s.to_dict() for s in self.selections],
        }


def _american(dec: float) -> int:
    if dec <= 1.0:
        return 0
    if dec >= 2.0:
        return round((dec - 1.0) * 100)
    return round(-100.0 / (dec - 1.0))
