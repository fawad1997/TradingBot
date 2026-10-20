"""Execution cost model (section 19).

net edge (R) = gross expected edge (R)
             - spread - commission - expected slippage - swap/financing - adverse selection
All costs are expressed both in account currency per lot and in R (fraction of the
planned stop distance) so they can be compared to the edge directly.
"""
from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

from app.config import CostCfg
from app.core.types import Side, SymbolSpec, TradePlan


@dataclass
class CostEstimate:
    spread_price: float
    slippage_price: float
    commission_per_lot: float
    swap_per_lot: float
    adverse_selection_price: float
    loss_per_lot_at_stop: float
    cost_r: float
    detail: dict

    def to_dict(self) -> dict:
        return {"spread_price": self.spread_price, "slippage_price": self.slippage_price,
                "commission_per_lot": self.commission_per_lot, "swap_per_lot": self.swap_per_lot,
                "adverse_selection_price": self.adverse_selection_price, "cost_r": round(self.cost_r, 4),
                **self.detail}


class SlippageModel:
    """Empirical slippage from our own fills (points, positive = adverse)."""

    def __init__(self, default_points: float, min_samples: int, maxlen: int = 500):
        self.default = default_points
        self.min_samples = min_samples
        self.samples: Deque[float] = deque(maxlen=maxlen)

    def add(self, points: float) -> None:
        self.samples.append(float(points))

    def expected_points(self) -> float:
        if len(self.samples) >= self.min_samples:
            s = sorted(self.samples)
            # 75th percentile of adverse slippage: conservative but data-driven
            return max(0.0, s[int(0.75 * (len(s) - 1))])
        return self.default

    def stats(self) -> dict:
        if not self.samples:
            return {"n": 0, "expected_points": self.expected_points()}
        return {"n": len(self.samples), "mean": statistics.fmean(self.samples),
                "median": statistics.median(self.samples), "expected_points": self.expected_points()}


class CostModel:
    def __init__(self, cfg: CostCfg, slippage: Optional[SlippageModel] = None):
        self.cfg = cfg
        self.slippage = slippage or SlippageModel(cfg.default_slippage_points, cfg.slippage_sample_min)

    def estimate(self, plan: TradePlan, spec: SymbolSpec, current_spread_price: float,
                 expected_hold_nights: float = 0.0, adverse_selection_price: float = 0.0) -> CostEstimate:
        stop = plan.stop_distance
        loss_per_lot = spec.loss_per_lot(stop) if stop > 0 else 0.0
        value_per_price_per_lot = (spec.tick_value_loss or spec.tick_value) / spec.tick_size if spec.tick_size else 0.0
        slip_price = self.slippage.expected_points() * spec.point
        # The plan's entry is already ask (BUY) / bid (SELL) and the stop is placed on the
        # exit side, so the ENTRY spread is inside the stop distance. What is NOT inside R:
        # entry slippage, exit slippage on the stop, commission, swap, adverse selection.
        commission = self.cfg.commission_per_lot_round_trip
        swap_pts = spec.swap_long if plan.direction is Side.BUY else spec.swap_short
        swap_per_lot = 0.0
        if spec.swap_mode == 1 and expected_hold_nights > 0:   # swap in points
            swap_per_lot = -min(0.0, swap_pts) * spec.point * value_per_price_per_lot * expected_hold_nights
        money_costs_per_lot = (2 * slip_price + adverse_selection_price) * value_per_price_per_lot \
            + commission + swap_per_lot
        cost_r = money_costs_per_lot / loss_per_lot if loss_per_lot > 0 else float("inf")
        return CostEstimate(spread_price=current_spread_price, slippage_price=slip_price,
                            commission_per_lot=commission, swap_per_lot=swap_per_lot,
                            adverse_selection_price=adverse_selection_price, loss_per_lot_at_stop=loss_per_lot,
                            cost_r=cost_r, detail={"spread_r": current_spread_price / stop if stop else None,
                                                   "stop_distance": stop})
