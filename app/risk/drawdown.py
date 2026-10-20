"""Equity high-water mark, daily loss and the risk state machine."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional, Tuple

from app.config import RiskCfg
from app.core.types import RiskState


@dataclass
class RiskSnapshot:
    state: RiskState
    multiplier: float
    equity: float
    balance: float
    peak_equity: float
    drawdown: float
    day: str
    day_start_equity: float
    daily_pnl: float
    daily_loss_remaining: float
    reason: str

    def to_dict(self) -> dict:
        return {k: (str(v) if isinstance(v, RiskState) else v) for k, v in self.__dict__.items()}


class DrawdownTracker:
    def __init__(self, cfg: RiskCfg, peak_equity: float = 0.0, day: Optional[str] = None,
                 day_start_equity: float = 0.0):
        self.cfg = cfg
        self.peak = peak_equity
        self.day = day
        self.day_start = day_start_equity
        self._tier: Optional[RiskState] = None     # last drawdown tier, for hysteresis

    def restore(self, saved: dict) -> None:
        self.peak = float(saved.get("peak_equity", self.peak) or 0.0)
        self.day = saved.get("day", self.day)
        self.day_start = float(saved.get("day_start_equity", self.day_start) or 0.0)

    def state_for_persistence(self) -> dict:
        return {"peak_equity": self.peak, "day": self.day, "day_start_equity": self.day_start}

    def update(self, equity: float, balance: float, now: datetime, forced: Optional[RiskState] = None,
               forced_reason: str = "") -> RiskSnapshot:
        today = now.astimezone(timezone.utc).date().isoformat()
        if self.day != today or self.day_start <= 0:
            self.day = today
            self.day_start = equity
        if equity > self.peak:
            self.peak = equity
        dd = 0.0 if self.peak <= 0 else max(0.0, 1.0 - equity / self.peak)
        daily_pnl = equity - self.day_start
        limit = self.day_start * self.cfg.daily_loss_limit_fraction
        remaining = max(0.0, limit + min(0.0, daily_pnl))
        c = self.cfg
        if dd >= c.dd_emergency:
            st, mult, why = RiskState.EMERGENCY, 0.0, f"drawdown {dd:.1%} >= {c.dd_emergency:.0%}"
        elif dd >= c.dd_no_new_trades:
            st, mult, why = RiskState.NO_NEW_TRADES, 0.0, f"drawdown {dd:.1%} >= {c.dd_no_new_trades:.0%}"
        elif remaining <= 0:
            st, mult, why = RiskState.NO_NEW_TRADES, 0.0, "daily loss limit reached"
        elif dd >= c.dd_restricted:
            st, mult, why = RiskState.RESTRICTED, c.restricted_multiplier, f"drawdown {dd:.1%}"
        elif dd >= c.dd_reduced:
            st, mult, why = RiskState.REDUCED, c.reduced_multiplier, f"drawdown {dd:.1%}"
        else:
            st, mult, why = RiskState.NORMAL, 1.0, "ok"
        # hysteresis: do not flap between tiers when equity hovers at a threshold (alert spam, size flip-flop)
        band = c.dd_recovery_band
        if st in (RiskState.NORMAL, RiskState.REDUCED) and self._tier in (RiskState.REDUCED, RiskState.RESTRICTED):
            held_thr = c.dd_restricted if self._tier is RiskState.RESTRICTED else c.dd_reduced
            if dd >= held_thr - band and (st is RiskState.NORMAL or self._tier is RiskState.RESTRICTED):
                st = self._tier
                mult = c.restricted_multiplier if st is RiskState.RESTRICTED else c.reduced_multiplier
                why = f"drawdown {dd:.1%} (recovery band)"
        self._tier = st if st in (RiskState.REDUCED, RiskState.RESTRICTED) else None
        if forced is not None:
            from app.core.types import worst_risk_state
            worst = worst_risk_state(st, forced)
            if worst != st:
                st, why = worst, forced_reason or f"forced {forced}"
                mult = 0.0 if worst in (RiskState.NO_NEW_TRADES, RiskState.EMERGENCY) else min(
                    mult, c.restricted_multiplier if worst == RiskState.RESTRICTED else c.reduced_multiplier)
        return RiskSnapshot(st, mult, equity, balance, self.peak, dd, today, self.day_start, daily_pnl,
                            remaining, why)
