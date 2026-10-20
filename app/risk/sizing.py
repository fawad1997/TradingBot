"""Position sizing = MIN(edge, volatility, portfolio, correlation, drawdown,
liquidity capacity, broker, margin budgets). Never rounds UP to the broker
minimum: if the minimum lot is more risk than the budgets allow -> NO TRADE."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional

from app.config import MAX_RISK_FRACTION_PER_TRADE, RiskCfg
from app.core.types import Side, SymbolSpec


@dataclass
class SizingInputs:
    equity: float
    free_margin: float
    used_margin: float
    side: Side
    entry: float
    sl: float
    edge_risk_fraction: float      # from fractional Kelly (validated) or research fraction
    risk_multiplier: float         # drawdown x volatility x strategy health
    open_risk_money: float         # risk still open in other positions
    daily_loss_remaining: float    # money still allowed to lose today
    commission_per_lot: float = 0.0


@dataclass
class SizingResult:
    feasible: bool
    volume: float
    risk_money: float
    risk_fraction: float
    loss_per_lot: float
    min_volume_risk: float
    reason: str
    budgets: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"feasible": self.feasible, "volume": self.volume, "risk_money": round(self.risk_money, 4),
                "risk_fraction": round(self.risk_fraction, 5), "loss_per_lot": round(self.loss_per_lot, 4),
                "min_volume_risk": round(self.min_volume_risk, 4), "reason": self.reason, "budgets": self.budgets}


def size_position(spec: SymbolSpec, inp: SizingInputs, cfg: RiskCfg,
                  calc_profit: Optional[Callable[[Side, float, float, float], Optional[float]]] = None,
                  calc_margin: Optional[Callable[[Side, float, float], Optional[float]]] = None) -> SizingResult:
    stop = abs(inp.entry - inp.sl)
    if stop <= 0 or inp.equity <= 0:
        return SizingResult(False, 0.0, 0.0, 0.0, 0.0, 0.0, "INVALID_STOP_OR_EQUITY")
    loss = None
    if calc_profit is not None:
        pr = calc_profit(inp.side, 1.0, inp.entry, inp.sl)
        if pr is not None and math.isfinite(pr):
            loss = abs(pr)
    if not loss:
        loss = spec.loss_per_lot(stop)
    loss_per_lot = loss + inp.commission_per_lot
    if loss_per_lot <= 0:
        return SizingResult(False, 0.0, 0.0, 0.0, 0.0, 0.0, "LOSS_PER_LOT_UNKNOWN")

    mult = max(0.0, inp.risk_multiplier)
    b_edge = inp.equity * inp.edge_risk_fraction * mult
    b_cap = inp.equity * min(cfg.max_risk_fraction_per_trade, MAX_RISK_FRACTION_PER_TRADE) * mult
    b_port = inp.equity * cfg.max_total_open_risk_fraction - inp.open_risk_money
    b_corr = b_port            # single instrument: correlation budget == portfolio budget
    b_daily = inp.daily_loss_remaining
    budget = min(b_edge, b_cap, b_port, b_corr, b_daily)
    vol_risk = budget / loss_per_lot if budget > 0 else 0.0

    vol_margin = vol_ml = float("inf")
    margin_per_lot = calc_margin(inp.side, 1.0, inp.entry) if calc_margin else None
    if margin_per_lot and margin_per_lot > 0:
        vol_margin = inp.free_margin * cfg.max_margin_usage_fraction / margin_per_lot
        # keep margin level after the trade above the configured floor
        allowed_total_margin = inp.equity * 100.0 / cfg.min_margin_level_after_pct
        vol_ml = max(0.0, (allowed_total_margin - inp.used_margin) / margin_per_lot)
    vol_cap = min(cfg.max_volume, spec.volume_max)
    raw = min(vol_risk, vol_margin, vol_ml, vol_cap)
    volume = spec.floor_volume(raw)
    min_vol_risk = loss_per_lot * spec.volume_min
    budgets = {"edge": round(b_edge, 4), "hard_cap": round(b_cap, 4), "portfolio": round(b_port, 4),
               "correlation": round(b_corr, 4), "daily_loss": round(b_daily, 4), "binding_money": round(budget, 4),
               "vol_by_risk": round(vol_risk, 6), "vol_by_margin": None if vol_margin == float("inf") else round(vol_margin, 6),
               "vol_by_margin_level": None if vol_ml == float("inf") else round(vol_ml, 6),
               "vol_capacity": vol_cap, "margin_per_lot": margin_per_lot, "risk_multiplier": mult}
    if volume < spec.volume_min - 1e-12:
        if budget <= 0:
            reason = "RISK_BUDGET_EXHAUSTED"
        elif vol_risk <= min(vol_margin, vol_ml):
            reason = "POSITION_SIZE_INFEASIBLE"
        else:
            reason = "MARGIN_INFEASIBLE"
        return SizingResult(False, 0.0, 0.0, 0.0, loss_per_lot, min_vol_risk, reason, budgets)
    risk_money = volume * loss_per_lot
    return SizingResult(True, volume, risk_money, risk_money / inp.equity, loss_per_lot, min_vol_risk, "OK", budgets)
